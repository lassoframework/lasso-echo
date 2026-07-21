# Podcast auto-ingest (deployed Monday job) — setup + process

Echo pulls the newest podcast episode from a Google Drive folder every Monday,
edits it headless, and drops the week's clips into Slack as HELD approval cards.
Nothing publishes. Everything is behind `AGENT_PODCAST_AUTO_ENABLED` (default OFF).

## ARCHITECTURE (read this first — do NOT make a separate Railway service)

The Monday run happens INSIDE the main Echo listener service's scheduler (the same
loop that runs the episode inbox, opus poll, etc.). It is NOT a separate Railway
cron service.

Why: a Railway volume attaches to exactly ONE service. The drafts live in
`/data/echo.db` on the main service's volume, and the Slack Approve button is
handled by that same service. A second service cannot mount that volume, so a
separate cron service writes to a different (empty) database and Approve says
"not found" — plus, pointed at the repo, it boots a SECOND full Echo daemon
(duplicate Slack listener). So the schedule lives in-process on the main service.

To arm it: set the env vars below ON THE MAIN ECHO SERVICE and set
`AGENT_PODCAST_AUTO_ENABLED=true`. The scheduler fires it every Monday at
`AGENT_DAILY_HOUR_UTC` (default 14:00 UTC, ~10am ET). No cron service, no cron
schedule string.

Manual one-off (for testing) still works from the main service shell:
`python -m agent podcast-auto`

---

## Weekly process (once set up)

1. Record in Riverside, then export the finished episode to Drive: Exports tab ->
   Share -> Drive icon -> Save to Google Drive. This is ONE manual click per
   episode (Riverside has no scheduled auto-push; see "Riverside reality" below).
2. Monday ~9am ET the cron pulls the newest episode, edits it, and spreads up to
   5 clips one-per-day across the next posting days as HELD drafts.
3. Each clip posts a Slack approval card to the approver (U06EPUUCL13).
4. You tap Approve. Because it runs on Railway (same DB the listener reads),
   Approve sticks and the card flips to "Approved."
5. Nothing goes live until `AGENT_PUBLISH_ENABLED` is armed AND Meta is connected.
   Until then, Approve = green-lit, not posted.

No file labeling needed. Echo pulls the newest video by created time, so just
get the episode into the folder.

## Riverside reality (read this — it shapes the folder setup)

Riverside does NOT have automatic/scheduled export to Google Drive. It is manual,
one click per export (Exports -> Share -> Drive). Two hard constraints:

- You CANNOT choose the destination folder. Every Drive export lands in a single
  folder named `Riverside` that Riverside auto-creates in My Drive.
- It exports rendered exports (finished episode/clips), not raw recordings.

Because of this, point Echo at that forced `Riverside` folder rather than a folder
you make yourself:
  1. Do one export to Drive to make the `Riverside` folder appear.
  2. Copy that folder's ID from its URL.
  3. Set `AGENT_PODCAST_DRIVE_FOLDER_ID` to the `Riverside` folder ID.
  4. Share the `Riverside` folder with the service account (Viewer).

GOTCHA: Echo grabs the NEWEST video in the folder. Everything you export from
Riverside piles into that one folder, so export ONLY the full episode to Drive
(or export it last), or Echo may grab a clip instead of the episode.

Truly zero-click is only possible with a third-party automation (Zapier/Make)
watching the `Riverside` folder. Riverside itself can't do it. Optional add-on.

Headless note: the cron cannot use Higgsfield (interactive auth), so auto reels
get the animated intro card + word-highlight captions + Treatment B side panels
+ Nano still cards. Full motion b-roll stays a manual `video-episode` run.

---

## One-time setup

### 1. Google Drive folder
- Do NOT hand-make a folder for this. Use the `Riverside` folder Riverside creates
  in My Drive the first time you export to Drive (you cannot change Riverside's
  destination). See "Riverside reality" above.
- Folder ID = the chunk after `/folders/` in the URL:
  `drive.google.com/drive/folders/`**`<FOLDER_ID>`**
- Share that `Riverside` folder with the service account (Viewer).

### 2. Google Cloud service account (headless credential)
- Console: https://console.cloud.google.com/apis/credentials (project `lasso-echo`).
- Enable the Drive API first:
  https://console.cloud.google.com/apis/library/drive.googleapis.com
- Create credentials -> Service account (e.g. `echo-podcast-puller`); skip roles.
- On the service account -> Keys -> Add key -> Create new key -> JSON. Download it.
- Copy the service account email (ends `@lasso-echo.iam.gserviceaccount.com`).
- Share the Drive folder with that email as Viewer. (Without this the key sees
  nothing.)
- The JSON key is a SECRET. It goes only into the Railway env var below. Never
  commit it or paste it into Slack. If it leaks, delete the key and make a new one.

### 3. Railway env vars (on both the listener service and the cron service)
| Var | Value |
|-----|-------|
| `AGENT_PODCAST_AUTO_ENABLED` | `true` |
| `AGENT_PODCAST_DRIVE_FOLDER_ID` | the folder ID from step 1 |
| `AGENT_GDRIVE_SA_JSON` | the FULL contents of the JSON key file (whole `{ ... }` blob), or a path to it |
| `AGENT_PODCAST_ACCOUNT_KEY` | optional; account the clips post under (default: episode-inbox tenant) |
| `AGENT_PODCAST_AUTO_MAX_CLIPS` | optional; clips per episode (default 5) |

Plus the same vars the editor + listener already use: `AGENT_DB_PATH=/data/echo.db`
(shared volume, so drafts land where Approve can find them), Slack token, R2/S3
media host creds, Anthropic + Deepgram keys.

### 4. No separate service — arm it on the MAIN service
Do NOT create a second Railway service (see ARCHITECTURE above). Instead, on the
existing main Echo listener service:
- Set `AGENT_PODCAST_AUTO_ENABLED=true` plus the podcast vars in the table above.
- The in-process scheduler fires it every Monday at `AGENT_DAILY_HOUR_UTC`
  (default 14:00 UTC). Adjust that env var if you want a different Monday hour.
- Turn OFF the legacy R2 episode inbox: `AGENT_EPISODE_INBOX_ENABLED=false`. It
  overlaps with this job (both ingest `lasso_episodes`) and, left on, double-
  processes the staged file and spams "processing failed" alerts.
- If you created a separate "echo podcast" cron service already, DELETE it — it
  can't see the volume and runs a duplicate daemon.

---

## First test (do this once, do not wait for Monday)
- From the MAIN Echo service shell (Railway: service -> ... -> Shell, or
  `railway ssh` into it), run: `python -m agent podcast-auto`
- It runs the full pipeline on the newest Drive episode and posts real cards to
  the store the Approve button reads.
- Approve one and confirm the card flips to "Approved" (proves the store fix).
- It will NOT publish (publish gate still OFF by design).

## Gotchas
- Set the vars on the MAIN service. A separate service can't see `/data/echo.db`.
- Turn the legacy episode inbox OFF (`AGENT_EPISODE_INBOX_ENABLED=false`) or it
  double-processes and spams alerts.
- Code must be deployed. If `podcast-auto` errors as unknown, the remote is behind
  — push `main`.
- If the job logs a `PodcastSourceError`, read it: it names the exact missing
  piece (folder id, key, folder not shared, or Drive API not enabled).
- Transcription: set `AGENT_TRANSCRIBE_API_KEY` (Deepgram) or the run falls back
  to local Whisper, which is very slow on a CPU box (many minutes per episode).
