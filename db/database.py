"""Engine / session bootstrap (SQLite)."""
from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

log = logging.getLogger(__name__)

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


# Columns added after the first release. create_all() never touches a table that
# already exists and SQLite can only add columns one at a time, so keep this map
# in sync with db/models.py — it runs on every startup and is a no-op once applied.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "posts": {
        "media_ids": "JSON",
        "publish_mode": "TEXT DEFAULT 'api'",
    },
}


def _migrate() -> None:
    """Additive, idempotent ALTER TABLE migration for SQLite (no Alembic here)."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl_type in columns.items():
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
                log.info("db: migrated %s — added column %s (%s)", table, name, ddl_type)


def init_db() -> None:
    """Create tables if they don't exist, then apply additive migrations."""
    Base.metadata.create_all(engine)
    _migrate()


def get_session() -> Session:
    return SessionLocal()
