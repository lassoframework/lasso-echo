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
CANCEL_POST = "cancel_post"
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

# The words a gym owner types for media that _DOMAIN_RE does not carry. KEPT OUT of
# _DOMAIN_RE on purpose (independent audit round 3, CRITICAL): _DOMAIN_RE also gates
# _BREAKAGE_RE, which is NOT behind the repeat flag, so widening it changed the DEFAULT
# path -- "the image is broken", "the clip is broken", "the footage didn't go out" all
# flipped from ESCALATE to code_fix, and "is the image broken?" flipped from a QUESTION
# to a code_fix. That contradicted every claim that the flag OFF is byte-for-byte the
# old classifier. These nouns now widen the flagged repeat rule ONLY.
#
# Bare "shot" stays out of both, for the same reason bare "site" is out of _DOMAIN_RE:
# "nice shot", "worth a shot", "a shot at the title".
_MEDIA_NOUN_RE = re.compile(
    r"\b(?:image|images|picture|pictures|pic|pics|clip|clips|footage)\b",
    re.IGNORECASE)


# WRONG OUTPUT, not a dead machine (John Weeks / Tough Temple, 2026-09-11).
#
# Every pattern in _BREAKAGE_RE describes something NOT HAPPENING: not posting, not
# going out, broken, error, failed. Echo's most common real client complaint is the
# OPPOSITE shape -- it is running fine and producing the WRONG THING. John's
# "9/14-9/16 are still repeat images" matched nothing above, so it classified ESCALATE
# and the fixer lane never saw it. He reported the same defect twice and a human
# carried the whole ticket both times.
#
# Kept as its OWN regex rather than widened into _BREAKAGE_RE for one reason: unlike a
# dead machine, this family collides with ordinary QUESTIONS ("how often do posts
# repeat?"), and _BREAKAGE_RE is deliberately checked BEFORE _QUESTION_RE. classify()
# therefore checks this one AFTER the question rule, so an owner ASKING about repeats
# still reaches the answer lane. _BREAKAGE_RE's own ordering is untouched.
#
# Still gated by _DOMAIN_RE (RT-M2): "same old same old" and "my duplicate gym keys"
# carry no Echo noun and still escalate.
_REPEAT_RE = re.compile(
    r"\b(?:repeat|repeats|repeated|repeating|duplicate|duplicates|duplicated|"
    r"duplicating|reuse|reuses|reused|reusing|re-used|re-using|recycled|recycling)\b|"
    r"\b(?:same|identical) (?:photo|photos|image|images|picture|pictures|pic|pics|"
    r"video|videos|clip|clips|post|posts|footage|thing)\b|"
    r"\b(?:over and over|again and again|twice in a row|multiple times|"
    r"more than once|(?:\d+|two|three|four|five|several) (?:days|times|weeks) in a row)"
    r"\b", re.IGNORECASE)

# NOT A BUG REPORT, however many repeat words it contains (independent audit round 2,
# 2026-09-11: 14 of 23 realistic benign client sentences dispatched a fixer request).
# A false code_fix is not free -- adapter.py sets the ticket to 'triage', so every later
# message from that owner classifies FOLLOW_UP until a human closes it, and the ACK Echo
# sends reads "I read that as something not working on our side". Telling a gym owner
# their thank-you note is a breakage report is worse than escalating it to a person.
#
# Three families, all of which SAY they are not complaints:
#   1. an instruction or request     -- "please reuse the photo from last Tuesday",
#                                       "can we repeat that promo?", "do not reuse ..."
#   2. the CLIENT is the actor       -- "I duplicated the calendar by accident",
#                                       "I recycled the caption", "we reused the logo"
#   3. approval or thanks            -- "thanks for fixing the duplicate images",
#                                       "same image different caption is fine by me"
# plus "repeat customers/clients/members", which is a business metric, not media.
_NOT_A_REPEAT_REPORT_RE = re.compile(
    # 1. an instruction, a request, or a hypothetical
    r"^\s*(?:please\b|pls\b|can (?:we|you)\b|could (?:we|you)\b|would (?:you|it)\b|"
    r"should (?:we|i)\b|is it (?:ok|okay|weird|fine)\b|let'?s\b|lets\b|do not\b|"
    r"don'?t\b|dont\b|never\b|make sure\b|go ahead\b|feel free\b|if\b|"
    r"it'?s fine\b|it is fine\b|honestly\b)|"
    r"\b(?:just |please |feel free to )(?:recycle|reuse|repeat|duplicate)\b|"
    # a REQUEST or a PREFERENCE, however it is worded: "we need you to reuse the same
    # photo", "we would like the same picture on both posts", "keep the duplicates".
    r"\b(?:need|needs|needed|want|wants|wanted|would like|'?d like|asked for|ask for|"
    r"requested|request|prefer|prefers|expect|expects|hoping|hope) (?:you |us |echo |"
    r"them )?(?:to )?\w*\s*(?:reuse|repeat|duplicate|recycle|keep|use|leave)?\b"
    r"(?=.*\b(?:reuse|reused|repeat|repeats|repeated|duplicate|duplicates|duplicated|"
    r"recycle|recycled|same)\b)|"
    r"\bfine to (?:reuse|repeat|duplicate|recycle|use)\b|"
    r"\bkeep (?:the )?same\b|"
    # gratitude ABOUT a past fix ("thanks for fixing the duplicate images"). Clause
    # scoped, so "thanks for the turnaround, but the repeats are still there" still
    # reports: the complaint lives in its own clause.
    r"\bthank(?:s| you) for\b|"
    # permission and hypotheticals: "the captions can repeat too", "the same post
    # twice would be weird", "that would look odd"
    r"\b(?:can|could|should|may) (?:repeat|reuse|duplicate|recycle)\b|"
    r"\bwould (?:be|look|seem|feel)\b|"
    # 2. a PERSON did it, not Echo -- the client, or someone at the gym. A person who
    #    NOTICED it is reporting, not causing: only causing verbs veto.
    r"\b(?:i|we) (?:just |already |think i |think we |always |usually |keep |kept |"
    r"accidentally |may have |might have |must have |are going to |am going to |"
    r"gonna |will |plan to |want to )*"
    r"(?:duplicated|duplicate|reused|reuse|re-used|recycled|recycle|repeated|repeat|"
    r"copied|copy|uploaded|approved|sent|added|dropped|loaded|pasted|emailed|"
    r"scheduled|imported|put)\b|"
    r"\b(?:my|our) (?:front desk|desk|coach|coaches|manager|assistant|staff|team|gm|"
    r"wife|husband|partner|kid|kids|son|daughter|intern|owner|trainer|trainers|"
    r"photographer|videographer|marketing (?:guy|person|girl|team)|admin|va)"
    r"\s+\w*\s*(?:duplicated|uploaded|reused|recycled|copied|sent|added|keeps)\b|"
    r"\brepeat(?:ing)? (?:myself|ourselves|itself)\b|"
    r"\b(?:accidentally|by accident|by mistake|my bad|oops|sorry about that|"
    r"my fault)\b|"
    # 3. thanks, approval, or "already resolved"
    r"\b(?:appreciate)\b|"
    r"\b(?:resolved|all set|all good|no longer an issue|nice work|looks good|"
    r"looks great|fixed now|sorted|sorted out|sorted it|unrelated)\b|"
    r"\b(?:did not|didn'?t|does not|doesn'?t) bother\b|"
    r"\b(?:no worries|not a problem|not an issue|totally fine|fine (?:with|by) "
    r"(?:us|me)|no big deal|hope that'?s ok|on purpose|intentional|deliberate|"
    r"for continuity)\b|"
    r"\b(?:love|loved|loves|liked|great|perfect|awesome|crushed|working well)\b|"
    r"\b(?:we|i) (?:like|love|prefer|are fine with|am fine with)\b|"
    # 4. a SCHEDULE, a CHARGE, or a business metric that repeats -- not media
    r"\brepeat (?:customer|customers|client|clients|member|members|business|rate)\b|"
    r"\bduplicate (?:charge|charges|payment|payments|invoice|billing|bill|"
    r"subscription|membership)\b|"
    r"\b(?:schedule|class|classes|session|sessions|workout|workouts|programming|"
    r"program|promo|promotion|event|hours) (?:repeats?|duplicates?)\b|"
    r"\brepeats? (?:weekly|daily|monthly|yearly|annually|every (?:year|week|month|day))"
    r"\b",
    re.IGNORECASE)

# A polite OPENER is not a verdict on the sentence (independent audit round 4, MAJOR).
# "heads up, the same picture is on three posts next week" and "fyi the duplicate images
# are back on the calendar" are REPORTS wearing manners, and vetoing the whole message on
# the first two words lost them. The opener is stripped, then the rest is judged.
_POLITE_OPENER_RE = re.compile(
    r"^\s*(?:heads up|fyi|just so you know|quick one|quick question|hey|hi|hello|"
    r"morning|good morning|ok so|okay so|so)\b[\s,:-]*", re.IGNORECASE)

# A message can be an apology AND a report ("thanks for the quick turnaround, but the
# repeat images are still there"). Judged clause by clause, so one benign clause cannot
# bury a real complaint and one repeat word cannot convict a benign sentence.
# CONTRASTIVE boundaries only (independent audit round 5). Splitting on " so " and
# ". " severed a justification from its statement -- "The class schedule repeats so the
# same photo is fine" became a bare "the same photo is fine" -- and manufactured false
# reports. A contrastive marker is the one place a complaint genuinely hides behind a
# pleasantry ("thanks ..., but the repeat images are still there").
_CLAUSE_SPLIT_RE = re.compile(
    r"\s+(?:but|however|though|although|except)\s+|[;\n]+", re.IGNORECASE)


# A REPORT says who/when/that-it-is-still-happening. An INSTRUCTION does not.
#
# Independent audit round 6: the veto list could not keep up with bare imperatives --
# "Put the duplicate graphic on Thursday's post too", "Stick with the same caption on
# the story", "Use that same picture again for the Friday reel". 16 of 20 imperatives
# dispatched a fixer request. Enumerating benign shapes is a losing game: a leading-verb
# veto also eats "Repeat photos again this week on the posts", which IS a complaint.
#
# So this inverts the test. On top of _REPEAT_RE + a domain noun, the clause must carry
# a POSITIVE report signal: someone other than the client doing it (you / echo / it),
# the client's OWN book as the subject (my posts, my calendar), a persistence marker
# (still, again, keeps, there are, back on, showing up), or a date. Every genuine
# complaint in five rounds of corpora carries one; an instruction carries none.
# STRONG signals: an instruction cannot carry one of these.
#   1. someone OTHER than the client is doing it, including the book itself
#   3. it is STILL happening / it is THERE
#   4. a run of days, a spread across posts, a numeric date
_REPORT_SIGNAL_RE = re.compile(
    r"\b(?:you|you'?re|youre|echo|it'?s|its|it is|they|the system|the calendar|"
    r"the feed|the queue|the book|the images?|the photos?|the pics?|the pictures?|"
    r"the posts?|the reels?|the clips?|the videos?|the footage|the captions?)\s+"
    r"(?:\w+\s+){0,3}?"
    r"(?:repeat|repeats|repeated|repeating|reuse|reused|reusing|duplicate|duplicated|"
    r"duplicating|recycled|recycling|used|using|put|putting|posted|scheduled)\b|"
    # PAST TENSE about what happened: an instruction never says "went out".
    r"\b(?:went out|came out|got posted|got published)\b|"
    # "these are duplicate images again" -- a demonstrative subject is report shaped.
    r"\b(?:these|those) (?:are|were|keep|look)\b|"
    r"\b(?:still|keeps|kept|back on|(?:are|is|were|was) back|showing up|showed up|"
    r"shows up|there'?s|there (?:are|is|were|was)|noticed|"
    r"why (?:is|are|do|does|did))\b|"
    r"\b(?:\d+|two|three|four|five|several) (?:days|weeks|times) in a row\b|"
    r"\b(?:is|are|sits?|sitting) on \w+ (?:posts?|days?|dates?)\b|"
    r"\bon (?:\d+|two|three|four|five|several|multiple|different) "
    r"(?:posts?|days?|dates?)\b|"
    r"\b\d{1,2}\s*/\s*\d{1,2}\b"
    , re.IGNORECASE)

# WEAK signal: the client's OWN book named as a possessive. It reads like a report
# ("my posts are repeating") but an INSTRUCTION names the same thing just as readily
# ("use the same image on my posts for the rest of the month"), and round 7 measured 15
# of 18 such imperatives dispatching a fixer request. So this is necessary, never
# sufficient: a clause whose ONLY evidence is the possessive must not also open with a
# bare imperative verb.
_OWN_BOOK_RE = re.compile(
    r"\b(?:my|our) (?:posts?|reels?|calendar|images?|photos?|pics?|pictures?|videos?|"
    r"clips?|footage|feed|story|stories|book|queue)\b", re.IGNORECASE)

# A clause that OPENS with a bare verb is an instruction, not a report. Only consulted
# when the possessive above is the sole evidence, so "Repeat photos again this week on
# the posts" -- which the round-6 comment rightly flagged as a real complaint -- is
# unaffected unless it carries nothing else either.
_LEADING_IMPERATIVE_RE = re.compile(
    r"^\s*(?:just |please |pls |also |and )?"
    r"(?:(?:use|put|post|run|swap|stick|keep|leave|load|send|bring|add|drop|set|make|"
    r"schedule|upload|go|pull|grab|throw|change|move)"
    # repeat / duplicate / reuse double as ADJECTIVES on the media noun, which is how a
    # report opens ("duplicate images on my calendar"). Only an object that is NOT a
    # media noun makes them imperative ("just repeat last month's photos").
    r"|(?:reuse|repeat|duplicate|recycle)(?!\s+(?:images?|photos?|pics?|pictures?|"
    r"videos?|clips?|posts?|reels?|footage)\b))\b",
    re.IGNORECASE)


def _reads_as_a_report(clause):
    """True when this clause reads as a REPORT of duplicate media rather than an
    instruction to produce some. See _REPORT_SIGNAL_RE / _OWN_BOOK_RE."""
    if _REPORT_SIGNAL_RE.search(clause):
        return True
    if not _OWN_BOOK_RE.search(clause):
        return False
    return not _LEADING_IMPERATIVE_RE.match(clause)


def is_repeat_report(text):
    """True when this reads as a CLIENT REPORTING duplicate media, not asking about it,
    instructing us to reuse something, or thanking us. Pure and deterministic."""
    t = _POLITE_OPENER_RE.sub("", (text or "").strip(), count=1).strip()
    if not t:
        return False
    for clause in _CLAUSE_SPLIT_RE.split(t):
        clause = (clause or "").strip(" ,")
        if not clause or not _REPEAT_RE.search(clause):
            continue
        if not (_DOMAIN_RE.search(clause) or _MEDIA_NOUN_RE.search(clause)):
            continue
        if _NOT_A_REPEAT_REPORT_RE.search(clause):
            continue
        # A REPORT, not an instruction: something must say who, when, or that it is
        # still happening. See _REPORT_SIGNAL_RE.
        if not _reads_as_a_report(clause):
            continue
        return True
    return False

_QUESTION_RE = re.compile(
    r"(\?\s*$)|^\s*(how|what|when|where|why|who|which|can you|could you|do you|does|is it|"
    r"are you|will|should i|did)\b", re.IGNORECASE)

# Ranger only: a request to DO something to ads. Kept narrow on purpose.
_ACTION_RE = re.compile(
    r"\b(pause|resume|unpause|turn (?:off|on)|scale|increase|decrease|raise|lower|"
    r"budget|spend|launch|relaunch|target(?:ing)?|audience|duplicate|kill|stop the ad|"
    r"start the ad)\b", re.IGNORECASE)

# A client asking to cancel/skip a scheduled content_calendar post. Two shapes:
#   1. a cancel verb, then (not immediately, within a short gap) a post-ish noun --
#      "cancel my post today", "can you skip tomorrow's scheduled post".
#   2. the "don't/won't post" negation, which already carries its own noun --
#      "please don't post today", "don't post tomorrow".
# Deliberately narrow (post/story/reel/schedule only) so "cancel my membership" or
# "stop calling me" never matches; this is content_calendar cancellation only, never
# billing, ads, or anything else "cancel"/"stop"/"kill" could mean elsewhere in Echo.
_CANCEL_NOUN = r"post|posts|posting|story|stories|reel|reels|schedule|scheduled"
_CANCEL_POST_RE = re.compile(
    rf"\b(?:cancel|skip|stop|pull|remove|kill|hold off on)\b"
    rf"(?:(?!\b(?:{_CANCEL_NOUN})\b).){{0,40}}"
    rf"\b(?:{_CANCEL_NOUN})\b"
    rf"|\b(?:don'?t|do not|won'?t)\s+post\b",
    re.IGNORECASE)

# Ranger request_type vocabulary (migration 0303), best effort from the text.
_REQUEST_TYPE_RULES = (
    ("pause_resume", re.compile(r"\b(pause|resume|unpause|turn (?:off|on)|stop|start)\b", re.I)),
    ("budget",       re.compile(r"\b(budget|spend|scale|increase|decrease|raise|lower)\b", re.I)),
    ("launch",       re.compile(r"\b(launch|relaunch|go live|new campaign)\b", re.I)),
    ("targeting",    re.compile(r"\b(target(?:ing)?|audience|geo|radius|age)\b", re.I)),
)

_VALID = frozenset({QUESTION, CODE_FIX, ACTION_REQUEST, CANCEL_POST, FOLLOW_UP})

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

    Returns a callable (text) -> label | None, or None when there is no key to call with.

    C1 (2026-09-05 audit, CRITICAL): this used to build and return the closure
    unconditionally, because the ANTHROPIC_API_KEY check lived inside answer_lane.default_llm
    at CALL time. So build_classify_llm could never see a failure, its NotWiredError branches
    were unreachable, and a keyless deployment booted while LOGGING "classifier LLM wired" --
    then escalated every message, because each call raised and classify() turned that into
    ESCALATE. That is the D51 flood wearing the badge of the fix for it. The key is now
    checked HERE, at build time, which is the only place a boot assertion can see it.

    The returned callable NEVER raises out to the caller: classify() already treats an
    exception as ESCALATE, and this returns None on anything unexpected, so the deterministic
    behaviour is the floor and the model can only ever fill the middle."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return None
    try:
        import anthropic  # noqa: F401 - m1 (audit 2): a key with no client library is still
        # "flagged on with nothing behind it". Imported HERE so the boot assertion sees it,
        # not at first call where the failure is indistinguishable from "nothing to say".
    except Exception:  # noqa: BLE001
        return None

    # Finding 4 (2026-09-05 audit 3): a PRESENT BUT INVALID key (revoked, typo'd, wrong
    # project) builds fine and cannot be detected without a network call, so the boot
    # assertion can never catch it. What it can do is refuse to be SILENT: classify() catches
    # every exception and returns ESCALATE with no log, which is byte-for-byte
    # indistinguishable from a healthy classifier that had nothing to say -- the D51 flood
    # again. So the closure logs each failure loudly and counts consecutive ones, and the
    # count is surfaced on the wiring health line. A dead key now looks like a dead key.
    state = {"consecutive_failures": 0}

    def _llm(text):
        from . import answer_lane as _al
        try:
            raw = _al.default_llm(_LLM_SYSTEM, str(text or "")[:4000], model=model)
        except Exception as e:  # noqa: BLE001 - re-raised after being made visible
            state["consecutive_failures"] += 1
            n = state["consecutive_failures"]
            level = "CRITICAL" if n >= 3 else "WARNING"
            print(f"[slack-convo/classifier] {level} model call failed "
                  f"({type(e).__name__}: {str(e)[:200]}); this is the "
                  f"{n}{'st' if n == 1 else 'nd' if n == 2 else 'rd' if n == 3 else 'th'} "
                  f"consecutive failure. Every message is escalating to a person while this "
                  f"lasts. Check ANTHROPIC_API_KEY on this service.")
            raise
        state["consecutive_failures"] = 0
        verdict = (raw or "").strip().splitlines()[0].strip() if raw else ""
        return verdict if verdict in _VALID else None

    _llm.failure_state = state
    return _llm


def classify(text, *, has_open_ticket, identity_product, llm=None, brain_hint=None,
            cancel_post_enabled=False, repeat_report_enabled=False):
    """One label from the fixed set, or None (escalate). Never raises.

    cancel_post_enabled (AGENT_SLACK_CANCEL_POST_ENABLED, default False): the ONLY gate
    on the CANCEL_POST rule below. False is byte identical to before this label existed --
    the rule is never checked and this function's behavior is unchanged.

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
    # Checked before breakage/question so "cancel my post" and "can you skip tomorrow's
    # post" never fall through to CODE_FIX or QUESTION (the latter's answer lane refuses
    # ANY message containing "cancel my" as a billing question -- see answer_lane.py's
    # _BILLING_RE -- which is exactly the escalate-with-no-help pattern this classification
    # exists to avoid).
    if cancel_post_enabled and _CANCEL_POST_RE.search(t):
        return CANCEL_POST
    # RT-M2: breakage AND an Echo-domain noun. Breakage alone escalates to a human.
    if _BREAKAGE_RE.search(t) and _DOMAIN_RE.search(t):
        return CODE_FIX
    if _QUESTION_RE.search(t):
        return QUESTION
    # WRONG-OUTPUT reports ("still repeat images", "the same photo three days in a
    # row"). Checked AFTER the question rule on purpose -- see _REPEAT_RE -- so an owner
    # ASKING about repeats reaches the answer lane, while an owner REPORTING them
    # reaches the fixer. Same _DOMAIN_RE gate every code_fix carries (RT-M2), plus
    # _NOT_A_REPEAT_REPORT_RE for the instruction / client-is-the-actor / thanks
    # families. Gated exactly like CANCEL_POST: repeat_report_enabled is the ONE switch
    # (AGENT_SLACK_REPEAT_CODE_FIX, default OFF), so False is byte identical to before
    # this rule existed.
    if repeat_report_enabled and is_repeat_report(t):
        return CODE_FIX
    # CANCEL_POST is gated on cancel_post_enabled even from a brain hint or the LLM
    # fallback: the flag is the ONE switch for this whole capability, so a learned
    # phrase or a model guess can never turn it on when it is off.
    allowed = _VALID if cancel_post_enabled else (_VALID - {CANCEL_POST})
    if brain_hint is not None:
        hinted = brain_hint.classification_hint_for(t)
        if hinted in allowed and hinted != FOLLOW_UP:
            return hinted
    if llm is not None:
        try:
            verdict = llm(t)
        except Exception:  # noqa: BLE001 - a model fault escalates, never dispatches
            return ESCALATE
        if verdict in allowed and verdict != FOLLOW_UP:
            return verdict
    return ESCALATE
