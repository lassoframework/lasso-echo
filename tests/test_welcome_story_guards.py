"""
Regression tests for the two recurring welcome-post defects, locked so they can
never come back:

DEFECT 1 (story posts at the wrong size): three layers of guard prove a welcome
STORY is ALWAYS a genuine 9:16 (1080x1920) and a square feed image (1080x1080) can
never leave render, be hosted, or be published as a story.
  a. RENDER assert  -> make_welcome(story) is 1080x1920; generate_posts raises on a
                       non-9:16 story; a square is never accepted as a story.
  b. HOST guard     -> the queue never hosts a square/None/off-size as a story_url.
  c. PUBLISH backstop-> build_welcome_story_draft returns None when the story asset is
                       missing/None, and blocks + ops-alerts a non-9:16 hosted asset.

DEFECT 2 (identical captions across clients): welcome_caption varies per gym
(deterministic, stable, dash-free, no "vendor", name/owner interpolate).

Offline: procedural backgrounds, fake host_fn, no network, no R2.
"""

import os
import re
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import welcome_templates as wt          # noqa: E402
from agent import welcome_posts                     # noqa: E402
from agent import welcome_queue                      # noqa: E402
from agent.accounts import Account, Platform         # noqa: E402

_DASH = re.compile(r"[‐-―−\-]")  # em/en/figure dashes + hyphen-minus


@pytest.fixture
def cache(tmp_path):
    return str(tmp_path / "cache")


def _ig():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM, token_env="X", target_id_env="Y")


class _FeedDraft:
    """Minimal stand-in for the run's feed draft: a welcome feed draft id starts
    with 'welcf_' so the story couples to it."""
    def __init__(self, draft_id="welcf_abc123"):
        self.draft_id = draft_id


# ==========================================================================
# DEFECT 1a — RENDER assert
# ==========================================================================

@pytest.mark.parametrize("tid", ["T1", "T2", "T7", "T8", "T9", "T10"])
def test_make_welcome_story_is_exactly_1080x1920(tmp_path, cache, tid):
    out = str(tmp_path / f"{tid}_story.png")
    path = wt.make_welcome(tid, "Iron Forge Fitness", "Jordan Blake", None,
                           format="story", out_path=out, cache_dir=cache)
    with Image.open(path) as im:
        assert im.size == (1080, 1920) == wt.STORY_SIZE


def test_is_story_size_rejects_square_and_none():
    assert wt.is_story_size((1080, 1920)) is True
    assert wt.is_story_size((1080, 1080)) is False   # the square feed image
    assert wt.is_story_size((1920, 1080)) is False   # landscape
    assert wt.is_story_size(None) is False


def test_make_welcome_story_raises_when_render_is_square(tmp_path, cache, monkeypatch):
    """If anything upstream produced a square as a story, make_welcome must refuse
    (never hand back a 1080x1080 as a story). Simulate the exact recurring defect by
    forcing the compositor's canvas to the square feed size for a story render."""
    real_render = wt._render

    def square_render(template, gym_name, owner_name, logo_path, out_path,
                      bg_client=None, cache_dir=None, prefer=None, fmt="feed"):
        # write a square where a story is expected, mimicking the crop-from-square bug
        Image.new("RGB", (wt.SIZE, wt.SIZE), (12, 20, 42)).save(out_path)
        return out_path, "placeholder", "text"

    monkeypatch.setattr(wt, "_render", square_render)
    with pytest.raises(ValueError):
        wt.make_welcome("T1", "All Kine", "Nate", None, format="story",
                        out_path=str(tmp_path / "sq_story.png"), cache_dir=cache)
    assert wt._render is square_render  # sanity: the patch was in effect
    monkeypatch.setattr(wt, "_render", real_render)


def test_generate_posts_raises_on_non_9_16_story(tmp_path, cache, monkeypatch):
    """generate_posts MUST assert the story file it wrote is 1080x1920 and raise if
    not. Force make_welcome to emit a square for the story branch."""
    def fake_make_welcome(template_id, gym_name, owner_name, logo_path,
                          format="feed", out_path=None, bg_client=None, cache_dir=None):
        size = (wt.SIZE, wt.SIZE) if format == "story" else (wt.SIZE, wt.SIZE)
        Image.new("RGB", size, (12, 20, 42)).save(out_path)
        return out_path

    monkeypatch.setattr(wt, "make_welcome", fake_make_welcome)
    with pytest.raises(ValueError):
        welcome_posts.generate_posts("T1", "GritX", "Sam", None,
                                     str(tmp_path / "out"), cache_dir=cache)


def test_generate_posts_story_is_9_16_on_the_real_path(tmp_path, cache):
    posts = welcome_posts.generate_posts("T1", "Iron Forge", "Sam", None,
                                         str(tmp_path / "out"), cache_dir=cache)
    with Image.open(posts["story"]) as im:
        assert im.size == wt.STORY_SIZE
    with Image.open(posts["feed"]) as im:
        assert im.size == (wt.SIZE, wt.SIZE)


# ==========================================================================
# DEFECT 1b — HOST guard (never host a square/None as a story_url)
# ==========================================================================

def _square(tmp_path, name="sq.png"):
    p = str(tmp_path / name)
    Image.new("RGB", (wt.SIZE, wt.SIZE), (12, 20, 42)).save(p)
    return p


def _story(tmp_path, name="story.png"):
    p = str(tmp_path / name)
    Image.new("RGB", wt.STORY_SIZE, (12, 20, 42)).save(p)
    return p


def test_local_story_helper_accepts_only_9_16(tmp_path):
    assert welcome_queue._local_story_is_9_16(_story(tmp_path)) is True
    assert welcome_queue._local_story_is_9_16(_square(tmp_path)) is False
    assert welcome_queue._local_story_is_9_16(None) is False
    assert welcome_queue._local_story_is_9_16(str(tmp_path / "missing.png")) is False


def test_enqueue_never_hosts_a_square_as_story_url(tmp_path):
    hosted = []

    def host(path):
        hosted.append(path)
        return f"https://cdn.test/{os.path.basename(path)}"

    feed = _square(tmp_path, "feed.png")       # a feed image is a square, fine as feed
    sq_story = _square(tmp_path, "square_story.png")  # a square masquerading as a story
    entry = {"gym_key": "domain:sq.com", "name": "Square Gym", "owner": "Pat",
             "template": "T1", "tier_label": "Launch",
             "posts": {"feed": feed, "story": sq_story}}
    rid = welcome_queue.enqueue(entry, host_fn=host)
    assert rid is not None                      # feed still queues
    row = [r for r in welcome_queue.queue_status() if r["gym_key"] == "domain:sq.com"][0]
    # the square story was NOT hosted: story_url is empty
    assert sq_story not in hosted
    with welcome_queue._conn() as conn:
        db_row = conn.execute("SELECT story_url FROM welcome_queue WHERE gym_key=?",
                              ("domain:sq.com",)).fetchone()
    assert (db_row["story_url"] or "") == ""


def test_enqueue_hosts_a_genuine_9_16_story(tmp_path):
    def host(path):
        return f"https://cdn.test/{os.path.basename(path)}"

    entry = {"gym_key": "domain:ok.com", "name": "Ok Gym", "owner": "Lee",
             "template": "T1", "tier_label": "Launch",
             "posts": {"feed": _square(tmp_path, "f.png"),
                       "story": _story(tmp_path, "s.png")}}
    assert welcome_queue.enqueue(entry, host_fn=host) is not None
    with welcome_queue._conn() as conn:
        db_row = conn.execute("SELECT story_url FROM welcome_queue WHERE gym_key=?",
                              ("domain:ok.com",)).fetchone()
    assert db_row["story_url"].endswith("s.png")


# ==========================================================================
# DEFECT 1c — PUBLISH backstop (build_welcome_story_draft)
# ==========================================================================

@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_WELCOME_QUEUE_ENABLED", "true")


def _queue_row(gym_key, name, story_url, tmp_path):
    """Insert a queued welcome row directly with a chosen story_url."""
    caption = welcome_queue.welcome_caption(name)
    with welcome_queue._conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO welcome_queue "
            "(gym_key, name, owner, template, caption, feed_url, story_url, tier, status) "
            "VALUES (?,?,?,?,?,?,?,?, 'queued')",
            (gym_key, name, "", "T1", caption, "https://cdn.test/feed.png",
             story_url, "Launch"))
        conn.commit()


def test_story_draft_none_when_story_url_missing(armed, tmp_path):
    _queue_row("domain:nostory.com", "No Story Gym", "", tmp_path)  # empty story_url
    d = welcome_queue.build_welcome_story_draft(_ig(), "2026-08-06",
                                                feed_draft=_FeedDraft())
    assert d is None


def test_story_draft_built_when_story_url_present(armed, tmp_path):
    _queue_row("domain:has.com", "Has Story Gym",
               "https://cdn.test/has_story.png", tmp_path)
    d = welcome_queue.build_welcome_story_draft(_ig(), "2026-08-06",
                                                feed_draft=_FeedDraft())
    assert d is not None and d.is_story
    assert d.creative_public_url == "https://cdn.test/has_story.png"


def test_story_draft_blocks_non_9_16_hosted_asset_and_alerts(armed, tmp_path, monkeypatch):
    """When a cheap dimension probe reports a non-9:16 hosted asset, the story is
    blocked (None) and ONE ops alert fires, mirroring commit 2c21a10."""
    _queue_row("domain:bad.com", "Bad Dims Gym",
               "https://cdn.test/bad_story.png", tmp_path)
    fired = []
    monkeypatch.setattr(welcome_queue.ops_alerts, "alert",
                        lambda msg, *a, **k: fired.append(msg))
    d = welcome_queue.build_welcome_story_draft(
        _ig(), "2026-08-06", feed_draft=_FeedDraft(),
        verify_dims=lambda url: (1080, 1080))  # a square hosted asset
    assert d is None
    assert len(fired) == 1 and "not 9:16" in fired[0]


def test_story_draft_passes_9_16_hosted_asset_when_probe_supplied(armed, tmp_path):
    _queue_row("domain:good.com", "Good Dims Gym",
               "https://cdn.test/good_story.png", tmp_path)
    d = welcome_queue.build_welcome_story_draft(
        _ig(), "2026-08-06", feed_draft=_FeedDraft(),
        verify_dims=lambda url: (1080, 1920))
    assert d is not None and d.is_story


# ==========================================================================
# DEFECT 2 — caption variance
# ==========================================================================

_SAMPLE = ["All Kine Fitness", "GritX", "Iron Forge", "Bell House Fitness",
           "Summit Strength", "CrossFit 812", "Peak Athletics", "Momentum Gym",
           "Anchor Fitness", "Rise Studio"]


def test_captions_vary_across_gyms():
    caps = {welcome_queue.welcome_caption(n) for n in _SAMPLE}
    # the recurring bug was one fixed template for everyone; demand real variance
    assert len(caps) >= 5, "welcome captions must vary gym to gym, not be identical"


def test_all_kine_and_gritx_differ():
    # the two gyms Blake reported as word-for-word identical
    assert welcome_queue.welcome_caption("All Kine Fitness", "Nate") != \
        welcome_queue.welcome_caption("GritX", "Sam")


def test_caption_is_stable_for_a_given_gym():
    a = welcome_queue.welcome_caption("All Kine Fitness", "Nate")
    b = welcome_queue.welcome_caption("All Kine Fitness", "Nate")
    assert a == b
    # spacing-insensitive stability (same gym, different incidental spacing)
    assert welcome_queue._welcome_variant_index("All Kine  Fitness") == \
        welcome_queue._welcome_variant_index("All Kine Fitness")


@pytest.mark.parametrize("name", _SAMPLE)
def test_every_variant_is_dash_free_and_never_says_vendor(name):
    cap = welcome_queue.welcome_caption(name, "Alexandra Fitzgerald")
    assert not _DASH.search(cap), f"caption for {name} carries a dash"
    assert "vendor" not in cap.lower()


def test_name_and_owner_interpolate():
    cap = welcome_queue.welcome_caption("Bell House Fitness", "Justin Christmas")
    assert "Bell House Fitness" in cap
    assert "Justin Christmas" in cap
    # without an owner the gym-team phrasing still resolves cleanly
    cap2 = welcome_queue.welcome_caption("GritX")
    assert "the GritX team" in cap2


def test_all_variants_dash_free_directly():
    for i, v in enumerate(welcome_queue._WELCOME_VARIANTS):
        s = v.format(name="Sample Gym", who="Owner and the Sample Gym team")
        assert not _DASH.search(s), f"variant {i} carries a dash"
        assert "vendor" not in s.lower()
    assert len(welcome_queue._WELCOME_VARIANTS) >= 5
