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
     actually wrote the new caption (the label always tells the truth).

HARD GUARANTEES:
  * Only WIPEABLE (pending/draft/queued) rows are ever patched. The store's
    patch_pending_plan carries a server-side status filter, so an approved /
    published / denied row can never be modified even on a caller bug.
  * Every patched row stays 'pending' — it re-enters the human approval queue.
  * No caption is invented here: every fresh caption comes from the gym's own
    approved sources via the existing gated builder path.
"""
from __future__ import annotations

from agent import config
from agent.caption_ledger import caption_hash
from agent.portal_calendar_store import _WIPEABLE_STATUSES


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
                           db=None, logger=None) -> dict:
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
        db:            injectable kv module (agent.db).

    Returns {ok, captions_fixed, repillared, gap_fill, skipped, actions}.
    """
    log = logger or (lambda m: print(f"[grade-fix] {m}"))
    if not config.grade_self_fix_enabled():
        return {"ok": False, "reason": "AGENT_GRADE_SELF_FIX off",
                "captions_fixed": 0, "repillared": 0, "gap_fill": "none",
                "skipped": 0, "actions": []}

    actions = []
    result = {"ok": True, "captions_fixed": 0, "repillared": 0,
              "gap_fill": "none", "skipped": 0, "actions": actions}
    rows = list(rows or [])

    if caption_regen is None:
        caption_regen = _default_caption_regen(gym_id, profile, log)

    # Every caption already on the book: a regenerated caption may never
    # collide with an existing one (or another fresh one from this pass).
    avoid = {(r.get("caption") or "").strip()
             for r in rows if (r.get("caption") or "").strip()}

    # ---- a) true duplicate captions (same hash on more than one date) ------
    dup_fixed, dup_skipped = _fix_duplicates(
        gym_id, rows, store, caption_regen, avoid, log)
    result["captions_fixed"] = dup_fixed
    result["skipped"] += dup_skipped
    if dup_fixed:
        actions.append(f"rewrote {dup_fixed} duplicate caption day(s) fresh on the same photo")

    # ---- c) category over-cap ----------------------------------------------
    over_fixed, over_skipped = _fix_overcap(
        gym_id, rows, store, defects, caption_regen, avoid, log)
    result["repillared"] = over_fixed
    result["skipped"] += over_skipped
    if over_fixed:
        actions.append(f"re-pillared {over_fixed} over-cap day(s) from a different approved source")

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

def _fix_duplicates(gym_id, rows, store, caption_regen, avoid, log):
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
            if caption_regen is None:
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


def _fix_overcap(gym_id, rows, store, defects, caption_regen, avoid, log):
    """Re-pillar excess wipeable days of an over-25% category by regenerating
    their caption from a DIFFERENT approved source category (the pillar label
    follows the caption that actually wrote the day). Latest days move first;
    a day with any human-owned row never moves. Returns (days_fixed, days_skipped)."""
    over_cats = [str(d[1]) for d in (defects or [])
                 if d and d[0] == "content_mix" and "over 25%" in str(d[2])]
    if not over_cats:
        return 0, 0
    if caption_regen is None:
        return 0, len(over_cats)

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
            if _patch_date_rows(gym_id, grp, store, new_cap, new_cat, log):
                fixed += 1
                moved = len(grp)
                excess -= moved
                counts[cat] -= moved
                counts[new_cat] = counts.get(new_cat, 0) + moved
                avoid.add(new_cap.strip())
            else:
                skipped += 1
    return fixed, skipped


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
