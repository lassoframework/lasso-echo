"""Scheduled bounded evidence pass. Never publishes or sends alerts.

Uses the existing Drive-connect gym allowlist. Clean hash-bound photo or video
evidence approves the exact asset version. Rotation prevents a broken first asset
from starving the rest of a backlog; failed scans remain pending for later retries.
"""
import argparse
import json
from datetime import datetime, timezone

from .. import config, gym_media_moderation as moderation
from ..integrations.drive_client import DriveClient
from ..media_source_store import default_store


def run(*, store=None, drive=None, vision=None, limit=50, now=None,
        gym_id=None, after=None):
    """Moderate one bounded pending-media page.

    The scheduled caller leaves ``gym_id`` and ``after`` unset, retaining its
    daily rotation across the global backlog.  An operator catch-up supplies a
    connected ``gym_id`` and may pass the returned ``next_cursor`` to continue
    in deterministic asset-id order.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("moderation limit must be 1..200")
    if gym_id is not None and (not isinstance(gym_id, str) or not gym_id.strip()):
        raise ValueError("gym_id must be a non-empty string")
    if after is not None and (not isinstance(after, str) or not after.strip()):
        raise ValueError("after must be a non-empty asset id")
    gym_id = gym_id.strip() if gym_id else None
    after = after.strip() if after else None
    store = store if store is not None else default_store()
    drive = drive if drive is not None else DriveClient()
    if not store.available() or not drive.available():
        return {"ok": False, "reason": "media store or Drive unavailable"}
    vision = vision if vision is not None else moderation.default_vision()
    if vision is None:
        return {"ok": False, "reason": "vision provider unarmed"}
    now = now or datetime.now(timezone.utc)
    candidates = {}
    for source in store.list_sources():
        gym = source.get("gym_id")
        if gym_id is not None and gym != gym_id:
            continue
        if source.get("kind", "gym_drive") != "gym_drive" or not gym:
            continue
        if not config.gym_drive_connect_active_for(gym):
            continue
        for asset in store.list_assets(gym, source_id=source["id"]):
            if (asset.get("gym_id") == gym and asset.get("kind") in ("photo", "video")
                    and asset.get("review_status") == "pending_review"
                    and asset.get("moderation_status") == "pending"
                    and asset.get("content_hash")):
                candidates[(gym, asset["id"])] = asset
    keys = sorted(candidates)
    if gym_id is not None and after is not None:
        keys = [key for key in keys if key[1] > after]
    elif gym_id is None and keys:
        offset = (now.date().toordinal() * limit) % len(keys)
        keys = keys[offset:] + keys[:offset]
    selected = keys[:limit]
    results = []
    for gym, asset_id in selected:
        try:
            result = moderation.moderate_asset(
                gym, asset_id, store=store, drive=drive, vision=vision,
                now_iso=now.isoformat())
            results.append({"ok": result["ok"], "reason": result.get("reason")})
        except Exception as exc:
            results.append({"ok": False, "reason": type(exc).__name__})
    result = {"ok": all(r["ok"] for r in results), "pending": len(candidates),
              "attempted": len(results), "recorded": sum(r["ok"] for r in results),
              "failures": [r["reason"] for r in results if not r["ok"]]}
    if gym_id is not None:
        result["next_cursor"] = (
            selected[-1][1] if selected and len(keys) > len(selected) else None)
    return result


def main(argv=None):
    """Run an explicitly gym-scoped catch-up page; never invoke globally by CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gym", required=True, dest="gym_id",
                        help="connected gym id to moderate")
    parser.add_argument("--limit", type=int, default=50,
                        help="assets to attempt (1..200; default: 50)")
    parser.add_argument("--after", help="continue after this asset id")
    args = parser.parse_args(argv)
    result = run(gym_id=args.gym_id, limit=args.limit, after=args.after)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
