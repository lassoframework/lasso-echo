"""Offline safety checks for bounded LASSO held-feed repair."""

from copy import deepcopy
from datetime import date

import pytest

from agent import variant_regen
from agent.jobs import lasso_held_media_repair as repair


def _row(rid, day="2026-10-05", **overrides):
    row = {key: None for key in repair._CAS_COLUMNS}
    row.update(id=rid, gym_id="lasso", status="pending", variant_status="active",
               account="instagram", format="feed", post_date=day,
               caption=f"Approved copy for {rid}.", image_url=f"https://old.example/{rid}.jpg",
               media_not_ready_reason=repair.HOLD_REASON,
               created_at="2026-10-01T00:00:00+00:00", slot_index=0)
    row.update(overrides)
    return row


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Store:
    def __init__(self, rows):
        self.rows = {row["id"]: deepcopy(row) for row in rows}
        self.cache = {}
        self.by_url = {}
        self.patches = []
        self.reads = 0

    def list_pending_media_between(self, gym, first, last):
        self.reads += 1
        # 2026-10-05 is outside the incident window, so the publisher catchup
        # lookback is the default seven days.
        assert (gym, first, last) == ("lasso", "2026-09-28", "2026-10-06")
        return [deepcopy(row) for row in self.rows.values()]

    def get_row(self, gym, rid):
        assert gym == "lasso"
        return deepcopy(self.rows[rid])

    def _client(self):
        return self

    @staticmethod
    def _rest(name):
        return name

    @staticmethod
    def _headers(extra=None):
        return extra or {}

    def get(self, url, params, headers, timeout):
        assert url == "echo_infographic_artifacts"
        if "image_url" in params:
            return _Response(self.by_url.get((params["tenant"], params["image_url"]), []))
        key = (params["source_identity->>source_id"],
               params["source_identity->>source_hash"])
        return _Response(self.cache.get(key, []))

    def post(self, url, params, headers, json, timeout):
        assert url == "echo_infographic_artifacts"
        key = (f"eq.{json['tenant']}", repair._eq(json["image_url"]))
        if key not in self.by_url:
            record = {k: deepcopy(json[k]) for k in
                      ("image_url", "evidence", "source_identity")}
            self.by_url[key] = [record]
            source = json["source_identity"]
            self.cache[(repair._eq(source["source_id"]),
                        repair._eq(source["source_hash"]))] = [record]
        return _Response([])

    def list_active_logical_post_rows(self, gym, logical_id):
        return [deepcopy(row) for row in self.rows.values()
                if row.get("gym_id") == gym and row.get("logical_post_id") == logical_id]

    def rows_in_range_repeat_hold(self, gym, first, last):
        return [deepcopy(row) for row in self.rows.values()
                if row.get("gym_id") == gym and first <= row.get("post_date", "") <= last]

    def patch(self, url, params, headers, json, timeout):
        assert url == "content_calendar"
        self.patches.append((params, json))
        rid = params["id"].removeprefix('eq.')
        row = self.rows[rid]
        # Simulate the database's full-field CAS, including a racing caption edit.
        if any(params[key] != repair._eq(row[key]) for key in repair._CAS_COLUMNS):
            return _Response([])
        row.update(json)
        return _Response([deepcopy(row)])


class _Artifacts:
    available = True

    def __init__(self):
        self.claims = []
        self.releases = []

    def claim(self, tenant, key, owner):
        self.claims.append((tenant, key, owner))
        return True

    def release(self, tenant, key, owner):
        self.releases.append((tenant, key, owner))


def _armed(monkeypatch):
    monkeypatch.setattr(repair.config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(repair.config, "lasso_infographic_quality_enabled",
                        lambda key: key == "lasso_ig")
    monkeypatch.setattr(variant_regen, "enabled", lambda: True)
    monkeypatch.setattr(repair.visual_writer_prepare, "enabled", lambda: False)
    monkeypatch.setattr(repair, "_local_day", lambda now: date(2026, 10, 5))


def test_repair_is_lasso_ig_feed_only_and_capped_per_day(monkeypatch):
    _armed(monkeypatch)
    rows = ([_row(f"today-{i}") for i in range(4)]
            + [_row(f"tomorrow-{i}", day="2026-10-06") for i in range(4)]
            + [_row("client", gym_id="client-gym")]
            + [_row("story", format="story")]
            + [_row("approved", status="approved")])
    store, artifacts = _Store(rows), _Artifacts()
    generated = []

    def fake_generate(row, account, **kwargs):
        generated.append((row["id"], account))
        return {"ok": True, "image_url": f"https://new.example/{row['id']}.png"}

    monkeypatch.setattr(variant_regen, "generate_variant_image", fake_generate)
    out = repair.run(store=store, artifact_store=artifacts)

    assert out["ok"] is True
    assert out["attempted"] == out["generated"] == out["repaired"] == 6
    assert len(generated) == len(artifacts.claims) == len(artifacts.releases) == 6
    assert all(account == "lasso_ig" for _, account in generated)
    assert store.rows["today-0"]["caption"] == "Approved copy for today-0."
    assert store.rows["today-0"]["source_media_url"] == "https://new.example/today-0.png"
    assert store.rows["today-0"]["media_not_ready_reason"] is None
    assert store.rows["today-3"]["media_not_ready_reason"] == repair.HOLD_REASON
    assert store.rows["tomorrow-3"]["media_not_ready_reason"] == repair.HOLD_REASON
    assert store.rows["client"]["media_not_ready_reason"] == repair.HOLD_REASON


def test_facebook_held_feed_repairs_in_its_own_tenant(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.config, "lasso_infographic_quality_enabled",
                        lambda key: key in ("lasso_ig", "lasso_fb"))
    store, artifacts = _Store([_row("fb", account="facebook"), _row("ig")]), _Artifacts()
    generated = []
    def fake_generate(row, account, **kwargs):
        generated.append((row["id"], account))
        return {"ok": True, "image_url": f"https://new.example/{row['id']}.png"}
    monkeypatch.setattr(variant_regen, "generate_variant_image", fake_generate)
    out = repair.run(store=store, artifact_store=artifacts, account_key="lasso_fb")
    assert out["repaired"] == 1
    assert generated == [("fb", "lasso_fb")]
    assert artifacts.claims[0][0] == artifacts.releases[0][0] == "lasso_fb"
    assert store.rows["ig"]["media_not_ready_reason"] == repair.HOLD_REASON


def _reviewed_ig_artifact(monkeypatch, store, ig_row):
    monkeypatch.setattr(repair.infographic_evidence, "brain_snapshot",
                        lambda: {"source": "hash"})
    source_id = f"content_calendar:{ig_row['id']}:caption"
    source_hash = repair.hashlib.sha256(ig_row["caption"].encode()).hexdigest()
    record = {
        "image_url": ig_row["image_url"],
        "source_identity": {"source_id": source_id, "source_hash": source_hash},
        "evidence": {"policy_version": repair.infographic_evidence.POLICY_VERSION,
                     "brain_snapshot": {"source": "hash"},
                     "brief_model": "gpt-6-astra", "grade_status": "PASS",
                     "image_sha256": "exact-image-hash", "review_response_id": "review"},
    }
    store.cache[(repair._eq(source_id), repair._eq(source_hash))] = [record]


def _story_artifact_for(ig_row, url):
    """A 9:16 Story artifact sharing the exact feed id/caption source."""
    source_id = f"content_calendar:{ig_row['id']}:caption"
    source_hash = repair.hashlib.sha256(ig_row["caption"].encode()).hexdigest()
    return {
        "image_url": url,
        "source_identity": {"source_id": source_id, "source_hash": source_hash},
        "evidence": {"policy_version": repair.infographic_evidence.POLICY_VERSION,
                     "brain_snapshot": {"source": "hash"},
                     "brief_model": "gpt-6-astra", "grade_status": "PASS",
                     "image_sha256": "story-image-hash",
                     "review_response_id": "review", "aspect": "9:16",
                     "pixels": "1080x1920",
                     "verified_dimensions": {"width": 1080, "height": 1920,
                                             "image_sha256": "story-image-hash"}},
    }


def test_newer_story_artifact_never_shadows_feed_artwork_for_reuse(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.infographic_evidence, "brain_snapshot",
                        lambda: {"source": "hash"})
    feed = _row("feed", caption="Approved feed copy.")
    store, artifacts = _Store([feed]), _Artifacts()
    _reviewed_ig_artifact(monkeypatch, store, dict(feed, image_url="https://cdn.example/feed-art.png"))
    source_id = "content_calendar:feed:caption"
    source_hash = repair.hashlib.sha256(feed["caption"].encode()).hexdigest()
    # created_at.desc: the Story render is the NEWEST record for this source.
    store.cache[(repair._eq(source_id), repair._eq(source_hash))].insert(
        0, _story_artifact_for(feed, "https://cdn.example/story-art.png"))
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("valid feed artifact must be reused")))
    out = repair.run(store=store, artifact_store=artifacts)
    assert out["reused"] == out["repaired"] == 1
    assert out["generated"] == 0
    assert store.rows["feed"]["image_url"] == "https://cdn.example/feed-art.png"
    assert store.rows["feed"]["media_not_ready_reason"] is None


def test_only_story_artifacts_means_no_feed_reuse(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.infographic_evidence, "brain_snapshot",
                        lambda: {"source": "hash"})
    feed = _row("feed", caption="Approved feed copy.")
    store, artifacts = _Store([feed]), _Artifacts()
    source_id = "content_calendar:feed:caption"
    source_hash = repair.hashlib.sha256(feed["caption"].encode()).hexdigest()
    store.cache[(repair._eq(source_id), repair._eq(source_hash))] = [
        _story_artifact_for(feed, "https://cdn.example/story-art.png")]
    generated = []
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda row, account, **kw: generated.append(row["id"]) or
                        {"ok": True, "image_url": "https://new.example/feed.png"})
    out = repair.run(store=store, artifact_store=artifacts)
    assert out["reused"] == 0
    assert out["generated"] == out["repaired"] == 1
    assert generated == ["feed"]
    assert store.rows["feed"]["image_url"] == "https://new.example/feed.png"


def test_facebook_mirror_ignores_story_only_ig_artifacts(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.config, "lasso_infographic_quality_enabled",
                        lambda key: key in ("lasso_ig", "lasso_fb"))
    monkeypatch.setattr(repair.infographic_evidence, "brain_snapshot",
                        lambda: {"source": "hash"})
    caption = "The same approved copy."
    ig = _row("ig", caption=caption, image_url="https://cdn.example/ig-feed.png",
              media_not_ready_reason=None)
    fb = _row("fb", account="facebook", caption=caption)
    store, artifacts = _Store([ig, fb]), _Artifacts()
    # The IG source has only a Story render; the FB mirror must keep waiting
    # rather than bind 9:16 media as feed artwork or pay for a new visual.
    source_id = "content_calendar:ig:caption"
    source_hash = repair.hashlib.sha256(caption.encode()).hexdigest()
    store.cache[(repair._eq(source_id), repair._eq(source_hash))] = [
        _story_artifact_for(ig, "https://cdn.example/ig-story.png")]
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("story-only mirror must not generate")))
    out = repair.run(store=store, artifact_store=artifacts, account_key="lasso_fb")
    assert out["generated"] == out["repaired"] == out["reused"] == 0
    assert out["skipped"] == 1
    assert store.rows["fb"]["media_not_ready_reason"] == repair.HOLD_REASON


def test_malformed_dimension_claim_fails_closed_without_reuse_or_spend(
        monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.infographic_evidence, "brain_snapshot",
                        lambda: {"source": "hash"})
    feed = _row("feed", caption="Approved feed copy.")
    store, artifacts = _Store([feed]), _Artifacts()
    _reviewed_ig_artifact(monkeypatch, store, feed)
    source_id = "content_calendar:feed:caption"
    source_hash = repair.hashlib.sha256(feed["caption"].encode()).hexdigest()
    record = store.cache[(repair._eq(source_id), repair._eq(source_hash))][0]
    record["evidence"]["aspect"] = "4:5"
    record["evidence"]["verified_dimensions"] = {"width": "1080", "height": 1350}
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("malformed record must not spend")))
    out = repair.run(store=store, artifact_store=artifacts)
    assert out["errors"] == 1
    assert out["generated"] == out["repaired"] == out["reused"] == 0
    assert store.patches == []
    assert store.rows["feed"]["media_not_ready_reason"] == repair.HOLD_REASON


def test_exact_facebook_mirror_reuses_ig_reviewed_visual_and_records_provenance(
        monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.config, "lasso_infographic_quality_enabled",
                        lambda key: key in ("lasso_ig", "lasso_fb"))
    logical_id = "11111111-1111-4111-8111-111111111111"
    caption = "One approved caption for this paired post."
    ig = _row("ig", caption=caption, image_url="https://new.example/ig.png",
              media_not_ready_reason=None, logical_post_id=logical_id)
    fb = _row("fb", account="facebook", caption=caption,
              logical_post_id=logical_id)
    store, artifacts = _Store([ig, fb]), _Artifacts()
    _reviewed_ig_artifact(monkeypatch, store, ig)
    fb_source_id = "content_calendar:fb:caption"
    fb_source_hash = repair.hashlib.sha256(caption.encode()).hexdigest()
    store.cache[(repair._eq(fb_source_id), repair._eq(fb_source_hash))] = [{
        "image_url": "https://new.example/older-fb.png",
        "source_identity": {"source_id": fb_source_id,
                            "source_hash": fb_source_hash},
        "evidence": {"policy_version": repair.infographic_evidence.POLICY_VERSION,
                     "brain_snapshot": {"source": "hash"},
                     "brief_model": "gpt-6-astra", "grade_status": "PASS",
                     "image_sha256": "older-hash", "review_response_id": "review"},
    }]
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("matching FB mirror must not generate")))

    out = repair.run(store=store, artifact_store=artifacts, account_key="lasso_fb")

    assert out["repaired"] == out["reused"] == 1
    assert out["generated"] == 0
    assert store.rows["fb"]["image_url"] == ig["image_url"]
    assert store.rows["fb"]["caption"] == caption
    fb_record = store.by_url[("eq.lasso_fb", repair._eq(ig["image_url"]))][0]
    assert fb_record["source_identity"] == {
        "source_id": "content_calendar:fb:caption",
        "source_hash": repair.hashlib.sha256(caption.encode()).hexdigest(),
    }
    assert fb_record["evidence"]["mirror_reuse_from"]["source_id"] == \
        "content_calendar:ig:caption"


def test_facebook_copy_difference_keeps_its_own_visual_generation(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.config, "lasso_infographic_quality_enabled",
                        lambda key: key in ("lasso_ig", "lasso_fb"))
    logical_id = "11111111-1111-4111-8111-111111111111"
    ig = _row("ig", caption="IG specific copy.", image_url="https://new.example/ig.png",
              media_not_ready_reason=None, logical_post_id=logical_id)
    fb = _row("fb", account="facebook", caption="FB specific copy.",
              logical_post_id=logical_id)
    store, artifacts = _Store([ig, fb]), _Artifacts()
    generated = []
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda row, account, **kwargs: generated.append(account) or
                        {"ok": True, "image_url": "https://new.example/fb.png"})
    out = repair.run(store=store, artifact_store=artifacts, account_key="lasso_fb")
    assert out["generated"] == out["repaired"] == 1
    assert generated == ["lasso_fb"]
    assert store.rows["fb"]["image_url"] == "https://new.example/fb.png"


def test_exact_facebook_mirror_waits_when_ig_visual_is_still_held(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.config, "lasso_infographic_quality_enabled",
                        lambda key: key in ("lasso_ig", "lasso_fb"))
    caption = "The same approved copy."
    ig = _row("ig", caption=caption)
    fb = _row("fb", account="facebook", caption=caption)
    store, artifacts = _Store([ig, fb]), _Artifacts()
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("held IG mirror must not trigger FB generation")))
    out = repair.run(store=store, artifact_store=artifacts, account_key="lasso_fb")
    assert out["generated"] == out["repaired"] == 0
    assert out["skipped"] == 1
    assert store.rows["fb"]["media_not_ready_reason"] == repair.HOLD_REASON


def test_facebook_mirror_provenance_conflict_keeps_hold(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.config, "lasso_infographic_quality_enabled",
                        lambda key: key in ("lasso_ig", "lasso_fb"))
    caption = "One exact mirrored caption."
    ig = _row("ig", caption=caption, image_url="https://new.example/ig.png",
              media_not_ready_reason=None)
    fb = _row("fb", account="facebook", caption=caption)
    store, artifacts = _Store([ig, fb]), _Artifacts()
    _reviewed_ig_artifact(monkeypatch, store, ig)
    store.by_url[("eq.lasso_fb", repair._eq(ig["image_url"]))] = [{
        "image_url": ig["image_url"],
        "source_identity": {"source_id": "someone-else", "source_hash": "other"},
        "evidence": {},
    }]
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("conflicting artifact must not generate")))
    out = repair.run(store=store, artifact_store=artifacts, account_key="lasso_fb")
    assert out["errors"] == 1 and out["generated"] == out["repaired"] == 0
    assert store.rows["fb"]["media_not_ready_reason"] == repair.HOLD_REASON


def test_changed_row_after_generation_is_not_swapped_and_cached_artifact_reuses(
        monkeypatch):
    _armed(monkeypatch)
    store, artifacts = _Store([_row("one")]), _Artifacts()
    generated = []

    def fake_generate(row, account, **kwargs):
        generated.append(row["id"])
        # Simulate a concurrent media edit after the paid generation. The real
        # variant path has persisted the reviewed artifact by this point.
        store.rows["one"]["image_url"] = "https://old.example/edited.jpg"
        source_id = f"content_calendar:{row['id']}:caption"
        source_hash = repair.hashlib.sha256(row["caption"].encode()).hexdigest()
        store.cache[(repair._eq(source_id), repair._eq(source_hash))] = [{
            "image_url": "https://new.example/one.png",
            "source_identity": {"source_id": source_id, "source_hash": source_hash},
            "evidence": {"policy_version": repair.infographic_evidence.POLICY_VERSION,
                         "brain_snapshot": {"source": "hash"},
                         "brief_model": "gpt-6-astra", "grade_status": "PASS",
                         "image_sha256": "hash", "review_response_id": "review"},
        }]
        return {"ok": True, "image_url": "https://new.example/one.png"}

    monkeypatch.setattr(variant_regen, "generate_variant_image", fake_generate)
    monkeypatch.setattr(repair.infographic_evidence, "brain_snapshot",
                        lambda: {"source": "hash"})
    first = repair.run(store=store, artifact_store=artifacts)
    assert first["generated"] == 1 and first["repaired"] == 0
    assert store.patches == []

    second = repair.run(store=store, artifact_store=artifacts)
    assert second["generated"] == 0 and second["reused"] == 1
    assert second["repaired"] == 1
    assert generated == ["one"]
    params, payload = store.patches[0]
    assert params["caption"] == repair._eq("Approved copy for one.")
    assert params["status"] == repair._eq("pending")
    assert params["media_not_ready_reason"] == repair._eq(repair.HOLD_REASON)
    assert payload["media_not_ready_reason"] is None


def test_disarmed_repair_never_reads_or_generates(monkeypatch):
    monkeypatch.setattr(repair.config, "lasso_three_feed_enabled", lambda: False)
    store = _Store([_row("one")])
    out = repair.run(store=store, artifact_store=_Artifacts())
    assert out["ok"] is False
    assert store.reads == 0
    assert store.patches == []


def test_caption_change_repairs_feed_but_never_swaps_a_managed_story(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair.infographic_evidence, "brain_snapshot",
                        lambda: {"source": "hash"})
    logical_id = "11111111-1111-4111-8111-111111111111"
    feed = _row("feed", caption="New approved source copy.",
                logical_post_id=logical_id,
                media_not_ready_reason=repair.CAPTION_HOLD_REASON)
    story = _row("story", format="story", caption="", logical_post_id=logical_id,
                 media_not_ready_reason=repair.CAPTION_HOLD_REASON)
    store, artifacts = _Store([feed, story]), _Artifacts()
    generated = []
    def fake_generate(row, account, **kwargs):
        generated.append((row["id"], row["caption"], row["format"]))
        url = f"https://new.example/{row['id']}.png"
        source_id = f"content_calendar:{row['id']}:caption"
        source_hash = repair.hashlib.sha256(row["caption"].encode()).hexdigest()
        store.cache[(repair._eq(source_id), repair._eq(source_hash))] = [{
            "image_url": url,
            "source_identity": {"source_id": source_id, "source_hash": source_hash},
            "evidence": {"policy_version": repair.infographic_evidence.POLICY_VERSION,
                         "brain_snapshot": {"source": "hash"},
                         "brief_model": "gpt-6-astra", "grade_status": "PASS",
                         "image_sha256": "reviewed-hash", "review_response_id": "review"},
        }]
        return {"ok": True, "image_url": url}
    monkeypatch.setattr(variant_regen, "generate_variant_image", fake_generate)

    out = repair.run(store=store, artifact_store=artifacts)
    # Only the feed is repaired. A managed Story is never regenerated from its
    # own id nor swapped by a direct CAS here; the daily paired Story job owns
    # it so the managed registry binding stays intact.
    assert out["repaired"] == out["generated"] == 1
    assert generated == [("feed", feed["caption"], "feed")]
    assert store.rows["feed"]["media_not_ready_reason"] is None
    assert store.rows["story"]["media_not_ready_reason"] == repair.CAPTION_HOLD_REASON
    assert store.rows["story"]["image_url"] == "https://old.example/story.jpg"
    assert len(artifacts.claims) == 1
    assert all(params["id"] == "eq.feed" for params, _ in store.patches)


def test_caption_story_stays_held_while_feed_or_review_is_unready(monkeypatch):
    _armed(monkeypatch)
    feed = _row("feed", caption="New approved source copy.",
                media_not_ready_reason=repair.CAPTION_HOLD_REASON)
    story = _row("story", format="story", caption="",
                 media_not_ready_reason=repair.CAPTION_HOLD_REASON)
    store, artifacts = _Store([feed, story]), _Artifacts()
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda row, account, **kw: {"ok": True,
                        "image_url": f"https://new.example/{row['id']}.png"})
    out = repair.run(store=store, artifact_store=artifacts)
    assert out["repaired"] == 0
    assert store.rows["feed"]["media_not_ready_reason"] == repair.CAPTION_HOLD_REASON
    assert store.rows["story"]["media_not_ready_reason"] == repair.CAPTION_HOLD_REASON
    assert len(store.patches) == 0


def test_caption_hold_cannot_clear_by_reusing_the_old_visual(monkeypatch):
    _armed(monkeypatch)
    row = _row("feed", media_not_ready_reason=repair.CAPTION_HOLD_REASON)
    store = _Store([row])
    assert repair._replace_exact(store, row, row["image_url"]) is None
    assert store.patches == []


def test_generic_repair_never_generates_or_swaps_a_held_story(monkeypatch):
    _armed(monkeypatch)
    caption = "New approved source copy."
    feed = _row("feed", caption=caption, image_url="https://new.example/feed.png",
                media_not_ready_reason=None)
    stories = [
        _row("story-caption", format="story", caption="",
             media_not_ready_reason=repair.CAPTION_HOLD_REASON),
        _row("story-repeat", format="story", caption="",
             media_not_ready_reason=repair.HOLD_REASON),
    ]
    store, artifacts = _Store([feed] + stories), _Artifacts()
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda row, account, **kw: (_ for _ in ()).throw(
                            AssertionError("held Story must never be regenerated here")))
    out = repair.run(store=store, artifact_store=artifacts)
    assert out["attempted"] == out["generated"] == out["repaired"] == 0
    assert artifacts.claims == [] and store.patches == []
    assert store.rows["story-caption"]["media_not_ready_reason"] == \
        repair.CAPTION_HOLD_REASON
    assert store.rows["story-repeat"]["media_not_ready_reason"] == repair.HOLD_REASON


def _post_grade_preparation_helper(runner):
    """Root's post-grade runner commit (73fe93c, integrated tree only) extracts
    held-media + paired-Story preparation into this helper. It is absent on
    this worker branch by design; root's integrated check runs these tests."""
    helper = getattr(runner, "_lasso_held_media_and_story_preparation", None)
    if helper is None:
        pytest.skip("runner._lasso_held_media_and_story_preparation lands with "
                    "root's post-grade runner commit; validated on the "
                    "integrated tree")
    return helper


def _call_helper(helper, day):
    import inspect
    if "scheduled_for" in inspect.signature(helper).parameters:
        return helper(scheduled_for=day)
    return helper(day)


def _stub_repair_waves(monkeypatch):
    from agent.jobs import lasso_daily_paired_stories as daily_stories
    repairs, pairings = [], []
    monkeypatch.setattr(repair, "run", lambda **kwargs:
                        repairs.append(kwargs) or {"ok": True, "attempted": 0,
                        "generated": 0, "reused": 0, "repaired": 0,
                        "skipped": 0, "errors": 0})
    monkeypatch.setattr(daily_stories, "run", lambda **kwargs:
                        pairings.append(kwargs) or {"ok": True,
                        "account": kwargs.get("account"), "eligible": 0,
                        "generated": 0, "reused": 0, "staged": 0,
                        "repaired": 0, "occupied": 0, "blocked": 0})
    return repairs, pairings


def test_no_voice_run_has_no_media_repair_wave_at_all(monkeypatch):
    """Post-grade ordering: held-media repair runs after grade/drafting, so a
    run without a voice doc exits at no_voice with zero repair or paired
    Story calls — no repair wave, and no possibility of a double generation.
    """
    from agent import runner
    _post_grade_preparation_helper(runner)
    monkeypatch.setattr(runner.config, "master_enabled", lambda: True)
    monkeypatch.setattr(runner.config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(runner.config, "calendar_autopublish_enabled",
                        lambda: True)
    monkeypatch.setattr(runner, "load_voice", lambda path: None)
    repairs, pairings = _stub_repair_waves(monkeypatch)

    class Poster:
        def post_notice(self, message):
            pass

    out = runner.run_daily(poster=Poster(), scheduled_for="2026-10-05T12:00:00+00:00")
    assert out["status"] == "no_voice"
    assert repairs == []
    assert pairings == []


def test_past_coverage_follows_publisher_catchup_window_not_fixed_dates(
        monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair, "_local_day", lambda now: date(2026, 10, 6))
    rows = [_row("current", day="2026-10-06")]
    rows += [_row(f"old-{i}", day="2026-10-02") for i in range(3)]
    rows += [_row("outside", day="2026-09-28")]

    class Store(_Store):
        def list_pending_media_between(self, gym, first, last):
            self.reads += 1
            # During the incident window the publisher lookback still covers
            # Oct 2, but never earlier than the dynamic catchup boundary.
            assert (gym, first, last) == ("lasso", "2026-09-29", "2026-10-07")
            return [deepcopy(row) for row in self.rows.values()]

    store = Store(rows)
    generated = []
    def fake_generate(row, account, **kwargs):
        generated.append(row["id"])
        return {"ok": True, "image_url": f"https://new.example/{row['id']}.png"}
    monkeypatch.setattr(variant_regen, "generate_variant_image", fake_generate)
    out = repair.run(store=store, artifact_store=_Artifacts())
    # Current runway first, then at most two past attempts per run so a held
    # backlog can never become an unbounded paid generation wave.
    assert out["attempted"] == out["repaired"] == 3
    assert generated == ["current", "old-0", "old-1"]
    assert store.rows["old-2"]["media_not_ready_reason"] == repair.HOLD_REASON
    assert store.rows["outside"]["media_not_ready_reason"] == repair.HOLD_REASON


def test_held_media_preparation_opts_in_only_during_recovery_dates(monkeypatch):
    """The post-grade preparation helper wires the incident backlog flag only
    inside the dated recovery window, calls each account exactly once per
    invocation (no double generation wave), and never opts in afterwards."""
    from agent import runner
    helper = _post_grade_preparation_helper(runner)
    monkeypatch.setattr(runner.config, "master_enabled", lambda: True)
    monkeypatch.setattr(runner.config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(runner.config, "calendar_autopublish_enabled",
                        lambda: True)
    repairs, pairings = _stub_repair_waves(monkeypatch)

    _call_helper(helper, "2026-10-06T12:00:00+00:00")
    assert repairs == [
        {"now": "2026-10-06T12:00:00+00:00", "account_key": "lasso_ig",
         "include_incident_backlog": True},
        {"now": "2026-10-06T12:00:00+00:00", "account_key": "lasso_fb",
         "include_incident_backlog": True},
    ]
    repairs.clear()
    pairings.clear()
    _call_helper(helper, "2026-10-12T12:00:00+00:00")
    assert all("include_incident_backlog" not in kwargs for kwargs in repairs)
    assert [kw.get("account_key") for kw in repairs].count("lasso_ig") <= 1
    assert [kw.get("account_key") for kw in repairs].count("lasso_fb") <= 1


def test_past_hold_ages_out_with_the_publisher_catchup_window(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(repair, "_local_day", lambda now: date(2026, 10, 6))
    class Store(_Store):
        def list_pending_media_between(self, gym, first, last):
            self.reads += 1
            assert (gym, first, last) == ("lasso", "2026-09-29", "2026-10-07")
            return [deepcopy(row) for row in self.rows.values()]
    store = Store([_row("old", day="2026-10-02")])
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda row, account, **kw: {"ok": True,
                        "image_url": "https://new.example/old.png"})
    # No recovery-window opt-in is needed: Oct 2 is inside the publisher
    # catchup window on Oct 6, so the held feed repairs on a normal run.
    out = repair.run(store=store, artifact_store=_Artifacts())
    assert out["attempted"] == out["repaired"] == 1
    assert store.rows["old"]["media_not_ready_reason"] is None

    # On Oct 12 the same row is older than the publisher catchup window; it
    # ages out exactly like an unpublishable catchup row and keeps its hold.
    monkeypatch.setattr(repair, "_local_day", lambda now: date(2026, 10, 12))
    store.rows["old"] = _row("old", day="2026-10-02")
    class LateStore(_Store):
        def list_pending_media_between(self, gym, first, last):
            self.reads += 1
            assert (gym, first, last) == ("lasso", "2026-10-05", "2026-10-13")
            return [deepcopy(row) for row in self.rows.values()]
    late = LateStore([_row("old", day="2026-10-02")])
    out = repair.run(store=late, artifact_store=_Artifacts())
    assert out["attempted"] == 0
    assert late.rows["old"]["media_not_ready_reason"] == repair.HOLD_REASON
    assert late.patches == []


def test_oct7_held_feed_still_repairs_on_oct8_retry(monkeypatch):
    """Regression: fixed Oct 2-5 backlog bounds silently dropped Oct 6/7 holds."""
    _armed(monkeypatch)
    monkeypatch.setattr(repair, "_local_day", lambda now: date(2026, 10, 8))
    class Store(_Store):
        def list_pending_media_between(self, gym, first, last):
            self.reads += 1
            # The publisher catchup lookback on Oct 8 reaches Oct 1.
            assert (gym, first, last) == ("lasso", "2026-10-01", "2026-10-09")
            return [deepcopy(row) for row in self.rows.values()]
    store = Store([_row("oct7", day="2026-10-07")])
    monkeypatch.setattr(variant_regen, "generate_variant_image",
                        lambda row, account, **kw: {"ok": True,
                        "image_url": "https://new.example/oct7.png"})
    out = repair.run(store=store, artifact_store=_Artifacts())
    assert out["attempted"] == out["generated"] == out["repaired"] == 1
    assert store.rows["oct7"]["media_not_ready_reason"] is None
    assert store.rows["oct7"]["image_url"] == "https://new.example/oct7.png"
