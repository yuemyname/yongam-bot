#!/bin/zsh
set -eu

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3가 필요합니다."
  echo "https://www.python.org/downloads/macos/ 에서 설치한 뒤 다시 실행하세요."
  echo
  read -k 1 "?아무 키나 누르면 닫힙니다."
  echo
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo ".env 설정 파일을 만들었습니다."
else
  echo "기존 .env 설정 파일을 그대로 사용합니다."
fi

echo "TextEdit에서 Bot Token과 Chat ID를 입력하고 저장하세요."
open -e .env
echo
echo "chat_id를 모르면 Telegram 봇에게 /start를 보낸 뒤 find_chat_id.command를 실행하세요."
echo
read -k 1 "?아무 키나 누르면 닫힙니다."
echo
