"""Drive original-source lineage (handoff 2026-10-05): the Drive md5Checksum
recorded at indexing (media_asset.content_hash) is trustworthy original-byte
identity. It must propagate indexing -> builder -> mirror -> persisted row for
BOTH original-served and rendition-backed drafts, kept strictly separate from
the delivered (possibly transformed) URL. No identity is ever inferred from a
delivery URL, and a foreign-gym / unknown asset fails closed."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_builder as builder  # noqa: E402
from agent import real_calendar_mirror as rcm  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402


class _Acct:
    key = "pierce_ig"
    platform = "instagram"


def _wire(monkeypatch):
    """Same offline stubs as the builder suite (vision + caption + hosting)."""
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None, alert=None: {
                            "version": 2, "quality": {"usable": True},
                            "safety_flags": [], "one_line": "people at the gym"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda a: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify",
                        lambda b, a, **k: {"ok": True, "bucket": "small_group",
                                           "verified_details": []})
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption", []))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, gym: "https://cdn.fake/served.jpg")


def _build(monkeypatch, tmp_path, asset, blobs=None):
    _wire(monkeypatch)
    store = FakeMediaStore(assets=[asset])
    drive = FakeDrive(blobs={asset["id"]: blobs or b"jpgbytes"})
    return builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path)), store


# ---- builder stamping ------------------------------------------------------

def test_original_photo_stamps_source_content_hash(monkeypatch, tmp_path):
    asset = make_asset("p1", gym_id="pierce", kind="photo",
                       content_hash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    draft, _ = _build(monkeypatch, tmp_path, asset)
    assert draft is not None
    assert draft.source_media_asset_id == "p1"
    assert draft.source_media_content_hash == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    # original bytes served: hosted-original provenance rides source_media_url
    assert draft.source_media_url == draft.creative_public_url


def test_rendition_backed_draft_keeps_source_identity_separate(monkeypatch, tmp_path):
    """HEIC -> JPEG rendition: the delivered URL is transformed, so it must NOT
    be recorded as source_media_url — but the ORIGINAL byte identity (the Drive
    md5Checksum) still stamps the draft, lineage intact."""
    monkeypatch.setattr("agent.gym_media_index.heic_to_jpeg",
                        lambda src, dest: open(dest, "wb").write(b"jpg") or dest)
    monkeypatch.setattr("agent.gym_media_index.ensure_rendition",
                        lambda asset, src, **k: ("https://cdn.fake/rend.jpg", True))
    asset = make_asset("h1", gym_id="pierce", kind="photo", title="IMG.HEIC",
                       mime="image/heic", content_hash="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    draft, _ = _build(monkeypatch, tmp_path, asset, blobs=b"heic")
    assert draft is not None
    assert draft.creative_public_url == "https://cdn.fake/rend.jpg"
    # the rendition URL is delivery, not source evidence
    assert not getattr(draft, "source_media_url", "")
    assert draft.source_media_content_hash == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_missing_content_hash_asset_never_stages(monkeypatch, tmp_path):
    """Fail closed on unproven identity: an asset with NO indexed Drive hash is
    refused by the selector itself (pickable drops a hashless asset), so no
    draft is ever staged and no lineage is guessed. The mirror-side omission
    is covered by test_mirror_omits_hash_when_draft_carries_none."""
    from agent import gym_media_selector as sel
    asset = make_asset("p2", gym_id="pierce", kind="photo", content_hash="")
    draft, _ = _build(monkeypatch, tmp_path, asset)
    assert draft is None
    assert sel.pickable("pierce", store=FakeMediaStore(assets=[asset])) == []


def test_cross_gym_asset_never_stamps_or_stages(monkeypatch, tmp_path):
    """A foreign-gym asset is blocked at the stage-time tenant assertion: no
    draft, no stamp, no publish — never cleared by guessing (spec §1.5d)."""
    _wire(monkeypatch)
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append((k, m)) or True)
    foreign = make_asset("x", gym_id="other_gym", kind="photo",
                         content_hash="cccccccccccccccccccccccccccccccc")
    monkeypatch.setattr("agent.gym_media_selector.pick_media",
                        lambda gym_id, kind_preference=None, store=None, now=None,
                        exclude_ids=(), post_date=None: foreign if "x" not in exclude_ids else None)
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-27", "faces", voice=object(), source=object(),
        store=FakeMediaStore(), drive=FakeDrive(blobs={"x": b"jpg"}),
        library_dir=str(tmp_path))
    assert draft is None
    assert fired and any("tenant" in m.lower() for _, m in fired)


# ---- mirror persistence ----------------------------------------------------

class _FakeStore:
    def __init__(self, drafts):
        self._drafts = list(drafts)

    def list_for_account(self, account_key):
        return [d for d in self._drafts if d.account_key == account_key]


def _mirror_draft(**over):
    fields = dict(draft_id="gymmedia_p1_2026-08-27", account_key="pierce_ig",
                  platform="instagram", caption="hi", hashtags=[],
                  creative_path="x.jpg", creative_public_url="https://cdn/served.jpg",
                  scheduled_for="", status=DraftStatus.PENDING,
                  day_key="2026-08-27", draft_type="gym_media", category="faces",
                  source_media_asset_id="p1")
    fields.update(over)
    d = Draft(**{k: v for k, v in fields.items() if k in Draft.__dataclass_fields__})
    # dynamic lineage attrs, exactly as the builder stamps them
    for k, v in over.items():
        if k not in Draft.__dataclass_fields__:
            setattr(d, k, v)
    return d


def _hash_flag_on(monkeypatch):
    from agent import config
    monkeypatch.setattr(config, "source_media_content_hash_enabled", lambda: True,
                        raising=False)


def test_mirror_row_carries_source_content_hash_distinct_from_delivery(monkeypatch):
    _hash_flag_on(monkeypatch)
    draft = _mirror_draft(source_media_url="https://cdn/served.jpg",
                          source_media_content_hash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    row = rcm.collect_real_drafts("pierce_ig", _FakeStore([draft]))[0]
    assert row["source_media_content_hash"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert row["source_media_asset_id"] == "p1"
    # delivery addresses stay separate fields
    assert row["image_url"] == "https://cdn/served.jpg"
    assert row["source_media_url"] == "https://cdn/served.jpg"


def test_mirror_row_rendition_backed_lineage_differs_from_delivery_url(monkeypatch):
    _hash_flag_on(monkeypatch)
    draft = _mirror_draft(creative_public_url="https://cdn/rend.jpg",
                          source_media_content_hash="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    row = rcm.collect_real_drafts("pierce_ig", _FakeStore([draft]))[0]
    assert row["image_url"] == "https://cdn/rend.jpg"
    assert row["source_media_content_hash"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert "source_media_url" not in row  # rendition URL never copied as source


def test_mirror_row_omits_hash_when_feature_off_by_default():
    # OFF default: even a Drive-stamped draft must not emit the column, so the
    # direct delete-then-insert callers never send an unknown pre-migration
    # column while the feature is disabled.
    draft = _mirror_draft(source_media_content_hash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    row = rcm.collect_real_drafts("pierce_ig", _FakeStore([draft]))[0]
    assert "source_media_content_hash" not in row


def test_mirror_omits_hash_when_draft_carries_none():
    draft = _mirror_draft()
    row = rcm.collect_real_drafts("pierce_ig", _FakeStore([draft]))[0]
    assert "source_media_content_hash" not in row


def test_source_hash_survives_pending_store_round_trip(tmp_path):
    store = PendingStore(str(tmp_path / "lineage.db"))
    draft = _mirror_draft(source_media_content_hash="dddddddddddddddddddddddddddddddd")
    store.put(draft)
    restored = PendingStore(str(tmp_path / "lineage.db")).get(draft.draft_id)
    assert restored.source_media_content_hash == draft.source_media_content_hash


def test_missing_hash_column_preflight_prevents_month_delete(monkeypatch):
    from agent import config
    monkeypatch.setattr(config, "source_media_content_hash_enabled", lambda: True,
                        raising=False)

    class Store(_FakeStore):
        def ensure_logical_post_id(self, *_a, **_k):
            raise AssertionError("logical identity should be OFF")

    class Calendar:
        deleted = 0

        def source_media_content_hash_schema_ready(self):
            return False

        def delete_month(self, *_a, **_k):
            self.deleted += 1

    draft = _mirror_draft(source_media_content_hash="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
    calendar = Calendar()
    with pytest.raises(ValueError, match="not confirmed ready"):
        rcm.mirror_to_supabase("pierce_ig", Store([draft]), calendar)
    assert calendar.deleted == 0


def test_assert_helpers_fail_closed():
    assert builder.assert_tenant({"id": "a", "gym_id": "pierce"}, "pierce") is True
    assert builder.assert_tenant({"id": "a", "gym_id": "other"}, "pierce") is False
    assert builder.assert_tenant({"id": "a"}, "pierce") is False
    assert builder.assert_source({"id": "a", "source_id": ""}, "pierce", None) is False
