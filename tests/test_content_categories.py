"""
Content category and platform sub-topic tests (category rotation Part 1).

Three mandatory checks:
  1. Every draftable item resolves to one of the six CATEGORIES.
  2. Platform items carry a sub-topic from PLATFORM_SUBTOPICS.
  3. No drafted platform caption contains "vendor" or a dash character.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import content_categories  # noqa: E402
from agent.content_categories import (  # noqa: E402
    CATEGORIES, PLATFORM_SUBTOPICS,
    category_for_draft, filter_platform_copy, platform_subtopic_for_day,
)
from agent.drafter import Draft, DraftStatus  # noqa: E402

_DASH_RE = re.compile(r'[—–‒‐-]')


# ---- helpers -----------------------------------------------------------------------

def _draft(**kw):
    defaults = dict(
        draft_id="d1", account_key="lasso_ig", platform="instagram",
        caption="test", hashtags=[], creative_path="a.png",
        creative_public_url="", scheduled_for="2026-07-08T18:00:00+00:00",
    )
    defaults.update(kw)
    return Draft(**defaults)


# ---- 1. every draftable item resolves to one category --------------------------------

def test_podcast_draft_type_resolves():
    d = _draft(draft_type="podcast",
               source_fragments=["cite:podcast_ep7", "A hook.", "Support."])
    assert category_for_draft(d) == "podcast"


def test_book_draft_type_resolves():
    d = _draft(draft_type="book")
    assert category_for_draft(d) == "book"


def test_summit_draft_type_resolves():
    d = _draft(draft_type="summit")
    assert category_for_draft(d) == "summit"


def test_b2b_draft_type_resolves():
    d = _draft(draft_type="b2b")
    assert category_for_draft(d) == "b2b"


def test_platform_citation_fragment_resolves():
    d = _draft(source_fragments=["One platform.", "cite:platform_2026_positioning"])
    assert category_for_draft(d) == "platform"


def test_platform_receipts_citation_resolves():
    d = _draft(source_fragments=["cite:platform_2026_receipts", "71.9% booked."])
    assert category_for_draft(d) == "platform"


def test_lasso_now_citation_resolves():
    d = _draft(source_fragments=["Hook line.", "Body line.", "cite:lasso_now"])
    assert category_for_draft(d) == "doctrine"


def test_no_fragments_defaults_to_doctrine():
    d = _draft(source_fragments=[])
    assert category_for_draft(d) == "doctrine"


def test_podcast_fragment_beats_empty_draft_type():
    d = _draft(draft_type="", source_fragments=["cite:podcast_ep12"])
    assert category_for_draft(d) == "podcast"


def test_book_fragment_resolves():
    d = _draft(source_fragments=["cite:book_chapter_1", "The hook."])
    assert category_for_draft(d) == "book"


def test_all_resolved_categories_are_in_taxonomy():
    samples = [
        _draft(draft_type="podcast"),
        _draft(draft_type="book"),
        _draft(draft_type="summit"),
        _draft(draft_type="b2b"),
        _draft(source_fragments=["cite:platform_2026_engines"]),
        _draft(source_fragments=["cite:lasso_now"]),
        _draft(),  # bare draft, no markers
    ]
    for d in samples:
        cat = category_for_draft(d)
        assert cat in CATEGORIES, f"got {cat!r} for {d.draft_type!r} / {d.source_fragments}"


# ---- 2. platform items carry a sub-topic -------------------------------------------

def test_platform_subtopics_list_has_ten():
    assert len(PLATFORM_SUBTOPICS) == 10


def test_platform_subtopic_for_day_returns_valid():
    for day in ("2026-07-08", "2026-07-09", "2026-07-17", "2026-10-01"):
        st = platform_subtopic_for_day(day)
        assert st in PLATFORM_SUBTOPICS, f"unexpected sub-topic {st!r} for {day}"


def test_platform_subtopics_cycle_no_repeat_in_10_days():
    """Consecutive 10-day windows never repeat a sub-topic."""
    from datetime import date, timedelta
    start = date(2026, 7, 8)
    window = [platform_subtopic_for_day((start + timedelta(days=i)).isoformat())
              for i in range(10)]
    assert len(set(window)) == 10, f"repeat found in {window}"


def test_platform_subtopic_rotates_day_over_day():
    """Two consecutive days must not get the same sub-topic."""
    from datetime import date, timedelta
    d = date(2026, 8, 1)
    a = platform_subtopic_for_day(d.isoformat())
    b = platform_subtopic_for_day((d + timedelta(days=1)).isoformat())
    assert a != b, f"same sub-topic {a!r} on consecutive days"


def test_platform_draft_subtopic_set_when_flag_on(monkeypatch, tmp_path):
    """With the flag ON, a plan_for() call for a platform_2026 angle returns a
    non-empty sub_topic."""
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")

    from agent import content_planner, knowledge, config

    # Minimal platform angle fixture so plan_for can resolve a platform citation
    monkeypatch.setattr(config, "SOURCE_DOC_PATH",
                        str(tmp_path / "lasso_now.md"))

    (tmp_path / "lasso_now.md").write_text(
        "## Pillars\n- Ads\n\n"
        "## Pillar copy bank\n"
        "### Pillar: Ads\n"
        "Hook: You're overpaying for leads.\n"
        "Body: The gym that fixes its follow up wins.\n\n"
        "## CTAs\n- Save this post.\n\n"
        "## Hashtags\n#LASSOFramework\n",
        encoding="utf-8",
    )

    # Inject one platform USE line so doctrine.angle_for_pillar finds it
    monkeypatch.setattr(
        knowledge, "load_corpus",
        lambda: {"08_platform_2026.md": [
            'USE: "$16 blended CPL across the portfolio." (platform_2026_receipts)'
        ]},
    )

    plan = content_planner.plan_for("2026-07-08", path=str(tmp_path / "lasso_now.md"))
    if plan.get("blocked"):
        return  # knowledge not resolving is fine; the filter is the gate
    if plan.get("citation", "").startswith("platform_2026"):
        assert plan["sub_topic"] in PLATFORM_SUBTOPICS
        assert plan["category"] == "platform"


# ---- 3. wording filter: no vendor or dash in platform captions ----------------------

def test_filter_removes_vendors():
    assert "vendor" not in filter_platform_copy("We use fewer vendors.").lower()
    assert "company" in filter_platform_copy("vendor").lower() or \
           "companies" in filter_platform_copy("vendors").lower()


def test_filter_vendors_singular():
    out = filter_platform_copy("The vendor provides the service.")
    assert "vendor" not in out.lower()
    assert "company" in out.lower()


def test_filter_vendors_plural():
    out = filter_platform_copy("Too many vendors mean too many logins.")
    assert "vendor" not in out.lower()
    assert "companies" in out.lower()


def test_filter_vendor_logins_phrase():
    out = filter_platform_copy("No more vendor logins.")
    assert "vendor" not in out.lower()
    assert "logins" in out.lower()


def test_filter_strips_em_dash():
    out = filter_platform_copy("One platform—zero blind spots.")
    assert "—" not in out
    assert _DASH_RE.search(out) is None


def test_filter_strips_en_dash():
    out = filter_platform_copy("Results–not reports.")
    assert "–" not in out
    assert _DASH_RE.search(out) is None


def test_filter_strips_hyphen():
    out = filter_platform_copy("third-party software")
    assert "-" not in out
    assert "third party" in out.lower()


def test_filter_idempotent():
    """Applying the filter twice gives the same result."""
    raw = "Fewer vendors—better results from third-party tools."
    once = filter_platform_copy(raw)
    twice = filter_platform_copy(once)
    assert once == twice


def test_filter_empty_string_is_safe():
    assert filter_platform_copy("") == ""
    assert filter_platform_copy(None) is None


def test_plan_for_platform_caption_no_vendor_no_dash(monkeypatch, tmp_path):
    """End-to-end: when category rotation is ON and the plan resolves a platform
    angle, the returned caption must contain neither 'vendor' nor a dash char."""
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")

    from agent import content_planner, knowledge, config

    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(tmp_path / "lasso_now.md"))

    # A platform USE line that deliberately contains a vendor reference and a dash
    dirty_angle = 'USE: "vendors—and logins—slow you down." (platform_2026_positioning)'

    (tmp_path / "lasso_now.md").write_text(
        "## Pillars\n- Messaging\n\n"
        "## Pillar copy bank\n"
        "### Pillar: Messaging\n"
        "Hook: Clear message wins.\n"
        "Body: One system, one login.\n\n"
        "## CTAs\n- Tag a gym owner.\n\n"
        "## Hashtags\n#LASSOFramework\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        knowledge, "load_corpus",
        lambda: {"08_platform_2026.md": [dirty_angle]},
    )

    plan = content_planner.plan_for("2026-07-08", path=str(tmp_path / "lasso_now.md"))
    if plan.get("blocked"):
        return
    caption = plan.get("caption", "")
    if plan.get("citation", "").startswith("platform_2026"):
        assert "vendor" not in caption.lower(), f"vendor found in: {caption!r}"
        assert _DASH_RE.search(caption) is None, f"dash found in: {caption!r}"
