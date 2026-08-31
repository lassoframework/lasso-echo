"""
website_intake.py — WEBSITE AUTO-INTAKE: read a client gym's source bundle off
its OWN website when the gym never filled the intake form.

WHY (Blake's rulings): "the only human thing should be the gym approving the
post" (2026-08-31) and "if they don't upload, scan their website" (2026-08-25).
Eight client gyms sat with ZERO client_sources (theboltonclub, crossfitlocal,
hillcountry, zanshinfitness630e22, district_h, train7164ae502, crossfitreverb,
crossfitnewtown), so nothing could draft for them, dup-caption remediation had
no material to regenerate from, and gap/infographic fills were blocked. The
facts those calendars need are already public on each gym's own website.

THE NO-FABRICATION LAW STILL GOVERNS. Every stored row is a verbatim-ish fact
COPIED/CONDENSED from a fetched page, carrying that page's URL as its citation.
The LLM only SELECTS facts from the fetched text; extract_sources then drops
any fact whose figures do not appear in the cited page's own text (the same
digit check drafter._output_claims_cleared runs on captions), any fact citing a
page we did not fetch, and any fact outside the 8-to-40-word window. Testimonial
and promo are deliberately NOT extracted: a member quote needs recorded
permission (social_proof law) and a promo is time-boxed, and a scrape can
verify neither.

Landing goes through the standard intake path (client_sources.submit_intake
under <base>_ig), so the status gate is unchanged: rows land PENDING unless
AGENT_INTAKE_AUTO_APPROVE is armed, and the POST approval gate is untouched
either way. A gym that already has ANY sources (approved or pending) is
SKIPPED — this lane only fills a vacuum, it never competes with a real intake.

Behind AGENT_WEBSITE_AUTO_INTAKE (config.website_auto_intake_enabled, default
OFF). Wired into the runner like zernio_profile_link: isolated, a failure never
takes the draft run down. Manual per-gym run:

    python -m agent website-intake --account <base> [--domain x.com] [--force]

Everything is injectable (fetch / llm / alert) so the whole lane is
unit-testable offline with no network and no Anthropic key.
"""

import json
import os
import re
from html.parser import HTMLParser

from . import client_sources, config, db

# Same browser-ish UA the welcome-pipeline scrapers use (website_scan /
# welcome_posts): some gym site hosts 403 a bare python-requests UA.
_UA = "Mozilla/5.0 (compatible; LASSO-Echo/1.0; +https://lassoframework.com)"

# The pages that carry a gym's facts. /pricing and /faq 404 on many sites;
# fetch_site_text tolerates that (a missing page is simply not in the result).
DEFAULT_PATHS = ("/", "/about", "/services", "/pricing", "/faq", "/contact")

# Per-page text cap: enough for a full marketing page, small enough that six
# pages still fit one extraction prompt comfortably.
PAGE_TEXT_CAP = 8000

# What we extract, and how many rows each category may land. testimonial and
# promo are absent ON PURPOSE (permission / time-box, see module docstring).
# Minimums are prompt guidance only — a thin site lands what it really has,
# never padding (padding would be fabrication).
CATEGORY_CAPS = {"service": 8, "about": 3, "offer": 3, "faq": 3, "educational": 5}

# A fact must be a real sentence, not a nav crumb ("Home About Contact") and
# not a whole page. Facts outside this window are dropped, never trimmed by us
# (trimming could change meaning).
FACT_MIN_WORDS, FACT_MAX_WORDS = 8, 40

# Mirrors drafter._FIGURE_RE: a standalone digit run, optionally with decimal /
# thousands separators. Redefined here (not imported) so this module does not
# pull drafter's full import chain just for one regex.
_FIGURE_RE = re.compile(r"\d[\d,\.]*")


# ---- 1. fetch ----------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Visible-text stripper on stdlib html.parser (no bs4 dependency — it is
    not in requirements and one tag-blind text pass does not justify adding it).
    Skips script/style/head/etc. subtrees; everything else's text is kept."""

    _SKIP = frozenset({"script", "style", "noscript", "template", "head",
                       "svg", "iframe"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        chunk = data.strip()
        if chunk:
            self.parts.append(chunk)


def _strip_html(html):
    """Visible text of an HTML page, whitespace-collapsed. Never raises: a
    parser blow-up on broken markup returns whatever was collected first."""
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
    except Exception:
        pass  # keep the parts gathered before the markup broke
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _default_fetch(url):
    """One page's HTML, or None on any failure (404s and timeouts are expected
    on gym sites; a missing page is not an error for this lane)."""
    import requests
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": _UA},
                         allow_redirects=True)
        if r.status_code >= 400:
            return None
        return r.text
    except Exception:
        return None


def fetch_site_text(domain, paths=DEFAULT_PATHS, fetch=None):
    """{url: visible_text} for up to len(paths) pages of https://<domain>.
    Pages that fail, 404, or strip to nothing are simply absent. Each page's
    text is capped at PAGE_TEXT_CAP chars. Never raises."""
    fetch = fetch or _default_fetch
    out = {}
    try:
        dom = re.sub(r"^https?://", "", str(domain or "").strip(), flags=re.I)
        dom = dom.strip("/")
        if not dom:
            return {}
        seen_text = set()
        for path in paths:
            url = f"https://{dom}{path}"
            try:
                html = fetch(url)
            except Exception:
                html = None  # an injected fetcher must not sink the whole scan
            if not html:
                continue
            text = _strip_html(html)[:PAGE_TEXT_CAP]
            if not text:
                continue
            # Many gym sites serve the homepage for every unknown path instead
            # of a 404; identical text would double every citation's weight.
            if text in seen_text:
                continue
            seen_text.add(text)
            out[url] = text
    except Exception:
        return out  # whatever was gathered still counts; never raises
    return out


# ---- 2. extract --------------------------------------------------------------

_SYSTEM = """You extract factual source material for a gym's social media agent, from the gym's OWN website text.

HARD RULES, no exceptions:
- Every fact must be copied or lightly condensed from the provided page text ONLY. Never add, infer, or embellish anything.
- Never write a number, price, or statistic that does not appear verbatim in the provided text.
- No dashes of any kind in your output (no hyphens, en dashes, or em dashes). Rewrite the sentence instead.
- Each fact is one plain sentence of 8 to 40 words.
- Each fact cites the exact page URL it came from. Only cite URLs you were given.

Return ONLY a JSON object, no prose, in this shape:
{"service": [{"fact": "...", "url": "..."}], "about": [...], "offer": [...], "faq": [...], "educational": [...]}

Category guidance:
- service: 3 to 8 facts, each one program or service the gym provides.
- about: 1 to 3 facts about who the gym is, its story, or its coaches.
- offer: 0 to 3 facts, ONLY if the site states a real offer, package, or price. Omit the category if the site states none.
- faq: 0 to 3 facts, each a common question and its answer as the site states it.
- educational: 2 to 5 how to or why it works facts the site itself states and the gym could teach.
Fewer real facts always beats padding. Omit any category the text does not support."""


def _call_llm(system, user):
    """The SAME Anthropic plumbing drafter._call_llm_caption uses (same env key,
    same AGENT_SB7_MODEL knob) with a larger max_tokens: a full source bundle
    (up to ~20 cited facts) does not fit the caption call's 400-token cap.
    Raises on a missing key/SDK; extract_sources catches and returns {}."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    try:
        import anthropic
    except Exception:
        raise RuntimeError("anthropic SDK not installed")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=config.sb7_model(), max_tokens=2000,
        system=system, messages=[{"role": "user", "content": user}])
    parts = getattr(resp, "content", []) or []
    return "".join(getattr(p, "text", "") or "" for p in parts)


def _parse_bundle_json(raw):
    """The JSON object out of an LLM reply, tolerating code fences and prose
    around it. Returns {} when no object parses."""
    text = str(raw or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _digits_cleared(fact, page_text):
    """True when every figure in `fact` appears verbatim in the cited page's
    text — the same substring digit check drafter._output_claims_cleared runs on
    captions, pointed at the fetched site instead of the voice doc. A fact with
    no figures is clean by definition."""
    for tok in _FIGURE_RE.findall(fact or ""):
        norm = tok.strip(".,")
        if norm and norm not in (page_text or ""):
            return False
    return True


def extract_sources(site_texts, gym_name, llm=None):
    """{category: [(fact, citation_url)]} extracted from fetched site text.

    The LLM only SELECTS; this function is the gate. A fact is DROPPED when:
      - its category is not in CATEGORY_CAPS (testimonial/promo never land here);
      - its citation is not a page we actually fetched (an invented URL is a
        fabricated citation);
      - any of its figures is absent from the cited page's text (invented number);
      - it falls outside the 8-to-40-word window (nav crumbs / run-on pastes).
    Facts past a category's cap are dropped, first-listed wins. Every kept fact
    is dash-scrubbed through copy_gate (the single house-style gate). Never
    raises; returns {} on any failure or an empty site."""
    try:
        if not site_texts:
            return {}
        from . import copy_gate
        llm = llm or _call_llm
        pages = "\n\n".join(f"PAGE {url}\n{text}"
                            for url, text in site_texts.items())
        user = (f"Gym name: {gym_name}\n\nWebsite text, one PAGE block per "
                f"fetched URL:\n\n{pages}")
        bundle = _parse_bundle_json(llm(_SYSTEM, user))
        out = {}
        for category, items in bundle.items():
            cat = str(category or "").strip().lower()
            cap = CATEGORY_CAPS.get(cat)
            if cap is None or not isinstance(items, list):
                continue
            kept = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                fact = str(item.get("fact") or "").strip()
                url = str(item.get("url") or "").strip()
                if not fact or url not in site_texts:
                    continue
                if not _digits_cleared(fact, site_texts[url]):
                    continue  # a figure the cited page never states: fabricated
                fact = copy_gate.scrub(fact)  # dash law, house gate
                words = len(fact.split())
                if not (FACT_MIN_WORDS <= words <= FACT_MAX_WORDS):
                    continue
                kept.append((fact, url))
                if len(kept) >= cap:
                    break
            if kept:
                out[cat] = kept
        return out
    except Exception:
        return {}


# ---- 3. voice doc ------------------------------------------------------------

def _bible_text(gym_name, domain, bundle):
    """A minimal durable brand bible in the lasso_voice.md shape (mirrors
    bible_drafter.draft_bible's sections) carrying ONLY site-derived facts plus
    neutral structure. No invented voice, pillars from what the site actually
    covers, and a CTA rotation that points at the gym's own site — nothing a
    scrape cannot verify."""
    from .bible_drafter import BASELINE_GUARDRAILS
    about = [f for f, _ in bundle.get("about", [])]
    services = [f for f, _ in bundle.get("service", [])]
    pillars = [c for c in ("service", "educational", "offer", "faq", "about")
               if bundle.get(c)]
    return f"""# {gym_name} Brand Bible — Echo Social Agent (auto-drafted from {domain})

> Generated by website auto-intake from the gym's OWN website. Every fact below
> was read off {domain}; nothing is invented. A human may refine this doc at any
> time; the POST approval gate is unchanged either way.

## 1. Who {gym_name} is
{chr(10).join(about) if about else f"{gym_name}. See {domain}."}

## 2. Who we talk TO (the avatar)
People the gym's own website speaks to. No avatar claims beyond the site's own words.

## 3. Voice and tone
Plain, warm, and direct, in the gym's own website language. No hype the site itself does not use.

## 4. Hard guardrails (never violate)
{BASELINE_GUARDRAILS}

## 5. Content pillars
{chr(10).join("- " + p for p in pillars) if pillars else "- service"}

Services from the site:
{chr(10).join("- " + s for s in services) if services else "- see " + domain}

## 6. Platform rules

### CTA rotation (cycle in order, one per post)
- Learn more at {domain}
- Send us a message to get started
- Visit {domain} to book your first visit
- Follow along for more from {gym_name}

### Hashtag strategy (3 to 5 per post)
Ask the gym for preferred hashtags before adding any. Until then, post without hashtags rather than invent them.
"""


def _write_bible_if_missing(base, gym_name, domain, bundle):
    """Write the durable per-gym voice doc to <client_voice_dir>/<base>/
    lasso_voice.md IF none exists. An existing bible (human-reviewed or from a
    real intake) is NEVER overwritten — human owns voice. Returns a one-line
    report string."""
    out_dir = os.path.join(config.client_voice_dir(), base)
    path = os.path.join(out_dir, "lasso_voice.md")
    if os.path.exists(path):
        return f"bible exists, untouched ({path})"
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_bible_text(gym_name, domain, bundle))
    return f"bible written ({path})"


# ---- 4. per-gym intake -------------------------------------------------------

def _gym_display_name(base):
    """The gym's real name, best source first: the gyms table (portal truth),
    then the account registry's display label with its platform suffix trimmed.
    Falls back to a titled form of the base key — used only as a LABEL in the
    bible/prompt, never as a fact."""
    row = db.gym_get(base) or {}
    for key in ("gym_name", "display_name"):
        name = str(row.get(key) or "").strip()
        if name:
            return name
    try:
        from .accounts import get_account
        acct = get_account(base) or get_account(f"{base}_ig")
        if acct is not None:
            name = re.sub(r"\s+(IG|FB)$", "", (acct.display_name or "").strip())
            if name:
                return name
    except Exception:
        pass
    return base.replace("_", " ").title()


def _resolve_domain(base, domain=None):
    """(domain, gym_name). Order: the explicit arg, then portal_domains keyed by
    every real name we hold for the gym (gyms table, account registry, the base
    key itself). Returns ("", name) when nothing is on record — the caller then
    reports and stops; a domain is NEVER guessed (wrong site = wrong facts)."""
    name = _gym_display_name(base)
    if (domain or "").strip():
        return (domain or "").strip(), name
    from . import portal_domains
    for candidate in (name, base):
        dom = portal_domains.domain_for(candidate)
        if dom:
            return dom, name
    return "", name


def intake_from_website(base, *, domain=None, status=None, force=False,
                        fetch=None, llm=None):
    """Auto-intake ONE gym from its website. Returns a summary dict, never
    raises:

      {"ok": True, "base", "domain", "landed", "status", "bible", "categories"}
      {"ok": False, "base", "reason"}

    SKIPS (unless force) any gym that already has sources in ANY status — this
    lane fills a vacuum, it never stacks a scrape on top of a real intake.
    Lands via client_sources.submit_intake under <base>_ig so the status gate
    (pending unless AGENT_INTAKE_AUTO_APPROVE) is the standard intake gate."""
    base = (base or "").strip()
    if not base:
        return {"ok": False, "base": base, "reason": "no account base given"}
    account_key = f"{base}_ig"
    try:
        # all_sources is tenant-variant aware, so rows under the bare base key
        # (portal intake) still count as "this gym already has sources".
        if not force and client_sources.all_sources(account_key):
            return {"ok": False, "base": base,
                    "reason": "gym already has sources (approved or pending); "
                              "use force to re-intake"}
        dom, gym_name = _resolve_domain(base, domain)
        if not dom:
            return {"ok": False, "base": base,
                    "reason": "no domain on record (pass --domain or add the gym "
                              "to portal_domains)"}
        site_texts = fetch_site_text(dom, fetch=fetch)
        if not site_texts:
            return {"ok": False, "base": base,
                    "reason": f"no readable pages fetched from {dom}"}
        bundle = extract_sources(site_texts, gym_name, llm=llm)
        if not bundle:
            return {"ok": False, "base": base,
                    "reason": f"no verifiable facts extracted from {dom}"}
        land_status = status or client_sources.intake_status()
        created = client_sources.submit_intake(
            account_key, bundle, status=land_status, default_citation=dom)
        bible_note = _write_bible_if_missing(base, gym_name, dom, bundle)
        db.audit("website_intake", base,
                 f"landed {len(created)} {land_status} source(s) from {dom}",
                 account_key)
        return {"ok": True, "base": base, "domain": dom,
                "landed": len(created), "status": land_status,
                "bible": bible_note, "categories": sorted(bundle)}
    except Exception as e:  # noqa: BLE001 - one gym's failure is a report, not a crash
        return {"ok": False, "base": base,
                "reason": f"{type(e).__name__}: {e}"}


# ---- 5. the fleet sweep (runner lane) -----------------------------------------

def _alert_once(base, message, alert=None):
    """One deduped ops alert per gym per outcome: the same message never posts
    twice (kv-stamped), so a daily runner pass cannot flood Slack with the same
    'could not auto-intake' line every day. Best effort, never raises."""
    try:
        key = f"website_intake_alert_{base}"
        if db.kv_get(key) == message:
            return False
        if alert is None:
            from .ops_alerts import alert as _ops_alert
            alert = _ops_alert
        alert(message)
        db.kv_set(key, message)
        return True
    except Exception:
        return False


def run(bases=None, websites=None, fetch=None, llm=None, alert=None):
    """The fleet sweep: for every client gym with ZERO client_sources, auto-
    intake from its website. Behind AGENT_WEBSITE_AUTO_INTAKE (flag OFF = total
    no-op: nothing fetched, nothing written). One deduped ops alert per gym on
    success or failure. Returns a summary dict; never raises past a gym.

    `websites` is an optional {base: domain} override map (the portal Supabase
    client_websites table is deliberately NOT read here — no new network reads;
    a caller that holds that mapping passes it in)."""
    if not config.website_auto_intake_enabled():
        return {"ok": False, "reason": "AGENT_WEBSITE_AUTO_INTAKE off"}
    if bases is None:
        from .calendar_autopublish import client_gym_bases
        bases = client_gym_bases()
    websites = websites or {}
    intaken, skipped, failed, landed = [], [], [], 0
    for base in bases:
        try:
            if client_sources.all_sources(f"{base}_ig"):
                skipped.append(base)  # has material already; not this lane's job
                continue
            out = intake_from_website(base, domain=websites.get(base),
                                      fetch=fetch, llm=llm)
        except Exception as e:  # noqa: BLE001 - the sweep survives any one gym
            out = {"ok": False, "base": base,
                   "reason": f"{type(e).__name__}: {e}"}
        if out.get("ok"):
            intaken.append(base)
            landed += out.get("landed", 0)
            _alert_once(base,
                        f"auto-intake landed {out['landed']} sources from "
                        f"{out['domain']} for {base} — no action needed",
                        alert=alert)
            print(f"[website-intake] {base}: {out['landed']} source(s) "
                  f"({out['status']}) from {out['domain']}; {out['bible']}")
        else:
            failed.append(base)
            _alert_once(base,
                        f"could not auto-intake {base}: {out.get('reason')}",
                        alert=alert)
            print(f"[website-intake] {base}: {out.get('reason')}")
    return {"ok": True, "intaken": intaken, "skipped": skipped,
            "failed": failed, "landed": landed}
