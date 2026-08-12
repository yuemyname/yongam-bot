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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_NAME = "CGV Telegram Watcher"
DEFAULT_API_URL = "https://cgv.co.kr/api/v1/booking/searchSchByMov"
DEFAULT_SEAT_API_URL = "https://cgv.co.kr/api/v1/booking/searchIfSeatData"
DEFAULT_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/movie"
DEFAULT_SEAT_PAGE_URL = "https://cgv.co.kr/cnm/selectVisitorCnt"
DEFAULT_SITE_NAME = "용산아이파크몰"
UNCLASSIFIED_ALERT_MIN_SEATS = 7
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
    request_timeout_seconds: int
    max_workers: int
    imax_keywords: tuple[str, ...]
    imax_code_values: tuple[str, ...]
    strict_imax_match: bool
    subscriptions_enabled: bool
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
            subscriptions_enabled=_parse_bool(
                value("SUBSCRIPTIONS_ENABLED", "true"),
                name="SUBSCRIPTIONS_ENABLED",
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

    def local_today(self) -> dt.date:
        return dt.datetime.now(ZoneInfo(self.timezone_name)).date()

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


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "version": 4,
            "notified": {},
            "seat_counts": {},
            "subscribers": {},
            "subscribers_initialized": False,
            "telegram_update_offset": 0,
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
        if not isinstance(loaded.get("seat_counts", {}), dict):
            raise RuntimeError(f"좌석 수 상태 파일 형식이 올바르지 않습니다: {self.path}")
        if not isinstance(loaded.get("subscribers", {}), dict):
            raise RuntimeError(f"구독자 상태 파일 형식이 올바르지 않습니다: {self.path}")
        self.data.update(loaded)
        self.data["version"] = 4
        self.data.setdefault("seat_counts", {})
        self.data.setdefault("subscribers", {})
        self.data.setdefault("subscribers_initialized", False)
        self.data.setdefault("telegram_update_offset", 0)

    def was_notified(self, key: str) -> bool:
        return key in self.data["notified"]

    def mark_notified(self, key: str, session: BookingSession) -> None:
        self.data["notified"][key] = {
            "notified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "date": session.date,
            "start_time": session.start_time,
            "screen_name": session.screen_name,
            "remaining_seats": session.remaining_seats,
            "total_seats": session.total_seats,
        }

    @property
    def subscribers_initialized(self) -> bool:
        return bool(self.data.get("subscribers_initialized", False))

    def initialize_subscribers(self, initial_chat_id: str) -> None:
        if initial_chat_id:
            self.add_subscriber(initial_chat_id, label="초기 관리자")
        self.data["subscribers_initialized"] = True

    def subscriber_ids(self) -> tuple[str, ...]:
        return tuple(str(chat_id) for chat_id in self.data["subscribers"])

    def is_subscribed(self, chat_id: str) -> bool:
        return str(chat_id) in self.data["subscribers"]

    def add_subscriber(
        self, chat_id: str, *, label: str = "", chat_type: str = ""
    ) -> bool:
        key = str(chat_id)
        if key in self.data["subscribers"]:
            return False
        self.data["subscribers"][key] = {
            "subscribed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "label": label[:100],
            "chat_type": chat_type[:30],
        }
        return True

    def remove_subscriber(self, chat_id: str) -> bool:
        return self.data["subscribers"].pop(str(chat_id), None) is not None

    @property
    def telegram_update_offset(self) -> int:
        value = self.data.get("telegram_update_offset", 0)
        return value if isinstance(value, int) and value >= 0 else 0

    def set_telegram_update_offset(self, offset: int) -> None:
        self.data["telegram_update_offset"] = max(0, int(offset))

    def seat_snapshot(self, key: str) -> SeatSnapshot | None:
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
        f"영화: {config.movie_label} ({config.movie_no})",
        f"극장: 용산아이파크몰 ({config.site_no})",
        "",
        _alert_date_banner(session.date),
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
    header = (
        "🎟️ CGV 예매 오픈 감지\n"
        f"영화: {config.movie_label} ({config.movie_no})\n"
        f"극장: 용산아이파크몰 ({config.site_no})\n\n"
    )
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
            header
            + _alert_date_banner(show_date)
            + "\n"
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
        if not self.state.subscribers_initialized:
            self.state.initialize_subscribers(config.telegram_chat_id)
            if not self.dry_run:
                self.state.save()

    def _broadcast_message(self, text: str) -> tuple[int, int, int]:
        subscriber_ids = self.state.subscriber_ids()
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
            self.logger.warning("등록된 Telegram 구독자가 없어 알림을 전송하지 않습니다.")
        return delivered, failed, len(subscriber_ids)

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

        state_changed = False
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
            command = text.split(maxsplit=1)[0].lower().split("@", 1)[0]
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
                reply = (
                    "✅ CGV 용산 IMAX 알림 구독이 완료되었습니다."
                    if added
                    else "✅ 이미 CGV 용산 IMAX 알림을 받고 있습니다."
                )
                reply += (
                    "\n\n예매 오픈과 좌석 수 변경을 알려드릴게요."
                    "\n알림 해지: /stop\n구독 상태: /status"
                )
            elif command in {"/stop", "/unsubscribe"}:
                removed = self.state.remove_subscriber(chat_id)
                state_changed = state_changed or removed
                reply = (
                    "🔕 알림 구독을 해지했습니다. 다시 받으려면 /start를 보내주세요."
                    if removed
                    else "현재 알림을 구독하고 있지 않습니다. 구독하려면 /start를 보내주세요."
                )
            elif command == "/status":
                reply = (
                    "✅ 현재 CGV 용산 IMAX 알림을 구독 중입니다."
                    if self.state.is_subscribed(chat_id)
                    else "🔕 현재 구독 중이 아닙니다. 알림을 받으려면 /start를 보내주세요."
                )
            elif command == "/help":
                reply = (
                    "🎬 CGV 용산 IMAX 알림 봇\n\n"
                    "/start - 알림 구독\n"
                    "/stop - 알림 해지\n"
                    "/status - 구독 상태 확인\n"
                    "/help - 사용법 보기"
                )
            else:
                reply = "사용 가능한 명령어를 보려면 /help를 보내주세요."

            try:
                self.telegram.send_message(reply, chat_id=chat_id)
            except TelegramError as exc:
                self.logger.warning("Telegram 구독 명령 답장 실패: %s", exc)

        if state_changed:
            self.state.save()
            self.logger.info(
                "Telegram 구독 명령 처리 완료: 현재 구독자 %d명",
                len(self.state.subscriber_ids()),
            )

    def run_cycle(self) -> CycleResult:
        self.sync_subscribers()
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

        snapshots: dict[str, SeatSnapshot] = {}
        seat_detail_errors: dict[str, str] = {}
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

        if seat_candidates:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self.config.max_workers, len(seat_candidates))
            ) as executor:
                pending_seats = {
                    executor.submit(self.cgv.fetch_seat_snapshot, session): session
                    for session in seat_candidates
                }
                for future in concurrent.futures.as_completed(pending_seats):
                    session = pending_seats[future]
                    key = session_keys[session]
                    try:
                        snapshots[key] = future.result()
                    except FetchError as exc:
                        seat_detail_errors[key] = str(exc)
                        snapshots[key] = SeatSnapshot(
                            total=session.remaining_seats or 0
                        )
                    except Exception as exc:
                        seat_detail_errors[key] = (
                            f"예상하지 못한 좌석 조회 오류: {type(exc).__name__}"
                        )
                        snapshots[key] = SeatSnapshot(
                            total=session.remaining_seats or 0
                        )

        if seat_detail_errors:
            self.logger.warning(
                "좌석 상세 조회 오류 %d개: %s "
                "(6석 이하는 보류, 7석 이상은 전체 잔여 수 기준 알림)",
                len(seat_detail_errors),
                next(iter(seat_detail_errors.values())),
            )

        detected_new_sessions = [
            session
            for session in sessions
            if not self.state.was_notified(
                session_keys[session]
            )
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

        state_changed = False
        suppressed_sold_out = len(sold_out_new_sessions)
        deferred_seat_detail_keys = {
            session_keys[session] for session in deferred_new_sessions
        }
        unclassified_new_keys = {
            session_keys[session]
            for session in new_sessions
            if snapshots[session_keys[session]].uses_unclassified_fallback
        }
        unclassified_fallback_alerts = len(unclassified_new_keys)
        suppressed_accessible_only = sum(
            snapshots[session_keys[session]].accessible_only
            for session in suppressed_new_sessions
        )
        suppressed_row_a_only = sum(
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
            else:
                for text, chunk_sessions in message_chunks(
                    new_sessions,
                    self.config,
                    unclassified_keys=unclassified_new_keys,
                ):
                    delivered, failed, total = self._broadcast_message(text)
                    if total and not delivered:
                        break
                    for session in chunk_sessions:
                        key = session.notification_key(
                            site_no=self.config.site_no, movie_no=self.config.movie_no
                        )
                        self.state.mark_notified(key, session)
                        state_changed = True
                    self.logger.info(
                        "Telegram 신규 회차 알림 %d개 전송: 성공 %d명, 실패 %d명",
                        len(chunk_sessions),
                        delivered,
                        failed,
                    )

        seat_changes = 0
        for session in sessions:
            key = session_keys[session]
            current = snapshots.get(key)
            if current is None:
                continue
            previous = previous_snapshots.get(key)
            if previous is None:
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    state_changed = True
                continue
            if not _seat_snapshot_changed(previous, current):
                continue

            if current.total == 0:
                suppressed_sold_out += 1
                self.logger.info(
                    "좌석 변경 알림 제외: %s 잔여 좌석이 0석입니다.",
                    _session_line(session),
                )
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    state_changed = True
                continue

            if current.should_suppress:
                if current.accessible_only:
                    suppressed_accessible_only += 1
                elif current.row_a_only:
                    suppressed_row_a_only += 1
                self.logger.info(
                    "좌석 변경 알림 제외: %s %s",
                    _session_line(session),
                    current.suppression_reason,
                )
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    state_changed = True
                continue

            if not current.alertable:
                deferred_seat_detail_keys.add(key)
                self.logger.info(
                    "좌석 변경 알림 보류: %s 좌석 종류 판별 실패 후 잔여 6석 이하입니다.",
                    _session_line(session),
                )
                continue

            # A session not notified before this cycle receives the booking-open
            # alert above.  Do not send a second seat-change message for the same
            # observation.
            if key not in previously_notified:
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    state_changed = True
                continue

            if current.uses_unclassified_fallback:
                unclassified_fallback_alerts += 1
                self.logger.info(
                    "좌석 종류 미확인 알림 허용: %s 잔여 7석 이상입니다.",
                    _session_line(session),
                )

            seat_changes += 1
            if self.dry_run:
                self.logger.info(
                    "드라이런 좌석 변경: %s (%d석 -> %d석)",
                    _session_line(session),
                    previous.total,
                    current.total,
                )
                continue
            delivered, failed, total = self._broadcast_message(
                seat_change_message(session, previous, current, self.config)
            )
            if delivered or total == 0:
                self.state.set_seat_snapshot(key, current)
                state_changed = True
                self.logger.info(
                    "Telegram 좌석 변경 알림 전송: %s (%d석 -> %d석), "
                    "성공 %d명, 실패 %d명",
                    _session_line(session),
                    previous.total,
                    current.total,
                    delivered,
                    failed,
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
                    "감시기는 계속 실행되며 다음 주기에 다시 시도합니다."
                )
                delivered, _failed, total = self._broadcast_message(error_text)
                if delivered or total == 0:
                    self.state.mark_error_notified(fingerprint)
                    state_changed = True
        elif self.state.clear_error():
            state_changed = True

        if state_changed:
            self.state.save()

        self.logger.info(
            "조회 완료: 성공 %d일, 오류 %d일, IMAX 회차 %d개, 신규 %d개, "
            "좌석변경 %d개, 장애인석만 남아 제외 %d개, A열만 남아 제외 %d개, "
            "0석 제외 %d개, 좌석판별 대기 %d개, 미판별 7석 이상 알림 %d개",
            len(payloads),
            len(errors),
            len(sessions),
            len(new_sessions),
            seat_changes,
            suppressed_accessible_only,
            suppressed_row_a_only,
            suppressed_sold_out,
            len(deferred_seat_detail_keys),
            unclassified_fallback_alerts,
        )
        return CycleResult(
            successful_dates=len(payloads),
            failed_dates=len(errors),
            matching_sessions=len(sessions),
            new_sessions=len(new_sessions),
            seat_changes=seat_changes,
            suppressed_accessible_only=suppressed_accessible_only,
            suppressed_row_a_only=suppressed_row_a_only,
            suppressed_sold_out=suppressed_sold_out,
            deferred_seat_details=len(deferred_seat_detail_keys),
            unclassified_fallback_alerts=unclassified_fallback_alerts,
            seat_detail_errors=len(seat_detail_errors),
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
        if (
            not config.dynamic_date_window
            and config.local_today() > config.target_end
        ):
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
