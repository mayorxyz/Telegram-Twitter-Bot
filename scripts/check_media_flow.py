"""Smoke check for the media pipeline (dev tool: temp DB + temp media dir, no credentials).

Run from the repo root:  python scripts/check_media_flow.py

Covers the five media-related fixes end to end without Telegram or the X API:

  1. the additive `posts.media_ids` migration on a legacy database
  2. media CRUD (attach / cap at 4 / detach one / detach all / delete cleanup)
  3. caption-less media posts render "📎 Media post" instead of a blank body
  4. album rules (images accumulate, video/gif never mixes, 4-image cap)
  5. preview rendering (single file vs album + card) and publish passing every
     attached file to the X client
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Redirected streams on Windows fall back to cp1252; keep the checks printable
# when their output is piped or written to a file.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

# Isolated environment: never touch data/bot.db or data/media.
_TMP = tempfile.mkdtemp(prefix="media_flow_")
os.environ["DB_PATH"] = os.path.join(_TMP, "check.db")
os.environ["MEDIA_DIR"] = os.path.join(_TMP, "media")
os.environ["DRY_RUN"] = "1"
os.environ["TIMEZONE"] = "UTC"
os.environ.setdefault("BOT_TOKEN", "0:check")
os.environ.setdefault("ADMIN_ID", "1")

logging.basicConfig(level=logging.WARNING, format="%(levelname)-8s %(name)s: %(message)s")

from sqlalchemy import inspect, text  # noqa: E402

from db import crud  # noqa: E402
from db.database import engine, init_db  # noqa: E402
from handlers import threads  # noqa: E402
from scheduler import publisher  # noqa: E402
from utils import config  # noqa: E402
from utils.formatting import display_content, format_preview  # noqa: E402
from utils.ui import show_preview, update_preview  # noqa: E402
from web import server as dashboard  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"    {'ok  ' if ok else 'FAIL'} {label}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(f"{label}{f' ({detail})' if detail else ''}")


def make_file(name: str) -> str:
    path = os.path.join(config.MEDIA_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0check")
    return path


class Msg:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    """Records the Telegram calls the preview layer makes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def _record(self, kind: str, kw: dict) -> Msg:
        self.calls.append((kind, kw))
        return Msg(99)

    async def send_photo(self, **kw):
        return self._record("photo", kw)

    async def send_video(self, **kw):
        return self._record("video", kw)

    async def send_animation(self, **kw):
        return self._record("animation", kw)

    async def send_document(self, **kw):
        return self._record("document", kw)

    async def send_media_group(self, **kw):
        self.calls.append(("album", kw))
        return [Msg(i) for i in range(len(kw.get("media") or []))]

    async def send_message(self, **kw):
        return self._record("text", kw)

    async def edit_message_text(self, **kw):
        return self._record("edit_text", kw)

    async def edit_message_media(self, **kw):
        return self._record("edit_media", kw)

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


class FakeContext:
    def __init__(self, bot):
        self.bot = bot
        self.user_data: dict = {}


def main() -> int:
    from types import SimpleNamespace

    print(f"temp workspace: {_TMP}")

    # 1. A legacy database (created before media_ids existed) is migrated in place.
    print("[1] additive migration for posts.media_ids")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE posts (id INTEGER PRIMARY KEY, content TEXT)"))
        conn.execute(text("INSERT INTO posts (id, content) VALUES (1, 'legacy row')"))
    init_db()
    cols = {c["name"] for c in inspect(engine).get_columns("posts")}
    check("media_ids added by init_db()", "media_ids" in cols, f"columns={sorted(cols)}")
    with engine.begin() as conn:
        kept = conn.execute(text("SELECT content FROM posts WHERE id = 1")).scalar()
    check("existing rows survive the migration", kept == "legacy row", f"got={kept!r}")
    init_db()
    check("running init_db() twice is safe", True)
    with engine.begin() as conn:      # rebuild the full schema for the checks below
        conn.execute(text("DROP TABLE posts"))
    init_db()

    photo_a = make_file("a.jpg")
    photo_b = make_file("b.jpg")
    photo_c = make_file("c.jpg")
    photo_d = make_file("d.jpg")
    photo_e = make_file("e.jpg")
    video_a = make_file("v.mp4")

    # 2. Media CRUD: attach, dedupe, cap at 4 images, detach one/all, delete cleanup.
    print("[2] media CRUD on a single-tweet post")
    post = crud.create_post(content="", media_ids=[photo_a, photo_b])
    check("create_post stores media_ids", post.media == [photo_a, photo_b], f"{post.media}")
    post = crud.add_post_media(post.id, [photo_a, photo_b, photo_c, photo_d, photo_e])
    check("add_post_media dedupes and caps at 4",
          post.media == [photo_a, photo_b, photo_c, photo_d], f"{post.media}")
    post = crud.remove_post_media(post.id, photo_a)
    check("remove_post_media detaches one file",
          post.media == [photo_b, photo_c, photo_d], f"{post.media}")
    check("detached file is deleted from disk", not os.path.exists(photo_a))
    post = crud.remove_post_media(post.id)
    check("remove_post_media() clears the rest", post.media == [], f"{post.media}")

    legacy_file = make_file("legacy.jpg")
    legacy = crud.create_post(content="old row", media_path=legacy_file)
    check("legacy media_path is the fallback", legacy.media == [legacy_file], f"{legacy.media}")
    legacy = crud.add_post_media(legacy.id, [legacy_file])
    check("legacy slot folded into media_ids",
          legacy.media == [legacy_file] and legacy.media_path is None,
          f"media={legacy.media} media_path={legacy.media_path!r}")

    doomed = make_file("doomed.jpg")
    trash = crud.create_post(content="bye", media_ids=[doomed])
    crud.delete_post(trash.id)
    check("delete_post removes the media file too",
          not os.path.exists(doomed) and crud.get_post(trash.id) is None)

    # 3. Caption-less media posts display a placeholder instead of a blank body.
    print("[3] caption-less media display text")
    check("no text + no media stays empty", display_content("", 0) == "")
    check("no text + media → placeholder", display_content("", 2) == "📎 Media post",
          repr(display_content("", 2)))
    check("real captions pass through", display_content("hello", 2) == "hello")
    card = crud.create_post(content="", media_ids=[photo_b, photo_c])
    preview = format_preview(card)
    check("preview shows the placeholder", "📎 Media post" in preview)
    check("preview keeps the char count at 0", "0/280" in preview)
    check("preview lists the attachment count", "2 media files attached" in preview)

    # 4. Album rules: images accumulate, video/gif never mixes, hard cap of 4.
    print("[4] album batching rules")
    check("photo detected as image", threads._kind_of(photo_a) == "image")
    check("mp4 detected as video", threads._kind_of(video_a) == "video")
    check("mime fallback works", threads._kind_of(None, "video/mp4") == "video")
    two_photos = crud.create_post(content="", media_ids=[photo_b, photo_c])
    check("2 photos + 1 photo can join", threads._can_join_album(two_photos, "image") is True)
    check("2 photos + a video cannot join", threads._can_join_album(two_photos, "video") is False)
    full = crud.create_post(content="", media_ids=[photo_a, photo_b, photo_c, photo_d])
    check("4 photos is the cap", threads._can_join_album(full, "image") is False)
    mixed = crud.create_post(content="", media_ids=[video_a])
    check("video posts never mix", threads._can_join_album(mixed, "image") is False)
    check("threads share the 4-media API limit", crud.MAX_MEDIA_PER_TWEET == 4)

    # 5. Preview rendering: single file with buttons, album + separate control card.
    print("[5] preview rendering (single file vs album)")
    solo = make_file("s5_solo.jpg")
    alb = [make_file(f"s5_{n}.jpg") for n in ("a", "b", "c")]
    clip = make_file("s5_clip.mp4")
    single_ctx = FakeContext(FakeBot())
    single = crud.create_post(content="", media_ids=[solo])
    asyncio.run(show_preview(single_ctx, 1, single))
    check("single photo → send_photo with buttons",
          single_ctx.bot.kinds() == ["photo"]
          and single_ctx.bot.calls[0][1].get("reply_markup") is not None,
          f"calls={single_ctx.bot.kinds()}")
    check("preview message id persisted", single.tg_message_id == 99,
          f"tg_message_id={single.tg_message_id}")
    before = len(single_ctx.bot.calls)
    asyncio.run(update_preview(single_ctx, 1, crud.get_post(single.id)))
    check("single-photo cards still edit their media",
          single_ctx.bot.kinds()[before:] == ["edit_media"],
          f"calls={single_ctx.bot.kinds()[before:]}")

    album_ctx = FakeContext(FakeBot())
    album = crud.create_post(content="", media_ids=alb)
    asyncio.run(show_preview(album_ctx, 1, album))
    kinds = album_ctx.bot.kinds()
    sent_media = album_ctx.bot.calls[0][1].get("media") if album_ctx.bot.calls else []
    check("album → send_media_group + text card", kinds == ["album", "text"], f"calls={kinds}")
    check("album carries all 3 files", len(sent_media) == 3, f"attached={len(sent_media)}")
    check("card keeps the buttons",
          album_ctx.bot.calls[-1][1].get("reply_markup") is not None)
    check("card body is not blank",
          "📎 Media post" in (album_ctx.bot.calls[-1][1].get("text") or ""))
    before = len(album_ctx.bot.calls)
    crud.update_post(album.id, content="now captioned")
    asyncio.run(update_preview(album_ctx, 1, crud.get_post(album.id)))
    check("album edits stay text edits", album_ctx.bot.kinds()[before:] == ["edit_text"],
          f"calls={album_ctx.bot.kinds()[before:]}")

    video_ctx = FakeContext(FakeBot())
    video_post = crud.create_post(content="clip", media_ids=[clip])
    asyncio.run(show_preview(video_ctx, 1, video_post))
    check("video → send_video", video_ctx.bot.kinds() == ["video"],
          f"calls={video_ctx.bot.kinds()}")

    # 6. Publishing: every attached file reaches the X client, empty text allowed.
    print("[6] publishing a multi-media post")
    captured: dict = {}

    def fake_publish(text_, media_paths=None, **kw):
        captured["text"] = text_
        captured["media"] = list(media_paths or [])
        return "999000111222333444"

    publisher.publish_tweet = fake_publish       # keeps this check offline
    batch = [make_file(f"s6_{n}.jpg") for n in ("a", "b", "c")]
    to_post = crud.create_post(content="", media_ids=batch)
    ok = asyncio.run(publisher.publish_post(SimpleNamespace(bot=FakeBot()), to_post.id,
                                            manual=True))
    check("publish_post reports success", ok is True)
    check("all 3 files handed to the X client",
          captured.get("media") == batch, f"got={captured.get('media')}")
    check("caption-less post publishes empty text", captured.get("text") == "",
          f"text={captured.get('text')!r}")
    fresh = crud.get_post(to_post.id)
    check("status + tweet id persisted",
          fresh.status == "published" and fresh.tweet_id == "999000111222333444",
          f"status={fresh.status} tweet_id={fresh.tweet_id}")

    # 7. Threads keep their per-tweet media and reply chaining.
    print("[7] publishing a thread with media")
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_publish_thread(text_, media_paths=None, reply_to=None, **kw):
        calls.append((text_, list(media_paths or []), reply_to))
        return f"id{len(calls)}"

    publisher.publish_tweet = fake_publish_thread
    t1 = make_file("s7_a.jpg")
    t2 = make_file("s7_b.jpg")
    t3 = make_file("s7_c.jpg")
    thread_post = crud.create_post(content="tweet one")
    crud.create_thread(thread_post.id, [
        {"content": "tweet one", "media_ids": [t1, t2]},
        {"content": "tweet two", "media_ids": [t3]},
    ])
    asyncio.run(publisher._publish_thread(SimpleNamespace(bot=FakeBot()),
                                          crud.get_post(thread_post.id)))
    check("tweet 1 got its 2 files", bool(calls) and calls[0][1] == [t1, t2],
          f"calls={calls}")
    check("tweet 2 got its 1 file", len(calls) > 1 and calls[1][1] == [t3],
          f"calls={calls}")
    check("tweet 2 replies to tweet 1", len(calls) > 1 and calls[1][2] == "id1",
          f"calls={calls}")

    # 8. Dashboard payload: bare file names + display text, no local paths.
    print("[8] dashboard payload")
    dash_post = crud.create_post(content="", media_ids=[make_file("s8_a.jpg"),
                                                        make_file("s8_b.jpg")])
    payload = dashboard._post_to_dict(crud.get_post(dash_post.id))
    check("media emitted as bare file names",
          payload["media_ids"] == ["s8_a.jpg", "s8_b.jpg"], f"{payload['media_ids']}")
    check("no absolute path leaks to the browser", payload["media_path"] is None,
          f"{payload['media_path']!r}")
    check("display_content placeholder present",
          payload["display_content"] == "📎 Media post", repr(payload["display_content"]))
    old_row = crud.create_post(content="legacy row", media_path=make_file("s8_legacy.jpg"))
    old_payload = dashboard._post_to_dict(crud.get_post(old_row.id))
    check("legacy media_path folded into the payload list",
          old_payload["media_path"] is None
          and old_payload["media_ids"] == ["s8_legacy.jpg"],
          f"media_path={old_payload['media_path']!r} media_ids={old_payload['media_ids']}")

    return report()


def report() -> int:
    print("\nRESULT:", "ALL CHECKS PASSED" if not failures else f"FAILED -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

