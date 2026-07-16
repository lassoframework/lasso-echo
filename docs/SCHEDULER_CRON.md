# Scheduler cron fallback — Railway deploy runbook

The in-listener scheduler (`_daily_scheduler`) fires `run-daily` once per day at
`AGENT_DAILY_HOUR_UTC` (default `14`, ~10am ET). A Railway env-var save causes a
redeploy, which historically landed AFTER the fire window and silently missed the
draw. The `>=` fire condition (merged 2026-07-16) fixes this for restarts: any
restart on or after the target hour fires immediately if today has not run yet.

The **Railway cron service** is the belt-and-suspenders layer. It runs
`run-daily` once on a fixed schedule, independent of the listener process. If both
fire on the same day, `run-daily` is idempotent: it reads the persisted `last_run_date`
and exits clean when today's draw has already happened.

## Root cause (diagnosed 2026-07-15)

The old fire condition was `now.hour == target_hour`, a 60-minute window. Any
Railway redeploy triggered after 14:59 UTC caused the draw to be silently skipped
for the rest of the calendar day. Evidence: two production misses on 2026-07-15 and
2026-07-16 at 164 min and 589 min past target.

Fixed in commit on 2026-07-16:
- `now.hour == target_hour` changed to `now.hour >= target_hour` in `_daily_scheduler`
- `run-daily` CLI is now idempotent: reads state file before running; exits clean if
  today already ran
- `_next_fire()` updated to show today's target time whenever today has not run yet

## Create the cron service (click by click)

1. Open the Echo project in Railway (the one running the `listen` worker).
2. Click **+ New** (top right) then **GitHub Repo** and pick `lasso-echo`
   (the same repo as the worker).
3. Railway creates a second service. Rename it: click the service card, then the
   name at the top, and call it `echo-daily-cron`.
4. Open the service's **Settings** tab:
   - Under **Deploy**, set **Custom Start Command** to:

         /opt/venv/bin/python -m agent run-daily

   - Under **Deploy**, tick **Cron Schedule** and enter:

         30 14 * * *

     This fires at 14:30 UTC daily, 30 minutes after the listener's own window.
     Adjust the minute to match `AGENT_DAILY_HOUR_UTC` if you change that env var.
     Format: `<minute> <hour> * * *` (UTC). Do NOT set `restartPolicyType`; cron
     services exit after each run.
5. Open the **Variables** tab and add the SAME env vars as the echo worker:
   - `AGENT_ENABLED`, `AGENT_SLACK_BOT_TOKEN`, `AGENT_SLACK_APP_TOKEN`,
     `AGENT_SLACK_CHANNEL_ID`, `AGENT_DAILY_HOUR_UTC`
   - All `AGENT_LASSO_*` token and user-id pairs
   - `AGENT_DB_PATH` pointing at the same volume as the worker (see below)
6. Open **Settings > Volumes** and **attach the SAME `/data` volume** that the
   worker uses. The state file (`/data/scheduler_state.json`) and the draft store
   live there; the cron must see the same files so it can read `last_run_date` and
   post to the same pending store.
7. Deploy. The cron will first run at the next scheduled minute.

## Verify the cron fires

After the first scheduled run check the Railway logs for the `echo-daily-cron`
service. You should see:

    [run-daily] 2026-07-16: draw already ran today. No-op.   <- if listener fired first
    -- OR --
    [runner] drafted 2 card(s) for 2 account(s)              <- if cron fired first

Either outcome is correct. The draft store and Slack cards are the authoritative
record; checking them is the full verification.

## Disable the listener scheduler (optional)

Once the cron service has proven reliable you can hand the draw fully to it:

1. On the ECHO WORKER service: add `AGENT_SCHEDULER_ENABLED=false`.
2. The listener loop will still run Slack button handling, the intake ingest pass,
   the podcast/episode inbox polls, the heartbeat watchdog, and all other timed
   lanes — just not the daily draw.

## Check current scheduler state

    python -m agent scheduler-status

Prints loop liveness, last draw date, next expected draw, and whether the
in-listener scheduler is enabled.
