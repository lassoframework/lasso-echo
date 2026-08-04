"""
LASSO Growth Summit 2026 — card rebuild spec (house style + fabrication clean).

The 14 reference cards were concept-only (C+ with hard defects: garbled text,
hallucinated logos, generated crowds sold as ours, baked dates, dashes, blocked
claims). This module rebuilds the CLEARED concepts through Echo's house style so
creative_studio renders them (Pro model + PIL text composition — never model
rendered headline text) and the Section 10 grade gate + fabrication gate pass.

Scope (Blake rulings, 2026-07-29):
  - Blocked claims DROPPED: no $3,000 LTV, no "8+ members", no 20x/10x ROI, no
    $299/$449 pricing, no "30 to 40 percent". Receipted spine only.
  - Scarcity concepts DEFERRED: 08 half full, 09 moving fast, 10 last seats,
    13 countdown, 14 final call are NOT built here (they need real registration
    numbers at post time).
  - Logo: agent/assets/summit_logo.png ONLY. No card generates a logo.
  - 06 the room: TYPE-LED both treatments (no real event photo exists; no
    fabricated crowds). Treatment B uses an aspirational venue anchor, never
    fake attendees captioned as proof.

Receipted spine (04_summit_campaign.md VERIFIED FACTS + 02_verified_stats.md):
  100 seats, 10 industry leaders, 2 days, November 7 and 8 2026, Virgin Hotel
  Nashville, complete 2027 growth playbook, 100 serious operators, close under
  vs at 70 percent (funnel 70/50/50/40).

No dashes/hyphens anywhere. "NOV 7 + 8" on art, "November 7 and 8" in captions.
No week numbers or post dates baked into art; the schedule owns dates.
"""

LOGO_ASSET = "agent/assets/summit_logo.png"
CTA_URL = "lassoframework.com/summit"
ACCOUNTS = ["lasso_ig", "lasso_fb"]

# Each concept renders TWO treatments:
#   A = type-led editorial (Anton oversized headline, ONE red word, Oswald
#       tracked eyebrow, red rule, Montserrat deck, ghosted-numeral depth layer)
#   B = data/photo-led (same hierarchy; anchor is a designed data element or an
#       aspirational venue treatment, still one red accent)
# canvas: "cream" (#FAF6F0) or "navy" (#121E3C). one_red names the single red element.

SUMMIT_CONCEPTS = [
    {
        "id": "01_invitation",
        "eyebrow": "NASHVILLE 2026",
        "headline": "I SAVED YOU A SEAT",
        "red_word": "SEAT",
        "support": "When the room is full there is no waitlist.",
        "deck": "100 seats. 10 leaders. 2 days. You leave with a plan.",
        "treatment_a": "cream canvas, oversized Anton headline, ghosted numeral 100 as depth layer, NOV 7 + 8 footer row",
        "treatment_b": "navy canvas, three fact blocks NOV 7 + 8 / VIRGIN HOTEL NASHVILLE / 100 SEATS, logo lockup top",
        "caption": (
            "I saved you a seat in Nashville.\n\n"
            "100 seats. 10 leaders. 2 days. You leave with a plan, not a notebook.\n\n"
            "November 7 and 8. Virgin Hotel Nashville.\n\n"
            "Claim your seat.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "02_deliverable",
        "eyebrow": "WHAT YOU LEAVE WITH",
        "headline": "A PLAN, NOT A NOTEBOOK",
        "red_word": "PLAN",
        "deck": "By Sunday you walk out with your 2027 growth plan.",
        "treatment_a": "navy canvas, oversized Anton headline with red PLAN, single depth wash",
        "treatment_b": "cream canvas, four checklist rows (revenue target and member math, the broken funnel leg and fix, sales retention and team plays, a 90 day action plan), one red rule",
        "caption": (
            "Most conferences leave you with a notebook you never open again.\n\n"
            "You leave this one with your 2027 growth plan done. Your revenue target "
            "and the member math. Your one broken funnel leg and the fix. Your sales, "
            "retention, and team plays for the year.\n\n"
            "A plan, not a notebook.\n\n"
            "Claim your seat.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "03_agenda",
        "eyebrow": "TEN SESSIONS",
        "headline": "TEN SESSIONS. ONE PLAN.",
        "red_word": "ONE PLAN",
        "deck": "Ten leaders. Ten sections. You build it page by page.",
        "treatment_a": "cream canvas, oversized Anton headline, ghosted 10 depth layer",
        "treatment_b": "navy canvas, two column numbered session grid 01 to 10 with flat labels, one red column rule",
        "caption": (
            "Ten sessions. Ten leaders. One plan you build page by page.\n\n"
            "Where you are now, your 2027 target, the funnel diagnostic, offer and "
            "positioning, and lead generation. Then the sales system, retention, team "
            "and leadership, client value, and pricing.\n\n"
            "You leave with a complete 2027 growth playbook.\n\n"
            "Claim your seat.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "04_funnel",
        "eyebrow": "FUNNEL DIAGNOSTIC",
        "headline": "MORE AD SPEND WILL NOT FIX A BROKEN FUNNEL",
        "red_word": "BROKEN",
        "deck": "Walk the four legs in order. The first fail is where 2027 hides.",
        "treatment_a": "navy canvas, oversized Anton headline with red BROKEN, single depth wash",
        "treatment_b": "navy canvas, four labeled bars CLOSE 70, SHOW 50, BOOKING 50, LEAD VOLUME 40, one red accent on the first failing bar only",
        "caption": (
            "More ad spend will not fix a broken funnel.\n\n"
            "Walk the four legs in order and stop at the first one that fails. Close "
            "rate at 70 percent or better. Show rate at 50. Booking at 50. Lead volume "
            "at 40.\n\n"
            "Fix the broken leg. Then scale.\n\n"
            "We build this together in Nashville.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "05_math",
        "eyebrow": "THE MATH",
        "headline": "A GOAL WITHOUT MATH IS A WISH",
        "red_word": "WISH",
        "deck": "Set the target. Then back into the members and leads it takes.",
        "treatment_a": "cream canvas, oversized Anton headline with red WISH, ghosted equals sign depth layer",
        "treatment_b": "cream canvas, four numbered method steps (set the target, subtract today, divide by revenue per member, apply your close rate), one red rule. NO stat slab.",
        "caption": (
            "A goal without math is a wish.\n\n"
            "Set your 2027 revenue target. Subtract where you are today. That gap is "
            "the work. Divide by revenue per member for the members you need, then "
            "apply your close rate for leads per month.\n\n"
            "Now you have a number, not a hope.\n\n"
            "Run the math with us in Nashville.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "06_room",
        "eyebrow": "WHO IS IN THE ROOM",
        "headline": "ONE ROOM. TWO DAYS.",
        "red_word": "ONE ROOM",
        "support": "No stage pitches. Every minute is real strategy.",
        "deck": "100 serious operators. Not hobbyists. The room is the ROI.",
        "treatment_a": "navy canvas, oversized Anton headline, ghosted 100 depth layer",
        "treatment_b": "navy canvas, aspirational Virgin Hotel ballroom or styled seating treatment (NO fabricated attendees, NO sold out claim), logo lockup, one red accent",
        "caption": (
            "One hundred serious operators. One room. Two days.\n\n"
            "Not hobbyists. Owners who came to leave with a plan, and 99 peers who get "
            "what you are carrying.\n\n"
            "The room is the ROI.\n\n"
            "November 7 and 8. Virgin Hotel Nashville.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "07_numbers",
        "eyebrow": "NASHVILLE 2026",
        "headline": "100 OWNERS. 10 LEADERS. 2 DAYS. 1 PLAN.",
        "red_word": "1 PLAN",
        "deck": "You did not come to Nashville for notes. You came for a plan.",
        "treatment_a": "navy canvas, stacked numerals 100 10 2 1 with red diamond separators as the single red accent, Anton",
        "treatment_b": "cream canvas, four fact blocks 100 OWNERS / 10 LEADERS / 2 DAYS / 1 PLAN, one red rule",
        "caption": (
            "100 owners. 10 leaders. 2 days. 1 plan.\n\n"
            "You did not come to Nashville for notes. You came for a plan.\n\n"
            "November 7 and 8. Virgin Hotel Nashville.\n\n"
            "Claim your seat.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "13_audience",
        "eyebrow": "WHO IT IS FOR",
        "headline": "BUILT FOR SERIOUS OPERATORS",
        "red_word": "SERIOUS",
        "support": "If that is you, there is a seat with your name on it.",
        "deck": "Established gym owners who came to build 2027. Not hobbyists.",
        "treatment_a": "navy image, oversized Anton headline with red SERIOUS, fact row",
        "treatment_b": "cream canvas, who it is for checklist, one red accent",
        "caption": (
            "This room is not for everyone.\n\n"
            "It is 100 serious operators. Established gym owners who came to build "
            "2027, not to collect more notes. No stage pitches, just real strategy "
            "from people who have done it.\n\n"
            "If that is you, there is a seat.\n\n"
            "Claim your seat.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "11_stakes",
        "eyebrow": "THE STAKES",
        "headline": "THE GYM THAT FIGURES THIS OUT WINS",
        "red_word": "WINS",
        "deck": "Most owners are one system away from a breakthrough year.",
        "treatment_a": "cream canvas, oversized Anton headline with red WINS, single depth wash",
        "treatment_b": "cream canvas, two column contrast (closing under 70 percent vs closing at 70 percent or better, burning ad spend vs a funnel that holds, doing it all yourself vs systems), one red divider. NO fabricated member or ROI stats.",
        "caption": (
            "The gym that figures this out wins the year.\n\n"
            "Closing under 70 percent versus closing at 70 percent or better with a "
            "real sales system. Burning ad spend versus a funnel that holds. Doing it "
            "all yourself versus a business that runs on systems.\n\n"
            "Most owners are one system away.\n\n"
            "100 owners. 10 leaders. 2 days. 1 plan.\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "id": "12_outcome",
        "eyebrow": "THE OUTCOME",
        "headline": "WALK OUT WITH YOUR 2027 GROWTH PLAN",
        "red_word": "2027",
        "deck": "Not inspiration. Not notes. A written plan you run Monday.",
        "treatment_a": "navy canvas, oversized Anton headline with red 2027, single depth wash",
        "treatment_b": "navy canvas, four checklist rows (revenue target and member math, funnel fix, sales retention and team playbook, a 90 day action plan), one red accent. NO pricing, NO ROI.",
        "caption": (
            "Walk out with your 2027 growth plan.\n\n"
            "Not inspiration. Not notes. A written plan you run the Monday you get "
            "home. Your revenue target and member math, your funnel fix, and your "
            "sales, retention, and team playbook.\n\n"
            "A complete 2027 growth playbook.\n\n"
            "Claim your seat.\n"
            "lassoframework.com/summit"
        ),
    },
]

# Arc order for scheduling: invitation and value early, proof and method mid,
# stakes and outcome late. Scarcity is deferred, so nothing here is a late
# scarcity concept. Treatments alternate A then B so the feed never repeats a
# composition back to back.
ARC_ORDER = [
    "01_invitation", "07_numbers", "02_deliverable", "13_audience", "03_agenda",
    "06_room", "04_funnel", "05_math", "11_stakes", "12_outcome",
]

DEFERRED_SCARCITY = ["08_half_full", "09_moving_fast", "10_last_seats",
                     "13_countdown", "14_final_call"]


def build_schedule(start_slots):
    """Given a list of scheduled slot dates (2x per week, Tue + Fri), return the
    ordered [(date, concept_id, treatment, account)] plan. 9 concepts x 2
    treatments = 18 assets, each cross posted to lasso_ig + lasso_fb. Treatments
    alternate A/B down the arc so no two adjacent slots share a composition."""
    plan = []
    slot = 0
    for concept_id in ARC_ORDER:
        for treatment in ("a", "b"):
            if slot >= len(start_slots):
                break
            date = start_slots[slot]
            for acct in ACCOUNTS:
                plan.append((date, concept_id, treatment, acct))
            slot += 1
    return plan
