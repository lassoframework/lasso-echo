"""
Part B client-social surface (agent/portal_social.py) on the SHARED Supabase
content_calendar table, the same data plane /calendar reads.

When SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are both set, /social reads
content_calendar and approve/edit/deny/kill act on content_calendar (NO publish).
When those creds are absent, the existing gym_calendar_queue / drafts path is used
unchanged (covered by test_portal_social.py, which stays green).

Everything offline: an injected fake SupabaseCalendarStore stands in for PostgREST;
no network, no db. Billing is delegated so the Stripe gate is bypassed (the portal
already enforced entitlement), matching production.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_social as ps
from agent import portal_calendar_store as pcs


# ---------------------------------------------------------------------------
# fixtures / a fake content_calendar store (records every read + write)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Flag ON, Supabase creds present, billing delegated (portal owns entitlement)."""
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    yield


def _row(row_id, gym_id="lasso", post_date="2026-08-06", account="instagram",
         status="pending", caption="hello", image_url="https://cdn/x.jpg",
         pillar="education", fmt="feed", scheduled_at=None):
    return {
        "id": row_id, "gym_id": gym_id, "post_date": post_date, "account": account,
        "status": status, "caption": caption, "image_url": image_url,
        "pillar": pillar, "format": fmt, "scheduled_at": scheduled_at,
    }


class _FakeStore:
    """Stands in for SupabaseCalendarStore. Enforces the same gym_id isolation the
    real PostgREST filters do, and RECORDS every set_status so tests can assert that
    NO write was issued on a cross-gym or missing row."""

    def __init__(self, rows=None):
        # rows keyed by id, each a content_calendar dict (carries its own gym_id)
        self._rows = {r["id"]: dict(r) for r in (rows or [])}
        self.patches = []  # (row_id, new_status) for every set_status attempt

    def list_month(self, account_key, month):
        prefix = month + "-"
        return [dict(r) for r in self._rows.values()
                if r.get("gym_id") == account_key
                and (r.get("post_date") or "").startswith(prefix)]

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None  # gym scoped: a cross gym id never loads
        return dict(r)

    def set_status(self, account_key, row_id, new_status):
        self.patches.append((row_id, new_status))
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None  # id+gym_id filter matched zero rows
        r["status"] = new_status
        return dict(r)


# ===========================================================================
# 1. /social from content_calendar: id, real format, image, caption
# ===========================================================================

def test_social_reads_content_calendar(monkeypatch):
    store = _FakeStore([
        _row("uuid-1", post_date="2026-08-05", caption="cap one",
             image_url="https://cdn/1.jpg", pillar="proof", fmt="feed"),
        _row("uuid-2", post_date="2026-08-20", caption="cap two",
             image_url="https://cdn/2.jpg", pillar="offer", fmt="story"),
    ])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)

    status, body = ps.handle_social("lasso", "2026-08")
    assert status == 200
    assert body["account_key"] == "lasso"
    assert body["month"] == "2026-08"
    assert body["active"] is True
    assert len(body["posts"]) == 2

    p1 = body["posts"][0]
    assert p1["id"] == "uuid-1"
    assert p1["day_key"] == "2026-08-05"
    assert p1["status"] == "pending"
    assert p1["pillar"] == "proof"
    assert p1["format"] == "feed"
    assert p1["image_public_url"] == "https://cdn/1.jpg"
    assert p1["caption"] == "cap one"
    # the story row keeps its REAL format (not derived from is_story)
    assert body["posts"][1]["format"] == "story"


def test_social_post_surfaces_scheduled_at_go_live_time(monkeypatch):
    # The portal shows clients WHEN a post publishes; the payload must pass scheduled_at through
    # exactly as stored, and leave it None (never fabricated) when the row is unstamped.
    store = _FakeStore([
        _row("uuid-1", post_date="2026-08-05", scheduled_at="2026-08-05T07:30:00-04:00"),
        _row("uuid-2", post_date="2026-08-06", scheduled_at=None),
    ])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    _, body = ps.handle_social("lasso", "2026-08")
    by_id = {p["id"]: p for p in body["posts"]}
    assert by_id["uuid-1"]["scheduled_at"] == "2026-08-05T07:30:00-04:00"
    assert by_id["uuid-2"]["scheduled_at"] is None


def test_every_social_post_carries_a_stable_id(monkeypatch):
    store = _FakeStore([_row("id-a"), _row("id-b", post_date="2026-08-07")])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    status, body = ps.handle_social("lasso", "2026-08")
    assert status == 200
    assert body["posts"]
    for p in body["posts"]:
        assert "id" in p and p["id"], "every /social post must carry content_calendar.id"
    assert {p["id"] for p in body["posts"]} == {"id-a", "id-b"}


def test_social_image_key_is_image_public_url_not_creative(monkeypatch):
    store = _FakeStore([_row("id-1", image_url="https://cdn/z.jpg")])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    _, body = ps.handle_social("lasso", "2026-08")
    p = body["posts"][0]
    assert p["image_public_url"] == "https://cdn/z.jpg"
    assert "creative_public_url" not in p


# ---- low_creative honesty -----------------------------------------------------

def test_low_creative_false_when_any_image_present(monkeypatch):
    store = _FakeStore([
        _row("id-1", image_url=""),               # one row missing creative
        _row("id-2", post_date="2026-08-09", image_url="https://cdn/2.jpg"),
    ])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    _, body = ps.handle_social("lasso", "2026-08")
    assert body["low_creative"] is False
    # rows are NOT filtered out for a missing image: both are returned
    assert len(body["posts"]) == 2


def test_low_creative_true_when_no_image_anywhere(monkeypatch):
    store = _FakeStore([_row("id-1", image_url=""),
                        _row("id-2", post_date="2026-08-09", image_url="")])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    _, body = ps.handle_social("lasso", "2026-08")
    assert body["low_creative"] is True
    assert len(body["posts"]) == 2  # still returned, just flagged


def test_social_store_error_is_500(monkeypatch):
    class _Boom:
        def list_month(self, *a, **k):
            raise RuntimeError("supabase down")
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: _Boom())
    status, body = ps.handle_social("lasso", "2026-08")
    assert status == 500
    assert "svc-key-secret" not in str(body)


# ===========================================================================
# 2. actions on content_calendar (NO publish)
# ===========================================================================

def test_approve_sets_status_approved(monkeypatch):
    store = _FakeStore([_row("id-1", status="pending")])
    status, body = ps.handle_approve("lasso", "id-1", "U_owner", sb_store=store)
    assert status == 200
    assert body == {"ok": True, "action": "approve", "draft_id": "id-1"}
    assert store.patches == [("id-1", "approved")]


def test_approve_idempotent_on_already_approved(monkeypatch):
    store = _FakeStore([_row("id-1", status="approved")])
    status, body = ps.handle_approve("lasso", "id-1", "U_owner", sb_store=store)
    assert status == 200
    assert body["ok"] is True
    assert body.get("idempotent") is True
    assert store.patches == [], "an already approved row must not be re-written"


def test_deny_sets_status_denied_and_charges_budget(db_tmp, monkeypatch):
    store = _FakeStore([_row("id-1", status="pending")])
    assert ps.recreate_spent("lasso") == 0
    status, body = ps.handle_deny("lasso", "id-1", "U_owner", note="wrong tone",
                                  sb_store=store)
    assert status == 200
    assert body["ok"] is True and body["action"] == "deny"
    assert store.patches == [("id-1", "denied")]
    assert ps.recreate_spent("lasso") == 1, "a successful deny burns one unit"
    assert body["recreate_budget"]["remaining"] == 14


def test_deny_409_when_budget_exhausted_no_write(db_tmp, monkeypatch):
    store = _FakeStore([_row("id-1", status="pending")])
    for _ in range(15):
        ps.spend_recreate("lasso")
    status, body = ps.handle_deny("lasso", "id-1", "U_owner", note="x", sb_store=store)
    assert status == 409
    assert body["ok"] is False
    assert store.patches == [], "an exhausted budget must not write to content_calendar"


def test_kill_confirm_sets_status_killed_free(db_tmp, monkeypatch):
    store = _FakeStore([_row("id-1", status="pending")])
    status, body = ps.handle_kill("lasso", "id-1", "U_owner", confirm=True,
                                  sb_store=store)
    assert status == 200
    assert body["ok"] is True and body["action"] == "kill"
    assert store.patches == [("id-1", "killed")]
    assert ps.recreate_spent("lasso") == 0, "kill is free"


def test_kill_requires_confirm_no_write(monkeypatch):
    store = _FakeStore([_row("id-1", status="pending")])
    status, body = ps.handle_kill("lasso", "id-1", "U_owner", confirm=False,
                                  sb_store=store)
    assert status == 400
    assert body["ok"] is False
    assert "confirm" in body["error"].lower()
    assert store.patches == []


def test_edit_clean_note_keeps_pending_no_write(monkeypatch):
    store = _FakeStore([_row("id-1", status="pending")])
    status, body = ps.handle_edit("lasso", "id-1", "U_owner",
                                  note="Please make the tone warmer.", sb_store=store)
    assert status == 200
    assert body["ok"] is True and body["action"] == "edit"
    assert body["note"] == "Please make the tone warmer."
    assert store.patches == [], "edit issues NO status write"


def test_edit_with_stat_no_receipt_is_422_no_write(monkeypatch):
    store = _FakeStore([_row("id-1", status="pending")])
    status, body = ps.handle_edit("lasso", "id-1", "U_owner",
                                  note="Say we cut costs by 80 percent.", sb_store=store)
    assert status == 422
    assert body["ok"] is False
    assert "fabrication" in body["error"].lower()
    assert store.patches == [], "a refused edit must not touch content_calendar"


# ===========================================================================
# 3. CROSS-GYM isolation: gym B row, gym A key -> 404, NO write
# ===========================================================================

@pytest.mark.parametrize("call", [
    lambda store: ps.handle_approve("gymA", "id-b", "U_a", sb_store=store),
    lambda store: ps.handle_edit("gymA", "id-b", "U_a", note="ok", sb_store=store),
    lambda store: ps.handle_deny("gymA", "id-b", "U_a", note="no", sb_store=store),
    lambda store: ps.handle_kill("gymA", "id-b", "U_a", confirm=True, sb_store=store),
])
def test_cross_gym_action_is_404_and_no_write(db_tmp, call):
    # the row belongs to gym B; gym A knows the id and tries to act on it
    store = _FakeStore([_row("id-b", gym_id="gymB", status="pending")])
    status, body = call(store)
    assert status == 404, "a cross gym id must be not found"
    assert body["ok"] is False
    assert body["error"] == "draft not found"
    assert store.patches == [], "CRITICAL: no PATCH may be issued on a cross gym row"


def test_cross_gym_deny_does_not_charge_budget(db_tmp):
    store = _FakeStore([_row("id-b", gym_id="gymB", status="pending")])
    status, _ = ps.handle_deny("gymA", "id-b", "U_a", note="x", sb_store=store)
    assert status == 404
    assert ps.recreate_spent("gymA") == 0, "a 404 deny must not cost a budget unit"


def test_missing_row_action_is_404(db_tmp):
    store = _FakeStore([])  # no rows at all
    status, body = ps.handle_approve("lasso", "ghost", "U_owner", sb_store=store)
    assert status == 404
    assert body["ok"] is False
    assert store.patches == []


# ===========================================================================
# 4. creds ABSENT -> the existing gym_calendar_queue / drafts path is taken
# ===========================================================================

def test_no_creds_social_does_not_touch_supabase(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    # if the Supabase store were constructed, this would blow up the test loudly
    def _boom(*a, **k):
        raise AssertionError("Supabase path must NOT run when creds are absent")
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", _boom)

    status, body = ps.handle_social("lasso", "2026-08")  # billing delegated -> active
    assert status == 200
    # the SQLite gym_calendar_queue is empty here, so posts is empty and NO id key path
    assert body["posts"] == []


def test_no_creds_action_uses_drafts_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    def _boom(*a, **k):
        raise AssertionError("Supabase path must NOT run when creds are absent")
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", _boom)

    # a None drafts store with no creds -> the old _load_owned_draft path, 404 on a
    # missing draft (proves the non-Supabase branch ran without raising)
    status, body = ps.handle_approve("lasso", "d1", "U_owner", store=None)
    assert status == 404
    assert body["ok"] is False


# ---------------------------------------------------------------------------
# a per-test temp DB for the recreate-budget kv counter
# ---------------------------------------------------------------------------

@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    yield


# ===========================================================================
# RED-BANNER SIGNAL: /social carries awaiting_media + upload_url for a CLIENT
# gym with no calendar (Echo is waiting on the gym's uploaded media). The LASSO
# gym is NEVER flagged awaiting_media. Additive fields (existing contract intact).
# ===========================================================================

@pytest.fixture
def _upload_env(monkeypatch):
    """The signing secret + a real upload base so upload_link_for mints a live link."""
    monkeypatch.setenv("AGENT_INTAKE_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://upload.lassoframework.com")
    yield


def test_client_gym_no_posts_flags_awaiting_media(monkeypatch, _upload_env):
    store = _FakeStore([])          # a CLIENT gym with an EMPTY calendar
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    status, body = ps.handle_social("gritx", "2026-08")
    assert status == 200
    assert body["posts"] == []
    assert body["awaiting_media"] is True
    assert body["upload_url"], "a client awaiting media must get a non-empty upload_url"
    assert body["upload_url"].startswith("https://upload.lassoframework.com/u/")


def test_client_gym_with_posts_not_awaiting_media(monkeypatch, _upload_env):
    store = _FakeStore([_row("uuid-1", gym_id="gritx", post_date="2026-08-05")])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    status, body = ps.handle_social("gritx", "2026-08")
    assert status == 200
    assert body["posts"], "the client has a calendar"
    assert body["awaiting_media"] is False
    assert body["upload_url"] == ""


def test_lasso_gym_never_flagged_awaiting_media(monkeypatch, _upload_env):
    store = _FakeStore([])          # LASSO's OWN calendar, empty this month
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    status, body = ps.handle_social("lasso", "2026-08")
    assert status == 200
    assert body["posts"] == []
    # the LASSO gym is its own dogfood calendar: NEVER an "upload your media" banner
    assert body["awaiting_media"] is False
    assert body["upload_url"] == ""


def test_lasso_framework_llc_key_never_flagged(monkeypatch, _upload_env):
    # the live dogfood key is 'lasso-framework-llc' (still a LASSO key): never flagged
    store = _FakeStore([])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    status, body = ps.handle_social("lasso-framework-llc", "2026-08")
    assert status == 200
    assert body["awaiting_media"] is False
    assert body["upload_url"] == ""
