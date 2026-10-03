import json
import stat

from agent import infographic_photo_maintenance as job


def rows():
    def row(id_, account, format_, status="pending"):
        return {"id": id_, "gym_id": "gym", "post_date": "2026-10-15",
                "account": account, "format": format_, "status": status,
                "variant_status": "active", "caption": "keep " + id_,
                "image_url": "https://r2/igfill_2026-10-15_card.png",
                "source_media_url": None, "source_media_asset_id": None}
    return [row("ig", "instagram", "feed"),
            row("fb", "facebook", "feed"),
            row("story", "instagram", "story")]


def test_inventory_never_picks_or_writes_and_is_private(tmp_path):
    path = tmp_path / "inventory.json"
    result = job.run(rows(), receipt_path=path)
    assert result[0]["status"] == "requires_hold_until_photo_verified"
    assert set(result[0]["pending_hold_ids"]) == {"ig", "fb", "story"}
    assert json.loads(path.read_text()) == result
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_approved_sibling_is_separately_identified():
    data = rows()
    data[1]["status"] = "approved"
    result = job.run(data)
    assert result[0]["status"] == "approved_sibling_requires_scoped_hold"
    assert result[0]["locked_ids"] == ["fb"]
    assert set(result[0]["pending_hold_ids"]) == {"ig", "story"}


def test_future_inventory_starts_tomorrow():
    class Read:
        def list_future_media_maintenance_rows(self, start, end):
            assert start == "2026-10-04" and end == "2027-10-03"
            return []
    assert job.load_future(Read(), today="2026-10-03") == []
