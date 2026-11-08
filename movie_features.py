"""Movie catalog, per-chat selections and localized Telegram controls.

Only the polling worker populates the catalog; commands never call CGV.
State and outbox updates share StateStore's re-entrant lock and atomic save.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import re


def choose(lang, ko, en):
    return en if lang == "en" else ko


class MovieStateMixin:
    def configure_movies(self, movie_no, movie_label):
        with self._lock:
            self.default_movie_no = movie_no
            for name in ("movie_catalog", "movie_sessions", "languages"):
                if not isinstance(self.data.setdefault(name, {}), dict):
                    raise RuntimeError(f"Invalid persisted {name}")
            self.data.setdefault("movie_catalog_initialized", False)
            self.default_movie_label = movie_label

    def language(self, chat_id):
        with self._lock:
            return "en" if self.data.get("languages", {}).get(str(chat_id)) == "en" else "ko"

    def set_language(self, chat_id, language):
        if language not in {"ko", "en"}:
            raise ValueError("Invalid language")
        with self._lock:
            self.data.setdefault("languages", {})[str(chat_id)] = language

    def selected_movies(self, chat_id):
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id), {})
            selected = record.get("movies")
            if selected is None:
                return (getattr(self, "default_movie_no", "30001323"),)
            if not isinstance(selected, list):
                return ()  # Corrupt preferences must not broaden subscriptions.
            return tuple(x for x in selected if isinstance(x, str) and re.fullmatch(r"[0-9]{1,20}", x))

    def wants_movie(self, chat_id, movie_no):
        return not movie_no or movie_no in self.selected_movies(chat_id)

    def anyone_wants_movie(self, movie_no):
        return any(self.wants_movie(c, movie_no) for c in self.subscriber_ids())

    def movie_revision(self, chat_id):
        with self._lock:
            value = self.data["subscribers"].get(str(chat_id), {}).get("movie_revision", 0)
            return value if type(value) is int and value >= 0 else 0

    def select_movie(self, chat_id, movie_no, enabled):
        with self._lock:
            record = self.data["subscribers"].get(str(chat_id))
            if not isinstance(record, dict) or movie_no not in self.movie_catalog():
                return False
            selected = set(self.selected_movies(chat_id))
            if (movie_no in selected) == enabled:
                return False
            selected.add(movie_no) if enabled else selected.discard(movie_no)
            record["movies"] = sorted(selected)
            record["movie_revision"] = self.movie_revision(chat_id) + 1
            if not enabled:
                pending = self.data["pending_deliveries"]
                for key, delivery in list(pending.items()):
                    if (delivery.get("chat_id") == str(chat_id)
                            and delivery.get("category") in {"open", "seats", "seats_sweet", "seats_unclassified", "movie_summary"}
                            and (delivery.get("movie_no") or self.default_movie_no) == movie_no):
                        del pending[key]
            return True

    def movie_catalog(self):
        with self._lock:
            catalog = copy.deepcopy(self.data.get("movie_catalog", {}))
            default = getattr(self, "default_movie_no", "30001323")
            catalog.setdefault(default, {"title": getattr(self, "default_movie_label", "오디세이"), "first_seen": "", "last_seen": ""})
            return catalog

    def movie_catalog_ready(self):
        with self._lock:
            return self.data.get("movie_catalog_initialized") is True

    def finish_movie_baseline(self):
        with self._lock:
            if not self.movie_catalog_ready():
                self.data["movie_catalog_initialized"] = True
                self.save()


class MovieWatcherMixin:
    def _observe_movies(self, show_date, sessions):
        """An empty *successful* day replaces only that day's cached sessions."""
        now = self.config.local_now().isoformat()
        with self.state._lock:
            catalog = self.state.data["movie_catalog"]
            cache = self.state.data["movie_sessions"]
            for day in list(cache):
                if day < self.config.local_today().isoformat():
                    del cache[day]
            cache[show_date.isoformat()] = {"checked_at": now, "sessions": [dataclasses.asdict(s) for s in sessions]}
            for session in sessions:
                number = session.movie_no
                if not number:
                    continue
                fresh = number not in catalog
                record = catalog.setdefault(number, {"first_seen": now})
                record.update(title=session.movie_label, last_seen=now)
                if fresh and self.state.movie_catalog_ready():
                    for chat_id in self.state.rotate_broadcast_recipients("movie", self.state.subscriber_ids()):
                        lang = self.state.language(chat_id)
                        text = choose(lang,
                            f"🎬 용산 IMAX 새 영화 발견\n\n{session.movie_label}\n처음 확인한 상영일: {session.date}\n\n이 영화의 알림을 받으려면 아래 버튼을 누르세요. 자동 구독되지 않습니다.\n영화 선택: /movies",
                            f"🎬 New movie at Yongsan IMAX\n\n{session.movie_label}\nFirst observed show date: {session.date}\n\nTap below to receive alerts for this movie. You are not subscribed automatically.\nChoose movies: /movies")
                        self.state.queue_pending_delivery(chat_id, text, "movie", movie_no=number,
                            reply_markup={"inline_keyboard": [[{"text": choose(lang, "🔔 이 영화 알림 받기", "🔔 Follow this movie"), "callback_data": f"mv:{number}:1"}]]})
            # Discovery dedup and notifications are committed together, even
            # when there are no movie-alert subscribers yet.
            self.state.save()
        self._delivery_wake_event.set()

    def _movie_menu(self, chat_id, page=0):
        lang = self.state.language(chat_id)
        catalog = self.state.movie_catalog()
        selected = set(self.state.selected_movies(chat_id))
        movies = sorted(catalog.items(), key=lambda item: (item[0] not in selected, item[1].get("title", ""), item[0]))
        page = max(0, min(page, max(0, (len(movies)-1)//8)))
        revision = self.state.movie_revision(chat_id)
        keyboard = [[{"text": ("✅ " if number in selected else "＋ ") + item.get("title", number)[:55],
                      "callback_data": f"mv:{number}:{int(number not in selected)}:{revision}"}]
                    for number, item in movies[page*8:page*8+8]]
        nav = []
        if page:
            nav.append({"text": "←", "callback_data": f"mp:{page-1}"})
        if (page+1)*8 < len(movies):
            nav.append({"text": "→", "callback_data": f"mp:{page+1}"})
        if nav:
            keyboard.append(nav)
        titles = ", ".join(catalog.get(n, {}).get("title", n) for n in sorted(selected)) or choose(lang, "없음", "None")
        text = choose(lang,
            f"🎬 알림 받을 영화 선택 ({page+1}/{max(1,(len(movies)+7)//8)})\n✅ 선택됨 · 버튼을 눌러 추가/해제 (복수 선택)\n\n현재: {titles}\n\n새 영화는 발견 알림만 보내며 자동 선택되지 않습니다. 신규 오픈·잔여좌석 알림은 선택한 영화만 받습니다.\n목록은 조회 범위에서 한 번 이상 확인한 영화이며, 현재 상영 여부는 선택 후 안내를 확인하세요.",
            f"🎬 Choose movies ({page+1}/{max(1,(len(movies)+7)//8)})\n✅ Selected · Tap to add/remove (multiple allowed)\n\nCurrent: {titles}\n\nNew discoveries are announced but not selected automatically. Opening/seat alerts only cover selected movies.\nThe catalog includes previously observed movies. Select one to see cached upcoming showings.")
        if not self.state.movie_catalog_ready():
            text += choose(lang, "\n⏳ 최초 영화 목록 수집 중입니다.", "\n⏳ Building the initial catalog.")
        return text[:3500], {"inline_keyboard": keyboard}

    def _handle_movie_command(self, chat_id, chat_type, command, argument):
        lang = self.state.language(chat_id)
        markup = None
        if chat_type != "private":
            text = choose(lang, "영화·언어 선택은 봇과의 개인 채팅에서 이용해주세요.", "Please choose movies/language in a private chat with the bot.")
        elif command == "/language":
            if argument in {"ko", "en"}:
                self.state.set_language(chat_id, argument)
                self.state.save()
                lang = argument
            text = choose(lang, "🌐 언어: 한국어\n한국어 또는 English를 선택하세요.", "🌐 Language: English\nChoose 한국어 or English.")
            markup = {"inline_keyboard": [[{"text": "한국어", "callback_data": "lang:ko"}, {"text": "English", "callback_data": "lang:en"}]]}
        elif not self.config.multi_movie_enabled:
            text = choose(lang, "영화 선택 기능이 아직 활성화되지 않았습니다.", "Movie selection is not enabled yet.")
        elif not self.state.is_subscribed(chat_id):
            text = choose(lang, "먼저 /start로 구독해주세요.", "Please subscribe with /start first.")
        else:
            text, markup = self._movie_menu(chat_id)
        try:
            self._send_or_queue_reply(text, chat_id=chat_id, reply_markup=markup)
        except self._telegram_error as exc:
            self.logger.warning("영화/언어 명령 응답 실패: %s", exc)

    def _handle_movie_callback(self, query):
        message = query.get("message") or {}
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        sender = query.get("from") or {}
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        chat_id = str(chat.get("id", ""))
        private_owner = (chat.get("type") == "private" and chat_id == str(sender.get("id")) and sender.get("is_bot") is False)
        try:
            self.telegram.answer_callback(str(query.get("id", "")), "" if private_owner else "Please use a private chat.")
        except self._telegram_error as exc:
            self.logger.warning("버튼 응답 실패: %s", exc)
        if not private_owner:
            return
        data = query.get("data", "")
        if not isinstance(data, str):
            return
        if data in {"lang:ko", "lang:en"}:
            self._handle_movie_command(chat_id, "private", "/language", data.split(":")[1])
            return
        if not self.config.multi_movie_enabled or not self.state.is_subscribed(chat_id):
            return
        match = re.fullmatch(r"mv:([0-9]{1,20}):([01])(?::([0-9]{1,12}))?", data)
        page_match = re.fullmatch(r"mp:([0-9]{1,5})", data)
        if not match and not page_match:
            return
        with self.state._lock:
            if match and (match[3] is None or int(match[3]) == self.state.movie_revision(chat_id)):
                changed = self.state.select_movie(chat_id, match[1], match[2] == "1")
                if changed and match[2] == "1":
                    self._queue_movie_summary(chat_id, match[1])
            text, markup = self._movie_menu(chat_id, int(page_match[1]) if page_match else 0)
            self.state.queue_pending_delivery(chat_id, text, "reply", reply_markup=markup)
            self.state.save()
        self._delivery_wake_event.set()

    def _queue_movie_summary(self, chat_id, movie_no):
        lang = self.state.language(chat_id)
        title = self.state.movie_catalog()[movie_no]["title"]
        rows = []
        now = self.config.local_now()
        for record in self.state.data["movie_sessions"].values():
            for raw in record.get("sessions", []):
                if raw.get("movie_no") != movie_no:
                    continue
                session = self._session_class(**raw)
                start = session.start_datetime(self.config.timezone_name)
                if start is not None and start > now + dt.timedelta(minutes=self.config.booking_close_margin_minutes):
                    rows.append((session, record["checked_at"]))
        header = choose(lang, f"✅ 영화 알림 선택: {title}\n\n최근 조회한 열린 회차 (실시간 예매 가능 보장 아님)\n", f"✅ Following: {title}\n\nLast observed upcoming showings (availability may change)\n")
        lines = []
        for session, checked in sorted(rows, key=lambda row: (row[0].date, row[0].start_time)):
            lines.append(f"{session.date} {session.start_time} — {session.remaining_seats if session.remaining_seats is not None else '?'}/{session.total_seats if session.total_seats is not None else '?'}\n{self._booking_url(session, self.config)}\n" + choose(lang, "조회: ", "Checked: ") + checked[:19])
        if not lines:
            lines = [choose(lang, "아직 조회된 예정 회차가 없습니다. 이후 새 회차를 발견하면 설정에 맞춰 알려드립니다.", "No upcoming showings are cached yet. Future openings will follow your alert settings.")]
        chunk = header
        for line in lines:
            if len(chunk) + len(line) > 3500:
                self.state.queue_pending_delivery(chat_id, chunk, "movie_summary", movie_no=movie_no)
                chunk = header
            chunk += "\n" + line + "\n"
        self.state.queue_pending_delivery(chat_id, chunk, "movie_summary", movie_no=movie_no)

    def _public_reply(self, command, chat_id, original):
        """Render built-in UI only. Operator-authored announcements are untouched."""
        lang = self.state.language(chat_id)
        if command in {"/status", "/start", "/subscribe"} and (self.config.multi_movie_enabled or lang == "en"):
            if not self.state.is_subscribed(chat_id):
                return choose(lang, "🔕 현재 구독 중이 아닙니다.\n/start — 구독\n/language — 언어 선택", "🔕 Not subscribed.\n/start — Subscribe\n/language — Language") + (choose(lang, "\n현재 신규 구독 중단 중입니다.", "\nNew subscriptions are currently closed.") if not self.config.new_subscriptions_enabled else "")
            catalog = self.state.movie_catalog()
            titles = ", ".join(catalog.get(n, {}).get("title", n) for n in self.state.selected_movies(chat_id)) or choose(lang, "없음", "None")
            mode = self.state.alert_mode(chat_id)
            days = self.state.show_day_selection(chat_id)
            dates = self.state.seat_date_range(chat_id)
            times = self.state.seat_time_range(chat_id)
            date_label = (dates[0] + " ~ " + (dates[1] or choose(lang, "이후", "onward"))) if dates else choose(lang, "제한 없음", "Any date")
            time_label = " ~ ".join(times) if times else choose(lang, "전체 시간", "Any time")
            mode_label = {"all": ("신규 오픈 + 잔여 좌석", "Openings + seats"), "open": ("신규 오픈만", "Openings only"), "seats": ("잔여 좌석만", "Seats only")}[mode]
            text = choose(lang,
                f"✅ 용산 IMAX 알림 구독 중\n\n🎬 선택한 영화: {titles}\n알림 종류: {mode_label[0]}\n상영 요일: {'토·일' if days == 'weekend' else '모든 요일'}\n잔여좌석 날짜: {date_label}\n잔여좌석 시간: {time_label} (한국시간)\n좌석 범위: {'명당' if self.state.seat_selection(chat_id) == 'sweet' else 'A열 제외'}\n최소 좌석: {self.state.min_seats(chat_id)}석\n\n/movies — 영화 선택\n/mode · /day · /date · /time · /seat · /count — 설정\n/language — 한국어 / English\n/desc — 사용 방법 · /stop — 해지\n\n새 영화는 발견 안내만 보내며 자동 구독되지 않습니다. 신규 오픈은 선택 영화·알림 종류·요일만 적용하고 좌석·날짜·시간 제한은 무시합니다.",
                f"✅ Subscribed to Yongsan IMAX alerts\n\n🎬 Movies: {titles}\nAlerts: {mode_label[1]}\nShow days: {'Sat/Sun' if days == 'weekend' else 'Every day'}\nSeat-alert dates: {date_label}\nSeat-alert times: {time_label} (Korea time)\nSeats: {'Sweet spots' if self.state.seat_selection(chat_id) == 'sweet' else 'All except row A'}\nMinimum seats: {self.state.min_seats(chat_id)}\n\n/movies — Choose movies\n/mode · /day · /date · /time · /seat · /count — Settings\n/language — 한국어 / English\n/desc — Guide · /stop — Unsubscribe\n\nNew movies are announced, not automatically selected. Opening alerts use movie, mode and weekday settings only; seat/date/time restrictions do not apply.")
            if self.config.open_only_mode:
                text += choose(lang, "\n⚠️ 현재 신규 오픈 전용 운영 중입니다. 좌석 조회는 중단되어 있습니다.", "\n⚠️ Openings-only operation: seat lookups are disabled.")
            paused = any((self.cgv_recovery_status_text(), self.seat_api_status_text(), self.telegram_send_status_text()))
            if paused:
                text += choose(lang, "\n⚠️ 조회 또는 발송에 대기/중단 상태가 있습니다. 운영자에게 문의해주세요.", "\n⚠️ Lookups or delivery are paused/delayed. Contact the operator.")
            return text
        if command in {"/help", "/desc", "/description"} and (
            self.config.multi_movie_enabled or self.config.open_only_mode or lang == "en"
        ):
            # Help is a command index; description is a separate, detailed guide.
            renderer = public_help if command == "/help" else public_guide
            return renderer(lang, self.config)
        if lang != "en":
            return original
        if command in {"/stop", "/unsubscribe"}:
            return "🔕 Unsubscribed. /start to subscribe again.\n/language — Language" + ("\nNew subscriptions are currently closed." if not self.config.new_subscriptions_enabled else "")
        if command in {"/developer", "/coffee", "/donate"}:
            return ("💬 Developer contact (not CGV support)\nhttps://www.instagram.com/silverflowerstar/" if command == "/developer" else "☕ Support the developer on Buy Me a Coffee\nhttps://buymeacoffee.com/yuemyname\nAll alerts are available regardless of donations.")
        if re.fullmatch(r"/(mode.*|alert|day.*|date.*|time.*|seat.*|count.*)", command):
            if not self.state.is_subscribed(chat_id):
                return "Please subscribe with /start first."
            error = any(x in original for x in ("알 수 없는", "형식을", "입력해", "올바른", "이전일", "빠를", "잘못", "유효", "존재하지", "종료일은", "시작일이"))
            prefix = "⚠️ Invalid setting. See the formats below.\n\n" if error else "Settings (saved values shown below):\n\n"
            if self.config.seat_alert_sweet_only and command.startswith("/seat"):
                prefix += "Sweet spots only (operator policy). /seat_all cannot enable all seats.\nF/G 16–29 · H/I 13–32 · J/K/L 11–34\n\n"
            return prefix + self._public_reply("/status", chat_id, "") + "\n\n" + EN_SETTINGS
        # Admin commands contain user-authored labels/prose; don't translate
        # those. Unknown commands have an English help hint.
        if original == "사용 가능한 명령어를 보려면 /help를 보내주세요.":
            return "Send /help for available commands."
        return original


EN_SETTINGS = (
    "/mode_all · /mode_open · /mode_seats — Alert type\n"
    "/day_all · /day_weekend — Every day / Sat & Sun\n"
    "/date YYYYMMDD — From that date (inclusive)\n"
    "/date YYYYMMDD YYYYMMDD — Inclusive range\n/date_all — Clear date filter\n"
    "/time 18:00 23:59 — Start-time range, Korea time\n"
    "/time 22:00 02:00 — Overnight range\n/time_all — Clear time filter\n"
    "/seat · /seat_sweet · /seat_all — Seat scope\n"
    "/count_1 · /count_2 — At least 1 / 2 available seats\n"
    "Date/time/minimum/seat filters only affect seat alerts. Weekdays affect openings too."
)


def _public_service_notes(lang, config):
    notes = []
    if config.open_only_mode:
        notes.append(choose(lang,
            "⚠️ 현재 신규 오픈 전용 운영 중입니다. 잔여좌석 조회·알림은 중단되어 있습니다. 기존 '잔여좌석만' 구독자의 신규 오픈 수신 설정: /mode_open",
            "⚠️ Openings-only operation: seat lookups/alerts are disabled. If you selected seats only, use /mode_open to receive opening alerts."))
    if not config.new_subscriptions_enabled:
        notes.append(choose(lang,
            "⏸ 신규 구독을 잠시 중단했습니다. 기존 구독자는 계속 이용할 수 있지만, 해지 후에는 재개될 때까지 재구독할 수 없습니다.",
            "⏸ New subscriptions are closed. Existing subscribers can continue, but cannot rejoin after unsubscribing until signups reopen."))
    return "\n\n".join(notes)


def public_help(lang, config):
    """A compact command index, not the detailed /desc walkthrough."""
    commands = [
        ("start", "알림 구독", "Subscribe"), ("stop", "알림 해지", "Unsubscribe"),
        ("status", "현재 구독 및 설정", "Current settings"), ("movies", "알림 받을 영화 선택 (복수 선택)", "Choose movies (multiple)"),
        ("language", "한국어 / English 선택", "Choose 한국어 / English"),
        ("mode", "알림 종류 선택", "Choose alert type"), ("mode_all", "신규 오픈 + 잔여좌석", "Openings + seats"),
        ("mode_open", "신규 오픈만", "Openings only"), ("mode_seats", "잔여좌석만", "Seats only"),
        ("day", "상영 요일 선택", "Show weekdays"), ("day_all", "모든 요일", "Every day"), ("day_weekend", "토·일 상영분", "Sat/Sun showings"),
        ("date", "잔여좌석 날짜 설정", "Seat-alert date range"), ("date_all", "날짜 제한 해제", "Clear date filter"),
        ("time", "잔여좌석 시간 설정", "Seat-alert time range"), ("time_all", "시간 제한 해제", "Clear time filter"),
        ("seat", "좌석 구역 확인", "Seat scope"),
        ("seat_all", "명당 전용 운영 안내" if config.seat_alert_sweet_only else "A열 제외 전체", "Sweet-only policy" if config.seat_alert_sweet_only else "All except row A"),
        ("seat_sweet", "명당 좌석", "Sweet spots"),
        ("count", "최소 좌석 수", "Minimum seats"), ("count_1", "1석 이상", "At least 1"), ("count_2", "2석 이상", "At least 2"),
        ("desc", "봇 설명과 사용법", "About and usage"), ("developer", "개발자 인스타그램 문의", "Contact developer"),
        ("coffee", "개발자 커피 후원", "Support developer"), ("help", "전체 명령어", "All commands")]
    if not config.multi_movie_enabled:
        commands = [entry for entry in commands if entry[0] != "movies"]
    if config.open_only_mode:
        inactive = {"mode_all", "mode_seats", "date", "date_all", "time", "time_all",
                    "seat", "seat_all", "seat_sweet", "count", "count_1", "count_2"}
        commands = [entry for entry in commands if entry[0] not in inactive]
    parts = [choose(lang, "📋 용아맥 봇 전체 명령어", "📋 Yongsan IMAX bot commands"),
             "\n".join(f"/{c} — {choose(lang, k, e)}" for c, k, e in commands)]
    if config.seat_alert_sweet_only and not config.open_only_mode:
        parts.append(choose(lang,
            "잔여좌석은 명당만 알리며, 신규 오픈에는 좌석 제한이 없습니다.",
            "Seat alerts are sweet spots only; opening alerts have no seat restriction."))
    if notes := _public_service_notes(lang, config):
        parts.append(notes)
    parts.append(choose(lang,
        "봇 소개·알림 기준·설정 예시·예매 방법 자세히 보기: /desc",
        "Use /desc for a detailed explanation of alerts, filters and booking."))
    return "\n\n".join(parts)


def public_guide(lang, config):
    """Detailed /desc content. Keep commands separate from Korean particles."""
    today = config.local_today()
    date_start = today.strftime("%Y%m%d")
    date_end = (today + dt.timedelta(days=2)).strftime("%Y%m%d")
    parts = [choose(lang,
        f"🎬 용아맥 봇 소개와 사용 방법\n\nCGV {config.site_name} IMAX의 예매 오픈과 예매 가능한 좌석을 알려주는 비공식 봇입니다. 자동 예매·결제는 하지 않습니다.",
        f"🎬 About the Yongsan IMAX bot\n\nAn unofficial bot for booking openings and available seats at CGV {config.site_name} IMAX. It does not reserve seats or pay for tickets.")]
    if notes := _public_service_notes(lang, config):
        parts.append(notes)

    cadence = choose(lang,
        f"신규 일정 확인: 약 {config.poll_interval_seconds}초 간격.",
        f"Schedule checks: about every {config.poll_interval_seconds}s.")
    if config.seat_poll_interval_seconds and not config.open_only_mode:
        cadence += choose(lang,
            f" 잔여좌석 확인: 회차별 최소 {config.seat_poll_interval_seconds}초 간격. 신규 일정이 우선이며 남은 좌석 작업은 다음 주기로 이월됩니다.",
            f" Seat checks: at least {config.seat_poll_interval_seconds}s apart per showing. Schedules take priority; unfinished seat work carries forward.")
    parts.append(choose(lang,
        f"🔎 조회 범위와 주기\n{config.scan_range_label()} (한국시간 기준).\n{cadence}\n조회 범위 밖의 회차는 감지하지 못할 수 있습니다.",
        f"🔎 Coverage and timing\n{config.scan_range_label('en')} (Korea time).\n{cadence}\nShowings outside this range may not be detected."))

    movies = choose(lang,
        "2. /movies — 봇과의 개인 채팅에서 원하는 영화를 눌러 선택합니다. ✅ 표시가 선택된 영화이며 여러 편을 고를 수 있습니다. 다시 누르면 해제됩니다.\n새 영화 발견 안내는 구독자에게 한 번 보내지만, 그 영화가 자동 선택되지는 않습니다. 영화 선택 시 최근 조회한 예정 회차를 안내하며 실시간 좌석을 보장하지 않습니다.",
        "2. In a private chat, use /movies and tap films to select them. ✅ means selected; tap again to remove. Multiple films are allowed.\nNew-film announcements do not automatically select the film. Selecting one shows cached upcoming showings, not guaranteed live availability.") if config.multi_movie_enabled else choose(lang,
        f"2. 현재 알림 영화: {config.movie_label}", f"2. Current film: {config.movie_label}")
    parts.append(choose(lang,
        "📌 시작하기\n1. /start — 알림 구독\n", "📌 Getting started\n1. /start — Subscribe\n") + movies + choose(lang,
        "\n3. /status — 선택 영화와 설정 확인\n/language — 한국어·English 선택",
        "\n3. Check /status for your settings. Use /language to choose Korean or English."))

    parts.append(choose(lang,
        "🔔 신규 오픈 알림\n처음 발견한 상영 회차를 한 번 알립니다. 매진·좌석 수 미확인 회차도 안내하며, 선택 영화·알림 종류·상영 요일만 적용합니다. 좌석 번호는 조회하지 않습니다.\n/mode — 알림 종류 확인\n/mode_open — 신규 오픈 받기",
        "🔔 Opening alerts\nEach newly seen showing is announced once, including sold-out or unknown-count showings. Movie, mode and show-day settings apply. Check /mode; use /mode_open to receive openings. Seat numbers are not queried.") if config.open_only_mode else choose(lang,
        "🔔 두 가지 알림\n• 신규 오픈: 처음 발견한 상영 회차를 한 번 알립니다. 매진이거나 좌석 수를 몰라도 신규 회차이면 안내합니다. 선택 영화·알림 종류·상영 요일만 적용하며 날짜·시간·좌석·최소 좌석 제한은 적용하지 않습니다.\n• 잔여좌석: 조건에 맞는 예매 가능 좌석(취소표 포함)을 알립니다. 좌석이 계속 남아 있으면 수가 같아도 다시 알릴 수 있습니다.\n/mode — 종류 확인·선택: /mode_all 둘 다 · /mode_open 신규만 · /mode_seats 좌석만",
        "🔔 Two alert types\n• Openings: each newly seen showing is announced once, even if sold out or its seat count is unknown. Movie, mode and show-day settings apply; date/time/seat/minimum-count filters do not.\n• Seats: available seats, including cancellations, matching your filters. Alerts can repeat while seats remain, even if the count is unchanged.\n/mode: /mode_all both · /mode_open openings · /mode_seats seats"))

    parts.append(choose(lang,
        "📅 상영 요일\n/day_all 모든 요일 · /day_weekend 토·일 상영분. 영화 상영일 기준이며 알림 도착 요일과는 무관합니다.\n/day — 현재 요일 설정 확인\n잔여좌석 날짜·시간·좌석·최소 좌석 설정은 현재 적용되지 않습니다.",
        "📅 Show weekdays\n/day_all every day · /day_weekend Sat/Sun showings, based on the show day, not the delivery day. Check /day. Seat date/time/scope/minimum-count settings do not apply in openings-only operation.") if config.open_only_mode else choose(lang,
        "📅 요일·날짜·시간 설정\n/day_all 모든 요일 · /day_weekend 토·일 상영분. 알림이 도착하는 요일이 아니라 영화 상영일 기준이며 신규·좌석 알림에 모두 적용합니다.\n/day — 현재 요일 설정 확인\n"
        f"/date {date_start} — 입력한 날짜 포함 이후\n/date {date_start} {date_end} — 양 끝 날짜 포함\n"
        "날짜는 하이픈 없이 YYYYMMDD로 입력합니다. 기간이 지나도 자동 해제되지 않습니다. 날짜 제한 해제: /date_all\n"
        "/time 18:00 23:59 — 저녁 상영\n/time 22:00 02:00 — 자정을 넘는 심야 상영\n"
        "한국시간 상영 시작시각 기준, 양 끝 포함. 시간 제한 해제: /time_all\n날짜·시간은 잔여좌석에만 적용하며 조회 범위를 늘리지는 않습니다.",
        "📅 Day, date and time filters\n/day_all every day · /day_weekend Sat/Sun showings. This is the film's show day, not the day you receive the alert; it applies to both alert types. Check with /day.\n"
        f"/date {date_start} — from this date, inclusive\n/date {date_start} {date_end} — inclusive range\n"
        "Enter YYYYMMDD without hyphens. Expired filters stay active; clear with /date_all.\n"
        "/time 18:00 23:59 — evening\n/time 22:00 02:00 — overnight\n"
        "Korea-time show starts, both endpoints included. Clear with /time_all. Date/time filters affect seats only and do not extend the scan range."))

    policy = choose(lang,
        "현재 모든 구독자에게 명당만 알립니다. 위치 미확인 좌석은 제외합니다.\n/seat_all — 현재 명당 전용이므로 전체 좌석 전환은 불가합니다.",
        "All subscribers receive sweet-spot seats only. Unverified locations are excluded; /seat_all cannot override this.") if config.seat_alert_sweet_only else choose(lang,
        "/seat_all — A열 제외 전체 · /seat_sweet — 아래 명당만 선택",
        "/seat_all selects all except row A; /seat_sweet selects the zones below.")
    if not config.open_only_mode:
        parts.append(choose(lang, "💺 명당과 최소 좌석\n", "💺 Sweet spots and minimum seats\n") + policy + "\n"
            "SweetSpot: J11~34, K11~34, L11~34\nExperienced: H13~32, I13~32\nExtremer: F16~29, G16~29\n" + choose(lang,
            "/seat · /seat_sweet — 구역 확인\n/count_1 — 대상 좌석 1석 이상\n/count_2 — 대상 좌석 2석 이상. 2석이 서로 붙어 있다는 뜻은 아닙니다.\n/count — 현재 최소 좌석 설정 확인\nA열만 남은 경우·0석·이미 시작한 회차는 좌석 알림에서 제외합니다. 명당 설정은 명당이 없으면 제외합니다.",
            "Check with /seat or /seat_sweet. /count_1 needs at least 1 qualifying seat; /count_2 needs 2, not necessarily adjacent. Check with /count.\nNo seat alerts for row-A-only availability, zero seats or started showings; sweet mode also requires a sweet-spot seat."))

        parts.append(choose(lang,
            "✉️ 좌석 알림 읽기\n상영 시간 → 좌석 번호 → 잔여 좌석 → 변동 좌석수 순서입니다.\n'잔여 좌석'은 전체 잔여/총좌석, '변동 좌석수'는 알림 대상 구역의 이전→현재 수입니다. 이전 수를 모르거나 같으면 현재 수만 표시합니다.",
            "✉️ Reading seat alerts\nShowtime → seat numbers → remaining seats → available-seat count.\nRemaining seats means the auditorium total available/total capacity; available-seat count compares qualifying seats, previous → current. If unchanged or previously unknown, only the current count is shown."))
    parts.append(choose(lang,
        "🎟️ 예매와 주의사항\n링크에는 영화·극장·날짜가 선택되어 있습니다. CGV에서 IMAX·상영시간·좌석을 확인한 뒤 직접 예매·결제해주세요. 알림 후 좌석이 매진될 수 있으며, 통신 지연·CGV 차단·발송 대기로 알림이 늦거나 누락될 수 있습니다.\n\n/stop — 알림 해지 · /help — 전체 명령어\n/developer — 개발자 인스타그램 문의\n/coffee — 개발자 커피 후원 (후원 여부와 기능은 무관)",
        "🎟️ Booking and limitations\nLinks preselect movie, cinema and date. Confirm IMAX, showtime and seats in CGV, then book/pay yourself. Seats may sell out; network delays, CGV blocks or delivery queues can delay or prevent alerts.\n\n/stop — Unsubscribe · /help — All commands\n/developer — Contact developer on Instagram\n/coffee — Support developer; donations do not change features."))
    return "\n\n".join(parts)


def localize_alert(text, language):
    """Translate generated label lines, never arbitrary movie titles or prose."""
    if language != "en":
        return text
    prefixes = {
        "영화:": "Movie:", "극장:": "Cinema:", "상영 시간:": "Showtime:",
        "좌석 번호:": "Seat numbers:", "잔여 좌석:": "Remaining seats:", "변동 좌석수:": "Available-seat count:",
        "🎟️ 예매 바로가기 (영화·극장·날짜 선택됨):": "🎟️ Book (movie/cinema/date selected):",
    }
    result = []
    for line in text.splitlines():
        if line == "🎟️ CGV 예매 오픈 감지": line = "🎟️ CGV booking opened"
        elif line == "💺 CGV 예매 가능 좌석": line = "💺 CGV available seats"
        elif line.startswith("📅 상영일:"):
            line = line.replace("상영일:", "Show date:", 1)
            for ko,en in zip("월화수목금토일", ("Mon","Tue","Wed","Thu","Fri","Sat","Sun")):
                line = line.replace(f"({ko})", f"({en})")
        else:
            for ko,en in prefixes.items():
                if line.startswith(ko):
                    line = en + line[len(ko):]
                    if ko in {"잔여 좌석:", "변동 좌석수:"}:
                        line = line.replace("이전 ", "previous ").replace("석", " seats")
                    break
            if line.startswith("상영 시작시간") or line.startswith("• 상영"):
                line = line.replace("상영 시작시간", "Showtime").replace("상영 시간", "Showtime").replace("석", " seats")
                line = line.replace("매진 · 취소표 나오면 알림", "sold out · cancellation alerts enabled").replace("매진", "sold out")
                line = line.replace("A열 여부 미확인 · 전체 잔여 수 기준", "row A not verified · total remaining seats")
            if line.startswith("⚠️ A열 여부"):
                line = "⚠️ Row A not verified · based on total remaining seats"
        result.append(line)
    return "\n".join(result)
