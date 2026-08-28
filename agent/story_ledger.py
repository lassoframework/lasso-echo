"""
story_ledger.py — the RE-INGEST GUARD (ECHO_STORY_STUDIO_BUILD §0).

Every Story Studio render's content_hash is written to render_ledger. When a
render is saved back into a client's Drive and later walked by the media sync, the
classifier recognizes its content_hash HERE and marks it a re-ingest: it is skipped
(never re-indexed as raw, never re-composed, never reposted). This is the EP124
failure mode wearing a new shirt — Echo eating its own output and reposting it — and
this guard is the single place it is caught.

Two backing stores, chosen automatically:
  * the Supabase render_ledger table (durable, cross-service — the sync job and the
    render lane run on different boxes), when Supabase creds are configured;
  * the volume-backed kv store as a local/offline fallback (a dev checkout, a test,
    a single-box deploy), keyed by content_hash.

Writes are idempotent (the same content_hash records once). Reads are a pure
membership test: is_echo_render(content_hash) -> bool. Nothing here renders,
downloads, or posts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

_KV_PREFIX = "render_ledger:"          # kv fallback key per content_hash
_TABLE = "render_ledger"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _norm(content_hash) -> str:
    return str(content_hash or "").strip().lower()


# ---- Supabase store (durable, cross-service) --------------------------------
def _supabase_conf():
    from . import config
    url = config.supabase_url()
    key = config.supabase_service_key()
    return (url, key) if (url and key) else (None, None)


def _sb_record(content_hash, meta, http=None):
    url, key = _supabase_conf()
    if not url or not key:
        return False
    if http is None:
        import requests  # lazy
        http = requests
    row = {
        "content_hash": _norm(content_hash),
        "gym_id": str((meta or {}).get("gym_id") or ""),
        "story_render_id": str((meta or {}).get("story_render_id") or ""),
        "recorded_at": _now_iso(),
    }
    try:
        r = http.post(
            f"{url.rstrip('/')}/rest/v1/{_TABLE}",
            params={"on_conflict": "content_hash"},
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates"},
            json=row, timeout=30)
        return r.status_code < 400
    except Exception as e:  # noqa: BLE001 - a ledger write failure must never crash a render
        print(f"[story-ledger] supabase record failed: {type(e).__name__}: {e}")
        return False


def _sb_has(content_hash, http=None):
    url, key = _supabase_conf()
    if not url or not key:
        return None  # store not configured; caller falls back to kv
    if http is None:
        import requests  # lazy
        http = requests
    try:
        r = http.get(
            f"{url.rstrip('/')}/rest/v1/{_TABLE}",
            params={"content_hash": f"eq.{_norm(content_hash)}",
                    "select": "content_hash", "limit": "1"},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=30)
        if r.status_code >= 400:
            return None
        return bool(r.json())
    except Exception as e:  # noqa: BLE001 - a read failure falls back to kv, never crashes
        print(f"[story-ledger] supabase lookup failed: {type(e).__name__}: {e}")
        return None


# ---- kv fallback (local / offline) ------------------------------------------
def _kv_record(content_hash, meta):
    from . import db
    db.kv_set(_KV_PREFIX + _norm(content_hash), json.dumps({
        "gym_id": str((meta or {}).get("gym_id") or ""),
        "story_render_id": str((meta or {}).get("story_render_id") or ""),
        "recorded_at": _now_iso(),
    }))


def _kv_has(content_hash):
    from . import db
    return bool(db.kv_get(_KV_PREFIX + _norm(content_hash), ""))


# ---- public API -------------------------------------------------------------
def record_render(content_hash, *, gym_id="", story_render_id="", http=None):
    """Stamp a completed Story render's content_hash into the ledger (idempotent).
    Writes to Supabase when configured AND always mirrors to the kv fallback so a
    same-box sync can catch a re-ingest even without Supabase. Returns the
    content_hash (normalized) for the caller to persist on the story_render row."""
    ch = _norm(content_hash)
    if not ch:
        return ch
    meta = {"gym_id": gym_id, "story_render_id": story_render_id}
    _sb_record(ch, meta, http=http)
    try:
        _kv_record(ch, meta)
    except Exception as e:  # noqa: BLE001
        print(f"[story-ledger] kv record failed: {type(e).__name__}: {e}")
    from . import db
    db.audit("story_ledger", ch[:16], f"render recorded (gym={gym_id})")
    return ch


def is_echo_render(content_hash, *, http=None) -> bool:
    """True when this content_hash is one of Echo's OWN past Story renders (so a file
    with these bytes must NEVER be re-ingested as raw and re-composed/reposted). Checks
    Supabase first (durable, cross-service); on a not-configured / unreachable store it
    falls back to the kv mirror. A miss on both returns False (a genuine client file)."""
    ch = _norm(content_hash)
    if not ch:
        return False
    hit = _sb_has(ch, http=http)
    if hit is True:
        return True
    if hit is None:  # store not configured / unreachable -> kv fallback
        return _kv_has(ch)
    # hit is False: Supabase answered and did not have it. Still honor a local kv
    # stamp (a render recorded on THIS box before Supabase was armed).
    return _kv_has(ch)
