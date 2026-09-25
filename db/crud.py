"""CRUD helpers. All state lives in the DB so the bot survives restarts."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from db.database import get_session
from db.models import Post, Setting, utcnow


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
        s.delete(p)
        s.commit()
        return True


def duplicate_post(post_id: int) -> Post | None:
    src = get_post(post_id)
    if src is None:
        return None
    return create_post(content=src.content, media_path=src.media_path,
                       auto_format=src.auto_format, tag=src.tag, status="draft")


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
