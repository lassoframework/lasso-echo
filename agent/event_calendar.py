"""
event_calendar.py — insert an event ARC into the existing month plan, re-grade
through the A-gate, guard overlaps, and sweep on cancel/ended/date-edit.

Calendar integration, not a side channel (EVENT_CAMPAIGNS_BUILD.md §2/§3). The arc
rows (gym_event.draft_arc) are folded into the gym's already-approved content_calendar
as category 'offer', DISPLACING doctrine/education slots FIRST and NEVER displacing
other proof or offer posts. After insertion the month is re-graded through the A-gate
(calendar_grade); a broken quota triggers the same remediation the month planner uses.

PURE core (merge_arc, overlap_thin, retime_arc, sweep_cancelled) operates on row
lists so every rule is offline-testable. The thin store-facing wrappers (stage_arc,
cancel_event, edit_event_dates) apply through the injectable SupabaseCalendarStore,
exactly like real_month_planner.apply_month_plan.

RAILS: every arc row lands 'pending'; approved rows never move on a date edit; a
cancelled/ended event flips its PENDING arc rows denied with reject_reason; the month
must still grade A after insertion.
"""

from . import gym_event as ge

# The calendar categories an event arc may DISPLACE. Doctrine/education are the
# house-teaching slots; an offer/event post takes their day first. Everything else
# (offer, proof, results, faces, community, invite, and the dated book/summit/welcome
# overrides) is PROTECTED and never displaced by an event arc.
_DISPLACEABLE = ("doctrine", "education")

# Portal-facing statuses that mean a human/publisher has touched the row. An event
# sweep never flips these (only PENDING arc rows flip denied).
_WIPEABLE = ("pending", "draft", "queued")

REJECT_CANCELLED = "event_cancelled"
REJECT_ENDED = "event_ended"
REJECT_DEAD_LINK = "event_link_dead"

# The A-gate protects an ALREADY-POPULATED month from being broken by an arc insert.
# Below this many existing rows the calendar is a sparse seed (a brand-new gym or the
# event is the first content), so there is no month to break and the gate is skipped —
# the normal month planner fills around the arc later. A ~2-week populated calendar
# (>= 10 rows) is enough to grade a month worth protecting.
_MIN_MONTH_FOR_GATE = 10

# FEED-REACHABLE statuses: what the gym's audience can actually end up seeing. The
# same positive allowlist the grade-read lane uses (c629ed8): dead rows (denied /
# killed / expired / retired) and placeholder sample books never count for or
# against a month. stage_arc thins, merges and A-grades against THIS view only —
# Zanshin's 100+ denied duplicate-purge rows graded the merged month D and blocked
# a healthy arc top-up the night before the promo (2026-08-31).
_FEED_REACHABLE = ("pending", "approved", "publishing", "published", "coach_review")


def _cat(row):
    return str((row or {}).get("pillar") or (row or {}).get("category") or "").lower()


def _status(row):
    return str((row or {}).get("status") or "").lower()


def _slot_key(row):
    return (
        str((row or {}).get("post_date") or "")[:10],
        str((row or {}).get("account") or "").lower(),
        str((row or {}).get("format") or "").lower(),
    )


def _is_arc(row):
    return bool((row or {}).get("event_id"))


# ---------------------------------------------------------------------------
# Overlap guard (§2): cap combined event posts at the category ceiling.
# ---------------------------------------------------------------------------

def overlap_thin(existing_rows, arc_rows, *, category_ceiling_fraction=0.25):
    """Two OVERLAPPING events for one gym must not flood the calendar: thin the SECOND
    arc so the combined event posts respect the offer category ceiling. `existing_rows`
    are the gym's current calendar rows (may already contain a FIRST event's arc);
    `arc_rows` is the NEW arc being inserted.

    KEY: an event's OWN arc is intended concentration, not a mix flaw (exactly like the
    Summit sprint the grader already exempts). So a single event on an otherwise
    event-free calendar is NEVER thinned by itself — the ceiling only bites when a
    PRIOR overlapping event's offer posts already consume the room. We measure the
    ceiling against the realistic month the combined runs span (a gym posts ~daily),
    subtract the PRIOR event's offer posts, and thin only the excess of the new arc
    beyond that room. Thinning drops the lowest-priority rows (during first, then
    final) and always keeps the arc spine (announce / recap). Pure; never thins the
    first arc or any approved row."""
    if not arc_rows:
        return []
    # PRIOR OVERLAPPING-event offer posts already on the calendar. A first event's arc
    # rows carry an event_id DIFFERENT from the new arc's; a plain live offer counts too.
    #
    # "OVERLAPPING" MEANS THE DATES ACTUALLY INTERSECT. This used to count ANY offer row
    # the shared monthly list_month read returned, so two events that never run at the
    # same time still thinned each other: Pete/Zanshin's Back to School (Sep 1 to 15) was
    # cut from 10 arc posts to 4 because Bring a Friend Week (Oct 3 to 10) happened to
    # have rows in the same month bucket. He saw "three posts this week then nothing
    # until the last day" — 6 DURING posts silently dropped. The ceiling still applies in
    # full when two events genuinely overlap; it just no longer fires when they do not.
    new_event_ids = {r.get("event_id") for r in arc_rows if r.get("event_id")}
    arc_dates = {str(r.get("post_date"))[:10] for r in arc_rows if r.get("post_date")}
    first_day, last_day = (min(arc_dates), max(arc_dates)) if arc_dates else ("", "")
    prior_offer = [r for r in existing_rows
                   if _cat(r) == ge.ARC_CATEGORY
                   and r.get("event_id") not in new_event_ids
                   and first_day <= str(r.get("post_date") or "")[:10] <= last_day]
    if not prior_offer:
        # No overlapping prior event: the arc's own concentration is by design, keep it.
        return list(arc_rows)
    # The realistic month the combined runs span (daily posting cadence assumed).
    all_dates = {str(r.get("post_date"))[:10]
                 for r in list(existing_rows) + list(arc_rows) if r.get("post_date")}
    span_days = _date_span(all_dates)
    projected_total = max(len(existing_rows) + len(arc_rows), span_days)
    ceiling = int(projected_total * category_ceiling_fraction)
    room = max(0, ceiling - len(prior_offer))
    if len(arc_rows) <= room:
        return list(arc_rows)
    keep_priority = {
        ge.ANNOUNCE: 0, ge.HOW_IT_WORKS: 1, ge.LAST_CALL: 2,
        ge.RECAP: 3, ge.FINAL_DAY: 4, ge.DURING: 5,
    }
    ordered = sorted(arc_rows,
                     key=lambda r: keep_priority.get(r.get("arc_kind"), 9))
    if room <= 0:
        return _spine_only(ordered)
    return ordered[:room]


# When the ceiling leaves literally no room, the second arc still keeps a minimal
# SPINE (announce + final + recap) so the event is not silent — thinned, not flooded,
# never starved to zero.
_MIN_ARC_SPINE = 3


def _spine_only(ordered_rows):
    """Keep only the arc spine (announce, final day, recap) when there is no room
    under the ceiling — the second event is present but minimal."""
    spine_kinds = (ge.ANNOUNCE, ge.FINAL_DAY, ge.RECAP)
    spine = [r for r in ordered_rows if r.get("arc_kind") in spine_kinds]
    return spine[:_MIN_ARC_SPINE]


def _date_span(dates):
    """Inclusive count of calendar days from the earliest to the latest date in the
    set (a gym posts ~daily, so this approximates the month the rows span). 0 for an
    empty set, 1 for a single date."""
    ds = sorted(d for d in dates if d)
    if not ds:
        return 0
    from datetime import date as _d
    try:
        return (_d.fromisoformat(ds[-1]) - _d.fromisoformat(ds[0])).days + 1
    except ValueError:
        return len(ds)


# ---------------------------------------------------------------------------
# Insertion (§2): displace doctrine/education first, never proof/offer.
# ---------------------------------------------------------------------------

def merge_arc(existing_rows, arc_rows):
    """Fold `arc_rows` into `existing_rows`, DISPLACING doctrine/education slots on the
    arc's dates FIRST and NEVER displacing other proof/offer posts. Returns the merged
    row list.

    Rule: for each arc row's (date), if a WIPEABLE doctrine/education feed row occupies
    that date, it is REMOVED (the arc takes its slot); a protected category (offer,
    proof, results, faces, community, invite) or any human-owned row is LEFT and the
    arc row is ADDED alongside (the day simply carries an extra offer post — the A-gate
    then decides if the mix holds). Approved rows are never touched. Pure."""
    arc_dates = {str(r.get("post_date"))[:10] for r in arc_rows}
    kept = []
    # Displace at most one displaceable wipeable row PER arc date (the arc adds one
    # feed per date; it should not wipe a whole day of teaching, just take one slot).
    displaced_budget = {d: sum(1 for r in arc_rows
                               if str(r.get("post_date"))[:10] == d
                               and r.get("format", "feed") == "feed")
                        for d in arc_dates}
    for row in existing_rows:
        d = str(row.get("post_date"))[:10]
        if (d in arc_dates and _cat(row) in _DISPLACEABLE
                and _status(row) in _WIPEABLE
                and str(row.get("format") or "feed").lower() == "feed"
                and displaced_budget.get(d, 0) > 0):
            displaced_budget[d] -= 1
            continue  # displaced by the arc; drop it
        kept.append(row)
    return kept + list(arc_rows)


def regrade(rows, *, profile="GYM"):
    """Re-grade the merged month through the A-gate with the same remediation the
    month planner uses. Returns (rows, grade). A broken quota is remediated in place
    up to 4 passes (real_month_planner._remediate). The month must still grade A after
    insertion; the caller alerts + refuses to stage when it cannot."""
    from agent.calendar_grade import grade_month, A_THRESHOLD
    from agent.real_month_planner import _remediate
    grade = grade_month(rows, profile=profile)
    attempts = 0
    while grade.total < A_THRESHOLD and attempts < 4:
        attempts += 1
        _remediate(rows, grade.defects)
        grade = grade_month(rows, profile=profile)
    return rows, grade


# ---------------------------------------------------------------------------
# Date edit (§2): re-time the arc, re-stage ONLY changed rows.
# ---------------------------------------------------------------------------

def retime_arc(old_arc_rows, new_event, *, today=None, avatar=None):
    """Editing an event's dates RE-TIMES its arc. Returns (restage_rows, keep_rows,
    remove_row_keys):
      * restage_rows: freshly grounded arc rows for the NEW dates whose slot changed
        from the old arc (these re-stage as 'pending').
      * keep_rows: old arc rows that DID NOT move AND are approved — they stay exactly
        as they are (approved unaffected rows stay approved).
      * remove_row_keys: old PENDING arc rows whose date changed (superseded by the
        re-timed arc; the store deletes/denies them).

    Pure: the new arc is planned from `new_event`; `today` injected. An approved old
    row on a date the new arc still uses is preserved (never re-staged over an
    approval)."""
    new_rows = ge.draft_arc(new_event, ge.plan_arc(new_event, today=today),
                            avatar=avatar)
    old_by_kind = {}
    for r in old_arc_rows:
        old_by_kind.setdefault(r.get("arc_kind"), []).append(r)

    keep_rows, restage_rows, remove_keys = [], [], []
    new_dates_by_kind = {r.get("arc_kind"): str(r.get("post_date"))[:10]
                         for r in new_rows}

    approved_old_dates = set()
    for kind, olds in old_by_kind.items():
        for old in olds:
            old_date = str(old.get("post_date"))[:10]
            new_date = new_dates_by_kind.get(kind)
            if _status(old) not in _WIPEABLE:
                # Approved/published etc: keep as-is if its date is unchanged, else it
                # is a human-owned row on a now-moved kind — keep it too (never revert
                # an approval), but the new arc row for that kind is still staged.
                keep_rows.append(old)
                if old_date == new_date:
                    approved_old_dates.add(old_date)
            else:
                # Pending old arc row: superseded by the re-timed arc.
                remove_keys.append(_slot_key(old))

    for nr in new_rows:
        d = str(nr.get("post_date"))[:10]
        # Skip re-staging a row onto a date an APPROVED old row of the same kind
        # already holds (unchanged, approved -> left alone).
        if d in approved_old_dates and nr.get("arc_kind") in {
                r.get("arc_kind") for r in keep_rows
                if str(r.get("post_date"))[:10] == d}:
            continue
        restage_rows.append(nr)
    return restage_rows, keep_rows, remove_keys


# ---------------------------------------------------------------------------
# Cancel / ended sweep (§2): flip PENDING arc rows denied.
# ---------------------------------------------------------------------------

def sweep_arc_rows(arc_rows, reason):
    """Given an event's arc rows, return (deny_ids, keep) for a cancel/ended sweep:
    every PENDING (wipeable) arc row is flipped denied with `reason`; approved/
    published/denied rows are left (a human owns them, or they already fired). Pure.

    `deny_ids` is the list of row ids to PATCH denied. A row with no id (not yet
    persisted) is simply dropped from staging by the caller."""
    deny_ids = []
    for row in arc_rows:
        if _status(row) in _WIPEABLE:
            rid = row.get("id")
            if rid:
                deny_ids.append(rid)
    return deny_ids


# ---------------------------------------------------------------------------
# Store-facing wrappers (apply through the injectable store).
# ---------------------------------------------------------------------------

def stage_arc(store, event, arc_rows, *, profile="GYM", logger=None,
              media_picker=None, media_host_fn=None, gate="hard"):
    """Insert `arc_rows` into the gym's live month plan through `store`, re-grade, and
    stage the kept rows as 'pending'. Returns a summary dict. Never publishes.

    Steps:
      1. read the gym's existing rows across the arc's months (store.list_month).
      2. overlap-thin the new arc against existing offer posts (overlap_thin).
      3. merge (displace doctrine/education first, never proof/offer) (merge_arc).
      4. re-grade through the A-gate with remediation (regrade). A month that cannot
         reach A after 4 passes is NOT staged (alert + refuse), matching apply_month_plan.
      5. insert the kept new arc rows (store.insert_rows). Existing rows are untouched
         (we only ADD arc rows and rely on the A-gate; we never delete a human row).

    gate: "hard" (default) refuses to stage a month that cannot hold A — the contract
    for CREATING an arc. "advisory" grades, logs and ALERTS a sub-A month but stages
    anyway: the top-up heal of an event the client already created and the gate
    already admitted once must never silently strand a running promo over a few
    caption soft flags (Zanshin Back To School graded C->D on the arc's own punchy
    beat lines the night before its start). Every staged row still lands PENDING
    behind the client's approval gate either way."""
    log = logger or (lambda m: print(f"[event-calendar] {m}"))
    if isinstance(event, dict):
        event = ge.GymEvent.from_row(event)
    gym_id = event.gym_id
    months = sorted({str(r.get("post_date"))[:7] for r in arc_rows if r.get("post_date")})

    existing = []
    lister = getattr(store, "list_month", None)
    if lister is not None:
        for m in months:
            try:
                existing.extend(lister(gym_id, m) or [])
            except Exception as exc:  # noqa: BLE001
                log(f"stage_arc: list_month {m} failed {type(exc).__name__}; treating as empty")

    # THE MONTH THE AUDIENCE SEES (Zanshin top-up refusal, 2026-08-31): list_month
    # returns EVERY status, but overlap-thin, merge and the A-gate are promises about
    # what will actually publish. Grading Zanshin's 100+ denied duplicate-purge rows
    # as "the month" scored the merged plan D(69) and refused to stage a healthy arc
    # the night before the promo started; a denied offer row was also consuming the
    # offer ceiling in overlap_thin. Thin/merge/grade against feed-reachable rows
    # only; the full `existing` list still feeds the occupancy guard below (which
    # NEEDS the denied/killed rows to apply the deny-reopens-once rule).
    live_existing = [r for r in existing if _status(r) in _FEED_REACHABLE]

    thinned = overlap_thin(live_existing, arc_rows)
    if len(thinned) < len(arc_rows):
        log(f"overlap guard: thinned event {event.id} arc from {len(arc_rows)} to "
            f"{len(thinned)} to respect the offer category ceiling")

    merged = merge_arc(live_existing, thinned)
    # A-GATE: the promise is the month must still grade A AFTER insertion — i.e. the
    # arc must not BREAK an already-good month. When there is a real existing month to
    # protect (enough rows for the grader to score a month, not a sparse seed), we grade
    # the merged plan and refuse to stage if it can't hold A after remediation. When the
    # calendar is empty / sparse (a brand-new gym, or the event IS the first content),
    # the arc is a seed the normal month planner fills around, so there is no month to
    # break — we skip the gate and stage the pending arc (every row still lands pending).
    grade = None
    if len(live_existing) >= _MIN_MONTH_FOR_GATE:
        merged, grade = regrade(merged, profile=profile)
        if grade.total < _a_threshold():
            from agent import ops_alerts
            if gate == "hard":
                ops_alerts.alert(
                    f"event arc insert: {gym_id}/{event.id} would drop the month to "
                    f"{grade.total} ({grade.letter}) after 4 remediation passes. NOT STAGING.")
                return {"ok": False, "reason": f"month would grade {grade.letter}",
                        "grade": grade.total, "staged": 0}
            # advisory: the top-up heal of an admitted, client-created arc. Visibility,
            # never a silent strand — the promo's missing beats still stage PENDING.
            ops_alerts.alert(
                f"event arc top-up: {gym_id}/{event.id} month grades {grade.total} "
                f"({grade.letter}) after remediation; staging anyway (advisory gate — "
                "top-up of an already-admitted client event; rows land pending).")
            log(f"advisory gate: month grades {grade.total} ({grade.letter}); staging anyway")

    # Stage only the NEW arc rows (existing rows already live). Recap rows that are
    # blocked (no media yet) are held out of staging until media arrives.
    to_stage = [r for r in thinned if not r.get("recap_blocked")]
    # RE-STAGE GUARD: stage_arc only ever ADDS, so staging an arc that is already on the
    # calendar duplicated every one of its days. Hit live on 2026-08-30 re-staging
    # Pete/Zanshin's Back to School promo: four dates came back twice, including three
    # where the client had already APPROVED the original. preserve_and_prune did not
    # catch it because an arc row carries no slot the incoming row collides on. Drop any
    # incoming row this event already occupies on the same date + account + format, so a
    # re-stage after an edit tops the arc up instead of doubling it.
    #
    # DENY = RECREATE (Pete/CrossFit Zanshin, 2026-08-31): a client deny burns a
    # recreate unit and, everywhere else in the product, buys a fresh replacement.
    # This guard used to count DENIED rows as occupying their slot forever, so a
    # denied arc beat could never be re-staged: Pete denied his Back To School rows
    # and the promo start date arrived with ZERO active posts and no way to heal.
    # A slot whose only rows for this event are DENIED now counts as OPEN — but at
    # most one automatic recreate per beat: a second deny on the same slot means
    # the client really does not want that beat, and it stays closed. Every other
    # status (pending/approved/published/killed/...) blocks exactly as before;
    # kill in particular remains permanent.
    already = set()
    denied_count = {}
    for r in existing:
        if str(r.get("event_id") or "") != str(event.id):
            continue
        slot = (str(r.get("post_date"))[:10],
                str(r.get("account") or "").lower(),
                str(r.get("format") or "").lower())
        if _status(r) == "denied":
            denied_count[slot] = denied_count.get(slot, 0) + 1
        else:
            already.add(slot)
    for slot, n in denied_count.items():
        if n >= 2:
            already.add(slot)
    if already:
        before = len(to_stage)
        to_stage = [r for r in to_stage
                    if (str(r.get("post_date"))[:10],
                        str(r.get("account") or "").lower(),
                        str(r.get("format") or "").lower()) not in already]
        if before != len(to_stage):
            log(f"re-stage: {before - len(to_stage)} row(s) for {event.id} already on "
                "the calendar, topping up rather than duplicating")
    held_recap = [r for r in thinned if r.get("recap_blocked")]
    # MEDIA: an arc row was staged with NO image_url, and a feed post without an image
    # cannot publish — Zanshin's entire first month (2026-08-30) was image-less rows
    # that could only ever fail. Attach a real photo from the gym's own pool; a row we
    # cannot give an image is HELD, never staged as an unpublishable promise.
    to_stage, held_media = _attach_media(gym_id, to_stage, log,
                                         picker=media_picker,
                                         host=media_host_fn)
    inserted = 0
    inserter = getattr(store, "insert_rows", None)
    if inserter is not None and to_stage:
        # Strip the transient planner-only keys the DB does not carry.
        payload = [_db_row(r) for r in to_stage]
        try:
            inserted = len(inserter(gym_id, payload) or [])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"insert failed {type(exc).__name__}",
                    "staged": 0}
    return {"ok": True, "staged": inserted, "held_recap": len(held_recap),
            "held_media": len(held_media),
            "thinned": len(arc_rows) - len(thinned),
            "grade": (grade.total if grade else None),
            "letter": (grade.letter if grade else None), "months": months}


def backfill_missing_media(store, gym_id, event_id, *, statuses=("pending", "approved"),
                            picker=None, host=None, logger=None):
    """Give a real photo to STALE arc rows that were staged with no image_url.

    stage_arc's own media-attach guard (_attach_media, below) HOLDS an image-less arc
    row out of staging today, but that guard did not always exist: rows inserted before
    it shipped (Pete/CrossFit Zanshin, Bring a Friend Week + the first pass of Back To
    School Special, both 2026-08-30) sit forever with image_url='', reaching the
    approval card with no photo and permanently failing publish_guard's media_missing
    check. This is the one-time (or periodic) catch-up for exactly that stale state —
    it changes NOTHING about the guard itself, which already prevents the problem for
    every event created from here on.

    Only rows whose status is in `statuses` (default pending/approved — never denied or
    killed rows, which a human or the system already decided against) AND whose
    image_url is currently empty are touched. Each is given the SAME real-photo pool
    pick + host that _attach_media uses (tenant-isolated, reuse-cooldown respected,
    usage stamped), then PATCHed through store.patch_media — id+gym_id scoped, and
    itself re-confirms the row still has no image before writing (never clobbers a row
    a human or a later pass already gave a photo, never crosses to a different row).
    Only image_url + source_media_asset_id are written; caption/status/date are never
    touched.

    Returns {"backfilled": [row_id, ...], "held": n} — `held` is how many candidate
    rows still could not get an image (pool exhausted / hosting failed), left exactly
    as they were for the next pass."""
    log = logger or (lambda m: print(f"[event-calendar] {m}"))
    lister = getattr(store, "list_event_rows", None)
    if lister is None:
        log(f"backfill_missing_media: store has no list_event_rows; nothing to do")
        return {"backfilled": [], "held": 0}
    rows = lister(gym_id, event_id) or []
    candidates = [r for r in rows
                  if str(r.get("status") or "").lower() in statuses
                  and not (r.get("image_url") or "").strip()]
    if not candidates:
        return {"backfilled": [], "held": 0}
    kept, held = _attach_media(gym_id, candidates, log, picker=picker, host=host)
    backfilled = []
    patcher = getattr(store, "patch_media", None)
    for row in kept:
        rid = row.get("id")
        if not rid or patcher is None:
            continue
        updated = patcher(gym_id, rid, row.get("image_url") or "",
                          row.get("source_media_asset_id") or "")
        if updated is not None:
            backfilled.append(rid)
    return {"backfilled": backfilled, "held": len(held)}


def top_up_arc(store, event, *, today=None, avatar=None, logger=None,
               media_picker=None, media_host_fn=None):
    """Re-plan an ACTIVE (scheduled/live) event's arc from `today` and stage ONLY the
    beats the calendar is missing.

    stage_arc's occupancy guard makes this idempotent: a date this event already holds
    with a live row (pending/approved/published/killed/...) or a twice-denied slot is
    skipped, so the only rows added are beats with no live row — a beat the client
    denied once (deny = recreate, same contract as the deny-backfill lane) or one that
    was never staged at all (guard-denied at publish, or dropped by the since-fixed
    overlap-thin bug). Every added row lands 'pending' behind the approval gate.
    Never publishes, never touches existing rows.

    Born 2026-08-31: Pete/CrossFit Zanshin's Back To School Special reached the night
    before its start date with every arc row denied and nothing re-staging them — the
    promo would have silently not run."""
    from . import event_engine as _ee
    if isinstance(event, dict):
        event = ge.GymEvent.from_row(event)
    rows = _ee.plan_event_arc(event, today=today, avatar=avatar, logger=logger)
    if not rows:
        return {"ok": True, "staged": 0, "reason": "no arc rows planned"}
    return stage_arc(store, event, rows, logger=logger,
                     media_picker=media_picker, media_host_fn=media_host_fn,
                     gate="advisory")


def _attach_media(gym_id, rows, log, *, picker=None, host=None):
    """Give every image-less arc row a real photo from the gym's OWN pool.

    Returns (rows_with_media, held_rows). A row that already carries an image_url is
    untouched. A row we cannot give an image is HELD OUT of staging: an Instagram feed
    post with no image cannot publish, so staging one only creates a card the client
    approves and then watches fail.

    Reuses the same selector + host the Drive lane uses, so tenant isolation, the
    eligibility gate and the reuse cooldown all apply. Best effort per row; any failure
    holds that row rather than sinking the arc."""
    need = [r for r in rows if not (r.get("image_url") or "").strip()]
    if not need:
        return rows, []
    try:
        from . import gym_media_selector as _sel, media_host
        from .integrations import drive_client as _dc
    except Exception as exc:  # noqa: BLE001 - no media lane available: hold, never stage blind
        log(f"event media: lane unavailable ({type(exc).__name__}); "
            f"holding {len(need)} image-less row(s)")
        return [r for r in rows if r not in need], list(need)

    picker = picker or (lambda exclude: _sel.pick_media(gym_id, exclude_ids=exclude))
    kept, held, used = [], [], []
    for row in rows:
        if (row.get("image_url") or "").strip():
            kept.append(row)
            continue
        asset = None
        try:
            asset = picker(tuple(used))
        except Exception as exc:  # noqa: BLE001
            log(f"event media: pick failed ({type(exc).__name__})")
        if not asset:
            held.append(row)
            continue
        try:
            url = (host or _host_asset)(asset, gym_id, _dc)
        except Exception as exc:  # noqa: BLE001
            log(f"event media: host failed for {asset.get('id')} ({type(exc).__name__})")
            url = ""
        if not url:
            held.append(row)
            continue
        row = dict(row)
        row["image_url"] = url
        row["source_media_asset_id"] = str(asset.get("id") or "")
        used.append(asset.get("id"))
        try:
            _sel.stamp_use(asset, gym_id, str(row.get("post_date") or ""))
        except Exception:  # noqa: BLE001 - the stamp is best effort
            pass
        kept.append(row)
    if held:
        log(f"event media: HELD {len(held)} row(s) with no available photo "
            f"(an image-less feed post cannot publish); the gym needs more media")
    return kept, held


def _host_asset(asset, gym_id, drive_mod):
    """Download one pool asset and host it, returning the public url ("" on failure)."""
    import os
    import tempfile
    from . import media_host
    fid = str(asset.get("drive_file_id") or asset.get("id") or "")
    if not fid:
        return ""
    suffix = os.path.splitext(str(asset.get("title") or ""))[1] or ".jpg"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        drive_mod.DriveClient().download(fid, tmp)
        return media_host.host_media(tmp, gym_id) or ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# Transient, planner-only keys that are NOT content_calendar columns and must be
# stripped before an insert (arc_kind/recap_blocked/arc_note are engine state).
_TRANSIENT_KEYS = ("arc_kind", "recap_blocked", "arc_note")


def _db_row(row):
    return {k: v for k, v in (row or {}).items() if k not in _TRANSIENT_KEYS}


def cancel_event(store, gym_id, event_id, *, ended=False, logger=None):
    """Cancel (or mark ended) an event: flip every PENDING arc row for `event_id`
    denied with reject_reason. Approved/published rows are LEFT (a human owns them).
    Returns a summary. Reason is event_ended when `ended` else event_cancelled."""
    log = logger or (lambda m: print(f"[event-calendar] {m}"))
    reason = REJECT_ENDED if ended else REJECT_CANCELLED
    rows = _event_rows(store, gym_id, event_id)
    denier = getattr(store, "deny_with_reason", None)
    denied = 0
    for row in rows:
        if _status(row) in _WIPEABLE and row.get("id") and denier is not None:
            try:
                if denier(gym_id, row["id"], reason):
                    denied += 1
            except Exception as exc:  # noqa: BLE001
                log(f"cancel_event: deny {row.get('id')} failed {type(exc).__name__}")
    return {"ok": True, "denied": denied, "reason": reason}


def _event_rows(store, gym_id, event_id):
    """Every content_calendar row carrying this event_id for the gym. Uses the store's
    event-scoped reader when present, else filters a month read. Gym-scoped."""
    getter = getattr(store, "list_event_rows", None)
    if getter is not None:
        try:
            return getter(gym_id, event_id) or []
        except Exception:
            return []
    return []


def guard_publish(store, event, row, *, http=None, today=None, logger=None):
    """The per-publish guard for an event arc row (called by the autopublisher before
    the network send). Returns (allowed, reason):
      * event ended/cancelled -> (False, event_ended|event_cancelled) and the row is
        flipped back to pending + reject_reason (posting stops dead).
      * a provided offer link that is DEAD -> (False, event_link_dead), the row flips
        back to pending + reject_reason and an alert fires (never posts a dead link).
      * a blocked recap (no real media) -> (False, 'recap_blocked').
      * otherwise -> (True, '').
    Gym-scoped through the store's id+gym_id filters. Never publishes."""
    from . import event_status as _es
    log = logger or (lambda m: print(f"[event-calendar] {m}"))
    if isinstance(event, dict):
        event = ge.GymEvent.from_row(event)
    is_recap = str(row.get("arc_kind") or "") == ge.RECAP
    # Recap gated on real media (never stock, never invented).
    if is_recap and not event.has_media:
        return False, "recap_blocked"
    # A cancelled event blocks EVERY remaining publish, recap included.
    if event.status == "cancelled":
        _revert(store, event.gym_id, row, REJECT_CANCELLED, log)
        return False, REJECT_CANCELLED
    # Offer-record date gate. The RECAP is the ONE arc post allowed AFTER the window
    # (it is the post-event proof, dated T+2), so it publishes once the event has ended
    # AS LONG AS real media exists (checked above). Every other arc post is blocked once
    # the window passes (posting stops dead).
    if not is_recap and not _es.publish_allowed(event, today=today):
        _revert(store, event.gym_id, row, REJECT_ENDED, log)
        return False, REJECT_ENDED
    # Dead-link guard.
    if event.link and not _es.verify_link(event.link, http=http):
        _revert(store, event.gym_id, row, REJECT_DEAD_LINK, log)
        try:
            from . import ops_alerts
            ops_alerts.alert(
                f"event link dead: {event.gym_id}/{event.id} offer link is not "
                f"reachable; the arc row was held back to pending, not published.")
        except Exception:
            pass
        return False, REJECT_DEAD_LINK
    return True, ""


def _revert(store, gym_id, row, reason, log):
    """Flip an arc row back to pending with reject_reason (the publisher's revert path).
    Uses the store's mark_publish_failed when present, else deny_with_reason as a
    fallback. Never raises out."""
    rid = row.get("id")
    if not rid:
        return
    fn = getattr(store, "mark_publish_failed", None)
    try:
        if fn is not None:
            fn(rid, revert_status="pending", reject_reason=reason)
            return
        denier = getattr(store, "deny_with_reason", None)
        if denier is not None:
            denier(gym_id, rid, reason)
    except Exception as exc:  # noqa: BLE001
        log(f"_revert {rid} failed {type(exc).__name__}")


def _a_threshold():
    from agent.calendar_grade import A_THRESHOLD
    return A_THRESHOLD
