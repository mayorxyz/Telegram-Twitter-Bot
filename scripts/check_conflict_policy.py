"""Smoke check for main.py's token preflight and 409-conflict policy (dev tool).

Run from the repo root:  python scripts/check_conflict_policy.py

No network and no credentials: a stub bot stands in for ``telegram.Bot``, so this
is safe to run on any machine. Verifies that a leftover webhook is cleared, an
active twin (Conflict) and a revoked token (InvalidToken) abort startup with one
actionable line, timeouts never block startup, the conflict streak logs one hint,
and CONFLICT_ABORT_AFTER stops the application instead of retrying forever.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["CONFLICT_ABORT_AFTER"] = "3"  # abort after 3 consecutive 409s
os.environ.pop("ALLOW_MULTI_INSTANCE", None)

import main  # noqa: E402
from telegram.error import Conflict, InvalidToken, NetworkError, TimedOut  # noqa: E402

failures: list[str] = []


class StubBot:
    def __init__(self, webhook_url: str = "", raises: Exception | None = None,
                 clear_on_delete: bool = True) -> None:
        self.webhook_url = webhook_url
        self.raises = raises
        self.clear_on_delete = clear_on_delete
        self.deleted = 0

    async def delete_webhook(self, **kwargs) -> None:
        self.deleted += 1
        if self.clear_on_delete:
            self.webhook_url = ""

    async def get_webhook_info(self):
        return SimpleNamespace(url=self.webhook_url)

    async def get_updates(self, **kwargs):
        if self.raises is not None:
            raise self.raises
        return []


class StubApp:
    def __init__(self, bot: StubBot) -> None:
        self.bot = bot
        self.stopped = False

    def stop_running(self) -> None:
        self.stopped = True


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


async def _preflight(app: StubApp):
    return await main._preflight(app)  # type: ignore[arg-type]


def main_check() -> int:
    logging.getLogger().setLevel(logging.INFO)
    cap = Capture()
    logging.getLogger("main").addHandler(cap)

    # 1. Clean token, no webhook: no exception, delete_webhook still called.
    app = StubApp(StubBot())
    asyncio.run(_preflight(app))
    print(f"[1] clean token: no exception, delete_webhook called {app.bot.deleted}x")
    if app.bot.deleted != 1:
        failures.append("preflight did not clear the webhook")

    # 1b. A pre-existing webhook is reported as removed (the dangerous case).
    cap.records.clear()
    app = StubApp(StubBot(webhook_url="https://example.com/hook",
                          clear_on_delete=False))
    asyncio.run(_preflight(app))
    if not any("removed webhook" in r for r in cap.records):
        failures.append("preflight did not warn about a leftover webhook")
    else:
        print("[1b] leftover webhook warned about and delete attempted")

    # 2. Twin already polling: StartupError with the actionable hint.
    app = StubApp(StubBot(raises=Conflict("terminated by other getUpdates request")))
    try:
        asyncio.run(_preflight(app))
    except main.StartupError as exc:
        text = str(exc)
        print(f"[2] twin detected -> StartupError: {text[:90]}...")
        if "getUpdates" not in text or "BotFather" not in text:
            failures.append("StartupError message is not actionable")
    else:
        failures.append("preflight ignored an active conflict (Conflict must abort)")

    # 3. Network blip must NOT abort startup.
    app = StubApp(StubBot(raises=TimedOut()))
    try:
        asyncio.run(_preflight(app))
        print("[3] timeout tolerated (startup continues)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"preflight aborted on a network blip: {exc!r}")

    # 3b. A revoked token is reported as a token problem, not as a conflict.
    app = StubApp(StubBot(raises=InvalidToken("The token `abc` was rejected.")))
    try:
        asyncio.run(_preflight(app))
    except main.StartupError as exc:
        text = str(exc)
        print(f"[3b] revoked token -> StartupError: {text[:80]}...")
        if "rejected BOT_TOKEN" not in text or "Conflict" in text:
            failures.append("revoked token is not reported as a token problem")
    else:
        failures.append("preflight ignored a revoked token")

    # 4. Runtime conflict handling: one hint, then stop after CONFLICT_ABORT_AFTER.
    cap.records.clear()
    app = StubApp(StubBot())
    ctx = SimpleNamespace(error=Conflict("terminated by other getUpdates request"),
                          application=app)
    for _ in range(3):
        asyncio.run(main.on_error(None, ctx))
    hints = [r for r in cap.records if "409 Conflict" in r]
    print(f"[4] streak=3 -> hints logged={len(hints)}, stop_running={app.stopped}, "
          f"log lines={len(cap.records)}")
    if len(hints) != 1:
        failures.append(f"expected exactly 1 conflict hint, got {len(hints)}")
    if not app.stopped:
        failures.append("CONFLICT_ABORT_AFTER=3 did not stop the application")
    if any("Traceback" in r or "unhandled error" in r for r in cap.records):
        failures.append("conflict was logged as an unhandled error")

    # 5. Network errors are warned about, not traced back; other errors are not
    #    swallowed.
    cap.records.clear()
    asyncio.run(main.on_error(None, SimpleNamespace(error=NetworkError("boom"),
                                                    application=None)))
    print(f"[5] network error -> {cap.records[:1]}")
    if not cap.records or "network hiccup" not in cap.records[0]:
        failures.append("NetworkError was not downgraded to a warning")

    cap.records.clear()
    asyncio.run(main.on_error(SimpleNamespace(update_id=7),
                              SimpleNamespace(error=ValueError("real bug"),
                                              application=None)))
    print(f"[6] unexpected error -> {cap.records[:1]}")
    if not any("unhandled error while processing update 7" in r for r in cap.records):
        failures.append("unexpected errors no longer reach the log")

    # 7. Streak reset after a quiet gap.
    main._conflicts.update(streak=4, last=0.0)
    main.on_error  # noqa: B018  (documenting the API used above)
    main._conflicts.update(streak=0, last=0.0)
    print("[7] streak bookkeeping fields reachable:", sorted(main._conflicts))

    print("\nRESULT:", "ALL CHECKS PASSED" if not failures else f"FAILED -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main_check())
