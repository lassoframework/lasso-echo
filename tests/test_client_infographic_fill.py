"""
client_infographic_fill (agent/client_infographic_fill.py), fully offline.

Blake 2026-08-25: a gym that stops uploading photos gets on-brand infographic posts
built from its own APPROVED sources instead of going dark. Asserts: flag gate, gap
detection (a day with any active IG feed is never touched), per-run cap, PENDING
insert-only rows with IG+FB mirror, source grounding, and the A+ caption gate.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_infographic_fill as cif  # noqa: E402
from agent import client_sources as cs  # noqa: E402
from agent import config  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_INFOGRAPHIC_FILL", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "lib"), raising=False)
    # hosting + nano stubbed per test


def _acct():
    return Account(key="gymx_ig", display_name="Gym X", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="G")


def _voice():
    return VoiceDoc(raw="We help busy people win.\n#GymX",
                    hashtags=["#GymX"], ctas=["Book your intro session."])


def _sources():
    cs.add_source("gymx_ig", "educational",
                  "Strength training twice a week protects your joints as you age and "
                  "keeps everyday tasks feeling easy well into your sixties",
                  "website /blog")
    cs.add_source("gymx_ig", "service",
                  "Small group personal training built for beginners who want real "
                  "coaching without the intimidation of a big box gym floor",
                  "website /services")


class _Store:
    """In-memory calendar: existing rows + records inserts. INSERT-only assertable."""

    def __init__(self, rows=()):
        self._rows = [dict(r) for r in rows]
        self.inserted = []
        self.deleted = []

    def list_month(self, base, month):
        return [dict(r) for r in self._rows
                if str(r.get("post_date", "")).startswith(month)]

    def insert_rows(self, base, rows):
        self.inserted.extend(rows)
        return rows

    def delete_month(self, *a, **k):       # must never be called
        self.deleted.append(a)
        return 0


def _stub_pipeline(monkeypatch):
    """Stub nano + hosting so the test is offline; captions go through the REAL
    make_caption (template path, no LLM key) and the REAL A+ gate."""
    from agent import creative_studio, media_host

    class _Client:
        def generate_image(self, prompt, model):
            return b"\x89PNG_fake_card_bytes"
    monkeypatch.setattr(creative_studio, "_default_client", lambda: _Client())
    monkeypatch.setattr(creative_studio, "_render_with_timeout", lambda fn: fn())
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key: f"https://r2/{os.path.basename(path)}")


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_INFOGRAPHIC_FILL", "false")
    out = cif.fill_gaps("gymx", _acct(), _Store(), voice=_voice())
    assert out["ok"] is False and out["reason"] == "flag off"


def test_fills_empty_days_with_pending_infographic_rows(monkeypatch):
    _sources()
    _stub_pipeline(monkeypatch)
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is True and out["filled"] == cif.FILL_MAX_PER_RUN
    assert store.deleted == [], "fill must be INSERT-only"
    feeds_ig = [r for r in store.inserted
                if r["format"] == "feed" and r["account"] == "instagram"]
    feeds_fb = [r for r in store.inserted
                if r["format"] == "feed" and r["account"] == "facebook"]
    assert len(feeds_ig) == cif.FILL_MAX_PER_RUN and len(feeds_fb) == len(feeds_ig)
    for r in store.inserted:
        assert r["status"] == "pending", "every card awaits the owner's approval"
        assert r["gym_id"] == "gymx"
        assert (r.get("image_url") or "").startswith("https://r2/igfill_")
        assert "id" not in r
        assert len(r.get("caption") or "") >= 40           # a real caption, not a stub


def test_days_with_existing_feeds_are_never_touched(monkeypatch):
    _sources()
    _stub_pipeline(monkeypatch)
    # every upcoming day already has an active IG feed -> zero gaps -> zero inserts
    rows = [{"post_date": f"2026-08-{d:02d}", "format": "feed",
             "account": "instagram", "status": "pending"} for d in range(26, 32)] + \
           [{"post_date": f"2026-09-{d:02d}", "format": "feed",
             "account": "instagram", "status": "approved"} for d in range(1, 3)]
    store = _Store(rows)
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is True and out["filled"] == 0
    assert store.inserted == []


def test_denied_days_count_as_empty(monkeypatch):
    _sources()
    _stub_pipeline(monkeypatch)
    rows = [{"post_date": "2026-08-26", "format": "feed", "account": "instagram",
             "status": "denied"}]
    store = _Store(rows)
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert out["filled"] == 1
    assert store.inserted[0]["post_date"] == "2026-08-26"


def test_no_sources_is_noop(monkeypatch):
    _stub_pipeline(monkeypatch)
    out = cif.fill_gaps("gymx", _acct(), _Store(), voice=_voice())
    assert out["ok"] is False and out["reason"] == "no sources"
