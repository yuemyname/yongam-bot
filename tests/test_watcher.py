import datetime as dt
import dataclasses
import http.client
import json
import logging
import os
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
    ALERT_SEATS_UNCLASSIFIED,
    ALERT_SYSTEM,
    SCAN_MODE_CURSOR,
    SCAN_MODE_FULL,
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
                                "seatStusCd": "00",
                                "seatSaleYn": "Y",
                                "seatSalfrmCd": "01",
                            },
                            {
                                "seatLocNo": "2",
                                "seatRowNm": "A",
                                "seatStusCd": "00",
                                "seatSaleYn": "Y",
                                "seatSalfrmCd": "04",
                            },
                            {
                                "seatLocNo": "3",
                                "seatRowNm": "C",
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
                        "seatStusCd": "00",
                        "seatSaleYn": "Y",
                        "seatSalfrmCd": "01",
                    },
                    {
                        "seatLocNo": "W1",
                        "seatRowNm": "B",
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
        self.assertFalse(snapshot.row_a_only)
        self.assertFalse(snapshot.should_suppress)


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
            record = json.loads(state_path.read_text(encoding="utf-8"))["seat_counts"][
                "session"
            ]
            self.assertEqual(record["usable"], 3)
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
            total=5, usable=2, mapped_total=5, available_rows=("A", "B")
        )
        same = dataclasses.replace(previous)
        changed = dataclasses.replace(previous, usable=3)

        self.assertFalse(_seat_snapshot_changed(previous, same))
        self.assertTrue(_seat_snapshot_changed(previous, changed))

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

    def test_unclassified_seat_alerts_skip_verified_only_subscribers(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            for chat_id in ("1", "2", "3"):
                store.add_subscriber(chat_id)
            store.set_alert_mode("3", ALERT_MODE_OPEN_ONLY)
            self.assertTrue(store.set_verified_seats_only("2", True))

            # Classified seat alerts still reach both seat subscribers.
            self.assertEqual(store.subscriber_ids_for(ALERT_SEATS), ("1", "2"))
            # Unclassified ones skip the subscriber who asked for verified only.
            self.assertEqual(
                store.subscriber_ids_for(ALERT_SEATS_UNCLASSIFIED), ("1",)
            )
            # The open-only subscriber is never in either seat audience.
            self.assertNotIn("3", store.subscriber_ids_for(ALERT_SEATS))

    def test_verified_seats_preference_defaults_off_and_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "notified.json"
            store = StateStore(path)
            store.add_subscriber("111")

            self.assertFalse(store.verified_seats_only("111"))
            self.assertTrue(store.set_verified_seats_only("111", True))
            self.assertFalse(store.set_verified_seats_only("111", True))
            store.save()

            reloaded = StateStore(path)
            reloaded.load()
            self.assertTrue(reloaded.verified_seats_only("111"))
            self.assertTrue(reloaded.set_verified_seats_only("111", False))

    def test_subscriber_stored_before_the_feature_accepts_unclassified(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.data["subscribers"]["999"] = {"subscribed_at": "2026-01-01T00:00:00"}

            self.assertFalse(store.verified_seats_only("999"))
            self.assertIn("999", store.subscriber_ids_for(ALERT_SEATS_UNCLASSIFIED))

    def test_rejects_an_unknown_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.add_subscriber("1")
            with self.assertRaises(ValueError):
                store.set_alert_mode("1", "nope")


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
            # A far-away retry must not move the ordinary expansion start.
            self.assertEqual(
                watcher._expansion_dates(plan)[0], dt.date(2026, 8, 24)
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

    def test_expansion_covers_the_rest_of_the_configured_reach(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary, cursor_expansion_days=21)
            watcher.state.advance_frontier(dt.date(2026, 8, 20))
            watcher._cycle_index = 1

            expansion = watcher._expansion_dates(watcher._plan_scan(self.TODAY))

            # Probe ended at 08-23; expansion continues to 08-20 + 21 days.
            self.assertEqual(expansion[0], dt.date(2026, 8, 24))
            self.assertEqual(expansion[-1], dt.date(2026, 9, 9))

    def test_frontier_only_moves_forward(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher(temporary)

            self.assertTrue(watcher.state.advance_frontier(dt.date(2026, 8, 20)))
            self.assertFalse(watcher.state.advance_frontier(dt.date(2026, 8, 15)))
            self.assertFalse(watcher.state.advance_frontier(dt.date(2026, 8, 20)))
            self.assertTrue(watcher.state.advance_frontier(dt.date(2026, 8, 21)))
            self.assertEqual(watcher.state.frontier_date, dt.date(2026, 8, 21))


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
            self.assertIn("잔여 좌석 변경", sent[0][1])

    def test_partial_open_delivery_retries_only_the_failed_subscriber(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
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

            # The failed-recipient queue survives a Railway restart.
            restarted = Watcher(config, logger=logger)
            restarted.cgv.fetch_date = watcher.cgv.fetch_date
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
            config = make_config(Path(temporary))
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
            self.assertEqual(sent, [])

    def _watcher_with_two_seat_audiences(self, temporary, name):
        """Two seat-alert subscribers: 'open-info' accepts unclassified, 'strict' does not."""

        config = make_config(Path(temporary))
        logger = logging.getLogger(f"watcher-{name}-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.state.remove_subscriber(config.telegram_chat_id)
        for chat_id in ("open-info", "strict"):
            watcher.state.add_subscriber(chat_id)
        watcher.state.set_verified_seats_only("strict", True)
        return watcher

    def test_unclassified_seat_change_skips_verified_only_subscriber(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher_with_two_seat_audiences(temporary, "strict-seat")
            remaining = {"count": 10}
            watcher.cgv.fetch_date = lambda _date: self._schedule_payload(
                remaining["count"]
            )
            # No A-row breakdown: CGV's detail response could not be classified,
            # so the alert carries the "A열 여부 미확인" fallback.
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
            result = watcher.run_cycle()

            self.assertEqual(result.seat_changes, 1)
            self.assertEqual({chat_id for chat_id, _ in sent}, {"open-info"})
            self.assertIn("A열 여부 미확인", sent[0][1])

    def test_classified_seat_change_reaches_verified_only_subscriber(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher = self._watcher_with_two_seat_audiences(temporary, "classified")
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

            watcher.run_cycle()
            sent.clear()
            remaining["count"] = 9
            result = watcher.run_cycle()

            self.assertEqual(result.seat_changes, 1)
            self.assertEqual(
                {chat_id for chat_id, _ in sent}, {"open-info", "strict"}
            )
            self.assertNotIn("A열 여부 미확인", sent[0][1])

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

    def test_seat_info_commands_change_and_report_preference(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)), subscriptions_enabled=True
            )
            logger = logging.getLogger(f"watcher-seat-cmd-{id(self)}")
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
                batch(1, "/seat_verified", chat_id=555),
                batch(2, "/start"),
                batch(3, "/seat"),
                batch(4, "/seat_verified@YongsanBot"),
                batch(5, "/status"),
                batch(6, "/seat 전체"),
                batch(7, "/seat nonsense"),
                batch(8, "/mode_open"),
                batch(9, "/seat_verified"),
            ]
            watcher.telegram.get_updates = lambda **_kwargs: batches.pop(0)
            watcher.telegram.send_message = lambda text, **kwargs: replies.append(
                (kwargs.get("chat_id"), text)
            )

            watcher.sync_subscribers()
            self.assertIn("구독 중이 아닙니다", replies[-1][1])

            watcher.sync_subscribers()
            self.assertFalse(watcher.state.verified_seats_only("111222"))

            watcher.sync_subscribers()
            self.assertIn("현재 잔여 좌석 알림 범위", replies[-1][1])

            watcher.sync_subscribers()
            self.assertTrue(watcher.state.verified_seats_only("111222"))
            self.assertIn("A열 제외 좌석이 확인된 알림만", replies[-1][1])

            watcher.sync_subscribers()
            self.assertIn("좌석 알림 범위:", replies[-1][1])

            watcher.sync_subscribers()
            self.assertFalse(watcher.state.verified_seats_only("111222"))

            watcher.sync_subscribers()
            self.assertIn("알 수 없는 좌석 알림 범위", replies[-1][1])

            # Switching to open-only alerts makes the seat preference inert, and
            # the reply should say so rather than silently accepting it.
            watcher.sync_subscribers()
            watcher.sync_subscribers()
            self.assertTrue(watcher.state.verified_seats_only("111222"))
            self.assertIn("잔여 좌석 알림을 받지 않는 설정", replies[-1][1])

            reloaded = StateStore(config.state_file)
            reloaded.load()
            self.assertTrue(reloaded.verified_seats_only("111222"))

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
            self.assertIn("잔여 좌석 변경", sent[1])

    def test_all_dates_are_checked_before_existing_seat_details(self):
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

            self.assertLess(
                events.index("schedule:2026-08-27"),
                events.index("seats:2026-08-26"),
            )

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
        self.assertIn("22:00 5/624 보류·A열 여부 미확인", line)

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

    def test_a_showing_never_announced_keeps_retrying_every_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            watcher, remaining, calls = self._unreadable_seat_map_watcher(
                temporary, "unannounced", deferred_recheck_cycles=5
            )
            remaining["count"] = 5

            per_cycle = []
            for _ in range(4):
                before = len(calls)
                watcher.run_cycle()
                per_cycle.append(len(calls) - before)

            # It still owes the subscriber a booking-open alert, so the
            # backoff must not apply to it.
            self.assertEqual(per_cycle, [1, 1, 1, 1])

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

            # No new alerts from here, so the queue has to drain instead of
            # being retried forever.
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
            watcher.state.set_verified_seats_only("2002", True)

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
            self.assertIn("A열 제외 좌석이 확인된 알림만 — 1명", operator_reply)

            # A subscriber gets the generic reply, so the command stays hidden.
            send(2000, "/stats", 2)
            self.assertIn("사용 가능한 명령어", replies[-1][1])
            self.assertNotIn("구독 현황", replies[-1][1])

            send(int(config.telegram_chat_id), "/help", 3)
            self.assertNotIn("stats", replies[-1][1])

    def test_stats_counts_subscribers_stored_before_the_settings_existed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "notified.json")
            store.data["subscribers"]["9"] = {"subscribed_at": "2026-01-01T00:00:00"}
            store.add_subscriber("10", chat_type="group")

            breakdown = store.subscriber_breakdown()

            self.assertEqual(breakdown["total"], 2)
            self.assertEqual(breakdown["modes"][ALERT_MODE_ALL], 2)
            self.assertEqual(breakdown["verified_seats_only"], 0)
            self.assertEqual(breakdown["chat_types"]["group"], 1)
            self.assertEqual(breakdown["chat_types"]["unknown"], 1)

    def test_welcome_says_no_setup_is_required(self):
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

            self.assertIn("따로 설정하실 것은 없습니다", welcome)
            # Listing every setting made a finished subscription look unfinished.
            for command in ("/mode_all", "/mode_open", "/mode_seats", "/seat_"):
                self.assertNotIn(command, welcome)
            self.assertLess(len(welcome.splitlines()), 10)

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
                usable=session.remaining_seats,
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
                usable=session.remaining_seats - 2,
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
                usable=0,
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
            self.assertIn("A열 여부 미확인", sent_messages[0])
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
