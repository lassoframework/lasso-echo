"""
Creative studio: Nano Banana infographic generation (Gemini image API).

OFF BY DEFAULT (`config.creative_studio_enabled()`). Two guarantees mirror the
rest of Echo:

  - NO FABRICATION: the prompt is built ONLY from approved input — the headline and
    the facts passed in. Empty facts -> generate NOTHING (return None), the same
    block-on-missing-input contract the drafter uses.
  - NO SECRETS: the API key is read lazily from env, never stored on a returned
    object, never logged. The real client is built only when the flag is on AND a
    key is present.

Publishing is unaffected: this module never posts and never touches the publish
flag. It only draws an image a human then reviews through the normal approval gate.

THE VARIANT SYSTEM (locked). Brand grammar is CONSTANT on every card: one
typography family (bold condensed headlines, two fonts max), the LASSO logo
lockup, red #E03131 as the single accent, and the footer line. What varies is
one CANVAS token and one LAYOUT token per card:

  CANVASES (4): cream (the current light canvas, the default feel), navy
  (deep navy #1A2340, white type, subtle vignette), red (bold red field,
  white and navy type, the highest urgency energy), split (a diagonal or
  vertical navy and cream split with a red rule line).

  LAYOUTS (8): stat_hero (one colossal number fills half the card, support
  line under it), framework (a numbered or stepped list as pills, rails, or a
  simple decision path), contrast (a two zone myth vs fact split with strong
  visual opposition), checklist (outcome rows with check marks), poster
  (headline dominant editorial, the current default look), and the V2
  additions: chart (one clean data visual, big labeled numbers), diagram
  (funnel / hub and spoke / flow arrows in thin line art, labeled nodes),
  device (a phone, browser, or profile grid mockup in thin outline as the
  hero visual). Every layout, one headline above.

Every canvas and layout combination passes the same READABILITY BAR: high
contrast, mobile legible, the headline readable at thumbnail size. Selection:
a concept may declare `layout` and `canvas` (explicit override); otherwise the
canvas hashes deterministically from the concept key (regen_library.canvas_for)
so re-renders are stable, and concepts WITHOUT variant fields render through
the original path byte for byte (zero change to approved cards). The daily
selector never serves the same canvas two days running when an alternative
exists (rotation's canvas guard).
"""

import os
import re

from . import config


# LOCKED LASSO V3 brand palette for infographic styling. The directive tells the model
# HOW to use each color (role + usage), not just lists hexes, so images come out on
# brand (navy/red/sky/cream) instead of navy/gray/white.
BRAND_PALETTE = (
    "LOCKED LASSO V3 BRAND PALETTE (apply these exact colors, do not substitute or "
    "invent others):\n"
    "- Cream #FAF6F0: THE canvas. The card background is always cream / off white with "
    "generous margins and whitespace. NEVER a full bleed solid color slab (never a solid "
    "navy or solid red background).\n"
    "- Navy #121E3C: the headline color and the primary line work / structure of the "
    "illustration. Bold but not shouting.\n"
    "- Sky Blue #5EB9E6: the supporting accent for icons, flow lines, and highlights.\n"
    "- Red #FF0000: the SINGLE accent. Exactly one red element on the card (one arrow "
    "or one emphasized element), never a red background, never red everywhere.\n"
    "Style: clean, flat, modern illustrated infographic on a light cream canvas; "
    "brand-consistent; calm, premium, easy to read."
)

# Composition: a LOCKED house style (consistent every card so the run reads as one brand
# system) while the illustrated SUBJECT varies by pillar. No forced monitor/dashboard.
# The house style is shared by every surface; the LAYOUT block is per surface (the 4:5
# feed card and the 9:16 Story compose differently), selected in build_prompt.
_HOUSE_STYLE_LEAD = (
    "House style, the LASSO brand system (keep this CONSISTENT on every card so the "
    "whole run reads as one brand system): a clean, minimal, modern FLAT infographic on "
    "a cream canvas with generous negative space, uncluttered and premium. ONE navy "
    "headline, bold but not shouting; the headline is the ONLY large text. All artwork "
    "is simple line-icon illustrations with small UPPERCASE labels, drawn with a "
    "consistent stroke weight in the brand palette."
)

# ---- Layout ARCHETYPES: the composition varies, the brand never does. Every card
# gets exactly ONE archetype; each block also sets the secondary knobs (illustration
# scale, label density, where the single red accent lands) so cards differ beyond
# structure. Palette and canvas never vary.
ARCHETYPES = {
    "flow": (
        "Archetype FLOW: the body of the card is an ILLUSTRATED DIAGRAM, a vertical "
        "flow diagram with the headline at the top. Line-icon steps connected by flow "
        "arrows running top to bottom. Illustration scale medium with a few clear "
        "steps; label density normal, one small UPPERCASE label per step; the single "
        "red accent is ONE arrow or ONE step marker."
    ),
    "split": (
        "Archetype SPLIT: a two side contrast, left vs right or top vs bottom, with "
        "the headline at the top. One side muted and faded, one side alive in the "
        "brand palette; the ONE red element marks the winning side only. A small "
        "illustrated diagram vignette on each side; illustration scale medium; label "
        "density low, one or two small UPPERCASE labels per side."
    ),
    "hero": (
        "Archetype HERO: ONE LARGE central illustration filling most of the card, with "
        "the headline at the top and MAXIMUM negative space. The boldest, simplest "
        "read on the feed. Illustration scale large; label density minimal, at most "
        "TWO small UPPERCASE labels on the whole card; the single red accent is one "
        "small element inside the hero illustration."
    ),
    "path": (
        "Archetype PATH: an ILLUSTRATED DIAGRAM of a winding journey path with labeled "
        "stops; the journey begins at the BOTTOM of the card and ends at the TOP, "
        "headline at the top. Illustration scale medium; label density normal, one "
        "small UPPERCASE label per stop; the single red accent marks the FINAL stop only."
    ),
    "headline": (
        "Archetype HEADLINE, typography forward (use sparingly): the headline IS the "
        "hero, LARGE and CENTERED in the MIDDLE of the card, with ONE small line-icon "
        "accent above or below it. No diagram. Label density zero; the single red "
        "accent is one emphasized word in the headline or the small icon, never both."
    ),
}

# Story requirement (Blake's stranger test, applied to EVERY concept and EVERY
# archetype): the benchmark card is follow_up_problem - a stranger reads it with
# no caption. Abstract symbolism (a path labeled GROW/PLAN/LEARN) fails.
STORY_REQUIREMENT = (
    "Story requirement (every card, every archetype): the illustration depicts a "
    "CONCRETE SCENE from a gym owner's world, never abstract symbolism. Allowed "
    "subjects: leads and people, phones and messages, calendars, a gym floor or "
    "front desk, members training, a funnel with people in it, money or growth "
    "outcomes shown through people. Every card shows a TENSION and a RESOLUTION "
    "readable at a glance: a problem state and an outcome state (before and after, "
    "blocked and flowing, empty and full), whatever the archetype. Labels are "
    "MEANINGFUL words a gym owner uses, like LEADS, NO FOLLOW UP, BOOKED, SHOWED, "
    "MEMBERS. Banned generic process labels: STEP 1, STEP 2, STEP 3, PLAN, GROW, "
    "LEARN, DISCOVER, LAUNCH, START, FINISH. The stranger test: someone who has "
    "never heard of LASSO must be able to say what the card is about from the "
    "image alone."
)

# BE CLEAR, NOT CUTE: the copy law on top of the story requirement. Applies to
# every headline on every card, both concept sets.
CLEAR_HEADLINE_LAW = (
    "Headline law, BE CLEAR, NOT CUTE: the headline states plainly what the card "
    "is about or what LASSO does. No slogans, no aphorisms, no wordplay, no clever "
    "compression. If a stranger must decode it, it fails. The two second test: a "
    "gym owner scrolling at speed understands the card's point from the headline "
    "plus the image in two seconds. Headline says it plainly, image shows it "
    "concretely, labels mean something: all three or the card fails."
)

# The order daily generated cards walk through (rotating, never random).
ARCHETYPE_ORDER = ["flow", "hero", "split", "path", "headline"]


def archetype_for_day(day_key):
    """The daily studio's archetype: a deterministic rotation by calendar day, so
    consecutive generated cards differ in composition (variety, not randomness)."""
    from datetime import date as _date
    ordinal = _date.fromisoformat(str(day_key)[:10]).toordinal()
    return ARCHETYPE_ORDER[ordinal % len(ARCHETYPE_ORDER)]


# Feed (4:5) fit: generic portrait guidance; the headline position and body
# structure come from the archetype block.
FEED_LAYOUT = (
    "Portrait fit: lay the whole design out for a TALL vertical frame so it FILLS the "
    "tall portrait canvas. Keep generous margins and make sure nothing is cut off at "
    "the edges."
)

# Story (9:16), FLOW default: a TRUE full screen vertical composition, never a reused
# or stretched feed card. IG overlays its own UI at the top and bottom of a Story, so
# those bands stay empty (safe zones).
STORY_LAYOUT = (
    "Story layout (9:16 FULL SCREEN vertical): compose this as a NEW full screen vertical "
    "design, never a cropped, stretched, or reused feed card. Put the one short headline in "
    "the UPPER THIRD of the frame. Center ONE single focal graphic in the MIDDLE of the "
    "frame, large and clear. Keep the TOP 250 pixels and the BOTTOM 250 pixels of the frame "
    "EMPTY as safe zones (the Instagram Story interface draws its own overlays there): no "
    "text, no icons, no key elements in those bands. Generous margins on every side, calm "
    "vertical balance, and nothing cut off at the edges."
)

# Story recomposition for the non-FLOW archetypes: the SAME archetype rebuilt for
# 9:16 with the same safe zones.
STORY_RECOMPOSE = (
    "Story recomposition (9:16 FULL SCREEN vertical): recompose this SAME archetype as "
    "a NEW full screen vertical design, never a cropped, stretched, or reused feed "
    "card. Keep the TOP 250 pixels and the BOTTOM 250 pixels of the frame EMPTY as "
    "safe zones (the Instagram Story interface draws its own overlays there): no text, "
    "no icons, no key elements in those bands. Calm vertical balance, generous margins "
    "on every side, and nothing cut off at the edges."
)

_HOUSE_STYLE_REST = (
    "Subject varies by pillar: choose simple icons that FIT this card's topic and message. "
    "Do NOT default to a computer, monitor, or dashboard every time; pick the everyday "
    "objects relevant to the subject, rendered in the SAME clean house style and palette. "
    "Avoid a dense collage of many icons and boxes.\n"
    "ONE idea per card. No multi panel text blocks, no stacked slogans, no text only "
    "compositions.\n"
    "Text: render ONLY the one short headline as large text; small UPPERCASE labels on "
    "icons are one or two words at most; do NOT put body sentences, paragraphs, or the "
    "caption on the image. Overall feel: minimal, modern, high end, brand-consistent, "
    "easy to read at a glance. Think one clean composition, not a busy poster."
)


def _composition_style(archetype="flow", is_story=False):
    """House style lead + the archetype's structure + the surface fit + the shared
    rules. The archetype changes the card's STRUCTURE, never its brand."""
    a = (archetype or "flow").lower()
    block = ARCHETYPES.get(a, ARCHETYPES["flow"])
    if is_story:
        surface = STORY_LAYOUT if a == "flow" else STORY_RECOMPOSE
    else:
        surface = FEED_LAYOUT
    return (f"{_HOUSE_STYLE_LEAD}\n{block}\n{STORY_REQUIREMENT}\n"
            f"{CLEAR_HEADLINE_LAW}\n{surface}\n{_HOUSE_STYLE_REST}")


# Kept for compatibility: the default feed composition (FLOW archetype).
COMPOSITION_STYLE = _composition_style("flow", False)

# THE ONE documented exception to the cream house spec: The Full Gym BOOK
# campaign cards mirror the book cover (black canvas, red and white type, the
# cover's red accent squares). Scoped ONLY to book campaign cards: nothing else
# may pass this palette; the house spec is untouched everywhere else.
BOOK_COVER_PALETTE = (
    "BOOK COVER STYLE (The Full Gym campaign only, mirrors the cover art): a "
    "BLACK canvas with red and white type and the cover's red accent squares. "
    "Red #FF0000 and white are the only type colors; bold, high contrast, "
    "minimal. This card intentionally does not use the cream house canvas."
)

# ---- THE VARIANT SYSTEM: locked canvas + layout tokens (see module docstring) ----
VARIANT_GRAMMAR = (
    "LOCKED BRAND GRAMMAR (constant on every card, never varies with canvas or "
    "layout): ONE typography family only, bold condensed headlines, two fonts "
    "maximum on the whole card. The LASSO logo lockup present once, small and "
    "consistent. Red #E03131 is THE accent color: exactly one red element or "
    "emphasis, never red everywhere. Footer line, small and letterspaced: "
    "LASSOFRAMEWORK.COM."
)

CANVASES = {
    "cream": (
        "Canvas token CREAM: the light house canvas. Cream #FAF6F0 background "
        "with generous margins and whitespace, navy #121E3C type and line work, "
        "sky blue #5EB9E6 supporting touches. Calm, premium, the default feel."
    ),
    "navy": (
        "Canvas token NAVY: deep navy #1A2340 field with a subtle dark "
        "vignette. White type, red #E03131 accents. Moody, cinematic, premium."
    ),
    "red": (
        "Canvas token RED: a bold red field. White and navy #1A2340 type only. "
        "The highest urgency energy in the system; keep the composition simple "
        "so the energy reads clean, never chaotic."
    ),
    "split": (
        "Canvas token SPLIT: a diagonal or vertical split, one zone deep navy "
        "#1A2340 and one zone cream #FAF6F0, divided by a thin red #E03131 "
        "rule line. Type flips color per zone: white on the navy zone, navy on "
        "the cream zone."
    ),
}

LAYOUTS = {
    "framework": (
        "Layout token FRAMEWORK: the content renders as a clean visual system "
        "for an ordered list: numbered pills, a vertical rail with stops, or a "
        "simple decision path. Each step short, one small label per step, "
        "clear top to bottom order."
    ),
    "contrast": (
        "Layout token CONTRAST: two zones in strong visual opposition, myth vs "
        "fact or problem vs fix. One zone muted and faded, one zone alive; the "
        "single red accent marks the winning zone only."
    ),
    "checklist": (
        "Layout token CHECKLIST: outcome rows, each with a check mark, for "
        "future state and benefit lists. Rows short and parallel; the check "
        "marks are the repeating graphic element."
    ),
    "poster": (
        "Layout token POSTER: headline dominant editorial card, the current "
        "default look. The headline is the hero with maximum negative space "
        "and at most one supporting graphic element."
    ),
    # ---- grammar V2 layouts: same brand grammar, same readability bar ----
    "chart": (
        "Layout token CHART: a single clean data visual dominates the card, "
        "comparison bars or a trend line, with BIG labeled numbers and the "
        "one headline above it. No gridlines clutter; the data shape itself "
        "is the graphic."
    ),
    "diagram": (
        "Layout token DIAGRAM: a funnel, hub and spoke, or flow of arrows "
        "drawn in thin line art with clearly labeled nodes, the one headline "
        "above it. Few nodes, short labels, one obvious reading order."
    ),
    "device": (
        "Layout token DEVICE: a phone, browser window, or profile grid "
        "mockup drawn in thin outline is the hero visual, the one headline "
        "above it. The mockup content is simple and readable; nothing "
        "outside the device frame competes with it."
    ),
}

READABILITY_BAR = (
    "READABILITY BAR (every canvas and layout combination, no exceptions): "
    "high contrast between type and field, mobile legible, and the headline "
    "readable at THUMBNAIL size in a feed. If a combination would compromise "
    "any of these, simplify the composition, never the contrast."
)

CANVAS_ORDER = ["cream", "navy", "red", "split"]

# RETIRED LAYOUTS: the giant-number-on-navy STAT SLAB is off brand and gimmicky
# (Blake, 2026-07-16). It is removed from the system; any concept that still
# names it remaps to CHART (a clean data visual with labeled numbers, never a
# colossal single figure) so no card renders a slab and no legacy concept crashes.
_RETIRED_LAYOUTS = {"stat_hero": "chart"}

# The copy law that retires the slab for good, applied to every card: no single
# giant number as the hero. Numbers live inside a real data visual or a sentence.
NO_STAT_SLAB_LAW = (
    "No stat slab: NEVER render one colossal number or percentage as the hero "
    "element filling the card. That layout is retired. Any figure appears only "
    "inside a real data visual (labeled bars or a trend) or within the headline "
    "sentence, at readable size, never as an oversized standalone slab."
)


def variant_block(canvas, layout):
    """The composed variant directive for one card: locked grammar + the one
    canvas token + the one layout token + the readability bar + the no-slab law.
    A retired layout remaps (loudly) instead of rendering off system; an unknown
    token still raises (a typo must never silently render off system)."""
    if layout in _RETIRED_LAYOUTS:
        remap = _RETIRED_LAYOUTS[layout]
        print(f"[creative-studio] layout '{layout}' is retired (stat slab); "
              f"rendering as '{remap}' in the house style instead.")
        layout = remap
    if canvas not in CANVASES:
        raise ValueError(f"unknown canvas: {canvas} ({', '.join(CANVAS_ORDER)})")
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout: {layout} ({', '.join(sorted(LAYOUTS))})")
    return (f"{VARIANT_GRAMMAR}\n{CANVASES[canvas]}\n{LAYOUTS[layout]}\n"
            f"{READABILITY_BAR}\n{NO_STAT_SLAB_LAW}")


# Copy mechanics from the brand bible: rendered copy carries no dashes.
NO_DASH_RULE = (
    "Copy mechanics: no em dashes, no en dashes, avoid hyphens in the rendered "
    "text. Use the word 'to' for ranges."
)


def _scrub_dashes(text):
    """Strip em/en dashes from approved copy (the brand no-dash rule). Hyphens and
    newlines are left intact; only the doubled spaces a removed dash leaves are
    collapsed."""
    if not text:
        return ""
    cleaned = str(text).replace("—", " ").replace("–", " ")
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def build_prompt(headline, facts, aspect=None, pixels=None, surface=None,
                 archetype=None, palette=None, canvas=None, layout=None):
    """
    Build the image prompt from APPROVED input only. The single on-image headline is
    the approved pillar hook (kept short); the approved body lines are passed as CONCEPT
    CONTEXT for the focal graphic and are NOT rendered as text on the image (the caption
    carries the words). Plus the brand palette, composition style, and no-dash rule.
    Dashes in the approved text are scrubbed. Nothing is invented.

    Aspect is PER USE, not a global switch: the defaults are the feed target
    (config.IMAGE_ASPECT / IMAGE_PIXELS, 4:5); a caller like Stories passes its own
    aspect (9:16) and surface label without changing the feed's target.

    `archetype` selects one of the LAYOUT ARCHETYPES (flow, split, hero, path,
    headline; default flow, the original look). The archetype varies the card's
    STRUCTURE; the brand system (canvas, palette, line-icon language) never varies.
    A Story surface recomposes the SAME archetype for 9:16 with the safe zones.

    `canvas` + `layout` select the LOCKED VARIANT SYSTEM instead (module
    docstring): the variant block replaces the archetype composition and the
    palette for this one card. Both None (the default everywhere) = the
    original path, byte for byte.
    """
    fact_lines = "\n".join(f"- {_scrub_dashes(f)}" for f in facts if str(f).strip())
    # Aspect first and prominent. Config-tunable; per-use overridable.
    use_aspect = aspect or config.IMAGE_ASPECT
    use_pixels = pixels or config.IMAGE_PIXELS
    use_surface = surface or "feed post"
    is_story = "story" in use_surface.lower()
    if canvas is not None or layout is not None:
        # the variant system: grammar + canvas + layout + readability bar
        # carry the whole brand directive (the canvas token IS the palette);
        # the story requirement and headline law still ride (they are copy and
        # story rules, not palette rules)
        composition = (f"{variant_block(canvas or 'cream', layout or 'poster')}\n"
                       f"{STORY_REQUIREMENT}\n{CLEAR_HEADLINE_LAW}")
        style_tail = composition
    else:
        composition = _composition_style(archetype, is_story)
        style_tail = f"{composition}\n{palette or BRAND_PALETTE}\n{NO_STAT_SLAB_LAW}"
    aspect = (
        f"Canvas: a VERTICAL {use_aspect} PORTRAIT ({use_pixels}, taller "
        f"than wide), designed for an Instagram and Facebook {use_surface}. Fit the entire "
        "composition inside this tall portrait frame with generous margins; nothing is cut "
        "off at the edges."
    )
    prompt = (
        f"{aspect}\n"
        "Design a clean, minimal, premium LASSO-branded infographic.\n"
        f"Headline (the ONLY text to render on the image, keep it short): "
        f"{_scrub_dashes(headline)}\n"
        "Concept context for the single focal graphic (do NOT render this text on the "
        "image; the caption carries the words):\n"
        f"{fact_lines}\n"
        f"{style_tail}\n"
        f"{NO_DASH_RULE}"
    )
    return _scrub_dashes(prompt)


# ---- Social proof cards: two templates in the locked V3 house style -----------
# Quote card: the verified quote IS the one short line; attribution small.
QUOTE_CARD_STYLE = (
    "Template: SOCIAL PROOF QUOTE CARD in the locked house style. The quote is the "
    "ONE short line of text, set large and centered with generous negative space; "
    "the attribution is rendered SMALL beneath it. No body sentences, no paragraphs, "
    "no other text, no icons competing with the quote. Subtle oversized quotation "
    "marks are allowed as a graphic element. Cream canvas (never a solid color slab), "
    "navy text and line work, sky blue for one supporting graphic touch, red for "
    "exactly ONE focal accent (a single underline or one emphasized word). Minimal, "
    "modern, premium."
)

# Number card: the verified stat sits INSIDE a clean composition, never a slab.
# The stat-slab (one colossal number as the hero) is retired brand-wide; a proof
# stat reads as one clear line supported by a small line-icon visual.
NUMBER_CARD_STYLE = (
    "Template: SOCIAL PROOF STAT CARD in the locked house style. The verified stat "
    "reads as ONE clear line at readable headline size (never one oversized slab "
    "number filling the card), with ONE short support line and the attribution SMALL at the "
    "bottom, beside a single simple line-icon graphic that fits the claim. No body "
    "sentences, no paragraphs, no dense graphics. Cream canvas (never a solid color "
    "slab), navy text and line work, sky blue for one supporting graphic touch, red "
    "for exactly ONE focal accent. Minimal, modern, premium. Do NOT render the number "
    "as an oversized standalone stat slab; the stat-slab layout is retired."
)


def build_social_proof_prompt(kind, main_line, support_line="", attribution="",
                              aspect=None, pixels=None, surface=None):
    """
    Build a social proof card prompt from a VERIFIED, PERMISSIONED entry only (the
    caller enforces permission + verification; nothing here invents text). kind is
    "quote" or "stat". Aspect is per use exactly like build_prompt: default is the
    4:5 feed target; a Story variant passes 9:16 + a story surface label.
    """
    use_aspect = aspect or config.IMAGE_ASPECT
    use_pixels = pixels or config.IMAGE_PIXELS
    use_surface = surface or "feed post"
    is_story = "story" in use_surface.lower()
    template = NUMBER_CARD_STYLE if kind == "stat" else QUOTE_CARD_STYLE
    canvas = (
        f"Canvas: a VERTICAL {use_aspect} PORTRAIT ({use_pixels}, taller "
        f"than wide), designed for an Instagram and Facebook {use_surface}. Fit the "
        "entire composition inside this tall portrait frame with generous margins; "
        "nothing is cut off at the edges."
        + (" Keep empty safe zones at the very top and bottom of the frame."
           if is_story else "")
    )
    lines = [
        canvas,
        "Design a clean, minimal, premium LASSO-branded social proof card.",
        f"Main line (render exactly this text): {_scrub_dashes(main_line)}",
    ]
    if kind == "stat" and support_line:
        lines.append(f"Support line (render small, beneath the stat): {_scrub_dashes(support_line)}")
    if attribution:
        lines.append(f"Attribution (render SMALL): {_scrub_dashes(attribution)}")
    lines.extend([template, BRAND_PALETTE, NO_STAT_SLAB_LAW, NO_DASH_RULE])
    return _scrub_dashes("\n".join(lines))


def generate_social_proof(kind, main_line, support_line="", attribution="",
                          client=None, out_path=None,
                          aspect=None, pixels=None, surface=None):
    """
    Generate a social proof card. Same gates as generate(): flag OFF -> None with no
    API call; an empty main line -> None (nothing to render honestly); no client and
    no key -> None. Returns {"path", "prompt"} on success.
    """
    if not config.creative_studio_enabled():
        return None
    if not str(main_line or "").strip():
        return None

    prompt = build_social_proof_prompt(kind, main_line, support_line, attribution,
                                       aspect=aspect, pixels=pixels, surface=surface)
    client = client or _default_client()
    if client is None:
        return None

    image_bytes = client.generate_image(prompt=prompt, model=config.NANO_MODEL)

    if out_path is None:
        slug = re.sub(r"[^a-z0-9]+", "_", str(main_line).lower()).strip("_")[:60] or "proof"
        out_path = os.path.join(config.LIBRARY_PATH, f"nano_proof_{slug}.png")
    with open(out_path, "wb") as fh:
        fh.write(image_bytes)

    return {"path": out_path, "prompt": prompt}


class _GeminiImageClient:
    """
    Thin wrapper over the Gemini image API. Constructed only with a present key;
    the key is held for the call and never logged, printed, or returned.
    """

    def __init__(self, api_key, genai_client=None):
        self._api_key = api_key          # never logged or exposed
        self._genai_client = genai_client  # injection seam for tests; None in production

    def generate_image(self, prompt, model):
        # The Gemini image models (gemini-3-pro-image, gemini-2.5-flash-image, ...)
        # support generateContent, NOT predict/generate_images. Lazy import so
        # flag-off / draft-only never needs the SDK installed.
        client = self._genai_client
        if client is None:
            from google import genai  # type: ignore
            from google.genai import types  # noqa: F401

            client = genai.Client(api_key=self._api_key)
        resp = client.models.generate_content(model=model, contents=prompt)
        # The image comes back as inline data on a response part. Return the first one.
        for part in resp.candidates[0].content.parts:
            if getattr(part, "inline_data", None):
                return part.inline_data.data  # raw image bytes
        raise ValueError("no image returned from Gemini")


def _default_client():
    """
    Build the real Nano Banana (Gemini image) client, but ONLY when the flag is on
    AND a key is present. Returns None otherwise so generate() no-ops safely. The
    key is read here lazily and never logged or returned.
    """
    if not config.creative_studio_enabled():
        return None
    key = os.environ.get(config.NANO_API_KEY_ENV)
    if not key:
        return None
    return _GeminiImageClient(key)


def spend_allowed(account_key=None, day=None):
    """One shared Gemini spend gate. True bumps the counter and allows the
    call; False means the day's cap for this bucket is spent (one ops alert
    per bucket per day). Buckets: per account when account_key is given —
    one client's volume never starves another — else the global bucket for
    account-less work (DAM autotag, library regen). Flag OFF allows all."""
    if not config.spend_cap_enabled():
        return True
    from datetime import date as _date
    from . import db as _db, ops_alerts as _ops
    day = day or _date.today().isoformat()
    bucket = f"gemini_calls:{account_key}" if account_key else "gemini_calls"
    cap = int(os.environ.get("AGENT_GEMINI_DAILY_CAP", "40"))
    if _db.counter_get(bucket, day) >= cap:
        alert_key = (f"spend_cap_alerted_{account_key}_{day}" if account_key
                     else f"spend_cap_alerted_{day}")
        if _db.kv_get(alert_key) != "1":
            _db.kv_set(alert_key, "1")
            who = f"account {account_key}" if account_key else "the shared pool"
            _ops.alert(f"Gemini daily cap reached for {who} ({cap} calls). "
                       "Generation paused for today; library-only selection "
                       "takes over.")
        return False
    _db.counter_bump(bucket, day)
    return True


def generate(headline, facts, client=None, out_path=None,
             aspect=None, pixels=None, surface=None, archetype=None,
             palette=None, canvas=None, layout=None, account_key=None):
    """
    Generate a LASSO infographic from APPROVED input. Returns {"path", "prompt"} on
    success, or None when it must not run:
      - the flag is OFF (creative_studio_enabled() is False) -> None, no API call
      - facts is empty (the no-fabrication gate)             -> None, no API call
      - no client and no key available                       -> None, no API call

    aspect/pixels/surface/archetype are per-use overrides (see build_prompt): the
    feed keeps its 4:5 default; a Story passes 9:16 for its own call only; the
    archetype varies the composition inside the locked brand (default flow).
    """
    if not config.creative_studio_enabled():
        return None
    if not facts:
        return None

    # Gemini spend cap (AGENT_SPEND_CAP_ENABLED, default OFF): at the daily cap
    # generation stops for the day (returning None makes every caller fall back
    # to library-only selection) with ONE ops alert. Counter resets by date key.
    # The cap is PER ACCOUNT when the caller passes account_key, so one client's
    # volume can never starve another client's creative; account-less calls
    # (DAM autotag, library regen) share the global bucket.
    if not spend_allowed(account_key=account_key):
        return None

    prompt = build_prompt(headline, facts, aspect=aspect, pixels=pixels,
                          surface=surface, archetype=archetype, palette=palette,
                          canvas=canvas, layout=layout)

    client = client or _default_client()
    if client is None:
        return None

    image_bytes = client.generate_image(prompt=prompt, model=config.NANO_MODEL)

    if out_path is None:
        slug = re.sub(r"[^a-z0-9]+", "_", (headline or "infographic").lower()).strip("_") or "infographic"
        out_path = os.path.join(config.LIBRARY_PATH, f"nano_{slug}.png")
    with open(out_path, "wb") as fh:
        fh.write(image_bytes)

    return {"path": out_path, "prompt": prompt}
