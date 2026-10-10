"""GBP staged-journal binding (2026-10-09): durable exact stage-attempt
identity, staged_pending vs finalized, fail-closed remote-use gating.

Local-only: a fake PostgREST transport and a tmp SQLite journal. No provider,
no PG, no production. The binding flag AGENT_GBP_STAGED_JOURNAL defaults OFF.
"""
from copy import deepcopy
import hashlib
import json
import uuid

import pytest

from agent import gbp_drive_use_journal as journal
from agent.gbp_drive_use_journal import JournalHold
from agent import portal_calendar_store as pcs
from agent.portal_calendar_store import (SupabaseCalendarStore,
                                         ReservationStoreError)
from agent import gbp_planner

GYM = "gym1"
FLAG = pcs.GBP_STAGED_JOURNAL_FLAG_ENV


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


@pytest.fixture
def jdb(tmp_path, monkeypatch):
    path = str(tmp_path / "journal.db")
    monkeypatch.setattr(journal, "_canonical_db_path", lambda: path)
    return path


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeHttp:
    """Alias read + the two batch RPCs. The stage receipt is echoed from the
    exact request args, like the real idempotent SQL authority would."""

    def __init__(self):
        self.stage_calls = []
        self.status_calls = []
        self.stage_exc = None
        self.on_stage = None
        self.status_payload = None

    def get(self, url, params=None, headers=None, timeout=None):
        assert pcs._FORWARD_TENANT_ALIAS_TABLE in url
        return _Resp(200, [])

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("rpc/" + pcs._STAGE_RPC):
            self.stage_calls.append(json)
            if self.on_stage:
                self.on_stage(json)
            if self.stage_exc is not None:
                raise self.stage_exc
            request = __import__("json").loads(json["p_request"])
            return _Resp(200, {
                "batch_id": json["p_batch_id"],
                "tenant_id": json["p_tenant_id"],
                "request_digest": json["p_request_digest"],
                "state": "staged",
                "member_row_ids": [m["row"]["id"] for m in request["members"]],
                "observation_row_ids": [],
                "old_row_ids": [o["id"] for o in request["old_rows"]],
                "finalize_receipt": None})
        if url.endswith("rpc/" + pcs._BATCH_STATUS_RPC):
            self.status_calls.append(json)
            return _Resp(200, self.status_payload)
        raise AssertionError("unexpected POST " + url)


def _store(http):
    return SupabaseCalendarStore(url="http://example.invalid",
                                 service_key="test-key", http=http)


def _rows(n=1):
    return [{"id": str(uuid.uuid4()), "logical_post_id": str(uuid.uuid4()),
             "gym_id": GYM, "post_date": f"2026-11-{i + 1:02d}",
             "status": "pending", "caption": f"caption {i}",
             "format": "update", "account": "googlebusiness"}
            for i in range(n)]


def _status_payload(entry, state="staged", finalize_receipt=None):
    return {"batch_id": entry["batch_id"], "tenant_id": entry["tenant_id"],
            "request_digest": entry["request_digest"], "state": state,
            "member_row_ids": list(entry["member_row_ids"]),
            "observation_row_ids": [],
            "old_row_ids": list(entry["old_row_ids"]),
            "finalize_receipt": finalize_receipt}


def _terminal_receipt(entry, **overrides):
    receipt = {"batch_id": entry["batch_id"], "state": "finalized",
               "tenant_id": entry["tenant_id"],
               "request_digest": entry["request_digest"],
               "row_ids": list(entry["member_row_ids"]),
               "reservation_ids": [str(uuid.uuid4())
                                   for _ in entry["member_row_ids"]],
               "archived_old_row_ids": list(entry["old_row_ids"])}
    receipt.update(overrides)
    return receipt


def _stage(store, http):
    return store.stage_forward_schedule_batch(GYM, _rows())


def test_off_path_leaves_no_durable_binding(jdb):
    http, store = _FakeHttp(), None
    store = _store(http)
    receipt = _stage(store, http)
    assert receipt["state"] == "staged"
    # Legacy behavior intact: in-memory attempt recorded, nothing durable.
    assert store.last_forward_stage_attempt["batch_id"] == receipt["batch_id"]
    assert journal.pending_forward_stages() == []
    assert journal.get_forward_stage(receipt["batch_id"]) is None


def test_ambiguous_flag_fails_closed_before_any_write(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "perhaps")
    http = _FakeHttp()
    with pytest.raises(ReservationStoreError):
        _stage(_store(http), http)
    assert http.stage_calls == []
    assert journal.pending_forward_stages() == []


def test_armed_records_exact_intent_before_rpc(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    http = _FakeHttp()
    seen = {}

    def during(args):
        entry = journal.get_forward_stage(args["p_batch_id"])
        seen["entry"] = entry

    http.on_stage = during
    store = _store(http)
    receipt = _stage(store, http)
    # The intent was durable BEFORE the RPC, bound to the exact request.
    intent = seen["entry"]
    assert intent is not None and intent["state"] == "stage_intent"
    assert intent["request_digest"] == hashlib.sha256(
        intent["request_text"].encode("utf-8")).hexdigest()
    assert intent["tenant_id"] == GYM
    # After the exact staged receipt the batch is staged_pending: candidates
    # only, never remote consumption evidence.
    bound = journal.get_forward_stage(receipt["batch_id"])
    assert bound["state"] == "staged_pending"
    assert not journal.forward_remote_use_allowed(receipt["batch_id"])
    assert not store.forward_remote_use_settlement_allowed(receipt["batch_id"])
    # Exact replay returns the same durable entry; a changed request under the
    # same batch id conflicts.
    again = journal.record_forward_stage_intent(
        {k: bound[k] for k in ("batch_id", "tenant_id", "request_digest",
                               "request_text", "member_row_ids", "old_row_ids")})
    assert again["batch_id"] == bound["batch_id"]
    changed = dict({k: bound[k] for k in ("batch_id", "tenant_id",
                                          "request_digest", "request_text",
                                          "member_row_ids", "old_row_ids")},
                   member_row_ids=[str(uuid.uuid4())])
    with pytest.raises(JournalHold):
        journal.record_forward_stage_intent(changed)


def test_durable_restart_recovers_lost_stage_ack(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    http = _FakeHttp()
    http.stage_exc = ConnectionError("lost acknowledgment")
    first = _store(http)
    with pytest.raises(ReservationStoreError):
        _stage(first, http)
    batch_id = first.last_forward_stage_attempt["batch_id"]
    # Restart: a NEW store has no process memory; the durable journal is the
    # only handle, resolved through the bound batch-status RPC.
    entry = journal.get_forward_stage(batch_id)
    assert entry["state"] == "stage_intent"
    http2 = _FakeHttp()
    http2.status_payload = _status_payload(entry, "staged")
    second = _store(http2)
    assert second.last_forward_stage_attempt is None
    data = second.resolve_forward_stage_attempt()
    assert data["state"] == "staged"
    assert http2.status_calls == [{"p_batch_id": batch_id}]
    # Recovery binds the receipt durably; still only a candidate batch.
    assert journal.get_forward_stage(batch_id)["state"] == "staged_pending"
    assert not second.forward_remote_use_settlement_allowed(batch_id)


def test_terminal_receipt_mismatch_holds(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    http = _FakeHttp()
    store = _store(http)
    receipt = _stage(store, http)
    entry = journal.get_forward_stage(receipt["batch_id"])
    assert entry["state"] == "staged_pending"
    # Finalized status with a substituted member row id: not terminal proof.
    bad = _terminal_receipt(entry, row_ids=[str(uuid.uuid4())])
    with pytest.raises(JournalHold):
        journal.record_forward_finalized(
            entry["batch_id"], _status_payload(entry, "finalized", bad))
    http.status_payload = _status_payload(entry, "finalized", bad)
    with pytest.raises(ReservationStoreError):
        store.bind_forward_finalization(entry["batch_id"])
    after = journal.get_forward_stage(entry["batch_id"])
    assert after["state"] == "staged_pending"
    assert after["finalize_receipt"] is None
    assert not store.forward_remote_use_settlement_allowed(entry["batch_id"])
    # A non-final readback also cannot finalize.
    http.status_payload = _status_payload(entry, "staged")
    with pytest.raises(ReservationStoreError):
        store.bind_forward_finalization(entry["batch_id"])


def test_exact_terminal_receipt_finalizes_and_allows_remote_use(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    http = _FakeHttp()
    store = _store(http)
    receipt = _stage(store, http)
    entry = journal.get_forward_stage(receipt["batch_id"])
    good = _terminal_receipt(entry)
    http.status_payload = _status_payload(entry, "finalized", good)
    data = store.bind_forward_finalization(entry["batch_id"])
    assert data["state"] == "finalized"
    bound = journal.get_forward_stage(entry["batch_id"])
    assert bound["state"] == "finalized"
    assert bound["finalize_receipt"] == good
    assert journal.pending_forward_stages() == []
    assert store.forward_remote_use_settlement_allowed(entry["batch_id"])
    # Exact replay is stable; a differing receipt under the same id holds.
    replay = journal.record_forward_finalized(
        entry["batch_id"], _status_payload(entry, "finalized", deepcopy(good)))
    assert replay["state"] == "finalized"
    with pytest.raises(JournalHold):
        journal.record_forward_finalized(
            entry["batch_id"],
            _status_payload(entry, "finalized",
                            _terminal_receipt(entry,
                                              reservation_ids=[str(uuid.uuid4())])))


def test_lost_stage_and_finalize_acks_settle_only_on_exact_terminal(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    http = _FakeHttp()
    captured = {}
    http.on_stage = lambda args: captured.update(args)
    http.stage_exc = ConnectionError("lost")
    with pytest.raises(ReservationStoreError):
        _stage(_store(http), http)
    import json as _json
    request = _json.loads(captured["p_request"])
    entry = journal.get_forward_stage(captured["p_batch_id"])
    assert entry["state"] == "stage_intent"
    good = _terminal_receipt(entry)
    http2 = _FakeHttp()
    http2.status_payload = _status_payload(entry, "finalized", good)
    second = _store(http2)
    data = second.resolve_forward_stage_attempt()
    assert data["state"] == "finalized"
    assert journal.get_forward_stage(entry["batch_id"])["state"] == "finalized"
    assert second.forward_remote_use_settlement_allowed(entry["batch_id"])
    assert request["tenant_id"] == entry["tenant_id"]


def _drive_journal_entry():
    from tests.test_gbp_drive_use_journal import make_request
    entry = journal.prepare(make_request(gym=GYM))
    return journal.record_write_intent(entry["use_id"])


def test_planner_holds_staged_candidate_when_armed(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    entry = _drive_journal_entry()
    logs = []
    candidate = dict(entry["calendar_row"], id=str(uuid.uuid4()),
                     variant_status="candidate",
                     media_not_ready_reason="forward_reservation_staged")
    pick = {"journal_entry": entry, "asset": {"id": entry["asset_id"]},
            "base": GYM, "day_key": entry["post_date"]}
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], pick, candidate, logs.append)
    assert ok is False
    assert journal.get(entry["use_id"])["state"] == "write_intent"
    assert any("not a landed placement" in m for m in logs)


def test_planner_holds_active_member_of_unfinalized_batch(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    entry = _drive_journal_entry()
    row_id = str(uuid.uuid4())
    request_text = json.dumps(
        {"tenant_id": GYM, "old_rows": [],
         "members": [{"row": {"id": row_id}, "observation": None}]},
        sort_keys=True, separators=(",", ":"))
    journal.record_forward_stage_intent({
        "batch_id": str(uuid.uuid4()), "tenant_id": GYM,
        "request_digest": hashlib.sha256(request_text.encode()).hexdigest(),
        "request_text": request_text, "member_row_ids": [row_id],
        "old_row_ids": []})
    persisted = dict(entry["calendar_row"], id=row_id, variant_status="active")
    pick = {"journal_entry": entry, "asset": {"id": entry["asset_id"]},
            "base": GYM, "day_key": entry["post_date"]}
    logs = []
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], pick, persisted, logs.append)
    assert ok is False
    assert journal.get(entry["use_id"])["state"] == "write_intent"
    assert any("not finalized" in m for m in logs)


def test_planner_off_path_untouched_by_binding(jdb):
    entry = _drive_journal_entry()
    persisted = dict(entry["calendar_row"], id=str(uuid.uuid4()),
                     variant_status="active", created_at="2026-10-09",
                     updated_at="2026-10-09")
    pick = {"journal_entry": entry, "asset": {"id": entry["asset_id"]},
            "base": GYM, "day_key": entry["post_date"]}
    # OFF: the legacy path runs and confirm_landed does the exact comparison
    # (the staged-candidate/batch guards never engage without the flag).
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], pick, persisted, lambda m: None)
    assert ok is False  # held downstream: no real remote authority here
    assert journal.get(entry["use_id"])["state"] in (
        "write_intent", "consumption_pending")


# ---- independent-review regression tests (2026-10-09) ----

def _make_stage_entry(member_row_id, tenant=GYM, logical="lp-1", row=None):
    request_text = json.dumps(
        {"tenant_id": tenant, "old_rows": [],
         "members": [{"row": dict(row or {}, id=member_row_id,
                                   logical_post_id=logical),
                      "observation": None}]},
        sort_keys=True, separators=(",", ":"))
    return {"batch_id": str(uuid.uuid4()), "tenant_id": tenant,
            "request_digest": hashlib.sha256(request_text.encode()).hexdigest(),
            "request_text": request_text, "member_row_ids": [member_row_id],
            "old_row_ids": []}


def _finalize(entry_dict):
    journal.record_forward_stage_intent(entry_dict)
    bound = journal.get_forward_stage(entry_dict["batch_id"])
    journal.record_forward_finalized(
        bound["batch_id"],
        _status_payload(bound, "finalized", _terminal_receipt(bound)))
    return journal.get_forward_stage(bound["batch_id"])


def _persisted_active(entry, row_id, logical="lp-1"):
    row = dict(entry["calendar_row"], id=row_id, variant_status="active",
               media_not_ready_reason=None,
               created_at="2026-10-09", updated_at="2026-10-09")
    row["logical_post_id"] = logical
    return row


def _pick(entry):
    return {"journal_entry": entry, "asset": {"id": entry["asset_id"]},
            "base": GYM, "day_key": entry["post_date"]}


def _drive_entry(logical="lp-1"):
    from tests.test_gbp_drive_use_journal import make_request
    e = journal.prepare(make_request(gym=GYM, logical=logical))
    return journal.record_write_intent(e["use_id"])


def test_armed_settlement_holds_without_durable_binding(jdb, monkeypatch):
    # Exact active row but NO durable batch binding: must hold before any
    # begin_consumption/stamp_use.
    monkeypatch.setenv(FLAG, "1")
    entry = _drive_entry()
    persisted = _persisted_active(entry, str(uuid.uuid4()))
    logs = []
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry), persisted, logs.append)
    assert ok is False
    assert journal.get(entry["use_id"])["state"] == "write_intent"
    assert any("no durable batch binding" in m for m in logs)


def test_armed_settlement_holds_on_cross_tenant_binding(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    _finalize(_make_stage_entry(row_id, tenant="other-gym"))
    persisted = _persisted_active(entry, row_id)
    logs = []
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry), persisted, logs.append)
    assert ok is False
    assert journal.get(entry["use_id"])["state"] == "write_intent"
    assert any("cross-tenant" in m for m in logs)


def test_armed_settlement_holds_on_frozen_member_mismatch(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    # Binding exists, tenant matches, finalized -- but the frozen member row
    # carries a different logical_post_id than the armed journal entry.
    _finalize(_make_stage_entry(row_id, logical="lp-OTHER"))
    persisted = _persisted_active(entry, row_id)
    logs = []
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry), persisted, logs.append)
    assert ok is False
    assert journal.get(entry["use_id"])["state"] == "write_intent"
    assert any("exact frozen member" in m for m in logs)


def test_armed_settlement_proceeds_with_exact_finalized_binding(jdb, monkeypatch):
    # Positive control: exact tenant + frozen member + terminal proof passes
    # the binding gate (the remote stamp itself then holds without a real
    # authority, leaving the same use UUID in consumption_pending).
    monkeypatch.setenv(FLAG, "1")
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    _finalize(_make_stage_entry(row_id, row=entry["calendar_row"]))
    persisted = _persisted_active(entry, row_id)
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry), persisted, lambda m: None)
    assert ok is False
    assert journal.get(entry["use_id"])["state"] == "consumption_pending"
    assert journal.get(entry["use_id"])["use_id"] == entry["use_id"]


def _consume_into_pending(entry, row_id, monkeypatch):
    """Drive the journal to consumption_pending via the OFF legacy path so
    recovery-state tests start from a durable mid-flight state."""
    persisted = _persisted_active(entry, row_id)
    gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry), persisted, lambda m: None)
    assert journal.get(entry["use_id"])["state"] == "consumption_pending"


def test_armed_recovery_consumption_pending_without_row_makes_no_remote_call(
        jdb, monkeypatch):
    # Lost ack -> recovery resumes consumption_pending with persisted_row=None
    # (readback error/missing). Armed mode must make ZERO remote calls and
    # keep the SAME use UUID.
    entry = _drive_entry()
    _consume_into_pending(entry, str(uuid.uuid4()), monkeypatch)
    monkeypatch.setenv(FLAG, "1")
    calls = []
    import agent.gym_media_selector as gms
    monkeypatch.setattr(gms, "stamp_use",
                        lambda *a, **k: calls.append((a, k)))
    logs = []
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry), None, logs.append)
    assert ok is False
    assert calls == []
    after = journal.get(entry["use_id"])
    assert after["state"] == "consumption_pending"
    assert after["use_id"] == entry["use_id"]
    assert any("exact persisted row" in m for m in logs)


def test_armed_recovery_confirmed_landed_requires_binding(jdb, monkeypatch):
    entry = _drive_entry()
    persisted = _persisted_active(entry, str(uuid.uuid4()))
    journal.confirm_landed(entry["use_id"], {
        "calendar_row": persisted, "asset_id": entry["asset_id"],
        "content_hash": entry["content_hash"]})
    monkeypatch.setenv(FLAG, "1")
    calls = []
    import agent.gym_media_selector as gms
    monkeypatch.setattr(gms, "stamp_use",
                        lambda *a, **k: calls.append((a, k)))
    # Recovery readback returns an exact active row but no durable binding.
    logs = []
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry), persisted, logs.append)
    assert ok is False
    assert calls == []
    assert journal.get(entry["use_id"])["state"] == "confirmed_landed"
    assert any("no durable batch binding" in m for m in logs)


def test_ambiguous_flag_holds_settlement_before_remote_mutation(
        jdb, monkeypatch):
    # Even with a perfect active row AND a finalized exact binding, an
    # ambiguous flag value holds before any remote mutation.
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    _consume_into_pending(entry, row_id, monkeypatch)
    _finalize(_make_stage_entry(row_id))
    monkeypatch.setenv(FLAG, "perhaps")
    calls = []
    import agent.gym_media_selector as gms
    monkeypatch.setattr(gms, "stamp_use",
                        lambda *a, **k: calls.append((a, k)))
    logs = []
    ok = gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry),
        _persisted_active(entry, row_id), logs.append)
    assert ok is False
    assert calls == []
    assert journal.get(entry["use_id"])["state"] == "consumption_pending"
    assert any("ambiguous" in m for m in logs)


def test_conflicting_member_ownership_rejected_before_stage_rpc(jdb):
    first = _make_stage_entry(str(uuid.uuid4()))
    journal.record_forward_stage_intent(first)
    # A different batch id claiming an already-bound member row id conflicts.
    second = _make_stage_entry(first["member_row_ids"][0])
    with pytest.raises(JournalHold, match="stage_member_conflict"):
        journal.record_forward_stage_intent(second)
    assert journal.get_forward_stage(second["batch_id"]) is None


def test_forward_stage_for_member_holds_on_multiple_matches(jdb):
    # Corrupt durable state (two batches owning one member, e.g. written
    # before the conflict guard): the lookup holds instead of returning the
    # first match.
    import sqlite3
    row_id = str(uuid.uuid4())
    first = _make_stage_entry(row_id)
    journal.record_forward_stage_intent(first)
    second = _make_stage_entry(row_id)
    conn = sqlite3.connect(jdb)
    conn.execute(
        "INSERT INTO gbp_forward_stage_journal"
        " (batch_id,tenant_id,request_digest,request_text,member_row_ids,"
        " old_row_ids,state) VALUES (?,?,?,?,?,?,?)",
        (second["batch_id"], second["tenant_id"], second["request_digest"],
         second["request_text"], json.dumps(second["member_row_ids"]),
         json.dumps(second["old_row_ids"]), "stage_intent"))
    conn.commit()
    conn.close()
    with pytest.raises(JournalHold, match="stage_member_ambiguous"):
        journal.forward_stage_for_member(row_id)


def test_recover_reads_back_consuming_states_when_armed(jdb, monkeypatch):
    # _recover_armed_drive_uses must re-read the exact persisted row for
    # consumption_pending entries before settlement; a readback error yields
    # a hold with zero remote calls and the same use UUID.
    entry = _drive_entry()
    _consume_into_pending(entry, str(uuid.uuid4()), monkeypatch)
    monkeypatch.setenv(FLAG, "1")
    calls = []
    import agent.gym_media_selector as gms
    monkeypatch.setattr(gms, "stamp_use",
                        lambda *a, **k: calls.append((a, k)))
    import agent.gym_media_index as gmi
    monkeypatch.setattr(gmi, "default_store", lambda: object())
    seen = {}

    class _Store:
        def authoritative_rows_for_keys(self, gym, proposed):
            seen["asked"] = True
            return None  # readback error: unknown outcome

    result = gbp_planner._recover_armed_drive_uses(
        GYM, GYM + "_gbp", _Store(), lambda m: None)
    assert seen.get("asked") is True
    assert result["ok"] is False
    assert entry["use_id"] in result["use_ids"]
    assert calls == []
    assert journal.get(entry["use_id"])["state"] == "consumption_pending"


def _at_consuming_state(entry, persisted, state):
    if state == "unknown_result":
        journal.mark_unknown(entry["use_id"])
    elif state in ("confirmed_landed", "consumption_pending"):
        journal.confirm_landed(entry["use_id"], {
            "calendar_row": persisted, "asset_id": entry["asset_id"],
            "content_hash": entry["content_hash"]})
        if state == "consumption_pending":
            journal.begin_consumption(entry["use_id"])


@pytest.mark.parametrize("state", ["write_intent", "unknown_result",
                                    "confirmed_landed", "consumption_pending"])
@pytest.mark.parametrize("field,value", [
    ("caption", "substituted caption"),
    ("source_media_asset_id", "substituted-asset"),
    ("gym_id", "other-gym"),
    ("logical_post_id", "other-logical-post"),
    ("unexpected_server_column", "unreviewed"),
])
def test_armed_every_consuming_state_rejects_changed_persisted_member(
        jdb, monkeypatch, state, field, value):
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    persisted = _persisted_active(entry, row_id)
    _finalize(_make_stage_entry(row_id, row=entry["calendar_row"]))
    _at_consuming_state(entry, persisted, state)
    monkeypatch.setenv(FLAG, "1")
    import agent.gym_media_selector as gms
    calls = []
    monkeypatch.setattr(gms, "stamp_use", lambda *a, **k: calls.append((a, k)))
    changed = dict(persisted, **{field: value})
    logs = []
    assert not gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], _pick(entry), changed, logs.append)
    assert calls == []
    after = journal.get(entry["use_id"])
    assert after["state"] == state
    assert after["use_id"] == entry["use_id"]
    assert any("exact frozen member" in m for m in logs)


@pytest.mark.parametrize("state", ["write_intent", "unknown_result",
                                    "confirmed_landed", "consumption_pending"])
def test_armed_exact_member_reaches_remote_with_same_uuid(
        jdb, monkeypatch, state):
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    persisted = _persisted_active(entry, row_id)
    # Real stage requests may include candidate/null markers. The SQL
    # finalizer changes only these markers to active/null.
    frozen = dict(entry["calendar_row"], variant_status="candidate",
                  media_not_ready_reason=None)
    _finalize(_make_stage_entry(row_id, row=frozen))
    _at_consuming_state(entry, persisted, state)
    monkeypatch.setenv(FLAG, "1")
    import agent.gym_media_selector as gms
    calls = []
    monkeypatch.setattr(gms, "stamp_use", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(gbp_planner, "_recorded_remote_receipt", lambda use_id: None)
    pick = dict(_pick(entry), store=object())
    assert not gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], pick, persisted, lambda m: None)
    assert len(calls) == 1
    assert calls[0][1]["use_id"] == entry["use_id"]
    assert calls[0][1]["asset_row"] == entry["asset_before"]
    assert calls[0][1]["source_row"] == entry["source_before"]
    assert journal.get(entry["use_id"])["state"] == "consumption_pending"


@pytest.mark.parametrize("field,value", [
    ("caption", "different frozen caption"),
    ("source_media_asset_id", "different-frozen-asset"),
])
def test_armed_recovery_rejects_different_frozen_member(
        jdb, monkeypatch, field, value):
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    persisted = _persisted_active(entry, row_id)
    _at_consuming_state(entry, persisted, "consumption_pending")
    # Current row still matches the use journal and landed proof. A different
    # staged payload with the same row/logical ID must independently hold.
    frozen = dict(entry["calendar_row"], **{field: value})
    _finalize(_make_stage_entry(row_id, row=frozen))
    monkeypatch.setenv(FLAG, "1")
    import agent.gym_media_selector as gms
    calls = []
    monkeypatch.setattr(gms, "stamp_use", lambda *a, **k: calls.append((a, k)))
    assert not gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], dict(_pick(entry), store=object()),
        persisted, lambda m: None)
    assert calls == []
    assert journal.get(entry["use_id"])["state"] == "consumption_pending"


@pytest.mark.parametrize("readback", ["changed_caption", "changed_asset", "error"])
def test_recovery_exact_finalized_binding_holds_changed_or_unreadable_row(
        jdb, monkeypatch, readback):
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    persisted = _persisted_active(entry, row_id)
    _at_consuming_state(entry, persisted, "consumption_pending")
    _finalize(_make_stage_entry(row_id, row=entry["calendar_row"]))
    monkeypatch.setenv(FLAG, "1")
    import agent.gym_media_selector as gms
    import agent.gym_media_index as gmi
    calls = []
    monkeypatch.setattr(gms, "stamp_use", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(gmi, "default_store", lambda: object())

    class _Store:
        def authoritative_rows_for_keys(self, gym, proposed):
            if readback == "error":
                raise ConnectionError("authoritative readback unavailable")
            field = "caption" if readback == "changed_caption" else "source_media_asset_id"
            return [dict(persisted, **{field: "substituted"})]

    result = gbp_planner._recover_armed_drive_uses(
        GYM, GYM + "_gbp", _Store(), lambda m: None)
    assert result["ok"] is False
    assert result["use_ids"] == [entry["use_id"]]
    assert calls == []
    assert journal.get(entry["use_id"])["state"] == "consumption_pending"


@pytest.mark.parametrize("proof_row", [None, {"id": "different-landed-row"}])
def test_armed_recovery_requires_matching_durable_landed_proof(
        jdb, monkeypatch, proof_row):
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    persisted = _persisted_active(entry, row_id)
    _at_consuming_state(entry, persisted, "consumption_pending")
    _finalize(_make_stage_entry(row_id, row=entry["calendar_row"]))
    current = journal.get(entry["use_id"])
    if proof_row is None:
        current["landed_proof"]["calendar_row"] = None
    else:
        current["landed_proof"]["calendar_row"].update(proof_row)
    monkeypatch.setattr(journal, "get", lambda use_id: current)
    monkeypatch.setenv(FLAG, "1")
    import agent.gym_media_selector as gms
    calls = []
    monkeypatch.setattr(gms, "stamp_use", lambda *a, **k: calls.append((a, k)))
    assert not gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], dict(_pick(entry), store=object()),
        persisted, lambda m: None)
    assert calls == []
    assert current["state"] == "consumption_pending"


@pytest.mark.parametrize("corruption", ["incomplete", "batch_id", "tenant_id",
                                       "request_digest", "row_ids",
                                       "reservation_ids", "archived_old_row_ids"])
@pytest.mark.parametrize("state", ["write_intent", "unknown_result",
                                    "confirmed_landed", "consumption_pending"])
def test_corrupt_persisted_terminal_proof_never_authorizes_remote_use(
        jdb, monkeypatch, corruption, state):
    import sqlite3
    entry = _drive_entry()
    row_id = str(uuid.uuid4())
    persisted = _persisted_active(entry, row_id)
    bound = _finalize(_make_stage_entry(row_id, row=entry["calendar_row"]))
    _at_consuming_state(entry, persisted, state)
    proof = deepcopy(bound["finalize_receipt"])
    if corruption == "incomplete":
        proof = {}
    elif corruption in ("row_ids", "archived_old_row_ids"):
        proof[corruption] = [str(uuid.uuid4())]
    elif corruption == "reservation_ids":
        proof[corruption] = []
    else:
        proof[corruption] = "mismatched"
    # Simulate valid JSON corruption AFTER the guarded terminal transition.
    with sqlite3.connect(jdb) as conn:
        conn.execute("UPDATE gbp_forward_stage_journal SET finalize_receipt=? WHERE batch_id=?",
                     (json.dumps(proof), bound["batch_id"]))
    monkeypatch.setenv(FLAG, "1")
    assert not journal.forward_remote_use_allowed(bound["batch_id"])
    assert not _store(_FakeHttp()).forward_remote_use_settlement_allowed(bound["batch_id"])
    import agent.gym_media_selector as gms
    calls = []
    monkeypatch.setattr(gms, "stamp_use", lambda *a, **k: calls.append((a, k)))
    logs = []
    assert not gbp_planner._settle_armed_drive_landing(
        GYM, entry["calendar_row"], dict(_pick(entry), store=object()),
        persisted, logs.append)
    assert calls == []
    assert journal.get(entry["use_id"])["state"] == state
    assert any("exact terminal proof" in message for message in logs)


def test_finalized_helper_rechecks_frozen_request_digest(jdb):
    import sqlite3
    bound = _finalize(_make_stage_entry(str(uuid.uuid4())))
    assert journal.forward_remote_use_allowed(bound["batch_id"])
    # Terminal receipt fields still match their columns, but the immutable
    # request text no longer hashes to that digest.
    with sqlite3.connect(jdb) as conn:
        conn.execute("UPDATE gbp_forward_stage_journal SET request_text=? WHERE batch_id=?",
                     ("{}", bound["batch_id"]))
    assert not journal.forward_remote_use_allowed(bound["batch_id"])


def test_finalized_helper_accepts_shared_same_group_reservation_proof(jdb, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    rows = _rows(2)
    # The authoritative reservation RPC permits same-day logical siblings
    # with the same source to share one reservation. Keep the frozen group
    # explicit rather than equating a per-row receipt list with uniqueness.
    rows[1].update(logical_post_id=rows[0]["logical_post_id"],
                   post_date=rows[0]["post_date"],
                   source_media_asset_id="same-asset")
    rows[0]["source_media_asset_id"] = "same-asset"
    request_text = json.dumps(
        {"tenant_id": GYM, "old_rows": [],
         "members": [{"row": row, "observation": None} for row in rows]},
        sort_keys=True, separators=(",", ":"))
    entry = journal.record_forward_stage_intent({
        "batch_id": str(uuid.uuid4()), "tenant_id": GYM,
        "request_digest": hashlib.sha256(request_text.encode()).hexdigest(),
        "request_text": request_text, "member_row_ids": [row["id"] for row in rows],
        "old_row_ids": []})
    shared_id = str(uuid.uuid4())
    proof = _terminal_receipt(entry, reservation_ids=[shared_id, shared_id])
    finalized = journal.record_forward_finalized(
        entry["batch_id"], _status_payload(entry, "finalized", proof))
    assert finalized["finalize_receipt"]["reservation_ids"] == [shared_id, shared_id]
    assert journal.forward_remote_use_allowed(entry["batch_id"])
    assert _store(_FakeHttp()).forward_remote_use_settlement_allowed(entry["batch_id"])
