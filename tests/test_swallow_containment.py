"""
Over-containment fixes: swallowed failures are now loud and can't loop.

M5: a failed dead-letter still marks the file processed (never re-picked
forever) and logs the secondary failure.
M6: an unreadable podcast_episodes table alerts instead of silently
rendering "Episode ?".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_failed_deadletter_still_marks_processed(monkeypatch):
    """R2 down during dead-letter: the key must land in manifest['processed']
    so the same bad file is not reprocessed every pass forever."""
    from agent import intake_ingest

    class _R2:
        def list_keys(self, prefix):
            return [f"{prefix}20260708_bad.jpg"]

        def get_bytes(self, key):
            return b"not really an image"

        def put_bytes(self, key, data):
            if "deadletter" in key:
                raise RuntimeError("R2 down during dead-letter")

        def delete(self, key):
            raise RuntimeError("R2 down")

    alerts = []
    monkeypatch.setattr("agent.intake_ingest.ops_alerts.alert",
                        lambda m, **kw: alerts.append(m))

    manifest = {"processed": [], "sha256": [], "phash": []}
    monkeypatch.setattr(intake_ingest, "_load_manifest",
                        lambda r2, client: manifest)
    monkeypatch.setattr(intake_ingest, "_save_manifest",
                        lambda r2, client, m: None)

    def _boom_converter(data, name):
        raise RuntimeError("corrupt file")

    stats = intake_ingest._process_client(
        "gym_alpha", _R2(), poster=None, converter=_boom_converter,
        phash=lambda d, n: None, moderator=lambda d, n: (True, ""))

    assert stats["deadlettered"] == 1
    assert manifest["processed"], (
        "the bad file's key must be marked processed even when dead-letter "
        "itself fails, or it is re-picked forever")
    assert alerts, "the dead-letter must still alert"


def test_unreadable_episode_table_alerts(monkeypatch):
    from agent import episode_inbox

    alerts = []
    monkeypatch.setattr("agent.episode_inbox.ops_alerts.alert",
                        lambda m, **kw: alerts.append(m))

    class _BoomConn:
        def __enter__(self):
            raise RuntimeError("db locked")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(episode_inbox.db, "connect", lambda *a, **kw: _BoomConn())
    out = episode_inbox._latest_episode_from_db()
    assert out == {}
    assert alerts and "unreadable" in alerts[0]
