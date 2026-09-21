import dataclasses
import datetime as dt
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from watcher import (
    ALERT_OPEN, ALERT_SEATS, ALERT_SEATS_SWEET, ALERT_SEATS_UNCLASSIFIED,
    ALERT_SYSTEM, ALERT_MODE_SEATS_ONLY, ALERT_MODE_OPEN_ONLY,
    BookingSession, CgvClient, Config, ConfigurationError, FetchError,
    SHOW_DAY_WEEKEND, TelegramError, Watcher,
)
from test_watcher import make_config, _kst


class OpenOnlyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temp.name)), open_only_mode=True,
            subscriptions_enabled=True, new_subscriptions_enabled=False,
        )
        clock = patch.object(Config, "local_now", return_value=_kst(dt.date(2026, 8, 26)))
        clock.start()
        self.addCleanup(clock.stop)
        self.logger = logging.getLogger(f"open-only-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.rows = [self.row()]
        self.watcher = self.restart()

    @staticmethod
    def row(time="1430", remaining=10):
        row = {
            "scnsNm": "IMAX관", "scnYmd": "20260826", "scnsrtTm": time,
            "scnsNo": "018", "scnSseq": time, "stcnt": 624,
        }
        if remaining is not None:
            row["frSeatCnt"] = remaining
        return row

    def restart(self, *, dry_run=False):
        watcher = Watcher(self.config, logger=self.logger, dry_run=dry_run)
        watcher.cgv = Mock()
        watcher.cgv.fetch_date.side_effect = lambda *_args, **_kwargs: {"data": self.rows}
        watcher.cgv.fetch_seat_snapshot.side_effect = AssertionError("seat API called")
        watcher.telegram = Mock()
        return watcher

    def command(self, text, chat_id=None):
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.watcher.state.telegram_update_offset,
            "message": {"text": text, "chat": {
                "id": chat_id or self.config.telegram_chat_id, "type": "private",
            }},
        }]
        self.watcher.sync_subscribers()
        return self.watcher.telegram.send_message.call_args.args[0]

    def test_only_new_showings_notify_across_restarts_and_seat_changes(self):
        first = self.watcher.run_cycle()
        self.assertEqual(first.new_sessions, 1)
        self.assertEqual(self.watcher.telegram.send_message.call_count, 1)
        self.assertIn("10/624석", self.watcher.telegram.send_message.call_args.args[0])
        for remaining in (9, 0, 7, 7):
            self.rows = [self.row(remaining=remaining)]
            self.assertEqual(self.watcher.run_cycle().new_sessions, 0)
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        self.assertEqual(self.watcher.telegram.send_message.call_count, 1)
        self.watcher = self.restart()
        self.rows.append(self.row("1800"))
        self.assertEqual(self.watcher.run_cycle().new_sessions, 1)
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        reply = self.watcher.telegram.send_message.call_args.args[0]
        self.assertIn("18:00", reply)
        self.assertNotIn("14:30", reply)

    def test_sold_out_and_unknown_seat_counts_still_announce_openings(self):
        self.rows = [self.row(remaining=0), self.row("1800", None)]
        self.assertEqual(self.watcher.run_cycle().new_sessions, 2)
        reply = self.watcher.telegram.send_message.call_args.args[0]
        self.assertIn("0/624석 (매진)", reply)
        self.assertIn("?/624석", reply)
        self.assertNotIn("취소표 나오면 알림", reply)
        self.assertNotIn("A열 제외 잔여 좌석:", reply)
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()

    def test_existing_notification_and_seat_history_are_preserved(self):
        session = BookingSession(date="2026-08-26", start_time="14:30")
        key = session.notification_key(site_no=self.config.site_no, movie_no=self.config.movie_no)
        self.watcher.state.mark_notified(key, session)
        from watcher import SeatSnapshot
        snapshot = SeatSnapshot(total=30)
        self.watcher.state.set_seat_snapshot(key, snapshot)
        self.watcher.state.save()
        self.watcher = self.restart()
        self.assertEqual(self.watcher.run_cycle().new_sessions, 0)
        self.assertEqual(self.watcher.state.seat_snapshot(key), snapshot)
        self.watcher.telegram.send_message.assert_not_called()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()

    def test_queue_drops_all_seat_categories_but_delivers_open_and_system(self):
        chat = self.config.telegram_chat_id
        for category in (ALERT_SEATS, ALERT_SEATS_SWEET, ALERT_SEATS_UNCLASSIFIED, ALERT_OPEN, ALERT_SYSTEM):
            self.watcher.state.queue_pending_delivery(chat, category, category)
        self.watcher.state.save()
        self.watcher = self.restart()
        self.watcher._retry_pending_deliveries()
        self.assertCountEqual(
            [call.args[0] for call in self.watcher.telegram.send_message.call_args_list],
            [ALERT_OPEN, ALERT_SYSTEM],
        )
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)
        self.watcher = self.restart()
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def test_seat_broadcasts_cannot_send_or_enqueue_even_with_explicit_recipients(self):
        with patch.object(self.watcher, "_delivery_worker_running", return_value=True):
            for category in (ALERT_SEATS, ALERT_SEATS_SWEET, ALERT_SEATS_UNCLASSIFIED):
                self.assertEqual(
                    self.watcher._broadcast_message("seat", category=category, recipients=[self.config.telegram_chat_id]),
                    (0, 0, 0),
                )
        self.watcher.telegram.send_message.assert_not_called()
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def test_new_open_uses_durable_outbox_and_is_not_requeued(self):
        with patch.object(self.watcher, "_delivery_worker_running", return_value=True):
            self.watcher.run_cycle()
        self.assertEqual(self.watcher.state.pending_delivery_count(), 1)
        self.watcher.telegram.send_message.assert_not_called()
        self.watcher = self.restart()
        self.watcher.run_cycle()
        self.assertEqual(self.watcher.telegram.send_message.call_count, 1)
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def test_failed_open_delivery_is_retried_without_seat_calls(self):
        self.watcher.telegram.send_message.side_effect = TelegramError("test unavailable")
        self.watcher.run_cycle()
        self.assertEqual(self.watcher.state.pending_delivery_count(), 1)
        self.watcher = self.restart()
        self.watcher._retry_pending_deliveries()
        self.watcher.run_cycle()
        self.assertEqual(self.watcher.telegram.send_message.call_count, 1)
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()

    def test_schedule_failure_does_not_mark_open_as_notified(self):
        self.watcher.cgv.fetch_date.side_effect = FetchError("HTTP 403")
        self.watcher.run_cycle()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        self.watcher = self.restart()
        self.assertEqual(self.watcher.run_cycle().new_sessions, 1)

    def test_existing_modes_and_day_filters_are_respected(self):
        state = self.watcher.state
        state.add_subscriber("seats-only")
        state.set_alert_mode("seats-only", ALERT_MODE_SEATS_ONLY)
        state.add_subscriber("weekend-only")
        state.set_show_day_selection("weekend-only", SHOW_DAY_WEEKEND)
        state.add_subscriber("open-only")
        state.set_alert_mode("open-only", ALERT_MODE_OPEN_ONLY)
        self.watcher.run_cycle()
        recipients = [call.kwargs["chat_id"] for call in self.watcher.telegram.send_message.call_args_list]
        self.assertCountEqual(recipients, [self.config.telegram_chat_id, "open-only"])
        self.assertEqual(state.alert_mode("seats-only"), ALERT_MODE_SEATS_ONLY)

    def test_info_commands_explain_effective_mode_and_gate_stays_closed(self):
        for command in ("/start", "/status", "/help", "/desc", "/mode"):
            self.assertIn("신규 오픈 전용", self.command(command))
        for command in ("/help", "/desc"):
            reply = self.command(command)
            self.assertIn("신규 구독을 잠시 중단", reply)
            self.assertNotIn("/mode_seats -", reply)
            self.assertNotIn("/seat_sweet -", reply)
        self.assertIn("신규 구독을 잠시 중단", self.command("/start", "new-user"))
        self.assertFalse(self.watcher.state.is_subscribed("new-user"))

    def test_inactive_settings_do_not_change_but_open_mode_can_be_selected(self):
        chat = self.config.telegram_chat_id
        state = self.watcher.state
        state.set_alert_mode(chat, ALERT_MODE_SEATS_ONLY)
        before = dict(state.data["subscribers"][chat])
        for command in ("/mode_seats", "/mode_all", "/seat_sweet", "/count_2"):
            self.assertIn("신규 오픈 전용", self.command(command))
            self.assertEqual(state.data["subscribers"][chat], before)
        self.assertIn("현재 수신 알림: 없음", self.command("/status"))
        self.command("/mode_open")
        self.assertEqual(state.alert_mode(chat), ALERT_MODE_OPEN_ONLY)
        self.command("/day_weekend")
        self.assertEqual(state.show_day_selection(chat), SHOW_DAY_WEEKEND)

    def test_seat_client_guard_prevents_any_http(self):
        client = CgvClient(self.config)
        with patch.object(client, "_get_json") as get_json:
            with self.assertRaises(FetchError):
                client.fetch_seat_snapshot(BookingSession(date="2026-08-26", start_time="14:30"))
            get_json.assert_not_called()

    def test_env_is_opt_in_and_conflicting_seat_diagnostic_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Config.from_env_file(self.config.project_dir / ".env").open_only_mode)
        with patch.dict(os.environ, {"OPEN_ONLY_MODE": "true"}, clear=True):
            self.assertTrue(Config.from_env_file(self.config.project_dir / ".env").open_only_mode)
        with patch.dict(os.environ, {"OPEN_ONLY_MODE": "typo"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Config.from_env_file(self.config.project_dir / ".env")
        with patch.dict(os.environ, {
            "OPEN_ONLY_MODE": "true", "CGV_HEADER_PROBE_REQUEST_ID": "probe-test",
            "CGV_HEADER_PROBE_SEAT_URL": "https://cgv.co.kr/api/v1/booking/searchIfSeatData",
        }, clear=True):
            with self.assertRaises(ConfigurationError):
                Config.from_env_file(self.config.project_dir / ".env")

    def test_dry_run_neither_notifies_nor_records_new_showings(self):
        self.watcher = self.restart(dry_run=True)
        self.assertEqual(self.watcher.run_cycle().new_sessions, 1)
        self.assertEqual(self.watcher.run_cycle().new_sessions, 1)
        self.watcher.telegram.send_message.assert_not_called()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
