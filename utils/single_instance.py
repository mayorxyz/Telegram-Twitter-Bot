"""Single-instance guard: exactly one long-poller per bot token, per machine.

Telegram serves **one** ``getUpdates`` consumer per bot token. When a second
poller appears — a second copy of this bot, a leftover ``python main.py``, or a
different project that reuses the same ``BOT_TOKEN`` — both sessions start
failing with::

    Conflict: terminated by other getUpdates request

PTB then retries that forever and floods the log with tracebacks, and incoming
messages get split unpredictably between the two processes.

This module turns that into a loud, early, one-line refusal: the process takes an
exclusive OS-level lock on ``data/bot.lock`` *before* polling starts.

* The lock is held by the OS, not tracked by a PID file, so a crashed or
  force-killed process never leaves a stale lock behind — no cleanup logic and no
  timeout heuristics needed.
* The holder writes ``pid=<n>`` after the locked byte, so the loser can name the
  likely culprit in its message (byte 0 stays reserved for the lock itself,
  because Windows byte-range locks are mandatory: reading the locked byte fails).
* ``ALLOW_MULTI_INSTANCE=1`` bypasses the guard for deliberate setups, e.g. a
  short-lived debug boot next to a production boot.

Implementation: ``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX — same
non-blocking, exclusive, process-scoped semantics.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from utils import config

log = logging.getLogger(__name__)

try:  # Windows
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]
    import fcntl

BYPASS_ENV = "ALLOW_MULTI_INSTANCE"
_TRUE_VALUES = {"1", "true", "yes", "on"}

# data/bot.lock — lives next to bot.db / jobs.db, created on demand.
LOCK_PATH = str(Path(config.MEDIA_DIR).resolve().parent / "bot.lock")

_instance_lock: "InstanceLock | None" = None


class AlreadyRunningError(RuntimeError):
    """Another live process already holds the single-instance lock."""


def _lock_fd(fd: int) -> None:
    """Take the exclusive lock or raise OSError when someone else holds it."""
    if msvcrt is not None:  # Windows: mandatory byte-range lock at offset 0
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # POSIX: advisory whole-file lock
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceLock:
    """Exclusive lock on a lock file; usable as a context manager."""

    def __init__(self, path: str = LOCK_PATH) -> None:
        self.path = os.path.abspath(path)
        self._fd: int | None = None

    # ---- public API ---------------------------------------------------------

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "InstanceLock":
        """Take the lock or raise :class:`AlreadyRunningError`."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # O_BINARY is a no-op on POSIX and keeps byte offsets exact on Windows:
        # without it the CRT translates "\n" to "\r\n" in text mode and byte 0 of
        # the file would not stay the single reserved, locked byte.
        fd = os.open(self.path,
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\n")  # msvcrt.locking() needs a byte to exist
            _lock_fd(fd)
        except OSError as exc:
            holder = self._describe_holder()
            os.close(fd)
            raise AlreadyRunningError(
                f"another instance of this bot is already running{holder} "
                f"(lock file: {self.path}). Telegram allows only ONE long-polling "
                f"process per bot token, so this process stops now. Stop the other "
                f"instance, or set {BYPASS_ENV}=1 to bypass this check."
            ) from exc
        self._fd = fd
        self._write_pid()
        return self

    def release(self) -> None:
        """Drop the lock; no-op when not held (the OS also releases it on exit)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_fd(fd)
        except OSError:  # pragma: no cover - only on exotic filesystems
            log.debug("single-instance: unlock failed", exc_info=True)
        finally:
            os.close(fd)

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()

    # ---- internals ----------------------------------------------------------

    def _write_pid(self) -> None:
        """Store our PID at byte 1+ (byte 0 is the locked byte on Windows)."""
        payload = f"pid={os.getpid()}\n".encode()
        os.lseek(self._fd, 1, os.SEEK_SET)
        os.write(self._fd, payload)
        os.ftruncate(self._fd, 1 + len(payload))

    def _describe_holder(self) -> str:
        """Best-effort read of the holder's PID, for the refusal message."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(1)  # byte 0 is locked; skip it
                holder = fh.readline().decode("utf-8", "replace").strip()
        except OSError:
            holder = ""
        return f" ({holder})" if holder else ""


def bypass_enabled() -> bool:
    """True when ``ALLOW_MULTI_INSTANCE`` is set to a truthy value."""
    return os.environ.get(BYPASS_ENV, "").strip().lower() in _TRUE_VALUES


def acquire_default() -> "InstanceLock | None":
    """Take the process-wide lock. Returns None when the guard is bypassed."""
    global _instance_lock
    if bypass_enabled():
        log.warning("single-instance guard BYPASSED because %s is set", BYPASS_ENV)
        return None
    if _instance_lock is not None:
        return _instance_lock
    lock = InstanceLock().acquire()
    _instance_lock = lock
    log.info("single-instance lock held (%s)", lock.path)
    return lock


def release_default() -> None:
    """Release the process-wide lock; safe to call more than once."""
    global _instance_lock
    lock, _instance_lock = _instance_lock, None
    if lock is not None:
        lock.release()
        log.info("single-instance lock released")
