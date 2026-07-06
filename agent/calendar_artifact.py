"""
Month calendar artifact (Part A: the assembler; Part B: calendar-html).

    python -m agent calendar-html --account <key> --month YYYY-MM [--upload]

PART A, assemble_month: a READ ONLY 30 day plan per account from EXISTING
state only: the posts table (published history), the drafts table (the same
rows the Slack cards read, so calendar and Slack can never disagree), the
seed-calendar approved key list, the posting schedule (skip days), and the
podcast / book special days as evidenced by each day's draft_type. Per day:
date, concept key, caption draft, canvas, layout, status (published /
approved / pending / draft / rest), and a special tag when one applies.
NO FABRICATION: a posting day with nothing planned emits status "draft" with
an empty concept, never an invented one.

PART B, the HTML artifact: a self contained navy grid calendar (approved
template: #1A2340 grid, per day concept, canvas + layout chips, status
colors, specials pinned, month stat rollup). --upload posts it to R2 under
echo/calendars/<account>_<month>.html and prints the public URL; otherwise
writes only the local file. The approve / edit / kill buttons are DISPLAY
ONLY previews (clearly labeled): the tap still happens in Slack until Stage 3
wires write back. Visible copy is dash free.
"""

import html as _html
import json
import os
from calendar import monthrange
from datetime import date as _date

from . import config, db, schedule

STATUS_ORDER = ("published", "approved", "pending")
CAL_PREFIX = "echo/calendars"

# status -> the artifact's chip color (navy grid template)
STATUS_COLORS = {
    "published": "#3AA76D",
    "approved": "#5EB9E6",
    "pending": "#E0A800",
    "draft": "#8A93A6",
    "rest": "#2A3452",
}


def _variant_of(concept_key):
    """(canvas, layout) for a library concept key like lasso_v2_<name>.png,
    resolved through the SAME assignment the renders use; ('', '') when the
    key is not a library concept (nothing guessed)."""
    stem = os.path.splitext(concept_key or "")[0]
    if stem.startswith("lasso_v2_"):
        name = stem[len("lasso_v2_"):]
        from .regen_library import CONCEPTS, variant_for
        if name in CONCEPTS:
            canvas, layout = variant_for(name)
            return canvas or "cream", layout or "poster"
    return "", ""


def _public_url_for(concept_key):
    """public_url from the concept sidecar JSON; '' when unavailable."""
    if not concept_key:
        return ""
    stem = os.path.splitext(concept_key)[0]
    sidecar = os.path.join(config.LIBRARY_PATH, stem + ".json")
    if not os.path.isfile(sidecar):
        return ""
    try:
        with open(sidecar, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("public_url", "") or ""
    except Exception:
        return ""


def _day_records(account_key, month):
    """{day: {"published": [...], "drafts": [...]}} straight from the same
    tables the Slack cards read."""
    out = {}
    with db.connect() as conn:
        for r in conn.execute(
                "SELECT published_at, creative_key, caption, mode FROM posts "
                "WHERE account_key=? AND substr(published_at, 1, 7)=?",
                (account_key, month)).fetchall():
            day = r["published_at"][:10]
            out.setdefault(day, {"published": [], "drafts": []})
            out[day]["published"].append(dict(r))
        for r in conn.execute(
                "SELECT day_key, status, draft_type, data FROM drafts "
                "WHERE account_key=? AND substr(day_key, 1, 7)=?",
                (account_key, month)).fetchall():
            try:
                rec = json.loads(r["data"] or "{}")
            except Exception:
                rec = {}
            rec.update({"status": r["status"], "draft_type": r["draft_type"]})
            out.setdefault(r["day_key"], {"published": [], "drafts": []})
            out[r["day_key"]]["drafts"].append(rec)
    return out


def _special_for(day, drafts):
    """The day's special tag: evidence first (the day's draft_type), then the
    schedule expectation (Monday is the podcast release day while the podcast
    flag is armed). Never a concept, only a tag."""
    for d in drafts:
        if d.get("draft_type") == "book":
            return "book campaign"
        if d.get("draft_type") == "podcast":
            return "podcast"
    weekday = _date.fromisoformat(day).weekday()
    if weekday == 0 and config.podcast_enabled():
        return "podcast release day"
    return ""


def assemble_month(account_key, month):
    """
    The 30 day plan: {"days": [...], "rollup": {...}, "seeded_keys": [...]}.
    READ ONLY; nothing is written anywhere.
    """
    year, mon = int(month[:4]), int(month[5:7])
    records = _day_records(account_key, month)
    try:
        seeded = json.loads(
            db.kv_get(f"approved_calendar_{account_key}_{month}", "") or "[]")
    except Exception:
        seeded = []
    days = []
    rollup = {"published": 0, "approved": 0, "pending": 0, "draft": 0,
              "rest": 0, "specials": 0}
    for n in range(1, monthrange(year, mon)[1] + 1):
        day = f"{month}-{n:02d}"
        rec = records.get(day, {"published": [], "drafts": []})
        special = _special_for(day, rec["drafts"])
        entry = {"date": day, "weekday": schedule.weekday_abbr(day),
                 "concept": "", "caption": "", "canvas": "", "layout": "",
                 "status": "", "special": special,
                 "public_url": "", "hashtags": "", "source": ""}
        if rec["published"]:
            p = rec["published"][0]
            entry["status"] = "published"
            entry["concept"] = p.get("creative_key") or ""
            entry["caption"] = p.get("caption") or ""
            entry["public_url"] = _public_url_for(entry["concept"])
        else:
            by_status = {d.get("status"): d for d in reversed(rec["drafts"])}
            for status in ("approved", "pending"):
                if status in by_status:
                    d = by_status[status]
                    entry["status"] = status
                    entry["concept"] = os.path.basename(d.get("creative_path") or "")
                    entry["caption"] = d.get("caption") or ""
                    entry["hashtags"] = " ".join(d.get("hashtags") or [])
                    entry["source"] = " ".join(d.get("source_fragments") or [])
                    entry["public_url"] = (d.get("creative_public_url") or
                                           _public_url_for(entry["concept"]))
                    break
        if not entry["status"]:
            # nothing planned: an open slot on a posting day, rest otherwise.
            # NOTHING is invented; the concept stays empty.
            entry["status"] = "draft" if schedule.should_post_on(day) else "rest"
        entry["canvas"], entry["layout"] = _variant_of(entry["concept"])
        rollup[entry["status"]] += 1
        if special:
            rollup["specials"] += 1
        days.append(entry)
    return {"account_key": account_key, "month": month, "days": days,
            "rollup": rollup, "seeded_keys": sorted(set(seeded))}
# ---- Part B: the HTML artifact ---------------------------------------------------------
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _month_title(month):
    return f"{_MONTHS[int(month[5:7]) - 1]} {month[:4]}"


def render_html(plan):
    """The self contained navy grid calendar. Visible copy dash free (dates
    render as day numbers; the month renders as words)."""
    e = _html.escape
    days = plan["days"]
    cells = {d["date"]: d for d in days}
    year, mon = int(plan["month"][:4]), int(plan["month"][5:7])
    first_weekday, n_days = monthrange(year, mon)  # Monday = 0
    rollup = plan["rollup"]

    def cell_html(d):
        color = STATUS_COLORS[d["status"]]
        bits = []
        # Visual: rendered image, pending placeholder, or nothing for empty days
        if d.get("public_url"):
            bits.append(
                "<div style=\"height:140px;overflow:hidden;margin-bottom:6px\">"
                f"<img src=\"{_html.escape(d['public_url'], quote=True)}\" "
                f"alt=\"{e(d['concept'])}\" "
                "style=\"width:100%;height:100%;object-fit:cover\"></div>")
        elif d.get("concept"):
            bits.append(
                "<div style=\"background:#2A3452;padding:6px;margin-bottom:4px;"
                "text-align:center;font-size:10px;color:#8A93A6\">image pending</div>"
                f"<div style=\"font-size:10px;color:#8A93A6;word-break:break-all;"
                f"margin-bottom:4px\">{e(d['concept'])}</div>")
        # Day number + special pin
        bits.append(f"<div style=\"font-weight:bold;margin:4px 0\">"
                    f"{int(d['date'][8:10])}</div>")
        if d["special"]:
            bits.append(f"<div style=\"color:#E03131;font-size:11px\">"
                        f"PINNED: {e(d['special'].upper())}</div>")
        # Full caption and hashtags exactly as the post will appear
        if d.get("caption"):
            bits.append(f"<div style=\"font-size:11px;color:#B9C2D8;margin:4px 0;"
                        f"white-space:pre-wrap\">{e(d['caption'])}</div>")
        if d.get("hashtags"):
            bits.append(f"<div style=\"font-size:10px;color:#5EB9E6;"
                        f"margin-bottom:4px\">{e(d['hashtags'])}</div>")
        # Open slot label for unplanned posting days
        if not d.get("concept") and d["status"] == "draft":
            bits.append("<div style=\"font-size:12px;color:#8A93A6\">open slot</div>")
        # Canvas / layout chips
        if d["canvas"]:
            bits.append(
                "<div style=\"font-size:10px;margin:4px 0\">"
                f"<span style=\"background:#2A3452;padding:1px 6px;"
                f"border-radius:8px\">{e(d['canvas'])}</span> "
                f"<span style=\"background:#2A3452;padding:1px 6px;"
                f"border-radius:8px\">{e(d['layout'])}</span></div>")
        bits.append(f"<div style=\"font-size:10px;color:{color}\">"
                    f"{e(d['status'].upper())}</div>")
        return (f"<td style=\"vertical-align:top;padding:8px;width:14%;"
                f"border:1px solid #2A3452;border-top:4px solid {color}\">"
                + "".join(bits) + "</td>")

    weeks, week = [], ["<td></td>"] * first_weekday
    for n in range(1, n_days + 1):
        week.append(cell_html(cells[f"{plan['month']}-{n:02d}"]))
        if len(week) == 7:
            weeks.append("<tr>" + "".join(week) + "</tr>")
            week = []
    if week:
        weeks.append("<tr>" + "".join(week + ["<td></td>"] * (7 - len(week)))
                     + "</tr>")

    header = "".join(f"<th style=\"padding:6px;color:#B9C2D8\">{w}</th>"
                     for w in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"))
    stats = (f"published {rollup['published']} / approved {rollup['approved']} "
             f"/ pending {rollup['pending']} / open {rollup['draft']} "
             f"/ rest {rollup['rest']} / specials {rollup['specials']}")
    buttons = (
        "<p style=\"color:#8A93A6\">PREVIEW ONLY: "
        "<button disabled>Approve</button> <button disabled>Edit</button> "
        "<button disabled>Kill</button> "
        "these buttons are display previews; the tap still happens in Slack "
        "until Stage 3 wires write back.</p>")
    return (
        "<html><head><title>"
        f"LASSO calendar: {e(plan['account_key'])} {e(_month_title(plan['month']))}"
        "</title></head>"
        "<body style=\"background:#1A2340;color:#FFFFFF;"
        "font-family:Helvetica,Arial,sans-serif;padding:24px\">"
        f"<h1>{e(plan['account_key'])}: {e(_month_title(plan['month']))}</h1>"
        f"<p>Month rollup: {stats}</p>"
        f"{buttons}"
        f"<table style=\"border-collapse:collapse;width:100%\">"
        f"<tr>{header}</tr>{''.join(weeks)}</table>"
        "<p style=\"color:#8A93A6\">Statuses read from the same store the "
        "Slack cards read; an open slot is an honest gap, never an invented "
        "post.</p></body></html>")


def cal_key(account_key, month):
    return f"{CAL_PREFIX}/{account_key}_{month}.html"


def run(account_key, month, upload=False, out_path=None, s3_client=None):
    """Assemble, render, write the local file, optionally upload. Read only
    against state; the only writes are the HTML file and the upload."""
    from .accounts import get_account
    if get_account(account_key) is None:
        print(f"calendar-html: unknown account {account_key!r}")
        return None
    import re
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        print(f"calendar-html: --month must be YYYY-MM, got {month!r}")
        return None
    plan = assemble_month(account_key, month)
    text = render_html(plan)
    out_path = out_path or os.path.join(
        config.LIBRARY_PATH, f"calendar_{account_key}_{month}.html")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    url = ""
    if upload:
        client = s3_client
        if client is None and config.hosting_enabled():
            from . import media_host
            client = media_host._default_client()
        if client is None:
            print("calendar-html: hosting is dark (flag or credentials); "
                  "local only.")
        else:
            try:
                client.put(cal_key(account_key, month), out_path)
                url = (f"{config.S3_PUBLIC_BASE_URL.rstrip('/')}/"
                       f"{cal_key(account_key, month)}")
            except Exception as ex:
                print(f"calendar-html: upload failed ({type(ex).__name__}: "
                      f"{ex}); the local file below still works.")
    print(f"calendar-html: {plan['rollup']} on {out_path}")
    if url:
        print(f"calendar-html: {url}")
    return {"plan": plan, "path": out_path, "url": url,
            "key": cal_key(account_key, month)}


def cli(args):
    account, month, upload, out_path = None, None, False, None
    i = 0
    while i < len(args):
        if args[i] == "--account" and i + 1 < len(args):
            account = args[i + 1]; i += 2; continue
        if args[i] == "--month" and i + 1 < len(args):
            month = args[i + 1]; i += 2; continue
        if args[i] == "--upload":
            upload = True; i += 1; continue
        if args[i] == "--out" and i + 1 < len(args):
            out_path = args[i + 1]; i += 2; continue
        print(f"unrecognized argument: {args[i]}\n"
              "usage: python -m agent calendar-html --account <key> "
              "--month YYYY-MM [--upload] [--out PATH]")
        return
    if not account or not month:
        print("usage: python -m agent calendar-html --account <key> "
              "--month YYYY-MM [--upload] [--out PATH]")
        return
    run(account, month, upload=upload, out_path=out_path)
