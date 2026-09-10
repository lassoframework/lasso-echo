"""
astra_prompt.py — the infographic BRIEF that feeds Astra.

Astra reads a creative brief through the Responses API and calls the image tool
itself, so it wants a BRIEF (voice, brand system, the one hook, the one CTA, a
concrete style spec) rather than the single dense sentence the Gemini path uses.
The Gemini prompt builder (creative_studio.build_prompt) is UNCHANGED and still
carries the fallback path byte for byte.

WHAT GOES IN THE BRIEF (spec section 3):
  - the brand VOICE doc (an excerpt, so tone carries into the rendered words)
  - brand COLORS and FONTS (the locked LASSO V3 palette + the house type family)
  - the post's HOOK (the one rendered headline) and its CTA
  - the FLAT EDITORIAL INFOGRAPHIC style spec: bold headline, a three element
    visual metaphor, a CTA button block, a URL footer — the Full Gym standard.

NO FABRICATION, same contract as everywhere else in Echo: the brief is built
ONLY from approved input handed in by the caller (headline, facts, CTA). It
invents no stat, offer, price, or client name. Empty facts -> the caller does
not generate at all (creative_studio.generate's gate, unchanged).

The brief deliberately carries the tokens the house-style grade gate reads
("exactly one" red, a named "visual anchor"), because the gate scores the prompt
that actually produced the image.
"""

import os

from . import config

# The house type system. The brand fonts ship in agent/assets/fonts and are the
# same family the PIL compositors use, so a generated card and a rendered card
# read as one system.
BRAND_TYPE_SYSTEM = (
    "BRAND TYPE SYSTEM (two families, never more):\n"
    "- HEADLINE: Anton, or the closest heavy condensed grotesque. Tight leading, "
    "sentence case or all caps, left aligned.\n"
    "- SUPPORT: Oswald Medium for eyebrows and labels, Montserrat Medium for the "
    "deck line and the footer. Small, letterspaced, quiet.\n"
    "Never a script face, never a serif display face, never more than two "
    "families on one card."
)

# The Full Gym standard, stated as four required blocks.
FLAT_EDITORIAL_SPEC = (
    "STYLE SPEC, FLAT EDITORIAL INFOGRAPHIC (the standard every card is held to):\n"
    "1. BOLD HEADLINE. One headline, the largest element on the card, left "
    "aligned in the upper area. It is the visual anchor of the composition. "
    "Nothing competes with it for scale.\n"
    "2. THREE ELEMENT VISUAL METAPHOR. Exactly three flat vector elements, drawn "
    "in clean line and solid fill, that show the idea concretely: three labeled "
    "nodes, three stacked bars, three stages, three icons in a row. Three, not "
    "two and not five. Each element carries one short UPPERCASE business label. "
    "No illustrated scenes, no people, no photorealism, no 3D, no gradients.\n"
    "3. CTA BUTTON BLOCK. One solid rounded rectangle near the lower third "
    "holding the call to action in short bold type, styled like a real interface "
    "button. This is the ONE red element on the card.\n"
    "4. URL FOOTER. The site URL set small, uppercase and letterspaced on the "
    "bottom margin, quiet and out of the way.\n"
    "Flat editorial means printed magazine infographic, not AI art: generous "
    "margins, one depth layer at most, high contrast, and no texture or pattern."
)

# Grade-gate compatible accent law: names the single red element explicitly.
SINGLE_ACCENT_LAW = (
    "COLOR LAW: red is used exactly one time on the card, and it is the CTA "
    "button block. Never a red background, never red type scattered through the "
    "card, never a second red element."
)

READABILITY_LAW = (
    "READABILITY: the headline must be legible at thumbnail size in a phone "
    "feed. High contrast between type and field. If the composition would "
    "compromise legibility, simplify the composition, never the contrast."
)

BANNED = (
    "BANNED, never render any of these: illustrated scenes, people or figures in "
    "action, cartoon elements, metaphorical props (dripping money, safes, broken "
    "pipes, locks), photorealistic photography, 3D effects, cluttered collages, "
    "watermarks, lorem ipsum, and generic process labels (STEP 1, PLAN, GROW, "
    "LEARN, DISCOVER, LAUNCH). Labels use real business language a gym owner "
    "recognizes: LEADS, BOOKED, SHOWED, CLOSED, FOLLOW UP, AD SPEND, SIGNUPS."
)

# Default footer URL. Matches the footer the existing variant grammar renders;
# a caller (a client gym card) passes its own.
DEFAULT_URL_FOOTER = "LASSOFRAMEWORK.COM"

# How much of the voice doc rides along. The doc is long; the brief only needs
# enough of it to set tone for the rendered words.
VOICE_EXCERPT_CHARS = 1200


def url_footer() -> str:
    raw = os.environ.get("AGENT_IMAGE_URL_FOOTER", "")
    return raw.strip() or DEFAULT_URL_FOOTER


def load_voice_excerpt(path=None, limit=VOICE_EXCERPT_CHARS) -> str:
    """An excerpt of the approved brand voice doc, or "" when it is missing.
    A missing voice doc never raises and never blocks: the rest of the brief
    (which carries every hard rule) still stands on its own."""
    try:
        from . import voice
        doc = voice.load_voice(path or config.VOICE_DOC_PATH)
    except Exception:
        return ""
    if doc is None:
        return ""
    raw = str(getattr(doc, "raw", "") or "").strip()
    if len(raw) > limit:
        raw = raw[:limit].rsplit("\n", 1)[0]
    return raw


def build_infographic_brief(headline, facts, *, cta="", surface="feed post",
                            pixels=None, aspect=None, voice_path=None,
                            palette=None, footer=None, kind="infographic"):
    """Build the Astra creative brief from APPROVED input ONLY.

    `headline` is the one hook rendered on the card. `facts` are the approved
    body lines: they steer the three element visual metaphor and are NOT rendered
    as sentences on the image. `cta` is the approved call to action that fills the
    button block. Nothing is invented here.

    Returns the brief string, dash-scrubbed and checked against the same hard
    rules the Gemini prompt is checked against (banned headline words, banned
    centered/symmetric instructions).
    """
    from . import creative_studio as _cs

    _cs._check_headline_hard_rules(headline)

    use_pixels = pixels or (config.STORY_PIXELS if "story" in str(surface).lower()
                            else config.IMAGE_PIXELS)
    use_aspect = aspect or (config.STORY_ASPECT if "story" in str(surface).lower()
                            else config.IMAGE_ASPECT)
    fact_lines = "\n".join(f"- {_cs._scrub_dashes(f)}"
                           for f in (facts or []) if str(f).strip())

    sections = [
        f"You are art directing ONE {kind} for the LASSO brand. Produce a single "
        f"finished image by calling the image generation tool.",
        f"CANVAS: {use_aspect} vertical, {use_pixels}, designed for an Instagram "
        f"and Facebook {surface}. The whole composition fits inside the frame "
        "with generous margins; nothing is cut off at the edges.",
    ]

    voice_excerpt = load_voice_excerpt(voice_path)
    if voice_excerpt:
        sections.append(
            "BRAND VOICE (the approved doc, for tone of the rendered words only; "
            "do not copy sentences out of it onto the card):\n" + voice_excerpt)

    sections.append(palette or _cs.BRAND_PALETTE)
    sections.append(BRAND_TYPE_SYSTEM)
    sections.append(
        "HOOK, the one headline rendered on the card, render it exactly and keep "
        f"it short: {_cs._scrub_dashes(headline)}")
    if str(cta or "").strip():
        sections.append(
            "CTA, the exact words inside the button block: "
            f"{_cs._scrub_dashes(cta)}")
    if fact_lines:
        sections.append(
            "APPROVED CONTEXT for the three element visual metaphor (do NOT "
            "render these sentences as body text on the image; they tell you "
            f"what the three elements should SHOW):\n{fact_lines}")
    sections.extend([
        FLAT_EDITORIAL_SPEC,
        f"URL FOOTER TEXT (render exactly): {footer or url_footer()}",
        SINGLE_ACCENT_LAW,
        READABILITY_LAW,
        _cs.STORY_REQUIREMENT,
        _cs.CLEAR_HEADLINE_LAW,
        _cs.NO_STAT_SLAB_LAW,
        BANNED,
        "NO FABRICATION: render only the hook, the CTA, the labels the approved "
        "context supports, and the URL footer. Invent no number, price, offer, "
        "date, client name, or claim that is not above.",
        _cs.NO_DASH_RULE,
    ])

    brief = _cs._scrub_dashes("\n\n".join(sections))
    _cs._check_prompt_hard_rules(brief)
    return brief
