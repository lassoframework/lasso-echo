"""
Idempotent daily drafts + supersede + expiry tests. Fully OFFLINE: fake poster,
real PendingStore on tmp files, no network. Asserts: the flag defaults OFF and
OFF means exactly today's behavior (a re-run re-cards, no expiry sweep); ON, a
same-content re-run returns the existing PENDING draft with no new card; changed
content SUPERSEDES the old record and edits its card in place; a pending draft
whose day has passed flips to EXPIRED with its card edited in place; and a stale
approve on a superseded or expired draft is a friendly no-op that never publishes.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import approvals, config  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.runner import run_daily  # noqa: E402
from agent.slack_surface import (SlackPoster, build_expired_blocks,  # noqa: E402
                                 build_superseded_blocks)
from agent.store import PendingStore  # noqa: E402

DAY = "2027-07-07"   # Wednesday: a posting day under the default cadence
DAY2 = "2027-07-08"  # Thursday: also a posting day

VOICE_V1 = "We help gym owners grow.\n\n## Hashtags\n#LASSOFramework"
VOICE_V2 = "We help gym owners grow.\n\n## Hashtags\n#LASSOFramework #SpeedToLead"


class FakePoster:
    """Records cards, notices, and in-place card edits. Never touches Slack."""

    def __init__(self):
        self.cards = []
        self.notices = []
        self.superseded = []
        self.expired = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True, "channel": "C1", "ts": f"ts{len(self.cards)}"}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def mark_superseded(self, draft):
        self.superseded.append(draft)
        return {"ok": True}

    def mark_expired(self, draft):
        self.expired.append(draft)
        return {"ok": True}


class ExplodingPublisher:
    def publish(self, draft, account):
        raise AssertionError("publish was called; the stale-draft gate failed")


def _acct():
    # A non-LASSO key so the run exercises the plain library path only.
    return Account(key="gym_ig", display_name="Gym IG", platform=Platform.INSTAGRAM,
                   token_env="IDEM_TEST_TOKEN", target_id_env="IDEM_TEST_TARGET")


def _setup_env(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    for f in ("AGENT_PUBLISH_ENABLED", "AGENT_STORIES_ENABLED",
              "AGENT_CONTENT_BRAIN_ENABLED", "AGENT_NANO_ENABLED",
              "AGENT_HOSTING_ENABLED", "AGENT_OPS_ALERTS_ENABLED"):
        monkeypatch.delenv(f, raising=False)


def _run(tmp_path, poster, store, day=DAY, voice_text=VOICE_V1):
    voice = tmp_path / "voice.md"
    voice.write_text(voice_text, encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("A plain approved note.", encoding="utf-8")
    return run_daily(poster=poster, voice_path=str(voice), library_path=str(lib),
                     scheduled_for=f"{day}T18:30:00+00:00",
                     accounts=[_acct()], store=store)


def _store(tmp_path):
    return PendingStore(path=str(tmp_path / "pending.json"))


# ---- 1. the flag defaults OFF -------------------------------------------------
def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", raising=False)
    assert config.idempotent_drafts_enabled() is False


# ---- 2. flag OFF -> exactly today's behavior: a re-run re-cards ---------------
def test_flag_off_rerun_posts_duplicate_card(monkeypatch, tmp_path):
    _setup_env(monkeypatch)
    monkeypatch.delenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", raising=False)
    poster, store = FakePoster(), _store(tmp_path)
    _run(tmp_path, poster, store)
    _run(tmp_path, poster, store)
    assert len(poster.cards) == 2                 # old behavior: duplicate card
    assert poster.cards[0].day_key == ""          # identity fields stay empty
    assert poster.cards[0].slack_ts == ""
    assert poster.superseded == [] and poster.expired == []


# ---- 3. flag ON: same-content re-run returns existing, no new card ------------
def test_flag_on_same_content_rerun_returns_existing(monkeypatch, tmp_path):
    _setup_env(monkeypatch)
    monkeypatch.setenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", "true")
    poster, store = FakePoster(), _store(tmp_path)

    first = _run(tmp_path, poster, store)
    assert len(poster.cards) == 1
    d1 = first["drafts"][0]
    assert d1.day_key == DAY and d1.draft_type == "feed"
    assert d1.slack_channel == "C1" and d1.slack_ts == "ts1"  # card ref captured

    second = _run(tmp_path, poster, store)
    assert len(poster.cards) == 1                 # no duplicate card
    d2 = second["drafts"][0]
    assert d2.draft_id == d1.draft_id             # the existing draft IS the result
    assert d2.status == DraftStatus.PENDING
    assert len(store.list_pending()) == 1         # one record, not two


# ---- 4. flag ON: changed content supersedes the old record --------------------
def test_flag_on_changed_content_supersedes(monkeypatch, tmp_path):
    _setup_env(monkeypatch)
    monkeypatch.setenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", "true")
    poster, store = FakePoster(), _store(tmp_path)

    first = _run(tmp_path, poster, store, voice_text=VOICE_V1)
    old = first["drafts"][0]
    second = _run(tmp_path, poster, store, voice_text=VOICE_V2)
    new = second["drafts"][0]

    # the old record flipped to SUPERSEDED and its card was edited in place
    assert [d.draft_id for d in poster.superseded] == [old.draft_id]
    assert store.get(old.draft_id).status == DraftStatus.SUPERSEDED
    # exactly one NEW card was posted, under a distinct draft id
    assert len(poster.cards) == 2
    assert new.draft_id != old.draft_id
    assert new.status == DraftStatus.PENDING
    # only the new draft is still pending for (account, day, feed)
    pending = store.list_pending()
    assert [d.draft_id for d in pending] == [new.draft_id]


# ---- 5. expiry: a pending draft whose day has passed flips EXPIRED ------------
def test_flag_on_expires_stale_pending_drafts(monkeypatch, tmp_path):
    _setup_env(monkeypatch)
    monkeypatch.setenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", "true")
    poster, store = FakePoster(), _store(tmp_path)

    first = _run(tmp_path, poster, store, day=DAY)
    old = first["drafts"][0]
    second = _run(tmp_path, poster, store, day=DAY2)
    new = second["drafts"][0]

    # yesterday's card was expired in place, today's card posted fresh
    assert [d.draft_id for d in poster.expired] == [old.draft_id]
    assert store.get(old.draft_id).status == DraftStatus.EXPIRED
    assert new.day_key == DAY2 and new.status == DraftStatus.PENDING
    assert len(poster.cards) == 2
    assert [d.draft_id for d in store.list_pending()] == [new.draft_id]


def test_expiry_sweep_is_flagless_now(monkeypatch, tmp_path):
    """CHANGED 2026-07-04 (queue triage): past-due PENDING cards self-expire
    even with the idempotency flag OFF. Retroactive zombie-queue kill."""
    monkeypatch.delenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", raising=False)
    store = _store(tmp_path)
    stale = Draft(draft_id="old1", account_key="lasso_ig", platform="instagram",
                  caption="stale", hashtags=[], creative_path="a.png",
                  creative_public_url="", scheduled_for="2026-07-01T18:30:00+00:00",
                  status=DraftStatus.PENDING, day_key="2026-07-01",
                  draft_type="feed")
    store.put(stale)
    from agent.runner import expire_past_due
    poster = FakePoster()
    out = expire_past_due(store, poster)
    assert [d.draft_id for d in out] == ["old1"]
    assert store.get("old1").status == DraftStatus.EXPIRED
    assert store.list_pending() == []                     # dropped from the queue

def _stale_draft(status):
    return Draft(draft_id="stale1", account_key="gym_ig", platform="instagram",
                 caption="x", hashtags=[], creative_path="a.png",
                 creative_public_url="", scheduled_for=f"{DAY}T18:30:00+00:00",
                 status=status, day_key=DAY, draft_type="feed")


def test_stale_approve_on_superseded_is_friendly_noop():
    res = approvals.handle_action(
        "approve", _stale_draft(DraftStatus.SUPERSEDED), config.APPROVER_SLACK_ID,
        publisher=ExplodingPublisher(), account=_acct())
    assert res.ok is False
    assert "superseded" in res.detail.lower()
    assert "newest card" in res.detail.lower()


def test_stale_approve_on_expired_is_friendly_noop():
    res = approvals.handle_action(
        "approve", _stale_draft(DraftStatus.EXPIRED), config.APPROVER_SLACK_ID,
        publisher=ExplodingPublisher(), account=_acct())
    assert res.ok is False
    assert "expired" in res.detail.lower()
    assert "today's card" in res.detail.lower()


# ---- 7. the card edit itself: chat.update in place, buttons removed -----------
class RecordingHTTP:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append((url, json.loads(data)))

        class R:
            def json(self):
                return {"ok": True}

        return R()


def _carded_draft():
    return Draft(draft_id="card1", account_key="gym_ig", platform="instagram",
                 caption="x", hashtags=[], creative_path="a.png",
                 creative_public_url="", scheduled_for=f"{DAY}T18:30:00+00:00",
                 day_key=DAY, draft_type="feed",
                 slack_channel="C1", slack_ts="123.456")


def test_mark_superseded_edits_card_in_place_no_buttons():
    http = RecordingHTTP()
    poster = SlackPoster(http=http, token="fake", channel="C1")
    poster.mark_superseded(_carded_draft())
    url, payload = http.calls[0]
    assert url.endswith("chat.update")            # an EDIT, not a new message
    assert payload["channel"] == "C1" and payload["ts"] == "123.456"
    assert "SUPERSEDED" in payload["blocks"][0]["text"]["text"]
    assert all(b["type"] != "actions" for b in payload["blocks"])  # buttons gone


def test_mark_expired_edits_card_in_place_no_buttons():
    http = RecordingHTTP()
    poster = SlackPoster(http=http, token="fake", channel="C1")
    poster.mark_expired(_carded_draft())
    url, payload = http.calls[0]
    assert url.endswith("chat.update")
    assert "EXPIRED" in payload["blocks"][0]["text"]["text"]
    assert all(b["type"] != "actions" for b in payload["blocks"])


def test_update_card_without_message_ref_is_safe_noop():
    http = RecordingHTTP()
    poster = SlackPoster(http=http, token="fake", channel="C1")
    res = poster.update_card("C1", "", "text", [])
    assert res == {"ok": False, "error": "no_message_ref"}
    assert http.calls == []                       # no network attempt at all


def test_superseded_and_expired_blocks_carry_no_buttons():
    d = _carded_draft()
    for blocks in (build_superseded_blocks(d), build_expired_blocks(d)):
        assert all(b["type"] != "actions" for b in blocks)
