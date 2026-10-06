"""Explicit client reuse policies, enforced again at the outbound boundary.

Ticket 74090ba1: Zanshin requested nine calendar months across all platforms.
Other clients retain their existing policy. Archive here means ineligible for
reuse during the window; original files and publishing history are retained.
"""
from calendar import monthrange
from datetime import datetime, timezone
import re


_LEGACY_REFRAME = re.compile(r"^([0-9a-f]{12})__feed\.jpg$")


def _evidence_sha256(asset):
    """Validated raw SHA-256 from evidence bound to this approved Drive row."""
    from . import gym_media_selector

    evidence = asset.get("moderation_json")
    digest = evidence.get("sha256") if isinstance(evidence, dict) else None
    if (asset.get("moderation_status") != "clean"
            or asset.get("review_status") != "approved"
            or asset.get("review_content_hash") != asset.get("content_hash")
            or not gym_media_selector._clean_moderation_evidence(asset)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or evidence.get("verdict") != "clean"
            or evidence.get("content_hash") != asset.get("content_hash")
            or evidence.get("asset_id") != asset.get("id")
            or evidence.get("gym_id") != asset.get("gym_id")):
        return None
    return digest


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
    unresolved_reframe = False
    verified_reframe_identity = False
    raw_source = media_guard.media_key(row.get("source_media_url"))
    for field in ("source_media_url", "image_url"):
        key = media_guard.media_key(row.get(field))
        if key:
            keys.add("file:" + key)
            legacy = _LEGACY_REFRAME.fullmatch(key)
            if legacy:
                keys.add("sha12:" + legacy.group(1))
                verified_reframe_identity = True
            resolved = media_guard.reframe_map(library_path, {key})
            for raw in resolved.values():
                keys.add("file:" + raw)
            if key.endswith("__feed.jpg") and not resolved:
                unresolved_reframe = True
    # Bind duplicate Drive uploads and pre-asset-id rows to their content hash.
    verified_asset_identity = False
    for asset in assets:
        names = {media_guard.media_key(asset.get(f)) for f in ("title", "rendition_url")}
        if (asset_id and asset_id == str(asset.get("id"))) or any(
                "file:" + name in keys for name in names if name):
            if asset.get("content_hash"):
                keys.add("hash:" + str(asset["content_hash"]))
            digest = _evidence_sha256(asset)
            if digest:
                keys.add("sha256:" + digest)
                keys.add("sha12:" + digest[:12])
                verified_asset_identity = True
    if (unresolved_reframe and not verified_reframe_identity
            and not verified_asset_identity
            and (not raw_source or raw_source.endswith("__feed.jpg"))):
        raise ValueError("reframed photo has no verifiable original identity")
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
        history = store.list_media_publish_history(gym_id, cutoff.isoformat())
        if not isinstance(history, list):
            raise ValueError("media history unavailable")
        if not library_path and any(
                str(item.get(field) or "").split("?", 1)[0].endswith("__feed.jpg")
                for item in [row, *history] for field in ("image_url", "source_media_url")):
            from .media_swap import library_path_for
            library_path = library_path_for(gym_id)
        keys = _keys(row, assets, library_path)
        if not keys:
            return "media_reuse_identity_missing"
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
