"""SQLAlchemy ORM models for the X-posting bot."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
