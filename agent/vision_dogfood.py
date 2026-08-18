"""
Echo Vision dogfood diff (ECHO_VISION_SPEC §9.2): the old-picks (vision OFF, least-recently-
served rotation) vs new-picks (vision ON, content-scored to the slot job) comparison for a
gym's library across a month's pillars. This DIFF is the go/no-go evidence — it goes to Blake
before any client gym converts (hard limit). Read-only: it selects + explains, it never
writes a calendar or publishes.

Run on the worker where the gym's analyzed library lives:
    python3 -m agent.vision_dogfood lasso
"""

import os
import sys

from . import library, rotation, dam, vision


def pick_diff(account_key, library_path, pillars, *, day="2026-09-01", served=None):
    """Per pillar, the vision-OFF pick vs the vision-ON pick + why. Offline: pass `served`
    (the load_served entries for this account) to make it deterministic in tests."""
    creatives = [c for c in library.list_creatives(library_path)
                 if getattr(c, "media_type", "") == "image"]
    served = served if served is not None else rotation.load_served().get(account_key, [])
    last = {}
    for e in served:
        last[e.get("key", "")] = e.get("date", "")

    def _legacy():
        # least-recently-served, cluster-keyed, name-stable (the vision-OFF behavior)
        pool = sorted(creatives, key=lambda c: (last.get(dam.rotation_key(c.path), ""),
                                                os.path.basename(c.path)))
        return pool[0] if pool else None

    rows = []
    off_pick = _legacy()
    for pillar in pillars:
        best, best_score = None, -99.0
        excluded = 0
        for c in creatives:
            a = vision.stored_analysis(c.path)
            ok, _r = vision.auto_plannable(a)
            if not ok:
                excluded += 1
                continue
            score, ok_slot = vision.content_score(a, pillar)
            if not ok_slot:
                continue
            if score > best_score:
                best, best_score = c, score
        on_name = os.path.basename(best.path) if best else None
        off_name = os.path.basename(off_pick.path) if off_pick else None
        reason = (f"score {best_score:.1f}"
                  + (" (WEAK match)" if best and best_score < vision.VISION_SCORE_FLOOR else "")
                  ) if best else "no plannable image for this slot"
        # A real content-matched swap: vision picked SOMETHING and it differs from legacy.
        # on=None is NOT a win — it means vision found nothing plannable (reported separately)
        # so the header count never overstates the upgrade.
        rows.append({"pillar": pillar, "vision_off": off_name, "vision_on": on_name,
                     "swapped": bool(on_name) and off_name != on_name,
                     "no_pick": on_name is None,
                     "reason": reason, "excluded_flagged": excluded})
    return rows


def format_report(account_key, rows):
    swapped = sum(1 for r in rows if r["swapped"])
    no_pick = sum(1 for r in rows if r["no_pick"])
    out = [f"ECHO VISION dogfood diff — {account_key}",
           f"{swapped}/{len(rows)} slots get a DIFFERENT content-matched photo with vision on"
           + (f"; {no_pick} slots have NO plannable image (vision would leave them for a coach)"
              if no_pick else ""),
           ""]
    for r in rows:
        mark = "NO-PICK" if r["no_pick"] else ("SWAPPED" if r["swapped"] else "same   ")
        out.append(f"[{mark}] {r['pillar']:<14} off={r['vision_off']}  ->  "
                   f"on={r['vision_on']}  ({r['reason']}; {r['excluded_flagged']} flagged)")
    return "\n".join(out)


# LASSO's month pillars (the client category slot jobs). GBP pillars differ; this is the
# FB/IG dogfood diff, which is where the photo-mismatch problem lives.
_DEFAULT_PILLARS = ("testimonial", "service", "community", "about", "offer", "faq")


def run(account_key="lasso_ig", *, library_path=None, pillars=None, logger=None):
    log = logger or (lambda m: print(m))
    lib = library_path or os.path.join("content_library",
                                       account_key.rsplit("_", 1)[0]
                                       if account_key.endswith(("_ig", "_fb")) else account_key)
    if not os.path.isdir(lib):
        # LASSO's committed cards are flat in content_library/ (lasso_*), like the GBP dogfood
        lib = "content_library"
    rows = pick_diff(account_key, lib, list(pillars or _DEFAULT_PILLARS))
    report = format_report(account_key, rows)
    log(report)
    return report


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    gym = argv[0] if argv else "lasso"
    acct = gym if gym.endswith(("_ig", "_fb")) else f"{gym}_ig"
    run(acct)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
