"""
Real-drafts calendar mirror (agent/real_calendar_mirror.py), all offline.

collect_real_drafts maps a gym's REAL drafts to content_calendar row shape (format,
date, platform), excludes demo-manifest ids, and treats a feed draft and its paired
story as SEPARATE rows. mirror_plan is a pure diff (upsert real + delete demo, gym
scoped). mirror_to_supabase applies the plan through an injected fake store and never
touches another gym. After a mirror a real gym carries ZERO demo ids. Part C: an action
on a mirrored real draft id round-trips (approve -> approved) with token isolation, no
publish.
"""

import os
import sys
import uuid as _uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import real_calendar_mirror as rcm
from agent import demo_calendar_queue as demo
from agent import portal_social as ps
from agent.drafter import Draft, DraftStatus


def _reject_non_uuid_id(row):
    """Model the real DB: an insert that carries an `id` at all is rejected. The insert
    path MUST omit id so gen_random_uuid fires; a draft id is a non-uuid string Postgres
    would refuse with 22P02. A fake that silently accepted it hid the live bug."""
    if "id" in row and row.get("id") not in (None, ""):
        try:
            _uuid.UUID(str(row["id"]))
        except (ValueError, AttributeError, TypeError):
            raise AssertionError(
                f"non-uuid id sent to insert (22P02 in real DB): {row.get('id')!r}")
        raise AssertionError("insert must not send id; the DB generates the uuid")


# ---- fakes ----------------------------------------------------------------

def _draft(draft_id, account_key="northside_ig", platform="instagram",
           day_key="2026-08-06", category="proof", caption="hi",
           url="https://cdn/x.jpg", status=DraftStatus.PENDING,
           is_story=False, draft_type="feed", scheduled_for=""):
    return Draft(
        draft_id=draft_id, account_key=account_key, platform=platform,
        caption=caption, hashtags=[], creative_path="x.png",
        creative_public_url=url, scheduled_for=scheduled_for, status=status,
        is_story=is_story, day_key=day_key, draft_type=draft_type, category=category)


class _FakeStore:
    """Stands in for PendingStore.list_for_account, scoped by account_key."""

    def __init__(self, drafts):
        self._drafts = list(drafts)

    def list_for_account(self, account_key):
        return [d for d in self._drafts if d.account_key == account_key]


class _FakeSB:
    """Stands in for SupabaseCalendarStore. Enforces gym_id isolation on every write and
    RECORDS every insert / delete so a test can prove no cross-gym write happens. Models
    the REAL schema: content_calendar.id is a DB-generated uuid, so insert_rows REJECTS a
    row that carries an id (a non-uuid draft id would be 22P02 in Postgres) and generates
    the uuid itself."""

    def __init__(self, rows=None):
        self._rows = {}
        for r in (rows or []):
            rid = r.get("id") or _uuid.uuid4().hex
            self._rows[rid] = dict(r, id=rid)
        self.upserts = []
        self.inserts = []
        self.deletes = []

    def list_month(self, account_key, month):
        return [dict(r) for r in self._rows.values()
                if str(r.get("gym_id")) == str(account_key)
                and (r.get("post_date") or "").startswith(month)]

    def insert_rows(self, account_key, rows):
        out = []
        for row in (rows or []):
            _reject_non_uuid_id(row)  # a fake that accepted a draft id hid the live bug
            assert str(row.get("gym_id")) == str(account_key), "cross-gym insert"
            rid = _uuid.uuid4().hex  # DB-generated uuid
            saved = dict(row, id=rid, gym_id=account_key)
            self._rows[rid] = saved
            self.inserts.append((account_key, dict(saved)))
            out.append(dict(saved))
        return out

    def delete_month(self, account_key, month):
        victims = [rid for rid, r in self._rows.items()
                   if str(r.get("gym_id")) == str(account_key)
                   and (r.get("post_date") or "").startswith(month)]
        for rid in victims:
            del self._rows[rid]
        self.deletes.append((account_key, month, len(victims)))
        return len(victims)

    def delete_row(self, account_key, row_id):
        self.deletes.append((account_key, row_id))
        r = self._rows.get(row_id)
        if r is not None and str(r.get("gym_id")) == str(account_key):
            del self._rows[row_id]
            return 1
        return 0


# ---- collect_real_drafts --------------------------------------------------

def test_collect_maps_format_date_platform():
    feed = _draft("realf_1", platform="instagram", day_key="2026-08-06",
                  category="proof", draft_type="feed")
    story = _draft("reals_1", platform="instagram", day_key="2026-08-06",
                   category="proof", is_story=True, draft_type="story")
    rows = rcm.collect_real_drafts("northside_ig", _FakeStore([feed, story]))
    assert len(rows) == 2
    by_fmt = {r["format"]: r for r in rows}
    assert set(by_fmt) == {"feed", "story"}, "feed + story are SEPARATE rows"
    assert by_fmt["feed"]["account"] == "instagram"      # platform -> account
    assert by_fmt["feed"]["post_date"] == "2026-08-06"   # day_key -> post_date
    assert by_fmt["feed"]["pillar"] == "proof"           # category -> pillar
    assert by_fmt["feed"]["gym_id"] == "northside_ig"
    assert by_fmt["story"]["format"] == "story"


def test_collect_excludes_demo_ids():
    demo_feed = _draft(demo._draft_id("northside_ig", "2026-08-06", "feed"))
    demo_story = _draft(demo._draft_id("northside_ig", "2026-08-06", "story"),
                        is_story=True, draft_type="story")
    real = _draft("realf_9", caption="only real")
    rows = rcm.collect_real_drafts("northside_ig",
                                   _FakeStore([demo_feed, demo_story, real]))
    # Rows carry NO id (the DB generates the uuid); the demo drafts are excluded up front
    # by their id namespace, so only the one real draft's row survives.
    assert all("id" not in r for r in rows), "row must not carry an id"
    assert len(rows) == 1
    assert rows[0]["caption"] == "only real"


def test_collect_skips_draft_without_hosted_url():
    no_url = _draft("realf_x", url="")
    rows = rcm.collect_real_drafts("northside_ig", _FakeStore([no_url]))
    assert rows == []


def test_collect_uses_scheduled_for_when_no_day_key():
    d = _draft("realf_s", day_key="", scheduled_for="2026-08-09T18:30:00+00:00")
    rows = rcm.collect_real_drafts("northside_ig", _FakeStore([d]))
    assert rows[0]["post_date"] == "2026-08-09"


# ---- mirror_plan ----------------------------------------------------------

def test_plan_upserts_real_deletes_demo_gym_scoped():
    real = _draft("realf_1", account_key="northside_ig", caption="the real one")
    store = _FakeStore([real])
    existing = [
        {"id": demo._draft_id("northside_ig", "2026-08-06", "feed"),
         "gym_id": "northside_ig", "post_date": "2026-08-06"},
        # another gym's row must NEVER be touched
        {"id": demo._draft_id("othergym", "2026-08-06", "feed"),
         "gym_id": "othergym", "post_date": "2026-08-06"},
    ]
    plan = rcm.mirror_plan("northside_ig", store, existing)
    # The one real draft's row is queued to write; it carries no id (DB generates it).
    assert len(plan["upsert"]) == 1
    assert plan["upsert"][0]["caption"] == "the real one"
    assert "id" not in plan["upsert"][0]
    assert plan["delete_ids"] == [existing[0]["id"]]
    assert existing[1]["id"] not in plan["delete_ids"], "other gym untouched"


def test_plan_never_upserts_a_demo_id():
    # even if a demo id somehow reached the store, the plan drops it from upsert.
    demo_draft = _draft(demo._draft_id("northside_ig", "2026-08-06", "feed"))
    plan = rcm.mirror_plan("northside_ig", _FakeStore([demo_draft]), [])
    assert plan["upsert"] == []


# ---- mirror_to_supabase (applies the plan) --------------------------------

def test_mirror_applies_and_leaves_zero_demo_ids():
    real = _draft("realf_1", account_key="northside_ig", day_key="2026-08-06",
                  caption="the real row")
    demo_id = demo._draft_id("northside_ig", "2026-08-06", "feed")
    sb = _FakeSB(rows=[{"id": demo_id, "gym_id": "northside_ig",
                        "post_date": "2026-08-06", "account": "instagram",
                        "status": "pending", "caption": "demo", "image_url": "u",
                        "pillar": "p", "format": "feed"}])
    summary = rcm.mirror_to_supabase("northside_ig", _FakeStore([real]), sb)
    assert summary["ok"] is True
    assert summary["inserted"] == 1
    assert summary["deleted"] == 1  # the whole Aug month wiped, then the real row inserted
    # After the mirror the gym holds the real row (a DB-generated uuid, NOT the draft id)
    # and ZERO demo ids: the delete-then-insert cleared the demo row.
    remaining = sb.list_month("northside_ig", "2026-08")
    assert len(remaining) == 1
    assert remaining[0]["caption"] == "the real row"
    ids = [r["id"] for r in remaining]
    assert not any(demo.is_demo_draft_id(i) for i in ids)
    assert _uuid.UUID(remaining[0]["id"])  # a real uuid, not the draft id


def test_mirror_refuses_demo_gym_id(monkeypatch):
    monkeypatch.setenv("AGENT_DEMO_CALENDAR_GYM_ID", "lasso_demo")
    sb = _FakeSB()
    summary = rcm.mirror_to_supabase("lasso_demo", _FakeStore([]), sb)
    assert summary["ok"] is False
    assert sb.inserts == [] and sb.deletes == []


def test_mirror_never_writes_another_gyms_row():
    real = _draft("realf_1", account_key="northside_ig", day_key="2026-08-06")
    sb = _FakeSB()
    rcm.mirror_to_supabase("northside_ig", _FakeStore([real]), sb)
    assert sb.inserts, "the real row was inserted"
    for account_key, row in sb.inserts:
        assert account_key == "northside_ig"
        assert row["gym_id"] == "northside_ig"


# ---- flag OFF changes nothing (runner hook) -------------------------------

def test_runner_hook_off_does_not_mirror(monkeypatch):
    from agent import runner, config
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.delenv("AGENT_REAL_CALENDAR_MIRROR", raising=False)  # OFF
    assert config.real_calendar_mirror_enabled() is False

    called = {"mirror": False}
    monkeypatch.setattr(rcm, "mirror_to_supabase",
                        lambda *a, **k: called.__setitem__("mirror", True))

    class _Poster:
        def post_notice(self, *a, **k):
            return {}

        def post_approval_card(self, *a, **k):
            return {"ok": True}

    # No accounts -> the draft loop is a no-op; only the mirror block matters here.
    monkeypatch.setattr(runner, "load_voice", lambda *a, **k: object())
    runner.run_daily(poster=_Poster(), accounts=[], store=_FakeStore([]))
    assert called["mirror"] is False, "flag OFF must never call the mirror"


# ---- Part C: action on a mirrored real draft round-trips -------------------

class _ActionSB:
    """Minimal content_calendar store for the portal action path: get_row + set_status,
    both gym scoped (the real store's double guard)."""

    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r and str(r.get("gym_id")) == str(account_key):
            return dict(r)
        return None

    def set_status(self, account_key, row_id, new_status):
        r = self._rows.get(row_id)
        if r and str(r.get("gym_id")) == str(account_key):
            r["status"] = new_status
            return dict(r)
        return None


def test_mirrored_draft_action_roundtrips_with_isolation(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")

    # A real draft mirrored to content_calendar for northside_ig. The row is inserted
    # WITHOUT an id and the DB assigns a uuid; the portal action keys off THAT uuid (the
    # value /social read back), never the draft id.
    real = _draft("realf_1", account_key="northside_ig", day_key="2026-08-06")
    row = rcm.collect_real_drafts("northside_ig", _FakeStore([real]))[0]
    assert "id" not in row
    row_uuid = _uuid.uuid4().hex
    sb = _ActionSB([dict(row, id=row_uuid)])

    status, body = ps.handle_approve("northside_ig", row_uuid, "actor-1", sb_store=sb)
    assert status == 200
    assert body["ok"] is True and body["action"] == "approve"
    assert sb.get_row("northside_ig", row_uuid)["status"] == "approved"

    # Token isolation: another gym's token can never act on this row.
    s2, b2 = ps.handle_approve("othergym", row_uuid, "actor-2", sb_store=sb)
    assert s2 == 404 and b2["ok"] is False
