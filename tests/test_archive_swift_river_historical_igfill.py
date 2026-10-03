import importlib.util
import json
import stat
from pathlib import Path


_PATH = Path(__file__).parents[1] / "scripts" / "archive_swift_river_historical_igfill.py"
_SPEC = importlib.util.spec_from_file_location("swift_igfill_archive", _PATH)
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
                     "media_not_ready_reason": script._HOLD_REASON,
                     "variant_status": "active", "created_at": f"{stamp}T00:00:00+00:00"})
    for number in range(9):
        stamp = "2026-09-03" if number < 8 else "2026-10-02"
        rows.append({"id": f"extra-{number}", "gym_id": script._BASE, "post_date": stamp,
                     "status": "pending", "account": "instagram", "format": "story",
                     "image_url": "https://cdn.test/story.jpg",
                     "source_media_url": f"https://cdn.test/igfill_extra_{number}.jpg",
                     "source_media_asset_id": None, "media_not_ready_reason": script._HOLD_REASON,
                     "variant_status": "active", "created_at": f"{stamp}T00:00:00+00:00"})
    return rows


class Calendar:
    def __init__(self, rows, fail_at=None, corrupt_readback=False):
        self.rows = {row["id"]: dict(row) for row in rows}
        self.fail_at, self.corrupt_readback, self.writes = fail_at, corrupt_readback, []

    def list_pending_media_between(self, gym, first, last):
        return [dict(row) for row in self.rows.values() if row["gym_id"] == gym
                and row["status"] == "pending" and first <= row["post_date"] <= last]

    def archive_pending_media(self, gym, current):
        row = self.rows[current["id"]]
        if (gym != script._BASE or row != current or row["variant_status"] != "active"
                or (self.fail_at and len(self.writes) + 1 == self.fail_at)):
            return None
        row["variant_status"] = "archived"
        self.writes.append(current["id"])
        return dict(row)

    def get_row(self, _gym, row_id):
        row = dict(self.rows[row_id])
        if self.corrupt_readback and row_id == self.writes[-1]:
            row["source_media_url"] = "https://unexpected.example/change.jpg"
        return row


def _ctx(rows, **kwargs):
    return {"account": object(), "calendar": Calendar(rows, **kwargs)}


def test_dry_run_is_exact_and_does_not_write():
    ctx = _ctx(_rows())
    out = script.run(ctx=ctx)
    assert out["ok"] and out["dry_run"]
    assert out["preflight"]["target_rows"] == 25
    assert out["preflight"]["target_dates"] == 17
    assert not ctx["calendar"].writes


def test_apply_archives_only_variants_and_writes_private_fsynced_receipt(tmp_path):
    ctx = _ctx(_rows())
    dry = script.run(ctx=ctx)
    receipt = tmp_path / "archive.json"
    out = script.run(ctx=ctx, apply=True, expected_digest=dry["preflight"]["target_digest"],
                     receipt_path=receipt)
    assert out["ok"] and len(out["changed_ids"]) == 25
    assert {row["variant_status"] for row in ctx["calendar"].rows.values()} == {"archived"}
    assert all(row["status"] == "pending" for row in ctx["calendar"].rows.values())
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "readback_verified" and len(saved["before_image"]) == 25
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_digest_drift_rejects_before_any_write(tmp_path):
    ctx = _ctx(_rows())
    digest = script.run(ctx=ctx)["preflight"]["target_digest"]
    ctx["calendar"].rows["2026-09-02-ig"]["source_media_url"] = "https://cdn.test/changed.jpg"
    out = script.run(ctx=ctx, apply=True, expected_digest=digest, receipt_path=tmp_path / "nope.json")
    assert not out["ok"] and out["reason"] == "target digest missing or changed"
    assert not ctx["calendar"].writes


def test_digest_covers_publication_schedule_and_slot_fields(tmp_path):
    ctx = _ctx(_rows())
    dry = script.run(ctx=ctx)
    row = ctx["calendar"].rows["2026-09-02-ig"]
    row.update(published_at=None, late_post_id=None,
               scheduled_at="2026-09-02T12:00:00+00:00", slot_index=1)
    out = script.run(ctx=ctx, apply=True, expected_digest=dry["preflight"]["target_digest"],
                     receipt_path=tmp_path / "schedule-drift.json")
    assert not out["ok"] and out["reason"] == "target digest missing or changed"
    assert not ctx["calendar"].writes


def test_pending_target_with_publication_marker_is_rejected(tmp_path):
    for field in ("published_at", "late_post_id"):
        rows = _rows()
        rows[0][field] = "marker"
        ctx = _ctx(rows)
        out = script.run(ctx=ctx)
        assert not out["ok"] and "exact 25-row" in out["reason"]
        assert not ctx["calendar"].writes


def test_partial_cas_conflict_preserves_write_ahead_receipt(tmp_path):
    ctx = _ctx(_rows(), fail_at=2)
    digest = script.run(ctx=ctx)["preflight"]["target_digest"]
    receipt = tmp_path / "partial.json"
    out = script.run(ctx=ctx, apply=True, expected_digest=digest, receipt_path=receipt)
    assert not out["ok"] and len(ctx["calendar"].writes) == 1
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "write_intent"
    assert saved["changed_ids"] == ctx["calendar"].writes
    assert saved["inflight_id"] not in saved["changed_ids"]


def test_readback_mismatch_is_reported_and_not_hidden(tmp_path):
    ctx = _ctx(_rows(), corrupt_readback=True)
    digest = script.run(ctx=ctx)["preflight"]["target_digest"]
    out = script.run(ctx=ctx, apply=True, expected_digest=digest,
                     receipt_path=tmp_path / "mismatch.json")
    assert not out["ok"] and "readback mismatch" in out["reason"]
    assert len(ctx["calendar"].writes) == 25


def test_apply_does_not_touch_a_same_day_sibling_or_other_tenant(tmp_path):
    rows = _rows()
    rows.extend([
        {**rows[0], "id": "same-group-sibling", "image_url": "https://cdn.test/real_photo.jpg",
         "source_media_url": None, "variant_of": rows[0]["id"]},
        {**rows[0], "id": "foreign-igfill", "gym_id": "other-tenant"},
    ])
    ctx = _ctx(rows)
    digest = script.run(ctx=ctx)["preflight"]["target_digest"]
    out = script.run(ctx=ctx, apply=True, expected_digest=digest,
                     receipt_path=tmp_path / "scoped.json")
    assert out["ok"] and len(out["changed_ids"]) == 25
    assert ctx["calendar"].rows["same-group-sibling"]["variant_status"] == "active"
    assert ctx["calendar"].rows["foreign-igfill"]["variant_status"] == "active"


def test_approval_rpc_migration_explicitly_requires_active_variant():
    sql = (Path(__file__).parents[1] / "migrations" /
           "DRAFT_archive_pending_media_variant_guard_20261003.sql").read_text()
    assert "variant_status = 'active'" in sql
