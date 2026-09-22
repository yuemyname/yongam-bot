"""Opt-in passive trace of ONE normal schedule request; never sends a request.

Observe the serialized bytes passed to HTTPConnection.send, not the caller's
header dictionary. A completed send is client-side TLS evidence, not a claim
that we can inspect the headers received by CGV. Never enable http.client debug
logging: it would expose credentials, cookies and full response bodies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit


SCHEDULE_PATH = "/api/v1/booking/searchSchByMov"
PUBLIC_HEADERS = frozenset({
    "host", "accept", "accept-encoding", "accept-language", "cache-control",
    "pragma", "referer", "user-agent", "priority", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest",
    "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
    "upgrade-insecure-requests",
})
PUBLIC_QUERY = {
    "coCd": r"[A-Za-z0-9]{1,8}", "siteNo": r"[0-9]{1,6}",
    "scnYmd": r"[0-9]{8}", "movNo": r"[0-9]{1,12}",
    "rtctlScopCd": r"[0-9]{1,4}",
}


def _safe_referer(value: str) -> str:
    parsed = urlsplit(value)
    # Keep only recognized public page URLs, never userinfo, query or fragment.
    if (parsed.scheme != "https" or parsed.netloc != "cgv.co.kr"
            or parsed.path not in {"/cnm/movieBook/movie", "/cnm/selectVisitorCnt"}):
        return "[redacted]"
    suffix = " [query/fragment redacted]" if parsed.query or parsed.fragment else ""
    return f"https://cgv.co.kr{parsed.path}{suffix}"


def summarize_headers(data: bytes) -> dict[str, Any]:
    """Return allowlisted data only; raw bytes never leave this function."""
    if len(data) > 65536 or b"\r\n\r\n" not in data:
        raise ValueError("Unsupported header block")
    lines = data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1").split("\r\n")
    method, target, version = lines[0].split(" ")
    parsed = urlsplit(target)
    if method != "GET" or parsed.path != SCHEDULE_PATH:
        raise ValueError("Not a schedule GET")
    query = {}
    omitted = 0
    customer_id_present = False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        customer_id_present |= key.lower() == "custno"
        if key in PUBLIC_QUERY and re.fullmatch(PUBLIC_QUERY[key], value):
            query[key] = value
        else:
            omitted += 1
    public = {}
    names = set()
    for line in lines[1:]:
        # Folded/malformed lines cannot safely be attributed to a public field.
        if line[:1].isspace() or ":" not in line:
            continue
        name, value = line.split(":", 1)
        name, value = name.lower(), value.strip()
        names.add(name)
        if name not in PUBLIC_HEADERS:
            continue
        if name == "referer":
            value = _safe_referer(value)
        elif name == "host" and value not in {"cgv.co.kr", "cgv.co.kr:443"}:
            value = "[redacted]"
        public[name] = value[:512]
    return {
        "method": method, "endpoint": SCHEDULE_PATH, "protocol": version,
        "query": query, "redacted_query_count": omitted, "headers": public,
        "authorization_present": "authorization" in names,
        "cookie_present": "cookie" in names,
        "customer_id_present": customer_id_present,
    }


class SentHeaderCapture:
    def __init__(self, connection: Any, request_id: str, logger: logging.Logger):
        self.connection = connection
        self.request_id = request_id
        self.logger = logger
        self.original_send = connection.send
        self.had_instance_send = "send" in vars(connection)
        self.seen = False
        connection.send = self.send

    def emit(self, event: str, **fields: Any) -> None:
        # Diagnostics must never turn an otherwise usable request into a failure.
        try:
            self.logger.info("CGV_WIRE_TRACE %s", json.dumps({
                "request_id": self.request_id, "event": event, **fields,
            }, ensure_ascii=False))
        except Exception:
            pass

    def send(self, data: Any) -> Any:
        if self.seen:
            return self.original_send(data)
        self.seen = True
        summary = None
        try:
            if isinstance(data, (bytes, bytearray, memoryview)):
                summary = summarize_headers(bytes(data))
        except Exception:
            pass
        # Delegate exactly once, unmodified. Log "sent" only after send returns.
        result = self.original_send(data)
        if summary is not None:
            self.emit("headers_sent", transport="HTTPS/TLS", **summary)
        else:
            self.emit("headers_unavailable")
        return result

    def response(self, status: int, content_type: str, diagnostics: dict[str, str]) -> None:
        self.emit("response", http_status=status, content_type=content_type[:160],
                  server=diagnostics.get("server", "")[:80],
                  cf_ray=diagnostics.get("cf-ray", "")[:80])

    def error(self, exc: Exception) -> None:
        # Exception text can contain a URL/credential; record the type only.
        self.emit("transport_error", error_type=type(exc).__name__)

    def close(self) -> None:
        if self.had_instance_send:
            self.connection.send = self.original_send
        else:
            del self.connection.send


class WireHeaderTrace:
    def __init__(self, request_id: str, state_dir: Path, logger: logging.Logger):
        self.request_id = request_id
        self.directory = state_dir / "cgv-wire-traces"
        self.logger = logger
        self.checked = False

    def begin(self, connection: Any, url: str) -> SentHeaderCapture | None:
        if not self.request_id or self.checked:
            return None
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "cgv.co.kr"
                or parsed.path != SCHEDULE_PATH):
            return None
        self.checked = True
        # Claim before observing network I/O, even if the attempt later fails.
        # An empty or partial claim still prevents repeats across restarts.
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            key = hashlib.sha256(self.request_id.encode()).hexdigest()
            fd = os.open(self.directory / f"{key}.claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            for path in (self.directory, self.directory.parent):
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return SentHeaderCapture(connection, self.request_id, self.logger)
        except FileExistsError:
            return None
        except Exception as exc:
            # Failed diagnostics must not skip a normal CGV request or expose
            # a filesystem path/exception message with private information.
            try:
                self.logger.warning("CGV_WIRE_TRACE unavailable error_type=%s", type(exc).__name__)
            except Exception:
                pass
            return None
