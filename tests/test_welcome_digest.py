"""
Daily new-client welcome digest to Slack. Fully OFFLINE.

Asserts: flag defaults OFF; the digest lists today's-served + queued welcome posts
with the template caption + hosted feed image; nothing new -> no message; kv dedupe;
never fabricates (shows only the queue's own caption/url).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, welcome_digest as wd  # noqa: E402


def _rows():
    return [
        {"gym_key": "cust:1", "name": "Bird Dog CrossFit", "owner": "Sam",
         "tier": "Launch", "caption": "Welcome to the LASSO family, Bird Dog CrossFit.",
         "feed_url": "https://r2/feed1.png", "story_url": "https://r2/s1.png",
         "status": "queued", "served_day": ""},
        {"gym_key": "portal:2", "name": "CrossFit Sunnyside", "owner": "",
         "tier": "", "caption": "Big welcome to CrossFit Sunnyside.",
         "feed_url": "https://r2/feed2.png", "story_url": "",
         "status": "served", "served_day": "2026-08-12"},
        {"gym_key": "cust:3", "name": "Old Gym", "owner": "", "tier": "",
         "caption": "x", "feed_url": "https://r2/old.png",
         "status": "served", "served_day": "2026-08-05"},   # old, not today
    ]


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_WELCOME_DIGEST", raising=False)
    assert config.welcome_digest_enabled() is False
    assert wd.run_daily() is None


def test_digest_shows_today_and_queued_with_template_and_image():
    out = wd.build_digest("2026-08-12", _rows())
    assert out["served_today"] == 1 and out["queued"] == 1
    t = out["text"]
    assert "Bird Dog CrossFit" in t and "CrossFit Sunnyside" in t
    assert "Welcome to the LASSO family" in t          # the template caption
    assert "https://r2/feed1.png" in t                 # hosted image link
    assert "Old Gym" not in t                           # served on a past day, excluded
    assert "TODAY" in t and "QUEUED" in t


def test_nothing_new_returns_none():
    rows = [{"gym_key": "c", "name": "G", "status": "served",
             "served_day": "2026-08-01", "caption": "x", "feed_url": "u"}]
    assert wd.build_digest("2026-08-12", rows) is None


class _KV(dict):
    def get(self, k, d=""):
        return dict.get(self, k, d)

    def set(self, k, v):
        self[k] = v


def test_run_daily_dedupes_per_day(monkeypatch):
    monkeypatch.setenv("AGENT_WELCOME_DIGEST", "true")
    kv, alerts = _KV(), []
    r1 = wd.run_daily(now=_dt("2026-08-12"), kv=kv,
                      alert=lambda m, **k: alerts.append(m), rows=_rows())
    assert r1 and len(alerts) == 1
    r2 = wd.run_daily(now=_dt("2026-08-12"), kv=kv,
                      alert=lambda m, **k: alerts.append(m), rows=_rows())
    assert r2 is None and len(alerts) == 1              # same-day dedupe


def _dt(day):
    from datetime import datetime, timezone
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, 12, tzinfo=timezone.utc)
