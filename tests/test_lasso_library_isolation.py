"""
Brand-integrity isolation: a LASSO post can only ever use LASSO's OWN brand assets,
never a client gym's uploaded media.

The bug this locks down (draft c109d97909, 2026-08-21): LASSO accounts have an empty
library_prefix, so their library resolves to the SHARED content_library/ parent, which
holds every client gym's library as a subfolder (content_library/eng, .../gritx, ...).
list_creatives() on that parent absorbed each gym subfolder as a bogus "carousel"
(content_library/eng -> a 23 photo carousel of ENG's members), so LASSO's library
fallback shipped a client's member photos under LASSO's B2B caption.

Fully offline (tmp_path; no S3, no db, no network).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import library  # noqa: E402
from agent.library import list_creatives, is_client_gym_asset  # noqa: E402


def _img(folder, name):
    p = os.path.join(folder, name)
    with open(p, "wb") as fh:
        fh.write(b"x" * 20000)
    return p


@pytest.fixture
def registered_gyms(monkeypatch):
    """Pin the registered gym library roots so the test does not depend on the live
    account registry."""
    monkeypatch.setattr(library, "_gym_library_dirnames",
                        lambda: {"eng", "gritx", "topfuel"})


def test_gym_subfolder_is_not_absorbed_as_a_carousel(tmp_path, registered_gyms):
    """content_library/ (the LASSO parent) holds LASSO's own root PNGs AND a gym
    subfolder. list_creatives must return the LASSO root assets and must NOT return
    the gym subfolder as a carousel."""
    parent = tmp_path / "content_library"
    parent.mkdir()
    # LASSO's own brand assets live at the parent ROOT (as in prod).
    _img(str(parent), "demo_07_we_chase.png")
    _img(str(parent), "lasso_v2_ads_done_for_you.png")
    # A client gym's library subfolder with many member photos (the ENG case).
    eng = parent / "eng"
    eng.mkdir()
    for n in ("Robin_trio.jpg", "Brooke3.jpg", "kids4.jpg", "Nicola.jpg"):
        _img(str(eng), n)

    creatives = list_creatives(str(parent))
    paths = [c.path for c in creatives]
    types = {c.media_type for c in creatives}

    # LASSO's own root assets are present...
    assert any(p.endswith("demo_07_we_chase.png") for p in paths)
    assert any(p.endswith("lasso_v2_ads_done_for_you.png") for p in paths)
    # ...and the gym subfolder was NOT absorbed as a carousel.
    assert not any(os.path.basename(p) == "eng" for p in paths)
    assert "carousel" not in types


def test_legitimate_carousel_bundle_still_works(tmp_path, registered_gyms):
    """A real carousel bundle (a subfolder that is NOT a registered gym library root)
    is still returned as a carousel: the fix removes cross-gym contamination only."""
    parent = tmp_path / "content_library"
    parent.mkdir()
    bundle = parent / "launch_week_carousel"
    bundle.mkdir()
    _img(str(bundle), "slide_1.png")
    _img(str(bundle), "slide_2.png")
    _img(str(bundle), "slide_3.png")

    creatives = list_creatives(str(parent))
    carousels = [c for c in creatives if c.media_type == "carousel"]
    assert len(carousels) == 1
    assert len(carousels[0].slides) == 3


def test_is_client_gym_asset(registered_gyms):
    assert is_client_gym_asset("/data/content_library/eng/Robin_trio.jpg")
    assert is_client_gym_asset("content_library/gritx/photo.jpg")
    # LASSO's own root assets are NOT client gym assets.
    assert not is_client_gym_asset("/data/content_library/demo_07_we_chase.png")
    assert not is_client_gym_asset("/data/content_library/lasso_v2_b2b_16_cpl.png")
    assert not is_client_gym_asset("")
