#!/usr/bin/env python3
"""One-off ops script: add crossfitchateau813e78 to Echo's dynamic account registry.

WHY THIS EXISTS
----------------
Support ticket 8821a483-fba3-520e-9a19-062591dd02b2, 2026-09-15. Echo raised:
  "ECHO ALERT: crossfitchateau813e78: not set up to post (not_registered,
   no_sources). the portal knows this gym but Echo's account registry does not,
   so it is in neither the build nor the publish lane."

The portal already recognizes this gym, but Echo's dynamic registry is missing
its base key. Running this script calls accounts.register_gym() under the exact
key from the alert. That writes the registry row and invalidates the in-process
account cache so normal build and publish discovery can see the gym.

This script NEVER touches billing, Stripe, pixel, CAPI, ad budgets, ad targeting,
published posts, or anything medical/legal.

WHAT THIS SCRIPT DOES
----------------------
- Calls agent.accounts.register_gym("crossfitchateau813e78") exactly once.
- Is idempotent: an existing row is updated in place by register_gym().
- Supports --dry-run, which only reads and reports registry/config state.
- Approves and publishes nothing and changes no safety gate.

PREREQUISITES
-------------
AGENT_DYNAMIC_ACCOUNTS must already be enabled on the Echo worker. The script
does not change configuration. register_gym() also enforces Echo's existing
client-eligibility check and refuses an unknown/non-Echo gym.

USAGE
-----
    # Preview only, with no writes:
    python3 scripts/register_crossfitchateau813e78.py --dry-run

    # Live run, writing or refreshing the dynamic registry row:
    python3 scripts/register_crossfitchateau813e78.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "crossfitchateau813e78"
_FORBIDDEN_FLAGS = {
    "--billing",
    "--stripe",
    "--pixel",
    "--capi",
    "--ad-budget",
    "--targeting",
    "--publish",
    "--approve",
}


def _sep():
    print("-" * 64)


def _parse_args():
    args = sys.argv[1:]
    allowed = {"--dry-run"}
    for flag in args:
        if flag.lower() in _FORBIDDEN_FLAGS:
            print(f"ERROR: flag '{flag}' is not allowed in this script.")
            sys.exit(1)
        if flag not in allowed:
            print(f"ERROR: unknown argument '{flag}'. Only --dry-run is supported.")
            sys.exit(1)
    return "--dry-run" in args


def _already_registered():
    from agent.accounts import _load_registry_rows

    return any(
        (row.get("base") or "").strip() == _BASE
        for row in _load_registry_rows()
    )


def _dry_run():
    from agent import config

    _sep()
    print(f"DRY RUN -- register gym {_BASE}")
    _sep()
    print(f"Gym base key : {_BASE}")
    print(f"Account keys : {_BASE}_ig, {_BASE}_fb")
    if _already_registered():
        print("Status       : ALREADY REGISTERED; a live run would refresh the row.")
    else:
        print("Status       : NOT REGISTERED; a live run would add the row.")

    if not config.dynamic_accounts_enabled():
        print()
        print("BLOCKED: dynamic accounts are disabled on this host.")
        print("A live run would exit without calling register_gym().")

    print()
    print("No writes performed in dry-run mode.")
    _sep()


def _live_run():
    from agent import config
    from agent.accounts import register_gym

    _sep()
    print(f"LIVE RUN -- register gym {_BASE}")
    _sep()

    if not config.dynamic_accounts_enabled():
        print("ERROR: dynamic accounts are disabled on this host.")
        print("Nothing was written.")
        _sep()
        sys.exit(1)

    if _already_registered():
        print(f"Gym '{_BASE}' is already registered; refreshing it in place.")

    keys = register_gym(_BASE)

    _sep()
    if not keys:
        print(f"register_gym('{_BASE}') returned no account keys; nothing was written.")
        print("Confirm the portal identifies this exact base key as an Echo client.")
        sys.exit(1)

    print(f"Done. Registry now contains account keys: {keys}")
    print("Normal account discovery can pick up this gym on its next cycle.")
    _sep()


def main():
    dry_run = _parse_args()
    print(f"register_crossfitchateau813e78 (dry_run={dry_run})")
    print()
    if dry_run:
        _dry_run()
    else:
        _live_run()


if __name__ == "__main__":
    main()
