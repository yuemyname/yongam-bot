import dataclasses
import datetime as dt
import gzip
import io
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

from cgv_public_matrix import BASE_HEADERS, VARIANTS, build_cases, probe, retry_after_seconds, run_matrix_once
from test_cgv_wire_trace import FakeTLSSocket
from test_watcher import make_config
from watcher import Config, ConfigurationError, CycleResult, main
import http.client


class PublicMatrixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.config = make_config(self.path)
        self.logger = logging.getLogger(f"matrix-test-{id(self)}")
        self.logger.addHandler(logging.NullHandler())
        self.base_date = dt.date(2026, 9, 30)
        self.compare_date = dt.date(2026, 9, 28)
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def run_matrix(self, **kwargs):
        return run_matrix_once("test-matrix", self.path, self.base_date, self.compare_date,
                               self.logger, sleeper=self.sleep, monotonic=lambda: self.now, **kwargs)

    def test_all_differences_are_individually_tested_plus_combination_and_dates(self):
        cases = build_cases(self.base_date, self.compare_date)
        self.assertEqual(len(cases), 14)
        self.assertEqual(len({case["case"] for case in cases}), 14)
        self.assertEqual(cases[0]["date"], self.base_date)
        self.assertEqual(cases[1]["date"], self.compare_date)
        for case, (_, key, value) in zip(cases[2:11], VARIANTS):
            delta = {k: v for k, v in case["headers"].items() if BASE_HEADERS.get(k) != v}
            self.assertEqual(delta, {key: value})
            self.assertEqual(case["date"], self.compare_date)
        for case in cases:
            self.assertFalse({"authorization", "cookie"} & {k.lower() for k in case["headers"]})
            self.assertEqual(case["headers"]["Referer"], BASE_HEADERS["Referer"])
        self.assertEqual(cases[-1]["headers"], cases[1]["headers"])
        self.assertEqual(cases[11]["headers"], {**BASE_HEADERS, **{k: v for _, k, v in VARIANTS}})

    def test_at_most_14_requests_spaced_30_seconds_and_never_repeated(self):
        times = []
        def fake_probe(case, *_args):
            times.append(self.now)
            return {"case": case["case"], "http_status": 403}
        with patch("cgv_public_matrix.probe", side_effect=fake_probe) as call:
            report = self.run_matrix()
            self.assertEqual(report["requests_sent"], 14)
            self.assertEqual(report["stop_reason"], "completed")
            self.assertTrue(all(b - a >= 30 for a, b in zip(times, times[1:])))
            self.assertIsNone(self.run_matrix())
            self.assertEqual(call.call_count, 14)
        self.assertEqual(self.now, 390)
        files = list((self.path / "cgv-public-matrices").iterdir())
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.stat().st_mode & 0o777 == 0o600 for f in files))

    def test_stops_on_rate_limit_retry_after_challenge_or_error(self):
        for marker in ({"http_status": 429}, {"retry_after_present": True},
                       {"challenge": True}, {"error_type": "TimeoutError"}):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                with patch("cgv_public_matrix.probe", return_value=marker) as call:
                    report = run_matrix_once("test", Path(temporary), self.base_date, self.compare_date,
                                             self.logger, sleeper=self.sleep, monotonic=lambda: self.now)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(report["requests_sent"], 1)
                self.assertNotEqual(report["stop_reason"], "completed")

    def test_shutdown_interrupts_wait_without_another_request(self):
        with patch("cgv_public_matrix.probe", return_value={"http_status": 403}) as call:
            report = self.run_matrix(should_stop=lambda: self.now >= 1)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(report["stop_reason"], "shutdown")

    def test_claim_exists_before_network_and_crash_cannot_restart_matrix(self):
        def crash(*_args):
            self.assertEqual(len(list((self.path / "cgv-public-matrices").glob("*.claim"))), 1)
            raise RuntimeError("crash")
        with patch("cgv_public_matrix.probe", side_effect=crash) as call:
            with self.assertRaises(RuntimeError):
                self.run_matrix()
            self.assertIsNone(self.run_matrix())
        self.assertEqual(call.call_count, 1)

    def test_actual_serialized_request_is_anonymous_and_does_not_follow_redirect(self):
        connection = http.client.HTTPSConnection("cgv.co.kr")
        sock = FakeTLSSocket(302)
        connection.sock = sock
        with patch("cgv_public_matrix.http.client.HTTPSConnection", return_value=connection):
            result = probe(build_cases(self.base_date, self.compare_date)[11], "test", self.logger)
        self.assertEqual(result["http_status"], 302)
        self.assertEqual(len(sock.sent), 1)
        raw = sock.sent[0].decode()
        self.assertIn("scnYmd=20260928", raw)
        self.assertIn("Accept-Encoding: gzip, deflate, br, zstd", raw)
        self.assertIn("Chrome/153.0.0.0", raw)
        self.assertIn("Sec-Fetch-Site: same-origin", raw)
        self.assertNotIn("custNo", raw)
        self.assertNotIn("Authorization:", raw)
        self.assertNotIn("Cookie:", raw)
        self.assertNotIn("hidden-response-secret", json.dumps(result))

    def test_gzip_is_validated_but_unsupported_encoding_is_not_misreported(self):
        for encoding in ("gzip", "br", "zstd"):
            connection = http.client.HTTPSConnection("cgv.co.kr")
            sock = FakeTLSSocket()
            connection.sock = sock
            body = gzip.compress(b'{"statusCode":0,"data":[]}')
            sock.makefile = lambda *_args: io.BytesIO(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: {encoding}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            with patch("cgv_public_matrix.http.client.HTTPSConnection", return_value=connection):
                result = probe(build_cases(self.base_date, self.compare_date)[2], "test", self.logger)
            self.assertEqual(result["http_status"], 200)
            self.assertEqual(result["api_response_valid"], encoding == "gzip")
            self.assertEqual(result["decode_supported"], encoding == "gzip")

    def test_transport_failure_has_no_retry_or_private_exception_text(self):
        connection = Mock()
        connection.request.side_effect = TimeoutError("private-url-or-token")
        with patch("cgv_public_matrix.http.client.HTTPSConnection", return_value=connection):
            result = probe(build_cases(self.base_date, self.compare_date)[0], "test", self.logger)
        self.assertEqual(connection.request.call_count, 1)
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertNotIn("private-", json.dumps(result))

    def test_config_rejects_conflicts_bad_dates_and_changed_targets(self):
        base = {"CGV_PUBLIC_MATRIX_REQUEST_ID": "test", "CGV_PUBLIC_MATRIX_BASE_DATE": "2026-09-30",
                "CGV_PUBLIC_MATRIX_COMPARE_DATE": "2026-09-28"}
        for change in ({"CGV_PUBLIC_MATRIX_REQUEST_ID": "../bad"},
                       {"CGV_PUBLIC_MATRIX_BASE_DATE": ""},
                       {"CGV_PUBLIC_MATRIX_COMPARE_DATE": "2026-09-30"},
                       {"CGV_WIRE_TRACE_REQUEST_ID": "other"},
                       {"CGV_SITE_NO": "9999"}, {"CGV_BOOKING_URL": "https://elsewhere"}):
            with patch.dict(os.environ, {**base, **change}):
                with self.assertRaises(ConfigurationError):
                    Config.from_env_file(self.path / ".env")

    def test_retry_after_accepts_seconds_dates_and_invalid_values(self):
        self.assertEqual(retry_after_seconds("7200"), 7200)
        self.assertEqual(retry_after_seconds("-1"), 0)
        self.assertEqual(retry_after_seconds(""), 0)
        self.assertGreater(retry_after_seconds("Wed, 23 Sep 2099 00:00:00 GMT"), 7200)

    def test_main_keeps_delivery_worker_alive_but_does_not_mix_in_normal_scan(self):
        config = dataclasses.replace(self.config, dynamic_date_window=True,
                                     cgv_public_matrix_request_id="test",
                                     cgv_public_matrix_base_date=self.base_date,
                                     cgv_public_matrix_compare_date=self.compare_date)
        handlers = {}
        watcher = Mock()
        def matrix(*_args, **kwargs):
            watcher.start_delivery_worker.assert_called_once()
            watcher.run_cycle.assert_not_called()
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            self.assertTrue(kwargs["should_stop"]())
            return {"cases": [{"http_status": 403}]}
        with patch("watcher.Config.from_env_file", return_value=config), patch("watcher.Watcher", return_value=watcher), \
             patch("watcher.configure_logging", return_value=self.logger), \
             patch("watcher.signal.signal", side_effect=lambda sig, handler: handlers.update({sig: handler})), \
             patch("watcher.run_matrix_once", side_effect=matrix):
            self.assertEqual(main([]), 0)
        watcher.run_cycle.assert_not_called()
        watcher.stop_delivery_worker.assert_called_once()

    def test_once_does_not_run_matrix(self):
        config = dataclasses.replace(self.config, cgv_public_matrix_request_id="test")
        watcher = Mock()
        watcher.run_cycle.return_value = CycleResult(1, 0, 0, 0)
        with patch("watcher.Config.from_env_file", return_value=config), patch("watcher.Watcher", return_value=watcher), \
             patch("watcher.configure_logging", return_value=self.logger), patch("watcher.run_matrix_once") as matrix:
            self.assertEqual(main(["--once"]), 0)
        matrix.assert_not_called()


if __name__ == "__main__":
    unittest.main()
