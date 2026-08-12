# CGV 용산 IMAX Telegram 봇 운영·개발 안내

이 문서는 봇을 설치하고 운영하거나 코드를 테스트하는 사람을 위한 안내입니다. 알림 구독과 사용 방법은 [README.md](README.md)를 참고하세요.

## BotFather 설정

1. Telegram에서 `@BotFather`에게 `/newbot`을 보내 봇을 만듭니다.
2. 발급된 HTTP API Token을 안전하게 보관합니다.
3. `/setcommands`를 실행하고 아래 명령어를 등록합니다.

```text
start - 알림 구독
stop - 알림 해지
status - 구독 상태 확인
help - 사용법 보기
```

BotFather의 `/setdescription`에는 저장소에서 제공하는 한글·영문 소개문을 등록할 수 있습니다.

## Railway 배포

저장소: [yuemyname/yongam-bot](https://github.com/yuemyname/yongam-bot)

1. Railway에서 **New Project → Deploy from GitHub repo**를 선택합니다.
2. `yuemyname/yongam-bot` 저장소를 연결합니다.
3. Volume을 추가하고 Mount Path를 `/data`로 설정합니다.
4. 서비스의 **Variables**에 다음 값을 등록합니다.

```dotenv
TELEGRAM_BOT_TOKEN=BotFather가_준_실제_토큰
TELEGRAM_CHAT_ID=운영자_chat_id
SUBSCRIPTIONS_ENABLED=true
```

`TELEGRAM_CHAT_ID`는 최초 운영자를 기존 알림 구독자로 한 번 등록하는 데 사용됩니다. 이후 일반 사용자는 자신의 채팅에서 `/start`만 보내면 자동 등록됩니다. `/stop`으로 해지한 운영자는 재배포 후에도 자동으로 다시 등록되지 않습니다.

구독자 목록, 처리한 Telegram 명령 위치, 기존 예매 알림 기록과 좌석 수는 `/data/notified.json`에 저장됩니다. Railway가 재시작되거나 새 버전을 배포해도 유지됩니다.

> Telegram에 webhook이 설정된 봇은 `getUpdates` 방식과 동시에 사용할 수 없습니다. 이 프로젝트 전용 봇에는 별도 webhook을 설정하지 마세요.

## Railway 로그 확인

정상 실행 시 다음과 비슷한 내용이 표시됩니다.

로그 시간은 `APP_TIMEZONE` 기준으로 표시되며 기본값은 한국시간(`Asia/Seoul`, `KST`)입니다.

```text
2026-08-13 00:54:36 KST INFO CGV Telegram Watcher 시작: ... 60초 간격
2026-08-13 00:54:39 KST INFO Telegram 구독 명령 처리 완료: 현재 구독자 3명
2026-08-13 00:54:41 KST INFO 조회 완료: 성공 28일, 오류 0일, ...
```

`HTTP 429`는 CGV가 요청을 일시적으로 제한한 상태입니다. 한 건이라도 발생하면 다음 전체 조회를 10분 뒤로 미루고, 연속으로 발생하면 20분, 30분까지 대기 시간을 늘립니다. 429 없이 조회가 끝나면 자동으로 1분 주기로 복귀합니다. 좌석 상세 판별만 실패한 경우에는 전체 잔여 좌석이 6석 이하면 알림을 보류하고, 7석 이상이면 좌석 종류를 확인하지 못했다는 표시와 함께 전체 잔여 수 기준으로 알림을 보냅니다.

## Mac에서 직접 실행

1. `setup.command`를 실행해 `.env`를 만듭니다.
2. `TELEGRAM_BOT_TOKEN`과 최초 운영자의 `TELEGRAM_CHAT_ID`를 입력합니다.
3. `test_telegram.command`로 Telegram 전송을 시험합니다.
4. `run.command`를 실행하거나 `install_background.command`로 로그인 시 자동 실행을 설치합니다.

Mac이 잠자기 상태이거나 덮개가 닫혀 있으면 감시가 중단될 수 있으므로 여러 구독자가 사용하는 봇은 Railway 운영을 권장합니다.

## 주요 설정값

| 이름 | 기본값 | 설명 |
|---|---:|---|
| `SUBSCRIPTIONS_ENABLED` | `true` | `/start`, `/stop` 자동 구독 기능 |
| `POLL_INTERVAL_SECONDS` | `60` | CGV 조회 및 Telegram 명령 확인 주기 |
| `RATE_LIMIT_BACKOFF_INITIAL_SECONDS` | `600` | HTTP 429 발생 후 첫 대기 시간(10분) |
| `RATE_LIMIT_BACKOFF_MAX_SECONDS` | `1800` | 연속 HTTP 429 시 최대 대기 시간(30분) |
| `DYNAMIC_DATE_WINDOW` | `true` | 오늘 기준 감시 범위를 매일 이동 |
| `TARGET_WINDOW_DAYS` | `28` | 오늘을 포함해 감시할 날짜 수 |
| `APP_TIMEZONE` | `Asia/Seoul` | 날짜 계산 기준 시간대 |
| `STRICT_IMAX_MATCH` | `true` | IMAX 이름 또는 코드가 있는 회차만 감지 |
| `MAX_WORKERS` | `4` | 날짜별 조회 동시 작업 수 |

## 개발 및 테스트

별도 Python 패키지 설치 없이 Python 3.10 이상에서 실행됩니다.

```bash
# 한 번 조회
python3 watcher.py --once

# Telegram 전송과 상태 저장 없이 확인
python3 watcher.py --once --dry-run

# 자동 테스트
python3 -m unittest discover -s tests -v
```

실제 `.env`, Bot Token, 구독자 상태 파일과 로그는 Git에 포함하지 마세요.
