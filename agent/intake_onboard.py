"""
intake-onboard: one command from a client's intake payload to an onboarded account.

    python -m agent intake-onboard --account <key> --file <intake.md|.txt|.pdf>
                                   [--month YYYY-MM] [--write-plan]

Chains the existing onboarding steps in order and prints one report:

  1. BIBLE    draft-bible on the intake (held for approval under
              brand_voice/drafts/<key>/, NEVER auto-activated). Skipped when the
              draft already exists (idempotent) so a re-run cannot clobber a
              reviewed draft, and an approved (active) bible is never touched by
              construction (drafts live in their own folder).
  2. SOURCES  category sections in the intake land as PENDING client sources for
              the account (never auto-approved; the drafting path cannot read
              them until a human approves). Deduped against everything already
              stored for the account, so a re-run adds nothing twice.
  3. LIBRARY  when the account's library has media: dam near-dupe scan. House
              concept regeneration (regen-library) is LASSO's builder and does
              not apply to a client's uploaded photos; it is reported as such.
  4. PLAN     plan-month preview for the target month (write only with
              --write-plan; respects AGENT_PLAN_MONTH_ENABLED).
  5. PREFLIGHT the standard 8-check report, printed last.

Manual-only by design (like draft-bible / seed-sources / capture-baseline): no
flag, never scheduled, nothing in the agent imports it. Every chained step keeps
its own gates. Idempotent: safe to re-run at any point during onboarding.
"""

import os
import sys
from datetime import date

from . import client_sources
from .accounts import get_account

_MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov")


def _load_text(path):
    """The intake payload text; PDF extracted via the doc-intake reader."""
    if path.lower().endswith(".pdf"):
        from .doc_intake import _extract_text
        return _extract_text(path)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _media_count(library_path):
    if not library_path or not os.path.isdir(library_path):
        return 0
    return sum(1 for n in os.listdir(library_path)
               if os.path.splitext(n)[1].lower() in _MEDIA_EXTS)


def _step_bible(account_key, text, report):
    from .bible_drafter import DRAFTS_DIR, draft_bible
    out_dir = os.path.join(DRAFTS_DIR, account_key)
    bible_path = os.path.join(out_dir, "lasso_voice.md")
    if os.path.exists(bible_path):
        report["bible"] = f"draft exists, not overwritten ({bible_path})"
        return
    bible, proof = draft_bible(account_key, text)
    os.makedirs(out_dir, exist_ok=True)
    with open(bible_path, "w", encoding="utf-8") as fh:
        fh.write(bible)
    with open(os.path.join(out_dir, "social_proof.md"), "w", encoding="utf-8") as fh:
        fh.write(proof)
    report["bible"] = f"drafted, held for approval ({bible_path})"


def _step_sources(account_key, text, report):
    from .seed_sources import parse_bundle
    bundle = parse_bundle(text)
    # Only the six client categories; the bible's own sections and any stray
    # headings are ignored here, never a hard error for the whole chain.
    bundle = {k: v for k, v in bundle.items()
              if k in client_sources.CLIENT_CATEGORIES}
    existing = {(s.category, s.text) for s in client_sources.all_sources(account_key)}
    fresh = {}
    skipped_dupes = 0
    for cat, items in bundle.items():
        for text_item, cite in items:
            if (cat, text_item) in existing:
                skipped_dupes += 1
                continue
            fresh.setdefault(cat, []).append((text_item, cite))
    if not fresh:
        report["sources"] = (f"nothing new ({skipped_dupes} already stored)"
                             if skipped_dupes else "no source sections in the intake")
        return
    created = client_sources.submit_intake(account_key, fresh, status="pending")
    note = f"{len(created)} landed PENDING (approve before drafting)"
    if skipped_dupes:
        note += f", {skipped_dupes} duplicate(s) skipped"
    report["sources"] = note


def _step_library(account, report):
    lib = account.library_path()
    n = _media_count(lib)
    if n == 0:
        report["library"] = f"no media yet ({lib}); scan + plan will have little to do"
        return
    try:
        from . import dam
        groups = dam.mark_near_dupes(lib)
        report["library"] = (f"{n} media item(s), near-dupe scan done "
                             f"({len(groups)} dupe group(s)). regen-library not "
                             "applicable: it builds LASSO house concepts, a client "
                             "library is the gym's own uploads.")
    except Exception as e:
        report["library"] = f"{n} media item(s); scan failed: {type(e).__name__}: {e}"


def _step_plan(account, month, write_plan, report):
    from .plan_month import plan_month
    out = plan_month(account.key, month, library_path=account.library_path(),
                     write=write_plan)
    if out is None:
        report["plan"] = "skipped (AGENT_PLAN_MONTH_ENABLED off)"
        return
    mode = "written" if write_plan else "preview"
    report["plan"] = (f"{month} {mode}: {len(out.get('planned', []))} day(s) "
                      f"planned, {len(out.get('skipped', []))} open")


def run_onboard(account_key, file_path, month=None, write_plan=False):
    """The whole chain. Returns {"account", "steps": {...}} and prints nothing
    itself (the CLI prints). Raises only on a missing account or unreadable file;
    every downstream step reports failure in-line instead of killing the chain."""
    account = get_account(account_key)
    if account is None:
        raise ValueError(f"no account matches '{account_key}'; add the Account "
                         "entry (onboard-client) before intake-onboard")
    text = _load_text(file_path)
    month = month or date.today().isoformat()[:7]
    report = {}
    _step_bible(account_key, text, report)
    _step_sources(account_key, text, report)
    _step_library(account, report)
    _step_plan(account, month, write_plan, report)
    return {"account": account_key, "steps": report}


_USAGE = ("usage: python -m agent intake-onboard --account <key> --file <path> "
          "[--month YYYY-MM] [--write-plan]")


def cli(args):
    account_key = path = month = None
    write_plan = False
    i = 0
    while i < len(args):
        if args[i] == "--account" and i + 1 < len(args):
            account_key = args[i + 1]; i += 2; continue
        if args[i] == "--file" and i + 1 < len(args):
            path = args[i + 1]; i += 2; continue
        if args[i] == "--month" and i + 1 < len(args):
            month = args[i + 1]; i += 2; continue
        if args[i] == "--write-plan":
            write_plan = True; i += 1; continue
        print(f"unrecognized: {args[i]}\n{_USAGE}")
        sys.exit(2)
    if not account_key or not path:
        print(_USAGE)
        sys.exit(2)
    if not os.path.isfile(path):
        print(f"intake-onboard: no file at {path}")
        sys.exit(2)
    try:
        out = run_onboard(account_key, path, month=month, write_plan=write_plan)
    except ValueError as e:
        print(f"intake-onboard: {e}")
        sys.exit(1)
    print(f"intake-onboard: {account_key} <- {path}")
    for step in ("bible", "sources", "library", "plan"):
        print(f"  {step:<8} {out['steps'].get(step, '(not run)')}")
    # Preflight last: the honest is-it-safe-to-draft verdict for this account.
    print()
    from .preflight import check_account, print_report
    print_report(check_account(get_account(account_key)))
    sys.exit(0)
