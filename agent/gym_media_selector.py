"""
gym_media_selector.py — pick the next Drive-sourced media asset for a gym
(gym_media_drive spec §6): the least-used, longest-unused eligible asset that the
coach has not hidden.

Mirrors podcast_selector.py exactly (stamp-at-stage + deny rollback), with the
gym-media rails:
  * TENANT ISOLATION (§1.5d): pick_media(gym_id) reads ONLY that gym's assets
    (the store filters by gym_id) AND re-asserts a.gym_id == gym_id in the loop.
    A row for the wrong gym can never be selected.
  * eligible is TRUE (not NULL/False) and excluded_by_coach is False.
  * 90-day reuse cooldown: never an asset used inside 90 days.
  * never the same asset twice in a MONTH (an asset used this calendar month is
    out, even if the 90-day window has not fully elapsed — a within-month repeat
    reads as a loop).
  * empty pool -> fall through (return None) + ONE deduped alert naming the gym
    ('media pool empty for {gym} — ask for photos'). Never reuse a cooling-down
    asset to fill a gap.

used_count / last_used_at are stamped ONLY at stage time (stamp_use, called by the
builder once the PENDING row is assembled) and ROLLED BACK on a coach deny
(rollback_use / observe_denials), so a denied post returns to the pool.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import gym_media_index as _idx

REUSE_COOLDOWN_DAYS = 90
_POOL_EMPTY_STAMP = "pool_empty:{}"          # per gym
_USE_KEY = "gym_media_use:{}:{}"             # gym base key, post_date


def _now_utc(now=None):
    return now or datetime.now(timezone.utc)


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def base_gym_key(account_key):
    """The gym base key a per-platform account key rolls up to (pierce_ig ->
    pierce), matching podcast_selector.base_gym_key / real_month_run."""
    base = str(account_key or "")
    for suf in ("_ig", "_fb"):
        if base.endswith(suf):
            return base[: -len(suf)]
    return base


def pick_media(gym_id, kind_preference=None, *, store=None, now=None, exclude_ids=()):
    """The least-used, longest-unused eligible, not-excluded asset for THIS gym, or
    None.

    Order: used_count ASC, last_used_at ASC NULLS FIRST (id tiebreak for
    determinism). Skips any asset used inside REUSE_COOLDOWN_DAYS and any asset
    already used THIS calendar month. `kind_preference` ('photo'|'video') filters
    to that kind when supplied (the planner's faces/community/results slots prefer
    photos); with no match of the preferred kind the pool is treated as empty for
    that slot (the caller falls through). `exclude_ids` skips assets that just
    failed validation in this same slot.

    Empty pool -> ONE deduped alert naming the gym and None. A cooling-down asset
    is NEVER reused to fill the gap."""
    base = base_gym_key(gym_id)
    store = store or _idx.default_store()
    if not store.available():
        print("[gym-media-selector] store unavailable; no asset selected (lane unarmed)")
        return None
    now = _now_utc(now)
    try:
        assets = store.list_assets(base)
    except Exception as e:  # noqa: BLE001 - a read failure is an empty pick, not a crash
        print(f"[gym-media-selector] asset read failed for {base}: "
              f"{type(e).__name__}: {e}")
        return None

    cutoff = now - timedelta(days=REUSE_COOLDOWN_DAYS)
    month = now.strftime("%Y-%m")

    candidates = []
    for a in assets:
        # TENANT re-assertion (defense in depth): even though the store filtered by
        # gym, never trust a row whose gym_id does not match this pick.
        if str(a.get("gym_id") or "") != base:
            continue
        if a.get("eligible") is not True:      # null (unprobed) and false fail closed
            continue
        if a.get("excluded_by_coach"):
            continue
        if a.get("id") in exclude_ids:
            continue
        if kind_preference and a.get("kind") != kind_preference:
            continue
        used_at = _parse_ts(a.get("last_used_at"))
        if used_at is not None:
            if used_at > cutoff:               # inside the 90-day reuse cooldown
                continue
            if used_at.strftime("%Y-%m") == month:   # already used this month
                continue
        candidates.append(a)

    if not candidates:
        _idx.dedup_alert(
            _POOL_EMPTY_STAMP.format(base),
            f"media pool empty for {base} — ask for photos. No eligible, "
            "not-hidden asset outside the 90-day reuse cooldown. The slot falls "
            "through to the existing media logic; nothing on cooldown was reused.")
        return None
    # Pool healthy again: reset the empty stamp so a future empty pool alerts once.
    _idx.clear_alert_stamp(_POOL_EMPTY_STAMP.format(base))

    _floor = datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda a: (
        int(a.get("used_count") or 0),
        _parse_ts(a.get("last_used_at")) or _floor,      # NULLS FIRST
        str(a.get("id") or "")))
    return candidates[0]


# ---- usage stamping + deny rollback ------------------------------------------
def stamp_use(asset, gym_id, post_date, *, store=None, now=None):
    """Stamp used_count += 1 and last_used_at = now — called ONLY when the slot is
    actually STAGED (the builder, after the PENDING row is assembled). Records prior
    values in kv so a coach deny rolls the stamp back and the asset returns to the
    pool."""
    base = base_gym_key(gym_id)
    store = store or _idx.default_store()
    now = _now_utc(now)
    prev_count = int(asset.get("used_count") or 0)
    prev_last = asset.get("last_used_at")
    store.update_asset(asset["id"], {
        "used_count": prev_count + 1,
        "last_used_at": now.isoformat(),
    })
    from . import db
    db.kv_set(_USE_KEY.format(base, post_date), json.dumps({
        "asset_id": asset["id"],
        "gym_id": base,
        "prev_used_count": prev_count,
        "prev_last_used_at": prev_last,
        "staged_at": now.isoformat(),
        "rolled_back": False,
    }))


def rollback_use(gym_id, post_date, *, store=None):
    """Roll a staged asset's usage stamp back (the coach denied the post): the asset
    returns to the pool exactly as it was. Idempotent. Returns True when a rollback
    actually happened."""
    from . import db
    base = base_gym_key(gym_id)
    key = _USE_KEY.format(base, post_date)
    try:
        rec = json.loads(db.kv_get(key, "") or "{}")
    except Exception:
        rec = {}
    if not rec or rec.get("rolled_back"):
        return False
    store = store or _idx.default_store()
    if not store.available():
        return False
    store.update_asset(rec["asset_id"], {
        "used_count": int(rec.get("prev_used_count") or 0),
        "last_used_at": rec.get("prev_last_used_at"),
    })
    rec["rolled_back"] = True
    db.kv_set(key, json.dumps(rec))
    return True


def rollback_asset(asset_id, *, store=None):
    """Return a specific asset to the pool by id, regardless of which slot staged it
    — used when the coach HIDES an asset a pending row is using (the row is flipped
    back with reject_reason='media_hidden' and the asset's usage stamp is undone).
    Scans the gym_media_use kv records for the matching asset. Idempotent."""
    from . import db
    store = store or _idx.default_store()
    if not store.available():
        return False
    rolled = False
    for key, rec in _use_records():
        if rec.get("asset_id") != asset_id or rec.get("rolled_back"):
            continue
        store.update_asset(rec["asset_id"], {
            "used_count": int(rec.get("prev_used_count") or 0),
            "last_used_at": rec.get("prev_last_used_at"),
        })
        rec["rolled_back"] = True
        db.kv_set(key, json.dumps(rec))
        rolled = True
    return rolled


def on_draft_denied(draft, *, store=None):
    """Slack deny-path hook: a denied gym-media draft rolls its asset's usage stamp
    back so the asset returns to the pool. Any other draft type is a no-op."""
    if (getattr(draft, "draft_type", "") or "").strip().lower() != "gym_media":
        return False
    day_key = getattr(draft, "day_key", "") or ""
    account_key = getattr(draft, "account_key", "") or ""
    if not day_key or not account_key:
        return False
    return rollback_use(account_key, day_key, store=store)


# ---- nightly denial observer (portal denies happen out-of-band) --------------
def _use_records():
    """Every gym_media_use kv record as (key, dict), unreadable rows skipped."""
    from . import db
    out = []
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM kv WHERE key LIKE 'gym_media_use:%'").fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            out.append((r["key"], json.loads(r["value"] or "{}")))
        except Exception:
            continue
    return out


def _default_fetch_rows(gym_id, post_date, http=None):
    """content_calendar rows (id, status, pillar, draft_type) for one gym+date, ALL
    statuses. Offline/creds-absent -> [] (the sweep then does nothing; safe)."""
    from . import config
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return []
    if http is None:
        import requests  # lazy
        http = requests
    r = http.get(
        f"{url.rstrip('/')}/rest/v1/content_calendar",
        params={"gym_id": f"eq.{gym_id}", "post_date": f"eq.{post_date}",
                "select": "id,status,pillar,draft_type"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30)
    if r.status_code >= 400:
        return []
    return r.json() or []


_LIVE_STATUSES = ("pending", "approved", "publishing", "published", "scheduled")


def observe_denials(*, store=None, fetch_rows=None):
    """Sweep every un-rolled-back gym_media_use record: when the calendar shows the
    slot's gym-media row DENIED (and no live gym-media row remains on that date),
    roll the usage stamp back so the asset returns to the pool. Mirrors
    podcast_selector.observe_denials. Idempotent. Returns a summary."""
    fetch_rows = fetch_rows or _default_fetch_rows
    checked = rolled = 0
    for key, rec in _use_records():
        if not rec or rec.get("rolled_back"):
            continue
        try:
            _, gym_id, post_date = key.split(":", 2)
        except ValueError:
            continue
        checked += 1
        try:
            rows = fetch_rows(gym_id, post_date) or []
        except Exception:
            continue
        mine = [r for r in rows
                if str(r.get("draft_type") or "").strip().lower() == "gym_media"]
        if not mine:
            continue
        denied = any(str(r.get("status") or "").lower() == "denied" for r in mine)
        live = any(str(r.get("status") or "").lower() in _LIVE_STATUSES for r in mine)
        if denied and not live:
            if rollback_use(gym_id, post_date, store=store):
                rolled += 1
    return {"checked": checked, "rolled_back": rolled}
