# MANUS BUILD HANDOFF — LASSO Audit Nurture (GHL)

**You are building 3 workflows in the GHL sub-account "LASSO Framework."**
Everything you need is in this file. Copy is final — paste verbatim. Do not paraphrase.

**Channel legend:**
- **[LINQ iMESSAGE]** — send from the Linq iMessage channel (blue bubble). Personal, 1:1 touches.
- **[GHL SMS]** — send from the standard GHL/LC SMS number. Utility + hard CTAs.
- Emails — use the **saved GHL email template** named in each step (build the templates first, Section B).

**Merge fields:** `{{contact.first_name}}`, `{{custom_values.booking_link}}` (GHL native — keep as-is).

**Launch state:** Leave all 3 workflows in **DRAFT / paused**. A human activates after review (LASSO SOP).

---

# SECTION A — GLOBAL SETUP (do first)

**A1. Tags** (Contacts → Tags): create `Website Audit Lead`, `IG Audit Lead`, `Client`

**A2. Pipeline** (Opportunities → Pipelines → the Audit pipeline), stages in this order:
1. `New Lead / Audit Requested`
2. `Audit Delivered / In Follow-Up`
3. `Closed Won (Client)`

**A3. Custom Value** (Settings → Custom Values): `booking_link` = the LASSO booking calendar URL.

**A4. Sender config** (both nurture workflows → Settings tab):
- Allow Re-Entry: **OFF**
- Stop on Response: **ON** (SMS + Email) — a reply pauses drips for manual takeover
- Confirm the **Linq iMessage channel** is available as a send option alongside the standard SMS number.

---

# SECTION B — EMAIL TEMPLATES (build before Workflow 2)

Build these as **saved templates**: Marketing → Emails → Templates → New → **Import/Code (HTML)** editor →
paste the matching HTML file (in the `emails/` folder of this handoff) → save under the exact name below.
Use the **HTML/code** editor, not drag-drop, so they render as designed.

| Template name (save as) | HTML file |
|---|---|
| `AUDIT — Website — 01 Welcome & Delivery` | email1_website.html |
| `AUDIT — Social — 01 Welcome & Delivery` | email1_ig.html |
| `AUDIT — 02 Reach vs Conversion` | email2_reach_vs_conversion.html |
| `AUDIT — 03 Proof / Receipts` | email3_proof.html |
| `AUDIT — 04 Path to Join` | email4_path_to_join.html |
| `AUDIT — 05 Full Toolbox` | email5_services.html |
| `AUDIT — 06 Growth Math` | email6_funnel_guide.html |
| `AUDIT — 07 ROI / Member Math` | email7_roi.html |
| `AUDIT — 08 90-Day Mark` | email8_final.html |
| `AUDIT — Monthly Newsletter` | email_monthly_template.html |

> ⚠️ Two templates carry a `[CLIENT RESULT]` / `[CLIENT STORY]` placeholder (03 and 07). Do NOT invent numbers.
> Leave the placeholder; a human fills it before activation.

---

# WORKFLOW 1 — "Echo Organic Audit" (intake + Day 1)

**Trigger:** Form Submitted → add BOTH: Website Audit Form, Instagram Audit Form.

**Steps in order:**

1. **Create/Update Opportunity** → Pipeline: Audit → Stage: `New Lead / Audit Requested`.

2. **If/Else** — condition: which form was submitted.
   - **Branch A — Website form:**
     - Add Tag: `Website Audit Lead`
     - Send Email → template `AUDIT — Website — 01 Welcome & Delivery`
   - **Branch B — Instagram form:**
     - Add Tag: `IG Audit Lead`
     - Send Email → template `AUDIT — Social — 01 Welcome & Delivery`

3. **Wait** 15 minutes.

4. **Create Task** → Title: `Call 1: Immediate Audit Lead - {{contact.first_name}}`
   Body: `Review submitted site/IG handle & make immediate outreach.` → Assign: sales rep.

5. **Send SMS 1 — [LINQ iMESSAGE]:**
   > Hey {{contact.first_name}} — Blake with LASSO. Got your audit request. We're pulling everything now, your report card lands within 48 hours. Quick q so I grade it right: what's the #1 thing you want more of right now — leads, shows, or members?

6. **Add to Workflow** → "Audit Lead Follow Up - Organic".

---

# WORKFLOW 2 — "Audit Lead Follow Up - Organic" (Day 2 → 90 + monthly loop)

**Trigger:** Contact Added to Workflow (from Workflow 1). *(Backup trigger: Tag Added = `Website Audit Lead` OR `IG Audit Lead`.)*

Build each row top to bottom. "Wait" is the gap BEFORE that day's actions.

### Day 2 — Wait 24 hours
- **Create Task:** `Call 2: 24hr Audit Follow-Up - {{contact.first_name}}` → Body: `24hr follow-up. Reference their audit.`
- **Send Email** → template `AUDIT — 02 Reach vs Conversion`
- **Send SMS 2 — [LINQ iMESSAGE]:**
  > Morning {{contact.first_name}} — while your audit's being built: about how many leads did your online presence send you last month? Rough guess is fine. It changes how I read your numbers.

### Day 4 — Wait 48 hours
- **Create Task:** `Call 3: 72hr Touchpoint - {{contact.first_name}}` → Body: `72hr touchpoint. Low-friction check-in.`
- **Send SMS 3 — [GHL SMS]:**
  > {{contact.first_name}}, quick one — when someone finds you online today, what's the next step you WANT them to take? Asking because that step is where most gyms leak.

### Day 8 — Wait 4 days
- **Create Task:** `Call 4: Value Follow-Up - {{contact.first_name}}` → Body: `Value follow-up. Share a proof point.`
- **Send Email** → template `AUDIT — 03 Proof / Receipts`

### Day 13 — Wait 5 days
- **Create Task:** `Call 5: Video Note Check-In - {{contact.first_name}}` → Body: `Offer a personal video walkthrough.`
- **Send SMS 4 — [LINQ iMESSAGE]:**
  > Hey {{contact.first_name}} — want me to record a quick 3-minute video walking through your audit? No call needed, I'll just text it over. Y or N?

### Day 18 — Wait 5 days
- **Create Task:** `Call 6: Mid-Sequence Touchpoint - {{contact.first_name}}` → Body: `Mid-sequence touchpoint.`
- **Send Email** → template `AUDIT — 04 Path to Join`

### Day 23 — Wait 5 days
- **Create Task:** `Call 7: Strategy Offer - {{contact.first_name}}` → Body: `Offer a strategy session.`
- **Send SMS 5 — [GHL SMS]:**
  > {{contact.first_name}} — worth 15 minutes to map the fix order together? I'll bring your numbers. Grab a time here: {{custom_values.booking_link}}

### Day 28 — Wait 5 days
- **Create Task:** `Call 8: Paid Marketing Transition - {{contact.first_name}}` → Body: `Transition to full-service overview.`
- **Send Email** → template `AUDIT — 05 Full Toolbox`

### Day 32 — Wait 4 days
- **Create Task:** `Call 9: Pre-Breakup Call - {{contact.first_name}}` → Body: `Pre-breakup call. Last personal push.`

### Day 36 — Wait 4 days
- **Create Task:** `Call 10: Final Manual Call Blitz - {{contact.first_name}}` → Body: `Final manual blitz.`
- **Send SMS 6 — [LINQ iMESSAGE]:**
  > Are you still looking to scale your marketing this month?

### Day 45 — Wait 9 days
- **Send Email** → template `AUDIT — 06 Growth Math`

### Day 60 — Wait 15 days
- **Send Email** → template `AUDIT — 07 ROI / Member Math`

### Day 75 — Wait 15 days
- **Send SMS 7 — [LINQ iMESSAGE]:**
  > Hey {{contact.first_name}}, been a minute. How's the gym trending vs earlier this year? If the online side still feels stuck, I've got a couple ideas worth 2 minutes.

### Day 90 — Wait 15 days
- **Send Email** → template `AUDIT — 08 90-Day Mark`

### Phase 3 — Monthly loop (after Day 90)
1. **Wait** 30 days
2. **Create Task:** `Monthly Check-In: {{contact.first_name}}` → Body: `Review dashboard history; monthly call, voice note, or personal DM.` → Assign: sales rep
3. **Send Email** → template `AUDIT — Monthly Newsletter`  *(alternate months, send SMS instead:)*
   **Monthly SMS — [GHL SMS]:**
   > {{contact.first_name}} — one thing we're seeing work in gyms right now: [MONTHLY INSIGHT]. Want the 2-minute version for your gym?
4. **Go To** → point back to Phase 3 Step 1 (Wait 30 days). This creates the indefinite monthly loop.

---

# WORKFLOW 3 — "Client Off-Ramp" (build new)

**Trigger (add both):** Opportunity Stage Changed → `Closed Won (Client)` **OR** Tag Added → `Client`.

**Steps:**
1. **Remove from Workflow** → "Audit Lead Follow Up - Organic"
2. **Remove from Workflow** → "Echo Organic Audit"
3. **Add Tag** → `Client`
4. **Add to Workflow** → Client Onboarding

> This is the safety net: the second a lead becomes a client, every audit call/text/email stops.

---

# CHANNEL MIX AT A GLANCE

| Msg | Day | Channel | Why |
|-----|-----|---------|-----|
| SMS 1 | 1 | **iMessage** | Warm human intro from Blake |
| SMS 2 | 2 | **iMessage** | Conversational question |
| SMS 3 | 4 | SMS | Utility question, guaranteed delivery |
| SMS 4 | 13 | **iMessage** | Personal video offer |
| SMS 5 | 23 | SMS | Hard booking CTA w/ link |
| SMS 6 | 36 | **iMessage** | 9-word re-engagement (reads personal) |
| SMS 7 | 75 | **iMessage** | Casual relationship check-in |
| Monthly | 90+ | SMS | Newsletter blast |

---

# FINAL TEST CHECKLIST (before a human activates)

1. Send yourself one test of **every** email template — confirm rendering + merge fields resolve.
2. Fire one test SMS on **each channel** — confirm the iMessage ones show **blue**, SMS ones send from the LC number.
3. Submit each intake form once → confirm correct tag + correct branch email fires.
4. Confirm Stop-on-Response pauses drips, and the Off-Ramp removes a contact when tagged `Client`.
5. Fill the `[CLIENT RESULT]` (Email 03) and `[CLIENT STORY]` (Email 07) placeholders.
6. Leave in DRAFT. Human flips to live.
