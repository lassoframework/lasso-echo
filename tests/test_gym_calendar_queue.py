"""
Part A tests: per-gym calendar engine, RULING 1 collision-shift, approval_surface
routing, and baseline capture. Offline (no network, no R2). Each test uses the
isolated per-test sqlite db (tests/conftest.py).

Asserts:
  * master flag AGENT_PORTAL_SOCIAL_ENABLED default OFF -> every hook inert
    (byte-for-byte current behavior).
  * per-gym keying: the LASSO demo is one gym; a client gym is an additional gym,
    each with its own dated slots keyed by (gym_id, account_key, day_key).
  * the served-once-per-day lock: at most one served post per (account_key, day_key).
  * RULING 1: the live book queue WINS the demo overlap dates; the calendar post
    SHIFTS to the next open day and never doubles up.
  * approval_surface routing: a client-gym draft produces NO Slack approval card
    while a LASSO draft still does; ops_alerts still fire for clients.
  * baseline storage: a setter + getter with a timestamp on the gym record.
  * no em/en/hyphen dashes and never the word "vendor" in any new client-facing string.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, gym_calendar_queue as gcq  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

_DASH = re.compile(r"[‐-―−\-]")  # em/en/figure dashes + hyphen-minus

# The demo overlap dates the book queue occupies for a LASSO account (book_queue has
# posts on 2026-08-12/15/19/22/26). Ruling 1 says the calendar SHIFTS off these.
_BOOK_DAYS = ["2026-08-12", "2026-08-15", "2026-08-19", "2026-08-22", "2026-08-26"]

_LASSO_GYM = "lasso_demo"
_CLIENT_GYM = "acme_gym"


def _lasso_ig():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM, token_env="X", target_id_env="Y")


def _client_ig():
    return Account(key="acme_ig", display_name="Acme Fitness IG",
                   platform=Platform.INSTAGRAM, token_env="X", target_id_env="Y")


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")


# ---- master flag gating: OFF -> inert --------------------------------------------------

def test_hook_inert_while_flag_off(monkeypatch):
    gcq.upsert_gym_post(_LASSO_GYM, "lasso_ig", "2026-08-07", num=1,
                        pillar="All in one offer", caption="hook",
                        feed_url="https://cdn.test/x.png")
    assert gcq.build_gym_calendar_draft(_LASSO_GYM, _lasso_ig(), "2026-08-07") is None


def test_surface_is_slack_while_flag_off():
    # flag off -> everyone routes to slack (today's behavior), even a client account
    assert gcq.approval_surface_for(_client_ig()) == "slack"
    assert gcq.approval_surface_for(_lasso_ig()) == "slack"


# ---- per-gym keying: LASSO demo is one gym, client gyms are additional gyms -------------

def test_per_gym_keying_isolated(armed):
    gcq.upsert_gym_post(_LASSO_GYM, "lasso_ig", "2026-08-07", num=1,
                        pillar="All in one offer", caption="lasso cap",
                        feed_url="https://cdn.test/lasso.png")
    gcq.upsert_gym_post(_CLIENT_GYM, "acme_ig", "2026-08-07", num=1,
                        pillar="All in one offer", caption="acme cap",
                        feed_url="https://cdn.test/acme.png",
                        zernio_profile_id="zprof_acme")
    lasso = gcq.build_gym_calendar_draft(_LASSO_GYM, _lasso_ig(), "2026-08-07")
    acme = gcq.build_gym_calendar_draft(_CLIENT_GYM, _client_ig(), "2026-08-07")
    assert lasso is not None and acme is not None
    # distinct gyms, distinct draft ids, distinct content — no cross-read
    assert lasso.draft_id != acme.draft_id
    assert lasso.caption == "lasso cap" and acme.caption == "acme cap"
    assert lasso.status is DraftStatus.PENDING and acme.status is DraftStatus.PENDING


def test_no_seeded_row_is_none(armed):
    assert gcq.build_gym_calendar_draft(_LASSO_GYM, _lasso_ig(), "2026-08-07") is None


def test_zernio_profile_id_persisted(armed):
    gcq.upsert_gym_post(_CLIENT_GYM, "acme_ig", "2026-08-07", num=1,
                        caption="c", feed_url="u", zernio_profile_id="zprof_acme")
    rows = gcq.queue_status(_CLIENT_GYM)
    assert len(rows) == 1
    with db.connect() as conn:
        row = conn.execute(
            "SELECT zernio_profile_id FROM gym_calendar_queue WHERE gym_id=?",
            (_CLIENT_GYM,)).fetchone()
    assert row["zernio_profile_id"] == "zprof_acme"


# ---- served-once-per-day lock ----------------------------------------------------------

def test_served_lock_one_post_per_account_per_day(armed):
    # two gyms both target the SAME account on the SAME day: only ONE serves that day;
    # the second shifts to the next open day (never two posts on one served_day).
    gcq.upsert_gym_post("gymA", "lasso_ig", "2026-08-07", num=1, caption="A",
                        feed_url="uA")
    gcq.upsert_gym_post("gymB", "lasso_ig", "2026-08-07", num=1, caption="B",
                        feed_url="uB")
    first = gcq.build_gym_calendar_draft("gymA", _lasso_ig(), "2026-08-07")
    second = gcq.build_gym_calendar_draft("gymB", _lasso_ig(), "2026-08-07")
    assert first is not None and second is not None
    assert first.day_key == "2026-08-07"
    assert second.day_key != "2026-08-07"          # shifted off the taken day
    assert first.day_key != second.day_key         # never doubled up


def test_mark_and_check_ledger(armed):
    assert gcq.account_served_on("lasso_ig", "2026-08-07") is False
    gcq.mark_account_served("lasso_ig", "2026-08-07", source="test")
    assert gcq.account_served_on("lasso_ig", "2026-08-07") is True
    # idempotent: re-marking the same slot does not raise or duplicate
    gcq.mark_account_served("lasso_ig", "2026-08-07", source="test2")


# ---- RULING 1: book queue WINS the overlap dates; calendar shifts -----------------------

def test_calendar_shifts_off_every_book_overlap_date(armed):
    """Seed a calendar slot on each demo overlap date the book queue occupies. The
    calendar post must SHIFT to the next open day (never the book-owned day) and never
    double up on an account's served day."""
    for day in _BOOK_DAYS:
        gcq.upsert_gym_post(_LASSO_GYM, "lasso_ig", day, num=1,
                            pillar="Proof", caption=f"cap {day}",
                            feed_url=f"https://cdn.test/{day}.png")
    served_days = set()
    for day in _BOOK_DAYS:
        d = gcq.build_gym_calendar_draft(_LASSO_GYM, _lasso_ig(), day)
        assert d is not None, f"expected a shifted draft for {day}"
        # the book queue owns `day`; the calendar must not serve on it
        assert d.day_key != day, f"calendar doubled up on book-owned {day}"
        assert not gcq._book_queue_occupies("lasso_ig", d.day_key), \
            f"shifted onto another book-owned day {d.day_key}"
        # never two calendar posts on one served day for this account
        assert d.day_key not in served_days, f"double post on {d.day_key}"
        served_days.add(d.day_key)


def test_book_queue_wins_is_flag_gated(monkeypatch):
    # with the flag OFF the engine is inert regardless of book overlap
    gcq.upsert_gym_post(_LASSO_GYM, "lasso_ig", _BOOK_DAYS[0], num=1, caption="c",
                        feed_url="u")
    assert gcq.build_gym_calendar_draft(_LASSO_GYM, _lasso_ig(), _BOOK_DAYS[0]) is None


# ---- approval_surface routing ----------------------------------------------------------

def test_surface_slack_for_lasso_portal_for_client(armed):
    assert gcq.approval_surface_for(_lasso_ig()) == "slack"
    assert gcq.approval_surface_for(_client_ig()) == "portal"


def _fake_client_account(monkeypatch):
    """Register a client gym account so runner.get_account resolves it to a portal
    surface, without touching the real ACCOUNTS list globally."""
    from agent import accounts as _accts
    client = Account(key="acme_ig", display_name="Acme", platform=Platform.INSTAGRAM,
                     token_env="X", target_id_env="Y", slack_channel="")
    monkeypatch.setattr(_accts, "get_account",
                        lambda k: client if k == "acme_ig" else None)
    return client


class _Poster:
    def __init__(self):
        self.cards = []
        self.notices = []

    def post_approval_card(self, d):
        self.cards.append(d)
        return {"ok": True, "channel": "c", "ts": "t"}

    def post_notice(self, m):
        self.notices.append(m)


class _Store:
    def __init__(self):
        self.saved = []

    def put(self, d):
        self.saved.append(d)


def test_client_draft_no_slack_card_lasso_draft_has_one(armed, monkeypatch):
    from agent import runner
    monkeypatch.setattr(config, "auto_approve_enabled", lambda: False)
    monkeypatch.setattr(config, "trust_dryrun_enabled", lambda: False)
    monkeypatch.setattr(config, "trust_autopublish_enabled", lambda: False)
    _fake_client_account(monkeypatch)

    client_draft = Draft(
        draft_id="gcalf_client", account_key="acme_ig", platform="instagram",
        caption="cap", hashtags=[], creative_path="x.png",
        creative_public_url="https://cdn.test/x.png",
        scheduled_for="2026-08-07T18:30:00Z", status=DraftStatus.PENDING,
        day_key="2026-08-07", draft_type="feed", force_approval=True)
    poster, store = _Poster(), _Store()
    runner._post_and_save(client_draft, store, poster, idempotent=False)
    assert client_draft not in poster.cards          # NO slack approval card
    assert client_draft in store.saved               # still saved, PENDING
    assert client_draft.status is DraftStatus.PENDING

    # a LASSO draft on the same run STILL cards
    from agent import accounts as _accts
    monkeypatch.setattr(_accts, "get_account", lambda k: _lasso_ig())
    lasso_draft = Draft(
        draft_id="gcalf_lasso", account_key="lasso_ig", platform="instagram",
        caption="cap", hashtags=[], creative_path="x.png",
        creative_public_url="https://cdn.test/x.png",
        scheduled_for="2026-08-07T18:30:00Z", status=DraftStatus.PENDING,
        day_key="2026-08-07", draft_type="feed", force_approval=True)
    poster2, store2 = _Poster(), _Store()
    runner._post_and_save(lasso_draft, store2, poster2, idempotent=False)
    assert lasso_draft in poster2.cards              # slack card as always


def test_post_and_save_flag_off_client_still_cards(monkeypatch):
    """FLAG OFF: byte-for-byte current behavior. A client account's draft still posts
    a Slack approval card (surface routing collapses to slack), so nothing about
    today's pipeline changes until AGENT_PORTAL_SOCIAL_ENABLED is armed."""
    from agent import runner
    monkeypatch.delenv("AGENT_PORTAL_SOCIAL_ENABLED", raising=False)
    monkeypatch.setattr(config, "auto_approve_enabled", lambda: False)
    monkeypatch.setattr(config, "trust_dryrun_enabled", lambda: False)
    monkeypatch.setattr(config, "trust_autopublish_enabled", lambda: False)
    _fake_client_account(monkeypatch)
    d = Draft(draft_id="gcalf_off", account_key="acme_ig", platform="instagram",
              caption="cap", hashtags=[], creative_path="x.png",
              creative_public_url="https://cdn.test/x.png",
              scheduled_for="2026-08-07T18:30:00Z", status=DraftStatus.PENDING,
              day_key="2026-08-07", draft_type="feed", force_approval=True)
    poster, store = _Poster(), _Store()
    runner._post_and_save(d, store, poster, idempotent=False)
    assert d in poster.cards          # flag off -> client still cards, unchanged
    assert d in store.saved


def test_ops_alerts_still_fire_for_clients(armed, monkeypatch):
    """ops_alerts (failures) go to Slack for EVERY gym, including a portal-surface
    client. The surface routing only skips the approval CARD, never the alert lane."""
    from agent import ops_alerts
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    posted = {}

    class _AlertPoster:
        def post_notice(self, text):
            posted["text"] = text
            return {"ok": True}

    ops_alerts.alert("account acme_ig hosting failed", poster=_AlertPoster())
    assert posted.get("text", "").startswith("ECHO ALERT:")
    assert "acme_ig" in posted["text"]


# ---- baseline capture ------------------------------------------------------------------

def test_baseline_setter_getter_and_timestamp():
    assert db.get_baseline_posts_per_week("acme_ig") == (None, None)
    ts = db.set_baseline_posts_per_week("acme_ig", 1.5)
    val, captured = db.get_baseline_posts_per_week("acme_ig")
    assert val == 1.5
    assert captured == ts and captured                 # timestamped on the gym record


def test_baseline_explicit_timestamp():
    db.set_baseline_posts_per_week("acme_ig", 2.0, captured_at="2026-08-06T00:00:00Z")
    val, captured = db.get_baseline_posts_per_week("acme_ig")
    assert val == 2.0 and captured == "2026-08-06T00:00:00Z"


# ---- no dashes, never "vendor" in any new client-facing string -------------------------

def test_no_dashes_and_no_vendor_in_engine_strings():
    """Any client-facing constant this module introduces is dash free and never uses
    the word 'vendor'. The pillar names are the client-facing constants Part A adds."""
    for pillar in gcq.PILLAR_ROTATION:
        assert not _DASH.search(pillar), f"pillar carries a dash: {pillar!r}"
        assert "vendor" not in pillar.lower(), f"pillar uses 'vendor': {pillar!r}"
    for did_kind in ("feed", "story"):
        did = gcq._draft_id("g", "acme_ig", "2026-08-07", did_kind)
        assert not _DASH.search(did)
        assert "vendor" not in did.lower()
