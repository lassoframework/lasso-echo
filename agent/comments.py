"""
Comment and DM handling. NOTHING HERE EVER AUTO-SENDS.

Policy (matches the brand gates):
  - Tier 1 (simple positive / neutral): the agent may like the comment and draft an
    APPROVED thank-you template, but it is HELD for human approval, never sent.
  - Tier 2 (price, hours, injury, refund, negative, or ANY question): surfaced to a
    human, who writes the reply. The agent drafts nothing sensitive.
  - DMs: a first contact is NEVER auto-handled — it is always surfaced to a human.

Every returned result carries auto_send=False. The AGENT_COMMENTS_ENABLED flag is OFF
by default; while off, handlers hold and do nothing.
"""

from . import config

# The default approved thank-you. It is HELD for human approval before it can ever be
# sent (auto_send is always False), so this is a safe editable default, not auto-copy.
THANK_YOU_TEMPLATE = "Thanks so much, we appreciate you."

# Tier 2 triggers: anything with money, hours, health/injury, refunds, negativity, or a
# question goes to a human. Substring match, case-insensitive.
_TIER2_KEYWORDS = (
    # price
    "price", "cost", "how much", "pricing", "expensive", "fee", "rate", "$", "dollar",
    # hours
    "hour", "open", "close", "what time", "when are you", "schedule",
    # injury / health
    "injur", "hurt", "pain", "knee", "shoulder", "recover", "rehab", "physio",
    # refund / billing
    "refund", "cancel", "money back", "charge", "billed", "chargeback", "dispute",
    # negative sentiment
    "scam", "terrible", "worst", "hate", "awful", "disappoint", "rude", "ripoff",
    "rip off", "waste", "sucks", "horrible", "garbage", "fraud", "bad",
)

_QUESTION_STARTS = (
    "how", "what", "when", "where", "why", "who", "which",
    "do you", "can i", "could you", "are you", "is there", "does", "will you",
)


def _is_question(low):
    return "?" in low or low.strip().startswith(_QUESTION_STARTS)


# CLEARLY positive markers: only these earn Tier 1. Conservative by design:
# anything uncertain is Tier 2 and goes to a human.
_TIER1_POSITIVE = (
    "thank", "love", "great", "awesome", "amazing", "congrats", "so good", "inspir",
    "let's go", "lets go", "fire", "beautiful", "nice", "proud", "yes!",
    "\U0001f525", "\u2764", "\U0001f4aa", "\U0001f44f", "\U0001f44d",
)


def classify_comment(text):
    """Return 'TIER1' or 'TIER2'. CONSERVATIVE: any question, any Tier 2 keyword,
    and anything not CLEARLY positive is TIER2. Only unmistakable positivity with
    zero triggers earns the Tier 1 path."""
    low = (text or "").lower()
    if _is_question(low):
        return "TIER2"
    if any(kw in low for kw in _TIER2_KEYWORDS):
        return "TIER2"
    if any(mark in low for mark in _TIER1_POSITIVE):
        return "TIER1"
    return "TIER2"  # uncertain = a human looks at it


def handle_comment(text):
    """
    Classify and decide the (held) action. Returns
    {tier, action, draft_reply, auto_send=False}. NEVER sends.
    """
    tier = classify_comment(text)
    if not config.comments_enabled():
        return {"tier": tier, "action": "held (comment handling disabled)",
                "draft_reply": "", "auto_send": False}
    if tier == "TIER1":
        return {"tier": tier,
                "action": "like + approved thank-you, held for approval",
                "draft_reply": THANK_YOU_TEMPLATE, "auto_send": False}
    return {"tier": tier,
            "action": "surface to human; human writes the reply",
            "draft_reply": "", "auto_send": False}


def handle_dm(text, first_contact=True):
    """
    DMs are never auto-handled. A first contact is always surfaced to a human. Returns
    {first_contact, action, auto_send=False}. NEVER sends.
    """
    result = {"first_contact": bool(first_contact), "auto_send": False}
    if not config.comments_enabled():
        result["action"] = "held (comment handling disabled)"
        return result
    result["action"] = ("surface to human (first contact, never auto-handled)"
                        if first_contact else "surface to human")
    return result


def fetch_recent_comments(account, http=None, limit_posts=5):
    """READ-ONLY Graph pull: comments on this account's recent published posts
    (media ids from the store). Returns [{media_id, comment_id, text}]. DMs are
    NEVER read here or anywhere: no direct message endpoint of any kind exists in this module.
    Nothing while AGENT_COMMENTS_ENABLED is OFF."""
    if not config.comments_enabled():
        return []
    token = account.get_token()
    if not token:
        return []
    from . import db
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT media_id FROM posts WHERE account_key=? AND mode='published' "
            "AND media_id != '' ORDER BY published_at DESC LIMIT ?",
            (account.key, limit_posts)).fetchall()
    if http is None:
        import requests  # lazy
        http = requests
    out = []
    for row in rows:
        media_id = row["media_id"]
        try:
            r = http.get(f"{config.GRAPH_API_BASE}/{media_id}/comments",
                         params={"fields": "id,text,message",
                                 "access_token": token},
                         timeout=30)
            for c in (r.json() or {}).get("data", []) or []:
                out.append({"media_id": media_id, "comment_id": c.get("id", ""),
                            "text": c.get("text") or c.get("message") or ""})
        except Exception as e:
            print(f"[comments] read failed for {media_id}: {type(e).__name__}: {e}")
    return out


def process_comments(account, http=None, poster=None):
    """
    The held-card pass: classify each new comment, queue the Tier 1 like +
    templated thank-you or the Tier 2 draft, and post ONE Slack card per comment.
    EVERYTHING IS HELD: nothing likes, replies, or sends without a human. A seen
    marker in the store keeps re-polls quiet. None while the flag is OFF.
    """
    if not config.comments_enabled():
        return None
    from . import db
    cards = []
    for c in fetch_recent_comments(account, http=http):
        if not c["comment_id"]:
            continue
        seen_key = f"comment_seen_{c['comment_id']}"
        if db.kv_get(seen_key):
            continue
        db.kv_set(seen_key, "1")
        decision = handle_comment(c["text"])
        assert decision["auto_send"] is False  # structural: nothing ever auto-sends
        if decision["tier"] == "TIER1":
            card = (f"COMMENT TIER 1 HELD for {account.key}: {c['text'][:120]!r}\n"
                    f"Queued (needs your approval): like + reply "
                    f"{decision['draft_reply']!r}. Nothing sent.")
        else:
            card = (f"COMMENT TIER 2 HELD for {account.key}: {c['text'][:120]!r}\n"
                    "Needs a human reply. Nothing sent, nothing drafted beyond "
                    "this card.")
        cards.append(card)
        if poster is not None:
            poster.post_notice(card)
        db.audit("comment", c["comment_id"],
                 f"{decision['tier']} held for approval", account.key)
    return cards
