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
from agent import real_calendar_mirror as real_mirror  # noqa: E402
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


def test_explicit_source_media_survives_store_and_real_calendar_mirror(tmp_path):
    """Persist raw provenance exactly; a delivered rendition is never a fallback."""
    store = PendingStore(str(tmp_path / "source_media.db"))
    draft = _draft(draft_id="source1", day_key="2026-07-03", draft_type="feed")
    draft.creative_public_url = "https://cdn.example/delivered-crop.jpg"
    draft.source_media_url = "https://cdn.example/raw-upload.jpg"
    store.put(draft)

    restored = store.get("source1")
    assert restored is not None
    assert restored.source_media_url == "https://cdn.example/raw-upload.jpg"
    rows = real_mirror.collect_real_drafts("lasso_ig", store)
    assert len(rows) == 1
    assert rows[0]["image_url"] == "https://cdn.example/delivered-crop.jpg"
    assert rows[0]["source_media_url"] == "https://cdn.example/raw-upload.jpg"


def test_missing_source_media_stays_missing_after_store_round_trip(tmp_path):
    store = PendingStore(str(tmp_path / "no_source_media.db"))
    draft = _draft(draft_id="source2")
    draft.creative_public_url = "https://cdn.example/delivered-crop.jpg"
    store.put(draft)

    restored = store.get("source2")
    assert restored is not None
    assert restored.source_media_url == ""
    assert not hasattr(restored, "thumbnail_url")
    assert not hasattr(restored, "poster_render_evidence")


def test_video_poster_evidence_survives_store_and_real_calendar_mirror(tmp_path):
    """The real mirror reads reloaded drafts, so poster proof must be durable draft state."""
    store = PendingStore(str(tmp_path / "poster_evidence.db"))
    draft = _draft(draft_id="video1", day_key="2026-07-03", draft_type="feed")
    draft.creative_path = "/clip.mp4"
    draft.creative_public_url = "https://cdn.example/clip.mp4"
    draft.thumbnail_url = "https://cdn.example/clip-poster.jpg"
    draft.poster_render_evidence = {
        "source_exact_url": draft.creative_public_url,
        "delivered_exact_url": draft.thumbnail_url,
        "operation": "render",
        "evidence_ref": "poster-round-trip",
    }
    store.put(draft)

    restored = store.get("video1")
    assert restored is not None
    assert restored.thumbnail_url == draft.thumbnail_url
    assert restored.poster_render_evidence == draft.poster_render_evidence

    evidence = {}
    rows = real_mirror.collect_real_drafts(
        "lasso_ig", store, poster_evidence_out=evidence)
    assert len(rows) == 1
    assert rows[0]["thumbnail_url"] == draft.thumbnail_url
    assert "poster_render_evidence" not in rows[0]
    assert evidence == {
        (draft.creative_public_url, draft.thumbnail_url):
        draft.poster_render_evidence,
    }


def test_render_evidence_survives_pending_store_round_trip_without_calendar_column(tmp_path):
    store = PendingStore(str(tmp_path / "render_evidence.db"))
    draft = _draft(draft_id="render1", day_key="2026-07-03", draft_type="feed")
    draft.creative_public_url = "https://cdn.example/delivered.jpg"
    draft.render_evidence = {
        "operation": "render", "source_exact_url": "https://cdn.example/source.jpg",
        "delivered_exact_url": draft.creative_public_url,
        "source_fingerprint": "source", "delivered_fingerprint": "delivered"}
    store.put(draft)

    restored = store.get("render1")
    assert restored.render_evidence == draft.render_evidence
    rows = real_mirror.collect_real_drafts("lasso_ig", store)
    assert len(rows) == 1
    assert "render_evidence" not in rows[0]


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


# ---- caption newline round-trip (section 9 caption standard) ----------------
def test_caption_newline_round_trip_through_store(tmp_path):
    """Mixed single and double newlines must survive a store put + get unchanged.
    Section 9 of lasso_house_style.md: paragraphs are separated by blank lines
    (double newline); CTA pair lines use a single newline."""
    caption = (
        "Most gyms don't have a lead problem. They have a follow up problem.\n"
        "\n"
        "That is our job, not yours.\n"
        "\n"
        "We built LASSO by running it on ourselves first.\n"
        "\n"
        "Your only job is signing people up.\n"
        "Book a walkthrough and see what done for you actually looks like."
    )
    d = Draft(draft_id="nl1", account_key="lasso_ig", platform="instagram",
              caption=caption, hashtags=["#GymOwner"],
              creative_path="content_library/lasso_v2_built_by_gym_owners.png",
              creative_public_url="", scheduled_for="2026-07-17T12:00:00",
              status=DraftStatus.PENDING, day_key="2026-07-17", draft_type="feed")
    store = PendingStore(str(tmp_path / "nl_test.db"))
    store.put(d)
    got = store.get("nl1")
    assert got is not None
    assert got.caption == caption, (
        f"Newline structure changed in round-trip.\n"
        f"Expected repr: {repr(caption)}\n"
        f"Got repr:      {repr(got.caption)}"
    )


def test_gym_upsert_preserves_display_name_when_not_passed(monkeypatch, tmp_path):
    """Audit 2026-08-25 CRITICAL: single-field upserts (upload_link, zernio_profile_id,
    baseline...) used to ERASE the stored display_name to '' — onboard.run even wiped its
    own write in the same call, blanking the gyms-table name for every portal gym and
    killing the zernio-profile-link display-name fallback. An empty display_name arg now
    means 'leave the stored name alone'; a non-empty one still updates it."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    from agent import db as _db
    _db.gym_upsert("uuidgym", display_name="Pierce Fitness")
    _db.gym_upsert("uuidgym", upload_link="https://x/u/tok")       # no name passed
    row = _db.gym_get("uuidgym")
    assert row["display_name"] == "Pierce Fitness"                  # preserved
    assert row["upload_link"] == "https://x/u/tok"
    _db.gym_upsert("uuidgym", zernio_profile_id="PID9")             # another field-only
    row = _db.gym_get("uuidgym")
    assert row["display_name"] == "Pierce Fitness"
    assert row["zernio_profile_id"] == "PID9"
    _db.gym_upsert("uuidgym", display_name="Pierce Wellness")       # explicit rename works
    assert _db.gym_get("uuidgym")["display_name"] == "Pierce Wellness"


# ---- kv durability signal (gritx needs-media storm guard, 2026-08-27) --------------
def test_kv_is_durable_signals(monkeypatch, tmp_path):
    """kv_is_durable: True with an explicit AGENT_DB_PATH (every test, and any
    deliberately configured process) or a mounted data volume; False on the CWD
    echo.db fallback (dev checkout / verify run / a service without the volume),
    where a dedup stamp dies with the process."""
    from agent import db
    # explicit db path -> durable (conftest's default posture)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "x.db"))
    assert db.kv_is_durable() is True
    # no db path + no data volume -> EPHEMERAL
    monkeypatch.delenv("AGENT_DB_PATH", raising=False)
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "missing_volume"))
    assert db.kv_is_durable() is False
    # a mounted data dir -> durable
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    assert db.kv_is_durable() is True
