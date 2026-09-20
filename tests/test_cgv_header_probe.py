import dataclasses
import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.parse

from cgv_header_probe import FETCH_METADATA_HEADERS, run_header_probe_once
from test_watcher import make_config
from watcher import Config, ConfigurationError, StateStore, USER_AGENT, Watcher


class CgvHeaderProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temporary.name)),
            cgv_header_probe_request_id="header-test-1",
            cgv_header_probe_date=dt.date(2026, 9, 30),
            cgv_recovery_request_id="existing-halt",
            subscriptions_enabled=True,
        )
        self.logger = logging.getLogger(f"header-probe-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        state = StateStore(self.config.state_file)
        state.initialize_subscribers(self.config.telegram_chat_id)
        state.set_cgv_recovery({
            "request_id": "existing-halt", "status": "halted",
            "reason": "HTTP 403", "announced_status": "halted",
        })
        state.save()
        self.connection = Mock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = b'{"statusCode":0,"data":[]}'
        self.response.getheader.side_effect = lambda key, default="": {
            "Content-Type": "application/json;charset=UTF-8", "CF-Ray": "test-ray",
            "Location": "https://cgv.co.kr/redirect",
        }.get(key, default)
        connection_patch = patch(
            "cgv_header_probe.http.client.HTTPSConnection", return_value=self.connection
        )
        self.factory = connection_patch.start()
        self.addCleanup(connection_patch.stop)

    def watcher(self, config=None, *, dry_run=False):
        watcher = Watcher(config or self.config, logger=self.logger, dry_run=dry_run)
        watcher.telegram = Mock()
        watcher.cgv = Mock()
        return watcher

    def claim_path(self):
        key = hashlib.sha256(self.config.cgv_header_probe_request_id.encode()).hexdigest()
        return self.config.state_file.parent / "cgv-header-probes" / f"{key}.claim.json"

    def test_success_has_exact_headers_and_durable_claim_but_never_resumes(self):
        def check_claim(*args, **kwargs):
            self.assertTrue(self.claim_path().exists())
            state = StateStore(self.config.state_file)
            state.load()
            self.assertEqual(state.cgv_recovery()["status"], "halted")

        self.connection.request.side_effect = check_claim
        before = self.config.state_file.read_bytes()
        watcher = self.watcher()
        with self.assertLogs(self.logger, level="INFO") as captured:
            self.assertTrue(watcher.run_cycle().cgv_paused)
        self.assertTrue(any('"schedule_response_valid": true' in line for line in captured.output))
        self.connection.request.assert_called_once()
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args[0], "GET")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(args[1]).query)
        self.assertEqual(query["scnYmd"], ["20260930"])
        self.assertNotIn("custNo", query)
        self.assertEqual(kwargs["headers"], {
            "Accept": "application/json", "Accept-Language": "ko-KR",
            "Cache-Control": "no-cache", "Pragma": "no-cache",
            "Referer": self.config.booking_url, "User-Agent": USER_AGENT,
            **FETCH_METADATA_HEADERS,
        })
        watcher.cgv.fetch_date.assert_not_called()
        watcher.cgv.fetch_seat_snapshot.assert_not_called()
        watcher.telegram.send_message.assert_not_called()
        self.assertEqual(self.config.state_file.read_bytes(), before)
        self.assertTrue(watcher.run_cycle().cgv_paused)
        self.assertTrue(self.watcher().run_cycle().cgv_paused)
        self.connection.request.assert_called_once()

    def test_removing_diagnostic_variables_keeps_persistent_halt(self):
        self.assertTrue(self.watcher().run_cycle().cgv_paused)
        restarted = self.watcher(dataclasses.replace(
            self.config, cgv_header_probe_request_id="", cgv_header_probe_date=None
        ))
        self.assertTrue(restarted.run_cycle().cgv_paused)
        restarted.cgv.fetch_date.assert_not_called()
        self.connection.request.assert_called_once()

    def test_halts_a_pending_recovery_before_diagnostic(self):
        watcher = self.watcher(dataclasses.replace(
            self.config, cgv_recovery_request_id="new-recovery",
            cgv_recovery_pause_seconds=0,
        ))
        self.assertEqual(watcher.state.cgv_recovery()["status"], "cooldown")
        self.assertTrue(watcher.run_cycle().cgv_paused)
        self.assertEqual(watcher.state.cgv_recovery()["status"], "halted")
        watcher.cgv.fetch_date.assert_not_called()

    def test_403_and_redirect_each_make_only_one_request(self):
        for status in (403, 302):
            with self.subTest(status=status):
                config = dataclasses.replace(self.config, cgv_header_probe_request_id=f"http-{status}")
                self.response.status = status
                self.response.read.return_value = b'<html>cloudflare secret-body</html>'
                self.connection.request.reset_mock()
                with self.assertLogs(self.logger, level="INFO") as captured:
                    self.assertTrue(self.watcher(config).run_cycle().cgv_paused)
                self.assertNotIn("secret-body", "\n".join(captured.output))
                self.assertTrue(self.watcher(config).run_cycle().cgv_paused)
                self.connection.request.assert_called_once()

    def test_transport_error_does_not_log_exception_text_or_retry(self):
        self.connection.getresponse.side_effect = TimeoutError("secret-token-in-error")
        with self.assertLogs(self.logger, level="INFO") as captured:
            self.assertTrue(self.watcher().run_cycle().cgv_paused)
        output = "\n".join(captured.output)
        self.assertIn("TimeoutError", output)
        self.assertNotIn("secret-token-in-error", output)
        self.assertTrue(self.watcher().run_cycle().cgv_paused)
        self.connection.request.assert_called_once()

    def test_partial_claim_from_crash_prevents_request(self):
        self.claim_path().parent.mkdir()
        self.claim_path().write_text("", encoding="utf-8")
        self.assertTrue(self.watcher().run_cycle().cgv_paused)
        self.factory.assert_not_called()

    def test_changed_date_does_not_rearm_same_id(self):
        self.assertTrue(self.watcher().run_cycle().cgv_paused)
        config = dataclasses.replace(self.config, cgv_header_probe_date=dt.date(2026, 10, 1))
        self.assertTrue(self.watcher(config).run_cycle().cgv_paused)
        self.connection.request.assert_called_once()

    def test_claim_sync_failure_never_sends_request(self):
        with patch("cgv_header_probe.os.fsync", side_effect=OSError("disk error")):
            self.assertTrue(self.watcher().run_cycle().cgv_paused)
        self.factory.assert_not_called()

    def test_result_write_failure_still_cannot_repeat_request(self):
        original = Path.open

        def fail_result(path, *args, **kwargs):
            if path.name.endswith(".result.json"):
                raise OSError("disk full")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", fail_result):
            self.assertTrue(self.watcher().run_cycle().cgv_paused)
        self.assertTrue(self.watcher().run_cycle().cgv_paused)
        self.connection.request.assert_called_once()

    def test_dry_run_does_not_consume_claim(self):
        self.assertTrue(self.watcher(dry_run=True).run_cycle().cgv_paused)
        self.assertFalse(self.claim_path().exists())
        self.factory.assert_not_called()

    def test_unexpected_endpoint_is_rejected_without_request(self):
        config = dataclasses.replace(self.config, api_url="https://example.com/schedule")
        self.assertTrue(self.watcher(config).run_cycle().cgv_paused)
        self.factory.assert_not_called()

    def test_disabled_mode_never_creates_probe_claims(self):
        watcher = self.watcher(dataclasses.replace(self.config, cgv_header_probe_request_id=""))
        self.assertTrue(watcher.run_cycle().cgv_paused)
        self.factory.assert_not_called()
        self.assertFalse(self.claim_path().exists())

    def test_status_commands_remain_available(self):
        watcher = self.watcher()
        self.assertTrue(watcher.run_cycle().cgv_paused)
        watcher.telegram.get_updates.return_value = [{
            "update_id": 1, "message": {
                "chat": {"id": 987654, "type": "private"}, "text": "/status",
            },
        }]
        self.assertTrue(watcher.sync_subscribers())
        self.assertIn("CGV 자동 조회 중단", watcher.telegram.send_message.call_args.args[0])

    def test_config_requires_explicit_date_and_valid_id(self):
        for values in (
            {"CGV_HEADER_PROBE_REQUEST_ID": "approved-test", "CGV_HEADER_PROBE_DATE": ""},
            {"CGV_HEADER_PROBE_REQUEST_ID": "../bad", "CGV_HEADER_PROBE_DATE": "2026-09-30"},
        ):
            with self.subTest(values=values), patch.dict("os.environ", values):
                with self.assertRaises(ConfigurationError):
                    make_config(self.config.project_dir)
        with patch.dict("os.environ", {
            "CGV_HEADER_PROBE_REQUEST_ID": "approved-test", "CGV_HEADER_PROBE_DATE": "2026-09-30",
        }):
            self.assertEqual(make_config(self.config.project_dir).cgv_header_probe_date, dt.date(2026, 9, 30))

    def test_invalid_json_is_reported_without_exposing_body(self):
        self.response.read.return_value = b'{"secret":'
        report = run_header_probe_once(self.config, self.logger, user_agent=USER_AGENT)
        self.assertTrue(report["json_parse_failed"])
        self.assertFalse(report["schedule_response_valid"])
        self.assertNotIn("secret", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
