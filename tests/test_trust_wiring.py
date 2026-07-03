"""
Trust ladder wiring tests. Offline, adversarial. Asserts: the FIRST-POST gate
(an account with zero real publishes is never eligible, whatever its level and
calendar); off-template always cards; book/comments/stories always card; trust
is per account, never transfers; DRY RUN marks and audits but never publishes;
autopublish fires ONLY for calendar-routine drafts on level 1+ accounts with
history; both flags off = fully inert.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, trust  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.runner import _post_and_save  # noqa: E402
from agent.trust import TrustLevel, auto_eligibility  # noqa: E402


def _acct(level=TrustLevel.ROUTINE_AUTO, key="lasso_ig"):
    return Account(key=key, display_name=key, platform=Platform.INSTAGRAM,
                   token_env="TW_T", target_id_env="TW_I", trust=level)


def _draft(creative="cal_card.png", day="2026-07-06", draft_type="feed",
           is_story=False, key="lasso_ig"):
    return Draft(draft_id="d1", account_key=key, platform="instagram",
                 caption="c", hashtags=[], creative_path=f"/lib/{creative}",
                 creative_public_url="u", scheduled_for="t",
                 status=DraftStatus.PENDING, day_key=day, draft_type=draft_type,
                 is_story=is_story)


def _approve_calendar(key="lasso_ig", month="2026-07", cards=("cal_card.png",)):
    db.kv_set(f"approved_calendar_{key}_{month}", json.dumps(list(cards)))


def _publish_history(key="lasso_ig"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at) VALUES ('h',?,'instagram','x','M','published',"
            "'2026-06-01T10:00:00')", (key,))
        conn.commit()


# ---- eligibility oracle (adversarial) ---------------------------------------------
def test_first_post_gate_never_automated():
    _approve_calendar()                       # calendar approved, level 1...
    ok, why = auto_eligibility(_acct(), _draft())
    assert ok is False                         # ...but ZERO publish history
    assert "first post" in why and "never automated" in why


def test_off_template_always_cards():
    _publish_history()
    _approve_calendar(cards=("some_other_card.png",))
    ok, why = auto_eligibility(_acct(), _draft("not_in_calendar.png"))
    assert ok is False and "off template" in why


def test_hard_exclusions_book_comments_stories():
    _publish_history()
    _approve_calendar()
    for kwargs, mark in ((dict(draft_type="book"), "book"),
                         (dict(draft_type="comment_t2"), "comment"),
                         (dict(is_story=True), "stories")):
        ok, why = auto_eligibility(_acct(), _draft(**kwargs))
        assert ok is False and mark in why, kwargs


def test_trust_is_per_account_never_transfers():
    _publish_history("lasso_ig")
    _publish_history("gym_b")
    _approve_calendar("lasso_ig")
    _approve_calendar("gym_b")
    ok_a, _ = auto_eligibility(_acct(TrustLevel.ROUTINE_AUTO, "lasso_ig"),
                               _draft(key="lasso_ig"))
    ok_b, why_b = auto_eligibility(_acct(TrustLevel.FULL_APPROVAL, "gym_b"),
                                   _draft(key="gym_b"))
    assert ok_a is True
    assert ok_b is False and "level 0" in why_b          # A's trust never helps B


def test_eligible_when_everything_lines_up():
    _publish_history()
    _approve_calendar()
    ok, why = auto_eligibility(_acct(), _draft())
    assert ok is True and "approved monthly calendar" in why


# ---- runner wiring -------------------------------------------------------------------
class RecordingPoster:
    def __init__(self):
        self.cards = []
        self.notices = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"channel": "C1", "ts": "1.2"}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


class Store:
    def __init__(self):
        self.saved = []

    def put(self, draft):
        self.saved.append(draft)


def test_both_flags_off_fully_inert(monkeypatch):
    monkeypatch.delenv("AGENT_TRUST_DRYRUN", raising=False)
    monkeypatch.delenv("AGENT_TRUST_AUTOPUBLISH", raising=False)
    _publish_history()
    _approve_calendar()
    d = _draft()
    poster = Store_poster = RecordingPoster()
    _post_and_save(d, Store(), poster, idempotent=True)
    assert poster.cards == [d]                            # carded exactly as today
    assert not getattr(d, "warnings", [])
    assert [r for r in db.audit_rows() if r["kind"].startswith("trust_")] == []


def test_dryrun_marks_audits_never_publishes(monkeypatch):
    monkeypatch.setenv("AGENT_TRUST_DRYRUN", "true")
    monkeypatch.delenv("AGENT_TRUST_AUTOPUBLISH", raising=False)
    # publishing anywhere would explode:
    import agent.meta_publisher as mp
    monkeypatch.setattr(mp, "publish", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("dry run published!")))
    monkeypatch.setattr("agent.accounts.get_account",
                        lambda key: _acct(TrustLevel.ROUTINE_AUTO, key))
    _publish_history()
    _approve_calendar()
    d = _draft()
    poster = RecordingPoster()
    _post_and_save(d, Store(), poster, idempotent=True)
    assert poster.cards == [d]                            # STILL cards for the tap
    assert any("would auto-publish at current trust" in w for w in d.warnings)
    rows = [r for r in db.audit_rows() if r["kind"] == "trust_dryrun"]
    assert len(rows) == 1


def test_autopublish_fires_only_when_eligible(monkeypatch):
    monkeypatch.setenv("AGENT_TRUST_AUTOPUBLISH", "true")

    class Result:
        mode = "would_publish"                            # publish flag still guards
        media_id = "M1"

    calls = []
    import agent.meta_publisher as mp
    monkeypatch.setattr(mp, "publish", lambda draft, acct: calls.append(draft) or Result())
    monkeypatch.setattr("agent.accounts.get_account",
                        lambda key: _acct(TrustLevel.ROUTINE_AUTO, key))
    _publish_history()
    _approve_calendar()
    poster = RecordingPoster()
    store = Store()
    d = _draft()                                          # calendar routine: eligible
    _post_and_save(d, store, poster, idempotent=True)
    assert calls == [d]                                   # went to the publisher
    assert poster.cards == []                             # no card
    assert d.status == DraftStatus.APPROVED
    assert any("AUTO PUBLISHED under trust" in n for n in poster.notices)
    # an off-template draft on the SAME account still cards
    d2 = _draft("rogue.png")
    d2.draft_id = "d2"
    _post_and_save(d2, store, poster, idempotent=True)
    assert d2 in poster.cards and len(calls) == 1


# ---- seed-calendar: approval evidence only ------------------------------------------
def test_seed_calendar_unapproved_never_enters_and_roundtrips(monkeypatch):
    from agent import seed_calendar
    from agent.store import PendingStore
    # an APPROVED queued draft, a PENDING one, a BLOCKED one
    store = Store2 = PendingStore()
    for did, status, creative in (("a1", DraftStatus.APPROVED, "cal_ok.png"),
                                  ("p1", DraftStatus.PENDING, "sneaky.png"),
                                  ("b1", DraftStatus.BLOCKED, "broken.png")):
        store.put(Draft(draft_id=did, account_key="lasso_ig", platform="instagram",
                        caption="c", hashtags=[], creative_path=f"/lib/{creative}",
                        creative_public_url="", scheduled_for="t", status=status,
                        day_key="2026-07-08", draft_type="feed"))
    # a really-published post (went through the tap)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at, creative_key) VALUES ('x','lasso_ig','instagram',"
            "'c','M','published','2026-07-06T18:30:00','tapped.png')")
        conn.commit()
    cal = seed_calendar.build_calendar("lasso_ig", "2026-07")
    assert sorted(cal["keys"]) == ["cal_ok.png", "tapped.png"]
    assert "sneaky.png" not in cal["keys"]                # ADVERSARIAL: pending out
    assert "broken.png" not in cal["keys"]
    # gaps = posting days with nothing approved (Saturdays excluded by cadence)
    assert "2026-07-06" not in cal["gaps"] and "2026-07-08" not in cal["gaps"]
    assert "2026-07-07" in cal["gaps"]
    assert "2026-07-11" not in cal["gaps"]                # a Saturday: skip day
    # write then read round-trips through the exact key trust reads
    seed_calendar.run("lasso_ig", "2026-07", write=True)
    assert trust.approved_calendar("lasso_ig", "2026-07") == {"cal_ok.png",
                                                              "tapped.png"}
