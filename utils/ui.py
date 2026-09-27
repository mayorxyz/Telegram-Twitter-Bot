"""Shared UI helpers: admin guard, preview rendering, undo storage."""
from __future__ import annotations

import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import ContextTypes

from utils import config
from utils.formatting import format_preview

# post_id -> deleted Post snapshot (in-memory; queue rebuilds jobs from DB anyway)
UNDO_WINDOW_SECONDS = 10
_undo_store: dict[int, tuple[object, float]] = {}


def is_admin(update: Update) -> bool:
    user = None
    if update.effective_user:
        user = update.effective_user
    elif update.callback_query and update.callback_query.from_user:
        user = update.callback_query.from_user
    return bool(user and user.id == config.ADMIN_ID)


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject non-admin usage silently-ish."""
    cq = update.callback_query
    if cq:
        await cq.answer("⛔ Not authorized", show_alert=True)
        return
    msg = update.message
    if msg:
        await msg.reply_text("⛔ This bot is private.")


def stash_for_undo(post) -> None:
    _undo_store[post.id] = (post, time.monotonic())


def pop_undo(post_id: int):
    entry = _undo_store.pop(post_id, None)
    if not entry:
        return None
    post, ts = entry
    if time.monotonic() - ts > UNDO_WINDOW_SECONDS:
        return None
    return post


def draft_keyboard(post_id: int, rss: bool = False) -> InlineKeyboardMarkup:
    from db import crud  # local import avoids circulars at module load

    token = f"r{post_id}" if rss else str(post_id)
    is_thread = crud.get_thread(post_id) is not None
    row_tpl = []
    if crud.get_templates():
        row_tpl.append(InlineKeyboardButton("📋 Templates",
                                            callback_data=f"tpl:inject:{token}"))
    if is_thread:
        rows = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"d:{token}:confirm"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"d:{token}:edit"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"d:{token}:cancel"),
            ],
            [
                InlineKeyboardButton("🖼 Attach Media", callback_data=f"d:{token}:tmedia"),
                InlineKeyboardButton("🔁 Duplicate", callback_data=f"d:{token}:dup"),
                InlineKeyboardButton("🏷 Tag", callback_data=f"d:{token}:tags"),
            ],
        ]
        if row_tpl:
            rows.append(row_tpl)
        return InlineKeyboardMarkup(rows)
    if rss:
        rows = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"d:r{post_id}:approve"),
                InlineKeyboardButton("❌ Reject", callback_data=f"d:r{post_id}:reject"),
            ],
            [
                InlineKeyboardButton("✏️ Edit", callback_data=f"d:r{post_id}:edit"),
                InlineKeyboardButton("🔄 Regenerate", callback_data=f"d:r{post_id}:regen"),
            ],
        ]
        if row_tpl:
            rows.append(row_tpl)
        return InlineKeyboardMarkup(rows)
    row1 = [
        InlineKeyboardButton("✅ Confirm", callback_data=f"d:{post_id}:confirm"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"d:{post_id}:edit"),
        InlineKeyboardButton("🔄 Regenerate", callback_data=f"d:{post_id}:regen"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"d:{post_id}:cancel"),
    ]
    row2 = [
        InlineKeyboardButton("🔁 Duplicate", callback_data=f"d:{post_id}:dup"),
        InlineKeyboardButton("🏷 Tag", callback_data=f"d:{post_id}:tags"),
        InlineKeyboardButton("📎 Media", callback_data=f"d:{post_id}:media"),
    ]
    rows = [row1, row2]
    if row_tpl:
        rows.append(row_tpl)
    return InlineKeyboardMarkup(rows)


def tag_keyboard(post_id: int) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(t, callback_data=f"d:{post_id}:tag:{t}")
            for t in ("crypto", "AI")]
    row2 = [InlineKeyboardButton(t, callback_data=f"d:{post_id}:tag:{t}")
            for t in ("meme", "other")]
    row2.append(InlineKeyboardButton("↩️ Back", callback_data=f"d:{post_id}:back"))
    return InlineKeyboardMarkup([row1, row2])


_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm"}


def media_ext(path: str) -> str:
    return os.path.splitext(path or "")[1].lower()


def media_keyboard(post_id: int, media: list[str] | None = None) -> InlineKeyboardMarkup:
    """One 🗑 button per attached file (threads use thread_remove_media_keyboard)."""
    paths = [p for p in (media or []) if p]
    if not paths:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("ℹ️ No media attached", callback_data=f"d:{post_id}:media"),
            InlineKeyboardButton("↩️ Back", callback_data=f"d:{post_id}:back"),
        ]])
    rows = [[InlineKeyboardButton(f"🗑 {os.path.basename(p)[:24]}",
                                  callback_data=f"d:{post_id}:rmmedia:{i}")]
            for i, p in enumerate(paths[:4])]
    if len(paths) > 1:
        rows.append([InlineKeyboardButton("🗑 Remove all",
                                          callback_data=f"d:{post_id}:unmedia")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"d:{post_id}:back")])
    return InlineKeyboardMarkup(rows)


async def send_media(context: ContextTypes.DEFAULT_TYPE, chat_id: int, paths: list[str],
                     caption: str | None = None,
                     keyboard: InlineKeyboardMarkup | None = None):
    """Show attached media in chat: an album for 2-4 images, else the single file.

    Returns the sent message for a single file, the message list for an album, or
    None when nothing could be sent. Telegram can't attach a reply_markup to a
    media group, so for an album the caller must post its own control card.
    """
    paths = [p for p in paths if p and os.path.isfile(p)]
    if not paths:
        return None
    if len(paths) > 1 and all(media_ext(p) in _PHOTO_EXTS for p in paths):
        handles = [open(p, "rb") for p in paths[:4]]
        try:
            media = [InputMediaPhoto(media=h,
                                     caption=caption if i == 0 else None,
                                     parse_mode="HTML" if i == 0 else None)
                     for i, h in enumerate(handles)]
            return await context.bot.send_media_group(chat_id=chat_id, media=media)
        finally:
            for h in handles:
                h.close()
    path = paths[0]
    ext = media_ext(path)
    with open(path, "rb") as fh:
        if ext in _PHOTO_EXTS:
            return await context.bot.send_photo(chat_id=chat_id, photo=fh, caption=caption,
                                                reply_markup=keyboard, parse_mode="HTML")
        if ext in _VIDEO_EXTS:
            return await context.bot.send_video(chat_id=chat_id, video=fh, caption=caption,
                                                reply_markup=keyboard, parse_mode="HTML")
        if ext == ".gif":
            return await context.bot.send_animation(chat_id=chat_id, animation=fh,
                                                    caption=caption,
                                                    reply_markup=keyboard, parse_mode="HTML")
        return await context.bot.send_document(chat_id=chat_id, document=fh, caption=caption,
                                               reply_markup=keyboard, parse_mode="HTML")


def thread_media_keyboard(post_id: int, n_tweets: int) -> InlineKeyboardMarkup:
    """Pick which tweet of the thread should receive the next photo(s)."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(n_tweets):
        rows.append([InlineKeyboardButton(f"Tweet {i + 1}",
                                          callback_data=f"thm:{post_id}:{i}")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"d:{post_id}:back")])
    return InlineKeyboardMarkup(rows)


def thread_templates_button(post_id: int, rss: bool = False) -> InlineKeyboardButton:
    """📋 Templates button for thread draft cards → per-tweet injection picker."""
    token = f"r{post_id}" if rss else str(post_id)
    return InlineKeyboardButton("📋 Templates", callback_data=f"tpl:inject:{token}")


def thread_remove_media_keyboard(post_id: int, slots: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """slots = [(tweet_position, filename), ...] — one 🗑 button per attached file."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, (pos, path) in enumerate(slots):
        name = os.path.basename(path)[:24]
        rows.append([InlineKeyboardButton(f"🗑 T{pos + 1}: {name}",
                                          callback_data=f"th:rmm:{post_id}:{idx}")])
    rows.append([InlineKeyboardButton("➕ Add more media", callback_data=f"d:{post_id}:tmedia"),
                 InlineKeyboardButton("↩️ Done", callback_data=f"d:{post_id}:back")])
    return InlineKeyboardMarkup(rows)


def _preview_payload(post, text_extra: str, rss: bool) -> tuple[str, InlineKeyboardMarkup]:
    html = format_preview(post)
    if text_extra:
        html = f"{text_extra}\n\n{html}"
    return html, draft_keyboard(post.id, rss=rss)


async def update_preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int, post,
                         text_extra: str = "", rss: bool = False) -> None:
    """Edit the existing preview card in place (message + keyboard)."""
    html, kb = _preview_payload(post, text_extra, rss)
    if not post.tg_message_id:
        await show_preview(context, chat_id, post, text_extra, rss)
        return
    is_thread = getattr(post, "thread", None) is not None
    media = [p for p in (getattr(post, "media", None) or []) if os.path.isfile(p)]
    single_photo = (not is_thread and len(media) == 1
                    and media_ext(media[0]) in _PHOTO_EXTS)
    try:
        if single_photo:
            # re-point the media too; caption-only edits can't change attachments
            with open(media[0], "rb") as fh:
                await context.bot.edit_message_media(
                    chat_id=chat_id, message_id=post.tg_message_id,
                    media=InputMediaPhoto(media=fh, caption=html, parse_mode="HTML"),
                    reply_markup=kb,
                )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=post.tg_message_id,
                text=html, reply_markup=kb, parse_mode="HTML",
            )
    except Exception:
        # stale message / wrong type — fall back to sending a fresh card
        from db import crud
        crud.update_post(post.id, tg_message_id=None)
        post.tg_message_id = None
        await show_preview(context, chat_id, post, text_extra, rss)


async def show_preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int, post,
                       text_extra: str = "", rss: bool = False) -> int:
    """Send a new preview card (or edit in place if one exists). Returns message id."""
    html, kb = _preview_payload(post, text_extra, rss)
    if post.tg_message_id:
        await update_preview(context, chat_id, post, text_extra, rss)
        return post.tg_message_id
    is_thread = getattr(post, "thread", None) is not None
    media = [] if is_thread else list(getattr(post, "media", None) or [])
    sent = None
    if media:
        album = len(media) > 1
        # a media group can't carry buttons or a long caption → pair it with a card
        shown = await send_media(context, chat_id, media,
                                 caption=(f"📎 <b>{len(media)} media files</b>"
                                          if album else html),
                                 keyboard=None if album else kb)
        if shown is None:
            pass                  # unreadable files → fall back to a plain text card
        elif album:
            sent = await context.bot.send_message(chat_id=chat_id, text=html,
                                                  reply_markup=kb, parse_mode="HTML")
        else:
            sent = shown          # the photo/video message already IS the card
    if sent is None:
        sent = await context.bot.send_message(chat_id=chat_id, text=html,
                                              reply_markup=kb, parse_mode="HTML")
    from db import crud
    crud.update_post(post.id, tg_message_id=sent.message_id)
    post.tg_message_id = sent.message_id
    return sent.message_id
