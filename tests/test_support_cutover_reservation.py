"""Adversarial tests for the durable support cutover reservation receipt.

Aligned to the frozen Portal DRAFT_0639 SQL (head
f7ca4a04175be10bf6d401e22e91660c4e63b120): Echo must fail closed on every
deviation — missing RPC, released/resumed reservation, stale epoch, wrong
pointer, pinned/duplicate tuple mismatch, live-lane defect, page echo
mismatch, mid-scan release/change/error, ABA local control flip and active
operations appearing during reads.
"""
import json
import os
import pytest

from agent import support_sender_fence as fence

RESERVATION_ID = "12345678-1234-5678-1234-567812345678"
EPOCH = 7
INV = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MSG = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TICKET = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SEND_OP = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
CLOSE_OP = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
SEND_GEN = 3
CLOSE_GEN = 5
LANES = ("support-resolution-send", "support-ticket-close")
_TUPLES = {"support-resolution-send": (SEND_GEN, SEND_OP),
           "support-ticket-close": (CLOSE_GEN, CLOSE_OP)}


def _pin(generation, operation_id):
    return {"generation": generation, "operation_id": operation_id}


def _pinned(send=None, close=None):
    return {"support-resolution-send": send or _pin(SEND_GEN, SEND_OP),
            "support-ticket-close": close or _pin(CLOSE_GEN, CLOSE_OP)}


def _live_lane(lane_name, **overrides):
    generation, operation_id = _TUPLES[lane_name]
    live = {"lane": lane_name, "paused": True, "generation": generation,
            "operation_id": operation_id, "reservation_id": RESERVATION_ID,
            "unresolved": 0, "pinned_generation": generation,
            "pinned_operation_id": operation_id, "drained": True}
    live.update(overrides)
    return live


def _status(**overrides):
    status = {"reservation_id": RESERVATION_ID, "epoch": EPOCH,
              "owner_epoch": EPOCH, "owner": "cutover-owner",
              "pinned": _pinned(),
              "send_generation": SEND_GEN, "send_operation_id": SEND_OP,
              "close_generation": CLOSE_GEN, "close_operation_id": CLOSE_OP,
              "support-resolution-send": _live_lane("support-resolution-send"),
              "support-ticket-close": _live_lane("support-ticket-close")}
    status.update(overrides)
    return status


def _page(lane, rows=(), **overrides):
    page = {"reservation_id": RESERVATION_ID, "epoch": EPOCH,
            "owner_epoch": EPOCH, "pinned": _pinned(),
            "send_generation": SEND_GEN, "send_operation_id": SEND_OP,
            "close_generation": CLOSE_GEN, "close_operation_id": CLOSE_OP,
            "lane": lane, "limit": fence._INVENTORY_PAGE, "returned": len(rows),
            "has_more": False,
            "next_after_started": rows[-1]["started_at"] if rows else None,
            "next_after_invocation": rows[-1]["invocation_id"] if rows else None,
            "invocations": list(rows)}
    page.update(overrides)
    return page


def _invocation(inv_id=INV, lane="support-resolution-send", outcome="completed",
                unresolved=False, started="2026-10-10T00:00:00+00:00"):
    return {"invocation_id": inv_id, "lane": lane, "outcome": outcome,
            "unresolved": unresolved, "generation": 1,
            "started_at": started,
            "ended_at": None if outcome == "running" else started,
            "ticket_id": TICKET, "request_version": 3, "message_id": MSG}


class GuardedBus:
    def __init__(self, statuses=None, pages=None, hook=None):
        self._statuses = list(statuses) if statuses is not None else None
        self._pages = pages or {}
        self._hook = hook
        self.inventory_calls = []

    def support_uncertain_outbound(self, limit=1000):
        return []

    def outbox(self, status, limit):
        return []

    def support_cutover_reservation_status(self, reservation_id, epoch):
        if self._hook:
            self._hook()
        if self._statuses:
            item = self._statuses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _status(reservation_id=reservation_id, epoch=epoch,
                       owner_epoch=epoch)

    def support_cutover_reservation_inventory(self, reservation_id, epoch,
                                              lane, *, limit,
                                              after_started=None,
                                              after_invocation=None):
        self.inventory_calls.append((lane, after_started, after_invocation))
        pages = self._pages.get(lane)
        if pages is None:
            return _page(lane, limit=limit)
        if isinstance(pages, list):
            return pages.pop(0) if pages else _page(lane, limit=limit)
        return pages


@pytest.fixture
def pause(monkeypatch, tmp_path):
    path = tmp_path / "control.json"
    monkeypatch.setenv("SUPPORT_MESSAGES_FENCE_ENABLED", "true")
    monkeypatch.setenv("SUPPORT_MESSAGES_FENCE_CONTROL_FILE", str(path))
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "test-sha")
    monkeypatch.delenv("SUPPORT_CUTOVER_RESERVATION_ID", raising=False)
    monkeypatch.delenv("SUPPORT_CUTOVER_OWNER_EPOCH", raising=False)
    path.write_text(json.dumps({"paused": True, "generation": "gen-1"}))
    return path


def test_held_reservation_empty_inventory_drains_admission(pause):
    result = fence.receipt(GuardedBus(), reservation_id=RESERVATION_ID,
                           owner_epoch=EPOCH)
    assert result["blockers"] == []
    assert result["reservation_held"] is True
    assert result["admission_drained"] is True
    assert result["effects_reconciled"] is True
    assert result["local_operations_drained"] is True
    assert result["local_drained"] is True
    assert result["fleet_drained"] is False


def test_missing_cutover_rpc_blocks(pause):
    class Bus:
        def support_uncertain_outbound(self, limit=1000):
            return []

        def outbox(self, status, limit):
            return []

    result = fence.receipt(Bus(), reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "reservation_cutover_status_unavailable" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["local_drained"] is False
    assert result["fleet_drained"] is False


@pytest.mark.parametrize("override", [
    {"reservation_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
    {"epoch": EPOCH + 1},
    {"owner_epoch": EPOCH + 1},
    {"epoch": str(EPOCH)},
    {"epoch": 0},
    {"owner_epoch": None},
    {"owner": ""},
    {"owner": 7},
    {"pinned": {"support-resolution-send": _pin(SEND_GEN, SEND_OP)}},  # close pin missing
    {"pinned": _pinned(close=_pin(CLOSE_GEN, "ffffffff-ffff-4fff-8fff-ffffffffffff"))},
    {"pinned": _pinned(send=_pin(SEND_GEN, "not-a-uuid"))},
    {"pinned": _pinned(send={"operation_id": SEND_OP})},  # generation omitted
    {"send_generation": SEND_GEN + 1},
    {"close_operation_id": SEND_OP},
    {"support-ticket-close": _live_lane("support-ticket-close", paused=False)},
    {"support-ticket-close": _live_lane("support-ticket-close", drained=False)},
    {"support-resolution-send": _live_lane("support-resolution-send", unresolved=1)},
    {"support-resolution-send": _live_lane("support-resolution-send", unresolved=True)},
    {"support-resolution-send": _live_lane("support-resolution-send",
                                           reservation_id=SEND_OP)},
    {"support-resolution-send": _live_lane("support-resolution-send", lane="support-ticket-close")},
    {"support-resolution-send": _live_lane("support-resolution-send",
                                           generation=SEND_GEN + 1)},
    {"support-ticket-close": _live_lane("support-ticket-close",
                                        pinned_operation_id=SEND_OP)},
])
def test_invalid_or_released_reservation_blocks(pause, override):
    bus = GuardedBus(statuses=[_status(**override)])
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "reservation_cutover_invalid" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["local_drained"] is False


@pytest.mark.parametrize("missing", [
    "reservation_id", "epoch", "owner_epoch", "owner", "pinned",
    "send_generation", "send_operation_id", "close_generation",
    "close_operation_id", "support-resolution-send", "support-ticket-close",
])
def test_status_missing_field_blocks(pause, missing):
    status = _status()
    del status[missing]
    bus = GuardedBus(statuses=[status])
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "reservation_cutover_invalid" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["local_drained"] is False


@pytest.mark.parametrize("missing", [
    "lane", "paused", "drained", "generation", "operation_id",
    "reservation_id", "unresolved", "pinned_generation", "pinned_operation_id",
])
def test_status_live_lane_missing_field_blocks(pause, missing):
    status = _status()
    del status["support-resolution-send"][missing]
    bus = GuardedBus(statuses=[status])
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "reservation_cutover_invalid" in result["blockers"]
    assert result["reservation_held"] is False


def test_reservation_change_during_scans_blocks(pause):
    changed = _status()
    changed["pinned"]["support-resolution-send"]["generation"] = SEND_GEN + 1
    changed["send_generation"] = SEND_GEN + 1
    changed["support-resolution-send"]["generation"] = SEND_GEN + 1
    changed["support-resolution-send"]["pinned_generation"] = SEND_GEN + 1
    bus = GuardedBus(statuses=[_status(), changed])
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "reservation_cutover_changed" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["local_drained"] is False


def test_reservation_release_during_scans_blocks(pause):
    bus = GuardedBus(statuses=[_status(), RuntimeError("released")])
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert any(b.startswith("database_read:") for b in result["blockers"])
    assert result["reservation_held"] is False
    assert result["local_drained"] is False
    assert result["fleet_drained"] is False


@pytest.mark.parametrize("page_override", [
    {"reservation_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
    {"epoch": EPOCH + 1},
    {"owner_epoch": EPOCH + 1},
    {"owner_epoch": str(EPOCH)},
    {"pinned": {"support-resolution-send": _pin(SEND_GEN, SEND_OP)}},
    {"pinned": _pinned(close=_pin(CLOSE_GEN + 1, CLOSE_OP))},
    {"send_generation": SEND_GEN + 1},
    {"send_operation_id": CLOSE_OP},
    {"close_generation": CLOSE_GEN - 1},
    {"close_operation_id": SEND_OP},
])
def test_page_echo_or_pin_mismatch_blocks(pause, page_override):
    lane = "support-resolution-send"
    bus = GuardedBus(pages={lane: _page(lane, **page_override)})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" in result["blockers"]
    assert result["admission_drained"] is False
    assert result["local_drained"] is False


@pytest.mark.parametrize("missing", [
    "reservation_id", "epoch", "owner_epoch", "pinned", "send_generation",
    "send_operation_id", "close_generation", "close_operation_id", "lane",
    "limit", "returned", "has_more", "invocations",
])
def test_page_missing_field_blocks(pause, missing):
    lane = "support-ticket-close"
    page = _page(lane)
    del page[missing]
    bus = GuardedBus(pages={lane: page})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" in result["blockers"]
    assert result["admission_drained"] is False


@pytest.mark.parametrize("lane,field,value", [
    ("support-resolution-send", "flat", True),
    ("support-resolution-send", "flat", 1.0),
    ("support-ticket-close", "flat", True),
    ("support-ticket-close", "flat", 1.0),
    ("support-resolution-send", "generation", True),
    ("support-resolution-send", "generation", 1.0),
    ("support-ticket-close", "pinned_generation", True),
    ("support-ticket-close", "pinned_generation", 1.0),
])
def test_non_int_generation_equal_to_pin_blocks(pause, lane, field, value):
    prefix = "send" if lane == "support-resolution-send" else "close"
    status = _status()
    status["pinned"][lane]["generation"] = 1
    status[f"{prefix}_generation"] = 1
    status[lane]["generation"] = 1
    status[lane]["pinned_generation"] = 1
    if field == "flat":
        status[f"{prefix}_generation"] = value
    else:
        status[lane][field] = value
    bus = GuardedBus(statuses=[status])
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "reservation_cutover_invalid" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["local_drained"] is False


@pytest.mark.parametrize("prefix,value", [
    ("send", True), ("send", 1.0),
    ("close", True), ("close", 1.0),
])
def test_page_non_int_generation_equal_to_pin_blocks(pause, prefix, value):
    lane = "support-resolution-send"
    pinned_lane = lane if prefix == "send" else "support-ticket-close"
    status = _status()
    status["pinned"][pinned_lane]["generation"] = 1
    status[f"{prefix}_generation"] = 1
    status[pinned_lane]["generation"] = 1
    status[pinned_lane]["pinned_generation"] = 1
    page = _page(lane, pinned=status["pinned"],
                 send_generation=status["send_generation"],
                 close_generation=status["close_generation"])
    page[f"{prefix}_generation"] = value
    bus = GuardedBus(statuses=[status, status], pages={lane: page})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" in result["blockers"]
    assert result["admission_drained"] is False
    assert result["local_drained"] is False


@pytest.mark.parametrize("missing", ["next_after_started", "next_after_invocation"])
def test_empty_page_missing_cursor_key_blocks(pause, missing):
    lane = "support-resolution-send"
    page = _page(lane)
    del page[missing]
    bus = GuardedBus(pages={lane: page})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" in result["blockers"]
    assert result["admission_drained"] is False
    assert result["local_drained"] is False


def test_multi_page_inventory_cursor_preserved(pause, monkeypatch):
    monkeypatch.setattr(fence, "_INVENTORY_PAGE", 2)
    lane = "support-resolution-send"
    rows = [_invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                        outcome="unknown", unresolved=True,
                        started="2026-10-10T00:00:00+00:00"),
            _invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                        outcome="unknown", unresolved=True,
                        started="2026-10-10T01:00:00+00:00")]
    page1 = _page(lane, rows, limit=2, returned=2, has_more=True)
    page2 = _page(lane, limit=2)
    bus = GuardedBus(pages={lane: [page1, page2]})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" not in result["blockers"]
    assert any(b.startswith("admission_unknown") for b in result["blockers"])
    assert bus.inventory_calls[0] == (lane, None, None)
    assert bus.inventory_calls[1] == (lane, "2026-10-10T01:00:00+00:00",
                                      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2")


def test_second_page_must_advance_past_prior_cursor(pause, monkeypatch):
    monkeypatch.setattr(fence, "_INVENTORY_PAGE", 2)
    lane = "support-resolution-send"
    first = [_invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                         outcome="unknown", unresolved=True,
                         started="2026-10-10T00:00:00+00:00"),
             _invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                         outcome="unknown", unresolved=True,
                         started="2026-10-10T01:00:00+00:00")]
    # A unique invocation on the next page still violates the keyset cursor
    # when its timestamp is earlier than the last row of the previous page.
    rewind = _invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
                         outcome="unknown", unresolved=True,
                         started="2026-10-10T00:30:00+00:00")
    bus = GuardedBus(pages={lane: [
        _page(lane, first, limit=2, returned=2, has_more=True),
        _page(lane, [rewind], limit=2, returned=1)]})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" in result["blockers"]
    assert result["admission_drained"] is False


def test_noncanonical_timestamp_ordering_uses_rfc3339(pause):
    lane = "support-resolution-send"
    # Chronologically increasing but lexically decreasing: a lexical compare
    # would wrongly reject this page.
    rows = [_invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                        outcome="unknown", unresolved=True,
                        started="2026-10-10T10:00:00+00:00"),
            _invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                        outcome="unknown", unresolved=True,
                        started="2026-10-10T09:30:00-01:00")]
    bus = GuardedBus(pages={lane: _page(lane, rows)})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" not in result["blockers"]
    assert any(b.startswith("admission_unknown") for b in result["blockers"])


def test_chronologically_reversed_noncanonical_page_blocks(pause):
    lane = "support-resolution-send"
    # Lexically increasing but chronologically reversed: only parsed ordering
    # catches this.
    rows = [_invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                        outcome="unknown", unresolved=True,
                        started="2026-10-10T09:30:00-01:00"),
            _invocation(inv_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                        outcome="unknown", unresolved=True,
                        started="2026-10-10T10:00:00+00:00")]
    bus = GuardedBus(pages={lane: _page(lane, rows)})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" in result["blockers"]
    assert result["admission_drained"] is False


def test_aba_control_flip_during_reads_blocks(pause):
    def flip_away_and_back():
        pause.write_text(json.dumps({"paused": True, "generation": "gen-2"}))
        fence._control_sequence()  # in-process observer sees the intermediate state
        pause.write_text(json.dumps({"paused": True, "generation": "gen-1"}))

    result = fence.receipt(GuardedBus(hook=flip_away_and_back),
                           reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "control_changed_during_read" in result["blockers"]
    assert result["local_drained"] is False


def test_unobserved_atomic_control_aba_blocks(pause, tmp_path):
    changed = False

    def replace_control():
        nonlocal changed
        if changed:
            return
        changed = True
        for generation in ("gen-2", "gen-1"):
            replacement = tmp_path / f"replacement-{generation}.json"
            replacement.write_text(json.dumps({"paused": True, "generation": generation}))
            os.replace(replacement, pause)

    result = fence.receipt(GuardedBus(hook=replace_control),
                           reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "control_file_changed_or_unavailable" in result["blockers"]
    assert result["local_operations_drained"] is False
    assert result["local_drained"] is False


def test_env_only_control_cannot_attest_cutover(monkeypatch, pause):
    monkeypatch.delenv("SUPPORT_MESSAGES_FENCE_CONTROL_FILE")
    monkeypatch.setenv("SUPPORT_MESSAGES_FENCE_PAUSED", "true")
    monkeypatch.setenv("SUPPORT_MESSAGES_FENCE_GENERATION", "gen-1")
    result = fence.receipt(GuardedBus(), reservation_id=RESERVATION_ID,
                           owner_epoch=EPOCH)
    assert "control_file_changed_or_unavailable" in result["blockers"]
    assert result["local_drained"] is False


def test_active_operation_appearing_during_reads_blocks(pause):
    def activate():
        with fence._LOCK:
            fence._ACTIVE["support-resolution-send"] = 1

    bus = GuardedBus(hook=activate)
    try:
        result = fence.receipt(bus, reservation_id=RESERVATION_ID,
                               owner_epoch=EPOCH)
    finally:
        with fence._LOCK:
            fence._ACTIVE.pop("support-resolution-send", None)
    assert "active_operations_changed_during_read" in result["blockers"]
    assert result["active"] == {"support-resolution-send": 1}
    assert result["local_operations_drained"] is False
    assert result["local_drained"] is False


@pytest.mark.parametrize("reservation_id, owner_epoch", [
    ("not-a-uuid", EPOCH),
    ("{" + RESERVATION_ID + "}", EPOCH),
    (123, EPOCH),
    (None, EPOCH),
    (RESERVATION_ID, 0),
    (RESERVATION_ID, -1),
    (RESERVATION_ID, "7"),
    (RESERVATION_ID, True),
    (RESERVATION_ID, None),
])
def test_malformed_uuid_or_epoch_blocks(pause, reservation_id, owner_epoch):
    result = fence.receipt(GuardedBus(), reservation_id=reservation_id,
                           owner_epoch=owner_epoch)
    assert "reservation_credentials_malformed" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["local_drained"] is False


def test_env_credentials_used_when_args_absent(pause, monkeypatch):
    monkeypatch.setenv("SUPPORT_CUTOVER_RESERVATION_ID", RESERVATION_ID)
    monkeypatch.setenv("SUPPORT_CUTOVER_OWNER_EPOCH", str(EPOCH))
    result = fence.receipt(GuardedBus())
    assert result["blockers"] == []
    assert result["reservation_held"] is True


def test_completed_send_stays_blocked_with_held_reservation(pause):
    lane = "support-resolution-send"
    bus = GuardedBus(pages={lane: _page(lane, [_invocation()])})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert any(blocker.startswith("admission_external_verification_required")
               or blocker.startswith("admission_completed_unverified")
               for blocker in result["blockers"]), result["blockers"]
    assert result["reservation_held"] is True
    assert result["admission_drained"] is True
    assert result["effects_reconciled"] is False
    assert result["local_drained"] is False
    assert result["fleet_drained"] is False


def test_running_invocation_blocks(pause):
    lane = "support-resolution-send"
    bus = GuardedBus(pages={lane: _page(lane, [_invocation(outcome="running",
                                                           unresolved=True)])})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert any(blocker.startswith("admission_running") for blocker in result["blockers"])
    assert f"admission_inventory_inconsistent:{lane}" in result["blockers"]
    assert result["admission_drained"] is False
    assert result["local_drained"] is False


def test_no_reservation_keeps_legacy_cross_lane_blocker(pause):
    class LegacyBus:
        def support_uncertain_outbound(self, limit=1000):
            return []

        def outbox(self, status, limit):
            return []

        def support_admission_status_lane(self, lane="support-resolution-send"):
            return {"lane": lane, "generation": 1, "unresolved": 0,
                    "paused": True, "drained": True, "operation_id": "op-1"}

        def support_admission_inventory(self, lane, *, limit, after_started=None,
                                        after_invocation=None):
            return {"lane": lane, "generation": 1, "operation_id": "op-1",
                    "paused": True, "limit": fence._INVENTORY_PAGE, "returned": 0,
                    "has_more": False, "next_after_started": None,
                    "next_after_invocation": None, "invocations": []}

    result = fence.receipt(LegacyBus())
    assert "admission_cross_lane_snapshot_unverified" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["admission_drained"] is False
    assert result["local_drained"] is False
    assert result["fleet_drained"] is False
