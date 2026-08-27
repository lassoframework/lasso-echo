"""
chat_publish.py — Blake can publish directly from chat, scoped by account ownership.

Product promise (this is a promise, not a safety rail, and it does not come off):
  * LASSO-OWNED accounts (lasso_ig, lasso_fb, Blake's personal): a chat message with
    an EXPLICIT publish verb publishes immediately to feed and/or story, no approval
    card, and Echo replies with the live permalink.
  * CLIENT-OWNED accounts (any gym): chat can DRAFT and SCHEDULE, but publishing to a
    client account still goes through that client's own approval surface. Blake cannot
    chat-publish to a gym. Echo drafts it, schedules it pending, and says so plainly.

Accident guards (without adding friction):
  * Publishing requires an EXPLICIT publish verb ("post it", "publish", "send it
    live", "post to stories"). Generation words ("make", "try", "show me", "draft")
    NEVER publish.
  * Ambiguous intent asks ONE question; it never publishes on a maybe.
  * After a real publish Echo reports the live permalink.
  * "undo that" within 5 minutes deletes the just-published post (LASSO accounts
    only) and confirms; after the window it says to remove it by hand.

Still holds: only Blake's Slack id may trigger anything; the fabrication gate, grade
gate, and dash/vendor scan run on EVERY asset before publish (chat or scheduled) —
speed never bypasses quality; chat cannot change flags, secrets, or env; cost is
reported per request.

Everything here is pure + injectable (publish_fn / delete_fn / gate_fn / draft_fn /
now) so it is unit-testable with no Slack, no Meta, and no store. Behind
AGENT_CHAT_PUBLISH_ENABLED (default OFF).
"""

import os
import re
import time

from . import config

# ---- intent -----------------------------------------------------------------

PUBLISH = "publish"
GENERATE = "generate"
AMBIGUOUS = "ambiguous"
NONE = "none"
UNDO = "undo"

# explicit publish verbs — nothing else publishes
_PUBLISH_PATTERNS = [
    r"\bpost it\b", r"\bpost (?:this|that|them|these)\b", r"\bpublish\b",
    r"\bsend it (?:live|now|out)\b", r"\bgo live\b", r"\bship it\b",
    r"\bpost to (?:stories|story|the feed|feed)\b", r"\bpush it live\b",
]
# generation verbs — these NEVER publish
_GENERATE_PATTERNS = [
    r"\bmake\b", r"\btry\b", r"\bshow me\b", r"\bdraft\b", r"\bcreate\b",
    r"\bgenerate\b", r"\bmock ?up\b", r"\bwhip up\b", r"\bdesign\b", r"\bwrite\b",
]
_UNDO_PATTERNS = [r"\bundo that\b", r"\bundo\b", r"\bdelete that\b",
                  r"\btake it (?:down|back)\b", r"\bremove that post\b"]


def classify_intent(text):
    """Classify a chat message. PUBLISH only when an explicit publish verb is present
    AND no generation verb muddies it; a message with both is AMBIGUOUS (ask one
    question, never guess). Generation-only is GENERATE and never publishes."""
    t = (text or "").lower()
    if any(re.search(p, t) for p in _UNDO_PATTERNS):
        return UNDO
    has_pub = any(re.search(p, t) for p in _PUBLISH_PATTERNS)
    has_gen = any(re.search(p, t) for p in _GENERATE_PATTERNS)
    if has_pub and has_gen:
        return AMBIGUOUS
    if has_pub:
        return PUBLISH
    if has_gen:
        return GENERATE
    return NONE


def wants_story(text):
    """True when the message targets Stories specifically."""
    return bool(re.search(r"\bstor(?:y|ies)\b", (text or "").lower()))


def wants_feed(text):
    return bool(re.search(r"\b(?:feed|grid)\b", (text or "").lower()))


def target_surfaces(text):
    """Which surfaces the publish should hit. Default both if unspecified? No — the
    default is FEED; a Story is opt-in ('post to stories'). Returns a list."""
    story, feed = wants_story(text), wants_feed(text)
    if story and not feed:
        return ["story"]
    if story and feed:
        return ["feed", "story"]
    return ["feed"]


# ---- account ownership ------------------------------------------------------

def owned_accounts():
    """The LASSO-owned account keys that may be chat-published directly. Configurable
    by env (AGENT_LASSO_OWNED_ACCOUNTS, csv); defaults to the LASSO IG + FB + Blake's
    personal. Everything else is treated as a client account."""
    raw = os.environ.get("AGENT_LASSO_OWNED_ACCOUNTS",
                         "lasso_ig,lasso_fb,blake_personal")
    return {k.strip() for k in raw.split(",") if k.strip()}


def ownership(account_key):
    return "lasso" if account_key in owned_accounts() else "client"


# ---- outcome ----------------------------------------------------------------

class Outcome:
    """A routing decision. kind is one of: published, drafted_for_client, ask,
    blocked, denied, not_a_command, undone, undo_expired, undo_none, disabled."""

    def __init__(self, kind, message, permalink=None, draft_id=None, cost=None,
                 surfaces=None):
        self.kind = kind
        self.message = message
        self.permalink = permalink
        self.draft_id = draft_id
        self.cost = cost
        self.surfaces = surfaces or []

    def as_dict(self):
        return {"kind": self.kind, "message": self.message,
                "permalink": self.permalink, "draft_id": self.draft_id,
                "cost": self.cost, "surfaces": self.surfaces}


UNDO_WINDOW_SECONDS = 300

# most-recent publish per (actor, account) so "undo that" knows what to delete
_LAST_PUBLISH = {}


def _remember_publish(actor, account_key, media_ids, surfaces, when):
    _LAST_PUBLISH[(actor, account_key)] = {
        "media_ids": media_ids, "surfaces": surfaces, "when": when,
        "account_key": account_key}


def last_publish(actor, account_key=None):
    if account_key is not None:
        return _LAST_PUBLISH.get((actor, account_key))
    # most recent across accounts for this actor
    mine = [(k, v) for k, v in _LAST_PUBLISH.items() if k[0] == actor]
    if not mine:
        return None
    return max(mine, key=lambda kv: kv[1]["when"])[1]


# ---- routing ----------------------------------------------------------------

def route(text, account_key, actor_slack_id, asset=None, *, publish_fn=None,
          draft_fn=None, gate_fn=None, delete_fn=None, now=None,
          approver_id=None):
    """Route a chat message to publish / draft / ask / block, scoped by ownership.

    asset: the thing to act on (e.g. {"caption","paths":{feed,story},...}); opaque
      here and passed through to gate_fn / publish_fn / draft_fn.
    publish_fn(account_key, asset, surfaces) -> {"permalink","media_ids","cost"}
    draft_fn(account_key, asset, surfaces)   -> {"draft_id"}
    gate_fn(asset) -> {"ok": bool, "reason": str}   (fabrication + grade + dash/vendor)
    delete_fn(account_key, media_ids) -> bool
    All injected so this is testable; the listener passes the real implementations.
    """
    approver_id = approver_id or config.APPROVER_SLACK_ID
    now = now if now is not None else time.time()

    if not config.chat_publish_enabled():
        return Outcome("disabled", "Chat publishing is off "
                       "(AGENT_CHAT_PUBLISH_ENABLED is not set).")

    # only Blake may trigger anything
    if actor_slack_id != approver_id:
        return Outcome("denied", "Only Blake can trigger a publish.")

    intent = classify_intent(text)

    if intent == UNDO:
        return _handle_undo(actor_slack_id, now, delete_fn)

    if intent in (GENERATE, NONE):
        # not a publish command; generation is handled elsewhere and never publishes
        return Outcome("not_a_command",
                       "Nothing published. Say \"post it\" to publish, or ask me to "
                       "make/try/show a draft.")

    if intent == AMBIGUOUS:
        return Outcome("ask", "Do you want me to make a fresh one, or publish the "
                       "current draft? I won't publish on a maybe.")

    # intent == PUBLISH
    surfaces = target_surfaces(text)

    if ownership(account_key) == "client":
        # client account: draft + schedule pending for the client's own approval
        res = (draft_fn or _noop_draft)(account_key, asset, surfaces)
        return Outcome("drafted_for_client",
                       f"That's a client account ({account_key}), so I can't publish "
                       f"it for them. I've drafted it and scheduled it as pending for "
                       f"their approval on their surface.",
                       draft_id=(res or {}).get("draft_id"), surfaces=surfaces)

    # LASSO-owned: gates ALWAYS run before a direct publish (speed never bypasses
    # quality) — fabrication, grade, dash/vendor
    gate = (gate_fn or _pass_gate)(asset)
    if not gate.get("ok"):
        reason = gate.get("reason") or "failed the content gate"
        return Outcome("blocked", f"Held back: {reason}. Nothing published.")

    # a publish that raises (e.g. a personal profile Graph API cannot post to) must
    # degrade to a clean message, never a crash that escapes the listener
    try:
        res = (publish_fn or _noop_publish)(account_key, asset, surfaces) or {}
    except Exception as e:
        return Outcome("blocked", f"Couldn't publish to {account_key}: {e}. "
                       "Nothing published.")
    if res.get("error"):
        return Outcome("blocked", f"Couldn't publish to {account_key}: "
                       f"{res['error']}. Nothing published.")
    # honest about draft-only: publishing armed vs not (AGENT_PUBLISH_ENABLED)
    if res.get("mode") and res.get("mode") != "published":
        return Outcome("would_publish",
                       f"Draft-only: publishing is not armed (AGENT_PUBLISH_ENABLED "
                       f"is off), so nothing was written to {account_key}. Arm it to "
                       f"go live.", surfaces=surfaces)

    _remember_publish(actor_slack_id, account_key, res.get("media_ids", []),
                      surfaces, now)
    cost = res.get("cost")
    cost_line = f" (cost: {cost})" if cost is not None else ""
    return Outcome("published",
                   f"Published to {account_key} ({', '.join(surfaces)}). "
                   f"{res.get('permalink', '')}{cost_line}".strip(),
                   permalink=res.get("permalink"), cost=cost, surfaces=surfaces)


def _handle_undo(actor, now, delete_fn):
    rec = last_publish(actor)
    if not rec:
        return Outcome("undo_none", "Nothing recent to undo.")
    if ownership(rec["account_key"]) != "lasso":
        return Outcome("undo_expired", "That was a client draft, not a live post I "
                       "can pull. Nothing to undo.")
    if now - rec["when"] > UNDO_WINDOW_SECONDS:
        return Outcome("undo_expired", "That post is past the 5 minute undo window. "
                       "It needs to be removed manually.")
    ok = (delete_fn or (lambda a, m: False))(rec["account_key"], rec["media_ids"])
    if ok:
        _LAST_PUBLISH.pop((actor, rec["account_key"]), None)
        return Outcome("undone", f"Pulled it back down from {rec['account_key']}.")
    return Outcome("undo_expired", "I couldn't delete it automatically; it needs to "
                   "be removed manually.")


# ---- default no-op injectables (used only if a caller forgets to pass one) ---

def _pass_gate(asset):
    return {"ok": True}


def _noop_publish(account_key, asset, surfaces):
    return {"permalink": "", "media_ids": [], "cost": None}


def _noop_draft(account_key, asset, surfaces):
    return {"draft_id": ""}


# ==========================================================================
# Live integration: resolve the target account + asset, build the real
# publish / draft / gate / delete functions, and route. The listener calls
# handle_message(); everything real is lazily imported so import stays cheap
# and the routing core above remains pure/testable.
# ==========================================================================

def resolve_account_key(text, default="lasso_ig"):
    """Pick the target account from the message. Explicit key wins; else a platform
    word maps to the LASSO account; else the default LASSO IG."""
    t = (text or "").lower()
    m = re.search(r"\b([a-z0-9]+(?:_[a-z0-9]+)*_(?:ig|fb))\b", t)
    if m:
        return m.group(1)
    if re.search(r"\b(facebook|fb page|the page)\b", t):
        return "lasso_fb"
    if re.search(r"\b(instagram|the gram|ig)\b", t):
        return "lasso_ig"
    return default


def _latest_pending(store, account_key):
    """The most recent PENDING draft for an account (what 'post it' refers to)."""
    try:
        pend = [d for d in store.list_pending() if d.account_key == account_key]
    except Exception:
        return None
    if not pend:
        return None
    # prefer the most recently scheduled/created; day_key is an ISO date string
    return sorted(pend, key=lambda d: (d.day_key or "", d.draft_id))[-1]


def _real_gate_fn(store):
    """The SAME gate scheduled posts get, applied before a chat publish: the
    fabrication scan (unverified stats / claims) plus the dash + 'vendor' scan on the
    caption. Speed never bypasses quality."""
    def gate(draft):
        if draft is None:
            return {"ok": False, "reason": "nothing staged to publish"}
        # fabrication: reuse the canonical scan, no auto-block, just read the verdict
        try:
            from . import fabrication_scan
            rep = fabrication_scan.scan(store=store, auto_block=False)
            for b in rep.get("blocked", []):
                if b.get("draft_id") == getattr(draft, "draft_id", None):
                    return {"ok": False, "reason": f"fabrication gate: {b.get('reason')}"}
        except Exception as e:
            return {"ok": False, "reason": f"fabrication gate could not run ({e})"}
        # dash / en-dash / em-dash / 'vendor' scan on the caption
        cap = getattr(draft, "caption", "") or ""
        if any(ch in cap for ch in ("—", "–", "-")) or "vendor" in cap.lower():
            return {"ok": False, "reason": "copy scan: dash or 'vendor' in the caption"}
        return {"ok": True}
    return gate


def _real_publish_fn():
    """Publish an approved-in-chat draft to a LASSO account and read back the live
    permalink. Honors AGENT_PUBLISH_ENABLED (a real Meta write needs it armed)."""
    def publish(account_key, draft, surfaces):
        from . import meta_publisher, publish_confirm
        from .accounts import get_account
        if draft is None:
            return {"error": "nothing staged to publish"}
        acct = get_account(account_key)
        # LASSO-VIA-ZERNIO (AGENT_LASSO_VIA_ZERNIO): armed, an approve-in-chat for a
        # LASSO account publishes through the SAME Zernio client lane, never
        # Meta-direct (which reads as a second publisher in Zernio analytics). If the
        # setup is incomplete, HOLD with the ONE deduped alert — no Meta-direct
        # fallback (that would recreate the taint).
        from . import lasso_zernio_route as _lzr
        _via_zernio = _lzr.should_route(account_key)
        if _via_zernio:
            held = _lzr.held(account_key)
            if held:
                return {"error": "LASSO-via-Zernio setup incomplete (missing: "
                        + ", ".join(held) + "); held, not sent Meta-direct."}
        try:
            if _via_zernio:
                result = _lzr.publish(draft, acct)
            else:
                result = meta_publisher.publish(draft, acct)
        except Exception as e:
            # e.g. a personal profile the Graph API cannot post to, or a Zernio
            # setup/resolver failure
            return {"error": str(e)}
        permalink = ""
        media_ids = []
        if getattr(result, "media_id", ""):
            media_ids = [result.media_id]
            try:
                permalink = publish_confirm.confirm_publish(draft, acct, result) or ""
            except Exception:
                permalink = ""
        return {"permalink": permalink, "media_ids": media_ids, "cost": None,
                "mode": getattr(result, "mode", "")}
    return publish


def _real_draft_fn(store, poster):
    """For a CLIENT account: keep the draft PENDING and card it on the client's own
    approval surface. Never publishes."""
    def draft(account_key, d, surfaces):
        if d is not None:
            try:
                store.put(d)
                if poster is not None:
                    poster.post_approval_card(d)
            except Exception:
                pass
            return {"draft_id": getattr(d, "draft_id", "")}
        return {"draft_id": ""}
    return draft


def _real_delete_fn():
    """Best-effort delete of a just-published post for the 5-minute undo (LASSO only).
    Meta's API can delete a FB Page post and some IG media; when it cannot, the caller
    reports that it must be removed by hand."""
    def delete(account_key, media_ids):
        if not config.publish_enabled():
            return False
        try:
            from . import meta_publisher
            fn = getattr(meta_publisher, "delete_media", None)
            if fn is None:
                return False
            return all(fn(account_key, mid) for mid in media_ids)
        except Exception:
            return False
    return delete


def handle_message(text, actor_slack_id, store=None, poster=None, now=None):
    """Entry point the Slack listener calls for a free-text message. Resolves the
    target account + the asset (most recent pending draft), builds the real
    functions, and routes. Returns an Outcome. Inert unless the flag is on."""
    if not config.chat_publish_enabled():
        return Outcome("disabled", "Chat publishing is off.")
    intent = classify_intent(text)
    if intent in (GENERATE, NONE):
        return Outcome("not_a_command", "")
    account_key = resolve_account_key(text)
    draft = _latest_pending(store, account_key) if store is not None else None
    return route(text, account_key, actor_slack_id, asset=draft,
                 publish_fn=_real_publish_fn(),
                 draft_fn=_real_draft_fn(store, poster),
                 gate_fn=_real_gate_fn(store),
                 delete_fn=_real_delete_fn(), now=now)
