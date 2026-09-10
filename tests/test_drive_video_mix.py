"""John Weeks / Tough Temple, 2026-09-10: the Drive lane stages VIDEOS on a
deterministic photo/video mix, Lane A stops placing stale repeats when the Drive
pool can fill the day, a Drive video row publishes as a video, and the dead
deny-sweep select is fixed.

Ground truth that motivated this: toughtemple52040e had 57 eligible Drive videos
and 6 eligible photos; zero videos had ever been staged (the builder hard-coded
kind_preference='photo'); the 68 upcoming rows cycled 24 distinct stills.

Fully OFFLINE: injected media store + drive fakes, stubbed probe/poster/caption/host.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, gym_media_builder as builder  # noqa: E402
from agent import gym_media_selector as sel  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("AGENT_VISION_GYMS", raising=False)
    monkeypatch.delenv("GYM_DRIVE_STAGE", raising=False)
    monkeypatch.delenv("GYM_DRIVE_CONNECT", raising=False)
    monkeypatch.delenv("GYM_DRIVE_CONNECT_GYMS", raising=False)
    client_content.clear_drive_pool_cache()
    yield
    client_content.clear_drive_pool_cache()


class _Acct:
    key = "toughtemple52040e_ig"
    platform = "instagram"


def _wire_builder(monkeypatch, host_url="https://cdn.fake/served.jpg",
                  probe=None, poster="https://cdn.fake/clip__poster.jpg"):
    """Stub the lanes the builder touches so pick -> probe -> caption -> host runs
    offline. Vision is wired to FAIL LOUDLY: no video may ever reach it."""
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda *a, **k: pytest.fail("vision must not run here"))
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption about the class", []))
    monkeypatch.setattr("agent.media_host.host_media", lambda path, gym: host_url)
    monkeypatch.setattr("agent.gym_media_index.probe_video",
                        lambda p: probe if probe is not None else
                        {"duration_sec": 21.0, "width": 1080, "height": 1920,
                         "codec": "h264"})
    monkeypatch.setattr(builder, "video_poster_url", lambda path, work, tenant: poster)


def _video_day(start=date(2026, 9, 11), slot=0, want=True):
    """The first day on/after start whose (day, slot) is / is not a video beat."""
    d = start
    for _ in range(builder.VIDEO_MIX_CYCLE + 1):
        if builder.is_video_slot(d.isoformat(), slot) is want:
            return d.isoformat()
        d += timedelta(days=1)
    raise AssertionError("mix pattern produced no such day")


# ---- 1. the mix target -------------------------------------------------------------
def test_mix_is_deterministic_and_lands_between_40_and_50_percent_video():
    days = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(66)]
    first = [builder.is_video_slot(d) for d in days]
    assert first == [builder.is_video_slot(d) for d in days], "re-run must match"
    share = sum(first) / len(first)
    assert 0.40 <= share <= 0.50, f"1x video share {share:.2f} outside 40..50%"
    # 2x: both slots of every day, still inside the band
    both = [builder.is_video_slot(d, s) for d in days for s in (0, 1)]
    share2 = sum(both) / len(both)
    assert 0.40 <= share2 <= 0.50, f"2x video share {share2:.2f} outside 40..50%"
    # the two slots of one day are not always the same kind
    assert any(builder.is_video_slot(d, 0) != builder.is_video_slot(d, 1) for d in days)


def test_kinds_for_slot_only_asks_for_what_the_pool_has():
    vday, pday = _video_day(want=True), _video_day(want=False)
    assert builder.kinds_for_slot({"photo", "video"}, vday) == ["video", "photo"]
    assert builder.kinds_for_slot({"photo", "video"}, pday) == ["photo", "video"]
    assert builder.kinds_for_slot({"photo"}, vday) == ["photo"]
    assert builder.kinds_for_slot({"video"}, pday) == ["video"]
    assert builder.kinds_for_slot(set(), vday) == []
    assert builder.kinds_for_slot({"other"}, vday) == []


# ---- 2. a Drive VIDEO stages, with the video row shape -----------------------------
def test_drive_video_stages_pending_with_poster_and_video_url(monkeypatch, tmp_path):
    _wire_builder(monkeypatch, host_url="https://cdn.fake/tt/abc/clip.mp4")
    store = FakeMediaStore(assets=[
        make_asset("v1", gym_id="toughtemple52040e", kind="video", title="clip.mp4",
                   mime="video/mp4"),
        make_asset("p1", gym_id="toughtemple52040e", kind="photo", title="team.jpg")])
    drive = FakeDrive(blobs={"v1": b"mp4bytes" * 512, "p1": b"jpg" * 1024})
    draft = builder.build_gym_media_draft(
        _Acct(), _video_day(want=True), "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path), now=NOW)
    assert draft is not None and draft.status == DraftStatus.PENDING
    assert draft.source_media_asset_id == "v1"
    assert draft.creative_public_url.endswith(".mp4")          # the video itself
    assert draft.thumbnail_url == "https://cdn.fake/clip__poster.jpg"
    assert draft.creative_path == "clip.mp4"
    assert store.assets["v1"]["used_count"] == 1               # stamped at stage time
    assert store.assets["p1"]["used_count"] == 0
    # probe data was written back (the index converges)
    assert store.assets["v1"]["duration_sec"] == 21.0 and store.assets["v1"]["eligible"]


def test_photo_beat_stages_a_photo_when_both_kinds_exist(monkeypatch, tmp_path):
    _wire_builder(monkeypatch)
    store = FakeMediaStore(assets=[
        make_asset("v1", gym_id="toughtemple52040e", kind="video", title="clip.mp4"),
        make_asset("p1", gym_id="toughtemple52040e", kind="photo", title="team.jpg")])
    drive = FakeDrive()
    draft = builder.build_gym_media_draft(
        _Acct(), _video_day(want=False), "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path), now=NOW)
    assert draft is not None and draft.source_media_asset_id == "p1"
    assert not getattr(draft, "thumbnail_url", "")


def test_video_beat_falls_back_to_a_photo_when_every_video_fails_its_gate(monkeypatch,
                                                                          tmp_path):
    """The preferred kind is exhausted (the only video fails its probe): the slot
    falls back to the other kind instead of leaving the day empty."""
    _wire_builder(monkeypatch, probe={"duration_sec": 240.0, "width": 1080,
                                      "height": 1920, "codec": "h264"})   # > 90s
    store = FakeMediaStore(assets=[
        make_asset("v_long", gym_id="toughtemple52040e", kind="video", title="long.mp4"),
        make_asset("p1", gym_id="toughtemple52040e", kind="photo", title="team.jpg")])
    draft = builder.build_gym_media_draft(
        _Acct(), _video_day(want=True), "faces", voice=object(), source=object(),
        store=store, drive=FakeDrive(), library_dir=str(tmp_path), now=NOW)
    assert draft is not None and draft.source_media_asset_id == "p1"
    assert store.assets["v_long"]["eligible"] is False        # gate wrote back


def test_photo_beat_falls_back_to_video_when_photos_are_on_cooldown(monkeypatch,
                                                                    tmp_path):
    """Tough Temple's exact state: every photo burned its cooldown, 57 videos idle."""
    _wire_builder(monkeypatch, host_url="https://cdn.fake/tt/abc/clip.mp4")
    used = (NOW - timedelta(days=3)).isoformat()
    store = FakeMediaStore(assets=[
        make_asset("p1", gym_id="toughtemple52040e", kind="photo", used_count=1,
                   last_used_at=used),
        make_asset("v1", gym_id="toughtemple52040e", kind="video", title="clip.mp4")])
    draft = builder.build_gym_media_draft(
        _Acct(), _video_day(want=False), "faces", voice=object(), source=object(),
        store=store, drive=FakeDrive(), library_dir=str(tmp_path), now=NOW)
    assert draft is not None and draft.source_media_asset_id == "v1"


def test_two_x_slots_pass_their_ordinal_into_the_mix(monkeypatch, tmp_path):
    """slot_index reaches the builder from append_gym_drive_drafts: pick a day whose
    slot 0 is a photo beat and slot 1 a video beat and check the kinds differ."""
    _wire_builder(monkeypatch, host_url="https://cdn.fake/tt/abc/clip.mp4")
    day = None
    d = date(2026, 9, 11)
    for _ in range(builder.VIDEO_MIX_CYCLE):
        if not builder.is_video_slot(d.isoformat(), 0) and builder.is_video_slot(d.isoformat(), 1):
            day = d.isoformat()
            break
        d += timedelta(days=1)
    assert day, "pattern has a photo/video split day"
    picked = []
    for slot in (0, 1):
        store = FakeMediaStore(assets=[
            make_asset("v1", gym_id="toughtemple52040e", kind="video", title="clip.mp4"),
            make_asset("p1", gym_id="toughtemple52040e", kind="photo")])
        draft = builder.build_gym_media_draft(
            _Acct(), day, "faces", voice=object(), source=object(), store=store,
            drive=FakeDrive(), library_dir=str(tmp_path), now=NOW, slot_index=slot)
        picked.append(draft.source_media_asset_id)
    assert picked == ["p1", "v1"]


# ---- 3. the WHOLE row path: a Drive video row publishes as a VIDEO ------------------
def test_drive_video_row_publishes_as_video_on_instagram_and_facebook(monkeypatch,
                                                                       tmp_path):
    from agent import client_month_run as cmr, client_sources as cs, zernio
    from agent import calendar_autopublish as cap
    from agent.accounts import Account, Platform
    from agent.voice import VoiceDoc
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("GYM_DRIVE_STAGE", "true")
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "true")
    monkeypatch.delenv("AGENT_STORY_FORMAT", raising=False)
    cs.add_source("gritx_ig", "service", "Small group training", "client social intake")
    store = FakeMediaStore(assets=[
        make_asset("v1", gym_id="gritx", kind="video", title="squat.mov",
                   mime="video/quicktime")])
    drive = FakeDrive(blobs={"v1": b"movbytes" * 512})
    _wire_builder(monkeypatch, host_url="https://pub.r2.dev/echo/gritx/abc/squat.mov")
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: drive)
    account = Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                      token_env="T", target_id_env="TID")
    voice = VoiceDoc(raw="We help members win.\n#GetFit", hashtags=["#GetFit"],
                     ctas=["Save this post."])
    start = date.fromisoformat(_video_day(want=True))
    extra = cmr.append_gym_drive_drafts(
        account, "gritx", start, 1, voice, log=lambda m: None, covered_days=set(),
        drive=drive, store=store, library_path=str(tmp_path))
    assert extra, "the Drive lane staged nothing for a video slot"
    rows = cmr._to_rows("gritx", extra)
    feeds = [r for r in rows if r["format"] == "feed"]
    assert {r["account"] for r in feeds} == {"instagram", "facebook"}, "FB mirror"
    for r in feeds:
        assert r["status"] == "pending" and r["gym_id"] == "gritx"
        assert r["image_url"].endswith(".mov")            # the video publishes
        assert r["thumbnail_url"] == "https://cdn.fake/clip__poster.jpg"  # display
        assert r["source_media_asset_id"] == "v1"
    story = [r for r in rows if r["format"] == "story"]
    assert story and story[0]["image_url"].endswith(".mov")   # story-format OFF: raw kept

    # publish-time: the aspect preflight leaves a video alone (never treats it as an
    # image) and the Zernio payload types it 'video'.
    for r in feeds:
        assert cap._normalize_feed_image(dict(r), account, store=None) == r
        draft = cap._draft_for(r)
        assert draft.creative_public_url == r["image_url"]
        posted = {}

        class _Http:
            def post(self, url, json=None, headers=None, timeout=None):
                posted["json"] = json

                class R:
                    status_code = 200

                    def json(self):
                        return {"_id": "z1"}
                return R()

        client = zernio.ZernioClient(api_key="k", http=_Http())
        platform = "instagram" if r["account"] == "instagram" else "facebook"
        client.create_post("acct", draft.caption, media_urls=[draft.creative_public_url],
                           platform=platform, story=False,
                           page_id="pg" if platform == "facebook" else None)
        assert posted["json"]["mediaItems"] == [
            {"type": "video", "url": r["image_url"]}], "sent as an image"
        assert posted["json"]["platforms"][0]["platform"] == platform
    # the portal reads the same row as a video card with the poster as its image
    from agent import portal_social as ps
    assert ps._media_kind(feeds[0]["image_url"]) == "video"


# ---- 4. Lane A: no stale repeat when the Drive pool can fill the day ----------------
def _still(lib, name):
    with open(os.path.join(lib, name), "wb") as fh:
        fh.write(b"\xff\xd8\xff" + b"\0" * 64)


def _served(lib, name, account_key, day_key):
    from agent import dam, rotation
    rotation.record_served(account_key, dam.rotation_key(os.path.join(lib, name)),
                           "service", day_key)


def test_legacy_pick_returns_none_when_drive_can_fill_instead_of_a_stale_repeat(
        tmp_path, monkeypatch):
    lib = str(tmp_path)
    _still(lib, "only.jpg")
    _served(lib, "only.jpg", "tt_ig", "2026-09-05")            # inside the 14-day window
    monkeypatch.setattr(client_content, "drive_pool_can_fill", lambda *a, **k: True)
    assert client_content.pick_image("tt_ig", "2026-09-11", lib, pillar="service") is None


def test_legacy_pick_keeps_the_stale_repeat_when_no_drive_pool(tmp_path, monkeypatch):
    lib = str(tmp_path)
    _still(lib, "only.jpg")
    _served(lib, "only.jpg", "tt_ig", "2026-09-05")
    monkeypatch.setattr(client_content, "drive_pool_can_fill", lambda *a, **k: False)
    pick = client_content.pick_image("tt_ig", "2026-09-11", lib, pillar="service")
    assert pick is not None and getattr(pick, "stale_reuse", False) is True


def test_allow_reuse_last_resort_still_gets_the_stale_pick(tmp_path, monkeypatch):
    """The denied-slot backfill already tried Drive first; its explicit allow_reuse
    fallback must never be emptied by the gate (a denied slot stays filled)."""
    lib = str(tmp_path)
    _still(lib, "only.jpg")
    _served(lib, "only.jpg", "tt_ig", "2026-09-05")
    monkeypatch.setattr(client_content, "drive_pool_can_fill",
                        lambda *a, **k: pytest.fail("gate must not run for allow_reuse"))
    pick = client_content.pick_image("tt_ig", "2026-09-11", lib, pillar="service",
                                     allow_reuse=True)
    assert pick is not None


def test_drive_pool_can_fill_needs_both_flags_and_a_pickable_asset(monkeypatch):
    calls = []
    monkeypatch.setattr(sel, "pickable", lambda *a, **k: calls.append(a) or [])
    # flags off: never reads the pool
    assert client_content.drive_pool_can_fill("tt_ig") is False and calls == []
    monkeypatch.setenv("GYM_DRIVE_STAGE", "true")
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "true")
    # flags on, empty pool: False
    assert client_content.drive_pool_can_fill("tt_ig") is False and len(calls) == 1
    client_content.clear_drive_pool_cache()
    monkeypatch.setattr(sel, "pickable",
                        lambda *a, **k: calls.append(a) or [{"id": "v1", "kind": "video"}])
    assert client_content.drive_pool_can_fill("tt_ig") is True
    n = len(calls)
    assert client_content.drive_pool_can_fill("tt_ig") is True and len(calls) == n, \
        "the per-gym answer is cached inside a run (one read, not thirty)"


def test_drive_pool_can_fill_never_raises(monkeypatch):
    monkeypatch.setenv("GYM_DRIVE_STAGE", "true")
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "true")

    def _boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(sel, "pickable", _boom)
    assert client_content.drive_pool_can_fill("tt_ig") is False


# ---- 5. the selector: pickable / pool_kinds share ONE rule with pick_media ---------
def test_pickable_and_pool_kinds_follow_the_pick_rules():
    used = (NOW - timedelta(days=3)).isoformat()
    store = FakeMediaStore(assets=[
        make_asset("p_cool", gym_id="tt", kind="photo", used_count=1, last_used_at=used),
        make_asset("v_ok", gym_id="tt", kind="video"),
        make_asset("v_hidden", gym_id="tt", kind="video", excluded_by_coach=True),
        make_asset("v_unprobed", gym_id="tt", kind="video", eligible=None),
        make_asset("x_other", gym_id="other", kind="video")])
    assert [a["id"] for a in sel.pickable("tt", store=store, now=NOW)] == ["v_ok"]
    assert sel.pool_kinds("tt", store=store, now=NOW) == {"video"}
    assert sel.pickable("tt", "photo", store=store, now=NOW) == []
    assert sel.pick_media("tt", store=store, now=NOW)["id"] == "v_ok"


def test_pickable_never_fires_the_pool_empty_alert(monkeypatch):
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append(k) or True)
    assert sel.pickable("tt", store=FakeMediaStore(assets=[]), now=NOW) == []
    assert fired == []


# ---- 6. the deny sweep actually selects the column it filters on --------------------
def test_deny_sweep_fetch_selects_source_media_asset_id(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    seen = {}

    class _Http:
        def get(self, url, params=None, headers=None, timeout=None):
            seen["params"] = params

            class R:
                status_code = 200

                def json(self):
                    return [{"id": "r1", "status": "denied", "pillar": "faces",
                             "source_media_asset_id": "a1"}]
            return R()

    rows = sel._default_fetch_rows("tt", "2026-09-12", http=_Http())
    assert "source_media_asset_id" in seen["params"]["select"].split(",")
    assert seen["params"]["gym_id"] == "eq.tt" and seen["params"]["post_date"] == "eq.2026-09-12"
    assert rows[0]["source_media_asset_id"] == "a1"


def test_deny_sweep_end_to_end_with_the_fixed_select_rolls_the_asset_back(monkeypatch):
    """The two halves together: a portal-denied Drive row (out-of-band, no Slack
    hook) returns its asset to the pool on the nightly sweep."""
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="tt")])
    sel.stamp_use(store.get_asset("a1"), "tt", "2026-09-12", store=store, now=NOW)
    assert store.assets["a1"]["used_count"] == 1
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")

    class _Http:
        def get(self, url, params=None, headers=None, timeout=None):
            assert "source_media_asset_id" in params["select"]

            class R:
                status_code = 200

                def json(self):
                    return [{"id": "r1", "status": "denied", "pillar": "faces",
                             "source_media_asset_id": "a1"}]
            return R()

    summary = sel.observe_denials(
        store=store, fetch_rows=lambda g, d: sel._default_fetch_rows(g, d, http=_Http()))
    assert summary["rolled_back"] == 1
    assert store.assets["a1"]["used_count"] == 0


# ---- 7. the store carries the media identity with a swap ----------------------------
def test_swap_media_carries_thumbnail_and_asset_id_and_nothing_else(monkeypatch):
    from agent import portal_calendar_store as pcs
    from tests.test_portal_calendar_supabase import _FakeHTTP, _Resp, _row
    http = _FakeHTTP(patch_resp=_Resp(200, [_row("id-m", gym_id="tt", status="pending")]))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    out = pcs.SupabaseCalendarStore().swap_media(
        "tt", "id-m", "https://cdn/squat.mp4",
        extra_fields={"thumbnail_url": "https://cdn/squat__poster.jpg",
                      "source_media_asset_id": "v1", "status": "approved",
                      "caption": "never"})
    assert out is not None
    _m, _u, params, _h, payload = http.calls[0]
    assert params["status"] == "in.(pending,coach_review)"
    assert payload == {"image_url": "https://cdn/squat.mp4",
                       "thumbnail_url": "https://cdn/squat__poster.jpg",
                       "source_media_asset_id": "v1"}, "status/caption must never ride along"
    # clearing: None values are sent so a stale poster / asset id is nulled
    http.calls.clear()
    pcs.SupabaseCalendarStore().swap_media(
        "tt", "id-m", "https://cdn/new.jpg",
        extra_fields={"thumbnail_url": None, "source_media_asset_id": None})
    assert http.calls[0][4] == {"image_url": "https://cdn/new.jpg",
                                "thumbnail_url": None, "source_media_asset_id": None}
