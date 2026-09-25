"""Thread builder: /thread command + `th:*` callbacks.

A thread is stored as a Thread row (1:1 with a Post) plus ThreadTweet rows.
Media is attached per tweet via the "attach slot" flow:

    th:media:<post_id>:<position>   -> arm context.user_data['th_attach']
    (admin sends photo/video)       -> file downloaded into that tweet's media_ids
    th:rmm:<post_id>:<position>     -> remove one file from a tweet
    th:set:<post_id>:<position>     -> replace one tweet's text inline
    th:add / th:rm:<post_id>:<pos>  -> append / drop tweets (max 25, min 2)

All state lives in SQLite; nothing here touches the X API.
"""
from __future__ import annotations

import logging
import os
import re
import uuid

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
                        note: str = "") -> None:
    """Re-render the builder card for a thread draft."""
    tweets = _tweets_of(post)
    html = _render_tweets(tweets)
    if note:
        html = f"{note}\n\n{html}"
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton("✅ Done — preview draft", callback_data=f"th:done:{post.id}"),
    ]]
    kb = InlineKeyboardMarkup(rows + _action_keyboard(post.id).inline_keyboard)
    if post.tg_message_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=post.tg_message_id,
                                                text=html, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            crud.update_post(post.id, tg_message_id=None)
            post.tg_message_id = None
    sent = await context.bot.send_message(chat_id=chat_id, text=html,
                                          reply_markup=kb, parse_mode="HTML")
    crud.update_post(post.id, tg_message_id=sent.message_id)
    post.tg_message_id = sent.message_id


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
    """Start a new thread draft.

    Usage:
        /thread                                  → empty 2-tweet builder
        /thread line1 --- line2 --- …            → seed tweets (split on '---')
        /thread with "tweet one" "tweet two"     → same, but quotes allow '|' inside text
    """
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
        tweets.append({"content": text, "media_ids": []})
        context.user_data.pop("th_flow", None)
        _save(context, post_id, tweets)
        await _show_builder(context, chat_id, crud.get_post(post_id),
                            note=f"➕ Tweet {len(tweets)} added.")
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
        tweets[pos]["content"] = text
        context.user_data.pop("th_flow", None)
        _save(context, post_id, tweets)
        await _show_builder(context, chat_id, crud.get_post(post_id),
                            note=f"✏️ Tweet {pos + 1} updated.")
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


async def handle_thread_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Photo/video consumed by the thread attach-slot flow. True = handled."""
    flow = context.user_data.get("th_attach")
    if not flow or not update.message:
        return False
    post_id, pos = flow
    msg = update.message
    chat_id = msg.chat.id
    post = crud.get_post(post_id)
    if post is None or crud.get_thread(post_id) is None:
        context.user_data.pop("th_attach", None)
        await msg.reply_text("ℹ️ That thread no longer exists.")
        return True
    tweets = _tweets_of(post)
    if not (0 <= pos < len(tweets)):
        context.user_data.pop("th_attach", None)
        await msg.reply_text("ℹ️ That tweet position is gone.")
        return True
    if len(tweets[pos]["media_ids"]) >= crud.MAX_MEDIA_PER_TWEET:
        await msg.reply_text(f"⚠️ Tweet {pos + 1} already has "
                             f"{crud.MAX_MEDIA_PER_TWEET} media files (X limit).")
        return True

    media = msg.photo[-1] if msg.photo else (
        msg.video if msg.video else (msg.document if msg.document else None))
    if media is None:
        await msg.reply_text("❌ Send a photo or video.")
        return True
    if msg.photo is None:
        doc = getattr(media, "file_name", None)
        mime = getattr(media, "mime_type", None)
        if doc is None and mime is None:
            await msg.reply_text("❌ Send a photo or video (mp4/mov).")
            return True
        path = await download_telegram_file(context, media.file_id, doc, mime)
    else:
        path = await download_telegram_file(context, media.file_id)

    tweets[pos]["media_ids"].append(path)
    _save(context, post_id, tweets)
    context.user_data.pop("th_attach", None)
    await _show_builder(context, chat_id, crud.get_post(post_id),
                        note=f"📎 Attached to tweet {pos + 1}.")
    return True


# --------------------------------------------------------------- callbacks --

async def thread_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles th:* and thm:* callbacks (builder + per-tweet media picker)."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    data = cq.data or ""
    chat_id = update.effective_chat.id

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

    # ---- thm:<post_id>:<position> — pick target tweet for next upload -------
    if data.startswith("thm:"):
        parts = data.split(":")
        post_id = int(parts[1])
        pos = int(parts[2])
        post = crud.get_post(post_id)
        if post is None:
            await cq.edit_message_text("ℹ️ This draft no longer exists.")
            return
        context.user_data["th_attach"] = (post_id, pos)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"📤 Now send the photo/video for tweet <b>{pos + 1}</b>.\n"
                  f"(up to {crud.MAX_MEDIA_PER_TWEET} files per tweet; "
                  f"currently {len(_tweets_of(post)[pos]['media_ids'])})"),
            parse_mode="HTML")
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

    # ---- done: persist + hand over to the normal draft preview -------------
    if action == "done":
        if len(tweets) < 2:
            await cq.answer("Threads need at least 2 tweets — add one first.",
                            show_alert=True)
            return
        empties = [i for i, t in enumerate(tweets)
                   if not t["content"].strip() and not t["media_ids"]]
        if empties:
            await cq.answer(f"Tweet(s) {', '.join(str(i + 1) for i in empties)} "
                            f"are empty — set text or attach media first.", show_alert=True)
            return
        # Keep per-tweet rows authoritative (media is attached per tweet!).
        # Only mirror tweet 1 onto post.content so queue/preview snippets stay
        # meaningful; merging all tweets into one blob would make the publisher
        # re-serialize the whole thread as a single oversized tweet.
        crud.reset_thread_tweet_state(post_id)
        fresh = crud.update_post(post_id, content=tweets[0]["content"],
                                 tg_message_id=None)
        await show_preview(context, chat_id, fresh,
                           text_extra="🧵 <b>Thread draft ready</b> — confirm, edit or cancel:")
        return

    # ---- add tweet ----------------------------------------------------------
    if action == "add":
        if len(tweets) >= crud.MAX_THREAD_TWEETS:
            await cq.answer(f"Max {crud.MAX_THREAD_TWEETS} tweets per thread.",
                            show_alert=True)
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

    # ---- media pickers -------------------------------------------------------
    if action == "mpick":
        rows = [[InlineKeyboardButton(
            f"🖼 Tweet {i + 1} ({len(t['media_ids'])}/{crud.MAX_MEDIA_PER_TWEET})",
            callback_data=f"thm:{post_id}:{i}")
            for i in range(len(tweets))][j:j + 2]
            for j in range(0, len(tweets), 2)]
        rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"th:menu:{post_id}")])
        await cq.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
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
