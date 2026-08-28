"""
Needs-media alert DIGEST (the eng 18-alert storm, 2026-08-28): a month build over
an empty library used to fire one Slack alert PER DAY-SLOT. Now _alert_needs_media
stamps + buffers, and flush_needs_media_alerts posts ONE digest per account per
run. Per-day kv dedup (the gritx-storm durable-or-silent rule) is unchanged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, db  # noqa: E402


import pytest


@pytest.fixture(autouse=True)
def _reset_buffer():
    client_content._NEEDS_MEDIA_BUFFER.clear()
    yield
    client_content._NEEDS_MEDIA_BUFFER.clear()


def _clear(account="eng_ig"):
    client_content._NEEDS_MEDIA_BUFFER.pop(account, None)
    try:
        with db._lock, db.connect() as conn:
            conn.execute("DELETE FROM kv WHERE key LIKE ?",
                         (f"needs_media_alerted_{account}_%",))
            conn.commit()
    except Exception:
        pass


def test_eighteen_empty_days_produce_one_digest(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    _clear()
    sent = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **k: sent.append(msg))
    days = [f"2026-09-{d:02d}" for d in range(16, 31)] + [
        "2026-10-01", "2026-10-02", "2026-10-03"]
    for d in days:
        client_content._alert_needs_media("eng_ig", d, "educational")
    assert sent == []  # nothing posts per day
    client_content.flush_needs_media_alerts("eng_ig")
    assert len(sent) == 1  # ONE digest, not 18 alerts
    msg = sent[0]
    assert "18 day(s)" in msg
    assert "2026-09-16 to 2026-10-03" in msg
    assert "Not blocked" in msg
    # flush is idempotent: a second flush posts nothing
    client_content.flush_needs_media_alerts("eng_ig")
    assert len(sent) == 1


def test_per_day_kv_dedupe_prevents_rebuffer(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    _clear()
    sent = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **k: sent.append(msg))
    client_content._alert_needs_media("eng_ig", "2026-09-16", "offer")
    client_content.flush_needs_media_alerts("eng_ig")
    assert len(sent) == 1
    # re-run of the same day: kv stamp blocks re-buffering -> no second digest
    client_content._alert_needs_media("eng_ig", "2026-09-16", "offer")
    client_content.flush_needs_media_alerts("eng_ig")
    assert len(sent) == 1


def test_single_day_digest_reads_naturally(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    client_content._NEEDS_MEDIA_BUFFER.clear()  # flush-all test: no cross-test residue
    _clear()
    sent = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **k: sent.append(msg))
    client_content._alert_needs_media("eng_ig", "2026-09-16", "offer")
    client_content.flush_needs_media_alerts()  # flush-all path (runner safety net)
    assert len(sent) == 1 and "1 day(s)" in sent[0] and "2026-09-16" in sent[0]


def test_preview_and_ephemeral_paths_never_buffer(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    _clear()
    client_content._alert_needs_media("eng_ig", "2026-09-17", "offer", enabled=False)
    assert "eng_ig" not in client_content._NEEDS_MEDIA_BUFFER
    monkeypatch.setattr(db, "kv_is_durable", lambda: False)
    client_content._alert_needs_media("eng_ig", "2026-09-18", "offer")
    assert "eng_ig" not in client_content._NEEDS_MEDIA_BUFFER
