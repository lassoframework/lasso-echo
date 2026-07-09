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
