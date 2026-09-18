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


def review_asset(gym_id, asset_id, action, *, store, operator,
                 note=None, release_ref=None, member_ref=None, expires_at=None):
    """Record a local operator decision, scoped to the existing gym and asset."""
    if action not in {"approve", "reject", "request_release"}:
        raise ValueError("unknown action")
    if not str(operator or "").strip():
        raise ValueError("operator identity is required")
    asset = store.get_asset(asset_id)
    if not asset or asset.get("gym_id") != gym_id:
        raise ValueError("asset not found for gym")

    now = datetime.now(timezone.utc).isoformat()
    fields = {"reviewed_by": operator, "reviewed_at": now,
              "review_note": note}
    if action == "approve":
        candidate = dict(asset, **fields, review_status="approved")
        if release_ref or member_ref or expires_at:
            if asset.get("people_detected") is not True:
                raise ValueError("release fields only apply to people assets")
            candidate.update(consent_status="granted", release_ref=release_ref,
                             consent_member_ref=member_ref,
                             consent_expires_at=expires_at)
        if not selector.is_usable(candidate):
            raise ValueError("approval requires technical eligibility, clean moderation evidence, known people status and valid consent")
        fields.update(review_status="approved")
        if release_ref or member_ref or expires_at:
            fields.update(consent_status="granted", release_ref=release_ref,
                          consent_member_ref=member_ref,
                          consent_expires_at=expires_at)
    elif action == "reject":
        fields["review_status"] = "rejected"
    else:
        fields.update(review_status="pending_review", consent_status="pending")

    # A conditional gym filter prevents the global Drive ID from being updated
    # if another tenant owns it. This CLI is the sole review writer.
    store.update_review_asset(gym_id, asset_id, fields)
    return fields


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gym_id")
    parser.add_argument("asset_id")
    parser.add_argument("action", choices=("approve", "reject", "request_release"))
    parser.add_argument("--note")
    parser.add_argument("--release-ref")
    parser.add_argument("--member-ref")
    parser.add_argument("--expires-at", help="ISO 8601 timestamp with timezone")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        parser.error("review requires an interactive operator shell")
    operator = getpass.getuser()
    result = review_asset(args.gym_id, args.asset_id, args.action,
                          store=default_store(), operator=operator,
                          note=args.note, release_ref=args.release_ref,
                          member_ref=args.member_ref, expires_at=args.expires_at)
    print(json.dumps({"asset_id": args.asset_id, "gym_id": args.gym_id,
                      "review_status": result["review_status"],
                      "reviewed_by": operator}))


if __name__ == "__main__":
    main()
