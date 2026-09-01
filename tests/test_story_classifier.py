"""
Story Studio Wave 1: the raw/finished/ambiguous classifier + the re-ingest guard.
Covers ECHO_STORY_STUDIO_BUILD §6 classifier cases and the intent-beats-inference and
re-ingest rails.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import ocr_check as ocr  # noqa: E402
from agent import story_classifier as sc  # noqa: E402


def _never(_hash):
    return False


# ---- §6 exact examples -----------------------------------------------------
def test_burned_caption_is_finished():
    sig = sc.Signals(filename="clip.mp4", width=1080, height=1920,
                     duration_sec=22, has_burned_text=True)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED
    assert any("overlay" in r.lower() for r in v.reasons)


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


# ---- 2026-09-01 regression fixtures: the 3 real proof-run misses ------------
# Tonight's Story Studio proof run pulled 3 already-finished, captioned clips out
# of Pierce Fitness's "raw footage" pool (media_asset, gym_id='piercefitness',
# source_id='908dca34afc34c7eafc69bcb12ac3b84'). Real metadata pulled straight off
# the media_asset rows (title/duration_sec/width/height); has_burned_text=True
# below is what agent/story_classifier.default_ocr_reader actually returns on
# these clips' frames once wired live (agent/jobs/sync_gym_media.py) — confirmed
# by hand: every one of the 3 carries real, meaningful rendered text or a
# composited graphic on top of the footage. These must NEVER again classify as
# RAW or AMBIGUOUS; a FINISHED verdict here is what
# agent/jobs/sync_gym_media.py's _quarantine_finished then excludes from the pool.

def test_p1_1_original21_burned_caption_and_banner_is_finished():
    # media_asset id 1swae9433P9jlS7gC7C59yWvmdwC8Qeee, "original (21).mp4":
    # burned-in "you are your own greatest project" plus a benefit list already
    # on the frame (verified by hand from the proof run's real frame extraction).
    sig = sc.Signals(filename="original (21).mp4", width=1080, height=1920,
                     duration_sec=10.002868, has_burned_text=True)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED


def test_p1_3_original35_burned_caption_is_finished():
    # media_asset id 1W3xnT-D6sFA1TuB-T2kaCoFLqlys3euI, "original (35).mp4":
    # burned-in "Everyone here started exactly where you are. / Your progress
    # matters most." (verified by hand from the proof run's real frame extraction).
    sig = sc.Signals(filename="original (35).mp4", width=1080, height=1920,
                     duration_sec=13.607684, has_burned_text=True)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED


def test_r2_dark_original48_composited_fake_sms_is_finished():
    # media_asset id 170XY3F7LHJZlJdsF8_aSe2gPYnq_SPu4, "original (48).mp4": a
    # fully composited fake-SMS marketing graphic ("Okay... I just booked. Let's
    # do this") over dimmed footage — not real footage at all, a designed asset.
    # The chat bubbles carry substantial rendered text, so OCR catches this the
    # same way it catches P1-1/P1-3 (verified by hand from the real frame).
    sig = sc.Signals(filename="original (48).mp4", width=1080, height=1920,
                     duration_sec=12.2, has_burned_text=True)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED


def test_these_3_clips_score_only_ambiguous_from_metadata_alone():
    # The OTHER half of the root cause: with NO vision signal wired in at all, these
    # 3 clips' metadata alone is not enough to call them anything -- classify() reads
    # AMBIGUOUS and defers to a human. (Before 2026-09-01 the dropped "9:16 and
    # 3..60s" signal gave them finished=0.45, which the echo-auto-sort tie-break
    # would then have resolved to "finished" off a signal that meant nothing; now
    # metadata alone genuinely carries no vote either way.)
    for title, dur in (
        ("original (21).mp4", 10.002868),
        ("original (35).mp4", 13.607684),
        ("original (48).mp4", 12.2),
    ):
        sig = sc.Signals(filename=title, width=1080, height=1920, duration_sec=dur)
        v = sc.classify(sig, ledger_lookup=_never)
        assert v.verdict == sc.AMBIGUOUS, title
        assert v.finished_score == 0.0 and v.raw_score == 0.0, title


# ---- 2026-09-01 regression: the CrossFit Reverb near-wipe -------------------
# A fleet-wide dry-run quarantined 31 of the 34 clips it reached in Reverb's raw
# pool. Every one was ordinary phone footage off the gym floor (IMG_47xx.MP4), and
# the signal that condemned them was "burned-in text found (OCR)" -- which at the
# time meant nothing more than "this frame has legible text on it". A CrossFit box
# is wall-to-wall text: programming whiteboards, class signage, workout timers.
# Applied for real, this would have emptied a client's Story pool on the launch day
# of the feature that needs it. These tests pin all three legs of the fix.

def test_reverb_camera_native_clip_with_only_in_scene_text_is_raw():
    # IMG_4902.MP4.mov, 9:16, 36s, whiteboard visible: the vision reader now answers
    # the overlay question and says no (has_burned_text False), so this is RAW.
    sig = sc.Signals(filename="IMG_4902.MP4.mov", width=1080, height=1920,
                     duration_sec=36.0017, has_burned_text=False)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.RAW


def test_a_single_overlay_hit_cannot_flip_a_camera_native_original():
    # THE guard. Even if the vision call misfires OVERLAY on a phone original, one
    # fallible signal must not condemn it: 0.55 finished vs 0.5 raw is inside
    # _DECIDE_MARGIN -> AMBIGUOUS (a human sorts it), never a silent quarantine.
    sig = sc.Signals(filename="IMG_4902.MP4.mov", width=1080, height=1920,
                     duration_sec=36.0017, has_burned_text=True)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.AMBIGUOUS


def test_two_independent_finished_signals_do_beat_a_camera_native_name():
    # Corroboration works in the other direction too: overlay text AND real cut
    # density on a long-enough clip is genuine evidence somebody edited this, even
    # though the phone filename survived.
    sig = sc.Signals(filename="IMG_4902.MP4.mov", width=1080, height=1920,
                     duration_sec=36.0017, has_burned_text=True, cut_density=0.5)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.FINISHED


def test_cut_density_on_a_short_clip_is_ignored_as_noise():
    # IMG_4908.MP4.mov: 3.4s, "0.59 cuts/sec" is 2 ffmpeg scene hits on a clip that
    # short -- camera movement, not editing. The signal must not count at all.
    sig = sc.Signals(filename="IMG_4908.MP4.mov", width=1080, height=1920,
                     duration_sec=3.4, has_burned_text=False, cut_density=0.59)
    v = sc.classify(sig, ledger_lookup=_never)
    assert v.verdict == sc.RAW
    assert any("ignored" in r for r in v.reasons)


def test_vertical_in_band_duration_is_no_longer_a_finished_signal():
    # The dropped signal, pinned so it cannot quietly return: a 9:16 clip of
    # in-band length, with nothing else known, votes for nothing.
    sig = sc.Signals(filename="movie.mp4", width=1080, height=1920, duration_sec=20)
    f, r, reasons = sc.score_signals(sig)
    assert f == 0.0 and r == 0.0
    assert not any("3..60" in x for x in reasons)


# ---- 2026-09-01: the real, live OCR / cut-density probes -------------------
def test_default_ocr_reader_none_when_reader_unarmed(monkeypatch):
    # creative_studio / nano off (no API key) -> the reader is None -> unknown
    # signal, never a fabricated verdict.
    monkeypatch.setattr("agent.ocr_check.overlay_reader", lambda: None)
    assert sc.default_ocr_reader("/does/not/matter") is None


def test_default_ocr_reader_true_on_post_production_overlay(monkeypatch, tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    monkeypatch.setattr(sc, "_probe_duration", lambda p: 10.0)
    monkeypatch.setattr(sc, "_extract_frame", lambda p, ts: str(fake_video))
    monkeypatch.setattr("agent.ocr_check.overlay_reader", lambda: (lambda b: "OVERLAY"))
    assert sc.default_ocr_reader(str(fake_video)) is True


def test_default_ocr_reader_false_on_in_scene_text(monkeypatch, tmp_path):
    # The Reverb case at the reader level: the frame HAS text, but it is a whiteboard.
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    monkeypatch.setattr(sc, "_probe_duration", lambda p: 10.0)
    monkeypatch.setattr(sc, "_extract_frame", lambda p, ts: str(fake_video))
    monkeypatch.setattr("agent.ocr_check.overlay_reader", lambda: (lambda b: "SCENE"))
    assert sc.default_ocr_reader(str(fake_video)) is False


def test_default_ocr_reader_unknown_when_the_model_answers_unparseably(
        monkeypatch, tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    monkeypatch.setattr(sc, "_probe_duration", lambda p: 10.0)
    monkeypatch.setattr(sc, "_extract_frame", lambda p, ts: str(fake_video))
    monkeypatch.setattr("agent.ocr_check.overlay_reader",
                        lambda: (lambda b: "I think maybe there is some text?"))
    assert sc.default_ocr_reader(str(fake_video)) is None


def test_one_overlay_frame_wins_over_a_clean_first_frame(monkeypatch, tmp_path):
    # A caption that only appears later in the clip still makes it finished; the
    # reader samples 2 frames and True on either is enough.
    answers = iter(["SCENE", "OVERLAY"])
    frames = iter(["f0.png", "f1.png"])

    def _fresh_frame(_path, _ts):
        # a NEW file per call: default_ocr_reader removes each frame after reading
        # it, so reusing one path would make the second read fail on a missing file.
        p = tmp_path / next(frames)
        p.write_bytes(b"x")
        return str(p)

    monkeypatch.setattr(sc, "_probe_duration", lambda p: 10.0)
    monkeypatch.setattr(sc, "_extract_frame", _fresh_frame)
    monkeypatch.setattr("agent.ocr_check.overlay_reader",
                        lambda: (lambda b: next(answers)))
    assert sc.default_ocr_reader("/clip.mp4") is True


# ---- the overlay answer parser (agent/ocr_check) ---------------------------
def test_parse_overlay_answer_reads_the_three_verdicts():
    assert ocr.parse_overlay_answer("OVERLAY") == ocr.OVERLAY
    assert ocr.parse_overlay_answer("scene") == ocr.SCENE
    assert ocr.parse_overlay_answer(" None.\n") == ocr.NONE


def test_parse_overlay_answer_refuses_to_guess():
    # Unrecognized, empty, or self-contradicting replies are UNKNOWN, not a vote.
    for bad in ("", None, "probably some text", "OVERLAY or maybe SCENE"):
        assert ocr.parse_overlay_answer(bad) is None


def test_overlay_verdict_is_none_when_unarmed():
    assert ocr.overlay_verdict("/x", reader=None) is None
