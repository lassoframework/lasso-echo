"""
Tenant scaffold from intake (Stage 2 Part 3). Offline.

Asserts: a complete intake payload scaffolds the whole tenant (voice doc the
voice loader reads, avatar, verified facts, tenant.json with approver + sender
phones + lanes + trust 0); the facts file feeds the fabrication gate (a note
citing a verified fact clears, an uncited stat still blocks); missing fields
block loud with nothing written; flag OFF = inert; PENDING facts never reach
the gate.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import rotation, tenants  # noqa: E402
from agent.voice import load_voice  # noqa: E402


def _payload(**over):
    p = {
        "key": "ironworks_gym",
        "name": "Ironworks Gym",
        "avatar": "Busy parents in Carmel who want strength without the 6am grind.",
        "voice": {
            "tone": "Warm, direct, zero hype.",
            "story": "A neighborhood gym that coaches real people.",
            "ctas": ["Save this for later.", "Send this to a friend."],
            "hashtags": ["#IronworksGym", "#CarmelFitness"],
        },
        "verified_facts": [
            "Members train an average of 3 sessions per week.",
            "Ironworks has served the neighborhood for 9 years.",
        ],
        "unverified_facts": ["We doubled revenue last year."],
        "approver": {"name": "Dana Ruff", "phone": "+13175550101"},
        "sender_phones": ["+13175550101", "+13175550102"],
        "media_lanes": ["sms", "upload"],
    }
    p.update(over)
    return p


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")


# ---- scaffold complete --------------------------------------------------------------------

def test_scaffold_complete(monkeypatch, tmp_path):
    _arm(monkeypatch)
    out = tenants.intake_create(_payload(), base_dir=str(tmp_path))
    assert out and not out.get("blocked")
    assert set(out["files"]) == {"voice.md", "avatar.md", "verified_facts.md",
                                 "tenant.json"}
    tdir = out["dir"]
    # the voice doc is loadable by the real loader, with CTAs + hashtags intact
    v = load_voice(os.path.join(tdir, "voice.md"))
    assert v is not None
    assert "Save this for later." in v.ctas
    assert "#IronworksGym" in v.hashtags
    # avatar verbatim
    avatar = open(os.path.join(tdir, "avatar.md"), encoding="utf-8").read()
    assert "Busy parents in Carmel" in avatar
    # tenant.json carries routing + approver + lanes + trust 0
    rec = json.load(open(os.path.join(tdir, "tenant.json"), encoding="utf-8"))
    assert rec["approver_name"] == "Dana Ruff"
    assert rec["approver_phone"] == "+13175550101"
    assert rec["sender_phones"] == ["+13175550101", "+13175550102"]
    assert rec["media_lanes"] == ["sms", "upload"]
    assert rec["trust"] == 0
    assert rec["storage_quota_mb"] > 0
    assert rec["monthly_recreate_budget"] > 0


def test_sender_phone_resolves_to_tenant(monkeypatch, tmp_path):
    _arm(monkeypatch)
    tenants.intake_create(_payload(), base_dir=str(tmp_path))
    assert tenants.tenant_for_sender("+13175550102", base_dir=str(tmp_path)) == "ironworks_gym"
    # formatting noise is normalized, never guessed beyond digits
    assert tenants.tenant_for_sender("(317) 555-0102", base_dir=str(tmp_path)) is None
    assert tenants.tenant_for_sender("+13175559999", base_dir=str(tmp_path)) is None
    assert tenants.tenant_for_sender("", base_dir=str(tmp_path)) is None


# ---- the facts file feeds the fabrication gate ----------------------------------------------

def test_facts_file_wired_to_gate(monkeypatch, tmp_path):
    _arm(monkeypatch)
    tenants.intake_create(_payload(), base_dir=str(tmp_path))
    claims = tenants.tenant_approved_claims("ironworks_gym", base_dir=str(tmp_path))
    assert "Members train an average of 3 sessions per week." in claims
    # a note citing the verified fact clears the gate
    assert rotation.is_gate_clean(
        "Members train an average of 3 sessions per week.",
        approved_claims=claims) is True
    # an uncited stat still blocks (adversarial)
    assert rotation.is_gate_clean(
        "Members lose 20% body fat in 30 days guaranteed.",
        approved_claims=claims) is False


def test_pending_facts_never_reach_the_gate(monkeypatch, tmp_path):
    _arm(monkeypatch)
    tenants.intake_create(_payload(), base_dir=str(tmp_path))
    claims = tenants.tenant_approved_claims("ironworks_gym", base_dir=str(tmp_path))
    assert all("doubled revenue" not in c for c in claims)
    assert rotation.is_gate_clean("We doubled revenue last year, up 100%.",
                                  approved_claims=claims) is False


# ---- blocking: loud, specific, nothing written ----------------------------------------------

def test_missing_fields_block_with_list(monkeypatch, tmp_path):
    _arm(monkeypatch)
    p = _payload()
    del p["approver"]
    p["media_lanes"] = []
    out = tenants.intake_create(p, base_dir=str(tmp_path))
    assert out["blocked"]
    joined = " ".join(out["blocked"])
    assert "approver" in joined and "media_lanes" in joined
    # all-or-nothing: nothing was written
    assert not os.path.isdir(os.path.join(str(tmp_path), "ironworks_gym"))


def test_bad_phone_blocks(monkeypatch, tmp_path):
    _arm(monkeypatch)
    out = tenants.intake_create(
        _payload(approver={"name": "Dana", "phone": "not a phone"}),
        base_dir=str(tmp_path))
    assert out["blocked"]
    assert any("phone" in p for p in out["blocked"])


def test_existing_tenant_never_overwritten(monkeypatch, tmp_path):
    _arm(monkeypatch)
    assert not tenants.intake_create(_payload(), base_dir=str(tmp_path)).get("blocked")
    out2 = tenants.intake_create(_payload(name="Impostor Gym"), base_dir=str(tmp_path))
    assert out2["blocked"]
    rec = tenants.load_tenant("ironworks_gym", base_dir=str(tmp_path))
    assert rec["name"] == "Ironworks Gym"    # the original stands


def test_flag_off_inert(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    assert tenants.intake_create(_payload(), base_dir=str(tmp_path)) is None
    assert not os.path.isdir(os.path.join(str(tmp_path), "ironworks_gym"))
