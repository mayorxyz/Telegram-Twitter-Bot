"""APScheduler wiring: persistent job store, publish jobs, RSS poller, catch-up."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import crud
from scheduler.publisher import publish_post
from utils import config, rss
from utils.timeutil import utc_now_aware

log = logging.getLogger(__name__)

# Module-level guard so bootstrap() can never double-initialize the scheduler.
_bootstrapped = False

JOBS_DB = os.path.join(os.path.dirname(config.MEDIA_DIR), "jobs.db")
os.makedirs(os.path.dirname(JOBS_DB), exist_ok=True)

_jobstore = SQLAlchemyJobStore(url=f"sqlite:///{JOBS_DB}")
scheduler = AsyncIOScheduler(
    jobstores={"default": _jobstore},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60 * 60},
)


# ------------------------------------------------------------ job funcs ----

async def publish_job(post_id: int) -> None:
    """Fires at scheduled_at. Kill-switch aware: re-arms in 5 min when paused."""
    application = getattr(scheduler, "application", None)
    if application is None:  # set in bootstrap()
        log.error("scheduler.application not set")
        return
    post = crud.get_post(post_id)
    if post is None or post.status != "scheduled":
        return
    if crud.get_bool("paused", False):
        log.info("paused — re-arming post %s in 5 min", post_id)
        scheduler.add_job(publish_job, "date",
                          run_date=datetime.now(timezone.utc) + timedelta(minutes=5),
                          args=[post_id], id=_job_id(post_id), replace_existing=True)
        return
    await publish_post(application, post_id, manual=False)


async def rss_poll_job() -> None:
    """Poll RSS feed; push new drafts to admin chat."""
    from handlers.drafts import push_rss_draft  # local import avoids cycle

    if crud.get_bool("paused", False):
        return
    application = getattr(scheduler, "application", None)
    if application is None:
        return
    try:
        items = await _run_in_thread(rss.fetch_new_items)
    except Exception:
        log.exception("RSS poll failed")
        return
    for item in items:
        post = crud.create_post(content=item["content"], tag=item["tag"],
                                auto_format=crud.get_bool("auto_format", True))
        rss.bump_today_count()
        try:
            await push_rss_draft(application.bot, post)
        except Exception:
            log.exception("failed to push RSS draft %s", post.id)


async def _run_in_thread(fn):
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn)


def _job_id(post_id: int) -> str:
    return f"publish_{post_id}"


# ------------------------------------------------------------- scheduling --

def schedule_post_job(post_id: int, when_utc_aware: datetime) -> None:
    scheduler.add_job(publish_job, "date", run_date=when_utc_aware,
                      args=[post_id], id=_job_id(post_id), replace_existing=True)
    log.info("scheduled post %s for %s", post_id, when_utc_aware)


def unschedule_post_job(post_id: int) -> None:
    try:
        scheduler.remove_job(_job_id(post_id))
    except Exception:
        pass


def reschedule_post_job(post_id: int, when_utc_aware: datetime) -> None:
    schedule_post_job(post_id, when_utc_aware)


# --------------------------------------------------------------- bootstrap -

async def reset_rss_count() -> None:
    """Midnight job: zero the RSS daily-draft counter."""
    from db.crud import set_setting

    set_setting("rss_count_today", "0")


def bootstrap(application) -> AsyncIOScheduler:
    """Attach PTB app, restore jobs from DB, register recurring jobs, start scheduler.

    Idempotent: guarded by the module-level _bootstrapped flag so a second call
    (e.g. from both main.py and an errant post_init hook) can never double-start
    the scheduler or duplicate the recurring RSS/reset jobs. Per-post publish
    jobs remain safe to re-add because they use replace_existing=True with
    deterministic ids.
    """
    global _bootstrapped
    scheduler.application = application  # type: ignore[attr-defined]
    if _bootstrapped:
        log.info("bootstrap() called again — skipping (scheduler already initialized)")
        return scheduler
    _bootstrapped = True

    now = utc_now_aware()

    # Recurring jobs are registered BEFORE the per-post restore loop so they can
    # never be clobbered by a second bootstrap path. add_job(...,
    # replace_existing=True) keeps this idempotent even though the persistent
    # SQLAlchemyJobStore reloads these rows on every restart.
    interval = max(5, config.RSS_POLL_MINUTES)
    scheduler.add_job(rss_poll_job, "interval", minutes=interval,
                      id="rss_poll", replace_existing=True,
                      next_run_time=now)  # first poll right after boot

    scheduler.add_job(reset_rss_count, "cron", hour=0, minute=0,
                      id="rss_reset", replace_existing=True)

    # Re-arm any publish jobs that were still pending when we last shut down
    # (they live in the persistent job store). Doing this BEFORE scheduler.start()
    # prevents APScheduler's misfire handling from silently dropping due jobs.
    for job in list(scheduler.get_jobs()):
        if job.id.startswith("publish_") and job.next_run_time is None:
            try:
                job.modify(next_run_time=now)
                log.info("re-armed pending publish job %s", job.id)
            except Exception:
                log.exception("could not re-arm job %s", job.id)

    for post in crud.scheduled_posts():
        when = post.scheduled_at.replace(tzinfo=timezone.utc) if post.scheduled_at else None
        if when is None:
            continue
        if when <= now:
            # missed while offline -> fire ASAP (unless paused)
            scheduler.add_job(publish_job, "date", run_date=now, args=[post.id],
                              id=_job_id(post.id), replace_existing=True)
            log.info("catch-up job for post %s (%s)", post.id, when)
        else:
            schedule_post_job(post.id, when)

    if not scheduler.running:
        scheduler.start()
    log.info("scheduler started; %d scheduled posts restored", crud.count_posts("scheduled"))
    return scheduler
