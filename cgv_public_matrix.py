"""Explicitly approved, bounded anonymous schedule-header comparison.

No cookies, credentials, seat endpoint, redirects, retries or notification writes.
All cases retain Referer. This is not a change to the normal CGV client.
"""
from __future__ import annotations

import argparse
import datetime as dt
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import platform
import re
import ssl
import time
from typing import Callable
from urllib.parse import urlencode

from cgv_header_probe import _decode_body, _fsync_directory
from cgv_wire_trace import SCHEDULE_PATH, SentHeaderCapture


INTERVAL_SECONDS = 30
MAX_CASES = 14
BASE_HEADERS = {
    "Accept": "application/json", "Accept-Language": "ko-KR",
    "Cache-Control": "no-cache", "Pragma": "no-cache",
    "Referer": "https://cgv.co.kr/cnm/movieBook/movie",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}
VARIANTS = (
    ("encoding", "Accept-Encoding", "gzip, deflate, br, zstd"),
    ("chrome153", "User-Agent", BASE_HEADERS["User-Agent"].replace("Chrome/150.", "Chrome/153.")),
    ("priority", "Priority", "u=1, i"),
    ("ch_ua", "Sec-CH-UA", '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"'),
    ("ch_mobile", "Sec-CH-UA-Mobile", "?0"),
    ("ch_platform", "Sec-CH-UA-Platform", '"macOS"'),
    ("fetch_dest", "Sec-Fetch-Dest", "empty"),
    ("fetch_mode", "Sec-Fetch-Mode", "cors"),
    ("fetch_site", "Sec-Fetch-Site", "same-origin"),
)


def retry_after_seconds(value: str) -> int:
    try:
        return max(0, int(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            return max(0, int((when - dt.datetime.now(dt.timezone.utc)).total_seconds()) + 1)
        except (ValueError, TypeError, OverflowError):
            return 0


def build_cases(base_date: dt.date, compare_date: dt.date) -> list[dict]:
    combined = {**BASE_HEADERS, **{key: value for _, key, value in VARIANTS}}
    return [
        {"case": "baseline_original_date", "date": base_date, "headers": dict(BASE_HEADERS)},
        {"case": "baseline_browser_date", "date": compare_date, "headers": dict(BASE_HEADERS)},
        *({"case": name, "date": compare_date, "headers": {**BASE_HEADERS, key: value}}
          for name, key, value in VARIANTS),
        {"case": "all_public_browser_date", "date": compare_date, "headers": dict(combined)},
        {"case": "all_public_original_date", "date": base_date, "headers": dict(combined)},
        {"case": "baseline_browser_date_repeat", "date": compare_date, "headers": dict(BASE_HEADERS)},
    ]


def probe(case: dict, request_id: str, logger: logging.Logger) -> dict:
    query = {"coCd": "A420", "siteNo": "0013", "scnYmd": case["date"].strftime("%Y%m%d"),
             "movNo": "30001323", "rtctlScopCd": "08"}
    report = {"case": case["case"], "date": case["date"].isoformat(),
              "time_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "credentials_used": False, "customer_id_used": False,
              "api_response_valid": False}
    connection = http.client.HTTPSConnection("cgv.co.kr", timeout=15, context=ssl.create_default_context())
    capture = SentHeaderCapture(connection, request_id + "." + case["case"], logger)
    started = time.monotonic()
    try:
        connection.request("GET", SCHEDULE_PATH + "?" + urlencode(query), headers=case["headers"])
        response = connection.getresponse()
        report.update(http_status=response.status,
                      content_type=response.getheader("Content-Type", "")[:160],
                      content_encoding=response.getheader("Content-Encoding", "").strip().lower()[:30],
                      server=response.getheader("Server", "")[:80],
                      cf_ray=response.getheader("CF-Ray", "")[:80],
                      challenge=response.getheader("CF-Mitigated", "").lower() == "challenge",
                      retry_after_present=bool(response.getheader("Retry-After", "")),
                      retry_after_seconds=retry_after_seconds(response.getheader("Retry-After", "")))
        body = response.read(5_000_001)
        report["response_bytes"] = len(body)
        # Send the exact requested Accept-Encoding, but never mislabel an
        # unsupported compressed response as API success or as a block page.
        if report["content_encoding"] not in {"", "identity", "gzip", "deflate"}:
            report["decode_supported"] = False
            return report
        decoded = _decode_body(body, report["content_encoding"])
        report["decode_supported"] = True
        report["body_sha256"] = hashlib.sha256(decoded).hexdigest()
        text = decoded.decode("utf-8-sig", errors="replace")
        report["cgv_block_marker"] = "비정상적으로 CGV에 접속" in text
        report["is_html"] = text.lstrip().startswith("<")
        if "json" in report["content_type"].lower():
            payload = json.loads(text)
            report["api_response_valid"] = (
                response.status == 200 and isinstance(payload, dict)
                and str(payload.get("statusCode")) == "0"
                and isinstance(payload.get("data"), (dict, list))
            )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        capture.close()
        connection.close()
        report["seconds"] = round(time.monotonic() - started, 3)
    return report


def run_matrix_once(
    request_id: str, state_dir: Path, base_date: dt.date, compare_date: dt.date,
    logger: logging.Logger, *, should_stop: Callable[[], bool] = lambda: False,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", request_id):
        raise ValueError("Invalid matrix ID")
    if base_date == compare_date:
        raise ValueError("Two distinct dates required")
    cases = build_cases(base_date, compare_date)
    assert len(cases) == MAX_CASES
    directory = state_dir / "cgv-public-matrices"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(request_id.encode()).hexdigest()
    try:
        fd = os.open(directory / f"{key}.claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        logger.info("CGV_PUBLIC_MATRIX_SKIPPED id=%s (이미 실행 기록 있음)", request_id)
        return None
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(directory)
    _fsync_directory(directory.parent)
    report = {"request_id": request_id, "max_requests": MAX_CASES, "interval_seconds": INTERVAL_SECONDS,
              "runtime": "railway" if os.environ.get("RAILWAY_DEPLOYMENT_ID") else "local",
              "python": platform.python_version(), "openssl": ssl.OPENSSL_VERSION,
              "cases": [], "stop_reason": "completed"}
    logger.info("CGV_PUBLIC_MATRIX_BEGIN %s", json.dumps(report, ensure_ascii=False))
    for index, case in enumerate(cases):
        if index:
            deadline = monotonic() + INTERVAL_SECONDS
            while not should_stop() and monotonic() < deadline:
                sleeper(min(0.5, max(0, deadline - monotonic())))
        if should_stop():
            report["stop_reason"] = "shutdown"
            break
        result = probe(case, request_id, logger)
        report["cases"].append(result)
        logger.info("CGV_PUBLIC_MATRIX_RESULT %s", json.dumps({"request_id": request_id, **result}, ensure_ascii=False))
        # 403 is the bounded comparison's subject. Rate/challenge signals or
        # transport/decode failures stop this approved set, never retry it.
        if result.get("http_status") == 429 or result.get("retry_after_present"):
            report["stop_reason"] = "rate_limit_or_retry_after"
            break
        if result.get("challenge") or result.get("error_type"):
            report["stop_reason"] = "challenge_or_error"
            break
    report["requests_sent"] = len(report["cases"])
    logger.info("CGV_PUBLIC_MATRIX_DONE %s", json.dumps(report, ensure_ascii=False))
    fd = os.open(directory / f"{key}.result.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(directory)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--base-date", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--compare-date", type=dt.date.fromisoformat, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_matrix_once(args.request_id, args.state_dir, args.base_date, args.compare_date,
                    logging.getLogger("cgv_public_matrix"))


if __name__ == "__main__":
    main()
