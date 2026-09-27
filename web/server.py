"""Read-only local dashboard: stdlib HTTP server exposing the posts table as JSON."""
from __future__ import annotations

import ast
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
from utils import config
from utils.formatting import display_content

log = logging.getLogger(__name__)

HOST = config.DASHBOARD_HOST
PORT = config.DASHBOARD_PORT
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")

_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()


def _safe_list(val) -> list:
    """Safely convert DB value to a list of strings, handling JSON, Python lists, or raw strings."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return []
        # Try standard JSON first
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        # Fallback for Python-style list strings like "['data/media/x.jpg']"
        try:
            parsed = ast.literal_eval(val)
            if isinstance(parsed, list):
                return parsed
        except (ValueError, SyntaxError):
            pass
        # If it's just a single raw path string
        return [val]
    return list(val)


def _iso_utc(dt: datetime | None) -> str | None:
    """Naive-UTC DB datetime -> ISO-8601 string with an explicit Z suffix."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds") + "Z"


def _basename(path) -> str | None:
    """Only the file name is ever exposed — /media/<name> is how it gets served."""
    if not path:
        return None
    name = os.path.basename(str(path).replace("\\", "/").rstrip("/"))
    return name or None


def _basenames(paths) -> list[str]:
    return [n for n in (_basename(p) for p in _safe_list(paths)) if n]


def _post_to_dict(post: Post) -> dict:
    """One Post row (plus its thread tweets, when present) as a JSON-safe dict."""
    thread = post.thread
    media = _basenames(post.media)
    tweets = [] if thread is None else [
        {"position": t.position,
         "content": t.content or "",
         "display_content": display_content(t.content, len(t.media_ids or [])),
         "media_ids": _basenames(t.media_ids)}
        for t in thread.tweets
    ]
    return {
        "id": post.id,
        "content": post.content or "",
        "display_content": display_content(post.content, len(media)),
        "status": post.status or "",
        "tag": post.tag,
        "scheduled_at": _iso_utc(post.scheduled_at),
        "tweet_id": post.tweet_id,
        "error": post.error,
        "is_thread": thread is not None,
        "tweets": tweets,
        "media_path": None if media else _basename(post.media_path),
        "media_ids":  media,
    }


def fetch_posts() -> list[dict]:
    """Every post, newest first. Returns [] when the DB/table isn't there yet."""
    if not os.path.exists(DB_PATH):
        return []
    stmt = (
        select(Post)
        .options(selectinload(Post.thread).selectinload(Thread.tweets))
        .order_by(Post.id.desc())
    )
    try:
        with SessionLocal() as session:
            return [_post_to_dict(p) for p in session.scalars(stmt)]
    except Exception:
        log.exception("dashboard: could not read posts from %s", DB_PATH)
        return []


class DashboardHandler(BaseHTTPRequestHandler):
    """Read-only handler: /, /index.html, /favicon.ico, /api/posts, and /media/."""

    server_version = "XPosterDashboard/1.0"
    protocol_version = "HTTP/1.1"

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
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _send_index(self) -> None:
        try:
            with open(INDEX_FILE, "rb") as fh:
                body = fh.read()
        except OSError:
            log.exception("dashboard: cannot read %s", INDEX_FILE)
            self._send(500, b"index.html missing", "text/plain; charset=utf-8")
            return
        self._send(200, body, "text/html; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        
        if path in ("/", "/index.html"):
            self._send_index()
            return

        if path == "/api/posts":
            self._send_json(fetch_posts())
            return

        # Serve media files statically with robust path handling
        if "/media/" in path or "data/media" in path:
            normalized_path = path.replace("\\", "/")
            
            if normalized_path.startswith("/data/media/"):
                filename = normalized_path[len("/data/media/"):]
            elif normalized_path.startswith("/media/"):
                filename = normalized_path[len("/media/"):]
            else:
                filename = normalized_path.split("/")[-1]
                
            file_path = os.path.abspath(os.path.join(config.MEDIA_DIR, filename))
            media_dir = os.path.abspath(config.MEDIA_DIR)
            
            log.debug("Media request: requested=%s, filename=%s, resolved=%s, allowed_dir=%s", 
                      path, filename, file_path, media_dir)
            
            if file_path.startswith(media_dir + os.sep) and os.path.isfile(file_path):
                self.send_response(200)
                if file_path.endswith((".jpg", ".jpeg")):
                    self.send_header("Content-Type", "image/jpeg")
                elif file_path.endswith(".png"):
                    self.send_header("Content-Type", "image/png")
                elif file_path.endswith((".mp4", ".mov", ".webm")):
                    self.send_header("Content-Type", "video/mp4")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=31536000")
                self.end_headers()
                try:
                    with open(file_path, "rb") as f:
                        self.wfile.write(f.read())
                except OSError as e:
                    log.error("Failed to read media file %s: %s", file_path, e)
                    self.send_response(500)
                    self.end_headers()
                return
            else:
                log.warning("Media access denied or not found: requested=%s, resolved=%s, allowed_dir=%s, exists=%s",
                            path, file_path, media_dir, os.path.isfile(file_path))
                self._send_json({"error": "file not found"}, status=404)
                return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        self._send_json({"error": "not found", "path": path}, status=404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def start_dashboard(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer | None:
    global _server
    with _lock:
        if _server is not None:
            return _server
        try:
            server = ThreadingHTTPServer((host, port), DashboardHandler)
        except OSError as exc:
            log.warning("dashboard: cannot bind %s:%s (%s)", host, port, exc)
            return None
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True).start()
        _server = server
        bound_host, bound_port = server.server_address[:2]
    log.info("dashboard: read-only UI at http://%s:%s", bound_host, bound_port)
    return _server


def stop_dashboard() -> None:
    global _server
    with _lock:
        server, _server = _server, None
    if server is None:
        return
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        log.exception("dashboard: shutdown failed")
    else:
        log.info("dashboard: stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    from db.database import init_db
    init_db()
    start_dashboard()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_dashboard()