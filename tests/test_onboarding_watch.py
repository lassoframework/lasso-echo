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
          platforms=None, pages=None, names=None, voice=None):
    sources = sources or {}
    profiles = profiles or {}
    platforms = platforms or {}
    pages = pages or {}
    return {
        "roster": lambda http=None: list(roster),
        "intake": lambda http=None: dict(intake or {}),
        "bases": lambda: list(bases),
        "approved_sources": lambda b: sources.get(b, []),
        # voice=None means "every gym has a real bible", so every pre-existing case
        # here keeps testing exactly what it was written to test.
        "voice": lambda b: True if voice is None else bool(voice.get(b)),
        "profile_id": lambda b: profiles.get(b, ""),
        "platforms": lambda pid: set(platforms.get(pid, set())),
        "fb_page": lambda b: pages.get(b, ""),
        "gym_name": lambda gid: (names or {}).get(gid, ""),
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


def test_a_key_mismatch_is_caught_while_it_is_still_stranding_the_answers():
    """Reverb's intake forwarded under crossfitreverb6cdf33 while its portal key was
    crossfitreverb30b5b2, so its answers landed where nothing reads them."""
    deps = _deps(roster=[("g1", "reverb30b5b2")], intake={"g1": "reverb6cdf33"},
                 bases=["reverb30b5b2"], sources={},
                 profiles={"reverb30b5b2": "p1"},
                 platforms={"p1": {"instagram"}}, pages={"reverb30b5b2": "1"})
    out = ow.run(deps=deps, alert=lambda m: None, kv=_KV())
    assert out == {"reverb30b5b2": [ow.REASON_KEY_MISMATCH, ow.REASON_NO_SOURCES]}


def test_a_repaired_key_mismatch_is_silent():
    """Once the sources are migrated onto the portal key the gym is healthy, but the
    echo_social_intake row records the ORIGINAL key forever. Measured on the first full
    sweep 2026-08-30: Pierce, Reverb, Hill Country and The Bolton Club all flagged
    key_mismatch with 17, 6, 22 and 3 sources respectively, all publishing normally.
    With alerts armed that is a daily page about four healthy gyms."""
    deps = _deps(roster=[("g1", "reverb30b5b2")], intake={"g1": "reverb6cdf33"},
                 bases=["reverb30b5b2"], sources={"reverb30b5b2": ["s"]},
                 profiles={"reverb30b5b2": "p1"},
                 platforms={"p1": {"instagram"}}, pages={"reverb30b5b2": "1"})
    seen = []
    assert ow.run(deps=deps, alert=seen.append, kv=_KV()) == {}
    assert seen == []


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


# ---- the bible that was never written (the expensive silent failure) ------------
_BIBLE_COMMAND = "python -m agent social-intake-sync --base"


def _no_bible(base="crossfitlocal", pid="p1"):
    """Sources approved, Zernio profile connected, FB page stamped: every other check
    in this file passes. The ONLY thing wrong is that no bible was ever produced."""
    return _deps(roster=[("g1", base)], intake={"g1": base}, bases=[base],
                 sources={base: ["a source"]}, profiles={base: pid},
                 platforms={pid: {"instagram", "facebook"}}, pages={base: "1234"},
                 voice={base: False})


def test_a_gym_with_no_brand_bible_is_caught():
    """THE most expensive silent failure in the system, and this watch could not see it:
    check_gym never read the voice-doc path at all, so a gym with approved sources, a
    connected profile and ZERO bible read as perfectly healthy while it silently never
    drafted a single post, forever. ENG went this way; crossfitlocal, hillcountry and
    theboltonclub all went the same way the week of 2026-08-31. The only nearby signal
    (client_media_sync's _alert_stall no_voice) fires once ever, carries no next
    command, and is unreachable for exactly these gyms."""
    seen = []
    out = ow.run(deps=_no_bible(), alert=seen.append, kv=_KV())
    assert out == {"crossfitlocal": [ow.REASON_NO_VOICE]}
    assert len(seen) == 1
    assert seen[0].startswith("crossfitlocal:")            # the alert names the gym
    # and it names the EXACT next command, under this gym's own key, so the operator
    # copies it instead of retyping it under the wrong one.
    assert f"{_BIBLE_COMMAND} crossfitlocal" in seen[0]


def test_a_gym_with_a_real_bible_is_never_flagged():
    """No false positives: the fleet's healthy gyms must stay silent, or the watch
    trains everyone to ignore it (the key_mismatch lesson, 2026-08-30)."""
    seen = []
    deps = _healthy(base="eng")
    deps["voice"] = lambda b: True
    assert ow.run(deps=deps, alert=seen.append, kv=_KV()) == {}
    assert seen == []


def test_no_sources_still_wins_over_no_voice():
    """A gym with no approved sources CANNOT have a bible: the bible is written FROM the
    intake answers. So no_sources is the step that unblocks the bible, it leads the
    alert, and no_voice is not piled on top as derived noise."""
    deps = _deps(roster=[("g1", "g")], intake={"g1": "g"}, bases=["g"],
                 sources={}, profiles={"g": "p1"},
                 platforms={"p1": {"instagram"}}, pages={"g": "1"},
                 voice={"g": False})
    seen = []
    out = ow.run(deps=deps, alert=seen.append, kv=_KV())
    assert out["g"][0] == ow.REASON_NO_SOURCES, "no_sources must lead"
    assert ow.REASON_NO_VOICE not in out["g"]
    assert _BIBLE_COMMAND not in seen[0]     # the alert leads with the sources fix


def test_no_voice_is_reported_before_no_profile():
    """Ordering matters because no_profile RETURNS EARLY. If the profile check ran
    first, a gym missing both would report only no_profile, and the day someone linked
    the profile it would go straight back to reading healthy while still drafting
    nothing. That is the exact invisibility this check exists to end."""
    deps = _deps(roster=[("g1", "g")], intake={"g1": "g"}, bases=["g"],
                 sources={"g": ["s"]}, profiles={}, voice={"g": False})
    out = ow.run(deps=deps, alert=lambda m: None, kv=_KV())
    assert out["g"] == [ow.REASON_NO_VOICE, ow.REASON_NO_PROFILE]


def test_the_bible_fix_names_a_real_command():
    assert ow.REASON_NO_VOICE in ow._FIX
    assert ow.REASON_NO_VOICE in ow.REASONS
    assert _BIBLE_COMMAND in ow._FIX[ow.REASON_NO_VOICE]
    assert ow._fix_for(ow.REASON_NO_VOICE, "hillcountry").endswith(
        f"{_BIBLE_COMMAND} hillcountry")


# ---- a bible that is nothing but TODOs is the same as no bible ------------------
def test_the_add_client_scaffold_counts_as_no_bible():
    """onboard.VOICE_TEMPLATE writes a fully-TODO doc and NO writer ever clobbers an
    existing file, so an unfilled scaffold is PERMANENT. It is non-empty, so load_voice
    hands back a VoiceDoc and preflight passes it, while it carries no avatar, no
    pillars, no CTAs and no hashtags. Reading it as a real bible is how a gym stays
    silently dead while every check says ready."""
    from agent.onboard import VOICE_TEMPLATE
    assert ow.bible_is_hollow(VOICE_TEMPLATE.format(name="Hill Country Fitness")) is True
    assert ow.bible_is_hollow("") is False      # empty is load_voice's job, not this


def test_a_half_filled_bible_is_never_called_hollow():
    """A human's work in progress must not page anyone: ONE real body line is enough."""
    raw = ("# Hill Country Brand Bible\n\n"
           "## 1. Who they are\n"
           "A neighborhood gym for busy parents in Dripping Springs.\n\n"
           "## 2. Voice and tone\n"
           "TODO: words they say, words they never say.\n")
    assert ow.bible_is_hollow(raw) is False


def test_a_wrapped_todo_paragraph_does_not_look_like_content():
    """VOICE_TEMPLATE's guardrails TODO runs three lines; only the first starts with
    TODO. Counting the continuations as real content would make every scaffold look
    filled, which is the whole bug."""
    raw = ("## 4. Hard guardrails\n"
           "TODO: client specific guardrails. House rules always apply: human approval\n"
           "on every post, no invented facts or stats, no em dashes in published copy.\n")
    assert ow.bible_is_hollow(raw) is True


# ---- the live reader resolves the path the DRAFTER uses, not a made-up one -------
def test_the_live_voice_reader_uses_the_durable_path(monkeypatch, tmp_path):
    """PIN THE PATH. Production writes the bible to
    <DATA_DIR>/brand_voice/<base>/lasso_voice.md (social_intake_reader), and the
    drafter + preflight both resolve it through
    client_media_sync._resolve_client_voice_path. onboard_verify checks
    'brand_voice/<key>.md' instead, which nothing ever writes; copying that mistake
    here would have this watch report the whole fleet wrong in one direction or the
    other."""
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(tmp_path / "brand_voice"))
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    voice = ow._live_deps()["voice"]
    assert voice("theboltonclub") is False              # nothing written yet

    from agent.onboard import VOICE_TEMPLATE
    doc = tmp_path / "brand_voice" / "theboltonclub" / "lasso_voice.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(VOICE_TEMPLATE.format(name="The Bolton Club"), encoding="utf-8")
    assert voice("theboltonclub") is False, "an all-TODO scaffold is not a bible"

    doc.write_text("# The Bolton Club Brand Bible\n\n## 1. Who they are\n"
                   "A strength and conditioning gym in Bolton.\n", encoding="utf-8")
    assert voice("theboltonclub") is True


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


# ---- auto-register: act on not_registered instead of asking a human -------------
def _unregistered(names=None):
    return _deps(roster=[("g1", "newbox")], intake={"g1": "newbox"}, bases=[],
                 sources={"newbox": ["s"]}, profiles={"newbox": "p1"},
                 platforms={"p1": {"instagram"}}, pages={"newbox": "1"},
                 names=({"g1": "New Box Fitness"} if names is None else names))


def test_autoregister_defaults_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_ONBOARDING_AUTOREGISTER", raising=False)
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    out = ow.run(deps=_unregistered(), alert=lambda m: None, kv=_KV())
    assert out == {"newbox": [ow.REASON_NOT_REGISTERED]}, "it acted while OFF"
    assert accounts.get_account("newbox_ig") is None
    accounts._dynamic_cache = None


def test_autoregister_puts_a_portal_known_gym_into_the_lane(monkeypatch, tmp_path):
    """register_gym has ONE production caller, the social-intake sweep, so a gym that
    has not submitted intake yet is in NEITHER lane and nothing ever puts it there.
    That hand step was paid five times (Hill Country, Bolton, Local, Reverb, Newtown)
    and does not survive 100 gyms."""
    monkeypatch.setenv("AGENT_ONBOARDING_AUTOREGISTER", "true")
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    seen = []
    out = ow.run(deps=_unregistered(), alert=seen.append, kv=_KV())
    assert accounts.get_account("newbox_ig") is not None
    assert out == {}, "the gym is healthy once registered, so nothing should be flagged"
    assert any("registered into Echo's account registry" in m for m in seen)
    accounts._dynamic_cache = None


def test_autoregister_never_invents_a_name(monkeypatch, tmp_path):
    """No real name means no registration: a fabricated one becomes the gym's Zernio
    profile name and its account label."""
    monkeypatch.setenv("AGENT_ONBOARDING_AUTOREGISTER", "true")
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    out = ow.run(deps=_unregistered(names={}), alert=lambda m: None, kv=_KV())
    assert out == {"newbox": [ow.REASON_NOT_REGISTERED]}
    assert accounts.get_account("newbox_ig") is None
    accounts._dynamic_cache = None


def test_autoregister_never_touches_lasso_or_staff(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ONBOARDING_AUTOREGISTER", "true")
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    assert ow.autoregister("lasso", "g9", deps=_unregistered()) is False
    assert ow.autoregister("blake_personal", "g9", deps=_unregistered()) is False
    accounts._dynamic_cache = None


def test_a_registration_failure_never_breaks_the_sweep(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ONBOARDING_AUTOREGISTER", "true")
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    monkeypatch.setattr(accounts, "register_gym",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    seen = []
    out = ow.run(deps=_unregistered(), alert=seen.append, kv=_KV())
    assert out == {"newbox": [ow.REASON_NOT_REGISTERED]}
    assert any("auto-register failed" in m for m in seen)


def test_autoregister_never_reports_success_when_it_wrote_nothing(monkeypatch, tmp_path):
    """FABRICATED SUCCESS. accounts.register_gym silently no-ops and returns [] (it does
    NOT raise) when AGENT_DYNAMIC_ACCOUNTS is off. Ignoring that return had this alert
    say 'registered into Echo's account registry', return True, and let run() drop
    not_registered from the day's alert while NOTHING was written: a gym reported as
    fixed that is still in neither lane. That is the exact failure class this sweep
    exists to catch, so it must never be the sweep's own behaviour."""
    monkeypatch.setenv("AGENT_ONBOARDING_AUTOREGISTER", "true")
    monkeypatch.delenv("AGENT_DYNAMIC_ACCOUNTS", raising=False)   # the registry is OFF
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    seen = []
    out = ow.run(deps=_unregistered(), alert=seen.append, kv=_KV())
    assert out == {"newbox": [ow.REASON_NOT_REGISTERED]}, "reported fixed while unwritten"
    assert not any("registered into Echo's account registry" in m for m in seen)
    assert any("did nothing" in m and "AGENT_DYNAMIC_ACCOUNTS" in m for m in seen)
    accounts._dynamic_cache = None
