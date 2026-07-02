"""
Social Grade client report card (the D+ moment on live data).

    /opt/venv/bin/python -m agent grade-card [--account key]

Renders the computed Social Grade (the existing engine in agent/reporting.py:
letter A to F, subscores, honest gaps) as a one-page V3-branded HTML + PDF
under /data/reports/: the grade, the six area rubric (consistency, mix,
engagement, growth, verified proof, and the before/after posting frequency
compare), all from live store data. Respects AGENT_GRADE_ENABLED: flag OFF
means nothing is computed and nothing is rendered. Drafts nothing, posts
nothing, publishes nothing.
"""

import os
from datetime import datetime, timezone

from . import config, monthly_report, reporting, rotation
from .accounts import active_accounts
from .pdf_report import brand_for, build_pdf


def _grade_inputs(account_key, now=None):
    """The compute_grade inputs from the live store (honest: missing = None)."""
    snaps, posts = monthly_report.gather(account_key, now=now)
    r = monthly_report.assemble(account_key, snaps, posts)
    published = [p for p in posts if p.get("mode") == "published"]
    pillar_counts = {}
    for p in published:
        pillar = rotation.pillar_of(p.get("creative_key") or "")
        pillar_counts[pillar] = pillar_counts.get(pillar, 0) + 1
    posting_days = 30 - (30 // 7) * len(config.POSTING_SKIP_DAYS)
    report_for_grade = {
        "account_key": account_key,
        "engagement_rate": r["engagement_rate"],
        "engagement_rate_baseline": None,           # gap until two cycles exist
        "followers_growth_rate": r["follower_rate"],
        "posting_freq_current": r["posts_published"],
    }
    return report_for_grade, posting_days, (pillar_counts or None), r


def _fmt(v):
    return "no data yet" if v is None else str(v)


def render_card(account, grade, extras, month, out_dir):
    """One page HTML + PDF. Returns (html_path, pdf_path)."""
    letter = grade.get("letter") or "no grade yet"
    subs = grade.get("subscores", {})
    rubric = [
        ("Consistency (published vs planned)", _fmt(subs.get("consistency"))),
        ("Content mix balance", _fmt(subs.get("mix"))),
        ("Engagement trend", _fmt(subs.get("engagement"))),
        ("Growth trend", _fmt(subs.get("growth"))),
        ("Verified proof usage", _fmt(subs.get("proof"))),
        ("Posts per week, before Echo vs now",
         f"{_fmt(grade.get('posting_freq_before'))} before, "
         f"{_fmt(grade.get('posting_freq_after'))} now"),
    ]
    gaps = grade.get("gaps", [])

    rows = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rubric)
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Social Grade: {account.key}</title></head>
<body style="font-family:Helvetica,Arial,sans-serif;background:#FAF6F0;color:#121E3C;padding:32px">
<h1 style="color:#121E3C">{brand_for(account)['name']}: Social Grade</h1>
<div style="font-size:96px;font-weight:bold;color:#FF0000">{letter}</div>
<p style="color:#5EB9E6;font-size:18px">Overall score: {_fmt(grade.get('score'))}</p>
<table cellpadding="8" style="background:#FFFFFF;border-radius:8px">{rows}</table>
<p>Gaps (never guessed, always named): {", ".join(gaps) or "none"}</p>
<p style="color:#5EB9E6">Every number above is read from live account data. A
missing input lowers nothing and fakes nothing; it is listed as a gap.</p>
</body></html>"""

    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, f"{account.key}_grade_{month}.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    sections = [
        ("heading", f"Grade: {letter} (score {_fmt(grade.get('score'))})"),
        ("heading", "The six areas"), ("table", rubric),
        ("para", "Gaps (never guessed, always named): " + (", ".join(gaps) or "none")),
        ("para", "Every number is read from live account data. A missing input "
                 "lowers nothing and fakes nothing; it is listed as a gap."),
    ]
    pdf_path = os.path.join(out_dir, f"{account.key}_grade_{month}.pdf")
    build_pdf(pdf_path, "Social Grade", account.key, sections,
              brand=brand_for(account))
    return html_path, pdf_path


def run(account=None, now=None, base_dir=None):
    """Build the grade card per account. None while AGENT_GRADE_ENABLED is OFF."""
    if not config.grade_enabled():
        print("grade-card: AGENT_GRADE_ENABLED is OFF. Nothing rendered.")
        return None
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    out_dir = os.environ.get("AGENT_REPORTS_DIR", "/data/reports")
    out = {}
    for acct in [a for a in active_accounts() if account in (None, a.key)]:
        report_for_grade, planned, pillar_counts, _r = _grade_inputs(acct.key, now=now)
        grade = reporting.compute_grade(report_for_grade, planned_posts=planned,
                                        pillar_counts=pillar_counts,
                                        proof_posts=None, baseline_month=month,
                                        base_dir=base_dir)
        html_path, pdf_path = render_card(acct, grade or {}, {}, month, out_dir)
        out[acct.key] = html_path
        out[acct.key + ":pdf"] = pdf_path
        print(f"grade card for {acct.key}: "
              f"{(grade or {}).get('letter') or 'no grade yet'} -> {pdf_path}")
    return out
