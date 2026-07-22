"""
Reels tests (draft-only). Same contracts as the gate/growth-pack tests:
no fabrication (caption from the client note), no network in draft-only mode.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import meta_publisher, slack_surface  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus, draft_post  # noqa: E402
from agent.library import Creative  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


# ---- helpers ----------------------------------------------------------------
def _acct(platform=Platform.INSTAGRAM, key="t_ig"):
    return Account(key=key, display_name="T", platform=platform,
                   token_env="T_TOKEN", target_id_env="T_ID")


def _voice():
    return VoiceDoc(raw="We help gym owners grow without burning out.\n#LASSO",
                    hashtags=["#LASSO"])


# ---- 1. video detection -----------------------------------------------------
def test_is_video_detection():
    assert meta_publisher._is_video("promo.mp4") is True
    assert meta_publisher._is_video("promo.MOV") is True          # case-insensitive
    assert meta_publisher._is_video("https://cdn.example.com/a.MP4") is True
    assert meta_publisher._is_video("card.jpg") is False
    assert meta_publisher._is_video("") is False
    assert meta_publisher._is_video(None) is False


# ---- 2. a video creative drafts PENDING, caption from the note (no fabrication) ----
def test_video_creative_drafts_pending_with_note_caption():
    note = "Book your free intro consult this Saturday."
    creative = Creative(path="/lib/promo.mp4", media_type="video", client_note=note,
                        public_url="https://cdn.example.com/promo.mp4")
    d = draft_post(_acct(), creative, "2026-07-01T09:00", voice=_voice())
    assert d.status == DraftStatus.PENDING
    assert note in d.caption                    # caption carries the client note verbatim
    assert d.creative_path == "/lib/promo.mp4"


# ---- 3. reel publish is dormant in draft-only (no network client passed) -----
def test_reel_publish_would_publish_in_draft_only(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)  # publishing OFF
    d = Draft(draft_id="d", account_key="k", platform="instagram", caption="c",
              hashtags=[], creative_path="/lib/promo.mp4",
              creative_public_url="https://cdn.example.com/promo.mp4",
              scheduled_for="t")
    # No http client passed at all: publish() must short-circuit before any network.
    res = meta_publisher.publish(d, _acct())
    assert res.ok is True
    assert res.mode == "would_publish"


# ---- 4. the Slack card labels a video creative as a Reel --------------------
def test_slack_card_labels_reel():
    d = Draft(draft_id="d", account_key="k", platform="instagram", caption="c",
              hashtags=[], creative_path="/lib/promo.mp4", creative_public_url="",
              scheduled_for="t")
    blocks = slack_surface.build_card_blocks(d)
    text = "".join(
        b.get("text", {}).get("text", "")
        for b in blocks if isinstance(b.get("text"), dict)
    )
    assert "Reel — promo.mp4" in text


# ---- 5. a FB reel posts to /videos, never /photos (the "Can't Read Files" bug) ----
class _FakeResp:
    status_code = 200
    def json(self):
        return {"id": "vid_1", "post_id": "p_1"}


class _FakeHTTP:
    def __init__(self):
        self.calls = []
    def post(self, url, data=None, timeout=None):
        self.calls.append(url)
        return _FakeResp()
    def get(self, url, params=None, timeout=None):
        # Returns FINISHED so _await_container_ready exits immediately
        class _PollResp:
            status_code = 200
            def json(self): return {"status_code": "FINISHED"}
        return _PollResp()


def test_story_crosspost_fires_after_ig_reel(monkeypatch):
    """When AGENT_STORY_CROSSPOST_ENABLED=true, a second story POST fires after the reel."""
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_STORY_CROSSPOST_ENABLED", "true")
    monkeypatch.setenv("T_TOKEN", "tok")
    monkeypatch.setenv("T_ID", "ig123")
    d = Draft(draft_id="d", account_key="t_ig", platform="instagram", caption="c",
              hashtags=[], creative_path="/lib/reel.mp4",
              creative_public_url="https://cdn.example.com/reel.mp4",
              scheduled_for="t")
    http = _FakeHTTP()
    meta_publisher.publish(d, _acct(platform=Platform.INSTAGRAM, key="t_ig"), http=http)
    # Should see at least 4 calls: reel container, reel publish, story container, story publish
    assert len(http.calls) >= 4
    assert any("/media" in c for c in http.calls)


def test_story_crosspost_off_by_default(monkeypatch):
    """Without the flag, only the reel calls fire (no story crosspost)."""
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.delenv("AGENT_STORY_CROSSPOST_ENABLED", raising=False)
    monkeypatch.setenv("T_TOKEN", "tok")
    monkeypatch.setenv("T_ID", "ig123")
    d = Draft(draft_id="d", account_key="t_ig", platform="instagram", caption="c",
              hashtags=[], creative_path="/lib/reel.mp4",
              creative_public_url="https://cdn.example.com/reel.mp4",
              scheduled_for="t")
    http = _FakeHTTP()
    meta_publisher.publish(d, _acct(platform=Platform.INSTAGRAM, key="t_ig"), http=http)
    # Only reel container + publish = 2 calls (no story crosspost)
    assert len(http.calls) == 2


def test_fb_reel_posts_to_videos_not_photos(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("T_TOKEN", "tok")
    monkeypatch.setenv("T_ID", "page123")
    d = Draft(draft_id="d", account_key="t_fb", platform="facebook", caption="c",
              hashtags=[], creative_path="/lib/reel.mp4",
              creative_public_url="https://cdn.example.com/reel.mp4",
              scheduled_for="t")
    http = _FakeHTTP()
    meta_publisher.publish(d, _acct(platform=Platform.FACEBOOK_PAGE, key="t_fb"),
                           http=http)
    joined = " ".join(http.calls)
    assert "/videos" in joined       # reel goes to the video endpoint
    assert "/photos" not in joined   # never the photo endpoint (mp4 -> "Can't Read Files")
