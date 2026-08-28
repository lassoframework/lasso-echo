"""
Story Studio Wave 1: the raw/finished/ambiguous classifier + the re-ingest guard.
Covers ECHO_STORY_STUDIO_BUILD §6 classifier cases and the intent-beats-inference and
re-ingest rails.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_classifier as sc  # noqa: E402


def _never(_hash):
    return False


# ---- §6 exact examples -----------------------------------------------------
def test_burned_caption_is_finished():
    sig = sc.Signals(filename="clip.mp4", width=1080, height=1920,
                     duration_sec=22, has_burned_text=True)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED
    assert any("burned" in r.lower() for r in v.reasons)


def test_img4021_mov_16x9_3min_is_raw():
    # IMG_4021.MOV, 16:9, 3 minutes -> raw (camera-native name + landscape + >90s).
    sig = sc.Signals(filename="IMG_4021.MOV", width=1920, height=1080,
                     duration_sec=180, has_burned_text=False)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.RAW


def test_vertical_22s_no_text_is_ambiguous_and_never_staged():
    # 9:16, 22s, no burned text -> the spec's ambiguous example. Must NOT auto-decide.
    sig = sc.Signals(filename="movie.mp4", width=1080, height=1920,
                     duration_sec=22, has_burned_text=False)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.AMBIGUOUS


# ---- re-ingest guard -------------------------------------------------------
def test_echo_render_is_finished_and_blocked_from_reingest():
    sig = sc.Signals(filename="whatever.mp4", content_hash="deadbeef",
                     width=1080, height=1920, duration_sec=30)
    v = sc.classify(sig, ledger_lookup=lambda h: h == "deadbeef")
    assert v.verdict == sc.FINISHED
    assert v.is_echo_render is True


def test_reingest_guard_overrides_a_declared_raw_lane():
    # Even a file declared 'raw' is blocked if its bytes are Echo's own render.
    sig = sc.Signals(filename="x.mp4", content_hash="abc123")
    v = sc.classify(sig, declared_lane=sc.LANE_RAW, ledger_lookup=lambda h: True)
    assert v.is_echo_render is True
    assert v.verdict == sc.FINISHED


# ---- intent beats inference ------------------------------------------------
def test_declared_finished_lane_wins_over_signals():
    # signals scream raw (landscape, long, camera name) but the declared lane wins.
    sig = sc.Signals(filename="IMG_9.MOV", width=1920, height=1080, duration_sec=200)
    v = sc.classify(sig, declared_lane=sc.LANE_FINISHED, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED
    assert v.declared is True
    assert v.confidence == 1.0


def test_declared_raw_lane_wins_over_finished_signals():
    sig = sc.Signals(filename="final_export.mp4", width=1080, height=1920,
                     duration_sec=20, has_burned_text=True)
    v = sc.classify(sig, declared_lane=sc.LANE_RAW, ledger_lookup=_never)
    assert v.verdict == sc.RAW
    assert v.declared is True


# ---- filename + cut density signals ----------------------------------------
def test_camera_native_filenames():
    for name in ("IMG_4021.MOV", "DJI_0007.MP4", "GX010123.MP4", "PXL_20260101.mp4",
                 "GOPR0111.MP4"):
        assert sc.camera_native_filename(name), name
    assert not sc.camera_native_filename("BirminghamHyrox_final.mp4")


def test_high_cut_density_pushes_finished():
    sig = sc.Signals(filename="movie.mp4", width=1080, height=1920,
                     duration_sec=25, has_burned_text=False, cut_density=0.4)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED


def test_missing_signals_degrade_to_ambiguous_not_crash():
    # nothing known but a neutral name -> ambiguous (fail to human), never a crash.
    sig = sc.Signals(filename="movie.mp4")
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.AMBIGUOUS


# ---- offline signal gathering ----------------------------------------------
def test_gather_signals_offline_leaves_probes_unknown():
    asset = {"title": "IMG_1.MOV", "content_hash": "h", "kind": "video",
             "duration_sec": 200, "width": 1920, "height": 1080}
    sig = sc.gather_signals(asset)  # no ocr_reader / cut_probe -> unknown
    assert sig.has_burned_text is None
    assert sig.cut_density is None
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.RAW  # still decides from metadata alone


def test_gather_signals_uses_injected_ocr():
    asset = {"title": "movie.mp4", "content_hash": "h", "kind": "video",
             "duration_sec": 20, "width": 1080, "height": 1920}
    sig = sc.gather_signals(asset, local_path="/x", ocr_reader=lambda p: True)
    assert sig.has_burned_text is True
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED
