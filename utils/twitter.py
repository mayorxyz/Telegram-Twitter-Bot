"""Thin tweepy wrapper for the X API v2 (upload media, post tweets/threads, fetch stats).

Free-tier only: v2 create_tweet + v1.1 chunked media upload via tweepy.API.
No premium endpoints (no metrics/search beyond what free tier allows).
"""
from __future__ import annotations

import logging
import os

import tweepy

from utils import config

log = logging.getLogger(__name__)

_client: tweepy.Client | None = None
_api: tweepy.API | None = None


def get_client() -> tweepy.Client:
    global _client
    if _client is None:
        _client = tweepy.Client(
            consumer_key=config.TWITTER_API_KEY,
            consumer_secret=config.TWITTER_API_SECRET,
            access_token=config.TWITTER_ACCESS_TOKEN,
            access_token_secret=config.TWITTER_ACCESS_SECRET,
            wait_on_rate_limit=True,
        )
    return _client


def get_api() -> tweepy.API:
    """v1.1 API — used only for media uploads (still available on Free tier)."""
    global _api
    if _api is None:
        auth = tweepy.OAuth1UserHandler(
            config.TWITTER_API_KEY, config.TWITTER_API_SECRET,
            config.TWITTER_ACCESS_TOKEN, config.TWITTER_ACCESS_SECRET,
        )
        _api = tweepy.API(auth, wait_on_rate_limit=True)
    return _api


class PublishError(Exception):
    """Raised when a tweet cannot be published."""


_MEDIA_KINDS = {".jpg": "image", ".jpeg": "image", ".png": "image",
                ".webp": "image", ".gif": "image"}


def _media_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _MEDIA_KINDS:
        return "image"
    if ext in {".mp4", ".mov"}:
        return "video"
    return "unknown"


_MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif",
                ".mp4": "video/mp4", ".mov": "video/quicktime"}


def _guess_mime(path: str) -> str:
    """MIME type from file extension (used for video chunked uploads)."""
    return _MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "application/octet-stream")

def upload_media(media_path: str) -> str:
    """Upload one image/video and return its media_id_string (Free-tier safe)."""
    if config.DRY_RUN:
        return "dryrun-media-0000"
    if not config.twitter_configured():
        raise PublishError("Twitter credentials are not configured in .env")
    if not media_path or not os.path.exists(media_path):
        raise PublishError(f"Media file missing: {media_path}")
    try:
        kind = _media_kind(media_path)
        mime = _guess_mime(media_path)
        if kind == "video":
            uploaded = get_api().create_media_upload(media_path, media_type=mime)
        else:
            uploaded = get_api().media_upload(media_path)
        return uploaded.media_id_string
    except Exception as exc:
        log.exception("media upload failed for %s", media_path)
        if isinstance(exc, tweepy.TweepyException):
            raise
        raise PublishError(f"Media upload failed: {exc}") from exc


def publish_tweet(text: str, media_paths: list[str] | None = None,
                  reply_to: str | None = None,
                  done_ids: dict[int, str] | None = None,
                  uploaded_map: dict[str, str] | None = None,
                  position: int | None = None) -> str:
    """Post a single tweet with 0..N media files. Returns the tweet id.

    - `media_paths`: local file paths; each is uploaded (or reused from
      `uploaded_map`) and attached to THIS tweet only — selective attachment.
    - `reply_to`: tweet id this one replies to (thread chaining).
    - Raises immediately if `done_ids` already contains `position` so callers
      can't double-post a completed thread step.
    """
    if config.DRY_RUN:
        import random
        log.info("[DRY RUN] would publish: %s", text)
        return f"dryrun-{random.randint(1000,9999)}"
    if not config.twitter_configured():
        raise PublishError("Twitter credentials are not configured in .env")
    if done_ids is not None and position is not None and position in done_ids:
        return done_ids[position]
    if not text or not text.strip():
        if not media_paths:
            raise PublishError("Empty tweet body")
        text = ""

    client = get_client()
    media_ids: list[str] = []
    for path in (media_paths or []):
        if uploaded_map and path in uploaded_map:
            media_ids.append(uploaded_map[path])
            continue
        mid = upload_media(path)
        if uploaded_map is not None:
            uploaded_map[path] = mid
        media_ids.append(mid)

    try:
        resp = client.create_tweet(text=text or None,
                                   media_ids=media_ids or None,
                                   quote_tweet_id=None,
                                   reply_to=reply_to)
        tweet_id = resp.data["id"]
        log.info("published tweet %s%s", tweet_id,
                 f" (reply_to={reply_to})" if reply_to else "")
        return str(tweet_id)
    except tweepy.TweepyException as exc:
        log.exception("create_tweet failed")
        raise PublishError(str(exc)) from exc


def get_tweet_stats(tweet_id: str) -> dict | None:
    """Public metrics for a tweet via the bearer token. Returns None if unavailable."""
    if not tweet_id:
        return None
    try:
        read_only = tweepy.Client(bearer_token=config.TWITTER_BEARER_TOKEN) \
            if config.TWITTER_BEARER_TOKEN else get_client()
        resp = read_only.get_tweet(
            int(tweet_id),
            tweet_fields=["public_metrics", "created_at"],
        )
        if resp.data is None:
            return None
        m = resp.data.public_metrics or {}
        return {
            "impressions": m.get("impression_count"),
            "likes": m.get("like_count"),
            "retweets": m.get("retweet_count"),
            "replies": m.get("reply_count"),
        }
    except Exception:
        log.exception("get_tweet_stats failed")
        return None
