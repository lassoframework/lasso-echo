"""Offline evidence for the dry-run caption audit and guarded feed correction."""
import json
import os

import pytest

from scripts import audit_gym_caption_format as audit


def _row(row_id, *, gym="crossfitlocal", status="pending", fmt="feed",
         hold=None, caption="Start here. Meet your coach; book a class."):
    return {"id": row_id, "gym_id": gym, "post_date": "2026-10-12",
            "status": status, "variant_status": "active", "format": fmt,
            "account": "instagram", "caption": caption,
            "media_not_ready_reason": hold, "published_at": None,
            "image_url": "https://cdn.example/photo.jpg"}


class _Store:
    def __init__(self, rows):
        self.rows = {row["id"]: dict(row) for row in rows}
        self.writes = []
        self.reads = []

    def list_future_media_maintenance_rows(self, start, end):
        self.reads.append((start, end))
        return list(self.rows.values())

    def format_pending_feed_caption_cas(self, gym_id, current):
        live = self.rows[current["id"]]
        if (live["gym_id"] != gym_id or live["status"] != "pending"
                or live["caption"] != current["caption"]):
            return None
        live["caption"] = audit.format_caption(live["caption"])
        self.writes.append(current["id"])
        return dict(live)

    def get_row(self, gym_id, row_id):
        row = self.rows[row_id]
        return dict(row) if row["gym_id"] == gym_id else None


def test_dry_run_reports_fleet_without_caption_text_or_writes():
    store = _Store([_row("a"), _row("b", status="approved"),
                    _row("c", fmt="story"), _row("d", hold="needs media"),
                    _row("e", gym="other")])
    result = audit.run(start="2026-10-05", end="2026-12-05", store=store)
    assert result["ok"] and result["dry_run"]
    assert result["audit"]["counts"]["rows_seen"] == 5
    assert result["audit"]["counts"]["eligible_pending_feed"] == 2
    assert store.writes == []
    assert store.reads == [("2026-10-05", "2026-12-05")]
    assert "Start here" not in json.dumps(result)


def test_apply_needs_digest_and_private_receipt_then_reads_back(tmp_path):
    store = _Store([_row("a"), _row("b", status="approved"),
                    _row("c", fmt="story")])
    dry = audit.run(start="2026-10-05", end="2026-12-05", store=store)
    path = tmp_path / "caption-receipt.json"
    bad = audit.run(start="2026-10-05", end="2026-12-05", store=store,
                    apply=True, expected_digest="wrong", receipt_path=str(path),
                    today="2026-10-05")
    assert not bad["ok"] and store.writes == [] and not path.exists()
    result = audit.run(start="2026-10-05", end="2026-12-05", store=store,
                       apply=True, expected_digest=dry["audit"]["target_digest"],
                       receipt_path=str(path), today="2026-10-05")
    assert result["ok"] and result["changed"] == 1
    assert store.writes == ["a"]
    assert store.rows["a"]["caption"] == "Start here.\n\nMeet your coach, book a class."
    assert store.rows["b"]["caption"] == _row("b")["caption"]
    assert store.rows["c"]["caption"] == _row("c")["caption"]
    receipt = path.read_text()
    assert "Start here" not in receipt
    assert "before_caption_sha256" in receipt and "after_row_sha256" in receipt
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_stale_caption_conflicts_without_rewriting(tmp_path):
    store = _Store([_row("a")])
    dry = audit.run(start="2026-10-05", end="2026-12-05", store=store)
    def stale(_gym, _current):
        return None
    store.format_pending_feed_caption_cas = stale
    result = audit.run(start="2026-10-05", end="2026-12-05", store=store,
                       apply=True, expected_digest=dry["audit"]["target_digest"],
                       receipt_path=str(tmp_path / "receipt.json"),
                       today="2026-10-05")
    assert not result["ok"] and result["conflicts"] == ["a"]
    assert store.writes == []


def test_malformed_partial_read_refused_before_audit_or_write():
    with pytest.raises(ValueError, match="out-of-scope"):
        audit.inspect([_row("a"), _row("a")], start="2026-10-05", end="2026-12-05")
    with pytest.raises(ValueError, match="incomplete"):
        audit.inspect([{"id": "a"}], start="2026-10-05", end="2026-12-05")
