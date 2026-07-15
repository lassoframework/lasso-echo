"""
Onboard dryrun: 30-day planning and drafting harness with no live tokens,
no network calls, no DB writes, and no Slack posts.

Usage:
    from agent.onboard_dryrun import run, render_dryrun_html
    result = run("my_gym", month="2026-08")
    html   = render_dryrun_html(result)

Or via CLI:
    python -m agent onboard-dryrun --account <key> [--month YYYY-MM] [--out <path>]

What it does:
    1. Creates a temporary directory with 5 stub concept JSON files (one per category)
       and a sample_source.md.
    2. Walks all posting days in the given month (gated by schedule.should_post_on).
    3. Assigns concepts round-robin across the stubs (no DB, no rotation window).
    4. Returns a dryrun_result dict; never writes to the database or calls any
       live service.

All flags default OFF.  This module never imports requests, boto3, slack_sdk,
or any other live I/O module.
"""

import json
import os
import tempfile
from calendar import monthrange
from datetime import date as _date

from . import schedule
from .content_categories import CATEGORIES


# The five category stubs used in dryrun: a representative cross-section.
_STUB_CATEGORIES = ("podcast", "platform", "b2b", "doctrine", "book")


def _make_stub_library(tmp_dir):
    """
    Write 5 stub concept JSON files and a sample_source.md into tmp_dir.
    Returns a list of stub dicts.
    """
    source_text = (
        "DRYRUN SAMPLE SOURCE\n\n"
        "This is placeholder content for the onboard dryrun harness. "
        "No real client data, no real claims, no fabricated facts. "
        "Replace this file with the actual approved source document before "
        "any real content is drafted."
    )
    source_path = os.path.join(tmp_dir, "sample_source.md")
    with open(source_path, "w", encoding="utf-8") as fh:
        fh.write(source_text)

    stubs = []
    for n, cat in enumerate(_STUB_CATEGORIES, start=1):
        key = f"dryrun_concept_{n}"
        caption = f"DRYRUN: {cat} sample post."
        stub = {
            "key": key,
            "caption": caption,
            "category": cat,
            "source_doc": "sample_source.md",
        }
        concept_path = os.path.join(tmp_dir, f"{key}.json")
        with open(concept_path, "w", encoding="utf-8") as fh:
            json.dump(stub, fh, indent=2)
        stubs.append(stub)
    return stubs


def _open_posting_days(month):
    """
    Return the list of YYYY-MM-DD day keys in the given month that pass
    schedule.should_post_on.  No DB access.
    """
    year, mon = int(month[:4]), int(month[5:7])
    n_days = monthrange(year, mon)[1]
    return [
        f"{month}-{d:02d}"
        for d in range(1, n_days + 1)
        if schedule.should_post_on(f"{month}-{d:02d}")
    ]


def run(account_key, month=None, _fixture_stubs=None):
    """
    Run the 30-day onboard dryrun for account_key.

    Parameters
    ----------
    account_key : str
        The account slug being onboarded (no live tokens required).
    month : str, optional
        YYYY-MM string.  Defaults to the current calendar month.
    _fixture_stubs : list of dicts, optional
        Inject pre-built stubs for unit tests (skips tmp dir creation).

    Returns
    -------
    dict with keys:
        account_key, month, days_planned, days_drafted, category_spread,
        drafts (list of draft dicts), gaps (list of day keys with no concept).
    """
    if month is None:
        today = _date.today()
        month = today.strftime("%Y-%m")

    # Seed stub library (or use injected fixture for tests)
    if _fixture_stubs is not None:
        stubs = _fixture_stubs
        tmp_dir = None
    else:
        tmp_dir = tempfile.mkdtemp(prefix="echo_dryrun_")
        stubs = _make_stub_library(tmp_dir)

    open_days = _open_posting_days(month)

    drafts = []
    gaps = []

    # Round-robin across stubs so every category gets a turn
    n_stubs = len(stubs)
    for i, day_key in enumerate(open_days):
        stub = stubs[i % n_stubs]
        draft = {
            "date": day_key,
            "concept": stub["key"],
            "caption": stub["caption"],
            "category": stub["category"],
            "source_doc": stub["source_doc"],
            "status": "draft",
        }
        drafts.append(draft)

    # Category spread
    category_spread = {}
    for d in drafts:
        cat = d["category"]
        category_spread[cat] = category_spread.get(cat, 0) + 1

    return {
        "account_key": account_key,
        "month": month,
        "days_planned": len(open_days),
        "days_drafted": len(drafts),
        "category_spread": category_spread,
        "drafts": drafts,
        "gaps": gaps,
    }


def render_dryrun_html(dryrun_result):
    """
    Produce a self-contained HTML review bundle for the given dryrun_result.

    No external dependencies: all CSS is inline, no external fonts, no CDN
    scripts, no remote images.  Safe to save to disk and open in any browser.
    """
    account_key = dryrun_result.get("account_key", "")
    month = dryrun_result.get("month", "")
    days_planned = dryrun_result.get("days_planned", 0)
    days_drafted = dryrun_result.get("days_drafted", 0)
    category_spread = dryrun_result.get("category_spread", {})
    drafts = dryrun_result.get("drafts", [])

    # Category bar chart (pure CSS, inline)
    bar_max = max(category_spread.values(), default=1)
    bar_rows = ""
    for cat, count in sorted(category_spread.items()):
        pct = int(round(count / bar_max * 100))
        bar_rows += f"""
        <tr>
          <td class="cat-label">{_esc(cat)}</td>
          <td class="bar-cell">
            <div class="bar" style="width:{pct}%">&nbsp;</div>
          </td>
          <td class="count-cell">{count}</td>
        </tr>"""

    # Draft table rows
    table_rows = ""
    for d in drafts:
        caption_preview = _esc((d.get("caption") or "")[:120])
        table_rows += f"""
        <tr>
          <td class="date-cell">{_esc(d.get('date',''))}</td>
          <td>{_esc(d.get('category',''))}</td>
          <td>{_esc(d.get('concept',''))}</td>
          <td class="caption-cell">{caption_preview}</td>
          <td>{_esc(d.get('source_doc',''))}</td>
        </tr>"""

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Echo Onboard Dryrun: {_esc(account_key)} {_esc(month)}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #121E3C;
      color: #FAF6F0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.5;
      padding: 2rem 1.5rem;
    }}
    a {{ color: #5EB9E6; }}
    h1 {{
      font-size: 1.5rem;
      color: #5EB9E6;
      margin-bottom: 0.25rem;
    }}
    h2 {{
      font-size: 1rem;
      color: #5EB9E6;
      margin: 1.5rem 0 0.5rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .meta {{
      font-size: 0.85rem;
      color: #b0c4d8;
      margin-bottom: 1.5rem;
    }}
    .stat-row {{
      display: flex;
      gap: 2rem;
      margin-bottom: 1.5rem;
      flex-wrap: wrap;
    }}
    .stat {{
      background: #1b2d52;
      border: 1px solid #2a4070;
      border-radius: 6px;
      padding: 0.75rem 1.25rem;
      min-width: 130px;
    }}
    .stat-label {{
      font-size: 0.75rem;
      color: #8aa8c8;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .stat-value {{
      font-size: 1.6rem;
      font-weight: 700;
      color: #5EB9E6;
    }}
    /* Bar chart */
    .bar-table {{
      border-collapse: collapse;
      width: 100%;
      max-width: 480px;
      margin-bottom: 1.5rem;
    }}
    .cat-label {{
      width: 90px;
      padding: 3px 8px 3px 0;
      color: #FAF6F0;
      text-align: right;
      font-size: 0.82rem;
    }}
    .bar-cell {{
      padding: 3px 6px;
    }}
    .bar {{
      background: #5EB9E6;
      height: 16px;
      border-radius: 2px;
      min-width: 4px;
    }}
    .count-cell {{
      padding: 3px 0 3px 6px;
      color: #b0c4d8;
      font-size: 0.82rem;
      white-space: nowrap;
    }}
    /* Draft table */
    .scroll-wrap {{
      overflow-x: auto;
      margin-bottom: 1.5rem;
    }}
    table.drafts {{
      border-collapse: collapse;
      width: 100%;
      min-width: 600px;
    }}
    table.drafts th {{
      background: #1b2d52;
      color: #5EB9E6;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.5rem 0.75rem;
      text-align: left;
      border-bottom: 1px solid #2a4070;
      white-space: nowrap;
    }}
    table.drafts td {{
      padding: 0.45rem 0.75rem;
      border-bottom: 1px solid #1e2e4e;
      vertical-align: top;
    }}
    table.drafts tr:hover td {{
      background: #1b2d52;
    }}
    .date-cell {{
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .caption-cell {{
      max-width: 340px;
      word-break: break-word;
    }}
    .footer {{
      margin-top: 2rem;
      padding-top: 1rem;
      border-top: 1px solid #2a4070;
      font-size: 0.8rem;
      color: #6a8faa;
    }}
  </style>
</head>
<body>
  <h1>Echo Onboard Dryrun</h1>
  <div class="meta">{_esc(account_key)} &nbsp;|&nbsp; {_esc(month)}</div>

  <div class="stat-row">
    <div class="stat">
      <div class="stat-label">Days Planned</div>
      <div class="stat-value">{days_planned}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Days Drafted</div>
      <div class="stat-value">{days_drafted}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Categories</div>
      <div class="stat-value">{len(category_spread)}</div>
    </div>
  </div>

  <h2>Category Spread</h2>
  <table class="bar-table" aria-label="Category spread">{bar_rows}
  </table>

  <h2>Draft Schedule</h2>
  <div class="scroll-wrap">
    <table class="drafts">
      <thead>
        <tr>
          <th>Date</th>
          <th>Category</th>
          <th>Concept</th>
          <th>Caption Preview</th>
          <th>Source Doc</th>
        </tr>
      </thead>
      <tbody>{table_rows}
      </tbody>
    </table>
  </div>

  <div class="footer">
    DRYRUN ONLY: nothing published, no live tokens used
  </div>
</body>
</html>"""
    return html


def _esc(text):
    """Minimal HTML escaping for inline content."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
