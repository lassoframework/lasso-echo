"""
podcast_selector.py — pick the next podcast clip (PODCAST_LIBRARY_BUILD_SPEC.md
Wave 3): the least-used, longest-unused POSTABLE clip.

Rails (never weakened):
  * Only postable == TRUE assets are candidates. Unprobed (postable null) is
    NOT selectable — fail closed, never post an unprobed file.
  * CLIP cooldown: never the same clip inside exclude_recent_days (120).
  * EPISODE cooldown: never the same episode inside 21 days — four clips from
    one episode dumped in a week reads as a loop, the exact failure this fixes.
  * used_count / last_used_at are stamped ONLY when the slot is staged
    (stamp_use, called by the builder at stage time) and ROLLED BACK when the
    coach denies the post (rollback_use). A denied post returns to the pool.
    Denials are observed two ways, mirroring how the deny-backfill jobs watch
    denials: a direct hook on the Slack deny path (on_draft_denied, wired in
    approvals.py) and a nightly observer over denied content_calendar rows
    (observe_denials, run by the indexer job).
  * Empty pool -> ONE deduped alert ('podcast clip pool empty') and None: the
    caller falls through to the existing category logic. NEVER a repost of a
    cooling-down clip to fill a gap.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import config, podcast_index as _idx

CLIP_COOLDOWN_DAYS = 120
EPISODE_COOLDOWN_DAYS = 21
_POOL_EMPTY_STAMP = "pool_empty"
_USE_KEY = "podcast_use:{}:{}"  # gym base key, post_date


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
    """The gym base key a per-platform account key rolls up to (lasso_ig ->
    lasso), matching real_month_run's cadence base resolution."""
    base = str(account_key or "")
    for suf in ("_ig", "_fb"):
        if base.endswith(suf):
            return base[: -len(suf)]
    return base


def _grounding_feed_episodes():
    """The set of episode numbers the show RSS feed grounds, best-effort. Any
    failure -> empty set (grounding then leans on notes_doc_id alone). Never
    raises out of the pick."""
    try:
        from . import podcast_feed_notes as _fn
        return set(_fn.episode_map().keys())
    except Exception as e:  # noqa: BLE001
        print(f"[podcast-selector] feed grounding lookup failed "
              f"({type(e).__name__}: {e}); grounding on notes_doc_id only")
        return set()


def _is_groundable(asset, feed_episodes):
    """A clip is groundable when the RSS feed has its episode OR its own
    notes_doc_id is set. Either source lets the caption ground in real text."""
    if str(asset.get("notes_doc_id") or "").strip():
        return True
    ep = asset.get("episode")
    try:
        return ep is not None and int(ep) in feed_episodes
    except (TypeError, ValueError):
        return False


def pick_clip(exclude_recent_days: int = CLIP_COOLDOWN_DAYS, *, store=None,
              now=None, exclude_ids=(), require_notes=True, feed_episodes=None):
    """The least-used, longest-unused STAGEABLE clip, or None.

    STAGEABLE = postable AND (when require_notes, the default) the clip's episode
    is GROUNDABLE. GROUNDABLE = the episode is in the RSS feed (`feed_episodes`)
    OR the clip's own notes_doc_id is set. The caption must ground in a real
    source, so a clip whose episode has neither can never stage; selecting only
    groundable clips means a stray un-groundable clip never sinks a slot that has
    groundable clips available. `require_notes=False` exposes the full postable
    pool for the builder's belt-and-suspenders rail and for tests.

    `feed_episodes` is the set of episode numbers the show feed grounds; pass it
    (a set) to keep the pick offline/deterministic, or leave None to have it
    fetched best-effort from podcast_feed_notes (empty on any failure, so
    grounding then leans on notes_doc_id alone).

    Order: used_count ASC, last_used_at ASC NULLS FIRST (id tiebreak for
    determinism). Skips any clip used inside `exclude_recent_days` and any
    EPISODE used inside EPISODE_COOLDOWN_DAYS (episode last-use = the max
    last_used_at across all of that episode's assets). `exclude_ids` lets a
    caller skip assets that just failed validation in this same slot.

    Empty pool -> ONE deduped ops alert and None (the slot falls through to the
    existing category logic; a clip on cooldown is never reposted to fill it)."""
    store = store or _idx.default_store()
    if not store.available():
        print("[podcast-selector] store unavailable; no clip selected (lane unarmed)")
        return None
    now = _now_utc(now)
    try:
        assets = store.list_assets()
    except Exception as e:  # noqa: BLE001 - a read failure is an empty pick, not a crash
        print(f"[podcast-selector] asset read failed: {type(e).__name__}: {e}")
        return None

    clip_cutoff = now - timedelta(days=int(exclude_recent_days))
    episode_cutoff = now - timedelta(days=EPISODE_COOLDOWN_DAYS)

    # Only reach for the feed when it can actually matter: require_notes is on,
    # the caller did not supply the set, and at least one postable clip lacks a
    # Doc (its episode might still ground via the feed). A pool that is fully
    # note-linked never needs a fetch.
    if require_notes and feed_episodes is None:
        need_feed = any(a.get("postable") is True
                        and not str(a.get("notes_doc_id") or "").strip()
                        for a in assets)
        feed_episodes = _grounding_feed_episodes() if need_feed else set()
    feed_episodes = feed_episodes or set()

    episode_last_use = {}
    for a in assets:
        ts = _parse_ts(a.get("last_used_at"))
        if ts is None:
            continue
        ep = a.get("episode")
        if ep not in episode_last_use or ts > episode_last_use[ep]:
            episode_last_use[ep] = ts

    candidates = []
    for a in assets:
        if a.get("postable") is not True:      # null (unprobed) and false both fail closed
            continue
        if require_notes and not _is_groundable(a, feed_episodes):
            continue  # neither feed nor Doc grounds this episode: never stageable
        if a.get("id") in exclude_ids:
            continue
        used_at = _parse_ts(a.get("last_used_at"))
        if used_at is not None and used_at > clip_cutoff:
            continue
        ep_used = episode_last_use.get(a.get("episode"))
        if ep_used is not None and ep_used > episode_cutoff:
            continue
        candidates.append(a)

    if not candidates:
        _idx.dedup_alert(_POOL_EMPTY_STAMP,
                         "podcast clip pool empty: no postable clip outside the "
                         "120d clip / 21d episode cooldowns. The slot falls "
                         "through to the existing category logic; nothing was "
                         "reposted to fill the gap.")
        return None
    # Pool is healthy again: reset the empty-pool stamp so a FUTURE empty pool
    # alerts once more.
    _idx.clear_alert_stamp(_POOL_EMPTY_STAMP)

    _floor = datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda a: (
        int(a.get("used_count") or 0),
        _parse_ts(a.get("last_used_at")) or _floor,   # NULLS FIRST
        str(a.get("id") or "")))
    return candidates[0]


# ---- usage stamping + deny rollback ------------------------------------------

def stamp_use(asset, gym_id, post_date, *, store=None, now=None):
    """Stamp used_count += 1 and last_used_at = now on the asset — called ONLY
    when the slot is actually STAGED (the builder, after the pending row's draft
    is fully assembled). Records the prior values in kv so a coach deny rolls
    the stamp back and the clip returns to the pool."""
    store = store or _idx.default_store()
    now = _now_utc(now)
    prev_count = int(asset.get("used_count") or 0)
    prev_last = asset.get("last_used_at")
    store.update_asset(asset["id"], {
        "used_count": prev_count + 1,
        "last_used_at": now.isoformat(),
    })
    from . import db
    db.kv_set(_USE_KEY.format(base_gym_key(gym_id), post_date), json.dumps({
        "asset_id": asset["id"],
        "episode": asset.get("episode"),
        "prev_used_count": prev_count,
        "prev_last_used_at": prev_last,
        "staged_at": now.isoformat(),
        "rolled_back": False,
    }))


def rollback_use(gym_id, post_date, *, store=None):
    """Roll a staged clip's usage stamp back (the coach denied the post): the
    asset returns to the pool exactly as it was. Idempotent: a second call for
    the same slot is a no-op. Returns True when a rollback actually happened."""
    from . import db
    key = _USE_KEY.format(base_gym_key(gym_id), post_date)
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


def on_draft_denied(draft, *, store=None):
    """Slack deny-path hook (wired into approvals.handle_action's deny branch,
    best effort): a denied PODCAST draft rolls its clip's usage stamp back so
    the clip returns to the pool. Any other category is a no-op."""
    if (getattr(draft, "category", "") or "").strip().lower() != "podcast":
        return False
    day_key = getattr(draft, "day_key", "") or ""
    account_key = getattr(draft, "account_key", "") or ""
    if not day_key or not account_key:
        return False
    return rollback_use(account_key, day_key, store=store)


# ---- nightly denial observer (portal denies happen out-of-band) ---------------

def _use_records():
    """Every podcast_use kv record as (key, dict), unreadable rows skipped."""
    from . import db
    out = []
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM kv WHERE key LIKE 'podcast_use:%'").fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            out.append((r["key"], json.loads(r["value"] or "{}")))
        except Exception:
            continue
    return out


def _default_fetch_rows(gym_id, post_date, http=None):
    """content_calendar rows (id, status, pillar) for one gym+date, ALL statuses
    (rows_in_range excludes denied, which is exactly what this sweep needs to
    see). Offline/creds-absent -> [] (the sweep then does nothing; safe)."""
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
                "select": "id,status,pillar"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30)
    if r.status_code >= 400:
        return []
    return r.json() or []


_LIVE_STATUSES = ("pending", "approved", "publishing", "published", "scheduled")


def observe_denials(*, store=None, fetch_rows=None):
    """Sweep every un-rolled-back podcast_use record: when the calendar shows the
    slot's podcast row DENIED (and no live podcast row remains on that date),
    roll the usage stamp back so the clip returns to the pool. This mirrors how
    the deny-backfill jobs observe denials (read denied rows, act once, stamp) —
    portal denies happen out-of-band from the Slack deny hook, so both paths
    exist. Idempotent via the record's rolled_back flag. Returns a summary."""
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
        pod = [r for r in rows
               if str(r.get("pillar") or "").strip().lower() == "podcast"]
        if not pod:
            continue
        denied = any(str(r.get("status") or "").lower() == "denied" for r in pod)
        live = any(str(r.get("status") or "").lower() in _LIVE_STATUSES for r in pod)
        if denied and not live:
            if rollback_use(gym_id, post_date, store=store):
                rolled += 1
    return {"checked": checked, "rolled_back": rolled}
