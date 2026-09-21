from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading

from agent import db, media_bridge, portal_social
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
    monkeypatch.setenv("AGENT_MEDIA_RUNWAY_LOCAL_FALLBACK", "true")
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


def test_worker_mirrors_episode_and_portal_reads_shared_snapshot(monkeypatch, tmp_path):
    """The portal can see a worker-written runway without sharing its SQLite file."""
    class Shared:
        def __init__(self):
            self.rows = {}

        def available(self):
            return True

        def read(self, base):
            return self.rows.get(base)

        def upsert(self, base, *, revision, fallback_episode, notice_state):
            self.rows[base] = {"revision": revision,
                               "fallback_episode": fallback_episode,
                               "notice_state": notice_state}
            return True

    class MediaStore:
        def available(self):
            return True

        def list_assets(self, gym):
            return []

    shared = Shared()
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: shared)
    monkeypatch.setattr("agent.media_source_store.default_store", lambda: MediaStore())
    monkeypatch.setattr("agent.config.posting_timezone_for", lambda base: "UTC")
    now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    # Reconciliation also projects the worker snapshot using the implicit clock.
    # Keep that clock aligned with this scenario; explicit expiry checks below
    # still advance time and exercise the real production expiration logic.
    from agent import calendar_autopublish
    local_now = calendar_autopublish._local_now
    monkeypatch.setattr(calendar_autopublish, "_local_now",
                        lambda value=None, tz_name=None: local_now(value or now, tz_name))

    state = media_bridge.episode("gymx", now=now)
    mirrored = shared.rows["gymx"]
    assert mirrored["revision"] == 1
    assert mirrored["fallback_episode"] == {
        "active": True, "episode_id": state["id"], "depleted_on": "2026-09-18",
        "dates": ["2026-09-19", "2026-09-20"], "drafts_need_review": True,
        "status": None,
    }
    assert mirrored["notice_state"] == {
        "status": "none", "episode_id": state["id"], "created_at": None,
        "delivery_confirmed": False,
    }

    out = portal_social._media_bridge_status("gymx_ig", now=now)
    assert out["fallback_episode"]["active"] is True
    assert out["fallback_episode"]["episode_id"] == state["id"]
    assert out["fallback_episode"]["dates"] == ["2026-09-19", "2026-09-20"]
    assert out["notice_state"]["status"] == "none"

    expired = portal_social._media_bridge_status("gymx_ig", now=now + timedelta(days=3))
    assert expired["fallback_episode"]["active"] is False
    assert expired["fallback_episode"]["dates"] == []
    assert expired["notice_state"]["status"] == "none"

    with db.connect() as conn:
        media_bridge._put(conn, media_bridge._outbox_key("gymx", state), {
            "status": "unresolved", "channel": "C123CLIENT", "created_at": "2026-09-18T12:00:00Z",
        })
        conn.commit()
    assert media_bridge.reconcile_notice("gymx", channel="C123CLIENT") == {
        "ok": True, "status": "ready"}
    assert shared.rows["gymx"]["notice_state"] == {
        "status": "ready", "episode_id": state["id"],
        "created_at": "2026-09-18T12:00:00Z", "delivery_confirmed": False,
    }
    assert portal_social._media_bridge_status("gymx", now=now)["notice_state"]["status"] == "ready"

    media_bridge.reset_notice("gymx")
    assert shared.rows["gymx"]["fallback_episode"] is None
    inactive = portal_social._media_bridge_status("gymx", now=now)
    assert inactive["fallback_episode"]["active"] is False
    assert inactive["notice_state"]["status"] == "none"


def test_shared_runway_missing_is_unknown_not_a_stale_local_episode(monkeypatch):
    class MediaStore:
        def available(self):
            return True

        def list_assets(self, gym):
            return []

    monkeypatch.setattr("agent.media_source_store.default_store", lambda: MediaStore())
    monkeypatch.setattr(media_bridge, "shared_snapshot", lambda base: None)
    monkeypatch.setattr(media_bridge, "episode", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("available shared store must not fall back to SQLite")))
    out = portal_social._media_bridge_status("gymx")
    assert out["fallback_episode"]["active"] is None
    assert out["notice_state"]["status"] == "unknown"


def test_shared_runway_read_failure_is_unknown_not_inactive(monkeypatch):
    class MediaStore:
        def available(self):
            return True

        def list_assets(self, gym):
            return []

    monkeypatch.setattr("agent.media_source_store.default_store", lambda: MediaStore())
    monkeypatch.setattr(media_bridge, "shared_snapshot", lambda base: (_ for _ in ()).throw(
        RuntimeError("shared store offline")))
    monkeypatch.setattr(media_bridge, "episode", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("shared read fault must stay neutral")))
    out = portal_social._media_bridge_status("gymx")
    assert out["fallback_episode"]["active"] is None
    assert out["notice_state"]["status"] == "unknown"


def test_missing_shared_credentials_are_unknown_without_explicit_local_fallback(
        monkeypatch, tmp_path):
    class MediaStore:
        def available(self):
            return True

        def list_assets(self, gym):
            return []

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "separate-portal.db"))
    monkeypatch.delenv("AGENT_MEDIA_RUNWAY_LOCAL_FALLBACK", raising=False)
    monkeypatch.setattr("agent.media_source_store.default_store", lambda: MediaStore())
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: None)
    monkeypatch.setattr(media_bridge, "episode", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("production must not read a separate-volume SQLite file")))
    out = portal_social._media_bridge_status("gymx")
    assert out["fallback_episode"]["active"] is None
    assert out["notice_state"]["status"] == "unknown"


def test_offline_local_fallback_requires_explicit_opt_in(monkeypatch):
    class MediaStore:
        def available(self):
            return True

        def list_assets(self, gym):
            return []

    state = {"id": "local", "depleted_on": "2026-09-18",
             "start": "2026-09-19", "end": "2026-09-20"}
    monkeypatch.setenv("AGENT_MEDIA_RUNWAY_LOCAL_FALLBACK", "true")
    monkeypatch.setattr("agent.media_source_store.default_store", lambda: MediaStore())
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: None)
    monkeypatch.setattr(media_bridge, "episode", lambda *a, **k: state)
    monkeypatch.setattr(media_bridge, "notice_status", lambda base: [])
    monkeypatch.setattr("agent.config.posting_timezone_for", lambda base: "UTC")
    out = portal_social._media_bridge_status(
        "gymx", now=datetime(2026, 9, 18, tzinfo=timezone.utc))
    assert out["fallback_episode"]["episode_id"] == "local"
    assert out["fallback_episode"]["active"] is True


def test_shared_write_failure_keeps_local_episode(monkeypatch, tmp_path):
    class BrokenShared:
        def available(self):
            return True

        def upsert(self, *args, **kwargs):
            raise RuntimeError("shared store offline")

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: BrokenShared())
    state = media_bridge.episode("gymx", now=datetime(2026, 9, 18, tzinfo=timezone.utc))
    assert state == media_bridge.episode("gymx", create=False)


def test_existing_local_episode_bootstraps_revision_and_shared_snapshot(monkeypatch, tmp_path):
    class Shared:
        def __init__(self):
            self.writes = []

        def available(self):
            return True

        def upsert(self, base, **snapshot):
            self.writes.append((base, snapshot))
            return True

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setattr("agent.config.posting_timezone_for", lambda base: "UTC")
    state = {"id": "pre-upgrade", "depleted_on": "2026-09-18",
             "start": "2026-09-19", "end": "2026-09-20"}
    with db.connect() as conn:
        media_bridge._put(conn, "media_bridge_episode_gymx", state)
        conn.commit()
    shared = Shared()
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: shared)

    assert media_bridge.episode(
        "gymx", create=False, now=datetime(2026, 9, 18, tzinfo=timezone.utc)) == state
    assert shared.writes[-1][1]["revision"] == 1
    assert shared.writes[-1][1]["fallback_episode"]["episode_id"] == "pre-upgrade"
    with db.connect() as conn:
        assert media_bridge._revision(conn, "gymx") == 1


def test_out_of_order_transition_race_cannot_restore_cleared_episode(monkeypatch, tmp_path):
    class OrderedShared:
        def __init__(self):
            self.row = None
            self.first_started = threading.Event()
            self.release_first = threading.Event()
            self.lock = threading.Lock()

        def available(self):
            return True

        def upsert(self, base, **snapshot):
            if snapshot["revision"] == 1:
                self.first_started.set()
                assert self.release_first.wait(5)
            with self.lock:
                if self.row is None or self.row["revision"] < snapshot["revision"]:
                    self.row = {"gym_id": base, **snapshot}
                    return True
                return self.row["revision"] == snapshot["revision"]

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setattr("agent.config.posting_timezone_for", lambda base: "UTC")
    shared = OrderedShared()
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: shared)
    errors = []

    def create_episode():
        try:
            media_bridge.episode(
                "gymx", now=datetime(2026, 9, 18, tzinfo=timezone.utc))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=create_episode)
    worker.start()
    assert shared.first_started.wait(5)
    media_bridge.reset_notice("gymx")
    shared.release_first.set()
    worker.join(5)

    assert not errors and not worker.is_alive()
    assert shared.row["revision"] == 2
    assert shared.row["fallback_episode"] is None
    assert shared.row["notice_state"]["status"] == "none"


def test_transient_shared_failure_self_heals_on_episode_and_status_reads(monkeypatch, tmp_path):
    class FlakyShared:
        def __init__(self):
            self.failures = 1
            self.successes = []

        def available(self):
            return True

        def upsert(self, base, **snapshot):
            if self.failures:
                self.failures -= 1
                return False
            self.successes.append((base, snapshot))
            return True

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setattr("agent.config.posting_timezone_for", lambda base: "UTC")
    shared = FlakyShared()
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: shared)
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)

    media_bridge.episode("gymx", now=now)
    assert shared.successes == []
    media_bridge.episode("gymx", now=now, create=False)
    assert shared.successes[-1][1]["revision"] == 1

    shared.failures = 1
    shared.successes.clear()
    media_bridge.notice_status("gymx")
    assert shared.successes == []
    media_bridge.notice_status("gymx")
    assert shared.successes[-1][1]["revision"] == 1

    shared.failures = 1
    shared.successes.clear()
    media_bridge.reset_notice("gymx")
    assert shared.successes == []
    assert media_bridge.episode("gymx", create=False, now=now) is None
    assert shared.successes[-1][1]["revision"] == 2
    assert shared.successes[-1][1]["fallback_episode"] is None


def test_sent_dedupe_early_return_retries_failed_shared_write(monkeypatch, tmp_path):
    class FlakyShared:
        def __init__(self):
            self.calls = 0
            self.row = None

        def available(self):
            return True

        def upsert(self, base, **snapshot):
            self.calls += 1
            if self.calls == 1:
                return False
            self.row = {"gym_id": base, **snapshot}
            return True

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setattr("agent.config.posting_timezone_for", lambda base: "UTC")
    monkeypatch.setattr(media_bridge.config, "slack_convo_client_reply_armed", lambda bot: True)
    monkeypatch.setattr("agent.media_bridge_route.resolve_client_route",
                        lambda base: SimpleNamespace(ok=True, channel="C123CLIENT"))
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: None)
    state = media_bridge.episode(
        "gymx", now=datetime(2026, 9, 18, tzinfo=timezone.utc))
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        media_bridge._put(conn, media_bridge._outbox_key("gymx", state), {
            "status": "sent", "channel": "C123CLIENT", "text": "already sent",
            "created_at": "2026-09-18T12:00:00Z", "ts": "123.456"})
        media_bridge._bump_revision(conn, "gymx")
        conn.commit()

    shared = FlakyShared()
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: shared)
    poster = SimpleNamespace(_chat_post=lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("dedupe must not send Slack again")))
    result = media_bridge.notify_bridge(
        "gymx", SimpleNamespace(), now=datetime(2026, 9, 18, tzinfo=timezone.utc),
        poster=poster)
    assert result == {"ok": True, "deduped": True}
    assert shared.calls == 2
    assert shared.row["revision"] == 2
    assert shared.row["notice_state"]["status"] == "sent"


def test_episode_and_notice_transitions_advance_revision_in_the_same_transaction(
        monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setattr("agent.config.posting_timezone_for", lambda base: "UTC")
    monkeypatch.setattr(media_bridge.config, "slack_convo_client_reply_armed", lambda bot: True)
    monkeypatch.setattr("agent.media_bridge_route.resolve_client_route",
                        lambda base: SimpleNamespace(ok=True, channel="C123CLIENT"))
    monkeypatch.setattr(media_bridge, "_shared_store", lambda: None)
    poster = SimpleNamespace(_chat_post=lambda **kwargs: {"ok": True, "ts": "123.456"})

    result = media_bridge.notify_bridge(
        "gymx", SimpleNamespace(), now=datetime(2026, 9, 18, tzinfo=timezone.utc),
        poster=poster)
    assert result == {"ok": True, "sent": True}
    state = media_bridge.episode("gymx", create=False)
    with db.connect() as conn:
        assert media_bridge._revision(conn, "gymx") == 3
        assert media_bridge._get(conn, media_bridge._outbox_key("gymx", state))["status"] == "sent"

    media_bridge.reset_notice("gymx")
    with db.connect() as conn:
        assert media_bridge._revision(conn, "gymx") == 4
        assert media_bridge._get(conn, "media_bridge_episode_gymx") is None


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
