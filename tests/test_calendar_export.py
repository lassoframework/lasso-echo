"""
calendar-export tests (calendar Part C). Offline. Asserts:
  - multi-account JSON wrapper has required fields (month, accounts, days, rollup)
  - standalone HTML contains all V3 brand colors
  - standalone HTML is self-contained (no external src= URLs)
  - standalone HTML has account switcher buttons for all accounts
  - standalone HTML has no em-dash or en-dash characters in rendered output
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_artifact, db  # noqa: E402

MONTH = "2026-07"


def test_calendar_export_json_has_required_fields(monkeypatch):
    """assemble_month wrapped in the multi-account dict has all required keys."""
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    plan_ig = calendar_artifact.assemble_month("lasso_ig", MONTH)
    plan_fb = calendar_artifact.assemble_month("lasso_fb", MONTH)
    payload = {
        "month": MONTH,
        "accounts": {"lasso_ig": plan_ig, "lasso_fb": plan_fb},
    }
    assert payload["month"] == MONTH
    assert "accounts" in payload
    for ak in ("lasso_ig", "lasso_fb"):
        acct_plan = payload["accounts"][ak]
        assert "days" in acct_plan, f"{ak} missing 'days'"
        assert "rollup" in acct_plan, f"{ak} missing 'rollup'"
        assert len(acct_plan["days"]) == 31


def test_standalone_html_has_brand_colors(monkeypatch):
    """generate_standalone_html output contains all V3 brand palette colors."""
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    plan_ig = calendar_artifact.assemble_month("lasso_ig", MONTH)
    plan_fb = calendar_artifact.assemble_month("lasso_fb", MONTH)
    html = calendar_artifact.generate_standalone_html(
        {"lasso_ig": plan_ig, "lasso_fb": plan_fb}, MONTH
    )
    assert "#121E3C" in html, "page/grid background navy missing"
    assert "#FF0000" in html, "accent red missing"
    assert "#FAF6F0" in html, "cream/text background missing"


def test_standalone_html_is_self_contained(monkeypatch):
    """No http:// or https:// in src= attributes (all CSS/JS inline, no CDN)."""
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    plan_ig = calendar_artifact.assemble_month("lasso_ig", MONTH)
    html = calendar_artifact.generate_standalone_html({"lasso_ig": plan_ig}, MONTH)
    # Find all src= attribute values
    src_pattern = re.compile(r'\bsrc\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
    for match in src_pattern.finditer(html):
        url = match.group(1)
        assert not url.startswith("http://") and not url.startswith("https://"), (
            f"External src= found: {url!r}"
        )


def test_standalone_html_account_switcher(monkeypatch):
    """Output contains button/tab text for each account key."""
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    plan_ig = calendar_artifact.assemble_month("lasso_ig", MONTH)
    plan_fb = calendar_artifact.assemble_month("lasso_fb", MONTH)
    html = calendar_artifact.generate_standalone_html(
        {"lasso_ig": plan_ig, "lasso_fb": plan_fb}, MONTH
    )
    assert "lasso_ig" in html, "lasso_ig account button/tab not found"
    assert "lasso_fb" in html, "lasso_fb account button/tab not found"
    # Should have a button element per account
    assert "tab-btn-lasso_ig" in html
    assert "tab-btn-lasso_fb" in html


def test_standalone_html_dash_free(monkeypatch):
    """No em-dash or en-dash characters appear anywhere in the rendered output."""
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    plan_ig = calendar_artifact.assemble_month("lasso_ig", MONTH)
    plan_fb = calendar_artifact.assemble_month("lasso_fb", MONTH)
    html = calendar_artifact.generate_standalone_html(
        {"lasso_ig": plan_ig, "lasso_fb": plan_fb}, MONTH
    )
    assert "—" not in html, "em-dash found in output"
    assert "–" not in html, "en-dash found in output"
