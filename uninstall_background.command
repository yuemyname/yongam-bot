#!/bin/zsh
set -u

LABEL="com.local.cgv-odyssey-telegram-watcher"
PLIST_FILE="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ -f "$PLIST_FILE" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST_FILE" >/dev/null 2>&1 || true
  TRASH_TARGET="$HOME/.Trash/${LABEL}.$(date +%Y%m%d-%H%M%S).plist"
  mv "$PLIST_FILE" "$TRASH_TARGET"
  echo "백그라운드 감시를 중지하고 설정을 휴지통으로 옮겼습니다."
  echo "복구 위치: $TRASH_TARGET"
else
  echo "설치된 백그라운드 감시 설정이 없습니다."
fi

echo
read -k 1 "?아무 키나 누르면 닫힙니다."
echo
