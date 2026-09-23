"""D72: the FIXER's ops-action lane (agent/fixer_ops.py) and its two HTTP mounts.

Auth (401 / 503), the catalog, each action's happy path against fakes, the org-floor
refusal, unknown actions, the volume preflight, the background job and its status route,
the ticket record and the audit line, and the intake_web transport."""
import io
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import fixer_ops as FO  # noqa: E402
from agent import fixer_ops_receipts as FR  # noqa: E402

SECRET = "s3cr3t-" * 4
GYM = "crossfitreverb30b5b2"
TICKET = "27728832-ae54-428c-af8b-0cb5c8ca1666"


class FakeBus:
    def __init__(self, ticket=None, tokens=None, error=None, reverse_tokens=None):
        self.rows = []
        self.ticket_row = ticket if ticket is not None else {
            "id": TICKET, "product": "echo", "source": "ops_fix", "client_id": None,
            "raw_text": f"OPS-FIX REQUEST: ECHO ALERT: repair {GYM}",
            "verification_before": {"fixer": {"triage": {"gym_key": GYM}}}}
        self.token_rows = tokens if tokens is not None else []
        self.reverse_token_rows = reverse_tokens
        self.error = error
        self.request_messages = []

    def _get(self, table, params):
        if self.error:
            raise self.error
        if table == "support_tickets":
            return [self.ticket_row] if self.ticket_row and params["id"] == f"eq.{TICKET}" else []
        if table == "echo_intake_tokens":
            if "echo_account_key" in params and self.reverse_token_rows is not None:
                return self.reverse_token_rows
            return self.token_rows
        if table == "support_messages":
            return [dict(row) for row in self.request_messages]
        raise AssertionError(f"unexpected table: {table}")

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


def test_client_ticket_gym_mapping_is_checked_before_ops_side_effect(armed):
    client_id = "a0fcb10f-73dc-4e56-b6ca-61ac9bc9470f"
    ticket = {"id": TICKET, "product": "echo", "source": "slack_conversation",
              "client_id": client_id}
    calls = []
    def reset(key):
        calls.append(key)
        return {"before": {"limit": 1, "used": 1, "remaining": 0},
                "after": {"limit": 1, "used": 0, "remaining": 1}}
    deps = {"reset_recreate_budget": reset}
    for tokens, expected in [
        ([{"gym_id": client_id, "echo_account_key": "differentgym123"}], "ticket_tenant_mismatch"),
        ([], "ticket_tenant_unconfirmed"),
        ([{"gym_id": client_id, "echo_account_key": GYM}] * 2, "ticket_tenant_unconfirmed"),
        ([{"gym_id": "other-uuid", "echo_account_key": GYM}], "ticket_tenant_unconfirmed"),
        ([{"gym_id": client_id, "echo_account_key": None}], "ticket_tenant_unconfirmed"),
    ]:
        status, body = _post("reset_recreate_budget", _body(),
                             deps={**deps, "bus": FakeBus(ticket, tokens)})
        assert status == 409 and body["error"] == expected
    status, body = _post("reset_recreate_budget", _body(),
                         deps={**deps, "bus": FakeBus(ticket, [{"gym_id": client_id,
                                                               "echo_account_key": GYM}])})
    assert status == 200 and body["ok"] is True and calls == [GYM]

    status, body = _post("reset_recreate_budget", _body(),
                         deps={**deps, "bus": FakeBus(
                             ticket, [{"gym_id": client_id, "echo_account_key": GYM}],
                             reverse_tokens=[{"gym_id": client_id, "echo_account_key": GYM},
                                             {"gym_id": "other-gym-uuid", "echo_account_key": GYM}])})
    assert status == 409 and body["error"] == "ticket_tenant_unconfirmed"
    assert calls == [GYM]


def test_tenant_store_failure_or_missing_client_does_not_run_action(armed):
    calls = []
    action = lambda key: calls.append(key)
    for bus, status in [
        (FakeBus(ticket={"id": TICKET, "product": "echo", "source": "slack_conversation",
                         "client_id": None}), 409),
        (FakeBus(ticket={"id": "other", "product": "echo", "source": "ops_fix",
                         "client_id": None}), 409),
        (FakeBus(error=RuntimeError("store unavailable")), 503),
    ]:
        result, _body_out = _post("reset_recreate_budget", _body(),
                                  deps={"bus": bus, "reset_recreate_budget": action})
        assert result == status
    assert calls == []


def test_internal_ops_ticket_requires_persisted_and_raw_tenant_binding(armed):
    calls = []
    action = lambda key: calls.append(key) or {
        "before": {"limit": 1, "used": 1, "remaining": 0},
        "after": {"limit": 1, "used": 0, "remaining": 1},
    }
    base = {"id": TICKET, "product": "echo", "source": "ops_fix", "client_id": None}
    hostile = [
        {**base, "raw_text": f"repair {GYM}",
         "verification_before": {"fixer": {"triage": {"gym_key": OTHER_GYM}}}},
        {**base, "raw_text": "repair some other gym",
         "verification_before": {"fixer": {"triage": {"gym_key": GYM}}}},
        {**base, "raw_text": f"repair prefix{GYM}suffix",
         "verification_before": {"fixer": {"triage": {"gym_key": GYM}}}},
    ]
    for ticket in hostile:
        status, body = _post("reset_recreate_budget", _body(), deps={
            "bus": FakeBus(ticket=ticket), "reset_recreate_budget": action})
        assert status == 409 and body["error"] == "ticket_tenant_unconfirmed"
    assert calls == []


def test_account_key_ticket_must_match_exactly(armed):
    ticket = {"id": TICKET, "product": "echo", "source": "slack_conversation",
              "client_id": "othergym123"}
    status, body = _post("reset_recreate_budget", _body(), deps={"bus": FakeBus(ticket)})
    assert status == 409 and body["error"] == "ticket_tenant_mismatch"


def test_org_floor_refusal_does_not_write_to_another_tenants_ticket(armed):
    bus = FakeBus(ticket={"id": TICKET, "product": "echo", "source": "slack_conversation",
                          "client_id": "othergym123"})
    status, body = _post("refund", _body(), deps={"bus": bus})
    assert status == 403 and body["error"] == "org_floor"
    assert bus.rows == []


@pytest.mark.parametrize("name", sorted(FO.ORG_FLOOR_ACTIONS))
def test_org_floor_actions_are_403_by_name(armed, name):
    bus = FakeBus()
    logs = []
    status, body = _post(name, _body(), deps={"bus": bus}, logs=logs)
    assert status == 403 and body["error"] == "org_floor"
    # round 2 (R4): the refusal leaves a trace on the ticket, and nothing else runs
    assert len(bus.rows) == 1 and "REFUSED: org_floor" in bus.rows[0]["body"]
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
    assert body["result"]["postcondition_verified"] is True
    assert len(bus.rows) == 1
    row = bus.rows[0]
    assert row["ticket_id"] == TICKET and row["author_type"] == "system"
    assert row["body"].startswith("OPS ACTION reset_recreate_budget by fixer: 200 ")
    assert row["kind"] == "escalation" and row["delivery_status"] is None, \
        "client-invisible kind, never posted by an outbox"
    assert row["meta"]["ops_action"] == "reset_recreate_budget"
    assert any(line.startswith("[fixer-ops] AUDIT action=reset_recreate_budget") for line in logs)


def test_reset_recreate_budget_refuses_unconfirmed_readback(armed):
    def reset(_key):
        return {"before": {"limit": 30, "used": 30, "remaining": 0},
                "after": {"limit": 30, "used": 1, "remaining": 29}}

    status, body = _post("reset_recreate_budget", _body(),
                         deps={"bus": FakeBus(), "reset_recreate_budget": reset})
    assert status == 409 and body["ok"] is False
    assert body["error"] == "postcondition_unconfirmed"


def test_resend_connect_link_forces_the_send_and_reports_a_decline(armed):
    seen = {}

    def notify(base_key, gym_id, name, *, force=False, alert=None, **kw):
        seen.update(base_key=base_key, gym_id=gym_id, name=name, force=force)
        return True

    deps = {"bus": FakeBus(), "gym_lookup": lambda k: ("g-uuid", "CrossFit Reverb"),
            "notify_new_gym": notify, "is_echo_client": lambda gid: gid == "g-uuid"}
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 200
    assert body["result"]["sent"] is None
    assert body["result"]["postcondition_verified"] is False
    assert body["result"]["sent_message_identity"] is None
    assert seen == {"base_key": GYM, "gym_id": "g-uuid", "name": "CrossFit Reverb", "force": True}

    def declines(base_key, gym_id, name, *, force=False, alert=None, **kw):
        alert("no single client_owner email found")
        return False

    deps["notify_new_gym"] = declines
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 409 and body["ok"] is False and "client_owner" in body["detail"]

    deps["gym_lookup"] = lambda k: (None, None)
    assert _post("resend_connect_link", _body(), deps=deps)[0] == 404


def test_resend_connect_link_verifies_only_with_provider_message_identity(armed):
    deps = {
        "bus": FakeBus(),
        "gym_lookup": lambda k: ("g-uuid", "CrossFit Reverb"),
        "notify_new_gym": lambda *a, **kw: {
            "sent": True,
            "provider": "slack",
            "channel": "D123",
            "ts": "1726682400.000100",
        },
        "is_echo_client": lambda gid: gid == "g-uuid",
    }
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 200
    assert body["result"]["sent"] is True
    assert body["result"]["postcondition_verified"] is True
    assert body["result"]["sent_message_identity"] == {
        "provider": "slack",
        "channel": "D123",
        "ts": "1726682400.000100",
        "message_id": None,
    }


@pytest.mark.parametrize("fake_identity", [
    {"sent": True, "provider": "slack", "channel": "D123"},
    {"sent": True, "message": "looks-like-proof"},
    {"sent": True, "message_identity": "not-an-object"},
    {"sent": True, "message_identity": {
        "provider": "email", "channel": "D123", "message_id": "m1"}},
])
def test_resend_connect_link_rejects_fabricated_message_identity(armed, fake_identity):
    deps = {
        "bus": FakeBus(),
        "gym_lookup": lambda k: ("g-uuid", "CrossFit Reverb"),
        "notify_new_gym": lambda *a, **kw: fake_identity,
        "is_echo_client": lambda gid: gid == "g-uuid",
    }
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 200
    assert body["result"]["sent"] is None
    assert body["result"]["postcondition_verified"] is False
    assert body["result"]["sent_message_identity"] is None


@pytest.mark.parametrize("category", ["ready", "missing", "ambiguous", "user-email-missing",
                                      "portal-unavailable"])
def test_resend_readiness_is_read_only_and_reports_only_category(armed, category):
    sent = []
    bus = FakeBus()
    deps = {
        "bus": bus,
        "gym_lookup": lambda k: ("g-uuid", "Gym Name"),
        "is_echo_client": lambda gid: gid == "g-uuid",
        "owner_readiness": lambda gid: category,
        "notify_new_gym": lambda *a, **kw: sent.append((a, kw)),
    }
    status, body = FO.handle(
        "GET", f"{FO.ROUTE_PREFIX}/resend_connect_link/readiness/{GYM}", _hdr(), b"",
        deps=deps)
    assert status == 200 and body == {"category": category}
    assert sent == [] and bus.rows == []


def test_resend_readiness_preserves_auth_and_client_gates(armed):
    path = f"{FO.ROUTE_PREFIX}/resend_connect_link/readiness/{GYM}"
    assert FO.handle("GET", path, _hdr(None), b"")[0] == 401
    deps = {"gym_lookup": lambda k: ("g-uuid", "Gym Name"),
            "is_echo_client": lambda gid: False}
    status, body = FO.handle("GET", path, _hdr(), b"", deps=deps)
    assert status == 403 and body["error"] == "not_echo_client"


def test_resend_readiness_turns_lookup_error_into_portal_unavailable(armed):
    path = f"{FO.ROUTE_PREFIX}/resend_connect_link/readiness/{GYM}"
    deps = {"gym_lookup": lambda k: ("g-uuid", "Gym Name"),
            "is_echo_client": lambda gid: True,
            "owner_readiness": lambda gid: (_ for _ in ()).throw(RuntimeError("down"))}
    status, body = FO.handle("GET", path, _hdr(), b"", deps=deps)
    assert status == 200 and body == {"category": "portal-unavailable"}


def test_release_denied_assets_runs_the_sweep_when_the_volume_is_there(armed):
    deps = {"bus": FakeBus(), "volume_available": lambda: True,
            "observe_denials": lambda **kw: {"checked": 4, "rolled_back": 2}}
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 200 and body["result"]["rolled_back"] == 2
    assert body["result"]["postcondition_verified"] is False


def test_release_denied_assets_cannot_rollback_another_gym(armed, monkeypatch):
    from agent import gym_media_selector as selector

    other = "anothergym"
    records = [
        (f"gym_media_use:{GYM}:2026-09-12", [{"rolled_back": False}]),
        (f"gym_media_use:{other}:2026-09-12", [{"rolled_back": False}]),
    ]
    rolled = []
    monkeypatch.setattr(selector, "_use_records", lambda: records)
    monkeypatch.setattr(selector, "_default_fetch_rows", lambda gym, day: [
        {"source_media_asset_id": "asset-1", "status": "denied"}])
    monkeypatch.setattr(selector, "rollback_use", lambda gym, day, **kw: rolled.append(gym) or True)
    status, body = _post("release_denied_assets", _body(), deps={
        "bus": FakeBus(), "volume_available": lambda: True})
    assert status == 200 and body["result"]["rolled_back"] == 1
    assert body["result"]["checked"] == 1
    assert rolled == [GYM]


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
    row = {"id": "row-abc-123", "gym_id": GYM, "post_date": "2026-09-12",
           "account": "instagram", "format": "feed", "status": "pending",
           "image_url": "https://img/new.jpg"}

    class Store:
        def get_row(self, account_key, row_id):
            assert (account_key, row_id) == (GYM, "row-abc-123")
            return row

        def list_month(self, account_key, month):
            assert (account_key, month) == (GYM, "2026-09")
            return [row]

    def handler(account_key, draft_id, actor_id, **kw):
        seen.update(account_key=account_key, draft_id=draft_id, actor_id=actor_id)
        return 200, {"ok": True, "action": "swap-media", "draft_id": draft_id,
                     "free": True, "image_public_url": "https://img/new.jpg",
                     "siblings_swapped": [], "siblings_left": [],
                     "sibling_results": []}

    status, body = _post("swap_media", _body(row_id="row-abc-123"),
                         deps={"bus": FakeBus(), "handle_swap_media": handler,
                               "calendar_store": Store()})
    assert status == 200 and body["result"]["free"] is True
    assert body["result"]["postcondition_verified"] is True
    assert seen == {"account_key": GYM, "draft_id": "row-abc-123",
                    "actor_id": f"fixer:{TICKET}"}
    # the wrapped function's refusal is passed through, not masked as success
    refuse = lambda a, d, actor, **kw: (409, {"ok": False, "error": "photo is locked"})  # noqa: E731
    status, body = _post("swap_media", _body(row_id="row-abc-123"),
                         deps={"bus": FakeBus(), "handle_swap_media": refuse})
    assert status == 409 and body["ok"] is False and body["error"] == "photo is locked"
    assert _post("swap_media", _body(), deps={"bus": FakeBus()})[0] == 400, "row_id required"


def test_swap_media_does_not_claim_success_when_readback_disagrees(armed):
    calls = []
    stale = {"id": "row-abc-123", "gym_id": GYM, "post_date": "2026-09-12",
             "account": "instagram", "format": "feed", "status": "pending",
             "image_url": "https://img/old.jpg"}

    class Store:
        def get_row(self, account_key, row_id):
            return dict(stale)

        def list_month(self, account_key, month):
            return [dict(stale)]

    def handler(*args):
        calls.append(args)
        return 200, {"ok": True, "image_public_url": "https://img/new.jpg",
                     "siblings_swapped": [], "siblings_left": [],
                     "sibling_results": []}

    status, body = _post("swap_media", _body(row_id="row-abc-123"), deps={
        "bus": FakeBus(), "handle_swap_media": handler, "calendar_store": Store()})
    assert status == 409 and body["error"] == "postcondition_unconfirmed"
    assert "calendar readback unconfirmed" in body["summary"]
    assert len(calls) == 1


@pytest.mark.parametrize("sibling_result", [
    {"siblings_swapped": ["sibling-1"]},
    {"siblings_left": ["sibling-1"]},
])
def test_swap_media_does_not_verify_unread_or_failed_sibling_writes(armed, sibling_result):
    row = {"id": "row-abc-123", "gym_id": GYM, "post_date": "2026-09-12",
           "account": "instagram", "format": "feed", "status": "pending",
           "image_url": "https://img/new.jpg"}

    class Store:
        def get_row(self, account_key, row_id):
            return dict(row)

        def list_month(self, account_key, month):
            return [dict(row)]

    status, body = _post("swap_media", _body(row_id="row-abc-123"), deps={
        "bus": FakeBus(), "calendar_store": Store(),
        "handle_swap_media": lambda *args: (200, {
            "ok": True, "image_public_url": "https://img/new.jpg",
            **sibling_result})})
    assert status == 409 and body["error"] == "postcondition_unconfirmed"


def test_swap_media_verifies_thumbnail_and_each_successful_sibling(armed):
    rows = {
        "row-abc-123": {"id": "row-abc-123", "gym_id": GYM, "post_date": "2026-09-12",
                        "account": "instagram", "format": "feed", "status": "pending",
                        "source_media_asset_id": "asset-1",
                        "image_url": "https://img/full.jpg",
                        "thumbnail_url": "https://img/thumb.jpg"},
        "sibling-1": {"id": "sibling-1", "gym_id": GYM, "post_date": "2026-09-12",
                      "account": "instagram", "format": "story", "status": "pending",
                      "source_media_asset_id": "asset-1",
                      "image_url": "https://img/sibling-full.jpg",
                      "thumbnail_url": "https://img/sibling-thumb.jpg"},
    }

    class Store:
        def get_row(self, account_key, row_id):
            row = rows.get(row_id)
            return dict(row) if row and row["gym_id"] == account_key else None

        def list_month(self, account_key, month):
            return [dict(r) for r in rows.values()
                    if r["gym_id"] == account_key and r["post_date"].startswith(month)]

    status, body = _post("swap_media", _body(row_id="row-abc-123"), deps={
        "bus": FakeBus(), "calendar_store": Store(),
        "handle_swap_media": lambda *args: (200, {
            "ok": True,
            "image_public_url": "https://img/thumb.jpg",
            "siblings_swapped": ["sibling-1"],
            "siblings_left": [],
            "sibling_results": [{
                "id": "sibling-1",
                "image_public_url": "https://img/sibling-thumb.jpg",
                "media_kind": "image",
                "video_url": None,
            }],
        }),
    })
    assert status == 200
    assert body["result"]["postcondition_verified"] is True


def test_swap_media_requires_public_media_identity_to_verify(armed):
    row = {"id": "row-abc-123", "gym_id": GYM, "post_date": "2026-09-12",
           "account": "instagram", "format": "feed", "status": "pending",
           "image_url": "https://img/old.jpg"}
    get_row_calls = []

    class Store:
        def get_row(self, account_key, row_id):
            get_row_calls.append(row_id)
            return dict(row)

        def list_month(self, account_key, month):
            return [dict(row)]

    status, body = _post("swap_media", _body(row_id="row-abc-123"), deps={
        "bus": FakeBus(), "calendar_store": Store(),
        "handle_swap_media": lambda *args: (200, {"ok": True, "video_url": "https://img/new.mp4"})})
    assert status == 409 and body["error"] == "postcondition_unconfirmed"
    assert get_row_calls == ["row-abc-123"], \
        "the pre-swap derivation reads the row once; a missing media identity " \
        "triggers no post-handler readback"


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
    assert body["result"]["postcondition_verified"] is True
    assert store.requeued == ["row-failed-1"]
    status, body = _post("requeue_failed_row", _body(row_id="row-live-22"), deps=deps)
    assert status == 409 and body["error"] == "row_not_failed"
    status, body = _post("requeue_failed_row", _body(row_id="row-other-33"), deps=deps)
    assert status == 404 and body["error"] == "row_not_found", "another gym's row never loads"
    assert store.requeued == ["row-failed-1"], "only the owned, failed row was written"


def test_requeue_failed_row_refuses_a_stale_readback(armed):
    class StaleStore(FakeCalendarStore):
        def get_row(self, account_key, row_id):
            row = super().get_row(account_key, row_id)
            if row and self.requeued:
                return {**row, "status": "failed"}
            return row

    store = StaleStore([{"id": "row-failed-1", "gym_id": GYM,
                         "status": "failed", "post_date": "2026-09-12"}])
    status, body = _post("requeue_failed_row", _body(row_id="row-failed-1"),
                         deps={"bus": FakeBus(), "calendar_store": store})
    assert status == 409 and body["ok"] is False
    assert body["error"] == "postcondition_unconfirmed"


def test_requeue_failed_row_refuses_wrong_tenant_write_response(armed):
    class WrongTenantStore(FakeCalendarStore):
        def requeue_failed_row(self, row_id):
            row = super().requeue_failed_row(row_id)
            return {**row, "gym_id": "another-gym"}

    store = WrongTenantStore([{"id": "row-failed-1", "gym_id": GYM,
                               "status": "failed", "post_date": "2026-09-12"}])
    status, body = _post("requeue_failed_row", _body(row_id="row-failed-1"),
                         deps={"bus": FakeBus(), "calendar_store": store})
    assert status == 409 and body["ok"] is False
    assert body["error"] == "postcondition_unconfirmed"


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
                   "observe_denials": lambda **kw: {"checked": 1, "rolled_back": 1}}


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
    assert out["job"]["result"]["postcondition_verified"] is False
    # two ticket records: the accepted call and the job's completion (order depends on the
    # runner; the synchronous test runner finishes the job before run_action records 202)
    bodies = [r["body"] for r in bus.rows]
    assert len(bodies) == 2
    assert any(b.startswith("OPS ACTION restage_month by fixer: 202 ") for b in bodies)
    assert any("done" in b and job_id in b for b in bodies)


def test_restage_month_denial_sweep_stays_within_the_requested_gym(monkeypatch):
    from agent import gym_media_selector as selector

    other = "anothergym"
    monkeypatch.setattr(selector, "_use_records", lambda: [
        (f"gym_media_use:{GYM}:2026-09-12", [{"rolled_back": False}]),
        (f"gym_media_use:{other}:2026-09-12", [{"rolled_back": False}]),
    ])
    monkeypatch.setattr(selector, "_default_fetch_rows", lambda gym, day: [
        {"source_media_asset_id": "asset-1", "status": "denied"}])
    rolled = []
    monkeypatch.setattr(selector, "rollback_use", lambda gym, day, **kw: rolled.append(gym) or True)
    out = FO.run_restage_month(GYM, days=1, deps={
        "sync_sources": lambda gym, budget: [],
        "build_month": lambda gym, start, days: {"ok": True, "upserted": 0},
    }, log=lambda *a: None)
    assert out["observe_denials"]["rolled_back"] == 1
    assert out["observe_denials"]["checked"] == 1
    assert rolled == [GYM]


def test_restage_month_job_failure_is_recorded_not_swallowed(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    _, deps = _restage_deps(bus, jobs, fail=True)
    status, body = _post("restage_month", _body(days=7), deps=deps)
    job = jobs.get(body["job_id"])
    assert job["status"] == "failed" and "voice doc missing" in job["error"]
    assert any("FAILED" in r["body"] for r in bus.rows)


def test_restage_month_rejects_a_build_that_returns_ok_false(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    _, deps = _restage_deps(bus, jobs)
    deps["build_month"] = lambda _gym, _start, _days: {
        "ok": False, "reason": "calendar store write failed", "upserted": 0,
    }
    status, body = _post("restage_month", _body(days=7), deps=deps)
    assert status == 202
    job = jobs.get(body["job_id"])
    assert job["status"] == "failed"
    assert "calendar store write failed" in job["error"]
    assert job["steps"][-1]["ok"] is False
    assert any("FAILED" in row["body"] for row in bus.rows)


def test_restage_month_accepts_a_successful_noop_build(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    _, deps = _restage_deps(bus, jobs)
    deps["build_month"] = lambda _gym, _start, _days: {
        "ok": True, "upserted": 0, "reason": "no eligible rows to change",
    }
    status, body = _post("restage_month", _body(days=7), deps=deps)
    assert status == 202
    job = jobs.get(body["job_id"])
    assert job["status"] == "done"
    assert job["steps"][-1]["ok"] is True


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


# ---- round 2 (audit of PR #107) -----------------------------------------------------------

def test_resend_connect_link_refuses_a_non_echo_client_and_fails_closed(armed):
    """MINOR: notify_new_gym(force=True) DM'd 36 non-clients live. The resend is gated on an
    echo_gym_settings row for the gym; no row, or an unverifiable lookup, is a refusal."""
    sent = []
    deps = {"bus": FakeBus(), "gym_lookup": lambda k: ("g-uuid", "Not A Client Gym"),
            "notify_new_gym": lambda *a, **kw: sent.append(a) or True,
            "is_echo_client": lambda gid: False}
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 403 and body["error"] == "not_echo_client" and sent == []

    def boom(gid):
        raise RuntimeError("supabase down")

    deps["is_echo_client"] = boom
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 403 and sent == [], "an unverifiable client status never sends"


def test_resend_connect_link_uses_the_one_echo_client_universe(armed):
    """After the #108 merge the default predicate IS echo_clients.is_echo_client (D73) --
    the same gate notify_new_gym applies itself -- driven here through its test override."""
    from agent import echo_clients
    sent = []
    deps = {"bus": FakeBus(), "gym_lookup": lambda k: ("g-uuid", "CrossFit Reverb"),
            "notify_new_gym": lambda *a, **kw: sent.append(a) or True}
    prior = echo_clients._override
    try:
        echo_clients.set_test_override(lambda ident: ident in ("g-uuid", GYM))
        assert _post("resend_connect_link", _body(), deps=deps)[0] == 200
        echo_clients.set_test_override(lambda ident: False)
        status, body = _post("resend_connect_link", _body(), deps=deps)
        assert status == 403 and body["error"] == "not_echo_client"
    finally:
        echo_clients.set_test_override(prior)
    assert len(sent) == 1


def test_org_floor_refusal_leaves_a_trace_on_the_ticket(armed):
    """R4: a 403 wrote an audit line but no support_messages row; a teammate reading the
    thread should see the FIXER tried."""
    bus = FakeBus()
    status, body = _post("refund", _body(), deps={"bus": bus})
    assert status == 403
    assert len(bus.rows) == 1
    assert bus.rows[0]["body"].startswith("OPS ACTION refund by fixer: REFUSED: org_floor")
    assert bus.rows[0]["kind"] == "escalation" and bus.rows[0]["delivery_status"] is None


# ---- round 3 (K3): hostile reservation lifecycle, durable readback, truthful fields --------
# Store-level reservation semantics live in tests/test_fixer_ops_receipts.py; this section
# pins the ACTION-layer contract: side-effect ordering, replay call counts, per-action
# readback truthfulness, and the receipts/readiness HTTP routes.

OTHER_GYM = "anothergym999"
KEY = "rsv-k3-000001"


def _keyed_body(key=KEY, gym=GYM, ticket=TICKET, **args):
    return {"gym_key": gym, "ticket_id": ticket, "args": args, "reservation_key": key}


def _keyed_swap_body(key=KEY, **args):
    bus = FakeBus()
    current = FO._business_request_key(bus._get, bus.ticket_row)
    return {**_keyed_body(key=key, **args), "request_key": current}


def _ok_reset(calls):
    def reset(key):
        calls.append(key)
        return {"before": {"limit": 30, "used": 30, "remaining": 0},
                "after": {"limit": 30, "used": 0, "remaining": 30}}
    return reset


# -- 1. reservation lifecycle ---------------------------------------------------------------

def test_keyed_call_reserves_before_the_side_effect_and_commits_done(armed):
    store = {}
    seen_status = []
    calls = []

    def reset(key):
        calls.append(key)
        seen_status.append(store.get(KEY, {}).get("status"))
        return {"before": {"limit": 30, "used": 30, "remaining": 0},
                "after": {"limit": 30, "used": 0, "remaining": 30}}

    deps = {"bus": FakeBus(), "reset_recreate_budget": reset, "receipt_store": store}
    status, body = _post("reset_recreate_budget", _keyed_body(), deps=deps)
    assert status == 200 and body["ok"] is True
    assert seen_status == ["reserved"], "the durable reservation exists BEFORE any side effect"
    assert store[KEY]["status"] == "done", "a 2xx commits the receipt"
    receipt = body["receipt"]
    assert receipt["key"] == KEY and receipt["status"] == "done"
    assert receipt["action"] == "reset_recreate_budget"
    assert receipt["gym_key"] == GYM and receipt["ticket_id"] == TICKET
    assert receipt["result"]["after"]["used"] == 0
    assert receipt["finished_at"] is not None


def test_a_reserved_receipt_replay_is_an_explicit_409_and_runs_nothing(armed):
    store = {}
    FR.begin(store, KEY, "reset_recreate_budget", GYM, TICKET, {})  # left "reserved"
    calls = []
    deps = {"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
            "receipt_store": store}
    status, body = _post("reset_recreate_budget", _keyed_body(), deps=deps)
    assert status == 409 and body["error"] == "reservation_in_progress"
    assert calls == [], "an in-progress reservation never re-executes"


def test_a_failed_receipt_replay_is_an_explicit_409_and_runs_nothing(armed):
    store = {}
    FR.begin(store, KEY, "reset_recreate_budget", GYM, TICKET, {})
    FR.fail(store, KEY, "postcondition_unconfirmed")
    calls = []
    deps = {"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
            "receipt_store": store}
    status, body = _post("reset_recreate_budget", _keyed_body(), deps=deps)
    assert status == 409 and body["error"] == "reservation_previously_failed"
    assert calls == [], "a failed receipt never re-executes"


def test_an_unknown_receipt_refuses_replay_and_runs_nothing(armed):
    store = {}
    FR.begin(store, KEY, "swap_media", GYM, TICKET, {"row_id": "row-abc-123"},
             request_key=_keyed_swap_body()["request_key"])
    FR.mark_unknown(store, KEY, "response lost after the write; outcome undetermined")
    calls = []
    deps = {"bus": FakeBus(), "receipt_store": store,
            "handle_swap_media": lambda *a, **kw: calls.append(a) or (200, {"ok": True})}
    status, body = _post("swap_media", _keyed_swap_body(row_id="row-abc-123"), deps=deps)
    assert status == 409 and body["error"] == "reservation_outcome_unknown"
    assert calls == [], "an unknown outcome is NEVER automatically retried"


# -- 2 + 3. durable readback + duplicate no-rerun --------------------------------------------

def test_receipt_readback_comes_from_the_store_with_exact_identity(armed):
    backing = {}
    deps = {"bus": FakeBus(), "reset_recreate_budget": _ok_reset([]),
            "receipt_store": backing}
    status, body = _post("reset_recreate_budget", _keyed_body(), deps=deps)
    assert status == 200

    # before anything exists for a key the read route 404s
    status, body = FO.handle("GET", f"{FO.ROUTE_PREFIX}/receipts/rsv-k3-absent01"
                             f"?gym_key={GYM}", _hdr(), b"",
                             deps={"receipt_store": backing}, log=lambda *a: None)
    assert status == 404 and body["error"] == "unknown_receipt"

    # a "restarted" reader: a brand-new mapping reloaded from the durable bytes,
    # so the answer cannot come from request memory
    reloaded = json.loads(json.dumps(backing))
    status, body = FO.handle("GET", f"{FO.ROUTE_PREFIX}/receipts/{KEY}?gym_key={GYM}",
                             _hdr(), b"", deps={"receipt_store": reloaded},
                             log=lambda *a: None)
    assert status == 200 and body["ok"] is True
    r = body["receipt"]
    assert r["key"] == KEY
    assert r["action"] == "reset_recreate_budget"
    assert r["gym_key"] == GYM and r["ticket_id"] == TICKET
    assert r["status"] == "done"
    assert r["payload_hash"] == FR.payload_hash("reset_recreate_budget", GYM, TICKET, {})

    # the same fresh store refuses another tenant
    status, body = FO.handle("GET", f"{FO.ROUTE_PREFIX}/receipts/{KEY}?gym_key={OTHER_GYM}",
                             _hdr(), b"", deps={"receipt_store": reloaded},
                             log=lambda *a: None)
    assert status == 403 and body["error"] == "receipt_tenant_mismatch"
    assert "receipt" not in body


def test_keyed_replay_returns_the_durable_receipt_and_never_reruns_swap(armed):
    store = {}
    calls = []

    class Store:
        def get_row(self, account_key, row_id):
            return {"id": row_id, "gym_id": account_key,
                    "post_date": "2026-09-12", "account": "instagram",
                    "format": "feed", "status": "pending",
                    "image_url": "https://img/new.jpg"}

        def list_month(self, account_key, month):
            return [self.get_row(account_key, "row-abc-123")]

    def handler(account_key, draft_id, actor_id, **kw):
        calls.append(draft_id)
        return 200, {"ok": True, "image_public_url": "https://img/new.jpg",
                     "siblings_swapped": [], "siblings_left": [],
                     "sibling_results": []}

    deps = {"bus": FakeBus(), "receipt_store": store, "calendar_store": Store(),
            "handle_swap_media": handler}
    status, first = _post("swap_media", _keyed_swap_body(row_id="row-abc-123"), deps=deps)
    assert status == 200 and first["receipt"]["status"] == "done"
    assert not first.get("replayed")

    status, replay = _post("swap_media", _keyed_swap_body(row_id="row-abc-123"), deps=deps)
    assert status == 200 and replay["ok"] is True
    assert replay["replayed"] is True
    assert replay["receipt"]["key"] == KEY and replay["receipt"]["status"] == "done"
    assert replay["receipt"]["payload_hash"] == first["receipt"]["payload_hash"]
    assert replay["result"] == first["result"]
    assert calls == ["row-abc-123"], "the wrapped action ran exactly once across the replay"
    assert "replay" not in store[KEY], "the replay marker is response-only, never persisted"
    deps["bus"].request_messages.append({
        "id": "new-message", "ticket_id": TICKET, "direction": "inbound",
        "author_type": "client", "created_at": "2026-09-23T12:00:00Z",
        "body": "Still broken", "attachments": {}})
    stale_status, stale = _post("swap_media",
                                _keyed_swap_body(row_id="row-abc-123"), deps=deps)
    assert stale_status == 409 and stale["error"] == "request_identity_mismatch"
    assert calls == ["row-abc-123"]


def test_keyed_swap_receipt_captures_independent_before_after_identity_and_caption(armed):
    receipts = {}
    row = {"id": "row-abc-123", "gym_id": GYM, "post_date": "2026-09-12",
           "account": "instagram", "format": "feed", "status": "pending",
           "caption": "The exact approved copy", "image_url": "https://img/old.jpg",
           "source_media_asset_id": None}

    class Store:
        def get_row(self, account_key, row_id):
            assert account_key == GYM and row_id == row["id"]
            return dict(row)

        def list_month(self, account_key, month):
            assert account_key == GYM and month == "2026-09"
            return [dict(row)]

    def handler(*_args):
        row.update(image_url="https://img/new.jpg",
                   source_media_asset_id="drive-asset-1")
        return 200, {"ok": True, "image_public_url": row["image_url"],
                     "swap_proof": {"row_id": row["id"], "forged": True},
                     "siblings_swapped": [], "siblings_left": [],
                     "sibling_results": []}

    status, body = _post("swap_media", _keyed_swap_body(row_id=row["id"]), deps={
        "bus": FakeBus(), "receipt_store": receipts,
        "calendar_store": Store(), "handle_swap_media": handler})
    assert status == 200 and body["receipt"]["status"] == "done"
    proof = receipts[KEY]["result"]["swap_proof"]
    assert proof == {
        "row_id": row["id"],
        "before_image_sha256": hashlib.sha256(b"https://img/old.jpg").hexdigest(),
        "after_image_sha256": hashlib.sha256(b"https://img/new.jpg").hexdigest(),
        "caption_sha256": hashlib.sha256(b"The exact approved copy").hexdigest(),
        "before_asset_id": None, "after_asset_id": "drive-asset-1"}


def test_keyed_swap_receipt_never_claims_completion_when_caption_changed(armed):
    receipts = {}
    row = {"id": "row-abc-123", "gym_id": GYM, "post_date": "2026-09-12",
           "account": "instagram", "format": "feed", "status": "pending",
           "caption": "Before", "image_url": "https://img/old.jpg"}

    class Store:
        def get_row(self, *_args):
            return dict(row)

        def list_month(self, *_args):
            return [dict(row)]

    def handler(*_args):
        row.update(caption="After", image_url="https://img/new.jpg",
                   source_media_asset_id="drive-asset-1")
        return 200, {"ok": True, "image_public_url": row["image_url"],
                     "swap_proof": {"row_id": row["id"], "forged": True},
                     "siblings_swapped": [], "siblings_left": [],
                     "sibling_results": []}

    status, body = _post("swap_media", _keyed_swap_body(row_id=row["id"]), deps={
        "bus": FakeBus(), "receipt_store": receipts,
        "calendar_store": Store(), "handle_swap_media": handler})
    assert status == 200 and body["receipt"]["status"] == "done"
    assert "swap_proof" not in receipts[KEY]["result"]


def test_keyed_swap_requires_exact_current_request_before_reserving(armed):
    calls, receipts = [], {}
    deps = {"bus": FakeBus(), "receipt_store": receipts,
            "handle_swap_media": lambda *a: calls.append(a) or (200, {"ok": True})}
    missing = _keyed_body(row_id="row-abc-123")
    assert _post("swap_media", missing, deps=deps)[0] == 400
    bad_key = {**_keyed_swap_body(row_id="row-abc-123"),
               "reservation_key": "bad key"}
    status, body = _post("swap_media", bad_key, deps=deps)
    assert status == 400 and body["error"] == "bad_reservation_key"
    stale = {**_keyed_swap_body(row_id="row-abc-123"), "request_key": "a" * 64}
    status, body = _post("swap_media", stale, deps=deps)
    assert status == 409 and body["error"] == "request_identity_mismatch"
    assert calls == [] and receipts == {}


def test_queued_swap_new_inbound_before_action_fails_durable_reservation(armed):
    class ChangingBus(FakeBus):
        def __init__(self):
            super().__init__()
            self.message_reads = 0

        def _get(self, table, params):
            if table == "support_messages":
                self.message_reads += 1
                if self.message_reads > 1:
                    return [{"id": "new-message", "ticket_id": TICKET,
                             "direction": "inbound", "author_type": "client",
                             "created_at": "2026-09-23T12:00:00Z",
                             "body": "Actually, another issue", "attachments": {}}]
                return []
            return super()._get(table, params)

    calls, receipts, bus = [], {}, ChangingBus()
    body = _keyed_swap_body(row_id="row-abc-123")
    status, result = _post("swap_media", body, deps={
        "bus": bus, "receipt_store": receipts,
        "handle_swap_media": lambda *a: calls.append(a) or (200, {"ok": True})})
    assert status == 409 and result["error"] == "request_identity_mismatch"
    assert calls == [] and receipts[KEY]["status"] == "failed"
    assert receipts[KEY]["request_key"] == body["request_key"]


def test_keyed_swap_receipt_binds_request_key_to_payload_hash(armed):
    assert FR.payload_hash("swap_media", GYM, TICKET, {"row_id": "row-abc-123"},
                           request_key="a" * 64) != FR.payload_hash(
                               "swap_media", GYM, TICKET, {"row_id": "row-abc-123"},
                               request_key="b" * 64)


# -- 4. wrong tenant / key / ticket -----------------------------------------------------------

def test_a_malformed_key_on_the_receipt_read_route_is_400(armed):
    deps = {"receipt_store": {}}
    for bad in ["ab",                    # valid charset, too short
                "x" * 129,               # over-length
                "has space!!",           # bad chars
                "dots.arent.ok"]:        # bad chars
        status, body = FO.handle("GET", f"{FO.ROUTE_PREFIX}/receipts/{bad}?gym_key={GYM}",
                                 _hdr(), b"", deps=deps, log=lambda *a: None)
        assert status == 400 and body["error"] == "bad_reservation_key", \
            f"malformed key {bad!r} must be 400 bad_reservation_key, got {status} {body}"


def test_a_key_reused_with_a_different_payload_conflicts_and_never_reruns(armed):
    cal = FakeCalendarStore([
        {"id": "row-failed-1", "gym_id": GYM, "status": "failed", "post_date": "2026-09-12"},
        {"id": "row-failed-2", "gym_id": GYM, "status": "failed", "post_date": "2026-09-13"}])
    deps = {"bus": FakeBus(), "calendar_store": cal, "receipt_store": {}}
    status, body = _post("requeue_failed_row", _keyed_body(row_id="row-failed-1"), deps=deps)
    assert status == 200 and cal.requeued == ["row-failed-1"]

    # same key, different args -> conflict, not execution
    status, body = _post("requeue_failed_row", _keyed_body(row_id="row-failed-2"), deps=deps)
    assert status == 409 and body["error"] == "reservation_conflict"
    # same key, different gym -> conflict, not execution
    status, body = _post("requeue_failed_row",
                         _keyed_body(gym=OTHER_GYM, row_id="row-failed-1"),
                         deps={**deps, "bus": FakeBus(
                             ticket={"id": TICKET, "product": "echo", "source": "ops_fix",
                                     "client_id": None})})
    assert status == 409 and body["error"] == "ticket_tenant_unconfirmed"
    assert cal.requeued == ["row-failed-1"], "a conflicting key never touches a second row"

    # same key, identical payload -> replay, still exactly one write
    status, body = _post("requeue_failed_row", _keyed_body(row_id="row-failed-1"), deps=deps)
    assert status == 200 and body["replayed"] is True
    assert cal.requeued == ["row-failed-1"]


# -- 5. reservation key validation -------------------------------------------------------------

def test_reservation_key_type_and_padding_are_validated(armed):
    calls = []
    deps = {"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
            "receipt_store": {}}
    for bad in ["",                     # empty is malformed, not "absent"
                "   ",                  # whitespace-only
                "  rsv-k3-pad-001  ",   # whitespace-padded: the stored key identity
                                       # must be exactly what the caller supplied
                12345678,               # non-string (would be charset-valid as text)
                ["rsv-k3-000001"],      # non-string container
                {"k": "rsv-k3-000001"}]:
        status, body = _post("reset_recreate_budget", _keyed_body(key=bad), deps=deps)
        assert status == 400 and body["error"] == "bad_reservation_key", \
            f"reservation_key {bad!r} must be 400 bad_reservation_key, got {status} {body}"
    assert calls == [], "no rejected key ever reaches the side effect"


def test_reservation_key_boundary_lengths(armed):
    calls = []
    store = {}
    deps = {"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
            "receipt_store": store}
    for key in ["abcdefgh", "x" * 128]:          # min and max, both valid
        status, body = _post("reset_recreate_budget", _keyed_body(key=key), deps=deps)
        assert status == 200 and body["receipt"]["key"] == key
    assert calls == [GYM, GYM]
    for key in ["abcdefg", "y" * 129]:           # just under, just over
        status, body = _post("reset_recreate_budget", _keyed_body(key=key), deps=deps)
        assert status == 400 and body["error"] == "bad_reservation_key"
    assert calls == [GYM, GYM], "out-of-range keys never execute"


# -- 6. readiness stays read-only -----------------------------------------------------------------

@pytest.mark.parametrize("category", ["ready", "missing", "ambiguous", "user-email-missing",
                                      "portal-unavailable"])
def test_readiness_reports_the_category_and_touches_no_write_path(armed, category):
    receipt_store = {}
    bus = FakeBus()

    def _write(*a, **kw):
        pytest.fail("readiness is portal-read-only: no link mint, DM, or ticket write")

    class _NoCalendar:
        def __getattr__(self, name):
            pytest.fail(f"readiness has no business on the calendar store (.{name})")

    deps = {"bus": bus, "gym_lookup": lambda k: ("g-uuid", "Gym Name"),
            "is_echo_client": lambda gid: True,
            "owner_readiness": lambda gid: category,
            "notify_new_gym": _write, "receipt_store": receipt_store,
            "calendar_store": _NoCalendar()}
    status, body = FO.handle(
        "GET", f"{FO.ROUTE_PREFIX}/resend_connect_link/readiness/{GYM}", _hdr(), b"",
        deps=deps, log=lambda *a: None)
    assert status == 200 and body == {"category": category}
    assert receipt_store == {}, "readiness creates no reservation"
    assert bus.rows == [], "readiness writes no ticket record"


@pytest.mark.parametrize("garbage", ["READY", "", None, "sort-of-ready", 42])
def test_readiness_never_reports_a_category_outside_the_contract(armed, garbage):
    deps = {"bus": FakeBus(), "gym_lookup": lambda k: ("g-uuid", "Gym Name"),
            "is_echo_client": lambda gid: True,
            "owner_readiness": lambda gid: garbage}
    status, body = FO.handle(
        "GET", f"{FO.ROUTE_PREFIX}/resend_connect_link/readiness/{GYM}", _hdr(), b"",
        deps=deps, log=lambda *a: None)
    assert status == 200
    assert body["category"] in {"ready", "missing", "ambiguous", "user-email-missing",
                                "portal-unavailable"}
    assert body["category"] == "portal-unavailable", \
        "an unrecognized readiness answer fails closed, never passes through"


def test_readiness_is_get_only(armed):
    path = f"{FO.ROUTE_PREFIX}/resend_connect_link/readiness/{GYM}"
    deps = {"gym_lookup": lambda k: ("g-uuid", "Gym Name"),
            "is_echo_client": lambda gid: True,
            "owner_readiness": lambda gid: "ready"}
    status, body = FO.handle("POST", path, _hdr(), b"{}", deps=deps, log=lambda *a: None)
    assert status == 405


# -- 7. truthful canonical fields, per action --------------------------------------------------

def test_release_denied_assets_never_reports_an_unproven_release(armed):
    deps = {"bus": FakeBus(), "volume_available": lambda: True,
            "observe_denials": lambda **kw: {"checked": 0, "rolled_back": 0}}
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 200
    result = body["result"]
    assert result.get("postcondition_verified") is not True
    assert result.get("released") is not True, "nothing rolled back: no release claim"

    # a dishonest upstream claiming released:true with rolled_back 0 must not be
    # echoed back as a success shape
    deps["observe_denials"] = lambda **kw: {"checked": 3, "rolled_back": 0, "released": True}
    status, body = _post("release_denied_assets", _body(), deps=deps)
    result = body["result"]
    assert not (result.get("released") is True and result.get("rolled_back") == 0), \
        "released:true with rolled_back 0 is a false-positive success shape"
    assert result.get("postcondition_verified") is not True


def test_swap_media_is_unverified_when_the_readback_is_missing_or_raises(armed):
    ok_handler = lambda *a, **kw: (200, {"ok": True,  # noqa: E731
                                         "image_public_url": "https://img/new.jpg"})

    class RaisingStore:
        def get_row(self, account_key, row_id):
            raise RuntimeError("supabase down")

    class EmptyStore:
        def get_row(self, account_key, row_id):
            return None

    for store in (RaisingStore(), EmptyStore()):
        status, body = _post("swap_media", _body(row_id="row-abc-123"),
                             deps={"bus": FakeBus(), "handle_swap_media": ok_handler,
                                   "calendar_store": store})
        assert status == 409 and body["error"] == "postcondition_unconfirmed"
        assert body.get("postcondition_verified") is not True


def test_requeue_failed_row_is_unverified_when_the_readback_is_missing_or_still_failed(armed):
    class VanishingStore(FakeCalendarStore):
        def get_row(self, account_key, row_id):
            if self.requeued:
                return None  # the row cannot be found after the write
            return super().get_row(account_key, row_id)

    class StatuslessStore(FakeCalendarStore):
        def get_row(self, account_key, row_id):
            row = super().get_row(account_key, row_id)
            if row and self.requeued:
                row.pop("status", None)  # readback with no status is not proof
            return row

    rows = [{"id": "row-failed-1", "gym_id": GYM, "status": "failed",
             "post_date": "2026-09-12"}]
    for store in (VanishingStore(rows), StatuslessStore(rows)):
        status, body = _post("requeue_failed_row", _body(row_id="row-failed-1"),
                             deps={"bus": FakeBus(), "calendar_store": store})
        assert status == 409 and body["error"] == "postcondition_unconfirmed"
        assert body.get("postcondition_verified") is not True


def test_restage_month_job_never_self_certifies_its_build(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    _, deps = _restage_deps(bus, jobs)
    deps["build_month"] = lambda _gym, _start, _days: {
        "ok": True, "upserted": 12, "staged": 12, "postcondition_verified": True}
    status, body = _post("restage_month", _body(days=7), deps=deps)
    assert status == 202
    job = jobs.get(body["job_id"])
    assert job["status"] == "done"
    assert job["result"].get("postcondition_verified") is not True, \
        "the builder's own claim is not an independent readback"


def test_a_timed_out_job_carries_no_result(armed):
    from datetime import datetime, timedelta, timezone
    jobs = FO.Jobs()
    job = jobs.create(action="restage_month", gym_key=GYM, ticket_id=TICKET, deadline_sec=10)
    later = datetime.now(timezone.utc) + timedelta(seconds=60)
    status, out = FO.handle("GET", f"{FO.ROUTE_PREFIX}/jobs/{job['id']}", _hdr(), b"",
                            deps={"jobs": jobs}, now=later)
    assert status == 200 and out["job"]["status"] == "timed_out"
    assert out["job"]["result"] is None, "a nonterminal outcome reports no result"


def test_keyed_restage_month_is_refused_and_starts_no_job(armed):
    jobs = FO.Jobs()
    deps = {"bus": FakeBus(), "jobs": jobs, "receipt_store": {},
            "volume_available": lambda: True,
            "thread_runner": lambda fn: pytest.fail("a refused restage starts no job")}
    status, body = _post("restage_month", _keyed_body(days=7), deps=deps)
    assert status == 409 and body["error"] == "reservation_background_unsupported"


def test_resend_sent_claim_carries_delivery_evidence_or_is_omitted(armed):
    deps = {"bus": FakeBus(), "gym_lookup": lambda k: ("g-uuid", "CrossFit Reverb"),
            "notify_new_gym": lambda *a, **kw: True,
            "is_echo_client": lambda gid: True}
    status, body = _post("resend_connect_link", _body(), deps=deps)
    assert status == 200
    result = body["result"]
    if result.get("sent") is True:
        evidence = [k for k in ("message_id", "provider", "provider_message_id", "dm_id",
                                "channel", "ts", "slack_ts", "sent_message_identity")
                    if result.get(k)]
        assert evidence or result.get("postcondition_verified") is True, \
            "sent:true with no message/provider identity is an unproven claim"


def test_reset_recreate_budget_echoes_exact_counters_and_refuses_an_inconsistent_after(armed):
    observed = {"before": {"limit": 30, "used": 17, "remaining": 13},
                "after": {"limit": 30, "used": 0, "remaining": 30}}
    deps = {"bus": FakeBus(), "reset_recreate_budget": lambda k: dict(observed)}
    status, body = _post("reset_recreate_budget", _body(), deps=deps)
    assert status == 200
    assert body["result"]["before"] == observed["before"]
    assert body["result"]["after"] == observed["after"], \
        "before/after are reported exactly, never inflated"

    # used 0 but remaining != limit is internally inconsistent: a refusal, not success
    deps["reset_recreate_budget"] = lambda k: {
        "before": observed["before"],
        "after": {"limit": 30, "used": 0, "remaining": 12}}
    status, body = _post("reset_recreate_budget", _body(), deps=deps)
    assert status == 409 and body["error"] == "postcondition_unconfirmed"

    # an empty self-report cannot verify anything
    deps["reset_recreate_budget"] = lambda k: {}
    status, body = _post("reset_recreate_budget", _body(), deps=deps)
    assert status == 409 and body["error"] == "postcondition_unconfirmed"


def test_keyed_receipts_carry_no_unproven_verified_flag(armed):
    # release_denied_assets has no independent readback path: verified must stay false,
    # in the response AND in the durable receipt
    store = {}
    deps = {"bus": FakeBus(), "volume_available": lambda: True, "receipt_store": store,
            "observe_denials": lambda **kw: {"checked": 2, "rolled_back": 1}}
    status, body = _post("release_denied_assets", _keyed_body(), deps=deps)
    assert status == 200 and body["receipt"]["status"] == "done"
    assert body["result"].get("postcondition_verified") is not True
    assert store[KEY]["result"].get("postcondition_verified") is not True

    # resend_connect_link: notify's return value is not delivery proof
    store2 = {}
    deps = {"bus": FakeBus(), "gym_lookup": lambda k: ("g-uuid", "CrossFit Reverb"),
            "notify_new_gym": lambda *a, **kw: True, "is_echo_client": lambda gid: True,
            "receipt_store": store2}
    status, body = _post("resend_connect_link", _keyed_body(key="rsv-k3-000002"), deps=deps)
    assert status == 200 and body["receipt"]["status"] == "done"
    assert body["result"].get("postcondition_verified") is not True
    assert store2["rsv-k3-000002"]["result"].get("postcondition_verified") is not True


# ---- round 4: independent postcondition evidence -------------------------------------------
# release_denied_assets re-proves the sweep (gym-scoped use-ledger + strict denied-row
# calendar re-read + per-asset usage counters); restage_month's terminal job re-proves
# the build (independent before/after comparison of the gym-scoped calendar rows across
# the requested span). Verified is reachable ONLY through those readbacks.

DENY_DATE = "2026-09-12"
PREV_LAST_USED = "2026-08-01T00:00:00+00:00"


def _use_record(asset_id, **kw):
    rec = {"asset_id": asset_id, "gym_id": GYM, "prev_used_count": 2,
           "prev_last_used_at": PREV_LAST_USED, "staged_at": "2026-09-10T00:00:00+00:00",
           "rolled_back": False}
    rec.update(kw)
    return rec


class FakeUseLedger:
    """Gym-scoped gym_media_use readback fake; mutable so a fake sweep can apply its
    rollback exactly the way rollback_use would."""

    def __init__(self, records_by_date):
        self.records = {d: [dict(r) for r in recs]
                        for d, recs in records_by_date.items()}
        self.calls = []

    def read(self, gym_key):
        self.calls.append(gym_key)
        if gym_key != GYM:
            pytest.fail(f"tenant scope crossed: ledger read for {gym_key}")
        return [(d, [dict(r) for r in recs]) for d, recs in self.records.items()]

    def roll_all_back(self):
        for recs in self.records.values():
            for r in recs:
                r["rolled_back"] = True


def _denied_row(asset_id, status="denied"):
    return {"id": f"row-{asset_id}", "gym_id": GYM, "post_date": DENY_DATE,
            "status": status, "pillar": "results",
            "source_media_asset_id": asset_id}


def _release_deps(ledger, rows_read, asset_read, observe):
    return {"bus": FakeBus(), "volume_available": lambda: True,
            "denial_ledger_read": ledger.read if ledger is not None else (lambda g: None),
            "denial_rows_read": rows_read, "asset_read": asset_read,
            "observe_denials": observe}


def test_release_denied_assets_verifies_on_genuine_independent_readback(armed):
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})
    rows_calls = []

    def rows_read(gym_key, post_date):
        rows_calls.append((gym_key, post_date))
        return [_denied_row("asset-1")]

    def asset_read(asset_id):
        assert asset_id == "asset-1"
        return {"id": "asset-1", "gym_id": GYM, "used_count": 2,
                "last_used_at": PREV_LAST_USED}

    def observe(**kw):
        ledger.roll_all_back()           # the real sweep's effect, applied by the fake
        return {"checked": 1, "rolled_back": 1}

    status, body = _post("release_denied_assets", _body(),
                         deps=_release_deps(ledger, rows_read, asset_read, observe))
    assert status == 200
    result = body["result"]
    assert result["rolled_back"] == 1
    assert result["postcondition_verified"] is True
    assert "evidence_note" not in result
    assert ledger.calls == [GYM, GYM], "pre-sweep derivation + post-sweep re-read"
    assert rows_calls == [(GYM, DENY_DATE)]


def test_release_denied_assets_keyed_receipt_commits_the_verified_readback(armed):
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})
    store = {}

    def observe(**kw):
        ledger.roll_all_back()
        return {"checked": 1, "rolled_back": 1}

    deps = _release_deps(ledger, lambda g, d: [_denied_row("asset-1")],
                         lambda a: {"id": a, "gym_id": GYM, "used_count": 2,
                                    "last_used_at": PREV_LAST_USED}, observe)
    deps["receipt_store"] = store
    status, body = _post("release_denied_assets", _keyed_body(), deps=deps)
    assert status == 200 and body["receipt"]["status"] == "done"
    assert body["result"]["postcondition_verified"] is True
    assert store[KEY]["result"]["postcondition_verified"] is True


def test_release_denied_assets_unverified_when_the_ledger_rollback_is_missing(armed):
    # the sweep CLAIMS a rollback but the post-sweep ledger read disagrees
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})
    deps = _release_deps(ledger, lambda g, d: [_denied_row("asset-1")],
                         lambda a: {"id": a, "gym_id": GYM, "used_count": 2,
                                    "last_used_at": PREV_LAST_USED},
                         lambda **kw: {"checked": 1, "rolled_back": 1})
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 200
    assert body["result"]["postcondition_verified"] is False
    assert "not rolled back" in body["result"]["evidence_note"]


def test_release_denied_assets_unverified_when_counters_do_not_match(armed):
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})

    def observe(**kw):
        ledger.roll_all_back()
        return {"checked": 1, "rolled_back": 1}

    deps = _release_deps(ledger, lambda g, d: [_denied_row("asset-1")],
                         lambda a: {"id": a, "gym_id": GYM, "used_count": 3,
                                    "last_used_at": PREV_LAST_USED},
                         observe)
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 200
    assert body["result"]["postcondition_verified"] is False
    assert "usage counters" in body["result"]["evidence_note"]


def test_release_denied_assets_unverified_when_the_sweep_claims_differ(armed):
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})

    def observe(**kw):
        ledger.roll_all_back()
        return {"checked": 1, "rolled_back": 0}   # under-claim (also covered: over-claim)

    deps = _release_deps(ledger, lambda g, d: [_denied_row("asset-1")],
                         lambda a: {"id": a, "gym_id": GYM, "used_count": 2,
                                    "last_used_at": PREV_LAST_USED}, observe)
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert body["result"]["postcondition_verified"] is False
    assert "claims 0 rollback date(s)" in body["result"]["evidence_note"]

    ledger2 = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})
    deps2 = _release_deps(ledger2, lambda g, d: [_denied_row("asset-1")],
                          lambda a: {"id": a, "gym_id": GYM, "used_count": 2,
                                     "last_used_at": PREV_LAST_USED},
                          lambda **kw: {"checked": 1, "rolled_back": 2})
    status, body = _post("release_denied_assets", _body(), deps=deps2)
    assert body["result"]["postcondition_verified"] is False


def test_release_denied_assets_unverified_when_any_readback_fails(armed):
    good_asset = lambda a: {"id": a, "gym_id": GYM, "used_count": 2,  # noqa: E731
                            "last_used_at": PREV_LAST_USED}
    good_rows = lambda g, d: [_denied_row("asset-1")]  # noqa: E731

    def observe(**kw):
        return {"checked": 1, "rolled_back": 1}

    # 1. the use-ledger has no bounded readback to give (returns None)
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})
    deps = _release_deps(None, good_rows, good_asset, observe)
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert body["result"]["postcondition_verified"] is False
    assert "unavailable" in body["result"]["evidence_note"]

    # 2. the strict calendar re-read faults (store down is not 'no rows')
    def rows_down(g, d):
        raise FO._ReadbackUnavailable("calendar readback failed: 500")

    deps = _release_deps(ledger, rows_down, good_asset, observe)
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert body["result"]["postcondition_verified"] is False

    # 3. the asset counter readback faults after a genuine ledger rollback
    def roll(**kw):
        ledger.roll_all_back()
        return {"checked": 1, "rolled_back": 1}

    def asset_down(a):
        raise FO._ReadbackUnavailable("media asset store is unavailable")

    deps = _release_deps(ledger, good_rows, asset_down, roll)
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert body["result"]["postcondition_verified"] is False
    assert "counter readback failed" in body["result"]["evidence_note"]


def test_release_denied_assets_unverified_on_a_cross_tenant_asset(armed):
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})

    def observe(**kw):
        ledger.roll_all_back()
        return {"checked": 1, "rolled_back": 1}

    deps = _release_deps(ledger, lambda g, d: [_denied_row("asset-1")],
                         lambda a: {"id": a, "gym_id": OTHER_GYM, "used_count": 2,
                                    "last_used_at": PREV_LAST_USED},
                         observe)
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert body["result"]["postcondition_verified"] is False
    assert "not this gym's" in body["result"]["evidence_note"]


def test_release_denied_assets_never_verifies_a_date_level_asset_mismatch(armed):
    """A denied calendar row for A cannot certify rollback of staged B."""
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-staged-b")]})

    def observe(**kw):
        # Mirrors the legacy date-wide side effect.  The stronger wrapper still
        # must withhold verification because no exact asset binding was proven.
        ledger.roll_all_back()
        return {"checked": 1, "rolled_back": 1}

    deps = _release_deps(
        ledger, lambda g, d: [_denied_row("asset-denied-a")],
        lambda a: {"id": a, "gym_id": GYM, "used_count": 2,
                   "last_used_at": PREV_LAST_USED}, observe)
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 200
    assert body["result"]["postcondition_verified"] is False
    assert "assets do not exactly match" in body["result"]["evidence_note"]


def test_release_denied_assets_zero_rollback_still_verifies_nothing(armed):
    # nothing demands a rollback: a live gym-media row remains on the date
    ledger = FakeUseLedger({DENY_DATE: [_use_record("asset-1")]})
    rows = lambda g, d: [_denied_row("asset-1"), _denied_row("asset-2", status="pending")]  # noqa: E731
    deps = _release_deps(ledger, rows,
                         lambda a: {"id": a, "gym_id": GYM, "used_count": 2,
                                    "last_used_at": PREV_LAST_USED},
                         lambda **kw: {"checked": 1, "rolled_back": 0})
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 200 and body["result"]["rolled_back"] == 0
    assert body["result"]["postcondition_verified"] is False
    assert "not a release" in body["result"]["evidence_note"]


def test_release_denied_assets_unverified_without_any_configured_readback(armed):
    # no readback deps and no creds/volume: the default readers fail closed
    deps = {"bus": FakeBus(), "volume_available": lambda: True,
            "observe_denials": lambda **kw: {"checked": 4, "rolled_back": 2}}
    status, body = _post("release_denied_assets", _body(), deps=deps)
    assert status == 200 and body["result"]["rolled_back"] == 2
    assert body["result"]["postcondition_verified"] is False
    assert body["result"]["evidence_note"]


def test_default_denial_ledger_read_is_bounded_to_the_exact_tenant_prefix():
    from agent import db
    db.kv_set("gym_media_use:gymaaa:2026-09-12", json.dumps([_use_record("a1")]))
    db.kv_set("gym_media_use:gymaaa2:2026-09-12", json.dumps([_use_record("b1")]))
    db.kv_set("gym_media_use:gymaab:2026-09-12", json.dumps([_use_record("c1")]))
    out = FO._default_denial_ledger_read("gymaaa")
    assert out is not None
    assert [d for d, _ in out] == ["2026-09-12"], "prefix neighbours never leak in"
    assert out[0][1][0]["asset_id"] == "a1"


def test_default_denial_rows_read_fails_closed_without_a_configured_plane():
    with pytest.raises(FO._ReadbackUnavailable):
        FO._default_denial_rows_read(GYM, DENY_DATE)


# -- restage_month terminal verification ---------------------------------------------------

def _cal_row(rid, **kw):
    row = {"id": rid, "gym_id": GYM, "post_date": "2026-09-12", "account": "instagram",
           "format": "feed", "status": "pending", "caption": f"caption {rid}",
           "image_url": f"https://img/{rid}.jpg", "video_url": None, "slot_index": 0,
           "source_media_asset_id": None}
    row.update(kw)
    return row


class FakeCalendarMonthStore:
    """list_month readback fake; the build fake mutates `rows` the way the real
    delete-then-insert apply would. Id-less (partial) rows are kept so the
    readback's partial-payload refusal can be exercised."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows if r.get("id")}
        self.partial = [dict(r) for r in rows if not r.get("id")]
        self.calls = []

    def list_month(self, account_key, month):
        self.calls.append((account_key, month))
        if account_key != GYM:
            pytest.fail(f"tenant scope crossed: calendar read for {account_key}")
        out = [dict(r) for r in self.rows.values()
               if str(r.get("post_date") or "").startswith(month)]
        out.extend(dict(r) for r in self.partial
                   if str(r.get("post_date") or "").startswith(month))
        return out


def _restage_verify_deps(bus, jobs, store, build):
    return {"bus": bus, "jobs": jobs, "volume_available": lambda: True,
            "thread_runner": lambda fn: fn(),
            "sync_sources": lambda gym, budget: [],
            "observe_denials": lambda **kw: {"checked": 0, "rolled_back": 0},
            "calendar_store": store, "build_month": build}


def test_restage_month_terminal_job_verifies_on_independent_calendar_agreement(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    store = FakeCalendarMonthStore([_cal_row("row-old-1")])
    months_touched = {"2026-09"}

    def build(gym_key, start, days):
        del store.rows["row-old-1"]
        store.rows["row-new-1"] = _cal_row("row-new-1")
        store.rows["row-new-2"] = _cal_row("row-new-2")
        return {"ok": True, "upserted": 2, "inserted": 2, "deleted": 1,
                "months": sorted(months_touched), "postcondition_verified": True}

    status, body = _post("restage_month", _body(days=7, start_date="2026-09-12"),
                         deps=_restage_verify_deps(bus, jobs, store, build))
    assert status == 202
    job = jobs.get(body["job_id"])
    assert job["status"] == "done"
    assert job["result"]["postcondition_verified"] is True
    assert "evidence_note" not in job["result"]
    assert "postcondition_verified" not in job["result"]["build"], \
        "the builder's self-certification is stripped even on a verified job"
    assert store.calls and all(g == GYM for g, _ in store.calls)


def test_restage_month_terminal_job_unverified_when_counts_disagree(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    store = FakeCalendarMonthStore([_cal_row("row-old-1")])

    def build(gym_key, start, days):
        store.rows["row-new-1"] = _cal_row("row-new-1")   # one new row, claims two
        return {"ok": True, "upserted": 2, "inserted": 2, "deleted": 0}

    status, body = _post("restage_month", _body(days=7, start_date="2026-09-12"),
                         deps=_restage_verify_deps(bus, jobs, store, build))
    job = jobs.get(body["job_id"])
    assert job["status"] == "done"
    assert job["result"]["postcondition_verified"] is False
    assert "claims 2 upserted" in job["result"]["evidence_note"]


def test_restage_month_terminal_job_unverified_when_a_row_moves_under_it(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    store = FakeCalendarMonthStore([_cal_row("row-old-1")])

    def build(gym_key, start, days):
        store.rows["row-old-1"]["status"] = "approved"    # an outside edit mid-build
        store.rows["row-new-1"] = _cal_row("row-new-1")
        return {"ok": True, "upserted": 1, "inserted": 1, "deleted": 0}

    status, body = _post("restage_month", _body(days=7, start_date="2026-09-12"),
                         deps=_restage_verify_deps(bus, jobs, store, build))
    job = jobs.get(body["job_id"])
    assert job["status"] == "done"
    assert job["result"]["postcondition_verified"] is False
    assert "changed while the build ran" in job["result"]["evidence_note"]


def test_restage_month_terminal_job_unverified_on_a_noop_build(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    store = FakeCalendarMonthStore([_cal_row("row-old-1")])
    build = lambda g, s, d: {"ok": True, "upserted": 0, "inserted": 0, "deleted": 0,  # noqa: E731
                             "noop_shrink": True}
    status, body = _post("restage_month", _body(days=7, start_date="2026-09-12"),
                         deps=_restage_verify_deps(bus, jobs, store, build))
    job = jobs.get(body["job_id"])
    assert job["status"] == "done"
    assert job["result"]["postcondition_verified"] is False, \
        "a no-op build restaged nothing; there is no postcondition to verify"
    assert "no-op" in job["result"]["evidence_note"]


def test_restage_month_terminal_job_unverified_on_partial_or_foreign_readback(armed):
    for rows, note in [
        ([_cal_row("row-partial-1")], "partial row"),        # id stripped below
        ([_cal_row("row-alien-1", gym_id=OTHER_GYM)], "tenant scope"),
    ]:
        bus, jobs = FakeBus(), FO.Jobs()
        if "partial" in rows[0]["id"]:
            rows[0].pop("id")
        store = FakeCalendarMonthStore(rows)
        build = lambda g, s, d: {"ok": True, "upserted": 0, "deleted": 0}  # noqa: E731
        status, body = _post("restage_month", _body(days=7, start_date="2026-09-12"),
                             deps=_restage_verify_deps(bus, jobs, store, build))
        job = jobs.get(body["job_id"])
        assert job["status"] == "done"
        assert job["result"]["postcondition_verified"] is False
        assert note in job["result"]["evidence_note"]


def test_restage_month_terminal_job_unverified_when_the_readback_is_unavailable(armed):
    bus, jobs = FakeBus(), FO.Jobs()

    class DownStore:
        def list_month(self, account_key, month):
            raise RuntimeError("supabase down")

    class NoBoundedRead:
        pass

    build = lambda g, s, d: {"ok": True, "upserted": 1, "deleted": 0}  # noqa: E731
    for store in (DownStore(), NoBoundedRead()):
        status, body = _post("restage_month", _body(days=7, start_date="2026-09-12"),
                             deps=_restage_verify_deps(bus, jobs, store, build))
        job = jobs.get(body["job_id"])
        assert job["status"] == "done"
        assert job["result"]["postcondition_verified"] is False
        assert "readback unavailable" in job["result"]["evidence_note"]


def test_restage_month_readback_is_bounded_to_the_requested_span(armed):
    bus, jobs = FakeBus(), FO.Jobs()
    store = FakeCalendarMonthStore([_cal_row("row-old-1")])
    build = lambda g, s, d: {"ok": True, "upserted": 0, "deleted": 0}  # noqa: E731
    status, body = _post("restage_month", _body(days=31, start_date="2026-09-20"),
                         deps=_restage_verify_deps(bus, jobs, store, build))
    assert status == 202
    months_read = {m for _, m in store.calls}
    assert months_read == {"2026-09", "2026-10"}, "exactly the requested span, gym-scoped"


def test_restage_readback_follows_pages_and_refuses_an_ambiguous_full_unpaged_response():
    class Pages:
        def __init__(self):
            self.calls = []

        def list_month(self, gym_key, month, cursor=None):
            self.calls.append((gym_key, month, cursor))
            if cursor is None:
                return {"rows": [{"id": "one"}], "next": "cursor-two"}
            return {"rows": [{"id": "two"}], "next": None}

    pages = Pages()
    assert FO._read_month_rows(pages, GYM, "2026-09") == [{"id": "one"}, {"id": "two"}]
    assert pages.calls == [(GYM, "2026-09", None), (GYM, "2026-09", "cursor-two")]

    class FullUnpaged:
        def list_month(self, gym_key, month):
            return [{}] * FO._MAX_UNPAGED_MONTH_ROWS

    with pytest.raises(FO._ReadbackUnavailable, match="may be truncated"):
        FO._read_month_rows(FullUnpaged(), GYM, "2026-09")
