#!/bin/zsh
set -u

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3가 필요합니다. README.md를 확인하세요."
  read -k 1 "?아무 키나 누르면 닫힙니다."
  echo
  exit 1
fi

if [[ ! -f .env ]]; then
  echo ".env가 없습니다. 먼저 setup.command를 실행하세요."
  read -k 1 "?아무 키나 누르면 닫힙니다."
  echo
  exit 1
fi

echo "CGV 감시기를 실행합니다. 이 창을 닫으면 감시가 중단됩니다."
echo "중단하려면 Control+C를 누르세요."
echo
python3 watcher.py
EXIT_CODE=$?

echo
echo "감시기가 종료되었습니다 (코드: $EXIT_CODE)."
read -k 1 "?아무 키나 누르면 닫힙니다."
echo
exit "$EXIT_CODE"
