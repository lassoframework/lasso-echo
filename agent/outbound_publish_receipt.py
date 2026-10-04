"""Optional, append-only audit receipts for outbound social publish decisions.

The receipt rail deliberately observes a decision; it does not authorize, block,
or retry a provider send.  It is disabled unless an operator supplies both the
feature flag and a destination path.  This keeps the existing draft-only and
approval gates authoritative while providing a bounded trail when the rail is
explicitly armed.
"""

import json
import os
from datetime import datetime, timezone


_FLAG = "AGENT_OUTBOUND_PUBLISH_RECEIPT"
_PATH = "AGENT_OUTBOUND_PUBLISH_RECEIPT_PATH"


class ReceiptWriteError(RuntimeError):
    """Raised only when the explicitly armed receipt rail cannot be made durable."""


def enabled():
    return os.environ.get(_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def record(*, lane, row, decision, reason="", attempted=False, outcome=""):
    """Append one redacted decision receipt, returning it or ``None`` when inert.

    URLs, captions, credentials, and provider payloads are deliberately excluded.
    ``source_media_asset_id`` is only represented as a boolean because older live
    photo and video rows legitimately do not carry that id.
    """
    armed = enabled()
    path = os.environ.get(_PATH, "").strip()
    if not armed:
        return None
    if not path:
        raise ReceiptWriteError("outbound publish receipt path is not configured")
    row = row or {}
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "lane": str(lane),
        "row_id": str(row.get("id") or ""),
        "gym_id": str(row.get("gym_id") or ""),
        "platform": str(row.get("account") or ""),
        "status": str(row.get("status") or ""),
        "approved": str(row.get("status") or "").strip().lower() == "approved",
        "has_media": bool(str(row.get("image_url") or "").strip()),
        "media_not_ready": bool(str(row.get("media_not_ready_reason") or "").strip()),
        "source_media_asset_id_present": bool(str(row.get("source_media_asset_id") or "").strip()),
        "decision": str(decision),
        "reason": str(reason)[:240],
        "attempted": bool(attempted),
        "outcome": str(outcome)[:80],
    }
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if not written:
                    raise OSError("receipt write made no progress")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ReceiptWriteError("outbound publish receipt could not be persisted") from exc
    return record
