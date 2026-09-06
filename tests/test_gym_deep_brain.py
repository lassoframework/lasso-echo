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
  - A SCRAPE NEVER AUTHORS THE BRAND BIBLE (the 2026-09-06 CRITICAL): a build
    leaves the active voice slot empty, never calls the bible writer, and writes
    exactly ONE file (the artifact) across the whole data tree;

NOTE ON TEST STYLE (D68, and the 2026-09-06 finding). Five checks here used to be
`assert "<some string>" in inspect.getsource(gdb)` — including the one nominally
covering the CRITICAL, which asserted only that the bible writer was CALLED and
nothing at all about whether that was safe. A test that greps source text is not
a test: it passes for any rewrite that keeps the substring and fails for any that
does not, regardless of behaviour. All five are now behavioural (the post fields
actually read are recorded by watched dicts, the headers actually sent are
recorded by a permissive fake requests.get, the files actually written are
diffed, reuse is proved by calling), and each was mutation-checked with
__pycache__ cleared. The one remaining use of inspect.getsource parses the module
into an AST to enumerate real imports, which is a structural fact, not a string.
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


def test_one_huge_response_is_bounded_by_the_per_fetch_cap(monkeypatch):
    """The cumulative MAX_BYTES budget is only consulted BEFORE a request, so a
    single 5MB page used to sail past it in full. The per-fetch ceiling bounds ONE
    response: whatever the fetcher hands back is cut at the line, the URL is
    recorded, and the cumulative budget stays honest."""
    huge = _html("x " * 3_000_000)                 # ~6MB from one page
    assert len(huge) > 5_000_000
    site = FakeSite(pages={_HOME: None})
    site.pages = {}

    def _fetch(url):
        site.requested.append(url)
        if url.endswith("/robots.txt"):
            return _ROBOTS_OPEN
        return huge if url == _HOME else None

    clock = FakeClock()
    crawl = gdb.crawl_site(_DOM, ("/",), fetch=_fetch, sleep=clock.sleep,
                           clock=clock, max_page_bytes=1000)
    assert crawl.truncated == [_HOME]
    assert crawl.bytes_used <= 1000, "a single response blew the byte budget"
    assert len(crawl.pages[_HOME]) <= 1000


def test_the_real_fetcher_stops_reading_a_huge_body_at_the_cap():
    """And it stops at the WIRE, not after the download: the streaming fetcher
    abandons the response mid-body, so the 5MB is never pulled down."""
    huge = "y" * 200_000
    get = _RecordingGet(body=huge)
    fetcher = gdb.CappedFetcher(get=get, max_bytes=2048)
    body = fetcher("https://gymx.com/")
    assert len(body) <= 2048
    assert fetcher.truncated == ["https://gymx.com/"]
    (_url, kwargs), = get.calls
    assert kwargs.get("stream") is True, "the body was not streamed"


def test_robots_5xx_denies_the_whole_host(monkeypatch):
    """A server error is NOT permission. robots.txt answering 503 used to be
    indistinguishable from 404 ('no rules stated') and silently opened the host."""
    policy = gdb.RobotsPolicy(fetch=lambda url: (503, ""))
    assert policy.allowed(_HOME) is False
    assert policy.allowed(_ABOUT) is False
    assert policy.denied_hosts and "503" in policy.denied_hosts[0]


def test_an_unreachable_robots_txt_denies_too():
    """Undetermined is not yes: a fetcher that cannot answer at all fails closed."""
    def _boom(url):
        raise OSError("connection reset")

    assert gdb.RobotsPolicy(fetch=_boom).allowed(_HOME) is False
    assert gdb.RobotsPolicy(fetch=lambda url: (None, None)).allowed(_HOME) is False


def test_robots_404_still_means_no_restriction_stated():
    """The other side of the rule, so failing closed does not become failing shut:
    an explicit 4xx is a host stating no restriction, which stays a crawl."""
    assert gdb.RobotsPolicy(fetch=lambda url: (404, "")).allowed(_HOME) is True
    assert gdb.RobotsPolicy(fetch=lambda url: _ROBOTS_OPEN).allowed(_HOME) is True


def test_a_robots_5xx_blocks_the_whole_build(monkeypatch):
    """End to end: a gym whose robots.txt is erroring produces NO artifact and NO
    rows, and nothing but robots.txt is ever requested."""
    class _Erroring(FakeSite):
        def __call__(self, url):
            self.requested.append(url)
            if url.endswith("/robots.txt"):
                return (500, "")
            return _html(self.pages.get(url) or "")

    out, site, _ = _build(monkeypatch, site=_Erroring())
    assert out["ok"] is False and out["blocked"] is True
    assert "robots" in out["reason"].lower()
    assert site.requested == [f"https://{_DOM}/robots.txt"]
    assert not os.path.exists(gdb.deep_brain_path("gymx"))
    assert cs.all_sources("gymx_ig") == []


def test_a_cross_host_redirect_is_re_checked_against_robots():
    """We checked gymx.com's robots.txt; a 301 to another host lands us somewhere
    whose rules we never read. The final URL is re-checked and the body DISCARDED
    when that host disallows us."""
    # a robots policy that says yes to gymx.com and no to the redirect target
    policy = gdb.RobotsPolicy(fetch=lambda url: (
        _ROBOTS_NO_EVERYTHING if "elsewhere.example" in url else _ROBOTS_OPEN))
    get = _RecordingGet(body=_html("secret content"),
                        url="https://elsewhere.example/landing")
    fetcher = gdb.CappedFetcher(policy, get=get)
    assert fetcher("https://gymx.com/about") is None
    assert fetcher.blocked_redirects and "elsewhere.example" in \
        fetcher.blocked_redirects[0]


def test_a_cross_host_redirect_that_robots_allows_still_reads():
    """The re-check is a gate, not a ban on redirects: an allowing host reads."""
    policy = gdb.RobotsPolicy(fetch=lambda url: _ROBOTS_OPEN)
    get = _RecordingGet(body=_html("moved but public"),
                        url="https://www2.gymx.com/about")
    fetcher = gdb.CappedFetcher(policy, get=get)
    body = fetcher("https://gymx.com/about")
    assert body and "moved but public" in body
    assert fetcher.blocked_redirects == []


def test_a_same_host_redirect_needs_no_re_check():
    """http -> https or /about -> /about/ on the SAME host keeps the decision we
    already made; no second robots lookup is forced."""
    policy = gdb.RobotsPolicy(fetch=lambda url: _ROBOTS_OPEN)
    get = _RecordingGet(body=_html("same host"), url="https://gymx.com/about/")
    fetcher = gdb.CappedFetcher(policy, get=get)
    assert "same host" in fetcher("https://gymx.com/about")
    assert fetcher.blocked_redirects == []


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


# ---- 8. a scrape never authors the voice doc (the 2026-09-06 CRITICAL) ---------

def _voice_slot(base="gymx"):
    """The ACTIVE voice-doc path the drafter actually reads for this gym."""
    from agent.client_media_sync import _resolve_client_voice_path
    return _resolve_client_voice_path(base, os.path.join("brand_voice", base,
                                                         "lasso_voice.md"))


def test_a_deep_brain_never_writes_the_brand_bible(monkeypatch):
    """THE CRITICAL. A pre-onboarding scrape must not author the gym's voice doc.

    It used to: build_deep_brain handed its scraped bundle to
    website_intake._write_bible_if_missing, which rendered those unapproved facts
    into <DATA_DIR>/brand_voice/<base>/lasso_voice.md — the slot
    _resolve_client_voice_path prefers over everything — with the citations
    stripped. voice.load_voice reads that file and drafter puts voice.raw straight
    into the LLM prompt as APPROVED material.

    So this asserts the OUTCOME, not the absence of a line: after a full,
    successful build, the active voice slot is still empty and load_voice still
    says 'no voice doc, do not draft'."""
    slot = _voice_slot()
    assert not os.path.exists(slot)
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True and out["landed"] == 2   # the build really ran
    assert not os.path.exists(slot), "a scrape wrote the gym's active voice doc"
    from agent import voice as vmod
    assert vmod.load_voice(slot) is None              # no voice doc -> no draft
    assert "not written" in out["bible"]


def test_the_scraped_bible_writer_is_never_called_by_a_deep_brain(monkeypatch):
    """The same rule from the other side, so an equivalent rewrite is caught too:
    the bible writer BLOWS UP if a deep brain reaches it, and a full build still
    succeeds — meaning nothing on the path touched it."""
    called = []

    def _explode(*a, **kw):
        called.append((a, kw))
        raise AssertionError("a scrape must never write a brand bible")

    monkeypatch.setattr(wi, "_write_bible_if_missing", _explode)
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True and out["facts"] == 2
    assert called == []


def test_a_scraped_figure_never_becomes_approved_prompt_material(monkeypatch):
    """END TO END. After a real build, every route by which a scraped figure could
    reach the drafter as APPROVED material is closed: not an approved source row,
    not an active voice doc. The figure lives only in the pending rows and the
    derived artifact, both of which a human reads before anything is drafted."""
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True
    assert cs.approved_sources("gymx_ig") == []
    rows = cs.all_sources("gymx_ig")
    assert rows and {r.status for r in rows} == {"pending"}
    # "2018" is a real scraped figure (from _GOOD_ABOUT). It is in the pending
    # material and NOWHERE the drafter reads as approved.
    assert any("2018" in r.text for r in rows)
    from agent import voice as vmod
    assert vmod.load_voice(_voice_slot()) is None


def test_a_human_bible_is_untouched_and_stays_human(monkeypatch):
    """A gym that already has a HUMAN bible keeps it byte for byte, and it stays
    APPROVED: nothing here stamps an existing doc auto-drafted."""
    from agent import config, voice as vmod
    voice_dir = os.path.join(config.client_voice_dir(), "gymx")
    os.makedirs(voice_dir, exist_ok=True)
    path = os.path.join(voice_dir, "lasso_voice.md")
    human = "# Gym X Brand Bible\nHUMAN REVIEWED, DO NOT TOUCH. We opened in 2015."
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(human)
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True
    assert open(path, encoding="utf-8").read() == human
    doc = vmod.load_voice(path)
    assert doc is not None and doc.auto_drafted is False


def _tree(root):
    """{path: (size, mtime)} for every file under root — a snapshot to diff."""
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def test_a_build_writes_exactly_one_file_and_that_is_the_artifact(monkeypatch,
                                                                  tmp_path):
    """The behavioral form of 'a separate per-gym file, never the shared brain'.

    Snapshot the whole data tree (decoys included: a stand-in shared lasso-brain
    corpus, a tenant_brain learning log, an existing bible), run a full build, and
    diff. Exactly ONE file may appear — the per-gym artifact — and no file that
    existed before may change. That covers the READ-ONLY corpus, the learning log
    and the brand bible in one assertion, without grepping a line of source."""
    from agent import config, tenant_brain
    monkeypatch.setenv("AGENT_TENANT_BRAIN_DIR", str(tmp_path / "brains"))
    decoys = {
        os.path.join(str(tmp_path), "lasso-brain", "book-doctrine.md"):
            "SHARED READ-ONLY CORPUS",
        os.path.join(config.client_voice_dir(), "gymx", "lasso_voice.md"):
            "HUMAN BIBLE",
        tenant_brain.brain_path("gymx"): "## learning log\n",
    }
    for path, body in decoys.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)

    db_path = os.environ["AGENT_DB_PATH"]

    def _snap():
        return {p: v for p, v in _tree(str(tmp_path)).items()
                if not p.startswith(db_path)}   # sqlite + its -wal/-shm churn

    before = _snap()
    out, _, _ = _build(monkeypatch)
    assert out["ok"] is True
    after = _snap()

    created = set(after) - set(before)
    changed = {p for p in set(before) & set(after) if before[p] != after[p]}
    artifact = os.path.abspath(out["artifact"])
    assert {os.path.abspath(p) for p in created} == {artifact}
    assert changed == set(), f"a build modified existing files: {changed}"
    assert artifact.endswith(os.path.join("deep_brains", "gymx.md"))
    assert os.path.abspath(tenant_brain.brain_path("gymx")) != artifact


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


def test_the_social_read_touches_no_field_outside_the_declared_set():
    """_POST_FIELDS_READ is a PROMISE. This measures it: every post handed to
    social_form_profile records which keys anyone asked it for, and the union of
    those reads must be a subset of the declared set. A future line that reaches
    for latestComments, taggedUsers or any other user-generated field fails here,
    which grepping the source for one field name could never do."""

    class _WatchedPost(dict):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.read = set()

        def get(self, key, default=None):
            self.read.add(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self.read.add(key)
            return super().__getitem__(key)

    watched = [_WatchedPost(p) for p in _POSTS]
    obs, top, err = gdb.social_form_profile("gymx", apify=FakeApify(posts=watched),
                                            today=_TODAY)
    assert err == "" and obs and top          # the read really happened
    touched = set().union(*(p.read for p in watched))
    assert touched, "nothing was read at all; the assertion below would be vacuous"
    extra = touched - set(gdb._POST_FIELDS_READ)
    assert extra == set(), f"read undeclared post field(s): {sorted(extra)}"


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

class _RecordingGet:
    """A stand-in for requests.get that records exactly how it was called and
    hands back a permissive response: 200, no redirect, a small body. It enforces
    NOTHING itself, so only the module under test can hold the no-credentials line
    (a fake that refused auth headers would pass even with the guard reverted)."""

    def __init__(self, body="<html><body>hi</body></html>", status=200, url=None):
        self.body = body
        self.status = status
        self._url = url
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(self.body, status=self.status,
                             url=self._url or url)


class _FakeResponse:
    def __init__(self, body, status=200, url="", chunk=64):
        self.text = body
        self.status_code = status
        self.url = url
        self.encoding = "utf-8"
        self.closed = False
        self._chunk = chunk

    def iter_content(self, chunk_size=1024):
        raw = self.text.encode("utf-8")
        step = max(1, int(chunk_size or self._chunk))
        for i in range(0, len(raw), step):
            yield raw[i:i + step]

    def close(self):
        self.closed = True


def test_the_real_page_fetcher_sends_nothing_but_a_user_agent():
    """Measured at the wire, not grepped: drive the PRODUCTION fetcher with a
    recording requests.get and inspect what it actually sent."""
    get = _RecordingGet()
    body = gdb.CappedFetcher(get=get)("https://gymx.com/about")
    assert body and "hi" in body               # the fetch really happened
    (url, kwargs), = get.calls
    assert url == "https://gymx.com/about"
    assert set(kwargs["headers"]) == {"User-Agent"}
    assert kwargs["headers"]["User-Agent"] == gdb._UA
    for banned in ("cookies", "auth", "cert", "data", "json"):
        assert banned not in kwargs, f"{banned!r} has no place in a public scrape"


def test_the_real_robots_fetcher_sends_nothing_but_a_user_agent():
    get = _RecordingGet(body="User-agent: *\nAllow: /\n")
    status, body = gdb._fetch_robots("https://gymx.com/robots.txt", get=get)
    assert status == 200 and "Allow" in body
    (url, kwargs), = get.calls
    assert url == "https://gymx.com/robots.txt"
    assert set(kwargs["headers"]) == {"User-Agent"}
    for banned in ("cookies", "auth", "cert", "data", "json"):
        assert banned not in kwargs


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

def test_it_reuses_the_existing_scraper_and_apify_paths(monkeypatch):
    """Reuse proven by CALLING, not by grepping: swap each shared helper for a
    recorder and watch a real build go through it."""
    seen = []
    real_extract, real_strip = wi.extract_sources, wi._strip_html
    real_digits = wi._digits_cleared

    def _extract(*a, **kw):
        seen.append("extract_sources")
        return real_extract(*a, **kw)

    def _strip(*a, **kw):
        seen.append("_strip_html")
        return real_strip(*a, **kw)

    def _digits(*a, **kw):
        seen.append("_digits_cleared")
        return real_digits(*a, **kw)

    monkeypatch.setattr(wi, "extract_sources", _extract)
    monkeypatch.setattr(wi, "_strip_html", _strip)
    monkeypatch.setattr(wi, "_digits_cleared", _digits)
    apify = FakeApify()
    out, _, _ = _build(monkeypatch, apify=apify)
    assert out["ok"] is True
    assert {"extract_sources", "_strip_html", "_digits_cleared"} <= set(seen)
    assert apify.calls, "the existing Apify client path was not used"
    assert gdb._UA is wi._UA


def test_the_deep_brain_pulled_in_no_new_dependency():
    """Every module name gym_deep_brain imports, read off its AST (module level
    and inside functions alike). A heavyweight scraper or a second HTTP client
    would show up here as a real import, not as a lucky substring."""
    import ast
    tree = ast.parse(inspect.getsource(gdb))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.name.split(".")[0] for a in node.names)
    for banned in ("bs4", "BeautifulSoup", "httpx", "selenium", "playwright",
                   "scrapy", "lxml"):
        assert banned not in imported, f"{banned} is a new dependency"
    # and it really does sit on the shared plumbing
    assert {"website_intake", "client_sources", "social_baseline"} <= imported
