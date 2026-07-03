"""
Insights backfill tests. Offline (fake Graph, recorded sleeps, zero real
waiting). Asserts: idempotency (two runs, one row set, values stable, no
duplicate rows); a 429 backs off exponentially and completes; --dry makes no
Graph call and writes nothing; views never impressions in the request.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import backfill, db  # noqa: E402


class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class FakeGraph:
    def __init__(self, ratelimit_first=0):
        self.requests = []
        self.remaining_429 = ratelimit_first

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": dict(params or {})})
        if self.remaining_429 > 0:
            self.remaining_429 -= 1
            return FakeResp(429, {"error": {"code": 4}})
        return FakeResp(200, {"data": [
            {"name": "views", "values": [{"value": 111}]},
            {"name": "likes", "values": [{"value": 7}]},
        ]})


def _seed_posts(monkeypatch, n=2):
    monkeypatch.setenv("AGENT_LASSO_IG_TOKEN", "tok-backfill")
    with db.connect() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO posts (draft_id, account_key, platform, caption, "
                "media_id, mode, published_at) VALUES (?, 'lasso_ig', 'instagram', "
                "'c', ?, 'published', ?)",
                (f"d{i}", f"M{i}", f"2026-07-0{i + 1}T10:00:00"))
        conn.commit()


def _rows():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT media_id, views, likes FROM posts ORDER BY id").fetchall()]


def test_idempotent_upsert_never_duplicates(monkeypatch):
    _seed_posts(monkeypatch)
    graph = FakeGraph()
    out1 = backfill.backfill_insights("lasso_ig", "2026-07-01", http=graph,
                                      sleeper=lambda s: None)
    out2 = backfill.backfill_insights("lasso_ig", "2026-07-01", http=graph,
                                      sleeper=lambda s: None)
    assert out1["updated"] == 2 and out2["updated"] == 2
    rows = _rows()
    assert len(rows) == 2                                  # NO duplicate rows
    assert all(r["views"] == 111 and r["likes"] == 7 for r in rows)
    # views requested, impressions never
    metrics = ",".join(r["params"].get("metric", "") for r in graph.requests)
    assert "views" in metrics and "impressions" not in metrics


def test_429_backs_off_and_completes(monkeypatch):
    _seed_posts(monkeypatch, n=1)
    graph = FakeGraph(ratelimit_first=2)                   # two 429s, then 200
    sleeps = []
    out = backfill.backfill_insights("lasso_ig", "2026-07-01", http=graph,
                                     sleeper=sleeps.append)
    assert out["updated"] == 1 and out["skipped"] == 0     # completed
    assert sleeps == [1, 2]                                # exponential backoff
    assert _rows()[0]["views"] == 111


def test_dry_makes_no_graph_call_writes_nothing(monkeypatch, capsys):
    _seed_posts(monkeypatch, n=2)

    class Exploding:
        def get(self, *a, **k):
            raise AssertionError("Graph call during --dry")

    out = backfill.backfill_insights("lasso_ig", "2026-07-01", dry=True,
                                     http=Exploding())
    assert out == {"posts": 2, "updated": 0, "skipped": 0}
    assert all(r["views"] is None for r in _rows())        # nothing written
    printed = capsys.readouterr().out
    assert "would backfill M0" in printed and "DRY" in printed


def test_since_filter_reads_store_only(monkeypatch):
    _seed_posts(monkeypatch, n=2)                          # 07-01 and 07-02
    graph = FakeGraph()
    out = backfill.backfill_insights("lasso_ig", "2026-07-02", http=graph,
                                     sleeper=lambda s: None)
    assert out["posts"] == 1                               # only the later post
    assert all("M1" in r["url"] for r in graph.requests)   # ids from the store
