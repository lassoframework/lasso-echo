"""Tests for agent/gym_media_moderation.py — the bounded Drive-media moderation
EVIDENCE producer — and the store's conditional update_moderation_asset write.

Fully offline: the drive fake writes known bytes to the tmp path, the store is
in-memory, and vision is an injected callable. The producer must NEVER approve
(review_status stays 'pending_review') and must fail closed (ok=False, no write)
on every degraded input.
"""
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent import gym_media_moderation as mod  # noqa: E402
from agent import gym_media_review  # noqa: E402
from agent import gym_media_selector as selector  # noqa: E402
from agent.media_source_store import MediaStoreError, SupabaseMediaStore  # noqa: E402
from tests.gym_media_fakes import FakeDrive, FakeMediaStore, make_asset  # noqa: E402

GYM = "pierce"
ASSET_ID = "mod-asset-1"
PHOTO_BYTES = b"\x89PNG-fake-photo-bytes-for-moderation" * 16
NOW = "2026-09-23T12:00:00+00:00"


def _hash_of(blob):
    return hashlib.md5(blob).hexdigest()


def _pending_asset(blob=PHOTO_BYTES, **over):
    """A photo asset in exactly the state the producer touches:
    review_status='pending_review', moderation_status='pending', no review or
    moderation columns populated yet."""
    asset = make_asset(fid=ASSET_ID, gym_id=GYM, kind="photo",
                       content_hash=_hash_of(blob), mime="image/jpeg")
    asset.update({"review_status": "pending_review", "reviewed_by": None,
                  "reviewed_at": None, "review_note": None,
                  "review_content_hash": None, "moderation_status": "pending",
                  "moderation_json": None, "people_detected": None,
                  "consent_status": None})
    asset.update(over)
    return asset


class ModerationFakeStore(FakeMediaStore):
    """FakeMediaStore plus the conditional update_moderation_asset write, mirroring
    SupabaseMediaStore semantics: refuses review columns, requires tenant+hash,
    and raises MediaStoreError(409) when the row no longer matches."""

    def __init__(self, *a, conflict=False, **kw):
        super().__init__(*a, **kw)
        self.moderation_updates = []
        self._conflict = conflict

    def update_moderation_asset(self, gym_id, asset_id, fields, *,
                                expected_content_hash):
        self.moderation_updates.append(
            {"gym_id": gym_id, "asset_id": asset_id,
             "fields": dict(fields), "expected_content_hash": expected_content_hash})
        if self._conflict:
            raise MediaStoreError(409, "asset changed during moderation")
        forbidden = {"review_status", "reviewed_by", "reviewed_at",
                     "review_note", "review_content_hash"} & set(fields)
        if not gym_id or not expected_content_hash or forbidden:
            raise MediaStoreError(400, "bad moderation write")
        asset = self.assets.get(asset_id)
        if (not asset or asset.get("gym_id") != gym_id or
                asset.get("content_hash") != expected_content_hash or
                asset.get("review_status") != "pending_review" or
                asset.get("moderation_status") != "pending"):
            raise MediaStoreError(409, "asset changed during moderation")
        asset.update(fields)
        return True


def _vision(raw):
    return lambda _bytes, _mime="image/jpeg": raw


def _clean_json(people):
    return json.dumps({"verdict": "clean", "people_detected": people})


def _setup(blob=PHOTO_BYTES, **asset_over):
    asset = _pending_asset(blob, **asset_over)
    store = ModerationFakeStore(assets=[asset])
    drive = FakeDrive(blobs={ASSET_ID: blob})
    return asset, store, drive


# ---- 1. clean photo, no people ------------------------------------------------
def test_clean_no_people_writes_evidence_but_never_approves():
    asset, store, drive = _setup()
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(_clean_json(False)), now_iso=NOW)
    assert out["ok"] is True
    assert out["moderation_status"] == "clean"
    assert out["people_detected"] is False
    assert out["verdict"] == "clean"

    row = store.get_asset(ASSET_ID)
    assert row["moderation_status"] == "clean"
    assert row["people_detected"] is False
    assert row["consent_status"] == "not_required"

    ev = row["moderation_json"]
    assert ev["provider"].startswith("gemini:")
    assert ev["verdict"] == "clean"
    assert ev["content_hash"] == asset["content_hash"]
    assert ev["asset_id"] == ASSET_ID
    assert ev["gym_id"] == GYM
    assert ev["people_detected"] is False
    assert ev["observed_at"] == NOW
    # Selector contract: the evidence validates, but is_usable still requires
    # operator review — moderation evidence alone never selects a photo.
    assert selector._clean_moderation_evidence(row) is True
    assert row["review_status"] == "pending_review"
    assert selector.is_usable(row) is False

    call = store.moderation_updates[-1]
    assert "review_status" not in call["fields"]
    assert call["expected_content_hash"] == asset["content_hash"]


# ---- 2. clean photo WITH people -----------------------------------------------
def test_clean_with_people_keeps_consent_pending_and_unusable():
    asset, store, drive = _setup()
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(_clean_json(True)), now_iso=NOW)
    assert out["ok"] is True
    row = store.get_asset(ASSET_ID)
    assert row["moderation_status"] == "clean"
    assert row["people_detected"] is True
    assert row["consent_status"] == "pending"
    assert selector._clean_moderation_evidence(row) is True
    assert selector.is_usable(row) is False  # no consent grant, no review


# ---- 3. unsafe / unknown verdicts ----------------------------------------------
@pytest.mark.parametrize("verdict", ["unsafe", "unknown"])
def test_unsafe_and_unknown_verdicts_flag(verdict):
    asset, store, drive = _setup()
    raw = json.dumps({"verdict": verdict, "people_detected": False})
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(raw), now_iso=NOW)
    assert out["ok"] is True
    assert out["verdict"] == verdict
    row = store.get_asset(ASSET_ID)
    assert row["moderation_status"] == "flagged"
    assert row["consent_status"] == "pending"
    assert selector.is_usable(row) is False


# ---- 4. stale hash: downloaded bytes drifted from indexed content_hash ---------
def test_hash_drift_refuses_and_writes_nothing():
    asset, store, drive = _setup()
    drifted = b"different-bytes-than-indexed"
    drive._blobs[ASSET_ID] = drifted
    assert _hash_of(drifted) != asset["content_hash"]
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(_clean_json(False)), now_iso=NOW)
    assert out["ok"] is False
    assert "hash drift" in out["reason"]
    assert store.moderation_updates == []
    row = store.get_asset(ASSET_ID)
    assert row["moderation_status"] == "pending"
    assert row["moderation_json"] is None


# ---- 5. cross-gym asset --------------------------------------------------------
def test_cross_gym_asset_refused_without_write():
    asset, store, drive = _setup(gym_id="othergym")
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(_clean_json(False)), now_iso=NOW)
    assert out["ok"] is False
    assert "another gym" in out["reason"]
    assert store.moderation_updates == []
    assert drive.downloads == []  # never even downloaded


# ---- 6. provider failures -------------------------------------------------------
def test_vision_raise_fails_closed():
    def boom(_b, _m):
        raise RuntimeError("quota")
    asset, store, drive = _setup()
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=boom, now_iso=NOW)
    assert out["ok"] is False
    assert "RuntimeError" in out["reason"]
    assert store.moderation_updates == []


@pytest.mark.parametrize("raw", [
    "not json at all",
    "```json\n{\"verdict\": \"clean\", \"people_detected\": false}\n```",
    json.dumps({"verdict": "clean"}),                                   # missing key
    json.dumps({"verdict": "clean", "people_detected": False,
                "confidence": 0.9}),                                    # extra key
    json.dumps({"verdict": "iffy", "people_detected": False}),          # verdict off-enum
    json.dumps({"verdict": "clean", "people_detected": "yes"}),         # not strict bool
    '{"verdict":"unsafe","verdict":"clean","people_detected":false}',
    '{"verdict":"clean","people_detected":true,"people_detected":false}',
])
def test_unparseable_or_ambiguous_output_fails_closed(raw):
    asset, store, drive = _setup()
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(raw), now_iso=NOW)
    assert out["ok"] is False
    assert store.moderation_updates == []
    assert store.get_asset(ASSET_ID)["moderation_status"] == "pending"


def test_people_detected_null_with_clean_is_ambiguous_no_write():
    # people_detected=null is a valid parse (consent stays pending, row records
    # None) — but here assert the AMBIGUOUS contract: null does NOT become a
    # no-people clean, and the row is not usable.
    asset, store, drive = _setup()
    raw = json.dumps({"verdict": "clean", "people_detected": None})
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(raw), now_iso=NOW)
    assert out["ok"] is True
    row = store.get_asset(ASSET_ID)
    assert row["moderation_status"] == "clean"
    assert row["people_detected"] is None
    assert row["consent_status"] == "pending"
    assert selector.is_usable(row) is False


def test_default_vision_unarmed_without_api_key(monkeypatch):
    monkeypatch.delenv(config.NANO_API_KEY_ENV, raising=False)
    assert mod.default_vision() is None
    asset, store, drive = _setup()
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=None, now_iso=NOW)
    assert out["ok"] is False
    assert "no vision provider" in out["reason"]
    assert store.moderation_updates == []


def test_parse_verdict_strictness():
    assert mod.parse_verdict('{"verdict":"clean","people_detected":false}') == ("clean", False)
    assert mod.parse_verdict('{"verdict":"unknown","people_detected":null}') == ("unknown", None)
    assert mod.parse_verdict("") is None
    assert mod.parse_verdict(None) is None
    assert mod.parse_verdict('{"verdict":"clean","people_detected":true,"x":1}') is None
    assert mod.parse_verdict('{"verdict":"unsafe","verdict":"clean","people_detected":false}') is None


# ---- 7. conditional-write conflict ----------------------------------------------
def test_conflict_409_returns_ok_false_no_crash():
    asset = _pending_asset()
    store = ModerationFakeStore(assets=[asset], conflict=True)
    drive = FakeDrive(blobs={ASSET_ID: PHOTO_BYTES})
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(_clean_json(False)), now_iso=NOW)
    assert out["ok"] is False
    assert "conflict" in out["reason"]
    assert store.get_asset(ASSET_ID)["moderation_status"] == "pending"
    assert selector.is_usable(store.get_asset(ASSET_ID)) is False


def test_unexpected_store_error_propagates():
    class LoudStore(ModerationFakeStore):
        def update_moderation_asset(self, *a, **kw):
            raise MediaStoreError(500, "store down")
    store = LoudStore(assets=[_pending_asset()])
    drive = FakeDrive(blobs={ASSET_ID: PHOTO_BYTES})
    with pytest.raises(MediaStoreError):
        mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                           vision=_vision(_clean_json(False)), now_iso=NOW)


# ---- additional fail-closed gates ------------------------------------------------
def test_non_photo_skipped_without_write():
    asset, store, drive = _setup(kind="video", mime="video/mp4")
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(_clean_json(False)), now_iso=NOW)
    assert out["ok"] is False
    assert "not a photo" in out["reason"]
    assert store.moderation_updates == []


def test_missing_content_hash_fails_closed():
    asset, store, drive = _setup(content_hash="")
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(_clean_json(False)), now_iso=NOW)
    assert out["ok"] is False
    assert store.moderation_updates == []


def test_already_moderated_or_reviewed_rows_not_rewritten():
    for over in ({"moderation_status": "clean"}, {"review_status": "approved"}):
        asset, store, drive = _setup(**over)
        out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                                 vision=_vision(_clean_json(False)), now_iso=NOW)
        assert out["ok"] is False
        assert store.moderation_updates == []


def test_missing_asset_fails_closed():
    store = ModerationFakeStore(assets=[])
    drive = FakeDrive()
    out = mod.moderate_asset(GYM, "nope", store=store, drive=drive,
                             vision=_vision(_clean_json(False)), now_iso=NOW)
    assert out["ok"] is False
    assert out["reason"] == "asset not found"


# ---- 9. no auto-approval: moderation + selector, then operator review -----------
def test_moderation_then_operator_approve_makes_usable_end_to_end():
    asset, store, drive = _setup()
    out = mod.moderate_asset(GYM, ASSET_ID, store=store, drive=drive,
                             vision=_vision(_clean_json(False)), now_iso=NOW)
    assert out["ok"] is True
    row = store.get_asset(ASSET_ID)
    assert selector.is_usable(row) is False  # evidence alone is not approval

    fields = gym_media_review.review_asset(GYM, ASSET_ID, "approve",
                                           store=store, operator="op-1")
    assert fields["review_status"] == "approved"
    final = store.get_asset(ASSET_ID)
    assert final["review_status"] == "approved"
    assert final["review_content_hash"] == final["content_hash"]
    assert selector.is_usable(final) is True  # ONLY after the operator path


# ---- 8. SupabaseMediaStore.update_moderation_asset unit tests (fake http) --------
class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        return self._body


class _FakeHttp:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def patch(self, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "json": dict(json or {}), "headers": dict(headers or {})})
        return self.resp


def _store(http):
    return SupabaseMediaStore(url="http://store.test", service_key="k", http=http)


def _valid_fields(hash_value="h1"):
    return {"moderation_status": "clean", "people_detected": False,
            "consent_status": "not_required",
            "moderation_json": {"verdict": "clean", "provider": "gemini:test",
                                "content_hash": hash_value, "asset_id": ASSET_ID,
                                "gym_id": GYM, "people_detected": False,
                                "observed_at": NOW}}


def test_update_moderation_asset_refuses_review_columns():
    http = _FakeHttp(_Resp(200, [{"id": ASSET_ID}]))
    store = _store(http)
    for col in ("review_status", "reviewed_by", "reviewed_at",
                "review_note", "review_content_hash"):
        with pytest.raises(MediaStoreError) as ei:
            store.update_moderation_asset(GYM, ASSET_ID, {col: "x"},
                                          expected_content_hash="h")
        assert ei.value.status == 400
    assert http.calls == []  # refused before any HTTP


@pytest.mark.parametrize("field,value", [
    ("consent_status", "granted"),
    ("release_ref", "fabricated"),
    ("consent_member_ref", "fabricated"),
    ("eligible", True),
    ("excluded_by_coach", False),
])
def test_update_moderation_asset_refuses_consent_and_other_state(field, value):
    http = _FakeHttp(_Resp(200, [{"id": ASSET_ID}]))
    fields = _valid_fields()
    fields[field] = value
    with pytest.raises(MediaStoreError) as ei:
        _store(http).update_moderation_asset(GYM, ASSET_ID, fields,
                                             expected_content_hash="h1")
    assert ei.value.status == 400
    assert http.calls == []


def test_update_moderation_asset_requires_tenant_and_hash():
    store = _store(_FakeHttp(_Resp(200, [{"id": ASSET_ID}])))
    with pytest.raises(MediaStoreError):
        store.update_moderation_asset("", ASSET_ID, {"moderation_status": "clean"},
                                      expected_content_hash="h")
    with pytest.raises(MediaStoreError):
        store.update_moderation_asset(GYM, ASSET_ID, {"moderation_status": "clean"},
                                      expected_content_hash="")


def test_update_moderation_asset_sends_conditional_patch():
    http = _FakeHttp(_Resp(200, [{"id": ASSET_ID}]))
    store = _store(http)
    fields = _valid_fields()
    assert store.update_moderation_asset(GYM, ASSET_ID, fields,
                                         expected_content_hash="h1") is True
    call = http.calls[-1]
    assert call["url"] == "http://store.test/rest/v1/media_asset"
    assert call["params"] == {"id": f"eq.{ASSET_ID}", "gym_id": f"eq.{GYM}",
                              "content_hash": "eq.h1",
                              "review_status": "eq.pending_review",
                              "moderation_status": "eq.pending"}
    assert call["json"] == fields
    assert "return=representation" in call["headers"]["Prefer"]


def test_update_moderation_asset_zero_rows_is_409():
    http = _FakeHttp(_Resp(200, []))
    store = _store(http)
    with pytest.raises(MediaStoreError) as ei:
        store.update_moderation_asset(GYM, ASSET_ID, _valid_fields(),
                                      expected_content_hash="h1")
    assert ei.value.status == 409
