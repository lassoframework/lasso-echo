"""Tests for chat-publish routing, scoped by account ownership.

Covers the spec's audit checklist item-for-item:
  [x] "post it" on a LASSO account publishes and returns a permalink
  [x] "post it" aimed at a client account does NOT publish, drafts instead, says why
  [x] "make me a card about X" does NOT publish
  [x] Ambiguous phrasing triggers one question, never a publish
  [x] undo works within the window on LASSO accounts
  [x] A fabricated claim is blocked even on a direct publish request
Plus: only Blake, generation words never publish, flag OFF is inert, story targeting.
"""
import pytest

from agent import chat_publish as cp, config


BLAKE = config.APPROVER_SLACK_ID


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AGENT_CHAT_PUBLISH_ENABLED", "true")
    # reset the per-actor publish memory between tests
    cp._LAST_PUBLISH.clear()
    yield
    cp._LAST_PUBLISH.clear()


def _pub_ok(**over):
    calls = []

    def publish_fn(account_key, asset, surfaces):
        calls.append((account_key, surfaces))
        return {"permalink": "https://instagram.com/p/ABC123",
                "media_ids": ["m_1"], "cost": "$0.00", **over}
    publish_fn.calls = calls
    return publish_fn


def _draft_fn():
    calls = []

    def draft_fn(account_key, asset, surfaces):
        calls.append((account_key, surfaces))
        return {"draft_id": "d_1"}
    draft_fn.calls = calls
    return draft_fn


# ---- intent classification --------------------------------------------------

@pytest.mark.parametrize("text,intent", [
    ("post it", cp.PUBLISH),
    ("publish this to the feed", cp.PUBLISH),
    ("send it live now", cp.PUBLISH),
    ("post to stories", cp.PUBLISH),
    ("make me a card about our new class", cp.GENERATE),
    ("try a cream version", cp.GENERATE),
    ("show me a draft", cp.GENERATE),
    ("make a card and post it", cp.AMBIGUOUS),
    ("what's on the calendar today", cp.NONE),
    ("undo that", cp.UNDO),
])
def test_classify_intent(text, intent):
    assert cp.classify_intent(text) == intent


def test_target_surfaces_default_feed_story_optin():
    assert cp.target_surfaces("post it") == ["feed"]
    assert cp.target_surfaces("post to stories") == ["story"]
    assert cp.target_surfaces("post it to feed and stories") == ["feed", "story"]


# ---- the audit checklist ----------------------------------------------------

def test_post_it_on_lasso_publishes_and_returns_permalink():
    pf = _pub_ok()
    out = cp.route("post it", "lasso_ig", BLAKE, asset={"caption": "hi"},
                   publish_fn=pf, gate_fn=lambda a: {"ok": True})
    assert out.kind == "published"
    assert out.permalink == "https://instagram.com/p/ABC123"
    assert pf.calls == [("lasso_ig", ["feed"])]


def test_post_it_on_client_drafts_not_publishes():
    pf = _pub_ok()
    df = _draft_fn()
    out = cp.route("post it", "district_h_ig", BLAKE, asset={"caption": "hi"},
                   publish_fn=pf, draft_fn=df, gate_fn=lambda a: {"ok": True})
    assert out.kind == "drafted_for_client"
    assert out.draft_id == "d_1"
    assert pf.calls == []                # never published to a client
    assert df.calls == [("district_h_ig", ["feed"])]
    assert "client account" in out.message and "pending" in out.message


def test_make_me_a_card_does_not_publish():
    pf = _pub_ok()
    out = cp.route("make me a card about X", "lasso_ig", BLAKE, asset={},
                   publish_fn=pf)
    assert out.kind == "not_a_command"
    assert pf.calls == []


def test_ambiguous_triggers_one_question_never_publishes():
    pf = _pub_ok()
    out = cp.route("make a card and post it", "lasso_ig", BLAKE, asset={},
                   publish_fn=pf)
    assert out.kind == "ask"
    assert pf.calls == []


def test_fabricated_claim_blocked_on_direct_publish():
    pf = _pub_ok()
    out = cp.route("post it", "lasso_ig", BLAKE, asset={"caption": "we have 900 members"},
                   publish_fn=pf,
                   gate_fn=lambda a: {"ok": False, "reason": "unverified stat: 900 members"})
    assert out.kind == "blocked"
    assert "900 members" in out.message
    assert pf.calls == []                # gate stops the publish


def test_undo_within_window_on_lasso():
    deleted = []
    pf = _pub_ok()
    cp.route("post it", "lasso_ig", BLAKE, asset={}, publish_fn=pf,
             gate_fn=lambda a: {"ok": True}, now=1000.0)
    out = cp.route("undo that", "lasso_ig", BLAKE,
                   delete_fn=lambda ak, mids: deleted.append((ak, mids)) or True,
                   now=1000.0 + 120)   # 2 min later, inside the 5 min window
    assert out.kind == "undone"
    assert deleted == [("lasso_ig", ["m_1"])]


def test_undo_after_window_says_manual():
    pf = _pub_ok()
    cp.route("post it", "lasso_ig", BLAKE, asset={}, publish_fn=pf,
             gate_fn=lambda a: {"ok": True}, now=1000.0)
    out = cp.route("undo that", "lasso_ig", BLAKE,
                   delete_fn=lambda ak, mids: True,
                   now=1000.0 + 600)   # 10 min later, past the window
    assert out.kind == "undo_expired" and "manually" in out.message


# ---- still-holds guards -----------------------------------------------------

def test_only_blake_can_trigger():
    pf = _pub_ok()
    out = cp.route("post it", "lasso_ig", "U_SOMEONE_ELSE", asset={}, publish_fn=pf)
    assert out.kind == "denied"
    assert pf.calls == []


def test_flag_off_is_inert(monkeypatch):
    monkeypatch.setenv("AGENT_CHAT_PUBLISH_ENABLED", "false")
    pf = _pub_ok()
    out = cp.route("post it", "lasso_ig", BLAKE, asset={}, publish_fn=pf)
    assert out.kind == "disabled"
    assert pf.calls == []


def test_generation_word_alone_never_publishes():
    pf = _pub_ok()
    for msg in ("draft a welcome for oak strength", "whip up a story", "design a card"):
        out = cp.route(msg, "lasso_ig", BLAKE, asset={}, publish_fn=pf)
        assert out.kind == "not_a_command"
    assert pf.calls == []


def test_post_to_stories_publishes_story_surface():
    pf = _pub_ok()
    out = cp.route("post to stories", "lasso_ig", BLAKE, asset={}, publish_fn=pf,
                   gate_fn=lambda a: {"ok": True})
    assert out.kind == "published" and out.surfaces == ["story"]
    assert pf.calls == [("lasso_ig", ["story"])]


def test_ownership_mapping():
    assert cp.ownership("lasso_ig") == "lasso"
    assert cp.ownership("lasso_fb") == "lasso"
    assert cp.ownership("district_h_ig") == "client"


def test_cost_reported_in_publish_message():
    pf = _pub_ok(cost="$0.03")
    out = cp.route("post it", "lasso_ig", BLAKE, asset={}, publish_fn=pf,
                   gate_fn=lambda a: {"ok": True})
    assert "0.03" in out.message


def test_publish_that_raises_degrades_to_blocked_not_crash():
    # regression (audit MAJOR): blake_personal / a Graph error must not escape
    def boom(account_key, asset, surfaces):
        raise RuntimeError("Graph API cannot publish to a personal profile")
    out = cp.route("post it", "blake_personal", BLAKE, asset={}, publish_fn=boom,
                   gate_fn=lambda a: {"ok": True})
    assert out.kind == "blocked" and "personal profile" in out.message


def test_publish_error_result_is_blocked():
    def errs(account_key, asset, surfaces):
        return {"error": "no token set"}
    out = cp.route("post it", "lasso_ig", BLAKE, asset={}, publish_fn=errs,
                   gate_fn=lambda a: {"ok": True})
    assert out.kind == "blocked" and "no token" in out.message


def test_draft_only_mode_is_honest_not_published():
    # regression (audit MINOR): publish flag off -> would_publish, not "published"
    def draft_only(account_key, asset, surfaces):
        return {"permalink": "", "media_ids": [], "mode": "would_publish"}
    out = cp.route("post it", "lasso_ig", BLAKE, asset={}, publish_fn=draft_only,
                   gate_fn=lambda a: {"ok": True})
    assert out.kind == "would_publish" and "not armed" in out.message


def test_meta_delete_media_is_noop_when_publish_disabled(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)
    from agent import meta_publisher
    assert meta_publisher.delete_media("lasso_fb", "123") is False


# ---- live integration glue --------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("post it", "lasso_ig"),
    ("publish to facebook", "lasso_fb"),
    ("post it to the page", "lasso_fb"),
    ("publish this to district_h_ig", "district_h_ig"),
])
def test_resolve_account_key(text, expected):
    assert cp.resolve_account_key(text) == expected


class _FakeStore:
    def __init__(self, drafts):
        self._d = drafts

    def list_pending(self):
        return self._d


class _D:
    def __init__(self, draft_id, account_key, day_key="2026-08-01", caption=""):
        self.draft_id = draft_id
        self.account_key = account_key
        self.day_key = day_key
        self.caption = caption


def test_latest_pending_picks_most_recent_for_account():
    store = _FakeStore([_D("a", "lasso_ig", "2026-08-01"),
                        _D("b", "lasso_ig", "2026-08-03"),
                        _D("c", "lasso_fb", "2026-08-05")])
    assert cp._latest_pending(store, "lasso_ig").draft_id == "b"
    assert cp._latest_pending(store, "lasso_ig") is not None
    assert cp._latest_pending(_FakeStore([]), "lasso_ig") is None


def test_handle_message_disabled_when_flag_off(monkeypatch):
    monkeypatch.setenv("AGENT_CHAT_PUBLISH_ENABLED", "false")
    out = cp.handle_message("post it", BLAKE, store=_FakeStore([]))
    assert out.kind == "disabled"


def test_handle_message_generation_is_not_a_command():
    out = cp.handle_message("make me a welcome card", BLAKE, store=_FakeStore([]))
    assert out.kind == "not_a_command"


def test_handle_message_client_account_drafts_not_publishes():
    # a client account never publishes; the real draft_fn keeps it pending
    d = _D("d1", "district_h_ig", "2026-08-04", caption="clean copy")
    out = cp.handle_message("publish this to district_h_ig", BLAKE,
                            store=_FakeStore([d]), poster=None)
    assert out.kind == "drafted_for_client" and out.draft_id == "d1"


def test_real_gate_blocks_dash_or_vendor_in_caption(monkeypatch):
    # bypass the fabrication scan (return clean) so we isolate the copy scan
    import agent.fabrication_scan as fs
    monkeypatch.setattr(fs, "scan", lambda **kw: {"blocked": []})
    gate = cp._real_gate_fn(store=_FakeStore([]))
    bad = _D("x", "lasso_ig", caption="best gym in town - join now")
    assert gate(bad)["ok"] is False
    good = _D("y", "lasso_ig", caption="best gym in town, join now")
    assert gate(good)["ok"] is True
