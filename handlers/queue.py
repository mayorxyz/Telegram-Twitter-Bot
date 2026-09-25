"""Queue view: paginated scheduled/draft list with per-item actions + 10s undo delete."""
from __future__ import annotations

import logging
import time
from datetime import timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import crud
from scheduler.jobs import unschedule_post_job
from utils import config
from utils.formatting import escape_html
from utils.timeutil import to_admin_tz
from utils.ui import is_admin

log = logging.getLogger(__name__)

PAGE_SIZE = 5
UNDO_WINDOW_SECONDS = 10


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
    posts = crud.list_posts(("scheduled", "draft"))
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
            log.debug("queue edit failed, sending fresh page")
    sent = await bot.send_message(chat_id=chat_id, text=html, reply_markup=kb, parse_mode="HTML")
    return sent.message_id


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    await render_queue(context.bot, update.effective_chat.id, page=0)


# ------------------------------------------------------------- internals ---

def _pending(context: ContextTypes.DEFAULT_TYPE) -> dict[int, float]:
    """bot_data-backed pending_delete map: post_id -> deletion timestamp."""
    return context.bot_data.setdefault("pending_delete", {})


def _stash(post, when_utc: timezone | None = None) -> None:
    """Full column snapshot so we can restore the exact row after undo."""
    from db.models import Post

    data = {c.name: getattr(post, c.name) for c in Post.__table__.columns}
    _undo_snapshots[post.id] = data


_undo_snapshots: dict[int, dict] = {}


def _restore(post_id: int) -> bool:
    data = _undo_snapshots.pop(post_id, None)
    if data is None:
        return False
    from db.models import Post

    with crud.get_session() as s:
        s.merge(Post(**data))
        s.commit()
    return True


async def _render_page0(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    await render_queue(context.bot, chat_id, page=0)


# --------------------------------------------------------------- callback --

async def queue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles q:* pagination/item actions plus del_confirm:, del_undo:, post_now:, now:."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return

    chat_id = update.effective_chat.id
    msg_id = cq.message.message_id if cq.message else None
    parts = cq.data.split(":")
    head = parts[0]

    # ---- top-level aliases registered outside the q: namespace --------------
    if head in ("del_confirm", "del_undo"):
        await _handle_delete_step(update, context, head, int(parts[1]))
        return

    if head in ("now", "post_now"):
        from scheduler.publisher import publish_post

        post_id = int(parts[1])
        await cq.edit_message_text(f"⚡ Publishing #{post_id}…")
        await publish_post(context.application, post_id, manual=True)
        await _render_page0(context, chat_id)
        return

    if head != "q":
        return

    action = parts[1]
    arg = parts[2] if len(parts) > 2 else ""

    if action == "p":
        page = 0 if arg == "-1" else int(arg)
        await render_queue(context.bot, chat_id, page=page, message_id=msg_id)

    elif action == "view":
        post = crud.get_post(int(arg))
        if post is None:
            await render_queue(context.bot, chat_id, message_id=msg_id)
            return
        from utils.ui import show_preview

        fresh = crud.update_post(post.id, tg_message_id=None)
        await show_preview(context, chat_id, fresh)

    elif action == "ed":
        post = crud.get_post(int(arg))
        if post is None:
            await cq.edit_message_text("ℹ️ Post not found.")
            return
        await start_editing_cq(update, context, post)

    elif action == "rs":
        from handlers.scheduler_flow import open_queue_picker

        await open_queue_picker(context, chat_id, int(arg))

    elif action == "del":
        post_id = int(arg)
        _pending(context)[post_id] = time.time()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm Delete", callback_data=f"del_confirm:{post_id}"),
            InlineKeyboardButton("↩️ Undo", callback_data=f"del_undo:{post_id}"),
        ]])
        await cq.edit_message_text(
            f"🗑 Delete post #{post_id}? Undo window: {UNDO_WINDOW_SECONDS} s.",
            reply_markup=kb)

    elif action == "back":
        await render_queue(context.bot, chat_id, message_id=msg_id)


async def _handle_delete_step(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              step: str, post_id: int) -> None:
    """del_confirm:<id> / del_undo:<id> — 10s window stored in context.bot_data."""
    cq = update.callback_query
    chat_id = update.effective_chat.id
    pending = _pending(context)
    ts = pending.get(post_id)
    expired = ts is None or (time.time() - ts) >= UNDO_WINDOW_SECONDS

    if step == "del_confirm":
        if expired:
            await cq.edit_message_text("⏱ Undo window expired.")
        else:
            post = crud.get_post(post_id)
            if post is not None:
                _stash(post)
                unschedule_post_job(post_id)
                crud.delete_post(post_id)
            pending.pop(post_id, None)
            await cq.edit_message_text("🗑 Deleted.")
        await _render_page0(context, chat_id)
        return

    # step == "del_undo"
    if expired:
        await cq.edit_message_text("⏱ Undo window expired.")
        await _render_page0(context, chat_id)
        return
    pending.pop(post_id, None)
    if _restore(post_id):
        post = crud.get_post(post_id)
        if post is not None and post.status == "scheduled" and post.scheduled_at:
            from scheduler.jobs import schedule_post_job

            when = post.scheduled_at
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            schedule_post_job(post_id, when)
    await cq.edit_message_text("↩️ Restored.")
    await _render_page0(context, chat_id)


async def start_editing_cq(update: Update, context: ContextTypes.DEFAULT_TYPE, post) -> None:
    """Queue → Edit: enter edit flow; result replaces the preview card of that post."""
    from handlers.editor import start_editing

    await start_editing(update, context, post, rss=False)
