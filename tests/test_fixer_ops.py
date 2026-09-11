"""D72: the FIXER's ops-action lane (agent/fixer_ops.py) and its two HTTP mounts.

Auth (401 / 503), the catalog, each action's happy path against fakes, the org-floor
refusal, unknown actions, the volume preflight, the background job and its status route,
the ticket record and the audit line, and the intake_web transport."""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import fixer_ops as FO  # noqa: E402

SECRET = "s3cr3t-" * 4
GYM = "crossfitreverb30b5b2"
TICKET = "27728832-ae54-428c-af8b-0cb5c8ca1666"


class FakeBus:
    def __init__(self):
        self.rows = []

    def record_outbound(self, **kw):
        self.rows.append(kw)
        return {"id": f"m-{len(self.rows)}", **kw}


def _hdr(secret=SECRET):
    h = {FO.HEADER: secret} if secret is not None else {}
    return lambda k, d=None: h.get(k, d)


def _post(action, body, *, deps=None, secret=SECRET, logs=None):
    return FO.handle("POST", f"{FO.ROUTE_PREFIX}/{action}", _hdr(secret),
                     json.dumps(body).encode(), deps=deps or {},
                     log=(logs.append if logs is not None else (lambda *a: None)))


def _body(action="reset_recreate_budget", **args):
    return {"gym_key": GYM, "ticket_id": TICKET, "args": args}


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv(FO.SECRET_ENV, SECRET)


# ---- auth -------------------------------------------------------------------------------

def test_missing_secret_env_is_503_not_open(monkeypatch):
    monkeypatch.delenv(FO.SECRET_ENV, raising=False)
    status, body = _post("reset_recreate_budget", _body(), secret=SECRET)
    assert status == 503 and body["error"] == "ops_secret_unset"


def test_wrong_or_missing_header_is_401(armed):
    assert _post("reset_recreate_budget", _body(), secret="nope")[0] == 401
    assert _post("reset_recreate_budget", _body(), secret=None)[0] == 401
    # auth runs BEFORE routing: an unknown action with no secret is still 401, not 404
    assert _post("does_not_exist", _body(), secret=None)[0] == 401


def test_paths_outside_the_prefix_are_not_ours(armed):
    assert FO.handle("POST", "/ops/heartbeat", _hdr(), b"{}") is None
    assert FO.handle("GET", "/healthz", _hdr(), b"") is None


def test_catalog_is_listable(armed):
    status, body = FO.handle("GET", FO.ROUTE_PREFIX, _hdr(), b"")
    assert status == 200
    names = {a["name"] for a in body["actions"]}
    assert names == {"resend_connect_link", "reset_recreate_budget", "release_denied_assets",
                     "swap_media", "requeue_failed_row", "restage_month"}
    assert "refund" in body["org_floor"] and "delete_published_post" in body["org_floor"]


# ---- validation --------------------------------------------------------------------------

def test_bad_json_and_missing_fields_are_400(armed):
    status, body = FO.handle("POST", f"{FO.ROUTE_PREFIX}/reset_recreate_budget", _hdr(),
                             b"not json")
    assert status == 400
    assert _post("reset_recreate_budget", {"ticket_id": TICKET})[0] == 400
    assert _post("reset_recreate_budget", {"gym_key": GYM})[0] == 400
    assert _post("reset_recreate_budget", {"gym_key": "../x", "ticket_id": TICKET})[0] == 400
    assert _post("reset_recreate_budget", {"gym_key": GYM, "ticket_id": TICKET,
                                           "args": "x"})[0] == 400


def test_unknown_action_is_404_and_never_runs(armed):
    status, body = _post("nuke_everything", _body())
    assert status == 404 and body["error"] == "unknown_action"


@pytest.mark.parametrize("name", sorted(FO.ORG_FLOOR_ACTIONS))
def test_org_floor_actions_are_403_by_name(armed, name):
    bus = FakeBus()
    logs = []
    status, body = _post(name, _body(), deps={"bus": bus}, logs=logs)
    assert status == 403 and body["error"] == "org_floor"
    assert bus.rows == [], "a refused action writes no ticket record"
    assert any("AUDIT" in line and "org floor" in line for line in logs)


# ---- each action, happy path against fakes ---------------------------------------------

def test_reset_recreate_budget_records_and_audits(armed):
    bus = FakeBus()
    logs = []
    calls = []

    def reset(key):
        calls.append(key)
        return {"before": {"limit": 30, "used": 30, "remaining": 0},
                "after": {"limit": 30, "used": 0, "remaining": 30}}

    status, body = _post("reset_recreate_budget", _body(),
                         deps={"bus": bus, "reset_recreate_budget": reset}, logs=logs)
    assert status == 200 and body["ok"] and calls == [GYM]
    assert body["result"]["after"]["used"] == 0
    assert len(bus.rows) == 1
    row = bus.rows[0]
    assert row["ticket_id"] == TICKET and row["author_type"] == "system"
    assert row["body"].startswith("OPS ACTION reset_recreate_budget by fixer: 200 ")
    assert row["kind"] == "escalation" and row["delivery_status"] is None, \
        "client-invisible kind, never posted by an outbox"
    assert row["meta"]["ops_action"] == "reset_recreate_budget"
    assert any(line.startswith("[fixer-ops] AUDIT action=reset_recreate_budget") for line in logs)


def test_resend_connect_link_forces_the_send_and_reports_a_decline(armed):
    seen = {}

    def notify(base_key, gym_id, name, *, force=False, alert=None, **kw):
        seen.update(base_key=base_key, gym_id=gym_id, name=name, force=force)
        return True

    deps = {"bus": FakeBus(), "gym_lookup": lambda k: ("g-uuid", "CrossFit Reverb"),
            "notify_new_gym": notify}
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 200 and body["result"]["sent"] is True
    assert seen == {"base_key": GYM, "gym_id": "g-uuid", "name": "CrossFit Reverb", "force": True}

    def declines(base_key, gym_id, name, *, force=False, alert=None, **kw):
        alert("no single client_owner email found")
        return False

    deps["notify_new_gym"] = declines
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 409 and body["ok"] is False and "client_owner" in body["detail"]

    deps["gym_lookup"] = lambda k: (None, None)
    assert _post("resend_connect_link", _body(), deps=deps)[0] == 404


def test_release_denied_assets_runs_the_sweep_when_the_volume_is_there(armed):
    deps = {"bus": FakeBus(), "volume_available": lambda: True,
            "observe_denials": lambda: {"checked": 4, "rolled_back": 2}}
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 200 and body["result"]["rolled_back"] == 2


def test_volume_bound_actions_refuse_honestly_on_a_host_without_it(armed):
    bus = FakeBus()
    deps = {"bus": bus, "volume_available": lambda: False,
            "observe_denials": lambda: pytest.fail("must not run")}
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 503 and body["error"] == "volume_unavailable"
    assert "echo worker" in body["detail"] and bus.rows == []
    status, body = _post("restage_month", _body(days=7), deps=deps)
    assert status == 503


def test_swap_media_passes_the_row_and_a_fixer_actor(armed):
    seen = {}

    def handler(account_key, draft_id, actor_id, **kw):
        seen.update(account_key=account_key, draft_id=draft_id, actor_id=actor_id)
        return 200, {"ok": True, "action": "swap-media", "draft_id": draft_id, "free": True}

    status, body = _post("swap_media", _body(row_id="row-abc-123"),
                         deps={"bus": FakeBus(), "handle_swap_media": handler})
    assert status == 200 and body["result"]["free"] is True
    assert seen == {"account_key": GYM, "draft_id": "row-abc-123",
                    "actor_id": f"fixer:{TICKET}"}
    # the wrapped function's refusal is passed through, not masked as success
    refuse = lambda a, d, actor, **kw: (409, {"ok": False, "error": "photo is locked"})  # noqa: E731
    status, body = _post("swap_media", _body(row_id="row-abc-123"),
                         deps={"bus": FakeBus(), "handle_swap_media": refuse})
    assert status == 409 and body["ok"] is False and body["error"] == "photo is locked"
    assert _post("swap_media", _body(), deps={"bus": FakeBus()})[0] == 400, "row_id required"


class FakeCalendarStore:
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.requeued = []

    def get_row(self, account_key, row_id):
        r = self.rows.get(row_id)
        return dict(r) if r and r.get("gym_id") == account_key else None

    def requeue_failed_row(self, row_id):
        self.requeued.append(row_id)
        r = self.rows[row_id]
        if r["status"] != "failed":
            return None
        r["status"] = "approved"
        return dict(r)


def test_requeue_failed_row_checks_ownership_and_state_first(armed):
    store = FakeCalendarStore([
        {"id": "row-failed-1", "gym_id": GYM, "status": "failed", "post_date": "2026-09-12"},
        {"id": "row-live-22", "gym_id": GYM, "status": "published"},
        {"id": "row-other-33", "gym_id": "othergym", "status": "failed"}])
    deps = {"bus": FakeBus(), "calendar_store": store}
    status, body = _post("requeue_failed_row", _body(row_id="row-failed-1"), deps=deps)
    assert status == 200 and body["result"]["status"] == "approved"
    assert store.requeued == ["row-failed-1"]
    status, body = _post("requeue_failed_row", _body(row_id="row-live-22"), deps=deps)
    assert status == 409 and body["error"] == "row_not_failed"
    status, body = _post("requeue_failed_row", _body(row_id="row-other-33"), deps=deps)
    assert status == 404 and body["error"] == "row_not_found", "another gym's row never loads"
    assert store.requeued == ["row-failed-1"], "only the owned, failed row was written"


# ---- restage_month: background job + status route ----------------------------------------

def _restage_deps(bus, jobs, *, fail=False):
    calls = {}

    def sync_sources(gym_key, render_budget):
        calls["sync"] = (gym_key, render_budget)
        return [{"gym_id": gym_key, "rendered": 3}]

    def build(gym_key, start, days):
        calls["build"] = (gym_key, start, days)
        if fail:
            raise RuntimeError("voice doc missing")
        return {"ok": True, "upserted": days * 2, "days": days}

    return calls, {"bus": bus, "jobs": jobs, "volume_available": lambda: True,
                   "thread_runner": lambda fn: fn(),        # synchronous for the test
                   "sync_sources": sync_sources, "build_month": build,
                   "observe_denials": lambda: {"checked": 1, "rolled_back": 1}}


def test_restage_month_returns_a_job_and_runs_the_recipe_in_order(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    calls, deps = _restage_deps(bus, jobs)
    status, body = _post("restage_month", _body(days=21, start_date="2026-09-12",
                                                render_budget=40), deps=deps)
    assert status == 202 and body["ok"] and body["status"] == "running"
    job_id = body["job_id"]
    assert calls["sync"] == (GYM, 40) and calls["build"] == (GYM, "2026-09-12", 21)
    status, out = FO.handle("GET", f"{FO.ROUTE_PREFIX}/jobs/{job_id}", _hdr(), b"",
                            deps={"jobs": jobs})
    assert status == 200 and out["job"]["status"] == "done"
    assert [s["step"] for s in out["job"]["steps"]] == ["prerender", "observe_denials", "build"]
    assert out["job"]["result"]["build"]["upserted"] == 42
    # two ticket records: the accepted call and the job's completion (order depends on the
    # runner; the synchronous test runner finishes the job before run_action records 202)
    bodies = [r["body"] for r in bus.rows]
    assert len(bodies) == 2
    assert any(b.startswith("OPS ACTION restage_month by fixer: 202 ") for b in bodies)
    assert any("done" in b and job_id in b for b in bodies)


def test_restage_month_job_failure_is_recorded_not_swallowed(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    _, deps = _restage_deps(bus, jobs, fail=True)
    status, body = _post("restage_month", _body(days=7), deps=deps)
    job = jobs.get(body["job_id"])
    assert job["status"] == "failed" and "voice doc missing" in job["error"]
    assert any("FAILED" in r["body"] for r in bus.rows)


def test_restage_month_bounds_its_inputs(armed):
    deps = {"bus": FakeBus(), "jobs": FO.Jobs(), "volume_available": lambda: True,
            "thread_runner": lambda fn: pytest.fail("must not start")}
    assert _post("restage_month", _body(days=32), deps=deps)[0] == 400
    assert _post("restage_month", _body(days=0), deps=deps)[0] == 400
    assert _post("restage_month", _body(days="x"), deps=deps)[0] == 400
    assert _post("restage_month", _body(days=5, start_date="tomorrow"), deps=deps)[0] == 400
    assert _post("restage_month", _body(days=5, render_budget=999), deps=deps)[0] == 400


def test_job_status_route_404s_unknown_and_reports_timeouts(armed):
    from datetime import datetime, timedelta, timezone
    jobs = FO.Jobs()
    assert FO.handle("GET", f"{FO.ROUTE_PREFIX}/jobs/{'0' * 32}", _hdr(), b"",
                     deps={"jobs": jobs})[0] == 404
    job = jobs.create(action="restage_month", gym_key=GYM, ticket_id=TICKET, deadline_sec=10)
    later = datetime.now(timezone.utc) + timedelta(seconds=60)
    status, out = FO.handle("GET", f"{FO.ROUTE_PREFIX}/jobs/{job['id']}", _hdr(), b"",
                            deps={"jobs": jobs}, now=later)
    assert status == 200 and out["job"]["status"] == "timed_out"


def test_a_wrapped_function_fault_is_a_named_500_with_no_ticket_record(armed):
    bus = FakeBus()

    def boom(key):
        raise ValueError("kv down")

    status, body = _post("reset_recreate_budget", _body(),
                         deps={"bus": bus, "reset_recreate_budget": boom})
    assert status == 500 and body["error"] == "ValueError" and bus.rows == []


# ---- the intake_web transport ------------------------------------------------------------

class _Headers(dict):
    def get(self, k, default=None):
        for key, val in self.items():
            if key.lower() == k.lower():
                return val
        return default


def _handler_instance(path, headers, body=b"{}"):
    from agent import intake_web
    server = intake_web.build_server(0)
    try:
        Handler = server.RequestHandlerClass
    finally:
        server.server_close()
    inst = Handler.__new__(Handler)
    inst.path = path
    inst.headers = _Headers(headers)
    inst.client_address = ("10.0.0.1", 5555)
    inst.rfile = io.BytesIO(body)
    inst.wfile = io.BytesIO()
    captured = {}
    inst._send_json = lambda obj, status=200, cors_origin="": captured.update(json=(status, obj))
    inst._deny = lambda code=404, msg="not found": captured.update(deny=(code, msg))
    inst._send_html = lambda body_str, status=200: captured.update(html=(status, body_str))
    return inst, captured


def test_intake_web_mounts_the_routes(armed, monkeypatch):
    monkeypatch.setattr(FO, "run_action",
                        lambda action, gym, ticket, args, **kw: (200, {"ok": True, "action": action,
                                                                       "echo": args}))
    payload = json.dumps(_body(row_id="r-123456")).encode()
    inst, cap = _handler_instance(f"{FO.ROUTE_PREFIX}/swap_media",
                                  {FO.HEADER: SECRET, "Content-Length": str(len(payload))},
                                  payload)
    inst.do_POST()
    assert cap["json"][0] == 200 and cap["json"][1]["echo"] == {"row_id": "r-123456"}
    inst, cap = _handler_instance(f"{FO.ROUTE_PREFIX}/swap_media",
                                  {FO.HEADER: "wrong", "Content-Length": str(len(payload))},
                                  payload)
    inst.do_POST()
    assert cap["json"][0] == 401
    inst, cap = _handler_instance(FO.ROUTE_PREFIX, {FO.HEADER: SECRET})
    inst.do_GET()
    assert cap["json"][0] == 200 and "actions" in cap["json"][1]


def test_intake_web_bounds_the_body_before_reading_it(armed):
    inst, cap = _handler_instance(f"{FO.ROUTE_PREFIX}/swap_media",
                                  {FO.HEADER: SECRET,
                                   "Content-Length": str(FO.MAX_BODY_BYTES + 1)}, b"x")
    inst.do_POST()
    assert cap["json"][0] == 413
