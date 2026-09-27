"""Environment configuration (loaded from .env).

Credential names: ``.env``/README document the ``X_*`` spelling, so those win;
the legacy ``TWITTER_*`` aliases are still honoured as a fallback so existing
deployments keep working (see :func:`_env`).
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


def _env(*names: str) -> str:
    """First non-empty value among ``names`` (whitespace-only counts as empty)."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _env_int(name: str, default: int) -> int:
    """Parse an int env var, falling back to ``default`` instead of crashing.

    A typo (``DASHBOARD_PORT=30o0``) must not kill startup with a ValueError
    traceback, so the bad value is logged and the default is used.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("config: %s=%r is not an integer — using %d", name, raw, default)
        return default


BOT_TOKEN: str = _env("BOT_TOKEN")
ADMIN_ID: int = _env_int("ADMIN_ID", 0)

# X/Twitter credentials: X_* (documented) first, legacy TWITTER_* as fallback.
TWITTER_API_KEY: str = _env("X_API_KEY", "TWITTER_API_KEY")
TWITTER_API_SECRET: str = _env("X_API_SECRET", "TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN: str = _env("X_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET: str = _env("X_ACCESS_TOKEN_SECRET", "TWITTER_ACCESS_SECRET")
TWITTER_BEARER_TOKEN: str = _env("X_BEARER_TOKEN", "TWITTER_BEARER_TOKEN")
DRY_RUN: bool = os.environ.get("DRY_RUN", "0").lower() in {"1", "true", "yes"}

RSS_URL: str = os.environ.get("RSS_URL", "")
RSS_MAX_PER_DAY: int = _env_int("RSS_MAX_PER_DAY", 5)
RSS_POLL_MINUTES: int = _env_int("RSS_POLL_MINUTES", 30)

# Local read-only dashboard (web/server.py reads these, so there is one source
# of truth for host/port).
DASHBOARD_HOST: str = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
DASHBOARD_PORT: int = _env_int("DASHBOARD_PORT", 3000)

# Polling-conflict policy (see README §12.1): consecutive 409s tolerated before
# main.py stops the bot cleanly. 0 = warn forever, never stop.
CONFLICT_ABORT_AFTER: int = _env_int("CONFLICT_ABORT_AFTER", 10)

TIMEZONE: str = os.environ.get("TIMEZONE", "UTC")

MEDIA_DIR: str = os.path.abspath(
    os.environ.get("MEDIA_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "media"))
)
os.makedirs(MEDIA_DIR, exist_ok=True)

X_CHAR_LIMIT: int = 280


def twitter_configured() -> bool:
    return all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET])
