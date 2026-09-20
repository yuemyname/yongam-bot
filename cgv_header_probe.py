"""Opt-in, at-most-once CGV header diagnostic. Never resumes monitoring."""

from __future__ import annotations

import hashlib
import gzip
import http.client
import io
import json
import logging
import os
from pathlib import Path
import ssl
import time
from typing import TYPE_CHECKING, Any
import urllib.parse
import zlib

if TYPE_CHECKING:
    from watcher import Config


FETCH_METADATA_HEADERS = {
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
MAX_RESPONSE_BYTES = 5_000_000
SEAT_PATH = "/api/v1/booking/searchIfSeatData"
SEAT_PUBLIC_HEADERS = {
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://cgv.co.kr/cnm/selectVisitorCnt",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
    "Priority": "u=1, i",
    "Sec-CH-UA": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
}


def _build_request(config: Config, user_agent: str) -> tuple[str, dict[str, str], dict[str, str]]:
    headers = {
        "Accept": "application/json", "Accept-Language": "ko-KR",
        "Cache-Control": "no-cache", "Pragma": "no-cache",
        "Referer": config.booking_url, "User-Agent": user_agent,
        **FETCH_METADATA_HEADERS,
    }
    if config.cgv_header_probe_seat_url:
        url = urllib.parse.urlsplit(config.cgv_header_probe_seat_url)
        if (url.scheme != "https" or url.netloc != "cgv.co.kr"
                or url.path != SEAT_PATH or url.fragment):
            raise ValueError("Unexpected seat diagnostic endpoint")
        pairs = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
        query = dict(pairs)
        # Reject ALL extra fields, notably custNo, tokens and duplicate keys.
        expected = {"coCd", "siteNo", "scnYmd", "scnsNo", "scnSseq", "seatAreaNo", "cusgdCd"}
        if set(query) != expected or len(pairs) != len(expected):
            raise ValueError("Only anonymous seat diagnostic parameters are allowed")
        if (query["coCd"] != config.company_code or query["siteNo"] != config.site_no
                or query["scnYmd"] != config.cgv_header_probe_date.strftime("%Y%m%d")):
            raise ValueError("Seat diagnostic does not match the explicit site/date")
        for name in ("scnsNo", "scnSseq", "seatAreaNo", "cusgdCd"):
            if not query[name].isascii() or not query[name].isdigit() or len(query[name]) > 6:
                raise ValueError("Invalid seat diagnostic identifier")
        headers.update(SEAT_PUBLIC_HEADERS)
        return SEAT_PATH, query, headers
    if config.api_url != "https://cgv.co.kr/api/v1/booking/searchSchByMov":
        raise ValueError("Unexpected diagnostic endpoint")
    return "/api/v1/booking/searchSchByMov", {
        "coCd": config.company_code, "siteNo": config.site_no,
        "scnYmd": config.cgv_header_probe_date.strftime("%Y%m%d"),
        "movNo": config.movie_no, "rtctlScopCd": config.rtctl_scope_code,
    }, headers


def _decode_body(body: bytes, encoding: str) -> bytes:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Response size limit exceeded")
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as handle:
            decoded = handle.read(MAX_RESPONSE_BYTES + 1)
    elif encoding == "deflate":
        # HTTP servers may use zlib-wrapped or raw DEFLATE streams.
        inflater = zlib.decompressobj()
        try:
            decoded = inflater.decompress(body, MAX_RESPONSE_BYTES + 1)
        except zlib.error:
            inflater = zlib.decompressobj(-zlib.MAX_WBITS)
            decoded = inflater.decompress(body, MAX_RESPONSE_BYTES + 1)
        if not inflater.eof:
            raise ValueError("Incomplete or oversized compressed response")
    else:
        raise ValueError("Unsupported response encoding")
    if len(decoded) > MAX_RESPONSE_BYTES:
        raise ValueError("Decoded response size limit exceeded")
    return decoded


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_header_probe_once(
    config: Config, logger: logging.Logger, *, user_agent: str
) -> dict[str, Any]:
    """Claim before HTTP; leave the claim in place on every possible outcome.

    The caller must keep its persisted CGV recovery latch halted. This module
    never reads login credentials, changes notification state or sends Telegram.
    """
    request_id = config.cgv_header_probe_request_id
    show_date = config.cgv_header_probe_date
    if not request_id or show_date is None:
        raise ValueError("Explicit diagnostic ID and date required")
    endpoint, query, headers = _build_request(config, user_agent)
    probe_kind = "seat" if endpoint == SEAT_PATH else "schedule"

    directory = config.state_file.parent / "cgv-header-probes"
    directory.mkdir(mode=0o700, exist_ok=True)
    _fsync_directory(directory.parent)
    key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    claim = directory / f"{key}.claim.json"
    report: dict[str, Any] = {
        "request_id": request_id,
        "time_kst": config.local_now().isoformat(timespec="seconds"),
        "runtime": "railway" if os.environ.get("RAILWAY_DEPLOYMENT_ID") else "local",
        "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID", ""),
        "endpoint": endpoint,
        "probe_kind": probe_kind,
        "http_protocol": "HTTP/1.1",
        "query": query,
        "show_date": show_date.isoformat(),
        "added_headers": FETCH_METADATA_HEADERS,
        "public_header_profile": "chrome153-seat" if probe_kind == "seat" else "schedule-fetch-metadata",
        "seat_public_headers": SEAT_PUBLIC_HEADERS if probe_kind == "seat" else {},
        "cookies_sent": False,
        "authorization_sent": False,
        "customer_id_sent": False,
        "max_requests": 1,
        "retries": 0,
        "follow_redirects": False,
        "automatic_scanning": "halted",
    }
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        logger.info("CGV_HEADER_PROBE_SKIPPED id=%s (이미 실행 기록 있음)", request_id)
        return {"request_id": request_id, "skipped": True}
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    # Persist the directory entry before network access as well as the file.
    # Even an empty/partial claim from a crash forbids another request.
    _fsync_directory(directory)
    logger.info("CGV_HEADER_PROBE_BEGIN %s", json.dumps(report, ensure_ascii=False))

    started = time.monotonic()
    connection = None
    try:
        connection = http.client.HTTPSConnection(
            "cgv.co.kr", timeout=min(config.request_timeout_seconds, 20),
            context=ssl.create_default_context(),
        )
        connection.request("GET", endpoint + "?" + urllib.parse.urlencode(query), headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        content_type = response.getheader("Content-Type", "")
        report.update(
            http_status=response.status, content_type=content_type,
            cf_ray=response.getheader("CF-Ray", ""),
            response_bytes_read=len(body), api_response_valid=False,
            content_encoding=response.getheader("Content-Encoding", "").strip().lower(),
        )
        if probe_kind == "schedule":
            report["schedule_response_valid"] = False
        decoded_body = _decode_body(body, report["content_encoding"])
        report["decoded_response_bytes"] = len(decoded_body)
        decoded = decoded_body.decode("utf-8-sig", errors="replace")
        report["is_html"] = decoded.lstrip().startswith("<")
        report["block_page_marker"] = (
            "비정상적으로 CGV에 접속" in decoded or "cloudflare" in decoded.lower()
        )
        if "json" in content_type.lower() and len(body) <= MAX_RESPONSE_BYTES:
            try:
                payload = json.loads(decoded)
                if isinstance(payload, dict):
                    status_code, data = payload.get("statusCode"), payload.get("data")
                    report["api_status_success"] = str(status_code) == "0"
                    report["data_type"] = type(data).__name__
                    if isinstance(data, list):
                        report["data_items"] = len(data)
                    report["api_response_valid"] = (
                        response.status == 200 and str(status_code) == "0"
                        and isinstance(data, (dict, list))
                    )
                    if probe_kind == "schedule":
                        report["schedule_response_valid"] = report["api_response_valid"]
            except json.JSONDecodeError:
                report["json_parse_failed"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        if connection is not None:
            connection.close()
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    logger.info("CGV_HEADER_PROBE_RESULT %s", json.dumps(report, ensure_ascii=False))
    # The immutable claim is the retry guard; a missing/incomplete result does
    # not authorize any retry. Keep summaries, never response bodies or cookies.
    with (directory / f"{key}.result.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(directory)
    return report
