"""Offline tests for replace_future_repeat_media / release_future_repeat_media_hold."""
import copy

import pytest

from agent.portal_calendar_store import SupabaseCalendarStore, PortalStoreError

HOLD = "cross_date_media_repeat_needs_new_visual"
NEW_IMAGE = "https://cdn.test/new-verified-photo.jpg"
NEW_ASSET = "asset-approved-123"
TODAY = "2026-10-07"


def _row(row_id="row-1", gym="gym-a", **over):
    row = {
        "id": row_id, "gym_id": gym, "post_date": "2026-11-01", "status": "pending",
        "slot_index": 1, "time_slot": None, "variant_status": "active",
        "account": "instagram", "format": "feed",
        "caption": "Keep this caption exactly, commas and all.",
        "image_url": f"https://cdn.test/old-{row_id}.jpg",
        "thumbnail_url": None, "source_media_url": None,
        "source_media_asset_id": None, "drive_file_id": None,
        "visual_group_key": None, "byte_hash": None, "r2_key": None,
        "media_not_ready_reason": HOLD,
        "created_at": "2026-09-30T12:00:00Z", "published_at": None,
        "late_post_id": None, "publish_claim_token": None,
        "publish_reservation_day": None, "scheduled_at": None,
    }
    row.update(over)
    return row


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class MemoryHttp:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.mutate_before_apply = None
        self.corrupt_response = None

    def patch(self, url, *, params, json, **kwargs):
        self.calls.append((url, copy.deepcopy(params), copy.deepcopy(json)))
        if self.mutate_before_apply is not None:
            self.mutate_before_apply()
        matches = []
        for row in self.store.rows.values():
            if all((row.get(field) is None if predicate == "is.null"
                    else str(row.get(field)) == predicate[3:])
                   for field, predicate in params.items()):
                matches.append(row)
        if len(matches) == 1:
            matches[0].update(json)
        body = copy.deepcopy(matches)
        if self.corrupt_response is not None and body:
            body[0].update(self.corrupt_response)
        return Response(body)


class MemoryStore(SupabaseCalendarStore):
    def __init__(self, rows):
        self.rows = {row["id"]: copy.deepcopy(row) for row in rows}
        self.http = MemoryHttp(self)

    def replace_future_repeat_media(self, account_key, current, **kwargs):
        kwargs.setdefault("today", TODAY)
        kwargs.setdefault("source_media_url", kwargs.get("image_url"))
        return super().replace_future_repeat_media(account_key, current, **kwargs)

    def _client(self):
        return self.http

    def _rest(self, table):
        return f"https://supabase.test/rest/v1/{table}"

    def _headers(self, extra=None):
        return dict(extra or {})


def _store(*rows):
    return MemoryStore(rows)


def test_replace_success_pending_preserves_everything_but_media():
    store = _store(_row())
    before = copy.deepcopy(store.rows["row-1"])
    after = store.replace_future_repeat_media(
        "gym-a", copy.deepcopy(before), image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET)
    assert after is not None
    live = store.rows["row-1"]
    assert live["image_url"] == NEW_IMAGE
    assert live["source_media_asset_id"] == NEW_ASSET
    assert live["media_not_ready_reason"].startswith(HOLD + ":staged:")  # held until verified
    for key in ("status", "caption", "post_date", "slot_index", "account",
                "format", "variant_status", "created_at"):
        assert live[key] == before[key]


def test_replace_success_approved_keeps_approval():
    store = _store(_row(status="approved"))
    after = store.replace_future_repeat_media(
        "gym-a", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET)
    assert after is not None and after["status"] == "approved"


def test_release_after_verification_clears_only_hold():
    store = _store(_row(status="approved"))
    original = copy.deepcopy(store.rows["row-1"])
    staged = store.replace_future_repeat_media(
        "gym-a", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET)
    assert staged is not None
    released = store.release_future_repeat_media_hold(
        "gym-a", copy.deepcopy(staged), original=original, today=TODAY)
    assert released is not None
    live = store.rows["row-1"]
    assert live["media_not_ready_reason"] is None
    assert live["status"] == "approved"
    assert live["image_url"] == NEW_IMAGE
    assert live["caption"] == staged["caption"]


def test_concurrent_edit_between_read_and_write_loses_race():
    store = _store(_row())
    observed = copy.deepcopy(store.rows["row-1"])

    def mutate():
        store.rows["row-1"]["caption"] = "Operator rewrote the caption."

    store.http.mutate_before_apply = mutate
    assert store.replace_future_repeat_media(
        "gym-a", observed, image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET) is None
    assert store.rows["row-1"]["image_url"].endswith("old-row-1.jpg")
    assert store.rows["row-1"]["caption"] == "Operator rewrote the caption."
    assert store.rows["row-1"]["media_not_ready_reason"] == HOLD


def test_concurrent_approval_change_loses_race():
    store = _store(_row())  # observed as pending
    observed = copy.deepcopy(store.rows["row-1"])

    def mutate():
        store.rows["row-1"]["status"] = "approved"

    store.http.mutate_before_apply = mutate
    assert store.replace_future_repeat_media(
        "gym-a", observed, image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET) is None
    assert store.rows["row-1"]["status"] == "approved"
    assert store.rows["row-1"]["image_url"].endswith("old-row-1.jpg")


def test_publish_claim_blocks_replace():
    store = _store(_row(publish_claim_token="tok-1"))
    assert store.replace_future_repeat_media(
        "gym-a", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET) is None
    assert store.http.calls == []
    assert store.rows["row-1"]["image_url"].endswith("old-row-1.jpg")


def test_published_or_reserved_rows_rejected():
    for over in ({"published_at": "2026-10-01T00:00:00Z"},
                 {"publish_reservation_day": "2026-11-01"},
                 {"scheduled_at": "2026-10-31T00:00:00Z"},
                 {"late_post_id": "lp-1"}):
        store = _store(_row(**over))
        assert store.replace_future_repeat_media(
            "gym-a", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
            source_media_asset_id=NEW_ASSET) is None
        assert store.http.calls == []


def test_foreign_gym_never_written():
    store = _store(_row(gym="gym-a"))
    assert store.replace_future_repeat_media(
        "gym-b", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET) is None
    assert store.http.calls == []
    assert store.rows["row-1"]["image_url"].endswith("old-row-1.jpg")


def test_wrong_hold_reason_rejected():
    for reason in (None, "some_other_hold", "cross_date_media_repeat_needs_new_visual "):
        store = _store(_row(media_not_ready_reason=reason))
        assert store.replace_future_repeat_media(
            "gym-a", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
            source_media_asset_id=NEW_ASSET) is None
        assert store.http.calls == []
        assert store.release_future_repeat_media_hold(
            "gym-a", copy.deepcopy(store.rows["row-1"])) is None


def test_no_photo_or_no_approved_asset_rejected():
    for kwargs in ({"image_url": "http://insecure.test/x.jpg"},
                   {"image_url": ""},
                   {"image_url": None},
                   {"source_media_asset_id": ""},
                   {"source_media_asset_id": "   "},
                   {"source_media_asset_id": None}):
        call = {"image_url": NEW_IMAGE, "source_media_asset_id": NEW_ASSET}
        call.update(kwargs)
        store = _store(_row())
        assert store.replace_future_repeat_media(
            "gym-a", copy.deepcopy(store.rows["row-1"]), **call) is None
        assert store.http.calls == []


def test_same_old_url_rejected():
    store = _store(_row(source_media_url="https://src.test/old.jpg"))
    observed = copy.deepcopy(store.rows["row-1"])
    assert store.replace_future_repeat_media(
        "gym-a", observed, image_url=observed["image_url"],
        source_media_asset_id=NEW_ASSET) is None
    assert store.replace_future_repeat_media(
        "gym-a", observed, image_url=observed["source_media_url"],
        source_media_asset_id=NEW_ASSET) is None
    assert store.http.calls == []


def test_readback_failure_returns_none_and_keeps_hold():
    store = _store(_row())
    store.http.corrupt_response = {"caption": "tampered readback"}
    assert store.replace_future_repeat_media(
        "gym-a", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET) is None


def test_readback_missing_hold_returns_none():
    store = _store(_row())
    store.http.corrupt_response = {"media_not_ready_reason": None}
    assert store.replace_future_repeat_media(
        "gym-a", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET) is None


def test_release_requires_exact_held_row_and_preserves_fields():
    store = _store(_row())
    # An unstaged or stale old row never releases the repeat hold.
    observed = copy.deepcopy(store.rows["row-1"])
    store.rows["row-1"]["image_url"] = NEW_IMAGE  # concurrent change
    assert store.release_future_repeat_media_hold("gym-a", observed) is None
    assert store.rows["row-1"]["media_not_ready_reason"] == HOLD


def test_missing_core_field_refused():
    store = _store(_row())
    observed = copy.deepcopy(store.rows["row-1"])
    del observed["caption"]
    assert store.replace_future_repeat_media(
        "gym-a", observed, image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET) is None
    assert store.http.calls == []


@pytest.fixture(autouse=True)
def preparation_disabled(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)


@pytest.mark.parametrize("day,today", [
    ("2020-01-01", TODAY), (TODAY, TODAY), ("bad", TODAY),
    ("2026-11-01", None), ("2026-11-01", "bad"),
    ("2026-11-01", "2026-10-07T00:00:00Z"),
])
def test_future_boundary_rejects_missing_malformed_today_and_nonfuture(day, today):
    store = _store(_row(post_date=day))
    original = copy.deepcopy(store.rows["row-1"])
    assert store.replace_future_repeat_media(
        "gym-a", original, image_url=NEW_IMAGE, source_media_asset_id=NEW_ASSET,
        today=today) is None
    assert store.http.calls == []


def test_missing_explicit_source_refused():
    store = _store(_row(source_media_url="https://cdn.test/old-raw.jpg"))
    assert SupabaseCalendarStore.replace_future_repeat_media(
        store, "gym-a", copy.deepcopy(store.rows["row-1"]), image_url=NEW_IMAGE,
        source_media_asset_id=NEW_ASSET, today=TODAY) is None
    assert store.http.calls == []


def test_replacement_clears_stale_source_provenance_and_thumbnail():
    store = _store(_row(source_media_url="https://cdn.test/old-raw.jpg",
                        source_media_asset_id="old-asset", thumbnail_url="https://cdn.test/old-poster.jpg",
                        drive_file_id="old-drive", byte_hash="old-hash",
                        visual_group_key="old-group", r2_key="old-key"))
    original = copy.deepcopy(store.rows["row-1"])
    staged = store.replace_future_repeat_media(
        "gym-a", original, image_url=NEW_IMAGE, source_media_asset_id=NEW_ASSET)
    assert staged["source_media_url"] == NEW_IMAGE
    for key in ("thumbnail_url", "drive_file_id", "byte_hash", "visual_group_key", "r2_key"):
        assert staged[key] is None
    assert store.release_future_repeat_media_hold(
        "gym-a", staged, original=original, today=TODAY) is not None


def test_unchanged_original_cannot_release_even_with_original_argument():
    store = _store(_row())
    original = copy.deepcopy(store.rows["row-1"])
    assert store.release_future_repeat_media_hold(
        "gym-a", original, original=original, today=TODAY) is None
    assert store.http.calls == []
    assert store.rows["row-1"]["media_not_ready_reason"] == HOLD


@pytest.mark.parametrize("change", [{"caption": "Different before caption"},
                                    {"image_url": "https://cdn.test/other-old.jpg"},
                                    {"gym_id": "gym-b"}, {"id": "other-row"}])
def test_release_rejects_wrong_original(change):
    store = _store(_row())
    original = copy.deepcopy(store.rows["row-1"])
    staged = store.replace_future_repeat_media(
        "gym-a", original, image_url=NEW_IMAGE, source_media_asset_id=NEW_ASSET)
    wrong = {**original, **change}
    before_calls = len(store.http.calls)
    assert store.release_future_repeat_media_hold(
        "gym-a", staged, original=wrong, today=TODAY) is None
    assert len(store.http.calls) == before_calls
    assert store.rows["row-1"]["media_not_ready_reason"] == staged["media_not_ready_reason"]


@pytest.mark.parametrize("change", [{"image_url": "https://cdn.test/tampered.jpg"},
                                    {"source_media_asset_id": "wrong-asset"},
                                    {"source_media_url": "https://cdn.test/wrong-source.jpg"},
                                    {"thumbnail_url": "https://cdn.test/old.jpg"}])
def test_release_receipt_binds_every_replacement_identity(change):
    store = _store(_row())
    original = copy.deepcopy(store.rows["row-1"])
    staged = store.replace_future_repeat_media(
        "gym-a", original, image_url=NEW_IMAGE, source_media_asset_id=NEW_ASSET)
    before_calls = len(store.http.calls)
    assert store.release_future_repeat_media_hold(
        "gym-a", {**staged, **change}, original=original, today=TODAY) is None
    assert len(store.http.calls) == before_calls


def test_release_loses_exact_cas_after_new_claim():
    store = _store(_row())
    original = copy.deepcopy(store.rows["row-1"])
    staged = store.replace_future_repeat_media(
        "gym-a", original, image_url=NEW_IMAGE, source_media_asset_id=NEW_ASSET)
    store.http.mutate_before_apply = lambda: store.rows["row-1"].update(publish_claim_token="new-claim")
    assert store.release_future_repeat_media_hold(
        "gym-a", staged, original=original, today=TODAY) is None
    assert store.rows["row-1"]["media_not_ready_reason"] == staged["media_not_ready_reason"]


def test_release_refuses_stage_that_has_aged_into_today():
    store = _store(_row())
    original = copy.deepcopy(store.rows["row-1"])
    staged = store.replace_future_repeat_media(
        "gym-a", original, image_url=NEW_IMAGE, source_media_asset_id=NEW_ASSET)
    before_calls = len(store.http.calls)
    assert store.release_future_repeat_media_hold(
        "gym-a", staged, original=original, today="2026-11-01") is None
    assert len(store.http.calls) == before_calls


def test_prepared_write_stage_receipt_binds_new_prepared_identity(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    store = _store(_row(status="approved"))
    original = copy.deepcopy(store.rows["row-1"])

    def prepare(account_key, current, payload, *evidence):
        assert payload["source_media_url"] == NEW_IMAGE
        assert payload["thumbnail_url"] is None
        assert payload["drive_file_id"] is None
        return {**payload, "visual_group_key": "new-group", "byte_hash": "new-hash"}

    monkeypatch.setattr(store, "_prepare_visual_replacement", prepare)
    staged = store.replace_future_repeat_media(
        "gym-a", original, image_url=NEW_IMAGE, source_media_asset_id=NEW_ASSET)
    assert staged["visual_group_key"] == "new-group"
    assert staged["status"] == "approved"
    assert store.release_future_repeat_media_hold(
        "gym-a", staged, original=original, today=TODAY) is not None


def test_prepared_write_fails_closed_without_draft_scene_columns(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    row = _row(status="approved")
    for key in ("drive_file_id", "visual_group_key", "byte_hash", "r2_key"):
        row.pop(key)
    store = _store(row)
    original = copy.deepcopy(store.rows["row-1"])

    def prepare(account_key, current, payload, *evidence):
        # The writer preparation computes persisted provenance that the live
        # unmigrated schema cannot store. This must fail before any PATCH.
        return {**payload, "visual_group_key": "new-group", "byte_hash": "new-hash"}

    monkeypatch.setattr(store, "_prepare_visual_replacement", prepare)
    with pytest.raises(PortalStoreError, match="requires migrated draft-scene columns"):
        store.replace_future_repeat_media(
            "gym-a", original, image_url=NEW_IMAGE, source_media_asset_id=NEW_ASSET)
    assert store.http.calls == []
    assert store.rows["row-1"]["image_url"] == original["image_url"]
    assert store.rows["row-1"]["media_not_ready_reason"] == HOLD
