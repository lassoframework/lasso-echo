"""
WEBSITE AUTO-INTAKE lane, fully OFFLINE (fake fetcher + fake LLM, tmp sqlite via
AGENT_DB_PATH, tmp voice dir via AGENT_CLIENT_VOICE_DIR).

Covered: extraction parses the LLM bundle and the digit gate drops fabricated
numbers and invented citations; output facts are dash-free; a gym with existing
sources is skipped; submit lands under <base>_ig with page-URL citations and the
standard intake status; the durable voice doc is written once and never
clobbered; flag OFF means total no-op; the runner wiring is flag-gated and
isolated; alerts are deduped per gym per outcome.
"""

import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_sources as cs  # noqa: E402
from agent import website_intake as wi  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(tmp_path / "brand_voice"))
    monkeypatch.delenv("AGENT_WEBSITE_AUTO_INTAKE", raising=False)
    monkeypatch.delenv("AGENT_INTAKE_AUTO_APPROVE", raising=False)
    yield


# ---- fixtures: a fake site and a fake LLM --------------------------------------

_HOME_TEXT = (
    "Gym X is a family owned strength and conditioning gym in Carmel serving "
    "busy parents since 2015. Our six week transformation challenge costs $199 "
    "and includes coaching and nutrition guidance for every new member. We "
    "offer small group personal training and every session is coached and "
    "scaled to your level. Warm up for ten minutes before every workout to "
    "protect your joints and build better sessions."
)
_HOME_URL = "https://gymx.com/"


def _fake_fetch(url):
    """Only the homepage exists; every other path 404s (returns None)."""
    if url == _HOME_URL:
        return f"<html><head><script>var x=1;</script></head><body><p>{_HOME_TEXT}</p></body></html>"
    return None


def _fake_llm(system, user):
    """A plausible extraction reply: valid facts plus one fabricated number,
    one invented citation, one nav crumb, and one em dash to scrub."""
    assert "PAGE https://gymx.com/" in user  # the fetched text reached the prompt
    return """Here is the bundle:
{"service": [
   {"fact": "We offer small group personal training — every session is coached and scaled to your level", "url": "https://gymx.com/"},
   {"fact": "Best gym in Carmel", "url": "https://gymx.com/"}
 ],
 "about": [
   {"fact": "Gym X is a family owned strength and conditioning gym in Carmel serving busy parents since 2015", "url": "https://gymx.com/"}
 ],
 "offer": [
   {"fact": "Our six week transformation challenge costs $199 and includes coaching and nutrition guidance for every new member", "url": "https://gymx.com/"},
   {"fact": "Memberships start at just $89 per month for unlimited access to all of our classes", "url": "https://gymx.com/"},
   {"fact": "Our six week transformation challenge costs $199 and includes coaching and nutrition guidance for every new member", "url": "https://gymx.com/pricing"}
 ],
 "educational": [
   {"fact": "Warm up for ten minutes before every workout to protect your joints and build better sessions", "url": "https://gymx.com/"}
 ],
 "testimonial": [
   {"fact": "I lost twenty pounds in my first three months training here with the coaches", "url": "https://gymx.com/"}
 ]}"""


def _extract():
    texts = wi.fetch_site_text("gymx.com", fetch=_fake_fetch)
    return texts, wi.extract_sources(texts, "Gym X", llm=_fake_llm)


# ---- 1. fetch strips HTML to visible text, tolerates 404s, never raises --------

def test_fetch_site_text_strips_and_tolerates_missing_pages():
    texts = wi.fetch_site_text("https://gymx.com/", fetch=_fake_fetch)
    assert list(texts) == [_HOME_URL]          # 404 pages are simply absent
    assert "var x=1" not in texts[_HOME_URL]   # script content never counts as text
    assert "$199" in texts[_HOME_URL]

    def _boom(url):
        raise RuntimeError("network down")
    assert wi.fetch_site_text("gymx.com", fetch=_boom) == {}


# ---- 2. extraction: digit gate, citation gate, word window, no testimonials ----

def test_extract_digit_gate_drops_fabricated_numbers():
    _, bundle = _extract()
    offers = [f for f, _ in bundle.get("offer", [])]
    assert any("$199" in f for f in offers)            # real price kept
    assert not any("$89" in f for f in offers)         # invented price dropped
    # the /pricing citation was never fetched: a fabricated citation is dropped
    assert all(u == _HOME_URL for _, u in bundle["offer"])
    assert len(bundle["offer"]) == 1


def test_extract_word_window_and_no_testimonials():
    _, bundle = _extract()
    services = [f for f, _ in bundle.get("service", [])]
    assert not any("Best gym in Carmel" in f for f in services)  # nav crumb dropped
    # testimonials need recorded permission; a scrape never lands one
    assert "testimonial" not in bundle
    assert bundle.get("about") and bundle.get("educational")


def test_extract_output_is_dash_free():
    _, bundle = _extract()
    for items in bundle.values():
        for fact, _url in items:
            assert not re.search(r"[—–-]", fact), fact


def test_extract_never_raises_on_garbage():
    assert wi.extract_sources({}, "Gym X", llm=_fake_llm) == {}
    assert wi.extract_sources({_HOME_URL: _HOME_TEXT}, "Gym X",
                              llm=lambda s, u: "not json at all") == {}

    def _boom(s, u):
        raise RuntimeError("no key")
    assert wi.extract_sources({_HOME_URL: _HOME_TEXT}, "Gym X", llm=_boom) == {}


# ---- 3. intake_from_website lands under <base>_ig, cited, standard status ------

def test_intake_lands_under_base_ig_with_citations(monkeypatch):
    out = wi.intake_from_website("gymx", domain="gymx.com",
                                 fetch=_fake_fetch, llm=_fake_llm)
    assert out["ok"] and out["landed"] >= 3
    rows = cs.all_sources("gymx_ig")
    assert rows and all(r.account_key == "gymx_ig" for r in rows)
    assert all(r.citation == _HOME_URL for r in rows)  # every fact cites its page
    # AGENT_INTAKE_AUTO_APPROVE unset: standard intake status is PENDING
    assert all(r.status == "pending" for r in rows)
    assert cs.approved_sources("gymx_ig") == []


def test_intake_status_follows_auto_approve_flag(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_AUTO_APPROVE", "true")
    out = wi.intake_from_website("gymx", domain="gymx.com",
                                 fetch=_fake_fetch, llm=_fake_llm)
    assert out["ok"] and out["status"] == "approved"
    assert cs.approved_sources("gymx_ig")


def test_intake_skips_gym_with_existing_sources():
    cs.add_source("gymx_ig", "about", "Family owned since 2015")
    out = wi.intake_from_website("gymx", domain="gymx.com",
                                 fetch=_fake_fetch, llm=_fake_llm)
    assert not out["ok"] and "already has sources" in out["reason"]
    # force overrides the vacuum-only rule (explicit human call)
    out = wi.intake_from_website("gymx", domain="gymx.com", force=True,
                                 fetch=_fake_fetch, llm=_fake_llm)
    assert out["ok"]


def test_intake_skips_gym_with_base_keyed_sources():
    # portal intakes land under the BARE base key; the variant-aware read must
    # still count them so a scrape never stacks on a real intake
    cs.add_source("gymx", "about", "Family owned since 2015")
    out = wi.intake_from_website("gymx", domain="gymx.com",
                                 fetch=_fake_fetch, llm=_fake_llm)
    assert not out["ok"] and "already has sources" in out["reason"]


def test_intake_never_raises_and_reports_reasons():
    # no domain anywhere on record
    out = wi.intake_from_website("nosuchgym")
    assert not out["ok"] and "no domain" in out["reason"]
    # domain resolves but the site is unreachable
    out = wi.intake_from_website("gymx", domain="gymx.com",
                                 fetch=lambda u: None, llm=_fake_llm)
    assert not out["ok"] and "no readable pages" in out["reason"]
    # LLM extraction yields nothing verifiable
    out = wi.intake_from_website("gymx", domain="gymx.com", fetch=_fake_fetch,
                                 llm=lambda s, u: "{}")
    assert not out["ok"] and "no verifiable facts" in out["reason"]
    assert cs.all_sources("gymx_ig") == []


# ---- 4. the durable voice doc: written once, never clobbered -------------------

def test_bible_written_when_missing_and_never_overwritten(tmp_path):
    from agent import config
    out = wi.intake_from_website("gymx", domain="gymx.com",
                                 fetch=_fake_fetch, llm=_fake_llm)
    assert out["ok"] and "bible written" in out["bible"]
    path = os.path.join(config.client_voice_dir(), "gymx", "lasso_voice.md")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as fh:
        bible = fh.read()
    assert "small group personal training" in bible       # site-derived fact
    assert "CTA rotation" in bible and "gymx.com" in bible
    assert "human approval" in bible                       # baseline guardrails
    # a second (forced) run leaves the existing doc alone: human owns voice
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\nHUMAN EDIT\n")
    out = wi.intake_from_website("gymx", domain="gymx.com", force=True,
                                 fetch=_fake_fetch, llm=_fake_llm)
    assert "bible exists, untouched" in out["bible"]
    with open(path, encoding="utf-8") as fh:
        assert "HUMAN EDIT" in fh.read()


# ---- 5. run(): flag gate, vacuum-only, deduped alerts ---------------------------

def test_run_flag_off_is_total_noop():
    calls = []
    out = wi.run(bases=["gymx"], websites={"gymx": "gymx.com"},
                 fetch=lambda u: calls.append(u), llm=_fake_llm,
                 alert=lambda m: calls.append(m))
    assert not out["ok"] and "AGENT_WEBSITE_AUTO_INTAKE" in out["reason"]
    assert calls == [] and cs.all_sources("gymx_ig") == []


def test_run_intakes_zero_source_gyms_and_alerts_once(monkeypatch):
    monkeypatch.setenv("AGENT_WEBSITE_AUTO_INTAKE", "true")
    alerts = []
    out = wi.run(bases=["gymx"], websites={"gymx": "gymx.com"},
                 fetch=_fake_fetch, llm=_fake_llm, alert=alerts.append)
    assert out["ok"] and out["intaken"] == ["gymx"] and out["landed"] >= 3
    assert len(alerts) == 1 and "auto-intake landed" in alerts[0]
    assert "no action needed" in alerts[0]
    # second sweep: the gym now has sources, so it is skipped and never re-alerted
    out = wi.run(bases=["gymx"], websites={"gymx": "gymx.com"},
                 fetch=_fake_fetch, llm=_fake_llm, alert=alerts.append)
    assert out["skipped"] == ["gymx"] and len(alerts) == 1


def test_run_failure_alert_is_deduped(monkeypatch):
    monkeypatch.setenv("AGENT_WEBSITE_AUTO_INTAKE", "true")
    alerts = []
    for _ in range(2):  # same failure two sweeps running -> ONE alert
        out = wi.run(bases=["gymfail"], websites={"gymfail": "bad.example"},
                     fetch=lambda u: None, llm=_fake_llm, alert=alerts.append)
        assert out["ok"] and out["failed"] == ["gymfail"]
    assert len(alerts) == 1 and "could not auto-intake" in alerts[0]


def test_run_survives_a_gym_that_blows_up(monkeypatch):
    monkeypatch.setenv("AGENT_WEBSITE_AUTO_INTAKE", "true")

    def _boom(u):
        raise RuntimeError("kaboom")
    out = wi.run(bases=["gymfail", "gymx"],
                 websites={"gymfail": "bad.example", "gymx": "gymx.com"},
                 fetch=_boom, llm=_fake_llm, alert=lambda m: None)
    assert out["ok"]  # the sweep itself never dies on one gym
    assert set(out["failed"]) == {"gymfail", "gymx"}


# ---- 6. runner wiring: flag-gated and isolated ----------------------------------

def test_runner_wiring_is_flag_gated_and_isolated():
    from agent import runner
    src = inspect.getsource(runner)
    assert "config.website_auto_intake_enabled()" in src
    block = src.split("config.website_auto_intake_enabled()", 1)[1]
    head = block[:1600]
    # the lane runs inside its own try/except so a sweep failure never takes
    # the draft run down (the zernio_profile_link isolation pattern)
    assert "try:" in head and "except Exception" in head
    assert "website_intake" in head
