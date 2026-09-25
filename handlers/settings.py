"""/settings menu, /pause + /resume kill switch, timezone persistence.

All flags live in the settings table (key/value) so they survive restarts.
"""
from __future__ import annotations

import logging
from zoneinfo import available_timezones

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import crud
from utils import config
from utils.ui import is_admin

log = logging.getLogger(__name__)


# ------------------------------------------------------------- keyboards ---

def _menu_kb() -> InlineKeyboardMarkup:
    af_on = crud.get_bool("auto_format", True)
    paused = crud.get_bool("paused", False)
    tz = crud.get_setting("timezone", "UTC") or "UTC"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✓ Auto-Format: {'ON' if af_on else 'OFF'}",
                              callback_data="set:af")],
        [InlineKeyboardButton("⏸ Pause publishing" if not paused else "▶️ Resume publishing",
                              callback_data="set:pause")],
        [InlineKeyboardButton(f"🌍 Change Timezone ({tz})", callback_data="tz:prompt")],
    ])


def _tz_kb() -> InlineKeyboardMarkup:
    rows = []
    popular = ["UTC", "Africa/Lagos", "Europe/London", "America/New_York",
               "America/Los_Angeles", "Asia/Dubai", "Asia/Kolkata", "Australia/Sydney"]
    cur = [InlineKeyboardButton(t, callback_data=f"tz:set:{t}") for t in popular[:4]]
    rows.append(cur)
    rows.append([InlineKeyboardButton(t, callback_data=f"tz:set:{t}") for t in popular[4:]])
    rows.append([InlineKeyboardButton("✍️ Type custom timezone", callback_data="tz:prompt")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="set:back")])
    return InlineKeyboardMarkup(rows)


async def _settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render/refresh the settings card in place."""
    cq = update.callback_query
    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"🌍 Timezone: <code>{crud.get_setting('timezone', 'UTC') or 'UTC'}</code>\n"
        f"✓ Auto-format: <b>{'ON' if crud.get_bool('auto_format', True) else 'OFF'}</b> "
        "(applied at publish time only)\n"
        f"⏯ Publishing: <b>{'PAUSED' if crud.get_bool('paused', False) else 'ACTIVE'}</b>\n"
        f"📡 RSS quota today: {crud.get_setting('rss_count_today', '0|0')} "
        f"(max {config.RSS_MAX_PER_DAY}/day)"
    )
    kb = _menu_kb()
    if cq:
        try:
            await cq.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            log.debug("settings edit failed, sending fresh menu")
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=text, reply_markup=kb, parse_mode="HTML")


# --------------------------------------------------------------- commands --

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    help_text = (
        "🤖 <b>X Poster</b> — solo automation bot.\n\n"
        "Send me any text → I draft it for review.\n"
        "Commands:\n"
        "• /queue — paginated scheduled/draft list\n"
        "• /settings — auto-format, timezone, pause\n"
        "• /pause — hold all publishing\n"
        "• /resume — release the hold\n"
        "• /set_tz &lt;Area/City&gt; — store your timezone\n"
        "• /help — this message"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    await start_command(update, context)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pause → paused=true in DB; scheduler skips due posts until /resume."""
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    crud.set_bool("paused", True)
    count = crud.count_posts("scheduled")
    await update.message.reply_text(f"⏸ Paused — {count} posts on hold")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resume → paused=false; re-arm every future scheduled post immediately."""
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    was_paused = crud.get_bool("paused", False)
    crud.set_bool("paused", False)
    if was_paused:
        from datetime import timezone as dt_tz

        from scheduler.jobs import schedule_post_job
        from utils.timeutil import utc_now_aware

        now = utc_now_aware()
        for post in crud.scheduled_posts():
            if post.scheduled_at is None:
                continue
            when = post.scheduled_at
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt_tz.utc)
            if when <= now:
                when = now  # missed while paused -> fire ASAP
            schedule_post_job(post.id, when)
    await update.message.reply_text("▶️ Resumed — resuming queue")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    await _settings_menu(update, context)


async def set_tz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_tz [Area/City] — with arg: validate+save; without: prompt for input."""
    if not is_admin(update):
        from utils.ui import deny

        await deny(update, context)
        return
    args = " ".join(context.args).strip() if context.args else ""
    if not args:
        context.user_data["tz_wait"] = True
        await update.message.reply_text(
            "🌍 Send a timezone string, e.g. <code>Africa/Lagos</code> or "
            "<code>Europe/Berlin</code>.", parse_mode="HTML")
        return
    await _save_timezone(update, context, args)


# -------------------------------------------------------------- callbacks --

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles set:* and tz:* inline callbacks."""
    cq = update.callback_query
    await cq.answer()
    if not is_admin(update):
        return
    parts = cq.data.split(":")
    head, action = parts[0], parts[1]

    if head == "set":
        if action == "af":
            new_val = not crud.get_bool("auto_format", True)
            crud.set_bool("auto_format", new_val)
            await _settings_menu(update, context)
        elif action == "pause":
            new_val = not crud.get_bool("paused", False)
            crud.set_bool("paused", new_val)
            if not new_val:
                await resume_from_toggle(context)
            await _settings_menu(update, context)
        elif action == "back":
            await _settings_menu(update, context)
        else:
            await _settings_menu(update, context)
        return

    if head == "tz":
        if action == "prompt":
            context.user_data["tz_wait"] = True
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back",
                                                             callback_data="set:back")]])
            await cq.edit_message_text(
                "🌍 Send a timezone string (IANA name), e.g. <code>Africa/Lagos</code>.",
                reply_markup=kb, parse_mode="HTML")
        elif action == "set":
            tz_name = ":".join(parts[2:])  # names never contain ':' but be safe
            await _save_timezone_cq(update, context, tz_name)
        return


async def handle_timezone_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called by main's message ingestion BEFORE the draft flow.

    Returns True when the text was consumed as a timezone answer."""
    if not context.user_data.pop("tz_wait", False):
        return False
    raw = (update.message.text or "").strip()
    if not raw:
        context.user_data["tz_wait"] = True
        await update.message.reply_text("⚠️ Empty — send a timezone like Africa/Lagos.")
        return True
    await _save_timezone(update, context, raw)
    return True


# ---------------------------------------------------------------- helpers --

def _valid_timezone(name: str) -> bool:
    return name in available_timezones()


async def _save_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         name: str) -> None:
    if not _valid_timezone(name):
        await update.message.reply_text(
            f"❌ '{name}' is not a valid IANA timezone (try Africa/Lagos).")
        return
    crud.set_setting("timezone", name)
    await update.message.reply_text(f"🌍 Timezone saved: {name}")


async def _save_timezone_cq(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            name: str) -> None:
    if not _valid_timezone(name):
        await update.callback_query.answer("Invalid timezone", show_alert=True)
        return
    crud.set_setting("timezone", name)
    await _settings_menu(update, context)


async def resume_from_toggle(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-arm jobs after un-pausing via the settings toggle."""
    from datetime import timezone as dt_tz

    from scheduler.jobs import schedule_post_job
    from utils.timeutil import utc_now_aware

    now = utc_now_aware()
    for post in crud.scheduled_posts():
        if post.scheduled_at is None:
            continue
        when = post.scheduled_at
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt_tz.utc)
        if when <= now:
            when = now
        schedule_post_job(post.id, when)
