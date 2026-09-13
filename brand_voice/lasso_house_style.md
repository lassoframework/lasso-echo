# LASSO House Style System

Source of truth for every infographic Echo generates.
Version: 1.3 (2026-09-13)

**v1.2 changed the default.** Cream is no longer THE canvas, it is one of seven.
Astra now draws a canvas, a composition, and an accent placement per card instead
of rendering one template every time. See Section 12. Everything the brand is
built on stays locked: the colors, two type families, no dashes, no fabrication,
the readability bar, and the human approval gate.

**v1.3 lifts the flat-only mandate for any FREED card.** Blake graded 5 real
Astra renders a C against a reference set of his own and diagnosed why: his
reference used film grain, glow, drop shadow, real photography, and a
consistent LASSO masthead, every one of which the old spec banned outright.
His ruling: "I want it to have the freedom to do this level without rules."
Texture, depth, and photography are now permitted tools on any freed card; the
LASSO avatar rule (never a competitive athlete) is the one line from the old
ban that survives, because it protects who LASSO markets to, not a technique.
LASSO's own account additionally gets a required masthead + byline lockup so
a run of cards reads as one series. See Section 12A.

All constants in `agent/creative_studio.py` that begin with `HOUSE_STYLE_`
or reference this document must match the scaffold in Section 8 exactly.
When this document changes, update those constants. Never the reverse.

---

## 1. Purpose

Every generated card in the Echo pipeline MUST follow this system.
The creative studio builds its generation prompts from Section 8 of this doc.
The grade gate in Section 9 determines whether a card enters the approval queue.

---

## 2. Brand DNA (constant, never varies)

Colors (LASSO V3, locked). Which color carries the FIELD varies per card (Section 12);
the values themselves never do:

| Token    | Hex       | Role                                               |
|----------|-----------|----------------------------------------------------|
| Cream    | #FAF6F0   | Canvas field, or type on a dark field              |
| Navy     | #121E3C   | Headlines, structure, dark canvas base             |
| Sky Blue | #5EB9E6   | Secondary accents, flow lines, supporting touches  |
| Red      | #FF0000   | THE single accent. One element only. Never two. May carry the whole field only in CANVAS MODE RED, where the single accent flips to white. |

Typography: headlines set bold and large. Eyebrow and deck are smaller.
Maximum two typefaces on any card. No slab serif. No script.

Logo: LASSO lockup appears once, small, letterspaced. Footer: LASSOFRAMEWORK.COM.

---

## 3. The Avatar

Busy gym owners and boutique studio operators. They are scrolling fast.
NOT competitive athletes, CrossFit competitors, or strength sport athletes.
They respond to business outcomes: more leads, higher show rates, booked members.

---

## 4. Hard Copy Rules (enforced in code, not just in prompts)

These rules apply to every card, every surface, every model:

1. No em dashes, no en dashes, no hyphens in rendered text on cards.
2. No "vendor" in rendered text or captions.
3. The stat slab is retired: one colossal number as the card hero is banned.
4. One accent element maximum. Never two. Red is that accent on every canvas
   except CANVAS MODE RED, where red is the field and the single accent flips to
   white. A red field is only ever a declared canvas mode, never a stray choice.
5. Left-aligned and asymmetric is the DEFAULT and the fallback. A composition
   mode in Section 12 may place type elsewhere when the mode calls for it, but no
   prompt may ever instruct a "centered composition" or a "symmetric layout":
   those exact phrases still raise in `creative_studio._check_prompt_hard_rules`.
6. No illustrated scenes, no cartoon figures, no metaphorical imagery, no
   AI-looking art. Visual elements are clean flat icons, structured data layouts,
   diagrams, device mockups, and type. The ONE photography exception is CANVAS
   MODE DUOTONE: a real documentary frame duotoned to the brand, never a posed
   stock smile, never a composite, never a competitive athlete.
7. No STEP 1 / STEP 2 / STEP 3 labels in the illustrated element.

---

## 5. Layout Archetypes

Six archetypes are registered in `creative_studio.ARCHETYPES`. Five are
illustration-based; one is type-led.

**Archetype 1 — EDITORIAL (type-led opener, built for social feeds)**
The card is carried by typography and color alone. No illustration, no diagram, no figures.
Three levels, all left-aligned:
- Eyebrow: small ALL CAPS letterspaced label, top left, in RED (the one red element on the card)
- Headline: large BOLD high-contrast editorial serif, left-aligned, tight leading. Set it BIG — magazine cover scale, not a book title page. This is the hero moment.
- Deck: one medium-weight sans-serif line below the headline, rendered on the image.

Visual anchor (REQUIRED — every editorial concept spec must declare one):
1. Full-width COLOR BLOCK (preferred): a bold color field occupying the top half or more of the card; the headline reverses out of it.
2. Duotone photo treatment: a full-bleed high-contrast photo behind the type.
3. Oversized headline scale: the headline fills the card at such scale it becomes a graphic element.

Composition:
- EVERY ZONE EARNS ITS PLACE — color, type, or photo.
- NO VACANT THIRDS. A large empty field is not "breathing room" — it is a failed card.
- Red exactly once: the eyebrow label only.
- Reference: magazine COVER or Nike/Alo campaign card. Never a book interior.

**Archetypes 2-6 — FLOW, SPLIT, HERO, PATH, HEADLINE**
Illustration-based archetypes. See `creative_studio.ARCHETYPES` for their
detailed composition rules.

---

## 6. What Varies Per Card

On the Gemini path (`creative_studio.build_prompt`):

- CANVAS: one of four variant tokens (cream, navy, red, split)
- LAYOUT: one of eight layout tokens (framework, contrast, checklist, poster,
  chart, diagram, device) or one of six archetypes
- SUBJECT: the concrete illustrated scene (varies by content pillar)

On the Astra path (the default engine), see **Section 12**: seven canvases,
seven compositions, six accent placements. Subject always comes from the approved
source doc only. The brand grammar is constant on every path.

---

## 7. Model Routing

Every card that carries rendered headline text, labels, or stat figures routes
to the HERO (Pro) model for highest text render accuracy.

Text-light fills (photographic, abstract scene, no on-card text) may route to
the FLASH model when AGENT_NANO_FLASH_ENABLED is ON. OFF by default. When OFF,
ALL cards use the Pro model.

The actual model used is logged per card with the routing reason so spend and
quality are visible. Neither model is hardcoded: both are read from config only.

---

## 8. Generation Prompt Scaffold

This is the source of truth for `creative_studio.build_prompt()`. Every card
generation prompt must include all four blocks below, in order:

### Block A: Typographic System

```
TYPOGRAPHIC SYSTEM (apply exactly, three levels all left-aligned):
EYEBROW: one short ALL-CAPS label in small type at the very top left of the
  content area, naming the context in 1 to 3 words (examples: LEAD SPEED,
  FOLLOW UP, BOOKING RATE). This is the only small-caps line.
HEADLINE: the ONE large text element, set BIG and BOLD directly below the
  eyebrow, left-aligned, never centered. This is what the reader sees first.
  The headline is the ONLY large text.
DECK: one short sentence set SMALL directly below the headline at about one
  third the headline size, left-aligned. One line of context, never competing
  with the headline.
```

### Block B: Layout Rules

```
LAYOUT RULES (no exceptions):
LEFT-ALIGNED: every text element anchors to the left edge of the content area.
  Nothing is centered. Nothing is symmetric.
ASYMMETRIC: visual weight sits on one side; the other side breathes. The text
  column is the left spine. The illustrated element occupies the right half, the
  bottom two thirds, or a diagonal zone.
ONE DEPTH LAYER: exactly one subtle depth element on the card: a light wash
  behind the illustrated element, a soft drop shadow on the headline type, or a
  very faint geometric shape in the background field. Never a texture. Never a
  pattern. One quiet layer only.
```

### Block C: Canvas Token

```
CANVAS: [cream #FAF6F0 field with navy #121E3C type and generous whitespace]
  OR [navy #121E3C field with white type and a subtle dark vignette].
  The field fills the card. Generous margins. Nothing cut off at the edges.
ONE RED ACCENT: exactly one element on the entire card uses red (#FF0000 or
  #E03131). One rule line, one emphasized word in the headline, one diagram node,
  one arrow tip. Never a red background. Never two red elements.
```

### Block D: Visual Element

```
VISUAL ELEMENT: [ARCHETYPE BLOCK from the archetype system in creative_studio.py]
  Premium B2B agency quality. Clean, professional, never illustrated-scene or
  AI-generated looking. Allowed: flat icons (1-2 max), structured tables, step
  diagrams with labeled nodes, data callouts, comparison columns, minimal flow
  arrows. Banned: illustrated scenes, people in action, cartoon figures,
  metaphorical imagery, 3D effects, generic process labels (STEP 1, STEP 2,
  STEP 3, GROW, PLAN, LEARN, DISCOVER).
  Required labels: real business language — LEADS, BOOKED, SHOWED, CLOSED,
  FOLLOW UP, AD SPEND, COST PER LEAD.
```

---

## 9. Caption Standard

Captions are B2B StoryBrand. Every caption follows this beat order:

1. **Problem** — one punchy sentence the gym owner feels immediately.
2. **Why it persists** — one sentence naming the cause (not the owner's fault).
3. **Solution** — LASSO as the mechanism that removes the problem.
4. **CTA tucked at the end** — two short lines, single-newline between them.
   The CTA is never loud. It earns the click.

**Caption length — the week must mix SHORT and MEDIUM. Never run the same length 3 days in a row.**

- **SHORT** — Each beat is one tight sentence. 3 to 4 paragraphs, each a single line.
  Total caption is 3 to 5 lines of copy before hashtags. Punchy. Scrolls in a second.
- **MEDIUM** — Beats are fleshed out. 3 to 4 paragraphs, some with 2 to 3 sentences grouped
  together. Total copy is 5 to 9 lines before hashtags.
- Longer captions (10+ lines) are not the LASSO standard. Use short and medium.

**Paragraph rhythm:**
- A blank line separates genuine beat SHIFTS only — a new thought, a tonal pivot, a new
  rhetorical move. NOT every sentence.
- Sentences that continue the SAME beat and flow directly from each other stay in the
  same paragraph (period + space, no blank line between them).
- A standalone hook line that creates a pause or a breath gets its own paragraph.
  Do NOT run a hook into the next beat by appending a sentence to it.
- The CTA is its own paragraph. Never append the CTA to the sentence above it.
- Three to four paragraphs is the target. Five is too many. Do not manufacture extra
  breaks to pad visual length.

**SHORT example (correct):**
```
Honest numbers or no numbers.

Ads, lead nurture, your website, your social, and your reporting.

LASSO puts it all in one place, done for you, so you stop duct taping tools together.

Save this for later.
```

**MEDIUM example (correct):**
```
Agencies send reports. LASSO hands you the cockpit.

We run the same system on ourselves before we ever hand it to you. No guesswork and no bait and switch.

Just the system, run for you.
```

**Anti-pattern — hook smashed into the next beat (wrong):**
```
Honest numbers or no numbers.Ads, lead nurture, your website, your social, and your reporting.

LASSO puts it all in one place...
```

**Anti-pattern — every sentence its own paragraph (wrong for MEDIUM; only valid for SHORT):**
```
Agencies send reports.

LASSO hands you the cockpit.

We run the same system on ourselves.

No guesswork.
```

**Hard copy rules (same as Section 4, enforced here for captions):**
- No em dashes, en dashes, or hyphens.
- Never the word "vendor."
- No stats or claim without an approved receipt in the source doc.
- Plainspoken. "You can lift conversions up to 80 percent" — not "80% uplift."

**Empty caption = BLOCKED.** A draft with no caption text cannot enter the approval
queue. The drafter returns status=BLOCKED if the generated caption is empty after
stripping whitespace. Source: `agent/drafter.py` (`draft_post`).

---

## 10. Six-Question Grade Gate

A card passes when it answers YES to five or more of the six questions.
A card that fails (YES to fewer than five) is regenerated once automatically.
If the regeneration also fails, the card surfaces to #echoclaude flagged
"house-style fail: [which questions]" and does NOT enter the approval queue.

This gate is additive to the fabrication gate. The fabrication gate owns whether
the card is TRUE. This gate owns whether it looks ELEVATED. Both must pass.

**Q1 Left-aligned?**
Is every text element (eyebrow, headline, deck) anchored to the left edge?
A centered headline or symmetric layout FAILS. Checkable: vision model.

**Q2 Scale contrast?**
Is there visible typographic scale between the eyebrow (small), headline (large),
and deck (medium)? Uniform text size FAILS. Checkable: vision model.

**Q3 Single red accent?**
Is there exactly one red element, or zero? Two red elements or a red background
FAILS. Checkable: programmatic heuristic (prompt scan), confirmed by vision model.

**Q4 No banned copy?**
Does the rendered text contain no em dashes, no en dashes, no hyphens, and no
"vendor"? Checkable: OCR scan of the rendered image or headline text scan.

**Q5 Thumbnail legible?**
Can the headline be read at 100px wide? Thin type, low contrast, or clutter
around the headline FAILS. Checkable: vision model.

**Q6 Feed-stopping?**
Does the prompt specify a visual anchor — an illustrated element, color block,
full-width band, duotone photo, or oversized headline scale — that would stop
a thumb in a social feed? A card that is elegant but inert FAILS. Editorial cards
with no declared visual anchor always fail this question. Checkable: programmatic
heuristic (anchor keyword scan), confirmed by vision model.

---

## 11. What This System Replaces

The following patterns are RETIRED. No new prompt may use them:

- Any "centered composition" or "symmetric layout" instruction
- The stat slab: one colossal number as the hero element
- Flat two-dimensional compositions with no depth layer
- On-card STEP 1 / STEP 2 / STEP 3 labels
- "cream canvas, navy headline" with no eyebrow, deck, or left-aligned constraint

Cards generated under old patterns are listed in
`content_library/style_exclusions.json` and excluded from rotation until
regenerated under this system.

---

## 12. The Astra Style Freedom System (v1.2)

**Flag:** `AGENT_ASTRA_STYLE_FREEDOM`, default **OFF**.
**Code:** `agent/astra_prompt.py`.
**Why:** every Astra card looked the same because every Astra card *was* the same.
The brief hardcoded one palette that named cream "THE canvas" and one composition
that demanded exactly three vector elements plus a CTA button that was always the
single red element. Same field, same furniture, same accent, every card. That is
sameness by construction, not model behavior.

With the flag OFF the brief is byte for byte what it was. With it ON, each card
draws one canvas, one composition, and one accent placement.

### Canvas modes (7)

| Mode | Field | Type | Use it for |
|---|---|---|---|
| `cream` | Cream #FAF6F0 | Navy | The calm editorial default |
| `navy` | Navy #121E3C | Cream and white | Moody, confident, cinematic |
| `split` | Navy and cream, seamed | Flips per zone | Before and after, two truths |
| `sky` | Sky Blue #5EB9E6 | Navy | Outcome and momentum ideas |
| `ink` | Near black #0B1020 | Cream | The most serious, highest contrast |
| `red` | Red #FF0000 | White and navy | Highest urgency. Accent flips to white |
| `duotone` | Full bleed duotone photo | Reversed out | When a real human moment carries it |

All seven are built from the locked LASSO V3 colors. This widens which color
carries the field. It does **not** add a color to the brand.

### Composition modes (7)

`flat_editorial` (the former only option, now one of seven), `type_poster`,
`data_story`, `diagram`, `split_screen`, `stack`, `device`. Full text in
`astra_prompt.COMPOSITION_MODES`.

The CTA button block and the three element metaphor are now features of
`flat_editorial` only. Six of the seven modes carry no button at all.

### Accent placements (6)

`one_word`, `one_node`, `rule_line`, `arrow_tip`, `cta_button`, `corner_block`.
Exactly one accent element remains the law. Only *where* it lands is free. That
single rule is what keeps a run of varied cards reading as one brand.

### Selection

`style_for(key)` hashes the card key (the headline, by default) three times with
three different salts, so canvas, composition, and accent do not move in lockstep.
Deterministic: a re-render of an approved card returns the same look. A concept
may pin any of the three by hand.

### What did NOT change

The LASSO V3 color values. Two type families, never more. No dashes in rendered
text. No fabrication: a missing note still blocks the draft. The readability bar
(thumbnail legible, high contrast). The six-question grade gate. The human
approval gate. All 294 canvas and composition and accent combinations were
verified against the grade gate and the hard copy rules before this shipped.

### Account scope: every account by default (widened 2026-09-13)

Blake, 2026-09-12: "This should only be for LASSO right now until a proven
[out]." One day later, same ask restated with the explicit widening: "This
applies to the real production system — LASSO's own account plus any client
gym using the auto-infographic path." The master flag being ON does not by
itself free every account's cards — it is still scoped per account — but the
scope now defaults to everyone.

**`AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS`**, default `*` (every account).
Comma-separated account-key bases (an `_ig`/`_fb` suffix is stripped before
the check, so `lasso_ig` and `lasso_fb` both mean `lasso`) still narrows the
rollout back by hand — set it to `lasso` to restore the 2026-09-12 LASSO-only
scope without a code change, or to a specific list (`lasso,eng,gritx`).

A missing account_key (book_campaign, podcast, summit, stories, and the
render-card CLI with no `--account`) is treated as `lasso`, since every
unscoped caller in this repo IS LASSO's own content pipeline.

`python -m agent render-card --account eng --brief-only` previews exactly what
a given account gets in production, without touching Railway or waiting for a
calendar slot.

### A CLIENT GYM gets a DIFFERENT freed brief than LASSO's own account

Freedom mode is not "LASSO's palette, reshuffled" once it reaches a client
gym. Two things change for a non-LASSO account_key (`astra_prompt.
is_lasso_account`) when it is in freedom scope:

- **Its own voice, not LASSO's.** `_voice_path_for` resolves the gym's own
  durable drafted voice doc (`<DATA_DIR>/brand_voice/<base>/lasso_voice.md`,
  same resolver `client_media_sync._resolve_client_voice_path` uses for that
  gym's captions), not `config.VOICE_DOC_PATH` (LASSO's own doc). Before
  2026-09-13 every Astra brief, client gym included, read LASSO's voice doc
  unconditionally.
- **Real palette latitude, not LASSO's locked hex.** `gym_brand_latitude`
  replaces `LOCKED_BRAND_COLORS` + a `CANVAS_MODES` entry: no LASSO hex value
  is named anywhere in the brief. Astra chooses the field, supporting colors,
  and accent color itself, grounded in that gym's own voice + approved
  context, with only the field's ENERGY (calm/moody/urgent/etc, still varied
  per card) carried over from the canvas system for structure.
  `accent_law_free` keeps the "exactly one accent, never scattered"
  discipline without naming red. Genuine guardrails — the banned list, the
  readability bar, the no-fabrication line, the no-dash rule, the copy
  hard-rule checks — are unchanged for a gym card.

LASSO's own account is UNCHANGED by this: it still gets `LOCKED_BRAND_COLORS`
+ its 7-canvas system, because those ARE LASSO's real agency colors, not a
template imposed on someone else's brand.

## 12A. Texture, Depth, Photography, and the LASSO Masthead (v1.3)

**Why:** Blake graded 5 real Astra renders a C against a reference set of his
own and diagnosed the gap precisely: his reference used film grain, a glow
halo, a drop shadow, a real skyline photo, and a clean icon-in-a-circle
illustration — every one of which the v1.2 spec banned outright
(`FLAT_EDITORIAL_SPEC` block 2: "no illustrated scenes, no photorealism, no
3D, no gradients"; its closing line: "one depth layer at most... no texture
or pattern"; the same bans repeated in `creative_studio.STORY_REQUIREMENT`,
which every brief carried unconditionally). His reference set also carried a
consistent masthead — the "LASSO." wordmark with a red-dot period, a rule,
and a small eyebrow tag — plus a "Sherman Merricks & Blake Ruff" byline, on
every card. His ruling: **"I want it to have the freedom to do this level
without rules."**

### What's freed (any card style-freedom frees: LASSO's own or a client gym)

`TEXTURE_AND_DEPTH_LAW` (`agent/astra_prompt.py`) replaces
`creative_studio.STORY_REQUIREMENT` for a freed card. Permitted now, not
banned: film grain, a soft vignette, a drop shadow, a halo glow, light
halftone or paper texture, layered depth, and real photography (a real event
venue, a real gym space, a real workplace scene). `FREE_BANNED` replaces the
flat-only `BANNED` constant: drops the texture/photo/3D/illustration bans,
keeps cartoon/juvenile clip art, corny stock-art metaphors, clutter,
watermarks, and generic process labels banned.

**The one line that survives from the old ban, on every canvas, freed or
locked:** the LASSO avatar rule. Never a competitive athlete, never
CrossFit or HYROX imagery, never a barbell hero shot — LASSO markets to gen
pop boutique fitness (busy professionals, beginners, weight loss, lifestyle
fitness, postpartum, 40+ reclaim), never competitive strength athletes. That
is an audience rule, not a technique rule, so lifting the technique ban never
touches it.

`_FLAT_EDITORIAL_FREE` (the `flat_editorial` composition text) no longer
mandates flat-only rendering in its own block 2 or closing line.

### A new composition mode: `icon_list`

An 8th composition mode, matching the most-used layout in Blake's reference
set: three to five numbered rows, each pairing a bold number or icon with a
label and a subhead, closed by an optional CTA bar. 336 canvas x composition
x accent combinations now (was 294), all re-verified against the grade gate.

### The LASSO masthead and byline (LASSO's own account only)

A client gym's freed card is that gym's brand, never LASSO's — so this
section applies only when `is_lasso_account(account_key)` is true.

- **`masthead_block(label)`** renders the "LASSO." wordmark (period as a
  solid red dot) plus a thin rule at the top of every card, so a run of freed
  cards reads as one series instead of one-off graphics. An optional `label`
  (the new `masthead_label` param on `build_infographic_brief`, or
  `--masthead-label` on the render-card CLI) adds a small eyebrow line
  rendered exactly as given — the caller supplies the FULL text (e.g. "THE
  FULL GYM • SALES" or just "THE FULL GYM"), never a bare topic word that the
  code prefixes itself: not every LASSO card is a book-pillar card, so
  hardcoding a "THE FULL GYM" prefix would be wrong (and, caught during
  testing, duplicated the phrase when a caller passed it as the label). If
  the label names two phrases it says to join them with a real bullet
  character, never a dash. `label` is never invented by the brief builder;
  the same no-fabrication contract as everywhere else in this doc holds.
- **`byline_footer(url)`** renders the standing byline — "Sherman Merricks &
  Blake Ruff" — above the URL footer. This is attribution (LASSO's own
  founders, already named in this doc's Section 1), not an invented fact, so
  it is a constant (`LASSO_BYLINE`) rather than caller-supplied copy.

### Book cover product shot (`kind="book"`, LASSO's own account only)

Blake sent a second reference batch, five more of his own cards, all "book"
pillar, all showing a photographic-style render of THE FULL GYM's actual
cover — angled, drop-shadowed, a real product shot — often with a small gold
accolade ribbon ("Reached #1 on Amazon in Marketing"). `book_cover_element
(badge)` adds this block when a LASSO card's `kind="book"`. The cover
render itself is a real brand asset (showing it invents nothing); the ribbon
TEXT is a specific claim, so `book_cover_badge` is caller-supplied only —
never defaulted, never invented. Omitting it renders the cover with no
ribbon at all, same no-fabrication contract as every other approved fact in
this brief.

### What did NOT change

The locked/off path (style freedom OFF) still uses `STORY_REQUIREMENT` and
`BANNED` verbatim, with no masthead — untouched, byte for byte, for any
account still on the single template. The LASSO V3 color values, two type
families, the no-dash rule, no fabrication, the readability bar, the
six-question grade gate, and the human approval gate are all still locked,
on and off. Blake's reference set included a "$499 GENERAL ADMISSION" price
that no approved fact set backs — that was deliberately NOT carried over.
Visual freedom never overrides no-fabrication.
