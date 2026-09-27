"""Thin tweepy wrapper for the X API v2 (upload media, post tweets/threads, fetch stats).

Free-tier only: v2 create_tweet + v1.1 chunked media upload via tweepy.API.
No premium endpoints (no metrics/search beyond what free tier allows).
"""
from __future__ import annotations

import logging
import os
import time

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


_MEDIA_KINDS = {
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".webp": "image", ".gif": "gif",
    ".mp4": "video", ".mov": "video", ".webm": "video",
}

# Twitter requires media_category for async uploads
_MEDIA_CATEGORY = {
    "image": "tweet_image",
    "gif":   "tweet_gif",
    "video": "tweet_video",
}


def media_kind(path: str) -> str:
    """'image' | 'gif' | 'video' | 'unknown' for a local media file path."""
    return _MEDIA_KINDS.get(os.path.splitext(path)[1].lower(), "unknown")


def _info_get(info, key: str, default=None):
    """Read a PROCESSING_INFO field; tweepy hands it over as a plain dict."""
    if info is None:
        return default
    if isinstance(info, dict):
        return info.get(key, default)
    return getattr(info, key, default)


def _await_processing(api: tweepy.API, media_id: str, timeout: int = 180) -> None:
    """Poll PROCESSING_INFO until terminal state or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = api.get_media_upload_status(media_id)
        except tweepy.TweepyException as e:
            log.warning("Error checking media status: %s", e)
            time.sleep(5)
            continue
            
        info = getattr(status, "processing_info", None)
        if info is None:
            return  # no async processing needed
        state = _info_get(info, "state")
        if state in (None, "succeeded"):
            return
        if state == "failed":
            err = _info_get(info, "error", {}) or {}
            raise PublishError(f"Media processing failed: {_info_get(err, 'message', state)}")
        wait = int(_info_get(info, "check_after_secs", 5) or 5)
        log.debug("media %s processing… state=%s, retry in %ss", media_id, state, wait)
        time.sleep(wait)
    raise PublishError(f"Media processing timed out after {timeout}s")


def upload_media(media_path: str) -> str:
    """Upload image/gif/video; return media_id_string. Free-tier safe."""
    if config.DRY_RUN:
        return "dryrun-media-0000"
    if not config.twitter_configured():
        raise PublishError("Twitter credentials are not configured in .env")
    if not media_path or not os.path.exists(media_path):
        raise PublishError(f"Media file missing: {media_path}")

    kind = media_kind(media_path)
    if kind == "unknown":
        raise PublishError(f"Unsupported media type: {media_path}")

    category = _MEDIA_CATEGORY[kind]

    try:
        api = get_api()
        # tweepy's media_upload drives INIT/APPEND/FINALIZE for us. Only the
        # chunked path (video/gif) understands wait_for_async_finalize — handing
        # that kwarg to a simple image upload raises TypeError.
        upload_kwargs = {
            "filename": media_path,
            "media_category": category,
            "chunked": kind in ("video", "gif"),
        }
        if kind in ("video", "gif"):
            upload_kwargs["wait_for_async_finalize"] = False  # we poll manually
        uploaded = api.media_upload(**upload_kwargs)
        
        if kind in ("video", "gif"):
            _await_processing(api, uploaded.media_id_string)

        log.info("uploaded %s media_id=%s", kind, uploaded.media_id_string)
        return uploaded.media_id_string

    except tweepy.TweepyException as exc:
        log.exception("media upload failed for %s", media_path)
        raise PublishError(f"Media upload failed: {exc}") from exc
    except PublishError:
        raise
    except Exception as exc:
        log.exception("media upload failed for %s", media_path)
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
        log.info("[DRY RUN] would publish: %s%s", text,
         f" + {len(media_paths)} media file(s)" if media_paths else "")
        return f"dryrun-{random.randint(1000,9999)}"
    if not config.twitter_configured():
        raise PublishError("Twitter credentials are not configured in .env")
    if done_ids is not None and position is not None and position in done_ids:
        return done_ids[position]
        
    # X API v2 rejects empty strings. If we only have media, text must be None.
    clean_text = text.strip() if text else None
    if not clean_text and not media_paths:
        raise PublishError("Empty tweet body")

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
        resp = client.create_tweet(
            text=clean_text,
            media_ids=media_ids or None,
            quote_tweet_id=None,
            in_reply_to_tweet_id=reply_to  # FIXED: was reply_to=reply_to
        )
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