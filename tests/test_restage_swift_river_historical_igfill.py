import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).parents[1] / "scripts" / "restage_swift_river_historical_igfill.py"
_SPEC = importlib.util.spec_from_file_location("historical_igfill", _PATH)
script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(script)


def _rows():
    rows = []
    for day in range(2, 18):
        date = f"2026-09-{day:02d}"
        rows.append({"id": f"{date}-ig", "gym_id": script._BASE,
                     "post_date": date, "account": "instagram", "format": "feed",
                     "status": "pending", "image_url": f"https://cdn/igfill_{date}.jpg",
                     "source_media_url": "https://cdn/igfill_raw.jpg",
                     "source_media_asset_id": None, "media_not_ready_reason": None})
    rows.extend([
        {"id": "2026-09-03-ig-story-source-only", "gym_id": script._BASE,
         "post_date": "2026-09-03", "account": "instagram", "format": "story",
         "status": "pending", "image_url": "https://cdn/rendered_story.jpg",
         "source_media_url": "https://cdn/igfill_sep03_story_source.jpg",
         "source_media_asset_id": None, "media_not_ready_reason": None},
        {"id": "2026-09-03-fb", "gym_id": script._BASE,
         "post_date": "2026-09-03", "account": "facebook", "format": "feed",
         "status": "pending", "image_url": "https://cdn/igfill_sep03_fb.jpg",
         "source_media_url": None, "source_media_asset_id": None,
         "media_not_ready_reason": None},
        {"id": "2026-09-04-story", "gym_id": script._BASE,
         "post_date": "2026-09-04", "account": "instagram", "format": "story",
         "status": "pending", "image_url": "https://cdn/igfill_sep04_story.jpg",
         "source_media_url": None, "source_media_asset_id": None,
         "media_not_ready_reason": None},
        {"id": "2026-09-05-gbp", "gym_id": script._BASE,
         "post_date": "2026-09-05", "account": "googlebusiness", "format": "update",
         "status": "pending", "image_url": "https://cdn/igfill_sep05_gbp.jpg",
         "source_media_url": None, "source_media_asset_id": None,
         "media_not_ready_reason": None},
        {"id": "2026-09-06-fb", "gym_id": script._BASE,
         "post_date": "2026-09-06", "account": "facebook", "format": "feed",
         "status": "pending", "image_url": "https://cdn/igfill_sep06_fb.jpg",
         "source_media_url": None, "source_media_asset_id": None,
         "media_not_ready_reason": None},
        {"id": "2026-09-02-fb", "gym_id": script._BASE,
         "post_date": "2026-09-02", "account": "facebook", "format": "feed",
         "status": "pending", "image_url": "https://cdn/igfill_fb.jpg",
         "source_media_url": None, "source_media_asset_id": None,
         "media_not_ready_reason": "held"},
        {"id": "2026-10-02-gbp", "gym_id": script._BASE,
         "post_date": "2026-10-02", "account": "googlebusiness", "format": "update",
         "status": "pending", "image_url": "https://cdn/igfill_gbp.jpg",
         "source_media_url": None, "source_media_asset_id": None,
         "media_not_ready_reason": None},
        {"id": "2026-10-02-story", "gym_id": script._BASE,
         "post_date": "2026-10-02", "account": "instagram", "format": "story",
         "status": "pending", "image_url": "https://cdn/igfill_story.jpg",
         "source_media_url": None, "source_media_asset_id": None,
         "media_not_ready_reason": None},
        {"id": "2026-10-02-fb", "gym_id": script._BASE,
         "post_date": "2026-10-02", "account": "facebook", "format": "feed",
         "status": "pending", "image_url": "https://cdn/igfill_fb.jpg",
         "source_media_url": None, "source_media_asset_id": None,
         "media_not_ready_reason": None},
    ])
    return rows


class Calendar:
    def __init__(self, rows, fail_at=None):
        self.rows = {row["id"]: dict(row) for row in rows}
        self.fail_at = fail_at
        self.swaps = []
        self.held_stages = []
        self.writes = 0

    def list_month(self, _gym, month):
        return [dict(row) for row in self.rows.values()
                if str(row["post_date"]).startswith(month)]

    def get_row(self, _gym, row_id):
        return dict(self.rows[row_id])

    def swap_media(self, gym, row_id, image_url, *, source_media_url=None, extra_fields=None):
        self.writes += 1
        if self.writes == self.fail_at:
            return None
        row = self.rows[row_id]
        if row["gym_id"] != gym or row["status"] not in ("pending", "coach_review"):
            return None
        row.update(image_url=image_url, source_media_url=source_media_url,
                   **extra_fields)
        self.swaps.append(row_id)
        return dict(row)

    def restage_held_media(self, gym, current, *, image_url=None, source_media_url=None,
                           extra_fields=None, release=False):
        assert release is False
        row = self.rows[current["id"]]
        if row != current or gym != script._BASE:
            return None
        self.writes += 1
        if self.writes == self.fail_at:
            return None
        row.update(image_url=image_url, source_media_url=source_media_url,
                   **extra_fields)
        self.held_stages.append(current["id"])
        return dict(row)


class Media:
    def __init__(self, assets, sources=None):
        self.assets = {asset["id"]: dict(asset) for asset in assets}
        self.sources = sources if sources is not None else [{
            "id": "source-ready", "gym_id": script._BASE,
            "kind": "gym_drive", "active": True,
            "revoked_externally": False, "sync_status": "ready"}]

    def available(self):
        return True

    def get_asset(self, asset_id):
        return dict(self.assets[asset_id])

    def list_sources(self, gym, include_inactive=False):
        assert gym == script._BASE and include_inactive is True
        return list(self.sources)


def _assets(count=20):
    return [{"id": f"photo-{index}", "gym_id": script._BASE,
             "source_id": "source-ready",
             "kind": "photo", "eligible": True, "excluded_by_coach": False,
             "review_status": "approved", "reviewed_by": "coach",
             "reviewed_at": "2026-08-01T00:00:00+00:00", "content_hash": f"hash-{index}",
             "review_content_hash": f"hash-{index}", "moderation_status": "clean",
             "moderation_json": {"verdict": "clean", "provider": "scanner",
                                  "content_hash": f"hash-{index}", "asset_id": f"photo-{index}",
                                  "gym_id": script._BASE, "people_detected": None,
                                  "observed_at": "2026-08-01T00:00:00+00:00"},
             "people_detected": None, "used_count": 0, "last_used_at": None}
            for index in range(count)]


def _ctx(rows, assets=None, fail_at=None):
    return {"account": object(), "calendar": Calendar(rows, fail_at),
            "media": Media(assets or _assets())}


def _wire_selector(monkeypatch, ctx):
    from agent import gym_media_selector
    monkeypatch.setattr(gym_media_selector, "pickable",
                        lambda *_a, **_kw: list(ctx["media"].assets.values()))
    monkeypatch.setattr(gym_media_selector, "is_usable", lambda asset: asset.get("eligible") is True)
    stamps = []
    monkeypatch.setattr(gym_media_selector, "stamp_use",
                        lambda asset, gym, day, **_kw: stamps.append((asset["id"], gym, day)))
    return stamps


def test_exact_scope_digest_and_dry_run_has_no_writes(monkeypatch):
    ctx = _ctx(_rows())
    _wire_selector(monkeypatch, ctx)
    out = script.run(ctx=ctx, ledger_rows=[])
    assert out["ok"] and out["dry_run"] and out["apply_available"] is False
    assert out["target_rows"] == 25 and out["target_days"] == 17
    assert len(out["target_digest"]) == 64
    assert sum(len(item["row_ids"]) for item in out["plan"]) == 25
    assert len({item["asset_id"] for item in out["plan"]}) == 17
    assert ctx["calendar"].writes == 0


def test_scope_rejects_wrong_hold_count_and_row_count():
    rows = _rows()
    rows[0]["media_not_ready_reason"] = "extra hold"
    assert script._target_rows(rows) is None
    assert script._target_rows(_rows()[:-1]) is None


def test_candidate_filter_excludes_book_and_active_usage_ledger(monkeypatch):
    from agent import gym_media_selector
    assets = _assets(19)
    monkeypatch.setattr(gym_media_selector, "pickable", lambda *_a, **_kw: assets)
    monkeypatch.setattr(gym_media_selector, "is_usable", lambda _asset: True)
    available = script._fresh_photos(Media(assets), {"photo-0"}, {"photo-1"},
                                     {"source-ready"})
    assert "photo-0" not in {item["id"] for item in available}
    assert "photo-1" not in {item["id"] for item in available}


def test_ledger_parser_fails_closed_and_excludes_even_rolled_back_history():
    key = f"gym_media_use:{script._BASE}:2026-09-01"
    rolled = {"gym_id": script._BASE, "asset_id": "returned", "rolled_back": True}
    consumed = {"gym_id": script._BASE, "asset_id": "used", "rolled_back": False}
    assert script._consumed_asset_ids(ledger_rows=[(key, json.dumps([rolled, consumed]))]) == {"used", "returned"}
    with __import__("pytest").raises(ValueError):
        script._consumed_asset_ids(ledger_rows=[(key, "not-json")])


def test_apply_is_blocked_before_store_resolution_or_any_write(monkeypatch):
    ctx = _ctx(_rows())
    monkeypatch.setattr(script, "_resolve", lambda: (_ for _ in ()).throw(
        AssertionError("apply resolved credentials")))
    out = script.run(ctx=ctx, apply=True, expected_digest="valid-looking-digest")
    assert not out["ok"] and out["writes_attempted"] == 0
    assert "atomic per-day" in out["reason"]
    assert ctx["calendar"].writes == 0
    assert script.run(apply=True)["writes_attempted"] == 0


def test_digest_changes_with_human_edit(monkeypatch):
    ctx = _ctx(_rows())
    _wire_selector(monkeypatch, ctx)
    dry = script.run(ctx=ctx, ledger_rows=[])
    ctx["calendar"].rows["2026-09-02-ig"]["caption"] = "human edit"
    out = script.run(ctx=ctx, ledger_rows=[])
    assert out["ok"] and out["target_digest"] != dry["target_digest"]
    assert ctx["calendar"].writes == 0


def test_source_provenance_excludes_unready_revoked_inactive_and_foreign(monkeypatch):
    ctx = _ctx(_rows())
    _wire_selector(monkeypatch, ctx)
    source = ctx["media"].sources[0]
    for change in ({"active": False}, {"revoked_externally": True},
                   {"sync_status": "queued"}, {"gym_id": "foreign"},
                   {"kind": "other"}):
        source.update(change)
        out = script.run(ctx=ctx, ledger_rows=[])
        assert not out["ok"] and out["available_photos"] == 0
        source.clear()
        source.update({"id": "source-ready", "gym_id": script._BASE,
                       "kind": "gym_drive", "active": True,
                       "revoked_externally": False, "sync_status": "ready"})
    ctx["media"].assets["photo-0"]["source_id"] = "missing"
    out = script.run(ctx=ctx, ledger_rows=[])
    assert out["ok"] and "photo-0" not in {item["asset_id"] for item in out["plan"]}


def test_duplicate_source_identity_fails_closed(monkeypatch):
    ctx = _ctx(_rows())
    _wire_selector(monkeypatch, ctx)
    ctx["media"].sources.append(dict(ctx["media"].sources[0]))
    out = script.run(ctx=ctx, ledger_rows=[])
    assert not out["ok"] and out["reason"].startswith("photo eligibility read failed")
