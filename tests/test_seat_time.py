import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from watcher import (
    ALERT_MODE_OPEN_ONLY, ALERT_MODE_SEATS_ONLY, ALERT_NOTICE, ALERT_OPEN,
    ALERT_SEATS, ALERT_SEATS_SWEET, ALERT_SEATS_UNCLASSIFIED,
    Config, SeatSnapshot, SEAT_SELECTION_SWEET, SHOW_DAY_WEEKEND, StateStore,
    TelegramError, Watcher,
)
from test_watcher import make_config, _kst


class SeatTimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(tmp.name)), subscriptions_enabled=True,
        )
        self.logger = logging.getLogger(f"seat-time-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = self.restart()
        self.chat = self.config.telegram_chat_id
        self.clock = patch.object(Config, "local_now", return_value=_kst(dt.date(2026, 8, 26)))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def restart(self):
        watcher = Watcher(self.config, logger=self.logger)
        watcher.telegram = Mock()
        watcher.telegram.get_updates.return_value = []
        watcher.cgv = Mock()
        return watcher

    def command(self, text, chat=None):
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.watcher.state.telegram_update_offset,
            "message": {"text": text, "chat": {"id": chat or self.chat, "type": "private"}},
        }]
        self.assertTrue(self.watcher.sync_subscribers())
        return self.watcher.telegram.send_message.call_args.args[0]

    def selected(self, time, category=ALERT_SEATS, date="2026-08-29", count=2):
        return self.chat in self.watcher.state.subscriber_ids_for(
            category, show_time=time, show_date=date, seats_available=count,
        )

    def test_existing_and_new_subscribers_default_to_all_without_migration(self):
        state = self.watcher.state
        state.data["subscribers"][self.chat].pop("seat_time_range", None)
        self.assertIsNone(state.seat_time_range(self.chat))
        for time in ("00:00", "23:59", None):
            self.assertTrue(self.selected(time))
        self.command("/start", "new")
        self.assertIsNone(state.seat_time_range("new"))

    def test_range_includes_both_boundaries_and_excludes_adjacent_minutes(self):
        self.command("/time 18:00 23:00")
        for time, expected in (("17:59", False), ("18:00", True), ("22:59", True),
                               ("23:00", True), ("23:01", False), (None, False)):
            with self.subTest(time=time):
                self.assertEqual(self.selected(time), expected)

    def test_overnight_range_uses_clock_time_not_notification_time(self):
        self.command("/time 22:00 02:00")
        for time, expected in (("21:59", False), ("22:00", True), ("23:59", True),
                               ("00:00", True), ("02:00", True), ("02:01", False)):
            with self.subTest(time=time):
                self.assertEqual(self.selected(time), expected)

    def test_same_time_selects_one_minute_and_full_day_includes_midnight(self):
        self.command("/time 18:00 18:00")
        self.assertTrue(self.selected("18:00"))
        self.assertFalse(self.selected("18:01"))
        self.command("/time 00:00 23:59")
        self.assertTrue(self.selected("00:00"))
        self.assertTrue(self.selected("23:59"))

    def test_group_command_normalizes_single_digit_hour_and_survives_restart(self):
        self.command("/time@YongamBot 9:00 18:00")
        self.watcher = self.restart()
        self.assertEqual(self.watcher.state.seat_time_range(self.chat), ("09:00", "18:00"))
        self.assertIn("09:00~18:00", self.command("/status"))
        self.assertIn("09:00~18:00", self.command("/time"))
        self.assertIn("이미", self.command("/time 09:00 18:00"))
        self.command("/start")
        self.assertEqual(self.watcher.state.seat_time_range(self.chat), ("09:00", "18:00"))

    def test_invalid_inputs_never_change_saved_preference(self):
        self.command("/time 18:00 23:59")
        for body in ("18:00", "24:00 02:00", "18:60 23:59", "1800 2359",
                     "18:00 23:59 extra", "aa bb", "-1:00 02:00", "18:00:00 23:00"):
            with self.subTest(body=body):
                self.assertIn("시간 형식", self.command("/time " + body))
                self.assertEqual(self.watcher.state.seat_time_range(self.chat), ("18:00", "23:59"))
        self.assertIn("시간 형식", self.command("/time_all extra"))

    def test_reset_preserves_other_preferences(self):
        for command in ("/day_weekend", "/seat_sweet", "/count_2", "/time 18:00 23:59"):
            self.command(command)
        self.assertIn("전체 시간", self.command("/time_all"))
        self.watcher = self.restart()
        state = self.watcher.state
        self.assertIsNone(state.seat_time_range(self.chat))
        self.assertEqual(state.show_day_selection(self.chat), SHOW_DAY_WEEKEND)
        self.assertEqual(state.seat_selection(self.chat), SEAT_SELECTION_SWEET)
        self.assertEqual(state.min_seats(self.chat), 2)

    def test_modes_days_seat_scopes_and_minimum_still_combine(self):
        for command in ("/time 22:00 02:00", "/day_weekend", "/seat_sweet", "/count_2"):
            self.command(command)
        self.assertTrue(self.selected("01:00", ALERT_SEATS_SWEET))
        self.assertFalse(self.selected("01:00", ALERT_SEATS_SWEET, date="2026-08-28"))
        self.assertFalse(self.selected("01:00", ALERT_SEATS_SWEET, count=1))
        self.assertFalse(self.selected("01:00", ALERT_SEATS))
        self.assertFalse(self.selected("03:00", ALERT_SEATS_SWEET))
        self.assertTrue(self.selected("12:00", ALERT_OPEN))
        self.assertFalse(self.selected("12:00", ALERT_OPEN, date="2026-08-28"))
        self.watcher.state.set_alert_mode(self.chat, ALERT_MODE_OPEN_ONLY)
        self.assertFalse(self.selected("01:00", ALERT_SEATS_SWEET))
        self.watcher.state.set_alert_mode(self.chat, ALERT_MODE_SEATS_ONLY)
        self.assertFalse(self.selected("12:00", ALERT_OPEN))

    def test_all_seat_categories_filter_but_openings_and_notices_do_not(self):
        self.command("/time 18:00 23:59")
        for category in (ALERT_SEATS, ALERT_SEATS_UNCLASSIFIED, ALERT_SEATS_SWEET):
            with self.subTest(category=category):
                self.watcher.state.set_seat_selection(
                    self.chat, SEAT_SELECTION_SWEET if category == ALERT_SEATS_SWEET else "all",
                )
                self.assertFalse(self.selected("14:30", category))
                self.assertTrue(self.selected("18:00", category))
        self.assertTrue(self.selected("14:30", ALERT_OPEN))
        self.assertTrue(self.selected(None, ALERT_OPEN))
        self.assertTrue(self.selected("14:30", ALERT_NOTICE))

    def test_non_subscriber_cannot_set_and_closed_gate_is_respected(self):
        self.assertIn("/start", self.command("/time 18:00 23:59", "stranger"))
        self.assertFalse(self.watcher.state.is_subscribed("stranger"))
        self.config = dataclasses.replace(self.config, new_subscriptions_enabled=False)
        self.watcher = self.restart()
        for command in ("/time", "/time 18:00 23:59", "/time_all"):
            self.assertIn("신규 구독을 잠시 중단", self.command(command, "stranger"))

    def test_global_open_only_preserves_time_preference_but_disables_commands(self):
        self.command("/time 18:00 23:59")
        self.config = dataclasses.replace(self.config, open_only_mode=True)
        self.watcher = self.restart()
        self.assertIn("신규 오픈 전용", self.command("/time_all"))
        self.assertEqual(self.watcher.state.seat_time_range(self.chat), ("18:00", "23:59"))

    def test_docs_replies_and_stats_describe_time_filter(self):
        for command in ("/start", "/desc", "/help", "/status"):
            reply = self.command(command)
            self.assertIn("/time", reply)
            self.assertLess(len(reply.encode("utf-16-le")) // 2, 4096)
        self.command("/time 22:00 02:00")
        for command in ("/statss", "/statsss"):
            self.assertIn("시간 범위 설정 — 1명", self.command(command))
        self.assertIn("22:00~02:00 (자정 통과)", self.command("/statss"))

    def test_malformed_stored_range_defaults_to_all_without_affecting_other_fields(self):
        state = self.watcher.state
        for value in ("18:00", ["18:00"], ["25:00", "02:00"], [None, {}]):
            state.data["subscribers"][self.chat]["seat_time_range"] = value
            self.assertIsNone(state.seat_time_range(self.chat))
            self.assertTrue(self.selected("14:30"))

    def test_outbox_survives_restart_and_rechecks_changed_preferences(self):
        with patch.object(self.watcher, "_delivery_worker_running", return_value=True):
            for category, time in ((ALERT_SEATS, "14:30"), (ALERT_SEATS, "18:00"), (ALERT_OPEN, "14:30")):
                self.watcher._broadcast_message(category + time, category=category,
                                                show_time=time, show_date="2026-08-26", seats_available=3)
        self.assertEqual(self.watcher.state.pending_delivery_count(), 3)
        self.command("/time 18:00 23:59")
        self.watcher = self.restart()
        self.watcher._retry_pending_deliveries()
        self.assertCountEqual([call.args[0] for call in self.watcher.telegram.send_message.call_args_list],
                              [ALERT_SEATS + "18:00", ALERT_OPEN + "14:30"])
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def test_failed_synchronous_send_persists_time_for_retry(self):
        self.watcher.telegram.send_message.side_effect = TelegramError("temporary failure")
        self.watcher._broadcast_message("seat", category=ALERT_SEATS, show_time="14:30")
        self.assertEqual(self.watcher.state.pending_deliveries()[0][1]["show_time"], "14:30")
        self.watcher.state.set_seat_time_range(self.chat, ("18:00", "23:59"))
        self.watcher.telegram.send_message.reset_mock()
        self.watcher._retry_pending_deliveries()
        self.watcher.telegram.send_message.assert_not_called()

    def test_legacy_queue_extracts_old_and_new_labels_and_keeps_unfiltered_unknown(self):
        state = self.watcher.state
        for label in ("상영 시간", "상영 시작시간"):
            state.queue_pending_delivery(self.chat, f"{label}: 14:30", ALERT_SEATS)
            state.queue_pending_delivery(self.chat, f"{label}: 18:00", ALERT_SEATS)
        state.queue_pending_delivery(self.chat, "unknown time", ALERT_SEATS)
        state.queue_pending_delivery(self.chat, "open", ALERT_OPEN)
        state.add_subscriber("unfiltered")
        state.queue_pending_delivery("unfiltered", "unknown time", ALERT_SEATS)
        self.command("/time 18:00 23:59")
        self.watcher = self.restart()
        self.watcher._retry_pending_deliveries()
        self.assertCountEqual([call.args[0] for call in self.watcher.telegram.send_message.call_args_list],
                              ["상영 시간: 18:00", "상영 시작시간: 18:00", "open", "unknown time"])

    def test_full_cycles_keep_all_openings_and_filter_only_seats_for_each_subscriber(self):
        for chat in (self.chat, "sweet", "all"):
            self.watcher.state.add_subscriber(chat)
        for chat in (self.chat, "sweet"):
            self.watcher.state.set_seat_time_range(chat, ("18:00", "23:59"))
        self.watcher.state.set_seat_selection("sweet", SEAT_SELECTION_SWEET)
        self.watcher.cgv.fetch_date.return_value = {"data": [
            {"scnsNm": "IMAX관", "scnYmd": "20260826", "scnsrtTm": time,
             "scnsNo": "018", "scnSseq": time, "frSeatCnt": 3, "stcnt": 624}
            for time in ("1430", "1800")
        ]}
        self.watcher.cgv.fetch_seat_snapshot.return_value = SeatSnapshot(
            total=3, usable=3, mapped_total=3, available_rows=("J",),
            available_seats=(("J", "20"), ("J", "21"), ("J", "22")),
        )
        with patch.object(self.watcher, "_delivery_worker_running", return_value=True):
            self.assertEqual(self.watcher.run_cycle().new_sessions, 2)
            self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
            openings = self.watcher.state.pending_deliveries()
            self.assertEqual(len(openings), 3)
            for key, record in openings:
                self.assertEqual(record["category"], ALERT_OPEN)
                self.assertIn("14:30", record["text"])
                self.assertIn("18:00", record["text"])
                self.watcher.state.remove_pending_delivery(key)
            self.assertEqual(self.watcher.run_cycle().new_sessions, 0)
        queued = [record for _, record in self.watcher.state.pending_deliveries()]
        self.assertEqual(len(queued), 4)
        self.assertCountEqual(
            [(item["chat_id"], item["show_time"]) for item in queued],
            [(self.chat, "18:00"), ("sweet", "18:00"), ("all", "14:30"), ("all", "18:00")],
        )
        # Query and history are deliberately not filtered along with delivery.
        self.assertEqual(self.watcher.cgv.fetch_seat_snapshot.call_count, 2)
        self.assertEqual(len(self.watcher.state.data["notified"]), 2)


if __name__ == "__main__":
    unittest.main()
