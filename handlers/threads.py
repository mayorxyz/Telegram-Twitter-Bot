"""Thread builder: /thread command + `th:*` callbacks.

A thread is stored as a Thread row (1:1 with a Post) plus ThreadTweet rows.
Media is attached per tweet via the "attach slot" flow:

    (no flow) + media                 -> prompts: Single tweet / New thread / Cancel
    th:mpick:<post_id>                -> prompt to send media
    (admin sends photo/video)         -> file downloaded, prompts for target tweet
    thm:<post_id>:<position>          -> attaches media to selected tweet
    th:rmm:<post_id>:<slot_index>     -> remove one file from a tweet
    th:set:<post_id>:<position>       -> replace one tweet's text inline
    th:add / th:rm:<post_id>:<pos>    -> append / drop tweets (max 25, min 2)

All state lives in SQLite; nothing here touches the X API.
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import crud
from utils import config
from utils.formatting import escape_html
from utils.ui import is_admin, show_preview

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers -

def _tweets_of(post) -> list[dict]:
    """Current thread as a plain list of {content, media_ids} dicts."""
    th = crud.get_thread(post.id)
    if th is None:
        return [{"content": post.content or "", "media_ids": []}]
    return [{"content": t.content, "media_ids": list(t.media_ids or [])}
            for t in sorted(th.tweets, key=lambda x: x.position)]


def _render_tweets(tweets: list[dict]) -> str:
    lines = [f"🧵 <b>Thread · {len(tweets)} tweets</b>", ""]
    for i, t in enumerate(tweets):
        content = t["content"] or ""
        n_media = len(t["media_ids"])
        badge = f"  📎×{n_media}" if n_media else ""
        snippet = escape_html(content.replace("\n", " ")[:90])
        empty = "<i>(empty)</i>" if not content.strip() else snippet
        lines.append(f"<b>{i + 1}.</b> {empty}{badge}")
    return "\n".join(lines)


def _action_keyboard(post_id: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("➕ Add tweet", callback_data=f"th:add:{post_id}"),
        InlineKeyboardButton("✏️ Set text", callback_data=f"th:setpick:{post_id}"),
    ], [
        InlineKeyboardButton("🖼 Attach media", callback_data=f"th:mpick:{post_id}"),
        InlineKeyboardButton("🗑 Remove media", callback_data=f"th:rmpick:{post_id}"),
    ], [
        InlineKeyboardButton("🗑 Drop tweet", callback_data=f"th:rmpickc:{post_id}"),
    ]]
    return InlineKeyboardMarkup(rows)


async def _show_builder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, post,
                        note: str = "", force_new_message: bool = False) -> None:
    """Re-render the builder card for a thread draft."""
    tweets = _tweets_of(post)
    html = _render_tweets(tweets)
    if note:
        html = f"{note}\n\n{html}"
    
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton("✅ Done — preview draft", callback_data=f"th:done:{post.id}"),
        InlineKeyboardButton("🚀 Post Now", callback_data=f"th:postnow:{post.id}"),
    ]]
    kb = InlineKeyboardMarkup(rows + list(_action_keyboard(post.id).inline_keyboard))
    
    if not force_new_message and post.tg_message_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=post.tg_message_id,
                                                text=html, reply_markup=kb, parse_mode="HTML")
            context.user_data["active_thread_post_id"] = post.id
            return
        except Exception:
            crud.update_post(post.id, tg_message_id=None)
            post.tg_message_id = None
            
    sent = await context.bot.send_message(chat_id=chat_id, text=html,
                                          reply_markup=kb, parse_mode="HTML")
    crud.update_post(post.id, tg_message_id=sent.message_id)
    post.tg_message_id = sent.message_id
    context.user_data["active_thread_post_id"] = post.id


def _save(context: ContextTypes.DEFAULT_TYPE, post_id: int,
          tweets: list[dict]) -> object | None:
    """Persist the working tweet list; keeps the builder card message id intact."""
    post_now = crud.get_post(post_id)
    if post_now is None:
        return None
    msg_id = post_now.tg_message_id
    crud.set_thread_content(post_id, tweets)
    fresh = crud.update_post(post_id, tg_message_id=msg_id)
    return fresh


# ----------------------------------------------------------------- commands -

async def thread_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new thread draft."""
    from utils.ui import deny

    if not is_admin(update):
        await deny(update, context)
        return
        
    raw = " ".join(context.args or []).strip()
    if raw.startswith("with "):
        parts = [p.strip() for p in re.findall(r'"([^"]+)"', raw)]
    else:
        parts = [x.strip() for x in raw.split("---") if x.strip()]
        
    if not parts:
        parts = ["", ""]
    if len(parts) > crud.MAX_THREAD_TWEETS:
        await update.message.reply_text(
            f"⚠️ Threads are capped at {crud.MAX_THREAD_TWEETS} tweets — "
            f"keeping the first {crud.MAX_THREAD_TWEETS}.")
        parts = parts[:crud.MAX_THREAD_TWEETS]

    first = parts[0]
    post = crud.create_post(content=first,
                            auto_format=crud.get_bool("auto_format", True))
    crud.create_thread(post.id, [{"content": c, "media_ids": []} for c in parts])
    context.user_data.pop("th_flow", None)
    context.user_data.pop("th_attach", None)
    
    await _show_builder(context, update.effective_chat.id, post,
                        note="🧵 <b>New thread</b> — add tweets, then attach media to any tweet.")


# ------------------------------------------------------------- text input --

async def handle_thread_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume text while the builder waits for a tweet body. True = handled."""
    flow = context.user_data.get("th_flow")
    if not flow or not update.message:
        return False
    kind = flow[0]
    msg = update.message
    chat_id = msg.chat.id
    text = (msg.text or "").strip()

    if kind == "add":
        _, post_id = flow
        post = crud.get_post(post_id)
        if post is None:
            context.user_data.pop("th_flow", None)
            await msg.reply_text("ℹ️ That thread no longer exists.")
            return True
        tweets = _tweets_of(post)
        if len(tweets) >= crud.MAX_THREAD_TWEETS:
            await msg.reply_text(f"⚠️ Max {crud.MAX_THREAD_TWEETS} tweets per thread reached.")
            return True
        
        note = None
        from utils.formatting import twitter_len, truncate_to_limit
        if twitter_len(text) > config.X_CHAR_LIMIT:
            await msg.reply_text(f"⚠️ Tweet is {twitter_len(text)} chars (limit: 280). Compressing with Groq AI...")
            from utils.groq_helper import compress_tweet, GroqError
            try:
                text = await compress_tweet(text)
            except GroqError:
                text = truncate_to_limit(text, config.X_CHAR_LIMIT)
            note = f"✏️ Tweet compressed to {twitter_len(text)}/280 chars with Groq AI."

        # Fill empty tweets first before appending
        empty_idx = next((i for i, t in enumerate(tweets) if not t["content"].strip() and not t["media_ids"]), None)
        if empty_idx is not None:
            tweets[empty_idx]["content"] = text
            note = note or f"✏️ Tweet {empty_idx + 1} filled."
        else:
            tweets.append({"content": text, "media_ids": []})
            note = note or f"➕ Tweet {len(tweets)} added."
        
        context.user_data.pop("th_flow", None)
        _save(context, post_id, tweets)
        
        fresh_post = crud.get_post(post_id)
        await _show_builder(context, chat_id, fresh_post, note=note, force_new_message=True)
        return True

    if kind == "set":
        _, post_id, pos = flow
        post = crud.get_post(post_id)
        if post is None:
            context.user_data.pop("th_flow", None)
            await msg.reply_text("ℹ️ That thread no longer exists.")
            return True
        tweets = _tweets_of(post)
        if not (0 <= pos < len(tweets)):
            context.user_data.pop("th_flow", None)
            await msg.reply_text("ℹ️ That tweet position is gone.")
            return True

        note = f"✏️ Tweet {pos + 1} updated."
        from utils.formatting import twitter_len, truncate_to_limit
        if twitter_len(text) > config.X_CHAR_LIMIT:
            await msg.reply_text(f"⚠️ Tweet {pos + 1} is {twitter_len(text)} chars (limit: 280). Compressing with Groq AI...")
            from utils.groq_helper import compress_tweet, GroqError
            try:
                text = await compress_tweet(text)
            except GroqError:
                text = truncate_to_limit(text, config.X_CHAR_LIMIT)
            note = f"✏️ Tweet {pos + 1} compressed to {twitter_len(text)}/280 chars with Groq AI."

        tweets[pos]["content"] = text
        context.user_data.pop("th_flow", None)
        _save(context, post_id, tweets)
        
        fresh_post = crud.get_post(post_id)
        await _show_builder(context, chat_id, fresh_post, note=note, force_new_message=True)
        return True


    return False


# ------------------------------------------------------------- media input --

_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "video/mp4": ".mp4",
                "video/quicktime": ".mov"}


def _guess_ext(file_name: str | None, mime: str | None) -> str:
    if file_name:
        ext = os.path.splitext(file_name)[1].lower()
        if ext in _EXT_BY_MIME.values():
            return ext
    return _EXT_BY_MIME.get(mime or "", ".jpg")


async def download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str,
                                 fname_hint: str | None = None,
                                 mime: str | None = None) -> str:
    """Download a Telegram file into MEDIA_DIR and return its local path."""
    file = await context.bot.get_file(file_id)
    fname = f"th_{uuid.uuid4().hex[:10]}{_guess_ext(fname_hint, mime)}"
    path = os.path.join(config.MEDIA_DIR, fname)
    await file.download_to_drive(path)
    return path


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
_MIME_KINDS = {"image/jpeg": "image", "image/png": "image", "image/webp": "image",
               "image/gif": "gif", "video/mp4": "video", "video/quicktime": "video",
               "video/webm": "video"}

_ALBUM_TTL_SECONDS = 600     # keep appending photos to the same draft for 10 minutes
_MAX_ALBUM = 4               # X allows up to 4 images per tweet


def _kind_of(file_name: str | None, mime: str | None = None) -> str:
    """X media category for a local path or Telegram file: image / gif / video / unknown."""
    ext = os.path.splitext(file_name or "")[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext == ".gif":
        return "gif"
    return _MIME_KINDS.get((mime or "").lower(), "unknown")


def _message_kind(msg) -> str:
    """Category of the media carried by an incoming Telegram message."""
    if msg.photo:
        return "image"
    if msg.animation:
        return "gif"
    if msg.video:
        return "video"
    doc = getattr(msg, "document", None)
    if doc is not None:
        return _kind_of(getattr(doc, "file_name", None), getattr(doc, "mime_type", None))
    return "unknown"


def _can_join_album(post, kind: str) -> bool:
    """X forbids mixing a video/gif with other media and caps a tweet at 4 images."""
    media = list(post.media)
    if kind != "image" or any(_kind_of(p) != "image" for p in media):
        return False
    return len(media) < _MAX_ALBUM


def _active_album(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """The draft we are currently appending media to (auto-expires after 10 min)."""
    album = context.user_data.get("media_album")
    if not album:
        return None
    if time.monotonic() - album.get("ts", 0.0) > _ALBUM_TTL_SECONDS:
        context.user_data.pop("media_album", None)
        return None
    post = crud.get_post(album.get("post_id"))
    if post is None or post.status != "draft":
        context.user_data.pop("media_album", None)
        return None
    return album


def _remember_album(context: ContextTypes.DEFAULT_TYPE, kind: str, post,
                    group: str | None = None) -> None:
    """Point the 'keep adding media here' window at `post` (group = Telegram album id)."""
    context.user_data["media_album"] = {"kind": kind, "post_id": post.id,
                                        "group": group, "ts": time.monotonic()}


def _pending_files(pending: dict) -> list[dict]:
    """Normalise the buffered Telegram files (single-file legacy shape included)."""
    files = pending.get("files")
    if files:
        return list(files)
    if pending.get("file_id"):
        return [{"file_id": pending["file_id"], "mime": pending.get("mime"),
                 "fname": pending.get("fname")}]
    return []


async def _download_pending(context: ContextTypes.DEFAULT_TYPE, pending: dict) -> list[str]:
    """Download every buffered file of a pending media batch (max 4)."""
    paths: list[str] = []
    for item in _pending_files(pending)[:_MAX_ALBUM]:
        paths.append(await download_telegram_file(context, item.get("file_id"),
                                                 item.get("fname"), item.get("mime")))
    return paths


async def _append_to_album(context: ContextTypes.DEFAULT_TYPE, album: dict,
                           path: str, kind: str):
    """Add one more Telegram-album file to the open draft. None when it can't join."""
    post = crud.get_post(album.get("post_id"))
    if post is None:
        return None
    if album.get("kind") == "thread":
        tweets = _tweets_of(post)
        if not tweets:
            return None
        current = list(tweets[0]["media_ids"])
        if (len(current) >= _MAX_ALBUM or kind != "image"
                or any(_kind_of(p) != "image" for p in current)):
            return None
        if not crud.update_tweet_media(post.id, 0, current + [path]):
            return None
        return crud.get_post(post.id)
    if not _can_join_album(post, kind):
        return None
    return crud.add_post_media(post.id, list(post.media) + [path])


def _joinable_album_post(context: ContextTypes.DEFAULT_TYPE):
    """The single draft the next photo may be appended to (None when closed/full)."""
    album = _active_album(context)
    if not album or album.get("kind") != "single":
        return None
    post = crud.get_post(album.get("post_id"))
    if post is None or post.thread is not None:
        return None
    return post if _can_join_album(post, "image") else None


def _media_choice_rows(context: ContextTypes.DEFAULT_TYPE) -> list[list[InlineKeyboardButton]]:
    """Picker rows for an incoming media batch (offers the open album when possible)."""
    album_post = _joinable_album_post(context)
    if album_post is not None:
        n = len(list(album_post.media))
        single_label = f"➕ Add to draft #{album_post.id} ({n}/{_MAX_ALBUM})"
    else:
        single_label = "📝 Single Tweet"
    return [
        [
            InlineKeyboardButton(single_label, callback_data="media:single"),
            InlineKeyboardButton("🧵 New Thread", callback_data="media:thread"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="media:cancel"),
        ],
    ]


def _media_prompt_text(context: ContextTypes.DEFAULT_TYPE, count: int = 1) -> str:
    """Prompt copy for the buffered media batch."""
    if count > 1:
        return (f"📎 <b>{count} files received.</b> Attach them to one tweet "
                f"(max {_MAX_ALBUM} images) or start a thread?")
    if _joinable_album_post(context) is not None:
        return "📎 <b>Photo received.</b> Add it to the open draft, or start something new?"
    return "📎 <b>Media received.</b> How would you like to use it?"


async def _refresh_media_prompt(context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    """Re-label the pending-media prompt as more album photos arrive."""
    msg_id = pending.get("prompt_id")
    if not msg_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=pending.get("chat_id") or config.ADMIN_ID,
            message_id=msg_id,
            text=_media_prompt_text(context, len(_pending_files(pending))),
            reply_markup=InlineKeyboardMarkup(_media_choice_rows(context)),
            parse_mode="HTML")
    except Exception:
        log.debug("media prompt refresh failed", exc_info=True)


async def handle_thread_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Photo/video consumed by the thread attach-slot flow, or initiates a new media flow."""
    if not update.message:
        return False

    msg = update.message
    media = (msg.photo[-1] if msg.photo else
             msg.video if msg.video else
             msg.animation if msg.animation else
             msg.document if msg.document else None)
    
    if media is None:
        return False

    flow = context.user_data.get("th_attach")
    if not flow and context.user_data.get("flow"):
        # an open ✏️ edit flow owns the next photo (attach to the draft being edited)
        return False
    caption = (msg.caption or "").strip()  # Capture caption if provided with media
    kind = _message_kind(msg)
    group_id = getattr(msg, "media_group_id", None)

    # ── No active flow → offer options ──────────────────────────────────────
    if not flow:
        # Photos sent after a choice was made ride along into the same draft
        # (a Telegram album arrives as N messages sharing one media_group_id).
        album = _active_album(context)
        if group_id and album and album.get("group") == group_id:
            path = await download_telegram_file(context, media.file_id,
                                                getattr(media, "file_name", None),
                                                getattr(media, "mime_type", None))
            fresh = await _append_to_album(context, album, path, kind)
            if fresh is not None:
                if album.get("kind") == "thread":
                    n = len(_tweets_of(fresh)[0]["media_ids"])
                    await _show_builder(context, msg.chat.id, fresh,
                                        note=f"➕ <b>Added to #{fresh.id}</b> — "
                                             f"{n}/{_MAX_ALBUM} on tweet 1.")
                else:
                    n = len(list(fresh.media))
                    await show_preview(context, msg.chat.id, fresh,
                                       text_extra=f"➕ <b>Added to #{fresh.id}</b> — "
                                                  f"{n}/{_MAX_ALBUM} attached.")
                return True
            if os.path.isfile(path):   # can't join → drop the copy, ask instead
                try:
                    os.remove(path)
                except OSError:
                    pass

        pending = context.user_data.get("pending_media")
        if pending and group_id and pending.get("group") == group_id:
            # another file of the same Telegram album: buffer it, don't re-ask
            files = _pending_files(pending)
            if len(files) < _MAX_ALBUM:
                files.append({"file_id": media.file_id,
                              "mime": getattr(media, "mime_type", None),
                              "fname": getattr(media, "file_name", None)})
                pending["files"] = files
                await _refresh_media_prompt(context, pending)
                return True

        # Check if there's an active thread builder
        # Find the most recent draft thread post
        active_thread_post_id = context.user_data.get("active_thread_post_id")
        active_post = crud.get_post(active_thread_post_id) if active_thread_post_id else None
        has_active_thread = (active_post is not None 
                             and crud.get_thread(active_post.id) is not None)

        context.user_data["pending_media"] = {
            "files":   [{"file_id": media.file_id,
                         "mime":    getattr(media, "mime_type", None),
                         "fname":   getattr(media, "file_name", None)}],
            "caption": caption,
            "kind":    kind,
            "group":   group_id,
            "chat_id": msg.chat.id,
            "prompt_id": None,
            "thread_post_id": active_thread_post_id if has_active_thread else None,
        }

        if has_active_thread:
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("➕ New tweet in thread",
                                        callback_data="media:thread_add"),
                    InlineKeyboardButton("📎 Attach to tweet",
                                        callback_data="media:thread_attach"),
                ],
                [InlineKeyboardButton("📝 Single Tweet", callback_data="media:single")],
                [InlineKeyboardButton("❌ Cancel",        callback_data="media:cancel")],
            ])
            text = "📎 <b>Media received.</b> You have an active thread — what should this do?"
        else:
            kb = InlineKeyboardMarkup(_media_choice_rows(context))
            text = _media_prompt_text(context, 1)

        sent = await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")
        pending = context.user_data["pending_media"]
        pending["prompt_id"] = sent.message_id
        return True

    # ── existing flow handling (th_attach) ──────────────────────────────────
    if flow[0] == "awaiting_media":
        post_id = flow[1]
        post = crud.get_post(post_id)
        if post is None or crud.get_thread(post_id) is None:
            context.user_data.pop("th_attach", None)
            await msg.reply_text("ℹ️ That thread no longer exists.")
            return True

        file_id = media.file_id
        fname   = getattr(media, "file_name", None)
        mime    = getattr(media, "mime_type", None)

        # Buffer files; album messages share a media_group_id
        batch = context.user_data.get("th_attach_batch", [])
        if len(batch) < crud.MAX_MEDIA_PER_TWEET:
            batch.append({"file_id": file_id, "fname": fname, "mime": mime})
        context.user_data["th_attach_batch"] = batch

        # If same album, wait for more; otherwise flush immediately
        prev_group = context.user_data.get("th_attach_group")
        if group_id and group_id == prev_group and len(batch) < crud.MAX_MEDIA_PER_TWEET:
            return True  # more album photos coming
        context.user_data["th_attach_group"] = group_id

        # Download all buffered files
        paths = []
        for item in batch:
            paths.append(await download_telegram_file(
                context, item["file_id"], item["fname"], item["mime"]))

        context.user_data.pop("th_attach_batch", None)
        context.user_data.pop("th_attach_group", None)
        context.user_data["th_attach_caption"] = caption
        context.user_data["th_attach"] = ("awaiting_target", post_id, paths)

        tweets = _tweets_of(post)
        buttons = [
            InlineKeyboardButton(
                f"🖼 Tweet {i+1} ({len(tweets[i]['media_ids'])}/{crud.MAX_MEDIA_PER_TWEET})",
                callback_data=f"thm:{post_id}:{i}")
            for i in range(len(tweets))
        ]
        rows = [buttons[j:j+2] for j in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton(
            f"➕ Add as tweet {len(tweets)+1}",
            callback_data=f"thm:{post_id}:new")])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"th:cancel_attach:{post_id}")])
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.reply_text(
            f"📎 <b>{len(paths)} file(s) ready.</b> Which tweet should they attach to?",
            reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
        return True

    # ── More album photos arriving after first was already processed ─────────
    if flow[0] == "awaiting_target" and group_id:
        post_id = flow[1]
        current_paths = flow[2] if isinstance(flow[2], list) else [flow[2]]

        if len(current_paths) < crud.MAX_MEDIA_PER_TWEET:
            path = await download_telegram_file(
                context, media.file_id,
                getattr(media, "file_name", None),
                getattr(media, "mime_type", None))
            current_paths.append(path)
            context.user_data["th_attach_caption"] = caption
            context.user_data["th_attach"] = ("awaiting_target", post_id, current_paths)

            # Update the picker prompt to reflect new count
            post = crud.get_post(post_id)
            if post:
                tweets = _tweets_of(post)
                buttons = [
                    InlineKeyboardButton(
                        f"🖼 Tweet {i+1} ({len(tweets[i]['media_ids'])}/{crud.MAX_MEDIA_PER_TWEET})",
                        callback_data=f"thm:{post_id}:{i}")
                    for i in range(len(tweets))
                ]
                rows = [buttons[j:j+2] for j in range(0, len(buttons), 2)]
                rows.append([InlineKeyboardButton(
                    f"➕ Add as tweet {len(tweets)+1}",
                    callback_data=f"thm:{post_id}:new")])
                rows.append([InlineKeyboardButton("❌ Cancel",
                             callback_data=f"th:cancel_attach:{post_id}")])
                try:
                    await msg.delete()
                except Exception:
                    pass
                await msg.reply_text(
                    f"📎 <b>{len(current_paths)} file(s) ready.</b> Which tweet?",
                    reply_markup=InlineKeyboardMarkup(rows),
                    parse_mode="HTML")
        return True

    return False


# --------------------------------------------------------------- callbacks --

async def thread_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles th:* and thm:* callbacks (builder + per-tweet media picker)."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    data = cq.data or ""
    chat_id = update.effective_chat.id

    # ---- NEW: Handle pending media actions ----------------------------------
    if data == "media:cancel":
        context.user_data.pop("pending_media", None)
        await cq.edit_message_text("❌ Cancelled.")
        return

    if data == "media:thread_add":
        pm = context.user_data.pop("pending_media", None)
        if not pm:
            await cq.edit_message_text("⚠️ Media expired.")
            return
        post_id = pm.get("thread_post_id")
        post = crud.get_post(post_id) if post_id else None
        if not post:
            await cq.edit_message_text("⚠️ Thread no longer exists.")
            return
        paths = await _download_pending(context, pm)
        tweets = _tweets_of(post)
        tweets.append({"content": pm.get("caption", ""), "media_ids": paths})
        _save(context, post_id, tweets)
        await _show_builder(context, chat_id, crud.get_post(post_id),
                            note=f"➕ New tweet added with {len(paths)} media.",
                            force_new_message=True)
        return

    if data == "media:thread_attach":
        pm = context.user_data.pop("pending_media", None)
        if not pm:
            await cq.edit_message_text("⚠️ Media expired.")
            return
        post_id = pm.get("thread_post_id")
        post = crud.get_post(post_id) if post_id else None
        if not post:
            await cq.edit_message_text("⚠️ Thread no longer exists.")
            return
        paths = await _download_pending(context, pm)
        # Re-use existing attach flow
        context.user_data["th_attach_caption"] = pm.get("caption", "")
        context.user_data["th_attach"] = ("awaiting_target", post_id, paths)
        tweets = _tweets_of(post)
        buttons = [
            InlineKeyboardButton(
                f"🖼 Tweet {i+1} ({len(tweets[i]['media_ids'])}/{crud.MAX_MEDIA_PER_TWEET})",
                callback_data=f"thm:{post_id}:{i}")
            for i in range(len(tweets))
        ]
        rows = [buttons[j:j+2] for j in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton(
            f"➕ Add as tweet {len(tweets)+1}",
            callback_data=f"thm:{post_id}:new")])
        rows.append([InlineKeyboardButton("❌ Cancel",
                     callback_data=f"th:cancel_attach:{post_id}")])
        await cq.edit_message_text(
            f"📎 <b>{len(paths)} file(s) ready.</b> Which tweet?",
            reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
        return

    if data == "media:single":
        pm = context.user_data.pop("pending_media", None)
        if not pm:
            await cq.edit_message_text("⚠️ Media expired, send again.")
            return
        paths = await _download_pending(context, pm)
        if not paths:
            await cq.edit_message_text("⚠️ Media expired, send again.")
            return
        caption = pm.get("caption", "")
        album = _active_album(context)
        post = crud.get_post(album["post_id"]) if album else None

        if (post is not None and post.thread is None
                and _can_join_album(post, pm.get("kind", "image"))):
            # grow the album draft that is already open instead of starting a new one
            post = crud.add_post_media(post.id, list(post.media) + paths)
            if post is not None and not (post.content or "").strip() and caption:
                post = crud.update_post(post.id, content=caption) or post
        else:
            post = crud.create_post(content=caption, media_ids=paths,
                                    auto_format=crud.get_bool("auto_format", True))
        _remember_album(context, "single", post, pm.get("group"))

        n = len(list(post.media))
        try:
            await cq.edit_message_text(
                f"➕ <b>Draft #{post.id}</b> · {n}/{_MAX_ALBUM} media files attached.",
                parse_mode="HTML")
        except Exception:
            pass
        await show_preview(context, chat_id, post,
                           text_extra=(f"📎 <b>{n}/{_MAX_ALBUM} media attached</b> — "
                                       f"send more photos, add a caption, or post now:"))
        return

    if data == "media:thread":
        pm = context.user_data.pop("pending_media", None)
        if not pm:
            await cq.edit_message_text("⚠️ Media expired, send again.")
            return
        paths = await _download_pending(context, pm)
        if not paths:
            await cq.edit_message_text("⚠️ Media expired, send again.")
            return
        caption = pm.get("caption", "")

        # Create post and initialize thread with the whole media batch in tweet 1
        post = crud.create_post(content=caption, auto_format=crud.get_bool("auto_format", True))
        crud.create_thread(post.id, [{"content": caption, "media_ids": paths},
                                     {"content": "", "media_ids": []}])
        _remember_album(context, "thread", post, pm.get("group"))
        try:
            await cq.edit_message_text(
                f"🧵 <b>Thread #{post.id}</b> · {len(paths)} media files on tweet 1.",
                parse_mode="HTML")
        except Exception:
            pass
        await _show_builder(context, chat_id, crud.get_post(post.id),
                            note="🧵 Media added to tweet 1 — fill in text and add more tweets.")
        return

    # ---- th:rmm:<post_id>:<slot_index> — detach one attached file -----------
    if data.startswith("th:rmm:"):
        parts = data.split(":")
        post_id = int(parts[2])
        slot_idx = int(parts[3])
        post = crud.get_post(post_id)
        if post is None:
            await cq.edit_message_text("ℹ️ This draft no longer exists.")
            return
        slots = [(i, m) for i, t in enumerate(_tweets_of(post)) for m in t["media_ids"]]
        if not (0 <= slot_idx < len(slots)):
            await _show_builder(context, chat_id, post, note="ℹ️ That file is already gone.")
            return
        pos, path = slots[slot_idx]
        ok = crud.remove_tweet_media(post_id, pos, path)
        note = (f"🗑 Detached <code>{escape_html(os.path.basename(path))}</code> "
                f"from tweet {pos + 1}." if ok else "ℹ️ File not found on disk.")
        await _show_builder(context, chat_id, crud.get_post(post_id), note=note)
        return

    # ---- thm:<post_id>:<position> — attach media to selected tweet ----------
    if data.startswith("thm:"):
        parts = data.split(":")
        post_id = int(parts[1])
        pos_raw = parts[2]          # could be "new" or a digit

        flow = context.user_data.get("th_attach")
        if not flow or flow[0] != "awaiting_target":
            await cq.answer("ℹ️ Attach flow expired.", show_alert=True)
            return

        _, _, temp_paths = flow
        if isinstance(temp_paths, str):
            temp_paths = [temp_paths]

        post = crud.get_post(post_id)
        if post is None:
            await cq.edit_message_text("ℹ️ This draft no longer exists.")
            return

        tweets = _tweets_of(post)
        context.user_data.pop("th_attach", None)

        if pos_raw == "new":
            # Pull caption from th_attach_caption if stored, else empty
            caption = context.user_data.pop("th_attach_caption", "")
            if len(tweets) >= crud.MAX_THREAD_TWEETS:
                await cq.answer(f"Max {crud.MAX_THREAD_TWEETS} tweets reached.", show_alert=True)
                return
            tweets.append({"content": caption, "media_ids": temp_paths})
            _save(context, post_id, tweets)
            note = f"➕ Tweet {len(tweets)} added with {len(temp_paths)} media."
        else:
            pos = int(pos_raw)
            if not (0 <= pos < len(tweets)):
                await cq.answer("ℹ️ That tweet position is gone.", show_alert=True)
                return
            slots_free = crud.MAX_MEDIA_PER_TWEET - len(tweets[pos]["media_ids"])
            to_attach  = temp_paths[:slots_free]
            dropped    = len(temp_paths) - len(to_attach)
            tweets[pos]["media_ids"].extend(to_attach)
            _save(context, post_id, tweets)
            note = f"📎 {len(to_attach)} file(s) attached to tweet {pos+1}."
            if dropped:
                note += f" ({dropped} dropped — tweet full.)"

        try:
            await cq.edit_message_text("✅ Done.")
        except Exception:
            pass
        await _show_builder(context, chat_id, crud.get_post(post_id),
                            note=note, force_new_message=True)
        return

    if not data.startswith("th:"):
        return
    parts = data.split(":")
    action = parts[1]
    post_id = int(parts[2])
    post = crud.get_post(post_id)
    if post is None:
        await cq.edit_message_text("ℹ️ This draft no longer exists.")
        return
    tweets = _tweets_of(post)

    # ---- postnow: finalize and schedule immediately -------------------------
    if action == "postnow":
        if len(tweets) < 2:
            await cq.answer("Threads need at least 2 tweets.", show_alert=True)
            return
        empties = [i for i, t in enumerate(tweets) if not t["content"].strip() and not t["media_ids"]]
        if empties:
            await cq.answer(f"Tweet(s) {', '.join(str(i + 1) for i in empties)} are empty.", show_alert=True)
            return
            
        crud.reset_thread_tweet_state(post_id)
        fresh = crud.update_post(post_id, content=tweets[0]["content"], tg_message_id=None)
        
        await show_preview(context, chat_id, fresh,
                           text_extra="🚀 <b>Thread ready to post</b> — confirm to publish now:")
        return

    # ---- cancel_attach: clean up temp file and return to builder ------------
    if action == "cancel_attach":
        flow = context.user_data.get("th_attach")
        if flow and flow[0] == "awaiting_target":
            paths = flow[2] if isinstance(flow[2], list) else [flow[2]]
            for p in paths:
                try:
                    os.remove(p)
                except Exception:
                    pass
        context.user_data.pop("th_attach", None)
        context.user_data.pop("th_attach_batch", None)
        context.user_data.pop("th_attach_group", None)
        context.user_data.pop("th_attach_caption", None)
        await _show_builder(context, chat_id, post, note="ℹ️ Attach cancelled.")
        return

    # ---- done: persist + hand over to the normal draft preview -------------
    if action == "done":
        if len(tweets) < 2:
            await cq.answer("Threads need at least 2 tweets — add one first.", show_alert=True)
            return
        empties = [i for i, t in enumerate(tweets)
                   if not t["content"].strip() and not t["media_ids"]]
        if empties:
            await cq.answer(f"Tweet(s) {', '.join(str(i + 1) for i in empties)} "
                            f"are empty — set text or attach media first.", show_alert=True)
            return
        crud.reset_thread_tweet_state(post_id)
        fresh = crud.update_post(post_id, content=tweets[0]["content"], tg_message_id=None)
        await show_preview(context, chat_id, fresh,
                           text_extra="🧵 <b>Thread draft ready</b> — confirm, edit or cancel:")
        return

    # ---- add tweet ----------------------------------------------------------
    if action == "add":
        if len(tweets) >= crud.MAX_THREAD_TWEETS:
            await cq.answer(f"Max {crud.MAX_THREAD_TWEETS} tweets per thread.", show_alert=True)
            return
        context.user_data["th_flow"] = ("add", post_id)
        await cq.edit_message_text("🆕 Send the text for the next tweet "
                                   "(this appends tweet "
                                   f"{len(tweets) + 1}/{crud.MAX_THREAD_TWEETS}).")
        return

    # ---- remove tweet at position -------------------------------------------
    if action == "rm":
        pos = int(parts[3])
        if len(tweets) <= 2:
            await cq.answer("A thread needs at least 2 tweets.", show_alert=True)
            return
        tweets.pop(pos)
        _save(context, post_id, tweets)
        await _show_builder(context, chat_id, crud.get_post(post_id),
                            note=f"🗑 Removed tweet {pos + 1}.")
        return

    # ---- set text pickers ----------------------------------------------------
    if action == "setpick":
        rows = [[InlineKeyboardButton(f"✏️ Tweet {i + 1}", callback_data=f"th:set:{post_id}:{i}")
                 for i in range(len(tweets))][j:j + 3]
                for j in range(0, len(tweets), 3)]
        rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"th:menu:{post_id}")])
        await cq.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "set":
        pos = int(parts[3])
        context.user_data["th_flow"] = ("set", post_id, pos)
        await cq.edit_message_text(f"✏️ Send the new text for tweet {pos + 1}.")
        return

    # ---- media pickers (reversed flow: send media first, then pick tweet) ---
    if action == "mpick":
        context.user_data["th_attach"] = ("awaiting_media", post_id)
        await cq.edit_message_text("📤 Send the photo/video you want to attach to the thread.")
        return

    if action == "rmpick":
        slots = [(i, m) for i, t in enumerate(tweets) for m in t["media_ids"]]
        if not slots:
            await cq.answer("No media attached yet.", show_alert=True)
            return
        from utils.ui import thread_remove_media_keyboard
        await cq.edit_message_text(
            "🗑 Tap a file to detach it from its tweet (you can then send a replacement).",
            reply_markup=thread_remove_media_keyboard(post_id, slots))
        return

    if action == "rmpickc":  # drop whole tweets
        if len(tweets) <= 2:
            await cq.answer("A thread needs at least 2 tweets.", show_alert=True)
            return
        rows = [[InlineKeyboardButton(f"🗑 Tweet {i + 1}", callback_data=f"th:rm:{post_id}:{i}")
                 for i in range(len(tweets))][j:j + 3]
                for j in range(0, len(tweets), 3)]
        rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"th:menu:{post_id}")])
        await cq.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "menu":
        await _show_builder(context, chat_id, post)
        return


# -------------------------------------------- attach-slot helper for drafts -

async def open_attach_picker(bot, chat_id: int, post_id: int,
                             message_id: int | None = None) -> None:
    """d:<token>:tmedia → choose which tweet receives the next photo."""
    post = crud.get_post(post_id)
    if post is None:
        await bot.send_message(chat_id=chat_id, text="ℹ️ This draft no longer exists.")
        return
    tweets = _tweets_of(post)
    from utils.ui import thread_media_keyboard

    text_lines = ["🖼 <b>Attach media to which tweet?</b>", ""]
    for i, t in enumerate(tweets):
        snippet = escape_html((t["content"] or "").replace("\n", " ")[:60])
        text_lines.append(f"<b>{i + 1}.</b> [{len(t['media_ids'])} media] {snippet}")
    html = "\n".join(text_lines)
    kb = thread_media_keyboard(post_id, len(tweets))
    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=html, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            log.debug("attach picker edit failed, sending fresh")
    await bot.send_message(chat_id=chat_id, text=html, reply_markup=kb, parse_mode="HTML")