"""
Auto welcome posts for new paying clients (the A+ loop). RUN BY HAND until
Blake arms AGENT_WELCOME_POSTS_ENABLED and wires a Railway cron to call it:

    /opt/venv/bin/python -m agent welcome-new-clients          # since last run
    /opt/venv/bin/python -m agent welcome-backfill --days 45   # one-time catch-up

Pipeline: new paying Stripe customer -> resolve gym (gym_resolve) -> scrape
logo (website_scan) -> generate feed (1080x1080) + story (1080x1920) (reusing
the kept welcome_templates set) -> ONE Slack approval card showing BOTH images
-> on Approve, publish BOTH the feed AND the story, to BOTH lasso_ig and
lasso_fb, through meta_publisher (the SAME Graph-API-direct lane every other
LASSO post already uses -- Zernio is a client-gym OAuth broker with no publish
call implemented anywhere in this repo, and is not involved here).

One card, four publishes on Approve. Under the hood this still holds FOUR real
per-target Drafts (IG feed, FB feed, IG story, FB story) in the normal
PendingStore -- the Slack card is a fifth, display-only "welcome_multi" Draft
that fans out to the other four when tapped. This means every existing gate
still applies per target: AGENT_PUBLISH_ENABLED (would_publish when OFF),
AGENT_STORIES_ENABLED (stories never actually post without it, even with
publish armed), and the approver check.

Guards, applied in this order:
  AT GENERATION TIME (build_roster):
    1. dedupe by GYM (welcome_ledger.gym_key), not by Stripe customer -- two
       contacts at one gym collapse to ONE welcome, reported as a collapse
    2. exclude a customer whose CURRENT subscription is delinquent or not an
       active paying status
    3. never welcome a gym twice (welcome_ledger.already_posted)
    4. an INFERRED gym name is NEVER generated into a post; it is surfaced to
       Blake as a plain yes/no confirmation request first
  RE-CHECKED AT APPROVE TIME (handle_welcome_approval), because minutes or
  hours may pass between a card landing and Blake tapping it:
    5. ledger dedupe again (never publish an already-published gym twice)
    6. consent: see the OPEN DECISION below
    7. subscription status again (a client can go delinquent between drafting
       and approving)

OPEN DECISION (flagging, not resolving -- same as the brand palette and
publish path already logged in PROGRESS.md): "consent" has no home yet. There
is no field anywhere in the portal/tenant scaffold for "this gym agreed to be
announced publicly." _consent_ok() below checks tenant.json for an explicit
`welcome_post_consent: true` and BLOCKS (does not fabricate a yes) when it is
absent -- including every gym resolved via Stripe/domain only (no portal
record at all). Blake needs to decide where this gets captured (at intake? a
portal checkbox?) before any gym can actually clear this gate.
"""

import hashlib
import os
import time

from . import config, gym_resolve, media_host, slack_surface, stripe_client, \
    tenants, welcome_ledger, welcome_templates

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


PUBLISH_ACCOUNTS = ("lasso_ig", "lasso_fb")  # LASSO's own accounts; never a client's


def _target_draft(draft_id, account_key, gym_name, path, url, is_story):
    """One REAL per-target Draft (a specific account + feed-or-story). Each of
    these is what actually reaches meta_publisher on approve; the Slack card
    itself is a separate, fifth "welcome_multi" Draft (see _primary_draft)."""
    import datetime as _dt

    from .accounts import get_account
    from .drafter import Draft, DraftStatus

    acct = get_account(account_key)
    day_key = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    return Draft(
        draft_id=draft_id, account_key=account_key,
        platform=(acct.platform if acct else "instagram"),
        caption=_welcome_caption(gym_name), hashtags=[],
        creative_path=path, creative_public_url=url or "", scheduled_for=day_key,
        status=DraftStatus.PENDING, day_key=day_key, draft_type="welcome",
        is_story=is_story,
    )


def _primary_draft(draft_id, gym_name, feed_path, feed_url):
    """The display-only card Draft: draft_type="welcome_multi" is what tells
    listener.py's button handler to fan out (see agent/listener.py) instead of
    publishing this Draft directly. Its own creative fields point at the feed
    image so a generic re-render (e.g. after an Edit) still shows something
    sane even before the custom two-image blocks are rebuilt."""
    import datetime as _dt

    from .drafter import Draft, DraftStatus

    day_key = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    return Draft(
        draft_id=draft_id, account_key="lasso_ig", platform="instagram",
        caption=_welcome_caption(gym_name), hashtags=[],
        creative_path="", creative_public_url=feed_url or "", scheduled_for=day_key,
        status=DraftStatus.PENDING, day_key=day_key, draft_type="welcome_multi",
    )


def welcome_multi_blocks(primary_draft_id, gym_name, owner_name, confidence,
                         source, logo_source, feed_url, story_url):
    """Block Kit for the ONE combined card: both images, then the standard
    approve/skip/edit actions (same action_ids listener.py's real Slack button
    handlers already listen for -- no new Slack wiring needed)."""
    header = (f"*Welcome post ready: {gym_name}*\n"
             f"Owner: {owner_name or '(not on file)'}  |  "
             f"Resolution: {confidence} via {source}\n"
             f"Logo: {logo_source or 'NONE (LOGO NOT FOUND)'}\n"
             f"Approving publishes the FEED to lasso_ig + lasso_fb and the "
             f"STORY to IG Stories + FB Stories, together.")
    blocks = [{"type": "header",
              "text": {"type": "plain_text", "text": f"Approve welcome: {gym_name}"}},
             {"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    if feed_url:
        blocks.append({"type": "image", "image_url": feed_url,
                       "alt_text": f"{gym_name} welcome feed post"})
    if story_url:
        blocks.append({"type": "image", "image_url": story_url,
                       "alt_text": f"{gym_name} welcome story"})
    blocks.append({"type": "actions",
                   "block_id": f"approve_block::{primary_draft_id}", "elements": [
        {"type": "button", "style": "primary",
         "text": {"type": "plain_text", "text": "Approve"},
         "action_id": "approve", "value": primary_draft_id},
        {"type": "button", "text": {"type": "plain_text", "text": "Edit"},
         "action_id": "edit", "value": primary_draft_id},
        {"type": "button", "style": "danger",
         "text": {"type": "plain_text", "text": "Skip"},
         "action_id": "skip", "value": primary_draft_id},
    ]})
    return blocks


def post_welcome_multi_card(poster, primary_draft_id, gym_name, owner_name,
                            confidence, source, logo_source, feed_url, story_url):
    """Post (or re-post, e.g. after an Edit) the one combined card. Returns the
    Slack API response dict."""
    blocks = welcome_multi_blocks(primary_draft_id, gym_name, owner_name,
                                  confidence, source, logo_source, feed_url, story_url)
    return poster._chat_post(text=f"Approve welcome: {gym_name}", blocks=blocks)


def generate_and_surface_gym(entry, poster=None, host_fn=None, template_id=None,
                             out_dir=None, cache_dir=None, store=None):
    """Generate feed + story for one INCLUDED, CONFIRMED roster entry and post
    ONE combined approval card for Blake's tap. Under the card sit four real
    per-target Drafts (IG/FB feed, IG/FB story), held un-carded in the same
    PendingStore, that the card's Approve fans out to (handle_welcome_approval).
    Nothing publishes here -- generation only ever DRAFTS; a real Meta call
    happens only from handle_welcome_approval, and only past every gate."""
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
    primary_id = f"wel_{base_hash}"
    target_ids = {
        "ig_feed_draft_id": (f"wel_{base_hash}_ig_feed", "lasso_ig", feed_path, feed_url, False),
        "fb_feed_draft_id": (f"wel_{base_hash}_fb_feed", "lasso_fb", feed_path, feed_url, False),
        "ig_story_draft_id": (f"wel_{base_hash}_ig_story", "lasso_ig", story_path, story_url, True),
        "fb_story_draft_id": (f"wel_{base_hash}_fb_story", "lasso_fb", story_path, story_url, True),
    }
    for field, (did, account_key, path, url, is_story) in target_ids.items():
        if not url:
            continue  # never draft a target with no public URL to publish from
        store.put(_target_draft(did, account_key, entry["gym_name"], path, url, is_story))

    primary = _primary_draft(primary_id, entry["gym_name"], feed_path, feed_url)
    store.put(primary)

    poster.post_notice(
        f"New client welcome -- {entry['gym_name']}: owner "
        f"{entry['owner_name'] or '(not on file)'}, resolved {entry['confidence']} "
        f"via {entry['source']}, logo {logo['source'] or 'NONE -- ' + logo['reason']}.")
    post_welcome_multi_card(poster, primary_id, entry["gym_name"], entry["owner_name"],
                            entry["confidence"], entry["source"], logo["source"] or "",
                            feed_url, story_url)

    welcome_ledger.record_posted(
        entry["gym_key"], entry["gym_name"], entry["owner_name"],
        entry["account_key"], entry["confidence"], entry["source"], tmpl_id,
        feed_url=feed_url or "", story_url=story_url or "",
        logo_source=logo["source"] or "",
        stripe_customer_id=entry.get("stripe_customer_id", ""),
        primary_draft_id=primary_id,
        ig_feed_draft_id=target_ids["ig_feed_draft_id"][0] if feed_url else "",
        fb_feed_draft_id=target_ids["fb_feed_draft_id"][0] if feed_url else "",
        ig_story_draft_id=target_ids["ig_story_draft_id"][0] if story_url else "",
        fb_story_draft_id=target_ids["fb_story_draft_id"][0] if story_url else "")
    return {"gym_key": entry["gym_key"], "posted": True, "primary_draft_id": primary_id,
           "feed_url": feed_url, "story_url": story_url}


# ---------------------------------------------------------------------------
# Approve-time guards + the fan-out itself
# ---------------------------------------------------------------------------

def _consent_ok(ledger_entry, base_dir=None):
    """See the OPEN DECISION in the module docstring. No fabricated yes: a gym
    with no portal record, or a portal record with no explicit
    welcome_post_consent: true, is held (not published)."""
    account_key = ledger_entry.get("account_key") or ""
    if not account_key:
        return False, ("no portal record to confirm consent (this gym resolved "
                       "via Stripe/domain only); Blake must confirm consent by "
                       "hand before this can publish")
    rec = tenants.load_tenant(account_key, base_dir)
    if not rec:
        return False, f"tenant record {account_key!r} not found; cannot confirm consent"
    if not rec.get("welcome_post_consent"):
        return False, ("no recorded consent to announce this client publicly "
                       "(tenant.json has no welcome_post_consent: true yet -- "
                       "OPEN DECISION, see welcome_new_clients.py docstring)")
    return True, ""


def _subscription_ok(ledger_entry, client=None):
    customer_id = ledger_entry.get("stripe_customer_id") or ""
    if not customer_id:
        return False, "no Stripe customer id on record; cannot verify the subscription is still active"
    client = client or stripe_client.default_client()
    if client is None:
        return False, f"Stripe read key not set (env {config.STRIPE_API_KEY_ENV}); cannot verify subscription"
    status = stripe_client.subscription_status(customer_id, client=client)
    if stripe_client.is_delinquent(status):
        return False, f"delinquent Stripe subscription ({status})"
    if not stripe_client.is_active_paying(status):
        return False, f"not an active paying subscription ({status or 'no subscription found'})"
    return True, ""


def handle_welcome_approval(kind, draft, actor_slack_id, client=None, store=None,
                            base_dir=None):
    """The fan-out for a "welcome_multi" primary Draft's Approve/Edit/Skip tap.
    Wired into listener.py's button handler (draft_type == "welcome_multi"),
    the exact same special-casing pattern already used for claim_promotion.
    Returns an approvals.ActionResult so the caller's Slack-update code path
    is identical to every other draft type."""
    from .accounts import get_account
    from .approvals import ActionResult, _is_approver, handle_action
    from .store import PendingStore

    store = store or PendingStore()
    entry = welcome_ledger.find_by_primary_draft_id(draft.draft_id)
    if entry is None:
        return ActionResult(ok=False, action=kind, draft_id=draft.draft_id,
                            detail="no welcome bundle found for this card "
                                   "(ledger lookup failed)")
    if not _is_approver(actor_slack_id, account=get_account(draft.account_key)):
        return ActionResult(ok=False, action=kind, draft_id=draft.draft_id,
                            detail=f"Denied: {actor_slack_id} is not the approver.")

    kind = (kind or "").lower()

    if kind == "skip":
        for field in welcome_ledger.BUNDLE_FIELDS:
            did = entry.get(field)
            if did:
                store.remove(did)
        welcome_ledger.mark_status(entry["gym_key"], "skipped")
        return ActionResult(ok=True, action="skip", draft_id=draft.draft_id,
                            detail=f"Skipped: {entry['gym_name']}. Nothing published.")

    if kind != "approve":
        return ActionResult(ok=False, action=kind, draft_id=draft.draft_id,
                            detail=f"unknown welcome action: {kind!r}")

    if entry.get("status") == "published":
        return ActionResult(ok=False, action="approve", draft_id=draft.draft_id,
                            detail=f"{entry['gym_name']} was already published "
                                   "(ledger dedupe). Nothing sent.")
    consent_ok, consent_reason = _consent_ok(entry, base_dir=base_dir)
    if not consent_ok:
        return ActionResult(ok=False, action="approve", draft_id=draft.draft_id,
                            detail=f"Held, nothing published: {consent_reason}")
    sub_ok, sub_reason = _subscription_ok(entry, client=client)
    if not sub_ok:
        return ActionResult(ok=False, action="approve", draft_id=draft.draft_id,
                            detail=f"Held, nothing published: {sub_reason}")

    targets = (("ig_feed_draft_id", "IG feed"), ("fb_feed_draft_id", "FB feed"),
              ("ig_story_draft_id", "IG story"), ("fb_story_draft_id", "FB story"))
    lines, any_fail = [], False
    for field, label in targets:
        did = entry.get(field)
        if not did:
            continue
        target = store.get(did)
        if target is None:
            lines.append(f"{label}: draft missing from the store (skipped)")
            any_fail = True
            continue
        try:
            res = handle_action("approve", target, actor_slack_id=actor_slack_id,
                                account=get_account(target.account_key))
        except Exception as e:
            lines.append(f"{label} ({target.account_key}): FAILED -- {type(e).__name__}: {e}")
            any_fail = True
            continue
        lines.append(f"{label} ({target.account_key}): {res.detail}")
        if res.ok:
            store.remove(did)
        else:
            any_fail = True

    store.remove(draft.draft_id)
    welcome_ledger.mark_status(entry["gym_key"], "published" if not any_fail else "partial")
    detail = f"{entry['gym_name']}:\n" + "\n".join(lines)
    return ActionResult(ok=not any_fail, action="approve", draft_id=draft.draft_id,
                        detail=detail)


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
