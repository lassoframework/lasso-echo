"""Real store contract for a duplicate rejected before the network publish call."""
import pytest

from agent import calendar_autopublish as cap, db
from agent.portal_calendar_store import SupabaseCalendarStore


class Response:
    status_code = 200
    text = ""

    def __init__(self, rows):
        self.rows = rows

    def json(self):
        return self.rows


class CalendarHTTP:
    """Apply the relevant PostgREST predicates to actual production-shaped rows."""
    def __init__(self, row):
        self.row = dict(row)
        self.calls = []

    def patch(self, url, *, params, headers, json, timeout):
        self.calls.append((params, json))
        for key, condition in params.items():
            value = self.row.get(key)
            if condition.startswith("eq.") and str(value) != condition[3:]:
                return Response([])
            if condition == "is.null" and value is not None:
                return Response([])
            if condition.startswith("not.in.(") and value in condition[8:-1].split(","):
                return Response([])
        self.row.update(json)
        return Response([dict(self.row)])


def store_for(**overrides):
    row = {"id": "dbe5fdea-61f3-46f8-a62b-643d639804ab", "gym_id": "lasso",
           "status": "publishing", "published_at": None, "late_post_id": None,
           **overrides}
    http = CalendarHTTP(row)
    store = SupabaseCalendarStore(url="https://example.invalid", service_key="fixture", http=http)
    return store, http


@pytest.fixture(autouse=True)
def no_real_audit(monkeypatch):
    monkeypatch.setattr(db, "audit", lambda *args, **kwargs: None)


def test_duplicate_claim_is_retired_even_though_portal_status_edits_refuse_it():
    store, http = store_for()
    rid = http.row["id"]
    assert store.set_status("lasso", rid, "deleted") is None
    assert http.row["status"] == "publishing"
    assert cap._mark_duplicate_content(store, "lasso", rid, "2026-09-11T23:10:40Z") is True
    assert http.row["status"] == "deleted"
    assert "duplicate content refused" in http.row["reject_reason"]
    assert http.row["published_at"] is None
    assert http.row["late_post_id"] is None


@pytest.mark.parametrize("changes", [
    {"gym_id": "another-gym"}, {"status": "published"}, {"status": "approved"},
    {"published_at": "2026-09-11T23:10:40Z"}, {"late_post_id": "provider-receipt"},
])
def test_cleanup_never_changes_other_tenants_or_rows_with_publish_evidence(changes):
    store, http = store_for(**changes)
    before = dict(http.row)
    assert cap._mark_duplicate_content(store, "lasso", http.row["id"], "earlier") is False
    assert http.row == before


def test_missing_adapter_and_failed_write_are_not_reported_as_success():
    assert cap._mark_duplicate_content(object(), "lasso", "row", "earlier") is False

    class Broken:
        def mark_duplicate_content(self, *args):
            raise RuntimeError("database unavailable")

    assert cap._mark_duplicate_content(Broken(), "lasso", "row", "earlier") is False


def test_idempotent_repeat_does_not_reactivate_the_retired_row():
    store, http = store_for()
    rid = http.row["id"]
    assert cap._mark_duplicate_content(store, "lasso", rid, "earlier") is True
    assert cap._mark_duplicate_content(store, "lasso", rid, "earlier") is False
    assert http.row["status"] == "deleted"


@pytest.mark.parametrize("previous", ["pending", "approved"])
def test_pre_network_ledger_fault_releases_real_store_claim(previous):
    store, http = store_for()
    row = {**http.row, "status": previous}
    assert cap._release_content_ledger_claim(store, "lasso", row, "content_stamp_failed") is True
    assert http.row["status"] == previous


@pytest.mark.parametrize("changes", [
    {"gym_id": "another-gym"}, {"status": "published"},
    {"published_at": "2026-09-11T12:00:00Z"}, {"late_post_id": "receipt"},
])
def test_ledger_release_cannot_overwrite_publication_or_another_tenant(changes, monkeypatch):
    from agent import ops_alerts
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", alerts.append)
    store, http = store_for(**changes)
    before = dict(http.row)
    assert cap._release_content_ledger_claim(store, "lasso", http.row, "read_failed") is False
    assert http.row == before
    assert alerts and "release was not confirmed" in alerts[0]
