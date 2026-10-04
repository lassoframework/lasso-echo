"""Real-PostgreSQL checks for persisted-state RPC results.

The tests call the two original public runtime names directly. A scratch-only
BEFORE UPDATE trigger models the authoritative scene trigger's required
behavior: an otherwise eligible publishing/approval attempt is converted into
a durable held row instead of raising. This specifically catches false success
from returning attempted state.

The suite runs only against a local Unix-socket database literally named
``echo_scene_rpc_persisted_test`` and never applies production state or flags.
Its exact migration stack is deliberately minimal: the original production RPC
definitions followed immediately by the standalone persisted-state repair. The
two stopped Kimi RPC drafts are preserved on disk but are not dependencies.
"""

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("VISUAL_SCENE_RPC_PERSISTED_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
STACK = [
    "calendar_claim_media_guard_20261002.sql",
    "DRAFT_visual_scene_rpc_persisted_state_20261004.sql",
]

pytestmark = pytest.mark.skipif(
    not DSN or not PSQL
    or not all((MIGRATIONS / name).exists() for name in STACK),
    reason="persisted-state RPC checks require a disposable local PostgreSQL "
           "database named echo_scene_rpc_persisted_test",
)


def _run(statement, check=True):
    done = subprocess.run(
        [PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DSN,
         "-c", statement], text=True, capture_output=True, timeout=60)
    if check and done.returncode:
        raise RuntimeError(done.stderr)
    return done


def _sql(statement):
    return _run(statement).stdout.strip()


def _one(statement):
    output = _sql(statement)
    return output.splitlines()[-1] if output else ""


@pytest.fixture(scope="module", autouse=True)
def _scratch_stack():
    if not DSN or not all((MIGRATIONS / name).exists() for name in STACK):
        pytest.skip("migration stack or scratch DSN unavailable")
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ "
        r"dbname=echo_scene_rpc_persisted_test(?: user=[A-Za-z0-9_-]+)?",
        DSN,
    ), "only the named disposable Unix-socket database is allowed"
    assert _one("select current_database()") == "echo_scene_rpc_persisted_test"

    _sql("drop schema public cascade; create schema public; "
         "grant usage on schema public to public")
    _sql("do $$ begin "
         "if not exists (select 1 from pg_roles where rolname='anon') then "
         "create role anon nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='authenticated') then "
         "create role authenticated nologin; end if; "
         "if not exists (select 1 from pg_roles where rolname='service_role') then "
         "create role service_role nologin; end if; end $$")
    _sql("create table public.content_calendar ("
         "id uuid primary key default gen_random_uuid(),"
         "gym_id text, status text, published_at timestamptz, late_post_id text,"
         "variant_status text, image_url text, media_not_ready_reason text,"
         "format text, account text, publish_reservation_day date,"
         "publish_claim_token uuid, post_date date, visual_group_key text)")

    script = "\n".join((MIGRATIONS / name).read_text() for name in STACK)
    done = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=script, text=True, capture_output=True, timeout=180)
    assert done.returncode == 0, done.stderr
    # CREATE OR REPLACE plus the repeated ACL statements must be replay-safe.
    repair = (MIGRATIONS / STACK[-1]).read_text()
    replay = subprocess.run(
        [PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", DSN],
        input=repair, text=True, capture_output=True, timeout=180)
    assert replay.returncode == 0, replay.stderr

    # Scratch-only deterministic stand-in for the merged scene BEFORE trigger.
    # Marking a row for an attempted target state makes that transition persist
    # as the exact frozen scene-held shape, including cleared claim state.
    _sql("create table public.scene_rpc_forced_hold ("
         "row_id uuid primary key, attempted_status text not null,"
         "persisted_status text)")
    _sql("create table public.scene_rpc_hold_evidence ("
         "row_id uuid primary key, attempted_status text not null,"
         "held_at timestamptz not null default now())")
    _sql("create function public.scene_rpc_force_hold() returns trigger "
         "language plpgsql as $$ begin "
         "if exists(select 1 from public.scene_rpc_forced_hold "
         "where row_id=new.id and attempted_status=new.status) then "
         "insert into public.scene_rpc_hold_evidence(row_id,attempted_status) "
         "values (new.id,new.status) on conflict (row_id) do nothing; "
         "select persisted_status into new.status "
         "from public.scene_rpc_forced_hold where row_id=new.id; "
         "new.variant_status := 'archived'; "
         "new.media_not_ready_reason := 'scene_review_hold'; "
         "new.publish_claim_token := null; "
         "new.publish_reservation_day := null; end if; return new; end $$")
    _sql("create trigger scene_rpc_force_hold before update "
         "on public.content_calendar for each row "
         "execute function public.scene_rpc_force_hold()")


@pytest.fixture(autouse=True)
def _isolate_scenarios():
    _sql("truncate public.scene_rpc_hold_evidence, "
         "public.scene_rpc_forced_hold, public.content_calendar")


GYM = "gym_" + uuid.uuid4().hex[:10]


def _insert_row(status="pending", variant_status="active",
                image_url="https://scratch.example/media.jpg",
                media_not_ready_reason=None, gym=GYM):
    reason = "null" if media_not_ready_reason is None \
        else f"'{media_not_ready_reason}'"
    image = "null" if image_url is None else f"'{image_url}'"
    return _one(
        "insert into public.content_calendar"
        "(gym_id,status,variant_status,image_url,media_not_ready_reason,"
        "format,account,post_date,visual_group_key) values "
        f"('{gym}','{status}','{variant_status}',{image},{reason},"
        "'feed','ig','2026-10-10','vg_scratch') returning id"
    )


def _force_hold(row_id, attempted_status, persisted_status="pending"):
    persisted = "null" if persisted_status is None else f"'{persisted_status}'"
    _sql("insert into public.scene_rpc_forced_hold"
         "(row_id,attempted_status,persisted_status) "
         f"values ('{row_id}','{attempted_status}',{persisted})")


def _row(row_id):
    raw = _one(
        "select jsonb_build_object("
        "'status',status,'variant_status',variant_status,"
        "'media_not_ready_reason',media_not_ready_reason,"
        "'publish_claim_token',publish_claim_token,"
        "'publish_reservation_day',publish_reservation_day)::text "
        f"from public.content_calendar where id='{row_id}'"
    )
    return json.loads(raw)


def _claim(row_id, gym=GYM, capacity=1, approved_only=False):
    return _one(
        "select public.claim_calendar_publish_slot_owned("
        f"'{row_id}','{gym}','2026-10-10','UTC',{capacity},"
        f"{'true' if approved_only else 'false'})"
    )


def _approve(row_id, gym=GYM):
    return _one(
        "select count(*) from public.approve_calendar_row_if_media_ready("
        f"'{row_id}','{gym}')"
    )


def _assert_held(row_id, attempted_status):
    row = _row(row_id)
    assert row == {
        "status": "pending",
        "variant_status": "archived",
        "media_not_ready_reason": "scene_review_hold",
        "publish_claim_token": None,
        "publish_reservation_day": None,
    }
    assert _one(
        "select attempted_status from public.scene_rpc_hold_evidence "
        f"where row_id='{row_id}'"
    ) == attempted_status
    assert _one(
        "select count(*) from public.scene_rpc_hold_evidence "
        f"where row_id='{row_id}'"
    ) == "1"


def test_original_claim_rejects_already_held_input_without_mutation():
    row_id = _insert_row(variant_status="archived",
                         media_not_ready_reason="scene_review_hold")
    before = _row(row_id)
    assert _claim(row_id) == ""
    assert _row(row_id) == before


def test_original_approval_rejects_already_held_input_without_mutation():
    row_id = _insert_row(variant_status="archived",
                         media_not_ready_reason="scene_review_hold")
    before = _row(row_id)
    assert _approve(row_id) == "0"
    assert _row(row_id) == before


def test_claim_trigger_converts_attempt_to_durable_hold_and_returns_null():
    row_id = _insert_row()
    _force_hold(row_id, "publishing")
    assert _claim(row_id) == ""
    _assert_held(row_id, "publishing")


def test_approval_trigger_converts_attempt_to_durable_hold_and_returns_empty():
    row_id = _insert_row()
    _force_hold(row_id, "approved")
    assert _approve(row_id) == "0"
    _assert_held(row_id, "approved")


def test_claim_trigger_persisting_null_status_cannot_return_token():
    row_id = _insert_row()
    _force_hold(row_id, "publishing", persisted_status=None)
    assert _claim(row_id) == ""
    row = _row(row_id)
    assert row["status"] is None
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    assert _one("select attempted_status from public.scene_rpc_hold_evidence "
                f"where row_id='{row_id}'") == "publishing"


def test_approval_trigger_persisting_null_status_cannot_return_row():
    row_id = _insert_row()
    _force_hold(row_id, "approved", persisted_status=None)
    assert _approve(row_id) == "0"
    row = _row(row_id)
    assert row["status"] is None
    assert row["variant_status"] == "archived"
    assert row["media_not_ready_reason"] == "scene_review_hold"
    assert row["publish_claim_token"] is None
    assert row["publish_reservation_day"] is None
    assert _one("select attempted_status from public.scene_rpc_hold_evidence "
                f"where row_id='{row_id}'") == "approved"


def test_claim_normal_success_and_replay_preserve_one_token():
    row_id = _insert_row()
    token = _claim(row_id)
    assert token
    row = _row(row_id)
    assert row["status"] == "publishing"
    assert row["publish_claim_token"] == token
    assert row["publish_reservation_day"] == "2026-10-10"
    assert _claim(row_id) == ""
    assert _row(row_id) == row


def test_approval_normal_success_and_replay_return_one_then_zero():
    row_id = _insert_row()
    assert _approve(row_id) == "1"
    assert _row(row_id)["status"] == "approved"
    assert _approve(row_id) == "0"
    assert _row(row_id)["status"] == "approved"


def test_tenant_isolation_remains_fail_closed_for_both_original_names():
    row_id = _insert_row()
    before = _row(row_id)
    assert _claim(row_id, gym="other_gym") == ""
    assert _approve(row_id, gym="other_gym") == "0"
    assert _row(row_id) == before


def test_original_media_and_capacity_gates_remain_fail_closed():
    blank_id = _insert_row(image_url="")
    assert _claim(blank_id) == ""
    assert _approve(blank_id) == "0"

    used_id = _insert_row(status="publishing")
    _sql("update public.content_calendar "
         "set publish_reservation_day='2026-10-10' "
         f"where id='{used_id}'")
    candidate_id = _insert_row()
    assert _claim(candidate_id, capacity=1) == ""
    assert _row(candidate_id)["status"] == "pending"


def test_approved_only_gate_remains_fail_closed_for_pending_input():
    row_id = _insert_row()
    assert _claim(row_id, approved_only=True) == ""
    assert _row(row_id)["status"] == "pending"


def test_replayed_repair_keeps_execute_service_role_only():
    signatures = (
        "public.claim_calendar_publish_slot_owned"
        "(uuid,text,date,text,integer,boolean)",
        "public.approve_calendar_row_if_media_ready(uuid,text)",
    )
    for signature in signatures:
        assert _one(
            "select has_function_privilege("
            f"'service_role','{signature}','EXECUTE')"
        ) == "t"
        for role in ("anon", "authenticated"):
            assert _one(
                "select has_function_privilege("
                f"'{role}','{signature}','EXECUTE')"
            ) == "f"


def _run_together(callable_):
    barrier = threading.Barrier(2)

    def ready_call():
        barrier.wait(timeout=5)
        return callable_()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ready_call) for _ in range(2)]
        return [future.result(timeout=30) for future in futures]


def test_concurrent_claims_yield_one_token_and_one_null():
    row_id = _insert_row()
    results = _run_together(lambda: _claim(row_id))
    tokens = [result for result in results if result]
    assert len(tokens) == 1
    assert results.count("") == 1
    row = _row(row_id)
    assert row["status"] == "publishing"
    assert row["publish_claim_token"] == tokens[0]


def test_concurrent_approvals_yield_one_row_and_one_empty_result():
    row_id = _insert_row()
    results = _run_together(lambda: _approve(row_id))
    assert sorted(results) == ["0", "1"]
    assert _row(row_id)["status"] == "approved"


def test_concurrent_forced_claims_both_report_null_and_leave_one_hold():
    row_id = _insert_row()
    _force_hold(row_id, "publishing")
    assert _run_together(lambda: _claim(row_id)) == ["", ""]
    _assert_held(row_id, "publishing")
