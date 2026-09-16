#!/usr/bin/env python3
"""One-off ops script: prepare Pierce Fitness's standard Echo connect link.

WHY THIS EXISTS
---------------
Support ticket 29fe5482-e744-5579-86ba-8a0d899e88b9, 2026-09-15. Echo reported
that piercefitness is known to the portal but is not registered in Echo's
dynamic account registry. The normal automatic connect-link notification was
unable to send because the portal did not contain exactly one client_owner
email.

This script mints the same signed connect URL used by the standard portal flow
for the exact key piercefitness. An operator must deliver the printed URL to
the verified gym owner through the existing client conversation. Completing
that flow lets the normal onboarding path establish the account registration;
this script does not insert a registry row or bypass any onboarding check.

The script NEVER touches billing, Stripe, pixel, CAPI, ad budgets, ad targeting,
published posts, approvals, or anything medical/legal.

USAGE
-----
    # Check prerequisites and intended action without minting a URL:
    python3 scripts/resend_connect_link_piercefitness.py --dry-run

    # Print the standard connect URL for verified manual delivery:
    python3 scripts/resend_connect_link_piercefitness.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "piercefitness"
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


def _prerequisites_ready():
    from agent import intake_tokens

    return intake_tokens.secret_present()


def _dry_run():
    print("DRY RUN -- prepare Echo connect link")
    print(f"Gym key     : {_BASE}")
    print("Delivery    : verified gym owner through the existing client conversation")
    if _prerequisites_ready():
        print("Signing     : READY")
        print("Result      : a live run would print the standard signed connect URL")
    else:
        print("Signing     : BLOCKED")
        print("Result      : the intake signing secret is unavailable on this host")
    print("No writes or messages were performed.")


def _live_run():
    from agent.intake_web import link_for

    if not _prerequisites_ready():
        print("ERROR: the intake signing secret is unavailable on this host.")
        print("Run this script on the Echo listener or intake-web service.")
        sys.exit(1)

    link = link_for(_BASE, kind="connect")
    if not link:
        print("ERROR: Echo could not mint the connect URL. Nothing was sent.")
        sys.exit(1)

    print(f"Gym key     : {_BASE}")
    print(f"Connect URL : {link}")
    print()
    print("Deliver this URL only to the verified Pierce Fitness owner through the")
    print("existing client conversation. Re-running prints an equivalent signed URL.")
    print("No message was sent automatically and no registry or publish gate was changed.")


def main():
    if _parse_args():
        _dry_run()
    else:
        _live_run()


if __name__ == "__main__":
    main()
