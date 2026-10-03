import json
import stat

from agent.portal_calendar_store import PortalStoreError, SupabaseCalendarStore
from scripts import hold_future_igfill_photos as hold
from scripts import release_future_igfill_photos as release


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


def test_complete_future_book_pages_past_500_and_fails_closed_on_error_or_stall():
    class Response:
        def __init__(self, status, data):
            self.status_code, self.data = status, data
        def json(self):
            return self.data
    class Http:
        def __init__(self, second):
            self.second, self.calls = second, []
        def get(self, url, **kwargs):
            self.calls.append(kwargs["params"])
            if len(self.calls) == 1:
                return Response(200, [{"id": f"{i:04d}"} for i in range(500)])
            return self.second
    second = Response(200, [{"id": "0500"}, {"id": "0501"}])
    http = Http(second)
    store = SupabaseCalendarStore(url="https://test.supabase.co",
                                  service_key="fake", http=http)
    assert len(store.list_future_media_maintenance_rows("2026-10-03", "2027-10-03")) == 502
    assert http.calls[1]["id"] == "gt.0499"
    for bad in (Response(503, []), Response(200, [{"id": "0499"}]),
                Response(200, [{"id": "0501"}, {"id": "0500"}])):
        store = SupabaseCalendarStore(url="https://test.supabase.co",
                                      service_key="fake", http=Http(bad))
        try:
            store.list_future_media_maintenance_rows("2026-10-03", "2027-10-03")
        except PortalStoreError:
            pass
        else:
            raise AssertionError("incomplete or stalled page accepted")


def test_partial_hold_cas_conflict_is_recorded_without_approval_change(tmp_path, monkeypatch):
    class ConflictStore(Store):
        def hold_future_infographic_media(self, gym, before, reason):
            if before["id"] == "approved":
                self.calls.append("approved-conflict")
                return None
            return super().hold_future_infographic_media(gym, before, reason)
    path = tmp_path / "hold.json"
    store = ConflictStore([row("pending", "pending"), row("approved", "approved")], path)
    digest = hold.run(store=store, today="2026-10-03")["preflight"]["target_digest"]
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_HOLD_ENABLED", "true")
    result = hold.run(store=store, today="2026-10-03", apply=True,
                      expected_digest=digest, receipt_path=path)
    saved = json.loads(path.read_text())
    assert result["ok"] is False
    assert saved["state"] == "partial_hold_conflicts"
    assert saved["changed_ids"] == ["pending"]
    assert saved["conflict_ids"] == ["approved"]
    assert store.rows["approved"]["status"] == "approved"
    assert store.rows["approved"]["media_not_ready_reason"] is None


def test_release_requires_original_private_receipt_and_preserves_approved(tmp_path, monkeypatch):
    class ReleaseStore(Store):
        def release_future_infographic_media(self, gym, before, reason):
            assert before["media_not_ready_reason"] == reason
            assert json.loads((tmp_path / "release.json").read_text())["inflight_id"] == before["id"]
            self.rows[before["id"]]["media_not_ready_reason"] = None
            return dict(self.rows[before["id"]])
    hold_path, release_path = tmp_path / "hold.json", tmp_path / "release.json"
    store = ReleaseStore([row("approved", "approved")], hold_path)
    digest = hold.run(store=store, today="2026-10-03")["preflight"]["target_digest"]
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_HOLD_ENABLED", "true")
    assert hold.run(store=store, today="2026-10-03", apply=True,
                    expected_digest=digest, receipt_path=hold_path)["ok"]
    dry = release.run(store=store, hold_receipt_path=hold_path)
    assert dry["ok"] and dry["preflight"]["target_count"] == 1
    assert not release.run(store=store, hold_receipt_path=hold_path, apply=True,
                           expected_digest=dry["preflight"]["target_digest"],
                           receipt_path=release_path)["ok"]
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RELEASE_ENABLED", "true")
    applied = release.run(store=store, hold_receipt_path=hold_path, apply=True,
                          expected_digest=dry["preflight"]["target_digest"],
                          receipt_path=release_path)
    assert applied["ok"]
    assert store.rows["approved"] == row("approved", "approved")
    assert json.loads(release_path.read_text())["state"] == "readback_verified"
    assert stat.S_IMODE(release_path.stat().st_mode) == 0o600
    hold_path.chmod(0o644)
    try:
        release.run(store=store, hold_receipt_path=hold_path)
    except ValueError:
        pass
    else:
        raise AssertionError("non-private original receipt accepted")


def test_release_store_cas_requires_exact_reason_and_keeps_approval():
    class Response:
        status_code = 200
        def __init__(self, rows):
            self.rows = rows
        def json(self):
            return self.rows
    class Http:
        def __init__(self):
            self.calls = []
        def patch(self, url, **kwargs):
            self.calls.append(kwargs)
            return Response([{**before, "media_not_ready_reason": None}])
    before = {**row("approved", "approved"), "media_not_ready_reason": hold.REASON}
    http = Http()
    store = SupabaseCalendarStore(url="https://test.supabase.co",
                                  service_key="fake", http=http)
    assert store.release_future_infographic_media("gym", before, "other") is None
    assert http.calls == []
    after = store.release_future_infographic_media("gym", before, hold.REASON)
    assert after["status"] == "approved"
    assert http.calls[0]["params"]["media_not_ready_reason"] == f"eq.{hold.REASON}"
    assert http.calls[0]["params"]["status"] == "eq.approved"
    assert http.calls[0]["json"] == {"media_not_ready_reason": None}


def test_release_records_partial_cas_conflict_without_clearing_other_hold(tmp_path, monkeypatch):
    class ConflictReleaseStore(Store):
        def release_future_infographic_media(self, gym, before, reason):
            if before["id"] == "pending":
                return None
            self.rows[before["id"]]["media_not_ready_reason"] = None
            return dict(self.rows[before["id"]])
    hold_path, release_path = tmp_path / "hold.json", tmp_path / "release.json"
    store = ConflictReleaseStore([row("approved", "approved"), row("pending", "pending")],
                                 hold_path)
    digest = hold.run(store=store, today="2026-10-03")["preflight"]["target_digest"]
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_HOLD_ENABLED", "true")
    assert hold.run(store=store, today="2026-10-03", apply=True,
                    expected_digest=digest, receipt_path=hold_path)["ok"]
    dry = release.run(store=store, hold_receipt_path=hold_path)
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RELEASE_ENABLED", "true")
    result = release.run(store=store, hold_receipt_path=hold_path, apply=True,
                         expected_digest=dry["preflight"]["target_digest"],
                         receipt_path=release_path)
    saved = json.loads(release_path.read_text())
    assert result["ok"] is False
    assert saved["state"] == "partial_release_conflicts"
    assert saved["changed_ids"] == ["approved"]
    assert saved["conflict_ids"] == ["pending"]
    assert store.rows["approved"]["status"] == "approved"
    assert store.rows["pending"]["media_not_ready_reason"] == hold.REASON
