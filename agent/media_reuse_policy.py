"""Explicit client reuse policies, enforced again at the outbound boundary.

Ticket 74090ba1: Zanshin requested nine calendar months across all platforms.
Other clients retain their existing policy. Archive here means ineligible for
reuse during the window; original files and publishing history are retained.
"""
from calendar import monthrange
from datetime import datetime, timezone


def reuse_months(gym_id):
    key = str(gym_id or "")
    for suffix in ("_ig", "_fb", "_gbp"):
        if key.endswith(suffix):
            key = key[:-len(suffix)]
            break
    return 9 if key == "zanshinfitness630e22" else 0


def months_before(value, months):
    index = value.year * 12 + value.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return value.replace(year=year, month=month,
                         day=min(value.day, monthrange(year, month)[1]))


def _keys(row, assets, library_path):
    from . import media_guard
    keys = set()
    asset_id = str(row.get("source_media_asset_id") or "")
    if asset_id:
        keys.add("asset:" + asset_id)
    for field in ("source_media_url", "image_url"):
        key = media_guard.media_key(row.get(field))
        if key:
            keys.add("file:" + key)
            for raw in media_guard.reframe_map(library_path, {key}).values():
                keys.add("file:" + raw)
    # Bind duplicate Drive uploads and pre-asset-id rows to their content hash.
    for asset in assets:
        names = {media_guard.media_key(asset.get(f)) for f in ("title", "rendition_url")}
        if (asset_id and asset_id == str(asset.get("id"))) or any(
                "file:" + name in keys for name in names if name):
            if asset.get("content_hash"):
                keys.add("hash:" + str(asset["content_hash"]))
    return keys


def publish_hold_reason(row, gym_id, store, *, now=None, library_path=None,
                        media_store=None):
    """Return a hold reason, or None. Never send with unavailable history.

    No same-day, platform, story, or small-library exception. Read actual send
    history (including archived variants), not the mutable staging-use stamp.
    """
    months = reuse_months(gym_id)
    if not months:
        return None
    try:
        now = now or datetime.now(timezone.utc)
        if isinstance(now, str):
            now = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("reuse check requires an aware clock")
        cutoff = months_before(now.astimezone(timezone.utc), months)
        from . import media_source_store
        media_store = media_store or media_source_store.default_store()
        assets = media_store.list_assets(gym_id)
        if not isinstance(assets, list):
            raise ValueError("media inventory unavailable")
        assets = [a for a in assets if a.get("gym_id") == gym_id]
        keys = _keys(row, assets, library_path)
        if not keys:
            return "media_reuse_identity_missing"
        history = store.list_media_publish_history(gym_id, cutoff.isoformat())
        for other in history:
            if str(other.get("id")) == str(row.get("id")):
                continue
            if other.get("gym_id") != gym_id:
                raise ValueError("history tenant mismatch")
            if keys & _keys(other, assets, library_path):
                return "media_reuse_nine_month_hold"
    except Exception:
        return "media_reuse_history_unavailable"
    return None
