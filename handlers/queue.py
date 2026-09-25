"""Queue view: paginated scheduled/draft list with per-item actions + undo delete."""
from __future__ import annotations

import logging
from datetime import timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import crud
from scheduler.jobs import unschedule_post_job
from utils import config
from utils.formatting import escape_html
from utils.timeutil import to_admin_tz
from utils.ui import is_admin, pop_undo, stash_for_undo

log = logging.getLogger(__name__)

PAGE_SIZE = 5


def _queue_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"q:ed:{post_id}"),
            InlineKeyboardButton("🕐 Reschedule", callback_data=f"q:rs:{post_id}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete", callback_data=f"q:del:{post_id}"),
            InlineKeyboardButton("⚡ Post Now", callback_data=f"now:{post_id}"),
        ],
    ])


async def render_queue(bot, chat_id: int, page: int = 0,
                       message_id: int | None = None) -> int | None:
    """Render one queue page. Edits in place when message_id given."""
    posts = [p for p in crud.list_posts(("scheduled", "draft"))]
    total_pages = max(1, (len(posts) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = posts[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines: list[str] = []
    if crud.get_bool("paused", False):
        lines.append("⏸ <b>PAUSED — publishing on hold</b>")
    lines.append(f"📋 <b>Queue</b> · {len(posts)} items · page {page + 1}/{total_pages}")
    lines.append("")

    rows: list[list[InlineKeyboardButton]] = []
    if not chunk:
        lines.append("<i>Nothing queued. Send me text to draft a post.</i>")
    for p in chunk:
        snippet = escape_html((p.content or "").replace("\n", " ")[:70])
        when = ""
        if p.status == "scheduled" and p.scheduled_at:
            loc = to_admin_tz(p.scheduled_at.replace(tzinfo=timezone.utc))
            when = f" 🕐 {loc:%d %b %H:%M}"
        elif p.status == "draft":
            when = " 📝 draft"
        tag = f" 🏷{p.tag}" if p.tag else ""
        lines.append(f"<b>#{p.id}</b>{when}{tag} · {snippet}")
        rows.append([InlineKeyboardButton(f"#{p.id}", callback_data=f"q:view:{p.id}")])
        r = []
        for label, cb in (
            ("✏️", f"q:ed:{p.id}"),
            ("🕐", f"q:rs:{p.id}"),
            ("🗑", f"q:del:{p.id}"),
            ("⚡", f"now:{p.id}"),
        ):
            r.append(InlineKeyboardButton(label, callback_data=cb))
        rows.append(r)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"q:p:{page - 1}"))
    nav.append(InlineKeyboardButton("🔄", callback_data="q:p:-1"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"q:p:{page + 1}"))
    rows.append(nav)
    kb = InlineKeyboardMarkup(rows)
    html = "\n".join(lines)

    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=html, reply_markup=kb, parse_mode="HTML")
            return message_id
        except Exception:
            pass
    sent = await bot.send_message(chat_id=chat_id, text=html, reply_markup=kb, parse_mode="HTML")
    return sent.message_id


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    await render_queue(context.bot, update.effective_chat.id, page=0)


async def q_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles q:* callbacks (pagination, edit, reschedule, delete, confirm, undo)."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    parts = cq.data.split(":")           # q:<action>:<arg>
    action, arg = parts[1], parts[2]
    chat_id = update.effective_chat.id
    msg_id = cq.message.message_id

    if action == "p":
        page = 0 if arg == "-1" else int(arg)
        await render_queue(context.bot, chat_id, page=page, message_id=msg_id)

    elif action == "view":
        post = crud.get_post(int(arg))
        if post is None:
            await render_queue(context.bot, chat_id, message_id=msg_id)
            return
        # open the full preview card for this post (draft actions apply)
        from utils.ui import show_preview

        fresh = crud.update_post(post.id, tg_message_id=None)
        await show_preview(context, chat_id, fresh)

    elif action == "ed":
        post = crud.get_post(int(arg))
        if post is None:
            return
        await start_editing_cq(update, context, post)

    elif action == "rs":
        from handlers.scheduler_flow import open_queue_picker

        await open_queue_picker(context, chat_id, int(arg))

    elif action == "del":
        post_id = int(arg)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data=f"dy:{post_id}"),
            InlineKeyboardButton("❌ No", callback_data="q:p:-1"),
        ]])
        await cq.edit_message_text(
            f"🗑 Delete post #{post_id}? This removes it from the queue.",
            reply_markup=kb)

    elif action == "back":
        await render_queue(context.bot, chat_id, message_id=msg_id)


async def start_editing_cq(update: Update, context: ContextTypes.DEFAULT_TYPE, post) -> None:
    """Queue → Edit: enter edit flow; result replaces the preview card of that post."""
    from handlers.editor import start_editing

    await start_editing(update, context, post, rss=False)


# ---------------------------------------------------- delete confirm/undo ---

async def dy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """dy:<post_id> → delete + offer 10s undo."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    post_id = int(cq.data.split(":")[1])
    post = crud.get_post(post_id)
    if post is None:
        await cq.edit_message_text("ℹ️ Already gone.")
        return
    stash_for_undo(post)
    unschedule_post_job(post_id)
    crud.delete_post(post_id)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Undo", callback_data=f"un:{post_id}")]])
    await cq.edit_message_text(
        f"🗑 Deleted post #{post_id}. <i>Undo available for 10 s.</i>",
        reply_markup=kb, parse_mode="HTML")

    async def expire(_ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=cq.message.chat_id, message_id=cq.message.message_id,
                reply_markup=None)
        except Exception:
            pass

    context.job_queue.run_once(expire, when=10)


async def un_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """un:<post_id> → restore within undo window."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    post_id = int(cq.data.split(":")[1])
    snapshot = pop_undo(post_id)
    if snapshot is None:
        await cq.edit_message_text("⌛ Undo window expired.")
        return
    from db.models import Post

    with crud.get_session() as s:
        merged = s.merge(Post(**{c: getattr(snapshot, c) for c in (
            "id", "content", "media_path", "status", "scheduled_at", "published_at",
            "tag", "auto_format", "tg_message_id", "tweet_id", "error")}))
        merged.created_at = snapshot.created_at
        s.commit()
    if snapshot.status == "scheduled" and snapshot.scheduled_at:
        from handlers.scheduler_flow import rearm_job

        rearm_job(post_id)
    await cq.edit_message_text(f"↩️ Restored post #{post_id}.")
