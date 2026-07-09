"""
Native clipper tests (Phase 1: selection). Fully OFFLINE: fake R2 client, fake
transcriber, fake LLM. No network, no spend, no key value ever printed.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import clipper, config  # noqa: E402


class _FakeClient:
    """R2 stand-in: records puts, answers exists from a known key set."""

    def __init__(self, present=()):
        self.present = set(present)
        self.puts = []

    def exists(self, key):
        return key in self.present

    def put(self, key, local_path):
        self.puts.append((key, local_path))
        self.present.add(key)


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_CLIPPER_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    # S3_PUBLIC_BASE_URL is a module constant captured at import; set it directly.
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")


# ---- Part 1: episode intake ---------------------------------------------------------

def test_intake_resolves_local_path(monkeypatch, tmp_path):
    _arm(monkeypatch)
    ep = tmp_path / "episode.mp4"
    ep.write_bytes(b"FAKE VIDEO BYTES")
    client = _FakeClient()
    out = clipper.stage_episode(str(ep), tenant="lasso_episodes", client=client)
    assert out["staged"] is True
    assert out["r2_key"].startswith("echo/lasso_episodes/")
    assert out["r2_key"].endswith("episode.mp4")
    assert out["public_url"].startswith("https://cdn.echo.test/echo/lasso_episodes/")
    assert client.puts and client.puts[0][0] == out["r2_key"]   # uploaded once


def test_intake_resolves_existing_r2_key(monkeypatch):
    _arm(monkeypatch)
    key = "echo/lasso_episodes/abc123/episode.mp4"
    client = _FakeClient(present=[key])
    out = clipper.stage_episode(key, client=client)
    assert out["staged"] is False                    # already in R2, not re-uploaded
    assert out["r2_key"] == key
    assert out["public_url"] == "https://cdn.echo.test/" + key
    assert client.puts == []                          # read-only on an existing key


def test_intake_rejects_missing_source(monkeypatch):
    _arm(monkeypatch)
    client = _FakeClient()
    with pytest.raises(clipper.ClipperError):
        clipper.stage_episode("echo/does/not/exist.mp4", client=client)


def test_intake_rejects_non_video_file(monkeypatch, tmp_path):
    _arm(monkeypatch)
    doc = tmp_path / "notes.txt"
    doc.write_text("not a video")
    with pytest.raises(clipper.ClipperError):
        clipper.stage_episode(str(doc), client=_FakeClient())


def test_clip_episode_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CLIPPER_ENABLED", raising=False)
    ep = tmp_path / "e.mp4"
    ep.write_bytes(b"x")
    assert clipper.clip_episode(str(ep)) is None


def test_clip_episode_stages_when_on(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    ep = tmp_path / "e.mp4"
    ep.write_bytes(b"FAKE")
    client = _FakeClient()
    out = clipper.clip_episode(str(ep), client=client,
                               transcriber=_fake_transcriber(),
                               llm=_llm_returning([]))
    assert out["staged"]["staged"] is True
    assert out["transcript"]["words"]
    assert "selection" in out
    assert "staged episode" in capsys.readouterr().out


# ---- Part 2: transcription with word-level timestamps + caching ---------------------

def _fake_transcriber(calls=None):
    """A transcriber returning word-level timestamps; counts invocations if a list
    is passed in (to prove caching)."""
    def _t(media_path):
        if calls is not None:
            calls.append(media_path)
        return {
            "words": [
                {"word": "Most", "start": 0.0, "end": 0.3},
                {"word": "gyms", "start": 0.3, "end": 0.7},
                {"word": "have", "start": 0.7, "end": 1.0},
                {"word": "a", "start": 1.0, "end": 1.1},
                {"word": "follow", "start": 1.1, "end": 1.5},
                {"word": "up", "start": 1.5, "end": 1.8},
                {"word": "problem", "start": 1.8, "end": 2.4},
            ],
            "segments": [{"speaker": "SPEAKER_0", "start": 0.0, "end": 2.4,
                          "text": "Most gyms have a follow up problem"}],
        }
    return _t


def test_transcribe_returns_word_timestamps(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    media = tmp_path / "e.mp4"
    media.write_bytes(b"FAKE")
    t = clipper.transcribe("echo/ep/abc/e.mp4", media_path=str(media),
                           transcriber=_fake_transcriber())
    assert t["words"][0] == {"word": "Most", "start": 0.0, "end": 0.3}
    assert all("start" in w and "end" in w for w in t["words"])
    assert t["segments"][0]["speaker"] == "SPEAKER_0"


def test_transcribe_caches_on_r2_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    media = tmp_path / "e.mp4"
    media.write_bytes(b"FAKE")
    calls = []
    tr = _fake_transcriber(calls)
    clipper.transcribe("echo/ep/abc/e.mp4", media_path=str(media), transcriber=tr)
    clipper.transcribe("echo/ep/abc/e.mp4", media_path=str(media), transcriber=tr)
    assert len(calls) == 1                            # second run hit the cache


def test_transcribe_needs_media_on_cache_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    with pytest.raises(clipper.ClipperError):
        clipper.transcribe("echo/ep/none/e.mp4", media_path=None,
                           transcriber=_fake_transcriber())


def test_transcribe_rejects_missing_word_timestamps(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    media = tmp_path / "e.mp4"
    media.write_bytes(b"FAKE")

    def _bad(media_path):
        return {"words": [{"word": "hi"}], "segments": []}   # no start/end

    with pytest.raises(clipper.ClipperError):
        clipper.transcribe("echo/ep/bad/e.mp4", media_path=str(media), transcriber=_bad)


# ---- Part 3: Claude moment selection ------------------------------------------------

# A transcript where every word carries a timestamp, long enough to slice 30-90s.
def _long_transcript():
    words, t = [], 0.0
    sentence = ("most gyms do not have a lead problem they have a follow up "
                "problem fix your follow up and you win more members").split()
    for i in range(120):                       # ~ enough words spanning >120s
        w = sentence[i % len(sentence)]
        words.append({"word": w, "start": round(t, 2), "end": round(t + 1.0, 2)})
        t += 1.0
    return {"words": words, "segments": [
        {"speaker": "S0", "start": 0.0, "end": t, "text": " ".join(
            w["word"] for w in words)}]}


def _llm_returning(moments):
    def _llm(system, user):
        return json.dumps({"moments": moments})
    return _llm


def test_select_returns_structured_candidates(monkeypatch):
    monkeypatch.setenv("AGENT_CLIPPER_SCORE_FLOOR", "80")
    t = _long_transcript()
    llm = _llm_returning([
        {"start_ts": 0.0, "end_ts": 40.0, "hook": "most gyms",
         "rationale": "opens on a strong claim about follow up; scored high because "
                      "it stands alone", "bucket": "doctrine", "score": 92},
        {"start_ts": 45.0, "end_ts": 100.0, "hook": "fix your follow up",
         "rationale": "clear payoff, self contained", "bucket": "platform",
         "score": 85},
    ])
    out = clipper.select_moments(t, llm=llm)
    assert len(out["accepted"]) == 2
    top = out["accepted"][0]
    assert top.score == 92 and top.bucket == "doctrine"
    assert top.duration == 40.0
    assert top.transcript_text                         # exact segment text attached
    assert out["accepted"][0].score >= out["accepted"][1].score   # ranked


def test_select_drops_below_score_floor(monkeypatch):
    monkeypatch.setenv("AGENT_CLIPPER_SCORE_FLOOR", "80")
    t = _long_transcript()
    llm = _llm_returning([
        {"start_ts": 0.0, "end_ts": 40.0, "hook": "most gyms",
         "rationale": "weak, marginal moment", "bucket": "doctrine", "score": 71}])
    out = clipper.select_moments(t, llm=llm)
    assert out["accepted"] == []
    assert out["dropped"][0].reason.startswith("score 71 below floor")


def test_select_drops_out_of_duration_window(monkeypatch):
    monkeypatch.setenv("AGENT_CLIPPER_SCORE_FLOOR", "80")
    monkeypatch.setenv("AGENT_CLIPPER_MIN_SEC", "30")
    monkeypatch.setenv("AGENT_CLIPPER_MAX_SEC", "90")
    t = _long_transcript()
    llm = _llm_returning([
        {"start_ts": 0.0, "end_ts": 8.0, "hook": "too short",
         "rationale": "great but tiny", "bucket": "doctrine", "score": 95}])
    out = clipper.select_moments(t, llm=llm)
    assert out["accepted"] == []
    assert "outside window" in out["dropped"][0].reason


def test_select_rejects_off_transcript_claim(monkeypatch):
    """A moment whose rationale asserts a stat NOT in the transcript is rejected by
    the fabrication gate."""
    monkeypatch.setenv("AGENT_CLIPPER_SCORE_FLOOR", "80")
    t = _long_transcript()                              # says nothing about body fat
    llm = _llm_returning([
        {"start_ts": 0.0, "end_ts": 40.0, "hook": "most gyms",
         "rationale": "This clip proves members lose 20% body fat in 6 weeks.",
         "bucket": "doctrine", "score": 96}])
    out = clipper.select_moments(t, llm=llm, approved_claims=[clipper.transcript_text(t)])
    assert out["accepted"] == []
    assert "not in the transcript" in out["dropped"][0].reason


def test_select_accepts_verbatim_transcript_claim(monkeypatch):
    """A hook/rationale that only restates transcript wording passes the gate."""
    monkeypatch.setenv("AGENT_CLIPPER_SCORE_FLOOR", "80")
    t = {"words": [
        {"word": "we", "start": 0.0, "end": 0.4},
        {"word": "book", "start": 0.4, "end": 0.8},
        {"word": "71", "start": 0.8, "end": 1.1},
        {"word": "percent", "start": 1.1, "end": 1.6},
        {"word": "of", "start": 1.6, "end": 1.8},
        {"word": "leads", "start": 1.8, "end": 2.2},
    ] + [{"word": "x", "start": 2.2 + i, "end": 3.2 + i} for i in range(40)],
        "segments": []}
    llm = _llm_returning([
        {"start_ts": 0.0, "end_ts": 40.0, "hook": "we book 71 percent of leads",
         "rationale": "strong number that is spoken in the clip", "bucket": "platform",
         "score": 90}])
    out = clipper.select_moments(t, llm=llm, approved_claims=[clipper.transcript_text(t)])
    assert len(out["accepted"]) == 1                    # 71 percent is in transcript


# ---- Part 4: dry-run plan output ----------------------------------------------------

def test_dry_run_prints_full_plan(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGENT_CLIPPER_SCORE_FLOOR", "80")
    ep = tmp_path / "e.mp4"
    ep.write_bytes(b"FAKE")

    def _transcriber(_p):
        return _long_transcript()

    llm = _llm_returning([
        {"start_ts": 0.0, "end_ts": 40.0, "hook": "most gyms",
         "rationale": "opens on a strong claim, self contained payoff",
         "bucket": "doctrine", "score": 92},
        {"start_ts": 5.0, "end_ts": 12.0, "hook": "too short",
         "rationale": "tiny", "bucket": "doctrine", "score": 95},
    ])
    out = clipper.clip_episode(str(ep), client=_FakeClient(),
                               transcriber=_transcriber, llm=llm)
    printed = capsys.readouterr().out
    # the plan shows timestamps, duration, score, hook, bucket, rationale, text
    assert "PLAN (SELECTION ONLY" in printed
    assert "score 92" in printed and "doctrine" in printed
    assert "[0:00-0:40]" in printed and "40s" in printed
    assert "hook : most gyms" in printed
    assert "why  : opens on a strong claim" in printed
    assert "text :" in printed
    assert "dropped:" in printed and "outside window" in printed  # short pick dropped
    assert len(out["selection"]["accepted"]) == 1


def test_dry_run_renders_and_writes_nothing(monkeypatch, tmp_path):
    """Phase 1 dry-run: no store, no render artifact, only the transcript cache."""
    _arm(monkeypatch)
    cache = tmp_path / "cache"
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(cache))
    ep = tmp_path / "e.mp4"
    ep.write_bytes(b"FAKE")
    client = _FakeClient()
    clipper.clip_episode(str(ep), client=client, transcriber=_fake_transcriber(),
                         llm=_llm_returning([]))
    # nothing rendered: the only artifacts are the staged upload (via fake client,
    # no local file) and the transcript cache json. No .mp4 clips written locally.
    produced = list(tmp_path.rglob("*.mp4"))
    assert produced == [ep]                              # only the source episode
    cache_files = list(cache.rglob("*.transcript.json"))
    assert len(cache_files) == 1                          # transcript cache only


def test_render_flag_says_phase_two(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    ep = tmp_path / "e.mp4"
    ep.write_bytes(b"FAKE")
    clipper.clip_episode(str(ep), render=True, client=_FakeClient(),
                         transcriber=_fake_transcriber(), llm=_llm_returning([]))
    assert "Phase 2" in capsys.readouterr().out
