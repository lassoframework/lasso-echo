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
  * GLOBAL ONCE-USED RULE (Blake, 2026-10-02): an asset with used_count > 0 is
    NEVER automatically selected again — not after 90 days, not after the
    calendar-month guard. Every asset is one-and-done for automatic selection.
    The explicit cooldown_fallback lane must not bypass this either.
  * 90-day reuse cooldown: never an asset used inside 90 days.
  * never the same asset twice in a MONTH (an asset used this calendar month is
    out, even if the 90-day window has not fully elapsed — a within-month repeat
    reads as a loop).
  * empty pool -> fall through (return None) + ONE deduped alert naming the gym
    ('media pool empty for {gym} — ask for photos'). Never reuse a cooling-down
    asset to fill a gap.

The automatic planner keeps those rules without exception. ``cooldown_fallback``
is a separate, explicit-user-action lane used only by the portal media swap: after
the normal pool is exhausted it can return the least-recently-used safe asset that
is not on the live forward book. Explicit client reuse promises remain hard gates.

used_count / last_used_at are stamped ONLY at stage time (stamp_use, called by the
builder once the PENDING row is assembled). STAGE-USE IS PERMANENT (Blake,
2026-10-02): once a photo is staged onto a calendar date it is never offered
again — not after a coach deny, not after a free media swap. rollback_use only
marks the use-records settled so a repeated deny/sweep is idempotent; it no
longer restores the counters, so a denied or swapped-out asset stays out of the
pool alongside the published ones.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from . import gym_media_index as _idx

REUSE_COOLDOWN_DAYS = 90

# DRAFT GLOBAL VISUAL LEDGER (PR235, 2026-10-03): when AGENT_VISUAL_GLOBAL_LEDGER
# is explicitly enabled, the DRAFT global exact-byte usage ledger
# (public.visual_global_usage, migrations/DRAFT_visual_global_history_20261002.sql)
# is authoritative for global byte reuse: a photo whose exact bytes the ledger
# shows previously used (reserved/published/released) by any canonical tenant is
# excluded from the pickable photo set BEFORE any caller decides whether the
# infographic fallback is allowed. The flag is tri-state: an unrecognized
# non-empty value is AMBIGUOUS and fails closed. Default OFF = byte-for-byte
# legacy behavior. This is a read-side belt only; the calendar claim trigger
# remains the authority and this read never mutates ledger state.
GLOBAL_LEDGER_FLAG_ENV = "AGENT_VISUAL_GLOBAL_LEDGER"


class GlobalLedgerUnavailable(RuntimeError):
    """The global visual ledger could not prove a photo's global usage status."""


def global_ledger_flag():
    """Tri-state read of AGENT_VISUAL_GLOBAL_LEDGER: True (on), False (off or
    unset), None (ambiguous value — fail closed). Matches the truthy set used by
    visual_writer_prepare.enabled(); anything else is not a silent default."""
    raw = (os.environ.get(GLOBAL_LEDGER_FLAG_ENV, "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("", "0", "false", "no", "off"):
        return False
    return None


def _md5_fingerprint(asset):
    """The global ledger's canonical key for an asset's exact bytes, or "".

    visual_global_usage is keyed by 'md5:<32 hex>' only (the schema deliberately
    uses MD5 for ALL media because Drive supplies MD5 natively). A SHA-256-only
    content hash has no global representation, so it can never be PROVEN unused
    cross-client — callers must treat it as unverifiable, not as clean."""
    digest = _byte_hash(asset)
    if re.fullmatch(r"[0-9a-f]{32}", digest):
        return f"md5:{digest}"
    return ""


def _complete_ledger_read(http, url, params, headers, *, maximum=100):
    """Make one exact, bounded ledger read and prove it was complete.

    Each visual_global_usage query is for at most 100 primary-key fingerprints,
    so it can match at most 100 rows.  One Range 0-99 request avoids offset
    pagination, whose moving window could incorrectly miss a concurrent row.
    """
    page_headers = dict(headers)
    page_headers.update({"Prefer": "count=exact", "Range-Unit": "items",
                         "Range": "0-99"})
    try:
        response = http.get(url, params=params, headers=page_headers, timeout=30)
    except Exception as exc:  # noqa: BLE001 - an incomplete ledger is unsafe
        raise GlobalLedgerUnavailable("global visual ledger read failed") from exc
    if not 200 <= response.status_code < 300:
        raise GlobalLedgerUnavailable(
            f"global visual ledger read failed ({response.status_code})")
    try:
        rows = response.json()
    except Exception as exc:  # noqa: BLE001
        raise GlobalLedgerUnavailable("global visual ledger read was malformed") from exc
    if not isinstance(rows, list):
        raise GlobalLedgerUnavailable("global visual ledger page was malformed")
    response_headers = getattr(response, "headers", {}) or {}
    content_range = (response_headers.get("Content-Range")
                     or response_headers.get("content-range") or "")
    match = re.fullmatch(r"(\*|\d+-\d+)/(\d+)", str(content_range))
    if not match:
        raise GlobalLedgerUnavailable("global visual ledger did not prove page completeness")
    span, total_text = match.groups()
    total = int(total_text)
    if total > maximum or len(rows) != total:
        raise GlobalLedgerUnavailable("global visual ledger page was incomplete")
    if total == 0:
        if span != "*":
            raise GlobalLedgerUnavailable("global visual ledger page was incomplete")
    elif span != f"0-{total - 1}":
        raise GlobalLedgerUnavailable("global visual ledger page was incomplete")
    return rows


def cross_client_used_fingerprints(base, fingerprints, *, http=None):
    """The subset of `fingerprints` ('md5:<hex>') the DRAFT global visual ledger
    shows used by any canonical tenant, including this gym's current tenant.

    Read-only PostgREST against the DRAFT schema (tenant_alias +
    visual_global_usage), the same tables visual_writer_prepare's writer side
    registers into — never a client-side guess. Any state counts: staged
    (reserved), confirmed (published) and released staged bytes all remain
    globally consumed per the ledger contract. FAILS CLOSED: missing creds, a
    failed/malformed read, an unmapped tenant, an ambiguous ledger row, or an
    unknown state raises GlobalLedgerUnavailable; the caller must not treat
    uncertainty as 'unused'."""
    from . import config
    base = str(base or "").strip()
    fingerprints = sorted({str(f) for f in (fingerprints or ()) if f})
    if not base or not fingerprints:
        return set()
    url = (config.supabase_url() or "").rstrip("/")
    key = config.supabase_service_key()
    if not url or not key:
        raise GlobalLedgerUnavailable("global visual ledger credentials are not configured")
    if http is None:
        import requests  # lazy, matches the repo pattern
        http = requests
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Accept": "application/json"}
    # Require the gym's immutable canonical mapping even though every ledger
    # usage excludes: an unmapped raw key can never be proven to have complete
    # history across aliases or imports.
    rows = _complete_ledger_read(
        http, f"{url}/rest/v1/tenant_alias",
        {"select": "alias_key,tenant_id", "alias_key": f"eq.{base}"}, headers)
    if len(rows) != 1 or not isinstance(rows[0], dict) \
            or rows[0].get("alias_key") != base:
        raise GlobalLedgerUnavailable(
            f"{base} has no canonical visual tenant mapping")
    try:
        uuid.UUID(str(rows[0].get("tenant_id")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise GlobalLedgerUnavailable(
            f"{base} has no canonical visual tenant mapping") from exc
    wanted = set(fingerprints)
    used = set()
    ordered = sorted(wanted)
    for start in range(0, len(ordered), 100):
        batch = ordered[start:start + 100]
        rows = _complete_ledger_read(
            http, f"{url}/rest/v1/visual_global_usage",
            {"select": "fingerprint,tenant_id,state,ambiguous",
             "fingerprint": "in.(" + ",".join(batch) + ")"}, headers)
        seen = set()
        for row in rows:
            if not isinstance(row, dict) \
                    or row.get("fingerprint") not in batch \
                    or row.get("fingerprint") in seen \
                    or row.get("state") not in ("reserved", "published", "released") \
                    or type(row.get("ambiguous")) is not bool:
                raise GlobalLedgerUnavailable(
                    "global visual usage returned an unreadable row")
            try:
                uuid.UUID(str(row.get("tenant_id")))
            except (TypeError, ValueError, AttributeError) as exc:
                raise GlobalLedgerUnavailable(
                    "global visual usage returned an unreadable row") from exc
            seen.add(row["fingerprint"])
            if row["ambiguous"]:
                # Sticky uncertainty is never cleared by ordinary writes; an
                # ambiguous usage row can never prove bytes are free.
                raise GlobalLedgerUnavailable(
                    "global visual usage row is ambiguous (unreconciled)")
            # A prior usage is permanent even when a gym's alias now resolves to the
            # same canonical tenant: imports, deletions, alias moves, and local asset
            # counters cannot establish that the exact bytes are safe to reuse.
            used.add(row["fingerprint"])
    return used
_POOL_EMPTY_STAMP = "pool_empty:{}"          # per gym
_USE_KEY = "gym_media_use:{}:{}"             # gym base key, post_date


def _publishing_gym(base):
    """True when this gym is live enough that an empty photo pool is a real problem
    (its publish flag is ON). Unknown gyms and any lookup failure answer True so a
    live gym's empty pool is never silently swallowed."""
    try:
        from . import db as _db
        row = _db.gym_get(base) or _db.gym_get(f"{base}_ig")
        if not row:
            return True
        flag = str(dict(row).get("publish_flag") or "").strip().upper()
        return flag != "OFF"
    except Exception:
        return True


def _now_utc(now=None):
    return now or datetime.now(timezone.utc)


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean_moderation_evidence(asset):
    """Only evidence for this exact Drive file and byte version can clear it.

    This validates binding, not the identity of a scan provider. A trusted scan
    producer is still required before operators can safely populate evidence.
    """
    evidence = asset.get("moderation_json")
    if asset.get("moderation_status") != "clean" or not isinstance(evidence, dict):
        return False
    content_hash = str(asset.get("content_hash") or "").strip()
    if not content_hash or not asset.get("id") or not asset.get("gym_id"):
        return False
    return (evidence.get("verdict") == "clean"
            and isinstance(evidence.get("provider"), str)
            and bool(evidence["provider"].strip())
            and evidence.get("content_hash") == content_hash
            and evidence.get("asset_id") == asset["id"]
            and evidence.get("gym_id") == asset["gym_id"]
            and evidence.get("people_detected") is asset.get("people_detected")
            and _parse_ts(evidence.get("observed_at")) is not None)


def is_usable(asset):
    """Is this media_asset row one this selector would ever hand to a post?

    THE ONE IMPLEMENTATION of the eligibility half of the pick, exported so nothing
    else has to re-derive it. `pick_media` calls it below, and so does
    client_dm_support.probes, which reports a library size to the gym owner: counting
    every row instead told a client their posts would draw from six unprobed videos
    this predicate rejects. A number told to a client has to be the number their posts
    actually run on, which means one predicate, not two.

    Deliberately NOT the whole pick: the cooldown and the this-month rule are about
    WHEN an asset may be reused, not whether it is usable at all.
    """
    a = asset or {}
    if a.get("eligible") is not True:        # null (unprobed) and false fail closed
        return False
    if a.get("excluded_by_coach"):
        return False
    if a.get("review_status") != "approved":
        return False
    if not a.get("reviewed_by") or not _parse_ts(a.get("reviewed_at")):
        return False
    if not str(a.get("content_hash") or "").strip():
        return False
    if a.get("review_content_hash") != a.get("content_hash"):
        return False
    if not _clean_moderation_evidence(a):
        return False
    # Photo releases are not a publishing requirement. Safety moderation remains
    # byte-bound and fail-closed above. Clean automatic moderation approves the
    # exact reviewed hash. Legacy consent columns remain readable for old rows but
    # never decide whether a clean, reviewed asset can publish.
    return True


def base_gym_key(account_key):
    """The gym base key a per-platform account key rolls up to (pierce_ig ->
    pierce), matching podcast_selector.base_gym_key / real_month_run. '_gbp'
    is stripped too: a Google Business Profile lane key must roll up to the
    same base so a GBP-used asset shares the gym's pool and reuse history."""
    base = str(account_key or "")
    for suf in ("_ig", "_fb", "_gbp"):
        if base.endswith(suf):
            return base[: -len(suf)]
    return base


def drive_asset_claim_id(gym_id, asset_id):
    """Legacy asset-ID claim key, retained for outstanding reservations."""
    return f"gbp_media:{base_gym_key(gym_id)}:{asset_id}"


def drive_content_claim_id(gym_id, asset):
    """Shared atomic reservation key for one gym's verified Drive bytes."""
    digest = _byte_hash(asset)
    if not digest or not is_usable(asset) or str(asset.get("gym_id")) != base_gym_key(gym_id):
        raise ValueError("Drive asset has no verified content hash")
    return f"gbp_media:{base_gym_key(gym_id)}:hash:{digest}"


def claim_drive_content(gym_id, asset, store):
    """Honor old ID claims, then atomically reserve the canonical byte key."""
    from . import db
    base = base_gym_key(gym_id)
    claim_id = drive_content_claim_id(base, asset)
    rows = store.list_assets(base)
    current = next((row for row in rows if str(row.get("id")) == str(asset["id"])
                    and str(row.get("gym_id")) == base), None)
    if current is None or _byte_hash(current) != _byte_hash(asset) or not is_usable(current):
        raise ValueError("Drive asset changed before claim")
    claimed = db.drive_asset_claimed_ids(base)
    if _byte_hash(asset) in _claimed_hashes(rows, claimed, base):
        return None
    state, _ = db.socialapi_claim(claim_id, f"{base}_gbp")
    return claim_id if state == "won" else None


def _has_prior_use(asset):
    """Fail closed on inconsistent use counters and preserve timestamp evidence."""
    try:
        count = int(asset.get("used_count") or 0)
    except (TypeError, ValueError):
        return True
    return count != 0 or bool(asset.get("last_used_at"))


def _byte_hash(asset):
    value = str(asset.get("content_hash") or "").strip().lower()
    return value if re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{64})", value) else ""


def _claimed_hashes(assets, claimed_ids, base):
    """Return claimed byte hashes, or fail closed on missing claim metadata."""
    canonical = {token[5:] for token in claimed_ids if token.startswith("hash:")}
    if any(not re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{64})", h)
           for h in canonical):
        raise ValueError("canonical claim hash unreadable")
    claimed_ids = {token for token in claimed_ids if not token.startswith("hash:")}
    claimed = {str(asset.get("id")): asset for asset in assets
               if str(asset.get("gym_id") or "") == base
               and str(asset.get("id")) in claimed_ids}
    if set(claimed) != claimed_ids:
        raise ValueError("claimed asset row missing")
    hashes = {_byte_hash(asset) for asset in claimed.values()}
    if "" in hashes:
        raise ValueError("claimed asset hash unreadable")
    return hashes | canonical


def pickable(gym_id, kind_preference=None, *, store=None, now=None, exclude_ids=(),
             strict_claims=False, ledger_http=None):
    """Every asset pick_media could hand out RIGHT NOW for this gym, in pick order
    (used_count ASC, last_used_at ASC NULLS FIRST, id tiebreak). [] when the pool is
    empty, the store is down, or the read fails. NEVER alerts: this is the read the
    planner, the Lane-A repeat gate and the portal swap use to ask "could the Drive
    pool fill this slot?" -- only pick_media (the actual pick) owns the pool-empty
    alert. Same eligibility + cooldown + this-month rules as pick_media, ONE
    implementation (pick_media is `pickable(...)[0]`). With strict_claims=True,
    claim read and legacy hash mapping errors propagate to callers that must
    distinguish uncertainty from a proven empty pool. With the DRAFT global
    visual ledger flag ON (see global_ledger_flag), globally previously-used
    photos are excluded and any ledger uncertainty fails closed the same way;
    ledger_http injects the ledger's HTTP client for tests."""
    base = base_gym_key(gym_id)
    store = store or _idx.default_store()
    if not store.available():
        return []
    now = _now_utc(now)
    try:
        assets = store.list_assets(base)
    except Exception as e:  # noqa: BLE001 - a read failure is an empty pick, not a crash
        print(f"[gym-media-selector] asset read failed for {base}: "
              f"{type(e).__name__}: {e}")
        return []
    try:
        from . import db
        claimed_ids = db.drive_asset_claimed_ids(base)
        claimed_hashes = _claimed_hashes(assets, claimed_ids, base)
    except Exception as e:  # noqa: BLE001 - unknown claims close the pool
        if strict_claims:
            raise
        print(f"[gym-media-selector] claim read failed for {base}: {type(e).__name__}")
        return []

    cutoff = now - timedelta(days=REUSE_COOLDOWN_DAYS)
    from .media_reuse_policy import reuse_months, months_before
    if reuse_months(base):
        cutoff = months_before(now, reuse_months(base))
    month = now.strftime("%Y-%m")
    excl = {str(i) for i in (exclude_ids or ()) if i}

    # A re-upload can get a new asset ID while carrying the same bytes. A prior
    # use of either alias consumes the hash for this gym as well.
    used_hashes = {_byte_hash(a)
                   for a in assets if str(a.get("gym_id") or "") == base
                   and _has_prior_use(a) and _byte_hash(a)}

    # DRAFT GLOBAL VISUAL LEDGER (PR235): with the flag ON, photos whose exact
    # bytes the global ledger shows used by ANY client leave the pickable
    # set here — BEFORE the planner picks and before client_infographic_fill's
    # depletion gate may decide the infographic fallback is allowed. OFF keeps
    # byte-for-byte legacy behavior. An ambiguous flag value or ANY ledger
    # uncertainty fails closed exactly like an unreadable claim set: strict
    # callers get the exception, the planning read gets an empty pool.
    ledger_flag = global_ledger_flag()
    ledger_used = set()
    if ledger_flag is not False:
        try:
            if ledger_flag is None:
                raise GlobalLedgerUnavailable(
                    f"{GLOBAL_LEDGER_FLAG_ENV} has an ambiguous value")
            ledger_photos = [a for a in assets
                             if str(a.get("gym_id") or "") == base
                             and str(a.get("kind") or "") == "photo"
                             and is_usable(a)]
            if any(not _md5_fingerprint(a) for a in ledger_photos):
                raise GlobalLedgerUnavailable(
                    "a usable photo has no global-ledger MD5 identity")
            ledger_used = cross_client_used_fingerprints(
                base,
                [_md5_fingerprint(a) for a in ledger_photos],
                http=ledger_http)
        except Exception as e:  # noqa: BLE001 - unproven bytes close the pool
            if strict_claims:
                raise
            print(f"[gym-media-selector] global ledger read failed for {base}: "
                  f"{type(e).__name__}")
            return []

    candidates = []
    for a in assets:
        # TENANT re-assertion (defense in depth): even though the store filtered by
        # gym, never trust a row whose gym_id does not match this pick.
        if str(a.get("gym_id") or "") != base:
            continue
        # eligible IS TRUE (null/unprobed fails closed) and not hidden by the coach.
        # ONE implementation, shared with client_dm_support.probes — see is_usable.
        if not is_usable(a):
            continue
        if str(a.get("id")) in excl:
            continue
        if (str(a.get("id")) in claimed_ids
                or (_byte_hash(a) and _byte_hash(a) in claimed_hashes)):
            continue
        if kind_preference and a.get("kind") != kind_preference:
            continue
        if ledger_flag and str(a.get("kind") or "") == "photo":
            fp = _md5_fingerprint(a)
            # A photo whose bytes cannot be keyed in the global ledger (no MD5)
            # can never be proven globally unused: fail closed, exclude it.
            if not fp or fp in ledger_used:
                continue
        # GLOBAL ONCE-USED RULE: any prior stage-use is out forever for automatic
        # selection, independent of the cooldown clocks below. Stage-use is
        # permanent — rollback on a deny settles the record WITHOUT restoring
        # used_count, so published, denied, and swapped-out assets alike never
        # return to the pool.
        if _has_prior_use(a) or (_byte_hash(a) and _byte_hash(a) in used_hashes):
            continue
        used_at = _parse_ts(a.get("last_used_at"))
        if used_at is not None:
            if used_at > cutoff:               # inside the 90-day reuse cooldown
                continue
            if used_at.strftime("%Y-%m") == month:   # already used this month
                continue
        candidates.append(a)

    _floor = datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda a: (
        int(a.get("used_count") or 0),
        _parse_ts(a.get("last_used_at")) or _floor,      # NULLS FIRST
        str(a.get("id") or "")))
    return candidates


def cooldown_fallback(gym_id, kind_preference=None, *, store=None, exclude_ids=()):
    """Unused assets for an explicit swap after the normal pool is exhausted.

    This is deliberately narrower than :func:`pickable`: callers must pass every
    asset already carried by the live forward book in ``exclude_ids``.  The
    selector still enforces tenant ownership, byte-bound review/moderation, coach
    exclusions, and kind preference.  A gym with an explicit long-term reuse
    policy (currently Zanshin's nine calendar months) gets no fallback at all.

    Month planning and automatic publishing never call this helper. An asset
    previously staged, or a same-byte re-upload of one, stays unavailable.
    """
    base = base_gym_key(gym_id)
    from .media_reuse_policy import reuse_months
    if reuse_months(base):
        return []
    store = store or _idx.default_store()
    if not store.available():
        return []
    try:
        assets = store.list_assets(base)
    except Exception as e:  # noqa: BLE001 - a read failure is no fallback
        print(f"[gym-media-selector] fallback asset read failed for {base}: "
              f"{type(e).__name__}: {e}")
        return []
    try:
        from . import db
        claimed_ids = db.drive_asset_claimed_ids(base)
        claimed_hashes = _claimed_hashes(assets, claimed_ids, base)
    except Exception as e:  # noqa: BLE001 - unknown claims close the fallback
        print(f"[gym-media-selector] claim read failed for {base}: {type(e).__name__}")
        return []
    excl = {str(i) for i in (exclude_ids or ()) if i}
    used_hashes = {_byte_hash(a)
                   for a in assets if str(a.get("gym_id") or "") == base
                   and _has_prior_use(a) and _byte_hash(a)}
    candidates = []
    for asset in assets:
        if str(asset.get("gym_id") or "") != base:
            continue
        # GLOBAL ONCE-USED RULE: the explicit lane may skip the cooldown clocks
        # but never re-offers an already-staged asset.
        if _has_prior_use(asset) or (_byte_hash(asset) and _byte_hash(asset) in used_hashes):
            continue
        if not is_usable(asset) or str(asset.get("id")) in excl:
            continue
        if (str(asset.get("id")) in claimed_ids
                or (_byte_hash(asset) and _byte_hash(asset) in claimed_hashes)):
            continue
        if kind_preference and asset.get("kind") != kind_preference:
            continue
        candidates.append(asset)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda a: (
        _parse_ts(a.get("last_used_at")) or floor,
        int(a.get("used_count") or 0),
        str(a.get("id") or "")))
    return candidates


def pool_kinds(gym_id, *, store=None, now=None, exclude_ids=()):
    """The media kinds ('photo' / 'video') the gym's Drive pool can hand out right
    now. The media-mix planner reads this to decide whether a video slot is even
    possible; a photo-only pool never gets a video slot asked of it."""
    return {str(a.get("kind") or "") for a in
            pickable(gym_id, store=store, now=now, exclude_ids=exclude_ids)}


def pick_media(gym_id, kind_preference=None, *, store=None, now=None, exclude_ids=()):
    """The least-used, longest-unused eligible, not-excluded asset for THIS gym, or
    None.

    Order: used_count ASC, last_used_at ASC NULLS FIRST (id tiebreak for
    determinism). Skips any asset with used_count > 0 (the global once-used
    rule: a staged asset never auto-selects again), any asset used inside
    REUSE_COOLDOWN_DAYS and any asset already used THIS calendar month.
    `kind_preference` ('photo'|'video') filters
    to that kind when supplied; with no match of the preferred kind the pool is
    treated as empty for that slot (the caller falls through, or -- the media-mix
    builder -- retries with the other kind). `exclude_ids` skips assets that just
    failed validation in this same slot.

    Empty pool -> ONE deduped alert naming the gym and None. A cooling-down asset
    is NEVER reused to fill the gap."""
    base = base_gym_key(gym_id)
    store = store or _idx.default_store()
    if not store.available():
        print("[gym-media-selector] store unavailable; no asset selected (lane unarmed)")
        return None
    candidates = pickable(base, kind_preference, store=store, now=now,
                          exclude_ids=exclude_ids)
    if not candidates:
        # KIND EXHAUSTION IS NOT AN EMPTY POOL (audit D6): the media-mix builder asks
        # for one kind first and falls back to the other, so "no photo left" while
        # videos remain must not page staff to "ask for photos". Alert only when the
        # pool has nothing of ANY kind.
        if kind_preference and pickable(base, None, store=store, now=now,
                                        exclude_ids=exclude_ids):
            return None
        # "Ask for photos" is only actionable for a gym that is actually posting.
        # A gym still onboarding (publish flag OFF, socials not connected yet) has an
        # empty pool BY DEFINITION, and paging staff about it every build is noise
        # that buries the alerts that matter. Fail LOUD: any doubt still alerts.
        if _publishing_gym(base):
            _idx.dedup_alert(
                _POOL_EMPTY_STAMP.format(base),
                f"media pool empty for {base} — ask for photos. No eligible, "
                "not-hidden asset outside the 90-day reuse cooldown. The slot falls "
                "through to the existing media logic; nothing on cooldown was reused.")
        return None
    # Pool healthy again: reset the empty stamp so a future empty pool alerts once.
    _idx.clear_alert_stamp(_POOL_EMPTY_STAMP.format(base))
    return candidates[0]


# ---- usage stamping + deny rollback ------------------------------------------
def _as_records(raw):
    """The list of use-records held under one kv key. A 2x day stages TWO gym-media
    posts on one date, so the value is a LIST. Legacy values (a single dict, written
    before 2026-08-30) are read as a one-element list, so old records still roll back."""
    try:
        val = json.loads(raw or "[]")
    except Exception:  # noqa: BLE001 - an unreadable record is simply skipped
        return []
    if isinstance(val, dict):
        return [val] if val else []
    if not isinstance(val, list):
        return []                                # a scalar (null/5/true) is not a record
    return [r for r in val if isinstance(r, dict)]


def stamp_use(asset, gym_id, post_date, *, store=None, now=None):
    """Stamp used_count += 1 and last_used_at = now — called ONLY when the slot is
    actually STAGED (the builder, after the PENDING row is assembled). The stamp is
    PERMANENT: a coach deny or a media swap settles the kv record (rollback_use) but
    never restores the counters, so a staged asset is never offered again.

    APPENDS to the date's record list rather than replacing it: at 2x two assets are
    staged on one date, and the old single-record write meant the PM stamp clobbered
    the AM one — so a denied AM asset could never be rolled back and silently sat out
    the 90-day cooldown, burning half the gym's pool over a month. Re-staging the SAME
    asset on the same date replaces its record rather than adding a second one (so a
    rollback cannot double-restore). NOTE it is not fully idempotent: the second stamp
    records the already-incremented count as `prev_used_count`, so a later rollback
    leaves a residual +1. Pre-existing; callers stamp once per staged slot."""
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
    key = _USE_KEY.format(base, post_date)
    records = [r for r in _as_records(db.kv_get(key, ""))
               if r.get("asset_id") != asset["id"]]
    records.append({
        "asset_id": asset["id"],
        "gym_id": base,
        "prev_used_count": prev_count,
        "prev_last_used_at": prev_last,
        "staged_at": now.isoformat(),
        "rolled_back": False,
    })
    db.kv_set(key, json.dumps(records))


def rollback_use(gym_id, post_date, *, store=None, asset_id=None,
                 restore_unstaged=False):
    """Settle the staged-use records for one gym+date on a coach deny.

    PERMANENT STAGE-USE (Blake, 2026-10-02): this function NO LONGER restores
    prev_used_count / prev_last_used_at. Once a photo has been staged onto a
    calendar slot it is never offered again — a denied post does not return its
    asset to the pool, matching the rule that already covered published and
    swapped-out assets. What remains here is bookkeeping: mark each matching
    record rolled_back so repeated denies and the nightly observe_denials sweep
    are idempotent. The stamped used_count / last_used_at are left untouched
    (fail closed: a lingering stamp costs one asset; a restored counter would
    re-offer media the coach already saw). Returns True when at least one
    record was settled. Idempotent.

    Settles EVERY un-settled record on that date by default: a 2x day stages two
    gym-media posts, and callers reach that form only once the whole date is
    denied with nothing live left on it (observe_denials' denied-and-not-live
    test, or a 1x deny where the date holds a single post).

    asset_id scopes the settle to ONE asset ON THIS DATE — what a single denied
    card needs when the day's other post still stands. Deliberately date-scoped:
    use-records are never cleared on publish, so a cross-date settle would also
    touch the record of the asset's earlier PUBLISHED post. Published history is
    never rewritten: the stamped counters stay exactly as stage time left them.
    restore_unstaged is only for a draft abandoned before any calendar row was
    persisted or shown. A coach deny, swap or rebuild never sets it."""
    from . import db
    base = base_gym_key(gym_id)
    key = _USE_KEY.format(base, post_date)
    records = _as_records(db.kv_get(key, ""))
    if not records or all(r.get("rolled_back") for r in records):
        return False
    if restore_unstaged:
        store = store or _idx.default_store()
        if not store.available():
            return False
    settled = False
    for rec in records:
        if rec.get("rolled_back"):
            continue
        if asset_id and rec.get("asset_id") != asset_id:
            continue
        if restore_unstaged:
            store.update_asset(rec["asset_id"], {
                "used_count": int(rec.get("prev_used_count") or 0),
                "last_used_at": rec.get("prev_last_used_at"),
            })
        rec["rolled_back"] = True
        settled = True
    if settled:
        db.kv_set(key, json.dumps(records))
    return settled


def rollback_asset(asset_id, *, store=None):
    """Settle every staged-use record for one asset, regardless of which slot
    staged it — used when the coach HIDES an asset a pending row is using (the
    row is flipped back with reject_reason='media_hidden'). Stage-use is
    PERMANENT (2026-10-02): the stamp is left in place so the asset can never be
    re-offered if it is later un-hidden; only the kv records are marked
    rolled_back for idempotency. Scans the gym_media_use kv records for the
    matching asset. Idempotent."""
    del store
    from . import db
    settled_any = False
    for key, records in _use_records():
        touched = False
        for rec in records:
            if rec.get("asset_id") != asset_id or rec.get("rolled_back"):
                continue
            rec["rolled_back"] = True
            touched = True
        if touched:
            # Rewrite the WHOLE list: the day's other post keeps its own record.
            db.kv_set(key, json.dumps(records))
            settled_any = True
    return settled_any


def on_draft_denied(draft, *, store=None):
    """Slack deny-path hook: a denied gym-media draft rolls its asset's usage stamp
    back so the asset returns to the pool. Any other draft type is a no-op."""
    if (getattr(draft, "draft_type", "") or "").strip().lower() != "gym_media":
        return False
    day_key = getattr(draft, "day_key", "") or ""
    account_key = getattr(draft, "account_key", "") or ""
    if not day_key or not account_key:
        return False
    # EXACT when we know the asset: a 2x day stages two gym-media posts, and denying
    # one must return only ITS photo, leaving the day's other (still-standing) post
    # stamped. Scoped to THIS DATE — never a cross-date rollback_asset, which would
    # also undo the same photo's earlier PUBLISHED record and re-pool a live image.
    asset_id = (getattr(draft, "source_media_asset_id", "") or "").strip() or None
    return rollback_use(account_key, day_key, store=store, asset_id=asset_id)


# ---- nightly denial observer (portal denies happen out-of-band) --------------
def _use_records():
    """Every gym_media_use kv record as (key, [record, ...]), unreadable rows skipped.
    One key holds a LIST because a 2x day stages two assets on one date; a legacy
    single-dict value is normalized to a one-element list by _as_records."""
    from . import db
    out = []
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM kv WHERE key LIKE 'gym_media_use:%'").fetchall()
    except Exception:
        return out
    for r in rows:
        # Per-row guard: ONE malformed value must never disable the whole ledger for
        # every gym (the callers all swallow exceptions, so it would fail silently).
        try:
            records = _as_records(r["value"])
        except Exception:  # noqa: BLE001 - an unreadable row is skipped, not fatal
            continue
        if records:
            out.append((r["key"], records))
    return out


# The columns observe_denials filters on. source_media_asset_id is the whole signal:
# a select that omits it makes the sweep a silent no-op (see _default_fetch_rows).
_FETCH_SELECT = "id,status,pillar,source_media_asset_id"


def _default_fetch_rows(gym_id, post_date, http=None):
    """content_calendar rows (id, status, pillar, source_media_asset_id) for one
    gym+date, ALL statuses. Offline/creds-absent -> [] (the sweep then does nothing;
    safe).

    THE SECOND DEAD FILTER (Tough Temple, 2026-09-10): observe_denials was fixed to
    key on source_media_asset_id, but this select never asked PostgREST for that
    column, so every live row still read '' and the sweep still rolled back nothing.
    Portal denies happen out-of-band (no on_draft_denied hook), so this sweep is the
    ONLY thing that returns a portal-denied Drive asset to the pool: with it dead,
    every denied photo stayed stamped for its 90-day cooldown and the pool read as
    empty while 57 eligible videos sat unused. The column is selected explicitly."""
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
                "select": _FETCH_SELECT},
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
    for key, records in _use_records():
        # A date is worth checking while ANY of its staged assets is un-rolled.
        if not any(not r.get("rolled_back") for r in records):
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
        # THE DEAD FILTER. This keyed on draft_type == 'gym_media', but content_calendar
        # has NO draft_type column: it reads None on every live row (measured across 229
        # ENG rows, 2026-08-30). So `mine` was ALWAYS empty and this sweep has never
        # rolled a single asset back since it was written. The sibling
        # podcast_selector.observe_denials was written correctly against `pillar` and
        # this one was simply never ported. A denied photo's real signal is a non-empty
        # source_media_asset_id, present on exactly the photo pillars and absent on
        # every generated one.
        mine = [r for r in rows if str(r.get("source_media_asset_id") or "").strip()]
        if not mine:
            continue
        denied = any(str(r.get("status") or "").lower() == "denied" for r in mine)
        live = any(str(r.get("status") or "").lower() in _LIVE_STATUSES for r in mine)
        if denied and not live:
            if rollback_use(gym_id, post_date, store=store):
                rolled += 1
    return {"checked": checked, "rolled_back": rolled}
