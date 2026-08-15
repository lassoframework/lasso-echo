"""
GBP dogfood entrypoint (agent/gbp_dogfood.py): offer/connection resolution, the
idempotency guard (never a duplicate month), the voice-missing BLOCK (no fabrication),
and a full real-cadence plan through the injected planner. Offline: caption_fn/image_fn
injected, sources seeded in sqlite, a fake store captures rows + serves idempotency.
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp_dogfood as gd, client_sources as cs  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    yield


class _Store:
    def __init__(self, existing=None, conns=None):
        self.rows = []
        self._existing = existing or []
        self._conns = conns or []

    def future_gbp_rows(self, gym, on_or_after):
        return list(self._existing)

    def connections_for(self, gym):
        return list(self._conns)

    def insert_rows(self, key, rows):
        self.rows.extend(rows)
        return rows


class _Clients:
    def __init__(self, rec):
        self._rec = rec

    def onboarding_intake(self, gym):
        return self._rec


def _voice():
    return VoiceDoc(raw="We coach busy Carmel parents back to strong.", hashtags=[],
                   ctas=["Book a free intro."])


def _seed(acct="lasso_ig"):
    cs.add_source(acct, "service", "Small group strength coaching for busy parents", "intake")
    cs.add_source(acct, "about", "Coaching the Carmel community for years", "intake")
    cs.add_source(acct, "faq", "New here? Your first session is a easy on-ramp", "intake")


def _cap(fact):
    return ("Carmel parents: real strength on a schedule that fits your life, coached "
            "step by step so you actually stick with it and feel the difference.")


def _img(day_key, used):
    used.add(day_key)
    return f"https://r2/gbp/{day_key}.jpg"


# ---- offer resolution ------------------------------------------------------

def test_offer_resolves_from_gym_record():
    clients = _Clients({"offers": ["12 Week Strength"], "ghl_link": "https://ghl/join"})
    name, d = gd._resolve_offer_for("lasso", clients)
    assert name == "12 Week Strength" and d["redeemOnlineUrl"] == "https://ghl/join"


def test_offer_skips_when_no_gym_record():
    assert gd._resolve_offer_for("lasso", _Clients(None)) == (None, None)
    assert gd._resolve_offer_for("lasso", None) == (None, None)


def test_offer_never_fabricated_without_url():
    clients = _Clients({"offers": ["12 Week Strength"], "ghl_link": ""})
    assert gd._resolve_offer_for("lasso", clients) == (None, None)


# ---- connection resolution -------------------------------------------------

def test_connection_location_none_before_connect():
    # not connected yet -> None location (planner plans without it; worker binds later)
    store = _Store(conns=[{"status": "pending", "gbp_location_id": "locations/1"}])
    loc, _ = gd._resolve_connection_location("lasso", store)
    assert loc is None


def test_connection_location_resolved_when_connected():
    store = _Store(conns=[{"status": "connected", "gbp_location_id": "locations/9"}])
    loc, _ = gd._resolve_connection_location("lasso", store)
    assert loc == "locations/9"


# ---- idempotency + block ---------------------------------------------------

def test_idempotent_skip_when_month_exists():
    _seed()
    store = _Store(existing=[{"id": "x", "post_date": "2026-09-02"}])
    out = gd.plan_gbp_dogfood("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                              city="Carmel", store=store, start=date(2026, 9, 1),
                              caption_fn=_cap, image_fn=_img)
    assert out["skipped_existing"] is True and out["planned"] == 0
    assert store.rows == []      # nothing written on the no-op


def test_idempotency_read_failure_aborts_without_write():
    # a swallowed read error must NOT re-plan (would double-write; insert has no upsert)
    _seed()

    class _Boom(_Store):
        def future_gbp_rows(self, gym, on_or_after):
            raise RuntimeError("transient 5xx")

    store = _Boom()
    out = gd.plan_gbp_dogfood("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                              city="Carmel", store=store, start=date(2026, 9, 1),
                              caption_fn=_cap, image_fn=_img)
    assert out["ok"] is False and out["planned"] == 0
    assert store.rows == []      # nothing written when the guard read fails


def test_missing_voice_blocks_never_fabricates():
    _seed()
    store = _Store()
    out = gd.plan_gbp_dogfood("lasso", "lasso_ig", voice=None, library_path="/x",
                              city="Carmel", store=store, start=date(2026, 9, 1),
                              caption_fn=_cap, image_fn=_img)
    assert out["ok"] is False and out["planned"] == 0
    assert store.rows == []


# ---- full cadence through the entrypoint -----------------------------------

def test_full_cadence_writes_pending_rows():
    _seed()
    store = _Store()
    out = gd.plan_gbp_dogfood(
        "lasso", "lasso_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), cta_url="https://gym.com/start",
        offer=None, events=[], caption_fn=_cap, image_fn=_img)
    assert out["ok"] and out["standard"] == 8 and out["photo"] == 4
    assert out["offer"] == 0            # LASSO has no live offer -> slot skipped
    assert all(r["account"] == "googlebusiness" and r["status"] == "pending"
               for r in store.rows)


def test_base_of_strips_platform_suffix():
    assert gd._base_of("lasso_ig") == "lasso"
    assert gd._base_of("gritx_fb") == "gritx"
    assert gd._base_of("lasso") == "lasso"
