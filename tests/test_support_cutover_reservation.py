"""Adversarial tests for the durable support cutover reservation receipt.

The Portal SQL contract is PROPOSED (pending finalization); Echo must fail
closed on every deviation: missing RPC, released/resumed reservation, stale
pointer, page echo mismatch, mid-scan lane change, ABA local control flip and
active operations appearing during reads.
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


def _pin(generation=1, operation_id="op-res-1"):
    return {"generation": generation, "operation_id": operation_id,
            "paused": True, "unresolved": 0}


def _status(**overrides):
    status = {"reservation_id": RESERVATION_ID, "owner_epoch": EPOCH,
              "state": "held", "is_current": True,
              "lanes": {lane: _pin() for lane in fence._LANES}}
    status.update(overrides)
    return status


def _page(lane, rows=(), **overrides):
    pins = {l: _pin() for l in fence._LANES}
    page = {"reservation_id": RESERVATION_ID, "owner_epoch": EPOCH, "lane": lane,
            "generation": pins[lane]["generation"],
            "operation_id": pins[lane]["operation_id"], "paused": True,
            "lanes": pins, "limit": fence._INVENTORY_PAGE, "returned": len(rows),
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

    def support_uncertain_outbound(self, limit=1000):
        return []

    def outbox(self, status, limit):
        return []

    def support_cutover_status(self, reservation_id, owner_epoch):
        if self._hook:
            self._hook()
        if self._statuses:
            return self._statuses.pop(0)
        return _status(reservation_id=reservation_id, owner_epoch=owner_epoch)

    def support_admission_inventory_guarded(self, reservation_id, owner_epoch,
                                            lane, *, limit, after_started=None,
                                            after_invocation=None):
        page = self._pages.get(lane)
        return page if page is not None else _page(lane)


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
    {"state": "released"},
    {"state": "resumed"},
    {"is_current": False},
    {"reservation_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
    {"owner_epoch": EPOCH + 1},
    {"lanes": {"support-resolution-send": _pin()}},  # close lane pin missing
    {"lanes": {lane: _pin() for lane in fence._LANES} |
              {"support-ticket-close": _pin() | {"unresolved": 1}}},
    {"lanes": {lane: _pin() for lane in fence._LANES} |
              {"support-resolution-send": _pin() | {"paused": False}}},
])
def test_invalid_or_released_reservation_blocks(pause, override):
    bus = GuardedBus(statuses=[_status(**override)])
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "reservation_cutover_invalid" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["local_drained"] is False


def test_reservation_change_during_scans_blocks(pause):
    changed = _status()
    changed["lanes"]["support-resolution-send"] = _pin(generation=2)
    bus = GuardedBus(statuses=[_status(), changed])
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert "reservation_cutover_changed" in result["blockers"]
    assert result["reservation_held"] is False
    assert result["local_drained"] is False


@pytest.mark.parametrize("page_override", [
    {"reservation_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
    {"owner_epoch": EPOCH + 1},
    {"generation": 2},
    {"operation_id": "op-other"},
    {"paused": False},
    {"lanes": {"support-resolution-send": _pin()}},
    {"lanes": {lane: _pin() for lane in fence._LANES} |
              {"support-ticket-close": _pin(operation_id="op-swapped")}},
])
def test_page_echo_or_pin_mismatch_blocks(pause, page_override):
    lane = "support-resolution-send"
    bus = GuardedBus(pages={lane: _page(lane, **page_override)})
    result = fence.receipt(bus, reservation_id=RESERVATION_ID, owner_epoch=EPOCH)
    assert f"admission_inventory_malformed:{lane}" in result["blockers"]
    assert result["admission_drained"] is False
    assert result["local_drained"] is False


def test_lane_status_change_inside_page_generation_blocks(pause):
    lane = "support-ticket-close"
    bus = GuardedBus(pages={lane: _page(lane, generation=9)})
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
            status = self.support_admission_status_lane(lane)
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
