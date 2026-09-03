"""
connect_link_notify.py — the gap behind onboarding_watch.autoregister(): registering a
gym leaves it with ZERO platforms connected, and sending that first connect link was
ALWAYS a manual step. Verified live 2026-09-03: five gyms (CrossFit Sunnyside, CrossFit
Local, CrossFit Newtown, District H, MFLH) had a real Zernio profile and zero connection
attempts ever logged, because nobody had sent a link. Two of the five (Local, Newtown)
had already cost a manual fix once before at the REGISTRATION step -- the same gyms
hitting the same class of gap twice.

Every test here uses a fake HTTP transport; nothing touches the network.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import connect_link_notify as cln  # noqa: E402
from agent import config  # noqa: E402


# ---- fakes ------------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeHttp:
    """Scripted GET/POST fake, keyed by URL prefix. Records every call so a test can
    assert nothing was attempted past a failure point."""

    def __init__(self, routes=None):
        self.routes = routes or {}          # {url_prefix: (status, body)}
        self.calls = []

    def _match(self, url):
        for prefix, (status, body) in self.routes.items():
            if url.startswith(prefix):
                return _Resp(status, body)
        raise AssertionError(f"unscripted request: {url}")

    def get(self, url, headers=None, timeout=30):
        self.calls.append(("GET", url))
        return self._match(url)

    def post(self, url, headers=None, data=None, timeout=30):
        self.calls.append(("POST", url, json.loads(data) if data else None))
        return self._match(url)


class _KV:
    def __init__(self, initial=None):
        self.d = dict(initial or {})

    def kv_get(self, k):
        return self.d.get(k, "")

    def kv_set(self, k, v):
        self.d[k] = v


REST = "https://ooqcvmcjspeltuuhcvlh.supabase.co"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_AUTO_CONNECT_LINK", "true")
    monkeypatch.setenv("AGENT_SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SUPABASE_URL", REST)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    monkeypatch.setenv("AGENT_INTAKE_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://echo-intake-web-production.up.railway.app")
    yield


def _full_routes(email="chad@crossfitlocal.com", owner_id="U06LXAZHVBM"):
    return {
        f"{REST}/rest/v1/gym_assignments":
            (200, [{"app_user_id": "user-1"}]),
        f"{REST}/rest/v1/app_users":
            (200, [{"email": email}]),
        "https://slack.com/api/users.lookupByEmail":
            (200, {"ok": True, "user": {"id": owner_id}}),
        "https://slack.com/api/conversations.open":
            (200, {"ok": True, "channel": {"id": "C0NEWDM"}}),
        "https://slack.com/api/chat.postMessage":
            (200, {"ok": True, "ts": "1.0"}),
    }


# ---- config flag --------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_AUTO_CONNECT_LINK", raising=False)
    assert config.auto_connect_link_enabled() is False


def test_flag_reads_true(monkeypatch):
    monkeypatch.setenv("AGENT_AUTO_CONNECT_LINK", "true")
    assert config.auto_connect_link_enabled() is True


# ---- off / dedupe: zero network calls ------------------------------------------------

def test_off_makes_no_calls_at_all(monkeypatch):
    monkeypatch.setenv("AGENT_AUTO_CONNECT_LINK", "false")
    http = _FakeHttp(_full_routes())
    sent = []
    out = cln.notify_new_gym("newbox", "g1", "New Box", http=http, db=_KV(),
                             alert=sent.append)
    assert out is False
    assert http.calls == []
    assert sent == []


def test_already_sent_makes_no_calls(monkeypatch):
    http = _FakeHttp(_full_routes())
    kv = _KV({"connect_link_sent_newbox": "1"})
    out = cln.notify_new_gym("newbox", "g1", "New Box", http=http, db=kv,
                             alert=lambda m: None)
    assert out is False
    assert http.calls == []


def test_missing_base_key_or_name_is_a_noop(monkeypatch):
    http = _FakeHttp(_full_routes())
    assert cln.notify_new_gym("", "g1", "New Box", http=http, db=_KV(),
                              alert=lambda m: None) is False
    assert cln.notify_new_gym("newbox", "g1", "", http=http, db=_KV(),
                              alert=lambda m: None) is False
    assert http.calls == []


# ---- resolve_owner_email -------------------------------------------------------------

def test_resolve_owner_email_happy_path():
    http = _FakeHttp(_full_routes(email="chad@crossfitlocal.com"))
    assert cln.resolve_owner_email("g1", http=http) == "chad@crossfitlocal.com"


def test_resolve_owner_email_zero_assignments_is_none():
    http = _FakeHttp({f"{REST}/rest/v1/gym_assignments": (200, [])})
    assert cln.resolve_owner_email("g1", http=http) is None


def test_resolve_owner_email_ambiguous_owners_is_none():
    """Two different client_owner rows: never guess which one is real."""
    http = _FakeHttp({
        f"{REST}/rest/v1/gym_assignments":
            (200, [{"app_user_id": "user-1"}, {"app_user_id": "user-2"}]),
    })
    assert cln.resolve_owner_email("g1", http=http) is None


def test_resolve_owner_email_missing_app_user_is_none():
    http = _FakeHttp({
        f"{REST}/rest/v1/gym_assignments": (200, [{"app_user_id": "user-1"}]),
        f"{REST}/rest/v1/app_users": (200, []),
    })
    assert cln.resolve_owner_email("g1", http=http) is None


def test_resolve_owner_email_read_failure_is_none():
    http = _FakeHttp({f"{REST}/rest/v1/gym_assignments": (500, {"error": "boom"})})
    assert cln.resolve_owner_email("g1", http=http) is None


def test_resolve_owner_email_no_creds_is_none(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")
    assert cln.resolve_owner_email("g1", http=_FakeHttp()) is None


# ---- full happy path -------------------------------------------------------------

def test_happy_path_sends_and_dedupes():
    http = _FakeHttp(_full_routes(email="chad@crossfitlocal.com", owner_id="U06LXAZHVBM"))
    kv = _KV()
    sent = []
    out = cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                             http=http, db=kv, alert=sent.append)
    assert out is True
    assert sent == [], "a clean send must not also escalate"
    assert kv.d["connect_link_sent_crossfitlocal"] == "1"

    method, url, payload = [c for c in http.calls if c[0] == "POST"
                            and "chat.postMessage" in c[1]][0]
    assert payload["channel"] == "C0NEWDM"
    assert "CrossFit Local" in payload["text"]
    assert "/portal/" in payload["text"] and "/connect" in payload["text"]


def test_happy_path_opens_dm_with_approver_and_owner():
    http = _FakeHttp(_full_routes(owner_id="U06LXAZHVBM"))
    cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                       http=http, db=_KV(), alert=lambda m: None)
    method, url, payload = [c for c in http.calls if c[0] == "POST"
                            and "conversations.open" in c[1]][0]
    users = set(payload["users"].split(","))
    assert users == {config.APPROVER_SLACK_ID, "U06LXAZHVBM"}


def test_second_call_after_success_is_deduped():
    http = _FakeHttp(_full_routes())
    kv = _KV()
    assert cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                              http=http, db=kv, alert=lambda m: None) is True
    n_calls_after_first = len(http.calls)
    assert cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                              http=http, db=kv, alert=lambda m: None) is False
    assert len(http.calls) == n_calls_after_first, "no new network calls on the resend"


# ---- every failure path escalates, none send silently --------------------------------

def test_no_bot_token_escalates(monkeypatch):
    monkeypatch.delenv("AGENT_SLACK_BOT_TOKEN", raising=False)
    http = _FakeHttp(_full_routes())
    sent = []
    out = cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                             http=http, db=_KV(), alert=sent.append)
    assert out is False
    assert http.calls == []
    assert "no Slack bot token" in sent[0]
    assert "intake-link --account crossfitlocal" in sent[0]


def test_no_owner_found_escalates_and_stops():
    http = _FakeHttp({f"{REST}/rest/v1/gym_assignments": (200, [])})
    sent = []
    out = cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                             http=http, db=_KV(), alert=sent.append)
    assert out is False
    assert "no single client_owner email" in sent[0]
    assert not any("slack.com" in c[1] for c in http.calls), \
        "must not attempt Slack at all without a resolved owner"


def test_no_slack_match_escalates_with_the_email():
    http = _FakeHttp({
        **_full_routes(),
        "https://slack.com/api/users.lookupByEmail":
            (200, {"ok": False, "error": "users_not_found"}),
    })
    sent = []
    cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                       http=http, db=_KV(), alert=sent.append)
    assert "no matching Slack account" in sent[0]
    assert "chad@crossfitlocal.com" in sent[0]
    assert not any("conversations.open" in c[1] for c in http.calls
                  if c[0] == "POST"), "must not open a DM without a resolved Slack id"


def test_link_mint_failure_escalates(monkeypatch):
    monkeypatch.delenv("AGENT_INTAKE_SIGNING_SECRET", raising=False)
    http = _FakeHttp(_full_routes())
    sent = []
    cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                       http=http, db=_KV(), alert=sent.append)
    assert "could not mint a connect link" in sent[0]
    assert not any("conversations.open" in c[1] for c in http.calls if c[0] == "POST")


def test_dm_open_failure_escalates_with_the_link_so_a_human_can_send_it():
    http = _FakeHttp({
        **_full_routes(),
        "https://slack.com/api/conversations.open": (200, {"ok": False, "error": "boom"}),
    })
    sent = []
    cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                       http=http, db=_KV(), alert=sent.append)
    assert "could not open a Slack DM" in sent[0]
    assert "/connect" in sent[0], "the link itself must ride along in the escalation"


def test_send_failure_escalates_with_the_link():
    http = _FakeHttp({
        **_full_routes(),
        "https://slack.com/api/chat.postMessage": (200, {"ok": False, "error": "boom"}),
    })
    sent = []
    cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                       http=http, db=_KV(), alert=sent.append)
    assert "failed to send" in sent[0]
    assert "/connect" in sent[0]


def test_a_dedupe_read_failure_still_lets_the_first_send_through():
    class _BrokenKvGet(_KV):
        def kv_get(self, k):
            raise RuntimeError("kv down")
    http = _FakeHttp(_full_routes())
    out = cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                             http=http, db=_BrokenKvGet(), alert=lambda m: None)
    assert out is True


def test_a_dedupe_write_failure_does_not_undo_the_send():
    class _BrokenKvSet(_KV):
        def kv_set(self, k, v):
            raise RuntimeError("kv down")
    http = _FakeHttp(_full_routes())
    out = cln.notify_new_gym("crossfitlocal", "g1", "CrossFit Local",
                             http=http, db=_BrokenKvSet(), alert=lambda m: None)
    assert out is True, "the message already sent; a stamp failure must not report failure"


# ---- none of the escalation lines are ever classified NOISE --------------------------

def test_every_escalation_stays_needs_triage():
    from agent import ops_triage as ot
    lines = [
        "auto connect-link for crossfitlocal: no Slack bot token configured; cannot "
        "send. Send the connect link by hand (python -m agent intake-link --account "
        "crossfitlocal).",
        "auto connect-link for crossfitlocal: no single client_owner email found in "
        "the portal's own records (zero or more than one on file). Send the connect "
        "link by hand (python -m agent intake-link --account crossfitlocal) and add "
        "the owner's contact so this sends on its own next time.",
        "auto connect-link for crossfitlocal: owner email chad@crossfitlocal.com has "
        "no matching Slack account. Send the connect link by hand (python -m agent "
        "intake-link --account crossfitlocal).",
        "auto connect-link for crossfitlocal: could not mint a connect link "
        "(AGENT_INTAKE_SIGNING_SECRET may be unset on this service). Send it by hand "
        "once the secret is set.",
    ]
    for line in lines:
        assert ot.classify(line) == ot.NEEDS_TRIAGE, line[:60]


# ---- wired into autoregister -----------------------------------------------------

def test_autoregister_calls_notify_after_a_successful_registration(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ONBOARDING_AUTOREGISTER", "true")
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts, onboarding_watch as ow
    accounts._dynamic_cache = None
    calls = []
    monkeypatch.setattr(cln, "notify_new_gym",
                        lambda base, gid, name, **kw: calls.append((base, gid, name)))
    d = {"gym_name": lambda gid: "New Box"}
    ow.autoregister("newbox", "g1", deps=d, alert=lambda m: None)
    assert calls == [("newbox", "g1", "New Box")]
    accounts._dynamic_cache = None


def test_notify_crash_does_not_undo_a_successful_registration(monkeypatch, tmp_path):
    """The registration already happened; a bug in the notify path must not make
    autoregister report failure or roll anything back."""
    monkeypatch.setenv("AGENT_ONBOARDING_AUTOREGISTER", "true")
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts, onboarding_watch as ow
    accounts._dynamic_cache = None

    def _boom(base, gid, name, **kw):
        raise RuntimeError("notify blew up")
    monkeypatch.setattr(cln, "notify_new_gym", _boom)
    d = {"gym_name": lambda gid: "New Box"}
    sent = []
    result = ow.autoregister("newbox", "g1", deps=d, alert=sent.append)
    assert result is True
    assert accounts.get_account("newbox_ig") is not None
    assert any("registered into Echo's account registry" in m for m in sent)
    assert any("crashed" in m for m in sent)
    accounts._dynamic_cache = None
