"""Disposable PG17 checks for the UNAPPLIED generated authority draft.

No existing DB, production DSN, network listener, install or runtime activation.
Run as a standalone script with PostgreSQL 17 tools and existing psycopg.
Evidence is local SQL-contract behavior, not live provider/palette parity.
"""
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/DRAFT_generated_source_palette_authority_20261007.sql"
PG = Path("/opt/homebrew/opt/postgresql@17/bin")


def source_op(source_id, text="exact source text"):
    return {"op": "source_upsert", "source_id": source_id, "category": "drive_folder",
            "exact_text": text, "citation": "citation:doc-1"}


def approval_op(source_id, revision=1, mode="explicit", evidence="approval:receipt-1"):
    return {"op": "source_approve", "source_id": source_id, "revision": revision,
            "approval_mode": mode, "approval_evidence": evidence}


def palette_op(key="brand", colors=("#112233", "#aabbcc"), evidence="sha256:file-revision-1"):
    return {"op": "palette_upsert", "palette_key": key, "colors": list(colors),
            "verification_evidence": evidence}


def main():
    import psycopg
    for tool in ("initdb", "pg_ctl"):
        if not (PG / tool).exists() and not shutil.which(tool):
            raise SystemExit(f"BLOCKED: {tool} unavailable; no install attempted")
    initdb = str(PG / "initdb") if (PG / "initdb").exists() else "initdb"
    pg_ctl = str(PG / "pg_ctl") if (PG / "pg_ctl").exists() else "pg_ctl"
    with tempfile.TemporaryDirectory(prefix="gen_authority_pg_", dir="/tmp") as tmp:
        root = Path(tmp)
        sock = root / "sock"
        sock.mkdir()
        data = root / "data"
        port = random.randint(41000, 59000)
        subprocess.run([initdb, "-D", str(data), "-U", "postgres", "--no-sync"],
                       check=True, capture_output=True, timeout=60)
        subprocess.run([pg_ctl, "-D", str(data), "-l", str(root / "pg.log"),
                        "-o", f"-k {sock} -p {port} -c listen_addresses=''",
                        "-w", "start"], check=True, capture_output=True, timeout=60)
        connections = []
        try:
            def dsn(role="postgres"):
                return f"host={sock} port={port} dbname=postgres user={role}"

            admin = psycopg.connect(dsn(), autocommit=True)
            connections.append(admin)

            def sql(q, args=None):
                cur = admin.execute(q, args)
                return cur.fetchall() if cur.description else None

            def denied(fn, reason=None, code=None):
                try:
                    fn()
                except psycopg.Error as e:
                    if reason:
                        assert reason in str(e), str(e)
                    if code:
                        assert e.sqlstate == code, (e.sqlstate, str(e))
                    return e
                raise AssertionError(f"unsafe operation accepted: {reason or code}")

            # Realistic default privileges are deliberately installed BEFORE
            # migration. service_role BYPASSRLS tests table ACLs, not only RLS.
            sql("create role anon login; create role authenticated login; "
                "create role service_role login bypassrls;")
            sql("alter default privileges in schema public grant all on tables "
                "to public, anon, authenticated, service_role;")
            sql("alter default privileges in schema public grant all on sequences "
                "to public, anon, authenticated, service_role;")
            sql("alter default privileges in schema public grant all on functions "
                "to public, anon, authenticated, service_role;")
            sql(MIGRATION.read_text())
            sql("create role owner_user login; grant generated_authority_owner_20261007 to owner_user;")
            sql("create role publisher_user login; grant generated_authority_publisher_20261007 to publisher_user;")

            def lane(role):
                conn = psycopg.connect(dsn(role), autocommit=True)
                conn.execute("set statement_timeout='8s'")
                connections.append(conn)
                return conn

            service = lane("service_role")
            owner = lane("owner_user")
            publisher = lane("publisher_user")

            def write(conn, tenant, epoch, ops):
                return conn.execute(
                    "select public.generated_authority_write_20261007(%s,%s,%s::jsonb)",
                    (tenant, epoch, json.dumps(ops))).fetchone()[0]

            def snapshot(conn, tenant):
                return conn.execute("select public.generated_authority_snapshot_20261007(%s)",
                                    (tenant,)).fetchone()[0]

            def validate(conn, tenant, epoch, ids=None, revisions=None, key="brand", palette_revision=1):
                return conn.execute(
                    "select public.generated_authority_validate_publish_20261007(%s,%s,%s,%s,%s,%s)",
                    (tenant, epoch, ids, revisions, key, palette_revision)).fetchone()[0]

            def seed(tenant):
                return write(service, tenant, 0, [source_op("src-1"), approval_op("src-1"), palette_op()])

            # Pending intake, revision-bound approval, then updated content
            # invalidating that approval. No intake silently grants approval.
            assert write(service, "gym-a", 0, [source_op("src-1"), palette_op()]) == 1
            snap = snapshot(owner, "gym-a")
            row = snap["sources"][0]
            assert row["status"] == "pending" and row["approval_revision"] is None
            assert row["exact_text"] == "exact source text" and row["citation"] == "citation:doc-1"
            denied(lambda: validate(publisher, "gym-a", 1, ["src-1"], [1]), "pending")
            for bad in [approval_op("src-1", revision=2), approval_op("src-1", revision=None),
                        approval_op("src-1", mode=""), approval_op("src-1", evidence=" "),
                        approval_op("src-1", mode="auto", evidence=None)]:
                denied(lambda bad=bad: write(service, "gym-a", 1, [bad]))
            assert write(service, "gym-a", 1, [dict(approval_op("src-1"), actor="forged-human")]) == 2
            row = snapshot(owner, "gym-a")["sources"][0]
            assert row["approval_revision"] == row["revision"] == 1
            assert row["approval_mode"] == "explicit" and row["approved_by"] == "service_role"
            assert validate(publisher, "gym-a", 2, ["src-1"], [1])["validated"] is True
            assert write(service, "gym-a", 2, [source_op("src-1", text="v2 text")]) == 3
            row = snapshot(owner, "gym-a")["sources"][0]
            assert row["revision"] == 2 and row["status"] == "pending"
            assert all(row[k] is None for k in ("approval_revision", "approval_mode", "approval_evidence", "approved_by"))
            denied(lambda: write(service, "gym-a", 3, [approval_op("src-1", revision=1)]), "stale source approval")
            assert write(service, "gym-a", 3, [approval_op("src-1", revision=2, mode="auto",
                         evidence="armed-intake-flag:trusted-receipt")]) == 4
            assert snapshot(owner, "gym-a")["sources"][0]["approval_mode"] == "auto"
            denied(lambda: validate(publisher, "gym-a", 4, ["src-1"], [1]), "stale approval/revision")
            assert validate(publisher, "gym-a", 4, ["src-1"], [2])["validated"]

            # Every source reference and the exact verified active palette is
            # mandatory. A NULL element must not disappear into SELECT INTO.
            for ids, revs in [(None, None), ([], []), ([None], [2]), ([""], [2]),
                              ([" \t"], [2]), (["src-1", None], [2, 2]),
                              (["src-1"], None), (["src-1"], []), (["src-1"], [None]),
                              (["src-1"], [0]), (["unknown"], [1])]:
                denied(lambda ids=ids, revs=revs: validate(publisher, "gym-a", 4, ids, revs))
            for key, rev in [(None, 1), ("", 1), (" ", 1), ("unknown", 1), ("brand", None),
                             ("brand", 0), ("brand", 2)]:
                denied(lambda key=key, rev=rev: validate(publisher, "gym-a", 4, ["src-1"], [2], key, rev))
            for bad_id in (None, "", " \t", 42):
                for op in (source_op(bad_id), approval_op(bad_id),
                           {"op": "source_tombstone", "source_id": bad_id}):
                    denied(lambda op=op: write(service, "gym-a", 4, [op]), "source_id")
            for colors in ([None], ["#112233", None], [123], [True], [{}], [[]], ["not-a-color"], []):
                denied(lambda colors=colors: write(service, "gym-a", 4, [palette_op(colors=colors)]), "colors")
            denied(lambda: write(service, "gym-a", 4, [palette_op(evidence=" ")]), "verification_evidence")
            denied(lambda: write(service, "gym-a", 4, [{"op": "nope"}]), "unknown op")
            denied(lambda: write(service, "gym-a", 4, []), "non-empty")

            # Rollback a partial batch, no CAS replay, and tenant partitioning.
            denied(lambda: write(service, "gym-a", 4, [source_op("rolled-back"), {"op": "nope"}]), "unknown op")
            assert snapshot(owner, "gym-a")["epoch"] == 4
            assert len(snapshot(owner, "gym-a")["sources"]) == 1
            denied(lambda: write(service, "gym-a", 3, [source_op("stale")]), "stale epoch")
            assert seed("gym-b") == 1
            assert snapshot(owner, "gym-b")["sources"][0]["exact_text"] == "exact source text"
            assert snapshot(owner, "gym-c")["epoch"] is None
            assert snapshot(owner, "gym-a")["sources"][0]["exact_text"] == "v2 text"
            assert write(service, "gym-b", 1, [source_op("only-b"), approval_op("only-b")]) == 2
            denied(lambda: validate(publisher, "gym-a", 4, ["only-b"], [1]), "unknown")

            # Palette revision changes invalidate prior palette pins; revoked
            # palettes and sources are permanently tombstoned.
            assert write(service, "gym-a", 4, [palette_op(colors=("#000000",),
                         evidence="sha256:file-revision-2")]) == 5
            denied(lambda: validate(publisher, "gym-a", 5, ["src-1"], [2]), "palette")
            assert validate(publisher, "gym-a", 5, ["src-1"], [2], palette_revision=2)["validated"]
            assert write(service, "gym-a", 5, [{"op": "palette_revoke", "palette_key": "brand"}]) == 6
            denied(lambda: validate(publisher, "gym-a", 6, ["src-1"], [2], palette_revision=3), "palette")
            denied(lambda: write(service, "gym-a", 6, [palette_op()]), "cannot be resurrected")
            denied(lambda: write(service, "gym-a", 6, [{"op": "palette_revoke", "palette_key": "brand"}]), "already revoked")
            assert write(service, "gym-a", 6, [{"op": "source_tombstone", "source_id": "src-1"}]) == 7
            row = snapshot(owner, "gym-a")["sources"][0]
            assert row["status"] == "revoked" and row["tombstoned_at"] is not None
            denied(lambda: write(service, "gym-a", 7, [source_op("src-1")]), "cannot be resurrected")
            denied(lambda: write(service, "gym-a", 7, [approval_op("src-1", revision=3)]), "revoked")
            denied(lambda: write(service, "gym-a", 7, [{"op": "source_tombstone", "source_id": "src-1"}]), "already revoked")

            # Function-only least privilege under default ACLs and BYPASSRLS.
            tables = ["generated_tenant_epoch_20261007", "generated_source_authority_20261007",
                      "generated_palette_authority_20261007", "generated_authority_audit_20261007"]
            for role in ("service_role", "owner_user", "publisher_user", "anon", "authenticated"):
                conn = lane(role)
                for table in tables:
                    for verb in (f"select * from public.{table}", f"delete from public.{table}",
                                 f"truncate public.{table}"):
                        denied(lambda verb=verb: conn.execute(verb), "permission denied")
                    assert sql("select has_table_privilege(%s,%s,'INSERT'), "
                               "has_table_privilege(%s,%s,'UPDATE'), has_table_privilege(%s,%s,'TRIGGER')",
                               (role, f"public.{table}", role, f"public.{table}", role, f"public.{table}")) == [(False, False, False)]
                denied(lambda: conn.execute("insert into public.generated_tenant_epoch_20261007(tenant_id) values ('acl-abuse')"), "permission denied")
                denied(lambda: conn.execute("update public.generated_tenant_epoch_20261007 set epoch=999"), "permission denied")
                denied(lambda: conn.execute("insert into public.generated_source_authority_20261007 "
                    "(tenant_id,source_id,category,exact_text,citation,status,revision,epoch) "
                    "values ('gym-a','acl-abuse','x','x','x','pending',1,7)"), "permission denied")
                denied(lambda: conn.execute("select nextval('public.generated_authority_audit_20261007_audit_id_seq')"), "permission denied")
            denied(lambda: write(owner, "gym-a", 7, [source_op("x")]), "permission denied")
            denied(lambda: write(publisher, "gym-a", 7, [source_op("x")]), "permission denied")
            denied(lambda: validate(owner, "gym-a", 7, ["src-1"], [3]), "permission denied")
            for role in ("anon", "authenticated"):
                denied(lambda role=role: snapshot(lane(role), "gym-a"), "permission denied")
                denied(lambda role=role: write(lane(role), "gym-a", 7, [source_op("x")]), "permission denied")
            denied(lambda: sql("update public.generated_authority_audit_20261007 set actor='forged'"), "append-only")
            denied(lambda: sql("truncate public.generated_authority_audit_20261007"), "append-only")
            denied(lambda: sql("delete from public.generated_tenant_epoch_20261007"), "append-only")

            # Audit actor reflects login despite SECURITY DEFINER, never the
            # migration owner or caller-supplied payload actor field.
            assert sql("select distinct actor from public.generated_authority_audit_20261007") == [("service_role",)]
            assert sql("select count(*) from public.generated_authority_audit_20261007 where tenant_id='gym-a'") == [(8,)]
            audit = sql("select epoch, revision, op from public.generated_authority_audit_20261007 "
                        "where tenant_id='gym-a' order by audit_id")
            assert [r[0] for r in audit] == [1, 1, 2, 3, 4, 5, 6, 7]

            # Deterministic overlap: use pg_stat_activity to establish the
            # contender is waiting on the held advisory lock before committing.
            def launch(fn):
                result = {}
                def run():
                    try:
                        result["value"] = fn()
                    except Exception as exc:
                        result["error"] = exc
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                return thread, result

            def waiting(conn):
                deadline = time.monotonic()+4
                while time.monotonic()<deadline:
                    if sql("select wait_event from pg_stat_activity where pid=%s", (conn.info.backend_pid,)) == [("advisory",)]:
                        return
                    time.sleep(0.01)
                raise AssertionError("contender did not overlap the held advisory lock")

            for kind in ("source", "palette"):
                tenant = f"race-{kind}"
                assert seed(tenant) == 1
                stale = lane("publisher_user")
                stale.execute("begin isolation level repeatable read")
                assert snapshot(stale, tenant)["epoch"] == 1  # establish old snapshot
                revoker = lane("service_role")
                revoker.execute("begin")
                revoke = ({"op": "source_tombstone", "source_id": "src-1"} if kind == "source"
                          else {"op": "palette_revoke", "palette_key": "brand"})
                assert write(revoker, tenant, 1, [revoke]) == 2  # uncommitted, holds lock
                thread, result = launch(lambda: validate(stale, tenant, 1, ["src-1"], [1]))
                waiting(stale)
                revoker.execute("commit")
                thread.join(5)
                assert not thread.is_alive()
                assert "value" not in result and isinstance(result.get("error"), psycopg.Error), result
                assert result["error"].sqlstate == "40001", result
                stale.execute("rollback")
                denied(lambda: validate(stale, tenant, 1, ["src-1"], [1]), "stale epoch")
                denied(lambda: validate(stale, tenant, 2, ["src-1"], [1]))

            # Reverse ordering: successful validation holds revocation until
            # that decision transaction ends. No claim of external publishing.
            assert seed("race-validated-first") == 1
            decision = lane("publisher_user")
            decision.execute("begin")
            assert validate(decision, "race-validated-first", 1, ["src-1"], [1])["validated"]
            revoker = lane("service_role")
            thread, result = launch(lambda: write(revoker, "race-validated-first", 1,
                                                  [{"op": "source_tombstone", "source_id": "src-1"}]))
            waiting(revoker)
            assert not result
            decision.execute("commit")
            thread.join(5)
            assert result == {"value": 2}, result

            # Two stale CAS writers overlap under RR: loser cannot overwrite
            # committed authority or leave an audit receipt.
            assert seed("race-cas") == 1
            loser = lane("service_role")
            loser.execute("begin isolation level repeatable read")
            assert snapshot(loser, "race-cas")["epoch"] == 1
            winner = lane("service_role")
            winner.execute("begin")
            assert write(winner, "race-cas", 1, [source_op("winner")]) == 2
            thread, result = launch(lambda: write(loser, "race-cas", 1, [source_op("loser")]))
            waiting(loser)
            winner.execute("commit")
            thread.join(5)
            assert not thread.is_alive() and result["error"].sqlstate == "40001", result
            loser.execute("rollback")
            assert [s["source_id"] for s in snapshot(owner, "race-cas")["sources"]] == ["src-1", "winner"]
            assert sql("select count(*) from public.generated_authority_audit_20261007 "
                       "where tenant_id='race-cas' and entity_key='loser'") == [(0,)]
            print("PASS: disposable PG17 authority approvals, palette pins/revocation, ACLs, audit and 4 overlapping races")
        finally:
            for conn in reversed(connections):
                conn.close()
            subprocess.run([pg_ctl, "-D", str(data), "-m", "fast", "stop"],
                           capture_output=True, timeout=60)


if __name__ == "__main__":
    sys.exit(main())
