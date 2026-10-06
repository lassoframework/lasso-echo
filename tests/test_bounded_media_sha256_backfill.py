"""Offline bounds, raw-byte identity, tenant safety, resume and interruption."""
import copy
import hashlib
import json
import os
import time

import pytest

from scripts.bounded_media_sha256_backfill import run
from tests.gym_media_fakes import bound_review_fields

BLOB = b"bounded raw photo identity"


class Store:
    def __init__(self):
        self.row = {"id": "photo1", "gym_id": "gym1", "source_id": "source1", "kind": "photo", "title": "photo.jpg"}
        self.row.update(bound_review_fields("photo1", "gym1", hashlib.md5(BLOB).hexdigest()))
        self.source = {"id": "source1", "gym_id": "gym1", "active": True}
        self.writes = []

    def get_asset(self, _):
        return copy.deepcopy(self.row)

    def get_source(self, _):
        return copy.deepcopy(self.source)

    def update_moderation_sha256(self, gym, asset, evidence, **kwargs):
        assert gym == "gym1" and asset == "photo1"
        assert kwargs["expected_moderation_json"] == self.row["moderation_json"]
        self.writes.append(copy.deepcopy(evidence))
        self.row["moderation_json"] = copy.deepcopy(evidence)


class Drive:
    def __init__(self, blob=BLOB, delay=0):
        self.blob, self.delay, self.calls = blob, delay, 0

    def download(self, _, path):
        self.calls += 1
        time.sleep(self.delay)
        path.write_bytes(self.blob)


def execute(tmp_path, store=None, drive=None, **kwargs):
    return run("gym1", ["photo1"], ledger_path=tmp_path / "ledger.json", store=store or Store(), drive=drive or Drive(), **kwargs)


def test_dry_run_exact_bytes_no_mutation_and_private_resume(tmp_path):
    store, drive = Store(), Drive()
    before = copy.deepcopy(store.row)
    result = execute(tmp_path, store, drive)
    assert result["ok"] and result["bytes_downloaded"] == len(BLOB)
    assert result["results"][0]["sha256"] == hashlib.sha256(BLOB).hexdigest()
    assert store.row == before and store.writes == []
    assert os.stat(tmp_path / "ledger.json").st_mode & 0o777 == 0o600
    again = execute(tmp_path, store, drive)
    assert again["results"][0]["resumed"] and drive.calls == 1
    assert again["bytes_downloaded"] == 0


def test_drift_refused(tmp_path):
    store = Store()
    result = execute(tmp_path, store, Drive(b"different bytes"))
    assert not result["ok"] and not store.writes


@pytest.mark.parametrize("field,value", [("gym_id", "gym2"), ("kind", "video"), ("review_status", "pending_review"), ("review_content_hash", "wrong")])
def test_asset_guards(tmp_path, field, value):
    store, drive = Store(), Drive()
    store.row[field] = value
    result = execute(tmp_path, store, drive)
    assert not result["ok"] and drive.calls == 0 and not store.writes


def test_source_guard(tmp_path):
    store, drive = Store(), Drive()
    store.source["gym_id"] = "other"
    assert not execute(tmp_path, store, drive)["ok"] and drive.calls == 0


def test_deadline_interrupts_download_and_no_success_ledger(tmp_path):
    store = Store()
    result = execute(tmp_path, store, Drive(delay=.5), deadline_seconds=.03)
    assert result["deadline_reached"] and result["elapsed_seconds"] < .3
    assert not store.writes and not (tmp_path / "ledger.json").exists()


def apply_run(tmp_path, store, drive, **kwargs):
    return run("gym1", ["photo1"], ledger_path=tmp_path / "apply.json", store=store,
               drive=drive, apply=True, dry_run_ledger=tmp_path / "ledger.json", **kwargs)


def test_apply_updates_only_sha_then_resume_rechecks(tmp_path):
    store, drive = Store(), Drive()
    before = copy.deepcopy(store.row)
    assert execute(tmp_path, store, drive)["ok"]
    assert apply_run(tmp_path, store, drive)["ok"]
    added = store.row["moderation_json"].pop("sha256")
    assert store.row == before
    store.row["moderation_json"]["sha256"] = added
    assert apply_run(tmp_path, store, drive)["results"][0]["already_applied"]
    assert len(store.writes) == 1


@pytest.mark.parametrize("damage", ["missing", "mode", "ids", "sha", "failed", "permissions", "md5", "evidence"])
def test_apply_requires_exact_successful_receipt(tmp_path, damage):
    store, drive = Store(), Drive()
    assert execute(tmp_path, store, drive)["ok"]
    path = tmp_path / "ledger.json"
    data = json.loads(path.read_text())
    if damage == "missing":
        path.unlink()
    elif damage == "permissions":
        path.chmod(0o644)
    elif damage == "md5":
        store.row["content_hash"] = "0" * 32
        store.row["review_content_hash"] = "0" * 32
        store.row["moderation_json"]["content_hash"] = "0" * 32
    elif damage == "evidence":
        store.row["moderation_json"]["provider"] = "changed"
    else:
        if damage == "mode":
            data["apply"] = True
        elif damage == "ids":
            data["results"]["other"] = data["results"]["photo1"]
        elif damage == "sha":
            data["results"]["photo1"]["sha256"] = "bad"
        elif damage == "failed":
            data["results"]["photo1"]["ok"] = False
        path.write_text(json.dumps(data))
    try:
        result = apply_run(tmp_path, store, drive)
        assert not result["ok"]
    except ValueError:
        pass
    assert not store.writes and drive.calls == 1


def test_expected_sha_enforced_before_write(tmp_path):
    store, drive = Store(), Drive()
    assert execute(tmp_path, store, drive)["ok"]
    path = tmp_path / "ledger.json"
    data = json.loads(path.read_text())
    data["results"]["photo1"]["sha256"] = "0" * 64
    path.write_text(json.dumps(data))
    result = apply_run(tmp_path, store, drive)
    assert not result["ok"] and result["results"][0]["manual_reconciliation_required"]
    assert not store.writes


@pytest.mark.parametrize("committed", [True, False])
def test_uncertain_apply_reconciles_only_exact_readback(tmp_path, committed):
    class InterruptedStore(Store):
        def update_moderation_sha256(self, *args, **kwargs):
            if committed:
                super().update_moderation_sha256(*args, **kwargs)
            time.sleep(.5)  # model uncertain network reply after possible DB write
    store, drive = InterruptedStore(), Drive()
    assert execute(tmp_path, store, drive)["ok"]
    interrupted = apply_run(tmp_path, store, drive, deadline_seconds=.03)
    assert interrupted["write_outcome_uncertain"]
    calls = drive.calls
    again = apply_run(tmp_path, store, drive)
    assert drive.calls == calls
    if committed:
        assert again["ok"] and again["results"][0]["already_applied"]
        assert len(store.writes) == 1
    else:
        assert not again["ok"] and again["results"][0]["manual_reconciliation_required"]
        assert not store.writes


def test_bound_but_different_existing_sha_requires_manual_reconciliation(tmp_path):
    store, drive = Store(), Drive()
    assert execute(tmp_path, store, drive)["ok"]
    store.row["moderation_json"]["sha256"] = "0" * 64
    result = apply_run(tmp_path, store, drive)
    assert not result["ok"] and result["results"][0]["manual_reconciliation_required"]
    assert not store.writes and drive.calls == 1


def test_bounds_and_unsafe_ledger(tmp_path):
    with pytest.raises(ValueError):
        run("gym1", list(map(str, range(11))), ledger_path=tmp_path / "ledger", store=Store(), drive=Drive())
    (tmp_path / "target").write_text("{}")
    (tmp_path / "ledger.json").symlink_to(tmp_path / "target")
    with pytest.raises(ValueError):
        execute(tmp_path)


def test_resume_does_not_trust_drifted_md5(tmp_path):
    store, drive = Store(), Drive()
    assert execute(tmp_path, store, drive)["ok"]
    store.row["content_hash"] = "0" * 32
    store.row["review_content_hash"] = "0" * 32
    store.row["moderation_json"]["content_hash"] = "0" * 32
    assert not execute(tmp_path, store, drive)["ok"] and drive.calls == 2
