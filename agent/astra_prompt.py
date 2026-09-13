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

# ---------------------------------------------------------------------------
# THE STYLE FREEDOM SYSTEM (AGENT_ASTRA_STYLE_FREEDOM, default OFF)
#
# Why this exists (Blake, 2026-09-13): every Astra card looked the same, because
# every Astra card WAS the same. The brief hardcoded one palette that declared
# cream "THE canvas", and one composition (FLAT_EDITORIAL_SPEC) that demanded
# exactly three vector elements plus a CTA button that was always the single red
# element. Same field, same furniture, same accent, every card. Sameness by
# construction, not by model behavior.
#
# The fix is range, not chaos. Each card draws one CANVAS, one COMPOSITION, and
# one ACCENT placement. What still never varies: the LASSO V3 colors, two type
# families, no dashes, no fabrication, the readability bar, and the human
# approval gate. Brand stays locked. The look breathes.
# ---------------------------------------------------------------------------

# CANVAS MODES. All seven are built from the locked LASSO V3 colors only; this
# widens which color carries the FIELD, it does not add colors to the brand.
# (The open "brand palette" decision in PROGRESS.md stays open and untouched.)
CANVAS_MODES = {
    "cream": (
        "CANVAS MODE CREAM: the light house field. Cream #FAF6F0 background, "
        "generous margins, navy #121E3C type and line work, sky blue #5EB9E6 "
        "supporting touches. Calm, premium, editorial."
    ),
    "navy": (
        "CANVAS MODE NAVY: a deep navy #121E3C field filling the frame, with a "
        "subtle darker vignette. Cream and white type, sky blue #5EB9E6 line "
        "work. Moody, cinematic, confident."
    ),
    "red": (
        "CANVAS MODE RED: a bold red #FF0000 field filling the frame. White and "
        "navy #121E3C type only. The highest urgency energy in the system: keep "
        "the composition simple and the type huge so it reads clean, never busy."
    ),
    "split": (
        "CANVAS MODE SPLIT: the frame divides into two zones, one deep navy "
        "#121E3C and one cream #FAF6F0, on a vertical, horizontal, or diagonal "
        "seam. Type flips per zone: cream type on the navy zone, navy type on "
        "the cream zone. The seam itself is a designed edge, not an accident."
    ),
    "sky": (
        "CANVAS MODE SKY: a sky blue #5EB9E6 field with navy #121E3C type and "
        "cream #FAF6F0 supporting shapes. Bright, open, the friendliest field "
        "in the system. Reserve it for outcome and momentum ideas."
    ),
    "ink": (
        "CANVAS MODE INK: a near black #0B1020 field with cream #FAF6F0 type. "
        "The most serious, highest contrast field. Line work is thin and precise; "
        "negative space does the work."
    ),
    "duotone": (
        "CANVAS MODE DUOTONE: one full bleed photograph treated as a two tone "
        "duotone in navy #121E3C and cream #FAF6F0, with the type reversed out "
        "over it. The photo is a real gym or workplace scene, high contrast, "
        "never a stock smile and never an illustrated or AI looking image. Type "
        "sits in the calmest area of the frame so it stays legible."
    ),
}

CANVAS_ORDER = ["cream", "navy", "split", "sky", "ink", "red", "duotone"]

# Variety is the goal, not a loud grid. An unweighted pick put the RED field on
# roughly a quarter of a month, which reads as shouting on a B2B credibility
# grid. The calm editorial fields carry the month; the loud ones are punctuation.
# Weights are pick frequency, not importance: cream and navy about 20% each,
# red about 7%.
_CANVAS_WEIGHTS = {
    "cream": 3, "navy": 3, "split": 2, "sky": 2, "ink": 2, "duotone": 2, "red": 1,
}
CANVAS_POOL = [c for c in CANVAS_ORDER for _ in range(_CANVAS_WEIGHTS[c])]

# COMPOSITION MODES. The old FLAT_EDITORIAL_SPEC is kept as one of seven, not
# as the law. Each mode changes the card's STRUCTURE and its furniture.
# The freedom-mode cut of the Full Gym standard. Identical to FLAT_EDITORIAL_SPEC
# except block 3 no longer declares the button "the ONE red element": under the
# freedom system the accent may land elsewhere, and two rules claiming the same
# accent is how a brief contradicts itself. The locked-mode constant is untouched.
_FLAT_EDITORIAL_FREE = FLAT_EDITORIAL_SPEC.replace(
    "styled like a real interface button. This is the ONE red element on the "
    "card.",
    "styled like a real interface button. Its color follows the COLOR LAW below, "
    "which decides where this card's single accent lands.",
)

COMPOSITION_MODES = {
    "flat_editorial": _FLAT_EDITORIAL_FREE,
    "type_poster": (
        "COMPOSITION TYPE POSTER: type carries the entire card. No diagram, no "
        "icon set, no button. The headline is set enormous, filling most of the "
        "frame, broken over two to four lines with tight leading, so the words "
        "themselves are the graphic. One small eyebrow label above it and one "
        "quiet deck line below it. Think a magazine cover or a Nike campaign "
        "card, never a book interior. Empty space is allowed here and only here, "
        "because the type is doing the work."
    ),
    "data_story": (
        "COMPOSITION DATA STORY: one clean data visual is the hero, sized large "
        "in the lower two thirds: labeled comparison bars, a simple trend line, "
        "or a two column before and after table. Real business labels and real "
        "readable figures, no gridline clutter, no legend. The headline sits "
        "above it. The shape of the data is the graphic."
    ),
    "diagram": (
        "COMPOSITION DIAGRAM: a funnel, a flow, or a hub and spoke drawn in thin "
        "precise line art with three to five clearly labeled nodes and one "
        "obvious reading order. Short UPPERCASE business labels only. The "
        "headline sits above it. No generic process words."
    ),
    "split_screen": (
        "COMPOSITION SPLIT SCREEN: the frame divides into two zones in strong "
        "visual opposition, before against after or broken against fixed. One "
        "zone is muted and low contrast, one zone is alive in full brand color. "
        "Each zone carries one short label. The divide is hard and deliberate."
    ),
    "stack": (
        "COMPOSITION STACK: three to five full width horizontal rows stacked down "
        "the frame, each row one short line of type with a small mark at its "
        "left edge. The rows are the graphic: even rhythm, even weight, one row "
        "emphasized. The headline sits above the stack."
    ),
    "device": (
        "COMPOSITION DEVICE: a phone screen, a browser window, or a dashboard "
        "panel drawn in thin outline is the hero visual, tilted or cropped by "
        "the frame edge rather than sitting flat and centered. Its contents are "
        "simple and readable. The headline sits above or beside it."
    ),
}

COMPOSITION_ORDER = [
    "flat_editorial", "type_poster", "data_story", "diagram",
    "split_screen", "stack", "device",
]

# Compositions that carry no CTA button block. On these, the accent cannot be
# the button, so the accent picker skips the button placement.
_NO_BUTTON_MODES = {"type_poster", "data_story", "diagram", "split_screen", "stack", "device"}

# ACCENT PLACEMENTS. Exactly one accent element stays the law; WHERE it lands
# is now free. That single rule is what keeps a run of varied cards reading as
# one brand.
ACCENT_PLACEMENTS = {
    "cta_button": "the CTA button block",
    "one_word": "one single emphasized word inside the headline",
    "rule_line": "one short horizontal rule under the eyebrow label",
    "one_node": "one node, bar, or row in the visual element",
    "arrow_tip": "one arrow head or one pointer in the visual element",
    "corner_block": "one small solid block anchored in one corner of the frame",
}

ACCENT_ORDER = ["one_word", "one_node", "rule_line", "arrow_tip",
                "cta_button", "corner_block"]

# Fields where red IS the canvas. The single accent flips to white so the card
# never fights itself, and the "exactly one" discipline still holds.
_RED_FIELD_CANVASES = {"red"}


def accent_law(canvas: str, placement: str) -> str:
    """The single accent law for one card, aware of its canvas and placement.

    Carries the tokens the house-style grade gate reads ("exactly one" + "red"),
    so a freed card is still graded by the same Q3 heuristic as a locked one.
    """
    where = ACCENT_PLACEMENTS.get(placement, ACCENT_PLACEMENTS["one_word"])
    if canvas in _RED_FIELD_CANVASES:
        return (
            "COLOR LAW: this card's field is already red, so red is not used a "
            "second time anywhere on it. Exactly one element is picked out in "
            f"pure white as the accent, and it is {where}. Never a second accent "
            "color, never scattered emphasis."
        )
    return (
        "COLOR LAW: red #FF0000 is used exactly one time on this card, and it is "
        f"{where}. Never a red field behind the whole card, never red type "
        "scattered through the card, never a second red element."
    )


# The latitude clause. Without this the model defaults to the safest, most
# generic reading of the brief, which is exactly how a run of cards converges
# on one look. It names "visual anchor" deliberately: the Q6 gate reads for it.
ART_DIRECTION_LATITUDE = (
    "ART DIRECTION LATITUDE (read this before you compose): you are art "
    "directing ONE card in a long running brand campaign, not filling in a "
    "template. Within the canvas, composition, and color law above, you decide "
    "the crop, the scale relationships, where the visual anchor sits, how much "
    "negative space each zone gets, and whether the type sits high, low, or "
    "against the edge. Push the scale contrast further than feels safe. Vary "
    "the composition from the obvious centered arrangement: anchor to an edge, "
    "let one element bleed off the frame, or drop the horizon high or low. "
    "Two cards from this brand should be recognizable as siblings, never as "
    "the same card with the words swapped."
)


def _pick(key: str, order: list, salt: str) -> str:
    """Deterministic choice from `order` for this card key.

    Same key plus same salt always returns the same token, so a re-render of an
    approved card is stable (the same contract regen_library.canvas_for keeps).
    The salt decorrelates the three picks so canvas, composition, and accent do
    not move in lockstep and reproduce a new, smaller set of repeats.
    """
    import hashlib
    digest = hashlib.sha256(f"{salt}:{key}".encode("utf-8")).hexdigest()[:8]
    return order[int(digest, 16) % len(order)]


def style_for(key: str, canvas=None, composition=None, accent=None) -> dict:
    """The full style selection for one card: {canvas, composition, accent}.

    `key` is any stable per card string (the headline is what the render path
    passes). Explicit arguments override the deterministic pick, so a concept
    can still pin its own look. An unknown token raises rather than silently
    rendering off system.
    """
    use_canvas = canvas or _pick(key, CANVAS_POOL, "canvas")
    use_comp = composition or _pick(key, COMPOSITION_ORDER, "composition")
    if use_canvas not in CANVAS_MODES:
        raise ValueError(
            f"unknown canvas mode: {use_canvas} ({', '.join(CANVAS_ORDER)})")
    if use_comp not in COMPOSITION_MODES:
        raise ValueError(
            f"unknown composition mode: {use_comp} ({', '.join(COMPOSITION_ORDER)})")
    if accent:
        use_accent = accent
    else:
        pool = [a for a in ACCENT_ORDER
                if not (a == "cta_button" and use_comp in _NO_BUTTON_MODES)]
        use_accent = _pick(key, pool, "accent")
    if use_accent not in ACCENT_PLACEMENTS:
        raise ValueError(
            f"unknown accent placement: {use_accent} "
            f"({', '.join(ACCENT_ORDER)})")
    return {"canvas": use_canvas, "composition": use_comp, "accent": use_accent}


# The colors themselves, stated WITHOUT declaring which one is the field. This
# is the freedom-mode replacement for creative_studio.BRAND_PALETTE, which opens
# with "Cream #FAF6F0: THE canvas ... the card background is always cream". Same
# five colors, same brand; the field is now the canvas mode's call.
LOCKED_BRAND_COLORS = (
    "LOCKED LASSO V3 COLORS (use these exact values, never substitute or invent "
    "a color; the CANVAS MODE below decides which one carries the field):\n"
    "- Cream #FAF6F0\n"
    "- Navy #121E3C\n"
    "- Sky Blue #5EB9E6\n"
    "- Red #FF0000\n"
    "- White #FFFFFF\n"
    "Quality bar on every field: premium B2B agency work, clean and deliberate. "
    "No gradients, no texture, no pattern fills, no gimmicks."
)


def banned_for(canvas: str) -> str:
    """The BANNED list for one card.

    Identical to BANNED on every canvas but DUOTONE, whose whole premise is a
    real photograph. There the photography ban is lifted and replaced with a
    tighter rule, because a blanket "no photography" line would cancel the mode.
    Everything else stays banned on every card, every mode.
    """
    if canvas != "duotone":
        return BANNED
    return (
        "BANNED, never render any of these: illustrated scenes, cartoon "
        "elements, metaphorical props (dripping money, safes, broken pipes, "
        "locks), 3D effects, cluttered collages, watermarks, lorem ipsum, and "
        "generic process labels (STEP 1, PLAN, GROW, LEARN, DISCOVER, LAUNCH). "
        "Labels use real business language a gym owner recognizes: LEADS, "
        "BOOKED, SHOWED, CLOSED, FOLLOW UP, AD SPEND, SIGNUPS.\n"
        "PHOTOGRAPHY on this card: one real, candid, documentary frame, duotoned "
        "to the brand. Never a stock posed smile, never a composite, never an AI "
        "looking render, never a competitive athlete or a barbell hero shot. The "
        "subject is an ordinary gym owner or a real gym space."
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
                            palette=None, footer=None, kind="infographic",
                            style_key=None, canvas=None, composition=None,
                            accent=None, freedom=None, account_key=None):
    """Build the Astra creative brief from APPROVED input ONLY.

    `headline` is the one hook rendered on the card. `facts` are the approved
    body lines: they steer the visual element and are NOT rendered as sentences
    on the image. `cta` is the approved call to action. Nothing is invented here.

    STYLE FREEDOM (AGENT_ASTRA_STYLE_FREEDOM, default OFF). When the flag is OFF
    this returns the original single template brief byte for byte: cream canvas,
    the four Full Gym blocks, red always the CTA button. When it is ON the card
    draws one CANVAS mode, one COMPOSITION mode, and one ACCENT placement (see
    style_for), chosen deterministically from `style_key` (default: the headline)
    so a re-render of an approved card is stable. `canvas`, `composition` and
    `accent` pin any of the three by hand; `freedom` overrides the flag, which is
    what the tests and a one off render use.

    ACCOUNT SCOPE (Blake, 2026-09-13: "only for LASSO right now until a
    proven [out]"). When `freedom` is not passed explicitly, the flag is
    resolved PER ACCOUNT via config.astra_style_freedom_enabled_for(account_key)
    rather than the bare master switch, so the flag being ON in production
    does not by itself free every client gym's cards. `account_key` is the
    same value every caller already threads through creative_studio.generate();
    a missing one is treated as LASSO (see that function's docstring).

    Returns the brief string, dash-scrubbed and checked against the same hard
    rules the Gemini prompt is checked against (banned headline words, and, in
    locked mode, banned centered/symmetric instructions).
    """
    from . import creative_studio as _cs

    _cs._check_headline_hard_rules(headline)

    use_pixels = pixels or (config.STORY_PIXELS if "story" in str(surface).lower()
                            else config.IMAGE_PIXELS)
    use_aspect = aspect or (config.STORY_ASPECT if "story" in str(surface).lower()
                            else config.IMAGE_ASPECT)
    fact_lines = "\n".join(f"- {_cs._scrub_dashes(f)}"
                           for f in (facts or []) if str(f).strip())

    use_freedom = (config.astra_style_freedom_enabled_for(account_key)
                   if freedom is None else bool(freedom))
    style = None
    if use_freedom:
        style = style_for(str(style_key or headline or ""), canvas=canvas,
                          composition=composition, accent=accent)

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

    if style:
        # The canvas mode IS this card's field instruction, so it replaces the
        # cream-locked BRAND_PALETTE. The color VALUES are unchanged: the LASSO
        # V3 palette still governs, only which color carries the field varies.
        sections.append(LOCKED_BRAND_COLORS)
        sections.append(CANVAS_MODES[style["canvas"]])
    else:
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
        what_shows = ("the visual element" if style
                      else "the three element visual metaphor")
        how_many = ("it" if style else "the three elements")
        sections.append(
            f"APPROVED CONTEXT for {what_shows} (do NOT render these sentences "
            "as body text on the image; they tell you what "
            f"{how_many} should SHOW):\n{fact_lines}")
    sections.extend([
        COMPOSITION_MODES[style["composition"]] if style else FLAT_EDITORIAL_SPEC,
        f"URL FOOTER TEXT (render exactly): {footer or url_footer()}",
        accent_law(style["canvas"], style["accent"]) if style else SINGLE_ACCENT_LAW,
        ART_DIRECTION_LATITUDE if style else READABILITY_LAW,
        READABILITY_LAW if style else "",
        _cs.STORY_REQUIREMENT,
        _cs.CLEAR_HEADLINE_LAW,
        _cs.NO_STAT_SLAB_LAW,
        banned_for(style["canvas"]) if style else BANNED,
        "NO FABRICATION: render only the hook, the CTA, the labels the approved "
        "context supports, and the URL footer. Invent no number, price, offer, "
        "date, client name, or claim that is not above.",
        _cs.NO_DASH_RULE,
    ])

    brief = _cs._scrub_dashes(
        "\n\n".join(sec for sec in sections if str(sec).strip()))
    _cs._check_prompt_hard_rules(brief)
    return brief
