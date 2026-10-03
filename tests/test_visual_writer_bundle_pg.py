"""Optional real-PostgreSQL checks for the draft atomic writer bundle.

Only the named disposable Unix-socket database is accepted. The fixture
recreates public there; it never connects to a network or production host.
"""
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import pytest


DSN = os.environ.get("VISUAL_GROUP_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
pytestmark = pytest.mark.skipif(not DSN, reason="isolated local VISUAL_GROUP_TEST_DSN unset")


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql(statement):
    done = subprocess.run([PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-d", DSN, "-c", statement], text=True,
                          capture_output=True, timeout=30)
    if done.returncode:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


@pytest.fixture(scope="module", autouse=True)
def database():
    if not DSN:
        pytest.skip("local DSN unset")
    assert PSQL
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_visual_ledger_test(?: user=[A-Za-z0-9_-]+)?",
        DSN), "only named disposable Unix-socket DB allowed"
    assert sql("select current_database()") == "echo_visual_ledger_test"
    sql("do $$ begin if not exists(select from pg_roles where rolname='anon') then create role anon; end if; "
        "if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if; "
        "if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; end $$;")
    sql("drop schema public cascade; create schema public; grant usage on schema public to anon,authenticated,service_role;")
    sql("""create table public.content_calendar(
        id uuid primary key default gen_random_uuid(), gym_id text, post_date date,
        status text default 'pending', account text, format text,
        variant_status text default 'active', image_url text, source_media_url text,
        source_media_asset_id text, drive_file_id text, byte_hash text, r2_key text,
        media_not_ready_reason text, published_at timestamptz, late_post_id text,
        publish_reservation_day date, publish_claim_token uuid,
        created_at timestamptz default now());
        create table public.media_asset(id text primary key,gym_id text,content_hash text);
        grant select,insert,update,delete on public.content_calendar to service_role;""")
    for name in ("DRAFT_visual_group_schema_20261002.sql",
                 "DRAFT_visual_group_claim_trigger_20261002.sql",
                 "DRAFT_visual_group_backfill_20261002.sql",
                 "DRAFT_visual_global_history_20261002.sql"):
        sql((MIGRATIONS / name).read_text())


def tenant():
    key, tid = "g_" + uuid.uuid4().hex, str(uuid.uuid4())
    sql(f"select public.visual_group_tenant_register({q(key)},{q(tid)}::uuid)")
    return tid


def bundle(tid, digest, url):
    aliases = [{"alias_kind": "byte_hash", "alias_value": "derived:md5:" + digest},
               {"alias_kind": "canonical_url", "alias_value": url}]
    fingerprint = "md5:" + digest
    evidence = {"source": "test_delivered_bytes", "verified_bytes": fingerprint,
                "delivered_url": url}
    return ("select public.visual_global_prepare_bundle("
            f"{q(tid)},{q(json.dumps(aliases))}::jsonb,{q(fingerprint)},"
            f"{q(json.dumps(evidence))}::jsonb,'test_actor',null)")


def test_concurrent_identical_bundle_reuses_one_group_and_identity():
    tid = tenant()
    digest = hashlib.md5(b"same delivered bytes").hexdigest()
    statement = bundle(tid, digest, "https://test/one.jpg")
    barrier = threading.Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return json.loads(sql(statement))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    assert sql(f"select count(*) from public.visual_group_alias where gym_id={q(tid)}") == "2"
    assert sql(f"select count(*) from public.visual_global_identity where tenant_id={q(tid)}") == "1"
    assert sql(f"select count(*) from public.visual_global_usage where fingerprint={q('md5:' + digest)}") == "0"


def test_conflicting_aliases_roll_back_and_second_url_cannot_poison_group():
    tid = tenant()
    digest = hashlib.md5(b"selected bytes").hexdigest()
    first = json.loads(sql(bundle(tid, digest, "https://test/first.jpg")))
    other = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url','https://test/other.jpg')")
    assert other != first["group_key"]
    with pytest.raises(RuntimeError, match="conflicting groups"):
        sql(bundle(tid, digest, "https://test/other.jpg"))
    assert sql(f"select count(*) from public.visual_group_alias where gym_id={q(tid)}") == "3"
    with pytest.raises(RuntimeError, match="another delivered URL"):
        sql(bundle(tid, digest, "https://test/second.jpg"))
    assert sql(f"select count(*) from public.visual_group_alias where gym_id={q(tid)}") == "3"
    assert json.loads(sql(bundle(tid, digest, "https://test/first.jpg"))) == first
    assert sql(f"select count(*) from public.visual_global_usage where fingerprint={q('md5:' + digest)}") == "0"
    claimed = sql("select public.visual_global_claim("
                  f"{q(tid)},{q(first['group_key'])},'2026-10-05',"
                  f"{q(uuid.uuid4())}::uuid,'instagram',false,false,{q('md5:' + digest)})")
    assert claimed == "md5:" + digest
