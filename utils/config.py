"""Environment configuration (loaded from .env)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
ADMIN_ID: int = int(os.environ.get("ADMIN_ID", "0"))

TWITTER_API_KEY: str = os.environ.get("TWITTER_API_KEY", "")
TWITTER_API_SECRET: str = os.environ.get("TWITTER_API_SECRET", "")
TWITTER_ACCESS_TOKEN: str = os.environ.get("TWITTER_ACCESS_TOKEN", "")
TWITTER_ACCESS_SECRET: str = os.environ.get("TWITTER_ACCESS_SECRET", "")
TWITTER_BEARER_TOKEN: str = os.environ.get("TWITTER_BEARER_TOKEN", "")

RSS_URL: str = os.environ.get("RSS_URL", "")
RSS_MAX_PER_DAY: int = int(os.environ.get("RSS_MAX_PER_DAY", "5"))
RSS_POLL_MINUTES: int = int(os.environ.get("RSS_POLL_MINUTES", "30"))

TIMEZONE: str = os.environ.get("TIMEZONE", "UTC")

MEDIA_DIR: str = os.path.abspath(
    os.environ.get("MEDIA_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "media"))
)
os.makedirs(MEDIA_DIR, exist_ok=True)

X_CHAR_LIMIT: int = 280


def twitter_configured() -> bool:
    return all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET])
