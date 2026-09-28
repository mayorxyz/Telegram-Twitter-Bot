"""Markdown -> plain-text conversion for X/Twitter, plus char-count helpers."""
from __future__ import annotations

import re

from utils.config import X_CHAR_LIMIT


def strip_markdown(text: str) -> str:
    """Convert Telegram/markdown styling into plain text suitable for an X post."""
    if not text:
        return ""
    out = text
    # links: [label](url) -> "label url"
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 \2", out)
    # bare markdown heading markers
    out = re.sub(r"^#{1,6}\s+", "", out, flags=re.MULTILINE)
    # blockquotes
    out = re.sub(r"^>\s?", "", out, flags=re.MULTILINE)
    # code fences / inline code keep their content only
    out = re.sub(r"```[a-zA-Z0-9_+-]*\n?", "", out)
    out = re.sub(r"`([^`]*)`", r"\1", out)
    # bold / italic / strikethrough / spoiler / underline markers
    out = re.sub(r"(\*\*\*|___)(.+?)\1", r"\2", out, flags=re.DOTALL)
    out = re.sub(r"(\*\*|__)(.+?)\1", r"\2", out, flags=re.DOTALL)
    out = re.sub(r"(?<!\w)([*_])([^*_\n]+)\1(?!\w)", r"\2", out)
    out = re.sub(r"~~(.+?)~~", r"\1", out, flags=re.DOTALL)
    out = re.sub(r"\|\|(.+?)\|\|", r"\1", out, flags=re.DOTALL)
    # escape leftovers
    out = out.replace("\\", "")
    # collapse >2 consecutive newlines
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def twitter_len(text: str) -> int:
    """X/Twitter weighted length: every URL counts as exactly 23 chars.

    This is the single source of truth for "how long will X think this is".
    ``effective_length`` is kept as the historical name and delegates here so
    the 280-char limit checks, previews and the publish guard all agree.
    """
    if not text:
        return 0

    def _url_len(m: "re.Match") -> str:
        return "x" * 23

    counted = re.sub(r"https?://\S+", _url_len, text)
    return len(counted)


def effective_length(text: str) -> int:
    """X counts URLs as 23 chars regardless of real length."""
    return twitter_len(text)


def truncate_to_limit(text: str, limit: int = 280, ellipsis: str = "...") -> str:
    """Hard-truncate ``text`` to ``limit`` twitter-weighted chars at a word
    boundary, appending ``ellipsis`` when trimming was needed.

    Safety net so we never push an over-280 string to X. The budget accounts
    for the ellipsis; whole words are dropped rather than cut mid-word. URLs
    are weighed as 23 chars (via ``twitter_len``) while choosing the cut.
    """
    if twitter_len(text) <= limit:
        return text
    budget = max(0, limit - len(ellipsis))
    words = text.split()
    candidate = ""
    for w in words:
        trial = (candidate + " " + w) if candidate else w
        if twitter_len(trial) > budget:
            break
        candidate = trial
    if not candidate:
        # A single over-budget token (e.g. one giant URL) — hard-cut by chars.
        candidate = text[:budget]
    return candidate + ellipsis


def over_limit(text: str) -> bool:
    return effective_length(text) > X_CHAR_LIMIT


def display_content(content: str, media_count: int = 0) -> str:
    """Card/dashboard body for a post: media-only posts get a readable placeholder.

    `content` stays the source of truth for publishing (a caption-less media
    tweet is published with empty text + attachments), so this only ever feeds
    the previews shown to the admin.
    """
    text = (content or "").strip()
    if text:
        return content or ""
    return "📎 Media post" if media_count else ""


def format_preview(post) -> str:
    """Build the HTML preview card shown in Telegram for a post row."""
    from datetime import timezone

    from db import crud
    from utils.timeutil import to_admin_tz

    status_emoji = {
        "draft": "📝 draft",
        "scheduled": "🕐 scheduled",
        "published": "✅ published",
        "failed": "❌ failed",
    }.get(post.status, post.status)

    lines = [f"<b>Post #{post.id}</b>  ·  {status_emoji}"]

    if getattr(post, "thread", None) is not None:
        # threads get their own per-tweet layout (char counts + media badges)
        body = format_thread_preview(post)
        return f"{lines[0]}\n{body}"

    if post.tag:
        lines.append(f"🏷 <code>{post.tag}</code>")
    body = post.content or ""
    media = list(getattr(post, "media", None) or [])
    shown = display_content(body, len(media))
    shown = shown if len(shown) <= 600 else shown[:600] + "…"
    lines.append("")
    lines.append(f"<pre>{escape_html(shown)}</pre>")

    final_text = strip_markdown(body) if post.auto_format else body
    n = effective_length(final_text)
    warn = " ⚠️ <i>(will be trimmed on publish — over limit!)</i>" if n > X_CHAR_LIMIT else ""
    lines.append(f"📏 {n}/{X_CHAR_LIMIT} chars (final){warn}")
    if media:
        n_media = len(media)
        lines.append(f"📎 {n_media} media file{'s' if n_media > 1 else ''} attached")
    if post.scheduled_at and post.status == "scheduled":
        loc = to_admin_tz(post.scheduled_at.replace(tzinfo=timezone.utc))
        lines.append(f"🕐 fires at {loc:%Y-%m-%d %H:%M} ({crud.get_setting('timezone', 'UTC')})")
    if post.published_at:
        lines.append(f"🚀 published {to_admin_tz(post.published_at.replace(tzinfo=timezone.utc)):%Y-%m-%d %H:%M}")
    if post.status == "failed" and post.error:
        lines.append(f"⚠️ <i>{escape_html(post.error[:200])}</i>")
    return "\n".join(lines)


# ------------------------------------------------------------- threads ------

def thread_overview(post) -> str:
    """Multi-line HTML overview of a thread (or the single-tweet body)."""
    lines: list[str] = []
    if getattr(post, "thread", None) is not None:
        for tt in sorted(post.thread.tweets, key=lambda t: t.position):
            snippet = escape_html((tt.content or "").replace("\n", " ")[:120])
            n_media = len(tt.media_ids or [])
            media_note = f"  📎×{n_media}" if n_media else ""
            done = " ✅" if tt.tweet_id else ""
            lines.append(f"<b>{tt.position + 1}.</b> {snippet}{media_note}{done}")
        return "\n".join(lines)
    body = post.content or ""
    shown = body if len(body) <= 600 else body[:600] + "…"
    return f"<pre>{escape_html(shown)}</pre>"


def format_thread_preview(post) -> str:
    """Full preview card body for a thread draft (used inside format_preview)."""
    from datetime import timezone as _tz

    from db import crud as _crud
    from utils.timeutil import to_admin_tz as _to_loc

    tweets = sorted(post.thread.tweets, key=lambda t: t.position)
    lines = [f"🧵 <b>Thread · {len(tweets)} tweets</b>"]
    if post.tag:
        lines.append(f"🏷 <code>{post.tag}</code>")
    lines.append("")
    total = 0
    over_any = False
    for tt in tweets:
        final = strip_markdown(tt.content or "") if post.auto_format else (tt.content or "")
        n = effective_length(final.strip())
        total += n
        warn = " ⚠️" if n > X_CHAR_LIMIT else ""
        if n > X_CHAR_LIMIT:
            over_any = True
        n_media = len(tt.media_ids or [])
        media_note = f"  📎×{n_media}" if n_media else ""
        snippet = escape_html(display_content(tt.content, n_media).replace("\n", " ")[:90])
        lines.append(f"<b>{tt.position + 1}.</b> [{n}/{X_CHAR_LIMIT}]{warn}{media_note} {snippet}")
    lines.append("")
    lines.append(f"📏 {total} chars total across {len(tweets)} tweets")
    if over_any:
        lines.append("⚠️ <i>At least one tweet is over the 280 limit!</i>")
    if post.scheduled_at and post.status == "scheduled":
        loc = _to_loc(post.scheduled_at.replace(tzinfo=_tz.utc))
        lines.append(f"🕐 fires at {loc:%Y-%m-%d %H:%M} "
                     f"({_crud.get_setting('timezone', 'UTC')})")
    if post.published_at:
        lines.append(f"🚀 published {_to_loc(post.published_at.replace(tzinfo=_tz.utc)):%Y-%m-%d %H:%M}")
    if post.status == "failed" and post.error:
        lines.append(f"⚠️ <i>{escape_html(post.error[:200])}</i>")
    return "\n".join(lines)


def escape_html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
