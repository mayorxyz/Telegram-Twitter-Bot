"""Engine / session bootstrap (SQLite)."""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "bot.db"))
DB_PATH = os.path.abspath(DB_PATH)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def init_db() -> None:
    """Create tables if they don't exist."""
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()
