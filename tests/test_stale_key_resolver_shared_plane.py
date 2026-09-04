"""The stale-key resolver has to work on the service a gym's request actually hits.

Dean Holcomb, CrossFit Reverb, 2026-09-04, twice in two minutes:
  "connected folders section says there are no connected folders. But when I reconnect
   my folder it says the folder is already connected."

Both answers were true, which is what made it confusing. His media_source row is bound
under crossfitreverb30b5b2; his portal link still carries the pre-re-key fingerprint
crossfitreverb6cdf33. _resolve_stale_fingerprint exists precisely to bridge that -- and it
did, on the WORKER. It could not on echo-intake-web, the service that serves
/portal/<token>/media/*, because it sourced its base list from client_gym_bases() ->
accounts.all_accounts() -> the registry FILE on the worker's Railway volume. That file does
not exist on the web service, so the list came back with no client gym in it, nothing
matched, and the stale key was handed back unchanged. The list read empty; the hijack rail
looks folders up globally and still found it. Two contradictory answers, one cause.

Live at the time of the fix: GET /portal/<6cdf33>/media/sources -> {"sources": []},
the same call under crossfitreverb30b5b2 -> his "Reverb LASSO Content" folder.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_routes as gmr  # noqa: E402

REVERB_STALE = "crossfitreverb6cdf33"
REVERB_REAL = "crossfitreverb30b5b2"


def _clear_cache():
    gmr._plane_bases_cache["at"] = 0.0
    gmr._plane_bases_cache["bases"] = frozenset()


def _plane(*keys):
    return lambda table, params: [{"echo_account_key": k} for k in keys]


def setup_function(_):
    _clear_cache()


def test_the_worker_path_still_works(monkeypatch):
    """The registry answer is unchanged where the registry exists."""
    monkeypatch.setattr(gmr, "_shared_plane_bases", lambda: set())
    monkeypatch.setattr(gmr._idx, "dedup_alert", lambda *a, **k: None)
    assert gmr._resolve_stale_fingerprint(
        REVERB_STALE, bases_fn=lambda: [REVERB_REAL, "eng"]) == REVERB_REAL


def test_an_empty_registry_no_longer_defeats_the_resolver(monkeypatch):
    """Dean's exact case: the web service has NO registry file, so the registry yields
    nothing. Before this fix that returned the stale key and his folder stayed invisible."""
    monkeypatch.setattr(gmr, "_shared_plane_bases", lambda: {REVERB_REAL, "eng", "topfuel"})
    monkeypatch.setattr(gmr._idx, "dedup_alert", lambda *a, **k: None)
    assert gmr._resolve_stale_fingerprint(REVERB_STALE, bases_fn=lambda: []) == REVERB_REAL


def test_a_registry_that_raises_still_resolves_from_the_plane(monkeypatch):
    """all_accounts() raising must degrade to the plane, not to the stale key."""
    def boom():
        raise RuntimeError("no registry on this service")
    monkeypatch.setattr(gmr, "_shared_plane_bases", lambda: {REVERB_REAL})
    monkeypatch.setattr(gmr._idx, "dedup_alert", lambda *a, **k: None)
    assert gmr._resolve_stale_fingerprint(REVERB_STALE, bases_fn=boom) == REVERB_REAL


def test_ambiguity_is_still_never_guessed(monkeypatch):
    """Two gyms may legitimately share a name-slug; the fingerprint is what separates
    them. More than one match must leave the key untouched."""
    monkeypatch.setattr(gmr, "_shared_plane_bases",
                        lambda: {"crossfitreverb111111", "crossfitreverb222222"})
    assert gmr._resolve_stale_fingerprint(REVERB_STALE, bases_fn=lambda: []) == REVERB_STALE


def test_a_key_that_is_itself_real_is_returned_untouched(monkeypatch):
    monkeypatch.setattr(gmr, "_shared_plane_bases", lambda: {REVERB_REAL})
    assert gmr._resolve_stale_fingerprint(REVERB_REAL, bases_fn=lambda: []) == REVERB_REAL


def test_everything_unreadable_leaves_the_key_alone(monkeypatch):
    """Both sources dark = today's behaviour exactly. Never remap on no evidence."""
    monkeypatch.setattr(gmr, "_shared_plane_bases", lambda: set())
    assert gmr._resolve_stale_fingerprint(REVERB_STALE, bases_fn=lambda: []) == REVERB_STALE


# ---- the shared-plane reader itself ----------------------------------------------------

def test_plane_reader_returns_the_portal_keys():
    got = gmr._shared_plane_bases(now_fn=lambda: 1000.0,
                                  get=_plane(REVERB_REAL, "eng", ""))
    assert got == {REVERB_REAL, "eng"}, "blank keys are dropped, never kept"


def test_plane_reader_fails_to_empty_never_raises():
    def boom(table, params):
        raise RuntimeError("supabase down")
    assert gmr._shared_plane_bases(now_fn=lambda: 1000.0, get=boom) == set()


def test_plane_reader_caches_within_the_ttl():
    calls = []

    def counting(table, params):
        calls.append(1)
        return [{"echo_account_key": REVERB_REAL}]

    gmr._shared_plane_bases(now_fn=lambda: 1000.0, get=counting)
    gmr._shared_plane_bases(now_fn=lambda: 1000.0 + gmr._PLANE_BASES_TTL - 1, get=counting)
    assert len(calls) == 1, "a per-request resolution must not become a per-request round trip"


def test_plane_reader_refreshes_after_the_ttl():
    calls = []

    def counting(table, params):
        calls.append(1)
        return [{"echo_account_key": REVERB_REAL}]

    gmr._shared_plane_bases(now_fn=lambda: 1000.0, get=counting)
    gmr._shared_plane_bases(now_fn=lambda: 1000.0 + gmr._PLANE_BASES_TTL + 1, get=counting)
    assert len(calls) == 2, "a newly onboarded gym must become visible"


def test_an_empty_plane_is_not_cached():
    """Caching an empty answer would pin the resolver dark for the whole TTL after one
    transient failure."""
    calls = []

    def flaky(table, params):
        calls.append(1)
        return [] if len(calls) == 1 else [{"echo_account_key": REVERB_REAL}]

    assert gmr._shared_plane_bases(now_fn=lambda: 1000.0, get=flaky) == set()
    assert gmr._shared_plane_bases(now_fn=lambda: 1000.5, get=flaky) == {REVERB_REAL}
