"""
THE ECHO CLIENT UNIVERSE GUARD (2026-09-11 incident, D73).

onboarding_watch called echo_intake_tokens "the AUTHORITATIVE list of Echo clients".
The portal mints that token for EVERY gym, so the onboarding lane swept the whole LASSO
ads fleet: ~110 non-clients auto-registered, 36 owners DMed a connect link "from Echo",
131 websites scraped, 153 ops tickets. The rule now:

    echo_gym_settings IS the Echo client universe.  echo_intake_tokens IS NOT.
    (markers: echo_gym_settings | echo_social_intake | gym_products social | plan
     echo_standalone -- see agent/echo_clients.py)

Part 1 is STATIC: any module that enumerates echo_intake_tokens, the portal `gyms`
table, or the account registry must CALL the gate (not merely import it), or sit on a
documented allowlist of single-gym / eq.-filtered readers. Part 2 is BEHAVIOURAL: each
fleet lane and each registration door, handed a non-client, produces ZERO alerts,
ZERO outbound messages and ZERO writes for that gym. Every behavioural test switches
OFF the suite-wide allow-all override.
"""
import ast
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import echo_clients as ec  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent")

# The gate's call surface. A lane must CALL one of these; importing is not gating.
GATE_CALLS = {"is_echo_client", "only_client_bases", "is_client_base", "only_clients",
              "echo_client_keys", "echo_client_gym_ids"}
GATE_SNAPSHOT = "snapshot"     # echo_clients.snapshot(...).is_client(...) also counts

# Every fleet lane / registration door the incident touched (or could have).
LANES = (
    "onboarding_watch.py",          # roster, autoregister, zero-token sweep
    "connect_link_notify.py",       # the DM
    "welcome_queue.py",             # portal welcome scan + prune
    "portal_gyms.py",               # the gyms-table enumerator behind the welcome scan
    "catchup_report.py",            # "gyms signed up in the last 60 days"
    "website_intake.py",            # fleet website scrape
    "client_media_sync.py",         # _client_bases: THE registry enumerator
    "calendar_autopublish.py",      # client_gym_bases: the other one
    os.path.join("jobs", "billing_customer_sync.py"),
    "accounts.py",                  # register_gym: THE place registry rows are created
    "onboard.py",                   # onboard.run: behind POST /portal/onboard + the CLI
)

# Modules that READ echo_intake_tokens for a gym that is ALREADY identified (key
# resolution, split-key repair, one gym's token row) -- not to enumerate gyms for a
# client-facing action. Each is a deliberate, reviewed exception. Adding a module here
# is a decision, not a fix: if it iterates the table to act on gyms, gate it instead.
TOKEN_READ_ALLOWLIST = {
    "echo_clients.py",              # the predicate itself (reads it for ALIASES only)
    "account_key_resolve.py",       # stale key -> live key, by gym_id (fails closed)
    "account_key_reconcile.py",     # operator repair of one gym's key
    "account_key_split_watch.py",   # detects a gym whose two keys disagree
    "gym_identity.py",              # name/market tokens for ONE base's grounding
    "intake_web.py",                # the row for ONE signed link's key
    "social_intake_reader.py",      # resolve ONE intake's raw key to its token key
    os.path.join("slack_convo", "listener_wiring.py"),   # ONE Slack user's gym -> its key
    "__main__.py",                  # onboarding-audit PRINTS the table name; its roster is
                                    # onboarding_watch.portal_keys, which is gated
}

# Modules that read the portal `gyms` table WITHOUT enumerating it for a client-facing
# action: eq.-filtered single-gym reads, or key-repair tooling. Everything else that
# touches the table must call the gate.
GYMS_READ_ALLOWLIST = {
    "echo_clients.py",              # reads it to build the universe
    "account_key_resolve.py",       # id,name for key derivation (fails closed)
    "account_key_reconcile.py",     # operator repair plan
    "account_key_split_watch.py",   # split detection
    "gym_identity.py",              # id=eq.<gym_id> single reads
    "support_inbox.py",             # id=eq.<gym_uuid> single read (_shared_gym_name)
    "portal_calendar_store.py",     # slug=eq./id=eq. resolvers + settings writers
    "reply_engine_watch.py",        # reply-lane audit; gyms read supplies NAMES only
    "account_key_doctor.py",        # _reads_ok: a limit=1 readability probe, no rows used
    os.path.join("slack_convo", "outbox.py"),           # id=eq.<gym> single read (a name)
    os.path.join("slack_convo", "listener_wiring.py"),  # id=eq.<gid> single read
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


def _tree(path):
    return ast.parse(_source(path))


def _code_strings(tree):
    """Every string constant in CODE (docstrings skipped: an Expr whose value is a
    Constant is documentation, and a module is allowed to talk about a table)."""
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            doc_ids.add(id(node.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc_ids:
            yield node.value


def _call_names(tree):
    """Every function name called in the module (bare or attribute)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name:
                out.add(name)
    return out


def _gate_refs(tree):
    """Attribute references to the gate (echo_clients.is_echo_client) that are BOUND
    rather than called directly -- the `is_client = deps.get(...) or
    echo_clients.is_echo_client` injection pattern -- and then invoked via the alias."""
    refs = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in GATE_CALLS \
                and isinstance(n.value, ast.Name) and n.value.id == "echo_clients":
            refs.add(n.attr)
    return refs


def _calls_gate(tree):
    calls = _call_names(tree)
    if calls & GATE_CALLS:
        return True
    if GATE_SNAPSHOT in calls and "is_client" in calls:
        return True
    # bound-then-called: the gate is injected as a dependency and invoked as is_client(...)
    return bool(_gate_refs(tree)) and "is_client" in calls


def _reads_token_table(tree):
    return any("echo_intake_tokens" in s for s in _code_strings(tree))


# The REST helper names this repo hands a table name to. "gyms" as an argument to
# anything else (a dict .get, an argparse positional, a summary key) is not a read.
REST_HELPERS = {"_rest", "_get", "_page", "_read_all", "_supabase_get", "_rest_get",
                "_select", "_fetch", "_table", "get_rows", "_rows"}


def _reads_gyms_table(tree, src):
    """A REST read of the portal `gyms` table: a code string ending '/rest/v1/gyms', a
    REST helper handed "gyms" as the table, or a module-level _TABLE = "gyms"."""
    for s in _code_strings(tree):
        if s.endswith("/rest/v1/gyms"):
            return True
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        if name not in REST_HELPERS:
            continue
        for a in n.args:
            if isinstance(a, ast.Constant) and a.value == "gyms":
                return True
    return '_TABLE = "gyms"' in src


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
def test_every_fleet_lane_calls_the_predicate(lane):
    path = os.path.join(ROOT, lane)
    src = _source(path)
    assert "echo_clients" in src, f"{lane} enumerates gyms without echo_clients"
    assert _calls_gate(_tree(path)), (
        f"{lane} imports echo_clients but never CALLS is_echo_client / only_client_bases "
        "/ is_client_base (an import is not a gate)")


def _check(readers, allowlist, what, fix):
    offenders, ungated, seen_allow = [], [], set()
    for path in _py_files():
        tree = _tree(path)
        src = _source(path)
        if not readers(tree, src):
            continue
        rel = _rel(path)
        if rel in allowlist:
            seen_allow.add(rel)
            continue
        if "echo_clients" not in src:
            offenders.append(rel)
        elif not _calls_gate(tree):
            ungated.append(rel)
    assert not offenders, f"{offenders} {what} without agent.echo_clients. {fix}"
    assert not ungated, f"{ungated} {what} and import echo_clients but never call the gate."
    return seen_allow


def test_no_module_reads_echo_intake_tokens_without_the_gate_or_a_ruling():
    seen = _check(lambda t, s: _reads_token_table(t), TOKEN_READ_ALLOWLIST,
                  "read echo_intake_tokens",
                  "That table is NOT the Echo client list (D73). Gate the enumeration with "
                  "echo_clients.is_echo_client, or add a reviewed TOKEN_READ_ALLOWLIST entry "
                  "with the reason it resolves ONE gym rather than acting on many.")
    stale = TOKEN_READ_ALLOWLIST - seen
    assert not stale, f"allowlist entries no longer read the table; remove them: {stale}"


def test_no_module_enumerates_the_portal_gyms_table_without_the_gate_or_a_ruling():
    seen = _check(_reads_gyms_table, GYMS_READ_ALLOWLIST,
                  "read the portal gyms table",
                  "The gyms table is the whole LASSO ads fleet (D73). A read that is not "
                  "id=eq./slug=eq. for ONE gym must filter through echo_clients.is_echo_client, "
                  "or sit on GYMS_READ_ALLOWLIST with the reason.")
    stale = GYMS_READ_ALLOWLIST - seen
    assert not stale, f"allowlist entries no longer read the gyms table: {stale}"


def test_allowlisted_gyms_readers_really_are_single_gym_or_repair_tools():
    """The allowlist's claim, checked: every allowlisted gyms reader that is not a key
    tool filters its gyms reads with an eq. predicate somewhere in the module. A module
    that later adds an unfiltered enumeration loses its exemption."""
    single = {"gym_identity.py", "support_inbox.py", "portal_calendar_store.py",
              os.path.join("slack_convo", "outbox.py"),
              os.path.join("slack_convo", "listener_wiring.py")}
    for rel in single:
        src = _source(os.path.join(ROOT, rel))
        assert 'f"eq.{' in src or "'eq.'" in src or '"eq.' in src, rel


def test_no_module_iterates_the_account_registry_without_the_gate_or_a_ruling():
    seen = _check(lambda t, s: _iterates_all_accounts(t), REGISTRY_ITER_ALLOWLIST,
                  "iterate all_accounts()",
                  "The dynamic registry is not the Echo client list (autoregister filled it "
                  "from the token table). Filter with echo_clients.only_client_bases, or add a "
                  "reviewed REGISTRY_ITER_ALLOWLIST entry with the reason.")
    stale = REGISTRY_ITER_ALLOWLIST - seen - {"accounts.py", "echo_clients.py"}
    assert not stale, f"allowlist entries no longer iterate the registry: {stale}"


def test_every_register_gym_caller_is_a_known_door():
    """register_gym is gated inside; the ONLY callers allowed to pass own_submission=True
    are the two intake-driven doors, where `base` is the submitting gym's own key."""
    own = {}
    for path in _py_files():
        rel = _rel(path)
        if rel == "accounts.py":
            continue
        for n in ast.walk(_tree(path)):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "register_gym":
                kws = {k.arg: k.value for k in n.keywords}
                v = kws.get("own_submission")
                own[rel] = bool(isinstance(v, ast.Constant) and v.value is True)
    assert own == {"intake_ingest.py": True,
                   "social_intake_reader.py": True,
                   "onboarding_watch.py": False}, own


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


def test_the_markers_are_named_in_the_predicate_module():
    doc = ec.__doc__
    for marker in ("echo_gym_settings", "echo_social_intake", "gym_products",
                   "echo_standalone", "NOT MARKERS", "echo_intake_tokens", "gym_billing"):
        assert marker in doc, marker
    assert ec.MARKERS == (ec.MARKER_SETTINGS, ec.MARKER_INTAKE, ec.MARKER_PRODUCT, ec.MARKER_PLAN)


def test_the_suite_override_is_installed_by_conftest():
    """Documented dependency: the lane tests written before the gate rely on the
    conftest allow-all override. If someone removes it, hundreds of tests fail
    closed for a reason this assertion names."""
    assert ec.is_echo_client("definitely-not-a-gym") is True


# ---- Part 2: behavioural ----------------------------------------------------------------

TOUGH = "0f0f0f0f-0000-4000-8000-000000000001"
NEWBIE = "0f0f0f0f-0000-4000-8000-000000000002"    # day-one client: intake row, not connected
BOOM = "0f0f0f0f-0000-4000-8000-000000000003"
UNIVERSE = ec.build(
    [{"gym_id": TOUGH}],
    [{"gym_id": TOUGH, "echo_account_key": "toughtemple52040e"},
     {"gym_id": NEWBIE, "echo_account_key": "newboxfitness0f0f0f"},
     {"gym_id": BOOM, "echo_account_key": "boomfitbcs3b4da7"}],
    [{"id": TOUGH, "name": "Tough Temple", "slug": "tough-temple"},
     {"id": NEWBIE, "name": "New Box Fitness", "slug": "new-box-fitness"},
     {"id": BOOM, "name": "BoomFit BCS", "slug": "boomfit-bcs"}],
    intake_rows=[{"gym_id": NEWBIE, "client_key": NEWBIE,
                  "echo_account_key": "newboxfitness0f0f0f"}])


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
        "gym_name": lambda gid: {TOUGH: "Tough Temple", BOOM: "BoomFit BCS",
                                 NEWBIE: "New Box Fitness"}.get(gid, ""),
        "zero_token": lambda known, http=None: list(zero_token),
        "is_client": ec.is_echo_client,
    }


def test_onboarding_watch_alerts_only_on_echo_clients(universe, monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_WATCH", "true")
    from agent import onboarding_watch as ow
    seen = []
    out = ow.run(deps=_ow_deps([(BOOM, "boomfitbcs3b4da7"), (TOUGH, "toughtemple52040e"),
                                (NEWBIE, "newboxfitness0f0f0f")],
                               zero_token=[("g-lead", "Dean Holcomb", "dean-holcomb")]),
                 alert=seen.append, kv=_KV())
    # the connected client AND the day-one (intake-only) client are both watched
    assert set(out) == {"toughtemple52040e", "newboxfitness0f0f0f"}
    assert len(seen) == 2
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
    # the DAY-ONE client (intake row, never connected) still registers + gets its link:
    # this is the bootstrap the settings-only rule deadlocked.
    assert ow.autoregister("newboxfitness0f0f0f", NEWBIE, deps=deps, alert=seen.append) is True
    assert [r["base"] for r in json.load(open(tmp_path / "reg.json"))] == ["newboxfitness0f0f0f"]
    assert len(sent) == 1 and sent[0][0] == "newboxfitness0f0f0f"


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
    assert cln._default_is_client(NEWBIE, "") is True          # the day-one client
    assert cln._default_is_client("", "toughtemple52040e") is True
    assert cln._default_is_client(BOOM, "boomfitbcs3b4da7") is False
    assert cln._default_is_client("", "") is False


def test_portal_gyms_reader_hands_out_only_echo_clients(universe, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    from agent import portal_gyms

    class _Resp:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

    rows = [{"id": BOOM, "name": "BoomFit BCS", "slug": "boomfit-bcs", "created_at": "x",
             "status": "active"},
            {"id": NEWBIE, "name": "New Box Fitness", "slug": "new-box-fitness",
             "created_at": "x", "status": "active"}]

    class _Http:
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/gyms"):
                return _Resp(rows[:1] if params.get("limit") == "1" else rows)
            return _Resp([])
    out = portal_gyms.PortalGymsReader(http=_Http()).list_recent_portal_gyms()
    assert [g["name"] for g in out] == ["New Box Fitness"]


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


# ---- registration doors (round 2) -------------------------------------------------------

def test_register_gym_refuses_a_non_client_with_one_alert(universe, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts, ops_alerts
    seen = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: seen.append(m))
    assert accounts.register_gym("boomfitbcs3b4da7", name="BoomFit BCS", gym_id=BOOM,
                                 door="test") == []
    assert accounts.register_gym("boomfitbcs3b4da7", name="BoomFit BCS", gym_id=BOOM) == []
    assert not (tmp_path / "reg.json").exists()
    assert len(seen) == 1 and "REFUSED" in seen[0] and "via test" in seen[0]
    # clients register by gym_id, by key, and the day-one intake client too
    assert accounts.register_gym("toughtemple086f51", name="Tough Temple", gym_id=TOUGH)
    assert accounts.register_gym("toughtemple52040e", name="Tough Temple")
    assert accounts.register_gym("newboxfitness0f0f0f", name="New Box", gym_id=NEWBIE)


def test_register_gym_own_submission_is_the_only_exemption(universe, monkeypatch, tmp_path):
    """The gym's own intake may register it before its marker is readable; the caller
    asserts the base IS the submitting gym's key. Nothing else bypasses the gate."""
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    assert accounts.register_gym("freshbox", name="Fresh Box", own_submission=True) \
        == ["freshbox_ig", "freshbox_fb"]
    assert accounts.register_gym("otherbox", name="Other Box") == []


def test_onboard_run_refuses_a_non_client_before_writing_anything(universe, monkeypatch, tmp_path):
    from agent import onboard, db
    from agent import account_key_mint
    monkeypatch.setattr(account_key_mint, "derive_mint_key",
                        lambda k, n, **kw: (k, {"derived": False, "reason": "test", "gym_uuid": BOOM}))
    with pytest.raises(onboard.OnboardRefused):
        onboard.run("boomfitbcs3b4da7", "BoomFit BCS", voice_dir=str(tmp_path / "v"),
                    brains_dir=str(tmp_path / "b"))
    assert db.gym_get("boomfitbcs3b4da7", _shared_read=False) is None
    assert not (tmp_path / "v").exists()
    # --force is the by-hand override; a client passes without it
    r = onboard.run("boomfitbcs3b4da7", "BoomFit BCS", voice_dir=str(tmp_path / "v"),
                    brains_dir=str(tmp_path / "b"), force=True)
    assert r["account_key"] == "boomfitbcs3b4da7"
    monkeypatch.setattr(account_key_mint, "derive_mint_key",
                        lambda k, n, **kw: (k, {"derived": False, "reason": "test", "gym_uuid": NEWBIE}))
    r = onboard.run("newboxfitness0f0f0f", "New Box Fitness", voice_dir=str(tmp_path / "v2"),
                    brains_dir=str(tmp_path / "b2"))
    assert r["account_key"] == "newboxfitness0f0f0f"


def test_portal_onboard_route_stops_the_mint_on_a_refusal(universe, monkeypatch):
    """POST /portal/onboard -> handle_portal_onboard -> onboard.run. The raise must reach
    the handler's generic failure path: no token in the response."""
    monkeypatch.setenv("AGENT_INTAKE_SIGNING_SECRET", "s" * 32)
    from agent import intake_web, account_key_mint
    monkeypatch.setattr(account_key_mint, "derive_mint_key",
                        lambda k, n, **kw: (k, {"derived": False, "reason": "test", "gym_uuid": BOOM}))
    status, resp = intake_web.handle_portal_onboard({"account_key": "boomfitbcs3b4da7",
                                                     "display_name": "BoomFit BCS"})
    assert status == 500 and "raw_token" not in resp
