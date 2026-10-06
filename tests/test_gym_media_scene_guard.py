"""
DRAFT global cross-tenant scene guard (pHash belt on PR235), offline.

STATUS: INCOMPLETE / NOT OPERATIONAL (Astra rejection 2026-10-03). The
prep-time scene writer architecture was rejected, so the scene ledger has no
legitimate writer and the guard is pinned SCENE_GUARD_OPERATIONAL = False:
an ARMED AGENT_VISUAL_SCENE_GUARD fails closed as not-yet-operational
(strict_claims raises SceneLedgerUnavailable; non-strict returns []), never
consulting the ledger. Flag OFF = byte-for-byte legacy behavior; an ambiguous
flag value fails closed. The full read machinery (unfiltered paginated scan
in pinned full-primary-key order, Content-Range completeness proofs, mid-scan
change and ordering-violation detection, bare-phash/group_key/used_date row
validation, tenant/date mapping) is kept intact for the redesign and pinned
here at the unit level — a one-bit-away phash MUST still be returned by the
full scan, and the cross-date hold policy inputs must be decidable from it.
"""

import os
import sys
from datetime import datetime, timezone as _tz

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_selector as gms  # noqa: E402
from agent import visual_scene  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402

GYM = "gymx"
TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOW_DT = datetime(2026, 10, 3, 12, 0, 0, tzinfo=_tz.utc)
TODAY = "2026-10-03"        # NOW_DT's date — the pick's target date
YESTERDAY = "2026-10-02"

# Controlled hamming distances (namespaced candidate form).
MINE = "scene:phash64:" + "0" * 16                # p1's scene
NEAR = "scene:phash64:" + "0" * 14 + "03"         # hamming 2 from MINE
ONE_BIT = "scene:phash64:" + "0" * 15 + "1"       # hamming EXACTLY 1 from MINE
CANDIDATE = "scene:phash64:" + "f" * 7 + "ff" + "0" * 7  # hamming 8 from p2's
DISTINCT = "scene:phash64:" + "f" * 16            # hamming 64 from MINE

# asset_id -> bare hex; monkeypatched over visual_scene.scene_fingerprint.
SCENE_MAP = {
    "p1": "0" * 16,
    "p2": "f" * 7 + "0" * 9,
    "p3": "f" * 16,
}


def _bare(namespaced):
    return namespaced.split(":", 2)[-1]


def _photo(asset_id):
    return make_asset(asset_id, gym_id=GYM, kind="photo")


def _read_bytes(asset):
    return b"bytes:" + str(asset["id"]).encode("utf-8")


@pytest.fixture
def scene_hashes(monkeypatch):
    """Deterministic scene fingerprints keyed off SCENE_MAP, no image decode."""

    def fake_scene_fingerprint(data):
        asset_id = data.decode("utf-8").split(":", 1)[1]
        return f"scene:phash64:{SCENE_MAP[asset_id]}"

    monkeypatch.setattr(visual_scene, "scene_fingerprint", fake_scene_fingerprint)


class _Resp:
    def __init__(self, rows, status=200, headers=None):
        self._rows = rows
        self.status_code = status
        self.headers = headers if headers is not None else (
            {"Content-Range": f"*/{len(rows)}" if not rows
             else f"0-{len(rows) - 1}/{len(rows)}"})

    def json(self):
        return self._rows


class FakeSceneHttp:
    """Scripted PostgREST for tenant_alias + a paginated visual_scene_phash
    full scan. Rows are served in the pinned full-primary-key order
    (phash, tenant_id, group_key) like the ordered real server; pass
    unordered=True to violate that ordering on purpose."""

    def __init__(self, tenant_rows, scene_rows, fail_on=None, unordered=False):
        self._tenant = tenant_rows
        self._scenes = (list(reversed(scene_rows)) if unordered
                        else sorted(scene_rows, key=lambda r: (
                            str(r.get("phash")), str(r.get("tenant_id")),
                            str(r.get("group_key")))))
        self._fail_on = fail_on
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self._fail_on and self._fail_on in url:
            return _Resp({"message": "boom"}, status=500)
        if "tenant_alias" in url:
            return _Resp(list(self._tenant))
        if "visual_scene_phash" in url:
            start, end = (int(part) for part in headers["Range"].split("-"))
            total = len(self._scenes)
            if total == 0:
                return _Resp([], headers={"Content-Range": "*/0"})
            page = self._scenes[start:end + 1]
            return _Resp(page, headers={
                "Content-Range": f"{start}-{start + len(page) - 1}/{total}"})
        raise AssertionError(f"unexpected scene ledger URL: {url}")


def _creds(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-test")


def _scene(namespaced_phash, tenant, group="vg_a", used_date=TODAY):
    """A well-formed DRAFT row: BARE 16-hex phash (the recording RPC strips the
    namespace before insert), tenant UUID, non-empty group_key, strict date."""
    return {"phash": _bare(namespaced_phash), "tenant_id": tenant,
            "group_key": group, "used_date": used_date}


# ---- unit: cross_tenant_scene_phashes ----------------------------------------

def test_cross_tenant_scene_phashes_maps_tenants_and_dates(monkeypatch):
    _creds(monkeypatch)
    http = FakeSceneHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}],
        [_scene(MINE, OTHER_TENANT, used_date=YESTERDAY),
         _scene(MINE, TENANT), _scene(NEAR, TENANT)])
    own, known = gms.cross_tenant_scene_phashes(GYM, http=http)
    assert own == TENANT
    assert known == {MINE: {OTHER_TENANT: {YESTERDAY}, TENANT: {TODAY}},
                     NEAR: {TENANT: {TODAY}}}


def test_one_bit_away_phash_is_returned_by_the_full_scan(monkeypatch):
    """P0 #1, read level: a hash sharing NO exact equality with any candidate
    must still come back — the scan is a full table read, never an IN filter."""
    _creds(monkeypatch)
    http = FakeSceneHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}],
        [_scene(ONE_BIT, OTHER_TENANT)])
    own, known = gms.cross_tenant_scene_phashes(GYM, http=http)
    assert ONE_BIT in known
    assert visual_scene.hamming_distance(MINE, ONE_BIT) == 1
    # The read carried no candidate hash: no exact-match filter was consulted.
    scene_calls = [c for c in http.calls if "visual_scene_phash" in c[0]]
    assert all("in." not in str(c[1]) and "eq." not in str(c[1])
               for c in scene_calls)


def test_empty_scene_table_is_proven_empty(monkeypatch):
    _creds(monkeypatch)
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], [])
    own, known = gms.cross_tenant_scene_phashes(GYM, http=http)
    assert own == TENANT and known == {}


def test_missing_creds_fail_closed(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=FakeSceneHttp([], []))


def test_read_failure_fails_closed(monkeypatch):
    _creds(monkeypatch)
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                         fail_on="visual_scene_phash")
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=http)


def test_missing_table_404_fails_closed(monkeypatch):
    """The scene table is DRAFT-only: a 404 (unapplied migration) is
    unavailability, never 'no known scenes'."""
    _creds(monkeypatch)
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                         fail_on="visual_scene_phash")
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=http)


def test_unmapped_tenant_fails_closed(monkeypatch):
    _creds(monkeypatch)
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=FakeSceneHttp([], []))


def test_malformed_canonical_tenant_uuid_fails_closed(monkeypatch):
    _creds(monkeypatch)
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": "not-a-uuid"}], [])
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=http)


@pytest.mark.parametrize("row", [
    # namespaced phash: the table stores the BARE 16-hex only
    {"phash": MINE, "tenant_id": TENANT, "group_key": "vg_a",
     "used_date": TODAY},
    {"phash": _bare(MINE), "tenant_id": "not-a-uuid", "group_key": "vg_a",
     "used_date": TODAY},
    {"phash": _bare(MINE), "tenant_id": TENANT, "group_key": "",
     "used_date": TODAY},
    {"phash": _bare(MINE), "tenant_id": TENANT, "group_key": "vg_a",
     "used_date": ""},
    {"phash": _bare(MINE), "tenant_id": TENANT, "group_key": "vg_a",
     "used_date": "10/03/2026"},
    {"phash": _bare(MINE), "tenant_id": TENANT, "group_key": "vg_a",
     "used_date": "2026-13-40"},
    {"phash": _bare(MINE), "tenant_id": TENANT, "group_key": "vg_a"},
])
def test_malformed_row_fails_closed(monkeypatch, row):
    _creds(monkeypatch)
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], [row])
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=http)


def test_scene_read_requires_content_range_proof(monkeypatch):
    _creds(monkeypatch)
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], [])
    original_get = http.get

    def missing_range(*args, **kwargs):
        response = original_get(*args, **kwargs)
        response.headers = {}
        return response

    http.get = missing_range
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=http)


def test_scene_scan_paginates_with_proven_pages(monkeypatch):
    _creds(monkeypatch)
    rows = [_scene(f"scene:phash64:{index:016x}", TENANT,
                   group=f"vg_{index}") for index in range(150)]
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], rows)
    own, known = gms.cross_tenant_scene_phashes(GYM, http=http)
    assert own == TENANT
    assert known == {f"scene:phash64:{row['phash']}": {TENANT: {TODAY}}
                     for row in rows}
    calls = [call for call in http.calls if "visual_scene_phash" in call[0]]
    assert [call[2]["Range"] for call in calls] == ["0-99", "100-199"]
    assert all(call[2]["Prefer"] == "count=exact" for call in calls)
    assert all(call[1]["order"] == "phash.asc,tenant_id.asc,group_key.asc"
               for call in calls)


def test_scene_scan_rejects_a_mid_scan_table_change(monkeypatch):
    _creds(monkeypatch)
    rows = [_scene(f"scene:phash64:{index:016x}", TENANT,
                   group=f"vg_{index}") for index in range(150)]
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], rows)
    original_get = http.get
    calls = {"n": 0}

    def shrinking(*args, **kwargs):
        calls["n"] += 1
        response = original_get(*args, **kwargs)
        if calls["n"] > 1 and "visual_scene_phash" in args[0]:
            response.headers = {"Content-Range": "100-149/149"}
        return response

    http.get = shrinking
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=http)


def test_scene_scan_rejects_out_of_order_pages(monkeypatch):
    """A page set that violates the pinned full-PK order proves the server did
    not honor the snapshot the spans were checked against: fail closed."""
    _creds(monkeypatch)
    rows = [_scene(f"scene:phash64:{index:016x}", TENANT,
                   group=f"vg_{index}") for index in range(150)]
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], rows,
                         unordered=True)
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.cross_tenant_scene_phashes(GYM, http=http)


# ---- pickable integration: the guard is armed-but-NOT-OPERATIONAL ------------
#
# ASTRA REJECTION (2026-10-03): the prep-time scene writer architecture was
# rejected (prep-time records mark UNUSED candidates as used, are not atomic
# with the real visual_global_usage claim, and race across gyms). The scene
# ledger therefore has NO legitimate writer, so an armed guard can never prove
# a scene 'distinct': pickable() fails closed as not-yet-operational, and the
# read machinery above is kept intact (and unit-tested) but unreachable until
# SCENE_GUARD_OPERATIONAL flips. The redesign list lives in the migration
# STATUS section and docs/VISUAL_SCENE_GUARD_DRAFT.md.

def test_scene_guard_is_pinned_not_operational():
    assert gms.SCENE_GUARD_OPERATIONAL is False


def test_flag_off_is_byte_for_byte_legacy(monkeypatch):
    """Flag unset: the scene ledger is never consulted and photos stay pickable
    exactly as before."""
    monkeypatch.delenv(gms.SCENE_GUARD_FLAG_ENV, raising=False)
    store = FakeMediaStore(assets=[_photo("p1")])

    def _boom(*a, **k):
        raise AssertionError("flag OFF must never touch the scene ledger")

    monkeypatch.setattr(gms, "cross_tenant_scene_phashes", _boom)
    picks = gms.pickable(GYM, store=store, now=NOW_DT)
    assert [a["id"] for a in picks] == ["p1"]


def test_armed_flag_fails_closed_not_operational(monkeypatch, scene_hashes, capsys):
    """Armed but not operational: the pool closes with the explicit
    not-operational reason, and the (writer-less, unreadable) scene ledger is
    NEVER consulted — the read machinery is unreachable."""
    _creds(monkeypatch)
    monkeypatch.setenv(gms.SCENE_GUARD_FLAG_ENV, "true")
    store = FakeMediaStore(assets=[_photo("p1"), _photo("p3")])

    def _boom(*a, **k):
        raise AssertionError("non-operational guard must never read the ledger")

    monkeypatch.setattr(gms, "cross_tenant_scene_phashes", _boom)
    picks = gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=None,
                         scene_read_bytes=_read_bytes)
    assert picks == []
    assert "scene ledger read failed" in capsys.readouterr().out
    with pytest.raises(gms.SceneLedgerUnavailable, match="not operational"):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=None,
                     scene_read_bytes=_read_bytes, strict_claims=True)


def test_armed_flag_never_reaches_the_read_machinery(monkeypatch, scene_hashes):
    """Even with a perfectly populated fake ledger, an armed guard raises
    before any scan: no table state can make it 'operational'."""
    _creds(monkeypatch)
    monkeypatch.setenv(gms.SCENE_GUARD_FLAG_ENV, "true")
    store = FakeMediaStore(assets=[_photo("p1")])
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}],
                         [_scene(MINE, OTHER_TENANT)])
    with pytest.raises(gms.SceneLedgerUnavailable, match="not operational"):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                     scene_read_bytes=_read_bytes, strict_claims=True)
    assert not [c for c in http.calls if "visual_scene_phash" in c[0]]


# ---- the read machinery survives for the redesign: unit-level pins -----------
# (reachable only via direct calls while SCENE_GUARD_OPERATIONAL is False)

def test_read_machinery_maps_tenants_and_dates_for_the_redesign(monkeypatch):
    _creds(monkeypatch)
    http = FakeSceneHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}],
        [_scene(MINE, OTHER_TENANT, used_date=YESTERDAY),
         _scene(MINE, TENANT), _scene(NEAR, TENANT)])
    own, known = gms.cross_tenant_scene_phashes(GYM, http=http)
    assert own == TENANT
    assert known == {MINE: {OTHER_TENANT: {YESTERDAY}, TENANT: {TODAY}},
                     NEAR: {TENANT: {TODAY}}}


def test_cross_date_hold_policy_is_classified_by_the_surviving_machinery():
    """The same-tenant cross-date hold policy inputs: a same-date near match
    vs a different-date one is decidable from the (phash -> tenant -> dates)
    mapping the scan returns, and classify_scene_all reports every match."""
    one_bit = "scene:phash64:" + "0" * 15 + "1"
    two_bit = "scene:phash64:" + "0" * 14 + "03"
    cand = "scene:phash64:" + "0" * 14 + "ff"
    c = visual_scene.classify_scene_all(MINE, [two_bit, cand, one_bit])
    assert c.kind == visual_scene.NEAR_FRAME
    assert c.near_matches == ((one_bit, 1), (two_bit, 2))
    assert c.candidate_matches == ((cand, 8),)


def test_flag_on_ledger_outage_fails_closed(monkeypatch, scene_hashes):
    _creds(monkeypatch)
    monkeypatch.setenv(gms.SCENE_GUARD_FLAG_ENV, "true")
    store = FakeMediaStore(assets=[_photo("p1")])
    http = FakeSceneHttp([], [], fail_on="tenant_alias")
    assert gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                        scene_read_bytes=_read_bytes) == []
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                     scene_read_bytes=_read_bytes, strict_claims=True)


def test_ambiguous_flag_value_fails_closed(monkeypatch):
    monkeypatch.setenv(gms.SCENE_GUARD_FLAG_ENV, "sometimes")
    store = FakeMediaStore(assets=[_photo("p1")])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, strict_claims=True)


def test_unfingerprintable_photo_fails_closed(monkeypatch):
    """A usable photo whose bytes cannot be scene-fingerprinted has no provable
    scene status: with the flag ON it closes the pool, never passes."""
    _creds(monkeypatch)
    monkeypatch.setenv(gms.SCENE_GUARD_FLAG_ENV, "true")
    store = FakeMediaStore(assets=[_photo("p1")])
    http = FakeSceneHttp([{"alias_key": GYM, "tenant_id": TENANT}], [])

    def _no_bytes(asset):
        return None

    assert gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                        scene_read_bytes=_no_bytes) == []
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                     scene_read_bytes=_no_bytes, strict_claims=True)
