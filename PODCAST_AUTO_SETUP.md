# Podcast auto-ingest (deployed Monday job) — setup + process

Echo pulls the newest podcast episode from a Google Drive folder every Monday,
edits it headless, and drops the week's clips into Slack as HELD approval cards.
Nothing publishes. Everything is behind `AGENT_PODCAST_AUTO_ENABLED` (default OFF).

Run command: `python -m agent podcast-auto`

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

### 4. Railway cron service
The main Echo service runs the Slack listener 24/7 — do not change its command.
Add the cron as its OWN service in the same project:
- + New -> GitHub Repo -> `lassoframework/lasso-echo` (same repo).
- Settings:
  - Custom Start Command: `python -m agent podcast-auto`
  - Cron Schedule: `0 13 * * 1`  (Mondays ~9am ET; Railway crons run in UTC)
- Variables: reference-copy all vars from the main service, plus the podcast ones.
  Point it at the same `AGENT_DB_PATH=/data/echo.db`.

---

## First test (do this once, do not wait for Monday)
- In Railway, trigger the cron service by hand (Run now / redeploy once).
- It runs the full pipeline on the newest Drive episode and posts real cards.
- Approve one and confirm the card flips to "Approved" (proves the store fix).
- It will NOT publish (publish gate still OFF by design).

## Gotchas
- Env vars alone do NOT trigger the job. The cron service is what runs it.
- Code must be deployed. If `podcast-auto` errors as unknown, the remote is behind
  — push `main`.
- If the job logs a `PodcastSourceError`, read it: it names the exact missing
  piece (folder id, key, folder not shared, or Drive API not enabled).
