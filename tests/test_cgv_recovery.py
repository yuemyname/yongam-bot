import dataclasses
import datetime as dt
import logging
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

from watcher import CgvClient, Config, CycleResult, FetchError, Watcher, main
from test_watcher import make_config, _kst


class CgvRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temporary.name)),
            cgv_recovery_request_id="recovery-test-1",
            subscriptions_enabled=True,
        )
        self.started = _kst(dt.date(2026, 8, 26))
        self.now = self.started
        clock = patch.object(Config, "local_now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.logger = logging.getLogger(f"recovery-test-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = self.restart()

    def restart(self, config=None, *, dry_run=False):
        watcher = Watcher(config or self.config, logger=self.logger, dry_run=dry_run)
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def due(self):
        self.now = self.started + dt.timedelta(hours=1)

    def test_no_requests_before_the_full_hour_and_only_operator_is_notified(self):
        self.watcher.state.add_subscriber("123")
        for seconds in (0, 120, 1800, 3599):
            self.now = self.started + dt.timedelta(seconds=seconds)
            self.assertTrue(self.watcher.run_cycle().cgv_paused)
        self.watcher.cgv.fetch_date.assert_not_called()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        self.watcher.telegram.send_message.assert_called_once()
        self.assertEqual(
            self.watcher.telegram.send_message.call_args.kwargs["chat_id"], "987654"
        )
        self.assertEqual(self.watcher._cycle_index, 0)

    def test_restart_keeps_deadline_and_failed_probe_never_repeats(self):
        deadline = self.watcher.state.cgv_recovery()["probe_at"]
        self.now += dt.timedelta(minutes=30)
        self.watcher = self.restart()
        self.assertEqual(self.watcher.state.cgv_recovery()["probe_at"], deadline)
        self.assertTrue(self.watcher.run_cycle().cgv_paused)
        self.watcher.cgv.fetch_date.assert_not_called()
        self.due()
        self.watcher.cgv.fetch_date.side_effect = FetchError("HTTP 403")
        self.assertTrue(self.watcher.run_cycle().cgv_paused)
        self.watcher.cgv.fetch_date.assert_called_once_with(
            self.config.target_start, single_attempt=True
        )
        self.assertEqual(self.watcher.state.cgv_recovery()["status"], "halted")

        # Removing the activation variable or restarting cannot lift the latch.
        self.now += dt.timedelta(hours=24)
        restarted = self.restart(dataclasses.replace(self.config, cgv_recovery_request_id=""))
        for _ in range(3):
            self.assertTrue(restarted.run_cycle().cgv_paused)
        restarted.cgv.fetch_date.assert_not_called()
        restarted.cgv.fetch_seat_snapshot.assert_not_called()

    def test_new_explicit_request_starts_another_full_hour(self):
        self.watcher._halt_cgv_recovery("HTTP 403")
        self.now += dt.timedelta(hours=2)
        restarted = self.restart(dataclasses.replace(
            self.config, cgv_recovery_request_id="recovery-test-2"
        ))
        state = restarted.state.cgv_recovery()
        self.assertEqual(state["status"], "cooldown")
        self.assertEqual(
            dt.datetime.fromisoformat(state["probe_at"]), self.now + dt.timedelta(hours=1)
        )
        self.assertTrue(restarted.run_cycle().cgv_paused)
        restarted.cgv.fetch_date.assert_not_called()

    def test_successful_empty_schedule_is_reused_and_scanning_resumes(self):
        self.due()
        self.watcher.state.note_schedule_failure(self.config.target_start)
        self.watcher.cgv.fetch_date.return_value = {"statusCode": 0, "data": []}
        result = self.watcher.run_cycle()
        self.assertFalse(result.cgv_paused)
        self.assertEqual(result.successful_dates, 1)
        self.assertEqual(self.watcher.state.failed_schedule_dates(), ())
        self.watcher.cgv.fetch_date.assert_called_once_with(
            self.config.target_start, single_attempt=True
        )
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        self.assertEqual(self.watcher.state.cgv_recovery()["status"], "recovered")

        restarted = self.restart()
        restarted.cgv.fetch_date.return_value = {"statusCode": 0, "data": []}
        self.assertEqual(restarted.run_cycle().successful_dates, 1)
        restarted.cgv.fetch_date.assert_called_once_with(self.config.target_start)

    def test_successful_probe_does_not_lose_a_new_opening(self):
        self.due()
        self.watcher.cgv.fetch_date.return_value = {
            "statusCode": "0",
            "data": [{
                "scnsNm": "IMAX관", "scnYmd": "20260826", "scnsrtTm": "1430",
                "scnsNo": "13", "scnSseq": "4", "frSeatCnt": "624", "stcnt": "624",
            }],
        }
        result = self.watcher.run_cycle()
        self.assertEqual(result.new_sessions, 1)
        self.assertEqual(result.successful_dates, 1)
        self.watcher.cgv.fetch_date.assert_called_once()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        texts = [call.args[0] for call in self.watcher.telegram.send_message.call_args_list]
        self.assertTrue(any("14:30" in text for text in texts))

    def test_any_failed_or_invalid_probe_halts_without_retry(self):
        failures = [
            FetchError("HTTP 403"), FetchError("HTTP 429"), FetchError("JSON 오류"),
            TimeoutError(), {}, [], {"statusCode": 1, "data": []},
            {"statusCode": 0, "data": "not a schedule"},
        ]
        for index, failure in enumerate(failures):
            with self.subTest(failure=failure):
                watcher = self.restart(dataclasses.replace(
                    self.config, cgv_recovery_request_id=f"failure-{index}"
                ))
                self.now += dt.timedelta(hours=1)
                if isinstance(failure, Exception):
                    watcher.cgv.fetch_date.side_effect = failure
                else:
                    watcher.cgv.fetch_date.return_value = failure
                self.assertTrue(watcher.run_cycle().cgv_paused)
                self.assertTrue(watcher.run_cycle().cgv_paused)
                watcher.cgv.fetch_date.assert_called_once()
                watcher.cgv.fetch_seat_snapshot.assert_not_called()
                self.assertEqual(watcher.state.cgv_recovery()["status"], "halted")

    def test_crash_after_claim_does_not_send_a_second_probe(self):
        record = self.watcher.state.cgv_recovery()
        record["status"] = "probing"
        self.watcher._save_cgv_recovery(record)
        self.due()
        restarted = self.restart()
        self.assertTrue(restarted.run_cycle().cgv_paused)
        self.assertEqual(restarted.state.cgv_recovery()["status"], "halted")
        restarted.cgv.fetch_date.assert_not_called()

    def test_corrupt_deadline_fails_closed(self):
        record = self.watcher.state.cgv_recovery()
        record["probe_at"] = "not-a-date"
        self.watcher._save_cgv_recovery(record)
        self.assertTrue(self.watcher.run_cycle().cgv_paused)
        self.watcher.cgv.fetch_date.assert_not_called()

    def test_403_after_recovery_halts_normal_scans_too(self):
        self.due()
        self.watcher.cgv.fetch_date.return_value = {"statusCode": 0, "data": []}
        self.assertFalse(self.watcher.run_cycle().cgv_paused)
        self.watcher.cgv.fetch_date.side_effect = FetchError("HTTP 403")
        result = self.watcher.run_cycle()
        self.assertTrue(result.cgv_paused)
        self.assertEqual(result.forbidden_requests, 1)
        self.assertTrue(self.watcher.run_cycle().cgv_paused)
        self.assertEqual(self.watcher.cgv.fetch_date.call_count, 2)

    def test_status_commands_keep_working_while_paused(self):
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": 1, "message": {
                "chat": {"id": 987654, "type": "private"}, "text": "/status",
            },
        }]
        self.assertTrue(self.watcher.sync_subscribers())
        reply = self.watcher.telegram.send_message.call_args.args[0]
        self.assertIn("재확인 예정: 2026-08-26 10:00:00 KST", reply)
        self.assertIn("구독 중", reply)
        self.watcher.cgv.fetch_date.assert_not_called()

    def test_dry_run_never_consumes_the_probe(self):
        self.due()
        restarted = self.restart(dry_run=True)
        self.assertTrue(restarted.run_cycle().cgv_paused)
        restarted.cgv.fetch_date.assert_not_called()
        self.assertEqual(restarted.state.cgv_recovery()["status"], "cooldown")

    def test_single_attempt_does_not_retry_a_transport_failure(self):
        client = CgvClient(self.config)
        connection = Mock()
        connection.getresponse.side_effect = TimeoutError("timeout")
        with patch.object(client, "_connection_for", return_value=connection):
            with self.assertRaises(FetchError):
                client.fetch_date(self.config.target_start, single_attempt=True)
        connection.request.assert_called_once()

    def test_single_attempt_does_not_follow_a_redirect(self):
        client = CgvClient(self.config)
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 302
        response.read.return_value = b""
        response.getheader.side_effect = lambda name, default="": {
            "Location": "https://cgv.co.kr/another-path",
            "Content-Type": "text/html",
        }.get(name, default)
        with patch.object(client, "_connection_for", return_value=connection):
            with self.assertRaisesRegex(FetchError, "리디렉션"):
                client.fetch_date(self.config.target_start, single_attempt=True)
        connection.request.assert_called_once()

    def test_main_keeps_telegram_workers_running_and_normal_interval_after_recovery(self):
        config = dataclasses.replace(self.config, dynamic_date_window=True)
        now = [0.0]
        starts = []
        handlers = {}

        def cycle():
            starts.append(now[0])
            if len(starts) == 4:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return CycleResult(0, 0, 0, 0, cgv_paused=len(starts) < 3)

        def advance(seconds):
            now[0] += seconds

        with (
            patch("watcher.Config.from_env_file", return_value=config),
            patch("watcher.configure_logging"),
            patch("watcher.Watcher") as factory,
            patch("watcher.threading.Thread") as thread,
            patch("watcher.signal.signal", side_effect=handlers.__setitem__),
            patch("watcher.time.monotonic", side_effect=lambda: now[0]),
            patch("watcher.time.sleep", side_effect=advance),
        ):
            factory.return_value.run_cycle.side_effect = cycle
            self.assertEqual(main([]), 0)
            factory.return_value.start_delivery_worker.assert_called_once()
            thread.return_value.start.assert_called_once()
            factory.return_value.stop_delivery_worker.assert_called_once()
        self.assertEqual(starts, [0.0, 1.0, 2.0, 122.0])


if __name__ == "__main__":
    unittest.main()
