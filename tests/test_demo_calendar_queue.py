"""
Demo calendar queue tests. Offline (no network, no R2). Mirrors test_welcome_queue.py.

Asserts: the runner hooks are inert while AGENT_DEMO_CALENDAR_ENABLED is OFF; on a demo
date they produce a PENDING feed draft with the correct verbatim caption + hosted URL,
cross-posted to BOTH lasso accounts as the SAME served item; a non-demo date is None; a
non-LASSO account is None; TWO POSTS PER DAY: every demo day is is_story so a paired story
fires on lasso_ig for every demo feed day, but only when this run's feed was a demo feed
draft AND a story URL exists, and never when the feed was another queue's post; a missing
manifest is a no-op that never raises; create_from_manifest is idempotent by day_key; and
every hook + body substring in the copy bank exists in an approved source (no fabrication).
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, demo_calendar_queue as dcq  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402

_DASH = re.compile(r"[‐-―−\-]")  # em/en/figure dashes + hyphen-minus

# two real demo dates. Every demo day is now is_story (2 posts/day), so both dates pair a
# story; post 1 (All in one offer) and post 5 (Proof) exercise different pillars.
_FEED_DAY = "2026-08-06"     # post 1, All in one offer, is_story True
_STORY_DAY = "2026-08-10"    # post 5, Proof, is_story True
_NON_DEMO_DAY = "2026-07-01"


def _ig():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM, token_env="X", target_id_env="Y")


def _fb():
    return Account(key="lasso_fb", display_name="LASSO FB",
                   platform=Platform.FACEBOOK_PAGE, token_env="X", target_id_env="Y")


def _seed_manifest(monkeypatch, tmp_path):
    """Write a manifest covering every feed + story file, point the module at it, and
    seed the DB queue from it. Returns the manifest dict."""
    manifest = {}
    for post in dcq.DEMO_POSTS:
        manifest[post["filename"]] = f"https://cdn.test/{post['filename']}"
        if post["is_story"]:
            sf = dcq._story_filename(post["filename"])
            manifest[sf] = f"https://cdn.test/{sf}"
    mpath = tmp_path / "demo_calendar_manifest.json"
    mpath.write_text(json.dumps(manifest))
    monkeypatch.setattr(dcq, "MANIFEST_PATH", str(mpath))
    dcq.create_from_manifest()
    return manifest


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_DEMO_CALENDAR_ENABLED", "true")


# ---- caption assembly (verbatim, dash free) --------------------------------------------

def test_caption_shape_is_hook_body_cta_hashtags():
    post = dcq.DEMO_POSTS[0]
    cap = dcq.build_caption(post)
    assert cap.startswith(post["hook"])
    assert post["body"] in cap
    assert post["cta"] in cap
    assert "#LASSOFramework" in cap
    # structure: hook \n\n body \n\n cta \n hashtags
    assert f"{post['hook']}\n\n{post['body']}\n\n{post['cta']}\n" in cap


def test_all_captions_are_dash_free():
    for post in dcq.DEMO_POSTS:
        cap = dcq.build_caption(post)
        assert not _DASH.search(cap), f"post {post['num']} caption carries a dash"


def test_thirty_posts_dates_unique_and_ordered():
    dates = [p["date"] for p in dcq.DEMO_POSTS]
    assert len(dcq.DEMO_POSTS) == 30
    assert len(set(dates)) == 30
    assert dates == sorted(dates)


def test_every_demo_day_carries_a_story():
    """Two posts per day: every one of the 30 days is is_story so it pairs a story."""
    assert all(p["is_story"] for p in dcq.DEMO_POSTS)
    assert sum(1 for p in dcq.DEMO_POSTS if p["is_story"]) == 30


# ---- runner hooks: flag gating ---------------------------------------------------------

def test_feed_hook_inert_while_flag_off(monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)
    assert dcq.build_demo_calendar_draft(_ig(), _FEED_DAY) is None


def test_story_hook_inert_while_flag_off(monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)

    class _D:
        draft_id = "demof_deadbeef"
    assert dcq.build_demo_calendar_story_draft(_ig(), _STORY_DAY, feed_draft=_D()) is None


# ---- feed hook: date match, cross-post, verbatim caption + hosted URL -------------------

def test_feed_hook_serves_both_lasso_accounts_same_item(armed, monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)
    ig = dcq.build_demo_calendar_draft(_ig(), _FEED_DAY)
    fb = dcq.build_demo_calendar_draft(_fb(), _FEED_DAY)
    assert ig is not None and fb is not None
    assert ig.status.value == "pending" and fb.status.value == "pending"
    assert ig.draft_type == "feed" and not ig.is_story
    # same served item across the fan-out, distinct id per account
    assert ig.creative_public_url == fb.creative_public_url
    assert ig.draft_id != fb.draft_id
    # verbatim caption for post 1
    post1 = next(p for p in dcq.DEMO_POSTS if p["date"] == _FEED_DAY)
    assert ig.caption == dcq.build_caption(post1)
    assert ig.creative_public_url == f"https://cdn.test/{post1['filename']}"


def test_feed_hook_non_demo_date_is_none(armed, monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)
    assert dcq.build_demo_calendar_draft(_ig(), _NON_DEMO_DAY) is None


def test_feed_hook_ignores_non_lasso_accounts(armed, monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)
    client = Account(key="acme_ig", display_name="Acme", platform=Platform.INSTAGRAM,
                     token_env="X", target_id_env="Y")
    assert dcq.build_demo_calendar_draft(client, _FEED_DAY) is None


# ---- story hook: coupled to a demo feed draft AND a story URL ---------------------------

def test_story_fires_only_with_demo_feed_and_story_url(armed, monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)
    feed = dcq.build_demo_calendar_draft(_ig(), _STORY_DAY)
    assert feed is not None and feed.draft_id.startswith("demof_")
    story = dcq.build_demo_calendar_story_draft(_ig(), _STORY_DAY, feed_draft=feed)
    assert story is not None
    assert story.is_story and story.draft_type == "story"
    assert story.creative_public_url.endswith("_story.png")


def test_story_refuses_when_feed_was_not_a_demo(armed, monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)

    class _D:  # a book post, not a demo feed
        draft_id = "book_deadbeef"
    assert dcq.build_demo_calendar_story_draft(_ig(), _STORY_DAY, feed_draft=_D()) is None


def test_every_demo_feed_day_pairs_a_story(armed, monkeypatch, tmp_path):
    """Two posts per day: EVERY demo feed day (not just Proof days) now pairs a story on
    lasso_ig. Exercise a non-Proof day (post 1) to prove the story is no longer gated to
    a subset of days."""
    _seed_manifest(monkeypatch, tmp_path)
    feed = dcq.build_demo_calendar_draft(_ig(), _FEED_DAY)  # post 1, All in one offer
    assert feed is not None and feed.draft_id.startswith("demof_")
    story = dcq.build_demo_calendar_story_draft(_ig(), _FEED_DAY, feed_draft=feed)
    assert story is not None
    assert story.is_story and story.draft_type == "story"
    assert story.creative_public_url.endswith("_story.png")


def test_all_thirty_days_fire_feed_and_story(armed, monkeypatch, tmp_path):
    """End to end: every one of the 30 dated days serves BOTH a feed draft on both LASSO
    accounts AND a paired story draft on lasso_ig = two posts per day."""
    _seed_manifest(monkeypatch, tmp_path)
    for post in dcq.DEMO_POSTS:
        day = post["date"]
        ig_feed = dcq.build_demo_calendar_draft(_ig(), day)
        fb_feed = dcq.build_demo_calendar_draft(_fb(), day)
        assert ig_feed is not None and fb_feed is not None, f"no feed on {day}"
        story = dcq.build_demo_calendar_story_draft(_ig(), day, feed_draft=ig_feed)
        assert story is not None and story.is_story, f"no story on {day}"


def test_story_ignores_non_story_account(armed, monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)
    feed = dcq.build_demo_calendar_draft(_fb(), _STORY_DAY)
    assert dcq.build_demo_calendar_story_draft(_fb(), _STORY_DAY, feed_draft=feed) is None


# ---- missing manifest: no-op, never raises ---------------------------------------------

def test_missing_manifest_is_a_noop(armed, monkeypatch, tmp_path):
    # point at a path that does not exist; nothing seeded, hooks must not raise
    monkeypatch.setattr(dcq, "MANIFEST_PATH", str(tmp_path / "does_not_exist.json"))
    assert dcq.build_demo_calendar_draft(_ig(), _FEED_DAY) is None
    assert dcq.create_from_manifest() == 0


# ---- seed idempotency ------------------------------------------------------------------

def test_create_from_manifest_is_idempotent(monkeypatch, tmp_path):
    _seed_manifest(monkeypatch, tmp_path)
    first = {r["day_key"] for r in dcq.queue_status()}
    assert len(first) == 30
    # re-seeding the same manifest adds nothing new (INSERT OR IGNORE by day_key)
    assert dcq.create_from_manifest() == 0
    assert len({r["day_key"] for r in dcq.queue_status()}) == 30


def test_seed_skips_posts_missing_from_manifest(monkeypatch, tmp_path):
    # a partial manifest (only the first post's feed file) seeds exactly one row
    post1 = dcq.DEMO_POSTS[0]
    mpath = tmp_path / "demo_calendar_manifest.json"
    mpath.write_text(json.dumps({post1["filename"]: "https://cdn.test/one.png"}))
    monkeypatch.setattr(dcq, "MANIFEST_PATH", str(mpath))
    assert dcq.create_from_manifest() == 1
    rows = dcq.queue_status()
    assert len(rows) == 1 and rows[0]["day_key"] == post1["date"]


# ---- no fabrication: every hook + body traces to an approved source ---------------------

def _approved_corpus():
    """The approved sources Echo may draft LASSO's brand from (the lasso_now copy bank +
    the two approved knowledge files), whitespace-normalized. Normalizing collapses the
    source's `Body:` line breaks so a body that groups two approved Body lines into one
    caption paragraph (per the caption-spacing rule) still matches verbatim, while any
    text NOT present in a source still fails."""
    root = os.path.dirname(os.path.dirname(__file__))
    parts = []
    for rel in ("brand_voice/lasso_now.md",
                "brand_voice/knowledge/08_platform_2026.md",
                "brand_voice/knowledge/02_verified_stats.md"):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            parts.append(f.read())
    raw = "\n".join(parts)
    # drop the source's line-label prefixes so grouped Body lines read as one run,
    # then collapse all whitespace to single spaces.
    raw = raw.replace("Body:", " ").replace("Hook:", " ").replace("USE:", " ")
    return re.sub(r"\s+", " ", raw)


def _norm(text):
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text):
    """Split into sentences on terminal punctuation. Each demo sentence must trace to an
    approved source line verbatim; a body may GROUP two approved lines into one caption
    paragraph (caption-spacing rule), so checking sentence by sentence proves every
    sentence is approved without requiring two non-adjacent source lines to be contiguous."""
    return [s for s in re.split(r"(?<=[.!?])\s+", _norm(text)) if s]


def test_every_hook_and_body_is_in_an_approved_source():
    corpus = _approved_corpus()
    for post in dcq.DEMO_POSTS:
        for sent in _sentences(post["hook"]) + _sentences(post["body"]):
            assert sent in corpus, \
                (f"post {post['num']} sentence not found verbatim in an approved "
                 f"source: {sent!r}")


def test_every_cta_is_an_approved_lasso_now_cta():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "brand_voice/lasso_now.md"), encoding="utf-8") as f:
        lasso_now = f.read()
    for post in dcq.DEMO_POSTS:
        assert post["cta"] in lasso_now, \
            f"post {post['num']} CTA is not an approved lasso_now CTA: {post['cta']!r}"


# ---- card-only gate: demo posts ALWAYS card, never auto-publish -------------------------

def test_demo_feed_and_story_drafts_force_approval(armed, monkeypatch, tmp_path):
    """Every demo draft carries force_approval=True so it cards for approve/deny/edit
    even when AGENT_AUTO_APPROVE_ENABLED is armed."""
    _seed_manifest(monkeypatch, tmp_path)
    feed = dcq.build_demo_calendar_draft(_ig(), _STORY_DAY)
    assert feed is not None and feed.force_approval is True
    story = dcq.build_demo_calendar_story_draft(_ig(), _STORY_DAY, feed_draft=feed)
    assert story is not None and story.force_approval is True


def test_force_approval_draft_cards_even_when_auto_approve_armed(monkeypatch):
    """_post_and_save must NOT auto-publish a force_approval draft: it cards and stays
    PENDING even with auto-approve (and trust autopublish) on. Guards the gate."""
    from agent import runner, config
    from agent.drafter import Draft, DraftStatus

    monkeypatch.setattr(config, "auto_approve_enabled", lambda: True)
    monkeypatch.setattr(config, "trust_dryrun_enabled", lambda: False)
    monkeypatch.setattr(config, "trust_autopublish_enabled", lambda: False)

    class _Poster:
        def __init__(self): self.cards = []; self.notices = []
        def post_approval_card(self, d): self.cards.append(d); return {"ok": True, "channel": "c", "ts": "t"}
        def post_notice(self, m): self.notices.append(m)

    class _Store:
        def __init__(self): self.saved = []
        def put(self, d): self.saved.append(d)

    d = Draft(draft_id="demof_x", account_key="lasso_ig", platform="instagram",
              caption="hook\n\nbody\n\ncta", hashtags=[], creative_path="x.png",
              creative_public_url="https://cdn.test/x.png",
              scheduled_for="2026-08-10T18:30:00Z", status=DraftStatus.PENDING,
              day_key="2026-08-10", draft_type="feed", force_approval=True)
    poster, store = _Poster(), _Store()
    runner._post_and_save(d, store, poster, idempotent=False)

    assert d.status == DraftStatus.PENDING          # never auto-approved/published
    assert d in poster.cards                          # carded for a human
    assert not poster.notices                          # no "Auto-published" notice
