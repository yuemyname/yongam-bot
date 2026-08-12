#!/bin/zsh
set -eu

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"
LABEL="com.local.cgv-odyssey-telegram-watcher"
PLIST_FILE="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f .env ]]; then
  echo ".env가 없습니다. 먼저 setup.command를 실행하세요."
  read -k 1 "?아무 키나 누르면 닫힙니다."
  echo
  exit 1
fi

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3가 필요합니다. README.md를 확인하세요."
  read -k 1 "?아무 키나 누르면 닫힙니다."
  echo
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

python3 - "$PLIST_FILE" "$PYTHON_BIN" "$PROJECT_DIR" "$LABEL" <<'PY'
import pathlib
import plistlib
import sys

plist_path = pathlib.Path(sys.argv[1])
python_bin = sys.argv[2]
project_dir = pathlib.Path(sys.argv[3])
label = sys.argv[4]
payload = {
    "Label": label,
    "ProgramArguments": [python_bin, str(project_dir / "watcher.py")],
    "WorkingDirectory": str(project_dir),
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ProcessType": "Background",
    "StandardOutPath": str(project_dir / "logs" / "launchd.out.log"),
    "StandardErrorPath": str(project_dir / "logs" / "launchd.err.log"),
}
with plist_path.open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=True)
PY

launchctl bootout "gui/$(id -u)" "$PLIST_FILE" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_FILE"

echo "백그라운드 감시를 설치하고 시작했습니다."
echo "Mac 로그인 후 자동으로 실행됩니다."
echo "로그: $PROJECT_DIR/logs/watcher.log"
echo
read -k 1 "?아무 키나 누르면 닫힙니다."
echo
