#!/usr/bin/env python3
"""Seed the website-intake for CrossFit Destind (crossfitdestind9acfa).

WHY THIS EXISTS
----------------
Echo auto-intake halted with "no domain on record" for gym crossfitdestind9acfa
because the gym has no entry in agent/portal_domains.py.  The intake code is
correct — it refuses to guess a domain.  This script supplies the real, verified
domain at run time so intake can proceed without waiting for a code deploy.

WHAT THIS SCRIPT DOES
-----------------------
- Passes the domain you provide via --domain to agent.website_intake.intake_from_website,
  bypassing the registry lookup (the function accepts an explicit domain arg).
- Lands sourced facts as pending — nothing publishes until a human approves the intake.
- Does NOT touch billing, Stripe, pixel, CAPI, ad budgets, targeting, or published posts.
- After a successful live run, add the domain to agent/portal_domains.py under both the
  human name and the base key so future auto-intake runs resolve without --domain.

IDEMPOTENCY
-----------
intake_from_website skips any gym that already has sources (approved or pending) unless
--force is passed.  Running this script a second time without --force is safe — it will
report "gym already has sources" and exit cleanly without creating duplicates.

USAGE
-----
    # Preview what would happen — no writes:
    python3 scripts/seed_intake_crossfitdestind.py --domain YOURDOMAIN.com --dry-run

    # Live run — seeds intake from the gym's website:
    python3 scripts/seed_intake_crossfitdestind.py --domain YOURDOMAIN.com

    # Re-seed even if sources already exist (use only if intake was corrupted):
    python3 scripts/seed_intake_crossfitdestind.py --domain YOURDOMAIN.com --force

Run from the repo root on the deployed Railway instance (so it shares the live Supabase
creds and the /data volume):
    railway ssh --service echo -- python3 scripts/seed_intake_crossfitdestind.py \
        --domain YOURDOMAIN.com --dry-run
    railway ssh --service echo -- python3 scripts/seed_intake_crossfitdestind.py \
        --domain YOURDOMAIN.com
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "crossfitdestind9acfa"
_GYM_NAME = "CrossFit Destind"

_FORBIDDEN_FLAGS = {"--billing", "--stripe", "--pixel", "--capi", "--ad-budget",
                    "--targeting", "--publish", "--approve"}


def _sep():
    print("-" * 64)


def _parse_args():
    args = sys.argv[1:]
    for flag in args:
        if flag.lower() in _FORBIDDEN_FLAGS:
            print(f"ERROR: flag '{flag}' is not allowed in this script.")
            sys.exit(1)

    domain = None
    dry_run = "--dry-run" in args
    force = "--force" in args

    i = 0
    while i < len(args):
        if args[i] == "--domain" and i + 1 < len(args):
            domain = args[i + 1].strip()
            i += 2
        else:
            i += 1

    if not domain:
        print("ERROR: --domain DOMAIN is required.")
        print("  Provide the gym's real, verified website domain, e.g.")
        print("  --domain crossfitdestind.com")
        sys.exit(1)

    # Strip accidental protocol prefix — the intake fetcher adds its own.
    domain = domain.lstrip("https://").lstrip("http://").rstrip("/")

    return domain, dry_run, force


def _check_existing_sources():
    """Return True if the gym already has sources in any status."""
    try:
        from agent import client_sources
        account_key = f"{_BASE}_ig"
        sources = client_sources.all_sources(account_key)
        return bool(sources)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: could not check existing sources: {exc}")
        return False


def _dry_run(domain, force):
    _sep()
    print(f"DRY RUN -- {_GYM_NAME} ({_BASE})")
    _sep()
    print(f"Domain to scrape : {domain}")
    has_sources = _check_existing_sources()
    if has_sources and not force:
        print("Existing sources : YES (intake would be SKIPPED; pass --force to override)")
    elif has_sources and force:
        print("Existing sources : YES (--force set; intake would REPLACE them)")
    else:
        print("Existing sources : none (intake would proceed)")
    print()
    print("No writes were performed.")
    print()
    print("AFTER a successful live run, also add to agent/portal_domains.py:")
    print(f'    "{_GYM_NAME.lower()}": {{"domain": "{domain}"}},')
    print(f'    "{_BASE}": {{"domain": "{domain}"}},')
    _sep()


def _live_run(domain, force):
    from agent.website_intake import intake_from_website

    _sep()
    print(f"LIVE RUN -- seeding intake for {_GYM_NAME} ({_BASE})")
    print(f"Domain : {domain}  force={force}")
    _sep()

    result = intake_from_website(_BASE, domain=domain, force=force)

    _sep()
    if result.get("ok"):
        print(f"Done. Landed {result.get('landed', 0)} source(s) "
              f"with status '{result.get('status', '?')}' from {result.get('domain', domain)}.")
        print("Every source is PENDING -- nothing publishes until approved.")
        if result.get("bible"):
            print(f"Bible note: {result['bible']}")
        print()
        print("NEXT STEP: add the domain to agent/portal_domains.py so future")
        print("auto-intake runs resolve this gym without --domain:")
        print(f'    "{_GYM_NAME.lower()}": {{"domain": "{domain}"}},')
        print(f'    "{_BASE}": {{"domain": "{domain}"}},')
    else:
        reason = result.get("reason", "(no reason)")
        print(f"Intake returned ok=False: {reason}")
        if "already has sources" in reason:
            print("  Re-run with --force if you want to replace existing sources.")
        sys.exit(1)
    _sep()


def main():
    domain, dry_run, force = _parse_args()

    print(f"{_GYM_NAME} intake seed script  (dry_run={dry_run}  force={force})")
    print()

    if dry_run:
        _dry_run(domain, force)
    else:
        _live_run(domain, force)


if __name__ == "__main__":
    main()
