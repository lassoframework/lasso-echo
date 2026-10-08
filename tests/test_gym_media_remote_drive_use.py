"""Selector stamp_use integration with agent.remote_drive_use (default OFF).

Disposable SQLite journals and a fake authority only; no production calls.
"""
from copy import deepcopy
import json
import sqlite3
import sys
import os
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config as settings  # noqa: E402
from agent import gym_media_selector as sel  # noqa: E402
from agent import remote_drive_use as remote  # noqa: E402
from agent.local_inventory_mutation import MutationHold  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset, make_source  # noqa: E402

EPOCH = str(uuid.uuid4())
USE_ID = str(uuid.uuid4())
STAMPED = "2026-10-08T12:00:00+00:00"


def asset_row(**over):
    row = make_asset("a1", gym_id="pierce", source_id="src1")
    row["drive_use_version"] = 1
    row.update(over)
    return row


def source_row(**over):
    row = make_source("src1", gym_id="pierce")
    row["drive_use_version"] = 2
    row.update(over)
    return row


class Authority:
    """remote_drive_use.DriveUseAuthority stand-in (never any network)."""

    def __init__(self, lose=False):
        self.calls = []
        self.result = None
        self.lose = lose

    def apply_use(self, request):
        self.calls.append("apply")
        after = dict(request["asset_before"], used_count=1,
                     last_used_at=STAMPED,
                     drive_use_version=request["asset_before"]["drive_use_version"] + 1)
        self.result = dict(state="applied", request=deepcopy(request),
                           asset_after=after,
                           source_after=deepcopy(request["source_before"]))
        if self.lose:
            raise OSError("lost commit ack")
        return self.result

    def use_receipt(self, request):
        self.calls.append("receipt")
        if self.result is None:
            raise OSError("no authoritative receipt")
        return self.result

    def close(self):
        pass


@pytest.fixture
def armed(tmp_path, monkeypatch):
    gym = tmp_path / "library" / "pierce"
    gym.mkdir(parents=True)
    db_file = tmp_path / "echo.db"
    sqlite3.connect(db_file).close()
    monkeypatch.setenv("AGENT_DB_PATH", str(db_file))
    monkeypatch.setenv("AGENT_REMOTE_DRIVE_USE_CAS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_INVENTORY_MUTATION_EPOCH", EPOCH)
    monkeypatch.setattr(settings, "LIBRARY_PATH", str(tmp_path / "library"))
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    authority = Authority()
    monkeypatch.setattr(remote.DriveUseAuthority, "from_environment",
                        classmethod(lambda cls: authority))
    store = FakeMediaStore(sources=[source_row()], assets=[asset_row()])
    return {"kv": kv, "authority": authority, "db_file": db_file, "store": store}


def stamp(store=None, use_id=USE_ID, a_row=None, s_row=None, gym="pierce"):
    a_row = a_row if a_row is not None else asset_row()
    s_row = s_row if s_row is not None else source_row()
    sel.stamp_use({"id": a_row["id"]}, gym, "2026-10-10", store=store,
                  use_id=use_id, asset_row=a_row, source_row=s_row)


def journal_states(db_file):
    with sqlite3.connect(db_file) as c:
        try:
            return c.execute("select state from remote_drive_use_attempt").fetchall()
        except sqlite3.OperationalError:
            return []


def test_default_off_legacy_stamp_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REMOTE_DRIVE_USE_CAS_ENABLED", raising=False)
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    store = FakeMediaStore(sources=[source_row()], assets=[asset_row()])
    sel.stamp_use(asset_row(), "pierce", "2026-10-10", store=store,
                  now=None, use_id=None)
    assert store.updates == [("a1", {"used_count": 1,
                                     "last_used_at": store.updates[0][1]["last_used_at"]})]
    records = json.loads(kv["gym_media_use:pierce:2026-10-10"])
    assert len(records) == 1 and records[0]["asset_id"] == "a1"
    assert "use_id" not in records[0]


def test_armed_success_preserves_receipt_and_local_bookkeeping(armed):
    stamp(store=armed["store"])
    assert armed["authority"].calls == ["apply"]
    # The ID-only update_asset form is never used in armed mode.
    assert armed["store"].updates == []
    records = json.loads(armed["kv"]["gym_media_use:pierce:2026-10-10"])
    assert records == [{"asset_id": "a1", "gym_id": "pierce", "use_id": USE_ID,
                        "prev_used_count": 0, "prev_last_used_at": None,
                        "staged_at": STAMPED, "rolled_back": False}]
    assert journal_states(armed["db_file"]) == [("confirmed",)]


def test_armed_replay_same_use_id_replays_receipt(armed):
    stamp(store=armed["store"])
    stamp(store=armed["store"])
    # The second stamp replays the confirmed journal receipt; no new remote call.
    assert armed["authority"].calls == ["apply"]
    records = json.loads(armed["kv"]["gym_media_use:pierce:2026-10-10"])
    assert len(records) == 1 and records[0]["use_id"] == USE_ID


@pytest.mark.parametrize("change", ["used", "source_gym", "hash", "version"])
def test_armed_stale_snapshot_holds_before_any_mutation(armed, change):
    a_row, s_row = asset_row(), source_row()
    if change == "used":
        a_row["used_count"] = 1
    if change == "source_gym":
        s_row["gym_id"] = "other"
    if change == "hash":
        a_row["content_hash"] = ""
    if change == "version":
        a_row["drive_use_version"] = True
    with pytest.raises(MutationHold, match="request_invalid"):
        stamp(store=armed["store"], a_row=a_row, s_row=s_row)
    assert armed["authority"].calls == []
    assert armed["store"].updates == []
    assert armed["kv"] == {}
    assert journal_states(armed["db_file"]) == []


def test_armed_lost_remote_ack_holds_then_recovers_by_same_id_readback(armed, monkeypatch):
    losing = Authority(lose=True)
    monkeypatch.setattr(remote.DriveUseAuthority, "from_environment",
                        classmethod(lambda cls: losing))
    with pytest.raises(MutationHold, match="outcome_unknown"):
        stamp(store=armed["store"])
    # Unknown remote outcome: no local bookkeeping, no store mutation.
    assert armed["kv"] == {}
    assert armed["store"].updates == []
    assert journal_states(armed["db_file"]) == [("unknown",)]
    # Retry with the SAME use id: receipt readback only, never a fresh apply.
    stamp(store=armed["store"])
    assert losing.calls == ["apply", "receipt"]
    records = json.loads(armed["kv"]["gym_media_use:pierce:2026-10-10"])
    assert records[0]["use_id"] == USE_ID


def test_armed_local_kv_failure_propagates_and_receipt_survives(armed, monkeypatch):
    def boom(k, v):
        raise RuntimeError("kv write failed")
    monkeypatch.setattr("agent.db.kv_set", boom)
    with pytest.raises(RuntimeError, match="kv write failed"):
        stamp(store=armed["store"])
    assert armed["store"].updates == []
    # The remote receipt is durably confirmed; a retry replays it without a new
    # remote apply and can then land the local bookkeeping.
    assert journal_states(armed["db_file"]) == [("confirmed",)]
    monkeypatch.setattr("agent.db.kv_set",
                        lambda k, v: armed["kv"].__setitem__(k, v))
    stamp(store=armed["store"])
    assert armed["authority"].calls == ["apply"]
    assert json.loads(armed["kv"]["gym_media_use:pierce:2026-10-10"])


def test_armed_restore_unstaged_holds_unsupported(armed):
    stamp(store=armed["store"])
    with pytest.raises(MutationHold, match="release_unavailable"):
        sel.rollback_use("pierce", "2026-10-10", store=armed["store"],
                         restore_unstaged=True)
    # No counter restoration: the remote apply stays the only stamp.
    assert armed["store"].updates == []
    assert armed["authority"].calls == ["apply"]


def test_armed_ordinary_deny_is_bookkeeping_only(armed):
    stamp(store=armed["store"])
    assert sel.rollback_use("pierce", "2026-10-10", store=armed["store"]) is True
    records = json.loads(armed["kv"]["gym_media_use:pierce:2026-10-10"])
    assert records[0]["rolled_back"] is True
    assert records[0]["use_id"] == USE_ID
    assert armed["store"].updates == []


@pytest.mark.parametrize("kwargs", [{"use_id": ""}, {"use_id": None, "asset_row": None},
                                    {"source_row": None}])
def test_armed_without_stable_id_or_snapshots_fails_closed(armed, kwargs):
    a_row, s_row = asset_row(), source_row()
    use_id = kwargs.get("use_id", USE_ID)
    a = kwargs.get("asset_row", a_row)
    s = kwargs.get("source_row", s_row)
    with pytest.raises(MutationHold, match="snapshot_required"):
        sel.stamp_use({"id": "a1"}, "pierce", "2026-10-10",
                      store=armed["store"], use_id=use_id,
                      asset_row=a, source_row=s)
    assert armed["authority"].calls == []
    assert armed["store"].updates == []
    assert armed["kv"] == {}
