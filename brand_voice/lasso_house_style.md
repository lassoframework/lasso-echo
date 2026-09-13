# LASSO House Style System

Source of truth for every infographic Echo generates.
Version: 1.2 (2026-09-13)

**v1.2 changes the default.** Cream is no longer THE canvas, it is one of seven.
Astra now draws a canvas, a composition, and an accent placement per card instead
of rendering one template every time. See Section 12. Everything the brand is
built on stays locked: the colors, two type families, no dashes, no fabrication,
the readability bar, and the human approval gate.

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
