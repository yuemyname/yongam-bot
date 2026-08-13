#!/usr/bin/env python3
"""CGV IMAX schedule watcher with Telegram notifications.

This project intentionally uses only Python's standard library.  CGV login
credentials are neither accepted nor sent.
"""

from __future__ import annotations

import argparse
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
import threading
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_NAME = "CGV Telegram Watcher"
DEFAULT_API_URL = "https://cgv.co.kr/api/v1/booking/searchSchByMov"
DEFAULT_SEAT_API_URL = "https://cgv.co.kr/api/v1/booking/searchIfSeatData"
DEFAULT_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/movie"
DEFAULT_SEAT_PAGE_URL = "https://cgv.co.kr/cnm/selectVisitorCnt"
DEFAULT_SITE_NAME = "용산아이파크몰"
UNCLASSIFIED_ALERT_MIN_SEATS = 7
STATE_VERSION = 6

# Scanning strategy.  "full" requests every date in the window each cycle.
# "cursor" requests only the already-open range plus a short probe past the
# frontier, which is where a new booking opening can first appear.
SCAN_MODE_FULL = "full"
SCAN_MODE_CURSOR = "cursor"
SCAN_MODES = (SCAN_MODE_FULL, SCAN_MODE_CURSOR)
DEFAULT_SCAN_MODE = SCAN_MODE_FULL

# Alert categories a broadcast can belong to.  "system" messages (fetch error
# notices) always reach every subscriber regardless of their preference.
ALERT_OPEN = "open"
ALERT_SEATS = "seats"
# A seat-change alert CGV's detail response could not classify.  It reaches the
# seat-alert subscribers who did not ask for verified seat information only.
ALERT_SEATS_UNCLASSIFIED = "seats_unclassified"
ALERT_SYSTEM = "system"

# Per-subscriber alert preference.  Subscribers stored before this feature have
# no saved mode and fall back to DEFAULT_ALERT_MODE, preserving old behaviour.
ALERT_MODE_ALL = "all"
ALERT_MODE_OPEN_ONLY = "open"
ALERT_MODE_SEATS_ONLY = "seats"
DEFAULT_ALERT_MODE = ALERT_MODE_ALL
ALERT_MODES: dict[str, frozenset[str]] = {
    ALERT_MODE_ALL: frozenset({ALERT_OPEN, ALERT_SEATS}),
    ALERT_MODE_OPEN_ONLY: frozenset({ALERT_OPEN}),
    ALERT_MODE_SEATS_ONLY: frozenset({ALERT_SEATS}),
}
ALERT_MODE_LABELS = {
    ALERT_MODE_ALL: "신규 오픈 + 잔여 좌석",
    ALERT_MODE_OPEN_ONLY: "신규 오픈만",
    ALERT_MODE_SEATS_ONLY: "잔여 좌석만",
}
# One-tap commands matter more than typing arguments on a phone keyboard, so
# each mode gets its own command as well as a "/mode <value>" argument form.
MODE_COMMAND_TARGETS = {
    "/mode_all": ALERT_MODE_ALL,
    "/mode_open": ALERT_MODE_OPEN_ONLY,
    "/mode_seats": ALERT_MODE_SEATS_ONLY,
}
MODE_COMMANDS = {"/mode", "/alert", *MODE_COMMAND_TARGETS}
ALERT_MODE_ALIASES = {
    "all": ALERT_MODE_ALL,
    "both": ALERT_MODE_ALL,
    "전체": ALERT_MODE_ALL,
    "모두": ALERT_MODE_ALL,
    "open": ALERT_MODE_OPEN_ONLY,
    "오픈": ALERT_MODE_OPEN_ONLY,
    "예매": ALERT_MODE_OPEN_ONLY,
    "seat": ALERT_MODE_SEATS_ONLY,
    "seats": ALERT_MODE_SEATS_ONLY,
    "좌석": ALERT_MODE_SEATS_ONLY,
    "잔여": ALERT_MODE_SEATS_ONLY,
}
MODE_GUIDE = (
    "알림 종류를 고를 수 있습니다.\n"
    "/mode_all - 신규 오픈 + 잔여 좌석 (기본)\n"
    "/mode_open - 신규 오픈만\n"
    "/mode_seats - 잔여 좌석만"
)

# Whether a subscriber accepts seat-change alerts CGV could not classify.
# Stored per subscriber; absent means "accept them", matching old behaviour.
SEAT_INFO_COMMAND_TARGETS = {
    "/seat_verified": True,
    "/seat_all": False,
}
SEAT_INFO_COMMANDS = {"/seat", "/seatinfo", *SEAT_INFO_COMMAND_TARGETS}
SEAT_INFO_ALIASES = {
    "verified": True,
    "확인": True,
    "일반": True,
    "all": False,
    "전체": False,
    "미확인": False,
}
SEAT_INFO_LABELS = {
    True: "일반 좌석이 확인된 알림만",
    False: "좌석 종류 미확인 알림도 포함",
}
# Operator-only. Deliberately left out of /help and the BotFather command list.
ADMIN_STATS_COMMANDS = {"/stats", "/subscribers"}
SEAT_INFO_GUIDE = (
    "잔여 좌석 알림의 범위를 고를 수 있습니다.\n"
    "/seat_all - 좌석 종류 미확인 알림도 받기 (기본)\n"
    "/seat_verified - 일반 좌석이 확인된 알림만 받기\n\n"
    "CGV 좌석 상세 조회가 실패하면 좌석 종류를 확인할 수 없어 "
    "'좌석 종류 미확인' 표시로 알림이 갑니다. "
    "/seat_verified를 고르면 이런 알림은 받지 않습니다."
)

# Keep one extra day so a subscriber in a different timezone never loses the
# de-duplication record for a show that is still "today" for them.
STATE_RETENTION_DAYS = 1
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


def _parse_scan_mode(value: str) -> str:
    mode = str(value).strip().lower()
    if mode not in SCAN_MODES:
        raise ConfigurationError(
            f"SCAN_MODE 값은 {' 또는 '.join(SCAN_MODES)} 여야 합니다."
        )
    return mode


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
    seat_api_url: str
    booking_url: str
    company_code: str
    site_no: str
    movie_no: str
    movie_label: str
    rtctl_scope_code: str
    dynamic_date_window: bool
    target_window_days: int
    timezone_name: str
    target_start: dt.date
    target_end: dt.date
    poll_interval_seconds: int
    telegram_command_poll_seconds: int
    cgv_request_spacing_seconds: int
    rate_limit_backoff_initial_seconds: int
    rate_limit_backoff_max_seconds: int
    request_timeout_seconds: int
    imax_keywords: tuple[str, ...]
    imax_code_values: tuple[str, ...]
    strict_imax_match: bool
    subscriptions_enabled: bool
    scan_mode: str
    booking_close_margin_minutes: int
    cursor_probe_days: int
    cursor_expansion_days: int
    full_scan_every_cycles: int
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
        timezone_name = value("APP_TIMEZONE", "Asia/Seoul")
        try:
            local_today = dt.datetime.now(ZoneInfo(timezone_name)).date()
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(
                f"APP_TIMEZONE을 찾을 수 없습니다: {timezone_name}"
            ) from exc

        dynamic_date_window = _parse_bool(
            value("DYNAMIC_DATE_WINDOW", "true"), name="DYNAMIC_DATE_WINDOW"
        )
        target_window_days = _parse_int(
            value("TARGET_WINDOW_DAYS", "28"),
            name="TARGET_WINDOW_DAYS",
            minimum=1,
            maximum=63,
        )
        if dynamic_date_window:
            target_start = local_today
            target_end = local_today + dt.timedelta(days=target_window_days - 1)
        else:
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
            seat_api_url=value("CGV_SEAT_API_URL", DEFAULT_SEAT_API_URL),
            booking_url=value("CGV_BOOKING_URL", DEFAULT_BOOKING_URL),
            company_code=value("CGV_COMPANY_CODE", "A420"),
            site_no=value("CGV_SITE_NO", "0013"),
            movie_no=value("CGV_MOVIE_NO", "30001323"),
            movie_label=value("MOVIE_LABEL", "오디세이"),
            rtctl_scope_code=value("CGV_RTCTL_SCOPE_CODE", "08"),
            dynamic_date_window=dynamic_date_window,
            target_window_days=target_window_days,
            timezone_name=timezone_name,
            target_start=target_start,
            target_end=target_end,
            poll_interval_seconds=_parse_int(
                value("POLL_INTERVAL_SECONDS", "60"),
                name="POLL_INTERVAL_SECONDS",
                minimum=30,
                maximum=86400,
            ),
            telegram_command_poll_seconds=_parse_int(
                value("TELEGRAM_COMMAND_POLL_SECONDS", "2"),
                name="TELEGRAM_COMMAND_POLL_SECONDS",
                minimum=1,
                maximum=60,
            ),
            cgv_request_spacing_seconds=_parse_int(
                value("CGV_REQUEST_SPACING_SECONDS", "2"),
                name="CGV_REQUEST_SPACING_SECONDS",
                minimum=0,
                maximum=60,
            ),
            rate_limit_backoff_initial_seconds=_parse_int(
                value("RATE_LIMIT_BACKOFF_INITIAL_SECONDS", "1800"),
                name="RATE_LIMIT_BACKOFF_INITIAL_SECONDS",
                minimum=60,
                maximum=86400,
            ),
            rate_limit_backoff_max_seconds=_parse_int(
                value("RATE_LIMIT_BACKOFF_MAX_SECONDS", "7200"),
                name="RATE_LIMIT_BACKOFF_MAX_SECONDS",
                minimum=60,
                maximum=86400,
            ),
            request_timeout_seconds=_parse_int(
                value("REQUEST_TIMEOUT_SECONDS", "15"),
                name="REQUEST_TIMEOUT_SECONDS",
                minimum=5,
                maximum=60,
            ),
            imax_keywords=keywords,
            imax_code_values=code_values,
            strict_imax_match=_parse_bool(
                value("STRICT_IMAX_MATCH", "true"), name="STRICT_IMAX_MATCH"
            ),
            subscriptions_enabled=_parse_bool(
                value("SUBSCRIPTIONS_ENABLED", "true"),
                name="SUBSCRIPTIONS_ENABLED",
            ),
            scan_mode=_parse_scan_mode(value("SCAN_MODE", DEFAULT_SCAN_MODE)),
            booking_close_margin_minutes=_parse_int(
                value("BOOKING_CLOSE_MARGIN_MINUTES", "0"),
                name="BOOKING_CLOSE_MARGIN_MINUTES",
                minimum=0,
                maximum=240,
            ),
            cursor_probe_days=_parse_int(
                value("CURSOR_PROBE_DAYS", "3"),
                name="CURSOR_PROBE_DAYS",
                minimum=1,
                maximum=28,
            ),
            cursor_expansion_days=_parse_int(
                value("CURSOR_EXPANSION_DAYS", "21"),
                name="CURSOR_EXPANSION_DAYS",
                minimum=1,
                maximum=60,
            ),
            full_scan_every_cycles=_parse_int(
                value("FULL_SCAN_EVERY_CYCLES", "10"),
                name="FULL_SCAN_EVERY_CYCLES",
                minimum=1,
                maximum=1440,
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

    def local_now(self) -> dt.datetime:
        return dt.datetime.now(ZoneInfo(self.timezone_name))

    def local_today(self) -> dt.date:
        return self.local_now().date()

    def target_range(self, *, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
        if not self.dynamic_date_window:
            return self.target_start, self.target_end
        start = today or self.local_today()
        return start, start + dt.timedelta(days=self.target_window_days - 1)

    def target_dates(self, *, today: dt.date | None = None) -> list[dt.date]:
        start, end = self.target_range(today=today)
        count = (end - start).days + 1
        return [start + dt.timedelta(days=offset) for offset in range(count)]


@dataclasses.dataclass(frozen=True, order=True)
class BookingSession:
    date: str
    start_time: str
    end_time: str = ""
    screen_name: str = ""
    format_name: str = "IMAX"
    schedule_id: str = ""
    screen_no: str = ""
    screen_sequence: str = ""
    remaining_seats: int | None = None
    total_seats: int | None = None

    def notification_key(self, *, site_no: str, movie_no: str) -> str:
        # The user-visible uniqueness requirement is a show date and start time.
        return f"{site_no}:{movie_no}:{self.date}:{self.start_time}"

    def start_datetime(self, timezone_name: str) -> dt.datetime | None:
        """Local start time, or None when the schedule had no usable time."""

        try:
            day = dt.date.fromisoformat(self.date)
            hour, minute = (int(part) for part in self.start_time.split(":", 1))
            return dt.datetime(
                day.year,
                day.month,
                day.day,
                hour,
                minute,
                tzinfo=ZoneInfo(timezone_name),
            )
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            return None


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
    "scnsrttm",
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
    "exposcnsnm",
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
    "exposcnsnm",
    "movknddsplnm",
}
SCHEDULE_ID_KEYS = {
    "schno",
    "schseq",
    "scheduleno",
    "scheduleid",
    "scnno",
    "scnseq",
    "scnsseq",
    "scnsno",
    "playseq",
    "playno",
}
SCREEN_NO_KEYS = {"scnsno"}
SCREEN_SEQUENCE_KEYS = {"scnsseq"}
REMAINING_SEAT_KEYS = {
    "frseatcnt",
    "remainingseatcnt",
    "remainseatcnt",
    "availableseatcnt",
}
TOTAL_SEAT_KEYS = {
    "stcnt",
    "totalseatcnt",
    "seatcapacity",
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


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"-?\d+", text):
        return None
    parsed = int(text)
    return parsed if parsed >= 0 else None


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

    def completeness(session: BookingSession) -> int:
        text_fields = (
            session.end_time,
            session.screen_name,
            session.format_name,
            session.schedule_id,
            session.screen_no,
            session.screen_sequence,
        )
        return sum(bool(value) for value in text_fields) + (
            2 if session.remaining_seats is not None else 0
        ) + (1 if session.total_seats is not None else 0)

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
        screen_no = _context_value(mapping, context, SCREEN_NO_KEYS)
        screen_sequence = _context_value(mapping, context, SCREEN_SEQUENCE_KEYS)
        remaining_seats = _nonnegative_int(
            _direct_value(mapping, REMAINING_SEAT_KEYS)
        )
        total_seats = _nonnegative_int(_direct_value(mapping, TOTAL_SEAT_KEYS))

        session = BookingSession(
            date=show_date,
            start_time=start_time,
            end_time=end_time,
            screen_name=screen_name,
            format_name=format_name,
            schedule_id=schedule_id,
            screen_no=screen_no,
            screen_sequence=screen_sequence,
            remaining_seats=remaining_seats,
            total_seats=total_seats,
        )
        # Yongsan has one IMAX screen.  Date + start time avoids duplicate
        # alerts when the same session appears in multiple response branches.
        identity = (session.date, session.start_time)
        existing = sessions.get(identity)
        if existing is None or completeness(session) > completeness(existing):
            sessions[identity] = session

    return sorted(sessions.values())


@dataclasses.dataclass(frozen=True)
class SeatSnapshot:
    """Seat totals observed for one screening."""

    total: int
    general: int | None = None
    accessible: int | None = None
    mapped_total: int | None = None
    available_rows: tuple[str, ...] | None = None

    @property
    def seat_map_complete(self) -> bool:
        """Whether the detail response can safely classify an alert.

        Counts must agree with the schedule, and row labels are required when
        at least one general seat remains so A-row-only availability can be
        excluded without guessing.
        """
        return (
            self.total > 0
            and self.general is not None
            and self.accessible is not None
            and self.mapped_total == self.total
            and (self.general == 0 or self.available_rows is not None)
        )

    @property
    def accessible_only(self) -> bool:
        # Suppress only when the seat-map count exactly agrees with the total
        # shown in CGV's schedule.  A partial/changed response must not hide an
        # alert for a normal seat.
        return (
            self.general == 0
            and self.accessible is not None
            and self.accessible > 0
            and self.mapped_total == self.total
        )

    @property
    def row_a_only(self) -> bool:
        # As with accessible-only detection, require a complete seat map before
        # suppressing an alert. Missing row labels or a count mismatch leave the
        # alert enabled so an incomplete response cannot hide a useful seat.
        return (
            self.total > 0
            and self.mapped_total == self.total
            and self.available_rows == ("A",)
        )

    @property
    def suppression_reason(self) -> str | None:
        if self.accessible_only:
            return "잔여 좌석이 장애인석뿐입니다."
        if self.row_a_only:
            return "잔여 좌석이 모두 A열입니다."
        return None

    @property
    def should_suppress(self) -> bool:
        return self.suppression_reason is not None

    @property
    def alertable(self) -> bool:
        """Whether this snapshot may trigger an alert.

        A complete seat map is preferred.  When CGV does not return enough
        detail to classify seat types, the schedule total is used only from
        seven remaining seats upward.
        """
        return (
            self.total > 0
            and not self.should_suppress
            and (
                self.seat_map_complete
                or self.total >= UNCLASSIFIED_ALERT_MIN_SEATS
            )
        )

    @property
    def uses_unclassified_fallback(self) -> bool:
        return self.alertable and not self.seat_map_complete


def _normalize_seat_row(value: Any) -> str:
    row = str(value or "").strip().upper()
    return re.sub(r"\s*열$", "", row).strip()


def extract_seat_snapshot(payload: Any, *, scheduled_remaining: int) -> SeatSnapshot:
    """Count anonymously viewable seats using CGV's own seat-map codes.

    CGV's booking UI treats ``seatStusCd=00`` as available and
    ``seatSalfrmCd=04`` as an accessible/preferential seat.
    """

    seats: dict[str, Mapping[str, Any]] = {}
    for mapping, _context in _walk_mappings(payload):
        status = _direct_value(mapping, {"seatstuscd"})
        seat_location = _direct_value(mapping, {"seatlocno"})
        if status is None or seat_location is None or seat_location == "":
            continue
        identity_parts = (
            seat_location,
            _direct_value(mapping, {"seatareano"}),
            _direct_value(mapping, {"seatrownm"}),
            _direct_value(mapping, {"seatno"}),
        )
        identity = ":".join(str(part or "") for part in identity_parts)
        seats[identity] = mapping

    if not seats:
        return SeatSnapshot(total=scheduled_remaining)

    general = 0
    accessible = 0
    available_rows: set[str] = set()
    row_labels_complete = True
    for seat in seats.values():
        if str(_direct_value(seat, {"seatstuscd"}) or "").strip() != "00":
            continue
        if str(_direct_value(seat, {"seatsaleyn"}) or "Y").strip().upper() == "N":
            continue
        disabled = str(_direct_value(seat, {"isdisabled"}) or "").strip().lower()
        if disabled in {"1", "true", "y", "yes"}:
            continue
        seat_row = _normalize_seat_row(_direct_value(seat, {"seatrownm"}))
        if seat_row:
            available_rows.add(seat_row)
        else:
            row_labels_complete = False
        if str(_direct_value(seat, {"seatsalfrmcd"}) or "").strip() == "04":
            accessible += 1
        else:
            general += 1

    mapped_total = general + accessible
    return SeatSnapshot(
        total=scheduled_remaining,
        general=general,
        accessible=accessible,
        mapped_total=mapped_total,
        available_rows=(
            tuple(sorted(available_rows))
            if row_labels_complete and mapped_total > 0
            else None
        ),
    )


class CgvClient:
    """Anonymous CGV schedule client; no login headers or cookie jar."""

    def __init__(self, config: Config):
        self.config = config
        self.ssl_context = ssl.create_default_context()

    def _get_json(self, url: str, *, referer: str) -> Any:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Accept-Language": "ko-KR",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": referer,
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
        return self._get_json(url, referer=self.config.booking_url)

    def fetch_seat_snapshot(self, session: BookingSession) -> SeatSnapshot:
        if session.remaining_seats is None:
            raise FetchError("상영 일정 응답에 잔여 좌석 수가 없습니다.")
        if not session.screen_no or not session.screen_sequence:
            return SeatSnapshot(total=session.remaining_seats)

        query = urllib.parse.urlencode(
            {
                "coCd": self.config.company_code,
                "siteNo": self.config.site_no,
                "scnYmd": session.date.replace("-", ""),
                "scnsNo": session.screen_no,
                "scnSseq": session.screen_sequence,
                "seatAreaNo": "",
                "cusgdCd": "",
                "custNo": "",
            }
        )
        payload = self._get_json(
            f"{self.config.seat_api_url}?{query}", referer=DEFAULT_SEAT_PAGE_URL
        )
        return extract_seat_snapshot(
            payload, scheduled_remaining=session.remaining_seats
        )


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, *, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context()

    def send_message(self, text: str, *, chat_id: str | None = None) -> None:
        endpoint = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": chat_id or self.chat_id,
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

    def get_updates(self, *, offset: int) -> list[Mapping[str, Any]]:
        endpoint = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        payload = json.dumps(
            {
                "offset": offset,
                "timeout": 0,
                "allowed_updates": ["message"],
            }
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
                body = response.read(2_000_000)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                parsed_error = json.loads(exc.read(100_000).decode("utf-8"))
                detail = str(parsed_error.get("description", ""))[:180]
            except Exception:
                pass
            suffix = f" - {detail}" if detail else ""
            raise TelegramError(
                f"Telegram 구독 명령 조회 오류: HTTP {exc.code}{suffix}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = str(getattr(exc, "reason", None) or exc)
            reason = reason.replace(self.bot_token, "[숨김]")
            raise TelegramError(f"Telegram 구독 명령 연결 실패: {reason[:180]}") from exc

        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramError("Telegram 구독 명령 응답을 해석할 수 없습니다.") from exc
        if not parsed.get("ok"):
            description = str(parsed.get("description", "알 수 없는 오류"))[:180]
            raise TelegramError(f"Telegram 구독 명령 조회 실패: {description}")
        updates = parsed.get("result", [])
        if not isinstance(updates, list):
            raise TelegramError("Telegram 구독 명령 응답 형식이 올바르지 않습니다.")
        return [item for item in updates if isinstance(item, Mapping)]


def _state_key_date(key: str, record: Any) -> dt.date | None:
    """Recover the show date for a stored record, or None when unknown."""

    candidates: list[str] = []
    if isinstance(record, Mapping):
        stored = record.get("date")
        if isinstance(stored, str):
            candidates.append(stored)
    parts = str(key).split(":")
    if len(parts) >= 3:
        candidates.append(parts[2])
    for candidate in candidates:
        try:
            return dt.date.fromisoformat(candidate.strip())
        except (TypeError, ValueError):
            continue
    return None


def _expired_state_key(key: str, record: Any, cutoff: dt.date) -> bool:
    show_date = _state_key_date(key, record)
    return show_date is not None and show_date < cutoff


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {
            "version": STATE_VERSION,
            "notified": {},
            "seat_counts": {},
            "subscribers": {},
            "subscribers_initialized": False,
            "telegram_update_offset": 0,
            "frontier_date": "",
            "last_error_fingerprint": "",
            "last_error_notified_at": "",
        }

    def load(self) -> None:
        with self._lock:
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
            if not isinstance(loaded.get("seat_counts", {}), dict):
                raise RuntimeError(f"좌석 수 상태 파일 형식이 올바르지 않습니다: {self.path}")
            if not isinstance(loaded.get("subscribers", {}), dict):
                raise RuntimeError(f"구독자 상태 파일 형식이 올바르지 않습니다: {self.path}")
            self.data.update(loaded)
            self.data["version"] = STATE_VERSION
            self.data.setdefault("seat_counts", {})
            self.data.setdefault("subscribers", {})
            self.data.setdefault("subscribers_initialized", False)
            self.data.setdefault("telegram_update_offset", 0)
            self.data.setdefault("frontier_date", "")

    def was_notified(self, key: str) -> bool:
        with self._lock:
            return key in self.data["notified"]

    def mark_notified(self, key: str, session: BookingSession) -> None:
        with self._lock:
            self.data["notified"][key] = {
                "notified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "date": session.date,
                "start_time": session.start_time,
                "screen_name": session.screen_name,
                "remaining_seats": session.remaining_seats,
                "total_seats": session.total_seats,
            }

    def prune_expired(self, today: dt.date) -> int:
        """Drop notification and seat records for shows that already played.

        Keys look like ``siteNo:movNo:YYYY-MM-DD:HH:MM`` so the show date is the
        third colon-separated field.  Anything unparseable is kept: an unknown
        key is far cheaper than a duplicate alert.
        """

        cutoff = today - dt.timedelta(days=STATE_RETENTION_DAYS)
        removed = 0
        with self._lock:
            for bucket in ("notified", "seat_counts"):
                records = self.data.get(bucket)
                if not isinstance(records, dict):
                    continue
                for key in [
                    key
                    for key in records
                    if _expired_state_key(key, records.get(key), cutoff)
                ]:
                    del records[key]
                    removed += 1
        return removed

    @property
    def subscribers_initialized(self) -> bool:
        with self._lock:
            return bool(self.data.get("subscribers_initialized", False))

    def initialize_subscribers(self, initial_chat_id: str) -> None:
        with self._lock:
            if initial_chat_id:
                self.add_subscriber(initial_chat_id, label="초기 관리자")
            self.data["subscribers_initialized"] = True

    def subscriber_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(str(chat_id) for chat_id in self.data["subscribers"])

    def is_subscribed(self, chat_id: str) -> bool:
        with self._lock:
            return str(chat_id) in self.data["subscribers"]

    def alert_mode(self, chat_id: str) -> str:
        """Return a subscriber's alert preference, defaulting to everything."""

        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return DEFAULT_ALERT_MODE
            mode = record.get("alert_mode")
            if isinstance(mode, str) and mode in ALERT_MODES:
                return mode
            return DEFAULT_ALERT_MODE

    def set_alert_mode(self, chat_id: str, mode: str) -> bool:
        """Store a new preference; returns False when it was already set."""

        if mode not in ALERT_MODES:
            raise ValueError(f"알 수 없는 알림 모드: {mode}")
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return False
            if record.get("alert_mode", DEFAULT_ALERT_MODE) == mode:
                return False
            updated = dict(record)
            updated["alert_mode"] = mode
            self.data["subscribers"][str(chat_id)] = updated
            return True

    def verified_seats_only(self, chat_id: str) -> bool:
        """Whether this subscriber declined unclassified seat-change alerts."""

        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return False
            return bool(record.get("verified_seats_only", False))

    def set_verified_seats_only(self, chat_id: str, value: bool) -> bool:
        """Store the seat-detail preference; returns False when unchanged."""

        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return False
            if bool(record.get("verified_seats_only", False)) is bool(value):
                return False
            updated = dict(record)
            updated["verified_seats_only"] = bool(value)
            self.data["subscribers"][str(chat_id)] = updated
            return True

    def subscriber_breakdown(self) -> dict[str, Any]:
        """Counts of who is subscribed and how they configured their alerts."""

        with self._lock:
            modes = {mode: 0 for mode in ALERT_MODES}
            chat_types: dict[str, int] = {}
            verified = 0
            for chat_id, record in self.data["subscribers"].items():
                modes[self.alert_mode(chat_id)] += 1
                if self.verified_seats_only(chat_id):
                    verified += 1
                kind = ""
                if isinstance(record, Mapping):
                    kind = str(record.get("chat_type") or "")
                chat_types[kind or "unknown"] = chat_types.get(kind or "unknown", 0) + 1
            return {
                "total": len(self.data["subscribers"]),
                "modes": modes,
                "verified_seats_only": verified,
                "chat_types": chat_types,
            }

    def subscriber_ids_for(self, category: str) -> tuple[str, ...]:
        """Subscribers who opted in to this alert category."""

        if category == ALERT_SYSTEM:
            return self.subscriber_ids()
        if category == ALERT_SEATS_UNCLASSIFIED:
            return tuple(
                chat_id
                for chat_id in self.subscriber_ids_for(ALERT_SEATS)
                if not self.verified_seats_only(chat_id)
            )
        with self._lock:
            return tuple(
                str(chat_id)
                for chat_id in self.data["subscribers"]
                if category in ALERT_MODES[self.alert_mode(chat_id)]
            )

    def add_subscriber(
        self, chat_id: str, *, label: str = "", chat_type: str = ""
    ) -> bool:
        with self._lock:
            key = str(chat_id)
            if key in self.data["subscribers"]:
                return False
            self.data["subscribers"][key] = {
                "subscribed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "label": label[:100],
                "chat_type": chat_type[:30],
                "alert_mode": DEFAULT_ALERT_MODE,
                "verified_seats_only": False,
            }
            return True

    def remove_subscriber(self, chat_id: str) -> bool:
        with self._lock:
            return self.data["subscribers"].pop(str(chat_id), None) is not None

    @property
    def telegram_update_offset(self) -> int:
        with self._lock:
            value = self.data.get("telegram_update_offset", 0)
            return value if isinstance(value, int) and value >= 0 else 0

    def set_telegram_update_offset(self, offset: int) -> None:
        with self._lock:
            self.data["telegram_update_offset"] = max(0, int(offset))

    @property
    def frontier_date(self) -> dt.date | None:
        """Latest show date an IMAX session was ever observed on."""

        with self._lock:
            raw = self.data.get("frontier_date", "")
            if not isinstance(raw, str) or not raw:
                return None
            try:
                return dt.date.fromisoformat(raw)
            except ValueError:
                return None

    def advance_frontier(self, observed: dt.date) -> bool:
        """Move the frontier forward only.

        A cycle cut short by HTTP 429 sees fewer dates than it asked for, so
        letting the frontier fall back to that partial maximum would make the
        watcher re-probe ground it already covered — and, worse, treat already
        known sessions as new.  Only advancing keeps a failed scan harmless.
        """

        with self._lock:
            current = self.frontier_date
            if current is not None and observed <= current:
                return False
            self.data["frontier_date"] = observed.isoformat()
            return True

    def seat_snapshot(self, key: str) -> SeatSnapshot | None:
        with self._lock:
            raw = self.data["seat_counts"].get(key)
            if not isinstance(raw, Mapping):
                return None
            total = _nonnegative_int(raw.get("total"))
            if total is None:
                return None
            raw_rows = raw.get("available_rows")
            available_rows = None
            if isinstance(raw_rows, list) and all(
                isinstance(row, str) for row in raw_rows
            ):
                available_rows = tuple(raw_rows)
            return SeatSnapshot(
                total=total,
                general=_nonnegative_int(raw.get("general")),
                accessible=_nonnegative_int(raw.get("accessible")),
                mapped_total=_nonnegative_int(raw.get("mapped_total")),
                available_rows=available_rows,
            )

    def set_seat_snapshot(self, key: str, snapshot: SeatSnapshot) -> None:
        with self._lock:
            self.data["seat_counts"][key] = {
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "total": snapshot.total,
                "general": snapshot.general,
                "accessible": snapshot.accessible,
                "mapped_total": snapshot.mapped_total,
                "available_rows": (
                    list(snapshot.available_rows)
                    if snapshot.available_rows is not None
                    else None
                ),
            }

    def save(self) -> None:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            self.data["last_error_fingerprint"] = fingerprint
            self.data["last_error_notified_at"] = dt.datetime.now(
                dt.timezone.utc
            ).isoformat()

    def clear_error(self) -> bool:
        with self._lock:
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
    if session.remaining_seats is not None or session.total_seats is not None:
        details.append(f"좌석 {_seat_ratio(session)}")
    suffix = f" — {' / '.join(details)}" if details else ""
    return f"• {session.date} {session.start_time}{suffix}"


def _seat_ratio(session: BookingSession, *, remaining: int | None = None) -> str:
    remaining_value = session.remaining_seats if remaining is None else remaining
    remaining_text = str(remaining_value) if remaining_value is not None else "?"
    total_text = str(session.total_seats) if session.total_seats is not None else "?"
    return f"{remaining_text}/{total_text}석"


def _alert_session_line(
    session: BookingSession, *, seat_detail_unclassified: bool = False
) -> str:
    line = f"• 상영 시작시간 {session.start_time} — {_seat_ratio(session)}"
    if seat_detail_unclassified:
        line += " ⚠️ 좌석 종류 미확인 · 전체 잔여 수 기준"
    return line


def _alert_date_banner(date_text: str) -> str:
    weekdays = ("월", "화", "수", "목", "금", "토", "일")
    try:
        parsed = dt.date.fromisoformat(date_text)
    except ValueError:
        display_date = date_text
    else:
        display_date = f"{date_text} ({weekdays[parsed.weekday()]})"
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 상영일: {display_date}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def booking_url_for_session(session: BookingSession, config: Config) -> str:
    """Build a CGV booking URL with movie, theater, and date preselected."""

    split = urllib.parse.urlsplit(config.booking_url)
    query = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
    query.update(
        {
            "coCd": config.company_code,
            "siteNo": config.site_no,
            "siteNm": DEFAULT_SITE_NAME,
            "movNo": config.movie_no,
            "scnYmd": session.date.replace("-", ""),
        }
    )
    return urllib.parse.urlunsplit(
        (
            split.scheme,
            split.netloc,
            split.path,
            urllib.parse.urlencode(query),
            split.fragment,
        )
    )


def _booking_footer(
    sessions: Sequence[BookingSession], config: Config
) -> str:
    links: dict[str, str] = {}
    for session in sessions:
        links.setdefault(session.date, booking_url_for_session(session, config))
    if len(links) == 1:
        lines = [f"예매 바로가기: {next(iter(links.values()))}"]
    else:
        lines = [
            f"예매 바로가기 ({show_date}): {url}"
            for show_date, url in links.items()
        ]
    return "\n\n" + "\n".join(lines)


def _seat_snapshot_changed(previous: SeatSnapshot, current: SeatSnapshot) -> bool:
    if previous.total != current.total:
        return True
    if (
        previous.general is not None
        and current.general is not None
        and previous.general != current.general
    ):
        return True
    return False


def seat_change_message(
    session: BookingSession,
    previous: SeatSnapshot,
    current: SeatSnapshot,
    config: Config,
) -> str:
    lines = [
        "💺 CGV 잔여 좌석 변경",
        _alert_date_banner(session.date),
        f"영화: {config.movie_label} ({config.movie_no})",
        f"극장: 용산아이파크몰 ({config.site_no})",
        "",
        f"상영 시작시간: {session.start_time}",
        (
            "잔여좌석/총좌석: "
            f"{_seat_ratio(session, remaining=current.total)} "
            f"(이전 {_seat_ratio(session, remaining=previous.total)})"
        ),
    ]
    if previous.general is not None and current.general is not None:
        lines.append(f"일반 예매 가능: {previous.general}석 → {current.general}석")
    if current.accessible is not None:
        lines.append(f"장애인석: {current.accessible}석 (장애인석만 남으면 알림 제외)")
    if current.uses_unclassified_fallback:
        lines.append("⚠️ 좌석 종류 미확인 · 전체 잔여 수 기준 알림")
    lines.extend(["", f"예매 바로가기: {booking_url_for_session(session, config)}"])
    return "\n".join(lines)


def message_chunks(
    sessions: Sequence[BookingSession],
    config: Config,
    *,
    unclassified_keys: set[str] | None = None,
    max_chars: int = 3500,
) -> list[tuple[str, list[BookingSession]]]:
    chunks: list[tuple[str, list[BookingSession]]] = []
    unclassified_keys = unclassified_keys or set()
    sessions_by_date: dict[str, list[BookingSession]] = {}
    for session in sessions:
        sessions_by_date.setdefault(session.date, []).append(session)

    def render(date_sessions: Sequence[BookingSession]) -> str:
        show_date = date_sessions[0].date
        lines = [
            _alert_session_line(
                session,
                seat_detail_unclassified=(
                    session.notification_key(
                        site_no=config.site_no, movie_no=config.movie_no
                    )
                    in unclassified_keys
                ),
            )
            for session in date_sessions
        ]
        return (
            "🎟️ CGV 예매 오픈 감지\n"
            + _alert_date_banner(show_date)
            + "\n"
            + f"영화: {config.movie_label} ({config.movie_no})\n"
            + f"극장: 용산아이파크몰 ({config.site_no})\n\n"
            + "\n".join(lines)
            + _booking_footer(date_sessions, config)
        )

    for date_sessions in sessions_by_date.values():
        current_sessions: list[BookingSession] = []
        for session in date_sessions:
            candidate = current_sessions + [session]
            if current_sessions and len(render(candidate)) > max_chars:
                chunks.append((render(current_sessions), list(current_sessions)))
                current_sessions = []
            current_sessions.append(session)
        if current_sessions:
            chunks.append((render(current_sessions), current_sessions))
    return chunks


@dataclasses.dataclass(frozen=True)
class CycleResult:
    successful_dates: int
    failed_dates: int
    matching_sessions: int
    new_sessions: int
    seat_changes: int = 0
    suppressed_accessible_only: int = 0
    suppressed_row_a_only: int = 0
    suppressed_sold_out: int = 0
    deferred_seat_details: int = 0
    unclassified_fallback_alerts: int = 0
    seat_detail_errors: int = 0
    rate_limited_requests: int = 0
    schedule_skipped_dates: int = 0
    seat_detail_skipped: int = 0
    requested_dates: int = 0
    full_scan: bool = True
    suppressed_closed: int = 0


@dataclasses.dataclass(frozen=True)
class ScanPlan:
    """Which dates one cycle requests, and how far it may widen."""

    dates: tuple[dt.date, ...]
    full_scan: bool
    # Last already-open date. Anything past it is probe territory: a session
    # found there means a new booking opening.
    open_end: dt.date | None = None
    window_end: dt.date | None = None

    def probe_hit(self, latest_session_date: str) -> bool:
        if self.open_end is None or not latest_session_date:
            return False
        return latest_session_date > self.open_end.isoformat()


@dataclasses.dataclass
class _CycleTally:
    """Totals a cycle accumulates while walking the schedule date by date."""

    successful_dates: int = 0
    matching_sessions: int = 0
    new_sessions: int = 0
    seat_changes: int = 0
    suppressed_accessible_only: int = 0
    suppressed_row_a_only: int = 0
    suppressed_sold_out: int = 0
    suppressed_closed: int = 0
    unclassified_fallback_alerts: int = 0
    seat_detail_errors: int = 0
    seat_detail_error_sample: str = ""
    rate_limited_requests: int = 0
    schedule_skipped_dates: int = 0
    seat_detail_skipped: int = 0
    rate_limited: bool = False
    latest_session_date: str = ""
    # A key can be deferred both as a new session and as a seat change; a set
    # keeps the reported count to one per showing.
    deferred_keys: set[str] = dataclasses.field(default_factory=set)
    # One entry per showing, emitted as a single DEBUG line at the end of the
    # cycle.  Only collected when DEBUG is on, so normal runs build nothing.
    verdicts: list[str] = dataclasses.field(default_factory=list)
    dirty: bool = False


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
        self._last_cgv_request_finished_at: float | None = None
        # Cycle 0 always sweeps the full window; a cursor scan runs in between.
        self._cycle_index = 0
        self.state.load()
        if not self.state.subscribers_initialized:
            self.state.initialize_subscribers(config.telegram_chat_id)
            if not self.dry_run:
                self.state.save()

    def _plan_scan(self, today: dt.date) -> ScanPlan:
        """Choose the dates to request this cycle.

        Full scans cover the whole window.  Cursor scans cover the open range
        plus a short probe, which is contiguous because ``open_end`` is never
        older than yesterday.
        """

        window = self.config.target_dates(today=today)
        if not window or self.config.scan_mode == SCAN_MODE_FULL:
            return ScanPlan(dates=tuple(window), full_scan=True)

        frontier = self.state.frontier_date
        # No frontier yet (first run, or a wiped volume) means there is nothing
        # to probe past, so the window has to be swept once to establish one.
        if frontier is None:
            return ScanPlan(dates=tuple(window), full_scan=True)
        if self._cycle_index % self.config.full_scan_every_cycles == 0:
            return ScanPlan(dates=tuple(window), full_scan=True)

        window_end = window[-1]
        # Yesterday as the floor keeps the probe anchored to today once every
        # observed show has played.
        open_end = min(max(frontier, today - dt.timedelta(days=1)), window_end)
        probe_end = min(
            open_end + dt.timedelta(days=self.config.cursor_probe_days), window_end
        )
        return ScanPlan(
            dates=tuple(date for date in window if date <= probe_end),
            full_scan=False,
            open_end=open_end,
            window_end=window_end,
        )

    def _expansion_dates(self, plan: ScanPlan) -> list[dt.date]:
        if plan.open_end is None or plan.window_end is None or not plan.dates:
            return []
        expansion_end = min(
            plan.open_end + dt.timedelta(days=self.config.cursor_expansion_days),
            plan.window_end,
        )
        start = plan.dates[-1] + dt.timedelta(days=1)
        return [
            start + dt.timedelta(days=offset)
            for offset in range((expansion_end - start).days + 1)
        ]

    def _fetch_schedules(
        self,
        dates: Sequence[dt.date],
        payloads: dict[dt.date, Any],
        errors: dict[dt.date, str],
    ) -> tuple[bool, int]:
        """Request each date in order; returns (rate limited, dates skipped)."""

        for index, show_date in enumerate(dates):
            self._wait_for_cgv_request_slot()
            try:
                payloads[show_date] = self.cgv.fetch_date(show_date)
            except FetchError as exc:
                message = str(exc)
                errors[show_date] = message
                if "HTTP 429" in message:
                    skipped = len(dates) - index - 1
                    self.logger.warning(
                        "CGV HTTP 429 감지: 남은 일정 조회 %d일을 즉시 생략합니다.",
                        skipped,
                    )
                    return True, skipped
            except Exception as exc:  # Keep a single malformed date from stopping the watcher.
                errors[show_date] = f"예상하지 못한 조회 오류: {type(exc).__name__}"
            finally:
                self._mark_cgv_request_finished()
        return False, 0

    def _extract_sessions(
        self, payloads: Mapping[dt.date, Any]
    ) -> dict[tuple[str, str], BookingSession]:
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
        return session_map

    def _split_closed_sessions(
        self, sessions: Sequence[BookingSession]
    ) -> tuple[list[BookingSession], list[BookingSession]]:
        """Separate showings that can still be booked from ones that cannot.

        CGV keeps returning a showing after it starts, and its seat counts keep
        moving, but nobody can book it any more.  A session whose schedule had
        no usable start time is treated as bookable: guessing it closed would
        silently drop a real alert.
        """

        cutoff = self.config.local_now() + dt.timedelta(
            minutes=self.config.booking_close_margin_minutes
        )
        bookable: list[BookingSession] = []
        closed: list[BookingSession] = []
        for session in sessions:
            starts_at = session.start_datetime(self.config.timezone_name)
            if starts_at is not None and starts_at <= cutoff:
                closed.append(session)
            else:
                bookable.append(session)
        return bookable, closed

    def _advance_frontier(
        self, session_map: Mapping[tuple[str, str], BookingSession]
    ) -> bool:
        if not session_map:
            return False
        latest = max(date_text for date_text, _ in session_map)
        try:
            observed = dt.date.fromisoformat(latest)
        except ValueError:
            return False
        if not self.state.advance_frontier(observed):
            return False
        self.logger.info("예매 오픈 관측 최대 상영일: %s", observed.isoformat())
        return True

    def _wait_for_cgv_request_slot(self) -> None:
        """Keep CGV requests spaced apart instead of sending them in a burst."""

        if self._last_cgv_request_finished_at is None:
            return
        elapsed = time.monotonic() - self._last_cgv_request_finished_at
        remaining = self.config.cgv_request_spacing_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _mark_cgv_request_finished(self) -> None:
        self._last_cgv_request_finished_at = time.monotonic()

    def _broadcast_message(
        self, text: str, *, category: str = ALERT_SYSTEM
    ) -> tuple[int, int, int]:
        """Send to subscribers opted in to ``category``.

        The returned total counts only those recipients, so callers that gate
        state updates on delivery still advance when nobody wants this
        category (total == 0) instead of retrying forever.
        """

        subscriber_ids = self.state.subscriber_ids_for(category)
        delivered = 0
        failed = 0
        for chat_id in subscriber_ids:
            try:
                self.telegram.send_message(text, chat_id=chat_id)
            except TelegramError as exc:
                failed += 1
                self.logger.error("구독자 Telegram 전송 실패: %s", exc)
            else:
                delivered += 1
        if not subscriber_ids:
            if self.state.subscriber_ids():
                self.logger.info(
                    "'%s' 알림을 받는 구독자가 없어 전송을 생략합니다.", category
                )
            else:
                self.logger.warning(
                    "등록된 Telegram 구독자가 없어 알림을 전송하지 않습니다."
                )
        return delivered, failed, len(subscriber_ids)

    def _subscriber_stats_message(self) -> str:
        """Operator-facing snapshot of the subscriber list."""

        stats = self.state.subscriber_breakdown()
        total = stats["total"]
        lines = ["📊 구독 현황", f"전체 {total}명"]

        chat_labels = {
            "private": "개인",
            "group": "그룹",
            "supergroup": "그룹",
            "channel": "채널",
            "unknown": "미상",
        }
        grouped: dict[str, int] = {}
        for kind, count in stats["chat_types"].items():
            label = chat_labels.get(kind, kind)
            grouped[label] = grouped.get(label, 0) + count
        if grouped:
            lines.append(
                "채팅 유형: "
                + ", ".join(
                    f"{label} {count}명"
                    for label, count in sorted(
                        grouped.items(), key=lambda item: -item[1]
                    )
                )
            )

        lines.append("")
        lines.append("알림 종류")
        for mode in (ALERT_MODE_ALL, ALERT_MODE_OPEN_ONLY, ALERT_MODE_SEATS_ONLY):
            lines.append(f"• {ALERT_MODE_LABELS[mode]} — {stats['modes'][mode]}명")

        verified = stats["verified_seats_only"]
        lines.append("")
        lines.append("잔여 좌석 알림 범위")
        lines.append(f"• {SEAT_INFO_LABELS[False]} — {total - verified}명")
        lines.append(f"• {SEAT_INFO_LABELS[True]} — {verified}명")
        return "\n".join(lines)

    def _handle_mode_command(
        self, chat_id: str, command: str, argument: str
    ) -> tuple[str, bool]:
        """Return the reply for an alert-mode command and whether state changed."""

        requested = MODE_COMMAND_TARGETS.get(command)
        if requested is None and argument:
            requested = ALERT_MODE_ALIASES.get(argument)
            if requested is None:
                return (f"알 수 없는 알림 종류입니다.\n\n{MODE_GUIDE}", False)

        current = self.state.alert_mode(chat_id)
        if requested is None:
            if not self.state.is_subscribed(chat_id):
                return (
                    "🔕 현재 구독 중이 아닙니다. /start로 구독한 뒤 알림 종류를 "
                    f"고를 수 있습니다.\n\n{MODE_GUIDE}",
                    False,
                )
            return (
                f"🔔 현재 알림 종류: {ALERT_MODE_LABELS[current]}\n\n{MODE_GUIDE}",
                False,
            )

        if not self.state.is_subscribed(chat_id):
            return (
                "먼저 /start로 구독해주세요. 구독 후에 알림 종류를 고를 수 있습니다.",
                False,
            )

        changed = self.state.set_alert_mode(chat_id, requested)
        label = ALERT_MODE_LABELS[requested]
        # Every label ends in a consonant, so "으로" is always the right particle.
        if not changed:
            return (f"🔔 이미 '{label}'으로 설정되어 있습니다.", False)
        return (
            f"✅ 알림 종류를 '{label}'으로 변경했습니다.\n\n{MODE_GUIDE}",
            True,
        )

    def _handle_seat_info_command(
        self, chat_id: str, command: str, argument: str
    ) -> tuple[str, bool]:
        """Return the reply for a seat-detail command and whether state changed."""

        requested = SEAT_INFO_COMMAND_TARGETS.get(command)
        if requested is None and argument:
            requested = SEAT_INFO_ALIASES.get(argument)
            if requested is None:
                return (f"알 수 없는 좌석 알림 범위입니다.\n\n{SEAT_INFO_GUIDE}", False)

        if not self.state.is_subscribed(chat_id):
            return (
                "🔕 현재 구독 중이 아닙니다. /start로 구독한 뒤 설정할 수 있습니다."
                f"\n\n{SEAT_INFO_GUIDE}",
                False,
            )

        # The preference is stored either way, but it cannot take effect while
        # seat alerts themselves are switched off.
        note = ""
        if ALERT_SEATS not in ALERT_MODES[self.state.alert_mode(chat_id)]:
            note = "\n\n참고: 현재 잔여 좌석 알림을 받지 않는 설정입니다. /mode 확인"

        if requested is None:
            current = self.state.verified_seats_only(chat_id)
            return (
                f"💺 현재 잔여 좌석 알림 범위\n→ {SEAT_INFO_LABELS[current]}"
                f"{note}\n\n{SEAT_INFO_GUIDE}",
                False,
            )

        changed = self.state.set_verified_seats_only(chat_id, requested)
        label = SEAT_INFO_LABELS[requested]
        if not changed:
            return (f"💺 이미 이렇게 설정되어 있습니다.\n→ {label}{note}", False)
        return (f"✅ 잔여 좌석 알림 범위를 변경했습니다.\n→ {label}{note}", True)

    def sync_subscribers(self) -> None:
        """Apply Telegram /start and /stop commands to the persistent list."""

        if self.dry_run or not self.config.subscriptions_enabled:
            return
        try:
            updates = self.telegram.get_updates(
                offset=self.state.telegram_update_offset
            )
        except TelegramError as exc:
            self.logger.warning("%s", exc)
            return

        # Any incoming update advances the offset and so has to be saved, but
        # only an actual join or leave is worth an INFO line — otherwise every
        # /status or /help looks like the subscriber list moved.
        state_changed = False
        subscribers_changed = False
        for update in sorted(
            updates,
            key=lambda item: _nonnegative_int(item.get("update_id")) or 0,
        ):
            update_id = _nonnegative_int(update.get("update_id"))
            if update_id is not None:
                next_offset = update_id + 1
                if next_offset > self.state.telegram_update_offset:
                    self.state.set_telegram_update_offset(next_offset)
                    state_changed = True

            message = update.get("message")
            if not isinstance(message, Mapping):
                continue
            chat = message.get("chat")
            if not isinstance(chat, Mapping) or chat.get("id") is None:
                continue
            text = str(message.get("text") or "").strip()
            if not text.startswith("/"):
                continue

            chat_id = str(chat["id"])
            fields = text.split()
            command = fields[0].lower().split("@", 1)[0]
            argument = fields[1].lower() if len(fields) > 1 else ""
            label = str(
                chat.get("title")
                or chat.get("username")
                or chat.get("first_name")
                or ""
            )
            chat_type = str(chat.get("type") or "")
            reply = ""

            if command in {"/start", "/subscribe"}:
                added = self.state.add_subscriber(
                    chat_id, label=label, chat_type=chat_type
                )
                state_changed = state_changed or added
                subscribers_changed = subscribers_changed or added
                reply = (
                    "✅ CGV 용산 IMAX 알림 구독이 완료되었습니다."
                    if added
                    else "✅ 이미 CGV 용산 IMAX 알림을 받고 있습니다."
                )
                # Lead with "nothing to configure".  Listing the settings here
                # made a finished subscription read like an unfinished setup.
                reply += (
                    "\n\n따로 설정하실 것은 없습니다."
                    "\n새 회차가 열리거나 잔여 좌석이 바뀌면 바로 알려드릴게요."
                    "\n\n알림이 많다고 느껴지면 /mode 로 종류를 줄일 수 있어요."
                    "\n자세한 설명 /desc · 해지 /stop"
                )
            elif command in {"/stop", "/unsubscribe"}:
                removed = self.state.remove_subscriber(chat_id)
                state_changed = state_changed or removed
                subscribers_changed = subscribers_changed or removed
                reply = (
                    "🔕 알림 구독을 해지했습니다. 다시 받으려면 /start를 보내주세요."
                    if removed
                    else "현재 알림을 구독하고 있지 않습니다. 구독하려면 /start를 보내주세요."
                )
            elif command == "/status":
                if self.state.is_subscribed(chat_id):
                    mode = self.state.alert_mode(chat_id)
                    mode_label = ALERT_MODE_LABELS[mode]
                    reply = (
                        "✅ 현재 CGV 용산 IMAX 알림을 구독 중입니다.\n"
                        f"알림 종류: {mode_label}"
                    )
                    if ALERT_SEATS in ALERT_MODES[mode]:
                        seat_label = SEAT_INFO_LABELS[
                            self.state.verified_seats_only(chat_id)
                        ]
                        reply += f"\n좌석 알림 범위: {seat_label}"
                    reply += "\n\n알림 종류 변경: /mode\n좌석 알림 범위 변경: /seat"
                else:
                    reply = (
                        "🔕 현재 구독 중이 아닙니다. "
                        "알림을 받으려면 /start를 보내주세요."
                    )
            elif command in MODE_COMMANDS:
                reply, mode_changed = self._handle_mode_command(
                    chat_id, command, argument
                )
                state_changed = state_changed or mode_changed
            elif command in SEAT_INFO_COMMANDS:
                reply, seat_info_changed = self._handle_seat_info_command(
                    chat_id, command, argument
                )
                state_changed = state_changed or seat_info_changed
            elif command in ADMIN_STATS_COMMANDS and chat_id == str(
                self.config.telegram_chat_id
            ):
                # Anyone else falls through to the unknown-command reply, so the
                # command is not advertised to subscribers at all.
                reply = self._subscriber_stats_message()
            elif command == "/help":
                reply = (
                    "🎬 CGV 용산 IMAX 알림 봇\n\n"
                    "/start - 알림 구독\n"
                    "/stop - 알림 해지\n"
                    "/status - 구독 상태 확인\n"
                    "/mode - 알림 종류 선택\n"
                    "/seat - 잔여 좌석 알림 범위 선택\n"
                    "/desc - 봇 설명과 사용 방법\n"
                    "/coffee - 개발자에게 커피 후원\n"
                    "/help - 사용법 보기\n\n"
                    "/mode 와 /seat 은 선택 사항입니다.\n"
                    "그대로 두시면 모든 알림을 받습니다."
                )
            elif command in {"/desc", "/description"}:
                reply = (
                    "🎬 용아맥 오디세이 알림 봇\n\n"
                    "CGV 용산아이파크몰 IMAX의 오디세이 예매 오픈과 "
                    "잔여 좌석 변화를 확인해 알려주는 비공식 봇입니다.\n"
                    "한국시간 기준 오늘부터 28일간의 상영 회차를 감시합니다.\n\n"
                    "🔔 알려드리는 내용\n"
                    "• 새 IMAX 상영 회차 예매 오픈\n"
                    "• 잔여 좌석 또는 일반 예매 가능 좌석 변경\n"
                    "• 상영일·시작시간·잔여좌석/총좌석·예매 링크\n\n"
                    "🚫 알림 제외\n"
                    "• 장애인석만 남은 경우\n"
                    "• A열만 남은 경우\n"
                    "• 잔여 좌석이 0석인 경우\n\n"
                    "🔧 알림 종류 선택 (선택 사항)\n"
                    "그대로 두시면 아래 알림을 모두 받습니다.\n"
                    "• /mode_all — 신규 오픈 + 잔여 좌석 (기본)\n"
                    "• /mode_open — 신규 오픈만\n"
                    "• /mode_seats — 잔여 좌석만\n\n"
                    "💺 잔여 좌석 알림 범위 (선택 사항)\n"
                    "• /seat_all — 좌석 종류 미확인 알림도 받기 (기본)\n"
                    "• /seat_verified — 일반 좌석이 확인된 알림만 받기\n\n"
                    "📌 사용 방법\n"
                    "1. /start — 알림 구독\n"
                    "2. 알림이 오면 예매 바로가기 링크 열기\n"
                    "3. CGV 화면에서 IMAX 버튼 선택 후 예매\n\n"
                    "/mode — 현재 알림 종류 확인·변경\n"
                    "/seat — 잔여 좌석 알림 범위 확인·변경\n"
                    "/status — 구독 상태 확인\n"
                    "/stop — 알림 해지\n"
                    "/coffee — 개발자에게 커피 후원\n"
                    "/help — 전체 명령어 보기"
                )
            elif command in {"/coffee", "/donate"}:
                reply = (
                    "☕ 개발자에게 커피 한 잔 후원하기\n\n"
                    "용아맥 알림 봇이 도움이 되었다면 Ko-fi에서 후원할 수 있어요.\n"
                    "https://ko-fi.com/yuemyname\n\n"
                    "후원 여부와 관계없이 모든 알림 기능은 동일하게 제공됩니다."
                )
            else:
                reply = "사용 가능한 명령어를 보려면 /help를 보내주세요."

            try:
                self.telegram.send_message(reply, chat_id=chat_id)
            except TelegramError as exc:
                self.logger.warning("Telegram 구독 명령 답장 실패: %s", exc)
            else:
                self.logger.debug("Telegram 명령 처리: %s", command)

        if state_changed:
            self.state.save()
        if subscribers_changed:
            self.logger.info(
                "Telegram 구독자 변경: 현재 %d명",
                len(self.state.subscriber_ids()),
            )

    def _record_verdicts(
        self,
        tally: "_CycleTally",
        sessions: Sequence[BookingSession],
        verdicts: Mapping[str, str],
        session_keys: Mapping[BookingSession, str],
        snapshots: Mapping[str, SeatSnapshot],
    ) -> None:
        """Note what happened to each showing, for the end-of-cycle DEBUG line."""

        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        for session in sessions:
            key = session_keys[session]
            snapshot = snapshots.get(key)
            remaining = snapshot.total if snapshot else session.remaining_seats
            tally.verdicts.append(
                f"{session.date[5:]} {session.start_time} "
                f"{remaining if remaining is not None else '?'}"
                f"/{session.total_seats or '?'} "
                f"{verdicts.get(key, '변화없음')}"
            )

    def _flush_state(self, tally: "_CycleTally") -> None:
        """Persist as soon as a date is done, not once the whole cycle is.

        Alerts now go out mid-scan, so a crash between sending and saving
        would resend them on the next cycle.  Saving per date keeps that
        window to one date's worth of work.
        """

        if tally.dirty and not self.dry_run:
            self.state.save()
            tally.dirty = False

    def _scan_and_alert(
        self,
        dates: Sequence[dt.date],
        errors: dict[dt.date, str],
        tally: "_CycleTally",
    ) -> None:
        """Fetch each date and alert on it before moving to the next.

        Alerting per date rather than after the whole sweep is what keeps the
        delay between reading a seat count and sending it near zero.  Today is
        requested first, so the showings closest to their start time — the ones
        whose seats move fastest — are also the ones alerted soonest.
        """

        for index, show_date in enumerate(dates):
            if tally.rate_limited:
                tally.schedule_skipped_dates += len(dates) - index
                break

            payload = None
            self._wait_for_cgv_request_slot()
            try:
                payload = self.cgv.fetch_date(show_date)
            except FetchError as exc:
                message = str(exc)
                errors[show_date] = message
                if "HTTP 429" in message:
                    # The guard at the top of the next iteration is what counts
                    # the skipped dates; adding them here too would double them.
                    tally.rate_limited = True
                    tally.rate_limited_requests += 1
                    self.logger.warning(
                        "CGV HTTP 429 감지: 남은 일정 조회 %d일을 즉시 생략합니다.",
                        len(dates) - index - 1,
                    )
            except Exception as exc:  # Keep a single malformed date from stopping the watcher.
                errors[show_date] = f"예상하지 못한 조회 오류: {type(exc).__name__}"
            finally:
                self._mark_cgv_request_finished()

            if payload is None:
                continue
            tally.successful_dates += 1

            session_map = self._extract_sessions({show_date: payload})
            for date_text, _start_time in session_map:
                if date_text > tally.latest_session_date:
                    tally.latest_session_date = date_text

            sessions, closed_sessions = self._split_closed_sessions(
                sorted(session_map.values())
            )
            if closed_sessions:
                tally.suppressed_closed += len(closed_sessions)
                closed_keys = {
                    session: f"closed:{index}"
                    for index, session in enumerate(closed_sessions)
                }
                self._record_verdicts(
                    tally,
                    closed_sessions,
                    {key: "제외·예매 마감" for key in closed_keys.values()},
                    closed_keys,
                    {},
                )
                self.logger.info(
                    "알림 제외: %s 상영이 시작돼 예매가 마감된 회차 %d개",
                    show_date.isoformat(),
                    len(closed_sessions),
                )
            tally.matching_sessions += len(sessions)
            if sessions:
                self._alert_for_sessions(sessions, tally)
            self._flush_state(tally)

    def _alert_for_sessions(
        self, sessions: Sequence[BookingSession], tally: "_CycleTally"
    ) -> None:
        """Read seat detail for one date's sessions and send what qualifies."""

        session_keys = {
            session: session.notification_key(
                site_no=self.config.site_no, movie_no=self.config.movie_no
            )
            for session in sessions
        }
        previously_notified = {
            key for key in session_keys.values() if self.state.was_notified(key)
        }
        previous_snapshots = {
            key: self.state.seat_snapshot(key) for key in session_keys.values()
        }

        verdicts: dict[str, str] = {}
        snapshots: dict[str, SeatSnapshot] = {}
        seat_candidates: list[BookingSession] = []
        for session in sessions:
            key = session_keys[session]
            remaining = session.remaining_seats
            if remaining is None:
                continue
            if remaining == 0:
                snapshots[key] = SeatSnapshot(total=0)
                continue

            previous = previous_snapshots.get(key)
            needs_detail = (
                previous is None
                or previous.total != remaining
                or (
                    key not in previously_notified
                    and not previous.seat_map_complete
                )
            )
            if needs_detail:
                seat_candidates.append(session)
            else:
                # The schedule total did not change, so the saved complete map
                # remains sufficient. Avoid requesting every open show each
                # minute, which can trigger CGV's HTTP 429 rate limit.
                snapshots[key] = previous

        if tally.rate_limited:
            tally.seat_detail_skipped += len(seat_candidates)
            for session in seat_candidates:
                snapshots[session_keys[session]] = SeatSnapshot(
                    total=session.remaining_seats or 0
                )
        else:
            for index, session in enumerate(seat_candidates):
                key = session_keys[session]
                self._wait_for_cgv_request_slot()
                try:
                    snapshots[key] = self.cgv.fetch_seat_snapshot(session)
                except FetchError as exc:
                    message = str(exc)
                    tally.seat_detail_errors += 1
                    tally.seat_detail_error_sample = (
                        tally.seat_detail_error_sample or message
                    )
                    snapshots[key] = SeatSnapshot(
                        total=session.remaining_seats or 0
                    )
                    if "HTTP 429" in message:
                        tally.rate_limited = True
                        tally.rate_limited_requests += 1
                        remaining_sessions = seat_candidates[index + 1 :]
                        tally.seat_detail_skipped += len(remaining_sessions)
                        for skipped_session in remaining_sessions:
                            snapshots[session_keys[skipped_session]] = SeatSnapshot(
                                total=skipped_session.remaining_seats or 0
                            )
                        self.logger.warning(
                            "CGV HTTP 429 감지: 남은 좌석 상세 조회 %d개를 즉시 생략합니다.",
                            len(remaining_sessions),
                        )
                        break
                except Exception as exc:
                    tally.seat_detail_errors += 1
                    tally.seat_detail_error_sample = (
                        tally.seat_detail_error_sample
                        or f"예상하지 못한 좌석 조회 오류: {type(exc).__name__}"
                    )
                    snapshots[key] = SeatSnapshot(
                        total=session.remaining_seats or 0
                    )
                finally:
                    self._mark_cgv_request_finished()

        detected_new_sessions = [
            session
            for session in sessions
            if session_keys[session] not in previously_notified
        ]
        sold_out_new_sessions = [
            session
            for session in detected_new_sessions
            if snapshots.get(session_keys[session]) is not None
            and snapshots[session_keys[session]].total == 0
        ]
        suppressed_new_sessions = [
            session
            for session in detected_new_sessions
            if snapshots.get(session_keys[session]) is not None
            and snapshots[session_keys[session]].total > 0
            and snapshots[session_keys[session]].should_suppress
        ]
        deferred_new_sessions = [
            session
            for session in detected_new_sessions
            if session not in sold_out_new_sessions
            and session not in suppressed_new_sessions
            and (
                snapshots.get(session_keys[session]) is None
                or not snapshots[session_keys[session]].alertable
            )
        ]
        new_sessions = [
            session
            for session in detected_new_sessions
            if snapshots.get(session_keys[session]) is not None
            and snapshots[session_keys[session]].alertable
        ]

        for session in sold_out_new_sessions:
            verdicts[session_keys[session]] = "제외·매진"
        for session in suppressed_new_sessions:
            snapshot = snapshots[session_keys[session]]
            verdicts[session_keys[session]] = (
                "제외·장애인석만" if snapshot.accessible_only else "제외·A열만"
            )
        for session in deferred_new_sessions:
            verdicts[session_keys[session]] = "보류·좌석종류 미확인"

        tally.suppressed_sold_out += len(sold_out_new_sessions)
        tally.deferred_keys.update(
            session_keys[session] for session in deferred_new_sessions
        )
        unclassified_new_keys = {
            session_keys[session]
            for session in new_sessions
            if snapshots[session_keys[session]].uses_unclassified_fallback
        }
        tally.unclassified_fallback_alerts += len(unclassified_new_keys)
        tally.suppressed_accessible_only += sum(
            snapshots[session_keys[session]].accessible_only
            for session in suppressed_new_sessions
        )
        tally.suppressed_row_a_only += sum(
            snapshots[session_keys[session]].row_a_only
            and not snapshots[session_keys[session]].accessible_only
            for session in suppressed_new_sessions
        )
        for session in suppressed_new_sessions:
            snapshot = snapshots[session_keys[session]]
            self.logger.info(
                "알림 제외: %s %s",
                _session_line(session),
                snapshot.suppression_reason,
            )

        if sold_out_new_sessions:
            self.logger.info(
                "알림 제외: 잔여 0석인 신규 회차 %d개",
                len(sold_out_new_sessions),
            )
        if deferred_new_sessions:
            self.logger.info(
                "알림 보류: 좌석 종류 판별 실패 후 잔여 6석 이하인 신규 회차 %d개",
                len(deferred_new_sessions),
            )

        if new_sessions:
            if self.dry_run:
                self.logger.info("드라이런: 새 회차 %d개(메시지/상태 저장 생략)", len(new_sessions))
                for session in new_sessions:
                    self.logger.info("드라이런 회차: %s", _session_line(session))
                tally.new_sessions += len(new_sessions)
            else:
                for text, chunk_sessions in message_chunks(
                    new_sessions,
                    self.config,
                    unclassified_keys=unclassified_new_keys,
                ):
                    delivered, failed, total = self._broadcast_message(
                        text, category=ALERT_OPEN
                    )
                    if total and not delivered:
                        break
                    for session in chunk_sessions:
                        self.state.mark_notified(session_keys[session], session)
                        verdicts[session_keys[session]] = "발송·예매 오픈"
                        tally.dirty = True
                    tally.new_sessions += len(chunk_sessions)
                    self.logger.info(
                        "Telegram 신규 회차 알림 %d개 전송: 성공 %d명, 실패 %d명",
                        len(chunk_sessions),
                        delivered,
                        failed,
                    )

        for session in sessions:
            key = session_keys[session]
            current = snapshots.get(key)
            if current is None:
                continue
            previous = previous_snapshots.get(key)
            if previous is None:
                verdicts.setdefault(key, "첫 관측·기록만")
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    tally.dirty = True
                continue
            if not _seat_snapshot_changed(previous, current):
                continue

            if current.total == 0:
                verdicts[key] = "제외·매진"
                tally.suppressed_sold_out += 1
                self.logger.info(
                    "좌석 변경 알림 제외: %s 잔여 좌석이 0석입니다.",
                    _session_line(session),
                )
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    tally.dirty = True
                continue

            if current.should_suppress:
                if current.accessible_only:
                    verdicts[key] = "제외·장애인석만"
                    tally.suppressed_accessible_only += 1
                elif current.row_a_only:
                    verdicts[key] = "제외·A열만"
                    tally.suppressed_row_a_only += 1
                self.logger.info(
                    "좌석 변경 알림 제외: %s %s",
                    _session_line(session),
                    current.suppression_reason,
                )
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    tally.dirty = True
                continue

            if not current.alertable:
                verdicts[key] = "보류·좌석종류 미확인"
                tally.deferred_keys.add(key)
                self.logger.info(
                    "좌석 변경 알림 보류: %s 좌석 종류 판별 실패 후 잔여 6석 이하입니다.",
                    _session_line(session),
                )
                continue

            # A session not notified before this cycle receives the booking-open
            # alert above.  Do not send a second seat-change message for the same
            # observation.
            if key not in previously_notified:
                verdicts.setdefault(key, "오픈 알림으로 갈음")
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    tally.dirty = True
                continue

            if current.uses_unclassified_fallback:
                tally.unclassified_fallback_alerts += 1
                self.logger.info(
                    "좌석 종류 미확인 알림 허용: %s 잔여 7석 이상입니다.",
                    _session_line(session),
                )

            tally.seat_changes += 1
            if self.dry_run:
                self.logger.info(
                    "드라이런 좌석 변경: %s (%d석 -> %d석)",
                    _session_line(session),
                    previous.total,
                    current.total,
                )
                continue
            delivered, failed, total = self._broadcast_message(
                seat_change_message(session, previous, current, self.config),
                category=(
                    ALERT_SEATS_UNCLASSIFIED
                    if current.uses_unclassified_fallback
                    else ALERT_SEATS
                ),
            )
            if delivered or total == 0:
                self.state.set_seat_snapshot(key, current)
                verdicts[key] = f"발송·좌석 변경 {previous.total}→{current.total}"
                tally.dirty = True
                self.logger.info(
                    "Telegram 좌석 변경 알림 전송: %s (%d석 -> %d석), "
                    "성공 %d명, 실패 %d명",
                    _session_line(session),
                    previous.total,
                    current.total,
                    delivered,
                    failed,
                )

        self._record_verdicts(tally, sessions, verdicts, session_keys, snapshots)

    def run_cycle(self) -> CycleResult:
        today = self.config.local_today()
        plan = self._plan_scan(today)
        dates = list(plan.dates)
        tally = _CycleTally()
        pruned_records = 0 if self.dry_run else self.state.prune_expired(today)
        if pruned_records:
            self.logger.info(
                "지난 상영일 상태 기록 %d개를 정리했습니다.", pruned_records
            )
            tally.dirty = True
        errors: dict[dt.date, str] = {}

        self.logger.info(
            "조회 시작: %s 모드, %d일 (%s~%s)",
            "전체" if plan.full_scan else "커서",
            len(dates),
            dates[0].isoformat() if dates else "-",
            dates[-1].isoformat() if dates else "-",
        )
        self._scan_and_alert(dates, errors, tally)

        # The probe found a session past the frontier, so a new booking opening
        # started.  Widen the scan now to find where the newly opened range
        # ends, instead of creeping forward a few days per cycle.
        if (
            not plan.full_scan
            and not tally.rate_limited
            and plan.probe_hit(tally.latest_session_date)
        ):
            expansion = self._expansion_dates(plan)
            if expansion:
                self.logger.info(
                    "예매 오픈 감지: %s까지 확장 조회합니다(%d일).",
                    expansion[-1].isoformat(),
                    len(expansion),
                )
                self._scan_and_alert(expansion, errors, tally)
                dates.extend(expansion)

        if tally.latest_session_date:
            try:
                observed = dt.date.fromisoformat(tally.latest_session_date)
            except ValueError:
                observed = None
            if observed is not None and self.state.advance_frontier(observed):
                self.logger.info("예매 오픈 관측 최대 상영일: %s", observed.isoformat())
                tally.dirty = tally.dirty or not self.dry_run
        self._cycle_index += 1

        if tally.seat_detail_errors:
            self.logger.warning(
                "좌석 상세 조회 오류 %d개: %s "
                "(6석 이하는 보류, 7석 이상은 전체 잔여 수 기준 알림)",
                tally.seat_detail_errors,
                tally.seat_detail_error_sample,
            )

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
                    + (
                        f"요청 제한으로 남은 {tally.schedule_skipped_dates}일 조회를 생략했습니다.\n"
                        if tally.schedule_skipped_dates
                        else ""
                    )
                    + "감시기는 계속 실행되며 자동 대기 후 다시 시도합니다."
                )
                delivered, _failed, total = self._broadcast_message(
                    error_text, category=ALERT_SYSTEM
                )
                if delivered or total == 0:
                    self.state.mark_error_notified(fingerprint)
                    tally.dirty = True
        elif self.state.clear_error():
            tally.dirty = True

        self._flush_state(tally)

        if tally.verdicts:
            self.logger.debug(
                "회차 판정 %d건 | %s",
                len(tally.verdicts),
                " | ".join(tally.verdicts),
            )

        self.logger.info(
            "조회 완료: 성공 %d일, 오류 %d일, IMAX 회차 %d개, 신규 %d개, "
            "좌석변경 %d개, 장애인석만 남아 제외 %d개, A열만 남아 제외 %d개, "
            "0석 제외 %d개, 예매 마감 제외 %d개, 좌석판별 대기 %d개, "
            "미판별 7석 이상 알림 %d개, "
            "HTTP 429 %d개, 일정 생략 %d일, 좌석상세 생략 %d개",
            tally.successful_dates,
            len(errors),
            tally.matching_sessions,
            tally.new_sessions,
            tally.seat_changes,
            tally.suppressed_accessible_only,
            tally.suppressed_row_a_only,
            tally.suppressed_sold_out,
            tally.suppressed_closed,
            len(tally.deferred_keys),
            tally.unclassified_fallback_alerts,
            tally.rate_limited_requests,
            tally.schedule_skipped_dates,
            tally.seat_detail_skipped,
        )
        return CycleResult(
            successful_dates=tally.successful_dates,
            failed_dates=len(errors),
            matching_sessions=tally.matching_sessions,
            new_sessions=tally.new_sessions,
            seat_changes=tally.seat_changes,
            suppressed_accessible_only=tally.suppressed_accessible_only,
            suppressed_row_a_only=tally.suppressed_row_a_only,
            suppressed_sold_out=tally.suppressed_sold_out,
            deferred_seat_details=len(tally.deferred_keys),
            unclassified_fallback_alerts=tally.unclassified_fallback_alerts,
            seat_detail_errors=tally.seat_detail_errors,
            rate_limited_requests=tally.rate_limited_requests,
            schedule_skipped_dates=tally.schedule_skipped_dates,
            seat_detail_skipped=tally.seat_detail_skipped,
            requested_dates=len(dates),
            full_scan=plan.full_scan,
            suppressed_closed=tally.suppressed_closed,
        )


class TimezoneFormatter(logging.Formatter):
    """Render log timestamps in the configured application timezone."""

    def __init__(self, *args: Any, timezone_name: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.timezone = ZoneInfo(timezone_name)

    def formatTime(
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        local_time = dt.datetime.fromtimestamp(record.created, tz=self.timezone)
        if datefmt:
            return local_time.strftime(datefmt)
        return local_time.isoformat(sep=" ", timespec="milliseconds")


def rate_limit_backoff_seconds(config: Config, consecutive_cycles: int) -> int:
    """Return an exponential HTTP 429 cooldown capped by configuration."""

    if consecutive_cycles < 1:
        return config.poll_interval_seconds
    cooldown = config.rate_limit_backoff_initial_seconds * (
        2 ** (consecutive_cycles - 1)
    )
    return max(
        config.poll_interval_seconds,
        min(cooldown, config.rate_limit_backoff_max_seconds),
    )


def run_command_loop(watcher: Watcher, stop_event: threading.Event) -> None:
    """Keep Telegram commands responsive without delaying CGV requests."""

    while not stop_event.is_set():
        try:
            watcher.sync_subscribers()
        except Exception:
            watcher.logger.exception("Telegram 명령 처리 중 예상하지 못한 오류")
        stop_event.wait(watcher.config.telegram_command_poll_seconds)


def configure_logging(config: Config, *, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("cgv_watcher")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = TimezoneFormatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
        timezone_name=config.timezone_name,
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

    range_start, range_end = config.target_range()
    range_label = (
        f"오늘부터 {config.target_window_days}일(매일 자동 갱신)"
        if config.dynamic_date_window
        else f"{range_start}~{range_end}"
    )
    logger.info(
        "%s 시작: siteNo=%s, movNo=%s, %s [%s~%s], %d초 간격",
        APP_NAME,
        config.site_no,
        config.movie_no,
        range_label,
        range_start,
        range_end,
        config.poll_interval_seconds,
    )
    logger.info("CGV 로그인 토큰과 로그인 쿠키는 사용하지 않습니다.")
    if config.subscriptions_enabled:
        logger.info(
            "Telegram 명령은 CGV 조회와 별도로 %d초 간격으로 확인합니다.",
            config.telegram_command_poll_seconds,
        )

    if args.once:
        watcher.sync_subscribers()
        result = watcher.run_cycle()
        return 0 if result.successful_dates else 3

    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    command_stop_event = threading.Event()
    command_thread: threading.Thread | None = None
    if config.subscriptions_enabled and not args.dry_run:
        command_thread = threading.Thread(
            target=run_command_loop,
            args=(watcher, command_stop_event),
            name="telegram-command-poller",
            daemon=True,
        )
        command_thread.start()

    consecutive_rate_limit_cycles = 0
    while not stop_requested:
        if (
            not config.dynamic_date_window
            and config.local_today() > config.target_end
        ):
            logger.info("감시 대상 마지막 날짜가 지나 정상 종료합니다.")
            break
        started = time.monotonic()
        result = watcher.run_cycle()
        if result.rate_limited_requests:
            consecutive_rate_limit_cycles += 1
            next_interval = rate_limit_backoff_seconds(
                config, consecutive_rate_limit_cycles
            )
            logger.warning(
                "CGV HTTP 429 요청 제한 감지: 다음 조회는 %d분 뒤에 시도합니다. "
                "(이번 주기 %d개, 연속 %d회)",
                max(1, next_interval // 60),
                result.rate_limited_requests,
                consecutive_rate_limit_cycles,
            )
        else:
            if consecutive_rate_limit_cycles:
                logger.info(
                    "CGV HTTP 429 요청 제한이 해제되어 %d초 조회 주기로 복귀합니다.",
                    config.poll_interval_seconds,
                )
            consecutive_rate_limit_cycles = 0
            next_interval = config.poll_interval_seconds
        elapsed = time.monotonic() - started
        sleep_seconds = max(0.5, next_interval - elapsed)
        deadline = time.monotonic() + sleep_seconds
        while not stop_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    command_stop_event.set()
    if command_thread is not None:
        command_thread.join(timeout=config.request_timeout_seconds + 1)
    logger.info("사용자 요청으로 감시기를 종료합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
