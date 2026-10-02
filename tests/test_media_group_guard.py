"""Focused tests for agent/media_group_guard.py (global no-repeat media repair).

Covers the in-process GroupLedger state machine (mirroring the DRAFT RPC
semantics), the flag-OFF passthrough posture (byte-identical behavior), and
the flag-ON holds at the selector / swap / outbound seams. Everything is
in-process: no DB, no network.

Run: PYTHONPATH=. python3 -m pytest tests/test_media_group_guard.py
"""

import pytest

from agent import media_group_guard as mgg
from agent.visual_identity import AssetRef, VisualGroup, manual_group_key

GYM = "gym_one"
D1 = "2026-10-05"
D2 = "2026-10-06"


@pytest.fixture(autouse=True)
def _clean_guard_state(monkeypatch):
    monkeypatch.delenv("AGENT_MEDIA_GROUP_GUARD", raising=False)
    mgg.reset_state()
    yield
    mgg.reset_state()


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_GROUP_GUARD", "true")


def _group_with_md5(md5, gym=GYM):
    member = AssetRef(gym_id=gym, asset_id=f"asset-{md5[:8]}", md5=md5)
    return VisualGroup(gym_id=gym, group_id=f"h-{md5[:8]}", members=[member])


# ---------------------------------------------------------------------------
# GroupLedger state machine (mirrors migrations/DRAFT_media_group_usage SQL)
# ---------------------------------------------------------------------------

def test_same_date_siblings_share_one_group():
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, "g1")
    assert ledger.claim(GYM, "g1", D1, "row-ig", "ig") == "claimed"
    assert ledger.claim(GYM, "g1", D1, "row-fb", "fb") == "shared"
    assert ledger.claim(GYM, "g1", D1, "row-gbp", "gbp") == "shared"


def test_cross_date_claim_blocked_while_reserved():
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, "g1")
    assert ledger.claim(GYM, "g1", D1, "row-ig", "ig") == "claimed"
    assert ledger.claim(GYM, "g1", D2, "row-ig-2", "ig") == "held_conflict"
    assert not ledger.is_available_for(GYM, "g1", D2)
    assert ledger.is_available_for(GYM, "g1", D1)


def test_publish_is_permanent_on_every_other_date():
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, "g1")
    ledger.claim(GYM, "g1", D1, "row-ig", "ig")
    ledger.claim(GYM, "g1", D1, "row-fb", "fb")
    assert ledger.publish(GYM, "g1", D1) == 2
    assert ledger.claim(GYM, "g1", D2, "row-3", "ig") == "held_used"
    # Same-date siblings of a published group may still join it.
    assert ledger.claim(GYM, "g1", D1, "row-story", "story") == "shared"


def test_release_frees_group_only_after_last_sibling_removed():
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, "g1")
    ledger.claim(GYM, "g1", D1, "row-ig", "ig")
    ledger.claim(GYM, "g1", D1, "row-fb", "fb")
    assert ledger.release(GYM, "g1", D1, "row-ig") == 1
    assert ledger.claim(GYM, "g1", D2, "row-x", "ig") == "held_conflict"
    assert ledger.release(GYM, "g1", D1, "row-fb") == 0
    assert ledger.claim(GYM, "g1", D2, "row-x", "ig") == "claimed"


def test_published_rows_are_never_released():
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, "g1")
    ledger.claim(GYM, "g1", D1, "row-ig", "ig")
    ledger.publish(GYM, "g1", D1)
    assert ledger.release(GYM, "g1", D1, "row-ig") == 1  # published row survives
    assert ledger.claim(GYM, "g1", D2, "row-x", "ig") == "held_used"


def test_terminal_and_missing_groups_fail_closed():
    ledger = mgg.GroupLedger()
    assert ledger.claim(GYM, "ghost", D1, "r", "ig") == "missing_group"
    assert not ledger.is_available_for(GYM, "ghost", D1)
    ledger.register_group(GYM, "g1")
    assert ledger.hold(GYM, "g1", "exhausted")
    assert ledger.claim(GYM, "g1", D1, "r", "ig") == "held_terminal"
    # Terminal is one-way.
    assert not ledger.hold(GYM, "g1", "unknown")
    assert not ledger.hold(GYM, "never-registered", "unknown")


def test_is_available_for_never_mutates():
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, "g1")
    assert ledger.is_available_for(GYM, "g1", D1)
    assert ledger.is_available_for(GYM, "g1", D2)  # dry run reserved nothing


# ---------------------------------------------------------------------------
# Flag OFF: every entry point is a passthrough (byte-identical behavior).
# ---------------------------------------------------------------------------

def test_flag_off_check_candidate_allows_even_unknown_identity():
    decision = mgg.check_candidate(GYM, target_date=D1)
    assert decision.allow and decision.reason == "guard_disabled"


def test_flag_off_swap_filter_returns_candidates_untouched():
    cands = [{"source": "drive", "key": "a1",
              "asset": {"id": "a1", "content_hash": "abc"}, "name": "a"}]
    assert mgg.filter_swap_candidates(GYM, D1, cands) == cands


def test_flag_off_selector_allows_anything():
    assert mgg.selector_candidate_allowed(GYM, {"id": "a1", "content_hash": "x"})
    assert mgg.selector_candidate_allowed(GYM, {})


def test_flag_off_outbound_hold_is_none():
    assert mgg.publish_hold_reason({"post_date": D1}, GYM) is None
    assert mgg.publish_hold_reason({}, "") is None


# ---------------------------------------------------------------------------
# Flag ON: check_candidate decisions
# ---------------------------------------------------------------------------

def test_flag_on_new_group_is_allowed(flag_on):
    ledger = mgg.GroupLedger()
    group = _group_with_md5("0" * 32)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    decision = mgg.check_candidate(
        GYM, target_date=D1, md5="f" * 32,
        phash="0123456789abcdef")  # phash differs from every known group
    assert decision.allow, decision


def test_flag_on_cross_date_reserved_group_holds(flag_on):
    md5 = "1" * 32
    group = _group_with_md5(md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    ledger.claim(GYM, group.group_key, D1, "row-ig", "ig")
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    held = mgg.check_candidate(GYM, target_date=D2, md5=md5)
    assert not held.allow and held.reason == mgg.HOLD_USED_OTHER_DATE
    shared = mgg.check_candidate(GYM, target_date=D1, md5=md5)
    assert shared.allow and shared.group_key == group.group_key


def test_flag_on_published_group_is_permanent(flag_on):
    md5 = "2" * 32
    group = _group_with_md5(md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    ledger.claim(GYM, group.group_key, D1, "row-ig", "ig")
    ledger.publish(GYM, group.group_key, D1)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    held = mgg.check_candidate(GYM, target_date=D2, md5=md5)
    assert not held.allow and held.reason == mgg.HOLD_PERMANENTLY_USED


def test_flag_on_unknown_identity_fails_closed(flag_on):
    mgg.register_known_groups(GYM, [_group_with_md5("3" * 32)])
    mgg.register_ledger(GYM, mgg.GroupLedger())
    decision = mgg.check_candidate(GYM, target_date=D1)  # no hashes, no phash
    assert not decision.allow and decision.reason == mgg.HOLD_IDENTITY_UNKNOWN


def test_flag_on_matched_group_without_ledger_holds_closed(flag_on):
    md5 = "4" * 32
    mgg.register_known_groups(GYM, [_group_with_md5(md5)])
    # No ledger registered: availability cannot be proven.
    decision = mgg.check_candidate(GYM, target_date=D1, md5=md5)
    assert not decision.allow and decision.reason == mgg.HOLD_IDENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# Flag ON at the swap seam (media_swap.candidates_for)
# ---------------------------------------------------------------------------

def _drive_cand(asset_id, content_hash):
    return {"source": "drive", "kind": "photo", "key": asset_id,
            "asset": {"id": asset_id, "content_hash": content_hash},
            "last_used": "", "used_count": 0, "name": asset_id}


def test_flag_on_swap_excludes_used_groups_and_holds_unknown(flag_on):
    from agent import media_swap as msw
    used_md5 = "5" * 32
    group = _group_with_md5(used_md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    ledger.claim(GYM, group.group_key, D1, "row-ig", "ig")
    ledger.publish(GYM, group.group_key, D1)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])

    monkey_local = lambda *a, **k: []
    drives = [_drive_cand("used-asset", used_md5)]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(msw, "local_candidates", monkey_local)
    monkeypatch.setattr(msw, "drive_candidates", lambda *a, **k: list(drives))
    try:
        cands = msw.candidates_for(GYM, {"post_date": D2}, store=None, lib="",
                                   book_state={}, asset_state={})
        assert cands == []  # recycled-across-dates group excluded; nothing left

        # Flag OFF: the same pool flows through untouched.
        monkeypatch.delenv("AGENT_MEDIA_GROUP_GUARD")
        cands = msw.candidates_for(GYM, {"post_date": D2}, store=None, lib="",
                                   book_state={}, asset_state={})
        assert [c["key"] for c in cands] == ["used-asset"]
    finally:
        monkeypatch.undo()


def test_flag_on_swap_exhaustion_fallback_recycles_nothing(flag_on):
    from agent import media_swap as msw
    used_md5 = "6" * 32
    group = _group_with_md5(used_md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    ledger.claim(GYM, group.group_key, D1, "row-ig", "ig")
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(msw, "local_candidates", lambda *a, **k: [])
    monkeypatch.setattr(msw, "drive_candidates",
                        lambda *a, **k: [_drive_cand("cooling", used_md5)])
    try:
        cands = msw.candidates_for(GYM, {"post_date": D2}, store=None, lib="",
                                   book_state={}, asset_state={})
        # The cooldown-relaxation lane ran (fresh pool empty) but its only
        # candidate is a group reserved on another date: excluded, not recycled.
        assert cands == []
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Flag ON at the selector seam (gym_media_selector.cooldown_fallback)
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, assets):
        self._assets = assets

    def available(self):
        return True

    def list_assets(self, base):
        return [a for a in self._assets if a.get("gym_id") == base]


def _usable_asset(asset_id, content_hash, gym=GYM, **over):
    asset = {
        "id": asset_id, "gym_id": gym, "kind": "photo",
        "eligible": True, "excluded_by_coach": False,
        "review_status": "approved", "reviewed_by": "echo",
        "reviewed_at": "2026-09-01T00:00:00+00:00",
        "content_hash": content_hash, "review_content_hash": content_hash,
        "people_detected": False,
        "moderation_status": "clean",
        "moderation_json": {
            "verdict": "clean", "provider": "test",
            "content_hash": content_hash, "asset_id": asset_id,
            "gym_id": gym, "people_detected": False,
            "observed_at": "2026-09-01T00:00:00+00:00",
        },
    }
    asset.update(over)
    return asset


def test_flag_on_selector_fallback_excludes_published_groups(flag_on):
    from agent import gym_media_selector as sel
    published_md5 = "7" * 32
    group = _group_with_md5(published_md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    ledger.claim(GYM, group.group_key, D1, "row-ig", "ig")
    ledger.publish(GYM, group.group_key, D1)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])

    store = _FakeStore([
        _usable_asset("published-asset", published_md5),
        _usable_asset("unverifiable-asset", "8" * 32),  # no phash evidence
    ])
    # Both hold: the published group is permanent; the other asset cannot prove
    # a fresh visual identity without phash material (fail closed).
    assert sel.cooldown_fallback(GYM, store=store) == []

    # Flag OFF: both flow through exactly as before.
    mgg.reset_state()
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("AGENT_MEDIA_GROUP_GUARD", raising=False)
        got = sel.cooldown_fallback(GYM, store=store)
    assert {a["id"] for a in got} == {"published-asset", "unverifiable-asset"}


def test_flag_on_selector_pickable_excludes_published_groups(flag_on):
    from agent import gym_media_selector as sel
    published_md5 = "9" * 32
    group = _group_with_md5(published_md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    ledger.claim(GYM, group.group_key, D1, "row-ig", "ig")
    ledger.publish(GYM, group.group_key, D1)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    store = _FakeStore([_usable_asset("published-asset", published_md5)])
    assert sel.pickable(GYM, store=store) == []


# ---------------------------------------------------------------------------
# Flag ON at the outbound seams
# ---------------------------------------------------------------------------

class _MediaStore:
    def __init__(self, assets):
        self._assets = assets

    def list_assets(self, gym_id):
        return list(self._assets)


def test_flag_on_outbound_hold_for_group_published_on_other_date(flag_on):
    md5 = "a" * 32
    group = _group_with_md5(md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    ledger.claim(GYM, group.group_key, D1, "row-ig", "ig")
    ledger.publish(GYM, group.group_key, D1)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    row = {"post_date": D2, "source_media_asset_id": "asset-1"}
    store = _MediaStore([{"id": "asset-1", "gym_id": GYM, "content_hash": md5}])
    reason = mgg.publish_hold_reason(row, GYM, media_store=store)
    assert reason == mgg.HOLD_PERMANENTLY_USED
    # Same date as the published siblings: allowed through.
    assert mgg.publish_hold_reason({**row, "post_date": D1}, GYM,
                                   media_store=store) is None


def test_flag_on_outbound_unknown_identity_holds_closed(flag_on):
    mgg.register_ledger(GYM, mgg.GroupLedger())
    mgg.register_known_groups(GYM, [])
    # No asset id, no hashes: identity unverifiable.
    assert mgg.publish_hold_reason({"post_date": D1}, GYM) == \
        mgg.HOLD_IDENTITY_UNKNOWN
    # No post_date at all: also closed.
    assert mgg.publish_hold_reason({}, GYM) == mgg.HOLD_IDENTITY_UNKNOWN


def test_flag_on_gbp_worker_group_hold_blocks_live_send(flag_on, monkeypatch):
    from agent import gbp_worker
    md5 = "b" * 32
    group = _group_with_md5(md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    ledger.claim(GYM, group.group_key, D1, "row-ig", "ig")
    ledger.publish(GYM, group.group_key, D1)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])

    monkeypatch.setattr("agent.publish_billing_gate.publishing_blocked",
                        lambda gym: False)
    row = {
        "gym_id": GYM, "post_date": D2, "source_media_asset_id": "asset-1",
        "caption": "Big community energy on the floor this week in Norwood with the whole crew",
        "image_url": "https://example.test/photo.jpg",
        "gbp_topic_type": "STANDARD",
    }
    connection = {"zernio_account_id": "za_1", "gbp_location_id": "loc_1"}
    store = _MediaStore([{"id": "asset-1", "gym_id": GYM, "content_hash": md5}])

    class _Client:
        def create_post_raw(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("network send attempted under a media hold")

    result = gbp_worker.publish_gbp_row(row, connection, client=_Client(),
                                        draft=False, media_store=store)
    assert result["ok"] is False
    assert result["reject_reason"] == mgg.HOLD_PERMANENTLY_USED
    assert result["held"] == "media_group"
    assert result["status"] == "approved"  # held visibly, never failed/sent


def test_flag_off_gbp_worker_group_hold_is_inert(monkeypatch):
    from agent import gbp_worker
    row = {"gym_id": GYM, "post_date": D2}
    assert gbp_worker._media_group_hold(row) is None


# ---------------------------------------------------------------------------
# Tiered classification: SCENE_MATCH_CANDIDATE (hamming 7-30) holds visibly.
# Synthetic band fixture only — hamming distances are constructed, NOT a claim
# about the live Swift River evidence.
# ---------------------------------------------------------------------------

_MEMBER_PHASH = "0" * 16          # all-zero 64-bit pHash
_SCENE_PHASH = "fffff" + "0" * 11  # hamming 20: inside the 7-30 scene band
_FAR_PHASH = "f" * 16             # hamming 64: beyond the scene band


def _phash_group(phash=_MEMBER_PHASH, gym=GYM):
    member = AssetRef(gym_id=gym, asset_id=f"asset-{phash[:6]}", phash=phash)
    return VisualGroup(gym_id=gym, group_id=f"h-{phash[:6]}",
                       members=[member])


def test_scene_band_candidate_holds_unconfirmed_never_allowed(flag_on):
    mgg.register_ledger(GYM, mgg.GroupLedger())
    mgg.register_known_groups(GYM, [_phash_group()])
    decision = mgg.check_candidate(GYM, target_date=D1, phash=_SCENE_PHASH)
    assert not decision.allow
    assert decision.reason == mgg.HOLD_SCENE_UNCONFIRMED
    # The suspect key is surfaced for corroboration, not membership.
    assert decision.group_key == _phash_group().group_key


def test_beyond_scene_band_is_a_genuinely_new_group(flag_on):
    mgg.register_ledger(GYM, mgg.GroupLedger())
    mgg.register_known_groups(GYM, [_phash_group()])
    decision = mgg.check_candidate(GYM, target_date=D1, phash=_FAR_PHASH)
    assert decision.allow
    assert decision.classification == "NEW_GROUP"


def test_scene_candidate_holds_at_the_swap_seam(flag_on):
    mgg.register_ledger(GYM, mgg.GroupLedger())
    mgg.register_known_groups(GYM, [_phash_group()])
    cand = {"source": "drive", "kind": "photo", "key": "a-scene",
            "asset": {"id": "a-scene", "content_hash": "c" * 32,
                      "phash": _SCENE_PHASH},
            "last_used": "", "used_count": 0, "name": "a-scene"}
    assert mgg.filter_swap_candidates(GYM, D1, [cand]) == []


def test_scene_candidate_holds_at_the_selector_seam(flag_on):
    from agent import gym_media_selector as sel
    mgg.register_ledger(GYM, mgg.GroupLedger())
    mgg.register_known_groups(GYM, [_phash_group()])
    store = _FakeStore([_usable_asset("a-scene", "d" * 32,
                                      phash=_SCENE_PHASH)])
    assert sel.pickable(GYM, store=store) == []
    assert sel.cooldown_fallback(GYM, store=store) == []


def test_scene_candidate_holds_at_the_outbound_seam(flag_on):
    mgg.register_ledger(GYM, mgg.GroupLedger())
    mgg.register_known_groups(GYM, [_phash_group()])
    row = {"post_date": D1, "source_media_asset_id": "a-scene"}
    store = _MediaStore([{"id": "a-scene", "gym_id": GYM,
                          "content_hash": "e" * 32, "phash": _SCENE_PHASH}])
    assert mgg.publish_hold_reason(row, GYM, media_store=store) == \
        mgg.HOLD_SCENE_UNCONFIRMED


# ---------------------------------------------------------------------------
# Manual groups: operator-assigned keys claim like any other group.
# ---------------------------------------------------------------------------

def test_register_manual_group_wraps_assign_and_keeps_audit(flag_on):
    ledger = mgg.GroupLedger()
    mgg.register_ledger(GYM, ledger)
    asset = AssetRef(gym_id=GYM, asset_id="sr-oct7", md5="1a" * 16)
    key = mgg.register_manual_group(
        GYM, [asset], "swift-river-scene",
        note="operator: same neighboring scene, 2026-10-02")
    assert key == manual_group_key(GYM, "swift-river-scene")
    assert key.endswith("::manual:swift-river-scene")
    assert ledger.group_status(GYM, key) == "active"
    groups = mgg.known_groups_for(GYM)
    assert len(groups) == 1 and groups[0].group_key == key
    assert any("operator" in note for note in groups[0].audit)
    # A byte-identical candidate now resolves to the manual group.
    decision = mgg.check_candidate(GYM, target_date=D1, md5="1a" * 16)
    assert decision.allow and decision.group_key == key


def test_manual_group_claims_follow_normal_semantics(flag_on):
    key = mgg.register_manual_group(GYM, [], "scene-a")
    ledger = mgg.GroupLedger()
    mgg.register_ledger(GYM, ledger)
    ledger.register_group(GYM, key)
    # Same-date siblings share; a third date conflicts while reserved.
    assert ledger.claim(GYM, key, D1, "row-ig", "ig") == "claimed"
    assert ledger.claim(GYM, key, D1, "row-fb", "fb") == "shared"
    assert ledger.claim(GYM, key, D2, "row-x", "ig") == "held_conflict"
    # Publish is permanent on every other date; terminal holds never recycle.
    ledger.publish(GYM, key, D1)
    assert ledger.claim(GYM, key, D2, "row-x", "ig") == "held_used"
    assert ledger.hold(GYM, key, "retired")
    assert ledger.claim(GYM, key, D1, "row-y", "ig") == "held_terminal"


# ---------------------------------------------------------------------------
# Swift-River-shaped scenario (SHAPE ONLY — synthetic ids, no live data):
# four pending dates, NULL identity, then operator manual grouping.
# ---------------------------------------------------------------------------

SWIFT_DATES = ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"]


def test_null_identity_pending_rows_all_hold_visibly(flag_on):
    """The incident shape: 16 rows across Oct 7-10 with source_media_asset_id
    NULL and nothing published. Every date fails closed — no substitution, no
    recycling, no silent fresh group."""
    mgg.register_ledger(GYM, mgg.GroupLedger())
    mgg.register_known_groups(GYM, [])
    for date in SWIFT_DATES:
        for channel in ("ig", "fb", "story", "gbp"):
            row = {"post_date": date, "source_media_asset_id": None,
                   "image_url": f"https://example.test/{channel}.jpg"}
            assert mgg.publish_hold_reason(row, GYM) == \
                mgg.HOLD_IDENTITY_UNKNOWN


def test_after_manual_grouping_dates_block_each_other_but_siblings_share(flag_on):
    """Operator asserts the four dates' media is one scene: one manual group.
    The group still cannot be reserved on four different dates — the rows must
    be re-planned to distinct groups or stay held — while same-date siblings
    share it."""
    ledger = mgg.GroupLedger()
    mgg.register_ledger(GYM, ledger)
    assets = [AssetRef(gym_id=GYM, asset_id=f"sr-{d}", drive_file_id=f"drv-{d}")
              for d in SWIFT_DATES]
    key = mgg.register_manual_group(
        GYM, assets, "swift-river-scene",
        note="operator scene assertion pending re-plan")
    # One claim per date: only the first lands; the other three hold.
    assert ledger.claim(GYM, key, SWIFT_DATES[0], "row-oct7-ig", "ig") == "claimed"
    for date in SWIFT_DATES[1:]:
        assert ledger.claim(GYM, key, date, f"row-{date}", "ig") == \
            "held_conflict"
    # Same-date siblings of the claimed slot share the group.
    assert ledger.claim(GYM, key, SWIFT_DATES[0], "row-oct7-fb", "fb") == "shared"
    # Publish makes even the first date's group permanent elsewhere.
    ledger.publish(GYM, key, SWIFT_DATES[0])
    for date in SWIFT_DATES[1:]:
        assert ledger.claim(GYM, key, date, f"row-{date}-b", "ig") == "held_used"
    # Nothing recycles: release of a published row is a no-op.
    assert ledger.release(GYM, key, SWIFT_DATES[0], "row-oct7-ig") == 2


# ---------------------------------------------------------------------------
# Immutable group keys: reservations survive membership growth.
# ---------------------------------------------------------------------------

def test_ledger_reservation_survives_membership_growth(flag_on):
    """Group keys are frozen at creation. Appending a member whose stable
    identity is lexicographically SMALLER than the founder's must not drift
    the key or disturb any reservation on it."""
    founder = AssetRef(gym_id=GYM, asset_id="founder", md5="f" * 32)
    group = VisualGroup(gym_id=GYM, group_id="h-founder", members=[founder])
    key_before = group.group_key
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, key_before)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    assert ledger.claim(GYM, key_before, D1, "row-ig", "ig") == "claimed"

    # A smaller-hash member joins the group (e.g. backfill discovers an
    # earlier byte-identical upload). The key must be unchanged.
    group.members.append(AssetRef(gym_id=GYM, asset_id="earlier",
                                  md5="0" * 32))
    assert group.group_key == key_before
    assert ledger.claim(GYM, key_before, D2, "row-x", "ig") == "held_conflict"
    assert ledger.is_available_for(GYM, key_before, D1)
    assert not ledger.is_available_for(GYM, key_before, D2)
    # And classification still routes both members to the same frozen key.
    assert mgg.check_candidate(GYM, target_date=D1, md5="0" * 32).allow


# ---------------------------------------------------------------------------
# Scene-hold resolution: confirm merges (rules then apply), reject frees.
# ---------------------------------------------------------------------------

def test_confirm_scene_match_merges_and_normal_rules_apply(flag_on):
    group = _phash_group()
    ledger = mgg.GroupLedger()
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    key = group.group_key
    ledger.register_group(GYM, key)

    asset = AssetRef(gym_id=GYM, asset_id="held-candidate",
                     phash=_SCENE_PHASH)
    held = mgg.check_candidate(GYM, target_date=D1, phash=_SCENE_PHASH)
    assert not held.allow and held.reason == mgg.HOLD_SCENE_UNCONFIRMED

    resolved = mgg.register_scene_confirmation(
        GYM, asset, key, note="operator: same scene, portal compare")
    assert resolved == key
    # The asset is now a member: same-date share is allowed ...
    ledger.claim(GYM, key, D1, "row-ig", "ig")
    assert mgg.check_candidate(GYM, target_date=D1,
                               phash=_SCENE_PHASH).allow
    # ... cross-date is blocked while reserved ...
    conflict = mgg.check_candidate(GYM, target_date=D2, phash=_SCENE_PHASH)
    assert not conflict.allow and conflict.reason == mgg.HOLD_USED_OTHER_DATE
    # ... and publish makes the merged group permanent on every other date.
    ledger.publish(GYM, key, D1)
    used = mgg.check_candidate(GYM, target_date=D2, phash=_SCENE_PHASH)
    assert not used.allow and used.reason == mgg.HOLD_PERMANENTLY_USED
    # The confirmation evidence is audited on the group.
    assert any("hamming" in note for note in group.audit)


def test_confirm_scene_match_releases_the_hold_at_a_seam(flag_on):
    group = _phash_group()
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    row = {"post_date": D1, "source_media_asset_id": "a-scene"}
    store = _MediaStore([{"id": "a-scene", "gym_id": GYM,
                          "content_hash": "e" * 32, "phash": _SCENE_PHASH}])
    assert mgg.publish_hold_reason(row, GYM, media_store=store) == \
        mgg.HOLD_SCENE_UNCONFIRMED
    mgg.register_scene_confirmation(
        GYM, AssetRef(gym_id=GYM, asset_id="a-scene", phash=_SCENE_PHASH),
        group.group_key, note="operator confirmed")
    assert mgg.publish_hold_reason(row, GYM, media_store=store) is None


def test_reject_scene_match_frees_the_asset_as_new_group(flag_on):
    group = _phash_group()
    ledger = mgg.GroupLedger()
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    key = group.group_key

    asset = AssetRef(gym_id=GYM, asset_id="held-candidate",
                     phash=_SCENE_PHASH)
    assert not mgg.check_candidate(GYM, target_date=D1,
                                   phash=_SCENE_PHASH).allow
    mgg.register_scene_rejection(GYM, asset, key,
                                 note="operator: different scene")
    freed = mgg.check_candidate(GYM, target_date=D1, asset_id="held-candidate",
                                phash=_SCENE_PHASH)
    assert freed.allow and freed.classification == "NEW_GROUP"
    # The rejection is recorded on the suspect group, keyed by stable identity.
    assert asset.stable_identity() in group.rejections
    assert any("rejected" in note for note in group.audit)


def test_reject_scene_match_frees_the_asset_at_the_selector_seam(flag_on):
    from agent import gym_media_selector as sel
    group = _phash_group()
    mgg.register_ledger(GYM, mgg.GroupLedger())
    mgg.register_known_groups(GYM, [group])
    asset = _usable_asset("a-scene", "d" * 32, phash=_SCENE_PHASH)
    store = _FakeStore([asset])
    assert sel.pickable(GYM, store=store) == []
    mgg.register_scene_rejection(
        GYM, AssetRef(gym_id=GYM, asset_id="a-scene", md5="d" * 32,
                      phash=_SCENE_PHASH),
        group.group_key, note="operator: different scene")
    assert [a["id"] for a in sel.pickable(GYM, store=store)] == ["a-scene"]


def test_byte_equality_overrides_a_rejection(flag_on):
    md5 = "9a" * 16
    group = _group_with_md5(md5)
    ledger = mgg.GroupLedger()
    ledger.register_group(GYM, group.group_key)
    mgg.register_ledger(GYM, ledger)
    mgg.register_known_groups(GYM, [group])
    asset = AssetRef(gym_id=GYM, asset_id="byte-twin", md5=md5)
    mgg.register_scene_rejection(GYM, asset, group.group_key)
    # Rejection records judgment on pHash evidence; identical bytes still
    # prove the same visual and resolve SAME_GROUP.
    decision = mgg.check_candidate(GYM, target_date=D1, md5=md5)
    assert decision.allow and decision.classification == "SAME_GROUP"
