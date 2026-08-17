# Portal build spec — Disconnect button + Go-live time display

Two UI features whose **backend + data are already live in Echo**. This is for whoever
owns the portal (Vercel / Next.js) repo. Nothing here needs Echo changes.

Echo base URL (the worker that brokers Zernio): the same host the portal already calls
for `social-connect` / `social-status` / `facebook-pages`. Every route is token-scoped:
`/portal/<token>/...` where `<token>` is the gym's existing portal token.

---

## 1. Disconnect / switch a social account

**Problem it solves:** a gym owner connects the wrong account (e.g. a personal or a
spouse's Instagram). Today they can't undo it from the portal. This adds a Disconnect
button so they can remove it and reconnect the right one.

### Endpoint (LIVE in Echo)
```
POST /portal/<token>/social-disconnect?platform=instagram
POST /portal/<token>/social-disconnect?platform=facebook
```
- No request body.
- **200** `{ "ok": true, "disconnected": 1, "platform": "instagram" }` — removed.
- **200** `{ "ok": true, "disconnected": 0, "detail": "nothing connected" }` — nothing
  was connected for that platform (idempotent; safe to call again).
- **400** bad/missing platform. **403** Zernio not configured. **404** unknown/revoked
  token. **502** `{ "error": "zernio ...", "detail": ... }` — Zernio call failed.

What Echo does on success: deletes that account from the gym's Zernio profile
(Zernio `DELETE /v1/accounts/{id}`), clears the connection snapshot the dashboard reads
(`echo_social_connections` → `state: not_connected, handle: null`), and (for Facebook)
forgets the stored page binding so a fresh page is picked on reconnect.

### UI
- On the social settings / connections panel, next to each **connected** platform show a
  **Disconnect** button (a small "switch account" affordance).
- Confirm dialog: "Disconnect @{handle} from {Gym}? You'll be able to reconnect the
  right account after." → on confirm, `POST …/social-disconnect?platform=…`.
- On 200, re-fetch `GET /portal/<token>/social-status` (already used) and re-render.
  The platform will show **not connected** with the existing **Connect** button, which
  the owner then uses to connect the correct account.
- Show the 502 detail as a soft error ("Couldn't reach Zernio, try again"); leave the
  UI unchanged on failure.

### Note
`social-status` reads Zernio **live**, so after a disconnect + reconnect the handle shown
is always the real one. The `echo_social_connections` snapshot is a display cache that
Echo now keeps in sync on disconnect.

### ⚠️ Portal-side BUG to fix (found live 2026-08-12, GritX)
The portal's own status-sync job is writing `echo_social_connections` rows with
**`state='connected'` and `handle=null`** when Zernio actually has **no account** for that
platform. Repro: a gym disconnects its only IG (Zernio `list_accounts` returns []), Echo's
`GET /portal/<token>/social-status` correctly returns `instagram.connected=false`, but the
portal sync overwrites the snapshot back to `connected` a short time later, so the dashboard
shows a phantom "Connected" with no handle and hides the Connect button — the owner is stuck.

Two fixes on the portal side:
1. **Trust `social-status`** as the source of truth (it reads Zernio live), or fix the sync
   so a null/absent handle NEVER maps to `connected`. A row with no account id and no handle
   is `not_connected`, full stop.
2. Render the platform as **not connected** whenever `handle` is null, and always show the
   **Connect** button in that state (even if a stale `state` says connected), so an owner is
   never trapped by a phantom-connected display.

Until this is fixed, a stuck gym can be reconnected with a direct Zernio connect link Echo
mints via `GET /portal/<token>/social-connect?platform=instagram` (returns the OAuth URL).

---

## 2. Show clients WHEN each post goes live

**Problem it solves:** the calendar shows a date but not a time, so clients don't know
when a post publishes. Echo now stamps every row's exact go-live time.

### Data (LIVE in Supabase)
New column on `content_calendar`:
```
scheduled_at  timestamptz   -- the post's planned go-live time (ISO 8601, tz-aware)
```
- Echo stamps it deterministically from the post's date + its stable slot time in
  **America/New_York**: 3 slots per day — **07:30, 12:30, 18:30 ET** (stories midday;
  feeds spread AM/PM by a stable hash of the row id, so a post's time never moves).
- It is stamped on every calendar row, **including rows still waiting on approval**, so
  the client sees the time *before* they approve.
- Display metadata only — it never affects status/approval. `null` only on very old rows
  or the brief window before the day's first publish sweep stamps them.

### UI
- Wherever a calendar cell / list row shows the date, also show the time from
  `scheduled_at`, formatted in the gym's local timezone (or ET with a "ET" label):
  e.g. **"Aug 13 · 7:30 AM"**.
- The `/portal/<token>/social` payload now ALWAYS carries `scheduled_at` per post —
  when the DB stamp is absent Echo synthesizes it from the post's own deterministic
  slot, so the UI never needs a fallback for missing times.
- Each post in that payload also now carries:
  - `platform` — `"instagram"` or `"facebook"` (a feed cross-posted to both is two
    rows; use this for the IG/FB badge instead of guessing)
  - `published_at` — when it actually went live (null until published)
  - `late_post_id` — the vendor post id once published
  - `media_kind` — `"video"` or `"image"`. **A video URL inside an `<img>` tag renders
    a BLANK card** (clients reported "no photo preview"). When `media_kind == "video"`,
    render `<video src={image_public_url} muted playsinline preload="metadata">`
    (optionally with `controls`) instead of an `<img>`.
- Status chips: the `status` field can also be `publishing` (in flight, seconds-long)
  and `killed` — render `publishing` like Approved with a spinner, `killed` however
  denied/removed content is shown. Any action on a `published` or `publishing` post
  returns **409** — disable the buttons for those states.
- **KILL now requires confirmation on EVERY route** (Blake ruling 2026-08-13): both
  `POST /portal/<token>/kill` and `POST /portal/<token>/posts/<id>/kill` refuse with
  **400** unless the JSON body carries `"confirm": true`. Show a "This permanently
  removes the post — are you sure?" dialog and send `confirm: true` only on yes. If
  the portal's kill button currently sends no confirm field, it will start getting
  400s — update it.

### Copy suggestion
On the calendar header or a tooltip: "Posts publish at 7:30 AM, 12:30 PM, or 6:30 PM ET
on their scheduled day. Approve any post and it goes out automatically at its time."

---

## Autonomy toggle (already wired — FYI, no portal work needed)
The **Echo / Autonomous** toggle already writes `echo_gym_settings.autonomous`. Echo now
reads it **per gym**: when ON, that gym's approved-or-pending posts publish on their own
at slot time; when OFF, every post waits for the client's approval. No portal change
needed — just confirm the toggle keeps writing `echo_gym_settings.autonomous` for the
gym in view.

---

## 3. NON-NEGOTIABLE: never destroy or reword an approved post (portal side)

**Problem it solves:** Dale (CrossFit ENG) approved posts, then on reload they (1) went
back to "waiting on you", (2) came back with different caption wording, and (3) some
approved posts disappeared. The Echo-side cause (a nightly rebuild deleting the whole
gym-month) is **fixed and deployed** — Echo now leaves any human-owned row untouched. The
portal must uphold the same invariant so it can never re-introduce the bug from its side.

**Invariant:** once a `content_calendar` row has a human-owned status — anything NOT in
`{pending, draft, queued, null}` (i.e. `approved`, `denied`, `killed`, `published`,
`publishing`, `failed`) — the portal must **never DELETE it, never re-INSERT over it, and
never change its `caption`**. The row is frozen at the moment the client acts.

Portal rules:
- **Approve / deny / kill** must be a `PATCH` that sends **only** `{ status: ... }` (plus
  an optional note column). Never send `caption`, never delete-and-recreate the row to
  change status. (Echo's own action endpoints already behave this way — match them.)
- **Do NOT run any client-side "sync"/"reseed" that deletes or bulk-rewrites
  `content_calendar` rows.** Only Echo writes the calendar (and it now preserves approvals).
  If the portal has any such job, remove it or scope it to `status in (pending,draft)`.
- **Editing a caption** is allowed only when the client themselves edits a *pending* draft.
  A caption on an approved row is locked; if the client wants to change it, flip it back to
  pending first (explicit action), then edit.

Acceptance: approve a post → hard-reload → status still Approved, caption byte-identical.
Wait past the next rebuild tick → still Approved, still same wording, still present.

---

## 4. Edit UX: reflect the save WITHOUT a manual refresh (portal side)

**Problem (Dale, CrossFit ENG, 2026-08-14):** "The edit is not sticking... you DO have to
refresh the screen to see the edits stick. And twice the system kicked me out." Verified
Echo-side: **the edit DOES persist** — `POST /portal/<token>/posts/<id>/edit` writes the
new caption and returns it. The card just doesn't update until a full page reload.

Portal fixes:
- The edit endpoint returns `{ ok, caption, status }`. **Use that response to update the
  card in place** (optimistic update / re-render from the returned `caption` + `status`),
  so the new wording shows immediately — no manual refresh. Editing also flips the row to
  `pending` (returned in `status`); reflect that chip change too.
- **The "kicked out" logouts:** investigate the portal session/token lifetime on the edit
  POST. The Echo token is long-lived; a mid-edit logout is a portal session-expiry or an
  auth round-trip on submit. Repro: open a post, edit, submit — the user should stay in.
- Note: Echo now also **learns** from every edit (records the before→after into the gym's
  brain so future captions match the approver's taste). No portal change needed for that;
  just keep sending the new caption as the edit `note`.

### 4b. The "reason why" field (Dale, CrossFit ENG, 2026-08-15)

**Problem:** "Took a fair amount of editing, approving, and refreshing to save the revised
copy... after refreshing the reason why feedback field disappeared, not sure Echo captured
my reasoning (may have had to double-enter)."

Two sides:

- **Portal side (Vercel) — the field disappearing on refresh:** the "reason why" textarea is
  losing its value on the re-render/refresh. This is the same class of issue as §4: the
  portal must (a) keep the reason field's value in component state so a re-render does not
  clear it, and (b) send it on submit and clear it only AFTER a `200`. The "double-enter"
  and the disappearing field are portal state/refresh bugs, not an Echo persistence bug.

- **Echo side — now CAPTURES the reason (fix 2026-08-15):** the edit endpoints
  (`POST /portal/<token>/posts/<id>/edit` and `POST /portal/<token>/edit`) now accept an
  OPTIONAL **`reason`** field in the JSON body (aliases `why` / `reason_why` also accepted),
  distinct from the new caption `note`. When present, Echo records it into the gym's brain as
  the edit's **style rule** (`tenant_brain` `edit_diff.rule`), so the approver's stated
  intent teaches the next caption directly — not only inferred from the before/after diff.
  The edit response now echoes **`reason_captured: true|false`** so the portal can confirm
  Echo received it (show a small "reasoning saved" tick). If the portal sends only `note`
  (no reason), behavior is unchanged; the reason is purely additive.
  - **Portal action:** send the reason-why textarea value as `reason` in the edit POST body,
    and use `reason_captured` in the response to confirm capture to the user (removing the
    "not sure Echo captured my reasoning" doubt). Fabrication rule still applies to the
    caption `note` (422 on an uncleared figure); the `reason` is style guidance and is
    fabrication-gated only when it is later folded into a prompt.

### 4c. Learning durability (Echo side, fix 2026-08-15)

The tenant brain (edit diffs, reasons, deny reasons, kills) previously wrote to a
repo-relative `brains/` dir which, on the deployed worker, resolved to `/app/brains` —
inside the container image and **wiped on every redeploy**. So a gym's accumulated edit
learning silently reset each deploy. Echo now roots the brain under the persistent `/data`
volume (`AGENT_TENANT_BRAIN_DIR`, default `<DATA_DIR>/brains`), so edits and reasons survive
deploys and the loop actually compounds. No portal change; noted so ops sets the volume path
if a custom mount is used.

## 5. Round 2 Dale (CrossFit ENG) beta feedback (reviewed Aug 18 post, 2026-08-17)

### 5a. The "reason why" text leaked INTO the caption body (PORTAL BUG)

**Symptom (Dale):** after refresh the edited caption stuck, BUT the text typed in the
"Why" reason field was pasted directly BELOW the updated post copy — the reason leaked
into the caption body.

**Root cause: this is a PORTAL bug, proven backend-clean.** Echo's edit endpoints write
the caption and the reason to two SEPARATE places and NEVER concatenate them:

- `content_calendar.caption` is set to EXACTLY the `note` (new caption) via
  `SupabaseCalendarStore.patch_caption` — `json={"caption": new_caption, ...}` with no
  reason appended (`agent/portal_calendar_store.py`).
- the `reason` is recorded only as the edit's teaching **rule** in the gym's tenant brain
  (`_learn_from_edit` -> `tenant_brain.edit_diff.rule`), never returned in `caption`.
- Regression tests pin this: `tests/test_edit_reason_no_caption_leak.py` asserts the
  persisted `caption` equals the note byte-for-byte and does NOT contain the reason text,
  on BOTH edit routes (`/posts/<id>/edit` and the legacy `/portal/<token>/edit`).

**Portal action (REQUIRED):** the frontend is concatenating the reason into the caption it
displays (or sends). Fix the edit form so:
  - the "Why" textarea value is sent ONLY as the `reason` body field, NEVER appended to the
    `note`/caption field;
  - after save, render the caption from the response's `caption` field alone (do not
    re-append the reason locally). Echo's response returns `{caption, status,
    reason_captured}` — `caption` is the clean post copy; show the reason separately (e.g. a
    muted "Why: …" line under the card) and use `reason_captured: true` to show a small
    "reasoning saved" tick so there is a clear signal Echo received it.

### 5b. Approving one day marks the NEXT day "Approved" too (PORTAL BUG)

**Symptom (Dale):** approving one day's post auto-advances the UI to the next day and shows
that next post as "Approved" even though it was never approved; a refresh reveals it is NOT
approved.

**Root cause: PORTAL optimistic-state bug, proven backend-clean.** Echo's approve endpoint
marks EXACTLY the row whose id was submitted and never the next day's row:

- `POST /portal/<token>/posts/<id>/approve` flips ONLY `content_calendar.id == <id>` (and
  `gym_id == account_key`) to `approved` via `SupabaseCalendarStore.set_status`, whose PATCH
  is filtered by `id=eq.<id>` AND `gym_id=eq.<account_key>` — a single row
  (`agent/portal_calendar_store.py`). It never advances a cursor and never touches a sibling.
- Regression test `tests/test_approve_marks_only_target_row.py` submits day N's id against a
  two-day calendar and asserts (i) exactly one PATCH went out, (ii) it carried day N's id,
  (iii) day N+1's row is untouched (still its prior status).

**Portal action (REQUIRED):** the frontend is optimistically painting the NEXT card as
"Approved" after an approve (a UI cursor advance + a status carry-over). Fix so:
  - an approve updates ONLY the card whose id was approved; do NOT copy its new status onto
    the next card when auto-advancing;
  - drive each card's badge from the server response for THAT id (or a re-fetch), never from
    the previously-approved card's state. Auto-advancing the view is fine; carrying the
    "Approved" badge to the next post is the bug.

### 5c. A saved STORY caption must show on the story (Echo side, FIXED 2026-08-17)

**Symptom (Dale):** the Monday Aug 17 story showed no caption even though he added a story
caption and saved.

**Root cause (Echo side):** a story publishes with an EMPTY body, so its caption lives only
on the burned MEDIA. When a client edits a story caption in the portal, `patch_caption`
updates `content_calendar.caption` but the already-hosted `image_url` still carries the OLD
(or no) caption, and the publisher shipped `image_url` verbatim.

**Echo fix (no portal change needed):**
  - the publisher now HOLDS a story whose rendered media does not carry its CURRENT caption
    (detected schema-free: the burned story media filename embeds the caption key), so a
    story is NEVER shipped stale/blank — it waits for a re-render (`calendar_autopublish
    ._story_media_is_stale`).
  - the calendar rebuild now RE-RENDERS the story with the client's EDITED caption (read from
    the existing story row) instead of overwriting it with the freshly generated feed caption
    (`client_month_run._edited_story_captions` + `_maybe_format_story`).
  - Tests: `tests/test_story_caption_saved_shows.py`.

**Instant re-render — NOW BUILT (Echo side, 2026-08-17), gated on one migration.** Echo can
re-burn a story caption synchronously at edit time. Sequence:
  1. Apply `migrations/DRAFT_content_calendar_source_media_url.sql` (adds a nullable
     `content_calendar.source_media_url text`; additive, no backfill, changes nothing alone).
  2. Set `AGENT_STORY_SOURCE_MEDIA=true` on the `echo` + `echo-intake-web` services.
  Then: the planner stores each story's raw source url; a story-caption edit re-burns the new
  caption onto fresh media immediately and swaps `image_url`; the edit response carries
  `story_reburned: true`. Best-effort — the caption edit persists regardless, and the monthly
  rebuild remains the backstop. Until the flag is armed, behavior is exactly as today.

---

## 6. Task #28 — Echo backend delivered; the exact frontend diff to apply

Echo now returns SERVER-TRUTH on every action so the portal can stop guessing. The two
symptoms below are frontend optimistic-state bugs; the backend was already correct and is now
also easier to bind to. **Apply this diff, then run the normal build/audit/fix loop on it.**

### What Echo returns now (contract — no portal request changes needed to READ these)
- `GET /portal/<token>/social` — each post already carries authoritative `id`, `day_key`,
  `status`. Bind each card's badge to ITS OWN `post.status`.
- Every action `POST` response now includes the **written row's** `status` + `day_key`:
  - `approve`/`deny`/`kill`/`requeue` → `{ok, action, draft_id, status, day_key, ...}`
  - `edit` → `{ok, action, draft_id, caption, status, day_key, reason, reason_captured,
    story_reburned, gbp_updated}`. `caption` is the clean post copy (NEVER the reason);
    `reason` echoes the "Why" text; `reason_captured` is a saved-tick boolean.

### 6a. Fix 5a — the "Why" reason must never touch the caption
```diff
  // edit submit
- body: JSON.stringify({ draft_id, actor_id, note: captionText + "\n" + whyText })   // BUG: reason concatenated
+ body: JSON.stringify({ draft_id, actor_id, note: captionText, reason: whyText })    // reason is its OWN field
  // on response:
- setCaption(prev => prev + "\n" + whyText)          // BUG: re-appends the reason locally
+ setCaption(res.caption)                             // render the clean caption from the server ONLY
+ setWhy(res.reason)                                  // keep the "Why" field populated (no more disappearing)
+ setReasonSaved(res.reason_captured)                 // show a small "reasoning saved ✓" tick
  // render the reason as a SEPARATE muted line under the card, e.g. <p className="why">Why: {why}</p>
```

### 6b. Fix 5b — approving one card must not mark the next card approved
```diff
  async function onApprove(id) {
    const res = await postAction(`${id}/approve`)
-   advanceToNextCard()
-   setCards(cs => cs.map(c => c.selected ? { ...c, status: "approved" } : c))  // BUG: paints the NOW-selected (next) card
+   // update ONLY the row the server actually wrote, keyed by the returned draft_id:
+   setCards(cs => cs.map(c => c.id === res.draft_id ? { ...c, status: res.status } : c))
+   advanceToNextCard()                                // advancing the VIEW is fine; carrying the badge is the bug
  }
  // Each card badge is driven by its own status (from /social or the per-id action response),
  // never copied from the previously-approved card. On refresh nothing changes because the
  // server was always the source of truth.
```

### 6c. Fix 5c — show the re-burned story image after an edit (once the migration is live)
```diff
  const res = await postAction(`${id}/edit`, { note, reason })
  setCaption(res.caption)
+ if (res.story_reburned) {
+   // Echo re-burned the caption onto fresh media; refresh this card's image from the server
+   refetchPost(id)            // or: setCard(c => ({ ...c, image_public_url: undefined })) then re-GET /social
+ }
```
Until `AGENT_STORY_SOURCE_MEDIA` is armed, `story_reburned` is always `false` and the story
re-renders on the monthly rebuild (unchanged) — so this branch is safe to ship immediately.

**Backend tests proving the contract (all green here):** `tests/test_portal_social_supabase.py`
(reason echo + per-target authoritative status), `tests/test_portal_calendar_supabase.py`
(action responses carry status+day_key; edit reason echoed), `tests/test_story_reburn.py`
(gated re-burn, best-effort, pre-migration safety).
