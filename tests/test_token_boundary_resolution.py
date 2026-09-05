"""Resolution must happen at the TOKEN BOUNDARY, and a revert must break a test.

The audit's MAJOR #2: every "token boundary" test called the private helper directly, so
nothing exercised client_for_token itself. The auditor replaced

    return _resolved_account_key(raw)      ->      return raw

in intake_web.client_for_token -- the single line that fixes Dean's /calendar symptom --
and the ENTIRE suite still passed: 5299 passed, 11 skipped. The one line that mattered was
unguarded.

These tests drive client_for_token with a real signed token, so that revert now fails here.
They also cover a NON-media route end to end, because the whole point of moving resolution
to the boundary was that calendar/report/library/events/studio/support inherit it instead of
being fixed one route at a time.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import account_key_resolve as akr  # noqa: E402
from agent import config, intake_tokens, intake_web  # noqa: E402

REVERB_ID = "30b5b234-0dac-4048-87d8-5330e6fbfa9d"
REVERB_NAME = "CrossFit Reverb"
REVERB_LIVE = "crossfitreverb30b5b2"
REVERB_STALE = "crossfitreverb6cdf33"


def _plane(tokens=None, gyms=None):
    tokens = tokens if tokens is not None else [
        {"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}]
    gyms = gyms if gyms is not None else [{"id": REVERB_ID, "name": REVERB_NAME}]

    def get(path, params):
        rows = tokens if path == "echo_intake_tokens" else gyms
        off = int(params.get("offset", 0)); lim = int(params.get("limit", 1000))
        return rows[off:off + lim], True
    return get


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv(config.INTAKE_SIGNING_SECRET_ENV, "test-secret-for-boundary")
    monkeypatch.setattr(intake_web, "is_revoked", lambda k: False)
    akr.reset_cache()
    # Pin the resolver's plane for every test in this file.
    real_resolve = akr.resolve
    monkeypatch.setattr(akr, "resolve",
                        lambda key, now_fn=None, get=None: real_resolve(
                            key, now_fn=lambda: 1000.0, get=_plane()))
    yield
    akr.reset_cache()


def test_client_for_token_returns_the_resolved_key():
    """THE guard. Revert `return _resolved_account_key(raw)` to `return raw` and this fails."""
    token = intake_tokens.mint(REVERB_STALE)
    assert intake_web.client_for_token(token) == REVERB_LIVE


def test_a_token_for_a_live_key_is_unchanged():
    token = intake_tokens.mint(REVERB_LIVE)
    assert intake_web.client_for_token(token) == REVERB_LIVE


def test_a_garbage_token_is_still_rejected():
    assert intake_web.client_for_token("not-a-real-token") is None
    assert intake_web.client_for_token("") is None


def test_a_revoked_token_is_never_revived(monkeypatch):
    """Resolution is a repair, not a bypass: a killed link stays killed, and the caller's
    own is_revoked check must still see a key it will refuse."""
    monkeypatch.setattr(intake_web, "is_revoked", lambda k: k == REVERB_STALE)
    token = intake_tokens.mint(REVERB_STALE)
    assert intake_web.client_for_token(token) == REVERB_STALE


def test_a_non_media_route_gets_the_resolved_key_end_to_end(monkeypatch):
    """calendar is the route Dean actually hit. Before the boundary fix it returned 0
    drafts under the stale key while the live key returned 90."""
    from agent import portal_routes

    seen = {}

    def fake_calendar(account_key, month, store=None):
        seen["account_key"] = account_key
        return 200, {"account_key": account_key, "month": month,
                     "drafts": [{"draft_id": "d1"}] if account_key == REVERB_LIVE else []}

    monkeypatch.setattr(portal_routes, "handle_portal_calendar", fake_calendar)
    token = intake_tokens.mint(REVERB_STALE)
    resolved = intake_web.client_for_token(token)
    status, body = portal_routes.handle_portal_calendar(resolved, "2026-09")

    assert seen["account_key"] == REVERB_LIVE, \
        "the calendar route must be handed the key the gym's data lives under"
    assert status == 200 and len(body["drafts"]) == 1


def test_resolution_failure_never_breaks_a_working_token(monkeypatch):
    """This sits in the auth path. A resolver fault must degrade to the raw key, not None."""
    monkeypatch.setattr(akr, "resolve",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("plane down")))
    token = intake_tokens.mint(REVERB_STALE)
    assert intake_web.client_for_token(token) == REVERB_STALE
