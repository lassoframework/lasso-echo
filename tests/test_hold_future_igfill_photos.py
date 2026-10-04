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
        if self.rows[before["id"]]["status"] == "approved":
            self.rows[before["id"]]["status"] = "pending"
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
    # approved rows atomically demote to pending; pending rows stay pending
    assert {r["status"] for r in saved["readback"]} == {"pending"}
    assert all(r["media_not_ready_reason"] == hold.REASON for r in saved["readback"])
    assert all(r["caption"] == "original caption" for r in saved["readback"])
    assert all(r["image_url"].endswith("card.png") for r in saved["readback"])


def test_store_cas_filters_protected_fields_and_demotes_approved_hold():
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
            return Response({**self.before, "media_not_ready_reason": "hold",
                             "status": "pending"})
    before = row("approved", "approved")
    http = Http(before)
    store = SupabaseCalendarStore(url="https://test.supabase.co",
                                  service_key="fake", http=http)
    result = store.hold_future_infographic_media("gym", before, "hold")
    assert result["status"] == "pending"
    url, call = http.calls[0]
    assert url.endswith("/content_calendar")
    assert call["json"] == {"media_not_ready_reason": "hold", "status": "pending"}
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


def test_release_requires_original_private_receipt_and_never_reapproves(tmp_path, monkeypatch):
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
    # the held approved row was demoted to pending and releases back to
    # pending only; release never restores the approval
    assert store.rows["approved"]["status"] == "pending"
    assert store.rows["approved"]["media_not_ready_reason"] is None
    assert all(r["status"] == "pending"
               for r in json.loads(release_path.read_text())["readback"])
    assert json.loads(release_path.read_text())["state"] == "readback_verified"
    assert stat.S_IMODE(release_path.stat().st_mode) == 0o600
    hold_path.chmod(0o644)
    try:
        release.run(store=store, hold_receipt_path=hold_path)
    except ValueError:
        pass
    else:
        raise AssertionError("non-private original receipt accepted")


def test_release_store_cas_requires_exact_reason_and_keeps_pending():
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
    # held rows are persisted as pending (approved rows demote on hold), and
    # release clears only the reason -- the row stays pending, never reapproved
    before = {**row("p1", "pending"), "media_not_ready_reason": hold.REASON}
    http = Http()
    store = SupabaseCalendarStore(url="https://test.supabase.co",
                                  service_key="fake", http=http)
    assert store.release_future_infographic_media("gym", before, "other") is None
    assert http.calls == []
    after = store.release_future_infographic_media("gym", before, hold.REASON)
    assert after["status"] == "pending"
    assert after["media_not_ready_reason"] is None
    assert http.calls[0]["params"]["media_not_ready_reason"] == f"eq.{hold.REASON}"
    assert http.calls[0]["params"]["status"] == "eq.pending"
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
    # the approved-origin row was demoted by the hold and stays pending
    assert store.rows["approved"]["status"] == "pending"
    assert store.rows["pending"]["media_not_ready_reason"] == hold.REASON


def _craft_hold_receipt(path, before_rows, readback_rows, changed):
    """Build a structurally valid hold receipt for release-path tests."""
    payload = {"operation": "hold_future_igfill_photos", "today": "2026-10-03",
               "reason": hold.REASON, "state": "readback_verified",
               "target_digest": hold._digest(before_rows),
               "target_count": len(before_rows), "before_image": before_rows,
               "readback": readback_rows, "changed_ids": changed,
               "conflict_ids": []}
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return payload


def test_hold_apply_readback_rejects_still_approved_false_success(tmp_path, monkeypatch):
    """A store reporting the row still approved after the hold write is the
    trigger-rejected state and must be a mismatch, not a success."""

    class NoDemoteStore(Store):
        def hold_future_infographic_media(self, gym, before, reason):
            self.rows[before["id"]]["media_not_ready_reason"] = reason
            return dict(self.rows[before["id"]])

    path = tmp_path / "hold.json"
    store = NoDemoteStore([row("approved", "approved")], path)
    digest = hold.run(store=store, today="2026-10-03")["preflight"]["target_digest"]
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_HOLD_ENABLED", "true")
    result = hold.run(store=store, today="2026-10-03", apply=True,
                      expected_digest=digest, receipt_path=path)
    assert result["ok"] is False
    assert "readback mismatch" in result["reason"]
    saved = json.loads(path.read_text())
    assert saved["changed_ids"] == []
    # receipt stalled at the write intent for the failed row: no readback was
    # ever recorded for a trigger-rejected hold
    assert saved["state"] == "write_intent"
    assert saved["inflight_id"] == "approved"


def test_release_accepts_legacy_approved_receipt_and_never_reapproves(tmp_path, monkeypatch):
    """Receipts written before the demotion patch still show the held row as
    approved; the persisted row is pending. Release must target the demoted
    image and verify the row returns to pending -- never reapproved."""
    original = row("a1", "approved")
    legacy_held = {**original, "media_not_ready_reason": hold.REASON}
    hold_path = tmp_path / "hold.json"
    _craft_hold_receipt(hold_path, [original], [legacy_held], ["a1"])
    persisted = {**legacy_held, "status": "pending"}

    class LegacyStore:
        def __init__(self):
            self.row = dict(persisted)
            self.release_images = []

        def get_row(self, gym, id_):
            return dict(self.row)

        def release_future_infographic_media(self, gym, before, reason):
            self.release_images.append(dict(before))
            self.row["media_not_ready_reason"] = None
            return dict(self.row)

    store = LegacyStore()
    dry = release.run(store=store, hold_receipt_path=hold_path)
    assert dry["ok"] and dry["preflight"]["target_count"] == 1
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RELEASE_ENABLED", "true")
    applied = release.run(store=store, hold_receipt_path=hold_path, apply=True,
                          expected_digest=dry["preflight"]["target_digest"],
                          receipt_path=tmp_path / "release.json")
    assert applied["ok"]
    assert store.release_images[0]["status"] == "pending"
    assert store.row["status"] == "pending"


def test_release_detects_false_success_when_store_reapproves(tmp_path, monkeypatch):
    """If the store wrongly returns the row to approved on release, the
    readback contract must flag it instead of reporting success."""
    original = row("a1", "approved")
    legacy_held = {**original, "media_not_ready_reason": hold.REASON}
    hold_path = tmp_path / "hold.json"
    _craft_hold_receipt(hold_path, [original], [legacy_held], ["a1"])

    class ReapprovingStore:
        def get_row(self, gym, id_):
            return {**original, "media_not_ready_reason": hold.REASON,
                    "status": "pending"}

        def release_future_infographic_media(self, gym, before, reason):
            return {**original, "media_not_ready_reason": None}  # wrongly approved

    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RELEASE_ENABLED", "true")
    dry = release.run(store=ReapprovingStore(), hold_receipt_path=hold_path)
    result = release.run(store=ReapprovingStore(), hold_receipt_path=hold_path,
                         apply=True,
                         expected_digest=dry["preflight"]["target_digest"],
                         receipt_path=tmp_path / "release.json")
    assert result["ok"] is False
    assert "readback mismatch" in result["reason"]


def test_release_never_targets_scene_review_hold_or_archived_rows(tmp_path):
    """Held rows carrying a scene_review_hold reason or an archived variant
    are conflicts at preflight, never release targets."""
    original = row("p1", "pending")
    held = {**original, "media_not_ready_reason": hold.REASON}
    hold_path = tmp_path / "hold.json"
    source = _craft_hold_receipt(hold_path, [original], [held], ["p1"])
    protected = dict(held)
    original_source = release._original_receipt(hold_path)
    monkeypatch_source = {"operation": source["operation"], "today": source["today"],
                          "reason": source["reason"], "state": source["state"],
                          "target_digest": source["target_digest"],
                          "target_count": source["target_count"],
                          "before_image": source["before_image"],
                          "changed_ids": ["p1", "p2", "p3"], "conflict_ids": []}
    scene_hold = dict(held, id="p2", media_not_ready_reason="scene_review_hold")
    archived = dict(held, id="p3", variant_status="archived")
    monkeypatch_source["readback"] = [held, scene_hold, archived]
    monkeypatch_source["before_image"] = [
        original, dict(original, id="p2"), dict(original, id="p3")]

    class EmptyStore:
        def get_row(self, gym, id_):
            return None

    original_fn = release._original_receipt
    release._original_receipt = lambda path: (monkeypatch_source, [held, scene_hold, archived])
    try:
        dry = release.run(store=EmptyStore(), hold_receipt_path=hold_path)
    finally:
        release._original_receipt = original_fn
    assert original_source  # sanity: the crafted receipt itself stays valid
    assert dry["ok"] is False
    assert set(dry["preflight"]["conflict_ids"]) == {"p1", "p2", "p3"}


def test_release_dry_run_flags_stale_or_drifted_rows(tmp_path):
    """A persisted row missing the hold reason (stale release, cleared or
    taken over) is a conflict, not a silent success."""
    original = row("p1", "pending")
    held = {**original, "media_not_ready_reason": hold.REASON}
    hold_path = tmp_path / "hold.json"
    _craft_hold_receipt(hold_path, [original], [held], ["p1"])

    class StaleStore:
        def get_row(self, gym, id_):
            return dict(original)  # reason already cleared

    dry = release.run(store=StaleStore(), hold_receipt_path=hold_path)
    assert dry["ok"] is False
    assert dry["preflight"]["conflict_ids"] == ["p1"]
    assert dry["preflight"]["target_count"] == 0


def test_release_preflight_rejects_persisted_approved_legacy_row(tmp_path, monkeypatch):
    """P1: a legacy receipt whose row is still persisted as approved-with-hold
    must be refused at preflight, before any mutation -- clearing only
    media_not_ready_reason would leave it approved and publishable."""
    original = row("a1", "approved")
    legacy_held = {**original, "media_not_ready_reason": hold.REASON}
    hold_path = tmp_path / "hold.json"
    _craft_hold_receipt(hold_path, [original], [legacy_held], ["a1"])

    class ApprovedStore:
        def __init__(self):
            self.release_calls = []

        def get_row(self, gym, id_):
            return dict(legacy_held)  # still approved with the hold reason

        def release_future_infographic_media(self, gym, before, reason):
            self.release_calls.append(dict(before))
            return {**before, "media_not_ready_reason": None}

    store = ApprovedStore()
    dry = release.run(store=store, hold_receipt_path=hold_path)
    assert dry["ok"] is False
    assert dry["preflight"]["conflict_ids"] == ["a1"]
    assert dry["preflight"]["target_count"] == 0
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RELEASE_ENABLED", "true")
    applied = release.run(store=store, hold_receipt_path=hold_path, apply=True,
                          expected_digest=dry["preflight"]["target_digest"],
                          receipt_path=tmp_path / "release.json")
    assert applied["ok"] is False
    assert store.release_calls == []  # fail closed: nothing was mutated


def test_store_release_rejects_approved_before_any_patch():
    """Direct store call: a persisted approved-with-hold row is refused with
    no HTTP PATCH issued, so it can never be released while publishable."""

    class Http:
        def __init__(self):
            self.calls = []

        def patch(self, url, **kwargs):
            self.calls.append((url, kwargs))
            raise AssertionError("PATCH must not fire for an approved row")

    http = Http()
    store = SupabaseCalendarStore(url="https://test.supabase.co",
                                  service_key="fake", http=http)
    approved_held = {**row("a1", "approved"),
                     "media_not_ready_reason": hold.REASON}
    assert store.release_future_infographic_media("gym", approved_held,
                                                  hold.REASON) is None
    assert http.calls == []
    # pending rows with the exact reason are still accepted and PATCHed
    pending_held = {**row("p1", "pending"),
                    "media_not_ready_reason": hold.REASON}

    class OkResponse:
        status_code = 200

        def json(self):
            return [{**pending_held, "media_not_ready_reason": None}]

    class OkHttp:
        def __init__(self):
            self.calls = []

        def patch(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return OkResponse()

    ok_http = OkHttp()
    store = SupabaseCalendarStore(url="https://test.supabase.co",
                                  service_key="fake", http=ok_http)
    after = store.release_future_infographic_media("gym", pending_held, hold.REASON)
    assert after["status"] == "pending"
    assert after["media_not_ready_reason"] is None
    assert ok_http.calls[0][1]["json"] == {"media_not_ready_reason": None}


def test_release_conflicts_on_present_scene_review_hold_and_archived_rows(tmp_path):
    """Held rows actually persisted behind a scene_review_hold or an archived
    variant are conflicts with their real rows present -- never released."""
    base = row("p1", "pending")
    held = {**base, "media_not_ready_reason": hold.REASON}
    scene_row = dict(held, id="p2", media_not_ready_reason="scene_review_hold")
    archived_row = dict(held, id="p3", variant_status="archived")
    hold_path = tmp_path / "hold.json"
    _craft_hold_receipt(hold_path, [base], [held], ["p1"])
    # _original_receipt validates that a readback row carries the exact hold
    # reason, so inject the protected persisted rows past receipt parsing.
    injected = {"operation": "hold_future_igfill_photos", "today": "2026-10-03",
                "reason": hold.REASON, "state": "readback_verified",
                "target_digest": hold._digest([base, dict(base, id="p2"),
                                               dict(base, id="p3")]),
                "target_count": 3,
                "before_image": [base, dict(base, id="p2"), dict(base, id="p3")],
                "readback": [held, scene_row, archived_row],
                "changed_ids": ["p1", "p2", "p3"], "conflict_ids": []}

    class PresentStore:
        def __init__(self):
            self.release_calls = []

        def get_row(self, gym, id_):
            return dict({"p1": held, "p2": scene_row, "p3": archived_row}[id_])

        def release_future_infographic_media(self, gym, before, reason):
            self.release_calls.append(before["id"])
            return dict(before, media_not_ready_reason=None)

    store = PresentStore()
    original_fn = release._original_receipt
    release._original_receipt = lambda path: (injected, [held, scene_row, archived_row])
    try:
        dry = release.run(store=store, hold_receipt_path=hold_path)
        assert dry["ok"] is False
        assert set(dry["preflight"]["conflict_ids"]) == {"p2", "p3"}
        assert dry["preflight"]["target_count"] == 1
        applied = release.run(store=store, hold_receipt_path=hold_path, apply=True,
                              expected_digest=injected["target_digest"],
                              receipt_path=tmp_path / "release.json")
    finally:
        release._original_receipt = original_fn
    assert applied["ok"] is False
    assert store.release_calls == []


def test_release_no_false_success_receipt_on_reapproval(tmp_path, monkeypatch):
    """If the store wrongly returns the row to approved on release, the run
    must fail and its receipt must not record a verified success."""
    original = row("a1", "approved")
    legacy_held = {**original, "media_not_ready_reason": hold.REASON}
    hold_path = tmp_path / "hold.json"
    _craft_hold_receipt(hold_path, [original], [legacy_held], ["a1"])

    class ReapprovingStore:
        def __init__(self):
            self.row = {**legacy_held, "status": "pending"}

        def get_row(self, gym, id_):
            if self.row["media_not_ready_reason"] is None:
                return {**original, "media_not_ready_reason": None}
            return dict(self.row)

        def release_future_infographic_media(self, gym, before, reason):
            assert before["status"] == "pending"  # demoted image only
            self.row["media_not_ready_reason"] = None
            self.row["status"] = "approved"  # store bug: reapproves
            return dict(self.row)

    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RELEASE_ENABLED", "true")
    store = ReapprovingStore()
    dry = release.run(store=store, hold_receipt_path=hold_path)
    receipt_path = tmp_path / "release.json"
    result = release.run(store=store, hold_receipt_path=hold_path, apply=True,
                         expected_digest=dry["preflight"]["target_digest"],
                         receipt_path=receipt_path)
    assert result["ok"] is False
    assert "readback mismatch" in result["reason"]
    saved = json.loads(receipt_path.read_text())
    assert saved["state"] != "readback_verified"
    assert saved["changed_ids"] == []
