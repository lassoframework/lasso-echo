from agent.jobs import lasso_daily_paired_stories as daily
from agent import calendar_autopublish as autopublish


def test_feed_preflight_fails_closed_and_accepts_exact_database_proof(monkeypatch):
    monkeypatch.setattr(daily.lasso_current_artifact,"current_pair", lambda *a, **k:True)
    feed = {"id": "feed"}
    class Store:
        def __init__(self, answer): self.answer = answer
        def lasso_paired_story_ready_for_feed(self, feed_id):
            assert feed_id == "feed"
            if isinstance(self.answer, Exception): raise self.answer
            return self.answer
    for answer in (False, None, "true", RuntimeError("unavailable")):
        assert not autopublish._paired_lasso_story_prepared(feed, Store(answer))
    assert autopublish._paired_lasso_story_prepared(feed, Store(True))


def _armed(monkeypatch):
    monkeypatch.setattr(daily.lasso_current_artifact,"anchor_current",lambda *a,**k:True)
    monkeypatch.setattr(daily.lasso_current_artifact,"current_pair",lambda *a,**k:False)
    monkeypatch.setattr(daily.lasso_current_artifact,"feed_reference",lambda _,feed:{'feed':feed,'artifact':{}})
    monkeypatch.setattr(daily.lasso_current_artifact,"evidence_current",lambda *a,**k:True)
    monkeypatch.setattr(daily.config, "calendar_autopublish_enabled", lambda: True)
    monkeypatch.setattr(daily.config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(daily.config, "lasso_infographic_quality_enabled", lambda *_: True)
    monkeypatch.setattr(daily.variant_regen, "enabled", lambda: True)
    monkeypatch.setattr(daily.visual_writer_prepare, "enabled", lambda: False)
    monkeypatch.setattr(daily, "_system_ready", lambda *_: True)


def test_occupied_slot_never_generates_or_stages(monkeypatch):
    _armed(monkeypatch)
    feed = {"id": "feed", "gym_id": "lasso", "account": "instagram",
            "format": "feed", "post_date": "2026-10-07", "slot_index": 0,
            "variant_status": "active", "status": "pending",
            "media_not_ready_reason": None, "caption": "Current caption",
            "image_url": "https://example.com/feed.png"}
    story = {"id": "story", "gym_id": "lasso", "account": "instagram",
             "format": "story", "post_date": "2026-10-07", "slot_index": 0,
             "variant_status": "active", "status": "published",
             "published_at": "2026-10-07T12:00:00Z", "late_post_id": "historical"}
    monkeypatch.setattr(daily.stage, "_active_day", lambda _, day:
                        [feed, story] if day == "2026-10-07" else [])
    monkeypatch.setattr(daily.variant_regen, "generate_variant_image",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("spent")))
    class Artifacts: available = True
    result = daily.run(now="2026-10-07T06:00:00-04:00", store=object(),
                       artifact_store=Artifacts())
    assert result["occupied"] == 1
    assert result["generated"] == result["staged"] == 0


def test_migrations_must_be_ready_before_any_generation(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(daily, "_system_ready", lambda *_: False)
    monkeypatch.setattr(daily.variant_regen, "generate_variant_image",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("spent")))
    class Artifacts: available = True
    result = daily.run(now="2026-10-07T06:00:00-04:00", store=object(),
                       artifact_store=Artifacts())
    assert not result["ok"]
    assert result["reason"] == "managed Story migrations not active"


def test_source_bound_reused_artifact_stages_once(monkeypatch):
    _armed(monkeypatch)
    feed = {"id": "feed", "gym_id": "lasso", "account": "instagram",
            "format": "feed", "post_date": "2026-10-07", "slot_index": 0,
            "variant_status": "active", "status": "pending",
            "media_not_ready_reason": None, "caption": "Current caption",
            "image_url": "https://example.com/feed.png"}
    monkeypatch.setattr(daily.stage, "_active_day", lambda _, day:
                        [feed] if day == "2026-10-07" else [])
    monkeypatch.setattr(daily, "_candidate_artifact", lambda *a: {
        "image_url": "https://example.com/story.png", "image_sha256": "a" * 64,
        "source_identity": {"source_id": "content_calendar:feed:caption",
                            "source_hash": daily.hashlib.sha256(feed["caption"].encode()).hexdigest()},
        "evidence": {"grade_status": "PASS", "aspect": "9:16",
                     "pixels": "1080x1920", "image_sha256": "a" * 64,
                     "verified_dimensions": {"width": 1080, "height": 1920,
                                             "image_sha256": "a" * 64},
                     "policy_version": daily.infographic_evidence.POLICY_VERSION}})
    monkeypatch.setattr(daily.stage, "plan_one", lambda *a: {"state": "ready"})
    monkeypatch.setattr(daily.stage, "apply_one", lambda *a: {"result": "inserted"})
    monkeypatch.setattr(daily, "_verify_staged_pair", lambda *a: None)
    monkeypatch.setattr(daily.variant_regen, "generate_variant_image",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("spent")))
    class Artifacts: available = True
    result = daily.run(now="2026-10-07T06:00:00-04:00", store=object(),
                       artifact_store=Artifacts())
    assert result["reused"] == result["staged"] == 1
    assert result["generated"] == 0


def test_autonomous_insert_reads_exact_story_and_feed_link(monkeypatch):
    action = {"story_id": "story", "feed_id": "feed", "account": "instagram",
              "date": "2026-10-07", "slot_index": 0,
              "story_image_url": "https://example.com/story.png",
              "story_scheduled_at": "2026-10-07T07:45:00-04:00",
              "feed_pillar": "doctrine", "feed_logical_post_id": "logical"}
    story = {"id": "story", "gym_id": "lasso", "account": "instagram",
             "post_date": "2026-10-07", "slot_index": 0, "format": "story",
             "variant_status": "active", "status": "pending", "caption": "",
             "pillar": "doctrine", "image_url": action["story_image_url"],
             "source_media_url": action["story_image_url"],
             "logical_post_id": "logical", "media_not_ready_reason": None,
             "scheduled_at": "2026-10-07T11:45:00Z"}
    link = {"story_id": "story", "feed_id": "feed"}
    monkeypatch.setattr(daily.stage, "_one", lambda _, table, __:
                        story if table == "content_calendar" else link)
    daily._verify_staged_pair(object(), action)
    link["feed_id"] = "another-feed"
    import pytest
    with pytest.raises(RuntimeError, match="source-link"):
        daily._verify_staged_pair(object(), action)


def test_release_only_after_feed_receipt(monkeypatch):
    monkeypatch.setattr(daily.lasso_current_artifact,"current_pair",lambda *a,**k:True)
    monkeypatch.setattr(daily, "_system_ready", lambda *_: True)
    feed = {"id": "feed", "gym_id": "lasso", "account": "instagram",
            "format": "feed", "post_date": "2026-10-07", "slot_index": 0, "variant_status": "active",
            "status": "pending", "published_at": None, "late_post_id": None}
    story = {"id": "story", "gym_id": "lasso", "account": "instagram",
             "format": "story", "post_date": "2026-10-07", "slot_index": 0, "variant_status": "active",
             "status": "pending", "media_not_ready_reason": "paired_feed_not_ready"}
    monkeypatch.setattr(daily.stage, "_active_day", lambda *_: [feed, story])
    assert daily.release_ready_holds(object(), "2026-10-07") == {"released": 0, "blocked": 0}
    feed.update(status="published", published_at="2026-10-07T12:00:00Z", late_post_id="late")
    class Store:
        def _client(self): return self
        def _rest(self, path): return path
        def _headers(self, extra=None): return extra or {}
        def post(self, path, *, headers, json, timeout):
            assert path == "rpc/release_lasso_paired_story_hold"
            return type("R", (), {"status_code": 200,
                "json": lambda self: {"result": "released", "id": "story"}})()
    assert daily.release_ready_holds(Store(), "2026-10-07")["released"] == 1


def test_release_catches_prior_day_only_with_publisher_window(monkeypatch):
    monkeypatch.setattr(daily.lasso_current_artifact,"current_pair",lambda *a,**k:True)
    monkeypatch.setattr(daily, "_system_ready", lambda *_: True)
    rows = [
        {"id": "old-feed", "gym_id": "lasso", "account": "instagram",
         "format": "feed", "post_date": "2026-10-06", "slot_index": 0,
         "variant_status": "active", "status": "published",
         "published_at": "2026-10-06T12:00:00Z", "late_post_id": "late-old"},
        {"id": "old-story", "gym_id": "lasso", "account": "instagram",
         "format": "story", "post_date": "2026-10-06", "slot_index": 0,
         "variant_status": "active", "status": "pending",
         "media_not_ready_reason": "paired_feed_not_ready"},
        {"id": "today-feed", "gym_id": "lasso", "account": "instagram",
         "format": "feed", "post_date": "2026-10-07", "slot_index": 0,
         "variant_status": "active", "status": "pending"},
    ]
    class Store:
        def rows_in_range_complete(self, gym, start, end):
            assert gym == "lasso" and end == "2026-10-07"
            return [r for r in rows if start <= r["post_date"] <= end]
        def _client(self): return self
        def _rest(self, path): return path
        def _headers(self, extra=None): return extra or {}
        def post(self, path, *, headers, json, timeout):
            assert json["p_story_id"] == "old-story"
            return type("R", (), {"status_code": 200,
                "json": lambda self: {"result": "released", "id": "old-story"}})()
    assert daily.release_ready_holds(Store(), "2026-10-07", catchup_days=0)["released"] == 0
    assert daily.release_ready_holds(Store(), "2026-10-07", catchup_days=1)["released"] == 1


def test_occupied_pending_story_repaired_from_exact_feed_artifact(monkeypatch):
    _armed(monkeypatch)
    feed = {"id": "feed", "gym_id": "lasso", "account": "instagram",
            "format": "feed", "post_date": "2026-10-07", "slot_index": 0,
            "variant_status": "active", "status": "pending", "pillar": "doctrine",
            "logical_post_id": "logical", "media_not_ready_reason": None,
            "caption": "Current caption", "image_url": "https://example.com/feed.png"}
    story = {"id": "story", "gym_id": "lasso", "account": "instagram",
             "format": "story", "post_date": "2026-10-07", "slot_index": 0,
             "variant_status": "active", "status": "pending", "pillar": "old",
             "logical_post_id": "logical", "media_not_ready_reason": "paired_feed_not_ready",
             "published_at": None, "late_post_id": None, "publish_claim_token": None}
    monkeypatch.setattr(daily.stage, "_active_day", lambda _, day:
                        [feed, story] if day == "2026-10-07" else [])
    monkeypatch.setattr(daily, "_candidate_artifact", lambda *a: {
        "image_url": "https://example.com/story.png", "image_sha256": "a" * 64,
        "source_identity": {"source_id": "content_calendar:feed:caption",
                            "source_hash": daily.hashlib.sha256(feed["caption"].encode()).hexdigest()},
        "evidence": {"grade_status": "PASS", "aspect": "9:16",
                     "pixels": "1080x1920", "image_sha256": "a" * 64,
                     "verified_dimensions": {"width": 1080, "height": 1920,
                                             "image_sha256": "a" * 64},
                     "policy_version": daily.infographic_evidence.POLICY_VERSION}})
    action = {"story_id": "story", "feed_id": "feed", "account": "instagram",
              "date": "2026-10-07", "slot_index": 0}
    monkeypatch.setattr(daily.repair, "plan_one", lambda _, item:
                        action if item["story_id"] == "story" else None)
    monkeypatch.setattr(daily.repair, "apply_one", lambda _, a:
                        {"result": "repaired", "hold_reason": "paired_feed_not_ready"}
                        if a is action else None)
    verified = []
    monkeypatch.setattr(daily, "_verify_staged_pair", lambda _, a, **kw:
                        verified.append((a, kw)))
    class Store:
        def lasso_paired_story_ready_for_feed(self, _): return False
    class Artifacts: available = True
    result = daily.run(now="2026-10-07T06:00:00-04:00", store=Store(),
                       artifact_store=Artifacts())
    assert result["repaired"] == result["reused"] == 1
    assert result["generated"] == result["staged"] == 0
    assert verified == [(action, {"expected_hold": "paired_feed_not_ready"})]


def test_media_held_story_is_repaired_from_exact_feed_through_rpc(monkeypatch):
    _armed(monkeypatch)
    for hold in ("caption_changed_needs_new_visual",
                 "cross_date_media_repeat_needs_new_visual"):
        feed = {"id": "feed", "gym_id": "lasso", "account": "instagram",
                "format": "feed", "post_date": "2026-10-07", "slot_index": 0,
                "variant_status": "active", "status": "pending", "pillar": "doctrine",
                "logical_post_id": "logical", "media_not_ready_reason": None,
                "caption": "Current caption", "image_url": "https://example.com/feed.png"}
        story = {"id": "story", "gym_id": "lasso", "account": "instagram",
                 "format": "story", "post_date": "2026-10-07", "slot_index": 0,
                 "variant_status": "active", "status": "pending", "pillar": "old",
                 "logical_post_id": "logical", "media_not_ready_reason": hold,
                 "published_at": None, "late_post_id": None, "publish_claim_token": None}
        monkeypatch.setattr(daily.stage, "_active_day", lambda _, day:
                            [feed, story] if day == "2026-10-07" else [])
        # The reused artifact binds the exact FEED id and caption, not the Story.
        monkeypatch.setattr(daily, "_candidate_artifact", lambda *a: {
            "image_url": "https://example.com/story.png", "image_sha256": "a" * 64,
            "source_identity": {"source_id": "content_calendar:feed:caption",
                                "source_hash": daily.hashlib.sha256(feed["caption"].encode()).hexdigest()},
            "evidence": {"grade_status": "PASS", "aspect": "9:16",
                         "pixels": "1080x1920", "image_sha256": "a" * 64,
                         "verified_dimensions": {"width": 1080, "height": 1920,
                                                 "image_sha256": "a" * 64},
                         "policy_version": daily.infographic_evidence.POLICY_VERSION}})
        action = {"story_id": "story", "feed_id": "feed", "account": "instagram",
                  "date": "2026-10-07", "slot_index": 0}
        monkeypatch.setattr(daily.repair, "plan_one", lambda _, item:
                            action if item["story_id"] == "story" else None)
        monkeypatch.setattr(daily.repair, "apply_one", lambda _, a:
                            {"result": "repaired", "hold_reason": "paired_feed_not_ready"}
                            if a is action else None)
        verified = []
        monkeypatch.setattr(daily, "_verify_staged_pair", lambda _, a, **kw:
                            verified.append((a, kw)))
        class Store:
            def lasso_paired_story_ready_for_feed(self, _): return False
        class Artifacts: available = True
        result = daily.run(now="2026-10-07T06:00:00-04:00", store=Store(),
                           artifact_store=Artifacts())
        assert result["repaired"] == result["reused"] == 1
        assert result["generated"] == result["staged"] == 0
        assert verified == [(action, {"expected_hold": "paired_feed_not_ready"})]


def test_backlog_preparation_hold_only_allows_exact_incident_window():
    feed = {"status": "pending", "media_not_ready_reason":
            daily.stage.BACKLOG_FEED_HOLD}
    assert daily.stage.allowed_feed_hold(feed, "2026-10-02")
    assert daily.stage.allowed_feed_hold(feed, "2026-10-05")
    assert not daily.stage.allowed_feed_hold(feed, "2026-10-06")
    assert not daily.stage.allowed_feed_hold(dict(feed, status="approved"), "2026-10-05")
    assert not daily.stage.allowed_feed_hold(dict(feed,
        media_not_ready_reason="unrelated_hold"), "2026-10-05")


def test_three_feed_story_cadence_continues_after_summit():
    feed = {"id": "feed", "gym_id": "lasso", "account": "facebook",
            "format": "feed", "post_date": "2026-11-09", "slot_index": 2,
            "variant_status": "active", "status": "pending",
            "media_not_ready_reason": None, "caption": "Current caption",
            "image_url": "https://example.com/feed.png"}
    assert daily._eligible(feed, "facebook", "2026-11-09")
    assert daily.stage.FIRST <= daily.stage.date.fromisoformat("2026-11-09") <= daily.stage.LAST
