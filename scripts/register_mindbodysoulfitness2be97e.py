#!/usr/bin/env python3
"""One-off ops script: add mindbodysoulfitness2be97e to Echo's dynamic account registry.

WHY THIS EXISTS
----------------
Support ticket 5b4bf7d2-c9cb-406e-abe9-ed57786226e5, 2026-09-12. Echo raised:
  "ECHO ALERT: mindbodysoulfitness2be97e: not set up to post (not_registered,
   no_sources). the portal knows this gym but Echo's account registry does not,
   so it is in neither the build nor the publish lane."

No catalog action covers gym registration. The fix is:
  1. Confirm AGENT_DYNAMIC_ACCOUNTS=true is armed on the echo worker.
  2. Confirm the gym is an Echo client (it should carry an echo_gym_settings or
     echo_social_intake row -- the alert says "the portal knows this gym", which
     satisfies the is_echo_client gate inside register_gym).
  3. Run this script once. accounts.register_gym() writes the gym's row to the
     JSON registry (gym_accounts.json on the worker volume) and invalidates the
     in-process cache so the very next build/publish cycle picks it up.

This script NEVER touches billing, Stripe, pixel, CAPI, ad budgets, ad targeting,
published posts, or anything medical/legal.

WHAT THIS SCRIPT DOES
----------------------
- Calls agent.accounts.register_gym('mindbodysoulfitness2be97e') exactly once.
- Idempotent: if the gym is already in the registry, register_gym updates the row
  in place and this script prints a warning and exits 0.
- Approves nothing, publishes nothing, changes no gate or billing.

PREREQUISITES
-------------
  AGENT_DYNAMIC_ACCOUNTS=true must be set in the echo worker environment. Without
  it register_gym returns [] immediately and nothing is written.

AFTER RUNNING
-------------
The gym is registered and will be picked up by the next build and publish cycles
automatically. No code change is needed.

Tokens (AGENT_MINDBODYSOULFITNESS2BE97E_IG_TOKEN, AGENT_MINDBODYSOULFITNESS2BE97E_IG_ID,
AGENT_MINDBODYSOULFITNESS2BE97E_FB_TOKEN, AGENT_MINDBODYSOULFITNESS2BE97E_FB_PAGE_ID)
must still be set BY HAND in Railway env before any content publishes.

USAGE
-----
    # Preview only -- no writes:
    python3 scripts/register_mindbodysoulfitness2be97e.py --dry-run

    # Live run -- writes the registry row:
    python3 scripts/register_mindbodysoulfitness2be97e.py

Run from the repo root on the deployed Railway instance (shared volume + creds):
    railway ssh --service echo -- python3 \\
        scripts/register_mindbodysoulfitness2be97e.py --dry-run
    railway ssh --service echo -- python3 \\
        scripts/register_mindbodysoulfitness2be97e.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "mindbodysoulfitness2be97e"

_FORBIDDEN_FLAGS = {"--billing", "--stripe", "--pixel", "--capi", "--ad-budget",
                    "--targeting", "--publish", "--approve"}


def _sep():
    print("-" * 64)


def _parse_args():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    for flag in args:
        if flag.lower() in _FORBIDDEN_FLAGS:
            print(f"ERROR: flag '{flag}' is not allowed in this script.")
            sys.exit(1)
    return dry_run


def _already_registered():
    from agent.accounts import _load_registry_rows
    rows = _load_registry_rows()
    return any((r.get("base") or "").strip() == _BASE for r in rows)


def _dry_run():
    _sep()
    print(f"DRY RUN -- register gym {_BASE}")
    _sep()
    print(f"Gym base key : {_BASE}")
    print(f"Account keys : {_BASE}_ig, {_BASE}_fb")

    already = _already_registered()
    if already:
        print(f"Status       : ALREADY REGISTERED in the dynamic registry.")
        print("               Live run would update the row in place (idempotent).")
    else:
        print("Status       : NOT YET in the registry -- live run would register.")

    from agent import config as _config
    if not _config.dynamic_accounts_enabled():
        print()
        print("WARNING: AGENT_DYNAMIC_ACCOUNTS is OFF on this host.")
        print("         register_gym() will return [] without writing anything.")
        print("         Arm it first: AGENT_DYNAMIC_ACCOUNTS=true (Railway env).")

    print()
    print("No writes performed in dry-run mode.")
    _sep()


def _live_run():
    from agent.accounts import register_gym
    from agent import config as _config

    _sep()
    print(f"LIVE RUN -- register gym {_BASE}")
    _sep()

    if not _config.dynamic_accounts_enabled():
        print("ERROR: AGENT_DYNAMIC_ACCOUNTS is OFF on this host.")
        print("       register_gym() would return [] without writing anything.")
        print("       Set AGENT_DYNAMIC_ACCOUNTS=true in Railway env and re-run.")
        _sep()
        sys.exit(1)

    already = _already_registered()
    if already:
        print(f"Gym '{_BASE}' is already in the registry. Updating in place (idempotent).")

    keys = register_gym(_BASE)

    _sep()
    if keys:
        print(f"Done. Registry now contains keys: {keys}")
        print()
        print("The next build and publish cycles will pick this gym up automatically.")
        print()
        print("Tokens to set BY HAND in Railway env before content can publish:")
        up = _BASE.upper()
        for suffix in ("IG_TOKEN", "IG_ID", "FB_TOKEN", "FB_PAGE_ID"):
            print(f"  AGENT_{up}_{suffix}")
    else:
        print(f"register_gym('{_BASE}') returned [] -- nothing was written.")
        print()
        print("This usually means one of:")
        print("  a) AGENT_DYNAMIC_ACCOUNTS is not armed (check Railway env).")
        print("  b) The gym failed the Echo-client check (no echo_gym_settings /")
        print("     echo_social_intake / social product / echo_standalone row).")
        print("     If the gym is a real Echo client, check the portal's echo_gym_settings")
        print("     table for this gym_id and escalate if the row is missing.")
        sys.exit(1)
    _sep()


def main():
    dry_run = _parse_args()
    print(f"register_mindbodysoulfitness2be97e (dry_run={dry_run})")
    print()
    if dry_run:
        _dry_run()
    else:
        _live_run()


if __name__ == "__main__":
    main()
