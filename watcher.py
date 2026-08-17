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
import http.client
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
STATE_VERSION = 13
TELEGRAM_BROADCAST_WORKERS = 4
# Ceiling for the command-poll backoff while Telegram is unreachable.
TELEGRAM_POLL_BACKOFF_MAX_SECONDS = 60
# Printed once per cycle so a long log can be read cycle by cycle.
CYCLE_SEPARATOR = "─" * 60
# Telegram descriptions that mean the chat is permanently unreachable.
UNRECOVERABLE_CHAT_MARKERS = (
    "bot was blocked",
    "user is deactivated",
    "bot was kicked",
    "chat not found",
    "peer_id_invalid",
    "bot can't initiate conversation",
)
# A queued retry that never succeeds is dropped rather than kept forever.
PENDING_DELIVERY_TTL_HOURS = 24

# Scanning strategy.  "full" requests every date in the window each cycle.
# "cursor" requests only the already-open range plus a short probe past the
# frontier, which is where a new booking opening can first appear.
SCAN_MODE_FULL = "full"
SCAN_MODE_CURSOR = "cursor"
SCAN_MODES = (SCAN_MODE_FULL, SCAN_MODE_CURSOR)
DEFAULT_SCAN_MODE = SCAN_MODE_CURSOR

# Alert categories a broadcast can belong to.  "system" messages (fetch error
# notices) always reach every subscriber regardless of their preference.
ALERT_OPEN = "open"
ALERT_SEATS = "seats"
# A classified seat-change alert rendered only with the subscriber's preferred
# central-seat preset.  Keeping a separate category also makes queued Telegram
# retries respect a later preferred-seat setting change.
ALERT_SEATS_SWEET = "seats_sweet"
# A seat-change alert CGV's detail response could not classify.  It reaches the
# seat-alert subscribers who did not ask for verified seat information only.
ALERT_SEATS_UNCLASSIFIED = "seats_unclassified"
# A CGV fetch failure.  Operator-only: a subscriber can do nothing about it,
# and the watcher retries the failed dates on its own within a cycle or two.
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

# Operator-only. Deliberately left out of /help and the BotFather command list.
ADMIN_STATS_COMMAND = "/stats"
# Preferred seats for cancellation-ticket alerts.  Booking-open alerts remain
# unfiltered because detecting a newly opened showing is the bot's top priority.
SEAT_SELECTION_ALL = "all"
SEAT_SELECTION_SWEET = "sweet"
DEFAULT_SEAT_SELECTION = SEAT_SELECTION_ALL
SEAT_SELECTIONS = (SEAT_SELECTION_ALL, SEAT_SELECTION_SWEET)
SEAT_SELECTION_LABELS = {
    SEAT_SELECTION_ALL: "모든 A열 제외 좌석",
    SEAT_SELECTION_SWEET: "명당 좌석만",
}
SEAT_SELECTION_COMMAND_TARGETS = {
    "/seat_sweet": SEAT_SELECTION_SWEET,
    "/seat_all": SEAT_SELECTION_ALL,
}
SEAT_SELECTION_COMMANDS = {"/seat", *SEAT_SELECTION_COMMAND_TARGETS}
SEAT_SELECTION_ALIASES = {
    "sweet": SEAT_SELECTION_SWEET,
    "명당": SEAT_SELECTION_SWEET,
    "all": SEAT_SELECTION_ALL,
    "전체": SEAT_SELECTION_ALL,
}
SWEET_SEAT_RANGES: dict[str, tuple[int, int]] = {
    "F": (16, 29),
    "G": (16, 29),
    "H": (13, 32),
    "I": (13, 32),
    "J": (11, 34),
    "K": (11, 34),
    "L": (11, 34),
}
SEAT_SELECTION_GUIDE = (
    "받고 싶은 잔여 좌석을 고를 수 있습니다.\n"
    "/seat_all - 모든 A열 제외 좌석 받기 (기본)\n"
    "/seat_sweet - 명당 좌석만 받기\n"
    "  F16~29 · G16~29 · H13~32 · I13~32 · J11~34 · K11~34 · L11~34\n\n"
    "신규 예매 오픈 알림은 이 설정과 관계없이 항상 전송됩니다."
)

# How many seats must be on sale before the alert is worth sending.  Someone
# booking a pair has no use for a showing with one seat left.
MIN_SEATS_DEFAULT = 1
MIN_SEATS_CHOICES = (1, 2)
MIN_SEATS_LABELS = {
    1: "1석부터 모두",
    2: "2석 이상 남았을 때만",
}
MIN_SEATS_COMMAND_TARGETS = {
    "/count_1": 1,
    "/count_2": 2,
}
MIN_SEATS_COMMANDS = {"/count", *MIN_SEATS_COMMAND_TARGETS}
MIN_SEATS_ALIASES = {
    "1": 1,
    "2": 2,
    "1석": 1,
    "2석": 2,
    "all": 1,
    "전체": 1,
}
MIN_SEATS_GUIDE = (
    "예매 가능한 좌석이 몇 석 이상일 때 알림을 받을지 고를 수 있습니다.\n"
    "/count_1 - 1석부터 모두 받기 (기본)\n"
    "/count_2 - 2석 이상 남았을 때만 받기\n\n"
    "신규 예매 오픈 알림은 이 설정과 관계없이 항상 전송됩니다."
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

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        description: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.description = description

    @property
    def recipient_gone(self) -> bool:
        """Whether this chat can never receive a message again.

        Telegram answers 403 for a chat the bot was blocked by, kicked from,
        or whose owner deleted their account, and 400 "chat not found" once the
        chat itself is gone.  Retrying any of those is wasted forever, unlike a
        timeout or a 5xx, so they are the cases worth telling apart.
        """

        text = self.description.lower()
        if self.status_code == 403:
            return True
        if any(marker in text for marker in UNRECOVERABLE_CHAT_MARKERS):
            return True
        return False


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


def _parse_show_date(value: str) -> dt.date | None:
    """Show date from a schedule record, or None when CGV sent junk."""

    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


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
    site_name: str
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
    deferred_recheck_cycles: int
    seat_recheck_always_days: int
    seat_recheck_rotate_days: int
    seat_recheck_rotate_cycles: int
    seat_alert_repeat_minutes: int
    pending_delivery_max_attempts: int
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
            site_name=value("CGV_SITE_NAME", DEFAULT_SITE_NAME),
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
            deferred_recheck_cycles=_parse_int(
                value("DEFERRED_RECHECK_CYCLES", "5"),
                name="DEFERRED_RECHECK_CYCLES",
                minimum=1,
                maximum=60,
            ),
            seat_recheck_always_days=_parse_int(
                value("SEAT_RECHECK_ALWAYS_DAYS", "2"),
                name="SEAT_RECHECK_ALWAYS_DAYS",
                minimum=0,
                maximum=28,
            ),
            seat_recheck_rotate_days=_parse_int(
                value("SEAT_RECHECK_ROTATE_DAYS", "7"),
                name="SEAT_RECHECK_ROTATE_DAYS",
                minimum=0,
                maximum=28,
            ),
            seat_recheck_rotate_cycles=_parse_int(
                value("SEAT_RECHECK_ROTATE_CYCLES", "5"),
                name="SEAT_RECHECK_ROTATE_CYCLES",
                minimum=1,
                maximum=60,
            ),
            seat_alert_repeat_minutes=_parse_int(
                value("SEAT_ALERT_REPEAT_MINUTES", "0"),
                name="SEAT_ALERT_REPEAT_MINUTES",
                minimum=0,
                maximum=1440,
            ),
            pending_delivery_max_attempts=_parse_int(
                value("PENDING_DELIVERY_MAX_ATTEMPTS", "30"),
                name="PENDING_DELIVERY_MAX_ATTEMPTS",
                minimum=1,
                maximum=500,
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
    usable: int | None = None
    mapped_total: int | None = None
    available_rows: tuple[str, ...] | None = None
    available_seats: tuple[tuple[str, str], ...] | None = None

    @property
    def seat_map_complete(self) -> bool:
        """Whether the detail response can safely classify an alert.

        Counts must agree with the schedule, and every saleable seat needs a
        row label so A-row availability can be excluded without guessing.
        """
        return (
            self.total > 0
            and self.usable is not None
            and self.mapped_total == self.total
            and self.available_rows is not None
        )

    @property
    def row_a_only(self) -> bool:
        return self.seat_map_complete and self.usable == 0

    @property
    def suppression_reason(self) -> str | None:
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


def _normalize_seat_number(value: Any) -> str:
    number = str(value if value is not None else "").strip().upper()
    return re.sub(r"\s*번$", "", number).strip()


def _natural_sort_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value)
        if part
    )


def _seat_location_sort_key(
    location: tuple[str, str],
) -> tuple[tuple[tuple[int, int | str], ...], tuple[tuple[int, int | str], ...]]:
    row, number = location
    return _natural_sort_key(row), _natural_sort_key(number)


def extract_seat_snapshot(payload: Any, *, scheduled_remaining: int) -> SeatSnapshot:
    """Count saleable non-A-row seats from CGV's anonymous seat map."""

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

    usable = 0
    mapped_total = 0
    available_rows: set[str] = set()
    available_seats: set[tuple[str, str]] = set()
    row_labels_complete = True
    seat_numbers_complete = True
    for seat in seats.values():
        if str(_direct_value(seat, {"seatstuscd"}) or "").strip() != "00":
            continue
        if str(_direct_value(seat, {"seatsaleyn"}) or "Y").strip().upper() == "N":
            continue
        disabled = str(_direct_value(seat, {"isdisabled"}) or "").strip().lower()
        if disabled in {"1", "true", "y", "yes"}:
            continue
        mapped_total += 1
        seat_row = _normalize_seat_row(_direct_value(seat, {"seatrownm"}))
        if seat_row:
            available_rows.add(seat_row)
            if seat_row != "A":
                usable += 1
                seat_number = _normalize_seat_number(
                    _direct_value(seat, {"seatno"})
                )
                if seat_number:
                    available_seats.add((seat_row, seat_number))
                else:
                    seat_numbers_complete = False
        else:
            row_labels_complete = False

    return SeatSnapshot(
        total=scheduled_remaining,
        usable=usable if row_labels_complete else None,
        mapped_total=mapped_total,
        available_rows=(
            tuple(sorted(available_rows))
            if row_labels_complete and mapped_total > 0
            else None
        ),
        available_seats=(
            tuple(sorted(available_seats, key=_seat_location_sort_key))
            if (
                row_labels_complete
                and seat_numbers_complete
                and len(available_seats) == usable
            )
            else None
        ),
    )


class CgvClient:
    """Anonymous CGV schedule client; no login headers or cookie jar."""

    def __init__(self, config: Config):
        self.config = config
        self.ssl_context = ssl.create_default_context()
        self._connections: dict[
            tuple[str, str, int | None], http.client.HTTPConnection
        ] = {}

    def _connection_key(self, url: str) -> tuple[str, str, int | None]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise FetchError("CGV API 주소 형식이 올바르지 않습니다.")
        return parsed.scheme, parsed.hostname, parsed.port

    def _connection_for(self, url: str) -> http.client.HTTPConnection:
        key = self._connection_key(url)
        connection = self._connections.get(key)
        if connection is not None:
            return connection
        scheme, hostname, port = key
        if scheme == "https":
            connection = http.client.HTTPSConnection(
                hostname,
                port=port,
                timeout=self.config.request_timeout_seconds,
                context=self.ssl_context,
            )
        else:
            connection = http.client.HTTPConnection(
                hostname,
                port=port,
                timeout=self.config.request_timeout_seconds,
            )
        self._connections[key] = connection
        return connection

    def _drop_connection(self, url: str) -> None:
        connection = self._connections.pop(self._connection_key(url), None)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _request(
        self, url: str, *, headers: Mapping[str, str]
    ) -> tuple[int, bytes, str, str]:
        parsed = urllib.parse.urlsplit(url)
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        last_error: Exception | None = None
        for attempt in range(2):
            connection = self._connection_for(url)
            try:
                connection.request("GET", target, headers=dict(headers))
                response = connection.getresponse()
                body = response.read(5_000_001)
                status = response.status
                content_type = response.getheader("Content-Type", "")
                location = response.getheader("Location", "")
                if response.getheader("Connection", "").lower() == "close":
                    self._drop_connection(url)
                return status, body, content_type, location
            except (http.client.HTTPException, TimeoutError, OSError) as exc:
                last_error = exc
                self._drop_connection(url)
                if attempt == 0:
                    continue
        assert last_error is not None
        raise last_error

    def _get_json(self, url: str, *, referer: str) -> Any:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "ko-KR",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": referer,
            "User-Agent": USER_AGENT,
        }

        current_url = url
        try:
            for redirect_count in range(4):
                status, body, content_type, location = self._request(
                    current_url, headers=headers
                )
                if status not in {301, 302, 303, 307, 308}:
                    break
                if redirect_count == 3 or not location:
                    raise FetchError("CGV API 주소 전환이 너무 많습니다.")
                current_url = urllib.parse.urljoin(current_url, location)
            else:  # pragma: no cover - the redirect guard above always exits.
                raise FetchError("CGV API 주소 전환에 실패했습니다.")
        except FetchError:
            raise
        except (http.client.HTTPException, TimeoutError, OSError) as exc:
            reason_text = str(exc)
            reason_text = re.sub(r"https?://\S+", "CGV 주소", reason_text)
            raise FetchError(f"CGV 연결 실패: {reason_text[:180]}") from exc

        if len(body) > 5_000_000:
            self._drop_connection(current_url)
            raise FetchError("CGV 응답 크기가 안전 제한을 초과했습니다.")
        if status == 403:
            error_body = body[:30_000].decode("utf-8", errors="replace")
            if (
                "비정상적으로 CGV에 접속" in error_body
                or "cloudflare" in error_body.lower()
            ):
                raise FetchError(
                    "CGV가 자동 조회를 차단했습니다(HTTP 403). "
                    "잠시 후 다시 시도하거나 네트워크를 바꿔 보세요."
                )
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
            raise TelegramError(
                f"Telegram 응답 오류: HTTP {exc.code}{suffix}",
                status_code=exc.code,
                description=detail,
            ) from exc
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
            raise TelegramError(
                f"Telegram 전송 실패: {description}", description=description
            )

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
            "failed_schedule_dates": {},
            "deferred": {},
            "seat_alerts": {},
            "pending_deliveries": {},
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
            if not isinstance(loaded.get("deferred", {}), dict):
                raise RuntimeError(f"보류 상태 파일 형식이 올바르지 않습니다: {self.path}")
            if not isinstance(loaded.get("failed_schedule_dates", {}), dict):
                raise RuntimeError(f"일정 재조회 상태 파일 형식이 올바르지 않습니다: {self.path}")
            if not isinstance(loaded.get("pending_deliveries", {}), dict):
                raise RuntimeError(f"재전송 상태 파일 형식이 올바르지 않습니다: {self.path}")
            self.data.update(loaded)
            self.data["version"] = STATE_VERSION
            self.data.setdefault("seat_counts", {})
            self.data.setdefault("subscribers", {})
            self.data.setdefault("subscribers_initialized", False)
            self.data.setdefault("telegram_update_offset", 0)
            self.data.setdefault("frontier_date", "")
            self.data.setdefault("failed_schedule_dates", {})
            self.data.setdefault("deferred", {})
            self.data.setdefault("seat_alerts", {})
            self.data.setdefault("pending_deliveries", {})

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
            for bucket in (
                "notified",
                "seat_counts",
                "deferred",
                "failed_schedule_dates",
                "seat_alerts",
            ):
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

    def seat_selection(self, chat_id: str) -> str:
        """Return the subscriber's preferred-seat preset."""

        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return DEFAULT_SEAT_SELECTION
            selection = record.get("seat_selection")
            if isinstance(selection, str) and selection in SEAT_SELECTIONS:
                return selection
            return DEFAULT_SEAT_SELECTION

    def set_seat_selection(self, chat_id: str, selection: str) -> bool:
        """Store a preferred-seat preset; returns False when unchanged."""

        if selection not in SEAT_SELECTIONS:
            raise ValueError(f"알 수 없는 좌석 선택: {selection}")
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return False
            if record.get("seat_selection", DEFAULT_SEAT_SELECTION) == selection:
                return False
            updated = dict(record)
            updated["seat_selection"] = selection
            self.data["subscribers"][str(chat_id)] = updated
            return True

    @staticmethod
    def _stored_min_seats(record: Mapping[str, Any]) -> int:
        # "min_cancel" is what this was called while it measured how many
        # seats freed up at once.  Subscribers who set it then keep their
        # choice under the name that describes what it now measures.
        for field in ("min_seats", "min_cancel"):
            minimum = record.get(field)
            if isinstance(minimum, int) and minimum in MIN_SEATS_CHOICES:
                return minimum
        return MIN_SEATS_DEFAULT

    def min_seats(self, chat_id: str) -> int:
        """Seats that must be on sale before this subscriber is alerted."""

        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return MIN_SEATS_DEFAULT
            return self._stored_min_seats(record)

    def set_min_seats(self, chat_id: str, minimum: int) -> bool:
        """Store a minimum seat count; returns False when unchanged."""

        if minimum not in MIN_SEATS_CHOICES:
            raise ValueError(f"알 수 없는 예매 가능 최소 좌석: {minimum}")
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return False
            if self._stored_min_seats(record) == minimum:
                return False
            updated = dict(record)
            updated.pop("min_cancel", None)
            updated["min_seats"] = minimum
            self.data["subscribers"][str(chat_id)] = updated
            return True

    def seat_alert_is_due(
        self, key: str, now: dt.datetime, repeat_minutes: int
    ) -> bool:
        """Whether this showing may be re-announced while seats stay on sale.

        With the interval at zero every cycle sends, which is the point of a
        stock alert.  Raising it is the one lever that shortens the queue if
        an opening floods, and it only throttles repeats — the caller sends a
        genuine change regardless.
        """

        if repeat_minutes <= 0:
            return True
        with self._lock:
            raw = self.data.setdefault("seat_alerts", {}).get(str(key))
        if not isinstance(raw, str):
            return True
        try:
            last = dt.datetime.fromisoformat(raw)
        except ValueError:
            return True
        return now - last >= dt.timedelta(minutes=repeat_minutes)

    def note_seat_alert(self, key: str, now: dt.datetime) -> None:
        with self._lock:
            self.data.setdefault("seat_alerts", {})[str(key)] = now.isoformat()

    def note_deferred(self, key: str, total: int, skips: int) -> bool:
        """Remember that a showing could not be classified at this seat count.

        CGV's seat map is readable or it is not; asking again sixty seconds
        later almost never changes the answer, so record how many cycles to
        coast before spending another request on it.
        """

        with self._lock:
            bucket = self.data.setdefault("deferred", {})
            record = {"total": int(total), "skips": max(0, int(skips))}
            if bucket.get(key) == record:
                return False
            bucket[key] = record
            return True

    def clear_deferred(self, key: str) -> bool:
        with self._lock:
            bucket = self.data.setdefault("deferred", {})
            return bucket.pop(key, None) is not None

    def take_deferred_skip(self, key: str, remaining: int) -> bool:
        """True when this cycle should coast instead of re-reading seat detail.

        A changed seat count always earns a fresh look — something happened,
        and the new count may well be classifiable.
        """

        with self._lock:
            bucket = self.data.setdefault("deferred", {})
            record = bucket.get(key)
            if not isinstance(record, Mapping):
                return False
            if record.get("total") != remaining:
                bucket.pop(key, None)
                return False
            skips = int(record.get("skips", 0))
            if skips <= 0:
                return False
            bucket[key] = {"total": remaining, "skips": skips - 1}
            return True

    def queue_pending_delivery(self, chat_id: str, text: str, category: str) -> bool:
        """Remember a failed Telegram delivery without duplicating the queue."""

        key_source = f"{chat_id}\0{category}\0{text}"
        key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
        with self._lock:
            bucket = self.data.setdefault("pending_deliveries", {})
            if key in bucket:
                return False
            bucket[key] = {
                "chat_id": str(chat_id),
                "text": text,
                "category": category,
                "queued_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "attempts": 0,
            }
            return True

    def drop_pending_for_chat(self, chat_id: str) -> int:
        """Discard every queued retry aimed at one chat."""

        target = str(chat_id)
        with self._lock:
            bucket = self.data.setdefault("pending_deliveries", {})
            keys = [
                key
                for key, record in bucket.items()
                if isinstance(record, Mapping) and record.get("chat_id") == target
            ]
            for key in keys:
                bucket.pop(key, None)
            return len(keys)

    def note_delivery_attempt(self, key: str, max_attempts: int) -> bool:
        """Count a failed retry; returns True once the entry is given up on."""

        with self._lock:
            bucket = self.data.setdefault("pending_deliveries", {})
            record = bucket.get(key)
            if not isinstance(record, Mapping):
                return False
            attempts = int(record.get("attempts", 0)) + 1
            if attempts >= max_attempts:
                bucket.pop(key, None)
                return True
            updated = dict(record)
            updated["attempts"] = attempts
            bucket[key] = updated
            return False

    def prune_pending_deliveries(self, now: dt.datetime) -> int:
        """Drop queued retries older than the TTL, whatever their attempt count.

        Attempts only tick when a retry actually runs, so a stalled watcher
        could otherwise hold a queue entry indefinitely.
        """

        cutoff = now - dt.timedelta(hours=PENDING_DELIVERY_TTL_HOURS)
        removed = 0
        with self._lock:
            bucket = self.data.setdefault("pending_deliveries", {})
            for key in list(bucket):
                record = bucket.get(key)
                queued_at = None
                if isinstance(record, Mapping):
                    try:
                        queued_at = dt.datetime.fromisoformat(
                            str(record.get("queued_at", ""))
                        )
                    except ValueError:
                        queued_at = None
                if queued_at is None:
                    # Unreadable timestamp: drop it rather than keep it forever.
                    bucket.pop(key, None)
                    removed += 1
                    continue
                if queued_at.tzinfo is None:
                    queued_at = queued_at.replace(tzinfo=dt.timezone.utc)
                if queued_at < cutoff:
                    bucket.pop(key, None)
                    removed += 1
        return removed

    def note_schedule_failure(self, show_date: dt.date) -> bool:
        """Persist a failed or skipped date so the next cycle prioritizes it."""

        date_text = show_date.isoformat()
        with self._lock:
            bucket = self.data.setdefault("failed_schedule_dates", {})
            if date_text in bucket:
                return False
            bucket[date_text] = {
                "date": date_text,
                "failed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            return True

    def clear_schedule_failure(self, show_date: dt.date) -> bool:
        with self._lock:
            bucket = self.data.setdefault("failed_schedule_dates", {})
            return bucket.pop(show_date.isoformat(), None) is not None

    def failed_schedule_dates(self) -> tuple[dt.date, ...]:
        with self._lock:
            bucket = self.data.setdefault("failed_schedule_dates", {})
            dates: list[dt.date] = []
            for date_text in bucket:
                try:
                    dates.append(dt.date.fromisoformat(str(date_text)))
                except ValueError:
                    continue
            return tuple(sorted(dates))

    def pending_deliveries(self) -> tuple[tuple[str, dict[str, str]], ...]:
        """Return a stable copy so Telegram calls happen outside the state lock."""

        with self._lock:
            records: list[tuple[str, dict[str, str]]] = []
            for key, raw in self.data.setdefault("pending_deliveries", {}).items():
                if not isinstance(raw, Mapping):
                    continue
                chat_id = raw.get("chat_id")
                text = raw.get("text")
                category = raw.get("category")
                if all(
                    isinstance(value, str) and value
                    for value in (chat_id, text, category)
                ):
                    records.append(
                        (
                            str(key),
                            {
                                "chat_id": chat_id,
                                "text": text,
                                "category": category,
                            },
                        )
                    )
            return tuple(records)

    def remove_pending_delivery(self, key: str) -> bool:
        with self._lock:
            bucket = self.data.setdefault("pending_deliveries", {})
            return bucket.pop(str(key), None) is not None

    def subscriber_breakdown(self) -> dict[str, Any]:
        """Counts of who is subscribed and how they configured their alerts."""

        with self._lock:
            modes = {mode: 0 for mode in ALERT_MODES}
            chat_types: dict[str, int] = {}
            seat_selections = {selection: 0 for selection in SEAT_SELECTIONS}
            min_seats_counts = {minimum: 0 for minimum in MIN_SEATS_CHOICES}
            for chat_id, record in self.data["subscribers"].items():
                modes[self.alert_mode(chat_id)] += 1
                seat_selections[self.seat_selection(chat_id)] += 1
                min_seats_counts[self.min_seats(chat_id)] += 1
                kind = ""
                if isinstance(record, Mapping):
                    kind = str(record.get("chat_type") or "")
                chat_types[kind or "unknown"] = chat_types.get(kind or "unknown", 0) + 1
            return {
                "total": len(self.data["subscribers"]),
                "modes": modes,
                "seat_selections": seat_selections,
                "min_seats": min_seats_counts,
                "chat_types": chat_types,
            }

    def subscriber_ids_for(
        self, category: str, *, seats_available: int | None = None
    ) -> tuple[str, ...]:
        """Subscribers who opted in to this alert category.

        ``seats_available`` is how many seats this alert says are on sale,
        counted inside whatever scope the category names.  Passing it applies
        each subscriber's minimum; leaving it None means the alert is not
        about seat availability, so nobody is filtered out.
        """

        def wants_this_many(chat_id: str) -> bool:
            minimum = self.min_seats(chat_id)
            # The default is not a threshold of one — it is no threshold at
            # all, so an alert reaches everyone who never set a minimum even
            # when the seat count could not be read.
            if seats_available is None or minimum == MIN_SEATS_DEFAULT:
                return True
            return minimum <= seats_available

        if category == ALERT_SYSTEM:
            # Routed by the caller, which knows who the operator is.
            return ()
        if category == ALERT_SEATS_UNCLASSIFIED:
            # Sweetness cannot be judged without a readable row, so an
            # unclassified alert only ever concerns whole-auditorium
            # subscribers.
            return self.subscriber_ids_for(
                ALERT_SEATS, seats_available=seats_available
            )
        if category in {ALERT_SEATS, ALERT_SEATS_SWEET}:
            selection = (
                SEAT_SELECTION_SWEET
                if category == ALERT_SEATS_SWEET
                else SEAT_SELECTION_ALL
            )
            with self._lock:
                return tuple(
                    str(chat_id)
                    for chat_id in self.data["subscribers"]
                    if ALERT_SEATS in ALERT_MODES[self.alert_mode(chat_id)]
                    and self.seat_selection(chat_id) == selection
                    and wants_this_many(chat_id)
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
                "seat_selection": DEFAULT_SEAT_SELECTION,
                "min_seats": MIN_SEATS_DEFAULT,
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
            raw_seats = raw.get("available_seats")
            available_seats = None
            if isinstance(raw_seats, list) and all(
                isinstance(location, list)
                and len(location) == 2
                and all(isinstance(part, str) for part in location)
                for location in raw_seats
            ):
                available_seats = tuple(
                    (location[0], location[1]) for location in raw_seats
                )
            return SeatSnapshot(
                total=total,
                usable=_nonnegative_int(raw.get("usable")),
                mapped_total=_nonnegative_int(raw.get("mapped_total")),
                available_rows=available_rows,
                available_seats=available_seats,
            )

    def set_seat_snapshot(self, key: str, snapshot: SeatSnapshot) -> None:
        with self._lock:
            self.data["seat_counts"][key] = {
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "total": snapshot.total,
                "usable": snapshot.usable,
                "mapped_total": snapshot.mapped_total,
                "available_rows": (
                    list(snapshot.available_rows)
                    if snapshot.available_rows is not None
                    else None
                ),
                "available_seats": (
                    [list(location) for location in snapshot.available_seats]
                    if snapshot.available_seats is not None
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
    if session.remaining_seats == 0:
        # Announced anyway, but say so plainly rather than sending someone to
        # a booking page with nothing on it.
        line += " (매진 · 취소표 나오면 알림)"
    if seat_detail_unclassified:
        line += " ⚠️ A열 여부 미확인 · 전체 잔여 수 기준"
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
    """Build a CGV booking URL with every shareable selection preselected."""

    split = urllib.parse.urlsplit(config.booking_url)
    query = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
    query.update(
        {
            "coCd": config.company_code,
            "siteNo": config.site_no,
            "siteNm": config.site_name,
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
        lines = [
            "🎟️ 예매 바로가기 (영화·극장·날짜 선택됨): "
            f"{next(iter(links.values()))}"
        ]
    else:
        lines = [
            f"🎟️ 예매 바로가기 ({show_date} · 영화·극장·날짜 선택됨): {url}"
            for show_date, url in links.items()
        ]
    return "\n\n" + "\n".join(lines)


def _seat_snapshot_changed(previous: SeatSnapshot, current: SeatSnapshot) -> bool:
    if previous.total != current.total:
        return True
    if (
        previous.usable is not None
        and current.usable is not None
        and previous.usable != current.usable
    ):
        return True
    if (
        previous.available_seats is not None
        and current.available_seats is not None
        and previous.available_seats != current.available_seats
    ):
        return True
    return False


def _compact_seat_numbers(numbers: Iterable[str]) -> str:
    unique = sorted(set(numbers), key=_natural_sort_key)
    numeric = sorted({int(number) for number in unique if number.isdigit()})
    nonnumeric = [number for number in unique if not number.isdigit()]
    parts: list[str] = []
    if numeric:
        start = end = numeric[0]
        for number in numeric[1:]:
            if number == end + 1:
                end = number
                continue
            parts.append(str(start) if start == end else f"{start}~{end}")
            start = end = number
        parts.append(str(start) if start == end else f"{start}~{end}")
    parts.extend(nonnumeric)
    return ", ".join(parts)


def _sweet_seat_snapshot(snapshot: SeatSnapshot) -> SeatSnapshot | None:
    """Return only seats inside the combined Sweet/Experienced/Extremer area."""

    if not snapshot.seat_map_complete or snapshot.available_seats is None:
        return None
    selected = tuple(
        (row, number)
        for row, number in snapshot.available_seats
        if number.isdigit()
        and row in SWEET_SEAT_RANGES
        and SWEET_SEAT_RANGES[row][0] <= int(number) <= SWEET_SEAT_RANGES[row][1]
    )
    selected_rows = tuple(
        sorted({row for row, _number in selected}, key=_natural_sort_key)
    )
    return SeatSnapshot(
        total=snapshot.total,
        usable=len(selected),
        mapped_total=snapshot.total,
        available_rows=selected_rows,
        available_seats=selected,
    )


def _available_seat_count(snapshot: SeatSnapshot) -> int:
    """Seats outside row A that a subscriber could book right now.

    Only confirmed seats count.  The schedule total includes row A, which
    this audience excluded, so it can never stand in: eight seats left with
    an unreadable map might be eight row-A seats.
    """

    return snapshot.usable if snapshot.usable is not None else 0


def _sweet_seats_available(
    previous: SeatSnapshot | None, current: SeatSnapshot
) -> tuple[SeatSnapshot | None, SeatSnapshot] | None:
    """Snapshots to alert on while a sweet seat is on sale, or None."""

    current_sweet = _sweet_seat_snapshot(current)
    if current_sweet is None or not current_sweet.available_seats:
        return None
    return (
        _sweet_seat_snapshot(previous) if previous is not None else None
    ), current_sweet


def _available_seat_line(
    snapshot: SeatSnapshot | None,
    *,
    label: str = "A열 제외 잔여 좌석",
    max_chars: int = 900,
) -> str | None:
    if snapshot is None or not snapshot.seat_map_complete:
        return None
    non_a_rows = tuple(
        sorted(
            (row for row in snapshot.available_rows or () if row != "A"),
            key=_natural_sort_key,
        )
    )
    if not non_a_rows:
        return None
    if snapshot.available_seats is None:
        rows = ", ".join(f"{row}열" for row in non_a_rows)
        return f"{label}: {rows} (좌석 번호 미확인)"

    seats_by_row: dict[str, list[str]] = {}
    for row, number in snapshot.available_seats:
        seats_by_row.setdefault(row, []).append(number)
    details = " / ".join(
        f"{row}{_compact_seat_numbers(seats_by_row.get(row, ()))}"
        for row in non_a_rows
    )
    line = f"{label}: {details}"
    if len(line) <= max_chars:
        return line

    counts = " / ".join(
        f"{row}열 {len(seats_by_row.get(row, ()))}석" for row in non_a_rows
    )
    return f"{label}: {counts} (좌석 번호가 많아 행별 수량으로 요약)"


def seat_change_message(
    session: BookingSession,
    previous: SeatSnapshot | None,
    current: SeatSnapshot,
    config: Config,
    *,
    availability: tuple[SeatSnapshot | None, SeatSnapshot] | None = None,
    scope_label: str = "A열 제외",
) -> str:
    previous_available, current_available = availability or (previous, current)
    ratio = _seat_ratio(session, remaining=current.total)
    # The alert repeats while seats stay on sale, so the previous count is
    # only worth printing when it actually differs.
    if previous is not None and previous.total != current.total:
        ratio += f" (이전 {_seat_ratio(session, remaining=previous.total)})"
    lines = [
        "💺 CGV 예매 가능 좌석",
        _alert_date_banner(session.date),
        f"영화: {config.movie_label} ({config.movie_no})",
        f"극장: {config.site_name} ({config.site_no})",
        "",
        f"상영 시작시간: {session.start_time}",
        f"잔여좌석/총좌석: {ratio}",
    ]
    if (
        previous_available is not None
        and previous_available.usable is not None
        and current_available.usable is not None
        and previous_available.usable != current_available.usable
    ):
        lines.append(
            f"{scope_label} 예매 가능: "
            f"{previous_available.usable}석 → {current_available.usable}석"
        )
    elif current_available.usable is not None:
        lines.append(f"{scope_label} 예매 가능: {current_available.usable}석")
    if seat_line := _available_seat_line(
        current_available, label=f"{scope_label} 잔여 좌석"
    ):
        lines.append(seat_line)
    if current.uses_unclassified_fallback:
        lines.append("⚠️ A열 여부 미확인 · 전체 잔여 수 기준 알림")
    lines.extend(
        [
            "",
            "🎟️ 예매 바로가기 (영화·극장·날짜 선택됨): "
            f"{booking_url_for_session(session, config)}",
        ]
    )
    return "\n".join(lines)


def message_chunks(
    sessions: Sequence[BookingSession],
    config: Config,
    *,
    unclassified_keys: set[str] | None = None,
    seat_snapshots: Mapping[str, SeatSnapshot] | None = None,
    max_chars: int = 3500,
) -> list[tuple[str, list[BookingSession]]]:
    chunks: list[tuple[str, list[BookingSession]]] = []
    unclassified_keys = unclassified_keys or set()
    seat_snapshots = seat_snapshots or {}
    sessions_by_date: dict[str, list[BookingSession]] = {}
    for session in sessions:
        sessions_by_date.setdefault(session.date, []).append(session)

    def render(date_sessions: Sequence[BookingSession]) -> str:
        show_date = date_sessions[0].date
        lines = []
        for session in date_sessions:
            key = session.notification_key(
                site_no=config.site_no, movie_no=config.movie_no
            )
            session_lines = [
                _alert_session_line(
                    session,
                    seat_detail_unclassified=(key in unclassified_keys),
                )
            ]
            if seat_line := _available_seat_line(seat_snapshots.get(key)):
                session_lines.append(f"  {seat_line}")
            lines.append("\n".join(session_lines))
        return (
            "🎟️ CGV 예매 오픈 감지\n"
            + _alert_date_banner(show_date)
            + "\n"
            + f"영화: {config.movie_label} ({config.movie_no})\n"
            + f"극장: {config.site_name} ({config.site_no})\n\n"
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
    deferred_rechecks_skipped: int = 0


@dataclasses.dataclass(frozen=True)
class ScanPlan:
    """Which dates one cycle requests, and how far it may widen."""

    dates: tuple[dt.date, ...]
    full_scan: bool
    # Last already-open date. Anything past it is probe territory: a session
    # found there means a new booking opening.
    open_end: dt.date | None = None
    window_end: dt.date | None = None
    probe_end: dt.date | None = None

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
    suppressed_row_a_only: int = 0
    suppressed_sold_out: int = 0
    suppressed_closed: int = 0
    unclassified_fallback_alerts: int = 0
    seat_detail_errors: int = 0
    seat_detail_error_sample: str = ""
    rate_limited_requests: int = 0
    schedule_skipped_dates: int = 0
    seat_detail_skipped: int = 0
    deferred_rechecks_skipped: int = 0
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
        self._command_poll_failures = 0
        self.state.load()
        if not self.state.subscribers_initialized:
            self.state.initialize_subscribers(config.telegram_chat_id)
            if not self.dry_run:
                self.state.save()

    def _plan_scan(self, today: dt.date) -> ScanPlan:
        """Choose the dates to request this cycle.

        Dates just past the observed booking frontier come first because a hit
        there is a new booking opening.  Already-open dates follow for seat
        changes; a periodic full scan appends the rest of the window.
        """

        window = self.config.target_dates(today=today)
        if not window:
            return ScanPlan(dates=tuple(window), full_scan=True)

        retry_set = set(self.state.failed_schedule_dates())
        retry_dates = [date for date in window if date in retry_set]

        def ordered_unique(*groups: Sequence[dt.date]) -> tuple[dt.date, ...]:
            seen: set[dt.date] = set()
            ordered: list[dt.date] = []
            for group in groups:
                for date in group:
                    if date not in seen:
                        seen.add(date)
                        ordered.append(date)
            return tuple(ordered)

        frontier = self.state.frontier_date
        # No frontier yet (first run, or a wiped volume) means there is nothing
        # to probe past, so the window has to be swept once to establish one.
        if frontier is None:
            return ScanPlan(
                dates=ordered_unique(retry_dates, window), full_scan=True
            )

        window_end = window[-1]
        # Yesterday as the floor keeps the probe anchored to today once every
        # observed show has played.
        open_end = min(max(frontier, today - dt.timedelta(days=1)), window_end)
        probe_end = min(
            open_end + dt.timedelta(days=self.config.cursor_probe_days), window_end
        )
        probe_dates = [date for date in window if open_end < date <= probe_end]
        # After probing just beyond the booking frontier, check known-open dates
        # from today forwards. Cancellation tickets matter most for screenings
        # that are closest to starting.
        open_dates = [date for date in window if date <= open_end]
        remaining_dates = [date for date in window if date > probe_end]
        full_scan = (
            self.config.scan_mode == SCAN_MODE_FULL
            or self._cycle_index % self.config.full_scan_every_cycles == 0
        )
        if full_scan:
            return ScanPlan(
                dates=ordered_unique(
                    probe_dates, retry_dates, remaining_dates, open_dates
                ),
                full_scan=True,
                probe_end=probe_end,
            )
        return ScanPlan(
            dates=ordered_unique(probe_dates, retry_dates, open_dates),
            full_scan=False,
            open_end=open_end,
            window_end=window_end,
            probe_end=probe_end,
        )

    def _walk_new_range(
        self,
        plan: ScanPlan,
        probe_dates: Sequence[dt.date],
        errors: dict[dt.date, str],
        tally: "_CycleTally",
    ) -> list[dt.date]:
        """Follow a newly opened range forward until a date has no showings.

        Booking opens as a contiguous block, so the first empty date past the
        probe marks where it ends. Walking one day at a time costs a couple of
        requests for a short opening instead of sweeping three weeks of empty
        dates, and it returns to today's cancellation tickets that much sooner.

        The empty date is still requested — that request is how the end is
        recognised — so the walk always overshoots by exactly one day.
        """

        if plan.window_end is None or not probe_dates:
            return []
        walked: list[dt.date] = []
        cursor = max(probe_dates) + dt.timedelta(days=1)
        # Purely a backstop; the empty date normally ends the walk first.
        limit = self.config.cursor_expansion_days
        while cursor <= plan.window_end and len(walked) < limit:
            if tally.rate_limited:
                break
            seen_before = tally.latest_session_date
            self._scan_and_alert([cursor], errors, tally)
            walked.append(cursor)
            if tally.latest_session_date == seen_before:
                break
            cursor += dt.timedelta(days=1)
        return walked

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

    def _operator_recipients(self) -> tuple[str, ...]:
        """The one chat that gets fetch-failure notices, if it is configured.

        Deliberately not gated on subscription: this is an operations notice
        addressed to whoever runs the bot, not something anyone opted into.
        """

        operator = str(self.config.telegram_chat_id or "").strip()
        return (operator,) if operator else ()

    def _broadcast_message(
        self,
        text: str,
        *,
        category: str = ALERT_SYSTEM,
        seats_available: int | None = None,
        recipients: Sequence[str] | None = None,
    ) -> tuple[int, int, int]:
        """Send to subscribers opted in to ``category``.

        The returned total counts only those recipients, so callers that gate
        state updates on delivery still advance when nobody wants this
        category (total == 0) instead of retrying forever.

        ``recipients`` addresses named chats instead, for a message that is
        not a subscription at all.
        """

        subscriber_ids = (
            tuple(recipients)
            if recipients is not None
            else self.state.subscriber_ids_for(
                category, seats_available=seats_available
            )
        )
        delivered = 0
        failed = 0

        def send(chat_id: str) -> tuple[str, TelegramError | None]:
            try:
                self.telegram.send_message(text, chat_id=chat_id)
            except TelegramError as exc:
                return chat_id, exc
            return chat_id, None

        results: list[tuple[str, TelegramError | None]] = []
        if subscriber_ids:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(TELEGRAM_BROADCAST_WORKERS, len(subscriber_ids)),
                thread_name_prefix="telegram-broadcast",
            ) as executor:
                results = list(executor.map(send, subscriber_ids))

        for chat_id, error in results:
            if error is not None:
                failed += 1
                if error.recipient_gone:
                    # Blocked or deleted chats never recover, and they cannot
                    # send /stop to remove themselves, so drop them here.
                    self._drop_unreachable_subscriber(chat_id, error)
                else:
                    self.state.queue_pending_delivery(chat_id, text, category)
                    self.logger.error(
                        "전송 실패(다음 주기 재시도): %s", error
                    )
            else:
                delivered += 1
        if not subscriber_ids:
            if self.state.subscriber_ids():
                self.logger.info(
                    "전송 생략: '%s' 알림을 받는 구독자가 없습니다.", category
                )
            else:
                self.logger.warning(
                    "전송 생략: 등록된 구독자가 없습니다."
                )
        return delivered, failed, len(subscriber_ids)

    def _drop_unreachable_subscriber(
        self, chat_id: str, error: TelegramError
    ) -> None:
        """Unsubscribe a chat Telegram says can never be messaged again."""

        removed = self.state.remove_subscriber(chat_id)
        self.state.drop_pending_for_chat(chat_id)
        if removed:
            self.logger.warning(
                "구독 해지: chat_id=%s 에 더 이상 보낼 수 없어 목록에서 제거했습니다. (%s)",
                chat_id,
                error,
            )
        self.state.save()

    def _retry_pending_deliveries(
        self,
        pending: Sequence[tuple[str, Mapping[str, str]]] | None = None,
    ) -> None:
        """Retry only recipients who missed an earlier broadcast."""

        if self.dry_run:
            return
        changed = False
        eligible_records: list[tuple[str, Mapping[str, str]]] = []
        for key, record in (
            self.state.pending_deliveries() if pending is None else pending
        ):
            chat_id = record["chat_id"]
            category = record["category"]
            eligible = set(self.state.subscriber_ids_for(category))
            if chat_id not in eligible:
                changed = self.state.remove_pending_delivery(key) or changed
                continue
            eligible_records.append((key, record))

        def resend(
            item: tuple[str, Mapping[str, str]],
        ) -> tuple[str, str, TelegramError | None]:
            key, record = item
            chat_id = record["chat_id"]
            try:
                self.telegram.send_message(record["text"], chat_id=chat_id)
            except TelegramError as exc:
                return key, chat_id, exc
            return key, chat_id, None

        results: list[tuple[str, str, TelegramError | None]] = []
        if eligible_records:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(
                    TELEGRAM_BROADCAST_WORKERS, len(eligible_records)
                ),
                thread_name_prefix="telegram-retry",
            ) as executor:
                results = list(executor.map(resend, eligible_records))

        for key, chat_id, error in results:
            if error is not None:
                if error.recipient_gone:
                    self._drop_unreachable_subscriber(chat_id, error)
                    changed = True
                    continue
                if self.state.note_delivery_attempt(
                    key, self.config.pending_delivery_max_attempts
                ):
                    changed = True
                    self.logger.warning(
                        "재전송 %d회 실패해 포기: chat_id=%s",
                        self.config.pending_delivery_max_attempts,
                        chat_id,
                    )
                else:
                    changed = True
                    self.logger.warning("재전송 실패: %s", error)
                continue
            changed = self.state.remove_pending_delivery(key) or changed
            self.logger.info("재전송 성공: chat_id=%s", chat_id)
        if changed:
            self.state.save()

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

        lines.append("")
        lines.append("잔여 좌석 대상")
        for selection in (SEAT_SELECTION_ALL, SEAT_SELECTION_SWEET):
            lines.append(
                f"• {SEAT_SELECTION_LABELS[selection]} — "
                f"{stats['seat_selections'][selection]}명"
            )

        lines.append("")
        lines.append("예매 가능 최소 좌석")
        for minimum in MIN_SEATS_CHOICES:
            lines.append(
                f"• {MIN_SEATS_LABELS[minimum]} — "
                f"{stats['min_seats'][minimum]}명"
            )
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

    def _handle_seat_selection_command(
        self, chat_id: str, command: str, argument: str
    ) -> tuple[str, bool]:
        """Return the seat-selection reply and whether its preference changed."""

        requested = SEAT_SELECTION_COMMAND_TARGETS.get(command)
        if requested is None and argument:
            requested = SEAT_SELECTION_ALIASES.get(argument)
            if requested is None:
                return (f"알 수 없는 좌석 선택입니다.\n\n{SEAT_SELECTION_GUIDE}", False)
        if not self.state.is_subscribed(chat_id):
            return (
                "🔕 현재 구독 중이 아닙니다. /start로 구독한 뒤 설정할 수 있습니다."
                f"\n\n{SEAT_SELECTION_GUIDE}",
                False,
            )

        note = ""
        if ALERT_SEATS not in ALERT_MODES[self.state.alert_mode(chat_id)]:
            note = "\n\n참고: 현재 잔여 좌석 알림을 받지 않는 설정입니다. /mode 확인"

        if requested is None:
            current = SEAT_SELECTION_LABELS[self.state.seat_selection(chat_id)]
            return (
                f"💺 현재 잔여 좌석 대상\n→ {current}{note}\n\n{SEAT_SELECTION_GUIDE}",
                False,
            )

        changed = self.state.set_seat_selection(chat_id, requested)
        label = SEAT_SELECTION_LABELS[requested]
        if not changed:
            return (f"🎯 이미 이렇게 설정되어 있습니다.\n→ {label}{note}", False)
        return (f"✅ 좌석 선택을 변경했습니다.\n→ {label}{note}", True)

    def _handle_min_seats_command(
        self, chat_id: str, command: str, argument: str
    ) -> tuple[str, bool]:
        """Return the minimum-cancellation reply and whether it changed."""

        requested = MIN_SEATS_COMMAND_TARGETS.get(command)
        if requested is None and argument:
            requested = MIN_SEATS_ALIASES.get(argument)
            if requested is None:
                return (f"알 수 없는 좌석 수입니다.\n\n{MIN_SEATS_GUIDE}", False)
        if not self.state.is_subscribed(chat_id):
            return (
                "🔕 현재 구독 중이 아닙니다. /start로 구독한 뒤 설정할 수 있습니다."
                f"\n\n{MIN_SEATS_GUIDE}",
                False,
            )

        note = ""
        if ALERT_SEATS not in ALERT_MODES[self.state.alert_mode(chat_id)]:
            note = "\n\n참고: 현재 잔여 좌석 알림을 받지 않는 설정입니다. /mode 확인"

        if requested is None:
            current = MIN_SEATS_LABELS[self.state.min_seats(chat_id)]
            return (
                f"🎫 현재 예매 가능 최소 좌석\n→ {current}{note}\n\n{MIN_SEATS_GUIDE}",
                False,
            )

        changed = self.state.set_min_seats(chat_id, requested)
        label = MIN_SEATS_LABELS[requested]
        if not changed:
            return (f"🎫 이미 이렇게 설정되어 있습니다.\n→ {label}{note}", False)
        return (f"✅ 예매 가능 최소 좌석을 변경했습니다.\n→ {label}{note}", True)

    def sync_subscribers(self) -> bool:
        """Apply Telegram /start and /stop commands to the persistent list.

        Returns False when Telegram could not be reached, so the caller can
        slow down instead of hammering an API that is already failing.
        """

        if self.dry_run or not self.config.subscriptions_enabled:
            return True
        try:
            updates = self.telegram.get_updates(
                offset=self.state.telegram_update_offset
            )
        except TelegramError as exc:
            # One line per outage, not one per poll.  Nothing is lost: the
            # offset only advances on a successful fetch, so every pending
            # command is still queued on Telegram's side.
            self._command_poll_failures += 1
            if self._command_poll_failures == 1:
                self.logger.warning("%s", exc)
            return False
        if self._command_poll_failures:
            self.logger.info(
                "Telegram 명령 조회 복구: 연속 %d회 실패 후 정상화",
                self._command_poll_failures,
            )
            self._command_poll_failures = 0

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
                # Explain the defaults and only the commands that subscribers
                # can see in the BotFather menu.
                reply += (
                    "\n\n🔔 기본 설정"
                    "\n• 신규 예매 오픈 + 예매 가능 좌석 알림"
                    "\n• 잔여 좌석은 모든 A열 제외 좌석"
                    "\n• 1석부터 모두 알림"
                    "\n\n필요할 때만 설정을 바꾸세요."
                    "\n• 알림 종류 선택: /mode"
                    "\n• 잔여 좌석 대상 선택: /seat"
                    "\n• 명당 좌석만 받기: /seat_sweet"
                    "\n• 2석 이상 남았을 때만 받기: /count_2"
                    "\n\n※ 신규 예매 오픈은 좌석 설정과 관계없이 항상 알려드립니다."
                    "\n현재 설정 /status · 자세한 설명 /desc · 해지 /stop"
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
                        selection_label = SEAT_SELECTION_LABELS[
                            self.state.seat_selection(chat_id)
                        ]
                        reply += f"\n잔여 좌석 대상: {selection_label}"
                        reply += (
                            "\n예매 가능 최소 좌석: "
                            f"{MIN_SEATS_LABELS[self.state.min_seats(chat_id)]}"
                        )
                    reply += (
                        "\n\n알림 종류 변경: /mode"
                        "\n잔여 좌석 대상 변경: /seat"
                        "\n예매 가능 최소 좌석 변경: /count"
                    )
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
            elif command in SEAT_SELECTION_COMMANDS:
                reply, seat_selection_changed = self._handle_seat_selection_command(
                    chat_id, command, argument
                )
                state_changed = state_changed or seat_selection_changed
            elif command in MIN_SEATS_COMMANDS:
                reply, min_seats_changed = self._handle_min_seats_command(
                    chat_id, command, argument
                )
                state_changed = state_changed or min_seats_changed
            elif command == ADMIN_STATS_COMMAND and chat_id == str(
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
                    "/status - 현재 구독 및 설정 확인\n"
                    "/mode - 알림 종류 선택\n"
                    "/mode_all - 신규 오픈과 잔여 좌석 모두 받기\n"
                    "/mode_open - 신규 예매 오픈만 받기\n"
                    "/mode_seats - 예매 가능 좌석만 받기\n"
                    "/seat - 잔여 좌석 대상 선택\n"
                    "/seat_all - 모든 A열 제외 좌석 받기 (기본)\n"
                    "/seat_sweet - 명당 좌석만 받기\n"
                    "/count - 예매 가능 최소 좌석 선택\n"
                    "/count_1 - 1석부터 모두 받기 (기본)\n"
                    "/count_2 - 2석 이상 남았을 때만 받기\n"
                    "/desc - 봇 설명과 사용 방법\n"
                    "/coffee - 개발자에게 커피 후원\n"
                    "/help - 전체 명령어 보기\n\n"
                    "/mode · /seat · /count 는 선택 사항입니다.\n"
                    "그대로 두시면 모든 알림을 받습니다."
                )
            elif command in {"/desc", "/description"}:
                reply = (
                    "🎬 용아맥 오디세이 알림 봇\n\n"
                    "CGV 용산아이파크몰 IMAX의 오디세이 예매 오픈과 "
                    "예매 가능한 좌석을 확인해 알려주는 비공식 봇입니다.\n"
                    "한국시간 기준 오늘부터 28일간의 상영 회차를 감시합니다.\n\n"
                    "🔔 알려드리는 내용\n"
                    "• 새 IMAX 상영 회차 예매 오픈\n"
                    "• 예매 가능한 A열 제외 좌석 (취소표 포함)\n"
                    "• 상영일·시작시간·잔여좌석/총좌석·좌석 행/번호·예매 링크\n\n"
                    "🚫 좌석 알림 제외\n"
                    "• A열만 남은 경우\n"
                    "• 잔여 좌석이 0석인 경우\n"
                    "• 상영이 이미 시작된 경우\n"
                    "※ 신규 회차 오픈 알림은 매진이어도 전송\n\n"
                    "⚙️ 기본 설정\n"
                    "• 신규 예매 오픈 + 예매 가능 좌석 알림\n"
                    "• 모든 A열 제외 좌석 알림\n"
                    "• 별도 설정 없이 바로 사용 가능\n\n"
                    "🔧 알림 종류 선택\n"
                    "• /mode — 현재 설정과 선택 방법 확인\n"
                    "• /mode_all — 신규 오픈과 예매 가능 좌석 모두 받기 (기본)\n"
                    "• /mode_open — 신규 예매 오픈만\n"
                    "• /mode_seats — 예매 가능 좌석만\n\n"
                    "💺 잔여 좌석 대상 (선택 사항)\n"
                    "기본값은 모든 A열 제외 좌석입니다.\n"
                    "• /seat_all — 모든 A열 제외 좌석 알림 (기본)\n"
                    "• /seat_sweet — 아래 세 구역만 알림\n"
                    "  Extremer: F16~29, G16~29\n"
                    "  Experienced: H13~32, I13~32\n"
                    "  SweetSpot: J11~34, K11~34, L11~34\n"
                    "• /seat — 현재 좌석 대상 확인\n"
                    "※ 신규 예매 오픈 알림은 좌석 설정과 관계없이 항상 전송\n\n"
                    "🎫 예매 가능 최소 좌석 (선택 사항)\n"
                    "기본값은 1석부터 모두 받기입니다.\n"
                    "• /count_1 — 1석부터 모두 받기 (기본)\n"
                    "• /count_2 — 2석 이상 남아 있을 때만\n"
                    "• /count — 현재 설정 확인\n\n"
                    "📌 사용 방법\n"
                    "1. /start — 알림 구독\n"
                    "2. 명당만 원하면 /seat_sweet (선택 사항)\n"
                    "3. 영화·극장·날짜가 선택된 예매 바로가기 링크 열기\n"
                    "4. CGV 화면에서 IMAX 버튼 선택 후 예매\n\n"
                    "📋 기타 명령어\n"
                    "• /stop — 알림 해지\n"
                    "• /status — 현재 구독 및 설정 확인\n"
                    "• /desc — 봇 설명과 사용 방법\n"
                    "• /coffee — 개발자에게 커피 후원\n"
                    "• /help — 전체 명령어 보기"
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
        return True

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
        """Finish each date completely before moving to the next.

        Probe dates just beyond the booking frontier are still requested first,
        so a new opening is discovered before anything else runs. After that,
        each date is closed out where it is read — schedule, seat detail, alert,
        save — because a cancellation ticket is worth little by the time the
        rest of the window has been swept.
        """

        for index, show_date in enumerate(dates):
            if tally.rate_limited:
                skipped_dates = dates[index:]
                tally.schedule_skipped_dates += len(skipped_dates)
                if not self.dry_run:
                    for skipped_date in skipped_dates:
                        tally.dirty = (
                            self.state.note_schedule_failure(skipped_date)
                            or tally.dirty
                        )
                    self._flush_state(tally)
                break

            payload = None
            self._wait_for_cgv_request_slot()
            try:
                payload = self.cgv.fetch_date(show_date)
            except FetchError as exc:
                message = str(exc)
                errors[show_date] = message
                if not self.dry_run:
                    tally.dirty = (
                        self.state.note_schedule_failure(show_date) or tally.dirty
                    )
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
                if not self.dry_run:
                    tally.dirty = (
                        self.state.note_schedule_failure(show_date) or tally.dirty
                    )
            finally:
                self._mark_cgv_request_finished()

            if payload is None:
                self._flush_state(tally)
                continue
            if not self.dry_run and self.state.clear_schedule_failure(show_date):
                tally.dirty = True
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
                    "제외: %s 상영이 시작돼 예매가 마감된 회차 %d개",
                    show_date.isoformat(),
                    len(closed_sessions),
                )
            tally.matching_sessions += len(sessions)
            if sessions:
                new_sessions = [
                    session
                    for session in sessions
                    if not self.state.was_notified(
                        session.notification_key(
                            site_no=self.config.site_no,
                            movie_no=self.config.movie_no,
                        )
                    )
                ]
                existing_sessions = [
                    session for session in sessions if session not in new_sessions
                ]
                if new_sessions:
                    self._alert_for_sessions(new_sessions, tally)
                    # Persist open alerts before spending time on cancellation
                    # tickets, both for priority and crash-safe de-duplication.
                    self._flush_state(tally)
                if existing_sessions:
                    self._alert_for_sessions(existing_sessions, tally)
            self._flush_state(tally)

    def _forces_seat_recheck(self, show_date: dt.date, today: dt.date) -> bool:
        """Whether this date's seats are re-read even with an unchanged total.

        The schedule API reports only a total, so a booking and a cancellation
        landing in the same minute — an A-row seat sold while a non-A seat
        frees up — leaves the total untouched and the change invisible.
        Re-reading the seat map catches it, but doing that for every date every
        cycle roughly triples the cycle, which would slow down the ordinary
        cancellation alerts this exists to speed up.

        So the nearest days are re-read every cycle, and the days behind them
        take turns: one slice per cycle, a full pass every rotate_cycles.
        """

        offset = (show_date - today).days
        if offset < 0:
            return False
        if offset < self.config.seat_recheck_always_days:
            return True
        if offset < self.config.seat_recheck_rotate_days:
            cycles = self.config.seat_recheck_rotate_cycles
            # Slotting by the date keeps a showing in the same slot across
            # restarts, so no date can be starved by an unlucky ordering.
            return show_date.toordinal() % cycles == self._cycle_index % cycles
        return False

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

        today = self.config.local_today()
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

            if key not in previously_notified:
                # A showing nobody has been told about goes out on the
                # schedule response alone.  Reading its seat map costs one
                # request per showing — twelve seconds on a five-show day
                # between spotting the opening and sending it — and buys
                # nothing: a showing that just opened has its whole auditorium
                # free, so there is no row-A-only case to exclude and no seat
                # list worth printing.  The map gets read on a later cycle,
                # once the count starts moving.
                snapshots[key] = SeatSnapshot(total=remaining)
                continue

            show_date = _parse_show_date(session.date)
            forced_recheck = show_date is not None and self._forces_seat_recheck(
                show_date, today
            )
            previous = previous_snapshots.get(key)
            needs_detail = (
                forced_recheck
                or previous is None
                or previous.total != remaining
                # The booking-open alert went out without reading the map, so
                # the first cycle after it buys the non-A baseline that seat
                # comparisons need.  Until it exists there is nothing to tell
                # a /seat_sweet or /count_2 subscriber apart by.
                or not previous.seat_map_complete
            )
            # A map CGV will not render must not be re-requested every minute,
            # which is what the deferral backoff below is for.
            if (
                needs_detail
                and key in previously_notified
                and not self.dry_run
                and self.state.take_deferred_skip(key, remaining)
            ):
                # Leaving the snapshot unset keeps the showing classified as
                # deferred downstream, exactly as a fresh unreadable map would.
                tally.deferred_keys.add(key)
                tally.deferred_rechecks_skipped += 1
                tally.dirty = True
                continue
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

        if not self.dry_run:
            for session in seat_candidates:
                key = session_keys[session]
                snapshot = snapshots.get(key)
                if snapshot is None:
                    continue
                # "We asked and still have no usable map."  A snapshot that
                # alerts off the schedule total alone counts too: it can carry
                # the alert, but not the non-A baseline, so re-reading it is
                # still worth throttling rather than repeating every cycle.
                unresolved = (
                    key in previously_notified
                    and snapshot.total > 0
                    and not snapshot.should_suppress
                    and not snapshot.seat_map_complete
                )
                changed = (
                    self.state.note_deferred(
                        key,
                        snapshot.total,
                        self.config.deferred_recheck_cycles - 1,
                    )
                    if unresolved
                    else self.state.clear_deferred(key)
                )
                tally.dirty = tally.dirty or changed

        detected_new_sessions = [
            session
            for session in sessions
            if session_keys[session] not in previously_notified
        ]
        # A date and start time never seen before is news on its own.  Being
        # sold out at first sight does not withhold it: the showing exists,
        # the subscriber wants it on their radar, and the cancellation alerts
        # that follow only work once it is on the announced list.  Nothing
        # disqualifies a newly discovered showing.
        new_sessions = [
            session
            for session in detected_new_sessions
            if snapshots.get(session_keys[session]) is not None
        ]

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
                    seat_snapshots=snapshots,
                ):
                    delivered, failed, total = self._broadcast_message(
                        text, category=ALERT_OPEN
                    )
                    for session in chunk_sessions:
                        self.state.mark_notified(session_keys[session], session)
                        verdicts[session_keys[session]] = "발송·예매 오픈"
                        tally.dirty = True
                    tally.new_sessions += len(chunk_sessions)
                    self.logger.info(
                        "전송: 신규 회차 알림 %d개, 성공 %d명, 실패 %d명",
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

            # A session not notified before this cycle was just handled by the
            # booking-open alert above.  It gets no second message, and none
            # of the exclusion rules below may overwrite that verdict.
            if key not in previously_notified:
                verdicts.setdefault(key, "오픈 알림으로 갈음")
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    tally.dirty = True
                continue

            # Most showings sit sold out cycle after cycle now that the alert
            # no longer waits for a change.  Log the moment a showing enters
            # an excluded state, not every minute it stays there; the DEBUG
            # verdict line still carries the full per-cycle census.
            changed = previous is None or _seat_snapshot_changed(
                previous, current
            )

            if current.total == 0:
                verdicts[key] = "제외·매진"
                tally.suppressed_sold_out += 1
                if changed:
                    self.logger.info(
                        "제외: %s 잔여 좌석이 0석입니다.",
                        _session_line(session),
                    )
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    tally.dirty = True
                continue

            if current.should_suppress:
                verdicts[key] = "제외·A열만"
                tally.suppressed_row_a_only += 1
                if changed:
                    self.logger.info(
                        "제외: %s %s",
                        _session_line(session),
                        current.suppression_reason,
                    )
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    tally.dirty = True
                continue

            if not current.alertable:
                verdicts[key] = "보류·A열 여부 미확인"
                tally.deferred_keys.add(key)
                if changed:
                    self.logger.info(
                        "보류: %s A열 여부 판별 실패 후 잔여 6석 이하입니다.",
                        _session_line(session),
                    )
                continue

            # Seats on sale are worth announcing whether or not the count
            # moved since the last look: the subscriber wants to know a seat
            # is bookable now, not that it changed.  A genuine change always
            # goes out; only repeats of an unchanged showing can be throttled.
            if not changed and not self.state.seat_alert_is_due(
                key, self.config.local_now(), self.config.seat_alert_repeat_minutes
            ):
                verdicts.setdefault(key, "대기·재알림 간격")
                if not self.dry_run:
                    self.state.set_seat_snapshot(key, current)
                    tally.dirty = True
                continue

            if current.uses_unclassified_fallback:
                tally.unclassified_fallback_alerts += 1
                self.logger.info(
                    "A열 여부 미확인 알림 허용: %s 잔여 7석 이상입니다.",
                    _session_line(session),
                )

            tally.seat_changes += 1
            if self.dry_run:
                self.logger.info(
                    "드라이런 예매 가능 좌석: %s (%d석)",
                    _session_line(session),
                    current.total,
                )
                continue
            all_category = (
                ALERT_SEATS_UNCLASSIFIED
                if current.uses_unclassified_fallback
                else ALERT_SEATS
            )
            # Each audience is measured against the seats it actually asked
            # about, so two seats on sale outside the sweet area do not meet a
            # sweet subscriber's two-seat minimum.
            open_all = _available_seat_count(current)
            if self.state.subscriber_ids_for(
                all_category, seats_available=open_all
            ):
                delivered, failed, total = self._broadcast_message(
                    seat_change_message(session, previous, current, self.config),
                    category=all_category,
                    seats_available=open_all,
                )
            else:
                delivered = failed = total = 0
            sweet_delivery = _sweet_seats_available(previous, current)
            open_sweet = (
                _available_seat_count(sweet_delivery[1]) if sweet_delivery else 0
            )
            if (
                not current.uses_unclassified_fallback
                and sweet_delivery is not None
                and self.state.subscriber_ids_for(
                    ALERT_SEATS_SWEET, seats_available=open_sweet
                )
            ):
                sweet_delivered, sweet_failed, sweet_total = self._broadcast_message(
                    seat_change_message(
                        session,
                        previous,
                        current,
                        self.config,
                        availability=sweet_delivery,
                        scope_label="명당",
                    ),
                    category=ALERT_SEATS_SWEET,
                    seats_available=open_sweet,
                )
                delivered += sweet_delivered
                failed += sweet_failed
                total += sweet_total
            if delivered or failed or total == 0:
                self.state.set_seat_snapshot(key, current)
                self.state.note_seat_alert(key, self.config.local_now())
                verdicts[key] = f"발송·예매 가능 {current.total}석"
                tally.dirty = True
                self.logger.info(
                    "전송: 예매 가능 좌석 %s (%d석), 성공 %d명, 실패 %d명",
                    _session_line(session),
                    current.total,
                    delivered,
                    failed,
                )

        self._record_verdicts(tally, sessions, verdicts, session_keys, snapshots)

    def run_cycle(self) -> CycleResult:
        # Snapshot before sending anything this cycle. A fresh failure should
        # wait until the next cycle instead of being retried immediately.
        pending_retries = self.state.pending_deliveries()
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
        if not self.dry_run:
            stale_retries = self.state.prune_pending_deliveries(
                self.config.local_now()
            )
            if stale_retries:
                self.logger.info(
                    "재전송 대기 %d건이 %d시간을 넘겨 정리했습니다.",
                    stale_retries,
                    PENDING_DELIVERY_TTL_HOURS,
                )
                tally.dirty = True
        errors: dict[dt.date, str] = {}

        # Cycles run back to back, so without a separator the log reads as one
        # unbroken stream and it is hard to tell where a cycle began.
        self.logger.info(CYCLE_SEPARATOR)
        self.logger.info(
            # The frontier is logged here rather than only when it advances:
            # in steady state it never moves, so it was invisible exactly when
            # someone wants to know how far booking currently reaches.
            "조회 시작: %s 모드, %d일 (%s~%s), 예매 열린 마지막 날 %s",
            "전체" if plan.full_scan else "커서",
            len(dates),
            min(dates).isoformat() if dates else "-",
            max(dates).isoformat() if dates else "-",
            frontier.isoformat() if (frontier := self.state.frontier_date) else "미관측",
        )
        if not plan.full_scan and plan.open_end is not None:
            probe_dates = [date for date in dates if date > plan.open_end]
            open_dates = [date for date in dates if date <= plan.open_end]
            self._scan_and_alert(probe_dates, errors, tally)

            # Expand immediately after a probe hit, before spending any time on
            # cancellation-ticket changes in the already-open range.
            if not tally.rate_limited and plan.probe_hit(tally.latest_session_date):
                walked = self._walk_new_range(plan, probe_dates, errors, tally)
                if walked:
                    self.logger.info(
                        "예매 오픈 감지: %s까지 이어서 조회했습니다(%d일).",
                        walked[-1].isoformat(),
                        len(walked),
                    )
                    dates.extend(walked)
            # Calling this even after a 429 lets _scan_and_alert account for
            # every known-open date skipped by the rate-limit stop.
            self._scan_and_alert(open_dates, errors, tally)
        else:
            self._scan_and_alert(dates, errors, tally)

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
                    error_text,
                    category=ALERT_SYSTEM,
                    recipients=self._operator_recipients(),
                )
                if delivered or _failed or total == 0:
                    self.state.mark_error_notified(fingerprint)
                    tally.dirty = True
        elif self.state.clear_error():
            tally.dirty = True

        self._flush_state(tally)
        # Old Telegram failures must never hold up this cycle's highest-priority
        # work: discovering and announcing a newly opened showing.
        self._retry_pending_deliveries(pending_retries)

        if tally.verdicts:
            self.logger.debug(
                "회차 판정 %d건 | %s",
                len(tally.verdicts),
                " | ".join(tally.verdicts),
            )

        self.logger.info(
            "조회 완료: 성공 %d일, 오류 %d일, IMAX 회차 %d개, 신규 %d개, "
            "예매 가능 좌석 %d개, A열만 남아 제외 %d개, "
            "0석 제외 %d개, 예매 마감 제외 %d개, 좌석판별 대기 %d개, "
            "미판별 7석 이상 알림 %d개, "
            "HTTP 429 %d개, 일정 생략 %d일, 좌석상세 생략 %d개, "
            "보류 재조회 생략 %d개",
            tally.successful_dates,
            len(errors),
            tally.matching_sessions,
            tally.new_sessions,
            tally.seat_changes,
            tally.suppressed_row_a_only,
            tally.suppressed_sold_out,
            tally.suppressed_closed,
            len(tally.deferred_keys),
            tally.unclassified_fallback_alerts,
            tally.rate_limited_requests,
            tally.schedule_skipped_dates,
            tally.seat_detail_skipped,
            tally.deferred_rechecks_skipped,
        )
        return CycleResult(
            successful_dates=tally.successful_dates,
            failed_dates=len(errors),
            matching_sessions=tally.matching_sessions,
            new_sessions=tally.new_sessions,
            seat_changes=tally.seat_changes,
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
            deferred_rechecks_skipped=tally.deferred_rechecks_skipped,
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

    base_delay = max(1, watcher.config.telegram_command_poll_seconds)
    delay = base_delay
    while not stop_event.is_set():
        try:
            reachable = watcher.sync_subscribers()
        except Exception:
            watcher.logger.exception("Telegram 명령 처리 중 예상하지 못한 오류")
            reachable = False
        # Polling every two seconds through a Telegram outage buys nothing —
        # the commands stay queued either way — and a few hundred failed
        # requests an hour is how a bot earns a rate limit of its own.
        delay = (
            base_delay
            if reachable
            else min(delay * 2, TELEGRAM_POLL_BACKOFF_MAX_SECONDS)
        )
        stop_event.wait(delay)


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
                f"대상: {config.movie_label} / {config.site_name} IMAX"
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
