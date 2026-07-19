# Intake Form Spec

Source of truth: `agent/intake_web.py` — `FORM_FIELDS`, `handle_intake_form()`, `handle_portal_intake()`, `normalize_portal_intake()`.

The HTML reference is `lasso_social_intake.html` in this folder. It is the canonical client-facing form.

---

## Seven Sections, Field by Field

### Section 1: Gym Basics

| Field name | Type | Required | Max length | Notes |
|---|---|---|---|---|
| `gym_name` | text input | Yes | 200 | 400 error if blank |
| `city` | text input | No | 200 | One city or list |
| `website` | text input | No | 200 | inputmode="url" |
| `about` | textarea | No | 4,000 | Free text. Not routed to PENDING sources; stored in archived payload for the bible draft |

### Section 2: Brand Voice

| Field name | Type | Required | Max length | Notes |
|---|---|---|---|---|
| `voice` | textarea | No | 4,000 | Words they love, words they avoid, tone |

Portal JSON equivalent fields: `voice.vibe`, `voice.words_to_use`, `voice.words_to_never_use`, `voice.sample_post_links`. These are assembled into one block.

### Section 3: Offers and Services

| Field name | Type | Required | Max length | Notes |
|---|---|---|---|---|
| `offers` | textarea | No | 4,000 | One per line. Current offers the gym is running |
| `services` | textarea | No | 4,000 | One per line. Programs, classes, memberships |
| `pricing_rule` | textarea | No | 4,000 | Exact wording Echo may use for pricing. If blank, no prices are ever posted |

The pricing rule callout (rendered in the form): "We never post a price, discount, or guarantee unless it is written here exactly as you want it to appear. If this box is empty, no prices are ever posted."

Portal JSON equivalent fields: `offers.front_door_offer`, `offers.services`, `offers.exact_pricing_wording`.

### Section 4: Audience

| Field name | Type | Required | Max length | Notes |
|---|---|---|---|---|
| `audience` | textarea | No | 4,000 | Who the gym talks to |

Portal JSON equivalent fields: `audience.ideal_member`, `audience.prior_struggles`. Assembled as two labelled lines.

### Section 5: Proof

| Field name | Type | Required | Max length | Notes |
|---|---|---|---|---|
| `proof` | textarea | No | 4,000 | Member wins the gym has permission to share, one per line |

The proof callout (rendered in the form): "Only share wins the member has agreed to make public. We hold every one for your approval before it can appear in a post."

Portal JSON equivalent fields: `proof.wins`, `proof.verifiable_numbers`.

### Section 6: Media Notes

| Field name | Type | Required | Max length | Notes |
|---|---|---|---|---|
| `media_notes` | textarea | No | 4,000 | What to feature, what to avoid, members off camera |

Portal JSON equivalent fields: `media_notes` (top-level string).

### Section 7: Approver

| Field name | Type | Required | Max length | Notes |
|---|---|---|---|---|
| `approver_name` | text input | No | 200 | Name of the person who approves posts |
| `approver_contact` | text input | No | 200 | Phone, email, or Slack |

Portal JSON equivalent fields: `approver.name`, `approver.role`, `approver.cell`, `approver.email`. Assembled as "Name (Role)" and "cell, email".

---

## Validation Rules

1. `gym_name` must not be blank. Returns `400 {"error": "gym.name is required"}`.
2. At least one field other than `gym_name` must be non-blank. Returns `400 {"error": "the intake is empty"}` or `{"error": "the form is empty"}` depending on submission path.
3. All field values are truncated to 4,000 characters at ingest. No client-side truncation needed, but inform the gym if they paste very long text.
4. The `about` field in the HTML form maps to `about` in the flat answers shape but is not routed to PENDING sources. It is archived in the payload for the bible drafter.

---

## Acknowledgment Checkboxes

The gym-facing form must include two checkboxes that gate the submit button. Both must be checked before submit is allowed.

1. "Everything I share here waits for my approval before a single post goes live."
2. "Member wins and photos I include here have permission to be used publicly."

These are client-side UI gates. Echo does not validate them server-side; the portal enforces them.

---

## Exact POST Payload (gym-facing HTML form)

Content-Type: `application/x-www-form-urlencoded`

```
gym_name=District+H
city=Indianapolis%2C+IN
website=https%3A%2F%2Fdistricth.com
about=Coach-led+small+group+training+since+2019
voice=Direct%2C+community-first
offers=21+day+intro+for+new+members
services=Small+group+training%0AOpen+gym
pricing_rule=21+days+for+%2421
audience=Busy+adults+28+to+45+in+Indianapolis
proof=Jake+dropped+18+lbs+in+8+weeks
media_notes=Feature+real+members
approver_name=Marcus+Webb
approver_contact=%2B13175550100
```

---

## Exact POST Payload (portal JSON path)

Content-Type: `application/json`

See `API_CONTRACT.md` section "Portal JSON intake" for the full nested schema.

---

## Post-Submit Flow

### HTML form path

1. Echo stores the payload in R2.
2. Echo responds with the `FORM_DONE_TMPL` HTML: "Got it. Thank you. Your answers are in and nothing posts until you approve it. One more step while you are here: send us your photos and videos." with a button linking to `/u/<token>`.
3. The gym taps the button and lands on the upload page.

### Portal JSON path

1. Portal POSTs JSON to `/intake/<token>`.
2. Echo responds with `{"status": "received", "account_key": "...", "pending_source_count": N, "upload_url": "https://echo-intake-web-production.up.railway.app/u/<token>"}`.
3. Portal shows a confirmation step: "Your intake is in. Now send us your photos and videos." with the `upload_url` as a prominent button or redirect.
4. The gym taps and lands on the Echo upload page at `/u/<token>`.

The portal does **not** need to build a custom upload UI. Echo owns the upload page.
