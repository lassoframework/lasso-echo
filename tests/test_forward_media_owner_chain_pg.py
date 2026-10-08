"""Disposable PostgreSQL 17 end-to-end owner->bind->attest->claim chain check.

Run standalone with PG17 tools and psycopg in the selected interpreter:
    /tmp/echo-forward-media-pg-20261007/bin/python tests/test_forward_media_owner_chain_pg.py

Uses only a disposable local cluster under /tmp, stdlib, psycopg and the real
draft/agent code. No production DSN, no network, no provider send. The guard
is enabled only inside this disposable process while its object reader points
at synthetic bytes, then restored. Applies ONLY
migrations/DRAFT_fixer_forward_media_claim_20261006.sql on
top of a minimal synthetic content_calendar prerequisite.
"""
import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import uuid

PG17_BIN = Path("/opt/homebrew/opt/postgresql@17/bin")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.forward_media_owner import ObjectReader

TENANT = "synthetic_tenant_chain"
GROUP = "synthetic_group_chain"
ASSET = "synthetic_asset_chain"
BASE = "https://media.example.test"
SOURCE_URL = BASE + "/echo/synthetic/original.jpg"
SOURCE_BYTES = b"synthetic immutable original bytes 20261007"
OWNER = "forward_media_chain_owner"
POST_DATE = "2026-10-10"
CROSS_DATE = "2026-10-11"


class FakeReader(ObjectReader):
    """Fake owner/attester object reader: immutable synthetic bytes per URL."""

    def __init__(self):
        self._objects = {SOURCE_URL: bytes(SOURCE_BYTES)}

    def read(self, url):
        if url not in self._objects:
            raise KeyError("unknown synthetic object")
        return self._objects[url]


def main():
    import psycopg

    from agent import forward_media_guard, media_host, visual_writer_prepare
    from agent.forward_media_owner import ForwardMediaOwnerPersistence
    from agent import forward_media_owner_packet

    tools = {}
    for tool in ("initdb", "pg_ctl", "psql"):
        candidate = PG17_BIN / tool
        resolved = str(candidate) if candidate.exists() else shutil.which(tool)
        if not resolved:
            raise SystemExit(f"BLOCKED: existing {tool} unavailable; no install attempted")
        tools[tool] = resolved
    free = shutil.disk_usage("/tmp").free
    if free < 5 * 1024**3:
        raise SystemExit(f"BLOCKED: disk guard, only {free} bytes free on /tmp")

    # Synthetic host allow-list for the attester's own-media URL check only.
    media_host.config.S3_PUBLIC_BASE_URL = BASE
    reader = FakeReader()
    assert not forward_media_guard.enabled(), "guard must stay OFF for runtime"

    with tempfile.TemporaryDirectory(prefix="forward_chain_pg_", dir="/tmp") as tmp:
        root = Path(tmp)
        sock = root / "sock"
        sock.mkdir()
        data = root / "data"
        port = random.randint(41000, 59000)
        subprocess.run([tools["initdb"], "-D", str(data), "-U", "postgres", "--no-sync"],
                       check=True, capture_output=True, timeout=60)
        started = False
        try:
            subprocess.run([tools["pg_ctl"], "-D", str(data), "-l", str(root / "pg.log"),
                            "-o", f"-k {sock} -p {port} -c listen_addresses=''",
                            "-w", "start"], check=True, capture_output=True, timeout=60)
            started = True
            psql = [tools["psql"], "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-h", str(sock),
                    "-p", str(port), "-U", "postgres", "-d", "postgres"]

            def sql(query, ok=True):
                result = subprocess.run(psql, input=query, text=True,
                                        capture_output=True, timeout=30)
                if ok and result.returncode:
                    raise AssertionError(result.stderr)
                if not ok:
                    assert result.returncode != 0, "unexpected SQL success"
                    return result.stderr
                return result.stdout.strip()

            def counts():
                out = sql("select (select count(*) from fixer_forward_media_original_registry_20261006)||'|'"
                          "||(select count(*) from fixer_forward_media_history_clearance_20261006)||'|'"
                          "||(select count(*) from fixer_forward_media_render_manifest_20261006)||'|'"
                          "||(select count(*) from fixer_forward_media_claim_receipt_20261006)||'|'"
                          "||(select count(*) from fixer_forward_media_use_20261006)||'|'"
                          "||(select count(*) from fixer_forward_media_lineage_20261006)||'|'"
                          "||(select count(*) from fixer_forward_media_object_read_20261006)||'|'"
                          "||(select count(*) from content_calendar);")
                return tuple(map(int, out.split("|")))

            # Prerequisite roles/table only; then ONLY the draft under test.
            sql("create role anon; create role authenticated; create role service_role;"
                "create table public.content_calendar("
                "id uuid primary key,gym_id text,post_date date,account text,format text,"
                "gbp_location_id text,status text,variant_status text,published_at timestamptz,"
                "publish_claim_token uuid,publish_reservation_day date,late_post_id text,"
                "image_url text,thumbnail_url text,media_not_ready_reason text);")
            sql((ROOT / "migrations/DRAFT_fixer_forward_media_claim_20261006.sql").read_text())
            sql("alter role fixer_forward_media_attester_20261006 login;")
            sql("grant select,insert,update,delete on public.content_calendar to service_role;")
            sql(f"create role {OWNER} login; grant fixer_forward_media_owner_20261006 to {OWNER};")
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{TENANT}',true);")

            def dsn(user):
                return f"host={sock} port={port} user={user} dbname=postgres"

            def lane(role):
                conn = psycopg.connect(dsn("postgres"))
                with conn.cursor() as cur:
                    cur.execute(f"set role {role}")
                return conn

            def insert_row(post_date):
                rid = str(uuid.uuid4())
                sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                    "image_url,source_media_url,visual_group_key,source_media_asset_id) values"
                    f"('{rid}','{TENANT}','{post_date}','approved','active',"
                    f"'{SOURCE_URL}','{SOURCE_URL}','{GROUP}','{ASSET}');")
                return rid

            # --- Owner packet: real one-packet JSON, --apply, injected lanes ---
            before = counts()
            packet_path = root / "packet.json"
            packet_path.write_text(json.dumps({
                "schema_version": 1,
                "tenant_id": TENANT,
                "source_asset_id": ASSET,
                "source_url": SOURCE_URL,
                "registry_evidence_ref": "synthetic-registry-receipt",
                "render_evidence_ref": "synthetic-render-receipt",
                "decision": "cleared_unused",
                "history_evidence_ref": "synthetic-independent-history-audit",
                "production_evidence_ref": "synthetic-fresh-production-receipt",
                "operation": "same_object",
            }), encoding="utf-8")
            owner_conn = psycopg.connect(dsn(OWNER))
            from agent.forward_media_lane import RUNTIME_NAMES
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update({k:v for k,v in saved.items() if k in RUNTIME_NAMES})
            os.environ.update(FORWARD_MEDIA_OWNER_DSN=dsn(OWNER),FORWARD_MEDIA_OWNER_ROLE=OWNER)
            out = io.StringIO()
            try:
                code, result = forward_media_owner_packet.run(
                    ["--packet", str(packet_path), "--apply"],
                    reader_factory=lambda: reader,
                    persistence_factory=lambda: ForwardMediaOwnerPersistence(
                        owner_conn, OWNER, reader),
                    out=out)
            finally:
                os.environ.clear()
                os.environ.update(saved)
            assert code == 0 and result["ok"] and result["applied"] and not result["replayed"], out.getvalue()
            after = counts()
            # Owner preparation wrote ONLY the three owner authority rows.
            assert after == (1, 1, 1, 0, 0, 0, 0, 0), (before, after)

            # Caller-provided digests/hashes are refused before any trust.
            try:
                forward_media_owner_packet.validate_packet({
                    "schema_version": 1, "tenant_id": TENANT, "source_asset_id": ASSET,
                    "source_url": SOURCE_URL, "registry_evidence_ref": "r",
                    "render_evidence_ref": "r", "decision": "cleared_unused",
                    "history_evidence_ref": "h", "production_evidence_ref": "p",
                    "operation": "same_object", "source_fingerprint": "md5:" + "0" * 32})
            except forward_media_owner_packet.PacketError as exc:
                assert exc.reason == "field_forbidden", exc.reason
            else:
                raise AssertionError("caller-provided fingerprint accepted")

            digest = sql("select manifest_digest from fixer_forward_media_render_manifest_20261006;")

            # --- Bind: service_role, row ID only, persisted digest read back ---
            def prepare_row(post_date):
                rid = insert_row(post_date)
                with lane("service_role") as conn:
                    with conn.cursor() as cur:
                        cur.execute("select public.fixer_bind_forward_media_manifest_20261006(%s)", (rid,))
                        assert cur.fetchone()[0] is True
                    conn.commit()
                assert sql(f"select render_manifest_digest from content_calendar where id='{rid}';") == digest
                return rid

            def publish_and_claim(post_date, claim=True):
                rid = prepare_row(post_date)
                token = str(uuid.uuid4())
                # Owned publish claim token simulation (digest column untouched).
                sql("set role service_role; update content_calendar set status='publishing',"
                    f"publish_claim_token='{token}',publish_reservation_day='{post_date}' where id='{rid}';")
                revision = sql("set role service_role; select "
                               f"public.fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
                # Exercise the production attester branch with a real, exact-role
                # connection to this disposable DB. Only the byte reader is
                # swapped for the immutable synthetic object map.
                env_names = ("AGENT_FORWARD_MEDIA_GUARD", "AGENT_FORWARD_MEDIA_ATTESTER_DSN",
                             "AGENT_FORWARD_MEDIA_ATTESTER_ROLE")
                prior_env = dict(os.environ)
                os.environ.clear()
                os.environ.update({k:v for k,v in prior_env.items() if k in RUNTIME_NAMES})
                os.environ['AGENT_S3_PUBLIC_BASE_URL'] = SOURCE_URL.rsplit('/',1)[0]
                prior_reader = forward_media_guard.read_public_object
                try:
                    os.environ["AGENT_FORWARD_MEDIA_GUARD"] = "true"
                    os.environ["AGENT_FORWARD_MEDIA_ATTESTER_DSN"] = dsn(
                        "fixer_forward_media_attester_20261006")
                    os.environ["AGENT_FORWARD_MEDIA_ATTESTER_ROLE"] = (
                        "fixer_forward_media_attester_20261006")
                    forward_media_guard.read_public_object = reader.read
                    try:
                        forward_media_guard.attest(rid, revision, read_bytes=reader.read)
                    except forward_media_guard.ForwardMediaVerificationHold as exc:
                        assert "callback override refused" in str(exc), exc
                    else:
                        raise AssertionError("production attester accepted caller bytes")
                    proof = forward_media_guard.attest(rid, revision)
                finally:
                    forward_media_guard.read_public_object = prior_reader
                    os.environ.clear()
                    os.environ.update(prior_env)
                if not claim:
                    return rid, token, revision, proof["evidence_id"]
                with lane("service_role") as conn:
                    with conn.cursor() as cur:
                        cur.execute("select public.fixer_claim_forward_media_20261006(%s,%s,%s,%s)",
                                    (rid, token, proof["evidence_id"], revision))
                        return rid, token, revision, cur.fetchone()[0]

            row_id, token, revision, claimed = publish_and_claim(POST_DATE)
            assert claimed is True
            # Receipt replay of the identical owned claim remains true.
            with lane("service_role") as conn:
                with conn.cursor() as cur:
                    cur.execute("select public.fixer_claim_forward_media_20261006(%s,%s,%s,%s)",
                                (row_id, token, sql("select evidence_id from fixer_forward_media_claim_receipt_20261006;"), revision))
                    assert cur.fetchone()[0] is True
                conn.commit()

            # Contract: same tenant/group/content date siblings may reuse bytes.
            _, _, _, sibling_claimed = publish_and_claim(POST_DATE)
            assert sibling_claimed is True
            # Contract: another content date may never reuse the same bytes.
            rid, token, rev, evidence_id = publish_and_claim(CROSS_DATE, claim=False)
            with lane("service_role") as conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute("select public.fixer_claim_forward_media_20261006(%s,%s,%s,%s)",
                                    (rid, token, evidence_id, rev))
                        conn.commit()
                    except psycopg.Error as exc:
                        conn.rollback()
                        assert "already consumed by another tenant/date/group" in str(exc), exc
                    else:
                        raise AssertionError("cross-date reuse claim succeeded")

            # --- Digest write authority: direct service INSERT/UPDATE denied ---
            err = sql("set role service_role; insert into content_calendar(id,gym_id,post_date,status,"
                      "variant_status,image_url,source_media_url,visual_group_key,source_media_asset_id,"
                      f"render_manifest_digest) values('{uuid.uuid4()}','{TENANT}','{POST_DATE}','approved',"
                      f"'active','{SOURCE_URL}','{SOURCE_URL}','{GROUP}','{ASSET}','{digest}');", ok=False)
            assert "render manifest digest changes require the validated binder" in err, err
            err = sql("set role service_role; update content_calendar set "
                      f"render_manifest_digest='sha256:{'0' * 64}' where id='{row_id}';", ok=False)
            assert "render manifest digest changes require the validated binder" in err, err

            final = counts()
            assert final[0:3] == (1, 1, 1), final  # exactly one synthetic tenant/asset chain
            assert sql(f"select count(*) from content_calendar where gym_id='{TENANT}';") == "3"
            print("PASS: owner packet -> service bind -> trusted attest -> owned claim chain; "
                  "sibling reuse allowed, cross-date reuse denied, direct digest writes denied")
        finally:
            if started:
                subprocess.run([tools["pg_ctl"], "-D", str(data), "-m", "immediate", "-w", "stop"],
                               check=True, capture_output=True, timeout=60)


if __name__ == "__main__":
    main()
