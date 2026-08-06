"""
Welcome drip queue tests. Offline (fake host_fn, no network, no R2). Asserts:
the caption is dash-free and carries the gym name/owner; enqueue is idempotent by
gym_key and stamps the welcome ledger (a gym is welcomed once); the queue serves
the OLDEST item, one per day, shared across the fan-out and order-independent; the
runner hooks are inert while AGENT_WELCOME_QUEUE_ENABLED is OFF and produce a feed
draft (both LASSO accounts) + a coupled story (lasso_ig) when armed; a story never
pops a gym the feed did not serve; scan_and_enqueue respects the flag and hosting.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, welcome_queue, welcome_posts  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402

_DASH = re.compile(r"[‐-―−\-]")  # em/en/figure dashes + hyphen-minus


def _ig():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM, token_env="X", target_id_env="Y")


def _fb():
    return Account(key="lasso_fb", display_name="LASSO FB",
                   platform=Platform.FACEBOOK_PAGE, token_env="X", target_id_env="Y")


def _entry(gym_key, name, owner="", template="T1"):
    return {"gym_key": gym_key, "name": name, "owner": owner, "template": template,
            "tier_label": "Launch",
            "posts": {"feed": f"{name}_feed.png", "story": f"{name}_story.png"}}


def _fake_host(path):
    return f"https://cdn.test/{os.path.basename(path)}"


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_WELCOME_QUEUE_ENABLED", "true")


# ---- caption ---------------------------------------------------------------------------

def test_caption_is_dash_free_and_names_the_gym():
    cap = welcome_queue.welcome_caption("Bell House Fitness", "Justin Christmas")
    assert "Bell House Fitness" in cap
    assert "Justin Christmas" in cap
    assert not _DASH.search(cap), "welcome caption must carry no dash of any kind"


def test_caption_without_owner_still_clean():
    cap = welcome_queue.welcome_caption("GritX")
    assert "GritX" in cap
    assert "the GritX team" in cap
    assert not _DASH.search(cap)


# ---- enqueue: idempotent + ledger ------------------------------------------------------

def test_enqueue_adds_once_and_stamps_ledger():
    rid = welcome_queue.enqueue(_entry("domain:a.com", "Gym A"), host_fn=_fake_host)
    assert rid is not None
    # second enqueue of the same gym is a no-op
    assert welcome_queue.enqueue(_entry("domain:a.com", "Gym A"), host_fn=_fake_host) is None
    # the ledger is stamped so a re-scan lands it in already_welcomed
    assert welcome_posts.already_welcomed("domain:a.com")
    rows = welcome_queue.queue_status()
    assert len(rows) == 1 and rows[0]["name"] == "Gym A"


def test_enqueue_hosting_failure_leaves_it_unqueued():
    rid = welcome_queue.enqueue(_entry("domain:b.com", "Gym B"),
                                host_fn=lambda p: "")  # host returns nothing
    assert rid is None
    assert welcome_queue.queue_status() == []
    assert not welcome_posts.already_welcomed("domain:b.com")  # not stamped, will retry


# ---- serving: one per day, oldest first, shared -----------------------------------------

def test_next_for_day_serves_oldest_and_is_idempotent():
    welcome_queue.enqueue(_entry("domain:1.com", "First"), host_fn=_fake_host)
    welcome_queue.enqueue(_entry("domain:2.com", "Second"), host_fn=_fake_host)
    a = welcome_queue.next_for_day("2026-08-05")
    assert a["name"] == "First"
    # same day, later caller (second account / story) gets the SAME item
    b = welcome_queue.next_for_day("2026-08-05")
    assert b["gym_key"] == a["gym_key"]
    # next day serves the next-oldest
    c = welcome_queue.next_for_day("2026-08-06")
    assert c["name"] == "Second"


def test_next_for_day_empty_queue_is_none():
    assert welcome_queue.next_for_day("2026-08-05") is None


# ---- runner hooks ----------------------------------------------------------------------

def test_feed_hook_inert_while_flag_off():
    welcome_queue.enqueue(_entry("domain:c.com", "Gym C"), host_fn=_fake_host)
    assert welcome_queue.build_welcome_queue_draft(_ig(), "2026-08-05") is None


def test_feed_hook_serves_both_lasso_accounts_same_gym(armed):
    welcome_queue.enqueue(_entry("domain:d.com", "Gym D"), host_fn=_fake_host)
    ig = welcome_queue.build_welcome_queue_draft(_ig(), "2026-08-05")
    fb = welcome_queue.build_welcome_queue_draft(_fb(), "2026-08-05")
    assert ig is not None and fb is not None
    assert ig.draft_type == "feed" and not ig.is_story
    assert ig.creative_public_url == fb.creative_public_url  # same served gym
    assert ig.draft_id != fb.draft_id                        # distinct per account
    assert "Gym D" in ig.caption


def test_feed_hook_ignores_non_lasso_accounts(armed):
    welcome_queue.enqueue(_entry("domain:e.com", "Gym E"), host_fn=_fake_host)
    client = Account(key="acme_ig", display_name="Acme", platform=Platform.INSTAGRAM,
                     token_env="X", target_id_env="Y")
    assert welcome_queue.build_welcome_queue_draft(client, "2026-08-05") is None


def test_story_couples_to_feed(armed, tmp_path):
    # The host guard (defense-in-depth layer b) only hosts a GENUINE 9:16 story, so a
    # real 1080x1920 asset must exist on disk for a story_url to be written and the
    # story to couple to the feed (mirrors the 2c21a10 "genuine 9:16 asset" contract).
    from PIL import Image
    story_path = str(tmp_path / "Gym F_story.png")
    Image.new("RGB", (1080, 1920), (12, 20, 42)).save(story_path)
    entry = _entry("domain:f.com", "Gym F")
    entry["posts"]["story"] = story_path
    welcome_queue.enqueue(entry, host_fn=_fake_host)
    feed = welcome_queue.build_welcome_queue_draft(_ig(), "2026-08-05")
    story = welcome_queue.build_welcome_story_draft(_ig(), "2026-08-05", feed_draft=feed)
    assert story is not None
    assert story.is_story and story.draft_type == "story"
    assert story.creative_public_url.endswith("_story.png")


def test_story_refuses_when_feed_was_not_a_welcome(armed):
    welcome_queue.enqueue(_entry("domain:g.com", "Gym G"), host_fn=_fake_host)
    # a non-welcome feed draft (e.g. a book post) must not let a welcome story pop
    class _D:
        draft_id = "book_deadbeef"
    assert welcome_queue.build_welcome_story_draft(_ig(), "2026-08-05", feed_draft=_D()) is None
    # and the queue item is untouched (still servable)
    assert welcome_queue.next_for_day("2026-08-05")["name"] == "Gym G"


def test_story_hook_inert_while_flag_off():
    welcome_queue.enqueue(_entry("domain:h.com", "Gym H"), host_fn=_fake_host)
    class _D:
        draft_id = "welcf_deadbeef"
    assert welcome_queue.build_welcome_story_draft(_ig(), "2026-08-05", feed_draft=_D()) is None


# ---- scan trigger gating ---------------------------------------------------------------

def test_scan_skips_when_flag_off():
    out = welcome_queue.scan_and_enqueue()
    assert out["scanned"] is False and "off" in out["reason"].lower()


def test_scan_force_still_needs_hosting(monkeypatch):
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    out = welcome_queue.scan_and_enqueue(force=True)
    assert out["scanned"] is False and "hosting" in out["reason"].lower()


# ---- manifest seed (Railway path) ------------------------------------------------------

def test_create_from_manifest_seeds_and_is_idempotent(monkeypatch, tmp_path):
    manifest = [
        {"gym_key": "domain:m1.com", "name": "Manifest One", "owner": "Ann",
         "template": "T1", "tier": "Launch", "caption": "Welcome to the LASSO family, Manifest One.",
         "feed_url": "https://cdn.test/m1_feed.png", "story_url": "https://cdn.test/m1_story.png"},
        {"gym_key": "cust:cus_x", "name": "Manifest Two", "owner": "",
         "template": "T2", "tier": "", "caption": "Welcome to the LASSO family, Manifest Two.",
         "feed_url": "https://cdn.test/m2_feed.png", "story_url": ""},
    ]
    mpath = tmp_path / "welcome_queue_manifest.json"
    mpath.write_text(__import__("json").dumps(manifest))
    monkeypatch.setattr(welcome_queue, "MANIFEST_PATH", str(mpath))

    assert welcome_queue.create_from_manifest() == 2
    names = {r["name"] for r in welcome_queue.queue_status()}
    assert names == {"Manifest One", "Manifest Two"}
    # both gyms are ledger-stamped so the daily scan never re-queues them
    assert welcome_posts.already_welcomed("domain:m1.com")
    # re-running seeds nothing new (idempotent by gym_key)
    assert welcome_queue.create_from_manifest() == 0
    assert len(welcome_queue.queue_status()) == 2


def test_manifest_seed_then_drip_serves_it(armed, monkeypatch, tmp_path):
    manifest = [{"gym_key": "domain:drip.com", "name": "Drip Gym", "owner": "Lee",
                 "template": "T1", "tier": "Launch",
                 "caption": "Welcome to the LASSO family, Drip Gym.",
                 "feed_url": "https://cdn.test/d_feed.png",
                 "story_url": "https://cdn.test/d_story.png"}]
    mpath = tmp_path / "welcome_queue_manifest.json"
    mpath.write_text(__import__("json").dumps(manifest))
    monkeypatch.setattr(welcome_queue, "MANIFEST_PATH", str(mpath))
    welcome_queue.create_from_manifest()
    feed = welcome_queue.build_welcome_queue_draft(_ig(), "2026-08-06")
    assert feed is not None and feed.creative_public_url == "https://cdn.test/d_feed.png"
    story = welcome_queue.build_welcome_story_draft(_ig(), "2026-08-06", feed_draft=feed)
    assert story is not None and story.is_story
