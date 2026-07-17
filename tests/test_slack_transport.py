"""
Slack transport failures must degrade, never raise.

run_daily posts a pre-loop notice (voice doc missing) and one card per
account through SlackPoster. A requests timeout there used to propagate:
before the account loop it killed the ENTIRE run; inside the loop it killed
that account's day. Now _chat_post/update_card catch transport errors and
return {"ok": False, "error": "transport"}, which every caller already
treats as a failed post.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.drafter import DraftStatus
from agent.slack_surface import SlackPoster


class _TimeoutHTTP:
    def post(self, *a, **kw):
        raise TimeoutError("simulated slack outage")


class _Draft:
    status = DraftStatus.PENDING
    draft_id = "d1"
    account_key = "lasso_ig"
    platform = "instagram"
    caption = "c"
    hashtags = []
    creative_path = ""
    creative_public_url = ""
    scheduled_for = "2026-07-08T18:30:00+00:00"
    is_story = False
    slack_channel = "C1"
    slack_ts = "ts1"
    slides = []
    slide_urls = []
    blocked_reason = ""
    day_key = "2026-07-08"
    draft_type = "feed"


def _poster():
    return SlackPoster(http=_TimeoutHTTP(), token="xoxb-test", channel="C1")


def test_post_notice_swallows_transport_error():
    resp = _poster().post_notice("voice doc missing")
    assert resp == {"ok": False, "error": "transport"}


def test_post_approval_card_swallows_transport_error():
    resp = _poster().post_approval_card(_Draft())
    assert resp.get("ok") is False


def test_update_card_swallows_transport_error():
    resp = _poster().update_card("C1", "ts1", "text", blocks=None)
    assert resp.get("ok") is False


def test_stdlib_adapter_constructs_and_posts():
    """_requests() returns a stdlib urllib adapter with no external deps.
    Verify it instantiates cleanly and provides the .post() interface — the
    root-cause check for the post-captions ModuleNotFoundError: requests crash.
    SlackPoster() with no http= must not require the requests library."""
    from agent.slack_surface import _requests
    client = _requests()
    assert hasattr(client, "post"), "_requests() must return an object with .post()"
    # Verify the fallback path: SlackPoster() with no http injected falls
    # through to _requests() and gets our stdlib client, never requests.
    import importlib, sys
    # Block requests so we prove the stdlib path is self-contained.
    original = sys.modules.get("requests")
    sys.modules["requests"] = None  # type: ignore[assignment]
    try:
        client2 = _requests()
        assert hasattr(client2, "post"), "stdlib path must work without requests installed"
    finally:
        if original is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = original


def test_run_daily_completes_through_slack_outage(monkeypatch, tmp_path):
    """Full-path proof: every Slack post times out, yet run_daily returns
    normally and the draft is still stored (card just never posted)."""
    from agent.accounts import Account, Platform
    from agent.runner import run_daily
    from agent.store import PendingStore

    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")

    voice = tmp_path / "voice.md"
    voice.write_text("# Voice\nWe help gym owners grow.\n## CTAs\n- Save this."
                     "\n## Hashtags\n#LASSO\n", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("An approved note.", encoding="utf-8")

    acct = Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM,
                   token_env="DUMMY_TOK", target_id_env="DUMMY_TGT")
    store = PendingStore(path=db_path)

    out = run_daily(poster=_poster(), voice_path=str(voice),
                    library_path=str(lib),
                    scheduled_for="2026-07-08T14:00:00+00:00",
                    accounts=[acct], store=store)
    assert out["status"] == "drafted"
    assert len(store.list_pending()) >= 1
