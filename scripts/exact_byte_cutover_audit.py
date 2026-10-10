#!/usr/bin/env python3
"""Read-only offline cutover auditor for the exact-byte send fence (20261010).

Audits a trusted operator-exported JSON snapshot of the
delivered_byte_send_fence_20261010.sql authority tables plus the published
content_calendar rows they reference. No live DB, no network, no mutations.
This is an initial-seed preflight only; SQL history_complete and digest checks
remain authoritative for activation.

The auditor enumerates:
  * every published calendar observation (and every required-but-missing one),
  * the canonical tenant -> target mapping and any unknown/missing targets,
  * every exact URL / byte digest pair and its historical occupancy,
  * historical cross-date and cross-tenant digest/URL conflicts,
  * unresolved or uncertain send attempts that block activation,
  * missing provider target / row revision / sibling evidence.

A historical DB-published URL byte observation is NEVER treated as a provider
receipt; it is reported as observation-only evidence.

Output is deterministic JSON (sorted keys, sorted blockers) on stdout.
Exit codes: 0 = ready (no blockers), 1 = blockers found, 2 = malformed export.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ACCOUNTS = ("instagram", "facebook", "googlebusiness")
FORMATS = ("feed", "story")
IMAGE_FIELDS_OK = (("image_url",), ("image_url", "thumbnail_url"))
OBSERVATION_KINDS = (
    "db_published_url_bytes",
    "provider_receipt_bytes",
    "platform_readback_bytes",
    "uncertain_bytes",
)
OUTCOMES = (
    "reserved_uncertain",
    "uncertain",
    "definite_no_send",
    "provider_accepted",
    "platform_verified",
)
MEDIA_COLUMNS = (
    "image_urls", "slide_urls", "media_urls", "carousel_urls",
    "media_items", "gbp_media", "video_url",
)

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
URL_RE = re.compile(r"^https://[^\s]+$")

SCHEMAS = {
    "tenants": ("calendar_gym_id", "canonical_tenant", "posting_timezone", "evidence_ref"),
    "targets": ("calendar_gym_id", "account", "format", "provider_target",
                "image_fields", "evidence_ref"),
    "sibling_proofs": ("shared_posting_identity", "canonical_tenant", "post_date",
                       "logical_post_id", "evidence_ref"),
    "sibling_members": ("calendar_row_id", "row_revision", "shared_posting_identity",
                        "evidence_ref"),
    "coverage": ("calendar_row_id", "row_revision", "complete", "no_images_verified",
                 "evidence_ref"),
    "observations": ("observation_id", "sha256", "byte_length", "observation_kind",
                     "evidence_ref", "observed_at", "recorded_at"),
    "calendar_rows": ("id", "gym_id", "account", "format", "post_date", "status"),
    "outcomes": ("attempt_id", "outcome", "evidence_ref", "recorded_at"),
}

_LISTS = tuple(SCHEMAS)


class ExportError(ValueError):
    pass


def _load(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExportError("invalid JSON: %s" % e)
    if not isinstance(data, dict):
        raise ExportError("export root must be a JSON object")
    for key, fields in SCHEMAS.items():
        if key not in data:
            raise ExportError("export missing required key %r" % key)
        value = data[key]
        if not isinstance(value, list):
            raise ExportError("export key %r must be a list" % key)
        for i, row in enumerate(value):
            if not isinstance(row, dict):
                raise ExportError("%s[%d] must be an object" % (key, i))
            missing = [f for f in fields if f not in row]
            if missing:
                raise ExportError("%s[%d] missing required fields: %s"
                                  % (key, i, ", ".join(missing)))
    unknown = set(data) - set(_LISTS) - {"cutover", "census"}
    if data.get("census") is not None and not isinstance(data.get("census"), dict):
        raise ExportError("export key 'census' must be an object")
    if unknown:
        raise ExportError("unknown export keys: %s" % ", ".join(sorted(unknown)))
    return data


def _is_published(row):
    return (row.get("status") == "published"
            or row.get("published_at") is not None
            or row.get("late_post_id") is not None)


def _row_urls(row):
    urls = []
    image_url = row.get("image_url")
    image_url = image_url if isinstance(image_url, str) else ""
    thumb = row.get("thumbnail_url")
    thumb = thumb if isinstance(thumb, str) else ""
    if image_url.strip(" "):
        urls.append(image_url)
    if thumb.strip(" ") and thumb not in urls:
        urls.append(thumb)
    return urls


def _media_present(row):
    for col in MEDIA_COLUMNS:
        value = row.get(col)
        # Match SQL's exact allowed JSON values: null, an empty array, or an
        # empty string. In particular, {} is non-null and must block.
        if value not in (None, "", []):
            return True
    return False


def audit(data):
    blockers = set()
    notes = set()

    def block(code, detail):
        blockers.add(json.dumps({"code": code, "detail": detail}, sort_keys=True))

    tenants = {t["calendar_gym_id"]: t for t in data["tenants"]}
    if len(tenants) != len(data["tenants"]):
        block("duplicate_tenant", "duplicate calendar_gym_id in tenant authority")
    for t in data["tenants"]:
        if not str(t.get("canonical_tenant") or "").strip():
            block("invalid_tenant",
                  "blank canonical_tenant for %s" % t["calendar_gym_id"])
        if not str(t["posting_timezone"]).strip():
            block("invalid_tenant", "blank posting_timezone for %s" % t["calendar_gym_id"])
        else:
            try:
                ZoneInfo(t["posting_timezone"])
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                block("unknown_timezone",
                      "posting_timezone %r is not an IANA zone for %s"
                      % (t["posting_timezone"], t["calendar_gym_id"]))

    target_keys = set()
    for t in data["targets"]:
        key = (t["calendar_gym_id"], t["account"], t["format"])
        if key in target_keys:
            block("duplicate_target", "duplicate target mapping %s/%s/%s" % key)
        target_keys.add(key)
        if t["account"] not in ACCOUNTS or t["format"] not in FORMATS:
            block("invalid_target", "unsupported account/format in %s" % (key,))
        pt = t.get("provider_target")
        if (not isinstance(pt, dict)
                or not str(pt.get("provider", "")).strip()
                or not str(pt.get("platform", "")).strip()
                or not str(pt.get("account_id", "")).strip()):
            block("invalid_provider_target",
                  "provider_target must declare provider/platform/account_id for %s" % (key,))
        if tuple(t.get("image_fields") or ()) not in IMAGE_FIELDS_OK:
            block("invalid_image_fields",
                  "image_fields %r not an audited declaration for %s"
                  % (t.get("image_fields"), key))
        if t["calendar_gym_id"] not in tenants:
            block("unknown_target_tenant",
                  "target %s references unmapped calendar_gym_id" % (key,))

    proofs = {p["shared_posting_identity"]: p for p in data["sibling_proofs"]}
    if len(proofs) != len(data["sibling_proofs"]):
        block("duplicate_sibling_proof", "duplicate shared_posting_identity")
    proof_triples = set()
    for p in data["sibling_proofs"]:
        if (not str(p.get("canonical_tenant") or "").strip()
                or not str(p.get("post_date") or "").strip()
                or not str(p.get("logical_post_id") or "").strip()):
            block("invalid_sibling_proof",
                  "sibling proof %s has blank canonical_tenant/post_date/logical_post_id"
                  % p.get("shared_posting_identity"))
        triple = (p["canonical_tenant"], p["post_date"], p["logical_post_id"])
        if triple in proof_triples:
            block("duplicate_sibling_proof",
                  "duplicate tenant/date/logical_post proof %s" % (triple,))
        proof_triples.add(triple)

    rows_by_id = {}
    for r in data["calendar_rows"]:
        rows_by_id.setdefault(r["id"], r)
    if len(rows_by_id) != len(data["calendar_rows"]):
        block("duplicate_calendar_row", "duplicate calendar row id in export")

    published = [r for r in data["calendar_rows"] if _is_published(r)]

    # Frozen export census: the operator must declare the expected total and
    # published calendar row counts captured when the snapshot was frozen.
    # A missing, zero, or mismatched census means the snapshot is incomplete
    # and must never read as ready.
    census = data.get("census")
    if not isinstance(census, dict):
        block("incomplete_export", "export lacks a frozen census")
    else:
        exp_total = census.get("calendar_rows")
        exp_pub = census.get("published_rows")
        if (not isinstance(exp_total, int) or not isinstance(exp_pub, int)
                or isinstance(exp_total, bool) or isinstance(exp_pub, bool)
                or exp_total < 1 or exp_pub < 1):
            block("incomplete_export",
                  "census must declare positive frozen calendar/published row totals")
        else:
            if exp_total != len(data["calendar_rows"]):
                block("census_mismatch",
                      "census expected %d calendar rows but export carries %d"
                      % (exp_total, len(data["calendar_rows"])))
            if exp_pub != len(published):
                block("census_mismatch",
                      "census expected %d published rows but export carries %d"
                      % (exp_pub, len(published)))

    coverage_by_row = {}
    for c in data["coverage"]:
        if not SHA256_RE.match(str(c.get("row_revision") or "")):
            block("invalid_row_revision",
                  "coverage for row %s lacks a sha256 row revision"
                  % c["calendar_row_id"])
        if c["calendar_row_id"] not in rows_by_id:
            block("unknown_coverage_row",
                  "coverage references unknown calendar row %s" % c["calendar_row_id"])
        if c["calendar_row_id"] in coverage_by_row:
            block("duplicate_coverage_row",
                  "duplicate coverage row for calendar row %s" % c["calendar_row_id"])
        coverage_by_row.setdefault(c["calendar_row_id"], c)
        if c.get("complete") is not True:
            block("incomplete_coverage",
                  "coverage complete=false for row %s" % c["calendar_row_id"])

    # Observation enumeration + digest occupancy census.
    obs_counts = {k: 0 for k in OBSERVATION_KINDS}
    obs_counts["malformed"] = 0
    digest_occupancy = {}  # sha256 -> set of (tenant, post_date)
    url_digests = {}       # exact_url -> set of sha256
    obs_index = set()      # (calendar_row_id, row_revision, exact_url)
    obs_entries = []       # (calendar_row_id, row_revision, exact_url, sha256)
    obs_ids = set()
    for o in data["observations"]:
        oid = o.get("observation_id")
        if oid in obs_ids:
            block("duplicate_observation", "duplicate observation_id %s" % oid)
        obs_ids.add(oid)
        kind = o.get("observation_kind")
        if kind not in OBSERVATION_KINDS:
            obs_counts["malformed"] += 1
            block("invalid_observation", "unknown observation_kind %r" % (kind,))
            continue
        obs_counts[kind] += 1
        if not SHA256_RE.match(str(o.get("sha256") or "")):
            obs_counts["malformed"] += 1
            block("invalid_observation", "observation %s lacks sha256 digest"
                  % o.get("observation_id"))
            continue
        if not isinstance(o.get("byte_length"), int) or not 1 <= o["byte_length"] <= 134217728:
            obs_counts["malformed"] += 1
            block("invalid_observation", "observation %s has invalid byte_length"
                  % o.get("observation_id"))
            continue
        url = str(o.get("exact_url") or "")
        if not URL_RE.match(url):
            obs_counts["malformed"] += 1
            block("invalid_observation", "observation %s has non-https exact_url"
                  % o.get("observation_id"))
            continue
        if o.get("calendar_row_id") and not o.get("row_revision"):
            block("missing_row_revision",
                  "observation %s tied to row %s has no row revision"
                  % (o.get("observation_id"), o["calendar_row_id"]))
        orow = rows_by_id.get(o.get("calendar_row_id")) if o.get("calendar_row_id") else None
        if o.get("calendar_row_id") and orow is None:
            block("unknown_observation_row",
                  "observation %s references unknown calendar row %s"
                  % (o.get("observation_id"), o["calendar_row_id"]))
        if orow is not None:
            orow_tenant = tenants.get(orow.get("gym_id"))
            if (orow_tenant is not None
                    and o.get("canonical_tenant") != orow_tenant["canonical_tenant"]):
                block("observation_tenant_mismatch",
                      "observation %s tenant does not match canonical tenant of row %s"
                      % (o.get("observation_id"), orow["id"]))
            if str(o.get("post_date")) != str(orow.get("post_date")):
                block("observation_date_mismatch",
                      "observation %s post_date does not match post_date of row %s"
                      % (o.get("observation_id"), orow["id"]))
        if not str(o.get("evidence_ref") or "").strip() or not str(o.get("observed_at") or "").strip():
            block("invalid_observation", "observation %s lacks evidence_ref/observed_at"
                  % o.get("observation_id"))
        digest_occupancy.setdefault(o["sha256"], set()).add(
            (o.get("canonical_tenant"), o.get("post_date")))
        url_digests.setdefault(url, set()).add(o["sha256"])
        obs_index.add((o.get("calendar_row_id"), o.get("row_revision"), url))
        obs_entries.append(
            (o.get("calendar_row_id"), o.get("row_revision"), url, o["sha256"]))

    # Historical cross-date conflicts: never collapse a digest to first tenant/date.
    for digest, occ in digest_occupancy.items():
        if len(occ) > 1:
            block("cross_date_digest_conflict",
                  "digest %s observed under multiple tenant/date occupancies: %s"
                  % (digest, sorted(json.dumps(sorted(o), default=str) for o in occ)))
    for url, digests in url_digests.items():
        if len(digests) > 1:
            block("url_digest_conflict",
                  "url %s observed with multiple digests" % url)

    # Cross-tenant duplicates and uncertain attempts.
    uncertain_attempts = 0
    for o in data["observations"]:
        if o.get("observation_kind") == "uncertain_bytes":
            uncertain_attempts += 1
            block("uncertain_send",
                  "uncertain byte observation %s for url %s cannot be reconciled"
                  % (o.get("observation_id"), o.get("exact_url")))
    # Derive the latest outcome per attempt deterministically. Append-only
    # attempts always start reserved_uncertain, so an old uncertain outcome
    # must not block when a later recorded outcome terminated successfully.
    # Ordering evidence is (recorded_at, outcome_id); identical ordering keys
    # with different outcomes are ambiguous and fail closed.
    attempt_records = {}
    for oc in data["outcomes"]:
        outcome = oc.get("outcome")
        if outcome not in OUTCOMES:
            block("invalid_outcome", "unknown outcome %r" % (outcome,))
            continue
        aid = oc.get("attempt_id")
        rec = oc.get("recorded_at")
        if not str(aid or "").strip() or not str(rec or "").strip():
            block("invalid_outcome",
                  "outcome for attempt %s lacks attempt_id/recorded_at" % (aid,))
            continue
        try:
            recorded_at = datetime.fromisoformat(str(rec).replace("Z", "+00:00"))
            if recorded_at.utcoffset() is None:
                raise ValueError("timestamp must include a timezone")
            recorded_at = recorded_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            block("invalid_outcome",
                  "outcome for attempt %s has malformed or timezone-naive recorded_at"
                  % (aid,))
            continue
        order = (recorded_at, str(oc.get("outcome_id") or ""))
        attempt_records.setdefault(aid, []).append((order, outcome))
    for aid, records in attempt_records.items():
        latest_order = max(r[0] for r in records)
        winners = {r[1] for r in records if r[0] == latest_order}
        if len(winners) > 1:
            block("ambiguous_outcome",
                  "attempt %s has ambiguous tied latest outcomes %s"
                  % (aid, sorted(winners)))
            continue
        latest = winners.pop()
        if latest in ("reserved_uncertain", "uncertain"):
            uncertain_attempts += 1
            block("uncertain_send",
                  "attempt %s latest recorded outcome is %s" % (aid, latest))

    # Every published calendar row must be fully observed or covered.
    missing_observations = 0
    current_rev_by_row = {}
    for row in published:
        rid = row["id"]
        tenant = tenants.get(row.get("gym_id"))
        if tenant is None:
            block("missing_tenant_mapping",
                  "published row %s has no canonical tenant mapping" % rid)
        key = (row.get("gym_id"), row.get("account"), row.get("format"))
        if tenant is not None and key not in target_keys:
            block("missing_provider_target",
                  "published row %s lacks provider target %s" % (rid, key))
        urls = _row_urls(row)
        cov = coverage_by_row.get(rid)
        # Every published row must carry exact-byte history coverage; the SQL
        # authority (exact_byte_history_complete_20261010) rejects any
        # published row without it, even if byte observations exist.
        if cov is None:
            block("missing_coverage",
                  "published row %s has no exact-byte history coverage" % rid)
        # The current revision authority is the row revision recorded on the
        # exported calendar row itself. Coverage and observations must match
        # it; a coverage row at any other (stale) revision must not redefine
        # the current revision.
        current_rev = row.get("row_revision")
        if not str(current_rev or "").strip():
            block("missing_row_revision",
                  "published row %s has no current row revision in the export" % rid)
            current_rev = None
        else:
            current_rev_by_row[rid] = current_rev
        if (cov is not None and current_rev is not None
                and cov.get("row_revision") != current_rev):
            block("coverage_revision_mismatch",
                  "coverage for published row %s is at a stale row revision "
                  "and does not match the exported row revision" % rid)
        # Unsupported nonempty media columns (slide_urls, media_urls, ...) are
        # rejected by the SQL authority for every published row, regardless of
        # coverage or image_url observations.
        if _media_present(row):
            block("unsupported_media",
                  "published row %s carries unsupported nonempty media columns" % rid)
        image_url = row.get("image_url")
        image_url = image_url if isinstance(image_url, str) else ""
        thumbnail_url = row.get("thumbnail_url")
        thumbnail_url = (thumbnail_url if isinstance(thumbnail_url, str)
                         else "")
        no_images_verified = (cov is not None
                              and cov.get("no_images_verified") is True)
        if (not image_url.strip(" ") and not thumbnail_url.strip(" ")
                and not no_images_verified):
            block("unverified_no_images",
                  "published row %s has no image URL but coverage does not verify no images"
                  % rid)
        if thumbnail_url.strip(" ") and not image_url.strip(" "):
            block("unsupported_thumbnail_only",
                  "published row %s has a thumbnail URL without an image_url" % rid)
        for url in urls:
            if (current_rev is None
                    or not any(rid == r and url == u and rev == current_rev
                               for (r, rev, u) in obs_index)):
                missing_observations += 1
                block("missing_observation",
                      "published row %s url %s has no exact byte observation "
                      "at the current row revision" % (rid, url))

    # Sibling evidence validation against actual row identity.
    for m in data["sibling_members"]:
        if not SHA256_RE.match(str(m.get("row_revision") or "")):
            block("invalid_row_revision",
                  "sibling member for row %s lacks a sha256 row revision"
                  % m.get("calendar_row_id"))
        proof = proofs.get(m.get("shared_posting_identity"))
        if proof is None:
            block("missing_sibling_evidence",
                  "sibling member for row %s references unknown proof %s"
                  % (m.get("calendar_row_id"), m.get("shared_posting_identity")))
            continue
        row = rows_by_id.get(m.get("calendar_row_id"))
        if row is not None:
            tenant = tenants.get(row.get("gym_id"))
            if tenant and tenant["canonical_tenant"] != proof["canonical_tenant"]:
                block("sibling_tenant_mismatch",
                      "row %s tenant does not match sibling proof tenant" % row["id"])
            if str(row.get("post_date")) != str(proof["post_date"]):
                block("sibling_date_mismatch",
                      "row %s post_date does not match sibling proof date" % row["id"])
    # Same-day sibling groups: more than one published row for a canonical
    # tenant/posting date means same-day sibling occupancy; each such row must
    # carry an independently proven sibling membership matching tenant+date.
    rows_by_tenant_date = {}
    for row in published:
        tenant = tenants.get(row.get("gym_id"))
        if tenant:
            rows_by_tenant_date.setdefault(
                (tenant["canonical_tenant"], str(row.get("post_date"))), []).append(row)
    member_index = {}
    for m in data["sibling_members"]:
        proof = proofs.get(m.get("shared_posting_identity"))
        if proof is not None:
            member_index[m["calendar_row_id"]] = proof
    for (ctenant, cdate), group in rows_by_tenant_date.items():
        if len(group) < 2:
            continue
        # Only rows that actually share one exact byte digest constitute a
        # single logical same-day post. Distinct digests are distinct posts
        # and must NOT be forced to carry sibling evidence.
        digest_groups = {}
        for row in group:
            rev = current_rev_by_row.get(row["id"])
            row_urls = set(_row_urls(row))
            for (_r, _rev, url, digest) in obs_entries:
                if _r == row["id"] and _rev == rev and url in row_urls:
                    digest_groups.setdefault(digest, []).append(row)
        for digest, dgroup in digest_groups.items():
            if len(dgroup) < 2:
                continue
            for row in dgroup:
                proof = member_index.get(row["id"])
                if (proof is None or proof["canonical_tenant"] != ctenant
                        or str(proof["post_date"]) != cdate):
                    block("missing_sibling_evidence",
                          "published row %s shares exact digest %s on posting day "
                          "%s/%s with sibling rows but lacks a matching proven "
                          "sibling membership" % (row["id"], digest, ctenant, cdate))

    observation_only = sum(1 for o in data["observations"]
                           if o.get("observation_kind") == "db_published_url_bytes")
    if observation_only:
        notes.add("%d db_published_url_bytes observation(s) are historical byte "
                  "observations only, never provider publication receipts"
                  % observation_only)

    cutover = data.get("cutover") or {}
    if cutover:
        if not COMMIT_RE.match(str(cutover.get("runtime_commit") or "")):
            block("invalid_cutover", "cutover runtime_commit is not a 40-hex commit")
        for f in ("deployment_evidence_ref", "complete_history_evidence_ref"):
            if not str(cutover.get(f) or "").strip():
                block("invalid_cutover", "cutover missing %s" % f)
    else:
        block("missing_cutover", "no proposed cutover evidence supplied")

    blocker_list = [json.loads(b) for b in sorted(blockers)]
    return {
        "ready": not blocker_list,
        "counts": {
            "tenants": len(data["tenants"]),
            "targets": len(data["targets"]),
            "sibling_proofs": len(data["sibling_proofs"]),
            "sibling_members": len(data["sibling_members"]),
            "coverage_rows": len(data["coverage"]),
            "coverage_incomplete": sum(1 for c in data["coverage"]
                                       if c.get("complete") is not True),
            "calendar_rows": len(data["calendar_rows"]),
            "published_rows": len(published),
            "observations": {k: v for k, v in sorted(obs_counts.items()) if v},
            "observation_only_db_published": observation_only,
            "missing_observations": missing_observations,
            "uncertain_attempts": uncertain_attempts,
        },
        "blockers": blocker_list,
        "blocker_total": len(blocker_list),
        "notes": sorted(notes),
    }


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as fh:
            data = _load(fh.read())
    except (ExportError, OSError) as e:
        print(json.dumps({"ready": False, "error": str(e)}, sort_keys=True))
        return 2
    report = audit(data)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
