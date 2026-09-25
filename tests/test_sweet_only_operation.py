import copy
import dataclasses
import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from watcher import (
    ALERT_OPEN, ALERT_REPLY, ALERT_SEATS, ALERT_SEATS_SWEET,
    ALERT_SEATS_UNCLASSIFIED, ALERT_MODE_OPEN_ONLY, ALERT_MODE_SEATS_ONLY,
    Config, ConfigurationError, SEAT_SELECTION_ALL, SEAT_SELECTION_SWEET,
    SeatSnapshot, StateStore, Watcher,
)
from test_watcher import make_config, _kst


class SweetOnlyOperationTests(unittest.TestCase):
    TODAY = dt.date(2026, 8, 26)

    def make_watcher(self, directory, **overrides):
        config = dataclasses.replace(
            make_config(Path(directory)), seat_alert_sweet_only=True,
            subscriptions_enabled=True, **overrides,
        )
        # Preserve an actual legacy whole-auditorium preference on disk.
        legacy = StateStore(config.state_file)
        legacy.initialize_subscribers(config.telegram_chat_id)
        legacy.remove_subscriber(config.telegram_chat_id)
        legacy.add_subscriber("100")
        legacy.save()
        logger = logging.getLogger(f"sweet-only-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        watcher = Watcher(config, logger=logger)
        watcher.telegram.send_message = Mock()
        return watcher

    def test_config_is_opt_in_and_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            self.assertFalse(config.seat_alert_sweet_only)
            env = Path(directory) / ".env"
            original = env.read_text()
            env.write_text(original + "SEAT_ALERT_SWEET_ONLY=true\n")
            self.assertTrue(Config.from_env_file(env).seat_alert_sweet_only)
            env.write_text(original + "SEAT_ALERT_SWEET_ONLY=invalid\n")
            with self.assertRaises(ConfigurationError):
                Config.from_env_file(env)

    def test_existing_preferences_are_preserved_and_restore_when_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = self.make_watcher(directory)
            original = copy.deepcopy(watcher.state.data["subscribers"])
            self.assertEqual(original["100"]["seat_selection"], SEAT_SELECTION_ALL)
            self.assertEqual(watcher.state.seat_selection("100"), SEAT_SELECTION_SWEET)
            self.assertFalse(watcher.state.set_seat_selection("100", SEAT_SELECTION_ALL))
            self.assertFalse(watcher.state.set_seat_selection("100", SEAT_SELECTION_SWEET))
            self.assertEqual(watcher.state.data["subscribers"], original)
            watcher.state.save()
            restarted = StateStore(watcher.config.state_file, sweet_only=True)
            restarted.load()
            self.assertEqual(restarted.seat_selection("100"), SEAT_SELECTION_SWEET)
            restored = StateStore(watcher.config.state_file)
            restored.load()
            self.assertEqual(restored.seat_selection("100"), SEAT_SELECTION_ALL)
            self.assertEqual(restored.data["subscribers"], original)

    def test_new_subscriber_defaults_to_sweet_and_stats_use_effective_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = self.make_watcher(directory)
            watcher.state.add_subscriber("200")
            self.assertEqual(watcher.state.data["subscribers"]["200"]["seat_selection"], "sweet")
            stats = watcher.state.subscriber_breakdown()
            self.assertEqual(stats["seat_selections"], {"all": 0, "sweet": 2})

    def test_effective_routing_keeps_modes_dates_times_and_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = self.make_watcher(directory)
            state = watcher.state
            state.set_min_seats("100", 2)
            state.set_seat_time_range("100", ("18:00", "23:59"))
            state.set_seat_date_range("100", ("2026-08-27", None))
            self.assertEqual(state.subscriber_ids_for(ALERT_SEATS), ())
            self.assertEqual(state.subscriber_ids_for(ALERT_SEATS_UNCLASSIFIED), ())
            self.assertEqual(state.subscriber_ids_for(ALERT_OPEN, show_date="2026-08-26", show_time="09:00", seats_available=0), ("100",))
            def recipients(date, clock, count):
                return state.subscriber_ids_for(ALERT_SEATS_SWEET, show_date=date, show_time=clock, seats_available=count)
            self.assertEqual(recipients("2026-08-27", "18:00", 2), ("100",))
            self.assertEqual(recipients("2026-08-26", "18:00", 2), ())
            self.assertEqual(recipients("2026-08-27", "17:00", 2), ())
            self.assertEqual(recipients("2026-08-27", "18:00", 1), ())
            state.set_alert_mode("100", ALERT_MODE_OPEN_ONLY)
            self.assertEqual(recipients("2026-08-27", "18:00", 2), ())
            self.assertEqual(state.subscriber_ids_for(ALERT_OPEN), ("100",))
            state.set_alert_mode("100", ALERT_MODE_SEATS_ONLY)
            self.assertEqual(state.subscriber_ids_for(ALERT_OPEN), ())

    def prepare_cycle(self, watcher, seats):
        watcher.cgv.fetch_date = lambda _date: {"data": [{
            "scnsNm": "IMAX관", "scnYmd": "20260826", "scnsrtTm": "2330",
            "scnsNo": "13", "scnSseq": "4", "frSeatCnt": len(seats), "stcnt": 624,
        }]}
        watcher.cgv.fetch_seat_snapshot = lambda _session: SeatSnapshot(
            total=len(seats), usable=len(seats), mapped_total=len(seats),
            available_rows=tuple(sorted({row for row, number in seats})),
            available_seats=tuple(sorted(seats)),
        )

    def test_open_is_unfiltered_but_later_seats_only_show_sweet_locations(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Config, "local_now", return_value=_kst(self.TODAY)):
            watcher = self.make_watcher(directory)
            seats = {("B", "1"), ("J", "10")}
            self.prepare_cycle(watcher, seats)
            original_fetch = watcher.cgv.fetch_seat_snapshot
            watcher.cgv.fetch_seat_snapshot = Mock(side_effect=AssertionError("Opening must not wait for seat detail"))
            watcher.run_cycle()
            watcher.cgv.fetch_seat_snapshot.assert_not_called()
            self.assertIn("예매 오픈 감지", watcher.telegram.send_message.call_args.args[0])
            watcher.cgv.fetch_seat_snapshot = original_fetch
            watcher.telegram.send_message.reset_mock()
            watcher.run_cycle()
            watcher.telegram.send_message.assert_not_called()
            seats.add(("K", "21"))
            watcher.run_cycle()
            watcher.telegram.send_message.assert_called_once()
            text = watcher.telegram.send_message.call_args.args[0]
            self.assertIn("좌석 번호: K21", text)
            self.assertIn("0석 → 1석", text)
            self.assertNotIn("B1", text)
            self.assertNotIn("J10", text)
            # This change deliberately does not change the repeat policy.
            watcher.telegram.send_message.reset_mock()
            watcher.run_cycle()
            watcher.telegram.send_message.assert_called_once()

    def test_unknown_positions_do_not_fall_back_to_total_even_above_seven(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Config, "local_now", return_value=_kst(self.TODAY)):
            watcher = self.make_watcher(directory)
            seats = {("B", str(i)) for i in range(10)}
            self.prepare_cycle(watcher, seats)
            watcher.run_cycle()
            watcher.telegram.send_message.reset_mock()
            watcher.cgv.fetch_seat_snapshot = lambda _session: SeatSnapshot(total=10)
            watcher.run_cycle()
            watcher.telegram.send_message.assert_not_called()

    def test_minimum_counts_only_sweet_seats_in_real_scan(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Config, "local_now", return_value=_kst(self.TODAY)):
            watcher = self.make_watcher(directory)
            watcher.state.set_min_seats("100", 2)
            seats = {("B", "1"), ("K", "21")}
            self.prepare_cycle(watcher, seats)
            watcher.run_cycle()
            watcher.telegram.send_message.reset_mock()
            watcher.run_cycle()
            watcher.telegram.send_message.assert_not_called()
            seats.add(("K", "22"))
            watcher.run_cycle()
            watcher.telegram.send_message.assert_called_once()
            self.assertIn("좌석 번호: K21~22", watcher.telegram.send_message.call_args.args[0])

    def test_direct_broadcast_cannot_bypass_sweet_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = self.make_watcher(directory)
            for category in (ALERT_SEATS, ALERT_SEATS_UNCLASSIFIED):
                self.assertEqual(watcher._broadcast_message("outside", category=category, recipients=("100",)), (0, 0, 0))
            watcher.telegram.send_message.assert_not_called()

    def test_old_general_queue_is_not_sent_and_open_sweet_reply_still_work(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = self.make_watcher(directory)
            for category in (ALERT_SEATS, ALERT_SEATS_UNCLASSIFIED, ALERT_SEATS_SWEET, ALERT_OPEN, ALERT_REPLY):
                watcher.state.queue_pending_delivery("100", category, category, seats_available=1)
            watcher._retry_pending_deliveries()
            texts = {call.args[0] for call in watcher.telegram.send_message.call_args_list}
            self.assertEqual(texts, {ALERT_SEATS_SWEET, ALERT_OPEN, ALERT_REPLY})
            self.assertEqual(watcher.state.pending_delivery_count(), 0)

    def test_commands_welcome_description_and_help_explain_global_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = self.make_watcher(directory)
            for i, command in enumerate(("/start", "/status", "/seat", "/seat_all", "/seat all", "/seat_sweet", "/desc", "/help"), 1):
                watcher.telegram.get_updates = lambda **kwargs: [{
                    "update_id": i,
                    "message": {"text": command, "chat": {"id": 100, "type": "private"}},
                }]
                watcher.sync_subscribers()
                text = watcher.telegram.send_message.call_args.args[0]
                self.assertIn("명당", text, command)
                self.assertNotIn("모든 A열 제외 좌석", text, command)
                if command in {"/start", "/desc", "/help"}:
                    self.assertIn("신규", text)
                if command == "/desc":
                    self.assertIn("위치 미확인 좌석은 제외", text)
                    self.assertIn("F16~29", text)
                self.assertEqual(watcher.state.data["subscribers"]["100"]["seat_selection"], "all")

    def test_open_only_mode_remains_stricter(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = self.make_watcher(directory, open_only_mode=True)
            watcher._broadcast_message("sweet", category=ALERT_SEATS_SWEET)
            watcher.telegram.send_message.assert_not_called()
            watcher._broadcast_message("opening", category=ALERT_OPEN)
            watcher.telegram.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
