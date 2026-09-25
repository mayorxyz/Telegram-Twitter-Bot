"""/report — weekly/monthly bot health digest computed purely from the local DB.

Free-tier safe: no X API calls whatsoever, only SQLite queries via db.crud.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import ContextTypes

from db import crud
from utils.timeutil import utc_now_aware

log = logging.getLogger(__name__)


def build_report() -> str:
    """Compose the HTML health report from DB state (no external API calls)."""
    now = utc_now_aware()
    published_7d = crud.count_published_since(now - timedelta(days=7))
    published_30d = crud.count_published_since(now - timedelta(days=30))
    scheduled = crud.count_posts("scheduled")
    drafts = crud.count_posts("draft")
    failed = crud.count_posts("failed")
    paused = crud.get_bool("paused", False)
    status = "Paused" if paused else "Active"
    dot = "🟡" if paused else "🟢"
    return (
        f"📊 <b>Bot Health Report</b>\n\n"
        f"{dot} <b>Status:</b> {status}\n"
        f"📅 <b>Last 7 Days:</b> {published_7d} published\n"
        f"🗓 <b>Last 30 Days:</b> {published_30d} published\n\n"
        f"⏳ <b>In Queue:</b> {scheduled} scheduled\n"
        f"📝 <b>Drafts:</b> {drafts} open\n"
        f"❌ <b>Failed:</b> {failed} failed"
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report — send the health digest to the admin chat."""
    from utils.ui import deny, is_admin

    if not is_admin(update):
        await deny(update, context)
        return
    await update.message.reply_text(build_report(), parse_mode="HTML")
