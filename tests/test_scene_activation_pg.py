"""Focused DRAFT scene activation checks on a disposable local PostgreSQL 17.

Reuses the scene-history fixture's private Unix-socket cluster and exact schema
stack. No supplied DSN, remote host, production SQL, or persistent database.
"""
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
SOURCE = MIGRATIONS / "DRAFT_visual_scene_activation_20261005.sql"
APPROVAL = Path(os.environ.get(
    "ECHO_APPROVAL_MIGRATION_PATH",
    MIGRATIONS / "calendar_approval_provenance_20261005.sql"))
SPEC = importlib.util.spec_from_file_location(
    "scene_history_fixture", ROOT / "tests/test_scene_history_backfill_pg.py")
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)

pytestmark = pytest.mark.skipif(
    not history.PG17_AVAILABLE,
    reason="local PG17 unavailable; disposable activation checks cannot run",
)


@pytest.fixture(scope="module", autouse=True)
def cluster():
    if not APPROVAL.is_file():
        pytest.skip("combined PG17 checks require calendar_approval_provenance_20261005.sql")
    start = history.cluster.__wrapped__()
    next(start)
    try:
        history._sql("alter table public.content_calendar "
                     "add column format text, add column caption text")
        history._sql("create table public.gyms (id uuid primary key, slug text, name text); "
                     "create table public.echo_intake_tokens (gym_id uuid, echo_account_key text); "
                     "create table public.echo_gym_settings (gym_id uuid primary key, "
                     "autonomous boolean, autonomy_updated_by text)")
        history._sql(APPROVAL.read_text())
        premature = history._run(SOURCE.read_text(), check=False)
        assert premature.returncode != 0
        assert "scene calendar claim, publish and approval wiring must precede activation" in premature.stderr
        history._sql((MIGRATIONS / "DRAFT_visual_scene_calendar_transaction_20261005.sql").read_text())
        history._sql(SOURCE.read_text())
        yield
    finally:
        try:
            next(start)
        except StopIteration:
            pass


@pytest.fixture(autouse=True)
def reset(cluster):
    history.reset.__wrapped__(None)
    history._sql("set session_replication_role=replica; "
                 "truncate public.visual_group_activation")


def test_clean_empty_history_arms_one_tenant_with_scene_receipt():
    tenant, _ = history._seed_tenant()
    proof = history._one(
        f"select public.visual_group_activate_guard('{tenant}','scene-test')::text")
    assert '"scene_history_covered": true' in proof
    assert '"scene_rows_backfilled_this_transaction": 0' in proof
    assert history._one(
        "select enforce::text from public.gym_visual_guard_settings "
        f"where gym_id='{tenant}'") == "true"
    assert history._one(
        "select (proof->>'scene_history_covered')::text from "
        f"public.visual_group_activation where gym_id='{tenant}'") == "true"
    assert history._one("select count(*) from public.visual_scene_phash_occupied") == "0"


def test_blocked_historical_row_refuses_and_rolls_back():
    tenant, group = history._seed_tenant()
    url, _ = history._seed_object(tenant, group, phash=None)
    history._insert_published(tenant, url)
    bad = history._run(
        f"select public.visual_group_activate_guard('{tenant}','scene-test')",
        check=False)
    assert bad.returncode != 0
    assert "historical scene coverage has blocked rows" in bad.stderr
    assert history._one(
        f"select count(*) from public.gym_visual_guard_settings "
        f"where gym_id='{tenant}' and enforce") == "0"
    assert history._one(
        f"select count(*) from public.visual_group_activation "
        f"where gym_id='{tenant}'") == "0"
    assert history._one("select count(*) from public.visual_scene_phash_occupied") == "0"


def test_role_surface_is_service_only_and_backfill_owner_only():
    assert history._one("select has_function_privilege('service_role',"
                        "'public.visual_group_activate_guard(text,text)','execute')") == "t"
    for role in ("anon", "authenticated"):
        assert history._one(f"select has_function_privilege('{role}',"
                            "'public.visual_group_activate_guard(text,text)','execute')") == "f"
    assert history._one("select has_function_privilege('service_role',"
                        "'public.visual_scene_backfill_occupied()','execute')") == "f"


def test_published_history_backfills_before_arm():
    tenant, group = history._seed_tenant()
    url, _ = history._seed_object(tenant, group, history._PHASH_A)
    history._insert_published(tenant, url)
    proof = history._one(
        f"select public.visual_group_activate_guard('{tenant}','scene-test')::text")
    assert '"scene_rows_backfilled_this_transaction": 1' in proof
    assert [item["state"] for item in history._coverage()] == ["covered"]
    assert len(history._occupied()) == 1
    assert history._one(
        "select enforce::text from public.gym_visual_guard_settings "
        f"where gym_id='{tenant}'") == "true"


def test_near_scene_history_refuses_entire_activation():
    first, first_group = history._seed_tenant()
    first_url, _ = history._seed_object(first, first_group, "0000000000000000")
    history._insert_published(first, first_url)
    second, second_group = history._seed_tenant()
    second_url, _ = history._seed_object(second, second_group, "0000000000000001")
    history._insert_published(second, second_url)
    bad = history._run(
        f"select public.visual_group_activate_guard('{first}','scene-test')",
        check=False)
    assert bad.returncode != 0
    assert "unresolved near-frame or uncertain similarity conflicts" in bad.stderr
    assert history._one("select count(*) from public.visual_scene_phash_occupied") == "0"
    assert history._one("select count(*) from public.visual_group_activation") == "0"
    assert history._one("select count(*) from public.gym_visual_guard_settings where enforce") == "0"


def test_calendar_writer_barrier_has_bounded_lock_wait():
    import subprocess
    import time

    tenant, _ = history._seed_tenant()
    app_name = "scene_activation_barrier_test"
    holder = subprocess.Popen(
        [str(history.BIN / "psql"), "-X", "-q", "-A", "-t",
         "-p", history.PORT, "-h", history._SOCK,
         "-d", "echo_scene_history_test", "-c",
         "begin; set application_name='scene_activation_barrier_test'; "
         "lock table public.content_calendar in access exclusive mode; "
         "select pg_sleep(7); commit"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = history._one(
                "select coalesce((select wait_event from pg_stat_activity "
                f"where application_name='{app_name}'),'')")
            if state == "PgSleep":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("scratch calendar lock holder did not start")
        started = time.monotonic()
        bad = history._run(
            f"select public.visual_group_activate_guard('{tenant}','scene-test')",
            check=False)
        elapsed = time.monotonic() - started
        assert bad.returncode != 0
        assert "lock timeout" in bad.stderr
        assert elapsed < 6.5
        assert history._one("select count(*) from public.visual_group_activation") == "0"
    finally:
        holder.communicate(timeout=15)
