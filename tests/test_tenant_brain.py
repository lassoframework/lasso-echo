"""
Per-gym tenant brain tests (Stage 2 Part 10). Offline, adversarial.

Asserts: a kill excludes the concept from THAT tenant's rotation only; one
tenant's brain never leaks into another's reads, prompts, or rotation; a brain
entry carrying an unverified claim is SKIPPED from prompts (the fabrication
gate stays the sole authority on claims); flag OFF = nothing records, nothing
filters.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import runway, tenant_brain  # noqa: E402
from agent.library import Creative  # noqa: E402


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "true")
    return str(tmp_path / "brains")


# ---- recording + reading -------------------------------------------------------------------

def test_events_record_and_read_back(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    assert tenant_brain.record_event("gym_a", "approve_streak", base_dir=bdir,
                                     streak=5)
    assert tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                                     before="Long caption", after="Short.",
                                     rule="Keep captions under two sentences.")
    assert tenant_brain.record_event("gym_a", "deny_reason", base_dir=bdir,
                                     reason="Never show the parking lot.")
    assert tenant_brain.record_event("gym_a", "kill", base_dir=bdir,
                                     concept="concept_x")
    events = tenant_brain.read_events("gym_a", base_dir=bdir)
    assert [e["kind"] for e in events] == ["approve_streak", "edit_diff",
                                           "deny_reason", "kill"]
    assert tenant_brain.killed_concepts("gym_a", base_dir=bdir) == {"concept_x"}
    assert tenant_brain.style_rules("gym_a", base_dir=bdir) == [
        "Keep captions under two sentences."]
    assert tenant_brain.deny_reasons("gym_a", base_dir=bdir) == [
        "Never show the parking lot."]


def test_unknown_kind_refused(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    assert tenant_brain.record_event("gym_a", "fact", base_dir=bdir,
                                     text="We are the best.") is False
    assert tenant_brain.read_events("gym_a", base_dir=bdir) == []


# ---- kill excludes from THAT tenant's rotation only ------------------------------------------

def _lib(tmp_path, names):
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    for n in names:
        (lib / n).write_bytes(b"PNG")
    return str(lib)


def test_kill_excludes_concept_for_that_tenant_only(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(tenant_brain, "brains_dir",
                        lambda base_dir=None: bdir)
    lib = _lib(tmp_path, ["concept_x.png", "concept_y.png"])
    tenant_brain.record_event("gym_a", "kill", base_dir=bdir, concept="concept_x")

    def _bases(account):
        eligible, _exc = runway.classify_creatives(account, lib)
        return {os.path.basename(c.path) for c in eligible}

    a = _bases("gym_a")
    b = _bases("gym_b")
    assert "concept_x.png" not in a, "killed concept still in gym_a rotation"
    assert "concept_y.png" in a
    assert "concept_x.png" in b, "the kill leaked onto gym_b"
    # the exclusion reason is named
    _e, excluded = runway.classify_creatives("gym_a", lib)
    assert excluded.get("concept_x.png", "").startswith("killed by the approver")


def test_brain_never_leaks_across_tenants(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    tenant_brain.record_event("gym_a", "deny_reason", base_dir=bdir,
                              reason="No stock photos.")
    tenant_brain.record_event("gym_b", "deny_reason", base_dir=bdir,
                              reason="No selfies.")
    assert tenant_brain.deny_reasons("gym_a", base_dir=bdir) == ["No stock photos."]
    assert tenant_brain.deny_reasons("gym_b", base_dir=bdir) == ["No selfies."]
    assert tenant_brain.prompt_notes("gym_a", base_dir=bdir) == ["No stock photos."]
    assert tenant_brain.killed_concepts("gym_b", base_dir=bdir) == set()


# ---- the brain can never introduce an unverified claim -----------------------------------------

def test_brain_entry_cannot_introduce_unverified_claim(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    tenant_brain.record_event("gym_a", "deny_reason", base_dir=bdir,
                              reason="Members get 80% better results here.")
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="x", after="y",
                              rule="Always say we save clients $5,000 a year.")
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="x", after="y",
                              rule="Lead with the member's first name.")
    notes = tenant_brain.prompt_notes("gym_a", base_dir=bdir)
    # the two claim-bearing lines are SKIPPED; the clean style rule survives
    assert notes == ["Lead with the member's first name."]
    joined = " ".join(notes)
    assert "80%" not in joined and "$5,000" not in joined


# ---- edit_examples: the learning signal the drafter feeds back --------------------------------

def test_edit_examples_returns_gate_clean_before_after_pairs(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="A long machine draft about the gym.",
                              after="Short and human.", rule="style")
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="Another long one.",
                              after="Punchy version.", rule="style")
    pairs = tenant_brain.edit_examples("gym_a", base_dir=bdir)
    assert pairs == [
        ("A long machine draft about the gym.", "Short and human."),
        ("Another long one.", "Punchy version."),
    ]


def test_edit_examples_skips_after_text_with_uncleared_claim(monkeypatch, tmp_path):
    """An edit whose approved after-text carries an unverified stat is SKIPPED,
    so an example can never smuggle a claim into a prompt."""
    bdir = _arm(monkeypatch, tmp_path)
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="x", after="Members get 80% better results here.",
                              rule="style")
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="y", after="Feel strong again.", rule="style")
    pairs = tenant_brain.edit_examples("gym_a", base_dir=bdir)
    assert pairs == [("y", "Feel strong again.")]
    assert all("80%" not in a for _b, a in pairs)


def test_edit_examples_skips_when_before_text_has_uncleared_claim(monkeypatch, tmp_path):
    """Fabrication safety is symmetric: a legacy BEFORE draft carrying an
    uncleared claim drops the whole pair, even when the after is clean."""
    bdir = _arm(monkeypatch, tmp_path)
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="We save you $5,000 a year for sure.",
                              after="Feel strong again.", rule="style")
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="clean before", after="clean after", rule="style")
    pairs = tenant_brain.edit_examples("gym_a", base_dir=bdir)
    assert pairs == [("clean before", "clean after")]
    assert all("$5,000" not in b for b, _a in pairs)


def test_edit_examples_caps_to_most_recent(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    for i in range(8):
        tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                                  before=f"before {i}", after=f"after {i}",
                                  rule="style")
    pairs = tenant_brain.edit_examples("gym_a", base_dir=bdir, limit=3)
    assert pairs == [("before 5", "after 5"), ("before 6", "after 6"),
                     ("before 7", "after 7")]


def test_edit_examples_never_leaks_across_tenants(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="a-before", after="a-after", rule="style")
    tenant_brain.record_event("gym_b", "edit_diff", base_dir=bdir,
                              before="b-before", after="b-after", rule="style")
    assert tenant_brain.edit_examples("gym_a", base_dir=bdir) == [("a-before", "a-after")]
    assert tenant_brain.edit_examples("gym_b", base_dir=bdir) == [("b-before", "b-after")]


def test_edit_examples_empty_when_flag_off(monkeypatch, tmp_path):
    bdir = _arm(monkeypatch, tmp_path)
    tenant_brain.record_event("gym_a", "edit_diff", base_dir=bdir,
                              before="x", after="y", rule="style")
    monkeypatch.delenv("AGENT_TENANT_BRAIN_ENABLED", raising=False)
    assert tenant_brain.edit_examples("gym_a", base_dir=bdir) == []


# ---- flag off = inert ---------------------------------------------------------------------------

def test_flag_off_records_and_filters_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_TENANT_BRAIN_ENABLED", raising=False)
    bdir = str(tmp_path / "brains")
    assert tenant_brain.record_event("gym_a", "kill", base_dir=bdir,
                                     concept="concept_x") is False
    assert not os.path.exists(os.path.join(bdir, "gym_a.md"))
    assert tenant_brain.killed_concepts("gym_a", base_dir=bdir) == set()
    assert tenant_brain.prompt_notes("gym_a", base_dir=bdir) == []


def test_flag_off_even_with_existing_brain_file(monkeypatch, tmp_path):
    """An armed session wrote a brain; disarming the flag makes reads inert
    (zero behavior change while OFF, whatever is on disk)."""
    bdir = _arm(monkeypatch, tmp_path)
    tenant_brain.record_event("gym_a", "kill", base_dir=bdir, concept="concept_x")
    monkeypatch.delenv("AGENT_TENANT_BRAIN_ENABLED", raising=False)
    assert tenant_brain.killed_concepts("gym_a", base_dir=bdir) == set()
    lib = _lib(tmp_path, ["concept_x.png"])
    monkeypatch.setattr(tenant_brain, "brains_dir", lambda base_dir=None: bdir)
    eligible, _exc = runway.classify_creatives("gym_a", lib)
    # flag OFF: the killed concept is BACK in rotation (reads are inert)
    assert "concept_x.png" in {os.path.basename(c.path) for c in eligible}
