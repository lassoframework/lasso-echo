"""
Tests for agent/onboard_dryrun.py

All tests are fully offline: no live tokens, no network calls, no DB writes.
"""

import sys
import os

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_stubs():
    """Minimal fixture stubs: one per category in _STUB_CATEGORIES."""
    from agent.onboard_dryrun import _STUB_CATEGORIES
    return [
        {
            "key": f"dryrun_concept_{n}",
            "caption": f"DRYRUN: {cat} sample post.",
            "category": cat,
            "source_doc": "sample_source.md",
        }
        for n, cat in enumerate(_STUB_CATEGORIES, start=1)
    ]


# ---------------------------------------------------------------------------
# test_dryrun_drafts_30_days
# ---------------------------------------------------------------------------

def test_dryrun_drafts_30_days():
    """days_drafted must equal a full-month posting count (28-31)."""
    from agent.onboard_dryrun import run
    result = run("fixture_gym", month="2026-08", _fixture_stubs=_stub_stubs())
    assert 28 <= result["days_drafted"] <= 31, (
        f"Expected 28-31 days drafted, got {result['days_drafted']}"
    )
    assert result["days_planned"] == result["days_drafted"]


# ---------------------------------------------------------------------------
# test_dryrun_category_spread
# ---------------------------------------------------------------------------

def test_dryrun_category_spread():
    """At least 3 distinct categories must appear in category_spread."""
    from agent.onboard_dryrun import run
    result = run("fixture_gym", month="2026-08", _fixture_stubs=_stub_stubs())
    spread = result["category_spread"]
    assert len(spread) >= 3, (
        f"Expected at least 3 categories, got {len(spread)}: {spread}"
    )


# ---------------------------------------------------------------------------
# test_dryrun_every_draft_cites_source
# ---------------------------------------------------------------------------

def test_dryrun_every_draft_cites_source():
    """Every draft in drafts[] must have a non-empty source_doc."""
    from agent.onboard_dryrun import run
    result = run("fixture_gym", month="2026-07", _fixture_stubs=_stub_stubs())
    drafts = result["drafts"]
    assert drafts, "Expected at least one draft"
    for d in drafts:
        assert d.get("source_doc"), (
            f"Draft on {d.get('date')} has empty source_doc"
        )


# ---------------------------------------------------------------------------
# test_dryrun_no_publish_no_live_tokens
# ---------------------------------------------------------------------------

def test_dryrun_no_publish_no_live_tokens():
    """
    The onboard_dryrun module must not import any live I/O module (requests,
    boto3, slack_sdk, httpx, urllib3) as a top-level import.  We inspect the
    module's own imports by checking sys.modules for those names BEFORE and
    AFTER importing the module in a fresh context.

    Because we cannot truly unload already-imported modules in the same
    process, we check that none of the live I/O packages were *introduced*
    by importing onboard_dryrun in an environment where they were absent.
    """
    # The set of live I/O module prefixes that must never be needed by dryrun
    live_io_prefixes = ("requests", "boto3", "slack_sdk", "httpx", "urllib3",
                        "aiohttp", "botocore")

    # Capture what was present before
    before = set(sys.modules.keys())

    # Import (or re-import) the module
    import importlib
    import agent.onboard_dryrun as mod
    importlib.reload(mod)

    # New entries introduced by the import
    after = set(sys.modules.keys())
    new_modules = after - before

    introduced_live = [
        m for m in new_modules
        if any(m == p or m.startswith(p + ".") for p in live_io_prefixes)
    ]
    assert introduced_live == [], (
        f"onboard_dryrun introduced live I/O modules: {introduced_live}"
    )


# ---------------------------------------------------------------------------
# test_render_html_is_self_contained
# ---------------------------------------------------------------------------

def test_render_html_is_self_contained():
    """
    render_dryrun_html output must contain no external src= URLs.

    An external URL is defined as any src= attribute whose value starts with
    http:// or https://.  Data URIs (data:) and relative paths are fine.
    """
    import re
    from agent.onboard_dryrun import run, render_dryrun_html

    result = run("fixture_gym", month="2026-08", _fixture_stubs=_stub_stubs())
    html = render_dryrun_html(result)

    assert html, "render_dryrun_html returned empty string"

    # Check for external src= attributes
    external_src = re.findall(r'src=["\']https?://', html, re.IGNORECASE)
    assert external_src == [], (
        f"HTML contains external src= URLs: {external_src}"
    )

    # Check for external href= attributes (stylesheets, etc.)
    external_href = re.findall(r'href=["\']https?://', html, re.IGNORECASE)
    assert external_href == [], (
        f"HTML contains external href= URLs: {external_href}"
    )

    # Must contain the footer disclaimer
    assert "DRYRUN ONLY" in html
