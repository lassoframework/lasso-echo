"""Occupied-caption repair uses actual guarded Story/feed CAS helper offline."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import uuid

import pytest
from agent import build_lock, caption_ledger, config, variant_regen
from agent.jobs import lasso_caption_recovery as job, grade_fix, lasso_feed_runway as runway

DAY = "2026-10-08"
NOW = datetime(2026, 10, 8, 9, tzinfo=ZoneInfo("America/New_York"))
OLD = "Old blocked approved caption."
NEW = ("A clear follow up plan helps your gym team.\n\n"
       "Your team can see the next action for every visitor and understand where the booking process needs attention. "
       "Keep the steps useful and easy to follow so each person receives a clear next action.\n\nBook a call.")


def row(fmt="feed", **kwargs):
    result = {key: None for key in job.FIELDS}
    result.update(id=str(uuid.uuid4()), gym_id="lasso", account="instagram", post_date=DAY,
        format=fmt, status="pending", variant_status="active", logical_post_id=str(uuid.uuid4()),
        created_at="2026-10-01T12:00:00+00:00", slot_index=0, caption=OLD if fmt == "feed" else "",
        image_url="https://cdn.example/old.png", pillar="book", scheduled_at=DAY+"T07:30:00-04:00")
    result.update(kwargs)
    return result


class Store:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.calls = []
        self.miss_format = None
        self.claim_during_patch = False
    def gym_autonomy(self, gym):
        assert gym == "lasso"
        return True
    def rows_in_range_complete(self, gym, first, last, **kw):
        return deepcopy(self.rows)
    def active_rows_on_day_complete(self, gym, day):
        return deepcopy([r for r in self.rows if r["post_date"] == day])
    def get_row(self, gym, ident):
        return deepcopy(next(r for r in self.rows if r["id"] == ident))
    def patch_pending_plan(self, gym, ident, **kw):
        original = next(r for r in self.rows if r["id"] == ident)
        self.calls.append((original["format"], ident, deepcopy(kw)))
        if self.claim_during_patch and original["format"] == "feed":
            original["status"] = "publishing"
            original["publish_claim_token"] = str(uuid.uuid4())
        if original != kw["expected_row"] or original["format"] == self.miss_format:
            return None
        if kw.get("caption") is not None:
            original["caption"] = kw["caption"]
        if kw.get("pillar"):
            original["pillar"] = kw["pillar"]
        original["media_not_ready_reason"] = "caption_changed_needs_new_visual"
        return deepcopy(original)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_CAPTION_RECOVERY", "true")
    for name in ("lasso_three_feed_enabled", "caption_cooldown_enabled", "real_month_plan_enabled", "content_brain_enabled"):
        monkeypatch.setattr(config, name, lambda: True)
    monkeypatch.setattr(config, "lasso_infographic_quality_enabled", lambda _: True)
    monkeypatch.setattr(variant_regen, "enabled", lambda: True)
    monkeypatch.setattr(build_lock, "acquire", lambda *a, **kw: True)
    monkeypatch.setattr(build_lock, "release", lambda *a, **kw: None)
    monkeypatch.setattr(build_lock, "start_heartbeat", lambda *a, **kw: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(runway, "_source_snapshot", lambda: {"approved": "unchanged"})
    monkeypatch.setattr(job, "_replacement_grade", lambda *a: True)
    monkeypatch.setattr(grade_fix, "_lasso_caption_regen", lambda log: lambda target, avoid: (NEW, "book"))
    monkeypatch.setattr(grade_fix, "_restamp_levers", lambda cap: {})
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda gym, cap, day: cap == OLD)
    stamps = []
    monkeypatch.setattr(caption_ledger, "record_staged_strict", lambda *a: stamps.append(a))
    def link(store, table, params):
        story_id = params["story_id"][3:]
        story = next(r for r in store.rows if r["id"] == story_id)
        feed = next(r for r in store.rows if r["format"] == "feed" and r["logical_post_id"] == story["logical_post_id"])
        return {"story_id": story_id, "feed_id": feed["id"]}
    monkeypatch.setattr(job.pairs, "_one", link)
    return stamps


def test_exact_single_feed_repair_preserves_other_account_and_identity(armed):
    feed = row(); other = row(account="facebook")
    store = Store([feed, other])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["ok"] and out["repaired"] == 1 and out["attempted"] == 1
    saved = store.get_row("lasso", feed["id"])
    assert saved["caption"] == NEW and saved["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    assert store.rows[1] == other
    assert all(saved[k] == feed[k] for k in job.FIELDS - {"caption", "media_not_ready_reason"})
    assert armed == [("lasso", NEW, DAY)]


def test_story_hold_precedes_feed_caption_mutation(armed):
    feed = row(); story = row("story", logical_post_id=feed["logical_post_id"])
    store = Store([feed, story])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["ok"] and [c[0] for c in store.calls] == ["story", "feed"]
    assert store.rows[1]["caption"] == ""
    assert store.rows[1]["media_not_ready_reason"] == "caption_changed_needs_new_visual"


@pytest.mark.parametrize("change", [dict(status="approved"), dict(status="publishing"), dict(status="published"),
    dict(published_at="receipt"), dict(publish_claim_token="lease"), dict(publish_reservation_day=DAY),
    dict(slot_index=None), dict(logical_post_id="invalid"), dict(created_at=None), dict(media_not_ready_reason="unknown_hold")])
def test_protected_or_incomplete_feed_never_reserves_or_patches(armed, change):
    feed = row(**change); store = Store([feed])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["repaired"] == 0 and not armed and not store.calls


@pytest.mark.parametrize("change", [dict(status="approved"), dict(status="published"), dict(publish_claim_token="lease"),
    dict(logical_post_id=str(uuid.uuid4())), dict(slot_index=None), dict(media_not_ready_reason="unknown_hold")])
def test_protected_or_orphan_story_refuses_before_reservation(armed, change):
    feed = row(); story = row("story", logical_post_id=feed["logical_post_id"]); story.update(change)
    store = Store([feed, story])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert not out["ok"] and not armed and not store.calls


def test_unknown_registry_feed_link_refuses_before_reservation(armed, monkeypatch):
    feed = row(); story = row("story", logical_post_id=feed["logical_post_id"])
    store = Store([feed, story])
    monkeypatch.setattr(job.pairs, "_one", lambda *a: {"story_id": story["id"], "feed_id": str(uuid.uuid4())})
    assert not job.run(account_key="lasso_ig", now=NOW, store=store)["ok"]
    assert not armed and not store.calls


def test_feed_cas_claim_race_reports_partial_and_retains_story_hold(armed):
    feed = row(); story = row("story", logical_post_id=feed["logical_post_id"])
    store = Store([feed, story]); store.claim_during_patch = True
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["partial"] == 1 and not out["ok"] and out["repaired"] == 0
    assert out["applied_ids"] == [story["id"]]
    assert store.rows[0]["caption"] == OLD and store.rows[0]["status"] == "publishing"
    assert store.rows[1]["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    assert len(armed) == 1


def test_story_cas_miss_never_rewrites_feed_and_reports_partial(armed):
    feed = row(); story = row("story", logical_post_id=feed["logical_post_id"])
    store = Store([feed, story]); store.miss_format = "story"
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["partial"] == 1 and not out["ok"] and [c[0] for c in store.calls] == ["story"]
    assert store.rows[0]["caption"] == OLD


def test_ledger_write_failure_is_explicit_partial_and_zero_calendar_write(armed, monkeypatch):
    def broken(*a):
        raise RuntimeError("uncertain ledger write")
    monkeypatch.setattr(caption_ledger, "record_staged_strict", broken)
    store = Store([row()])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["partial"] == 1 and not store.calls


def test_exhausted_source_is_named_and_does_not_render_or_reserve(armed, monkeypatch):
    monkeypatch.setattr(grade_fix, "_lasso_caption_regen", lambda log: lambda *a: None)
    monkeypatch.setattr(job.real_month_run, "plan_and_build", lambda *a, **kw: [])
    store = Store([row()])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["reason"] == "approved same-topic source exhausted" and not armed and not store.calls


def test_grade_replaces_target_and_never_appends_fourth_slot(monkeypatch):
    from agent import calendar_grade
    feed = row(); other = row(account="facebook"); book = [feed, other]; frozen = deepcopy(book); views = []
    monkeypatch.setattr(calendar_grade, "grade_month", lambda view, **kw: views.append(view) or SimpleNamespace(total=96))
    assert job._replacement_grade(book, feed, NEW)
    assert len(views[0]) == len(book) and views[0][0]["caption"] == NEW
    assert views[0][1] == other and book == frozen


def test_default_off_manual_and_wrong_account_are_no_write(armed, monkeypatch):
    store = Store([row()])
    monkeypatch.delenv("AGENT_LASSO_CAPTION_RECOVERY")
    assert job.run(account_key="lasso_ig", now=NOW, store=store)["reason"] == "caption lane disabled"
    monkeypatch.setenv("AGENT_LASSO_CAPTION_RECOVERY", "true")
    assert job.run(account_key="client_ig", now=NOW, store=store)["reason"] == "unknown account"
    store.gym_autonomy = lambda gym: None
    assert not job.run(account_key="lasso_ig", now=NOW, store=store)["ok"]
    assert not armed and not store.calls


def test_expected_topic_cas_refuses_concurrent_pillar_change(armed):
    from agent.portal_calendar_store import SupabaseCalendarStore
    expected = row(); actual = dict(expected, pillar="summit"); calls = []
    class HTTP:
        def patch(self, url, *, params, headers, json, timeout):
            calls.append((params, json))
            assert params["pillar"] == "eq.book"
            return SimpleNamespace(status_code=200, json=lambda: [])
    store = SupabaseCalendarStore(url="https://example.test", service_key="test", http=HTTP())
    assert store.patch_pending_plan("lasso", expected["id"], caption=NEW, pillar="book", expected_row=expected) is None
    assert actual["pillar"] == "summit" and len(calls) == 1


def test_legacy_expected_snapshot_without_pillar_retains_query_shape(armed):
    from agent.portal_calendar_store import SupabaseCalendarStore
    expected = row(); expected.pop("pillar"); calls = []
    class HTTP:
        def patch(self, url, *, params, headers, json, timeout):
            calls.append(params)
            assert "pillar" not in params
            return SimpleNamespace(status_code=200, json=lambda: [dict(expected, **json)])
    store = SupabaseCalendarStore(url="https://example.test", service_key="test", http=HTTP())
    assert store.patch_pending_plan("lasso", expected["id"], caption=NEW, expected_row=expected)
    assert len(calls) == 1


def test_fresh_source_change_refuses_before_reservation(armed, monkeypatch):
    snapshots = iter([{"approved": "old"}, {"approved": "new"}])
    monkeypatch.setattr(runway, "_source_snapshot", lambda: next(snapshots))
    store = Store([row()])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["reason"] == "caption source or protected book changed" and not armed and not store.calls


def test_one_repair_only_and_current_before_tomorrow(armed):
    current = row(); tomorrow = row(post_date="2026-10-09", slot_index=1)
    store = Store([tomorrow, current])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["repaired"] == 1 and out["target_id"] == current["id"]
    assert store.rows[0]["caption"] == OLD and len(armed) == 1


def test_strict_ledger_read_failure_stops_before_reservation(armed, monkeypatch):
    def broken(*a):
        raise RuntimeError("history unreadable")
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", broken)
    store = Store([row()])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert not out["ok"] and not armed and not store.calls


def test_same_fuzzy_caption_with_different_cta_stays_blocked(armed, monkeypatch):
    old = NEW + "\n\nRead more at lassoframework.com."
    target = row(caption=old)
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda gym, cap, day: cap == old)
    monkeypatch.setattr(grade_fix, "_lasso_caption_regen", lambda log: lambda *a: (NEW, "book"))
    store = Store([target])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["reason"] == "approved same-topic source exhausted" and not armed and not store.calls


def test_copy_repair_runs_before_feed_media_story_prep_and_publisher_is_separate(monkeypatch, tmp_path):
    from agent.jobs import lasso_cadence_recovery as lane, lasso_held_media_repair, lasso_daily_paired_stories
    monkeypatch.setenv("AGENT_LASSO_CADENCE_RECOVERY", "true")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(lane, "_record", lambda *a: None)
    monkeypatch.setattr(lane, "_publish_limits", lambda *a: (0, 3))
    calls = []
    monkeypatch.setattr(job, "run", lambda **kw: calls.append("copy") or {"ok": True})
    monkeypatch.setattr(runway, "run", lambda **kw: calls.append("runway") or {"ok": True})
    monkeypatch.setattr(lasso_held_media_repair, "run", lambda **kw: calls.append("media") or {"ok": True})
    monkeypatch.setattr(lasso_daily_paired_stories, "run", lambda **kw: calls.append("story") or {"ok": True})
    assert lane.run_prep_once("lasso_ig", now=NOW, store=Store([]))["ok"]
    assert calls == ["copy", "runway", "media", "story"]


def test_readback_failure_after_mutation_is_partial_not_pure_refusal(armed):
    store = Store([row()])
    def broken(*a):
        raise RuntimeError("readback unavailable")
    store.get_row = broken
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["partial"] == 1 and out["blocked"] == 1 and not out["ok"]
    assert store.rows[0]["media_not_ready_reason"] == "caption_changed_needs_new_visual"


def test_approved_source_only_fallback_preserves_original_identity_and_topic(armed, monkeypatch):
    from agent.drafter import Draft, DraftStatus
    hook = "A clear follow up plan helps your gym team."
    old = hook + "\n\n" + "Older approved body details for this source topic. " * 5
    target = row(caption=old)
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda gym, cap, day: cap == old)
    monkeypatch.setattr(grade_fix, "_lasso_caption_regen", lambda log: lambda *a: None)
    calls = []
    def plan(*args, **kw):
        assert kw["source_only"] is True
        assert kw["slot_selector"](SimpleNamespace(fmt="feed", post_date=DAY, cadence_slot=0))
        calls.append(args)
        draft = Draft(draft_id="source-only", account_key="lasso_ig", platform="instagram",
            caption=NEW, hashtags=[], creative_path="", creative_public_url="", scheduled_for=DAY,
            status=DraftStatus.PENDING, source_fragments=[hook], category="book", day_key=DAY)
        draft.cadence_slot_index = 0
        return [draft]
    monkeypatch.setattr(job.real_month_run, "plan_and_build", plan)
    store = Store([target])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["ok"] and len(calls) == 1
    saved = store.get_row("lasso", target["id"])
    assert saved["id"] == target["id"] and saved["logical_post_id"] == target["logical_post_id"]
    assert saved["slot_index"] == target["slot_index"] and saved["pillar"] == "book"
    assert saved["scheduled_at"] == target["scheduled_at"]


@pytest.mark.parametrize("account", ["lasso_ig", "lasso_fb"])
def test_account_lane_scopes_and_fresh_tomorrow_window(armed, account):
    platform = "instagram" if account.endswith("ig") else "facebook"
    target = row(account=platform, post_date="2026-10-09")
    store = Store([target])
    assert job.run(account_key=account, now=NOW, store=store)["repaired"] == 1
    assert store.rows[0]["account"] == platform and store.rows[0]["post_date"] == "2026-10-09"


def test_source_day_after_tomorrow_is_not_repaired(armed):
    store = Store([row(post_date="2026-10-10")])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["ok"] and out["attempted"] == 0 and not armed and not store.calls


def test_source_gate_switch_after_candidate_refuses_before_reservation(armed, monkeypatch):
    reads = iter([True, False])
    store = Store([row()]); store.gym_autonomy = lambda gym: next(reads)
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["reason"] == "fresh caption gate closed" and not armed and not store.calls


def test_registered_legacy_null_logical_source_and_null_schedule_repair_exactly(armed):
    feed = row(logical_post_id=None, scheduled_at=None, media_not_ready_reason="cross_date_media_repeat_needs_new_visual")
    story = row("story", logical_post_id=None, scheduled_at=None)
    store = Store([feed, story])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert out["ok"] and out["repaired"] == 1
    assert [c[0] for c in store.calls] == ["story", "feed"]
    assert all(r["logical_post_id"] is None and r["scheduled_at"] is None for r in store.rows)
    assert all(c[2]["expected_row"]["logical_post_id"] is None for c in store.calls)


def test_legacy_null_feed_cannot_infer_nonnull_story_identity(armed):
    feed = row(logical_post_id=None, scheduled_at=None)
    story = row("story", logical_post_id=str(uuid.uuid4()))
    store = Store([feed, story])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert not out["ok"] and not armed and not store.calls


def test_legacy_null_identity_requires_exact_registered_story_feed_uuid(armed, monkeypatch):
    feed = row(logical_post_id=None); story = row("story", logical_post_id=None)
    store = Store([feed, story])
    monkeypatch.setattr(job.pairs, "_one", lambda *a: {"story_id": story["id"], "feed_id": str(uuid.uuid4())})
    assert not job.run(account_key="lasso_ig", now=NOW, store=store)["ok"]
    assert not armed and not store.calls


def test_flag_flip_cannot_enter_legacy_helper_write_path(armed, monkeypatch):
    calls = []
    def flag():
        calls.append(1)
        # Initial and fresh wrapper gates passed; the helper's strict branch
        # sees OFF. Even if later calls see ON, the proxy refuses unvalidated
        # context instead of permitting a legacy no-ledger/no-Story write.
        return len(calls) != 3
    monkeypatch.setattr(config, "lasso_three_feed_enabled", flag)
    store = Store([row()])
    out = job.run(account_key="lasso_ig", now=NOW, store=store)
    assert not out["ok"] and not armed and not store.calls
