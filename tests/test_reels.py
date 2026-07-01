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
