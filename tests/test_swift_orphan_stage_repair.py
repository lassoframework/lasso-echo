import copy
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from tools import swift_orphan_stage_repair as repair

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path):
    entries = []
    for ident, (date, staged) in repair.TARGETS.items():
        records = [{"asset_id": "sibling", "rolled_back": False, "sent": "preserve"},
                   {"asset_id": ident, "gym_id": repair.GYM, "prev_used_count": 0,
                    "prev_last_used_at": None, "staged_at": staged, "rolled_back": False}]
        entries.append({"source_before": {"id": "source", "gym_id": repair.GYM, "active": True}, "key": f"gym_media_use:{repair.GYM}:{date}", "ledger_before": json.dumps(records),
                        "asset": {"id": ident, "gym_id": repair.GYM, "source_id": "source",
                                  "content_hash": "frozenhash", "drive_modified": "2026-09-01T00:00:00Z",
                                  "used_count": 1, "last_used_at": staged, "eligible": True,
                                  "review_status": "approved", "excluded_by_coach": False}})
    plan = {"schema": 1, "gym_id": repair.GYM, "entries": entries}
    db_path = tmp_path / "echo.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO kv VALUES (?, ?)", [(e["key"], e["ledger_before"]) for e in entries])
    receipt = {"plan_sha256": repair.digest(plan), "gym_id": repair.GYM, "fence_id": "fence-1",
               "verified_by": "independent-operator", "independently_verified": True,
               "fence_held": True, "retain_until_journal_complete": True,
               "writers": sorted(repair.WRITERS), "other_relevant_writers": [],
               "verified_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
               **{key: [] for key in repair.CLEAR}}
    api = FakeAPI(plan)
    return plan, db_path, receipt, api, tmp_path / "journal.json"


class FakeAPI:
    def __init__(self, plan):
        self.rows = {e["asset"]["id"]: copy.deepcopy(e["asset"]) for e in plan["entries"]}
        self.sources = {e["asset"]["id"]: copy.deepcopy(e["source_before"]) for e in plan["entries"]}
        self.calls = 0
        self.crash = False
        self.before_cas = None

    def read(self, entry):
        return copy.deepcopy(self.rows[entry["asset"]["id"]])

    def read_source(self, entry):
        return copy.deepcopy(self.sources[entry["asset"]["id"]])

    def cas(self, entry):
        if self.before_cas:
            self.before_cas()
        assert repair.equal(self.read(entry), entry["asset"])
        self.calls += 1
        self.rows[entry["asset"]["id"]] = repair.after_asset(entry)
        if self.crash:
            self.crash = False
            raise RuntimeError("lost response after remote commit")


def apply(context):
    plan, db, receipt, api, journal = context
    return repair.run(plan, db, apply=True, receipt=receipt, api=api, journal_path=journal, now=NOW)


def test_dry_run_is_read_only(context):
    plan, db, _, api, journal = context
    before = db.read_bytes()
    assert repair.run(plan, db)["mode"] == "dry_run"
    assert db.read_bytes() == before
    assert not journal.exists() and api.calls == 0


def test_stale_counter_fails_before_any_write(context):
    plan, _, _, api, journal = context
    api.rows[plan["entries"][1]["asset"]["id"]]["used_count"] = 2
    with pytest.raises(repair.Blocked, match="changed asset"):
        apply(context)
    assert api.calls == 0 and not journal.exists()


def test_changed_ledger_fails_before_any_write(context):
    plan, path, _, api, journal = context
    with sqlite3.connect(path) as db:
        db.execute("UPDATE kv SET value='[]' WHERE key=?", (plan["entries"][2]["key"],))
    with pytest.raises(repair.Blocked, match="whole ledger"):
        apply(context)
    assert api.calls == 0 and not journal.exists()


@pytest.mark.parametrize("gate", ["active_owners", "unknown_sends", "leases", "queued_writes"])
def test_active_owner_or_unknown_send_blocks(context, gate):
    context[2][gate] = ["unsafe"]
    with pytest.raises(repair.Blocked, match="fence"):
        apply(context)
    assert context[3].calls == 0


def test_crash_resume_never_double_decrements(context):
    context[3].crash = True
    with pytest.raises(RuntimeError, match="lost response"):
        apply(context)
    journal = json.loads(context[4].read_text())
    assert list(journal["states"].values()) == ["intent"]
    assert apply(context)["complete"]
    assert context[3].calls == 3
    assert apply(context)["complete"]
    assert context[3].calls == 3


def test_siblings_preserved(context):
    assert apply(context)["complete"]
    with sqlite3.connect(context[1]) as db:
        for entry in context[0]["entries"]:
            before = json.loads(entry["ledger_before"])
            after = json.loads(repair.ledger_read(db, entry["key"]))
            assert after[0] == before[0]
            assert after[1] == dict(before[1], rolled_back=True)


def test_change_between_portal_and_ledger_retains_partial_intent(context):
    plan, path, _, api, journal = context
    def mutate():
        with sqlite3.connect(path) as db:
            db.execute("UPDATE kv SET value='[]' WHERE key=?", (plan["entries"][0]["key"],))
    api.before_cas = mutate
    with pytest.raises(repair.Blocked, match="whole ledger changed"):
        apply(context)
    assert api.calls == 1
    assert not json.loads(journal.read_text())["complete"]
    with pytest.raises(repair.Blocked):
        apply(context)
    assert api.calls == 1


def test_source_version_changed_blocks(context):
    context[3].rows[context[0]["entries"][0]["asset"]["id"]]["drive_modified"] = "2026-10-08T00:00:00Z"
    with pytest.raises(repair.Blocked):
        apply(context)
    assert context[3].calls == 0


def test_new_fence_cannot_adopt_partial_journal(context):
    context[3].crash = True
    with pytest.raises(RuntimeError):
        apply(context)
    context[2]["fence_id"] = "other-fence"
    with pytest.raises(repair.Blocked, match="journal plan/fence mismatch"):
        apply(context)


def test_unsupported_cas_has_no_apply_fallback(context):
    context[0]["entries"][2]["asset"]["source_id"] = "unsupported\\source"
    context[0]["entries"][2]["source_before"]["id"] = "unsupported\\source"
    with pytest.raises(repair.Blocked, match="unsupported atomic CAS"):
        apply(context)
    assert context[3].calls == 0


def test_supabase_adapter_pins_atomic_predicates(context):
    entry = context[0]["entries"][0]
    class Response:
        status_code = 200
        def json(self):
            return [repair.after_asset(entry)]
    class Store:
        def available(self): return True
        def _client(self): return self
        def _rest(self, table): return "offline/" + table
        def _headers(self, headers): return headers
        def patch(self, url, **kwargs):
            assert kwargs["params"] == {k: repair.filter_value(entry["asset"][k]) for k in repair.PINS}
            assert kwargs["json"] == {"used_count": 0, "last_used_at": None}
            return Response()
    repair.SupabaseCAS(Store()).cas(entry)


def test_changed_source_ownership_blocks(context):
    context[3].sources[context[0]["entries"][0]["asset"]["id"]]["gym_id"] = "other-tenant"
    with pytest.raises(repair.Blocked, match="source ownership"):
        apply(context)
    assert context[3].calls == 0


def test_crash_after_ledger_commit_resumes_without_rewriting(context, monkeypatch):
    original = repair.save_journal
    crashed = False
    def save(path, journal):
        nonlocal crashed
        if not crashed and "complete" in journal["states"].values():
            crashed = True
            raise RuntimeError("crash after ledger commit before journal progress")
        original(path, journal)
    monkeypatch.setattr(repair, "save_journal", save)
    with pytest.raises(RuntimeError, match="after ledger commit"):
        apply(context)
    assert apply(context)["complete"]
    assert context[3].calls == 3


def test_fence_expiration_after_remote_write_keeps_ledger_unchanged(context):
    calls = 0
    def clock():
        nonlocal calls
        calls += 1
        return NOW if calls <= 3 else NOW + timedelta(minutes=11)
    plan, path, receipt, api, journal = context
    with pytest.raises(repair.Blocked, match="expired"):
        repair.run(plan, path, apply=True, receipt=receipt, api=api, journal_path=journal, now=clock)
    assert api.calls == 1
    with sqlite3.connect(path) as db:
        assert repair.ledger_read(db, plan["entries"][0]["key"]) == plan["entries"][0]["ledger_before"]
    assert not json.loads(journal.read_text())["complete"]


@pytest.mark.parametrize("event", ["source_read", "asset_read", "journal_fsync"])
def test_expiry_during_remote_preparation_prevents_cas(context, monkeypatch, event):
    plan, path, receipt, api, journal = context
    expired = False
    if event in ("source_read", "asset_read"):
        name = "read_source" if event == "source_read" else "read"
        original = getattr(api, name)
        reads = 0
        def read(entry):
            nonlocal expired, reads
            result = original(entry)
            reads += 1
            if reads == 4:  # Three preflight reads, then first attended repair.
                expired = True
            return result
        monkeypatch.setattr(api, name, read)
    else:
        original = repair.save_journal
        def save(target, value):
            nonlocal expired
            original(target, value)  # Actual fsync and durable intent.
            expired = True
        monkeypatch.setattr(repair, "save_journal", save)
    with pytest.raises(repair.Blocked, match="expired"):
        repair.run(plan, path, apply=True, receipt=receipt, api=api, journal_path=journal,
                   now=lambda: NOW + timedelta(minutes=11) if expired else NOW)
    assert api.calls == 0
    assert json.loads(journal.read_text())["states"][plan["entries"][0]["asset"]["id"]] == "intent"
    with sqlite3.connect(path) as db:
        for entry in plan["entries"]:
            assert repair.ledger_read(db, entry["key"]) == entry["ledger_before"]


@pytest.mark.parametrize("event", ["lock_acquisition", "before_update", "before_commit"])
def test_expiry_in_sqlite_transaction_prevents_durable_ledger_write(context, monkeypatch, event):
    plan, path, receipt, api, journal = context
    original = repair.connect
    expired = False
    class Connection:
        def __init__(self, db):
            self.db = db
            self.in_transaction = False
        def __enter__(self): return self
        def __exit__(self, *args):
            try:
                return self.db.__exit__(*args)
            finally:
                self.db.close()
        def execute(self, sql, *args):
            nonlocal expired
            result = self.db.execute(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                self.in_transaction = True
                if event == "lock_acquisition": expired = True
            elif self.in_transaction and sql.startswith("SELECT") and event == "before_update":
                expired = True
            elif sql.startswith("UPDATE") and event == "before_commit":
                expired = True
            return result
        def commit(self): self.db.commit()
        def rollback(self): self.db.rollback()
    monkeypatch.setattr(repair, "connect", lambda *args, **kwargs: Connection(original(*args, **kwargs)))
    with pytest.raises(repair.Blocked, match="expired"):
        repair.run(plan, path, apply=True, receipt=receipt, api=api, journal_path=journal,
                   now=lambda: NOW + timedelta(minutes=11) if expired else NOW)
    assert api.calls == 1  # Remote half committed under a valid fence; keep the fence.
    assert not json.loads(journal.read_text())["complete"]
    with sqlite3.connect(path) as db:
        for entry in plan["entries"]:
            assert repair.ledger_read(db, entry["key"]) == entry["ledger_before"]
