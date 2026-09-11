#!/usr/bin/env python3
"""Restage the next 30 days of CrossFit Reverb's content calendar.

WHY THIS EXISTS
----------------
Support ticket 4941e162-2923-495f-8efb-d2554dea5aec, Dean Holcomb / CrossFit Reverb,
2026-09-11. Dean's four asks, verbatim from the ticket:
  1. captions were "almost the same as one another"
  2. every caption closed on "How do I get started with training at CrossFit Reverb?"
     (an FAQ heading mined into a nonsensical closing CTA)
  3. delete the current proposed (pending) posts and rebuild the next 30 days with
     wider caption variety and a CTA that makes sense
  4. he uploaded MANY MORE photos and videos to the connected Drive folder and saw NO
     reels or video stories -- he expects at least one video reel/story per week

Ground truth checked before writing this script:
  - agent/cta_self_question_gate.py (already on main, wired into client_month_run.py,
    default ON) already blocks item 2 for every future generation.
  - Railway prod has GYM_DRIVE_CONNECT=true (global) and GYM_DRIVE_STAGE=true, and
    CrossFit Reverb (crossfitreverb30b5b2) has 47 eligible video assets in media_asset.
    The video pre-pass in client_month_run.py (append_gym_drive_drafts with
    video_beats_only=True) already claims video beats before Lane A. Video routing was
    NOT broken.
  - The actual defect: content_calendar showed FIVE separate insert timestamps within
    about two hours (multiple retried/overlapping rebuild attempts, consistent with the
    FIXER's stale-run recovery kicking in twice), several landing near-duplicate
    captions on the SAME post_date as PENDING rows side by side. That is only possible
    when two build_client_month calls for this gym overlapped. agent/build_lock.py (this
    same PR) closes that: a second concurrent call for the same gym is now refused
    cleanly instead of racing.

WHAT THIS SCRIPT DOES
----------------------
- Calls agent.client_month_run.build_client_month ONCE for CrossFit Reverb, which does
  a gym-scoped delete-then-insert across every month the new 30-day span touches
  (agent/client_month_run.py _apply / portal_calendar_store.delete_month).
- Deletes ONLY wipeable rows (status NULL/pending/draft/queued) for gym_id
  crossfitreverb30b5b2. Approved, published, denied, killed, and coach_review rows are
  NEVER touched (delete_month's preserve_human=True default, plus preserve_and_prune).
- Every new row lands PENDING, drawing from CrossFit Reverb's connected Drive pool
  (photos + videos) with every existing gate enforced: A+ / banned-word, the CTA
  self-question gate, the same-concept-per-day guard, the video pre-pass.
- Never publishes anything, never touches billing/Stripe/pixel/CAPI/ad budget/targeting.
- Idempotent: safe to run twice. The per-gym build lock (agent/build_lock.py) refuses a
  second concurrent invocation rather than racing it.

USAGE
-----
    python3 scripts/restage_crossfit_reverb.py --dry-run     # preview only, no writes
    python3 scripts/restage_crossfit_reverb.py                # live rebuild

Run from the repo root on the deployed Railway instance (so it shares the /data volume
and the live Supabase creds):
    railway ssh --service echo -- python3 scripts/restage_crossfit_reverb.py --dry-run
    railway ssh --service echo -- python3 scripts/restage_crossfit_reverb.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "crossfitreverb30b5b2"
_IG_KEY = f"{_BASE}_ig"
_GYM_NAME = "CrossFit Reverb"
_DAYS = 30

_FORBIDDEN_FLAGS = {"--billing", "--stripe", "--pixel", "--capi", "--ad-budget",
                    "--targeting", "--publish", "--approve"}


def _sep():
    print("-" * 64)


def _resolve():
    """Resolve account, library path, voice, banned words, store. Returns a dict;
    any missing piece is None with a WARN already printed."""
    from agent import accounts as _accts
    from agent import config
    from agent.voice import load_voice
    from agent.client_media_sync import _resolve_client_voice_path, _banned_words_for

    account = _accts.get_account(_IG_KEY)
    if account is None:
        print(f"WARN: account '{_IG_KEY}' not found in the account registry.")

    lib_dir = os.path.join(config.LIBRARY_PATH, _BASE)
    if not os.path.isdir(lib_dir):
        print(f"NOTE: no content_library directory at {lib_dir} -- "
              "CrossFit Reverb is a Drive-only gym, this is expected.")

    repo_voice_path = os.path.join("brand_voice", _BASE, "lasso_voice.md")
    if account is not None:
        repo_voice_path = getattr(account, "voice_doc_path", lambda: repo_voice_path)()
    voice_path = _resolve_client_voice_path(_BASE, repo_voice_path)
    voice = load_voice(voice_path)
    if voice is None:
        print(f"ERROR: could not load a voice doc from '{voice_path}'.")

    banned_words = _banned_words_for(_BASE)

    store = None
    if config.portal_calendar_supabase_enabled():
        try:
            from agent.portal_calendar_store import SupabaseCalendarStore
            store = SupabaseCalendarStore()
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: could not create SupabaseCalendarStore: {exc}")
    else:
        print("WARN: portal Supabase creds absent (PORTAL_SUPABASE_* env vars).")

    return {"account": account, "lib_dir": lib_dir, "voice": voice,
            "banned_words": banned_words, "store": store}


def _dry_run(ctx, start, days):
    from agent.client_month_run import _client_media_count
    from agent import build_lock

    _sep()
    print(f"DRY RUN -- {_GYM_NAME} ({_BASE})")
    _sep()
    print(f"Start date   : {start.isoformat()}")
    print(f"Days         : {days}")
    print(f"Library path : {ctx['lib_dir']}")
    print(f"Usable media : {_client_media_count(ctx['lib_dir'])} local file(s)")
    print(f"Banned words : {list(ctx['banned_words']) or '(none)'}")
    print(f"Build lock   : {'HELD (a live build is in progress right now)' if build_lock.is_locked(_BASE) else 'free'}")

    store = ctx["store"]
    if store is None:
        print("WARN: no calendar store -- cannot read existing rows or write anything.")
        _sep()
        return

    months = sorted({(start + timedelta(days=i)).isoformat()[:7] for i in range(days)})
    rows = []
    for month in months:
        try:
            rows.extend(store.list_month(_BASE, month) or [])
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: could not read {month}: {exc}")
    wipeable_statuses = {"pending", "draft", "queued", ""}
    wipeable = [r for r in rows if (r.get("status") or "").lower() in wipeable_statuses]
    locked = [r for r in rows if (r.get("status") or "").lower() not in wipeable_statuses]
    print(f"Existing rows in the next {days} days : {len(rows)}")
    print(f"  wipeable (will be deleted+rebuilt)  : {len(wipeable)}")
    print(f"  locked / human-owned (kept as is)   : {len(locked)}")
    print()
    print("Run without --dry-run to apply this rebuild.")
    _sep()


def _live_run(ctx, start, days):
    from agent.client_month_run import build_client_month

    if ctx["account"] is None:
        print(f"ERROR: account '{_IG_KEY}' not found -- cannot build.")
        sys.exit(1)
    if ctx["voice"] is None:
        print("ERROR: voice doc missing -- cannot build.")
        sys.exit(1)
    if ctx["store"] is None:
        print("ERROR: no calendar store -- cannot write rows.")
        sys.exit(1)

    _sep()
    print(f"LIVE RUN -- restaging {_GYM_NAME} ({_BASE})")
    print(f"Start: {start.isoformat()}  Days: {days}")
    _sep()
    result = build_client_month(
        ctx["account"], _BASE, start.isoformat(), days,
        voice=ctx["voice"], library_path=ctx["lib_dir"], store=ctx["store"],
        banned_words=tuple(ctx["banned_words"]),
        logger=lambda m: print(f"  {m}"),
    )
    _sep()
    if result.get("ok"):
        print(f"Done. Inserted {result.get('upserted', 0)} row(s) across "
              f"{result.get('days', 0)} day(s). "
              f"Skipped (banned word): {result.get('skipped_banned', 0)}.")
        print("Every row is PENDING -- nothing publishes until Dean approves it.")
    else:
        print(f"Build returned ok=False: {result.get('reason', '(no reason)')}")
        if result.get("reason") == "build_in_progress":
            print("  Another build is already running for this gym right now. "
                  "Wait for it to finish, then rerun this script.")
        sys.exit(1)
    _sep()


def main():
    dry_run = "--dry-run" in sys.argv
    for flag in sys.argv[1:]:
        if flag.lower() in _FORBIDDEN_FLAGS:
            print(f"ERROR: flag '{flag}' is not allowed in this script. "
                  "This script only rebuilds pending content_calendar rows.")
            sys.exit(1)

    print(f"{_GYM_NAME} restage script (dry_run={dry_run})")
    print()
    ctx = _resolve()
    start = date.today()
    if dry_run:
        _dry_run(ctx, start, _DAYS)
    else:
        _live_run(ctx, start, _DAYS)


if __name__ == "__main__":
    main()
