import concurrent.futures
import dataclasses
import datetime as dt
import io
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

from watcher import (
    ALERT_OPEN, ALERT_REPLY, ALERT_SEATS, Config, StateStore, TelegramClient,
    TelegramDeferred, TelegramError, Watcher, _telegram_retry_after,
)
from test_watcher import make_config, _kst


class TelegramResponseTests(unittest.TestCase):
    def test_http_429_preserves_retry_after(self):
        payload = {"ok": False, "description": "Too Many Requests: retry after 30000",
                   "parameters": {"retry_after": 30000}}
        error = urllib.error.HTTPError("https://example.invalid", 429, "limit", {},
                                       io.BytesIO(json.dumps(payload).encode()))
        self.addCleanup(error.close)
        client = TelegramClient("fake-token", "123")
        with patch("watcher.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramError) as caught:
                client.send_message("test")
        self.assertTrue(caught.exception.rate_limited)
        self.assertEqual(caught.exception.retry_after_seconds, 30000)

    def test_json_error_with_http_200_also_preserves_429(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({
            "ok": False, "error_code": 429, "description": "slow down",
            "parameters": {"retry_after": 99},
        }).encode()
        with patch("watcher.urllib.request.urlopen", return_value=response):
            with self.assertRaises(TelegramError) as caught:
                TelegramClient("fake-token", "123").send_message("test")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after_seconds, 99)

    def test_parse_prefers_parameters_and_falls_back_to_description(self):
        self.assertEqual(_telegram_retry_after({"parameters": {"retry_after": 0}}), 0)
        self.assertEqual(_telegram_retry_after({"parameters": {"retry_after": 120},
                                                "description": "retry after 5"}), 120)
        self.assertEqual(_telegram_retry_after({"description": "Too Many Requests: retry after 86400"}), 86400)
        for value in (True, -1, 1.5, "3", None):
            self.assertIsNone(_telegram_retry_after({"parameters": {"retry_after": value}}))
        self.assertIsNone(_telegram_retry_after([1, 2]))

    def test_malformed_http_body_still_reports_429(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limit", {}, io.BytesIO(b"bad"))
        self.addCleanup(error.close)
        with patch("watcher.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramError) as caught:
                TelegramClient("fake-token", "123").send_message("test")
        self.assertTrue(caught.exception.rate_limited)
        self.assertIsNone(caught.exception.retry_after_seconds)

    def test_private_and_group_pacing_prevents_requests_without_sleeping(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true}'
        for chat, interval in (("123", 1.1), ("-123", 3.1)):
            client = TelegramClient("fake-token", chat)
            with patch("watcher.urllib.request.urlopen", return_value=response) as request:
                with patch("watcher.time.monotonic", return_value=100):
                    client.send_message("first")
                    with self.assertRaises(TelegramDeferred):
                        client.send_message("second")
                    client.send_message("other chat", chat_id="456")
                self.assertEqual(request.call_count, 2)
                with patch("watcher.time.monotonic", return_value=100 + interval):
                    client.send_message("later")
                self.assertEqual(request.call_count, 3)


class FloodWaitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = dataclasses.replace(make_config(Path(tmp.name)),
                                           subscriptions_enabled=True, telegram_broadcast_workers=1)
        self.logger = logging.getLogger(f"telegram-flood-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.watcher = self.restart()
        self.chat = self.config.telegram_chat_id

    def restart(self):
        watcher = Watcher(self.config, logger=self.logger)
        watcher.telegram = Mock()
        watcher.telegram.get_updates.return_value = []
        watcher.cgv = Mock()
        return watcher

    def pause(self, seconds=3600):
        now = dt.datetime.now(dt.timezone.utc)
        self.watcher.state.pause_telegram_sends(now + dt.timedelta(seconds=seconds), now)
        self.watcher.state.save()

    def finish_pause(self):
        self.watcher.state.data["telegram_send_backoff"]["retry_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        ).isoformat()

    def command(self, text):
        self.watcher.telegram.get_updates.return_value = [{
            "update_id": self.watcher.state.telegram_update_offset,
            "message": {"text": text, "chat": {"id": self.chat, "type": "private"}},
        }]
        self.assertTrue(self.watcher.sync_subscribers())

    def test_429_pauses_all_sends_and_preserves_attempts_even_at_limit(self):
        state = self.watcher.state
        for text in ("first", "second", "third"):
            state.queue_pending_delivery(self.chat, text, ALERT_OPEN)
        for record in state.data["pending_deliveries"].values():
            record["attempts"] = self.config.pending_delivery_max_attempts - 1
        self.watcher.telegram.send_message.side_effect = TelegramError(
            "limited", status_code=429, retry_after_seconds=30000,
        )
        before = dt.datetime.now(dt.timezone.utc)
        self.watcher._retry_pending_deliveries()
        self.assertEqual(self.watcher.telegram.send_message.call_count, 1)
        self.assertEqual(state.pending_delivery_count(), 3)
        self.assertGreaterEqual((state.telegram_send_retry_at() - before).total_seconds(), 30001)
        for record in state.data["pending_deliveries"].values():
            self.assertEqual(record["attempts"], self.config.pending_delivery_max_attempts - 1)
        self.watcher._retry_pending_deliveries()
        self.assertEqual(self.watcher.telegram.send_message.call_count, 1)

    def test_wait_is_durable_and_restarting_cannot_reset_it(self):
        self.pause(30000)
        retry_at = self.watcher.state.telegram_send_retry_at()
        self.watcher = self.restart()
        self.assertEqual(self.watcher.state.telegram_send_retry_at(), retry_at)
        self.assertEqual(self.watcher._broadcast_message("open", category=ALERT_OPEN), (1, 0, 1))
        self.watcher.telegram.send_message.assert_not_called()
        self.assertEqual(self.watcher.state.pending_delivery_count(), 1)

    def test_missing_retry_after_uses_safe_fallback_and_non429_stays_transient(self):
        self.watcher.telegram.send_message.side_effect = TelegramError("limited", status_code=429)
        before = dt.datetime.now(dt.timezone.utc)
        with self.assertRaises(TelegramDeferred):
            self.watcher._send_telegram("open", chat_id=self.chat)
        self.assertGreaterEqual((self.watcher.state.telegram_send_retry_at() - before).total_seconds(), 61)
        self.finish_pause()
        self.watcher.telegram.send_message.side_effect = TelegramError("server", status_code=500)
        self.watcher.state.queue_pending_delivery(self.chat, "open", ALERT_OPEN)
        self.watcher._retry_pending_deliveries()
        self.assertEqual(self.watcher.state.pending_deliveries()[0][1]["attempts"], 1)

    def test_ttl_excludes_wait_once_and_new_messages_get_remaining_wait(self):
        state = self.watcher.state
        now = dt.datetime.now(dt.timezone.utc)
        state.queue_pending_delivery(self.chat, "old", ALERT_OPEN)
        old = next(iter(state.data["pending_deliveries"].values()))
        old["queued_at"] = (now - dt.timedelta(hours=23)).isoformat()
        state.pause_telegram_sends(now + dt.timedelta(hours=10), now)
        expiry = dt.datetime.fromisoformat(old["expires_at"])
        self.assertEqual(expiry, now + dt.timedelta(hours=11))
        self.assertFalse(state.pause_telegram_sends(now + dt.timedelta(hours=5), now))
        self.assertEqual(dt.datetime.fromisoformat(old["expires_at"]), expiry)
        state.pause_telegram_sends(now + dt.timedelta(hours=12), now + dt.timedelta(minutes=1))
        self.assertEqual(dt.datetime.fromisoformat(old["expires_at"]), now + dt.timedelta(hours=13))
        state.queue_pending_delivery(self.chat, "new", ALERT_OPEN)
        fresh = next(v for v in state.data["pending_deliveries"].values() if v["text"] == "new")
        self.assertEqual(dt.datetime.fromisoformat(fresh["expires_at"]), now + dt.timedelta(hours=36))
        state.save()
        self.watcher = self.restart()
        self.assertEqual(self.watcher.state.prune_pending_deliveries(now + dt.timedelta(hours=12)), 0)

    def test_concurrent_429s_keep_the_longest_deadline(self):
        self.config = dataclasses.replace(self.config, telegram_broadcast_workers=2)
        self.watcher = self.restart()
        barrier = threading.Barrier(2)
        def send(text, **kwargs):
            barrier.wait(timeout=2)
            raise TelegramError("limited", status_code=429, retry_after_seconds=int(text))
        self.watcher.telegram.send_message.side_effect = send
        before = dt.datetime.now(dt.timezone.utc)
        def attempt(seconds):
            with self.assertRaises(TelegramDeferred):
                self.watcher._send_telegram(str(seconds), chat_id=str(seconds))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(attempt, (10, 30000)))
        self.assertGreaterEqual((self.watcher.state.telegram_send_retry_at() - before).total_seconds(), 30001)
        loaded = StateStore(self.config.state_file)
        loaded.load()
        self.assertEqual(loaded.telegram_send_retry_at(), self.watcher.state.telegram_send_retry_at())

    def test_commands_apply_during_pause_and_reply_survives_unsubscribe(self):
        self.pause()
        self.command("/date 20261003")
        self.assertEqual(self.watcher.state.seat_date_range(self.chat), ("2026-10-03", None))
        self.command("/stop")
        self.assertFalse(self.watcher.state.is_subscribed(self.chat))
        self.watcher.telegram.send_message.assert_not_called()
        self.watcher = self.restart()
        self.assertEqual(self.watcher.state.pending_delivery_count(), 2)
        self.assertTrue(all(v["category"] == ALERT_REPLY for _, v in self.watcher.state.pending_deliveries()))
        self.finish_pause()
        self.watcher._retry_pending_deliveries()
        self.assertEqual(self.watcher.telegram.send_message.call_count, 2)
        self.assertEqual(self.watcher.state.pending_delivery_count(), 0)

    def test_cgv_scan_and_new_open_queue_continue_without_sends_during_pause(self):
        self.pause()
        self.watcher.cgv.fetch_date.return_value = {"data": [{
            "scnsNm": "IMAX관", "scnYmd": "20260826", "scnsrtTm": "1430",
            "scnsNo": "018", "scnSseq": "4", "frSeatCnt": 10, "stcnt": 624,
        }]}
        with patch.object(Config, "local_now", return_value=_kst(dt.date(2026, 8, 26))):
            self.assertEqual(self.watcher.run_cycle().new_sessions, 1)
        self.watcher.cgv.fetch_date.assert_called_once()
        self.watcher.cgv.fetch_seat_snapshot.assert_not_called()
        self.watcher.telegram.send_message.assert_not_called()
        self.assertEqual(self.watcher.state.pending_delivery_count(), 1)

    def test_delivery_worker_wait_is_interruptible_and_resumes_openings_first(self):
        self.pause()
        state = self.watcher.state
        for category in (ALERT_SEATS, ALERT_REPLY, ALERT_OPEN):
            state.queue_pending_delivery(self.chat, category, category)
        self.watcher.start_delivery_worker()
        time.sleep(0.03)
        started = time.monotonic()
        self.watcher.stop_delivery_worker(timeout=1)
        self.assertLess(time.monotonic() - started, 1)
        self.watcher.telegram.send_message.assert_not_called()
        self.assertEqual(state.pending_delivery_count(), 3)
        self.finish_pause()
        self.watcher.start_delivery_worker()
        try:
            deadline = time.monotonic() + 2
            while state.pending_delivery_count() and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            self.watcher.stop_delivery_worker(timeout=1)
        self.assertEqual([call.args[0] for call in self.watcher.telegram.send_message.call_args_list],
                         [ALERT_OPEN, ALERT_REPLY, ALERT_SEATS])

    def test_pacing_deferral_preserves_message_without_starting_global_pause(self):
        self.watcher.state.queue_pending_delivery(self.chat, "seat", ALERT_SEATS)
        self.watcher.telegram.send_message.side_effect = TelegramDeferred(
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1),
        )
        self.watcher._retry_pending_deliveries()
        record = self.watcher.state.pending_deliveries()[0][1]
        self.assertEqual(record["attempts"], 0)
        self.assertTrue(record["next_attempt_at"])
        self.assertIsNone(self.watcher.state.telegram_send_retry_at())

    def test_corrupt_persisted_wait_fails_closed_instead_of_sending(self):
        self.watcher.state.data["telegram_send_backoff"] = {"retry_at": "bad"}
        self.watcher.state.save()
        with self.assertRaises(ValueError):
            self.restart()


if __name__ == "__main__":
    unittest.main()
