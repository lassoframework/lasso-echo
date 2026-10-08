"""Disposable PostgreSQL acceptance for the draft owner persistence adapter.

Run as a standalone script with PostgreSQL 17 tools and psycopg installed in the
selected interpreter. No production DSN, network connection, or existing DB is used.
"""
from dataclasses import replace
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile

from agent.forward_media_owner import (
    ForwardMediaOwnerPersistence, ObjectReader, OwnerPersistenceError,
)
from agent.forward_media_prepare import build_render_manifest, prepare_generated_original

ROOT = Path(__file__).resolve().parents[1]
OWNER = "forward_media_owner_test"
ORIGINAL = b"synthetic original image bytes"
RENDER = b"synthetic rendered image bytes"


class Reader(ObjectReader):
    def read(self, url):
        return {
            "https://media.example.test/original.jpg": ORIGINAL,
            "https://media.example.test/render.jpg": RENDER,
        }[url]


def main():
    import psycopg

    for tool in ("initdb", "pg_ctl", "psql"):
        if not shutil.which(tool):
            raise SystemExit(f"BLOCKED: {tool} unavailable; no install attempted")
    with tempfile.TemporaryDirectory(prefix="forward_owner_pg_") as tmp:
        root = Path(tmp)
        sock = root / "sock"
        sock.mkdir()
        data = root / "data"
        port = random.randint(41000, 59000)
        subprocess.run(["initdb", "-D", str(data), "-U", "postgres", "--no-sync"],
                       check=True, capture_output=True, timeout=60)
        started = False
        try:
            subprocess.run(["pg_ctl", "-D", str(data), "-l", str(root / "pg.log"),
                            "-o", f"-k {sock} -p {port} -c listen_addresses=''",
                            "-w", "start"], check=True, capture_output=True, timeout=60)
            started = True
            psql = ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-h", str(sock),
                    "-p", str(port), "-U", "postgres", "-d", "postgres"]

            def sql(query):
                result = subprocess.run(psql, input=query, text=True,
                                        capture_output=True, timeout=30)
                if result.returncode:
                    raise AssertionError(result.stderr)
                return result.stdout.strip()

            def sql_denied(query):
                result = subprocess.run(psql, input=query, text=True,
                                        capture_output=True, timeout=30)
                assert result.returncode != 0, "unexpected SQL privilege bypass"
                assert "permission denied" in result.stderr.lower() \
                    or "immutable" in result.stderr.lower(), result.stderr

            sql("create role anon; create role authenticated; create role service_role login;"
                "create table public.content_calendar("
                "id uuid primary key,gym_id text,post_date date,account text,format text,"
                "gbp_location_id text,status text,variant_status text,published_at timestamptz,"
                "publish_claim_token uuid,publish_reservation_day date,late_post_id text,"
                "image_url text,thumbnail_url text,media_not_ready_reason text);")
            sql((ROOT / "migrations/DRAFT_fixer_forward_media_claim_20261006.sql").read_text())
            sql(f"create role {OWNER} login; "
                f"grant fixer_forward_media_owner_20261006 to {OWNER};")

            original, clearance = prepare_generated_original(
                "synthetic_tenant", "fresh_synthetic_asset",
                "https://media.example.test/original.jpg", ORIGINAL,
                "synthetic-generation-receipt", "synthetic-registry-evidence",
                "synthetic-independent-history-audit")
            manifest = build_render_manifest(
                original, "https://media.example.test/render.jpg", RENDER,
                "render", "synthetic-render-evidence", render_recipe={"op": "synthetic"})
            dsn = f"host={sock} port={port} user={OWNER} dbname=postgres"
            # Model the same explicit clean process contract as production.
            old_environment = dict(os.environ)
            from agent.forward_media_lane import RUNTIME_NAMES
            clean_environment = {k: v for k, v in old_environment.items() if k in RUNTIME_NAMES}
            clean_environment.update(FORWARD_MEDIA_OWNER_DSN=dsn, FORWARD_MEDIA_OWNER_ROLE=OWNER)
            os.environ.clear()
            os.environ.update(clean_environment)
            try:
                with psycopg.connect(dsn, autocommit=False) as conn:
                    owner = ForwardMediaOwnerPersistence(conn, OWNER, Reader())
                    assert owner.persist(original, clearance, manifest)["replayed"] is False
                    assert owner.persist(original, clearance, manifest)["replayed"] is True
                    # A distinct manifest is a permitted extension, not a
                    # conflict. Change the immutable existing registry tuple
                    # and its matching clearance to exercise real conflict.
                    bad_original = replace(original, registry_evidence_ref="changed-registry")
                    bad_clearance = replace(clearance, registry_evidence_ref="changed-registry")
                    try:
                        owner.persist(bad_original, bad_clearance, manifest)
                    except OwnerPersistenceError:
                        pass
                    else:
                        raise AssertionError("changed registry was accepted")
                assert sql("select count(*) from public.fixer_forward_media_original_registry_20261006") == "1"
                assert sql("select count(*) from public.fixer_forward_media_history_clearance_20261006") == "1"
                assert sql("select count(*) from public.fixer_forward_media_render_manifest_20261006") == "1"
                sql_denied(f"set role {OWNER}; update public.fixer_forward_media_original_registry_20261006 "
                           "set registry_evidence_ref='changed' where tenant_id='synthetic_tenant';")
                sql_denied(f"set role {OWNER}; insert into public.fixer_forward_media_claim_receipt_20261006 "
                           "default values;")
                sql_denied("set role service_role; select count(*) from "
                           "public.fixer_forward_media_original_registry_20261006;")
                sql_denied("set role fixer_forward_media_attester_20261006; insert into "
                           "public.fixer_forward_media_original_registry_20261006 default values;")
                with psycopg.connect(f"host={sock} port={port} user=service_role dbname=postgres",
                                     autocommit=False) as conn:
                    try:
                        ForwardMediaOwnerPersistence(conn, OWNER, Reader()).persist(
                            original, clearance, manifest)
                    except OwnerPersistenceError:
                        pass
                    else:
                        raise AssertionError("service role impersonated owner")
            finally:
                os.environ.clear()
                os.environ.update(old_environment)
            print("PASS: real PG17 owner insert, exact replay, conflict rollback, role isolation")
        finally:
            if started:
                subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                               check=True, capture_output=True, timeout=60)


if __name__ == "__main__":
    main()
