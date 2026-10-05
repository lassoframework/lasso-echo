from agent.jobs import lasso_daily_paired_stories as daily


def _armed(monkeypatch):
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
             "variant_status": "active", "status": "pending"}
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
                     "policy_version": daily.infographic_evidence.POLICY_VERSION}})
    monkeypatch.setattr(daily.stage, "plan_one", lambda *a: {"state": "ready"})
    monkeypatch.setattr(daily.stage, "apply_one", lambda *a: {"result": "inserted"})
    monkeypatch.setattr(daily.variant_regen, "generate_variant_image",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("spent")))
    class Artifacts: available = True
    result = daily.run(now="2026-10-07T06:00:00-04:00", store=object(),
                       artifact_store=Artifacts())
    assert result["reused"] == result["staged"] == 1
    assert result["generated"] == 0


def test_release_only_after_feed_receipt(monkeypatch):
    monkeypatch.setattr(daily, "_system_ready", lambda *_: True)
    feed = {"id": "feed", "gym_id": "lasso", "account": "instagram",
            "format": "feed", "slot_index": 0, "variant_status": "active",
            "status": "pending", "published_at": None, "late_post_id": None}
    story = {"id": "story", "gym_id": "lasso", "account": "instagram",
             "format": "story", "slot_index": 0, "variant_status": "active",
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
