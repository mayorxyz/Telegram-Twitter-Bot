"""Smoke check for the local dashboard (dev tool: localhost only, no credentials).

Run from the repo root:  python scripts/check_dashboard.py

Verifies that the dashboard starts on an ephemeral port, serves the page and the
JSON API, stops idempotently, reports a busy port as a warning instead of a
traceback (the bot must keep polling without it), and can be restarted.
"""
from __future__ import annotations

import logging
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")

from web.server import PORT, start_dashboard, stop_dashboard  # noqa: E402

failures: list[str] = []


def _get(url: str):
    import urllib.request

    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - localhost only
        return resp.status, resp.read()


def main() -> int:
    print(f"configured default port: {PORT}")

    # 1. Start on an ephemeral port and serve the page + the JSON API.
    srv = start_dashboard(port=0)
    if srv is None:
        failures.append("could not start the dashboard on an ephemeral port")
        print("[1] FAILED to start")
        return report()
    host, port = srv.server_address[:2]
    print(f"[1] started on http://{host}:{port}")

    status, page = _get(f"http://{host}:{port}/")
    print(f"[2] GET /          -> {status} ({len(page)} bytes)")
    if status != 200 or b"<" not in page:
        failures.append("GET / did not return the dashboard page")

    status, payload = _get(f"http://{host}:{port}/api/posts")
    print(f"[3] GET /api/posts -> {status} ({len(payload)} bytes of JSON)")
    if status != 200 or not payload.strip().startswith(b"["):
        failures.append("GET /api/posts did not return a JSON array")

    # 4. Stop twice: the second call must be a no-op, never an exception.
    stop_dashboard()
    stop_dashboard()
    print("[4] stop_dashboard() idempotent")

    # 5. A port owned by another socket: warn and return None, no traceback.
    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        res = start_dashboard(port=busy)
        print(f"[5] busy port {busy} -> returned {res!r} (None = handled, bot keeps "
              f"polling)")
        if res is not None:
            failures.append("a busy port was not reported as a startup failure")
            stop_dashboard()
    finally:
        blocker.close()

    # 6. Stop must actually free the port: start again right away.
    srv = start_dashboard(port=0)
    print(f"[6] restart after stop works: {srv.server_address[:2] if srv else None}")
    if srv is None:
        failures.append("the dashboard could not restart after a stop")
    else:
        stop_dashboard()

    return report()


def report() -> int:
    print("\nRESULT:", "ALL CHECKS PASSED" if not failures else f"FAILED -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
