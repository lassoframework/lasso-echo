"""Fail-closed one-ticket caller checks; no external writes."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from agent import eng_sept5_recreation as repair


def _rows(target):
    rows = []
    for account, fmt in (("instagram", "feed"), ("facebook", "feed"),
                         ("instagram", "story")):
        rows.append({
            "gym_id": repair.TENANT, "account": account, "format": fmt,
            "post_date": target.isoformat(), "logical_post_id": repair.LOGICAL_POST_ID,
            "status": "pending", "media_not_ready_reason": None,
            "source_media_asset_id": "different-asset", "source_media_url": "https://example.test/raw",
            "image_url": "https://example.test/raw", "caption": "New copy" if fmt == "feed" else "",
            repair.pcs.RESERVATION_PROOF: {
                "source_sha256": "a" * 64, "phash_v1": 1,
                "source_media_asset_id": "different-asset"},
            repair.observation_bridge.METADATA: [{"provenance_status": "unverified"}],
        })
    return rows


def test_exact_three_siblings_and_future_date():
    target = date.today() + timedelta(days=2)
    assert len(repair._checked_candidates(_rows(target), target)) == 3
    assert repair._future_target(target) == target
    with pytest.raises(repair.EngRecreationRefused):
        repair._future_target(date.today())


@pytest.mark.parametrize("change", [
    lambda rows: rows[0].update(gym_id="other"),
    lambda rows: rows[0].update(source_media_asset_id=repair.ORIGINAL_ASSET),
    lambda rows: rows[0].update(source_media_url="http://example.test/raw"),
    lambda rows: rows[0].update(source_media_url="https://example.test/other"),
    lambda rows: rows[1].update(caption="Different copy"),
    lambda rows: rows[2].pop(repair.observation_bridge.METADATA),
    lambda rows: rows[0].update(approved_at="2026-10-10"),
])
def test_changed_tenant_media_siblings_or_approval_refused(change):
    target = date.today() + timedelta(days=2)
    rows = _rows(target)
    change(rows)
    with pytest.raises(repair.EngRecreationRefused):
        repair._checked_candidates(rows, target)


def test_ambiguous_stack_flag_refuses(monkeypatch):
    monkeypatch.setattr(repair.pcs, "forward_reservation_flag", lambda: None)
    with pytest.raises(repair.EngRecreationRefused):
        repair._required_stack()
