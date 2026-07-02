"""
Client welcome kit generator (onboarding polish). RUN BY HAND:

    /opt/venv/bin/python -m agent welcome-kit --account <key>

One V3-branded page (HTML + PDF, /data/reports/) that tells a new client how
working with Echo feels: how approval works, how to text creative in (once
their intake link is live), what the monthly report covers, and the trust rules
in plain language. Copy is FIXED TEMPLATE LANGUAGE only (plus the client's
display name); nothing is drafted from unapproved material, NO PRICING (the
pricing wording is unconfirmed), and no dash characters anywhere.
"""

import os

from .accounts import active_accounts, get_account
from .pdf_report import brand_for, build_pdf

SECTIONS = [
    ("heading", "How posting works"),
    ("para", "Every post is drafted for you and waits for a human approval tap "
             "before anything publishes. No post ever goes out on its own. You "
             "see the exact image and caption in Slack, and one tap approves, "
             "edits, or skips it."),
    ("heading", "How to send us creative"),
    ("para", "When your private upload link is live, you text photos and short "
             "clips straight from your phone with one line about what is "
             "happening in them. That one line becomes the caption's raw "
             "material. Real moments beat polished stock every time."),
    ("heading", "What the monthly report covers"),
    ("list", ["Views, reach, likes, comments, saves, and shares",
              "Follower growth, net and rate",
              "Posting frequency before and after Echo",
              "Your top and bottom posts",
              "What we will refresh next cycle and what we need from you"]),
    ("heading", "The trust rules, in plain language"),
    ("list", ["Every post is approved by a human before it publishes.",
              "We never invent claims, numbers, or member stories. If we cannot "
              "verify it from your own material, it does not ship.",
              "Photos of people only run with recorded permission.",
              "Your voice stays yours: the brand guide we build together is the "
              "only source we draft from."]),
]


def run(account_key, out_dir=None):
    """Render the kit for one account. Returns (html_path, pdf_path) or None."""
    acct = get_account(account_key)
    if acct is None:
        known = ", ".join(a.key for a in active_accounts())
        print(f"welcome-kit: unknown account {account_key!r} (known: {known})")
        return None
    out_dir = out_dir or os.environ.get("AGENT_REPORTS_DIR", "/data/reports")
    os.makedirs(out_dir, exist_ok=True)
    brand = brand_for(acct)

    body_html = ""
    for kind, payload in SECTIONS:
        if kind == "heading":
            body_html += f'<h2 style="color:#121E3C">{payload}</h2>'
        elif kind == "para":
            body_html += f'<p style="color:#121E3C">{payload}</p>'
        elif kind == "list":
            items = "".join(f"<li>{x}</li>" for x in payload)
            body_html += f'<ul style="color:#121E3C">{items}</ul>'
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Welcome: {brand['name']}</title></head>
<body style="font-family:Helvetica,Arial,sans-serif;background:#FAF6F0;padding:32px">
<h1 style="color:#121E3C">Welcome, {brand['name']}</h1>
<p style="color:#5EB9E6;font-size:16px">How your done for you social works</p>
{body_html}
</body></html>"""

    html_path = os.path.join(out_dir, f"{acct.key}_welcome.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    pdf_path = os.path.join(out_dir, f"{acct.key}_welcome.pdf")
    build_pdf(pdf_path, "Welcome", "How your done for you social works",
              SECTIONS, brand=brand)
    print(f"welcome kit for {acct.key}: {pdf_path}")
    return html_path, pdf_path
