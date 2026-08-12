#!/bin/zsh
set -u

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

python3 watcher.py --test-telegram
EXIT_CODE=$?

echo
read -k 1 "?아무 키나 누르면 닫힙니다."
echo
exit "$EXIT_CODE"
