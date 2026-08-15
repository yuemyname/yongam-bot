import datetime as dt
import dataclasses
import http.client
import json
import logging
import os
import re
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
from zoneinfo import ZoneInfo

from watcher import (
    ALERT_MODE_ALL,
    ALERT_MODE_OPEN_ONLY,
    ALERT_MODE_SEATS_ONLY,
    ALERT_OPEN,
    ALERT_SEATS,
    ALERT_SEATS_SWEET,
    ALERT_SEATS_UNCLASSIFIED,
    ALERT_SYSTEM,
    SCAN_MODE_CURSOR,
    SCAN_MODE_FULL,
    MIN_SEATS_DEFAULT,
    SEAT_SELECTION_ALL,
    SEAT_SELECTION_SWEET,
    STATE_VERSION,
    BookingSession,
    CgvClient,
    Config,
    FetchError,
    SeatSnapshot,
    StateStore,
    PENDING_DELIVERY_TTL_HOURS,
    TelegramError,
    TimezoneFormatter,
    Watcher,
    _available_seat_line,
    _available_seat_count,
    _sweet_seat_snapshot,
    booking_url_for_session,
    _seat_snapshot_changed,
    extract_seat_snapshot,
    extract_sessions,
    rate_limit_backoff_seconds,
    run_command_loop,
)


def _kst(day: dt.date, hour: int = 9, minute: int = 0) -> dt.datetime:
    """A fixed local timestamp so time-based suppression is deterministic."""
    return dt.datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo("Asia/Seoul")
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
            usable=2,
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

    def test_counts_saleable_non_a_seats_without_using_sale_form_code(self):
        payload = {
            "data": {
                "items": [
                    {
                        "seats": [
                            {
                                "seatLocNo": "1",
                                "seatRowNm": "B",
                                "seatNo": "12",
                                "seatStusCd": "00",
                                "seatSaleYn": "Y",
                                "seatSalfrmCd": "01",
                            },
                            {
                                "seatLocNo": "2",
                                "seatRowNm": "A",
                                "seatNo": "1",
                                "seatStusCd": "00",
                                "seatSaleYn": "Y",
                                "seatSalfrmCd": "04",
                            },
                            {
                                "seatLocNo": "3",
                                "seatRowNm": "C",
                                "seatNo": "14",
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
        self.assertEqual(snapshot.usable, 1)
        self.assertEqual(snapshot.mapped_total, 2)
        self.assertEqual(snapshot.available_rows, ("A", "B"))
        self.assertEqual(snapshot.available_seats, (("B", "12"),))
        self.assertFalse(snapshot.row_a_only)

    def test_row_a_only_ignores_sale_form_code(self):
        seats = [
            {
                "seatLocNo": "W1",
                "seatRowNm": "A",
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
        self.assertTrue(matching.row_a_only)
        self.assertFalse(partial.row_a_only)

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

    def test_sale_form_code_does_not_override_the_actual_row(self):
        payload = {
            "data": {
                "seats": [
                    {
                        "seatLocNo": "A1",
                        "seatRowNm": "A",
                        "seatNo": "1",
                        "seatStusCd": "00",
                        "seatSaleYn": "Y",
                        "seatSalfrmCd": "01",
                    },
                    {
                        "seatLocNo": "W1",
                        "seatRowNm": "B",
                        "seatNo": "7",
                        "seatStusCd": "00",
                        "seatSaleYn": "Y",
                        "seatSalfrmCd": "04",
                    },
                ]
            }
        }

        snapshot = extract_seat_snapshot(payload, scheduled_remaining=2)

        self.assertEqual(snapshot.usable, 1)
        self.assertEqual(snapshot.available_rows, ("A", "B"))
        self.assertEqual(snapshot.available_seats, (("B", "7"),))
        self.assertFalse(snapshot.row_a_only)
        self.assertFalse(snapshot.should_suppress)

    def test_formats_rows_and_compacts_consecutive_seat_numbers(self):
        snapshot = SeatSnapshot(
            total=7,
            usable=6,
            mapped_total=7,
            available_rows=("A", "B", "C"),
            available_seats=(
                ("B", "1"),
                ("B", "2"),
                ("B", "3"),
                ("B", "5"),
                ("C", "10"),
                ("C", "11"),
            ),
        )

        self.assertEqual(
            _available_seat_line(snapshot),
            "A열 제외 잔여 좌석: B1~3, 5 / C10~11",
        )

    def test_keeps_row_information_when_seat_number_is_missing(self):
        payload = {
            "data": {
                "seats": [
                    {
                        "seatLocNo": "internal-1",
                        "seatRowNm": "B",
                        "seatStusCd": "00",
                        "seatSaleYn": "Y",
                    }
                ]
            }
        }

        snapshot = extract_seat_snapshot(payload, scheduled_remaining=1)

        self.assertTrue(snapshot.seat_map_complete)
        self.assertIsNone(snapshot.available_seats)
        self.assertEqual(
            _available_seat_line(snapshot),
            "A열 제외 잔여 좌석: B열 (좌석 번호 미확인)",
        )

    def test_summarizes_rows_when_seat_location_line_is_too_long(self):
        snapshot = SeatSnapshot(
            total=6,
            usable=6,
            mapped_total=6,
            available_rows=("B", "C"),
            available_seats=(
                ("B", "1"),
                ("B", "3"),
                ("B", "5"),
                ("C", "2"),
                ("C", "4"),
                ("C", "6"),
            ),
        )

        self.assertEqual(
            _available_seat_line(snapshot, max_chars=20),
            "A열 제외 잔여 좌석: B열 3석 / C열 3석 (좌석 번호가 많아 행별 수량으로 요약)",
        )

    def test_sweet_preset_combines_all_three_recommended_areas(self):
        locations = (
            ("F", "15"),
            ("F", "16"),
            ("G", "29"),
            ("G", "30"),
            ("H", "13"),
            ("I", "32"),
            ("J", "11"),
            ("K", "34"),
            ("L", "34"),
            ("L", "35"),
            ("M", "20"),
        )
        snapshot = SeatSnapshot(
            total=len(locations),
            usable=len(locations),
            mapped_total=len(locations),
            available_rows=tuple(sorted({row for row, _number in locations})),
            available_seats=locations,
        )

        sweet = _sweet_seat_snapshot(snapshot)

        self.assertIsNotNone(sweet)
        self.assertEqual(
            sweet.available_seats,
            (
                ("F", "16"),
                ("G", "29"),
                ("H", "13"),
                ("I", "32"),
                ("J", "11"),
                ("K", "34"),
                ("L", "34"),
            ),
        )


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

    def test_persists_failed_schedule_dates_until_they_succeed(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "notified.json"
            failed_date = dt.date(2026, 9, 1)
            store = StateStore(state_path)

            self.assertTrue(store.note_schedule_failure(failed_date))
            store.save()

            reloaded = StateStore(state_path)
            reloaded.load()
            self.assertEqual(reloaded.failed_schedule_dates(), (failed_date,))
            self.assertTrue(reloaded.clear_schedule_failure(failed_date))
            self.assertEqual(reloaded.failed_schedule_dates(), ())

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
                    usable=3,
                    mapped_total=5,
                    available_rows=("A", "B"),
                    available_seats=(("B", "10"), ("B", "11"), ("B", "12")),
                ),
            )
            store.save()

            reloaded = StateStore(state_path)
            reloaded.load()
            self.assertEqual(reloaded.data["version"], STATE_VERSION)
            self.assertEqual(reloaded.seat_snapshot("session").usable, 3)
            self.assertEqual(
                reloaded.seat_snapshot("session").available_rows, ("A", "B")
            )
            self.assertEqual(
                reloaded.seat_snapshot("session").available_seats,
                (("B", "10"), ("B", "11"), ("B", "12")),
            )
            record = json.loads(state_path.read_text(encoding="utf-8"))["seat_counts"][
                "session"
            ]
            self.assertEqual(record["usable"], 3)
            self.assertEqual(
                record["available_seats"],
                [["B", "10"], ["B", "11"], ["B", "12"]],
            )
            self.assertNotIn("general", record)
            self.assertNotIn("accessible", record)

    def test_old_seat_breakdown_loads_without_guessing_usable_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "notified.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 8,
                        "notified": {},
                        "seat_counts": {
                            "session": {
                                "total": 5,
                                "general": 3,
                                "accessible": 2,
                                "mapped_total": 5,
                                "available_rows": ["A", "B"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            store = StateStore(state_path)
            store.load()

            snapshot = store.seat_snapshot("session")
            self.assertEqual(snapshot.total, 5)
            self.assertIsNone(snapshot.usable)
            self.assertFalse(snapshot.seat_map_complete)

    def test_change_detection_compares_non_a_usable_count(self):
        previous = SeatSnapshot(
            total=5,
            usable=2,
            mapped_total=5,
            available_rows=("A", "B"),
            available_seats=(("B", "1"), ("B", "2")),
        )
        same = dataclasses.replace(previous)
        changed = dataclasses.replace(previous, usable=3)
        moved = dataclasses.replace(
            previous, available_seats=(("B", "1"), ("B", "3"))
        )

        self.assertFalse(_seat_snapshot_changed(previous, same))
        self.assertTrue(_seat_snapshot_changed(previous, changed))
        self.assertTrue(_seat_snapshot_changed(previous, moved))

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

    def test_subscriber_stored_before_the_feature_accepts_unclassified(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.data["subscribers"]["999"] = {"subscribed_at": "2026-01-01T00:00:00"}

            self.assertEqual(store.seat_selection("999"), SEAT_SELECTION_ALL)
            self.assertIn("999", store.subscriber_ids_for(ALERT_SEATS_UNCLASSIFIED))

    def test_rejects_an_unknown_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.add_subscriber("1")
            with self.assertRaises(ValueError):
                store.set_alert_mode("1", "nope")

    def test_seat_selection_defaults_to_all_and_routes_sweet_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "notified.json"
            store = StateStore(path)
            store.add_subscriber("all")
            store.add_subscriber("sweet")

            self.assertEqual(store.seat_selection("all"), SEAT_SELECTION_ALL)
            self.assertTrue(
                store.set_seat_selection("sweet", SEAT_SELECTION_SWEET)
            )
            self.assertEqual(store.subscriber_ids_for(ALERT_SEATS), ("all",))
            self.assertEqual(
                store.subscriber_ids_for(ALERT_SEATS_SWEET), ("sweet",)
            )
            self.assertEqual(
                store.subscriber_ids_for(ALERT_SEATS_UNCLASSIFIED), ("all",)
            )
            self.assertEqual(
                store.subscriber_ids_for(ALERT_OPEN), ("all", "sweet")
            )
            store.save()

            reloaded = StateStore(path)
            reloaded.load()
            self.assertEqual(
                reloaded.seat_selection("sweet"), SEAT_SELECTION_SWEET
            )


class ScanPlanTests(unittest.TestCase):
    TODAY = dt.date(2026, 8, 13)

    def _watcher(self, temporary, **overrides):
        settings = {
            "dynamic_date_window": True,
            "target_window_days": 28,
            "scan_mode": SCAN_MODE_CURSOR,
            **overrides,
        }
        config = dataclasses.replace(make_config(Path(temporary)), **settings)
        logger = logging.getLogger(f"scan-plan-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        return Watcher(config, logger=logger)

    def test_full_mode_always_requests_the_whole_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary, scan_mode=SCAN_MODE_FULL)
            watcher.state.advance_frontier(dt.date(2026, 8, 20))

            plan = watcher._plan_scan(self.TODAY)

            self.assertTrue(plan.full_scan)
            self.assertEqual(len(plan.dates), 28)

    def test_cursor_mode_sweeps_the_window_when_no_frontier_is_known(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)

            plan = watcher._plan_scan(self.TODAY)

            self.assertTrue(plan.full_scan)
            self.assertEqual(len(plan.dates), 28)

    def test_cursor_mode_requests_open_range_plus_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            watcher.state.advance_frontier(dt.date(2026, 8, 20))
            watcher._cycle_index = 1

            plan = watcher._plan_scan(self.TODAY)

            self.assertFalse(plan.full_scan)
            self.assertEqual(plan.open_end, dt.date(2026, 8, 20))
            # The three-day new-opening probe comes before known open dates.
            self.assertEqual(
                plan.dates[:3],
                (
                    dt.date(2026, 8, 21),
                    dt.date(2026, 8, 22),
                    dt.date(2026, 8, 23),
                ),
            )
            self.assertEqual(plan.dates[3], self.TODAY)
            self.assertEqual(plan.dates[-1], dt.date(2026, 8, 20))
            self.assertEqual(len(plan.dates), 11)

    def test_failed_date_outside_cursor_is_retried_after_the_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            watcher.state.advance_frontier(dt.date(2026, 8, 20))
            watcher.state.note_schedule_failure(dt.date(2026, 9, 1))
            watcher._cycle_index = 1

            plan = watcher._plan_scan(self.TODAY)

            self.assertEqual(
                plan.dates[:4],
                (
                    dt.date(2026, 8, 21),
                    dt.date(2026, 8, 22),
                    dt.date(2026, 8, 23),
                    dt.date(2026, 9, 1),
                ),
            )

    def test_probe_stays_anchored_to_today_when_the_frontier_has_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            watcher.state.advance_frontier(dt.date(2026, 1, 1))
            watcher._cycle_index = 1

            plan = watcher._plan_scan(self.TODAY)

            self.assertFalse(plan.full_scan)
            self.assertEqual(plan.open_end, self.TODAY - dt.timedelta(days=1))
            self.assertEqual(plan.dates[0], self.TODAY)
            self.assertEqual(plan.dates[-1], dt.date(2026, 8, 15))

    def test_probe_is_clamped_to_the_window_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            watcher.state.advance_frontier(dt.date(2026, 9, 9))
            watcher._cycle_index = 1

            plan = watcher._plan_scan(self.TODAY)

            self.assertEqual(plan.dates[0], self.TODAY)
            self.assertEqual(plan.dates[-1], dt.date(2026, 9, 9))
            self.assertEqual(len(plan.dates), 28)

    def test_a_full_scan_runs_every_configured_number_of_cycles(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary, full_scan_every_cycles=10)
            watcher.state.advance_frontier(dt.date(2026, 8, 20))

            full_scan_cycles = []
            for index in range(21):
                watcher._cycle_index = index
                if watcher._plan_scan(self.TODAY).full_scan:
                    full_scan_cycles.append(index)

            self.assertEqual(full_scan_cycles, [0, 10, 20])

    def test_frontier_only_moves_forward(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)

            self.assertTrue(watcher.state.advance_frontier(dt.date(2026, 8, 20)))
            self.assertFalse(watcher.state.advance_frontier(dt.date(2026, 8, 15)))
            self.assertFalse(watcher.state.advance_frontier(dt.date(2026, 8, 20)))
            self.assertTrue(watcher.state.advance_frontier(dt.date(2026, 8, 21)))
            self.assertEqual(watcher.state.frontier_date, dt.date(2026, 8, 21))


class MinimumSeatsTests(unittest.TestCase):
    """The /count setting: how many seats must be on sale."""

    TODAY = dt.date(2026, 8, 26)

    @staticmethod
    def _snapshot(usable, *, rows=("A", "H"), seats=None):
        available = (
            seats
            if seats is not None
            else tuple(("H", str(i)) for i in range(1, usable + 1))
        )
        return SeatSnapshot(
            total=usable + 1,  # one A-row seat always sits unsold
            usable=usable,
            mapped_total=usable + 1,
            available_rows=rows,
            available_seats=available,
        )

    def test_available_count_ignores_row_a(self):
        # Four seats on sale, one of them in row A.
        self.assertEqual(_available_seat_count(self._snapshot(3)), 3)

    def test_available_count_is_zero_when_rows_cannot_be_read(self):
        # The schedule total counts row A, which is exactly what this
        # audience excluded, so an unreadable map confirms nothing.
        self.assertEqual(_available_seat_count(SeatSnapshot(total=11)), 0)

    def test_a_partly_read_map_counts_only_its_confirmed_non_a_seats(self):
        """Eight seats left, one confirmed outside row A.

        The other seven were not classified, so a two-seat subscriber must
        not be told this showing has a pair.
        """

        current = SeatSnapshot(
            total=8, usable=1, mapped_total=2, available_rows=("A", "H")
        )

        self.assertTrue(current.uses_unclassified_fallback)
        self.assertEqual(_available_seat_count(current), 1)

    def _watcher(self, temporary, **overrides):
        config = dataclasses.replace(
            make_config(Path(temporary)),
            target_start=self.TODAY,
            target_end=self.TODAY,
            **overrides,
        )
        logger = logging.getLogger(f"min-seats-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.state.remove_subscriber(config.telegram_chat_id)
        return watcher

    def test_default_is_no_threshold_at_all(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            watcher.state.add_subscriber("any")

            self.assertEqual(watcher.state.min_seats("any"), MIN_SEATS_DEFAULT)
            # Even a seat count the bot could not read still reaches the
            # default audience; the setting must not silently narrow them.
            self.assertEqual(
                watcher.state.subscriber_ids_for(ALERT_SEATS, seats_available=0),
                ("any",),
            )

    def test_two_seat_minimum_filters_by_how_many_are_on_sale(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            watcher.state.add_subscriber("any")
            watcher.state.add_subscriber("pair")
            self.assertTrue(watcher.state.set_min_seats("pair", 2))

            for available, expected in ((0, ("any",)), (1, ("any",)),
                                        (2, ("any", "pair")),
                                        (5, ("any", "pair"))):
                self.assertEqual(
                    watcher.state.subscriber_ids_for(
                        ALERT_SEATS, seats_available=available
                    ),
                    expected,
                    f"{available}석 남음",
                )

    def test_booking_open_alerts_ignore_the_minimum(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            watcher.state.add_subscriber("pair")
            watcher.state.set_min_seats("pair", 2)

            # Detecting a newly opened showing is the bot's whole point, so no
            # seat-count setting may withhold it.
            self.assertEqual(
                watcher.state.subscriber_ids_for(ALERT_OPEN), ("pair",)
            )
            self.assertEqual(
                watcher.state.subscriber_ids_for(ALERT_SYSTEM), ("pair",)
            )

    def _run_seat_levels(self, watcher, levels):
        usable = {"n": levels[0]}
        watcher.cgv.fetch_date = lambda _date: {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": self.TODAY.strftime("%Y%m%d"),
                    "scnsrtTm": "2330",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": usable["n"] + 1,
                    "stcnt": 200,
                }
            ]
        }
        watcher.cgv.fetch_seat_snapshot = lambda _session: self._snapshot(
            usable["n"]
        )
        sent = []
        watcher.telegram.send_message = lambda text, **kwargs: sent.append(
            (kwargs.get("chat_id"), text)
        )

        watcher.run_cycle()  # first sighting: booking-open alert
        watcher.run_cycle()  # baseline seat map
        received = []
        for count in levels[1:]:
            usable["n"] = count
            before = len(sent)
            watcher.run_cycle()
            received.append(sorted(chat for chat, _text in sent[before:]))
        return received

    def test_one_seat_left_skips_the_pair_subscriber(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                watcher.state.add_subscriber("any")
                watcher.state.add_subscriber("pair")
                watcher.state.set_min_seats("pair", 2)

                # 5석 -> 1석 -> 3석 -> 3석(변화 없음)
                received = self._run_seat_levels(watcher, [5, 1, 3, 3])

            self.assertEqual(
                received, [["any"], ["any", "pair"], ["any", "pair"]]
            )

    def test_a_sold_out_showing_reaches_nobody(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                watcher.state.add_subscriber("any")
                received = self._run_seat_levels(watcher, [5, 0, 0])

            self.assertEqual(received, [[], []])

    def test_sweet_subscribers_are_measured_inside_the_sweet_area(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            watcher.state.add_subscriber("pair-sweet")
            watcher.state.set_seat_selection("pair-sweet", SEAT_SELECTION_SWEET)
            watcher.state.set_min_seats("pair-sweet", 2)

            # Three seats on sale, only one of them inside the sweet ranges.
            current = self._snapshot(
                3, seats=(("H", "20"), ("H", "1"), ("H", "2"))
            )
            sweet = _sweet_seat_snapshot(current)

            self.assertEqual(_available_seat_count(current), 3)
            self.assertEqual(_available_seat_count(sweet), 1)
            self.assertEqual(
                watcher.state.subscriber_ids_for(
                    ALERT_SEATS_SWEET,
                    seats_available=_available_seat_count(sweet),
                ),
                (),
            )

    def test_count_commands_change_report_and_persist_the_minimum(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"count-cmd-{id(self)}")
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
                batch(1, "/count_2", chat_id=555),
                batch(2, "/start"),
                batch(3, "/count_2@YongsanBot"),
                batch(4, "/status"),
                batch(5, "/count_2"),
                batch(6, "/count"),
                batch(7, "/count 9"),
                batch(8, "/count_1"),
            ]
            watcher.telegram.get_updates = lambda **_kwargs: batches.pop(0)
            watcher.telegram.send_message = lambda text, **kwargs: replies.append(
                (kwargs.get("chat_id"), text)
            )

            watcher.sync_subscribers()
            self.assertIn("구독 중이 아닙니다", replies[-1][1])

            watcher.sync_subscribers()
            self.assertEqual(watcher.state.min_seats("111222"), MIN_SEATS_DEFAULT)

            watcher.sync_subscribers()
            self.assertEqual(watcher.state.min_seats("111222"), 2)
            self.assertIn("2석 이상 남았을 때만", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("예매 가능 최소 좌석: 2석 이상 남았을 때만", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("이미 이렇게 설정", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("현재 예매 가능 최소 좌석", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("알 수 없는 좌석 수", replies[-1][1])
            self.assertEqual(watcher.state.min_seats("111222"), 2)

            watcher.sync_subscribers()
            self.assertEqual(watcher.state.min_seats("111222"), MIN_SEATS_DEFAULT)

            reloaded = StateStore(config.state_file)
            reloaded.load()
            self.assertEqual(
                reloaded.min_seats("111222"), MIN_SEATS_DEFAULT
            )


class SeatsOnSaleAlertTests(unittest.TestCase):
    """The alert reports what is bookable now, not what changed."""

    TODAY = dt.date(2026, 8, 26)
    KEY = "0013:30001323:2026-08-26:23:30"

    def _watcher(self, temporary, **overrides):
        config = dataclasses.replace(
            make_config(Path(temporary)),
            target_start=self.TODAY,
            target_end=self.TODAY,
            **overrides,
        )
        logger = logging.getLogger(f"on-sale-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.state.remove_subscriber(config.telegram_chat_id)
        watcher.state.add_subscriber("구독자")
        return watcher

    def _run(self, watcher, levels):
        """Walk a showing through seat counts; return alerts per cycle."""

        free = {"n": levels[0]}
        watcher.cgv.fetch_date = lambda _date: {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": self.TODAY.strftime("%Y%m%d"),
                    "scnsrtTm": "2330",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": free["n"],
                    "stcnt": 624,
                }
            ]
        }

        def seat(_session):
            seats = tuple(("B", str(i)) for i in range(1, free["n"] + 1))
            return SeatSnapshot(
                total=len(seats),
                usable=len(seats),
                mapped_total=len(seats),
                available_rows=("B",) if seats else (),
                available_seats=seats,
            )

        watcher.cgv.fetch_seat_snapshot = seat
        sent = []
        watcher.telegram.send_message = lambda text, **_kwargs: sent.append(text)

        per_cycle = []
        for count in levels:
            free["n"] = count
            before = len(sent)
            watcher.run_cycle()
            per_cycle.append(sent[before:])
        return per_cycle

    def test_an_unchanged_showing_is_announced_again_every_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                cycles = self._run(watcher, [3, 3, 3, 3])

            headers = [
                [text.splitlines()[0] for text in cycle] for cycle in cycles
            ]
            self.assertEqual(
                headers,
                [
                    ["🎟️ CGV 예매 오픈 감지"],
                    ["💺 CGV 예매 가능 좌석"],
                    ["💺 CGV 예매 가능 좌석"],
                    ["💺 CGV 예매 가능 좌석"],
                ],
            )

    def test_a_sold_out_showing_goes_quiet_and_speaks_up_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                # opened -> on sale -> sold out -> still sold out -> a
                # cancellation ticket appears
                cycles = self._run(watcher, [3, 3, 0, 0, 2])

            self.assertEqual([len(cycle) for cycle in cycles], [1, 1, 0, 0, 1])
            self.assertIn("잔여좌석/총좌석: 2/624석", cycles[4][0])

    def test_a_repeat_carries_no_previous_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                cycles = self._run(watcher, [5, 5, 4, 4])

            # The cycle the count moved contrasts with the old value; the
            # cycle after it has nothing to contrast with, so it says only
            # what is on sale.
            self.assertIn("잔여좌석/총좌석: 4/624석 (이전 5/624석)", cycles[2][0])
            self.assertIn("잔여좌석/총좌석: 4/624석\n", cycles[3][0])
            self.assertNotIn("이전", cycles[3][0])

    def test_the_repeat_interval_throttles_only_unchanged_showings(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                # The clock is pinned, so nothing is ever due again: every
                # repeat is throttled and only real movement gets through.
                watcher = self._watcher(temporary, seat_alert_repeat_minutes=30)
                cycles = self._run(watcher, [5, 5, 5, 4, 4])

            self.assertEqual([len(cycle) for cycle in cycles], [1, 1, 0, 1, 0])
            self.assertIn("예매 오픈 감지", cycles[0][0])
            self.assertIn("4/624석 (이전 5/624석)", cycles[3][0])


class BookingOpenWithoutSeatDetailTests(unittest.TestCase):
    """Booking-open alerts ship on the schedule response alone."""

    TODAY = dt.date(2026, 8, 26)
    KEY = "0013:30001323:2026-08-26:14:30"
    # Two sweet seats (J11~34), two ordinary ones, one in row A.
    SEATS = [("A", "5"), ("B", "10"), ("C", "11"), ("J", "20"), ("J", "21")]

    def _watcher(self, temporary):
        config = dataclasses.replace(
            make_config(Path(temporary)),
            target_start=self.TODAY,
            target_end=self.TODAY,
        )
        logger = logging.getLogger(f"open-fast-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.state.remove_subscriber(config.telegram_chat_id)
        for who in ("기본", "명당", "2매이상"):
            watcher.state.add_subscriber(who)
        watcher.state.set_seat_selection("명당", SEAT_SELECTION_SWEET)
        watcher.state.set_min_seats("2매이상", 2)
        return watcher

    def _wire(self, watcher, sold):
        """Sell seats off the front of SEATS; return the request/alert logs."""

        def free_seats():
            return tuple(self.SEATS[sold["n"]:])

        watcher.cgv.fetch_date = lambda _date: {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": self.TODAY.strftime("%Y%m%d"),
                    "scnsrtTm": "1430",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": len(free_seats()),
                    "stcnt": 624,
                }
            ]
        }
        seat_requests = []

        def fetch_seat(session):
            seat_requests.append(session.start_time)
            seats = free_seats()
            non_a = tuple(seat for seat in seats if seat[0] != "A")
            return SeatSnapshot(
                total=len(seats),
                usable=len(non_a),
                mapped_total=len(seats),
                available_rows=tuple(sorted({row for row, _ in seats})),
                available_seats=non_a,
            )

        watcher.cgv.fetch_seat_snapshot = fetch_seat
        sent = []
        watcher.telegram.send_message = lambda text, **kwargs: sent.append(
            (kwargs.get("chat_id"), text)
        )
        return seat_requests, sent

    def test_the_opening_cycle_makes_no_seat_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                seat_requests, sent = self._wire(watcher, {"n": 0})
                result = watcher.run_cycle()

            self.assertEqual(seat_requests, [])
            self.assertEqual(result.new_sessions, 1)
            self.assertEqual(
                sorted(chat for chat, _text in sent),
                ["2매이상", "기본", "명당"],
            )
            # /seat_sweet and /count_2 never withhold a booking-open alert.
            self.assertTrue(
                all("예매 오픈 감지" in text for _chat, text in sent)
            )

    def test_the_next_cycle_records_the_baseline_and_announces_the_seats(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                seat_requests, sent = self._wire(watcher, {"n": 0})
                watcher.run_cycle()
                sent.clear()
                watcher.run_cycle()

                self.assertEqual(seat_requests, ["14:30"])
                stored = watcher.state.seat_snapshot(self.KEY)

            self.assertTrue(stored.seat_map_complete)
            self.assertEqual(stored.usable, 4)
            # The map it just read says seats are on sale, so every audience
            # hears about them.
            self.assertEqual(
                sorted(chat for chat, _text in sent),
                ["2매이상", "기본", "명당"],
            )

    def test_seat_alerts_are_normal_from_the_baseline_onward(self):
        with tempfile.TemporaryDirectory() as temporary:
            sold = {"n": 0}
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                seat_requests, sent = self._wire(watcher, sold)
                watcher.run_cycle()  # 예매 오픈
                watcher.run_cycle()  # 좌석표 기준선

                def cycle(sold_count):
                    sold["n"] = sold_count
                    sent.clear()
                    watcher.run_cycle()
                    return sorted(chat for chat, _text in sent)

                # A5, B10, C11 sell: five seats down to two.
                sale = cycle(3)
                # B10 and C11 come back: two ordinary seats freed at once.
                pair = cycle(1)
                pair_message = next(text for _chat, text in sent)
                # A5 comes back: the non-A count does not move.
                row_a = cycle(0)

            # Two seats left and both are sweet ones, so every audience
            # qualifies: the alert reports what is on sale, not what moved.
            self.assertEqual(sale, ["2매이상", "기본", "명당"])
            self.assertEqual(pair, ["2매이상", "기본", "명당"])
            self.assertEqual(row_a, ["2매이상", "기본", "명당"])
            self.assertIn("잔여좌석/총좌석: 4/624석 (이전 2/624석)", pair_message)
            self.assertIn("A열 제외 예매 가능: 2석 → 4석", pair_message)
            self.assertIn("A열 제외 잔여 좌석: B10 / C11 / J20~21", pair_message)

    def test_a_sold_out_new_showing_is_still_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher(temporary)
                seat_requests, sent = self._wire(watcher, {"n": len(self.SEATS)})
                result = watcher.run_cycle()

            self.assertEqual(seat_requests, [])
            self.assertEqual(sent, [])
            self.assertEqual(result.new_sessions, 0)
            self.assertEqual(result.suppressed_sold_out, 1)


class ForcedSeatRecheckTests(unittest.TestCase):
    """Seat maps re-read even when the schedule total did not move."""

    TODAY = dt.date(2026, 8, 26)

    def _watcher(self, temporary, **overrides):
        config = dataclasses.replace(make_config(Path(temporary)), **overrides)
        logger = logging.getLogger(f"forced-recheck-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        return Watcher(config, logger=logger)

    def _forced_offsets(self, watcher, cycle, window=14):
        watcher._cycle_index = cycle
        return [
            offset
            for offset in range(window)
            if watcher._forces_seat_recheck(
                self.TODAY + dt.timedelta(days=offset), self.TODAY
            )
        ]

    def test_nearest_days_are_rechecked_every_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)

            for cycle in range(10):
                self.assertEqual(
                    self._forced_offsets(watcher, cycle)[:2], [0, 1]
                )

    def test_rotation_covers_every_date_once_per_full_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)
            cycles = watcher.config.seat_recheck_rotate_cycles

            rotated = [
                offset
                for cycle in range(cycles)
                for offset in self._forced_offsets(watcher, cycle)
                if offset >= watcher.config.seat_recheck_always_days
            ]

            # One slice per cycle, and the whole rotation window exactly once.
            self.assertEqual(sorted(rotated), [2, 3, 4, 5, 6])

    def test_dates_beyond_the_rotation_window_are_never_forced(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)

            forced = {
                offset
                for cycle in range(20)
                for offset in self._forced_offsets(watcher, cycle)
            }

            self.assertEqual(max(forced), watcher.config.seat_recheck_rotate_days - 1)

    def test_past_show_dates_are_never_forced(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)

            for cycle in range(10):
                watcher._cycle_index = cycle
                self.assertFalse(
                    watcher._forces_seat_recheck(
                        self.TODAY - dt.timedelta(days=1), self.TODAY
                    )
                )

    def test_zero_length_windows_disable_the_forced_recheck(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(
                temporary,
                seat_recheck_always_days=0,
                seat_recheck_rotate_days=0,
            )

            for cycle in range(10):
                self.assertEqual(self._forced_offsets(watcher, cycle), [])

    def _watcher_for_composition_change(self, temporary, **overrides):
        config = dataclasses.replace(
            make_config(Path(temporary)),
            target_start=self.TODAY,
            target_end=self.TODAY,
            **overrides,
        )
        logger = logging.getLogger(f"forced-recheck-run-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        return Watcher(config, logger=logger)

    @staticmethod
    def _snapshot(usable):
        # Nine seats stay on sale; only the A-row share of them moves.
        return SeatSnapshot(
            total=9,
            usable=usable,
            mapped_total=9,
            available_rows=("A", "H"),
            available_seats=tuple(("H", str(i)) for i in range(1, usable + 1)),
        )

    def _run_composition_change(self, watcher):
        """Announce a showing, then move seats between A and H rows."""

        watcher.cgv.fetch_date = lambda _date: {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": self.TODAY.strftime("%Y%m%d"),
                    "scnsrtTm": "2330",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": 9,
                    "stcnt": 200,
                }
            ]
        }
        usable = {"count": 7}
        seat_requests = []

        def fetch_seat(_session):
            seat_requests.append(usable["count"])
            return self._snapshot(usable["count"])

        watcher.cgv.fetch_seat_snapshot = fetch_seat
        alerts = []
        watcher.telegram.send_message = lambda text, **_kwargs: alerts.append(text)

        watcher.run_cycle()  # booking-open alert, no seat request
        watcher.run_cycle()  # baseline seat map
        alerts.clear()
        seat_requests.clear()
        # The total stays at 9: an A-row seat sells while an H-row seat frees.
        usable["count"] = 8
        watcher.run_cycle()
        return seat_requests, alerts

    def test_composition_change_under_an_unchanged_total_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher_for_composition_change(temporary)
                seat_requests, alerts = self._run_composition_change(watcher)

            self.assertEqual(seat_requests, [8])
            self.assertEqual(len(alerts), 1)
            self.assertIn("A열 제외 예매 가능: 7석 → 8석", alerts[0])

    def test_composition_change_is_missed_without_the_forced_recheck(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Config, "local_now", return_value=_kst(self.TODAY)
            ):
                watcher = self._watcher_for_composition_change(
                    temporary,
                    seat_recheck_always_days=0,
                    seat_recheck_rotate_days=0,
                )
                seat_requests, alerts = self._run_composition_change(watcher)

            # Documents why the forced re-check exists: the schedule total is
            # identical, so nothing asks CGV for the seat map again and the
            # alert that does go out still carries the stale seat list.
            self.assertEqual(seat_requests, [])
            self.assertEqual(len(alerts), 1)
            self.assertIn("A열 제외 예매 가능: 7석", alerts[0])


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
    def setUp(self):
        # Alert suppression now depends on the wall clock, so pin it well
        # before every show time these tests use.
        patcher = patch.object(
            Config, "local_now", return_value=_kst(dt.date(2026, 8, 13))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_failed_schedule_date_is_saved_and_cleared_after_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-schedule-retry-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.telegram.send_message = lambda *_args, **_kwargs: None
            failed_date = dt.date(2026, 8, 26)

            watcher.cgv.fetch_date = lambda _date: (_ for _ in ()).throw(
                FetchError("일시적인 일정 오류")
            )
            first = watcher.run_cycle()

            self.assertEqual(first.failed_dates, 1)
            persisted = StateStore(config.state_file)
            persisted.load()
            self.assertEqual(persisted.failed_schedule_dates(), (failed_date,))

            watcher.cgv.fetch_date = lambda _date: {"data": []}
            second = watcher.run_cycle()

            self.assertEqual(second.failed_dates, 0)
            self.assertEqual(watcher.state.failed_schedule_dates(), ())

    def test_stops_remaining_seat_requests_after_first_rate_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-seat-rate-limit-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.telegram.send_message = lambda *_args, **_kwargs: None
            remaining = {"count": 2}
            watcher.cgv.fetch_date = lambda _date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": "20260826",
                        "scnsrtTm": start_time,
                        "scnsNo": "13",
                        "scnSseq": sequence,
                        "frSeatCnt": remaining["count"],
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

            # Booking-open alerts make no seat requests, so the announcement
            # and the baseline read have to happen before the rate limit is
            # what the cycle runs into.
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                usable=session.remaining_seats,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            watcher.run_cycle()
            watcher.run_cycle()
            remaining["count"] = 3
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
                usable=session.remaining_seats - 2,
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
            self.assertIn("예매 가능 좌석", sent[0][1])

    def test_sweet_selection_filters_changes_but_not_new_openings(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-sweet-routing-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.state.remove_subscriber(config.telegram_chat_id)
            watcher.state.add_subscriber("all")
            watcher.state.add_subscriber("sweet")
            watcher.state.set_seat_selection("sweet", SEAT_SELECTION_SWEET)
            seats = {("B", "1"), ("B", "5")}

            watcher.cgv.fetch_date = lambda _date: self._schedule_payload(len(seats))

            def snapshot(_session):
                locations = tuple(sorted(seats))
                return SeatSnapshot(
                    total=len(locations),
                    usable=len(locations),
                    mapped_total=len(locations),
                    available_rows=tuple(
                        sorted({row for row, _number in locations})
                    ),
                    available_seats=locations,
                )

            watcher.cgv.fetch_seat_snapshot = snapshot
            sent = []
            watcher.telegram.send_message = lambda text, **kwargs: sent.append(
                (kwargs.get("chat_id"), text)
            )

            first = watcher.run_cycle()
            self.assertEqual(first.new_sessions, 1)
            self.assertEqual({chat_id for chat_id, _text in sent}, {"all", "sweet"})
            self.assertTrue(all("예매 오픈 감지" in text for _chat_id, text in sent))

            # The open alert went out without reading seats, so the next
            # cycle is the first to know which seats these are. None of them
            # is a sweet seat, so only the whole-auditorium subscriber hears.
            sent.clear()
            watcher.run_cycle()
            self.assertEqual([chat_id for chat_id, _text in sent], ["all"])

            sent.clear()
            seats.add(("B", "2"))
            second = watcher.run_cycle()
            self.assertEqual(second.seat_changes, 1)
            self.assertEqual([chat_id for chat_id, _text in sent], ["all"])

            sent.clear()
            seats.add(("K", "21"))
            third = watcher.run_cycle()
            self.assertEqual(third.seat_changes, 1)
            self.assertEqual({chat_id for chat_id, _text in sent}, {"all", "sweet"})
            sweet_text = next(text for chat_id, text in sent if chat_id == "sweet")
            self.assertIn("명당 예매 가능: 0석 → 1석", sweet_text)
            self.assertIn("명당 잔여 좌석: K21", sweet_text)
            self.assertNotIn("B1", sweet_text)

    def test_partial_open_delivery_retries_only_the_failed_subscriber(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Throttle repeats so this exercises the retry queue rather than
            # the every-cycle availability alert.
            config = dataclasses.replace(
                make_config(Path(temporary)), seat_alert_repeat_minutes=60
            )
            logger = logging.getLogger(f"watcher-partial-send-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.state.remove_subscriber(config.telegram_chat_id)
            watcher.state.add_subscriber("ok")
            watcher.state.add_subscriber("retry")
            watcher.cgv.fetch_date = lambda _date: self._schedule_payload(10)
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                usable=session.remaining_seats,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
                available_seats=tuple(
                    ("B", str(number))
                    for number in range(1, session.remaining_seats + 1)
                ),
            )

            attempts = []
            successful = []

            def first_send(text, **kwargs):
                chat_id = kwargs.get("chat_id")
                attempts.append(chat_id)
                if chat_id == "retry":
                    raise TelegramError("일시적인 테스트 오류")
                successful.append(chat_id)

            watcher.telegram.send_message = first_send
            first = watcher.run_cycle()
            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(successful, ["ok"])
            self.assertEqual(len(watcher.state.pending_deliveries()), 1)

            # The failed-recipient queue survives a Railway restart.  The
            # showing sells out meanwhile, so nothing new competes with the
            # retry for this cycle's sends.
            restarted = Watcher(config, logger=logger)
            restarted.cgv.fetch_date = lambda _date: self._schedule_payload(0)
            restarted.cgv.fetch_seat_snapshot = watcher.cgv.fetch_seat_snapshot
            self.assertEqual(len(restarted.state.pending_deliveries()), 1)

            def recovered_send(text, **kwargs):
                chat_id = kwargs.get("chat_id")
                attempts.append(chat_id)
                successful.append(chat_id)

            restarted.telegram.send_message = recovered_send
            second = restarted.run_cycle()

            self.assertEqual(second.new_sessions, 0)
            self.assertEqual(attempts.count("ok"), 1)
            self.assertEqual(attempts.count("retry"), 2)
            self.assertEqual(successful, ["ok", "retry"])
            self.assertEqual(restarted.state.pending_deliveries(), ())

    def test_broadcasts_to_multiple_subscribers_concurrently(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-parallel-send-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.state.remove_subscriber(config.telegram_chat_id)
            for chat_id in ("1", "2", "3", "4"):
                watcher.state.add_subscriber(chat_id)

            lock = threading.Lock()
            release = threading.Event()
            active = 0
            max_active = 0

            def blocked_send(_text, **_kwargs):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active >= 2:
                        release.set()
                self.assertTrue(release.wait(timeout=1))
                with lock:
                    active -= 1

            watcher.telegram.send_message = blocked_send
            delivered, failed, total = watcher._broadcast_message(
                "test", category=ALERT_OPEN
            )

            self.assertGreaterEqual(max_active, 2)
            self.assertEqual((delivered, failed, total), (4, 0, 4))

    def test_partial_seat_delivery_retries_without_repeating_for_others(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), seat_alert_repeat_minutes=60
            )
            logger = logging.getLogger(f"watcher-partial-seat-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            watcher.state.remove_subscriber(config.telegram_chat_id)
            watcher.state.add_subscriber("ok")
            watcher.state.add_subscriber("retry")
            remaining = {"count": 10}
            watcher.cgv.fetch_date = lambda _date: self._schedule_payload(
                remaining["count"]
            )
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                usable=session.remaining_seats,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            watcher.telegram.send_message = lambda text, **kwargs: None
            watcher.run_cycle()

            attempts = []

            def partial_send(text, **kwargs):
                chat_id = kwargs.get("chat_id")
                attempts.append(chat_id)
                if chat_id == "retry":
                    raise TelegramError("일시적인 테스트 오류")

            remaining["count"] = 9
            watcher.telegram.send_message = partial_send
            changed = watcher.run_cycle()
            self.assertEqual(changed.seat_changes, 1)
            self.assertEqual(len(watcher.state.pending_deliveries()), 1)

            watcher.telegram.send_message = lambda text, **kwargs: attempts.append(
                kwargs.get("chat_id")
            )
            unchanged = watcher.run_cycle()

            self.assertEqual(unchanged.seat_changes, 0)
            self.assertEqual(attempts.count("ok"), 1)
            self.assertEqual(attempts.count("retry"), 2)
            self.assertEqual(watcher.state.pending_deliveries(), ())

    def test_open_alert_is_marked_notified_even_with_no_open_subscribers(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher_with_three_alert_modes(temporary, "no-open-subs")
            watcher.state.remove_subscriber("1")
            watcher.state.remove_subscriber("3")
            watcher.cgv.fetch_date = lambda _date: self._schedule_payload(10)
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                usable=session.remaining_seats - 2,
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
            # The seats-only subscriber hears about the seats on sale, but
            # never about the opening itself.
            self.assertEqual([chat for chat, _text in sent], ["2"])
            self.assertIn("예매 가능 좌석", sent[0][1])

    def _watcher_with_two_seat_audiences(self, temporary, name):
        """Two seat subscribers: 'open-info' wants every seat, 'strict' only sweet ones."""

        config = make_config(Path(temporary))
        logger = logging.getLogger(f"watcher-{name}-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.state.remove_subscriber(config.telegram_chat_id)
        for chat_id in ("open-info", "strict"):
            watcher.state.add_subscriber(chat_id)
        watcher.state.set_seat_selection("strict", SEAT_SELECTION_SWEET)
        return watcher

    def test_unclassified_seat_change_is_recorded_with_no_recipients(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher_with_two_seat_audiences(temporary, "strict-only")
            watcher.state.remove_subscriber("open-info")
            remaining = {"count": 10}
            watcher.cgv.fetch_date = lambda _date: self._schedule_payload(
                remaining["count"]
            )
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats
            )
            sent = []
            watcher.telegram.send_message = lambda text, **kwargs: sent.append(
                (kwargs.get("chat_id"), text)
            )

            watcher.run_cycle()
            sent.clear()
            remaining["count"] = 9
            second = watcher.run_cycle()
            third = watcher.run_cycle()

            # Nobody wants the alert, but the snapshot must still advance so the
            # same change is not re-evaluated every cycle forever.
            self.assertEqual(second.seat_changes, 1)
            self.assertEqual(third.seat_changes, 0)
            self.assertEqual(sent, [])

    def test_seat_selection_commands_change_and_report_sweet_preference(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-seats-cmd-{id(self)}")
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
                batch(1, "/seat_sweet", chat_id=555),
                batch(2, "/start"),
                batch(3, "/seat_sweet@YongsanBot"),
                batch(4, "/status"),
                batch(5, "/seat_all"),
                batch(6, "/seat_all"),
                batch(7, "/seat"),
            ]
            watcher.telegram.get_updates = lambda **_kwargs: batches.pop(0)
            watcher.telegram.send_message = lambda text, **kwargs: replies.append(
                (kwargs.get("chat_id"), text)
            )

            watcher.sync_subscribers()
            self.assertIn("구독 중이 아닙니다", replies[-1][1])

            watcher.sync_subscribers()
            self.assertEqual(
                watcher.state.seat_selection("111222"), SEAT_SELECTION_ALL
            )

            watcher.sync_subscribers()
            self.assertEqual(
                watcher.state.seat_selection("111222"), SEAT_SELECTION_SWEET
            )
            self.assertIn("명당 좌석만", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("잔여 좌석 대상: 명당 좌석만", replies[-1][1])

            watcher.sync_subscribers()
            self.assertEqual(
                watcher.state.seat_selection("111222"), SEAT_SELECTION_ALL
            )

            watcher.sync_subscribers()
            self.assertIn("이미 이렇게 설정", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("현재 잔여 좌석 대상", replies[-1][1])
            self.assertEqual(
                watcher.state.seat_selection("111222"), SEAT_SELECTION_ALL
            )

            reloaded = StateStore(config.state_file)
            reloaded.load()
            self.assertEqual(
                reloaded.seat_selection("111222"), SEAT_SELECTION_ALL
            )

    def _cursor_watcher(self, temporary, name, **overrides):
        settings = {
            "dynamic_date_window": True,
            "target_window_days": 28,
            "scan_mode": SCAN_MODE_CURSOR,
            **overrides,
        }
        config = dataclasses.replace(make_config(Path(temporary)), **settings)
        logger = logging.getLogger(f"cursor-{name}-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.telegram.send_message = lambda text, **_kwargs: None
        watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
            total=session.remaining_seats,
            usable=session.remaining_seats - 2,
            mapped_total=session.remaining_seats,
            available_rows=("B",),
        )
        return watcher

    @staticmethod
    def _dated_payload(show_date):
        return {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": show_date.strftime("%Y%m%d"),
                    "scnsrtTm": "1430",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": 100,
                    "stcnt": 200,
                }
            ]
        }

    def test_cursor_scan_narrows_requests_and_expands_on_a_new_opening(self):
        today = dt.date(2026, 8, 13)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=_kst(today)):
                watcher = self._cursor_watcher(temporary, "expansion")
                open_through = {"date": dt.date(2026, 8, 20)}
                requested = []

                def fetch(show_date):
                    requested.append(show_date)
                    if show_date <= open_through["date"]:
                        return self._dated_payload(show_date)
                    return {"data": []}

                watcher.cgv.fetch_date = fetch

                first = watcher.run_cycle()
                self.assertTrue(first.full_scan)
                self.assertEqual(first.requested_dates, 28)
                self.assertEqual(
                    watcher.state.frontier_date, dt.date(2026, 8, 20)
                )

                # Nothing new opened: the cursor asks for the open range plus a
                # three-day probe only.
                requested.clear()
                second = watcher.run_cycle()
                self.assertFalse(second.full_scan)
                self.assertEqual(second.requested_dates, 11)
                self.assertEqual(
                    requested[:3],
                    [
                        dt.date(2026, 8, 21),
                        dt.date(2026, 8, 22),
                        dt.date(2026, 8, 23),
                    ],
                )
                self.assertEqual(
                    requested[3:],
                    [
                        dt.date(2026, 8, 13),
                        dt.date(2026, 8, 14),
                        dt.date(2026, 8, 15),
                        dt.date(2026, 8, 16),
                        dt.date(2026, 8, 17),
                        dt.date(2026, 8, 18),
                        dt.date(2026, 8, 19),
                        dt.date(2026, 8, 20),
                    ],
                )

                # CGV opens further ahead; the probe catches it and the cycle
                # widens to find the new end of the open range.
                open_through["date"] = dt.date(2026, 8, 30)
                requested.clear()
                third = watcher.run_cycle()

                self.assertFalse(third.full_scan)
                self.assertGreater(third.requested_dates, 11)
                self.assertIn(dt.date(2026, 8, 30), requested)
                self.assertLess(
                    requested.index(dt.date(2026, 8, 24)),
                    requested.index(today),
                    "확장 신규 오픈 날짜가 기존 취소표 날짜보다 먼저여야 합니다",
                )
                self.assertEqual(
                    watcher.state.frontier_date, dt.date(2026, 8, 30)
                )

    def test_cursor_scan_detects_the_new_sessions_it_probes(self):
        today = dt.date(2026, 8, 13)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=_kst(today)):
                watcher = self._cursor_watcher(temporary, "detect")
                open_through = {"date": dt.date(2026, 8, 20)}
                watcher.cgv.fetch_date = lambda show_date: (
                    self._dated_payload(show_date)
                    if show_date <= open_through["date"]
                    else {"data": []}
                )

                watcher.run_cycle()
                open_through["date"] = dt.date(2026, 8, 24)
                result = watcher.run_cycle()

                # 08-21 through 08-24 opened; all four must be reported.
                self.assertEqual(result.new_sessions, 4)

    def test_rate_limited_partial_scan_does_not_regress_the_frontier(self):
        today = dt.date(2026, 8, 13)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=_kst(today)):
                watcher = self._cursor_watcher(temporary, "ratelimit")
                watcher.cgv.fetch_date = lambda show_date: (
                    self._dated_payload(show_date)
                    if show_date <= dt.date(2026, 8, 20)
                    else {"data": []}
                )
                watcher.run_cycle()
                self.assertEqual(
                    watcher.state.frontier_date, dt.date(2026, 8, 20)
                )

                # Every later request fails, so this cycle observes only 08-14.
                def limited(show_date):
                    if show_date > dt.date(2026, 8, 14):
                        raise FetchError("CGV 응답 오류: HTTP 429")
                    return self._dated_payload(show_date)

                watcher.cgv.fetch_date = limited
                result = watcher.run_cycle()

                self.assertGreater(result.rate_limited_requests, 0)
                self.assertEqual(
                    watcher.state.frontier_date, dt.date(2026, 8, 20)
                )

    def test_full_scan_mode_keeps_requesting_every_date(self):
        today = dt.date(2026, 8, 13)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=_kst(today)):
                watcher = self._cursor_watcher(
                    temporary, "fullmode", scan_mode=SCAN_MODE_FULL
                )
                watcher.cgv.fetch_date = lambda show_date: (
                    self._dated_payload(show_date)
                    if show_date <= dt.date(2026, 8, 20)
                    else {"data": []}
                )

                for _ in range(3):
                    result = watcher.run_cycle()
                    self.assertTrue(result.full_scan)
                    self.assertEqual(result.requested_dates, 28)

    def test_frontier_survives_a_restart_and_keeps_the_cursor_narrow(self):
        today = dt.date(2026, 8, 13)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=_kst(today)):
                watcher = self._cursor_watcher(temporary, "restart")
                watcher.cgv.fetch_date = lambda show_date: (
                    self._dated_payload(show_date)
                    if show_date <= dt.date(2026, 8, 20)
                    else {"data": []}
                )
                watcher.run_cycle()

                # A fresh process reads the frontier back from the state file,
                # so only the first cycle after a restart sweeps the window.
                restarted = self._cursor_watcher(temporary, "restart-2")
                restarted.cgv.fetch_date = watcher.cgv.fetch_date
                restarted._cycle_index = 1

                plan = restarted._plan_scan(today)
                self.assertFalse(plan.full_scan)
                self.assertEqual(len(plan.dates), 11)

    def _watcher_for_showtime(self, temporary, name, start_time, **overrides):
        """A watcher whose only session starts at ``start_time`` on 2026-08-13."""

        settings = {
            "dynamic_date_window": False,
            "target_start": dt.date(2026, 8, 13),
            "target_end": dt.date(2026, 8, 13),
            **overrides,
        }
        config = dataclasses.replace(make_config(Path(temporary)), **settings)
        logger = logging.getLogger(f"closed-{name}-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        remaining = {"count": 100}
        watcher.cgv.fetch_date = lambda _date: {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": "20260813",
                    "scnsrtTm": start_time,
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": remaining["count"],
                    "stcnt": 200,
                }
            ]
        }
        watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
            total=session.remaining_seats,
            usable=session.remaining_seats - 2,
            mapped_total=session.remaining_seats,
            available_rows=("B",),
        )
        return watcher, remaining

    def test_seat_change_on_a_started_showing_is_not_alerted(self):
        now = _kst(dt.date(2026, 8, 13), hour=16)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=now):
                # The 14:30 showing already started; CGV still reports it.
                watcher, remaining = self._watcher_for_showtime(
                    temporary, "past", "1430"
                )
                sent = []
                watcher.telegram.send_message = lambda text, **_k: sent.append(text)

                first = watcher.run_cycle()
                remaining["count"] = 99
                second = watcher.run_cycle()

                self.assertEqual(first.new_sessions, 0)
                self.assertEqual(first.suppressed_closed, 1)
                self.assertEqual(second.seat_changes, 0)
                self.assertEqual(sent, [])

    def test_seat_change_on_an_upcoming_showing_is_still_alerted(self):
        now = _kst(dt.date(2026, 8, 13), hour=16)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=now):
                watcher, remaining = self._watcher_for_showtime(
                    temporary, "future", "1900"
                )
                sent = []
                watcher.telegram.send_message = lambda text, **_k: sent.append(text)

                first = watcher.run_cycle()
                remaining["count"] = 99
                second = watcher.run_cycle()

                self.assertEqual(first.new_sessions, 1)
                self.assertEqual(first.suppressed_closed, 0)
                self.assertEqual(second.seat_changes, 1)
                self.assertEqual(len(sent), 2)

    def test_booking_close_margin_suppresses_showings_about_to_start(self):
        now = _kst(dt.date(2026, 8, 13), hour=16)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=now):
                # 16:20 is still ahead, but within a 30 minute close margin.
                watcher, _ = self._watcher_for_showtime(
                    temporary,
                    "margin",
                    "1620",
                    booking_close_margin_minutes=30,
                )
                watcher.telegram.send_message = lambda text, **_k: None

                result = watcher.run_cycle()

                self.assertEqual(result.suppressed_closed, 1)
                self.assertEqual(result.new_sessions, 0)

    def test_showing_starting_after_the_margin_is_kept(self):
        now = _kst(dt.date(2026, 8, 13), hour=16)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=now):
                watcher, _ = self._watcher_for_showtime(
                    temporary,
                    "margin-keep",
                    "1640",
                    booking_close_margin_minutes=30,
                )
                watcher.telegram.send_message = lambda text, **_k: None

                result = watcher.run_cycle()

                self.assertEqual(result.suppressed_closed, 0)
                self.assertEqual(result.new_sessions, 1)

    def test_session_without_a_usable_start_time_is_never_suppressed(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"closed-notime-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            session = BookingSession(date="2026-08-13", start_time="")

            bookable, closed = watcher._split_closed_sessions([session])

            self.assertEqual(bookable, [session])
            self.assertEqual(closed, [])

    def test_alerts_are_sent_during_the_scan_not_after_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)),
                dynamic_date_window=True,
                target_window_days=3,
            )
            logger = logging.getLogger(f"watcher-interleave-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)

            events = []

            def fetch(show_date):
                events.append(("fetch", show_date.isoformat()))
                return {
                    "data": [
                        {
                            "scnsNm": "IMAX관",
                            "scnYmd": show_date.strftime("%Y%m%d"),
                            "scnsrtTm": "2200",
                            "scnsNo": "13",
                            "scnSseq": "4",
                            "frSeatCnt": 100,
                            "stcnt": 200,
                        }
                    ]
                }

            watcher.cgv.fetch_date = fetch
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                usable=session.remaining_seats - 2,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            watcher.telegram.send_message = lambda text, **_kwargs: events.append(
                ("send", text)
            )

            result = watcher.run_cycle()

            self.assertEqual(result.new_sessions, 3)
            kinds = [kind for kind, _ in events]
            last_fetch = len(kinds) - 1 - kinds[::-1].index("fetch")
            first_send = kinds.index("send")
            # The delay between reading a seat count and sending it is only
            # small because the first date's alert goes out before the last
            # date is even requested.
            self.assertLess(first_send, last_fetch)
            self.assertEqual(
                events[0], ("fetch", dt.date(2026, 8, 13).isoformat())
            )
            self.assertEqual(events[1][0], "send")

    def test_new_open_alert_precedes_seat_change_on_the_same_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)),
                dynamic_date_window=True,
                target_window_days=1,
            )
            logger = logging.getLogger(f"watcher-open-priority-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            phase = {"new_open": False}

            def fetch(show_date):
                sessions = [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": show_date.strftime("%Y%m%d"),
                        "scnsrtTm": "1900",
                        "scnsNo": "13",
                        "scnSseq": "1",
                        "frSeatCnt": 9 if phase["new_open"] else 10,
                        "stcnt": 200,
                    }
                ]
                if phase["new_open"]:
                    sessions.append(
                        {
                            "scnsNm": "IMAX관",
                            "scnYmd": show_date.strftime("%Y%m%d"),
                            "scnsrtTm": "2000",
                            "scnsNo": "13",
                            "scnSseq": "2",
                            "frSeatCnt": 100,
                            "stcnt": 200,
                        }
                    )
                return {"data": sessions}

            watcher.cgv.fetch_date = fetch
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                usable=session.remaining_seats,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            watcher.telegram.send_message = lambda text, **_kwargs: None
            watcher.run_cycle()

            sent = []
            phase["new_open"] = True
            watcher.telegram.send_message = lambda text, **_kwargs: sent.append(text)
            result = watcher.run_cycle()

            self.assertEqual(result.new_sessions, 1)
            self.assertEqual(result.seat_changes, 1)
            self.assertEqual(len(sent), 2)
            self.assertIn("예매 오픈 감지", sent[0])
            self.assertIn("예매 가능 좌석", sent[1])

    def test_seat_detail_runs_before_the_next_date_is_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)),
                target_start=dt.date(2026, 8, 26),
                target_end=dt.date(2026, 8, 27),
                scan_mode=SCAN_MODE_FULL,
            )
            logger = logging.getLogger(f"watcher-discovery-first-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            existing = BookingSession(
                date="2026-08-26",
                start_time="10:00",
                screen_name="IMAX관",
                screen_no="13",
                screen_sequence="1",
                remaining_seats=101,
                total_seats=624,
            )
            existing_key = existing.notification_key(
                site_no=config.site_no, movie_no=config.movie_no
            )
            watcher.state.mark_notified(existing_key, existing)
            watcher.state.set_seat_snapshot(
                existing_key,
                SeatSnapshot(
                    total=101,
                    usable=101,
                    mapped_total=101,
                    available_rows=("B",),
                ),
            )

            events = []

            def fetch_date(show_date):
                events.append(f"schedule:{show_date.isoformat()}")
                if show_date == dt.date(2026, 8, 26):
                    return {
                        "data": [
                            {
                                "scnsNm": "IMAX관",
                                "scnYmd": "20260826",
                                "scnsrtTm": "1000",
                                "scnsNo": "13",
                                "scnSseq": "1",
                                "frSeatCnt": 100,
                                "stcnt": 624,
                            }
                        ]
                    }
                return {"data": []}

            watcher.cgv.fetch_date = fetch_date

            def fetch_seats(session):
                events.append(f"seats:{session.date}")
                return SeatSnapshot(
                    total=100,
                    usable=100,
                    mapped_total=100,
                    available_rows=("B",),
                )

            watcher.cgv.fetch_seat_snapshot = fetch_seats
            watcher._broadcast_message = lambda *_args, **_kwargs: (1, 0, 1)

            watcher.run_cycle()

            # A cancellation ticket is worth little by the time the rest of
            # the window has been swept, so each date closes out where it is
            # read rather than queueing its seat detail for the end.
            self.assertLess(
                events.index("seats:2026-08-26"),
                events.index("schedule:2026-08-27"),
            )

    def test_probe_dates_are_still_requested_before_anything_else(self):
        today = dt.date(2026, 8, 13)
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)),
                dynamic_date_window=True,
                target_window_days=28,
                scan_mode=SCAN_MODE_CURSOR,
            )
            logger = logging.getLogger(f"watcher-probe-first-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            with patch.object(Config, "local_now", return_value=_kst(today)):
                watcher = Watcher(config, logger=logger)
                watcher.state.advance_frontier(dt.date(2026, 8, 20))
                watcher._cycle_index = 1
                requested = []
                watcher.cgv.fetch_date = lambda show_date: (
                    requested.append(show_date), {"data": []}
                )[1]
                watcher.telegram.send_message = lambda text, **_kwargs: None

                watcher.run_cycle()

            # Seat detail moved inline, but discovering a new opening still
            # comes first: the three probe dates lead the cycle.
            self.assertEqual(
                requested[:3],
                [dt.date(2026, 8, 21), dt.date(2026, 8, 22), dt.date(2026, 8, 23)],
            )
            self.assertEqual(requested[3], today)

    def _cursor_walk_watcher(self, temporary, name, open_through):
        config = dataclasses.replace(
            make_config(Path(temporary)),
            dynamic_date_window=True,
            target_window_days=28,
            scan_mode=SCAN_MODE_CURSOR,
        )
        logger = logging.getLogger(f"walk-{name}-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.telegram.send_message = lambda text, **_kwargs: None
        watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
            total=600, usable=598, mapped_total=600, available_rows=("B",)
        )
        live = {open_through["start"] + dt.timedelta(days=i) for i in range(12)}
        open_through["live"] = live
        requested = []

        def fetch(show_date):
            requested.append(show_date)
            if show_date not in live:
                return {"data": []}
            return {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": show_date.strftime("%Y%m%d"),
                        "scnsrtTm": "2200",
                        "scnsNo": "13",
                        "scnSseq": "4",
                        "frSeatCnt": 600,
                        "stcnt": 624,
                    }
                ]
            }

        watcher.cgv.fetch_date = fetch
        return watcher, requested

    def test_a_new_opening_is_followed_only_to_its_last_date(self):
        today = dt.date(2026, 8, 14)
        frontier = dt.date(2026, 8, 25)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=_kst(today)):
                state = {"start": today}
                watcher, requested = self._cursor_walk_watcher(
                    temporary, "short", state
                )
                watcher.run_cycle()
                requested.clear()

                # Only four new dates open; the range ends at 8/29.
                for offset in range(4):
                    state["live"].add(dt.date(2026, 8, 26) + dt.timedelta(days=offset))
                result = watcher.run_cycle()

                ahead = [date for date in requested if date > frontier]
                # 8/26..8/29 have showings, 8/30 is the empty date that ends
                # the walk. Nothing beyond it is requested.
                self.assertEqual(ahead[-1], dt.date(2026, 8, 30))
                self.assertEqual(len(ahead), 5)
                self.assertEqual(result.new_sessions, 4)
                self.assertEqual(
                    watcher.state.frontier_date, dt.date(2026, 8, 29)
                )

    def test_no_opening_costs_only_the_probe(self):
        today = dt.date(2026, 8, 14)
        frontier = dt.date(2026, 8, 25)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=_kst(today)):
                state = {"start": today}
                watcher, requested = self._cursor_walk_watcher(
                    temporary, "quiet", state
                )
                watcher.run_cycle()
                requested.clear()

                watcher.run_cycle()

                ahead = [date for date in requested if date > frontier]
                self.assertEqual(len(ahead), 3)
                self.assertEqual(ahead[-1], dt.date(2026, 8, 28))

    def test_the_walk_returns_to_today_after_the_new_range_ends(self):
        today = dt.date(2026, 8, 14)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Config, "local_now", return_value=_kst(today)):
                state = {"start": today}
                watcher, requested = self._cursor_walk_watcher(
                    temporary, "back", state
                )
                watcher.run_cycle()
                requested.clear()
                for offset in range(4):
                    state["live"].add(dt.date(2026, 8, 26) + dt.timedelta(days=offset))

                watcher.run_cycle()

                # Today's cancellation tickets come straight after the walk,
                # not after a fixed three-week sweep.
                end_of_walk = requested.index(dt.date(2026, 8, 30))
                self.assertEqual(requested[end_of_walk + 1], today)

    def test_state_is_saved_as_each_date_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)),
                dynamic_date_window=True,
                target_window_days=3,
            )
            logger = logging.getLogger(f"watcher-flush-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            seen_on_disk = []

            def fetch(show_date):
                # What a crash at this moment would leave behind.
                reloaded = StateStore(config.state_file)
                reloaded.load()
                seen_on_disk.append(len(reloaded.data["notified"]))
                return {
                    "data": [
                        {
                            "scnsNm": "IMAX관",
                            "scnYmd": show_date.strftime("%Y%m%d"),
                            "scnsrtTm": "2200",
                            "scnsNo": "13",
                            "scnSseq": "4",
                            "frSeatCnt": 100,
                            "stcnt": 200,
                        }
                    ]
                }

            watcher.cgv.fetch_date = fetch
            watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
                total=session.remaining_seats,
                usable=session.remaining_seats - 2,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )
            watcher.telegram.send_message = lambda text, **_kwargs: None

            watcher.run_cycle()

            # Each date's alert is on disk before the next date is requested,
            # so a crash mid-cycle cannot resend what already went out.
            self.assertEqual(seen_on_disk, [0, 1, 2])

    def _run_cycle_capturing(self, level, *, times, now=None):
        """Run one cycle at the given log level and return the emitted records."""

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append((record.levelno, record.getMessage()))

        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)),
                dynamic_date_window=True,
                target_window_days=1,
            )
            logger = logging.getLogger(f"watcher-verdict-{level}-{id(self)}")
            logger.handlers = [Capture()]
            logger.setLevel(level)
            watcher = Watcher(config, logger=logger)
            watcher.telegram.send_message = lambda text, **_kwargs: None
            watcher.cgv.fetch_date = lambda show_date: {
                "data": [
                    {
                        "scnsNm": "IMAX관",
                        "scnYmd": show_date.strftime("%Y%m%d"),
                        "scnsrtTm": start,
                        "scnsNo": "13",
                        "scnSseq": str(index),
                        "frSeatCnt": remaining,
                        "stcnt": 624,
                    }
                    for index, (start, remaining) in enumerate(times)
                ]
            }
            watcher.cgv.fetch_seat_snapshot = lambda session: (
                SeatSnapshot(total=session.remaining_seats)
                if session.remaining_seats <= 6
                else SeatSnapshot(
                    total=session.remaining_seats,
                    usable=session.remaining_seats - 2,
                    mapped_total=session.remaining_seats,
                    available_rows=("B",),
                )
            )
            watcher.run_cycle()
        return records

    def test_verbose_runs_summarise_every_showing_in_one_line(self):
        now = _kst(dt.date(2026, 8, 13))
        with patch.object(Config, "local_now", return_value=now):
            records = self._run_cycle_capturing(
                logging.DEBUG,
                # already started, normal, nearly sold out
                times=[("0700", 400), ("1900", 412), ("2200", 5)],
            )

        verdicts = [msg for level, msg in records if msg.startswith("회차 판정")]
        self.assertEqual(len(verdicts), 1, "판정 요약은 주기당 한 줄이어야 합니다")
        line = verdicts[0]
        self.assertIn("회차 판정 3건", line)
        self.assertIn("07:00 400/624 제외·예매 마감", line)
        self.assertIn("19:00 412/624 발송·예매 오픈", line)
        # Five seats left used to be held back pending the row-A check; a
        # booking-open alert no longer waits for one.
        self.assertIn("22:00 5/624 발송·예매 오픈", line)

    def test_normal_runs_do_not_carry_the_per_showing_summary(self):
        now = _kst(dt.date(2026, 8, 13))
        with patch.object(Config, "local_now", return_value=now):
            records = self._run_cycle_capturing(
                logging.INFO, times=[("1900", 412), ("2200", 5)]
            )

        self.assertFalse([m for _l, m in records if m.startswith("회차 판정")])
        self.assertTrue([m for _l, m in records if m.startswith("조회 완료")])

    def _unreadable_seat_map_watcher(self, temporary, name, **overrides):
        """A watcher whose seat map is unreadable once a showing drops to ≤6."""

        settings = {
            "dynamic_date_window": True,
            "target_window_days": 1,
            **overrides,
        }
        config = dataclasses.replace(make_config(Path(temporary)), **settings)
        logger = logging.getLogger(f"defer-{name}-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.telegram.send_message = lambda text, **_kwargs: None
        remaining = {"count": 100}
        watcher.cgv.fetch_date = lambda show_date: {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": show_date.strftime("%Y%m%d"),
                    "scnsrtTm": "2200",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": remaining["count"],
                    "stcnt": 624,
                }
            ]
        }
        calls = []

        def seat(session):
            calls.append(session.remaining_seats)
            if session.remaining_seats <= 6:
                return SeatSnapshot(total=session.remaining_seats)
            return SeatSnapshot(
                total=session.remaining_seats,
                usable=session.remaining_seats - 2,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
            )

        watcher.cgv.fetch_seat_snapshot = seat
        return watcher, remaining, calls

    def test_an_announced_showing_rechecks_an_unreadable_map_on_a_backoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher, remaining, calls = self._unreadable_seat_map_watcher(
                temporary, "backoff", deferred_recheck_cycles=5
            )
            watcher.run_cycle()
            remaining["count"] = 5

            per_cycle = []
            for _ in range(10):
                before = len(calls)
                per_cycle.append(watcher.run_cycle())
                per_cycle[-1] = len(calls) - before

            # One read, then four cycles coasting, repeating.
            self.assertEqual(per_cycle, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0])

    def test_a_changed_seat_count_cancels_the_backoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher, remaining, calls = self._unreadable_seat_map_watcher(
                temporary, "changed", deferred_recheck_cycles=5
            )
            watcher.run_cycle()
            remaining["count"] = 5
            watcher.run_cycle()
            watcher.run_cycle()

            before = len(calls)
            remaining["count"] = 4
            watcher.run_cycle()

            # Something moved, so the next look happens immediately.
            self.assertEqual(len(calls) - before, 1)

    def test_an_unannounced_showing_is_sent_before_its_seats_are_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher, remaining, calls = self._unreadable_seat_map_watcher(
                temporary, "unannounced", deferred_recheck_cycles=5
            )
            remaining["count"] = 5
            sent = []
            watcher.telegram.send_message = lambda text, **_kwargs: sent.append(
                text
            )

            first = watcher.run_cycle()

            # Six seats or fewer used to be held back until the seat map could
            # rule out row A. The booking-open alert now goes out on the
            # schedule response alone, so no seat request happens at all.
            self.assertEqual(calls, [])
            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(len(sent), 1)
            self.assertIn("예매 오픈 감지", sent[0])

    def _watcher_with_failing_subscriber(self, temporary, name, error, **overrides):
        settings = {
            "dynamic_date_window": True,
            "target_window_days": 1,
            **overrides,
        }
        config = dataclasses.replace(make_config(Path(temporary)), **settings)
        logger = logging.getLogger(f"delivery-{name}-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.state.add_subscriber("999", chat_type="private")
        remaining = {"count": 100}

        def send(text, chat_id=None, **_kwargs):
            if str(chat_id) == "999":
                raise error

        watcher.telegram.send_message = send
        watcher.cgv.fetch_date = lambda show_date: {
            "data": [
                {
                    "scnsNm": "IMAX관",
                    "scnYmd": show_date.strftime("%Y%m%d"),
                    "scnsrtTm": "2200",
                    "scnsNo": "13",
                    "scnSseq": "4",
                    "frSeatCnt": remaining["count"],
                    "stcnt": 624,
                }
            ]
        }
        watcher.cgv.fetch_seat_snapshot = lambda session: SeatSnapshot(
            total=session.remaining_seats,
            usable=session.remaining_seats - 2,
            mapped_total=session.remaining_seats,
            available_rows=("B",),
        )
        return watcher, remaining

    def test_a_blocked_chat_is_unsubscribed_rather_than_retried(self):
        blocked = TelegramError(
            "Telegram 응답 오류: HTTP 403",
            status_code=403,
            description="Forbidden: bot was blocked by the user",
        )
        with tempfile.TemporaryDirectory() as temporary:
            watcher, remaining = self._watcher_with_failing_subscriber(
                temporary, "blocked", blocked
            )

            for index in range(3):
                remaining["count"] = 100 - index
                watcher.run_cycle()

            # A blocked chat cannot send /stop, so nothing else would ever
            # remove it and every future broadcast would keep paying for it.
            self.assertFalse(watcher.state.is_subscribed("999"))
            self.assertEqual(watcher.state.data["pending_deliveries"], {})

    def test_a_transient_failure_is_still_queued_for_retry(self):
        transient = TelegramError(
            "Telegram 응답 오류: HTTP 500",
            status_code=500,
            description="Internal Server Error",
        )
        with tempfile.TemporaryDirectory() as temporary:
            watcher, remaining = self._watcher_with_failing_subscriber(
                temporary, "transient", transient
            )
            remaining["count"] = 99
            watcher.run_cycle()

            self.assertTrue(watcher.state.is_subscribed("999"))
            self.assertEqual(len(watcher.state.data["pending_deliveries"]), 1)

    def test_a_retry_that_never_succeeds_is_given_up_on(self):
        transient = TelegramError(
            "Telegram 응답 오류: HTTP 500",
            status_code=500,
            description="Internal Server Error",
        )
        with tempfile.TemporaryDirectory() as temporary:
            watcher, remaining = self._watcher_with_failing_subscriber(
                temporary, "giveup", transient, pending_delivery_max_attempts=3
            )
            for index in range(3):
                remaining["count"] = 100 - index
                watcher.run_cycle()
            peak = len(watcher.state.data["pending_deliveries"])

            # Selling out stops new alerts, so the queue has to drain instead
            # of being retried forever.
            remaining["count"] = 0
            for _ in range(5):
                watcher.run_cycle()

            self.assertGreater(peak, 0)
            self.assertEqual(watcher.state.data["pending_deliveries"], {})

    def test_queued_retries_expire_after_the_ttl(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            now = dt.datetime(2026, 8, 13, 9, 0, tzinfo=dt.timezone.utc)
            store.queue_pending_delivery("1", "오래된 알림", ALERT_SEATS)
            key = next(iter(store.data["pending_deliveries"]))
            store.data["pending_deliveries"][key]["queued_at"] = (
                now - dt.timedelta(hours=PENDING_DELIVERY_TTL_HOURS + 1)
            ).isoformat()
            store.queue_pending_delivery("1", "최근 알림", ALERT_SEATS)

            removed = store.prune_pending_deliveries(now)

            self.assertEqual(removed, 1)
            self.assertEqual(len(store.data["pending_deliveries"]), 1)

    def test_recipient_gone_recognises_permanent_telegram_errors(self):
        gone = [
            TelegramError("x", status_code=403, description="Forbidden: bot was blocked by the user"),
            TelegramError("x", status_code=403, description="Forbidden: user is deactivated"),
            TelegramError("x", description="Bad Request: chat not found"),
            TelegramError("x", description="Forbidden: bot was kicked from the group chat"),
        ]
        transient = [
            TelegramError("x", status_code=500, description="Internal Server Error"),
            TelegramError("x", status_code=429, description="Too Many Requests"),
            TelegramError("Telegram 연결 실패: timed out"),
        ]
        for error in gone:
            self.assertTrue(error.recipient_gone, error.description)
        for error in transient:
            self.assertFalse(error.recipient_gone, error.description)

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

    def test_only_a_join_or_leave_logs_the_subscriber_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-subcount-log-{id(self)}")
            records = []

            class Capture(logging.Handler):
                def emit(self, record):
                    records.append(record.getMessage())

            logger.handlers = [Capture()]
            logger.setLevel(logging.INFO)
            watcher = Watcher(config, logger=logger)
            watcher.telegram.send_message = lambda text, **_kwargs: None

            def batch(update_id, text):
                return [
                    {
                        "update_id": update_id,
                        "message": {
                            "text": text,
                            "chat": {"id": 4242, "type": "private"},
                        },
                    }
                ]

            def counted(label):
                return [line for line in records if "구독자 변경" in line]

            batches = [
                batch(1, "/start"),
                batch(2, "/status"),
                batch(3, "/help"),
                batch(4, "/mode_open"),
                batch(5, "/start"),
                batch(6, "안녕하세요"),
                batch(7, "/stop"),
            ]
            watcher.telegram.get_updates = lambda **_kwargs: batches.pop(0)

            watcher.sync_subscribers()
            self.assertEqual(len(counted("join")), 1)

            # Reads, a settings change, a repeat /start and plain chatter all
            # advance the update offset but leave the roster alone.
            for _ in range(5):
                watcher.sync_subscribers()
            self.assertEqual(len(counted("quiet")), 1)
            self.assertEqual(watcher.state.telegram_update_offset, 7)

            watcher.sync_subscribers()
            self.assertEqual(len(counted("leave")), 2)
            self.assertFalse(watcher.state.is_subscribed("4242"))

    def test_stats_command_answers_the_operator_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-stats-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            for index in range(4):
                watcher.state.add_subscriber(
                    str(2000 + index), chat_type="private"
                )
            watcher.state.set_alert_mode("2000", ALERT_MODE_OPEN_ONLY)
            watcher.state.set_alert_mode("2001", ALERT_MODE_SEATS_ONLY)
            watcher.state.set_seat_selection("2002", SEAT_SELECTION_SWEET)
            watcher.state.set_seat_selection("2003", SEAT_SELECTION_SWEET)

            replies = []
            watcher.telegram.send_message = lambda text, **kwargs: replies.append(
                (str(kwargs.get("chat_id")), text)
            )

            def send(chat_id, text, update_id):
                watcher.telegram.get_updates = lambda **_kwargs: [
                    {
                        "update_id": update_id,
                        "message": {
                            "text": text,
                            "chat": {"id": chat_id, "type": "private"},
                        },
                    }
                ]
                watcher.sync_subscribers()

            send(int(config.telegram_chat_id), "/stats", 1)
            operator_reply = replies[-1][1]
            self.assertIn("전체 5명", operator_reply)
            self.assertIn("신규 오픈만 — 1명", operator_reply)
            self.assertIn("잔여 좌석만 — 1명", operator_reply)
            self.assertIn("모든 A열 제외 좌석 — 3명", operator_reply)
            self.assertIn("명당 좌석만 — 2명", operator_reply)

            # A subscriber gets the generic reply, so the command stays hidden.
            send(2000, "/stats", 2)
            self.assertIn("사용 가능한 명령어", replies[-1][1])
            self.assertNotIn("구독 현황", replies[-1][1])

            send(int(config.telegram_chat_id), "/help", 3)
            help_reply = replies[-1][1]
            self.assertNotIn("stats", help_reply)
            for command in (
                "/start -",
                "/stop -",
                "/status -",
                "/mode -",
                "/mode_all -",
                "/mode_open -",
                "/mode_seats -",
                "/seat -",
                "/seat_sweet -",
                "/seat_sweet -",
                "/desc -",
                "/coffee -",
                "/help -",
            ):
                self.assertIn(command, help_reply)

    def test_stats_counts_subscribers_stored_before_the_settings_existed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.data["subscribers"]["9"] = {"subscribed_at": "2026-01-01T00:00:00"}
            store.add_subscriber("10", chat_type="group")

            breakdown = store.subscriber_breakdown()

            self.assertEqual(breakdown["total"], 2)
            self.assertEqual(breakdown["modes"][ALERT_MODE_ALL], 2)
            self.assertEqual(breakdown["seat_selections"][SEAT_SELECTION_ALL], 2)
            self.assertEqual(breakdown["seat_selections"][SEAT_SELECTION_ALL], 2)
            self.assertEqual(breakdown["chat_types"]["group"], 1)
            self.assertEqual(breakdown["chat_types"]["unknown"], 1)

    def test_welcome_explains_optional_sweet_seat_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-welcome-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            replies = []
            watcher.telegram.send_message = lambda text, **_kwargs: replies.append(text)
            watcher.telegram.get_updates = lambda **_kwargs: [
                {
                    "update_id": 1,
                    "message": {
                        "text": "/start",
                        "chat": {"id": 777, "type": "private"},
                    },
                }
            ]

            watcher.sync_subscribers()
            welcome = replies[-1]

            self.assertIn("🔔 기본 설정", welcome)
            self.assertIn("신규 예매 오픈 + 예매 가능 좌석 알림", welcome)
            self.assertIn("잔여 좌석은 모든 A열 제외 좌석", welcome)
            self.assertIn("알림 종류 선택: /mode", welcome)
            self.assertIn("잔여 좌석 대상 선택: /seat", welcome)
            self.assertIn("명당 좌석만 받기: /seat_sweet", welcome)
            self.assertIn("신규 예매 오픈은 좌석 설정과 관계없이 항상", welcome)
            # Listing every setting made a finished subscription look unfinished.
            for command in ("/mode_all", "/mode_open", "/mode_seats", "/seat_all"):
                self.assertNotIn(command, welcome)
            self.assertLess(len(welcome.splitlines()), 18)

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
            self.assertIn("A열만 남은 경우", description)
            self.assertIn("⚙️ 기본 설정", description)
            self.assertIn("/mode — 현재 설정과 선택 방법 확인", description)
            self.assertIn(
                "/mode_all — 신규 오픈과 잔여 좌석 모두 받기", description
            )
            self.assertIn("/mode_open — 신규 예매 오픈만", description)
            self.assertIn("/mode_seats — 예매 가능 좌석만", description)
            self.assertIn("/seat_all — 모든 A열 제외 좌석 알림 (기본)", description)
            self.assertIn("/seat_sweet", description)
            self.assertIn("Extremer: F16~29, G16~29", description)
            self.assertIn("Experienced: H13~32, I13~32", description)
            self.assertIn("SweetSpot: J11~34, K11~34, L11~34", description)
            # The verified/unverified split is gone; one seat choice remains.
            self.assertNotIn("/seat_verified", description)
            self.assertNotIn("/seat_default", description)
            self.assertIn("신규 예매 오픈 알림은 좌석 설정과 관계없이 항상", description)
            self.assertIn("/start — 알림 구독", description)
            self.assertIn("명당만 원하면 /seat_sweet", description)
            self.assertIn("예매 바로가기 링크 열기", description)
            self.assertIn("/stop — 알림 해지", description)
            self.assertIn("/status — 현재 구독 및 설정 확인", description)
            self.assertIn("/desc — 봇 설명과 사용 방법", description)
            self.assertIn("/coffee — 개발자에게 커피 후원", description)
            self.assertIn("/help — 전체 명령어 보기", description)
            self.assertLess(len(description), 4096)

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
                usable=session.remaining_seats,
                mapped_total=session.remaining_seats,
                available_rows=("B",),
                available_seats=tuple(
                    ("B", str(number))
                    for number in range(1, session.remaining_seats + 1)
                ),
            )
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            first = watcher.run_cycle()
            second = watcher.run_cycle()

            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(second.new_sessions, 0)
            # One booking-open alert, then one availability alert once the
            # seat map is read. The opening itself is never repeated.
            self.assertEqual(len(sent_messages), 2)
            self.assertIn("예매 오픈 감지", sent_messages[0])
            self.assertIn("예매 가능 좌석", sent_messages[1])
            self.assertIn("📅 상영일: 2026-08-26 (수)", sent_messages[0])
            self.assertIn("━━━━━━━━━━━━━━━━━━━━", sent_messages[0])
            self.assertIn(
                "상영 시작시간 14:30 — 100/624석",
                sent_messages[0],
            )
            # No seat map is read for a booking-open alert, so it carries the
            # start time and the count and nothing about seat locations.
            self.assertNotIn("잔여 좌석:", sent_messages[0])
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

    def test_sends_one_alert_per_cycle_while_seats_are_on_sale(self):
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
                usable=session.remaining_seats - 2,
                mapped_total=session.remaining_seats,
                available_rows=("A", "B"),
                available_seats=tuple(
                    ("B", str(number))
                    for number in range(1, session.remaining_seats - 1)
                ),
            )
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            first = watcher.run_cycle()
            remaining["count"] = 9
            second = watcher.run_cycle()
            third = watcher.run_cycle()
            remaining["count"] = 0
            fourth = watcher.run_cycle()

            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(second.seat_changes, 1)
            # Still on sale, so it is announced again even though nothing
            # moved -- the point of the alert is the seat, not the change.
            self.assertEqual(third.seat_changes, 1)
            self.assertEqual(fourth.seat_changes, 0)
            self.assertEqual(len(sent_messages), 3)
            self.assertIn(
                "잔여좌석/총좌석: 9/200석 (이전 10/200석)", sent_messages[1]
            )
            # The repeat has no previous count to contrast with.
            self.assertIn("잔여좌석/총좌석: 9/200석\n", sent_messages[2])
            self.assertIn(
                "A열 제외 잔여 좌석: B1~7",
                sent_messages[1],
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

    def test_suppresses_change_when_only_a_row_seats_remain(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            logger = logging.getLogger(f"watcher-a-row-change-{id(self)}")
            logger.handlers = [logging.NullHandler()]
            watcher = Watcher(config, logger=logger)
            availability = {"total": 3, "usable": 1, "rows": ("A", "B")}

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
                usable=availability["usable"],
                mapped_total=availability["total"],
                available_rows=availability["rows"],
            )
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            watcher.run_cycle()
            availability.update(total=2, usable=0, rows=("A",))
            second = watcher.run_cycle()

            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(second.seat_changes, 0)
            self.assertEqual(second.suppressed_row_a_only, 1)

    def test_a_new_showing_is_sent_before_row_a_can_be_ruled_out(self):
        """Row A is not checked on a booking-open alert, by design.

        The check costs one request per showing, and a showing that has just
        opened has its whole auditorium free, so there is nothing to exclude.
        Only a restart, which makes every showing look new again, can reach a
        row-A-only showing here.
        """

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
            seat_calls = []

            def seat(_session):
                seat_calls.append(_session.start_time)
                return SeatSnapshot(
                    total=2, usable=0, mapped_total=2, available_rows=("A",)
                )

            watcher.cgv.fetch_seat_snapshot = seat
            sent_messages = []
            watcher.telegram.send_message = (
                lambda text, **_kwargs: sent_messages.append(text)
            )

            first = watcher.run_cycle()

            self.assertEqual(seat_calls, [])
            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(len(sent_messages), 1)

            # The very next cycle reads the map, and from then on the row-A
            # rule applies again: no further alert for this showing.
            second = watcher.run_cycle()

            self.assertEqual(seat_calls, ["14:30"])
            self.assertEqual(second.new_sessions, 0)
            self.assertEqual(len(sent_messages), 1)

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
                    usable=2,
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
            # A seat request that cannot succeed no longer delays the alert,
            # because the alert never waited for one.
            self.assertEqual(first.new_sessions, 1)
            self.assertEqual(first.seat_detail_errors, 0)
            self.assertTrue(watcher.state.was_notified(key))
            self.assertEqual(len(sent_messages), 1)

            # The baseline read is what now hits the failure, and it is the
            # one that gets put on the backoff.
            second = watcher.run_cycle()

            self.assertEqual(second.new_sessions, 0)
            self.assertEqual(second.seat_detail_errors, 1)
            self.assertIn(key, watcher.state.data["deferred"])
            self.assertEqual(len(sent_messages), 1)

            # Backoff in effect: the next cycles coast instead of re-asking.
            detail_available["value"] = True
            seat_calls_before = second.seat_detail_errors
            third = watcher.run_cycle()

            self.assertEqual(third.seat_detail_errors, 0)
            self.assertEqual(third.deferred_rechecks_skipped, 1)
            self.assertIsNone(watcher.state.seat_snapshot(key).usable)
            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(seat_calls_before, 1)

    def test_a_new_session_alerts_without_asking_for_seat_detail(self):
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
            # The failing seat request is never made, so it cannot be counted.
            self.assertEqual(result.seat_detail_errors, 0)
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("7/200석", sent_messages[0])
            # No seat map was read, so the alert claims nothing about row A —
            # neither a seat list nor the "unverified" warning that goes with
            # falling back to the schedule total.
            self.assertNotIn("A열", sent_messages[0])
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
                    usable=8,
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
            self.assertIn("A열 여부 미확인", sent_messages[1])

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
                usable=1,
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
    def test_reuses_one_https_connection_for_multiple_cgv_requests(self):
        class FakeResponse:
            status = 200

            def read(self, _limit):
                return b'{"data": []}'

            def getheader(self, name, default=""):
                return "application/json" if name == "Content-Type" else default

        class FakeConnection:
            def __init__(self):
                self.requests = 0

            def request(self, _method, _target, *, headers):
                self.requests += 1
                self.assert_no_login(headers)

            @staticmethod
            def assert_no_login(headers):
                lowered = {key.lower() for key in headers}
                if "authorization" in lowered or "cookie" in lowered:
                    raise AssertionError("login headers must not be sent")

            def getresponse(self):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as temporary:
            client = CgvClient(make_config(Path(temporary)))
            connection = FakeConnection()
            with patch(
                "watcher.http.client.HTTPSConnection", return_value=connection
            ) as created:
                client.fetch_date(dt.date(2026, 8, 26))
                client.fetch_date(dt.date(2026, 8, 27))

            self.assertEqual(created.call_count, 1)
            self.assertEqual(connection.requests, 2)

    def test_reconnects_once_when_a_pooled_connection_was_closed(self):
        class FakeResponse:
            status = 200

            def read(self, _limit):
                return b'{"data": []}'

            def getheader(self, name, default=""):
                return "application/json" if name == "Content-Type" else default

        class ClosedConnection:
            def request(self, *_args, **_kwargs):
                raise http.client.RemoteDisconnected("closed")

            def close(self):
                return None

        class HealthyConnection:
            def request(self, *_args, **_kwargs):
                return None

            def getresponse(self):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as temporary:
            client = CgvClient(make_config(Path(temporary)))
            with patch(
                "watcher.http.client.HTTPSConnection",
                side_effect=[ClosedConnection(), HealthyConnection()],
            ) as created:
                payload = client.fetch_date(dt.date(2026, 8, 26))

            self.assertEqual(payload, {"data": []})
            self.assertEqual(created.call_count, 2)

    def test_anonymous_request_has_expected_query_and_no_login_headers(self):
        class FakeResponse:
            status = 200

            def read(self, _limit):
                return b'{"data": []}'

            def getheader(self, name, default=""):
                return "application/json" if name == "Content-Type" else default

        class FakeConnection:
            def __init__(self):
                self.calls = []

            def request(self, method, target, *, headers):
                self.calls.append((method, target, headers))

            def getresponse(self):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            client = CgvClient(config)
            connection = FakeConnection()
            with patch.object(client, "_connection_for", return_value=connection):
                payload = client.fetch_date(dt.date(2026, 8, 26))

            _method, target, request_headers = connection.calls[0]
            headers = {key.lower(): value for key, value in request_headers.items()}
            query = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(target).query,
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

            def read(self, _limit):
                return b'{"data": {"items": []}}'

            def getheader(self, name, default=""):
                return "application/json" if name == "Content-Type" else default

        class FakeConnection:
            def __init__(self):
                self.calls = []

            def request(self, method, target, *, headers):
                self.calls.append((method, target, headers))

            def getresponse(self):
                return FakeResponse()

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
            connection = FakeConnection()
            with patch.object(client, "_connection_for", return_value=connection):
                snapshot = client.fetch_seat_snapshot(session)

            _method, target, request_headers = connection.calls[0]
            headers = {key.lower(): value for key, value in request_headers.items()}
            query = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(target).query,
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


class DocumentedCommandTests(unittest.TestCase):
    """Every command a subscriber can see must actually do something."""

    REPO = Path(__file__).resolve().parent.parent

    def _handled_commands(self) -> set[str]:
        source = (self.REPO / "watcher.py").read_text(encoding="utf-8")
        return set(re.findall(r'"(/[a-z0-9_]+)"', source))

    def test_botfather_list_only_registers_commands_that_exist(self):
        development = (self.REPO / "DEVELOPMENT.md").read_text(encoding="utf-8")
        block = development.split("`/setcommands`")[1].split("```")[1]
        listed = {
            f"/{line.split(' - ')[0].strip()}"
            for line in block.splitlines()
            if " - " in line
        }

        self.assertTrue(listed, "BotFather 목록을 찾지 못했습니다")
        # A command in the menu that the bot ignores answers with the generic
        # "unknown command" reply, which reads as the bot being broken.
        self.assertEqual(listed - self._handled_commands(), set())

    def test_readme_table_only_lists_commands_that_exist(self):
        readme = (self.REPO / "README.md").read_text(encoding="utf-8")
        listed = set(re.findall(r"^\| `(/[a-z0-9_]+)` \|", readme, re.MULTILINE))

        self.assertTrue(listed, "README 명령어 표를 찾지 못했습니다")
        self.assertEqual(listed - self._handled_commands(), set())

    def test_the_operator_command_stays_out_of_public_lists(self):
        development = (self.REPO / "DEVELOPMENT.md").read_text(encoding="utf-8")
        readme = (self.REPO / "README.md").read_text(encoding="utf-8")
        block = development.split("`/setcommands`")[1].split("```")[1]

        self.assertNotIn("stats", block)
        self.assertNotIn("| `/stats` |", readme)
