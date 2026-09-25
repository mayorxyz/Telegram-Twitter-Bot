"""Publishing pipeline: takes a Post row, pushes it to X, updates DB + notifies admin."""
from __future__ import annotations

import logging
from datetime import timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from db import crud
from utils import config
from utils.formatting import escape_html, strip_markdown
from utils.timeutil import to_admin_tz
from utils.twitter import PublishError, publish_tweet

log = logging.getLogger(__name__)


def final_text_for(post) -> str:
    """Apply auto-format at publish time only; draft content stays raw."""
    text = post.content or ""
    if post.auto_format:
        text = strip_markdown(text)
    return text.strip()


async def publish_post(application: Application, post_id: int, manual: bool = False) -> bool:
    """Publish one post by id. Returns True on success. Never raises."""
    post = crud.get_post(post_id)
    if post is None:
        log.warning("publish: post %s vanished", post_id)
        return False
    if not manual and crud.get_bool("paused", False):
        log.info("publish skipped (paused): post %s", post_id)
        return False

    text = final_text_for(post)
    try:
        tweet_id = publish_tweet(text, post.media_path)
    except PublishError as exc:
        crud.update_post(post_id, status="failed", error=str(exc))
        await _notify_failure(application, post_id, str(exc))
        return False
    except Exception as exc:  # network / unexpected
        log.exception("unexpected publish error")
        crud.update_post(post_id, status="failed", error=f"Unexpected: {exc}")
        await _notify_failure(application, post_id, str(exc))
        return False

    from datetime import datetime
    crud.update_post(post_id, status="published", published_at=datetime.now(timezone.utc),
                     tweet_id=tweet_id, error=None)
    post = crud.get_post(post_id)
    await _notify_success(application, post)
    return True


async def _notify_success(application: Application, post) -> None:
    chat_id = config.ADMIN_ID
    loc = to_admin_tz(post.published_at.replace(tzinfo=timezone.utc)) if post.published_at else ""
    html = (f"✅ <b>Published!</b> Post #{post.id} → "
            f"<a href='https://x.com/i/web/status/{post.tweet_id}'>tweet {post.tweet_id}</a>"
            f"{f' at {loc:%Y-%m-%d %H:%M}' if loc else ''}")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Duplicate", callback_data=f"d:{post.id}:dup"),
        InlineKeyboardButton("📊 Stats", callback_data=f"st:{post.id}"),
    ]])
    try:
        if post.tg_message_id:
            await application.bot.edit_message_text(chat_id=chat_id, message_id=post.tg_message_id,
                                                    text=html, reply_markup=kb, parse_mode="HTML")
        else:
            await application.bot.send_message(chat_id=chat_id, text=html,
                                               reply_markup=kb, parse_mode="HTML")
    except Exception:
        log.exception("success notify failed")


async def _notify_failure(application: Application, post_id: int, error: str) -> None:
    post = crud.get_post(post_id)
    if post is None:
        return
    html = (f"❌ <b>Publish failed</b> for post #{post_id}\n"
            f"<pre>{escape_html((post.content or '')[:200])}</pre>\n"
            f"⚠️ <code>{escape_html(error[:300])}</code>")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Retry", callback_data=f"f:{post_id}:retry"),
        InlineKeyboardButton("✏️ Edit & Retry", callback_data=f"f:{post_id}:editretry"),
        InlineKeyboardButton("🗑 Discard", callback_data=f"f:{post_id}:discard"),
    ]])
    try:
        if post.tg_message_id:
            await application.bot.edit_message_text(chat_id=config.ADMIN_ID,
                                                    message_id=post.tg_message_id,
                                                    text=html, reply_markup=kb, parse_mode="HTML")
        else:
            await application.bot.send_message(chat_id=config.ADMIN_ID, text=html,
                                               reply_markup=kb, parse_mode="HTML")
    except Exception:
        log.exception("failure notify failed")
