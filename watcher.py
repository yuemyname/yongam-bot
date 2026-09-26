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
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cgv_header_probe import run_header_probe_once
from cgv_wire_trace import WireHeaderTrace
from cgv_public_matrix import run_matrix_once


APP_NAME = "CGV Telegram Watcher"
DEFAULT_API_URL = "https://cgv.co.kr/api/v1/booking/searchSchByMov"
DEFAULT_SEAT_API_URL = "https://cgv.co.kr/api/v1/booking/searchIfSeatData"
DEFAULT_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/movie"
DEFAULT_SEAT_PAGE_URL = "https://cgv.co.kr/cnm/selectVisitorCnt"
DEFAULT_SITE_NAME = "용산아이파크몰"
UNCLASSIFIED_ALERT_MIN_SEATS = 7
STATE_VERSION = 15
DEFAULT_TELEGRAM_BROADCAST_WORKERS = 16
# Telegram documents a free broadcast ceiling of about 30 messages/second.
# Leave headroom for command replies and transient timing differences.
DEFAULT_TELEGRAM_BROADCAST_RATE_PER_SECOND = 25
# Ceiling for the command-poll backoff while Telegram is unreachable.
TELEGRAM_POLL_BACKOFF_MAX_SECONDS = 60
CGV_RECOVERY_PAUSE_SECONDS = 3600
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
# The sender takes at most one second of work before looking at the durable
# queue again.  That lets a newly discovered opening jump ahead of an older
# seat-change backlog even when hundreds of people are subscribed.
DELIVERY_RETRY_BACKOFF_MAX_SECONDS = 300
TELEGRAM_FLOOD_WAIT_FALLBACK_SECONDS = 60
TELEGRAM_FLOOD_WAIT_MARGIN_SECONDS = 1

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
# An announcement the operator typed.  Everyone subscribed gets it, whatever
# they set /mode to — it is not one of the alerts those settings filter.
ALERT_NOTICE = "notice"
# Direct command replies are addressed even to unsubscribed chats.
ALERT_REPLY = "reply"

# Smaller numbers leave the durable Telegram outbox first.  Booking openings
# are the reason this bot exists, so they always pre-empt seat changes and
# operator announcements at the next short sender batch boundary.
DELIVERY_CATEGORY_PRIORITIES = {
    ALERT_OPEN: 0,
    ALERT_REPLY: 5,
    ALERT_SEATS: 10,
    ALERT_SEATS_SWEET: 10,
    ALERT_SEATS_UNCLASSIFIED: 10,
    ALERT_NOTICE: 20,
    ALERT_SYSTEM: 20,
}
DEFAULT_DELIVERY_PRIORITY = 30
ROTATING_ALERT_CATEGORIES = frozenset({
    ALERT_OPEN, ALERT_SEATS, ALERT_SEATS_SWEET, ALERT_SEATS_UNCLASSIFIED, ALERT_NOTICE,
})

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

# Which show dates a subscriber wants. This is based on the movie's show date,
# not the day when the bot happens to discover the opening.
SHOW_DAY_ALL = "all"
SHOW_DAY_WEEKEND = "weekend"
DEFAULT_SHOW_DAY = SHOW_DAY_ALL
SHOW_DAY_SELECTIONS = (SHOW_DAY_ALL, SHOW_DAY_WEEKEND)
SHOW_DAY_LABELS = {
    SHOW_DAY_ALL: "모든 요일 상영분",
    SHOW_DAY_WEEKEND: "주말(토·일) 상영분만",
}
SHOW_DAY_COMMAND_TARGETS = {
    "/day_all": SHOW_DAY_ALL,
    "/day_weekend": SHOW_DAY_WEEKEND,
}
SHOW_DAY_COMMANDS = {"/day", *SHOW_DAY_COMMAND_TARGETS}
SHOW_DAY_ALIASES = {
    "all": SHOW_DAY_ALL,
    "전체": SHOW_DAY_ALL,
    "모두": SHOW_DAY_ALL,
    "weekend": SHOW_DAY_WEEKEND,
    "주말": SHOW_DAY_WEEKEND,
    "토일": SHOW_DAY_WEEKEND,
}
SHOW_DAY_GUIDE = (
    "알림을 받을 상영일을 고를 수 있습니다.\n"
    "/day_all - 모든 요일 상영분 받기 (기본)\n"
    "/day_weekend - 토·일 상영분만 받기\n\n"
    "알림이 도착한 요일이 아니라 영화 상영일 기준입니다."
)

# Clock-time preference is deliberately separate from show-day selection:
# openings bypass it, but every seat alert (including fallback) uses it.
SEAT_TIME_COMMANDS = {"/time", "/time_all"}
SEAT_TIME_NOTE = "신규 오픈 알림에는 시간 제한을 적용하지 않습니다. (기존 알림 종류·요일 설정은 유지)"
SEAT_TIME_GUIDE = (
    "잔여좌석 알림을 받을 상영 시작시간을 설정합니다. (한국시간)\n"
    "/time 18:00 23:59 - 저녁 상영만\n"
    "/time 22:00 02:00 - 심야 상영만 (자정 통과)\n"
    "/time_all - 전체 시간 받기 (기본)\n\n"
    "시작·끝 시각을 모두 포함합니다. 알림 수신 시간이 아닌 영화 시작시간 기준입니다.\n"
    + SEAT_TIME_NOTE
)


def _clock_minutes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value)
    return int(match[1]) * 60 + int(match[2]) if match else None


def _seat_time_label(time_range: tuple[str, str] | None) -> str:
    if time_range is None:
        return "전체 시간"
    start, end = time_range
    return f"{start}~{end}" + (" (자정 통과)" if start > end else "")


SEAT_DATE_COMMANDS = {"/date", "/date_all"}
SEAT_DATE_NOTE = "신규 오픈 알림에는 날짜 제한을 적용하지 않습니다. (기존 알림 종류·요일 설정은 유지)"
SEAT_DATE_GUIDE = (
    "잔여좌석 알림을 받을 영화 상영일을 설정합니다.\n"
    "/date 20261003 - 10월 3일 포함 이후\n"
    "/date 20261003 20261005 - 10월 3~5일 (양 끝 포함)\n"
    "/date_all - 날짜 제한 해제 (기본)\n\n"
    "날짜는 하이픈 없이 YYYYMMDD로 입력하세요. 알림 도착일이 아닌 CGV 상영일 기준입니다.\n"
    "조회 범위는 기존처럼 오늘부터 28일이며, 기간이 지나도 설정은 자동 해제되지 않습니다.\n"
    + SEAT_DATE_NOTE
)


def _iso_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _compact_date_input(value: str) -> str:
    if not re.fullmatch(r"[0-9]{8}", value):
        raise ValueError("날짜는 하이픈 없이 YYYYMMDD로 입력해주세요.")
    parsed = _iso_date(f"{value[:4]}-{value[4:6]}-{value[6:]}")
    if parsed is None:
        raise ValueError("존재하지 않는 날짜입니다. 연·월·일을 확인해주세요.")
    return parsed.isoformat()


def _normalized_seat_date_range(value: Any) -> tuple[str, str | None] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    start = _iso_date(value[0])
    end = _iso_date(value[1])
    if start is None or (value[1] is not None and end is None):
        return None
    if end is not None and start > end:
        return None
    return start.isoformat(), end.isoformat() if end is not None else None


def _seat_date_label(date_range: tuple[str, str | None] | None) -> str:
    if date_range is None:
        return "전체 날짜"
    start, end = date_range
    return f"{start}~{end} (포함)" if end is not None else f"{start}부터 (포함)"

# Operator-only. Deliberately left out of /help and the BotFather command list,
# and spelled so a subscriber does not land on it by guessing.
ADMIN_STATS_COMMAND = "/statss"
# Same aggregate view without the potentially long per-subscriber list.
ADMIN_STATS_SUMMARY_COMMAND = "/statsss"
# Operator-only broadcast.  Two steps on purpose: a typo here reaches every
# subscriber at once and cannot be taken back.
ADMIN_NOTICE_COMMAND = "/notice"
ADMIN_NOTICE_SEND_COMMAND = "/notice_send"
# Answering one chat, as opposed to the whole list.
ADMIN_REPLY_COMMAND = "/reply"
# Lifts the durable CGV halt without a redeploy.  The halt exists to make an
# operator look before the bot sends another request; this is that look.
ADMIN_CGV_RESUME_COMMAND = "/cgv_resume"
ADMIN_COMMANDS = {
    ADMIN_STATS_COMMAND,
    ADMIN_STATS_SUMMARY_COMMAND,
    ADMIN_NOTICE_COMMAND,
    ADMIN_NOTICE_SEND_COMMAND,
    ADMIN_REPLY_COMMAND,
    ADMIN_CGV_RESUME_COMMAND,
}
# Recovery record statuses that let normal scanning run.  "recovered" is the
# automatic probe's verdict and re-halts on the next 403; "resumed" is the
# operator's explicit choice and goes back to plain interval retries.
CGV_RECOVERY_STATUS_RECOVERED = "recovered"
CGV_RECOVERY_STATUS_RESUMED = "resumed"
CGV_RECOVERY_SCANNING_STATUSES = frozenset({
    CGV_RECOVERY_STATUS_RECOVERED,
    CGV_RECOVERY_STATUS_RESUMED,
})
# A draft goes stale rather than waiting around to be sent by accident.
NOTICE_DRAFT_TTL_MINUTES = 10
# Plain-text messages are relayed to the operator.  The bot is public, so one
# chat is capped per hour and a long message is trimmed.
FORWARD_MAX_PER_HOUR = 5
FORWARD_MAX_CHARS = 500
# Recent senders, so a reply can name who it reached even before they
# subscribe.  In memory: a restart just loses the convenience.
FORWARD_RECENT_SENDERS = 200
REPLY_HEADER = "💬 운영자 답장"
REPLY_GUIDE = (
    "구독자 한 명에게 답장합니다.\n"
    "사용법: /reply <chat_id> 보낼 내용\n\n"
    "chat_id 는 '💬 구독자 메시지' 알림 첫 줄에 있습니다.\n"
    "예: /reply 7061234567 /count_2 로 바꾸시면 알림이 줄어요."
)
# Marks the message as something a person wrote, not one of the alerts.
NOTICE_HEADER = "📢 공지"
NOTICE_GUIDE = (
    "공지를 구독자 전원에게 보냅니다.\n"
    "사용법: /notice 보낼 내용\n\n"
    "먼저 미리보기가 나오고, /notice_send 로 확인해야 실제로 전송됩니다.\n\n"
    "알림이 잦다는 얘기가 나올 때 쓸 문구 예시입니다. 그대로 복사해서 쓰세요.\n\n"
    "/notice 알림이 너무 자주 오면 아래처럼 줄일 수 있어요.\n"
    "• /count_2 — 2석 이상 남았을 때만 받기\n"
    "• /seat_sweet — 중앙 명당 구역에 자리가 있을 때만 받기\n"
    "• /mode_open — 새 회차 오픈 알림만 받기\n"
    "지금 설정은 /status 로 확인할 수 있어요."
)
# The reply carries every subscriber and Telegram rejects a message past 4096
# characters, so the list is trimmed to fit rather than losing the whole reply.
STATS_MAX_CHARS = 3900
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
SWEET_ONLY_GUIDE = (
    "현재 잔여좌석 알림은 모든 구독자에게 명당 좌석만 전송합니다.\n"
    "F16~29 · G16~29 · H13~32 · I13~32 · J11~34 · K11~34 · L11~34\n"
    "명당 밖 좌석과 위치를 확인하지 못한 좌석은 알리지 않습니다.\n"
    "/seat · /seat_sweet — 명당 구역 확인\n"
    "운영 중에는 /seat_all로 전체 좌석으로 전환할 수 없습니다.\n"
    "신규 오픈에는 좌석 제한을 적용하지 않습니다. 기존 알림 종류·요일 설정은 유지됩니다."
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
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.description = description
        self.retry_after_seconds = retry_after_seconds

    @property
    def rate_limited(self) -> bool:
        return self.status_code == 429

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
    telegram_broadcast_workers: int
    telegram_broadcast_rate_per_second: int
    cgv_request_spacing_seconds: int
    rate_limit_backoff_initial_seconds: int
    rate_limit_backoff_max_seconds: int
    forbidden_backoff_seconds: int
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
    cgv_recovery_request_id: str = ""
    cgv_recovery_pause_seconds: int = CGV_RECOVERY_PAUSE_SECONDS
    cgv_header_probe_request_id: str = ""
    cgv_header_probe_date: dt.date | None = None
    cgv_header_probe_seat_url: str = ""
    new_subscriptions_enabled: bool = True
    open_only_mode: bool = False
    seat_alert_sweet_only: bool = False
    cgv_wire_trace_request_id: str = ""
    cgv_public_matrix_request_id: str = ""
    cgv_public_matrix_base_date: dt.date | None = None
    cgv_public_matrix_compare_date: dt.date | None = None

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

        open_only_mode = _parse_bool(
            value("OPEN_ONLY_MODE", "false"), name="OPEN_ONLY_MODE"
        )
        header_probe_id = value("CGV_HEADER_PROBE_REQUEST_ID")
        if open_only_mode and header_probe_id and value("CGV_HEADER_PROBE_SEAT_URL"):
            raise ConfigurationError("신규 오픈 전용 모드에서는 좌석 API 진단을 실행할 수 없습니다.")
        header_probe_date = None
        if header_probe_id:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", header_probe_id):
                raise ConfigurationError("CGV_HEADER_PROBE_REQUEST_ID 형식이 올바르지 않습니다.")
            header_probe_date = _parse_date(
                value("CGV_HEADER_PROBE_DATE"), name="CGV_HEADER_PROBE_DATE"
            )

        wire_trace_id = value("CGV_WIRE_TRACE_REQUEST_ID")
        if wire_trace_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", wire_trace_id):
            raise ConfigurationError("CGV_WIRE_TRACE_REQUEST_ID 형식이 올바르지 않습니다.")

        matrix_id = value("CGV_PUBLIC_MATRIX_REQUEST_ID")
        matrix_base_date = matrix_compare_date = None
        if matrix_id:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", matrix_id):
                raise ConfigurationError("CGV_PUBLIC_MATRIX_REQUEST_ID 형식이 올바르지 않습니다.")
            matrix_base_date = _parse_date(value("CGV_PUBLIC_MATRIX_BASE_DATE"), name="CGV_PUBLIC_MATRIX_BASE_DATE")
            matrix_compare_date = _parse_date(value("CGV_PUBLIC_MATRIX_COMPARE_DATE"), name="CGV_PUBLIC_MATRIX_COMPARE_DATE")
            if matrix_base_date == matrix_compare_date:
                raise ConfigurationError("헤더 비교에는 서로 다른 날짜 두 개가 필요합니다.")
            if header_probe_id or wire_trace_id:
                raise ConfigurationError("헤더 비교는 다른 진단과 동시에 실행할 수 없습니다.")
            if (value("CGV_API_URL", DEFAULT_API_URL) != DEFAULT_API_URL
                    or value("CGV_BOOKING_URL", DEFAULT_BOOKING_URL) != DEFAULT_BOOKING_URL
                    or value("CGV_COMPANY_CODE", "A420") != "A420"
                    or value("CGV_SITE_NO", "0013") != "0013"
                    or value("CGV_MOVIE_NO", "30001323") != "30001323"
                    or value("CGV_RTCTL_SCOPE_CODE", "08") != "08"):
                raise ConfigurationError("헤더 비교는 승인된 용산 IMAX 일정 조회 조건만 지원합니다.")

        return cls(
            project_dir=project_dir,
            cgv_wire_trace_request_id=wire_trace_id,
            cgv_public_matrix_request_id=matrix_id,
            cgv_public_matrix_base_date=matrix_base_date,
            cgv_public_matrix_compare_date=matrix_compare_date,
            open_only_mode=open_only_mode,
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
                value("POLL_INTERVAL_SECONDS", "120"),
                name="POLL_INTERVAL_SECONDS",
                minimum=30,
                maximum=86400,
            ),
            cgv_recovery_request_id=value("CGV_RECOVERY_REQUEST_ID"),
            cgv_header_probe_request_id=header_probe_id,
            cgv_header_probe_date=header_probe_date,
            cgv_header_probe_seat_url=value("CGV_HEADER_PROBE_SEAT_URL"),
            cgv_recovery_pause_seconds=_parse_int(
                value("CGV_RECOVERY_PAUSE_SECONDS", str(CGV_RECOVERY_PAUSE_SECONDS)),
                name="CGV_RECOVERY_PAUSE_SECONDS",
                minimum=0,
                maximum=86400,
            ),
            telegram_command_poll_seconds=_parse_int(
                value("TELEGRAM_COMMAND_POLL_SECONDS", "2"),
                name="TELEGRAM_COMMAND_POLL_SECONDS",
                minimum=1,
                maximum=60,
            ),
            telegram_broadcast_workers=_parse_int(
                value(
                    "TELEGRAM_BROADCAST_WORKERS",
                    str(DEFAULT_TELEGRAM_BROADCAST_WORKERS),
                ),
                name="TELEGRAM_BROADCAST_WORKERS",
                minimum=1,
                maximum=30,
            ),
            telegram_broadcast_rate_per_second=_parse_int(
                value(
                    "TELEGRAM_BROADCAST_RATE_PER_SECOND",
                    str(DEFAULT_TELEGRAM_BROADCAST_RATE_PER_SECOND),
                ),
                name="TELEGRAM_BROADCAST_RATE_PER_SECOND",
                minimum=1,
                maximum=30,
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
            # A 403 is a verdict on the source, not a rate signal, so knocking
            # every two minutes only keeps the block warm.  Long, flat wait.
            forbidden_backoff_seconds=_parse_int(
                value("FORBIDDEN_BACKOFF_SECONDS", "1800"),
                name="FORBIDDEN_BACKOFF_SECONDS",
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
            new_subscriptions_enabled=_parse_bool(
                value("NEW_SUBSCRIPTIONS_ENABLED", "true"),
                name="NEW_SUBSCRIPTIONS_ENABLED",
            ),
            seat_alert_sweet_only=_parse_bool(
                value("SEAT_ALERT_SWEET_ONLY", "false"),
                name="SEAT_ALERT_SWEET_ONLY",
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
        self._wire_trace = WireHeaderTrace(
            config.cgv_wire_trace_request_id, config.state_file.parent,
            logging.getLogger("cgv_watcher"),
        )
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
        self, url: str, *, headers: Mapping[str, str], attempts: int = 2
    ) -> tuple[int, bytes, str, str, dict[str, str]]:
        parsed = urllib.parse.urlsplit(url)
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        last_error: Exception | None = None
        for attempt in range(attempts):
            connection = self._connection_for(url)
            capture = self._wire_trace.begin(connection, url)
            try:
                connection.request("GET", target, headers=dict(headers))
                response = connection.getresponse()
                body = response.read(5_000_001)
                status = response.status
                content_type = response.getheader("Content-Type", "")
                location = response.getheader("Location", "")
                # Only what tells a CGV block from an edge (Cloudflare) block;
                # no cookies or anything that identifies a session.
                diagnostics = {
                    "server": response.getheader("Server", ""),
                    "cf-ray": response.getheader("CF-RAY", ""),
                }
                if capture is not None:
                    capture.response(status, content_type, diagnostics)
                if response.getheader("Connection", "").lower() == "close":
                    self._drop_connection(url)
                return status, body, content_type, location, diagnostics
            except (http.client.HTTPException, TimeoutError, OSError) as exc:
                if capture is not None:
                    capture.error(exc)
                last_error = exc
                self._drop_connection(url)
                if attempt + 1 < attempts:
                    continue
            finally:
                if capture is not None:
                    capture.close()
        assert last_error is not None
        raise last_error

    def _get_json(
        self, url: str, *, referer: str, single_attempt: bool = False
    ) -> Any:
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
                status, body, content_type, location, diagnostics = self._request(
                    current_url, headers=headers, attempts=1 if single_attempt else 2
                )
                if status not in {301, 302, 303, 307, 308}:
                    break
                if single_attempt:
                    raise FetchError(
                        f"CGV 단일 재확인 응답이 리디렉션입니다(HTTP {status})."
                    )
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
            # Which side blocked us decides what can help: CGV's own page
            # means their application rule, a Cloudflare page means the edge
            # rejected the source network before CGV saw the request.
            if "비정상적으로 CGV에 접속" in error_body:
                block_kind = "CGV 자체 차단 페이지"
            elif "cloudflare" in error_body.lower():
                block_kind = "Cloudflare 차단 페이지"
            else:
                block_kind = ""
            if block_kind:
                detail = [block_kind]
                if diagnostics.get("server"):
                    detail.append(f"server={diagnostics['server'][:40]}")
                if diagnostics.get("cf-ray"):
                    detail.append("cf-ray 있음")
                raise FetchError(
                    f"CGV가 자동 조회를 차단했습니다(HTTP 403, {', '.join(detail)}). "
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

    def fetch_date(
        self, show_date: dt.date, *, single_attempt: bool = False
    ) -> Any:
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
        return self._get_json(
            url, referer=self.config.booking_url, single_attempt=single_attempt
        )

    def fetch_seat_snapshot(self, session: BookingSession) -> SeatSnapshot:
        if self.config.open_only_mode:
            raise FetchError("신규 오픈 전용 모드에서는 좌석 API를 호출하지 않습니다.")
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


class TelegramDeferred(TelegramError):
    """No send attempted: preserve the outbox without counting a failure."""

    def __init__(self, retry_at: dt.datetime):
        super().__init__("Telegram 발송 대기 중 (대기열에 보관)")
        self.retry_at = retry_at


def _telegram_retry_after(parsed: Any) -> int | None:
    if not isinstance(parsed, Mapping):
        return None
    parameters = parsed.get("parameters")
    value = parameters.get("retry_after") if isinstance(parameters, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    # Some proxies omit parameters but retain Telegram's English description.
    match = re.search(r"\bretry after ([0-9]+)\b", str(parsed.get("description", "")), re.I)
    return int(match[1]) if match else None


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, *, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context()
        self._chat_send_lock = threading.Lock()
        self._chat_next_send: dict[str, float] = {}

    def send_message(self, text: str, *, chat_id: str | None = None) -> None:
        target = str(chat_id or self.chat_id)
        with self._chat_send_lock:
            now = time.monotonic()
            delay = self._chat_next_send.get(target, 0.0) - now
            if delay > 0:
                raise TelegramDeferred(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay))
            # Respect both per-chat and per-group limits, including replies.
            self._chat_next_send[target] = now + (3.1 if target.startswith("-") else 1.1)
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
            parsed = {}
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
                retry_after_seconds=_telegram_retry_after(parsed),
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
                f"Telegram 전송 실패: {description}", description=description,
                status_code=parsed.get("error_code"),
                retry_after_seconds=_telegram_retry_after(parsed),
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
    def __init__(self, path: Path, *, sweet_only: bool = False):
        self.path = path
        # An operational restriction, not a destructive subscriber migration.
        self.sweet_only = sweet_only
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
            "cgv_recovery": {},
            "seat_api_backoff": {},
            "telegram_send_backoff": {},
            "broadcast_rotation": {},
            "delivery_sequence": 0,
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
            if not isinstance(loaded.get("cgv_recovery", {}), dict):
                raise RuntimeError(f"CGV 재확인 상태 파일 형식이 올바르지 않습니다: {self.path}")
            if not isinstance(loaded.get("seat_api_backoff", {}), dict):
                raise RuntimeError(f"좌석 API 대기 상태 파일 형식이 올바르지 않습니다: {self.path}")
            if not isinstance(loaded.get("telegram_send_backoff", {}), dict):
                raise RuntimeError("Telegram 발송 대기 상태 형식이 올바르지 않습니다.")
            rotation = loaded.get("broadcast_rotation", {})
            if not isinstance(rotation, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in rotation.items()
            ):
                raise RuntimeError("Telegram 발송 순번 상태 형식이 올바르지 않습니다.")
            sequence = loaded.get("delivery_sequence", 0)
            if type(sequence) is not int or sequence < 0:
                raise RuntimeError("Telegram 대기열 순번 형식이 올바르지 않습니다.")
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

    def cgv_recovery(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data.get("cgv_recovery", {}))

    def set_cgv_recovery(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self.data["cgv_recovery"] = dict(record)

    def seat_api_backoff(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data.get("seat_api_backoff", {}))

    def set_seat_api_backoff(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self.data["seat_api_backoff"] = dict(record)

    def was_notified(self, key: str) -> bool:
        with self._lock:
            return key in self.data["notified"]

    def telegram_send_retry_at(self) -> dt.datetime | None:
        with self._lock:
            raw = self.data.get("telegram_send_backoff", {}).get("retry_at")
            if not raw:
                return None
            parsed = dt.datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                raise RuntimeError("Telegram 발송 대기 시각에 시간대가 없습니다.")
            return parsed.astimezone(dt.timezone.utc)

    @staticmethod
    def _delivery_expiry(record: Mapping[str, Any]) -> dt.datetime:
        raw = record.get("expires_at") or record.get("queued_at", "")
        parsed = dt.datetime.fromisoformat(str(raw))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        if not record.get("expires_at"):
            parsed += dt.timedelta(hours=PENDING_DELIVERY_TTL_HOURS)
        return parsed

    def pause_telegram_sends(self, retry_at: dt.datetime, now: dt.datetime) -> bool:
        """Persist the longest wait; exclude flood-wait time from queue TTL."""

        with self._lock:
            previous = self.telegram_send_retry_at()
            blocked_from = max(now, previous) if previous else now
            if retry_at <= blocked_from:
                return False
            extension = retry_at - blocked_from
            for record in self.data["pending_deliveries"].values():
                if not isinstance(record, dict):
                    continue
                try:
                    record["expires_at"] = (self._delivery_expiry(record) + extension).isoformat()
                except (ValueError, TypeError):
                    continue
            self.data["telegram_send_backoff"] = {
                "retry_at": retry_at.isoformat(), "observed_at": now.isoformat(),
                "reason": "Telegram HTTP 429",
            }
            return True

    def defer_pending_delivery(self, key: str, retry_at: dt.datetime) -> bool:
        with self._lock:
            record = self.data["pending_deliveries"].get(key)
            if not isinstance(record, dict):
                return False
            record["next_attempt_at"] = retry_at.isoformat()
            return True

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

    def subscriber_details(self) -> tuple[dict[str, Any], ...]:
        """Subscribers ordered by join time with their delivery settings."""

        with self._lock:
            chat_ids = tuple(str(chat_id) for chat_id in self.data["subscribers"])
            records = []
            for chat_id in chat_ids:
                record = self.data["subscribers"].get(chat_id)
                stored = record if isinstance(record, Mapping) else {}
                records.append(
                    {
                        "chat_id": chat_id,
                        "label": str(stored.get("label") or ""),
                        "chat_type": str(stored.get("chat_type") or ""),
                        "subscribed_at": str(stored.get("subscribed_at") or ""),
                        "alert_mode": self.alert_mode(chat_id),
                        "show_day": self.show_day_selection(chat_id),
                        "seat_time_range": self.seat_time_range(chat_id),
                        "seat_date_range": self.seat_date_range(chat_id),
                        "seat_selection": self.seat_selection(chat_id),
                        "min_seats": self.min_seats(chat_id),
                    }
                )
            def joined_at(record: Mapping[str, Any]) -> tuple[int, float, str]:
                stored = str(record.get("subscribed_at") or "")
                try:
                    moment = dt.datetime.fromisoformat(stored)
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=dt.timezone.utc)
                    timestamp = moment.timestamp()
                except (OSError, OverflowError, ValueError):
                    # Legacy records without a usable timestamp stay visible,
                    # but follow every record whose join time can be ordered.
                    return (1, 0.0, str(record.get("chat_id") or ""))
                return (0, timestamp, str(record.get("chat_id") or ""))

            return tuple(sorted(records, key=joined_at))

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

    def show_day_selection(self, chat_id: str) -> str:
        """Return which show dates this subscriber wants to hear about."""

        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return DEFAULT_SHOW_DAY
            selection = record.get("show_day")
            if isinstance(selection, str) and selection in SHOW_DAY_SELECTIONS:
                return selection
            return DEFAULT_SHOW_DAY

    def set_show_day_selection(self, chat_id: str, selection: str) -> bool:
        """Store a show-day preference; returns False when unchanged."""

        if selection not in SHOW_DAY_SELECTIONS:
            raise ValueError(f"알 수 없는 상영일 선택: {selection}")
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return False
            if record.get("show_day", DEFAULT_SHOW_DAY) == selection:
                return False
            updated = dict(record)
            updated["show_day"] = selection
            self.data["subscribers"][str(chat_id)] = updated
            return True

    def wants_show_date(self, chat_id: str, show_date: str | None) -> bool:
        """Whether ``show_date`` passes this subscriber's weekday filter."""

        if show_date is None or self.show_day_selection(chat_id) == SHOW_DAY_ALL:
            return True
        try:
            parsed = dt.date.fromisoformat(show_date)
        except ValueError:
            # Session dates are validated before this point. If an old queued
            # record lacks a readable date, favor not missing an opening.
            return True
        return parsed.weekday() >= 5

    def seat_selection(self, chat_id: str) -> str:
        """Return the subscriber's preferred-seat preset."""

        if self.sweet_only:
            return SEAT_SELECTION_SWEET
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping):
                return DEFAULT_SEAT_SELECTION
            selection = record.get("seat_selection")
            if isinstance(selection, str) and selection in SEAT_SELECTIONS:
                return selection
            return DEFAULT_SEAT_SELECTION

    def seat_time_range(self, chat_id: str) -> tuple[str, str] | None:
        """Legacy and unset preferences mean all screening times."""

        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            value = record.get("seat_time_range") if isinstance(record, Mapping) else None
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                return None
            minutes = tuple(_clock_minutes(part) for part in value)
            if any(part is None for part in minutes):
                return None
            return tuple(f"{part // 60:02d}:{part % 60:02d}" for part in minutes)

    def set_seat_time_range(self, chat_id: str, value: tuple[str, str] | None) -> bool:
        if value is not None:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError("상영 시간 범위는 시작·끝 시각이 필요합니다.")
            minutes = tuple(_clock_minutes(part) for part in value)
            if any(part is None for part in minutes):
                raise ValueError("상영 시간은 00:00~23:59 형식이어야 합니다.")
            value = tuple(f"{part // 60:02d}:{part % 60:02d}" for part in minutes)
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping) or self.seat_time_range(chat_id) == value:
                return False
            updated = dict(record)
            updated["seat_time_range"] = list(value) if value else None
            self.data["subscribers"][str(chat_id)] = updated
            return True

    def wants_seat_time(self, chat_id: str, show_time: str | None) -> bool:
        time_range = self.seat_time_range(chat_id)
        if time_range is None:
            return True
        current = _clock_minutes(show_time)
        if current is None:
            # Never guess an unknown time for a subscriber who opted in.
            return False
        start, end = (_clock_minutes(part) for part in time_range)
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def seat_date_range(self, chat_id: str) -> tuple[str, str | None] | None:
        """Missing/legacy preferences keep all show dates eligible."""

        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            value = record.get("seat_date_range") if isinstance(record, Mapping) else None
            return _normalized_seat_date_range(value)

    def set_seat_date_range(
        self, chat_id: str, value: tuple[str, str | None] | None
    ) -> bool:
        normalized = _normalized_seat_date_range(value)
        if value is not None and normalized is None:
            raise ValueError("올바른 날짜 범위를 입력해주세요. 시작일은 종료일보다 늦을 수 없습니다.")
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, Mapping) or self.seat_date_range(chat_id) == normalized:
                return False
            updated = dict(record)
            updated["seat_date_range"] = list(normalized) if normalized else None
            self.data["subscribers"][str(chat_id)] = updated
            return True

    def wants_seat_date(self, chat_id: str, show_date: str | None) -> bool:
        date_range = self.seat_date_range(chat_id)
        if date_range is None:
            return True
        parsed = _iso_date(show_date)
        if parsed is None:
            return False
        current = parsed.isoformat()
        start, end = date_range
        return current >= start and (end is None or current <= end)

    def set_seat_selection(self, chat_id: str, selection: str) -> bool:
        """Store a preferred-seat preset; returns False when unchanged."""

        if selection not in SEAT_SELECTIONS:
            raise ValueError(f"알 수 없는 좌석 선택: {selection}")
        if self.sweet_only:
            return False
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

    def rotate_broadcast_recipients(
        self, category: str, recipients: Sequence[str], *, advance: bool = True
    ) -> tuple[str, ...]:
        """Rotate a stable eligible ring, independently for each alert category.

        Remember the last first recipient, rather than an array offset, so
        subscriptions and filters can change without resetting to the first ID.
        Direct replies and operator messages do not consume broadcast turns.
        """

        with self._lock:
            unique = tuple(dict.fromkeys(str(chat_id) for chat_id in recipients))
            if not unique or category not in ROTATING_ALERT_CATEGORIES:
                return unique
            ordered = sorted(unique)
            previous = self.data["broadcast_rotation"].get(category)
            start = 0
            if previous is not None:
                start = next((i for i, chat_id in enumerate(ordered) if chat_id > previous), 0)
            rotated = tuple(ordered[start:] + ordered[:start])
            if advance:
                self.data["broadcast_rotation"][category] = rotated[0]
            return rotated

    def queue_broadcast_deliveries(
        self, recipients: Sequence[str], text: str, category: str, *,
        show_date: str | None = None, show_time: str | None = None,
        seats_available: int | None = None,
    ) -> int:
        """Save the rotated outbox and its cursor together before sending."""

        with self._lock:
            ordered = self.rotate_broadcast_recipients(category, recipients, advance=False)
            added = 0
            first_added = None
            for chat_id in ordered:
                if self.queue_pending_delivery(
                    chat_id, text, category, show_date=show_date,
                    show_time=show_time, seats_available=seats_available,
                ):
                    added += 1
                    if first_added is None:
                        first_added = chat_id
            if added:
                if category in ROTATING_ALERT_CATEGORIES:
                    self.data["broadcast_rotation"][category] = first_added
                # The sender cannot see this batch until this lock is released.
                self.save()
            return added

    def queue_pending_delivery(
        self,
        chat_id: str,
        text: str,
        category: str,
        *,
        show_date: str | None = None,
        show_time: str | None = None,
        seats_available: int | None = None,
    ) -> bool:
        """Persist one Telegram delivery without duplicating the outbox."""

        key_source = f"{chat_id}\0{category}\0{show_date or ''}\0{text}"
        key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
        with self._lock:
            bucket = self.data.setdefault("pending_deliveries", {})
            if key in bucket:
                return False
            now = dt.datetime.now(dt.timezone.utc)
            retry_at = self.telegram_send_retry_at()
            expires_at = max(now, retry_at) if retry_at else now
            expires_at += dt.timedelta(hours=PENDING_DELIVERY_TTL_HOURS)
            self.data["delivery_sequence"] += 1
            bucket[key] = {
                "chat_id": str(chat_id),
                "text": text,
                "category": category,
                "show_date": show_date or "",
                "show_time": show_time or "",
                "seats_available": seats_available,
                "priority": DELIVERY_CATEGORY_PRIORITIES.get(
                    category, DEFAULT_DELIVERY_PRIORITY
                ),
                "queued_at": now.isoformat(),
                "queue_sequence": self.data["delivery_sequence"],
                "attempts": 0,
                "next_attempt_at": "",
            }
            if retry_at and retry_at > now:
                bucket[key]["expires_at"] = expires_at.isoformat()
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
        """Count a failed send and schedule bounded exponential backoff."""

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
            retry_seconds = min(
                DELIVERY_RETRY_BACKOFF_MAX_SECONDS,
                2 ** min(attempts, 8),
            )
            updated["next_attempt_at"] = (
                dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(seconds=retry_seconds)
            ).isoformat()
            bucket[key] = updated
            return False

    def prune_pending_deliveries(self, now: dt.datetime) -> int:
        """Drop queued retries older than the TTL, whatever their attempt count.

        Attempts only tick when a retry actually runs, so a stalled watcher
        could otherwise hold a queue entry indefinitely.
        """

        removed = 0
        with self._lock:
            bucket = self.data.setdefault("pending_deliveries", {})
            for key in list(bucket):
                record = bucket.get(key)
                expires_at = None
                if isinstance(record, Mapping):
                    try:
                        expires_at = self._delivery_expiry(record)
                    except (ValueError, TypeError):
                        expires_at = None
                if expires_at is None:
                    # Unreadable timestamp: drop it rather than keep it forever.
                    bucket.pop(key, None)
                    removed += 1
                    continue
                if expires_at < now:
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

    def pending_deliveries(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        """Return a stable copy so Telegram calls happen outside the state lock."""

        with self._lock:
            records: list[tuple[str, dict[str, Any]]] = []
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
                    copied = {
                        "chat_id": chat_id,
                        "text": text,
                        "category": category,
                    }
                    show_date = raw.get("show_date")
                    if isinstance(show_date, str) and show_date:
                        copied["show_date"] = show_date
                    show_time = raw.get("show_time")
                    if not show_time and category in {
                        ALERT_SEATS, ALERT_SEATS_SWEET, ALERT_SEATS_UNCLASSIFIED,
                    }:
                        # Queued messages from before this release have no
                        # metadata. Recover only the exact known field label.
                        match = re.search(
                            r"^(?:상영 시간|상영 시작시간): ([0-2]?\d:[0-5]\d)$",
                            text, re.MULTILINE,
                        )
                        show_time = match[1] if match else None
                    if _clock_minutes(show_time) is not None:
                        copied["show_time"] = show_time
                    seats_available = raw.get("seats_available")
                    if isinstance(seats_available, int):
                        copied["seats_available"] = seats_available
                    copied["priority"] = raw.get(
                        "priority",
                        DELIVERY_CATEGORY_PRIORITIES.get(
                            category, DEFAULT_DELIVERY_PRIORITY
                        ),
                    )
                    copied["queued_at"] = str(raw.get("queued_at") or "")
                    sequence = raw.get("queue_sequence", 0)
                    copied["queue_sequence"] = sequence if type(sequence) is int and sequence >= 0 else 0
                    try:
                        copied["attempts"] = int(raw.get("attempts") or 0)
                    except (TypeError, ValueError):
                        copied["attempts"] = 0
                    copied["next_attempt_at"] = str(
                        raw.get("next_attempt_at") or ""
                    )
                    records.append(
                        (
                            str(key),
                            copied,
                        )
                    )
            return tuple(records)

    def pending_delivery_count(self) -> int:
        with self._lock:
            return len(self.data.setdefault("pending_deliveries", {}))

    def remove_pending_delivery(self, key: str) -> bool:
        with self._lock:
            bucket = self.data.setdefault("pending_deliveries", {})
            return bucket.pop(str(key), None) is not None

    def subscriber_breakdown(self) -> dict[str, Any]:
        """Counts of who is subscribed and how they configured their alerts."""

        with self._lock:
            modes = {mode: 0 for mode in ALERT_MODES}
            chat_types: dict[str, int] = {}
            show_days = {selection: 0 for selection in SHOW_DAY_SELECTIONS}
            seat_selections = {selection: 0 for selection in SEAT_SELECTIONS}
            min_seats_counts = {minimum: 0 for minimum in MIN_SEATS_CHOICES}
            seat_times = {"all": 0, "filtered": 0}
            seat_dates = {"all": 0, "filtered": 0}
            for chat_id, record in self.data["subscribers"].items():
                modes[self.alert_mode(chat_id)] += 1
                show_days[self.show_day_selection(chat_id)] += 1
                seat_selections[self.seat_selection(chat_id)] += 1
                min_seats_counts[self.min_seats(chat_id)] += 1
                seat_times["filtered" if self.seat_time_range(chat_id) else "all"] += 1
                seat_dates["filtered" if self.seat_date_range(chat_id) else "all"] += 1
                kind = ""
                if isinstance(record, Mapping):
                    kind = str(record.get("chat_type") or "")
                chat_types[kind or "unknown"] = chat_types.get(kind or "unknown", 0) + 1
            return {
                "total": len(self.data["subscribers"]),
                "modes": modes,
                "show_days": show_days,
                "seat_selections": seat_selections,
                "min_seats": min_seats_counts,
                "seat_times": seat_times,
                "seat_dates": seat_dates,
                "chat_types": chat_types,
            }

    def subscriber_ids_for(
        self,
        category: str,
        *,
        seats_available: int | None = None,
        show_date: str | None = None,
        show_time: str | None = None,
    ) -> tuple[str, ...]:
        """Subscribers who opted in to this alert category.

        ``seats_available`` is how many seats this alert says are on sale,
        counted inside whatever scope the category names.  Passing it applies
        each subscriber's minimum; leaving it None means the alert is not
        about seat availability, so nobody is filtered out.

        ``show_date`` is the ISO date printed in a movie alert.  Omitting it
        keeps non-show messages such as announcements outside the day filter.
        ``show_time`` filters seat alerts only; openings ignore the clock.
        The per-subscriber date range also applies to seat categories only;
        the existing weekday filter still applies to both seats and openings.
        """

        def wants_this_many(chat_id: str) -> bool:
            minimum = self.min_seats(chat_id)
            # The default is not a threshold of one — it is no threshold at
            # all, so an alert reaches everyone who never set a minimum even
            # when the seat count could not be read.
            if seats_available is None or minimum == MIN_SEATS_DEFAULT:
                return True
            return minimum <= seats_available

        def wants_this_date(chat_id: str) -> bool:
            return self.wants_show_date(chat_id, show_date)

        if category == ALERT_NOTICE:
            return self.subscriber_ids()
        if category == ALERT_SYSTEM:
            # Routed by the caller, which knows who the operator is.
            return ()
        if category == ALERT_SEATS_UNCLASSIFIED:
            # Sweetness cannot be judged without a readable row, so an
            # unclassified alert only ever concerns whole-auditorium
            # subscribers.
            return self.subscriber_ids_for(
                ALERT_SEATS,
                seats_available=seats_available,
                show_date=show_date,
                show_time=show_time,
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
                    and wants_this_date(chat_id)
                    and self.wants_seat_date(chat_id, show_date)
                    and self.wants_seat_time(chat_id, show_time)
                    and self.seat_selection(chat_id) == selection
                    and wants_this_many(chat_id)
                )
        with self._lock:
            return tuple(
                str(chat_id)
                for chat_id in self.data["subscribers"]
                if category in ALERT_MODES[self.alert_mode(chat_id)]
                and wants_this_date(chat_id)
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
                "show_day": DEFAULT_SHOW_DAY,
                "seat_time_range": None,
                "seat_date_range": None,
                "seat_selection": SEAT_SELECTION_SWEET if self.sweet_only else DEFAULT_SEAT_SELECTION,
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
    session: BookingSession, *, seat_detail_unclassified: bool = False,
    open_only_mode: bool = False,
) -> str:
    line = f"• 상영 시작시간 {session.start_time} — {_seat_ratio(session)}"
    if session.remaining_seats == 0:
        # Announced anyway, but say so plainly rather than sending someone to
        # a booking page with nothing on it.
        line += " (매진)" if open_only_mode else " (매진 · 취소표 나오면 알림)"
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
        f"상영 시간: {session.start_time}",
    ]
    if seat_line := _available_seat_line(current_available, label="좌석 번호"):
        lines.append(seat_line)
    lines.append(f"잔여 좌석: {ratio}")
    if (
        previous_available is not None
        and previous_available.usable is not None
        and current_available.usable is not None
        and previous_available.usable != current_available.usable
    ):
        lines.append(
            "변동 좌석수: "
            f"{previous_available.usable}석 → {current_available.usable}석"
        )
    elif current_available.usable is not None:
        lines.append(f"변동 좌석수: {current_available.usable}석")
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
                    open_only_mode=config.open_only_mode,
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
    forbidden_requests: int = 0
    schedule_skipped_dates: int = 0
    seat_detail_skipped: int = 0
    requested_dates: int = 0
    full_scan: bool = True
    suppressed_closed: int = 0
    deferred_rechecks_skipped: int = 0
    cgv_paused: bool = False
    # Schedule blocks alone control the main loop's whole-cycle backoff.
    seat_forbidden_requests: int = 0
    seat_rate_limited_requests: int = 0


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
    forbidden_requests: int = 0
    seat_forbidden_requests: int = 0
    seat_rate_limited_requests: int = 0
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


class _BroadcastRateLimiter:
    """Cap broadcast starts within a rolling one-second window."""

    def __init__(
        self,
        max_per_second: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.max_per_second = max(1, max_per_second)
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._started_at: list[float] = []

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                cutoff = now - 1.0
                self._started_at = [
                    started for started in self._started_at if started > cutoff
                ]
                if len(self._started_at) < self.max_per_second:
                    self._started_at.append(now)
                    return
                delay = max(0.001, 1.0 - (now - self._started_at[0]))
            self._sleeper(delay)


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
        self.state = StateStore(config.state_file, sweet_only=config.seat_alert_sweet_only)
        self._telegram_send_gate = threading.Lock()
        self._last_cgv_request_finished_at: float | None = None
        self._telegram_broadcast_limiter = _BroadcastRateLimiter(
            config.telegram_broadcast_rate_per_second
        )
        self._delivery_stop_event = threading.Event()
        self._delivery_wake_event = threading.Event()
        self._delivery_thread: threading.Thread | None = None
        # Cycle 0 always sweeps the full window; a cursor scan runs in between.
        self._cycle_index = 0
        self._command_poll_failures = 0
        # Drafted announcement awaiting confirmation; in memory only, so a
        # restart drops it rather than sending something the operator forgot.
        self._notice_draft: tuple[str, dt.datetime] | None = None
        self._forwarded_at: dict[str, list[dt.datetime]] = {}
        self._recent_senders: dict[str, str] = {}
        self._recovery_payloads: dict[dt.date, Any] = {}
        self._header_probe_checked = False
        self.state.load()
        # Validate a persisted wait before any sender can start (fail closed).
        self.state.telegram_send_retry_at()
        if not self.state.subscribers_initialized:
            self.state.initialize_subscribers(config.telegram_chat_id)
            if not self.dry_run:
                self.state.save()
        request_id = config.cgv_recovery_request_id
        if request_id and self.state.cgv_recovery().get("request_id") != request_id:
            now = config.local_now()
            self._save_cgv_recovery({
                "request_id": request_id,
                "status": "cooldown",
                "started_at": now.isoformat(),
                "pause_seconds": config.cgv_recovery_pause_seconds,
                "probe_at": (
                    now + dt.timedelta(seconds=config.cgv_recovery_pause_seconds)
                ).isoformat(),
            })

    def _telegram_pause_until(self) -> dt.datetime | None:
        retry_at = self.state.telegram_send_retry_at()
        return retry_at if retry_at and retry_at > dt.datetime.now(dt.timezone.utc) else None

    def telegram_send_status_text(self) -> str:
        retry_at = self._telegram_pause_until()
        if not retry_at:
            return ""
        local = retry_at.astimezone(ZoneInfo(self.config.timezone_name))
        return (
            f"⏳ Telegram 발송 제한 대기: {local:%Y-%m-%d %H:%M:%S %Z} 이후 재시도\n"
            "CGV 조회·설정 변경은 계속하며 알림과 답장은 대기열에 보관합니다."
        )

    def _send_telegram(self, text: str, *, chat_id: str) -> None:
        # Do not hold this gate through network I/O. Requests already in flight
        # may complete, but no new request starts after the shared pause is set.
        with self._telegram_send_gate:
            if retry_at := self._telegram_pause_until():
                raise TelegramDeferred(retry_at)
        self._telegram_broadcast_limiter.wait()
        with self._telegram_send_gate:
            if retry_at := self._telegram_pause_until():
                raise TelegramDeferred(retry_at)
        try:
            self.telegram.send_message(text, chat_id=chat_id)
        except TelegramError as exc:
            if not exc.rate_limited:
                raise
            seconds = exc.retry_after_seconds
            if seconds is None:
                seconds = TELEGRAM_FLOOD_WAIT_FALLBACK_SECONDS
            now = dt.datetime.now(dt.timezone.utc)
            retry_at = now + dt.timedelta(seconds=seconds + TELEGRAM_FLOOD_WAIT_MARGIN_SECONDS)
            with self._telegram_send_gate:
                changed = self.state.pause_telegram_sends(retry_at, now)
                retry_at = self.state.telegram_send_retry_at()
                if changed:
                    # Durable before other workers or a restart can send again.
                    self.state.save()
                    self.logger.warning(
                        "Telegram HTTP 429: 전체 발송 대기, 재시도 %s (CGV 조회 유지)",
                        retry_at.astimezone(ZoneInfo(self.config.timezone_name)).isoformat(),
                    )
                    self._delivery_wake_event.set()
            raise TelegramDeferred(retry_at) from exc

    def _send_or_queue_reply(self, text: str, *, chat_id: str) -> bool:
        try:
            self._send_telegram(text, chat_id=chat_id)
        except TelegramDeferred:
            self.state.queue_pending_delivery(chat_id, text, ALERT_REPLY)
            self.state.save()
            self._delivery_wake_event.set()
            return False
        return True

    def _save_cgv_recovery(self, record: Mapping[str, Any]) -> None:
        self.state.set_cgv_recovery(record)
        if not self.dry_run:
            self.state.save()

    def _seat_lookup_paused(self) -> bool:
        record = self.state.seat_api_backoff()
        if not record:
            return False
        try:
            retry_at = dt.datetime.fromisoformat(record["retry_at"])
            if retry_at.tzinfo is None:
                raise ValueError("missing timezone")
            return self.config.local_now() < retry_at
        except (KeyError, TypeError, ValueError):
            # A broken seat timer must neither flood CGV nor stop schedules.
            return True

    def seat_api_status_text(self) -> str:
        if self.config.open_only_mode or not self._seat_lookup_paused():
            return ""
        record = self.state.seat_api_backoff()
        try:
            retry_at = dt.datetime.fromisoformat(record["retry_at"])
            if retry_at.tzinfo is None:
                raise ValueError("missing timezone")
            label = retry_at.astimezone(ZoneInfo(self.config.timezone_name)).strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            )
        except (KeyError, TypeError, ValueError):
            label = "대기 시각 오류 — 운영자 확인 필요"
        return (
            f"⏸ 좌석 상세 조회만 대기 중 (HTTP {record.get('status_code', '?')})\n"
            f"재시도 가능 시각: {label}\n"
            "신규 오픈 일정 조회는 계속합니다."
        )

    def _pause_seat_lookup(self, status_code: int) -> None:
        previous = self.state.seat_api_backoff()
        failures = 1
        if previous.get("status_code") == status_code:
            try:
                failures += max(0, int(previous.get("failures", 0)))
            except (ValueError, TypeError):
                pass
        seconds = (
            self.config.forbidden_backoff_seconds if status_code == 403
            else rate_limit_backoff_seconds(self.config, failures)
        )
        now = self.config.local_now()
        self.state.set_seat_api_backoff({
            "status_code": status_code,
            "failures": failures,
            "blocked_at": now.isoformat(),
            "retry_at": (now + dt.timedelta(seconds=seconds)).isoformat(),
        })
        # Persist before continuing schedules so a restart cannot bypass it.
        if not self.dry_run:
            self.state.save()
        self.logger.warning(
            "좌석 API HTTP %d: 좌석 조회·새 좌석 알림만 %d초 대기합니다. "
            "신규 오픈 일정 조회는 계속합니다. (연속 %d회)",
            status_code, seconds, failures,
        )

    def _clear_seat_backoff(self) -> None:
        if not self.state.seat_api_backoff():
            return
        self.state.set_seat_api_backoff({})
        if not self.dry_run:
            self.state.save()
        self.logger.info("좌석 API 재조회 성공: 잔여 좌석 조회·알림을 재개합니다.")

    def cgv_recovery_status_text(self) -> str:
        record = self.state.cgv_recovery()
        status = record.get("status")
        if status == "cooldown":
            try:
                probe_at = dt.datetime.fromisoformat(record["probe_at"])
                if probe_at.tzinfo is None:
                    raise ValueError("missing timezone")
                label = probe_at.astimezone(ZoneInfo(self.config.timezone_name)).strftime(
                    "%Y-%m-%d %H:%M:%S %Z"
                )
            except (KeyError, TypeError, ValueError):
                return "⏸ CGV 조회 중단: 재확인 시각 오류, 운영자 확인 필요"
            pause_seconds = record.get("pause_seconds", CGV_RECOVERY_PAUSE_SECONDS)
            heading = "⏸ CGV 조회 중단 — 단일 재확인 대기"
            if pause_seconds == 0:
                heading = "🔎 CGV 즉시 단일 재확인 (운영자 승인)"
            elif pause_seconds == CGV_RECOVERY_PAUSE_SECONDS:
                heading = "⏸ CGV 조회 1시간 중단"
            return (
                f"{heading}\n"
                f"재확인 예정: {label}\n"
                "일정 요청 1건만 확인하며, 실패하면 자동 조회를 중단합니다."
            )
        if status == "probing":
            return "🔎 CGV 일정 요청 1건 재확인 중"
        if status == "halted":
            return (
                "⛔ CGV 자동 조회 중단 — 운영자 확인 필요\n"
                f"원인: {record.get('reason', '재확인 실패')}\n"
                "자동 재시도하지 않습니다. 현재 새 예매·좌석 알림을 감지할 수 없습니다."
            )
        if status == CGV_RECOVERY_STATUS_RECOVERED:
            return f"✅ CGV 재확인 성공 — {self.config.poll_interval_seconds}초 조회로 복귀"
        return ""

    def _handle_cgv_resume_command(self) -> str:
        """Clear the durable halt so the next cycle scans again.

        Unlike a successful automatic probe, a manual resume does not re-halt
        on the next 403: the operator has seen the block and chosen the plain
        poll-interval retry instead.  The record keeps its request id so a
        restart with the same ``CGV_RECOVERY_REQUEST_ID`` does not start a
        fresh cooldown.
        """

        if self.config.cgv_header_probe_request_id:
            return (
                "헤더 단일 진단 모드에서는 재개할 수 없습니다.\n"
                "Railway Variables 에서 CGV_HEADER_PROBE_REQUEST_ID 를 지우고 "
                "재배포한 뒤 다시 보내주세요."
            )
        record = self.state.cgv_recovery()
        status = record.get("status")
        if not record or status in CGV_RECOVERY_SCANNING_STATUSES:
            return (
                "CGV 자동 조회가 중단된 상태가 아닙니다. "
                f"{self.config.poll_interval_seconds}초 간격으로 계속 조회합니다."
            )
        previous_reason = record.get("reason", "")
        record.update(
            status=CGV_RECOVERY_STATUS_RESUMED,
            resumed_from=status,
            resumed_at=self.config.local_now().isoformat(),
        )
        self._save_cgv_recovery(record)
        self.logger.warning(
            "운영자 명령으로 CGV 자동 조회 재개 (이전 상태: %s, 원인: %s)",
            status,
            previous_reason or "-",
        )
        detail = f"\n중단 원인: {previous_reason}" if previous_reason else ""
        return (
            "▶️ CGV 자동 조회를 재개합니다.\n"
            f"다음 주기부터 {self.config.poll_interval_seconds}초 간격으로 조회합니다. "
            "HTTP 403이 다시 나오면 자동 중단하지 않고 "
            f"{max(1, self.config.forbidden_backoff_seconds // 60)}분 뒤 재시도합니다."
            f"{detail}"
        )

    def _announce_cgv_recovery(self) -> None:
        record = self.state.cgv_recovery()
        status = record.get("status")
        if self.dry_run or record.get("announced_status") == status:
            return
        text = self.cgv_recovery_status_text()
        if not text:
            return
        self.logger.warning("%s", text.replace("\n", " | "))
        self._broadcast_message(
            text + "\nTelegram 명령과 기존 발송 대기열은 계속 처리합니다.",
            category=ALERT_SYSTEM,
            recipients=self._operator_recipients(),
        )
        record["announced_status"] = status
        self._save_cgv_recovery(record)

    def _halt_cgv_recovery(self, reason: str) -> None:
        record = self.state.cgv_recovery()
        record.update(
            status="halted", reason=reason,
            finished_at=self.config.local_now().isoformat(),
        )
        self._save_cgv_recovery(record)
        self._announce_cgv_recovery()

    def _cgv_recovery_blocks_scan(self) -> bool:
        """Persist an at-most-once probe; never repeat an ambiguous attempt."""

        if self.config.cgv_header_probe_request_id:
            # A diagnostic is NOT a recovery attempt: even HTTP 200 must never
            # resume scans. Persist the existing halt before doing any I/O.
            if self.state.cgv_recovery().get("status") != "halted":
                record = self.state.cgv_recovery()
                record.update(
                    status="halted",
                    reason="헤더 단일 진단 모드: 결과와 관계없이 자동 조회 중단 유지",
                    finished_at=self.config.local_now().isoformat(),
                )
                self._save_cgv_recovery(record)
            if not self.dry_run and not self._header_probe_checked:
                self._header_probe_checked = True
                try:
                    run_header_probe_once(self.config, self.logger, user_agent=USER_AGENT)
                except Exception as exc:
                    # No exception text: URLs, response bodies or credentials
                    # must not leak into logs. Do not retry after ambiguity.
                    self.logger.error(
                        "CGV_HEADER_PROBE_ERROR type=%s; 자동 조회 중단 유지",
                        type(exc).__name__,
                    )
            self._announce_cgv_recovery()
            return True

        record = self.state.cgv_recovery()
        if not record or record.get("status") in CGV_RECOVERY_SCANNING_STATUSES:
            return False
        status = record.get("status")
        if status == "halted":
            self._announce_cgv_recovery()
            return True
        if status != "cooldown":
            # A crash after the claim may have happened before or after sending.
            self._halt_cgv_recovery("재확인 실행 상태가 불확실하여 추가 요청을 중단했습니다.")
            return True
        try:
            probe_at = dt.datetime.fromisoformat(record["probe_at"])
            if probe_at.tzinfo is None:
                raise ValueError("missing timezone")
        except (KeyError, TypeError, ValueError):
            self._halt_cgv_recovery("재확인 예정 시각을 읽을 수 없습니다.")
            return True
        self._announce_cgv_recovery()
        if self.dry_run or self.config.local_now() < probe_at:
            return True

        # Claim and fsync BEFORE making any network request. Restarting must
        # neither shorten the pause nor give us a second probe.
        record = self.state.cgv_recovery()
        record.update(status="probing", probe_started_at=self.config.local_now().isoformat())
        self._save_cgv_recovery(record)
        try:
            plan = self._plan_scan(self.config.local_today())
            if not plan.dates:
                raise FetchError("재확인할 상영일이 없습니다.")
            show_date = plan.dates[0]
            self.logger.info("CGV 단일 재확인 시작: %s (재시도·리디렉션 없음)", show_date)
            self._wait_for_cgv_request_slot()
            try:
                payload = self.cgv.fetch_date(show_date, single_attempt=True)
            finally:
                self._mark_cgv_request_finished()
            if (
                not isinstance(payload, dict)
                or str(payload.get("statusCode")) != "0"
                or not isinstance(payload.get("data"), (dict, list))
            ):
                raise FetchError("CGV 정상 일정 응답(statusCode=0, data)을 확인하지 못했습니다.")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
            self._halt_cgv_recovery(reason)
            return True

        record.update(
            status=CGV_RECOVERY_STATUS_RECOVERED,
            finished_at=self.config.local_now().isoformat(),
        )
        self._save_cgv_recovery(record)
        # Process a successful probe in the normal alert pipeline without
        # refetching or marking newly opened showings as already notified.
        self._recovery_payloads[show_date] = payload
        self._announce_cgv_recovery()
        return False

    def _delivery_worker_running(self) -> bool:
        worker = self._delivery_thread
        return worker is not None and worker.is_alive()

    def start_delivery_worker(self) -> None:
        """Start the persistent outbox sender used by the long-running bot."""

        if self.dry_run or self._delivery_worker_running():
            return
        self._delivery_stop_event.clear()
        self._delivery_wake_event.set()
        self._delivery_thread = threading.Thread(
            target=self._delivery_loop,
            name="telegram-delivery-worker",
            daemon=True,
        )
        self._delivery_thread.start()
        self.logger.info(
            "Telegram 영속 발송 대기열 시작: 신규 오픈 최우선, "
            "최대 초당 %d건, 알림별 수신자 시작 순번 순환",
            self.config.telegram_broadcast_rate_per_second,
        )

    def stop_delivery_worker(self, *, timeout: float | None = None) -> None:
        """Stop sending; unsent records stay on disk for the next process."""

        worker = self._delivery_thread
        if worker is None:
            return
        self._delivery_stop_event.set()
        self._delivery_wake_event.set()
        worker.join(timeout=timeout)
        if worker.is_alive():
            self.logger.warning(
                "Telegram 발송 작업 종료 대기 초과: 남은 %d건은 다음 실행에서 복구합니다.",
                self.state.pending_delivery_count(),
            )
        else:
            self.logger.info(
                "Telegram 발송 작업 종료: 대기 %d건",
                self.state.pending_delivery_count(),
            )
        self._delivery_thread = None

    @staticmethod
    def _delivery_due_at(record: Mapping[str, Any]) -> dt.datetime:
        raw = record.get("next_attempt_at")
        if isinstance(raw, str) and raw:
            try:
                parsed = dt.datetime.fromisoformat(raw)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=dt.timezone.utc)
                return parsed.astimezone(dt.timezone.utc)
            except ValueError:
                pass
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    @staticmethod
    def _delivery_sort_key(
        item: tuple[str, Mapping[str, Any]],
    ) -> tuple[int, str, int, str]:
        key, record = item
        try:
            priority = int(record.get("priority", DEFAULT_DELIVERY_PRIORITY))
        except (TypeError, ValueError):
            priority = DEFAULT_DELIVERY_PRIORITY
        sequence = record.get("queue_sequence", 0)
        if type(sequence) is not int or sequence < 0:
            sequence = 0
        return priority, str(record.get("queued_at") or ""), sequence, key

    def _delivery_loop(self) -> None:
        """Drain short priority batches while CGV scanning continues."""

        waiting = False
        while not self._delivery_stop_event.is_set():
            if retry_at := self._telegram_pause_until():
                if not waiting:
                    self.logger.info(self.telegram_send_status_text())
                waiting = True
                remaining = (retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds()
                # New queue entries never wake this flood wait early. Shutdown
                # is interruptible; no worker sleeps for hours on network work.
                self._delivery_stop_event.wait(min(30.0, max(0.05, remaining)))
                continue
            if waiting:
                self.logger.info("Telegram 발송 대기 종료: 신규 오픈 우선으로 재시도합니다.")
                waiting = False
            now = dt.datetime.now(dt.timezone.utc)
            pending = self.state.pending_deliveries()
            due = [
                item
                for item in pending
                if self._delivery_due_at(item[1]) <= now
            ]
            if due:
                due.sort(key=self._delivery_sort_key)
                # Re-read priorities after at most one second's worth of sends,
                # so a new opening can jump ahead of a large seat-alert queue.
                batch_size = min(
                    self.config.telegram_broadcast_rate_per_second,
                    len(due),
                )
                try:
                    self._retry_pending_deliveries(due[:batch_size])
                except Exception:
                    # A malformed old record or an unforeseen Telegram error
                    # must not kill the only sender.  The durable records stay
                    # in place and the worker retries after a short pause.
                    self.logger.exception(
                        "Telegram 발송 작업 오류: 대기열을 보존하고 다시 시도합니다."
                    )
                    self._delivery_wake_event.wait(1.0)
                continue

            wait_seconds = 1.0
            future_due = [
                self._delivery_due_at(record)
                for _key, record in pending
                if self._delivery_due_at(record) > now
            ]
            if future_due:
                wait_seconds = min(
                    wait_seconds,
                    max(0.05, (min(future_due) - now).total_seconds()),
                )
            self._delivery_wake_event.clear()
            self._delivery_wake_event.wait(wait_seconds)

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
        show_date: str | None = None,
        show_time: str | None = None,
        recipients: Sequence[str] | None = None,
    ) -> tuple[int, int, int]:
        """Deliver or durably queue a message for the opted-in subscribers.

        In the long-running service the first value is the number accepted by
        the persistent outbox; without the background worker (tests and
        ``--once``) it counts delivered messages plus safely deferred sends. The total
        counts only selected recipients, so state still advances when nobody
        wants this category (total == 0) instead of retrying forever.

        ``recipients`` addresses named chats instead, for a message that is
        not a subscription at all.
        """

        if self.config.open_only_mode and category in {
            ALERT_SEATS, ALERT_SEATS_SWEET, ALERT_SEATS_UNCLASSIFIED,
        }:
            return 0, 0, 0
        if self.config.seat_alert_sweet_only and category in {ALERT_SEATS, ALERT_SEATS_UNCLASSIFIED}:
            return 0, 0, 0

        subscriber_ids = (
            tuple(recipients)
            if recipients is not None
            else self.state.subscriber_ids_for(
                category,
                seats_available=seats_available,
                show_date=show_date,
                show_time=show_time,
            )
        )
        if subscriber_ids and self._delivery_worker_running():
            added = self.state.queue_broadcast_deliveries(
                subscriber_ids, text, category, show_date=show_date,
                show_time=show_time, seats_available=seats_available,
            )
            self._delivery_wake_event.set()
            self.logger.info(
                "Telegram 발송 대기열 등록: 대상 %d명, 신규 %d건, 현재 %d건, 종류 %s",
                len(subscriber_ids),
                added,
                self.state.pending_delivery_count(),
                category,
            )
            return len(subscriber_ids), 0, len(subscriber_ids)

        delivered = 0
        failed = 0
        deferred = 0
        broadcast_started = time.monotonic()

        def send(chat_id: str) -> tuple[str, TelegramError | None]:
            try:
                self._send_telegram(text, chat_id=chat_id)
            except TelegramError as exc:
                return chat_id, exc
            return chat_id, None

        results: list[tuple[str, TelegramError | None]] = []
        if subscriber_ids:
            subscriber_ids = self.state.rotate_broadcast_recipients(category, subscriber_ids)
            if not self.dry_run:
                self.state.save()
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(
                    self.config.telegram_broadcast_workers, len(subscriber_ids)
                ),
                thread_name_prefix="telegram-broadcast",
            ) as executor:
                results = list(executor.map(send, subscriber_ids))

        for chat_id, error in results:
            if error is not None:
                if isinstance(error, TelegramDeferred):
                    deferred += 1
                else:
                    failed += 1
                if error.recipient_gone:
                    # Blocked or deleted chats never recover, and they cannot
                    # send /stop to remove themselves, so drop them here.
                    self._drop_unreachable_subscriber(chat_id, error)
                else:
                    self.state.queue_pending_delivery(
                        chat_id,
                        text,
                        category,
                        show_date=show_date,
                        show_time=show_time,
                        seats_available=seats_available,
                    )
                    if not isinstance(error, TelegramDeferred):
                        self.logger.error("전송 실패(다음 주기 재시도): %s", error)
            else:
                delivered += 1
        if len(subscriber_ids) > 1:
            self.logger.info(
                "Telegram 발송 완료: 대상 %d명, 성공 %d명, 실패 %d명, %.2f초",
                len(subscriber_ids),
                delivered,
                failed,
                time.monotonic() - broadcast_started,
            )
        if not subscriber_ids:
            if self.state.subscriber_ids():
                self.logger.info(
                    "전송 생략: '%s' 알림을 받는 구독자가 없습니다.", category
                )
            else:
                self.logger.warning(
                    "전송 생략: 등록된 구독자가 없습니다."
                )
        return delivered + deferred, failed, len(subscriber_ids)

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
        pending: Sequence[tuple[str, Mapping[str, Any]]] | None = None,
    ) -> None:
        """Retry only recipients who missed an earlier broadcast."""

        if self.dry_run or self._telegram_pause_until():
            return
        changed = False
        eligible_records: list[tuple[str, Mapping[str, Any]]] = []
        for key, record in (
            sorted(self.state.pending_deliveries(), key=self._delivery_sort_key)
            if pending is None else pending
        ):
            chat_id = record["chat_id"]
            category = record["category"]
            if self.config.seat_alert_sweet_only and category in {ALERT_SEATS, ALERT_SEATS_UNCLASSIFIED}:
                changed = self.state.remove_pending_delivery(key) or changed
                continue
            if self.config.open_only_mode and category in {
                ALERT_SEATS, ALERT_SEATS_SWEET, ALERT_SEATS_UNCLASSIFIED,
            }:
                changed = self.state.remove_pending_delivery(key) or changed
                continue
            # ALERT_SYSTEM has no subscribers by design — it is addressed to
            # the operator — so ask the same source the original send used.
            eligible = {chat_id} if category == ALERT_REPLY else set(
                self._operator_recipients()
                if category == ALERT_SYSTEM
                else self.state.subscriber_ids_for(
                    category,
                    seats_available=record.get("seats_available"),
                    show_date=record.get("show_date"),
                    show_time=record.get("show_time"),
                )
            )
            if chat_id not in eligible:
                changed = self.state.remove_pending_delivery(key) or changed
                continue
            eligible_records.append((key, record))

        def resend(
            item: tuple[str, Mapping[str, Any]],
        ) -> tuple[str, str, TelegramError | None]:
            key, record = item
            chat_id = record["chat_id"]
            try:
                self._send_telegram(record["text"], chat_id=chat_id)
            except TelegramError as exc:
                return key, chat_id, exc
            return key, chat_id, None

        results: list[tuple[str, str, TelegramError | None]] = []
        if eligible_records:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(
                    self.config.telegram_broadcast_workers, len(eligible_records)
                ),
                thread_name_prefix="telegram-retry",
            ) as executor:
                results = list(executor.map(resend, eligible_records))

        for key, chat_id, error in results:
            if error is not None:
                if isinstance(error, TelegramDeferred):
                    changed = self.state.defer_pending_delivery(key, error.retry_at) or changed
                    continue
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
            if not self._delivery_worker_running():
                self.logger.info("재전송 성공: chat_id=%s", chat_id)
        if changed:
            self.state.save()
        if self._delivery_worker_running() and eligible_records:
            failures = sum(1 for _key, _chat_id, error in results
                           if error is not None and not isinstance(error, TelegramDeferred))
            deferred = sum(isinstance(error, TelegramDeferred) for _key, _chat_id, error in results)
            self.logger.info(
                "Telegram 대기열 발송: 처리 %d건, 성공 %d건, 실패 %d건, 대기 %d건, 남음 %d건",
                len(results),
                len(results) - failures - deferred,
                failures,
                deferred,
                self.state.pending_delivery_count(),
            )

    def _subscriber_stats_message(self, *, include_subscribers: bool = True) -> str:
        """Operator-facing aggregate snapshot, optionally with subscriber rows."""

        stats = self.state.subscriber_breakdown()
        total = stats["total"]
        lines = ["📊 구독 현황", f"전체 {total}명"]
        if self.config.open_only_mode:
            lines.append("운영 모드: 신규 오픈 전용 (아래는 저장된 개인 설정)")

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
        lines.append("상영일")
        for selection in SHOW_DAY_SELECTIONS:
            lines.append(
                f"• {SHOW_DAY_LABELS[selection]} — "
                f"{stats['show_days'][selection]}명"
            )

        lines.append("")
        lines.append("잔여 좌석 대상")
        for selection in (SEAT_SELECTION_ALL, SEAT_SELECTION_SWEET):
            lines.append(
                f"• {SEAT_SELECTION_LABELS[selection]} — "
                f"{stats['seat_selections'][selection]}명"
            )

        lines.extend([
            "", "잔여좌석 상영 날짜",
            f"• 전체 날짜 — {stats['seat_dates']['all']}명",
            f"• 날짜 범위 설정 — {stats['seat_dates']['filtered']}명",
            "", "잔여좌석 상영 시간",
            f"• 전체 시간 — {stats['seat_times']['all']}명",
            f"• 시간 범위 설정 — {stats['seat_times']['filtered']}명",
        ])

        lines.append("")
        lines.append("예매 가능 최소 좌석")
        for minimum in MIN_SEATS_CHOICES:
            lines.append(
                f"• {MIN_SEATS_LABELS[minimum]} — "
                f"{stats['min_seats'][minimum]}명"
            )

        if not include_subscribers:
            return "\n".join(lines)

        records = self.state.subscriber_details()
        if records:
            lines.append("")
            lines.append("📋 구독자 목록")
            # Budget by characters, not by a subscriber count: one group with a
            # long title costs several times what a bare chat id does, and
            # overshooting loses the whole message including the totals above.
            budget = STATS_MAX_CHARS - len("\n".join(lines)) - len("\n… 외 999명")
            listed = 0
            for record in records:
                kind = chat_labels.get(record["chat_type"] or "unknown", "미상")
                parts = [record["chat_id"], kind]
                if record["label"]:
                    parts.insert(1, record["label"])
                if joined := self._local_join_date(record["subscribed_at"]):
                    parts.append(joined)
                entry = (
                    f"{listed + 1}. "
                    + " · ".join(parts)
                    + "\n   "
                    + " · ".join(
                        (
                            ALERT_MODE_LABELS[record["alert_mode"]],
                            SHOW_DAY_LABELS[record["show_day"]],
                            _seat_time_label(record["seat_time_range"]),
                            _seat_date_label(record["seat_date_range"]),
                            SEAT_SELECTION_LABELS[record["seat_selection"]],
                            MIN_SEATS_LABELS[record["min_seats"]],
                        )
                    )
                )
                if len(entry) + 1 > budget:
                    break
                budget -= len(entry) + 1
                lines.append(entry)
                listed += 1
            if listed < len(records):
                lines.append(f"… 외 {len(records) - listed}명")
        return "\n".join(lines)

    def _forward_to_operator(
        self, chat: Mapping[str, Any], chat_id: str, text: str
    ) -> None:
        """Relay a subscriber's plain-text message to the operator."""

        operator = self._operator_recipients()
        if not text or not operator or chat_id in operator:
            return
        # The bot is public, so anyone can type into it.  Cap each chat per
        # hour rather than letting one person fill the operator's inbox.
        now = self.config.local_now()
        recent = [
            seen
            for seen in self._forwarded_at.get(chat_id, ())
            if now - seen < dt.timedelta(hours=1)
        ]
        if len(recent) >= FORWARD_MAX_PER_HOUR:
            self._forwarded_at[chat_id] = recent
            self.logger.info(
                "메시지 전달 생략: chat_id=%s 가 1시간 안에 %d건을 넘겼습니다.",
                chat_id,
                FORWARD_MAX_PER_HOUR,
            )
            return
        self._forwarded_at[chat_id] = [*recent, now]

        body = text[:FORWARD_MAX_CHARS]
        if len(text) > FORWARD_MAX_CHARS:
            body += f"\n… (뒷부분 {len(text) - FORWARD_MAX_CHARS}자 생략)"
        who = [chat_id]
        label = str(
            chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        )
        if label:
            who.append(label)
        if not self.state.is_subscribed(chat_id):
            who.append("구독 안 함")
        if len(self._recent_senders) >= FORWARD_RECENT_SENDERS:
            self._recent_senders.pop(next(iter(self._recent_senders)), None)
        self._recent_senders[chat_id] = label
        self._broadcast_message(
            "💬 구독자 메시지\n"
            + " · ".join(who)
            + "\n━━━━━━━━━━━━━━━━━━━━\n"
            + body,
            category=ALERT_SYSTEM,
            recipients=operator,
        )
        # Silence would read as the bot being broken, and the sender has no
        # way to know a person will see this.
        try:
            self._send_or_queue_reply(
                "메시지를 운영자에게 전달했습니다. 답장이 늦을 수 있어요.\n"
                "사용법은 /help, 설정은 /status 로 확인하실 수 있습니다.",
                chat_id=chat_id,
            )
        except TelegramError as exc:
            self.logger.warning("메시지 수신 확인 답장 실패: %s", exc)

    def _handle_reply_command(self, body: str) -> str:
        """Answer one chat by id, the way the relayed message reports it."""

        target, _, message = body.partition(" ")
        target = target.strip()
        message = message.strip()
        if not target or not message:
            return REPLY_GUIDE
        # Telegram chat ids are integers, negative for groups.  Catching a
        # mistyped id here beats sending someone else's answer to a stranger.
        if not re.fullmatch(r"-?\d+", target):
            return (
                f"'{target[:40]}' 은 chat_id 형식이 아닙니다."
                f"\n\n{REPLY_GUIDE}"
            )
        if target in self._operator_recipients():
            return "자기 자신에게는 보내지 않습니다."

        try:
            delivered = self._send_or_queue_reply(
                f"{REPLY_HEADER}\n\n{message}", chat_id=target
            )
            if not delivered:
                return "⏳ Telegram 발송 대기 중입니다. 답장을 대기열에 보관했습니다."
        except TelegramError as exc:
            if exc.recipient_gone:
                self._drop_unreachable_subscriber(target, exc)
                return f"보낼 수 없는 상대입니다. 구독 목록에서 정리했습니다.\n{exc}"
            self.logger.warning("답장 실패: chat_id=%s (%s)", target, exc)
            return f"전송에 실패했습니다. 다시 시도해 주세요.\n{exc}"

        # Name the recipient back, so a wrong id shows up immediately.
        who = self._recent_senders.get(target, "")
        if not who:
            record = self.state.subscriber_details()
            who = next(
                (r["label"] for r in record if r["chat_id"] == target), ""
            )
        known = self.state.is_subscribed(target) or target in self._recent_senders
        description = " · ".join(
            part for part in (target, who, "" if known else "⚠️ 모르는 대상") if part
        )
        self.logger.info("답장 전송: chat_id=%s", target)
        return f"✅ 답장을 보냈습니다.\n{description}"

    def _handle_notice_command(
        self, command: str, body: str
    ) -> tuple[str, bool]:
        """Draft, preview, and send an announcement to every subscriber."""

        if command == ADMIN_NOTICE_SEND_COMMAND:
            draft = self._notice_draft
            if draft is None:
                return ("보낼 공지가 없습니다. /notice 로 먼저 작성하세요.", False)
            text, drafted_at = draft
            age = self.config.local_now() - drafted_at
            if age > dt.timedelta(minutes=NOTICE_DRAFT_TTL_MINUTES):
                self._notice_draft = None
                return (
                    f"작성한 지 {NOTICE_DRAFT_TTL_MINUTES}분이 지나 취소했습니다."
                    " /notice 로 다시 작성하세요.",
                    False,
                )
            delivered, failed, total = self._broadcast_message(
                text, category=ALERT_NOTICE
            )
            self._notice_draft = None
            if self._delivery_worker_running():
                self.logger.info("공지 발송 대기열 등록: 대상 %d명", total)
                result = (
                    f"✅ 공지를 발송 대기열에 등록했습니다.\n대상 {total}명\n"
                    "신규 오픈 알림을 먼저 보낸 뒤 순서대로 전송합니다."
                )
            else:
                self.logger.info(
                    "공지 발송: 대상 %d명, 성공 %d명, 실패 %d명",
                    total,
                    delivered,
                    failed,
                )
                result = (
                    f"✅ 공지를 보냈습니다.\n대상 {total}명 · 성공 {delivered}명"
                    f" · 실패 {failed}명"
                    + (
                        "\n실패한 분에게는 다음 주기에 다시 시도합니다."
                        if failed
                        else ""
                    )
                )
            return (
                result,
                False,
            )

        if not body:
            self._notice_draft = None
            return (NOTICE_GUIDE, False)

        text = f"{NOTICE_HEADER}\n\n{body}"
        self._notice_draft = (text, self.config.local_now())
        recipients = len(self.state.subscriber_ids_for(ALERT_NOTICE))
        return (
            "미리보기 — 아직 아무에게도 가지 않았습니다.\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{text}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"수신 대상 {recipients}명\n\n"
            f"보내려면 {ADMIN_NOTICE_SEND_COMMAND}\n"
            f"고치려면 {ADMIN_NOTICE_COMMAND} 로 다시 작성\n"
            f"{NOTICE_DRAFT_TTL_MINUTES}분 안에 보내지 않으면 취소됩니다.",
            False,
        )

    def _local_join_date(self, stored: str) -> str:
        """The subscription date in the operator's timezone, or "" if unusable."""

        try:
            moment = dt.datetime.fromisoformat(stored)
        except ValueError:
            return ""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return moment.astimezone(ZoneInfo(self.config.timezone_name)).strftime(
            "%m-%d"
        )

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
        if self.config.open_only_mode and self.state.is_subscribed(chat_id):
            if requested not in {ALERT_MODE_OPEN_ONLY}:
                return (
                    "🎟️ 현재 봇은 신규 오픈 전용으로 운영 중입니다.\n"
                    "잔여좌석 조회·취소표 알림은 중단되어 있습니다.\n"
                    f"저장된 개인 설정: {ALERT_MODE_LABELS[current]}\n"
                    "신규 오픈 알림 받기: /mode_open",
                    False,
                )
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
        if self.config.open_only_mode:
            return (f"✅ 알림 종류를 '{label}'으로 변경했습니다.\n상영일 선택: /day", True)
        return (
            f"✅ 알림 종류를 '{label}'으로 변경했습니다.\n\n{MODE_GUIDE}",
            True,
        )

    def _handle_seat_selection_command(
        self, chat_id: str, command: str, argument: str
    ) -> tuple[str, bool]:
        """Return the seat-selection reply and whether its preference changed."""

        if self.config.seat_alert_sweet_only:
            return SWEET_ONLY_GUIDE, False
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

    def _handle_show_day_command(
        self, chat_id: str, command: str, argument: str
    ) -> tuple[str, bool]:
        """Return the show-day reply and whether its preference changed."""

        requested = SHOW_DAY_COMMAND_TARGETS.get(command)
        if requested is None and argument:
            requested = SHOW_DAY_ALIASES.get(argument)
            if requested is None:
                return (f"알 수 없는 상영일 선택입니다.\n\n{SHOW_DAY_GUIDE}", False)
        if not self.state.is_subscribed(chat_id):
            return (
                "🔕 현재 구독 중이 아닙니다. /start로 구독한 뒤 설정할 수 있습니다."
                f"\n\n{SHOW_DAY_GUIDE}",
                False,
            )

        if requested is None:
            current = SHOW_DAY_LABELS[self.state.show_day_selection(chat_id)]
            return (
                f"📅 현재 알림 상영일\n→ {current}\n\n{SHOW_DAY_GUIDE}",
                False,
            )

        changed = self.state.set_show_day_selection(chat_id, requested)
        label = SHOW_DAY_LABELS[requested]
        if not changed:
            return (f"📅 이미 이렇게 설정되어 있습니다.\n→ {label}", False)
        return (f"✅ 알림 상영일을 변경했습니다.\n→ {label}", True)

    def _handle_seat_time_command(
        self, chat_id: str, command: str, body: str
    ) -> tuple[str, bool]:
        if not self.state.is_subscribed(chat_id):
            return ("먼저 /start로 구독해주세요.\n\n" + SEAT_TIME_GUIDE, False)
        if command == "/time" and not body:
            return (
                "🕒 현재 잔여좌석 상영 시간: "
                + _seat_time_label(self.state.seat_time_range(chat_id))
                + "\n\n" + SEAT_TIME_GUIDE,
                False,
            )
        fields = body.split()
        if (command == "/time_all" and fields) or (
            command == "/time"
            and (len(fields) != 2 or any(_clock_minutes(part) is None for part in fields))
        ):
            return ("시간 형식을 확인해주세요. 예: /time 18:00 23:59\n\n" + SEAT_TIME_GUIDE, False)
        requested = tuple(fields) if command == "/time" else None
        changed = self.state.set_seat_time_range(chat_id, requested)
        label = _seat_time_label(self.state.seat_time_range(chat_id))
        note = ""
        if ALERT_SEATS not in ALERT_MODES[self.state.alert_mode(chat_id)]:
            note = "\n참고: 현재 잔여좌석 알림이 꺼져 있습니다. /mode 확인"
        return (
            ("✅ 잔여좌석 상영 시간을 변경했습니다." if changed else "🕒 이미 설정된 상영 시간입니다.")
            + f"\n→ {label} (한국시간)\n{SEAT_TIME_NOTE}{note}\n설정 해제: /time_all",
            changed,
        )

    def _handle_seat_date_command(
        self, chat_id: str, command: str, body: str
    ) -> tuple[str, bool]:
        if not self.state.is_subscribed(chat_id):
            return ("먼저 /start로 구독해주세요.\n\n" + SEAT_DATE_GUIDE, False)
        if command == "/date" and not body:
            return (
                "📅 현재 잔여좌석 상영 날짜: "
                + _seat_date_label(self.state.seat_date_range(chat_id))
                + "\n\n" + SEAT_DATE_GUIDE,
                False,
            )
        fields = body.split()
        if (command == "/date_all" and fields) or (
            command == "/date" and len(fields) not in {1, 2}
        ):
            return ("날짜 한 개 또는 시작일·종료일 두 개를 입력해주세요.\n\n" + SEAT_DATE_GUIDE, False)
        try:
            requested = None
            if command == "/date":
                requested = (
                    _compact_date_input(fields[0]),
                    _compact_date_input(fields[1]) if len(fields) == 2 else None,
                )
            changed = self.state.set_seat_date_range(chat_id, requested)
        except ValueError as exc:
            return (f"{exc}\n\n{SEAT_DATE_GUIDE}", False)
        label = _seat_date_label(self.state.seat_date_range(chat_id))
        note = ""
        if ALERT_SEATS not in ALERT_MODES[self.state.alert_mode(chat_id)]:
            note = "\n참고: 현재 잔여좌석 알림이 꺼져 있습니다. /mode 확인"
        return (
            ("✅ 잔여좌석 상영 날짜를 변경했습니다." if changed else "📅 이미 설정된 상영 날짜입니다.")
            + f"\n→ {label}\n{SEAT_DATE_NOTE}{note}"
            + "\n오늘부터 28일 조회 범위 안에서 적용됩니다. 설정 해제: /date_all",
            changed,
        )

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
            chat_id = str(chat["id"])
            if not text.startswith("/"):
                # Somebody wrote a sentence, not a command.  Relay it: the
                # bot is public, and a question typed here would otherwise
                # vanish into silence.
                self._forward_to_operator(chat, chat_id, text)
                continue

            fields = text.split()
            command = fields[0].lower().split("@", 1)[0]
            argument = fields[1].lower() if len(fields) > 1 else ""
            # An announcement body is prose: keep its case and spacing.
            body = text.split(None, 1)[1].strip() if len(fields) > 1 else ""
            label = str(
                chat.get("title")
                or chat.get("username")
                or chat.get("first_name")
                or ""
            )
            chat_type = str(chat.get("type") or "")
            reply = ""
            signup_notice = "⏸ 현재 신규 구독을 잠시 중단했습니다."
            subscription_prompt = (
                "알림을 받으려면 /start를 보내주세요."
                if self.config.new_subscriptions_enabled
                else signup_notice
            )

            if (
                not self.config.new_subscriptions_enabled
                and not self.state.is_subscribed(chat_id)
                and command in (
                    {"/start", "/subscribe"}
                    | MODE_COMMANDS
                    | SHOW_DAY_COMMANDS
                    | SEAT_TIME_COMMANDS
                    | SEAT_DATE_COMMANDS
                    | SEAT_SELECTION_COMMANDS
                    | MIN_SEATS_COMMANDS
                )
            ):
                # Keep polling and acknowledging commands, without adding a
                # subscriber or changing their settings. This also covers
                # groups, aliases and former subscribers trying to rejoin.
                reply = signup_notice + "\n신규 구독이 재개된 뒤 /start를 보내주세요."
            elif command in {"/start", "/subscribe"}:
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
                    "\n• 모든 요일 상영분 · 잔여좌석 날짜·시간 제한 없음"
                    f"\n• 잔여 좌석은 {'명당 좌석만 (운영 정책)' if self.config.seat_alert_sweet_only else '모든 A열 제외 좌석'}"
                    "\n• 1석부터 모두 알림"
                    "\n\n필요할 때만 설정을 바꾸세요."
                    "\n• 알림 종류 선택: /mode"
                    "\n• 주말 상영분만 받기: /day_weekend"
                    "\n• 잔여좌석 날짜 선택: /date · 시간 선택: /time"
                    f"\n• 잔여 좌석 대상 {'확인' if self.config.seat_alert_sweet_only else '선택'}: /seat"
                    f"\n• {'명당 구역 확인' if self.config.seat_alert_sweet_only else '명당 좌석만 받기'}: /seat_sweet"
                    "\n• 2석 이상 남았을 때만 받기: /count_2"
                    "\n※ 신규 예매 오픈은 좌석·날짜·시간 설정과 관계없이 알려드립니다."
                    " (기존 알림 종류·요일 설정은 유지)"
                    "\n현재 설정 /status · 자세한 설명 /desc · 해지 /stop"
                )
            elif command in {"/stop", "/unsubscribe"}:
                removed = self.state.remove_subscriber(chat_id)
                state_changed = state_changed or removed
                subscribers_changed = subscribers_changed or removed
                reply = (
                    "🔕 알림 구독을 해지했습니다."
                    if removed
                    else "현재 알림을 구독하고 있지 않습니다."
                )
                reply += f"\n{subscription_prompt}"
                if not self.config.new_subscriptions_enabled:
                    reply += "\n신규 구독이 재개될 때까지 재구독할 수 없습니다."
            elif command == "/status":
                if self.state.is_subscribed(chat_id):
                    mode = self.state.alert_mode(chat_id)
                    mode_label = ALERT_MODE_LABELS[mode]
                    reply = (
                        "✅ 현재 CGV 용산 IMAX 알림을 구독 중입니다.\n"
                        f"알림 종류: {mode_label}\n"
                        "알림 상영일: "
                        f"{SHOW_DAY_LABELS[self.state.show_day_selection(chat_id)]}"
                    )
                    reply += (
                        "\n잔여좌석 상영 날짜: "
                        + _seat_date_label(self.state.seat_date_range(chat_id))
                        + " (신규 오픈에는 미적용)"
                        "\n잔여좌석 상영 시간: "
                        + _seat_time_label(self.state.seat_time_range(chat_id))
                        + " (한국시간, 신규 오픈에는 미적용)"
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
                        "\n알림 상영일 변경: /day"
                        "\n잔여좌석 날짜 변경: /date · 해제: /date_all"
                        "\n잔여좌석 상영 시간 변경: /time · 해제: /time_all"
                        "\n잔여 좌석 대상 변경: /seat"
                        "\n예매 가능 최소 좌석 변경: /count"
                    )
                else:
                    reply = f"🔕 현재 구독 중이 아닙니다.\n{subscription_prompt}"
                recovery_status = self.cgv_recovery_status_text()
                if recovery_status:
                    reply += f"\n\n{recovery_status}"
                if seat_status := self.seat_api_status_text():
                    reply += f"\n\n{seat_status}"
                if send_status := self.telegram_send_status_text():
                    reply += f"\n\n{send_status}"
            elif command in MODE_COMMANDS:
                reply, mode_changed = self._handle_mode_command(
                    chat_id, command, argument
                )
                state_changed = state_changed or mode_changed
            elif self.config.open_only_mode and command in (
                SEAT_SELECTION_COMMANDS | MIN_SEATS_COMMANDS | SEAT_TIME_COMMANDS | SEAT_DATE_COMMANDS
            ):
                reply = (
                    "🎟️ 현재 봇은 신규 오픈 전용으로 운영 중입니다.\n"
                    "잔여좌석 조회·취소표 알림은 중단되어 좌석·최소 좌석 수·날짜·시간 설정은 적용되지 않습니다.\n"
                    "기존 설정은 그대로 보관합니다. 현재 설정: /status"
                )
            elif command in SHOW_DAY_COMMANDS:
                reply, show_day_changed = self._handle_show_day_command(
                    chat_id, command, argument
                )
                state_changed = state_changed or show_day_changed
            elif command in SEAT_DATE_COMMANDS:
                reply, seat_date_changed = self._handle_seat_date_command(
                    chat_id, command, body
                )
                state_changed = state_changed or seat_date_changed
            elif command in SEAT_TIME_COMMANDS:
                reply, seat_time_changed = self._handle_seat_time_command(
                    chat_id, command, body
                )
                state_changed = state_changed or seat_time_changed
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
            elif command in ADMIN_COMMANDS and chat_id == str(
                self.config.telegram_chat_id
            ):
                # Anyone else falls through to the unknown-command reply, so
                # these are not advertised to subscribers at all.
                if command == ADMIN_STATS_COMMAND:
                    reply = self._subscriber_stats_message()
                elif command == ADMIN_STATS_SUMMARY_COMMAND:
                    reply = self._subscriber_stats_message(
                        include_subscribers=False
                    )
                elif command == ADMIN_REPLY_COMMAND:
                    reply = self._handle_reply_command(body)
                elif command == ADMIN_CGV_RESUME_COMMAND:
                    reply = self._handle_cgv_resume_command()
                else:
                    reply, _changed = self._handle_notice_command(command, body)
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
                    "/day - 알림 상영일 선택\n"
                    "/day_all - 모든 요일 상영분 받기\n"
                    "/day_weekend - 토·일 상영분만 받기\n"
                    "/date - 잔여좌석 상영 날짜 확인·변경\n"
                    "/date_all - 잔여좌석 날짜 제한 해제\n"
                    "/time - 잔여좌석 상영 시간 확인·변경\n"
                    "/time_all - 잔여좌석 전체 시간 받기\n"
                    f"/seat - {'명당 구역 확인' if self.config.seat_alert_sweet_only else '잔여 좌석 대상 선택'}\n"
                    f"/seat_all - {'명당 전용 안내 (전체 좌석 전환 불가)' if self.config.seat_alert_sweet_only else '모든 A열 제외 좌석 받기 (기본)'}\n"
                    f"/seat_sweet - {'명당 좌석만 받기 (운영 정책)' if self.config.seat_alert_sweet_only else '명당 좌석만 받기'}\n"
                    "/count - 예매 가능 최소 좌석 선택\n"
                    "/count_1 - 1석부터 모두 받기 (기본)\n"
                    "/count_2 - 2석 이상 남았을 때만 받기\n"
                    "/desc - 봇 설명과 사용 방법\n"
                    "/coffee - 개발자에게 커피 후원\n"
                    "/help - 전체 명령어 보기\n\n"
                    "/mode · /day · /date · /time · /seat · /count 는 선택 사항입니다.\n"
                    f"{'잔여좌석은 명당만, 신규 오픈은 좌석 제한 없이 받습니다.' if self.config.seat_alert_sweet_only else '그대로 두시면 모든 알림을 받습니다.'}\n"
                    "시간 설정 예: /time 18:00 23:59\n"
                    "날짜 설정 예: /date 20261003 (당일 포함 이후)\n"
                    "날짜·시간 제한은 잔여좌석 알림에만 적용됩니다."
                )
            elif command in {"/desc", "/description"}:
                reply = (
                    "🎬 용아맥 오디세이 알림 봇\n\n"
                    "CGV 용산아이파크몰 IMAX의 오디세이 예매 오픈과 "
                    "예매 가능한 좌석을 확인해 알려주는 비공식 봇입니다.\n"
                    "한국시간 기준 오늘부터 28일간의 상영 회차를 감시합니다.\n\n"
                    "🔔 알려드리는 내용\n"
                    "• 새 IMAX 상영 회차 예매 오픈\n"
                    f"• 예매 가능한 {'명당' if self.config.seat_alert_sweet_only else 'A열 제외'} 좌석 (취소표 포함)\n"
                    "• 상영일·상영 시간·좌석 번호·잔여 좌석·변동 좌석수·예매 링크\n\n"
                    "🚫 좌석 알림 제외\n"
                    "• A열만 남은 경우\n"
                    "• 잔여 좌석이 0석인 경우\n"
                    "• 상영이 이미 시작된 경우\n"
                    "※ 신규 회차 오픈 알림은 매진이어도 전송\n\n"
                    "⚙️ 기본 설정\n"
                    "• 신규 예매 오픈 + 예매 가능 좌석 알림\n"
                    f"• {'명당 좌석만 알림 (운영 정책)' if self.config.seat_alert_sweet_only else '모든 A열 제외 좌석 알림'}\n"
                    "• 모든 요일·전체 날짜·전체 상영 시간\n"
                    "• 별도 설정 없이 바로 사용 가능\n\n"
                    "🔧 알림 종류 선택\n"
                    "• /mode — 현재 설정과 선택 방법 확인\n"
                    "• /mode_all — 신규 오픈과 예매 가능 좌석 모두 받기 (기본)\n"
                    "• /mode_open — 신규 예매 오픈만\n"
                    "• /mode_seats — 예매 가능 좌석만\n\n"
                    "📅 알림 상영일 선택 (선택 사항)\n"
                    "기본값은 모든 요일 상영분 받기입니다.\n"
                    "• /day_all — 모든 요일 상영분 받기 (기본)\n"
                    "• /day_weekend — 토·일 상영분만 받기\n"
                    "• /day — 현재 설정 확인\n"
                    "※ 알림 도착 요일이 아니라 영화 상영일 기준이며, "
                    "오픈·좌석 알림에 모두 적용\n\n"
                    "📆 잔여좌석 상영 날짜 (선택 사항)\n"
                    "• /date — 현재 설정과 사용 방법\n"
                    "• /date 20261003 — 10월 3일 포함 이후\n"
                    "• /date 20261003 20261005 — 10월 3~5일 (양 끝 포함)\n"
                    "• /date_all — 날짜 제한 해제 (기본)\n"
                    "※ 하이픈 없이 YYYYMMDD 입력. CGV 상영일 기준이며 오늘부터 28일 조회 범위 안에서 적용. "
                    "기간이 지나도 자동 해제되지 않으며 신규 오픈에는 날짜 제한 없음\n\n"
                    "🕒 잔여좌석 상영 시간 (선택 사항)\n"
                    "• /time — 현재 설정과 사용 방법\n"
                    "• /time 18:00 23:59 — 저녁 상영만\n"
                    "• /time 22:00 02:00 — 자정을 넘는 심야 상영만\n"
                    "• /time_all — 전체 시간 받기 (기본)\n"
                    "※ 한국시간 상영 시작시각 기준, 시작·끝 시각 포함. "
                    "잔여좌석 알림에만 적용하며 신규 오픈에는 시간 제한 없음\n\n"
                    f"💺 잔여 좌석 대상 ({'명당 전용 운영' if self.config.seat_alert_sweet_only else '선택 사항'})\n"
                    f"{'모든 구독자에게 아래 명당 구역만 알립니다. 위치 미확인 좌석은 제외합니다.' if self.config.seat_alert_sweet_only else '기본값은 모든 A열 제외 좌석입니다.'}\n"
                    f"• /seat_all — {'명당 전용 운영 중에는 전체 좌석 전환 불가' if self.config.seat_alert_sweet_only else '모든 A열 제외 좌석 알림 (기본)'}\n"
                    f"• /seat_sweet — 아래 세 구역{' 확인' if self.config.seat_alert_sweet_only else '만 알림'}\n"
                    "  Extremer: F16~29, G16~29\n"
                    "  Experienced: H13~32, I13~32\n"
                    "  SweetSpot: J11~34, K11~34, L11~34\n"
                    "• /seat — 현재 좌석 대상 확인\n"
                    "※ 신규 예매 오픈 알림에는 좌석·날짜·시간 제한 미적용 (알림 종류·요일 설정 유지)\n\n"
                    "🎫 예매 가능 최소 좌석 (선택 사항)\n"
                    "기본값은 1석부터 모두 받기입니다.\n"
                    "• /count_1 — 1석부터 모두 받기 (기본)\n"
                    "• /count_2 — 2석 이상 남아 있을 때만\n"
                    "• /count — 현재 설정 확인\n\n"
                    "📌 사용 방법\n"
                    "1. /start — 알림 구독\n"
                    "2. 주말 상영분만 원하면 /day_weekend (선택 사항)\n"
                    f"3. {'명당 구역 확인: /seat_sweet' if self.config.seat_alert_sweet_only else '명당만 원하면 /seat_sweet (선택 사항)'}\n"
                    "4. 영화·극장·날짜가 선택된 예매 바로가기 링크 열기\n"
                    "5. CGV 화면에서 IMAX 버튼 선택 후 예매\n\n"
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

            if self.config.open_only_mode:
                reply = self._open_only_command_reply(command, chat_id, reply)

            if (
                not self.config.new_subscriptions_enabled
                and command in {"/help", "/desc", "/description"}
            ):
                reply = (
                    f"{signup_notice}\n"
                    "기존 구독자의 설정 변경·구독 해지는 계속 이용할 수 있습니다.\n"
                    "해지 후에는 신규 구독이 재개될 때까지 재구독할 수 없습니다.\n\n"
                    + reply
                )

            try:
                self._send_or_queue_reply(reply, chat_id=chat_id)
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

    def _open_only_command_reply(self, command: str, chat_id: str, reply: str) -> str:
        """Describe the effective service without overwriting saved preferences."""
        note = (
            "🎟️ 신규 오픈 전용 운영\n"
            "잔여좌석 조회·취소표 알림은 중단되어 있습니다.\n"
            "좌석·최소 좌석 수 설정은 적용되지 않으며 상영일 필터는 유지됩니다."
        )
        if command in {"/start", "/subscribe", "/status"}:
            if not self.state.is_subscribed(chat_id):
                return reply
            current = self.state.alert_mode(chat_id)
            effective = (
                "신규 예매 오픈"
                if ALERT_OPEN in ALERT_MODES[current]
                else "없음 (기존 '잔여좌석만' 설정) — 받으려면 /mode_open"
            )
            return (
                "✅ 현재 CGV 용산 IMAX 알림을 구독 중입니다.\n\n"
                f"{note}\n\n현재 수신 알림: {effective}\n"
                f"알림 상영일: {SHOW_DAY_LABELS[self.state.show_day_selection(chat_id)]}\n"
                "설정 확인 /status · 상영일 선택 /day · 설명 /desc · 해지 /stop"
                + (f"\n\n{status}" if (status := self.cgv_recovery_status_text()) else "")
            )
        if command in {"/help", "/desc", "/description"}:
            return (
                f"🎬 CGV {self.config.site_name} IMAX 알림 봇\n\n{note}\n\n"
                f"영화: {self.config.movie_label}\n"
                "상영 일정에 새 날짜·시작시간이 나타나면 중복 없이 알려드립니다.\n"
                "매진·좌석 수 미확인 회차도 신규이면 알립니다.\n"
                "좌석 수는 일정 응답에 포함된 값만 표시하며 좌석 번호는 조회하지 않습니다.\n"
                "기존 '잔여좌석만' 구독자는 /mode_open으로 바꾸면 신규 알림을 받습니다.\n\n"
                "/start - 알림 구독\n/stop - 알림 해지\n/status - 현재 구독 및 설정\n"
                "/mode - 현재 알림 설정\n/mode_open - 신규 예매 오픈 받기\n"
                "/day - 알림 상영일 선택\n/day_all - 모든 요일\n/day_weekend - 토·일 상영분\n"
                "/desc - 봇 설명과 사용 방법\n/coffee - 개발자에게 커피 후원\n/help - 명령어 보기"
            )
        return reply

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
            prefetched = show_date in self._recovery_payloads
            if not prefetched:
                self._wait_for_cgv_request_slot()
            try:
                payload = (
                    self._recovery_payloads.pop(show_date)
                    if prefetched else self.cgv.fetch_date(show_date)
                )
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
                elif "HTTP 403" in message:
                    # Stop this cycle after a forbidden response, then let the
                    # main loop retry after the configured polling interval.
                    tally.rate_limited = True
                    tally.forbidden_requests += 1
                    self.logger.warning(
                        "CGV HTTP 403 차단 감지: 남은 일정 조회 %d일을 즉시 생략합니다.",
                        len(dates) - index - 1,
                    )
            except Exception as exc:  # Keep a single malformed date from stopping the watcher.
                errors[show_date] = f"예상하지 못한 조회 오류: {type(exc).__name__}"
                if not self.dry_run:
                    tally.dirty = (
                        self.state.note_schedule_failure(show_date) or tally.dirty
                    )
            finally:
                if not prefetched:
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

        if self.config.open_only_mode:
            self._alert_new_sessions_only(sessions, tally)
            return

        if self._seat_lookup_paused():
            # No stale seat maps or schedule-total fallback alerts while the
            # seat endpoint is blocked. Keep the last verified baseline.
            for session in sessions:
                key = session.notification_key(
                    site_no=self.config.site_no, movie_no=self.config.movie_no
                )
                if self.state.was_notified(key) and session.remaining_seats != 0:
                    tally.seat_detail_skipped += 1
                    tally.deferred_keys.add(key)
            self._alert_new_sessions_only(sessions, tally)
            return

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
        freshly_fetched: set[str] = set()
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
                    if session.screen_no and session.screen_sequence:
                        freshly_fetched.add(key)
                        self._clear_seat_backoff()
                except FetchError as exc:
                    message = str(exc)
                    tally.seat_detail_errors += 1
                    tally.seat_detail_error_sample = (
                        tally.seat_detail_error_sample or message
                    )
                    snapshots[key] = SeatSnapshot(
                        total=session.remaining_seats or 0
                    )
                    if "HTTP 429" in message or "HTTP 403" in message:
                        if "HTTP 429" in message:
                            tally.seat_rate_limited_requests += 1
                            status_code = 429
                        else:
                            tally.seat_forbidden_requests += 1
                            status_code = 403
                        self._pause_seat_lookup(status_code)
                        remaining_sessions = seat_candidates[index + 1 :]
                        tally.seat_detail_skipped += len(remaining_sessions)
                        # Keep successful responses from earlier in this
                        # batch, but don't emit cached maps or guesses for
                        # unverified shows or overwrite their saved baseline.
                        for existing_key in previously_notified:
                            if existing_key in freshly_fetched:
                                continue
                            snapshots.pop(existing_key, None)
                            tally.deferred_keys.add(existing_key)
                            verdicts[existing_key] = "보류·좌석 API 대기"
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
                        text,
                        category=ALERT_OPEN,
                        show_date=chunk_sessions[0].date,
                    )
                    for session in chunk_sessions:
                        self.state.mark_notified(session_keys[session], session)
                        verdicts[session_keys[session]] = (
                            "대기열·예매 오픈"
                            if self._delivery_worker_running()
                            else "발송·예매 오픈"
                        )
                        tally.dirty = True
                    tally.new_sessions += len(chunk_sessions)
                    if self._delivery_worker_running():
                        self.logger.info(
                            "최우선 대기열: 신규 회차 알림 %d개, 대상 %d명",
                            len(chunk_sessions),
                            total,
                        )
                    else:
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

            if current.uses_unclassified_fallback and not self.config.seat_alert_sweet_only:
                tally.unclassified_fallback_alerts += 1
            # Row A could not be ruled out, so say so on the line that reports
            # the send rather than on one of its own: a line that is not a
            # delivery should not read like one.
            unclassified = (
                " ⚠️A열 여부 미확인" if current.uses_unclassified_fallback else ""
            )

            tally.seat_changes += 1
            if self.dry_run:
                self.logger.info(
                    "드라이런 예매 가능 좌석: %s (%d석)%s",
                    _session_line(session),
                    current.total,
                    unclassified,
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
                all_category,
                seats_available=open_all,
                show_date=session.date,
                show_time=session.start_time,
            ):
                delivered, failed, total = self._broadcast_message(
                    seat_change_message(session, previous, current, self.config),
                    category=all_category,
                    seats_available=open_all,
                    show_date=session.date,
                    show_time=session.start_time,
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
                    ALERT_SEATS_SWEET,
                    seats_available=open_sweet,
                    show_date=session.date,
                    show_time=session.start_time,
                )
            ):
                sweet_delivered, sweet_failed, sweet_total = self._broadcast_message(
                    seat_change_message(
                        session,
                        previous,
                        current,
                        self.config,
                        availability=sweet_delivery,
                    ),
                    category=ALERT_SEATS_SWEET,
                    seats_available=open_sweet,
                    show_date=session.date,
                    show_time=session.start_time,
                )
                delivered += sweet_delivered
                failed += sweet_failed
                total += sweet_total
            if delivered or failed or total == 0:
                self.state.set_seat_snapshot(key, current)
                self.state.note_seat_alert(key, self.config.local_now())
                verdicts[key] = (
                    f"대기열·예매 가능 {current.total}석"
                    if self._delivery_worker_running()
                    else f"발송·예매 가능 {current.total}석"
                )
                tally.dirty = True
                if self._delivery_worker_running():
                    self.logger.info(
                        "좌석 대기열: %s (%d석)%s, 대상 %d명",
                        _session_line(session),
                        current.total,
                        unclassified,
                        total,
                    )
                else:
                    self.logger.info(
                        "전송: 예매 가능 좌석 %s (%d석)%s, 성공 %d명, 실패 %d명",
                        _session_line(session),
                        current.total,
                        unclassified,
                        delivered,
                        failed,
                    )

        self._record_verdicts(tally, sessions, verdicts, session_keys, snapshots)

    def _alert_new_sessions_only(
        self, sessions: Sequence[BookingSession], tally: "_CycleTally"
    ) -> None:
        """Use only schedule data; never load, compare or refresh seat maps."""
        new_sessions = [
            session for session in sessions
            if not self.state.was_notified(session.notification_key(
                site_no=self.config.site_no, movie_no=self.config.movie_no,
            ))
        ]
        if self.dry_run:
            tally.new_sessions += len(new_sessions)
            self.logger.info("드라이런: 신규 오픈 전용, 새 회차 %d개", len(new_sessions))
            return
        for text, chunk_sessions in message_chunks(new_sessions, self.config):
            delivered, failed, total = self._broadcast_message(
                text, category=ALERT_OPEN, show_date=chunk_sessions[0].date,
            )
            for session in chunk_sessions:
                self.state.mark_notified(session.notification_key(
                    site_no=self.config.site_no, movie_no=self.config.movie_no,
                ), session)
            tally.new_sessions += len(chunk_sessions)
            tally.dirty = True
            # The outbox is saved before these de-duplication records.
            self._flush_state(tally)
            self.logger.info(
                "신규 오픈 전용: 새 회차 %d개, 대상 %d명, 접수/성공 %d명, 실패 %d명 (좌석 API 호출 없음)",
                len(chunk_sessions), total, delivered, failed,
            )

    def run_cycle(self) -> CycleResult:
        if self._cgv_recovery_blocks_scan():
            return CycleResult(0, 0, 0, 0, cgv_paused=True)
        # Snapshot before sending anything this cycle. A fresh failure should
        # wait until the next cycle instead of being retried immediately.
        pending_retries = (
            ()
            if self._delivery_worker_running()
            else self.state.pending_deliveries()
        )
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
                "좌석 상세 조회 오류 %d개: %s (%s)",
                tally.seat_detail_errors,
                tally.seat_detail_error_sample,
                "차단 중에는 새 좌석 알림 보류, 신규 오픈 조회 유지"
                if self._seat_lookup_paused()
                else "6석 이하는 보류, 7석 이상은 전체 잔여 수 기준 알림",
            )

        # Only the automatic probe's success is provisional.  After an
        # operator resume a 403 falls through to the poll-interval retry.
        cgv_paused = (
            tally.forbidden_requests > 0
            and self.state.cgv_recovery().get("status") == CGV_RECOVERY_STATUS_RECOVERED
        )
        if cgv_paused:
            self._halt_cgv_recovery("재확인 성공 후 정상 조회에서 HTTP 403이 다시 발생했습니다.")

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
                and not cgv_paused
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
        if not self._delivery_worker_running():
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
            "일정 HTTP 429 %d개, 일정 HTTP 403 %d개, "
            "좌석 HTTP 429 %d개, 좌석 HTTP 403 %d개, 일정 생략 %d일, 좌석상세 생략 %d개, "
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
            tally.forbidden_requests,
            tally.seat_rate_limited_requests,
            tally.seat_forbidden_requests,
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
            forbidden_requests=tally.forbidden_requests,
            schedule_skipped_dates=tally.schedule_skipped_dates,
            seat_detail_skipped=tally.seat_detail_skipped,
            requested_dates=len(dates),
            full_scan=plan.full_scan,
            suppressed_closed=tally.suppressed_closed,
            deferred_rechecks_skipped=tally.deferred_rechecks_skipped,
            cgv_paused=cgv_paused,
            seat_forbidden_requests=tally.seat_forbidden_requests,
            seat_rate_limited_requests=tally.seat_rate_limited_requests,
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
    if config.seat_alert_sweet_only:
        logger.info("잔여좌석 명당 전용 운영: 모든 구독자에게 명당만 알림, 신규 오픈 좌석 제한 없음")
    if config.open_only_mode:
        logger.info("신규 오픈 전용 운영: 일정 API만 조회, 좌석 API·취소표 알림 중단")
    else:
        logger.info("신규 오픈·잔여 좌석 운영: 좌석 API 403·429 대기는 일정 조회와 분리")
    if config.subscriptions_enabled:
        logger.info(
            "Telegram 명령은 CGV 조회와 별도로 %d초 간격으로 확인합니다.",
            config.telegram_command_poll_seconds,
        )
        logger.info(
            "Telegram 신규 구독: %s (기존 구독자 설정·해지 명령 유지)",
            "허용" if config.new_subscriptions_enabled else "중단",
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
    if not args.dry_run:
        watcher.start_delivery_worker()
    if config.subscriptions_enabled and not args.dry_run:
        command_thread = threading.Thread(
            target=run_command_loop,
            args=(watcher, command_stop_event),
            name="telegram-command-poller",
            daemon=True,
        )
        command_thread.start()

    consecutive_rate_limit_cycles = 0
    consecutive_forbidden_cycles = 0
    matrix_checked = False
    while not stop_requested:
        if (
            not config.dynamic_date_window
            and config.local_today() > config.target_end
        ):
            logger.info("감시 대상 마지막 날짜가 지나 정상 종료합니다.")
            break
        started = time.monotonic()
        matrix_report = None
        if config.cgv_public_matrix_request_id and not matrix_checked and not args.dry_run:
            matrix_checked = True
            matrix_report = run_matrix_once(
                config.cgv_public_matrix_request_id, config.state_file.parent,
                config.cgv_public_matrix_base_date, config.cgv_public_matrix_compare_date,
                logger, should_stop=lambda: stop_requested,
            )
        if matrix_report is not None:
            # Comparisons never feed detections/notifications or recovery state.
            # Telegram workers keep running; normal CGV polling cannot overlap.
            # Use at least the existing 403 cooldown before normal scanning.
            cooldown = max(1800, config.forbidden_backoff_seconds,
                           config.rate_limit_backoff_initial_seconds,
                           *(item.get("retry_after_seconds", 0) for item in matrix_report["cases"]))
            deadline = time.monotonic() + cooldown
            logger.info("CGV 헤더 비교 종료: 정상 조회 전 %d초 대기 (기존 복구 상태 유지)", cooldown)
            while not stop_requested and time.monotonic() < deadline:
                time.sleep(min(0.5, deadline - time.monotonic()))
            continue
        result = watcher.run_cycle()
        if result.cgv_paused:
            # The durable recovery gate controls whether any CGV request is
            # allowed. Telegram command/sender threads keep running meanwhile.
            next_interval = 1
        elif result.forbidden_requests:
            consecutive_forbidden_cycles += 1
            consecutive_rate_limit_cycles = 0
            next_interval = config.forbidden_backoff_seconds
            logger.warning(
                "CGV HTTP 403 차단 감지: 다음 조회는 %d분 뒤에 시도합니다. "
                "(이번 주기 %d개, 연속 %d회)",
                max(1, next_interval // 60),
                result.forbidden_requests,
                consecutive_forbidden_cycles,
            )
        elif result.rate_limited_requests:
            consecutive_rate_limit_cycles += 1
            consecutive_forbidden_cycles = 0
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
            if consecutive_forbidden_cycles:
                logger.info(
                    "CGV HTTP 403 차단이 해제되어 %d초 조회 주기로 복귀합니다.",
                    config.poll_interval_seconds,
                )
            if consecutive_rate_limit_cycles:
                logger.info(
                    "CGV HTTP 429 요청 제한이 해제되어 %d초 조회 주기로 복귀합니다.",
                    config.poll_interval_seconds,
                )
            consecutive_forbidden_cycles = 0
            consecutive_rate_limit_cycles = 0
            next_interval = config.poll_interval_seconds
        elapsed = time.monotonic() - started
        if result.cgv_paused or result.forbidden_requests or result.rate_limited_requests:
            # The log promises a cooldown after the block response, not merely
            # a start-to-start interval that includes time already spent.
            sleep_seconds = float(next_interval)
        else:
            sleep_seconds = max(0.5, next_interval - elapsed)
        deadline = time.monotonic() + sleep_seconds
        while not stop_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    command_stop_event.set()
    if command_thread is not None:
        command_thread.join(timeout=config.request_timeout_seconds + 1)
    watcher.stop_delivery_worker(timeout=config.request_timeout_seconds + 1)
    logger.info("사용자 요청으로 감시기를 종료합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
