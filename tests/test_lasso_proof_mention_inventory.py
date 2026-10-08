"""Mention applicability requires exact, explicit owned LASSO source evidence."""
from pathlib import Path
from copy import deepcopy
from datetime import date, timedelta

import pytest
from agent import calendar_grade as grade, config, social_proof

PENDING = Path(__file__).resolve().parents[1] / "brand_voice/knowledge/03_social_proof_pending.md"


def rows():
    # Synthetic production-shaped book: 70% book, one long hook, eight numeric
    # rows, no permission-granted client mentions. No real client claims used.
    words = "steady clear calm focused warm simple active stable sound sharp ready patient honest useful able solid quiet direct fresh local".split()
    result = []
    for i, word in enumerate(words):
        hook = f"A {word} plan helps a gym owner follow through."
        if i == 0:
            hook += " A thoughtful booking process makes it easier to focus on the people walking through your door and organize the next step for each visitor."
        cap = hook + "\n\nYour calendar gives the team a clear next action and helps owners understand what needs attention. Keep the process useful and easy to follow."
        if i < 4:
            cap += f" This is synthetic planning note {i + 1}."
        cap += "\n\nBook a call at lassoframework.com."
        pillar = "book" if i < 14 else ("doctrine", "echo", "platform")[i % 3]
        for account in ("instagram", "facebook"):
            result.append(dict(gym_id="lasso", account=account, format="feed", caption=cap,
                pillar=pillar, post_date=(date(2026, 10, 8) + timedelta(days=i)).isoformat(),
                image_url="https://example.test/synthetic.png"))
    return result


@pytest.fixture
def verified(monkeypatch, tmp_path):
    voice = tmp_path / "brand_voice"
    (voice / "knowledge").mkdir(parents=True)
    pending = voice / "knowledge/03_social_proof_pending.md"
    pending.write_bytes(PENDING.read_bytes())
    monkeypatch.delenv("AGENT_SOCIAL_PROOF_PATH", raising=False)
    monkeypatch.setattr(config, "SOCIAL_PROOF_PATH", "brand_voice/social_proof.md")
    monkeypatch.setattr(grade, "_proof_inventory_root", lambda: tmp_path)
    monkeypatch.setattr(social_proof, "source_path", lambda key: voice / "social_proof.md")
    monkeypatch.setenv("AGENT_CTA_VARIETY", "false")
    return voice, pending


def test_verified_zero_mentions_returns_explicit_metadata_and_both_account_receipts(verified):
    source = grade._lasso_zero_mention_inventory(rows())
    assert source["approved_entries"] == 0 and source["pending_entries"] == 13
    assert source["pending_sha256"] == grade._LASSO_PENDING_PROOF_SHA256
    assert source["confirmed_absent_accounts"] == ["lasso_ig", "lasso_fb"]
    assert len(source["source_paths"]) == 2


def test_real_production_shaped_book_moves_88_to_96_without_other_rule_changes(verified, monkeypatch):
    book = rows(); frozen = deepcopy(book)
    monkeypatch.setenv("AGENT_SOCIAL_PROOF_PATH", "custom.md")
    before = grade.grade_month(book, profile="B2B")
    monkeypatch.delenv("AGENT_SOCIAL_PROOF_PATH")
    after = grade.grade_month(book, profile="B2B")
    assert (before.total, after.total) == (88, 96)
    assert before.scores["visual_match"] == 7 and after.scores["visual_match"] == 15
    assert {k: v for k, v in before.scores.items() if k != "visual_match"} == {
        k: v for k, v in after.scores.items() if k != "visual_match"}
    assert grade.A_THRESHOLD == 90 and book == frozen
    assert after.exempt["visual_match: client mention quota inapplicable with verified zero approved inventory"] == 8
    assert after.exemption_evidence["client_mentions"]["pending_entries"] == 13
    assert not any("@mention" in d[2] for d in after.defects)


@pytest.mark.parametrize("name", ["social_proof.md", "social_proof.lasso_ig.md", "social_proof.lasso_fb.md"])
@pytest.mark.parametrize("content", ["", "Permission: yes\nVerified date: today", "unknown content"])
def test_any_existing_proof_object_retains_mention_quota(verified, name, content):
    voice, _ = verified
    (voice / name).write_text(content)
    assert grade._lasso_zero_mention_inventory(rows()) is None
    assert grade.grade_month(rows(), profile="B2B").scores["visual_match"] == 7


@pytest.mark.parametrize("override", ["", "other.md"])
def test_even_empty_explicit_override_retains_quota(verified, monkeypatch, override):
    monkeypatch.setenv("AGENT_SOCIAL_PROOF_PATH", override)
    assert grade._lasso_zero_mention_inventory(rows()) is None


def test_config_override_retains_quota_even_after_env_changes(verified, monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_PROOF_PATH", "unknown.md")
    assert grade._lasso_zero_mention_inventory(rows()) is None


@pytest.mark.parametrize("failure", ["changed", "missing", "missing_parent", "unreadable"])
def test_unknown_pending_or_parent_evidence_retains_quota(verified, monkeypatch, failure):
    voice, pending = verified
    if failure == "changed":
        pending.write_bytes(pending.read_bytes() + b"\nchanged")
    elif failure == "missing":
        pending.unlink()
    elif failure == "missing_parent":
        monkeypatch.setattr(grade, "_proof_inventory_root", lambda: voice / "missing")
    else:
        original = Path.read_bytes
        def broken(path):
            if path == pending:
                raise PermissionError("pending inventory inaccessible")
            return original(path)
        monkeypatch.setattr(Path, "read_bytes", broken)
    assert grade._lasso_zero_mention_inventory(rows()) is None


def test_parent_permission_error_retains_quota(verified, monkeypatch):
    def broken(path):
        raise PermissionError("directory enumeration failed")
    monkeypatch.setattr(Path, "iterdir", broken)
    assert grade._lasso_zero_mention_inventory(rows()) is None


def test_unreadable_approved_candidate_cannot_hide_behind_source_path_fallback(verified, monkeypatch):
    voice, _ = verified
    (voice / "social_proof.lasso_ig.md").write_text("permission unknown")
    # Actual resolver returning default does not authorize treating this object
    # as absent. The helper independently examines per-account candidates.
    assert grade._lasso_zero_mention_inventory(rows()) is None


def test_dangling_source_symlink_is_unknown_not_absent(verified):
    voice, _ = verified
    (voice / "social_proof.lasso_fb.md").symlink_to(voice / "nonexistent")
    assert grade._lasso_zero_mention_inventory(rows()) is None


@pytest.mark.parametrize("change", [dict(gym_id="foreign"), dict(gym_id=None), dict(account="legacy")])
def test_mixed_or_unknown_scope_retains_quota(verified, change):
    book = rows(); book[0].update(change)
    assert grade._lasso_zero_mention_inventory(book) is None
    assert grade.grade_month(book, profile="B2B").scores["visual_match"] == 7


def test_gym_profile_and_direct_unknown_profile_do_not_receive_exemption(verified):
    result = grade.grade_month(rows(), profile="GYM")
    assert "client_mentions" not in result.exemption_evidence
    assert grade._proof_numbers(rows(), []) == 7


def test_numeric_and_mixed_claim_penalties_remain_with_zero_mention_exemption(verified):
    book = rows()
    for row in book:
        row["caption"] = "No numeric claim here. Book a call."
    defects = []
    assert grade._proof_numbers(book, defects, allow_lasso_inventory=True) == 7
    assert any("number" in d[2] for d in defects)
    for i, row in enumerate(book):
        row["caption"] = f"Synthetic check for {100 if i % 2 else 200} gyms. Book a call."
    defects = []
    assert grade._proof_numbers(book, defects, allow_lasso_inventory=True) == 12
    assert any("mixed gym_count" in d[2] for d in defects)
