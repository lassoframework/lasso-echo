from agent.jobs import queued_gym_media as queue


class Store:
    def __init__(self):
        self.pending = {"id": "s1", "gym_id": "pierce", "kind": "gym_drive",
                        "active": True, "sync_claim_token": "claim1"}
        self.finished = []

    def available(self):
        return True

    def claim_sync(self):
        row, self.pending = self.pending, None
        return row

    def finish_sync(self, *args):
        self.finished.append(args)
        return True


def test_success_claim_and_completion(monkeypatch):
    monkeypatch.setattr(queue.config, "gym_drive_connect_active_for", lambda _: True)
    store = Store()
    calls = []

    def sync(source, **kw):
        calls.append(kw)
        return {"ok": True}

    assert queue.run_one(store=store, sync=sync)
    assert calls[0]["sweep_missing"] is False
    assert store.finished == [("s1", "claim1", True, None)]
    assert not queue.run_one(store=store)


def test_failure_records_status_and_manual_retry(monkeypatch):
    monkeypatch.setattr(queue.config, "gym_drive_connect_active_for", lambda _: True)
    store = Store()
    assert queue.run_one(store=store, sync=lambda source, **kw: {"ok": False,
                                                                 "error": "DriveTimeout"})
    assert store.finished == [("s1", "claim1", False, "DriveTimeout")]
    store.pending = {"id": "s1", "gym_id": "pierce", "kind": "gym_drive",
                     "active": True, "sync_claim_token": "claim2"}
    assert queue.run_one(store=store, sync=lambda source, **kw: {"ok": True})
    assert store.finished[-1] == ("s1", "claim2", True, None)


def test_disabled_gym_does_not_sync(monkeypatch):
    monkeypatch.setattr(queue.config, "gym_drive_connect_active_for", lambda _: False)
    store = Store()
    assert queue.run_one(store=store, sync=lambda *a, **kw: None, log=lambda _: None)
    assert store.finished[0][2] is False


def test_migration_never_reclaims_uncertain_running_sync():
    from pathlib import Path
    migration = (Path(__file__).resolve().parents[1] / "migrations" /
                 "media_source_sync_request_20260917.sql").read_text()
    assert "AND sync_status = 'queued'" in migration
    assert "sync_started_at <" not in migration
