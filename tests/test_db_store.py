"""
SQLite store tests (Tier 2 storage swap). Asserts: legacy json migrates once with
a backup; draft flows read/write equivalently through the new store; the posts
table mirrors log_post; concurrent writes are safe (WAL smoke); the rotation
served log enforces the no-repeat window through the new store.
"""

import json
import os
import sqlite3
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, postlog, rotation  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402


def _draft(draft_id="d1", status=DraftStatus.PENDING, day_key="", draft_type=""):
    return Draft(draft_id=draft_id, account_key="lasso_ig", platform="instagram",
                 caption="cap", hashtags=["#x"], creative_path="/c.jpg",
                 creative_public_url="", scheduled_for="t", status=status,
                 day_key=day_key, draft_type=draft_type)


# ---- migration -----------------------------------------------------------------
def test_migrates_legacy_pending_json_with_backup(tmp_path, monkeypatch):
    legacy = tmp_path / "pending_drafts.json"
    legacy.write_text(json.dumps({
        "old1": {"draft_id": "old1", "account_key": "lasso_ig", "platform": "instagram",
                 "caption": "legacy cap", "status": "pending"}}), encoding="utf-8")
    monkeypatch.setattr("agent.store.STORE_PATH_DEFAULT", str(legacy))
    store = PendingStore()
    got = store.get("old1")
    assert got is not None and got.caption == "legacy cap"
    assert not legacy.exists()                          # backed up, not deleted
    assert (tmp_path / "pending_drafts.json.migrated.bak").exists()


def test_migrates_served_and_postlog(tmp_path, monkeypatch):
    served = tmp_path / "rotation_served.json"
    served.write_text(json.dumps({"lasso_ig": [
        {"key": "a.jpg", "pillar": "p1", "date": "2026-07-01",
         "archetype": "flow", "set": "brand"}]}), encoding="utf-8")
    monkeypatch.setenv("AGENT_ROTATION_STATE_DIR", str(tmp_path))
    loaded = rotation.load_served()
    assert loaded["lasso_ig"][0]["key"] == "a.jpg"
    assert loaded["lasso_ig"][0]["archetype"] == "flow"
    assert (tmp_path / "rotation_served.json.migrated.bak").exists()


# ---- draft flow equivalence -------------------------------------------------------
def test_draft_flow_put_get_remove_find(tmp_path):
    store = PendingStore()
    d = _draft(day_key="2026-07-03", draft_type="feed")
    store.put(d)
    got = store.get("d1")
    assert got.caption == "cap" and got.hashtags == ["#x"]
    assert got.status == DraftStatus.PENDING
    found = store.find_pending("lasso_ig", "2026-07-03", "feed")
    assert found is not None and found.draft_id == "d1"
    assert store.find_pending("lasso_ig", "2026-07-04", "feed") is None
    assert len(store.list_pending()) == 1
    d.status = DraftStatus.APPROVED
    store.put(d)                                        # idempotent upsert
    assert store.list_pending() == []
    assert store.remove("d1") is True
    assert store.get("d1") is None


def test_log_post_mirrors_to_posts_table(tmp_path):
    postlog.log_post("lasso_ig", "instagram", "hello", "M1", "would_publish", "d9",
                     path=str(tmp_path / "log.jsonl"))
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM posts").fetchone()
    assert row["account_key"] == "lasso_ig" and row["media_id"] == "M1"
    assert row["mode"] == "would_publish"
    # the jsonl still exists for compat
    assert (tmp_path / "log.jsonl").exists()


# ---- concurrency smoke (WAL) --------------------------------------------------------
def test_concurrent_served_writes_are_safe():
    errors = []

    def writer(n):
        try:
            for i in range(10):
                rotation.record_served("lasso_ig", f"c{n}_{i}.jpg", "p1", "2026-07-03")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(rotation.load_served()["lasso_ig"]) == 40


# ---- rotation window still enforced through the new store --------------------------
def test_rotation_window_through_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ROTATION_ENABLED", "true")
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    from agent import config
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    lib = tmp_path / "library"
    lib.mkdir()
    for name in ("lasso_p1_a.jpg", "lasso_p2_b.jpg"):
        (lib / name).write_bytes(b"img")
        (lib / (name[:-4] + ".txt")).write_text("clean", encoding="utf-8")
    rotation.record_served("lasso_ig", "lasso_p1_a.jpg", "p1", "2026-07-02")
    kind, creative = rotation.choose("lasso_ig", "2026-07-03", str(lib))
    assert os.path.basename(creative.path) == "lasso_p2_b.jpg"   # window holds
