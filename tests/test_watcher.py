import datetime as dt
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

from watcher import (
    BookingSession,
    CgvClient,
    Config,
    SeatSnapshot,
    StateStore,
    Watcher,
    booking_url_for_session,
    extract_seat_snapshot,
    extract_sessions,
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
                "STRICT_IMAX_MATCH=true",
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
            self.assertEqual(reloaded.data["version"], 3)
            self.assertEqual(reloaded.seat_snapshot("session").general, 3)
            self.assertEqual(
                reloaded.seat_snapshot("session").available_rows, ("A", "B")
            )


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


class WatcherIntegrationTests(unittest.TestCase):
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
            sent_messages = []
            watcher.telegram.send_message = sent_messages.append

            first = watcher.run_cycle()
            second = watcher.run_cycle()

            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(second.new_sessions, 0)
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("일자: 2026-08-26", sent_messages[0])
            self.assertIn(
                "상영 시작시간 14:30 — 잔여좌석/총좌석: 100/624석",
                sent_messages[0],
            )
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
            )
            sent_messages = []
            watcher.telegram.send_message = sent_messages.append

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
            )
            sent_messages = []
            watcher.telegram.send_message = sent_messages.append

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
            watcher.telegram.send_message = sent_messages.append

            result = watcher.run_cycle()

            self.assertEqual(sent_messages, [])
            self.assertEqual(result.new_sessions, 0)
            self.assertEqual(result.suppressed_row_a_only, 1)


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
