"""Trusted local operator review of Drive assets. No public HTTP write route.

The operator must run this from an interactive shell with service credentials.
Automated moderation and people detection are separate evidence-producing jobs;
this command never invents their verdicts.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone

from . import gym_media_selector as selector
from .media_source_store import default_store


def review_asset(gym_id, asset_id, action, *, store, operator, note=None):
    """Record a local operator decision, scoped to the existing gym and asset."""
    if action not in {"approve", "reject"}:
        raise ValueError("unknown action")
    if not str(operator or "").strip():
        raise ValueError("operator identity is required")
    asset = store.get_asset(asset_id)
    if not asset or asset.get("gym_id") != gym_id:
        raise ValueError("asset not found for gym")
    content_hash = str(asset.get("content_hash") or "").strip()
    if not content_hash:
        raise ValueError("review requires a known Drive content hash")

    now = datetime.now(timezone.utc).isoformat()
    fields = {"reviewed_by": operator, "reviewed_at": now,
              "review_note": note, "review_content_hash": content_hash}
    if action == "approve":
        candidate = dict(asset, **fields, review_status="approved",
                         consent_status="not_required")
        if not selector.is_usable(candidate):
            raise ValueError("approval requires technical eligibility and clean moderation evidence")
        fields.update(review_status="approved", consent_status="not_required")
    else:
        fields["review_status"] = "rejected"

    # A conditional gym filter prevents the global Drive ID from being updated
    # if another tenant owns it. This CLI is the sole review writer.
    # The store commits the decision and append-only event in one transaction,
    # conditional on the hash and review state seen above. A concurrent sync or
    # second reviewer must make this call fail rather than silently win a race.
    store.update_review_asset(
        gym_id, asset_id, fields, expected_content_hash=content_hash,
        expected_review_status=asset.get("review_status"),
        expected_reviewed_at=asset.get("reviewed_at"))
    return fields


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gym_id")
    parser.add_argument("asset_id")
    parser.add_argument("action", choices=("approve", "reject"))
    parser.add_argument("--note")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        parser.error("review requires an interactive operator shell")
    operator = getpass.getuser()
    result = review_asset(args.gym_id, args.asset_id, args.action,
                          store=default_store(), operator=operator,
                          note=args.note)
    print(json.dumps({"asset_id": args.asset_id, "gym_id": args.gym_id,
                      "review_status": result["review_status"],
                      "reviewed_by": operator}))


if __name__ == "__main__":
    main()
