"""
Two-tier comment engine tests (the held-card pass). Offline, mocked Graph.
Asserts: conservative classification (uncertain = Tier 2); the Tier 1 card
queues like + templated thank-you HELD; Tier 2 cards are labeled TIER 2; DMs
are structurally untouchable (no conversations read exists, handle_dm always
surfaces to a human, first contact never automated); re-polls stay quiet;
everything inert while AGENT_COMMENTS_ENABLED is OFF.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import comments, db  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402


class FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeGraph:
    def __init__(self, comments_by_media):
        self.comments_by_media = comments_by_media
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append(url)
        media_id = url.rstrip("/").split("/")[-2]
        return FakeResp({"data": self.comments_by_media.get(media_id, [])})


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _acct(monkeypatch):
    monkeypatch.setenv("CE_TOKEN", "tok-ce")
    return Account(key="lasso_ig", display_name="IG", platform=Platform.INSTAGRAM,
                   token_env="CE_TOKEN", target_id_env="CE_ID")


def _seed_post(media_id="M1"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at) VALUES ('d','lasso_ig','instagram','c',?,"
            "'published','2026-07-02T10:00:00')", (media_id,))
        conn.commit()


# ---- conservative classification ----------------------------------------------
def test_uncertain_is_tier2():
    for text in ("Interesting.", "Saw this yesterday", "hm", "First",
                 "my buddy goes here"):
        assert comments.classify_comment(text) == "TIER2", text


def test_spec_keywords_are_tier2():
    for text in ("what are your hours", "price?", "my knee hurts after class",
                 "I want a refund", "this place is bad"):
        assert comments.classify_comment(text) == "TIER2", text


# ---- the held-card pass -----------------------------------------------------------
def test_process_comments_holds_both_tiers(monkeypatch):
    monkeypatch.setenv("AGENT_COMMENTS_ENABLED", "true")
    acct = _acct(monkeypatch)
    _seed_post("M1")
    graph = FakeGraph({"M1": [
        {"id": "c1", "text": "Love this! 🔥"},
        {"id": "c2", "text": "How much is a membership?"},
    ]})
    poster = RecordingPoster()
    cards = comments.process_comments(acct, http=graph, poster=poster)
    assert len(cards) == 2
    t1 = next(c for c in cards if "TIER 1" in c)
    t2 = next(c for c in cards if "TIER 2" in c)
    assert "HELD" in t1 and "like + reply" in t1 and "Nothing sent" in t1
    assert "HELD" in t2 and "human reply" in t2          # clearly labeled TIER 2
    assert poster.notices == cards
    # audit carries the tier distinction
    kinds = [(r["subject"], r["reason"]) for r in db.audit_rows()
             if r["kind"] == "comment"]
    assert ("c1", "TIER1 held for approval") in kinds
    assert ("c2", "TIER2 held for approval") in kinds
    # a re-poll is quiet (seen markers)
    assert comments.process_comments(acct, http=graph, poster=poster) == []


def test_inert_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_COMMENTS_ENABLED", raising=False)
    acct = _acct(monkeypatch)
    _seed_post("M1")

    class Exploding:
        def get(self, *a, **k):
            raise AssertionError("Graph read while OFF")

    assert comments.process_comments(acct, http=Exploding()) is None
    assert comments.fetch_recent_comments(acct, http=Exploding()) == []


# ---- DMs: structurally untouchable ------------------------------------------------
def test_dms_never_touched(monkeypatch):
    """First contact with a person is never automated. Enforced two ways: the
    module contains NO conversations/DM read anywhere, and handle_dm always
    surfaces to a human with auto_send False."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "agent", "comments.py"), encoding="utf-8").read()
    for endpoint in ("conversations", "/messages", "message_", "inbox"):
        assert endpoint not in src, f"DM surface {endpoint!r} must not exist"
    monkeypatch.setenv("AGENT_COMMENTS_ENABLED", "true")
    out = comments.handle_dm("hey, interested in joining", first_contact=True)
    assert out["auto_send"] is False
    assert "never auto-handled" in out["action"]
    # even a non-first-contact DM only ever surfaces to a human
    out2 = comments.handle_dm("thanks!", first_contact=False)
    assert out2["auto_send"] is False and "human" in out2["action"]


def test_graph_read_uses_comment_edge_only(monkeypatch):
    monkeypatch.setenv("AGENT_COMMENTS_ENABLED", "true")
    acct = _acct(monkeypatch)
    _seed_post("M9")
    graph = FakeGraph({"M9": []})
    comments.fetch_recent_comments(acct, http=graph)
    assert all(u.endswith("/comments") for u in graph.requests)
