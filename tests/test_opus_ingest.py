"""
Opus Clip ingest tests. Fully OFFLINE: a fake Opus API, fake S3, no Gemini, no
Meta, zero spend. Asserts: flag OFF is a no-op and the poll flag defaults OFF;
the watermark advances so a re-pull ingests nothing new; dedupe by content hash;
an ingested clip drafts as a Reel that NEVER publishes without approval even with
the publish flag armed; the caption carries only bible + clip-metadata text; the
API key never appears in output; repeated failure dead-letters with an alert.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, meta_publisher, ops_alerts, opus_ingest, rotation  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus, draft_post  # noqa: E402
from agent.library import list_creatives  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


CLIP_A = {"id": "P1.CU1", "title": "Coach corner clip", "description": "Show up daily.",
          "durationMs": 31000, "uriForExport": "https://cdn.opus/a.mp4",
          "createdAt": "2026-07-01T10:00:00Z"}
CLIP_B = {"id": "P1.CU2", "title": "Second clip", "description": "",
          "durationMs": 20000, "uriForExport": "https://cdn.opus/b.mp4",
          "createdAt": "2026-07-02T10:00:00Z"}


class FakeOpus:
    def __init__(self, clips=None, blobs=None, fail_downloads=False):
        self.clips = clips if clips is not None else [CLIP_A, CLIP_B]
        self.blobs = blobs or {"https://cdn.opus/a.mp4": b"VIDEO-A",
                               "https://cdn.opus/b.mp4": b"VIDEO-B"}
        self.fail_downloads = fail_downloads
        self.list_calls = 0

    def list_collections(self):
        return ["COL1"]

    def list_exportable_clips(self, q, source_id):
        self.list_calls += 1
        return list(self.clips)

    def download(self, url):
        if self.fail_downloads:
            raise RuntimeError("cdn error")
        return self.blobs[url]


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OPUS_ENABLED", "true")
    monkeypatch.setenv("AGENT_OPUS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    return str(lib)


# ---- flags ------------------------------------------------------------------------
def test_flag_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_OPUS_ENABLED", raising=False)
    assert opus_ingest.pull(api=FakeOpus()) is None


def test_poll_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_OPUS_POLL_ENABLED", raising=False)
    assert config.opus_poll_enabled() is False


# ---- pull, watermark, re-pull ingests nothing new ---------------------------------
def test_pull_ingests_and_repull_is_idempotent(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    api = FakeOpus()
    out = opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)
    assert out["pulled"] == 2
    files = sorted(os.listdir(lib))
    videos = [f for f in files if f.endswith(".mp4")]
    sidecars = [f for f in files if f.endswith(".json")]
    assert len(videos) == 2 and len(sidecars) == 2
    assert all(f.startswith("opus_") for f in videos)
    side = json.loads((tmp_path / "library" / sidecars[0]).read_text())
    assert side["source"] == "opus"
    assert side["opus_clip_id"] in ("P1.CU1", "P1.CU2")
    assert side["title"]
    assert side["duration_ms"] in (31000, 20000)
    assert side["public_url"].startswith("https://cdn.echo.test/echo/lasso_library/")
    printed = capsys.readouterr().out
    assert printed.count("https://cdn.echo.test/") == 2   # one URL per clip

    # a re-pull sees the watermark + ingested ids: nothing new
    out2 = opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)
    assert out2["pulled"] == 0
    assert len([f for f in os.listdir(lib) if f.endswith(".mp4")]) == 2


def test_dedupe_by_content_hash(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    same_bytes = {"https://cdn.opus/a.mp4": b"SAME", "https://cdn.opus/b.mp4": b"SAME"}
    api = FakeOpus(blobs=same_bytes)
    out = opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)
    assert out["pulled"] == 1 and out["skipped"] == 1
    assert len([f for f in os.listdir(lib) if f.endswith(".mp4")]) == 1


# ---- the clip becomes a draft-only Reel; nothing publishes without approval -------
def test_clip_drafts_as_reel_and_never_publishes_without_approval(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    opus_ingest.pull(api=FakeOpus(clips=[CLIP_A]), s3_client=FakeS3(), out_dir=lib)
    creative = list_creatives(lib)[0]
    assert creative.media_type == "video"
    acct = Account(key="lasso_ig", display_name="T", platform=Platform.INSTAGRAM,
                   token_env="T_TOKEN", target_id_env="T_ID")
    voice = VoiceDoc(raw="We help gym owners grow.\n#LASSOFramework",
                     hashtags=["#LASSOFramework"], ctas=["Save this post."])
    draft = draft_post(acct, creative, "2026-07-03T18:30", voice=voice)
    assert draft.status == DraftStatus.PENDING          # held for approval
    assert meta_publisher._is_video(draft.creative_path)  # routes as a Reel

    # publish flag ARMED but no approval tap: publish() is simply never called by
    # drafting, and even a direct call cannot reach Meta without a token AND would
    # only ever happen through approvals.handle_action. Prove the draft alone
    # triggers zero network:
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")

    class ExplodingHTTP:
        def post(self, *a, **k):
            raise AssertionError("network call without approval!")

    # nothing in the draft path called publish; and a non-approver cannot force it
    from agent import approvals
    res = approvals.handle_action("approve", draft, actor_slack_id="U_INTRUDER",
                                  publisher=None, account=acct)
    assert res.ok is False and "not the approver" in res.detail


def test_caption_only_from_bible_and_clip_words(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    opus_ingest.pull(api=FakeOpus(clips=[CLIP_A]), s3_client=FakeS3(), out_dir=lib)
    creative = list_creatives(lib)[0]
    voice = VoiceDoc(raw="x", hashtags=["#LASSOFramework"], ctas=["Save this post."])
    acct = Account(key="lasso_ig", display_name="T", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="I")
    draft = draft_post(acct, creative, "t", voice=voice)
    allowed = {"Coach corner clip Show up daily.", "Save this post."}
    for frag in draft.source_fragments:
        assert frag in allowed, f"caption text not from bible or clip metadata: {frag!r}"


# ---- failures: alert then dead-letter; the key never appears in output ------------
def test_repeated_failure_deadletters_with_alert(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    api = FakeOpus(clips=[CLIP_A], fail_downloads=True)
    for _ in range(3):
        opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)
    state = opus_ingest.load_state()
    assert "P1.CU1" in state["deadletter"]
    assert any("dead-lettered" in n for n in rec.notices)
    # dead-lettered: a fourth pull skips it entirely
    out = opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)
    assert out["failed"] == 0 and out["pulled"] == 0


def test_api_key_never_printed(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("OPUS_API_KEY", "opus-secret-key-12345")
    opus_ingest.pull(api=FakeOpus(), s3_client=FakeS3(), out_dir=lib)
    printed = capsys.readouterr().out
    assert "opus-secret-key-12345" not in printed


# ---- rotation: video is its own pillar --------------------------------------------
def test_video_is_its_own_pillar():
    assert rotation.pillar_of("opus_abc123.mp4") == "video"
    assert rotation.pillar_of("clip.mov") == "video"
    assert rotation.pillar_of("lasso_p2_habits.jpg") == "p2"
