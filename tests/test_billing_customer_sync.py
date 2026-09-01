"""billing_customer_sync: the wire that makes the publish billing gate reachable.

The audit found the gate armed in production and structurally inert because nothing ever
wrote gyms.stripe_customer_id. These pin the sync's rails: flag-gated, never blanks a
stored id, never invents a mapping, and an unavailable shared plane is a no-op.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db as _db  # noqa: E402
from agent.jobs import billing_customer_sync as bcs  # noqa: E402


class _FakeStore:
    def __init__(self, uuids, billing_rows):
        self._uuids = uuids
        self._rows = billing_rows

    def resolve_gym_uuid(self, base):
        return self._uuids.get(base)


def _patch_fetch(monkeypatch, rows):
    monkeypatch.setattr(bcs, "fetch_customer_ids", lambda store: rows)


class _Acct:
    def __init__(self, key):
        self.key = key


def _db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_BILLING_CUSTOMER_SYNC", "true")


def test_flag_off_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("AGENT_BILLING_CUSTOMER_SYNC", raising=False)
    out = bcs.run(store=_FakeStore({}, {}), accounts=["eng"])
    assert out["skipped"] == "flag off"


def test_writes_the_customer_id_onto_the_echo_row(tmp_path, monkeypatch):
    _db_env(tmp_path, monkeypatch)
    _db.gym_upsert("eng", display_name="ENG")
    _patch_fetch(monkeypatch, {"uuid-eng": "cus_live_1"})
    fired = []
    out = bcs.run(store=_FakeStore({"eng": "uuid-eng"}, {}), accounts=["eng"],
                  alert=lambda m: fired.append(m))
    assert out["written"] == 1 and out["written_gyms"] == ["eng"]
    assert (_db.gym_get("eng") or {})["stripe_customer_id"] == "cus_live_1"
    # Arming the gate's reach is never silent.
    assert fired and "billing gate" in fired[0]


def test_never_blanks_a_stored_id_when_upstream_has_none(tmp_path, monkeypatch):
    _db_env(tmp_path, monkeypatch)
    _db.gym_upsert("eng", display_name="ENG", stripe_customer_id="cus_keep")
    _patch_fetch(monkeypatch, {})           # upstream returned nothing for this gym
    out = bcs.run(store=_FakeStore({"eng": "uuid-eng"}, {}), accounts=["eng"])
    assert out["written"] == 0 and out["no_customer"] == ["eng"]
    assert (_db.gym_get("eng") or {})["stripe_customer_id"] == "cus_keep"


def test_never_invents_a_mapping_for_an_unresolvable_gym(tmp_path, monkeypatch):
    _db_env(tmp_path, monkeypatch)
    _db.gym_upsert("mystery", display_name="Mystery")
    _patch_fetch(monkeypatch, {"uuid-eng": "cus_live_1"})
    out = bcs.run(store=_FakeStore({}, {}), accounts=["mystery"])
    assert out["written"] == 0 and out["unmapped"] == ["mystery"]
    assert not (_db.gym_get("mystery") or {}).get("stripe_customer_id")


def test_unavailable_shared_plane_is_a_no_op(tmp_path, monkeypatch):
    _db_env(tmp_path, monkeypatch)
    monkeypatch.setattr(bcs, "_store", lambda: None)
    out = bcs.run(accounts=["eng"])
    assert out["skipped"] == "shared plane unavailable"


def test_apply_false_reports_without_writing(tmp_path, monkeypatch):
    _db_env(tmp_path, monkeypatch)
    _db.gym_upsert("eng", display_name="ENG")
    _patch_fetch(monkeypatch, {"uuid-eng": "cus_live_1"})
    out = bcs.run(apply=False, store=_FakeStore({"eng": "uuid-eng"}, {}),
                  accounts=["eng"])
    assert out["written"] == 1
    assert not (_db.gym_get("eng") or {}).get("stripe_customer_id")


def test_base_strips_platform_suffix():
    assert bcs._base(_Acct("eng_ig")) == "eng"
    assert bcs._base(_Acct("eng_fb")) == "eng"
    assert bcs._base(_Acct("lasso")) == "lasso"
