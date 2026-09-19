"""Episode and ambiguous Slack transport failure cases."""
from datetime import datetime, timezone
from types import SimpleNamespace

from agent import config, media_bridge as bridge
from agent.media_bridge_route import Route


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_MEDIA_BRIDGE_ALERTS", "true")
    monkeypatch.setattr(config, "posting_timezone_for", lambda _: "America/New_York")
    monkeypatch.setattr(config, "slack_convo_client_reply_armed", lambda _: True)
    from agent import media_bridge_route
    monkeypatch.setattr(media_bridge_route, "resolve_client_route",
                        lambda _: Route(channel="C123CLIENT", gym_id="gymx"))


def test_episode_stays_two_calendar_days_across_restart_and_month(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    day1 = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)
    day3 = datetime(2026, 10, 2, 16, tzinfo=timezone.utc)
    assert bridge.bridge_days("gymx", now=day1) == ["2026-10-01", "2026-10-02"]
    assert bridge.bridge_days("gymx", now=day2) == ["2026-10-02"]
    assert bridge.bridge_days("gymx", now=day3) == []
    assert bridge.episode("gymx", now=day3)["depleted_on"] == "2026-09-30"
    bridge.rearm_for_new_upload("gymx", "approved-new-upload", usable=True)
    assert bridge.bridge_days("gymx", now=day3) == ["2026-10-03", "2026-10-04"]


class Poster:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def _chat_post(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_ambiguous_result_never_resends_without_reconciliation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    account = SimpleNamespace()
    now = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    poster = Poster(TimeoutError("accepted then timed out"))
    first = bridge.notify_bridge("gymx", account, now=now, poster=poster)
    assert first["reconcile"]
    assert bridge.notify_bridge("gymx", account, now=now, poster=poster)["reason"] == "unresolved send"
    assert len(poster.calls) == 1
    assert bridge.reconcile_notice("gymx", delivered=True, channel="C123CLIENT", ts="123.456")["ok"]
    assert bridge.notify_bridge("gymx", account, now=now, poster=poster)["deduped"]
    assert len(poster.calls) == 1


def test_verified_absence_can_retry_and_success_is_durable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    account = SimpleNamespace()
    now = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    poster = Poster({"ok": False, "error": "timeout"})
    assert bridge.notify_bridge("gymx", account, now=now, poster=poster)["reconcile"]
    assert bridge.reconcile_notice("gymx", delivered=False)["status"] == "ready"
    poster.result = {"ok": True, "ts": "123.456"}
    assert bridge.notify_bridge("gymx", account, now=now, poster=poster)["sent"]
    assert bridge.notify_bridge("gymx", account, now=now, poster=poster)["deduped"]
    assert len(poster.calls) == 2


def test_slack_channel_must_exist_and_include_echo_before_intent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    class LivePoster(Poster):
        def _send(self, url, payload):
            if url.endswith("auth.test"):
                return {"ok": True, "user_id": "U0BE39F02KV"}
            return {"ok": True, "channel": {"id": "C123CLIENT", "is_member": False,
                                            "is_archived": False}}

    poster = LivePoster({"ok": True, "ts": "123.456"})
    result = bridge.notify_bridge(
        "gymx", SimpleNamespace(), poster=poster,
        now=datetime(2026, 9, 30, 16, tzinfo=timezone.utc))
    assert result["reason"] == "Echo channel membership not verified"
    assert poster.calls == []
    assert bridge.episode("gymx", create=False) is None


def test_new_usable_upload_same_day_gets_new_notice_identity(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    now = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    poster = Poster({"ok": True, "ts": "123.456"})
    account = SimpleNamespace()
    assert bridge.notify_bridge("gymx", account, now=now, poster=poster)["sent"]
    first = bridge.episode("gymx", now=now)
    bridge.rearm_for_new_upload("gymx", "approved-new-upload", usable=True)
    assert bridge.notify_bridge("gymx", account, now=now, poster=poster)["sent"]
    assert bridge.episode("gymx", now=now)["id"] != first["id"]
    assert len(poster.calls) == 2
