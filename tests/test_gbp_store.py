"""
GbpStore (agent/gbp_store.py): the availability guard reflects real creds, the
idempotency reader excludes terminal rows, and onboarding_intake reads through the
store's own _get. Offline via a fake base store capturing PostgREST params.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.gbp_store import GbpStore  # noqa: E402


class _FakeBase:
    """Stands in for SupabaseCalendarStore: carries _url/_key and captures _get calls."""

    def __init__(self, url="", key="", rows=None):
        self._url = url
        self._key = key
        self.captured = []
        self._rows = rows or []

    # GbpStore._get calls self._s._client().get(...); we bypass that by giving GbpStore a
    # base that ALSO exposes _get so we intercept at the higher level in these tests.


class _CapturingStore(GbpStore):
    """GbpStore whose _get is stubbed to capture (table, params) and return canned rows."""

    def __init__(self, base, rows=None):
        super().__init__(base=base)
        self._rows = rows or []

    def _get(self, table, params):
        self._s.captured.append((table, params))
        return self._rows


def test_available_false_without_creds():
    assert GbpStore(base=_FakeBase(url="", key="")).available() is False


def test_available_true_with_creds():
    assert GbpStore(base=_FakeBase(url="https://x.supabase.co", key="svc")).available() is True


def test_available_prefers_base_available_when_present():
    class _WithAvail(_FakeBase):
        def available(self):
            return False
    # base says unavailable even though creds look set -> honored
    s = GbpStore(base=_WithAvail(url="u", key="k"))
    assert s.available() is False


def test_future_gbp_rows_excludes_terminal_statuses():
    base = _FakeBase(url="u", key="k")
    s = _CapturingStore(base)
    s.future_gbp_rows("lasso", "2026-09-01")
    table, params = base.captured[-1]
    assert table == "content_calendar"
    assert params["account"] == "eq.googlebusiness"
    assert params["gym_id"] == "eq.lasso"
    assert params["status"] == "not.in.(failed,denied,deleted)"


def test_onboarding_intake_queries_by_base_name():
    base = _FakeBase(url="u", key="k")
    s = _CapturingStore(base, rows=[{"business_name": "LASSO", "offers": [], "ghl_link": ""}])
    rec = s.onboarding_intake("lasso_ig")
    table, params = base.captured[-1]
    assert table == "onboarding_intake"
    assert params["business_name"] == "ilike.*lasso*"   # base key, suffix stripped
    assert rec["business_name"] == "LASSO"


def test_onboarding_intake_none_when_empty():
    s = _CapturingStore(_FakeBase(url="u", key="k"), rows=[])
    assert s.onboarding_intake("lasso") is None


def test_any_gbp_rows_queries_gym_scoped():
    base = _FakeBase(url="u", key="k")
    s = _CapturingStore(base, rows=[{"id": "x"}])
    assert s.any_gbp_rows("lasso") is True
    table, params = base.captured[-1]
    assert table == "content_calendar"
    assert params["account"] == "eq.googlebusiness" and params["gym_id"] == "eq.lasso"


def test_any_gbp_rows_false_when_empty():
    s = _CapturingStore(_FakeBase(url="u", key="k"), rows=[])
    assert s.any_gbp_rows("lasso") is False


def test_release_coach_review_patches_status():
    # release flips coach_review -> pending via a filtered PATCH
    captured = {}

    class _Resp:
        status_code = 200
        text = "[]"
        def json(self):
            return [{"id": "1", "status": "pending"}, {"id": "2", "status": "pending"}]

    class _Client:
        def patch(self, url, params=None, headers=None, json=None, timeout=None):
            captured["params"] = params
            captured["json"] = json
            return _Resp()

    class _Base(_FakeBase):
        def _client(self):
            return _Client()
        def _rest(self, path):
            return f"https://x/rest/v1/{path}"
        def _headers(self, extra=None):
            return {}

    s = GbpStore(base=_Base(url="u", key="k"))
    released = s.release_coach_review("lasso")
    assert len(released) == 2
    assert captured["params"]["status"] == "eq.coach_review"
    assert captured["params"]["gym_id"] == "eq.lasso"
    assert captured["json"] == {"status": "pending"}
