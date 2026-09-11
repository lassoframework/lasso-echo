"""
agent/echo_clients.py -- the ONE Echo client universe predicate (2026-09-11 incident:
echo_intake_tokens was treated as the client list; it is the whole LASSO fleet).

Every test here switches OFF the suite-wide allow-all override (conftest) and drives
the real predicate over fake readings. No network.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import echo_clients as ec  # noqa: E402

TOUGH = "0f0f0f0f-0000-4000-8000-000000000001"     # an Echo client (echo_gym_settings)
LOCAL = "0f0f0f0f-0000-4000-8000-000000000002"     # an Echo client, legacy hand key
BOOM = "0f0f0f0f-0000-4000-8000-000000000003"      # ads-only: token row, NO settings row
LEAD = "0f0f0f0f-0000-4000-8000-000000000004"      # ads-only, no token row at all

SETTINGS = [{"gym_id": TOUGH}, {"gym_id": LOCAL}]
TOKENS = [{"gym_id": TOUGH, "echo_account_key": "toughtemple52040e"},
          {"gym_id": TOUGH, "echo_account_key": "toughtemple086f51"},   # split key
          {"gym_id": LOCAL, "echo_account_key": "crossfitlocal"},
          {"gym_id": BOOM, "echo_account_key": "boomfitbcs3b4da7"}]
GYMS = [{"id": TOUGH, "name": "Tough Temple", "slug": "tough-temple"},
        {"id": LOCAL, "name": "CrossFit Local", "slug": "crossfit-local"},
        {"id": BOOM, "name": "BoomFit BCS", "slug": "boomfit-bcs"},
        {"id": LEAD, "name": "Dean Holcomb", "slug": "dean-holcomb"}]


def _set():
    return ec.build(SETTINGS, TOKENS, GYMS)


# ---- the pure build -------------------------------------------------------------------

def test_clients_are_exactly_the_settings_rows():
    s = _set()
    assert s.ok
    assert s.gym_ids == {TOUGH, LOCAL}
    assert s.is_client(TOUGH) and s.is_client(LOCAL)
    assert not s.is_client(BOOM) and not s.is_client(LEAD)


def test_a_token_row_alone_is_not_a_client():
    """THE incident: BoomFit has an echo_intake_tokens row (the portal mints one for
    every gym) and no echo_gym_settings row. Not a client, by id or by key."""
    s = _set()
    assert not s.is_client(BOOM)
    assert not s.is_client("boomfitbcs3b4da7")
    assert not s.is_client("boomfitbcs3b4da7_ig")
    assert "boomfitbcs3b4da7" in s.other_keys
    assert BOOM in s.other_gym_ids and LEAD in s.other_gym_ids


def test_every_alias_of_a_client_gym_is_admitted():
    s = _set()
    for alias in ("toughtemple52040e", "toughtemple086f51",       # both token rows
                  "tough-temple", "toughtemple",                   # slug + bare name
                  ec._echo_key(TOUGH, "Tough Temple"),             # Echo derivation
                  ec._portal_key(TOUGH, "Tough Temple"),           # portal derivation
                  "toughtemple52040e_ig", "TOUGHTEMPLE52040E_fb"):  # suffixed / cased
        assert s.is_client(alias), alias
        assert s.key_to_gym[ec.normalize_key(alias)] == TOUGH
    assert s.is_client("crossfitlocal") and s.is_client("crossfitlocal_ig")


def test_derivations_match_the_two_real_mint_sites():
    """Echo: slug + sha256(id)[:6] (account_key._base_key). Portal: slug + rawUUID[:6]
    (social-onboard.ts). Both must be admitted, or split gyms lose half their data."""
    from agent.account_key import _base_key
    assert ec._echo_key(TOUGH, "Tough Temple") == _base_key(TOUGH, "Tough Temple")
    assert ec._portal_key(TOUGH, "Tough Temple") == "toughtemple0f0f0f"
    assert ec._portal_key("30b5b234-0dac-4048-87d8-5330e6fbfa9d", "CrossFit Reverb") \
        == "crossfitreverb30b5b2"


def test_a_client_alias_can_never_be_a_non_client_marker():
    """If a non-client's token key equalled a client alias the client wins: other_keys
    is defined minus keys, so the cleanup can never remove a client under that key."""
    tokens = TOKENS + [{"gym_id": BOOM, "echo_account_key": "toughtemple"}]
    s = ec.build(SETTINGS, tokens, GYMS)
    assert s.is_client("toughtemple")
    assert "toughtemple" not in s.other_keys


def test_empty_settings_is_refused_not_believed():
    s = ec.build([], TOKENS, GYMS)
    assert not s.ok and "zero rows" in s.error
    assert not s.is_client(TOUGH)      # unknown = not a client


def test_unknown_set_answers_false_for_everything():
    s = ec.ClientSet(ok=False, error="boom")
    assert not s.is_client(TOUGH) and not s.is_client("anything") and not s.is_client("")


def test_normalize_key():
    assert ec.normalize_key(" CrossFitLocal_IG ") == "crossfitlocal"
    assert ec.normalize_key("gritx_fb") == "gritx"
    assert ec.normalize_key(TOUGH.upper()) == TOUGH
    assert ec.normalize_key(None) == ""


# ---- the live reader, offline ---------------------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Http:
    """Routes by table name; supports the offset/limit paging the reader uses."""

    def __init__(self, tables, fail=None, status=200):
        self.tables = tables
        self.fail = set(fail or ())
        self.status = status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        table = url.rsplit("/rest/v1/", 1)[1]
        self.calls.append((table, dict(params or {})))
        assert "Bearer" in headers.get("Authorization", "")
        if "raise" in self.fail:
            raise ConnectionError("down")
        if table in self.fail:
            return _Resp(500, {"message": "boom"})
        rows = list(self.tables.get(table, []))
        flt = params.get("id")
        if flt and flt.startswith("in.("):
            wanted = set(flt[4:-1].split(","))
            rows = [r for r in rows if r.get("id") in wanted]
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 1000))
        return _Resp(self.status, rows[off:off + lim])


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key-not-real")


def _tables():
    return {"echo_gym_settings": SETTINGS, "echo_intake_tokens": TOKENS, "gyms": GYMS}


def test_live_load_reads_three_tables_and_only_client_gyms(creds, real_echo_clients):
    http = _Http(_tables())
    s = ec._load(http=http)
    assert s.ok and s.gym_ids == {TOUGH, LOCAL}
    tables = [t for t, _ in http.calls]
    assert tables == ["echo_gym_settings", "echo_intake_tokens", "gyms"]
    # the gyms read is scoped to the client ids; the fleet's 158 rows are never pulled
    gyms_params = [p for t, p in http.calls if t == "gyms"][0]
    assert gyms_params["id"].startswith("in.(") and BOOM not in gyms_params["id"]


def test_no_creds_fails_closed(real_echo_clients, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    s = ec._load(http=_Http(_tables()))
    assert not s.ok and "creds" in s.error
    assert ec.is_echo_client(TOUGH) is False


@pytest.mark.parametrize("broken", ["echo_gym_settings", "echo_intake_tokens", "gyms"])
def test_any_table_failing_fails_closed(creds, real_echo_clients, broken):
    s = ec._load(http=_Http(_tables(), fail={broken}))
    assert not s.ok and broken in s.error
    assert not s.is_client(TOUGH)


def test_transport_exception_fails_closed(creds, real_echo_clients):
    s = ec._load(http=_Http(_tables(), fail={"raise"}))
    assert not s.ok


def test_a_truncated_page_fails_closed(creds, real_echo_clients, monkeypatch):
    """PostgREST caps at 1000 rows; paging past _MAX_PAGES means the plane is bigger
    than the fleet could be, so the reading is INCOMPLETE and not believed."""
    monkeypatch.setattr(ec, "_PAGE", 1)
    monkeypatch.setattr(ec, "_MAX_PAGES", 2)
    s = ec._load(http=_Http(_tables()))       # 4 token rows > 2 pages of 1
    assert not s.ok


def test_paging_assembles_the_whole_table(creds, real_echo_clients, monkeypatch):
    monkeypatch.setattr(ec, "_PAGE", 1)
    monkeypatch.setattr(ec, "_MAX_PAGES", 20)
    s = ec._load(http=_Http(_tables()))
    assert s.ok and "boomfitbcs3b4da7" in s.other_keys and s.is_client("toughtemple086f51")


def test_predicate_functions_over_the_live_reader(creds, real_echo_clients):
    http = _Http(_tables())
    assert ec.is_echo_client(TOUGH, http=http)
    assert ec.is_echo_client("crossfitlocal_ig", http=http)
    assert not ec.is_echo_client(BOOM, http=http)
    assert not ec.is_echo_client("boomfitbcs3b4da7", http=http)
    assert ec.echo_client_gym_ids(http=http) == {TOUGH, LOCAL}
    assert "toughtemple086f51" in ec.echo_client_keys(http=http)
    assert ec.only_clients([TOUGH, BOOM, "crossfitlocal", "boomfitbcs3b4da7"], http=http) \
        == [TOUGH, "crossfitlocal"]


def test_good_reading_is_cached_five_minutes_and_failure_twenty_seconds(
        creds, real_echo_clients, monkeypatch):
    clock = {"t": 1000.0}
    http = _Http(_tables())
    ec.snapshot(http=http, now_fn=lambda: clock["t"])
    ec.snapshot(http=http, now_fn=lambda: clock["t"])
    assert len(http.calls) == 3                       # one read, three tables
    clock["t"] += 299
    ec.snapshot(http=http, now_fn=lambda: clock["t"])
    assert len(http.calls) == 3                       # still cached
    clock["t"] += 2
    ec.snapshot(http=http, now_fn=lambda: clock["t"])
    assert len(http.calls) == 6                       # re-read after 5 min
    # a failure is retried after 20s, not 300s
    bad = _Http(_tables(), fail={"echo_gym_settings"})
    ec.reset_cache()
    s = ec.snapshot(http=bad, now_fn=lambda: clock["t"])
    assert not s.ok
    clock["t"] += 19
    ec.snapshot(http=bad, now_fn=lambda: clock["t"])
    assert len(bad.calls) == 1
    clock["t"] += 2
    ec.snapshot(http=bad, now_fn=lambda: clock["t"])
    assert len(bad.calls) == 2


def test_fresh_forces_a_reread(creds, real_echo_clients):
    http = _Http(_tables())
    ec.snapshot(http=http)
    ec.snapshot(http=http, fresh=True)
    assert len(http.calls) == 6


def test_is_echo_client_never_raises(real_echo_clients, monkeypatch):
    monkeypatch.setattr(ec, "snapshot", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert ec.is_echo_client(TOUGH) is False
    assert ec.echo_client_keys() == frozenset()
    assert ec.echo_client_gym_ids() == frozenset()


# ---- the account-registry form --------------------------------------------------------

def test_hardcoded_bases_are_trusted_without_a_plane_read(real_echo_clients, monkeypatch):
    """LASSO's own daily run (and the four hand-written client gyms) never wait on
    Supabase: with the universe UNREADABLE they still pass, dynamic bases do not."""
    monkeypatch.setattr(ec, "_load", lambda http=None, now=None: ec.ClientSet(ok=False, error="down"))
    assert ec.only_client_bases(["lasso", "district_h", "eng", "gritx", "topfuel",
                                 "toughtemple52040e", "boomfitbcs3b4da7"]) \
        == ["lasso", "district_h", "eng", "gritx", "topfuel"]


def test_dynamic_bases_pass_by_stamped_gym_id_or_by_key(real_echo_clients, monkeypatch, tmp_path):
    import json
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps([
        {"base": "toughtemple086f51", "name": "Tough Temple", "gym_id": TOUGH},
        {"base": "boomfitbcs3b4da7", "name": "BoomFit BCS", "gym_id": BOOM},
        {"base": "renamedkey", "name": "Renamed", "gym_id": LOCAL},    # id vouches
        {"base": "crossfitlocal", "name": "CrossFit Local"},           # key vouches
        {"base": "orphan", "name": "Orphan"},
    ]))
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(reg))
    monkeypatch.setattr(ec, "_load", lambda http=None, now=None: _set())
    assert ec.only_client_bases(["toughtemple086f51", "boomfitbcs3b4da7", "renamedkey",
                                 "crossfitlocal", "orphan"]) \
        == ["toughtemple086f51", "renamedkey", "crossfitlocal"]


def test_the_suite_override_is_visible_and_reversible():
    """conftest installs allow-all for the suite; this file's fixture removes it. Make
    sure both directions do what they say, so no lane test passes by accident."""
    ec.set_test_override(lambda i: True)
    assert ec.is_echo_client("anything-at-all")
    ec.set_test_override(lambda i: False)
    assert not ec.is_echo_client(TOUGH)
    ec.set_test_override(None)
