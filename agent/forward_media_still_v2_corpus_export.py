"""Offline, unapproved still-v2 baseline proposal from frozen saved evidence.

No connections, provider fetches, signing, provisioning or approval exist here.
Use the exact original collector snapshot alongside its hash-only manifest.
``current`` is a complete schema-2 cohort with collector row fields plus the
DB-computed ``calendar_visual_digest``. An independently retained expected
cohort digest prevents silently substituting a stale/different input file.

``reviews`` binds snapshot_sha256 and manifest_digest, and contains records
indexed by row_ref_sha256. Each record binds calendar_visual_digest and the
exact calendar published_binding_ref, with media_kind and review_evidence_ref.
Still records additionally bind inspected_sha256 and immutable_bytes_ref.
Video records require video_classification_ref; their frames stay unreviewed.
References are evidence pointers for independent review, never authentication
or proof of historical byte immutability. Missing evidence remains unresolved.

The private proposal includes rows_json, excluded_rows_json and an unsigned
candidate worksheet with NO visual nonmatch decisions. scope_complete is
always false, even with no holds. The calendar cohort is not a complete claim
receipt spine: committed/in-flight claims must be reconciled separately before
any baseline can be independently approved. Recheck live digests at sealing.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import re
import stat
from urllib.parse import urlsplit

from .historical_published_visual_corpus import (
    CorpusError, load_snapshot, _validate_resume_records,
)
from .forward_media_photo_certificate import MAX_CORPUS_ROWS, canonical, digest

_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BARE_SHA = re.compile(r"[0-9a-f]{64}\Z")
MAX_INPUT_BYTES = 16 * 1024 * 1024
SCOPE = "published_still_images_and_derivatives"
LEGACY_FIELDS = frozenset({"id", "gym_id", "status", "published_at", "image_url",
                          "late_post_id", "post_date", "source_media_asset_id", "source_media_url"})

# Execute as one statement in BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
# save the result privately, then COMMIT. No REST pagination/count assumptions.
# This projection matches fixer_forward_media_photo_content_20261007 exactly.
# The bound rejects an oversized cohort rather than returning a truncated one.
# Re-read under a NEW transaction before sealing; this SQL is not a live fence.
CURRENT_COHORT_SQL = """
WITH cohort AS MATERIALIZED (
 SELECT r.id,
   jsonb_build_object('id',r.id,'gym_id',r.gym_id,'status',r.status,
     'published_at',r.published_at,'image_url',r.image_url,
     'late_post_id',r.late_post_id,'post_date',r.post_date,
     'source_media_asset_id',r.source_media_asset_id,
     'source_media_url',r.source_media_url) AS legacy_projection,
   jsonb_build_object('calendar_row_id',r.id,'tenant_id',r.gym_id,
     'group_key',r.visual_group_key,'post_date',r.post_date,
     'source_asset_id',r.source_media_asset_id,'source_url',r.source_media_url,
     'image_url',r.image_url,'thumbnail_url',r.thumbnail_url,
     'account',r.account,'format',r.format,'caption',r.caption,
     'gbp_location_id',r.gbp_location_id) AS visual_content
 FROM public.content_calendar r
 WHERE r.status='published' OR r.published_at IS NOT NULL OR r.late_post_id IS NOT NULL
), counted AS (SELECT count(*) AS n FROM cohort), packed AS (
 SELECT coalesce(jsonb_agg(jsonb_build_object('legacy_projection',legacy_projection,
   'calendar_visual_digest','sha256:'||encode(sha256(convert_to(visual_content::text,'UTF8')),'hex'))
   ORDER BY id),'[]'::jsonb) AS rows
 FROM cohort WHERE (SELECT n FROM counted)<=2500
)
SELECT jsonb_build_object('schema_version',2,'complete',n<=2500,
 'row_count',n,'rows',rows,'db_rows_json',rows::text,
 'db_cohort_digest','sha256:'||encode(sha256(convert_to(rows::text,'UTF8')),'hex'))
FROM counted CROSS JOIN packed
""".strip()


class CorpusExportHold(ValueError):
    """Static reason only; private identities/URLs never appear in errors."""


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4096


def _rows(snapshot):
    if not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise CorpusExportHold("cohort_shape_invalid")
    try:
        rows, snapshot_sha = load_snapshot(snapshot)
    except CorpusError:
        raise CorpusExportHold("cohort_shape_invalid") from None
    # A single row ID at two versions is ambiguous even if collector permits it.
    if len(rows) > MAX_CORPUS_ROWS or len({r["row_id"] for r in rows}) != len(rows):
        raise CorpusExportHold("cohort_duplicate_or_oversized")
    return rows, snapshot_sha


def current_rows(current):
    if not isinstance(current, dict) or type(current.get("schema_version")) is not int or current["schema_version"] != 2:
        raise CorpusExportHold("current_cohort_invalid")
    rows, _ = _rows({**current, "schema_version": 1})
    original = {r["row_id"]: r for r in current["rows"]}
    for row in rows:
        sql_digest = original[row["row_id"]].get("calendar_visual_digest")
        if not isinstance(sql_digest, str) or not _SHA.fullmatch(sql_digest):
            raise CorpusExportHold("current_sql_visual_digest_required")
        row["calendar_visual_digest"] = sql_digest
    return rows


def cohort_digest(current):
    """Canonical Python input digest; NOT PostgreSQL jsonb/spine digest."""
    return digest(current_rows(current))


def cohort_from_sql_export(export, *, expected_db_cohort_digest):
    """Normalize a frozen CURRENT_COHORT_SQL result, without DB/network I/O.

    Preserve the original census revision algorithm: SHA256 of the nine-field
    legacy JSON projection. Timestamp serialization differences fail reuse
    safely; do not rewrite a saved revision to make old evidence fit.
    The expected DB digest must come from the acquisition receipt separately.
    """
    if (not isinstance(export, dict) or type(export.get("schema_version")) is not int
            or export["schema_version"] != 2 or export.get("complete") is not True
            or not isinstance(expected_db_cohort_digest, str)
            or not _SHA.fullmatch(expected_db_cohort_digest)
            or export.get("db_cohort_digest") != expected_db_cohort_digest
            or not isinstance(export.get("rows"), list)
            or not isinstance(export.get("db_rows_json"), str)
            or len(export["db_rows_json"].encode()) > MAX_INPUT_BYTES
            or type(export.get("row_count")) is not int
            or export["row_count"] != len(export["rows"])):
        raise CorpusExportHold("frozen_sql_export_invalid")
    try:
        parsed = json.loads(export["db_rows_json"], object_pairs_hook=_unique_object)
    except (ValueError, CorpusExportHold):
        raise CorpusExportHold("frozen_sql_export_invalid") from None
    # PostgreSQL's text serialization differs from Python canonical JSON.
    # Hash the exact DB text and also check it encodes the supplied row objects.
    if ("sha256:" + hashlib.sha256(export["db_rows_json"].encode()).hexdigest() != expected_db_cohort_digest
            or parsed != export["rows"]):
        raise CorpusExportHold("frozen_sql_export_digest_mismatch")
    rows = []
    for item in export["rows"]:
        raw = item.get("legacy_projection") if isinstance(item, dict) else None
        if (not isinstance(raw, dict) or set(raw) != LEGACY_FIELDS
                or not (raw["status"] == "published" or raw["published_at"] is not None or raw["late_post_id"] is not None)):
            raise CorpusExportHold("sql_sent_signal_projection_invalid")
        rows.append({"row_id": raw["id"], "revision": digest(raw)[7:],
                     "gym_id": raw["gym_id"], "published_at": raw["published_at"],
                     "image_url": raw["image_url"],
                     "calendar_visual_digest": item.get("calendar_visual_digest")})
    current = {"schema_version": 2, "complete": True, "row_count": len(rows), "rows": rows}
    current["rows"] = current_rows(current)
    return current


def _review_index(reviews, snapshot_sha, manifest_digest, saved_rows):
    if (not isinstance(reviews, dict) or type(reviews.get("schema_version")) is not int
            or reviews["schema_version"] != 1 or reviews.get("snapshot_sha256") != snapshot_sha
            or reviews.get("manifest_digest") != manifest_digest
            or not isinstance(reviews.get("records"), list)
            or len(reviews["records"]) > MAX_CORPUS_ROWS):
        raise CorpusExportHold("saved_review_manifest_stale_or_invalid")
    expected = {digest([r["row_id"], r["revision"]])[7:] for r in saved_rows}
    indexed = {}
    for review in reviews["records"]:
        ref = review.get("row_ref_sha256") if isinstance(review, dict) else None
        if not isinstance(ref, str) or not _BARE_SHA.fullmatch(ref) or ref not in expected or ref in indexed:
            raise CorpusExportHold("saved_review_duplicate_or_unknown_row")
        indexed[ref] = review
    return indexed


def reconcile(current, saved_snapshot, saved_manifest, reviews, *, expected_cohort_digest):
    """Propose unapproved baseline arrays and explicit per-row review holds."""
    now = current_rows(current)
    current_digest = digest(now)
    if not isinstance(expected_cohort_digest, str) or not _SHA.fullmatch(expected_cohort_digest) or current_digest != expected_cohort_digest:
        raise CorpusExportHold("current_cohort_stale_or_substituted")
    saved, snapshot_sha = _rows(saved_snapshot)
    if not isinstance(saved_manifest, dict) or type(saved_manifest.get("schema_version")) is not int or saved_manifest["schema_version"] != 1:
        raise CorpusExportHold("saved_byte_manifest_invalid")
    try:
        records = _validate_resume_records(saved_manifest, saved, snapshot_sha)
    except CorpusError:
        raise CorpusExportHold("saved_byte_manifest_stale_or_invalid") from None
    manifest_digest = digest(saved_manifest)
    indexed_reviews = _review_index(reviews, snapshot_sha, manifest_digest, saved)
    old = {r["row_id"]: r for r in saved}
    rows, excluded, worksheet = [], [], []
    for row in now:
        key = "calendar:" + row["row_id"]
        ref = digest([row["row_id"], row["revision"]])[7:]
        proposal = {"history_key": key, "calendar_visual_digest": row["calendar_visual_digest"],
                    "published_binding_ref": key, "resolved": False,
                    "media_kind": "unknown", "visual_sha256": None}
        reason = None
        prior = old.get(row["row_id"])
        if prior is None:
            reason = "added_since_saved_snapshot"
        elif any(prior[field] != row[field] for field in prior):
            reason = "identity_revision_tenant_date_or_url_changed"
        if reason is None:
            try:
                url = urlsplit(row["image_url"]) if isinstance(row["image_url"], str) else None
                valid_url = (url is not None and url.scheme == "https" and url.hostname
                             and not url.username and not url.password
                             and url.port in (None, 443) and not url.fragment)
            except ValueError:
                valid_url = False
            if not valid_url:
                reason = "published_url_missing_or_ambiguous"
        review = indexed_reviews.get(ref)
        record = records.get(ref)
        if reason is None:
            if review is None:
                reason = "review_evidence_missing"
            elif (review.get("calendar_visual_digest") != row["calendar_visual_digest"]
                    or review.get("published_binding_ref") != key
                    or not _text(review.get("review_evidence_ref"))):
                reason = "review_visual_digest_or_binding_stale"
            elif review.get("media_kind") == "video":
                if not _text(review.get("video_classification_ref")):
                    reason = "video_classification_evidence_missing"
                else:
                    proposal.update(media_kind="reviewed_video_scope_exclusion", resolved=True,
                                     video_frames_reviewed=False,
                                     review_evidence_ref=review["review_evidence_ref"],
                                     video_classification_ref=review["video_classification_ref"])
            elif review.get("media_kind") == "still_photo":
                if record is None or record.get("status") != "hashed":
                    reason = "saved_immutable_byte_evidence_missing_or_unknown"
                elif (review.get("inspected_sha256") != "sha256:" + record["image_sha256"]
                        or not _text(review.get("immutable_bytes_ref"))):
                    reason = "review_inspected_bytes_unbound"
                else:
                    proposal.update(media_kind="still_photo", resolved=True,
                                     visual_sha256=review["inspected_sha256"],
                                     immutable_bytes_ref=review["immutable_bytes_ref"],
                                     review_evidence_ref=review["review_evidence_ref"])
            else:
                reason = "unknown_or_ambiguous_media_kind"
        if reason:
            proposal["hold_reason"] = reason
        (excluded if proposal["media_kind"] == "reviewed_video_scope_exclusion" else rows).append(proposal)
        worksheet.append({"row_ref_sha256": ref, "history_key_sha256": digest(key),
                          "calendar_visual_digest": row["calendar_visual_digest"],
                          "binding_sha256": digest(key), "media_kind": proposal["media_kind"],
                          "inspected_sha256": proposal["visual_sha256"],
                          "reconciliation_status": reason or "evidence_bound_pending_independent_approval",
                          "candidate_disposition": None, "candidate_review_evidence_ref": None})
    removed = sorted(digest([r["row_id"], r["revision"]]) for r in saved if r["row_id"] not in {x["row_id"] for x in now})
    holds = dict(sorted(Counter(x["reconciliation_status"] for x in worksheet if x["reconciliation_status"] != "evidence_bound_pending_independent_approval").items()))
    return {"schema_version": 2, "scope": SCOPE, "scope_complete": False,
            "policy_approved": False, "candidate_absence_means_unused": False,
            "independent_approval_required": True, "claims_reconciliation_required": True,
            "current_cohort_digest": current_digest, "saved_snapshot_sha256": snapshot_sha,
            "saved_manifest_digest": manifest_digest,
            "declared_full_fleet_row_count": len(now), "rows_json": rows,
            "excluded_rows_json": excluded, "removed_saved_row_refs_sha256": removed,
            "hold_counts": holds, "candidate_worksheet": {"candidate": None,
                "candidate_review_required": True, "video_frames_reviewed": False, "rows": worksheet}}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CorpusExportHold("json_duplicate_key")
        result[key] = value
    return result


def load_private_json(path):
    try:
        with open(path, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise CorpusExportHold("input_permissions_not_private")
            data = stream.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise CorpusExportHold("input_exceeds_bound")
        return json.loads(data, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(CorpusExportHold("json_nonfinite")))
    except CorpusExportHold:
        raise
    except (OSError, ValueError, UnicodeError):
        raise CorpusExportHold("input_unavailable_or_invalid") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("current", "saved-snapshot", "saved-manifest", "reviews", "expected-cohort-digest", "output"):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args(argv)
    try:
        proposal = reconcile(*(load_private_json(path) for path in
            (args.current, args.saved_snapshot, args.saved_manifest, args.reviews)),
            expected_cohort_digest=args.expected_cohort_digest)
        data = (canonical(proposal) + "\n").encode()
        # Never overwrite another lane's evidence. Output has private IDs/refs.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        print(canonical({"scope_complete": False, "row_count": proposal["declared_full_fleet_row_count"],
                         "hold_counts": proposal["hold_counts"],
                         "proposal_sha256": hashlib.sha256(data).hexdigest()}))
        return 0
    except (CorpusExportHold, OSError):
        print(canonical({"status": "hold", "reason": "offline_export_refused"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
