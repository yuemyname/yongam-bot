"""Opt-in, at-most-once CGV header diagnostic. Never resumes monitoring."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import ssl
import time
from typing import TYPE_CHECKING, Any
import urllib.parse

if TYPE_CHECKING:
    from watcher import Config


FETCH_METADATA_HEADERS = {
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
MAX_RESPONSE_BYTES = 5_000_000


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
    # The diagnostic is scoped to this public schedule endpoint, not arbitrary
    # URLs supplied through environment variables (or redirect responses).
    if config.api_url != "https://cgv.co.kr/api/v1/booking/searchSchByMov":
        raise ValueError("Unexpected diagnostic endpoint")

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
        "endpoint": "/api/v1/booking/searchSchByMov",
        "show_date": show_date.isoformat(),
        "added_headers": FETCH_METADATA_HEADERS,
        "cookies_sent": False,
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

    headers = {
        "Accept": "application/json",
        "Accept-Language": "ko-KR",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": config.booking_url,
        "User-Agent": user_agent,
        **FETCH_METADATA_HEADERS,
    }
    query = urllib.parse.urlencode({
        "coCd": config.company_code,
        "siteNo": config.site_no,
        "scnYmd": show_date.strftime("%Y%m%d"),
        "movNo": config.movie_no,
        "rtctlScopCd": config.rtctl_scope_code,
    })
    started = time.monotonic()
    connection = None
    try:
        connection = http.client.HTTPSConnection(
            "cgv.co.kr", timeout=min(config.request_timeout_seconds, 20),
            context=ssl.create_default_context(),
        )
        connection.request("GET", report["endpoint"] + "?" + query, headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        content_type = response.getheader("Content-Type", "")
        report.update(
            http_status=response.status, content_type=content_type,
            cf_ray=response.getheader("CF-Ray", ""),
            response_bytes_read=len(body), schedule_response_valid=False,
        )
        decoded = body.decode("utf-8-sig", errors="replace")
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
                    report["schedule_response_valid"] = (
                        response.status == 200 and str(status_code) == "0"
                        and isinstance(data, (dict, list))
                    )
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
