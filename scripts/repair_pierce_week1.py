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

# Service names, adult audience, and consultation steps are in Pierce's approved
# client_sources on /data. Copy does not imply a person pictured is a member.
COPY = {
    ("2026-09-20", 0): ("service", "What would you like daily movement to feel like six months from now? Personal Training at Pierce Fitness gives you a way to work on your own goals with a coach. Tell us what you want to make easier in everyday life."),
    ("2026-09-21", 0): ("service", "A first fitness goal does not have to be a number on a scale. It might be completing a walk, lifting with confidence, or keeping a routine. Bring your goal to a free consultation and talk through where to begin."),
    ("2026-09-21", 1): ("service", "Want coaching with a smaller group around you? Semi Private Personal Training is one of the options at Pierce Fitness. Ask a coach how it works and decide whether it fits the way you like to train."),
    ("2026-09-22", 1): ("service", "Starting again after a long break can feel like a big step. You can begin by telling a coach what matters to you now. Pierce Fitness offers Personal Training, small group options, and classes, so you can explore a starting point that feels right."),
    ("2026-09-23", 0): ("service", "Strength has a different purpose for everyone. Maybe yours is carrying groceries more easily or feeling steadier in daily life. Tell a Pierce Fitness coach what you want to work toward and ask which training option fits."),
    ("2026-09-23", 1): ("service", "Do you enjoy the energy of a room full of people? Group Fitness Classes are an option at Pierce Fitness. If you are deciding between a class and a smaller coaching setting, ask us to walk you through both."),
    ("2026-09-24", 0): ("service", "Your starting point deserves its own conversation. A free consultation at Pierce Fitness includes time to talk about your goals and a Visbody scan to see your baseline. Come with questions and leave with a clearer next step."),
    ("2026-09-24", 1): ("service", "Some goals call for one on one attention. Others feel better with a few people beside you. Pierce Fitness offers Personal Training and Semi Private Personal Training. Which setting would help you show up consistently?"),
    ("2026-09-25", 0): ("service", "Training and food questions often show up together. Nutrition Coaching is available at Pierce Fitness alongside the training options. Bring your questions to a coach and explore what kind of support makes sense for you."),
    ("2026-09-25", 1): ("service", "A useful routine is one you can keep returning to. If twice a week is realistic for your life, start there and talk with a coach about the training option that fits. What days could you make time for yourself?"),
}
MISSING = (("2026-09-20", 0), ("2026-09-21", 1))


def _rows(store):
    return [r for r in store.list_month(BASE, "2026-09")
            if "2026-09-19" <= str(r.get("post_date", ""))[:10] <= "2026-09-25"]


def main():
    from agent.portal_calendar_store import SupabaseCalendarStore, _media_stage_belt
    from agent import media_guard
    from agent.copy_gate import bound_opening_hook, violations

    copy = {key: (pillar, bound_opening_hook(caption))
            for key, (pillar, caption) in COPY.items()}
    if any(violations(caption) for _, caption in copy.values()):
        raise SystemExit("Prepared copy failed Pierce's punctuation gate")

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
        pillar, caption = copy[(day, slot)]
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
    for (day, slot), (pillar, caption) in copy.items():
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
    for (day, slot), (_, caption) in copy.items():
        matching = [r for r in rows if str(r.get("post_date"))[:10] == day
                    and r.get("slot_index") == slot
                    and r.get("account") in ("instagram", "facebook")
                    and r.get("format") == "feed"]
        if len(matching) != 2 or any(r.get("caption") != caption for r in matching):
            raise SystemExit(f"Copy did not persist on both feeds for {day} slot{slot}")
    print("Verified 14 IG and 14 FB pending feeds, 14 distinct IG captions")


if __name__ == "__main__":
    main()
