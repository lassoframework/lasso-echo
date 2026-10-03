"""
historical_media_clearance.py — explicit per-asset historical review/clearance
for older approved Drive media (the ~610-asset backlog).

Every historical asset stays UNAVAILABLE until a named reviewer individually
proves it unused and records an explicit clearance receipt in
public.media_historical_clearance (DRAFT migration, owned by the SQL child).
Known-used assets are permanently ineligible; ambiguous assets are held.
NOTHING is auto-cleared.

IDENTITY IS EXACT AND ONLY THIS: (gym_id, asset_id Drive file ID, CURRENT
content_hash). NEVER infer identity from filename, URL, or pHash. A clearance
recorded against an older content_hash NEVER clears the asset after its bytes
change — stale-hash clearance never clears; the asset must be re-reviewed.

Tri-state flag AGENT_HISTORICAL_MEDIA_CLEARANCE mirrors
gym_media_selector.global_ledger_flag: on / off / ambiguous-fails-closed.
Repo rule: every new capability ships behind a flag default OFF.

ACTIVATION WATERMARK (2026-10-03 repair): the clearance gate applies ONLY to
assets that predate a fixed, explicitly configured PER-GYM cutoff. Configure
one ISO-8601 timestamp per gym in
AGENT_HISTORICAL_MEDIA_CLEARANCE_CUTOFF_<GYM_ID> (gym id uppercased, every
non-alphanumeric character replaced with '_'). An asset whose first-seen
timestamp is at/after the cutoff is EXEMPT from this gate (it is still subject
to normal review and the global ledger). Timestamp precedence is
first_indexed_at (stamped once at insert, never re-stamped) over indexed_at
(which is bumped on every re-sync PATCH, so it can NEVER prove an asset is
new). indexed_at is consulted ONLY when the first_indexed_at column is absent
entirely (rows predating it); a present-but-NULL or present-but-unparseable
first_indexed_at is UNKNOWN (never a fallback to indexed_at) and fails closed. A missing cutoff, an unparseable cutoff, or an
unknown/unparseable asset timestamp all FAIL CLOSED: the asset is treated as
historical and requires a 'cleared' receipt.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

HISTORICAL_CLEARANCE_FLAG_ENV = "AGENT_HISTORICAL_MEDIA_CLEARANCE"
CUTOFF_ENV_PREFIX = "AGENT_HISTORICAL_MEDIA_CLEARANCE_CUTOFF_"


def _cutoff_env_name(gym_id):
    safe = re.sub(r"[^A-Za-z0-9]", "_", str(gym_id or "")).upper()
    return CUTOFF_ENV_PREFIX + safe if safe else ""


def _parse_ts(value):
    """Parse an ISO-8601 timestamp to an aware UTC datetime. Returns None on
    anything unknown or unparseable — callers fail closed on None."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clearance_cutoff(gym_id):
    """The per-gym activation watermark as an aware datetime, or None when the
    cutoff is not configured or is unparseable. None FAILS CLOSED: callers must
    treat every asset as historical (clearance required)."""
    name = _cutoff_env_name(gym_id)
    if not name:
        return None
    return _parse_ts(os.environ.get(name))


def asset_first_seen(asset):
    """When Echo first saw this asset. first_indexed_at is stamped ONCE at
    insert and never re-stamped; indexed_at is bumped on every re-sync PATCH
    and so only answers 'last touched'. indexed_at is the fallback ONLY for
    rows where the first_indexed_at column is absent entirely (predating the
    column, backfilled at migration). A present-but-unparseable
    first_indexed_at is corrupt data, not a legacy row, and never falls
    through to indexed_at. None = unknown = fail closed."""
    a = asset or {}
    if "first_indexed_at" in a:
        # Column present on the row. A NULL first_indexed_at is UNKNOWN, never
        # 'legacy': indexed_at is bumped on every re-sync PATCH, so falling back
        # to it here could launder an old asset into 'new'. Fail closed.
        value = a.get("first_indexed_at")
        if value is None:
            return None
        return _parse_ts(value)
    # Column absent entirely (rows predating it, backfilled at migration):
    # indexed_at is the only timestamp available.
    return _parse_ts(a.get("indexed_at"))


def asset_is_historical(asset, cutoff):
    """True when the historical-clearance gate applies to this asset. An asset
    first seen at/after the per-gym cutoff is exempt (normal review + global
    ledger still apply). An unknown timestamp or a missing/unparseable cutoff
    FAILS CLOSED as historical."""
    if cutoff is None:
        return True
    first_seen = asset_first_seen(asset)
    if first_seen is None:
        return True
    return first_seen < cutoff


class HistoricalClearanceUnavailable(RuntimeError):
    """The historical-clearance ledger could not prove an asset's status."""


def historical_clearance_flag():
    """Tri-state read of AGENT_HISTORICAL_MEDIA_CLEARANCE: True (on), False
    (off or unset — the default), None (ambiguous value — fail closed).
    Matches gym_media_selector.global_ledger_flag semantics exactly."""
    raw = (os.environ.get(HISTORICAL_CLEARANCE_FLAG_ENV, "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("", "0", "false", "no", "off"):
        return False
    return None


def _current_hash(asset):
    value = str((asset or {}).get("content_hash") or "").strip().lower()
    return value if re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{64})", value) else ""


def _bound_evidence(evidence, gym_id, asset_id, content_hash):
    """A receipt's evidence must be a dict binding the SAME identity triple."""
    return (isinstance(evidence, dict)
            and evidence.get("gym_id") == gym_id
            and evidence.get("asset_id") == asset_id
            and evidence.get("content_hash") == content_hash)


def clearance_status(asset, clearance_rows):
    """The asset's historical-clearance status against the gym's receipt rows.

    Matches ONLY on the exact identity triple (gym_id, asset_id, CURRENT
    content_hash) — never filename, URL, or pHash. Returns:
      * 'known_used' — ANY known_used receipt for (gym_id, asset_id), whatever
        its hash. Permanent: cannot be revoked, never expires.
      * 'cleared' — a receipt with decision='cleared', NOT revoked, whose
        content_hash equals the asset's CURRENT content_hash, with a nonempty
        reviewer and a dict evidence binding the same gym_id/asset_id/hash.
      * 'held' — otherwise, when an active (unrevoked) 'held' receipt matches
        the current identity.
      * 'uncleared' — no qualifying receipt at all.
    """
    a = asset or {}
    gym_id = str(a.get("gym_id") or "")
    asset_id = str(a.get("id") or "")
    source_id = str(a.get("source_id") or "")
    current = _current_hash(a)
    if not gym_id or not asset_id:
        return "uncleared"
    held = False
    for row in clearance_rows or ():
        if not isinstance(row, dict):
            continue
        # Identity is the quadruple (gym_id, asset_id, source_id, CURRENT
        # content_hash). A receipt recorded against another source NEVER
        # matches this asset — a missing/mismatched source_id fails closed.
        if (str(row.get("gym_id") or "") != gym_id
                or str(row.get("asset_id") or "") != asset_id
                or not source_id
                or str(row.get("source_id") or "") != source_id):
            continue
        decision = row.get("decision")
        if decision == "known_used":
            # Permanent and one-way: revocation can never apply to known_used,
            # and the hash is irrelevant — the bytes were seen in use.
            return "known_used"
        row_hash = str(row.get("content_hash") or "").strip().lower()
        if row.get("revoked_at"):
            continue
        if decision == "cleared":
            if (current and row_hash == current
                    and str(row.get("reviewer") or "").strip()
                    and _bound_evidence(row.get("evidence"),
                                        gym_id, asset_id, current)):
                return "cleared"
        elif decision == "held" and current and row_hash == current:
            held = True
    return "held" if held else "uncleared"
