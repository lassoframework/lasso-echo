"""
scene_history_review.py — DRAFT / OFF. Bounded, READ-ONLY per-gym review queue
over (1) historical PUBLISHED calendar rows and (2) older approved Drive
assets, classifying each item as PROVED IDENTITY (exact row/asset binding on
authoritative history) vs USED SCENE BYTES (asset lane only: the exact bytes
appear on history but no entry binds that particular asset) vs UNRESOLVED
with explicit reasons, against the draft occupied-scene / publish-claim
history (Child A's
migrations/DRAFT_visual_scene_history_backfill_20261005.sql — unapplied).

WHAT THIS IS FOR: before any scene-history activation is ever considered, a
human needs to know which historical published rows and approved library
photos have a PROVED exact-byte identity on the draft history tables
(fingerprint bound to the exact row, group and non-blank date — or the exact
asset) and which do not, and WHY.
This job only answers that question. It never clears an asset, never writes a
row, never arms a guard, never touches production or GHL, and never calls the
network. There is deliberately NO --apply mode: the module exposes no write
path at all.

HARD GUARANTEES:
  * READ-ONLY — only the read methods of the injected stores are called
    (list_published_rows / list_assets / get_asset / list_occupied_scenes /
    list_publish_claims). The job holds no write handle.
  * BOUNDED — per-gym caps on published rows (MAX_PUBLISHED_ROWS_PER_GYM),
    approved assets (MAX_APPROVED_ASSETS_PER_GYM), history rows
    (MAX_HISTORY_ROWS_PER_GYM) and the emitted queue (MAX_QUEUE_ITEMS);
    queue order is deterministic.
  * PHOTOS FIRST — photo items always rank ahead of video items.
  * TENANT ISOLATION — every read is keyed by gym_id, and every returned row
    is RE-FILTERED tenant-side: a poisoned reader returning another gym's
    rows has them dropped before classification, never classified. A calendar
    row bound to an asset owned by a DIFFERENT gym is classified unresolved
    with reason 'tenant_mismatch' — the other gym's asset fields are never
    copied into the queue item.

ASSUMED INTERFACE (Child A, coordination by assumption — do not edit their
files): the history reader exposes
    list_occupied_scenes(gym_id, limit) -> [row]
    list_publish_claims(gym_id, limit)  -> [row]
with canonical columns tenant_id, group_key, used_date (claims: claim_date),
fingerprint ('md5:<32hex>'), calendar_row_id, channel — the shape of
public.visual_scene_phash_occupied / the publish-claim tables in the
2026-10-05 draft ledger migrations. Fingerprints are MD5-keyed only (the
ledger rejects anything else), so a row/asset whose only hash is not an MD5
classifies unresolved with 'fingerprint_unverifiable' rather than guessing.

Usage (inspection only):
  python -m agent.jobs.scene_history_review GYM [GYM ...]
"""

from __future__ import annotations

import argparse
import re
import sys

# Bounds — deliberately small; this is a human review queue, not a sweep.
MAX_PUBLISHED_ROWS_PER_GYM = 200
MAX_APPROVED_ASSETS_PER_GYM = 200
MAX_HISTORY_ROWS_PER_GYM = 1000
MAX_QUEUE_ITEMS = 400

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
PHOTO_MIME_PREFIX = "image/"

PROVED = "proved_identity"
USED_BYTES = "used_scene_bytes"
UNRESOLVED = "unresolved"


def _log(msg):
    print(f"[scene-history-review] {msg}")


def _fingerprint(value):
    """Canonical 'md5:<32hex>' fingerprint, or None when the value is not an
    MD5 byte fingerprint (a sha256, a URL, a Drive id — none of those are
    comparable to the md5-keyed draft ledger)."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v.startswith("md5:"):
        v = v[4:]
    if _MD5_RE.match(v):
        return f"md5:{v}"
    return None


def _is_photo(row):
    mime = str(row.get("mime_type") or "").lower()
    if mime:
        return mime.startswith(PHOTO_MIME_PREFIX)
    kind = str(row.get("kind") or row.get("format") or "").lower()
    return kind not in ("video", "reel", "clip")


def _row_date(row):
    return str(row.get("post_date") or row.get("used_date") or "")


class _HistoryIndex:
    """Same-tenant-only fingerprint -> list of authoritative history entries
    (group_key, date, calendar_row_id, asset_id), built from the draft
    occupied-scene + publish-claim readers. Cross-tenant rows are dropped
    here, before any classification can see them. Proof is EXACT-BINDING
    only: a bare fingerprint+date match never proves identity."""

    def __init__(self, gym_id, history):
        self.by_fingerprint = {}
        if history is None:
            return
        for method, date_key in (("list_occupied_scenes", "used_date"),
                                 ("list_publish_claims", "claim_date")):
            reader = getattr(history, method, None)
            if reader is None:
                continue
            for row in reader(gym_id, MAX_HISTORY_ROWS_PER_GYM) or []:
                if row.get("tenant_id") != gym_id:
                    continue  # tenant boundary: silently drop, never classify
                fp = _fingerprint(row.get("fingerprint"))
                if not fp:
                    continue
                day = str(row.get(date_key) or row.get("used_date") or "")
                entry = {
                    "date": day,
                    "group_key": str(row.get("group_key") or ""),
                    "calendar_row_id": (str(row["calendar_row_id"])
                                        if row.get("calendar_row_id") else None),
                    "asset_id": (str(row["asset_id"])
                                 if row.get("asset_id") else None),
                }
                self.by_fingerprint.setdefault(fp, []).append(entry)

    def entries_for(self, fingerprint):
        return self.by_fingerprint.get(fingerprint, [])

    def proves_row(self, fingerprint, row_id, group_key, date):
        """True only when an authoritative history entry binds the EXACT
        calendar row, group and date to this fingerprint."""
        if not (row_id and group_key and date):
            return False
        for e in self.entries_for(fingerprint):
            if (e["calendar_row_id"] == str(row_id)
                    and e["group_key"] == str(group_key)
                    and e["date"] == str(date)):
                return True
        return False

    def dates_for(self, fingerprint):
        return {e["date"] for e in self.entries_for(fingerprint)}

    def proves_asset(self, fingerprint, asset_id):
        """True only when a history entry binds THIS particular asset."""
        if not asset_id:
            return False
        return any(e["asset_id"] == str(asset_id)
                   for e in self.entries_for(fingerprint))


def _classify_published_row(row, gym_id, media_store, history_index):
    """(classification, reasons, fingerprint) for one historical published
    calendar row. PROVED requires an exact binding: the row's group, its
    non-blank date, its fingerprint and its row id must all match ONE
    authoritative occupied/claim history entry, and a bound source asset
    must be an approved same-tenant asset. Anything weaker is UNRESOLVED
    with an explicit reason. Reasons are ordered: the first blocking reason
    is the primary one; later reasons add context."""
    reasons = []
    asset_id = row.get("source_media_asset_id")
    byte_hash = row.get("byte_hash")
    drive_file_id = row.get("drive_file_id")

    asset = None
    if asset_id:
        asset = media_store.get_asset(asset_id)
        if asset is None:
            return UNRESOLVED, ["asset_missing"], None
        if asset.get("gym_id") != gym_id:
            # Tenant breach evidence: classify, never leak the asset's fields.
            return UNRESOLVED, ["tenant_mismatch"], None
        if str(asset.get("review_status") or "").lower() != "approved":
            # A published row whose bound source asset is not approved is
            # never proved, regardless of any fingerprint match.
            return UNRESOLVED, ["asset_not_approved"], None
    elif not (byte_hash or drive_file_id):
        return UNRESOLVED, ["no_media_binding"], None

    fingerprint = _fingerprint(byte_hash)
    if fingerprint is None and asset is not None:
        fingerprint = _fingerprint(asset.get("content_hash"))
    if fingerprint is None:
        raw = byte_hash or (asset or {}).get("content_hash")
        reasons.append("fingerprint_missing" if not raw
                       else "fingerprint_unverifiable")
        return UNRESOLVED, reasons, None

    day = _row_date(row)
    if not day:
        return UNRESOLVED, ["missing_date"], fingerprint
    group = str(row.get("visual_group_key") or row.get("group_key") or "")
    if not group:
        return UNRESOLVED, ["group_missing"], fingerprint

    entries = history_index.entries_for(fingerprint)
    if not entries:
        return UNRESOLVED, ["no_history_record"], fingerprint
    if history_index.proves_row(fingerprint, row.get("id"), group, day):
        return PROVED, ["row_bound_on_history"], fingerprint
    dates = history_index.dates_for(fingerprint)
    if day not in dates:
        return UNRESOLVED, ["history_date_mismatch"], fingerprint
    # Same fingerprint and date exist, but no entry binds THIS row/group:
    # near-match evidence is explicitly not proof.
    return UNRESOLVED, ["history_row_binding_mismatch"], fingerprint


def _classify_approved_asset(asset, gym_id, history_index):
    """Approved library assets distinguish USED SCENE BYTES (the exact bytes
    appear on the history, but no entry binds this particular asset) from
    PROVED identity (an entry binds this asset id)."""
    fingerprint = _fingerprint(asset.get("content_hash"))
    if fingerprint is None:
        raw = asset.get("content_hash")
        return UNRESOLVED, ["fingerprint_missing" if not raw
                            else "fingerprint_unverifiable"], None
    if not history_index.entries_for(fingerprint):
        return UNRESOLVED, ["never_published"], fingerprint
    if history_index.proves_asset(fingerprint, asset.get("id")):
        return PROVED, ["asset_bound_on_history"], fingerprint
    return USED_BYTES, ["scene_bytes_on_history_asset_unbound"], fingerprint


def _sort_queue(items):
    # Deterministic: photos first, then newest date first, then stable ref
    # tiebreak. Python sorts are stable, so apply the keys inside-out.
    items.sort(key=lambda i: str(i["ref"]))
    items.sort(key=lambda i: i.get("date") or "", reverse=True)
    items.sort(key=lambda i: 0 if i["media_kind"] == "photo" else 1)
    return items


def build_review_queue(gym_id, calendar, media_store, history=None, *,
                       reviewed_before=None,
                       max_published=MAX_PUBLISHED_ROWS_PER_GYM,
                       max_assets=MAX_APPROVED_ASSETS_PER_GYM,
                       max_items=MAX_QUEUE_ITEMS):
    """The bounded read-only review queue for ONE gym. ``calendar`` exposes
    list_published_rows(gym_id, limit); ``media_store`` the
    SupabaseMediaStore/FakeMediaStore read surface; ``history`` the assumed
    Child A draft-history reader (None = no history applied yet, so nothing
    can prove identity). ``reviewed_before`` (ISO date/datetime) limits the
    asset lane to OLDER approved assets."""
    if not gym_id:
        raise ValueError("gym_id is required (tenant isolation)")

    history_index = _HistoryIndex(gym_id, history)
    items = []

    rows = calendar.list_published_rows(gym_id, max_published) or []
    for row in rows[:max_published]:
        if row.get("gym_id") != gym_id:
            continue  # tenant boundary: drop before classification
        classification, reasons, fingerprint = _classify_published_row(
            row, gym_id, media_store, history_index)
        items.append({
            "lane": "published_row",
            "ref": row.get("id"),
            "gym_id": gym_id,
            "date": _row_date(row),
            "media_kind": "photo" if _is_photo(row) else "video",
            "fingerprint": fingerprint,
            "classification": classification,
            "reasons": reasons,
        })

    assets = media_store.list_assets(gym_id) or []
    approved = []
    for asset in assets:
        if asset.get("gym_id") != gym_id:
            continue  # tenant boundary (list_assets already scopes; belt+braces)
        if str(asset.get("review_status") or "").lower() != "approved":
            continue
        if reviewed_before and str(asset.get("reviewed_at") or "") > str(reviewed_before):
            continue
        approved.append(asset)
    approved.sort(key=lambda a: str(a.get("reviewed_at") or ""))
    for asset in approved[:max_assets]:
        classification, reasons, fingerprint = _classify_approved_asset(
            asset, gym_id, history_index)
        items.append({
            "lane": "approved_asset",
            "ref": asset.get("id"),
            "gym_id": gym_id,
            "date": str(asset.get("reviewed_at") or "")[:10],
            "media_kind": "photo" if _is_photo(asset) else "video",
            "fingerprint": fingerprint,
            "classification": classification,
            "reasons": reasons,
        })

    return _sort_queue(items)[:max_items]


def summarize(items):
    counts = {PROVED: 0, USED_BYTES: 0, UNRESOLVED: 0}
    reason_counts = {}
    for item in items:
        counts[item["classification"]] += 1
        for reason in item["reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {"proved_identity": counts[PROVED],
            "used_scene_bytes": counts[USED_BYTES],
            "unresolved": counts[UNRESOLVED],
            "reasons": reason_counts, "total": len(items)}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="DRAFT/OFF read-only scene-history review queue "
                    "(no writes, no apply mode).")
    parser.add_argument("gyms", nargs="+", help="gym ids to review")
    parser.add_argument("--reviewed-before", default=None,
                        help="only approved assets reviewed at/before this ISO date")
    args = parser.parse_args(argv)

    # Stores are wired by the operator at run time; this module never
    # constructs a production client on its own and never arms anything.
    _log("DRAFT/OFF: read-only classification only — no writes, no flags.")
    _log("wire calendar/media/history readers and call build_review_queue(); "
         "the CLI carries no store credentials by design.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
