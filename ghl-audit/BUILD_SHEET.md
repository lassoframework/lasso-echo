# GHL BUILD SHEET — Echo Organic Audit + Audit Lead Follow Up

Everything below maps 1:1 to your architecture. SMS = Linq iMessage number (blue).
Emails = paste the matching HTML file from the nurture pack (email HTML → GHL email
step → "Code/HTML" editor → paste). Full copy for each item is in `copy_master.md`.

---

## STEP 0 — SETTINGS (do once, before touching steps)

**Tags** (Contacts → Tags → New): `Website Audit Lead`, `IG Audit Lead`, `Client`

**Pipeline** (Opportunities → Pipelines → the audit pipeline), stages in order:
1. New Lead / Audit Requested
2. Audit Delivered / In Follow-Up
3. Closed Won (Client)

**Custom Value** (Settings → Custom Values → Add): `booking_link` = your Calendly/GHL calendar URL.
Now `{{custom_values.booking_link}}` resolves everywhere.

**Both workflows → Settings tab:**
- Allow Re-Entry: **OFF**
- Stop on Response: **ON** (SMS + Email)
- Sender: SMS from the **Linq iMessage** number; Email from your sending domain.

---

## WORKFLOW 1 — "Echo Organic Audit" (intake + Day 1)

**Trigger:** Form Submitted → Website Audit Form **OR** Instagram Audit Form (add both).

1. **Create/Update Opportunity** → Pipeline: audit pipeline → Stage: *New Lead / Audit Requested*.
2. **If/Else** branch on which form fired:
   - **Website branch:** Add Tag `Website Audit Lead` → Send **Email 1A** (`email1_website.html`)
   - **IG branch:** Add Tag `IG Audit Lead` → Send **Email 1B** (`email1_ig.html`)
3. **Wait 15 minutes**
4. **Create Task** → Title `Call 1: Immediate Audit Lead - {{contact.first_name}}` → Body: "Review submitted site/IG handle & make immediate outreach." → Assign: sales rep.
5. **Send SMS 1** (opt-in confirmation — `copy_master.md` › SMS 1)
6. **Add to Workflow** → "Audit Lead Follow Up - Organic" (hands off the long engine)

> Keeping intake and the long nurture in two workflows (as you set up) means Stop-on-Response and the off-ramp only have to detach the follow-up engine.

---

## WORKFLOW 2 — "Audit Lead Follow Up - Organic" (Day 2 → 90 + monthly loop)

**Trigger:** Added to workflow (from Workflow 1). *(Or Contact Tag = Website/IG Audit Lead.)*

| Day | Wait | Step | Content |
|-----|------|------|---------|
| 2 | 24 hr | Task `Call 2: 24hr Audit Follow-Up - {{contact.first_name}}` + **Email 2** + **SMS 2** | `email2_reach_vs_conversion.html`; SMS 2 |
| 4 | 48 hr | Task `Call 3: 72hr Touchpoint - {{contact.first_name}}` + **SMS 3** | SMS 3 |
| 8 | 4 days | Task `Call 4: Value Follow-Up - {{contact.first_name}}` + **Email 3** | `email3_proof.html` |
| 13 | 5 days | Task `Call 5: Video Note Check-In - {{contact.first_name}}` + **SMS 4** | SMS 4 |
| 18 | 5 days | Task `Call 6: Mid-Sequence Touchpoint - {{contact.first_name}}` + **Email 4** | `email4_path_to_join.html` |
| 23 | 5 days | Task `Call 7: Strategy Offer - {{contact.first_name}}` + **SMS 5** | SMS 5 |
| 28 | 5 days | Task `Call 8: Paid Marketing Transition - {{contact.first_name}}` + **Email 5** | `email5_services.html` |
| 32 | 4 days | Task `Call 9: Pre-Breakup Call - {{contact.first_name}}` | — |
| 36 | 4 days | Task `Call 10: Final Manual Call Blitz - {{contact.first_name}}` + **SMS 6** | SMS 6 (9-word) |
| 45 | 9 days | **Email 6** | `email6_funnel_guide.html` |
| 60 | 15 days | **Email 7** | `email7_roi.html` |
| 75 | 15 days | **SMS 7** | SMS 7 |
| 90 | 15 days | **Email 8** | `email8_final.html` |

**Phase 3 — Monthly loop (after Day 90):**
1. Wait 30 days
2. Create Task `Monthly Check-In: {{contact.first_name}}` → Body: "Review history on dashboard; monthly call, voice note, or personal DM." → Assign: sales rep
3. Send Email **or** SMS → `email_monthly_template.html` / monthly loop SMS (rotate)
4. **Go To** → back to loop Step 1 (Wait 30 days)

---

## WORKFLOW 3 — "Client Off-Ramp" (new — build this)

**Trigger:** Opportunity Stage Changed → *Closed Won (Client)*  **OR**  Tag Added → `Client`.
1. Remove from Workflow → "Audit Lead Follow Up - Organic" (and Echo Organic Audit)
2. Add Tag → `Client`
3. Add to Workflow → Client Onboarding

---

## GOTCHAS
- Email HTML: use the **Code/HTML** email editor, not the drag-drop builder, so the template renders as designed.
- `{{contact.first_name}}` is GHL's merge field — already in every SMS/subject.
- Before activating: send yourself one test of each email + SMS. Confirm merge fields and the Linq blue bubble.
- Launch **paused** — human review before turning live (per LASSO SOP).
