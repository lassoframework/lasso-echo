"""Scheduled bounded evidence pass. Never approves, publishes, or sends alerts.

Uses the existing Drive-connect gym allowlist. Human review remains mandatory,
including clean no-people photos. Rotation prevents a broken first asset from
starving the rest of a backlog; failed scans remain pending for later retries.
"""
from datetime import datetime, timezone

from .. import config, gym_media_moderation as moderation
from ..integrations.drive_client import DriveClient
from ..media_source_store import default_store


def run(*, store=None, drive=None, vision=None, limit=50, now=None):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("moderation limit must be 1..200")
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
        if source.get("kind", "gym_drive") != "gym_drive" or not gym:
            continue
        if not config.gym_drive_connect_active_for(gym):
            continue
        for asset in store.list_assets(gym, source_id=source["id"]):
            if (asset.get("gym_id") == gym and asset.get("kind") == "photo"
                    and asset.get("review_status") == "pending_review"
                    and asset.get("moderation_status") == "pending"
                    and asset.get("content_hash")):
                candidates[(gym, asset["id"])] = asset
    keys = sorted(candidates)
    if keys:
        offset = (now.date().toordinal() * limit) % len(keys)
        keys = (keys[offset:] + keys[:offset])[:limit]
    results = []
    for gym, asset_id in keys:
        try:
            result = moderation.moderate_asset(
                gym, asset_id, store=store, drive=drive, vision=vision,
                now_iso=now.isoformat())
            results.append({"ok": result["ok"], "reason": result.get("reason")})
        except Exception as exc:
            results.append({"ok": False, "reason": type(exc).__name__})
    return {"ok": all(r["ok"] for r in results), "pending": len(candidates),
            "attempted": len(results), "recorded": sum(r["ok"] for r in results),
            "failures": [r["reason"] for r in results if not r["ok"]]}
