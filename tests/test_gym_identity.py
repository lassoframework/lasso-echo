"""
gym_identity: the overlay's identity anchor, read from the gym's OWN row.

Why this module exists: story_overlay refuses to burn a Story without a city/brand
token and carries no default of its own ("the frontend must supply it"). Right for a
coach tap; impossible for a request Echo builds server-side. The event story offer is
exactly that, and it HELD every time. These tests pin that the anchor is READ, never
invented, and that an unknowable gym still resolves to nothing (so the rail still fires).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import gym_identity as gi  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = ""

    def json(self):
        return self._p


class _Http:
    """Fake shared plane. `roster` maps base->gym_id; `gyms` is the row it points at."""

    def __init__(self, roster=None, gym_row=None, fail=None):
        self.roster = roster if roster is not None else [
            {"gym_id": "uuid-1", "echo_account_key": "pierce"}]
        self.gym_row = gym_row
        self.fail = fail or set()
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if "echo_intake_tokens" in url:
            if "roster" in self.fail:
                return _Resp(None, 500)
            return _Resp(self.roster)
        if url.endswith("/gyms"):
            if "gyms" in self.fail:
                return _Resp(None, 500)
            return _Resp([self.gym_row] if self.gym_row else [])
        return _Resp([])


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr("agent.config.supabase_url", lambda: "https://sb.test")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "k")
    gi.reset_cache()
    yield
    gi.reset_cache()


# ---- the pure part ----------------------------------------------------------
def test_tokens_from_takes_the_name_and_the_city_half_of_market():
    assert gi.tokens_from("Pierce Fitness", "Carmel, IN") == ["Pierce Fitness", "Carmel"]


def test_tokens_from_dedupes_case_insensitively():
    assert gi.tokens_from("Carmel", "carmel, IN") == ["Carmel"]


def test_tokens_from_drops_blanks_and_trims():
    assert gi.tokens_from("  Pierce  ", "   ") == ["Pierce"]
    assert gi.tokens_from(None, None) == []
    assert gi.tokens_from("", "") == []


def test_tokens_from_mirrors_the_portal_helper():
    """identityTokensFrom in lasso-ops-portal must agree with this, or the coach lane
    and the event lane would anchor a gym differently."""
    assert gi.tokens_from("Pierce Fitness", None) == ["Pierce Fitness"]
    assert gi.tokens_from(None, "Carmel, IN") == ["Carmel"]


# ---- the two-hop lookup -----------------------------------------------------
def test_tokens_for_joins_the_roster_then_the_gym_row():
    http = _Http(gym_row={"name": "Pierce Fitness", "market": "Carmel, IN"})
    assert gi.tokens_for("pierce", http=http) == ["Pierce Fitness", "Carmel"]
    # the roster is filtered server-side on the base key, not scanned client-side.
    assert http.calls[0][1]["echo_account_key"] == "eq.pierce"
    assert http.calls[1][1]["id"] == "eq.uuid-1"


def test_an_unmapped_base_resolves_to_nothing_so_the_rail_still_fires():
    """LASSO's own accounts and test bases are not portal gyms. [] is a real answer:
    the caller reaches story_overlay's refusal and HOLDS honestly."""
    http = _Http(roster=[])
    assert gi.tokens_for("lasso_ig", http=http) == []


def test_a_gym_with_no_name_resolves_to_nothing_rather_than_a_guess():
    http = _Http(gym_row={"name": "", "market": ""})
    assert gi.tokens_for("pierce", http=http) == []


def test_a_read_failure_is_not_a_crash():
    assert gi.tokens_for("pierce", http=_Http(fail={"roster"})) == []
    assert gi.tokens_for("pierce", http=_Http(fail={"gyms"})) == []


def test_a_throwing_http_client_is_not_a_crash():
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    assert gi.tokens_for("pierce", http=_Boom()) == []


def test_no_creds_resolves_to_nothing(monkeypatch):
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    assert gi.tokens_for("pierce", http=_Http()) == []


def test_an_empty_base_never_reaches_the_network():
    http = _Http()
    assert gi.tokens_for("", http=http) == []
    assert http.calls == []


# ---- caching ----------------------------------------------------------------
def test_the_second_lookup_is_cached_not_refetched():
    http = _Http(gym_row={"name": "Pierce Fitness", "market": "Carmel, IN"})
    gi.tokens_for("pierce", http=http)
    calls = len(http.calls)
    assert gi.tokens_for("pierce", http=http) == ["Pierce Fitness", "Carmel"]
    assert len(http.calls) == calls, "a render must not pay the round trip twice"


def test_a_MISS_is_cached_too_so_a_nameless_gym_is_not_hammered():
    http = _Http(roster=[])
    gi.tokens_for("nope", http=http)
    calls = len(http.calls)
    gi.tokens_for("nope", http=http)
    assert len(http.calls) == calls


def test_the_cache_returns_a_COPY_so_a_caller_cannot_poison_it():
    http = _Http(gym_row={"name": "Pierce Fitness", "market": "Carmel, IN"})
    first = gi.tokens_for("pierce", http=http)
    first.append("INJECTED")
    assert gi.tokens_for("pierce", http=http) == ["Pierce Fitness", "Carmel"]
