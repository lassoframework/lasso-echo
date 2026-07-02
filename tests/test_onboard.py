"""
add-client scaffold tests: contents, idempotency (nothing destructive on
re-run), the printed checklist + config entry, and key validation.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import onboard  # noqa: E402


def test_scaffold_contents(tmp_path, capsys):
    out = onboard.add_client("iron_path", "Iron Path", root=str(tmp_path))
    assert len(out["created"]) == 3
    voice = (tmp_path / "brand_voice" / "iron_path" / "lasso_voice.md").read_text()
    assert "Iron Path Brand Bible" in voice
    assert voice.count("TODO") >= 5                     # nothing pre-filled
    proof = (tmp_path / "brand_voice" / "iron_path" / "social_proof.md").read_text()
    assert "Permission: yes" in proof                    # the rule header
    assert (tmp_path / "content_library" / "iron_path" / ".gitkeep").exists()
    printed = capsys.readouterr().out
    assert 'key="iron_path_ig"' in printed               # the config entry
    assert "trust defaults to FULL_APPROVAL" in printed
    assert "BY-HAND CHECKLIST" in printed
    for step in ("capture-baseline", "check-tokens", "Shadow week",
                 "STAGE2_RUNBOOK.md", "Railway env"):
        assert step in printed


def test_idempotent_rerun_never_destroys(tmp_path):
    onboard.add_client("iron_path", "Iron Path", root=str(tmp_path))
    voice_path = tmp_path / "brand_voice" / "iron_path" / "lasso_voice.md"
    voice_path.write_text("HAND EDITED CONTENT", encoding="utf-8")
    out2 = onboard.add_client("iron_path", "Iron Path", root=str(tmp_path))
    assert out2["created"] == []                         # nothing rewritten
    assert len(out2["skipped"]) == 3
    assert voice_path.read_text() == "HAND EDITED CONTENT"


def test_key_validation(tmp_path, capsys):
    for bad in ("", "9start", "UPPER", "has-dash", "a", "x" * 40, "sp ace"):
        assert onboard.add_client(bad, "X", root=str(tmp_path)) is None, bad
    assert "invalid key" in capsys.readouterr().out
    assert not (tmp_path / "brand_voice").exists()       # nothing created
