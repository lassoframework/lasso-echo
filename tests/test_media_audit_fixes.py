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
    """3 pickable assets, 10 days, a stale 5-photo library: 10 feed days = 3 Drive +
    7 spaced repeats. Before this fix: 3 posts and 7 EMPTY days."""
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
    assert len({r["post_date"] for r in feeds}) == 10, "an empty day is never acceptable"
    drive = [r for r in feeds if r.get("source_media_asset_id")]
    repeats = [r for r in feeds if not r.get("source_media_asset_id")]
    assert len(drive) == 3 and len(repeats) == 7
    assert sum("placed a spaced repeat" in m for m in logs) == 7
    # the repeats are spread: 5 distinct photos before any photo is used twice
    assert len({r["image_url"] for r in repeats}) == 5


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
