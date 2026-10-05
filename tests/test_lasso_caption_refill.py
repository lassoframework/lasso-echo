"""Offline guards for additive LASSO refill and autonomous caption repair."""
from types import SimpleNamespace

import pytest

from agent import caption_ledger, config, copy_gate, real_month_planner
from agent.jobs import grade_fix, grade_sweep
from agent.portal_calendar_store import SupabaseCalendarStore


@pytest.fixture(autouse=True)
def _armed_caption_history(monkeypatch):
    monkeypatch.setattr(config, "caption_cooldown_enabled", lambda: True)


def _row(day, slot, account, fmt="feed", *, caption="Approved source copy", rid=None):
    return {
        "id": rid or f"{day}-{slot}-{account}-{fmt}", "gym_id": "lasso",
        "post_date": day, "slot_index": slot, "account": account,
        "format": fmt, "caption": caption, "image_url": f"https://cdn.test/{slot}.jpg",
        "status": "pending", "variant_status": "active",
        "created_at": "2026-10-01T00:00:00Z", "logical_post_id": None,
        "source_media_asset_id": None, "media_not_ready_reason": None,
    }


def test_lasso_refill_preserves_prepared_pair_and_fills_missing_slot(monkeypatch):
    day = "2026-10-08"
    prepared = [_row(day, 0, "instagram"), _row(day, 0, "facebook"),
                _row(day, 0, "instagram", "story")]
    proposed = [
        {k: v for k, v in _row(day, slot, account, fmt,
                              caption=f"Approved source copy {slot}").items() if k != "id"}
        for slot in range(2)
        for account, fmt in (("instagram", "feed"), ("facebook", "feed"),
                             ("instagram", "story"))
    ]
    class Store:
        def __init__(self):
            self.inserted = []
            self.deleted = 0
        def rows_in_range_complete(self, gym, first, last, *, all_statuses=False):
            assert all_statuses is True
            return prepared
        def insert_rows(self, gym, rows):
            self.inserted.extend(rows)
            return rows
        def delete_month(self, *args, **kwargs):
            self.deleted += 1
            raise AssertionError("additive refill must never delete")
    store = Store()
    monkeypatch.setattr(real_month_planner, "to_calendar_rows", lambda drafts, key: proposed)
    monkeypatch.setattr("agent.portal_calendar_store.preserve_and_prune",
                        lambda store, key, months, rows: (rows, 0))
    monkeypatch.setattr("agent.cadence.resolve_posts_per_day", lambda *a, **k: 3)
    result = real_month_planner.apply_month_plan(
        "lasso", [object()], store, span_months=["2026-10"], preserve_existing=True)
    assert result["ok"] and result["deleted"] == 0
    assert len(store.inserted) == 3
    assert {r["slot_index"] for r in store.inserted} == {1}
    assert store.deleted == 0


def test_additive_refill_treats_draft_and_queued_as_occupied(monkeypatch):
    day = "2026-10-08"
    existing = [_row(day, 0, "instagram"), _row(day, 1, "instagram")]
    existing[0]["status"] = "draft"
    existing[1]["status"] = "queued"
    proposed = [{k: v for k, v in _row(day, slot, "instagram",
                                     caption=f"Approved source copy {slot}").items()
                 if k != "id"} for slot in range(3)]
    class Store:
        def __init__(self):
            self.inserted = []
        def rows_in_range_complete(self, gym, first, last, *, all_statuses=False):
            assert all_statuses is True
            return existing
        def insert_rows(self, gym, rows):
            self.inserted.extend(rows)
            return rows
        def delete_month(self, *a, **k):
            raise AssertionError("additive refill cannot delete")
    store = Store()
    monkeypatch.setattr(real_month_planner, "to_calendar_rows", lambda drafts, key: proposed)
    monkeypatch.setattr("agent.portal_calendar_store.preserve_and_prune",
                        lambda store, key, months, rows: (rows, 0))
    monkeypatch.setattr("agent.cadence.resolve_posts_per_day", lambda *a, **k: 3)
    out = real_month_planner.apply_month_plan(
        "lasso", [object()], store, span_months=["2026-10"], preserve_existing=True)
    assert out["ok"] and out["deleted"] == 0
    assert [r["slot_index"] for r in store.inserted] == [2]


def test_complete_calendar_read_includes_all_active_statuses_when_requested():
    calls = []
    class HTTP:
        def get(self, url, *, params, headers, timeout):
            calls.append(dict(params))
            assert "status" not in params
            return SimpleNamespace(status_code=200, json=lambda: [
                _row("2026-10-08", 0, "instagram", rid="a") | {"status": "draft"},
                _row("2026-10-08", 1, "instagram", rid="b") | {"status": "queued"},
            ])
    store = SupabaseCalendarStore(url="https://example.test", service_key="test", http=HTTP())
    rows = store.rows_in_range_complete("lasso", "2026-10-01", "2026-10-31",
                                        all_statuses=True)
    assert [r["status"] for r in rows] == ["draft", "queued"]
    assert calls[0]["variant_status"] == "eq.active"
    assert calls[0]["limit"] == "500"


def test_lasso_refill_fails_before_write_when_complete_read_missing(monkeypatch):
    class Store:
        def delete_month(self, *a, **k):
            raise AssertionError("must not delete")
        def insert_rows(self, *a, **k):
            raise AssertionError("must not insert")
    monkeypatch.setattr(real_month_planner, "to_calendar_rows", lambda drafts, key: [])
    result = real_month_planner.apply_month_plan(
        "lasso", [object()], Store(), span_months=["2026-10"],
        preserve_existing=True)
    assert not result["ok"]
    assert result["reason"] == "store write failed: RuntimeError"


def test_lasso_regen_uses_only_original_source_topic(monkeypatch):
    from agent import content_planner
    doc = content_planner.load_source_doc()
    source = doc.copy_bank["All in one offer"]
    original = "\n\n".join([source["hooks"][0], *source["bodies"], doc.ctas[0]])
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda *a, **k: False)
    regen = grade_fix._lasso_caption_regen(lambda *_: None)
    out = regen(_row("2026-10-08", 0, "instagram", caption=original),
                {original})
    assert out is not None
    caption, _category = out
    assert source["hooks"][0] in caption
    assert all(body in caption for body in source["bodies"])
    assert caption_ledger.caption_hash(caption) != caption_ledger.caption_hash(original)
    assert regen(_row("2026-10-08", 0, "instagram", caption="unknown source"), set()) is None


def test_lasso_copy_gate_preserves_url_cta_but_rejects_prose_punctuation(monkeypatch):
    assert not copy_gate.lasso_violations("Book at https://lasso-framework.com/start")
    assert "banned_colon" in copy_gate.lasso_violations("Next step: book your call")
    assert "banned_semicolon" in copy_gate.lasso_violations("Plan; then act")
    doc = SimpleNamespace(
        copy_bank={"source": {"hooks": ["Build a system your team can run."],
                              "bodies": ["Give every lead one clear next step."]}},
        ctas=["Book at https://lasso-framework.com/start"],
        pillars_with_copy=lambda: ["source"])
    monkeypatch.setattr("agent.content_planner.load_source_doc", lambda: doc)
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda *a, **k: False)
    regen = grade_fix._lasso_caption_regen(lambda *_: None)
    out = regen(_row("2026-10-08", 0, "instagram",
                     caption="Build a system your team can run."), set())
    assert out is not None
    assert out[0].endswith("https://lasso-framework.com/start")


def test_strict_ledger_fails_closed_and_stamps_both_keys():
    class KV:
        def __init__(self):
            self.values = {}
            self.fail = False
        def kv_get(self, key, default=""):
            if self.fail:
                raise OSError("kv unavailable")
            return self.values.get(key, default)
        def kv_set(self, key, value):
            if self.fail:
                raise OSError("kv unavailable")
            self.values[key] = value
    kv = KV()
    cap = "Sourced caption with a fresh angle."
    assert not caption_ledger.is_blocked_strict("lasso", cap, "2026-10-08", db=kv)
    caption_ledger.record_staged_strict("lasso", cap, "2026-10-08", db=kv)
    assert caption_ledger.is_blocked_strict("lasso", cap, "2026-10-09", db=kv)
    assert not caption_ledger.is_blocked_strict("lasso", cap, "2026-10-08", db=kv)
    kv.fail = True
    with pytest.raises(OSError):
        caption_ledger.is_blocked_strict("lasso", cap, "2026-10-10", db=kv)


def test_strict_ledger_refuses_disabled_history(monkeypatch):
    monkeypatch.setattr(config, "caption_cooldown_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="requires cooldown history"):
        caption_ledger.is_blocked_strict("lasso", "Fresh copy", "2026-10-08", db=object())
    with pytest.raises(RuntimeError, match="requires cooldown history"):
        caption_ledger.record_staged_strict("lasso", "Fresh copy", "2026-10-08", db=object())


def test_strict_ledger_retry_is_idempotent_but_distinct_same_day_copy_counts():
    class KV:
        def __init__(self):
            self.values = {}
        def kv_get(self, key, default=""):
            return self.values.get(key, default)
        def kv_set(self, key, value):
            self.values[key] = value
    import json
    kv = KV()
    first, second, day = "Fresh copy.", "Fresh copy!", "2026-10-08"
    assert caption_ledger.caption_hash(first) == caption_ledger.caption_hash(second)
    caption_ledger.record_staged_strict("lasso", first, day, db=kv)
    caption_ledger.record_staged_strict("lasso", first, day, db=kv)
    caption_ledger.record_staged_strict("lasso", second, day, db=kv)
    fuzzy = json.loads(kv.values[caption_ledger.ledger_key(
        "lasso", caption_ledger.caption_hash(first))])
    first_verbatim = json.loads(kv.values[caption_ledger.verbatim_key(
        "lasso", caption_ledger.verbatim_hash(first))])
    second_verbatim = json.loads(kv.values[caption_ledger.verbatim_key(
        "lasso", caption_ledger.verbatim_hash(second))])
    assert fuzzy["uses"] == 2
    assert first_verbatim == {"dates": [day], "uses": 1}
    assert second_verbatim == {"dates": [day], "uses": 1}


def test_exact_caption_patch_filters_original_generation():
    current = _row("2026-10-08", 0, "instagram", rid="123")
    calls = []
    class HTTP:
        def patch(self, url, *, params, headers, json, timeout):
            calls.append((params, json))
            return SimpleNamespace(status_code=200, json=lambda: [dict(current, **json)])
    store = SupabaseCalendarStore(url="https://example.test", service_key="test", http=HTTP())
    result = store.patch_pending_plan("lasso", "123", caption="Fresh source copy",
                                      expected_row=current)
    assert result["caption"] == "Fresh source copy"
    assert result["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    params, payload = calls[0]
    assert payload["caption"] == "Fresh source copy"
    assert payload["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    assert params["caption"] == "eq.Approved source copy"
    assert params["created_at"] == "eq.2026-10-01T00:00:00Z"
    assert params["image_url"] == "eq.https://cdn.test/0.jpg"
    assert params["status"] == "eq.pending"
    assert store.patch_pending_plan("lasso", "123", caption="Bad",
                                    expected_row=dict(current, status="published")) is None
    assert len(calls) == 1


def test_existing_story_is_held_in_the_exact_patch_without_changing_empty_copy():
    current = _row("2026-10-08", 0, "instagram", "story", caption="", rid="story")
    calls = []
    class HTTP:
        def patch(self, url, *, params, headers, json, timeout):
            calls.append((params, json))
            return SimpleNamespace(status_code=200, json=lambda: [dict(current, **json)])
    store = SupabaseCalendarStore(url="https://example.test", service_key="test", http=HTTP())
    after = store.patch_pending_plan("lasso", "story", expected_row=current,
                                     force_caption_visual_hold=True)
    assert after["caption"] == ""
    assert after["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    assert calls[0][1]["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    assert "caption" not in calls[0][1]


def test_lasso_date_repair_stamps_ledger_and_keeps_siblings_together(monkeypatch):
    day = "2026-10-08"
    rows = [_row(day, 0, "instagram"), _row(day, 0, "facebook"),
            _row(day, 0, "instagram", "story")]
    calls = []
    class Store:
        def active_rows_on_day_complete(self, gym, day):
            return [dict(r) for r in rows]
        def patch_pending_plan(self, gym, rid, *, caption=None, pillar=None,
                               expected_row, levers=None,
                               force_caption_visual_hold=False):
            calls.append((rid, dict(expected_row)))
            current = next(r for r in rows if r["id"] == rid)
            assert current["caption"] == expected_row["caption"]
            if caption is not None:
                current["caption"] = caption
            current["media_not_ready_reason"] = "caption_changed_needs_new_visual"
            return dict(current)
    new = "A source grounded caption with a different opening."
    assert grade_fix._patch_date_rows("lasso", rows, Store(), new, "doctrine",
                                      lambda *_: None)
    assert len(calls) == 3
    assert calls[0][0].endswith("-story")
    assert all(r["caption"] == new for r in rows)
    assert all(r["media_not_ready_reason"] == "caption_changed_needs_new_visual"
               for r in rows)
    assert caption_ledger.is_blocked_strict("lasso", new, "2026-10-09")


def test_lasso_date_repair_holds_when_ledger_unavailable(monkeypatch):
    row = _row("2026-10-08", 0, "instagram")
    class Store:
        def patch_pending_plan(self, *a, **k):
            raise AssertionError("no patch after ledger failure")
    def fail(*a, **k):
        raise OSError("unavailable")
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", fail)
    assert not grade_fix._patch_date_rows("lasso", [row], Store(),
                                          "Fresh caption", "doctrine", lambda *_: None)
    assert row["caption"] == "Approved source copy"


def test_lasso_partial_caption_patch_is_explicit_and_changed_rows_stay_held(monkeypatch):
    day = "2026-10-08"
    rows = [_row(day, 0, "instagram", rid="ig"),
            _row(day, 0, "facebook", rid="fb"),
            _row(day, 0, "instagram", "story", rid="story")]
    rows[-1]["caption"] = ""
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda *a, **k: False)
    stamped = []
    monkeypatch.setattr(caption_ledger, "record_staged_strict",
                        lambda *args: stamped.append(args))

    class Store:
        def active_rows_on_day_complete(self, gym, date):
            return [dict(r) for r in rows]
        def patch_pending_plan(self, gym, rid, *, caption=None, pillar=None,
                               expected_row, levers=None,
                               force_caption_visual_hold=False):
            if rid == "fb":
                return None  # a concurrent write beats the second feed CAS
            current = next(r for r in rows if r["id"] == rid)
            if caption is not None:
                current["caption"] = caption
            current["media_not_ready_reason"] = "caption_changed_needs_new_visual"
            return dict(current)

    with pytest.raises(grade_fix.PartialLassoCaptionRepair) as caught:
        grade_fix._patch_date_rows("lasso", rows, Store(),
                                   "Fresh source grounded copy", "doctrine",
                                   lambda *_: None)
    assert caught.value.applied_ids == ("story", "ig")
    assert stamped
    assert rows[0]["caption"] == "Fresh source grounded copy"
    assert rows[0]["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    assert rows[2]["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    assert rows[1]["caption"] == "Approved source copy"
    report = grade_sweep._merge_fix(
        {"ok": True, "actions": []},
        {"ok": False, "partial": True,
         "partial_row_ids": list(caught.value.applied_ids), "actions": []})
    assert report["partial"] is True
    assert report["partial_row_ids"] == ["story", "ig"]
    assert report["ok"] is False


def test_lasso_mechanical_caption_change_still_holds_existing_visual(monkeypatch):
    row = _row("2026-10-08", 0, "instagram", caption="Old CTA")
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda *a, **k: False)
    monkeypatch.setattr(caption_ledger, "record_staged_strict", lambda *a, **k: None)
    class Store:
        def active_rows_on_day_complete(self, gym, date):
            return [dict(row)]
        def patch_pending_plan(self, gym, rid, *, caption=None, pillar=None,
                               expected_row, levers=None,
                               force_caption_visual_hold=False):
            assert expected_row["caption"] == "Old CTA"
            assert caption == "Approved booking CTA"
            return dict(row, caption=caption,
                        media_not_ready_reason="caption_changed_needs_new_visual")
    assert grade_fix._patch_date_rows("lasso", [row], Store(),
                                      "Approved booking CTA", None, lambda *_: None)
    assert row["media_not_ready_reason"] == "caption_changed_needs_new_visual"
