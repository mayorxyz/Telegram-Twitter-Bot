from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import timezone

from dotenv import load_dotenv
load_dotenv()  # ← must be before config is read

from telegram import BotCommand
from telegram.error import Conflict, InvalidToken, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from db import crud
from db.database import init_db
from handlers import drafts, editor, queue, report, scheduler_flow, settings, templates, threads
from handlers.scheduler_flow import on_intent_done
from scheduler import jobs
from utils import config, single_instance

log = logging.getLogger(__name__)

# --- one long-poller per bot token ------------------------------------------
# Telegram serves ONE getUpdates consumer per token. A second poller (another
# copy of this bot, a leftover process, another project reusing BOT_TOKEN, a
# webhook) makes getUpdates answer "Conflict: terminated by other getUpdates
# request" forever. We therefore (a) refuse to start when the local lock is held,
# (b) probe the token at boot, and (c) classify the runtime Conflict instead of
# dumping a traceback per retry.
CONFLICT_RESET_AFTER = 120.0  # seconds of quiet that end a conflict streak
CONFLICT_ABORT_AFTER = config.CONFLICT_ABORT_AFTER  # int, 0 = warn only, never stop

_CONFLICT_HINT = (
    "409 Conflict — another process is polling this bot token "
    "(Telegram: 'terminated by other getUpdates request'). Only ONE long-polling "
    "consumer may run per token. Look for a second copy of this bot, a leftover "
    "'python main.py', another project reusing BOT_TOKEN, or a configured webhook; "
    "if the token ever leaked, revoke it in @BotFather and update .env."
)

_conflicts = {"streak": 0, "last": 0.0}

# PTB raises InvalidToken before any polling starts, so this is the one token
# failure that is neither a conflict nor a network blip.
_TOKEN_REJECTED_HINT = (
    "Telegram rejected BOT_TOKEN — copy the current token from @BotFather "
    "(/mybots → API Token) into .env: a revoked, mistyped, or whitespace-padded "
    "token can never poll. Telegram said: "
)


class StartupError(RuntimeError):
    """Startup preflight failed; the message is user-facing and actionable."""


def _soften_stream_errors() -> None:
    """Never crash on a non-ASCII log message when output is piped or redirected.

    Windows falls back to the locale encoding (cp1252 on most machines) as soon as
    stdout/stderr are redirected, and messages here contain "—" and "→". An
    unencodable character would make ``logging`` print a "--- Logging error ---"
    block instead of the real line, exactly the noise this module removes.
    ``errors="replace"`` keeps the encoding and degrades odd glyphs to "?".
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # detached or closed stream
            pass


def _note_conflict(application: Application | None) -> None:
    """Rate-limit conflict logging and stop cleanly after a long streak."""
    now = time.monotonic()
    if now - _conflicts["last"] > CONFLICT_RESET_AFTER:
        _conflicts["streak"] = 0  # the token worked again in between
    _conflicts["last"] = now
    _conflicts["streak"] += 1
    streak = _conflicts["streak"]

    if streak == 1:
        log.error("%s", _CONFLICT_HINT)
    elif streak % 10 == 0:
        log.warning("still conflicting after %d consecutive 409s — polling is dead "
                    "until the other poller stops", streak)

    if CONFLICT_ABORT_AFTER and streak >= CONFLICT_ABORT_AFTER:
        log.critical("stopping after %d consecutive 409 conflicts: this token has "
                     "another poller, so this instance can never receive updates. "
                     "Fix the cause, then restart (CONFLICT_ABORT_AFTER=0 disables "
                     "this auto-stop).", streak)
        if application is not None:
            application.stop_running()


async def on_error(update, context) -> None:
    """Classify PTB errors: a conflict is one actionable line, not a traceback."""
    error = context.error
    if isinstance(error, Conflict):
        _note_conflict(context.application)  # update is None for polling errors
        return
    if isinstance(error, (TimedOut, NetworkError)):
        log.warning("network hiccup while talking to Telegram: %s", error)
        return
    log.error("unhandled error while processing update %s",
              getattr(update, "update_id", None), exc_info=error)



async def _preflight(application: Application) -> None:
    """Refuse to poll a token that someone else owns.

    Best-effort but fail-fast: a webhook (which silently swallows getUpdates) is
    removed, and the update queue is probed once so an *actively polling* twin is
    reported at boot instead of after minutes of 409 retries. Network blips never
    block startup — only a real Conflict does.
    """
    bot = application.bot

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        info = await bot.get_webhook_info()
        if info.url:
            log.warning("startup: removed webhook %s so long polling can own this token",
                        info.url)
        else:
            log.info("startup: no webhook set — long polling owns this token")
    except Exception as exc:  # a webhook lookup must never block startup
        log.warning("startup: could not check the webhook (%s) — continuing", exc)

    try:
        await bot.get_updates(timeout=0, limit=1)  # probe: is a twin polling right now?
    except Conflict as exc:
        raise StartupError(f"{_CONFLICT_HINT} Telegram said: {exc}") from None
    except InvalidToken as exc:
        # Reachable when the token is revoked between initialize() and this probe.
        raise StartupError(_TOKEN_REJECTED_HINT + str(exc)) from None
    except (TimedOut, NetworkError) as exc:
        log.warning("startup: poll probe skipped (%s)", exc)


async def post_init(application: Application) -> None:
    """Preflight the token, set up commands, seed settings, arm jobs, start dashboard."""

    # 0. Fail fast when another poller (or a webhook) owns this bot token.
    await _preflight(application)

    # 1. Set up bot commands menu
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("help", "Show help"),
        BotCommand("draft", "Create a new tweet draft"),
        BotCommand("thread", "Create a new thread"),
        BotCommand("queue", "View scheduled posts"),
        BotCommand("pause", "Pause/resume posting"),
        BotCommand("report", "View posting statistics"),
        BotCommand("settings", "Bot settings"),
    ]
    await application.bot.set_my_commands(commands)
    log.info("Bot command menu set.")

    # 2. Seed default settings and repopulate the scheduler from the DB.
    if not crud.get_setting("timezone"):
        crud.set_setting("timezone", config.TIMEZONE or "UTC")
    if crud.get_setting("auto_format", "") == "":
        crud.set_bool("auto_format", True)
    if crud.get_setting("paused", "") == "":
        crud.set_bool("paused", False)
    if not crud.get_setting("rss_url") and config.RSS_URL:
        crud.set_setting("rss_url", config.RSS_URL)

    for post in crud.scheduled_posts():
        if post.scheduled_at is None:
            continue
        when = post.scheduled_at
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        jobs.schedule_post_job(post.id, when)
    log.info("startup reload: %d scheduled posts re-armed", crud.count_posts("scheduled"))

    # 3. Start read-only local dashboard (binds DASHBOARD_PORT, default 3000)
    from web.server import start_dashboard
    dashboard = start_dashboard()
    if dashboard is not None:
        host, port = dashboard.server_address[:2]
        log.info("Dashboard running at http://%s:%s", host, port)


async def post_shutdown(application: Application) -> None:
    """Stop the scheduler and dashboard, then release the single-instance lock."""
    try:
        if jobs.scheduler.running:
            # AsyncIOScheduler.shutdown is deferred onto the loop, so yield once to
            # let it actually run before the loop closes.
            jobs.scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
            log.info("shutdown: scheduler stopped")
    except Exception:
        log.exception("shutdown: scheduler shutdown failed")

    try:
        from web.server import stop_dashboard

        stop_dashboard()
    except Exception:
        log.exception("shutdown: dashboard shutdown failed")

    single_instance.release_default()
    log.info("shutdown complete")


_MENU_MAP = {
    "📝 New Tweet":  "new_tweet",
    "🧵 New Thread": "thread",
    "📋 Queue":      "queue",
    "⚙️ Settings":   "settings",
    "📊 Report":     "report",
    "📄 Templates":  "template",
}


async def ingest_text(update, context) -> None:
    """Plain text in: consume active input flows first, else create a draft."""
    from utils.ui import deny, is_admin

    if not is_admin(update):
        await deny(update, context)
        return

    msg = update.message
    if msg is None:
        return

    raw_text = (msg.text or "").strip()
    menu_action = _MENU_MAP.get(raw_text)
    if menu_action:
        if menu_action == "new_tweet":
            await msg.reply_text("✏️ Send your tweet text.")
            return
        elif menu_action == "thread":
            await threads.thread_command(update, context)
            return
        elif menu_action == "queue":
            await queue.queue_command(update, context)
            return
        elif menu_action == "settings":
            await settings.settings_command(update, context)
            return
        elif menu_action == "report":
            await report.report_command(update, context)
            return
        elif menu_action == "template":
            await templates.template_command(update, context)
            return

    if await editor.handle_flow_message(update, context):
        return

    if await settings.handle_timezone_input(update, context):
        return

    if await templates.handle_template_input(update, context):
        return

    if await threads.handle_thread_input(update, context):
        return

    text = (msg.text or "").strip()
    if not text:
        return
    from utils.formatting import twitter_len
    if twitter_len(text) > config.X_CHAR_LIMIT:
        await editor._handle_long_text(update, context, text=text)
        return
    await drafts.new_draft_message(update, context, text)



async def ingest_photo(update, context) -> None:
    """Photo in: attach to an open edit/thread flow or start a new photo draft."""
    from utils.ui import deny, is_admin

    if not is_admin(update):
        await deny(update, context)
        return
    if await threads.handle_thread_media(update, context):
        return
    await editor.handle_media(update, context)


async def ingest_video(update, context) -> None:
    """Video/gif/document in: opens the media flow (single tweet or thread)."""
    from utils.ui import deny, is_admin

    if not is_admin(update):
        await deny(update, context)
        return
    await threads.handle_thread_media(update, context)


def build_application() -> Application:
    token = os.getenv("BOT_TOKEN", config.BOT_TOKEN)
    if not token:
        raise SystemExit("BOT_TOKEN is not set — copy .env.example to .env and fill it in.")
    if not config.ADMIN_IDS:
        raise SystemExit("ADMIN_ID or ADMIN_IDS is not set — copy .env.example to .env and fill it in.")

    # Build the app with the startup/shutdown hooks attached in the builder
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ---- command handlers (registered before generic callbacks/messages) ----
    app.add_handler(CommandHandler("start", settings.start_command))
    app.add_handler(CommandHandler("help", settings.help_command))
    app.add_handler(CommandHandler("queue", queue.queue_command))
    app.add_handler(CommandHandler("pause", settings.pause_command))
    app.add_handler(CommandHandler("resume", settings.resume_command))
    app.add_handler(CommandHandler("settings", settings.settings_command))
    app.add_handler(CommandHandler("set_tz", settings.set_tz_command))
    app.add_handler(CommandHandler("template", templates.template_command))
    app.add_handler(CommandHandler("report", report.report_command))
    app.add_handler(CommandHandler("thread", threads.thread_command))

    # ---- plain messages: text/photo ingestion (draft + edit/custom-time flows) --
    app.add_handler(MessageHandler(filters.PHOTO, ingest_photo))
    # videos/documents are consumed only while the thread attach-slot flow is open
    app.add_handler(MessageHandler((filters.VIDEO | filters.ANIMATION) & ~filters.COMMAND,
                                   ingest_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ingest_text))

    # ---- inline keyboard callbacks -------------------------------------------
    # Registration order matters: PTB dispatches to the FIRST handler whose
    # pattern matches. Patterns below are mutually exclusive.
    # 1) Draft cards: d:<id>:<action>, tag:, dup:, f: (failure actions), st: (stats)
    app.add_handler(CallbackQueryHandler(drafts.draft_callback, pattern=r"^(d|tag|dup|f|st):"))
    # 2) Calendar/time picker + immediate publish from cards: cal:, day:, hr:, mn:, now:, quick:, post_intent:
    app.add_handler(CallbackQueryHandler(scheduler_flow.sched_callback,
                                         pattern=r"^(cal|day|hr|mn|now|quick|post_intent):"))
    # 3) Queue view pagination + item actions: q:<action>:<id>
    app.add_handler(CallbackQueryHandler(queue.queue_callback, pattern=r"^q:"))
    # 4) Queue delete two-step (10s undo window) + post-now aliases outside q: namespace
    app.add_handler(CallbackQueryHandler(queue.queue_callback,
                                         pattern=r"^(del_confirm|del_undo|post_now):"))
    # 5) Editor flow buttons: ed_save, ed_cancel, custom_time, comp
    app.add_handler(CallbackQueryHandler(editor.editor_callback,
                                         pattern=r"^(ed_save|ed_cancel|custom_time|comp|comp_):?"))


    # 6) Settings menu + timezone picker: set:, tz:
    app.add_handler(CallbackQueryHandler(settings.settings_callback, pattern=r"^(set|tz):"))
    # 7) Templates management + injection into drafts: tpl:  (no conflict with t:)
    app.add_handler(CallbackQueryHandler(templates.template_callback, pattern=r"^tpl:"))
    # 8) Thread builder buttons: th:, thm:, AND media: actions
    app.add_handler(CallbackQueryHandler(threads.thread_callback, pattern=r"^(th(m)?|media):"))

    # ---- global error classification: 409 conflicts / network blips ---------
    app.add_error_handler(on_error)

    # ---- intent-done: mark a manually-posted tweet as posted_manual -----------
    app.add_handler(CallbackQueryHandler(on_intent_done, pattern=r"^intent_done:"))

    return app


def main() -> None:
    _soften_stream_errors()  # before any handler exists (see the docstring above)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # One long-poller per token: refuse to start next to a live instance.
    try:
        single_instance.acquire_default()
    except single_instance.AlreadyRunningError as exc:
        log.error("%s", exc)
        raise SystemExit(2)

    try:
        init_db()
        app = build_application()
        jobs.bootstrap(app)
        app.run_polling(drop_pending_updates=True)
    except StartupError as exc:
        log.error("startup aborted: %s", exc)
        raise SystemExit(1)
    except InvalidToken as exc:
        # Raised by bot.initialize()/get_me() before polling: no traceback needed.
        log.error("%s", _TOKEN_REJECTED_HINT + str(exc))
        raise SystemExit(1)
    finally:
        single_instance.release_default()  # backstop; post_shutdown does it too


if __name__ == "__main__":
    main()