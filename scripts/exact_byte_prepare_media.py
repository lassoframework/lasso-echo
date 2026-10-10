#!/usr/bin/env python3
"""Operator CLI: prepare protected exact-byte media and bind eligible rows.

MANUAL OPERATOR TOOL ONLY. Nothing runs this automatically. It never enables
the fence, never creates/demotes an approval, never sends, never touches an
approved/provenance-bearing/published/denied/killed/historical/claimed/
attempted row (pending/unapproved rows only), and never retries an
unknown commit outcome. Every bind goes through the owner-only
exact_byte_prepare_bind_20261010 RPC's complete before-image CAS; COMMIT is
the linearization point. If the commit outcome is UNKNOWN (lost connection /
error at commit), the CLI aborts the whole batch and prints the row id for
manual reconciliation against exact_byte_media_prepare_20261010.

Required environment (secrets only ever read from env, never logged):
  EXACT_BYTE_PREPARE_DSN             owner role psycopg DSN (exact_byte_owner_20261010)
  EXACT_BYTE_R2_ACCOUNT_ID           pinned R2 account id
  EXACT_BYTE_R2_BUCKET               protected bucket
  EXACT_BYTE_R2_PUBLIC_BASE_URL      https public base (no trailing slash)
  EXACT_BYTE_R2_PROTECTED_PREFIX     dedicated protected key prefix ending in '/'
  EXACT_BYTE_R2_ACCESS_KEY_ID        R2 object credential (read/write objects)
  EXACT_BYTE_R2_SECRET_ACCESS_KEY    R2 object credential secret
  EXACT_BYTE_LOCK_ATTESTATION_FILE   signed lock-rule envelope (JSON file)
  EXACT_BYTE_ADMIN_PUBLIC_KEY_FILE   pinned Ed25519 admin public key (32 raw bytes)
  EXACT_BYTE_SOURCE_HOST_ALLOWLIST   comma-separated allowed source hosts
                                     (exact host or dot-suffix; e.g.
                                     "pub-<account>.r2.dev,images.unsplash.com"
                                     to preserve the current R2 and Unsplash
                                     sources; empty/unset fails CLOSED)

Usage:
  python scripts/exact_byte_prepare_media.py --plan --row-id UUID [--row-id UUID ...]
  python scripts/exact_byte_prepare_media.py --execute --row-id UUID ... \
      --evidence-ref "operator ticket/reference" [--retention-seconds 1209600]

Exit codes: 0 ok (or plan), 1 refused/failed (batch aborted), 2 usage/config error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.exact_byte_media_prepare import (  # noqa: E402
    MediaPrepareError, prepare_calendar_row_media,
)
from agent.r2_immutable_media import (  # noqa: E402
    ImmutableMediaError, SignedLockRuleSource,
)

ENV_VARS = (
    "EXACT_BYTE_PREPARE_DSN", "EXACT_BYTE_R2_ACCOUNT_ID", "EXACT_BYTE_R2_BUCKET",
    "EXACT_BYTE_R2_PUBLIC_BASE_URL", "EXACT_BYTE_R2_PROTECTED_PREFIX",
    "EXACT_BYTE_R2_ACCESS_KEY_ID", "EXACT_BYTE_R2_SECRET_ACCESS_KEY",
    "EXACT_BYTE_LOCK_ATTESTATION_FILE", "EXACT_BYTE_ADMIN_PUBLIC_KEY_FILE",
    "EXACT_BYTE_SOURCE_HOST_ALLOWLIST",
)
# Preparation works only on pending/unapproved rows (2026-10-10
# independent-review ruling); a prepared row returns through the normal
# client reapproval flow before it can be approved and sent.
ELIGIBLE_STATUS = "pending"


class CliError(ValueError):
    pass


def require_env():
    config = {}
    missing = []
    for name in ENV_VARS:
        value = os.environ.get(name)
        if not value:
            missing.append(name)
        else:
            config[name] = value
    if missing:
        raise CliError("missing environment: %s" % ", ".join(missing))
    base = config["EXACT_BYTE_R2_PUBLIC_BASE_URL"]
    if not base.startswith("https://") or base != base.rstrip("/"):
        raise CliError("invalid EXACT_BYTE_R2_PUBLIC_BASE_URL")
    prefix = config["EXACT_BYTE_R2_PROTECTED_PREFIX"]
    if not prefix.endswith("/") or prefix.startswith("/"):
        raise CliError("invalid EXACT_BYTE_R2_PROTECTED_PREFIX")
    allowlist = tuple(h.strip() for h in
                      config["EXACT_BYTE_SOURCE_HOST_ALLOWLIST"].split(",")
                      if h.strip())
    if not allowlist:
        raise CliError("invalid EXACT_BYTE_SOURCE_HOST_ALLOWLIST")
    config["_source_allowlist"] = allowlist
    return config


def build_lock_source(config):
    attestation = Path(config["EXACT_BYTE_LOCK_ATTESTATION_FILE"]).read_bytes()
    public_key = Path(config["EXACT_BYTE_ADMIN_PUBLIC_KEY_FILE"]).read_bytes()
    return SignedLockRuleSource(lambda: attestation, public_key)


def build_s3(config):
    import boto3
    return boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % config["EXACT_BYTE_R2_ACCOUNT_ID"],
        aws_access_key_id=config["EXACT_BYTE_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=config["EXACT_BYTE_R2_SECRET_ACCESS_KEY"],
    )


def fetch_row(conn, row_id):
    """Complete row snapshot through the owner-only read RPC.

    The owner role has no SELECT grant on content_calendar by design; this
    narrow SECURITY DEFINER read path is the only snapshot channel.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select public.exact_byte_prepare_read_row_20261010(%s)",
            (row_id,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        raise CliError("row not found")
    return row[0]


def client_side_eligible(row):
    """Advisory pre-checks only; the RPC re-enforces every one server-side."""
    problems = []
    if row.get("status") != ELIGIBLE_STATUS:
        problems.append("status != %r" % ELIGIBLE_STATUS)
    if row.get("variant_status") != "active":
        problems.append("variant_status != 'active'")
    if row.get("publish_claim_token") or row.get("publish_reservation_day"):
        problems.append("active claim/lease")
    if row.get("published_at") or row.get("late_post_id"):
        problems.append("historical row")
    if not row.get("image_url"):
        problems.append("missing image_url")
    # Fail closed on ANY approved/provenance-bearing row, including legacy
    # 'approved' rows whose approval_digest is NULL: rebinding media would
    # silently publish new bytes under a stale approval. Advisory only; the
    # RPC re-enforces server-side and never recomputes/rewrites/forges an
    # approval.
    for field in ("approval_digest", "approval_kind", "approved_by", "approved_at"):
        if row.get(field):
            problems.append("recorded approval provenance (%s); media change "
                            "needs fresh review" % field)
    return problems


def image_fields_for(row):
    return ("image_url", "thumbnail_url") if row.get("thumbnail_url") else ("image_url",)


def plan(conn, row_ids):
    report = []
    for row_id in row_ids:
        try:
            row = fetch_row(conn, row_id)
            problems = client_side_eligible(row)
            report.append({"row_id": row_id, "eligible": not problems,
                           "problems": problems,
                           "image_url": row.get("image_url"),
                           "thumbnail_url": row.get("thumbnail_url")})
        except CliError as e:
            report.append({"row_id": row_id, "eligible": False, "problems": [str(e)]})
    return report


def execute(conn, config, row_ids, evidence_ref, retention_seconds):
    s3 = build_s3(config)
    lock_source = build_lock_source(config)
    results = []
    for row_id in row_ids:
        # Read the reviewed before-image in its own committed read-only
        # transaction; preparation performs no database writes.
        row = fetch_row(conn, row_id)
        try:
            conn.commit()  # end the read-only transaction; no writes exist here
        except Exception:
            raise CliError("row %s read transaction unavailable" % row_id)
        problems = client_side_eligible(row)
        if problems:
            raise CliError("row %s not eligible: %s" % (row_id, "; ".join(problems)))
        retention_until = int(time.time()) + retention_seconds
        try:
            bindings = prepare_calendar_row_media(
                row, image_fields=image_fields_for(row),
                bucket=config["EXACT_BYTE_R2_BUCKET"],
                account_id=config["EXACT_BYTE_R2_ACCOUNT_ID"],
                public_base_url=config["EXACT_BYTE_R2_PUBLIC_BASE_URL"],
                protected_prefix=config["EXACT_BYTE_R2_PROTECTED_PREFIX"],
                s3=s3, lock_source=lock_source,
                source_allowlist=config["_source_allowlist"],
                required_retention_until=retention_until)
        except (MediaPrepareError, ImmutableMediaError) as e:
            raise CliError("row %s preparation failed: %s" % (row_id, e))
        thumbnail_url = next((b.prepared_url for b in bindings if b.role == "thumbnail"), None)
        prepare_id = str(uuid.uuid4())
        # An RPC refusal is a definite no-write: roll back and abort the row.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select public.exact_byte_prepare_bind_20261010(%s,%s,%s,%s,%s,%s,%s)",
                    (prepare_id, row_id, json.dumps(row),
                     bindings[0].prepared_url, thumbnail_url,
                     json.dumps([b.receipt() for b in bindings]), evidence_ref))
                result = cur.fetchone()[0]
        except Exception as e:
            conn.rollback()
            raise CliError("row %s bind refused: %s"
                           % (row_id, getattr(e, "diag", None) and
                              getattr(e.diag, "message_primary", None) or "rpc refusal"))
        # COMMIT is the bind's linearization point. ANY failure here (lost
        # connection, timeout, error after the server committed) is an UNKNOWN
        # outcome: abort the whole batch and never retry this row blindly.
        try:
            conn.commit()
        except Exception:
            raise CliError(
                "UNKNOWN COMMIT OUTCOME for row %s (prepare_id %s): batch aborted; "
                "reconcile exact_byte_media_prepare_20261010 manually before any retry"
                % (row_id, prepare_id))
        results.append({"row_id": row_id, "prepare_id": prepare_id,
                        "rpc_result": result,
                        "bindings": [b.receipt() for b in bindings]})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="read-only eligibility report")
    mode.add_argument("--execute", action="store_true", help="prepare + bind via owner RPC")
    parser.add_argument("--row-id", action="append", required=True,
                        help="calendar row UUID (repeatable); validated strictly")
    parser.add_argument("--evidence-ref", help="required with --execute")
    parser.add_argument("--retention-seconds", type=int, default=14 * 86400,
                        help="lock horizon covering provider fetch/retry window")
    args = parser.parse_args(argv)
    row_ids = []
    for raw in args.row_id:
        try:
            row_ids.append(str(uuid.UUID(raw)))
        except ValueError:
            print(json.dumps({"error": "invalid row id: %r" % raw}))
            return 2
    if args.execute and not args.evidence_ref:
        print(json.dumps({"error": "--evidence-ref required with --execute"}))
        return 2
    if args.retention_seconds <= 0:
        print(json.dumps({"error": "--retention-seconds must be positive"}))
        return 2
    try:
        config = require_env()
    except CliError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    import psycopg
    try:
        conn = psycopg.connect(config["EXACT_BYTE_PREPARE_DSN"], autocommit=False)
    except Exception:
        print(json.dumps({"error": "database connection unavailable"}))
        return 2
    try:
        if args.plan:
            output = {"mode": "plan", "rows": plan(conn, row_ids)}
        else:
            output = {"mode": "execute",
                      "rows": execute(conn, config, row_ids, args.evidence_ref,
                                      args.retention_seconds)}
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except CliError as e:
        print(json.dumps({"error": str(e), "aborted": True}))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
