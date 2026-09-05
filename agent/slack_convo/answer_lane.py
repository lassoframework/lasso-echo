"""
answer_lane.py — answer a question from LIVE account state, or decline to answer at all.

Two rails, both absolute:

  1. GROUNDED ONLY. The model is handed a snapshot of facts fetched from the real seams
     (connection status, calendar counts, the ticket's own conversation) and instructed to
     answer from those facts and nothing else. If the facts do not contain the answer it
     emits the sentinel NO_ANSWER and this returns None -- the adapter escalates to a human.
     The snapshot is stored on the ticket as verification_before/after -- what was true when
     we said it -- which is the gate the outbox requires before posting anything substantive.

  2. BILLING IS NEVER ANSWERED. Price, cost, charge, invoice, refund, subscription, card,
     Stripe: refused BEFORE any model call. The Alex / CrossFit Chateau case (2026-09-03) is
     the reason -- a client was told $99 in Slack while the dashboard showed $149; only Blake
     reconciles that. Org rule: never touch a client's billing without explicit approval, and
     answering about it is touching it.

HARDENING (2026-09-03 audits):
  V-M4   A snapshot in which every seam failed used to count as "grounded" and the model's
         polite "I don't have that in front of me" was treated as an answer, resolving the
         ticket with nobody following up. Now: all-unavailable facts -> None; the model is
         told to emit exactly NO_ANSWER when facts do not cover the question -> None. None
         means the adapter escalates.
  RT-m2  The transcript handed to the model used to include internal rows (hold notices
         quoting drafts, ticket ids). Now only the person's own words and replies that were
         actually POSTED to them.
  V-M10  The question is the message that triggered this call, passed explicitly, not the
         last of a truncated 40-row read.
  V-m9   In-word hyphens ("re-run") are removed too; the voice rule is no hyphens anywhere.

Everything is injected (fetch_state, llm) so this is offline-testable with no model.
"""
import json
import re
import re as _re

NO_ANSWER = "NO_ANSWER"

_BILLING_RE = re.compile(
    r"\b(price|prices|pricing|cost|costs|charge|charged|charges|bill|billing|billed|"
    r"invoice|refund|refunds|subscription|subscribe|cancel my|payment|pay for|paid|"
    r"stripe|credit card|card on file|\$\s?\d)\b", re.IGNORECASE)

_SYSTEM = """You are {bot}, replying to a {who} in a Slack conversation on behalf of LASSO.

You may ONLY use the FACTS block. Every sentence you write must be supported by a fact in
it. If the facts do not answer the question, reply with exactly the single token NO_ANSWER
and nothing else. Do not guess. Do not speculate about causes. Do not promise timing. Never
say "should be fixed" or "should work"; either it is verified true in the facts or you do
not claim it.

Never discuss price, billing, charges, refunds, or subscriptions, even if asked.

Voice: plain, direct, warm, short. Lead with the answer. No em dashes, no en dashes, no
hyphens anywhere in the reply. No bullet points. Two to five sentences at most.
{voice}"""


def is_billing(text):
    return bool(_BILLING_RE.search(text or ""))


def _all_unavailable(facts):
    """True when nothing in the snapshot is a usable fact (every seam failed or empty)."""
    if not isinstance(facts, dict) or not facts:
        return True
    usable = 0
    for k, v in facts.items():
        if k in ("identity_kind", "account_key", "unavailable"):
            continue
        if isinstance(v, dict) and set(v.keys()) == {"unavailable"}:
            continue
        if v in (None, "", {}, []):
            continue
        usable += 1
    return usable == 0


def default_fetch_state(ticket, who):
    """Live facts for a CLIENT's account from the repo's real seams. Best effort per seam: a
    failed read is recorded as unavailable, never invented. Staff/coach questions get only the
    conversation (they are asking about the system, not their own account)."""
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
        facts["calendar_this_month"] = by or {"unavailable": "no rows"}
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
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, identity.reply_voice_doc), "r", encoding="utf-8") as fh:
            doc = fh.read()[:4000]
    except Exception:  # noqa: BLE001 - a missing voice doc means default voice only
        doc = ""
    return doc + _brain_tone_notes(identity.name)


def _brain_tone_notes(agent_name):
    """D40 (wiring D34-D38's brain.py in): tone notes ONLY, appended alongside the
    reply-voice doc, never in place of it. This is the entire surface area brain.py is
    allowed into answer_lane.py through -- `BrainHint.tone_notes` is a tuple of short
    style phrases, structurally incapable of carrying a fact (see brain.py's own
    docstring: the dataclass has no field that could hold one). This function is never
    called anywhere near `facts`, `grounding`, or the FACTS block of the user prompt in
    answer() below -- tests/test_support_brain.py asserts that structural separation
    directly (a poisoned tone_notes entry cannot appear in the facts dict or grounding),
    which is the actual enforcement now, replacing D36's original "no import at all" rule
    (logged as D39 in DECISIONS.md: that blanket rule made wiring style guidance in at all
    impossible, which was never Blake's intent -- "shapes classification and reply style
    only, never facts" always meant a narrower, structural boundary, not zero import)."""
    try:
        from . import brain as _brain
        notes = _brain.load_hint(agent_name).tone_notes
    except Exception:  # noqa: BLE001 - a brain read failure never blocks a reply
        return ""
    if not notes:
        return ""
    return "\n\nAdditional tone notes for this agent:\n" + "\n".join(f"- {n}" for n in notes)


_HYPHEN_IN_WORD = re.compile(r"(?<=\w)-(?=\w)")


def _no_dashes(text):
    """Hard rule for anything a client reads: no em dash, en dash, or hyphen anywhere."""
    t = (text or "").replace("—", ",").replace("–", ",").replace(" - ", ", ")
    return _HYPHEN_IN_WORD.sub(" ", t)


def conversation_for_model(messages):
    """RT-m2: only what the person said and what was actually POSTED to them. Internal rows
    (escalations, hold notices, fixer requests) and unposted drafts never reach the model."""
    out = []
    for m in messages or []:
        d = m.get("direction")
        att = m.get("attachments") or {}
        if d == "inbound":
            out.append(m)
        elif d == "outbound" and m.get("delivery_status") == "posted" and \
                att.get("kind") in ("ack", "answer", "template", "status"):
            out.append(m)
    return out


def answer(ticket, who, messages, question=None, *, identity, fetch_state=None, llm=None,
           speaks_as=None):
    """Return {'body': str, 'grounding': dict} or None (escalate). Never raises.

    `speaks_as` (finding 13, 2026-09-05 audit 3): under D50 cross-product routing the
    KNOWLEDGE and voice doc come from one identity while the message is posted by another --
    so the system prompt used to say "You are Wrangler" on a reply going out of Scout's bot,
    in Scout's DM. The client would see one bot introduce itself as another. The name in the
    prompt is now the bot that will actually speak; everything else about the routing is
    unchanged."""
    convo = conversation_for_model(messages)
    speaker = speaks_as or identity.name
    q = (question or "").strip()
    if not q:
        inbound = [m for m in convo if m.get("direction") == "inbound"]
        q = (inbound[-1]["body"] if inbound else (ticket.get("raw_text") or "")).strip()
    if not q or is_billing(q):
        return None   # refused before any model call; the adapter escalates
    try:
        facts = (fetch_state or default_fetch_state)(ticket, who)
    except Exception as e:  # noqa: BLE001
        facts = {"unavailable": type(e).__name__}
    if _all_unavailable(facts):
        return None   # V-M4: a snapshot of failures is not grounding
    # Audit 5, finding 6: `bot` used to record the identity whose knowledge drafted the
    # answer, while the client was spoken to by a different one -- a false entry in the
    # record this system treats as evidence. It records both, named for what they are.
    grounding = {"question": q[:500], "facts": facts, "thread_len": len(convo),
                 "bot": speaker, "domain_guidance_from": identity.name}
    # Audit 4, finding 5: swapping one token of the system prompt was not enough -- the
    # appended VOICE DOC is the longer and far more specific identity instruction, and under
    # cross-product routing it still named the other bot throughout ("Wrangler is the LASSO
    # team member who builds and maintains gym websites", five times over). The voice a
    # client hears must belong to the bot that is actually speaking, so the voice doc comes
    # from the SPEAKER; what routing moves is the subject matter, stated explicitly.
    voice = _voice_rules(identity)
    if speaks_as and speaks_as != identity.name:
        # Audit 5, finding 6: the audit-4 fix swapped the whole voice doc to the SPEAKER's,
        # which removed the only thing D50 routing actually moves -- the routed product's
        # domain guidance -- leaving a capability that did nothing. The doc that matters is
        # the routed one; what must not survive is the other bot's NAME, because the client
        # is talking to exactly one bot. So the routed guidance is kept and every mention of
        # the routed bot's name is rewritten to the speaker's, which is precisely the
        # substitution a human would make reading it aloud.
        voice = _re.sub(rf"\b{_re.escape(identity.name)}\b", speaker.capitalize(), voice,
                        flags=_re.IGNORECASE)
        voice += (f"\n\nThis question is about the client's {identity.product}. Answer it "
                  f"from the FACTS block, in your own voice as {speaker.capitalize()}, and "
                  f"never introduce yourself as any other name.")
    system = _SYSTEM.format(bot=speaker.capitalize(), who=who.kind, voice=voice)
    user = ("FACTS:\n" + json.dumps(facts, default=str, indent=1)[:6000] +
            "\n\nCONVERSATION SO FAR (most recent last):\n" +
            "\n".join(f"- {m.get('author_type')}: {str(m.get('body') or '')[:300]}"
                      for m in convo[-8:]) +
            f"\n\nQUESTION: {q}")
    try:
        body = (llm or default_llm)(system, user)
    except Exception:  # noqa: BLE001 - the adapter escalates on None
        return None
    body = (body or "").strip()
    if not body or NO_ANSWER in body.upper():
        return None
    body = _no_dashes(body).strip()
    if not body or is_billing(body):
        return None
    return {"body": body, "grounding": grounding}
