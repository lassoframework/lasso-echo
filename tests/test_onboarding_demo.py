"""
Onboarding SAMPLE month (AGENT_ONBOARDING_DEMO, default OFF).

A brand-new gym cannot get a real calendar until intake produces approved sources, and
Echo must never invent facts to fill the gap — so it showed a new client an EMPTY
portal. These tests pin the sample month AND, more importantly, the rails that keep a
sample from ever behaving like real content.
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import onboarding_demo as od  # noqa: E402
from agent import calendar_autopublish as cap  # noqa: E402


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_DEMO", "true")
    yield


class _Store:
    def __init__(self, rows=None, fail=False):
        self.rows = list(rows or [])
        self.inserted = []
        self.deleted = []
        self.fail = fail

    def list_month(self, gym_id, month):
        return [r for r in self.rows
                if str(r.get("post_date", ""))[:7] == month
                and r.get("gym_id") == gym_id]

    def insert_rows(self, gym_id, rows):
        if self.fail:
            raise RuntimeError("supabase down")
        out = []
        for i, r in enumerate(rows):
            row = dict(r)
            row["id"] = f"s{len(self.inserted) + i}"
            out.append(row)
        self.inserted.extend(out)
        self.rows.extend(out)
        return out

    def delete_rows(self, gym_id, ids):
        self.deleted.extend(ids)
        self.rows = [r for r in self.rows if r.get("id") not in set(ids)]
        return len(ids)


# ---- the flag ---------------------------------------------------------------
def test_flag_off_seeds_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_DEMO", "false")
    store = _Store()
    out = od.seed("hillcountry", store=store)
    assert out["seeded"] == 0 and store.inserted == []


# ---- the shape the client sees ----------------------------------------------
def test_sample_month_shows_cadence_pillars_and_a_story_per_day():
    rows = od.build_rows("hillcountry", days=3, start=date(2026, 9, 1))
    days = sorted({r["post_date"] for r in rows})
    assert days == ["2026-09-01", "2026-09-02", "2026-09-03"]
    for d in days:
        day_rows = [r for r in rows if r["post_date"] == d]
        assert {r["format"] for r in day_rows} == {"feed", "story"}
        assert {r["account"] for r in day_rows} == {"instagram", "facebook"}
    # the copy rotates rather than repeating one line all month
    feeds = [r["caption"] for r in rows if r["format"] == "feed"
             and r["account"] == "instagram"]
    assert len(set(feeds)) == len(feeds)


def test_every_sample_row_is_marked_twice_and_carries_no_image():
    """Marked by BOTH pillar and caption prefix so a caption edit cannot un-mark a row
    the publisher must skip. No image by design: a sample is not the gym's content, and
    a row with no image_url cannot even enter due_rows."""
    for r in od.build_rows("hillcountry", days=4):
        assert r["pillar"] == od.SAMPLE_PILLAR
        assert r["caption"].startswith(od.SAMPLE_PREFIX)
        assert r["image_url"] == ""
        assert r["status"] == "draft"
        assert od.is_sample_row(r)


def test_sample_copy_makes_no_factual_claim_about_the_gym():
    """The no-fabrication gate: a sample invents nothing. No digits (prices, stats,
    class times, dates), and it says outright that it is a placeholder."""
    for r in od.build_rows("hillcountry", days=6):
        cap_text = r["caption"]
        assert not any(ch.isdigit() for ch in cap_text), f"a number leaked: {cap_text!r}"
        assert "—" not in cap_text and "–" not in cap_text   # brand dash rule
    feed = od.build_rows("hillcountry", days=1)[0]["caption"]
    assert "sample" in feed.lower() and "replaced" in feed.lower()


# ---- the publish rail (the one that really matters) -------------------------
def test_a_sample_row_can_never_publish_even_if_approved(monkeypatch):
    """The rail is checked BEFORE the approval gate, the slot gate and the claim, so it
    holds regardless of status, autonomy, catch_all, or a client tapping approve.

    The publish flags are ARMED here on purpose: with them off publish_due returns
    before reaching the rail and this test would prove nothing."""
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    # 'eng' is used because its accounts RESOLVE in the registry: with an unresolvable
    # gym the row is dropped by _account_for and the test would pass without the rail.
    row = {"id": "r1", "gym_id": "eng", "account": "instagram",
           "post_date": "2026-09-01", "format": "feed", "pillar": od.SAMPLE_PILLAR,
           "caption": od.SAMPLE_PREFIX + "anything", "image_url": "https://cdn/x.jpg",
           "status": "approved"}
    assert od.is_sample_row(row) is True

    claimed, published = [], []

    class _S:
        def due_rows(self, gym_id, run_date, catchup_days=0):
            return [row]

        def mark_publishing(self, rid, *a, **k):
            claimed.append(rid)
            return True

        def mark_publish_failed(self, rid, *a, **k):
            return True

    class _Pub:
        def publish(self, *a, **k):
            published.append(1)
            raise RuntimeError("must never get here")

    out = cap.publish_due("2026-09-01", gym_id="eng", store=_S(),
                          approved_only=False, catch_all=True, publisher=_Pub())
    # NOTE the assertions that matter: publish_due SWALLOWS a publisher exception and
    # records the row as failed, so raising inside _Pub proves nothing on its own.
    # Without the rail this row is CLAIMED and publish() IS called.
    assert published == [], "the publisher was called for a SAMPLE row"
    assert claimed == [], "a SAMPLE row was claimed for publish"
    assert out.get("skipped") == ["r1"], f"the sample was not skipped: {out}"
    assert out.get("failed") == [] and out.get("published") == []


def test_is_sample_row_detects_either_marker_and_ignores_real_rows():
    assert od.is_sample_row({"pillar": "sample", "caption": "no prefix"}) is True
    assert od.is_sample_row({"pillar": "offer",
                             "caption": "SAMPLE: edited pillar"}) is True
    assert od.is_sample_row({"pillar": "offer", "caption": "A real post"}) is False
    assert od.is_sample_row({}) is False and od.is_sample_row(None) is False


# ---- seeding refuses to touch real content ----------------------------------
def test_seed_refuses_when_the_gym_already_has_real_content():
    real = {"id": "r1", "gym_id": "hillcountry", "post_date": date.today().isoformat(),
            "pillar": "offer", "caption": "A real post", "status": "pending"}
    store = _Store(rows=[real])
    out = od.seed("hillcountry", store=store)
    assert out["seeded"] == 0 and store.inserted == []
    assert "real content" in out["reason"]


def test_seed_is_idempotent_so_the_frequent_scan_can_call_it_every_pass():
    store = _Store()
    first = od.seed("hillcountry", days=3, store=store)
    assert first["seeded"] > 0
    again = od.seed("hillcountry", days=3, store=store)
    assert again["seeded"] == 0 and again["reason"] == "already sampled"


def test_seed_refuses_when_the_calendar_is_unreadable():
    """An unreadable calendar must NEVER read as 'the gym is empty' — that would seed
    samples on top of a real month."""
    class _Unreadable:
        def list_month(self, gym_id, month):
            raise RuntimeError("supabase down")

        def insert_rows(self, gym_id, rows):
            raise AssertionError("must not insert when the calendar cannot be read")

    out = od.seed("hillcountry", store=_Unreadable())
    assert out["seeded"] == 0 and "unreadable" in out["reason"]


def test_seed_survives_an_insert_failure():
    out = od.seed("hillcountry", store=_Store(fail=True))
    assert out["ok"] is False and out["seeded"] == 0


# ---- clearing ---------------------------------------------------------------
def test_clear_removes_only_sample_rows():
    real = {"id": "real1", "gym_id": "hillcountry",
            "post_date": date.today().isoformat(), "pillar": "offer",
            "caption": "A real post"}
    store = _Store(rows=[real])
    # seed alongside a real row is refused, so place samples directly
    store.rows.extend(store.insert_rows("hillcountry",
                                        od.build_rows("hillcountry", days=2)))
    removed = od.clear("hillcountry", store=store)
    assert removed > 0
    assert any(r.get("id") == "real1" for r in store.rows), "a real row was deleted"
    assert not [r for r in store.rows if od.is_sample_row(r)]
