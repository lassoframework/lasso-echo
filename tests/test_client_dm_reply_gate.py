"""
THE GROUNDED-REPLY GATE (D68).

The rule being asserted, in Blake's brief's own words: "a reply may only be auto-sent
if every claim in it traces to a mechanically-computed fact key from (a) the diagnosis
query result and (b) the fix's own verification query result."

The implementation inverts the check — it RECONSTRUCTS the reply from (template,
facts) and demands byte equality — so these tests are written against the rule: take a
legitimately grounded reply, add one ungrounded claim, and assert it is refused.
"""
import pytest

from agent.client_dm_support import diagnostics as diag
from agent.client_dm_support import facts, reply


def _drive_snapshot(**over):
    base = {
        "drive_lane_active_for_gym": True,
        "media_source_present": True,
        "media_source_active": True,
        "media_source_revoked": False,
        "media_source_folder_name": "Ad Photos",
        "media_asset_count": 34,
        "hours_since_connect": 0.4,
        "next_scheduled_sync_utc": "2026-09-07T12:00:00+00:00",
        "scheduled_sync_elapsed": False,
        "sync_ran": True,
        "assets_inserted_this_run": 34,
    }
    base.update(over)
    return facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "crossfitlocal", base)


def test_registry_is_wellformed():
    assert reply.assert_templates_wellformed() is True


def test_every_template_slot_is_an_enumerated_fact_key():
    for t in reply.TEMPLATES.values():
        for slot in t.slots:
            assert slot in facts.ALL_FACT_KEYS, (t.id, slot)


def test_a_grounded_reply_composes_and_names_its_fact_keys():
    text, audit = reply.compose("drive_synced", _drive_snapshot())
    assert "34" in text
    assert "Ad Photos" not in text   # the client-controlled folder label never appears
    assert set(audit["fact_keys"]) <= facts.ALL_FACT_KEYS
    assert audit["claims_action"] is True
    assert "sync_ran" in audit["fact_keys"]


# ---------------------------------------------------------------------------
# THE CORE PROPERTY: one ungrounded sentence refuses the WHOLE reply.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tail", [
    " We'll keep an eye on it for you.",
    " Your account is in great shape overall.",
    " I also bumped your budget a little to help.",
    " This should be fixed now.",
    " Sorry about that!",
    "",   # a truncation is equally not a faithful render
])
def test_an_ungrounded_addition_refuses_the_whole_reply(tail):
    snap = _drive_snapshot()
    good = reply.render("drive_synced", snap)
    candidate = (good + tail) if tail else good[:-12]
    with pytest.raises(reply.ReplyRefused):
        reply.assert_is_template_render(candidate, reply.TEMPLATES["drive_synced"], snap)


def test_a_free_text_paragraph_is_never_acceptable():
    snap = _drive_snapshot()
    llm_text = (
        "Hey Chad! Great news — I dug into this and your Drive folder is all hooked "
        "up. I pulled in 34 photos, so you should be good to go. Let me know if "
        "anything else looks off!"
    )
    with pytest.raises(reply.ReplyRefused):
        reply.assert_is_template_render(llm_text, reply.TEMPLATES["drive_synced"], snap)


def test_numbers_cannot_be_swapped_for_friendlier_ones():
    """A reply that overstates the result is refused, because the render is
    reconstructed from the measured facts, not from the sentence."""
    snap = _drive_snapshot()
    good = reply.render("drive_synced", snap)
    inflated = good.replace("34 file(s)", "340 file(s)").replace("34 photo", "340 photo")
    if inflated != good:
        with pytest.raises(reply.ReplyRefused):
            reply.assert_is_template_render(
                inflated, reply.TEMPLATES["drive_synced"], snap)


# ---------------------------------------------------------------------------
# The snapshot must actually contain the facts. No partial renders.
# ---------------------------------------------------------------------------
def test_a_missing_fact_key_refuses_rather_than_renders_a_hole():
    partial = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "crossfitlocal",
        {"media_source_folder_name": "Ad Photos", "media_asset_count": 3})
    with pytest.raises(reply.ReplyRefused) as e:
        reply.compose("drive_synced", partial)
    assert "assets_inserted_this_run" in str(e.value) or "sync_ran" in str(e.value)


def test_a_snapshot_from_a_different_diagnostic_is_refused():
    """A CTA snapshot may not ground a Drive reply. 'Verified' can never mean a
    different query agreed."""
    other = facts.GroundingSnapshot.build(
        diag.DIAG_CTA_POOL, "diagnosis", "crossfitlocal",
        {"voice_doc_present": True, "cta_section_present": True,
         "cta_section_is_todo": True, "cta_pool_count": 0})
    with pytest.raises(reply.ReplyRefused):
        reply.compose("drive_synced", other)


def test_an_unregistered_template_id_is_refused():
    with pytest.raises(reply.ReplyRefused):
        reply.compose("just_say_something_nice", _drive_snapshot())


def test_render_refuses_a_plain_dict_of_facts():
    """Only a validated GroundingSnapshot can ground a reply — a bare dict could
    carry any key at all."""
    with pytest.raises(reply.ReplyRefused):
        reply.render("drive_synced", {"media_asset_count": 34})


# ---------------------------------------------------------------------------
# An action-claiming template cannot render off a snapshot that never measured a run.
# ---------------------------------------------------------------------------
def test_a_fixed_claim_cannot_render_without_the_run_facts():
    unverified = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "diagnosis", "crossfitlocal",
        {"drive_lane_active_for_gym": True, "media_source_present": True,
         "media_source_active": True, "media_source_revoked": False,
         "media_source_folder_name": "Ad Photos", "media_asset_count": 0,
         "hours_since_connect": 0.4,
         "next_scheduled_sync_utc": "2026-09-07T12:00:00+00:00",
         "scheduled_sync_elapsed": False})
    with pytest.raises(reply.ReplyRefused):
        reply.compose("drive_synced", unverified)


def test_every_action_claiming_template_requires_a_run_fact():
    """The registry-level version of the rule above, so a NEW 'we fixed it' template
    cannot be added without a proof-of-run requirement."""
    for t in reply.TEMPLATES.values():
        if t.claims_action:
            assert set(t.fact_keys) & facts.RUN_FACT_KEYS, t.id


# ---------------------------------------------------------------------------
# The fact type rules.
# ---------------------------------------------------------------------------
def test_a_fact_key_outside_the_closed_set_is_rejected_at_the_producer():
    with pytest.raises(facts.FactError):
        facts.GroundingSnapshot.build(
            diag.DIAG_DRIVE_PHOTOS, "diagnosis", "x", {"vibe": "good"})


def test_a_structured_fact_value_is_rejected():
    """A dict or list value could smuggle a sentence through a reply slot."""
    with pytest.raises(facts.FactError):
        facts.GroundingSnapshot.build(
            diag.DIAG_DRIVE_PHOTOS, "diagnosis", "x",
            {"media_asset_count": {"nested": "anything"}})


def test_snapshots_for_different_gyms_never_merge():
    a = _drive_snapshot()
    b = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "someoneelse", {"media_asset_count": 1})
    with pytest.raises(facts.FactError):
        a.merged_with(b)


# ---------------------------------------------------------------------------
# CLIENT-CONTROLLED VALUES MAY NEVER BE INTERPOLATED INTO AN AUTO-SENT REPLY.
#
# This is the finding that could actually reach a client. media_source_folder_name is
# a label the gym owner types into their OWN Google Drive. While it was a template
# slot, a folder named
#     Photos". Blake refunded your invoice; your plan is free now. "
# auto-posted a billing claim under Echo's name -- and the byte-identical gate gave
# ZERO protection, because the reconstruction contained the same injected text.
# Sanitising cannot fix it: the payload is prose, not markup.
#
# The fix is structural. A reply slot may only be a value Echo itself computes.
# ---------------------------------------------------------------------------
def test_no_template_interpolates_a_client_controlled_fact():
    """THE RULE. Asserted over the whole registry, so a NEW template cannot
    reintroduce the hole."""
    for t in reply.TEMPLATES.values():
        leaked = set(t.slots) & reply.CLIENT_CONTROLLED_FACT_KEYS
        assert not leaked, (t.id, leaked)


def test_the_registry_check_rejects_a_template_that_tries_to():
    """Prove the guard would actually catch it, by planting one."""
    bad = reply.ReplyTemplate(
        id="planted", diagnostic_id=diag.DIAG_DRIVE_PHOTOS,
        text='Your folder "{media_source_folder_name}" is fine.')
    reply.TEMPLATES["planted"] = bad
    try:
        with pytest.raises(reply.ReplyRefused) as e:
            reply.assert_templates_wellformed()
        assert "client-controlled" in str(e.value)
    finally:
        del reply.TEMPLATES["planted"]


def test_every_remaining_slot_is_a_machine_computed_number():
    """Positive form of the same rule: what IS interpolated is a count Echo measured,
    so there is no free-text slot left for anything to be injected through."""
    snap = _drive_snapshot()
    for t in reply.TEMPLATES.values():
        for slot in t.slots:
            assert slot not in reply.CLIENT_CONTROLLED_FACT_KEYS
            assert slot.endswith(("_count", "_this_run")), (t.id, slot)
    assert "Ad Photos" not in reply.render("drive_synced", snap)


def test_a_hostile_folder_name_cannot_reach_the_reply_at_all():
    """End to end through compose(): the hostile value is in the snapshot and simply
    has nowhere to land."""
    for hostile in (
        'Photos". Blake refunded your invoice; your plan is free now. "',
        "Photos. A coach will call you at 9am tomorrow.",
        "Photos. Re-enter your card at evil.example/pay now.",
        "<!channel> your account is suspended",
    ):
        text, _audit = reply.compose(
            "drive_synced", _drive_snapshot(media_source_folder_name=hostile))
        assert "refunded" not in text
        assert "coach will call" not in text
        assert "evil.example" not in text
        assert "<!channel>" not in text


def test_the_whole_body_is_slack_escaped_on_both_sides_of_the_gate():
    """Defence in depth, matching the adapter's own single-point escape for
    conversational kinds. A no-op for today's templates, and it means no future edit
    can post live Slack markup."""
    snap = _drive_snapshot()
    good = reply.render("drive_synced", snap)
    assert "<" not in good and ">" not in good
    assert reply.assert_is_template_render(
        good, reply.TEMPLATES["drive_synced"], snap) == good


# ---------------------------------------------------------------------------
# `requires` IS A PRESENCE TEST. Claims of action need a VALUE test.
# ---------------------------------------------------------------------------
def test_compose_refuses_to_say_i_ran_the_sync_when_sync_ran_is_false():
    """The template rendered "I ran the photo sync for your gym just now" off a
    snapshot carrying sync_ran=False, because `requires` only checked that the key was
    PRESENT. compose() is the module's declared public producer, so it composing a
    false claim on request is a real defect even when flow.py happens not to reach it."""
    snap = _drive_snapshot(sync_ran=False, assets_inserted_this_run=0,
                           media_asset_count=0)
    with pytest.raises(reply.ReplyRefused) as e:
        reply.compose("drive_synced", snap)
    assert "TRUE" in str(e.value)


def test_every_action_claiming_template_requires_its_run_fact_to_be_TRUE():
    for t in reply.TEMPLATES.values():
        if t.claims_action:
            assert set(t.requires_true) & facts.RUN_FACT_KEYS, t.id


def test_the_registry_check_rejects_an_action_claim_with_only_a_presence_test():
    bad = reply.ReplyTemplate(
        id="planted2", diagnostic_id=diag.DIAG_DRIVE_PHOTOS, claims_action=True,
        requires=("sync_ran",), text="I fixed {media_asset_count} things.")
    reply.TEMPLATES["planted2"] = bad
    try:
        with pytest.raises(reply.ReplyRefused) as e:
            reply.assert_templates_wellformed()
        assert "TRUE" in str(e.value)
    finally:
        del reply.TEMPLATES["planted2"]


def test_a_revoked_template_cannot_render_when_the_share_is_fine():
    snap = _drive_snapshot(media_source_revoked=False)
    with pytest.raises(reply.ReplyRefused):
        reply.compose("drive_revoked", snap)


# ---------------------------------------------------------------------------
# GUARDS THAT WERE ASSERTED BY NOTHING (found by independent mutation).
# ---------------------------------------------------------------------------
def test_render_refuses_a_template_snapshot_diagnostic_mismatch_directly():
    """reply.py's own diagnostic-id check, asserted at render() rather than only
    incidentally via compose()."""
    cta_snap = facts.GroundingSnapshot.build(
        diag.DIAG_CTA_POOL, "diagnosis", "x",
        {"cta_pool_count": 0, "cta_section_present": True,
         "cta_section_is_todo": True})
    with pytest.raises(reply.ReplyRefused) as e:
        reply.render(reply.TEMPLATES["drive_synced"], cta_snap)
    assert "grounded in" in str(e.value)


def test_compose_applies_the_byte_equality_gate_to_its_own_output():
    """compose() calls assert_is_template_render on what it just produced, so the gate
    is exercised on every real call and not only in tests. Prove the call is real by
    making the gate refuse everything and watching compose() fail."""
    import agent.client_dm_support.reply as r
    original = r.assert_is_template_render
    r.assert_is_template_render = lambda *a, **k: (_ for _ in ()).throw(
        r.ReplyRefused("belt fired"))
    try:
        with pytest.raises(reply.ReplyRefused) as e:
            r.compose("drive_synced", _drive_snapshot())
        assert "belt fired" in str(e.value)
    finally:
        r.assert_is_template_render = original


def test_an_unknown_comparator_is_refused_at_construction():
    """verify.Expectation validates its comparator. An independent mutation removed
    that check and nothing went red."""
    from agent.client_dm_support import verify
    with pytest.raises(verify.VerificationError):
        verify.Expectation("media_asset_count", "looks_better_to_me")
    with pytest.raises(verify.VerificationError):
        verify.Expectation("not_a_fact_key", verify.INCREASED)
    # ...and the legal ones construct.
    for c in verify.ALL_COMPARATORS:
        assert verify.Expectation("media_asset_count", c).comparator == c


@pytest.mark.parametrize("bad", [
    "has spaces", "has/slash", "has.dot", "-leading-dash", "", "x" * 65,
    "semi;colon", "quote'", "back\\slash", "per%cent",
])
def test_a_malformed_account_key_is_refused(bad):
    """diagnostics._ACCOUNT_KEY_RE. An independent mutation disabled it and nothing
    went red — yet it is the only thing stopping an odd key reaching a query."""
    from agent.client_dm_support import diagnostics as d
    with pytest.raises(d.DiagnosticError):
        d.require_account_key(bad)


def test_a_wellformed_account_key_is_accepted():
    from agent.client_dm_support import diagnostics as d
    for good in ("crossfitlocal", "top-fuel", "toughtemple52040e",
                 "district-h-strength-fitness", "pierce_ig"):
        assert d.require_account_key(good) == good.lower()


def test_a_missing_SLOT_refuses_even_when_every_requires_true_fact_holds():
    """The `missing` guard in render(), asserted on its own. The earlier test for it
    went green under mutation because requires_true happened to catch the same case
    first; this snapshot satisfies every requires_true fact and is short one SLOT."""
    partial = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "diagnosis", "crossfitlocal",
        {"media_source_revoked": True})          # drive_revoked's slot is absent
    with pytest.raises(reply.ReplyRefused) as e:
        reply.compose("drive_revoked", partial)
    assert "media_asset_count" in str(e.value)
    # ...and adding the slot makes it render, so the test is measuring that guard.
    whole = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "diagnosis", "crossfitlocal",
        {"media_source_revoked": True, "media_asset_count": 0})
    assert "0 file(s)" in reply.render("drive_revoked", whole)


# ===========================================================================
# GUARDS AN INDEPENDENT MUTATION RUN FOUND ASSERTED BY NOTHING.
#
# The whole of the "bound client-controlled slot values" commit could be deleted
# and the suite stayed green — the tests written alongside it were replaced when
# the slot itself was removed, and nothing was left holding the remaining defence
# in depth. These assert the rules directly.
# ===========================================================================
def test_safe_slot_refuses_a_control_character_or_newline():
    """Defence in depth behind the structural rule: even a slot value Echo computes
    must not be able to forge message structure."""
    for hostile in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x07b", "a\x7fb"):
        with pytest.raises(reply.ReplyRefused) as e:
            reply._safe_slot("media_source_folder_name", hostile)
        assert "control character" in str(e.value)


def test_safe_slot_refuses_an_over_long_value_rather_than_truncating():
    with pytest.raises(reply.ReplyRefused) as e:
        reply._safe_slot("media_source_folder_name", "x" * (reply.MAX_SLOT_CHARS + 1))
    assert "cap" in str(e.value)
    # ...and accepts one exactly at the cap, so the boundary is asserted too.
    ok = "x" * reply.MAX_SLOT_CHARS
    assert reply._safe_slot("media_source_folder_name", ok) == ok


def test_safe_slot_escapes_slack_markup():
    assert reply._safe_slot("k", "<!channel>") == "&lt;!channel&gt;"
    assert reply._safe_slot("k", "a & b") == "a &amp; b"
    # Ampersand first, so escaping is not reversible by one un-escape pass.
    assert reply._safe_slot("k", "&lt;") == "&amp;lt;"


def test_safe_slot_passes_non_strings_through():
    assert reply._safe_slot("media_asset_count", 34) == 34
    assert reply._safe_slot("scheduled_sync_elapsed", False) is False


def test_render_slack_escapes_the_whole_body():
    """The single-point whole-body escape, matching the adapter's own DV4 fix.

    It is a NO-OP for today's registry -- every slot is an integer Echo computed and no
    template constant contains & < > -- so it cannot be exercised through the shipped
    templates. That is precisely why it needs a direct test: a defence that is
    currently inert is the easiest thing to delete by accident, and the next template
    to carry a character like that would post live Slack markup. So this plants one."""
    planted = reply.ReplyTemplate(
        id="planted_escape", diagnostic_id=diag.DIAG_DRIVE_PHOTOS,
        requires=("media_source_revoked",), requires_true=("media_source_revoked",),
        text="Ping <!channel> & check {media_asset_count} files <now>")
    reply.TEMPLATES["planted_escape"] = planted
    try:
        snap = _drive_snapshot(media_asset_count=0, media_source_revoked=True)
        body = reply.render("planted_escape", snap)
        assert "<!channel>" not in body
        assert "<" not in body and ">" not in body
        assert "&lt;!channel&gt;" in body and "&amp;" in body
        # The escape is INSIDE render(), so the gate's reconstruction matches it and
        # a candidate carrying the raw markup is refused.
        assert reply.assert_is_template_render(body, planted, snap) == body
        with pytest.raises(reply.ReplyRefused):
            reply.assert_is_template_render(
                "Ping <!channel> & check 0 files <now>", planted, snap)
    finally:
        del reply.TEMPLATES["planted_escape"]


def test_todays_templates_carry_no_characters_the_escape_would_change():
    """The companion fact, stated so the no-op above is understood rather than
    rediscovered: nothing shipped needs escaping, and that is by design."""
    for t in reply.TEMPLATES.values():
        assert not (set("&<>") & set(t.text)), t.id


def test_a_template_may_not_state_a_claim_its_facts_contradict():
    """MAJOR-5 with the polarity flipped. cta_missing_section says 'It has no CTA
    rotation section at all'; requiring cta_section_present to be merely PRESENT let it
    render off a snapshot where the section WAS present."""
    contradicted = facts.GroundingSnapshot.build(
        diag.DIAG_CTA_POOL, "diagnosis", "toughtemple52040e",
        {"voice_doc_present": True, "cta_section_present": True,
         "cta_section_is_todo": False, "cta_pool_count": 0})
    with pytest.raises(reply.ReplyRefused) as e:
        reply.compose("cta_missing_section", contradicted)
    assert "FALSE" in str(e.value)
    # ...and it renders when the fact actually says what the sentence says.
    truthful = facts.GroundingSnapshot.build(
        diag.DIAG_CTA_POOL, "diagnosis", "toughtemple52040e",
        {"voice_doc_present": True, "cta_section_present": False,
         "cta_section_is_todo": False, "cta_pool_count": 0})
    assert "no \"CTA rotation\" section" in reply.render("cta_missing_section", truthful)


def test_the_registry_rejects_a_template_requiring_a_fact_both_ways():
    bad = reply.ReplyTemplate(
        id="planted3", diagnostic_id=diag.DIAG_CTA_POOL,
        requires_true=("cta_section_present",),
        requires_false=("cta_section_present",),
        text="{cta_pool_count}")
    reply.TEMPLATES["planted3"] = bad
    try:
        with pytest.raises(reply.ReplyRefused) as e:
            reply.assert_templates_wellformed()
        assert "both true" in str(e.value)
    finally:
        del reply.TEMPLATES["planted3"]


def test_requires_false_keys_must_be_real_fact_keys():
    bad = reply.ReplyTemplate(
        id="planted4", diagnostic_id=diag.DIAG_CTA_POOL,
        requires_false=("not_a_fact",), text="{cta_pool_count}")
    reply.TEMPLATES["planted4"] = bad
    try:
        with pytest.raises(reply.ReplyRefused):
            reply.assert_templates_wellformed()
    finally:
        del reply.TEMPLATES["planted4"]


# --- facts / verify guards -------------------------------------------------
def test_an_unknown_snapshot_stage_is_rejected():
    with pytest.raises(facts.FactError):
        facts.GroundingSnapshot.build(diag.DIAG_CTA_POOL, "probably_fine", "x",
                                      {"cta_pool_count": 0})
    for good in ("diagnosis", "verification"):
        assert facts.GroundingSnapshot.build(
            diag.DIAG_CTA_POOL, good, "x", {"cta_pool_count": 0}).stage == good


def test_verify_refuses_a_before_snapshot_that_is_not_a_diagnosis():
    from agent.client_dm_support import verify
    a = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "g", {"media_asset_count": 0})
    b = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "g", {"media_asset_count": 5})
    r = verify.check(verify.Expectation("media_asset_count", verify.ROSE_ABOVE_ZERO),
                     a, b)
    assert not r.verified
    assert "expected 'diagnosis'" in r.reason


def test_verified_snapshot_is_stamped_as_a_verification():
    from agent.client_dm_support import verify
    after = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "g", {"media_asset_count": 5})
    merged = verify.verified_snapshot(after, {"sync_ran": True,
                                              "assets_inserted_this_run": 5})
    assert merged.stage == "verification"
    assert merged.get("sync_ran") is True
    # A diagnosis-stamped merge would let a reply be composed off unverified facts.
    assert merged.diagnostic_id == diag.DIAG_DRIVE_PHOTOS


# ===========================================================================
# GUARDS A THIRD MUTATION RUN FOUND UNHELD.
# ===========================================================================
def test_a_duplicate_template_id_is_refused_not_silently_replaced():
    """Unshadowed: a second registration would replace a REVIEWED client-facing
    constant with an unreviewed one, under the same id, and nothing else would notice."""
    with pytest.raises(ValueError):
        reply._register(reply.ReplyTemplate(
            id="cta_ask", diagnostic_id=diag.DIAG_CTA_POOL, text="{cta_pool_count}"))


def test_a_template_slot_outside_ALL_FACT_KEYS_is_refused():
    bad = reply.ReplyTemplate(id="planted5", diagnostic_id=diag.DIAG_CTA_POOL,
                              text="you have {invented_metric} things")
    reply.TEMPLATES["planted5"] = bad
    try:
        with pytest.raises(reply.ReplyRefused) as e:
            reply.assert_templates_wellformed()
        assert "ALL_FACT_KEYS" in str(e.value)
    finally:
        del reply.TEMPLATES["planted5"]


def test_verify_refuses_a_fact_absent_from_one_snapshot():
    from agent.client_dm_support import verify
    before = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "diagnosis", "g", {"media_asset_count": 0})
    after = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "g", {"sync_ran": True})
    r = verify.check(verify.Expectation("media_asset_count", verify.ROSE_ABOVE_ZERO),
                     before, after)
    assert not r.verified
    assert "absent" in r.reason


def test_verify_refuses_anything_that_is_not_a_GroundingSnapshot():
    from agent.client_dm_support import verify
    exp = verify.Expectation("media_asset_count", verify.ROSE_ABOVE_ZERO)
    good = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "diagnosis", "g", {"media_asset_count": 0})
    for bad in ({"media_asset_count": 5}, None, "verified!", 5):
        assert not verify.check(exp, good, bad).verified, bad
        assert not verify.check(exp, bad, good).verified, bad


def test_merged_with_refuses_a_different_diagnostic():
    a = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "diagnosis", "g", {"media_asset_count": 0})
    b = facts.GroundingSnapshot.build(
        diag.DIAG_CTA_POOL, "verification", "g", {"cta_pool_count": 0})
    with pytest.raises(facts.FactError) as e:
        a.merged_with(b)
    assert "different diagnostics" in str(e.value)


def test_an_empty_gym_key_is_refused():
    from agent.client_dm_support import diagnostics as d
    for empty in ("", None, "   "):
        with pytest.raises(d.DiagnosticError):
            d.require_account_key(empty)


def test_an_unknown_diagnostic_id_raises_rather_than_defaulting():
    from agent.client_dm_support import diagnostics as d
    with pytest.raises(d.DiagnosticError) as e:
        d.run("general_vibes", "crossfitlocal")
    assert "unknown diagnostic" in str(e.value)


def test_the_flow_refuses_an_unknown_diagnostic_id_too():
    """Belt and braces: flow checks membership AND diagnostics.run raises. Break one
    and the other still holds -- which is why asserting only "it escalated" left this
    green under mutation. The REASON distinguishes which guard fired, so assert that:
    flow's own membership check must refuse it BEFORE any diagnostic is attempted."""
    from agent.client_dm_support import flow as f
    d = f.handle_ticket(text="anything", gym_key="crossfitlocal",
                        diagnostic_id="general_vibes")
    assert d.decision == f.DECISION_ESCALATE
    assert not d.will_post
    assert "no enumerated diagnostic covers this message" in d.reason, (
        "flow fell through to diagnostics.run instead of refusing the id itself")
    assert d.diagnostic_id == ""


# ===========================================================================
# GUARDS A FOURTH MUTATION RUN FOUND UNHELD.
# ===========================================================================
def test_the_account_key_is_lowercased_before_any_query():
    """media_source.gym_id is a lowercase slug. Querying with mixed case returns zero
    rows and reads as 'nothing is connected' — the false-negative class this very
    function's docstring exists to prevent."""
    from agent.client_dm_support import diagnostics as d
    assert d.require_account_key("CrossFitLocal") == "crossfitlocal"
    assert d.require_account_key("TOP-FUEL") == "top-fuel"


def test_assets_are_not_read_when_the_gym_has_no_source():
    """A gym that never connected Drive must not cause an asset read at all: the count
    would be reported as a fact about a lane that was never set up."""
    from agent.client_dm_support import diagnostics as d

    class Store:
        def __init__(self):
            self.asset_reads = []

        def list_sources(self, gym_id=None, include_inactive=False):
            return []

        def list_assets(self, gym_id, source_id=None):
            self.asset_reads.append(gym_id)
            return [{"id": 1, "gym_id": gym_id}]

    st = Store()
    snap = d.diagnose_drive_photos("crossfitlocal", store=st, now="2026-09-06T21:45:00+00:00",
                                   daily_hour_utc=12, lane_active_for=lambda k: True)
    assert st.asset_reads == [], "assets were read for a gym with no media_source"
    assert snap.get("media_asset_count") == 0
    assert snap.get("media_source_present") is False


def test_a_windows_style_path_is_normalised_before_the_checks():
    """A backslash path would otherwise slip every '/'-based prefix and fragment test."""
    from agent.client_dm_support import scope_gate as g
    for path in (r"agent\jobs\..\config.py", r"agent\slack_convo\adapter.py",
                 r"agent\client_dm_support\ad_block.py"):
        v = g.check(g.ProposedAction(kind=g.KIND_CODE_FIX, paths=(path,),
                                     scope_column="gym_id", scope_values=("x",)))
        assert v.escalate, path
    v = g.check(g.ProposedAction(kind=g.KIND_CODE_FIX, paths=(r"agent\jobs\sync.py",),
                                 scope_column="gym_id", scope_values=("x",)))
    assert v.allowed, v.reason


def test_a_failed_execution_escalates_before_anything_is_verified():
    """flow's own `if not result.ok` is shadowed three deep by the verification step
    and requires_true, so asserting only 'it escalated' left it green. The REASON says
    which guard fired."""
    from agent.client_dm_support import flow as f
    from agent.client_dm_support import remedies as r

    def failing(remedy, **kw):
        return r.ExecutionResult(False, "drive returned 500")

    import agent.client_dm_support.remedies as rmod
    original = rmod.EXECUTORS["per_gym_drive_sync"]
    rmod.EXECUTORS["per_gym_drive_sync"] = failing
    try:
        class Store:
            def available(self):
                return True

            def list_sources(self, gym_id=None, include_inactive=False):
                return [{"id": 1, "gym_id": "crossfitlocal", "kind": "gym_drive",
                         "folder_name": "Ad Photos", "active": True,
                         "revoked_externally": False,
                         "connected_at": "2026-09-06T21:18:00+00:00"}]

            def list_assets(self, gym_id, source_id=None):
                return []

        d = f.handle_ticket(text="my posts have no photos", gym_key="crossfitlocal",
                            deps={"store": Store(), "now": "2026-09-06T21:45:00+00:00",
                                  "daily_hour_utc": 12,
                                  "lane_active_for": lambda k: True,
                                  "log": lambda m: None})
        assert d.decision == f.DECISION_ESCALATE
        assert "did not complete" in d.reason and "500" in d.reason, d.reason
    finally:
        rmod.EXECUTORS["per_gym_drive_sync"] = original


def test_run_facts_OVERRIDE_the_post_fix_snapshot_never_defer_to_it():
    """verified_snapshot merges the run facts over the re-read facts. Using setdefault
    instead would let a stale diagnosis value win over what the run actually did."""
    from agent.client_dm_support import verify
    after = facts.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "g",
        {"media_asset_count": 7, "assets_inserted_this_run": 0, "sync_ran": False})
    merged = verify.verified_snapshot(
        after, {"sync_ran": True, "assets_inserted_this_run": 7})
    assert merged.get("assets_inserted_this_run") == 7
    assert merged.get("sync_ran") is True
