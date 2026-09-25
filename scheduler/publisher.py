"""Publishing pipeline: takes a Post row, pushes it to X, updates DB + notifies admin."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import tweepy
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from db import crud
from utils import config
from utils.formatting import escape_html, strip_markdown
from utils.timeutil import to_admin_tz
from utils.twitter import PublishError, publish_tweet

log = logging.getLogger(__name__)

# media paths that failed upload during the last attempt (for "skip & retry")
_last_upload_failures: dict[int, list[str]] = {}


def _is_rate_limit(exc: BaseException) -> bool:
    """True for tweepy 429 responses (TooManyRequests or HTTPException with status 429)."""
    if isinstance(exc, tweepy.errors.TooManyRequests):
        return True
    if isinstance(exc, tweepy.errors.HTTPException):
        return getattr(exc, "status_code", None) == 429
    return False


def final_text_for(post) -> str:
    """Apply auto-format at publish time only; draft content stays raw."""
    text = post.content or ""
    if post.auto_format:
        text = strip_markdown(text)
    return text.strip()


def _final_thread_texts(post) -> list[str]:
    """Auto-formatted text for each tweet of a thread (raw rows stay untouched)."""
    out = []
    for tt in post.thread.tweets:
        t = tt.content or ""
        if post.auto_format:
            t = strip_markdown(t)
        out.append(t.strip())
    return out


async def publish_post(application: Application, post_id: int, manual: bool = False) -> bool:
    """Publish one post by id. Returns True on success. Never raises.

    Free-tier protection: a 429 from the X API does NOT mark the post failed —
    it is pushed back 15 minutes, the APScheduler job is re-armed, and the
    admin is notified.
    """
    post = crud.get_post(post_id)
    if post is None:
        log.warning("publish: post %s vanished", post_id)
        return False
    if not manual and crud.get_bool("paused", False):
        log.info("publish skipped (paused): post %s", post_id)
        return False

    try:
        if post.thread is not None:
            await _publish_thread(application, post)
        else:
            await _publish_single(application, post)
        return True
    except PublishError as exc:
        if _is_rate_limit(exc.__cause__ if exc.__cause__ else exc):
            await _handle_rate_limit(application, post_id)
            return False
        crud.update_post(post_id, status="failed", error=str(exc))
        await _notify_failure(application, post_id, str(exc))
        return False
    except Exception as exc:  # network / unexpected (incl. raw tweepy errors)
        if _is_rate_limit(exc):
            await _handle_rate_limit(application, post_id)
            return False
        log.exception("unexpected publish error")
        crud.update_post(post_id, status="failed", error=f"Unexpected: {exc}")
        await _notify_failure(application, post_id, str(exc))
        return False


async def _publish_single(application: Application, post) -> None:
    text = final_text_for(post)
    media_paths = [post.media_path] if post.media_path else None
    tweet_id = publish_tweet(text, media_paths)
    crud.update_post(post.id, status="published",
                     published_at=datetime.now(timezone.utc),
                     tweet_id=tweet_id, error=None)
    fresh = crud.get_post(post.id)
    await _notify_success(application, fresh)


async def _publish_thread(application: Application, post) -> None:
    """Post a thread tweet-by-tweet with selective media + resume-safe chaining.

    Each ThreadTweet row carries its own media_ids list; tweets reply_to the
    previous tweet's id. Already-posted positions are skipped (their ids are
    reused), so a retry after a mid-thread failure never double-posts.
    """
    tweets = sorted(post.thread.tweets, key=lambda t: t.position)
    if not tweets:
        raise PublishError("Thread has no tweets")
    texts = _final_thread_texts(post)

    done_ids: dict[int, str] = {tt.position: tt.tweet_id
                                for tt in tweets if tt.tweet_id}
    uploaded_map: dict[str, str] = {}
    for tt in tweets:
        for path, mid in zip(tt.media_ids or [], tt.uploaded_media_ids or []):
            uploaded_map[path] = mid

    prev_id: str | None = None
    first_id: str | None = next(iter(done_ids.values()), None)
    skipped_uploads: list[str] = []

    for idx, tt in enumerate(tweets):
        pos = tt.position
        if pos in done_ids:
            prev_id = done_ids[pos]
            first_id = first_id or prev_id
            continue

        paths = [p for p in (tt.media_ids or []) if os.path.isfile(p)]
        missing = len(tt.media_ids or []) - len(paths)
        if missing:
            skipped_uploads.extend([p for p in (tt.media_ids or []) if not os.path.isfile(p)])
        try:
            tid = publish_tweet(texts[idx], paths or None,
                                reply_to=prev_id,
                                done_ids=done_ids, uploaded_map=uploaded_map,
                                position=pos)
        except PublishError as exc:
            if "Media upload failed" in str(exc) and paths:
                # remember broken files; "Retry (skip bad media)" drops them
                _last_upload_failures[post.id] = [
                    p for p in (tt.media_ids or []) if p not in uploaded_map]
            raise

        done_ids[pos] = tid
        first_id = first_id or tid
        prev_id = tid
        crud.record_tweet_publish(post.id, pos, tid,
                                  uploaded_media_ids=[uploaded_map[p] for p in paths
                                                      if p in uploaded_map])

    _last_upload_failures.pop(post.id, None)
    crud.update_post(post.id, status="published",
                     published_at=datetime.now(timezone.utc),
                     tweet_id=first_id, error=None)
    fresh = crud.get_post(post.id)
    await _notify_success(application, fresh,
                          extra=(f"\n🧵 <b>Thread: {len(tweets)} tweets</b>"
                                 + (f" · ⚠️ {len(skipped_uploads)} missing media file(s) skipped"
                                    if skipped_uploads else "")))


async def skip_bad_media_and_retry(application: Application, post_id: int) -> bool:
    """Drop media files whose upload failed, then retry publishing."""
    bad = _last_upload_failures.pop(post_id, [])
    th = crud.get_thread(post_id)
    if th is not None and bad:
        with crud.get_session() as s:
            from db.models import ThreadTweet

            for tt in s.scalars(select(ThreadTweet).where(
                    ThreadTweet.thread_id == th.id)).all():
                cur = list(tt.media_ids or [])
                new = [p for p in cur if p not in bad]
                if new != cur:
                    tt.media_ids = new or None
                    tt.uploaded_media_ids = None
                    tt.tweet_id = None  # this tweet must be re-posted without the media
            s.commit()
    return await publish_post(application, post_id, manual=True)


async def _handle_rate_limit(application: Application, post_id: int) -> None:
    """Smart backoff: reschedule +15 min, re-arm the job, notify the admin."""
    from scheduler.jobs import schedule_post_job

    new_time = datetime.utcnow() + timedelta(minutes=15)
    crud.update_post(post_id, status="scheduled", scheduled_at=new_time,
                     error="Rate limited, auto-rescheduled")
    schedule_post_job(post_id, new_time.replace(tzinfo=timezone.utc))
    log.warning("rate limited on post %s — pushed to %s UTC", post_id, new_time)
    try:
        await application.bot.send_message(
            chat_id=config.ADMIN_ID,
            text=(f"⚠️ <b>X API Rate Limit Hit</b>\n\n"
                  f"Post #{post_id} was auto-rescheduled for "
                  f"<code>{new_time.strftime('%Y-%m-%d %H:%M UTC')}</code>.\n"
                  f"This protects your Free Tier daily quota."),
            parse_mode="HTML")
    except Exception:
        log.exception("rate-limit notification failed")


async def _notify_success(application: Application, post, extra: str = "") -> None:
    chat_id = config.ADMIN_ID
    loc = to_admin_tz(post.published_at.replace(tzinfo=timezone.utc)) if post.published_at else ""
    html = (f"✅ <b>Published!</b> Post #{post.id} → "
            f"<a href='https://x.com/i/web/status/{post.tweet_id}'>tweet {post.tweet_id}</a>"
            f"{f' at {loc:%Y-%m-%d %H:%M}' if loc else ''}{extra}")
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
    body = post.content or ""
    thread_note = ""
    if post.thread is not None:
        n = len(post.thread.tweets)
        done = sum(1 for t in post.thread.tweets if t.tweet_id)
        body = f"🧵 {n}-tweet thread — {done}/{n} tweets already posted (retry resumes)"
        thread_note = "\n"
    html = (f"❌ <b>Publish failed</b> for post #{post_id}\n"
            f"<pre>{escape_html(body[:200])}</pre>\n{thread_note}"
            f"⚠️ <code>{escape_html(error[:300])}</code>")
    rows = [[
        InlineKeyboardButton("🔁 Retry", callback_data=f"f:{post_id}:retry"),
        InlineKeyboardButton("✏️ Edit & Retry", callback_data=f"f:{post_id}:editretry"),
        InlineKeyboardButton("🗑 Discard", callback_data=f"f:{post_id}:discard"),
    ]]
    if post_id in _last_upload_failures:
        rows.insert(0, [InlineKeyboardButton("🚫 Retry (skip bad media)",
                                             callback_data=f"f:{post_id}:skipmedia")])
    kb = InlineKeyboardMarkup(rows)
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
