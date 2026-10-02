#!/usr/bin/env python3
"""Refire the interrupted 2026-09-15 Echo daily draw exactly once.

Support ticket 568686c3-38f7-54fe-8678-27d027311bb7 reported that the daily
draw was interrupted after its last-run stamp was written. As a result,
blake_personal and mflhaa5139 had no rows for 2026-09-15, while a normal
run-daily invocation would no-op.

This script performs the approved recovery by invoking the existing
`run-daily --force` path. That path uses deterministic draft identifiers,
per-draft publish claims, and the 24-hour Meta deduplication guard, so
re-running this recovery does not create a second publish for a draft that
was already handled. The existing daily runner also owns all content,
approval, and publish gates.

The script is deliberately date-locked to 2026-09-15. It refuses to run on
another UTC date, where a forced fleet-wide draw would no longer match this
ticket. It never changes billing, Stripe, pixel/CAPI, ad budgets, ad
targeting, published-post records, or medical/legal content.

Usage:
    python3 scripts/refire_interrupted_draw_20260915.py --dry-run
    python3 scripts/refire_interrupted_draw_20260915.py
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

# Direct script execution sets sys.path[0] to scripts/, not the repository root.
# Add the root explicitly so the deployed /app/agent package is importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_TARGET_DATE = "2026-09-15"
_MISSING_GYMS = ("blake_personal", "mflhaa5139")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the fixed recovery action without running the daily draw",
    )
    args = parser.parse_args(argv)

    today = datetime.now(timezone.utc).date().isoformat()
    gyms = ", ".join(_MISSING_GYMS)
    if args.dry_run:
        print(f"DRY RUN: would force the {_TARGET_DATE} daily draw recovery.")
        print(f"Expected missing gyms: {gyms}")
        print("Action: python -m agent run-daily --force")
        print("No rows, publish state, or operational state were changed.")
        return 0

    if today != _TARGET_DATE:
        parser.error(
            f"refusing recovery on {today}; this one-off is locked to "
            f"{_TARGET_DATE} for {gyms}"
        )

    # Use the real CLI path so its force semantics and completion stamps remain
    # identical to the operator command named in the incident alert.
    from agent.__main__ import main as agent_main

    agent_main(["run-daily", "--force"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
