"""Planner rerenders cannot replace a source-bound managed LASSO Story."""

import pytest

from agent import portal_calendar_store as pcs

STORY = "f4505c63-6352-44aa-bea5-cf78f0be587d"


def _rows(hold):
    retained = {"id": STORY, "gym_id": "lasso", "account": "instagram",
                "post_date": "2026-10-06", "format": "story", "time_slot": "morning",
                "slot_index": 0, "status": "pending", "variant_status": "active",
                "created_at": "2026-10-05T12:00:00Z",
                "image_url": "https://example.com/source-bound.png",
                "media_not_ready_reason": hold}
    proposed = dict(retained, id="planner-proposal",
                    image_url="https://example.com/new-planner-media.png",
                    media_not_ready_reason=None)
    return retained, proposed


@pytest.mark.parametrize("hold", ["paired_feed_not_ready", None])
def test_managed_story_preserved_even_after_hold_clears(hold):
    retained, proposed = _rows(hold)
    class Store:
        def rows_in_range(self, *args): return [retained]
        def managed_lasso_paired_story_ids(self, ids):
            assert ids == [STORY]
            return {STORY}
        def recover_story_media_hold(self, *args):
            pytest.fail("managed Story media overwritten")
    output, recovered = pcs._reconcile_story_media_holds(Store(), "lasso", [proposed])
    assert output == recovered == []


def test_registry_read_failure_preserves_all_retained_lasso_stories():
    retained, proposed = _rows(None)
    class Store:
        def rows_in_range(self, *args): return [retained]
        def managed_lasso_paired_story_ids(self, ids):
            raise RuntimeError("registry unavailable")
        def recover_story_media_hold(self, *args):
            pytest.fail("unconfirmed managed Story media overwritten")
    output, recovered = pcs._reconcile_story_media_holds(Store(), "lasso", [proposed])
    assert output == recovered == []


def test_unregistered_pending_story_keeps_existing_recovery_path():
    retained, proposed = _rows("Story media not ready: render failed")
    class Store:
        def rows_in_range(self, *args): return [retained]
        def managed_lasso_paired_story_ids(self, ids): return set()
        def recover_story_media_hold(self, gym, current, next_row):
            assert gym == "lasso" and current == retained and next_row == proposed
            return dict(current, image_url=next_row["image_url"],
                        media_not_ready_reason=None)
    output, recovered = pcs._reconcile_story_media_holds(Store(), "lasso", [proposed])
    assert output == [] and len(recovered) == 1
    assert recovered[0]["image_url"] == proposed["image_url"]


def test_registry_read_returns_only_exact_uuid_membership():
    class HTTP:
        def get(self, url, *, params, headers, timeout):
            assert url.endswith("/lasso_managed_paired_stories")
            assert params["story_id"] == f"in.({STORY})"
            return type("R", (), {"status_code": 200,
                "json": lambda self: [{"story_id": STORY,
                                       "feed_id": "c1af4c9f-5abe-5cbf-84c4-a508e26f5ead"}]})()
    store = pcs.SupabaseCalendarStore(url="https://example.test", service_key="test", http=HTTP())
    assert store.managed_lasso_paired_story_ids([STORY]) == {STORY}


def test_registry_read_rejects_out_of_scope_response():
    class HTTP:
        def get(self, url, *, params, headers, timeout):
            return type("R", (), {"status_code": 200,
                "json": lambda self: [{"story_id": "other", "feed_id": "feed"}]})()
    store = pcs.SupabaseCalendarStore(url="https://example.test", service_key="test", http=HTTP())
    with pytest.raises(RuntimeError, match="malformed"):
        store.managed_lasso_paired_story_ids([STORY])
