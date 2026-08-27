"""The tap is untouched: every staged podcast row lands PENDING, a podcast row
passes publish_guard like any row, and the podcast category still cannot exceed
25% of a month (regression)."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_library_builder as builder
from agent import publish_guard, real_month_planner as rmp
from agent.drafter import DraftStatus
from tests.podcast_fakes import (FakeDrive, FakeStore, FakeZernio,
                                 NOTES_DOC_TEXT, make_asset)

ACCT = SimpleNamespace(key="lasso_ig", platform="instagram")


def _probe_ok(path):
    return {"duration_sec": 42.0, "width": 1080, "height": 1920}


def _build(store=None, drive=None, zc=None, day="2026-09-03"):
    store = store or FakeStore([make_asset()])
    drive = drive or FakeDrive(docs={"doc140": NOTES_DOC_TEXT})
    zc = zc or FakeZernio()
    d = builder.build_podcast_clip_draft(ACCT, day, store=store, drive=drive,
                                         zernio_client=zc, probe_fn=_probe_ok)
    return d, store, zc


def test_staged_draft_is_pending():
    draft, store, zc = _build()
    assert draft is not None
    assert draft.status == DraftStatus.PENDING       # the human tap is untouched
    assert draft.category == "podcast"
    assert draft.caption
    assert draft.creative_public_url.startswith("https://cdn.fake/")
    assert zc.uploads, "the clip was uploaded through the presigned flow"
    # usage stamped ONLY at stage time
    assert store.assets["clip140s1"]["used_count"] == 1
    # the probe wrote back real data
    assert store.assets["clip140s1"]["aspect"] == "9:16"
    assert store.assets["clip140s1"]["postable"] is True
    # grounded provenance travels with the draft
    assert any(f.startswith("drive_doc:") for f in draft.source_fragments)


def test_staged_calendar_rows_all_land_pending():
    draft, _, _ = _build()
    rows = rmp.to_calendar_rows([draft], "lasso")
    assert rows, "the draft maps to calendar rows"
    for row in rows:
        assert row["status"] == "pending"
        assert row["pillar"] == "podcast"
        assert row["gym_id"] == "lasso"
        assert row["image_url"].startswith("https://cdn.fake/")


def test_upload_not_ready_means_no_stage():
    store = FakeStore([make_asset()])
    draft, store, _ = _build(store=store, zc=FakeZernio(ready=False))
    assert draft is None
    assert store.assets["clip140s1"]["used_count"] == 0  # nothing stamped


def test_failed_gate_marks_asset_and_tries_next_clip():
    def probe_wide(path):
        return {"duration_sec": 42.0, "width": 1920, "height": 1080}  # 16:9
    store = FakeStore([
        make_asset(fid="wide", episode=140, clip_index=1,
                   title="GMMS-140-S1.mp4", duration=None, width=None,
                   height=None, aspect=None, postable=True),
    ])
    d = builder.build_podcast_clip_draft(
        ACCT, "2026-09-03", store=store,
        drive=FakeDrive(docs={"doc140": NOTES_DOC_TEXT}),
        zernio_client=FakeZernio(), probe_fn=probe_wide)
    assert d is None
    assert store.assets["wide"]["postable"] is False   # written back, fail closed
    assert store.assets["wide"]["used_count"] == 0


# ---- publish-time recheck: a podcast row flows through publish_guard ----------

def test_podcast_payload_passes_publish_guard_like_any_row():
    draft, _, _ = _build()
    payload = publish_guard.PublishPayload(
        row_id="r1", gym_id="lasso", platform="instagram",
        caption=draft.caption, category="podcast", media_ready=True)
    assert publish_guard.check(payload, handles_fn=lambda g: []) == []


def test_podcast_payload_blocked_when_media_not_ready():
    draft, _, _ = _build()
    payload = publish_guard.PublishPayload(
        row_id="r1", gym_id="lasso", platform="instagram",
        caption=draft.caption, category="podcast", media_ready=False)
    assert publish_guard.check(payload, handles_fn=lambda g: []) == [
        publish_guard.MEDIA_MISSING]


def test_podcast_payload_blocked_when_caption_emptied():
    payload = publish_guard.PublishPayload(
        row_id="r1", gym_id="lasso", platform="instagram",
        caption="", category="podcast", media_ready=True)
    assert publish_guard.EMPTY_CAPTION in publish_guard.check(
        payload, handles_fn=lambda g: [])


# ---- regression: podcast can still never exceed 25% of a month ----------------

def test_podcast_over_25_percent_is_remediated():
    rows = []
    for i in range(20):
        pillar = "podcast" if i < 8 else ("b2b" if i % 2 else "platform")
        rows.append({"gym_id": "lasso", "post_date": f"2026-09-{i + 1:02d}",
                     "pillar": pillar, "category": pillar, "format": "feed",
                     "caption": f"Real caption number {i} with enough substance "
                                f"to stand alone. Sign up today.",
                     "status": "pending"})
    assert sum(r["pillar"] == "podcast" for r in rows) / len(rows) > 0.25
    rmp._remediate(rows, [])
    n = len(rows)
    pod = sum(r["pillar"] == "podcast" for r in rows)
    assert pod / n <= 0.25


def test_calendar_grade_flags_podcast_over_25_percent():
    from agent.calendar_grade import grade_month
    rows = []
    for i in range(12):
        pillar = "podcast" if i < 6 else ("b2b" if i % 2 else "platform")
        rows.append({"gym_id": "lasso", "post_date": f"2026-09-{i + 1:02d}",
                     "pillar": pillar, "format": "feed",
                     "caption": f"Unique caption {i} carrying a concrete point "
                                f"about gym growth. Sign up today.",
                     "status": "pending"})
    grade = grade_month(rows, profile="B2B")
    assert any(leg == "content_mix" and "podcast" in str(subject)
               for leg, subject, _ in grade.defects)
