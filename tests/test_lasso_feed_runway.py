"""Meaningful offline runway proofs; no paid renderer or production writer."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import uuid

import pytest
from agent import build_lock, config, caption_ledger, real_month_run, real_month_planner
from agent.jobs import lasso_feed_runway as job, lasso_cadence_recovery as lane

NOW = datetime(2026, 10, 8, 10, tzinfo=ZoneInfo("America/New_York"))
DAY = "2026-10-08"


def feed(account="instagram", slot=2, fmt="feed", **kw):
    return dict(id=str(uuid.uuid4()), gym_id="lasso", account=account, format=fmt,
                post_date=DAY, slot_index=slot, variant_status="active", status="pending",
                logical_post_id=str(uuid.uuid4()), caption="Approved original", **kw)


class Store:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.staged = []
    def gym_autonomy(self, gym):
        assert gym == "lasso"
        return True
    def rows_in_range_complete(self, gym, first, last, **kw):
        assert gym == "lasso" and kw == {"all_statuses": True}
        return deepcopy(self.rows)
    def stage_lasso_runway_feed(self, row, **kw):
        self.staged.append(deepcopy(row))
        self.rows.append(deepcopy(row))
        return {"result": "inserted", "id": row["id"]}
    def get_row(self, gym, row_id):
        return deepcopy(next(r for r in self.rows if r["id"] == row_id))


class Artifacts:
    available = True
    def __init__(self):
        self.claims = []
    def claim(self, *args):
        self.claims.append(args)
        return True
    def release(self, *args):
        pass


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_FEED_RUNWAY", "true")
    for f in ("real_month_plan_enabled", "lasso_three_feed_enabled", "logical_post_id_enabled"):
        monkeypatch.setattr(config, f, lambda: True)
    monkeypatch.setattr(config, "lasso_infographic_quality_enabled", lambda _: True)
    from agent import variant_regen
    monkeypatch.setattr(variant_regen, "enabled", lambda: True)
    monkeypatch.setattr(build_lock, "acquire", lambda *a, **k: True)
    monkeypatch.setattr(build_lock, "release", lambda *a, **k: None)
    monkeypatch.setattr(build_lock, "start_heartbeat", lambda *a, **k: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda *a: False)
    monkeypatch.setattr(caption_ledger, "record_staged_strict", lambda *a: None)
    monkeypatch.setattr(job, "_grade", lambda *a: True)
    monkeypatch.setattr(job, "_source_snapshot", lambda: {"approved": "fixed"})
    # Map an approved planner draft without reproducing the full planner here.
    def rows(drafts, gym):
        return [dict(gym_id=gym, account=a, format="feed", post_date=DAY, slot_index=0,
                     caption="Approved fresh source copy", pillar="doctrine", status="pending",
                     image_url="", logical_post_id=str(uuid.uuid4()))
                for a in ("instagram", "facebook")]
    monkeypatch.setattr(real_month_planner, "to_calendar_rows", rows)
    monkeypatch.setattr(job.repair, "_reviewed_artifact_record", lambda *a:
                        {"image_url": "https://cdn.example/reviewed.png"})


def planner(*args, **kw):
    assert args[2] == 1 and kw["source_only"] is True
    assert kw["slot_selector"](SimpleNamespace(fmt="feed", post_date=DAY, cadence_slot=0))
    assert not kw["slot_selector"](SimpleNamespace(fmt="story", post_date=DAY, cadence_slot=0))
    assert not kw["slot_selector"](SimpleNamespace(fmt="feed", post_date=DAY, cadence_slot=2))
    return [SimpleNamespace(source_fragments=["Approved fresh source copy"])]


def test_summit_masks_no_regular_slots_and_only_target_account_inserts(armed):
    original = [feed("instagram", s) for s in range(3)] + [feed("facebook", 2)]
    store = Store(original)
    out = job.run(account_key="lasso_fb", now=NOW, store=store,
                  artifact_store=Artifacts(), horizon_days=2, planner=planner)
    assert out["ok"] and out["inserted"] == 1 and out["reused"] == 1
    assert out["missing"][:2] == [["facebook", DAY, 0], ["facebook", DAY, 1]]
    assert store.rows[:-1] == original
    assert len(store.staged) == 1 and store.staged[0]["account"] == "facebook"
    assert store.staged[0]["scheduled_at"] == "2026-10-08T07:30:00-04:00"


def test_orphan_story_never_gets_new_feed_underneath(armed):
    store = Store([feed("facebook", 0, "story")])
    def forbidden(*a, **k):
        raise AssertionError("slot0 source must never build")
    # Other empty slots can progress, but the first orphan remains a named hold.
    out = job.run(account_key="lasso_fb", now=NOW, store=store,
                  artifact_store=Artifacts(), horizon_days=2, planner=forbidden)
    assert out["target"] == ["facebook", DAY, 1] and out["blocked"] >= 1
    assert store.staged == []


@pytest.mark.parametrize("change", [dict(slot_index=None), dict(logical_post_id="malformed"),
                                   dict(gym_id="other"), dict(account="legacy"), dict(variant_status=None)])
def test_ambiguous_source_book_holds_before_build(armed, change):
    row = feed(); row.update(change)
    store = Store([row])
    out = job.run(account_key="lasso_fb", now=NOW, store=store,
                  artifact_store=Artifacts(), horizon_days=2, planner=planner)
    assert not out["ok"] and not store.staged and out["attempted"] == 0


@pytest.mark.parametrize("status", ["pending", "approved", "publishing", "published", "failed"])
def test_any_existing_active_feed_is_occupied(status):
    row = feed(); row["status"] = status
    assert ("instagram", DAY, "feed", 2) in job._coverage([row], DAY, DAY)


def test_disabled_and_unknown_autonomy_are_zero_work(armed, monkeypatch):
    monkeypatch.delenv("AGENT_LASSO_FEED_RUNWAY")
    assert job.run(account_key="lasso_fb")["reason"] == "runway lane disabled"
    monkeypatch.setenv("AGENT_LASSO_FEED_RUNWAY", "true")
    store = Store([]); store.gym_autonomy = lambda _: None
    assert not job.run(account_key="lasso_fb", store=store, artifact_store=Artifacts())["ok"]
    assert not store.staged


def test_render_requires_durable_exact_caption_review(armed, monkeypatch):
    store = Store([])
    monkeypatch.setattr(job.repair, "_reviewed_artifact_record", lambda *a: None)
    calls = []
    def render(row, account):
        calls.append((deepcopy(row), account))
        return {"ok": True, "image_url": "https://unproven.example"}
    out = job.run(account_key="lasso_fb", now=NOW, store=store, artifact_store=Artifacts(),
                  horizon_days=2, planner=planner, render=render)
    assert out["generated"] == 1 and out["reason"] == "exact-caption PASS artifact missing"
    assert len(calls) == 1 and calls[0][0]["id"] and not store.staged


def test_source_changes_during_render_refuse_stale_insert(armed, monkeypatch):
    snapshots = iter([{"approved": "old"}, {"approved": "new"}])
    monkeypatch.setattr(job, "_source_snapshot", lambda: next(snapshots))
    store = Store([])
    out = job.run(account_key="lasso_fb", now=NOW, store=store, artifact_store=Artifacts(),
                  horizon_days=2, planner=planner)
    assert out["reason"] == "approved source changed during render" and not store.staged


def test_grade_uses_existing_book_without_mutation(monkeypatch):
    from agent import calendar_grade
    existing = [feed()]; original = deepcopy(existing); observed = []
    monkeypatch.setattr(config, "calendar_grade_enabled_for", lambda _: True)
    monkeypatch.setattr(calendar_grade, "grade_month", lambda rows, **kw:
                        observed.append(rows) or SimpleNamespace(total=100))
    candidate = feed("facebook", 0)
    assert job._grade(existing, candidate)
    assert observed == [existing + [candidate]] and existing == original


def test_worker_topup_precedes_repair_and_stories(monkeypatch, tmp_path):
    from agent.jobs import lasso_held_media_repair, lasso_daily_paired_stories
    monkeypatch.setenv("AGENT_LASSO_CADENCE_RECOVERY", "true")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(lane, "_record", lambda *a: None)
    monkeypatch.setattr(lane, "_publish_limits", lambda *a: (0, 3))
    calls = []
    monkeypatch.setattr(job, "run", lambda **kw: calls.append("runway") or {"ok": True})
    monkeypatch.setattr(lasso_held_media_repair, "run", lambda **kw: calls.append("repair") or {"ok": True})
    monkeypatch.setattr(lasso_daily_paired_stories, "run", lambda **kw: calls.append("stories") or {"ok": True})
    assert lane.run_prep_once("lasso_fb", now=NOW, store=Store([]))["ok"]
    assert calls == ["runway", "repair", "stories"]


def test_source_only_builder_map_never_calls_ordinary_paid_sources(monkeypatch):
    from agent import daily_studio
    account = SimpleNamespace(key="lasso_ig", platform="instagram")
    monkeypatch.setattr(config, "lasso_editorial_calendar_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(daily_studio, "build_daily_infographic_draft",
        lambda *a, **kw: calls.append(kw) or SimpleNamespace(caption="approved"))
    builders = real_month_run.real_builders_map(account, source_only=True)
    assert not {"podcast", "welcome", "testimonial"} & builders.keys()
    for name, builder in builders.items():
        builder(None, DAY)
    assert calls and all(k.get("source_only") is True for k in calls)


def test_fresh_insert_autonomy_gate_closes_after_review(armed, monkeypatch):
    store = Store([])
    reads = iter([True, True, False])
    store.gym_autonomy = lambda gym: next(reads)
    out = job.run(account_key="lasso_fb", now=NOW, store=store, artifact_store=Artifacts(),
                  horizon_days=2, planner=planner)
    assert out["reason"] == "fresh insert gate closed" and not store.staged


@pytest.mark.parametrize("account_key,platform", [("lasso_ig", "instagram"), ("lasso_fb", "facebook")])
@pytest.mark.parametrize("logical_enabled", [False, True])
def test_real_selected_summit_source_path_preserves_target_account(monkeypatch, account_key, platform, logical_enabled):
    from agent import cadence
    monkeypatch.setenv("AGENT_REAL_MONTH_PLAN", "true")
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    monkeypatch.setenv("AGENT_LASSO_EDITORIAL_CALENDAR", "true")
    monkeypatch.setattr(config, "lasso_editorial_calendar_enabled", lambda: True)
    monkeypatch.setattr(config, "logical_post_id_enabled", lambda: logical_enabled)
    monkeypatch.setattr(cadence, "resolve_posts_per_day_live", lambda _: 3)
    account = SimpleNamespace(key=account_key, platform=platform)
    drafts = real_month_run.plan_and_build(account_key, DAY, 1, account=account,
        source_only=True, slot_selector=lambda s: s.fmt == "feed" and s.cadence_slot == 2,
        book_dates=[], welcome_dates=[], summit_day_fn=lambda d: False,
        sprint_day_fn=lambda d: False, sprint_feed_count_fn=lambda d: 0)
    assert len(drafts) == 1 and not drafts[0].creative_public_url
    rows = real_month_planner.to_calendar_rows(drafts, "lasso")
    selected = [r for r in rows if r["account"] == platform]
    assert len(selected) == 1 and selected[0]["slot_index"] == 2
    assert selected[0]["pillar"] == "summit" and selected[0]["caption"]

    assert ("logical_post_id" in selected[0]) is logical_enabled


def test_unreadable_strict_caption_reservation_stops_before_render_or_write(armed, monkeypatch):
    def broken(*a):
        raise RuntimeError("ledger receipt unreadable")
    monkeypatch.setattr(caption_ledger, "record_staged_strict", broken)
    store = Store([]); artifacts = Artifacts()
    out = job.run(account_key="lasso_fb", now=NOW, store=store, artifact_store=artifacts,
                  horizon_days=2, planner=planner)
    assert "ledger receipt unreadable" in out["reason"]
    assert not store.staged and not artifacts.claims


def test_pending_inventory_changes_during_render_refuse_fresh_grade(armed, monkeypatch, tmp_path):
    from pathlib import Path
    from agent import calendar_grade, social_proof
    voice = tmp_path / "brand_voice"
    (voice / "knowledge").mkdir(parents=True)
    pending = voice / "knowledge/03_social_proof_pending.md"
    original = Path(__file__).resolve().parents[1] / "brand_voice/knowledge/03_social_proof_pending.md"
    pending.write_bytes(original.read_bytes())
    monkeypatch.delenv("AGENT_SOCIAL_PROOF_PATH", raising=False)
    monkeypatch.setattr(config, "SOCIAL_PROOF_PATH", "brand_voice/social_proof.md")
    monkeypatch.setattr(calendar_grade, "_proof_inventory_root", lambda: tmp_path)
    monkeypatch.setattr(social_proof, "source_path", lambda _: voice / "social_proof.md")
    monkeypatch.setattr(job, "_grade", lambda existing, candidate:
        calendar_grade._lasso_zero_mention_inventory([candidate]) is not None)
    rendered = []
    monkeypatch.setattr(job.repair, "_reviewed_artifact_record", lambda *a:
        {"image_url": "https://cdn.example/reviewed.png"} if rendered else None)
    def render(row, account):
        rendered.append(row["id"])
        pending.write_bytes(pending.read_bytes() + b"\nsource changed while rendering")
        return {"ok": True, "image_url": "https://cdn.example/reviewed.png"}
    store = Store([])
    out = job.run(account_key="lasso_fb", now=NOW, store=store,
                  artifact_store=Artifacts(), horizon_days=2, planner=planner, render=render)
    assert out["reason"] == "fresh existing plus candidate calendar grade"
    assert out["generated"] == 1 and not store.staged


@pytest.mark.parametrize("key", ["lasso_ig", "lasso_fb"])
def test_source_only_repeated_primary_uses_owned_gym_ledger_and_fresh_fallback(monkeypatch, key):
    from agent.drafter import Draft, DraftStatus
    account = SimpleNamespace(key=key, platform="instagram" if key.endswith("ig") else "facebook")
    monkeypatch.setattr(config, "caption_cooldown_enabled", lambda: True)
    monkeypatch.setattr(config, "logical_post_id_enabled", lambda: True)
    calls = []
    def check(gym, caption, day):
        calls.append((gym, caption))
        return caption == "Repeated approved primary"
    monkeypatch.setattr(caption_ledger, "is_blocked", check)
    def build(caption):
        return lambda target, day: Draft(draft_id="source", account_key=key,
            platform=account.platform, caption=caption, hashtags=[], scheduled_for=DAY, status=DraftStatus.PENDING,
            source_fragments=[caption], creative_path="", creative_public_url="")
    slot = real_month_planner.PlanSlot(post_date=DAY, category="book", fmt="feed", cadence_slot=0)
    drafts = real_month_planner.build_month_drafts([slot], {
        "book": build("Repeated approved primary"), "doctrine": build("Fresh approved fallback")},
        account=account, source_only=True, logger=lambda m: None)
    assert len(drafts) == 1 and drafts[0].caption == "Fresh approved fallback"
    assert calls and {gym for gym, caption in calls} == {"lasso"}
    assert not drafts[0].creative_public_url


def test_normal_account_target_cooldown_scope_remains_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(caption_ledger, "is_blocked", lambda gym, cap, day:
        calls.append(gym) or False)
    draft = SimpleNamespace(caption="Fresh approved copy")
    assert real_month_planner._cooldown_checked(draft, lambda *a: draft,
        SimpleNamespace(key="lasso_ig"), DAY, "book", lambda m: None) is draft
    assert calls == ["lasso_ig"]


@pytest.mark.parametrize("logical_enabled", [False, True])
def test_runway_identity_mode_keeps_global_flag_and_exact_feed_story_source(armed, monkeypatch, logical_enabled):
    from agent.jobs import lasso_paired_story_backfill as pairs, lasso_daily_paired_stories as stories
    monkeypatch.setattr(config, "logical_post_id_enabled", lambda: logical_enabled)
    store = Store([])
    out = job.run(account_key="lasso_fb", now=NOW, store=store,
                  artifact_store=Artifacts(), horizon_days=2, planner=planner)
    assert out["ok"] and len(store.staged) == 1
    staged = store.staged[0]
    expected_feed = str(uuid.uuid5(job.NAMESPACE, f"lasso_fb|{DAY}|0"))
    assert staged["id"] == expected_feed and config.logical_post_id_enabled() is logical_enabled
    logical = staged["logical_post_id"]
    assert logical == (str(uuid.uuid5(job.NAMESPACE, f"logical|lasso_fb|{DAY}|0")) if logical_enabled else None)
    identity = pairs.source_identity(staged)
    assert identity["source_feed_id"] == staged["id"]
    assert identity["source_feed_caption"] == staged["caption"]
    assert identity["source_logical_post_id"] == (logical or "")
    # Exercise the real managed Story readback contract, including legacy NULL.
    story_id = str(uuid.uuid4())
    story_schedule = pairs._scheduled_after_feed(staged, DAY)
    story = dict(staged, id=story_id, format="story", caption="", image_url="https://cdn.test/story.png",
                 source_media_url="https://cdn.test/story.png", scheduled_at=story_schedule,
                 media_not_ready_reason=None)
    action = dict(story_id=story_id, feed_id=staged["id"], account="facebook", date=DAY,
                  slot_index=0, story_scheduled_at=story_schedule, story_image_url=story["image_url"],
                  feed_pillar=staged["pillar"], feed_logical_post_id=logical)
    link = {"story_id": story_id, "feed_id": staged["id"]}
    monkeypatch.setattr(stories.stage, "_one", lambda store, table, params:
                        story if table == "content_calendar" else link)
    stories._verify_staged_pair(store, action)
    link["feed_id"] = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="source-link"):
        stories._verify_staged_pair(store, action)
