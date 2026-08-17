"""
GBP planner (agent/gbp_planner.py): offer resolver, and plan_gbp_month cadence +
row shape. Offline: caption_fn + image_fn injected (no LLM, no real images), sources
seeded in the sqlite client_sources, a fake store captures inserted rows.
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp_planner as gp, client_sources as cs  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    yield


class _Store:
    def __init__(self):
        self.rows = []

    def insert_rows(self, key, rows):
        self.rows.extend(rows)
        return rows


def _voice():
    return VoiceDoc(raw="We coach busy Carmel parents back to strong.", hashtags=[],
                   ctas=["Book a free intro."])


def _seed(acct="lasso_ig"):
    cs.add_source(acct, "service", "Small group strength coaching for busy parents", "intake")
    cs.add_source(acct, "about", "Coaching the Carmel community for years", "intake")
    cs.add_source(acct, "faq", "New here? Your first session is a easy on-ramp", "intake")


def _cap(fact):
    # a valid GBP caption naming the city, no dashes/hashtags/phone
    return ("Carmel parents: real strength on a schedule that fits your life, coached "
            "step by step so you actually stick with it and feel the difference.")


def _img(day_key, used):
    used.add(day_key)
    return f"https://r2/gbp/{day_key}.jpg"


# ---- offer resolver --------------------------------------------------------

def test_resolve_offer_from_jsonb_array_and_link():
    name, d = gp.resolve_offer(["12 Week Strength", "Free Trial"], "https://ghl/join")
    assert name == "12 Week Strength"
    assert d == {"redeemOnlineUrl": "https://ghl/join"}


def test_resolve_offer_skips_without_url_or_name():
    assert gp.resolve_offer(["12 Week Strength"], "") == (None, None)
    assert gp.resolve_offer([], "https://ghl/join") == (None, None)
    assert gp.resolve_offer(None, None) == (None, None)


def test_resolve_offer_dict_element_uses_name_not_stringified_dict():
    # a jsonb offer element that is an object must yield its name, never "{'name': ...}"
    name, d = gp.resolve_offer([{"name": "Free Trial", "id": 7}], "https://ghl/join")
    assert name == "Free Trial" and d == {"redeemOnlineUrl": "https://ghl/join"}
    # an object with no name-ish field -> skip (never fabricate a name from the dict repr)
    assert gp.resolve_offer([{"id": 7}], "https://ghl/join") == (None, None)


def test_plan_reports_failure_when_store_cannot_persist():
    _seed()

    class _NoPersist:
        pass  # no insert_rows

    out = gp.plan_gbp_month("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                            city="Carmel", store=_NoPersist(), start=date(2026, 9, 1),
                            offer=None, events=[], caption_fn=_cap, image_fn=_img)
    assert out["ok"] is False and out["planned"] == 0   # never a phantom success


# ---- cadence + row shape ---------------------------------------------------

def test_full_cadence_with_offer_and_event():
    _seed()
    store = _Store()
    out = gp.plan_gbp_month(
        "lasso", "lasso_ig", voice=_voice(), library_path="/nope", city="Carmel",
        store=store, start=date(2026, 9, 1), days=30, cta_url="https://gym.com/start",
        offer=("12 Week Strength", {"redeemOnlineUrl": "https://ghl/join"}),
        events=[{"title": "Open House", "fact": "Open house this month in Carmel",
                 "schedule": {"startDate": "2026-09-20", "endDate": "2026-09-20"}}],
        caption_fn=_cap, image_fn=_img)
    assert out["ok"]
    assert out["standard"] == 8 and out["offer"] == 1 and out["event"] == 1
    assert out["photo"] == 4
    # every row is a pending googlebusiness row keyed to the portal gym
    for r in store.rows:
        assert r["account"] == "googlebusiness" and r["status"] == "pending"
        assert r["gym_id"] == "lasso"
    # OFFER row: NO cta fields, carries offer + window
    offer_rows = [r for r in store.rows if r["gbp_topic_type"] == "OFFER"]
    assert len(offer_rows) == 1
    o = offer_rows[0]
    assert "gbp_cta_type" not in o and "gbp_cta_url" not in o
    assert o["gbp_offer"]["redeemOnlineUrl"] == "https://ghl/join"
    # the offer window opens on the offer's own post day and runs OFFER_WINDOW_DAYS
    assert o["gbp_event"]["schedule"]["startDate"] == o["post_date"]
    from datetime import date as _d, timedelta as _td
    _s = _d.fromisoformat(o["gbp_event"]["schedule"]["startDate"])
    _e = _d.fromisoformat(o["gbp_event"]["schedule"]["endDate"])
    assert (_e - _s).days == gp.OFFER_WINDOW_DAYS <= 30
    # STANDARD rows carry the CTA
    std = [r for r in store.rows if r["gbp_topic_type"] == "STANDARD" and r["format"] == "update"]
    assert len(std) == 8 and all(r["gbp_cta_type"] == "LEARN_MORE" for r in std)
    # photo drops: format photo, no caption gate applied
    photos = [r for r in store.rows if r["format"] == "photo"]
    assert len(photos) == 4 and all(r["caption"] == "" for r in photos)


def test_injected_facts_drive_standard_without_client_sources():
    # a tenant with NO client_sources (present==[]) still plans a full STANDARD run from an
    # injected real fact list (e.g. LASSO's lasso_now.md copy bank). Gates still apply.
    store = _Store()
    facts = [("All in one offer", "Ads, nurture, site, social, reporting in one place."),
             ("Sales are now", "The job is closing members, not building funnels."),
             ("Proof", "71.9% booked vs an 18.5% industry average.")]
    out = gp.plan_gbp_month(
        "lasso", "lasso_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), offer=None, events=[],
        facts=facts, caption_fn=_cap, image_fn=_img)
    assert out["ok"] and out["standard"] == 8      # cycled facts fill all 8 slots
    # the injected pillar names ride onto the rows
    pillars = {r["pillar"] for r in store.rows if r["gbp_topic_type"] == "STANDARD"
               and r["format"] == "update"}
    assert pillars <= {"All in one offer", "Sales are now", "Proof"}


def test_no_offer_skips_offer_slot():
    _seed()
    store = _Store()
    out = gp.plan_gbp_month("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                            city="Carmel", store=store, start=date(2026, 9, 1),
                            offer=None, events=[], caption_fn=_cap, image_fn=_img)
    assert out["ok"] and out["offer"] == 0     # never fabricate an offer


def test_sub_a_plus_caption_skips_slot_not_ships():
    _seed()
    store = _Store()
    # caption_fn returns a dash-laden (non-A+) caption -> every STANDARD slot skips
    out = gp.plan_gbp_month(
        "lasso", "lasso_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), offer=None, events=[],
        caption_fn=lambda f: None, image_fn=_img)   # None = not A+, skip
    assert out["standard"] == 0
    # photo drops still land (no caption gate)
    assert out["photo"] == 4
