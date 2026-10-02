"""Regression coverage for concurrent first-open schema migrations."""

import sqlite3
import threading

from agent import db


def test_concurrent_connect_migrates_old_posts_schema_once(tmp_path):
    """Concurrent callers cannot race the additive post-column migrations."""
    path = tmp_path / "old_echo.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE posts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT, account_key TEXT, "
            "platform TEXT, caption TEXT, media_id TEXT, permalink TEXT, mode TEXT, "
            "creative_key TEXT, archetype TEXT, set_name TEXT, published_at TEXT)"
        )

    barrier = threading.Barrier(8)
    errors = []
    errors_lock = threading.Lock()

    def connect():
        try:
            barrier.wait()
            conn = db.connect(str(path))
            conn.close()
        except Exception as exc:  # captured so all racing callers can finish
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=connect) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
    assert set(db._POST_METRIC_COLUMNS).issubset(columns)
