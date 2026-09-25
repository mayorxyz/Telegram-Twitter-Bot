"""CRUD helpers. All state lives in the DB so the bot survives restarts."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import func, select

from db.database import get_session
from db.models import Post, Setting, Template, Thread, ThreadTweet, utcnow


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- posts -----

def create_post(content: str, media_path: str | None = None,
                auto_format: bool = True, tag: str | None = None,
                status: str = "draft") -> Post:
    with get_session() as s:
        p = Post(content=content, media_path=media_path, auto_format=auto_format,
                 tag=tag, status=status)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def get_post(post_id: int) -> Post | None:
    with get_session() as s:
        return s.get(Post, post_id)


def update_post(post_id: int, **fields) -> Post | None:
    """Update arbitrary columns on a post. Returns fresh object or None."""
    if "scheduled_at" in fields and fields["scheduled_at"] is not None:
        fields["scheduled_at"] = _to_naive_utc(fields["scheduled_at"])
    with get_session() as s:
        p = s.get(Post, post_id)
        if p is None:
            return None
        for k, v in fields.items():
            if hasattr(p, k):
                setattr(p, k, v)
        s.commit()
        s.refresh(p)
        return p


def delete_post(post_id: int) -> bool:
    with get_session() as s:
        p = s.get(Post, post_id)
        if p is None:
            return False
        paths = [p.media_path] if p.media_path else []
        for t in (p.thread.tweets if p.thread else []):
            paths.extend(t.media_ids or [])
        s.delete(p)  # cascades to thread + thread tweets
        s.commit()
    for path in paths:  # drop local media copies once the row is gone
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    return True


def duplicate_post(post_id: int) -> Post | None:
    src = get_post(post_id)
    if src is None:
        return None
    clone = create_post(content=src.content, media_path=src.media_path,
                        auto_format=src.auto_format, tag=src.tag, status="draft")
    if src.thread:  # threads are cloned tweet-by-tweet (media paths shared until save)
        create_thread(clone.id, [
            {"content": t.content, "media_ids": list(t.media_ids or [])}
            for t in src.thread.tweets
        ])
    return clone


def list_posts(statuses: tuple[str, ...] | None = None) -> list[Post]:
    stmt = select(Post).order_by(Post.id.desc())
    if statuses:
        stmt = stmt.where(Post.status.in_(statuses))
    with get_session() as s:
        return list(s.scalars(stmt))


def count_posts(*statuses: str) -> int:
    stmt = select(func.count(Post.id))
    if statuses:
        stmt = stmt.where(Post.status.in_(statuses))
    with get_session() as s:
        return s.scalar(stmt) or 0


def scheduled_posts() -> list[Post]:
    return list_posts(("scheduled",))


def count_published_since(when_utc: datetime) -> int:
    """Number of posts published at/after `when_utc` (naive or aware; stored naive UTC)."""
    when = _to_naive_utc(when_utc) if when_utc.tzinfo else when_utc
    stmt = select(func.count(Post.id)).where(Post.status == "published",
                                             Post.published_at >= when)
    with get_session() as s:
        return s.scalar(stmt) or 0


# --------------------------------------------------------------- threads ----

MAX_THREAD_TWEETS = 25          # X API hard limit per thread
MAX_MEDIA_PER_TWEET = 4         # images/videos attached to a single tweet


def get_thread(post_id: int) -> Thread | None:
    with get_session() as s:
        return s.scalar(select(Thread).where(Thread.post_id == post_id))


def create_thread(post_id: int, tweets: list[dict]) -> Thread | None:
    """Create a thread for a post. `tweets` = [{content, media_ids?}, ...] in order."""
    if not (1 <= len(tweets) <= MAX_THREAD_TWEETS):
        return None
    with get_session() as s:
        existing = s.scalar(select(Thread).where(Thread.post_id == post_id))
        if existing is not None:
            s.delete(existing)   # rebuild-in-place semantics
            s.flush()
        th = Thread(post_id=post_id)
        for pos, t in enumerate(tweets):
            content = str(t.get("content", "") or "")
            media = [str(m) for m in (t.get("media_ids") or [])][:MAX_MEDIA_PER_TWEET]
            th.tweets.append(ThreadTweet(position=pos, content=content,
                                         media_ids=media or None))
        s.add(th)
        s.commit()
        s.refresh(th)
        return th


def set_thread_content(post_id: int, tweets: list[dict]) -> Thread | None:
    """Replace all tweets of an existing thread (used by the builder save step)."""
    th = get_thread(post_id)
    if th is None:
        return create_thread(post_id, tweets)
    if not (1 <= len(tweets) <= MAX_THREAD_TWEETS):
        return None
    with get_session() as s:
        row = s.get(Thread, th.id)
        s.query(ThreadTweet).filter_by(thread_id=row.id).delete()
        row.tweets = []
        s.flush()
        for pos, t in enumerate(tweets):
            media = [str(m) for m in (t.get("media_ids") or [])][:MAX_MEDIA_PER_TWEET]
            row.tweets.append(ThreadTweet(position=pos,
                                          content=str(t.get("content", "") or ""),
                                          media_ids=media or None))
        s.commit()
        s.refresh(row)
        return row


def update_tweet_media(post_id: int, position: int, paths: list[str]) -> bool:
    """Attach/detach media on one tweet of a thread. Paths must already be on disk."""
    th = get_thread(post_id)
    if th is None:
        return False
    with get_session() as s:
        tt = s.scalar(select(ThreadTweet).where(ThreadTweet.thread_id == th.id,
                                                ThreadTweet.position == position))
        if tt is None:
            return False
        keep_existing = [m for m in (tt.media_ids or [])
                         if m in paths and os.path.isfile(m)]
        tt.media_ids = [p for p in dict.fromkeys(keep_existing + paths)][:MAX_MEDIA_PER_TWEET] \
            or None
        s.commit()
    return True


def remove_tweet_media(post_id: int, position: int, path: str) -> bool:
    """Remove one media file from a thread tweet; deletes the local copy."""
    th = get_thread(post_id)
    if th is None:
        return False
    removed = False
    with get_session() as s:
        tt = s.scalar(select(ThreadTweet).where(ThreadTweet.thread_id == th.id,
                                                ThreadTweet.position == position))
        if tt is None:
            return False
        current = list(tt.media_ids or [])
        if path in current:
            current.remove(path)
            tt.media_ids = current or None
            tt.uploaded_media_ids = None   # force re-upload mapping on next publish
            removed = True
            s.commit()
    if removed:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    return removed


def record_tweet_publish(post_id: int, position: int, tweet_id: str,
                         uploaded_media_ids: list[str] | None = None) -> None:
    """Persist per-tweet results so partial-thread retries skip finished tweets."""
    th = get_thread(post_id)
    if th is None:
        return
    with get_session() as s:
        tt = s.scalar(select(ThreadTweet).where(ThreadTweet.thread_id == th.id,
                                                ThreadTweet.position == position))
        if tt is None:
            return
        tt.tweet_id = str(tweet_id)
        if uploaded_media_ids is not None:
            tt.uploaded_media_ids = [str(m) for m in uploaded_media_ids] or None
        s.commit()


def reset_thread_tweet_state(post_id: int) -> None:
    """Clear per-tweet tweet ids (used when the whole thread body was edited)."""
    th = get_thread(post_id)
    if th is None:
        return
    with get_session() as s:
        rows = s.scalars(select(ThreadTweet).where(ThreadTweet.thread_id == th.id)).all()
        for tt in rows:
            tt.tweet_id = None
        s.commit()


# -------------------------------------------------------------- templates ---

def get_templates() -> list[Template]:
    stmt = select(Template).order_by(Template.name)
    with get_session() as s:
        return list(s.scalars(stmt))


def get_template(template_id: int) -> Template | None:
    with get_session() as s:
        return s.get(Template, template_id)


def create_template(name: str, content: str) -> Template | None:
    """Create a template. Returns None when name/content empty or name already exists."""
    name = (name or "").strip()
    content = (content or "").strip()
    if not name or not content:
        return None
    with get_session() as s:
        exists = s.scalar(select(Template.id).where(Template.name == name))
        if exists is not None:
            return None
        t = Template(name=name, content=content)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t


def delete_template(template_id: int) -> bool:
    with get_session() as s:
        t = s.get(Template, template_id)
        if t is None:
            return False
        s.delete(t)
        s.commit()
        return True


# ------------------------------------------------------------- settings -----

def get_setting(key: str, default: str = "") -> str:
    with get_session() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with get_session() as s:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
        s.commit()


def get_bool(key: str, default: bool = False) -> bool:
    return get_setting(key, str(default)).lower() == "true"


def set_bool(key: str, value: bool) -> None:
    set_setting(key, "true" if value else "false")


# ----------------------------------------------------------------- misc ----

def now_utc() -> datetime:
    return utcnow()
