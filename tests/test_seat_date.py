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
    Config, SeatSnapshot, SEAT_SELECTION_SWEET, SHOW_DAY_WEEKEND,
    TelegramError, Watcher,
)
from test_watcher import make_config, _kst


class SeatDateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(tmp.name)), subscriptions_enabled=True,
            target_start=dt.date(2026, 10, 2), target_end=dt.date(2026, 10, 4),
        )
        self.logger = logging.getLogger(f"seat-date-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = self.restart()
        self.chat = self.config.telegram_chat_id
        clock = patch.object(Config, "local_now", return_value=_kst(dt.date(2026, 10, 2)))
        clock.start()
        self.addCleanup(clock.stop)

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

    def selected(self, date, category=ALERT_SEATS, time="18:00", count=2):
        return self.chat in self.watcher.state.subscriber_ids_for(
            category, show_date=date, show_time=time, seats_available=count,
        )

    def test_existing_and_new_subscribers_default_to_all_dates(self):
        state = self.watcher.state
        state.data["subscribers"][self.chat].pop("seat_date_range", None)
        for date in (None, "2026-10-02", "2027-01-01"):
            self.assertTrue(self.selected(date))
        self.command("/start", "new")
        self.assertIsNone(state.seat_date_range("new"))
        self.watcher = self.restart()
        self.assertIsNone(self.watcher.state.seat_date_range(self.chat))

    def test_single_date_means_inclusive_from_not_single_day(self):
        self.assertIn("2026-10-03부터 (포함)", self.command("/date 20261003"))
        self.assertEqual(self.watcher.state.seat_date_range(self.chat), ("2026-10-03", None))
        for date, expected in (("2026-10-02", False), ("2026-10-03", True),
                               ("2026-10-04", True), ("2027-01-01", True)):
            with self.subTest(date=date):
                self.assertEqual(self.selected(date), expected)

    def test_closed_range_includes_both_ends_and_same_date_means_one_day(self):
        self.command("/date 20261003 20261005")
        for day in range(2, 7):
            self.assertEqual(self.selected(f"2026-10-{day:02d}"), 3 <= day <= 5)
        self.command("/date 20261003 20261003")
        self.assertTrue(self.selected("2026-10-03"))
        self.assertFalse(self.selected("2026-10-04"))

    def test_leap_days_and_cross_year_ranges(self):
        self.command("/date 20240229 20250101")
        self.assertTrue(self.selected("2024-02-29"))
        self.assertTrue(self.selected("2024-12-31"))
        self.assertTrue(self.selected("2025-01-01"))
        self.assertFalse(self.selected("2025-01-02"))

    def test_invalid_date_or_range_does_not_change_saved_preference(self):
        self.command("/date 20261003")
        for body in ("2026-10-03", "2026103", "202610030", "20261301", "20261000",
                     "20260931", "20260229", "00000101", "abcd", "20261005 20261003",
                     "20261003 20261005 extra", "20261003 bad", "２０２６１００３"):
            with self.subTest(body=body):
                reply = self.command("/date " + body)
                self.assertIn("YYYYMMDD", reply)
                self.assertNotIn("변경했습니다", reply)
                self.assertEqual(self.watcher.state.seat_date_range(self.chat), ("2026-10-03", None))
        self.assertIn("입력해주세요", self.command("/date_all 20261003"))

    def test_group_commands_report_and_persist_without_start_resetting(self):
        self.command("/date@YongamBot 20261003 20261005")
        self.watcher = self.restart()
        self.assertIn("2026-10-03~2026-10-05", self.command("/date"))
        self.assertIn("2026-10-03~2026-10-05", self.command("/status"))
        self.assertIn("이미", self.command("/date 20261003 20261005"))
        self.command("/start")
        self.assertEqual(self.watcher.state.seat_date_range(self.chat), ("2026-10-03", "2026-10-05"))
        self.watcher.cgv.fetch_date.assert_not_called()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()

    def test_date_all_only_resets_dates_not_other_preferences(self):
        for command in ("/day_weekend", "/seat_sweet", "/count_2", "/time 18:00 23:59", "/date 20261003"):
            self.command(command)
        before = dict(self.watcher.state.data["subscribers"][self.chat])
        self.assertIn("전체 날짜", self.command("/date_all"))
        self.watcher = self.restart()
        after = dict(self.watcher.state.data["subscribers"][self.chat])
        self.assertIsNone(after.pop("seat_date_range"))
        before.pop("seat_date_range")
        self.assertEqual(before, after)
        self.assertIn("이미", self.command("/date_all"))

    def test_date_time_day_scope_minimum_and_mode_all_combine(self):
        for command in ("/date 20261003 20261005", "/time 22:00 02:00",
                        "/day_weekend", "/seat_sweet", "/count_2"):
            self.command(command)
        self.assertTrue(self.selected("2026-10-03", ALERT_SEATS_SWEET, time="01:00"))
        self.assertFalse(self.selected("2026-10-05", ALERT_SEATS_SWEET, time="01:00"))
        self.assertFalse(self.selected("2026-10-10", ALERT_SEATS_SWEET, time="01:00"))
        self.assertFalse(self.selected("2026-10-03", ALERT_SEATS_SWEET, time="03:00"))
        self.assertFalse(self.selected("2026-10-03", ALERT_SEATS_SWEET, time="01:00", count=1))
        self.assertFalse(self.selected("2026-10-03", ALERT_SEATS, time="01:00"))
        self.assertTrue(self.selected("2026-10-10", ALERT_OPEN, time="12:00"))
        self.assertFalse(self.selected("2026-10-09", ALERT_OPEN, time="12:00"))
        self.watcher.state.set_alert_mode(self.chat, ALERT_MODE_OPEN_ONLY)
        self.assertFalse(self.selected("2026-10-03", ALERT_SEATS_SWEET, time="01:00"))
        self.watcher.state.set_alert_mode(self.chat, ALERT_MODE_SEATS_ONLY)
        self.assertFalse(self.selected("2026-10-10", ALERT_OPEN))

    def test_all_seat_categories_filter_but_openings_and_notices_bypass_dates(self):
        self.command("/date 20261003 20261003")
        for category in (ALERT_SEATS, ALERT_SEATS_UNCLASSIFIED, ALERT_SEATS_SWEET):
            with self.subTest(category=category):
                self.watcher.state.set_seat_selection(
                    self.chat, SEAT_SELECTION_SWEET if category == ALERT_SEATS_SWEET else "all",
                )
                self.assertTrue(self.selected("2026-10-03", category))
                self.assertFalse(self.selected("2026-10-04", category))
                self.assertFalse(self.selected(None, category))
                self.assertFalse(self.selected("2026-13-01", category))
        for category in (ALERT_OPEN, ALERT_NOTICE):
            self.assertTrue(self.selected("2026-10-02", category))
            self.assertTrue(self.selected("2026-10-04", category))
            self.assertTrue(self.selected(None, category))

    def test_expired_range_is_not_silently_reset_and_query_window_unchanged(self):
        dates = self.config.target_dates()
        self.command("/date 20250101 20250102")
        self.assertFalse(self.selected("2026-10-03"))
        self.assertTrue(self.selected("2026-10-03", ALERT_OPEN))
        self.command("/date 20280101")
        self.assertEqual(self.config.target_dates(), dates)
        self.assertIn("오늘부터 28일", self.command("/date"))
        self.assertIn("자동 해제되지 않습니다", self.command("/date"))

    def test_non_subscriber_and_closed_gate_cannot_change_date_settings(self):
        self.assertIn("/start", self.command("/date 20261003", "stranger"))
        self.assertFalse(self.watcher.state.is_subscribed("stranger"))
        self.config = dataclasses.replace(self.config, new_subscriptions_enabled=False)
        self.watcher = self.restart()
        for command in ("/date", "/date 20261003", "/date_all"):
            self.assertIn("신규 구독을 잠시 중단", self.command(command, "stranger"))
        self.assertIn("변경했습니다", self.command("/date 20261003"))

    def test_global_open_only_preserves_dates_and_disables_date_commands(self):
        self.command("/date 20261003")
        self.config = dataclasses.replace(self.config, open_only_mode=True)
        self.watcher = self.restart()
        self.assertIn("신규 오픈 전용", self.command("/date_all"))
        self.assertEqual(self.watcher.state.seat_date_range(self.chat), ("2026-10-03", None))

    def test_docs_replies_and_stats_include_date_filter_within_telegram_limit(self):
        for command in ("/start", "/desc", "/help", "/status", "/date"):
            reply = self.command(command)
            self.assertIn("/date", reply)
            self.assertLess(len(reply.encode("utf-16-le")) // 2, 4096)
        self.command("/date 20261003")
        for command in ("/statss", "/statsss"):
            self.assertIn("날짜 범위 설정 — 1명", self.command(command))
        self.assertIn("2026-10-03부터 (포함)", self.command("/statss"))
        self.command("/stop")
        self.assertLess(len(self.command("/desc").encode("utf-16-le")) // 2, 4096)

    def test_malformed_stored_range_defaults_to_all_and_setter_rejects_it(self):
        state = self.watcher.state
        for value in ("20261003", ["2026-10-03"], [None, None], ["bad", None],
                      ["2026-10-03", ""], ["2026-10-03", "2026-10-02"],
                      ["2026-10-03", {}], ["2026-02-29", None]):
            with self.subTest(value=value):
                state.data["subscribers"][self.chat]["seat_date_range"] = value
                self.assertIsNone(state.seat_date_range(self.chat))
                self.assertTrue(self.selected("2026-10-02"))
                with self.assertRaises(ValueError):
                    state.set_seat_date_range(self.chat, value)

    def test_outbox_rechecks_date_settings_after_restart_including_legacy_unknown_date(self):
        with patch.object(self.watcher, "_delivery_worker_running", return_value=True):
            for category, date in ((ALERT_SEATS, "2026-10-02"), (ALERT_SEATS, "2026-10-03"),
                                   (ALERT_SEATS, None), (ALERT_OPEN, "2026-10-02"), (ALERT_NOTICE, None)):
                self.watcher._broadcast_message(category + str(date), category=category,
                                                show_date=date, show_time="18:00", seats_available=3)
        self.command("/date 20261003")
        self.watcher = self.restart()
        self.watcher._retry_pending_deliveries()
        self.assertCountEqual([call.args[0] for call in self.watcher.telegram.send_message.call_args_list],
                              [ALERT_SEATS + "2026-10-03", ALERT_OPEN + "2026-10-02", ALERT_NOTICE + "None"])
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def test_immediate_send_and_failed_send_retry_both_respect_dates(self):
        self.command("/date 20261003")
        self.watcher.telegram.send_message.reset_mock()
        self.assertEqual(self.watcher._broadcast_message("too early", category=ALERT_SEATS,
                                                        show_date="2026-10-02"), (0, 0, 0))
        self.watcher.telegram.send_message.assert_not_called()
        self.watcher.telegram.send_message.side_effect = TelegramError("temporary failure")
        self.watcher._broadcast_message("eligible", category=ALERT_SEATS, show_date="2026-10-03")
        self.assertEqual(self.watcher.state.pending_deliveries()[0][1]["show_date"], "2026-10-03")
        self.watcher.state.set_seat_date_range(self.chat, ("2026-10-04", None))
        self.watcher.telegram.send_message.reset_mock()
        self.watcher._retry_pending_deliveries()
        self.watcher.telegram.send_message.assert_not_called()

    def test_full_cycles_keep_every_open_and_query_date_but_filter_seat_notifications(self):
        for chat in ("sweet", "all", "night"):
            self.watcher.state.add_subscriber(chat)
        self.watcher.state.set_seat_date_range(self.chat, ("2026-10-03", None))
        self.watcher.state.set_seat_date_range("sweet", ("2026-10-03", "2026-10-03"))
        self.watcher.state.set_seat_selection("sweet", SEAT_SELECTION_SWEET)
        self.watcher.state.set_seat_date_range("night", ("2026-10-03", None))
        self.watcher.state.set_seat_time_range("night", ("22:00", "02:00"))
        self.watcher.cgv.fetch_date.side_effect = lambda day: {"data": [{
            "scnsNm": "IMAX관", "scnYmd": day.strftime("%Y%m%d"), "scnsrtTm": "1800",
            "scnsNo": "018", "scnSseq": "4", "frSeatCnt": 3, "stcnt": 624,
        }]}
        self.watcher.cgv.fetch_seat_snapshot.return_value = SeatSnapshot(
            total=3, usable=3, mapped_total=3, available_rows=("J",),
            available_seats=(("J", "20"), ("J", "21"), ("J", "22")),
        )
        with patch.object(self.watcher, "_delivery_worker_running", return_value=True):
            self.assertEqual(self.watcher.run_cycle().new_sessions, 3)
            self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
            openings = self.watcher.state.pending_deliveries()
            self.assertEqual(len(openings), 12)
            for key, record in openings:
                self.assertEqual(record["category"], ALERT_OPEN)
                self.watcher.state.remove_pending_delivery(key)
            self.assertEqual(self.watcher.run_cycle().new_sessions, 0)
        queued = [record for _, record in self.watcher.state.pending_deliveries()]
        self.assertCountEqual(
            [(item["chat_id"], item["show_date"]) for item in queued],
            [(self.chat, "2026-10-03"), (self.chat, "2026-10-04"), ("sweet", "2026-10-03"),
             ("all", "2026-10-02"), ("all", "2026-10-03"), ("all", "2026-10-04")],
        )
        self.assertEqual(self.watcher.cgv.fetch_date.call_count, 6)
        self.assertEqual(self.watcher.cgv.fetch_seat_snapshot.call_count, 3)
        self.assertEqual(len(self.watcher.state.data["notified"]), 3)


if __name__ == "__main__":
    unittest.main()
