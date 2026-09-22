import dataclasses
import datetime as dt
import http.client
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cgv_wire_trace import SCHEDULE_PATH, WireHeaderTrace, summarize_headers
from test_watcher import make_config
from watcher import CgvClient, Config, ConfigurationError, FetchError


class FakeTLSSocket:
    """Use the real HTTP serializer and response parser without any network."""
    def __init__(self, status=200):
        self.sent = []
        self.status = status

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, *_args):
        body = b'{"statusCode":0,"data":[]}'
        return io.BytesIO(
            f"HTTP/1.1 {self.status} Result\r\nContent-Length: {len(body)}\r\n".encode()
            + b"Content-Type: application/json\r\nServer: cloudflare\r\n"
            + b"CF-Ray: safe-test-ray\r\nSet-Cookie: hidden-response-secret\r\n\r\n" + body
        )

    def close(self):
        pass


class WireTraceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = dataclasses.replace(
            make_config(Path(temp.name)), cgv_wire_trace_request_id="wire-test-1",
        )
        self.logger = logging.getLogger("cgv_watcher")

    def connection(self, status=200):
        connection = http.client.HTTPSConnection("cgv.co.kr")
        sock = FakeTLSSocket(status)
        connection.sock = sock
        return connection, sock

    def request(self, config=None, status=200):
        client = CgvClient(config or self.config)
        connection, sock = self.connection(status)
        with patch.object(client, "_connection_for", return_value=connection):
            client.fetch_date(dt.date(2026, 9, 30), single_attempt=True)
        return client, connection, sock

    def records(self, captured):
        return [json.loads(line.split("CGV_WIRE_TRACE ", 1)[1])
                for line in captured.output if "CGV_WIRE_TRACE {" in line]

    def test_observes_actual_serialized_send_and_matching_response_without_changes(self):
        disabled = dataclasses.replace(self.config, cgv_wire_trace_request_id="")
        _, _, baseline = self.request(disabled)
        with self.assertLogs(self.logger, level="INFO") as captured:
            _, connection, sock = self.request()
        self.assertEqual(sock.sent, baseline.sent)
        self.assertEqual(len(sock.sent), 1)
        self.assertNotIn("send", vars(connection))
        request, response = self.records(captured)
        self.assertEqual(request["event"], "headers_sent")
        self.assertEqual(request["headers"]["referer"], "https://cgv.co.kr/cnm/movieBook/movie")
        # These fields are added by http.client, not passed in the caller dict.
        self.assertEqual(request["headers"]["host"], "cgv.co.kr")
        self.assertEqual(request["headers"]["accept-encoding"], "identity")
        self.assertEqual(request["protocol"], "HTTP/1.1")
        self.assertFalse(request["authorization_present"])
        self.assertFalse(request["cookie_present"])
        self.assertFalse(request["customer_id_present"])
        self.assertEqual(response["request_id"], request["request_id"])
        self.assertEqual(response["http_status"], 200)
        self.assertNotIn("hidden-response-secret", "\n".join(captured.output))

    def test_disabled_has_no_claim_or_logs(self):
        with self.assertNoLogs(self.logger, level="INFO"):
            self.request(dataclasses.replace(self.config, cgv_wire_trace_request_id=""))
        self.assertFalse((self.config.state_file.parent / "cgv-wire-traces").exists())

    def test_same_id_is_not_repeated_across_clients_but_requests_continue(self):
        with self.assertLogs(self.logger, level="INFO") as captured:
            self.request()
            _, _, second = self.request()
        self.assertEqual(len(self.records(captured)), 2)
        self.assertEqual(len(second.sent), 1)
        claims = list((self.config.state_file.parent / "cgv-wire-traces").iterdir())
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].read_bytes(), b"")
        self.assertEqual(claims[0].stat().st_mode & 0o777, 0o600)

    def test_connection_reuse_remains_intact(self):
        client = CgvClient(self.config)
        connection, sock = self.connection()
        with self.assertLogs(self.logger, level="INFO") as captured:
            with patch.object(client, "_connection_for", return_value=connection):
                client.fetch_date(dt.date(2026, 9, 30))
                client.fetch_date(dt.date(2026, 10, 1))
        self.assertEqual(len(sock.sent), 2)
        self.assertEqual(len(self.records(captured)), 2)

    def test_forbidden_is_still_forbidden_without_an_extra_request(self):
        client = CgvClient(self.config)
        connection, sock = self.connection(403)
        with self.assertLogs(self.logger, level="INFO") as captured:
            with patch.object(client, "_connection_for", return_value=connection):
                with self.assertRaisesRegex(FetchError, "HTTP 403"):
                    client.fetch_date(dt.date(2026, 9, 30))
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(self.records(captured)[1]["http_status"], 403)

    def test_credentials_unknown_values_and_referer_query_are_never_logged(self):
        raw = (
            f"GET {SCHEDULE_PATH}?coCd=A420&scnYmd=20260930&custNo=private-customer&"
            "accessToken=private-query&siteNo=private-invalid-site HTTP/1.1\r\n"
            "Authorization: Bearer private-auth\r\nCookie: private-cookie\r\n"
            "Proxy-Authorization: private-proxy\r\nX-Token: private-custom\r\n"
            "Referer: https://cgv.co.kr/cnm/movieBook/movie?token=private-ref#private-fragment\r\n"
            "Accept: application/json\r\n\r\nprivate-body"
        ).encode()
        summary = summarize_headers(raw)
        self.assertNotIn("private-", json.dumps(summary))
        self.assertTrue(summary["authorization_present"])
        self.assertTrue(summary["cookie_present"])
        self.assertTrue(summary["customer_id_present"])
        self.assertEqual(summary["redacted_query_count"], 3)
        self.assertEqual(summary["query"], {"coCd": "A420", "scnYmd": "20260930"})
        for referer in ("https://user:private@cgv.co.kr/cnm/movieBook/movie", "https://elsewhere/private"):
            result = summarize_headers(f"GET {SCHEDULE_PATH} HTTP/1.1\r\nReferer: {referer}\r\n\r\n".encode())
            self.assertEqual(result["headers"]["referer"], "[redacted]")

    def test_failed_send_does_not_claim_headers_were_sent_and_does_not_repeat(self):
        client = CgvClient(self.config)
        connection, sock = self.connection()
        with self.assertLogs(self.logger, level="INFO") as captured:
            with patch.object(client, "_connection_for", return_value=connection), patch.object(
                sock, "sendall", side_effect=OSError("private-exception-secret")
            ):
                with self.assertRaises(OSError):
                    client._request("https://cgv.co.kr" + SCHEDULE_PATH, headers={}, attempts=1)
            self.request()  # Same ID already claimed despite failure.
        records = self.records(captured)
        self.assertEqual([r["event"] for r in records], ["transport_error"])
        self.assertNotIn("private-exception-secret", "\n".join(captured.output))
        self.assertNotIn("send", vars(connection))

    def test_capture_failure_does_not_change_the_normal_response(self):
        with patch("cgv_wire_trace.summarize_headers", side_effect=ValueError("private-error")):
            with self.assertLogs(self.logger, level="INFO") as captured:
                _, _, sock = self.request()
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(self.records(captured)[0]["event"], "headers_unavailable")
        self.assertNotIn("private-error", "\n".join(captured.output))

    def test_claim_failure_leaves_normal_request_unchanged(self):
        with patch("cgv_wire_trace.os.open", side_effect=OSError("private-path")):
            with self.assertLogs(self.logger, level="WARNING") as captured:
                _, _, sock = self.request()
        self.assertEqual(len(sock.sent), 1)
        self.assertNotIn("private-path", "\n".join(captured.output))

    def test_logger_failure_does_not_change_network_behavior(self):
        with patch.object(self.logger, "info", side_effect=RuntimeError("logger unavailable")):
            _, _, sock = self.request()
        self.assertEqual(len(sock.sent), 1)

    def test_only_normal_https_schedule_is_observed(self):
        trace = WireHeaderTrace("test", self.config.state_file.parent, self.logger)
        connection, _ = self.connection()
        for url in (
            "http://cgv.co.kr" + SCHEDULE_PATH,
            "https://elsewhere" + SCHEDULE_PATH,
            "https://private@cgv.co.kr" + SCHEDULE_PATH,
            "https://cgv.co.kr/api/v1/booking/searchIfSeatData",
            "https://api.telegram.org/test",
        ):
            self.assertIsNone(trace.begin(connection, url))
        self.assertFalse(trace.directory.exists())

    def test_config_validates_id(self):
        for value in ("with space", "../unsafe", "x" * 97):
            with patch.dict(os.environ, {"CGV_WIRE_TRACE_REQUEST_ID": value}):
                with self.assertRaises(ConfigurationError):
                    Config.from_env_file(self.config.project_dir / ".env")


if __name__ == "__main__":
    unittest.main()
