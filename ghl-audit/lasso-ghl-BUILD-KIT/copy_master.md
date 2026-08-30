# GHL Build — Master Copy Pack (Echo Organic Audit + Audit Lead Follow Up)

**Workflow mapping:**

- **Echo Organic Audit** = intake + Day 1 (opportunity, branch tag, SMS 1, Email 1, Call task 1) → adds contact to follow-up workflow
- **Audit Lead Follow Up – Organic** = Day 2 → Day 90 engine + monthly loop
- **Client Off-Ramp** = new third workflow (Closed Won / Client tag → remove from both)

**SMS channel: every SMS step sends from the Linq iMessage number (blue bubble).**
Copy is written for iMessage: short, human, one question at a time, no corporate sign-offs.

---

## SMS (Linq / iMessage)

**SMS 1 — Day 1, +15 min (opt-in confirmation)**
> Hey {{contact.first_name}} — Blake with LASSO. Got your audit request. We're pulling everything now, your report card lands within 48 hours. Quick q so I grade it right: what's the #1 thing you want more of right now — leads, shows, or members?

**SMS 2 — Day 2 (metric check-in)**
> Morning {{contact.first_name}} — while your audit's being built: about how many leads did your online presence send you last month? Rough guess is fine. It changes how I read your numbers.

**SMS 3 — Day 4 (bottleneck question)**
> {{contact.first_name}}, quick one — when someone finds you online today, what's the next step you WANT them to take? Asking because that step is where most gyms leak.

**SMS 4 — Day 13 (video walk-through offer)**
> Hey {{contact.first_name}} — want me to record a quick 3-minute video walking through your audit? No call needed, I'll just text it over. Y or N?

**SMS 5 — Day 23 (direct booking ask)**
> {{contact.first_name}} — worth 15 minutes to map the fix order together? I'll bring your numbers. Grab a time here: {{custom_values.booking_link}}

**SMS 6 — Day 36 (9-word re-engagement)**
> Are you still looking to scale your marketing this month?

**SMS 7 — Day 75 (casual check-in)**
> Hey {{contact.first_name}}, been a minute. How's the gym trending vs earlier this year? If the online side still feels stuck, I've got a couple of ideas worth 2 minutes.

**Monthly loop SMS (rotate with the newsletter email)**
> {{contact.first_name}} — one thing we're seeing work in gyms right now: [MONTHLY INSIGHT]. Want the 2-minute version for your gym?

---

## EMAILS (subjects + body copy — full HTML in /home/claude/ghl/emails/)

**Email 1A — Website branch — "Your website report card is being built"**
Grade-what-we-measure welcome. 48-hour promise. CTA: book 15 min.

**Email 1B — IG branch — "Your social report card is being built"**
Six dimensions, most gyms score a D, every point fixable. CTA: book 15 min.

**Email 2 — Day 2 — "Reach vs. conversion (the number that pays rent)"**
Reach is a vanity metric; the funnel legs are what pay. Close ≥70 / show ≥50 / booking ≥50 / lead volume — diagnose upstream before spending more.

**Email 3 — Day 8 — "Proof: we run our own playbook"**
LASSO's own IG, last 30 days: 89 posts, 4,790 reached, 7,351 views — receipts from our own publisher analytics. Slot for a client result: [CLIENT RESULT — insert real number before publishing].

**Email 4 — Day 18 — "The Path to Join (the 10 points most gyms drop)"**
Dead bio links, expired offers, no CTA. Three fixes this week.

**Email 5 — Day 28 — "Everything we do, on one page"**
Full service overview: DFY paid ads + sales training + Echo DFY social ($99/mo). Invitation: free audit review call.

**Email 6 — Day 45 — "The 4 numbers that run your gym's growth"**
Funnel/CRO guide — booked-to-close math, where to look first, what "good" looks like.

**Email 7 — Day 60 — "The math of one member"**
ROI spotlight — LTV vs. acquisition math. Slot: [CLIENT STORY — insert real story before publishing].

**Email 8 — Day 90 — "Your last 90 days just became a new report card"**
The grading window rolled over. Offer a fresh audit + strategy session link.

**Monthly newsletter template** — rotating value note, same shell.

---

## Task titles/bodies, tags, pipeline: per Blake's spec, built verbatim in GHL.
## Custom value to create: `booking_link` (Settings → Custom Values).
