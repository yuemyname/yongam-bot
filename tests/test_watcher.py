import datetime as dt
import dataclasses
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse

from watcher import (
    ALERT_MODE_ALL,
    ALERT_MODE_OPEN_ONLY,
    ALERT_MODE_SEATS_ONLY,
    ALERT_OPEN,
    ALERT_SEATS,
    ALERT_SYSTEM,
    STATE_VERSION,
    BookingSession,
    CgvClient,
    Config,
    FetchError,
    SeatSnapshot,
    StateStore,
    TimezoneFormatter,
    Watcher,
    booking_url_for_session,
    extract_seat_snapshot,
    extract_sessions,
    rate_limit_backoff_seconds,
    run_command_loop,
)


def make_config(project_dir: Path) -> Config:
    env_path = project_dir / ".env"
    env_path.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=123456:test-token",
                "TELEGRAM_CHAT_ID=987654",
                "DYNAMIC_DATE_WINDOW=false",
                "TARGET_START_DATE=2026-08-26",
                "TARGET_END_DATE=2026-08-26",
                "CGV_REQUEST_SPACING_SECONDS=0",
                "STRICT_IMAX_MATCH=true",
                "SUBSCRIPTIONS_ENABLED=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return Config.from_env_file(env_path)


class ExtractSessionsTests(unittest.TestCase):
    def test_extracts_nested_imax_sessions_and_deduplicates(self):
        payload = {
            "data": {
                "movie": {"movNo": "30001323"},
                "screens": [
                    {
                        "spclScnsNm": "IMAX LASER 2D",
                        "scnsNm": "IMAX관",
                        "schedules": [
                            {
                                "scnYmd": "20260826",
                                "scnFrTm": "1430",
                                "scnToTm": "1710",
                                "schNo": "abc-1",
                            },
                            {
                                "scnYmd": "20260826",
                                "scnFrTm": "14:30",
                                "schNo": "abc-1",
                            },
                        ],
                    }
                ],
            }
        }
        sessions = extract_sessions(
            payload, requested_date=dt.date(2026, 8, 26), strict_imax_match=True
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].date, "2026-08-26")
        self.assertEqual(sessions[0].start_time, "14:30")
        self.assertEqual(sessions[0].screen_name, "IMAX관")

    def test_filters_non_imax_session_in_strict_mode(self):
        payload = {
            "data": [
                {"screenName": "일반 2관", "startTime": "09:10"},
                {"screenName": "용산 IMAX", "startTime": "12:20"},
            ]
        }
        sessions = extract_sessions(
            payload, requested_date=dt.date(2026, 8, 27), strict_imax_match=True
        )
        self.assertEqual([item.start_time for item in sessions], ["12:20"])

    def test_accepts_configured_imax_code(self):
        payload = {
            "groups": [
                {
                    "specialScreenCode": "08",
                    "items": [{"playStartTime": "2355", "screenName": "20관"}],
                }
            ]
        }
        sessions = extract_sessions(
            payload,
            requested_date=dt.date(2026, 9, 8),
            code_values=("08",),
            strict_imax_match=True,
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].start_time, "23:55")

    def test_non_strict_mode_handles_omitted_format_label(self):
        payload = {"result": [{"scnFrTm": "0830", "scnYmd": "20260901"}]}
        sessions = extract_sessions(
            payload,
            requested_date=dt.date(2026, 9, 1),
            strict_imax_match=False,
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].date, "2026-09-01")

    def test_extracts_cgv_schedule_ids_and_remaining_seats(self):
        payload = {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": "20260826",
                    "scnsrtTm": "1430",
                    "scnendTm": "1710",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": "182",
                    "stcnt": "624",
                }
            ]
        }
        sessions = extract_sessions(
            payload, requested_date=dt.date(2026, 8, 26), strict_imax_match=True
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].screen_no, "13")
        self.assertEqual(sessions[0].screen_sequence, "4")
        self.assertEqual(sessions[0].remaining_seats, 182)
        self.assertEqual(sessions[0].total_seats, 624)


class SeatSnapshotTests(unittest.TestCase):
    def test_alertable_requires_matching_counts_and_complete_row_labels(self):
        complete = SeatSnapshot(
            total=2,
            general=2,
            accessible=0,
            mapped_total=2,
            available_rows=("B",),
        )
        missing_rows = dataclasses.replace(complete, available_rows=None)
        count_mismatch = dataclasses.replace(complete, mapped_total=1)
        unclassified_six = SeatSnapshot(total=6)
        unclassified_seven = SeatSnapshot(total=7)
        sold_out = SeatSnapshot(total=0)

        self.assertTrue(complete.seat_map_complete)
        self.assertTrue(complete.alertable)
        self.assertFalse(missing_rows.alertable)
        self.assertFalse(count_mismatch.alertable)
        self.assertFalse(unclassified_six.alertable)
        self.assertTrue(unclassified_seven.alertable)
        self.assertTrue(unclassified_seven.uses_unclassified_fallback)
        self.assertFalse(sold_out.alertable)

    def test_counts_available_general_and_accessible_seats(self):
        payload = {
            "data": {
                "items": [
                    {
                        "seats": [
                            {
                                "seatLocNo": "1",
                                "seatStusCd": "00",
                                "seatSaleYn": "Y",
                                "seatSalfrmCd": "01",
                            },
                            {
                                "seatLocNo": "2",
                                "seatStusCd": "00",
                                "seatSaleYn": "Y",
                                "seatSalfrmCd": "04",
                            },
                            {
                                "seatLocNo": "3",
                                "seatStusCd": "02",
                                "seatSaleYn": "Y",
                                "seatSalfrmCd": "01",
                            },
                        ]
                    }
                ]
            }
        }
        snapshot = extract_seat_snapshot(payload, scheduled_remaining=2)
        self.assertEqual(snapshot.general, 1)
        self.assertEqual(snapshot.accessible, 1)
        self.assertFalse(snapshot.accessible_only)

    def test_accessible_only_requires_map_total_to_match_schedule(self):
        seats = [
            {
                "seatLocNo": "W1",
                "seatStusCd": "00",
                "seatSaleYn": "Y",
                "seatSalfrmCd": "04",
            }
        ]
        matching = extract_seat_snapshot(
            {"data": {"items": [{"seats": seats}]}}, scheduled_remaining=1
        )
        partial = extract_seat_snapshot(
            {"data": {"items": [{"seats": seats}]}}, scheduled_remaining=2
        )
        self.assertTrue(matching.accessible_only)
        self.assertFalse(partial.accessible_only)

    def test_row_a_only_requires_complete_matching_map(self):
        def seat(number, row):
            return {
                "seatLocNo": number,
                "seatRowNm": row,
                "seatStusCd": "00",
                "seatSaleYn": "Y",
                "seatSalfrmCd": "01",
            }

        only_a = extract_seat_snapshot(
            {"data": {"items": [{"seats": [seat("1", "A"), seat("2", "A열")]}]}},
            scheduled_remaining=2,
        )
        mixed_rows = extract_seat_snapshot(
            {"data": {"items": [{"seats": [seat("1", "A"), seat("2", "B")]}]}},
            scheduled_remaining=2,
        )
        missing_row = extract_seat_snapshot(
            {"data": {"items": [{"seats": [seat("1", "A"), seat("2", "")]}]}},
            scheduled_remaining=2,
        )
        count_mismatch = extract_seat_snapshot(
            {"data": {"items": [{"seats": [seat("1", "A"), seat("2", "A")]}]}},
            scheduled_remaining=3,
        )

        self.assertTrue(only_a.row_a_only)
        self.assertTrue(only_a.should_suppress)
        self.assertFalse(mixed_rows.row_a_only)
        self.assertFalse(missing_row.row_a_only)
        self.assertFalse(count_mismatch.row_a_only)


class StateStoreTests(unittest.TestCase):
    def test_persists_notification_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "data" / "notified.json"
            store = StateStore(state_path)
            session = BookingSession(date="2026-08-26", start_time="14:30")
            key = session.notification_key(site_no="0013", movie_no="30001323")
            store.mark_notified(key, session)
            store.save()

            reloaded = StateStore(state_path)
            reloaded.load()
            self.assertTrue(reloaded.was_notified(key))
            parsed = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["notified"][key]["start_time"], "14:30")

    def test_migrates_v1_state_and_persists_seat_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "notified.json"
            state_path.write_text(
                json.dumps({"version": 1, "notified": {}}), encoding="utf-8"
            )
            store = StateStore(state_path)
            store.load()
            store.set_seat_snapshot(
                "session",
                SeatSnapshot(
                    total=5,
                    general=3,
                    accessible=2,
                    mapped_total=5,
                    available_rows=("A", "B"),
                ),
            )
            store.save()

            reloaded = StateStore(state_path)
            reloaded.load()
            self.assertEqual(reloaded.data["version"], STATE_VERSION)
            self.assertEqual(reloaded.seat_snapshot("session").general, 3)
            self.assertEqual(
                reloaded.seat_snapshot("session").available_rows, ("A", "B")
            )


class StatePruningTests(unittest.TestCase):
    @staticmethod
    def _key(date_text: str, start_time: str = "14:30") -> str:
        return f"0013:30001323:{date_text}:{start_time}"

    def test_removes_past_shows_but_keeps_today_and_yesterday(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            today = dt.date(2026, 8, 13)
            for offset in (-5, -2, -1, 0, 3):
                key = self._key((today + dt.timedelta(days=offset)).isoformat())
                store.data["notified"][key] = {"date": None}
                store.set_seat_snapshot(key, SeatSnapshot(total=4))

            removed = store.prune_expired(today)

            # -5 and -2 fall before the one-day retention margin; both buckets.
            self.assertEqual(removed, 4)
            surviving = {
                key.split(":")[2] for key in store.data["notified"]
            }
            self.assertEqual(
                surviving,
                {"2026-08-12", "2026-08-13", "2026-08-16"},
            )
            self.assertEqual(len(store.data["seat_counts"]), 3)

    def test_keeps_records_whose_date_cannot_be_parsed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.data["notified"]["legacy-key-without-date"] = {}
            store.data["notified"]["0013:30001323:not-a-date:14:30"] = {}

            self.assertEqual(store.prune_expired(dt.date(2026, 8, 13)), 0)
            self.assertEqual(len(store.data["notified"]), 2)

    def test_prefers_the_stored_date_field_over_the_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            # Key says a future date, the record says the show already played.
            store.data["notified"][self._key("2026-12-01")] = {"date": "2026-01-01"}

            self.assertEqual(store.prune_expired(dt.date(2026, 8, 13)), 1)
            self.assertEqual(store.data["notified"], {})


class AlertModeStateTests(unittest.TestCase):
    def test_defaults_to_all_and_persists_across_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "notified.json"
            store = StateStore(path)
            store.add_subscriber("111", label="구독자", chat_type="private")

            self.assertEqual(store.alert_mode("111"), ALERT_MODE_ALL)
            self.assertTrue(store.set_alert_mode("111", ALERT_MODE_SEATS_ONLY))
            self.assertFalse(store.set_alert_mode("111", ALERT_MODE_SEATS_ONLY))
            store.save()

            reloaded = StateStore(path)
            reloaded.load()
            self.assertEqual(reloaded.alert_mode("111"), ALERT_MODE_SEATS_ONLY)

    def test_subscriber_stored_before_the_feature_falls_back_to_all(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.data["subscribers"]["999"] = {"subscribed_at": "2026-01-01T00:00:00"}

            self.assertEqual(store.alert_mode("999"), ALERT_MODE_ALL)
            self.assertIn("999", store.subscriber_ids_for(ALERT_OPEN))
            self.assertIn("999", store.subscriber_ids_for(ALERT_SEATS))

    def test_routes_each_category_to_the_opted_in_subscribers(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            for chat_id, mode in (
                ("1", ALERT_MODE_ALL),
                ("2", ALERT_MODE_OPEN_ONLY),
                ("3", ALERT_MODE_SEATS_ONLY),
            ):
                store.add_subscriber(chat_id)
                store.set_alert_mode(chat_id, mode)

            self.assertEqual(store.subscriber_ids_for(ALERT_OPEN), ("1", "2"))
            self.assertEqual(store.subscriber_ids_for(ALERT_SEATS), ("1", "3"))
            self.assertEqual(store.subscriber_ids_for(ALERT_SYSTEM), ("1", "2", "3"))

    def test_rejects_an_unknown_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.add_subscriber("1")
            with self.assertRaises(ValueError):
                store.set_alert_mode("1", "nope")


class ConfigTests(unittest.TestCase):
    def test_dynamic_window_is_today_plus_27_days(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary)
            env_path = project_dir / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=123456:test-token",
                        "TELEGRAM_CHAT_ID=987654",
                        "DYNAMIC_DATE_WINDOW=true",
                        "TARGET_WINDOW_DAYS=28",
                        "APP_TIMEZONE=Asia/Seoul",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = Config.from_env_file(env_path)
            dates = config.target_dates(today=dt.date(2026, 8, 12))

            self.assertEqual(len(dates), 28)
            self.assertEqual(dates[0], dt.date(2026, 8, 12))
            self.assertEqual(dates[-1], dt.date(2026, 9, 8))

    def test_booking_url_preselects_movie_theater_and_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            session = BookingSession(date="2026-08-26", start_time="14:30")
            url = booking_url_for_session(session, config)
            query = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(url).query, keep_blank_values=True
                )
            )

            self.assertEqual(query["coCd"], "A420")
            self.assertEqual(query["siteNo"], "0013")
            self.assertEqual(query["siteNm"], "용산아이파크몰")
            self.assertEqual(query["movNo"], "30001323")
            self.assertEqual(query["scnYmd"], "20260826")

    def test_railway_environment_works_without_dotenv_and_uses_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary)
            volume_dir = project_dir / "railway-data"
            railway_values = {
                "TELEGRAM_BOT_TOKEN": "123456:railway-token",
                "TELEGRAM_CHAT_ID": "987654",
                "RAILWAY_VOLUME_MOUNT_PATH": str(volume_dir),
            }
            with patch.dict(os.environ, railway_values, clear=False):
                config = Config.from_env_file(project_dir / "missing.env")

            self.assertEqual(config.state_file, volume_dir.resolve() / "notified.json")
            self.assertEqual(config.log_file, volume_dir.resolve() / "watcher.log")

    def test_rate_limit_backoff_grows_and_caps_at_two_hours(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))

            self.assertEqual(rate_limit_backoff_seconds(config, 0), 60)
            self.assertEqual(rate_limit_backoff_seconds(config, 1), 1800)
            self.assertEqual(rate_limit_backoff_seconds(config, 2), 3600)
            self.assertEqual(rate_limit_backoff_seconds(config, 3), 7200)
            self.assertEqual(rate_limit_backoff_seconds(config, 4), 7200)


class LoggingTests(unittest.TestCase):
    def test_formats_railway_utc_timestamp_as_korean_time(self):
        formatter = TimezoneFormatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S %Z",
            timezone_name="Asia/Seoul",
        )
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="조회 완료",
            args=(),
            exc_info=None,
        )
        record.created = dt.datetime(
            2026, 8, 12, 15, 54, 36, tzinfo=dt.timezone.utc
        ).timestamp()

        self.assertEqual(
            formatter.format(record),
            "2026-08-13 00:54:36 KST INFO 조회 완료",
        )


class WatcherIntegrationTests(unittest.TestCase):
    def test_reports_schedule_rate_limit_for_adaptive_backoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), target_end=dt.date(2026, 8, 28)
            )
            logger = logging.getLogger(f"watcher-rate-limit-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger, dry_run=True)

            attempted_dates = []

            def fail_with_rate_limit(show_date):
                attempted_dates.append(show_date)
                raise FetchError("CGV 응답 오류: HTTP 429")

            watcher.cgv.fetch_date = fail_with_rate_limit
            result = watcher.run_cycle()

            self.assertEqual(result.failed_dates, 1)
            self.assertEqual(result.rate_limited_requests, 1)
            self.assertEqual(result.schedule_skipped_dates, 2)
            self.assertEqual(attempted_dates, [dt.date(2026, 8, 26)])

    def test_stops_remaining_seat_requests_after_first_rate_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-seat-rate-limit-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger, dry_run=True)
            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": start_time,
                        "scnsNo": "13",
                        "scnSseq": sequence,
                        "frSeatCnt": 2,
                        "stcnt": 200,
                    }
                    for start_time, sequence in (
                        ("1000", "1"),
                        ("1400", "2"),
                        ("1800", "3"),
                    )
                ]
            }
            attempted_sessions = []

            def fail_first_seat_request(session):
                attempted_sessions.append(session.start_time)
                raise FetchError("CGV 응답 오류: HTTP 429")

            watcher.cgv.fetch_seat_snapshot = fail_first_seat_request
            result = watcher.run_cycle()

            self.assertEqual(attempted_sessions, ["10:00"])
            self.assertEqual(result.rate_limited_requests, 1)
            self.assertEqual(result.seat_detail_errors, 1)
            self.assertEqual(result.seat_detail_skipped, 2)

    def test_start_and_stop_commands_persist_subscribers(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-subscriptions-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            replies = []
            batches = [
                [
                    {
                        "update_id": 10,
                        "message": {
                            "text": "/start@YongsanBot",
                            "chat": {
                                "id": 111222,
                                "type": "private",
                                "first_name": "구독자",
                            },
                        },
                    }
                ],
                [
                    {
                        "update_id": 11,
                        "message": {
                            "text": "/stop",
                            "chat": {"id": 111222, "type": "private"},
                        },
                    }
                ],
            ]
            watcher.telegram.get_updates = lambda **_kwargs: batches.pop(0)
            watcher.telegram.send_message = (
                lambda text, **kwargs: replies.append((kwargs.get("chat_id"), text))
            )

            watcher.sync_subscribers()
            self.assertTrue(watcher.state.is_subscribed("111222"))
            self.assertIn("구독이 완료", replies[-1][1])
            self.assertEqual(watcher.state.telegram_update_offset, 11)

            watcher.sync_subscribers()
            self.assertFalse(watcher.state.is_subscribed("111222"))
            self.assertIn("구독을 해지", replies[-1][1])
            self.assertEqual(watcher.state.telegram_update_offset, 12)

            reloaded = StateStore(config.state_file)
            reloaded.load()
            self.assertFalse(reloaded.is_subscribed("111222"))
            self.assertTrue(reloaded.is_subscribed(config.telegram_chat_id))

    @staticmethod
    def _schedule_payload(remaining: int) -> dict:
        return {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": "20260826",
                    "scnsrtTm": "1430",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": remaining,
                    "stcnt": 200,
                }
            ]
        }

    def _watcher_with_three_alert_modes(self, temporary, name):
        config = make_config(Path(temporary))
        logger = logging.getLogger(f"watcher-{name}-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.state.remove_subscriber(config.telegram_chat_id)
        for chat_id, mode in (
            ("1", ALERT_MODE_OPEN_ONLY),
            ("2", ALERT_MODE_SEATS_ONLY),
            ("3", ALERT_MODE_ALL),
        ):
            watcher.state.add_subscriber(chat_id)
            watcher.state.set_alert_mode(chat_id, mode)
        return watcher

    def test_alert_mode_routes_open_and_seat_alerts_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher_with_three_alert_modes(temporary, "alert-routing")
            remaining = {"count": 10}
            watcher.cgv.fetch_date = lambda _date: self._schedule_payload(
                remaining["count"]
            )
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                general=session.remaining_seats - 2,
                accessible=2,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            sent = []
            watcher.telegram.send_message = lambda text, **kwargs: sent.append(
                (kwargs.get("chat_id"), text)
            )

            first = watcher.run_cycle()
            self.assertEqual(first.new_sessions, 1)
            self.assertEqual({chat_id for chat_id, _ in sent}, {"1", "3"})
            self.assertIn("예매 오픈 감지", sent[0][1])

            sent.clear()
            remaining["count"] = 9
            second = watcher.run_cycle()
            self.assertEqual(second.seat_changes, 1)
            self.assertEqual({chat_id for chat_id, _ in sent}, {"2", "3"})
            self.assertIn("잔여 좌석 변경", sent[0][1])

    def test_open_alert_is_marked_notified_even_with_no_open_subscribers(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher_with_three_alert_modes(temporary, "no-open-subs")
            watcher.state.remove_subscriber("1")
            watcher.state.remove_subscriber("3")
            watcher.cgv.fetch_date = lambda _date: self._schedule_payload(10)
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                general=session.remaining_seats - 2,
                accessible=2,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            sent = []
            watcher.telegram.send_message = lambda text, **kwargs: sent.append(
                (kwargs.get("chat_id"), text)
            )

            # Only a seats-only subscriber remains, so the open alert has no
            # recipients.  It must still be recorded, or it would re-fire every
            # cycle forever once an "all" subscriber joins later.
            first = watcher.run_cycle()
            second = watcher.run_cycle()

            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(second.new_sessions, 0)
            self.assertEqual(sent, [])

    def test_fetch_error_alert_reaches_every_alert_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher_with_three_alert_modes(temporary, "error-fanout")

            def fail(_date):
                raise FetchError("CGV 연결 실패: 테스트")

            watcher.cgv.fetch_date = fail
            sent = []
            watcher.telegram.send_message = lambda text, **kwargs: sent.append(
                (kwargs.get("chat_id"), text)
            )

            watcher.run_cycle()

            self.assertEqual({chat_id for chat_id, _ in sent}, {"1", "2", "3"})
            self.assertIn("CGV 감시 조회 오류", sent[0][1])

    def test_run_cycle_prunes_records_for_shows_that_already_played(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-prune-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            stale = "0013:30001323:2026-01-01:14:30"
            watcher.state.data["notified"][stale] = {"date": "2026-01-01"}
            watcher.state.set_seat_snapshot(stale, SeatSnapshot(total=4))
            watcher.cgv.fetch_date = lambda _date: {"data": []}
            watcher.telegram.send_message = lambda text, **_kwargs: None

            watcher.run_cycle()

            self.assertNotIn(stale, watcher.state.data["notified"])
            self.assertNotIn(stale, watcher.state.data["seat_counts"])
            reloaded = StateStore(config.state_file)
            reloaded.load()
            self.assertNotIn(stale, reloaded.data["notified"])

    def test_mode_commands_change_and_report_alert_preference(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-mode-cmd-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            replies = []

            def batch(update_id, text, chat_id=111222):
                return [
                    {
                        "update_id": update_id,
                        "message": {
                            "text": text,
                            "chat": {"id": chat_id, "type": "private"},
                        },
                    }
                ]

            batches = [
                batch(1, "/mode_seats", chat_id=555),
                batch(2, "/start"),
                batch(3, "/mode_seats@YongsanBot"),
                batch(4, "/status"),
                batch(5, "/mode"),
                batch(6, "/mode 오픈"),
                batch(7, "/mode nonsense"),
            ]
            watcher.telegram.get_updates = lambda **_kwargs: batches.pop(0)
            watcher.telegram.send_message = lambda text, **kwargs: replies.append(
                (kwargs.get("chat_id"), text)
            )

            watcher.sync_subscribers()
            self.assertIn("먼저 /start", replies[-1][1])
            self.assertEqual(watcher.state.alert_mode("555"), ALERT_MODE_ALL)

            watcher.sync_subscribers()
            self.assertEqual(watcher.state.alert_mode("111222"), ALERT_MODE_ALL)

            watcher.sync_subscribers()
            self.assertEqual(
                watcher.state.alert_mode("111222"), ALERT_MODE_SEATS_ONLY
            )
            self.assertIn("잔여 좌석만", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("알림 종류: 잔여 좌석만", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("현재 알림 종류: 잔여 좌석만", replies[-1][1])

            watcher.sync_subscribers()
            self.assertEqual(
                watcher.state.alert_mode("111222"), ALERT_MODE_OPEN_ONLY
            )

            watcher.sync_subscribers()
            self.assertIn("알 수 없는 알림 종류", replies[-1][1])
            self.assertEqual(
                watcher.state.alert_mode("111222"), ALERT_MODE_OPEN_ONLY
            )

            reloaded = StateStore(config.state_file)
            reloaded.load()
            self.assertEqual(reloaded.alert_mode("111222"), ALERT_MODE_OPEN_ONLY)

    def test_desc_command_explains_bot_and_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-desc-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            replies = []
            watcher.telegram.get_updates = lambda **_kwargs: [
                {
                    "update_id": 20,
                    "message": {
                        "text": "/desc@YongsanBot",
                        "chat": {
                            "id": 111222,
                            "type": "private",
                            "first_name": "구독자",
                        },
                    },
                }
            ]
            watcher.telegram.send_message = (
                lambda text, **kwargs: replies.append((kwargs.get("chat_id"), text))
            )

            watcher.sync_subscribers()

            self.assertEqual(replies[0][0], "111222")
            description = replies[0][1]
            self.assertIn("CGV 용산아이파크몰 IMAX", description)
            self.assertIn("오디세이 예매 오픈", description)
            self.assertIn("오늘부터 28일", description)
            self.assertIn("장애인석만 남은 경우", description)
            self.assertIn("A열만 남은 경우", description)
            self.assertIn("/start — 알림 구독", description)
            self.assertIn("예매 바로가기 링크 열기", description)
            self.assertIn("/stop — 알림 해지", description)

    def test_coffee_command_shows_kofi_donation_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-coffee-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            replies = []
            watcher.telegram.get_updates = lambda **_kwargs: [
                {
                    "update_id": 21,
                    "message": {
                        "text": "/coffee@YongsanBot",
                        "chat": {
                            "id": 111222,
                            "type": "private",
                            "first_name": "구독자",
                        },
                    },
                }
            ]
            watcher.telegram.send_message = (
                lambda text, **kwargs: replies.append((kwargs.get("chat_id"), text))
            )

            watcher.sync_subscribers()

            self.assertEqual(replies[0][0], "111222")
            self.assertIn("커피 한 잔 후원", replies[0][1])
            self.assertIn("https://ko-fi.com/yuemyname", replies[0][1])
            self.assertIn("모든 알림 기능은 동일", replies[0][1])

    def test_telegram_commands_are_polled_while_cgv_scan_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)),
                subscriptions_enabled=True,
                telegram_command_poll_seconds=2,
            )
            logger = logging.getLogger(f"watcher-command-poll-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            scan_started = threading.Event()
            release_scan = threading.Event()
            command_replied = threading.Event()
            command_stop = threading.Event()

            def blocked_fetch(_date):
                scan_started.set()
                release_scan.wait(2)
                return {"data": []}

            watcher.cgv.fetch_date = blocked_fetch
            watcher.telegram.get_updates = lambda **_kwargs: [
                {
                    "update_id": 22,
                    "message": {
                        "text": "/start",
                        "chat": {
                            "id": 111222,
                            "type": "private",
                            "first_name": "구독자",
                        },
                    },
                }
            ]

            def record_reply(_text, **_kwargs):
                command_replied.set()
                command_stop.set()

            watcher.telegram.send_message = record_reply
            scan_thread = threading.Thread(target=watcher.run_cycle)
            command_thread = threading.Thread(
                target=run_command_loop,
                args=(watcher, command_stop),
            )
            scan_thread.start()
            self.assertTrue(scan_started.wait(1))
            command_thread.start()
            try:
                self.assertTrue(command_replied.wait(1))
                self.assertTrue(watcher.state.is_subscribed("111222"))
            finally:
                command_stop.set()
                release_scan.set()
                command_thread.join(2)
                scan_thread.join(2)

    def test_successful_send_is_persisted_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary)
            config = make_config(project_dir)
            logger = logging.getLogger(f"watcher-test-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "screenName": "IMAX관",
                        "scnYmd": "20260826",
                        "scnFrTm": "1430",
                        "frSeatCnt": 100,
                        "stcnt": 624,
                    }
                ]
            }
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                general=session.remaining_seats,
                accessible=0,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            first = watcher.run_cycle()
            second = watcher.run_cycle()

            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(second.new_sessions, 0)
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("📅 상영일: 2026-08-26 (수)", sent_messages[0])
            self.assertIn("━━━━━━━━━━━━━━━━━━━━", sent_messages[0])
            self.assertIn(
                "상영 시작시간 14:30 — 100/624석",
                sent_messages[0],
            )
            self.assertLess(
                sent_messages[0].index("📅 상영일"),
                sent_messages[0].index("영화: 오디세이"),
            )
            self.assertLess(
                sent_messages[0].index("영화: 오디세이"),
                sent_messages[0].index("극장: 용산아이파크몰"),
            )
            self.assertNotIn("잔여좌석/총좌석:", sent_messages[0])
            self.assertTrue(config.state_file.exists())

    def test_sends_one_alert_for_each_seat_count_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-seat-change-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            remaining = {"count": 10}

            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": "1430",
                        "scnsNo": "13",
                        "scnSseq": "4",
                        "frSeatCnt": remaining["count"],
                        "stcnt": 200,
                    }
                ]
            }
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                general=session.remaining_seats - 2,
                accessible=2,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            first = watcher.run_cycle()
            remaining["count"] = 9
            second = watcher.run_cycle()
            third = watcher.run_cycle()

            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(second.seat_changes, 1)
            self.assertEqual(third.seat_changes, 0)
            self.assertEqual(len(sent_messages), 2)
            self.assertIn(
                "잔여좌석/총좌석: 9/200석 (이전 10/200석)", sent_messages[1]
            )
            self.assertIn("📅 상영일: 2026-08-26 (수)", sent_messages[1])
            self.assertLess(
                sent_messages[1].index("📅 상영일"),
                sent_messages[1].index("영화: 오디세이"),
            )
            self.assertLess(
                sent_messages[1].index("영화: 오디세이"),
                sent_messages[1].index("극장: 용산아이파크몰"),
            )

    def test_suppresses_change_when_only_accessible_seats_remain(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-accessible-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            availability = {"total": 3, "general": 1, "accessible": 2}

            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": "1430",
                        "scnsNo": "13",
                        "scnSseq": "4",
                        "frSeatCnt": availability["total"],
                        "stcnt": 200,
                    }
                ]
            }
            watcher.cgv.fetch_seat_snapshot = lambda _session: SeatSnapshot(
                total=availability["total"],
                general=availability["general"],
                accessible=availability["accessible"],
                mapped_total=availability["general"] + availability["accessible"],
                available_rows=("B",),
            )
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            watcher.run_cycle()
            availability.update(total=2, general=0, accessible=2)
            second = watcher.run_cycle()

            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(second.seat_changes, 0)
            self.assertEqual(second.suppressed_accessible_only, 1)

    def test_suppresses_new_session_when_only_row_a_remains(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-row-a-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)

            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": "1430",
                        "scnsNo": "13",
                        "scnSseq": "4",
                        "frSeatCnt": 2,
                        "stcnt": 200,
                    }
                ]
            }
            watcher.cgv.fetch_seat_snapshot = lambda _session: SeatSnapshot(
                total=2,
                general=2,
                accessible=0,
                mapped_total=2,
                available_rows=("A",),
            )
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            result = watcher.run_cycle()

            self.assertEqual(sent_messages, [])
            self.assertEqual(result.new_sessions, 0)
            self.assertEqual(result.suppressed_row_a_only, 1)

    def test_defers_new_alert_after_seat_detail_error_then_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-deferred-detail-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": "1430",
                        "scnsNo": "13",
                        "scnSseq": "4",
                        "frSeatCnt": 2,
                        "stcnt": 200,
                    }
                ]
            }
            detail_available = {"value": False}

            def fetch_seat_snapshot(_session):
                if not detail_available["value"]:
                    raise FetchError("CGV 응답 오류: HTTP 429")
                return SeatSnapshot(
                    total=2,
                    general=2,
                    accessible=0,
                    mapped_total=2,
                    available_rows=("B",),
                )

            watcher.cgv.fetch_seat_snapshot = fetch_seat_snapshot
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            first = watcher.run_cycle()
            session = BookingSession(date="2026-08-26", start_time="14:30")
            key = session.notification_key(site_no="0013", movie_no="30001323")
            self.assertEqual(first.new_sessions, 0)
            self.assertEqual(first.deferred_seat_details, 1)
            self.assertEqual(first.seat_detail_errors, 1)
            self.assertFalse(watcher.state.was_notified(key))
            self.assertEqual(sent_messages, [])

            detail_available["value"] = True
            second = watcher.run_cycle()

            self.assertEqual(second.new_sessions, 1)
            self.assertEqual(second.deferred_seat_details, 0)
            self.assertEqual(len(sent_messages), 1)
            self.assertTrue(watcher.state.was_notified(key))

    def test_alerts_new_session_with_seven_seats_after_detail_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-fallback-new-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": "1430",
                        "scnsNo": "13",
                        "scnSseq": "4",
                        "frSeatCnt": 7,
                        "stcnt": 200,
                    }
                ]
            }

            def fail_seat_detail(_session):
                raise FetchError("CGV 응답 오류: HTTP 429")

            watcher.cgv.fetch_seat_snapshot = fail_seat_detail
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            result = watcher.run_cycle()
            session = BookingSession(date="2026-08-26", start_time="14:30")
            key = session.notification_key(site_no="0013", movie_no="30001323")

            self.assertEqual(result.new_sessions, 1)
            self.assertEqual(result.deferred_seat_details, 0)
            self.assertEqual(result.seat_detail_errors, 1)
            self.assertEqual(result.unclassified_fallback_alerts, 1)
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("7/200석", sent_messages[0])
            self.assertIn("좌석 종류 미확인", sent_messages[0])
            self.assertTrue(watcher.state.was_notified(key))

    def test_alerts_seat_change_with_seven_seats_after_detail_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-fallback-change-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            remaining = {"count": 8}
            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": "1430",
                        "scnsNo": "13",
                        "scnSseq": "4",
                        "frSeatCnt": remaining["count"],
                        "stcnt": 200,
                    }
                ]
            }

            def fetch_seat_snapshot(session):
                if session.remaining_seats == 7:
                    raise FetchError("CGV 응답 오류: HTTP 429")
                return SeatSnapshot(
                    total=8,
                    general=8,
                    accessible=0,
                    mapped_total=8,
                    available_rows=("B",),
                )

            watcher.cgv.fetch_seat_snapshot = fetch_seat_snapshot
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            first = watcher.run_cycle()
            remaining["count"] = 7
            second = watcher.run_cycle()

            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(second.seat_changes, 1)
            self.assertEqual(second.deferred_seat_details, 0)
            self.assertEqual(second.seat_detail_errors, 1)
            self.assertEqual(second.unclassified_fallback_alerts, 1)
            self.assertEqual(len(sent_messages), 2)
            self.assertIn("7/200석 (이전 8/200석)", sent_messages[1])
            self.assertIn("좌석 종류 미확인", sent_messages[1])

    def test_suppresses_change_to_zero_remaining_seats(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-sold-out-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            remaining = {"count": 1}
            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": "1430",
                        "scnsNo": "13",
                        "scnSseq": "4",
                        "frSeatCnt": remaining["count"],
                        "stcnt": 200,
                    }
                ]
            }
            watcher.cgv.fetch_seat_snapshot = lambda _session: SeatSnapshot(
                total=1,
                general=1,
                accessible=0,
                mapped_total=1,
                available_rows=("B",),
            )
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            first = watcher.run_cycle()
            remaining["count"] = 0
            second = watcher.run_cycle()
            third = watcher.run_cycle()

            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(second.seat_changes, 0)
            self.assertEqual(second.suppressed_sold_out, 1)
            self.assertEqual(third.seat_changes, 0)
            self.assertEqual(len(sent_messages), 1)


class CgvClientTests(unittest.TestCase):
    def test_anonymous_request_has_expected_query_and_no_login_headers(self):
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

            def read(self, _limit):
                return b'{"data": []}'

        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            client = CgvClient(config)
            with patch("watcher.urllib.request.urlopen", return_value=FakeResponse()) as opened:
                payload = client.fetch_date(dt.date(2026, 8, 26))

            request = opened.call_args.args[0]
            headers = {key.lower(): value for key, value in request.header_items()}
            query = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(request.full_url).query,
                    keep_blank_values=True,
                )
            )
            self.assertEqual(payload, {"data": []})
            self.assertNotIn("authorization", headers)
            self.assertNotIn("cookie", headers)
            self.assertEqual(query["siteNo"], "0013")
            self.assertEqual(query["movNo"], "30001323")
            self.assertEqual(query["scnYmd"], "20260826")

    def test_seat_request_is_anonymous_and_uses_schedule_identifiers(self):
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

            def read(self, _limit):
                return b'{"data": {"items": []}}'

        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            client = CgvClient(config)
            session = BookingSession(
                date="2026-08-26",
                start_time="14:30",
                screen_no="13",
                screen_sequence="4",
                remaining_seats=182,
            )
            with patch("watcher.urllib.request.urlopen", return_value=FakeResponse()) as opened:
                snapshot = client.fetch_seat_snapshot(session)

            request = opened.call_args.args[0]
            headers = {key.lower(): value for key, value in request.header_items()}
            query = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(request.full_url).query,
                    keep_blank_values=True,
                )
            )
            self.assertEqual(snapshot.total, 182)
            self.assertNotIn("authorization", headers)
            self.assertNotIn("cookie", headers)
            self.assertEqual(query["scnsNo"], "13")
            self.assertEqual(query["scnSseq"], "4")
            self.assertEqual(query["custNo"], "")


if __name__ == "__main__":
    unittest.main()
