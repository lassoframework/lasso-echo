"""grade_fix.py — forward-book self-remediation (AGENT_GRADE_SELF_FIX, default OFF).

Blake's ruling (2026-08-27): "i keep getting F score it should fix it on its own
without sending me alot of slacks." When a gym's FORWARD BOOK grades below A the
nightly sweep (agent/jobs/grade_sweep.py) calls this module to repair what Echo
can honestly repair, then regrades. Flag OFF -> remediate_forward_book returns
ok:False and touches nothing.

WHAT IT FIXES (never fabricates, never publishes, never auto-approves):
  a. TRUE duplicate captions (the same caption hash on more than one post_date;
     the same-date IG/FB cross-post + paired story are ONE post by design and
     are not duplicates): the earliest date keeps its caption (or, when a
     human-owned row holds the caption, THAT date keeps it); every other fully
     wipeable date gets a FRESH caption regenerated through the SAME machinery
     the client build/backfill uses (client_month_run._clean_draft_for_day:
     A+ gate, banned words, copy gate, opening/angle variety), PATCHed onto the
     existing rows so the day keeps its SAME photo and slot.
  b. Day gaps: filled only where the EXISTING lanes can legitimately fill
     (client gyms: the scan/grow lane, which only builds when unused media
     exists; LASSO: the real month planner refill, which re-runs its own
     A-gate). An unfillable gap is recorded ONCE in kv and never storms the
     channel; heavy lanes run at most once per gym per day (kv stamp).
  c. Category over-cap (a pillar above 25% of the book): the excess wipeable
     dates get a caption regenerated from a DIFFERENT approved source category
     on the same photo, and the rows' pillar is re-pointed to the pillar that
     actually wrote the new caption (the label always tells the truth). The
     pass ITERATES (bounded) until every category is at or under 25% or no
     more wipeable rows can honestly move.
  d. Caption craft + path (Blake, 2026-08-27 "make it an A+ from our end"):
     a fully wipeable day whose caption trips the grader's craft checks
     (no_ask / thin_caption / hook_too_long, the same copy_gate.soft_flags the
     calendar grader scores) gets a FRESH caption regenerated on the SAME
     photo through the same gated builder path. The regenerated caption is
     swapped in ONLY when it actually clears the bar (exactly one ask, first
     line inside the hook band, length 150 to 500, zero soft flags, zero hard
     violations); otherwise the row keeps its current caption, so a decent
     caption is never traded for a worse one. When the book carries fewer
     than 5 booking-term asks (the path_to_join GYM leg), the regen of these
     ALREADY FLAGGED days is biased to carry the gym's REAL booking CTA (the
     first booking-term CTA in its approved voice-doc rotation; nothing is
     ever invented, and a missing CTA is an honest skip). Days that already
     pass are never touched.

  e. Off-avatar hooks (right_audience): a wipeable day whose HOOK leaks the
     wrong avatar (competitive-athlete language on a GYM book, elite/advanced
     language anywhere) is regenerated. Added 2026-08-31 — this defect class
     had NO repair path at all, so a book could sit one caption short of an A
     forever. The replacement lands only when it is BOTH on-avatar and
     craft-clean; an off-avatar hook is never traded for a worse caption.

  g. BODY sameness (AGENT_CTA_VARIETY, Blake 2026-09-06): two posts whose
     MIDDLE reads as the same template — caption_variety.body_similarity at or
     above BODY_SIMILARITY_THRESHOLD — get the LATER one regenerated fresh on
     the same photo. This is a real repair, not a trim: the replacement is an
     actual new caption grounded in the gym's own approved sources, and it
     lands only when it is craft-clean AND genuinely different from every
     other body in the book. A gym whose approved sources cannot supply a
     genuinely different post is an HONEST SKIP, counted and logged, never a
     forced or fabricated difference.

HARD GUARANTEES:
  * Only WIPEABLE (pending/draft/queued) rows are ever patched. The store's
    patch_pending_plan carries a server-side status filter, so an approved /
    published / denied row can never be modified even on a caller bug.
  * Every patched row stays 'pending' — it re-enters the human approval queue.
  * No caption is invented here: every fresh caption comes from the gym's own
    approved sources via the existing gated builder path. The MECHANICAL repair
    moves a line break and appends copy the gym already approved; it writes no
    new words at all.

THE FLOOR — what this module deliberately CANNOT repair, and why. A converging
loop needs a documented floor, otherwise "still below A" reads as failure when
it is actually the honest limit:

  1. APPROVED ROWS. A human approved that caption; Echo does not rewrite it.
     This is the single largest floor on a mature book (measured 2026-08-31:
     29 of pierce's 35 flagged posts and 9 of topfuel's 10 were approved).
     It clears the moment the gym re-approves or denies those days.
  2. NO APPROVED BOOKING CTA. The no_ask repair carries the gym's OWN booking
     CTA; a gym whose bible CTA rotation is an onboarding TODO and whose
     approved sources contain no booking-ask sentence gets an honest skip
     rather than an invented CTA. Fixed by filling the bible, not by code.
  3. THIN CAPTIONS. Mechanics cannot add content, so a too-short caption needs
     the regen, which needs enough approved source material. A gym with a thin
     library stays thin.
  4. CONTENT SUPPLY. A day gap with no unused media, and a category over-cap
     with no other approved source to move to, are supply problems. The lanes
     are asked once and an unfillable gap is recorded once, never re-announced.
  5. B2B CAPTION CONTENT. LASSO's captions come from the pillar builders; only
     the mechanical repair applies here, never the regen.
  6. BODY SAMENESS ON A THIN LIBRARY. The body repair needs the regen to hand
     back a caption whose MIDDLE is genuinely different from every other post
     in the book. A gym with too few approved sources or pillars physically
     cannot supply one, and the pass says so (`body_unrepairable`) rather than
     writing a difference that is not real. Fixed by adding approved source
     material, not by code.
"""
from __future__ import annotations

from agent import caption_variety, config, copy_gate
from agent.calendar_grade import _BOOKING_RE
from agent.caption_ledger import caption_hash
from agent.portal_calendar_store import _WIPEABLE_STATUSES

# The craft soft flags this module repairs (calendar_grade._caption_craft
# scores copy_gate.soft_flags; these three are the fixable caption defects).
_CRAFT_FLAGS = ("no_ask", "thin_caption", "hook_too_long")
_CAPTION_MIN, _CAPTION_MAX = 150, 500   # regen acceptance band (grader median wants >= 150)
_HOOK_MAX = 125                          # copy_gate hook_too_long band
_OVERCAP_MAX_ITER = 6                    # bounded convergence for the over-cap pass

# LLM WALL-CLOCK BUDGET PER GYM PER PASS (2026-08-31). Measured live: one
# _clean_draft_for_day regen costs 6 to 8 SECONDS. gritx alone has 30 flagged
# posts, so the old "LLM first, always" craft pass cost ~4 minutes for ONE gym
# and the nightly sweep across ten gyms could never finish — which is why five
# of seven books sat at C or worse with a repair that "runs" every night.
# The deterministic mechanical repair clears the same bar for the overwhelming
# majority of rows in ~0 seconds, so it is now tried FIRST and the LLM is the
# fallback, under a budget. When the budget is spent the pass keeps going on
# mechanics alone: a bounded pass that always finishes beats an unbounded one
# that never does.
_LLM_BUDGET_S = 90.0

# THE CRAFT PASS'S CONSECUTIVE-FAILURE CUTOFF (AGENT_CTA_VARIETY, 2026-09-06).
#
# Measured on production the morning this shipped: hillcountry through
# `remediate_forward_book` with llm_budget_s=400.0 (4.4x the default) returned
# craft_attempted=18, craft_fixed=0 -- EIGHTEEN consecutive regens, not one of
# which cleared `_clears_craft`. train7164ae502 ran 25+ the same way. Every one
# of those calls costs 6 to 8 seconds, so the craft pass alone spent the whole
# per-gym budget and `_fix_body_sameness` (which runs last, by design) logged
# "out of LLM budget" with body_fixed=0 on all three books. Raising the budget
# did NOT help, because the failure rate was 100%: more budget bought more
# failures. The cure is to stop asking.
#
# N=4. Two numbers bound it. Above: the observed streaks are 18 and 25+, so any
# N well under 18 catches the real case; N=4 costs at most ~30s of the default
# 90s budget before the pass concedes. Below: N must be high enough that a book
# where the regen genuinely works is never cut off by a run of bad luck -- and
# because a SUCCESS RESETS the counter (see below), tripping N=4 requires four
# failures with no success anywhere between them, which on a book with a
# working regen does not happen.
#
# A SUCCESS RESETS THE COUNTER, deliberately. What this cutoff detects is "this
# book's material cannot clear the craft bar", not "this pass has done enough
# work". A book where the regen clears 20 days should get all 20; only an
# unbroken run of failures is evidence about the book. Mechanical wins do not
# touch the counter either way: they consume no LLM call and say nothing about
# whether the LLM can help. And the cutoff stops LLM REGENS ONLY -- the free,
# deterministic `_mechanical_repair` keeps running on every remaining day,
# because that is the lane that actually clears most flagged posts.
_CRAFT_LLM_FAILURE_CAP = 4

# THE OVER-CAP PASS'S CONSECUTIVE-FAILURE CUTOFF (2026-09-07). The craft cutoff
# above was built from a 2026-09-06 measurement that named `_fix_craft` as the
# pass that ate the budget. Re-measured with per-pass wall clocks on the SAME
# gym the next morning, it is not: on hillcountry `_fix_overcap` spent 97 of the
# 90 second budget on TWO regens that both returned None, `_fix_craft` got 0.0
# seconds (all 18 of its days fell back to mechanics, which cleared none), and
# `_fix_body_sameness` was handed an already-expired deadline and broke on its
# first day. A cutoff on craft alone leaves that untouched, because the pass in
# front of craft never concedes.
#
# The over-cap regen fails for the same systemic reason and needs the same stop.
# It gets its own name rather than sharing craft's because it fails FASTER: an
# over-cap regen must come back with a DIFFERENT category, which is a stricter
# ask than craft's, so fewer attempts are needed before the book has answered.
# TWO consecutive failures is that answer. One can be a transient (an API
# hiccup, one awkward source); the second, with no success between them, is the
# book saying its approved sources cannot move a day off this category tonight.
# A success resets the run, exactly as it does for craft, so a book where the
# move genuinely works is never cut off. The next nightly sweep retries from
# scratch, so the cost of conceding early is one deferred repair; the cost of
# not conceding is measurably the whole book's budget.
_OVERCAP_LLM_FAILURE_CAP = 2

# The LLM-backed passes of `remediate_forward_book`, IN THE ORDER IT RUNS THEM.
# Used only to hand each pass its own sub-deadline (see `_LlmBudget`); the
# non-LLM passes (violations, invalid closings, ask excess) are absent because
# they cost no wall clock.
_LLM_PASS_ORDER = ("duplicates", "overcap", "craft", "audience", "body")


def _deadline(budget_s=None):
    """A monotonic wall-clock deadline for this pass's LLM spend."""
    import time
    return time.monotonic() + (float(budget_s) if budget_s is not None
                               else _LLM_BUDGET_S)


def _budget_left(deadline) -> bool:
    import time
    return deadline is None or time.monotonic() < deadline


class _LlmBudget:
    """The per-gym LLM wall clock, SHARED across passes with a floor reserved
    for the ones that have not run yet (AGENT_CTA_VARIETY, 2026-09-06).

    THE BUG THIS EXISTS FOR. Every LLM-backed pass used to be handed the SAME
    `deadline` object, so the budget was first-come-first-served: whichever pass
    ran first could legally spend all of it, and the passes after it got a
    deadline already in the past. Measured on production, that is exactly what
    happened -- the craft pass burned the entire 90s (and the entire 400s, when
    the budget was raised to test it) on regens that cleared nothing, and
    `_fix_body_sameness`, which runs LAST because it has to measure a caption
    with the closing line the earlier passes move, never got a single call on
    hillcountry, topfuel or train7164ae502.

    THE RULE. With N LLM-backed passes and a total budget T, every pass is
    handed an allowance of AT LEAST T/N when it asks for its deadline, no matter
    how much the passes before it consumed. A pass that asks early can still use
    the budget its predecessors left unspent -- it just may not reach into the
    T/N floor belonging to each pass that comes after it:

        allowance = max(T/N, remaining - (passes_after * T/N))

    So the guaranteed invariant is a FLOOR, not a fixed slice: an early pass on
    a book that needs it still gets most of the budget, and the last pass still
    gets T/N. A non-positive total (the `llm_budget_s=-1` idiom callers use to
    switch the LLM off entirely) yields a non-positive allowance and therefore a
    deadline already past, which is the behaviour those callers rely on.
    """

    def __init__(self, total_s=None, passes=_LLM_PASS_ORDER):
        import time
        self.passes = tuple(passes)
        self.total = (float(total_s) if total_s is not None else _LLM_BUDGET_S)
        self.floor = self.total / max(1, len(self.passes))
        self._end = time.monotonic() + self.total

    def deadline_for(self, pass_name):
        """A fresh monotonic deadline for one named pass."""
        import time
        now = time.monotonic()
        try:
            idx = self.passes.index(pass_name)
        except ValueError:                       # an unknown pass: no reserve
            idx = len(self.passes) - 1
        after = len(self.passes) - idx - 1
        remaining = self._end - now
        allowance = max(self.floor, remaining - (after * self.floor))
        return now + allowance


def _default_db():
    from agent import db
    return db


def _is_wipeable(row) -> bool:
    status = str((row or {}).get("status") or "").lower()
    return (not status) or status in _WIPEABLE_STATUSES


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def remediate_forward_book(gym_id, rows, store, *, profile, defects,
                           today_iso, caption_regen=None, gap_filler=None,
                           booking_cta=None, db=None, logger=None,
                           llm_budget_s=None) -> dict:
    """Best-effort self-remediation for one gym's forward book.

    Args:
        gym_id:        the tenant base ('gritx', 'lasso', ...).
        rows:          the forward-book rows the grade was computed over.
        store:         the calendar store (patch_pending_plan for caption/pillar
                       rewrites; the gap lanes use their own store paths).
        profile:       'GYM' or 'B2B' (from real_month_planner._profile_for).
        defects:       the CalendarGrade.defects list that drove the below-A grade.
        today_iso:     'YYYY-MM-DD'.
        caption_regen: injectable (row, avoid_captions, avoid_category) ->
                       (caption, category) | None. Defaults to the client build
                       context (None for B2B / when the context is unavailable).
        gap_filler:    injectable (gym_id, profile, store, today_iso, log, db) -> str.
        booking_cta:   injectable approved booking CTA string (the craft pass
                       resolves it from the gym's voice-doc CTA rotation when
                       None and the book is short of booking asks).
        db:            injectable kv module (agent.db).

    Returns {ok, captions_fixed, repillared, craft_fixed, craft_attempted,
             booking_asks_added, ask_trimmed, invalid_closings_removed,
             body_pairs, body_fixed, body_unrepairable,
             gap_fill, skipped, actions}.
    """
    log = logger or (lambda m: print(f"[grade-fix] {m}"))
    if not config.grade_self_fix_enabled():
        return {"ok": False, "reason": "AGENT_GRADE_SELF_FIX off",
                "captions_fixed": 0, "repillared": 0, "craft_fixed": 0,
                "craft_attempted": 0, "booking_asks_added": 0, "ask_trimmed": 0,
                "invalid_closings_removed": 0, "body_pairs": 0,
                "body_fixed": 0, "body_unrepairable": 0,
                "gap_fill": "none", "skipped": 0, "actions": []}

    actions = []
    result = {"ok": True, "captions_fixed": 0, "repillared": 0,
              "craft_fixed": 0, "craft_attempted": 0, "booking_asks_added": 0,
              "ask_trimmed": 0, "invalid_closings_removed": 0,
              "audience_fixed": 0, "audience_attempted": 0, "scrubbed": 0,
              "body_pairs": 0, "body_fixed": 0, "body_unrepairable": 0,
              "gap_fill": "none", "skipped": 0, "actions": actions}
    rows = list(rows or [])
    # BUDGET FAIRNESS (AGENT_CTA_VARIETY). With the flag armed each LLM-backed
    # pass draws its own sub-deadline out of one shared clock, so no earlier
    # pass can starve a later one (see `_LlmBudget`). Flag off = the old single
    # shared deadline, unchanged, first-come-first-served.
    budget = (_LlmBudget(llm_budget_s) if config.cta_variety_enabled() else None)
    deadline = _deadline(llm_budget_s)

    def _pass_deadline(name):
        return deadline if budget is None else budget.deadline_for(name)

    if caption_regen is None:
        caption_regen = _default_caption_regen(gym_id, profile, log)

    # Every caption already on the book: a regenerated caption may never
    # collide with an existing one (or another fresh one from this pass).
    avoid = {(r.get("caption") or "").strip()
             for r in rows if (r.get("caption") or "").strip()}

    # ---- f) hard copy violations (scrubbed, deterministically) -------------
    scrubbed = _fix_violations(gym_id, rows, store, log)
    result["scrubbed"] = scrubbed
    if scrubbed:
        actions.append(f"scrubbed banned dashes from {scrubbed} caption(s)")

    # ---- a) true duplicate captions (same hash on more than one date) ------
    dup_fixed, dup_skipped = _fix_duplicates(
        gym_id, rows, store, caption_regen, avoid, log,
        deadline=_pass_deadline("duplicates"))
    result["captions_fixed"] = dup_fixed
    result["skipped"] += dup_skipped
    if dup_fixed:
        actions.append(f"rewrote {dup_fixed} duplicate caption day(s) fresh on the same photo")

    # ---- c) category over-cap (iterates to convergence) ---------------------
    over_fixed, over_skipped = _fix_overcap(
        gym_id, rows, store, defects, caption_regen, avoid, log,
        deadline=_pass_deadline("overcap"))
    result["repillared"] = over_fixed
    result["skipped"] += over_skipped
    if over_fixed:
        actions.append(f"re-pillared {over_fixed} over-cap day(s) from a different approved source")

    # ---- d0) closings that were never valid CTAs ----------------------------
    # BEFORE the craft pass, because the ask deficit has to be counted against
    # REAL asks. An invalid closing makes a book look like it is already asking,
    # which is exactly why the craft pass found only 1 flagged day on a book
    # where 30 of 31 posts carried the same nonsensical line.
    invalid_closings = _fix_invalid_closings(gym_id, rows, store, log)
    result["invalid_closings_removed"] = invalid_closings
    if invalid_closings:
        actions.append(f"removed a closing line that is not a valid CTA from "
                       f"{invalid_closings} post(s)")

    # ---- d) caption craft + path (booking asks) ------------------------------
    craft_fixed, craft_attempted, booking_added = _fix_craft(
        gym_id, rows, store, profile, caption_regen, avoid, log,
        booking_cta=booking_cta, deadline=_pass_deadline("craft"))
    result["craft_fixed"] = craft_fixed
    result["craft_attempted"] = craft_attempted
    result["booking_asks_added"] = booking_added
    result["skipped"] += max(0, craft_attempted - craft_fixed)
    if craft_fixed:
        actions.append(f"rewrote {craft_fixed} caption(s) that tripped craft flags (ask/hook/length)")
    if booking_added:
        actions.append(f"carried the gym's real booking CTA onto {booking_added} day(s)")

    # ---- d2) ask EXCESS (the other half of the 33% target) -------------------
    # _fix_craft can only ADD, and only to a craft-flagged day, so on a book
    # already stapled to 96.8% asks it has nothing to grip. This removes the
    # staple. Runs after the craft pass so it measures the book as repaired.
    ask_trimmed = _fix_ask_excess(gym_id, rows, store, log)
    result["ask_trimmed"] = ask_trimmed
    if ask_trimmed:
        actions.append(f"trimmed the stapled closing ask off {ask_trimmed} post(s)")

    # ---- e) off-avatar hooks (right_audience) -------------------------------
    aud_fixed, aud_attempted = _fix_audience(
        gym_id, rows, store, profile, caption_regen, avoid, log,
        deadline=_pass_deadline("audience"))
    result["audience_fixed"] = aud_fixed
    result["audience_attempted"] = aud_attempted
    result["skipped"] += max(0, aud_attempted - aud_fixed)
    if aud_fixed:
        actions.append(f"rewrote {aud_fixed} off-avatar hook(s) back onto the gym's avatar")

    # ---- g) body sameness (the MIDDLE of two posts, not their closing) -------
    # LAST of the caption passes, deliberately, and the order is load bearing:
    # `caption_variety.body_text` measures a caption with its CLOSING LINE
    # STRIPPED, so every pass above that touches a closing line MOVES the cut
    # this pass measures from. d0 removes an invalid closing, d appends a real
    # one and d2 trims a stapled one; a caption reading "hook / body / CTA" has
    # a two line body before those passes and a one line body after. Measuring
    # body sameness first would score a book that no longer exists by the end of
    # the run -- and it is exactly that shape that produces the real 1.0 pairs
    # (hillcountry, topfuel and train7164ae502 all repeat a HOOK verbatim, which
    # only becomes the whole body once the closing is off). Running last also
    # means the fresh captions written by c) over-cap and e) audience are
    # themselves checked for body sameness instead of being trusted.
    body_pairs, body_fixed, body_unrepairable = _fix_body_sameness(
        gym_id, rows, store, caption_regen, avoid, log,
        deadline=_pass_deadline("body"))
    result["body_pairs"] = body_pairs
    result["body_fixed"] = body_fixed
    result["body_unrepairable"] = body_unrepairable
    result["skipped"] += body_unrepairable
    if body_fixed:
        actions.append(f"rewrote {body_fixed} post(s) whose body read as a repeat "
                       "of another post in the book")
    if body_unrepairable:
        actions.append(f"{body_unrepairable} near-duplicate body/bodies could not "
                       "be honestly rewritten (source material too thin)")

    # ---- b) day gaps ---------------------------------------------------------
    gap_dates = [str(d[1])[:10] for d in (defects or [])
                 if d and d[0] == "consistency" and "gap of" in str(d[2])]
    if gap_dates:
        filler = gap_filler if gap_filler is not None else _default_gap_filler
        outcome = "error"
        try:
            outcome = filler(gym_id, profile, store, today_iso, log, db=db)
        except Exception as exc:  # noqa: BLE001 - remediation is best effort
            log(f"{gym_id}: gap filler raised {type(exc).__name__}")
        result["gap_fill"] = outcome
        if outcome == "filled":
            actions.append("refilled day gaps through the existing build lane")
        else:
            # No honest fill exists (no unused media / lane dark). NOT a defect
            # to re-announce hourly: record each gap once and move on.
            _record_gaps_once(gym_id, gap_dates, log, db=db)

    return result


# ---------------------------------------------------------------------------
# f) hard copy violations — the house scrubber, not a rewrite
# ---------------------------------------------------------------------------

def _fix_violations(gym_id, rows, store, log) -> int:
    """Scrub banned dashes and intraword hyphens out of wipeable captions.

    A copy violation is a HARD block: calendar_grade zeroes the ENTIRE
    caption_craft leg (20 points) for the whole book on the first offending
    row. Measured 2026-08-31, ONE row with an intraword hyphen was costing
    LASSO its whole craft leg and no pass could clear it, because every other
    repair path only ever looked at soft flags.

    copy_gate.scrub is the house scrubber and its documented contract is
    'rewrite, never reject': long dashes become ', ', intraword hyphens become
    a space, and URLs, @handles and #tags pass through untouched. It writes no
    new words, so this is a formatting fix, not a caption rewrite — the safest
    repair in the module. Returns the number of rows scrubbed."""
    fixed = 0
    for r in rows:
        cap = r.get("caption") or ""
        if not cap.strip() or not copy_gate.violations(cap):
            continue
        if not _is_wipeable(r):
            continue                    # human-owned copy is never rewritten
        clean = copy_gate.scrub(cap)
        if not clean or clean == cap or copy_gate.violations(clean):
            continue                    # scrubber could not honestly clear it
        if _patch_date_rows(gym_id, [r], store, clean, None, log):
            fixed += 1
    return fixed


# ---------------------------------------------------------------------------
# a) duplicate captions
# ---------------------------------------------------------------------------

def _fix_duplicates(gym_id, rows, store, caption_regen, avoid, log,
                    deadline=None):
    """Rewrite true duplicate-caption days (same hash on >1 post_date).

    Keeper: the earliest date holding the hash — unless a human-owned row holds
    it on some date, in which case EVERY fully wipeable date is rewritten (the
    human-owned caption always stays). A date with any non-wipeable row is
    never rewritten. Returns (days_fixed, days_skipped)."""
    by_hash: dict = {}
    for r in rows:
        h = caption_hash(r.get("caption") or "")
        d = str(r.get("post_date") or "")[:10]
        by_hash.setdefault(h, {}).setdefault(d, []).append(r)

    fixed = skipped = 0
    for _h, by_date in by_hash.items():
        if len(by_date) <= 1:
            continue
        locked_dates = {d for d, grp in by_date.items()
                        if any(not _is_wipeable(r) for r in grp)}
        wipe_dates = sorted(d for d in by_date if d not in locked_dates)
        fix_dates = wipe_dates if locked_dates else wipe_dates[1:]
        for d in fix_dates:
            if caption_regen is None or not _budget_left(deadline):
                skipped += 1
                continue
            out = None
            try:
                out = caption_regen(by_date[d][0], avoid, "")
            except Exception as exc:  # noqa: BLE001
                log(f"{gym_id} {d}: caption regen raised {type(exc).__name__}")
            if not out:
                skipped += 1
                log(f"{gym_id} {d}: no fresh caption could be built for the "
                    "duplicate; left in place")
                continue
            new_cap, new_cat = out
            if _patch_date_rows(gym_id, by_date[d], store, new_cap, new_cat, log):
                fixed += 1
                avoid.add(new_cap.strip())
            else:
                skipped += 1
    return fixed, skipped


# ---------------------------------------------------------------------------
# c) category over-cap
# ---------------------------------------------------------------------------

def _cat_of(row):
    return (row.get("pillar") or row.get("category") or "")


def _cat_posts(rows):
    """The book as (post_key, rows) pairs — the SAME grouping the grader uses.

    2026-08-31: the grader counts mix per POST (one photo cross-posted to IG,
    FB and a story is one serving of its pillar), but this pass still counted
    ROWS. The arithmetic disagreed, so the headroom guard believed a target
    category had room it did not have, and a 'repair' moved 3 sunnyside days
    out of an over-cap pillar straight into a NEW over-cap pillar: 71 (C) ->
    67 (D). A repair must never be able to lower the grade."""
    from agent.calendar_grade import posts_of
    return posts_of(rows)


def _over_cap_cats(rows):
    """Categories currently above 25% of the book, counted in POSTS exactly as
    calendar_grade._content_mix counts them."""
    from collections import Counter
    posts = _cat_posts(rows)
    n = len(posts)
    if not n:
        return []
    counts = Counter(_cat_of(grp[0]) for _k, grp in posts)
    return sorted(cat for cat, count in counts.items() if count / n > 0.25)


def _fix_overcap(gym_id, rows, store, defects, caption_regen, avoid, log,
                 deadline=None):
    """Iterate the over-cap move until every category is at or under 25% of
    the plan or no more wipeable rows can honestly move. Activation is still
    gated on the GRADER's defect list (this pass never runs speculatively);
    after the first pass the over-cap set is recomputed from the live rows so
    a single sweep converges instead of leaving residue (eng offer 31%, pierce
    49% both survived the old single pass). Bounded at _OVERCAP_MAX_ITER; an
    iteration that moves nothing stops the loop (honest stop).
    Returns (days_fixed, days_skipped)."""
    over_cats = [str(d[1]) for d in (defects or [])
                 if d and d[0] == "content_mix" and "over 25%" in str(d[2])]
    if not over_cats:
        return 0, 0
    if caption_regen is None:
        return 0, len(over_cats)

    total_fixed = total_skipped = 0
    # ONE cutoff across every iteration (see _OVERCAP_LLM_FAILURE_CAP): a regen
    # that has failed twice running does not become likelier to work because the
    # outer loop went round again. Armed with AGENT_CTA_VARIETY, like the craft
    # cutoff and the budget split, so the flag-off posture is unchanged.
    misses = [0] if config.cta_variety_enabled() else None
    for _ in range(_OVERCAP_MAX_ITER):
        if not _budget_left(deadline):
            break              # bounded: the nightly run always finishes
        if misses is not None and misses[0] >= _OVERCAP_LLM_FAILURE_CAP:
            break
        fixed, skipped = _overcap_pass(gym_id, rows, over_cats, store,
                                       caption_regen, avoid, log,
                                       deadline=deadline, misses=misses)
        total_fixed += fixed
        total_skipped += skipped
        over_cats = _over_cap_cats(rows)
        if not over_cats or fixed == 0:
            break
    return total_fixed, total_skipped


def _overcap_pass(gym_id, rows, over_cats, store, caption_regen, avoid, log,
                  deadline=None, misses=None):
    """One over-cap move wave: re-pillar excess wipeable days of each over-25%
    category by regenerating their caption from a DIFFERENT approved source
    category (the pillar label follows the caption that actually wrote the
    day). Latest days move first; a day with any human-owned row never moves;
    a move that would push the TARGET category itself over 25% is skipped so
    the loop converges instead of ping-ponging. Returns (days_fixed, days_skipped)."""
    from collections import Counter
    posts = _cat_posts(rows)
    n = len(posts) or 1
    counts = Counter(_cat_of(grp[0]) for _k, grp in posts)

    fixed = skipped = 0
    for cat in over_cats:
        excess = counts.get(cat, 0) - int(0.25 * n)
        if excess <= 0:
            continue
        mine = [((day, h), grp) for (day, h), grp in posts
                if _cat_of(grp[0]) == cat]
        for (day, _h), grp in sorted(mine, key=lambda kv: kv[0][0], reverse=True):
            if excess <= 0:
                break
            if any(not _is_wipeable(r) for r in grp):
                continue                       # human-owned day is never re-pointed
            if not _budget_left(deadline):
                break                          # bounded LLM spend, honest stop
            if misses is not None and misses[0] >= _OVERCAP_LLM_FAILURE_CAP:
                # This book's approved sources cannot move a day off this
                # category tonight. Stop paying for the answer and hand the rest
                # of the budget to the passes behind this one.
                log(f"{gym_id}: {misses[0]} over-cap regens in a row moved "
                    "nothing on this book; stopping over-cap LLM regens and "
                    "yielding the remaining budget")
                break
            out = None
            try:
                out = caption_regen(grp[0], avoid, cat)
            except Exception as exc:  # noqa: BLE001
                log(f"{gym_id} {day}: over-cap regen raised {type(exc).__name__}")
            if not out:
                skipped += 1
                if misses is not None:
                    misses[0] += 1
                continue
            new_cap, new_cat = out
            if not new_cat or str(new_cat).lower() == str(cat).lower():
                skipped += 1                   # the content does not support a move
                if misses is not None:
                    misses[0] += 1
                continue
            # Headroom in POST space, the same units the grader scores in: a
            # move that would push the TARGET over 25% trades one defect for
            # another and is never worth making.
            if (counts.get(new_cat, 0) + 1) / n > 0.25:
                skipped += 1                   # target has no headroom: honest skip
                if misses is not None:
                    misses[0] += 1
                continue
            if _patch_date_rows(gym_id, grp, store, new_cap, new_cat, log):
                fixed += 1
                excess -= 1
                counts[cat] -= 1
                counts[new_cat] = counts.get(new_cat, 0) + 1
                avoid.add(new_cap.strip())
                if misses is not None:
                    misses[0] = 0     # the move works here: never cut it off
            else:
                skipped += 1
                if misses is not None:
                    misses[0] += 1
    return fixed, skipped


# ---------------------------------------------------------------------------
# d) caption craft + path (booking asks)
# ---------------------------------------------------------------------------

def _ask_count(text) -> int:
    return len(list(copy_gate.ASK_RE.finditer(str(text or ""))))


def _craft_flags(caption):
    """The fixable craft flags this caption trips (grader's own soft flags)."""
    flags = set(copy_gate.soft_flags(caption or ""))
    return [f for f in _CRAFT_FLAGS if f in flags]


def _clears_craft(caption, allow_no_ask=False) -> bool:
    """The post-regen assertion: a fresh caption replaces a flagged one ONLY
    when it is strictly clean — exactly one ask, first line inside the hook
    band, length 150 to 500, zero hard violations, zero soft flags. Anything
    less and the row keeps its current caption (never swap in a worse one).

    allow_no_ask (AGENT_CTA_VARIETY, Dean Holcomb 2026-09-05): once the book
    already carries its booking-ask floor, a caption with NO ask is a legitimate
    caption and must be allowed to pass. Without this, `no_ask` in soft_flags
    made "every post ends in an ask" structurally unavoidable, which is the
    doctrine that produced 90 identical closing lines on Reverb's book. The rest
    of the bar is unchanged: an ask-less caption still has to be clean.
    """
    cap = (caption or "").strip()
    if not cap or copy_gate.violations(cap):
        return False
    asks = _ask_count(cap)
    if asks != 1 and not (allow_no_ask and asks == 0):
        return False
    first = cap.splitlines()[0].strip()
    if not first or len(first) > _HOOK_MAX:
        return False
    if not (_CAPTION_MIN <= len(cap) <= _CAPTION_MAX):
        return False
    flags = set(copy_gate.soft_flags(cap))
    if allow_no_ask:
        flags.discard("no_ask")
    if flags:
        return False
    return True


def _ask_target_posts(n_posts: int, pool_size=None) -> int:
    """How many POSTS of an `n_posts` book should carry a booking ask.

    `max(grader floor, share)`, then CAPPED BY WHAT THE GYM CAN HONESTLY SUPPLY:

      * the grader's path_to_join GYM leg wants `min(5, n)` posts carrying a
        booking-SPECIFIC term,
      * Blake's ask-rate target (config.caption_ask_rate_target, 0.33) is the
        share of the book that should close on an ask at all, and
      * caption_variety.honest_ask_ceiling is the most the gym's APPROVED CTA
        pool can cover without repeating a closing inside the window.

    THE CAP IS THE POINT (Blake, 2026-09-06: "No fake/generic/repeated CTA just
    to hit a target"). A gym with ONE approved CTA physically cannot ask more
    than once per window; asking it for a third of its book is asking it to
    repeat, which is exactly what Dean complained about. Measured on the live
    fleet at window 10, only 2 of 18 gyms have a ceiling at or above the 33%
    target. Targeting the unreachable number would mark 11 gyms down nightly
    for a defect no code can clear, and burn LLM budget re-attempting it.

    pool_size None means "not known here": the cap is not applied and the
    behavior is the pre-cap one. A caller that knows the pool should pass it.
    """
    if n_posts <= 0:
        return 0
    share = int(round(config.caption_ask_rate_target() * n_posts))
    want = min(n_posts, max(1, min(5, n_posts), share))
    if pool_size is None:
        return want
    ceiling = caption_variety.honest_ask_ceiling(
        n_posts, pool_size, config.caption_variety_window())
    return min(want, ceiling)


def _booking_deficit(rows, pool_size=None) -> int:
    """How many more booking asks the book still wants.

    FLAG OFF: rows, and a flat floor of `min(5, n)` -- byte for byte the
    pre-2026-09-06 behavior.

    AGENT_CTA_VARIETY ARMED: POSTS, and the target is `_ask_target_posts`.
    Counting POSTS is what makes the rate mean anything: one calendar post
    deliberately spans several rows (the IG feed, its Facebook mirror and the
    paired story share one caption on one date), so Reverb's 31 posts are 93
    rows. A share taken over ROWS would ask for 31 posts' worth of CTA on a
    31 post book -- 100%, the exact defect this is meant to end -- because the
    caller decrements this deficit once per POST repaired, not once per row.
    """
    if not config.cta_variety_enabled():
        n = len(rows)
        have = sum(1 for r in rows if _BOOKING_RE.search(r.get("caption") or ""))
        return max(0, min(5, n) - have)

    posts = {}
    for r in rows or []:
        key = (str(r.get("post_date") or "")[:10], caption_hash(r.get("caption") or ""))
        posts.setdefault(key, r.get("caption") or "")
    n = len(posts)
    have = sum(1 for cap in posts.values() if _BOOKING_RE.search(cap or ""))
    return max(0, _ask_target_posts(n, pool_size) - have)


def _booking_cta_pool(gym_id, log):
    """EVERY usable booking CTA the gym has approved, in rotation order.

    THE REVERB DEFECT (Dean Holcomb, ticket 4941e162, 2026-09-05). The old
    `_booking_cta_for` returned exactly ONE string and `_mechanical_repair`
    stapled it onto every craft-flagged day. Measured on his real book that was
    90 of 93 rows carrying the identical closing line. Worse, the line it chose
    was not a CTA at all: `min(candidates, key=len)` preferred the SHORTEST
    qualifying sentence, and the shortest thing in his approved sources that
    matched ASK_RE was an FAQ HEADING, "How do I get started with training at
    CrossFit Reverb?" (complete with the U+200B it was pasted in with). It
    contains the ask phrase "get started", so ASK_RE said yes. It is a question
    the reader is asked, not an instruction, which is exactly why Dean said it
    "doesn't make sense".

    Two changes, both of which need AGENT_CTA_VARIETY armed:
      * every candidate now passes copy_gate.is_cta_shaped, which rejects
        questions and headings. The Reverb line is rejected by name;
      * a POOL is returned instead of one string, so callers can rotate. The
        auto-generated bible's "### CTA rotation (cycle in order, one per post)"
        section has promised rotation since day one and no code ever delivered it.

    Ordering: the voice-doc rotation first, in the order the gym wrote it (that
    IS the gym's stated preference), then verbatim booking-ask sentences from
    approved sources, shortest first. Everything here is copy the gym already
    approved; nothing is invented. A gym with no usable CTA gets an empty pool
    and an honest skip, never a fabricated ask.
    """
    strict = config.cta_variety_enabled()
    pool, seen = [], set()

    def _offer(text):
        t = copy_gate.scrub(str(text or "")).strip()
        if not t or t.lower() in seen:
            return
        if not (_BOOKING_RE.search(t) and copy_gate.ASK_RE.search(t)):
            return
        if strict:
            defects = copy_gate.cta_defects(t)
            if defects:
                log(f"{gym_id}: CTA candidate rejected ({','.join(defects)}): {t[:70]}")
                return
        seen.add(t.lower())
        pool.append(t)

    try:
        from agent.client_media_sync import (_account_for_base,
                                             _resolve_client_voice_path)
        from agent.voice import load_voice
        account = _account_for_base(gym_id)
        if account is not None:
            voice = load_voice(
                _resolve_client_voice_path(gym_id, account.voice_doc_path()))
            for cta in (getattr(voice, "ctas", None) or []):
                _offer(cta)
        import re as _re
        from agent import client_sources as _cs
        extras = []
        for src in _cs.approved_sources(f"{gym_id}_ig"):
            for sent in _re.split(r"(?<=[.!?])\s+", str(src.text or "")):
                text = copy_gate.scrub(sent).strip()
                if text and len(text) <= 120:
                    extras.append(text)
        for text in sorted(extras, key=len):
            _offer(text)
    except Exception as exc:  # noqa: BLE001 - the bias is best effort
        log(f"{gym_id}: booking CTA unavailable: {type(exc).__name__}")
    return pool


def _booking_cta_for(gym_id, log):
    """The gym's REAL booking CTA: the first CTA in its approved voice-doc
    rotation that is both a booking term and a recognized ask; when the bible's
    CTA section is empty (a hand-fill onboarding TODO — ENG 2026-08-31), a
    VERBATIM booking-ask sentence from the gym's own APPROVED sources. Both are
    approved copy; a gym with neither gets an honest skip (never an invented CTA).

    Single-CTA compatibility shim. With AGENT_CTA_VARIETY armed this is the
    HEAD of _booking_cta_pool, so it inherits the is_cta_shaped filter and a
    heading can no longer be chosen. Callers that want variety must use the
    pool; this one string is what the pre-2026-09-05 callers expect.
    """
    if config.cta_variety_enabled():
        pool = _booking_cta_pool(gym_id, log)
        return pool[0] if pool else None
    try:
        from agent.client_media_sync import (_account_for_base,
                                             _resolve_client_voice_path)
        from agent.voice import load_voice
        account = _account_for_base(gym_id)
        if account is not None:
            voice = load_voice(
                _resolve_client_voice_path(gym_id, account.voice_doc_path()))
            for cta in (getattr(voice, "ctas", None) or []):
                text = copy_gate.scrub(str(cta or "")).strip()
                if text and _BOOKING_RE.search(text) and copy_gate.ASK_RE.search(text):
                    return text
        # BIBLE CTA SECTION EMPTY (ENG 2026-08-31: onboarding left '### CTA rotation'
        # as a hand-fill TODO, so every no_ask repair had nothing approved to carry
        # and 0/56 flagged days could clear). Fall back to the gym's own APPROVED
        # client sources: a VERBATIM sentence that is already a booking ask (booking
        # term + ask shape) is approved copy by definition — zero fabrication, same
        # bar as the voice-doc rotation. Shortest qualifying sentence wins (a CTA
        # should be punchy); nothing qualifying keeps the honest None.
        import re as _re
        from agent import client_sources as _cs
        candidates = []
        for src in _cs.approved_sources(f"{gym_id}_ig"):
            for sent in _re.split(r"(?<=[.!?])\s+", str(src.text or "")):
                text = copy_gate.scrub(sent).strip()
                if (text and len(text) <= 120
                        and _BOOKING_RE.search(text)
                        and copy_gate.ASK_RE.search(text)):
                    candidates.append(text)
        if candidates:
            return min(candidates, key=len)
    except Exception as exc:  # noqa: BLE001 - the bias is best effort
        log(f"{gym_id}: booking CTA unavailable: {type(exc).__name__}")
    return None


def _mechanical_repair(caption, cta):
    """Deterministic, ZERO-FABRICATION repair of the fixable craft dimensions
    (2026-08-31: the LLM regen cleared 0 of ENG's 56 flagged days — it kept missing
    the all-at-once bar — so mechanics now fix what mechanics can):

      * hook_too_long: re-lineate the first line at its first sentence boundary so the
        hook fits the band — NOT ONE WORD changes, only a line break moves;
      * no_ask: append the gym's APPROVED booking CTA as the caption's single ask.

    thin_caption needs real content and stays the regen's job. Returns the repaired
    caption, or None when mechanics cannot help (an unbreakable first sentence)."""
    import re as _re
    cap = (caption or "").strip()
    if not cap:
        return None
    lines = cap.splitlines()
    first = lines[0].strip()
    if len(first) > _HOOK_MAX:
        m = _re.match(r"^(.{10,%d}?[.!?])\s+(\S.*)$" % _HOOK_MAX, first)
        if not m:
            return None
        rest = "\n".join([m.group(2)] + [ln for ln in lines[1:]]).strip()
        cap = f"{m.group(1)}\n\n{rest}"
    if _ask_count(cap) == 0 and (cta or "").strip():
        cap = f"{cap}\n{cta.strip()}"
    return cap.strip()


def _fix_craft(gym_id, rows, store, profile, caption_regen, avoid, log,
               booking_cta=None, deadline=None):
    """Repair the caption of every fully wipeable day that trips a craft flag
    (no_ask / thin_caption / hook_too_long), on the SAME photo. A fresh caption
    lands only when it clears _clears_craft; otherwise the day keeps its caption
    (tracked as attempted-but-not-fixed). While the book is short of booking-term
    asks, a repaired day whose caption carries no ask gets the gym's REAL booking
    CTA appended as its single ask (approved copy only). Days that already pass
    are never touched.

    CANDIDATE ORDER (2026-08-31, the convergence fix). The MECHANICAL repair is
    tried FIRST and the LLM regen second. Mechanics costs nothing, fabricates
    nothing (it only moves a line break and appends the gym's own approved CTA),
    and measured live it clears the bar for 30/30 gritx, 28/29 hillcountry,
    20/20 zanshin and 30/30 reverb posts. The old order paid 6 to 8 seconds of
    LLM per post BEFORE trying it, which is what stopped the nightly pass from
    ever finishing. The LLM now runs only where mechanics genuinely cannot help
    (chiefly thin_caption, which needs real content), and only while the pass
    still has wall-clock budget.

    B2B (LASSO) gets the MECHANICAL lane too: re-lineating a hook and appending
    LASSO's own approved booking CTA is zero-fabrication and honest for any
    profile. Only the LLM regen stays GYM-only, because a B2B caption's content
    is owned by the pillar builders.
    Returns (days_fixed, days_attempted, booking_asks_added)."""
    llm_ok = caption_regen is not None and profile != "B2B"
    variety = config.cta_variety_enabled()

    cta_pool = []
    if variety:
        # The whole approved pool, so the append can ROTATE instead of stapling
        # one line onto every day (Dean Holcomb, Reverb, 2026-09-05). Resolved
        # BEFORE the deficit now, because the deficit is capped by what this
        # pool can honestly cover without repeating inside the window.
        cta_pool = _booking_cta_pool(gym_id, log)
        if booking_cta is not None and booking_cta not in cta_pool:
            cta_pool = [booking_cta] + cta_pool
        booking_cta = cta_pool[0] if cta_pool else None
    elif booking_cta is None:
        booking_cta = _booking_cta_for(gym_id, log)

    deficit = _booking_deficit(rows, len(cta_pool) if variety else None)

    # The book's closing line PER DATE, kept in date order. The anti-repetition
    # rule is "no two posts inside the window share a closing", NOT "never
    # repeat": a gym with four approved CTAs must still be able to fill a
    # 31 day book and clear the grader's 5 booking-ask floor. A global
    # never-repeat rule caps the book at pool-size asks, which measured on
    # Reverb's real pool of four left the book one ask short of an A.
    window = config.caption_variety_window()
    closing_by_date = {}
    if variety:
        for r in rows:
            dk = str(r.get("post_date") or "")[:10]
            sig = caption_variety.closing_signature(r.get("caption") or "")
            if dk and sig:
                closing_by_date[dk] = sig

    def _recent_closings(day_key):
        """Closings on the `window` dated posts immediately before day_key."""
        earlier = sorted(k for k in closing_by_date if k < day_key)
        return {closing_by_date[k] for k in earlier[-window:]}

    # One post spans same-date rows sharing a caption (IG feed + FB mirror +
    # paired story); group by (date, hash) so the day moves together and a 2x
    # day's two distinct posts stay independent.
    groups: dict = {}
    for r in rows:
        d = str(r.get("post_date") or "")[:10]
        h = caption_hash(r.get("caption") or "")
        groups.setdefault((d, h), []).append(r)

    fixed = attempted = booking_added = 0
    day_index = -1
    # The consecutive-failure cutoff (see _CRAFT_LLM_FAILURE_CAP). Counts only
    # LLM regens: a mechanical win neither advances nor resets it, because a
    # mechanical win is not evidence about the regen.
    llm_misses = 0
    llm_conceded = False
    for (d, _h), grp in sorted(groups.items()):
        if any(not _is_wipeable(r) for r in grp):
            continue                            # human-owned day: never touched
        cap = grp[0].get("caption") or ""
        if not cap.strip():
            continue        # caption-less story / GBP photo post: nothing to craft
        if not _craft_flags(cap):
            continue                            # already passes: never touched
        attempted += 1
        day_index += 1

        # THE ASK-RATE GATE. Append a booking CTA only while the book is still
        # short of its booking-ask floor. Once the floor is met, this day's
        # repair carries no ask and `allow_no_ask` lets it pass the craft bar.
        # Before this gate every flagged day got the CTA, which is how one line
        # ended up on 90 of 93 rows.
        day_cta = booking_cta
        allow_no_ask = False
        if variety:
            if deficit > 0 and cta_pool:
                # Pool order is the gym's own stated preference (its bible's CTA
                # rotation, in the order it wrote it). No index rotation is
                # applied on top: pick_non_colliding already guarantees the
                # window has no repeat, and a rotation offset was measured to
                # change nothing on top of that. Unasserted no-op code is worse
                # than no code.
                try:
                    day_cta = caption_variety.pick_non_colliding(
                        cta_pool, _recent_closings(d))
                except caption_variety.NoOptionAvailable:
                    # Every approved CTA already closes a post inside the
                    # window. Leaving this post ask-less is the honest outcome;
                    # repeating one is what Dean complained about.
                    day_cta = None
                    allow_no_ask = True
            else:
                day_cta = None
                allow_no_ask = True

        # --- candidate 1: the free, deterministic, zero-fabrication repair ---
        repaired = _mechanical_repair(cap, day_cta)
        candidates = []
        if repaired and repaired != cap:
            candidates.append((repaired, None,
                               _ask_count(cap) == 0 and _ask_count(repaired) == 1))
        winner = next(((c, cat, cta_used) for c, cat, cta_used in candidates
                       if c and c not in avoid
                       and _clears_craft(c, allow_no_ask=allow_no_ask)), None)

        # --- candidate 2: the LLM regen, only when mechanics could not help ---
        if (winner is None and llm_ok and not llm_conceded
                and _budget_left(deadline)):
            out = None
            try:
                out = caption_regen(grp[0], avoid, "")
            except Exception as exc:  # noqa: BLE001
                log(f"{gym_id} {d}: craft regen raised {type(exc).__name__}")
            if out:
                regen_cap, new_cat = out
                regen_cap = (regen_cap or "").strip()
                carried_cta = False
                if (deficit > 0 and day_cta
                        and not _BOOKING_RE.search(regen_cap)
                        and _ask_count(regen_cap) == 0):
                    # The already-flagged day is the honest place to carry the
                    # gym's real booking CTA: it becomes the caption's single ask.
                    regen_cap = f"{regen_cap}\n{day_cta}".strip()
                    carried_cta = True
                if (regen_cap and regen_cap not in avoid
                        and _clears_craft(regen_cap, allow_no_ask=allow_no_ask)):
                    winner = (regen_cap, new_cat, carried_cta)
            if winner is None:
                llm_misses += 1
                if variety and llm_misses >= _CRAFT_LLM_FAILURE_CAP:
                    # This book's material cannot clear the craft bar through
                    # the regen. Stop paying 6 to 8 seconds a call for it and
                    # hand the rest of the budget to the passes after this one;
                    # mechanics keeps running on every remaining day.
                    llm_conceded = True
                    log(f"{gym_id}: {llm_misses} craft regens in a row cleared "
                        "nothing on this book; stopping craft LLM regens and "
                        "yielding the remaining budget (mechanical repair "
                        "continues)")
            else:
                llm_misses = 0        # the regen works here: never cut it off

        if winner is None:
            log(f"{gym_id} {d}: neither the mechanically repaired nor the "
                "regenerated caption clears the craft bar; keeping the current "
                "caption")
            continue
        new_cap, new_cat, carried_cta = winner
        if _patch_date_rows(gym_id, grp, store, new_cap, new_cat or None, log):
            fixed += 1
            avoid.add(new_cap)
            if variety:
                # This day's closing line is now spoken for, so no post inside
                # the window after it may reuse it. THE mechanical
                # anti-repetition guarantee, and the thing nothing in the
                # codebase did before: no code looked at a caption's tail.
                sig = caption_variety.closing_signature(new_cap)
                if sig:
                    closing_by_date[d] = sig
            if _BOOKING_RE.search(new_cap):
                # Unflagged: rows, as _booking_deficit has always counted them.
                # Under variety: POSTS, which is what calendar_grade._path
                # actually scores (it groups same-date rows into one post), so
                # the ask floor is reached at the same point the grader wants it.
                deficit = max(0, deficit - (1 if variety else len(grp)))
                if carried_cta:
                    booking_added += 1
    return fixed, attempted, booking_added


# ---------------------------------------------------------------------------
# e) off-avatar hooks (right_audience)
# ---------------------------------------------------------------------------

def _fix_invalid_closings(gym_id, rows, store, log):
    """Strip a closing line that was never a valid ask, from EVERY post carrying
    it, regardless of the book's ask rate.

    BLAKE'S RULING, 2026-09-06: *"If a gym's CTA pool is empty or too thin to
    supply a real ask, DO NOT force one in. No fake/generic/repeated CTA just to
    hit a target. On those gyms, write the caption with no booking ask at all --
    good copy, no ask -- rather than degrade quality or repeat the same CTA to
    hit 33%."*

    That ruling corrected this module's own first answer, and the correction is
    worth stating plainly because it is easy to get backwards. `_fix_ask_excess`
    trims a book DOWN TO the target, which on Dean Holcomb's book meant KEEPING
    the nonsensical FAQ heading on ten posts in order to reach 33%. Hitting the
    number by preserving copy the client complained about is exactly the
    "degrade quality to hit a target" Blake ruled out. A line that is not a
    valid CTA is not a partial ask to be rationed. It is wrong copy, and it goes
    from every post that carries it.

    WHAT COUNTS AS INVALID. `copy_gate.cta_defects` decides, not this function:
    a question, an interrogative opener, an over-long heading. Dean's line --
    "How do I get started with training at CrossFit Reverb?", complete with the
    U+200B it was pasted in with -- trips `cta_is_question`. It reached 90 of his
    93 rows because the OLD selector asked only "does this contain an ask
    phrase", and "get started" is an ask phrase.

    WHAT IS PROTECTED. Same bar as every other repair here: flag armed, wipeable
    row only, the line must be the caption's LAST line, it must currently read as
    an ask (so ordinary body copy is never mistaken for a CTA), and the remainder
    must still clear the craft bar. A caption that would be left worse keeps its
    line.

    WHY IT RUNS FIRST. The ask DEFICIT must be counted against REAL asks. Left
    in place, an invalid closing makes the book look like it is already asking,
    which is precisely why `_fix_craft` had nothing to grip on Dean's book: 30 of
    31 posts "had an ask" and only 1 day was flagged.

    Returns the number of POSTS cleaned.
    """
    if not config.cta_variety_enabled():
        return 0

    groups: dict = {}
    for r in rows or []:
        d = str(r.get("post_date") or "")[:10]
        cap = r.get("caption") or ""
        if not d or not str(cap).strip():
            continue
        groups.setdefault((d, caption_hash(cap)), []).append(r)

    cleaned = 0
    for key in sorted(groups):
        grp = groups[key]
        if any(not _is_wipeable(r) for r in grp):
            continue                                # human-owned day: never touched
        cap = grp[0].get("caption") or ""
        lines = list(cap.splitlines())
        idx = max((i for i, ln in enumerate(lines) if ln.strip()), default=-1)
        if idx < 0:
            continue
        last = lines[idx]
        if not copy_gate.ASK_RE.search(last):
            continue                    # not read as an ask: ordinary body copy
        if copy_gate.is_cta_shaped(last):
            continue                    # a real CTA: the rate rules own it, not this
        candidate = "\n".join(lines[:idx]).rstrip()
        if not _clears_craft(candidate, allow_no_ask=True):
            continue                    # removing it would leave a worse caption
        if _patch_date_rows(gym_id, grp, store, candidate, None, log):
            cleaned += 1
    if cleaned:
        log(f"{gym_id}: removed a closing line that is not a valid CTA from "
            f"{cleaned} post(s) (copy_gate.cta_defects rejected it)")
    return cleaned


def _fix_ask_excess(gym_id, rows, store, log):
    """Remove the STAPLED closing ask from a book that carries far too many.

    WHY THIS EXISTS. `_fix_craft` sizes how many asks get ADDED, and that alone
    cannot deliver a 33% book, because it only ever touches a craft-FLAGGED day
    and a caption that already ends in an ask is not flagged. Measured on Dean
    Holcomb's live book (crossfitreverb30b5b2) the morning after the CTA fix
    shipped: 30 of 31 posts (96.8%) still closed on the same line, exactly 1 post
    was craft-flagged, and the repair loop therefore had nothing to grip. His
    ticket said the captions "all end with 'How do I get started with training at
    CrossFit Reverb?' which doesn't make sense" and that was still true.

    The fleet's own engagement data agrees, independently: the cross gym rollup
    run on 2026-09-06 over 90 posts across 5 gyms found ONE result that survived
    Benjamini Hochberg (q = 0.0296, Cohen's d = 0.49), and it was that feed posts
    WITHOUT an ask outperform feed posts WITH one.

    WHAT IT WILL AND WILL NOT DO. This pass only ever DELETES a line the machine
    previously stapled on. It writes nothing, invents nothing, and rewrites no
    body copy. Every one of these must hold before a single line is removed:

      1. AGENT_CTA_VARIETY is armed (this whole rail is behind it).
      2. The book is genuinely ABOVE its ask target. It stops the moment the
         target is reached and can never take a book below it.
      3. The row is wipeable -- a machine draft, never a human-owned day.
      4. The line removed is the caption's LAST line and is ITSELF an ask. A
         caption whose ask is woven into the body is left completely alone.
      5. That closing is REPEATED elsewhere in the book. A bespoke closing that
         appears once is the gym's own voice, not a staple, and is never touched.
      6. What remains still clears the craft bar (length, hook, zero violations,
         zero other soft flags). A trim that would leave a worse caption is
         skipped, exactly like every other repair here.

    Deterministic: the most-repeated closing is trimmed first, then by date, so
    the same book trims identically and a diff stays reviewable.

    Returns the number of POSTS trimmed.
    """
    if not config.cta_variety_enabled():
        return 0

    groups: dict = {}
    for r in rows or []:
        d = str(r.get("post_date") or "")[:10]
        cap = r.get("caption") or ""
        if not d or not str(cap).strip():
            continue
        groups.setdefault((d, caption_hash(cap)), []).append(r)
    if not groups:
        return 0

    n = len(groups)
    target = _ask_target_posts(n)
    asking = [k for k, grp in groups.items()
              if copy_gate.ASK_RE.search(grp[0].get("caption") or "")]
    excess = len(asking) - target
    if excess <= 0:
        return 0

    # How often each closing line appears across the book. A staple repeats; a
    # gym's own bespoke sign-off does not.
    closing_counts: dict = {}
    for k, grp in groups.items():
        sig = caption_variety.closing_signature(grp[0].get("caption") or "")
        if sig:
            closing_counts[sig] = closing_counts.get(sig, 0) + 1

    def _rank(key):
        grp = groups[key]
        sig = caption_variety.closing_signature(grp[0].get("caption") or "")
        return (-closing_counts.get(sig, 0), key[0])

    trimmed = 0
    for key in sorted(asking, key=_rank):
        if trimmed >= excess:
            break
        grp = groups[key]
        if any(not _is_wipeable(r) for r in grp):
            continue                                # human-owned day: never touched
        cap = grp[0].get("caption") or ""
        lines = [ln for ln in cap.splitlines()]
        idx = max((i for i, ln in enumerate(lines) if ln.strip()), default=-1)
        if idx < 0:
            continue
        last = lines[idx]
        if not copy_gate.ASK_RE.search(last):
            continue                                # the ask is in the body: leave it
        sig = caption_variety.closing_signature(cap)
        if closing_counts.get(sig, 0) < 2:
            continue                                # appears once: the gym's own voice
        candidate = "\n".join(lines[:idx]).rstrip()
        if not _clears_craft(candidate, allow_no_ask=True):
            continue                                # trimming would leave it worse
        if _patch_date_rows(gym_id, grp, store, candidate, None, log):
            trimmed += 1
            closing_counts[sig] = closing_counts.get(sig, 1) - 1
    if trimmed:
        log(f"{gym_id}: trimmed the stapled closing ask off {trimmed} post(s); "
            f"book was {len(asking)}/{n} asking, target {target}")
    return trimmed


def _fix_audience(gym_id, rows, store, profile, caption_regen, avoid, log,
                  deadline=None):
    """Rewrite the caption of every fully wipeable day whose HOOK leaks the
    wrong avatar (the grader's right_audience leg: competitive-athlete language
    for a GYM book, elite/advanced language anywhere).

    Added 2026-08-31: this defect class had NO repair path at all, so a book
    could sit one caption short of an A forever. There is no mechanical fix —
    the hook has to say something different — so this is the one pass that must
    use the regen, and it runs under the same wall-clock budget. A gym without a
    regen context is an honest skip. Returns (days_fixed, days_attempted)."""
    from agent.calendar_grade import _ATHLETE_WORDS, _ELITE_WORDS
    if caption_regen is None:
        return 0, 0

    def _leaks(caption):
        cap = (caption or "").strip()
        if not cap:
            return False
        first = cap.splitlines()[0]
        if profile != "B2B" and _ATHLETE_WORDS.search(first):
            return True
        return bool(_ELITE_WORDS.search(first))

    groups: dict = {}
    for r in rows:
        d = str(r.get("post_date") or "")[:10]
        h = caption_hash(r.get("caption") or "")
        groups.setdefault((d, h), []).append(r)

    fixed = attempted = 0
    for (d, _h), grp in sorted(groups.items()):
        if any(not _is_wipeable(r) for r in grp):
            continue
        cap = grp[0].get("caption") or ""
        if not _leaks(cap):
            continue
        if not _budget_left(deadline):
            break
        attempted += 1
        out = None
        try:
            out = caption_regen(grp[0], avoid, "")
        except Exception as exc:  # noqa: BLE001
            log(f"{gym_id} {d}: audience regen raised {type(exc).__name__}")
        if not out:
            continue
        new_cap, new_cat = out
        new_cap = (new_cap or "").strip()
        # Replace ONLY with a caption that is both on-avatar and craft-clean:
        # never trade an off-avatar hook for a worse caption.
        if not new_cap or new_cap in avoid or _leaks(new_cap):
            log(f"{gym_id} {d}: the regenerated hook still leaks the wrong "
                "avatar; keeping the current caption")
            continue
        if not _clears_craft(new_cap):
            continue
        if _patch_date_rows(gym_id, grp, store, new_cap, new_cat or None, log):
            fixed += 1
            avoid.add(new_cap)
    return fixed, attempted


# ---------------------------------------------------------------------------
# g) body sameness — the same template in the MIDDLE of two posts
# ---------------------------------------------------------------------------

def _body_posts(rows):
    """The book as (date, caption) POST groups in date order.

    Same grouping every other pass here uses: one calendar post deliberately
    spans several rows (the IG feed, its Facebook mirror and the paired story
    share one caption on one date), and a 2x day's two distinct captions stay
    two independent posts. Rows with no caption (a caption-less story / GBP
    photo post) have no body to compare and are left out entirely."""
    groups: dict = {}
    for r in rows or []:
        d = str(r.get("post_date") or "")[:10]
        cap = r.get("caption") or ""
        if not d or not str(cap).strip():
            continue
        groups.setdefault((d, caption_hash(cap)), []).append(r)
    return [(key, groups[key]) for key in sorted(groups)]


def _body_collides(caption, others, threshold) -> bool:
    """True when this caption's BODY reads as the same template as any of
    `others` (already-normalized comparison lives in caption_variety)."""
    return any(caption_variety.body_similarity(caption, other) >= threshold
               for other in others)


def _fix_body_sameness(gym_id, rows, store, caption_regen, avoid, log,
                       deadline=None, threshold=None):
    """Regenerate the LATER post of every near-duplicate BODY pair.

    BLAKE'S RULING, 2026-09-06: *"Build the actual body-copy repair, not just
    detection ... build the repair path that acts when a post's body copy is a
    near-duplicate of another post in the same gym's book (not just the closing
    line, the actual middle content/structure). Same rules as before apply: no
    forced/fake content, real repair only, don't touch gyms where the pool /
    source material can't support a genuinely different post, flag those
    honestly rather than papering over them."*

    WHY THIS IS A REGEN AND NOT A TRIM. `_fix_ask_excess` and
    `_fix_invalid_closings` only ever DELETE a line, which is the honest repair
    for a stapled closing. There is no line you can delete to make a caption's
    MIDDLE different; the middle is the post. So this pass is the duplicate
    lane's repair (a fresh caption from the gym's own approved sources, through
    the same injectable `caption_regen`, on the SAME photo), aimed at a defect
    `_fix_duplicates` cannot see: those two captions are not identical, they are
    merely the same template.

    SCOPE: BOOK-WIDE, not the 10-post anti-repetition window. This is a
    deliberate departure from the other collision kinds and it follows
    `caption_variety.report()`, whose body-similarity metric is book-wide for a
    stated reason: "a template can recur outside the anti-repetition window and
    a client reviewing a full month still sees it." Measured on the real fleet
    the same morning this shipped, both scopes are needed and the window alone
    would miss real repeats -- train7164ae502 repeats "You've tried the big box
    gyms." on 2026-09-18 and 2026-09-30 (12 posts apart, OUTSIDE the window,
    body similarity 1.0), and gritx repeats a whole caption verbatim 2026-08-27
    -> 2026-10-01 (30+ posts apart). O(n^2) over a month's book is ~800 string
    comparisons and costs nothing; the LLM spend is what is bounded, by the same
    `deadline` every other regen pass here shares.

    WHICH SIDE MOVES: THE LATER DATE, always. Three reasons, in order of
    weight: (1) it is the rule `_fix_duplicates` already uses -- the earliest
    date keeps its caption -- and one file should not hold two different answers
    to the same question; (2) the earlier post is nearer to publishing, so it
    has had the longest exposure to human review and is the more expensive one
    to churn; (3) it makes the pass deterministic, so the same book repairs
    identically and a diff stays reviewable.

    CHAINS OF THREE OR MORE. Each post is considered for regeneration AT MOST
    ONCE, as the later side. train7164ae502's real book has six posts opening
    "You've tried solo workouts and ..."; naive pairwise repair would rewrite
    the same day up to five times. Instead the earliest member of a chain is
    the anchor and each later member gets one attempt, in date order, checked
    against the book AS REPAIRED SO FAR -- so a repair that already broke the
    chain spares the days after it (re-checked, not assumed).

    WHAT LANDS. A fresh caption is written only when it is ALL of: non-empty,
    not already on the book (`avoid`), craft-clean (`_clears_craft`, ask-less
    allowed -- the ask-rate rail owns the closing line, not this pass), and
    genuinely different: BELOW the threshold against every other body in the
    book, including the ones this pass has already written. Anything short of
    that and the day KEEPS ITS CURRENT CAPTION and is counted
    `body_unrepairable` -- the gym's approved sources cannot honestly supply a
    different post today, and that is a content-supply fact to report, not a
    defect to paper over with a forced difference.

    Returns (pairs_found, days_fixed, days_unrepairable)."""
    if not config.cta_variety_enabled():
        return 0, 0, 0

    thresh = (caption_variety.BODY_SIMILARITY_THRESHOLD if threshold is None
              else float(threshold))
    posts = _body_posts(rows)
    caps = [grp[0].get("caption") or "" for _key, grp in posts]

    # The pairs as FOUND, before anything is repaired, counted exactly the way
    # caption_variety.report()["body_similarity"]["pairs_over_threshold"] counts
    # them, so the before/after numbers are directly comparable.
    later_sides, pairs_found = [], 0
    for i in range(len(caps)):
        for j in range(i + 1, len(caps)):
            if caption_variety.body_similarity(caps[i], caps[j]) >= thresh:
                pairs_found += 1
                if j not in later_sides:
                    later_sides.append(j)      # the LATER date is the one that moves
    if not pairs_found:
        return 0, 0, 0

    # THE CONTENT-MIX HEADROOM GUARD (2026-09-07). A fresh caption brings its own
    # pillar, and this pass writes that pillar onto the day -- so a body repair
    # MOVES a post between categories exactly as `_overcap_pass` does, and can
    # push the target over the grader's 25% cap. Measured on topfuel's live book
    # the day the repair first fired: body_max 1.0 -> 0.087 and pairs 4 -> 0, but
    # the content_mix leg went 20 -> 17 ('offer is 26% of posts') and the book
    # went 91 (A) -> 87 (B). `_cat_posts` already carries the ruling this breaks:
    # "A repair must never be able to lower the grade." So this pass now uses the
    # same headroom test the over-cap pass uses, in the same POST units the
    # grader counts in, and a repair that would breach the cap is not made.
    from collections import Counter

    from agent.calendar_grade import _MIX_CAP_MIN_POSTS
    cat_counts = Counter(_cat_of(grp[0]) for _k, grp in _cat_posts(rows))
    n_cat_posts = sum(cat_counts.values()) or 1

    def _cat_headroom(old_cat, new_cat) -> bool:
        """True when writing `new_cat` onto a day that currently reads `old_cat`
        keeps the target category at or under the grader's 25% cap.

        Two ways to be fine. Staying in the same category never moves the counts.
        And a book BELOW `calendar_grade._MIX_CAP_MIN_POSTS` is exempt from the
        cap in the grader itself, so guarding against it here would refuse real
        repairs to avoid a defect that is not measured: on a 4 post book the cap
        allows ONE post per pillar, which no repair could ever satisfy. The guard
        mirrors the grader exactly, or it is not a guard, it is a second opinion.
        """
        if not new_cat or str(new_cat).lower() == str(old_cat or "").lower():
            return True
        if n_cat_posts < _MIX_CAP_MIN_POSTS:
            return True
        return (cat_counts.get(new_cat, 0) + 1) / n_cat_posts <= 0.25

    fixed = unrepairable = 0
    for j in sorted(later_sides):
        (day, _h), grp = posts[j]
        others = [caps[k] for k in range(len(caps)) if k != j]
        if not _body_collides(caps[j], others, thresh):
            continue                # an earlier repair already broke this chain
        if any(not _is_wipeable(r) for r in grp):
            # Floor 1: a human approved this caption. Echo does not rewrite it.
            log(f"{gym_id} {day}: body reads as a repeat of another post but the "
                "day is human owned; left in place")
            continue
        if caption_regen is None:
            unrepairable += 1
            log(f"{gym_id} {day}: body reads as a repeat of another post and no "
                "caption regen context is available for this gym; left in place")
            continue
        if not _budget_left(deadline):
            # Bounded LLM spend, honest stop: the nightly sweep always finishes.
            log(f"{gym_id} {day}: body repair stopped, the pass is out of LLM "
                "budget; remaining near-duplicate bodies left in place")
            break
        out = None
        try:
            out = caption_regen(grp[0], avoid, "")
        except Exception as exc:  # noqa: BLE001 - remediation is best effort
            log(f"{gym_id} {day}: body regen raised {type(exc).__name__}")
        new_cap = ((out or (None, None))[0] or "").strip()
        new_cat = (out or (None, None))[1] if out else None
        # THE HOOK RE-LINEATION (2026-09-07). `_mechanical_repair` with no CTA
        # moves exactly ONE line break: it re-lineates an over-long first line at
        # its first sentence boundary and writes not a single word. `_fix_craft`
        # has trusted it for precisely this since 2026-08-31. Without it this
        # pass threw away good captions over a formatting detail it already owns
        # the fix for: measured on hillcountry's live book, 15 of 16 fresh drafts
        # cleared every CONTENT bar and were rejected on `hook_too_long` alone.
        if new_cap and not _clears_craft(new_cap, allow_no_ask=True):
            relined = _mechanical_repair(new_cap, None)
            if relined and _clears_craft(relined, allow_no_ask=True):
                new_cap = relined
        if not new_cap or new_cap in avoid:
            unrepairable += 1
            log(f"{gym_id} {day}: no fresh caption could be built for the "
                "near-duplicate body; left in place (source material too thin)")
            continue
        if _body_collides(new_cap, others, thresh):
            unrepairable += 1
            log(f"{gym_id} {day}: the regenerated caption's body still reads as "
                "the same template as another post; left in place rather than "
                "forcing a difference the gym's sources cannot support")
            continue
        if not _clears_craft(new_cap, allow_no_ask=True):
            unrepairable += 1
            log(f"{gym_id} {day}: the regenerated caption does not clear the "
                "craft bar; keeping the current caption")
            continue
        old_cat = _cat_of(grp[0])
        if not _cat_headroom(old_cat, new_cat):
            unrepairable += 1
            log(f"{gym_id} {day}: the fresh caption's pillar ({new_cat}) is "
                "already at the 25% content-mix cap; keeping the current "
                "caption rather than trading a repeated body for an over-cap "
                "category")
            continue
        if _patch_date_rows(gym_id, grp, store, new_cap, new_cat or None, log):
            fixed += 1
            avoid.add(new_cap)
            caps[j] = new_cap          # later chain members re-check against this
            if new_cat and str(new_cat).lower() != str(old_cat or "").lower():
                # Keep the running mix honest for the days still to be judged.
                cat_counts[new_cat] = cat_counts.get(new_cat, 0) + 1
                if old_cat:
                    cat_counts[old_cat] = max(0, cat_counts.get(old_cat, 0) - 1)
        else:
            unrepairable += 1
    if fixed or unrepairable:
        log(f"{gym_id}: body sameness, {pairs_found} pair(s) at or above "
            f"{thresh}; rewrote {fixed}, {unrepairable} could not be honestly "
            "made different")
    return pairs_found, fixed, unrepairable


# ---------------------------------------------------------------------------
# Shared patch primitive
# ---------------------------------------------------------------------------

def _patch_date_rows(gym_id, date_rows, store, new_cap, new_cat, log) -> bool:
    """PATCH every wipeable row of one date group (the IG feed, its FB mirror,
    and the paired story deliberately share one caption, so the whole day moves
    together and stays ONE post). Non-wipeable rows are skipped here AND blocked
    server-side by patch_pending_plan. Returns True when at least one row was
    patched. Mutates the local row dicts to match so the caller's regrade sees
    the fix even before a store re-read."""
    patcher = getattr(store, "patch_pending_plan", None)
    if patcher is None:
        return False

    # RE-STAMP THE LEARNING LEVERS against the caption we are actually writing.
    # They were stamped at stage time against the pre-repair text and nothing
    # ever corrected them: measured on Reverb's live book, ask_type='none' on
    # 93 of 93 rows while 90 ended in an ask, and caption_len_band='mid' on
    # 100%. metrics_sync copies these onto post_metrics and monthly_retro
    # compares on them, so a stale lever is a lie the learner trains on.
    levers = _restamp_levers(new_cap)

    patched_any = False
    for r in date_rows:
        if not _is_wipeable(r):
            continue                            # never touched, by policy
        try:
            kwargs = {"caption": new_cap, "pillar": (new_cat or None)}
            if levers:
                kwargs["levers"] = levers
            updated = patcher(gym_id, r.get("id"), **kwargs)
        except TypeError:
            # A store predating the levers kwarg (older fakes, other callers).
            # The caption fix must never be lost over a metadata refresh.
            #
            # THE RETRY NEEDS ITS OWN GUARD (2026-09-06). This call used to sit
            # bare inside the handler, so anything it raised escaped the loop
            # ENTIRELY and aborted that gym's whole grade-fix pass. Before the
            # levers change a single broad `except Exception` caught every store
            # error and moved to the next row; splitting TypeError out quietly
            # took that protection away from the retry path, which is the path
            # most likely to fail (it exists precisely because the store is not
            # the shape we expected). One row must never cost the other thirty.
            try:
                updated = patcher(gym_id, r.get("id"),
                                  caption=new_cap, pillar=(new_cat or None))
            except Exception as exc:  # noqa: BLE001
                log(f"{gym_id} {r.get('post_date')}: caption patch retry failed: "
                    f"{type(exc).__name__}")
                continue
        except Exception as exc:  # noqa: BLE001
            log(f"{gym_id} {r.get('post_date')}: caption patch failed: "
                f"{type(exc).__name__}")
            continue
        if updated:
            r["caption"] = new_cap
            if new_cat:
                r["pillar"] = new_cat
            r.update(levers)
            patched_any = True
    return patched_any


def _restamp_levers(caption) -> dict:
    """The learning levers this caption actually earns, or {} when the flag is
    off. Classification only: it reads the caption and writes no copy."""
    if not config.cta_variety_enabled():
        return {}
    try:
        from agent import lever_stamp
        return {"hook_family": lever_stamp.hook_family(caption),
                "ask_type": lever_stamp.ask_type(caption),
                "caption_len_band": lever_stamp.caption_len_band(caption)}
    except Exception:  # noqa: BLE001 - a metadata refresh never blocks a fix
        return {}


# ---------------------------------------------------------------------------
# Default caption regeneration (client gyms: the REAL build context)
# ---------------------------------------------------------------------------

def _default_caption_regen(gym_id, profile, log):
    """Build the caption-regen closure from the gym's REAL build context
    (registry account, voice doc, media library, banned words) — the exact
    context the scan/backfill lanes use, so every fresh caption clears the
    same A+ / banned-word / copy gates with the same opening/angle variety.

    Returns None when the context cannot be assembled (B2B/LASSO — its captions
    come from the pillar builders and the refill lane owns them — or a missing
    account/voice): the caller then leaves captions alone and the defect is
    reported through the deduped held alert, never fixed dishonestly."""
    if profile == "B2B":
        return None
    try:
        from agent.client_media_sync import (_account_for_base, _banned_words_for,
                                             _library_dir,
                                             _resolve_client_voice_path)
        from agent.voice import load_voice
        account = _account_for_base(gym_id)
        if account is None:
            return None
        library_path = _library_dir(gym_id)
        voice = load_voice(_resolve_client_voice_path(gym_id, account.voice_doc_path()))
        if voice is None:
            return None
        banned = _banned_words_for(gym_id)
    except Exception as exc:  # noqa: BLE001
        log(f"{gym_id}: caption regen context unavailable: {type(exc).__name__}")
        return None

    from datetime import date as _d, timedelta as _td

    from agent.client_month_run import _clean_draft_for_day

    def _regen(row, avoid_captions, avoid_category=""):
        day = str((row or {}).get("post_date") or "")[:10]
        if not day:
            return None
        try:
            base = _d.fromisoformat(day)
        except ValueError:
            return None
        # Walk a few day keys so the deterministic source rotation lands on a
        # different approved source (and, for over-cap moves, a different
        # category). _clean_draft_for_day itself walks neighbours too; the
        # record_serve/ledger is untouched (we only borrow the caption; the
        # row keeps its own photo).
        #
        # require_media=False FOLLOWS FROM THAT LAST CLAUSE (2026-09-07). The row
        # keeps its own photo, so the draft's creative is the one part of it this
        # closure never uses -- and demanding one was silently disabling EVERY
        # repair pass on any gym whose media library is fully served. Measured
        # live on hillcountry, with the body pass given the entire 300s budget
        # and no competition: 5 of 5 body regens and 2 of 2 over-cap regens
        # returned None, every one of them with the single drop reason 'not A+:
        # no media (empty creative url)', while the captions those same drafts
        # carried were clean, on-avatar and 0.01 body-similar to the book. This
        # is why the shipped body-copy repair moved nothing on three real books:
        # it was never starved of budget so much as starved of a caption.
        #
        # The caption bar is FULLY intact (banned words, the avatar rail, the A+
        # caption checks, grounding). Only the photo requirement is lifted, for a
        # caller that takes no photo. It is also ~30x cheaper: the media failure
        # forced the builder's whole 8 day neighbour walk on every attempt, which
        # is what turned a 1.5 second regen into a 48 second one.
        for step in range(4):
            key = (base + _td(days=step * 3)).isoformat()
            try:
                draft, _drop = _clean_draft_for_day(
                    account, key, voice, library_path, banned, log,
                    allow_reuse=True, avoid_captions=tuple(avoid_captions),
                    require_media=False)
            except Exception as exc:  # noqa: BLE001
                log(f"{gym_id} {day}: caption regen failed: {type(exc).__name__}")
                return None
            if draft is None:
                continue
            cap = (getattr(draft, "caption", "") or "").strip()
            cat = (getattr(draft, "category", "") or "").strip()
            if not cap or cap in avoid_captions:
                continue
            if avoid_category and cat and cat.lower() == str(avoid_category).lower():
                continue
            return cap, cat
        return None

    return _regen


# ---------------------------------------------------------------------------
# b) day gaps — the EXISTING lanes only, once per gym per day
# ---------------------------------------------------------------------------

def _default_gap_filler(gym_id, profile, store, today_iso, log, db=None):
    """Fill forward gaps through the lane that already owns the gym's builds.
    Heavy, so it runs at most once per gym per day (kv stamp). Returns a short
    outcome string for the sweep report."""
    _db = db if db is not None else _default_db()
    stamp = f"grade_fix_lane_{gym_id}_{today_iso}"
    try:
        if _db.kv_get(stamp):
            return "already_ran_today"
        _db.kv_set(stamp, "1")
    except Exception:  # noqa: BLE001 - a kv failure never blocks the lane
        pass
    if profile == "B2B":
        return _lasso_refill(gym_id, store, today_iso, log)
    return _client_grow(gym_id, store, log)


def _client_grow(gym_id, store, log):
    """Reuse the EXISTING scan/grow lane (client_media_sync.scan_and_generate)
    for this gym only. The lane's own guards decide: it builds only when unused
    media exists, preserves approvals, and never shrinks or wipes. A gym with
    no unused media is an honest no-op, never a fabricated fill."""
    if not config.client_media_sync_enabled():
        return "lane_off"
    try:
        from agent import client_media_sync
        res = client_media_sync.scan_and_generate(clients=[gym_id], store=store)
        if res.get("generated"):
            return "filled"
        return "no_media"
    except Exception as exc:  # noqa: BLE001
        log(f"{gym_id}: grow lane failed: {type(exc).__name__}")
        return "error"


def _lasso_refill(gym_id, store, today_iso, log):
    """The real month planner refill (the same Wave 6 lane that refills freed
    slots). Behind AGENT_REAL_MONTH_PLAN; apply_month_plan itself preserves
    approvals and re-runs the calendar A-gate, so a refill can only ever stage
    an A book. Nothing publishes; every refilled row lands 'pending'."""
    if not config.real_month_plan_enabled():
        return "lane_off"
    try:
        from agent import real_month_planner, real_month_run
        acct_key = gym_id if str(gym_id).endswith(("_ig", "_fb")) else f"{gym_id}_ig"
        drafts = real_month_run.plan_and_build(acct_key, today_iso, 30)
        if not drafts:
            return "no_content"
        span = real_month_planner.plan_span_months(today_iso, 30)
        res = real_month_planner.apply_month_plan(gym_id, drafts, store,
                                                  span_months=span)
        return "filled" if res.get("ok") else "held"
    except Exception as exc:  # noqa: BLE001
        log(f"{gym_id}: real month refill failed: {type(exc).__name__}")
        return "error"


def _record_gaps_once(gym_id, gap_dates, log, db=None):
    """An unfillable gap is NOT a defect to re-announce every sweep: record
    each gap date once (kv) and log it; the sweep's alert dedup keeps the
    channel quiet after that."""
    _db = db if db is not None else _default_db()
    for d in gap_dates:
        key = f"grade_gap_known_{gym_id}_{d}"
        try:
            if _db.kv_get(key):
                continue
            _db.kv_set(key, "1")
        except Exception:  # noqa: BLE001
            continue
        log(f"{gym_id}: forward gap before {d} cannot be filled honestly "
            "(no unused media/content); recorded once")
