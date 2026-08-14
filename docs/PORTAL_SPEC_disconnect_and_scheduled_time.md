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
