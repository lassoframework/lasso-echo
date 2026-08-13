"""
A+ quality gate: no client calendar post is written unless its caption is a REAL
caption + real media + no dash + no banned word. A thin 'HYROX'-tier caption is
dropped, not published.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import post_quality as pq  # noqa: E402


class _Draft:
    def __init__(self, caption, url="https://r2/x.jpg"):
        self.caption = caption
        self.creative_public_url = url


# ---- caption checks --------------------------------------------------------

def test_raw_source_word_is_not_a_plus():
    assert pq.caption_issues("HYROX")
    assert pq.caption_issues("Small group training")          # 3 words, too thin


def test_thin_body_with_cta_still_fails():
    # the fallback baseline: raw word + CTA. Total content is still thin -> not A+.
    assert pq.caption_issues("HYROX\n\nSave this post.")


def test_real_storybrand_caption_passes():
    cap = ("You walked in nervous, unsure if you belonged. Coach Lester met you with "
           "patience and a real smile. That is the difference between a gym and a family.")
    assert pq.caption_issues(cap) == []


def test_punchy_hook_then_body_passes():
    # a short HOOK followed by a real BODY must NOT be flagged thin (was a false
    # positive when only the first paragraph's words were counted).
    cap = ("Your coach notices you. Really notices.\n\nThat's rare. Most gyms let you "
           "blend into the crowd. Here you are seen from day one.\n\nBook a free intro.")
    assert pq.caption_issues(cap) == []


def test_hashtags_do_not_count_as_content():
    # hashtag lines are not caption copy; a thin caption padded with tags still fails.
    assert pq.caption_issues("HYROX\n\n#eng #hyrox #crossfit #fitness #gym #train #win")


def test_dash_is_rejected():
    assert any("dash" in i for i in pq.caption_issues(
        "You are strong and ready - come train with us today at the gym near you."))
    # a hyphen INSIDE a word is fine
    assert pq.caption_issues(
        "Our co-op community shows up every single morning to train together strong.") == []


def test_banned_word_rejected():
    cap = ("Come compete with us every single week and push your limits hard at the box "
           "downtown today.")
    assert any("banned" in i for i in pq.caption_issues(cap, banned_words=["compete"]))


def test_empty_caption_rejected():
    assert pq.caption_issues("") == ["empty caption"]


# ---- post-level (caption + media) -----------------------------------------

def test_post_needs_media():
    good_cap = ("You are busy and stuck between work and family life. We coach people "
                "just like you back to strong. Start today.")
    assert pq.is_a_plus(_Draft(good_cap))
    assert not pq.is_a_plus(_Draft(good_cap, url=""))
    assert any("media" in i for i in pq.post_issues(_Draft(good_cap, url="")))


# ---- builder integration: SB7 on -> thin caption dropped -------------------

def test_builder_drops_thin_caption_when_sb7_on(monkeypatch, tmp_path):
    import json
    from agent import client_month_run as cmr, client_content, client_sources as cs
    from agent.accounts import Account, Platform
    from agent.voice import VoiceDoc

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")

    # SB7 returns a THIN caption (simulating an LLM that echoed the source) ->
    # make_caption rejects the echo, falls back to the raw source -> gate drops it.
    from agent.drafter import StoryBrandGenerator
    monkeypatch.setattr(StoryBrandGenerator, "build",
                        lambda self, v, c, account=None: ("HYROX", ["#x"], ["b"]))
    cs.add_source("q_ig", "service", "HYROX", "intake")

    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(3):
        (lib / f"p{i}.jpg").write_bytes(b"\xff\xd8\xffJ")
        (lib / f"p{i}.json").write_text(json.dumps({"public_url": f"https://m/p{i}.jpg"}))

    acct = Account(key="q_ig", display_name="Q Gym", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="G")

    class _Store:
        def __init__(self):
            self.inserted = []

        def list_month(self, *a):
            return []

        def delete_month(self, *a, **k):
            return 0

        def insert_rows(self, k, rows):
            self.inserted.extend(rows)
            return rows

    store = _Store()
    out = cmr.build_client_month(acct, "q", "2026-08-01", days=4,
                                 voice=VoiceDoc(raw="Q voice.", hashtags=["#q"],
                                                ctas=["Book now."]),
                                 library_path=str(lib), store=store, banned_words=())
    # every day's only source is the thin 'HYROX' -> all dropped -> nothing inserted
    assert store.inserted == [], "a thin caption must never reach the calendar"
    assert out["days"] == 0
