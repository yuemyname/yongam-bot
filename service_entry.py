"""Deployment entry point with an explicit, fail-closed maintenance role."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import signal
import sys
import threading


def standby() -> int:
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    print("BOT_RUNTIME_ROLE=standby: CGV/Telegram workers are disabled; state is untouched.", flush=True)
    while not stopped.wait(30):
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    role = os.environ.get("BOT_RUNTIME_ROLE", "bot").strip().lower()
    if role == "standby":
        return standby()
    if role != "bot":
        print("Invalid BOT_RUNTIME_ROLE; no workers started.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime-lock", type=Path)
    args, remaining = parser.parse_known_args(argv)
    lock = None
    if args.runtime_lock is not None:
        lock = args.runtime_lock.open("a")
        os.chmod(args.runtime_lock, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            print("Another local bot owns this runtime lock; no workers started.", file=sys.stderr)
            return 75
    try:
        from watcher import main as run_watcher
        return run_watcher(remaining)
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
