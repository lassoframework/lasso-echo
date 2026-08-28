"""
story_sort_queue.py — the "Sort these" queue for AMBIGUOUS media (spec §0.3).

When the classifier cannot confidently call a file raw or finished, the file is
NEVER auto-staged. It is enqueued here (story_sort_queue table) with its thumbnail
and the classifier's reasons, and a coach-channel digest fires when the queue is
non-empty. A coach taps Raw / Finished / Skip; the resolution feeds back the intent
(a later sync of the SAME bytes is then a declared lane, not a re-guess).

A silent wrong guess is the only unacceptable outcome, so this queue is the safety
net under the classifier: ambiguity is surfaced to a human, never resolved by Echo.

Store: Supabase story_sort_queue when configured, else the volume kv fallback so the
queue works on a single box / in tests. enqueue is idempotent per (gym_id, asset_id).
Nothing here posts to a feed, stages, or composes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

_KV_PREFIX = "story_sort_queue:"       # kv fallback key per gym+asset
_TABLE = "story_sort_queue"

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _supabase_conf():
    from . import config
    url = config.supabase_url()
    key = config.supabase_service_key()
    return (url, key) if (url and key) else (None, None)


# ---- enqueue ----------------------------------------------------------------
def enqueue(gym_id, asset_id, *, thumbnail_url="", reasons=(), verdict="ambiguous",
            http=None):
    """Add ONE ambiguous asset to the sort queue (idempotent per gym+asset). Returns
    True when a NEW row was written (False when it was already queued). Writes to
    Supabase when configured; always mirrors to kv so the digest works offline."""
    row = {
        "gym_id": str(gym_id or ""),
        "asset_id": str(asset_id or ""),
        "thumbnail_url": thumbnail_url or "",
        "reasons": list(reasons or []),
        "verdict": verdict or "ambiguous",
        "status": STATUS_PENDING,
        "enqueued_at": _now_iso(),
    }
    wrote_new = _kv_enqueue(row)
    _sb_enqueue(row, http=http)
    return wrote_new


def _kv_enqueue(row):
    from . import db
    key = _KV_PREFIX + row["gym_id"] + ":" + row["asset_id"]
    if db.kv_get(key, ""):
        return False
    db.kv_set(key, json.dumps(row))
    return True


def _sb_enqueue(row, http=None):
    url, key = _supabase_conf()
    if not url or not key:
        return False
    if http is None:
        import requests  # lazy
        http = requests
    try:
        r = http.post(
            f"{url.rstrip('/')}/rest/v1/{_TABLE}",
            params={"on_conflict": "gym_id,asset_id"},
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "resolution=ignore-duplicates"},
            json={**row, "reasons": json.dumps(row["reasons"])}, timeout=30)
        return r.status_code < 400
    except Exception as e:  # noqa: BLE001
        print(f"[story-sort-queue] supabase enqueue failed: {type(e).__name__}: {e}")
        return False


# ---- read (for the digest + the portal) -------------------------------------
def pending(gym_id=None):
    """Every pending sort-queue row (optionally filtered to one gym), from the kv
    mirror (always present). Ordered by enqueued_at."""
    from . import db
    rows = []
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT key, value FROM kv WHERE key LIKE ?",
                (_KV_PREFIX + "%",)).fetchall()
    except Exception:
        return rows
    for r in cur:
        try:
            rec = json.loads(r["value"] or "{}")
        except Exception:
            continue
        if not rec or rec.get("status") != STATUS_PENDING:
            continue
        if gym_id is not None and rec.get("gym_id") != str(gym_id):
            continue
        rows.append(rec)
    rows.sort(key=lambda x: x.get("enqueued_at") or "")
    return rows


def resolve(gym_id, asset_id, lane, *, resolved_by="", http=None):
    """A coach tapped Raw / Finished / Skip on a queued asset. Marks the kv + Supabase
    row resolved and returns the declared lane the caller feeds back to the classifier
    on the next sync (so the SAME bytes are then a declared lane, not a re-guess).
    lane in {'raw','finished','skip'}. Idempotent."""
    from . import db
    key = _KV_PREFIX + str(gym_id) + ":" + str(asset_id)
    try:
        rec = json.loads(db.kv_get(key, "") or "{}")
    except Exception:
        rec = {}
    if rec:
        rec["status"] = STATUS_RESOLVED
        rec["resolved_lane"] = lane
        rec["resolved_by"] = resolved_by
        rec["resolved_at"] = _now_iso()
        db.kv_set(key, json.dumps(rec))
    _sb_resolve(gym_id, asset_id, lane, resolved_by, http=http)
    return lane


def _sb_resolve(gym_id, asset_id, lane, resolved_by, http=None):
    url, key = _supabase_conf()
    if not url or not key:
        return False
    if http is None:
        import requests  # lazy
        http = requests
    try:
        r = http.patch(
            f"{url.rstrip('/')}/rest/v1/{_TABLE}",
            params={"gym_id": f"eq.{gym_id}", "asset_id": f"eq.{asset_id}"},
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"status": STATUS_RESOLVED, "resolved_lane": lane,
                  "resolved_by": resolved_by, "resolved_at": _now_iso()},
            timeout=30)
        return r.status_code < 400
    except Exception as e:  # noqa: BLE001
        print(f"[story-sort-queue] supabase resolve failed: {type(e).__name__}: {e}")
        return False


# ---- coach-channel digest (fires only when the queue is non-empty) ----------
def post_digest(gym_id, *, poster=None, channel=None):
    """Post ONE coach-channel digest listing the gym's pending sort-queue items, or do
    nothing when the queue is empty (spec §0.3: digest WHEN non-empty). Returns the
    count posted (0 = nothing to sort, no message). The message is scannable and names
    the count + the ask (tap Raw / Finished / Skip in the portal)."""
    items = pending(gym_id)
    if not items:
        return 0
    lines = [f"Sort these for {gym_id}: {len(items)} file(s) Echo could not "
             f"confidently call raw or finished. Tap Raw / Finished / Skip in the "
             f"portal media tab so nothing is guessed."]
    for it in items[:10]:
        why = "; ".join(it.get("reasons") or []) or "no strong signal"
        lines.append(f"  - {it.get('asset_id')}: {why}")
    msg = "\n".join(lines)
    try:
        if poster is not None and hasattr(poster, "post_notice"):
            poster.post_notice(msg)
        else:
            from . import ops_alerts
            ops_alerts.alert(msg)
    except Exception as e:  # noqa: BLE001 - a digest failure is not a crash
        print(f"[story-sort-queue] digest post failed: {type(e).__name__}: {e}")
    return len(items)
