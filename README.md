# CGV 용산 IMAX Telegram 알림 봇

CGV 용산아이파크몰 IMAX의 예매 오픈과 잔여 좌석 변화를 Telegram으로 알려주는 비공식 알림 봇입니다.

- 영화: 오디세이 (`movNo=30001323`)
- 극장: CGV 용산아이파크몰 (`siteNo=0013`)
- 감시 기간: 한국시간 기준 오늘을 포함한 28일(4주)
- 확인 주기: 1분
- CGV 로그인·쿠키: 사용하지 않음

## 알림 받는 방법

1. Telegram에서 운영자가 알려준 봇을 엽니다.
2. **시작** 버튼을 누르거나 `/start`를 보냅니다.
3. `✅ CGV 용산 IMAX 알림 구독이 완료되었습니다.`라는 답장이 오면 등록 완료입니다.

봇은 1분마다 새 명령을 확인하므로 답장이 오는 데 최대 약 1분이 걸릴 수 있습니다.

| 명령어 | 기능 |
|---|---|
| `/start` | 알림 구독 |
| `/stop` | 알림 해지 |
| `/status` | 현재 구독 상태 확인 |
| `/help` | 사용 가능한 명령어 확인 |

그룹에서 알림을 받으려면 봇을 그룹에 추가한 뒤 그룹 안에서 `/start`를 보내세요. 그룹에서 명령어가 `/start@봇이름`으로 표시되어도 정상 처리됩니다.

## 어떤 알림이 오나요?

### 예매 오픈 알림

새 IMAX 상영 회차가 열리면 날짜별로 모아서 알려줍니다.

```text
🎟️ CGV 예매 오픈 감지
영화: 오디세이 (30001323)
극장: 용산아이파크몰 (0013)

━━━━━━━━━━━━━━━━━━━━
📅 상영일: 2026-08-26 (수)
━━━━━━━━━━━━━━━━━━━━
• 상영 시작시간 14:30 — 620/624석
• 상영 시작시간 18:00 — 598/624석

예매 바로가기: https://cgv.co.kr/cnm/movieBook/movie?...
```

### 잔여 좌석 변경 알림

이미 열린 회차의 전체 잔여 좌석 수 또는 일반 예매 가능 좌석 수가 바뀌면 알려줍니다.

```text
💺 CGV 잔여 좌석 변경
영화: 오디세이 (30001323)
극장: 용산아이파크몰 (0013)

━━━━━━━━━━━━━━━━━━━━
📅 상영일: 2026-08-26 (수)
━━━━━━━━━━━━━━━━━━━━
상영 시작시간: 18:00
잔여좌석/총좌석: 597/624석 (이전 598/624석)
일반 예매 가능: 140석 → 139석
장애인석: 2석

예매 바로가기: https://cgv.co.kr/cnm/movieBook/movie?...
```

## 알림하지 않는 경우

공개 좌석표에서 다음 상태가 확실하게 확인되면 알림을 보내지 않습니다.

- 예매 가능한 좌석이 장애인석뿐인 경우
- 예매 가능한 좌석이 모두 A열인 경우
- 좌석 수가 이전 확인과 동일한 경우

좌석표의 좌석 수와 상영일정의 잔여 수가 정확히 일치하고 모든 좌석의 열 정보가 있을 때만 제외합니다. CGV 좌석 상세 조회가 불완전하면 A열 또는 장애인석뿐이라고 추측해서 알림을 숨기지 않습니다.

## 예매 링크 사용법

알림의 링크에는 오디세이, 용산아이파크몰, 해당 상영일이 설정됩니다.

CGV 예매 화면은 IMAX 필터 선택 상태를 링크로 전달하지 않으므로, 화면이 열리면 상단의 **IMAX** 버튼을 한 번 눌러주세요. 봇이 감지하고 알리는 회차 자체는 IMAX 조건으로만 선별됩니다.

## 자주 묻는 질문

### `/start`를 보냈는데 답장이 없어요

- 최대 1분 정도 기다려 보세요.
- 봇을 차단하지 않았는지 확인하세요.
- 개인 채팅에서는 Telegram의 **시작** 버튼도 눌러 보세요.
- 계속 답장이 없다면 운영자에게 Railway 실행 상태를 확인해 달라고 요청하세요.

### 구독 전에 이미 열린 회차도 다시 알려주나요?

이미 전체 구독자에게 알린 예매 오픈을 새 구독자에게 다시 보내지는 않습니다. 구독한 뒤 발생하는 새 회차와 좌석 변화부터 받을 수 있습니다.

### Mac을 꺼도 알림이 오나요?

Railway에 배포된 봇은 Mac과 관계없이 계속 실행됩니다. Mac에서만 직접 실행한 경우에는 Mac이 켜져 있고 잠자기 상태가 아니어야 합니다.

### 공식 CGV 봇인가요?

아닙니다. CGV 공개 상영일정·좌석 조회 기능을 이용하는 개인 제작 비공식 봇입니다. CGV가 API 또는 접속 정책을 바꾸면 조회가 일시적으로 실패할 수 있습니다.

## 개인정보

구독 기능은 알림 전송에 필요한 Telegram `chat_id`, 채팅 유형, 표시 이름과 구독 시각만 Railway 영구 볼륨에 저장합니다. Telegram Bot Token은 Railway 비밀 환경변수에만 저장하며 메시지나 GitHub 저장소에 기록하지 않습니다. CGV 로그인 정보와 쿠키는 요구하거나 저장하지 않습니다.

---

## 운영자 안내

아래 내용은 봇을 설치하고 운영하는 사람을 위한 안내입니다.

### BotFather 설정

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

### Railway 배포

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

### Railway 로그 확인

정상 실행 시 다음과 비슷한 내용이 표시됩니다.

```text
CGV Telegram Watcher 시작: ... 60초 간격
Telegram 구독 명령 처리 완료: 현재 구독자 3명
조회 완료: 성공 28일, 오류 0일, ...
```

`HTTP 429`는 CGV가 좌석 상세 요청을 일시적으로 제한한 상태입니다. 이때 봇은 확인되지 않은 좌석을 A열 또는 장애인석뿐이라고 판단하지 않고 다음 주기에 다시 조회합니다. 제한이 계속되면 `POLL_INTERVAL_SECONDS=120`으로 늘릴 수 있습니다.

### Mac에서 직접 실행

1. `setup.command`를 실행해 `.env`를 만듭니다.
2. `TELEGRAM_BOT_TOKEN`과 최초 운영자의 `TELEGRAM_CHAT_ID`를 입력합니다.
3. `test_telegram.command`로 Telegram 전송을 시험합니다.
4. `run.command`를 실행하거나 `install_background.command`로 로그인 시 자동 실행을 설치합니다.

Mac이 잠자기 상태이거나 덮개가 닫혀 있으면 감시가 중단될 수 있으므로 여러 구독자가 사용하는 봇은 Railway 운영을 권장합니다.

### 주요 설정값

| 이름 | 기본값 | 설명 |
|---|---:|---|
| `SUBSCRIPTIONS_ENABLED` | `true` | `/start`, `/stop` 자동 구독 기능 |
| `POLL_INTERVAL_SECONDS` | `60` | CGV 조회 및 Telegram 명령 확인 주기 |
| `DYNAMIC_DATE_WINDOW` | `true` | 오늘 기준 감시 범위를 매일 이동 |
| `TARGET_WINDOW_DAYS` | `28` | 오늘을 포함해 감시할 날짜 수 |
| `APP_TIMEZONE` | `Asia/Seoul` | 날짜 계산 기준 시간대 |
| `STRICT_IMAX_MATCH` | `true` | IMAX 이름 또는 코드가 있는 회차만 감지 |
| `MAX_WORKERS` | `4` | 날짜별 조회 동시 작업 수 |

### 개발 및 테스트

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
