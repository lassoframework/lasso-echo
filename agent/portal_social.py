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

import os
from datetime import datetime, timezone

from . import config, db as _db
from . import portal_approvals as _pa
from . import portal_calendar_store as _pcs
from . import rotation as _rotation
from .drafter import DraftStatus


# The server-enforced recreate budget: 15 denies per calendar month per gym. This is
# NOT read from tenant data (which can default to zero); Part B guarantees every
# social gym the same 15. A deny burns one unit; the 16th deny in a month is refused
# with 409 so the gym asks for a fresh concept instead of burning the queue.
MONTHLY_RECREATE_BUDGET = 15


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


def _calendar_post(row):
    """One gym_calendar_queue row folded into the portal post shape. Never invents a
    caption, stat, or connection; empty fields stay empty (Part A leaves content to a
    later phase). format is derived from is_story only (no fabricated metadata)."""
    return {
        "day_key": row.get("day_key"),
        "status": row.get("status") or "queued",
        "pillar": row.get("pillar") or "",
        "format": "story" if row.get("is_story") else "feed",
        "image_public_url": row.get("feed_url") or row.get("story_url") or "",
        "caption": row.get("caption") or "",
    }


def _content_calendar_post(row):
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
        "caption": row.get("caption") or "",
        "scheduled_at": scheduled_at,
        # Publish record (display only): when it actually went out + the vendor post id.
        "published_at": row.get("published_at"),
        "late_post_id": row.get("late_post_id"),
    }
    # gym_id scopes the hosted fallback card (media_host tenant isolation); the portal
    # post shape itself is unchanged (no new keys).
    return _with_display_image(post, tenant=row.get("gym_id"))


_VIDEO_URL_EXTS = (".mp4", ".mov", ".m4v", ".webm")


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


def _handle_social_supabase(account_key, month, now=None):
    """/social from the SHARED content_calendar table (the live portal data plane).
    Reads every row for THIS gym in the month via the same SupabaseCalendarStore that
    powers /calendar, returns each with a stable id + real format + image_public_url +
    caption. low_creative is honest: true only when NO row in the month carries a
    non-empty image_public_url; posts are never filtered out for a missing image."""
    try:
        sb = _pcs.SupabaseCalendarStore()
        rows = sb.list_month(account_key, month)
        posts = [_content_calendar_post(r) for r in rows]
    except Exception as exc:
        return 500, {"error": f"store error: {type(exc).__name__}"}

    # HONEST low_creative: computed from the RAW rows' image_url, BEFORE the
    # no-creative fallback substitutes hosted infographics into the display field —
    # else a month running on fallback cards never triggers the "upload media" nudge.
    low_creative = not any((r.get("image_url") or "").strip() for r in rows)
    _, days_remaining = _low_creative_and_days(
        account_key, month, today=(now.date() if now else None))
    awaiting_media, upload_url = _awaiting_media_signal(account_key, posts)
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
        posts = [_calendar_post(dict(r)) for r in rows]
    except Exception as exc:
        return 500, {"error": f"db error: {type(exc).__name__}"}

    low_creative, days_remaining = _low_creative_and_days(account_key, month,
                                                          today=(now.date() if now else None))
    awaiting_media, upload_url = _awaiting_media_signal(account_key, posts)
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


def _action_gates(account_key, draft_id, actor_id, reader):
    """The flag / ids / Stripe-active gates shared by BOTH data planes. Returns None to
    proceed, or (status, body) to short-circuit. Ownership is checked separately (the
    two planes prove ownership against different stores)."""
    if not config.portal_social_enabled():
        return _disabled("action")
    if not account_key:
        return (400, {"ok": False, "error": "missing account_key"})
    if not draft_id:
        return (400, {"ok": False, "error": "draft_id required"})
    if not actor_id:
        return (400, {"ok": False, "error": "actor_id required"})
    if not is_social_active(account_key, reader=reader):
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


def _handle_approve_supabase(account_key, draft_id, actor_id, reader, sb_store):
    short = _action_gates(account_key, draft_id, actor_id, reader)
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
            return 200, {"ok": True, "action": "approve", "draft_id": draft_id,
                         "detail": "Already approved.", "idempotent": True}
        if str(row.get("status") or "").lower() == "published":
            return 200, {"ok": True, "action": "approve", "draft_id": draft_id,
                         "detail": "Already published.", "idempotent": True}
        updated = sb_store.set_status(account_key, draft_id,
                                      _pcs.action_status("approve"))
        if updated is None:
            return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
        return 200, {"ok": True, "action": "approve", "draft_id": draft_id}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}


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


def _handle_edit_supabase(account_key, draft_id, actor_id, note, reader, sb_store,
                          reason=""):
    short = _action_gates(account_key, draft_id, actor_id, reader)
    if short is not None:
        return short
    # the fabrication gate runs BEFORE any store touch: an unsupported claim is refused
    # 422 whether or not the row exists, so a stat can never reach the caption.
    if not _rotation.is_gate_clean(note):
        return 422, {"ok": False, "action": "edit", "draft_id": draft_id,
                     "error": "fabrication gate: the note carries a claim with no "
                              "approved receipt. Cite an approved source or drop the "
                              "figure."}
    if not note:
        return 400, {"ok": False, "action": "edit", "draft_id": draft_id,
                     "error": "note (new caption text) is required for edit"}
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
        # LEARN: feed the (before -> after) edit AND the approver's explicit reason
        # into the gym's brain so future captions move toward the approver's taste
        # (Dale's youth-content guidance). The reason, when sent, is captured as the
        # edit's rule so the stated intent is not dropped.
        _learn_from_edit(account_key, before, note, reason=reason)
        return 200, {"ok": True, "action": "edit", "draft_id": draft_id,
                     "caption": updated.get("caption", ""),
                     "status": updated.get("status", "pending"),
                     "reason_captured": bool((reason or "").strip())}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}


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
    return 200, {"ok": True, "action": "deny", "draft_id": draft_id,
                 "recreate_budget": _budget_state(account_key)}


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
        return 200, {"ok": True, "action": "kill", "draft_id": draft_id}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}


# ==========================================================================
# POST /portal/<token>/posts/<id>/approve  (idempotent)
# ==========================================================================

def handle_approve(account_key, draft_id, actor_id, store=None, reader=None, sb_store=None):
    """Approve a post. Idempotent: approving an already-APPROVED post is a clean 200
    no-op (never a double publish). With Supabase creds present, flips the shared
    content_calendar row's status to 'approved' (NO publish). Otherwise delegates to
    portal_approvals.approve, which runs the same gated publish Slack uses."""
    if config.portal_calendar_supabase_enabled():
        return _handle_approve_supabase(account_key, draft_id, actor_id, reader,
                                        sb_store or _pcs.SupabaseCalendarStore())
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

def handle_deny(account_key, draft_id, actor_id, note="", store=None, reader=None, sb_store=None):
    """Deny a post with a reason. Each deny burns one unit of the server-enforced
    15/month recreate budget; the 16th deny in a month is refused with 409 (the gym
    asks for a fresh concept instead). The budget is spent ONLY when the underlying
    deny succeeds, so a failed deny never costs the gym a unit. With Supabase creds
    present, a successful deny flips the shared row's status to 'denied' (NO publish)."""
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
    """POST /portal/<token>/autonomy  body {"autonomous": true|false}.

    Flips per-account autonomy. On ON: persist the flag, then auto-approve EVERY
    currently-pending post for THIS account through the SAME gated approve path a
    manual approve uses (so publishing behaves identically and still obeys
    AGENT_PUBLISH_ENABLED inside publish()). On OFF: clear the flag and un-approve
    NOTHING. Returns {ok, autonomous, approved_count}.

    Idempotent + null-safe: flipping ON twice re-persists ON and only approves posts
    that are STILL pending (already-approved posts are not in the pending sweep, so
    they never double publish and are not re-counted). A bad/empty account or an
    approve failure never 500s: it returns a clean body.

    Gates: flag OFF -> disabled (404); missing account -> 400; Stripe social product
    not ACTIVE -> 402. TOKEN ISOLATION: only this account's pending drafts are ever
    touched (a draft belonging to another gym is skipped)."""
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
        return 200, {"ok": True, "autonomous": False, "approved_count": 0,
                     "account_key": account_key,
                     "shared_persisted": shared_persisted}

    # ON: auto-approve every currently-pending post for THIS account via the same
    # gated approve path a manual approve uses. Never fabricates a publish.
    actor = actor_id or _autonomy_actor(account_key)
    approved = 0
    for draft_id in _pending_ids_for(account_key, store):
        try:
            result = _pa.approve(account_key, draft_id, actor, store=store)
            if result.get("ok"):
                approved += 1
        except Exception:
            # One bad draft never aborts the sweep or 500s the flip; the rest still
            # auto-approve and the flag stays ON for future posts.
            continue
    return 200, {"ok": True, "autonomous": True, "approved_count": approved,
                 "account_key": account_key, "shared_persisted": shared_persisted}


# ==========================================================================
# GET /portal/<token>/metrics  -> the Part D report SHAPE (null values until Part C/D)
# ==========================================================================

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
    baseline_ppw, baseline_at = _db.get_baseline_posts_per_week(account_key)
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
        baseline_ppw, baseline_at = _db.get_baseline_posts_per_week(account_key)
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
