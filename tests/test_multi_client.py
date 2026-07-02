"""
Multi-client foundation tests. Two fake clients resolve fully ISOLATED configs
(voice doc, social proof doc, library, channel, approvers) and can never cross-read
each other's docs or libraries. Backward compatibility: an account with empty
multi-client fields (client zero, the LASSO accounts) resolves to the exact global
config values, so existing behavior is unchanged.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.accounts import Account, Platform, get_account  # noqa: E402
from agent.drafter import draft_post  # noqa: E402
from agent.library import pick_next  # noqa: E402
from agent.trust import TrustLevel  # noqa: E402
from agent.voice import load_voice  # noqa: E402


def _client(key, base):
    return Account(
        key=key, display_name=key, platform=Platform.INSTAGRAM,
        token_env=f"T_{key.upper()}", target_id_env=f"I_{key.upper()}",
        voice_doc=str(base / "voice.md"),
        social_proof_doc=str(base / "social_proof.md"),
        library_prefix=str(base / "library"),
        slack_channel=f"C_{key.upper()}",
        approvers=[f"U_{key.upper()}"],
    )


def _stand_up(base, cta):
    base.mkdir()
    (base / "voice.md").write_text(
        f"We help {base.name} gyms grow.\n### CTA rotation\n- {cta}\n#Tag{base.name}",
        encoding="utf-8")
    (base / "social_proof.md").write_text("## Entry\nQuote: q\nPermission: yes\nVerified: 2026-07-01\n",
                                          encoding="utf-8")
    lib = base / "library"
    lib.mkdir()
    (lib / "card.jpg").write_bytes(b"img")
    (lib / "card.txt").write_text(f"{base.name} note", encoding="utf-8")


# ---- two clients resolve isolated configs -------------------------------------
def test_two_clients_resolve_isolated_configs(tmp_path):
    a_base, b_base = tmp_path / "clienta", tmp_path / "clientb"
    _stand_up(a_base, "Save this for later.")
    _stand_up(b_base, "Send this to a friend.")
    a, b = _client("client_a", a_base), _client("client_b", b_base)

    assert a.voice_doc_path() != b.voice_doc_path()
    assert a.social_proof_doc_path() != b.social_proof_doc_path()
    assert a.library_path() != b.library_path()
    assert a.approval_channel() == "C_CLIENT_A" and b.approval_channel() == "C_CLIENT_B"
    assert a.approver_ids() == ["U_CLIENT_A"] and b.approver_ids() == ["U_CLIENT_B"]
    # every resolved path stays inside the client's own directory
    for p in (a.voice_doc_path(), a.social_proof_doc_path(), a.library_path()):
        assert str(a_base) in p and str(b_base) not in p


def test_clients_never_cross_read_docs_or_libraries(tmp_path):
    a_base, b_base = tmp_path / "clienta", tmp_path / "clientb"
    _stand_up(a_base, "Save this for later.")
    _stand_up(b_base, "Send this to a friend.")
    a, b = _client("client_a", a_base), _client("client_b", b_base)

    # drafting for A uses A's voice + A's library note; B's text never appears
    voice_a = load_voice(a.voice_doc_path())
    creative_a = pick_next(a, a.library_path(), set())
    d = draft_post(a, creative_a, "2026-07-02T12:00", voice=voice_a)
    assert "clienta note" in d.caption
    assert "clientb" not in d.caption.lower()
    # and B's draft carries B's, never A's
    voice_b = load_voice(b.voice_doc_path())
    creative_b = pick_next(b, b.library_path(), set())
    db = draft_post(b, creative_b, "2026-07-02T12:00", voice=voice_b)
    assert "clientb note" in db.caption
    assert "clienta" not in db.caption.lower()


# ---- backward compatibility: empty fields = the exact global config ------------
def test_empty_fields_fall_back_to_global_config():
    lasso = get_account("lasso_ig")
    assert lasso.voice_doc_path() == config.VOICE_DOC_PATH
    assert lasso.social_proof_doc_path() == config.SOCIAL_PROOF_PATH
    assert lasso.library_path() == config.LIBRARY_PATH
    assert lasso.approval_channel() == config.SLACK_CHANNEL_ID
    assert lasso.approver_ids() == [config.APPROVER_SLACK_ID]


def test_trust_level_defaults_to_full_approval(tmp_path):
    c = _client("client_c", tmp_path / "c")
    assert c.trust_level == TrustLevel.FULL_APPROVAL  # every new client starts gated
