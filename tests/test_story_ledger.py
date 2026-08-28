"""
Story Studio Wave 1: the render_ledger re-ingest guard, end to end through the kv
fallback (offline) and the sync-path skip.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_ledger  # noqa: E402


def test_record_then_recognize_via_kv_fallback(monkeypatch):
    # No Supabase creds -> kv fallback. Record a render, then recognize its hash.
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    story_ledger.record_render("HASH_ECHO_1", gym_id="pierce", story_render_id="r1")
    assert story_ledger.is_echo_render("HASH_ECHO_1") is True
    assert story_ledger.is_echo_render("hash_echo_1") is True  # case-normalized
    assert story_ledger.is_echo_render("NOT_RECORDED") is False


def test_empty_hash_is_never_a_match():
    assert story_ledger.is_echo_render("") is False
    assert story_ledger.record_render("") == ""


def test_reingest_skipped_in_sync_path(monkeypatch):
    # A walked file whose content_hash is a recorded Echo render is dropped before insert.
    from agent.jobs import sync_gym_media as sync
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    story_ledger.record_render("ECHO_REBUILT", gym_id="pierce", story_render_id="r2")

    rows = [
        {"id": "a1", "title": "IMG_1.MOV", "content_hash": "CLIENT_RAW"},
        {"id": "a2", "title": "pierce_story_final.mp4", "content_hash": "ECHO_REBUILT"},
    ]
    kept, skipped = sync._drop_reingested(rows, "pierce", lambda m: None)
    assert [r["id"] for r in kept] == ["a1"]
    assert "pierce_story_final.mp4" in skipped
