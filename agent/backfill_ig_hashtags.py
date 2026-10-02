"""
backfill_ig_hashtags.py — fold each gym's APPROVED hashtag line into FUTURE,
UNPUBLISHED Instagram FEED rows of the shared content_calendar that are missing it.

THE GAP THIS CLOSES (2026-09-19). The portal (/social + /calendar) serves the
STORED content_calendar caption verbatim, and the Zernio publish wire starts
from that same stored caption (zernio_publisher sends the caption rebuilt from the
row; draft.hashtags is a draft-store field the calendar row never carries).
Captions staged before the hashtag reach fix therefore show -- and would publish
-- with NO hashtags at all. This backfill rewrites the stored caption of the
eligible rows so the portal preview and the publish wire are the same exact
base copy, tags included. The existing optional mention lane may append
allowlisted @handles at publish time.

THE SELECT/FOLD RULE IS SHARED, NOT LOCAL: the pure "which tags, folded how"
logic lives in agent/ig_feed_hashtags.py (ensure_feed_tag_line), the
calendar-boundary helper built alongside this command. This module owns only
the sweep around it -- which rows are eligible, how the gym's VoiceDoc is
resolved, the dry-run report, and the approval-safe write. In short (see
ig_feed_hashtags' own docstring for the full contract):
  * Tags come from the gym's approved VoiceDoc (voice.load_voice ->
    VoiceDoc.hashtags, hex colors already filtered there) or an explicit
    operator-approved account fallback when the doc has no usable tags. Nothing
    is invented;
    a gym with no approved tags is SKIPPED, never patched.
  * The selection is the drafter's OWN (drafter._select_hashtags: brand tier
    first, deterministic rotation, capped at TemplateGenerator.HASHTAG_LIMIT =
    5). 3 to 5 tags on ONE final line; a doc approving fewer than 3 contributes
    exactly those -- never padded, never invented.
  * The existing caption body is preserved BYTE-FOR-BYTE. Pending rows stay
    pending. Approved rows return to pending because visible copy changed and
    the owner must approve the exact final post. The write atomically guards
    expected status plus the absence of a publish record.
  * NEVER touched: rows whose status is published / publishing / denied /
    killed / failed (IMMUTABLE_STATUSES, filtered client-side AND guarded
    server-side), story-format rows (stories publish with
    an empty body by design), Facebook / GBP rows (Facebook copy stays
    unchanged), variant 'candidate' rows, and past-dated rows.
  * IDEMPOTENT: a caption already ending in a compliant all-approved tag line
    is returned byte-identical by ensure_feed_tag_line, and a tag already
    present anywhere in the caption body is never duplicated onto the final
    line. A second --apply run reports 0 changes.

DRY-RUN BY DEFAULT. With no --apply the command prints exactly which rows would
change (gym, date, row id, the resulting final caption line) and writes
NOTHING. --apply performs the caption-only PATCHes.

SCOPE: explicit by hand, like backfill-section7 / account-key-reconcile --
--gym <base[,base...]> for named gyms or --all for LASSO plus every client gym
base the registry knows (calendar_autopublish.client_gym_bases). No flag arms
this into any scheduled path; it is a manual maintenance command. It never
publishes; approval and publish gates are untouched.

    python -m agent backfill-ig-hashtags --gym eng             # dry-run, one gym
    python -m agent backfill-ig-hashtags --all                 # dry-run, fleet
    python -m agent backfill-ig-hashtags --gym eng --apply     # write, one gym
"""

from datetime import date

from . import drafter as _drafter
from . import ig_feed_hashtags as _ifh
from .voice import load_voice


# Rows in these states are NEVER touched, no matter what. published/publishing
# are additionally refused server-side by patch_caption_preserve_status's race
# guard; denied/killed/failed are excluded here, at read time.
IMMUTABLE_STATUSES = frozenset({"published", "publishing", "denied", "killed", "failed"})
# Backfill is deliberately narrower than "not terminal." Only a row in one of
# these known workflow states may be atomically patched. Approved rows are
# returned to pending as content changed, so the owner sees and approves the
# exact final copy. Pending rows remain pending.
MUTABLE_STATUSES = frozenset({"pending", "approved"})

# The IG feed tag-line cap the report is framed around. ONE source: the
# drafter's own limit, the same ceiling the shared helper folds to.
TAG_LIMIT = _drafter.TemplateGenerator.HASHTAG_LIMIT  # 5

# How many calendar months forward from the current month are scanned. The
# forward book lives at most a month-ish ahead (today+31 horizons); 3 covers
# the current month, next month, and slack, and extra month reads are cheap and
# empty when nothing is staged there.
DEFAULT_MONTHS = 3


# ---------------------------------------------------------------------------
# Row eligibility + plan (PURE)
# ---------------------------------------------------------------------------

def eligible_row(row, today_iso):
    """(True, '') when the row is a FUTURE, UNPUBLISHED Instagram FEED row this
    backfill may patch, else (False, reason).

    The negative space is the safety contract: not Instagram, a story, an
    immutable status (published/publishing/denied/killed/failed), a variant
    candidate, a past or unparseable date, or no row id all exclude the row.
    An IG row with no explicit format is treated as feed -- the mirror and the
    month builders stamp stories explicitly, and a story row must never be
    patched here."""
    account = str((row or {}).get("account") or "").strip().lower()
    if account != "instagram":
        return False, "not_instagram"
    fmt = str((row or {}).get("format") or "feed").strip().lower()
    if fmt == "story":
        return False, "story"
    status = str((row or {}).get("status") or "").strip().lower()
    if status in IMMUTABLE_STATUSES:
        return False, f"status_{status}"
    if status not in MUTABLE_STATUSES:
        return False, f"status_{status or 'unknown'}"
    if str((row or {}).get("variant_status") or "active").strip().lower() != "active":
        return False, "variant_candidate"
    post_date = str((row or {}).get("post_date") or "")[:10]
    if not post_date or post_date < today_iso:
        return False, "not_future"
    if not (row or {}).get("id"):
        return False, "no_id"
    return True, ""


def _unchanged_reason(caption, voice):
    """Why ensure_feed_tag_line left this caption byte-identical (reporting
    only): nothing to tag onto, already compliant, or no approved tag left to
    add without duplicating one already present."""
    cap = caption or ""
    if not cap.strip():
        return "empty_caption"
    tag_kind = _ifh.final_tag_line_kind(cap, getattr(voice, "hashtags", None) or [])
    if tag_kind == "manual":
        return "manual_tag_line"
    if tag_kind == "compliant":
        return "already_tagged"
    return "no_tags_added"


def plan_gym(base, rows, voice, today_iso):
    """PURE diff for ONE gym: which rows would change, and why the rest skip.

    The fold itself is ig_feed_hashtags.ensure_feed_tag_line (the shared
    calendar-boundary helper): unchanged return means skip, changed return is
    the new stored caption. Returns {"changes": [{gym, row_id, post_date,
    status, new_caption, tag_line}], "skipped": {reason: count}}. No I/O; the
    same rows and voice doc always produce the same plan."""
    changes, skipped = [], {}
    for row in rows or []:
        ok, why = eligible_row(row, today_iso)
        if not ok:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        caption = row.get("caption") or ""
        new_caption = _ifh.ensure_feed_tag_line(caption, voice)
        if new_caption == caption:
            reason = _unchanged_reason(caption, voice)
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        tag_line = [ln for ln in new_caption.split("\n") if ln.strip()][-1]
        changes.append({
            "gym": base,
            "row_id": row.get("id"),
            "post_date": str(row.get("post_date") or "")[:10],
            "status": str(row.get("status") or ""),
            "new_caption": new_caption,
            "tag_line": tag_line,
        })
    return {"changes": changes, "skipped": skipped}


# ---------------------------------------------------------------------------
# Live readers (the production wiring; everything above is injectable around it)
# ---------------------------------------------------------------------------

def _voice_for_base(base):
    """The gym's approved VoiceDoc, resolved EXACTLY like production build-time
    callers (runner.draft_for_new_upload, portal_social._voice_for): the
    registry account (<base>_ig else <base>), then durable-first voice path
    resolution via client_media_sync._resolve_client_voice_path, then
    voice.load_voice. A voice doc with no usable hashtag section may use the
    account's explicit operator-approved fallback. None when the doc is
    missing/empty -- the caller SKIPS the gym (no voice doc, never invent)."""
    try:
        from dataclasses import replace

        from .accounts import approved_hashtags_for, get_account
        from .client_media_sync import _resolve_client_voice_path
        account = get_account(f"{base}_ig") or get_account(base)
        repo_path = (account.voice_doc_path() if account is not None
                     else f"brand_voice/{base}/lasso_voice.md")
        loaded = load_voice(_resolve_client_voice_path(base, repo_path))
        if loaded is None:
            return None
        tags = approved_hashtags_for(account, loaded.hashtags)
        return replace(loaded, hashtags=tags)
    except Exception:  # noqa: BLE001 - an unreadable bible is 'cannot tag', not a crash
        return None


def _default_store():
    """The shared content_calendar store, or None when Supabase creds are
    absent (a dry-run on a creds-less host still says so honestly)."""
    from .portal_calendar_store import SupabaseCalendarStore
    store = SupabaseCalendarStore()
    return store if store.available() else None


def _all_bases():
    """--all: LASSO first (its gym_id='lasso' rows are real forward-book rows),
    then every client gym base the registry knows (client_gym_bases is
    echo-clients-gated and fails closed to the hardcoded set)."""
    try:
        from .calendar_autopublish import client_gym_bases
        bases = ["lasso"] + [b for b in (client_gym_bases() or []) if b != "lasso"]
    except Exception:  # noqa: BLE001 - an unreadable registry means LASSO only
        bases = ["lasso"]
    seen, out = set(), []
    for b in bases:
        b = (b or "").strip()
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _month_iter(today_iso, count):
    """['YYYY-MM', ...] for the current month plus count-1 months forward."""
    year, mon = int(today_iso[:4]), int(today_iso[5:7])
    out = []
    for i in range(count):
        m = mon + i
        y = year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        out.append(f"{y:04d}-{m:02d}")
    return out


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def run(bases, *, store=None, voice_loader=None, today_iso=None,
        months=DEFAULT_MONTHS, apply=False, printer=print):
    """Plan (and with apply=True, write) the hashtag-line backfill.

    DRY-RUN unless apply=True: every would-change row is printed as
    'gym  date  row_id  ->  final caption line' and NOTHING is written. The
    write itself is patch_caption_for_hashtag_backfill -- caption only for a
    pending row, caption plus a pending reset for an approved row, guarded by
    id + gym_id + expected status + no publish record. Returns a summary dict; never raises out (a bad
    gym or a failed PATCH is reported and the sweep continues)."""
    today_iso = today_iso or date.today().isoformat()
    store = store if store is not None else _default_store()
    voice_loader = voice_loader or _voice_for_base
    summary = {"ok": True, "mode": "apply" if apply else "dry-run",
               "today": today_iso, "gyms": [], "changes": 0, "written": 0,
               "skipped": {}}
    if store is None:
        printer("backfill-ig-hashtags: Supabase creds not set -- cannot read "
                "content_calendar. Run on the worker (creds live there).")
        summary["ok"] = False
        summary["reason"] = "no_store"
        return summary

    mode = "APPLY" if apply else "DRY-RUN"
    printer(f"backfill-ig-hashtags [{mode}] as-of {today_iso}, "
            f"{len(bases)} gym(s), {months} month(s) forward")
    for base in bases:
        voice = voice_loader(base)
        if voice is None or not (voice.hashtags or []):
            printer(f"  {base}: SKIPPED -- no usable voice doc or 0 approved "
                    "hashtags (never invent tags)")
            summary["gyms"].append({"gym": base, "skipped_voice": True})
            continue

        rows, seen_ids = [], set()
        for month in _month_iter(today_iso, months):
            for row in store.list_month(base, month) or []:
                rid = row.get("id")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    rows.append(row)
        plan = plan_gym(base, rows, voice, today_iso)

        gym_changes = plan["changes"]
        for reason, count in sorted(plan["skipped"].items()):
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + count
        written = 0
        for ch in gym_changes:
            printer(f"  {base}  {ch['post_date']}  {ch['row_id']}  "
                    f"[{ch['status'] or '?'}]  ->  {ch['tag_line']}")
            if apply:
                try:
                    updated = store.patch_caption_for_hashtag_backfill(
                        base, ch["row_id"], ch["new_caption"],
                        expected_status=ch["status"].strip().lower())
                except Exception as exc:  # noqa: BLE001 - one bad row never stops the sweep
                    printer(f"    ! PATCH FAILED ({type(exc).__name__}); row left unchanged")
                    continue
                if updated is None:
                    # Zero rows matched: the row flipped to publishing/published
                    # between read and write (the server-side race guard) or was
                    # deleted. Reported, never retried blind.
                    printer("    ! row changed under us or is no longer safely "
                            "patchable; left untouched")
                    continue
                written += 1
        summary["changes"] += len(gym_changes)
        summary["written"] += written
        summary["gyms"].append({"gym": base, "rows": len(rows),
                                "changes": len(gym_changes), "written": written})

    verb = "patched" if apply else "would patch"
    printer(f"backfill-ig-hashtags [{mode}]: {verb} {summary['changes']} row(s)"
            + (f" ({summary['written']} written)" if apply else
               " (dry-run; re-run with --apply to write)"))
    if summary["skipped"]:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(summary["skipped"].items()))
        printer(f"  skipped: {detail}")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = ("usage: python -m agent backfill-ig-hashtags "
          "(--gym <base[,base...]> | --all) [--apply] [--months N] [--today YYYY-MM-DD]\n"
          "  DRY-RUN by default: prints each FUTURE, UNPUBLISHED Instagram FEED row\n"
          "  missing its approved hashtag line (gym, date, row id, resulting final\n"
          "  caption line). --apply keeps pending rows pending and resets changed\n"
          "  approved rows to pending for reapproval.\n"
          "  Never touches published/publishing/denied/killed/failed rows, stories,\n"
          "  or Facebook rows; never duplicates a tag already in the caption.")


def cli(argv):
    """CLI entry. Scope is explicit by hand: --gym <base[,base...]> (repeatable)
    or --all (LASSO + every client base). No scope prints usage and writes
    nothing. --help/-h prints usage and exits 0."""
    argv = list(argv or [])
    if "--help" in argv or "-h" in argv:
        print(_USAGE)
        return 0
    bases, do_all, apply = [], False, False
    months, today_iso = DEFAULT_MONTHS, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--gym", "--base", "--account") and i + 1 < len(argv):
            bases.extend(b.strip() for b in argv[i + 1].split(",") if b.strip())
            i += 2
            continue
        if a == "--all":
            do_all = True
            i += 1
            continue
        if a == "--apply":
            apply = True
            i += 1
            continue
        if a == "--months" and i + 1 < len(argv):
            try:
                months = max(1, int(argv[i + 1]))
            except ValueError:
                print(_USAGE)
                return 2
            i += 2
            continue
        if a == "--today" and i + 1 < len(argv):  # testing seam; defaults to date.today()
            today_iso = argv[i + 1]
            i += 2
            continue
        i += 1
    if do_all:
        every = _all_bases()
        bases = every + [b for b in bases if b not in every]
    if not bases:
        print(_USAGE)
        return 2
    summary = run(bases, apply=apply, months=months, today_iso=today_iso)
    return 0 if summary.get("ok") else 1


__all__ = ["IMMUTABLE_STATUSES", "MUTABLE_STATUSES", "TAG_LIMIT", "DEFAULT_MONTHS", "eligible_row",
           "plan_gym", "run", "cli"]
