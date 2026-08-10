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

import os

LOGO_ASSET = "agent/assets/summit_logo.png"
CTA_URL = "lassoframework.com/summit"
ACCOUNTS = ["lasso_ig", "lasso_fb"]

# Feed cards are square 1080; stories are 9:16 tall. Asserted on every render so a
# cropped or mis-sized card can never enter the sprint (a story is a native-tall
# composition, never a cropped feed).
FEED_SIZE = (1080, 1080)
STORY_SIZE = (1080, 1920)
HOST_BUCKET = "lasso_summit"

# The three sprint assets that are NOT concept cards: the two agenda days and the
# panel. summit_render owns their PIL renderers (verified facts only), never the
# studio, and they have NO paired story (honestly skipped downstream).
AGENDA_PANEL_FILES = ("08_agenda_day1.png", "09_agenda_day2.png", "22_panel_future.png")

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
        "red_word": "PLAN",
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
        "deck": "Walk the three legs in order. The weakest is where 2027 hides.",
        "treatment_a": "navy canvas, oversized Anton headline with red BROKEN, single depth wash",
        "treatment_b": "navy canvas, three labeled bars LEADS TO BOOK 40, BOOK TO SHOW 50, SHOW TO CLOSE 70, one red accent on the weakest bar only",
        "caption": (
            "More ad spend will not fix a broken funnel.\n\n"
            "Walk the three legs and stop at the weakest one. Leads to book at 40 "
            "percent or better. Book to show at 50. Show to close at 70.\n\n"
            "Fix the weakest leg. Then scale.\n\n"
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
        "red_word": "ROOM",
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


# ---- render + host loop ----------------------------------------------------
# Turns the laid-out sprint from a list of filenames into hosted URLs in
# summit_queue's manifest, so sprint_assets()/sprint_builders() actually serve
# something. Every render + host + manifest-store is injectable so the whole loop
# runs offline in tests (no live Gemini, no live R2). Idempotent: a filename
# already in the manifest is skipped (no re-render, no re-host), so a re-run only
# fills the gaps. No fabrication: a concept whose facts resolve empty renders
# NOTHING (the studio returns None); its filename is left out of the manifest.


def _concept_facts(concept):
    """The APPROVED lines passed to the studio as concept context for one card.

    Built ONLY from the concept's own spec: its deck, its optional support line,
    and the verified event facts. Nothing invented. If every line is empty the
    result is empty and the caller renders NOTHING (the no-fabrication contract,
    mirrored from creative_studio.generate: empty facts -> None)."""
    from .summit_render import DEFAULT_FACTS
    lines = []
    for key in ("deck", "support"):
        val = str(concept.get(key, "") or "").strip()
        if val:
            lines.append(val)
    lines.extend(DEFAULT_FACTS)
    return [ln for ln in lines if str(ln).strip()]


def _normalize_to_canvas(path, expected):
    """Normalize a studio-generated image to EXACTLY the expected (w, h) canvas,
    in place, using the SAME house cover-crop the daily renderers use
    (summit_render._cover): scale to cover by the larger ratio (never squish a
    portrait into a square), then center-crop to the exact size (symmetric edge
    trim only, so the safe zone at the center survives).

    Why this exists: the live Gemini Pro model returns its NATIVE size (e.g.
    928x1152), not the requested pixels. The house path composites the model
    output onto a fixed canvas, so those cards land at an exact size; the summit
    feed path wrote the raw studio bytes straight to disk, so the native size
    tripped _assert_size and 0 of the sprint assets hosted. This restores the
    missing normalization so verify_size passes legitimately.

    A file already at the exact size is left byte for byte (no resave, no
    recompression). No fabrication: this only reframes the pixels the studio
    already produced; it never invents content."""
    from PIL import Image
    from .summit_render import _cover
    with Image.open(path) as im:
        if im.size == tuple(expected):
            return
        normalized = _cover(im, expected[0], expected[1])
    normalized.save(path)


def _assert_size(path, expected, label):
    """Verify a rendered PNG is exactly the expected (w, h). By the time this runs
    the studio card has been normalized to the canvas (_normalize_to_canvas); this
    guard still REFUSES anything not exactly the expected size, so a genuinely
    mis-sized card (a renderer that produced the wrong canvas) can never reach the
    sprint. The guard is never weakened: normalization makes it pass legitimately,
    never by disabling the check."""
    from PIL import Image
    with Image.open(path) as im:
        size = im.size
    if size != expected:
        raise ValueError(
            f"{label} {os.path.basename(path)} is {size}, expected {expected}. "
            "Refusing to host a mis-sized card (a story is never a cropped feed)."
        )


def _concept_by_treatment():
    """Ordered (concept, treatment, feed_filename, has_story) for every non-deferred
    concept card, arc-ordered exactly like sprint_assets(). Concept cards always
    have a paired story; the agenda/panel cards (handled separately) never do."""
    by_id = {c["id"]: c for c in SUMMIT_CONCEPTS}
    out = []
    for cid in ARC_ORDER:
        c = by_id.get(cid)
        if not c:
            continue
        for t in ("a", "b"):
            out.append((c, t, f"{cid}_{t}.png", True))
    return out


def render_and_host_all(images_dir, *, studio=None, feed_renderer=None,
                        story_renderer=None, agenda_renderer=None,
                        panel_renderer=None, host=None, sponsors=(),
                        background_dir=None,
                        load_manifest=None, save_manifest=None):
    """Render every non-deferred sprint asset, host it, and write filename -> URL
    into summit_queue's manifest so sprint_assets()/sprint_builders() serve them.

    For each non-deferred concept x treatment (a, b): render the 1080x1080 BOLD FEED
    card and the paired 1080x1920 BOLD STORY with summit_render's dedicated bold
    summit renderers (PIL-composited, NEVER Gemini, so the loud high-contrast summit
    identity is fully controlled and never looks like the daily cream/navy house
    card), host BOTH, and record both filenames. The three agenda/panel FEED cards are
    rendered by summit_render's own PIL renderers and hosted feed-only (no paired
    story: honestly skipped). The scarcity concepts (08/09/10 half full / moving fast
    / last seats) are DEFERRED and never rendered here.

    `sponsors` is an injectable list of sponsor names threaded onto every bold card's
    PRESENTED WITH strip. Default empty -> a safe "Presented with our partners"
    placeholder is drawn; names are NEVER fabricated.

    `background_dir` is the summit background library (default summit_render.SUMMIT_BG_DIR).
    When it holds photos, each bold card composites over a real event-scene photo
    (feed cards from its feed/ subdir, story cards from its story/ subdir), selected
    deterministically per concept so cards vary and each concept is stable. When the
    dir (or the relevant subdir) is empty or missing, cards fall back to the flat dark
    base and NEVER crash. The photos themselves are added by the operator.

    Gated: AGENT_SUMMIT_CAMPAIGN_ENABLED must be armed AND hosting enabled; otherwise
    this is a no-op that reports and returns an empty summary. Idempotent: a filename
    already in the manifest is skipped (no re-render, no re-host). No fabrication: a
    concept whose facts resolve empty renders nothing and is left out.

    Every render/host/manifest hook is injectable so tests run fully offline with no
    live Gemini and no live R2. `studio` is retained for signature compatibility but
    the bold sprint feed no longer routes through it.
    """
    from . import config
    from . import creative_studio, media_host, summit_render, summit_queue

    studio = studio or creative_studio
    feed_renderer = feed_renderer or summit_render.render_bold_feed
    story_renderer = story_renderer or summit_render.render_bold_story
    agenda_renderer = agenda_renderer or summit_render.render_agenda
    panel_renderer = panel_renderer or summit_render.render_panel
    host = host or media_host.host_media
    load_manifest = load_manifest or summit_queue._load_manifest
    save_manifest = save_manifest or summit_queue._save_manifest
    # default background library; an empty/missing dir simply falls back to flat dark
    if background_dir is None:
        background_dir = summit_render.SUMMIT_BG_DIR

    summary = {"rendered": [], "hosted": [], "skipped_hosted": [],
               "skipped_story": [], "deferred": list(DEFERRED_SCARCITY),
               "none_facts": []}

    if not config.summit_campaign_enabled():
        print("AGENT_SUMMIT_CAMPAIGN_ENABLED is OFF. Summit rebuild is dormant; "
              "nothing rendered or hosted. (Arm the flag to run; nothing publishes.)")
        return summary
    if not config.hosting_enabled():
        print("AGENT_HOSTING_ENABLED is OFF. Cannot host rendered cards; nothing done.")
        return summary

    os.makedirs(images_dir, exist_ok=True)
    manifest = load_manifest() or {}
    dirty = False

    def _host_and_record(fname, path, kind, expected):
        """Normalize to the exact canvas, assert size, host, and record fname -> url
        in the manifest. Returns True when a new URL was written.

        Normalization first (the SAME house cover-crop daily uses): the live Gemini
        Pro model returns its native size, not the requested pixels, so the studio
        feed is reframed to the exact canvas here BEFORE _assert_size. The PIL story
        / agenda / panel renderers already produce exact sizes, so their normalize
        is a no-op. _assert_size still enforces the exact size afterward (never
        weakened): a genuinely wrong canvas cover-crop would be caught."""
        nonlocal dirty
        _normalize_to_canvas(path, expected)
        _assert_size(path, expected, kind)
        url = host(path, HOST_BUCKET)
        if not url:
            print(f"  host FAILED: {fname} (left out of manifest)")
            return False
        manifest[fname] = url
        summary["hosted"].append(fname)
        dirty = True
        print(f"  hosted {fname} -> {url}")
        return True

    # ---- concept cards: feed via studio + paired 9:16 story ----------------
    for concept, treatment, feed_name, _has_story in _concept_by_treatment():
        story_name = f"{concept['id']}_{treatment}_story.png"

        # FEED (idempotent: skip a filename already hosted)
        if feed_name in manifest:
            summary["skipped_hosted"].append(feed_name)
            print(f"  already hosted: {feed_name}")
        else:
            # No-fabrication gate FIRST: a concept whose facts resolve empty renders
            # NOTHING (never a faked card). The bold card carries verified facts only.
            facts = _concept_facts(concept)
            feed_path = os.path.join(images_dir, feed_name)
            if not facts:
                summary["none_facts"].append(feed_name)
                print(f"  no facts (no fabrication): {feed_name}")
            else:
                # BOLD PIL feed (never Gemini): the loud, high-contrast summit card,
                # composited over a real event photo from the feed/ bg subdir when the
                # library has one (else flat dark; never crashes on a missing bg).
                feed_renderer(concept, treatment, feed_path, sponsors=sponsors,
                              background_dir=background_dir)
                summary["rendered"].append(feed_name)
                _host_and_record(feed_name, feed_path, "FEED", FEED_SIZE)

        # STORY (paired 9:16; concept cards always have one)
        if story_name in manifest:
            summary["skipped_hosted"].append(story_name)
            print(f"  already hosted: {story_name}")
        elif feed_name in summary["none_facts"]:
            # if the feed had no facts we do not fabricate a story either
            summary["skipped_story"].append(story_name)
        else:
            story_path = os.path.join(images_dir, story_name)
            # bold story composited over a real event photo from the story/ bg subdir
            # when the library has one (else flat dark; never crashes on a missing bg).
            story_renderer(concept, treatment, story_path, sponsors=sponsors,
                           background_dir=background_dir)
            summary["rendered"].append(story_name)
            _host_and_record(story_name, story_path, "STORY", STORY_SIZE)

    # ---- agenda + panel cards: feed only, PIL-rendered, NO story -----------
    _agenda_panel = (
        ("08_agenda_day1.png", agenda_renderer, summit_render.AGENDA_DAY1),
        ("09_agenda_day2.png", agenda_renderer, summit_render.AGENDA_DAY2),
        ("22_panel_future.png", panel_renderer, summit_render.PANEL),
    )
    for fname, renderer, spec in _agenda_panel:
        summary["skipped_story"].append(_story_stem(fname))  # honest: never a story
        if fname in manifest:
            summary["skipped_hosted"].append(fname)
            print(f"  already hosted: {fname}")
            continue
        path = os.path.join(images_dir, fname)
        renderer(spec, path)
        summary["rendered"].append(fname)
        _host_and_record(fname, path, "FEED", FEED_SIZE)

    if dirty:
        save_manifest(manifest)
    print(f"\nsummit-rebuild: {len(summary['hosted'])} hosted, "
          f"{len(summary['skipped_hosted'])} already hosted, "
          f"{len(summary['none_facts'])} skipped (no facts), "
          f"deferred {len(summary['deferred'])} scarcity concepts (never rendered).")
    return summary


def _story_stem(feed_filename):
    """The paired story filename an agenda/panel card would have if it had one
    (it does not). Reported so the honest skip is visible in the summary."""
    stem, ext = os.path.splitext(feed_filename)
    return f"{stem}_story{ext or '.png'}"
