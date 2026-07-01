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


def classify_comment(text):
    """Return 'TIER1' or 'TIER2'. Any question, or any Tier 2 keyword, is TIER2."""
    low = (text or "").lower()
    if _is_question(low):
        return "TIER2"
    if any(kw in low for kw in _TIER2_KEYWORDS):
        return "TIER2"
    return "TIER1"


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
