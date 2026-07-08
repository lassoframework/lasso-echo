"""
Three podcast touches per episode (category rotation Part 3).

Asserts one episode yields all three touches across its week:
  Mon release card (infographic), Thu Opus clip (video), Sun episode infographic,
each PENDING and held for the tap, each cited podcast_ep<N>. Also: clip selection
matches the episode by number in the sidecar, a stat bearing clip note is benched,
and the whole controller is inert while the flag is OFF.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, media_host, ops_alerts  # noqa: E402
from agent import podcast_feed, podcast_touches, podcast_transcripts  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402

from test_podcast_feed import FEED  # noqa: E402
from test_podcast_transcripts import TRANSCRIPT  # noqa: E402


class FakeNano:
    def __init__(self):
        self.prompts = []

    def generate_image(self, prompt, model):
        self.prompts.append(prompt)
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


def _acct():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM, token_env="X", target_id_env="Y")


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib))
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    return lib


def _write_clip(lib, stem, title, note, public_url="https://cdn.echo.test/clip.mp4"):
    (lib / f"{stem}.mp4").write_bytes(b"FAKEMP4")
    sidecar = {"source": "opus", "opus_clip_id": stem, "title": title, "note": note}
    if public_url:
        sidecar["public_url"] = public_url
    (lib / f"{stem}.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _seed_episode_7(monkeypatch, tmp_path):
    """Arm, detect episode 7, ingest its transcript, drop one matching clip."""
    lib = _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)                 # stores episodes 6 and 7
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")     # transcript for the infographic
    _write_clip(lib, "opus_ep7clip", "Episode 7 highlight",
                "Follow up wins the month.")
    return lib


# ---- the headline lock: one episode yields all three touches ----------------------------
def test_one_episode_yields_all_three_touches(monkeypatch, tmp_path):
    _seed_episode_7(monkeypatch, tmp_path)
    touches = podcast_touches.touches_for_episode(
        _acct(), 7, "2026-07-06", nano_client=FakeNano(), s3_client=FakeS3())

    assert set(touches) == {"release", "clip", "infographic"}
    assert touches["release"] is not None
    assert touches["clip"] is not None
    assert touches["infographic"] is not None

    # every touch is held for the tap; nothing is auto anything
    for key, draft in touches.items():
        assert draft.status == DraftStatus.PENDING, f"{key} not held for approval"
        assert draft.draft_type == "podcast"
        assert "cite:podcast_ep7" in draft.source_fragments, f"{key} missing citation"


def test_touches_land_on_the_right_days(monkeypatch, tmp_path):
    _seed_episode_7(monkeypatch, tmp_path)
    touches = podcast_touches.touches_for_episode(
        _acct(), 7, "2026-07-06", nano_client=FakeNano(), s3_client=FakeS3())
    assert touches["release"].scheduled_for.startswith("2026-07-06")   # Mon
    assert touches["clip"].scheduled_for.startswith("2026-07-09")      # Thu
    assert touches["infographic"].scheduled_for.startswith("2026-07-12")  # Sun


def test_release_is_infographic_clip_is_video(monkeypatch, tmp_path):
    _seed_episode_7(monkeypatch, tmp_path)
    touches = podcast_touches.touches_for_episode(
        _acct(), 7, "2026-07-06", nano_client=FakeNano(), s3_client=FakeS3())
    # the clip touch carries the video creative path (a Reel), the others a rendered card
    assert touches["clip"].creative_path.endswith(".mp4")
    assert "EPISODE 7" in touches["release"].caption
    assert "episode 7" in touches["infographic"].caption.lower()


def test_clip_caption_is_verbatim_note_dash_free(monkeypatch, tmp_path):
    _seed_episode_7(monkeypatch, tmp_path)
    touches = podcast_touches.touches_for_episode(
        _acct(), 7, "2026-07-06", nano_client=FakeNano(), s3_client=FakeS3())
    cap = touches["clip"].caption
    assert "Follow up wins the month." in cap
    assert not podcast_touches._DASH_RE.search(cap)


# ---- clip selection: matches the episode by number --------------------------------------
def test_clip_matched_by_episode_number(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    _write_clip(lib, "opus_other", "Episode 6 clip", "A different episode.")
    _write_clip(lib, "opus_seven", "Highlights from ep 7", "Follow up wins the month.")
    found = podcast_touches.clip_for_episode(7)
    assert found is not None
    path, sidecar = found
    assert path.endswith("opus_seven.mp4")


def test_no_clip_for_episode_returns_none(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    _write_clip(lib, "opus_other", "Episode 6 clip", "A different episode.")
    assert podcast_touches.clip_for_episode(7) is None


def test_clip_touch_none_when_no_matching_clip(monkeypatch, tmp_path):
    """The other two touches still build; only the clip touch is None."""
    lib = _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    # no clip written for episode 7
    touches = podcast_touches.touches_for_episode(
        _acct(), 7, "2026-07-06", nano_client=FakeNano(), s3_client=FakeS3())
    assert touches["clip"] is None
    assert touches["release"] is not None
    assert touches["infographic"] is not None


# ---- fabrication gate: a stat bearing clip note is benched ------------------------------
def test_clip_with_unverified_stat_note_is_benched(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    _write_clip(lib, "opus_seven", "Episode 7 clip",
                "This clip drove a 300% lift in bookings.")
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    draft = podcast_touches.build_clip_touch(_acct(), 7, "2026-07-09",
                                             s3_client=FakeS3())
    assert draft is None
    assert len(fired) == 1
    assert "benched" in fired[0].lower() or "unverified" in fired[0].lower()


# ---- flag off = inert -------------------------------------------------------------------
def test_flag_off_touches_none(monkeypatch, tmp_path):
    _seed_episode_7(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    assert podcast_touches.touches_for_episode(
        _acct(), 7, "2026-07-06", nano_client=FakeNano(), s3_client=FakeS3()) is None
    assert podcast_touches.build_clip_touch(_acct(), 7, "2026-07-09",
                                            s3_client=FakeS3()) is None


def test_episode_card_none_without_transcript(monkeypatch, tmp_path):
    from agent import podcast_cards
    _arm(monkeypatch, tmp_path)
    # no transcript ingested for episode 42
    assert podcast_cards.build_episode_card(
        _acct(), 42, "2026-07-12", nano_client=FakeNano(), s3_client=FakeS3()) is None
