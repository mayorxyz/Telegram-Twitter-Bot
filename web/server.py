"""Read-only local dashboard: stdlib HTTP server exposing the posts table as JSON.

Solo-use helper. Binds localhost only and has no authentication — it renders the
same rows the Telegram admin already sees in chat (drafts, queue, failures), for
quick eyeballing in a browser while the bot is polling. It is started from
main.on_startup() on a daemon thread so it can never block the polling loop.

Routes
    GET /            -> web/static/index.html (the dashboard page)
    GET /index.html  -> same
    GET /favicon.ico -> 204 (nothing to serve)
    GET /api/posts   -> JSON array of every post, newest first
    OPTIONS *        -> CORS preflight (so file:// can hit the API too)

JSON contract (consumed by web/static/index.html):
    [{id, content, status, tag, scheduled_at, tweet_id, error, is_thread, tweets}]
    tweets = [{position, content, media_ids}]   # [] for non-thread posts
`scheduled_at` is naive-UTC in the DB, so it is serialized with a trailing "Z"
and the page converts it to the viewer's local time.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.database import DB_PATH, SessionLocal
from db.models import Post, Thread

log = logging.getLogger(__name__)

HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")

_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()


def _iso_utc(dt: datetime | None) -> str | None:
    """Naive-UTC DB datetime -> ISO-8601 string with an explicit Z suffix."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds") + "Z"


def _post_to_dict(post: Post) -> dict:
    """One Post row (plus its thread tweets, when present) as a JSON-safe dict."""
    thread = post.thread
    tweets = [] if thread is None else [
        {"position": t.position,
         "content": t.content or "",
         "media_ids": list(t.media_ids or [])}
        for t in thread.tweets
    ]
    return {
        "id": post.id,
        "content": post.content or "",
        "status": post.status or "",
        "tag": post.tag,
        "scheduled_at": _iso_utc(post.scheduled_at),
        "tweet_id": post.tweet_id,
        "error": post.error,
        "is_thread": thread is not None,
        "tweets": tweets,
    }


def fetch_posts() -> list[dict]:
    """Every post, newest first. Returns [] when the DB/table isn't there yet."""
    if not os.path.exists(DB_PATH):
        return []  # before the first init_db()
    stmt = (
        select(Post)
        .options(selectinload(Post.thread).selectinload(Thread.tweets))
        .order_by(Post.id.desc())
    )
    try:
        # SessionLocal is the exact factory crud.get_session() wraps.
        with SessionLocal() as session:
            return [_post_to_dict(p) for p in session.scalars(stmt)]
    except Exception:  # missing table / locked file / schema drift
        log.exception("dashboard: could not read posts from %s", DB_PATH)
        return []


class DashboardHandler(BaseHTTPRequestHandler):
    """Read-only handler: /, /index.html, /favicon.ico and /api/posts."""

    server_version = "XPosterDashboard/1.0"
    protocol_version = "HTTP/1.1"  # keep-alive: the page polls every few seconds

    # ---- response helpers ---------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # browser navigated away
            self.close_connection = True

    def _send_json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send_index(self) -> None:
        try:
            with open(INDEX_FILE, "rb") as fh:
                body = fh.read()
        except OSError:
            log.exception("dashboard: cannot read %s", INDEX_FILE)
            self._send(500, b"index.html missing", "text/plain; charset=utf-8")
            return
        self._send(200, body, "text/html; charset=utf-8")

    # ---- routing ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802  (stdlib naming)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/posts":
            self._send_json(fetch_posts())
        elif path in ("/", "/index.html"):
            self._send_index()
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send_json({"error": "not found", "path": path}, status=404)

    def do_OPTIONS(self) -> None:  # noqa: N802  (CORS preflight)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- log noise control --------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        """Per-request lines at DEBUG — a polling page would flood INFO."""
        log.debug("%s - %s", self.address_string(), fmt % args)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def start_dashboard(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer | None:
    """Start the dashboard on a daemon thread. Idempotent and never raises.

    Returns the running server, or None when the port cannot be bound — the bot
    has to keep working even if the dashboard cannot start.
    """
    global _server
    with _lock:
        if _server is not None:
            return _server  # already running
        try:
            server = ThreadingHTTPServer((host, port), DashboardHandler)
        except OSError:
            log.exception("dashboard: cannot bind %s:%s — skipping", host, port)
            return None
        server.daemon_threads = True  # in-flight polls die with the process
        threading.Thread(target=server.serve_forever, name="dashboard-http",
                         daemon=True).start()
        _server = server
    log.info("dashboard: read-only UI at http://%s:%s", host, port)
    return _server


if __name__ == "__main__":
    # Standalone: `python -m web.server` — eyeball the UI without the bot running.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    from db.database import init_db

    init_db()
    start_dashboard()
    try:
        threading.Event().wait()  # serve_forever() runs in the daemon thread
    except KeyboardInterrupt:
        pass


