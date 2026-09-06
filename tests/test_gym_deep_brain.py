"""
PER-GYM DEEP BRAIN (agent/gym_deep_brain.py), fully OFFLINE: fake fetcher, fake
LLM, fake Apify client, fake clock/sleep, tmp sqlite + tmp artifact/voice dirs.

Blake's ruling (2026-09-06): "create a deeper brain for each gym scraping their
website and all their social media prior to starting with us."

Covered, one test per rule the module claims:
  - AGENT_GYM_DEEP_BRAIN OFF is a total no-op (nothing fetched, nothing written);
  - robots.txt is obeyed: a disallowed URL is SKIPPED and never requested, and a
    site that disallows everything BLOCKS the build with no artifact;
  - rate limiting: a minimum gap between same-host requests, and robots'
    Crawl-delay wins when it is larger;
  - PII: emails and phones that are not the business's OWN published contact
    details are redacted before anything is stored;
  - attribution is structural: an unattributed claim cannot be constructed, a
    citation we never fetched is refused, and the digit guard drops any figure
    the cited page does not state;
  - facts land PENDING with the page URL as the citation, even with
    AGENT_INTAKE_AUTO_APPROVE armed;
  - an existing brand bible is never overwritten;
  - missing data BLOCKS and produces no artifact (no domain / no handle / social
    read failed);
  - social capture is FORM only and never reads comments or commenter identities;
  - the artifact is a separate per-gym file and never lands under lasso-brain;
  - the CLI subcommand is registered and dispatches.
"""

import inspect
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_sources as cs  # noqa: E402
from agent import gym_deep_brain as gdb  # noqa: E402
from agent import website_intake as wi  # noqa: E402


# ---- fixtures: a fake site, a fake feed, a fake clock --------------------------

_DOM = "gymx.com"
_HOME = "https://gymx.com/"
_ABOUT = "https://gymx.com/about"
_PRICING = "https://gymx.com/pricing"

# The homepage is where the BUSINESS publishes its own contact details.
_HOME_TEXT = (
    "Gym X is a family owned strength and conditioning gym in Carmel serving "
    "busy parents since 2015. Call the front desk at 317 555 0142 or email "
    "hello@gymx.com to get started today. Our six week starter program costs "
    "$199 and includes coaching and nutrition guidance for every new member."
)
# The about page carries a MEMBER's personal email and phone. Neither is the
# business's own published contact detail, so both must be redacted.
_ABOUT_TEXT = (
    "Coach Dana Ruiz has led the morning classes since 2018 and coach Marco "
    "Bell runs the evening sessions every weekday. Member Sarah reached out "
    "from sarah.member@gmail.com and her number 317 555 9987 is on file for "
    "scheduling questions."
)
_PRICING_TEXT = (
    "Drop in visits are welcome and every new member starts with a free "
    "consultation with a coach before joining any class at Gym X."
)

_ROBOTS_OPEN = "User-agent: *\nAllow: /\n"
_ROBOTS_NO_PRICING = "User-agent: *\nDisallow: /pricing\n"
_ROBOTS_NO_EVERYTHING = "User-agent: *\nDisallow: /\n"
_ROBOTS_SLOW = "User-agent: *\nCrawl-delay: 9\nAllow: /\n"

_PAGES = {_HOME: _HOME_TEXT, _ABOUT: _ABOUT_TEXT, _PRICING: _PRICING_TEXT}


def _html(text):
    return f"<html><head><title>t</title></head><body><p>{text}</p></body></html>"


class FakeSite:
    """A gym site. Records every URL actually requested, so a test can assert a
    robots-disallowed page was never fetched (not merely dropped afterwards)."""

    def __init__(self, robots=_ROBOTS_OPEN, pages=None):
        self.robots = robots
        self.pages = dict(pages if pages is not None else _PAGES)
        self.requested = []

    def __call__(self, url):
        self.requested.append(url)
        if url.endswith("/robots.txt"):
            return self.robots
        text = self.pages.get(url)
        return _html(text) if text else None


class FakeClock:
    """A clock that only moves when sleep() is called, so a rate-limit test
    asserts the delay was taken without any real waiting."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(float(seconds), 3))
        self.now += float(seconds)


_GOOD_SERVICE = ("Gym X offers a six week starter program that includes "
                 "coaching and nutrition guidance for every new member.")
_GOOD_ABOUT = ("Coach Dana Ruiz has led the morning classes since 2018 and "
               "coach Marco Bell runs the evening sessions every weekday.")
_FAB_PRICE = ("Gym X charges $999 for a mystery package that nobody on the "
              "website ever mentions anywhere at all.")
_GHOST_URL = "https://gymx.com/ghost"


def _fake_llm(system, user):
    """Selects two real facts, one fabricated price, and one fact citing a page
    that was never fetched. The gates must keep the last two out."""
    import json
    return json.dumps({
        "service": [{"fact": _GOOD_SERVICE, "url": _HOME},
                    {"fact": _FAB_PRICE, "url": _HOME}],
        "about": [{"fact": _GOOD_ABOUT, "url": _ABOUT},
                  {"fact": "Gym X runs a second secret location that the site "
                           "never mentions on any page anywhere at all.",
                   "url": _GHOST_URL}],
    })


_TODAY = date(2026, 9, 1)

_POSTS = [
    {"timestamp": "2026-08-28T12:00:00.000Z",
     "caption": "Six weeks in and feeling strong \U0001F4AA\nBook your free "
                "intro at the link in bio #gymx #carmel",
     "type": "Video", "productType": "clips", "videoPlayCount": 5200,
     "likesCount": 90, "url": "https://www.instagram.com/p/AAA/",
     # user-generated content the module must never read:
     "latestComments": [{"ownerUsername": "nosypete",
                         "text": "SECRETCOMMENTTOKEN call me at 317 555 7777"}]},
    {"timestamp": "2026-08-20T12:00:00.000Z",
     "caption": "Coach spotlight. Dana has coached mornings here for years.",
     "type": "Image", "likesCount": 40, "shortCode": "BBB",
     "taggedUsers": [{"username": "someMemberHandle"}]},
    {"timestamp": "2026-08-10T12:00:00.000Z",
     "caption": "New session times posted. Sign up today at the link in bio.",
     "type": "Image", "likesCount": 61, "url": "https://www.instagram.com/p/CCC/"},
]


class FakeApify:
    def __init__(self, posts=None, error=None):
        self._posts = list(posts if posts is not None else _POSTS)
        self._error = error
        self.calls = []

    def fetch_posts(self, handle, newer_than_days, results_limit=None):
        self.calls.append((handle, newer_than_days, results_limit))
        if self._error:
            raise RuntimeError(self._error)
        return self._posts


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN_DIR", str(tmp_path / "deep_brains"))
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(tmp_path / "brand_voice"))
    monkeypatch.delenv("AGENT_GYM_DEEP_BRAIN", raising=False)
    monkeypatch.delenv("AGENT_INTAKE_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AGENT_GYM_DEEP_BRAIN_CRAWL_DELAY", raising=False)
    yield


def _build(monkeypatch, base="gymx", **kw):
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "true")
    site = kw.pop("site", None) or FakeSite()
    clock = kw.pop("clock", None) or FakeClock()
    kw.setdefault("domain", _DOM)
    kw.setdefault("handle", "gymx")
    kw.setdefault("llm", _fake_llm)
    kw.setdefault("apify", FakeApify())
    kw.setdefault("today", _TODAY)
    kw.setdefault("paths", ("/", "/about", "/pricing"))
    out = gdb.build_deep_brain(base, fetch=site, sleep=clock.sleep, clock=clock, **kw)
    return out, site, clock


# ---- 1. the flag ---------------------------------------------------------------

def test_flag_off_is_a_total_noop(tmp_path):
    """OFF by default: nothing is fetched, nothing is written, the reason names
    the flag."""
    site = FakeSite()
    out = gdb.build_deep_brain("gymx", domain=_DOM, handle="gymx", fetch=site,
                               llm=_fake_llm, apify=FakeApify())
    assert out["ok"] is False and out["blocked"] is True
    assert "AGENT_GYM_DEEP_BRAIN" in out["reason"]
    assert site.requested == []
    assert not os.path.exists(gdb.deep_brain_path("gymx"))
    assert cs.all_sources("gymx_ig") == []


# ---- 2. robots.txt --------------------------------------------------------------

def test_robots_disallowed_page_is_never_requested(monkeypatch):
    """A Disallow'd URL is SKIPPED and the fetcher never sees it."""
    site = FakeSite(robots=_ROBOTS_NO_PRICING)
    clock = FakeClock()
    crawl = gdb.crawl_site(_DOM, ("/", "/about", "/pricing"), fetch=site,
                           sleep=clock.sleep, clock=clock)
    assert _PRICING in crawl.skipped_robots
    assert _PRICING not in site.requested        # never fetched, not merely dropped
    assert _PRICING not in crawl.pages
    assert _HOME in crawl.pages and _ABOUT in crawl.pages


def test_robots_disallowing_everything_blocks_the_build(monkeypatch):
    """robots.txt closing the whole site is a BLOCK: no artifact, no rows."""
    out, site, _ = _build(monkeypatch, site=FakeSite(robots=_ROBOTS_NO_EVERYTHING))
    assert out["ok"] is False and out["blocked"] is True
    assert "robots" in out["reason"].lower()
    assert site.requested == [f"https://{_DOM}/robots.txt"]
    assert not os.path.exists(gdb.deep_brain_path("gymx"))
    assert cs.all_sources("gymx_ig") == []


def test_robots_is_fetched_once_per_host(monkeypatch):
    """The policy caches per host: three pages, one robots.txt request."""
    site = FakeSite()
    clock = FakeClock()
    gdb.crawl_site(_DOM, ("/", "/about", "/pricing"), fetch=site,
                   sleep=clock.sleep, clock=clock)
    assert site.requested.count(f"https://{_DOM}/robots.txt") == 1


# ---- 3. rate limiting -----------------------------------------------------------

def test_same_host_requests_are_rate_limited(monkeypatch):
    """A minimum gap between two requests to the same host. The first request
    waits for nothing; every one after it waits the full delay."""
    site = FakeSite()
    clock = FakeClock()
    crawl = gdb.crawl_site(_DOM, ("/", "/about", "/pricing"), fetch=site,
                           sleep=clock.sleep, clock=clock)
    assert crawl.crawl_delay == 2.0
    assert clock.sleeps == [2.0, 2.0]


def test_robots_crawl_delay_beats_our_floor(monkeypatch):
    """A site asking for a LONGER delay gets it; our floor is a floor, never a
    ceiling."""
    site = FakeSite(robots=_ROBOTS_SLOW)
    clock = FakeClock()
    crawl = gdb.crawl_site(_DOM, ("/", "/about", "/pricing"), fetch=site,
                           sleep=clock.sleep, clock=clock)
    assert crawl.crawl_delay == 9.0
    assert clock.sleeps == [9.0, 9.0]


def test_crawl_delay_floor_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN_CRAWL_DELAY", "5")
    site = FakeSite()
    clock = FakeClock()
    crawl = gdb.crawl_site(_DOM, ("/", "/about"), fetch=site,
                           sleep=clock.sleep, clock=clock)
    assert crawl.crawl_delay == 5.0
    assert clock.sleeps == [5.0]


# ---- 4. caps --------------------------------------------------------------------

def test_page_cap_stops_a_runaway_crawl(monkeypatch):
    site = FakeSite()
    clock = FakeClock()
    crawl = gdb.crawl_site(_DOM, ("/", "/about", "/pricing"), fetch=site,
                           sleep=clock.sleep, clock=clock, max_pages=2)
    assert len(crawl.fetched) == 2
    assert crawl.skipped_cap == [_PRICING]
    assert _PRICING not in site.requested


def test_byte_cap_stops_a_runaway_crawl(monkeypatch):
    site = FakeSite()
    clock = FakeClock()
    crawl = gdb.crawl_site(_DOM, ("/", "/about", "/pricing"), fetch=site,
                           sleep=clock.sleep, clock=clock, max_bytes=10)
    assert len(crawl.fetched) == 1
    assert len(crawl.skipped_cap) == 2


# ---- 5. PII ---------------------------------------------------------------------

def test_business_contacts_are_the_business_own_published_details():
    emails, phones = gdb.business_contacts(_PAGES, _DOM)
    assert emails == {"hello@gymx.com"}
    assert phones == {"3175550142"}          # published on the homepage
    assert "3175559987" not in phones        # a member's number on /about


def test_scrub_pii_redacts_everything_that_is_not_the_business():
    emails, phones = gdb.business_contacts(_PAGES, _DOM)
    out = gdb.scrub_pii(_ABOUT_TEXT, emails, phones)
    assert "sarah.member@gmail.com" not in out
    assert "317 555 9987" not in out and "3175559987" not in out
    assert gdb.EMAIL_REDACTION in out and gdb.PHONE_REDACTION in out
    # Coach names as the gym itself publishes them are business info, kept.
    assert "Dana Ruiz" in out and "Marco Bell" in out
    # The business's own published details survive.
    kept = gdb.scrub_pii(_HOME_TEXT, emails, phones)
    assert "hello@gymx.com" in kept and "317 555 0142" in kept


def test_stored_page_text_and_artifact_carry_no_member_pii(monkeypatch):
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True
    body = open(out["artifact"], encoding="utf-8").read()
    assert "sarah.member@gmail.com" not in body
    assert "317 555 9987" not in body
    crawl = gdb.crawl_site(_DOM, ("/", "/about"), fetch=FakeSite(),
                           sleep=FakeClock().sleep, clock=FakeClock())
    assert "sarah.member@gmail.com" not in crawl.pages[_ABOUT]
    assert gdb.PHONE_REDACTION in crawl.pages[_ABOUT]


# ---- 6. attribution is structural ----------------------------------------------

def test_an_unattributed_claim_cannot_be_constructed():
    """The guarantee is in the type, not in a convention: no source URL, no fact."""
    with pytest.raises(gdb.UnattributedClaim):
        gdb.GroundedFact(category="service", text="Gym X runs classes", source_url="")
    with pytest.raises(gdb.UnattributedClaim):
        gdb.GroundedFact(category="service", text="Gym X runs classes",
                         source_url="   ")
    with pytest.raises(gdb.UnattributedClaim):
        gdb.TopPost(permalink="", metric="likes", value=1.0)
    # a real citation constructs fine
    assert gdb.GroundedFact("service", "Gym X runs classes", _HOME).source_url == _HOME


def test_a_brain_refuses_anything_that_is_not_a_grounded_fact():
    brain = gdb.DeepBrain(base="gymx", gym_name="Gym X", domain=_DOM,
                          pages={_HOME: _HOME_TEXT})
    with pytest.raises(TypeError):
        brain.add_fact("Gym X runs classes")
    with pytest.raises(TypeError):
        brain.add_fact({"text": "Gym X runs classes", "url": _HOME})
    assert brain.facts == []


def test_a_citation_we_never_fetched_is_refused():
    brain = gdb.DeepBrain(base="gymx", gym_name="Gym X", domain=_DOM,
                          pages={_HOME: _HOME_TEXT})
    ok = brain.add_fact(gdb.GroundedFact("service", "Gym X runs small classes",
                                         _GHOST_URL))
    assert ok is False
    assert brain.facts == []
    assert any("not a page this crawl fetched" in r for r in brain.refused)


def test_the_digit_guard_drops_a_figure_the_page_never_states():
    brain = gdb.DeepBrain(base="gymx", gym_name="Gym X", domain=_DOM,
                          pages={_HOME: _HOME_TEXT})
    assert brain.add_fact(gdb.GroundedFact("service", _GOOD_SERVICE, _HOME)) is True
    assert brain.add_fact(gdb.GroundedFact("offer", _FAB_PRICE, _HOME)) is False
    assert [f.text for f in brain.facts] == [_GOOD_SERVICE]
    assert any("not stated on the cited page" in r for r in brain.refused)


def test_the_digit_guard_runs_against_the_scrubbed_text():
    """A redacted phone number's digits cannot be smuggled back in as a fact."""
    emails, phones = gdb.business_contacts(_PAGES, _DOM)
    brain = gdb.DeepBrain(base="gymx", gym_name="Gym X", domain=_DOM,
                          pages={_ABOUT: gdb.scrub_pii(_ABOUT_TEXT, emails, phones)})
    bad = gdb.GroundedFact("about", "Reach the coaching team on 317 555 9987 any "
                                    "weekday morning before class starts.", _ABOUT)
    assert brain.add_fact(bad) is False


def test_a_voice_observation_must_say_what_it_measured():
    with pytest.raises(ValueError):
        gdb.VoiceObservation(metric="cadence", value=3, basis="")
    with pytest.raises(ValueError):
        gdb.VoiceObservation(metric="", value=3, basis="12 posts")
    assert gdb.VoiceObservation("cadence", 3, "12 posts").basis == "12 posts"


def test_every_fact_in_a_built_artifact_carries_its_source(monkeypatch):
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True
    body = open(out["artifact"], encoding="utf-8").read()
    assert _GOOD_SERVICE in body and f"source: {_HOME}" in body
    assert _GOOD_ABOUT in body and f"source: {_ABOUT}" in body
    # the fabricated price and the invented citation are absent
    assert "$999" not in body
    assert _GHOST_URL not in body


# ---- 7. landing: pending, never auto-approved ----------------------------------

def test_facts_land_pending_with_the_page_url_as_the_citation(monkeypatch):
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True and out["landed"] == 2
    rows = cs.all_sources("gymx_ig")
    assert len(rows) == 2
    assert {r.status for r in rows} == {"pending"}
    assert {r.citation for r in rows} == {_HOME, _ABOUT}
    # the drafting path (approved only) still sees nothing
    assert cs.approved_sources("gymx_ig") == []


def test_intake_auto_approve_never_reaches_a_scrape(monkeypatch):
    """A scrape is not the gym handing us its own material: AGENT_INTAKE_AUTO_APPROVE
    must not auto-trust it."""
    monkeypatch.setenv("AGENT_INTAKE_AUTO_APPROVE", "true")
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True
    assert {r.status for r in cs.all_sources("gymx_ig")} == {"pending"}
    assert cs.approved_sources("gymx_ig") == []


# ---- 8. never overwrite human work ---------------------------------------------

def test_an_existing_bible_is_never_overwritten(monkeypatch, tmp_path):
    from agent import config
    voice_dir = os.path.join(config.client_voice_dir(), "gymx")
    os.makedirs(voice_dir, exist_ok=True)
    path = os.path.join(voice_dir, "lasso_voice.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("HUMAN REVIEWED BIBLE, DO NOT TOUCH")
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True
    assert open(path, encoding="utf-8").read() == "HUMAN REVIEWED BIBLE, DO NOT TOUCH"
    assert "untouched" in out["bible"]


def test_the_artifact_is_a_separate_per_gym_file_never_the_shared_brain(monkeypatch):
    out, _, _ = _build(monkeypatch)
    path = os.path.abspath(out["artifact"])
    assert path.endswith(os.path.join("deep_brains", "gymx.md"))
    assert "lasso-brain" not in path
    # the shared READ-ONLY corpus is named in the module docstring (to say it is
    # never touched) and NOWHERE in the code itself
    code = inspect.getsource(gdb).split('"""', 2)[-1]
    assert "lasso-brain" not in code and "lasso_brain" not in code
    # and NOT the tenant_brain learning log
    from agent import tenant_brain
    assert os.path.abspath(tenant_brain.brain_path("gymx")) != path


# ---- 9. blocked, never guessed --------------------------------------------------

def test_no_domain_on_record_blocks(monkeypatch):
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "true")
    site = FakeSite()
    out = gdb.build_deep_brain("nosuchgym_zz", handle="x", fetch=site,
                               llm=_fake_llm, apify=FakeApify())
    assert out["blocked"] is True and "domain" in out["reason"]
    assert site.requested == []
    assert not os.path.exists(gdb.deep_brain_path("nosuchgym_zz"))


def test_no_public_handle_on_record_blocks(monkeypatch):
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "true")
    site = FakeSite()
    out = gdb.build_deep_brain("nosuchgym_zz", domain=_DOM, fetch=site,
                               llm=_fake_llm, apify=FakeApify())
    assert out["blocked"] is True and "handle" in out["reason"]
    assert site.requested == []
    assert not os.path.exists(gdb.deep_brain_path("nosuchgym_zz"))


def test_a_failed_social_read_blocks_and_writes_nothing(monkeypatch):
    out, _, _ = _build(monkeypatch, apify=FakeApify(error="apify 429"))
    assert out["blocked"] is True and "social" in out["reason"]
    assert not os.path.exists(gdb.deep_brain_path("gymx"))
    assert cs.all_sources("gymx_ig") == []


def test_an_unreadable_site_blocks(monkeypatch):
    out, _, _ = _build(monkeypatch, site=FakeSite(pages={}))
    assert out["blocked"] is True
    assert "no readable public pages" in out["reason"]
    assert not os.path.exists(gdb.deep_brain_path("gymx"))
    assert cs.all_sources("gymx_ig") == []


def test_no_verifiable_fact_blocks(monkeypatch):
    out, _, _ = _build(monkeypatch, llm=lambda s, u: "no json here at all")
    assert out["blocked"] is True and "no verifiable facts" in out["reason"]
    assert not os.path.exists(gdb.deep_brain_path("gymx"))
    assert cs.all_sources("gymx_ig") == []


def test_a_dry_run_writes_nothing(monkeypatch):
    out, _, _ = _build(monkeypatch, dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True
    assert out["facts"] == 2
    assert not os.path.exists(gdb.deep_brain_path("gymx"))
    assert cs.all_sources("gymx_ig") == []


# ---- 10. social: FORM only, no comments ----------------------------------------

def test_social_profile_is_form_only_and_never_reads_comments(monkeypatch):
    obs, top, err = gdb.social_form_profile("gymx", apify=FakeApify(),
                                            today=_TODAY)
    assert err == ""
    metrics = {o.metric for o in obs}
    assert "posting cadence (posts/week)" in metrics
    assert "median caption length (chars)" in metrics
    assert "median opening line length (chars)" in metrics
    assert "posts using emoji (%)" in metrics
    assert "median hashtags per post" in metrics
    assert "posts carrying an ask (%)" in metrics
    assert all(o.basis for o in obs)
    blob = " ".join(f"{o.metric}{o.value}{o.basis}" for o in obs)
    assert "SECRETCOMMENTTOKEN" not in blob and "nosypete" not in blob
    assert "someMemberHandle" not in blob
    assert "latestComments" not in gdb._POST_FIELDS_READ
    assert "latestComments" not in inspect.getsource(gdb)


def test_top_posts_are_the_gyms_own_and_carry_permalinks(monkeypatch):
    _, top, err = gdb.social_form_profile("gymx", apify=FakeApify(), today=_TODAY)
    assert err == ""
    assert top and top[0].permalink == "https://www.instagram.com/p/AAA/"
    assert top[0].metric == "video plays" and top[0].value == 5200.0
    assert all(p.permalink for p in top)
    # the shortCode-only post still gets a real permalink
    assert any(p.permalink.endswith("/BBB/") for p in top)


def test_no_commenter_text_reaches_the_artifact(monkeypatch):
    out, _, _ = _build(monkeypatch)
    body = open(out["artifact"], encoding="utf-8").read()
    assert "SECRETCOMMENTTOKEN" not in body
    assert "nosypete" not in body
    assert "someMemberHandle" not in body
    assert "317 555 7777" not in body


def test_an_empty_public_feed_is_an_honest_empty_not_a_fabrication():
    obs, top, err = gdb.social_form_profile("gymx", apify=FakeApify(posts=[]),
                                            today=_TODAY)
    assert obs == [] and top == [] and err


def test_no_handle_is_an_honest_empty():
    obs, top, err = gdb.social_form_profile("", apify=FakeApify(), today=_TODAY)
    assert obs == [] and top == [] and "handle" in err


# ---- 11. public-only, no credentials --------------------------------------------

def test_the_module_never_builds_an_auth_header():
    src = inspect.getsource(gdb)
    for banned in ("Authorization", "Cookie", "cookies=", "auth=", "login",
                   "password", "sessionid"):
        assert banned not in src, f"{banned!r} has no place in a public scrape"


# ---- 12. CLI --------------------------------------------------------------------

def test_cli_subcommand_is_registered_and_dispatches(monkeypatch, capsys):
    from agent import __main__ as m
    names = [n for group in m._COMMANDS.values() for n, _ in group]
    assert any(n.startswith("gym-deep-brain") for n in names)

    seen = {}

    def _fake_build(base, **kw):
        seen.update({"base": base, **kw})
        return {"ok": True, "base": base, "domain": _DOM, "handle": "gymx",
                "artifact": "/tmp/x.md", "facts": 2, "voice": 8, "top_posts": 3,
                "landed": 2, "bible": "bible written", "notes": [],
                "dry_run": False}

    monkeypatch.setattr(gdb, "build_deep_brain", _fake_build)
    m.main(["gym-deep-brain", "--account", "gymx", "--domain", _DOM,
            "--handle", "gymx"])
    assert seen["base"] == "gymx" and seen["domain"] == _DOM
    assert seen["handle"] == "gymx" and seen["dry_run"] is False
    out = capsys.readouterr().out
    assert "deep brain from gymx.com" in out
    assert "PENDING" in out and "approve-sources" in out


def test_cli_reports_a_block_and_names_no_artifact(monkeypatch, capsys):
    from agent import __main__ as m
    monkeypatch.setattr(gdb, "build_deep_brain",
                        lambda base, **kw: {"ok": False, "blocked": True,
                                            "base": base, "reason": "no domain"})
    m.main(["gym-deep-brain", "--account", "gymx"])
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "no artifact" in out and "no domain" in out


def test_cli_without_an_account_prints_usage(capsys):
    from agent import __main__ as m
    m.main(["gym-deep-brain"])
    assert "usage: python -m agent gym-deep-brain" in capsys.readouterr().out


# ---- 13. reuse, not a rebuild ---------------------------------------------------

def test_it_reuses_the_existing_scraper_and_apify_paths():
    src = inspect.getsource(gdb)
    assert "website_intake._strip_html" in src
    assert "website_intake._digits_cleared" in src
    assert "website_intake.extract_sources" in src
    assert "website_intake._write_bible_if_missing" in src
    assert "social_baseline.ApifyClient" in src
    assert gdb._UA is wi._UA
    # no new dependency crept in
    assert "bs4" not in src and "BeautifulSoup" not in src and "httpx" not in src
