"""
Comment/DM handling tests. NOTHING auto-sends. Tier 2 (price, hours, injury, refund,
negative, or any question) goes to a human; Tier 1 gets a held thank-you; a first-contact
DM is always surfaced. The flag is OFF by default (handlers hold while off).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import comments, db  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402


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


# ---- state-advance discipline: seen_key only after card delivered ------------

def _acct():
    return Account(key="lasso_ig", display_name="lasso_ig",
                   platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TI")


class _Comment:
    def __init__(self, comment_id, text):
        self.d = {"comment_id": comment_id, "text": text, "created": "2099-01-01T12:00:00"}

    def __getitem__(self, k):
        return self.d[k]

    def get(self, k, default=None):
        return self.d.get(k, default)


class ExplodingPoster:
    def post_notice(self, text):
        raise RuntimeError("Slack is down")


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)


def _kv_delete(key):
    """Remove a kv entry so tests start from a clean state."""
    try:
        with db._lock, db.connect() as conn:
            conn.execute("DELETE FROM kv WHERE key=?", (key,))
            conn.commit()
    except Exception:
        pass


def test_seen_key_unset_when_poster_raises(monkeypatch):
    """If the Slack post raises, comment_seen_X must NOT be stamped so the
    comment remains eligible on the next poll."""
    monkeypatch.setenv("AGENT_COMMENTS_ENABLED", "true")
    comment_id = "cid_explode_state_test_001"
    _kv_delete(f"comment_seen_{comment_id}")
    monkeypatch.setattr(comments, "fetch_recent_comments",
                        lambda account, http=None: [
                            _Comment(comment_id, "Love this!")
                        ])

    import pytest
    with pytest.raises(RuntimeError, match="Slack is down"):
        comments.process_comments(_acct(), poster=ExplodingPoster())

    assert db.kv_get(f"comment_seen_{comment_id}") == ""


def test_seen_key_set_after_successful_post(monkeypatch):
    """Successful post stamps the seen key so the comment is not re-served."""
    monkeypatch.setenv("AGENT_COMMENTS_ENABLED", "true")
    comment_id = "cid_success_state_test_002"
    _kv_delete(f"comment_seen_{comment_id}")
    monkeypatch.setattr(comments, "fetch_recent_comments",
                        lambda account, http=None: [
                            _Comment(comment_id, "Love this!")
                        ])

    poster = RecordingPoster()
    cards = comments.process_comments(_acct(), poster=poster)

    assert cards
    assert db.kv_get(f"comment_seen_{comment_id}") == "1"
    assert len(poster.notices) == 1


def test_seen_key_set_when_no_poster(monkeypatch):
    """When poster is None the card is in the return list; seen key still stamps
    (no retry needed since the card is in the return value)."""
    monkeypatch.setenv("AGENT_COMMENTS_ENABLED", "true")
    comment_id = "cid_noposter_state_test_003"
    _kv_delete(f"comment_seen_{comment_id}")
    monkeypatch.setattr(comments, "fetch_recent_comments",
                        lambda account, http=None: [
                            _Comment(comment_id, "Love this!")
                        ])

    cards = comments.process_comments(_acct(), poster=None)

    assert cards
    assert db.kv_get(f"comment_seen_{comment_id}") == "1"
