"""PR268 armed-trigger compatibility for future-infographic hold/release CAS.

The armed visual-group claim trigger (migrations/DRAFT_visual_group_claim_trigger_20261002.sql,
the ``visual media not ready for approval, claim or finalize`` check) rejects any
UPDATE leaving an approved/publishing row with a non-null media_not_ready_reason.
So the protective hold must demote approved placeholders to pending in the same
atomic CAS, and an ordinary release must never reactivate scene-held or archived
rows. These tests run without a live Postgres by faking the REST transport; a
static check pins the armed trigger clause so drift fails loudly.
"""
import pathlib

import pytest

from agent.portal_calendar_store import SupabaseCalendarStore

REASON = "Photo-first hold: unverified infographic placeholder; approved gym photo required"
MIGRATION = (pathlib.Path(__file__).resolve().parent.parent
             / "migrations" / "DRAFT_visual_group_claim_trigger_20261002.sql")


def row(id_, status, **overrides):
    base = {"id": id_, "gym_id": "gym", "post_date": "2026-10-03",
            "status": status, "variant_status": "active",
            "account": "instagram", "format": "feed",
            "caption": "original caption",
            "image_url": "https://r2/igfill_2026-10-03_card.png",
            "source_media_url": None, "source_media_asset_id": None,
            "media_not_ready_reason": None, "created_at": "2026-10-01T00:00:00Z",
            "published_at": None, "late_post_id": None}
    base.update(overrides)
    return base


class Response:
    def __init__(self, rows, status_code=200):
        self.status_code, self.rows = status_code, rows

    def json(self):
        return self.rows


class Http:
    """Fake transport: applies the payload to a copy of `serves` if matched."""

    def __init__(self, serves, mutate=None):
        self.serves, self.calls, self.mutate = serves, [], mutate

    def patch(self, url, **kwargs):
        self.calls.append(kwargs)
        after = dict(self.serves)
        if self.mutate:
            self.mutate(after)
        else:
            after.update(kwargs["json"])
        return Response([after])


def store_for(http):
    return SupabaseCalendarStore(url="https://test.supabase.co",
                                 service_key="fake", http=http)


def test_armed_trigger_rejects_approved_with_hold_reason_static():
    """Pin the PR268 clause that made approved+hold_reason an error."""
    sql = MIGRATION.read_text()
    assert "new.status in ('approved','publishing')" in sql.replace("\n", " ")
    assert "new.media_not_ready_reason is not null" in sql
    assert "visual media not ready for approval, claim or finalize" in sql


def test_hold_pending_row_changes_only_reason():
    before = row("p1", "pending")
    http = Http(before)
    after = store_for(http).hold_future_infographic_media("gym", before, REASON)
    assert after["status"] == "pending"
    assert http.calls[0]["json"] == {"media_not_ready_reason": REASON}
    assert http.calls[0]["params"]["status"] == "eq.pending"


def test_hold_approved_row_demotes_to_pending_in_same_cas():
    before = row("a1", "approved")
    http = Http(before)
    after = store_for(http).hold_future_infographic_media("gym", before, REASON)
    assert after["status"] == "pending"
    assert after["media_not_ready_reason"] == REASON
    call = http.calls[0]
    # predicate still matches the approved before image exactly
    assert call["params"]["status"] == "eq.approved"
    assert call["params"]["published_at"] == "is.null"
    assert call["params"]["late_post_id"] == "is.null"
    # single atomic write: hold + demotion, nothing else
    assert call["json"] == {"media_not_ready_reason": REASON, "status": "pending"}


def test_hold_approved_row_still_approved_afterwards_is_false_success():
    """If the server reports the row still approved with the reason set, the
    write cannot have survived the armed trigger's intent; refuse success."""
    before = row("a1", "approved")
    http = Http(before, mutate=lambda r: r.update({"media_not_ready_reason": REASON}))
    assert store_for(http).hold_future_infographic_media("gym", before, REASON) is None


def test_hold_stale_cas_conflict_returns_none():
    before = row("a1", "approved")

    class EmptyHttp:
        def __init__(self):
            self.calls = []

        def patch(self, url, **kwargs):
            self.calls.append(kwargs)
            return Response([])

    http = EmptyHttp()
    assert store_for(http).hold_future_infographic_media("gym", before, REASON) is None
    assert http.calls  # CAS attempted, matched zero rows

    drifted = Http(before, mutate=lambda r: r.update(
        {"media_not_ready_reason": REASON, "status": "pending",
         "image_url": "https://r2/swapped.png"}))
    assert store_for(drifted).hold_future_infographic_media("gym", before, REASON) is None


@pytest.mark.parametrize("status,overrides", [
    ("published", {}),
    ("publishing", {}),
    ("approved", {"published_at": "2026-10-03T01:00:00Z"}),
    ("approved", {"late_post_id": "late-1"}),
    ("approved", {"variant_status": "archived"}),
    ("approved", {"media_not_ready_reason": "scene_review_hold"}),
])
def test_hold_refuses_protected_rows_without_http(status, overrides):
    http = Http(row("x", status))
    before = row("x", status, **overrides)
    assert store_for(http).hold_future_infographic_media("gym", before, REASON) is None
    assert http.calls == []


def test_release_clears_hold_but_never_restores_approval():
    held = row("a1", "pending", media_not_ready_reason=REASON)
    http = Http(held)
    after = store_for(http).release_future_infographic_media("gym", held, REASON)
    assert after["media_not_ready_reason"] is None
    assert after["status"] == "pending"  # later reapproval required
    assert http.calls[0]["json"] == {"media_not_ready_reason": None}
    assert "status" not in http.calls[0]["json"]


def test_release_cannot_reactivate_archived_scene_review_hold():
    archived = row("s1", "pending", variant_status="archived",
                   media_not_ready_reason="scene_review_hold")
    http = Http(archived)
    store = store_for(http)
    assert store.release_future_infographic_media(
        "gym", archived, "scene_review_hold") is None
    # even an active scene_review_hold row is not an ordinary release target
    active_scene = row("s2", "pending", media_not_ready_reason="scene_review_hold")
    assert store.release_future_infographic_media(
        "gym", active_scene, "scene_review_hold") is None
    assert http.calls == []


def test_release_wrong_reason_and_stale_conflict_return_none():
    held = row("p1", "pending", media_not_ready_reason=REASON)
    http = Http(held)
    store = store_for(http)
    assert store.release_future_infographic_media("gym", held, "other") is None
    assert http.calls == []

    class EmptyHttp:
        def patch(self, url, **kwargs):
            return Response([])

    assert store_for(EmptyHttp()).release_future_infographic_media(
        "gym", held, REASON) is None
