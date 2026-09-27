"""Smoke check for the single-instance guard (dev tool: no network, no credentials).

Run from the repo root:  python scripts/check_single_instance.py

Verifies: a second process is refused with the holder's PID, ALLOW_MULTI_INSTANCE
bypasses the guard, release frees the lock for the next process, and the lock file
keeps byte 0 reserved with ``pid=<n>`` written after it.
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Redirected streams on Windows fall back to cp1252; keep the checks printable
# when their output is piped or written to a file.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

from utils import single_instance as si  # noqa: E402

CHILD_CODE = (
    "import sys; sys.path.insert(0, r'{root}'); "
    "from utils import single_instance as si; "
    "si.acquire_default(); print('CHILD-LOCK-OK')"
).format(root=ROOT)


def run_child(env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", CHILD_CODE], cwd=ROOT, env=env,
                          capture_output=True, text=True)


def main() -> int:
    failures = []

    # 1. Parent takes the lock.
    lock = si.acquire_default()
    assert lock is not None, "expected a lock object"
    print(f"[1] parent acquired {lock.path} (held={lock.held}, pid written)")

    # 2. Second process must be refused with an actionable message.
    p = run_child()
    last_err = p.stderr.strip().splitlines()[-1] if p.stderr.strip() else ""
    ok = p.returncode != 0 and "already running" in p.stderr and "pid=" in p.stderr
    print(f"[2] child refused: exit={p.returncode} msg={last_err[:140]}")
    if not ok:
        failures.append("child was not refused with a clear holder-PID message")

    # 3. Bypass env lets the child start anyway.
    p = run_child({si.BYPASS_ENV: "1"})
    print(f"[3] child with {si.BYPASS_ENV}=1: exit={p.returncode} "
          f"out={p.stdout.strip()[:60]}")
    if "CHILD-LOCK-OK" not in p.stdout:
        failures.append("bypass did not let the child acquire")

    # 4. Release frees the lock, then a child can take it.
    si.release_default()
    print(f"[4] parent released (held={lock.held})")
    if lock.held:
        failures.append("release left the lock held")

    p = run_child()
    print(f"[5] child after release: exit={p.returncode} out={p.stdout.strip()[:60]}")
    if "CHILD-LOCK-OK" not in p.stdout:
        failures.append(f"child could not acquire a released lock (stderr={last_err[:120]})")

    # 6. Lock file content: byte 0 reserved, pid stored after it.
    with open(lock.path, "rb") as fh:
        raw = fh.read()
    print(f"[6] lock file bytes={raw!r}")
    if not raw[1:].decode().startswith("pid="):
        failures.append("pid is not written after the reserved byte 0")

    # 7. Re-acquiring in-process is idempotent, double release is safe.
    again = si.acquire_default()
    si.release_default()
    si.release_default()
    print(f"[7] idempotent acquire/release ok ({again is not None})")

    print("\nRESULT:", "ALL CHECKS PASSED" if not failures else f"FAILED -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
