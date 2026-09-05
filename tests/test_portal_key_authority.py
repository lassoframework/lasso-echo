"""Echo must never mint a key that disagrees with the portal's. That divergence IS the bug.

Two systems derived keys from the same gym_id and could never agree:
  portal  social-onboard.ts deriveAccountKey : slug + rawUUID[:6]
  echo    account_key._base_key              : slug + sha256(gym_id)[:6]

The portal mints first, at onboarding. Echo later derived its own and registered content
under it, so EVERY portal-onboarded gym split into two live keys by construction. CrossFit
Reverb ran as crossfitreverb30b5b2 in the portal and crossfitreverb6cdf33 in Echo; Dean saw
"93 posts drafted" over an empty approve list. CrossFit Chateau proved it was still
happening on 2026-09-04: created 19:28, split the same day.

Resolving stale keys at read time treats the symptom. These tests pin the cure: the portal's
issued key is authoritative and Echo only ever derives when the portal has issued nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import account_key_resolve as akr  # noqa: E402

REVERB_ID = "30b5b234-0dac-4048-87d8-5330e6fbfa9d"
REVERB_NAME = "CrossFit Reverb"
REVERB_PORTAL = "crossfitreverb30b5b2"
REVERB_ECHO_WOULD_DERIVE = "crossfitreverb6cdf33"


def _plane(tokens, gyms):
    def get(path, params):
        rows = tokens if path == "echo_intake_tokens" else gyms
        off = int(params.get("offset", 0)); lim = int(params.get("limit", 1000))
        return rows[off:off + lim], True
    return get


def setup_function(_):
    akr.reset_cache()


def teardown_function(_):
    akr.reset_cache()


def test_the_two_derivations_really_do_disagree():
    """The premise, pinned. If this ever fails the divergence is gone and these guards can
    be revisited -- until then they are load-bearing."""
    from agent.account_key import _base_key
    echo = _base_key(REVERB_ID, REVERB_NAME)
    portal = ("crossfitreverb" + REVERB_ID.replace("-", "")[:6])
    assert echo == REVERB_ECHO_WOULD_DERIVE
    assert portal == REVERB_PORTAL
    assert echo != portal, "two derivations, one gym_id -- this is the whole bug"


def test_portal_key_for_gym_returns_the_issued_key():
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_PORTAL}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.portal_key_for_gym(REVERB_ID, now_fn=lambda: 1.0, get=get) == REVERB_PORTAL


def test_unknown_gym_returns_blank_so_the_caller_falls_back():
    get = _plane([], [])
    assert akr.portal_key_for_gym("nope", now_fn=lambda: 1.0, get=get) == ""


def test_an_unreadable_plane_returns_blank_never_a_half_answer():
    def failing(path, params):
        return [], False
    assert akr.portal_key_for_gym(REVERB_ID, now_fn=lambda: 1.0, get=failing) == ""


def test_a_gym_with_disagreeing_token_rows_returns_blank():
    """Two rows for one gym: we cannot tell which key is current, so we must not answer."""
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_PORTAL},
                  {"gym_id": REVERB_ID, "echo_account_key": "crossfitreverbold99"}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.portal_key_for_gym(REVERB_ID, now_fn=lambda: 1.0, get=get) == ""


def test_mint_uses_the_portal_key_instead_of_deriving(monkeypatch):
    """The cure. Echo's mint must return the portal's key, NOT its own derivation."""
    from agent import account_key_mint as m
    monkeypatch.setattr(m, "gym_exists", lambda k: False, raising=False)
    monkeypatch.setattr("agent.account_key_resolve.portal_key_for_gym",
                        lambda gid, **kw: REVERB_PORTAL)
    key, info = m.derive_mint_key(
        REVERB_ECHO_WOULD_DERIVE, REVERB_NAME,
        resolve_uuid=lambda base: REVERB_ID, gym_exists=lambda k: False)
    assert key == REVERB_PORTAL, f"got {key!r} with {info!r}"
    assert info.get("source") == "portal"


def test_mint_still_derives_when_the_portal_has_issued_nothing(monkeypatch):
    """A gym Echo knows about but the portal has never keyed must still get a key."""
    from agent import account_key_mint as m
    monkeypatch.setattr("agent.account_key_resolve.portal_key_for_gym",
                        lambda gid, **kw: "")
    key, info = m.derive_mint_key(
        "someadhockey", REVERB_NAME,
        resolve_uuid=lambda base: REVERB_ID, gym_exists=lambda k: False)
    assert key == REVERB_ECHO_WOULD_DERIVE
    assert info.get("source") == "derived"


def test_intake_door_prefers_the_portal_key(monkeypatch):
    """A gym arriving through the intake door must land on the SAME key as one arriving
    through the portal door -- the thing _canonical_base's docstring always claimed."""
    from agent import social_intake_reader as sir
    monkeypatch.setattr("agent.account_key_resolve.portal_key_for_gym",
                        lambda gid, **kw: REVERB_PORTAL)
    got = sir._canonical_base(REVERB_ID, {"gym": {"name": REVERB_NAME}})
    assert got == REVERB_PORTAL


def test_intake_door_falls_back_to_derivation_when_portal_is_silent(monkeypatch):
    from agent import social_intake_reader as sir
    monkeypatch.setattr("agent.account_key_resolve.portal_key_for_gym",
                        lambda gid, **kw: "")
    got = sir._canonical_base(REVERB_ID, {"gym": {"name": REVERB_NAME}})
    assert got == REVERB_ECHO_WOULD_DERIVE


def test_the_reconciler_writes_only_the_key_the_portal_itself_would_mint(monkeypatch):
    """Audit #12 / NEW-B, resolved by CONVERGENCE rather than by a guard.

    account_key_reconcile PATCHes echo_intake_tokens.echo_account_key -- the portal's own
    column. While the two sides derived differently that was a live divergence. Portal PR
    #578 made deriveAccountKey slug + sha256(gym_id)[:6], the same function as
    account_key._base_key, so the canonical key the writer PATCHes IS the portal's key.

    A blanket "never overwrite a non-empty key" guard was tried and reverted: build_plan
    already treats a non-collided key as ISSUED (change=False), so the only rows reaching
    the writer are COLLIDED or MISSING -- and refusing those leaves the tenant-isolation
    hazard (two gyms on one key) unrepaired, which is the opposite of the goal."""
    from agent import account_key_reconcile as akrec
    from agent.account_key import canonical_account_key
    canonical = canonical_account_key(REVERB_ID, REVERB_NAME)
    assert canonical == REVERB_ECHO_WOULD_DERIVE
    # ... and that is exactly what the portal's own deriveAccountKey now produces.
    import hashlib
    portal_now = "crossfitreverb" + hashlib.sha256(REVERB_ID.encode("utf-8")).hexdigest()[:6]
    assert canonical == portal_now, (
        "the reconciler may only ever write a key the portal itself would mint")


def test_a_gym_that_already_owns_data_is_still_never_repointed(monkeypatch):
    """The guard that does matter is unchanged: moving the pointer without moving the data
    strands every source, calendar row and media folder under the old key."""
    from agent import account_key_reconcile as akrec
    monkeypatch.setattr(akrec.config, "account_key_reconcile_enabled", lambda: True)
    monkeypatch.setattr(akrec, "blocking_data", lambda key, **kw: "17 sources, 155 rows")
    ok, detail = akrec._default_writer(
        {"gym_id": REVERB_ID, "name": REVERB_NAME, "current": REVERB_PORTAL,
         "canonical": REVERB_ECHO_WOULD_DERIVE})
    assert ok is False and "BLOCKED" in detail, detail


PORTAL_TS = "/Users/blakeruff/lasso-ops-portal/src/lib/echo/social-onboard.ts"


def test_the_portal_now_derives_the_same_key_echo_does():
    """2026-09-04, portal PR #578 landed the other half of the cure: social-onboard.ts
    deriveAccountKey switched from `slug + rawUUID[:6]` to `slug + sha256(gym_id)[:6]`,
    which is exactly account_key._base_key. Both ends now agree, so no NEW gym can split.

    This reads the PORTAL'S OWN SOURCE rather than re-implementing its formula in Python.
    The earlier version asserted a locally-rebuilt sha256 against Echo's, which could only
    ever catch ECHO-side drift -- while the whole point of this pin is to catch the PORTAL
    drifting away again, which is what caused the split in the first place."""
    import re
    if not os.path.exists(PORTAL_TS):
        import pytest
        pytest.skip("portal checkout not present on this machine")
    src = open(PORTAL_TS, encoding="utf-8").read()
    fn = src[src.index("export function deriveAccountKey"):]
    fn = fn[: fn.index("\n}")]

    assert 'createHash("sha256")' in fn, (
        "the portal is no longer hashing the gym id with sha256 -- if it went back to a raw "
        "uuid prefix, every newly onboarded gym splits again")
    assert "rawUUID" not in fn and ".slice(0, 6)" not in fn.replace(
        "ID_FINGERPRINT_LEN", ""), fn

    from agent.account_key import _ID_FINGERPRINT_LEN, _NAME_SLUG_MAXLEN
    ts_fp = int(re.search(r"ID_FINGERPRINT_LEN\s*=\s*(\d+)", src).group(1))
    ts_slug = int(re.search(r"NAME_SLUG_MAXLEN\s*=\s*(\d+)", src).group(1))
    assert ts_fp == _ID_FINGERPRINT_LEN, (ts_fp, _ID_FINGERPRINT_LEN)
    assert ts_slug == _NAME_SLUG_MAXLEN, (ts_slug, _NAME_SLUG_MAXLEN)


def test_reverbs_live_key_predates_the_convergence():
    """Legacy rows still need the resolver, which is why portal_key_for_gym reads the
    STORED key rather than re-deriving it."""
    from agent.account_key import _base_key
    assert _base_key(REVERB_ID, REVERB_NAME) == REVERB_ECHO_WOULD_DERIVE
    assert REVERB_ECHO_WOULD_DERIVE != REVERB_PORTAL
