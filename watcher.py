#!/usr/bin/env python3
"""CGV IMAX schedule watcher with Telegram notifications.

This project intentionally uses only Python's standard library.  CGV login
credentials are neither accepted nor sent.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import getpass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import ssl
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request


APP_NAME = "CGV Telegram Watcher"
DEFAULT_API_URL = "https://cgv.co.kr/api/v1/booking/searchSchByMov"
DEFAULT_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/movie"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


class ConfigurationError(ValueError):
    """Raised when .env is incomplete or invalid."""


class FetchError(RuntimeError):
    """A safe-to-display CGV fetch error."""


class TelegramError(RuntimeError):
    """A safe-to-display Telegram API error."""


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigurationError(f"{name} 값은 true 또는 false여야 합니다.")


def _parse_int(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 값은 정수여야 합니다.") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(
            f"{name} 값은 {minimum}~{maximum} 범위여야 합니다."
        )
    return parsed


def _parse_date(value: str, *, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 값은 YYYY-MM-DD 형식이어야 합니다.") from exc


def load_dotenv(path: Path) -> dict[str, str]:
    """Read a small, dependency-free subset of the dotenv format."""
    if not path.exists():
        raise ConfigurationError(
            f"설정 파일이 없습니다: {path}\n"
            "먼저 setup.command를 실행하거나 .env.example을 .env로 복사하세요."
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(
                f"{path.name} {line_number}번째 줄에 '='가 없습니다."
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigurationError(
                f"{path.name} {line_number}번째 줄의 설정 이름이 올바르지 않습니다."
            )
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = (
                    value.replace(r"\n", "\n")
                    .replace(r"\t", "\t")
                    .replace(r'\"', '"')
                    .replace(r"\\", "\\")
                )
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


@dataclasses.dataclass(frozen=True)
class Config:
    project_dir: Path
    telegram_bot_token: str
    telegram_chat_id: str
    api_url: str
    booking_url: str
    company_code: str
    site_no: str
    movie_no: str
    movie_label: str
    rtctl_scope_code: str
    target_start: dt.date
    target_end: dt.date
    poll_interval_seconds: int
    request_timeout_seconds: int
    max_workers: int
    imax_keywords: tuple[str, ...]
    imax_code_values: tuple[str, ...]
    strict_imax_match: bool
    error_alert_cooldown_seconds: int
    state_file: Path
    log_file: Path

    @classmethod
    def from_env_file(
        cls, path: Path, *, allow_missing_telegram: bool = False
    ) -> "Config":
        path = path.expanduser().resolve()
        if path.exists():
            file_values = load_dotenv(path)
        elif os.environ.get("TELEGRAM_BOT_TOKEN") or allow_missing_telegram:
            # Railway and similar hosts inject settings as environment variables;
            # they should never require a committed .env file.
            file_values = {}
        else:
            raise ConfigurationError(
                f"설정 파일이 없습니다: {path}\n"
                "로컬에서는 setup.command를 실행하고, Railway에서는 Variables를 설정하세요."
            )

        def value(name: str, default: str = "") -> str:
            return os.environ.get(name, file_values.get(name, default)).strip()

        token = value("TELEGRAM_BOT_TOKEN")
        chat_id = value("TELEGRAM_CHAT_ID")
        placeholders = {"", "여기에_봇_토큰", "여기에_채팅_ID", "YOUR_BOT_TOKEN", "YOUR_CHAT_ID"}
        if not allow_missing_telegram and token in placeholders:
            raise ConfigurationError("TELEGRAM_BOT_TOKEN을 .env에 입력하세요.")
        if not allow_missing_telegram and chat_id in placeholders:
            raise ConfigurationError("TELEGRAM_CHAT_ID를 .env에 입력하세요.")

        project_dir = path.parent
        target_start = _parse_date(
            value("TARGET_START_DATE", "2026-08-26"), name="TARGET_START_DATE"
        )
        target_end = _parse_date(
            value("TARGET_END_DATE", "2026-09-08"), name="TARGET_END_DATE"
        )
        if target_end < target_start:
            raise ConfigurationError("TARGET_END_DATE는 시작일보다 빠를 수 없습니다.")
        if (target_end - target_start).days > 62:
            raise ConfigurationError("조회 날짜 범위는 최대 63일까지 지원합니다.")

        keywords = tuple(
            item.strip() for item in value("IMAX_KEYWORDS", "IMAX,아이맥스").split(",")
            if item.strip()
        )
        if not keywords:
            raise ConfigurationError("IMAX_KEYWORDS에 한 개 이상의 값을 입력하세요.")

        code_values = tuple(
            item.strip().upper()
            for item in value("IMAX_CODE_VALUES", "08").split(",")
            if item.strip()
        )

        volume_mount = value("RAILWAY_VOLUME_MOUNT_PATH")
        state_dir_value = value("STATE_DIR", volume_mount)
        log_dir_value = value("LOG_DIR", volume_mount)

        def resolved_path(raw: str, fallback: Path) -> Path:
            candidate = Path(raw).expanduser() if raw else fallback
            if not candidate.is_absolute():
                candidate = project_dir / candidate
            return candidate.resolve()

        state_dir = resolved_path(state_dir_value, project_dir / "data")
        log_dir = resolved_path(log_dir_value, project_dir / "logs")
        state_file = resolved_path(
            value("STATE_FILE"), state_dir / "notified.json"
        )
        log_file = resolved_path(value("LOG_FILE"), log_dir / "watcher.log")

        return cls(
            project_dir=project_dir,
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            api_url=value("CGV_API_URL", DEFAULT_API_URL),
            booking_url=value("CGV_BOOKING_URL", DEFAULT_BOOKING_URL),
            company_code=value("CGV_COMPANY_CODE", "A420"),
            site_no=value("CGV_SITE_NO", "0013"),
            movie_no=value("CGV_MOVIE_NO", "30001323"),
            movie_label=value("MOVIE_LABEL", "오디세이"),
            rtctl_scope_code=value("CGV_RTCTL_SCOPE_CODE", "08"),
            target_start=target_start,
            target_end=target_end,
            poll_interval_seconds=_parse_int(
                value("POLL_INTERVAL_SECONDS", "60"),
                name="POLL_INTERVAL_SECONDS",
                minimum=30,
                maximum=86400,
            ),
            request_timeout_seconds=_parse_int(
                value("REQUEST_TIMEOUT_SECONDS", "15"),
                name="REQUEST_TIMEOUT_SECONDS",
                minimum=5,
                maximum=60,
            ),
            max_workers=_parse_int(
                value("MAX_WORKERS", "4"),
                name="MAX_WORKERS",
                minimum=1,
                maximum=8,
            ),
            imax_keywords=keywords,
            imax_code_values=code_values,
            strict_imax_match=_parse_bool(
                value("STRICT_IMAX_MATCH", "true"), name="STRICT_IMAX_MATCH"
            ),
            error_alert_cooldown_seconds=_parse_int(
                value("ERROR_ALERT_COOLDOWN_SECONDS", "21600"),
                name="ERROR_ALERT_COOLDOWN_SECONDS",
                minimum=300,
                maximum=604800,
            ),
            state_file=state_file,
            log_file=log_file,
        )

    def target_dates(self) -> list[dt.date]:
        count = (self.target_end - self.target_start).days + 1
        return [self.target_start + dt.timedelta(days=offset) for offset in range(count)]


@dataclasses.dataclass(frozen=True, order=True)
class BookingSession:
    date: str
    start_time: str
    end_time: str = ""
    screen_name: str = ""
    format_name: str = "IMAX"
    schedule_id: str = ""

    def notification_key(self, *, site_no: str, movie_no: str) -> str:
        # The user-visible uniqueness requirement is a show date and start time.
        return f"{site_no}:{movie_no}:{self.date}:{self.start_time}"


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", str(value).lower())


DATE_KEYS = {
    "scnymd",
    "playymd",
    "playdate",
    "showdate",
    "screeningdate",
    "date",
}
START_TIME_KEYS = {
    "scnfrtm",
    "scnstarttm",
    "scnsttm",
    "starttm",
    "starttime",
    "playstarttm",
    "playstarttime",
    "showtime",
}
END_TIME_KEYS = {
    "scntotm",
    "scnendtm",
    "endtm",
    "endtime",
    "playendtm",
    "playendtime",
}
SCREEN_NAME_KEYS = {
    "scnsnm",
    "scnrmnm",
    "scnnm",
    "screenname",
    "screenroomname",
    "auditoriumnm",
    "auditoriumname",
    "hallnm",
    "hallname",
    "theaternm",
    "spclscnsnm",
    "specialscreenname",
}
FORMAT_NAME_KEYS = {
    "formatnm",
    "formatname",
    "filmtypenm",
    "playkindnm",
    "scnstypenm",
    "screentypename",
    "spclscnsnm",
    "specialscreenname",
}
SCHEDULE_ID_KEYS = {
    "schno",
    "schseq",
    "scheduleno",
    "scheduleid",
    "scnno",
    "scnseq",
    "scnsno",
    "playseq",
    "playno",
}
IMAX_CODE_KEYS = {
    "rtctlscopcd",
    "spclscncd",
    "spclscnsdivcd",
    "specialscreencode",
    "screenformatcd",
    "screentypecd",
    "scnstypecd",
}


def _scalar_pairs(mapping: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for key, value in mapping.items():
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        rendered = str(value).strip()
        if rendered:
            pairs.append((str(key), rendered[:300]))
    return tuple(pairs)


def _walk_mappings(
    value: Any,
    ancestors: tuple[tuple[str, str], ...] = (),
) -> Iterator[tuple[Mapping[str, Any], tuple[tuple[str, str], ...]]]:
    if isinstance(value, Mapping):
        local_pairs = _scalar_pairs(value)
        context = (ancestors + local_pairs)[-120:]
        yield value, context
        for child in value.values():
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk_mappings(child, context)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_mappings(child, ancestors)


def _direct_value(mapping: Mapping[str, Any], candidate_keys: set[str]) -> Any:
    for key, value in mapping.items():
        if _normalized_key(key) in candidate_keys and value is not None and value != "":
            return value
    return None


def _context_value(
    mapping: Mapping[str, Any],
    context: Sequence[tuple[str, str]],
    candidate_keys: set[str],
) -> str:
    direct = _direct_value(mapping, candidate_keys)
    if direct is not None and direct != "":
        return str(direct).strip()
    for key, value in reversed(context):
        if _normalized_key(key) in candidate_keys and value:
            return value.strip()
    return ""


def _normalize_date(value: Any, fallback: dt.date) -> str:
    text = str(value or "").strip()
    compact_match = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", text)
    if compact_match:
        candidate = "-".join(compact_match.groups())
        try:
            return dt.date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass
    dashed_match = re.search(r"(?<!\d)(20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)", text)
    if dashed_match:
        year, month, day = (int(part) for part in dashed_match.groups())
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            pass
    return fallback.isoformat()


def _normalize_time(value: Any) -> str:
    text = str(value or "").strip()
    colon_match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", text)
    if colon_match:
        return f"{int(colon_match.group(1)):02d}:{colon_match.group(2)}"
    compact_match = re.fullmatch(r"([01]\d|2[0-3])([0-5]\d)(?:[0-5]\d)?", text)
    if compact_match:
        return f"{compact_match.group(1)}:{compact_match.group(2)}"
    embedded_match = re.search(r"(?:T|\s)([01]\d|2[0-3])([0-5]\d)(?:[0-5]\d)?", text)
    if embedded_match:
        return f"{embedded_match.group(1)}:{embedded_match.group(2)}"
    return ""


def _imax_match(
    context: Sequence[tuple[str, str]],
    *,
    keywords: Sequence[str],
    code_values: Sequence[str],
) -> tuple[bool, str]:
    joined_values = " | ".join(value for _, value in context)
    upper_values = joined_values.upper()
    for keyword in keywords:
        if keyword.upper() in upper_values:
            return True, keyword

    code_set = {item.upper() for item in code_values}
    for key, value in context:
        if _normalized_key(key) in IMAX_CODE_KEYS and value.strip().upper() in code_set:
            return True, f"IMAX 코드 {value.strip()}"
    return False, ""


def extract_sessions(
    payload: Any,
    *,
    requested_date: dt.date,
    keywords: Sequence[str] = ("IMAX", "아이맥스"),
    code_values: Sequence[str] = ("08",),
    strict_imax_match: bool = True,
) -> list[BookingSession]:
    """Extract showtimes defensively from CGV's nested JSON response."""
    sessions: dict[tuple[str, str], BookingSession] = {}

    for mapping, context in _walk_mappings(payload):
        raw_start = _direct_value(mapping, START_TIME_KEYS)
        start_time = _normalize_time(raw_start)
        if not start_time:
            continue

        is_imax, matched_format = _imax_match(
            context, keywords=keywords, code_values=code_values
        )
        if strict_imax_match and not is_imax:
            continue

        raw_date = _direct_value(mapping, DATE_KEYS)
        show_date = _normalize_date(raw_date, requested_date)
        end_time = _normalize_time(_direct_value(mapping, END_TIME_KEYS))
        screen_name = _context_value(mapping, context, SCREEN_NAME_KEYS)
        format_name = _context_value(mapping, context, FORMAT_NAME_KEYS)
        if not format_name:
            format_name = matched_format or "IMAX 후보"
        schedule_id = _context_value(mapping, context, SCHEDULE_ID_KEYS)

        session = BookingSession(
            date=show_date,
            start_time=start_time,
            end_time=end_time,
            screen_name=screen_name,
            format_name=format_name,
            schedule_id=schedule_id,
        )
        # Yongsan has one IMAX screen.  Date + start time avoids duplicate
        # alerts when the same session appears in multiple response branches.
        identity = (session.date, session.start_time)
        existing = sessions.get(identity)
        if existing is None or len(str(dataclasses.asdict(session))) > len(
            str(dataclasses.asdict(existing))
        ):
            sessions[identity] = session

    return sorted(sessions.values())


class CgvClient:
    """Anonymous CGV schedule client; no login headers or cookie jar."""

    def __init__(self, config: Config):
        self.config = config
        self.ssl_context = ssl.create_default_context()

    def fetch_date(self, show_date: dt.date) -> Any:
        query = urllib.parse.urlencode(
            {
                "coCd": self.config.company_code,
                "siteNo": self.config.site_no,
                "scnYmd": show_date.strftime("%Y%m%d"),
                "movNo": self.config.movie_no,
                "rtctlScopCd": self.config.rtctl_scope_code,
            }
        )
        url = f"{self.config.api_url}?{query}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Accept-Language": "ko-KR",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": self.config.booking_url,
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.config.request_timeout_seconds,
                context=self.ssl_context,
            ) as response:
                status = getattr(response, "status", 200)
                body = response.read(5_000_000)
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            body = exc.read(30_000).decode("utf-8", errors="replace")
            if exc.code == 403 and (
                "비정상적으로 CGV에 접속" in body
                or "cloudflare" in body.lower()
            ):
                raise FetchError(
                    "CGV가 자동 조회를 차단했습니다(HTTP 403). "
                    "잠시 후 다시 시도하거나 네트워크를 바꿔 보세요."
                ) from exc
            raise FetchError(f"CGV 응답 오류: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            reason_text = str(reason or exc)
            reason_text = re.sub(r"https?://\S+", "CGV 주소", reason_text)
            raise FetchError(f"CGV 연결 실패: {reason_text[:180]}") from exc

        if status != 200:
            raise FetchError(f"CGV 응답 오류: HTTP {status}")
        if "json" not in content_type.lower() and body.lstrip().startswith(b"<"):
            raise FetchError("CGV가 JSON 대신 웹 차단/오류 페이지를 반환했습니다.")
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError("CGV 응답을 JSON으로 해석할 수 없습니다.") from exc


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, *, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context()

    def send_message(self, text: str) -> None:
        endpoint = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": APP_NAME},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.ssl_context
            ) as response:
                body = response.read(1_000_000)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                parsed = json.loads(exc.read(100_000).decode("utf-8"))
                detail = str(parsed.get("description", ""))[:180]
            except Exception:
                pass
            suffix = f" - {detail}" if detail else ""
            raise TelegramError(f"Telegram 응답 오류: HTTP {exc.code}{suffix}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = str(getattr(exc, "reason", None) or exc)
            reason = reason.replace(self.bot_token, "[숨김]")
            raise TelegramError(f"Telegram 연결 실패: {reason[:180]}") from exc

        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramError("Telegram 응답을 해석할 수 없습니다.") from exc
        if not parsed.get("ok"):
            description = str(parsed.get("description", "알 수 없는 오류"))[:180]
            raise TelegramError(f"Telegram 전송 실패: {description}")


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "version": 1,
            "notified": {},
            "last_error_fingerprint": "",
            "last_error_notified_at": "",
        }

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"중복 방지 상태 파일을 읽을 수 없습니다: {self.path}"
            ) from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("notified", {}), dict):
            raise RuntimeError(f"중복 방지 상태 파일 형식이 올바르지 않습니다: {self.path}")
        self.data.update(loaded)

    def was_notified(self, key: str) -> bool:
        return key in self.data["notified"]

    def mark_notified(self, key: str, session: BookingSession) -> None:
        self.data["notified"][key] = {
            "notified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "date": session.date,
            "start_time": session.start_time,
            "screen_name": session.screen_name,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        rendered = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def should_notify_error(self, fingerprint: str, cooldown_seconds: int) -> bool:
        if fingerprint != self.data.get("last_error_fingerprint", ""):
            return True
        raw_last = self.data.get("last_error_notified_at", "")
        try:
            last = dt.datetime.fromisoformat(raw_last)
        except (TypeError, ValueError):
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        elapsed = dt.datetime.now(dt.timezone.utc) - last
        return elapsed.total_seconds() >= cooldown_seconds

    def mark_error_notified(self, fingerprint: str) -> None:
        self.data["last_error_fingerprint"] = fingerprint
        self.data["last_error_notified_at"] = dt.datetime.now(
            dt.timezone.utc
        ).isoformat()

    def clear_error(self) -> bool:
        if not self.data.get("last_error_fingerprint"):
            return False
        self.data["last_error_fingerprint"] = ""
        self.data["last_error_notified_at"] = ""
        return True


def _session_line(session: BookingSession) -> str:
    details: list[str] = []
    if session.screen_name:
        details.append(session.screen_name)
    if session.end_time:
        details.append(f"종료 {session.end_time}")
    suffix = f" — {' / '.join(details)}" if details else ""
    return f"• {session.date} {session.start_time}{suffix}"


def message_chunks(
    sessions: Sequence[BookingSession], config: Config, *, max_chars: int = 3500
) -> list[tuple[str, list[BookingSession]]]:
    header = (
        "🎟️ CGV 예매 오픈 감지\n"
        f"영화: {config.movie_label} ({config.movie_no})\n"
        f"극장: 용산아이파크몰 ({config.site_no})\n\n"
    )
    footer = f"\n\n예매: {config.booking_url}"
    chunks: list[tuple[str, list[BookingSession]]] = []
    current_lines: list[str] = []
    current_sessions: list[BookingSession] = []

    for session in sessions:
        line = _session_line(session)
        candidate = header + "\n".join(current_lines + [line]) + footer
        if current_lines and len(candidate) > max_chars:
            chunks.append(
                (header + "\n".join(current_lines) + footer, list(current_sessions))
            )
            current_lines = []
            current_sessions = []
        current_lines.append(line)
        current_sessions.append(session)

    if current_lines:
        chunks.append((header + "\n".join(current_lines) + footer, current_sessions))
    return chunks


@dataclasses.dataclass(frozen=True)
class CycleResult:
    successful_dates: int
    failed_dates: int
    matching_sessions: int
    new_sessions: int


class Watcher:
    def __init__(
        self,
        config: Config,
        *,
        logger: logging.Logger,
        dry_run: bool = False,
    ):
        self.config = config
        self.logger = logger
        self.dry_run = dry_run
        self.cgv = CgvClient(config)
        self.telegram = TelegramClient(
            config.telegram_bot_token,
            config.telegram_chat_id,
            timeout=config.request_timeout_seconds,
        )
        self.state = StateStore(config.state_file)
        self.state.load()

    def run_cycle(self) -> CycleResult:
        dates = self.config.target_dates()
        payloads: dict[dt.date, Any] = {}
        errors: dict[dt.date, str] = {}

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.config.max_workers, len(dates))
        ) as executor:
            pending = {
                executor.submit(self.cgv.fetch_date, show_date): show_date
                for show_date in dates
            }
            for future in concurrent.futures.as_completed(pending):
                show_date = pending[future]
                try:
                    payloads[show_date] = future.result()
                except FetchError as exc:
                    errors[show_date] = str(exc)
                except Exception as exc:  # Keep a single malformed date from stopping the watcher.
                    errors[show_date] = f"예상하지 못한 조회 오류: {type(exc).__name__}"

        session_map: dict[tuple[str, str], BookingSession] = {}
        for show_date, payload in payloads.items():
            for session in extract_sessions(
                payload,
                requested_date=show_date,
                keywords=self.config.imax_keywords,
                code_values=self.config.imax_code_values,
                strict_imax_match=self.config.strict_imax_match,
            ):
                session_map[(session.date, session.start_time)] = session

        sessions = sorted(session_map.values())
        new_sessions = [
            session
            for session in sessions
            if not self.state.was_notified(
                session.notification_key(
                    site_no=self.config.site_no, movie_no=self.config.movie_no
                )
            )
        ]

        state_changed = False
        if new_sessions:
            if self.dry_run:
                self.logger.info("드라이런: 새 회차 %d개(메시지/상태 저장 생략)", len(new_sessions))
                for session in new_sessions:
                    self.logger.info("드라이런 회차: %s", _session_line(session))
            else:
                for text, chunk_sessions in message_chunks(new_sessions, self.config):
                    try:
                        self.telegram.send_message(text)
                    except TelegramError as exc:
                        self.logger.error("%s", exc)
                        break
                    for session in chunk_sessions:
                        key = session.notification_key(
                            site_no=self.config.site_no, movie_no=self.config.movie_no
                        )
                        self.state.mark_notified(key, session)
                        state_changed = True
                    self.logger.info("Telegram 신규 회차 알림 %d개 전송", len(chunk_sessions))

        if errors:
            unique_errors = sorted(set(errors.values()))
            fingerprint_source = "\n".join(unique_errors)
            fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
            sample_dates = ", ".join(
                show_date.isoformat() for show_date in sorted(errors)[:4]
            )
            self.logger.warning(
                "CGV 조회 오류 %d/%d일 (%s): %s",
                len(errors),
                len(dates),
                sample_dates,
                unique_errors[0],
            )
            if (
                not self.dry_run
                and self.state.should_notify_error(
                    fingerprint, self.config.error_alert_cooldown_seconds
                )
            ):
                status_word = "전체" if len(errors) == len(dates) else "일부"
                error_text = (
                    "⚠️ CGV 감시 조회 오류\n"
                    f"{status_word} 날짜 조회에 실패했습니다 ({len(errors)}/{len(dates)}일).\n"
                    f"원인: {unique_errors[0]}\n"
                    "감시기는 계속 실행되며 다음 주기에 다시 시도합니다."
                )
                try:
                    self.telegram.send_message(error_text)
                except TelegramError as exc:
                    self.logger.error("오류 알림도 전송하지 못했습니다: %s", exc)
                else:
                    self.state.mark_error_notified(fingerprint)
                    state_changed = True
        elif self.state.clear_error():
            state_changed = True

        if state_changed:
            self.state.save()

        self.logger.info(
            "조회 완료: 성공 %d일, 오류 %d일, IMAX 회차 %d개, 신규 %d개",
            len(payloads),
            len(errors),
            len(sessions),
            len(new_sessions),
        )
        return CycleResult(
            successful_dates=len(payloads),
            failed_dates=len(errors),
            matching_sessions=len(sessions),
            new_sessions=len(new_sessions),
        )


def configure_logging(config: Config, *, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("cgv_watcher")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def find_chat_ids() -> int:
    print("먼저 Telegram에서 만든 봇에게 /start 메시지를 보내세요.")
    token = getpass.getpass("Bot Token (화면에 표시되지 않음): ").strip()
    if not token:
        print("Bot Token이 비어 있습니다.", file=sys.stderr)
        return 2
    endpoint = f"https://api.telegram.org/bot{token}/getUpdates"
    request = urllib.request.Request(
        endpoint, headers={"Accept": "application/json", "User-Agent": APP_NAME}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=15, context=ssl.create_default_context()
        ) as response:
            payload = json.loads(response.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"Telegram 응답 오류: HTTP {exc.code}", file=sys.stderr)
        return 2
    except Exception as exc:
        safe_error = str(exc).replace(token, "[숨김]")
        print(f"조회 실패: {safe_error}", file=sys.stderr)
        return 2

    if not payload.get("ok"):
        print(
            f"Telegram 오류: {payload.get('description', '알 수 없는 오류')}",
            file=sys.stderr,
        )
        return 2

    chats: dict[str, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            chat = value.get("chat")
            if isinstance(chat, dict) and "id" in chat:
                chats[str(chat["id"])] = chat
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload.get("result", []))
    if not chats:
        print("chat_id를 찾지 못했습니다. 봇에게 /start를 보낸 뒤 다시 실행하세요.")
        return 1

    print("\n찾은 chat_id:")
    for chat_id, chat in chats.items():
        label = chat.get("title") or chat.get("username") or chat.get("first_name") or "이름 없음"
        print(f"  {chat_id}  ({chat.get('type', 'unknown')}: {label})")
    print("\n사용할 숫자를 .env의 TELEGRAM_CHAT_ID에 입력하세요.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CGV 용산 IMAX 예매 오픈 감시기")
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parent / ".env"),
        help=".env 파일 경로",
    )
    parser.add_argument("--once", action="store_true", help="한 번만 조회하고 종료")
    parser.add_argument(
        "--dry-run", action="store_true", help="Telegram 전송과 상태 저장 없이 조회"
    )
    parser.add_argument(
        "--test-telegram", action="store_true", help="Telegram 테스트 메시지 전송"
    )
    parser.add_argument(
        "--find-chat-id", action="store_true", help="Bot Token으로 chat_id 찾기"
    )
    parser.add_argument("--verbose", action="store_true", help="상세 로그")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.find_chat_id:
        return find_chat_ids()

    try:
        config = Config.from_env_file(
            Path(args.env_file), allow_missing_telegram=args.dry_run
        )
    except ConfigurationError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    logger = configure_logging(config, verbose=args.verbose)
    if args.test_telegram:
        client = TelegramClient(
            config.telegram_bot_token,
            config.telegram_chat_id,
            timeout=config.request_timeout_seconds,
        )
        try:
            client.send_message(
                "✅ CGV 감시기 Telegram 연결 테스트 성공\n"
                f"대상: {config.movie_label} / 용산아이파크몰 IMAX"
            )
        except TelegramError as exc:
            logger.error("%s", exc)
            return 2
        logger.info("Telegram 테스트 메시지를 전송했습니다.")
        return 0

    try:
        watcher = Watcher(config, logger=logger, dry_run=args.dry_run)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    logger.info(
        "%s 시작: siteNo=%s, movNo=%s, %s~%s, %d초 간격",
        APP_NAME,
        config.site_no,
        config.movie_no,
        config.target_start,
        config.target_end,
        config.poll_interval_seconds,
    )
    logger.info("CGV 로그인 토큰과 로그인 쿠키는 사용하지 않습니다.")

    if args.once:
        result = watcher.run_cycle()
        return 0 if result.successful_dates else 3

    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stop_requested:
        if dt.date.today() > config.target_end:
            logger.info("감시 대상 마지막 날짜가 지나 정상 종료합니다.")
            return 0
        started = time.monotonic()
        watcher.run_cycle()
        elapsed = time.monotonic() - started
        sleep_seconds = max(0.5, config.poll_interval_seconds - elapsed)
        deadline = time.monotonic() + sleep_seconds
        while not stop_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    logger.info("사용자 요청으로 감시기를 종료합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
