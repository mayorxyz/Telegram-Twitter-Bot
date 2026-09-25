"""Edit & custom-time input flows (conversation-based, state in DB).

States stored in context.user_data['flow']:
  ('edit_content', post_id, rss:bool)   – waiting for replacement text
  ('custom_time', post_id, date_iso, from_queue:bool) – waiting for HH:MM / full datetime
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from db import crud
from utils import config
from utils.formatting import effective_length, escape_html, format_preview
from utils.timeutil import from_admin_local, utc_now_aware
from utils.ui import is_admin, update_preview

log = logging.getLogger(__name__)


async def start_editing(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        post, rss: bool = False) -> None:
    """Prompt the admin to type replacement text; live char-count comes with each edit."""
    cq = update.callback_query
    context.user_data["flow"] = ("edit_content", post.id, rss)
    kb = _exit_keyboard(post.id, rss)
    hint = (f"✏️ <b>Edit post #{post.id}</b>\n"
            f"Send the new text — I'll replace it in place.\n"
            f"(current: {effective_length(post.content or '')}/{config.X_CHAR_LIMIT} chars)")
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=cq.message.message_id,
            text=hint, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text=hint, reply_markup=kb, parse_mode="HTML")


def _exit_keyboard(post_id: int, rss: bool) -> "telegram.InlineKeyboardMarkup":
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    token = f"r{post_id}" if rss else str(post_id)
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Cancel edit",
                                                       callback_data=f"d:{token}:back")]])


async def start_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            post_id: int, date_iso: str | None,
                            from_queue: bool = False) -> None:
    cq = update.callback_query
    context.user_data["flow"] = ("custom_time", post_id, date_iso or "", from_queue)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    token = f"q{post_id}" if from_queue else str(post_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "↩️ Back", callback_data=f"cal:{token}:open")]])
    text = ("🕐 Send a custom time:\n"
            "• <code>HH:MM</code> (for the selected day)\n"
            "• <code>YYYY-MM-DD HH:MM</code>")
    if not date_iso:
        text += "\n(no day selected — use full datetime)"
    try:
        await cq.edit_message_text(text=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text=text, reply_markup=kb, parse_mode="HTML")


async def handle_flow_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called by the text/photo handlers first. Returns True if a flow consumed the msg."""
    flow = context.user_data.get("flow")
    if not flow or not update.message:
        return False
    msg = update.message
    chat_id = msg.chat.id

    if flow[0] == "edit_content":
        _, post_id, rss = flow
        post = crud.get_post(post_id)
        if post is None:
            context.user_data.pop("flow", None)
            await msg.reply_text("ℹ️ That draft no longer exists.")
            return True
        new_text = msg.text or ""
        if not new_text.strip():
            await msg.reply_text("⚠️ Empty text — send real content or ↩️ Cancel edit.")
            return True
        post = crud.update_post(post_id, content=new_text)
        context.user_data.pop("flow", None)
        n = effective_length(new_text)
        extra = (f"✏️ <b>Updated</b> · {n}/{config.X_CHAR_LIMIT} chars"
                 + (" ⚠️ over limit!" if n > config.X_CHAR_LIMIT else ""))
        await update_preview(context, chat_id, post, text_extra=extra, rss=rss)
        return True

    if flow[0] == "custom_time":
        _, post_id, date_iso, from_queue = flow
        raw = (msg.text or "").strip()
        when = _parse_custom(raw, date_iso)
        if when is None:
            await msg.reply_text("❌ Couldn't parse. Use HH:MM or YYYY-MM-DD HH:MM.")
            return True
        if when <= utc_now_aware():
            await msg.reply_text("⏰ That's in the past — pick a future time.")
            return True
        context.user_data.pop("flow", None)
        from handlers.scheduler_flow import reschedule_existing, save_schedule

        if from_queue:
            await reschedule_existing(context, chat_id, post_id, when)
        else:
            await save_schedule(context, chat_id, post_id, when)
        return True

    return False


def _parse_custom(raw: str, date_iso: str) -> datetime | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if m and date_iso:
        naive = datetime.fromisoformat(date_iso).replace(hour=int(m.group(1)),
                                                         minute=int(m.group(2)))
        return from_admin_local(naive)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", raw)
    if m:
        naive = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         int(m.group(4)), int(m.group(5)))
        return from_admin_local(naive)
    return None


# ------------------------------------------------------------- media ------

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo attached while editing a draft → attach to that draft; else new draft."""
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    msg = update.message
    photo = msg.photo[-1] if msg.photo else None
    if photo is None:
        return
    file = await context.bot.get_file(photo.file_id)
    fname = f"media_{msg.message_id}.jpg"
    path = os.path.join(config.MEDIA_DIR, fname)
    await file.download_to_drive(path)

    flow = context.user_data.get("flow")
    if flow and flow[0] == "edit_content":
        post = crud.update_post(flow[1], media_path=path)
        await update_preview(context, msg.chat.id, post, text_extra="📎 Media attached.")
        return
    caption = msg.text or "📷 (photo post)"
    from handlers.drafts import new_draft_message

    await new_draft_message(update, context, caption, media_path=path)
