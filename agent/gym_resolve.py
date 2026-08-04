"""
Gym resolution for the welcome-post pipeline (Part A).

Source-of-truth order, first hit wins:
  1. PORTAL tenant record (brand_voice/tenants/<key>/tenant.json, from Social
     Intake) matched to the Stripe customer -> CONFIRMED
  2. Stripe customer.name (the business name field at checkout) -> CONFIRMED
  3. Email domain inference (birddogcrossfit.com -> Bird Dog Crossfit) -> INFERRED
  4. Web search fallback (search_fn injected; no wired search API in this repo,
     so this tier is a no-op until one is provided) -> INFERRED

INFERRED resolutions are never postable on their own: the caller (welcome_new_
clients) must surface them to Blake for a yes/no before generating anything.

Portal matching note (be honest about the gap): tenant.json does not currently
store the client's email, and Stripe customer metadata is not guaranteed to
carry an account_key. match_portal_tenant() checks, in order: an explicit
metadata account_key/gym_key/tenant_key, then an exact case-insensitive name
match against each tenant's stored name. Until Blake wires one of those two at
checkout or intake, most new clients will resolve at tier 2 or 3, not tier 1 -
this is reported, not hidden.
"""

import re
from dataclasses import dataclass

from . import tenants

_GYM_SUFFIXES = (
    "crossfit", "fitness", "gym", "athletics", "barbell", "strength",
    "training", "performance", "wellness", "health", "cycle", "cycling",
    "yoga", "pilates", "bootcamp", "conditioning", "fit",
)

CONFIRMED = "CONFIRMED"
INFERRED = "INFERRED"


@dataclass
class GymResolution:
    gym_name: str
    owner_name: str
    confidence: str        # CONFIRMED or INFERRED
    source: str             # "portal" | "stripe_business_name" | "email_domain" | "web_search" | "unresolved"
    account_key: str = ""   # matched tenant key, if any
    website: str = ""        # best-guess website for the logo scrape
    note: str = ""           # honest caveat for INFERRED / partial resolutions


def normalize_owner_name(raw):
    """Title-case an owner name pulled from Stripe (which is often all caps or
    inconsistently cased: 'RYAN PARR', 'Just Estes'). Handles apostrophes,
    hyphens, and Mc/Mac prefixes without inventing or correcting the name
    itself -- only casing changes."""
    raw = (raw or "").strip()
    if not raw:
        return ""

    def cap_word(word):
        if not word:
            return word
        # O'Brien, D'Angelo
        if "'" in word:
            return "'".join(cap_word(p) for p in word.split("'"))
        m = re.match(r"^(Mc|Mac)([a-zA-Z]+)$", word, re.IGNORECASE)
        if m and len(m.group(2)) > 1:
            return m.group(1)[0].upper() + m.group(1)[1:].lower() + \
                m.group(2)[0].upper() + m.group(2)[1:].lower()
        return word[0].upper() + word[1:].lower() if len(word) > 1 else word.upper()

    parts = raw.split()
    out_parts = []
    for part in parts:
        segs = part.split("-")
        out_parts.append("-".join(cap_word(s) for s in segs))
    return " ".join(out_parts)


def _domain_stem(email_or_url):
    """The registrable-ish stem of a domain: strips scheme/www/path/query and
    the last dot-segment (TLD, or the second-to-last for a two-part TLD like
    co.uk -- best-effort, not a public-suffix-list lookup)."""
    s = str(email_or_url or "").strip().lower()
    if "@" in s and "://" not in s:
        s = s.split("@", 1)[1]
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/", 1)[0]
    s = s[4:] if s.startswith("www.") else s
    labels = [x for x in s.split(".") if x]
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net") and len(labels[-1]) == 2:
        return labels[-3]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0] if labels else ""


def domain_to_gym_name(email_or_url):
    """Best-effort, human-reviewable guess at a gym name from a domain. Splits
    on hyphens/underscores directly (unambiguous); for a smashed domain, splits
    off a known gym-type suffix word (crossfit, fitness, gym, ...) and leaves
    the remainder as one capitalized token, since general word segmentation of
    a smashed compound has no reliable answer without a dictionary. This is
    ALWAYS surfaced as INFERRED -- it is a starting guess, not a fact."""
    stem = _domain_stem(email_or_url)
    if not stem:
        return ""
    if "-" in stem or "_" in stem:
        words = re.split(r"[-_]+", stem)
        return " ".join(w.capitalize() for w in words if w)
    lower = stem.lower()
    for suffix in sorted(_GYM_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix) and len(lower) > len(suffix):
            prefix = stem[: -len(suffix)]
            return f"{prefix.capitalize()} {suffix.capitalize()}"
    return stem.capitalize()


def website_from_email(email):
    domain = str(email or "").split("@")[-1].strip().lower()
    if not domain or "." not in domain:
        return ""
    return f"https://{domain}"


def match_portal_tenant(stripe_customer, base_dir=None):
    """Match a Stripe customer to an existing portal tenant record. Returns
    (account_key, tenant_record) or (None, None). See the module docstring for
    the honest gap this depends on (Stripe metadata or an exact name match)."""
    metadata = getattr(stripe_customer, "metadata", None) or {}
    for meta_key in ("account_key", "gym_key", "tenant_key"):
        key = metadata.get(meta_key)
        if key:
            rec = tenants.load_tenant(key, base_dir)
            if rec is not None:
                return key, rec
    business_name = (getattr(stripe_customer, "name", "") or "").strip().lower()
    if business_name:
        for key in tenants.list_tenants(base_dir):
            rec = tenants.load_tenant(key, base_dir)
            if rec and str(rec.get("name", "")).strip().lower() == business_name:
                return key, rec
    return None, None


def resolve_gym(stripe_customer, base_dir=None, search_fn=None):
    """
    Resolve one Stripe customer to a gym name + owner name, per the source
    order in the module docstring. Never returns an unconfirmed name marked
    CONFIRMED. `search_fn(query) -> {"name": str, "website": str} or None` is
    the web-search fallback; no search API is wired in this repo today, so
    passing None (the default) simply skips tier 4.
    """
    account_key, tenant_rec = match_portal_tenant(stripe_customer, base_dir)
    if tenant_rec is not None:
        return GymResolution(
            gym_name=tenant_rec.get("name", ""),
            owner_name=normalize_owner_name(tenant_rec.get("approver_name", "")),
            confidence=CONFIRMED,
            source="portal",
            account_key=account_key,
            website=website_from_email(getattr(stripe_customer, "email", "")),
        )

    business_name = (getattr(stripe_customer, "name", "") or "").strip()
    if business_name:
        return GymResolution(
            gym_name=business_name,
            owner_name="",  # Stripe's customer.name is the business, not a person
            confidence=CONFIRMED,
            source="stripe_business_name",
            website=website_from_email(getattr(stripe_customer, "email", "")),
        )

    email = getattr(stripe_customer, "email", "") or ""
    domain_guess = domain_to_gym_name(email)
    if domain_guess:
        return GymResolution(
            gym_name=domain_guess,
            owner_name="",
            confidence=INFERRED,
            source="email_domain",
            website=website_from_email(email),
            note="guessed from the email domain; confirm with Blake before posting",
        )

    if search_fn is not None:
        found = search_fn(f"gym owner {email}" if email else stripe_customer.id)
        if found and found.get("name"):
            return GymResolution(
                gym_name=found["name"],
                owner_name="",
                confidence=INFERRED,
                source="web_search",
                website=found.get("website", ""),
                note="found via web search; confirm with Blake before posting",
            )

    return GymResolution(
        gym_name="", owner_name="", confidence=INFERRED, source="unresolved",
        note="could not resolve a gym name from the portal, Stripe, domain, or search",
    )
