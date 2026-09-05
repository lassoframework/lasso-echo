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
    r"never (?:posted|published|went out)|404|500|"
    # RTF-1 (2026-09-05): the live gap. Every phrasing below was a real client sentence
    # shape that the list above missed, so it fell past CODE_FIX to ESCALATE with "the
    # classifier did not decide". Present-progressive negation ("are not going out") and
    # the "stopped / no longer / wrong" family were simply absent. Widening is safe here
    # because CODE_FIX still requires a _DOMAIN_RE noun in the same message (RT-M2): a bare
    # "I am not going to make it" has no Echo-domain noun and still escalates.
    r"(?:are|is|isn't|isnt|aren't|arent|has|hasn't|hasnt|have|haven't|havent|was|were|"
    r"still)?\s*not (?:going out|posting|publishing|showing|showing up|appearing|"
    r"updating|syncing|loading|connecting|sending|working)|"
    r"stopped (?:working|posting|publishing|showing|syncing|updating|going out|sending)|"
    r"no longer (?:working|posts|posting|publishing|showing|syncing|updating)|"
    r"(?:showing|shows|showing up with|displaying|displays) the wrong|"
    r"(?:wrong|incorrect|out of date|outdated) (?:hours|address|phone|number|info|"
    r"information|schedule|times|link|price)|"
    r"nothing (?:posted|published|went out|happened|shows|showed up)|"
    r"(?:still|yet) nothing|"
    r"keeps? (?:failing|erroring|crashing|logging me out)|"
    r"(?:has|have|had)(?:n't|nt)? (?:posted|published|gone out|updated|synced|recreated|"
    r"regenerated|shown up|come through)|"
    r"nothing (?:was |has been |ever |got )?(?:recreated|regenerated|generated|created|"
    r"posted|published|sent|updated|synced)|"
    r"won't (?:post|publish|go out|send|load|connect|update)|"
    r"wont (?:post|publish|go out|send|load|connect|update))", re.IGNORECASE)

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
    r"sign in|echo|dashboard|reply|replies|comment|comments|drive|folder|"
    # RTF-1: the website product's nouns, which were missing entirely -- every
    # Wrangler-shaped breakage report ("the website is showing the wrong hours") failed
    # RT-M2's domain check and escalated. Bare "site" is deliberately NOT here: the existing
    # RT-M2 guard case "the site crashed my brain lol" is exactly the figurative use that
    # word invites, and every real report of ours says website / homepage / page, or names
    # the thing that is wrong (hours, address, form).
    r"website|websites|web site|homepage|home page|landing page|web page|webpage|"
    r"url|domain|form|forms|booking|book now|hours|address)\b", re.IGNORECASE)

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


# ---- cross-product routing (D50, 2026-09-05) -------------------------------------------
# Blake: "a website question should reach the identity that can actually answer it,
# regardless of entry point, WHEN the classifier is confident about the content; low
# confidence stays with the entry-point agent."
#
# This decides WHICH BOT'S KNOWLEDGE AND VOICE drafts the answer. It never changes the
# ticket's channel, its client_id/gym, its bot_identity, or who the reply is delivered to --
# see adapter._answer_product / outbox delivery, which are untouched by this. A website
# question about Gym A is still answered in Gym A's own conversation by Gym A's own ticket;
# only the product knowledge used to draft it moves. That containment is the whole Frame 2
# safety argument, and tests assert it directly.
_WEBSITE_RE = re.compile(
    r"\b(website|web site|websites|homepage|home page|landing page|web page|webpage|"
    r"our site|my site|the site|your site|site's|sites)\b", re.IGNORECASE)
# Terms that mean the message is really about the OTHER products. Any of these present and
# the website signal is no longer unambiguous, so confidence drops and routing does not fire.
_NOT_WEBSITE_RE = re.compile(
    r"\b(instagram|ig|facebook|fb|reel|reels|story|stories|caption|captions|post|posts|"
    r"posting|publish|published|calendar|ad|ads|adset|ad set|campaign|budget|spend|"
    r"targeting|audience|cpl|lead|leads)\b", re.IGNORECASE)

CONFIDENT = "confident"
UNSURE = "unsure"


def product_hint(text):
    """(product, confidence) for cross-product routing, or (None, UNSURE).

    Deterministic and deliberately narrow: an unmistakable website noun with no competing
    product noun in the same message is CONFIDENT; anything else is UNSURE, which the
    adapter treats as "stay with the entry-point identity", the unchanged behaviour."""
    t = (text or "").strip()
    if not t:
        return None, UNSURE
    if _WEBSITE_RE.search(t) and not _NOT_WEBSITE_RE.search(t):
        return "websites", CONFIDENT
    return None, UNSURE


# ---- the LLM fallback, wired for real (D51, 2026-09-05) ---------------------------------

_LLM_SYSTEM = """You label one inbound support message for a LASSO support bot. Reply with
EXACTLY ONE of these tokens and nothing else:

answerable_question  - the person is asking something that could be answered from their own
                       account state (is X connected, what is scheduled, what happened to Y).
code_fix             - the person is reporting that something we run is broken or not doing
                       what it should.
action_request       - the person is asking us to CHANGE something on their ads.
UNSURE               - anything else, or you are not confident. Choose this freely; a wrong
                       label sends a client a wrong answer, UNSURE only asks a human to look.

Never explain. Never output any other text."""


def default_classify_llm(model=None):
    """A real LLM classifier for the ambiguous middle, or None when no key is configured.

    THE BUG THIS CLOSES (found live 2026-09-05): listener_wiring.live_deps() hardcoded
    `classify_llm=None`, so in production classify() could only ever reach the deterministic
    rules -- config.slack_convo_model()'s own docstring has promised "the LLM fallback of the
    classifier" since the day it was written, and nothing was ever wired to it. Every message
    the regexes did not recognise fell to ESCALATE by construction, which is exactly the
    "the classifier did not decide" flood in #fixer.

    Returns a callable (text) -> label | None. It NEVER raises out to the caller: classify()
    already treats an exception as ESCALATE, and this returns None on anything unexpected, so
    the deterministic behaviour is the floor and the model can only ever fill the middle."""
    def _llm(text):
        from . import answer_lane as _al
        raw = _al.default_llm(_LLM_SYSTEM, str(text or "")[:4000], model=model)
        verdict = (raw or "").strip().splitlines()[0].strip() if raw else ""
        return verdict if verdict in _VALID else None
    return _llm


def classify(text, *, has_open_ticket, identity_product, llm=None, brain_hint=None):
    """One label from the fixed set, or None (escalate). Never raises.

    llm(text) -> one of the labels, or anything else (ignored). Only consulted when the
    rules do not decide; a wrong label from it cannot widen the set.

    brain_hint (D40, wiring D34-D38's brain.py in): an optional BrainHint whose
    classification_hints are phrase->label pairs LEARNED from this identity's own resolved
    tickets (brain.py docstring: "shapes classification and reply style only, never
    facts"). Consulted in the SAME deterministic slot as the rule-based checks above --
    before the LLM step, since a phrase match is exact-string matching, not a guess -- and
    filtered through the identical _VALID/no-FOLLOW_UP rule the llm verdict already uses,
    so a brain hint can never mint a label outside the fixed set or force a re-trigger."""
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
    if brain_hint is not None:
        hinted = brain_hint.classification_hint_for(t)
        if hinted in _VALID and hinted != FOLLOW_UP:
            return hinted
    if llm is not None:
        try:
            verdict = llm(t)
        except Exception:  # noqa: BLE001 - a model fault escalates, never dispatches
            return ESCALATE
        if verdict in _VALID and verdict != FOLLOW_UP:
            return verdict
    return ESCALATE
