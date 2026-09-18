#!/usr/bin/env python3
"""Fill Pierce's two missing first-week feed slots and vary pending copy.

One-time, tenant-scoped repair. Run only on the Echo worker after PR #182 is
deployed. Uses the original 49-row backup for already hosted, approved media.
All rows remain pending for Bryan's review; no publisher is called.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "piercefitness"
BACKUP = Path("/data/pierce_calendar_reset_20260918.json")

# Every concrete fact below is in Pierce's approved client_sources on /data.
COPY = {
    ("2026-09-20", 0): ("testimonial", "Violet's goal was about everyday life. After three months at Pierce Fitness, she could walk up the stairs without pain and get down on the floor to play with her grandkids again. Those moments are worth training for."),
    ("2026-09-21", 0): ("testimonial", "Karen completed her first 5K at 56. Progress can look different for everyone. For Karen, it meant crossing a finish line. What goal would make you proud?"),
    ("2026-09-21", 1): ("service", "Semi Private Personal Training is one of the ways to train at Pierce Fitness. If you want coaching with a smaller group around you, ask us how this option works and whether it fits your goals."),
    ("2026-09-23", 0): ("testimonial", "David came in struggling with everyday back pain. Now he is deadlifting more than he thought possible without worrying about his back. His story is a reminder that progress is personal. It starts with where you are."),
    ("2026-09-23", 1): ("service", "Group Fitness Classes are one of the training options at Pierce Fitness. Some people want to train alongside others. Some prefer a smaller setting. Tell a coach what feels right for you and explore the options together."),
    ("2026-09-24", 0): ("testimonial", "Michelle lost 18 lbs and says she has more energy now than she did in her 40s. Her progress is hers. What would feeling stronger and more energized let you do in your own life?"),
    ("2026-09-24", 1): ("testimonial", "Aaron lost 10% body fat in less than six months at Pierce Fitness. That is his result, not a promise for anyone else. If you are ready to work toward your own goal, a coach can help you find a place to begin."),
    ("2026-09-25", 0): ("service", "Nutrition Coaching is available at Pierce Fitness alongside training. If you have questions about food and your fitness goals, bring them to a coach. You can explore what support fits your situation."),
    ("2026-09-25", 1): ("testimonial", "Amanda has trained twice a week for almost a year. Her story is about finding a rhythm she could keep. What would a routine that fits your life look like?"),
}
MISSING = (("2026-09-20", 0), ("2026-09-21", 1))


def _rows(store):
    return [r for r in store.list_month(BASE, "2026-09")
            if "2026-09-19" <= str(r.get("post_date", ""))[:10] <= "2026-09-25"]


def main():
    from agent.portal_calendar_store import SupabaseCalendarStore, _media_stage_belt
    from agent import media_guard

    if datetime.now(ZoneInfo("America/Toronto")).date().isoformat() > "2026-09-20":
        raise SystemExit("First week has begun publishing; manual re-audit required")
    if not BACKUP.exists():
        raise SystemExit("Original Pierce reset backup is missing")
    saved = json.loads(BACKUP.read_text())
    if saved.get("gym_id") != BASE or len(saved.get("rows", [])) != 49:
        raise SystemExit("Original backup tenant or row count mismatch")
    store = SupabaseCalendarStore()
    rows = _rows(store)
    if any(r.get("status") != "pending" for r in rows):
        raise SystemExit("A first-week row has been human-owned or published; stop")
    feeds = [r for r in rows if r.get("account") == "instagram"
             and r.get("format") == "feed"]
    mirrors = [r for r in rows if r.get("account") == "facebook"
               and r.get("format") == "feed"]
    stories = [r for r in rows if r.get("account") == "instagram"
               and r.get("format") == "story"]
    present = {(str(r["post_date"])[:10], r.get("slot_index")) for r in feeds}
    mirror_slots = {(str(r["post_date"])[:10], r.get("slot_index")) for r in mirrors}
    story_slots = {(str(r["post_date"])[:10], r.get("slot_index")) for r in stories}
    expected = {(f"2026-09-{d:02d}", slot) for d in range(19, 26)
                for slot in (0, 1)}
    if (expected - present != set(MISSING) or present != mirror_slots
            or story_slots != expected or len(feeds) != 12
            or len(mirrors) != 12 or len(stories) != 14):
        raise SystemExit("Unexpected live feed, mirror, or story slot shape")
    print("Preflight 12 paired feeds and 14 stories, missing Sep20 slot0 and Sep21 slot1 feeds")
    if "--dry-run" in sys.argv:
        return

    backup_feeds = [r for r in saved["rows"] if r.get("status") == "approved"
                    and r.get("account") == "instagram"
                    and r.get("format") == "feed" and r.get("image_url")]
    preferred = ["acb493eb-967c-4f4d-b193-8e113bc71344",
                 "091c823e-8ed3-4e6b-998f-fd803fadc5f6"]
    backup_feeds.sort(key=lambda r: (0 if r.get("id") in preferred else 1,
                                     preferred.index(r["id"]) if r.get("id") in preferred else 99))
    used_this_repair = set()
    for day, slot in MISSING:
        pillar, caption = COPY[(day, slot)]
        inserted = None
        for candidate in backup_feeds:
            key = media_guard.row_media_key(candidate)
            if not key or key in used_this_repair:
                continue
            pair = [{"gym_id": BASE, "account": account, "format": "feed",
                     "post_date": day, "slot_index": slot, "pillar": pillar,
                     "time_slot": "evening",
                     "caption": caption, "image_url": candidate["image_url"],
                     "source_media_url": candidate.get("source_media_url"),
                     "status": "pending"}
                    for account in ("instagram", "facebook")]
            if len(_media_stage_belt(store, BASE, pair, alert=lambda _: None)) != 2:
                continue
            result = store.insert_rows(BASE, pair)
            if len(result) != 2:
                raise SystemExit(f"Partial insert for {day} slot{slot}; inspect live rows")
            inserted = result
            used_this_repair.add(key)
            print(f"Filled {day} slot{slot} with backed-up approved media {candidate['id']}")
            break
        if inserted is None:
            raise SystemExit(f"No eligible distinct backed-up media for {day} slot{slot}")

    rows = _rows(store)
    for (day, slot), (pillar, caption) in COPY.items():
        for row in rows:
            if (str(row.get("post_date"))[:10], row.get("slot_index")) != (day, slot):
                continue
            if row.get("account") not in ("instagram", "facebook") or row.get("format") != "feed":
                continue
            if row.get("caption") == caption and row.get("pillar") == pillar:
                continue
            result = store.patch_pending_plan(BASE, row["id"], caption=caption,
                                              pillar=pillar)
            if result is None:
                raise SystemExit(f"Pending edit failed for {day} slot{slot}")
    rows = _rows(store)
    feeds = [r for r in rows if r.get("account") == "instagram"
             and r.get("format") == "feed"]
    mirror = [r for r in rows if r.get("account") == "facebook"
              and r.get("format") == "feed"]
    if len(feeds) != 14 or len(mirror) != 14:
        raise SystemExit(f"Final feed shape {len(feeds)} IG and {len(mirror)} FB")
    if len({r.get("caption") for r in feeds}) != 14:
        raise SystemExit("Feed captions are not distinct")
    print("Verified 14 IG and 14 FB pending feeds, 14 distinct IG captions")


if __name__ == "__main__":
    main()
