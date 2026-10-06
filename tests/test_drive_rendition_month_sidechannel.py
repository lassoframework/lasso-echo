"""Regression coverage for HEIC Drive rendition proof at the client-month writer.

The rendition receipt is deliberately draft-owned until the prepared calendar
writer consumes it.  This test follows the real Drive builder, PendingStore,
mirror collector, and client-month insert helper without a network or a real
calendar database.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run as cmr  # noqa: E402
from agent import gym_media_builder as builder  # noqa: E402
from agent import real_calendar_mirror as mirror  # noqa: E402
from agent.store import PendingStore  # noqa: E402
from tests.gym_media_fakes import FakeDrive, FakeMediaStore, make_asset  # noqa: E402


class _Account:
    key = "pierce_ig"
    platform = "instagram"


class _CalendarStore:
    def __init__(self):
        self.calls = []

    def insert_rows(self, gym_id, rows, **kwargs):
        self.calls.append((gym_id, rows, kwargs))
        return rows


def test_heic_drive_rendition_proof_survives_store_and_reaches_month_insert(
        monkeypatch, tmp_path):
    """A GBP proof map cannot replace the HEIC draft's URL keyed evidence."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *args, **kwargs: ("Grounded gym caption", []))
    monkeypatch.setattr("agent.gym_media_index.heic_to_jpeg",
                        lambda _src, dest: open(dest, "wb").write(b"jpeg") or dest)

    raw_url = "https://cdn.test/pierce/raw/heic-original.heic"
    delivered_url = "https://cdn.test/pierce/renditions/heic.jpg"
    evidence = {
        "operation": "render",
        "source_exact_url": raw_url,
        "delivered_exact_url": delivered_url,
        "source_fingerprint": "md5:raw",
        "delivered_fingerprint": "md5:rendition",
        "source_byte_length": 11,
        "delivered_byte_length": 9,
        "evidence_ref": "test:heic-render",
        "observed_by": "test",
        "rendered_by": "test",
    }

    # The builder's real guarded branch hosts the raw file first, makes a fresh
    # rendition, and requires its proof callback before it returns a Draft.
    monkeypatch.setattr("agent.media_host.host_media", lambda *_args: raw_url)
    monkeypatch.setattr(builder, "rendition_evidence_for",
                        lambda _src, _out, source, delivered, _hash: (
                            dict(evidence) if (source, delivered) == (raw_url, delivered_url)
                            else None))

    def ensure_rendition(_asset, source, **kwargs):
        output = tmp_path / "fresh-heic-rendition.jpg"
        output.write_bytes(b"jpeg")
        assert kwargs["proof_fn"](source, output, delivered_url) is True
        return delivered_url, True

    monkeypatch.setattr("agent.gym_media_index.ensure_rendition", ensure_rendition)
    media_store = FakeMediaStore(assets=[make_asset(
        "heic-asset", gym_id="pierce", kind="photo", title="IMG.HEIC",
        mime="image/heic")])
    drive = FakeDrive(blobs={"heic-asset": b"raw-heic-bytes"})

    draft = builder.build_gym_media_draft(
        _Account(), "2026-10-14", "community", voice=object(), source=object(),
        store=media_store, drive=drive, library_dir=str(tmp_path))

    assert draft is not None
    assert draft.source_media_url == raw_url
    assert draft.creative_public_url == delivered_url
    assert draft.render_evidence == evidence

    # The runtime store and mirror each preserve the non-column side channel.
    pending = PendingStore(str(tmp_path / "pending.db"))
    pending.put(draft)
    rehydrated = pending.get(draft.draft_id)
    assert rehydrated is not None and rehydrated.render_evidence == evidence
    mirrored_evidence = {}
    mirrored_rows = mirror.collect_real_drafts(
        "pierce_ig", pending, render_evidence_out=mirrored_evidence)
    assert mirrored_evidence == {delivered_url: evidence}

    # GBP has its own distinct delivery URL. Merge must retain the HEIC claim,
    # then the client-month writer must receive it by its delivered URL.
    gbp_url = "https://cdn.test/pierce/gbp/crop.jpg"
    gbp_evidence = {"source_exact_url": raw_url, "delivered_exact_url": gbp_url}
    merged = cmr._merge_render_evidence_by_url(
        cmr._render_evidence_by_url([rehydrated]), {gbp_url: gbp_evidence})
    assert merged == {delivered_url: evidence, gbp_url: gbp_evidence}

    calendar = _CalendarStore()
    cmr._insert_rows_with_poster_evidence(
        calendar.insert_rows, "pierce", mirrored_rows, {}, merged)
    assert len(calendar.calls) == 1
    gym_id, rows, kwargs = calendar.calls[0]
    assert gym_id == "pierce"
    assert rows == mirrored_rows
    assert kwargs["render_evidence_by_url"] == merged
    assert kwargs["render_evidence_by_url"][delivered_url] == evidence
