"""Trusted local operator moderation scan for gym Drive media. No public HTTP.

Runs agent/gym_media_moderation.moderate_asset over an explicit, bounded list of
asset IDs for ONE gym — never a gym-wide sweep. DEFAULT IS DRY-RUN: without
--apply the download, hash check and vision scan still run, and the would-be
evidence JSON is printed, but NOTHING is written. --apply performs the
conditional evidence write and requires an interactive operator shell
(sys.stdin.isatty()). This command never approves: review_status stays
'pending_review' and agent/gym_media_review.py remains the sole review writer.
Secrets are never printed; one JSON line per asset is the whole output.
"""
from __future__ import annotations

import argparse
import json
import sys

from .. import gym_media_moderation as mod
from ..integrations.drive_client import DriveClient
from ..media_source_store import default_store

_MAX_ASSETS = 50  # bounded: an explicit list, never a sweep


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gym_id")
    parser.add_argument("--asset-id", dest="asset_ids", action="append",
                        required=True,
                        help="one media_asset id; repeat for each asset "
                             f"(max {_MAX_ASSETS})")
    parser.add_argument("--apply", action="store_true",
                        help="perform the conditional evidence write (default is "
                             "dry-run: scan and print would-be evidence, no write)")
    args = parser.parse_args(argv)

    asset_ids = [str(a).strip() for a in args.asset_ids if str(a).strip()]
    if not asset_ids or len(asset_ids) > _MAX_ASSETS:
        parser.error(f"--asset-id requires 1..{_MAX_ASSETS} explicit ids")
    if args.apply and not sys.stdin.isatty():
        parser.error("--apply requires an interactive operator shell")

    store = default_store()
    drive = DriveClient()
    vision = mod.default_vision()

    for asset_id in asset_ids:
        if not args.apply:
            # Dry-run: everything EXCEPT the write. Reuse moderate_asset against a
            # store shim whose conditional update is a capture-only no-op, so the
            # would-be evidence is computed by the exact production path.
            captured = {}

            class _DryStore:
                def get_asset(self, aid, _s=store):
                    return _s.get_asset(aid)

                def update_moderation_asset(self, gym_id, aid, fields, *,
                                            expected_content_hash):
                    captured.update(fields)
                    return True

            result = mod.moderate_asset(args.gym_id, asset_id, store=_DryStore(),
                                        drive=drive, vision=vision)
            if result.get("ok"):
                result["dry_run"] = True
                result["would_write"] = captured
                result.pop("evidence", None)
        else:
            result = mod.moderate_asset(args.gym_id, asset_id, store=store,
                                        drive=drive, vision=vision)
            result.pop("evidence", None)  # full evidence lives on the row
        print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
