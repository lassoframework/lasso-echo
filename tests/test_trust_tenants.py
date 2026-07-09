"""
Trust ladder wiring for Stage 2 tenants (Part 4). Offline, adversarial.

The law: trust is PER-ACCOUNT DATA, never global. A brand new tenant starts at
FULL_APPROVAL no matter what any other account has earned, and can NEVER
auto-publish: level 0 blocks, the flag double-gate blocks, and the first-post
gate blocks even a hand-raised level with an approved calendar.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, tenants, trust  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.trust import TrustLevel  # noqa: E402


def _tenant(monkeypatch, tmp_path, key="newgym", **over):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    payload = {
        "key": key, "name": "New Gym", "avatar": "Local families.",
        "voice": {"tone": "Warm."},
        "approver": {"name": "Sam", "phone": "+13175550200"},
        "sender_phones": ["+13175550200"], "media_lanes": ["upload"],
    }
    payload.update(over)
    out = tenants.intake_create(payload, base_dir=str(tmp_path))
    assert not out.get("blocked"), out
    return out


def _acct(key="newgym", level=TrustLevel.FULL_APPROVAL):
    return Account(key=key, display_name=key, platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y", trust=level)


def _draft(key="newgym", day="2026-08-03"):
    return Draft(draft_id="t1", account_key=key, platform="instagram",
                 caption="c", hashtags=[], creative_path="a.png",
                 creative_public_url="", scheduled_for=f"{day}T18:00:00+00:00",
                 day_key=day, draft_type="feed")


# ---- a new tenant starts at level 0, per account, regardless of neighbors -----------------

def test_new_tenant_starts_full_approval(monkeypatch, tmp_path):
    _tenant(monkeypatch, tmp_path)
    assert trust.level_for_tenant("newgym", base_dir=str(tmp_path)) == TrustLevel.FULL_APPROVAL


def test_new_tenant_full_approval_even_when_neighbor_is_level_1(monkeypatch, tmp_path):
    """Another tenant's earned level NEVER leaks onto a new tenant."""
    _tenant(monkeypatch, tmp_path, key="veteran_gym")
    # hand-raise the veteran (the only way a level changes: hand-edited data)
    vpath = os.path.join(str(tmp_path), "veteran_gym", "tenant.json")
    rec = json.load(open(vpath))
    rec["trust"] = 1
    json.dump(rec, open(vpath, "w"))
    _tenant(monkeypatch, tmp_path, key="newgym")
    assert trust.level_for_tenant("veteran_gym", base_dir=str(tmp_path)) == TrustLevel.ROUTINE_AUTO
    assert trust.level_for_tenant("newgym", base_dir=str(tmp_path)) == TrustLevel.FULL_APPROVAL


def test_missing_or_corrupt_tenant_fails_safe(monkeypatch, tmp_path):
    assert trust.level_for_tenant("ghost", base_dir=str(tmp_path)) == TrustLevel.FULL_APPROVAL
    _tenant(monkeypatch, tmp_path, key="corrupt_gym")
    cpath = os.path.join(str(tmp_path), "corrupt_gym", "tenant.json")
    rec = json.load(open(cpath))
    rec["trust"] = "banana"
    json.dump(rec, open(cpath, "w"))
    assert trust.level_for_tenant("corrupt_gym", base_dir=str(tmp_path)) == TrustLevel.FULL_APPROVAL


# ---- the headline lock: a new tenant can NEVER auto-publish -------------------------------

def test_new_tenant_can_never_auto_publish(monkeypatch, tmp_path):
    """Adversarial stack: flag armed, another account level 1 with an approved
    calendar, and the new tenant still cards on every path."""
    monkeypatch.setenv("AGENT_TRUST_LADDER_ENABLED", "true")
    _tenant(monkeypatch, tmp_path)
    level = trust.level_for_tenant("newgym", base_dir=str(tmp_path))
    acct = _acct("newgym", level)
    d = _draft("newgym")

    # requires_approval: True at level 0 even with the flag armed
    assert trust.requires_approval(acct, d) is True
    eligible, reason = trust.auto_eligibility(acct, d)
    assert eligible is False
    assert "level 0" in reason


def test_even_a_hand_raised_new_tenant_hits_the_first_post_gate(monkeypatch, tmp_path):
    """Belt and suspenders: even if someone hand-raises a brand new tenant to
    level 1 AND approves its calendar, zero publish history means the first
    post still cards (first post never automated)."""
    monkeypatch.setenv("AGENT_TRUST_LADDER_ENABLED", "true")
    key = "eager_gym_probe"
    _tenant(monkeypatch, tmp_path, key=key)
    acct = _acct(key, TrustLevel.ROUTINE_AUTO)
    d = _draft(key)
    # approve its calendar for the month
    db.kv_set(f"approved_calendar_{key}_2026-08", json.dumps(["a.png"]))
    try:
        eligible, reason = trust.auto_eligibility(acct, d)
        assert eligible is False
        assert "first post" in reason
    finally:
        with db._lock, db.connect() as conn:
            conn.execute("DELETE FROM kv WHERE key=?",
                         (f"approved_calendar_{key}_2026-08",))
            conn.commit()


def test_flag_off_double_gate_blocks_everything(monkeypatch, tmp_path):
    """With AGENT_TRUST_LADDER_ENABLED off (the default), even level 1 data
    changes nothing: everything requires approval."""
    monkeypatch.delenv("AGENT_TRUST_LADDER_ENABLED", raising=False)
    acct = _acct("whoever", TrustLevel.ROUTINE_AUTO)
    assert trust.requires_approval(acct, _draft("whoever")) is True
