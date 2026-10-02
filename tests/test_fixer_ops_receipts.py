"""Hostile tests for the ops-action reservation/receipt lane.

Contract under test (shared, do not rename anything):

  agent/fixer_ops_receipts.py
    ReceiptError(code, status) with .code / .status
    payload_hash(action, gym_key, ticket_id, args) -> sha256 of canonical json
    begin(store, key, action, gym_key, ticket_id, args) -> receipt (idempotent replay,
        reservation_conflict 409 on a different payload, reservation_outcome_unknown 409
        ALWAYS for an unknown-outcome receipt, bad_reservation_key 400 on a bad key,
        store_unavailable 503 when the store raises)
    commit(store, key, result) / fail(store, key, error) / mark_unknown(store, key, detail)
    get_receipt(store, key, gym_key) -> receipt (unknown_receipt 404,
        receipt_tenant_mismatch 403; tenant-bound)

  agent/fixer_ops.py
    POST /ops/actions/<action> accepts an optional body field "reservation_key":
    begin() before any side effect, replay returns the stored result without
    re-executing (200, replayed=true), commit() on success, fail() on a 409-class
    refusal, mark_unknown() on an undetermined outcome. deps["receipt_store"] is the
    injectable store. GET /ops/actions/receipts/<key>?gym_key=<gym> under the same
    X-Fixer-Ops-Secret auth.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import fixer_ops as FO  # noqa: E402
from agent import fixer_ops_receipts as FR  # noqa: E402

SECRET = "s3cr3t-" * 4
GYM = "crossfitreverb30b5b2"
OTHER_GYM = "anothergym999"
TICKET = "27728832-ae54-428c-af8b-0cb5c8ca1666"
TICKET_B = "ticket-b-9999"
KEY = "rsv-test-0001"


def _ops_ticket(ticket_id=TICKET, gym_key=GYM):
    """Clientless ops ticket carrying both persisted and raw tenant evidence."""
    return {
        "id": ticket_id,
        "product": "echo",
        "source": "ops_fix",
        "client_id": None,
        "raw_text": f"OPS-FIX REQUEST: ECHO ALERT for {gym_key}",
        "verification_before": {"fixer": {"triage": {"gym_key": gym_key}}},
    }


class FakeBus:
    def __init__(self, ticket=None, tokens=None):
        self.rows = []
        self.ticket_row = ticket if ticket is not None else _ops_ticket()
        self.token_rows = tokens if tokens is not None else []

    def _get(self, table, params):
        if table == "support_tickets":
            return [self.ticket_row] if self.ticket_row and params["id"] == f"eq.{TICKET}" else []
        if table == "echo_intake_tokens":
            return self.token_rows
        if table == "support_messages":
            return []
        raise AssertionError(f"unexpected table: {table}")

    def record_outbound(self, **kw):
        self.rows.append(kw)
        return {"id": f"m-{len(self.rows)}", **kw}


class AnyTicketBus(FakeBus):
    """ops_fix-lane ticket for ANY ticket id, so tenant binding passes and the
    reservation layer is what refuses."""

    def _get(self, table, params):
        if table == "support_tickets":
            return [_ops_ticket(params["id"][len("eq."):])]
        return super()._get(table, params)


class ReceiptStore(dict):
    """Thread-safe in-memory stand-in for the kv/sqlite store. Truthy even when empty
    so `deps.get("receipt_store") or default_store()` never falls through to sqlite."""

    def __init__(self, delay=0.0):
        super().__init__()
        self._lock = threading.Lock()
        self._delay = delay

    def __bool__(self):
        return True

    def get(self, k, default=None):
        with self._lock:
            if self._delay:
                time.sleep(self._delay)  # widen any check-then-act race window
            return super().get(k, default)

    def __getitem__(self, k):
        with self._lock:
            if self._delay:
                time.sleep(self._delay)
            return super().__getitem__(k)

    def __setitem__(self, k, v):
        with self._lock:
            super().__setitem__(k, v)

    def __contains__(self, k):
        with self._lock:
            if self._delay:
                time.sleep(self._delay)
            return super().__contains__(k)

    def setdefault(self, k, default=None):
        with self._lock:
            return super().setdefault(k, default)


class ExplodingStore:
    def _boom(self, *a, **kw):
        raise RuntimeError("kv down")

    get = __getitem__ = __setitem__ = __contains__ = setdefault = pop = _boom


def _hdr(secret=SECRET):
    h = {FO.HEADER: secret} if secret is not None else {}
    return lambda k, d=None: h.get(k, d)


def _post(action, body, *, deps=None, secret=SECRET, logs=None):
    return FO.handle("POST", f"{FO.ROUTE_PREFIX}/{action}", _hdr(secret),
                     json.dumps(body).encode(), deps=deps or {},
                     log=(logs.append if logs is not None else (lambda *a: None)))


def _get_receipt(key, gym=GYM, *, deps=None, secret=SECRET):
    return FO.handle("GET", f"{FO.ROUTE_PREFIX}/receipts/{key}?gym_key={gym}",
                     _hdr(secret), b"", deps=deps or {}, log=lambda *a: None)


def _body(reservation_key=None, gym=GYM, ticket=TICKET, **args):
    b = {"gym_key": gym, "ticket_id": ticket, "args": args}
    if reservation_key is not None:
        b["reservation_key"] = reservation_key
    return b


def _ok_reset(calls):
    def reset(key):
        calls.append(key)
        return {"before": {"limit": 30, "used": 30, "remaining": 0},
                "after": {"limit": 30, "used": 0, "remaining": 30}}
    return reset


def _reserved(store, key=KEY, action="reset_recreate_budget", gym=GYM, ticket=TICKET,
              args=None):
    return FR.begin(store, key, action, gym, ticket, {} if args is None else args)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv(FO.SECRET_ENV, SECRET)


# ---- module: ReceiptError + payload_hash -------------------------------------------------

def test_receipt_error_shape():
    e = FR.ReceiptError("reservation_conflict", 409)
    assert isinstance(e, Exception)
    assert e.code == "reservation_conflict" and e.status == 409


def test_payload_hash_is_canonical_json():
    action, gym, ticket = "reset_recreate_budget", GYM, TICKET
    args = {"b": 1, "a": {"y": [2, 3], "x": "café"}}
    expected = hashlib.sha256(json.dumps(
        {"action": action, "gym_key": gym, "ticket_id": ticket, "args": args},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    assert FR.payload_hash(action, gym, ticket, args) == expected
    # key order inside args is irrelevant
    assert FR.payload_hash(action, gym, ticket, {"a": {"x": "café", "y": [2, 3]}, "b": 1}) == expected
    # every field is bound into the hash
    other = FR.payload_hash(action, gym, ticket, {"b": 1, "a": {"y": [2, 3], "x": " Café"}})
    assert other != expected
    assert FR.payload_hash(action, OTHER_GYM, ticket, args) != expected
    assert FR.payload_hash(action, gym, TICKET_B, args) != expected
    assert FR.payload_hash("swap_media", gym, ticket, args) != expected


# ---- module: begin / commit / fail / mark_unknown ----------------------------------------

def test_begin_creates_a_reserved_receipt():
    store = {}
    r = _reserved(store, args={"row_id": "r1"})
    assert set(r) == {"schema_version", "key", "action", "gym_key", "ticket_id",
                      "payload_hash", "status", "created_at", "result", "error",
                      "finished_at"}
    assert r["schema_version"] == 1
    assert r["key"] == KEY and r["action"] == "reset_recreate_budget"
    assert r["gym_key"] == GYM and r["ticket_id"] == TICKET
    assert r["payload_hash"] == FR.payload_hash("reset_recreate_budget", GYM, TICKET,
                                                {"row_id": "r1"})
    assert r["status"] == "reserved"
    assert r["result"] is None and r["error"] is None and r["finished_at"] is None
    datetime.fromisoformat(r["created_at"])  # parses as ISO
    assert "replay" not in r


def test_begin_replay_is_idempotent_and_does_not_mutate():
    store = {}
    first = _reserved(store)
    with pytest.raises(FR.ReceiptError) as ei:
        _reserved(store)
    assert ei.value.code == "reservation_in_progress" and ei.value.status == 409
    FR.commit(store, KEY, {"completed": True})
    second = _reserved(store)
    assert second.get("replay") is True
    assert second["result"] == {"completed": True}
    assert store[KEY]["created_at"] == first["created_at"]
    assert "replay" not in store[KEY]


def test_begin_conflict_on_a_different_payload_is_409():
    store = {}
    _reserved(store)
    for args in ({"row_id": "r2"}, {"x": 1}):
        with pytest.raises(FR.ReceiptError) as ei:
            _reserved(store, args=args)
        assert ei.value.code == "reservation_conflict" and ei.value.status == 409
    with pytest.raises(FR.ReceiptError) as ei:
        FR.begin(store, KEY, "reset_recreate_budget", OTHER_GYM, TICKET, {})
    assert ei.value.code == "reservation_conflict"
    with pytest.raises(FR.ReceiptError) as ei:
        FR.begin(store, KEY, "reset_recreate_budget", GYM, TICKET_B, {})
    assert ei.value.code == "reservation_conflict"


@pytest.mark.parametrize("bad", ["", "short", "x" * 129, "has space!!", "dots.arent.ok",
                                 "unicode-é-key"])
def test_begin_and_get_receipt_reject_bad_keys(bad):
    with pytest.raises(FR.ReceiptError) as ei:
        FR.begin({}, bad, "reset_recreate_budget", GYM, TICKET, {})
    assert ei.value.code == "bad_reservation_key" and ei.value.status == 400
    with pytest.raises(FR.ReceiptError) as ei:
        FR.get_receipt({}, bad, GYM)
    assert ei.value.code == "bad_reservation_key" and ei.value.status == 400


def test_commit_fail_and_mark_unknown_transitions():
    store = {}
    _reserved(store)
    r = FR.commit(store, KEY, {"sent": True})
    assert r["status"] == "done" and r["result"] == {"sent": True}
    assert r["finished_at"] is not None and r["error"] is None

    _reserved(store, key="rsv-fail-0001")
    r = FR.fail(store, "rsv-fail-0001", "postcondition_unconfirmed")
    assert r["status"] == "failed" and r["error"] == "postcondition_unconfirmed"
    assert r["finished_at"] is not None and r["result"] is None

    _reserved(store, key="rsv-unkn-0001")
    r = FR.mark_unknown(store, "rsv-unkn-0001", "response lost after commit?")
    assert r["status"] == "unknown" and r["error"] == "response lost after commit?"
    assert r["finished_at"] is not None


def test_unknown_outcome_blocks_every_retry_forever():
    store = {}
    _reserved(store)
    FR.mark_unknown(store, KEY, "undetermined")
    # same payload: still refused, never re-executed
    with pytest.raises(FR.ReceiptError) as ei:
        _reserved(store)
    assert ei.value.code == "reservation_outcome_unknown" and ei.value.status == 409
    # different payload: NOT a plain conflict -- the outcome is unknown, always 409
    with pytest.raises(FR.ReceiptError) as ei:
        _reserved(store, args={"different": True})
    assert ei.value.code == "reservation_outcome_unknown" and ei.value.status == 409


def test_get_receipt_is_tenant_bound():
    store = {}
    _reserved(store)
    with pytest.raises(FR.ReceiptError) as ei:
        FR.get_receipt({}, "rsv-absent-01", GYM)
    assert ei.value.code == "unknown_receipt" and ei.value.status == 404
    with pytest.raises(FR.ReceiptError) as ei:
        FR.get_receipt(store, KEY, OTHER_GYM)
    assert ei.value.code == "receipt_tenant_mismatch" and ei.value.status == 403
    r = FR.get_receipt(store, KEY, GYM)
    assert r["key"] == KEY and r["gym_key"] == GYM


def test_a_failing_store_is_wrapped_as_503_everywhere():
    for fn in (lambda: FR.begin(ExplodingStore(), KEY, "reset_recreate_budget", GYM, TICKET, {}),
               lambda: FR.commit(ExplodingStore(), KEY, {}),
               lambda: FR.fail(ExplodingStore(), KEY, "x"),
               lambda: FR.mark_unknown(ExplodingStore(), KEY, "x"),
               lambda: FR.get_receipt(ExplodingStore(), KEY, GYM)):
        with pytest.raises(FR.ReceiptError) as ei:
            fn()
        assert ei.value.code == "store_unavailable" and ei.value.status == 503


# ---- HTTP: reservation_key validation + backwards compatibility ---------------------------

def test_no_reservation_key_means_no_receipt_backwards_compatible(armed):
    calls = []
    status, body = _post("reset_recreate_budget", _body(),
                         deps={"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
                               "receipt_store": ReceiptStore()})
    assert status == 200 and body["ok"] is True
    assert "receipt" not in body and "replayed" not in body
    assert calls == [GYM]


@pytest.mark.parametrize("bad", ["short", "x" * 129, "has space!!"])
def test_a_bad_reservation_key_is_400_and_runs_nothing(armed, bad):
    calls = []
    status, body = _post("reset_recreate_budget", _body(reservation_key=bad),
                         deps={"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
                               "receipt_store": ReceiptStore()})
    assert status == 400 and body["error"] == "bad_reservation_key"
    assert calls == []


def test_an_unavailable_store_is_503_and_runs_nothing(armed):
    calls = []
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY),
                         deps={"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
                               "receipt_store": ExplodingStore()})
    assert status == 503 and body["error"] == "store_unavailable"
    assert calls == []


# ---- scenario 1: hostile duplicate --------------------------------------------------------

def test_duplicate_key_with_different_payload_conflicts_and_executes_once(armed):
    calls = []
    store = ReceiptStore()
    deps = {"bus": AnyTicketBus(), "reset_recreate_budget": _ok_reset(calls),
            "receipt_store": store}
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 200 and calls == [GYM]

    # same key, different args -> 409 reservation_conflict, no second execution
    status, body = _post("reset_recreate_budget",
                         _body(reservation_key=KEY, extra="different"), deps=deps)
    assert status == 409 and body["error"] == "reservation_conflict"
    # same key, different gym -> refused, no second execution
    status, body = _post("reset_recreate_budget",
                         _body(reservation_key=KEY, gym=OTHER_GYM), deps=deps)
    assert status == 409 and body["error"] == "ticket_tenant_unconfirmed"
    # same key, different ticket -> refused, no second execution
    status, body = _post("reset_recreate_budget",
                         _body(reservation_key=KEY, ticket=TICKET_B), deps=deps)
    assert status == 409 and body["error"] == "reservation_conflict"

    assert calls == [GYM], "the side effect ran exactly once across all duplicates"


# ---- scenario 2: lost response ------------------------------------------------------------

def test_a_client_retry_replays_the_stored_receipt_without_re_executing(armed):
    calls = []
    store = ReceiptStore()
    deps = {"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
            "receipt_store": store}
    status, first = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 200 and first["ok"] is True
    assert first["receipt"]["status"] == "done"
    assert first["receipt"]["result"] is not None
    assert first["receipt"]["finished_at"] is not None
    assert not first.get("replayed")

    # simulated retry after the response was lost: identical key + payload
    status, replay = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 200 and replay["ok"] is True
    assert replay.get("replayed") is True
    assert replay["receipt"]["key"] == KEY
    assert replay["receipt"]["status"] == "done"
    assert replay["receipt"]["payload_hash"] == first["receipt"]["payload_hash"]
    assert replay["receipt"]["result"] == first["receipt"]["result"]
    assert calls == [GYM], "the retry must NOT re-execute the side effect"


# ---- scenario 3: tenant mismatch -----------------------------------------------------------

def test_receipts_are_tenant_bound_and_never_leak(armed):
    calls = []
    store = ReceiptStore()
    deps = {"bus": AnyTicketBus(), "reset_recreate_budget": _ok_reset(calls),
            "receipt_store": store}
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 200

    # GET under the wrong gym_key -> 403, and no cross-tenant data in the body
    status, body = _get_receipt(KEY, gym=OTHER_GYM, deps=deps)
    assert status == 403 and body["error"] == "receipt_tenant_mismatch"
    assert "receipt" not in body
    assert GYM not in json.dumps(body) and TICKET not in json.dumps(body)

    # replay of the same key under a different gym_key is refused, not executed
    status, body = _post("reset_recreate_budget",
                         _body(reservation_key=KEY, gym=OTHER_GYM), deps=deps)
    assert status == 409 and body["error"] == "ticket_tenant_unconfirmed"
    assert calls == [GYM]


# ---- scenario 4: concurrency ----------------------------------------------------------------

def test_concurrent_same_key_calls_execute_exactly_once(armed):
    N = 8
    store = ReceiptStore(delay=0.02)  # widen any check-then-act race in the store layer
    calls = []
    calls_lock = threading.Lock()

    def reset(key):
        with calls_lock:
            calls.append(key)
        time.sleep(0.05)  # hold the reservation open so the racers overlap in-flight
        return {"before": {"limit": 30, "used": 30, "remaining": 0},
                "after": {"limit": 30, "used": 0, "remaining": 30}}

    deps = {"bus": FakeBus(), "reset_recreate_budget": reset, "receipt_store": store}
    barrier = threading.Barrier(N)
    results = [None] * N

    def worker(i):
        barrier.wait(timeout=10)
        results[i] = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(r is not None for r in results), "every racer finished"

    statuses = [s for s, _ in results]
    assert set(statuses) <= {200, 409}, f"unexpected statuses: {sorted(set(statuses))}"
    executors = [b for s, b in results if s == 200 and not b.get("replayed")]
    replays = [b for s, b in results if s == 200 and b.get("replayed")]
    assert len(executors) == 1, f"exactly one call executes; got {len(executors)}"
    assert calls == [GYM], "the side effect ran exactly once under a same-key race"
    for b in replays:
        assert b["receipt"]["key"] == KEY


def test_sqlite_reservation_is_atomic_across_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    from agent import db
    db.connect().close()  # the real worker initializes its schema before serving
    gate = tmp_path / "start"
    script = """
import pathlib, sys, time
from agent import fixer_ops_receipts as receipts
ready, gate = map(pathlib.Path, sys.argv[1:3])
gym, ticket = sys.argv[3:5]
ready.touch()
deadline = time.monotonic() + 15
while not gate.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('reservation start gate never opened')
    time.sleep(.01)
try:
    receipts.begin(receipts.KvReceiptStore(), 'multiprocess-key-0001',
                   'swap_media', gym, ticket, {'row_id': 'r1'})
    print('owner')
except receipts.ReceiptError as exc:
    print(exc.code)
"""
    procs = [subprocess.Popen([sys.executable, "-c", script,
                               str(tmp_path / f"ready-{i}"), str(gate), GYM, TICKET],
                              cwd=os.path.dirname(os.path.dirname(__file__)),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for i in range(4)]
    try:
        deadline = time.monotonic() + 15
        while not all((tmp_path / f"ready-{i}").exists() for i in range(4)):
            assert time.monotonic() < deadline, "all subprocesses must reach the start gate"
            time.sleep(.01)
        gate.touch()
        outputs = [proc.communicate(timeout=15) for proc in procs]
        for proc, (_, stderr) in zip(procs, outputs):
            assert proc.returncode == 0, stderr
        outcomes = [stdout.strip() for stdout, _ in outputs]
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
    assert outcomes.count("owner") == 1
    assert outcomes.count("reservation_in_progress") == 3


def test_default_receipt_store_fails_closed_without_durable_db(armed, monkeypatch,
                                                              tmp_path):
    monkeypatch.delenv("AGENT_DB_PATH", raising=False)
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "missing-volume"))
    calls = []
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY),
                         deps={"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls)})
    assert status == 503 and body["error"] == "receipt_store_not_durable"
    assert calls == []


def test_wrapped_5xx_is_unknown_and_cannot_reexecute(armed):
    store, calls = ReceiptStore(), []

    def uncertain(_key):
        calls.append("attempt")
        return 503, {"error": "upstream_timeout"}

    deps = {"bus": FakeBus(), "reset_recreate_budget": uncertain,
            "receipt_store": store}
    status, _ = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 503
    assert store[KEY]["status"] == "unknown"
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 409 and body["error"] == "reservation_outcome_unknown"
    assert calls == ["attempt"]


def test_background_action_does_not_claim_a_completed_receipt(armed):
    store = ReceiptStore()
    status, body = _post("restage_month", _body(reservation_key=KEY, days=7),
                         deps={"bus": FakeBus(), "receipt_store": store,
                               "volume_available": lambda: True})
    assert status == 409 and body["error"] == "reservation_background_unsupported"
    assert len(store) == 0


# ---- scenario 5: GET /ops/actions/receipts/<key> ------------------------------------------

def test_receipt_route_requires_the_secret(armed):
    store = ReceiptStore()
    _reserved(store)
    deps = {"receipt_store": store}
    assert _get_receipt(KEY, secret=None, deps=deps)[0] == 401
    assert _get_receipt(KEY, secret="wrong", deps=deps)[0] == 401


def test_receipt_route_validates_the_key_and_404s_unknown(armed):
    deps = {"receipt_store": ReceiptStore()}
    status, body = _get_receipt("ab", deps=deps)  # valid charset, too short
    assert status == 400 and body["error"] == "bad_reservation_key"
    status, body = _get_receipt("rsv-absent-999", deps=deps)
    assert status == 404 and body["error"] == "unknown_receipt"


def test_receipt_route_returns_the_receipt(armed):
    store = ReceiptStore()
    _reserved(store)
    FR.commit(store, KEY, {"sent": True})
    status, body = _get_receipt(KEY, deps={"receipt_store": store})
    assert status == 200 and body["ok"] is True
    r = body["receipt"]
    assert r["key"] == KEY and r["gym_key"] == GYM and r["ticket_id"] == TICKET
    assert r["status"] == "done" and r["result"] == {"sent": True}
    assert r["payload_hash"] == FR.payload_hash("reset_recreate_budget", GYM, TICKET, {})


def test_receipt_route_store_unavailable_is_503(armed):
    status, body = _get_receipt(KEY, deps={"receipt_store": ExplodingStore()})
    assert status == 503 and body["error"] == "store_unavailable"


# ---- scenario 6: unknown outcome -------------------------------------------------------------

def test_mark_unknown_then_any_retry_is_409_and_never_re_executes(armed):
    store = ReceiptStore()
    _reserved(store)
    FR.mark_unknown(store, KEY, "response lost; outcome undetermined")
    calls = []
    deps = {"bus": FakeBus(), "reset_recreate_budget": _ok_reset(calls),
            "receipt_store": store}
    # same payload
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 409 and body["error"] == "reservation_outcome_unknown"
    # different payload: still the unknown-outcome refusal, not a silent retry
    status, body = _post("reset_recreate_budget",
                         _body(reservation_key=KEY, different=True), deps=deps)
    assert status == 409 and body["error"] == "reservation_outcome_unknown"
    assert calls == []


def test_an_undetermined_failure_marks_unknown_and_blocks_retries(armed):
    store = ReceiptStore()
    calls = []

    def boom(key):
        calls.append(key)
        raise ValueError("kv down mid-write")

    deps = {"bus": FakeBus(), "reset_recreate_budget": boom, "receipt_store": store}
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status in (500, 503), f"an undetermined outcome is never a 2xx/409; got {status}"
    receipt = FR.get_receipt(store, KEY, GYM)
    assert receipt["status"] == "unknown", "the reservation must be marked unknown, not retried"

    calls.clear()
    deps["reset_recreate_budget"] = _ok_reset(calls)  # the world recovers
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 409 and body["error"] == "reservation_outcome_unknown"
    assert calls == [], "an unknown outcome is NEVER automatically retried"


# ---- refusal bookkeeping ----------------------------------------------------------------------

def test_a_wrapped_refusal_marks_the_receipt_failed(armed):
    store = ReceiptStore()

    def unconfirmed(key):
        return {"before": {"limit": 30, "used": 30, "remaining": 0},
                "after": {"limit": 30, "used": 1, "remaining": 29}}

    deps = {"bus": FakeBus(), "reset_recreate_budget": unconfirmed, "receipt_store": store}
    status, body = _post("reset_recreate_budget", _body(reservation_key=KEY), deps=deps)
    assert status == 409 and body["error"] == "postcondition_unconfirmed"
    receipt = FR.get_receipt(store, KEY, GYM)
    assert receipt["status"] == "failed" and receipt["error"]
    assert receipt["finished_at"] is not None


@pytest.mark.parametrize("action,args,deps_factory", [
    ("swap_media", {"row_id": "row-ambiguous-1"}, lambda store: {
        "handle_swap_media": lambda *a, **kw: (
            200, {"ok": True, "image_public_url": "https://img/new.jpg",
                  "siblings_swapped": [], "sibling_results": []}),
        "calendar_store": type("MissingSwapReadback", (), {
            "get_row": lambda self, gym_key, row_id: None,
        })(),
    }),
    ("requeue_failed_row", {"row_id": "row-ambiguous-2"}, lambda store: type(
        "RequeueDeps", (), {})()),
])
def test_post_write_ambiguous_readback_marks_receipt_unknown_and_blocks_retry(
        armed, action, args, deps_factory):
    receipt_store = ReceiptStore()
    if action == "requeue_failed_row":
        class AmbiguousRequeueStore:
            def __init__(self):
                self.reads = 0

            def get_row(self, gym_key, row_id):
                self.reads += 1
                if self.reads == 1:
                    return {"id": row_id, "gym_id": gym_key, "status": "failed",
                            "late_post_id": None}
                return None

            def requeue_failed_row(self, row_id):
                return {"id": row_id, "gym_id": GYM, "status": "approved"}

        action_deps = {"calendar_store": AmbiguousRequeueStore()}
    else:
        action_deps = deps_factory(receipt_store)
    deps = {"bus": FakeBus(), "receipt_store": receipt_store, **action_deps}
    body = _body(reservation_key=KEY, **args)
    if action == "swap_media":
        body["request_key"] = FO._business_request_key(deps["bus"]._get,
                                                       deps["bus"].ticket_row)

    status, result = _post(action, body, deps=deps)
    assert status == 503 and result["error"] == "reservation_outcome_unknown"
    receipt = FR.get_receipt(receipt_store, KEY, GYM)
    assert receipt["status"] == "unknown"

    status, result = _post(action, body, deps=deps)
    assert status == 409 and result["error"] == "reservation_outcome_unknown"
