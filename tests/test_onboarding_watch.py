"""
Onboarding readiness watch (AGENT_ONBOARDING_WATCH, default OFF).

The gap this closes: connection_watch sweeps the ACCOUNT REGISTRY, but every failure of
this class arrives as a gym MISSING from that registry, so it cannot see them. Hill
Country is connection_watch's own founding story and was absent from the registry for
weeks; CrossFit Reverb went the same way within hours of signing up on 2026-08-30.
This watch audits against the PORTAL roster instead, so a gym cannot hide by being
absent from Echo's side.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import onboarding_watch as ow  # noqa: E402


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_WATCH", "true")
    yield


class _KV:
    def __init__(self):
        self.d = {}

    def get(self, k, default=""):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def _deps(*, roster=(), intake=None, bases=(), sources=None, profiles=None,
          platforms=None, pages=None):
    sources = sources or {}
    profiles = profiles or {}
    platforms = platforms or {}
    pages = pages or {}
    return {
        "roster": lambda http=None: list(roster),
        "intake": lambda http=None: dict(intake or {}),
        "bases": lambda: list(bases),
        "approved_sources": lambda b: sources.get(b, []),
        "profile_id": lambda b: profiles.get(b, ""),
        "platforms": lambda pid: set(platforms.get(pid, set())),
        "fb_page": lambda b: pages.get(b, ""),
    }


def _healthy(base="okgym", pid="p1"):
    return _deps(roster=[("g1", base)], intake={"g1": base}, bases=[base],
                 sources={base: ["a source"]}, profiles={base: pid},
                 platforms={pid: {"instagram", "facebook"}}, pages={base: "1234"})


# ---- the flag ---------------------------------------------------------------
def test_flag_off_is_a_no_op(monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_WATCH", "false")
    seen = []
    assert ow.run(deps=_healthy(), alert=seen.append, kv=_KV()) == {}
    assert seen == []


def test_a_fully_set_up_gym_is_silent():
    seen = []
    assert ow.run(deps=_healthy(), alert=seen.append, kv=_KV()) == {}
    assert seen == []


# ---- the case connection_watch structurally cannot see ----------------------
def test_a_gym_missing_from_the_registry_is_caught():
    """CrossFit Reverb, 2026-08-30: the portal had minted its key and it had approved
    sources, but it was in NEITHER lane, so it could never post and nothing noticed.
    connection_watch iterates the registry, so this gym is invisible to it."""
    deps = _deps(roster=[("g1", "reverb")], intake={"g1": "reverb"}, bases=[],
                 sources={"reverb": ["a source"]}, profiles={"reverb": "p1"},
                 platforms={"p1": {"instagram"}}, pages={"reverb": "1"})
    seen = []
    out = ow.run(deps=deps, alert=seen.append, kv=_KV())
    assert out == {"reverb": [ow.REASON_NOT_REGISTERED]}
    assert len(seen) == 1
    assert "not set up to post" in seen[0] and "not_registered" in seen[0]
    assert "registry" in seen[0]                 # the alert names the fix


def test_a_key_mismatch_is_caught():
    """Reverb's intake forwarded under crossfitreverb6cdf33 while its portal key was
    crossfitreverb30b5b2, so its answers landed where nothing reads them."""
    deps = _deps(roster=[("g1", "reverb30b5b2")], intake={"g1": "reverb6cdf33"},
                 bases=["reverb30b5b2"], sources={"reverb30b5b2": ["s"]},
                 profiles={"reverb30b5b2": "p1"},
                 platforms={"p1": {"instagram"}}, pages={"reverb30b5b2": "1"})
    out = ow.run(deps=deps, alert=lambda m: None, kv=_KV())
    assert out == {"reverb30b5b2": [ow.REASON_KEY_MISMATCH]}


def test_zero_connected_platforms_is_caught():
    """connection_watch skips a gym with NOTHING connected by design (it only reports
    PARTIAL connections), so this state had no watcher at all."""
    deps = _deps(roster=[("g1", "g")], intake={"g1": "g"}, bases=["g"],
                 sources={"g": ["s"]}, profiles={"g": "p1"}, platforms={"p1": set()})
    out = ow.run(deps=deps, alert=lambda m: None, kv=_KV())
    assert out == {"g": [ow.REASON_NOT_CONNECTED]}


def test_facebook_connected_without_a_page_is_caught():
    """LIVE on Reverb: FB connected, no page stamped, so every Facebook publish raises
    'no Facebook page selected'."""
    deps = _deps(roster=[("g1", "g")], intake={"g1": "g"}, bases=["g"],
                 sources={"g": ["s"]}, profiles={"g": "p1"},
                 platforms={"p1": {"instagram", "facebook"}}, pages={})
    out = ow.run(deps=deps, alert=lambda m: None, kv=_KV())
    assert out == {"g": [ow.REASON_NO_FB_PAGE]}


def test_no_sources_and_no_profile_are_caught_together():
    deps = _deps(roster=[("g1", "g")], intake={"g1": "g"}, bases=["g"])
    out = ow.run(deps=deps, alert=lambda m: None, kv=_KV())
    assert out["g"] == [ow.REASON_NO_SOURCES, ow.REASON_NO_PROFILE]


# ---- alerting discipline ----------------------------------------------------
def test_one_alert_per_gym_per_issue_set_per_day():
    deps = _deps(roster=[("g1", "g")], intake={"g1": "g"}, bases=[],
                 sources={"g": ["s"]}, profiles={"g": "p1"},
                 platforms={"p1": {"instagram"}}, pages={"g": "1"})
    kv, seen = _KV(), []
    ow.run(deps=deps, alert=seen.append, kv=kv, today="2026-08-30")
    ow.run(deps=deps, alert=seen.append, kv=kv, today="2026-08-30")
    assert len(seen) == 1, "the watch storms"
    ow.run(deps=deps, alert=seen.append, kv=kv, today="2026-08-31")
    assert len(seen) == 2, "a new day should re-report an unfixed gym"


def test_one_gyms_failure_never_blocks_the_sweep():
    def _boom(_b):
        raise RuntimeError("zernio down")

    deps = _healthy(base="good")
    deps["roster"] = lambda http=None: [("g0", "bad"), ("g1", "good")]
    deps["intake"] = lambda http=None: {"g0": "bad", "g1": "good"}
    deps["bases"] = lambda: ["bad", "good"]
    inner = deps["approved_sources"]
    deps["approved_sources"] = lambda b: _boom(b) if b == "bad" else inner(b)
    seen = []
    out = ow.run(deps=deps, alert=seen.append, kv=_KV())
    assert out == {} and seen == []             # 'good' is healthy, 'bad' skipped safely


def test_an_unreadable_registry_never_reports_every_gym_unregistered():
    """If client_gym_bases cannot be read, EVERY gym would look unregistered. The sweep
    must stay silent rather than alert the whole fleet."""
    deps = _healthy()
    deps["bases"] = lambda: (_ for _ in ()).throw(RuntimeError("registry unreadable"))
    seen = []
    assert ow.run(deps=deps, alert=seen.append, kv=_KV()) == {}
    assert seen == []


# ---- LASSO is not a client gym and must never be audited as one -----------------
def test_lasso_and_staff_accounts_are_never_flagged():
    """LASSO is excluded from client_gym_bases BY DESIGN (its own lane) and grounds its
    copy in brand_voice rather than client_sources, so auditing it reported
    not_registered + no_sources every single day. Caught on the first live sweep."""
    deps = _deps(roster=[("g1", "lasso"), ("g2", "blake_personal")],
                 intake={}, bases=[])
    seen = []
    assert ow.run(deps=deps, alert=seen.append, kv=_KV()) == {}
    assert seen == []
    assert ow.is_client_gym("lasso") is False
    assert ow.is_client_gym("lasso_demo") is False
    assert ow.is_client_gym("blake_personal") is False
    assert ow.is_client_gym("eng") is True
