from datetime import datetime, timedelta, timezone

from agent import portal_social
from tests.gym_media_fakes import make_asset


def _reviewed(fid, gym):
    return make_asset(fid, gym_id=gym)


def test_media_summary_counts_review_separately_and_scopes_tenant(monkeypatch):
    pending = _reviewed("pending", "gymx")
    pending["review_status"] = "pending_review"
    usable = _reviewed("usable", "gymx")
    expired = _reviewed("expired", "gymx")
    expired.update(people_detected=True, consent_status="granted",
                   consent_member_ref="m1", release_ref="r1",
                   consent_expires_at="2020-01-01T00:00:00Z")
    class Store:
        def available(self): return True
        def list_assets(self, gym):
            assert gym == "gymx"
            return [pending, usable, expired]
    monkeypatch.setattr("agent.media_source_store.default_store", lambda: Store())
    monkeypatch.setattr("agent.media_bridge.episode", lambda *a, **k: None)
    monkeypatch.setattr("agent.ghl_intake.upload_link_for", lambda gym: "/u/token" if gym == "gymx" else "")
    out = portal_social._media_bridge_status("gymx_ig")
    assert out["media_review"] == {"status": "awaiting", "pending_review_count": 1,
                                    "publishable_count": 1}
    assert out["upload_action"] == {"url": "/u/token", "label": "Upload media",
                                     "received_means_indexed": False}


def test_inventory_failure_is_unknown_not_zero(monkeypatch):
    class Broken:
        def available(self): return True
        def list_assets(self, gym): raise RuntimeError("offline")
    monkeypatch.setattr("agent.media_source_store.default_store", lambda: Broken())
    monkeypatch.setattr("agent.media_bridge.episode", lambda *a, **k: None)
    out = portal_social._media_bridge_status("gymx")
    assert out["media_review"]["status"] == "unknown"
    assert out["media_review"]["publishable_count"] is None


def test_fixed_episode_dates_and_current_notice_only(monkeypatch):
    class Store:
        def available(self): return True
        def list_assets(self, gym): return []
    monkeypatch.setattr("agent.media_source_store.default_store", lambda: Store())
    state = {"id": "current", "depleted_on": "2026-09-18",
             "start": "2026-09-19", "end": "2026-09-20"}
    monkeypatch.setattr("agent.media_bridge.episode", lambda *a, **k: state)
    monkeypatch.setattr("agent.media_bridge.notice_status", lambda base: [
        {"episode_id": "old", "status": "sent", "ts": "123"},
        {"episode_id": "current", "status": "unresolved",
         "created_at": "2026-09-18T10:00:00Z", "ts": None}])
    monkeypatch.setattr("agent.config.posting_timezone_for", lambda base: "UTC")
    now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    out = portal_social._media_bridge_status("gymx", now=now)
    assert out["fallback_episode"]["dates"] == ["2026-09-19", "2026-09-20"]
    assert out["fallback_episode"]["drafts_need_review"] is True
    assert out["notice_state"]["status"] == "unresolved"
    assert out["notice_state"]["delivery_confirmed"] is False

    expired = portal_social._media_bridge_status("gymx", now=now + timedelta(days=3))
    assert expired["fallback_episode"]["active"] is False
    assert expired["fallback_episode"]["dates"] == []
    assert expired["notice_state"]["status"] == "none"


def test_both_social_data_branches_include_additive_status(monkeypatch, tmp_path):
    marker = {"media_review": {"status": "unknown"},
              "fallback_episode": {"dates": []},
              "notice_state": {"status": "none"},
              "upload_action": {"url": "/u/token"}}
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setattr(portal_social.config, "portal_social_enabled", lambda: True)
    monkeypatch.setattr(portal_social, "is_social_active", lambda *a, **k: True)
    monkeypatch.setattr(portal_social, "_media_bridge_status", lambda *a, **k: marker)
    monkeypatch.setattr(portal_social, "_low_creative_and_days", lambda *a, **k: (True, 0))
    monkeypatch.setattr(portal_social, "_budget_state", lambda *a, **k: {})
    monkeypatch.setattr(portal_social, "_awaiting_media_signal", lambda *a: (True, "/u/token"))

    monkeypatch.setattr(portal_social.config, "portal_calendar_supabase_enabled", lambda: False)
    status, local = portal_social.handle_social("gymx", "2026-09")
    assert status == 200 and all(local[k] == v for k, v in marker.items())

    class Supabase:
        def list_month(self, account, month): return []
    monkeypatch.setattr(portal_social._pcs, "SupabaseCalendarStore", Supabase)
    monkeypatch.setattr(portal_social.config, "portal_calendar_supabase_enabled", lambda: True)
    status, shared = portal_social.handle_social("gymx", "2026-09")
    assert status == 200 and all(shared[k] == v for k, v in marker.items())
