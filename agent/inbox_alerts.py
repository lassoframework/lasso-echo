"""inbox_alerts.py — reply-needed coach alerts (flag AGENT_INBOX_ALERTS, default OFF).

Daily READ-ONLY sweep per gym (all client gyms + lasso): pull unhandled inbound
engagement from Zernio — post comments (GET /v1/inbox/comments + the per-post
thread), mentions (GET /v1/inbox/mentions), and reviews (GET /v1/inbox/reviews,
Facebook + Google Business aggregated) — classify each item, and post AT MOST
one Slack card per gym per day when actionable items exist.

HARD RULES:
- READ ONLY. This module NEVER replies, hides, deletes, or likes anything.
  It only tells a human coach what is waiting. The only ZernioClient methods
  it may call are the GET-only inbox/demographics reads.
- ONE card per gym per day MAX, deduped via kv stamp `inbox_alert_<gym>_<date>`.
  The stamp is written only after a card is actually sent, so a morning with
  nothing actionable does not eat the day's card.
- A card is sent ONLY when there are actionable items (member comments waiting
  for a reply, or spam to hide). Neutral items (emoji-only, friend tags) are
  counted nowhere and never carded.
- A Zernio error on ONE gym never blocks the rest (per-gym isolation), and a
  failure in one SOURCE (say reviews) never drops the gym's other sources.
- No invented data: every carded line carries the real post URL and the real
  comment text (truncated), straight from the API response.

CLASSIFICATION (pure, tested):
  member_comment — genuine text from a real person; a coach should reply.
  spam           — obvious solicitation/link-bait ("hit her up on snap",
                   crypto, onlyfans, "check my page", ...). Spammers dodge
                   filters with Cyrillic/Greek homoglyphs (seen LIVE on
                   topfuel 2026-08-25: "hit hеr uр οn snap" with mixed
                   scripts), so text is homoglyph-normalized first.
  neutral        — emoji-only, @mention-only friend tags, empty. No action.

Everything is injectable (zernio, notifier, kv, now) so the whole path unit
tests without a network call. run(gyms=None) callable standalone.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from . import config
from .zernio import ZernioClient, _parse_iso

# An unanswered comment keeps appearing on the daily card (max one card/day)
# until someone replies or it ages past this window — the nag IS the feature.
# Older than this is ancient history, never carded.
COMMENT_LOOKBACK_DAYS = 7
# Only threads on posts this recent are fetched (one thread call per post).
POST_LOOKBACK_DAYS = 30
# Reviews linger unanswered longer and deserve a longer window.
REVIEW_LOOKBACK_DAYS = 14
MAX_ITEMS_PER_CARD = 5
SNIPPET_LEN = 100


# ---- classification (pure) --------------------------------------------------------

# Common Cyrillic/Greek lookalikes spammers substitute for Latin letters.
_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "і": "i", "о": "o", "р": "p", "с": "c", "у": "y",
    "х": "x", "ѕ": "s", "ӏ": "l", "һ": "h", "ԝ": "w", "ј": "j", "ԁ": "d",
    "ɡ": "g", "ο": "o", "α": "a", "ε": "e", "ι": "i", "υ": "u", "ν": "v",
    "τ": "t", "κ": "k", "ρ": "p", "ϲ": "c", "ѡ": "w", "β": "b",
})

SPAM_PATTERNS = (
    "hit her up", "hit him up", "hit me up",
    "on snap", "snapchat", "add her on", "add him on",
    "onlyfans", "only fans", "0nlyfans",
    "crypto", "bitcoin", "forex", "investment plan", "trading account",
    "check my page", "check out my page", "check my profile",
    "check out my profile", "check my bio", "link in my bio",
    "follow my page", "follow me for",
    "dm me for", "dm for promo", "promote it on",
    "telegram", "whatsapp me", "cashapp", "cash app",
    "make money", "earn daily", "earn from home", "passive income",
    "click the link", "click my link", "free followers",
)


def _normalize(text):
    """Lowercased, homoglyph-flattened, NFKC-normalized text for matching."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    return t.translate(_HOMOGLYPHS).lower()


_WORD_RE = re.compile(r"[a-z0-9]")


def classify(text):
    """member_comment | spam | neutral for one comment/mention text."""
    norm = _normalize(text)
    for pattern in SPAM_PATTERNS:
        if pattern in norm:
            return "spam"
    # Neutral: emoji-only / no letters at all.
    if not _WORD_RE.search(norm):
        return "neutral"
    # Neutral: pure friend tags ("@handle", "@a @b", "@handle 👀") — every
    # WORD token is a mention; emoji-only tokens around the tag don't make it
    # a sentence.
    tokens = [t for t in norm.split() if _WORD_RE.search(t)]
    if tokens and all(t.startswith("@") for t in tokens):
        return "neutral"
    return "member_comment"


def needs_reply(comment):
    """True when a comment is a real inbound item still waiting on the gym:
    not the gym's own comment, not hidden, and no owner reply in its thread."""
    c = comment or {}
    if (c.get("from") or {}).get("isOwner"):
        return False
    if c.get("isHidden"):
        return False
    for reply in c.get("replies") or []:
        if (reply.get("from") or {}).get("isOwner"):
            return False
    return True


def _age_days(ts, now):
    parsed = _parse_iso(ts) if isinstance(ts, str) else ts
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 86400.0


def _snippet(text):
    t = " ".join(str(text or "").split())
    return t[:SNIPPET_LEN]


# ---- the per-gym sweep (injectable zernio; read only) -----------------------------


def _comment_items(gym_id, zernio, profile_id, now):
    """Unhandled comment items on the gym's recent posts. One thread call per
    commented post; a failed thread fetch skips THAT post only."""
    items = []
    listing = zernio.list_inbox_comments(profile_id) or {}
    for post in listing.get("data") or []:
        if not isinstance(post, dict):
            continue
        if not post.get("commentCount"):
            continue
        post_age = _age_days(post.get("createdTime"), now)
        if post_age is None or post_age > POST_LOOKBACK_DAYS:
            continue
        try:
            thread = zernio.inbox_post_comments(
                post.get("id"), post.get("accountId")) or {}
        except Exception:
            continue  # one bad thread never drops the gym's other posts
        for c in thread.get("comments") or []:
            if not isinstance(c, dict) or not needs_reply(c):
                continue
            age = _age_days(c.get("createdTime"), now)
            if age is None or age > COMMENT_LOOKBACK_DAYS:
                continue
            kind = classify(c.get("message"))
            if kind == "neutral":
                continue
            items.append({
                "kind": kind, "source": "comment",
                "text": _snippet(c.get("message")),
                "url": c.get("url") or post.get("permalink") or "",
                "age_days": age,
            })
    return items


def _mention_items(gym_id, zernio, profile_id, now):
    items = []
    listing = zernio.list_inbox_mentions(profile_id) or {}
    for m in listing.get("data") or []:
        if not isinstance(m, dict):
            continue
        age = _age_days(m.get("createdTime") or m.get("publishedAt"), now)
        if age is None or age > COMMENT_LOOKBACK_DAYS:
            continue
        text = m.get("content") or m.get("text") or m.get("message") or ""
        kind = classify(text)
        if kind == "neutral":
            continue
        items.append({
            "kind": kind, "source": "mention",
            "text": _snippet(text),
            "url": m.get("permalink") or m.get("url") or "",
            "age_days": age,
        })
    return items


def _review_items(gym_id, zernio, profile_id, now):
    """Recent reviews with NO reply yet. hasReply is the platform's own flag —
    never guessed."""
    items = []
    listing = zernio.list_inbox_reviews(profile_id) or {}
    for r in listing.get("data") or []:
        if not isinstance(r, dict) or r.get("hasReply"):
            continue
        age = _age_days(r.get("created"), now)
        if age is None or age > REVIEW_LOOKBACK_DAYS:
            continue
        items.append({
            "kind": "member_comment", "source": "review",
            "text": _snippet(r.get("text")),
            "url": r.get("reviewUrl") or "",
            "age_days": age,
        })
    return items


def sweep_gym(gym_id, zernio, now):
    """All actionable items for one gym. Per-SOURCE isolation: a failing
    source is reported in `errors` and never drops the other sources."""
    profile_id = zernio.find_profile_id(gym_id)
    if not profile_id:
        return {"gym_id": gym_id, "ok": False,
                "reason": "no Zernio profile for gym (reported, not guessed)"}
    items = []
    errors = []
    for name, fn in (("comments", _comment_items),
                     ("mentions", _mention_items),
                     ("reviews", _review_items)):
        try:
            items.extend(fn(gym_id, zernio, profile_id, now))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}")
    return {"gym_id": gym_id, "ok": True, "items": items, "errors": errors}


# ---- the card (pure) ---------------------------------------------------------------


def build_card(gym_id, items):
    """The one Slack card, or None when nothing is actionable. Capped at
    MAX_ITEMS_PER_CARD lines; every line carries the real URL + real text."""
    member = [i for i in items or [] if i["kind"] == "member_comment"]
    spam = [i for i in items or [] if i["kind"] == "spam"]
    if not member and not spam:
        return None
    parts = [f"REPLY NEEDED at {gym_id}:"]
    if member:
        oldest = max(int(i.get("age_days") or 0) for i in member)
        parts.append(f"{len(member)} member comment(s) waiting "
                     f"(oldest {oldest} day(s))")
    if spam:
        parts.append(("and " if member else "")
                     + f"{len(spam)} spam comment(s) to hide")
    lines = [" ".join(parts)]
    shown = sorted(member, key=lambda i: -(i.get("age_days") or 0)) \
        + sorted(spam, key=lambda i: -(i.get("age_days") or 0))
    for i, item in enumerate(shown[:MAX_ITEMS_PER_CARD], start=1):
        tag = "SPAM" if item["kind"] == "spam" else item["source"].upper()
        lines.append(f"{i}. [{tag}] \"{item['text']}\" {item['url']}".rstrip())
    overflow = len(shown) - MAX_ITEMS_PER_CARD
    if overflow > 0:
        lines.append(f"...and {overflow} more. Check the inbox.")
    return "\n".join(lines)


# ---- notifier (the coach-channel pattern; injectable) ------------------------------


def _coach_channel(gym_id):
    """The gym's own coach/approval Slack channel from the account registry
    (accounts.slack_channel is the per-client channel, set by hand), or None."""
    try:
        from .accounts import all_accounts
        for a in all_accounts():
            k = a.key or ""
            base = k
            for suf in ("_ig", "_fb"):
                if base.endswith(suf):
                    base = base[: -len(suf)]
                    break
            if base == gym_id and getattr(a, "slack_channel", ""):
                return a.slack_channel
    except Exception:
        pass
    return None


def _default_notifier(gym_id, text):
    """Coach channel when the gym has one; ops channel fallback (the
    monthly_retro digest pattern). A failed post is logged, never raised."""
    try:
        ch = None if gym_id == "lasso" else _coach_channel(gym_id)
        if ch:
            from .slack_surface import SlackPoster
            SlackPoster(channel=ch).post_notice(text)
        else:
            from . import ops_alerts
            ops_alerts.alert(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[inbox-alerts] card post failed for {gym_id}: "
              f"{type(exc).__name__}")


def _default_gyms():
    try:
        from .calendar_autopublish import client_gym_bases
        gyms = list(client_gym_bases() or [])
        if "lasso" not in gyms:
            gyms = ["lasso"] + gyms
        return gyms
    except Exception:
        return ["lasso"]


# ---- run ---------------------------------------------------------------------------


def run(gyms=None, zernio=None, now=None, notifier=None, kv_get=None, kv_set=None):
    """The daily sweep. Behind AGENT_INBOX_ALERTS (default OFF -> no-op, no
    client constructed, no network touched). Per gym: sweep, build the card,
    send AT MOST one per day (kv stamp inbox_alert_<gym>_<date>, written only
    after a successful send). A gym's failure is reported and never blocks
    the rest."""
    if not config.inbox_alerts_enabled():
        return {"ok": False, "reason": "AGENT_INBOX_ALERTS is OFF (default). "
                                       "No sweep performed.", "gyms": []}
    now = now or datetime.now(timezone.utc)
    zernio = zernio or ZernioClient()
    notifier = notifier if notifier is not None else _default_notifier
    if kv_get is None or kv_set is None:
        from . import db as _db
        kv_get = kv_get or _db.kv_get
        kv_set = kv_set or _db.kv_set
    gyms = list(gyms) if gyms else _default_gyms()
    day = now.date().isoformat()

    results = []
    cards_sent = 0
    for gym_id in gyms:
        stamp = f"inbox_alert_{gym_id}_{day}"
        try:
            if kv_get(stamp):
                results.append({"gym_id": gym_id, "ok": True,
                                "skipped": "card already sent today"})
                continue
            summary = sweep_gym(gym_id, zernio, now)
            if not summary.get("ok"):
                results.append(summary)
                continue
            card = build_card(gym_id, summary.get("items") or [])
            if card:
                notifier(gym_id, card)
                kv_set(stamp, now.isoformat())
                cards_sent += 1
                summary["card_sent"] = True
            else:
                summary["card_sent"] = False
            summary.pop("items", None)
            results.append(summary)
        except Exception as exc:  # noqa: BLE001
            results.append({"gym_id": gym_id, "ok": False,
                            "reason": f"sweep failed: {type(exc).__name__}"})
    return {"ok": True, "cards_sent": cards_sent, "gyms": results}
