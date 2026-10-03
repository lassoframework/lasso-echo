"""
DRAFT historical media clearance — explicit per-asset review gate, offline.

With AGENT_HISTORICAL_MEDIA_CLEARANCE enabled, an asset is pickable ONLY with
an explicit 'cleared' receipt matching the exact identity triple (gym_id,
asset_id, CURRENT content_hash) — never filename, URL, or pHash. known_used is
permanent; held and uncleared are excluded; a hash change re-blocks; any read
failure or ambiguous flag fails closed. Flag OFF = byte-for-byte legacy.
"""

import hashlib
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_selector as gms  # noqa: E402
from agent import historical_media_clearance as hmc  # noqa: E402
from agent.media_source_store import MediaStoreError, SupabaseMediaStore  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402

GYM = "gymx"
NOW_DT = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def _hash(seed):
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _photo(asset_id):
    return make_asset(asset_id, gym_id=GYM, kind="photo",
                      content_hash=_hash(asset_id))


def _receipt(asset, decision="cleared", reviewer="blake", revoked=False,
             evidence=True, content_hash=None, gym_id=None, asset_id=None):
    gid = gym_id if gym_id is not None else asset["gym_id"]
    aid = asset_id if asset_id is not None else asset["id"]
    chash = content_hash if content_hash is not None else asset["content_hash"]
    return {"gym_id": gid, "asset_id": aid, "source_id": "src1",
            "content_hash": chash, "decision": decision, "reviewer": reviewer,
            "evidence": ({"gym_id": gid, "asset_id": aid, "content_hash": chash}
                         if evidence else None),
            "recorded_at": "2026-10-01T00:00:00Z",
            "revoked_at": "2026-10-02T00:00:00Z" if revoked else None,
            "revoked_by": "blake" if revoked else None,
            "revocation_reason": "r" if revoked else None}


class ClearanceFakeStore(FakeMediaStore):
    """FakeMediaStore plus the DRAFT clearance read (raising = unapplied migration)."""

    def __init__(self, *a, clearances=None, clearance_down=False, **kw):
        super().__init__(*a, **kw)
        self._clearances = list(clearances or [])
        self._clearance_down = clearance_down

    def list_historical_clearances(self, gym_id):
        if not gym_id:
            raise MediaStoreError(400, "gym_id required")
        if self._clearance_down:
            raise MediaStoreError(404, "media_historical_clearance unapplied")
        return [dict(r) for r in self._clearances if r.get("gym_id") == gym_id]


def _flag_on(monkeypatch):
    monkeypatch.setenv(hmc.HISTORICAL_CLEARANCE_FLAG_ENV, "true")


def _flag_off(monkeypatch):
    monkeypatch.delenv(hmc.HISTORICAL_CLEARANCE_FLAG_ENV, raising=False)


# ---- flag tri-state -----------------------------------------------------------

def test_flag_default_off(monkeypatch):
    _flag_off(monkeypatch)
    assert hmc.historical_clearance_flag() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_flag_on_values(monkeypatch, value):
    monkeypatch.setenv(hmc.HISTORICAL_CLEARANCE_FLAG_ENV, value)
    assert hmc.historical_clearance_flag() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_flag_off_values(monkeypatch, value):
    monkeypatch.setenv(hmc.HISTORICAL_CLEARANCE_FLAG_ENV, value)
    assert hmc.historical_clearance_flag() is False


@pytest.mark.parametrize("value", ["maybe", "sometimes", "2", "enabled"])
def test_flag_ambiguous_is_fail_closed_sentinel(monkeypatch, value):
    monkeypatch.setenv(hmc.HISTORICAL_CLEARANCE_FLAG_ENV, value)
    assert hmc.historical_clearance_flag() is None


# ---- clearance_status predicate ------------------------------------------------

def test_cleared_requires_exact_hash_unrevoked_valid_evidence():
    asset = _photo("a1")
    assert hmc.clearance_status(asset, [_receipt(asset)]) == "cleared"
    # revoked receipt never clears
    assert hmc.clearance_status(asset, [_receipt(asset, revoked=True)]) == "uncleared"
    # empty reviewer never clears
    assert hmc.clearance_status(asset, [_receipt(asset, reviewer=" ")]) == "uncleared"
    # missing / unbound evidence never clears
    assert hmc.clearance_status(asset, [_receipt(asset, evidence=False)]) == "uncleared"
    bad = _receipt(asset)
    bad["evidence"] = {"gym_id": "other", "asset_id": "a1",
                       "content_hash": asset["content_hash"]}
    assert hmc.clearance_status(asset, [bad]) == "uncleared"
    bad2 = _receipt(asset)
    bad2["evidence"] = {"gym_id": GYM, "asset_id": "a1", "content_hash": _hash("stale")}
    assert hmc.clearance_status(asset, [bad2]) == "uncleared"


def test_hash_change_after_clearance_reblocks():
    """Stale-hash clearance NEVER clears: identity includes the CURRENT hash."""
    asset = _photo("a1")
    rows = [_receipt(asset, content_hash=_hash("old-bytes"))]
    assert hmc.clearance_status(asset, rows) == "uncleared"


def test_known_used_is_permanent_regardless_of_hash_and_revocation():
    asset = _photo("a1")
    rows = [_receipt(asset, decision="known_used", content_hash=_hash("ancient"),
                     revoked=True)]
    assert hmc.clearance_status(asset, rows) == "known_used"
    # known_used wins even alongside a valid cleared receipt
    rows.append(_receipt(asset))
    assert hmc.clearance_status(asset, rows) == "known_used"


def test_held_and_uncleared_statuses():
    asset = _photo("a1")
    assert hmc.clearance_status(asset, []) == "uncleared"
    assert hmc.clearance_status(asset, [_receipt(asset, decision="held")]) == "held"
    # held receipt for a stale hash does not hold the current bytes
    assert hmc.clearance_status(
        asset, [_receipt(asset, decision="held", content_hash=_hash("old"))]
    ) == "uncleared"
    # receipts for another gym/asset never match
    assert hmc.clearance_status(asset, [_receipt(asset, gym_id="other")]) == "uncleared"
    assert hmc.clearance_status(asset, [_receipt(asset, asset_id="zzz")]) == "uncleared"


# ---- pickable / cooldown_fallback wiring ---------------------------------------

def test_flag_off_is_byte_for_byte_legacy(monkeypatch):
    _flag_off(monkeypatch)
    store = ClearanceFakeStore(assets=[_photo("p1")], clearance_down=True)
    picks = gms.pickable(GYM, store=store, now=NOW_DT)
    assert [a["id"] for a in picks] == ["p1"]
    assert [a["id"] for a in gms.cooldown_fallback(GYM, store=store)] == ["p1"]


def test_flag_on_requires_cleared_receipt(monkeypatch):
    _flag_on(monkeypatch)
    cleared, held, plain, known = (_photo("cleared"), _photo("held"),
                                   _photo("plain"), _photo("known"))
    rows = [_receipt(cleared), _receipt(held, decision="held"),
            _receipt(known, decision="known_used")]
    store = ClearanceFakeStore(assets=[cleared, held, plain, known],
                               clearances=rows)
    picks = gms.pickable(GYM, store=store, now=NOW_DT)
    assert [a["id"] for a in picks] == ["cleared"]
    assert [a["id"] for a in gms.cooldown_fallback(GYM, store=store)] == ["cleared"]


def test_flag_on_clearance_read_failure_fails_closed(monkeypatch):
    _flag_on(monkeypatch)
    store = ClearanceFakeStore(assets=[_photo("p1")], clearance_down=True)
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    with pytest.raises(hmc.HistoricalClearanceUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, strict_claims=True)
    assert gms.cooldown_fallback(GYM, store=store) == []


def test_ambiguous_flag_fails_closed(monkeypatch):
    monkeypatch.setenv(hmc.HISTORICAL_CLEARANCE_FLAG_ENV, "sometimes")
    store = ClearanceFakeStore(assets=[_photo("p1")],
                               clearances=[_receipt(_photo("p1"))])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    with pytest.raises(hmc.HistoricalClearanceUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, strict_claims=True)
    assert gms.cooldown_fallback(GYM, store=store) == []


def test_flag_on_stale_hash_clearance_excludes(monkeypatch):
    """An asset cleared before a Drive content swap is not cleared now."""
    _flag_on(monkeypatch)
    asset = _photo("p1")
    store = ClearanceFakeStore(
        assets=[asset],
        clearances=[_receipt(asset, content_hash=_hash("previous-bytes"))])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []


# ---- SupabaseMediaStore methods (fake http) -------------------------------------

class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        return self._body


class _FakeHttp:
    def __init__(self, get_rows=None, get_status=200, rpc_body=None, rpc_status=200):
        self.get_rows = get_rows or []
        self.get_status = get_status
        self.rpc_body = rpc_body
        self.rpc_status = rpc_status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {})))
        return _Resp(self.get_status, list(self.get_rows), text="get failed")

    def post(self, url, json=None, headers=None, timeout=None, params=None):
        self.calls.append(("POST", url, dict(json or {})))
        return _Resp(self.rpc_status, self.rpc_body, text="rpc refused")


def _store(http):
    return SupabaseMediaStore(url="http://store.test", service_key="k", http=http)


def test_list_historical_clearances_requires_gym_id():
    with pytest.raises(MediaStoreError):
        _store(_FakeHttp()).list_historical_clearances("")


def test_list_historical_clearances_scopes_to_gym():
    http = _FakeHttp(get_rows=[{"gym_id": GYM, "asset_id": "a1"}])
    rows = _store(http).list_historical_clearances(GYM)
    assert rows == [{"gym_id": GYM, "asset_id": "a1"}]
    _, url, params = http.calls[0]
    assert "media_historical_clearance" in url
    assert params["gym_id"] == f"eq.{GYM}"


def test_list_historical_clearances_error_raises_never_silent_empty():
    http = _FakeHttp(get_status=404)
    with pytest.raises(MediaStoreError):
        _store(http).list_historical_clearances(GYM)


def test_record_historical_clearance_rpc_payload_and_result():
    receipt = {"gym_id": GYM, "asset_id": "a1", "decision": "cleared"}
    http = _FakeHttp(rpc_body=receipt)
    asset = _photo("a1")
    out = _store(http).record_historical_clearance(
        GYM, "a1", asset["content_hash"], "cleared", "blake",
        {"gym_id": GYM, "asset_id": "a1", "content_hash": asset["content_hash"]})
    assert out == receipt
    _, url, payload = http.calls[0]
    assert url.endswith("/rpc/record_historical_media_clearance")
    assert payload["p_gym_id"] == GYM and payload["p_asset_id"] == "a1"
    assert payload["p_decision"] == "cleared" and payload["p_reviewer"] == "blake"


def test_record_historical_clearance_refusal_raises():
    with pytest.raises(MediaStoreError):
        _store(_FakeHttp(rpc_status=404)).record_historical_clearance(
            GYM, "a1", "h", "cleared", "blake", {})
    with pytest.raises(MediaStoreError):
        _store(_FakeHttp(rpc_body={"ok": False})).record_historical_clearance(
            GYM, "a1", "h", "cleared", "blake", {})


def test_revoke_historical_clearance_rpc_and_refusal():
    http = _FakeHttp(rpc_body=True)
    assert _store(http).revoke_historical_clearance(GYM, "a1", "h", "blake", "dup")
    _, url, payload = http.calls[0]
    assert url.endswith("/rpc/revoke_historical_media_clearance")
    assert payload["p_expected_content_hash"] == "h"
    with pytest.raises(MediaStoreError):
        _store(_FakeHttp(rpc_body=False)).revoke_historical_clearance(
            GYM, "a1", "h", "blake", "known_used is permanent")
    with pytest.raises(MediaStoreError):
        _store(_FakeHttp(rpc_status=500)).revoke_historical_clearance(
            GYM, "a1", "h", "blake", "r")


# ---- activation watermark (per-gym cutoff) -------------------------------------

CUTOFF = "2026-10-01T00:00:00Z"


def _set_cutoff(monkeypatch, value=CUTOFF):
    monkeypatch.setenv(hmc._cutoff_env_name(GYM), value)


def test_cutoff_env_name_sanitizes_gym_id():
    assert hmc._cutoff_env_name("gymx") == \
        "AGENT_HISTORICAL_MEDIA_CLEARANCE_CUTOFF_GYMX"
    assert hmc._cutoff_env_name("my-gym.1") == \
        "AGENT_HISTORICAL_MEDIA_CLEARANCE_CUTOFF_MY_GYM_1"
    assert hmc._cutoff_env_name("") == ""


def test_cutoff_unset_or_unparseable_returns_none(monkeypatch):
    monkeypatch.delenv(hmc._cutoff_env_name(GYM), raising=False)
    assert hmc.clearance_cutoff(GYM) is None
    _set_cutoff(monkeypatch, "not-a-timestamp")
    assert hmc.clearance_cutoff(GYM) is None
    _set_cutoff(monkeypatch, "   ")
    assert hmc.clearance_cutoff(GYM) is None


def test_cutoff_parses_iso8601(monkeypatch):
    _set_cutoff(monkeypatch)
    assert hmc.clearance_cutoff(GYM) == datetime(2026, 10, 1,
                                                 tzinfo=timezone.utc)
    _set_cutoff(monkeypatch, "2026-10-01T02:30:00+02:30")
    assert hmc.clearance_cutoff(GYM) == datetime(2026, 10, 1,
                                                 tzinfo=timezone.utc)


def test_flag_on_without_cutoff_gates_every_asset(monkeypatch):
    """No configured per-gym cutoff = fail closed: every asset is historical."""
    _flag_on(monkeypatch)
    monkeypatch.delenv(hmc._cutoff_env_name(GYM), raising=False)
    new = _photo("new")
    new["first_indexed_at"] = "2026-10-03T00:00:00Z"
    store = ClearanceFakeStore(assets=[new])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    assert gms.cooldown_fallback(GYM, store=store) == []


def test_asset_after_cutoff_is_exempt_from_clearance(monkeypatch):
    """Newly indexed approved photos are NOT starved by the historical gate:
    an asset first seen at/after the gym cutoff is pickable with no receipt
    (normal review + global ledger still apply)."""
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    new = _photo("new")
    new["first_indexed_at"] = "2026-10-02T12:00:00Z"  # after cutoff
    edge = _photo("edge")
    edge["first_indexed_at"] = "2026-10-01T00:00:00Z"  # exactly at cutoff
    old = _photo("old")
    old["first_indexed_at"] = "2026-09-15T00:00:00Z"  # before cutoff
    store = ClearanceFakeStore(assets=[new, edge, old])
    assert sorted(a["id"] for a in gms.pickable(GYM, store=store,
                                                now=NOW_DT)) == ["edge", "new"]
    assert sorted(a["id"] for a in gms.cooldown_fallback(
        GYM, store=store)) == ["edge", "new"]


def test_old_asset_after_cutoff_still_gated_until_cleared(monkeypatch):
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    old = _photo("old")
    old["first_indexed_at"] = "2026-09-15T00:00:00Z"
    store = ClearanceFakeStore(assets=[old])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    store2 = ClearanceFakeStore(assets=[old], clearances=[_receipt(old)])
    assert [a["id"] for a in gms.pickable(GYM, store=store2,
                                          now=NOW_DT)] == ["old"]


def test_reindexed_old_asset_is_not_treated_as_new(monkeypatch):
    """indexed_at is bumped on every re-sync PATCH, so it can NEVER launder an
    old asset into 'new': first_indexed_at governs, and this asset's old
    first_indexed_at keeps it gated even though indexed_at is after cutoff."""
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    old = _photo("reindexed")
    old["first_indexed_at"] = "2026-08-27T00:00:00Z"   # true first-seen
    old["indexed_at"] = "2026-10-03T09:00:00Z"         # bumped by re-sync
    store = ClearanceFakeStore(assets=[old])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []


def test_unknown_timestamp_fails_closed(monkeypatch):
    """Missing or unparseable timestamps are treated as historical, never as
    new: the asset stays gated until explicitly cleared."""
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    missing = _photo("missing")
    missing.pop("first_indexed_at", None)
    missing.pop("indexed_at", None)
    garbage = _photo("garbage")
    garbage["first_indexed_at"] = "whenever"
    garbage["indexed_at"] = "2026-10-03T00:00:00Z"  # unusable while garbage wins
    store = ClearanceFakeStore(assets=[missing, garbage])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    assert gms.cooldown_fallback(GYM, store=store) == []


def test_unparseable_cutoff_fails_closed(monkeypatch):
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch, "next tuesday-ish")
    new = _photo("new")
    new["first_indexed_at"] = "2026-10-02T12:00:00Z"
    store = ClearanceFakeStore(assets=[new])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []


# ---- versioned append-only receipts ---------------------------------------------

def _v_receipt(asset, version, **kw):
    r = _receipt(asset, **kw)
    r["version"] = version
    return r


def test_corrected_version_after_revocation_clears():
    """The whole point of versioned receipts: v1 cleared against OLD bytes is
    revoked; v2 cleared against the CURRENT hash clears the asset. The revoked
    v1 remains as immutable history and does not block the correction."""
    asset = _photo("a1")
    rows = [_v_receipt(asset, 1, content_hash=_hash("old-bytes"), revoked=True),
            _v_receipt(asset, 2)]
    assert hmc.clearance_status(asset, rows) == "cleared"


def test_revoked_v1_without_correction_stays_uncleared():
    asset = _photo("a1")
    rows = [_v_receipt(asset, 1, revoked=True)]
    assert hmc.clearance_status(asset, rows) == "uncleared"


def test_known_used_version_permanently_blocks_later_cleared_version():
    """known_used is permanent: even if a later 'cleared' version exists in
    the rows, the asset is known_used forever."""
    asset = _photo("a1")
    rows = [_v_receipt(asset, 1, decision="known_used",
                       content_hash=_hash("ancient")),
            _v_receipt(asset, 2)]
    assert hmc.clearance_status(asset, rows) == "known_used"


def test_corrected_cleared_version_makes_old_asset_pickable(monkeypatch):
    """End to end: an old (pre-cutoff) asset whose v1 receipt was revoked is
    pickable once a v2 receipt clears the current hash."""
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    old = _photo("old")
    old["first_indexed_at"] = "2026-09-15T00:00:00Z"
    rows = [_v_receipt(old, 1, content_hash=_hash("stale"), revoked=True),
            _v_receipt(old, 2)]
    store = ClearanceFakeStore(assets=[old], clearances=rows)
    assert [a["id"] for a in gms.pickable(GYM, store=store,
                                          now=NOW_DT)] == ["old"]


# ---- 2026-10-03 independent-review repairs --------------------------------------

def test_reuse_cooldown_distinct_from_historical_cutoff(monkeypatch):
    """REGRESSION: the historical-clearance cutoff must NOT overwrite the
    90-day reuse cooldown clock (they were one shared `cutoff` variable, so
    enabling clearance silently replaced REUSE_COOLDOWN_DAYS inside
    pickable()). Guard both levels: distinct locals in the source, and normal
    selection unchanged under an extreme clearance cutoff."""
    src = open(gms.__file__, encoding="utf-8").read()
    assert "reuse_cutoff = now - timedelta(days=REUSE_COOLDOWN_DAYS)" in src
    assert "if used_at > reuse_cutoff:" in src
    assert "historical_cutoff = clearance_cutoff(base)" in src
    assert "\n    cutoff = clearance_cutoff(base)" not in src

    # Behavioral half: clearance cutoff FAR in the future makes every asset
    # historical; cleared assets must still follow the normal pool order and
    # gates exactly (the reuse clock must not have been replaced by it).
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch, "2027-01-01T00:00:00Z")
    used = _photo("used")
    used["used_count"] = 1
    used["last_used_at"] = "2026-06-01T00:00:00Z"   # long rested, but staged
    fresh = _photo("fresh")
    store = ClearanceFakeStore(assets=[used, fresh],
                               clearances=[_receipt(used), _receipt(fresh)])
    # the global once-used rule still excludes `used`; the historical gate
    # did not reopen the reuse lane by clobbering the cooldown clocks
    assert [a["id"] for a in gms.pickable(GYM, store=store,
                                          now=NOW_DT)] == ["fresh"]


def test_present_null_first_indexed_at_is_unknown_never_indexed_at(monkeypatch):
    """REGRESSION: a present-but-NULL first_indexed_at is UNKNOWN and fails
    closed. indexed_at is bumped on every re-sync PATCH, so falling back to it
    here would launder an old asset into 'new'."""
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    a = _photo("nullstamp")
    a["first_indexed_at"] = None                       # column present, NULL
    a["indexed_at"] = "2026-10-03T09:00:00Z"           # post-cutoff re-sync
    assert hmc.asset_first_seen(a) is None
    store = ClearanceFakeStore(assets=[a])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    assert gms.cooldown_fallback(GYM, store=store) == []
    # explicit clearance still frees it (fail closed, not fail permanently)
    store2 = ClearanceFakeStore(assets=[a], clearances=[_receipt(a)])
    assert [x["id"] for x in gms.pickable(GYM, store=store2,
                                          now=NOW_DT)] == ["nullstamp"]


def test_column_absent_first_indexed_at_still_uses_indexed_at(monkeypatch):
    """Legacy rows (column absent entirely, backfilled at migration) still use
    indexed_at — only a PRESENT NULL is unknown."""
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    a = _photo("legacy")
    a.pop("first_indexed_at", None)
    a["indexed_at"] = "2026-10-02T12:00:00Z"           # after cutoff
    store = ClearanceFakeStore(assets=[a])
    assert [x["id"] for x in gms.pickable(GYM, store=store,
                                          now=NOW_DT)] == ["legacy"]


def test_clearance_requires_matching_source_id():
    """REGRESSION: identity is the quadruple (gym_id, asset_id, source_id,
    CURRENT content_hash). A receipt from another source NEVER clears; a
    missing asset source_id fails closed even for known_used."""
    asset = _photo("a1")  # source_id="src1"
    other_source = _receipt(asset)
    other_source["source_id"] = "src2"
    assert hmc.clearance_status(asset, [other_source]) == "uncleared"
    assert hmc.clearance_status(asset, [_receipt(asset)]) == "cleared"
    no_source = _receipt(asset)
    del no_source["source_id"]
    assert hmc.clearance_status(asset, [no_source]) == "uncleared"
    orphan = _photo("orphan")
    orphan["source_id"] = None
    assert hmc.clearance_status(
        orphan, [_receipt(orphan, decision="known_used")]) == "uncleared"


def _video(asset_id):
    v = make_asset(asset_id, gym_id=GYM, kind="video", mime="video/mp4",
                   content_hash=_hash(asset_id))
    v["duration_sec"] = 12.0
    return v


def test_story_candidates_gated_by_shared_clearance(monkeypatch):
    """REGRESSION: the raw Story/Reel lane selects from the same media_asset
    pool and must pass the SAME historical-clearance gate (it previously read
    list_assets directly with no clearance check at all)."""
    from agent import story_candidates as sc
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    old = _video("oldclip")
    old["first_indexed_at"] = "2026-09-15T00:00:00Z"
    new = _video("newclip")
    new["first_indexed_at"] = "2026-10-02T12:00:00Z"
    no_ledger = lambda _v: False
    store = ClearanceFakeStore(assets=[old, new])
    cands, _ = sc.discover_candidates(GYM, store=store,
                                      ledger_lookup=no_ledger)
    assert [c["asset_id"] for c in cands] == ["newclip"]
    store2 = ClearanceFakeStore(assets=[old, new],
                                clearances=[_receipt(old)])
    cands2, _ = sc.discover_candidates(GYM, store=store2,
                                       ledger_lookup=no_ledger)
    assert sorted(c["asset_id"] for c in cands2) == ["newclip", "oldclip"]


def test_story_candidates_clearance_read_failure_fails_closed(monkeypatch):
    from agent import story_candidates as sc
    _flag_on(monkeypatch)
    _set_cutoff(monkeypatch)
    new = _video("newclip")
    new["first_indexed_at"] = "2026-10-02T12:00:00Z"
    store = ClearanceFakeStore(assets=[new], clearance_down=True)
    cands, assets = sc.discover_candidates(GYM, store=store,
                                           ledger_lookup=lambda _v: False)
    assert cands == [] and assets == {}
    with pytest.raises(hmc.HistoricalClearanceUnavailable):
        sc.discover_candidates(GYM, store=store, strict=True,
                               ledger_lookup=lambda _v: False)


def test_story_candidates_flag_off_is_legacy(monkeypatch):
    from agent import story_candidates as sc
    _flag_off(monkeypatch)
    old = _video("oldclip")
    old["first_indexed_at"] = "2026-09-15T00:00:00Z"
    store = ClearanceFakeStore(assets=[old], clearance_down=True)
    cands, _ = sc.discover_candidates(GYM, store=store,
                                      ledger_lookup=lambda _v: False)
    assert [c["asset_id"] for c in cands] == ["oldclip"]
