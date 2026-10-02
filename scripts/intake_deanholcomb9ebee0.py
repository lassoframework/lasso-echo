#!/usr/bin/env python3
"""One-off ops script: website auto-intake for gym deanholcomb9ebee0.

WHY THIS EXISTS
----------------
Support ticket 47c11985-86ab-4a18-8f5a-f4a6efc390fb, 2026-09-11. Echo raised:
  "ECHO ALERT: could not auto-intake deanholcomb9ebee0: no domain on record
   (pass --domain or add the gym to portal_domains)"

deanholcomb9ebee0 is not present in agent/portal_domains._REGISTRY, so the
auto-intake fleet sweep has no domain to scrape and skips the gym.

No catalog action covers this case; the resolution is:
  1. Supply the gym's real domain via --domain (the operator verifies it first).
  2. This script runs website_intake.intake_from_website once for deanholcomb9ebee0
     using that domain, landing sources as PENDING for human review.
  3. A permanent entry for deanholcomb9ebee0 in agent/portal_domains.py (see the
     "AFTER RUNNING" section below) lets future auto-intake runs resolve this
     gym without needing --domain again.

This script NEVER touches billing, Stripe, pixel, CAPI, ad budgets, ad
targeting, published posts, or anything medical/legal.

WHAT THIS SCRIPT DOES
----------------------
- Calls agent.website_intake.intake_from_website ONCE for deanholcomb9ebee0
  using the operator-supplied --domain argument.
- The domain is used directly; portal_domains is not consulted.
- Sources land PENDING (or whatever intake_status() resolves to), gated by the
  standard AGENT_INTAKE_AUTO_APPROVE flag (default OFF).
- Idempotent guard: if the gym already has sources, the script prints a warning
  and exits with status 0 unless --force is also passed.
- Approves nothing, publishes nothing, changes no gate or billing.

AFTER RUNNING (permanent fix)
------------------------------
Add a permanent entry to agent/portal_domains.py so future fleet sweeps do not
need --domain:

    # Looked up + verified YYYY-MM-DD (ticket 47c11985).
    "deanholcomb9ebee0": {
        "domain": "<the-real-domain-you-verified>",
    },

No other code change is needed. The compact-index builder in portal_domains.py
will pick it up automatically on next deploy.

USAGE
-----
    # Preview only -- no writes:
    python3 scripts/intake_deanholcomb9ebee0.py --domain <gym-website.com> --dry-run

    # Live run -- lands sources as PENDING:
    python3 scripts/intake_deanholcomb9ebee0.py --domain <gym-website.com>

    # Re-intake if sources already exist:
    python3 scripts/intake_deanholcomb9ebee0.py --domain <gym-website.com> --force

Run from the repo root on the deployed Railway instance (shared volume + creds):
    railway ssh --service echo -- python3 scripts/intake_deanholcomb9ebee0.py \\
        --domain <gym-website.com> --dry-run
    railway ssh --service echo -- python3 scripts/intake_deanholcomb9ebee0.py \\
        --domain <gym-website.com>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "deanholcomb9ebee0"

_FORBIDDEN_FLAGS = {"--billing", "--stripe", "--pixel", "--capi", "--ad-budget",
                    "--targeting", "--publish", "--approve"}


def _sep():
    print("-" * 64)


def _parse_args():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args

    for flag in args:
        if flag.lower() in _FORBIDDEN_FLAGS:
            print(f"ERROR: flag '{flag}' is not allowed in this script.")
            sys.exit(1)

    domain = None
    for i, arg in enumerate(args):
        if arg == "--domain" and i + 1 < len(args):
            domain = args[i + 1].strip()
        elif arg.startswith("--domain="):
            domain = arg.split("=", 1)[1].strip()

    return dry_run, force, domain


def _dry_run(domain, force):
    from agent import client_sources

    _sep()
    print(f"DRY RUN -- intake for {_BASE}")
    _sep()
    account_key = f"{_BASE}_ig"
    print(f"Gym base key  : {_BASE}")
    print(f"Account key   : {account_key}")
    print(f"Domain to use : {domain}")

    existing = client_sources.all_sources(account_key)
    if existing and not force:
        print(f"Existing sources ({len(existing)}) found -- would SKIP (pass --force to override).")
    elif existing:
        print(f"Existing sources ({len(existing)}) found -- --force set, would PROCEED anyway.")
    else:
        print("No existing sources found -- would proceed with intake.")

    print()
    print("No writes performed in dry-run mode.")
    print()
    print("NEXT STEP (permanent fix): add to agent/portal_domains.py:")
    print(f"    \"{_BASE}\": {{")
    print(f"        \"domain\": \"{domain}\",")
    print(f"    }},")
    _sep()


def _live_run(domain, force):
    from agent.website_intake import intake_from_website

    _sep()
    print(f"LIVE RUN -- website auto-intake for {_BASE}")
    print(f"Domain: {domain}  force={force}")
    _sep()

    result = intake_from_website(_BASE, domain=domain, force=force)

    _sep()
    if result.get("ok"):
        landed = result.get("landed", 0)
        status = result.get("status", "?")
        print(f"Done. Landed {landed} source(s) with status '{status}'.")
        bible = result.get("bible")
        if bible:
            print(f"Bible note: {bible}")
        print()
        print("Sources are PENDING -- a human must approve before any draft uses them.")
        print()
        print("NEXT STEP (permanent fix): add to agent/portal_domains.py so future")
        print("auto-intake runs can resolve this gym without --domain:")
        print(f"    # Looked up + verified {__import__('datetime').date.today().isoformat()}"
              f" (ticket 47c11985).")
        print(f"    \"{_BASE}\": {{")
        print(f"        \"domain\": \"{domain}\",")
        print(f"    }},")
    else:
        reason = result.get("reason", "(no reason)")
        print(f"Intake returned ok=False: {reason}")
        if "already has sources" in reason:
            print("  The gym already has sources. Pass --force to re-intake anyway.")
        sys.exit(1)
    _sep()


def main():
    dry_run, force, domain = _parse_args()

    if not domain:
        print(
            "ERROR: --domain is required.\n"
            "  Verify the gym's real website domain first, then:\n"
            f"    python3 scripts/intake_deanholcomb9ebee0.py --domain <gym-website.com>\n"
            f"  Use --dry-run to preview without writing."
        )
        sys.exit(1)

    print(f"intake_deanholcomb9ebee0 (dry_run={dry_run}, force={force})")
    print()

    if dry_run:
        _dry_run(domain, force)
    else:
        _live_run(domain, force)


if __name__ == "__main__":
    main()
