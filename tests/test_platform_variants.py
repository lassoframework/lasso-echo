"""
Per-platform caption variant tests (flag AGENT_PLATFORM_VARIANTS_ENABLED, default
OFF). Selection and arrangement of APPROVED content only, no new text. Asserts:
flag OFF -> IG and FB get identical captions and hashtags, exactly as today; flag
ON -> IG keeps up to 5 approved tags, FB keeps at most 2, and every tag on either
platform is one of the approved tags.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, daily_studio, drafter  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus, draft_post, variant_hashtags  # noqa: E402
from agent.library import Creative  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402

APPROVED_TAGS = ["#LASSOFramework", "#GymMarketingMadeSimple", "#GymOwner",
                 "#FitnessMarketing", "#GymGrowth"]


def _voice():
    return VoiceDoc(raw="We help gym owners grow.", hashtags=list(APPROVED_TAGS),
                    ctas=["Save this post."])


def _creative():
    return Creative(path="lib/asset.png", media_type="image",
                    client_note="Our members love the 6am class.")


def _acct(platform, key):
    return Account(key=key, display_name=key, platform=platform,
                   token_env="X", target_id_env="Y")


def _draft(platform, key):
    return draft_post(_acct(platform, key), _creative(),
                      "2026-07-01T18:30:00-04:00", voice=_voice())


# ---- 1. flag OFF -> identical captions and hashtags, exactly as today ---------
def test_flag_off_identical_across_platforms(monkeypatch):
    monkeypatch.delenv("AGENT_PLATFORM_VARIANTS_ENABLED", raising=False)
    ig = _draft(Platform.INSTAGRAM, "lasso_ig")
    fb = _draft(Platform.FACEBOOK_PAGE, "lasso_fb")
    assert ig.status == fb.status == DraftStatus.PENDING
    assert ig.caption == fb.caption
    assert ig.hashtags == fb.hashtags          # unchanged behavior
    assert len(ig.hashtags) == 5


# ---- 2. flag ON -> IG keeps up to 5, FB keeps at most 2 -----------------------
def test_flag_on_ig_5_fb_2(monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_VARIANTS_ENABLED", "true")
    ig = _draft(Platform.INSTAGRAM, "lasso_ig")
    fb = _draft(Platform.FACEBOOK_PAGE, "lasso_fb")
    assert len(ig.hashtags) == 5
    assert len(fb.hashtags) <= 2
    # selection only: every tag on either platform is an approved tag
    for tag in ig.hashtags + fb.hashtags:
        assert tag in APPROVED_TAGS, f"unapproved tag: {tag!r}"
    # FB's tags are a selection from IG's set (arrangement, not new content)
    assert all(t in ig.hashtags for t in fb.hashtags)
    # the caption text itself is identical: only the tags differ
    assert ig.caption == fb.caption


# ---- 3. the helper is pure selection -------------------------------------------
def test_variant_hashtags_selection_only(monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_VARIANTS_ENABLED", "true")
    fb = variant_hashtags(Platform.FACEBOOK_PAGE, APPROVED_TAGS)
    ig = variant_hashtags(Platform.INSTAGRAM, APPROVED_TAGS)
    assert fb == APPROVED_TAGS[:2]
    assert ig == APPROVED_TAGS[:5]
    monkeypatch.delenv("AGENT_PLATFORM_VARIANTS_ENABLED", raising=False)
    assert variant_hashtags(Platform.FACEBOOK_PAGE, APPROVED_TAGS) == APPROVED_TAGS


# ---- 4. the daily studio path honors the variant too ---------------------------
FIXTURE = """# LASSO Now (VARIANTS TEST FIXTURE)
## Pillars
- Speed To Lead
## Pillar copy bank
### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer inside five minutes and you book three times more.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework #GymMarketingMadeSimple #GymOwner #FitnessMarketing #GymGrowth
"""


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


def test_daily_studio_fb_gets_at_most_2_tags(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PLATFORM_VARIANTS_ENABLED", "true")
    monkeypatch.setenv("AGENT_CONTENT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    src = tmp_path / "lasso_now.md"
    src.write_text(FIXTURE, encoding="utf-8")

    fb = daily_studio.build_daily_infographic_draft(
        _acct(Platform.FACEBOOK_PAGE, "lasso_fb"), "2026-07-01",
        nano_client=FakeNano(), s3_client=FakeS3(), source_path=str(src))
    assert fb.status == DraftStatus.PENDING
    assert len(fb.hashtags) <= 2

    ig = daily_studio.build_daily_infographic_draft(
        _acct(Platform.INSTAGRAM, "lasso_ig"), "2026-07-01",
        nano_client=FakeNano(), s3_client=FakeS3(), source_path=str(src))
    assert len(ig.hashtags) == 5
