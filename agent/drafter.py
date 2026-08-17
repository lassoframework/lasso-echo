"""
Drafter: turns a voice doc + one creative into one post draft.

NO FABRICATION. The default caption generator only recombines text the human
already approved: the brand voice doc and the client-provided note on the
creative. It never invents an offer, a price, a claim, or a fact.

The generator is pluggable. A future LLM generator can slot in here, but it MUST
be constrained to the voice doc + client note and stay inside the same contract.
"""

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from . import config
from . import content_planner
from . import media_host
from . import ops_alerts
from .accounts import Platform
from .voice import load_voice


class DraftStatus(Enum):
    PENDING = "pending"      # waiting for Blake
    BLOCKED = "blocked"      # cannot draft (e.g. no voice doc)
    APPROVED = "approved"
    SKIPPED = "skipped"
    # Replaced by a newer draft for the same account + day + type (idempotent
    # re-run whose content changed). Its card is edited to a superseded state and
    # approving it does nothing (see approvals.handle_action).
    SUPERSEDED = "superseded"
    # A PENDING draft whose posting day has passed (idempotent flag ON only): the
    # next daily run flips it here, edits its card to an expired state, and
    # approving it does nothing (see approvals.handle_action).
    EXPIRED = "expired"


@dataclass
class Draft:
    draft_id: str
    account_key: str
    platform: str
    caption: str
    hashtags: list
    creative_path: str
    creative_public_url: str
    scheduled_for: str
    status: DraftStatus = DraftStatus.PENDING
    blocked_reason: str = ""
    # source spans we composed FROM, kept for the no-fabrication test + audit
    source_fragments: list = field(default_factory=list)
    # carousel support: local slide paths + their public URLs (empty for singles)
    slides: list = field(default_factory=list)
    slide_urls: list = field(default_factory=list)
    # Google Business Profile only: structured CTA button + post topic (Meta paths ignore).
    cta_type: str = ""
    cta_url: str = ""
    topic_type: str = "STANDARD"
    # Stories: True for a 9:16 Story draft so the card and the publisher can never
    # confuse it with a feed post. Feed drafts leave this False.
    is_story: bool = False
    # Idempotent daily drafts (flag AGENT_IDEMPOTENT_DRAFTS_ENABLED, default OFF):
    # the (account, day, type) identity of the draft plus the Slack message that
    # carries its card, so a re-run can find it and a superseding run can edit the
    # old card in place. All four stay empty while the flag is OFF.
    day_key: str = ""
    draft_type: str = ""       # "feed" or "story" (empty while the flag is OFF)
    slack_channel: str = ""
    slack_ts: str = ""
    warnings: list = field(default_factory=list)  # card-time notes (e.g. OCR check)
    # Category rotation (AGENT_CATEGORY_ROTATION, default OFF). Empty while off.
    category: str = ""    # one of content_categories.CATEGORIES, or "" when not set
    sub_topic: str = ""   # platform sub-topic (ads, google, nurture, ...) or ""
    # Thin-library grace (client sources, AGENT_CLIENT_SOURCES): a caption-ready
    # day with no image yet. PENDING and held; a human adds a photo before it can
    # publish. NOT blocked. False for every other draft.
    needs_media: bool = False
    # Force the approval card even when AGENT_AUTO_APPROVE_ENABLED is armed: this
    # draft ALWAYS waits for a human to approve, deny, or edit it, and never
    # auto-publishes. Used by the demo calendar so Blake experiences the review
    # flow while the rest of the fleet keeps auto-publishing. Default False =
    # every existing draft behaves exactly as before. This only ever STRENGTHENS
    # the gate (adds a required approval); it never bypasses one.
    force_approval: bool = False


def _make_id(account_key, creative_path, scheduled_for):
    h = hashlib.sha1(f"{account_key}|{creative_path}|{scheduled_for}".encode()).hexdigest()
    return h[:10]


def _stem(creative):
    """The creative filename stem, used as the stable rotation key."""
    stem = getattr(creative, "stem", None)
    if stem:
        return stem
    path = getattr(creative, "path", "") or ""
    return os.path.splitext(os.path.basename(path))[0]


def _det_index(key, n):
    """Deterministic index in [0, n) from sha1(key). Stable across re-runs."""
    if n <= 0:
        return 0
    return int(hashlib.sha1((key or "").encode()).hexdigest(), 16) % n


def _pick_cta(voice, creative):
    """
    Pick one CTA from the approved rotation in the voice doc.

    Growth-hint CTAs (save / tag / share / dm / send) are PREFERRED — they drive
    the reach signals that actually grow an account. Selection within the chosen
    pool is deterministic by sha1 of the creative filename stem, so the same card
    always gets the same CTA while different cards rotate through the list.
    Returns "" if the voice doc defines no CTAs.
    """
    if not voice.ctas:
        return ""
    growth = [c for c in voice.ctas
              if any(h in c.lower() for h in TemplateGenerator.GROWTH_CTA_HINTS)]
    pool = growth if growth else list(voice.ctas)
    return pool[_det_index(_stem(creative), len(pool))]


def _caption_has_cta(caption, voice):
    """True if the caption already ends with an approved CTA verbatim, so we
    don't append (and duplicate) one."""
    low = caption.lower()
    return any(c.lower() in low for c in voice.ctas)


def _select_hashtags(voice, creative):
    """
    Select up to HASHTAG_LIMIT (5) hashtags from the approved set in the voice
    doc. Brand-tier tags come first if present, then niche/topic tags rotated
    deterministically per creative. In 2026, 3–5 tags is the whole strategy —
    more does not help (see the bible's hashtag section).
    """
    BRAND_TAGS = {"#LASSOFramework", "#GymMarketingMadeSimple", "#LASSOPinnacle"}
    all_tags = list(voice.hashtags)

    brand = [t for t in all_tags if t in BRAND_TAGS]
    rest = [t for t in all_tags if t not in BRAND_TAGS]

    offset = _det_index(_stem(creative), max(len(rest), 1))
    rotated = rest[offset:] + rest[:offset]

    limit = TemplateGenerator.HASHTAG_LIMIT
    selected = brand + rotated[: max(0, limit - len(brand))]
    return selected[:limit]


# Facebook best practice: at most 2 hashtags, at the end of the caption (the
# composer already appends hashtags at the end, so placement is preserved).
FB_HASHTAG_LIMIT = 2


def variant_hashtags(platform, hashtags):
    """
    Per-platform hashtag selection (Task: platform variants). SELECTION ONLY, never
    new text: every returned tag is one of the approved tags passed in.

    Flag OFF (default) -> the list is returned unchanged, exactly today's behavior.
    Flag ON  -> Instagram keeps up to 5 (the existing cap); a Facebook Page keeps
    at most FB_HASHTAG_LIMIT (2).
    """
    tags = list(hashtags or [])
    if not config.platform_variants_enabled():
        return tags
    if platform == Platform.FACEBOOK_PAGE:
        return tags[:FB_HASHTAG_LIMIT]
    return tags[:TemplateGenerator.HASHTAG_LIMIT]


import re as _re_scaffold

# Lines the LLM sometimes prepends despite "output ONLY the caption body": a markdown
# header (# Caption Body:), a bare label (Caption:, Body:, Post:), or a preamble
# (Here's the caption:). Stripped so scaffolding never reaches a gym's feed.
_SCAFFOLD_LINE = _re_scaffold.compile(
    r"^\s*(#{1,6}\s*)?(caption( body| text)?|body|post|here'?s[^\n:]*)\s*:?\s*$",
    _re_scaffold.IGNORECASE)


def _strip_llm_scaffold(text):
    """Remove leading markdown headers / 'Caption:'-style labels / wrapping quotes the
    LLM may add around the caption body. Returns the clean caption text."""
    lines = (text or "").strip().splitlines()
    # drop leading scaffold/label/blank lines
    while lines and (not lines[0].strip() or _SCAFFOLD_LINE.match(lines[0])
                     or lines[0].lstrip().startswith("#")):
        lines.pop(0)
    out = "\n".join(lines).strip()
    # unwrap surrounding quotes if the whole body is quoted
    if len(out) >= 2 and out[0] in "\"'“”" and out[-1] in "\"'“”":
        out = out[1:-1].strip()
    return out


def _call_llm_caption(system, user):
    """Call Claude for SB7 caption generation. Raises on missing key or SDK."""
    import os as _os
    from . import config as _cfg
    key = _os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    try:
        import anthropic
    except Exception:
        raise RuntimeError("anthropic SDK not installed")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=_cfg.sb7_model(), max_tokens=400,
        system=system, messages=[{"role": "user", "content": user}])
    parts = getattr(resp, "content", []) or []
    return "".join(getattr(p, "text", "") or "" for p in parts)


import re as _re_claims
# A figure the caption might carry: a standalone number, optionally with a decimal
# or thousands separators (579, 6, 12,000, 4.5). Currency/percent/multiplier signs
# around it don't change the digits, so matching the digit run is sufficient.
_FIGURE_RE = _re_claims.compile(r"\d[\d,\.]*")

# Cross-day OPENING-REPETITION guard (Ryan Parr, 2026-08-17). Each planned day's
# caption is generated independently; with a small problem palette the model kept
# opening several days in a row with the same hook ("You're juggling too much" x3,
# then "You're swamped" x3). We normalize the first few words of each accepted
# caption into an "opening signature" and feed the running set back into the next
# day's prompt as a STYLE-only "do not open like these" instruction. Never a source
# of facts; a caption is always still produced (this never blocks a post).
_OPENING_WORDS = 7
_OPENING_COLLIDE_WORDS = 4   # two openings collide if they share this many leading words
# Apostrophes (straight or curly) are REMOVED so "You're" and "Youre" normalize the
# same; all other punctuation becomes a token break.
_OPENING_APOS = _re_claims.compile(r"['‘’]")
_OPENING_STOP = _re_claims.compile(r"[^\w\s]")


def _opening_tokens(caption):
    """Normalized leading tokens of a caption's first real (non-hashtag) line:
    lowercased, apostrophes removed (so "You're" and "Youre" both -> "youre"), other
    punctuation split, whitespace collapsed. Returns [] for an empty caption."""
    for line in (caption or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = _OPENING_STOP.sub(" ", _OPENING_APOS.sub("", line)).lower()
        toks = cleaned.split()
        if toks:
            return toks
    return []


def opening_signature(caption, words=_OPENING_WORDS):
    """A normalized signature of a caption's OPENING: the first `words` normalized
    words of its first non-hashtag line. Two captions that lead with the same hook
    (modulo punctuation/case) share a leading prefix, so the month builder can record
    and avoid opener repetition. Returns "" for an empty caption."""
    return " ".join(_opening_tokens(caption)[:words])


def openings_collide(caption, avoid_openings, prefix=_OPENING_COLLIDE_WORDS):
    """True when `caption` opens like any phrase in `avoid_openings`: they share the
    same first `prefix` normalized words (or one opening is shorter and is a full
    leading prefix of the other). This is the cross-day repetition Ryan flagged —
    "You're juggling too much ..." on several days running — detected even when the
    words AFTER the shared hook differ. STYLE signal only; it never blocks a post."""
    cap_toks = _opening_tokens(caption)
    if not cap_toks:
        return False
    cap_head = cap_toks[:prefix]
    for other in avoid_openings or ():
        other_toks = _opening_tokens(other) if isinstance(other, str) else []
        if not other_toks:
            continue
        other_head = other_toks[:prefix]
        n = min(len(cap_head), len(other_head))
        if n and cap_head[:n] == other_head[:n]:
            return True
    return False


def _output_claims_cleared(body, voice, client_note):
    """True when every figure in `body` appears verbatim in an approved input (the
    client note or the voice doc). No approved figure -> clean. This blocks an LLM
    from smuggling an invented stat/price/count into a caption; a rephrased but real
    number (its digits are in the sources) still passes."""
    sources = f"{client_note}\n{getattr(voice, 'raw', '') or ''}"
    for tok in _FIGURE_RE.findall(body or ""):
        norm = tok.strip(".,")
        if norm and norm not in sources:
            return False
    return True


class StoryBrandGenerator:
    """
    LLM-powered caption generator using the StoryBrand SB7 framework.

    Applies the framework structure (customer as hero, problem-first, gym as guide,
    one clear CTA) while drawing ONLY from the approved voice doc and the client's
    note. No invented facts, stats, prices, or offers ever.

    Falls back to TemplateGenerator silently on any LLM failure so the card
    always gets a caption. Gated behind AGENT_SB7_ENABLED (OFF by default).
    """

    HASHTAG_LIMIT = 5

    _SYSTEM = (
        "You are a direct-response social media copywriter for a boutique gym or "
        "fitness studio. Write captions using StoryBrand SB7 principles:\n"
        "  Hero = the customer (busy professional, beginner, lifestyle seeker, 40+ reclaim)\n"
        "  Problem = their real pain (time, energy, stuck, overwhelmed, intimidated, "
        "no results, no accountability, tried everything, guilt, low confidence)\n"
        "  Guide = the gym signals brief empathy then authority\n"
        "  Plan = one implied simple step from the creative\n"
        "  CTA = provided separately; do NOT include it in the body\n\n"
        "HARD RULES:\n"
        "- Draw ONLY from the brand voice doc and client note provided. No invented "
        "facts, stats, prices, or offers.\n"
        "- No em dashes, en dashes, or hyphens used as punctuation dashes.\n"
        "- Keep the customer's problem central, but VARY the ENTRY POINT. Do not open "
        "every caption the same way. Rotate how you begin: sometimes the problem, "
        "sometimes the outcome they want, sometimes a question, sometimes a scene or "
        "member moment from the photo, sometimes a myth to bust. Even with limited "
        "source material, make the OPENING WORDS feel fresh, not a repeat of a stock "
        "hook. Be punchy and direct.\n"
        "- Body max 260 characters (hashtags and CTA appended separately).\n"
        "- Output ONLY the caption body text. No CTA. No hashtags. No quotes.\n"
        "- Never mention specific numbers, percentages, or prices unless they appear "
        "verbatim in the client note."
    )

    @staticmethod
    def _avoid_openings_block(avoid_openings):
        """A HARD prompt instruction listing recent opening phrases this caption must
        NOT begin with or closely paraphrase, so consecutive planned days stop leading
        with the same hook (Ryan Parr, 2026-08-17). STYLE guidance only: it steers the
        entry point, never a source of facts. Returns "" when there is nothing to
        avoid (a brand-new gym / the first day behaves exactly as before)."""
        seen, phrases = set(), []
        for p in avoid_openings or ():
            p = (p or "").strip()
            low = p.lower()
            if p and low not in seen:
                seen.add(low)
                phrases.append(p)
        if not phrases:
            return ""
        lines = ["OPENINGS ALREADY USED ON RECENT DAYS. Do NOT begin this caption with "
                 "any of these, and do NOT closely paraphrase them. Choose a DIFFERENT "
                 "entry point (a different problem angle, an outcome, a question, a scene, "
                 "or a member moment) so this day's opening words are clearly distinct:"]
        for p in phrases:
            lines.append(f"- {p}")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _brain_guidance(account):
        """Fold THIS gym's learned preferences into the prompt so every edit
        makes the next caption better. Two signals, both fabrication-gated at the
        source (tenant_brain), so nothing here can introduce an unverified claim:

          - deny reasons + style notes  (tenant_brain.prompt_notes, deduped)
          - past before/after edits      (tenant_brain.edit_examples)

        Returns "" when there is no account, the brain flag is OFF, or nothing
        has been learned yet, so a brand-new gym behaves exactly as before."""
        if account is None:
            return ""
        key = getattr(account, "key", "") or ""
        if not key:
            return ""
        try:
            from . import tenant_brain
            # dedupe prompt_notes preserving order (the edit path records a
            # generic "style preference" rule, so raw notes repeat; collapse them)
            seen, notes = set(), []
            for n in tenant_brain.prompt_notes(key):
                if n not in seen:
                    seen.add(n)
                    notes.append(n)
            examples = tenant_brain.edit_examples(key)
        except Exception as exc:
            print(f"[sb7] brain guidance unavailable ({type(exc).__name__}: {exc})")
            return ""
        if not notes and not examples:
            return ""
        parts = ["THIS GYM'S LEARNED PREFERENCES (style guidance only, NEVER a "
                 "source of facts):"]
        for n in notes:
            parts.append(f"- {n}")
        if examples:
            parts.append("Recent edits this gym's approver made. Learn the STYLE "
                         "shift they prefer; do NOT copy any specific facts from them:")
            for before, after in examples:
                if before:
                    parts.append(f"  BEFORE: {before}")
                parts.append(f"  AFTER (preferred): {after}")
        return "\n".join(parts) + "\n\n"

    def build(self, voice, creative, account=None, avoid_openings=()):
        """Write one SB7 caption.

        avoid_openings (optional): normalized opening phrases used on RECENT planned
        days (see opening_signature). Folded into the prompt as a HARD "do not open
        like these" instruction so consecutive days stop leading with the same hook
        (Ryan Parr, 2026-08-17). STYLE-only: it never carries a fact. When a generated
        caption's opening still collides with a recent one, we retry ONCE with a
        stronger nudge and prefer the more varied result; we NEVER block the post over
        it — a caption is always produced (template fallback + figure gate stay intact).
        A brand-new gym / the first day passes avoid_openings empty and behaves exactly
        as before."""
        client_note = (creative.client_note or "").strip()
        cta = _pick_cta(voice, creative)
        hashtags = _select_hashtags(voice, creative)

        if not client_note:
            return TemplateGenerator().build(voice, creative)

        guidance = self._brain_guidance(account)
        avoid_block = self._avoid_openings_block(avoid_openings)
        avoid_list = [p for p in (avoid_openings or ()) if (p or "").strip()]

        def _compose(extra_nudge=""):
            user = (
                f"BRAND VOICE DOC:\n{voice.raw}\n\n"
                f"CLIENT NOTE ON THIS POST:\n{client_note}\n\n"
                f"{guidance}"
                f"{avoid_block}"
                f"{extra_nudge}"
                "Write a StoryBrand-structured caption body. Problem-first. "
                "Gym as guide, not hero. Max 260 characters. Caption body only."
            )
            return _strip_llm_scaffold(_call_llm_caption(self._SYSTEM, user) or "")

        try:
            body = _compose()
            if not body:
                raise ValueError("empty LLM response")
            # CROSS-DAY OPENING VARIETY: if the opening still collides with a recent
            # day's opening, retry ONCE with a stronger nudge and keep whichever result
            # is distinct. This never blocks: the collided caption is still valid copy,
            # so we only PREFER the varied one; a caption is always produced.
            if avoid_list and openings_collide(body, avoid_list):
                retry = _compose(
                    "IMPORTANT: your opening MUST be clearly different from the recent "
                    "openings listed above. Start from a different angle entirely.\n\n")
                if retry and not openings_collide(retry, avoid_list):
                    body = retry
            # OUTPUT FABRICATION GATE (deterministic, never skipped): every figure
            # (stat, price, count) in the generated caption MUST trace to an approved
            # input (the client note or the voice doc). A caption carrying a number
            # the sources do not clear is a possible hallucination, so we REJECT it
            # and fall back to the verbatim template rather than surface an invented
            # stat. This restores the deterministic no-fabrication floor the template
            # has; the prompt's HARD RULES are belt, this is suspenders.
            if not _output_claims_cleared(body, voice, client_note):
                print("[sb7] output carried a number not in the approved sources; "
                      "falling back to template (no fabrication)")
                return TemplateGenerator().build(voice, creative)
            fragments = [body]
            if cta and not _caption_has_cta(body, voice):
                fragments.append(cta)
            caption = "\n\n".join(fragments).strip()
            return caption, hashtags, fragments
        except Exception as exc:
            print(f"[sb7] LLM caption failed ({type(exc).__name__}: {exc}), "
                  "falling back to template")
            return TemplateGenerator().build(voice, creative)


class TemplateGenerator:
    """
    Deterministic, zero-fabrication caption builder (the safe Stage 1 baseline).
    Caption = client's approved note + one CTA from the voice doc rotation.
    Hashtags are pulled from the approved doc, brand tier always included.

    Upgrade path: a constrained LLM generator can replace this to write a real
    hook / problem / insight / CTA caption, but it must draw ONLY from the voice
    doc + client note and keep this same contract.
    """

    HASHTAG_LIMIT = 5
    GROWTH_CTA_HINTS = ("save", "tag", "share", "dm", "send")

    def build(self, voice, creative, account=None):
        # account is accepted for a uniform generator interface but ignored: the
        # deterministic template never leans on learned preferences.
        fragments = []

        # 1. Client note (the core body — verbatim, no fabrication)
        if creative.client_note:
            fragments.append(creative.client_note.strip())

        caption = "\n\n".join(fragments).strip()

        # 2. CTA from the approved rotation — appended verbatim, but ONLY if the
        #    caption doesn't already carry one.
        cta = _pick_cta(voice, creative)
        if cta and not _caption_has_cta(caption, voice):
            fragments.append(cta)
            caption = "\n\n".join(fragments).strip()

        # 3. Hashtags: brand tier first, rest rotated per creative, capped at 5.
        hashtags = _select_hashtags(voice, creative)

        return caption, hashtags, fragments


def draft_post(account, creative, scheduled_for, voice=None,
               generator=None, voice_path=None):
    """
    Build one Draft for one account. Returns a Draft.

    If the voice doc is missing -> returns a BLOCKED draft. We draft NOTHING.
    """
    if voice is None:
        voice = load_voice(voice_path or config.VOICE_DOC_PATH)

    draft_id = _make_id(account.key, getattr(creative, "path", "none"), scheduled_for)

    if voice is None:
        return Draft(
            draft_id=draft_id,
            account_key=account.key,
            platform=account.platform,
            caption="",
            hashtags=[],
            creative_path="",
            creative_public_url="",
            scheduled_for=scheduled_for,
            status=DraftStatus.BLOCKED,
            blocked_reason="Brand voice doc missing or empty. Not drafting.",
        )

    if creative is None:
        return Draft(
            draft_id=draft_id,
            account_key=account.key,
            platform=account.platform,
            caption="",
            hashtags=[],
            creative_path="",
            creative_public_url="",
            scheduled_for=scheduled_for,
            status=DraftStatus.BLOCKED,
            blocked_reason="No creative available in the library. Not drafting.",
        )

    # PIXEL FABRICATION GATE (always on, never weakened): the words rendered INTO
    # the creative must resolve to an approved receipt, the same rule captions
    # obey. A rendered stat with no approved source BLOCKS the card and NAMES the
    # number; it never softens, never falls back, never publishes. Deterministic
    # and free (reads the recorded rendered text in the sidecar); the OCR belt
    # only runs when the studio reader is available.
    from . import pixel_gate
    ok, gate_reason = pixel_gate.gate_creative(creative)
    if not ok:
        ops_alerts.alert(f"fabrication gate BLOCKED a card for {account.key}: "
                         f"{gate_reason} ({getattr(creative, 'path', '')}).")
        return Draft(
            draft_id=draft_id,
            account_key=account.key,
            platform=account.platform,
            caption="",
            hashtags=[],
            creative_path=getattr(creative, "path", ""),
            creative_public_url="",
            scheduled_for=scheduled_for,
            status=DraftStatus.BLOCKED,
            blocked_reason="Fabrication gate (pixels): " + gate_reason,
        )

    if generator is None:
        generator = StoryBrandGenerator() if config.sb7_enabled() else TemplateGenerator()
    gen = generator

    cta_type = cta_url = ""
    topic_type = "STANDARD"

    # Daily content brain: for a LASSO account with the brain armed, compose the
    # caption ONLY from the approved source doc (never the per-creative note, never
    # invented text). A blocked plan blocks the draft. Off / non-LASSO -> unchanged.
    plan_category = ""
    plan_sub_topic = ""
    if config.content_brain_enabled() and account.key.startswith("lasso"):
        plan = content_planner.plan_for(date.today().isoformat())
        if plan.get("blocked"):
            # Surfaced on the Slack card AND (flag ON) as one ops alert.
            ops_alerts.alert(f"content plan blocked for {account.key}: {plan['reason']}")
            return Draft(
                draft_id=draft_id,
                account_key=account.key,
                platform=account.platform,
                caption="",
                hashtags=[],
                creative_path="",
                creative_public_url="",
                scheduled_for=scheduled_for,
                status=DraftStatus.BLOCKED,
                blocked_reason="Content brain: " + plan["reason"],
            )
        if account.platform == Platform.GOOGLE_BUSINESS:
            # GBP variant: trimmed summary, NO hashtags, a structured CTA button + url.
            caption, hashtags, fragments = plan["summary"], [], plan["summary_fragments"]
            cta_type, cta_url = config.GBP_DEFAULT_CTA, config.GBP_CTA_URL
        else:
            caption, hashtags, fragments = plan["caption"], plan["hashtags"], plan["fragments"]
        plan_category = plan.get("category", "")
        plan_sub_topic = plan.get("sub_topic", "")
    else:
        # Pass the account so the SB7 generator can fold in this gym's learned
        # preferences (edits, deny reasons). TemplateGenerator ignores it.
        caption, hashtags, fragments = gen.build(voice, creative, account=account)

    # Caption standard (section 9): a draft with no caption text cannot enter the
    # approval queue — the content brain or generator returned nothing usable.
    if not caption.strip():
        return Draft(
            draft_id=draft_id,
            account_key=account.key,
            platform=account.platform,
            caption="",
            hashtags=[],
            creative_path=getattr(creative, "path", ""),
            creative_public_url="",
            scheduled_for=scheduled_for,
            status=DraftStatus.BLOCKED,
            blocked_reason="Caption standard (section 9): empty caption. Voice doc or content plan returned no text.",
        )

    # Per-platform variant (flag OFF -> unchanged): selection only, from the same
    # approved set. FB keeps at most 2 tags; IG keeps its existing cap of 5.
    hashtags = variant_hashtags(account.platform, hashtags)

    creative_public_url = getattr(creative, "public_url", "")
    slides = list(getattr(creative, "slides", []) or [])
    slide_urls = list(getattr(creative, "slide_urls", []) or [])

    # Scale-harden: when hosting is armed, publish the local creative(s) to S3 and use
    # the hosted URLs (tenant-scoped by account). OFF, or any failure, leaves the
    # existing sidecar URLs untouched -> current behavior is unchanged.
    if config.hosting_enabled():
        hosted = media_host.host_media(creative.path, account.key)
        if hosted:
            creative_public_url = hosted
        if slides:
            hosted_slides = media_host.host_many(slides, account.key)
            if hosted_slides:
                slide_urls = hosted_slides

    return Draft(
        draft_id=draft_id,
        account_key=account.key,
        platform=account.platform,
        caption=caption,
        hashtags=hashtags,
        creative_path=creative.path,
        creative_public_url=creative_public_url,
        scheduled_for=scheduled_for,
        status=DraftStatus.PENDING,
        source_fragments=fragments,
        slides=slides,
        slide_urls=slide_urls,
        cta_type=cta_type,
        cta_url=cta_url,
        topic_type=topic_type,
        category=plan_category,
        sub_topic=plan_sub_topic,
    )
