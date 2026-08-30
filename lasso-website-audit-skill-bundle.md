# LASSO Website Audit — single-file skill bundle

Hand this one file to Claude Code. It contains every file in the skill.

**To install, tell Claude Code:** "Install the skill contained in this file. For each `==== FILE:` section below, create that file at `.claude/skills/<path shown>` with the section's exact contents, then list the folder so I can confirm all 6 files landed."

The result should be:

```
.claude/skills/lasso-website-audit/
├── SKILL.md
├── reference/
│   ├── rubric.md
│   ├── measurement.md
│   └── copy-voice.md
└── assets/
    ├── build_audit.py
    └── example_site.json
```

Nothing below this line needs editing — it is the skill, verbatim.

================================================================
==== FILE: lasso-website-audit/SKILL.md
================================================================

---
name: lasso-website-audit
description: Build a LASSO Six-Lens Website Audit — a measured, graded PDF audit of a gym's website, used as a sales asset. Grades StoryBrand, SEO, AI Search/GEO, UI/UX, Conversion and Platform out of 100. Use when asked to audit, grade, or review a gym's website or landing page; when given a domain and asked for a website audit or "six lens audit"; or when asked to re-run or batch these audits.
---

# LASSO Six-Lens Website Audit

Produces a 4-page PDF that measures a gym's website element by element, grades it
on six lenses out of 100, mines the owner's own Google reviews for the copy they
should be using, and closes on a ranked fix list.

**The product is the honesty.** These go to gym owners who can check every claim.
**There is no "could not verify" tier.** Every check either gets an answer or is
explicitly named as still open, with the reason it could not be completed. An audit
that hedges on whether a sitemap exists has not been done.

**Write it as a standalone audit.** Even when a site has been graded before, the
deliverable reads as a first look: a technical baseline of what is true today, not
a diff against an earlier document. The client is being shown their site, not our
version history. (If a genuine before/after is ever wanted, see the variant note
under step 7.)

## The pipeline

1. **Measure the site** in a real browser → `reference/measurement.md`
2. **Resolve the root files** — robots.txt, sitemap, llms.txt, schema
3. **Pull the Google listing, all reviews, and the competitor set** via Apify
4. **Mine the review corpus** — this is the page nobody else writes
5. **Score** the six lenses → `reference/rubric.md`
6. **Write** → `reference/copy-voice.md`
7. **Build** → `assets/build_audit.py` + a JSON entry
8. **Critique** — adversarial review pass, then fix. Do not skip.

## 1–2. Measure the site, resolve the root files

Full scripts in `reference/measurement.md`. The non-obvious parts:

- **Many gym sites 403 datacenter IPs.** Apify Lighthouse actors and PageSpeed
  actors will fail or, worse, *succeed against a block page* and hand you fast,
  meaningless numbers. Always check the returned `statusCode`. If it is 403,
  discard the run.
- **Measure timings in a real browser instead**, and take at least two loads.
  Timings vary a lot between loads; report a **range**, never a single fake-precise
  number. Say so on the card.
- **Root files live at non-obvious paths.** `/sitemap.xml` often 404s while
  `/sitemap_index.xml` (Yoast) is live. Check both before reporting a sitemap
  missing. Record the actual status code for each.
- **Check whether a dead-looking nav link is a dropdown parent.** `href="#"` on a
  nav item with a submenu is correct and normal. `href="#"` with no submenu and an
  anchor class is a real defect. Getting this wrong ships a false finding — verify
  with `li.querySelector('ul, .sub-menu')`.

## 3. Pull the Google data

Three Apify calls, all cheap. Actor names and exact inputs in
`reference/measurement.md`.

| What | Actor | Why it matters |
|---|---|---|
| Listing NAP, rating, review count | `beatanalytics/google-maps-reviews-scraper` | Resolves "live review count", and the raw address string exposes NAP defects verbatim |
| Every review | `compass/Google-Maps-Reviews-Scraper` | The corpus for step 4 |
| Competitor set | same, with `searchQueries` + `searchLocations` | Ranks the client against 15–20 real local rivals by review volume and rating |

The listing address string is worth reading character by character. On the worked
example Google itself renders the address as **"Suite 203 Suite 203"** — a NAP
defect no fetch-only review would ever catch.

## 4. Mine the review corpus — the page that makes this an A

Read the reviews. Not a summary of them — the actual text. Then build a two-column
table: **what members say, unprompted** against **what the site says**. Count both
sides.

This is the highest-value page in the audit because it converts opinion into
evidence. On the worked example it produced:

- Members name **six coaches** across the corpus. The homepage names **one**.
- Members self-identify at **58, 60, 64, 69, 70, 75, 85**. The homepage names no age.
- Members arrive after **arthritis, back surgery, knee replacement, open-heart
  recovery**, and one is on a **GLP-1 protecting muscle**. The site addresses none of it.
- Members quantify results — *"16 lbs of fat, 4 lbs of muscle in 32 sessions."*
  The site shows no numbers.
- A member coined **"gymtimidation"** — better copy than the site has.

**Hunt specifically for a negative review that documents the defect you are
selling against.** The worked example contains a prospect explaining, publicly, that
they went elsewhere because nobody would quote a price. That single quote is worth
more than the entire conversion lens, because it turns "publish a price" from an
opinion into a lost customer with a date on it. Quote it at length and let it sit.

Also hunt for a member who **answers an objection better than the site does**, and
tell the owner to put that sentence on the page.

## 5. Score

Six lenses, 100 points. Bands and descriptors in `reference/rubric.md`.

| Lens | Points |
|---|---|
| StoryBrand | 20 |
| SEO | 20 |
| Conversion | 20 |
| AI Search / GEO | 15 |
| UI / UX | 15 |
| Platform | 10 |

Grades: **A** 90+ · **B** 80–89 · **C** 70–79 · **D** 60–69 · **F** below 60.

Conversion carries 20 points because booked intros are the job. A site that is
beautiful, fast and unbookable fails.

Grade what is live today. Do not soften a total to reach a nicer band, and do not
justify a score by reference to an earlier audit — the card stands on its own
measurements.

## 6–7. Write and build

Voice rules in `reference/copy-voice.md`. Build:

```bash
cd assets && npm i @fontsource/oswald @fontsource/nunito-sans
python3 build_audit.py            # reads site.json -> <slug>.html
chromium --headless --disable-gpu --no-sandbox \
  --virtual-time-budget=6000 --no-pdf-header-footer \
  --print-to-pdf=out.pdf file://$PWD/<slug>.html
```

Page order: **1** the headline number, four stat tiles and the **technical
baseline** table · **2** the review corpus and competitor rank · **3** the grade
lens by lens plus method · **4** the fix list, what is already working, and the CTA.

The page-1 baseline table is three columns — *what we checked · what we found on the
live site · PASS or FAIL*. Keep it to **11 rows** or the page-1 callout clips. Lead
with the passes: the credibility of the fails depends on the reader trusting that
you noticed what works.

*Variant, only if explicitly asked for a before/after:* swap that table for four
columns and add a prior-value column. Default is standalone.

Palette, fonts and layout are shared with `lasso-social-report-card` — navy
`#152B4F`, red `#EF3B42`, accent `#64B2E0`, cream `#FAF6EC`, Oswald + Nunito Sans
embedded as base64. Do not re-invent them. No emoji in card copy — the render
environment has no emoji font and they come out as tofu boxes.

Keep the fix table to **10 rows or fewer** or page 4 overflows and clips the CTA.

## 8. Critique — required

Render every page to PNG and look at it. Check specifically:

- Any content clipped at a page edge, especially the page-4 CTA button.
- Any leftover artifact if you adapted a template from another audit type
  (the word "Instagram", "POSTS", "SOCIAL REPORT CARD").
- Does any number contradict another anywhere in the document?
- Does the points-lost column sum to the stated total?
- Is any finding stated more confidently than the measurement supports?

## Output

`<Client_Name>_Website_Audit.pdf`, plus a project note recording the score per
lens, the headline findings, which checks were resolved, and the deliverable name.

================================================================
==== FILE: lasso-website-audit/reference/rubric.md
================================================================

# The six-lens rubric

100 points. Score against what the live site does for a visitor trying to become a
member. Every score needs a measured figure or a verbatim quote behind it.

Bands: **A** 90+ · **B** 80–89 · **C** 70–79 · **D** 60–69 · **F** below 60.

---

## StoryBrand — 20

Does the site name the visitor's problem, offer a plan, and put a human guide in
front of them?

| Score | Looks like |
|---|---|
| 18–20 | Problem named in the buyer's words, clear plan, named coaches with credentials, stakes stated, one obvious first step |
| 14–17 | Strong problem copy and a named first step, but the guide has no face or the stakes are missing |
| 10–13 | Generic hero copy, features not outcomes, no people |
| 5–9 | Talks about equipment and square footage |
| 0–4 | No discernible message |

Check: H1 content (an unprovable superlative like "THE BEST X IN Y" costs points
where a member outcome belongs), coach names present vs coach names in reviews,
whether an age or life-stage is ever named.

## SEO — 20

| Score | Looks like |
|---|---|
| 18–20 | Unique well-formed title and meta, valid canonical, viewport, OG tags, full alt coverage, live and fresh sitemap, clean robots, consistent NAP |
| 14–17 | Technically sound with one or two real defects — NAP inconsistency, thin pages |
| 10–13 | Missing meta, duplicate titles, or a stale sitemap |
| 5–9 | Sitewide noindex risk, broken canonicals |
| 0–4 | Not crawlable |

Measure: title length (50–60 ideal), meta length (140–160), image alt coverage as a
fraction, sitemap `lastmod`, and word count per page.

## Conversion — 20

**Weighted heaviest because booked intros are the job.**

| Score | Looks like |
|---|---|
| 18–20 | Price or range published, self-serve booking embedded, one clear ask per screen, risk reversal, transitional offer |
| 14–17 | Clear single ask and a working path, but no price or no self-booking |
| 10–13 | Gated pricing, form-only capture, callback-dependent |
| 5–9 | Pricing gated *and* the path is broken or hidden |
| 0–4 | No working route from interest to booking |

Count dollar figures on the page. Count form elements. Check whether a booking
widget is actually embedded or merely referenced. **Check that the nav items
carrying buying intent actually resolve.**

## AI Search / GEO — 15

| Score | Looks like |
|---|---|
| 14–15 | Valid schema of the right type, answer-first FAQ, named authors with credentials, llms.txt, AI crawlers allowed |
| 11–13 | Schema and FAQ present, author authority thin |
| 8–10 | Schema present but generic type, no author signals |
| 4–7 | No schema, or AI crawlers blocked in robots.txt |
| 0–3 | Nothing an engine can cite |

`ExerciseGym` is the correct type for a gym and most sites never ship it — credit
it. llms.txt returning 404 is a small, cheap, confirmed gap.

## UI / UX — 15

| Score | Looks like |
|---|---|
| 14–15 | Clean build, no dead links, negligible layout shift, dedicated contact page, obvious hierarchy |
| 11–13 | Solid with a cosmetic defect or a missing contact route |
| 8–10 | Dead links in the nav, or a visible copy error in a template region |
| 4–7 | Multiple broken links, layout shift, mobile problems |
| 0–3 | Broken |

CLS below 0.1 is good and below 0.01 is excellent — say so, it is rare. A grammar
error in the nav or footer appears on every page; weight it accordingly.

## Platform — 10

| Score | Looks like |
|---|---|
| 9–10 | Fast server, fully instrumented (tag manager, analytics, ad pixel, CRM), schema, sane stack |
| 7–8 | Right stack and tracking, but slow paint or heavy page |
| 5–6 | Tracking gaps or unclear CRM capture |
| 3–4 | Render-blocking mess, no analytics |
| 0–2 | Broken or unmaintained |

Separate **server** from **page**. A 61ms TTFB with a 9-second first paint is not a
hosting problem; it is what loads after the server responds. Say which.

---

## Scoring discipline

- Score from measured figures before writing prose.
- Do not nudge a total to reach a nicer band.
- Grade what is live today. The card stands on its own measurements, not on
  comparison to any earlier document.
- Where two lenses could absorb the same criticism, put it in the one whose
  definition it actually matches and reference it from the other.

================================================================
==== FILE: lasso-website-audit/reference/measurement.md
================================================================

# Measurement

Everything on the card must come from one of these. Nothing is estimated.

---

## A. Real-browser measurement (primary)

Many gym sites return **403 to datacenter IPs**, which breaks Apify Lighthouse
actors and PageSpeed actors. A real logged-in browser is not blocked. Run this on
the live homepage, twice, on separate loads.

### Timings, weight and Core Web Vitals

```js
window.__M={lcp:0,cls:0,shifts:0};
try{new PerformanceObserver(l=>{for(const e of l.getEntries())window.__M.lcp=Math.round(e.startTime)})
  .observe({type:'largest-contentful-paint',buffered:true});}catch(e){}
try{new PerformanceObserver(l=>{for(const e of l.getEntries())
  if(!e.hadRecentInput){window.__M.cls+=e.value;window.__M.shifts++}})
  .observe({type:'layout-shift',buffered:true});}catch(e){}
await new Promise(r=>setTimeout(r,9000));
const nav=performance.getEntriesByType('navigation')[0]||{};
const paints={};performance.getEntriesByType('paint').forEach(p=>paints[p.name]=Math.round(p.startTime));
const res=performance.getEntriesByType('resource');
({ttfb:Math.round(nav.responseStart||0), fcp:paints['first-contentful-paint'],
  lcp:window.__M.lcp, cls:+window.__M.cls.toFixed(4), shifts:window.__M.shifts,
  domContentLoaded:Math.round(nav.domContentLoadedEventEnd||0),
  loadEvent:Math.round(nav.loadEventEnd||0), requests:res.length,
  totalKB:Math.round(res.reduce((s,r)=>s+(r.transferSize||0),0)/1024)})
```

**Report timings as a range across loads.** On the worked example TTFB came back
61ms on one load and 8,682ms on the next — a 140× swing. FCP was 8.8s and 10.3s,
consistent in magnitude but not precise. The honest card said "roughly 9 to 10
seconds to paint anything" and recommended a controlled test before quoting a
single figure. CLS was stable at 0.001 across both loads and *was* quoted exactly.

### DOM facts, schema, links, copy gaps

```js
const txt=document.body.innerText, H=document.documentElement.innerHTML;
const links=[...document.querySelectorAll('a')];
const imgs=[...document.querySelectorAll('img')];
({words:txt.split(/\s+/).length,
  title:document.title, titleLen:document.title.length,
  metaDesc:(document.querySelector('meta[name=description]')||{}).content,
  canonical:(document.querySelector('link[rel=canonical]')||{}).href,
  viewport:!!document.querySelector('meta[name=viewport]'),
  og:[...document.querySelectorAll('meta[property^="og:"]')].length,
  h1:[...document.querySelectorAll('h1')].map(h=>h.innerText.trim()),
  schema:[...document.querySelectorAll('script[type="application/ld+json"]')]
    .map(s=>{try{const j=JSON.parse(s.textContent);
      return (Array.isArray(j)?j:[j]).map(x=>x['@type']||(x['@graph']||[]).map(g=>g['@type']).join('+')).join(',')}
      catch(e){return 'PARSE_ERROR'}}),
  links:links.length, images:imgs.length,
  imgsNoAlt:imgs.filter(i=>!(i.getAttribute('alt')||'').trim()).length,
  forms:document.querySelectorAll('form').length,
  telLinks:links.filter(a=>(a.getAttribute('href')||'').startsWith('tel:')).length,
  mailLinks:links.filter(a=>(a.getAttribute('href')||'').startsWith('mailto:')).length,
  prices:txt.match(/\$\s?\d[\d,]*(\.\d{2})?/g)||[],
  pixels:{gtm:/googletagmanager\.com\/gtm/.test(H), ga4:/gtag\/js\?id=G-/.test(H),
          fbq:/connect\.facebook\.net.*fbevents/.test(H),
          ghl:/leadconnectorhq|gohighlevel|msgsndr/i.test(H)}})
```

### Dead links — verify before you report

`href="#"` on a nav item **with a submenu is correct**. Only a `#` with no submenu
is a defect. Check it:

```js
[...document.querySelectorAll('a[href="#"]')].map(a=>{
  const li=a.closest('li');
  const sub=li?li.querySelector('ul, .sub-menu'):null;
  return {txt:(a.innerText||'').trim(), cls:a.className,
          hasSubmenu:!!sub, subCount:sub?sub.querySelectorAll('a').length:0};
});
```

On the worked example this separated a legitimate `MORE` dropdown from two genuine
defects — `GET STARTED` and `PRICING`, both `elementor-item-anchor` with no
submenu and an empty target, each rendered three times across desktop, mobile and
sticky headers. Reporting all 18 `#` links as broken would have been wrong;
reporting two as broken was right.

---

## B. Root files — resolve, never assume

Fetch each and record the status code. `/sitemap.xml` frequently 404s on WordPress
while the real one is elsewhere.

| Path | What a real answer looks like |
|---|---|
| `/robots.txt` | Note whether AI crawlers (GPTBot, ClaudeBot, PerplexityBot) are blocked, and which sitemap it points to |
| `/sitemap_index.xml` | Yoast default. Check `lastmod` — it proves whether the content engine is live |
| `/sitemap.xml` | Check only after the index; absence here is not absence of a sitemap |
| `/llms.txt` | A 404 is a real finding. Report "absent, returns 404", not "could not verify" |

Schema comes from the DOM script above, not from a guess.

---

## C. Apify — the Google data

### Listing + NAP
```json
// beatanalytics/google-maps-reviews-scraper
{"searchQueries":["<Business Name>"], "searchLocations":["<City, State>"],
 "maxPlacesPerSearch":12, "maxReviewsPerPlace":1, "language":"en", "country":"us"}
```
Returns `placeName`, `placeAddress`, `placeRating`, `placeReviewCount`,
`placePhone`, `placeWebsite`, `placePlaceId`, `placeCategories`. **Read
`placeAddress` literally** — duplicated suite lines and inconsistent names show up
here verbatim.

### Every review
```json
// compass/Google-Maps-Reviews-Scraper
{"placeIds":["<placePlaceId>"], "maxReviews":<count+20>,
 "reviewsSort":"newest", "language":"en", "personalData":false}
```
~$0.00045/review — 304 reviews cost under 15 cents. Fetch the `text` field in
pages of ~130 and read them. Note how many you actually read and report counts
against that number, not against the full corpus.

### Competitor set
```json
// beatanalytics/google-maps-reviews-scraper
{"searchQueries":["personal training gym","strength training gym","small group personal training"],
 "searchLocations":["<City, State>"], "maxPlacesPerSearch":12,
 "maxReviewsPerPlace":1, "language":"en", "country":"us"}
```
Use 3 search terms to widen the net, dedupe, sort by `placeReviewCount`, and rank
the client inside it. Always check `totalItemCount` against `itemCount` when
paging results — a first page of 15 with a total of 20 silently truncates.

---

## D. What not to trust

| Trap | Why | Handling |
|---|---|---|
| Lighthouse actor returns fast scores | It measured a 403 block page | Always check `statusCode`; discard 403 runs |
| PageSpeed Insights actors | Shared API keys hit daily quota; anonymous PSI is also rate-limited | Expect 429; fall back to real-browser measurement |
| A single timing sample | Varies wildly load to load | Two loads minimum, report a range |
| `href="#"` count | Dropdown parents are legitimate | Check for a submenu first |
| `/sitemap.xml` 404 | The real sitemap is often `/sitemap_index.xml` | Check both |
| Cached browser load | Transfer sizes read as 0 | Note cache state, or hard-reload |

================================================================
==== FILE: lasso-website-audit/reference/copy-voice.md
================================================================

# Voice and copy

House voice: punchy, direct-response, evidence-first. Short sentences. No fluff, no
consultant vocabulary, no hedging. The reader is a busy gym owner who can check
every claim you make.

---

## Non-negotiables

- **Every finding quotes a measured figure or verbatim page copy.** No adjective
  stands in for a number.
- **Numerals, not words.** "304 reviews", not "over three hundred".
- **US spelling. No emoji in card copy** — the render environment has no emoji
  font. Describe it instead.
- **Lead with what is genuinely working.** These sites usually do two or three
  things better than the agencies auditing them. Say so first and mean it.
- **Never claim a check you did not complete.** If a measurement is unstable, give
  the range and say it needs a controlled test. That reads as more competent, not
  less.
- **Never reveal a stack of other clients.** No "in this book".
- **Distinguish server from page, lab from field, plays from views.** Precision on
  these is what separates this audit from a template.

## The shape of a finding

> **Claim → measured proof → what it costs them.**

Good:

> "Two header items are anchor links with an empty target: GET STARTED and PRICING.
> They render three times each across the desktop, mobile and sticky headers, so 12
> of your 64 homepage links go nowhere. Clicking either one scrolls the visitor
> back to the top of the page."

Bad:

> "Navigation could be improved and some links may not be working as intended."

## The review-corpus page

This is the page that makes the audit an A, and it has a specific shape.

1. **A two-column gap table.** What members say, unprompted, against what the site
   says. Count both columns. Red marks the gap.
2. **One long verbatim quote** that documents the defect you are selling against —
   ideally a negative review naming the exact problem. Let it run at length. Do not
   trim it to be kind; do not editorialise inside it.
3. **A closing line that names what the quote proves.** e.g. *"This is not our
   opinion about pricing. It is a prospective member, in their own words, on your
   Google listing, explaining that they chose another gym because they could not
   find out what yours costs."*
4. **The competitor rank table**, client row highlighted, so the reader sees their
   own standing in their own market.

The rhetorical move: **the owner already owns the words their website is missing.**
They are sitting on Google, written by customers, for free.

## Tone by grade

| Grade | Register |
|---|---|
| A / B | Peer to peer. Respect first, then the one gap. |
| C | Even-handed. Two things working, two costing money. |
| D | Direct and unhedged, still warm. Name the failure and show that the fix is small. |
| F | No softening and no piling on. State it, then spend real space on the asset they already have. |

## Lines that have worked

- "You earned the visit. Now let them say yes."
- "Great front door. Locked back door."
- "This is not a redesign. It is two href values."
- "Interest with no path is a lost lead, daily."
- "Nothing on this list is a rebuild."
- "Your members have already written the website you should have."

================================================================
==== FILE: lasso-website-audit/assets/build_audit.py
================================================================

import base64, json, os
FD=os.environ.get('FONTSOURCE_DIR','./node_modules/@fontsource')
def b64(p): return base64.b64encode(open(p,'rb').read()).decode()
FACES=""
for fam,pkg,ws in [('Oswald','oswald',[400,600,700]),('Nunito Sans','nunito-sans',[400,600,700])]:
    for w in ws:
        FACES+=f"@font-face{{font-family:'{fam}';font-style:normal;font-weight:{w};src:url(data:font/woff2;base64,{b64(f'{FD}/{pkg}/files/{pkg}-latin-{w}-normal.woff2')}) format('woff2');}}\n"

NAVY="#152B4F"; RED="#EF3B42"; BLUE="#64B2E0"; CREAM="#FAF6EC"
TRACK="#EFEAD9"; GOLD="#C9992F"; ORANGE="#D06A2C"; GREEN="#7BB25E"; ICE="#EAF3FA"; INK="#22262E"; MUTED="#7E8598"

def barcolor(p): return GREEN if p>=80 else GOLD if p>=70 else ORANGE if p>=60 else RED
def letter(p): return 'A' if p>=90 else 'B' if p>=80 else 'C' if p>=70 else 'D' if p>=60 else 'F'
def pill(l): return {'A':GREEN,'B':GOLD,'C':ORANGE,'D':RED,'F':RED}[l]

CSS = FACES + f"""
*{{margin:0;padding:0;box-sizing:border-box}}
@page{{size:letter;margin:0}}
body{{font-family:'Nunito Sans',sans-serif;color:{INK};-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.page{{width:8.5in;height:11in;position:relative;overflow:hidden;page-break-after:always;background:#fff}}
.page:last-child{{page-break-after:auto}}
.band{{background:{NAVY};padding:22px 46px 0;border-bottom:4px solid {RED}}}
.brow{{display:flex;justify-content:space-between;align-items:flex-start}}
.logo{{font-family:'Oswald';font-weight:700;color:#fff;font-size:26px;line-height:.95;letter-spacing:.5px}}
.logo small{{display:block;font-weight:400;font-size:8.5px;letter-spacing:4.6px;color:#C7D2E4;margin-top:3px}}
.tagchip{{font-family:'Oswald';font-weight:600;font-size:8.5px;letter-spacing:2.2px;color:{BLUE};
  border:1px solid rgba(100,178,224,.55);padding:5px 11px}}
.lock{{text-align:right;margin-top:14px}}
.lock .sc{{font-family:'Oswald';font-weight:700;font-size:30px;color:#fff;line-height:1;letter-spacing:-.5px}}
.lock .sc small{{font-size:15px;color:#8FA3C0;font-weight:400}}
.lock .lb{{display:inline-block;font-family:'Oswald';font-weight:700;font-size:17px;color:#fff;
  width:27px;height:27px;line-height:27px;text-align:center;margin-left:8px;vertical-align:6px}}
.lock .cap2{{font-size:7.5px;color:#8FA3C0;letter-spacing:1.6px;margin-top:6px;font-family:'Oswald';font-weight:600}}
.kchip{{display:inline-block;background:{RED};color:#fff;font-family:'Oswald';font-weight:600;
  font-size:9px;letter-spacing:2.4px;padding:5px 12px;margin:20px 0 12px}}
h1.title{{font-family:'Oswald';font-weight:700;font-size:40px;line-height:1;color:#fff;letter-spacing:-.3px}}
h1.title span{{color:{BLUE}}}
.sub{{color:#BFCBDE;font-size:10.5px;line-height:1.75;margin-top:11px;padding-bottom:14px;max-width:6.6in}}
.sub b{{color:#fff;font-weight:700}}
.body{{padding:14px 46px 0}}
.kick{{font-family:'Oswald';font-weight:600;font-size:9.5px;letter-spacing:2.6px;color:{NAVY};
  margin:0 0 8px;display:flex;align-items:center;gap:9px}}
.kick:before{{content:"";width:14px;height:2.5px;background:{RED};display:inline-block}}
/* hero figure */
.hero{{background:{NAVY};padding:13px 24px;display:flex;align-items:flex-end;justify-content:space-between}}
.hero .fig{{font-family:'Oswald';font-weight:700;font-size:46px;color:#fff;line-height:.9;letter-spacing:-1px}}
.hero .lab{{font-size:10px;color:#BFCBDE;margin-top:7px;letter-spacing:.2px}}
.hero .dl{{text-align:right}}
.hero .dv{{font-family:'Oswald';font-weight:700;font-size:26px;line-height:1}}
.hero .dt{{font-size:8.5px;color:#9DAAC0;margin-top:5px}}
/* stat tiles */
.tiles{{display:flex;gap:9px;margin-top:9px}}
.tile{{flex:1;background:{CREAM};border:1px solid #E7E0CE;padding:9px 11px 10px}}
.tile .l{{font-size:8px;color:{MUTED};letter-spacing:.3px;line-height:1.3;height:18px}}
.tile .v{{font-family:'Oswald';font-weight:700;font-size:21px;color:{NAVY};line-height:1;margin-top:3px}}
.tile .d{{font-size:8.2px;font-weight:700;margin-top:5px}}
.flat{{color:{MUTED} !important}}
.tile .p{{font-size:7.5px;color:{MUTED};margin-top:1px}}
/* tables */
table{{width:100%;border-collapse:collapse;font-size:9px}}
thead th{{background:{NAVY};color:#fff;font-family:'Oswald';font-weight:600;font-size:8px;
  letter-spacing:2px;text-align:left;padding:7px 10px}}
tbody td{{padding:4.5px 10px;vertical-align:top;line-height:1.55;border-bottom:1px solid #E8EAEF}}
tbody tr:nth-child(even) td{{background:#FBFAF6}}
.num{{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}}
th.num{{text-align:right}}
tr.hl td{{background:#FDECEC !important;font-weight:700}}
tr.hl td:first-child{{box-shadow:inset 3px 0 0 {RED}}}
td.dim{{width:1.35in}}
td.dim b{{font-family:'Oswald';font-weight:600;font-size:9.5px;letter-spacing:1.2px;color:{NAVY};display:block}}
td.dim i{{font-family:'Oswald';font-weight:400;font-style:normal;font-size:9px;color:#8A8F9E;display:block;margin-top:2px}}
.cap{{font-size:8.6px;color:#4A5061;font-style:italic}}
.noask{{font-family:'Oswald';font-weight:600;font-size:8.5px;letter-spacing:1px;color:{RED}}}
/* grade */
.gcard{{background:{CREAM};border:1px solid #E7E0CE;padding:14px 18px 12px}}
.glabel{{font-family:'Oswald';font-weight:600;font-size:8px;letter-spacing:2.2px;color:{MUTED};margin-bottom:11px}}
.grow{{display:flex;align-items:center;gap:12px;margin-bottom:7px}}
.gname{{font-family:'Oswald';font-weight:600;font-size:9.5px;letter-spacing:1.5px;color:{NAVY};width:.98in}}
.glost{{font-family:'Oswald';font-weight:600;font-size:9.5px;width:.52in;text-align:right;font-variant-numeric:tabular-nums;color:{MUTED}}}
.gtrack{{flex:1;height:9px;background:{TRACK}}}
.gfill{{height:9px}}
.gscore{{font-family:'Oswald';font-weight:600;font-size:10.5px;width:.62in;text-align:right;font-variant-numeric:tabular-nums}}
.gtot{{display:flex;align-items:center;justify-content:space-between;border-top:1px solid #E2DAC6;margin-top:11px;padding-top:10px}}
.gtot .t{{font-family:'Oswald';font-weight:700;font-size:15px;color:{NAVY};letter-spacing:.6px}}
.pill{{font-family:'Oswald';font-weight:700;font-size:19px;color:#fff;width:31px;height:31px;
  display:flex;align-items:center;justify-content:center}}
.diag{{background:{ICE};border-left:4px solid {NAVY};padding:11px 16px;margin-top:10px}}
.diag h4{{font-family:'Oswald';font-weight:600;font-size:9.5px;letter-spacing:2.2px;color:{NAVY};margin-bottom:7px}}
.diag p{{font-size:9.8px;line-height:1.68;color:#2C3140}}
.callout{{background:{NAVY};padding:11px 18px 12px;margin-top:8px}}
.callout h4{{font-family:'Oswald';font-weight:600;font-size:9px;letter-spacing:2.2px;color:{BLUE};margin-bottom:6px}}
.callout p{{font-size:10.5px;line-height:1.6;color:#fff}}
.callout p b{{color:{RED}}}
.callout p em{{font-style:normal;color:#F3E9D2;text-decoration:underline;text-underline-offset:2px}}
.callout .close{{color:#BFCBDE;font-size:9.2px;line-height:1.55;margin-top:6px;display:block}}
/* page 3 */
.p2head{{padding:34px 46px 0}}
h2{{font-family:'Oswald';font-weight:700;font-size:26px;color:{NAVY};letter-spacing:-.2px}}
h2 span{{color:{BLUE}}}
.p2sub{{font-size:10px;line-height:1.7;color:#565C6B;margin-top:9px;max-width:6.6in}}
.arrow{{display:flex;align-items:center;gap:6px}}
.gp{{font-family:'Oswald';font-weight:700;font-size:10.5px;color:#fff;width:19px;height:19px;
  display:flex;align-items:center;justify-content:center}}
.ar{{color:#A9AFBD;font-size:10px}}
.note{{font-size:9.2px;color:#565C6B;line-height:1.6;margin-top:9px}}
.cards{{display:flex;gap:11px}}
.card{{flex:1;background:{CREAM};border:1px solid #E7E0CE;padding:11px 12px 12px;text-align:center}}
.card h5{{font-family:'Oswald';font-weight:600;font-size:9.5px;letter-spacing:1.6px;color:{NAVY};margin-bottom:6px}}
.card p{{font-size:8.6px;line-height:1.6;color:#63697A}}
.card{{text-align:left !important}}
.good{{background:{CREAM};border-left:4px solid {GOLD};padding:10px 15px}}
.good h4{{font-family:'Oswald';font-weight:600;font-size:9.5px;letter-spacing:2.2px;color:{GOLD};margin-bottom:6px}}
.good p{{font-size:9.6px;line-height:1.65;color:#3A3F4D}}
.cta{{background:{NAVY};padding:15px 22px;margin-top:10px}}
.cta .pre{{font-family:'Oswald';font-weight:600;font-size:8.5px;letter-spacing:2.4px;color:{BLUE};margin-bottom:9px}}
.cta h3{{font-family:'Oswald';font-weight:700;font-size:21px;color:#fff;letter-spacing:.2px}}
.cta h3 span{{color:{RED}}}
.cta p{{font-size:9.5px;line-height:1.7;color:#BFCBDE;margin-top:9px;max-width:5.9in}}
.btn{{display:inline-block;text-decoration:none;background:{RED};color:#fff;font-family:'Oswald';font-weight:600;
  font-size:10px;letter-spacing:2px;padding:9px 16px;margin-top:11px}}
.foot{{position:absolute;bottom:0;left:0;right:0;background:{NAVY};padding:15px 46px;
  display:flex;justify-content:space-between;align-items:center}}
.foot .l{{font-family:'Oswald';font-weight:700;font-size:11px;color:#fff;letter-spacing:1.4px}}
.foot .l small{{display:block;font-family:'Nunito Sans';font-weight:400;font-size:8px;
  color:#9DAAC0;letter-spacing:0;margin-top:2px}}
.foot .r{{font-family:'Oswald';font-weight:600;font-size:9px;color:{BLUE};letter-spacing:1.8px}}
.srcline{{font-size:7.4px;color:#9AA0AE;margin-top:5px;font-style:italic}}
.method{{margin-top:20px;border-top:1px solid #E4E7ED;padding-top:12px;display:flex;gap:26px}}
.method .mh{{font-family:'Oswald';font-weight:600;font-size:8.5px;letter-spacing:2.2px;color:{MUTED};width:1.5in;flex-shrink:0}}
.method p{{font-size:8.4px;line-height:1.65;color:#6A7080}}
"""

def build(g):
    rails=g['rails']; total=sum(r[1] for r in rails); mx=sum(r[2] for r in rails)
    tl=letter(total*100/mx)
    grows="".join(f"""<div class="grow"><div class="gname">{n}</div>
      <div class="gtrack"><div class="gfill" style="width:{s*100/o:.0f}%;background:{barcolor(s*100/o)}"></div></div>
      <div class="gscore" style="color:{barcolor(s*100/o)}">{s}/{o}</div>
      <div class="glost">{'&mdash;' if o-s==0 else '&minus;'+str(o-s)}</div></div>""" for n,s,o,_ in rails)
    trows="".join(f"""<tr><td class="dim"><b>{n}</b><i>{s}/{o}</i></td><td>{e}</td></tr>""" for n,s,o,e in rails)
    IMP={'HIGH':RED,'MED':GOLD,'LOW':MUTED}
    erows="".join(f"""<tr><td class="num" style="text-align:left;width:.3in;font-weight:700;color:{NAVY}">{i+1}</td>
      <td>{f['t']}</td><td style="width:.95in;font-size:8.4px;color:{MUTED}">{f['lens']}</td>
      <td class="num" style="width:.6in"><span style="font-family:'Oswald';font-weight:600;font-size:8.5px;letter-spacing:1px;color:{IMP[f['imp']]}">{f['imp']}</span></td>
      <td class="num" style="width:.62in;font-size:8.4px;color:{MUTED}">{f['eff']}</td></tr>""" for i,f in enumerate(g['fixes']))
    tiles="".join(f"""<div class="tile"><div class="l">{t['l']}</div><div class="v">{t['v']}</div>
      <div class="d" style="color:{GREEN if t['good'] else (MUTED if t['good'] is None else RED)}">{t['d']}</div><div class="p">{t['p']}</div></div>"""
      for t in g['tiles'])
    cmp_rows="".join(f"""<tr class="{'hl' if r.get('hl') else ''}"><td style="width:1.75in">{r['m']}</td>
      <td>{r['b']}</td>
      <td class="num" style="width:.75in;color:{GREEN if r['good'] else (RED if r['good'] is False else MUTED)};font-weight:700">{r['d']}</td></tr>"""
      for r in g['compare'])
    reach_rows="".join(f"""<tr><td class="num" style="text-align:left;width:.62in">{r['d']}</td>
      <td class="cap">&ldquo;{r['c']}&rdquo;</td><td class="num">{r['v']}</td>
      <td class="num" style="font-weight:700;color:{NAVY}">{r['q']}</td>
      <td class="num">{r['l']}</td><td class="num"><span class="noask">&mdash;</span></td></tr>"""
      for r in g['reach'])
    revrows="".join(f"""<tr><td>{r['t']}</td><td class="num" style="font-weight:700;color:{NAVY}">{r['a']}</td>
      <td class="num" style="font-weight:700;color:{RED if r['bad'] else GREEN}">{r['b']}</td></tr>""" for r in g['revgap'])
    comprows="".join(f"""<tr class="{'hl' if c.get('me') else ''}"><td class="num" style="text-align:left;width:.3in">{i+1}</td>
      <td>{c['n']}</td><td class="num">{c['r']}</td><td class="num" style="font-weight:700">{c['c']}</td></tr>""" for i,c in enumerate(g['comps']))
    cards="".join(f"""<div class="card"><h5>{t}</h5><p>{b}</p></div>""" for t,b in g['week'])
    return f"""<html><head><meta charset="utf-8"><title>{g['name']} Social Report Card</title>
<style>{CSS}</style></head><body>

<div class="page">
  <div class="band">
    <div class="brow"><div class="logo">LASSO<small>FRAMEWORK</small></div>
    <div><div class="tagchip">LASSO WEBSITE AUDIT</div>
      <div class="lock"><span class="sc">{total}<small>/100</small></span>
      <span class="lb" style="background:{pill(tl)}">{tl}</span>
      <div class="cap2">OVERALL GRADE</div></div></div></div>
    <div class="kchip">SIX-LENS WEBSITE AUDIT</div>
    <h1 class="title">{g['t1']} <span>{g['t2']}</span></h1>
    <div class="sub">{g['subline']}<br>
    Audited {g['audit_date']} on the live site at {g['handle']}. {g['posts']}<br>
    <b>{g['short']}</b></div>
  </div>
  <div class="body">
    <div class="kick">THE HEADLINE NUMBER</div>
    <div class="hero"><div><div class="fig">{g['hero']['v']}</div><div class="lab">{g['hero']['l']}</div></div>
      <div class="dl"><div class="dv" style="color:{RED if g['hero'].get('down') else GREEN}">{g['hero']['d']}</div><div class="dt">{g['hero']['p']}</div></div></div>
    <div class="tiles">{tiles}</div>

    <div class="kick" style="margin-top:16px">{g['cmpkick']}</div>
    <table><thead><tr><th>WHAT WE CHECKED</th><th>WHAT WE FOUND ON THE LIVE SITE</th><th class="num">RESULT</th></tr></thead>
    <tbody>{cmp_rows}</tbody></table>
    <div class="srcline">{g['source']}</div>

    <div class="callout"><h4>{g['callout_h']}</h4><p>{g['callout']}</p>
      <span class="close">{g['callout_close']}</span></div>
  </div>
</div>


<div class="page">
  <div class="p2head"><h2>WHAT YOUR 304 REVIEWS SAY, <span>AND YOUR SITE DOESN&rsquo;T.</span></h2>
    <div class="p2sub">{g['revsub']}</div></div>
  <div class="body" style="padding-top:20px">
    <div class="kick">THE GAP, TERM BY TERM</div>
    <table><thead><tr><th>WHAT MEMBERS SAY, UNPROMPTED</th><th class="num">IN REVIEWS</th><th class="num">ON THE SITE</th></tr></thead>
    <tbody>{revrows}</tbody></table>
    <div class="srcline">{g['revsrc']}</div>
    <div class="kick" style="margin-top:16px">THE REVIEW YOU SHOULD READ TWICE</div>
    <div class="callout"><h4>{g['quote_h']}</h4><p>{g['quote']}</p>
      <span class="close">{g['quote_close']}</span></div>
    <div class="kick" style="margin-top:16px">NAPLES PERSONAL TRAINING, RANKED BY REVIEW VOLUME</div>
    <table><thead><tr><th>#</th><th>GYM</th><th class="num">RATING</th><th class="num">REVIEWS</th></tr></thead>
    <tbody>{comprows}</tbody></table>
    <div class="srcline">{g['compsrc']}</div>
  </div>
</div>

<div class="page">
  <div class="p2head"><h2>THE GRADE, <span>LENS BY LENS.</span></h2>
    <div class="p2sub">Six lenses, scored against what the live site actually does &mdash; not against taste. The right-hand column is points lost.</div></div>
  <div class="body" style="padding-top:22px">
    <div class="gcard">
      <div class="glabel">{g['name'].upper()} &nbsp;&bull;&nbsp; {g['handle'].upper()} &nbsp;&bull;&nbsp; {g['window']}</div>
      {grows}
      <div class="gtot"><div class="t">TOTAL: {total} / {mx}</div>
      <div class="pill" style="background:{pill(tl)}">{tl}</div></div>
    </div>
    <div class="kick" style="margin-top:17px">WHAT WE FOUND, LENS BY LENS</div>
    <table><thead><tr><th>LENS</th><th>THE EVIDENCE FROM THE LIVE SITE</th></tr></thead>
    <tbody>{trows}</tbody></table>
    <div class="diag"><h4>THE ONE-SENTENCE DIAGNOSIS</h4><p>{g['diag']}</p></div>
    <div class="method"><div class="mh">HOW THIS<br>WAS MEASURED</div><p>{g['method']}</p></div>
  </div>
</div>

<div class="page">
  <div class="p2head"><h2>THE FIX LIST, <span>RANKED BY RETURN.</span></h2>
    <div class="p2sub">{g['fixsub']}</div></div>
  <div class="body" style="padding-top:24px">
    <div class="kick">RANKED BY RETURN</div>
    <table><thead><tr><th>#</th><th>FIX</th><th>LENS</th><th class="num">IMPACT</th><th class="num">EFFORT</th></tr></thead>
    <tbody>{erows}</tbody></table>
    <div class="note">{g['note']}</div>
    <div class="kick" style="margin-top:14px">ALREADY WORKING. DO NOT TOUCH.</div>
    <div class="cards">{cards}</div>
    <div class="kick" style="margin-top:14px">WHY THIS GRADE IS GOOD NEWS</div>
    <div class="good"><h4>{g['goodh']}</h4><p>{g['good']}</p></div>
    <div class="cta">
      <div class="pre">{g['name'].upper()}, THIS IS THE WHOLE DECISION</div>
      <h3>{g['cta1']} <span>{g['cta2']}</span></h3>
      <p>{g['ctabody']}</p><a class="btn" href="{g["cta_url"]}">{g["cta_btn"]}</a>
    </div>
  </div>
  <div class="foot"><div class="l">LASSO FRAMEWORK<small>We chase. You close.</small></div>
  <div class="r">LASSOFRAMEWORK.COM</div></div>
</div>
</body></html>"""

if __name__=="__main__":
    D=json.load(open('example_site.json'))
    for slug,g in D.items():
        open(f"./{slug}.html","w").write(build(g))
        print("wrote",slug)

================================================================
==== FILE: lasso-website-audit/assets/example_site.json
================================================================

{
 "evolve": {
  "name": "Project Evolve",
  "t1": "PROJECT",
  "t2": "EVOLVE",
  "handle": "projectevolvenaples.com",
  "audit_date": "August 28, 2026",
  "window": "LIVE SITE, MEASURED AUG 28, 2026",
  "posts": "64 links, 8 images and 3 schema blocks inspected in a real browser.",
  "subline": "Six-lens website audit &mdash; StoryBrand, SEO, AI Search, UI, Conversion, Platform &mdash; measured element by element on the live site.",
  "short": "The short version: you are the most-reviewed personal training gym in Naples, and the nav item that says PRICING goes nowhere.",
  "prev_label": "LAST AUDIT",
  "cur_label": "MEASURED NOW",
  "source": "Every figure on this page was measured on August 28, 2026: page timings and DOM facts from two real-browser loads, Google listing and review data from the Apify Google Maps scrapers, and the site&rsquo;s root files fetched directly. Nothing here is estimated.",
  "hero": {
   "v": "304",
   "l": "Five-star Google reviews at a 5.0 rating &mdash; 2nd of 20 Naples gyms, and 2.3&times; your closest rival",
   "d": "5.0 &#9733;",
   "p": "verified live on Google Maps, Aug 28"
  },
  "tiles": [
   {
    "l": "Prices anywhere on the site",
    "v": "$0",
    "d": "&#9660; none found",
    "p": "0 dollar figures in 818 words",
    "good": false
   },
   {
    "l": "Dead links in the header nav",
    "v": "2",
    "d": "&#9660; GET STARTED, PRICING",
    "p": "12 instances across 3 headers",
    "good": false
   },
   {
    "l": "Coaches named on the homepage",
    "v": "1",
    "d": "&#9660; of 4 on staff",
    "p": "biggest AI-citation gap",
    "good": false
   },
   {
    "l": "Layout shift (CLS)",
    "v": "0.001",
    "d": "&#9650; excellent",
    "p": "2 shifts, well inside Google's bar",
    "good": true
   }
  ],
  "compare": [
   {
    "m": "Structured data (schema)",
    "b": "3 JSON-LD blocks live, including <b>ExerciseGym</b> and Organization &mdash; the correct type for a gym",
    "d": "PASS",
    "good": true
   },
   {
    "m": "robots.txt and sitemap",
    "b": "robots blocks only /wp-admin/ and allows every AI crawler. Sitemap live at /sitemap_index.xml, last modified <b>Aug 22, 2026</b>",
    "d": "PASS",
    "good": true
   },
   {
    "m": "On-page SEO basics",
    "b": "Title 63 characters, meta 139, canonical self-references correctly, and all 8 images carry alt text",
    "d": "PASS",
    "good": true
   },
   {
    "m": "Layout stability (CLS)",
    "b": "0.001 across two loads, 2 shifts &mdash; well inside Google&rsquo;s threshold",
    "d": "PASS",
    "good": true
   },
   {
    "m": "Tracking stack",
    "b": "Google Tag Manager, GA4, the Meta pixel and GoHighLevel all firing",
    "d": "PASS",
    "good": true
   },
   {
    "m": "Google Business Profile",
    "b": "<b>304 reviews at a 5.0 rating</b> &mdash; 2nd of 20 Naples gyms by volume",
    "d": "PASS",
    "good": true
   },
   {
    "m": "Address consistency (NAP)",
    "b": "Google renders your address as <b>&ldquo;Suite 203 Suite 203&rdquo;</b> &mdash; the suite line is duplicated",
    "d": "FAIL",
    "good": false
   },
   {
    "m": "llms.txt",
    "b": "Returns a 404. Absent, so AI engines get no reading instructions",
    "d": "FAIL",
    "good": false
   },
   {
    "m": "Time to first paint",
    "b": "Roughly <b>9 to 10 seconds</b> on 99&ndash;105 requests, against a 61ms server response",
    "d": "FAIL",
    "good": false
   },
   {
    "m": "Prices published",
    "b": "<b>Zero dollar figures</b> anywhere in 818 words of homepage copy",
    "d": "FAIL",
    "good": false
   },
   {
    "m": "Header navigation",
    "b": "<b>GET STARTED and PRICING are anchor links with an empty target</b> &mdash; 12 of 64 links go nowhere",
    "d": "FAIL",
    "good": false,
    "hl": true
   }
  ],
  "rc0": "CHECK",
  "rc1": "STATUS",
  "rc2": "",
  "rc3": "",
  "reach": [],
  "callout_h": "THE ONE THING TO FIX FIRST",
  "callout": "Your header has six items. Three of them &mdash; Location, Reviews, Blog &mdash; work. <b>The two that carry buying intent, GET STARTED and PRICING, are anchor links with an empty target.</b> They were built to jump to a section and the section ID was never filled in, so clicking either one scrolls the visitor back to the top of the page. They render three times each across the desktop, mobile and sticky headers, so 12 of your 64 homepage links go nowhere.",
  "callout_close": "This is not a redesign. It is two href values. A visitor who wants to give you money clicks PRICING, gets thrown back to the top, and leaves &mdash; and you have 304 five-star reviews proving they would have stayed.",
  "rails": [
   [
    "STORYBRAND",
    14,
    20,
    "The problem copy is genuinely good and stays: <b>&ldquo;ARE YOU TIRED OF: Working Hard But Your Body Isn&rsquo;t Responding?&rdquo;</b> hits external, internal and philosophical pain in one block, and the Starting Point Session is a real named first step. Then the H1 is <b>&ldquo;THE BEST PERSONAL TRAINING IN NAPLES FL&rdquo;</b> &mdash; an unprovable superlative where the member outcome should be. And the guide has no face: of at least six coaches your members name in reviews, <b>exactly one appears anywhere in 818 words of homepage copy.</b>"
   ],
   [
    "SEO",
    17,
    20,
    "Strong, and stronger than most gym sites we measure. Title is 63 characters and well formed, meta description 139, canonical is self-referencing and correct, viewport is set, 10 Open Graph tags, and <b>all 8 images carry alt text &mdash; zero missing.</b> robots.txt is clean and blocks nothing but /wp-admin/. The Yoast sitemap index is live with a post sitemap last modified <b>August 22, 2026</b>, which means the content engine is genuinely still running. Points off for the doubled suite line in the NAP and the thin 818-word homepage."
   ],
   [
    "AI SEARCH / GEO",
    11,
    15,
    "<b>Three JSON-LD blocks are live, including ExerciseGym and Organization</b> &mdash; ExerciseGym is exactly the right type and most gyms never ship it. robots.txt allows every AI crawler; nothing is blocked. The answer-first FAQ remains a real asset. What is missing is now confirmed rather than suspected: <b>llms.txt returns a 404</b>. And the authority gap is stark &mdash; your members name Joe, Marcon, Jake, Aja, Cody, Marc and Dianna across hundreds of reviews, while the site gives an AI engine one name and zero credentials to cite."
   ],
   [
    "UI / UX",
    9,
    15,
    "A clean, modern Elementor build with essentially no layout shift &mdash; <b>CLS measured 0.001 across two loads</b>, which is excellent and rare. Then the defects. Two header items are dead anchors. The grammar error <b>&ldquo;What Peoples Are Saying&rdquo;</b> is still live in the nav and the footer, on the page that holds your 304 reviews. There is still no dedicated contact page &mdash; Location &amp; Contact is a homepage anchor &mdash; and one phone link with no email address anywhere."
   ],
   [
    "CONVERSION",
    8,
    20,
    "The weakest lens and the reason for the grade. <b>Zero dollar figures appear anywhere on the page.</b> There is no form element on the homepage at all &mdash; capture runs entirely through eight Elementor popups. There is no embedded booking calendar, so every intro still waits on a human callback. The nav item labelled PRICING is a dead link. And the cost of that is not theoretical: <b>a prospective member wrote a public review explaining that they went elsewhere because nobody would give them a price.</b>"
   ],
   [
    "PLATFORM",
    7,
    10,
    "The stack is right and fully instrumented: WordPress and Elementor, with <b>Google Tag Manager, GA4, the Meta pixel and GoHighLevel all firing</b>, plus valid schema. Server response is quick when warm. The problem is what happens after: across two real-browser loads the page took <b>roughly 9 to 10 seconds to paint anything</b>, on 99 to 105 requests. Those timings varied between loads and belong in a controlled test before anyone quotes a single number &mdash; but nothing we measured came close to acceptable."
   ]
  ],
  "diag": "Project Evolve is the second most-reviewed personal training gym in Naples with a perfect 5.0 across 304 reviews &mdash; and the two header links a ready-to-buy visitor actually clicks, GET STARTED and PRICING, point at nothing.",
  "fixsub": "Work top down. The first three change revenue this week and none of them is a redesign. The rest protect credibility and compound.",
  "fixes": [
   {
    "t": "<b>Give GET STARTED and PRICING real destinations.</b> Both are anchor links with an empty target across all three headers. This is an href change, not a build.",
    "lens": "Conversion / UI",
    "imp": "HIGH",
    "eff": "Minutes"
   },
   {
    "t": "<b>Publish a starting price.</b> One honest number or range. Zero dollar figures exist on the site today, and price shoppers leave rather than fill a form to find out.",
    "lens": "Conversion",
    "imp": "HIGH",
    "eff": "Low"
   },
   {
    "t": "<b>Embed a booking calendar.</b> Every intro waits on a callback today. Self-booking closes the visitor while intent is hot.",
    "lens": "Conversion",
    "imp": "HIGH",
    "eff": "Medium"
   },
   {
    "t": "<b>Put your coaches on the site, using your members&rsquo; own words.</b> Joe, Marcon, Jake, Aja, Cody, Marc, Dianna. The reviews already supply the proof; the site supplies one name.",
    "lens": "StoryBrand / GEO",
    "imp": "HIGH",
    "eff": "Medium"
   },
   {
    "t": "<b>Fix the paint time.</b> Nine to ten seconds to first paint on 100+ requests, against a 61ms server response. The server is fine; what loads after it is not. Run a controlled test, then cut render-blocking assets.",
    "lens": "Platform",
    "imp": "HIGH",
    "eff": "Medium"
   },
   {
    "t": "<b>Fix the doubled suite line.</b> Google currently reads your address as &ldquo;Suite 203 Suite 203.&rdquo; One name, one suite, one phone, everywhere.",
    "lens": "SEO / GEO",
    "imp": "MED",
    "eff": "Low"
   },
   {
    "t": "<b>Correct &ldquo;What Peoples Are Saying&rdquo;</b> in the nav and the footer. It sits on the page holding 304 five-star reviews.",
    "lens": "UI",
    "imp": "MED",
    "eff": "Minutes"
   },
   {
    "t": "<b>Speak to the buyer your reviews describe.</b> Members self-identify at 58 to 85, arriving after arthritis, back surgery, knee replacement and open-heart recovery, and one is on a GLP-1 protecting muscle. The homepage addresses none of it.",
    "lens": "StoryBrand",
    "imp": "HIGH",
    "eff": "Low"
   },
   {
    "t": "<b>Put the reviews on the homepage.</b> 304 at a 5.0 average is the strongest proof you own and the homepage does not spend it.",
    "lens": "Conversion",
    "imp": "MED",
    "eff": "Low"
   },
   {
    "t": "<b>Ship llms.txt.</b> It returns a 404 today. Cheap to ship, and it is how AI engines are told what to read.",
    "lens": "GEO",
    "imp": "LOW",
    "eff": "Minutes"
   }
  ],
  "note": "Grades measure what the live site does for a visitor trying to become a member, scored out of 100. Page-timing figures come from two real-browser loads and varied between them; treat them as a range and confirm in a controlled test before quoting a single number. Every other figure on this audit was read directly from the live page, the live Google listing, or the site&rsquo;s root files.",
  "week": [
   [
    "#2 OF 20 IN NAPLES",
    "304 reviews at 5.0, second only to CrossFit Naples and 2.3x your nearest personal-training rival."
   ],
   [
    "THE CONTENT ENGINE",
    "Post sitemap last modified Aug 22, 2026. The expensive habit is already running."
   ],
   [
    "CORRECT SCHEMA",
    "ExerciseGym plus Organization, live and valid. Almost no gym site ships this."
   ]
  ],
  "goodh": "NOTHING ON THIS LIST IS A REBUILD",
  "good": "The expensive things are done. You have 304 five-star reviews, a live content engine, correct schema, a full tracking stack and a server that answers in 61 milliseconds. Your members have already written your coaches page, your age positioning, your results numbers and your price rebuttal &mdash; for free, on Google. The top fixes are two href values, one number, and copying words you already own.",
  "cta1": "YOU EARNED THE VISIT.",
  "cta2": "NOW LET THEM SAY YES.",
  "cta_url": "https://lassoframework.com",
  "cta_btn": "BOOK THE WALKTHROUGH",
  "ctabody": "Project Evolve is one of the better-built gym sites in Naples. The only thing between it and a steady flow of booked intros is a locked door. Book fifteen minutes and we will walk the fix list together.",
  "method": "Measured August 28, 2026. Page timings, DOM structure, link targets, schema blocks, image alt coverage and word counts were read from two real-browser loads of the live homepage. All 304 Google reviews, the listing name, address, phone, rating and review count, and the 20-gym Naples competitor set were pulled with the Apify Google Maps scrapers; the 130 most recent reviews were read in full for the language analysis. robots.txt, sitemap_index.xml and llms.txt were fetched directly and their status codes recorded. Where a check could not be completed it is named as open rather than answered. Page-timing figures varied between the two loads and are reported as a range rather than a single number; nothing else on this audit is estimated.",
  "revsub": "We pulled all 304 of your Google reviews and read the 130 most recent. Your members have already written the website you should have. Here is what they say that your homepage does not.",
  "revgap": [
   {
    "t": "Coach <b>Joe</b> named by members",
    "a": "~30 reviews",
    "b": "0 mentions",
    "bad": true
   },
   {
    "t": "Coach <b>Marcon</b> named by members",
    "a": "~26 reviews",
    "b": "0 mentions",
    "bad": true
   },
   {
    "t": "Coach <b>Aja</b>, plus Cody, Marc and Dianna",
    "a": "named repeatedly",
    "b": "0 mentions",
    "bad": true
   },
   {
    "t": "<b>Jake</b> (owner) named by members",
    "a": "~13 reviews",
    "b": "1 mention",
    "bad": true
   },
   {
    "t": "Members who state their age &mdash; 58, 60, 64, 69, 70, 75, 85",
    "a": "10+ reviews",
    "b": "no age mentioned",
    "bad": true
   },
   {
    "t": "Medical context &mdash; arthritis, back surgery avoided, knee replacement prep, open-heart recovery",
    "a": "6+ reviews",
    "b": "not addressed",
    "bad": true
   },
   {
    "t": "<b>GLP-1 / Tirzepatide</b> muscle retention",
    "a": "named explicitly",
    "b": "0 mentions",
    "bad": true
   },
   {
    "t": "Hard results &mdash; &ldquo;16 lbs of fat, 4 lbs of muscle in 32 sessions,&rdquo; &ldquo;dropped over 30 pounds,&rdquo; &ldquo;close to 40lbs&rdquo;",
    "a": "quantified by members",
    "b": "no numbers on page",
    "bad": true
   },
   {
    "t": "&ldquo;<b>Gymtimidation</b>&rdquo; &mdash; a member&rsquo;s own word for the core objection",
    "a": "member-written",
    "b": "not used",
    "bad": true
   },
   {
    "t": "Small-group cap of 6",
    "a": "praised repeatedly",
    "b": "<b>on the site</b>",
    "bad": false
   }
  ],
  "revsrc": "Counts are from the 130 most recent of your 304 reviews, pulled via the Apify Google Maps Reviews scraper on August 28, 2026. Site counts are from the live homepage, 818 words. Coach-name tallies are approximate because members spell names inconsistently; the direction is not in doubt.",
  "quote_h": "A LOST LEAD, PUBLISHED ON YOUR OWN LISTING",
  "quote": "&ldquo;They would not give me a ballpark, guesstimate, any hint of a price of their memberships&hellip; I felt that they were trying to get me in to trap me rather than being open about their pricing. <b>Not sure why the pricing is so secretive.</b> I&rsquo;m not even in the area yet, just trying to do my research to make my gym decision upon arrival and <b>their phone call deterred me.</b>&rdquo;",
  "quote_close": "This is not our opinion about pricing. It is a prospective member, in their own words, on your Google listing, explaining that they chose another gym because they could not find out what yours costs. Meanwhile another member does the price framing for you: &ldquo;One on one personal training has gotten expensive, and to have this level of attention at a fraction of the cost is really beneficial.&rdquo; That sentence belongs on the pricing page you do not have.",
  "comps": [
   {
    "n": "CrossFit Naples",
    "r": "5.0",
    "c": "383"
   },
   {
    "n": "<b>Project Evolve Personal Training</b>",
    "r": "5.0",
    "c": "304",
    "me": true
   },
   {
    "n": "D1 Training Naples",
    "r": "4.8",
    "c": "219"
   },
   {
    "n": "Real Fitness Naples",
    "r": "4.9",
    "c": "150"
   },
   {
    "n": "BILD By Coach O",
    "r": "5.0",
    "c": "131"
   },
   {
    "n": "Galaxy Fit Lab Personal Training",
    "r": "4.9",
    "c": "121"
   },
   {
    "n": "Pure Skill Fitness",
    "r": "5.0",
    "c": "118"
   },
   {
    "n": "Max Flex Fitness",
    "r": "5.0",
    "c": "104"
   },
   {
    "n": "PrimeF.A.S.T",
    "r": "5.0",
    "c": "91"
   },
   {
    "n": "Optimal Fitness Community",
    "r": "5.0",
    "c": "84"
   }
  ],
  "compsrc": "Top 10 of 20 Naples personal-training gyms returned by the Apify Google Maps scraper on August 28, 2026 across three search terms. Project Evolve ranks 2nd of 20 by review volume and is tied for the highest rating in the set. Against its two closest personal-training rivals &mdash; BILD (131) and Galaxy Fit Lab (121) &mdash; Project Evolve holds 2.3 times the review volume.",
  "cmpkick": "THE TECHNICAL BASELINE"
 }
}
