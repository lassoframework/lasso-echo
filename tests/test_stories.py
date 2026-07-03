"""
Stories tests. Fully OFFLINE: fake nano + S3 clients, fake/exploding HTTP, no real
SDKs and no network. Asserts: flag OFF is fully dormant (no Story drafts at all);
flag ON yields one PENDING Story draft per account, clearly labeled STORY, with a
9:16 canvas requested from creative_studio; every Story is held for approval; and
the publish path makes NO network call unless BOTH the publish flag AND the stories
flag are armed (and then hits the STORIES endpoint shape, never a caption).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, meta_publisher, stories  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.runner import run_daily  # noqa: E402
from agent.slack_surface import build_card_blocks, _fallback_text  # noqa: E402

DAY = "2027-07-07"  # a Wednesday: a posting day under the default cadence


class CaptureNano:
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


class ExplodingHTTP:
    def post(self, *a, **k):
        raise AssertionError("network was called; the gate failed")

    get = post


class RecordingHTTP:
    def __init__(self):
        self.calls = []

    def post(self, url, data=None, timeout=None, **k):
        self.calls.append((url, dict(data or {})))

        class R:
            status_code = 200

            def json(self):
                return {"id": "OK_ID", "post_id": "OK_ID"}

        return R()


def _acct(platform=Platform.INSTAGRAM, key="lasso_ig"):
    return Account(key=key, display_name=key, platform=platform,
                   token_env="STORY_TEST_TOKEN", target_id_env="STORY_TEST_TARGET")


def _feed_draft(**kw):
    base = dict(
        draft_id="feed123", account_key="lasso_ig", platform="instagram",
        caption="Leads go cold in minutes.", hashtags=["#LASSOFramework"],
        creative_path="nano_leads_go_cold.png",
        creative_public_url="https://cdn.echo.test/echo/lasso_ig/feed.png",
        scheduled_for="2027-07-07T18:30:00-04:00", status=DraftStatus.PENDING,
        source_fragments=["Leads go cold in minutes.",
                          "Answer inside five minutes and you book three times more."],
    )
    base.update(kw)
    return Draft(**base)


# ---- 1. flag OFF -> fully dormant, no Story drafts at all --------------------
def test_flag_off_generates_no_story_drafts(monkeypatch):
    monkeypatch.delenv("AGENT_STORIES_ENABLED", raising=False)
    assert stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft()) is None


# ---- 2. flag ON -> one PENDING Story per account, labeled, held --------------
def test_flag_on_pending_story_per_account(monkeypatch):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.delenv("AGENT_NANO_ENABLED", raising=False)     # generation off:
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)  # reuse feed image
    for platform, key in ((Platform.INSTAGRAM, "lasso_ig"),
                          (Platform.FACEBOOK_PAGE, "lasso_fb")):
        fd = _feed_draft(account_key=key, platform=platform)
        story = stories.build_story_draft(_acct(platform, key), DAY, feed_draft=fd)
        assert story is not None
        assert story.is_story is True
        assert story.status == DraftStatus.PENDING       # held for approval
        assert story.caption == "" and story.hashtags == []
        # reuses the day's approved creative, never invents one
        assert story.creative_public_url == fd.creative_public_url
        # every fragment traces to the feed draft's approved text
        assert story.source_fragments == fd.source_fragments


# ---- 3. 9:16 requested per-use; the feed target stays 4:5 --------------------
def test_story_requests_9_16_and_feed_stays_4_5(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")

    cap = CaptureNano()
    story = stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft(),
                                      nano_client=cap, s3_client=FakeS3())
    assert story.status == DraftStatus.PENDING
    assert len(cap.prompts) == 1
    assert "9:16" in cap.prompts[0]
    assert "1080x1920" in cap.prompts[0]
    assert "Story" in cap.prompts[0]
    # true vertical composition, not a reused feed card: story layout markers
    assert "UPPER THIRD" in cap.prompts[0]
    assert "safe zones" in cap.prompts[0].lower()
    assert "FILLS the tall portrait canvas" not in cap.prompts[0]
    # per-use aspect: the module-level feed target was not switched
    assert config.IMAGE_ASPECT == "4:5"
    # the 9:16 render got its own file; the feed image was not overwritten
    assert os.path.basename(story.creative_path).startswith("nano_story_")
    assert story.creative_public_url.startswith("https://cdn.echo.test/echo/lasso_ig/")


# ---- 3b. Story layout is its own vertical composition; feed layout untouched --
def test_story_layout_true_vertical_feed_unchanged():
    story = creative_studio.build_prompt(
        "Leads go cold in minutes.", ["Answer inside five minutes."],
        aspect="9:16", pixels="1080x1920", surface="Story")
    # headline upper third, one centered focal graphic, 250px top/bottom safe zones
    assert "UPPER THIRD" in story
    assert "ONE single focal graphic in the MIDDLE" in story
    assert "TOP 250 pixels" in story and "BOTTOM 250 pixels" in story
    assert "never a cropped, stretched, or reused feed card" in story
    # the feed flow guidance must NOT leak into the Story composition
    assert "FILLS the tall portrait canvas" not in story
    # house style + V3 palette unchanged on the Story
    assert "House style" in story
    for hexcode in ("#121E3C", "#FF0000", "#5EB9E6", "#FAF6F0"):
        assert hexcode in story, hexcode

    # the default (feed) prompt is untouched: original layout, no story markers
    feed = creative_studio.build_prompt(
        "Leads go cold in minutes.", ["Answer inside five minutes."])
    assert "FILLS the tall portrait canvas" in feed
    assert "UPPER THIRD" not in feed
    assert "safe zones" not in feed.lower()
    # the shipped feed composition constant is intact inside the feed prompt
    assert creative_studio.COMPOSITION_STYLE in feed


# ---- 4. the card is clearly labeled STORY ------------------------------------
def test_story_card_labeled(monkeypatch):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    story = stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft())
    blocks = build_card_blocks(story)
    header = blocks[0]["text"]["text"]
    assert "STORY" in header
    assert "STORY" in _fallback_text(story)
    # a feed draft's card carries no STORY label
    assert "STORY" not in build_card_blocks(_feed_draft())[0]["text"]["text"]


# ---- 5. schedule respected: a skip day drafts no Story ------------------------
def test_skip_day_drafts_no_story(monkeypatch):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    assert stories.build_story_draft(_acct(), "2026-07-04",  # a Saturday
                                     feed_draft=_feed_draft()) is None


# ---- 6. runner: flag ON adds one Story card per account, flag OFF none -------
class _Poster:
    def __init__(self):
        self.cards = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True}

    def post_notice(self, text):
        return {"ok": True}


class _Store:
    def __init__(self):
        self.saved = []

    def put(self, draft):
        self.saved.append(draft)


def _run_daily(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)  # draft only
    voice = tmp_path / "voice.md"
    voice.write_text("We help gym owners grow.\n\n## CTAs\n- Save this post.\n\n"
                     "## Hashtags\n#LASSOFramework", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    poster, store = _Poster(), _Store()
    out = run_daily(poster=poster, voice_path=str(voice), library_path=str(lib),
                    scheduled_for=f"{DAY}T18:30:00+00:00",
                    accounts=[_acct()], store=store)
    return out, poster, store


def test_runner_flag_off_no_story_cards(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_STORIES_ENABLED", raising=False)
    out, poster, _ = _run_daily(tmp_path, monkeypatch)
    assert out["status"] == "drafted"
    assert not any(getattr(d, "is_story", False) for d in poster.cards)


def test_runner_flag_on_one_story_card_per_account(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    out, poster, store = _run_daily(tmp_path, monkeypatch)
    story_cards = [d for d in poster.cards if getattr(d, "is_story", False)]
    assert len(story_cards) == 1                     # one account -> one Story
    assert story_cards[0].status == DraftStatus.PENDING
    assert any(getattr(d, "is_story", False) for d in store.saved)  # held for approval


# ---- 7. publish path: dormant unless BOTH gates are armed ---------------------
def test_story_publish_no_network_with_publish_off(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    story = _feed_draft(is_story=True)
    res = meta_publisher.publish(story, _acct(), http=ExplodingHTTP())
    assert res.mode == "would_publish"


def test_story_publish_no_network_with_stories_off(monkeypatch):
    # Even with publishing armed, a Story makes NO network call until the stories
    # flag is ALSO armed (both gates required).
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.delenv("AGENT_STORIES_ENABLED", raising=False)
    story = _feed_draft(is_story=True)
    res = meta_publisher.publish(story, _acct(), http=ExplodingHTTP())
    assert res.mode == "would_publish"
    assert "stories" in res.detail.lower()


def test_story_publish_endpoint_shape_when_both_armed(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setenv("STORY_TEST_TOKEN", "fake-token")
    monkeypatch.setenv("STORY_TEST_TARGET", "1789")
    http = RecordingHTTP()
    res = meta_publisher.publish(_feed_draft(is_story=True), _acct(), http=http)
    assert res.ok and res.mode == "published"
    url, data = http.calls[0]
    assert url.endswith("/1789/media")
    assert data.get("media_type") == "STORIES"
    assert "caption" not in data                     # a Story carries no caption
    assert http.calls[1][0].endswith("/1789/media_publish")
