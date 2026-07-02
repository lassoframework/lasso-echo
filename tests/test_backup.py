"""
Store backup + restore tests. Offline (fake R2 dict). Asserts: backup writes a
consistent snapshot and the retention sweep prunes old keys; restore lands in
staging with verification counts and NEVER touches the live db without
--confirm; with --confirm the old live db is kept as .pre_restore.bak; the
nightly trigger is inert while the flag is OFF and fires once per night.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import backup, db, ops_alerts  # noqa: E402

NOW = datetime(2026, 7, 15, 2, 5, tzinfo=timezone.utc)


class FakeR2:
    def __init__(self):
        self.objects = {}

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get_bytes(self, key):
        return self.objects[key]

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data

    def delete(self, key):
        self.objects.pop(key, None)


def _seed_live_db():
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at) VALUES ('d','lasso_ig','instagram','c','M',"
            "'published','2026-07-10T10:00:00')")
        conn.commit()


def test_backup_writes_and_retention_prunes():
    _seed_live_db()
    r2 = FakeR2()
    r2.objects["echo/backups/echo_20260601T020000.db"] = b"OLD"      # 44 days old
    r2.objects["echo/backups/echo_20260710T020000.db"] = b"RECENT"   # 5 days old
    key = backup.backup_now(r2=r2, now=NOW)
    assert key == "echo/backups/echo_20260715T020500.db"
    assert len(r2.objects[key]) > 1000                               # real snapshot
    assert "echo/backups/echo_20260601T020000.db" not in r2.objects  # pruned
    assert "echo/backups/echo_20260710T020000.db" in r2.objects      # kept


def test_backup_failure_alerts_once(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")

    class Rec:
        notices = []

        def post_notice(self, text):
            Rec.notices.append(text)
            return {"ok": True}

    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: Rec())

    class BrokenR2(FakeR2):
        def put_bytes(self, *a, **k):
            raise RuntimeError("r2 down")

    assert backup.backup_now(r2=BrokenR2(), now=NOW) is None
    assert len([n for n in Rec.notices if "backup failed" in n]) == 1


def test_restore_staging_never_touches_live_without_confirm():
    _seed_live_db()
    r2 = FakeR2()
    key = backup.backup_now(r2=r2, now=NOW)
    live = db.db_path()
    live_bytes = open(live, "rb").read()
    counts = backup.restore_store(key, r2=r2, confirm=False)
    assert counts["posts"] == 1                                       # verified
    assert open(live, "rb").read() == live_bytes                      # UNTOUCHED
    assert os.path.exists(live + ".restore_staging")                  # staging only


def test_restore_confirm_replaces_and_keeps_bak():
    _seed_live_db()
    r2 = FakeR2()
    key = backup.backup_now(r2=r2, now=NOW)
    live = db.db_path()
    counts = backup.restore_store(key, r2=r2, confirm=True)
    assert counts["posts"] == 1
    assert os.path.exists(live)                                       # restored live
    assert os.path.exists(live + ".pre_restore.bak")                  # old kept
    with db.connect() as conn:                                        # data intact
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


def test_nightly_trigger_inert_when_off_and_once_when_on(monkeypatch):
    monkeypatch.delenv("AGENT_BACKUP_ENABLED", raising=False)
    assert backup.maybe_backup(now=NOW, r2=FakeR2()) is None          # inert OFF
    monkeypatch.setenv("AGENT_BACKUP_ENABLED", "true")
    monkeypatch.setenv("AGENT_BACKUP_HOUR_UTC", "2")
    r2 = FakeR2()
    assert backup.maybe_backup(now=NOW, r2=r2) is not None            # fires at hour
    assert backup.maybe_backup(now=NOW, r2=r2) is None                # once per night
    assert db.kv_get("backup_done_date") == "2026-07-15"
