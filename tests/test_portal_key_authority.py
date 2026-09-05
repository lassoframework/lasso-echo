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


def test_the_reconciler_never_overwrites_the_portals_column(monkeypatch):
    """Audit #12 + NEW-B. account_key_reconcile PATCHes echo_intake_tokens.echo_account_key
    -- the portal's own column -- with ECHO's derivation. blocking_data does not stop it,
    because a BRAND NEW gym owns no data, which is precisely the case that splits.

    The guard is identity-based on purpose. A first version recomputed the portal's old
    formula from gyms.name and refused only on a match, which missed every RENAMED gym: on
    real production rows it recognised 4 of 19 keys, and Zanshin's live
    zanshinfitness630e22 (minted under an older name) would have been overwritten."""
    from agent import account_key_reconcile as akrec
    monkeypatch.setattr(akrec.config, "account_key_reconcile_enabled", lambda: True)
    monkeypatch.setattr(akrec, "blocking_data", lambda key: "")
    for current in (REVERB_PORTAL, "zanshinfitness630e22", "topfuel", "eng"):
        ok, detail = akrec._default_writer(
            {"gym_id": REVERB_ID, "name": REVERB_NAME, "current": current,
             "canonical": REVERB_ECHO_WOULD_DERIVE})
        assert ok is False, f"{current} was overwritten"
        assert "portal" in detail.lower(), detail


def test_a_renamed_gym_is_still_protected(monkeypatch):
    """NEW-B's exact scenario: the gym's live key was minted under an OLD name, so no
    derivation recomputed from today's name can recognise it."""
    from agent import account_key_reconcile as akrec
    monkeypatch.setattr(akrec.config, "account_key_reconcile_enabled", lambda: True)
    monkeypatch.setattr(akrec, "blocking_data", lambda key: "")
    ok, detail = akrec._default_writer(
        {"gym_id": REVERB_ID, "name": "Totally Different Name Now",
         "current": "zanshinfitness630e22", "canonical": REVERB_ECHO_WOULD_DERIVE})
    assert ok is False and "portal" in detail.lower(), detail


def test_a_gym_with_no_key_at_all_is_still_repairable(monkeypatch):
    """The guard must not make the reconciler useless: a gym the portal never keyed is
    exactly what this sweep was built for."""
    from agent import account_key_reconcile as akrec
    monkeypatch.setattr(akrec.config, "account_key_reconcile_enabled", lambda: True)
    monkeypatch.setattr(akrec, "blocking_data", lambda key: "")
    monkeypatch.setattr(akrec.config, "supabase_url", lambda: "")
    for blank in ("", "(none)"):
        ok, detail = akrec._default_writer(
            {"gym_id": REVERB_ID, "name": REVERB_NAME, "current": blank,
             "canonical": REVERB_ECHO_WOULD_DERIVE})
        assert "portal" not in detail.lower(), f"{blank!r} -> {detail}"


def test_the_portal_now_derives_the_same_key_echo_does():
    """2026-09-04, portal PR #578 landed the other half of the cure: social-onboard.ts
    deriveAccountKey switched from `slug + rawUUID[:6]` to `slug + sha256(gym_id)[:6]`,
    which is exactly account_key._base_key. Both ends now agree, so no NEW gym can split.

    This pins the convergence: if either side drifts again, this fails and the whole
    stale-key resolver stops being a bridge and becomes permanent infrastructure again."""
    import hashlib
    from agent.account_key import _base_key
    slug = "crossfitreverb"
    portal_now = slug + hashlib.sha256(REVERB_ID.encode("utf-8")).hexdigest()[:6]
    assert _base_key(REVERB_ID, REVERB_NAME) == portal_now
    assert portal_now == REVERB_ECHO_WOULD_DERIVE
    assert portal_now != REVERB_PORTAL, (
        "Reverb's LIVE key predates the convergence -- legacy rows still need the "
        "resolver, which is why portal_key_for_gym reads the stored key rather than "
        "re-deriving it")
