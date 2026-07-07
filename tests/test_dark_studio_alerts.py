"""
Dark-studio ops alert tests (Part 1 of silent-miss sweep).
Every call site that calls creative_studio.generate must fire one ops alert
when the studio returns None. Fully offline: generate is patched to return None;
ops_alerts._default_poster is patched to a recording poster.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import (  # noqa: E402
    book_campaign, config, creative_studio, doc_intake, ops_alerts,
    podcast_cards, regen_library, stories, summit,
)
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def post_thread_reply(self, channel, ts, text):
        return {"ok": True}


def _arm_alerts(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    return rec


def _acct(key="lasso_ig"):
    return Account(key=key, display_name=key, platform=Platform.INSTAGRAM,
                   token_env="DARK_TEST_TOKEN", target_id_env="DARK_TEST_TARGET")


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


# ---- stories -----------------------------------------------------------------------

def test_story_dark_studio_fires_ops_alert(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **kw: None)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    poster = _arm_alerts(monkeypatch)

    # creative_path must start with "nano_" so _is_studio_creative returns True
    feed_img = tmp_path / "nano_hook_line.png"
    feed_img.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    feed = Draft(
        draft_id="feed1", account_key="lasso_ig", platform="instagram",
        caption="c", hashtags=[], creative_path=str(feed_img),
        creative_public_url="https://cdn.test/feed.png",
        scheduled_for="2026-07-01T18:30:00+00:00", status=DraftStatus.PENDING,
        source_fragments=["Hook line.", "Body line one.", "Body line two."],
    )

    result = stories.build_story_draft(
        _acct(), "2026-07-01", feed_draft=feed, s3_client=FakeS3(),
    )
    # Story still returns (using feed image fallback), but alert fires
    assert result is not None
    alerts = [n for n in poster.notices if "ECHO ALERT:" in n]
    assert len(alerts) == 1
    assert "story" in alerts[0].lower()
    assert "studio" in alerts[0].lower() or "Gemini" in alerts[0]


# ---- doc_intake --------------------------------------------------------------------

def test_doc_intake_dark_studio_fires_ops_alert(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DOC_INTAKE_ENABLED", "true")
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **kw: None)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    poster = _arm_alerts(monkeypatch)

    text = "Hook line one.\nBody line two.\nBody line three."
    drafts = doc_intake.process_document(
        text=text, account=_acct(), s3_client=FakeS3(),
    )
    assert drafts  # still produces a text-only draft
    alerts = [n for n in poster.notices if "ECHO ALERT:" in n]
    assert len(alerts) >= 1
    assert "doc intake" in alerts[0].lower()
    assert "studio" in alerts[0].lower() or "Gemini" in alerts[0]


# ---- summit ------------------------------------------------------------------------

SUMMIT_KNOWLEDGE = """\
# Summit campaign (test fixture)

## VERIFIED FACTS (postable)
- USE: LASSO books 71.9 percent of leads. (platform_2026_receipts)

## APPROVED ANGLES (rotate; one fact based angle per post)
- USE: The gym that answers first wins the member. (platform_2026_receipts)
- USE: One platform handles ads sales and social. (platform_2026_receipts)
"""


def test_summit_dark_studio_fires_ops_alert(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SUMMIT_CAMPAIGN_ENABLED", "true")
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **kw: None)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    poster = _arm_alerts(monkeypatch)

    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "04_summit_campaign.md").write_text(SUMMIT_KNOWLEDGE, encoding="utf-8")

    result = summit.build_summit_draft(
        _acct(), "2026-07-07", knowledge_dir=str(kd), s3_client=FakeS3(),
    )
    assert result is None  # dark studio: returns None so normal path runs
    alerts = [n for n in poster.notices if "ECHO ALERT:" in n]
    assert len(alerts) == 1
    assert "summit" in alerts[0].lower()
    assert "studio" in alerts[0].lower() or "Gemini" in alerts[0]


# ---- book_campaign -----------------------------------------------------------------

def test_book_dark_studio_fires_ops_alert(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BOOK_CAMPAIGN_ENABLED", "true")
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **kw: None)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    poster = _arm_alerts(monkeypatch)

    real_kd = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge")
    monkeypatch.setattr(config, "BOOK_DIR", real_kd)
    # Patch _existing_card to return None so _finish_draft calls generate
    monkeypatch.setattr(book_campaign, "_existing_card", lambda n: None)

    result = book_campaign.build_book_draft(
        _acct(), "2026-07-06", s3_client=FakeS3(),
    )
    assert result is None
    alerts = [n for n in poster.notices if "ECHO ALERT:" in n]
    assert len(alerts) == 1
    assert "book" in alerts[0].lower()
    assert "studio" in alerts[0].lower() or "Gemini" in alerts[0]


# ---- podcast_cards -----------------------------------------------------------------

def test_podcast_card_dark_studio_fires_ops_alert(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **kw: None)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    poster = _arm_alerts(monkeypatch)

    from agent import podcast_cards, podcast_transcripts
    ep_n = 99
    hook = "The gym that answers first wins the member."
    support = "Speed matters in lead follow up."
    monkeypatch.setattr(podcast_transcripts, "contains_verbatim",
                        lambda n, text: True)
    monkeypatch.setattr(podcast_cards, "queue_item_for",
                        lambda day_key: {"id": 1, "episode": ep_n,
                                         "hook": hook, "support": support})

    result = podcast_cards.build_card_draft(
        _acct(), "2026-07-07", s3_client=FakeS3(),
    )
    assert result is None
    alerts = [n for n in poster.notices if "ECHO ALERT:" in n]
    assert len(alerts) == 1
    assert "podcast card" in alerts[0].lower()
    assert "studio" in alerts[0].lower() or "Gemini" in alerts[0]


# ---- regen_library -----------------------------------------------------------------

def test_regen_library_dark_studio_fires_ops_alert(monkeypatch, tmp_path):
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **kw: None)
    poster = _arm_alerts(monkeypatch)

    first_key = list(regen_library.CONCEPTS)[0]
    result = regen_library.run(only=first_key, dry_run=False,
                               s3_client=FakeS3(), out_dir=str(tmp_path))
    alerts = [n for n in poster.notices if "ECHO ALERT:" in n]
    assert len(alerts) == 1
    assert "regen" in alerts[0].lower()
    assert "studio" in alerts[0].lower() or "Gemini" in alerts[0]
