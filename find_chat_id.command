#!/bin/zsh
set -u

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3가 필요합니다. README.md를 확인하세요."
  EXIT_CODE=1
else
  python3 watcher.py --find-chat-id
  EXIT_CODE=$?
fi

echo
read -k 1 "?아무 키나 누르면 닫힙니다."
echo
exit "$EXIT_CODE"
