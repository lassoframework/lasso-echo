"""Guarded wiring layer for per-gym visual media groups (global no-repeat
media repair).

Everything in this module sits behind AGENT_MEDIA_GROUP_GUARD (default OFF).
When the flag is OFF every public entry point is a pure passthrough: selectors
and the swap lane see unfiltered candidates and the outbound gates return no
hold, so behavior is byte-identical to before this module existed.

When the flag is ON, a candidate whose visual group is used or reserved on a
DIFFERENT date is excluded/held; a published group is permanently used on every
other date; unknown identity fails closed (held, never recycled); and a
suspected-but-unconfirmed scene match (pHash hamming 7-30 — a conservative
policy band chosen to include the single measured Swift River pair, hamming 28;
<=6 is near-frame only) holds visibly as
media_group_scene_unconfirmed, never approved and never treated as proof of
difference. Same-date siblings may share one group. This mirrors the RPC
semantics in migrations/DRAFT_media_group_usage_20261002.sql so in-repo
callers have correct behavior before that migration is applied.

AUTHORITY NOTE: GroupLedger here is an in-process stand-in. Once the DRAFT
migration is applied, the Postgres RPCs (claim_media_group and friends) are
AUTHORITATIVE — they serialize concurrent claims with row locks, which an
in-process ledger cannot do across workers. This module exists so the seams
(selector, swap, outbound) are wired and fail-closed today; the registered
ledger/groups must be refreshed from the DB tables once they exist. Group
keys are FROZEN at group creation (visual_identity assigns a stable group_id;
keys are never re-derived from membership), so persistence must store
group_id/group_key explicitly — and the DRAFT migration likely also needs a
rejections/audit persistence column (follow-up for the migration owner).

Identity evidence at these seams is byte-level today (Drive content_hash /
local file md5): no pHash is stored on media_asset rows yet (the seam helpers
pass a stored ``phash``/``dct_phash`` field through the moment one exists), so
near-scene detection engages fully once the backfill clusters historical media
(vision.cluster_library) and registers the resulting groups here. Anything
without verifiable identity holds closed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import config
from .visual_identity import (
    AssetRef,
    Classification,
    VisualGroup,
    classify_candidate,
    manual_group_key,
    assign_manual_group,
    confirm_scene_match,
    reject_scene_match,
    MANUAL_KEY_PREFIX,
)

HOLD_USED_OTHER_DATE = "media_group_used_other_date"
HOLD_PERMANENTLY_USED = "media_group_permanently_used"
HOLD_TERMINAL = "media_group_terminal_hold"
HOLD_IDENTITY_UNKNOWN = "media_group_identity_unknown"
HOLD_SCENE_UNCONFIRMED = "media_group_scene_unconfirmed"

_GROUP_TERMINAL_STATUSES = ("exhausted", "unknown", "retired")
_CLAIM_CHANNELS = ("ig", "fb", "story", "gbp")


def enabled():
    """AGENT_MEDIA_GROUP_GUARD, default OFF (config.media_group_guard_enabled)."""
    return config.media_group_guard_enabled()


# ---------------------------------------------------------------------------
# In-process per-gym reservation ledger (mirrors the DRAFT RPC semantics).
# ---------------------------------------------------------------------------


class GroupLedger:
    """In-memory mirror of media_group + media_group_usage for ONE process.

    Semantics match migrations/DRAFT_media_group_usage_20261002.sql exactly:
    same-date siblings share; cross-date claims conflict while reserved;
    published is permanent; release frees the group only when its last
    reserved sibling is removed; terminal statuses (exhausted/unknown/retired)
    never recycle. The DB RPC is authoritative once the migration is applied.
    """

    def __init__(self):
        self._groups = {}   # (gym_id, group_key) -> status
        self._usage = {}    # (gym_id, group_key, date, sibling_id) -> {channel, status}

    # -- group registry ------------------------------------------------------
    def register_group(self, gym_id, group_key, status="active"):
        """Insert a group row. Unknown gym/group pairs fail closed on claim
        (missing_group), exactly like the RPC."""
        self._groups[(str(gym_id), str(group_key))] = str(status or "active")

    def hold(self, gym_id, group_key, status):
        """One-way move to a terminal status. Returns False when the group is
        missing or already terminal (mirrors hold_media_group)."""
        if status not in _GROUP_TERMINAL_STATUSES:
            raise ValueError("invalid media group hold status")
        key = (str(gym_id), str(group_key))
        if self._groups.get(key) != "active":
            return False
        self._groups[key] = status
        return True

    def group_status(self, gym_id, group_key):
        return self._groups.get((str(gym_id), str(group_key)))

    # -- usage queries -------------------------------------------------------
    def _usage_rows(self, gym_id, group_key):
        return [(k, u) for k, u in self._usage.items()
                if k[0] == str(gym_id) and k[1] == str(group_key)]

    def _published_exists(self, gym_id, group_key):
        return any(u["status"] == "published"
                   for _, u in self._usage_rows(gym_id, group_key))

    def _other_date_reserved(self, gym_id, group_key, date):
        return any(u["status"] == "reserved" and k[2] != str(date)
                   for k, u in self._usage_rows(gym_id, group_key))

    def _same_date_exists(self, gym_id, group_key, date):
        return any(k[2] == str(date)
                   for k, _ in self._usage_rows(gym_id, group_key))

    # -- state machine (mirrors the RPCs) ------------------------------------
    def claim(self, gym_id, group_key, date, sibling_id, channel):
        """Reserve a group for one sibling on one date. Returns the RPC outcome
        string: claimed / shared / held_conflict / held_used / held_terminal /
        missing_group. Only claimed/shared mutate."""
        gym_id, group_key, date = str(gym_id), str(group_key), str(date)
        if channel not in _CLAIM_CHANNELS or not sibling_id or not date:
            raise ValueError("invalid media group claim arguments")
        status = self._groups.get((gym_id, group_key))
        if status is None:
            return "missing_group"
        if status != "active":
            return "held_terminal"
        if self._published_exists(gym_id, group_key) and not self._same_date_exists(
                gym_id, group_key, date):
            return "held_used"
        if self._other_date_reserved(gym_id, group_key, date):
            return "held_conflict"
        same_date = self._same_date_exists(gym_id, group_key, date)
        self._usage.setdefault((gym_id, group_key, date, str(sibling_id)),
                               {"channel": channel, "status": "reserved"})
        return "shared" if same_date else "claimed"

    def is_available_for(self, gym_id, group_key, date):
        """True when a claim for this date would succeed (dry run, no writes)."""
        gym_id, group_key, date = str(gym_id), str(group_key), str(date)
        status = self._groups.get((gym_id, group_key))
        if status is None or status != "active":
            return False
        if self._published_exists(gym_id, group_key) and not self._same_date_exists(
                gym_id, group_key, date):
            return False
        return not self._other_date_reserved(gym_id, group_key, date)

    def permanently_used(self, gym_id, group_key):
        """True when the group has any published usage (permanent on all dates)
        or sits in a terminal status."""
        status = self._groups.get((str(gym_id), str(group_key)))
        if status is None or status != "active":
            return True
        return self._published_exists(gym_id, group_key)

    def publish(self, gym_id, group_key, date, sibling_id=None):
        """Flip reserved rows for this date (optionally one sibling) to
        published. Permanent. Returns the number of rows flipped."""
        gym_id, group_key, date = str(gym_id), str(group_key), str(date)
        count = 0
        for k, u in self._usage.items():
            if k[:3] == (gym_id, group_key, date) and u["status"] == "reserved" \
                    and (sibling_id is None or k[3] == str(sibling_id)):
                u["status"] = "published"
                count += 1
        return count

    def release(self, gym_id, group_key, date, sibling_id):
        """Delete one sibling's RESERVED row. Returns the reservations still
        active for the group afterwards — the group is reusable exactly when
        this returns 0 (release-after-last-sibling)."""
        key = (str(gym_id), str(group_key), str(date), str(sibling_id))
        if self._usage.get(key, {}).get("status") == "reserved":
            del self._usage[key]
        return len(self._usage_rows(gym_id, group_key))

    def unavailability_reason(self, gym_id, group_key, date):
        """The HOLD_* reason a claim for this date would produce."""
        status = self._groups.get((str(gym_id), str(group_key)))
        if status is None:
            return HOLD_IDENTITY_UNKNOWN
        if status != "active":
            return HOLD_TERMINAL
        if self._published_exists(gym_id, group_key) and not self._same_date_exists(
                gym_id, group_key, str(date)):
            return HOLD_PERMANENTLY_USED
        if self._other_date_reserved(gym_id, group_key, str(date)):
            return HOLD_USED_OTHER_DATE
        return ""


# ---------------------------------------------------------------------------
# Process registries: how the seams find this gym's groups/ledger without
# threading through files held by other PRs. Populated by the backfill /
# DB-refresh path; empty registry + flag ON fails closed (unknown identity).
# ---------------------------------------------------------------------------

_LEDGERS = {}
_KNOWN_GROUPS = {}


def register_ledger(gym_id, ledger):
    _LEDGERS[str(gym_id)] = ledger


def register_known_groups(gym_id, groups):
    _KNOWN_GROUPS[str(gym_id)] = list(groups or [])


def ledger_for(gym_id):
    return _LEDGERS.get(str(gym_id))


def known_groups_for(gym_id):
    return _KNOWN_GROUPS.get(str(gym_id), [])


def reset_state():
    """Test/backfill helper: drop every registered ledger and group list."""
    _LEDGERS.clear()
    _KNOWN_GROUPS.clear()


def register_manual_group(gym_id, assets, label, *, note=None, ledger=None):
    """Operator/backfill path: force assets into the manual group
    ``{gym_id}::manual:{label}`` (wraps visual_identity.assign_manual_group)
    and register that key in the gym's ledger so claims on it behave like any
    other group (same-date share, cross-date block, publish permanence,
    terminal holds). The manual label pins the frozen group key, so the key is
    stable across every registry operation regardless of membership changes.

    This is the sanctioned Swift River remediation: pHash cannot prove scene
    identity in the 7-30 hamming band, so a human (or corroborated automation)
    asserts it. ``note`` should record who/when/why; it lands in the group's
    append-only audit trail, which lives in the registered known-groups list
    for the life of this process. Returns the manual group key."""
    gym_id = str(gym_id)
    key = manual_group_key(gym_id, label)
    groups = list(known_groups_for(gym_id))
    if assets:
        for asset in assets:
            _, groups = assign_manual_group(asset, groups, label, note=note)
    elif not any(g.gym_id == gym_id and getattr(g, "manual_label", None)
                 and g.group_key == key for g in groups):
        # Empty assertion still creates the (memberless) group so the key
        # exists to claim; no synthetic lineage is invented.
        group = VisualGroup(gym_id=gym_id,
                            manual_label=key.split(MANUAL_KEY_PREFIX, 1)[1])
        if note:
            group.audit.append(note)
        groups.append(group)
    register_known_groups(gym_id, groups)
    led = ledger if ledger is not None else ledger_for(gym_id)
    if led is not None and led.group_status(gym_id, key) is None:
        led.register_group(gym_id, key)
    return key


def register_scene_confirmation(gym_id, asset, into_group_key, *, note=None,
                                ledger=None):
    """Resolve a SCENE_MATCH_CANDIDATE hold as SAME scene (wraps
    visual_identity.confirm_scene_match against the registered known groups):
    the asset moves into the existing group — whose key is frozen, so every
    ledger reservation on it is untouched — with the phash/hamming evidence
    and ``note`` recorded in the group's audit trail. After confirmation the
    normal rules apply to the merged group: same-date share, cross-date block,
    publish permanence. Returns the group key."""
    gym_id = str(gym_id)
    groups = list(known_groups_for(gym_id))
    result, groups = confirm_scene_match(asset, groups, into_group_key,
                                         note=note)
    register_known_groups(gym_id, groups)
    led = ledger if ledger is not None else ledger_for(gym_id)
    if led is not None and led.group_status(gym_id, result.group_key) is None:
        led.register_group(gym_id, result.group_key)
    return result.group_key


def register_scene_rejection(gym_id, asset, suspect_group_key, *, note=None):
    """Resolve a SCENE_MATCH_CANDIDATE hold as DIFFERENT (wraps
    visual_identity.reject_scene_match): the judgment is recorded on the
    suspect group (keyed by the asset's stable identity) with an audit note,
    so future classification of the asset returns NEW_GROUP instead of
    re-holding. Byte-level proof would still match. Returns the suspect key."""
    gym_id = str(gym_id)
    groups = list(known_groups_for(gym_id))
    groups = reject_scene_match(asset, groups, suspect_group_key, note=note)
    register_known_groups(gym_id, groups)
    return suspect_group_key


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardDecision:
    """allow=True lets the candidate through; allow=False holds it visibly with
    a reason (HOLD_*). group_key is set when the candidate matched a group."""

    allow: bool
    reason: str = ""
    group_key: str | None = None
    classification: str = ""


def check_candidate(gym_id, *, target_date, ledger=None, known_groups=None,
                    asset_id="", phash=None, md5=None, sha256=None,
                    drive_file_id=None, storage_key=None):
    """Classify one candidate against the gym's known groups and check the
    ledger. Flag OFF: unconditional allow (passthrough). Flag ON: NEW_GROUP is
    allowed; SAME_GROUP is allowed only when available for target_date;
    UNKNOWN_IDENTITY (or a missing ledger to prove availability) holds closed;
    SCENE_MATCH_CANDIDATE (hamming 7-30, conservative policy band containing
    the one measured Swift River pair at 28)
    ALWAYS holds with media_group_scene_unconfirmed — a suspected scene match
    is neither approval nor proof of difference; it needs corroboration or an
    operator manual-group assignment.
    """
    if not enabled():
        return GuardDecision(True, reason="guard_disabled")
    gym_id = str(gym_id)
    ledger = ledger if ledger is not None else ledger_for(gym_id)
    if known_groups is None:
        known_groups = known_groups_for(gym_id)
    asset = AssetRef(gym_id=gym_id, asset_id=str(asset_id or "candidate"),
                     phash=phash, md5=md5, sha256=sha256,
                     drive_file_id=drive_file_id, storage_key=storage_key)
    result = classify_candidate(asset, list(known_groups))
    if result.classification is Classification.UNKNOWN_IDENTITY:
        return GuardDecision(False, reason=HOLD_IDENTITY_UNKNOWN,
                             classification=result.classification.value)
    if result.classification is Classification.SCENE_MATCH_CANDIDATE:
        return GuardDecision(False, reason=HOLD_SCENE_UNCONFIRMED,
                             group_key=result.group_key,
                             classification=result.classification.value)
    if result.classification is Classification.NEW_GROUP:
        return GuardDecision(True, classification=result.classification.value)
    group_key = result.group_key
    if ledger is None:
        # A matched group with no ledger to prove availability cannot be
        # shown to be free for this date — fail closed.
        return GuardDecision(False, reason=HOLD_IDENTITY_UNKNOWN,
                             group_key=group_key,
                             classification=result.classification.value)
    if ledger.is_available_for(gym_id, group_key, target_date):
        return GuardDecision(True, group_key=group_key,
                             classification=result.classification.value)
    return GuardDecision(
        False,
        reason=ledger.unavailability_reason(gym_id, group_key, target_date)
        or HOLD_USED_OTHER_DATE,
        group_key=group_key,
        classification=result.classification.value,
    )


# ---------------------------------------------------------------------------
# Seam helpers (each is a flag-off passthrough).
# ---------------------------------------------------------------------------


def _local_md5(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except Exception:  # noqa: BLE001 - unreadable bytes are unknown identity
        return None


def swap_candidate_identity(cand):
    """AssetRef identity kwargs for a media_swap candidate. Drive candidates
    carry content_hash (Drive md5Checksum) and, once the backfill lands, a
    stored pHash; local files are hashed lazily, only ever under the flag."""
    if cand.get("source") == "drive":
        asset = cand.get("asset") or {}
        return {"asset_id": str(asset.get("id") or cand.get("key") or ""),
                "md5": str(asset.get("content_hash") or "") or None,
                "phash": str(asset.get("phash") or asset.get("dct_phash")
                             or "") or None,
                "drive_file_id": str(asset.get("drive_file_id") or "") or None}
    return {"asset_id": str(cand.get("key") or ""),
            "md5": _local_md5(cand.get("path") or "")}


def filter_swap_candidates(gym_id, target_date, candidates, *, log=None):
    """media_swap.candidates_for seam. Flag OFF: the input list, untouched.
    Flag ON: drop every candidate whose group is unavailable for target_date
    and every candidate whose identity cannot be verified (fail closed — the
    empty result falls into the existing REASON_NO_FRESH_PHOTO path)."""
    cands = list(candidates or [])
    if not enabled():
        return cands
    say = log or (lambda msg: print(f"[media-group-guard] {msg}"))
    kept = []
    for cand in cands:
        decision = check_candidate(gym_id, target_date=target_date,
                                   **swap_candidate_identity(cand))
        if decision.allow:
            kept.append(cand)
        else:
            say(f"{gym_id}: swap candidate {cand.get('name')} held "
                f"({decision.reason})")
    return kept


def selector_candidate_allowed(gym_id, asset):
    """gym_media_selector seam (pick path + cooldown fallback). Flag OFF:
    always True. Flag ON: an asset whose group is permanently published or
    terminal is never handed out again; unverifiable identity
    (UNKNOWN_IDENTITY) and suspected-but-unconfirmed scene matches
    (SCENE_MATCH_CANDIDATE) hold closed — the selector never substitutes a
    cooldown or recycled asset to satisfy a hold. Date-scoped reservations do
    NOT block here — the selector has no target date; the claim itself happens
    at staging/publish."""
    if not enabled():
        return True
    gym_id = str(gym_id)
    asset = asset or {}
    result = classify_candidate(
        AssetRef(gym_id=gym_id, asset_id=str(asset.get("id") or "asset"),
                 md5=str(asset.get("content_hash") or "") or None,
                 phash=str(asset.get("phash") or asset.get("dct_phash")
                           or "") or None,
                 drive_file_id=str(asset.get("drive_file_id") or "") or None),
        known_groups_for(gym_id))
    if result.classification is Classification.UNKNOWN_IDENTITY:
        return False
    if result.classification is Classification.SCENE_MATCH_CANDIDATE:
        return False
    if result.classification is Classification.NEW_GROUP:
        return True
    ledger = ledger_for(gym_id)
    if ledger is None:
        return False
    return not ledger.permanently_used(gym_id, result.group_key)


def publish_hold_reason(row, gym_id, *, media_store=None):
    """Outbound re-check alongside media_reuse_policy.publish_hold_reason.
    Returns a HOLD_* reason or None. Flag OFF: always None.

    A row may publish when its visual group is available for the row's own
    post_date (same-date siblings included). A group published or reserved on
    a DIFFERENT date holds; unverifiable identity (UNKNOWN_IDENTITY) and
    unconfirmed scene matches (SCENE_MATCH_CANDIDATE) hold closed."""
    if not enabled():
        return None
    gym_id = str(gym_id or "")
    date = str((row or {}).get("post_date") or "")[:10]
    if not gym_id or not date:
        return HOLD_IDENTITY_UNKNOWN
    md5 = None
    phash = None
    asset_id = str((row or {}).get("source_media_asset_id") or "")
    if asset_id:
        try:
            from . import media_source_store
            store = media_store or media_source_store.default_store()
            assets = store.list_assets(gym_id)
            if not isinstance(assets, list):
                raise ValueError("media inventory unavailable")
            for a in assets:
                if str(a.get("id")) == asset_id:
                    md5 = str(a.get("content_hash") or "") or None
                    phash = str(a.get("phash") or a.get("dct_phash")
                                or "") or None
                    break
        except Exception:  # noqa: BLE001 - identity unverifiable: hold closed
            return HOLD_IDENTITY_UNKNOWN
    decision = check_candidate(gym_id, target_date=date, asset_id=asset_id,
                               md5=md5, phash=phash)
    return None if decision.allow else decision.reason
