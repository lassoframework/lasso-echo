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

HARD GUARANTEES:
  * Only WIPEABLE (pending/draft/queued) rows are ever patched. The store's
    patch_pending_plan carries a server-side status filter, so an approved /
    published / denied row can never be modified even on a caller bug.
  * Every patched row stays 'pending' — it re-enters the human approval queue.
  * No caption is invented here: every fresh caption comes from the gym's own
    approved sources via the existing gated builder path.
"""
from __future__ import annotations

from agent import config, copy_gate
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


def _deadline(budget_s=None):
    """A monotonic wall-clock deadline for this pass's LLM spend."""
    import time
    return time.monotonic() + (float(budget_s) if budget_s is not None
                               else _LLM_BUDGET_S)


def _budget_left(deadline) -> bool:
    import time
    return deadline is None or time.monotonic() < deadline


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
             booking_asks_added, gap_fill, skipped, actions}.
    """
    log = logger or (lambda m: print(f"[grade-fix] {m}"))
    if not config.grade_self_fix_enabled():
        return {"ok": False, "reason": "AGENT_GRADE_SELF_FIX off",
                "captions_fixed": 0, "repillared": 0, "craft_fixed": 0,
                "craft_attempted": 0, "booking_asks_added": 0,
                "gap_fill": "none", "skipped": 0, "actions": []}

    actions = []
    result = {"ok": True, "captions_fixed": 0, "repillared": 0,
              "craft_fixed": 0, "craft_attempted": 0, "booking_asks_added": 0,
              "audience_fixed": 0, "audience_attempted": 0,
              "gap_fill": "none", "skipped": 0, "actions": actions}
    rows = list(rows or [])
    deadline = _deadline(llm_budget_s)

    if caption_regen is None:
        caption_regen = _default_caption_regen(gym_id, profile, log)

    # Every caption already on the book: a regenerated caption may never
    # collide with an existing one (or another fresh one from this pass).
    avoid = {(r.get("caption") or "").strip()
             for r in rows if (r.get("caption") or "").strip()}

    # ---- a) true duplicate captions (same hash on more than one date) ------
    dup_fixed, dup_skipped = _fix_duplicates(
        gym_id, rows, store, caption_regen, avoid, log, deadline=deadline)
    result["captions_fixed"] = dup_fixed
    result["skipped"] += dup_skipped
    if dup_fixed:
        actions.append(f"rewrote {dup_fixed} duplicate caption day(s) fresh on the same photo")

    # ---- c) category over-cap (iterates to convergence) ---------------------
    over_fixed, over_skipped = _fix_overcap(
        gym_id, rows, store, defects, caption_regen, avoid, log,
        deadline=deadline)
    result["repillared"] = over_fixed
    result["skipped"] += over_skipped
    if over_fixed:
        actions.append(f"re-pillared {over_fixed} over-cap day(s) from a different approved source")

    # ---- d) caption craft + path (booking asks) ------------------------------
    craft_fixed, craft_attempted, booking_added = _fix_craft(
        gym_id, rows, store, profile, caption_regen, avoid, log,
        booking_cta=booking_cta, deadline=deadline)
    result["craft_fixed"] = craft_fixed
    result["craft_attempted"] = craft_attempted
    result["booking_asks_added"] = booking_added
    result["skipped"] += max(0, craft_attempted - craft_fixed)
    if craft_fixed:
        actions.append(f"rewrote {craft_fixed} caption(s) that tripped craft flags (ask/hook/length)")
    if booking_added:
        actions.append(f"carried the gym's real booking CTA onto {booking_added} day(s)")

    # ---- e) off-avatar hooks (right_audience) -------------------------------
    aud_fixed, aud_attempted = _fix_audience(
        gym_id, rows, store, profile, caption_regen, avoid, log,
        deadline=deadline)
    result["audience_fixed"] = aud_fixed
    result["audience_attempted"] = aud_attempted
    result["skipped"] += max(0, aud_attempted - aud_fixed)
    if aud_fixed:
        actions.append(f"rewrote {aud_fixed} off-avatar hook(s) back onto the gym's avatar")

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


def _over_cap_cats(rows):
    """Categories currently above 25% of the book (the grader's own bar)."""
    from collections import Counter
    n = len(rows)
    if not n:
        return []
    counts = Counter(_cat_of(r) for r in rows)
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
    for _ in range(_OVERCAP_MAX_ITER):
        if not _budget_left(deadline):
            break              # bounded: the nightly run always finishes
        fixed, skipped = _overcap_pass(gym_id, rows, over_cats, store,
                                       caption_regen, avoid, log,
                                       deadline=deadline)
        total_fixed += fixed
        total_skipped += skipped
        over_cats = _over_cap_cats(rows)
        if not over_cats or fixed == 0:
            break
    return total_fixed, total_skipped


def _overcap_pass(gym_id, rows, over_cats, store, caption_regen, avoid, log,
                  deadline=None):
    """One over-cap move wave: re-pillar excess wipeable days of each over-25%
    category by regenerating their caption from a DIFFERENT approved source
    category (the pillar label follows the caption that actually wrote the
    day). Latest days move first; a day with any human-owned row never moves;
    a move that would push the TARGET category itself over 25% is skipped so
    the loop converges instead of ping-ponging. Returns (days_fixed, days_skipped)."""
    from collections import Counter
    n = len(rows) or 1
    counts = Counter(_cat_of(r) for r in rows)

    fixed = skipped = 0
    for cat in over_cats:
        excess = counts.get(cat, 0) - int(0.25 * n)
        if excess <= 0:
            continue
        by_date: dict = {}
        for r in rows:
            if _cat_of(r) == cat:
                by_date.setdefault(str(r.get("post_date") or "")[:10], []).append(r)
        for d in sorted(by_date, reverse=True):
            if excess <= 0:
                break
            grp = by_date[d]
            if any(not _is_wipeable(r) for r in grp):
                continue                       # human-owned day is never re-pointed
            if not _budget_left(deadline):
                break                          # bounded LLM spend, honest stop
            out = None
            try:
                out = caption_regen(grp[0], avoid, cat)
            except Exception as exc:  # noqa: BLE001
                log(f"{gym_id} {d}: over-cap regen raised {type(exc).__name__}")
            if not out:
                skipped += 1
                continue
            new_cap, new_cat = out
            if not new_cat or str(new_cat).lower() == str(cat).lower():
                skipped += 1                   # the content does not support a move
                continue
            moved = len(grp)
            if (counts.get(new_cat, 0) + moved) / n > 0.25:
                skipped += 1                   # target has no headroom: honest skip
                continue
            if _patch_date_rows(gym_id, grp, store, new_cap, new_cat, log):
                fixed += 1
                excess -= moved
                counts[cat] -= moved
                counts[new_cat] = counts.get(new_cat, 0) + moved
                avoid.add(new_cap.strip())
            else:
                skipped += 1
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


def _clears_craft(caption) -> bool:
    """The post-regen assertion: a fresh caption replaces a flagged one ONLY
    when it is strictly clean — exactly one ask, first line inside the hook
    band, length 150 to 500, zero hard violations, zero soft flags. Anything
    less and the row keeps its current caption (never swap in a worse one)."""
    cap = (caption or "").strip()
    if not cap or copy_gate.violations(cap):
        return False
    if _ask_count(cap) != 1:
        return False
    first = cap.splitlines()[0].strip()
    if not first or len(first) > _HOOK_MAX:
        return False
    if not (_CAPTION_MIN <= len(cap) <= _CAPTION_MAX):
        return False
    if copy_gate.soft_flags(cap):
        return False
    return True


def _booking_deficit(rows) -> int:
    """How many more booking-term rows the path_to_join GYM leg wants
    (>= min(5, n) rows carrying a booking-specific ask)."""
    n = len(rows)
    have = sum(1 for r in rows if _BOOKING_RE.search(r.get("caption") or ""))
    return max(0, min(5, n) - have)


def _booking_cta_for(gym_id, log):
    """The gym's REAL booking CTA: the first CTA in its approved voice-doc
    rotation that is both a booking term and a recognized ask; when the bible's
    CTA section is empty (a hand-fill onboarding TODO — ENG 2026-08-31), a
    VERBATIM booking-ask sentence from the gym's own APPROVED sources. Both are
    approved copy; a gym with neither gets an honest skip (never an invented CTA)."""
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

    deficit = _booking_deficit(rows)
    if booking_cta is None:
        booking_cta = _booking_cta_for(gym_id, log)

    # One post spans same-date rows sharing a caption (IG feed + FB mirror +
    # paired story); group by (date, hash) so the day moves together and a 2x
    # day's two distinct posts stay independent.
    groups: dict = {}
    for r in rows:
        d = str(r.get("post_date") or "")[:10]
        h = caption_hash(r.get("caption") or "")
        groups.setdefault((d, h), []).append(r)

    fixed = attempted = booking_added = 0
    for (d, _h), grp in sorted(groups.items()):
        if any(not _is_wipeable(r) for r in grp):
            continue                            # human-owned day: never touched
        cap = grp[0].get("caption") or ""
        if not cap.strip():
            continue        # caption-less story / GBP photo post: nothing to craft
        if not _craft_flags(cap):
            continue                            # already passes: never touched
        attempted += 1

        # --- candidate 1: the free, deterministic, zero-fabrication repair ---
        repaired = _mechanical_repair(cap, booking_cta)
        candidates = []
        if repaired and repaired != cap:
            candidates.append((repaired, None,
                               _ask_count(cap) == 0 and _ask_count(repaired) == 1))
        winner = next(((c, cat, cta_used) for c, cat, cta_used in candidates
                       if c and c not in avoid and _clears_craft(c)), None)

        # --- candidate 2: the LLM regen, only when mechanics could not help ---
        if winner is None and llm_ok and _budget_left(deadline):
            out = None
            try:
                out = caption_regen(grp[0], avoid, "")
            except Exception as exc:  # noqa: BLE001
                log(f"{gym_id} {d}: craft regen raised {type(exc).__name__}")
            if out:
                regen_cap, new_cat = out
                regen_cap = (regen_cap or "").strip()
                carried_cta = False
                if (deficit > 0 and booking_cta
                        and not _BOOKING_RE.search(regen_cap)
                        and _ask_count(regen_cap) == 0):
                    # The already-flagged day is the honest place to carry the
                    # gym's real booking CTA: it becomes the caption's single ask.
                    regen_cap = f"{regen_cap}\n{booking_cta}".strip()
                    carried_cta = True
                if (regen_cap and regen_cap not in avoid
                        and _clears_craft(regen_cap)):
                    winner = (regen_cap, new_cat, carried_cta)

        if winner is None:
            log(f"{gym_id} {d}: neither the mechanically repaired nor the "
                "regenerated caption clears the craft bar; keeping the current "
                "caption")
            continue
        new_cap, new_cat, carried_cta = winner
        if _patch_date_rows(gym_id, grp, store, new_cap, new_cat or None, log):
            fixed += 1
            avoid.add(new_cap)
            if _BOOKING_RE.search(new_cap):
                deficit = max(0, deficit - len(grp))
                if carried_cta:
                    booking_added += 1
    return fixed, attempted, booking_added


# ---------------------------------------------------------------------------
# e) off-avatar hooks (right_audience)
# ---------------------------------------------------------------------------

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
    patched_any = False
    for r in date_rows:
        if not _is_wipeable(r):
            continue                            # never touched, by policy
        try:
            updated = patcher(gym_id, r.get("id"),
                              caption=new_cap, pillar=(new_cat or None))
        except Exception as exc:  # noqa: BLE001
            log(f"{gym_id} {r.get('post_date')}: caption patch failed: "
                f"{type(exc).__name__}")
            continue
        if updated:
            r["caption"] = new_cap
            if new_cat:
                r["pillar"] = new_cat
            patched_any = True
    return patched_any


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
        for step in range(4):
            key = (base + _td(days=step * 3)).isoformat()
            try:
                draft, _drop = _clean_draft_for_day(
                    account, key, voice, library_path, banned, log,
                    allow_reuse=True, avoid_captions=tuple(avoid_captions))
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
