"""Thin tweepy wrapper for the X API v2 (upload media, post tweet, fetch stats)."""
from __future__ import annotations

import logging
import os

import tweepy

from utils import config

log = logging.getLogger(__name__)

_client: tweepy.Client | None = None


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


class PublishError(Exception):
    """Raised when a tweet cannot be published."""


def publish_tweet(text: str, media_path: str | None = None) -> str:
    """Post a tweet. Returns the tweet id. Raises PublishError on failure."""
    if not config.twitter_configured():
        raise PublishError("Twitter credentials are not configured in .env")
    if not text or not text.strip():
        raise PublishError("Empty tweet body")

    client = get_client()
    media_ids: list[int] = []
    if media_path and os.path.exists(media_path):
        try:
            from tweepy.media import MediaMedia  # noqa: F401  (v2 media object check)
        except Exception:
            pass
        try:
            uploaded = client.upload_media(media_path)
            media_ids = [uploaded.media_id_string]
        except Exception as exc:
            log.exception("media upload failed")
            raise PublishError(f"Media upload failed: {exc}") from exc

    try:
        resp = client.create_tweet(text=text, media_ids=media_ids or None)
        tweet_id = resp.data["id"]
        log.info("published tweet %s", tweet_id)
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
