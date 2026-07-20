"""
handoff_refresh.py — generates /data/handoff_live.html for the Echo admin tracker.

Called by the scheduler at 12pm and 4pm ET, and served at
/admin/tracker/<token>/handoff. Reads live state from SQLite; writes a
self-contained LASSO-branded HTML status page. Never raises on DB failure.
"""

import os
from datetime import datetime, timezone


def _fetch_stats():
    """Pull live stats from DB. Returns a dict with safe fallback values."""
    stats = {
        "pending_count": "—",
        "last_run_date": "—",
        "alerts": [],
        "pending_by_account": [],
    }
    try:
        from . import db

        with db.connect() as conn:
            # pending draft count
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM drafts WHERE status='pending'"
                ).fetchone()
                stats["pending_count"] = row["n"] if row else 0
            except Exception:
                pass

            # last daily run date
            try:
                row = conn.execute(
                    "SELECT value FROM kv WHERE key='last_run_date'"
                ).fetchone()
                stats["last_run_date"] = row["value"] if row else "—"
            except Exception:
                pass

            # recent ops alerts (last 5) — reason is the message body
            try:
                rows = conn.execute(
                    "SELECT reason, ts FROM audit "
                    "WHERE kind='ops_alert' ORDER BY ts DESC LIMIT 5"
                ).fetchall()
                stats["alerts"] = [{"body": r["reason"], "ts": r["ts"]} for r in rows]
            except Exception:
                stats["alerts"] = []

            # pending drafts by account
            try:
                rows = conn.execute(
                    "SELECT account_key, COUNT(*) AS n FROM drafts "
                    "WHERE status='pending' GROUP BY account_key"
                ).fetchall()
                stats["pending_by_account"] = [
                    {"account_key": r["account_key"], "n": r["n"]} for r in rows
                ]
            except Exception:
                pass

    except Exception:
        pass

    return stats


def _html(stats, generated_at):
    pending = stats["pending_count"]
    last_run = stats["last_run_date"]
    alerts = stats["alerts"]
    by_account = stats["pending_by_account"]

    # accounts active: lasso_ig + lasso_fb
    accounts_active = "lasso_ig / lasso_fb"

    # alerts section
    if alerts:
        alert_rows = "\n".join(
            f'<li class="alert-item"><span class="alert-ts">{a["ts"]}</span>'
            f'<span class="alert-body">{a["body"]}</span></li>'
            for a in alerts
        )
        alerts_html = f'<ul class="alert-list">{alert_rows}</ul>'
    else:
        alerts_html = '<p class="muted">No recent alerts.</p>'

    # pending by account breakdown
    if by_account:
        acct_items = " &nbsp;|&nbsp; ".join(
            f'{r["account_key"]}: <strong>{r["n"]}</strong>' for r in by_account
        )
        acct_breakdown = f'<p class="acct-breakdown">{acct_items}</p>'
    else:
        acct_breakdown = ""

    return f"""<title>Echo Live Status</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #FAF6F0;
    color: #121E3C;
    padding: 24px 16px 48px;
    max-width: 720px;
    margin: 0 auto;
  }}
  /* header */
  .eyebrow {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: #5EB9E6;
    margin-bottom: 4px;
  }}
  h1 {{
    font-size: 28px;
    font-weight: 800;
    color: #121E3C;
    line-height: 1.1;
  }}
  .gen-ts {{
    font-size: 12px;
    color: #888;
    margin-top: 4px;
    margin-bottom: 28px;
  }}
  /* grade card */
  .grade-card {{
    background: #121E3C;
    color: #FAF6F0;
    border-radius: 12px;
    padding: 24px 28px;
    display: flex;
    align-items: center;
    gap: 24px;
    margin-bottom: 28px;
  }}
  .grade-chip {{
    font-size: 52px;
    font-weight: 900;
    color: #5EB9E6;
    line-height: 1;
    flex-shrink: 0;
  }}
  .grade-sub {{
    font-size: 15px;
    color: #c0cfe0;
    line-height: 1.4;
  }}
  /* stats row */
  .stats-row {{
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 32px;
  }}
  .stat-card {{
    background: #fff;
    border: 1.5px solid #e0ddd8;
    border-radius: 10px;
    padding: 16px 20px;
    flex: 1;
    min-width: 150px;
  }}
  .stat-label {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: #888;
    margin-bottom: 6px;
  }}
  .stat-value {{
    font-size: 22px;
    font-weight: 800;
    color: #121E3C;
  }}
  /* sections */
  .section {{
    margin-bottom: 32px;
  }}
  .section-title {{
    font-size: 13px;
    font-weight: 800;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: #FF0000;
    margin-bottom: 12px;
    padding-bottom: 6px;
    border-bottom: 2px solid #FF0000;
  }}
  /* todo list */
  .todo-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .todo-item {{
    background: #fff;
    border: 1.5px solid #e0ddd8;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 14px;
    line-height: 1.5;
  }}
  .todo-item .deadline {{
    display: inline-block;
    background: #FF0000;
    color: #fff;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .06em;
    text-transform: uppercase;
    border-radius: 4px;
    padding: 1px 6px;
    margin-left: 8px;
    vertical-align: middle;
  }}
  /* grade path list */
  .path-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  .path-item {{
    font-size: 14px;
    padding-left: 20px;
    position: relative;
    line-height: 1.5;
  }}
  .path-item::before {{
    content: ">";
    position: absolute;
    left: 0;
    color: #5EB9E6;
    font-weight: 700;
  }}
  /* pipeline note */
  .pipeline-note {{
    background: #121E3C;
    color: #c0cfe0;
    border-radius: 8px;
    padding: 14px 18px;
    font-size: 13px;
    line-height: 1.6;
  }}
  /* alerts */
  .alert-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  .alert-item {{
    background: #fff;
    border-left: 3px solid #FF0000;
    border-radius: 0 8px 8px 0;
    padding: 10px 14px;
    font-size: 13px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }}
  .alert-ts {{
    font-size: 11px;
    color: #888;
  }}
  .alert-body {{
    color: #121E3C;
    word-break: break-word;
  }}
  .muted {{
    color: #888;
    font-size: 14px;
  }}
  .acct-breakdown {{
    font-size: 13px;
    color: #555;
    margin-top: 8px;
  }}
  /* footer */
  .footer {{
    margin-top: 40px;
    padding-top: 16px;
    border-top: 1px solid #e0ddd8;
    font-size: 11px;
    color: #aaa;
    text-align: center;
  }}
  @media (max-width: 480px) {{
    .grade-card {{ flex-direction: column; gap: 12px; }}
    .grade-chip {{ font-size: 40px; }}
    .stats-row {{ flex-direction: column; }}
  }}
</style>

<p class="eyebrow">ECHO STATUS</p>
<h1>Live System Status</h1>
<p class="gen-ts">Generated: {generated_at}</p>

<div class="grade-card">
  <div class="grade-chip">B+</div>
  <div class="grade-sub">One real client + 30-day month = A</div>
</div>

<div class="stats-row">
  <div class="stat-card">
    <div class="stat-label">Pending Approvals</div>
    <div class="stat-value">{pending}</div>
    {acct_breakdown}
  </div>
  <div class="stat-card">
    <div class="stat-label">Last Daily Run</div>
    <div class="stat-value" style="font-size:16px;padding-top:4px">{last_run}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Accounts Active</div>
    <div class="stat-value" style="font-size:14px;padding-top:6px">{accounts_active}</div>
  </div>
</div>

<div class="section">
  <div class="section-title">Blake: do these by hand</div>
  <ul class="todo-list">
    <li class="todo-item">
      Re-record Meta App Review screencast and resubmit
      <span class="deadline">Due 2026-07-18 PAST DUE</span>
    </li>
    <li class="todo-item">
      Run <code>python -m agent regen-library --set all</code> on Railway to generate 13 missing lasso_v2 cards
    </li>
    <li class="todo-item">
      Create Railway cron service (runbook at docs/SCHEDULER_CRON.md)
    </li>
    <li class="todo-item">
      Set <code>AGENT_CLIPPER_ENABLED=true</code> and <code>AGENT_CLIPPER_RENDER_ENABLED=true</code> in Railway
    </li>
    <li class="todo-item">
      Set <code>AGENT_EPISODE_INBOX_ENABLED=true</code> and <code>AGENT_TRANSCRIBE_API_KEY</code> in Railway
    </li>
    <li class="todo-item">
      Set <code>AGENT_PODCAST_ENABLED=true</code> and <code>AGENT_PODCAST_FEED_URL=&lt;RSS feed URL&gt;</code> in Railway
    </li>
    <li class="todo-item">
      Upload first Riverside episode export: <code>python -m agent episode-upload --file &lt;path&gt;</code>
    </li>
  </ul>
</div>

<div class="section">
  <div class="section-title">What moves the grade to A</div>
  <ul class="path-list">
    <li class="path-item">One real gym client completes a full 30-day posting month</li>
    <li class="path-item">Meta App Review cleared for client-owned assets</li>
    <li class="path-item">Railway cron confirmed running (not just in-process scheduler)</li>
  </ul>
</div>

<div class="section">
  <div class="section-title">Stage 2 pipeline status</div>
  <div class="pipeline-note">
    The full Stage 2 pipeline is built and dark behind feature flags. Clipper,
    episode inbox, podcast feed watcher, and GBP publisher are all code-complete
    and gated off. Each turns on via a single Railway env var when ready.
  </div>
</div>

<div class="section">
  <div class="section-title">Recent ops alerts</div>
  {alerts_html}
</div>

<div class="footer">Echo Live Status &nbsp;|&nbsp; Generated {generated_at} &nbsp;|&nbsp; LASSO Framework</div>
"""


def generate(data_dir=None):
    """Generate /data/handoff_live.html from live DB state. Returns the output path."""
    if data_dir is None:
        data_dir = os.environ.get("AGENT_DATA_DIR", "/data")

    out_path = os.path.join(data_dir, "handoff_live.html")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    stats = _fetch_stats()
    html = _html(stats, generated_at)

    os.makedirs(data_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    return out_path
