# GHL Workflow Copy — Website Audit + Organic Audit Opt-Ins

Paste-ready email and SMS for both workflows. Same skeleton for each:
confirm → deliver → 3-touch follow-up → last call. Exit any contact who books.

**Custom fields to create first (Settings → Custom Fields):**

- `website_grade` — letter grade from the website audit (e.g. C)
- `social_grade` — letter grade from the social report card (e.g. D)
- `audit_link` — URL to the PDF (Google Drive or GHL media link)
- `biggest_fix` — one-line top fix from the audit (coach fills in at delivery)

**Workflow settings (both):**

- Trigger: form submitted (each audit has its own form)
- Goal event: appointment booked → contact exits workflow
- Delivery is gated on a tag your team applies when the PDF is ready:
  `website-audit-ready` / `social-audit-ready`
- Internal notification fires at opt-in so the team starts the audit clock

---

# WORKFLOW 1: WEBSITE AUDIT

## Step 0 — Internal notification (instant)

> 🔔 New website audit request: {{contact.name}} — {{contact.gym_name}} — {{contact.website}}. 48-hour clock started. Run the Six-Lens audit, upload PDF, set `audit_link` + `website_grade` + `biggest_fix`, apply tag `website-audit-ready`.

## Step 1 — Instant confirmation

**SMS (instant):**

> {{contact.first_name}}, it's LASSO. Your website audit is in the queue. We grade 6 things: your message, SEO, AI search, UX, conversion path, and platform. Report card lands within 48 hours. Keep an eye on your inbox.

**Email (instant)**
Subject: **Your website audit is underway**

> {{contact.first_name}},
>
> Got your request. Your audit is in the queue.
>
> Here's what happens next. We put {{contact.website}} through the same six-lens audit we run for our clients:
>
> 1. **Message** — does a stranger know what you do in 5 seconds?
> 2. **SEO** — can Google actually find and read your site?
> 3. **AI search** — when someone asks ChatGPT for a gym near you, do you exist?
> 4. **UX** — speed, mobile, dead links
> 5. **Conversion** — is there one clear path to booking?
> 6. **Platform** — the technical foundation under it all
>
> Every check is measured, not guessed. You get a graded report card out of 100 within 48 hours, plus the ranked fix list.
>
> No prep needed on your end. Talk soon.
>
> — LASSO

## Step 2 — Delivery (fires on tag `website-audit-ready`)

**Email**
Subject: **Your website grade: {{contact.website_grade}}**

> {{contact.first_name}},
>
> Your audit is done. {{contact.gym_name}} scored a **{{contact.website_grade}}**.
>
> Full report card here:
> {{contact.audit_link}}
>
> Every claim in it is checkable — we measured your site, pulled your Google reviews, and compared you against the gyms competing for the same members.
>
> The one thing I'd fix first: **{{contact.biggest_fix}}**
>
> Want to walk through it? Grab 15 minutes and I'll show you exactly where the leaks are and what order to fix them in:
> [BOOKING LINK]
>
> — LASSO

**SMS (15 min after email):**

> {{contact.first_name}} — your website report card just hit your inbox. {{contact.gym_name}} scored a {{contact.website_grade}}. The #1 fix is on page 4. Want me to walk you through it? 15 min: [BOOKING LINK]

## Step 3 — Follow-up 1 (Day 1 after delivery)

**Email**
Subject: **The page nobody reads (but should)**

> {{contact.first_name}},
>
> Quick one. Most owners open their report card, look at the grade, and close it.
>
> Skip to the review-mining page instead. We pulled your actual Google reviews and found the exact words your happiest members use to describe you. That's the copy your website should be using — and right now it isn't.
>
> Your members already wrote your marketing. It's free. It's sitting in your reviews.
>
> If you want, I'll show you the three lines I'd put on your homepage tomorrow:
> [BOOKING LINK]
>
> — LASSO

## Step 4 — Follow-up 2 (Day 3)

**SMS:**

> {{contact.first_name}}, did the grade surprise you? Most owners think their site is fine because it looks fine. The audit measures what a lead actually experiences. Happy to walk through the fix list — [BOOKING LINK]

## Step 5 — Follow-up 3 (Day 6)

**Email**
Subject: **What a {{contact.website_grade}} costs you**

> {{contact.first_name}},
>
> Here's the math that matters.
>
> Your ads, your Google listing, your word of mouth — they all send people to one place: your website. If that page loses half of them before they book, everything upstream gets twice as expensive.
>
> A {{contact.website_grade}} website isn't a design problem. It's a lead-cost problem.
>
> The fix list in your report card is ranked for a reason: the top two items usually move booking rate within 30 days. You can hand it to your web person as-is, or we can talk through doing it for you.
>
> [BOOKING LINK]
>
> — LASSO

## Step 6 — Last call (Day 10)

**Email**
Subject: **Closing the file on {{contact.gym_name}}**

> {{contact.first_name}},
>
> Last note from me on this. Your report card doesn't expire, but sites drift — new plugins, new pages, new leaks. The audit is a snapshot of today.
>
> If now's not the time, no problem. When you're ready to fix it, the ranked list is waiting in that PDF, and so am I.
>
> One reply gets you on my calendar.
>
> — LASSO

---

# WORKFLOW 2: ORGANIC (SOCIAL) AUDIT

## Step 0 — Internal notification (instant)

> 🔔 New social report card request: {{contact.name}} — {{contact.gym_name}} — IG: {{contact.instagram_handle}}. 48-hour clock started. Run the report card, upload PDF, set `audit_link` + `social_grade` + `biggest_fix`, apply tag `social-audit-ready`.

## Step 1 — Instant confirmation

**SMS (instant):**

> {{contact.first_name}}, it's LASSO. Your social report card is in the works. We grade your last 90 days of Instagram + Facebook on the 6 things members actually judge. Grade lands within 48 hours.

**Email (instant)**
Subject: **Your social report card is in the works**

> {{contact.first_name}},
>
> Got your handle. We're pulling your last 90 days of Instagram and Facebook right now.
>
> Here's what we grade — 100 points across six things a potential member actually notices:
>
> 1. **Consistency** — a quiet feed reads as a closed gym
> 2. **Content mix** — all promos feels like ads, all random feels like noise
> 3. **Caption craft** — they read two lines; those lines pull them in or scroll them past
> 4. **Visual match** — real faces and real floors, not stock
> 5. **Right audience** — does a nervous beginner think "that could be me"?
> 6. **Path to join** — interest with no next step is a lost lead
>
> Heads up: most gyms we audit score a D. Not from laziness — from no system. Every point is fixable, and the card shows you how.
>
> Report card in your inbox within 48 hours.
>
> — LASSO

## Step 2 — Delivery (fires on tag `social-audit-ready`)

**Email**
Subject: **{{contact.gym_name}}'s social grade: {{contact.social_grade}}**

> {{contact.first_name}},
>
> Your report card is ready. {{contact.gym_name}} graded a **{{contact.social_grade}}**.
>
> See the full breakdown, dimension by dimension:
> {{contact.audit_link}}
>
> This grade measures behaviors, not luck. Not followers, not likes, not the algorithm — six things you control, scored from your actual last 90 days of posts.
>
> Your biggest point leak: **{{contact.biggest_fix}}**
>
> Two ways to raise the grade:
>
> **Do it yourself.** The card shows exactly what an A looks like on every dimension. It's about ten hours a week.
>
> **Let Echo do it.** $99/month. A full month planned and staged before the month starts, every caption through a quality gate, and nothing posts without your tap. Ten minutes a week instead of ten hours.
>
> Reply "FIX" or grab 15 minutes: [BOOKING LINK]
>
> — LASSO

**SMS (15 min after email):**

> {{contact.first_name}} — report card's in your inbox. {{contact.gym_name}} scored a {{contact.social_grade}}. Every point is fixable, and the card shows the path. Want it done for you? Reply FIX.

## Step 3 — Follow-up 1 (Day 1 after delivery)

**Email**
Subject: **The grade isn't a talent problem**

> {{contact.first_name}},
>
> One thing I want to make sure landed from your report card.
>
> A {{contact.social_grade}} doesn't mean your gym is bad at social. It means nobody at your gym has ten spare hours a week — so social becomes whatever got posted at 9pm. That's a time problem, not a talent problem.
>
> And time problems have a fix.
>
> Echo runs your Instagram and Facebook on the exact rubric that just graded you. Month planned in advance. Every post approved by you. Fresh report card every month so you watch the grade climb.
>
> $99/month. Reply "FIX" and we'll start on next month's plan.
>
> — LASSO

## Step 4 — Follow-up 2 (Day 3)

**SMS:**

> {{contact.first_name}}, quick gut check: when's the last time your gym posted? If you had to look, that's the Consistency score. Echo fixes it for $99/mo — you just tap approve. Reply FIX and I'll set it up.

## Step 5 — Follow-up 3 (Day 6)

**Email**
Subject: **What members see before they ever walk in**

> {{contact.first_name}},
>
> Here's the pattern we see over and over.
>
> A lead hears about your gym. Before they book, they check your Instagram. Not to count followers — to answer one question: *is this place for someone like me?*
>
> If the last post is three weeks old, they read closed. If it's all max-effort athletes, they read "not for me." Either way, you paid for that lead and your feed sent them away.
>
> Your report card shows exactly which signals your feed is sending. Echo fixes every one of them for $99/month, and you approve every post before it goes live.
>
> Reply "FIX" or book here: [BOOKING LINK]
>
> — LASSO

## Step 6 — Last call (Day 10)

**Email**
Subject: **Next month's grade is being written right now**

> {{contact.first_name}},
>
> Last one from me.
>
> Your grade isn't fixed — it's just your last 90 days. Which means the next 90 days are being graded starting today, whether there's a plan behind them or not.
>
> If you want the next report card to look different, the $99 door is open whenever you're ready. If you'd rather run the playbook yourself, everything you need is in the card.
>
> Either way — the grade only moves when the posting does.
>
> — LASSO

---

# BUILD NOTES

- **Exit condition on both:** appointment booked OR reply containing "FIX" → remove from workflow, notify team.
- **The "wait for tag" step** is what keeps the 48-hour promise honest. If your team ever slips past 48 hours, add a safety email at hour 44: "Taking a little longer to be thorough — card lands tomorrow."
- **Send windows:** emails 9–11am local, SMS 12–2pm or 5–7pm. Never before 8am or after 8pm.
- **SMS compliance:** first SMS should append "Reply STOP to opt out" if these contacts didn't come through an existing conversation.
- **The grade merge field is the whole hook.** A subject line with their actual grade ("Your website grade: C") will outperform anything generic. Don't ship the delivery email until the custom fields are set.
