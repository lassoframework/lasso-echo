"""portal_domains: a recorded domain must resolve from the gym's BASE KEY too, not
only from its human name (the lookup miss that left The Bolton Club without a bible)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_domains as pd  # noqa: E402


def test_domain_resolves_from_a_base_key_not_only_a_human_name():
    """Echo's callers pass BASE KEYS ('theboltonclub'), while the registry is filed under
    human names ('the bolton club'). The miss left a live gym without a voice bible."""
    assert pd.domain_for("the bolton club") == "theboltonclub.com"
    assert pd.domain_for("theboltonclub") == "theboltonclub.com"
    assert pd.record_for("theboltonclub") is not None
    # An unrecorded gym still returns empty, never a guessed domain.
    assert pd.domain_for("nosuchgymanywhere") == ""


def test_base_key_index_refuses_an_ambiguous_match():
    """Two recorded names that compact to the same base key must resolve to NOTHING
    rather than to whichever was inserted last."""
    saved = dict(pd._COMPACT_INDEX)
    try:
        pd._COMPACT_INDEX.pop("ambiguousgym", None)
        assert pd.domain_for("ambiguousgym") == ""
    finally:
        pd._COMPACT_INDEX.clear()
        pd._COMPACT_INDEX.update(saved)
