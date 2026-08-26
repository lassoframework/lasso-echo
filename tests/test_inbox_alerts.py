"""Reply-needed coach alerts (agent/inbox_alerts.py, flag AGENT_INBOX_ALERTS).

Everything offline via injected fakes. The spam fixtures include the REAL
homoglyph spam seen live on topfuel 2026-08-25 ("srу ur dms arе ϲӏoѕеd ... hit
hеr uр οn snap ..." — mixed Cyrillic/Greek lookalikes), so the classifier is
pinned against the exact evasion the wild actually uses.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import inbox_alerts  # noqa: E402
from agent.inbox_alerts import (  # noqa: E402
    MAX_ITEMS_PER_CARD, build_card, classify, needs_reply, run, sweep_gym,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

# The live topfuel spam comment, verbatim (Cyrillic/Greek homoglyphs included).
LIVE_HOMOGLYPH_SPAM = ("srу ur dms arе ϲӏoѕеd sо іm sаyіng іt hеrе my frіеnd "
                       "just mоvеd hеrе аnd knοԝs nobοdy. hit hеr uр οn snap "
                       "zoeyyawindrf і havе а fееlіng u two might сlick")


# ---- classifier -------------------------------------------------------------------

def test_spam_classifier_catches_known_patterns():
    spam = [
        LIVE_HOMOGLYPH_SPAM,
        "hit her up on snap katie123",
        "I made $5000 in crypto last week, DM me for details",
        "check my page for free onlyfans content",
        "earn daily with our bitcoin investment plan",
        "message me on telegram @dealz",
        "click the link to get free followers",
    ]
    for text in spam:
        assert classify(text) == "spam", text


def test_genuine_member_comments_pass():
    genuine = [
        "Kinda chic being a member at Top Fuel",
        "I mow your grass",
        "❤️the big dawgs!\U0001f44f\U0001f44f",
        "I went to Top Fuel for five years, from age 50-55. I always felt "
        "encouraged, supported, and befriended by everyone there.",
        "What time is the Saturday class?",
        "Do you have snap fitness style day passes?",  # 'snap' inside a real question
    ]
    for text in genuine:
        assert classify(text) == "member_comment", text


def test_neutral_emoji_only_and_friend_tags():
    assert classify("\U0001f525\U0001f525\U0001f525") == "neutral"
    assert classify("❤️") == "neutral"
    assert classify("@ciesielskialexa") == "neutral"
    assert classify("@one @two") == "neutral"
    assert classify("@rushelle_miller \U0001f440") == "neutral"  # live topfuel shape
    assert classify("") == "neutral"
    assert classify(None) == "neutral"


def test_needs_reply_logic():
    fresh = {"from": {"isOwner": False}, "isHidden": False, "replies": []}
    assert needs_reply(fresh)
    assert not needs_reply({"from": {"isOwner": True}})
    assert not needs_reply({"from": {"isOwner": False}, "isHidden": True})
    answered = {"from": {"isOwner": False},
                "replies": [{"from": {"isOwner": True}, "message": "thanks!"}]}
    assert not needs_reply(answered)
    # A reply from another NON-owner does not count as handled.
    piled_on = {"from": {"isOwner": False},
                "replies": [{"from": {"isOwner": False}, "message": "same!"}]}
    assert needs_reply(piled_on)


# ---- fakes -------------------------------------------------------------------------

def _post(pid, account_id="acct1", count=1, created="2026-08-26T09:00:00.000Z"):
    return {"id": pid, "accountId": account_id, "platform": "instagram",
            "content": "post caption", "createdTime": created,
            "permalink": f"https://instagram.com/p/{pid}/", "commentCount": count}


def _comment(text, created="2026-08-26T09:30:00+0000", owner=False, url=None):
    return {"id": "c1", "message": text, "createdTime": created,
            "from": {"name": "someone", "isOwner": owner},
            "replies": [], "isHidden": False, "url": url}


class FakeZernio:
    """profiles: {gym_id: profile_id}; threads: {post_id: [comments]}."""

    def __init__(self, profiles, posts=None, threads=None, mentions=None,
                 reviews=None, broken=()):
        self.profiles = profiles
        self.posts = posts or {}
        self.threads = threads or {}
        self.mentions = mentions or {}
        self.reviews = reviews or {}
        self.broken = set(broken)  # profile_ids whose reads raise

    def find_profile_id(self, name):
        return self.profiles.get(name)

    def _check(self, profile_id):
        if profile_id in self.broken:
            raise RuntimeError("zernio down")

    def list_inbox_comments(self, profile_id, **kw):
        self._check(profile_id)
        return {"data": self.posts.get(profile_id, [])}

    def inbox_post_comments(self, post_id, account_id, **kw):
        return {"comments": self.threads.get(post_id, [])}

    def list_inbox_mentions(self, profile_id, **kw):
        self._check(profile_id)
        return {"data": self.mentions.get(profile_id, [])}

    def list_inbox_reviews(self, profile_id, **kw):
        self._check(profile_id)
        return {"data": self.reviews.get(profile_id, [])}


class FakeKv:
    def __init__(self):
        self.store = {}

    def get(self, key, default=""):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = str(value)


def _armed(monkeypatch, value="true"):
    monkeypatch.setenv("AGENT_INBOX_ALERTS", value)


# ---- flag gate ---------------------------------------------------------------------

def test_flag_off_is_a_noop(monkeypatch):
    monkeypatch.delenv("AGENT_INBOX_ALERTS", raising=False)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("flag OFF must not touch the client")

    out = run(zernio=Boom(), notifier=lambda g, t: (_ for _ in ()).throw(
        AssertionError("flag OFF must not notify")))
    assert out["ok"] is False
    assert "OFF" in out["reason"]


# ---- sweep + card ------------------------------------------------------------------

def _fake_for_one_gym():
    return FakeZernio(
        profiles={"topfuel": "prof_tf"},
        posts={"prof_tf": [_post("p1", count=2)]},
        threads={"p1": [
            _comment(LIVE_HOMOGLYPH_SPAM),
            _comment("Love this gym, when is the next open house?"),
            _comment("\U0001f525\U0001f525"),  # neutral, never carded
        ]},
    )


def test_sweep_and_card_content(monkeypatch):
    _armed(monkeypatch)
    z = _fake_for_one_gym()
    summary = sweep_gym("topfuel", z, NOW)
    kinds = sorted(i["kind"] for i in summary["items"])
    assert kinds == ["member_comment", "spam"]
    card = build_card("topfuel", summary["items"])
    assert card.startswith("REPLY NEEDED at topfuel:")
    assert "1 member comment(s) waiting" in card
    assert "1 spam comment(s) to hide" in card
    assert "https://instagram.com/p/p1/" in card
    # comment text present, truncated to 100 chars
    for line in card.splitlines()[1:]:
        start = line.find('"')
        end = line.rfind('"')
        assert end - start - 1 <= 100


def test_card_caps_at_five_items(monkeypatch):
    items = [{"kind": "member_comment", "source": "comment",
              "text": f"comment {i}", "url": f"https://x/{i}", "age_days": i}
             for i in range(8)]
    card = build_card("gym", items)
    numbered = [ln for ln in card.splitlines() if ln[:2] in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.")]
    assert len(numbered) == MAX_ITEMS_PER_CARD
    assert "3 more" in card


def test_no_actionable_items_no_card():
    assert build_card("gym", []) is None
    assert build_card("gym", [{"kind": "neutral", "source": "comment",
                               "text": "x", "url": "", "age_days": 0}]) is None


def test_one_card_per_gym_per_day_dedupe(monkeypatch):
    _armed(monkeypatch)
    z = _fake_for_one_gym()
    kv = FakeKv()
    sent = []
    out1 = run(gyms=["topfuel"], zernio=z, now=NOW,
               notifier=lambda g, t: sent.append((g, t)),
               kv_get=kv.get, kv_set=kv.set)
    assert out1["cards_sent"] == 1 and len(sent) == 1
    out2 = run(gyms=["topfuel"], zernio=z, now=NOW,
               notifier=lambda g, t: sent.append((g, t)),
               kv_get=kv.get, kv_set=kv.set)
    assert out2["cards_sent"] == 0 and len(sent) == 1
    assert out2["gyms"][0]["skipped"] == "card already sent today"


def test_gym_with_nothing_actionable_keeps_its_card_for_later(monkeypatch):
    """No actionable items -> no card AND no stamp (the day is not eaten)."""
    _armed(monkeypatch)
    z = FakeZernio(profiles={"eng": "prof_eng"}, posts={"prof_eng": []})
    kv = FakeKv()
    out = run(gyms=["eng"], zernio=z, now=NOW,
              notifier=lambda g, t: (_ for _ in ()).throw(AssertionError("no card")),
              kv_get=kv.get, kv_set=kv.set)
    assert out["cards_sent"] == 0
    assert kv.store == {}


def test_zernio_error_on_one_gym_never_blocks_the_rest(monkeypatch):
    _armed(monkeypatch)
    z = FakeZernio(
        profiles={"gritx": "prof_gx", "topfuel": "prof_tf"},
        posts={"prof_tf": [_post("p1")]},
        threads={"p1": [_comment("great class today, sign me up")]},
        broken={"prof_gx"},
    )
    kv = FakeKv()
    sent = []
    out = run(gyms=["gritx", "topfuel"], zernio=z, now=NOW,
              notifier=lambda g, t: sent.append(g),
              kv_get=kv.get, kv_set=kv.set)
    assert out["ok"] is True
    by_gym = {r["gym_id"]: r for r in out["gyms"]}
    # gritx: all three sources failed -> ok but zero items, errors reported
    assert by_gym["gritx"]["errors"]
    assert sent == ["topfuel"]


def test_read_only_never_calls_write_methods(monkeypatch):
    """The sweep must never touch a reply/hide/delete method, even if the
    client exposes them."""
    _armed(monkeypatch)

    class WriteTrap(FakeZernio):
        def reply_to_comment(self, *a, **k):
            raise AssertionError("READ ONLY violated")

        def delete_comment(self, *a, **k):
            raise AssertionError("READ ONLY violated")

        def hide_comment(self, *a, **k):
            raise AssertionError("READ ONLY violated")

    z = WriteTrap(profiles={"topfuel": "prof_tf"},
                  posts={"prof_tf": [_post("p1")]},
                  threads={"p1": [_comment(LIVE_HOMOGLYPH_SPAM)]})
    kv = FakeKv()
    out = run(gyms=["topfuel"], zernio=z, now=NOW, notifier=lambda g, t: None,
              kv_get=kv.get, kv_set=kv.set)
    assert out["cards_sent"] == 1


def test_stale_and_owner_and_answered_comments_not_carded(monkeypatch):
    _armed(monkeypatch)
    z = FakeZernio(
        profiles={"topfuel": "prof_tf"},
        posts={"prof_tf": [_post("p1", count=3, created="2026-08-10T09:00:00.000Z")]},
        threads={"p1": [
            _comment("old comment", created="2026-08-12T10:00:00+0000"),  # stale (>7d)
            _comment("our own comment", owner=True),
            {"id": "c9", "message": "answered already",
             "createdTime": "2026-08-26T08:00:00+0000",
             "from": {"isOwner": False},
             "replies": [{"from": {"isOwner": True}, "message": "thanks"}],
             "isHidden": False},
        ]},
    )
    summary = sweep_gym("topfuel", z, NOW)
    assert summary["items"] == []


def test_unreplied_recent_review_is_carded(monkeypatch):
    _armed(monkeypatch)
    z = FakeZernio(
        profiles={"topfuel": "prof_tf"},
        posts={"prof_tf": []},
        reviews={"prof_tf": [
            {"id": "r1", "platform": "facebook", "hasReply": False,
             "text": "Great gym, wonderful coaches",
             "created": "2026-08-25T10:00:00+0000",
             "reviewUrl": "https://facebook.com/r1"},
            {"id": "r2", "platform": "facebook", "hasReply": True,
             "text": "Loved it", "created": "2026-08-25T10:00:00+0000",
             "reviewUrl": "https://facebook.com/r2"},
            {"id": "r3", "platform": "facebook", "hasReply": False,
             "text": "ancient review", "created": "2020-04-16T23:40:54+0000",
             "reviewUrl": "https://facebook.com/r3"},
        ]},
    )
    summary = sweep_gym("topfuel", z, NOW)
    urls = [i["url"] for i in summary["items"]]
    assert urls == ["https://facebook.com/r1"]
    assert summary["items"][0]["source"] == "review"
