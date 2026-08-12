"""
Extra welcomes per day (catch-up lane): AGENT_WELCOME_PER_DAY posts (N-1) more welcomes
beyond the daily run's one, each feed->lasso_ig+lasso_fb + story->lasso_ig, through the
gated post path. Offline (in-memory queue, injected poster).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, welcome_queue as wq, db  # noqa: E402


def _seed(monkeypatch, tmp_path, n):
    # isolate the sqlite db + arm the queue
    monkeypatch.setenv("AGENT_WELCOME_QUEUE_ENABLED", "true")
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "echo.db"), raising=False)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "echo.db"), raising=False)
    with db._lock, wq._conn() as conn:
        conn.executescript(wq._SCHEMA)
        for i in range(n):
            conn.execute(
                "INSERT INTO welcome_queue (gym_key,name,caption,feed_url,story_url,status) "
                "VALUES (?,?,?,?,?, 'queued')",
                (f"g{i}", f"Gym {i}", f"Welcome Gym {i}",
                 f"https://r2/feed{i}.png", f"https://r2/story{i}.png"))
        conn.commit()


def test_flag_defaults_to_one(monkeypatch):
    monkeypatch.delenv("AGENT_WELCOME_PER_DAY", raising=False)
    assert config.welcome_per_day() == 1


def test_per_day_one_posts_no_extras(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, 5)
    monkeypatch.setenv("AGENT_WELCOME_PER_DAY", "1")
    posts = []
    out = wq.publish_extra_welcomes("2026-08-13", post_fn=posts.append)
    assert out == [] and posts == []                  # daily run posts the only one


def test_per_day_two_posts_one_extra_gym(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, 5)
    monkeypatch.setenv("AGENT_WELCOME_PER_DAY", "2")
    drafts = []
    out = wq.publish_extra_welcomes("2026-08-13", post_fn=drafts.append)
    assert len(out) == 1                              # one EXTRA gym
    # that gym posts: feed on lasso_ig + lasso_fb, story on lasso_ig = 3 drafts
    assert len(drafts) == 3
    keys = sorted(d.account_key for d in drafts)
    assert keys == ["lasso_fb", "lasso_ig", "lasso_ig"]
    assert all(d.topic_type == "WELCOME" for d in drafts)
    types = sorted(d.draft_type for d in drafts)
    assert types == ["feed", "feed", "story"]


def test_extra_lane_advances_to_new_gyms(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, 5)
    monkeypatch.setenv("AGENT_WELCOME_PER_DAY", "3")
    drafts = []
    out = wq.publish_extra_welcomes("2026-08-13", post_fn=drafts.append)
    assert len(out) == 2                              # 2 extras (per_day 3 - 1)
    assert out[0] != out[1]                           # distinct gyms, never the same
    # both gyms are now marked served for the day
    with wq._conn() as conn:
        served = conn.execute("SELECT count(*) FROM welcome_queue WHERE served_day=?",
                              ("2026-08-13",)).fetchone()[0]
    assert served == 2


def test_stops_when_queue_exhausted(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, 1)                   # only ONE queued
    monkeypatch.setenv("AGENT_WELCOME_PER_DAY", "5")  # want 4 extras
    out = wq.publish_extra_welcomes("2026-08-13", post_fn=lambda d: None)
    assert len(out) == 1                              # only what exists, no crash


def test_off_when_queue_disabled(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, 5)
    monkeypatch.delenv("AGENT_WELCOME_QUEUE_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_WELCOME_PER_DAY", "3")
    assert wq.publish_extra_welcomes("2026-08-13", post_fn=lambda d: None) == []
