"""SQLAlchemy ORM models for the X-posting bot."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(Text, default="")
    media_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # draft / scheduled / published / failed
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC naive
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC naive
    tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    auto_format: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # telegram message id of the preview card (for edit_message_* updates)
    tg_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # tweet id after successful publish (used by /stats)
    tweet_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # extra error info when status == failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # one-to-one link to a thread body (present only for multi-tweet posts)
    thread: Mapped["Thread | None"] = relationship(
        back_populates="post", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Post {self.id} [{self.status}] {(self.content or '')[:30]!r}>"


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Setting {self.key}={self.value!r}>"


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Template {self.id} {self.name!r}>"


class Thread(Base):
    """A multi-tweet thread attached 1:1 to a Post row (post.status drives lifecycle)."""

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # relationship to tweets
    post: Mapped["Post"] = relationship(back_populates="thread")
    tweets: Mapped[list["ThreadTweet"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan",
        order_by="ThreadTweet.position", lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Thread {self.id} post={self.post_id} tweets={len(self.tweets)}>"


class ThreadTweet(Base):
    """One tweet inside a thread. media_ids is a JSON list of local file paths."""

    __tablename__ = "thread_tweets"
    __table_args__ = (UniqueConstraint("thread_id", "position", name="uq_thread_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-indexed in thread
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # list[str] of paths
    # X media id strings after upload (kept so retries don't re-upload)
    uploaded_media_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # resulting tweet id once this tweet of the thread has been posted
    tweet_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # relationships
    thread: Mapped["Thread"] = relationship(back_populates="tweets")

    @property
    def media(self) -> list[str]:
        return list(self.media_ids or [])

    def __repr__(self) -> str:  # pragma: no cover
        n = len(self.media_ids or [])
        return f"<ThreadTweet t{self.thread_id}[{self.position}] ({n} media) {(self.content or '')[:24]!r}>"
