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
- Read it straight from the existing `/portal/<token>/social` calendar payload if it
  surfaces `scheduled_at` (add it to the select if not), or query the column directly.
- When `scheduled_at` is null, fall back to the date only (don't show a fabricated time).

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
