"""
Auto welcome posts for new paying clients (the A+ loop). RUN BY HAND until
Blake arms AGENT_WELCOME_POSTS_ENABLED and wires a Railway cron to call it:

    /opt/venv/bin/python -m agent welcome-new-clients          # since last run
    /opt/venv/bin/python -m agent welcome-backfill --days 45   # one-time catch-up

Pipeline: new paying Stripe customer -> resolve gym (gym_resolve) -> scrape
logo (website_scan) -> generate feed + story (welcome_templates.make_welcome,
reusing the kept template set) -> surface to Slack for Blake's tap
(welcome_ledger records the post; nothing here ever calls a Meta publish path).

Guards (Part D), applied in this order, every run:
  1. dedupe by GYM (welcome_ledger.gym_key), not by Stripe customer -- two
     contacts at one gym collapse to ONE welcome, reported as a collapse
  2. exclude a customer whose CURRENT subscription is delinquent or not an
     active paying status (checked at generation time, not just at creation)
  3. never welcome a gym twice (welcome_ledger.already_posted)
  4. an INFERRED gym name is NEVER generated into a post; it is surfaced to
     Blake as a plain yes/no confirmation request first
"""

import os
import time

from . import config, gym_resolve, media_host, slack_surface, stripe_client, \
    welcome_ledger, welcome_templates

DEFAULT_TEMPLATE_ID = "T1"


def _default_template_id():
    kept = welcome_templates.active_templates()
    ids = [t["id"] for t in kept]
    return DEFAULT_TEMPLATE_ID if DEFAULT_TEMPLATE_ID in ids else (ids[0] if ids else None)


def _collapse_by_gym(resolved):
    """resolved: [(stripe_customer, GymResolution), ...]. Collapses to one
    entry per gym_key, keeping the FIRST (earliest-created) customer as the
    representative and recording how many contacts collapsed into it."""
    order = []
    by_key = {}
    for customer, res in resolved:
        key = welcome_ledger.gym_key(res.gym_name, res.account_key)
        if not key:
            key = f"unresolved:{customer.id}"
        if key not in by_key:
            by_key[key] = {"key": key, "customer": customer, "resolution": res,
                          "collapsed_contacts": 1}
            order.append(key)
        else:
            by_key[key]["collapsed_contacts"] += 1
    return [by_key[k] for k in order]


def _roster_entry(gym, client):
    """Apply the delinquency + already-posted guards to one collapsed gym.
    Returns a roster dict with include/exclude reasoning, never mutates state."""
    customer = gym["customer"]
    res = gym["resolution"]
    key = gym["key"]
    entry = {
        "gym_key": key,
        "gym_name": res.gym_name,
        "owner_name": res.owner_name,
        "confidence": res.confidence,
        "source": res.source,
        "account_key": res.account_key,
        "website": res.website,
        "note": res.note,
        "collapsed_contacts": gym["collapsed_contacts"],
        "stripe_customer_id": customer.id,
        "include": False,
        "exclude_reason": "",
    }
    if res.source == "unresolved" or not res.gym_name:
        entry["exclude_reason"] = "could not resolve a gym name"
        return entry
    if welcome_ledger.already_posted(key):
        entry["exclude_reason"] = "already welcomed (ledger)"
        return entry
    status = stripe_client.subscription_status(customer.id, client=client)
    entry["subscription_status"] = status
    if stripe_client.is_delinquent(status):
        entry["exclude_reason"] = f"delinquent Stripe subscription ({status})"
        return entry
    if not stripe_client.is_active_paying(status):
        entry["exclude_reason"] = f"not an active paying subscription ({status or 'no subscription found'})"
        return entry
    entry["include"] = True
    return entry


def build_roster(since_ts, client=None, base_dir=None, search_fn=None):
    """The full roster for a since_ts window: one row per deduped gym, with
    include/exclude reasoning. Returns {"error": "..."} when Stripe is not
    reachable (no key set, or the read failed) -- never a silently-empty
    roster in that case."""
    client = client or stripe_client.default_client()
    if client is None:
        return {"error": f"Stripe read key not set (env {config.STRIPE_API_KEY_ENV}); "
                         "cannot resolve new paying clients."}
    customers = stripe_client.list_new_customers(since_ts, client=client)
    if customers is None:
        return {"error": "Stripe customer list read failed."}
    resolved = [(c, gym_resolve.resolve_gym(c, base_dir=base_dir, search_fn=search_fn))
               for c in customers]
    gyms = _collapse_by_gym(resolved)
    roster = [_roster_entry(g, client) for g in gyms]
    return {"roster": roster, "customers_seen": len(customers), "gyms_deduped": len(gyms)}


def _resolve_logo(entry, out_dir=None):
    """Blake's override wins if present; otherwise scrape the resolved website.
    Returns a dict: {"path": ... or None, "source": "override"|"scraped"|None,
    "reason": "" or a LOGO NOT FOUND explanation}."""
    from . import website_scan

    out_dir = out_dir or config.welcome_logo_dir()
    override_path = os.path.join(out_dir, f"{_safe_slug(entry['gym_key'])}_override.png")
    if os.path.isfile(override_path):
        return {"path": override_path, "source": "override", "reason": ""}
    if not entry.get("website"):
        return {"path": None, "source": None, "reason": "LOGO NOT FOUND: no website resolved"}
    scraped_path = os.path.join(out_dir, f"{_safe_slug(entry['gym_key'])}.png")
    result = website_scan.scrape_logo(entry["website"], scraped_path)
    if result["ok"]:
        return {"path": result["path"], "source": result["source"], "reason": ""}
    return {"path": None, "source": None, "reason": result["reason"]}


def _safe_slug(s):
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-") or "gym"


def _post_confirm_request(poster, entry):
    """An INFERRED gym name is surfaced as a plain yes/no confirmation -- no
    image is ever generated for an unconfirmed name."""
    text = (f"New client needs a name check before a welcome post: "
           f"*{entry['gym_name']}* (guessed via {entry['source']}, "
           f"{entry.get('note', '')}). Reply `confirm {entry['gym_key']}` to "
           f"generate the welcome post, or `correct {entry['gym_key']} <real name>`.")
    poster.post_notice(text)


def _welcome_caption(gym_name):
    return f"Welcome to the LASSO family, {gym_name}. Let's grow."


def _welcome_draft(draft_id, gym_name, path, url, is_story, account_key):
    """Build a Draft the SAME way the existing manual `welcome-client --post`
    command does (see _welcome_client in __main__.py): posted through the
    normal PendingStore + SlackPoster.post_approval_card path, so Approve /
    Edit / Skip are the REAL, already-wired reply protocol (approvals.py),
    not a parallel one-off. Publishing still requires AGENT_PUBLISH_ENABLED
    AND a human Approve, exactly like every other draft in this system."""
    import datetime as _dt

    from .drafter import Draft, DraftStatus

    day_key = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    return Draft(
        draft_id=draft_id, account_key=account_key or "lasso_ig",
        platform="instagram", caption=_welcome_caption(gym_name), hashtags=[],
        creative_path=path, creative_public_url=url or "", scheduled_for=day_key,
        status=DraftStatus.PENDING, day_key=day_key, draft_type="feed",
        is_story=is_story,
    )


def generate_and_surface_gym(entry, poster=None, host_fn=None, template_id=None,
                             out_dir=None, cache_dir=None, store=None):
    """Generate feed + story for one INCLUDED, CONFIRMED roster entry and post
    both as normal PENDING drafts (approval cards) to Slack for Blake's tap.
    Returns a result dict; records the ledger on a successful post. Nothing
    here calls a publish path -- that only ever happens if Blake taps Approve
    AND AGENT_PUBLISH_ENABLED is armed, same as every other draft."""
    import hashlib

    poster = poster or slack_surface.SlackPoster()
    host_fn = host_fn or (lambda path: media_host.host_media(path, "lasso_welcome"))
    tmpl_id = template_id or _default_template_id()

    if entry["confidence"] != gym_resolve.CONFIRMED:
        _post_confirm_request(poster, entry)
        return {"gym_key": entry["gym_key"], "posted": False,
               "reason": "INFERRED name held for confirmation"}

    if not config.hosting_enabled():
        poster.post_notice(
            f"Welcome post for {entry['gym_name']} could not be surfaced: "
            "AGENT_HOSTING_ENABLED is not set, so the creative has no public URL.")
        return {"gym_key": entry["gym_key"], "posted": False,
               "reason": "hosting disabled (AGENT_HOSTING_ENABLED)"}

    logo = _resolve_logo(entry, out_dir=out_dir)
    feed_path = welcome_templates.make_welcome(
        tmpl_id, entry["gym_name"], entry["owner_name"], logo["path"],
        cache_dir=cache_dir, format="feed")
    story_path = welcome_templates.make_welcome(
        tmpl_id, entry["gym_name"], entry["owner_name"], logo["path"],
        cache_dir=cache_dir, format="story")

    feed_url = host_fn(feed_path)
    story_url = host_fn(story_path)

    from .store import PendingStore
    store = store or PendingStore()

    base_hash = hashlib.sha1(f"welcome|{entry['gym_key']}|{tmpl_id}".encode()).hexdigest()[:12]
    feed_id = f"wel_{base_hash}"
    story_id = f"wel_{base_hash}_story"

    feed_draft = _welcome_draft(feed_id, entry["gym_name"], feed_path, feed_url,
                                False, entry["account_key"])
    store.put(feed_draft)
    poster.post_notice(
        f"New client welcome -- {entry['gym_name']}: owner "
        f"{entry['owner_name'] or '(not on file)'}, resolved {entry['confidence']} "
        f"via {entry['source']}, logo {logo['source'] or 'NONE -- ' + logo['reason']}.")
    poster.post_approval_card(feed_draft)

    if story_url:
        story_draft = _welcome_draft(story_id, entry["gym_name"], story_path,
                                     story_url, True, entry["account_key"])
        store.put(story_draft)
        poster.post_approval_card(story_draft)

    welcome_ledger.record_posted(
        entry["gym_key"], entry["gym_name"], entry["owner_name"],
        entry["account_key"], entry["confidence"], entry["source"], tmpl_id,
        feed_url=feed_url or "", story_url=story_url or "",
        logo_source=logo["source"] or "")
    return {"gym_key": entry["gym_key"], "posted": True,
           "feed_draft_id": feed_id, "story_draft_id": story_id if story_url else "",
           "feed_url": feed_url, "story_url": story_url}


def run_pipeline(since_ts, client=None, base_dir=None, search_fn=None,
                 poster=None, host_fn=None, template_id=None,
                 out_dir=None, cache_dir=None):
    """Build the roster for since_ts and generate+surface every INCLUDED gym.
    Returns {"roster": [...], "results": [...]} or {"error": "..."}. Behind
    config.welcome_posts_enabled() -- OFF is a documented no-op, matching every
    other flag in this codebase."""
    if not config.welcome_posts_enabled():
        return {"error": "AGENT_WELCOME_POSTS_ENABLED is OFF; nothing run."}
    roster = build_roster(since_ts, client=client, base_dir=base_dir, search_fn=search_fn)
    if roster.get("error"):
        return roster
    results = []
    for entry in roster["roster"]:
        if not entry["include"]:
            continue
        results.append(generate_and_surface_gym(
            entry, poster=poster, host_fn=host_fn, template_id=template_id,
            out_dir=out_dir, cache_dir=cache_dir))
    return {"roster": roster["roster"], "results": results}


def run_backfill(days=None, client=None, base_dir=None, search_fn=None,
                 poster=None, host_fn=None, template_id=None,
                 out_dir=None, cache_dir=None, now_ts=None):
    """Part E: one-time catch-up over new paying clients from the last `days`
    days (default config.WELCOME_BACKFILL_DAYS, 45). Same guards, same flag
    gate, same everything as run_pipeline -- this is just its since_ts."""
    days = days if days is not None else config.WELCOME_BACKFILL_DAYS
    now_ts = now_ts if now_ts is not None else time.time()
    since_ts = int(now_ts - days * 86400)
    return run_pipeline(since_ts, client=client, base_dir=base_dir,
                        search_fn=search_fn, poster=poster, host_fn=host_fn,
                        template_id=template_id, out_dir=out_dir, cache_dir=cache_dir)
