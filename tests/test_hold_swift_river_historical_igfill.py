import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).parents[1] / "scripts" / "hold_swift_river_historical_igfill.py"
_SPEC = importlib.util.spec_from_file_location("swift_igfill_hold", _PATH)
script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(script)


def _rows():
    rows = []
    for day in range(2, 18):
        stamp = f"2026-09-{day:02d}"
        rows.append({"id": f"{stamp}-ig", "gym_id": script._BASE, "post_date": stamp,
                     "status": "pending", "account": "instagram", "format": "feed",
                     "image_url": f"https://cdn.test/igfill_{stamp}.jpg",
                     "source_media_url": None, "source_media_asset_id": None,
                     "media_not_ready_reason": None, "variant_status": "active"})
    for number in range(9):
        stamp = "2026-09-03" if number < 8 else "2026-10-02"
        rows.append({"id": f"extra-{number}", "gym_id": script._BASE, "post_date": stamp,
                     "status": "pending", "account": "instagram", "format": "story",
                     "image_url": "https://cdn.test/story.jpg",
                     "source_media_url": f"https://cdn.test/igfill_extra_{number}.jpg",
                     "source_media_asset_id": None, "media_not_ready_reason": None,
                     "variant_status": "active"})
    return rows


class Calendar:
    def __init__(self, rows):
        self.rows = {row["id"]: dict(row) for row in rows}
        self.writes = []
    def list_pending_media_between(self, gym, first, last):
        return [dict(row) for row in self.rows.values()
                if row["gym_id"] == gym and row["status"] == "pending"
                and first <= row["post_date"] <= last]
    def get_row(self, _gym, row_id): return dict(self.rows[row_id])
    def hold_pending_media(self, gym, current, reason):
        row = self.rows[current["id"]]
        if gym != script._BASE or row != current: return None
        row["media_not_ready_reason"] = reason
        self.writes.append(current["id"])
        return dict(row)


def _ctx(rows): return {"account": object(), "calendar": Calendar(rows)}


def test_dry_run_is_exact_and_does_not_write():
    ctx = _ctx(_rows())
    out = script.run(ctx=ctx)
    assert out["ok"] and out["dry_run"]
    assert out["preflight"]["target_rows"] == 25
    assert out["preflight"]["target_dates"] == 17
    assert not ctx["calendar"].writes


def test_apply_requires_digest_and_receipt_before_writing(tmp_path):
    ctx = _ctx(_rows())
    dry = script.run(ctx=ctx)
    assert not script.run(ctx=ctx, apply=True, expected_digest=dry["preflight"]["target_digest"])["ok"]
    assert not ctx["calendar"].writes
    receipt = tmp_path / "hold.json"
    out = script.run(ctx=ctx, apply=True, expected_digest=dry["preflight"]["target_digest"], receipt_path=receipt)
    assert out["ok"] and len(out["changed_ids"]) == 25
    saved = receipt.read_text()
    assert '"before_image"' in saved and '"readback_verified"' in saved
    assert all(row["status"] == "pending" for row in ctx["calendar"].rows.values())


def test_scope_change_rejects_without_writes():
    ctx = _ctx(_rows()[:-1])
    out = script.run(ctx=ctx)
    assert not out["ok"] and "exact 25-row" in out["reason"]
    assert not ctx["calendar"].writes


def test_partial_conflict_preserves_changed_ids_in_receipt(tmp_path):
    ctx = _ctx(_rows())
    digest = script.run(ctx=ctx)["preflight"]["target_digest"]
    calendar = ctx["calendar"]
    original = calendar.hold_pending_media
    def fail_second(gym, current, reason):
        if calendar.writes:
            return None
        return original(gym, current, reason)
    calendar.hold_pending_media = fail_second
    receipt = tmp_path / "partial.json"
    out = script.run(ctx=ctx, apply=True, expected_digest=digest, receipt_path=receipt)
    assert not out["ok"] and len(calendar.writes) == 1
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "write_intent"
    assert saved["changed_ids"] == calendar.writes
    assert saved["inflight_id"] in calendar.rows
    assert saved["inflight_id"] not in calendar.writes


def test_nonactive_pending_igfill_breaks_exact_scope():
    rows = _rows() + [{**_rows()[0], "id": "archived-extra", "variant_status": "archived"}]
    ctx = _ctx(rows)
    out = script.run(ctx=ctx)
    assert not out["ok"] and not ctx["calendar"].writes


def test_receipt_failure_after_cas_leaves_durable_write_intent(tmp_path, monkeypatch):
    ctx = _ctx(_rows())
    digest = script.run(ctx=ctx)["preflight"]["target_digest"]
    receipt = tmp_path / "interrupted.json"
    original = script._write_receipt
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("disk full")
        return original(*args, **kwargs)
    monkeypatch.setattr(script, "_write_receipt", interrupted)
    out = script.run(ctx=ctx, apply=True, expected_digest=digest, receipt_path=receipt)
    assert not out["ok"] and len(ctx["calendar"].writes) == 1
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "write_intent"
    assert saved["inflight_id"] == ctx["calendar"].writes[0]
