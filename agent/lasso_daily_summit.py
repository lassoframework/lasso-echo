"""Dated extra LASSO Growth Summit feed drafts.

This module is intentionally separate from the rotating weekly Summit builder.
It can only serve the exact approved catalog entry for a requested date, to the
canonical LASSO accounts, while the dated daily-extra flag is armed. It creates
held drafts only and has no publishing or scheduling write path.
"""

import hashlib
import json
import uuid
from pathlib import Path

from . import config, creative_studio, media_host, schedule
from .drafter import Draft, DraftStatus, _make_id
from .infographic_artifacts import ArtifactStore


CATALOG_PATH = Path(__file__).resolve().parents[1] / "brand_voice" / "lasso_summit_daily.json"
CANONICAL_LASSO_KEYS = frozenset(("lasso", "lasso_ig", "lasso_fb"))
SHARED_TENANT = "lasso"


def _catalog_entry(day_key, catalog_path=CATALOG_PATH):
    """Return one exact dated entry, refusing malformed or duplicate catalogs."""
    try:
        payload = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    entries = payload.get("posts", [])
    directions = {
        str(item.get("id") or "").strip(): str(item.get("direction") or "").strip()
        for item in payload.get("visual_concepts", []) if item.get("id")
    }
    matches = [entry for entry in entries if entry.get("date") == str(day_key)]
    if len(matches) != 1:
        return None
    entry = dict(matches[0])
    on_image = entry.get("on_image") or {}
    resolved_direction = str(entry.get("art_direction") or "").strip()
    if not resolved_direction:
        resolved_direction = directions.get(str(entry.get("visual_concept") or "").strip(), "")
    if not (entry.get("caption") and entry.get("date") and on_image.get("headline")
            and on_image.get("facts") and on_image.get("cta") and on_image.get("footer")):
        return None
    # An ID such as "plan_pages" is a routing key, not a usable creative brief.
    # Refuse it rather than silently flattening the campaign into a generic card.
    if not resolved_direction:
        return None
    entry["art_direction"] = resolved_direction
    return entry


def _source_identity(entry):
    """Stable provenance for approved copy, independent of a later hosted URL."""
    approved = {
        "date": entry["date"],
        "caption": entry["caption"],
        "visual_concept": entry.get("visual_concept", ""),
        "art_direction": entry["art_direction"],
        "on_image": entry["on_image"],
    }
    raw = json.dumps(approved, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {"source_id": f"lasso_summit_daily_catalog:{entry['date']}",
            "source_hash": digest}, f"lasso_summit_daily:{entry['date']}:{digest}"


def _canonical_lasso(account):
    return str(getattr(account, "key", account) or "").strip().lower() in CANONICAL_LASSO_KEYS


def build_daily_summit(account, day_key, *, catalog_path=CATALOG_PATH,
                       creative_generate=None, host_fn=None, artifact_store=None,
                       enabled_fn=None, nano_client=None, s3_client=None):
    """Build one exact dated Summit Draft, or ``None`` when its gates do not clear.

    Dependencies are injectable so tests never need a generator, media host, or
    shared artifact service. A catalog hosted URL is usable only when the shared
    artifact store independently returns that same reviewed URL for this source.
    """
    enabled_fn = enabled_fn or config.lasso_summit_daily_enabled
    if not _canonical_lasso(account) or not enabled_fn(day_key):
        return None
    entry = _catalog_entry(day_key, catalog_path)
    if entry is None:
        return None

    source, cache_key = _source_identity(entry)
    artifacts = artifact_store if artifact_store is not None else ArtifactStore()
    def reviewed_cache():
        try:
            return artifacts.cached(SHARED_TENANT, cache_key)
        except Exception:
            return None

    reviewed_cached_url = reviewed_cache()

    # A nonempty catalog URL is mutable input, so it remains usable only when
    # the reviewed cache independently names that exact URL. A blank catalog URL
    # allows the source-keyed reviewed cache to be reused by both IG and FB.
    catalog_url = str(entry.get("hosted_image_url") or "").strip()
    if catalog_url and catalog_url != reviewed_cached_url:
        return None
    if reviewed_cached_url:
        creative_path = reviewed_cached_url
        hosted_url = reviewed_cached_url
        image_engine = ""
    else:
        owner = str(uuid.uuid4())
        try:
            claimed = artifacts.claim(SHARED_TENANT, cache_key, owner)
        except Exception:
            claimed = False
        if not claimed:
            # Another account may have completed the same source-keyed artifact
            # between our first cache read and its successful claim.
            reviewed_cached_url = reviewed_cache()
            if not reviewed_cached_url:
                return None
            creative_path = reviewed_cached_url
            hosted_url = reviewed_cached_url
            image_engine = ""
        else:
            try:
                # Close the cache-read/claim race: a prior owner may have saved
                # the reviewed artifact immediately before releasing its lease.
                reviewed_cached_url = reviewed_cache()
                if reviewed_cached_url:
                    creative_path = reviewed_cached_url
                    hosted_url = reviewed_cached_url
                    image_engine = ""
                else:
                    creative_generate = creative_generate or creative_studio.generate
                    host_fn = host_fn or media_host.host_media
                    image = creative_generate(
                        entry["on_image"]["headline"], entry["on_image"]["facts"],
                        client=nano_client, account_key=getattr(account, "key", "lasso"),
                        aspect="4:5", surface="feed", cta=entry["on_image"]["cta"],
                        footer=entry["on_image"]["footer"], art_direction=entry["art_direction"],
                        draft_id=_make_id(getattr(account, "key", "lasso"), cache_key, day_key),
                    )
                    if not image or not image.get("path"):
                        return None
                    hosted_url = host_fn(image["path"], SHARED_TENANT, client=s3_client)
                    if not hosted_url:
                        return None
                    try:
                        artifacts.save(SHARED_TENANT, hosted_url, image["path"], source,
                                       cache_key=cache_key)
                    except Exception:
                        return None
                    creative_path = image["path"]
                    image_engine = image.get("route", "")
            finally:
                try:
                    artifacts.release(SHARED_TENANT, cache_key, owner)
                except Exception:
                    pass

    draft = Draft(
        draft_id=_make_id(getattr(account, "key", "lasso"), cache_key, day_key),
        account_key=getattr(account, "key", ""), platform=getattr(account, "platform", ""),
        caption=entry["caption"], hashtags=[], creative_path=creative_path,
        creative_public_url=hosted_url, scheduled_for=schedule.scheduled_for(day_key),
        status=DraftStatus.PENDING, source_fragments=[entry["caption"]],
        infographic_copy=dict(entry["on_image"]), day_key=str(day_key),
        draft_type="summit", category="summit", is_story=False, image_engine=image_engine,
    )
    draft.cadence_slot_index = 2
    draft.source_identity = source
    return draft
