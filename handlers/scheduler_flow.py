"""Scheduling flow: entry buttons → month calendar → hours → minutes → save."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from db import crud
from handlers.calendar_kb import (
    build_dt_utc,
    calendar_keyboard,
    entry_keyboard,
    hours_keyboard,
    minutes_keyboard,
    shift_month,
)
from scheduler.jobs import schedule_post_job
from utils.formatting import escape_html, format_preview
from utils.timeutil import admin_tz, to_admin_tz, utc_now_aware
from utils.ui import is_admin, show_preview

log = logging.getLogger(__name__)

# per-chat calendar cursor: chat_id -> (year, month)  (UI-only state; safe in memory)
_cal_cursor: dict[int, tuple[int, int]] = {}


def _get_cursor(chat_id: int) -> tuple[int, int]:
    if chat_id in _cal_cursor:
        return _cal_cursor[chat_id]
    now = utc_now_aware().astimezone(admin_tz())
    return now.year, now.month


async def set_calendar_view(bot, chat_id: int, message_id: int, post_id: int,
                            year: int, month: int) -> None:
    """Render the month grid for one post into an existing message."""
    _cal_cursor[chat_id] = (year, month)
    kb = calendar_keyboard(post_id, year, month)
    label = (f"📅 {calendar_name(year, month)} — pick a day "
             f"({crud.get_setting('timezone', 'UTC')})")
    await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                text=label, reply_markup=kb)


def calendar_name(year: int, month: int) -> str:
    import calendar as _c

    return f"{_c.month_name[month]} {year}"


async def enter_scheduling(context: ContextTypes.DEFAULT_TYPE, chat_id: int, post) -> None:
    """Replace the draft card with the scheduling entry view."""
    tz = crud.get_setting("timezone", "") or "UTC"
    html = (f"<b>Schedule post #{post.id}</b>  ·  tz: <code>{tz}</code>\n\n"
            f"{format_preview(post)}")
    kb = entry_keyboard(post.id)
    if post.tg_message_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=post.tg_message_id,
                                                text=html, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    sent = await context.bot.send_message(chat_id=chat_id, text=html,
                                          reply_markup=kb, parse_mode="HTML")
    crud.update_post(post.id, tg_message_id=sent.message_id)
    post.tg_message_id = sent.message_id


async def save_schedule(context: ContextTypes.DEFAULT_TYPE, chat_id: int, post_id: int,
                        when_utc: datetime) -> None:
    """Persist scheduled status + register APScheduler job."""
    post = crud.update_post(post_id, status="scheduled", scheduled_at=when_utc, error=None)
    if post is None:
        return
    schedule_post_job(post_id, when_utc)
    loc = to_admin_tz(when_utc)
    html = (f"🕐 <b>Scheduled!</b> Post #{post_id} will fire "
            f"{loc:%a %d %b %Y %H:%M} ({crud.get_setting('timezone', 'UTC')})\n\n"
            f"{format_preview(post)}")
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🕐 Reschedule", callback_data=f"cal:{post_id}:open"),
        InlineKeyboardButton("⚡ Post Now", callback_data=f"now:{post_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"d:{post_id}:cancel"),
    ]])
    if post.tg_message_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=post.tg_message_id,
                                                text=html, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    sent = await context.bot.send_message(chat_id=chat_id, text=html,
                                          reply_markup=kb, parse_mode="HTML")
    crud.update_post(post_id, tg_message_id=sent.message_id)


async def sched_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles cal:, day:, hr:, mn:, now:, quick: callbacks.

    post_id token may be 'q<id>' → queue reschedule mode (updates existing scheduled post).
    """
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    parts = cq.data.split(":")
    kind = parts[0]
    chat_id = update.effective_chat.id
    msg_id = cq.message.message_id

    def parse_pid(token: str) -> tuple[int, bool]:
        # returns (post_id, from_queue)
        if token.startswith("q"):
            return int(token[1:]), True
        return int(token), False

    if kind == "cal":
        post_id, _from_q = parse_pid(parts[1])
        token = parts[2]
        y, m = _get_cursor(chat_id)
        if token == "prev":
            y, m = shift_month(datetime(y, m, 1), -1)
        elif token == "next":
            y, m = shift_month(datetime(y, m, 1), +1)
        else:  # open / noop -> jump to current month
            now = utc_now_aware().astimezone(admin_tz())
            y, m = now.year, now.month
        await set_calendar_view(context.bot, chat_id, msg_id, post_id, y, m)

    elif kind == "day":
        post_id, from_q = parse_pid(parts[1])
        date_iso = parts[2]
        kb = hours_keyboard(parts[1], date_iso)
        d = datetime.fromisoformat(date_iso)
        text = f"🕐 {d:%A %d %B %Y} — pick an hour ({crud.get_setting('timezone', 'UTC')}):"
        await _safe_edit(context.bot, chat_id, msg_id, text, kb,
                         lambda: _reopen_picker(context, chat_id, post_id, from_q))

    elif kind == "hr":
        pid_token, date_iso, hh = parts[1], parts[2], parts[3]
        kb = minutes_keyboard(pid_token, date_iso, int(hh))
        await _safe_edit(context.bot, chat_id, msg_id,
                         f"⏱ {date_iso} {hh}:xx — pick minutes:", kb,
                         lambda: _reopen_picker(
                             context, chat_id, *parse_pid(pid_token)))

    elif kind == "mn":
        pid_token, date_iso, hh, mm = parts[1], parts[2], parts[3], parts[4]
        if mm == "c" or hh == "c":
            from handlers.editor import start_custom_time

            post_id, from_q = parse_pid(pid_token)
            await start_custom_time(update, context, post_id, date_iso, from_queue=from_q)
            return
        when = build_dt_utc(date_iso, int(hh), int(mm))
        if when <= utc_now_aware():
            await cq.answer("⏰ That time is in the past", show_alert=True)
            return
        post_id, from_q = parse_pid(pid_token)
        if from_q:
            await reschedule_existing(context, chat_id, post_id, when)
        else:
            await save_schedule(context, chat_id, post_id, when)

    elif kind == "quick":
        pid_token, minutes = parts[1], parts[2]
        post_id, from_q = parse_pid(pid_token)
        when = utc_now_aware() + timedelta(minutes=int(minutes))
        if from_q:
            await reschedule_existing(context, chat_id, post_id, when)
        else:
            await save_schedule(context, chat_id, post_id, when)

    elif kind == "now":
        post_id = int(parts[1])
        from scheduler.publisher import publish_post

        post = crud.get_post(post_id)
        if post is None:
            await cq.edit_message_text("ℹ️ Post not found.")
            return
        await cq.edit_message_text(f"⚡ Publishing post #{post_id}…")
        ok = await publish_post(context.application, post_id, manual=True)
        if not ok:
            log.info("manual publish of %s failed", post_id)


async def _safe_edit(bot, chat_id: int, msg_id: int, text: str, kb, on_fail) -> None:
    """Edit picker message; if it's a stale queue row, run fallback instead."""
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                    text=text, reply_markup=kb)
    except Exception:
        await on_fail()


async def _reopen_picker(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                         post_id: int, from_queue: bool) -> None:
    """When a picker step can't edit its message (e.g. opened from a queue row)."""
    if from_queue:
        await open_queue_picker(context, chat_id, post_id)
    else:
        post = crud.get_post(post_id)
        if post:
            await enter_scheduling(context, chat_id, post)


async def open_queue_picker(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                            post_id: int) -> None:
    """Reschedule flow launched from /queue: send a fresh calendar card for that post."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    post = crud.get_post(post_id)
    if post is None:
        await context.bot.send_message(chat_id=chat_id, text="ℹ️ Post disappeared.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 Pick Date", callback_data=f"cal:q{post_id}:open"),
        InlineKeyboardButton("⏱ +30 min", callback_data=f"quick:q{post_id}:30"),
        InlineKeyboardButton("↩️ Queue", callback_data="q:back"),
    ]])
    await context.bot.send_message(
        chat_id=chat_id,
        text=(f"🕐 Reschedule post #{post_id}\n"
              f"<pre>{escape_html((post.content or '')[:150])}</pre>"),
        reply_markup=kb, parse_mode="HTML")


async def reschedule_existing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, post_id: int,
                              when_utc: datetime) -> None:
    """Update scheduled_at on an already-scheduled post + re-arm its job."""
    post = crud.update_post(post_id, status="scheduled", scheduled_at=when_utc, error=None)
    if post is None:
        return
    schedule_post_job(post_id, when_utc)
    loc = to_admin_tz(when_utc)
    html = (f"🕐 <b>Rescheduled!</b> Post #{post_id} now fires "
            f"{loc:%a %d %b %Y %H:%M} ({crud.get_setting('timezone', 'UTC')})\n\n"
            f"{format_preview(post)}")
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🕐 Reschedule", callback_data=f"cal:q{post_id}:open"),
        InlineKeyboardButton("⚡ Post Now", callback_data=f"now:{post_id}"),
        InlineKeyboardButton("↩️ Queue", callback_data="q:back"),
    ]])
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=post.tg_message_id,
                                            text=html, reply_markup=kb, parse_mode="HTML")
    except Exception:
        sent = await context.bot.send_message(chat_id=chat_id, text=html,
                                              reply_markup=kb, parse_mode="HTML")
        crud.update_post(post_id, tg_message_id=sent.message_id)
