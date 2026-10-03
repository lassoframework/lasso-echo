#!/usr/bin/env python3
"""Replace receipt-owned future igfill holds with approved never-used gym photos.

Dry run selects photos without preparing, hosting, claiming, or writing. Apply
requires the original private hold receipt, the exact dry-run digest, an explicit
OFF-by-default flag and a new private write-ahead receipt. No month is rebuilt.
Each row replaces media and clears only the PR249 hold in the same CAS. Pending
and approved states, caption and slot identity survive unchanged. Sibling cards
share one photo; different logical posts never share reviewed photo bytes.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import gym_media_selector as selector, media_guard, media_swap
from agent.portal_calendar_store import SupabaseCalendarStore
from scripts.hold_future_igfill_photos import REASON, _FIELDS, _digest, _image, _is_fill, _receipt
from scripts.release_future_igfill_photos import _original_receipt

_ROW_FIELDS = (*_FIELDS, "thumbnail_url", "publish_claim_token", "publish_reservation_day",
               "slot_index", "scheduled_at")
_ASSET_GUARDS = ("id", "gym_id", "source_id", "kind", "content_hash", "review_content_hash",
                 "review_status", "reviewed_by", "reviewed_at", "eligible", "excluded_by_coach",
                 "moderation_status", "moderation_json", "people_detected", "used_count",
                 "last_used_at", "drive_modified", "title", "size_bytes", "mime_type")


def _snapshot(row):
    if not isinstance(row, dict) or any(key not in row for key in _ROW_FIELDS):
        raise ValueError("complete calendar row required")
    return {key: row[key] for key in _ROW_FIELDS}


def _identity(row):
    # Same source bytes across feed/story/GBP are one logical post, never all rows
    # on a date: a second independent post on that date receives a different photo.
    return (row["gym_id"], row["post_date"],
            row.get("source_media_asset_id") or media_guard.row_media_key(row) or row["id"])


def _candidate(asset):
    return {"source": "drive", "kind": "photo", "key": str(asset["id"]),
            "asset": copy.deepcopy(asset), "last_used": "", "used_count": 0,
            "name": str(asset.get("title") or asset["id"])}


def _available_photos(gym, media_store, book):
    if not media_store.available():
        raise ValueError("gym media inventory unavailable")
    assets = media_store.list_assets(gym)
    by_id = {str(asset.get("id")): asset for asset in assets
             if asset.get("gym_id") == gym}
    used_ids = {str(row["source_media_asset_id"]) for row in book if row.get("source_media_asset_id")}
    if not used_ids.issubset(by_id):
        raise ValueError("calendar photo identity is absent from gym media inventory")
    used_hashes = {selector._byte_hash(by_id[asset_id]) for asset_id in used_ids}
    if "" in used_hashes:
        raise ValueError("calendar photo content hash unavailable")
    # Selector reads the same complete inventory we just verified. Its usual
    # second network read catches errors as an empty pool, which would incorrectly
    # report missing photos during a transient error in this operator.
    class Inventory:
        def available(self):
            return True
        def list_assets(self, selected_gym):
            if selected_gym != gym:
                raise ValueError("unexpected gym inventory")
            return copy.deepcopy(assets)
    photo_pool = selector.pickable(gym, "photo", store=Inventory(),
                                   exclude_ids=used_ids, strict_claims=True)
    used_keys = {media_guard.row_media_key(row) for row in book}
    result, seen = [], set(used_hashes)
    for asset in photo_pool:
        content_hash = selector._byte_hash(asset)
        # Explicit counters avoid treating missing or malformed usage as unused.
        if (asset.get("gym_id") != gym or asset.get("kind") != "photo"
                or not selector.is_usable(asset) or not content_hash
                or isinstance(asset.get("used_count"), bool)
                or asset.get("used_count") != 0
                or asset.get("last_used_at") not in (None, "")
                or content_hash in seen
                or (asset.get("title") and media_guard.media_key(asset["title"]) in used_keys)
                or (asset.get("rendition_url")
                    and media_guard.media_key(asset["rendition_url"]) in used_keys)):
            continue
        seen.add(content_hash)
        result.append(asset)
    return result


def preflight(store, media_store, hold_receipt_path, *, today):
    source, held = _original_receipt(hold_receipt_path)
    current, conflicts, expired = [], [], []
    for original in held:
        row = store.get_row(original["gym_id"], str(original["id"]))
        if row is None or _image(row) != original:
            conflicts.append(str(original["id"]))
            continue
        snapshot = _snapshot(row)
        if (not _is_fill(row) or row["variant_status"] != "active"
                or row["status"] not in ("pending", "approved")
                or row["publish_claim_token"] is not None
                or row["publish_reservation_day"] is not None):
            conflicts.append(str(row["id"]))
            continue
        if date.fromisoformat(str(row["post_date"])[:10]) < date.fromisoformat(today):
            expired.append(snapshot)
            continue
        current.append(snapshot)
    groups = {}
    for row in current:
        groups.setdefault(_identity(row), []).append(row)
    planned, blocked, books = [], [
        {"gym_id": row["gym_id"], "row_ids": [str(row["id"])], "reason": "blocked_expired"}
        for row in expired], {}
    for gym in sorted({row["gym_id"] for row in current}):
        book = store.list_photo_restage_book(gym)
        books[gym] = book
        photos = _available_photos(gym, media_store, book)
        for identity, rows in sorted(groups.items()):
            if identity[0] != gym:
                continue
            ids = {str(row["id"]) for row in rows}
            unowned = [str(row["id"]) for row in book
                       if row.get("variant_status") == "active"
                       and row.get("status") in ("pending", "approved", "publishing", "published", "coach_review")
                       and _identity(row) == identity and str(row["id"]) not in ids]
            if unowned:
                blocked.append({"gym_id": gym, "row_ids": sorted(ids),
                                "reason": "same_media_sibling_outside_hold_receipt"})
                continue
            if not photos:
                blocked.append({"gym_id": gym, "row_ids": sorted(ids),
                                "reason": "no_unused_approved_gym_photo"})
                continue
            asset = photos.pop(0)
            planned.append({"gym_id": gym, "rows": sorted(rows, key=lambda row: str(row["id"])),
                            "asset": asset})
    image = {"today": today, "original_target_digest": source["target_digest"],
             "receipt_changed_ids": source["changed_ids"], "before_image": current,
             "blocked_expired": expired,
             "groups": planned, "blocked": blocked,
             "book_digests": {gym: _digest(book) for gym, book in books.items()}}
    return {"operation": "restage_future_igfill_photos", **image,
            "original_hold_receipt": str(Path(hold_receipt_path).resolve()),
            "target_digest": _digest(image), "target_count": len(current) + len(expired),
            "replacement_count": sum(len(group["rows"]) for group in planned),
            "held_count": sum(len(group["row_ids"]) for group in blocked),
            "conflict_ids": conflicts}, books


class _ReviewedDrive:
    """Check downloaded ORIGINAL bytes before media_swap renders or hosts them."""
    def __init__(self, drive, asset, used_keys=()):
        self.drive, self.asset = drive, asset
        self.used_keys = set(used_keys)
    def available(self):
        return self.drive.available()
    def download(self, asset_id, path):
        if asset_id != self.asset["id"]:
            raise ValueError("unexpected Drive asset")
        self.drive.download(asset_id, path)
        expected = selector._byte_hash(self.asset)
        digest = hashlib.md5() if len(expected) == 32 else hashlib.sha256()
        sha256 = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                sha256.update(chunk)
        if not expected or digest.hexdigest() != expected:
            raise ValueError("downloaded photo differs from approved bytes")
        if f"{sha256.hexdigest()[:12]}__feed.jpg" in self.used_keys:
            raise ValueError("photo already carried by calendar reframe")


def _host_verified(path, tenant, *, host_fn=None, http=None):
    """Require the public object to contain the exact locally prepared bytes.

    Content-addressed URLs alone cannot prove a cached object contains those bytes.
    No old asset rendition URL participates in this operator's preparation path.
    """
    from agent import media_host
    if http is None:
        import requests
        http = requests
    size = os.path.getsize(path)
    if size <= 0 or size > 25 * 1024 * 1024:
        raise ValueError("prepared image outside verification size bound")
    expected = hashlib.sha256()
    with open(path, "rb") as image:
        for chunk in iter(lambda: image.read(1024 * 1024), b""):
            expected.update(chunk)
    url = (host_fn or media_host.host_media)(path, tenant)
    if (not isinstance(url, str) or not url.startswith("https://")
            or url != media_host.public_url_for(media_host.key_for(path, tenant))):
        raise ValueError("hosted image URL does not match prepared bytes and gym")
    response = http.get(url, stream=True, timeout=(10, 20), allow_redirects=False)
    try:
        if response.status_code != 200:
            raise ValueError("prepared image delivery unavailable")
        delivered, received = hashlib.sha256(), 0
        deadline = time.monotonic() + 25
        for chunk in response.iter_content(chunk_size=64 * 1024):
            received += len(chunk)
            if received > size or time.monotonic() > deadline:
                raise ValueError("prepared image delivery verification exceeded bounds")
            delivered.update(chunk)
        if received != size or delivered.digest() != expected.digest():
            raise ValueError("delivered image differs from prepared bytes")
    finally:
        response.close()
    return url


def _prepare(gym, root, **kwargs):
    from agent import config, feed_image, gym_media_index, story_image
    from agent.integrations.drive_client import DriveClient
    from agent.jobs.media_repeat_sweep import _gym_name
    candidate = kwargs["candidates_fn"](gym, root)[0]
    book = kwargs["store"].list_photo_restage_book(gym)
    drive = _ReviewedDrive(DriveClient(), candidate["asset"],
                           [media_guard.row_media_key(row) for row in book])
    rows = (root, *kwargs.get("siblings", ()))
    if any(row.get("format") == "story" for row in rows) and not config.story_format_enabled():
        raise ValueError("captioned story renderer must be enabled for photo restage")
    work = tempfile.mkdtemp(prefix="held-photo-verified_")
    proofs = {}
    try:
        def host(path):
            url = _host_verified(path, gym)
            digest = hashlib.sha256()
            with open(path, "rb") as prepared:
                for chunk in iter(lambda: prepared.read(1024 * 1024), b""):
                    digest.update(chunk)
            proofs[url] = {"image_url": url, "sha256": digest.hexdigest(),
                           "size_bytes": os.path.getsize(path)}
            return url

        def materialize(selected):
            if not drive.available() or selected["key"] != candidate["key"]:
                return None
            raw = os.path.join(work, os.path.basename(candidate["asset"].get("title") or "photo.jpg"))
            if not media_swap._download_bounded(drive, selected["key"], raw,
                                                media_swap.SWAP_DOWNLOAD_TIMEOUT_SEC):
                return None
            # Render a HEIC freshly from verified original bytes. Never ask
            # ensure_rendition for a potentially stale persisted URL or object.
            photo = raw
            if gym_media_index.needs_rendition(candidate["asset"]):
                photo = os.path.join(work, "verified-original.jpg")
                gym_media_index.heic_to_jpeg(raw, photo)
            from PIL import Image
            with Image.open(photo) as image:
                image.verify()
            return {"path": photo, "hosted": host(photo)}

        def feed(path):
            # A private temporary render directory prevents old derivative cache
            # hits. If this original is already in spec, host its verified bytes.
            rendered = feed_image.get_or_make_feed_image(path, work)
            from PIL import Image
            with Image.open(rendered or path) as image:
                if feed_image.needs_autofit(*image.size):
                    raise ValueError("feed render missing or outside supported geometry")
                image.verify()
            return host(rendered or path)

        def story(base, row, path, _lib):
            rendered = story_image.get_or_make_story_image(
                path, (row.get("caption") or "").strip(), _gym_name(base), work)
            if not rendered:
                raise ValueError("captioned story render failed")
            from PIL import Image
            with Image.open(rendered) as image:
                if image.size != (1080, 1920):
                    raise ValueError("story render outside required 9:16 geometry")
                image.verify()
            return host(rendered)

        pick = media_swap.pick_replacement(gym, root, materialize_fn=materialize,
                                           feed_fn=feed, reburn_fn=story, **kwargs)
        if pick.get("ok"):
            pick["_verified_media"] = list(proofs.values())
        return pick
    finally:
        shutil.rmtree(work)


def _reserve(gym, row, pick, asset, media_store):
    # Existing shared canonical-byte claim participates in planners and swaps.
    current = media_store.get_asset(str(asset["id"]))
    if current is None or any(current.get(key) != asset.get(key) for key in _ASSET_GUARDS):
        return False
    claim = selector.claim_drive_content(gym, asset, media_store)
    if not claim:
        return False
    pick.update(_drive_claim_id=claim, _drive_claim_account=f"{gym}_gbp")
    selector.stamp_use(asset, gym, str(row["post_date"])[:10], store=media_store)
    pick["_drive_stamped"] = True
    return True


def _payload(pick):
    return {"image_url": pick["image_url"], "source_media_url": pick.get("source_media_url"),
            "source_media_asset_id": pick["source_media_asset_id"],
            "thumbnail_url": pick.get("thumbnail_url") or None, "media_not_ready_reason": None}


def _writer_readback(row, *, prepared_write):
    """The writer's verified result is the readback contract, not our input.

    Prepared visual writes assign canonical lineage after rendering.  It is not
    safe to recreate those values from the held row and operator payload.
    """
    result = _snapshot(row)
    if prepared_write:
        for key in ("visual_group_key", "byte_hash"):
            if not row.get(key):
                raise ValueError(f"prepared writer result missing {key}")
            result[key] = row[key]
    return result


def run(*, store=None, media_store=None, hold_receipt_path=None, today=None,
        apply=False, expected_digest=None, receipt_path=None, prepare_fn=None,
        reserve_fn=None, settle_fn=None):
    today = today or datetime.now(timezone.utc).date().isoformat()
    store = store or SupabaseCalendarStore()
    if media_store is None:
        from agent.media_source_store import default_store
        media_store = default_store()
    plan, books = preflight(store, media_store, hold_receipt_path, today=today)
    if not apply:
        return {"ok": not plan["conflict_ids"], "dry_run": True, "preflight": plan}
    if os.environ.get("ECHO_FUTURE_IGFILL_RESTAGE_ENABLED", "").lower() != "true":
        return {"ok": False, "reason": "restage apply flag OFF"}
    if (plan["conflict_ids"] or not plan["groups"] or not receipt_path
            or expected_digest != plan["target_digest"]):
        return {"ok": False, "reason": "nonempty conflict-free exact digest and new receipt required"}
    if Path(receipt_path).resolve() == Path(hold_receipt_path).resolve():
        return {"ok": False, "reason": "restage receipt must differ from original"}
    progress = {**plan, "state": "before_write", "changed_ids": [], "readback": [],
                "inflight_ids": [], "prepared": [], "reservations": [],
                "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        _receipt(receipt_path, progress, create=True)
    except Exception as exc:
        return {"ok": False, "reason": f"before-image receipt failed:{type(exc).__name__}"}
    prepare = prepare_fn or _prepare
    reserve = reserve_fn or _reserve
    settle = settle_fn or media_swap.after_swap
    from agent import visual_writer_prepare
    prepared_write = visual_writer_prepare.enabled()
    try:
        for group in plan["groups"]:
            gym, rows, asset = group["gym_id"], group["rows"], group["asset"]
            root = rows[0]
            pick = prepare(gym, root, store=store, media_store=media_store,
                           siblings=rows[1:], candidates_fn=lambda _gym, _row: [_candidate(asset)])
            variants = [(root, pick)] + [(row, (pick.get("siblings") or {}).get(str(row["id"])))
                                        for row in rows[1:]]
            for row, variant in variants:
                if (not variant or not variant.get("ok") or variant.get("kind") != "photo"
                        or variant.get("source") != "drive"
                        or variant.get("source_media_asset_id") != str(asset["id"])
                        or not str(variant.get("image_url") or "").startswith("https://")
                        or _is_fill(variant)):
                    raise ValueError("replacement or sibling preparation failed")
            progress["inflight_ids"] = [str(row["id"]) for row in rows]
            progress["prepared"] = [{"row_id": str(row["id"]), **_payload(variant)}
                                    for row, variant in variants]
            progress["verified_media"] = pick.get("_verified_media", [])
            progress["state"] = "reservation_intent"
            _receipt(receipt_path, progress)
            if not reserve(gym, root, pick, asset, media_store):
                raise ValueError("photo claim conflict; all remaining holds retained")
            progress["reservations"].append({"gym_id": gym, "asset_id": asset["id"],
                                              "content_hash": selector._byte_hash(asset),
                                              "claim_id": pick.get("_drive_claim_id")})
            progress["state"] = "write_intent"
            _receipt(receipt_path, progress)
            for row, variant in variants:
                payload = _payload(variant)
                updated = store.replace_future_infographic_media(
                    gym, row, reason=REASON, **{key: value for key, value in payload.items()
                                               if key != "media_not_ready_reason"},
                    render_evidence=variant.get("render_evidence"))
                if updated is None:
                    raise ValueError("exact row CAS conflict; reconcile reserved photo before retry")
                observed = store.get_row(gym, str(row["id"]))
                expected = _writer_readback(updated, prepared_write=prepared_write)
                if _writer_readback(observed, prepared_write=prepared_write) != expected:
                    raise ValueError("readback mismatch; reconcile before retry")
                progress["changed_ids"].append(str(row["id"]))
                progress["readback"].append(expected)
                progress["inflight_ids"].remove(str(row["id"]))
                progress["state"] = "partial_restage"
                _receipt(receipt_path, progress)
            settle(gym, root, pick, media_store=media_store, book_rows=books[gym],
                   swapped_ids=[str(row["id"]) for row in rows])
        progress["state"] = "readback_verified"
        _receipt(receipt_path, progress)
    except Exception as exc:
        progress["state"] = "reconcile_required"
        progress["error"] = f"{type(exc).__name__}: {exc}"
        # Preserve claims on every partial or uncertain outcome. Releasing them
        # here could re-offer bytes already visible on a successfully changed row.
        try:
            _receipt(receipt_path, progress)
        except Exception:
            pass
        return {"ok": False, "reason": "restage interrupted; private receipt requires reconciliation",
                "receipt": str(receipt_path), "changed_ids": progress["changed_ids"],
                "inflight_ids": progress["inflight_ids"]}
    return {"ok": True, "receipt": str(receipt_path), "target_digest": plan["target_digest"],
            "changed_ids": progress["changed_ids"], "held": plan["blocked"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold-receipt", required=True)
    parser.add_argument("--today", help="UTC date YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt", help="new private JSON path required for apply")
    args = parser.parse_args(argv)
    try:
        result = run(hold_receipt_path=args.hold_receipt, today=args.today, apply=args.apply,
                     expected_digest=args.expected_digest, receipt_path=args.receipt)
    except Exception as exc:
        result = {"ok": False, "reason": f"preflight failed:{type(exc).__name__}"}
    displayed = dict(result)
    if "preflight" in displayed:
        plan = displayed["preflight"]
        displayed["preflight"] = {key: value for key, value in plan.items()
                                  if key not in ("before_image", "groups", "blocked_expired")}
        displayed["preflight"]["blocked_expired_count"] = len(plan["blocked_expired"])
        displayed["preflight"]["photo_groups"] = [
            {"gym_id": group["gym_id"], "row_count": len(group["rows"]),
             "asset_id": group["asset"]["id"],
             "content_hash": selector._byte_hash(group["asset"])} for group in plan["groups"]]
    print(json.dumps(displayed, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
