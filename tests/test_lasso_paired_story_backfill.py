from pathlib import Path
import hashlib
import json

import pytest

from agent.jobs import lasso_paired_story_backfill as job


FEED_ID = "aa070663-2e0a-4878-982b-1a51c6fa2311"
LOGICAL_ID = "af9baabe-8e36-4e09-bd54-212203d2c612"
DAY = "2026-10-07"
STORY_URL = "https://cdn.example/lasso-story-ig-0.png"
DIGEST = "a" * 64


def fixture(monkeypatch, *, account="instagram", logical=LOGICAL_ID,
            status="pending", existing=None):
    from agent import lasso_visual_standard
    feed = {"id": FEED_ID, "gym_id": "lasso", "account": account,
            "format": "feed", "post_date": DAY, "slot_index": 0,
            "variant_status": "active", "status": status, "pillar": "guide",
            "caption": f"Independent {account} approved copy",
            "image_url": "https://cdn.example/approved-feed.png",
            "scheduled_at": "2026-10-07T07:30:00-04:00",
            "logical_post_id": logical, "media_not_ready_reason": None,
            "published_at": "2026-10-07T11:31:00+00:00" if status == "published" else None,
            "late_post_id": "post-123" if status == "published" else None}
    identity = job.source_identity(feed)
    artifact = {"tenant": "lasso", "image_url": STORY_URL,
                "image_sha256": DIGEST,
                "evidence": {"grade_status": "PASS", "image_sha256": DIGEST,
                             "policy_version": "review-v1", "aspect": "9:16",
                             "style_conformant": True, "style_violations": [],
                             "visual_standard_version": lasso_visual_standard.VERSION,
                             "pixels": "1080x1920", "verified_dimensions": {
                                 "width": 1080, "height": 1920,
                                 "image_sha256": DIGEST}},
                "source_identity": identity}
    monkeypatch.setattr(job, "_feed", lambda store, feed_id: feed)
    monkeypatch.setattr(job, "_artifact", lambda store, url, tenant: artifact)
    class Store: pass
    monkeypatch.setattr(job, "_active_day", lambda store, day: list(existing or []))
    item = {"account": account, "date": DAY, "slot_index": 0,
            "feed_id": FEED_ID, "story_image_url": STORY_URL,
            "story_sha256": DIGEST, "policy_version": "review-v1"}
    return Store(), item, feed, artifact


@pytest.mark.parametrize("account", ["instagram", "facebook"])
def test_exact_feed_pairing_and_local_schedule(monkeypatch, account):
    store, item, feed, _ = fixture(monkeypatch, account=account)
    action = job.plan_one(store, item)
    assert action["account"] == account
    assert action["feed_caption"] == feed["caption"]
    assert action["story_scheduled_at"] == "2026-10-07T07:45:00-04:00"
    assert action["feed_logical_post_id"] == LOGICAL_ID
    assert action["story_id"] == job.deterministic_id(account, FEED_ID, 0)


def test_null_feed_schedule_uses_publishers_canonical_pair(monkeypatch):
    from agent import calendar_autopublish
    store, item, feed, _ = fixture(monkeypatch)
    feed["scheduled_at"] = None
    monkeypatch.setattr(calendar_autopublish, "slot_time_for_row",
        lambda row: "07:45" if row["format"] == "story" else "07:30")
    action = job.plan_one(store, item)
    assert action["feed_scheduled_at"] is None
    assert action["story_scheduled_at"] == "2026-10-07T07:45:00-04:00"


def test_unrelated_historical_story_requires_exact_db_exception(monkeypatch):
    historical = {"id": "af677ffb-7834-455e-852c-b865a5155ac4",
                  "account": "instagram", "format": "story", "post_date": DAY,
                  "slot_index": 0, "variant_status": "active", "status": "published",
                  "published_at": "2026-10-07T12:00:00Z", "late_post_id": "old"}
    store, item, _, _ = fixture(monkeypatch, existing=[historical])
    monkeypatch.setattr(job, "unrelated_published_story", lambda *a: False)
    with pytest.raises(ValueError, match="active Story already occupies"):
        job.plan_one(store, item)
    monkeypatch.setattr(job, "unrelated_published_story", lambda _, story_id, feed_id:
                        story_id == historical["id"] and feed_id == FEED_ID)
    assert job.plan_one(store, item)["state"] == "ready"


def test_facebook_cannot_use_ig_artifact_or_caption(monkeypatch):
    store, item, feed, artifact = fixture(monkeypatch, account="facebook")
    artifact["source_identity"]["source_account"] = "instagram"
    with pytest.raises(ValueError, match="bound to this exact feed"):
        job.plan_one(store, item)


@pytest.mark.parametrize("account,tenant", [("instagram", "lasso_ig"),
                                             ("facebook", "lasso_fb")])
def test_variant_regen_account_artifact_contract(monkeypatch, account, tenant):
    import hashlib
    store, item, feed, artifact = fixture(monkeypatch, account=account)
    item["artifact_tenant"] = tenant
    artifact["tenant"] = tenant
    artifact["source_identity"] = {
        "source_id": f"content_calendar:{FEED_ID}:caption",
        "source_hash": hashlib.sha256(feed["caption"].encode()).hexdigest()}
    action = job.plan_one(store, item)
    assert action["artifact_tenant"] == tenant
    assert action["source_hash"] == artifact["source_identity"]["source_hash"]
    feed["caption"] = "caption swapped after variant review"
    with pytest.raises(ValueError, match="bound to this exact feed"):
        job.plan_one(store, item)
    artifact["source_identity"] = job.source_identity(feed)
    feed["caption"] = "Facebook caption changed after Story review"
    with pytest.raises(ValueError, match="bound to this exact feed"):
        job.plan_one(store, item)


def test_legacy_null_logical_id_requires_full_source_binding(monkeypatch):
    store, item, feed, artifact = fixture(monkeypatch, logical=None)
    assert job.plan_one(store, item)["feed_logical_post_id"] is None
    artifact["source_identity"].pop("source_logical_post_id")
    with pytest.raises(ValueError, match="bound to this exact feed"):
        job.plan_one(store, item)


def test_reviewed_real_story_required(monkeypatch):
    store, item, _, artifact = fixture(monkeypatch)
    artifact["evidence"]["aspect"] = "4:5"
    with pytest.raises(ValueError, match="9:16 reviewed artifact"):
        job.plan_one(store, item)
    artifact["evidence"]["aspect"] = "9:16"
    item["story_image_url"] = "https://cdn.example/approved-feed.png"
    with pytest.raises(ValueError, match="source feed"):
        job.plan_one(store, item)


def test_manifest_refuses_declared_aspect_without_measured_dimensions(monkeypatch):
    store, item, _, artifact = fixture(monkeypatch)
    artifact["evidence"].pop("verified_dimensions")
    with pytest.raises(ValueError, match="9:16 reviewed artifact"):
        job.plan_one(store, item)
    artifact["evidence"]["verified_dimensions"] = {
        "width": 1080, "height": 1920, "image_sha256": "b" * 64}
    with pytest.raises(ValueError, match="9:16 reviewed artifact"):
        job.plan_one(store, item)


def test_existing_held_story_occupies_slot(monkeypatch):
    store, item, _, _ = fixture(monkeypatch, existing=[{
        "id": "existing", "account": "instagram", "format": "story",
        "status": "coach_review", "slot_index": 0}])
    with pytest.raises(ValueError, match="already occupies"):
        job.plan_one(store, item)


def test_published_historical_plus_pending_is_explicit_conflict(monkeypatch):
    rows = [
        {"id": "published-old", "account": "instagram", "format": "story",
         "status": "published", "slot_index": 0},
        {"id": "pending-old", "account": "instagram", "format": "story",
         "status": "pending", "slot_index": 0},
    ]
    store, item, _, _ = fixture(monkeypatch, existing=rows)
    with pytest.raises(ValueError, match="published-old.*pending-old"):
        job.plan_one(store, item)


def test_exact_published_story_with_receipt_is_satisfied(monkeypatch):
    row = {"id": job.deterministic_id("instagram", FEED_ID, 0),
           "account": "instagram", "format": "story", "status": "published",
           "slot_index": 0, "image_url": STORY_URL,
           "logical_post_id": LOGICAL_ID,
           "published_at": "2026-10-07T12:00:00+00:00", "late_post_id": "late-1"}
    store, item, _, _ = fixture(monkeypatch, existing=[row])
    assert job.plan_one(store, item)["state"] == "satisfied_published"
    monkeypatch.setattr(job, "apply_one", lambda *_: pytest.fail("republished Story"))
    assert job.run({"stories": [item]}, store, apply=True)[0]["receipt"]["result"] == "already_published"


def test_complete_day_read_includes_null_status_and_fails_closed(monkeypatch):
    page = [{"id": "a", "gym_id": "lasso", "post_date": DAY,
             "account": "instagram", "format": "story", "status": None,
             "variant_status": None, "slot_index": 0}]
    monkeypatch.setattr(job, "_request_rows", lambda *args: page)
    assert job._active_day(object(), DAY) == page
    page[0]["gym_id"] = "other"
    with pytest.raises(RuntimeError, match="out of scope"):
        job._active_day(object(), DAY)


def test_published_feed_needs_real_receipt(monkeypatch):
    store, item, feed, artifact = fixture(monkeypatch, status="published")
    assert job.plan_one(store, item)["feed_status"] == "published"
    feed["late_post_id"] = None
    with pytest.raises(ValueError, match="incomplete publish receipt"):
        job.plan_one(store, item)


def test_dry_run_never_posts_and_rpc_payload_has_exact_cas(monkeypatch):
    store, item, _, _ = fixture(monkeypatch)
    original_apply = job.apply_one
    monkeypatch.setattr(job, "apply_one", lambda *_: pytest.fail("dry-run wrote"))
    assert job.run({"stories": [item]}, store)[0]["receipt"]["result"] == "dry_run"
    monkeypatch.setattr(job, "apply_one", original_apply)
    action = job.plan_one(store, item)
    class Client:
        def post(self, path, *, headers, json, timeout):
            assert path == job.RPC
            assert json["p_feed_caption"] == action["feed_caption"]
            assert json["p_feed_status"] == "pending"
            assert json["p_story_id"] == action["story_id"]
            assert json["p_feed_logical_post_id"] == LOGICAL_ID
            return type("Response", (), {"status_code": 200,
                "json": lambda self: {"result": "inserted", "id": action["story_id"]}})()
    class PostStore:
        def _client(self): return Client()
        def _rest(self, path): return path
        def _headers(self, extra=None): return extra or {}
    assert job.apply_one(PostStore(), action)["result"] == "inserted"


def test_manifest_duplicate_slot_rejected(monkeypatch):
    store, item, _, _ = fixture(monkeypatch)
    with pytest.raises(ValueError, match="repeats"):
        job.run({"stories": [item, dict(item)]}, store)


def test_sql_insert_only_barriers_and_service_role():
    sql = (Path(__file__).resolve().parents[1] / "migrations" /
           "lasso_paired_story_backfill_20261005.sql").read_text()
    assert "lock table public.content_calendar in share row exclusive mode" not in sql
    assert "lasso_story_legacy_slot_guard" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "lasso_active_story_account_day_slot_unique" in sql
    assert "create table if not exists public.lasso_managed_paired_stories" in sql
    assert "insert into public.lasso_managed_paired_stories" in sql
    assert "occupied_story_slot" in sql
    assert "source_feed_caption' = p_feed_caption" in sql
    assert "source_account' = v_account" in sql
    assert "source_logical_post_id'" in sql
    assert "encode(sha256(convert_to(p_feed_caption, 'UTF8')), 'hex')" in sql
    assert "a.evidence->>'aspect' = '9:16'" in sql
    assert "'pending', p_story_scheduled_at" in sql
    assert "grant execute on function public.stage_lasso_paired_story" in sql
    assert "to service_role" in sql
    assert "delete from" not in sql.lower()


def test_variant_story_review_records_measured_9x16_before_artifact_save(tmp_path):
    from PIL import Image
    from agent import infographic_evidence, lasso_visual_standard, variant_regen

    path = tmp_path / "reviewed-story.png"
    Image.new("RGB", (1080, 1920), "#ffffff").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = {"infographic_copy": {"headline": "Approved", "facts": ["Fact"],
              "cta": "", "footer": ""},
              "policy_version": infographic_evidence.POLICY_VERSION,
              "brain_snapshot": infographic_evidence.brain_snapshot(),
              "brief_model": "gpt-6-astra", "grade_status": "PASS",
              "response_id": "render-id", "review_response_id": "review-id",
              "style_conformant": True, "style_violations": [],
              "visual_standard_version": lasso_visual_standard.VERSION,
              "image_sha256": digest}
    review_path = Path(str(path) + ".review.json")
    review_path.write_text(json.dumps(receipt))
    assert variant_regen._attest_reviewed_story_dimensions(path)
    stamped = json.loads(review_path.read_text())
    assert stamped["aspect"] == "9:16"
    assert stamped["pixels"] == "1080x1920"
    assert stamped["verified_dimensions"] == {"width": 1080, "height": 1920,
                                                "image_sha256": digest}
    assert infographic_evidence.reviewed_asset(path)


def test_variant_story_wrong_pixels_never_stamp_review(tmp_path):
    from PIL import Image
    from agent import infographic_evidence, lasso_visual_standard, variant_regen

    path = tmp_path / "wrong-story.png"
    Image.new("RGB", (1080, 1350), "#ffffff").save(path)
    receipt = {"infographic_copy": {"headline": "Approved", "facts": ["Fact"],
              "cta": "", "footer": ""},
              "policy_version": infographic_evidence.POLICY_VERSION,
              "brain_snapshot": infographic_evidence.brain_snapshot(),
              "brief_model": "gpt-6-astra", "grade_status": "PASS",
              "response_id": "render-id", "review_response_id": "review-id",
              "style_conformant": True, "style_violations": [],
              "visual_standard_version": lasso_visual_standard.VERSION,
              "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    review_path = Path(str(path) + ".review.json")
    review_path.write_text(json.dumps(receipt))
    assert not variant_regen._attest_reviewed_story_dimensions(path)
    assert "aspect" not in json.loads(review_path.read_text())


def test_variant_story_review_missing_style_protocol_never_stamps(tmp_path):
    """A 9:16 PASS receipt without style protocol evidence is not a reviewed asset."""
    from PIL import Image
    from agent import infographic_evidence, variant_regen

    path = tmp_path / "unstyled-story.png"
    Image.new("RGB", (1080, 1920), "#ffffff").save(path)
    receipt = {"infographic_copy": {"headline": "Approved", "facts": ["Fact"],
              "cta": "", "footer": ""},
              "policy_version": infographic_evidence.POLICY_VERSION,
              "brain_snapshot": infographic_evidence.brain_snapshot(),
              "brief_model": "gpt-6-astra", "grade_status": "PASS",
              "response_id": "render-id", "review_response_id": "review-id",
              "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    review_path = Path(str(path) + ".review.json")
    review_path.write_text(json.dumps(receipt))
    assert infographic_evidence.reviewed_asset(path) is None
    assert not variant_regen._attest_reviewed_story_dimensions(path)
    assert "aspect" not in json.loads(review_path.read_text())
