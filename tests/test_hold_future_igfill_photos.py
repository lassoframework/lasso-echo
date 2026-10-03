import json
import stat

from agent.portal_calendar_store import SupabaseCalendarStore
from scripts import hold_future_igfill_photos as hold


def row(id_, status, *, day="2026-10-03", image=None):
    return {"id": id_, "gym_id": "gym", "post_date": day,
            "status": status, "variant_status": "active",
            "account": "instagram", "format": "feed",
            "caption": "original caption", "image_url": image or
            f"https://r2/igfill_{day}_card.png",
            "source_media_url": None, "source_media_asset_id": None,
            "media_not_ready_reason": None, "created_at": "2026-10-01T00:00:00Z",
            "published_at": None, "late_post_id": None}


class Store:
    def __init__(self, rows, receipt=None):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.receipt = receipt
        self.calls = []

    def list_future_media_maintenance_rows(self, start, end):
        assert start == "2026-10-03" and end == "2027-10-03"
        return list(self.rows.values())

    def hold_future_infographic_media(self, gym, before, reason):
        progress = json.loads(self.receipt.read_text())
        assert progress["state"] == "write_intent"
        assert progress["inflight_id"] == before["id"]
        assert progress["before_image"]
        self.calls.append(before["id"])
        self.rows[before["id"]]["media_not_ready_reason"] = reason
        return dict(self.rows[before["id"]])

    def get_row(self, gym, id_):
        return dict(self.rows[id_])


def test_targets_include_approved_and_pending_but_preserve_live():
    data = [row("pending", "pending"), row("approved", "approved"),
            row("published", "published"), row("old", "pending", day="2026-10-02"),
            row("photo", "pending", image="https://r2/real.jpg")]
    found = hold.targets(data, today="2026-10-03")
    assert {r["id"] for r in found} == {"pending", "approved"}


def test_apply_requires_flag_digest_and_private_writeahead_receipt(tmp_path, monkeypatch):
    data = [row("pending", "pending"), row("approved", "approved")]
    path = tmp_path / "receipt.json"
    store = Store(data, path)
    dry = hold.run(store=store, today="2026-10-03")
    assert dry["preflight"]["pending_count"] == 1
    assert dry["preflight"]["approved_count"] == 1
    assert store.calls == []
    assert not hold.run(store=store, today="2026-10-03", apply=True,
                        expected_digest=dry["preflight"]["target_digest"],
                        receipt_path=path)["ok"]
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_HOLD_ENABLED", "true")
    result = hold.run(store=store, today="2026-10-03", apply=True,
                      expected_digest=dry["preflight"]["target_digest"],
                      receipt_path=path)
    assert result["ok"] is True
    assert set(store.calls) == {"pending", "approved"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["state"] == "readback_verified"
    assert {r["status"] for r in saved["readback"]} == {"pending", "approved"}
    assert all(r["caption"] == "original caption" for r in saved["readback"])
    assert all(r["image_url"].endswith("card.png") for r in saved["readback"])


def test_store_cas_filters_protected_fields_and_mutates_only_hold():
    class Response:
        status_code = 200
        def __init__(self, value):
            self.value = value
        def json(self):
            return [self.value]
    class Http:
        def __init__(self, before):
            self.before = before
            self.calls = []
        def patch(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response({**self.before, "media_not_ready_reason": "hold"})
    before = row("approved", "approved")
    http = Http(before)
    store = SupabaseCalendarStore(url="https://test.supabase.co",
                                  service_key="fake", http=http)
    result = store.hold_future_infographic_media("gym", before, "hold")
    assert result["status"] == "approved"
    url, call = http.calls[0]
    assert url.endswith("/content_calendar")
    assert call["json"] == {"media_not_ready_reason": "hold"}
    assert call["params"]["status"] == "eq.approved"
    assert call["params"]["caption"] == "eq.original caption"
    assert call["params"]["image_url"].endswith("card.png")
