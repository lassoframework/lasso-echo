"""Backfill raw SHA-256 identity for ONE clean approved gym Drive asset.

The exact Drive bytes are downloaded and MD5-checked against ``content_hash``.
Dry-run is the default. ``--apply`` requires an interactive operator shell and
performs a tenant/hash/status/evidence compare-and-swap that changes only the
existing ``moderation_json`` object.
"""
from __future__ import annotations

import argparse
import json
import sys

from .. import gym_media_moderation as moderation
from ..integrations.drive_client import DriveClient
from ..media_source_store import default_store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gym_id")
    parser.add_argument("--asset-id", required=True,
                        help="exactly one media_asset Drive file id")
    parser.add_argument("--apply", action="store_true",
                        help="write the derived sha256 (default: dry-run)")
    args = parser.parse_args(argv)
    if args.apply and not sys.stdin.isatty():
        parser.error("--apply requires an interactive operator shell")
    result = moderation.backfill_asset_sha256(
        args.gym_id, args.asset_id, store=default_store(), drive=DriveClient(),
        apply=args.apply)
    print(json.dumps(result, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
