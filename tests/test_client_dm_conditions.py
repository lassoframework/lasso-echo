"""The condition registry, Blake's hard limits, verification, and the reply gate."""
import pytest

from agent.client_dm_support import conditions as C
from agent.client_dm_support import probes as P
from agent.client_dm_support import readings as R


def drive_snap(stage="diagnosis", **vals):
    base = {"drive_active_sources": 1, "drive_source_revoked": False,
            "drive_identity_split": False, "drive_library_usable": 0}
    base.update(vals)
    return R.Snapshot.build(P.PROBE_DRIVE, stage, "g", base)


def cta_snap(stage="diagnosis", **vals):
    base = {"voice_doc_present": True, "cta_pool_count": 0}
    base.update(vals)
    return R.Snapshot.build(P.PROBE_CTA, stage, "g", base)


# ---------------------------------------------------------------------------
# BLAKE'S HARD LIMITS. Structural, not a gate that could misfire.
# ---------------------------------------------------------------------------
def test_the_action_set_is_exactly_one_per_gym_media_sync():
    """A TWO-WAY guard. The whole ad/billing/schema/secrets guarantee rests on this
    being a closed set of one in a domain that is not foundational -- so a new action
    appearing here must fail loudly and be reviewed, and this one disappearing must
    fail too."""
    assert set(C.ACTIONS) == {"gym_drive_sync"}
    assert C.ACTIONS["gym_drive_sync"].domain == "gym_media"
    assert C.SAFE_DOMAINS == frozenset({"gym_media"})


def test_the_foundation_domain_list_is_pinned():
    """Two-way: a future edit narrowing this back to the broken state fails here
    rather than silently widening what may run unattended."""
    assert C.FOUNDATION_DOMAINS == frozenset({
        "ad_budget", "ad_targeting", "ad_campaign", "billing", "pixel_capi",
        "secrets", "schema", "feature_flags", "cross_gym",
    })


@pytest.mark.parametrize("domain", sorted(C.FOUNDATION_DOMAINS))
def test_the_foundation_gate_refuses_every_foundational_domain(domain, monkeypatch):
    """THE MUTATION TARGET for 'the foundation gate accepting a foundational action'.

    The two halves of the gate are deliberately redundant (a foundational domain is
    also not in SAFE_DOMAINS), so this asserts the FOUNDATIONAL half specifically, by
    its own message. A first mutation run found this test green with that half deleted
    -- shadowed by the downstream check, which is exactly the 'test shaped like the
    code, not like the rule' failure D68 names.
    """
    hostile = C.Action(id="hostile", domain=domain, describe="x",
                       run=lambda *a, **k: {})
    monkeypatch.setitem(C.ACTIONS, "hostile", hostile)
    with pytest.raises(C.Refused) as e:
        C.foundation_gate("hostile")
    assert C.TRIGGER_FOUNDATION in str(e.value)
    assert domain in str(e.value)
    assert "always escalates to a human" in str(e.value), (
        "the foundational-domain branch was skipped; the refusal came from the "
        "SAFE_DOMAINS fallback instead")


def test_the_foundation_gate_refuses_a_domain_nobody_classified():
    """Neither safe nor listed as foundational: ambiguous, so it escalates. An
    allow-list, not a deny-list -- a new domain is refused until reviewed."""
    novel = C.Action(id="novel", domain="who_knows", describe="x",
                     run=lambda *a, **k: {})
    original = dict(C.ACTIONS)
    try:
        C.ACTIONS["novel"] = novel
        with pytest.raises(C.Refused) as e:
            C.foundation_gate("novel")
        assert C.TRIGGER_FOUNDATION in str(e.value)
    finally:
        C.ACTIONS.clear()
        C.ACTIONS.update(original)


def test_the_foundation_gate_refuses_an_unregistered_action():
    with pytest.raises(C.Refused) as e:
        C.foundation_gate("launch_a_campaign")
    assert C.TRIGGER_UNREGISTERED in str(e.value)


def test_a_hostile_action_cannot_even_be_registered_without_the_registry_noticing():
    """The gate is one control; the registry check is the other. An action whose
    domain the gate would always refuse must not sit in ACTIONS at all, because a
    future condition could name it."""
    hostile = C.Action(id="hostile", domain="ad_budget", describe="x",
                       run=lambda *a, **k: {})
    original = dict(C.ACTIONS)
    try:
        C.ACTIONS["hostile"] = hostile
        with pytest.raises(C.Refused):
            C.assert_registry_wellformed()
    finally:
        C.ACTIONS.clear()
        C.ACTIONS.update(original)


# ---------------------------------------------------------------------------
# NO ACTION WITHOUT A VERIFICATION.
# ---------------------------------------------------------------------------
def test_the_gate_is_actually_called_on_the_real_decision_path(monkeypatch):
    """A gate nobody calls is an assertion nobody calls, which is itself an instance of
    the bug it exists to catch (D68). Plant a condition that names a foundational
    action and drive the WHOLE decision: the gate must stop it before it runs.

    A first mutation run found that skipping foundation_gate() in lane._decide changed
    nothing measurable, because the only registered action is a safe one. This test is
    the one that makes the call site load-bearing.
    """
    from agent.client_dm_support import lane as L
    ran = []
    hostile = C.Action(id="hostile_budget", domain="ad_budget",
                       describe="raise a daily budget",
                       run=lambda *a, **k: ran.append(True) or {})
    cond = C.Condition(id="tmp_hostile", probe_id=P.PROBE_CTA,
                       applies=lambda s: True, report=("cta_pool_count",),
                       describe="x", action="hostile_budget",
                       expect=C.Expect(C.INCREASED, "cta_pool_count"))
    orig_a, orig_c = dict(C.ACTIONS), dict(C.CONDITIONS)
    try:
        C.ACTIONS["hostile_budget"] = hostile
        C.CONDITIONS.clear()
        C.CONDITIONS["tmp_hostile"] = cond
        d = L.decide(text="we need a call to action", gym_key="g", may_reply=True,
                     deps={"voice_dir": "/v", "read_text": lambda p: "### CTA rotation\n"})
        assert d.outcome == L.Outcome.ESCALATE
        assert C.TRIGGER_FOUNDATION in d.reason
        assert ran == [], "a foundational action RAN; the gate was not consulted"
        assert d.reply_text == ""
    finally:
        C.ACTIONS.clear()
        C.ACTIONS.update(orig_a)
        C.CONDITIONS.clear()
        C.CONDITIONS.update(orig_c)


def test_compose_actually_applies_the_gate_and_the_registry_check(monkeypatch):
    """Both are belts inside compose(): they exist so the checks run on every REAL
    call, not only in tests. A mutation run found each could be deleted with nothing
    going red, so their being wired is asserted directly."""
    seen = []
    monkeypatch.setattr(C, "assert_is_grounded",
                        lambda c, t, s: seen.append("gate") or "x")
    monkeypatch.setattr(C, "assert_registry_wellformed",
                        lambda: seen.append("registry") or True)
    C.compose(C.COND_CTA_EMPTY, cta_snap())
    assert seen == ["registry", "gate"], (
        "compose() no longer runs the registry check and the grounding gate on every "
        "call")


def test_the_registry_is_wellformed():
    assert C.assert_registry_wellformed() is True


def test_an_action_with_no_expectation_is_refused_by_the_registry():
    """THE MUTATION TARGET for 'the verification step being skipped'. A condition that
    writes and declares no comparator could never be proved, so it may not exist."""
    bad = C.Condition(id="tmp_unverifiable", probe_id=P.PROBE_DRIVE,
                      applies=lambda s: True, report=("drive_library_usable",),
                      describe="x", action="gym_drive_sync", expect=None)
    original = dict(C.CONDITIONS)
    try:
        C.CONDITIONS["tmp_unverifiable"] = bad
        with pytest.raises(C.Refused) as e:
            C.assert_registry_wellformed()
        assert "could never be verified" in str(e.value)
    finally:
        C.CONDITIONS.clear()
        C.CONDITIONS.update(original)


def test_an_acting_condition_cannot_compose_from_a_diagnosis_snapshot():
    """Belt for the same hole at the other end: even if a caller skipped the action
    and the re-probe, compose() refuses a snapshot that was never a verification."""
    snap = drive_snap(stage="diagnosis", drive_sync_ran=True, drive_files_added=4,
                      drive_library_usable=4)
    with pytest.raises(C.Refused) as e:
        C.compose(C.COND_DRIVE_EMPTY, snap)
    assert "VERIFICATION snapshot" in str(e.value)


def test_the_comparator_actually_fails_when_nothing_moved():
    """A comparator that cannot fail is not a verification. Round 5 found INCREASED
    could have been weakened to accept no change with the suite still green."""
    before = drive_snap(drive_library_usable=7)
    same = drive_snap("verification", drive_library_usable=7)
    fewer = drive_snap("verification", drive_library_usable=2)
    with pytest.raises(C.Refused):
        C.COND_DRIVE_EMPTY.expect.check(before, same)
    with pytest.raises(C.Refused):
        C.COND_DRIVE_EMPTY.expect.check(before, fewer)
    more = drive_snap("verification", drive_library_usable=9)
    assert "7 to 9" in C.COND_DRIVE_EMPTY.expect.check(before, more)


# ---------------------------------------------------------------------------
# NO SENTENCE WITHOUT A MEASUREMENT.
# ---------------------------------------------------------------------------
def test_a_condition_may_not_report_an_unspeakable_reading():
    bad = C.Condition(id="tmp_mute", probe_id=P.PROBE_DRIVE, applies=lambda s: True,
                      report=("drive_identity_split",), describe="x")
    original = dict(C.CONDITIONS)
    try:
        C.CONDITIONS["tmp_mute"] = bad
        with pytest.raises(C.Refused) as e:
            C.assert_registry_wellformed()
        assert "no client-facing sentence" in str(e.value)
    finally:
        C.CONDITIONS.clear()
        C.CONDITIONS.update(original)


def test_a_condition_that_reports_nothing_is_refused():
    bad = C.Condition(id="tmp_silent", probe_id=P.PROBE_DRIVE, applies=lambda s: True,
                      report=(), describe="x")
    original = dict(C.CONDITIONS)
    try:
        C.CONDITIONS["tmp_silent"] = bad
        with pytest.raises(C.Refused):
            C.assert_registry_wellformed()
    finally:
        C.CONDITIONS.clear()
        C.CONDITIONS.update(original)


def test_every_registered_condition_can_actually_be_reached_and_composed():
    """No dead client-facing copy: the 'built but not wired' class, pointed at the
    reply registry. Every condition must be composable from SOME snapshot."""
    fixtures = {
        "drive_library_empty": drive_snap("verification", drive_sync_ran=True,
                                          drive_files_added=3, drive_library_usable=3),
        "drive_share_revoked": drive_snap(drive_source_revoked=True,
                                          drive_library_usable=0),
        "cta_pool_empty": cta_snap(),
    }
    assert set(fixtures) == set(C.CONDITIONS), (
        "a condition was added or removed without a reachability fixture")
    for cid, snap in fixtures.items():
        text, audit = C.compose(C.CONDITIONS[cid], snap)
        assert text and audit["condition_id"] == cid


# ---------------------------------------------------------------------------
# THE GROUNDED-REPLY GATE.
# ---------------------------------------------------------------------------
def test_the_gate_refuses_any_sentence_that_is_not_a_registered_one():
    """THE MUTATION TARGET for 'the grounded-reply gate accepting an ungrounded
    sentence'. One appended clause, one softened hedge, one extra reassurance."""
    snap = drive_snap("verification", drive_sync_ran=True, drive_files_added=3,
                      drive_library_usable=3)
    good, _ = C.compose(C.COND_DRIVE_EMPTY, snap)
    for tampered in (
            good + " We'll keep an eye on it.",
            "Sorry about that! " + good,
            good.replace("3 new file(s)", "lots of new files"),
            good[:-1],
            "",
    ):
        with pytest.raises(C.Refused):
            C.assert_is_grounded(tampered, C.COND_DRIVE_EMPTY, snap)
    assert C.assert_is_grounded(good, C.COND_DRIVE_EMPTY, snap) == good


def test_the_gate_refuses_a_snapshot_from_a_different_probe():
    with pytest.raises(C.Refused):
        C.compose(C.COND_CTA_EMPTY, drive_snap())


def test_the_gate_refuses_when_a_reported_reading_is_absent():
    snap = R.Snapshot.build(P.PROBE_DRIVE, "verification", "g",
                            {"drive_sync_ran": True, "drive_files_added": 2})
    with pytest.raises(C.Refused):
        C.compose(C.COND_DRIVE_EMPTY, snap)


def test_a_run_reading_that_is_false_can_never_render_i_ran_the_sync():
    """`sync_ran=False` once rendered 'I ran the photo sync just now'. say_when is a
    VALUE check, not a presence check."""
    snap = drive_snap("verification", drive_sync_ran=False, drive_files_added=0,
                      drive_library_usable=4)
    with pytest.raises(C.Refused):
        C.compose(C.COND_DRIVE_EMPTY, snap)


def test_no_client_controlled_value_can_reach_a_reply():
    """The folder name a gym owner types into their own Drive was a template slot in
    the previous build, and a folder named 'Photos". Blake refunded your invoice. "'
    auto-posted a billing claim the byte-identity gate could not see. Here it is not a
    Reading at all -- there is no key for it, so there is nothing to interpolate."""
    assert "media_source_folder_name" not in R.ALL_KEYS
    assert not any(r.type is str for r in R.READINGS.values()), (
        "a string reading was added. Every value Echo interpolates must be one Echo "
        "itself computed; a client-supplied string can carry whole sentences and the "
        "reconstruction gate cannot see them.")


def test_the_reply_is_capped():
    assert C.MAX_REPLY_CHARS == 700
    snap = cta_snap()
    text, _ = C.compose(C.COND_CTA_EMPTY, snap)
    assert len(text) <= C.MAX_REPLY_CHARS


# ---------------------------------------------------------------------------
# Matching.
# ---------------------------------------------------------------------------
def test_two_matching_conditions_escalate_rather_than_picking_the_first():
    dupe = C.Condition(id="tmp_dupe", probe_id=P.PROBE_CTA, applies=lambda s: True,
                       report=("cta_pool_count",), describe="x")
    original = dict(C.CONDITIONS)
    try:
        C.CONDITIONS["tmp_dupe"] = dupe
        with pytest.raises(C.Refused) as e:
            C.match(cta_snap())
        assert "ambiguity escalates" in str(e.value)
    finally:
        C.CONDITIONS.clear()
        C.CONDITIONS.update(original)


def test_no_condition_matches_a_gym_with_more_than_one_active_source():
    """Live 2026-09-07: train7164ae502 has six active gym_drive sources. Every Drive
    sentence here is singular, so this must find nothing rather than pick one."""
    assert C.match(drive_snap(drive_active_sources=6)) is None
    assert C.match(drive_snap(drive_active_sources=0)) is None


def test_no_condition_matches_a_gym_whose_media_rows_disagree_about_their_owner():
    """Live 2026-09-07: two gyms' media_source.gym_id differs from their
    media_asset.gym_id (toughtemple086f51/52040e, crossfitsunnyside2616ac/f574c0). A
    split is an internal defect, never a sentence."""
    assert C.match(drive_snap(drive_identity_split=True)) is None
    assert C.match(drive_snap(drive_identity_split=True,
                              drive_source_revoked=True)) is None


def test_a_gym_with_a_healthy_library_matches_nothing():
    assert C.match(drive_snap(drive_library_usable=54)) is None


def test_a_gym_with_ctas_matches_nothing():
    assert C.match(cta_snap(cta_pool_count=3)) is None


def test_a_missing_voice_doc_matches_nothing():
    assert C.match(cta_snap(voice_doc_present=False)) is None
