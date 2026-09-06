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
    assert "Ad Photos" in text
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
