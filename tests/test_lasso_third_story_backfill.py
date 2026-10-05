import json
from pathlib import Path

import pytest

from agent.jobs import lasso_third_story_backfill as job


CATALOG = Path(__file__).resolve().parents[1] / "brand_voice" / "lasso_summit_daily.json"
DAY = "2026-10-03"
FEED_ID = "27c09ca8-cfc6-463d-a511-ff50b833819f"
FEED_URL = "https://media.example/feed.png"
STORY_URL = "https://media.example/story.png"
DIGEST = "a" * 64
LOGICAL = "319e7d10-7d82-4545-8d95-aee4bb2b1168"


def test_repaired_feed_artifact_can_use_ig_tenant(monkeypatch):
    seen = []
    def fake_read(_store, _table, params):
        seen.append(params["tenant"])
        return [{"tenant": "lasso_ig"}] if params["tenant"] == "eq.lasso_ig" else []
    monkeypatch.setattr(job, "_read", fake_read)
    assert job._artifact(object(), FEED_URL, ("lasso", "lasso_ig"))["tenant"] == "lasso_ig"
    assert seen == ["eq.lasso", "eq.lasso_ig"]


def test_malformed_manifest_item_isolated(monkeypatch):
    monkeypatch.setattr(job, "plan_one", lambda _store, item, _catalog: {
        "date": item["date"], "story_id": "story", "feed_id": "feed"})
    results = job.run({"stories": [{"feed_id": "missing-date"}, {"date": DAY}]},
                      object(), CATALOG)
    assert results[0]["receipt"]["result"] == "blocked"
    assert results[1]["receipt"]["result"] == "dry_run"


def fixture(monkeypatch, *, logical=LOGICAL, occupied=None, aspect="9:16"):
    entry = job._catalog_entry(DAY, catalog_path=CATALOG)
    source, _ = job._source_identity(entry)
    feed = {"id": FEED_ID, "gym_id": "lasso", "account": "instagram",
            "format": "feed", "post_date": DAY, "slot_index": 2,
            "pillar": "summit", "variant_status": "active", "status": "pending",
            "caption": entry["caption"], "image_url": FEED_URL,
            "logical_post_id": logical}
    feed_artifact = {"image_url": FEED_URL, "image_sha256": "b" * 64,
                     "source_identity": {"source_hash": source["source_hash"]},
                     "evidence": {"grade_status": "PASS", "image_sha256": "b" * 64}}
    story_artifact = {"image_url": STORY_URL, "image_sha256": DIGEST,
                      "source_identity": {"source_hash": source["source_hash"],
                                          "source_feed_id": FEED_ID,
                                          "source_feed_image_url": FEED_URL},
                      "evidence": {"grade_status": "PASS", "image_sha256": DIGEST,
                                   "policy_version": "reviewed-v1", "aspect": aspect}}
    monkeypatch.setattr(job, "_feed", lambda _store, _id: feed)
    monkeypatch.setattr(job, "_artifact", lambda _store, url, *_tenants:
                        feed_artifact if url == FEED_URL else story_artifact)
    monkeypatch.setattr(job, "_story_rows", lambda _store, _day: occupied or [])
    item = {"date": DAY, "feed_id": FEED_ID, "story_image_url": STORY_URL,
            "story_sha256": DIGEST, "policy_version": "reviewed-v1",
            "scheduled_at": "2026-10-03T12:30:00+00:00"}
    return item, feed, story_artifact


def test_dry_run_binds_reviewed_story_to_exact_feed_and_legacy_null(monkeypatch):
    item, _, _ = fixture(monkeypatch, logical=None)
    action = job.plan_one(object(), item, CATALOG)
    assert action["status"] == "ready"
    assert action["logical_post_id"] is None
    assert action["story_id"] == job.story_id(DAY)
    assert action["story_image_url"] != action["feed_image_url"]
    monkeypatch.setattr(job, "apply_one", lambda *_: pytest.fail("dry-run wrote"))
    assert job.run({"stories": [item]}, object(), CATALOG)[0]["receipt"]["result"] == "dry_run"


def test_rejects_unreviewed_aspect_and_changed_source(monkeypatch):
    item, feed, story_artifact = fixture(monkeypatch, aspect="4:5")
    with pytest.raises(ValueError, match="reviewed 9:16"):
        job.plan_one(object(), item, CATALOG)
    story_artifact["evidence"]["aspect"] = "9:16"
    feed["caption"] = "Changed after review"
    # A caption that no longer matches the catalog is only admissible via the
    # LIVE-feed path, and that caption must still carry approved Summit facts.
    with pytest.raises(ValueError, match="approved Summit facts"):
        job.plan_one(object(), item, CATALOG)


def test_schedule_uses_lasso_local_date(monkeypatch):
    item, _, _ = fixture(monkeypatch)
    item["scheduled_at"] = "2026-10-04T01:30:00+00:00"
    assert job.plan_one(object(), item, CATALOG)["date"] == DAY
    item["scheduled_at"] = "2026-10-03T01:30:00+00:00"
    with pytest.raises(ValueError, match="LASSO local feed day"):
        job.plan_one(object(), item, CATALOG)


def test_held_or_existing_slot_blocks_before_rpc(monkeypatch):
    item, _, _ = fixture(monkeypatch, occupied=[{"id": "other", "status": "coach_review",
                                                  "slot_index": 2, "image_url": ""}])
    with pytest.raises(ValueError, match="already occupied"):
        job.plan_one(object(), item, CATALOG)


def test_rpc_payload_is_narrow_and_readback_required(monkeypatch):
    item, _, _ = fixture(monkeypatch)
    action = job.plan_one(object(), item, CATALOG)
    class Client:
        def post(self, _url, *, headers, json, timeout):
            assert set(json) == {"p_feed_id", "p_feed_caption", "p_feed_image_url",
                                 "p_story_id", "p_story_image_url", "p_story_source_url",
                                 "p_story_sha256", "p_source_hash", "p_caption_hash",
                                 "p_policy_version", "p_scheduled_at"}
            assert json["p_story_id"] == action["story_id"]
            assert json["p_caption_hash"] is None
            return type("Response", (), {"status_code": 200,
                                          "json": lambda self: {"result": "inserted", "id": action["story_id"]}})()
    class Store:
        def _client(self): return Client()
        def _rest(self, table): return table
        def _headers(self, extra=None): return extra or {}
    assert job.apply_one(Store(), action)["result"] == "inserted"


def test_sql_grants_service_role_only_and_checks_active_slot():
    sql = (CATALOG.parents[1] / "migrations" / "lasso_third_story_backfill_20261005.sql").read_text()
    assert "lock table public.content_calendar in share row exclusive mode" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "occupied_story_slot" in sql
    assert "a.evidence->>'aspect' = '9:16'" in sql
    assert "p_caption_hash text default null" in sql
    assert "date '2026-09-23' and date '2026-11-08'" in sql
    assert "grant execute on function public.stage_lasso_third_story" in sql
    assert "to service_role" in sql


LIVE_CAPTION = ("Coach-led reviews build the plan.\n\n"
                "LASSO Growth Summit. November 7 and 8 in Nashville.\n"
                "Claim your seat at lassoframework.com/summit")


def live_fixture(monkeypatch, *, day=DAY, caption=LIVE_CAPTION, feed_url=FEED_URL,
                 story_hash=None, feed_hash=None, story_feed_url=FEED_URL):
    """LIVE-feed path: provenance is the exact caption hash, not the catalog."""
    caption_hash = job._caption_hash(caption)
    feed = {"id": FEED_ID, "gym_id": "lasso", "account": "instagram",
            "format": "feed", "post_date": day, "slot_index": 2,
            "pillar": "summit", "variant_status": "active", "status": "pending",
            "caption": caption, "image_url": feed_url,
            "logical_post_id": LOGICAL}
    feed_artifact = {"image_url": feed_url, "image_sha256": "b" * 64,
                     "source_identity": {"source_hash": feed_hash or caption_hash},
                     "evidence": {"grade_status": "PASS", "image_sha256": "b" * 64}}
    story_artifact = {"image_url": STORY_URL, "image_sha256": DIGEST,
                      "source_identity": {"source_hash": story_hash or caption_hash,
                                          "source_feed_id": FEED_ID,
                                          "source_feed_image_url": story_feed_url},
                      "evidence": {"grade_status": "PASS", "image_sha256": DIGEST,
                                   "policy_version": "reviewed-v1", "aspect": "9:16"}}
    monkeypatch.setattr(job, "_feed", lambda _store, _id: feed)
    monkeypatch.setattr(job, "_artifact", lambda _store, url, *_tenants:
                        feed_artifact if url == feed_url else story_artifact)
    monkeypatch.setattr(job, "_story_rows", lambda _store, _day: [])
    item = {"date": day, "feed_id": FEED_ID, "story_image_url": STORY_URL,
            "story_sha256": DIGEST, "policy_version": "reviewed-v1",
            "scheduled_at": f"{day}T12:30:00+00:00"}
    return item, feed, story_artifact


def test_live_feed_path_accepted_with_all_bindings(monkeypatch):
    item, _, _ = live_fixture(monkeypatch)
    action = job.plan_one(object(), item, CATALOG)
    assert action["status"] == "ready"
    assert action["source_hash"] == job._caption_hash(LIVE_CAPTION)
    assert action["caption_hash"] == job._caption_hash(LIVE_CAPTION)


def test_live_feed_caption_can_omit_date_and_venue_when_event_and_url_are_exact(monkeypatch):
    caption = ("Growth looks different when you talk it through with another owner. "
               "Join the conversation at the LASSO Growth Summit. "
               "Claim your seat at lassoframework.com/summit")
    item, _, _ = live_fixture(monkeypatch, day="2026-10-06", caption=caption)
    item["scheduled_at"] = "2026-10-06T12:15:00-04:00"
    action = job.plan_one(object(), item, CATALOG)
    assert action["caption_hash"] == job._caption_hash(caption)


def test_live_feed_rejects_caption_hash_mismatch(monkeypatch):
    item, _, _ = live_fixture(monkeypatch, feed_hash="c" * 64)
    with pytest.raises(ValueError, match="exact reviewed"):
        job.plan_one(object(), item, CATALOG)
    item, _, _ = live_fixture(monkeypatch, story_hash="c" * 64)
    with pytest.raises(ValueError, match="reviewed 9:16"):
        job.plan_one(object(), item, CATALOG)


def test_live_feed_rejects_outside_summit_window(monkeypatch):
    from datetime import date as date_cls
    monkeypatch.setattr(job, "START", date_cls(2026, 9, 1))
    item, _, _ = live_fixture(monkeypatch, day="2026-09-20")
    item["scheduled_at"] = "2026-09-20T12:30:00+00:00"
    with pytest.raises(ValueError, match="Summit window"):
        job.plan_one(object(), item, CATALOG)
    # Boundary dates of the live Summit window are admissible.
    item, _, _ = live_fixture(monkeypatch, day="2026-09-23")
    item["scheduled_at"] = "2026-09-23T12:30:00+00:00"
    assert job.plan_one(object(), item, CATALOG)["status"] == "ready"


def test_live_feed_rejects_caption_without_approved_facts(monkeypatch):
    item, _, _ = live_fixture(monkeypatch, caption="Just vibes, no facts here.")
    with pytest.raises(ValueError, match="approved Summit facts"):
        job.plan_one(object(), item, CATALOG)
    # A venue mention without the official event and destination is not enough.
    item, _, _ = live_fixture(monkeypatch,
                              caption="Live at the Virgin Hotel Nashville soon.")
    with pytest.raises(ValueError, match="approved Summit facts"):
        job.plan_one(object(), item, CATALOG)


def test_live_feed_rejects_changed_feed_url(monkeypatch):
    item, _, _ = live_fixture(monkeypatch, story_feed_url="https://media.example/other.png")
    with pytest.raises(ValueError, match="reviewed 9:16"):
        job.plan_one(object(), item, CATALOG)


def test_live_feed_apply_sends_caption_hash_and_uses_local_date(monkeypatch, tmp_path):
    item, _, _ = live_fixture(monkeypatch)
    action = job.plan_one(object(), item, CATALOG)
    seen = {}
    class Client:
        def post(self, _url, *, headers, json, timeout):
            seen.update(json)
            return type("Response", (), {"status_code": 200,
                                          "json": lambda self: {"result": "inserted"}})()
    class Store:
        def _client(self): return Client()
        def _rest(self, table): return table
        def _headers(self, extra=None): return extra or {}
    monkeypatch.setattr(job, "_story_rows", lambda _store, _day: [
        {"id": action["story_id"], "status": "pending", "slot_index": 2,
         "image_url": STORY_URL, "logical_post_id": LOGICAL}])
    assert job.apply_one(Store(), action)["result"] == "inserted"
    assert seen["p_caption_hash"] == job._caption_hash(LIVE_CAPTION)


def test_apply_refused_while_visual_writer_armed(monkeypatch, tmp_path):
    import agent.visual_writer_prepare as vwp
    monkeypatch.setattr(vwp, "enabled", lambda: True)
    manifest = tmp_path / "manifest.json"
    receipt = tmp_path / "receipt.json"
    manifest.write_text(json.dumps({"stories": []}))
    with pytest.raises(SystemExit, match="visual writer is armed"):
        job.main(["--manifest", str(manifest), "--receipt", str(receipt),
                  "--apply"])
    assert not receipt.exists()
