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
                 "status": "", "special": special}
        if rec["published"]:
            p = rec["published"][0]
            entry["status"] = "published"
            entry["concept"] = p.get("creative_key") or ""
            entry["caption"] = (p.get("caption") or "")[:80]
        else:
            by_status = {d.get("status"): d for d in reversed(rec["drafts"])}
            for status in ("approved", "pending"):
                if status in by_status:
                    d = by_status[status]
                    entry["status"] = status
                    entry["concept"] = os.path.basename(d.get("creative_path") or "")
                    entry["caption"] = (d.get("caption") or "")[:80]
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
