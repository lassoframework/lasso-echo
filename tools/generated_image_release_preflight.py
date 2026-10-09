"""Read-only diagnostic for the blocked production-shaped image install.

This is deliberately NOT an installer. It checks six exact source files and
reconciles a catalog receipt. No pending SQL, verifier, DDL, role grant, customer
row query or flag change is executed. The existing synthetic full-manifest tests
remain separate evidence. A complete baseline and compatible entry overlay are
required before an installation/rehearsal runner can be implemented.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "ooqcvmcjspeltuuhcvlh"
CLAIM = "claim_calendar_publish_slot_owned"
IDENTITY = ("p_row_id uuid, p_gym_id text, p_day date, p_timezone text, "
            "p_capacity integer, p_approved_only boolean, p_require_approval_proof boolean")
LIVE_BODY_MD5 = "c624eedcee819496129639108be991f6"
ENTRY_EXPECTED_MD5 = "db59bf4d6d0be4c42e5e49ab0b5a8b4f"
ENTRY_OVERLAY_MD5 = "7c09844f2c8c00190e007b1bca83d2a0"
ACL = ["postgres=X/postgres", "service_role=X/postgres"]
PINS = {
    "migrations/lasso_october7_catchup_capacity_20261008.sql": "c8f1c4c3af064e17df3670252153b2698a64ec607271127093d0f9aaf1d26927",
    "migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql": "1818dc65918f1640932e39e198b8b60ad8576131124a832b592f5ba2208d326a",
    "migrations/DRAFT_fixer_forward_corpus_atomic_cutover_20261008.sql": "9ba1c7cbfc25a197abeb26f12c9b439f849491ad0fab1b60a4bdb07446426d57",
    "tests/fixtures/portal_0611_echo_source_brand_bundle_held_20261008.sql": "55374ffe33677a1ef8ccbb41256c8abc9634645d0d9658f55ebc40fd1bc21dd2",
    "tests/fixtures/generated_client_release_manifest/0611_echo_source_brand_bundle.verify.sql": "4e99445cf04fff7046d8da64d13e02eb5ca875a7cc988c64cd9ad0e08a67063f",
    "tests/fixtures/generated_client_release_manifest/DRAFT_0625_generated_client_approval.sql": "d54a5b9e1f857d31912540f38f626b59c891296a4aebd7b3c2d55a1f4abdc705",
}
SQL0611_SHA = PINS["tests/fixtures/portal_0611_echo_source_brand_bundle_held_20261008.sql"]
SQL0611_VERSION = "20261009143836"
_UNSET = object()

# Catalog/ledger only. No customer rows, stored function bodies or credentials
# leave this query. The identity is the existing 0611 operator's frozen target.
CATALOG_SQL = """
select jsonb_build_object(
 'project_id', 'ooqcvmcjspeltuuhcvlh',
 'captured_at_utc', clock_timestamp(),
 'database', current_database(),
 'server_version', current_setting('server_version'),
 'system_identifier', (select system_identifier::text from pg_control_system()),
 'public_0611', coalesce((select jsonb_agg(jsonb_build_object(
   'filename',filename,'checksum',checksum)) from public.schema_migrations
   where filename like '0611%' or filename like '%echo_source_brand_bundle%'), '[]'::jsonb),
 'supabase_0611', coalesce((select jsonb_agg(jsonb_build_object(
   'version',version,'name',name,'sql_sha256',
   encode(sha256(convert_to(array_to_string(statements,''),'UTF8')),'hex')))
   from supabase_migrations.schema_migrations
   where version='0611' or name like '%0611%' or name like '%echo_source_brand_bundle%'), '[]'::jsonb),
 'ledger_0625', coalesce((select jsonb_agg(jsonb_build_object('version',version,'name',name))
   from supabase_migrations.schema_migrations
   where version='0625' or name like '%0625%' or name like '%generated_client_approval%'), '[]'::jsonb),
 'public_0625', coalesce((select jsonb_agg(filename) from public.schema_migrations
   where filename like '%0625%' or filename like '%generated_client_approval%'), '[]'::jsonb),
 'objects_0625', coalesce((select jsonb_agg(name order by name) from (
   select c.relname::text as name from pg_class c join pg_namespace n on n.oid=c.relnamespace
    where n.nspname='public' and c.relname='calendar_generated_artifact_versions'
   union all select p.proname::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname like 'calendar_generated_%'
   union all select a.attname::text from pg_attribute a
    where a.attrelid=to_regclass('public.content_calendar') and not a.attisdropped
     and a.attname in ('creative_origin','generated_artifact_version_id','generated_artifact_sha256')
 ) o), '[]'::jsonb),
 'claim', coalesce((select jsonb_agg(jsonb_build_object(
   'name',p.proname,'identity',pg_get_function_identity_arguments(p.oid),
   'owner',pg_get_userbyid(p.proowner),'body_md5',md5(p.prosrc),
   'acl',p.proacl::text[],'config',p.proconfig,'definer',p.prosecdef))
   from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='public' and p.proname='claim_calendar_publish_slot_owned'), '[]'::jsonb)
)
"""


def exact_files(root: Path = ROOT) -> dict:
    """Compare before connecting; a changed input never opens a DB connection."""
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in PINS}


def fresh_catalog(open_connection) -> dict:
    """Independent readback, including after an operator's uncertain prior ACK.

    The caller supplies a NEW authorized, TLS-verified connection. This helper
    has no prior-write handle and cannot retry an apply. Target identity is
    checked by reconciliation. Production transport provisioning is external.
    """
    db = open_connection()
    try:
        db.execute("BEGIN READ ONLY")
        db.execute("SET LOCAL statement_timeout='30s'; SET LOCAL lock_timeout='5s'")
        return db.execute(CATALOG_SQL).fetchone()[0]
    finally:
        try:
            db.rollback()
        finally:
            db.close()


def validated_receipt(value) -> dict | None:
    """Project typed catalog fields; never reflect unvalidated offline content."""
    if not isinstance(value, dict):
        return None

    def text(item):
        return isinstance(item, str) and len(item) <= 2048

    def rows(name, fields):
        items = value.get(name)
        if not isinstance(items, list) or len(items) > 1000:
            return None
        if any(not isinstance(row, dict) or any(key not in row or not check(row[key])
               for key, check in fields.items()) for row in items):
            return None
        return [{key: row[key] for key in fields} for row in items]

    def strings(items):
        return isinstance(items, list) and len(items) <= 1000 and all(text(item) for item in items)

    def md5(item):
        return isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item) is not None

    scalar_names = ("project_id", "database", "server_version", "system_identifier")
    if any(not text(value.get(name)) for name in scalar_names):
        return None
    captured = value.get("captured_at_utc")
    if not isinstance(captured, str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", captured) is None:
        return None
    try:
        parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError:
        return None
    result = {name: value[name] for name in scalar_names}
    result["captured_at_utc"] = parsed.isoformat()
    for name, fields in {
        "public_0611": {"filename": text, "checksum": text},
        "supabase_0611": {"version": text, "name": text, "sql_sha256": text},
        "ledger_0625": {"version": text, "name": text},
        "claim": {"name": text, "identity": text, "owner": text, "body_md5": md5,
                  "acl": strings, "config": lambda item: item is None or strings(item),
                  "definer": lambda item: isinstance(item, bool)},
    }.items():
        result[name] = rows(name, fields)
        if result[name] is None:
            return None
    for name in ("public_0625", "objects_0625"):
        if not strings(value.get(name)):
            return None
        result[name] = list(value[name])
    return result


def reconcile(receipt: dict) -> dict:
    receipt = validated_receipt(receipt)
    if receipt is None:
        return {"state": "invalid_catalog_receipt"}
    target_ok = all(receipt.get(k) == v for k, v in {
        "project_id": PROJECT, "database": "postgres", "server_version": "17.6",
        "system_identifier": "7642734024280108049",
    }.items())
    public, supabase = receipt.get("public_0611"), receipt.get("supabase_0611")
    ledger_ok = (public == [{"filename": "0611_echo_source_brand_bundle.sql", "checksum": SQL0611_SHA}]
                 and isinstance(supabase, list) and len(supabase) == 1
                 and supabase[0].get("name") == "0611_echo_source_brand_bundle"
                 and supabase[0].get("sql_sha256") == SQL0611_SHA
                 and supabase[0].get("version") == SQL0611_VERSION)
    pending0625 = all(receipt.get(k) == [] for k in ("ledger_0625", "public_0625", "objects_0625"))
    claims = receipt.get("claim")
    claim = claims[0] if isinstance(claims, list) and len(claims) == 1 else {}
    scope_ok = (claim.get("identity") == IDENTITY and claim.get("owner") == "postgres"
                and claim.get("name") == CLAIM and claim.get("definer") is True
                and claim.get("config") == ["search_path=public"]
                and sorted(claim.get("acl") or []) == sorted(ACL))
    return {
        "target": "matched" if target_ok else "drift",
        "0611": "already_applied_ledger_only" if target_ok and ledger_ok else "drift",
        "0611_catalog_full_verification": "not_performed_by_this_diagnostic",
        "0625": "pending" if target_ok and pending0625 else "drift_or_unverified_applied",
        "claim_scope": "matched" if target_ok and scope_ok else "drift",
        "claim_body_md5": claim.get("body_md5"),
        "claim_source": "current_whole_file_match" if target_ok and scope_ok and claim.get("body_md5") == LIVE_BODY_MD5 else "drift",
        "calendar_entry": "drift" if claim.get("body_md5") != ENTRY_EXPECTED_MD5 or not scope_ok or not target_ok else "body_prerequisite_only_matched",
        "entry_expected_md5": ENTRY_EXPECTED_MD5,
        "atomic_cutover_expected_overlay_md5": ENTRY_OVERLAY_MD5,
        "captured_at_utc": receipt.get("captured_at_utc"),
    }


def preflight(root: Path = ROOT, *, receipt=_UNSET, open_connection=None) -> dict:
    report = {"mode": "read_only", "mutation_supported": False,
              "installation_authorized": False, "production_rehearsal": "BLOCKED",
              "missing": ["owner-frozen complete baseline DDL/catalog/ACL export",
                          "whole entry/cutover overlay preserving current October 7 catchup body"]}
    try:
        actual = exact_files(root)
    except OSError:
        return {**report, "state": "file_missing"}
    mismatches = [name for name in PINS if actual[name] != PINS[name]]
    report["file_sha256"] = actual
    if mismatches:
        return {**report, "state": "hash_drift", "mismatches": mismatches}
    if receipt is not _UNSET and open_connection is not None:
        raise ValueError("Choose receipt reconciliation or a fresh connection")
    if open_connection is not None:
        receipt = fresh_catalog(open_connection)
    if receipt is _UNSET:
        return {**report, "state": "catalog_receipt_required"}
    receipt = validated_receipt(receipt)
    if receipt is None:
        return {**report, "state": "invalid_catalog_receipt"}
    return {**report, "state": "blocked", "catalog": reconcile(receipt)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-receipt", type=Path,
                        help="Offline JSON receipt from CATALOG_SQL; authenticity must be independently verified")
    args = parser.parse_args(argv)
    receipt = _UNSET
    if args.catalog_receipt:
        try:
            receipt = json.loads(args.catalog_receipt.read_text())
        except (OSError, UnicodeError, ValueError, RecursionError):
            # Do not echo the file, path, parser details or arbitrary contents.
            receipt = None
    result = preflight(receipt=receipt)
    print(json.dumps(result, indent=2, sort_keys=True))
    # Every current state is a no-go. No success exit implies install acceptance.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
