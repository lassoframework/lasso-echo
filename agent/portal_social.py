"""
portal_social.py: the token-scoped client portal endpoints (Part B).

Part A shipped the per-gym calendar ENGINE (gym_calendar_queue), the 3-tier
collision rule, approval-surface routing, and baseline storage. Part B is the HTTP
CONTRACT the client portal calls, all behind the SAME master flag
AGENT_PORTAL_SOCIAL_ENABLED (default OFF). Flag OFF -> every handler here is inert
and returns a disabled response, so the service is byte-for-byte its current self.

Endpoints (each token->account_key resolved in intake_web BEFORE these handlers):
  GET  /portal/<token>/social              -> the month calendar for THIS gym.
       Additive signal fields for the red upload banner: awaiting_media (bool, True when
       a CLIENT gym has no posts because Echo is waiting on its uploaded media) and
       upload_url (str, the per-gym tokenized upload link when awaiting_media, else "").
       The LASSO gym is NEVER flagged awaiting_media.
  POST /portal/<token>/posts/<id>/approve  -> idempotent approve
  POST /portal/<token>/posts/<id>/edit     -> note; re-runs the fabrication gate (422 on fail)
  POST /portal/<token>/posts/<id>/deny     -> reason; decrements the 15/month recreate budget (409 when out)
  POST /portal/<token>/posts/<id>/kill     -> permanent, free, requires confirm=true
  POST /portal/<token>/autonomy            -> flip per-account autonomy; ON auto-approves
                                              every currently-pending post + future posts
  GET  /portal/<token>/metrics             -> the Part D report SHAPE (null values until Part C/D)

THREE HARD GATES on every action + read route:
  1. AGENT_PORTAL_SOCIAL_ENABLED must be ON, else the route is disabled (the HTTP
     layer 404s a disabled route; the handler itself returns a disabled marker).
  2. The gym's Stripe SOCIAL PRODUCT must be ACTIVE, else 402 + an empty-state
     payload (never a live calendar, never a fabricated connection).
  3. TOKEN ISOLATION: a draft is acted on ONLY when it belongs to THIS account_key.
     gym A's token can never read or act on gym B's calendar, drafts, budget, or
     metrics. Every handler re-checks draft.account_key == account_key AFTER load,
     because store.get(draft_id) is not account-scoped on its own.

Actions delegate to portal_approvals (which delegates to approvals.handle_action) so
the portal and Slack act on the SAME draft records, audit trail, and brain signals.
No new publish path: approve routes a draft through the existing gated publish.

HARD COPY RULES (grep-asserted in tests): no em/en/hyphen dashes and never the word
"vendor" in any client-facing string here. Verified stats only.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

from . import config, db as _db
from . import portal_approvals as _pa
from . import portal_calendar_store as _pcs
from .portal_visibility import client_visible as _client_visible
from . import rotation as _rotation
from .drafter import DraftStatus


# The server-enforced recreate budget: 30 denies per calendar month per gym (raised
# from 15, Blake 2026-09-08). This is NOT read from tenant data (which can default
# to zero); Part B guarantees every social gym the same 30. A deny burns one unit;
# the 31st deny in a month is refused with 409 so the gym asks for a fresh concept
# instead of burning the queue.
MONTHLY_RECREATE_BUDGET = 30


def _poster_evidence_is_current(variant, visual_writer_prepare, cache=None):
    """Read-only parity check for the writer's poster-edge evidence contract.

    The durable writer repeats this check before its scene RPC and calendar
    mutation. Swap callers run it first so stale or mismatched proof cannot
    consume a local reservation or stamp a Drive asset that never reaches the
    calendar.
    """
    image_url = (variant or {}).get("image_url")
    poster_url = (variant or {}).get("thumbnail_url")
    if not poster_url or poster_url == image_url:
        return True
    evidence = (variant or {}).get("poster_render_evidence")
    if not isinstance(evidence, dict):
        return False
    observed = cache if cache is not None else {}

    def exact(url, role):
        if url not in observed:
            observed[url] = visual_writer_prepare._exact_bytes(
                url, visual_writer_prepare._bytes_for_url, role)
        return observed[url]

    try:
        image_bytes = exact(image_url, "poster source")
        poster_bytes = exact(poster_url, "poster")
        expected = {
            "source_exact_url": image_url,
            "delivered_exact_url": poster_url,
            "source_fingerprint": visual_writer_prepare._md5(image_bytes),
            "delivered_fingerprint": visual_writer_prepare._md5(poster_bytes),
            "source_byte_length": len(image_bytes),
            "delivered_byte_length": len(poster_bytes),
        }
        return (all(evidence.get(key) == value for key, value in expected.items())
                and evidence.get("operation") in ("render", "reburn", "rehost"))
    except Exception:  # noqa: BLE001 - unreadable or changed proof must fail closed
        return False


# ==========================================================================
# disabled + empty-state responses (kept identical in shape to the live ones)
# ==========================================================================

def _disabled(route):
    """AGENT_PORTAL_SOCIAL_ENABLED is OFF: the route is dark. The HTTP layer turns
    this into a 404 so a dark feature is indistinguishable from an absent one."""
    return 404, {"error": "portal social is not enabled", "route": route}


def _empty_calendar(account_key, month):
    """The 402 empty state for a gym with no ACTIVE social product: a well-formed,
    empty calendar the portal can render as "your social plan is not active yet",
    never a live calendar and never a fabricated post."""
    return {
        "account_key": account_key,
        "month": month,
        "active": False,
        "posts": [],
        "recreate_budget": {"limit": MONTHLY_RECREATE_BUDGET, "used": 0,
                            "remaining": MONTHLY_RECREATE_BUDGET},
        "low_creative": False,
        "days_remaining": None,
    }


# ==========================================================================
# Stripe: is this gym's SOCIAL product ACTIVE?
# ==========================================================================

# The Stripe product id for the client-social subscription. Read by NAME from env so
# it is set by hand in Railway and never hard-coded. Empty => no product configured,
# so no gym reads as active (fail closed: a paid feature never opens without its
# product id set).
SOCIAL_PRODUCT_ID_ENV = "STRIPE_SOCIAL_PRODUCT_ID"


def social_product_id():
    return (os.environ.get(SOCIAL_PRODUCT_ID_ENV) or "").strip()


class StripeSocialReader:
    """Reads whether a gym holds an ACTIVE subscription to the social product, keyed
    by the gym's stored Stripe customer id. Restricted read-only key, read by name at
    call time, never logged. Injectable so the whole surface is offline-testable."""

    def __init__(self, api_key=None):
        self._key = api_key or config.stripe_api_key()

    def available(self):
        return bool(self._key)

    def social_active(self, customer_id, product_id):
        """True iff the customer has a subscription in an active-billing state whose
        price points at the social product. RAISES on a real Stripe/network error so
        the caller fails closed (402), never opens a paid feature on a flaky read."""
        import stripe
        stripe.api_key = self._key
        subs = stripe.Subscription.list(
            customer=customer_id, status="all", limit=100,
            expand=["data.items.data.price"])
        for s in subs.auto_paging_iter():
            status = getattr(s, "status", None)
            if status not in ("active", "trialing", "past_due"):
                continue
            items_obj = getattr(s, "items", None)
            items = getattr(items_obj, "data", []) or []
            for it in items:
                price = getattr(it, "price", None)
                if not price:
                    continue
                prod = getattr(price, "product", None)
                pid = getattr(prod, "id", prod) if prod else None
                if pid and str(pid) == str(product_id):
                    return True
        return False

    def echo_active(self, customer_id, products, base):
        """Echo products only; explicitly scoped subscriptions cannot cross gyms."""
        import stripe
        from .intake_web import _supabase_token_gym
        stripe.api_key = self._key
        subs = stripe.Subscription.list(customer=customer_id, status="all", limit=100,
                                        expand=["data.items.data.price"])
        gym = None
        looked_up = False
        for sub in subs.auto_paging_iter():
            if sub.get("status") not in ("active", "trialing", "past_due"):
                continue
            matches = False
            for item in (sub.get("items") or {}).get("data", []):
                product = (item.get("price") or {}).get("product")
                product = product.get("id") if isinstance(product, dict) else product
                matches = matches or product in products
            if not matches:
                continue
            metadata = sub.get("metadata") or {}
            sub_gym = metadata.get("gym_id") or metadata.get("gymId")
            if sub_gym:
                if not looked_up:
                    gym = _supabase_token_gym(base)
                    looked_up = True
                if gym is None:
                    raise RuntimeError("Echo subscription gym mapping unavailable")
                if gym.get("echo_account_key") != base or gym.get("gym_id") != sub_gym:
                    continue
            return True
        return False


def _stripe_customer_id(account_key):
    """The gym's Stripe customer id from its gyms row, or None. Never provisions."""
    row = _db.gym_get(account_key) or {}
    return (row.get("stripe_customer_id") or "").strip() or None


def is_social_active(account_key, reader=None):
    """True iff this gym has an ACTIVE social-product subscription. Fails CLOSED:
    no product id configured, no customer id on the gym, no Stripe key, or any read
    error => not active (the portal gets a clean 402 empty state, never a live
    calendar). A reader is injectable for tests.

    EXCEPTION: when billing is delegated to the portal (AGENT_SOCIAL_BILLING_DELEGATED),
    the portal has already enforced the subscription/entitlement before calling Echo,
    so Echo trusts that gate and does not re-check Stripe here. The token auth and the
    AGENT_PORTAL_SOCIAL_ENABLED flag still gate every request."""
    if config.social_billing_delegated():
        return True
    product_id = social_product_id()
    if not product_id:
        return False
    customer_id = _stripe_customer_id(account_key)
    if not customer_id:
        return False
    reader = reader or StripeSocialReader()
    if not reader.available():
        return False
    try:
        return bool(reader.social_active(customer_id, product_id))
    except Exception:
        return False  # fail closed: a paid feature never opens on a flaky read


# ==========================================================================
# server-enforced recreate budget (15 / calendar month / gym)
# ==========================================================================

def _budget_key(account_key, now=None):
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    return f"portal_recreate_spent_{account_key}_{month}"


def recreate_spent(account_key, now=None):
    """How many recreates (denies) this gym has burned this calendar month."""
    try:
        return int(_db.kv_get(_budget_key(account_key, now)) or 0)
    except (TypeError, ValueError):
        return 0


def recreate_remaining(account_key, now=None):
    """Units left in this gym's month budget (never negative)."""
    return max(0, MONTHLY_RECREATE_BUDGET - recreate_spent(account_key, now))


def spend_recreate(account_key, now=None):
    """Burn one unit of THIS gym's month budget. Returns True and counts the spend,
    or False when the budget is exhausted (the caller returns 409). Server-enforced:
    the count lives in the shared kv store, scoped to (account_key, month), so a
    client cannot bypass it. Isolation: the key carries the account_key, so gym A's
    spend never touches gym B's budget."""
    spent = recreate_spent(account_key, now)
    if spent >= MONTHLY_RECREATE_BUDGET:
        return False
    _db.kv_set(_budget_key(account_key, now), str(spent + 1))
    return True


def _budget_state(account_key, now=None):
    used = recreate_spent(account_key, now)
    return {"limit": MONTHLY_RECREATE_BUDGET, "used": used,
            "remaining": max(0, MONTHLY_RECREATE_BUDGET - used)}


def reset_recreate_budget(account_key, now=None):
    """Operator action (D72, the FIXER's ops lane): zero THIS gym's spend for the current
    month so the portal's deny / recreate-caption buttons work again. Idempotent -- a
    second call on an already-zero month changes nothing. Returns {before, after}; the key
    carries the account_key, so gym A's reset never touches gym B."""
    before = _budget_state(account_key, now)
    if before["used"]:
        _db.kv_set(_budget_key(account_key, now), "0")
    return {"before": before, "after": _budget_state(account_key, now)}


# ==========================================================================
# GET /portal/<token>/social  -> month calendar for THIS gym
# ==========================================================================

_MONTH_DAYS = {  # non-leap; February corrected below
    1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def _days_in_month(year, month):
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        return 29
    return _MONTH_DAYS[month]


def _low_creative_and_days(account_key, month, today=None):
    """(low_creative, days_remaining) for the month.

    days_remaining: whole days left in the calendar month from today (0 on the last
    day; None when today is not inside this month). low_creative: the gym's calendar
    has no queued (unserved) row left for the rest of the month, so the client is
    told the queue is running low. A gym with plenty of queued posts is never flagged.
    """
    from . import gym_calendar_queue as _gcq
    today = today or datetime.now(timezone.utc).date()
    y, m = int(month[:4]), int(month[5:7])
    days_remaining = None
    if today.year == y and today.month == m:
        days_remaining = _days_in_month(y, m) - today.day

    # queued rows for THIS gym in THIS month that have not served yet
    prefix = month + "-"
    queued_ahead = 0
    try:
        with _gcq._conn() as conn:
            rows = conn.execute(
                "SELECT day_key, status FROM gym_calendar_queue "
                "WHERE account_key=? AND day_key LIKE ?",
                (account_key, prefix + "%")).fetchall()
        for r in rows:
            if (r["status"] or "queued") == "queued":
                queued_ahead += 1
    except Exception:
        queued_ahead = 0
    low_creative = queued_ahead == 0
    return low_creative, days_remaining


def _calendar_post(row, is_lasso=False):
    """One gym_calendar_queue row folded into the portal post shape. Never invents a
    caption, stat, or connection; empty fields stay empty (Part A leaves content to a
    later phase). format is derived from is_story only (no fabricated metadata)."""
    return {
        "day_key": row.get("day_key"),
        "status": row.get("status") or "queued",
        "pillar": row.get("pillar") or "",
        "format": "story" if row.get("is_story") else "feed",
        "image_public_url": row.get("feed_url") or row.get("story_url") or "",
        # A LASSO story shows the image ONLY (Blake 2026-08-18): its caption is burned onto
        # the media, so the display caption is blanked for LASSO's own dogfood calendar.
        # CLIENT gyms KEEP their story caption (the owner needs to read/approve it) — blanking
        # it for a gym made the portal show an "Echo is writing" placeholder. The raw caption
        # always survives on the row for the edit / re-burn / publish paths.
        "caption": "" if (is_lasso and row.get("is_story")) else (row.get("caption") or ""),
    }


def _content_calendar_post(row, is_lasso=False):
    """One shared content_calendar row folded into the portal post shape. Carries a
    STABLE id (content_calendar.id) that the portal POSTs back to /posts/<id>/... .
    format is the row's own 'feed'/'story' value (never derived); a row with no format
    stays 'feed'. No field is invented: empty caption / image stay empty strings."""
    fmt = (row.get("format") or "").strip().lower()
    if fmt not in ("feed", "story"):
        fmt = "feed"
    # Go-live time: the stored stamp when present; else SYNTHESIZED from the row's own
    # deterministic slot (the same pure function the publisher stamps from), so a
    # pending/future post shows its real publish time before its day arrives. Never
    # fabricated: it is exactly the time the row will go out.
    scheduled_at = row.get("scheduled_at")
    if not scheduled_at:
        try:
            from .calendar_autopublish import scheduled_iso_for_row
            scheduled_at = scheduled_iso_for_row(row) or None
        except Exception:
            scheduled_at = None
    post = {
        "id": row.get("id"),
        "day_key": row.get("post_date"),
        "status": row.get("status") or "",
        "pillar": row.get("pillar") or "",
        "format": fmt,
        # WHICH page this row posts to (instagram|facebook) — a feed cross-posted to
        # IG + FB is two rows; without this the portal renders two identical cards
        # with no way to tag them.
        "platform": (row.get("account") or "").strip().lower(),
        # DISPLAY image: for a VIDEO row this is the hosted POSTER FRAME
        # (content_calendar.thumbnail_url) so the calendar shows a real frame instead
        # of a blank card — no portal change needed, because this field is display-only
        # (the publisher reads the DB image_url column, the actual video, directly).
        # An image row is unchanged.
        "image_public_url": (row.get("thumbnail_url")
                             or row.get("image_url") or ""),
        # video|image, from the underlying media URL's extension. Lets the portal upgrade
        # a video card to a real <video> player + play badge over the poster.
        "media_kind": _media_kind(row.get("image_url") or ""),
        # the actual video URL (present only for video rows) for that <video> upgrade.
        "video_url": (row.get("image_url") or "")
                     if _media_kind(row.get("image_url") or "") == "video" else None,
        # LASSO story = image only in the portal: its caption is burned onto the media, so the
        # display caption is blanked for LASSO's OWN calendar (Blake 2026-08-18). CLIENT gyms
        # KEEP their story caption so the owner can read/approve it. Row caption untouched for
        # the edit / re-burn / publish paths.
        "caption": "" if (is_lasso and fmt == "story") else (row.get("caption") or ""),
        "scheduled_at": scheduled_at,
        # Publish record (display only): when it actually went out + the vendor post id.
        "published_at": row.get("published_at"),
        "late_post_id": row.get("late_post_id"),
        # Needs-media hold signal: nonempty when this row was staged WITHOUT media
        # (e.g. a Story whose reviewed 9:16 render/hosting failed). The portal shows
        # the truthful reason; approval + every publish lane reject the row while it
        # is set or the image is blank.
        "media_not_ready_reason": (row.get("media_not_ready_reason") or ""),
        "needs_media": bool((row.get("media_not_ready_reason") or "").strip())
                       or not (row.get("image_url") or "").strip(),
    }
    # gym_id scopes the hosted fallback card (media_host tenant isolation); the portal
    # post shape itself is unchanged (no new keys).
    return _with_display_image(post, tenant=row.get("gym_id"))


from .media_types import VIDEO_EXTS as _VIDEO_URL_EXTS   # ONE definition (audit D1)


def _media_kind(url):
    """'video' or 'image' from the media URL's extension (query string ignored)."""
    path = (url or "").split("?", 1)[0].lower()
    return "video" if path.endswith(_VIDEO_URL_EXTS) else "image"


def _with_display_image(post, tenant=None):
    """No-creative fallback hook (AGENT_NO_CREATIVE_FALLBACK, default OFF). Flag OFF ->
    the post is returned byte-for-byte (current behavior). Flag ON and the row has NO
    usable creative image -> its image_public_url degrades to a clean website-style
    infographic rendered from the post's OWN approved caption / pillar and HOSTED, so the
    portal is served a PUBLIC url it can actually display (never a blank card, never a
    fabricated photo, never a local path). A row with a usable image is untouched; a row
    with no approved text, or when hosting is unavailable, is also untouched (empty stays
    empty, the portal shows its existing empty state). Thin + isolated: this only decides
    the DISPLAY image, it never publishes and touches no other field or gate."""
    if not config.no_creative_fallback_enabled():
        return post
    if (post.get("image_public_url") or "").strip():
        return post
    from . import no_creative_fallback as _ncf
    fallback = _ncf.display_image_for(post, tenant=tenant)
    if fallback:
        post = dict(post)
        post["image_public_url"] = fallback
    return post


def _is_lasso_gym(account_key):
    """True for the LASSO gym itself (its own dogfood calendar). LASSO's OWN calendar
    is allowed to be infographic-driven and is NEVER flagged awaiting_media. Every LASSO
    key starts with 'lasso' (e.g. 'lasso', 'lasso_ig', 'lasso-framework-llc'); a client
    gym uses its own slug and never carries this prefix."""
    return str(account_key or "").strip().lower().startswith("lasso")


def _awaiting_media_signal(account_key, posts):
    """The red-banner SIGNAL for the portal. Returns (awaiting_media, upload_url):

      * awaiting_media (bool): True when this is a CLIENT gym (NOT the LASSO gym) AND it
        has NO posts in the calendar (Echo is WAITING on the gym's uploaded media before
        it builds anything). False for the LASSO gym always, and False whenever the gym
        has any posts.
      * upload_url (str): when awaiting_media is True, the gym's per-gym tokenized upload
        link (ghl_intake.upload_link_for, placeholder-safe); else "".

    Additive: this only computes the two signal fields; it never filters or mutates
    posts and never publishes."""
    if _is_lasso_gym(account_key) or (posts or []):
        return False, ""
    from . import ghl_intake as _ghl
    base_key = _base_of_account(account_key)
    try:
        upload_url = _ghl.upload_link_for(base_key) or ""
    except Exception:
        upload_url = ""
    return True, upload_url


def _base_of_account(account_key):
    """The tenant base for an account key ('gritx_ig' -> 'gritx'). The /social handler's
    account_key is already the tenant base in the live content_calendar path, but strip a
    stray _ig/_fb suffix defensively so the upload link is minted for the tenant."""
    key = str(account_key or "").strip()
    for suffix in ("_ig", "_fb"):
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def _neutral_media_bridge_state():
    """A shared-state read failed, so never claim an inactive runway."""
    return (
        {"active": None, "depleted_on": None, "dates": [],
         "drafts_need_review": None, "status": "unknown"},
        {"status": "unknown", "delivery_confirmed": False},
    )


def _media_bridge_payload(state, notice, base, *, now=None):
    """Normalize worker-local or shared snapshots to the stable portal contract."""
    if state:
        from .calendar_autopublish import _local_now
        today = _local_now(now, config.posting_timezone_for(base)).date()
        active = today.isoformat() <= state["end"]
        fallback = {"active": active, "episode_id": state["id"],
                    "depleted_on": state["depleted_on"],
                    "dates": [state["start"], state["end"]] if active else [],
                    "drafts_need_review": active}
    else:
        active = False
        fallback = {"active": False, "depleted_on": None, "dates": [],
                    "drafts_need_review": False}

    # An expired episode is historical, not proof of a current client notice.
    current = state if state and active else None
    if not current or (notice or {}).get("episode_id") != current["id"]:
        notice = None
    status = notice.get("status") if notice else "none"
    if status not in {"none", "unresolved", "ready", "sent"}:
        status = "unresolved"
    return fallback, {
        "status": status,
        "episode_id": current["id"] if current else None,
        "created_at": notice.get("created_at") if notice else None,
        "delivery_confirmed": bool(status == "sent" and notice.get("ts")),
    }


def _shared_media_bridge_payload(fallback, notice, base, *, now=None):
    """Use the store's already-validated portal projection without local dates."""
    if fallback is None:
        rendered = {"active": False, "depleted_on": None, "dates": [],
                    "drafts_need_review": False}
        current_id = None
    elif not isinstance(fallback, dict):
        raise ValueError("invalid shared fallback episode")
    else:
        from .calendar_autopublish import _local_now
        dates = fallback.get("dates") or []
        today = _local_now(now, config.posting_timezone_for(base)).date().isoformat()
        active = bool(fallback.get("active") and dates and today <= dates[-1])
        rendered = {
            "active": active,
            "episode_id": fallback.get("episode_id"),
            "depleted_on": fallback.get("depleted_on"),
            "dates": dates if active else [],
            "drafts_need_review": bool(fallback.get("drafts_need_review") and active),
        }
        if fallback.get("status") == "unknown":
            rendered["status"] = "unknown"
        current_id = rendered["episode_id"] if rendered["active"] else None

    if not isinstance(notice, dict):
        raise ValueError("invalid shared notice state")
    if rendered.get("status") == "unknown":
        return rendered, {"status": "unknown", "delivery_confirmed": False}
    if current_id is None or notice.get("episode_id") != current_id:
        return rendered, {"status": "none", "episode_id": None,
                          "created_at": None, "delivery_confirmed": False}
    status = notice.get("status")
    if status not in {"none", "unresolved", "ready", "sent", "unknown"}:
        status = "unresolved"
    return rendered, {
        "status": status,
        "episode_id": current_id,
        "created_at": notice.get("created_at"),
        "delivery_confirmed": bool(notice.get("delivery_confirmed")),
    }


def _local_media_bridge_payload(base, *, now=None):
    """The historical single-service SQLite read, retained for local deployments."""
    from . import media_bridge
    state = media_bridge.episode(base, now=now, create=False)
    notices = media_bridge.notice_status(base) if state else []
    notice = next((row for row in notices if row.get("episode_id") == state["id"]), None) \
        if state else None
    return _media_bridge_payload(state, notice, base, now=now)


def _media_bridge_status(account_key, *, now=None):
    """Read-only tenant media status for the portal. Unknown is never zero."""
    base = _base_of_account(account_key)
    from . import gym_media_selector as selector, media_bridge
    from .media_source_store import default_store

    try:
        store = default_store()
        if not store.available():
            raise RuntimeError("media store unavailable")
        assets = store.list_assets(base)
        pending = sum((a.get("review_status") or "pending_review") == "pending_review"
                      for a in assets)
        publishable = sum(selector.is_usable(a) for a in assets)
        review = {"status": "awaiting" if pending else "ready",
                  "pending_review_count": pending,
                  "publishable_count": publishable}
    except Exception:
        review = {"status": "unknown", "pending_review_count": None,
                  "publishable_count": None, "reason": "media inventory unavailable"}

    try:
        snapshot = media_bridge.shared_snapshot(base)
        if snapshot is media_bridge._SHARED_RUNWAY_UNAVAILABLE:
            if media_bridge.local_runway_fallback_enabled():
                fallback, notice_state = _local_media_bridge_payload(base, now=now)
            else:
                fallback, notice_state = _neutral_media_bridge_state()
        elif snapshot is None:
            # A shared read cannot distinguish a missing row from an outage or a
            # not-yet-projected worker transition.  Never call that runway clear.
            fallback, notice_state = _neutral_media_bridge_state()
        elif not isinstance(snapshot, dict):
            raise ValueError("invalid shared runway snapshot")
        else:
            fallback, notice_state = _shared_media_bridge_payload(
                snapshot.get("fallback_episode"), snapshot.get("notice_state"),
                base, now=now,
            )
    except Exception:
        fallback, notice_state = _neutral_media_bridge_state()

    try:
        from . import ghl_intake
        upload_url = ghl_intake.upload_link_for(base) or ""
    except Exception:
        upload_url = ""
    return {"media_review": review, "fallback_episode": fallback,
            "notice_state": notice_state,
            "upload_action": {"url": upload_url, "label": "Upload media",
                              "received_means_indexed": False}}


# ---- B12: a post the client already rejected must leave their calendar ----------
# THE DEFECT: a client denies a post, Echo issues a replacement (deny backfill,
# client_month_run.backfill_denied_slots), and the ORIGINAL row stays on the calendar
# forever. backfill_denied_slots is INSERT ONLY and nothing anywhere transitions a
# denied row out of 'denied' -- portal_calendar_store._WIPEABLE_STATUSES deliberately
# treats it as human owned so a rebuild cannot destroy it. Meanwhile the client render
# carried exactly ONE status filter, `!= "coach_review"`, so denied / killed / deleted
# rows were mapped into cards and shipped to the owner beside their replacements.
#
# MEASURED ON PRODUCTION 2026-09-05, September book: LASSO 45% of rows, ENG 40%,
# pierce 34%, zanshin 33% were denied or deleted. One in three cards on a client's
# calendar was content they had already rejected or that had been removed.
#
# THIS IS A GUARD, SO IT DEFAULTS ON, with a named escape hatch
# (ECHO_PORTAL_SHOW_REJECTED=true restores the old payload byte for byte). Rows are
# never deleted or re-statused by this -- they stay in content_calendar for audit, the
# publisher still excludes them (portal_calendar_store.due_rows), and every derived
# signal (low_creative, days_remaining, awaiting_media, recreate_budget) is computed
# from the SAME row set as before so no banner changes behavior.
def _handle_social_supabase(account_key, month, now=None):
    """/social from the SHARED content_calendar table (the live portal data plane).
    Reads every row for THIS gym in the month via the same SupabaseCalendarStore that
    powers /calendar, returns each with a stable id + real format + image_public_url +
    caption. low_creative is honest: true only when NO row in the month carries a
    non-empty image_public_url; posts are never filtered out for a missing image."""
    try:
        sb = _pcs.SupabaseCalendarStore()
        rows = sb.list_month(account_key, month)
        # GATE 2 (coach-screens-first-month): rows a coach has NOT yet released are
        # WITHHELD from the owner. 'coach_review' posts never appear in the owner /social
        # view (nor feed low_creative/awaiting) until a coach flips them to 'pending'.
        # But remember they EXIST (audit 2026-08-25 MAJOR): a fully-built, coach-withheld
        # first month must not show the owner the red "Echo is waiting on your uploads"
        # banner — the calendar is built, it is just being screened.
        has_withheld_calendar = any(
            str((r or {}).get("status") or "").lower() == "coach_review" for r in rows)
        rows = [r for r in rows
                if str((r or {}).get("status") or "").lower() != "coach_review"]
        # B12: the CARDS drop rejected/removed content; `rows` (and therefore
        # low_creative) stays exactly what it was, so no banner flips behind this.
        posts = [_content_calendar_post(r, is_lasso=_is_lasso_gym(account_key))
                 for r in _client_visible(rows)]
        signal_posts = [_content_calendar_post(r, is_lasso=_is_lasso_gym(account_key))
                        for r in rows]
    except Exception as exc:
        return 500, {"error": f"store error: {type(exc).__name__}"}

    # HONEST low_creative: computed from the RAW rows' image_url, BEFORE the
    # no-creative fallback substitutes hosted infographics into the display field —
    # else a month running on fallback cards never triggers the "upload media" nudge.
    low_creative = not any((r.get("image_url") or "").strip() for r in rows)
    _, days_remaining = _low_creative_and_days(
        account_key, month, today=(now.date() if now else None))
    # Signal on the UNFILTERED set: a month that is entirely denied is still a BUILT
    # month, and must not raise the red "Echo is waiting on your uploads" banner.
    awaiting_media, upload_url = _awaiting_media_signal(account_key, signal_posts)
    if has_withheld_calendar:
        awaiting_media = False        # built + coach-screened is NOT "waiting on uploads"
    return 200, {
        "account_key": account_key,
        "month": month,
        "active": True,
        "posts": posts,
        "recreate_budget": _budget_state(account_key, now=now),
        "low_creative": low_creative,
        "days_remaining": days_remaining,
        # Red-banner SIGNAL (additive): a CLIENT gym with no calendar is awaiting its
        # uploaded media; upload_url is the per-gym tokenized link. LASSO is never flagged.
        "awaiting_media": awaiting_media,
        "upload_url": upload_url,
        **_media_bridge_status(account_key, now=now),
    }


def handle_social(account_key, month, reader=None, now=None):
    """GET /portal/<token>/social?month=YYYY-MM (month optional; defaults to the
    current UTC month). Returns THIS gym's month calendar: posts, statuses, pillar,
    format, image public_urls, recreate budget state, low_creative + days_remaining.

    Gates: flag OFF -> disabled; Stripe social product not ACTIVE -> 402 empty state;
    TOKEN ISOLATION -> every row is filtered to account_key so no other gym's post is
    ever returned."""
    if not config.portal_social_enabled():
        return _disabled("social")
    if not account_key:
        return 400, {"error": "missing account_key"}

    month = (month or "").strip() or (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    if len(month) != 7 or month[4] != "-" or not month[:4].isdigit() \
            or not month[5:7].isdigit() or not (1 <= int(month[5:7]) <= 12):
        return 400, {"error": "month must be YYYY-MM"}

    if not is_social_active(account_key, reader=reader):
        return 402, _empty_calendar(account_key, month)

    # Shared Supabase content_calendar wins when creds are present (the live portal
    # data plane, same source /calendar reads). No creds -> the existing
    # gym_calendar_queue path below, byte for byte, so every existing test stays green.
    if config.portal_calendar_supabase_enabled():
        return _handle_social_supabase(account_key, month, now=now)

    from . import gym_calendar_queue as _gcq
    prefix = month + "-"
    try:
        with _gcq._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM gym_calendar_queue WHERE account_key=? AND day_key LIKE ? "
                "ORDER BY day_key",
                (account_key, prefix + "%")).fetchall()
        all_rows = [dict(r) for r in rows]
        posts = [_calendar_post(r, is_lasso=_is_lasso_gym(account_key))
                 for r in _client_visible(all_rows)]
        signal_posts = [_calendar_post(r, is_lasso=_is_lasso_gym(account_key))
                        for r in all_rows]
    except Exception as exc:
        return 500, {"error": f"db error: {type(exc).__name__}"}

    low_creative, days_remaining = _low_creative_and_days(account_key, month,
                                                          today=(now.date() if now else None))
    awaiting_media, upload_url = _awaiting_media_signal(account_key, signal_posts)
    return 200, {
        "account_key": account_key,
        "month": month,
        "active": True,
        "posts": posts,
        "recreate_budget": _budget_state(account_key, now=now),
        "low_creative": low_creative,
        "days_remaining": days_remaining,
        # Red-banner SIGNAL (additive): a CLIENT gym with no calendar is awaiting its
        # uploaded media; upload_url is the per-gym tokenized link. LASSO is never flagged.
        "awaiting_media": awaiting_media,
        "upload_url": upload_url,
        **_media_bridge_status(account_key, now=now),
    }


# ==========================================================================
# POST action helpers: ownership (token isolation) + Stripe gate
# ==========================================================================

def _load_owned_draft(account_key, draft_id, store):
    """Load the draft and PROVE it belongs to account_key. Returns (draft, None) on a
    clean hit, or (None, (status, body)) when it is missing OR belongs to another gym.

    THIS is the token-isolation guard for actions: store.get(draft_id) is not
    account-scoped, so without this check gym A's token (knowing gym B's id) could
    reach gym B's draft. A cross-gym id is treated as not-found (404) so it never even
    confirms the other gym's draft exists."""
    draft = store.get(draft_id) if store is not None else None
    if draft is None:
        return None, (404, {"ok": False, "error": "draft not found",
                            "draft_id": draft_id})
    if (getattr(draft, "account_key", None) or "") != account_key:
        # Do NOT leak that the id exists for another gym: same 404 as unknown.
        return None, (404, {"ok": False, "error": "draft not found",
                            "draft_id": draft_id})
    return draft, None


def _action_gates(account_key, draft_id, actor_id, reader,
                  allow_portal_social_disabled=False,
                  allow_client_billing_inactive=False):
    """The flag / ids / Stripe-active gates shared by BOTH data planes. Returns None to
    proceed, or (status, body) to short-circuit. Ownership is checked separately (the
    two planes prove ownership against different stores)."""
    if not allow_portal_social_disabled and not config.portal_social_enabled():
        return _disabled("action")
    if not account_key:
        return (400, {"ok": False, "error": "missing account_key"})
    if not draft_id:
        return (400, {"ok": False, "error": "draft_id required"})
    if not actor_id:
        return (400, {"ok": False, "error": "actor_id required"})
    if not allow_client_billing_inactive and not is_social_active(account_key, reader=reader):
        return (402, {"ok": False, "error": "social plan is not active",
                      "account_key": account_key})
    return None


def _action_preamble(account_key, draft_id, actor_id, store, reader):
    """Shared gate for every POST action: flag, ids, Stripe-active, ownership.
    Returns (draft, None) to proceed, or (None, (status, body)) to short-circuit."""
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return None, short
    return _load_owned_draft(account_key, draft_id, store)


# ==========================================================================
# Supabase content_calendar action path (the live portal data plane)
# ==========================================================================
#
# When SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are both set, actions read and write
# the SHARED content_calendar table via SupabaseCalendarStore (the same source
# /calendar and /social use), NOT the local ephemeral SQLite drafts. NOTHING here
# publishes: approve only flips a row's status to 'approved'; a separate human armed
# publish path (untouched) owns any real post.
#
# TOKEN ISOLATION is double guarded: get_row is scoped by gym_id, and set_status
# filters the PATCH by BOTH id and gym_id. A row whose gym_id differs (or a missing
# row) is a 404 that never reveals it exists and never issues a write.

def _sb_load_owned_row(account_key, draft_id, sb_store):
    """(row, None) when the row exists AND belongs to account_key, else
    (None, (404 body)). get_row is gym scoped, so a cross gym id can never load."""
    row = sb_store.get_row(account_key, draft_id)
    if row is None:
        return None, (404, {"ok": False, "error": "draft not found",
                            "draft_id": draft_id})
    return row, None


def _published_is_final(row, action, draft_id):
    """A published row's creative is already live on the gym's page; no portal action
    may rewrite it. A row in 'publishing' is mid-claim (the publisher owns it for the
    seconds between the atomic claim and the result) — an action flipping it back to
    pending/approved would make it claimable AGAIN and double-post. Returns the 409
    response for either state, else None."""
    status = str((row or {}).get("status") or "").lower()
    if status == "published":
        return 409, {"ok": False, "action": action, "draft_id": draft_id,
                     "error": "this post is already published; it can no longer be "
                              "edited, denied, or killed from the portal"}
    if status == "publishing":
        return 409, {"ok": False, "action": action, "draft_id": draft_id,
                     "error": "this post is publishing right now; try again in a "
                              "minute once it lands"}
    return None


# Portal ECHO_VERIFIED_APPROVAL_PROOF_CONTRACT.md snapshot fields (Echo half,
# 2026-10-05). The object is the exact visible card at tap, NOT actor identity:
# the Clerk actor and write access are proven separately by the portal.
_PROOF_SNAPSHOT_FORMATS = ("feed", "story")
_PROOF_SNAPSHOT_PLATFORMS = ("instagram", "facebook", "googlebusiness")
_PROOF_SNAPSHOT_DAY_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_expected_creative(expected):
    """Validate + normalize the portal's visible-card snapshot. Returns
    (normalized, None) when the snapshot is well-formed, else (None, reason).
    caption may be string or null; media_url must be a nonempty final
    image/video URL; day_key is the YYYY-MM-DD visible post_date; format is
    feed/story (plus GBP photo); platform is the canonical account platform. scheduled_for is
    NOT part of the contract (/social keys on post_date; scheduled_at can be
    synthesized while the DB is null) and is never required."""
    if not isinstance(expected, dict):
        return None, "expected_creative must be an object"
    caption = expected.get("caption")
    if caption is not None and not isinstance(caption, str):
        return None, "caption must be a string or null"
    media_url = str(expected.get("media_url") or "").strip()
    if not media_url:
        return None, "media_url must be a nonempty URL"
    day_key = str(expected.get("day_key") or "").strip()
    if not _PROOF_SNAPSHOT_DAY_KEY.match(day_key):
        return None, "day_key must be YYYY-MM-DD"
    fmt = str(expected.get("format") or "").strip().lower()
    platform = str(expected.get("platform") or "").strip().lower()
    if platform not in _PROOF_SNAPSHOT_PLATFORMS:
        return None, "platform must be instagram, facebook or googlebusiness"
    if fmt not in _PROOF_SNAPSHOT_FORMATS and not (
            platform == "googlebusiness" and fmt == "photo"):
        return None, "format must be feed or story (googlebusiness also allows photo)"
    return {"caption": caption, "media_url": media_url, "day_key": day_key,
            "format": fmt, "platform": platform}, None


def _handle_approve_supabase(account_key, draft_id, actor_id, reader, sb_store,
                             expected_creative=None,
                             allow_portal_social_disabled=False,
                             allow_client_billing_inactive=False):
    short = _action_gates(account_key, draft_id, actor_id, reader,
                          allow_portal_social_disabled=allow_portal_social_disabled,
                          allow_client_billing_inactive=allow_client_billing_inactive)
    if short is not None:
        return short
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        # idempotent: an already approved row is a clean no-op (never a re-publish);
        # an already PUBLISHED row is also a clean no-op (the approval already ran its
        # course), never a status rewrite back to 'approved'.
        if (row.get("status") or "") == _pcs.action_status("approve"):
            if config.approval_proof_enabled():
                expected, why = _validate_expected_creative(expected_creative)
                if expected is None:
                    return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                                 "error": "review_refresh_required", "detail": why}
                recover = getattr(sb_store, "recover_unproved_approval", None)
                recovered = (recover(account_key, draft_id, expected)
                             if callable(recover) else None)
                if recovered is not None:
                    digest = recovered.get("approval_digest")
                    if isinstance(digest, str) and digest.strip():
                        return 200, {"ok": True, "action": "approve", "draft_id": draft_id,
                                     "approval_state": "approved_unproved_retry",
                                     "idempotent": True, "approval_digest": digest}
                return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                             "error": "review_refresh_required"}
            # APPROVAL-PROOF CONTRACT: explicitly distinguishable from a NEW
            # approval; the portal must NOT stamp a verified-approval proof
            # for an idempotent replay.
            return 200, {"ok": True, "action": "approve", "draft_id": draft_id,
                         "detail": "Already approved.", "idempotent": True,
                         "approval_state": "already_approved"}
        if str(row.get("status") or "").lower() == "published":
            # Distinguishable terminal state; never mints a new stamp.
            return 200, {"ok": True, "action": "approve", "draft_id": draft_id,
                         "detail": "Already published.", "idempotent": True,
                         "approval_state": "published"}
        # MID-CLAIM GUARD (audit 2026-08-25 MAJOR): a row in 'publishing' is owned by the
        # publisher for the seconds between the atomic claim and the result. Approving it
        # here would flip it back to 'approved' — claimable AGAIN next tick — and the SAME
        # creative publishes twice (a client double-tapping Approve was enough to hit
        # this). Mirror the edit/deny/kill guard: 409, try again in a minute.
        if str(row.get("status") or "").lower() == "publishing":
            return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                         "error": "this post is publishing right now; try again in a "
                                  "minute once it lands"}
        # GATE 2: a withheld first-month row cannot be approved by the owner. It stays
        # invisible in /social, but guard the action too in case an id leaks.
        if str(row.get("status") or "").lower() == "coach_review":
            return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                         "error": "this post is still in coach review and has not been "
                                  "released yet"}
        if str(row.get("status") or "").lower() in ("denied", "killed", "deleted", "failed"):
            return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                         "error": "this post is no longer awaiting approval"}
        # MEDIA HOLD GATE: a needs-media row (reason set, or no usable media at all)
        # can never be approved — even if a stale status already says pending. The
        # hold stays visible until real media lands; nothing here lifts that gate.
        _not_ready = (row.get("media_not_ready_reason") or "").strip()
        if _not_ready:
            return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                         "error": f"media not ready: {_not_ready[:200]}",
                         "media_not_ready_reason": _not_ready}
        if not (row.get("image_url") or "").strip():
            return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                         "error": "this post has no media yet; add media before "
                                  "approving"}
        # The production store uses an atomic DB predicate so media removal
        # between the pre-read and this write cannot approve a stale hold.
        # APPROVAL PROVENANCE (defect 1 repair, 2026-10-05): an Echo bearer
        # token authenticates the gym's approval INTENT, never a verified
        # HUMAN. The RPC records status='approved' + a digest of the exact
        # content served and leaves approval_kind/approved_by UNPROVED (NULL);
        # the browser-body actor_id is spoofable and is NOT forwarded (the RPC
        # takes no actor parameter at all). Human provenance is stamped only
        # by the PORTAL, server-side, via calendar_stamp_verified_approval
        # with an authenticated Clerk actor and this response's
        # approval_digest (docs/ECHO_VERIFIED_APPROVAL_PROOF_CONTRACT.md).
        # VISIBLE-CARD SNAPSHOT GATE (Echo half of the portal
        # ECHO_VERIFIED_APPROVAL_PROOF contract, 2026-10-05): only when
        # AGENT_APPROVAL_PROOF is ON does Echo require the portal's
        # expected_creative snapshot. Absent/malformed snapshot, or a compare
        # mismatch inside the atomic RPC, is a 409 review_refresh_required:
        # no status change, no digest. Flag OFF ignores the body field
        # entirely and keeps the legacy wire + error shape.
        _proof = config.approval_proof_enabled()
        _expected = None
        if _proof:
            _expected, _why = _validate_expected_creative(expected_creative)
            if _expected is None:
                return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                             "error": "review_refresh_required",
                             "detail": _why}
        approve_ready = getattr(sb_store, "approve_ready", None)
        if callable(approve_ready):
            updated = (approve_ready(account_key, draft_id)
                       if _expected is None
                       else approve_ready(account_key, draft_id,
                                          expected_creative=_expected))
        else:
            updated = sb_store.set_status(account_key, draft_id,
                                          _pcs.action_status("approve"))
        if updated is None:
            if _proof:
                # Stale snapshot OR the row changed under the tap: the card
                # the human saw is no longer the locked row. Fail closed into
                # fresh review; the digest is NOT stamped.
                return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                             "error": "review_refresh_required"}
            return 409, {"ok": False, "action": "approve", "draft_id": draft_id,
                         "error": "post changed before approval; refresh and review it again"}
        # NEW approval: explicit approval_state + idempotent=False + the
        # digest the portal must present to calendar_stamp_verified_approval.
        result = _action_result("approve", draft_id, updated)
        result["approval_state"] = "approved"
        result["idempotent"] = False
        result["approval_digest"] = str((updated or {}).get("approval_digest") or "")
        return 200, result
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}


def _gym_display_name(account_key):
    """Best-effort gym display name for the story caption card, from the registry account;
    falls back to the title-cased base key. Never raises."""
    try:
        from .accounts import get_account
        acct = get_account(f"{account_key}_ig") or get_account(account_key)
        name = getattr(acct, "display_name", "") if acct else ""
        return name or account_key.replace("_", " ").title()
    except Exception:  # noqa: BLE001
        return account_key.replace("_", " ").title()


def maybe_reburn_story(account_key, row, new_caption, sb_store, *, logger=None):
    """Task #28 (§5c): after a STORY caption edit, re-burn the new caption onto fresh media
    from the row's raw source_media_url and swap image_url — so a saved story caption shows
    immediately, not only on the monthly rebuild. Fully gated + best-effort: a no-op unless
    story_reburn.should_reburn(row), and any failure leaves the saved caption edit intact
    (the rebuild is the backstop). Returns the new image_url or None."""
    log = logger or (lambda m: print(f"[portal-social] {m}"))
    try:
        from . import story_reburn
        if not story_reburn.should_reburn(row):
            return None
        from . import visual_writer_prepare
        receipt_evidence = None
        if visual_writer_prepare.enabled():
            reburned = story_reburn.reburn_with_evidence(
                row.get("source_media_url"), new_caption, _gym_display_name(account_key),
                account_key, logger=log)
            if not isinstance(reburned, tuple) or len(reburned) != 2:
                return None
            new_url, receipt_evidence = reburned
            receipt_evidence = (receipt_evidence.as_dict()
                                if hasattr(receipt_evidence, "as_dict")
                                else receipt_evidence)
            if (not receipt_evidence
                    or receipt_evidence.get("source_exact_url") != row.get("source_media_url")
                    or receipt_evidence.get("delivered_exact_url") != new_url):
                return None
        else:
            new_url = story_reburn.reburn(
                row.get("source_media_url"), new_caption, _gym_display_name(account_key),
                account_key, logger=log)
        if not new_url:
            return None
        patch_args = {}
        if receipt_evidence is not None:
            patch_args["render_evidence"] = receipt_evidence
            # The caption save happens before this best-effort reburn. Bind the
            # media write to that authoritative post-edit row so a concurrent
            # visual/slot swap cannot be overwritten by a prepared stale group.
            patch_args["expected_row"] = row
        persisted = sb_store.patch_image_url(account_key, row.get("id"), new_url, **patch_args)
        if not isinstance(persisted, dict) or persisted.get("image_url") != new_url:
            return None
        return new_url
    except Exception as exc:  # noqa: BLE001 - a re-burn must NEVER fail the saved edit
        log(f"story re-burn skipped ({type(exc).__name__})")
        return None


def _action_result(action, draft_id, updated_row):
    """Task #28 (false-approval fix): return the AUTHORITATIVE per-row status + day_key from
    the just-written row so the portal binds THAT card's badge to server truth — instead of
    optimistically carrying the new status onto the next card. The UI updates only the row
    whose id it submitted; every other card keeps its own /social status."""
    row = updated_row or {}
    return {"ok": True, "action": action, "draft_id": draft_id,
            "status": row.get("status") or "",
            "day_key": row.get("post_date") or ""}


def _learn_from_edit(account_key, before, after, reason=""):
    """Record a client's caption edit into THIS gym's brain (tenant_brain edit_diff) so
    the drafter's NEXT caption for the gym moves toward the approver's taste. Keyed by
    the gym's GENERATION account ({base}_ig) — the same key drafter._brain_guidance
    reads — so a portal edit actually teaches the SB7 prompt. Best effort, never raises,
    no-op while AGENT_TENANT_BRAIN_ENABLED is off.

    reason (optional, Dale 2026-08-15): the approver's EXPLICIT "reason why" note — WHY
    they changed the caption — sent as a field distinct from the new caption. When
    present it is recorded as the edit's `rule` (the schema's style-rule slot), so the
    approver's stated intent teaches the prompt directly instead of being only INFERRED
    from the before/after diff. Fabrication-safe: the rule text still passes the gate in
    prompt_notes before it can reach a prompt, so a reason carrying an uncleared figure
    is skipped from prompts (it is still recorded for the human audit trail)."""
    before = (before or "").strip()
    after = (after or "").strip()
    reason = (reason or "").strip()
    # Record when the caption actually changed, OR when there is an explicit reason to
    # capture (a reason with no text change still teaches the gym's style preference).
    if (not after or after == before) and not reason:
        return
    try:
        from . import tenant_brain
        base = str(account_key or "")
        for suf in ("_ig", "_fb"):
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        fields = {"before": before, "after": after}
        if reason:
            fields["rule"] = reason
        tenant_brain.record_event(f"{base}_ig", "edit_diff", **fields)
    except Exception as exc:  # noqa: BLE001 - learning must never break an edit
        print(f"[portal-social] brain edit_diff record failed for "
              f"{account_key}: {type(exc).__name__}")


def _edit_gate_claims(account_key):
    """The claim set an EDIT must clear for THIS gym (audit 2026-08-25 MAJOR): the gym's
    OWN approved client sources — never LASSO's global stats, which could 'clear' a claim
    that is false for this gym (cross-tenant clearance) while the gym's real facts got
    false 422s. LASSO's own account keeps the global set (None -> is_gate_clean default).
    Best effort: an unreadable source table returns [] (fail closed on figures)."""
    base = (account_key or "").strip()
    if not base or base.startswith("lasso"):
        return None
    try:
        from . import client_sources
        return client_sources.approved_claims(base)
    except Exception:  # noqa: BLE001
        return []


def _clean_edit_note(note):
    """The no-dash copy law applied to an EDITED caption: dashes are cleaned (the same
    filter the build lane uses) instead of bouncing the owner with an error — a client
    typing an em dash should not lose their edit; what PUBLISHES must be dash-free
    (previously a dashed edit sailed through and the story reburn burned it onto media)."""
    try:
        from .content_categories import filter_platform_copy
        cleaned = filter_platform_copy(note or "")
        return cleaned if (cleaned or "").strip() else (note or "")
    except Exception:  # noqa: BLE001 - cleaning must never eat an edit
        return note or ""


def _split_note_meta(note, reason=""):
    """Split a trailing edit-rationale meta block off an EDIT note BEFORE it is saved
    as the caption (CrossFit ENG live leak, 2026-08-23: a caption published ending with
    '[why] Removed word parents and added people ...'). A note pasted as
    'new caption [why] because ...' stores ONLY the caption body; the rationale moves
    into the edit's `reason` (the brain's rule slot) when the approver did not send one
    separately — the reason lives in its own field, never in the caption. A note that
    is ALL meta returns "" so the normal empty-note validation refuses it (held for the
    human, never silently saved). Returns (note_body, reason)."""
    from . import post_quality
    body, meta = post_quality.split_meta_suffix(note or "")
    if not meta:
        return note or "", reason or ""
    if not (body or "").strip():
        return "", reason or ""
    if not (reason or "").strip():
        # keep only the rationale text: drop the bracketed label itself
        reason = post_quality._EDIT_META_RE.sub("", meta, count=1).strip()
    return body.strip(), reason or ""


def _handle_edit_supabase(account_key, draft_id, actor_id, note, reader, sb_store,
                          reason=""):
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    note = _clean_edit_note(note)
    # a '[why] ...' rationale pasted into the note is captured as the reason, never
    # saved into the caption (the ENG 2026-08-23 leak class, closed at the source).
    note, reason = _split_note_meta(note, reason)
    from .copy_gate import format_caption
    try:
        note = format_caption(note)
    except ValueError:
        return 422, {"ok": False, "action": "edit", "draft_id": draft_id,
                     "error": "The caption has a semicolon inside a URL. "
                              "Edit the link before saving."}
    # the fabrication gate runs BEFORE any store touch: an unsupported claim is refused
    # 422 whether or not the row exists, so a stat can never reach the caption. Gated
    # against THIS gym's own approved claims (LASSO globals only for LASSO itself).
    if not _rotation.is_gate_clean(note, approved_claims=_edit_gate_claims(account_key)):
        return 422, {"ok": False, "action": "edit", "draft_id": draft_id,
                     "error": "fabrication gate: the note carries a claim with no "
                              "approved receipt. Cite an approved source or drop the "
                              "figure."}
    if not note:
        return 400, {"ok": False, "action": "edit", "draft_id": draft_id,
                     "error": "note (new caption text) is required for edit"}
    # DURABLE WRITE FIRST, then LEARN (Dale, 2026-08-17: "saving took several attempts /
    # timed out / booted me out"). The caption edit is the durable calendar write; the
    # learning/brain write is best-effort and must NEVER be able to fail the save. So the
    # store round-trips run inside this try (a real store error is a 500), but learning is
    # deliberately OUTSIDE it: once patch_caption returns the updated row, the edit HAS
    # persisted, and nothing after it may turn that success into an error the client
    # retries against.
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        final = _published_is_final(row, "edit", draft_id)
        if final is not None:
            return final
        before = row.get("caption") or ""
        updated = sb_store.patch_caption(account_key, draft_id, note)
        if updated is None:
            return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    # The caption is now durably saved. LEARN best-effort: feed the (before -> after)
    # edit AND the approver's explicit reason into the gym's brain so future captions
    # move toward the approver's taste (Dale's youth-content guidance). _learn_from_edit
    # never raises, but we still guard here so even a catastrophic failure cannot flip a
    # persisted edit into a 500 the client keeps retrying.
    try:
        _learn_from_edit(account_key, before, note, reason=reason)
    except Exception as exc:  # noqa: BLE001 - learning may never fail a saved edit
        print(f"[portal-social] learn-from-edit failed post-save for "
              f"{account_key}: {type(exc).__name__}")
    # Task #28 (§5c): a STORY caption edit re-burns onto fresh media immediately (gated,
    # best-effort — the caption is already saved). Use the authoritative row returned
    # by that save so the prepared media PATCH is bound to the new caption and status.
    reburned = maybe_reburn_story(account_key, updated, note, sb_store)
    return 200, {"ok": True, "action": "edit", "draft_id": draft_id,
                 "caption": updated.get("caption", ""),
                 "story_reburned": bool(reburned),
                 "status": updated.get("status", "pending"),
                 "day_key": updated.get("post_date", ""),
                 # Task #28: echo the reason text back (not just a captured bool) so the
                 # portal repopulates the "Why" field after save and shows it as a separate
                 # "Why: …" line — never appended to the caption.
                 "reason": (reason or ""),
                 "reason_captured": bool((reason or "").strip())}


def _handle_deny_supabase(account_key, draft_id, actor_id, note, reader, sb_store):
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    if recreate_remaining(account_key) <= 0:
        return 409, {"ok": False, "action": "deny", "draft_id": draft_id,
                     "error": "recreate budget for this month is used up",
                     "recreate_budget": _budget_state(account_key)}
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        final = _published_is_final(row, "deny", draft_id)
        if final is not None:
            return final
        # idempotent: re-denying an already denied row never burns a second unit.
        if (row.get("status") or "") == _pcs.action_status("deny"):
            return 200, {"ok": True, "action": "deny", "draft_id": draft_id,
                         "detail": "Already denied.", "idempotent": True,
                         "recreate_budget": _budget_state(account_key)}
        updated = sb_store.set_status(account_key, draft_id,
                                      _pcs.action_status("deny"))
        if updated is None:
            return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    # Charge the budget only after a successful deny (never on a 404 or store error).
    spend_recreate(account_key)
    return 200, {**_action_result("deny", draft_id, updated),
                 "recreate_budget": _budget_state(account_key)}


# ==========================================================================
# POST /portal/<token>/posts/<id>/swap-media  (B6: FREE, never charges the budget)
#
# THE BUDGET DESIGN BUG (Pete, zanshin): the portal's only levers are approve /
# edit / deny / kill, so "use a different photo" and "the caption needs work" are
# BOTH a deny and both burn one of the 15 monthly recreates. Pete ran out of
# recreates swapping PHOTOS and then could not fix a caption. The counter was never
# wrong -- the two actions were never separated.
#
# THE SPLIT: swapping the pixels regenerates nothing, so it is free and unlimited.
# Regenerating COPY still costs one of 15. The write goes through
# SupabaseCalendarStore.swap_media, which is status-guarded server-side to
# pending / coach_review, so an approved or live post can never be repointed and
# the gym's approval always keeps exactly the pixels it approved.
# ==========================================================================

# ---- durable swap action receipts (DRAFT v3, 2026-10-04) --------------------
#
# An explicit opaque client action_id binds one swap to one durable,
# tenant-scoped receipt (public.portal_action_receipt; migration
# migrations/portal_action_receipt_draft_20261004.sql is a DRAFT applied by the
# database operator only). ALL receipt access goes through the three SECURITY
# DEFINER RPC wrappers on SupabaseCalendarStore -- this module NEVER reads,
# inserts, updates or patches the receipt table directly.
#
# The binding contract (Portal PR750 sends ONLY action_id):
#   * the request fingerprint is derived from the immutable request tuple
#     (gym, row, actor, action, action_id) BEFORE any calendar row is read;
#   * begin runs FIRST after the auth/capability gates and captures the
#     before_state itself, so an exact same-binding replay returns the stored
#     terminal outcome even when the row was since deleted or its media moved;
#   * a conflicting reuse (different post, actor or fingerprint) is a 409;
#   * a missing or historical-NULL logical_post_id row is HELD for manual
#     review -- sibling grouping by date/media inference is never a fallback;
#   * the swap group is EXACTLY the claim RPC's frozen member manifest
#     (gym_id, logical_post_id, variant_status='active'): no
#     media_swap.sibling_rows, no date/media inference, no per-row swap_media
#     writes on this path -- one apply RPC writes the whole group atomically;
#   * success is ONLY the persisted terminal receipt apply returns; a timeout
#     or lost response is reconciled by replaying the same apply, never by
#     trusting a mutable row read or a raw write response.
#
# Flag: ECHO_SWAP_ACTION_RECEIPT (default OFF). OFF + an explicit action_id is
# a 503 (never a silently non-idempotent swap); callers that send no action_id
# keep the legacy path byte-for-byte.

_RECEIPT_ACTION = "swap-media"
# Sentinel distinguishing "client sent no action_id key at all" (legacy
# path) from an explicit value -- an explicit JSON null, empty string or
# non-string must be REJECTED on the receipt path, never silently
# treated as the legacy no-ID call.
_NO_ACTION_ID = object()
_RECEIPT_MAX_ACTION_ID = 128
_RECEIPT_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}\Z")
_RECEIPT_MEDIA_KEYS = ("image_url", "source_media_url", "thumbnail_url",
                       "source_media_asset_id")


def _swap_receipts_enabled():
    """ECHO_SWAP_ACTION_RECEIPT, default OFF. The durable idempotency receipt
    for portal photo swaps. NEW capability, ships dark; arm by hand only after
    the DRAFT migration is applied."""
    return config._truthy(os.environ.get("ECHO_SWAP_ACTION_RECEIPT", "false"))


def _swap_receipt_gyms():
    """ECHO_SWAP_ACTION_RECEIPT_GYMS, default EMPTY. Comma-separated exact
    account keys permitted on the explicit-action_id receipt path. Whitespace
    around each config entry is trimmed and matching is exact; absent or
    empty means NO gym is allowed -- fail closed, never a wildcard enable."""
    raw = os.environ.get("ECHO_SWAP_ACTION_RECEIPT_GYMS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _swap_receipt_gym_allowed(account_key):
    """Exact account-key membership in the receipt
    allowlist. A missing gym or a missing list is NOT allowed."""
    return isinstance(account_key, str) and account_key in _swap_receipt_gyms()


def _receipt_fingerprint(account_key, draft_id, actor_id, action_id):
    """Immutable binding over the request tuple ONLY (gym, row, actor, action,
    action_id), computable before any calendar row is read. The media the
    request was issued against is captured by SQL begin as before_state, never
    by this fingerprint."""
    bind = {"action": _RECEIPT_ACTION, "gym": str(account_key),
            "row": str(draft_id), "actor": str(actor_id or ""),
            "action_id": str(action_id)}
    raw = json.dumps(bind, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _receipt_clean_url(url):
    """A public object identity URL: http(s) only, never signed/tokenized (no
    query or fragment). Anything else is unsafe to freeze onto a receipt."""
    if not isinstance(url, str) or not url:
        return False
    low = url.lower()
    if not (low.startswith("https://") or low.startswith("http://")):
        return False
    return "?" not in url and "#" not in url


def _receipt_media_identity(source, require_asset_id=False):
    """The allowlisted per-row media identity for claim/apply, or None when any
    identity piece is missing or unsafe (the caller then HOLDS: no claim, no
    write). Keys with empty values are omitted so SQL's NULL-tolerant equality
    sees exactly the frozen shape."""
    source = source or {}
    image_url = source.get("image_url")
    if not _receipt_clean_url(image_url):
        return None
    out = {"image_url": image_url}
    for key in ("source_media_url", "thumbnail_url"):
        value = source.get(key)
        if value:
            if not _receipt_clean_url(value):
                return None
            out[key] = value
    asset_id = source.get("source_media_asset_id") or source.get("asset_id")
    if asset_id:
        out["source_media_asset_id"] = str(asset_id)
    elif require_asset_id:
        return None
    return out


_RECEIPT_LOCAL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
#: Provenance prefix for a tenant-owned local-library pick (no media_asset row
#: exists; the SQL claim/apply allowlists this exact prefix and nothing else).
RECEIPT_LOCAL_ASSET_PREFIX = "local:"


def _receipt_selected_asset(pick):
    """The claim selected_asset: asset_id + public object identity keys only.
    None (hold) when the asset id is missing or any URL is unsafe.

    A Drive pick carries its media_asset id. A LOCAL-library pick has no Drive
    asset id (media_swap._finish leaves it empty): its durable provenance is
    ``local:<library key>`` -- the library-relative name inside the gym's OWN
    library root (media_swap.local_candidates / library_path_for), which the
    SQL claim accepts without a media_asset row. A pick with neither a Drive
    asset id nor a safe local key is unproven media: hold, never claim."""
    identity = _receipt_media_identity(pick)
    if identity is None:
        return None
    asset_id = identity.pop("source_media_asset_id", None)
    if not asset_id:
        key = str((pick or {}).get("key") or "")
        if (str((pick or {}).get("source") or "") != "local"
                or not _RECEIPT_LOCAL_KEY_RE.match(key) or ".." in key):
            return None
        asset_id = RECEIPT_LOCAL_ASSET_PREFIX + key
    out = {"asset_id": asset_id, "image_url": identity["image_url"]}
    for key in ("source_media_url", "thumbnail_url"):
        if key in identity:
            out[key] = identity[key]
    kind = (pick or {}).get("kind")
    if kind:
        out["kind"] = str(kind)
    key = (pick or {}).get("key")
    if key:
        out["key"] = str(key)
    return out


def _receipt_identity_matches(pick, selected):
    """Exact equality over the WHOLE frozen selected identity (asset id /
    provenance, hosted object, source and thumbnail URLs). A concurrent claim
    loser whose candidate shares only the hosted URL -- or nothing -- is NOT
    the frozen selection and must never be settled as if it were."""
    own = _receipt_selected_asset(pick)
    if own is None or not isinstance(selected, dict):
        return False
    return all(str(own.get(key) or "") == str(selected.get(key) or "")
               for key in ("asset_id", "image_url", "source_media_url",
                           "thumbnail_url"))


def _settle_receipt_ledgers(ms, account_key, receipt, own_pick=None):
    """Settle the usage ledgers against the receipt's FROZEN selected asset --
    durable, exact-asset-bound and idempotent, so a lost apply response that
    committed is settled by the terminal replay, never skipped. A losing
    reservation was already released and is NEVER settled here.

    own_pick (this attempt's reservation) is used only when its identity is
    exactly the frozen selection. Otherwise the settlement pick is rebuilt
    from the receipt: a Drive asset re-derives the deterministic byte-bound
    claim key (gym_media_selector.drive_content_claim_id) so claim_done is an
    idempotent no-op when the first attempt already settled, and the usage
    stamp itself is never repeated (_drive_stamped -- reserve_local_pick
    stamped pre-claim; stamp_use is not idempotent). A 'local:' asset
    re-reserves through the once-only served ledger keyed by the library file.
    Best-effort: a ledger hiccup never turns a committed swap into a failure.
    """
    try:
        selected = receipt.get("selected_asset") or {}
        before = receipt.get("before_state") or {}
        asset_id = str(selected.get("asset_id") or "")
        if not asset_id or not before:
            return
        if own_pick is not None and _receipt_identity_matches(own_pick, selected):
            ms.after_swap(account_key, before, own_pick)
            return
        pick = {"source": ("local" if asset_id.startswith(RECEIPT_LOCAL_ASSET_PREFIX)
                           else "drive"),
                "kind": str(selected.get("kind") or "photo"),
                "key": str(selected.get("key") or ""),
                "source_media_asset_id": asset_id}
        if pick["source"] == "local":
            lib = ms.library_path_for(account_key)
            key = asset_id[len(RECEIPT_LOCAL_ASSET_PREFIX):]
            path = os.path.join(lib, key) if lib else ""
            if not path:
                return
            pick["path"] = path
        else:
            from . import gym_media_index as _idx
            from . import gym_media_selector as _sel
            store = _idx.default_store()
            asset = store.get_asset(asset_id) if store is not None else None
            if not asset:
                return
            base = _sel.base_gym_key(account_key)
            pick["_drive_claim_id"] = _sel.drive_content_claim_id(base, asset)
            pick["_drive_claim_account"] = f"{base}_gbp"
            pick["_drive_stamped"] = True
        ms.after_swap(account_key, before, pick)
    except Exception as exc:  # noqa: BLE001
        print(f"[portal-social] receipt swap ledger settle failed for "
              f"{receipt.get('action_id')}: {type(exc).__name__}")


def _receipt_store_capable(sb_store):
    return all(callable(getattr(sb_store, name, None)) for name in
               ("action_receipt_begin", "action_receipt_claim",
                "action_receipt_apply", "get_row",
                "list_active_logical_post_rows"))


def _receipt_short(account_key, draft_id, action_id, status, error, reason):
    return status, {"ok": False, "action": _RECEIPT_ACTION, "draft_id": draft_id,
                    "action_id": action_id, "error": error, "reason": reason,
                    "recreate_budget": _budget_state(account_key)}


def _receipt_unknown(account_key, draft_id, action_id):
    return _receipt_short(account_key, draft_id, action_id, 503,
                          "Photo swap outcome could not be verified.",
                          "swap_outcome_unknown")


def _receipt_success_body(account_key, draft_id, action_id, receipt,
                          idempotent=False):
    """Built ONLY from a persisted terminal receipt's after_state and
    sibling_outcomes -- never from a mutable row re-read or a raw write
    response."""
    after = receipt.get("after_state") or {}
    outcomes = [o for o in (receipt.get("sibling_outcomes") or [])
                if isinstance(o, dict)]
    swapped = [o.get("id") for o in outcomes
               if o.get("swapped") and o.get("id") and str(o.get("id")) != str(draft_id)]
    left = [o.get("id") for o in outcomes if not o.get("swapped") and o.get("id")]
    kind = _media_kind(after.get("image_url", "") or "")
    body = {"ok": True, "action": _RECEIPT_ACTION, "draft_id": draft_id,
            "action_id": action_id,
            "siblings_swapped": swapped, "siblings_left": left,
            "sibling_results": [],
            "image_public_url": (after.get("thumbnail_url")
                                 or after.get("image_url", "")),
            "media_kind": kind,
            "video_url": after.get("image_url", "") if kind == "video" else None,
            "caption": after.get("caption", ""),
            "status": after.get("status", "pending"),
            "day_key": after.get("post_date", ""),
            "free": True,
            "recreate_budget": _budget_state(account_key)}
    if idempotent:
        body["idempotent"] = True
    return body


def _receipt_failed_body(account_key, draft_id, action_id, receipt):
    err = receipt.get("error") or {}
    reason = str(err.get("reason") or "swap_failed")
    try:
        code = int(receipt.get("response_status") or 409)
    except (TypeError, ValueError):
        code = 409
    if not 400 <= code <= 599:
        code = 409
    body = {"ok": False, "action": _RECEIPT_ACTION, "draft_id": draft_id,
            "action_id": action_id, "idempotent": True,
            "error": "This photo swap could not be completed.",
            "reason": reason,
            "recreate_budget": _budget_state(account_key)}
    return code, body


def _handle_swap_media_receipt(sb_store, account_key, draft_id, actor_id,
                               action_id, picker=None):
    """The explicit-action-id swap path (ECHO_SWAP_ACTION_RECEIPT). Runs the
    begin RPC BEFORE any calendar row read; every outcome is derived from the
    durable receipt, and the media write is exactly one apply RPC over the
    frozen member manifest. Legacy (no action_id) never enters here."""
    from . import media_swap as _ms

    # (1) Presence and shape: an explicit action_id is validated as sent. An
    # empty, non-string or out-of-shape value is a 400 -- it must NEVER fall
    # into the legacy no-ID path.
    if not isinstance(action_id, str):
        return _receipt_short(account_key, draft_id, str(action_id)[:64], 400,
                              "action_id must be an opaque string",
                              "invalid_action_id")
    # Validated AS SENT: surrounding whitespace is rejected by the anchored
    # shape below, never stripped -- a stripped id would fingerprint a request
    # tuple the client never sent.
    if (not action_id or len(action_id) > _RECEIPT_MAX_ACTION_ID
            or not _RECEIPT_ACTION_ID_RE.match(action_id)):
        return _receipt_short(account_key, draft_id, action_id, 400,
                              "action_id must be 1-128 opaque characters",
                              "invalid_action_id")

    # (2) Capability gates: flag OFF + explicit action_id refuses closed; a
    # store without the RPC wrappers can never run this path.
    if not _swap_receipts_enabled():
        return _receipt_short(account_key, draft_id, action_id, 503,
                              "action receipts are not enabled for this gym",
                              "receipts_disabled")
    if not _receipt_store_capable(sb_store):
        return _receipt_short(account_key, draft_id, action_id, 503,
                              "action receipts are unavailable on this store",
                              "receipt_store_unavailable")

    # (3) Immutable fingerprint from the request tuple, BEFORE any row read.
    fp = _receipt_fingerprint(account_key, draft_id, actor_id, action_id)

    # (4) BEGIN FIRST: SQL binds the tuple and captures before_state itself.
    # The same binding replays terminally even when the row was deleted or its
    # media moved; a conflicting reuse 409s; a missing / historical-NULL row is
    # a manual-review hold with no receipt and no heuristic fallback.
    try:
        receipt = sb_store.action_receipt_begin(
            account_key, action_id, _RECEIPT_ACTION, draft_id, actor_id, fp)
    except _pcs.ReceiptHoldError:
        return _receipt_short(account_key, draft_id, action_id, 409,
                              "This post needs a manual review before its photo can be swapped.",
                              "swap_manual_review")
    except _pcs.ReceiptConflictError:
        return _receipt_short(account_key, draft_id, action_id, 409,
                              "This action_id is already bound to a different request.",
                              "action_id_conflict")
    except Exception:  # noqa: BLE001 - receipt durability failure refuses the swap
        return _receipt_short(account_key, draft_id, action_id, 503,
                              "action receipt could not be persisted; refusing to swap",
                              "receipt_store_unavailable")

    status = receipt.get("status")
    if status == "succeeded":
        # Exact terminal replay: the stored outcome verbatim, no row read. The
        # frozen selection's ledger settlement is idempotent, so a first
        # attempt whose success response was lost before settling is settled
        # HERE (a committed apply is never reported without settlement).
        _settle_receipt_ledgers(_ms, account_key, receipt)
        return 200, _receipt_success_body(account_key, draft_id, action_id,
                                          receipt, idempotent=True)
    if status == "failed":
        return _receipt_failed_body(account_key, draft_id, action_id, receipt)
    if status not in ("started", "selected"):
        # 'uncertain' or anything unexpected: fail closed, always.
        return _receipt_unknown(account_key, draft_id, action_id)

    own_pick = None        # a reserve_local_pick reservation THIS attempt owns
    apply_started = False  # once the apply RPC is invoked the outcome is unknown
    try:
        if status == "started":
            # FRESH SELECTION. The row exists (begin just captured it); a race
            # delete means we cannot pick safely, so hold for manual review
            # rather than inferring anything.
            try:
                row = sb_store.get_row(account_key, draft_id)
            except Exception:  # noqa: BLE001
                row = None
            if row is None:
                return _receipt_short(
                    account_key, draft_id, action_id, 409,
                    "This post needs a manual review before its photo can be swapped.",
                    "swap_manual_review")
            final = _published_is_final(row, _RECEIPT_ACTION, draft_id)
            if final is not None:
                return final
            if str(row.get("status") or "") not in ("pending", "coach_review"):
                return 409, {"ok": False, "action": _RECEIPT_ACTION,
                             "draft_id": draft_id, "action_id": action_id,
                             "error": "This post is no longer waiting for review.",
                             "reason": "not_swappable",
                             "recreate_budget": _budget_state(account_key)}
            logical = row.get("logical_post_id")
            if not logical:
                # Historical NULL: manual-review hold, never a heuristic group.
                return _receipt_short(
                    account_key, draft_id, action_id, 409,
                    "This post needs a manual review before its photo can be swapped.",
                    "swap_manual_review")

            # The swap group is derived ONLY from (gym, logical_post_id,
            # active) rows -- the exact scope the claim RPC freezes into the
            # member manifest. media_swap.sibling_rows and any date/media
            # inference are deliberately NOT consulted on this path.
            members = sb_store.list_active_logical_post_rows(account_key, logical)
            members = sorted((m for m in members if isinstance(m, dict)),
                             key=lambda m: str(m.get("id") or ""))
            member_ids = [str(m.get("id") or "") for m in members]
            if str(draft_id) not in member_ids:
                return _receipt_short(account_key, draft_id, action_id, 409,
                                      "This post's swap group changed; nothing was swapped.",
                                      "swap_group_stale")

            # The picker renders the clicked primary itself; passing the
            # primary among `siblings` would render its Story variant TWICE.
            # Only the OTHER members are shaped as variants here; the claim
            # still freezes the full member manifest for apply.
            others = [m for m in members if str(m.get("id")) != str(draft_id)]
            pick = (picker or _ms.pick_replacement)(account_key, row,
                                                    store=sb_store,
                                                    siblings=others)
            if not pick.get("ok"):
                return 409, {"ok": False, "action": _RECEIPT_ACTION,
                             "draft_id": draft_id, "action_id": action_id,
                             "error": _ms.client_message(pick.get("reason")),
                             "reason": pick.get("reason"),
                             "failed_sibling": pick.get("failed_sibling"),
                             "recreate_budget": _budget_state(account_key)}
            selected = _receipt_selected_asset(pick)
            if selected is None:
                return _receipt_short(account_key, draft_id, action_id, 409,
                                      "verified media evidence is unavailable",
                                      "media_evidence_unavailable")
            variants = pick.get("siblings") or {}
            # Every manifest member needs a shaped variant BEFORE anything is
            # frozen: one missing or unsafe variant holds the whole group.
            planned = {}
            for mid in member_ids:
                if mid == str(draft_id):
                    continue
                variant = variants.get(mid) or {}
                if not variant.get("ok"):
                    return 409, {"ok": False, "action": _RECEIPT_ACTION,
                                 "draft_id": draft_id, "action_id": action_id,
                                 "error": _ms.client_message(_ms.REASON_STORY_REBURN),
                                 "reason": _ms.REASON_STORY_REBURN,
                                 "failed_sibling": mid,
                                 "recreate_budget": _budget_state(account_key)}
                identity = _receipt_media_identity(variant)
                if identity is None:
                    return _receipt_short(account_key, draft_id, action_id, 409,
                                          "verified media evidence is unavailable",
                                          "media_evidence_unavailable")
                # A variant of a LOCAL pick carries no Drive asset id of its
                # own (media_swap._finish leaves it empty); its source asset
                # identity is the selected local provenance. Drive variants
                # already carry their asset id and are never overwritten.
                if ("source_media_asset_id" not in identity
                        and selected["asset_id"].startswith(
                            RECEIPT_LOCAL_ASSET_PREFIX)):
                    identity["source_media_asset_id"] = selected["asset_id"]
                planned[mid] = identity

            # Reserve the once-used media BEFORE the selection is frozen, so a
            # frozen selection always implies a prior successful reservation.
            if not _ms.reserve_local_pick(account_key, row, pick):
                return _receipt_short(account_key, draft_id, action_id, 503,
                                      "photo swap is held while media settles",
                                      "reservation_unavailable")
            own_pick = pick

            try:
                won = sb_store.action_receipt_claim(account_key, action_id, fp,
                                                    selected, planned)
            except _pcs.ReceiptHoldError:
                _ms.release_local_pick(own_pick)
                own_pick = None
                return _receipt_short(
                    account_key, draft_id, action_id, 409,
                    "This post needs a manual review before its photo can be swapped.",
                    "swap_manual_review")
            except _pcs.ReceiptConflictError:
                _ms.release_local_pick(own_pick)
                own_pick = None
                return _receipt_short(account_key, draft_id, action_id, 409,
                                      "This post's swap group changed; nothing was swapped.",
                                      "swap_group_stale")
            except _pcs.ReceiptSelectionError:
                _ms.release_local_pick(own_pick)
                own_pick = None
                return _receipt_short(account_key, draft_id, action_id, 409,
                                      "verified media evidence is unavailable",
                                      "media_evidence_unavailable")
            except Exception:
                # UNKNOWN: the claim may have persisted our selection. Retain
                # the reservation (fail closed); the replay reconciles.
                return _receipt_unknown(account_key, draft_id, action_id)
            receipt = won

        # From here the receipt is 'selected' (just claimed, or a replayed
        # selection): the FROZEN selection and manifest govern -- the picker is
        # never consulted again for this action_id.
        frozen_selected = receipt.get("selected_asset") or {}
        frozen_planned = receipt.get("planned_siblings") or {}
        manifest = receipt.get("member_manifest") or {}
        manifest_members = [m for m in (manifest.get("members") or [])
                            if isinstance(m, dict)]
        manifest_ids = sorted(str(m.get("calendar_row_id") or "")
                              for m in manifest_members)
        if (not frozen_selected.get("asset_id")
                or not _receipt_clean_url(frozen_selected.get("image_url"))
                or not manifest_ids or str(draft_id) not in manifest_ids):
            # A frozen selection without verifiable evidence can never be
            # applied: hold, never write.
            return _receipt_short(account_key, draft_id, action_id, 409,
                                  "verified media evidence is unavailable",
                                  "media_evidence_unavailable")
        # The prepared row set must cover the frozen manifest EXACTLY (primary
        # plus every planned sibling); a stale sibling or a membership change
        # aborts the whole group before any write.
        prepared_rows = []
        for mid in manifest_ids:
            if mid in frozen_planned:
                media = dict(frozen_planned[mid])
            else:
                media = {"image_url": frozen_selected.get("image_url"),
                         "source_media_url": frozen_selected.get("source_media_url"),
                         "thumbnail_url": frozen_selected.get("thumbnail_url"),
                         "source_media_asset_id": frozen_selected.get("asset_id")}
            identity = _receipt_media_identity(media)
            if identity is None:
                return _receipt_short(account_key, draft_id, action_id, 409,
                                      "verified media evidence is unavailable",
                                      "media_evidence_unavailable")
            prepared_rows.append({"calendar_row_id": mid, "media": identity})
        planned_ids = sorted(mid for mid in frozen_planned if mid in manifest_ids)
        extra = sorted(set(frozen_planned) - set(manifest_ids))
        covered = sorted({str(draft_id)} | set(planned_ids))
        if extra or covered != manifest_ids:
            if own_pick is not None:
                _ms.release_local_pick(own_pick)
                own_pick = None
            return _receipt_short(account_key, draft_id, action_id, 409,
                                  "This post's swap group changed; nothing was swapped.",
                                  "swap_group_stale")

        # If a concurrent claimant WON with a different selection, ours can
        # never be written (apply validates against the frozen identity), so
        # releasing our reservation is definite and safe. The winner's
        # selection was already reserved by the winning attempt before its
        # claim landed. The comparison is over the ENTIRE frozen identity
        # (asset id / local provenance, image, source and thumbnail URLs):
        # an equal hosted URL under a DIFFERENT asset id is still a losing
        # reservation and is released, never settled.
        if own_pick is not None and \
                not _receipt_identity_matches(own_pick, frozen_selected):
            _ms.release_local_pick(own_pick)
            own_pick = None

        apply_started = True
        try:
            final_receipt = sb_store.action_receipt_apply(
                account_key, action_id, fp, {"rows": prepared_rows})
        except _pcs.ReceiptHoldError:
            if own_pick is not None:
                _ms.release_local_pick(own_pick)
                own_pick = None
            return _receipt_short(
                account_key, draft_id, action_id, 409,
                "This post needs a manual review before its photo can be swapped.",
                "swap_manual_review")
        except _pcs.ReceiptConflictError:
            # SQLSTATE 23514 from apply: the transaction raised and ROLLED BACK
            # -- nothing was written. Releasing our own unused reservation is
            # safe; the group itself is stale, so the client is told to stop.
            if own_pick is not None:
                _ms.release_local_pick(own_pick)
                own_pick = None
            return _receipt_short(account_key, draft_id, action_id, 409,
                                  "This post's swap group changed; nothing was swapped.",
                                  "swap_group_stale")
        except _pcs.ReceiptSelectionError:
            if own_pick is not None:
                _ms.release_local_pick(own_pick)
                own_pick = None
            return _receipt_short(account_key, draft_id, action_id, 409,
                                  "verified media evidence is unavailable",
                                  "media_evidence_unavailable")
        except Exception:
            # UNKNOWN: the apply may have committed. Never infer the outcome
            # from a mutable row; retain the reservation; the SAME apply is
            # replayed by the next request with this action_id.
            return _receipt_unknown(account_key, draft_id, action_id)
        final_status = final_receipt.get("status")
        if final_status == "failed":
            if own_pick is not None:
                _ms.release_local_pick(own_pick)
                own_pick = None
            return _receipt_failed_body(account_key, draft_id, action_id,
                                        final_receipt)
        if final_status != "succeeded":
            return _receipt_unknown(account_key, draft_id, action_id)
    except Exception as exc:  # noqa: BLE001
        # Before apply starts nothing can have landed and our exact reservation
        # is safe to release. Once apply starts the outcome is UNKNOWN: retain.
        if own_pick is not None and not apply_started:
            _ms.release_local_pick(own_pick)
        body = {"ok": False, "action": _RECEIPT_ACTION, "draft_id": draft_id,
                "action_id": action_id,
                "error": f"store error: {type(exc).__name__}"}
        return (503 if apply_started else 500), body

    # Success is ONLY the persisted terminal receipt. Settle the usage ledgers
    # against the FROZEN selection (a loser reservation was already released
    # and is never settled): best-effort, idempotent, and re-run by every
    # terminal replay, so a lost response can never skip settlement.
    _settle_receipt_ledgers(_ms, account_key, final_receipt, own_pick=own_pick)
    return 200, _receipt_success_body(account_key, draft_id, action_id,
                                      final_receipt)


def handle_swap_media(account_key, draft_id, actor_id, reader=None, sb_store=None,
                      picker=None, action_id=_NO_ACTION_ID):
    """Swap this post's photo for a fresh one. FREE and unlimited: the budget is
    neither read nor charged, and the response echoes the UNCHANGED budget so the
    client can see it cost nothing. The caption is untouched.

    Flag: config.media_swap_free_enabled() (ECHO_MEDIA_SWAP_FREE, default OFF).
    Flag off -> 403 and not one store read is issued.

    action_id (DRAFT receipts v3, 2026-10-04): an explicit opaque client
    idempotency key. When present the swap runs the durable receipt path
    (ECHO_SWAP_ACTION_RECEIPT, default OFF -> 503): begin binds the request
    tuple before any row read, one claim freezes the exact active logical-post
    group, one apply writes it atomically, and an exact replay returns the
    persisted terminal receipt. Old callers pass nothing and get byte-for-byte
    today's legacy behavior."""
    return _handle_swap_media(account_key, draft_id, actor_id, reader=reader,
                              sb_store=sb_store, picker=picker,
                              action_id=action_id)


def handle_fixer_swap_media(account_key, draft_id, actor_id, reader=None, sb_store=None,
                            picker=None):
    """Run the same guarded photo swap from the authenticated Fixer ops lane.

    The client portal can remain dark while a support ticket is repaired. This
    bypasses the client portal's feature and Stripe view gates after the existing
    fail-closed Echo-client entitlement verifies the tenant. The media-swap flag,
    tenant-scoped row read, status, consent/picker, and server-side write guards
    still apply. It is intentionally not routed by the public portal HTTP handler.
    """
    return _handle_swap_media(account_key, draft_id, actor_id, reader=reader,
                              sb_store=sb_store, picker=picker,
                              allow_portal_social_disabled=True,
                              require_fixer_entitlement=True)


def _handle_swap_media(account_key, draft_id, actor_id, reader=None, sb_store=None,
                       picker=None, allow_portal_social_disabled=False,
                       require_fixer_entitlement=False, action_id=_NO_ACTION_ID):
    """Shared implementation for public and authenticated Fixer swap requests."""
    short = _action_gates(account_key, draft_id, actor_id, reader,
                          allow_portal_social_disabled=allow_portal_social_disabled,
                          allow_client_billing_inactive=require_fixer_entitlement)
    if short is not None:
        return short
    if require_fixer_entitlement:
        try:
            from . import echo_clients
            entitled = echo_clients.is_echo_client(account_key)
        except Exception:  # noqa: BLE001 - entitlement uncertainty must refuse
            entitled = False
        if not entitled:
            return 403, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                         "error": "not_echo_client", "account_key": account_key}
    from . import media_swap as _ms
    if not _ms.enabled():
        return 403, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                     "error": "photo swap is not enabled for this gym"}
    if not config.portal_calendar_supabase_enabled():
        return 503, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                     "error": "photo swap needs the shared calendar plane"}
    if action_id is not _NO_ACTION_ID and not _swap_receipt_gym_allowed(account_key):
        # Tenant allowlist (ECHO_SWAP_ACTION_RECEIPT_GYMS): an explicit
        # action_id from a gym outside the list fails CLOSED with a 503
        # BEFORE any store call -- never a silent fallback to the legacy
        # non-idempotent path.
        return 503, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                     "account_key": account_key,
                     "error": "action receipts are not enabled for this gym",
                     "reason": "receipt_gym_not_allowed"}
    sb_store = sb_store or _pcs.SupabaseCalendarStore()
    if action_id is not _NO_ACTION_ID:
        # Durable receipt path: begin runs BEFORE the first calendar row read,
        # so an exact same-binding replay resolves from the receipt even when
        # the row was since deleted or its media moved. The legacy path below
        # is reached only when no action_id was sent at all.
        return _handle_swap_media_receipt(sb_store, account_key, draft_id,
                                          actor_id, action_id, picker=picker)
    pick = None
    local_landed = False
    primary_write_started = False
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        final = _published_is_final(row, "swap-media", draft_id)
        if final is not None:
            return final
        # SIBLINGS MOVE WITH THE CLICKED ROW (audit 3c): the FB mirror and the paired
        # story of this post share its media. Swapping only the clicked row left
        # Facebook publishing the rejected still. Read the day's rows once, find the
        # same-post siblings, and have the picker shape the SAME new creative for each
        # (a story sibling is re-burned with its own caption).
        month_rows = _month_rows_for(sb_store, account_key, row)
        # Only siblings that CAN move are asked for (pending / coach_review); an
        # approved or live sibling keeps the pixels the gym approved and is reported
        # below. Every requested variant is computed by the picker BEFORE any write
        # (all or nothing): one failed variant is a 409 and nothing changes.
        all_siblings = _ms.sibling_rows(row, month_rows or [],
                                        lib=_ms.library_path_for(account_key))
        siblings = [s for s in all_siblings
                    if str(s.get("status") or "").lower() in ("pending", "coach_review")]
        locked_siblings = [str(s.get("id") or "") for s in all_siblings if s not in siblings]
        pick = (picker or _ms.pick_replacement)(account_key, row, store=sb_store,
                                                 siblings=siblings)
        if not pick.get("ok"):
            # A swap that cannot happen is a 409 the client can read, NOT a charge.
            return 409, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                         "error": _ms.client_message(pick.get("reason")),
                         "reason": pick.get("reason"),
                         "failed_sibling": pick.get("failed_sibling"),
                         "recreate_budget": _budget_state(account_key)}
        variants = pick.get("siblings") or {}
        missing = [str(s.get("id")) for s in siblings
                   if not (variants.get(str(s.get("id"))) or {}).get("ok")]
        if missing:
            # A picker that could not shape one sibling: nothing is written.
            return 409, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                         "error": _ms.client_message(_ms.REASON_STORY_REBURN),
                         "reason": _ms.REASON_STORY_REBURN, "failed_sibling": missing[0],
                         "recreate_budget": _budget_state(account_key)}
        from . import visual_writer_prepare
        if visual_writer_prepare.enabled():
            poster_byte_cache = {}
            for variant in [pick] + [variants[str(s.get("id"))] for s in siblings]:
                if (not variant.get("source_media_url")
                        or (variant.get("source_media_url") != variant.get("image_url")
                            and not isinstance(variant.get("render_evidence"), dict))):
                    return 409, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                                 "error": "verified media evidence is unavailable",
                                 "reason": "media_evidence_unavailable",
                                 "recreate_budget": _budget_state(account_key)}
                if not _poster_evidence_is_current(
                        variant, visual_writer_prepare, poster_byte_cache):
                    return 409, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                                 "error": "verified poster evidence is unavailable",
                                 "reason": "poster_evidence_unavailable",
                                 "recreate_budget": _budget_state(account_key)}
        # The media identity travels WITH the pixels (2026-09-10): a video's poster
        # frame (or a cleared poster when a video row becomes a photo) and the Drive
        # asset id now on the row (or None when it left the Drive pool).
        write_args = {"source_media_url": pick.get("source_media_url"),
                      "extra_fields": _ms.swap_fields(pick)}
        if pick.get("render_evidence") is not None:
            write_args["render_evidence"] = pick["render_evidence"]
        if pick.get("poster_render_evidence") is not None:
            write_args["poster_render_evidence"] = pick["poster_render_evidence"]
        # LOCAL ONCE-USED: reserve before the first calendar mutation. A failed
        # served-ledger write holds the swap, so the new pixels cannot land while
        # remaining eligible for a later pick.
        if not _ms.reserve_local_pick(account_key, row, pick):
            return 503, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                         "error": "Could not reserve that photo safely. Please try again.",
                         "reason": "served_ledger_unavailable",
                         "recreate_budget": _budget_state(account_key)}
        # The media identity travels WITH the pixels (2026-09-10): a video's poster
        # frame (or a cleared poster when a video row becomes a photo) and the Drive
        # asset id now on the row (or None when it left the Drive pool).
        primary_write_started = True
        updated = sb_store.swap_media(account_key, draft_id, pick["image_url"],
                                      **write_args)
        if updated is None:
            # Prepared writes can land the PATCH and still return None when the
            # representation fails validation. Only a fresh, tenant-scoped row
            # showing the original identity and not the target proves a no-op.
            try:
                fresh = sb_store.get_row(account_key, draft_id)
            except Exception:  # noqa: BLE001 - unreadable outcome stays reserved
                fresh = None
            original_fields = ("image_url", "source_media_url", "thumbnail_url",
                               "source_media_asset_id")
            definite_noop = (isinstance(fresh, dict)
                             and str(fresh.get("gym_id")) == str(account_key)
                             and str(fresh.get("id")) == str(draft_id)
                             and all(fresh.get(field) == row.get(field)
                                     for field in original_fields)
                             and fresh.get("image_url") != pick["image_url"])
            if definite_noop:
                _ms.release_local_pick(pick)
                return 409, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                             "error": ("This post is already approved or live, so its "
                                       "photo is locked. Deny it if you want it redone."),
                             "recreate_budget": _budget_state(account_key)}
            return 503, {"ok": False, "action": "swap-media", "draft_id": draft_id,
                         "error": "Photo swap outcome could not be verified.",
                         "reason": "swap_outcome_unknown",
                         "recreate_budget": _budget_state(account_key)}
        local_landed = True
        # Same-post siblings, one operation, the SAME per-row server-side status guard
        # (a sibling approved between the read and this write matches nothing and is
        # reported as left).
        swapped, left = [draft_id], list(locked_siblings)
        sibling_results = []
        for sib in siblings:
            sid = str(sib.get("id") or "")
            var = variants[sid]
            try:
                write_args = {"source_media_url": var.get("source_media_url"),
                              "extra_fields": _ms.swap_fields(var)}
                if var.get("render_evidence") is not None:
                    write_args["render_evidence"] = var["render_evidence"]
                if var.get("poster_render_evidence") is not None:
                    write_args["poster_render_evidence"] = var["poster_render_evidence"]
                done = sb_store.swap_media(account_key, sid, var["image_url"],
                                           **write_args)
            except Exception as exc:  # noqa: BLE001 - one sibling never undoes the swap
                print(f"[portal-social] sibling swap failed for {sid}: {type(exc).__name__}")
                done = None
            if done is not None:
                swapped.append(sid)
                sibling_kind = _media_kind(done.get("image_url", ""))
                sibling_results.append({
                    "id": sid,
                    "image_public_url": (done.get("thumbnail_url")
                                         or done.get("image_url", "")),
                    "media_kind": sibling_kind,
                    "video_url": (done.get("image_url", "")
                                  if sibling_kind == "video" else None),
                })
            else:
                left.append(sid)
        # The write landed: settle the Drive usage ledger + the served ledger so the
        # asset now on the row cools down, and the one it replaced returns to the pool
        # ONLY when no remaining row on the book still carries it. A failed re-read
        # is None = unknown = leave it stamped (never [] = "nothing carries it").
        _ms.after_swap(account_key, row, pick,
                       book_rows=_month_rows_for(sb_store, account_key, row),
                       swapped_ids=swapped)
    except Exception as exc:
        # Before the remote call starts, nothing can have landed and the exact
        # reservation is safe to release. Once it starts, an exception is an
        # UNKNOWN outcome: retain fail closed because the row may have changed.
        if pick is not None and not local_landed and not primary_write_started:
            _ms.release_local_pick(pick)
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    return 200, {"ok": True, "action": "swap-media", "draft_id": draft_id,
                 "siblings_swapped": [s for s in swapped if s != draft_id],
                 "siblings_left": left,
                 "sibling_results": sibling_results,
                 # Display url: a video row's poster frame, else the media itself (the
                 # same rule _post_from_row applies for the calendar card).
                 "image_public_url": (updated.get("thumbnail_url")
                                      or updated.get("image_url", "")),
                 "media_kind": _media_kind(updated.get("image_url", "")),
                 "video_url": (updated.get("image_url", "")
                               if _media_kind(updated.get("image_url", "")) == "video"
                               else None),
                 "caption": updated.get("caption", ""),
                 "status": updated.get("status", "pending"),
                 "day_key": updated.get("post_date", ""),
                 "free": True,
                 "recreate_budget": _budget_state(account_key)}


def _month_rows_for(sb_store, account_key, row):
    """The gym's rows for the clicked row's month (the sibling search space + the
    post-swap book read). None on any failure = UNKNOWN: no sibling is swapped and
    media_swap.after_swap leaves the old asset stamped (an empty LIST would read as
    "nothing else carries it" and roll the asset back; audit 3c residual)."""
    lister = getattr(sb_store, "list_month", None)
    month = str((row or {}).get("post_date") or "")[:7]
    if lister is None or len(month) != 7:
        return None
    try:
        return [r for r in (lister(account_key, month) or []) if isinstance(r, dict)]
    except Exception:  # noqa: BLE001
        return None


def _account_for(account_key):
    """Best-effort Account for a gym's generation account (_ig, else base). Never
    raises; None when the registry has nothing for this key."""
    try:
        from .accounts import get_account
        return get_account(f"{account_key}_ig") or get_account(account_key)
    except Exception:  # noqa: BLE001
        return None


def _voice_for(account_key, account=None):
    """The gym's loaded VoiceDoc, via the SAME durable-first resolution every other
    build-time caller uses (client_media_sync._resolve_client_voice_path). None when
    the account or its bible is missing -- callers must treat that as 'cannot draft'."""
    account = account if account is not None else _account_for(account_key)
    if account is None:
        return None
    try:
        from . import client_media_sync as _cms
        from .voice import load_voice
        return load_voice(
            _cms._resolve_client_voice_path(account_key, account.voice_doc_path()))
    except Exception:  # noqa: BLE001
        return None


# ==========================================================================
# POST /portal/<token>/posts/<id>/recreate-caption
#
# THE MISSING HALF OF B6 (partial-regen, Blake, 2026-09-07): swap-media (above) made
# "the photo is wrong" free by keeping the caption and changing only the pixels. This
# is the other half -- "the caption is wrong" -- and rewrites ONLY the copy on the
# gym's EXACT SAME photo, instead of today's full deny/recreate, which regenerates
# BOTH and can hand back a different photo even when only the words were the
# problem. Unlike swap-media this STILL charges the budget: regenerating copy is the
# expensive act (see media_swap.py's own docstring). The write goes through
# SupabaseCalendarStore.patch_caption -- the SAME call a human caption edit already
# uses -- so an approved or live post can never have its caption silently rewritten.
# ==========================================================================

def handle_recreate_caption(account_key, draft_id, actor_id, reader=None,
                            sb_store=None):
    """Rewrite ONLY this post's caption, on its EXACT SAME photo. Costs one of the
    monthly 15 recreates. Flag: config.caption_recreate_scoped_enabled()
    (ECHO_CAPTION_RECREATE_SCOPED, default OFF -> 403, no store read)."""
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    from . import caption_swap as _cs
    if not _cs.enabled():
        return 403, {"ok": False, "action": "recreate-caption", "draft_id": draft_id,
                     "error": "caption-only recreate is not enabled for this gym"}
    if not config.portal_calendar_supabase_enabled():
        return 503, {"ok": False, "action": "recreate-caption", "draft_id": draft_id,
                     "error": "caption recreate needs the shared calendar plane"}
    if recreate_remaining(account_key) <= 0:
        return 409, {"ok": False, "action": "recreate-caption", "draft_id": draft_id,
                     "error": "recreate budget for this month is used up",
                     "reason": "budget_exhausted",
                     "recreate_budget": _budget_state(account_key)}
    account = _account_for(account_key)
    voice = _voice_for(account_key, account)
    if account is None or voice is None:
        return 500, {"ok": False, "action": "recreate-caption", "draft_id": draft_id,
                     "error": "this gym's voice doc is not configured"}
    sb_store = sb_store or _pcs.SupabaseCalendarStore()
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        final = _published_is_final(row, "recreate-caption", draft_id)
        if final is not None:
            return final
        from . import client_media_sync as _cms
        result = _cs.recreate_caption(
            account_key, row, account=account, voice=voice,
            banned_words=_cms._banned_words_for(account_key))
        if not result.get("ok"):
            # A scoped attempt that could not produce a clean result is a 409 the
            # client can read, NOT a charge -- the post's caption is unchanged.
            return 409, {"ok": False, "action": "recreate-caption",
                         "draft_id": draft_id,
                         "error": _cs.client_message(result.get("reason")),
                         "reason": result.get("reason"),
                         "recreate_budget": _budget_state(account_key)}
        updated = sb_store.patch_caption(account_key, draft_id, result["caption"])
        if updated is None:
            # patch_caption filters to status NOT IN (publishing, published): a row
            # that matched nothing went live between the read and the write.
            return 409, {"ok": False, "action": "recreate-caption",
                         "draft_id": draft_id,
                         "error": ("This post is already approved or live, so its "
                                   "caption is locked. Deny it if you want it "
                                   "redone."),
                         "recreate_budget": _budget_state(account_key)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    # STORY RE-BURN (independent audit, 2026-09-08): a story's caption is burned
    # into the media itself, not read as text -- PR #597's UI hides this button for
    # story-format posts, but that is client-side only, and every OTHER invariant
    # in this file (ownership, published-is-final, budget) is enforced server-side
    # regardless of what the client does. Without this, a direct call on a story
    # row (curl, a retried request, a future UI regression) would patch the DB
    # caption while the live image kept showing the OLD burned-in text -- DB and
    # pixels silently diverge. Same call `_handle_edit_supabase` already makes;
    # `maybe_reburn_story` is itself gated on `story_reburn.should_reburn(row)` and
    # is a no-op for a feed row, so this is always safe to call unconditionally.
    reburned = maybe_reburn_story(account_key, updated,
                                 updated.get("caption") or "", sb_store)
    # Charge the budget only after a successful, persisted recreate.
    spend_recreate(account_key)
    return 200, {"ok": True, "action": "recreate-caption", "draft_id": draft_id,
                 "caption": updated.get("caption", ""),
                 "status": updated.get("status", "pending"),
                 "day_key": updated.get("post_date", ""),
                 "story_reburned": bool(reburned),
                 "free": False,
                 "recreate_budget": _budget_state(account_key)}


# ==========================================================================
# Variant pairing (0318): "regenerate this photo" produces a v2 CANDIDATE
# side by side with the live creative, instead of overwriting it. A human
# picks between them via pick-variant, which does not touch approval status.
#
# GET  /portal/<token>/posts/<id>/variants      -- list the group (active + candidates)
# POST /portal/<token>/posts/<id>/regen-variant -- generate a new candidate FOR <id>
# POST /portal/<token>/posts/<id>/pick-variant  -- <id> IS the candidate; promote it
#
# Flag: config.variant_pairing_enabled() (ECHO_VARIANT_PAIRING, default OFF).
# ==========================================================================

def handle_list_variants(account_key, draft_id, actor_id, reader=None, sb_store=None):
    """The variant group (active + candidates) `draft_id` belongs to. Read-only,
    so NOT gated behind the ECHO_VARIANT_PAIRING flag -- there is nothing to
    show if no candidate was ever created (create is gated), and a client
    reading their own already-scoped calendar is never a new capability."""
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    if not config.portal_calendar_supabase_enabled():
        return 503, {"ok": False, "action": "variants", "draft_id": draft_id,
                     "error": "variant listing needs the shared calendar plane"}
    sb_store = sb_store or _pcs.SupabaseCalendarStore()
    try:
        group = sb_store.get_variant_group(account_key, draft_id)
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    if not group:
        return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
    return 200, {"ok": True, "action": "variants", "draft_id": draft_id,
                 "variants": [{
                     "id": v.get("id"), "variant_status": v.get("variant_status"),
                     "image_url": v.get("image_url"), "caption": v.get("caption"),
                     "status": v.get("status"), "thumbnail_url": v.get("thumbnail_url"),
                 } for v in group]}


def handle_regen_variant(account_key, draft_id, actor_id, reader=None, sb_store=None,
                         regen_fn=None):
    """Generate a NEW image for the logical post `draft_id` represents and store
    it as a linked 'candidate' row, WITHOUT touching `draft_id` itself. The
    live creative stays exactly what it was; the candidate awaits a pick.

    Flag: config.variant_pairing_enabled() (ECHO_VARIANT_PAIRING, default OFF
    -> 403, no store read, no Astra call)."""
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    from . import variant_regen as _vr
    if not _vr.enabled():
        return 403, {"ok": False, "action": "regen-variant", "draft_id": draft_id,
                     "error": "variant regeneration is not enabled for this gym"}
    if not config.portal_calendar_supabase_enabled():
        return 503, {"ok": False, "action": "regen-variant", "draft_id": draft_id,
                     "error": "variant regeneration needs the shared calendar plane"}
    sb_store = sb_store or _pcs.SupabaseCalendarStore()
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        final = _published_is_final(row, "regen-variant", draft_id)
        if final is not None:
            return final
        gen = regen_fn or _vr.generate_variant_image
        result = gen(row, account_key)
        if not result.get("ok"):
            return 409, {"ok": False, "action": "regen-variant", "draft_id": draft_id,
                         "error": _vr.client_message(result.get("reason")),
                         "reason": result.get("reason")}
        candidate_args = {}
        for field in ("thumbnail_url", "source_media_url", "render_evidence",
                      "poster_render_evidence"):
            if result.get(field) is not None:
                candidate_args[field] = result[field]
        candidate = sb_store.create_variant_candidate(
            account_key, row, result["image_url"], **candidate_args)
        if candidate is None:
            return 500, {"ok": False, "action": "regen-variant", "draft_id": draft_id,
                         "error": "the new image could not be saved as a candidate"}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    return 200, {"ok": True, "action": "regen-variant", "draft_id": draft_id,
                 "candidate": {"id": candidate.get("id"),
                              "image_url": candidate.get("image_url"),
                              "caption": candidate.get("caption"),
                              "variant_status": candidate.get("variant_status")}}


def handle_regen_variant_from_brief(account_key, draft_id, actor_id, brief,
                                    reader=None, sb_store=None, regen_fn=None):
    """Generate a NEW image for the logical post `draft_id` represents FROM A
    HUMAN-TYPED BRIEF, and store it as a linked 'candidate' row, WITHOUT
    touching `draft_id` itself. Companion to handle_regen_variant: that button
    regenerates from the post's own existing pillar/caption with no new input;
    this one is the "type what you want" button next to it — a deliberate,
    per-request ask, grounded in the gym's own on-file brand material
    (variant_regen_brief._brand_brain_facts), never invented. Human-initiated
    every time: there is no path that reaches this handler except an explicit
    call carrying a non-empty `brief` a person typed that turn.

    Flag: config.variant_pairing_enabled() (ECHO_VARIANT_PAIRING, default OFF
    -> 403, no store read, no Astra call) — same gate handle_regen_variant uses."""
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    from . import variant_regen_brief as _vrb
    if not _vrb.enabled():
        return 403, {"ok": False, "action": "regen-variant-brief", "draft_id": draft_id,
                     "error": "variant regeneration is not enabled for this gym"}
    brief_text = str(brief or "").strip()
    if not brief_text:
        return 400, {"ok": False, "action": "regen-variant-brief", "draft_id": draft_id,
                     "error": _vrb.client_message(_vrb.REASON_NO_BRIEF),
                     "reason": _vrb.REASON_NO_BRIEF}
    if not config.portal_calendar_supabase_enabled():
        return 503, {"ok": False, "action": "regen-variant-brief", "draft_id": draft_id,
                     "error": "variant regeneration needs the shared calendar plane"}
    sb_store = sb_store or _pcs.SupabaseCalendarStore()
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        final = _published_is_final(row, "regen-variant-brief", draft_id)
        if final is not None:
            return final
        gen = regen_fn or _vrb.generate_variant_from_brief
        result = gen(row, account_key, brief_text)
        if not result.get("ok"):
            return 409, {"ok": False, "action": "regen-variant-brief", "draft_id": draft_id,
                         "error": _vrb.client_message(result.get("reason")),
                         "reason": result.get("reason")}
        candidate_args = {}
        for field in ("thumbnail_url", "source_media_url", "render_evidence",
                      "poster_render_evidence"):
            if result.get(field) is not None:
                candidate_args[field] = result[field]
        candidate = sb_store.create_variant_candidate(
            account_key, row, result["image_url"], **candidate_args)
        if candidate is None:
            return 500, {"ok": False, "action": "regen-variant-brief", "draft_id": draft_id,
                         "error": "the new image could not be saved as a candidate"}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    return 200, {"ok": True, "action": "regen-variant-brief", "draft_id": draft_id,
                 "candidate": {"id": candidate.get("id"),
                              "image_url": candidate.get("image_url"),
                              "caption": candidate.get("caption"),
                              "variant_status": candidate.get("variant_status")}}


def handle_pick_variant(account_key, draft_id, actor_id, reader=None, sb_store=None):
    """Promote the candidate `draft_id` to 'active' for its group. `draft_id`
    here IS the candidate's own row id (the id the client is looking at in
    the side-by-side picker) -- ownership is still proven the same way every
    other action proves it (get_row is gym-scoped), and the actual atomic
    work happens server-side in content_calendar_swap_variant so this handler
    never has a read-then-write race window of its own.

    Flag: config.variant_pairing_enabled() (ECHO_VARIANT_PAIRING, default OFF
    -> 403, no store read)."""
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    from . import variant_regen as _vr
    if not _vr.enabled():
        return 403, {"ok": False, "action": "pick-variant", "draft_id": draft_id,
                     "error": "variant picking is not enabled for this gym"}
    if not config.portal_calendar_supabase_enabled():
        return 503, {"ok": False, "action": "pick-variant", "draft_id": draft_id,
                     "error": "variant picking needs the shared calendar plane"}
    sb_store = sb_store or _pcs.SupabaseCalendarStore()
    try:
        # Ownership check up front, same 404-on-cross-gym contract as every
        # other action -- the RPC ALSO re-checks gym_id itself (belt and
        # braces: the RPC is the true authority, this is just consistent UX).
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        result = sb_store.swap_variant(account_key, draft_id, actor=actor_id)
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    if not result.get("ok"):
        error = result.get("error", "unknown")
        status = 409
        if error == "not_found":
            status = 404
        return status, {"ok": False, "action": "pick-variant", "draft_id": draft_id,
                        "error": error}
    return 200, {"ok": True, "action": "pick-variant", "draft_id": draft_id,
                 "active_id": result.get("active_id"),
                 "archived_previous_active": result.get("archived_previous_active")}


def _handle_kill_supabase(account_key, draft_id, actor_id, confirm, reader, sb_store):
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    if not confirm:
        return 400, {"ok": False, "action": "kill", "draft_id": draft_id,
                     "error": "kill is permanent and requires confirm=true"}
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        final = _published_is_final(row, "kill", draft_id)
        if final is not None:
            return final
        updated = sb_store.set_status(account_key, draft_id,
                                      _pcs.action_status("kill"))
        if updated is None:
            return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
        return 200, _action_result("kill", draft_id, updated)
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}


# ==========================================================================
# POST /portal/<token>/posts/<id>/approve  (idempotent)
# ==========================================================================

def handle_approve(account_key, draft_id, actor_id, store=None, reader=None,
                   sb_store=None, expected_creative=None):
    """Approve a post. Idempotent: approving an already-APPROVED post is a clean 200
    no-op (never a double publish). With Supabase creds present, flips the shared
    content_calendar row's status to 'approved' (NO publish). Otherwise delegates to
    portal_approvals.approve, which runs the same gated publish Slack uses.

    expected_creative (optional): the portal's visible-card snapshot, required
    and compared ONLY when AGENT_APPROVAL_PROOF is ON; ignored (legacy) when
    OFF."""
    if config.portal_calendar_supabase_enabled():
        return _handle_approve_supabase(account_key, draft_id, actor_id, reader,
                                        sb_store or _pcs.SupabaseCalendarStore(),
                                        expected_creative=expected_creative)
    draft, short = _action_preamble(account_key, draft_id, actor_id, store, reader)
    if short is not None:
        return short
    # idempotent: already approved -> succeed without re-publishing
    if getattr(draft, "status", None) == DraftStatus.APPROVED:
        return 200, {"ok": True, "action": "approve", "draft_id": draft_id,
                     "detail": "Already approved.", "idempotent": True}
    result = _pa.approve(account_key, draft_id, actor_id, store=store)
    return (200 if result.get("ok") else 403), result


# ==========================================================================
# POST /portal/<token>/posts/<id>/edit  (note; re-runs the fabrication gate -> 422)
# ==========================================================================

def handle_edit(account_key, draft_id, actor_id, note="", store=None, reader=None,
                sb_store=None, reason=""):
    """Request a revision with a note. The note is re-run through the fabrication gate
    (rotation.is_gate_clean): a note that introduces a stat, percentage, or price with
    no approved receipt is REFUSED with 422 so an unsupported claim can never enter the
    caption. With Supabase creds present, a clean note keeps the shared row 'pending'
    and echoes the note (no schema change, no publish). Otherwise delegates to
    portal_approvals.edit.

    reason (optional): the approver's explicit 'reason why' note, distinct from the new
    caption. It is recorded into the gym's brain as the edit's style rule so the stated
    intent teaches the next caption (Dale, 2026-08-15). Persisted only through the
    Supabase learning path; the legacy path keeps its existing contract."""
    if config.portal_calendar_supabase_enabled():
        return _handle_edit_supabase(account_key, draft_id, actor_id, note, reader,
                                     sb_store or _pcs.SupabaseCalendarStore(),
                                     reason=reason)
    draft, short = _action_preamble(account_key, draft_id, actor_id, store, reader)
    if short is not None:
        return short
    if not _rotation.is_gate_clean(note):
        return 422, {"ok": False, "action": "edit", "draft_id": draft_id,
                     "error": "fabrication gate: the note carries a claim with no "
                              "approved receipt. Cite an approved source or drop the "
                              "figure."}
    result = _pa.edit(account_key, draft_id, actor_id, note=note, store=store)
    return (200 if result.get("ok") else 403), result


# ==========================================================================
# POST /portal/<token>/posts/<id>/deny  (decrements the 15/month budget -> 409)
# ==========================================================================

def handle_deny(account_key, draft_id, actor_id, note="", store=None, reader=None,
                sb_store=None, intent=""):
    """Deny a post with a reason. Each deny burns one unit of the server-enforced
    15/month recreate budget; the 16th deny in a month is refused with 409 (the gym
    asks for a fresh concept instead). The budget is spent ONLY when the underlying
    deny succeeds, so a failed deny never costs the gym a unit. With Supabase creds
    present, a successful deny flips the shared row's status to 'denied' (NO publish).

    intent (B6): the portal's deny reason chips are "Use a different photo" and
    "Caption needs work", and both used to land here and charge. intent="media"
    routes the photo chip to the FREE swap instead, so a gym can never run out of
    recreates fixing pixels. intent="caption" (partial-regen, 2026-09-07) routes the
    "Caption needs work" chip to a SCOPED caption-only recreate (caption_swap.py) that
    keeps the gym's EXACT SAME photo instead of today's full recreate, which can (and
    often does) hand back a different photo too even though only the words were
    wrong -- symmetric to the media chip's fix, and it still charges the budget
    (regenerating copy is the expensive act; see caption_swap.py). Gated on
    ECHO_CAPTION_RECREATE_SCOPED: with the flag off (or the scoped attempt itself
    refusing, e.g. no approved source left) this falls straight through to today's
    full deny/recreate, so a gym is never left with no path forward. Any other intent
    value (including the default) is the full recreate, charges exactly as before."""
    if str(intent or "").strip().lower() == "media":
        from . import media_swap as _ms
        if _ms.enabled():
            return handle_swap_media(account_key, draft_id, actor_id, reader=reader,
                                     sb_store=sb_store)
    if str(intent or "").strip().lower() == "caption":
        from . import caption_swap as _cs
        if _cs.enabled():
            status, body = handle_recreate_caption(
                account_key, draft_id, actor_id, reader=reader, sb_store=sb_store)
            # A scoped attempt that could not produce a clean caption-only result
            # (no approved source, photo unreachable, gate exhausted, budget) falls
            # through to the full recreate below rather than dead-ending the coach —
            # EXCEPT a budget-exhausted 409, which must stay a 409 (falling through
            # would double-spend nothing, since deny below re-checks the same budget
            # and correctly still refuses).
            if status == 200 or (isinstance(body, dict)
                                 and body.get("reason") == "budget_exhausted"):
                return status, body
    if config.portal_calendar_supabase_enabled():
        return _handle_deny_supabase(account_key, draft_id, actor_id, note, reader,
                                     sb_store or _pcs.SupabaseCalendarStore())
    draft, short = _action_preamble(account_key, draft_id, actor_id, store, reader)
    if short is not None:
        return short
    if recreate_remaining(account_key) <= 0:
        return 409, {"ok": False, "action": "deny", "draft_id": draft_id,
                     "error": "recreate budget for this month is used up",
                     "recreate_budget": _budget_state(account_key)}
    result = _pa.deny(account_key, draft_id, actor_id, note=note, store=store)
    if not result.get("ok"):
        return 403, result
    # Charge the budget only after a successful deny (never on a no-op or failure).
    spend_recreate(account_key)
    result["recreate_budget"] = _budget_state(account_key)
    return 200, result


# ==========================================================================
# POST /portal/<token>/posts/<id>/deny-day  (Dean/Reverb, 2026-09-10)
#
# THE COMPLAINT: "one day of posts consists of the same picture + caption across
# 3-4 formats for post/story/GMB/etc, and I apparently have to click on each format
# and deny and request a re-work on each one? ... It seems like it will then return
# different caption+pictures across different formats on the same day." Denying one
# format at a time regenerated JUST that row, so a day could end up mixing a brand
# new rework on one format with the ORIGINAL rejected concept still sitting pending
# on the others. This is a day-wide action: find every same-day row for this gym
# still in a denyable status (pending/coach_review) -- feed, story, Facebook, AND
# Google Business, every format Dean named -- and deny them together, so the whole
# day reworks as ONE consistent concept instead of a patchwork.
# ==========================================================================

def handle_deny_day(account_key, draft_id, actor_id, note="", store=None, reader=None,
                    sb_store=None):
    """Deny every denyable same-day row for this gym together (one action instead of
    one click per format). Charges the recreate budget EXACTLY ONCE for the whole
    day, not once per format -- the day is one concept, not N separate recreates.

    DOUBLE-CHARGE GUARD (independent audit, 2026-09-10): a naive check-then-act
    (read budget, write N rows, spend once) still double-charges if two deny-day
    calls race on the SAME set of rows (a double-click, a client retry) -- both
    read the budget before either spends, both successfully re-PATCH the same
    already-pending rows (idempotent at the DB layer), and both then charge. The
    charge is deduped on a kv stamp keyed by the EXACT sorted set of target row
    ids: a genuine repeat call denying the SAME rows shares the key and is
    skipped; a LATER, legitimate deny-day on that day's NEXT rework (a fresh set
    of row ids after a rebuild) hashes differently and still charges. This
    narrows the race to a single local kv read+write (serialized by db.py's own
    lock within one process) rather than eliminating cross-process races
    entirely -- the same tolerance level as every other kv-stamped dedup in this
    codebase, never a distributed lock.

    Supabase-only (the shared content_calendar plane is what carries a day's other
    formats); 503s on the local-drafts plane, which has no day-spanning book."""
    if not config.portal_calendar_supabase_enabled():
        return 503, {"ok": False, "action": "deny-day", "draft_id": draft_id,
                     "error": "deny-day needs the shared calendar plane"}
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    sb_store = sb_store or _pcs.SupabaseCalendarStore()
    try:
        row, miss = _sb_load_owned_row(account_key, draft_id, sb_store)
        if miss is not None:
            return miss
        final = _published_is_final(row, "deny-day", draft_id)
        if final is not None:
            return final
        day_key = str(row.get("post_date") or "")[:10]
        month_rows = _month_rows_for(sb_store, account_key, row) or [row]
        denyable_statuses = {"pending", "coach_review"}
        targets = [r for r in month_rows
                  if str(r.get("post_date") or "")[:10] == day_key
                  and str(r.get("status") or "").lower() in denyable_statuses]
        if not targets:
            # Already denied/approved/published elsewhere: report the clicked row's
            # own state rather than a 404 -- the day may be clean by now.
            return 200, {"ok": True, "action": "deny-day", "draft_id": draft_id,
                         "detail": "Nothing left to deny for this day.",
                         "idempotent": True, "day_key": day_key,
                         "recreate_budget": _budget_state(account_key)}
        if recreate_remaining(account_key) <= 0:
            return 409, {"ok": False, "action": "deny-day", "draft_id": draft_id,
                         "error": "recreate budget for this month is used up",
                         "recreate_budget": _budget_state(account_key)}
        # Stamp keyed to the EXACT target set, computed BEFORE any write, so the
        # dedupe check happens as early as possible (narrowest race window).
        import hashlib
        target_ids = sorted(str(r.get("id") or "") for r in targets if r.get("id"))
        charge_key = ("denyday_charged_" + account_key + "_" + day_key + "_"
                     + hashlib.sha256("|".join(target_ids).encode()).hexdigest()[:16])
        from . import db as _db
        already_charged = bool(_db.kv_get(charge_key))
        if not already_charged:
            _db.kv_set(charge_key, "1")
        denied_ids = []
        for r in targets:
            rid = str(r.get("id") or "")
            if not rid:
                continue
            updated = sb_store.set_status(account_key, rid, _pcs.action_status("deny"))
            if updated is not None:
                denied_ids.append(rid)
        if not denied_ids:
            return 404, {"ok": False, "error": "no denyable rows found",
                         "draft_id": draft_id}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}
    # ONE unit for the whole day (Dean's actual complaint: N clicks, N charges today) --
    # skipped when a raced duplicate call already charged for this exact row set.
    if not already_charged:
        spend_recreate(account_key)
    return 200, {"ok": True, "action": "deny-day", "draft_id": draft_id,
                "day_key": day_key, "denied_ids": denied_ids,
                "recreate_budget": _budget_state(account_key)}


# ==========================================================================
# POST /portal/<token>/posts/<id>/kill  (permanent, free, requires confirm=true)
# ==========================================================================

def handle_kill(account_key, draft_id, actor_id, confirm=False, store=None, reader=None, sb_store=None):
    """Permanently ban this creative concept for THIS gym only. Free (never charges the
    recreate budget) and permanent. Requires confirm=true; without it, 400 and nothing
    happens. With Supabase creds present, flips the shared row's status to 'killed' (NO
    publish). Otherwise delegates to portal_approvals.kill (confirmed=True)."""
    if config.portal_calendar_supabase_enabled():
        return _handle_kill_supabase(account_key, draft_id, actor_id, confirm, reader,
                                     sb_store or _pcs.SupabaseCalendarStore())
    draft, short = _action_preamble(account_key, draft_id, actor_id, store, reader)
    if short is not None:
        return short
    if not confirm:
        return 400, {"ok": False, "action": "kill", "draft_id": draft_id,
                     "error": "kill is permanent and requires confirm=true"}
    result = _pa.kill(account_key, draft_id, actor_id, confirmed=True, store=store)
    return (200 if result.get("ok") else 403), result


# ==========================================================================
# POST /portal/<token>/autonomy  -> flip per-account autonomy on/off
# ==========================================================================

def _autonomy_actor(account_key):
    """The actor id an autonomous auto-approve acts AS. The gym owner flipped the
    toggle in the portal, so the approval is made on the account's OWN authority: its
    first configured approver, falling back to the global approver (the same fallback
    account.approver_ids() already uses). This keeps every auto-approve inside the
    existing approver gate rather than bypassing it."""
    from .accounts import get_account as _get_acct
    acct = _get_acct(account_key)
    if acct is not None:
        try:
            ids = acct.approver_ids()
            if ids:
                return ids[0]
        except Exception:
            pass
    return config.APPROVER_SLACK_ID


def _pending_ids_for(account_key, store):
    """Draft ids of THIS account's currently-PENDING posts. Scoped to account_key
    (isolation: gym A never sees gym B's pending). Tolerates a store without
    list_pending (returns none, so the flip still succeeds with approved_count 0)."""
    lister = getattr(store, "list_pending", None)
    if lister is None:
        return []
    try:
        pending = lister() or []
    except Exception:
        return []
    ids = []
    for d in pending:
        if (getattr(d, "account_key", None) or "") != account_key:
            continue  # TOKEN ISOLATION: only this account's drafts
        if getattr(d, "status", None) != DraftStatus.PENDING:
            continue
        did = getattr(d, "draft_id", "") or ""
        if did:
            ids.append(did)
    return ids


def handle_autonomy(account_key, autonomous, actor_id=None, store=None, reader=None):
    """Persist automatic/manual handling for this gym's entire pending queue.

    Automatic mode publishes eligible pending posts at their scheduled times
    without further approval. Saving the mode is not a permanent human approval:
    it must not bulk-approve or publish the queue inside this request. Returning
    to manual therefore stops automatic handling of still-pending posts, while
    explicit approvals and posts already sent remain intact.
    """
    if not config.portal_social_enabled():
        return _disabled("autonomy")
    if not account_key:
        return 400, {"ok": False, "error": "missing account_key"}
    if not is_social_active(account_key, reader=reader):
        return 402, {"ok": False, "error": "social plan is not active",
                     "account_key": account_key,
                     "autonomous": _db.is_autonomous(account_key)}

    want_on = bool(autonomous)
    # Persist first so a later approve crash cannot leave the flag unset while posts
    # went out under it (the flag is the durable record of the client's choice).
    try:
        _db.set_autonomy(account_key, want_on)
    except Exception as exc:
        return 500, {"ok": False,
                     "error": f"could not persist autonomy: {type(exc).__name__}",
                     "account_key": account_key}
    # DUAL-WRITE the SHARED plane: the publish lane (a different Railway service with
    # its own empty SQLite) reads echo_gym_settings.autonomous via gym_autonomy. The
    # local kv write above alone would return {ok:true} while the publisher never sees
    # the flag. Best effort here — the kv is still the local record; a Supabase outage
    # must not 500 the toggle — but a miss is REPORTED in the response, never silent.
    shared_persisted = False
    try:
        if config.portal_calendar_supabase_enabled():
            from .portal_calendar_store import SupabaseCalendarStore
            base = account_key
            for suf in ("_ig", "_fb"):
                if base.endswith(suf):
                    base = base[: -len(suf)]
                    break
            shared_persisted = bool(SupabaseCalendarStore().set_gym_autonomy(
                base, want_on, actor=actor_id or ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[portal-social] autonomy shared-plane write failed for "
              f"{account_key}: {type(exc).__name__}")

    if not want_on:
        # Manual restored: clear only. Never un-approve anything already published.
        # HARD ERROR when the OFF write did not reach the shared plane (audit
        # 2026-08-25 MAJOR): the publish lane reads Supabase — an OFF that only landed
        # in the local kv leaves the gym AUTO-PUBLISHING posts the owner explicitly
        # de-authorized, behind an {ok:true}. Turning autonomy ON degrades safely
        # (worst case: posts wait for manual approval), so ON keeps best-effort; OFF
        # is the de-authorization and must be durable or loudly fail so the client
        # retries. (When the shared plane is not configured, local kv IS the plane.)
        if config.portal_calendar_supabase_enabled() and not shared_persisted:
            return 503, {"ok": False, "autonomous": True,
                         "account_key": account_key, "shared_persisted": False,
                         "error": "could not durably turn autonomy OFF (shared store "
                                  "write failed); the gym would keep auto-publishing. "
                                  "Please try again."}
        return 200, {"ok": True, "autonomous": False, "approved_count": 0,
                     "account_key": account_key,
                     "shared_persisted": shared_persisted}

    return 200, {"ok": True, "autonomous": True, "approved_count": 0,
                 "account_key": account_key, "shared_persisted": shared_persisted}


def handle_cadence(account_key, posts_per_day, actor_id=None, reader=None):
    """POST /portal/<token>/cadence  body {"posts_per_day": 1|2}.

    Persists the gym's posting-cadence preference (CADENCE_SPEC.md). The preference
    is SAVED regardless of ECHO_CADENCE_2X_ENABLED — the kill switch gates behavior,
    not the client's stored choice (Blake's spec: 'even if a gym toggles 2x in the
    portal, the feature does nothing until the env flag is armed by hand').

    DUAL-WRITE, SHARED PLANE REQUIRED: the local kv row is only this service's
    record; the worker's planners read echo_gym_settings.posts_per_day. Unlike the
    autonomy ON path (which degrades safely), a cadence write that misses the shared
    plane is INVISIBLE to the worker in BOTH directions — a saved 2x that never
    doubles, or worse a saved 1x that keeps posting 2x against the client's wish.
    So when the Supabase plane is configured, a failed shared write is a 503 and
    the client retries; when it is not configured, the local kv IS the plane.

    REPLAN: the endpoint never replans inline (this service does not render). The
    worker's daily lane detects a cadence change (cadence_applied stamp mismatch)
    and rebuilds UNAPPROVED FUTURE days only. Response carries replanned=false and
    replan='next daily cycle' so the caller can set expectations honestly.

    Gates: flag OFF -> disabled (404); missing account -> 400; bad value -> 400;
    Stripe social product not ACTIVE -> 402. Never weakens approval/publish gates:
    cadence only changes how many PAUSED pending drafts a day carries."""
    if not config.portal_social_enabled():
        return _disabled("cadence")
    if not account_key:
        return 400, {"ok": False, "error": "missing account_key"}
    try:
        want = int(posts_per_day)
    except (TypeError, ValueError):
        want = 0
    if want not in (1, 2):
        return 400, {"ok": False, "error": "posts_per_day must be 1 or 2",
                     "account_key": account_key}
    if not is_social_active(account_key, reader=reader):
        return 402, {"ok": False, "error": "social plan is not active",
                     "account_key": account_key}

    base = account_key
    for suf in ("_ig", "_fb"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break

    # Local record first (mirrors autonomy: the kv is the durable local choice).
    try:
        _db.set_posts_per_day(base, want)
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False,
                     "error": f"could not persist cadence: {type(exc).__name__}",
                     "account_key": account_key}

    # Shared plane: REQUIRED when configured (see docstring). Honest 503 on a miss.
    shared_persisted = False
    if config.portal_calendar_supabase_enabled():
        try:
            from .portal_calendar_store import SupabaseCalendarStore
            shared_persisted = bool(SupabaseCalendarStore().set_gym_posts_per_day(
                base, want, actor=actor_id or ""))
        except Exception as exc:  # noqa: BLE001
            print(f"[portal-social] cadence shared-plane write failed for "
                  f"{account_key}: {type(exc).__name__}")
        if not shared_persisted:
            return 503, {"ok": False, "posts_per_day": want,
                         "account_key": account_key, "shared_persisted": False,
                         "error": "could not durably save the posting cadence "
                                  "(shared store write failed). Please try again."}

    return 200, {"ok": True, "posts_per_day": want, "account_key": account_key,
                 "shared_persisted": shared_persisted,
                 "cadence_armed": config.cadence_2x_enabled(),
                 "replanned": False, "replan": "next daily cycle"}


# ==========================================================================
# GET /portal/<token>/metrics  -> the Part D report SHAPE (null values until Part C/D)
# ==========================================================================

def _baseline_posts_per_week(account_key):
    """The gym's pre-Echo posting cadence, as (posts_per_week, captured_at).

    LOCAL SQLite first (a worker that has one keeps working exactly as before), then the
    SHARED plane, which is where the Apify capture actually writes it. The fallback is the
    load-bearing half: db.set_baseline_posts_per_week has no production caller (only test
    helpers), while agent/social_baseline.py writes every real measurement to Supabase
    social_baseline. So the local read was always empty in production, the portal rendered
    "capturing your baseline" forever, and every before/after pair came back
    {before: null} -- which is why the cards read "with Echo so far" instead of the real
    "Before Echo -> With Echo". The intake-web service has no volume, so it can ONLY see
    the shared plane. Never raises: a baseline is a nice-to-have, not a reason to 500 a
    metrics read.
    """
    try:
        ppw, at = _db.get_baseline_posts_per_week(account_key)
        if ppw is not None:
            return ppw, at
    except Exception:
        pass
    try:
        return _pcs.SupabaseCalendarStore().social_baseline_posts_per_week(account_key)
    except Exception:
        return None, None


def _metrics_shape(account_key, days):
    """The Part D metrics payload SHAPE. Part C wires the real Zernio analytics
    numbers into this exact shape; Part D assembles the before/after story. Until then
    every value is null / empty (a GAP), NEVER a fabricated 0. Each metric names its
    availability so the portal renders "not available on this account" rather than a
    made-up number. The baseline (Part A, real if captured) is included so Part D's
    before/after has its "before" the moment analytics land.

    Availability booleans reflect the flags: analytics_available reads
    AGENT_ZERNIO_ANALYTICS_ENABLED, report_available reads AGENT_MONTHLY_REPORT_ENABLED.
    Both OFF today, so the portal shows the report as pending, honestly."""
    baseline_ppw, baseline_at = _baseline_posts_per_week(account_key)
    return {
        "account_key": account_key,
        "window_days": days,
        "analytics_available": config.zernio_analytics_enabled(),
        "report_available": config.monthly_report_enabled(),
        # DATA SOURCE HONESTY: "zernio" is reserved for numbers that came from a LIVE
        # Zernio pull (map_metrics on a hasAnalyticsAccess payload). This is the
        # SEED / unavailable shape: it carries no live numbers, so its data_source is
        # None. Labeling this "zernio" merely because the flag is on would brand a seed
        # payload as real data, which is the fabrication the honesty gate forbids.
        "data_source": None,
        # NARRATIVE GATE (the caption fabrication gate applied to metrics prose): Echo
        # emits NO invented narrative here. The portal composes any prose. narrative
        # stays null unless the numbers came from a live Zernio pull.
        "narrative": None,
        # per-post engagement metrics Zernio DOES expose (Part C fills the list)
        "posts": [],
        "totals": {
            "posts_published": None,
            "likes": None,
            "comments": None,
            "saves": None,
            "shares": None,
        },
        # follower / reach / impressions may be unavailable per account: GAPS, not 0s.
        # followers is a TOTAL (from Zernio accounts[].followersCount) when it lands;
        # follower_delta is a genuine 30-day change and stays null until one exists (the
        # portal shows "coming soon"), NEVER a delta computed from the total.
        "audience": {
            "followers": None,
            "follower_delta": None,
            "reach": None,
            "impressions": None,
        },
        # the before/after posting-frequency story (baseline is real if captured)
        "frequency": {
            "baseline_posts_per_week": baseline_ppw,
            "baseline_captured_at": baseline_at,
            "current_posts_per_week": None,
        },
        # proof of growth (Part D). Until a live Zernio pull lands, every leg is null so
        # the portal renders "coming soon" rather than a fabricated 0. followers stays
        # null even when analytics land (no dated follower series to derive a rate from).
        "before_after": {
            "followers_per_month": {"before": None, "after": None},
            "reach_per_month": {"before": None, "after": None},
            "saves_per_month": {"before": None, "after": None},
            "likes_per_month": {"before": None, "after": None},
            "comments_per_month": {"before": None, "after": None},
            "shares_per_month": {"before": None, "after": None},
        },
        # what performed best: null (not a shell of zeros) until a published month of
        # real data exists, so the portal shows "coming soon".
        "learnings": None,
        # explicit gap notes so the portal never substitutes a zero for missing data
        "gaps": ["Live analytics are not connected yet; no numbers are shown "
                 "rather than a made up zero."] if not config.zernio_analytics_enabled() else [],
    }


def _live_metrics(account_key, days, zclient):
    """Try a LIVE Zernio analytics pull for this gym, folded into the metrics SHAPE.

    Returns the real payload when the gym resolves to a Zernio profile AND that profile
    holds the analytics add-on (hasAnalyticsAccess). Returns None (so the caller falls
    back to the honest null shape) when the profile can't be resolved, Zernio errors, or
    the add-on is off. NEVER raises: a Zernio failure must never 500 this endpoint.

    Read-only: only ZernioClient.analytics_window is called (a GET). No writes, no
    publish. The Zernio key stays inside the client and is never logged here."""
    from . import zernio as _z
    from . import zernio_routes as _zr
    from . import zernio_analytics as _za

    try:
        client = zclient if zclient is not None else _z.ZernioClient()
        pid = _zr._resolve_profile_id(account_key) or client.find_profile_id(account_key)
        if not pid:
            return None
        analytics_json = client.analytics_window(pid, days)
        if not (analytics_json or {}).get("hasAnalyticsAccess"):
            return None
        baseline_ppw, baseline_at = _baseline_posts_per_week(account_key)
        payload = _za.map_metrics(analytics_json, days, baseline_ppw, baseline_at,
                                  account_key=account_key)
        # Overlay the flags the pure mapper leaves to the caller (report flag; gaps note
        # only when analytics is unavailable, which it is not on this real path).
        payload["report_available"] = config.monthly_report_enabled()
        # Honesty: if the analytics pull hit its page cap, the totals cover only the
        # most recent posts in the window, so say so rather than present a partial
        # total as complete.
        payload["gaps"] = (
            ["These totals cover the most recent posts in the window; some older "
             "posts were not included."]
            if (analytics_json or {}).get("_pages_capped") else []
        )
        return payload
    except Exception:
        return None  # fail to the honest null shape, never a 500 and never a fake number


def handle_metrics(account_key, days=30, reader=None, zclient=None):
    """GET /portal/<token>/metrics?days=N. Returns the Part D report SHAPE for THIS gym.

    When AGENT_ZERNIO_ANALYTICS_ENABLED is ON and the gym's Zernio profile holds the
    analytics add-on, the shape carries REAL Zernio numbers (per _live_metrics +
    zernio_analytics.map_metrics). Otherwise every value stays null / empty (a GAP,
    never a fabricated 0). The Zernio client is injectable (zclient) so tests run offline.

    Gates: flag OFF -> disabled; Stripe social product not ACTIVE -> 402; TOKEN
    ISOLATION -> the baseline read, the profile resolution, and the whole payload are
    keyed to account_key. A Zernio error can never 500 this route (it falls to null)."""
    if not config.portal_social_enabled():
        return _disabled("metrics")
    if not account_key:
        return 400, {"error": "missing account_key"}
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    if days <= 0:
        days = 30
    days = min(days, 365)     # clamp: an unbounded ?days= flows into the Zernio pager
    if not is_social_active(account_key, reader=reader):
        return 402, {"account_key": account_key, "active": False,
                     "window_days": days, "posts": [], "totals": {}, "audience": {},
                     "frequency": {}, "gaps": ["Social plan is not active."]}

    if config.zernio_analytics_enabled():
        live = _live_metrics(account_key, days, zclient)
        if live is not None:
            return 200, live

    return 200, _metrics_shape(account_key, days)
