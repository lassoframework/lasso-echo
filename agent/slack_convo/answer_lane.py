"""
answer_lane.py — answer a question from LIVE account state, or decline to answer at all.

Two rails, both absolute:

  1. GROUNDED ONLY. The model is handed a snapshot of facts fetched from the real seams
     (connection status, calendar counts, the ticket's own thread) and instructed to answer
     from those facts and nothing else. If the facts do not contain the answer, it says so
     in one line and the adapter escalates to a human. The snapshot is stored on the ticket
     as verification_before/after -- what was true when we said it -- which is exactly the
     gate the outbox requires before posting anything substantive.

  2. BILLING IS NEVER ANSWERED. Price, cost, charge, invoice, refund, subscription, card,
     Stripe: refused BEFORE any model call, returned as "escalate". The Alex / CrossFit
     Chateau case (2026-09-03) is the reason -- a client was told $99 in Slack while the
     dashboard showed $149; only Blake reconciles that. Org rule: never touch a client's
     billing without explicit approval, and answering about it is touching it.

Everything is injected (fetch_state, llm) so this is offline-testable with no model.
"""
import json
import re

_BILLING_RE = re.compile(
    r"\b(price|prices|pricing|cost|costs|charge|charged|charges|bill|billing|billed|"
    r"invoice|refund|refunds|subscription|subscribe|cancel my|payment|pay for|paid|"
    r"stripe|credit card|card on file|\$\s?\d)\b", re.IGNORECASE)

_SYSTEM = """You are {bot}, replying to {who} in a Slack conversation on behalf of LASSO.

You may ONLY use the FACTS block below. Every sentence you write must be supported by a
fact in it. If the facts do not answer the question, reply with exactly one sentence
saying you do not have that in front of you and a person will follow up. Do not guess.
Do not speculate about causes. Do not promise timing. Never say "should be fixed" or
"should work"; either it is verified true in the facts or you do not claim it.

Never discuss price, billing, charges, refunds, or subscriptions, even if asked.

Voice: plain, direct, warm, short. Lead with the answer. No em dashes, no en dashes, no
hyphens anywhere in the reply. No bullet points. Two to five sentences at most.
{voice}"""


def is_billing(text):
    return bool(_BILLING_RE.search(text or ""))


def default_fetch_state(ticket, who):
    """Live facts for a CLIENT's account from the repo's real seams. Best effort per
    seam: a failed read is recorded as unknown, never invented. Staff/coach questions get
    only the thread (they are asking about the system, not their own account)."""
    facts = {"identity_kind": who.kind, "account_key": who.account_key or None}
    if not who.account_key:
        return facts
    try:
        from .. import zernio_routes as _zr
        status, body = _zr.handle_social_status(who.account_key)
        facts["social_status"] = body if status == 200 else {"unavailable": status}
    except Exception as e:  # noqa: BLE001
        facts["social_status"] = {"unavailable": type(e).__name__}
    try:
        from datetime import date
        from ..portal_calendar_store import SupabaseCalendarStore
        st = SupabaseCalendarStore()
        rows = st.list_month(who.account_key, date.today().strftime("%Y-%m")) or []
        by = {}
        for r in rows:
            by[r.get("status") or "?"] = by.get(r.get("status") or "?", 0) + 1
        facts["calendar_this_month"] = by
    except Exception as e:  # noqa: BLE001
        facts["calendar_this_month"] = {"unavailable": type(e).__name__}
    return facts


def default_llm(system, user, *, model=None):
    import os
    from .. import config
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(model=model or config.slack_convo_model(), max_tokens=400,
                                  system=system, messages=[{"role": "user", "content": user}])
    parts = [getattr(b, "text", "") for b in (resp.content or [])]
    return "".join(parts).strip()


def _voice_rules(identity):
    try:
        import os
        from .. import config as _c
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, identity.reply_voice_doc)
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()[:4000]
    except Exception:  # noqa: BLE001 - a missing voice doc means default voice only
        return ""


def _no_dashes(text):
    # Hard rule for anything a client reads. Replace, never let one through.
    return (text or "").replace("—", ",").replace("–", ",").replace(" - ", ", ")


def answer(ticket, who, messages, *, identity, fetch_state=None, llm=None):
    """Return {'body': str, 'grounding': dict} or None (escalate). Never raises."""
    thread = [m for m in (messages or []) if m.get("direction") == "inbound"]
    question = (thread[-1]["body"] if thread else (ticket.get("raw_text") or "")).strip()
    if not question:
        return None
    if is_billing(question):
        return None   # refused before any model call; the adapter escalates
    try:
        facts = (fetch_state or default_fetch_state)(ticket, who)
    except Exception as e:  # noqa: BLE001
        facts = {"unavailable": type(e).__name__}
    grounding = {"question": question[:500], "facts": facts,
                 "thread_len": len(thread), "bot": identity.name}
    system = _SYSTEM.format(bot=identity.name.capitalize(),
                            who=who.kind, voice=_voice_rules(identity))
    user = ("FACTS:\n" + json.dumps(facts, default=str, indent=1)[:6000] +
            "\n\nCONVERSATION SO FAR (most recent last):\n" +
            "\n".join(f"- {m.get('author_type')}: {str(m.get('body') or '')[:300]}"
                      for m in (messages or [])[-8:]) +
            f"\n\nQUESTION: {question}")
    try:
        body = (llm or default_llm)(system, user)
    except Exception:  # noqa: BLE001 - the adapter escalates on None
        return None
    body = _no_dashes(body).strip()
    if not body or is_billing(body):
        return None
    return {"body": body, "grounding": grounding}
