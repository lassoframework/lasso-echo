from pathlib import Path
import pytest

from agent.jobs import lasso_paired_story_repair as repair
from test_lasso_paired_story_backfill import FEED_ID, LOGICAL_ID, DAY, STORY_URL, DIGEST

STORY_ID = "f4505c63-6352-44aa-bea5-cf78f0be587d"


def setup(monkeypatch):
    feed = {"id": FEED_ID, "gym_id": "lasso", "account": "instagram",
            "format": "feed", "post_date": DAY, "slot_index": 0,
            "variant_status": "active", "status": "pending", "pillar": "doctrine",
            "caption": "Current exact caption", "image_url": "https://cdn.example/feed.png",
            "scheduled_at": None, "logical_post_id": LOGICAL_ID,
            "media_not_ready_reason": None, "published_at": None, "late_post_id": None}
    story = {"id": STORY_ID, "gym_id": "lasso", "account": "instagram",
             "format": "story", "post_date": DAY, "slot_index": 0,
             "variant_status": "active", "status": "pending", "pillar": "platform",
             "caption": "", "image_url": "https://cdn.example/old-story.png",
             "source_media_url": "https://cdn.example/old-story.png",
             "scheduled_at": None, "logical_post_id": LOGICAL_ID,
             "media_not_ready_reason": "paired_feed_not_ready",
             "published_at": None, "late_post_id": None, "publish_claim_token": None}
    artifact = {"image_sha256": DIGEST, "evidence": {"grade_status": "PASS",
                "image_sha256": DIGEST, "policy_version": "v1", "aspect": "9:16"},
                "source_identity": {"source_id": f"content_calendar:{FEED_ID}:caption",
                "source_hash": repair.hashlib.sha256(feed["caption"].encode()).hexdigest()}}
    monkeypatch.setattr(repair.base, "_feed", lambda *_: feed)
    monkeypatch.setattr(repair.base, "_artifact", lambda *_: artifact)
    monkeypatch.setattr(repair.base, "_one", lambda *_: story)
    monkeypatch.setattr(repair.base, "_active_day", lambda *_: [feed, story])
    monkeypatch.setattr(repair.base, "_scheduled_after_feed", lambda *_: "2026-10-07T07:45:00-04:00")
    item = {"story_id": STORY_ID, "feed_id": FEED_ID,
            "story_image_url": STORY_URL, "story_sha256": DIGEST,
            "artifact_tenant": "lasso_ig", "policy_version": "v1"}
    return object(), item, story, feed, artifact


def test_repair_plan_exact_pending_pair_and_null_schedule(monkeypatch):
    store, item, story, feed, _ = setup(monkeypatch)
    action = repair.plan_one(store, item)
    assert action["expected_story"]["image_url"] == story["image_url"]
    assert action["expected_feed"]["scheduled_at"] is None
    assert action["story_scheduled_at"] == "2026-10-07T07:45:00-04:00"
    assert action["source_hash"] == repair.hashlib.sha256(feed["caption"].encode()).hexdigest()


def test_repair_refuses_claimed_published_and_logical_mismatch(monkeypatch):
    store, item, story, feed, _ = setup(monkeypatch)
    story["publish_claim_token"] = "claimed"
    with pytest.raises(ValueError, match="unclaimed"):
        repair.plan_one(store, item)
    story["publish_claim_token"] = None
    story["status"] = "published"
    with pytest.raises(ValueError, match="unclaimed"):
        repair.plan_one(store, item)
    story["status"] = "pending"
    story["logical_post_id"] = None
    with pytest.raises(ValueError, match="exact feed pair"):
        repair.plan_one(store, item)


def test_repair_refuses_ambiguous_feed_or_stale_artifact(monkeypatch):
    store, item, story, feed, artifact = setup(monkeypatch)
    monkeypatch.setattr(repair.base, "_active_day", lambda *_: [feed, dict(feed, id="other"), story])
    with pytest.raises(ValueError, match="ambiguous"):
        repair.plan_one(store, item)
    monkeypatch.setattr(repair.base, "_active_day", lambda *_: [feed, story])
    feed["caption"] = "changed"
    with pytest.raises(ValueError, match="does not prove"):
        repair.plan_one(store, item)


def test_repair_refuses_slot_with_historical_published_story(monkeypatch):
    store, item, story, feed, _ = setup(monkeypatch)
    historical = {"id": "older", "account": "instagram", "format": "story",
                  "slot_index": 0, "status": "published", "variant_status": "active",
                  "published_at": "2026-10-07T12:00:00Z", "late_post_id": "late"}
    monkeypatch.setattr(repair.base, "_active_day", lambda *_: [feed, story, historical])
    with pytest.raises(ValueError, match="historical Story already published"):
        repair.plan_one(store, item)


def test_repair_dry_run_and_rpc_cas(monkeypatch):
    store, item, _, _, _ = setup(monkeypatch)
    original_apply = repair.apply_one
    monkeypatch.setattr(repair, "apply_one", lambda *_: pytest.fail("dry-run wrote"))
    assert repair.run({"repairs": [item]}, store)[0]["receipt"]["result"] == "dry_run"
    monkeypatch.setattr(repair, "apply_one", original_apply)
    action = repair.plan_one(store, item)
    class Client:
        def post(self, path, *, headers, json, timeout):
            assert path == repair.RPC
            assert json["p_expected_story"]["pillar"] == "platform"
            assert json["p_expected_feed"]["pillar"] == "doctrine"
            assert json["p_expected_feed"]["scheduled_at"] is None
            return type("R", (), {"status_code": 200, "json": lambda self: {
                "result": "repaired", "id": STORY_ID, "hold_reason": "paired_feed_not_ready"}})()
    class Store:
        def _client(self): return Client()
        def _rest(self, path): return path
        def _headers(self, extra=None): return extra or {}
    assert repair.apply_one(Store(), action)["result"] == "repaired"


def test_repair_sql_preserves_rows_and_enforces_claim_source():
    sql = (Path(__file__).resolve().parents[1] / "migrations" /
           "lasso_paired_story_repair_20261005.sql").read_text()
    assert "for update" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "lasso_story_current_source" in sql
    assert "lasso_story_publish_source_guard" in sql
    assert "lasso_managed_paired_stories" in sql
    assert "where m.story_id = old.id" in sql
    assert "publish_claim_token is not null" in sql
    assert "grant execute on function public.repair_lasso_paired_story" in sql
    assert "delete from" not in sql.lower()
