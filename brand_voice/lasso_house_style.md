# LASSO House Style System

Source of truth for every infographic Echo generates.
Version: 1.0 (2026-07-17)

All constants in `agent/creative_studio.py` that begin with `HOUSE_STYLE_`
or reference this document must match the scaffold in Section 7 exactly.
When this document changes, update those constants. Never the reverse.

---

## 1. Purpose

Every generated card in the Echo pipeline MUST follow this system.
The creative studio builds its generation prompts from Section 7 of this doc.
The grade gate in Section 8 determines whether a card enters the approval queue.

---

## 2. Brand DNA (constant, never varies)

Colors (LASSO V3, locked):

| Token    | Hex       | Role                                               |
|----------|-----------|----------------------------------------------------|
| Cream    | #FAF6F0   | The default canvas field                           |
| Navy     | #121E3C   | Headlines, structure, dark canvas base             |
| Sky Blue | #5EB9E6   | Secondary accents, flow lines, supporting touches  |
| Red      | #FF0000   | THE single accent. One element only. Never background. Never two. |

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
4. One red element maximum. Never a red background. Never two red elements.
5. No centered symmetric compositions. Every card is left-aligned and asymmetric.
6. No abstract symbolism. Images show concrete gym owner scenes only.
7. No STEP 1 / STEP 2 / STEP 3 labels in the illustrated element.

---

## 5. What Varies Per Card

- CANVAS: cream (light, default) or navy (dark, cinematic) dominant field
- LAYOUT: one of eight layout tokens (framework, contrast, checklist, poster,
  chart, diagram, device, and the five archetypes)
- SUBJECT: the concrete illustrated scene (varies by content pillar)

Canvas and layout are set per-card from the variant system. Subject comes from
the approved source doc only. Everything else is constant.

---

## 6. Model Routing

Every card that carries rendered headline text, labels, or stat figures routes
to the HERO (Pro) model for highest text render accuracy.

Text-light fills (photographic, abstract scene, no on-card text) may route to
the FLASH model when AGENT_NANO_FLASH_ENABLED is ON. OFF by default. When OFF,
ALL cards use the Pro model.

The actual model used is logged per card with the routing reason so spend and
quality are visible. Neither model is hardcoded: both are read from config only.

---

## 7. Generation Prompt Scaffold (Section 7)

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

### Block D: Illustrated Element

```
ILLUSTRATED ELEMENT: [ARCHETYPE BLOCK from the archetype system in creative_studio.py]
  The illustration depicts a CONCRETE SCENE from a gym owner's world.
  No abstract symbols. No generic business icons.
  Banned labels: STEP 1, STEP 2, STEP 3, GROW, PLAN, LEARN, DISCOVER.
  Allowed labels: LEADS, NO FOLLOW UP, BOOKED, SHOWED, MEMBERS, and other
  words a gym owner uses.
```

---

## 8. Five-Question Grade Gate (Section 8)

A card passes when it answers YES to four or more questions.
A card that fails (YES to fewer than four) is regenerated once automatically.
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

---

## 9. What This System Replaces

The following patterns are RETIRED. No new prompt may use them:

- Any "centered composition" or "symmetric layout" instruction
- The stat slab: one colossal number as the hero element
- Flat two-dimensional compositions with no depth layer
- On-card STEP 1 / STEP 2 / STEP 3 labels
- "cream canvas, navy headline" with no eyebrow, deck, or left-aligned constraint

Cards generated under old patterns are listed in
`content_library/style_exclusions.json` and excluded from rotation until
regenerated under this system.
