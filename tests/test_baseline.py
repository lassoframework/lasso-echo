"""
Baseline capture tests. Fully OFFLINE: stubbed Graph pages only, no network.
Asserts: capture-baseline writes a dated JSON with the expected shape from paged
Graph history; a no-token account is recorded as a gap (never guessed); the
token value never lands in the JSON or the summary; and NOTHING in the agent
schedules or imports it (manual-only by design, verified against the source).
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import baseline  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402

TOKEN = "tok_baseline_secret_789"
NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent")


class PagedGraph:
    """Serves a fixed sequence of Graph pages; follows paging.next like Meta does."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        payload = self.pages.pop(0)

        class R:
            status_code = 200

            def json(self):
                return payload

        return R()


class ExplodingHTTP:
    def get(self, *a, **k):
        raise AssertionError("network was called for an account with no token")

    post = get


def _acct(key="lasso_ig"):
    return Account(key=key, display_name=key, platform=Platform.INSTAGRAM,
                   token_env="BL_TEST_TOKEN", target_id_env="BL_TEST_TARGET")


def _pages():
    # Two pages: 2 posts in the last 2 weeks + 1 near the window's far edge.
    return [
        {"data": [{"timestamp": "2026-06-28T12:00:00+0000"},    # 3 days ago  -> week 0
                  {"timestamp": "2026-06-20T12:00:00+0000"}],   # 11 days ago -> week 1
         "paging": {"next": "https://graph.test/page2"}},
        {"data": [{"timestamp": "2026-05-10T12:00:00+0000"}],   # 52 days ago -> week 7
         "paging": {}},
    ]


# ---- 1. writes a dated JSON with the expected shape -------------------------------
def test_writes_dated_json_with_expected_shape(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BL_TEST_TOKEN", TOKEN)
    monkeypatch.setenv("BL_TEST_TARGET", "1789")
    graph = PagedGraph(_pages())
    path, summary = baseline.capture_baseline(
        http=graph, accounts=[_acct()], now=NOW, out_dir=str(tmp_path))

    assert os.path.basename(path) == "baseline_2026-07.json"
    on_disk = json.loads(open(path, encoding="utf-8").read())
    rec = on_disk["accounts"]["lasso_ig"]
    assert on_disk["window_weeks"] == 8
    assert rec["posts_total"] == 3
    assert rec["posts_per_week"] == [1, 1, 0, 0, 0, 0, 0, 1]
    assert rec["avg_posts_per_week"] == 0.38

    # the read hit the IG /media edge, followed paging, and asked for timestamps
    first_url, first_params = graph.calls[0]
    assert first_url.endswith("/1789/media")
    assert first_params["fields"] == "timestamp"
    assert graph.calls[1][0] == "https://graph.test/page2"

    # a human summary was printed
    out = capsys.readouterr().out
    assert "Pre Echo posting baseline" in out
    assert "lasso_ig: 3 post(s)" in out


# ---- 2. a no-token account is a recorded gap, never guessed ------------------------
def test_no_token_account_recorded_as_gap(monkeypatch, tmp_path):
    monkeypatch.delenv("BL_TEST_TOKEN", raising=False)
    path, summary = baseline.capture_baseline(
        http=ExplodingHTTP(), accounts=[_acct()], now=NOW, out_dir=str(tmp_path))
    assert summary["accounts"]["lasso_ig"] == {
        "platform": Platform.INSTAGRAM, "error": "no token set"}


def test_read_failure_recorded_not_guessed(monkeypatch, tmp_path):
    monkeypatch.setenv("BL_TEST_TOKEN", TOKEN)
    monkeypatch.setenv("BL_TEST_TARGET", "1789")

    class Failing:
        def get(self, url, params=None, timeout=None):
            class R:
                status_code = 500

                def json(self):
                    return {}

            return R()

    _, summary = baseline.capture_baseline(
        http=Failing(), accounts=[_acct()], now=NOW, out_dir=str(tmp_path))
    assert "read failed" in summary["accounts"]["lasso_ig"]["error"]


# ---- 3. the token never lands in the JSON or the summary ---------------------------
def test_token_never_in_json_or_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BL_TEST_TOKEN", TOKEN)
    monkeypatch.setenv("BL_TEST_TARGET", "1789")
    path, summary = baseline.capture_baseline(
        http=PagedGraph(_pages()), accounts=[_acct()], now=NOW, out_dir=str(tmp_path))
    assert TOKEN not in open(path, encoding="utf-8").read()
    assert TOKEN not in str(summary)
    assert TOKEN not in capsys.readouterr().out


# ---- 4. manual-only: NOTHING in the agent schedules or imports baseline ------------
def test_nothing_in_the_agent_schedules_baseline():
    for name in sorted(os.listdir(AGENT_DIR)):
        if not name.endswith(".py") or name in ("baseline.py", "__main__.py"):
            continue
        src = open(os.path.join(AGENT_DIR, name), encoding="utf-8").read()
        assert "capture_baseline" not in src, f"{name} calls capture_baseline"
        assert "from .baseline" not in src and "from agent.baseline" not in src, (
            f"{name} imports the baseline module")
