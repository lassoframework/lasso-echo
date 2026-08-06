"""
Demo calendar RENDER tests. Offline, pure PIL (no network, no R2).

Guards the Blake ruling that every one of the 30 feed cards + 6 story cards is a
DISTINCT image. The pillar copy bank repeats hooks across days (six All-in-one days,
etc.), so the compositor derives a deterministic per-post VARIANT from (pillar, num):
a ground shade, a layout composition, and an accent placement. Same-pillar cards stay
a family; no two are byte-identical.

Asserts:
  * render_all emits exactly 36 files (30 feed + 6 story) and they are ALL distinct by
    content hash (the core anti-duplication contract).
  * the render_all built-in collision gate raises if any two files ever match.
  * every card is a distinct member of its pillar family: same-pillar cards differ.
  * the on-image invariants still hold (no digit, no dash) for every label + headline.
  * variation is deterministic: re-rendering yields the identical hash set.
"""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import demo_calendar_render as r  # noqa: E402
from agent.demo_calendar_queue import DEMO_POSTS, _story_filename  # noqa: E402

_DASHES = "‐‑‒–—―−-"


def _hash(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _hashes_by_name(tmp_path):
    paths = r.render_all(str(tmp_path))
    return {os.path.basename(p): _hash(p) for p in paths}, paths


# ---- the core contract: 36 files, all distinct -----------------------------------------

def test_render_all_emits_30_feed_plus_6_story(tmp_path):
    _, paths = _hashes_by_name(tmp_path)
    feed = [p for p in paths if not p.endswith("_story.png")]
    story = [p for p in paths if p.endswith("_story.png")]
    assert len(feed) == 30
    assert len(story) == 6
    assert len(paths) == 36


def test_all_36_cards_are_distinct_images(tmp_path):
    by_name, paths = _hashes_by_name(tmp_path)
    hashes = list(by_name.values())
    # the whole point: no two of the 36 files share an image hash
    assert len(set(hashes)) == 36, (
        "duplicate art: only "
        f"{len(set(hashes))} distinct images across {len(hashes)} files")


def test_render_all_raises_on_any_collision(tmp_path, monkeypatch):
    """The built-in distinct-art gate must fire if two posts ever render identically.
    Force a collision by making every post map to the SAME variant, then confirm the
    gate raises rather than silently emitting clones."""
    monkeypatch.setattr(r, "_variant", lambda post: (r.NAVY, 0, 0))
    with pytest.raises(AssertionError, match="duplicate art"):
        r.render_all(str(tmp_path))


# ---- same-pillar cards are a distinct family, never clones ------------------------------

def test_same_pillar_cards_are_all_distinct(tmp_path):
    by_name, _ = _hashes_by_name(tmp_path)
    by_pillar = {}
    for post in DEMO_POSTS:
        by_pillar.setdefault(post["pillar"], []).append(by_name[post["filename"]])
    for pillar, hs in by_pillar.items():
        assert len(set(hs)) == len(hs), (
            f"pillar {pillar!r} has duplicate feed cards: {len(hs)} files, "
            f"{len(set(hs))} distinct")


def test_variation_is_deterministic(tmp_path):
    """Two independent renders must produce the identical set of image hashes (the
    variation is seeded by (pillar, num), never random)."""
    a = {os.path.basename(p): _hash(p) for p in r.render_all(str(tmp_path / "a"))}
    b = {os.path.basename(p): _hash(p) for p in r.render_all(str(tmp_path / "b"))}
    assert a == b


# ---- on-image invariants survive the variation -----------------------------------------

def test_no_onimage_text_carries_a_digit():
    texts = (list(r._EYEBROW.values()) + list(r._ONIMAGE_HEADLINE.values())
             + [t for v in r._TILES.values() for t in v]
             + [s for v in r._STEPS.values() for s in v]
             + [b for v in r._BARS.values() for b in v]
             + list(r._PROOF_LINES))
    for t in texts:
        assert not any(c.isdigit() for c in t), f"on-image text has a digit: {t!r}"


def test_no_onimage_text_carries_a_dash():
    texts = (list(r._EYEBROW.values()) + list(r._ONIMAGE_HEADLINE.values())
             + [t for v in r._TILES.values() for t in v]
             + [s for v in r._STEPS.values() for s in v]
             + [b for v in r._BARS.values() for b in v]
             + list(r._PROOF_LINES) + list(r._RED_WORD.values()))
    for t in texts:
        assert not any(ch in _DASHES for ch in t), f"on-image text has a dash: {t!r}"


def test_every_post_has_a_deterministic_variant():
    """Every post resolves to a concrete (ground, layout_index, accent_index); grounds
    stay in the navy lineage so same-pillar cards read as kin."""
    for post in DEMO_POSTS:
        ground, layout_i, accent_i = r._variant(post)
        assert ground in r.GROUNDS
        assert isinstance(layout_i, int) and layout_i >= 0
        assert isinstance(accent_i, int) and accent_i >= 0
