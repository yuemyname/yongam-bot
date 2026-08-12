# CGV 용산 IMAX 예매 오픈 Telegram 알리미

CGV 공개 상영일정·좌석 조회 API를 **1분마다** 확인합니다. 아래 조건의 IMAX 회차가 나타나면 Telegram으로 날짜와 시작시간을 즉시 알리고, 이후 잔여 좌석 수가 바뀔 때도 알려줍니다.

- 극장: CGV 용산아이파크몰 (`siteNo=0013`)
- 영화: 오디세이 (`movNo=30001323`)
- 날짜: `2026-08-26`~`2026-09-08`
- 포맷: 응답의 `IMAX`/`아이맥스` 표기 또는 특수관 코드 `08`
- 좌석 변경: 전체 잔여 수 또는 일반 예매 가능 좌석 수가 바뀔 때 알림
- 제외 조건: 예매 가능한 좌석이 장애인석뿐이라고 공개 좌석표로 확인된 경우 알림하지 않음
- CGV 로그인 토큰, `accessToken`, 쿠키: **사용하지 않음**

알림을 보낸 회차와 마지막 좌석 수는 로컬의 `data/notified.json` 또는 Railway 영구 볼륨에 저장됩니다. 프로그램을 다시 시작해도 같은 예매 오픈이나 같은 좌석 수를 중복 알림하지 않습니다.

> 중요: 이 도구는 CGV가 웹 예매 화면에서 사용하는 비로그인 조회 주소를 이용합니다. CGV가 API 형식이나 접속 제한 정책을 바꾸면 조회가 실패할 수 있습니다. 이때 프로그램은 이를 “아직 미오픈”으로 처리하지 않고 Telegram 오류 알림과 `logs/watcher.log`에 남긴 뒤 계속 재시도합니다. 1분 간격은 접속 제한 위험이 있으므로 더 짧게 설정하지 마세요.

## Railway와 Vercel 중 무엇이 적합한가요?

이 프로젝트에는 **Railway를 권장합니다.**

| 항목 | Railway | Vercel |
|---|---|---|
| 실행 방식 | 계속 실행되는 백그라운드 서비스 | 요청이 올 때만 실행되는 함수/Cron |
| 1분 감시 | 프로그램 내부에서 바로 가능 | 1분 Cron은 Pro 이상 필요 |
| 중복 알림 상태 | `/data` 영구 볼륨에 저장 | 별도 데이터베이스/KV 필요 |
| 중복 실행 | 단일 서비스로 단순하게 관리 | Cron 중복·겹침에 대비한 잠금 필요 |
| 이 프로젝트와의 적합성 | **권장** | 비권장 |

Railway는 장기 실행 백그라운드 작업과 [영구 볼륨](https://docs.railway.com/volumes)을 지원합니다. 반면 Vercel Hobby의 Cron은 공식 제한상 하루 한 번만 실행할 수 있고, 1분 Cron은 Pro 이상에서 가능합니다. 자세한 내용은 [Vercel Cron 사용량 문서](https://vercel.com/docs/cron-jobs/usage-and-pricing)를 참고하세요.

## Railway에 배포하기

저장소: [yuemyname/yongam-bot](https://github.com/yuemyname/yongam-bot)

1. [Railway](https://railway.com/)에 로그인합니다.
2. **New Project → Deploy from GitHub repo**를 선택합니다.
3. `yuemyname/yongam-bot` 저장소를 선택합니다.
4. 생성된 서비스에 Volume을 추가하고 Mount Path를 `/data`로 설정합니다.
5. 서비스의 **Variables** 탭에 아래 두 값을 추가합니다.

```dotenv
TELEGRAM_BOT_TOKEN=BotFather가_준_실제_토큰
TELEGRAM_CHAT_ID=find_chat_id에서_확인한_숫자
```

6. 변경사항을 배포합니다. 로그에 `CGV Telegram Watcher 시작`이 나타나면 실행 중입니다.

`railway.toml`이 Python 실행 명령, 단일 인스턴스, 오류 시 재시작, `/data` 볼륨 요구사항을 설정합니다. Railway가 제공하는 `RAILWAY_VOLUME_MOUNT_PATH`를 프로그램이 자동 감지하므로 `STATE_DIR`을 따로 설정할 필요는 없습니다. 이 프로그램은 백그라운드 작업이므로 공개 도메인이나 웹 포트도 필요하지 않습니다.

처음 GitHub 저장소를 연결하면 Variables와 Volume을 만들기 전에 첫 배포가 실패할 수 있습니다. 두 설정을 추가한 뒤 다시 배포하면 됩니다. `.env` 파일이나 실제 Bot Token은 GitHub에 올리지 마세요.

> Railway 같은 클라우드 서버 IP도 CGV의 HTTP 403 차단을 받을 수 있습니다. 배포 후 반드시 로그를 확인하세요. 차단이 지속되면 `POLL_INTERVAL_SECONDS=120`으로 늘리거나 다른 실행 환경이 필요할 수 있습니다.

---

아래 내용은 Mac에서 직접 실행할 때의 안내입니다.

## Mac 1. 준비물

- macOS
- 인터넷 연결
- Python 3.10 이상
- Telegram 계정

Terminal에서 다음 명령으로 Python을 확인할 수 있습니다.

```bash
python3 --version
```

명령을 찾을 수 없다면 [Python macOS 다운로드](https://www.python.org/downloads/macos/)에서 Python 3를 설치하세요. 별도 Python 패키지 설치는 필요 없습니다.

## Mac 2. BotFather로 Telegram 봇 만들기

1. Telegram에서 공식 계정 **@BotFather**를 검색합니다.
2. 채팅에서 `/newbot`을 보냅니다.
3. 안내에 따라 봇 이름과 `bot`으로 끝나는 사용자 이름을 정합니다.
4. BotFather가 보내 준 **HTTP API Token**을 복사합니다. 이 값은 비밀번호처럼 다루고 다른 사람에게 보내지 마세요.
5. 방금 만든 봇의 채팅을 열어 **시작**을 누르거나 `/start`를 보냅니다.

## Mac 3. chat_id 확인하기

가장 쉬운 방법은 다음과 같습니다.

1. 만든 봇에게 `/start`를 보냅니다.
2. 이 폴더의 `find_chat_id.command`를 더블 클릭합니다.
3. Bot Token을 붙여 넣고 Enter를 누릅니다. 입력 중에는 토큰이 화면에 표시되지 않습니다.
4. 출력된 숫자를 복사합니다. 개인 채팅은 보통 양수, 그룹 채팅은 보통 음수입니다.

결과가 없다면 봇에게 메시지를 한 번 더 보낸 후 다시 실행하세요. 그룹으로 알림을 받으려면 봇을 그룹에 추가하고 그룹에서 메시지를 보낸 뒤 그룹의 음수 `chat_id`를 사용합니다.

직접 확인하려면 Bot API의 `getUpdates` 응답 안에서 `"chat":{"id": ...}` 값을 찾아도 됩니다.

## Mac 4. 설정하기

1. `setup.command`를 더블 클릭합니다.
2. 자동으로 열린 `.env` 파일에서 아래 두 줄을 실제 값으로 바꿉니다.
3. TextEdit에서 저장합니다.

```dotenv
TELEGRAM_BOT_TOKEN=123456789:실제_봇_토큰
TELEGRAM_CHAT_ID=123456789
```

나머지 대상 값은 이미 요청한 조건으로 설정되어 있습니다. `.env`는 ZIP과 Git에서 제외되며, 토큰은 코드나 로그에 기록되지 않습니다.

macOS에서 “확인되지 않은 개발자” 경고가 나오면 파일을 Control-클릭 → **열기**를 선택하세요.

## Mac 5. Telegram 연결 시험

`test_telegram.command`를 더블 클릭합니다. Telegram에 다음과 같은 테스트 메시지가 오면 설정이 올바릅니다.

```text
✅ CGV 감시기 Telegram 연결 테스트 성공
대상: 오디세이 / 용산아이파크몰 IMAX
```

## Mac 6. 실행하기

### 간단 실행

`run.command`를 더블 클릭합니다. 열린 Terminal 창을 유지하는 동안 1분마다 확인합니다. 중단하려면 Terminal 창에서 `Control+C`를 누릅니다.

Mac이 잠자기 상태이거나 꺼져 있거나 인터넷 연결이 없으면 확인할 수 없습니다. 덮개를 닫은 노트북도 보통 잠자기 상태가 됩니다.

### Mac 로그인 후 백그라운드 자동 실행

설정과 Telegram 테스트를 마친 후 `install_background.command`를 더블 클릭합니다.

- 설치 즉시 백그라운드 감시 시작
- Mac 로그인 시 자동 시작
- 실행 로그: `logs/watcher.log`
- 대상 마지막 날(`2026-09-08`)이 지나면 정상 종료

백그라운드 실행을 중단하고 자동 시작을 제거하려면 `uninstall_background.command`를 더블 클릭합니다. 자동 시작 설정은 바로 삭제하지 않고 Mac의 휴지통으로 옮깁니다. `.env`, 알림 기록, 로그는 그대로 남습니다.

## 알림 예시

```text
🎟️ CGV 예매 오픈 감지
영화: 오디세이 (30001323)
극장: 용산아이파크몰 (0013)

• 2026-08-26 14:30 — IMAX관 / 종료 17:10
• 2026-08-26 18:00 — IMAX관 / 종료 20:40 / 잔여 142석

예매: https://cgv.co.kr/cnm/movieBook/movie
```

한 번에 여러 회차가 열리면 한 메시지에 묶어 보냅니다. Telegram 전송이 성공한 회차만 중복 방지 파일에 기록하므로, 전송 실패 때문에 알림이 영구 누락되지 않습니다.

좌석 수가 바뀌면 다음과 같이 별도 메시지가 옵니다.

```text
💺 CGV 잔여 좌석 변경
영화: 오디세이 (30001323)
극장: 용산아이파크몰 (0013)

• 2026-08-26 18:00 — IMAX관 / 종료 20:40 / 잔여 141석
전체 잔여: 142석 → 141석
일반 예매 가능: 140석 → 139석
장애인석: 2석 (장애인석만 남으면 알림 제외)
```

CGV 좌석표에서 예매 가능 상태(`seatStusCd=00`)를 세고, CGV가 장애인석으로 표시하는 코드(`seatSalfrmCd=04`)를 따로 분리합니다. 좌석표 응답과 상영일정의 전체 잔여 수가 정확히 일치할 때만 “장애인석만 남음”으로 판단하므로, 좌석 상세 응답이 불완전할 때 일반 좌석 알림을 잘못 숨기지 않습니다.

## 설정값 설명

| 이름 | 기본값 | 설명 |
|---|---:|---|
| `POLL_INTERVAL_SECONDS` | `60` | 조회 주기(초), 최소 30초 |
| `REQUEST_TIMEOUT_SECONDS` | `15` | 날짜별 CGV 요청 제한시간 |
| `MAX_WORKERS` | `4` | 14개 날짜를 나눠 조회하는 동시 요청 수 |
| `STRICT_IMAX_MATCH` | `true` | `IMAX` 이름/코드가 있는 회차만 알림 |
| `ERROR_ALERT_COOLDOWN_SECONDS` | `21600` | 같은 CGV 오류 알림 재전송 간격(6시간) |
| `CGV_SEAT_API_URL` | 공개 좌석 조회 주소 | 보통 수정하지 않음 |

CGV가 일정 응답에서 포맷 이름과 특수관 코드를 모두 생략해 실제 IMAX 회차가 누락되는 경우에만 `STRICT_IMAX_MATCH=false`로 바꾸세요. 이 설정은 API가 반환한 해당 영화 회차 전체를 IMAX 후보로 취급하므로 일반관 회차가 함께 알림될 수 있습니다.

## Terminal에서 직접 확인하기

프로젝트 폴더에서 다음 명령을 사용할 수 있습니다.

```bash
# 한 번 조회하고 실제 신규 회차가 있으면 알림
python3 watcher.py --once

# Telegram 전송과 중복 방지 저장 없이 한 번 조회
python3 watcher.py --once --dry-run

# Telegram 연결 시험
python3 watcher.py --test-telegram

# 단위 테스트
python3 -m unittest discover -s tests -v
```

`--dry-run`은 `.env`의 Telegram 값이 비어 있어도 실행됩니다.

## 문제 해결

### Telegram 메시지가 오지 않음

- 봇과의 채팅에서 `/start`를 먼저 보냈는지 확인하세요.
- `test_telegram.command`를 실행하세요.
- `.env`의 Token과 Chat ID 앞뒤에 공백이 없는지 확인하세요.
- 그룹 Chat ID는 음수일 수 있습니다.

### CGV 조회 오류 또는 HTTP 403

CGV의 자동 접속 차단입니다. 프로그램 오류가 아니라 CGV 측 제한일 수 있습니다.

- 잠시 기다린 후 자동 재시도를 확인합니다.
- VPN/프록시를 끄거나 다른 네트워크를 사용해 봅니다.
- `POLL_INTERVAL_SECONDS`를 `120` 이상으로 늘리면 제한 위험을 줄일 수 있습니다.
- `logs/watcher.log`에서 최근 오류를 확인합니다.

CGV 오류가 난 날짜는 “미오픈”으로 확정하지 않으며, 다음 주기에 다시 확인합니다.

좌석 상세 조회만 실패하면 전체 잔여 수 변경 감시는 계속됩니다. 이때는 장애인석만 남았는지 확정할 수 없어 해당 필터를 적용하지 않으며, Railway 또는 `logs/watcher.log`에 `좌석 상세 조회 오류`가 표시됩니다.

### 같은 회차를 다시 알리고 싶음

감시기를 먼저 중지한 뒤 `data/notified.json`을 별도 위치에 백업하고, 파일 안의 해당 회차 항목만 제거하세요. 파일 전체를 지우면 이미 열린 모든 회차를 다시 알릴 수 있습니다.

## 개인정보와 보안

- CGV 로그인 정보, 로그인 토큰, 로그인 쿠키를 요구하거나 전송하지 않습니다.
- Telegram Bot Token은 로컬 `.env` 또는 Railway 비밀 환경변수에서만 읽습니다.
- `.env`는 로그와 중복 방지 파일에 복사되지 않습니다.
- Bot Token이 노출되었다면 BotFather의 `/revoke`로 즉시 폐기하고 새 토큰을 발급하세요.
