"""gym_media_drive §7 WIRING: a gym that connected Google Drive actually gets
PENDING content_calendar posts built from its synced photo pool.

This is the regression guard for the audit's CRITICAL #1: build_gym_media_draft had
NO production caller, so a synced Drive photo never became a post. It is now wired
into client_month_run.build_client_month behind GYM_DRIVE_STAGE + the per-gym
GYM_DRIVE_CONNECT arming. Also covers CRITICAL #2: the staged row carries
source_media_asset_id so the portal hide + the removed-from-Drive sweep flip it back
to needs_media.

Fully OFFLINE: injected media store + drive fakes, stubbed vision/caption/hosting.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run as cmr, client_sources as cs  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    yield


class _FakeCalStore:
    def __init__(self):
        self.deleted = []
        self.inserted = []

    def delete_month(self, base_key, month):
        self.deleted.append((base_key, month))
        return 0

    def insert_rows(self, base_key, rows):
        self.inserted.extend(rows)
        return rows


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _account():
    return Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID")


def _lib(tmp_path, n=2):
    """A tiny uploaded-media library so the MEDIA-REQUIRED guard passes and the
    uploaded-media loop places a couple feeds; the Drive lane then fills the GAP days."""
    import json
    lib = tmp_path / "gritx_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
        (lib / f"photo_{i:02d}.json").write_text(
            json.dumps({"public_url": f"https://gritx.media/photo_{i:02d}.jpg"}))
    return str(lib)


def _stock_sources(account_key="gritx_ig"):
    cs.add_source(account_key, "offer", "21 day kickstart for busy parents",
                  "client social intake")
    cs.add_source(account_key, "service", "Small group training",
                  "client social intake")
    cs.add_source(account_key, "about", "Who we help: parents in their 40s",
                  "client social intake")


def _arm_drive_lane(monkeypatch, drive, store):
    """Flip GYM_DRIVE_STAGE on + arm the gym, and point the builder at the fakes +
    stub the vision/caption/host lanes so the pick->caption->host path runs offline."""
    monkeypatch.setenv("GYM_DRIVE_STAGE", "true")
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "true")
    # The builder resolves its own store/drive from these factories when the wiring
    # calls it without injecting them (the production call path).
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: drive)
    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None: {
                            "version": 2, "quality": {"usable": True},
                            "safety_flags": [], "one_line": "members in a class"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda a: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify",
                        lambda b, a, **k: {"ok": True, "bucket": "small_group",
                                           "verified_details": []})
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption about the class", []))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, gym: "https://cdn.fake/drive_served.jpg")


# ---- CRITICAL #1: a synced Drive photo becomes a PENDING calendar row -------------
def test_connected_drive_photo_becomes_pending_post(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepic1", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepic1": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True

    # A row sourced from the Drive asset landed, PENDING, on the real gym (gym_id=base),
    # carrying its served url AND the source_media_asset_id stamp.
    drive_rows = [r for r in cal.inserted
                  if r.get("source_media_asset_id") == "drivepic1"]
    assert drive_rows, "no content_calendar row was built from the connected Drive photo"
    for r in drive_rows:
        assert r["status"] == "pending"           # the human tap is untouched
        assert r["gym_id"] == "gritx"
        assert r["image_url"] == "https://cdn.fake/drive_served.jpg"
    # usage was stamped on the media asset at stage time (90-day reuse cooldown basis).
    assert store.assets["drivepic1"]["used_count"] == 1


# ---- flag OFF -> the Drive lane is inert (uploaded-media month unchanged) ----------
def test_drive_lane_inert_when_flag_off(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepic1", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepic1": b"jpgbytes"})
    # Arm the fakes/stubs but keep GYM_DRIVE_STAGE OFF.
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: drive)
    monkeypatch.delenv("GYM_DRIVE_STAGE", raising=False)
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "true")

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True
    assert not [r for r in cal.inserted if r.get("source_media_asset_id")]
    # the media asset was never touched (no stage, no usage stamp)
    assert store.assets["drivepic1"]["used_count"] == 0


# ---- flag ON but gym NOT armed -> lane inert --------------------------------------
def test_drive_lane_inert_when_gym_not_armed(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepic1", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepic1": b"jpgbytes"})
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: drive)
    monkeypatch.setenv("GYM_DRIVE_STAGE", "true")
    # GYM_DRIVE_CONNECT off AND gritx not in the pilot allowlist -> not armed.
    monkeypatch.delenv("GYM_DRIVE_CONNECT", raising=False)
    monkeypatch.delenv("GYM_DRIVE_CONNECT_GYMS", raising=False)

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True
    assert not [r for r in cal.inserted if r.get("source_media_asset_id")]


# ---- Dean Holcomb / CrossFit Reverb, 2026-08-30: a DRIVE-ONLY gym (zero
# ---- content_library uploads) must still build from its connected Drive pool -----
def test_drive_only_gym_empty_library_still_builds(monkeypatch, tmp_path):
    """The MEDIA-REQUIRED guard used to return awaiting_media whenever
    content_library/<base> was empty, even when the gym is connected via Google
    Drive with real synced photos ready — the exact self-serve portal flow. That
    early return sat BEFORE the GYM-DRIVE LANE, so the lane (already correctly
    gated) never got a chance to run for a Drive-only gym."""
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepicY", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepicY": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)

    empty_lib = tmp_path / "empty_gritx_lib"
    empty_lib.mkdir()          # exists but holds NO media files at all

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=3, voice=_voice(),
        library_path=str(empty_lib), store=cal, banned_words=())

    assert out["ok"] is True, f"expected a built month, got {out}"
    assert not out.get("awaiting_media")
    drive_rows = [r for r in cal.inserted
                  if r.get("source_media_asset_id") == "drivepicY"]
    assert drive_rows, "no content_calendar row was built from the connected Drive photo"
    for r in drive_rows:
        assert r["status"] == "pending"


def test_drive_only_gym_empty_library_stays_awaiting_when_drive_not_armed(
        monkeypatch, tmp_path):
    """REGRESSION GUARD: an empty content_library with NO connected Drive lane
    (flags off) must still report awaiting_media, exactly as before."""
    monkeypatch.delenv("GYM_DRIVE_STAGE", raising=False)
    monkeypatch.delenv("GYM_DRIVE_CONNECT", raising=False)
    monkeypatch.delenv("GYM_DRIVE_CONNECT_GYMS", raising=False)
    _stock_sources()
    empty_lib = tmp_path / "empty_gritx_lib2"
    empty_lib.mkdir()

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=3, voice=_voice(),
        library_path=str(empty_lib), store=cal, banned_words=())

    assert out["ok"] is False
    assert out.get("awaiting_media") is True
    assert cal.inserted == []


# ---- pool empty -> no Drive rows, uploaded-media month intact ---------------------
def test_empty_pool_adds_no_drive_rows(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[])          # nothing synced yet
    drive = FakeDrive(blobs={})
    _arm_drive_lane(monkeypatch, drive, store)

    cal = _FakeCalStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=_lib(tmp_path), store=cal, banned_words=())
    assert out["ok"] is True
    assert not [r for r in cal.inserted if r.get("source_media_asset_id")]
    # the uploaded-media month still built its own real-photo rows
    assert any(r.get("image_url", "").startswith("https://gritx.media/")
               for r in cal.inserted)


# ---- no approved source -> a Drive photo NEVER posts on imagination ---------------
def test_no_approved_source_builds_no_drive_post(monkeypatch, tmp_path):
    # A synced Drive photo + an armed lane, but the gym has NO approved sources at all.
    # The Drive caption's fact must come from an approved source, so the lane declines
    # to stage (a photo never posts without an approved fact behind the copy). We still
    # need the uploaded-media guard to pass, so seed sources for the UPLOADED month via
    # a DIFFERENT gym is not possible; instead assert the Drive lane specifically added
    # nothing by resolving the helper directly against a source-less account.
    from datetime import date
    store = FakeMediaStore(assets=[
        make_asset("drivepicX", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepicX": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    # gritx_ig has no client_sources rows in this test (none added).
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 3, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store)
    assert extra == []                       # nothing staged without an approved fact
    assert store.assets["drivepicX"]["used_count"] == 0


# ---- the helper marks the draft PENDING + stamps the asset id ---------------------
def test_append_helper_builds_pending_with_asset_id(monkeypatch, tmp_path):
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepic9", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepic9": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    from datetime import date
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 3, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store)
    assert extra, "the helper built no Drive drafts"
    d = extra[0]
    assert d.status == DraftStatus.PENDING
    assert d.draft_type == "gym_media"
    assert d.source_media_asset_id == "drivepic9"


# ---- Dale/ENG 2026-08-30: the Drive lane must emit the SAME cards as the
# ---- uploaded-media loop (paired story + the 2x cadence), not a bare 1x feed.
def test_drive_lane_pairs_a_story_with_every_feed(monkeypatch, tmp_path):
    """The lane used to append a bare _mark_feed draft, so a gym whose month the
    Drive lane filled got feed rows and ZERO stories. Dale (ENG) lost every story
    for a full month the day this lane took over his calendar."""
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("drivepicS", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"drivepicS": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    from datetime import date
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 1, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store,
        library_path=_lib(tmp_path))
    assert extra, "the helper built no Drive drafts"
    assert any(getattr(d, "is_story", False) for d in extra), \
        "a Drive feed must carry its paired story, exactly like an uploaded-media feed"
    assert any(not getattr(d, "is_story", False) for d in extra), "the feed itself is gone"


def test_drive_lane_honors_the_2x_cadence(monkeypatch, tmp_path):
    """The lane hard-coded one post per day (`for i in range(days)`), so a gym on 2x
    still got a single daily post and no slot ordinal — Dale's 'only one post a day
    goes out' after he turned on 2x."""
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("d1", gym_id="gritx", kind="photo", title="a.jpg"),
        make_asset("d2", gym_id="gritx", kind="photo", title="b.jpg")])
    drive = FakeDrive(blobs={"d1": b"jpgbytes", "d2": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    from datetime import date
    # Captions are GROUNDED in the day's approved source, so two slots drawing
    # different sources produce different copy (the stub models that).
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda a, source, *r, **k: (f"Caption about {source}", []))
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 1, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store,
        library_path=_lib(tmp_path), slots_per_day=2)
    feeds = [d for d in extra if not getattr(d, "is_story", False)]
    assert len(feeds) == 2, f"2x must stage two feeds on the day, got {len(feeds)}"
    assert feeds[0].caption != feeds[1].caption, \
        "a 2x day must never publish the same words twice"
    assert sorted(getattr(d, "cadence_slot_index", None) for d in feeds) == [0, 1], \
        "each 2x feed carries its slot ordinal so publish times are deterministic"


def test_drive_lane_at_1x_is_unchanged_one_feed_per_day(monkeypatch, tmp_path):
    """1x keeps its shape: one feed per day and NO slot ordinal, so the row shape and
    publish hashing do not move for the gyms that never toggled. (Slot 0 picks the
    same category as the old code; only the no-source-in-that-pillar FALLBACK now
    walks a rotated order, which can land on a different approved fact.)"""
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("e1", gym_id="gritx", kind="photo", title="a.jpg"),
        make_asset("e2", gym_id="gritx", kind="photo", title="b.jpg")])
    drive = FakeDrive(blobs={"e1": b"jpgbytes", "e2": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    from datetime import date
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 1, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store,
        library_path=_lib(tmp_path))
    feeds = [d for d in extra if not getattr(d, "is_story", False)]
    assert len(feeds) == 1
    assert getattr(feeds[0], "cadence_slot_index", None) is None


def test_drive_story_burns_its_caption_from_the_hosted_media(monkeypatch, tmp_path):
    """PRODUCTION POSTURE (AGENT_STORY_FORMAT on). A Drive draft carries the asset
    TITLE in creative_path, not a readable local file, so the caption burn used to
    fail and the captionless guard dropped every Drive story. The story must now be
    rendered from the HOSTED media and kept."""
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("dS2", gym_id="gritx", kind="photo", title="class.jpg")])
    drive = FakeDrive(blobs={"dS2": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")

    seen = {}
    # The hosted media is fetched through the same download_bytes lane the
    # publish-time aspect preflight uses (pub-*.r2.dev 403s a plain GET).
    monkeypatch.setattr("agent.media_host.download_bytes", lambda url: b"realphotobytes")

    def _fake_story_image(photo_path, caption, gym_name, library_path, logger=None):
        # Proves we handed the renderer a REAL readable file, not the bare title.
        seen["path"] = photo_path
        seen["readable"] = os.path.isfile(photo_path)
        seen["bytes"] = open(photo_path, "rb").read()
        out = os.path.join(str(library_path), "burned__story.jpg")
        with open(out, "wb") as fh:
            fh.write(b"burned")
        return out

    monkeypatch.setattr("agent.story_image.get_or_make_story_image", _fake_story_image)
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda asset, key: "https://cdn.test/story.jpg")

    from datetime import date
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 1, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store,
        library_path=_lib(tmp_path))

    stories = [d for d in extra if getattr(d, "is_story", False)]
    assert stories, "the Drive story was dropped by the captionless guard"
    assert seen.get("readable") is True, "the renderer got a bare title, not a real file"
    assert seen.get("bytes") == b"realphotobytes"
    assert stories[0].creative_public_url == "https://cdn.test/story.jpg"
    # the temp download is not left behind
    assert not os.path.isfile(seen["path"])


def test_drive_lane_never_stages_the_same_caption_twice_in_a_day(monkeypatch, tmp_path):
    """HARD GUARD: whatever the generator does, a repeat caption on one day is DROPPED,
    never staged — and its photo goes back to the pool instead of being burned."""
    _stock_sources()
    store = FakeMediaStore(assets=[
        make_asset("z1", gym_id="gritx", kind="photo", title="a.jpg"),
        make_asset("z2", gym_id="gritx", kind="photo", title="b.jpg")])
    drive = FakeDrive(blobs={"z1": b"jpgbytes", "z2": b"jpgbytes"})
    _arm_drive_lane(monkeypatch, drive, store)
    # A generator that returns identical copy no matter the source.
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("The exact same words", []))
    from datetime import date
    extra = cmr.append_gym_drive_drafts(
        _account(), "gritx", date(2026, 8, 1), 1, _voice(),
        log=lambda m: None, covered_days=set(), drive=drive, store=store,
        library_path=_lib(tmp_path), slots_per_day=2)
    feeds = [d for d in extra if not getattr(d, "is_story", False)]
    assert len(feeds) == 1, "the duplicate second post must be dropped, not staged"
    # The dropped draft's asset was returned to the pool, not burned for 90 days.
    burned = [a for a in store.assets.values() if int(a.get("used_count") or 0) > 0]
    assert len(burned) == 1, f"a dropped draft burned its photo: {burned}"
