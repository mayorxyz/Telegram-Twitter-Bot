from __future__ import annotations

import logging
import os
from datetime import timezone

from dotenv import load_dotenv
load_dotenv()  # ← must be before config is read

from telegram import BotCommand
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
from scheduler import jobs
from utils import config

log = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Set up bot commands, seed settings, repopulate scheduler, and start dashboard."""
    
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

    # 3. Start read-only local dashboard
    from web.server import start_dashboard
    start_dashboard()
    log.info("Dashboard running at http://localhost:%s", config.DASHBOARD_PORT or 3000)


async def ingest_text(update, context) -> None:
    """Plain text in: consume active input flows first, else create a draft."""
    from utils.ui import deny, is_admin

    if not is_admin(update):
        await deny(update, context)
        return

    msg = update.message
    if msg is None:
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
    """Video/document in: thread attach-slot flow consumes it; otherwise ignored.

    (Single-tweet media stays photo-only by design — see editor.handle_media.)
    """
    from utils.ui import deny, is_admin

    if not is_admin(update):
        await deny(update, context)
        return
    await threads.handle_thread_media(update, context)


def build_application() -> Application:
    token = os.getenv("BOT_TOKEN", config.BOT_TOKEN)
    if not token:
        raise SystemExit("BOT_TOKEN is not set — copy .env.example to .env and fill it in.")
    if not config.ADMIN_ID:
        raise SystemExit("ADMIN_ID is not set — copy .env.example to .env and fill it in.")

    # Build the app and attach post_init directly in the builder
    app = Application.builder().token(token).post_init(post_init).build()

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
    # 2) Calendar/time picker + immediate publish from cards: cal:, day:, hr:, mn:, now:, quick:
    app.add_handler(CallbackQueryHandler(scheduler_flow.sched_callback,
                                         pattern=r"^(cal|day|hr|mn|now|quick):"))
    # 3) Queue view pagination + item actions: q:<action>:<id>
    app.add_handler(CallbackQueryHandler(queue.queue_callback, pattern=r"^q:"))
    # 4) Queue delete two-step (10s undo window) + post-now aliases outside q: namespace
    app.add_handler(CallbackQueryHandler(queue.queue_callback,
                                         pattern=r"^(del_confirm|del_undo|post_now):"))
    # 5) Editor flow buttons: ed_save, ed_cancel, custom_time
    app.add_handler(CallbackQueryHandler(editor.editor_callback,
                                         pattern=r"^(ed_save|ed_cancel|custom_time):"))
    # 6) Settings menu + timezone picker: set:, tz:
    app.add_handler(CallbackQueryHandler(settings.settings_callback, pattern=r"^(set|tz):"))
    # 7) Templates management + injection into drafts: tpl:  (no conflict with t:)
    app.add_handler(CallbackQueryHandler(templates.template_callback, pattern=r"^tpl:"))
    # 8) Thread builder buttons: th: and thm: per-tweet media picker.
    app.add_handler(CallbackQueryHandler(threads.thread_callback, pattern=r"^th(m)?:"))

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    init_db()
    app = build_application()
    jobs.bootstrap(app)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()