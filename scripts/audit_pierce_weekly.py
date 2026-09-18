#!/usr/bin/env python3
"""Read-only audit of the Pierce Fitness Saturday-Friday weekly calendar block.

Queries the portal calendar store ONLY through ``list_month`` (never a
mutation), restricted to the ``piercefitness`` tenant and the seven selected
dates. Verifies the weekly shape Bryan reviews:

  * exactly two Instagram feed slots per day (slot_index 0 and 1),
  * each Instagram feed paired with a Facebook feed mirror on the same slot
    (same normalized caption, same media key),
  * exactly two Instagram stories per day (slot_index 0 and 1),
  * only pending/approved rows satisfy slots; denied, killed, deleted,
    failed, or candidate rows raise a finding and leave the slot open,
  * fourteen distinct nonblank normalized Instagram feed captions across the
    week and fourteen distinct feed media keys,
  * observed Google Business update counts (reported, not enforced).

Reports findings as codes plus caption/media FINGERPRINTS (sha256 prefixes)
and status counts only -- raw captions and secrets are never printed.

Usage:
    python3 scripts/audit_pierce_weekly.py --start 2026-09-19 [--json]

Exit codes: 0 = structurally complete week, 1 = gaps found,
2 = usage error or no readable store.
"""
import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "piercefitness"
DAYS = 7
FEED_FORMATS = ("feed", "reel")
STORY_FORMAT = "story"
GBP_ACCOUNT = "googlebusiness"
EXPECTED_SLOTS = (0, 1)
READY_STATUSES = frozenset({"pending", "approved"})
# Seeded onboarding samples are not real placed content (mirrors
# agent.onboarding_demo.is_sample_row / _existing_feed_count's exclusion).
_SAMPLE_PILLAR = "sample"
_SAMPLE_PREFIX = "SAMPLE: "


def week_dates(start, days=DAYS):
    return [start + timedelta(days=i) for i in range(days)]


def week_months(dates):
    return sorted({d.isoformat()[:7] for d in dates})


def _day_of(row):
    return str(row.get("post_date") or "")[:10]


def _norm_caption(row):
    return " ".join(str(row.get("caption") or "").split())


def _media_key(row):
    """Same contract as agent.media_guard.row_media_key: the raw source when
    present, else image_url, basename with any query string stripped."""
    url = row.get("source_media_url") or row.get("image_url")
    return str(url or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _status(row):
    return str(row.get("status") or "").lower()


def _is_ready(row):
    """Only pending/approved rows count as ready placed content. Denied,
    killed, deleted, failed, or candidate rows (and any unknown status)
    must not satisfy a weekly slot."""
    return _status(row) in READY_STATUSES


def _is_sample(row):
    if str(row.get("pillar") or "").strip().lower() == _SAMPLE_PILLAR:
        return True
    return str(row.get("caption") or "").lstrip().startswith(_SAMPLE_PREFIX)


def _category(row):
    account = str(row.get("account") or "").lower()
    fmt = str(row.get("format") or "").lower()
    if account == GBP_ACCOUNT:
        return "gbp"
    if account in ("facebook", "fb"):
        return "fb_feed" if fmt in FEED_FORMATS else "other"
    if account in ("instagram", "ig", ""):
        if fmt in FEED_FORMATS:
            return "ig_feed"
        if fmt == STORY_FORMAT:
            return "ig_story"
    return "other"


def _fetch_week_rows(store, base, dates):
    """Read the week's rows through list_month ONLY, scoped to the tenant and
    the exact seven dates. Any row naming another gym_id is dropped even if a
    broken store returned it."""
    wanted = {d.isoformat() for d in dates}
    rows = []
    for month in week_months(dates):
        for row in store.list_month(base, month) or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("gym_id") or base) != base:
                continue
            if _day_of(row) not in wanted:
                continue
            rows.append(row)
    return rows


def audit_week(store, start, *, base=BASE, days=DAYS):
    """Audit the seven-day block beginning at ``start``. Read-only against the
    store. Returns a JSON-serializable report; ``ok`` is True only when no
    structural finding was raised."""
    dates = week_dates(start, days)
    rows = _fetch_week_rows(store, base, dates)
    live = [r for r in rows if not _is_sample(r)]
    samples = len(rows) - len(live)

    ig_feeds, fb_feeds, stories, gbp = {}, {}, {}, {}
    non_ready = []
    for row in live:
        day = _day_of(row)
        cat = _category(row)
        if cat == "gbp":
            gbp.setdefault(day, []).append(row)  # reported, not enforced
            continue
        if cat not in ("ig_feed", "fb_feed", "ig_story"):
            continue
        if not _is_ready(row):
            non_ready.append((cat, day, row))
            continue
        target = {"ig_feed": ig_feeds, "fb_feed": fb_feeds,
                  "ig_story": stories}[cat]
        target.setdefault(day, {}).setdefault(
            row.get("slot_index"), []).append(row)

    findings = []

    def finding(code, day, **extra):
        findings.append({"code": code, "date": day, **extra})

    for cat, day, row in non_ready:
        extra = {"status": _status(row)}
        if cat in ("ig_feed", "fb_feed"):
            extra["slot"] = row.get("slot_index")
        finding(f"non_ready_{cat}", day, **extra)

    captions, media_keys = {}, {}
    for day in [d.isoformat() for d in dates]:
        day_ig = ig_feeds.get(day, {})
        day_fb = fb_feeds.get(day, {})
        for slot in EXPECTED_SLOTS:
            ig_slot = day_ig.get(slot, [])
            fb_slot = day_fb.get(slot, [])
            if not ig_slot:
                finding("missing_ig_feed", day, slot=slot)
            elif len(ig_slot) > 1:
                finding("duplicate_ig_feed_slot", day, slot=slot,
                        count=len(ig_slot))
            if not fb_slot:
                finding("missing_fb_feed_mirror", day, slot=slot)
            elif len(fb_slot) > 1:
                finding("duplicate_fb_feed_slot", day, slot=slot,
                        count=len(fb_slot))
            if len(ig_slot) == 1 and len(fb_slot) == 1:
                ig_cap, fb_cap = _norm_caption(ig_slot[0]), _norm_caption(fb_slot[0])
                if ig_cap != fb_cap:
                    finding("unpaired_feed_caption", day, slot=slot,
                            ig_fingerprint=_fingerprint(ig_cap),
                            fb_fingerprint=_fingerprint(fb_cap))
                ig_key, fb_key = _media_key(ig_slot[0]), _media_key(fb_slot[0])
                if ig_key != fb_key:
                    finding("unpaired_feed_media", day, slot=slot,
                            ig_media_fingerprint=_fingerprint(ig_key),
                            fb_media_fingerprint=_fingerprint(fb_key))
            if len(ig_slot) == 1:
                cap = _norm_caption(ig_slot[0])
                key = _media_key(ig_slot[0])
                if not cap:
                    finding("blank_ig_caption", day, slot=slot)
                if not key:
                    finding("blank_ig_media", day, slot=slot)
                captions.setdefault(cap, []).append((day, slot))
                media_keys.setdefault(key, []).append((day, slot))
        for label, day_slots in (("ig", day_ig), ("fb", day_fb)):
            extra_slots = sorted(
                (s for s in day_slots if s not in EXPECTED_SLOTS), key=str)
            if extra_slots:
                finding(f"unexpected_{label}_feed_slots", day,
                        slots=extra_slots)
        day_stories = stories.get(day, {})
        for slot in EXPECTED_SLOTS:
            st_slot = day_stories.get(slot, [])
            if not st_slot:
                finding("missing_ig_story", day, slot=slot)
            elif len(st_slot) > 1:
                finding("duplicate_ig_story_slot", day, slot=slot,
                        count=len(st_slot))
        extra_story_slots = sorted(
            (s for s in day_stories if s not in EXPECTED_SLOTS), key=str)
        if extra_story_slots:
            finding("unexpected_ig_story_slots", day, slots=extra_story_slots)

    for cap, slots in captions.items():
        if cap and len(slots) > 1:
            findings.append({"code": "duplicate_ig_caption", "date": None,
                             "fingerprint": _fingerprint(cap),
                             "slots": [[d, s] for d, s in slots]})
    for key, slots in media_keys.items():
        if key and len(slots) > 1:
            findings.append({"code": "duplicate_feed_media", "date": None,
                             "media_fingerprint": _fingerprint(key),
                             "slots": [[d, s] for d, s in slots]})

    status_counts = Counter(_status(r) for r in live)
    gbp_formats = Counter(str(r.get("format") or "").lower()
                          for day_rows in gbp.values() for r in day_rows)
    per_day = {}
    for d in dates:
        day = d.isoformat()
        per_day[day] = {
            "ig_feed_slots": sorted(s for s in ig_feeds.get(day, {})
                                    if isinstance(s, int)),
            "fb_feed_slots": sorted(s for s in fb_feeds.get(day, {})
                                    if isinstance(s, int)),
            "ig_story_slots": sorted(s for s in stories.get(day, {})
                                     if isinstance(s, int)),
            "ig_stories": sum(len(v) for v in stories.get(day, {}).values()),
            "gbp_updates": len(gbp.get(day, [])),
        }

    return {
        "base": base,
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "days": days,
        "months_read": week_months(dates),
        "ok": not findings,
        "counts": {
            "rows": len(live),
            "sample_rows_excluded": samples,
            "ig_feeds": sum(len(v) for slots in ig_feeds.values()
                            for v in slots.values()),
            "fb_feeds": sum(len(v) for slots in fb_feeds.values()
                            for v in slots.values()),
            "ig_stories": sum(len(v) for slots in stories.values()
                              for v in slots.values()),
            "non_ready_rows": len(non_ready),
            "gbp_updates": sum(len(v) for v in gbp.values()),
            "distinct_ig_captions": len([c for c in captions if c]),
            "distinct_feed_media_keys": len([k for k in media_keys if k]),
        },
        "status_counts": dict(sorted(status_counts.items())),
        "gbp_format_counts": dict(sorted(gbp_formats.items())),
        "per_day": per_day,
        "findings": findings,
    }


def _default_store():
    from agent.client_media_sync import _default_store as build
    return build()


def _print_human(report):
    r = report
    print(f"Pierce weekly audit: {r['start']}..{r['end']} "
          f"(months read: {', '.join(r['months_read'])})")
    c = r["counts"]
    print(f"Rows: {c['rows']} live ({c['sample_rows_excluded']} sample excluded); "
          f"IG feeds {c['ig_feeds']}/14, FB mirrors {c['fb_feeds']}/14, "
          f"IG stories {c['ig_stories']}/14, GBP updates {c['gbp_updates']}")
    if c["non_ready_rows"]:
        print(f"Non-ready rows excluded from slots: {c['non_ready_rows']}")
    print(f"Distinct IG captions: {c['distinct_ig_captions']}; "
          f"distinct feed media keys: {c['distinct_feed_media_keys']}")
    print(f"Status counts: {r['status_counts'] or '{}'}")
    if r["gbp_format_counts"]:
        print(f"GBP formats: {r['gbp_format_counts']}")
    for day, shape in r["per_day"].items():
        print(f"  {day}: ig_slots={shape['ig_feed_slots']} "
              f"fb_slots={shape['fb_feed_slots']} "
              f"story_slots={shape['ig_story_slots']} "
              f"gbp={shape['gbp_updates']}")
    if r["findings"]:
        print(f"FINDINGS ({len(r['findings'])}):")
        for f in r["findings"]:
            day = f.get("date") or "week"
            slot = f.get("slot")
            where = f"{day}" + (f" slot{slot}" if slot is not None else "")
            detail = {k: v for k, v in f.items()
                      if k not in ("code", "date", "slot")}
            print(f"  {f['code']} @ {where} {detail if detail else ''}")
    print("RESULT: " + ("PASS" if r["ok"] else "GAPS FOUND"))


def main(argv=None, store=None):
    parser = argparse.ArgumentParser(
        description="Read-only audit of the Pierce weekly calendar block.")
    parser.add_argument("--start", required=True,
                        help="First day of the block, a Saturday, YYYY-MM-DD")
    parser.add_argument("--json", action="store_true",
                        help="Emit the full report as JSON for later review")
    args = parser.parse_args(argv)
    try:
        start = date.fromisoformat(args.start)
    except ValueError:
        print(f"Invalid --start date: {args.start!r}", file=sys.stderr)
        return 2
    if start.weekday() != 5:
        print(f"--start {start} is not a Saturday; the Pierce weekly block "
              f"runs Saturday through Friday", file=sys.stderr)
        return 2
    if store is None:
        store = _default_store()
    if store is None or not callable(getattr(store, "list_month", None)):
        print("No readable calendar store (portal Supabase not armed)",
              file=sys.stderr)
        return 2
    report = audit_week(store, start)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
