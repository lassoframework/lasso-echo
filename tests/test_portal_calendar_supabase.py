"""
Tests for the Supabase-backed portal calendar data plane.

All offline: a fake HTTP client stands in for `requests`; no network, no db.

Invariants:
  1. calendar read maps content_calendar rows -> the exact portal shape.
  2. month filter is correct (gte first-of-month, lte last-of-month).
  3. approve/deny/kill PATCH the status, filtered by BOTH id AND gym_id.
  4. cross-gym draft_id (gym B row, gym A key) -> 404, NO write issued.
  5. report returns the null shape (never a fabricated 0).
  6. when creds absent, handle_portal_calendar keeps the SQLite path (unchanged).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_routes, portal_calendar_store as pcs


# ---- fake HTTP client (mimics requests: get/patch -> response obj) -------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    """Records every call; returns canned responses keyed by method."""

    def __init__(self, get_resp=None, patch_resp=None, post_resp=None,
                 delete_resp=None):
        self.calls = []
        self._get_resp = get_resp or _Resp(200, [])
        self._patch_resp = patch_resp or _Resp(200, [])
        self._post_resp = post_resp or _Resp(201, [])
        self._delete_resp = delete_resp or _Resp(200, [])

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, params or {}, headers or {}))
        return self._get_resp

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("patch", url, params or {}, headers or {}, json or {}))
        return self._patch_resp

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, params or {}, headers or {}, json))
        return self._post_resp

    def delete(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("delete", url, params or {}, headers or {}, json))
        return self._delete_resp


def _row(row_id, gym_id="lasso", post_date="2026-08-06", account="instagram",
         status="pending", caption="hello", image_url="https://cdn/x.jpg",
         pillar="education"):
    return {
        "id": row_id, "gym_id": gym_id, "post_date": post_date,
        "account": account, "status": status, "caption": caption,
        "image_url": image_url, "pillar": pillar,
    }


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    # Portal flag ON + Supabase creds present by default; individual tests
    # override where they need the SQLite path.
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")


# ---- 1. read maps rows -> exact portal shape ----------------------------------

def test_calendar_maps_rows_to_portal_shape(monkeypatch):
    rows = [_row("id-1", caption="cap", image_url="https://cdn/1.jpg", pillar="proof")]
    http = _FakeHTTP(get_resp=_Resp(200, rows))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_calendar("lasso", "2026-08")
    assert status == 200
    assert body["account_key"] == "lasso"
    assert body["month"] == "2026-08"
    d = body["drafts"][0]
    assert d == {
        "draft_id": "id-1", "day_key": "2026-08-06", "status": "pending",
        "platform": "instagram", "caption": "cap",
        "creative_public_url": "https://cdn/1.jpg", "scheduled_for": None,
        "blocked_reason": None, "pillar": "proof",
    }
    for key in ("draft_id", "day_key", "status", "platform", "caption",
                "creative_public_url", "scheduled_for", "blocked_reason", "pillar"):
        assert key in d


def test_calendar_null_caption_and_image(monkeypatch):
    rows = [_row("id-2", caption="", image_url="", pillar="")]
    http = _FakeHTTP(get_resp=_Resp(200, rows))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_calendar("lasso", "2026-08")
    d = body["drafts"][0]
    assert d["caption"] is None
    assert d["creative_public_url"] is None
    assert d["pillar"] is None


# ---- 2. month filter correctness ----------------------------------------------

def test_calendar_month_filter_bounds(monkeypatch):
    http = _FakeHTTP(get_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    portal_routes.handle_portal_calendar("lasso", "2026-08")
    method, url, params, headers = http.calls[0]
    assert method == "get"
    assert url.endswith("/rest/v1/content_calendar")
    assert params["gym_id"] == "eq.lasso"
    # August has 31 days.
    assert params["post_date"] == ["gte.2026-08-01", "lte.2026-08-31"]
    assert params["order"] == "post_date"


def test_calendar_month_filter_february(monkeypatch):
    http = _FakeHTTP(get_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    portal_routes.handle_portal_calendar("lasso", "2026-02")
    _, _, params, _ = http.calls[0]
    assert params["post_date"] == ["gte.2026-02-01", "lte.2026-02-28"]


# ---- 3. approve/deny/kill PATCH status with id+gym_id filter -------------------

@pytest.mark.parametrize("action,expected_status", [
    ("approve", "approved"),
    ("deny", "denied"),
    ("kill", "killed"),
])
def test_action_patches_status_with_isolation_filter(monkeypatch, action, expected_status):
    pre = _Resp(200, [_row("id-9", gym_id="lasso")])
    patched = _Resp(200, [_row("id-9", gym_id="lasso", status=expected_status)])
    http = _FakeHTTP(get_resp=pre, patch_resp=patched)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_action(action, "lasso", "id-9",
                                                      "actor-1", confirm=True)
    assert status == 200
    assert body == {"ok": True, "action": action, "draft_id": "id-9"}

    # One GET (pre-check) then one PATCH; both scoped by gym_id.
    get_call = [c for c in http.calls if c[0] == "get"][0]
    patch_call = [c for c in http.calls if c[0] == "patch"][0]
    assert get_call[2]["id"] == "eq.id-9"
    assert get_call[2]["gym_id"] == "eq.lasso"
    assert patch_call[2]["id"] == "eq.id-9"
    assert patch_call[2]["gym_id"] == "eq.lasso", "isolation filter must be on the PATCH"
    assert patch_call[4] == {"status": expected_status}


def test_action_edit_writes_caption_and_reverts_to_pending(monkeypatch):
    pre = _Resp(200, [_row("id-e", gym_id="lasso", status="approved")])
    patched = _Resp(200, [_row("id-e", gym_id="lasso", status="pending",
                               caption="shorter wording")])
    http = _FakeHTTP(get_resp=pre, patch_resp=patched)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_action(
        "edit", "lasso", "id-e", "actor-1", note="shorter wording")
    assert status == 200
    assert body["ok"] is True
    assert body["caption"] == "shorter wording"
    assert body["status"] == "pending"
    # edit MUST issue a patch with caption + status
    patches = [c for c in http.calls if c[0] == "patch"]
    assert patches, "edit must write caption to the database"
    payload = patches[0][4]  # json arg
    assert payload.get("caption") == "shorter wording"
    assert payload.get("status") == "pending"


def test_action_edit_empty_note_returns_400(monkeypatch):
    pre = _Resp(200, [_row("id-e", gym_id="lasso", status="pending")])
    http = _FakeHTTP(get_resp=pre)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_action(
        "edit", "lasso", "id-e", "actor-1", note="")
    assert status == 400
    assert "required" in body.get("error", "")
    assert not [c for c in http.calls if c[0] == "patch"]


# ---- 4. CROSS-GYM isolation: gym B row, gym A key -> 404, NO write -------------

def test_cross_gym_action_returns_404_and_no_write(monkeypatch):
    # Pre-check scoped to gym A returns nothing (the row is gym B's).
    http = _FakeHTTP(get_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_action("approve", "gymA", "id-of-gymB", "actor")
    assert status == 404
    assert body == {"ok": False, "error": "draft not found", "draft_id": "id-of-gymB"}
    # Critical: no PATCH ever issued.
    assert not [c for c in http.calls if c[0] == "patch"]


def test_action_patch_zero_rows_returns_404(monkeypatch):
    # Row passes the pre-check but the PATCH matches zero rows (e.g. a race):
    # still a 404, never a false success.
    pre = _Resp(200, [_row("id-x", gym_id="lasso")])
    http = _FakeHTTP(get_resp=pre, patch_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_action("approve", "lasso", "id-x", "actor")
    assert status == 404
    assert body["ok"] is False


def test_action_patch_wrong_gym_in_response_returns_404(monkeypatch):
    # Defensive: even if a PATCH somehow echoed a foreign gym_id, reject it.
    pre = _Resp(200, [_row("id-x", gym_id="lasso")])
    http = _FakeHTTP(get_resp=pre, patch_resp=_Resp(200, [_row("id-x", gym_id="other")]))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_action("approve", "lasso", "id-x", "actor")
    assert status == 404


# ---- 5. patch_caption writes caption + resets status to pending ---------------

def test_patch_caption_writes_caption_and_status(monkeypatch):
    patched = _Resp(200, [_row("id-c", gym_id="lasso", status="pending",
                                caption="new caption text")])
    http = _FakeHTTP(patch_resp=patched)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    result = pcs.SupabaseCalendarStore().patch_caption("lasso", "id-c", "new caption text")
    assert result is not None
    assert result["caption"] == "new caption text"
    assert result["status"] == "pending"
    method, url, params, headers, payload = http.calls[0]
    assert method == "patch"
    assert params["id"] == "eq.id-c"
    assert params["gym_id"] == "eq.lasso"
    assert payload == {"caption": "new caption text", "status": "pending"}


def test_patch_caption_cross_gym_returns_none(monkeypatch):
    # PATCH returns a row for a different gym — reject it.
    patched = _Resp(200, [_row("id-c", gym_id="other", caption="new")])
    http = _FakeHTTP(patch_resp=patched)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    result = pcs.SupabaseCalendarStore().patch_caption("lasso", "id-c", "new")
    assert result is None


# ---- 6. report returns the null shape (no zeros) ------------------------------

def test_report_null_shape(monkeypatch):
    status, body = portal_routes.handle_portal_report("lasso", "14")
    assert status == 200
    assert body["account_key"] == "lasso"
    assert body["window_days"] == 14
    for key in ("posts_published", "engagement_rate", "likes", "comments",
                "saves", "shares", "views", "reach", "follower_delta"):
        assert body[key] is None, f"{key} must be null, never a fabricated 0"
    assert body["health"] == {"label": None}
    assert body["top_posts"] == []
    assert isinstance(body["gaps"], list) and body["gaps"]


def test_report_flag_off_returns_403(monkeypatch):
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)
    status, body = portal_routes.handle_portal_report("lasso", "30")
    assert status == 403


# ---- 6. no creds -> SQLite path unchanged -------------------------------------

def test_no_creds_uses_sqlite_path(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert config_supabase_disabled()

    # If it tried Supabase it would need a client; instead it must hit the db
    # path. Force the db.connect to prove the SQLite branch is taken.
    hit = {"db": False}

    class _FakeConn:
        def __enter__(self):
            hit["db"] = True
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            class _Cur:
                def fetchall(self_inner):
                    return []
            return _Cur()

    monkeypatch.setattr("agent.portal_routes._db.connect", lambda: _FakeConn())
    status, body = portal_routes.handle_portal_calendar("lasso", "2026-08")
    assert status == 200
    assert hit["db"] is True, "SQLite path must run when creds absent"
    assert body["drafts"] == []


def test_no_creds_action_uses_portal_approvals(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    calls = []

    def _fake_approve(account_key, draft_id, actor_id, store=None, **kw):
        calls.append((account_key, draft_id, actor_id))
        return {"ok": True, "action": "approve", "draft_id": draft_id}

    monkeypatch.setattr("agent.portal_routes._pa.approve", _fake_approve)
    status, body = portal_routes.handle_portal_action("approve", "lasso", "d1", "actor")
    assert status == 200
    assert calls == [("lasso", "d1", "actor")], "must delegate to portal_approvals when no creds"


def config_supabase_disabled():
    from agent import config
    return not config.portal_calendar_supabase_enabled()


# ---- secret hygiene -----------------------------------------------------------

def test_service_key_never_in_error(monkeypatch):
    # A 500 from Supabase must not leak the key even if the body echoes it.
    err_text = "boom svc-key-secret boom"
    http = _FakeHTTP(get_resp=_Resp(500, [], text=err_text))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)

    status, body = portal_routes.handle_portal_calendar("lasso", "2026-08")
    assert status == 500
    # The handler returns only the exception type name, never the detail text.
    assert "svc-key-secret" not in str(body)


# ---- 7. insert_rows: NO id sent, DB uuid returned; delete_month gym+month scoped ----
# These prove the fix for the 22P02 bug: content_calendar.id is a DB-generated uuid, so
# a write must NOT send `id`. The apply path is delete-then-insert.

def test_insert_rows_sends_no_id_and_forces_gym(monkeypatch):
    import uuid
    new_id = str(uuid.uuid4())
    returned = [{"id": new_id, "gym_id": "lasso", "post_date": "2026-08-06",
                 "account": "instagram", "status": "pending", "caption": "hi",
                 "image_url": "u", "pillar": "proof", "format": "feed"}]
    http = _FakeHTTP(post_resp=_Resp(201, returned))
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-key-secret", http=http)
    # Caller hands rows that (defensively) include a non-uuid id and a foreign gym; both
    # must be corrected: id stripped, gym forced.
    rows = [{"id": "demof_lasso_2026-08-06_feed", "gym_id": "someone_else",
             "post_date": "2026-08-06", "caption": "hi", "format": "feed"}]
    out = store.insert_rows("lasso", rows)

    post_call = [c for c in http.calls if c[0] == "post"][0]
    sent = post_call[4]
    assert isinstance(sent, list) and len(sent) == 1
    assert "id" not in sent[0], "insert must NOT send id (DB generates the uuid)"
    assert sent[0]["gym_id"] == "lasso", "gym_id forced to the account key"
    # on_conflict/upsert is gone: a plain insert, no query params.
    assert post_call[2] == {}
    # The DB-returned uuid comes back.
    assert out == returned
    assert uuid.UUID(out[0]["id"])


def test_insert_rows_empty_is_noop(monkeypatch):
    http = _FakeHTTP()
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc", http=http)
    assert store.insert_rows("lasso", []) == []
    assert not [c for c in http.calls if c[0] == "post"], "no POST for empty rows"


def test_insert_rows_normalizes_heterogeneous_keys(monkeypatch):
    # PostgREST PGRST102: every object in a batch must have the SAME keys. A video row
    # carries thumbnail_url, a photo row doesn't -> the mixed batch used to 400 the whole
    # insert (GritX rebuild stuck). Every object must go out with the union of keys.
    http = _FakeHTTP(post_resp=_Resp(201, []))
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-key-secret", http=http)
    rows = [
        {"post_date": "2026-08-14", "format": "feed", "caption": "video post",
         "image_url": "u.mp4", "thumbnail_url": "poster.jpg"},   # has thumbnail
        {"post_date": "2026-08-15", "format": "feed", "caption": "photo post",
         "image_url": "p.jpg"},                                   # NO thumbnail
    ]
    store.insert_rows("gritx", rows)
    sent = [c for c in http.calls if c[0] == "post"][0][4]
    keysets = [frozenset(o.keys()) for o in sent]
    assert len(set(keysets)) == 1, "all objects in the batch must have identical keys"
    assert "thumbnail_url" in keysets[0]
    # the photo row's missing thumbnail is present as None (not absent)
    photo = next(o for o in sent if o["caption"] == "photo post")
    assert photo["thumbnail_url"] is None


def test_delete_month_scoped_by_gym_and_month(monkeypatch):
    deleted = [{"id": "u1", "gym_id": "lasso", "post_date": "2026-08-10"}]
    http = _FakeHTTP(delete_resp=_Resp(200, deleted))
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc", http=http)
    n = store.delete_month("lasso", "2026-08")
    assert n == 1
    del_call = [c for c in http.calls if c[0] == "delete"][0]
    params = del_call[2]
    assert params["gym_id"] == "eq.lasso", "gym scope on the DELETE"
    assert params["post_date"] == ["gte.2026-08-01", "lte.2026-08-31"], "month bounds"


def test_delete_month_ignores_foreign_gym_rows_in_response(monkeypatch):
    # Defensive: even if the DELETE echoed a foreign gym row, it is not counted.
    resp = [{"id": "u1", "gym_id": "lasso", "post_date": "2026-08-10"},
            {"id": "u2", "gym_id": "other", "post_date": "2026-08-11"}]
    http = _FakeHTTP(delete_resp=_Resp(200, resp))
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc", http=http)
    assert store.delete_month("lasso", "2026-08") == 1


# ---- GATE 2 store helpers: first-month signal + coach release -----------------

def test_has_owner_visible_rows_true_when_non_coach_review_exists():
    http = _FakeHTTP(get_resp=_Resp(200, [{"id": "x"}]))
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc", http=http)
    assert store.has_owner_visible_rows("gritx") is True
    _, _, params, _ = http.calls[-1]
    assert params["gym_id"] == "eq.gritx"
    assert params["status"] == "neq.coach_review"   # coach_review rows don't count


def test_has_owner_visible_rows_false_when_empty():
    http = _FakeHTTP(get_resp=_Resp(200, []))
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc", http=http)
    assert store.has_owner_visible_rows("gritx") is False


def test_release_coach_review_flips_all_platforms_to_pending():
    released = [{"id": "1", "account": "instagram"}, {"id": "2", "account": "facebook"}]
    http = _FakeHTTP(patch_resp=_Resp(200, released))
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc", http=http)
    out = store.release_coach_review("gritx")
    assert len(out) == 2
    _, _, params, _, body = http.calls[-1]
    assert params["gym_id"] == "eq.gritx"
    assert params["status"] == "eq.coach_review"     # only withheld rows
    assert "account" not in params                    # every platform, one shot
    assert body == {"status": "pending"}


# ---- G2 requeue: failed-row recovery + words-changed routing ------------------

def test_requeue_failed_no_change_back_to_approved(monkeypatch):
    pre = _Resp(200, [_row("id-f", gym_id="lasso", status="failed")])
    patched = _Resp(200, [_row("id-f", gym_id="lasso", status="approved")])
    http = _FakeHTTP(get_resp=pre, patch_resp=patched)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    status, body = portal_routes.handle_portal_action("requeue", "lasso", "id-f", "a1")
    assert status == 200 and body["ok"] is True
    assert body["status"] == "approved" and body["words_changed"] is False
    payload = [c for c in http.calls if c[0] == "patch"][0][4]
    assert payload["status"] == "approved"        # straight back to the queue
    assert payload["reject_reason"] == ""          # failure cleared
    assert "caption" not in payload                # words unchanged -> no caption write


def test_requeue_failed_with_word_change_reenters_owner_approval(monkeypatch):
    pre = _Resp(200, [_row("id-f", gym_id="lasso", status="failed", caption="old words")])
    patched = _Resp(200, [_row("id-f", gym_id="lasso", status="pending",
                               caption="a calmer rewrite for the day")])
    http = _FakeHTTP(get_resp=pre, patch_resp=patched)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    status, body = portal_routes.handle_portal_action(
        "requeue", "lasso", "id-f", "a1", note="a calmer rewrite for the day")
    assert status == 200 and body["words_changed"] is True
    assert body["status"] == "pending"             # owner must re-approve changed words
    payload = [c for c in http.calls if c[0] == "patch"][0][4]
    assert payload["status"] == "pending"
    assert payload["caption"] == "a calmer rewrite for the day"
    assert payload["reject_reason"] == ""


def test_requeue_rejects_non_failed_row(monkeypatch):
    pre = _Resp(200, [_row("id-p", gym_id="lasso", status="pending")])
    http = _FakeHTTP(get_resp=pre)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    status, body = portal_routes.handle_portal_action("requeue", "lasso", "id-p", "a1")
    assert status == 409 and "only a failed" in body["error"].lower()
    assert [c for c in http.calls if c[0] == "patch"] == []   # never writes


def test_requeue_word_change_hits_fabrication_gate(monkeypatch):
    pre = _Resp(200, [_row("id-f", gym_id="lasso", status="failed", caption="old")])
    http = _FakeHTTP(get_resp=pre)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    status, body = portal_routes.handle_portal_action(
        "requeue", "lasso", "id-f", "a1", note="We grew this gym 300% last month")
    assert status == 422 and "fabrication" in body["error"].lower()
    assert [c for c in http.calls if c[0] == "patch"] == []   # blocked before any write


def test_requeue_refused_on_legacy_plane():
    # requeue only exists on the content_calendar plane; a passed store bypasses supabase
    status, body = portal_routes.handle_portal_action(
        "requeue", "lasso", "id", "a1", store=object())
    assert status == 400 and "content_calendar" in body["error"]


# ---- G1 edit accepts + persists GBP structured fields ------------------------

def test_edit_persists_gbp_structured_fields(monkeypatch):
    pre = _Resp(200, [_row("id-g", gym_id="lasso", status="approved")])
    patched = _Resp(200, [_row("id-g", gym_id="lasso", status="pending")])
    http = _FakeHTTP(get_resp=pre, patch_resp=patched)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    gbp = {"topicType": "EVENT", "ctaType": "BOOK", "ctaUrl": "https://gym.com/e",
           "event": {"schedule": {"startDate": "2026-10-01", "endDate": "2026-10-01"}}}
    status, body = portal_routes.handle_portal_action(
        "edit", "lasso", "id-g", "a1", gbp=gbp)
    assert status == 200 and body["ok"] is True
    assert set(body["gbp_updated"]) == {"gbp_topic_type", "gbp_cta_type", "gbp_cta_url",
                                        "gbp_event"}
    payload = [c for c in http.calls if c[0] == "patch"][-1][4]
    assert payload["gbp_topic_type"] == "EVENT" and payload["gbp_cta_type"] == "BOOK"
    assert payload["gbp_cta_url"] == "https://gym.com/e"
    assert payload["status"] == "pending"      # a structured edit resets approval


def test_edit_rejects_bad_gbp_topic_before_write(monkeypatch):
    pre = _Resp(200, [_row("id-g", gym_id="lasso", status="approved")])
    http = _FakeHTTP(get_resp=pre)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    status, body = portal_routes.handle_portal_action(
        "edit", "lasso", "id-g", "a1", gbp={"topicType": "REEL"})
    assert status == 422 and "topic" in body["error"].lower()
    assert [c for c in http.calls if c[0] == "patch"] == []    # rejected before any write


def test_edit_requires_note_or_gbp(monkeypatch):
    pre = _Resp(200, [_row("id-g", gym_id="lasso", status="approved")])
    http = _FakeHTTP(get_resp=pre)
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    status, body = portal_routes.handle_portal_action("edit", "lasso", "id-g", "a1")
    assert status == 400 and "caption" in body["error"].lower()
