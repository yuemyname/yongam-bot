"""Stage an initial private Mac deployment. Does not start or stop any service.

Railway variable JSON is read from stdin, never from command-line arguments.
Existing runtime configuration/state is deliberately never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tarfile

LABEL = "com.local.cgv-odyssey-telegram-watcher"


def state_summary(raw: bytes) -> dict:
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get("subscribers_initialized") is not True:
        raise ValueError("Expected an initialized complete bot state")
    for name in ("subscribers", "notified", "pending_deliveries", "cgv_recovery"):
        if not isinstance(state.get(name), dict):
            raise ValueError(f"Invalid state section: {name}")
    return {"sha256": hashlib.sha256(raw).hexdigest(),
            "subscribers": len(state["subscribers"]), "notified": len(state["notified"]),
            "pending_deliveries": len(state["pending_deliveries"])}


def local_values(variables: dict, root: Path, source: str) -> dict[str, str]:
    allowed = set(re.findall(r'value\(\s*"([A-Z_0-9]+)"', source))
    values = {key: str(value) for key, value in variables.items()
              if key in allowed and not key.startswith("RAILWAY_")}
    if not values.get("TELEGRAM_BOT_TOKEN") or not values.get("TELEGRAM_CHAT_ID"):
        raise ValueError("Telegram configuration is missing")
    for key in list(values):
        if key.startswith(("CGV_HEADER_PROBE_", "CGV_PUBLIC_MATRIX_", "CGV_WIRE_TRACE_")):
            values[key] = ""
    values.update(STATE_FILE=str(root / "data/notified.json"), STATE_DIR=str(root / "data"),
                  LOG_FILE=str(root / "logs/watcher.log"), LOG_DIR=str(root / "logs"))
    return values


def private_write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def launch_payload(root: Path, release: Path, python: str) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", python, str(release / "service_entry.py"),
                             "--runtime-lock", str(root / "data/runtime.lock"),
                             "--env-file", str(root / "config/.env"), "--verbose"],
        "WorkingDirectory": str(release), "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
        "EnvironmentVariables": {"BOT_RUNTIME_ROLE": "bot", "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(root / "logs/launchd.out.log"),
        "StandardErrorPath": str(root / "logs/launchd.err.log"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path.home() / "Library/Application Support/YongamBot")
    parser.add_argument("--plist", type=Path, default=Path.home() / f"Library/LaunchAgents/{LABEL}.plist")
    parser.add_argument("--railway-stopped", action="store_true", required=True,
                        help="Confirm Railway workers were stopped before taking this snapshot")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Mac deployment must be prepared on macOS")
    os.umask(0o077)
    root = args.root.expanduser().resolve()
    if root.exists() or args.plist.exists():
        parser.error("Runtime or LaunchAgent already exists; refusing to overwrite")
    raw = args.state.read_bytes()
    summary = state_summary(raw)
    repo = Path(__file__).resolve().parent
    values = local_values(json.load(sys.stdin), root, (repo / "watcher.py").read_text())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    archive = subprocess.check_output(["git", "archive", revision], cwd=repo)
    release = root / "releases" / revision
    for directory in (root, root / "config", root / "data", root / "logs", root / "backups", release):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(release, filter="data")
    dotenv = "".join(f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in sorted(values.items()))
    private_write(root / "config/.env", dotenv.encode())
    from watcher import Config
    config = Config.from_env_file(root / "config/.env")
    if config.state_file != root / "data/notified.json" or config.log_file != root / "logs/watcher.log":
        raise ValueError("Unexpected state/log path; refusing activation")
    private_write(root / "data/notified.json", raw)
    private_write(root / "backups/railway-final-state.json", raw)
    args.plist.parent.mkdir(parents=True, exist_ok=True)
    private_write(args.plist, plistlib.dumps(launch_payload(root, release, sys.executable)))
    print(json.dumps({"staged": True, "started": False, "revision": revision, **summary,
                      "open_only": config.open_only_mode,
                      "new_subscriptions": config.new_subscriptions_enabled,
                      "interval_seconds": config.poll_interval_seconds}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
