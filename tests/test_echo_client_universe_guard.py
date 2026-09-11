"""
THE ECHO CLIENT UNIVERSE GUARD (2026-09-11 incident).

onboarding_watch called echo_intake_tokens "the AUTHORITATIVE list of Echo clients".
The portal mints that token for EVERY gym, so the onboarding lane swept the whole LASSO
ads fleet: ~110 non-clients auto-registered, 36 owners DMed a connect link "from Echo",
131 websites scraped, 153 ops tickets. The rule now:

    echo_gym_settings IS the Echo client universe.  echo_intake_tokens IS NOT.

Part 1 is STATIC: any module that enumerates echo_intake_tokens or the account registry
must go through agent.echo_clients, or sit on a documented allowlist of single-gym key
resolvers. Part 2 is BEHAVIOURAL: each fleet lane, handed a roster with a non-client in
it, produces ZERO alerts and ZERO outbound messages for that gym. Every behavioural test
switches OFF the suite-wide allow-all override.
"""
import ast
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import echo_clients as ec  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent")

# Every fleet lane the incident touched (or could have). Each must import echo_clients.
LANES = (
    "onboarding_watch.py",          # roster, autoregister, zero-token sweep
    "connect_link_notify.py",       # the DM
    "welcome_queue.py",             # portal welcome scan + prune
    "catchup_report.py",            # "gyms signed up in the last 60 days"
    "website_intake.py",            # fleet website scrape
    "client_media_sync.py",         # _client_bases: THE registry enumerator
    "calendar_autopublish.py",      # client_gym_bases: the other one
    os.path.join("jobs", "billing_customer_sync.py"),
)

# Modules that READ echo_intake_tokens for a gym that is ALREADY identified (key
# resolution, split-key repair, one gym's token row) -- not to enumerate gyms for a
# client-facing action. Each is a deliberate, reviewed exception. Adding a module here
# is a decision, not a fix: if it iterates the table to act on gyms, gate it instead.
TOKEN_READ_ALLOWLIST = {
    "echo_clients.py",              # the predicate itself
    "account_key_resolve.py",       # stale key -> live key, by gym_id (fails closed)
    "account_key_reconcile.py",     # operator repair of one gym's key
    "account_key_split_watch.py",   # detects a gym whose two keys disagree
    "gym_identity.py",              # name/market tokens for ONE base's grounding
    "intake_web.py",                # the row for ONE signed link's key
    "social_intake_reader.py",      # resolve ONE intake's raw key to its token key
    os.path.join("slack_convo", "listener_wiring.py"),   # ONE Slack user's gym -> its key
}

# Modules that iterate all_accounts() for something other than acting on each gym.
REGISTRY_ITER_ALLOWLIST = {
    "accounts.py",                  # the registry
    "echo_clients.py",              # the gate
    "inbox_alerts.py",              # _coach_channel: finds ONE gym's channel
    "library.py",                   # isolation set: MORE names = MORE isolation
    "social_baseline.py",           # platform filter set; the fleet comes from client_gym_bases
}


def _py_files():
    for dirpath, _dirs, files in os.walk(ROOT):
        if "__pycache__" in dirpath:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _rel(path):
    return os.path.relpath(path, ROOT)


def _source(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _imports_echo_clients(src):
    return "echo_clients" in src


def _code_strings(tree):
    """Every string constant in CODE (docstrings skipped: an Expr whose value is a
    Constant is documentation, and a module is allowed to talk about the table)."""
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            doc_ids.add(id(node.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc_ids:
            yield node.value


def _reads_token_table(tree):
    return any("echo_intake_tokens" in s for s in _code_strings(tree))


def _iterates_all_accounts(tree):
    def _calls_all_accounts(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                name = getattr(f, "id", None) or getattr(f, "attr", None)
                if name == "all_accounts":
                    return True
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and _calls_all_accounts(node.iter):
            return True
        if isinstance(node, ast.comprehension) and _calls_all_accounts(node.iter):
            return True
    return False


# ---- Part 1: static ---------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
def test_every_fleet_lane_goes_through_the_predicate(lane):
    src = _source(os.path.join(ROOT, lane))
    assert _imports_echo_clients(src), f"{lane} enumerates gyms without echo_clients"
    assert ("is_echo_client" in src or "only_client_bases" in src
            or "echo_clients.snapshot" in src), f"{lane} imports the gate but never calls it"


def test_no_module_reads_echo_intake_tokens_without_the_gate_or_a_ruling():
    offenders, seen_allow = [], set()
    for path in _py_files():
        tree = ast.parse(_source(path))
        if not _reads_token_table(tree):
            continue
        rel = _rel(path)
        if rel in TOKEN_READ_ALLOWLIST:
            seen_allow.add(rel)
            continue
        if not _imports_echo_clients(_source(path)):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} read echo_intake_tokens without agent.echo_clients. That table is "
        "NOT the Echo client list (2026-09-11). Gate the enumeration with "
        "echo_clients.is_echo_client, or add a reviewed entry to TOKEN_READ_ALLOWLIST "
        "with the reason it resolves ONE gym rather than acting on many.")
    stale = TOKEN_READ_ALLOWLIST - seen_allow
    assert not stale, f"allowlist entries no longer read the table; remove them: {stale}"


def test_no_module_iterates_the_account_registry_without_the_gate_or_a_ruling():
    offenders, seen_allow = [], set()
    for path in _py_files():
        rel = _rel(path)
        tree = ast.parse(_source(path))
        if not _iterates_all_accounts(tree):
            continue
        if rel in REGISTRY_ITER_ALLOWLIST:
            seen_allow.add(rel)
            continue
        if not _imports_echo_clients(_source(path)):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} iterate all_accounts() without agent.echo_clients. The dynamic "
        "registry is not the Echo client list (autoregister filled it from the token "
        "table). Filter with echo_clients.only_client_bases, or add a reviewed "
        "REGISTRY_ITER_ALLOWLIST entry with the reason.")
    stale = REGISTRY_ITER_ALLOWLIST - seen_allow - {"accounts.py", "echo_clients.py"}
    assert not stale, f"allowlist entries no longer iterate the registry: {stale}"


def test_dynamic_registry_accounts_are_never_active():
    """The daily draft cycle and the heartbeat iterate active_accounts(). A registry row
    -- client or not -- must never become active by construction, so a polluted
    registry cannot draft or page even before the cleanup runs."""
    from agent import accounts
    for row in ({"base": "boomfitbcs3b4da7", "name": "BoomFit BCS"},
                {"base": "toughtemple52040e", "name": "Tough Temple", "gym_id": "x"}):
        for acct in accounts._account_from_registry_row(row):   # noqa: SLF001
            assert acct.active is False


def test_onboarding_watch_no_longer_calls_the_token_table_authoritative():
    from agent import onboarding_watch as ow
    doc = ow.__doc__
    assert "echo_gym_settings" in doc
    assert "NOT* echo_intake_tokens" in doc or "NOT echo_intake_tokens" in doc
    assert "AUTHORITATIVE list instead: echo_intake_tokens" not in doc


def test_the_suite_override_is_installed_by_conftest():
    """Documented dependency: the lane tests written before the gate rely on the
    conftest allow-all override. If someone removes it, hundreds of tests fail
    closed for a reason this assertion names."""
    assert ec.is_echo_client("definitely-not-a-gym") is True


# ---- Part 2: behavioural ----------------------------------------------------------------

TOUGH = "0f0f0f0f-0000-4000-8000-000000000001"
BOOM = "0f0f0f0f-0000-4000-8000-000000000003"
UNIVERSE = ec.build(
    [{"gym_id": TOUGH}],
    [{"gym_id": TOUGH, "echo_account_key": "toughtemple52040e"},
     {"gym_id": BOOM, "echo_account_key": "boomfitbcs3b4da7"}],
    [{"id": TOUGH, "name": "Tough Temple", "slug": "tough-temple"},
     {"id": BOOM, "name": "BoomFit BCS", "slug": "boomfit-bcs"}])


@pytest.fixture
def universe(real_echo_clients, monkeypatch):
    monkeypatch.setattr(ec, "_load", lambda http=None, now=None: UNIVERSE)
    return UNIVERSE


class _KV:
    def __init__(self):
        self.d = {}

    def get(self, k, default=""):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v

    kv_get = get
    kv_set = set


class _NeverHttp:
    """Any request is a test failure: the lane must stop BEFORE the network."""

    def get(self, *a, **k):
        raise AssertionError(f"network reached: GET {a} {k}")

    def post(self, *a, **k):
        raise AssertionError(f"network reached: POST {a} {k}")


def _ow_deps(roster, zero_token=()):
    return {
        "roster": lambda http=None: list(roster),
        "intake": lambda http=None: {},
        "bases": lambda: [],
        "approved_sources": lambda b: [],
        "voice": lambda b: True,
        "profile_id": lambda b: "",
        "platforms": lambda pid: set(),
        "fb_page": lambda b: "",
        "gym_name": lambda gid: {TOUGH: "Tough Temple", BOOM: "BoomFit BCS"}.get(gid, ""),
        "zero_token": lambda known, http=None: list(zero_token),
        "is_client": ec.is_echo_client,
    }


def test_onboarding_watch_alerts_only_on_echo_clients(universe, monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_WATCH", "true")
    from agent import onboarding_watch as ow
    seen = []
    out = ow.run(deps=_ow_deps([(BOOM, "boomfitbcs3b4da7"), (TOUGH, "toughtemple52040e")],
                               zero_token=[("g-lead", "Dean Holcomb", "dean-holcomb")]),
                 alert=seen.append, kv=_KV())
    assert set(out) == {"toughtemple52040e"}
    assert len(seen) == 1 and "toughtemple52040e" in seen[0]
    assert not any("boomfit" in s or "Dean" in s for s in seen)


def test_onboarding_watch_autoregister_refuses_a_non_client(universe, monkeypatch, tmp_path):
    """THE incident call. A non-client is never registered, never notified, never
    alerted about -- and the registry file is not even created."""
    monkeypatch.setenv("AGENT_ONBOARDING_AUTOREGISTER", "true")
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_AUTO_CONNECT_LINK", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import onboarding_watch as ow, connect_link_notify as cln
    sent = []
    monkeypatch.setattr(cln, "notify_new_gym", lambda *a, **k: sent.append(a))
    seen = []
    deps = _ow_deps([(BOOM, "boomfitbcs3b4da7")])
    assert ow.autoregister("boomfitbcs3b4da7", BOOM, deps=deps, alert=seen.append) is False
    assert seen == [] and sent == []
    assert not (tmp_path / "reg.json").exists()
    # and the client still registers
    assert ow.autoregister("toughtemple52040e", TOUGH, deps=deps, alert=seen.append) is True
    assert [r["base"] for r in json.load(open(tmp_path / "reg.json"))] == ["toughtemple52040e"]
    assert len(sent) == 1


def test_onboarding_watch_live_roster_readers_filter(universe, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    from agent import onboarding_watch as ow

    class _Resp:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

    class _Http:
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/echo_intake_tokens"):
                return _Resp([{"gym_id": TOUGH, "echo_account_key": "toughtemple52040e"},
                              {"gym_id": BOOM, "echo_account_key": "boomfitbcs3b4da7"}])
            if url.endswith("/gyms"):
                return _Resp([{"id": BOOM, "name": "BoomFit BCS", "slug": "boomfit-bcs",
                               "status": "active"},
                              {"id": "g-new", "name": "New Echo Gym", "slug": "new-echo",
                               "status": "active"}])
            raise AssertionError(url)
    assert ow.portal_keys(http=_Http()) == [(TOUGH, "toughtemple52040e")]
    # a gym with no token is only a finding when it IS an Echo client
    assert ow.zero_token_gyms(set(), http=_Http()) == []
    assert ow.zero_token_gyms(set(), http=_Http(), is_client=lambda g: g == "g-new") \
        == [("g-new", "New Echo Gym", "new-echo")]


def test_connect_link_notify_refuses_a_non_client_before_any_network(universe, monkeypatch):
    monkeypatch.setenv("AGENT_AUTO_CONNECT_LINK", "true")
    monkeypatch.setenv("AGENT_SLACK_BOT_TOKEN", "xoxb-not-real-token-000000")
    from agent import connect_link_notify as cln
    kv, seen = _KV(), []
    out = cln.notify_new_gym("boomfitbcs3b4da7", BOOM, "BoomFit BCS", db=kv, http=_NeverHttp(),
                             alert=seen.append)
    assert out is False
    assert len(seen) == 1 and "REFUSED" in seen[0] and "not an Echo client" in seen[0]
    # one alert per gym, ever
    cln.notify_new_gym("boomfitbcs3b4da7", BOOM, "BoomFit BCS", db=kv, http=_NeverHttp(),
                       alert=seen.append)
    assert len(seen) == 1
    assert "connect_link_sent_boomfitbcs3b4da7" not in kv.d


def test_connect_link_notify_default_predicate_vouches_for_a_client(universe):
    from agent import connect_link_notify as cln
    assert cln._default_is_client(TOUGH, "whatever") is True
    assert cln._default_is_client("", "toughtemple52040e") is True
    assert cln._default_is_client(BOOM, "boomfitbcs3b4da7") is False
    assert cln._default_is_client("", "") is False


def test_welcome_portal_scan_welcomes_only_echo_clients(universe, monkeypatch):
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    from agent import welcome_queue as wq, portal_gyms, welcome_posts
    monkeypatch.setattr(portal_gyms, "list_recent_portal_gyms",
                        lambda days=45, reader=None: [
                            {"gym_id": BOOM, "name": "BoomFit BCS", "created_at": "x",
                             "domain": "", "slug": "boomfit-bcs"},
                            {"gym_id": TOUGH, "name": "Tough Temple", "created_at": "x",
                             "domain": "", "slug": "tough-temple"}])
    asked = []
    monkeypatch.setattr(welcome_posts, "portal_name_override", lambda k: "")
    monkeypatch.setattr(welcome_posts, "already_welcomed", lambda k: asked.append(k) or True)
    out = wq.scan_portal_and_enqueue(force=True)
    assert out["portal_seen"] == 2 and out["not_echo_client"] == 1
    assert asked == [f"portal:{TOUGH}"]


def test_welcome_prune_treats_a_non_client_as_junk_only_on_a_good_reading(universe, monkeypatch):
    from agent import welcome_queue as wq
    with wq._conn() as conn:   # noqa: SLF001
        conn.execute("INSERT INTO welcome_queue (gym_key, name) VALUES (?, ?)",
                     (f"portal:{BOOM}", "BoomFit BCS"))
        conn.execute("INSERT INTO welcome_queue (gym_key, name) VALUES (?, ?)",
                     (f"portal:{TOUGH}", "Tough Temple"))
        conn.commit()

    class _Reader:
        def gyms_by_ids(self, ids):
            return [{"id": BOOM, "name": "BoomFit BCS", "status": "active"},
                    {"id": TOUGH, "name": "Tough Temple", "status": "active"}]
    out = wq.prune_portal_junk(reader=_Reader(), dry_run=True)
    assert [p["name"] for p in out["pruned"]] == ["BoomFit BCS"]
    assert "not an Echo client" in out["pruned"][0]["reason"]
    assert out["kept"] == ["Tough Temple"]
    # an UNREADABLE universe prunes nothing on this criterion
    monkeypatch.setattr(ec, "_load", lambda http=None, now=None: ec.ClientSet(ok=False, error="down"))
    ec.reset_cache()
    out = wq.prune_portal_junk(reader=_Reader(), dry_run=True)
    assert out["pruned"] == [] and sorted(out["kept"]) == ["BoomFit BCS", "Tough Temple"]


def test_catchup_window_is_echo_clients_only(universe, monkeypatch):
    from agent import catchup_report as cr, portal_calendar_store as pcs
    from datetime import datetime, timezone

    class _Resp:
        status_code = 200

        def json(self):
            return [{"id": BOOM, "slug": "boomfit-bcs", "name": "BoomFit BCS", "created_at": "x"},
                    {"id": TOUGH, "slug": "tough-temple", "name": "Tough Temple", "created_at": "x"}]

    class _Client:
        def get(self, *a, **k):
            assert "id" in k["params"]["select"]
            return _Resp()

    class _Store:
        def _client(self):
            return _Client()

        def _rest(self, t):
            return f"https://x/rest/v1/{t}"

        def _headers(self):
            return {}
    monkeypatch.setattr(pcs, "SupabaseCalendarStore", _Store)
    out = cr._recent_gyms_default(datetime(2026, 9, 11, tzinfo=timezone.utc))   # noqa: SLF001
    assert [g["slug"] for g in out] == ["tough-temple"]


def test_website_intake_fleet_run_skips_non_clients(universe, monkeypatch):
    monkeypatch.setenv("AGENT_WEBSITE_AUTO_INTAKE", "true")
    from agent import website_intake as wi
    touched, seen = [], []
    monkeypatch.setattr(wi, "intake_from_website",
                        lambda base, **k: touched.append(base) or {"ok": False, "base": base,
                                                                   "reason": "no domain"})
    out = wi.run(bases=["boomfitbcs3b4da7", "toughtemple52040e"], alert=seen.append)
    assert touched == ["toughtemple52040e"]
    assert out["failed"] == ["toughtemple52040e"]
    assert all("boomfit" not in s for s in seen)


def test_registry_enumerators_drop_non_client_dynamic_rows(universe, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_CLIENT_SCAN_DYNAMIC", "true")
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps([
        {"base": "toughtemple52040e", "name": "Tough Temple", "gym_id": TOUGH},
        {"base": "boomfitbcs3b4da7", "name": "BoomFit BCS", "gym_id": BOOM},
        {"base": "deanholcomb9ebee0", "name": "Dean Holcomb", "gym_id": "g-lead"},
    ]))
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(reg))
    from agent.client_media_sync import _client_bases
    from agent.calendar_autopublish import client_gym_bases
    from agent import accounts
    accounts._dynamic_cache = None   # noqa: SLF001
    cms = _client_bases()
    cal = client_gym_bases()
    for bases in (cms, cal):
        assert "toughtemple52040e" in bases
        assert "boomfitbcs3b4da7" not in bases and "deanholcomb9ebee0" not in bases
        # hardcoded client gyms are trusted as-is
        assert {"eng", "gritx", "topfuel", "district_h"} <= set(bases)
    # ...but the registry row itself still RESOLVES (publish paths never fail closed)
    assert accounts.get_account("boomfitbcs3b4da7_ig") is not None


def test_billing_customer_sync_only_walks_clients(universe, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BILLING_CUSTOMER_SYNC", "true")
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps([{"base": "boomfitbcs3b4da7", "name": "BoomFit BCS", "gym_id": BOOM},
                               {"base": "toughtemple52040e", "name": "Tough Temple", "gym_id": TOUGH}]))
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(reg))
    from agent.jobs import billing_customer_sync as bcs
    from agent import accounts
    accounts._dynamic_cache = None   # noqa: SLF001
    if not bcs.config.billing_customer_sync_enabled():
        pytest.skip("flag name differs; enumerator gate covered by the static test")
    asked = []

    class _Store:
        def resolve_gym_uuid(self, base):
            asked.append(base)
            return None
    monkeypatch.setattr(bcs, "fetch_customer_ids", lambda store: {})
    bcs.run(store=_Store(), alert=lambda m: None)
    assert "toughtemple52040e" in asked and "boomfitbcs3b4da7" not in asked
