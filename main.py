"""Entry point: build the PTB application, wire handlers, bootstrap APScheduler.

Solo-use bot — ADMIN_ID is enforced inside each handler (utils.ui.is_admin).
All state lives in SQLite so the bot survives restarts; on startup every
status=scheduled post is re-armed as an APScheduler date job.
"""
from __future__ import annotations

import logging
import os
from datetime import timezone

from dotenv import load_dotenv

load_dotenv()

from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from db import crud  # noqa: E402
from db.database import init_db  # noqa: E402
from handlers import (  # noqa: E402
    drafts,
    editor,
    queue,
    report,
    scheduler_flow,
    settings,
    templates,
)
from scheduler import jobs  # noqa: E402
from utils import config  # noqa: E402

log = logging.getLogger(__name__)


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

    text = (msg.text or "").strip()
    if not text:
        return
    await drafts.new_draft_message(update, context, text)


async def ingest_photo(update, context) -> None:
    """Photo in: attach to an open edit flow or start a new photo draft."""
    from utils.ui import deny, is_admin

    if not is_admin(update):
        await deny(update, context)
        return
    await editor.handle_media(update, context)


async def on_startup(app: Application) -> None:
    """Seed default settings and repopulate the scheduler from the DB."""
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


def build_application() -> Application:
    token = os.getenv("BOT_TOKEN", config.BOT_TOKEN)
    if not token:
        raise SystemExit("BOT_TOKEN is not set — copy .env.example to .env and fill it in.")
    if not config.ADMIN_ID:
        raise SystemExit("ADMIN_ID is not set — copy .env.example to .env and fill it in.")

    app = Application.builder().token(token).build()

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

    # ---- plain messages: text/photo ingestion (draft + edit/custom-time flows) --
    app.add_handler(MessageHandler(filters.PHOTO, ingest_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ingest_text))

    # ---- inline keyboard callbacks -------------------------------------------
    app.add_handler(CallbackQueryHandler(drafts.draft_callback, pattern=r"^(d|tag|dup|f|st):"))
    app.add_handler(CallbackQueryHandler(scheduler_flow.sched_callback,
                                         pattern=r"^(cal|day|hr|mn|now|quick):"))
    app.add_handler(CallbackQueryHandler(queue.queue_callback, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(queue.queue_callback,
                                         pattern=r"^(del_confirm|del_undo|post_now):"))
    app.add_handler(CallbackQueryHandler(editor.editor_callback,
                                         pattern=r"^(ed_save|ed_cancel|custom_time):"))
    app.add_handler(CallbackQueryHandler(settings.settings_callback, pattern=r"^(set|tz):"))
    app.add_handler(CallbackQueryHandler(templates.template_callback, pattern=r"^tpl:"))

    app.post_init = on_startup
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
