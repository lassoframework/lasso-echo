"""
classifier.py — what kind of message is this? question, code fix, action request,
follow-up, or "a human should look".

Blake (spec item 4): "classify the message (question, code fix, action request, follow-up on
an open ticket). Question goes to the answer lane. Code fix goes to the FIXER worker for that
product with the same before/after verification gate. Action request on a Ranger identity
goes to the Ranger lane. Follow-up in an open thread attaches to that ticket and re-triggers
the worker with the new message as an instruction."

Deterministic FIRST, the same philosophy as ops_triage.py: every branch below is a plain,
testable rule, and the default when nothing matches is ESCALATE -- a human looks -- never a
guess that dispatches a worker or answers a client. An optional LLM classifier can be
injected for the ambiguous middle, but it is only ever consulted after the rules, and its
answer must still be one of the fixed labels or it is discarded.

Order matters:
  1. an OPEN ticket already owns this conversation  -> follow_up (attach + re-trigger)
  2. a Ranger identity + an ad-action verb           -> action_request (Ranger lane)
  3. breakage signals                                -> code_fix
  4. a question                                      -> answerable_question
  5. an LLM verdict, if one is injected and confident
  6. otherwise                                       -> escalate (None)
"""
import re

QUESTION = "answerable_question"
CODE_FIX = "code_fix"
ACTION_REQUEST = "action_request"
FOLLOW_UP = "follow_up"
ESCALATE = None

# "still broken", "not working", "error", "failed", "can't connect" ... a report that
# something is wrong. Word-bounded so "error" does not fire inside "terror" etc.
_BREAKAGE_RE = re.compile(
    r"\b(broken|not working|isn't working|isnt working|doesn't work|doesnt work|won't|"
    r"wont|can't|cant|cannot|error|errors|erroring|failed|failing|fails|crash|crashed|"
    r"stuck|still (?:not|broken|failing|down)|bug|glitch|won't load|not loading|"
    r"didn't (?:post|publish|go out|send)|didnt (?:post|publish|go out|send)|"
    r"never (?:posted|published|went out)|404|500)\b", re.IGNORECASE)

_QUESTION_RE = re.compile(
    r"(\?\s*$)|^\s*(how|what|when|where|why|who|which|can you|could you|do you|does|is it|"
    r"are you|will|should i|did)\b", re.IGNORECASE)

# Ranger only: a request to DO something to ads. Kept narrow on purpose.
_ACTION_RE = re.compile(
    r"\b(pause|resume|unpause|turn (?:off|on)|scale|increase|decrease|raise|lower|"
    r"budget|spend|launch|relaunch|target(?:ing)?|audience|duplicate|kill|stop the ad|"
    r"start the ad)\b", re.IGNORECASE)

# Ranger request_type vocabulary (migration 0303), best effort from the text.
_REQUEST_TYPE_RULES = (
    ("pause_resume", re.compile(r"\b(pause|resume|unpause|turn (?:off|on)|stop|start)\b", re.I)),
    ("budget",       re.compile(r"\b(budget|spend|scale|increase|decrease|raise|lower)\b", re.I)),
    ("launch",       re.compile(r"\b(launch|relaunch|go live|new campaign)\b", re.I)),
    ("targeting",    re.compile(r"\b(target(?:ing)?|audience|geo|radius|age)\b", re.I)),
)

_VALID = frozenset({QUESTION, CODE_FIX, ACTION_REQUEST, FOLLOW_UP})

# RT-M2: a breakage word alone is a hair trigger ("I can't make Thursday", "my bad, my
# error"). A code fix needs the breakage to be ABOUT something we run. Word-bounded.
_DOMAIN_RE = re.compile(
    r"\b(post|posts|posting|posted|publish|published|publishing|story|stories|reel|reels|"
    r"caption|captions|calendar|schedule|scheduled|instagram|ig|facebook|fb|page|google|"
    r"gbp|business profile|connect|connection|connected|connecting|link|upload|uploads|"
    r"photo|photos|video|videos|media|approve|approval|approvals|portal|login|log in|"
    r"sign in|echo|dashboard|reply|replies|comment|comments|drive|folder)\b", re.IGNORECASE)

# V-m4: greetings, thanks, acknowledgements. Never a ticket, never a page.
_CHATTER_RE = re.compile(
    r"^\s*(hey|hi|hello|yo|thanks|thank you|thx|ty|ok|okay|k|got it|sounds good|great|"
    r"perfect|awesome|cool|nice|will do|on it|done|yep|yes|no|nope|sure|np|no problem|"
    r"lol|haha|👍|🙏|✅)[\s!.,]*(\S+[\s!.,]*){0,3}$", re.IGNORECASE)


def is_chatter(text):
    """A greeting / thanks / one-word acknowledgement, up to a few trailing words."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 60 and bool(_CHATTER_RE.match(t))


def request_type_for(text):
    """Ranger request_type for an action request, or 'other'."""
    t = text or ""
    for label, rx in _REQUEST_TYPE_RULES:
        if rx.search(t):
            return label
    return "other"


def classify(text, *, has_open_ticket, identity_product, llm=None):
    """One label from the fixed set, or None (escalate). Never raises.

    llm(text) -> one of the labels, or anything else (ignored). Only consulted when the
    rules do not decide; a wrong label from it cannot widen the set."""
    t = (text or "").strip()
    if not t:
        return ESCALATE
    if has_open_ticket:
        return FOLLOW_UP
    if identity_product == "ranger" and _ACTION_RE.search(t):
        return ACTION_REQUEST
    # RT-M2: breakage AND an Echo-domain noun. Breakage alone escalates to a human.
    if _BREAKAGE_RE.search(t) and _DOMAIN_RE.search(t):
        return CODE_FIX
    if _QUESTION_RE.search(t):
        return QUESTION
    if llm is not None:
        try:
            verdict = llm(t)
        except Exception:  # noqa: BLE001 - a model fault escalates, never dispatches
            return ESCALATE
        if verdict in _VALID and verdict != FOLLOW_UP:
            return verdict
    return ESCALATE
