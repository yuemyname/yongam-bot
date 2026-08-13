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
mode - 알림 종류 확인·변경
mode_all - 신규 오픈 + 잔여 좌석 받기
mode_open - 신규 오픈만 받기
mode_seats - 잔여 좌석만 받기
seat - 좌석 알림 범위 확인·변경
seat_all - 좌석 종류 미확인 알림도 받기
seat_verified - 일반 좌석 확인된 알림만 받기
desc - 봇 설명과 사용 방법
coffee - 개발자에게 커피 후원
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

구독자 목록, 처리한 Telegram 명령 위치, 기존 예매 알림 기록과 좌석 수, 실패한 일정 재조회 목록은 `/data/notified.json`에 저장됩니다. Railway가 재시작되거나 새 버전을 배포해도 유지됩니다.

> Telegram에 webhook이 설정된 봇은 `getUpdates` 방식과 동시에 사용할 수 없습니다. 이 프로젝트 전용 봇에는 별도 webhook을 설정하지 마세요.

## Railway 로그 확인

정상 실행 시 다음과 비슷한 내용이 표시됩니다.

로그 시간은 `APP_TIMEZONE` 기준으로 표시되며 기본값은 한국시간(`Asia/Seoul`, `KST`)입니다.

```text
2026-08-13 00:54:36 KST INFO CGV Telegram Watcher 시작: ... 60초 간격
2026-08-13 00:54:39 KST INFO Telegram 구독자 변경: 현재 3명
2026-08-13 00:54:41 KST INFO 조회 완료: 성공 28일, 오류 0일, ...
```

`HTTP 429`는 CGV가 요청을 일시적으로 제한한 상태입니다. 봇은 CGV 요청을 최소 2초 간격으로 하나씩 보내며, 첫 429가 나오면 해당 주기의 남은 일정·좌석 요청을 즉시 중단합니다. 실패하거나 생략된 일정 날짜는 상태 파일에 보존해 다음 정상 주기에 신규 오픈 후보 다음으로 우선 재조회합니다. 다음 조회는 30분 뒤로 미루고, 연속으로 발생하면 60분, 최대 120분까지 대기 시간을 늘립니다. 429 없이 조회가 끝나면 자동으로 1분 주기로 복귀합니다. 좌석 상세 판별만 실패한 경우에는 전체 잔여 좌석이 6석 이하면 알림을 보류하고, 7석 이상이면 좌석 종류를 확인하지 못했다는 표시와 함께 전체 잔여 수 기준으로 알림을 보냅니다.

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
| `POLL_INTERVAL_SECONDS` | `60` | CGV 전체 조회 주기 |
| `TELEGRAM_COMMAND_POLL_SECONDS` | `2` | CGV 조회 중·대기 중 Telegram 명령 확인 주기 |
| `CGV_REQUEST_SPACING_SECONDS` | `2` | 연속 CGV 요청 사이의 최소 간격 |
| `RATE_LIMIT_BACKOFF_INITIAL_SECONDS` | `1800` | HTTP 429 발생 후 첫 대기 시간(30분) |
| `RATE_LIMIT_BACKOFF_MAX_SECONDS` | `7200` | 연속 HTTP 429 시 최대 대기 시간(120분) |
| `DYNAMIC_DATE_WINDOW` | `true` | 오늘 기준 감시 범위를 매일 이동 |
| `TARGET_WINDOW_DAYS` | `28` | 오늘을 포함해 감시할 날짜 수 |
| `APP_TIMEZONE` | `Asia/Seoul` | 날짜 계산 기준 시간대 |
| `STRICT_IMAX_MATCH` | `true` | IMAX 이름 또는 코드가 있는 회차만 감지 |
| `BOOKING_CLOSE_MARGIN_MINUTES` | `0` | 상영 시작 몇 분 전부터 예매 마감으로 볼지 |
| `DEFERRED_RECHECK_CYCLES` | `5` | 보류된 회차를 몇 주기마다 다시 조회할지 |
| `SCAN_MODE` | `cursor` | 신규 오픈 후보를 먼저 보는 조회 방식 (`full`로 전체 조회 가능) |
| `CURSOR_PROBE_DAYS` | `3` | `cursor` 모드에서 프론티어 뒤로 더 볼 일수 |
| `CURSOR_EXPANSION_DAYS` | `21` | 신규 오픈 감지 시 한 번에 확장 조회할 일수 |
| `FULL_SCAN_EVERY_CYCLES` | `10` | `cursor` 모드에서도 전체를 조회하는 주기 |

`DEFERRED_RECHECK_CYCLES`는 이미 예매 오픈 알림을 보낸 회차의 좌석 상세 판별이 실패했을 때만 적용됩니다. 아직 한 번도 알리지 않은 신규 회차는 오픈 알림을 놓치지 않도록 매 주기 다시 확인하고, 잔여 좌석 수가 바뀐 경우에도 설정된 대기 횟수와 관계없이 즉시 다시 확인합니다.

## 조회 범위 — full과 cursor

`SCAN_MODE=full`은 매 주기 감시 기간 28일을 모두 조회합니다. `CGV_REQUEST_SPACING_SECONDS=2` 때문에 한 주기에 최소 약 56초가 걸리고, 좌석 상세 조회가 붙으면 `POLL_INTERVAL_SECONDS`를 넘겨 사실상 쉬는 시간 없이 돌아갑니다.

기본값인 `SCAN_MODE=cursor`는 조회 범위를 좁히고 신규 오픈을 우선합니다. 상태 파일에 **프론티어** — IMAX 회차가 관측된 가장 늦은 상영일 — 를 저장하고, 매 주기 다음 순서로 조회합니다.

```
[프론티어+1 .. +3]        커서 (신규 오픈 감지용)
[프론티어 .. 오늘]        이미 열린 구간 (최신 날짜부터 역순)
```

예를 들어 마지막 예매 가능일이 8월 20일이면 `8/21 → 8/22 → 8/23 → 8/20 → 8/19 → … → 오늘` 순서로 일정 API를 요청합니다.

커서 구간에서 회차가 잡히면 같은 주기 안에서 `프론티어 + CURSOR_EXPANSION_DAYS`까지 확장 조회해 새로 열린 범위의 끝을 한 번에 찾습니다.

예매가 13일 앞까지 열려 있을 때 주기당 요청은 28건에서 **17건**으로 줄어듭니다. 절감폭은 예매가 얼마나 앞까지 열려 있는지에 비례해 달라집니다.

**누락 방지 장치**

- `FULL_SCAN_EVERY_CYCLES` 주기마다 한 번은 전체를 조회합니다. 커서가 놓친 것이 있어도 최대 그 주기 안에 잡힙니다
- 상태 파일에 프론티어가 없으면(최초 실행, 볼륨 초기화) 전체를 조회합니다
- 프론티어는 **전진만** 합니다. HTTP 429로 조회가 중간에 끊겨도 프론티어가 뒤로 밀리지 않습니다
- 실패했거나 429 뒤에 생략된 날짜는 상태 파일에 남겨 다음 정상 주기에 우선 재조회합니다
- 기존 좌석 상세 조회는 전체 일정 탐색 뒤에 실행해 신규 회차 발견을 막지 않습니다

**알려진 한계**

프론티어와 다음 오픈 날짜 사이에 `CURSOR_PROBE_DAYS`보다 큰 공백이 있으면 커서가 넘지 못하고, 다음 전체 조회 때까지 감지가 늦어집니다. 기본값 기준 최대 10주기입니다. `CURSOR_PROBE_DAYS`를 늘리면 줄어들지만 그만큼 요청이 늘어납니다.

**전환 방법**

매 주기 로그에 조회 범위가 남습니다.

```text
조회 시작: 전체 모드, 28일 (2026-08-13~2026-09-09)
조회 시작: 커서 모드, 17일 (2026-08-13~2026-08-29)
```

기본값 그대로 `cursor`로 운영하면 됩니다. 전체 28일을 매 주기 모두 확인해야 할 때만 Railway 환경변수에 `SCAN_MODE=full`을 넣으세요. `cursor` 모드도 `FULL_SCAN_EVERY_CYCLES` 주기마다 전체 범위를 확인합니다.

## 구독 현황 확인 — `/stats`

`.env`의 `TELEGRAM_CHAT_ID`로 지정된 운영자 계정에서만 응답합니다. 다른 사람이 보내면 일반 안내 문구가 나가므로, 명령이 있다는 사실 자체가 구독자에게 드러나지 않습니다. **BotFather 명령어 목록에 등록하지 마세요.**

```text
📊 구독 현황
전체 11명
채팅 유형: 개인 8명, 그룹 2명, 미상 1명

알림 종류
• 신규 오픈 + 잔여 좌석 — 8명
• 신규 오픈만 — 2명
• 잔여 좌석만 — 1명

잔여 좌석 알림 범위
• 좌석 종류 미확인 알림도 포함 — 9명
• 일반 좌석이 확인된 알림만 — 2명
```

`/subscribers`도 같은 결과를 냅니다. 알림 종류를 설정한 적 없는 구독자는 기본값(`신규 오픈 + 잔여 좌석`, `미확인 포함`)으로 집계됩니다.

## 회차별 판정 확인

평소 로그는 알림을 보냈거나 제외·보류한 회차만 남깁니다. 좌석 수가 그대로인 회차는 아무 줄도 남기지 않습니다. 매분 수십 줄이 쌓이는 것을 막기 위해서입니다.

특정 회차가 왜 알림이 안 왔는지 확인하려면 `--verbose`로 실행하세요. 주기 끝에 그 주기에 본 모든 회차의 판정이 **한 줄로** 나옵니다.

```text
DEBUG 회차 판정 3건 | 08-13 07:00 400/624 제외·예매 마감 | 08-13 19:00 412/624 발송·예매 오픈 | 08-13 22:00 5/624 보류·좌석종류 미확인
```

판정 문구는 다음 중 하나입니다.

| 판정 | 뜻 |
|---|---|
| `발송·예매 오픈` | 새 회차로 감지해 알림 발송 |
| `발송·좌석 변경 412→411` | 좌석 수 변동으로 알림 발송 |
| `변화없음` | 직전 확인과 좌석 수가 같아 아무 일도 하지 않음 |
| `첫 관측·기록만` | 처음 본 회차의 좌석 수를 저장만 함 |
| `오픈 알림으로 갈음` | 같은 주기에 오픈 알림을 보내 좌석 알림은 생략 |
| `제외·예매 마감` | 상영이 이미 시작됨 |
| `제외·매진` | 잔여 0석 |
| `제외·장애인석만` / `제외·A열만` | 남은 좌석이 그 종류뿐 |
| `보류·좌석종류 미확인` | 좌석표를 못 읽었고 잔여 6석 이하 |

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
