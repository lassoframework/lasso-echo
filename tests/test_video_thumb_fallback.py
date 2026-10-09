"""Video thumbnail fallback (2026-10-09, hardened): black/washed/tiny/
undecodable Drive thumbnails fall back to the BEST representative JPEG frame
from the original bytes; plausible informative Drive thumbnails keep the fast
path; unknown/zero/over-cap sizes and empty downloads close non-200; cache is
keyed by gym+asset+nonempty content hash. Offline except one explicitly
gated real-ffmpeg smoke test."""
import io
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from PIL import Image  # noqa: E402

from agent import gym_media_routes as gm  # noqa: E402
from agent import video_thumb_fallback as vt  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402


@pytest.fixture(autouse=True)
def _arm(monkeypatch):
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    vt.clear_cache()
    yield
    vt.clear_cache()


def _jpeg(img):
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _noisy_img(seed_shift=0, size=240):
    """Deterministic detailed image whose JPEG is plausibly sized (>>4KB),
    like the 10-16KB recognizable Drive posters in production."""
    img = Image.new("RGB", (size, size))
    img.putdata([((x * 4 + seed_shift) % 256, (y * 4 + x + seed_shift) % 256,
                  ((x + y) * 2 + seed_shift * 7) % 256)
                 for y in range(size) for x in range(size)])
    return img


def _stripes_img(size=64):
    """Small HIGH-CONTRAST black/white stripe image: mean/std look
    'informative' but the JPEG is tiny — the HYROX blurred-poster pattern."""
    img = Image.new("RGB", (size, size))
    img.putdata([(255, 255, 255) if (x // 4) % 2 else (0, 0, 0)
                 for y in range(size) for x in range(size)])
    return img


BLACK = _jpeg(Image.new("RGB", (64, 64), (0, 0, 0)))
WASHED = _jpeg(Image.new("RGB", (64, 64), (255, 255, 255)))
REAL = _jpeg(_noisy_img())                 # informative AND plausibly sized
REAL2 = _jpeg(_noisy_img(seed_shift=90))
HYROX_LIKE = _jpeg(_stripes_img())         # tiny, high-contrast, uninformative
GARBAGE = b"\x00\x01\x02not-a-jpeg\x03"    # undecodable poster bytes

assert len(REAL) >= vt.TINY_POSTER_SUSPECT_BYTES
assert len(HYROX_LIKE) < vt.TINY_POSTER_SUSPECT_BYTES


def _video_asset(fid="a1", gym_id="pierce", content_hash="hashA", size=50_000_000):
    return make_asset(fid, gym_id=gym_id, kind="video", mime="video/mp4",
                      title="clip.mp4", size=size, content_hash=content_hash)


def _patch_frame(monkeypatch, frames):
    """Fake representative_frame: returns frames popped in order, records calls."""
    calls = []
    def fake(path, **kw):
        calls.append(path)
        return frames.pop(0) if frames else None
    monkeypatch.setattr(vt, "representative_frame", fake)
    return calls


def _serve(asset, thumb, blobs=None, gym="pierce"):
    store = FakeMediaStore(assets=[asset])
    drive = FakeDrive(thumbs=({asset["id"]: thumb} if thumb is not None else {}),
                      blobs=blobs)
    return (*gm.handle_thumbnail(gym, asset["id"], store=store, drive=drive), drive)


# ---- Drive-poster judgement ------------------------------------------------

def test_black_drive_thumbnail_falls_back_to_real_frame(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    status, ctype, data, drive = _serve(_video_asset(), BLACK)
    assert status == 200 and ctype == "image/jpeg"
    assert data == REAL and calls and "a1" in drive.downloads


def test_washed_drive_thumbnail_falls_back(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    status, _, data, _ = _serve(_video_asset(), WASHED)
    assert status == 200 and data == REAL and calls


def test_tiny_high_contrast_hyrox_like_poster_is_suspect(monkeypatch):
    """A <4KB poster whose luma/std look informative (blurred HYROX pattern)
    must NOT take the fast path; the real frame is served instead."""
    stats = vt._decode_stats(HYROX_LIKE)
    assert stats is not None and vt._is_informative(*stats)  # std/mean pass!
    calls = _patch_frame(monkeypatch, [REAL])
    status, _, data, drive = _serve(_video_asset(), HYROX_LIKE)
    assert status == 200 and data == REAL
    assert calls and "a1" in drive.downloads


def test_undecodable_poster_never_served_as_200(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    status, ctype, data, _ = _serve(_video_asset(), GARBAGE)
    assert status == 200 and data == REAL and calls  # fell back, not served
    # and when no frame can be produced the garbage must fail closed:
    _patch_frame(monkeypatch, [])
    vt.clear_cache()
    status, ctype, data, _ = _serve(_video_asset("a2"), GARBAGE)
    assert status == 404 and data != GARBAGE


def test_informative_plausible_drive_thumbnail_fast_path_no_download(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    status, _, data, drive = _serve(_video_asset(), REAL)
    assert status == 200 and data == REAL
    assert calls == [] and "a1" not in drive.downloads


def test_cross_gym_404_before_any_fallback(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    store = FakeMediaStore(assets=[_video_asset(gym_id="pierce")])
    drive = FakeDrive(thumbs={"a1": BLACK})
    status, _, _ = gm.handle_thumbnail("otherg", "a1", store=store, drive=drive)
    assert status == 404 and calls == [] and drive.downloads == []


def test_no_usable_frame_fails_closed(monkeypatch):
    _patch_frame(monkeypatch, [])
    status, _, _, _ = _serve(_video_asset(), BLACK)
    assert status == 404


def test_no_drive_thumbnail_and_no_frame_fails_closed(monkeypatch):
    _patch_frame(monkeypatch, [])
    status, _, _, _ = _serve(_video_asset(), None)
    assert status == 404


# ---- size gates -------------------------------------------------------------

def test_oversized_source_never_downloaded(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    status, _, _, drive = _serve(
        _video_asset(size=vt.MAX_SOURCE_BYTES + 1), BLACK)
    assert status == 404 and calls == [] and drive.downloads == []


def test_unknown_or_zero_declared_size_never_downloaded(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    for bad in (0, None, "not-a-number"):
        status, _, _, drive = _serve(_video_asset(size=bad), BLACK)
        assert status == 404, bad
    assert calls == [] and drive.downloads == []


def test_empty_download_fails_closed(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    status, _, _, drive = _serve(_video_asset(), BLACK, blobs={"a1": b""})
    assert status == 404 and calls == [] and "a1" in drive.downloads


def test_downloaded_size_over_cap_fails_closed(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    big = b"x" * (vt.MAX_SOURCE_BYTES + 1)
    status, _, _, _ = _serve(_video_asset(), BLACK, blobs={"a1": big})
    assert status == 404 and calls == []


# ---- cache ------------------------------------------------------------------

def test_cache_hit_avoids_second_download(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL])
    asset = _video_asset()
    store = FakeMediaStore(assets=[asset])
    drive = FakeDrive(thumbs={"a1": BLACK})
    gm.handle_thumbnail("pierce", "a1", store=store, drive=drive)
    assert len(drive.downloads) == 1 and len(calls) == 1
    status, _, data = gm.handle_thumbnail("pierce", "a1", store=store,
                                          drive=drive)
    assert status == 200 and data == REAL
    assert len(drive.downloads) == 1 and len(calls) == 1   # served from cache


def test_no_cache_without_content_hash(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL, REAL])
    asset = _video_asset(content_hash="")
    _serve(asset, BLACK)
    _serve(asset, BLACK)
    assert len(calls) == 2            # both requests recomputed


def test_cache_key_isolation_gym_and_content_hash(monkeypatch):
    calls = _patch_frame(monkeypatch, [REAL, REAL])
    a1 = _video_asset("a1", gym_id="pierce", content_hash="hashA")
    a2 = _video_asset("a2", gym_id="reverb", content_hash="hashA")
    store = FakeMediaStore(assets=[a1, a2])
    drive = FakeDrive(thumbs={"a1": BLACK, "a2": BLACK})
    gm.handle_thumbnail("pierce", "a1", store=store, drive=drive)
    gm.handle_thumbnail("reverb", "a2", store=store, drive=drive)
    # Different gyms must NOT share a cached frame even for identical hashes.
    assert drive.downloads == ["a1", "a2"]
    # Same gym + same asset but CHANGED content hash must recompute.
    calls.extend([REAL])
    store.assets["a1"]["content_hash"] = "hashB"
    gm.handle_thumbnail("pierce", "a1", store=store, drive=drive)
    assert drive.downloads.count("a1") == 2


# ---- frame selection scoring (real code, fake ffmpeg runner) ----------------

class _Proc:
    def __init__(self, out=b"", rc=0):
        self.stdout = out
        self.returncode = rc


def _scripted_runner(frames_by_seek, duration="8.0"):
    """Fake runner: ffprobe answers `duration`; ffmpeg answers per -ss value."""
    calls = []
    def runner(cmd, timeout):
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return _Proc(duration.encode())
        ss = cmd[cmd.index("-ss") + 1]
        return _Proc(frames_by_seek.get(float(ss), b""))
    return runner, calls


def _frame(mean_target, contrast):
    """JPEG frame with roughly the given luma mean and controllable detail."""
    size = 240
    img = Image.new("RGB", (size, size))
    img.putdata([(int(mean_target + ((x * 7 + y * 13) % 2 and contrast or -contrast)),
                  int(mean_target), int(mean_target))
                 for y in range(size) for x in range(size)])
    return _jpeg(img)


def test_best_frame_wins_over_first_acceptable():
    """Every candidate within budget is scored; the balanced brightness/detail
    winner is served even when a merely-acceptable frame appears first."""
    good = _frame(122, 50)       # near target luma, decent detail
    ok_first = _frame(200, 30)   # acceptable but bright, less detail
    frames = {0.25: ok_first, 1.0: good, 2.5: good, 5.0: ok_first,
              1.2: ok_first, 2.8: good, 4.4: good, 6.4: good}
    runner, calls = _scripted_runner(frames)
    chosen = vt.representative_frame("/tmp/fake.mp4", runner=runner)
    assert chosen is not None
    s_chosen = vt._decode_stats(chosen)
    s_ok = vt._decode_stats(ok_first)
    assert abs(s_chosen[0] - 125) < abs(s_ok[0] - 125)
    # duration was probed and duration-fraction seeks were issued
    assert calls[0][0] == "ffprobe"
    seeks = [float(c[c.index("-ss") + 1]) for c in calls if c[0] == "ffmpeg"]
    assert 2.5 in seeks and any(s > 5.0 for s in seeks)


def test_representative_frame_returns_none_when_all_uninformative():
    runner, _ = _scripted_runner({0.25: BLACK, 1.0: BLACK, 2.5: WASHED})
    assert vt.representative_frame("/tmp/fake.mp4", runner=runner) is None


def test_total_ffmpeg_time_is_bounded():
    """Total wall budget is enforced: extraction stops when the shared
    deadline is exhausted, rather than 20s being spent per timestamp."""
    # t=0: deadline 18s. t=0: probe with full budget. t=10: first extraction
    # gets min(run cap, 8s remaining). t=19: shared deadline exhausted -> stop.
    ticks = iter([0.0, 0.0, 10.0, 19.0, 19.5, 20.0, 21.0])
    seen_timeouts = []
    base_runner, calls = _scripted_runner({0.25: REAL}, duration=None)
    def runner(cmd, timeout):
        seen_timeouts.append((cmd[0], timeout))
        return base_runner(cmd, timeout)
    out = vt.representative_frame("/tmp/fake.mp4", runner=runner,
                                  clock=lambda: next(ticks))
    assert out is not None                       # the one extracted frame wins
    ffmpeg_timeouts = [t for c, t in seen_timeouts if c == "ffmpeg"]
    assert len(ffmpeg_timeouts) == 1             # stopped at the shared deadline
    assert ffmpeg_timeouts[0] <= vt.FFMPEG_RUN_CAP_SEC
    assert ffmpeg_timeouts[0] <= vt.FFMPEG_TOTAL_BUDGET_SEC - 10.0


def test_max_source_bytes_is_much_smaller_than_300mb():
    assert vt.MAX_SOURCE_BYTES <= 60 * 1024 * 1024
    assert vt.FFMPEG_TOTAL_BUDGET_SEC < 20


# ---- real ffmpeg smoke test (only when the binary exists) -------------------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_ffmpeg_generated_video_smoke(tmp_path):
    """End-to-end: generate a short real video, extract a representative frame
    with the actual ffmpeg path, and verify it decodes as an informative,
    byte-bounded JPEG."""
    vid = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=10:duration=3",
         "-pix_fmt", "yuv420p", str(vid)], check=True)
    frame = vt.representative_frame(str(vid))
    assert frame is not None and len(frame) <= vt.THUMB_MAX_BYTES
    assert vt.is_informative_image(frame)
    with Image.open(io.BytesIO(frame)) as img:
        assert img.format == "JPEG" and max(img.size) <= vt.THUMB_MAX_DIM
