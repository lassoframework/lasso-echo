"""/v1/profiles must be read in FULL, not just the first page.

Every Zernio alias lookup (find_profile_id, find_profile_id_any) matches over
list_profiles(). That call used to send a bare {"limit": 100} and read one page, so
the moment the LASSO org passed 100 profiles, every gym whose profile sorted past the
first page would silently fail to match and Echo would CREATE A DUPLICATE profile under
its account_key. That is exactly the Zanshin/Pete bug (2026-08-27) re-opened for the
whole roster at once, and it would have arrived quietly.

These tests drive the REAL ZernioClient.list_profiles with a stubbed transport, because
every other zernio fake in the suite overrides list_profiles wholesale and therefore
cannot see paging at all.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio as z       # noqa: E402


class _PagedTransport:
    """Serves `n` profiles named p0..p(n-1) through Zernio's skip/limit contract."""

    def __init__(self, n, total_override=None, short_page_at=None, omit_total=False,
                 ignore_skip=False):
        self.n = n
        self.total_override = total_override
        self.short_page_at = short_page_at
        self.omit_total = omit_total
        self.ignore_skip = ignore_skip
        self.calls = []

    def __call__(self, path, params=None):
        params = params or {}
        skip = int(params.get("skip") or 0)
        limit = int(params.get("limit") or 100)
        self.calls.append((path, skip, limit))
        rows = [{"_id": f"id{i:04d}", "name": f"p{i}"} for i in range(self.n)]
        page = rows[0:limit] if self.ignore_skip else rows[skip:skip + limit]
        if self.short_page_at is not None and skip == self.short_page_at:
            page = page[:1]
        out = {"profiles": page, "skip": skip, "limit": limit}
        if not self.omit_total:
            out["total"] = self.n if self.total_override is None else self.total_override
        return out


def _client(transport):
    c = z.ZernioClient.__new__(z.ZernioClient)     # no network, no api key needed
    c._get = transport
    return c


def test_single_page_org_still_makes_exactly_one_call():
    t = _PagedTransport(12)
    out = _client(t).list_profiles()
    assert len(out["profiles"]) == 12
    assert len(t.calls) == 1, "a small org must not pay for extra round trips"


def test_reads_every_page_past_the_100_profile_cliff():
    t = _PagedTransport(250)
    out = _client(t).list_profiles()
    names = [p["name"] for p in out["profiles"]]
    assert len(names) == 250
    assert names[0] == "p0" and names[-1] == "p249"
    assert len(names) == len(set(names)), "no duplicates across pages"
    assert [c[1] for c in t.calls] == [0, 100, 200]


def test_a_gym_on_page_three_is_still_found_by_alias():
    # The actual regression: the Zanshin-style lookup must resolve even when the gym's
    # profile sorts well past the first page, so no duplicate is ever created.
    t = _PagedTransport(250)
    t.n = 250
    rows = [{"_id": f"id{i:04d}", "name": f"p{i}"} for i in range(250)]
    rows[233] = {"_id": "zanshin_pid_24charxxxxx", "name": "Zanshin Fitness"}

    def transport(path, params=None):
        params = params or {}
        skip = int(params.get("skip") or 0)
        limit = int(params.get("limit") or 100)
        return {"profiles": rows[skip:skip + limit], "total": len(rows),
                "skip": skip, "limit": limit}

    c = _client(transport)
    assert c.find_profile_id("Zanshin Fitness") == "zanshin_pid_24charxxxxx"
    assert c.find_profile_id_any("zanshinfitness630e22",
                                 "Zanshin Fitness") == "zanshin_pid_24charxxxxx"


def test_exact_multiple_of_the_page_size_terminates():
    t = _PagedTransport(200)
    out = _client(t).list_profiles()
    assert len(out["profiles"]) == 200
    assert [c[1] for c in t.calls] == [0, 100]


def test_a_short_page_ends_the_walk_even_if_total_disagrees():
    # An inflated `total` must never cause an endless walk.
    t = _PagedTransport(150, total_override=99999)
    out = _client(t).list_profiles()
    assert len(out["profiles"]) == 150
    assert len(t.calls) <= 3


def test_garbage_total_does_not_raise_and_returns_page_one():
    t = _PagedTransport(40, total_override="lots")
    out = _client(t).list_profiles()
    assert len(out["profiles"]) == 40


def test_missing_profiles_key_is_tolerated():
    c = _client(lambda path, params=None: {})
    assert c.list_profiles()["profiles"] == []


def test_none_response_is_tolerated():
    c = _client(lambda path, params=None: None)
    assert c.list_profiles()["profiles"] == []


# ---------------------------------------------------------------------------
# The stop condition must be a SHORT PAGE, never `total`.
#
# The live 2026-08-06 response recorded in zernio.py's own docstring carries NO
# `total` field. A total-driven loop reads exactly one page and quietly does
# nothing, which looks like a working fix while gym 101 still gets a duplicate
# profile created under its account_key.
# ---------------------------------------------------------------------------
def test_paginates_when_the_server_sends_NO_total_field():
    t = _PagedTransport(250, omit_total=True)
    out = _client(t).list_profiles()
    assert len(out["profiles"]) == 250, "pagination must not depend on `total`"
    assert [c[1] for c in t.calls] == [0, 100, 200]


def test_finds_a_page_three_gym_with_no_total_field():
    rows = [{"_id": f"id{i:04d}", "name": f"p{i}"} for i in range(250)]
    rows[233] = {"_id": "zanshin_pid_24charxxxxx", "name": "Zanshin Fitness"}

    def transport(path, params=None):
        params = params or {}
        skip, limit = int(params.get("skip") or 0), int(params.get("limit") or 100)
        return {"profiles": rows[skip:skip + limit], "skip": skip, "limit": limit}

    assert _client(transport).find_profile_id("Zanshin Fitness") == "zanshin_pid_24charxxxxx"


def test_a_server_that_ignores_skip_stops_instead_of_looping_to_the_cap():
    # Replaying page one forever must terminate on the first repeated id, not
    # accumulate duplicates until the page cap.
    t = _PagedTransport(250, omit_total=True, ignore_skip=True)
    out = _client(t).list_profiles()
    ids = [p["_id"] for p in out["profiles"]]
    assert len(ids) == len(set(ids)), "must never return duplicate profiles"
    assert len(t.calls) <= 3, f"should stop fast, made {len(t.calls)} calls"


def test_page_cap_is_bounded_by_requests_not_by_a_server_count():
    # A huge org must not hang a synchronous connect handler.
    from agent.zernio import ZernioClient
    assert ZernioClient._PROFILE_MAX_PAGES <= 50
