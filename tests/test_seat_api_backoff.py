import dataclasses
import datetime as dt
import logging
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_watcher import make_config, _kst
from watcher import (
    BookingSession, CGV_RECOVERY_STATUS_RECOVERED, Config, CycleResult,
    FetchError, SeatSnapshot, Watcher, main,
)


class SeatApiBackoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(self.tmp.name)), target_end=dt.date(2026, 8, 28),
        )
        self.now = _kst(dt.date(2026, 8, 26), 9)
        clock = patch.object(Config, "local_now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.logger = logging.getLogger(self.id())
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = self.create_watcher()
        self.session = BookingSession(
            date="2026-08-26", start_time="14:30", screen_name="IMAX관",
            screen_no="018", screen_sequence="1", remaining_seats=8,
            total_seats=624,
        )
        self.key = self.session.notification_key(site_no="0013", movie_no="30001323")
        self.previous = SeatSnapshot(
            total=8, usable=8, mapped_total=8, available_rows=("J",),
            available_seats=tuple(("J", str(n)) for n in range(11, 19)),
        )
        self.watcher.state.mark_notified(self.key, self.session)
        self.watcher.state.set_seat_snapshot(self.key, self.previous)
        self.watcher.state.save()

    def create_watcher(self):
        watcher = Watcher(self.config, logger=self.logger)
        watcher.cgv = Mock()
        watcher.cgv.fetch_date.side_effect = lambda day: {"data": [{
            "scnsNm": "IMAX관", "scnYmd": day.strftime("%Y%m%d"),
            "scnsrtTm": "1430", "scnsNo": "018", "scnSseq": "1",
            "frSeatCnt": 7, "stcnt": 624,
        }]}
        watcher.telegram = Mock()
        return watcher

    def test_seat_403_does_not_skip_dates_or_new_openings_or_halt_recovery(self):
        self.watcher.state.set_cgv_recovery({"status": CGV_RECOVERY_STATUS_RECOVERED})
        self.watcher.cgv.fetch_seat_snapshot.side_effect = FetchError("HTTP 403")
        result = self.watcher.run_cycle()
        self.assertEqual(result.successful_dates, 3)
        self.assertEqual(result.new_sessions, 2)
        self.assertEqual(result.seat_forbidden_requests, 1)
        self.assertEqual(result.forbidden_requests, 0)
        self.assertEqual(result.rate_limited_requests, 0)
        self.assertEqual(result.schedule_skipped_dates, 0)
        self.assertEqual(result.seat_changes, 0)
        self.assertFalse(result.cgv_paused)
        self.watcher.cgv.fetch_seat_snapshot.assert_called_once()
        self.assertEqual(self.watcher.state.seat_snapshot(self.key), self.previous)
        self.assertEqual(self.watcher.state.cgv_recovery()["status"], CGV_RECOVERY_STATUS_RECOVERED)
        messages = [call.args[0] for call in self.watcher.telegram.send_message.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("예매 오픈 감지" in text for text in messages))

    def test_cooldown_survives_restart_and_count_changes_then_recovers(self):
        self.watcher.cgv.fetch_seat_snapshot.side_effect = FetchError("HTTP 403")
        self.watcher.run_cycle()
        restarted = self.create_watcher()
        self.now += dt.timedelta(seconds=1799)
        result = restarted.run_cycle()
        restarted.cgv.fetch_seat_snapshot.assert_not_called()
        self.assertEqual(result.successful_dates, 3)
        self.assertEqual(result.seat_changes, 0)
        self.assertEqual(restarted.state.seat_snapshot(self.key), self.previous)
        self.assertIn("신규 오픈 일정 조회는 계속", restarted.seat_api_status_text())
        self.now += dt.timedelta(seconds=1)
        restarted.cgv.fetch_seat_snapshot.return_value = SeatSnapshot(
            total=7, usable=7, mapped_total=7, available_rows=("K",),
        )
        result = restarted.run_cycle()
        self.assertEqual(result.seat_changes, 3)
        self.assertEqual(restarted.cgv.fetch_seat_snapshot.call_count, 3)
        self.assertEqual(restarted.state.seat_api_backoff(), {})
        self.assertEqual(restarted.seat_api_status_text(), "")

    def test_block_keeps_fresh_success_but_never_alerts_unverified_batch_tail(self):
        sessions = [dataclasses.replace(self.session, start_time=time, screen_sequence=str(i))
                    for i, time in enumerate(("10:00", "14:30", "18:00"), 1)]
        for session in sessions:
            key = session.notification_key(site_no="0013", movie_no="30001323")
            self.watcher.state.mark_notified(key, session)
            self.watcher.state.set_seat_snapshot(key, self.previous)
        self.watcher.cgv.fetch_date.side_effect = lambda day: {"data": [
            {"scnsNm": "IMAX관", "scnYmd": day.strftime("%Y%m%d"),
             "scnsrtTm": session.start_time.replace(":", ""), "scnsNo": "018",
             "scnSseq": session.screen_sequence, "frSeatCnt": 7, "stcnt": 624}
            for session in sessions
        ] if day == self.config.target_start else []}
        fresh = SeatSnapshot(total=7, usable=7, mapped_total=7, available_rows=("J",))
        self.watcher.cgv.fetch_seat_snapshot.side_effect = [fresh, FetchError("HTTP 403")]
        result = self.watcher.run_cycle()
        self.assertEqual(self.watcher.cgv.fetch_seat_snapshot.call_count, 2)
        self.assertEqual(result.seat_changes, 1)
        self.assertEqual(result.seat_detail_skipped, 1)
        self.assertEqual(result.successful_dates, 3)
        for i, session in enumerate(sessions):
            key = session.notification_key(site_no="0013", movie_no="30001323")
            self.assertEqual(self.watcher.state.seat_snapshot(key), fresh if i == 0 else self.previous)

    def test_seat_429_backoff_escalates_without_stopping_schedule(self):
        self.watcher.cgv.fetch_seat_snapshot.side_effect = FetchError("HTTP 429")
        for delay in (1800, 3600, 7200, 7200):
            result = self.watcher.run_cycle()
            self.assertEqual(result.successful_dates, 3)
            self.assertEqual(result.rate_limited_requests, 0)
            self.assertEqual(result.seat_rate_limited_requests, 1)
            self.assertEqual(result.seat_changes, 0)
            record = self.watcher.state.seat_api_backoff()
            self.assertEqual(dt.datetime.fromisoformat(record["retry_at"]), self.now + dt.timedelta(seconds=delay))
            self.now += dt.timedelta(seconds=delay)
        self.assertEqual(self.watcher.cgv.fetch_seat_snapshot.call_count, 4)

    def test_repeated_seat_403_backoff_stays_flat(self):
        self.watcher.cgv.fetch_seat_snapshot.side_effect = FetchError("HTTP 403")
        for _ in range(3):
            self.watcher.run_cycle()
            record = self.watcher.state.seat_api_backoff()
            self.assertEqual(dt.datetime.fromisoformat(record["retry_at"]), self.now + dt.timedelta(seconds=1800))
            self.now += dt.timedelta(seconds=1800)

    def test_schedule_block_still_stops_whole_cycle_during_seat_pause(self):
        self.watcher._pause_seat_lookup(403)
        self.watcher.cgv.fetch_date.side_effect = FetchError("HTTP 403")
        result = self.watcher.run_cycle()
        self.assertEqual(result.forbidden_requests, 1)
        self.assertEqual(result.schedule_skipped_dates, 2)
        self.watcher.cgv.fetch_date.assert_called_once()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()

    def test_invalid_seat_timer_blocks_only_seats(self):
        for record in ({"retry_at": "invalid"}, {"retry_at": "2026-08-26T09:00:00"}):
            self.watcher.state.set_seat_api_backoff(record)
            result = self.watcher.run_cycle()
            self.assertEqual(result.successful_dates, 3)
            self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
            self.assertIn("운영자 확인 필요", self.watcher.seat_api_status_text())

    def test_main_keeps_120_second_interval_for_seat_blocks(self):
        config = dataclasses.replace(self.config, dynamic_date_window=True)
        now = [0.0]
        starts = []
        handlers = {}

        def cycle():
            starts.append(now[0])
            now[0] += 5
            if len(starts) == 3:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return CycleResult(3, 0, 3, 0, seat_forbidden_requests=1, seat_rate_limited_requests=1)

        def advance(seconds):
            now[0] += seconds

        with (
            patch("watcher.Config.from_env_file", return_value=config),
            patch("watcher.configure_logging"),
            patch("watcher.Watcher") as factory,
            patch("watcher.signal.signal", side_effect=handlers.__setitem__),
            patch("watcher.time.monotonic", side_effect=lambda: now[0]),
            patch("watcher.time.sleep", side_effect=advance),
        ):
            factory.return_value.run_cycle.side_effect = cycle
            self.assertEqual(main([]), 0)
            factory.return_value.start_delivery_worker.assert_called_once()
        self.assertEqual(starts, [0.0, 120.0, 240.0])


if __name__ == "__main__":
    unittest.main()
