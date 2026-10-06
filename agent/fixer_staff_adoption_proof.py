"""Read-only staff-adoption proof boundary for Aimee's Swift River complaint.

The ticket says the calendar was not rebuilt from Drive, generic LASSO images
remained, and a requested swap appeared unchanged.  Calendar-wide evidence
cannot prove that reported swap changed; this route stays fail closed until an
authoritative target row and post-swap readback are bound to this request.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .fixer_evidence import read_rest

PATH = "/ops/actions/staff-adoption/business-proof"
TICKET_ID = "ae8c7e39-7509-4948-b06a-a24954c3b0a3"
REQUESTER = "U06F8BUH7CG"
CHANNEL = "C0C2MHAAUMU"
THREAD = "1790946130.867599"
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_PAGE = 200
_MAX_MESSAGES = 2000
_MAX_BODY = 64 * 1024
_SWIFT = "swiftrivercrossfite5c9db"
_ARCHIVE = Path("/data/swift-river-historical-igfill-archive-20261003-0642.json")
_HIST_START, _HIST_END = "2026-09-02", "2026-10-02"
_FUTURE_START, _FUTURE_END = "2026-10-03", "2026-10-11"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_GYM_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}\Z")
_ROW_ID = re.compile(r"[A-Za-z0-9_-]{6,80}\Z")
_MAX_MEDIA_BYTES = 10 * 1024 * 1024
_FIELDS = {"ticket_id", "request_version", "client_id", "slack_channel_id",
           "slack_thread_ts", "request_messages"}
_MESSAGE_FIELDS = {"id", "author_id", "created_at", "body"}


def _unavailable(code):
    return 503, {"error": code}


def _valid_message(row):
    stamp = row.get("created_at") if isinstance(row, dict) else None
    try:
        if not isinstance(stamp, str) or not stamp or datetime.fromisoformat(
                stamp.replace("Z", "+00:00")).utcoffset() is None:
            return False
    except ValueError:
        return False
    return (isinstance(row, dict) and set(row) == _MESSAGE_FIELDS
            and isinstance(row.get("id"), str) and _UUID.fullmatch(row["id"])
            and row.get("author_id") == REQUESTER
            and isinstance(row.get("body"), str))


def _read_messages(read):
    """Keyset pages to exhaustion; a full last page is never treated as complete."""
    messages = []
    last_id = None
    while True:
        params = {"ticket_id": f"eq.{TICKET_ID}", "direction": "eq.inbound",
                  "select": "id,ticket_id,direction,author_id,created_at,body",
                  "order": "id.asc", "limit": str(_PAGE)}
        if last_id is not None:
            params["id"] = f"gt.{last_id}"
        rows = read("support_messages", params)
        if not isinstance(rows, list) or len(rows) > _PAGE:
            return None
        if not rows:
            return messages if messages else None
        for row in rows:
            if (not isinstance(row, dict) or row.get("ticket_id") != TICKET_ID
                    or row.get("direction") != "inbound"):
                return None
            message = {key: row.get(key) for key in _MESSAGE_FIELDS}
            if (not _valid_message(message)
                    or (last_id is not None and message["id"] <= last_id)):
                return None
            messages.append(message)
            last_id = message["id"]
            if len(messages) > _MAX_MESSAGES:
                return None
        if len(rows) < _PAGE:
            return messages


def _igfill(row):
    return any("igfill_" in urlparse(str(row.get(key) or "")).path.lower()
               for key in ("image_url", "source_media_url"))


def _archive_receipt():
    """Read the private write-ahead receipt without following a replacement path."""
    try:
        info = _ARCHIVE.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600):
            return None
        value = json.loads(_ARCHIVE.read_bytes())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("operation") != "swift_river_historical_igfill_archive":
        return None
    if (value.get("client") != _SWIFT or value.get("target_rows") != 25
            or value.get("target_dates") != 17 or value.get("state") != "readback_verified"
            or not isinstance(value.get("before_image"), list)
            or not isinstance(value.get("readback"), list)
            or not isinstance(value.get("changed_ids"), list)):
        return None
    before, after = value["before_image"], value["readback"]
    ids = [row.get("id") for row in before if isinstance(row, dict)]
    if len(before) != 25 or len(after) != 25 or len(ids) != 25 or set(ids) != set(value["changed_ids"]):
        return None
    if {str(row.get("post_date") or "")[:10] for row in before}.__len__() != 17:
        return None
    return value


def _rows(read, start, end):
    rows = read("content_calendar", {"gym_id": f"eq.{_SWIFT}",
        "post_date": [f"gte.{start}", f"lte.{end}"],
        "select": "id,gym_id,post_date,status,variant_status,image_url,source_media_url,source_media_asset_id,caption,media_not_ready_reason,published_at,late_post_id,scheduled_at,slot_index,account,format,variant_of,created_at",
        "order": "post_date.asc,id.asc", "limit": "500"})
    if not isinstance(rows, list) or len(rows) >= 500 or any(not isinstance(row, dict) or row.get("gym_id") != _SWIFT for row in rows):
        return None
    return rows


def _media_digest(url, http_get):
    from . import config
    base = str(config.S3_PUBLIC_BASE_URL or "").rstrip("/")
    parsed, expected = urlparse(url), urlparse(base)
    if (parsed.scheme != "https" or not parsed.netloc or not expected.netloc
            or parsed.netloc != expected.netloc or not url.startswith(base + "/")):
        return None
    try:
        response = http_get(url, timeout=10, allow_redirects=False, stream=True)
        if getattr(response, "status_code", None) != 200:
            return None
        content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
        if not content_type.startswith("image/"):
            return None
        data = b""
        for chunk in response.iter_content(65536):
            data += chunk
            if len(data) > _MAX_MEDIA_BYTES:
                return None
        return hashlib.sha256(data).hexdigest() if data else None
    except Exception:
        return None


def _proof(read, http_get=None):
    receipt = _archive_receipt()
    if receipt is None:
        return None, "archive_receipt_unavailable"
    historical = _rows(read, _HIST_START, _HIST_END)
    if historical is None:
        return None, "archive_readback_unavailable"
    receipt_rows = {row["id"]: row for row in receipt["readback"] if isinstance(row, dict) and isinstance(row.get("id"), str)}
    live = [row for row in historical if row.get("id") in receipt_rows]
    if (len(live) != 25 or set(row.get("id") for row in live) != set(receipt_rows)
            or any(row.get("variant_status") != "archived" or not _igfill(row) for row in live)):
        return None, "historical_archive_mismatch"
    # Receipt fields are the archived operation's full before/readback contract.
    for row in live:
        expected = receipt_rows[row["id"]]
        if any(row.get(key) != expected.get(key) for key in expected):
            return None, "historical_archive_mismatch"
    future = _rows(read, _FUTURE_START, _FUTURE_END)
    if future is None:
        return None, "future_calendar_unavailable"
    active = [row for row in future if row.get("variant_status", "active") == "active"]
    by_day = {}
    asset_ids = set()
    for row in active:
        day = str(row.get("post_date") or "")[:10]
        if _igfill(row) or not isinstance(row.get("image_url"), str) or not row["image_url"]:
            return None, "future_photo_first_mismatch"
        asset = row.get("source_media_asset_id")
        if asset is not None and (not isinstance(asset, str) or not asset):
            return None, "future_photo_provenance_unavailable"
        if asset:
            asset_ids.add(asset)
        if row.get("account") == "instagram" and row.get("format") == "feed":
            by_day.setdefault(day, []).append(row)
    expected_days = {f"2026-10-{day:02d}" for day in range(3, 12)}
    if set(by_day) != expected_days:
        return None, "future_calendar_incomplete"
    if http_get is None:
        import requests
        http_get = requests.get
    seen_urls, seen_digests = set(), set()
    for day in sorted(by_day):
        urls = {row["image_url"] for row in by_day[day]}
        if len(by_day[day]) != 1 or len(urls) != 1 or urls & seen_urls:
            return None, "future_cross_day_media_mismatch"
        digest = _media_digest(next(iter(urls)), http_get)
        if digest is None:
            return None, "future_media_bytes_unavailable"
        if digest in seen_digests:
            return None, "future_cross_day_media_mismatch"
        seen_urls |= urls; seen_digests.add(digest)
    from . import gym_media_selector
    for asset_id in asset_ids:
        assets = read("media_asset", {"id": f"eq.{asset_id}", "gym_id": f"eq.{_SWIFT}",
            "select": "id,gym_id,kind,eligible,excluded_by_coach,review_status,reviewed_by,reviewed_at,content_hash,review_content_hash,moderation_status,moderation_json,people_detected", "limit": "2"})
        if not isinstance(assets, list) or len(assets) != 1 or assets[0].get("id") != asset_id:
            return None, "future_photo_provenance_unavailable"
        if assets[0].get("kind") not in ("photo", "video") or not gym_media_selector.is_usable(assets[0]):
            return None, "future_photo_provenance_mismatch"
    return f"swift_river_photo_first_archive:25:17:{','.join(sorted(d[:12] for d in seen_digests))}", None


def handle(raw_body, *, read=None, http_get=None, receipt_read=None):
    """Return (HTTP status, JSON body). Authentication is owned by fixer_ops.handle."""
    if not isinstance(raw_body, bytes) or len(raw_body) > _MAX_BODY:
        return 413, {"error": "too_large"}
    try:
        body = json.loads(raw_body)
    except (UnicodeError, ValueError, TypeError):
        return 400, {"error": "bad_request"}
    if not isinstance(body, dict) or set(body) != _FIELDS:
        return 400, {"error": "bad_request"}
    version = body.get("request_version")
    supplied = body.get("request_messages")
    if (body.get("ticket_id") != TICKET_ID or type(version) is not int or version < 0
            or body.get("client_id") is not None
            or body.get("slack_channel_id") != CHANNEL
            or body.get("slack_thread_ts") != THREAD
            or not isinstance(supplied, list) or not 0 < len(supplied) <= _MAX_MESSAGES
            or any(not _valid_message(row) for row in supplied)
            or len({row["id"] for row in supplied}) != len(supplied)):
        return 400, {"error": "bad_request"}

    source = read or read_rest
    try:
        tickets = source("support_tickets", {
            "id": f"eq.{TICKET_ID}",
            "select": "id,product,source,status,is_test,client_id,identity_kind,"
                      "request_version,slack_user_id,slack_channel_id,slack_thread_ts,verification_before,created_at,raw_text",
            "limit": "2"})
        if not isinstance(tickets, list) or len(tickets) != 1 or not isinstance(tickets[0], dict):
            return _unavailable("ticket_identity_unavailable")
        ticket = tickets[0]
        if (ticket.get("id") != TICKET_ID or ticket.get("product") != "echo"
                or ticket.get("source") != "slack_conversation"
                or ticket.get("status") != "hold" or ticket.get("is_test") is not False
                or ticket.get("client_id") is not None
                or ticket.get("identity_kind") != "coach"
                or ticket.get("slack_user_id") != REQUESTER
                or ticket.get("slack_channel_id") != CHANNEL
                or ticket.get("slack_thread_ts") != THREAD
                or type(ticket.get("request_version")) is not int
                or ticket["request_version"] != version):
            return 409, {"error": "ticket_identity_mismatch"}
        messages = _read_messages(source)
    except Exception:  # Source failures never become proof.
        return _unavailable("authoritative_source_unavailable")
    if messages is None:
        return _unavailable("inbound_thread_unavailable")
    canonical = lambda rows: sorted(rows, key=lambda row: row["id"])
    if canonical(supplied) != canonical(messages):
        return 409, {"error": "request_messages_mismatch"}

    pointer = ((ticket.get("verification_before") or {}).get("fixer") or {}).get("staff_swap")
    required = {"gym_key", "row_id", "reservation_key", "before_image_sha256", "caption_sha256"}
    if not isinstance(pointer, dict) or set(pointer) != required:
        return _unavailable("business_check_unavailable")
    if (not isinstance(pointer["gym_key"], str) or not _GYM_KEY.fullmatch(pointer["gym_key"])
            or not isinstance(pointer["row_id"], str) or not _ROW_ID.fullmatch(pointer["row_id"])
            or not isinstance(pointer["reservation_key"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", pointer["reservation_key"])
            or any(not isinstance(pointer[key], str) or not re.fullmatch(r"[0-9a-f]{64}", pointer[key])
                   for key in ("before_image_sha256", "caption_sha256"))
            or not callable(receipt_read)):
        return _unavailable("business_check_unavailable")
    try:
        from .fixer_ops import _business_request_key
        current_key = _business_request_key(source, ticket)
        if not current_key:
            return _unavailable("current_request_unavailable")
        receipt = receipt_read(pointer["reservation_key"], pointer["gym_key"])
        proof = ((receipt or {}).get("result") or {}).get("swap_proof") or {}
        if (receipt.get("action") != "swap_media" or receipt.get("status") != "done"
                or receipt.get("ticket_id") != TICKET_ID or receipt.get("gym_key") != pointer["gym_key"]
                or receipt.get("request_key") != current_key
                or ((receipt.get("result") or {}).get("postcondition_verified") is not True)
                or proof.get("row_id") != pointer["row_id"]
                or proof.get("before_image_sha256") != pointer["before_image_sha256"]
                or proof.get("before_image_sha256") == proof.get("after_image_sha256")):
            return 409, {"error": "swap_receipt_mismatch"}
        finished = datetime.fromisoformat(str(receipt.get("finished_at") or "").replace("Z", "+00:00"))
        if finished.tzinfo is None or finished.utcoffset() is None:
            return _unavailable("receipt_timestamp_unavailable")
        newest = max(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")) for row in messages)
        if finished < newest:
            return 409, {"error": "swap_receipt_stale"}
        rows = source("content_calendar", {"id": f"eq.{pointer['row_id']}", "gym_id": f"eq.{pointer['gym_key']}",
            "select": "id,gym_id,status,caption,image_url,source_media_asset_id", "limit": "2"})
        if not isinstance(rows, list) or len(rows) != 1:
            return 409, {"error": "swap_readback_mismatch"}
        row = rows[0]; digest = lambda value: hashlib.sha256(str(value).encode()).hexdigest()
        if (row.get("status") != "pending" or digest(row.get("caption")) != pointer["caption_sha256"]
                or digest(row.get("image_url")) != proof.get("after_image_sha256")
                or row.get("source_media_asset_id") != proof.get("after_asset_id")
                or not isinstance(row.get("source_media_asset_id"), str)):
            return 409, {"error": "swap_readback_mismatch"}
        from . import gym_media_selector
        assets = source("media_asset", {"id": f"eq.{row['source_media_asset_id']}", "gym_id": f"eq.{pointer['gym_key']}",
            "select": "id,gym_id,kind,eligible,excluded_by_coach,review_status,reviewed_by,reviewed_at,content_hash,review_content_hash,moderation_status,moderation_json,people_detected", "limit": "2"})
        if not isinstance(assets, list) or len(assets) != 1 or not gym_media_selector.is_usable(assets[0]):
            return 409, {"error": "swap_asset_mismatch"}
    except Exception:
        return _unavailable("authoritative_source_unavailable")
    sha = str(os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "").lower()
    if not _SHA.fullmatch(sha):
        return _unavailable("production_identity_unavailable")
    return 200, {"source": "independent_business_check", "verified": True, "symptom_resolved": True,
        "ticket_id": TICKET_ID, "request_version": version, "client_id": None,
        "check_id": "staff_ticket_keyed_media_swap", "production_sha": sha,
        "captured_at": datetime.now().astimezone().isoformat(), "request_message_ids": sorted(row["id"] for row in messages),
        "evidence": f"swap:{pointer['row_id']}:{row['source_media_asset_id']}"}
    return _unavailable("business_check_unavailable")
