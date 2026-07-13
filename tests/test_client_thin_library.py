"""
Thin-library grace (Part 4). A client with approved sources but no photos gets
caption-ready days flagged needs-media (held, one ops alert), never a hard blocked
card. A client with neither approved text nor a usable creative gets a clear
blocked reason. A source-backed template card fills the slot when one is available.
Fully OFFLINE (tmp sqlite + tmp library).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, client_sources as cs, ops_alerts  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus, draft_post  # noqa: E402
from agent.library import pick_next  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    yield


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _client():
    return Account(key="gym_alpha_ig", display_name="Gym Alpha",
                   platform=Platform.INSTAGRAM, token_env="T", target_id_env="TID",
                   slack_channel="C_ALPHA")


def _empty_lib(tmp_path):
    lib = tmp_path / "empty_lib"
    lib.mkdir(exist_ok=True)
    return str(lib)


def _wire_alerts(monkeypatch):
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: fired.append(m))
    return fired


# ---- 1. sources but NO photos -> caption-ready needs-media, not blocked --------
def test_sources_no_photos_is_needs_media(tmp_path, monkeypatch):
    fired = _wire_alerts(monkeypatch)
    acct = _client()
    cs.add_source(acct.key, "offer", "6 week challenge for $199", "website /pricing")
    d = client_content.build_client_draft(acct, "2026-07-01", _voice(),
                                          _empty_lib(tmp_path))
    assert d is not None
    assert d.status == DraftStatus.PENDING          # held, NOT blocked
    assert d.needs_media is True
    assert client_content.classify(d) == "needs-media"
    assert d.caption.strip()                        # caption is ready
    assert d.creative_public_url == "" and d.creative_path == ""
    # exactly one ops alert names the account and the fix
    assert len(fired) == 1
    assert "needs-media" in fired[0] and "gym_alpha_ig" in fired[0]
    assert "Not blocked" in fired[0]


# ---- 2. the needs-media ops alert is deduped per day --------------------------
def test_needs_media_alert_deduped(tmp_path, monkeypatch):
    fired = _wire_alerts(monkeypatch)
    acct = _client()
    cs.add_source(acct.key, "offer", "Free intro session", "website /start")
    lib = _empty_lib(tmp_path)
    client_content.build_client_draft(acct, "2026-07-01", _voice(), lib)
    client_content.build_client_draft(acct, "2026-07-01", _voice(), lib)  # re-run
    assert len(fired) == 1                           # one alert for the day, not two


# ---- 3. a source-backed template card fills the slot when available -----------
def test_template_card_fills_the_slot(tmp_path, monkeypatch):
    fired = _wire_alerts(monkeypatch)
    acct = _client()
    cs.add_source(acct.key, "offer", "6 week challenge for $199", "website /pricing")

    def _template(account, source, day_key):
        return "https://cdn.echo.test/echo/gym_alpha_ig/template_offer.png"

    d = client_content.build_client_draft(acct, "2026-07-01", _voice(),
                                          _empty_lib(tmp_path), template_fn=_template)
    assert d.status == DraftStatus.PENDING
    assert d.needs_media is False
    assert client_content.classify(d) == "ready"
    assert d.creative_public_url.endswith("template_offer.png")
    assert "template_card" in d.source_fragments
    assert fired == []                               # no needs-media alert fired


# ---- 4. neither approved text nor a creative -> a clear blocked reason ---------
def test_nothing_at_all_is_blocked(tmp_path):
    """No approved sources: build_client_draft declines (None) and the library
    pick blocks with a clear reason (the runner's real fallback path)."""
    acct = _client()
    lib = _empty_lib(tmp_path)
    # no sources stocked at all
    d = client_content.build_client_draft(acct, "2026-07-01", _voice(), lib)
    assert d is None                                 # nothing to say -> defer
    # the runner's fallback: library pick is empty -> a blocked draft with a reason
    creative = pick_next(acct, lib, set())
    blocked = draft_post(acct, creative, "2026-07-01T18:30:00+00:00", voice=_voice())
    assert blocked.status == DraftStatus.BLOCKED
    assert client_content.classify(blocked) == "blocked"
    assert blocked.blocked_reason and "library" in blocked.blocked_reason.lower()


# ---- 5. needs_media round-trips through the store -----------------------------
def test_needs_media_persists_through_store(tmp_path):
    from agent.store import PendingStore
    acct = _client()
    cs.add_source(acct.key, "faq", "Do you offer childcare? Yes.", "website /faq")
    d = client_content.build_client_draft(acct, "2026-07-01", _voice(),
                                          _empty_lib(tmp_path))
    store = PendingStore(path=str(tmp_path / "store.db"))
    store.put(d)
    got = store.get(d.draft_id)
    assert got.needs_media is True
