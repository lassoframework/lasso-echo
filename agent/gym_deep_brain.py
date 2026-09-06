"""
gym_deep_brain.py — the PER-GYM DEEP BRAIN: read a prospective gym's OWN website
and its OWN public social feed BEFORE onboarding, and compile a per-gym artifact
the caption generator can voice-match and ground against from day one.

Blake (2026-09-06): "create a deeper brain for each gym scraping their website
and all their social media prior to starting with us."

WHAT THIS IS NOT
  - NOT an edit to ~/LASSO/lasso-brain. That corpus is READ-ONLY shared LASSO
    doctrine; agents compile FROM it and never write to it. Nothing in this
    module opens a path under it. The deep brain is a SEPARATE per-gym file at
    config.gym_deep_brain_dir()/<base>.md (<DATA_DIR>/deep_brains).
  - NOT tenant_brain.py. That is the append-only per-gym LEARNING log
    (<DATA_DIR>/brains/<tenant>.md, "## <ts> <kind> <json>" lines) fed by human
    approvals and edits. Different format, different lifecycle, different dir.
  - NOT an approval path. Scraped facts land PENDING, full stop.

THE TWO KINDS OF THING IN THE ARTIFACT, and why they are different types
  1. GroundedFact  — a CONTENT claim about the gym (a service, an offer, an FAQ
     answer, who they are). It is a claim about the gym ONLY if it came from the
     gym's own published material, so GroundedFact REQUIRES a source_url and
     refuses to construct without one: an unattributed claim is not representable
     in this module's data model. On top of that, DeepBrain.add_fact refuses any
     fact whose citation is not a page we actually fetched, and re-runs
     website_intake._digits_cleared against that page's STORED (PII-scrubbed)
     text, so no figure lands that the source page does not itself state.
  2. VoiceObservation — an observation about FORM (caption length, hook shape,
     emoji and hashtag habits, posting cadence, whether they ask). Form is
     measured, not claimed, so a VoiceObservation carries a `basis` (what was
     measured over what sample) instead of a source URL. Voice observations are
     never landed as client_sources rows and are never quotable as fact.

SCRAPING LAW (all enforced in code, all tested)
  - PUBLIC pages only. No credentialed, logged-in, or private fetch anywhere in
    this module: the only header sent is User-Agent, and no auth/cookie header is
    ever constructed. The social side goes through the same public Apify actor
    social_baseline already uses.
  - robots.txt is RESPECTED, and FAILS CLOSED. Per host, /robots.txt is fetched
    once and cached; a URL its rules disallow for the UA we send is SKIPPED and
    recorded as skipped, never requested. A 5xx or an unreachable robots.txt
    DENIES the whole host (a server error is not permission); only an explicit
    4xx means "this host states no restriction". Crawl-delay is honored when it
    is larger than our own floor. A redirect that lands on a DIFFERENT host is
    re-checked against that host's robots.txt and the body discarded if it
    disallows us.
  - RATE LIMITED: at least config.gym_deep_brain_crawl_delay() seconds between
    two requests to the same host (default 2.0), and at least robots' own
    Crawl-delay when that is larger. The sleep is injectable so tests never sleep.
  - CAPPED TWICE: MAX_PAGES pages and MAX_BYTES bytes per gym (cumulative), so a
    scrape cannot run away on a site that serves a page for every path, AND
    MAX_PAGE_BYTES for ONE response, streamed and abandoned at the line so a
    single huge page is never pulled down in full.
  - NO PII beyond what the business publishes about ITSELF. Coach names as the
    gym itself publishes them are business information and are kept. Emails and
    phone numbers that are NOT the business's own published contact details are
    REDACTED before anything is stored (scrub_pii), and social captions are
    scrubbed with an EMPTY allowlist because a caption's phone number could
    belong to anyone. Comments and commenter identities are never read: the post
    fields this module touches are enumerated in _POST_FIELDS_READ.

MISSING DATA BLOCKS, IT NEVER GUESSES. No domain on record, no public social
handle, robots disallowing everything, a failed fetch, or an Apify error each
produce an honest {"ok": False, "blocked": True, "reason": ...} and NO artifact
file, NO client_sources row, NO bible. Nothing is synthesized to fill a hole.

A SCRAPE NEVER AUTHORS THE BRAND BIBLE (finding 2026-09-06, CRITICAL). This
module used to hand its scraped bundle to website_intake._write_bible_if_missing,
which rendered those facts into <DATA_DIR>/brand_voice/<base>/lasso_voice.md —
the ACTIVE voice-doc slot client_media_sync._resolve_client_voice_path prefers
over everything else — with the citations stripped. voice.load_voice reads that
file, drafter puts voice.raw straight into the LLM prompt, and
drafter._output_claims_cleared treats the voice doc as an APPROVED source, so a
scraped, unapproved figure cleared the exact gate that exists to block invented
figures. That call is GONE. The build writes the derived artifact and PENDING
client_sources rows and nothing else; a human writes or activates the bible, and
the human bible-drafting path (intake_onboard -> bible_drafter, which holds its
output under brand_voice/drafts/ for approval) is untouched.

NEVER OVERWRITES HUMAN WORK. Only the derived deep-brain artifact itself is
written here, and a re-run regenerates that one file. No voice doc, no brand
bible, no approved source row is ever written by this module.

Behind AGENT_GYM_DEEP_BRAIN (config.gym_deep_brain_enabled, default OFF: every
entry point returns blocked, nothing is fetched, nothing is written). Manual
per-gym run:

    python -m agent gym-deep-brain --account <base> [--domain x.com]
                                   [--handle <ig>] [--dry-run]

Everything is injectable (fetch / llm / apify / sleep / clock) so the whole lane
is unit-testable offline with no network, no Anthropic key, and no Apify token.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import client_sources, config, db, website_intake

# The SAME browser-ish UA website_intake / website_scan send. robots.txt is
# evaluated against this exact string (and its product token, see _UA_TOKENS).
_UA = website_intake._UA

# robotparser matches a user-agent line by lowercasing everything before the
# first "/" of the agent string, so the full UA above reduces to "mozilla" and a
# rule written for our product token would silently not apply. Check BOTH and
# take the most restrictive answer, so a site that names LASSO-Echo is obeyed.
_UA_TOKENS = (_UA, "LASSO-Echo")

# Pages worth reading on a gym site. Same starting set as website_intake plus the
# two that most often carry schedule and coach material.
DEFAULT_PATHS = ("/", "/about", "/services", "/programs", "/pricing", "/faq",
                 "/schedule", "/contact")

# Runaway guards. A boutique gym site is a handful of pages; anything past this
# is a path-explosion and gets cut off with a note, never followed.
MAX_PAGES = 12
MAX_BYTES = 2_000_000

# PER-FETCH ceiling (finding 2026-09-06). MAX_BYTES alone is CUMULATIVE and is
# only consulted BEFORE a request, so a single 5MB response downloaded in full
# before the budget noticed. MAX_PAGE_BYTES bounds ONE response: the default
# fetcher streams and abandons the body the moment it crosses this line, and
# crawl_site truncates whatever any fetcher hands back to the same ceiling. A
# gym marketing page is tens of KB; half a megabyte is already generous.
MAX_PAGE_BYTES = 500_000

# robots.txt is a small text file. A host that answers with megabytes is not
# stating rules, and we stop reading at this line.
ROBOTS_MAX_BYTES = 100_000

_CHUNK = 8192

# How far back the social read looks. Six months is enough to see cadence and
# form without paying for a whole feed.
SOCIAL_WINDOW_DAYS = 180
SOCIAL_RESULTS_LIMIT = 200

# The ONLY post fields this module reads. Comment bodies, commenter usernames,
# tagged users and any other user-generated field are deliberately absent: a
# gym's own caption is the gym's own material, a commenter's words are not.
_POST_FIELDS_READ = ("timestamp", "caption", "type", "productType", "isPinned",
                     "videoPlayCount", "likesCount", "url", "shortCode")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Ten-digit North American shapes with optional country code, bounded so a long
# digit run (an order number, a timestamp) is not mistaken for a phone.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")

_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")

# Emoji-ish codepoint ranges, enough to tell "this gym uses emoji" from "this
# gym does not". Deliberately a range check and not a new dependency.
_EMOJI_RANGES = ((0x1F300, 0x1FAFF), (0x1F000, 0x1F2FF), (0x2600, 0x27BF),
                 (0x2190, 0x21FF), (0xFE00, 0xFE0F), (0x2B00, 0x2BFF))

EMAIL_REDACTION = "[email redacted]"
PHONE_REDACTION = "[phone redacted]"


# ---- 1. robots.txt ------------------------------------------------------------

# What a host says when it will not tell us its rules. RFC 9309 §2.3.1.4: an
# "unavailable for legal reasons" or server-error response means the crawler MUST
# assume complete disallow. This is the parsed form of that assumption.
_DENY_ALL_LINES = ["User-agent: *", "Disallow: /"]


def _robots_result(value):
    """Normalize whatever a robots fetcher returned into (status, body).

    Three shapes are accepted so the same policy object works in production and
    under an injected test fetcher:
      (status, body)  -> used as is (this is what _fetch_robots returns);
      a string        -> a body that was read, i.e. HTTP 200;
      None            -> nothing readable came back, status unknown.
    """
    if isinstance(value, tuple) and len(value) == 2:
        status, body = value
        try:
            status = None if status is None else int(status)
        except (TypeError, ValueError):
            status = None
        return status, body
    if value is None:
        return None, None
    return 200, value


def _fetch_robots(url, *, get=None, max_bytes=ROBOTS_MAX_BYTES):
    """(status, body) for a robots.txt. The STATUS is the point: the old fetcher
    collapsed every non-200 into None, so a 503 was indistinguishable from a 404
    and both meant "no rules stated" — i.e. a host having a bad five minutes
    silently granted us permission to crawl it. Returns (None, None) when the
    request could not be made at all, which the policy also treats as a denial."""
    if get is None:
        import requests
        get = requests.get
    try:
        r = get(url, timeout=15, headers={"User-Agent": _UA},
                allow_redirects=True)
    except Exception:  # noqa: BLE001 - unreachable is not permission either
        return None, None
    try:
        status = int(getattr(r, "status_code", 200) or 200)
    except (TypeError, ValueError):
        status = 200
    body = ""
    if status < 400:
        try:
            body = str(getattr(r, "text", "") or "")[:max_bytes]
        except Exception:  # noqa: BLE001
            body = ""
    return status, body


class RobotsPolicy:
    """Per-host robots.txt, fetched once and cached, on stdlib
    urllib.robotparser (no new dependency).

    allowed(url) is the gate every fetch passes through. A disallowed URL is
    never requested. Crawl-delay, when the host states one, is returned by
    crawl_delay(url) and the caller takes the LARGER of it and our own floor.

    HOW EACH ANSWER IS READ (finding 2026-09-06):
      * 2xx/3xx      -> parse the body; the host stated its rules.
      * 4xx (404 …)  -> the host states NO restriction, which per RFC 9309 is
                        unrestricted crawling. We do not invent a rule it did
                        not write.
      * 5xx          -> DENY EVERYTHING. A server error is not permission. This
                        used to read as "no rules", so a host erroring out
                        silently opened itself to the crawler.
      * unreachable  -> DENY EVERYTHING, for the same reason: we could not
                        determine the rules, and undetermined is not yes.
    Nothing else in the module can override a denial: allowed() is consulted
    before the fetch callable ever sees a URL."""

    def __init__(self, fetch=None):
        self._fetch = fetch or _fetch_robots
        self._cache = {}
        self.fetched_hosts = []
        self.denied_hosts = []   # hosts we failed CLOSED on, with the reason

    def _parser(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        if host in self._cache:
            return self._cache[host]
        scheme = urllib.parse.urlsplit(url).scheme or "https"
        rp = urllib.robotparser.RobotFileParser()
        raw = None
        try:
            raw = self._fetch(f"{scheme}://{host}/robots.txt")
        except Exception:  # noqa: BLE001 - a raising fetcher is not permission
            raw = (None, None)
        status, body = _robots_result(raw)
        deny = status is None or status >= 500
        try:
            rp.parse(_DENY_ALL_LINES if deny else str(body or "").splitlines())
        except Exception:  # noqa: BLE001 - malformed robots.txt states nothing
            rp.parse([] if not deny else _DENY_ALL_LINES)
        if deny:
            self.denied_hosts.append(
                f"{host}: robots.txt answered {status if status else 'nothing'}; "
                "failing closed, nothing on this host is crawled")
        self._cache[host] = rp
        self.fetched_hosts.append(host)
        return rp

    def allowed(self, url):
        """True when EVERY user-agent token we send is allowed this URL. Most
        restrictive wins, so a `Disallow` aimed at either the full UA string or
        at the LASSO-Echo product token keeps us off the page."""
        rp = self._parser(url)
        for token in _UA_TOKENS:
            try:
                if not rp.can_fetch(token, url):
                    return False
            except Exception:  # noqa: BLE001 - a parser blow-up is not permission
                return False
        return True

    def crawl_delay(self, url):
        """The host's stated Crawl-delay in seconds, or None. The largest value
        across our UA tokens wins (the site's strictest statement about us)."""
        rp = self._parser(url)
        best = None
        for token in _UA_TOKENS:
            try:
                d = rp.crawl_delay(token)
            except Exception:  # noqa: BLE001
                d = None
            if d is None:
                continue
            try:
                d = float(d)
            except (TypeError, ValueError):
                continue
            best = d if best is None else max(best, d)
        return best


# ---- 1b. the page fetcher: per-fetch cap + redirect re-check -------------------

class CappedFetcher:
    """The deep brain's own page fetcher. Two things website_intake's plain
    requests.get could not do, both from the 2026-09-06 finding:

    1. PER-FETCH BYTE CAP. The body is STREAMED and abandoned the moment it
       crosses max_bytes, so a 5MB response is never pulled down in full. The
       old cumulative MAX_BYTES check ran only BEFORE a request, so the first
       oversized page was always downloaded whole before anything noticed.

    2. ROBOTS RE-CHECK AFTER A CROSS-HOST REDIRECT. We ask https://gymx.com/about
       having checked gymx.com's robots.txt; a 301 to blog.othersite.com lands us
       on a host whose rules we never read. When the final URL's host differs from
       the one we asked for, this re-runs the robots gate against the FINAL url
       and DISCARDS the body if that host disallows it.

    `get` is injectable so the whole thing is unit-testable with no network."""

    def __init__(self, robots=None, *, max_bytes=MAX_PAGE_BYTES, get=None):
        self.robots = robots
        self.max_bytes = int(max_bytes)
        self._get = get
        self.blocked_redirects = []   # final URLs dropped by the robots re-check
        self.truncated = []           # URLs cut off at the per-fetch cap

    def _requests_get(self):
        if self._get is not None:
            return self._get
        import requests
        return requests.get

    def __call__(self, url):
        try:
            resp = self._requests_get()(
                url, timeout=15, headers={"User-Agent": _UA},
                allow_redirects=True, stream=True)
        except Exception:  # noqa: BLE001 - a failed page is not a crash
            return None
        try:
            try:
                if int(getattr(resp, "status_code", 200) or 200) >= 400:
                    return None
            except (TypeError, ValueError):
                pass
            final = str(getattr(resp, "url", "") or url)
            if not self._redirect_allowed(url, final):
                return None
            return self._read_capped(url, resp)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _redirect_allowed(self, asked, final):
        """False when a redirect took us to a DIFFERENT host that robots.txt
        disallows. Same host (or no policy to ask) keeps the original decision."""
        asked_host = urllib.parse.urlsplit(asked).netloc.lower()
        final_host = urllib.parse.urlsplit(final).netloc.lower()
        if not final_host or final_host == asked_host:
            return True
        if self.robots is None:
            return True
        try:
            ok = self.robots.allowed(final)
        except Exception:  # noqa: BLE001 - an unreadable policy is not permission
            ok = False
        if not ok:
            self.blocked_redirects.append(
                f"{asked} redirected to {final}; robots.txt on {final_host} "
                "disallows it, so the response was discarded unread")
        return ok

    def _read_capped(self, url, resp):
        buf = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                buf.extend(chunk)
                if len(buf) >= self.max_bytes:
                    self.truncated.append(url)
                    break
        except Exception:  # noqa: BLE001 - keep whatever arrived before the break
            pass
        encoding = getattr(resp, "encoding", None) or "utf-8"
        try:
            return bytes(buf[:self.max_bytes]).decode(encoding, "replace")
        except Exception:  # noqa: BLE001 - an unknown encoding is not a crash
            return bytes(buf[:self.max_bytes]).decode("utf-8", "replace")


# ---- 2. rate limit ------------------------------------------------------------

class HostRateLimiter:
    """A minimum gap between two requests to the SAME host. `sleep` and `clock`
    are injectable so tests assert the delay was taken without ever sleeping."""

    def __init__(self, sleep=None, clock=None):
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._last = {}

    def wait(self, host, delay):
        """Sleep however long is still owed before the next request to `host`.
        Returns the seconds actually slept (0.0 on the first request)."""
        try:
            delay = float(delay or 0.0)
        except (TypeError, ValueError):
            delay = 0.0
        waited = 0.0
        prev = self._last.get(host)
        if delay > 0 and prev is not None:
            owed = delay - (self._clock() - prev)
            if owed > 0:
                self._sleep(owed)
                waited = owed
        self._last[host] = self._clock()
        return waited


# ---- 3. PII scrubbing ---------------------------------------------------------

def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _registrable(domain):
    """The bare host of a domain, www stripped and scheme/path removed."""
    dom = re.sub(r"^https?://", "", str(domain or "").strip(), flags=re.I)
    dom = dom.split("/")[0].strip().lower()
    return dom[4:] if dom.startswith("www.") else dom


def business_contacts(pages, domain):
    """(emails, phone_digits) that are the BUSINESS's own published contact
    details, derived from the pages we fetched. Everything else is a person's
    contact detail and gets redacted.

    An email is the business's own when its domain is the gym's own domain (or a
    subdomain of it): hello@gymx.com on gymx.com is the gym publishing how to
    reach the gym. A gmail address in a testimonial, a member's work address, a
    vendor's address are not.

    A phone number is the business's own when the gym publishes it on its
    homepage or its contact page, which is exactly what "the business's own
    published contact details" means. A number that appears only in some other
    page's body text is not assumed to be the business's."""
    host = _registrable(domain)
    emails, phones = set(), set()
    for url, text in (pages or {}).items():
        for match in _EMAIL_RE.findall(text or ""):
            at = match.rsplit("@", 1)[-1].lower()
            if host and (at == host or at.endswith("." + host)):
                emails.add(match.lower())
        path = urllib.parse.urlsplit(url).path or "/"
        if path in ("", "/") or path.rstrip("/").lower().endswith("/contact"):
            for match in _PHONE_RE.findall(text or ""):
                d = _digits(match)
                if len(d) >= 10:
                    phones.add(d[-10:])
    return emails, phones


def scrub_pii(text, allow_emails=(), allow_phones=()):
    """`text` with every email and phone number that is NOT in the business's own
    published contact details replaced by a redaction marker.

    This runs BEFORE anything is stored, so a member's email, a commenter's
    number, or a personal mobile pasted into a page never reaches the artifact,
    the client_sources table, or a prompt. Coach and staff names are untouched:
    a gym publishing who coaches there is publishing business information, not a
    member's identity."""
    allow_emails = {str(e).lower() for e in (allow_emails or ())}
    allow_phones = {_digits(p)[-10:] for p in (allow_phones or ()) if _digits(p)}
    out = _EMAIL_RE.sub(
        lambda m: m.group(0) if m.group(0).lower() in allow_emails
        else EMAIL_REDACTION, str(text or ""))

    def _phone(m):
        d = _digits(m.group(0))
        return m.group(0) if d[-10:] in allow_phones else PHONE_REDACTION

    return _PHONE_RE.sub(_phone, out)


# ---- 4. the crawl -------------------------------------------------------------

@dataclass
class CrawlResult:
    domain: str
    pages: dict = field(default_factory=dict)      # url -> scrubbed visible text
    fetched: list = field(default_factory=list)    # urls actually requested
    skipped_robots: list = field(default_factory=list)
    skipped_cap: list = field(default_factory=list)
    skipped_redirect: list = field(default_factory=list)
    truncated: list = field(default_factory=list)
    bytes_used: int = 0
    crawl_delay: float = 0.0
    business_emails: set = field(default_factory=set)
    business_phones: set = field(default_factory=set)


def crawl_site(domain, paths=DEFAULT_PATHS, *, fetch=None, sleep=None, clock=None,
               robots=None, delay=None, max_pages=MAX_PAGES, max_bytes=MAX_BYTES,
               max_page_bytes=MAX_PAGE_BYTES):
    """Fetch up to max_pages PUBLIC pages of https://<domain>, obeying robots.txt
    and rate-limiting per host, and return a CrawlResult whose page text has
    already been PII-scrubbed.

    A URL robots disallows is recorded in skipped_robots and NEVER requested (the
    fetch callable does not see it). Requests to the same host are separated by
    at least max(config.gym_deep_brain_crawl_delay(), robots' Crawl-delay).
    Never raises: a page that fails is simply absent.

    BYTE CAPS, both of them (finding 2026-09-06). max_bytes is the CUMULATIVE
    budget for the gym and is checked before each request. max_page_bytes bounds
    ONE response: the production fetcher streams and abandons a body that crosses
    it, and whatever ANY fetcher hands back is truncated to it here, so an
    injected or third-party fetcher cannot blow the budget with a single page
    either. Truncated URLs are recorded in result.truncated."""
    dom = _registrable(domain)
    result = CrawlResult(domain=dom)
    if not dom:
        return result
    # No injected fetcher = production: robots.txt goes through the STATUS-AWARE
    # reader (a 5xx must deny, see RobotsPolicy) and pages go through the capped,
    # redirect-re-checking fetcher. An injected fetcher is a test/caller's own and
    # is used for both, exactly as before.
    robots = robots if robots is not None else RobotsPolicy(
        fetch=fetch if fetch is not None else _fetch_robots)
    fetch = fetch or CappedFetcher(robots, max_bytes=max_page_bytes)
    limiter = HostRateLimiter(sleep=sleep, clock=clock)
    floor = config.gym_deep_brain_crawl_delay() if delay is None else float(delay)
    stated = robots.crawl_delay(f"https://{dom}/")
    effective = max(floor, float(stated)) if stated is not None else floor
    result.crawl_delay = effective

    raw = {}
    for path in paths:
        if len(result.fetched) >= max_pages or result.bytes_used >= max_bytes:
            result.skipped_cap.append(f"https://{dom}{path}")
            continue
        url = f"https://{dom}{path}"
        if not robots.allowed(url):
            result.skipped_robots.append(url)
            continue
        limiter.wait(dom, effective)
        try:
            html = fetch(url)
        except Exception:  # noqa: BLE001 - one page's failure is not the crawl's
            html = None
        result.fetched.append(url)
        if not html:
            continue
        # PER-FETCH CEILING, enforced on whatever came back. The production
        # fetcher already stopped reading at this line; this is the belt for an
        # injected fetcher, and it is what keeps bytes_used honest.
        if len(html) > max_page_bytes:
            html = html[:max_page_bytes]
            result.truncated.append(url)
        result.bytes_used += len(html)
        text = website_intake._strip_html(html)[:website_intake.PAGE_TEXT_CAP]
        if not text or text in raw.values():
            continue  # many gym sites serve the homepage for every unknown path
        raw[url] = text

    # What the fetcher itself refused or cut, so the artifact can say so.
    result.skipped_redirect = list(getattr(fetch, "blocked_redirects", ()) or ())
    for u in (getattr(fetch, "truncated", ()) or ()):
        if u not in result.truncated:
            result.truncated.append(u)

    emails, phones = business_contacts(raw, dom)
    result.business_emails, result.business_phones = emails, phones
    result.pages = {u: scrub_pii(t, emails, phones) for u, t in raw.items()}
    return result


# ---- 5. the artifact's types --------------------------------------------------

class UnattributedClaim(ValueError):
    """Raised when a claim about a gym is built without the source it came from."""


@dataclass(frozen=True)
class GroundedFact:
    """One CONTENT claim about the gym, and the URL it was read from.

    Attribution is structural, not a convention: source_url is a required
    positional field and __post_init__ refuses a blank one, so an unattributed
    claim cannot be constructed at all. The category must be a real
    client_sources category so a fact can always land through the standard,
    unchanged approval path."""

    category: str
    text: str
    source_url: str

    def __post_init__(self):
        if not str(self.text or "").strip():
            raise ValueError("a grounded fact needs text")
        if not str(self.source_url or "").strip():
            raise UnattributedClaim(
                "a grounded fact must carry the source URL it came from; an "
                "unattributed claim about a gym is not representable")
        cat = str(self.category or "").strip().lower()
        if cat not in client_sources.CLIENT_CATEGORIES:
            raise ValueError(f"unknown category {self.category!r}; one of "
                             f"{client_sources.CLIENT_CATEGORIES}")


@dataclass(frozen=True)
class VoiceObservation:
    """One observation about FORM (how this gym writes and how often it posts).

    Form is MEASURED, so the provenance a voice observation carries is `basis` —
    what was measured, over what sample — rather than a page URL. A voice
    observation is never landed as a client_sources row and is never a claim
    about the gym; it tells the drafter how to sound, not what to assert."""

    metric: str
    value: object
    basis: str

    def __post_init__(self):
        if not str(self.metric or "").strip():
            raise ValueError("a voice observation needs a metric name")
        if not str(self.basis or "").strip():
            raise ValueError("a voice observation must state what it measured "
                             "and over what sample")


@dataclass(frozen=True)
class TopPost:
    """One of the gym's OWN best-performing public posts, by permalink.

    Kept as evidence of what has already worked FOR THIS GYM. `hook` is the
    post's own first line, PII-scrubbed; it is never landed as a fact and never
    quoted as a claim. permalink is required for the same reason source_url is."""

    permalink: str
    metric: str
    value: float
    hook: str = ""

    def __post_init__(self):
        if not str(self.permalink or "").strip():
            raise UnattributedClaim(
                "a top post must carry its permalink; an unattributed post is "
                "not representable")


# ---- 6. the artifact ----------------------------------------------------------

def deep_brain_dir(base_dir=None):
    if base_dir:
        return base_dir
    try:
        return config.gym_deep_brain_dir()
    except Exception:  # noqa: BLE001 - config must never break an artifact write
        return "deep_brains"


def deep_brain_path(base, base_dir=None):
    return os.path.join(deep_brain_dir(base_dir), f"{base}.md")


@dataclass
class DeepBrain:
    base: str
    gym_name: str
    domain: str
    handle: str = ""
    generated_at: str = ""
    pages: dict = field(default_factory=dict)
    facts: list = field(default_factory=list)
    voice: list = field(default_factory=list)
    top_posts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    refused: list = field(default_factory=list)

    def add_fact(self, fact):
        """Accept one GroundedFact into the artifact, or refuse it with a reason.

        THREE gates, all of which must pass:
          1. it is a GroundedFact (which already refused to exist unattributed);
          2. its citation is a page THIS crawl actually fetched — a URL we never
             read is a fabricated citation even when the URL is real;
          3. website_intake._digits_cleared over that page's STORED text, so no
             figure lands that the source page does not itself state. The stored
             text is the PII-scrubbed text, so a redacted phone number's digits
             can never be reintroduced through a fact either.
        Returns True when the fact landed."""
        if not isinstance(fact, GroundedFact):
            raise TypeError("only a GroundedFact may enter a deep brain; an "
                            "unattributed claim is not representable")
        page = self.pages.get(fact.source_url)
        if page is None:
            self.refused.append(
                f"{fact.source_url}: citation is not a page this crawl fetched")
            return False
        if not website_intake._digits_cleared(fact.text, page):
            self.refused.append(
                f"{fact.source_url}: a figure in the fact is not stated on the "
                "cited page")
            return False
        self.facts.append(fact)
        return True

    def add_voice(self, observation):
        if not isinstance(observation, VoiceObservation):
            raise TypeError("only a VoiceObservation may enter the voice section")
        self.voice.append(observation)
        return True

    def bundle(self):
        """{category: [(fact, citation_url)]} — the shape client_sources and
        website_intake._bible_text both already speak."""
        out = {}
        for f in self.facts:
            out.setdefault(f.category, []).append((f.text, f.source_url))
        return out

    def to_markdown(self):
        lines = [
            f"# {self.gym_name} — Echo deep brain",
            "",
            f"> Compiled by Echo from {self.gym_name}'s OWN public website "
            f"({self.domain}) and its OWN public Instagram feed"
            + (f" (@{self.handle})" if self.handle else "") + ".",
            "> Every FACT below carries the URL it was read from. Nothing here "
            "was invented, inferred, or filled in.",
            "> Facts land in client_sources as PENDING; a human approves them "
            "before Echo may draft from them.",
            "",
            f"- base: {self.base}",
            f"- domain: {self.domain}",
            f"- instagram: {('@' + self.handle) if self.handle else '(none)'}",
            f"- generated_at: {self.generated_at}",
            "",
            "## Grounded facts (attributed, pending human approval)",
            "",
        ]
        if self.facts:
            for f in self.facts:
                lines.append(f"- [{f.category}] {f.text}")
                lines.append(f"  - source: {f.source_url}")
        else:
            lines.append("- (none)")
        lines += ["", "## Voice observations (FORM only, never facts)", ""]
        if self.voice:
            for v in self.voice:
                lines.append(f"- {v.metric}: {v.value}")
                lines.append(f"  - measured from: {v.basis}")
        else:
            lines.append("- (none)")
        lines += ["", "## What already worked for this gym", ""]
        if self.top_posts:
            for p in self.top_posts:
                lines.append(f"- {p.metric} {p.value}: {p.permalink}")
                if p.hook:
                    lines.append(f"  - own opening line: {p.hook}")
        else:
            lines.append("- (none)")
        if self.notes:
            lines += ["", "## Crawl notes", ""]
            lines += [f"- {n}" for n in self.notes]
        if self.refused:
            lines += ["", "## Refused (kept out of the artifact on purpose)", ""]
            lines += [f"- {r}" for r in self.refused]
        lines.append("")
        return "\n".join(lines)

    def write(self, base_dir=None):
        """Write the DERIVED deep-brain artifact. This file is Echo's own output
        and a re-run regenerates it; the human-owned brand bible and voice doc are
        NEVER touched here: this module writes no bible and no voice doc at all."""
        path = deep_brain_path(self.base, base_dir)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_markdown())
        return path


# ---- 7. social form profile ---------------------------------------------------

def _hook(caption):
    for line in str(caption or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _has_emoji(text):
    for ch in str(text or ""):
        cp = ord(ch)
        for lo, hi in _EMOJI_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _permalink(post):
    url = str((post or {}).get("url") or "").strip()
    if url:
        return url
    code = str((post or {}).get("shortCode") or "").strip()
    return f"https://www.instagram.com/p/{code}/" if code else ""


def social_form_profile(handle, *, apify=None, days=SOCIAL_WINDOW_DAYS,
                        today=None, results_limit=SOCIAL_RESULTS_LIMIT):
    """(observations, top_posts, error) read off the gym's OWN PUBLIC Instagram
    feed through the existing Apify path (social_baseline.ApifyClient).

    Captures FORM, not content: caption length, hook shape, emoji and hashtag
    habits, posting cadence, and whether the gym asks for anything. Plus which of
    the gym's OWN posts performed best, by permalink.

    Comments and commenter identities are NEVER read: the only post fields
    touched are _POST_FIELDS_READ. Every string that survives into the artifact
    is scrubbed with an EMPTY contact allowlist, because a number in a caption
    could belong to anyone.

    On any Apify failure returns ([], [], reason) — an honest empty, never a
    fabricated cadence."""
    from . import social_baseline

    handle = str(handle or "").strip().lstrip("@")
    if not handle:
        return [], [], "no public instagram handle on record"
    client = apify if apify is not None else social_baseline.ApifyClient()
    try:
        posts = client.fetch_posts(handle, days, results_limit)
    except Exception as e:  # noqa: BLE001 - ApifyError and anything under it
        return [], [], f"{type(e).__name__}: {e}"
    posts = [p for p in (posts or []) if isinstance(p, dict) and not p.get("isPinned")]
    if not posts:
        return [], [], f"no public posts read for @{handle} in the last {days} days"

    end = (today or datetime.now(timezone.utc).date())
    if isinstance(end, datetime):
        end = end.date()
    start = end - timedelta(days=int(days))
    try:
        measures = social_baseline.compute_measures(posts, start, end + timedelta(days=1))
    except Exception:  # noqa: BLE001 - a bad window is not a fabricated profile
        return [], [], "could not measure the public feed window"

    sampled = measures.get("posts_count") or 0
    if not sampled:
        return [], [], f"no public posts for @{handle} inside the measured window"
    basis = (f"{sampled} public post(s) from https://www.instagram.com/{handle}/ "
             f"between {start.isoformat()} and {end.isoformat()}")

    captions = [str(p.get("caption") or "") for p in posts]
    hooks = [_hook(c) for c in captions if _hook(c)]
    hook_len = social_baseline._median([len(h) for h in hooks]) if hooks else None
    tags = [len(_HASHTAG_RE.findall(c)) for c in captions]
    emoji_posts = sum(1 for c in captions if _has_emoji(c))

    obs = [
        VoiceObservation("posts sampled", sampled, basis),
        VoiceObservation("posting cadence (posts/week)",
                         measures.get("posts_per_week"), basis),
        VoiceObservation("median caption length (chars)",
                         measures.get("median_caption_length"), basis),
        VoiceObservation("median opening line length (chars)", hook_len, basis),
        VoiceObservation("median hashtags per post",
                         social_baseline._median(tags) if tags else None, basis),
        VoiceObservation("posts using emoji (%)",
                         round(100.0 * emoji_posts / len(captions), 1)
                         if captions else None, basis),
        VoiceObservation("posts carrying an ask (%)", measures.get("ask_pct"), basis),
        VoiceObservation("reel / video share (%)",
                         measures.get("reels_share_pct"), basis),
    ]

    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    ranked = []
    for p in posts:
        link = _permalink(p)
        if not link:
            continue  # an unattributed post is not representable
        plays = _num(p.get("videoPlayCount"))
        if plays is not None:
            ranked.append((plays, "video plays", link, _hook(p.get("caption"))))
            continue
        likes = _num(p.get("likesCount"))
        if likes is not None:
            ranked.append((likes, "likes", link, _hook(p.get("caption"))))
    ranked.sort(key=lambda t: t[0], reverse=True)
    top = [TopPost(permalink=link, metric=metric, value=float(val),
                   hook=scrub_pii(hook)[:200])
           for val, metric, link, hook in ranked[:3]]
    return obs, top, ""


# ---- 8. resolution ------------------------------------------------------------

def resolve_handle(base, gym_name, handle=None):
    """The gym's PUBLIC instagram handle: the explicit arg, else the curated
    portal_domains record. "" when nothing is on record — a handle is NEVER
    guessed from the gym's name (a wrong handle is a wrong gym's voice)."""
    if str(handle or "").strip():
        return str(handle).strip().lstrip("@")
    from . import portal_domains
    for candidate in (gym_name, base):
        rec = portal_domains.record_for(candidate) or {}
        ig = str(rec.get("instagram") or "").strip()
        if ig:
            return ig.lstrip("@")
    return ""


def _blocked(base, reason):
    try:
        db.audit("gym_deep_brain", base, f"blocked: {reason}")
    except Exception:  # noqa: BLE001 - the audit is a courtesy, not the answer
        pass
    return {"ok": False, "blocked": True, "base": base, "reason": reason}


# ---- 9. the build -------------------------------------------------------------

def build_deep_brain(base, *, domain=None, handle=None, fetch=None, llm=None,
                     apify=None, sleep=None, clock=None, robots=None,
                     dry_run=False, base_dir=None, days=SOCIAL_WINDOW_DAYS,
                     today=None, paths=DEFAULT_PATHS):
    """Compile ONE gym's deep brain. Returns a summary dict, never raises:

      {"ok": True, "base", "domain", "handle", "artifact", "facts", "voice",
       "top_posts", "landed", "bible", "notes"}
      {"ok": False, "blocked": True, "base", "reason"}

    BLOCKS (no artifact, no client_sources row, no bible) when the flag is off,
    no domain is on record, no public handle is on record, robots disallows
    everything, no page could be read, no verifiable fact could be extracted, or
    the public social read failed. Nothing is ever synthesized to fill a hole.

    WRITES EXACTLY TWO THINGS: the derived artifact file, and PENDING
    client_sources rows. It does NOT write the brand bible / voice doc — see the
    module docstring. The "bible" key in the summary says so out loud.

    LANDS the grounded facts through client_sources.add_source with
    status="pending" and the page URL as the citation. Scraped material is NEVER
    auto-approved, not even when AGENT_INTAKE_AUTO_APPROVE is armed: a machine
    read a stranger's website, and a human still has to say yes before Echo may
    assert any of it. The post approval gate is untouched either way.

    dry_run=True computes everything and writes NOTHING (no artifact, no rows,
    no bible) so a human can inspect what a real run would land."""
    base = str(base or "").strip()
    if not base:
        return _blocked(base, "no account base given")
    if not config.gym_deep_brain_enabled():
        return _blocked(base, "AGENT_GYM_DEEP_BRAIN off")
    try:
        dom, gym_name = website_intake._resolve_domain(base, domain)
        if not dom:
            return _blocked(base, "no domain on record (pass --domain or add the "
                                  "gym to portal_domains); a domain is never guessed")
        ig = resolve_handle(base, gym_name, handle)
        if not ig:
            return _blocked(base, "no public social handle on record (pass "
                                  "--handle); a handle is never guessed")

        crawl = crawl_site(dom, paths, fetch=fetch, sleep=sleep, clock=clock,
                           robots=robots)
        if not crawl.pages:
            if crawl.skipped_robots and not crawl.fetched:
                return _blocked(base, f"robots.txt on {dom} disallows every page "
                                      "we would read; nothing was fetched")
            return _blocked(base, f"no readable public pages fetched from {dom}")

        bundle = website_intake.extract_sources(crawl.pages, gym_name, llm=llm)
        if not bundle:
            return _blocked(base, f"no verifiable facts extracted from {dom}")

        obs, top, social_error = social_form_profile(
            ig, apify=apify, days=days, today=today)
        if social_error:
            return _blocked(base, f"public social read failed: {social_error}")

        brain = DeepBrain(
            base=base, gym_name=gym_name, domain=dom, handle=ig,
            generated_at=datetime.now(timezone.utc).isoformat(),
            pages=dict(crawl.pages))
        for category, items in bundle.items():
            for text, url in items:
                try:
                    brain.add_fact(GroundedFact(category=category, text=text,
                                                source_url=url))
                except (ValueError, TypeError) as e:
                    brain.refused.append(f"{category}: {e}")
        if not brain.facts:
            return _blocked(base, f"every extracted fact failed attribution or the "
                                  f"figure check for {dom}")
        for o in obs:
            brain.add_voice(o)
        brain.top_posts = list(top)
        if crawl.skipped_robots:
            brain.notes.append(
                f"{len(crawl.skipped_robots)} page(s) SKIPPED, never requested, "
                f"because robots.txt on {dom} disallows them: "
                + ", ".join(crawl.skipped_robots))
        if crawl.skipped_cap:
            brain.notes.append(f"{len(crawl.skipped_cap)} page(s) skipped at the "
                               f"{MAX_PAGES}-page / {MAX_BYTES}-byte cap")
        for note in crawl.skipped_redirect:
            brain.notes.append(note)
        if crawl.truncated:
            brain.notes.append(
                f"{len(crawl.truncated)} response(s) cut off at the "
                f"{MAX_PAGE_BYTES}-byte per-page ceiling: "
                + ", ".join(crawl.truncated))
        brain.notes.append(f"crawl delay honored between requests: "
                           f"{crawl.crawl_delay}s")

        if dry_run:
            return {"ok": True, "base": base, "domain": dom, "handle": ig,
                    "artifact": "", "facts": len(brain.facts),
                    "voice": len(brain.voice), "top_posts": len(brain.top_posts),
                    "landed": 0,
                    "bible": "not written: a scrape never authors the brand "
                             "bible (and this is a dry run)",
                    "notes": list(brain.notes), "dry_run": True,
                    "markdown": brain.to_markdown()}

        artifact = brain.write(base_dir)
        account_key = f"{base}_ig"
        landed = 0
        for f in brain.facts:
            # PENDING, always. Never intake_status(): a scrape is not the gym
            # handing us its own material, so auto-approve must not reach it.
            client_sources.add_source(account_key, f.category, f.text,
                                      citation=f.source_url, status="pending")
            landed += 1
        # NO BIBLE. A scrape never authors a gym's voice doc (see the module
        # docstring, "A SCRAPE NEVER AUTHORS THE BRAND BIBLE"). The facts above
        # are pending client_sources rows; a human approves them, and a human
        # writes or activates the bible.
        bible = ("not written: a scrape never authors the brand bible; the "
                 "facts above are PENDING for a human to approve")
        db.audit("gym_deep_brain", base,
                 f"deep brain from {dom} + @{ig}: {len(brain.facts)} fact(s) "
                 f"landed pending, {len(brain.voice)} voice observation(s)",
                 account_key)
        return {"ok": True, "base": base, "domain": dom, "handle": ig,
                "artifact": artifact, "facts": len(brain.facts),
                "voice": len(brain.voice), "top_posts": len(brain.top_posts),
                "landed": landed, "bible": bible, "notes": list(brain.notes),
                "dry_run": False}
    except Exception as e:  # noqa: BLE001 - one gym's failure is a report, not a crash
        return _blocked(base, f"{type(e).__name__}: {e}")
