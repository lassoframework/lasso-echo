"""
welcome_posts.py — auto welcome posts from brand-new paying clients.

Pipeline: a brand new paying client -> resolve the gym name -> scrape their logo ->
generate feed + story welcome posts -> SURFACE to the approval channel for Blake's
tap. Runs on every new client from here forward, plus a one-time 45-day backfill.

Hard rules (do not weaken):
  * Behind AGENT_WELCOME_POSTS_ENABLED, default OFF.
  * NOTHING publishes. A client account is never chat-published to; these are drafts
    held for approval on the LASSO side only.
  * "Brand new client" is defined by SUBSCRIPTION, not customer.created (which fires
    for sponsors/partners like InBody and Funding Metrics who never subscribe): the
    customer's FIRST-EVER subscription started inside the window, is active, is not
    canceled / set to cancel, and is on one of the confirmed core tiers.
  * Guards: exclude delinquent subscriptions (status checked at generation time),
    dedupe BY GYM not by customer, never welcome a gym twice (a kv ledger), and
    normalize the owner name to proper title case.
  * Confidence: a gym name from the portal or the Stripe business name is CONFIRMED;
    a name inferred from an email domain or a web search is INFERRED and is surfaced
    for a yes/no BEFORE any post is generated.

Stripe is read through an injectable reader (StripeReader) so the whole pipeline is
unit-testable offline; the default reader uses the Stripe SDK with a restricted,
read-only key (config.stripe_api_key()).
"""

import datetime
import hashlib
import os
import re

from . import config, db, welcome_templates as wt, website_scan

# Blake-confirmed core tiers (2026-08-04). Only a FIRST subscription on one of these
# makes a customer a brand new client for the welcome pipeline. The $250 weekly and
# the $99.99 SKU are intentionally excluded. Tier is stored on the gym record for
# later segmenting; it is NEVER shown on the public post.
CORE_TIERS = {
    "prod_UegCbUqO3fs1no": "Launch",   # $699
    "prod_UegGywD7BQpQUF": "Ascend",   # $999
    "prod_UegHHCMqpG7ajx": "Apex",     # $1,499
}

# the six kept welcome templates rotate so new gyms do not all look identical
ROTATION = ["T1", "T2", "T7", "T8", "T9", "T10"]

CONFIRMED, INFERRED = "CONFIRMED", "INFERRED"
LEDGER_PREFIX = "welcome_posted_"


# ==========================================================================
# Normalization helpers
# ==========================================================================

def normalize_owner(name):
    """Proper title case for an owner name. Stripe carries 'RYAN PARR' and
    'Just Estes'; both must come out clean ('Ryan Parr', 'Just Estes'). Keeps common
    intra-name capitals (McCoy, O'Brien) reasonable without overreaching."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name:
        return ""
    out = []
    for word in name.split(" "):
        if not word:
            continue
        low = word.lower()
        if low.startswith("mc") and len(low) > 2:
            out.append("Mc" + low[2:].capitalize())
        elif "'" in word:
            out.append("'".join(p.capitalize() for p in low.split("'")))
        elif "-" in word:
            out.append("-".join(p.capitalize() for p in low.split("-")))
        else:
            out.append(word.capitalize())
    return " ".join(out)


_FREEMAIL = frozenset([
    "gmail.com", "yahoo.com", "yahoo.ca", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "mac.com", "live.com", "msn.com",
    "aol.com", "protonmail.com", "proton.me",
])


def _domain(email_or_url):
    s = (email_or_url or "").strip().lower()
    if not s:
        return ""
    if "@" in s:
        s = s.split("@", 1)[1]
    s = re.sub(r"^https?://", "", s)
    s = s.split("/", 1)[0]
    s = re.sub(r"^www\.", "", s)
    return s


# common gym-word boundaries so a smashed domain reads as a name (INFERRED only)
_GYM_WORDS = ("crossfit", "fitness", "gym", "strength", "athletics", "training",
              "performance", "barbell", "wellness", "studio", "club", "health",
              "body", "fit", "iron", "forge", "method", "collective", "co",
              "boxing", "yoga", "pilates", "cycle", "run", "lab")


def gym_name_from_domain(domain):
    """Best-effort readable gym name from a bare domain (INFERRED, always confirmed
    by a human before use). 'birddogcrossfit.com' -> 'Bird Dog Crossfit'."""
    core = _domain(domain)
    if not core:
        return ""
    core = core.rsplit(".", 1)[0]                 # drop TLD
    core = re.sub(r"[-_]+", " ", core)
    if " " not in core:
        # greedily split known gym words off a smashed token
        tokens = []
        rest = core
        changed = True
        while changed and rest:
            changed = False
            for w in sorted(_GYM_WORDS, key=len, reverse=True):
                if rest.endswith(w) and len(rest) > len(w):
                    tokens.insert(0, w)
                    rest = rest[: -len(w)]
                    changed = True
                    break
        if rest:
            tokens.insert(0, rest)
        core = " ".join(tokens) if tokens else core
    # brand-correct capitalization for words that carry internal capitals
    caps_fix = {"crossfit": "CrossFit", "hyrox": "HYROX", "emom": "EMOM"}
    return " ".join(caps_fix.get(p, p.capitalize()) for p in core.split())


# ==========================================================================
# Classification: is this customer a BRAND NEW client?
# ==========================================================================

NEW = "NEW"
EXISTING_ADDING = "EXISTING_ADDING_PRODUCT"
IGNORED = "IGNORED"


def classify(customer, cutoff_ts, core_tiers=CORE_TIERS):
    """Classify a customer by their SUBSCRIPTION history.

    customer: {"id", "email", "name", "business_name", "website",
               "subs": [{"id","created"(epoch),"status","canceled_at","cancel_at",
                         "product_id"}]}
    Returns {"status": NEW|EXISTING_ADDING_PRODUCT|IGNORED, "reason", "tier",
             "tier_label", "start_date"(epoch)}.
    """
    subs = sorted(customer.get("subs", []), key=lambda s: s.get("created", 0))
    if not subs:
        return {"status": IGNORED, "reason": "no subscriptions (sponsor/partner)",
                "tier": None, "tier_label": None, "start_date": None}
    first = subs[0]
    tier_label = core_tiers.get(first.get("product_id"))
    in_window = first.get("created", 0) >= cutoff_ts
    canceled = bool(first.get("canceled_at") or first.get("cancel_at"))
    active = first.get("status") == "active" and not canceled

    base = {"tier": first.get("product_id"), "tier_label": tier_label,
            "start_date": first.get("created")}

    if in_window and active and tier_label:
        return {"status": NEW, "reason": "first-ever subscription in window, "
                f"active, {tier_label} tier", **base}
    if not in_window and any(s.get("created", 0) >= cutoff_ts for s in subs):
        return {"status": EXISTING_ADDING,
                "reason": "existing client (first sub predates the window) who added "
                          "a product in-window", **base}
    reasons = []
    if not in_window:
        reasons.append("first subscription predates the window")
    if in_window and not tier_label:
        reasons.append("first subscription is not a core tier "
                       "(sponsorship / weekly / SKU / one-off)")
    if in_window and not active:
        why = first.get("status")
        reasons.append(f"delinquent or inactive (status={why}"
                       + (", canceled" if canceled else "") + ")")
    return {"status": IGNORED, "reason": "; ".join(reasons) or "not a new client",
            **base}


# ==========================================================================
# Gym resolution + dedupe key
# ==========================================================================

def gym_dedupe_key(customer, portal_row=None):
    """The key two contacts at ONE gym must share so the gym is welcomed once. Portal
    account_key wins; else a non-freemail domain; else the customer id."""
    if portal_row and portal_row.get("account_key"):
        return "portal:" + portal_row["account_key"]
    dom = _domain(customer.get("email") or customer.get("website"))
    if dom and dom not in _FREEMAIL:
        return "domain:" + dom
    return "cust:" + str(customer.get("id"))


def resolve_gym(customer, portal_row=None, web_search=None):
    """Resolve the gym name + confidence. Order (first hit wins): portal record ->
    Stripe business name -> email-domain inference -> web search. Portal / business
    name are CONFIRMED; domain / search are INFERRED (surfaced for a yes/no first)."""
    dom = _domain(customer.get("email") or customer.get("website"))
    website = (customer.get("website") or (f"https://{dom}" if dom else "")).strip()
    owner = normalize_owner((portal_row or {}).get("owner_name")
                            or customer.get("name") or "")

    def result(name, confidence, source):
        return {"name": (name or "").strip(), "confidence": confidence,
                "source": source, "website": website, "owner": owner,
                "account_key": (portal_row or {}).get("account_key")}

    if portal_row and (portal_row.get("gym_name") or portal_row.get("display_name")):
        return result(portal_row.get("gym_name") or portal_row.get("display_name"),
                      CONFIRMED, "portal")
    bn = (customer.get("business_name") or "").strip()
    if bn:
        return result(bn, CONFIRMED, "stripe_business_name")
    if dom and dom not in _FREEMAIL:
        return result(gym_name_from_domain(dom), INFERRED, "email_domain")
    if web_search:
        found = web_search(customer)
        if found:
            return result(found, INFERRED, "web_search")
    return result("", INFERRED, "none")


# ==========================================================================
# Ledger (never welcome a gym twice) + template rotation
# ==========================================================================

def already_welcomed(gym_key):
    return bool(db.kv_get(LEDGER_PREFIX + gym_key, ""))


def mark_welcomed(gym_key, when=None):
    stamp = when or datetime.datetime.now(datetime.timezone.utc).isoformat()
    db.kv_set(LEDGER_PREFIX + gym_key, stamp)


def pick_template(gym_key):
    """Deterministic rotation across the kept templates so a gym always maps to the
    same template on a re-run, but the set as a whole varies gym to gym."""
    h = int(hashlib.sha1(gym_key.encode()).hexdigest(), 16)
    return ROTATION[h % len(ROTATION)]


# ==========================================================================
# Generate feed + story
# ==========================================================================

def generate_posts(template_id, gym_name, owner_name, logo_path, out_dir,
                   bg_client=None, cache_dir=None):
    """Render both sizes off the same design system. Returns {"feed":path,"story":path}."""
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", (gym_name or "gym").lower()).strip("_")
    out = {}
    for fmt in ("feed", "story"):
        suffix = "" if fmt == "feed" else "_story"
        p = os.path.join(out_dir, f"welcome_{safe}{suffix}.png")
        out[fmt] = wt.make_welcome(template_id, gym_name, owner_name, logo_path,
                                   format=fmt, out_path=p, bg_client=bg_client,
                                   cache_dir=cache_dir)
    return out


# ==========================================================================
# Default Stripe reader (SDK). Injectable; tests pass a fake.
# ==========================================================================

class StripeReader:
    """Reads customers + their full subscription history from Stripe with a
    restricted, read-only key. Returns plain dicts in the shape classify() expects.
    The key is read by name at call time and never logged."""

    def __init__(self, api_key=None):
        self._key = api_key or config.stripe_api_key()

    def available(self):
        return bool(self._key)

    def customers(self):
        import stripe
        stripe.api_key = self._key
        by_cust = {}
        # status='all' so we see canceled/past_due history and can find the FIRST sub
        subs = stripe.Subscription.list(
            status="all", limit=100,
            expand=["data.customer", "data.items.data.price"])
        for s in subs.auto_paging_iter():
            # Stripe v9 returns typed objects; use getattr with fallback, not .get()
            cust = getattr(s, "customer", None)
            cid = getattr(cust, "id", cust) if cust else None
            if not cid:
                continue
            items_obj = getattr(s, "items", None)
            items = getattr(items_obj, "data", []) or []
            product_id = None
            if items:
                price = getattr(items[0], "price", None)
                if price:
                    prod = getattr(price, "product", None)
                    product_id = getattr(prod, "id", prod) if prod else None
            meta = getattr(cust, "metadata", None) or {}
            # business_name is the Stripe metadata field only — never fall back to
            # cust.name or the gym_name would become the owner's personal name
            biz = getattr(meta, "get", lambda k, d=None: None)("business_name") or ""
            web = (getattr(meta, "get", lambda k, d=None: None)("website") or "")
            rec = by_cust.setdefault(cid, {
                "id": cid,
                "email": getattr(cust, "email", "") or "",
                "name": getattr(cust, "name", "") or "",
                "business_name": biz,
                "website": web,
                "subs": [],
            })
            rec["subs"].append({
                "id": getattr(s, "id", None),
                "created": getattr(s, "start_date", None) or getattr(s, "created", None),
                "status": getattr(s, "status", None),
                "canceled_at": getattr(s, "canceled_at", None),
                "cancel_at": getattr(s, "cancel_at", None),
                "product_id": product_id,
            })
        return list(by_cust.values())


def portal_logo_override(account_key):
    """A human-dropped logo for this gym wins over any scrape. Blake drops a file at
    <logo_dir>/overrides/<account_key>.(png|jpg|jpeg|webp); the first match is used.
    Returns a path or None."""
    base = os.path.join(website_scan.logo_dir(None), "overrides")
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = os.path.join(base, account_key + ext)
        if os.path.isfile(p):
            return p
    return None


def _portal_lookup(customer):
    """Best-effort match of a Stripe customer to an existing portal gym row, by an
    account_key derived from the email domain. Returns the gyms dict or None."""
    dom = _domain(customer.get("email") or customer.get("website"))
    if not dom:
        return None
    stem = re.sub(r"[^a-z0-9]+", "_", dom.rsplit(".", 1)[0]).strip("_")
    for row in db.gym_list():
        ak = (row.get("account_key") or "").lower()
        if ak.startswith(stem) or stem in ak:
            return row
    return None


# ==========================================================================
# Backfill orchestration
# ==========================================================================

def backfill(window_days=45, now=None, reader=None, scraper=None,
             portal_lookup=None, cache_dir=None, out_dir=None, bg_client=None):
    """Assemble the welcome roster for the last `window_days`. Returns a structured
    report; it does NOT post (surfacing is a separate step) and NEVER publishes.

    report = {
      "window_days", "included": [gym...], "excluded": [{customer, status, reason}],
      "collapsed": [{gym_key, customers}], "needs_confirmation": [gym...],
      "needs_logo": [gym...], "already_welcomed": [...],
    }
    Each included gym carries: name, confidence, source, owner, tier_label,
    start_date, gym_key, template, logo(status/source), posts{feed,story}.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff_ts = int((now - datetime.timedelta(days=window_days)).timestamp())
    reader = reader or StripeReader()
    scraper = scraper or website_scan.fetch_logo
    portal_lookup = portal_lookup or _portal_lookup
    out_dir = out_dir or os.path.join(cache_dir or wt._cache_dir(None), "welcome_client")

    report = {"window_days": window_days, "included": [], "excluded": [],
              "collapsed": [], "needs_confirmation": [], "needs_logo": [],
              "already_welcomed": [], "stripe_available": reader.available()}
    if not reader.available():
        report["error"] = ("no Stripe key (set STRIPE_API_KEY in Railway, restricted "
                            "read-only). Nothing read; roster not guessed.")
        return report

    # 1. classify every customer
    news = []
    for cust in reader.customers():
        cls = classify(cust, cutoff_ts)
        if cls["status"] != NEW:
            report["excluded"].append({"customer": cust.get("id"),
                                       "email": cust.get("email"),
                                       "status": cls["status"],
                                       "reason": cls["reason"]})
            continue
        news.append((cust, cls))

    # 2. dedupe by gym (two contacts at one gym => one post)
    seen = {}
    for cust, cls in news:
        prow = portal_lookup(cust)
        key = gym_dedupe_key(cust, prow)
        if key in seen:
            seen[key]["customers"].append(cust.get("id"))
            continue
        seen[key] = {"cust": cust, "cls": cls, "portal": prow,
                     "customers": [cust.get("id")], "gym_key": key}
    for key, g in seen.items():
        if len(g["customers"]) > 1:
            report["collapsed"].append({"gym_key": key, "customers": g["customers"]})

    # 3. resolve + guard + generate
    for key, g in seen.items():
        cust, prow = g["cust"], g["portal"]
        gym = resolve_gym(cust, prow)
        entry = {"gym_key": key, "name": gym["name"], "confidence": gym["confidence"],
                 "source": gym["source"], "owner": gym["owner"],
                 "website": gym["website"], "tier_label": g["cls"]["tier_label"],
                 "start_date": g["cls"]["start_date"],
                 "customers": g["customers"], "account_key": gym["account_key"]}

        if already_welcomed(key):
            report["already_welcomed"].append(entry)
            continue

        # Empty name = can't make a card, hold for manual input
        # INFERRED name from a real domain = generate card, Blake approves/skips in Slack
        if not gym["name"]:
            entry["template"] = pick_template(key)
            report["needs_confirmation"].append(entry)
            continue

        entry["template"] = pick_template(key)
        ak = gym["account_key"] or ("cust_" + re.sub(r"[^a-z0-9]+", "_",
                                                     str(cust.get("id")).lower()))
        # a human-dropped portal logo wins over any scrape
        override = portal_logo_override(ak)
        logo = scraper(gym["website"], ak, override_path=override, out_dir=None)
        entry["logo"] = {"status": logo.status, "source": logo.source,
                         "note": logo.note}
        if not logo.ok:
            report["needs_logo"].append(entry)
            continue

        entry["posts"] = generate_posts(entry["template"], gym["name"], gym["owner"],
                                        logo.path, out_dir, bg_client=bg_client,
                                        cache_dir=cache_dir)
        report["included"].append(entry)

    return report


# ==========================================================================
# Surface the roster to Slack (review only; NOTHING publishes)
# ==========================================================================

def _fmt_date(epoch):
    if not epoch:
        return "?"
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%d")


def surface_to_slack(report, poster, host_fn, channel=None):
    """Post the welcome roster to the approval channel: one message per included gym
    (feed inline, story threaded) with name, owner, confidence, and logo source, plus
    a roster summary of who was excluded / needs confirmation / needs a logo. Held for
    Blake's tap. NOTHING publishes; a client account is never published to from here.
    Returns a summary dict."""
    ch = channel or poster._channel

    def host(path):
        try:
            return host_fn(path, "lasso_welcome")
        except Exception:
            return None

    if report.get("error"):
        poster._chat_post(text="Welcome backfill blocked",
                          blocks=[{"type": "section", "text": {"type": "mrkdwn",
                                   "text": f"*Welcome backfill blocked:* {report['error']}"}}],
                          channel=ch)
        return {"posted": 0, "error": report["error"]}

    inc = report["included"]
    intro = (f"*New-client welcome posts* (last {report['window_days']} days)\n"
             f"{len(inc)} ready for approval, {len(report['needs_confirmation'])} need a "
             f"name confirm, {len(report['needs_logo'])} need a logo, "
             f"{len(report['excluded'])} excluded. Nothing is published; each is held "
             f"for your tap.")
    poster._chat_post(text="New-client welcome posts",
                      blocks=[{"type": "section",
                               "text": {"type": "mrkdwn", "text": intro}}], channel=ch)

    posted = 0
    for g in inc:
        feed_url = host(g["posts"]["feed"])
        story_url = host(g["posts"]["story"])
        head = (f"*{g['name']}*  ({g['confidence']} via {g['source']})\n"
                f"owner: {g['owner'] or 'unknown'}   template: {g['template']}   "
                f"logo: {g['logo']['source']}\n"
                f"_Approve / Edit / Skip — held for your tap, nothing published._")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
        if feed_url:
            blocks.append({"type": "image", "image_url": feed_url,
                           "alt_text": f"{g['name']} welcome feed"})
        resp = poster._chat_post(text=f"Welcome: {g['name']}", blocks=blocks, channel=ch)
        ts = (resp or {}).get("ts")
        if story_url:
            poster._chat_post(
                text=f"{g['name']} story",
                blocks=[{"type": "image", "image_url": story_url,
                         "alt_text": f"{g['name']} welcome story"},
                        {"type": "context", "elements": [
                            {"type": "mrkdwn", "text": f"{g['name']} STORY 1080x1920"}]}],
                channel=ch, thread_ts=ts)
        # stamp the ledger the moment a gym is actually surfaced, so a second
        # backfill run never re-welcomes it (idempotency is self-contained here,
        # not dependent on a downstream approval step). A dry run never reaches
        # this path, so it never stamps.
        mark_welcomed(g["gym_key"])
        posted += 1

    # roster summary so the logic is visible, not just the output
    def _lines(items, fmt):
        return "\n".join(fmt(x) for x in items) or "_none_"

    summary = (
        "*Roster logic*\n"
        f"*Needs name confirm (INFERRED):*\n"
        + _lines(report["needs_confirmation"],
                 lambda g: f"- {g['name'] or '(unknown)'} - {g['source']} - reply yes/no")
        + "\n\n*Needs a logo (drop one in the portal):*\n"
        + _lines(report["needs_logo"], lambda g: f"- {g['name']} - {g['logo']['note']}")
        + "\n\n*Excluded:*\n"
        + _lines(report["excluded"],
                 lambda e: f"- {e.get('email') or e['customer']} - {e['status']} - {e['reason']}")
    )
    poster._chat_post(text="Welcome roster logic",
                      blocks=[{"type": "section",
                               "text": {"type": "mrkdwn", "text": summary[:2900]}}],
                      channel=ch)
    return {"posted": posted, "needs_confirmation": len(report["needs_confirmation"]),
            "needs_logo": len(report["needs_logo"]), "excluded": len(report["excluded"])}
