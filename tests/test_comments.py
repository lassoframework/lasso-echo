"""
Comment/DM handling tests. NOTHING auto-sends. Tier 2 (price, hours, injury, refund,
negative, or any question) goes to a human; Tier 1 gets a held thank-you; a first-contact
DM is always surfaced. The flag is OFF by default (handlers hold while off).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import comments  # noqa: E402


def _on(monkeypatch):
    monkeypatch.setenv("AGENT_COMMENTS_ENABLED", "true")


# ---- classification ---------------------------------------------------------
def test_classify_tier2_triggers():
    for text in [
        "How much is a membership?",       # question + price
        "What are your hours",             # hours
        "I tweaked my knee, can I still train?",  # injury + question
        "I want a refund please",          # refund
        "This place is a total scam",      # negative
        "Do you have parking",             # question start
    ]:
        assert comments.classify_comment(text) == "TIER2", text


def test_classify_tier1_simple_positive():
    for text in ["Love this!", "Great work team", "So inspiring", "🔥🔥"]:
        assert comments.classify_comment(text) == "TIER1", text


# ---- handle_comment: nothing auto-sends -------------------------------------
def test_tier1_drafts_held_thank_you(monkeypatch):
    _on(monkeypatch)
    r = comments.handle_comment("Love this!")
    assert r["tier"] == "TIER1"
    assert r["draft_reply"] == comments.THANK_YOU_TEMPLATE
    assert "thank" in r["action"].lower()
    assert r["auto_send"] is False


def test_tier2_surfaced_no_draft(monkeypatch):
    _on(monkeypatch)
    r = comments.handle_comment("How much does it cost?")
    assert r["tier"] == "TIER2"
    assert r["draft_reply"] == ""
    assert "surface" in r["action"].lower()
    assert r["auto_send"] is False


def test_nothing_ever_auto_sends(monkeypatch):
    _on(monkeypatch)
    for text in ["Love this!", "What are your prices?", "I want a refund"]:
        assert comments.handle_comment(text)["auto_send"] is False


# ---- DMs: first contact never auto-handled ----------------------------------
def test_handle_dm_first_contact_surfaced(monkeypatch):
    _on(monkeypatch)
    r = comments.handle_dm("hey are you open?", first_contact=True)
    assert r["auto_send"] is False
    assert r["first_contact"] is True
    assert "first contact" in r["action"].lower()


# ---- flag off: handlers hold, still never send ------------------------------
def test_flag_off_holds(monkeypatch):
    monkeypatch.delenv("AGENT_COMMENTS_ENABLED", raising=False)  # OFF
    c = comments.handle_comment("Love this!")
    assert c["auto_send"] is False and "disabled" in c["action"].lower()
    d = comments.handle_dm("hi", first_contact=True)
    assert d["auto_send"] is False and "disabled" in d["action"].lower()
