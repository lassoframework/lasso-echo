"""
Slack control surface: the live listener (Socket Mode).

This is the always-on process that turns the Approve / Edit / Skip buttons on a
card into real actions through the approval gate. Socket Mode means NO public URL
and NO request-URL config: it holds an outbound websocket to Slack, which is the
simplest, safest setup for a single internal workspace.

Needs two tokens (set by hand in Railway, never in chat or code):
  AGENT_SLACK_BOT_TOKEN   xoxb-...   (bot token)
  AGENT_SLACK_APP_TOKEN   xapp-...   (app-level token, scope connections:write)

The approver gate still lives in approvals.handle_action: only the configured
approver's taps do anything. Everyone else is denied.

Run:  python -m agent listen
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

from . import config, ops_alerts, schedule
from .approvals import handle_action
from .accounts import get_account
from .drafter import Draft, DraftStatus, draft_post
from .store import PendingStore
from .runner import run_daily


def _redraft_with_note(old: Draft, note: str) -> Draft:
    """Blake's edit note becomes the new caption, re-held for approval."""
    acct = get_account(old.account_key)
    new = Draft(
        draft_id=old.draft_id + "e",
        account_key=old.account_key,
        platform=old.platform,
        caption=note.strip(),
        hashtags=old.hashtags,
        creative_path=old.creative_path,
        creative_public_url=old.creative_public_url,
        scheduled_for=old.scheduled_for,
        status=DraftStatus.PENDING,
        source_fragments=[note.strip()],
    )
    return new


# The scheduler's fire date persists to /data (the volume on the echo service) so a
# redeploy inside the fire window cannot double-fire even with idempotency disarmed.
_SCHEDULER_STATE_FILE = "scheduler_state.json"


def _scheduler_state_path():
    return os.path.join(os.environ.get("AGENT_SCHEDULER_STATE_DIR", "/data"),
                        _SCHEDULER_STATE_FILE)


def _read_state():
    """The persisted scheduler-state dict, or {} when /data is unavailable."""
    try:
        with open(_scheduler_state_path(), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _write_state(d):
    """Best-effort persist of the whole scheduler-state dict; a missing /data
    never breaks the scheduler."""
    try:
        with open(_scheduler_state_path(), "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    except Exception as e:
        print(f"[scheduler] could not persist state: {type(e).__name__}: {e}")


def _read_last_run_date():
    """The persisted last fire date, or None when /data is unavailable or empty
    (in-memory tracking then carries the day, exactly the old behavior)."""
    return _read_state().get("last_run_date")


def _write_last_run_date(day):
    """Best-effort persist; a missing /data never breaks the scheduler. Merges so
    sibling markers (e.g. the weekly podcast-auto date) are not clobbered."""
    d = _read_state()
    d["last_run_date"] = day
    _write_state(d)


def _read_podcast_auto_date():
    """The persisted date the weekly podcast auto-ingest last fired, or None."""
    return _read_state().get("podcast_auto_last_date")


def _write_podcast_auto_date(day):
    d = _read_state()
    d["podcast_auto_last_date"] = day
    _write_state(d)


def _podcast_auto_due(now, last_date, target_hour):
    """True when the weekly Drive->edit->schedule auto-ingest should fire: Monday,
    at/after the target hour (UTC), and not already fired today. Pure + testable;
    the AGENT_PODCAST_AUTO_ENABLED gate is checked by the caller."""
    return (now.weekday() == 0                       # Monday
            and now.hour >= target_hour
            and last_date != now.date().isoformat())


def _fire_daily(store, today, run=run_daily):
    """
    One scheduled fire, LOUD on every no-card outcome. Any result other than a
    drafted run with at least one card (on a posting day) raises one ops alert, so
    a silent no-card morning is impossible while AGENT_OPS_ALERTS_ENABLED is true.
    A skip day (schedule.should_post_on false) drafting zero cards is EXPECTED and
    does not alert.
    """
    try:
        out = run(store=store)
    except Exception as e:
        print(f"[scheduler] run_daily error: {e}")  # log either way (old behavior)
        ops_alerts.alert("scheduled draft run produced no cards - "
                         f"{type(e).__name__}: {e}")
        return None
    status = (out or {}).get("status")
    drafts = (out or {}).get("drafts") or []
    if status != "drafted":
        ops_alerts.alert(f"scheduled draft run produced no cards - status '{status}' "
                         "(check AGENT_ENABLED and the voice doc)")
    elif not drafts and schedule.should_post_on(today):
        ops_alerts.alert("scheduled draft run produced no cards - drafted 0 drafts "
                         "on a posting day")
    return out


def _client_media_scan_due(now_mono, last_mono, interval_secs):
    """PURE throttle predicate for the frequent client-media lane: True when at least
    interval_secs have elapsed since the last scan (last_mono is 0.0 on first tick, so
    the very first eligible tick always runs). Kept pure + tiny so a test can assert
    the throttle without touching the loop or R2."""
    return (now_mono - last_mono) >= interval_secs


def run_client_media_lane(*, now_mono, last_mono, interval_secs, scan=None):
    """The listener's FREQUENT client-media lane: PROMPTLY sync each onboarded client
    gym's R2 uploads and auto-build its DRAFT calendar the moment it uploads, instead
    of waiting up to 24h for the once/day run_daily pass (BUG 2). Returns the new
    'last run' monotonic marker: unchanged when self-guarded off / throttled, else
    now_mono after a run.

    Self-guarded on AGENT_CLIENT_MEDIA_SYNC (scan_and_generate also re-checks the flag
    and no-ops when off, belt and suspenders). Throttled to interval_secs so a ~60s
    loop does not hammer R2; a scan with nothing new is a cheap no-op either way
    (scan_and_generate skips gyms whose media count == existing feed count). Fully
    isolated in try/except: a scan failure never kills the scheduler loop, exactly
    like every other lane. DRAFTS ONLY: nothing here publishes (scan_and_generate has
    no publish path)."""
    if not config.client_media_sync_enabled():
        return last_mono
    if not _client_media_scan_due(now_mono, last_mono, interval_secs):
        return last_mono
    scan = scan or _default_client_media_scan
    try:
        scan()
    except Exception as e:
        print(f"[client-media-sync] frequent lane failed: {type(e).__name__}: {e}")
        ops_alerts.alert(f"client media frequent lane failed: {type(e).__name__}: {e}."
                         " The draft run is unaffected.")
    return now_mono


def _default_client_media_scan():
    """Run the real client-media scan (lazy import so flag-off never imports it)."""
    from .client_media_sync import scan_and_generate
    scan_and_generate()


# Scheduler process heartbeat (no flag, honest observability): one kv row the
# loop refreshes each cycle so `status` can prove the listen process is alive
# and show when the next daily draw fires. Distinct from heartbeat.py (which
# records that each ACCOUNT'S daily run happened); this one says the SCHEDULER
# LOOP itself is breathing.
_HEARTBEAT_KEY = "scheduler_heartbeat"
LATE_DRAW_GRACE_MINUTES = 30


def _next_fire(now, target_hour, last_run_date):
    """The next daily-draw fire time: today at target_hour UTC when today's draw
    has not happened yet (regardless of current hour — the >= fire condition
    ensures a late restart still fires today), else tomorrow."""
    from datetime import timedelta
    today_fire = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if last_run_date != now.date().isoformat():
        return today_fire
    return today_fire + timedelta(days=1)


def write_scheduler_heartbeat(now, target_hour, last_run_date):
    """One kv write per loop cycle: timestamp + next fire time. Best effort;
    a db hiccup never touches the loop."""
    from . import db
    try:
        db.kv_set(_HEARTBEAT_KEY, json.dumps({
            "ts": now.isoformat(),
            "next_fire": _next_fire(now, target_hour, last_run_date).isoformat(),
        }))
    except Exception as e:
        print(f"[scheduler] heartbeat write failed: {type(e).__name__}: {e}")


def read_scheduler_heartbeat():
    """{'ts', 'next_fire'} from the kv heartbeat, or None when the listener has
    never run (or the row is unreadable)."""
    from . import db
    try:
        raw = db.kv_get(_HEARTBEAT_KEY, "")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def check_late_draw(now, last_run_date, target_hour):
    """One ops alert (deduped per day) when today's scheduled draw is more than
    LATE_DRAW_GRACE_MINUTES past the target hour and still has not fired. Returns
    True when the alert fired this call (for tests)."""
    from . import db
    today = now.date().isoformat()
    if last_run_date == today:
        return False                       # today's draw happened: nothing late
    deadline = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    if now < deadline + timedelta(minutes=LATE_DRAW_GRACE_MINUTES):
        return False                       # inside the window (or before it)
    dedup_key = f"late_draw_alerted_{today}"
    try:
        if db.kv_get(dedup_key):
            return False
        db.kv_set(dedup_key, "1")
    except Exception:
        pass                               # a db hiccup must not silence the alert
    minutes_late = int((now - deadline).total_seconds() // 60)
    ops_alerts.alert(
        f"scheduled daily draw is {minutes_late} minutes late (target "
        f"{target_hour:02d}:00 UTC, no run recorded for {today}). The listener "
        "loop is alive but the draw did not fire; check the deploy, or run "
        "`python -m agent run-daily` by hand / via the Railway cron fallback "
        "(see PROGRESS.md).")
    return True


def _print_scheduled_lanes():
    """One startup line per scheduled lane, armed or dormant. A lane whose
    flag is off used to be INVISIBLE — it simply never fired and never said
    so (the plan-month silence class). Now the log shows what is and is not
    armed the moment the scheduler starts."""
    lanes = [
        ("intake ingest", config.intake_enabled(), "AGENT_INTAKE_ENABLED"),
        ("client media sync (frequent)", config.client_media_sync_enabled(),
         "AGENT_CLIENT_MEDIA_SYNC"),
        ("opus poll", config.opus_enabled() and config.opus_poll_enabled(),
         "AGENT_OPUS_ENABLED + AGENT_OPUS_POLL_ENABLED"),
        ("podcast feed", config.podcast_enabled(), "AGENT_PODCAST_ENABLED"),
        ("episode inbox", config.episode_inbox_enabled(),
         "AGENT_EPISODE_INBOX_ENABLED"),
        ("podcast auto-ingest (Mon)", config.podcast_auto_enabled(),
         "AGENT_PODCAST_AUTO_ENABLED"),
        ("reporting snapshot", config.reporting_enabled(),
         "AGENT_REPORTING_ENABLED"),
        ("evening digest", config.digest_enabled(), "AGENT_DIGEST_ENABLED"),
        ("weekly report", config.weekly_report_enabled(),
         "AGENT_WEEKLY_REPORT_ENABLED"),
        ("nightly brain", config.brain_proposals_enabled(),
         "AGENT_BRAIN_PROPOSALS_ENABLED"),
        ("nightly backup", config.backup_enabled(), "AGENT_BACKUP_ENABLED"),
    ]
    for name, armed, env in lanes:
        state = "ARMED" if armed else f"dormant ({env} off)"
        print(f"[scheduler] {name}: {state}")


def _daily_scheduler(store):
    """
    Minimal in-process daily trigger. Fires run_daily once per day at the target
    UTC hour. Simple by design. For stricter reliability, run `run-daily` as a
    Railway cron service instead and disable this with AGENT_SCHEDULER_ENABLED=false.
    """
    _print_scheduled_lanes()
    target_hour = int(os.environ.get("AGENT_DAILY_HOUR_UTC", "14"))  # ~10am ET
    ingest_every = max(1, int(os.environ.get("AGENT_INTAKE_POLL_MINUTES", "5"))) * 60
    intake_sync_every = max(1, int(os.environ.get("AGENT_SOCIAL_INTAKE_SYNC_MINUTES", "15"))) * 60
    opus_every = max(1, int(os.environ.get("AGENT_OPUS_POLL_MINUTES", "60"))) * 60
    podcast_every = max(1, int(os.environ.get("AGENT_PODCAST_POLL_MINUTES", "60"))) * 60
    inbox_every = config.episode_inbox_poll_minutes() * 60
    cms_every = config.client_media_sync_minutes() * 60
    last_run_date = _read_last_run_date()  # survives a redeploy inside the window
    last_pcast_auto = _read_podcast_auto_date()  # weekly Monday auto-ingest guard
    last_ingest = 0.0
    last_intake_sync = 0.0
    last_opus = 0.0
    last_podcast = 0.0
    last_inbox = 0.0
    last_cms = 0.0
    while True:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        # Process heartbeat + late-draw watchdog (no flag): the heartbeat proves
        # this loop is breathing (read by `status`); the watchdog fires one
        # deduped ops alert when today's draw is >30 min past the target hour.
        write_scheduler_heartbeat(now, target_hour, last_run_date)
        try:
            check_late_draw(now, last_run_date, target_hour)
        except Exception as e:
            print(f"[scheduler] late-draw check failed: {type(e).__name__}: {e}")
        if now.hour >= target_hour and last_run_date != today:
            _fire_daily(store, today)
            last_run_date = today
            _write_last_run_date(today)
            # Daily metrics snapshot AFTER the daily draft: READ-ONLY Graph pulls
            # (views, never impressions), dormant unless AGENT_REPORTING_ENABLED.
            # Failures alert inside snapshot_all; nothing here crashes the loop.
            if config.reporting_enabled():
                try:
                    from . import reporting_live
                    reporting_live.snapshot_all()
                except Exception as e:
                    print(f"[reporting] snapshot pass failed: {type(e).__name__}: {e}")
        # Calendar auto-publish SLOT-FIRE lane: dormant unless AGENT_CALENDAR_AUTOPUBLISH
        # (also self-guards on AGENT_PUBLISH_ENABLED inside publish_due). The once/day
        # run_daily draw is far too coarse for time-of-day spacing and would orphan
        # later-slot rows, so this lane fires each SPRINT_SLOT_TIME (in POSTING_TIMEZONE)
        # on the loop's ~1-min cadence, deduped per (slot, day) via kv; the last slot
        # sweeps every straggler (catch_all). run_date is the LOCAL posting day (slots
        # are local). Isolated in try/except; a failure never kills the loop.
        if config.calendar_autopublish_enabled():
            try:
                from . import calendar_autopublish
                from zoneinfo import ZoneInfo
                _local_today = now.astimezone(
                    ZoneInfo(config.POSTING_TIMEZONE)).date().isoformat()
                calendar_autopublish.run_slot_ticks(
                    _local_today, notifier=ops_alerts._default_poster())
                # CLIENT gyms: publish each connected gym's APPROVED, due rows to its
                # OWN IG/FB via Zernio, scheduled at the row's slot time. Self-gated by
                # AGENT_ZERNIO_PUBLISH (a no-op unless armed); one gym never blocks another.
                calendar_autopublish.publish_client_gyms(
                    _local_today, notifier=ops_alerts._default_poster())
                # Stale-'publishing' watchdog (alert-only, never reverts): a worker
                # that died between the claim and the publish leaves a row stuck;
                # this surfaces it to a human instead of silent forever-orphaning.
                calendar_autopublish.sweep_stuck_publishing()
            except Exception as e:
                print(f"[calendar-autopublish] slot-fire lane failed: "
                      f"{type(e).__name__}: {e}")
                ops_alerts.alert(f"calendar slot-fire lane failed: "
                                 f"{type(e).__name__}: {e}. The draft run is unaffected.")
        # New-client CATCH-UP report: one Slack message a day listing every gym signed
        # up in the last 60 days and its calendar coverage, until everyone is caught up.
        # INDEPENDENT of the autopublish flag (a report must never be silenced by a
        # publish switch); self-deduped per day, flag-gated inside (AGENT_CATCHUP_REPORT),
        # and an error never kills the loop.
        try:
            from . import catchup_report
            catchup_report.run_daily()
        except Exception as e:
            print(f"[catchup] daily report failed: {type(e).__name__}: {e}")
        # New-client WELCOME digest: one Slack message a day showing every new client's
        # welcome post (template caption + hosted image), today's + queued. Flag-gated
        # inside, self-deduped per day, read-only; an error never kills the loop.
        try:
            from . import welcome_digest
            welcome_digest.run_daily()
        except Exception as e:
            print(f"[welcome-digest] daily post failed: {type(e).__name__}: {e}")
        # Intake ingest: dormant unless AGENT_INTAKE_ENABLED. Runs INSIDE this
        # listener (the one process with /data + R2); an error never kills the loop.
        if config.intake_enabled() and time.monotonic() - last_ingest >= ingest_every:
            last_ingest = time.monotonic()
            try:
                from . import intake_ingest
                intake_ingest.process_all()
            except Exception as e:
                print(f"[intake] ingest pass failed: {type(e).__name__}: {e}")
        # CLIENT MEDIA SYNC frequent lane: dormant unless AGENT_CLIENT_MEDIA_SYNC.
        # Picks up a client gym's fresh R2 upload PROMPTLY (throttled to
        # AGENT_CLIENT_MEDIA_SYNC_MINUTES, default 5) and auto-builds its DRAFT
        # calendar, instead of waiting up to 24h for the once/day run_daily pass
        # (which still runs, belt and suspenders). Cheap no-op when nothing changed;
        # self-guarded, throttled, and fully isolated inside run_client_media_lane.
        last_cms = run_client_media_lane(
            now_mono=time.monotonic(), last_mono=last_cms, interval_secs=cms_every)
        # Social-intake forward: dormant unless AGENT_SOCIAL_INTAKE_SYNC. Maps every
        # un-routed echo_social_intake row into Echo (voice/proof + client_sources)
        # and marks it routed, so no gym is ever stranded the way ENG was. Nothing
        # publishes; an error never kills the loop. Distinct from the client-media
        # lane above: that syncs uploaded MEDIA; this maps submitted INTAKE FORMS.
        if (config.social_intake_sync_enabled()
                and time.monotonic() - last_intake_sync >= intake_sync_every):
            last_intake_sync = time.monotonic()
            try:
                from . import social_intake_reader
                synced = social_intake_reader.sync_unrouted()
                mapped = [r for r in synced if r.get("ok")]
                if mapped:
                    print(f"[intake-sync] mapped {len(mapped)} gym(s) into Echo: "
                          + ", ".join(r["base"] for r in mapped))
            except Exception as e:
                print(f"[intake-sync] pass failed: {type(e).__name__}: {e}")
        # Opus Clip poll: FULLY INERT unless BOTH AGENT_OPUS_ENABLED and
        # AGENT_OPUS_POLL_ENABLED are armed. Errors alert (inside pull), never crash.
        if (config.opus_enabled() and config.opus_poll_enabled()
                and time.monotonic() - last_opus >= opus_every):
            last_opus = time.monotonic()
            try:
                from . import opus_ingest
                opus_ingest.pull()
            except Exception as e:
                print(f"[opus] poll pass failed: {type(e).__name__}: {e}")
        # Podcast feed poll: FULLY INERT unless AGENT_PODCAST_ENABLED. A new
        # episode is stored exactly once (idempotent by guid); a malformed feed
        # or missing feed url fails LOUD here (log + one ops alert), never
        # silent, and never crashes the loop. Detection only; drafting stays in
        # the daily run's priority chain.
        if config.podcast_enabled() and time.monotonic() - last_podcast >= podcast_every:
            last_podcast = time.monotonic()
            try:
                from . import podcast_feed
                podcast_feed.poll()
            except Exception as e:
                print(f"[podcast] poll pass failed: {type(e).__name__}: {e}")
                ops_alerts.alert(f"podcast feed poll failed: {type(e).__name__}: {e}")
        # Episode inbox watcher: FULLY INERT unless AGENT_EPISODE_INBOX_ENABLED.
        # On each pass: list the watched R2 prefix, guard against in-progress
        # uploads (size stability), claim + Phase 1 clip selection, post ranked
        # plan to Slack #echoclaude. Also runs the Monday 9am nudge check when
        # the flag is armed. Errors alert (inside poll/check_monday_nudge) and
        # never crash the loop.
        if config.episode_inbox_enabled():
            if time.monotonic() - last_inbox >= inbox_every:
                last_inbox = time.monotonic()
                try:
                    from . import episode_inbox
                    episode_inbox.poll()
                except Exception as e:
                    print(f"[inbox] poll pass failed: {type(e).__name__}: {e}")
                    ops_alerts.alert(
                        f"episode inbox poll failed: {type(e).__name__}: {e}"
                    )
            try:
                from . import episode_inbox
                episode_inbox.check_monday_nudge(now=now)
            except Exception as e:
                print(f"[inbox] nudge check failed: {type(e).__name__}: {e}")
        # Weekly podcast auto-ingest: Monday at/after the target hour, pull the
        # newest episode from the Drive folder, edit it, and schedule the week as
        # HELD drafts. Runs INSIDE this listener (the one process with /data + R2
        # + the store the Approve buttons read), NOT a separate Railway service
        # (a Railway volume attaches to a single service, so a second service
        # cannot see /data/echo.db). Dormant unless AGENT_PODCAST_AUTO_ENABLED.
        # Errors alert (inside run), never crash the loop.
        if config.podcast_auto_enabled() and _podcast_auto_due(
                now, last_pcast_auto, target_hour):
            last_pcast_auto = today
            _write_podcast_auto_date(today)
            try:
                from . import podcast_auto
                podcast_auto.run(today=now.date())
            except Exception as e:
                print(f"[podcast-auto] weekly run failed: {type(e).__name__}: {e}")
                ops_alerts.alert(
                    f"podcast auto-ingest failed: {type(e).__name__}: {e}")
        # Card self-expiry sweep (no flag, queue hygiene): hourly, cheap.
        if now.minute == 0:
            try:
                from . import ops_alerts as _oa
                from .runner import expire_past_due
                expire_past_due(store, _oa._default_poster(), now=now)
            except Exception as e:
                print(f"[expiry] sweep failed: {type(e).__name__}: {e}")
        # Heartbeat morning check (no flag, honest observability): one alert
        # per enabled account per day when the daily run missed its window.
        try:
            from . import heartbeat
            heartbeat.check_heartbeats(now=now)
        except Exception as e:
            print(f"[heartbeat] check failed: {type(e).__name__}: {e}")
        # Evening digest: one line per day at AGENT_DIGEST_HOUR_UTC, dormant
        # unless AGENT_DIGEST_ENABLED. Never crashes the loop.
        if config.digest_enabled():
            try:
                from . import digest
                poster = ops_alerts._default_poster()
                digest.maybe_send(poster, now=now, library_path=config.LIBRARY_PATH)
            except Exception as e:
                print(f"[digest] pass failed: {type(e).__name__}: {e}")
        # Sunday operator report: one weekly card at 6 PM ET, dormant unless
        # AGENT_WEEKLY_REPORT_ENABLED. Never crashes the loop.
        if config.weekly_report_enabled():
            try:
                from . import ops_alerts as _oa2, weekly_report
                weekly_report.maybe_send(_oa2._default_poster(), now=now)
            except Exception as e:
                print(f"[weekly] pass failed: {type(e).__name__}: {e}")
        # Nightly brain: one read-only proposal note, dormant unless
        # AGENT_BRAIN_PROPOSALS_ENABLED. Never crashes the loop.
        if config.brain_proposals_enabled():
            try:
                from . import brain
                brain.maybe_send(ops_alerts._default_poster(), now=now)
            except Exception as e:
                print(f"[brain] pass failed: {type(e).__name__}: {e}")
        # Nightly store backup: dormant unless AGENT_BACKUP_ENABLED. One ops
        # alert on failure only; never crashes the loop.
        if config.backup_enabled():
            try:
                from . import backup
                backup.maybe_backup(now=now)
            except Exception as e:
                print(f"[backup] pass failed: {type(e).__name__}: {e}")
        # Handoff live-page refresh at 12pm + 4pm ET (16:00 + 20:00 UTC by default).
        # Writes /data/handoff_live.html so the tracker route always has a fresh view.
        # No flag required; fires as long as the loop is running. Idempotent per hour.
        try:
            _hh = {int(h.strip()) for h in
                   os.environ.get("AGENT_HANDOFF_REFRESH_HOURS_UTC", "16,20").split(",")
                   if h.strip().isdigit()}
            if now.hour in _hh and now.minute < 10:
                _hkey = f"handoff_refresh_{today}_{now.hour}"
                from . import db as _db_hr
                if (_db_hr.kv_get(_hkey, "") or "") != "done":
                    from . import handoff_refresh as _hr
                    _hr.generate()
                    _db_hr.kv_set(_hkey, "done")
        except Exception as e:
            print(f"[handoff] refresh failed: {type(e).__name__}: {e}")
        time.sleep(60)


def run_listener():
    # Startup config hygiene: placeholder AGENT_OPUS_PROJECT_IDS values (P1
    # pattern / under 6 chars) get ONE warning naming each bad value and are
    # never sent to the API. Ingest revalidates on every pass; this line only
    # makes the misconfiguration visible the moment the service boots.
    from . import opus_ingest
    opus_ingest.validated_project_ids()
    # Generation model sanity check: verify AGENT_NANO_MODEL and
    # AGENT_NANO_MODEL_FLASH resolve in the live Gemini API. Fires one ops_alert
    # naming the bad model string(s) and listing available image models if either
    # 404s. Same class of self-announcing guard as the OCR model check.
    from . import creative_studio as _cs
    _cs.validate_generation_models()
    # Facebook connect page: a small HTTP surface INSIDE this process (it needs
    # the /data store for the page token). Dormant unless AGENT_CONNECT_ENABLED;
    # while off, no thread starts and the routes would 404 anyway.
    if config.connect_enabled():
        import threading as _threading
        from . import connect_web
        _threading.Thread(target=connect_web.serve, daemon=True).start()
    import os
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("slack_bolt is not installed. Add it (pip install slack_bolt) and redeploy.")
        return

    bot_token = os.environ.get(config.SLACK_BOT_TOKEN_ENV)
    app_token = os.environ.get("AGENT_SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        print("Missing Slack tokens. Set AGENT_SLACK_BOT_TOKEN (xoxb-) and "
              "AGENT_SLACK_APP_TOKEN (xapp-) in the environment by hand.")
        return

    app = App(token=bot_token)
    store = PendingStore()

    def _act(ack, body, action, client, kind):
        ack()
        draft_id = action.get("value")
        actor = body.get("user", {}).get("id", "")
        channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
        ts = body.get("message", {}).get("ts") or body.get("container", {}).get("message_ts")
        draft = store.get(draft_id)
        if not draft:
            client.chat_postMessage(channel=channel, text=f"Draft {draft_id} not found (it may have expired).")
            return
        if getattr(draft, "draft_type", "") == "claim_promotion":
            # standing claim promotion (podcast Part F): same approver gate,
            # its own write path; the post approval flow stays untouched
            from . import podcast_promote
            res = podcast_promote.handle_promotion_action(kind, draft, actor)
        else:
            res = handle_action(kind, draft, actor_slack_id=actor,
                                account=get_account(draft.account_key))
        if not res.ok:
            client.chat_postMessage(channel=channel, text=f":no_entry: {res.detail}")
            return
        store.remove(draft_id)
        label = {"approve": "Approved", "skip": "Skipped"}[kind]
        try:
            client.chat_update(channel=channel, ts=ts,
                               text=f"{label} by <@{actor}> — {res.detail}",
                               blocks=[{"type": "section", "text": {"type": "mrkdwn",
                                        "text": f":white_check_mark: *{label}* by <@{actor}>\n{res.detail}"}}])
        except Exception:
            client.chat_postMessage(channel=channel, text=f"{label}: {res.detail}")

    @app.action("approve")
    def on_approve(ack, body, action, client):
        _act(ack, body, action, client, "approve")

    @app.action("skip")
    def on_skip(ack, body, action, client):
        _act(ack, body, action, client, "skip")

    @app.action("edit")
    def on_edit(ack, body, action, client):
        ack()
        draft_id = action.get("value")
        trigger_id = body.get("trigger_id")
        client.views_open(trigger_id=trigger_id, view={
            "type": "modal", "callback_id": "edit_submit",
            "private_metadata": draft_id,
            "title": {"type": "plain_text", "text": "Edit caption"},
            "submit": {"type": "plain_text", "text": "Re-hold for approval"},
            "blocks": [{
                "type": "input", "block_id": "note",
                "label": {"type": "plain_text", "text": "New caption"},
                "element": {"type": "plain_text_input", "action_id": "v", "multiline": True},
            }],
        })

    @app.view("edit_submit")
    def on_edit_submit(ack, body, view, client):
        ack()
        # A malformed or replayed modal payload must not 500 the socket
        # handler: missing blocks read as empty and the submit no-ops below.
        draft_id = (view or {}).get("private_metadata", "")
        note = (((view or {}).get("state", {}).get("values", {})
                 .get("note", {}).get("v", {}) or {}).get("value") or "")
        actor = body.get("user", {}).get("id", "")
        if not draft_id or not note:
            print("[listener] edit_submit payload missing draft_id or note; ignored")
            return
        old = store.get(draft_id)
        if not old:
            return
        from .accounts import get_account as _get_account
        from .approvals import _is_approver as _gate
        if not _gate(actor, account=_get_account(old.account_key or "")):
            return
        new = _redraft_with_note(old, note)
        store.remove(draft_id)
        store.put(new)
        from .slack_surface import SlackPoster
        SlackPoster(token=os.environ.get(config.SLACK_BOT_TOKEN_ENV)).post_approval_card(new)

    @app.command("/echo-draft")
    def on_draft_now(ack, respond):
        ack()
        out = run_daily(store=store)
        respond(f"Drafting: {out['status']} ({len(out.get('drafts', []))} card(s)) -> #echoclaude")

    @app.message("")
    def on_chat_message(message, say):
        """Free-text chat: Blake can publish LASSO accounts directly (explicit verb),
        client accounts only draft. Inert unless AGENT_CHAT_PUBLISH_ENABLED. Stays
        SILENT on anything that is not an actionable publish/undo command so it never
        spams the channel."""
        if not config.chat_publish_enabled():
            return
        # ignore bot / edited / non-user events
        if message.get("bot_id") or message.get("subtype"):
            return
        actor = message.get("user", "")
        # only Blake is ever engaged; every other user's chatter is left untouched
        # (route() also gates on the approver, this just avoids replying to others)
        if actor != config.APPROVER_SLACK_ID:
            return
        text = message.get("text", "") or ""
        from . import chat_publish
        # cheap pre-filter: only engage on a publish/undo verb, so normal chatter is
        # untouched and non-Blake users are handled by the actor gate in route()
        if chat_publish.classify_intent(text) in (chat_publish.GENERATE,
                                                   chat_publish.NONE):
            return
        poster = None
        try:
            from .slack_surface import SlackPoster
            poster = SlackPoster(token=os.environ.get(config.SLACK_BOT_TOKEN_ENV))
        except Exception:
            poster = None
        out = chat_publish.handle_message(text, actor, store=store, poster=poster)
        if out.kind in ("not_a_command", "disabled"):
            return
        try:
            say(out.message)
        except Exception:
            pass

    if str(os.environ.get("AGENT_SCHEDULER_ENABLED", "true")).lower() in {"1", "true", "yes", "on"}:
        threading.Thread(target=_daily_scheduler, args=(store,), daemon=True).start()
        print("Daily scheduler started.")

    print("Echo listener online (Socket Mode). Draft-only:", not config.publish_enabled())
    SocketModeHandler(app, app_token).start()
