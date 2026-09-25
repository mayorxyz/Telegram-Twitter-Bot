"""RSS polling -> draft generation (feedparser + heuristics)."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timezone as dt_timezone
from html import unescape

import feedparser

from db import crud
from utils import config

log = logging.getLogger(__name__)


def _seen_key(link: str) -> str:
    return "rss_seen_" + hashlib.sha1(link.encode()).hexdigest()[:16]


def already_processed(link: str) -> bool:
    return crud.get_setting(_seen_key(link), "") == "1"


def mark_processed(link: str) -> None:
    crud.set_setting(_seen_key(link), "1")


def today_draft_count() -> int:
    raw = crud.get_setting("rss_count_today", "")
    try:
        stored_date, n = raw.split("|", 1)
        if stored_date == date.today().isoformat():
            return int(n)
    except (ValueError, AttributeError):
        pass
    return 0


def bump_today_count() -> None:
    crud.set_setting("rss_count_today", f"{date.today().isoformat()}|{today_draft_count() + 1}")


_TAG_PATTERNS = [
    ("crypto", r"\b(bitcoin|btc|ethereum|eth|crypto|token|blockchain|solana|defi|nft|altcoin)\b"),
    ("AI", r"\b(ai|gpt|llm|openai|anthropic|claude|machine learning|neural|model|agent)\b"),
    ("meme", r"\b(meme|viral|funny|lol|dank)\b"),
]


def guess_tag(text: str) -> str:
    low = text.lower()
    for tag, pat in _TAG_PATTERNS:
        if re.search(pat, low):
            return tag
    return "other"


def clean_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def make_draft_content(entry) -> str:
    """Generate a tweet-length draft from an RSS entry."""
    title = clean_html(getattr(entry, "title", "")) or "New post"
    link = getattr(entry, "link", "")
    summary = clean_html(getattr(entry, "summary", ""))

    # hook sentence from the summary
    sentence = re.split(r"(?<=[.!?])\s+", summary)[0] if summary else ""
    sentence = sentence[:140].rstrip()
    if len(sentence) == 140 and " " in sentence:
        sentence = sentence[: sentence.rfind(" ")]

    body = f"📰 {title}"
    if sentence and sentence.lower() != title.lower():
        body += f"\n\n{sentence}"
    # URLs count as 23 chars on X — keep within 280 effective chars
    while len(body) + 1 + 23 > config.X_CHAR_LIMIT and "\n" in body:
        body = body.rsplit("\n", 1)[0]
    if link:
        body += f"\n{link}"
    return body


def fetch_new_items() -> list[dict]:
    """Return up to remaining-quota new RSS items as dicts {title, content, link, tag}."""
    url = crud.get_setting("rss_url", "") or config.RSS_URL
    if not url:
        return []
    max_day = config.RSS_MAX_PER_DAY
    done = today_draft_count()
    remaining = max(0, max_day - done)
    if remaining == 0:
        log.debug("RSS daily quota reached (%d/%d)", done, max_day)
        return []

    try:
        feed = feedparser.parse(url)
    except Exception:
        log.exception("RSS parse failed")
        return []

    items: list[dict] = []
    for e in feed.entries:
        if len(items) >= remaining:
            break
        link = getattr(e, "link", "")
        if not link or already_processed(link):
            continue
        content = make_draft_content(e)
        items.append({
            "title": clean_html(getattr(e, "title", "")),
            "content": content,
            "link": link,
            "tag": guess_tag(f"{e.get('title', '')} {e.get('summary', '')}"),
        })
        mark_processed(link)  # claim immediately so we don't loop on failures
    return items
