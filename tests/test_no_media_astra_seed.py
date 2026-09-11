"""
no_media_astra_seed (agent/no_media_astra_seed.py), fully offline.

Blake 2026-09-11: a gym with ZERO real media AND no approved sources gets
Echo-generated Astra infographics unique to it, scraped from its own website +
Instagram (gym_deep_brain), instead of sitting on the generic sample month
forever. Asserts: flag gate, no-op with no facts, one-scrape-per-gym marking,
gap detection reuse, per-run cap, PENDING insert-only rows, and that a scrape
or generate failure never raises.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import no_media_astra_seed as nmas  # noqa: E402
from agent import config, db  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib_dir), raising=False)
    monkeypatch.setenv("AGENT_NO_MEDIA_ASTRA_SEED", "true")
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")


def _acct():
    return Account(key="chateau_ig", display_name="CrossFit Chateau",
                  platform=Platform.INSTAGRAM, token_env="T", target_id_env="G")


class _Fact:
    def __init__(self, text, source_url="https://crossfitchateau.com/about"):
        self.text = text
        self.source_url = source_url
        self.category = "service"


class _Store:
    def __init__(self, rows=()):
        self._rows = [dict(r) for r in rows]
        self.inserted = []

    def list_month(self, base, month):
        return [dict(r) for r in self._rows
                if str(r.get("post_date", "")).startswith(month)]

    def insert_rows(self, base, rows):
        self.inserted.extend(rows)
        return rows


def _stub_pipeline(monkeypatch):
    from agent import creative_studio, media_host

    class _Client:
        def generate_image(self, prompt, model):
            return b"\x89PNG_fake_card_bytes"
    monkeypatch.setattr(creative_studio, "_default_client", lambda: _Client())
    monkeypatch.setattr(creative_studio, "_render_with_timeout", lambda fn: fn())
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key: f"https://r2/{os.path.basename(path)}")


def _stub_deep_brain(monkeypatch, facts):
    from agent import gym_deep_brain

    def _fake_build(base, **kw):
        return {"ok": True, "base": base, "facts": facts}
    monkeypatch.setattr(gym_deep_brain, "build_deep_brain", _fake_build)


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_NO_MEDIA_ASTRA_SEED", "false")
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store)
    assert n == 0
    assert store.inserted == []


def test_no_facts_is_noop(monkeypatch):
    def _blocked(base, **kw):
        return {"ok": False, "blocked": True, "base": base, "reason": "no domain on record"}
    from agent import gym_deep_brain
    monkeypatch.setattr(gym_deep_brain, "build_deep_brain", _blocked)
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store)
    assert n == 0
    assert store.inserted == []


def test_generates_grounded_pending_rows(monkeypatch):
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [
        _Fact("Small group coaching built for beginners who never touched a barbell"),
        _Fact("Free childcare during every morning class"),
    ])
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=2, days_ahead=5)
    assert n == 2
    assert len(store.inserted) == 2
    for row in store.inserted:
        assert row["status"] == "pending"
        assert row["gym_id"] == "chateau"
        assert row["image_url"].startswith("https://r2/")
        assert row["caption"]


def test_existing_active_day_is_skipped(monkeypatch):
    """Gap detection reuses client_infographic_fill's _empty_upcoming_days: a day
    with an existing active IG feed row is never touched."""
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    from datetime import date, timedelta
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    store = _Store(rows=[{"post_date": tomorrow, "format": "feed",
                         "account": "instagram", "status": "pending"}])
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=1, days_ahead=1)
    assert n == 0
    assert store.inserted == []


def test_scrapes_once_per_gym(monkeypatch):
    """A second call reuses the pending sources already landed instead of
    re-scraping (kv marker set by the first call)."""
    _stub_pipeline(monkeypatch)
    calls = {"n": 0}
    from agent import gym_deep_brain

    def _counting_build(base, **kw):
        calls["n"] += 1
        return {"ok": True, "base": base,
               "facts": [_Fact("Real fact about Chateau")]}
    monkeypatch.setattr(gym_deep_brain, "build_deep_brain", _counting_build)
    from agent import client_sources
    monkeypatch.setattr(client_sources, "pending_sources",
                        lambda base, category=None: [_Fact("Real fact about Chateau")])

    store1 = _Store()
    nmas.seed_gaps("chateau", _acct(), store1, max_rows=1, days_ahead=1)
    store2 = _Store()
    nmas.seed_gaps("chateau", _acct(), store2, max_rows=1, days_ahead=2)
    assert calls["n"] == 1


def test_generate_failure_never_raises(monkeypatch):
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    from agent import creative_studio

    def _boom(*a, **k):
        raise RuntimeError("astra timeout")
    monkeypatch.setattr(creative_studio, "generate", _boom)
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=1, days_ahead=1)
    assert n == 0
    assert store.inserted == []


def test_deep_brain_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "false")
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store)
    assert n == 0
    assert store.inserted == []
