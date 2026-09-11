"""Independent audit of PR #98 (2026-09-10), the MAJOR + MINOR fixes:

  2c  a partial Drive pool must never turn repeats into EMPTY days
  D1  ONE video-extension definition (agent/media_types.py) at every site; an .mkv
      never reaches the wire typed "image" (rendition to .mp4 or skipped)
  D3  a rebuild releases the Drive assets of the rows it deletes (safe to re-run)
  D2  the autofit lane never localizes a video
  D6  kind exhaustion is not a "pool empty" alert
  D7  the GBP mirror counts the Drive videos it skips
  +   the live posture (AGENT_FEED_AUTOFIT + AGENT_STORY_FORMAT ON) for a Drive video
  +   the Facebook leg through zernio_publisher.publish (page resolution), not a
      hand-built create_post

Fully OFFLINE.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, client_month_run as cmr, client_sources as cs  # noqa: E402
from agent import gym_media_builder as builder, gym_media_selector as sel  # noqa: E402
from agent import media_types as mt  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    for k in ("AGENT_VISION_GYMS", "GYM_DRIVE_STAGE", "GYM_DRIVE_CONNECT",
              "GYM_DRIVE_CONNECT_GYMS", "AGENT_HOSTING_ENABLED", "AGENT_FEED_AUTOFIT",
              "AGENT_STORY_FORMAT", "AGENT_STORY_SOURCE_MEDIA"):
        monkeypatch.delenv(k, raising=False)
    client_content.clear_drive_pool_cache()
    yield
    client_content.clear_drive_pool_cache()


# ---- shared offline harness ---------------------------------------------------------
class _CalStore:
    """A calendar store with list_month, so the guard/release paths see existing rows."""

    def __init__(self, existing=()):
        self.existing = [dict(r) for r in existing]
        self.inserted = []
        self.deleted = []

    def list_month(self, base_key, month):
        return [dict(r) for r in self.existing
                if str(r.get("post_date", "")).startswith(month)]

    def delete_month(self, base_key, month, preserve_dates=()):
        keep = {str(d)[:10] for d in (preserve_dates or ())}
        before = len(self.existing)
        self.existing = [r for r in self.existing
                         if not (str(r.get("post_date", "")).startswith(month)
                                 and str(r.get("status") or "pending") in ("pending", "draft", "queued")
                                 and str(r.get("post_date", ""))[:10] not in keep)]
        self.deleted.append((base_key, month))
        return before - len(self.existing)

    def insert_rows(self, base_key, rows):
        self.inserted.extend(rows)
        self.existing.extend(dict(r, id=f"new{len(self.existing)}") for r in rows)
        return rows


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit", hashtags=["#GetFit"],
                    ctas=["Save this post."])


def _account():
    return Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID")


def _lib(tmp_path, n=5):
    import json
    lib = tmp_path / "gritx_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
        (lib / f"photo_{i:02d}.json").write_text(
            json.dumps({"public_url": f"https://gritx.media/photo_{i:02d}.jpg"}))
    return str(lib)


def _stale_ledger(monkeypatch, n=5):
    """Every local photo served INSIDE the 14-day window of EVERY day of an August
    build (dated after the span, the polluted-ledger shape the real incidents had)."""
    served = [{"key": f"photo_{i:02d}.jpg", "date": f"2026-09-{1 + i:02d}",
               "pillar": "service"} for i in range(n)]
    monkeypatch.setattr(client_content.rotation, "load_served",
                        lambda: {"gritx_ig": list(served)})


def _sources():
    cs.add_source("gritx_ig", "offer", "21 day kickstart for busy parents",
                  "client social intake")
    cs.add_source("gritx_ig", "service", "Small group training", "client social intake")
    cs.add_source("gritx_ig", "about", "Who we help: parents in their 40s",
                  "client social intake")


def _arm(monkeypatch, store, drive, host=None):
    monkeypatch.setenv("GYM_DRIVE_STAGE", "true")
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "true")
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient", lambda *a, **k: drive)
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda *a, **k: pytest.fail("vision must not run"))
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption about the class", []))
    monkeypatch.setattr("agent.media_host.host_media",
                        host or (lambda path, gym: f"https://cdn.fake/{os.path.basename(path)}"))
    monkeypatch.setattr("agent.gym_media_index.probe_video",
                        lambda p: {"duration_sec": 20.0, "width": 1080, "height": 1920,
                                   "codec": "h264"})
    monkeypatch.setattr(builder, "video_poster_url",
                        lambda path, work, tenant: "https://cdn.fake/poster.jpg")


def _feeds(store):
    return [r for r in store.inserted if r["format"] == "feed" and r["account"] == "instagram"]


# ---- 2c: never an empty day ---------------------------------------------------------
def test_partial_pool_fills_every_day_three_drive_then_spaced_repeats(monkeypatch,
                                                                       tmp_path):
    """3 pickable assets, 10 days, a stale 5-photo library. Round 2 filled all 10
    days (3 Drive + 7 repeats) and thereby repealed Blake's standing rule (N photos ->
    at most N feeds, never pad). Round 3 (audit R-A1): the media cap binds the whole
    build. max_feed_days = 5 photos; the 3 Drive feeds count against it, so the
    fallback may add at most 2 spaced repeats; the other 5 deferred days stay
    UNCOVERED exactly as they would have before the PR."""
    _sources()
    _stale_ledger(monkeypatch)
    store = FakeMediaStore(assets=[make_asset(f"a{i}", gym_id="gritx", title=f"t{i}.jpg")
                                   for i in range(3)])
    _arm(monkeypatch, store, FakeDrive())
    cal = _CalStore()
    logs = []
    out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=10, voice=_voice(),
                                 library_path=_lib(tmp_path), store=cal, banned_words=(),
                                 logger=logs.append)
    assert out["ok"] is True
    feeds = _feeds(cal)
    drive = [r for r in feeds if r.get("source_media_asset_id")]
    repeats = [r for r in feeds if not r.get("source_media_asset_id")]
    assert len(drive) == 3 and len(repeats) == 2, "cap = 5 photos -> 3 Drive + 2 repeats"
    assert len({r["post_date"] for r in feeds}) == 5
    assert sum("placed a spaced repeat" in m for m in logs) == 2
    assert any("filling 2 of 7 deferred day(s)" in m for m in logs), logs
    assert len({r["image_url"] for r in repeats}) == 2
    # the truthful digest names the Drive pool, and the small-library alert stays quiet
    # (5 photos vs 5 covered days is not "smaller than the book")
    assert any("Drive pool ran short; 2 day(s)" in m for m in logs)


def test_two_photos_one_drive_asset_never_pads_past_the_cap(monkeypatch, tmp_path):
    """Blake's rule verbatim: 2 photos + 1 Drive asset on a 30-day span -> 2 feeds
    (1 Drive + 1 repeat), 28 days uncovered. Pre-PR this gym got 2 feeds; it must
    not get 30 now."""
    _sources()
    _stale_ledger(monkeypatch, n=2)
    store = FakeMediaStore(assets=[make_asset("only", gym_id="gritx", title="t.jpg")])
    _arm(monkeypatch, store, FakeDrive())
    cal = _CalStore()
    logs = []
    out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=30, voice=_voice(),
                                 library_path=_lib(tmp_path, n=2), store=cal, banned_words=(),
                                 logger=logs.append)
    assert out["ok"] is True
    feeds = _feeds(cal)
    drive = [r for r in feeds if r.get("source_media_asset_id")]
    repeats = [r for r in feeds if not r.get("source_media_asset_id")]
    assert len(drive) == 1
    assert len(repeats) <= 2 and len(feeds) == 2, f"{len(feeds)} feeds on a 2-photo gym"
    assert len({r["post_date"] for r in feeds}) == 2
    assert any("stay uncovered under the media cap" in m for m in logs), logs


def test_tough_temple_like_pool_yields_zero_repeats(monkeypatch, tmp_path):
    """63 pickable assets, a 30-day span, a stale local library: every day is a Drive
    post and NOT ONE local still repeats."""
    _sources()
    _stale_ledger(monkeypatch)
    store = FakeMediaStore(assets=[
        make_asset(f"a{i:02d}", gym_id="gritx", kind="video" if i % 2 else "photo",
                   title=f"t{i:02d}.{'mp4' if i % 2 else 'jpg'}") for i in range(63)])
    _arm(monkeypatch, store, FakeDrive())
    cal = _CalStore()
    logs = []
    out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=30, voice=_voice(),
                                 library_path=_lib(tmp_path), store=cal, banned_words=(),
                                 logger=logs.append)
    assert out["ok"] is True
    feeds = _feeds(cal)
    assert len({r["post_date"] for r in feeds}) == 30
    assert all(r.get("source_media_asset_id") for r in feeds), "a local still repeated"
    assert len({r["source_media_asset_id"] for r in feeds}) == 30
    assert not any("placed a spaced repeat" in m for m in logs)
    videos = [r for r in feeds if mt.is_video_url(r["image_url"])]
    assert 0.35 <= len(videos) / 30 <= 0.55, f"{len(videos)}/30 video days"


# ---- D3: a rebuild releases the assets of the rows it deletes -----------------------
def _pending_drive_rows(asset_ids, start=date(2026, 8, 1)):
    rows = []
    for i, aid in enumerate(asset_ids):
        pd = (start + timedelta(days=i)).isoformat()
        for acct, fmt in (("instagram", "feed"), ("facebook", "feed"), ("instagram", "story")):
            rows.append({"id": f"old_{aid}_{acct}_{fmt}", "gym_id": "gritx", "post_date": pd,
                         "account": acct, "format": fmt, "status": "pending",
                         "caption": "c", "image_url": f"https://cdn.fake/{aid}.jpg",
                         "source_media_asset_id": aid})
    return rows


def test_rebuild_releases_wipeable_drive_assets_and_repicks_them(monkeypatch, tmp_path):
    """The first build staged a1..a3 (stamped, used this month). A second build in the
    same month used to find the pool EMPTY (every asset "used this month") and fall back
    to repeats while the assets cooled down for rows that no longer existed."""
    _sources()
    store = FakeMediaStore(assets=[make_asset(f"a{i}", gym_id="gritx", title=f"t{i}.jpg")
                                   for i in range(1, 4)])
    _arm(monkeypatch, store, FakeDrive())
    for i in range(1, 4):                      # what build #1 did
        sel.stamp_use(store.get_asset(f"a{i}"), "gritx", f"2026-08-0{i}", store=store)
    assert all(store.assets[f"a{i}"]["used_count"] == 1 for i in range(1, 4))
    cal = _CalStore(existing=_pending_drive_rows(["a1", "a2", "a3"]))
    logs = []
    out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=3, voice=_voice(),
                                 library_path=_lib(tmp_path, n=0), store=cal, banned_words=(),
                                 logger=logs.append)
    assert out["ok"] is True and out["inserted"] > 0
    assert any("released 3 Drive asset(s)" in m for m in logs), logs
    feeds = _feeds(cal)
    assert sorted(r["source_media_asset_id"] for r in feeds) == ["a1", "a2", "a3"], \
        "the second build must see the same pool the first one did"
    # stamped exactly once for the rows that now exist, never 2
    assert all(store.assets[f"a{i}"]["used_count"] == 1 for i in range(1, 4))


def test_a_build_that_writes_nothing_restamps_the_released_assets(monkeypatch, tmp_path):
    """never-wipe-to-empty: no approved sources -> the Drive builder stages nothing ->
    _apply writes nothing -> the OLD rows survive, so their assets must be stamped
    again (the release is undone). Idempotent: run twice, same state."""
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="gritx")])
    _arm(monkeypatch, store, FakeDrive())
    sel.stamp_use(store.get_asset("a1"), "gritx", "2026-08-01", store=store)
    cal = _CalStore(existing=_pending_drive_rows(["a1"]))
    for _ in range(2):
        out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=1,
                                     voice=_voice(), library_path=_lib(tmp_path, n=0),
                                     store=cal, banned_words=())
        assert not out.get("inserted")
        assert store.assets["a1"]["used_count"] == 1, "old row still owns its asset"
        assert len(cal.existing) == 3, "nothing was deleted"


def test_rollback_new_drive_drafts_returns_unwritten_picks(monkeypatch):
    from agent.drafter import Draft, DraftStatus
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="gritx")])
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    sel.stamp_use(store.get_asset("a1"), "gritx", "2026-08-05", store=store)
    d = Draft(draft_id="x", account_key="gritx_ig", platform="instagram", caption="c",
              hashtags=[], creative_path="t.jpg", creative_public_url="u",
              scheduled_for="", status=DraftStatus.PENDING, day_key="2026-08-05",
              source_media_asset_id="a1")
    cmr._rollback_new_drive_drafts([d, d], lambda m: None)
    assert store.assets["a1"]["used_count"] == 0


# ---- D1: one video definition, no raw unpublishable container ----------------------
def test_every_site_uses_the_shared_video_extension_constant():
    from agent import (calendar_autopublish, client_month_run, gbp_mirror,
                       gym_media_index, media_localize, media_swap, portal_social, zernio)
    assert zernio._VIDEO_EXTS is mt.VIDEO_EXTS
    assert gym_media_index._VIDEO_EXTS is mt.VIDEO_EXTS
    assert portal_social._VIDEO_URL_EXTS is mt.VIDEO_EXTS
    assert media_swap._VIDEO_EXTS is mt.VIDEO_EXTS
    assert client_month_run._VIDEO_EXTS is mt.VIDEO_EXTS
    assert gbp_mirror._VIDEO_EXTS is mt.VIDEO_EXTS
    assert media_localize._VIDEO_EXTS is mt.VIDEO_EXTS
    assert calendar_autopublish.is_video_url is mt.is_video_url
    assert gym_media_index._is_publishable_video is mt.is_publishable_video
    # the wire types every recognised video as video, never image
    for ext in mt.VIDEO_EXTS:
        assert zernio._media_type(f"https://cdn/x{ext}?v=1") == "video", ext
        assert portal_social._media_kind(f"https://cdn/x{ext}") == "video", ext
        assert calendar_autopublish._normalize_feed_image(
            {"image_url": f"https://cdn/x{ext}"}, None, None) == {"image_url": f"https://cdn/x{ext}"}


def test_media_types_helpers():
    assert mt.ext_of("https://cdn/a/b/clip.MKV?x=1#f") == ".mkv"
    assert mt.ext_of("https://cdn/a.b/noext") == ""
    assert mt.is_video_url("clip.hevc") and not mt.is_video_url("still.jpg")
    assert mt.is_publishable_video("clip.mov") and not mt.is_publishable_video("clip.webm")
    assert set(mt.PUBLISHABLE_VIDEO_EXTS) < set(mt.VIDEO_EXTS)


def test_an_mkv_asset_is_transcoded_to_mp4_before_it_reaches_the_wire(monkeypatch, tmp_path):
    from agent import zernio
    from agent import gym_media_index as _idx
    store = FakeMediaStore(assets=[make_asset("m1", gym_id="gritx", kind="video",
                                              title="raw.mkv", mime="video/x-matroska")])
    _arm(monkeypatch, store, FakeDrive())

    def _fake_h264(src, dest, runner=None):
        with open(dest, "wb") as fh:
            fh.write(b"mp4" * 1024)
        return dest
    monkeypatch.setattr(_idx, "hevc_to_h264", _fake_h264)

    class _A:
        key = "gritx_ig"
        platform = "instagram"
    draft = builder.build_gym_media_draft(_A(), "2026-08-03", "faces", voice=object(),
                                          source=object(), store=store, drive=FakeDrive(),
                                          library_dir=str(tmp_path), now=NOW)
    assert draft is not None
    assert draft.creative_public_url.endswith(".mp4"), draft.creative_public_url
    assert store.assets["m1"]["rendition_url"] == draft.creative_public_url
    assert zernio._media_type(draft.creative_public_url) == "video"
    assert mt.is_publishable_video(draft.creative_public_url)


def test_an_mkv_asset_with_no_converter_is_skipped_with_a_reason(monkeypatch, tmp_path):
    from agent import gym_media_index as _idx
    store = FakeMediaStore(assets=[make_asset("m1", gym_id="gritx", kind="video",
                                              title="raw.mkv", mime="video/x-matroska")])
    _arm(monkeypatch, store, FakeDrive())

    def _no_ffmpeg(src, dest, runner=None):
        raise _idx.ConversionUnavailable("ffmpeg unavailable")
    monkeypatch.setattr(_idx, "hevc_to_h264", _no_ffmpeg)

    class _A:
        key = "gritx_ig"
        platform = "instagram"
    draft = builder.build_gym_media_draft(_A(), "2026-08-03", "faces", voice=object(),
                                          source=object(), store=store, drive=FakeDrive(),
                                          library_dir=str(tmp_path), now=NOW)
    assert draft is None, "a raw .mkv must never be staged"
    assert store.assets["m1"]["eligible"] is False
    assert store.assets["m1"]["reject_reason"] == _idx.REJECT_CONVERT_UNAVAILABLE


def test_swap_materialize_refuses_an_unrenditioned_webm(monkeypatch, tmp_path):
    from agent import media_swap as msw
    from agent import gym_media_index as _idx
    monkeypatch.setattr(_idx, "ensure_rendition", lambda *a, **k: (None, False))
    monkeypatch.setattr(_idx, "probe_video",
                        lambda p: {"duration_sec": 20.0, "width": 1080, "height": 1920})
    asset = make_asset("w1", gym_id="gritx", kind="video", title="clip.webm")
    cand = {"source": "drive", "kind": "video", "key": "w1", "asset": asset}
    out = msw._materialize("gritx", cand, str(tmp_path), drive=FakeDrive(),
                           media_store=FakeMediaStore(assets=[asset]))
    assert out is None


# ---- D6: kind exhaustion is not an empty pool ---------------------------------------
def test_pick_media_does_not_alert_when_only_the_preferred_kind_is_exhausted(monkeypatch):
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append(k) or True)
    monkeypatch.setattr(sel, "_publishing_gym", lambda base: True)
    store = FakeMediaStore(assets=[make_asset("v1", gym_id="tt", kind="video")])
    assert sel.pick_media("tt", "photo", store=store, now=NOW) is None
    assert fired == [], "videos remain: not an empty pool"
    assert sel.pick_media("tt", store=FakeMediaStore(assets=[]), now=NOW) is None
    assert fired == ["pool_empty:tt"]


# ---- D2: the autofit lane never localizes a video ------------------------------------
def test_autofit_short_circuits_a_video_before_localizing(monkeypatch):
    monkeypatch.setenv("AGENT_FEED_AUTOFIT", "true")
    from agent.drafter import Draft, DraftStatus
    monkeypatch.setattr("agent.media_localize.local_source_for",
                        lambda *a, **k: pytest.fail("a video must never be localized"))
    monkeypatch.setattr("agent.media_localize.local_copy",
                        lambda *a, **k: pytest.fail("a video must never be localized"))
    feed = Draft(draft_id="x", account_key="gritx_ig", platform="instagram", caption="c",
                 hashtags=[], creative_path="squat.mov",
                 creative_public_url="https://cdn/squat.mov", scheduled_for="",
                 status=DraftStatus.PENDING)
    cmr._maybe_format_feed(_account(), feed, "/nowhere", lambda m: None)
    assert feed.creative_public_url == "https://cdn/squat.mov"


# ---- D7: the GBP mirror names the Drive videos it skips ------------------------------
def test_gbp_mirror_counts_remote_video_skips(monkeypatch):
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    from agent import gbp_mirror
    from agent.drafter import Draft, DraftStatus
    monkeypatch.setattr("agent.config.gbp_mirror_active_for", lambda g: True)
    vid = Draft(draft_id="v", account_key="gritx_ig", platform="instagram", caption="c",
                hashtags=[], creative_path="squat.mov",
                creative_public_url="https://cdn/squat.mov", scheduled_for="",
                status=DraftStatus.PENDING, day_key="2026-08-03", draft_type="feed")
    monkeypatch.setattr(gbp_mirror, "eligible_drafts", lambda drafts: list(drafts))
    monkeypatch.setattr(gbp_mirror, "cropped_url",
                        lambda *a, **k: pytest.fail("no crop for a remote video"))
    logs = []
    ctx = {"account_gen_key": "gritx_ig", "cta_url": "https://x", "city": "Indy"}
    rows = gbp_mirror.rows_for("gritx", [vid], ctx=ctx, logger=logs.append)
    assert rows == []
    assert any("1 remote video(s) with no poster to crop" in m for m in logs), logs


# ---- the LIVE posture: AGENT_FEED_AUTOFIT + AGENT_STORY_FORMAT on -------------------
def test_drive_video_under_autofit_and_story_format_on(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FEED_AUTOFIT", "true")
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    _sources()
    store = FakeMediaStore(assets=[make_asset("v1", gym_id="gritx", kind="video",
                                              title="squat.mov", mime="video/quicktime")])
    drive = FakeDrive(blobs={"v1": b"mov" * 1024})
    _arm(monkeypatch, store, drive)
    monkeypatch.setattr("agent.media_localize.local_copy",
                        lambda *a, **k: pytest.fail("a video must never be localized"))
    monkeypatch.setattr("agent.media_host.download_bytes", lambda url: b"movbytes" * 64)
    made = []

    def _story_video(video_path, caption, gym_name, library_path, **k):
        made.append((os.path.splitext(video_path)[1], caption))
        out = tmp_path / "story_burn.mp4"
        out.write_bytes(b"x" * 64)
        return str(out)
    monkeypatch.setattr("agent.story_image.get_or_make_story_video", _story_video)
    monkeypatch.setattr("agent.story_image.get_or_make_story_image",
                        lambda *a, **k: pytest.fail("a video story is not a still card"))
    vday = next(d for d in (date(2026, 8, 1) + timedelta(days=i) for i in range(12))
                if builder.is_video_slot(d.isoformat()))
    extra = cmr.append_gym_drive_drafts(_account(), "gritx", vday, 1, _voice(),
                                        log=lambda m: None, covered_days=set(),
                                        drive=drive, store=store, library_path=str(tmp_path))
    rows = cmr._to_rows("gritx", extra)
    feeds = [r for r in rows if r["format"] == "feed"]
    stories = [r for r in rows if r["format"] == "story"]
    assert len(feeds) == 2 and len(stories) == 1
    for r in feeds:
        assert r["image_url"].endswith("squat.mov"), "autofit must leave a video alone"
        assert r["thumbnail_url"] == "https://cdn.fake/poster.jpg"
    assert stories[0]["image_url"].endswith("story_burn.mp4")
    assert stories[0]["source_media_url"].endswith("squat.mov")
    assert "thumbnail_url" not in stories[0]
    assert made == [(".mov", "A grounded caption about the class")]


# ---- the FACEBOOK leg through zernio_publisher.publish ------------------------------
def test_facebook_video_row_publishes_through_the_real_publisher(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")
    from agent import calendar_autopublish as cap, zernio_publisher as zp
    row = {"id": "r-fb", "gym_id": "gritx", "account": "facebook", "post_date": "2026-08-03",
           "format": "feed", "status": "approved", "caption": "A grounded caption about the class",
           "image_url": "https://pub.r2.dev/echo/gritx/abc/squat.mov",
           "thumbnail_url": "https://cdn.fake/poster.jpg", "source_media_asset_id": "v1"}
    draft = cap._draft_for(row)
    draft.account_key = "gritx_fb"
    draft.platform = Platform.FACEBOOK_PAGE
    account = Account(key="gritx_fb", display_name="GritX FB", platform=Platform.FACEBOOK_PAGE,
                      token_env="T", target_id_env="TID")
    calls = {}

    class _Client:
        def list_accounts(self, profile_id):
            calls["profile"] = profile_id
            return {"accounts": [{"platform": "facebook", "_id": "fbacct"},
                                 {"platform": "instagram", "_id": "igacct"}]}

        def create_post(self, account_id, body, media_urls=None, scheduled_for=None,
                        page_id=None, platform=None, story=False):
            calls.update(account_id=account_id, body=body, media_urls=media_urls,
                         page_id=page_id, platform=platform, story=story)
            return {"_id": "post-1"}

    res = zp.publish(draft, account, client=_Client(),
                     profile_resolver=lambda k: "prof-gritx",
                     page_resolver=lambda k: "page-77")
    assert res.ok and res.mode == "published" and res.media_id == "post-1"
    assert calls["profile"] == "prof-gritx"
    assert calls["account_id"] == "fbacct" and calls["platform"] == "facebook"
    assert calls["page_id"] == "page-77" and calls["story"] is False
    assert calls["media_urls"] == [row["image_url"]]
    # and the client it goes through types that url as a video
    from agent import zernio
    assert zernio._media_type(calls["media_urls"][0]) == "video"


# =====================================================================================
# Round 3 (re-audit still B): R-A1 cap (above), R-D1 rendition budget, 3c/D3 residuals,
# the Drive-lane A+ gate, D1 residual.
# =====================================================================================
from agent import gym_media_index as _gmi  # noqa: E402


class _Proc:
    def __init__(self, rc=0):
        self.returncode = rc


def test_hevc_to_h264_is_budgeted_scaled_and_fast(tmp_path):
    """Audit R-D1 #2: preset veryfast, crf 23, long edge <= 1080, faststart, 180 s."""
    seen = {}

    def runner(args, **kw):
        seen["args"], seen["kw"] = args, kw
        out = args[-1]
        with open(out, "wb") as fh:
            fh.write(b"x")
        return _Proc(0)
    dest = tmp_path / "out.mp4"
    _gmi.hevc_to_h264(tmp_path / "in.mov", dest, runner=runner)
    a = seen["args"]
    assert a[a.index("-preset") + 1] == "veryfast" and a[a.index("-crf") + 1] == "23"
    assert "+faststart" in a and "libx264" in a
    vf = a[a.index("-vf") + 1]
    assert "min(1080,iw)" in vf and "force_original_aspect_ratio=decrease" in vf
    assert seen["kw"]["timeout"] == _gmi.RENDITION_TIMEOUT_SEC == 180


def test_hevc_to_h264_timeout_is_transient_not_unavailable(tmp_path):
    import subprocess

    def slow(args, **kw):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=kw.get("timeout"))
    with pytest.raises(_gmi.RenditionTimeout):
        _gmi.hevc_to_h264(tmp_path / "in.mov", tmp_path / "o.mp4", runner=slow, timeout=7)
    assert not issubclass(_gmi.RenditionTimeout, _gmi.ConversionUnavailable)


def test_needs_rendition_catches_hevc_in_an_mp4_container():
    """Audit R-D1 #1: an HEVC .mp4 was never probed (hint list was ('.mov',))."""
    mp4 = make_asset("v", gym_id="g", kind="video", title="clip.mp4", mime="video/mp4")
    assert _gmi.needs_rendition(mp4, {"codec": "hevc"}) is True
    assert _gmi.needs_rendition(mp4, {"codec": "h264"}) is False
    assert _gmi.needs_rendition(mp4, None) is False            # publishable container
    mkv = make_asset("v", gym_id="g", kind="video", title="clip.mkv")
    assert _gmi.needs_rendition(mkv, {"codec": "h264"}) is True
    heic = make_asset("p", gym_id="g", kind="photo", title="IMG.HEIC", mime="image/heic")
    assert _gmi.needs_rendition(heic) is True


def test_ensure_rendition_probes_every_video_and_persists_the_real_key(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://pub.r2.dev")
    asset = make_asset("v1", gym_id="gritx", kind="video", title="clip.mp4", mime="video/mp4")
    store = FakeMediaStore(assets=[asset])
    calls = []

    def fake_h264(src, dest, timeout=None):
        calls.append(timeout)
        with open(dest, "wb") as fh:
            fh.write(b"mp4")
        return dest
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"hevc-bytes")
    url, conv = _gmi.ensure_rendition(
        asset, src, store=store, hevc_fn=fake_h264,
        probe_fn=lambda p: {"codec": "hevc", "duration_sec": 10, "width": 1080, "height": 1920},
        host_fn=lambda path, gym: f"https://pub.r2.dev/echo/{gym}/abc123/{os.path.basename(path)}")
    assert conv is True and url.endswith(".mp4") and calls == [180]
    assert store.assets["v1"]["rendition_url"] == url
    assert store.assets["v1"]["rendition_key"] == f"echo/gritx/abc123/{os.path.basename(url)}", \
        "rendition_key must be the REAL R2 key host_media wrote, not the lookup key"


def test_ensure_rendition_respects_the_budget_and_never_encodes_past_it(tmp_path):
    asset = make_asset("v1", gym_id="gritx", kind="video", title="clip.mov")
    store = FakeMediaStore(assets=[asset])
    budget = _gmi.RenditionBudget(1)
    encoded = []

    def fake_h264(src, dest, timeout=None):
        encoded.append(1)
        with open(dest, "wb") as fh:
            fh.write(b"mp4")
        return dest
    info = {"codec": "hevc", "duration_sec": 10, "width": 1080, "height": 1920}
    url, _ = _gmi.ensure_rendition(asset, tmp_path / "a.mov", store=store, hevc_fn=fake_h264,
                                   probe_info=info, budget=budget,
                                   host_fn=lambda p, g: "https://cdn/a.mp4")
    assert url and budget.spent and encoded == [1]
    second = make_asset("v2", gym_id="gritx", kind="video", title="b.mov")
    with pytest.raises(_gmi.RenditionBudgetExhausted):
        _gmi.ensure_rendition(second, tmp_path / "b.mov", store=store, hevc_fn=fake_h264,
                              probe_info=info, budget=budget,
                              host_fn=lambda p, g: "https://cdn/b.mp4")
    assert encoded == [1], "a spent budget must not encode"
    # a video that needs no rendition costs nothing
    plain = make_asset("v3", gym_id="gritx", kind="video", title="c.mp4")
    assert _gmi.ensure_rendition(plain, tmp_path / "c.mp4", store=store, budget=budget,
                                 probe_info={"codec": "h264"}) == (None, False)


def test_builder_never_hosts_raw_hevc_when_the_budget_is_spent(monkeypatch, tmp_path):
    """The exact Tough Temple failure: 56 HEVC .mov clips, 0 renditioned. Budget spent
    -> the clip is skipped (rendition_missing, STILL eligible for the nightly pass),
    never staged raw because ".mov is a publishable container"."""
    store = FakeMediaStore(assets=[make_asset("hv", gym_id="gritx", kind="video",
                                              title="squat.mov", mime="video/quicktime")])
    _arm(monkeypatch, store, FakeDrive())
    monkeypatch.setattr("agent.gym_media_index.probe_video",
                        lambda p: {"duration_sec": 20.0, "width": 1080, "height": 1920,
                                   "codec": "hevc"})
    monkeypatch.setattr(_gmi, "hevc_to_h264",
                        lambda *a, **k: pytest.fail("a spent budget must not encode"))

    class _A:
        key = "gritx_ig"
        platform = "instagram"
    spent = _gmi.RenditionBudget(0)
    draft = builder.build_gym_media_draft(_A(), "2026-08-03", "faces", voice=object(),
                                          source=object(), store=store, drive=FakeDrive(),
                                          library_dir=str(tmp_path), now=NOW,
                                          rendition_budget=spent)
    assert draft is None, "raw HEVC must never become a row"
    a = store.assets["hv"]
    # a spent budget never even PICKS an unrenditioned video (nothing to transcode it
    # with), so the asset is untouched: still eligible, still unstamped, nothing hosted
    assert a["eligible"] is True and a["used_count"] == 0 and not a.get("rendition_url")


def test_builder_transcode_timeout_skips_the_clip_transiently(monkeypatch, tmp_path):
    store = FakeMediaStore(assets=[make_asset("hv", gym_id="gritx", kind="video",
                                              title="squat.mov")])
    _arm(monkeypatch, store, FakeDrive())
    monkeypatch.setattr("agent.gym_media_index.probe_video",
                        lambda p: {"duration_sec": 20.0, "width": 1080, "height": 1920,
                                   "codec": "hevc"})

    def slow(src, dest, timeout=None):
        raise _gmi.RenditionTimeout("ffmpeg ran past 180s")
    monkeypatch.setattr(_gmi, "hevc_to_h264", slow)

    class _A:
        key = "gritx_ig"
        platform = "instagram"
    draft = builder.build_gym_media_draft(_A(), "2026-08-03", "faces", voice=object(),
                                          source=object(), store=store, drive=FakeDrive(),
                                          library_dir=str(tmp_path), now=NOW,
                                          rendition_budget=_gmi.RenditionBudget(8))
    assert draft is None
    assert store.assets["hv"]["reject_reason"] == _gmi.REJECT_RENDITION_MISSING
    assert store.assets["hv"]["eligible"] is True


def test_builder_with_spent_budget_prefers_renditioned_videos_then_photos(monkeypatch,
                                                                          tmp_path):
    """Audit R-D1 #3: budget spent -> a video already carrying a rendition_url is
    picked over a never-used unrenditioned one; with no renditioned video left the
    slot falls back to a photo, without encoding anything."""
    store = FakeMediaStore(assets=[
        make_asset("raw1", gym_id="gritx", kind="video", title="raw1.mov"),
        make_asset("rend", gym_id="gritx", kind="video", title="rend.mov", used_count=0),
        make_asset("p1", gym_id="gritx", kind="photo", title="p.jpg")])
    store.assets["rend"]["rendition_url"] = "https://cdn.fake/rend.mp4"
    _arm(monkeypatch, store, FakeDrive())
    monkeypatch.setattr(_gmi, "hevc_to_h264",
                        lambda *a, **k: pytest.fail("a spent budget must not encode"))

    class _A:
        key = "gritx_ig"
        platform = "instagram"
    vday = next(d for d in (date(2026, 8, 1) + timedelta(days=i) for i in range(12))
                if builder.is_video_slot(d.isoformat()))
    spent = _gmi.RenditionBudget(0)
    first = builder.build_gym_media_draft(_A(), vday, "faces", voice=object(),
                                          source=object(), store=store, drive=FakeDrive(),
                                          library_dir=str(tmp_path), now=NOW,
                                          rendition_budget=spent)
    assert first.source_media_asset_id == "rend"
    assert first.creative_public_url == "https://cdn.fake/rend.mp4"
    second = builder.build_gym_media_draft(_A(), vday, "faces", voice=object(),
                                           source=object(), store=store, drive=FakeDrive(),
                                           library_dir=str(tmp_path), now=NOW,
                                           rendition_budget=spent, exclude_ids=("rend",))
    assert second.source_media_asset_id == "p1", "no renditioned video left -> a photo"
    assert store.assets["raw1"]["used_count"] == 0


def test_month_build_threads_one_budget_of_rendition_max_per_build(monkeypatch, tmp_path):
    monkeypatch.setenv("RENDITION_MAX_PER_BUILD", "2")
    _sources()
    _stale_ledger(monkeypatch)
    store = FakeMediaStore(assets=[make_asset(f"hv{i}", gym_id="gritx", kind="video",
                                              title=f"c{i}.mov") for i in range(5)])
    _arm(monkeypatch, store, FakeDrive())
    monkeypatch.setattr("agent.gym_media_index.probe_video",
                        lambda p: {"duration_sec": 20.0, "width": 1080, "height": 1920,
                                   "codec": "hevc"})
    encoded = []

    def fake_h264(src, dest, timeout=None):
        encoded.append(os.path.basename(str(src)))
        with open(dest, "wb") as fh:
            fh.write(b"mp4")
        return dest
    monkeypatch.setattr(_gmi, "hevc_to_h264", fake_h264)
    cal = _CalStore()
    out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=5, voice=_voice(),
                                 library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True
    assert len(encoded) == 2, f"RENDITION_MAX_PER_BUILD=2 but encoded {encoded}"
    drive = [r for r in _feeds(cal) if r.get("source_media_asset_id")]
    assert len(drive) == 2 and all(r["image_url"].endswith(".mp4") for r in drive)
    # the three clips the budget did not reach are untouched and still eligible for
    # the nightly pre-render pass; none was hosted raw
    rest = [a for a in store.assets.values() if not a.get("rendition_url")]
    assert len(rest) == 3 and all(a["eligible"] is True and a["used_count"] == 0
                                  for a in rest)
    # and the days the pool could not cover fell to spaced repeats under the cap
    assert len({r["post_date"] for r in _feeds(cal)}) == 5


def test_swap_materialize_is_bounded_and_never_returns_raw_hevc(monkeypatch, tmp_path):
    from agent import media_swap as msw
    monkeypatch.setattr(_gmi, "probe_video",
                        lambda p: {"duration_sec": 20.0, "width": 1080, "height": 1920,
                                   "codec": "hevc"})
    seen = {}

    def slow(src, dest, timeout=None):
        seen["timeout"] = timeout
        raise _gmi.RenditionTimeout("too slow")
    monkeypatch.setattr(_gmi, "hevc_to_h264", slow)
    asset = make_asset("hv", gym_id="gritx", kind="video", title="squat.mov")
    cand = {"source": "drive", "kind": "video", "key": "hv", "asset": asset}
    out = msw._materialize("gritx", cand, str(tmp_path), drive=FakeDrive(),
                           media_store=FakeMediaStore(assets=[asset]))
    assert out is None, "a timed-out transcode means next candidate, never raw HEVC"
    assert seen["timeout"] == msw.SWAP_TRANSCODE_TIMEOUT_SEC == 45


def test_sync_prerender_pass_renders_within_budget_and_prehosts_playable_clips(monkeypatch):
    from agent.jobs import sync_gym_media as job
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://pub.r2.dev")
    assets = [make_asset("h1", gym_id="gritx", kind="video", title="a.mov"),
              make_asset("h2", gym_id="gritx", kind="video", title="b.mov"),
              make_asset("h3", gym_id="gritx", kind="video", title="c.mov"),
              make_asset("a_ok", gym_id="gritx", kind="video", title="d.mp4"),
              make_asset("done", gym_id="gritx", kind="video", title="e.mov"),
              make_asset("unp", gym_id="gritx", kind="video", title="f.mov", eligible=None),
              make_asset("ph", gym_id="gritx", kind="photo")]
    assets[4]["rendition_url"] = "https://cdn/e.mp4"          # already rendered
    store = FakeMediaStore(assets=assets)
    encoded = []

    def fake_h264(src, dest, timeout=None):
        encoded.append(os.path.basename(str(src)))
        with open(dest, "wb") as fh:
            fh.write(b"mp4")
        return dest
    monkeypatch.setattr(_gmi, "hevc_to_h264", fake_h264)
    hosted = []

    def host(path, gym):
        hosted.append(os.path.basename(path))
        return f"https://pub.r2.dev/echo/{gym}/k/{os.path.basename(path)}"

    def probe(p):
        return {"duration_sec": 20.0, "width": 1080, "height": 1920,
                "codec": "h264" if str(p).endswith(".mp4") else "hevc"}
    merged = {a["id"]: dict(a) for a in store.assets.values()}
    rendered, prehosted, skipped = job._prerender_pass(
        "gritx", FakeDrive(), store, merged, set(merged), probe, lambda m: None,
        budget_n=2, host_fn=host)
    assert rendered == 2 and len(encoded) == 2, "RENDITION_MAX_PER_SYNC binds"
    assert prehosted == 1 and "d.mp4" in hosted, "a playable clip is hosted as-is once"
    assert store.assets["a_ok"]["rendition_url"].endswith("d.mp4")
    assert store.assets["a_ok"]["rendition_key"].startswith("echo/gritx/")
    assert not store.assets["unp"].get("rendition_url")
    assert not store.assets["h3"].get("rendition_url"), "third HEVC clip waits for tomorrow"
    # audit round 4 #5: the pass STOPS at budget spent; h3 is neither downloaded nor
    # probed (it is not even counted as skipped)
    assert skipped == 0
    assert "c.mov" not in hosted and "c.mov" not in encoded


def test_sync_source_threads_render_budget(monkeypatch):
    from agent.jobs import sync_gym_media as job
    monkeypatch.setenv("RENDITION_MAX_PER_SYNC", "3")
    monkeypatch.setattr(job, "_post_digest", lambda *a, **k: None)
    seen = {}

    def fake_pass(*a, **k):
        seen.update(k)
        return 0, 0, 0
    monkeypatch.setattr(job, "_prerender_pass", fake_pass)
    from tests.gym_media_fakes import make_source
    store = FakeMediaStore(sources=[make_source(gym_id="gritx")], assets=[])
    out = job.sync_source(make_source(gym_id="gritx"), drive=FakeDrive(), store=store,
                          probe_fn=lambda p: None, log=lambda m: None)
    assert out["ok"] and seen["budget_n"] == 3
    assert {"rendered", "prehosted", "render_skipped"} <= set(out)


# ---- D3 residual -------------------------------------------------------------------
def test_a_raise_between_release_and_apply_restores_the_released_stamps(monkeypatch,
                                                                         tmp_path):
    """Audit round 4 #2: the round-3 version of this test used ONE asset, so the
    build re-picked the very asset it had released on the same day and the leak was
    masked (it passed on the round-2 tree too). Here the pending row carries z_old
    and the pool also holds a_new (sorts first, so the build picks IT); after the
    mid-build raise: z_old must be stamped again (its row survived) and a_new must be
    unstamped (its draft never landed). Round 2 leaves z_old=0 and a_new=1."""
    _sources()
    store = FakeMediaStore(assets=[make_asset("z_old", gym_id="gritx", title="old.jpg"),
                                   make_asset("a_new", gym_id="gritx", title="new.jpg")])
    _arm(monkeypatch, store, FakeDrive())
    sel.stamp_use(store.get_asset("z_old"), "gritx", "2026-08-01", store=store)
    cal = _CalStore(existing=_pending_drive_rows(["z_old"]))

    def boom(*a, **k):
        raise RuntimeError("mid-build crash")
    # the ask-coverage lane sits between the picks and the apply and is not wrapped
    monkeypatch.setenv("ECHO_GYM_ASK_COVERAGE", "true")
    monkeypatch.setattr(cmr, "_approved_gym_ask", boom)
    with pytest.raises(RuntimeError):
        cmr.build_client_month(_account(), "gritx", "2026-08-01", days=1, voice=_voice(),
                               library_path=_lib(tmp_path, n=0), store=cal, banned_words=())
    assert store.assets["z_old"]["used_count"] == 1, "released stamp must be restored"
    assert store.assets["a_new"]["used_count"] == 0, "the unlanded pick must be rolled back"
    assert len(cal.existing) == 3


def test_delete_ok_but_insert_failed_rolls_back_this_builds_new_stamps(monkeypatch,
                                                                       tmp_path):
    _sources()
    _stale_ledger(monkeypatch)
    store = FakeMediaStore(assets=[make_asset("n1", gym_id="gritx"),
                                   make_asset("old", gym_id="gritx")])
    _arm(monkeypatch, store, FakeDrive())
    sel.stamp_use(store.get_asset("old"), "gritx", "2026-08-01", store=store)

    class _Broken(_CalStore):
        def insert_rows(self, base_key, rows):
            raise RuntimeError("insert failed")
    cal = _Broken(existing=_pending_drive_rows(["old"]))
    out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=1, voice=_voice(),
                                 library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is False and out.get("deleted", 0) > 0
    picked = [a for a in ("n1", "old") if store.assets[a]["used_count"]]
    assert picked == [], "an unlanded pick must not keep its stamp; deleted rows stay free"


# ---- the Drive lane runs the A+ gate --------------------------------------------------
def test_drive_lane_drops_a_caption_that_fails_the_a_plus_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    _sources()
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="gritx"),
                                   make_asset("a2", gym_id="gritx")])
    _arm(monkeypatch, store, FakeDrive())
    from agent import post_quality
    verdicts = iter([False, True])
    monkeypatch.setattr(post_quality, "is_a_plus",
                        lambda draft, banned, require_media=True: next(verdicts))
    logs = []
    extra = cmr.append_gym_drive_drafts(_account(), "gritx", date(2026, 8, 1), 2, _voice(),
                                        log=logs.append, covered_days=set(),
                                        drive=FakeDrive(), store=store,
                                        library_path=str(tmp_path), banned_words=("x",))
    feeds = [d for d in extra if not getattr(d, "is_story", False)]
    assert len(feeds) == 1, "the failing caption must be dropped"
    assert any("failed the A+/banned-word gate" in m for m in logs)
    counts = sorted(store.assets[a]["used_count"] for a in ("a1", "a2"))
    assert counts == [0, 1], "the dropped draft's asset returns to the pool"


def test_drive_lane_banned_word_gate_without_sb7(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_SB7_ENABLED", raising=False)
    _sources()
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="gritx")])
    _arm(monkeypatch, store, FakeDrive())
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("Join our BOOTCAMP today", []))
    extra = cmr.append_gym_drive_drafts(_account(), "gritx", date(2026, 8, 1), 1, _voice(),
                                        log=lambda m: None, covered_days=set(),
                                        drive=FakeDrive(), store=store,
                                        library_path=str(tmp_path),
                                        banned_words=("bootcamp",))
    assert extra == [] and store.assets["a1"]["used_count"] == 0


def test_drive_lane_retries_a_failed_caption_once_on_the_same_asset(monkeypatch, tmp_path):
    """Audit round 4 #3: a caption that fails A+ no longer costs the day its video.
    One fresh caption on the SAME asset; the second verdict keeps it."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    _sources()
    store = FakeMediaStore(assets=[make_asset("v1", gym_id="gritx", kind="video",
                                              title="clip.mp4")])
    _arm(monkeypatch, store, FakeDrive())
    captions = iter(["Weak first try", "A grounded caption about the class"])
    calls = []
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: (calls.append(k.get("avoid_openings")) or
                                         (next(captions), [])))
    from agent import post_quality
    verdicts = iter([False, True])
    monkeypatch.setattr(post_quality, "is_a_plus",
                        lambda draft, banned, require_media=True: next(verdicts))
    logs = []
    extra = cmr.append_gym_drive_drafts(_account(), "gritx", date(2026, 8, 1), 1, _voice(),
                                        log=logs.append, covered_days=set(),
                                        drive=FakeDrive(), store=store,
                                        library_path=str(tmp_path), banned_words=("x",))
    feeds = [d for d in extra if not getattr(d, "is_story", False)]
    assert len(feeds) == 1 and feeds[0].source_media_asset_id == "v1"
    assert feeds[0].caption == "A grounded caption about the class"
    assert len(calls) == 2 and calls[1], "the retry carries the failed opening to avoid"
    assert store.assets["v1"]["used_count"] == 1
    assert any("retried once with a fresh caption" in m for m in logs)


def test_drive_lane_drops_after_the_second_failed_caption(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    _sources()
    store = FakeMediaStore(assets=[make_asset("v1", gym_id="gritx", kind="video",
                                              title="clip.mp4")])
    _arm(monkeypatch, store, FakeDrive())
    captions = iter(["first", "second"])
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: (next(captions), []))
    from agent import post_quality
    monkeypatch.setattr(post_quality, "is_a_plus", lambda *a, **k: False)
    logs = []
    extra = cmr.append_gym_drive_drafts(_account(), "gritx", date(2026, 8, 1), 1, _voice(),
                                        log=logs.append, covered_days=set(),
                                        drive=FakeDrive(), store=store,
                                        library_path=str(tmp_path), banned_words=("x",))
    assert extra == []
    assert store.assets["v1"]["used_count"] == 0, "rolled back only after the retry"
    assert any("failed the A+/banned-word gate twice" in m for m in logs)


def test_small_library_alert_ignores_drive_covered_days(monkeypatch, tmp_path):
    """Audit round 4 #4: a 5-still gym whose month is mostly Drive video is not a
    'small library'. The digest compares the library against Lane A days + fallback
    fills only."""
    _sources()
    _stale_ledger(monkeypatch)
    store = FakeMediaStore(assets=[make_asset(f"a{i}", gym_id="gritx", title=f"t{i}.jpg")
                                   for i in range(3)])
    _arm(monkeypatch, store, FakeDrive())
    fired = []
    from agent import media_guard
    monkeypatch.setattr(media_guard, "alert_small_library",
                        lambda base, day, log=None: fired.append(day))
    cal = _CalStore()
    out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=10, voice=_voice(),
                                 library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True
    feeds = _feeds(cal)
    assert len([r for r in feeds if not r.get("source_media_asset_id")]) == 2   # fills
    assert fired == [], "5 stills covering 2 days is not a small library"
    # the helper's own comparison: the SAME fill with 99 Lane A days behind it alerts
    logs = []
    cal2 = _CalStore()
    filled = cmr._fill_uncovered_days(
        _account(), "gritx", _voice(), _lib(tmp_path), (), logs.append,
        deferred_days={"2026-08-20"}, covered_days=set(), locked_keys=set(),
        used_keys=set(), drafts=[], store=cal2, start=date(2026, 8, 20), days=1,
        max_fill=1, local_days=99)
    assert filled == 1 and fired == ["2026-08-20"]


# =====================================================================================
# Round 5: MAJOR 1 (video beats claimed before Lane A), MAJOR 2 (sync_source resolves
# a stale gym_id), minors.
# =====================================================================================
def _tt_pool(n_videos=57, n_photos=6, renditioned=True):
    assets = []
    for i in range(n_videos):
        a = make_asset(f"v{i:02d}", gym_id="gritx", kind="video", title=f"clip{i:02d}.mp4",
                       mime="video/mp4")
        if renditioned:
            a["rendition_url"] = f"https://cdn.fake/rend/clip{i:02d}.mp4"
        assets.append(a)
    for i in range(n_photos):
        assets.append(make_asset(f"p{i:02d}", gym_id="gritx", kind="photo",
                                 title=f"team{i:02d}.jpg"))
    return assets


def _video_beats(start, days, slot=0):
    return sum(1 for i in range(days)
               if builder.is_video_slot((start + timedelta(days=i)).isoformat(), slot))


def test_video_beats_are_claimed_by_drive_before_lane_a_with_fresh_stills(monkeypatch,
                                                                          tmp_path):
    """Tough Temple's rebuild: 95 FRESH local stills, 57 renditioned Drive videos, 6
    Drive photos, 20 days at 1x. Before round 5 Lane A took every day (20 stills, 0
    videos). Now every video beat is a Drive video, every photo beat a fresh still:
    0 repeats, 0 uncovered, and each video day carries its FB mirror + story."""
    _sources()
    store = FakeMediaStore(assets=_tt_pool())
    _arm(monkeypatch, store, FakeDrive())
    start = date(2026, 8, 1)
    cal = _CalStore()
    logs = []
    out = cmr.build_client_month(_account(), "gritx", start.isoformat(), days=20,
                                 voice=_voice(), library_path=_lib(tmp_path, n=95),
                                 store=cal, banned_words=(), logger=logs.append)
    assert out["ok"] is True
    feeds = _feeds(cal)
    beats = _video_beats(start, 20)
    assert 8 <= beats <= 10                                   # the 5/11 pattern over 20 days
    videos = [r for r in feeds if mt.is_video_url(r["image_url"])]
    stills = [r for r in feeds if not r.get("source_media_asset_id")]
    assert len(videos) == beats, f"{len(videos)} video days for {beats} beats"
    assert len(stills) == 20 - beats and len(feeds) == 20, "0 uncovered"
    assert len({r["post_date"] for r in feeds}) == 20, "no double placement"
    assert len({r["image_url"] for r in feeds}) == 20, "0 repeats"
    assert all(r.get("source_media_asset_id", "").startswith("v") for r in videos)
    assert any("video pre-pass claimed" in m for m in logs)
    rows = cal.inserted
    for v in videos:
        d = v["post_date"]
        assert any(r["post_date"] == d and r["format"] == "feed" and r["account"] == "facebook"
                   and r["image_url"] == v["image_url"] for r in rows), "FB mirror"
        assert any(r["post_date"] == d and r["format"] == "story" for r in rows), "story"
    # one feed per (date, account): the day-shape assertion inside _apply also held
    assert len({(r["post_date"], r["account"]) for r in rows if r["format"] == "feed"}) == 40


def test_video_beats_then_drive_photos_when_no_still_is_fresh(monkeypatch, tmp_path):
    """Same pool, every local still stale: video beats -> Drive videos; the deferred
    photo beats -> the 6 Drive photos first, then videos again. ~14 video + 6 Drive
    photo days, 0 repeats, 0 uncovered."""
    _sources()
    _stale_ledger(monkeypatch, n=95)
    store = FakeMediaStore(assets=_tt_pool())
    _arm(monkeypatch, store, FakeDrive())
    start = date(2026, 8, 1)
    cal = _CalStore()
    out = cmr.build_client_month(_account(), "gritx", start.isoformat(), days=20,
                                 voice=_voice(), library_path=_lib(tmp_path, n=95),
                                 store=cal, banned_words=())
    assert out["ok"] is True
    feeds = _feeds(cal)
    assert len(feeds) == 20 and len({r["post_date"] for r in feeds}) == 20
    videos = [r for r in feeds if mt.is_video_url(r["image_url"])]
    photos = [r for r in feeds if r.get("source_media_asset_id", "").startswith("p")]
    assert len(photos) == 6 and len(videos) == 14
    assert not any(not r.get("source_media_asset_id") for r in feeds), "0 repeats"
    assert len({r["source_media_asset_id"] for r in feeds}) == 20


def test_no_drive_pool_keeps_lane_a_unchanged(monkeypatch, tmp_path):
    _sources()
    store = FakeMediaStore(assets=[])
    _arm(monkeypatch, store, FakeDrive())
    calls = []
    real = cmr.append_gym_drive_drafts

    def spy(*a, **k):
        calls.append(k.get("video_beats_only"))
        return real(*a, **k)
    monkeypatch.setattr(cmr, "append_gym_drive_drafts", spy)
    cal = _CalStore()
    out = cmr.build_client_month(_account(), "gritx", "2026-08-01", days=20, voice=_voice(),
                                 library_path=_lib(tmp_path, n=95), store=cal, banned_words=())
    assert out["ok"] is True
    feeds = _feeds(cal)
    assert len(feeds) == 20 and not any(r.get("source_media_asset_id") for r in feeds)
    assert True not in calls, "no pre-pass without a video pool"
    assert client_content.drive_pool_has_video("gritx_ig") is False


def test_pre_pass_claims_only_the_video_slot_of_a_2x_day(monkeypatch, tmp_path):
    _sources()
    store = FakeMediaStore(assets=_tt_pool(n_videos=4, n_photos=2))
    _arm(monkeypatch, store, FakeDrive())
    # distinct captions per call: the two slots of one day must be two concepts
    n = iter(range(100))
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: (f"A grounded caption number {next(n)}", []))
    d = date(2026, 8, 1)
    while not (not builder.is_video_slot(d.isoformat(), 0)
               and builder.is_video_slot(d.isoformat(), 1)):
        d += timedelta(days=1)
    covered = set()
    extra = cmr.append_gym_drive_drafts(_account(), "gritx", d, 1, _voice(), log=lambda m: None,
                                        covered_days=set(), drive=FakeDrive(), store=store,
                                        library_path=str(tmp_path), slots_per_day=2,
                                        covered_slots=covered, video_beats_only=True,
                                        kind_prefs=("video",))
    feeds = [x for x in extra if not getattr(x, "is_story", False)]
    assert len(feeds) == 1 and feeds[0].cadence_slot_index == 1
    assert covered == {(d.isoformat(), 1)}
    assert mt.is_video_url(feeds[0].creative_public_url)
    # the gap-fill lane then respects the owned slot and fills only slot 0
    more = cmr.append_gym_drive_drafts(_account(), "gritx", d, 1, _voice(), log=lambda m: None,
                                       covered_days=set(), drive=FakeDrive(), store=store,
                                       library_path=str(tmp_path), slots_per_day=2,
                                       covered_slots=covered,
                                       day_captions_seed={d.isoformat(): [feeds[0].caption]})
    more_feeds = [x for x in more if not getattr(x, "is_story", False)]
    assert [x.cadence_slot_index for x in more_feeds] == [0]
    assert covered == {(d.isoformat(), 0), (d.isoformat(), 1)}


def test_pre_pass_leaves_a_video_beat_to_lane_a_when_no_video_is_pickable(monkeypatch,
                                                                          tmp_path):
    """kind_prefs=('video',) means NO photo fallback in the pre-pass."""
    _sources()
    store = FakeMediaStore(assets=_tt_pool(n_videos=0, n_photos=3))
    _arm(monkeypatch, store, FakeDrive())
    covered = set()
    extra = cmr.append_gym_drive_drafts(_account(), "gritx", date(2026, 8, 1), 11, _voice(),
                                        log=lambda m: None, covered_days=set(),
                                        drive=FakeDrive(), store=store,
                                        library_path=str(tmp_path), covered_slots=covered,
                                        video_beats_only=True, kind_prefs=("video",))
    assert extra == [] and covered == set()
    assert all(a["used_count"] == 0 for a in store.assets.values())


# ---- final verification (g): a poisoned asset is excluded after its second failure --
def test_a_poisoned_asset_is_skipped_after_two_gate_failures_and_beats_still_get_videos(
        monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    _sources()
    store = FakeMediaStore(assets=_tt_pool(n_videos=6, n_photos=0))
    _arm(monkeypatch, store, FakeDrive())
    n = iter(range(1000))
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: (f"A grounded caption number {next(n)}", []))
    from agent import post_quality
    gate_calls = []

    def gate(draft, banned, require_media=True):
        gate_calls.append(draft.source_media_asset_id)
        return draft.source_media_asset_id != "v00"       # v00 is poisoned, always fails
    monkeypatch.setattr(post_quality, "is_a_plus", gate)
    covered, failed = set(), set()
    start = date(2026, 8, 1)
    extra = cmr.append_gym_drive_drafts(_account(), "gritx", start, 11, _voice(),
                                        log=lambda m: None, covered_days=set(),
                                        drive=FakeDrive(), store=store,
                                        library_path=str(tmp_path), covered_slots=covered,
                                        video_beats_only=True, kind_prefs=("video",),
                                        failed_assets=failed, banned_words=("x",))
    feeds = [d for d in extra if not getattr(d, "is_story", False)]
    beats = _video_beats(start, 11)
    assert failed == {"v00"}
    assert gate_calls.count("v00") == 2, "gated twice (once + one retry), then excluded"
    # v00 is least-used again after its rollback; without the exclusion it would have
    # been re-picked on every later beat. Every OTHER beat still got a video.
    assert len(feeds) == beats - 1 and "v00" not in {f.source_media_asset_id for f in feeds}
    assert store.assets["v00"]["used_count"] == 0
    assert len({f.source_media_asset_id for f in feeds}) == beats - 1


# ---- final verification (h): an exception mid-lane never loses finished drafts ------
def test_a_raise_mid_lane_keeps_finished_drafts_and_rolls_back_the_in_flight_asset(
        monkeypatch, tmp_path):
    """Raise on the 3rd _finish_feed_with_story: the two finished video drafts land,
    the third asset is returned to the pool, Lane A covers every remaining day ->
    20/20 feeds and no stamped-but-unlanded asset."""
    _sources()
    store = FakeMediaStore(assets=_tt_pool())
    _arm(monkeypatch, store, FakeDrive())
    calls = {"n": 0}
    real_finish = cmr._finish_feed_with_story

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("story lane blew up")
        return real_finish(*a, **k)
    monkeypatch.setattr(cmr, "_finish_feed_with_story", flaky)
    start = date(2026, 8, 1)
    cal = _CalStore()
    logs = []
    out = cmr.build_client_month(_account(), "gritx", start.isoformat(), days=20,
                                 voice=_voice(), library_path=_lib(tmp_path, n=95),
                                 store=cal, banned_words=(), logger=logs.append)
    assert out["ok"] is True
    feeds = _feeds(cal)
    assert len(feeds) == 20 and len({r["post_date"] for r in feeds}) == 20, "20/20 feeds"
    videos = [r for r in feeds if r.get("source_media_asset_id")]
    assert len(videos) == 2, "the two drafts finished before the raise landed"
    assert any("staging raised RuntimeError" in m for m in logs)
    landed = {r["source_media_asset_id"] for r in videos}
    stamped = {a["id"] for a in store.assets.values() if a["used_count"]}
    assert stamped == landed, f"stamped-but-unlanded: {stamped - landed}"
    # the slots the pre-pass claimed are exactly the ones that landed (Lane A filled
    # the rest), so no day has two feeds on one account
    assert len({(r["post_date"], r["account"]) for r in cal.inserted
                if r["format"] == "feed"}) == 40


# ---- MAJOR 2: sync_source resolves a stale gym_id exactly like run() -----------------
def test_sync_source_resolves_a_stale_gym_id_before_touching_the_store(monkeypatch):
    from agent.jobs import sync_gym_media as job
    from tests.gym_media_fakes import make_source, photo
    monkeypatch.setattr(job, "_post_digest", lambda *a, **k: None)
    monkeypatch.setattr("agent.gym_media_routes._resolve_stale_fingerprint",
                        lambda g, **k: "toughtemple52040e" if g == "toughtemple086f51" else g)
    # the asset already indexed under the REAL key (id == the Drive file id)
    existing = make_asset("p1", gym_id="toughtemple52040e", source_id="src1", kind="photo")
    store = FakeMediaStore(sources=[make_source("src1", gym_id="toughtemple086f51")],
                           assets=[existing])
    listed = []
    real_list = store.list_assets

    def spy_list(gym_id, source_id=None):
        listed.append(gym_id)
        return real_list(gym_id, source_id=source_id)
    store.list_assets = spy_list
    drive = FakeDrive(files=[photo("p1")])
    out = job.sync_source(make_source("src1", gym_id="toughtemple086f51"), drive=drive,
                          store=store, probe_fn=lambda p: None, log=lambda m: None)
    assert out["ok"] and out["gym_id"] == "toughtemple52040e"
    assert listed and set(listed) == {"toughtemple52040e"}, "listed under the RESOLVED key"
    assert out["inserted"] == 0, "the existing asset is recognised, not re-inserted"
    assert len(store.assets) == 1 and store.assets["p1"]["gym_id"] == "toughtemple52040e"


def test_recipe_step_0b_remap_matches_run(monkeypatch):
    """The PR body's pre-step mirrors run(): src['gym_id'] =
    gym_media_routes._resolve_stale_fingerprint(src['gym_id'])."""
    from agent import gym_media_routes as gmr
    assert gmr._resolve_stale_fingerprint("x", resolve=lambda g: "y") == "y"
    assert gmr._resolve_stale_fingerprint("x", resolve=lambda g: None) == "x"


# ---- round 5 minors ----------------------------------------------------------------
def test_recaption_retry_carries_the_builders_grounding(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    _sources()
    store = FakeMediaStore(assets=[make_asset("v1", gym_id="gritx", kind="video",
                                              title="clip.mp4")])
    _arm(monkeypatch, store, FakeDrive())
    seen = []
    captions = iter(["weak", "A grounded caption about the class"])
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: (seen.append(k) or (next(captions), [])))
    from agent import post_quality
    verdicts = iter([False, True])
    monkeypatch.setattr(post_quality, "is_a_plus",
                        lambda draft, banned, require_media=True: next(verdicts))
    cmr.append_gym_drive_drafts(_account(), "gritx", date(2026, 8, 1), 1, _voice(),
                                log=lambda m: None, covered_days=set(), drive=FakeDrive(),
                                store=store, library_path=str(tmp_path), banned_words=("x",))
    assert len(seen) == 2
    first, retry = seen
    assert getattr(retry["creative"], "path", "").endswith("clip.mp4")
    assert os.path.basename(getattr(first["creative"], "path", "")) == \
        os.path.basename(getattr(retry["creative"], "path", ""))
    assert "verified" in retry and retry["verified"] == first.get("verified")


def test_local_hevc_video_is_never_a_swap_candidate(monkeypatch, tmp_path):
    from agent import media_swap as msw
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "raw.mov").write_bytes(b"\0" * 4096)
    (lib / "ok.mp4").write_bytes(b"\0" * 4096)
    monkeypatch.setattr(_gmi, "probe_video",
                        lambda p, timeout=None: {"duration_sec": 10, "width": 1080,
                                                 "height": 1920,
                                                 "codec": "hevc" if str(p).endswith(".mov") else "h264"})
    monkeypatch.setattr(msw, "_last_served_local", lambda base: {})
    cands = msw.local_candidates("gritx", str(lib), "2026-09-20", set())
    assert [c["key"] for c in cands] == ["ok.mp4"]


def test_swap_probe_and_host_run_under_the_request_deadline(monkeypatch, tmp_path):
    from agent import media_swap as msw
    seen = {}

    def probe(p, timeout=None):
        seen["probe_timeout"] = timeout
        return {"duration_sec": 10, "width": 1080, "height": 1920, "codec": "h264"}
    monkeypatch.setattr(_gmi, "probe_video", probe)
    asset = make_asset("v1", gym_id="gritx", kind="video", title="c.mp4")
    cand = {"source": "drive", "kind": "video", "key": "v1", "asset": asset}
    ticks = iter([0.0] + [30.0] * 20)          # 45 s of the 75 s budget left
    dl = msw._Deadline(msw.SWAP_REQUEST_DEADLINE_SEC, clock=lambda: next(ticks))
    out = msw._materialize("gritx", cand, str(tmp_path), drive=FakeDrive(),
                           media_store=FakeMediaStore(assets=[asset]),
                           budget=_gmi.RenditionBudget(1), deadline=dl)
    assert out and out["path"].endswith("c.mp4")
    assert seen["probe_timeout"] == pytest.approx(45.0), "probe bounded by the time left"
    # hosting is refused once the deadline has passed
    ticks2 = iter([0.0, 1.0, 10_000.0] + [10_000.0] * 10)
    out2 = msw.pick_replacement(
        "gritx", {"id": "r", "gym_id": "gritx", "post_date": "2026-09-20", "format": "feed",
                  "image_url": "https://cdn/old.jpg"},
        store=None, library_path="", candidates_fn=lambda g, r: [{"source": "local",
                                                                 "kind": "photo", "key": "n.jpg",
                                                                 "path": "/x/n.jpg"}],
        materialize_fn=lambda c: {"path": c["path"], "hosted": None},
        host_fn=lambda p: pytest.fail("hosting must not start past the deadline"),
        clock=lambda: next(ticks2))
    assert out2 == {"ok": False, "reason": msw.REASON_TIMEOUT}


def test_download_worker_thread_is_a_daemon(monkeypatch):
    import threading
    from agent import media_swap as msw
    made = []
    real = threading.Thread

    class Spy(real):
        def __init__(self, *a, **k):
            made.append(k.get("daemon"))
            super().__init__(*a, **k)
    monkeypatch.setattr(threading, "Thread", Spy)

    class _Drive:
        def download(self, file_id, dest):
            return dest
    assert msw._download_bounded(_Drive(), "x", "/tmp/y", timeout=2) is True
    assert made == [True]


# ---- D1 residual ---------------------------------------------------------------------
def test_story_reburn_and_meta_publisher_use_the_shared_video_definition():
    from agent import meta_publisher, story_reburn
    assert story_reburn._VIDEO_EXTS is mt.VIDEO_EXTS
    assert meta_publisher.is_video_url is mt.is_video_url
    for ext in mt.VIDEO_EXTS:
        assert meta_publisher._is_video(f"https://cdn/x{ext}") is True, ext
    assert meta_publisher._is_video("https://cdn/x.jpg") is False
    assert meta_publisher._is_video(None) is False
