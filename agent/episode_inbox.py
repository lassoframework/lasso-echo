"""
Episode inbox watcher (Parts 1-5).

Dormant behind AGENT_EPISODE_INBOX_ENABLED (default OFF). When armed, the
listener polls the watched R2 prefix every AGENT_EPISODE_INBOX_POLL_MINUTES
for new episode files (mp4/mov/mp3/wav). Each file is claimed exactly once
(marker in the persistent db kv, survives restarts), Phase 1 clip selection
runs, and the ranked plan is posted to Slack #echoclaude for human approval.
NOTHING renders and NOTHING publishes automatically.

Part 5 adds a Monday 9am nudge: if the newest episode in the RSS feed arrived
within AGENT_EPISODE_NUDGE_WINDOW_DAYS and no inbox file has been matched to
it yet, one plain nudge is posted to Slack. Idempotent: never nudges twice for
the same episode on the same day.

Master flag: AGENT_EPISODE_INBOX_ENABLED (default false)
"""

import json
import os
import time

from . import config, db, ops_alerts

# ---- constants ------------------------------------------------------------------

_ACCEPTED_EXTS = frozenset({".mp4", ".mov", ".mp3", ".wav"})

_KV_CLAIM_PFX = "inbox_claimed:"      # + R2 key
_KV_PREV_SIZES = "inbox_prev_sizes"   # JSON {key: size}
_KV_LAST_RUN = "inbox_last_run"       # ISO timestamp
_KV_EP_MATCHED_PFX = "inbox_ep_matched:"   # + episode_number
_KV_NUDGE_PFX = "inbox_nudge_sent:"   # + episode_guid + ":" + date

# Phrases banned from plan output because they imply newness
_EVERGREEN_BANNED = (
    "new episode", "just dropped", "out now", "just released",
    "episode is live", "available now", "freshly dropped",
    "brand new episode", "just came out", "listen now",
)


# ---- Part 1: inbox convention + state ------------------------------------------

def _accept_ext(key: str) -> bool:
    return os.path.splitext(key)[1].lower() in _ACCEPTED_EXTS


def _claim_key_name(key: str) -> str:
    return f"{_KV_CLAIM_PFX}{key}"


def _is_claimed(key: str) -> bool:
    return bool(db.kv_get(_claim_key_name(key), ""))


def _claim(key: str) -> bool:
    """Try to claim key. Returns True if this call wins the claim, else False."""
    name = _claim_key_name(key)
    if db.kv_get(name, ""):
        return False
    db.kv_set(name, json.dumps({"status": "claimed", "claimed_at": time.time()}))
    return True


def _mark_processed(key: str) -> None:
    name = _claim_key_name(key)
    try:
        state = json.loads(db.kv_get(name, "{}"))
    except Exception:
        state = {}
    state["status"] = "processed"
    state["processed_at"] = time.time()
    db.kv_set(name, json.dumps(state))


def _mark_failed(key: str, reason: str) -> None:
    name = _claim_key_name(key)
    try:
        state = json.loads(db.kv_get(name, "{}"))
    except Exception:
        state = {}
    state["status"] = "failed"
    state["failed_at"] = time.time()
    state["reason"] = str(reason)[:500]
    db.kv_set(name, json.dumps(state))


def _get_prev_sizes() -> dict:
    try:
        return json.loads(db.kv_get(_KV_PREV_SIZES, "{}"))
    except Exception:
        return {}


def _set_prev_sizes(sizes: dict) -> None:
    db.kv_set(_KV_PREV_SIZES, json.dumps(sizes))


# ---- Part 4: RSS episode matching + evergreen guard ----------------------------

def _evergreen_check(text: str) -> list:
    """Return list of banned phrases found in text (empty = clean)."""
    low = text.lower()
    return [p for p in _EVERGREEN_BANNED if p in low]


def _latest_episode_from_db() -> dict:
    """Return the newest episode row from podcast_episodes, or {}."""
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT episode, title, published, guid "
                "FROM podcast_episodes ORDER BY detected_at DESC LIMIT 1"
            ).fetchone()
        if row:
            return {
                "episode": row["episode"],
                "title": row["title"] or "",
                "published": row["published"] or "",
                "guid": row["guid"] or "",
            }
    except Exception as e:
        print(f"[inbox] episode table unreadable: {type(e).__name__}: {e}")
        ops_alerts.alert(f"episode inbox: podcast_episodes table unreadable "
                         f"({type(e).__name__}); plans will show Episode ? "
                         "until this is fixed")
    return {}


def _mark_ep_matched(episode_number) -> None:
    """Record that an inbox file was matched to this episode number."""
    if episode_number is not None:
        db.kv_set(f"{_KV_EP_MATCHED_PFX}{episode_number}", "1")


def _is_ep_matched(episode_number) -> bool:
    if episode_number is None:
        return False
    return bool(db.kv_get(f"{_KV_EP_MATCHED_PFX}{episode_number}", ""))


# ---- Part 2: watcher loop -------------------------------------------------------

def _default_client():
    """Build the R2 client (requires AGENT_HOSTING_ENABLED + credentials). Returns None
    when the inbox watcher runs with a test-injected client."""
    try:
        from . import media_host
        return media_host._default_client()
    except Exception:
        return None


def _post_plan_to_slack(selection, episode_meta: dict, r2_key: str,
                        poster=None) -> None:
    """Format the Phase 1 selection plan with episode metadata and post to Slack."""
    from . import clipper as _clipper
    plan_text = _clipper.print_plan(selection)

    # Build header with episode info (evergreen: no recency phrases)
    ep_num = episode_meta.get("episode") or "?"
    ep_title = episode_meta.get("title") or "(unknown)"
    ep_date = episode_meta.get("published") or "?"
    header = (
        f"*INBOX CLIP PLAN*  |  file: `{os.path.basename(r2_key)}`\n"
        f"Episode {ep_num}: {ep_title}  |  published: {ep_date}\n"
        f"{'─' * 56}\n"
    )

    # Evergreen guard on the header (plan_text is already screened by clipper)
    violations = _evergreen_check(header)
    if violations:
        # Replace episode title with a safe label rather than blocking the plan
        header = header.replace(ep_title, "(episode title withheld: recency phrase)")
        ops_alerts.alert(
            f"[episode_inbox] evergreen violation in plan header: {violations}"
        )

    full_message = header + plan_text
    if poster is None:
        from .slack_surface import SlackPoster
        import os as _os
        poster = SlackPoster(
            token=_os.environ.get(config.SLACK_BOT_TOKEN_ENV, "")
        )
    try:
        poster.post_notice(full_message)
    except Exception as e:
        ops_alerts.alert(
            f"[episode_inbox] could not post plan to Slack: {type(e).__name__}: {e}"
        )


def poll(client=None, transcriber=None, llm=None, poster=None) -> dict:
    """
    One inbox poll pass.

    - Lists the watched prefix.
    - Guards against in-progress uploads (size must be stable across two polls).
    - Claims stable, unclaimed files and invokes Phase 1 clip selection.
    - Posts the ranked plan to Slack.
    - Marks each file processed or failed.
    - Updates db.kv last-run timestamp.

    Returns a summary dict for tests/ops.
    """
    if not config.episode_inbox_enabled():
        return {"status": "disabled"}

    db.kv_set(_KV_LAST_RUN, _now_iso())

    prefix = config.episode_inbox_prefix()
    r2_client = client or _default_client()

    if r2_client is None:
        ops_alerts.alert(
            "[episode_inbox] poll called but no R2 client available "
            "(check AGENT_HOSTING_ENABLED + credentials)"
        )
        return {"status": "no_client"}

    # List objects under prefix
    try:
        objects = r2_client.list_prefix(prefix)
    except Exception as e:
        ops_alerts.alert(
            f"[episode_inbox] list_prefix failed: {type(e).__name__}: {e}"
        )
        return {"status": "list_failed", "error": str(e)}

    # Build current sizes map
    prev_sizes = _get_prev_sizes()
    current_sizes = {}
    stable_keys = []
    for obj in objects:
        key = obj["key"]
        size = obj["size"]
        if not _accept_ext(key):
            continue
        current_sizes[key] = size
        if _is_claimed(key):
            continue
        # Size-stability guard: must have same size in two consecutive polls
        if prev_sizes.get(key) == size:
            stable_keys.append(key)

    _set_prev_sizes(current_sizes)

    processed = 0
    failed = 0

    for key in stable_keys:
        if not _claim(key):
            continue  # another pass already claimed it (shouldn't happen; single-thread)

        episode_meta = _latest_episode_from_db()
        try:
            from . import clipper as _clipper
            result = _clipper.clip_episode(
                key,
                transcriber=transcriber,
                llm=llm,
                account_key=config.episode_inbox_tenant(),
            )
            selection = result.get("selection", [])
            _post_plan_to_slack(selection, episode_meta, key, poster=poster)
            ep_num = episode_meta.get("episode")
            if ep_num is not None:
                _mark_ep_matched(ep_num)
            _mark_processed(key)
            processed += 1
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            _mark_failed(key, reason)
            failed += 1
            ops_alerts.alert(f"[episode_inbox] processing failed for {key}: {reason}")
            # Never crash the loop: continue to next file

    return {
        "status": "ok",
        "prefix": prefix,
        "objects_found": len(current_sizes),
        "stable_found": len(stable_keys),
        "processed": processed,
        "failed": failed,
    }


# ---- Part 3: ops surface --------------------------------------------------------

def _inbox_status_counts() -> dict:
    """Count files seen/claimed/processed/failed from kv markers."""
    seen = claimed = processed = failed = 0
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT value FROM kv WHERE key LIKE ?",
                (f"{_KV_CLAIM_PFX}%",)
            ).fetchall()
        for row in rows:
            seen += 1
            try:
                state = json.loads(row["value"])
                s = state.get("status", "claimed")
            except Exception:
                s = "claimed"
            if s == "claimed":
                claimed += 1
            elif s == "processed":
                processed += 1
            elif s == "failed":
                failed += 1
    except Exception:
        pass
    return {
        "seen": seen,
        "claimed": claimed,
        "processed": processed,
        "failed": failed,
    }


def inbox_status() -> dict:
    """Return the current inbox watcher state for the CLI."""
    counts = _inbox_status_counts()
    return {
        "enabled": config.episode_inbox_enabled(),
        "prefix": config.episode_inbox_prefix(),
        "poll_interval_minutes": config.episode_inbox_poll_minutes(),
        "last_run": db.kv_get(_KV_LAST_RUN, "never"),
        **counts,
    }


def inbox_status_cli() -> None:
    s = inbox_status()
    print("episode inbox watcher")
    print(f"  enabled        : {s['enabled']}  (env AGENT_EPISODE_INBOX_ENABLED)")
    print(f"  prefix         : {s['prefix']}")
    print(f"  poll interval  : {s['poll_interval_minutes']} min")
    print(f"  last run       : {s['last_run']}")
    print(f"  files seen     : {s['seen']}")
    print(f"  claimed        : {s['claimed']}")
    print(f"  processed      : {s['processed']}")
    print(f"  failed         : {s['failed']}")


# ---- Part 5: Monday nudge -------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _nudge_sent_key(episode_guid: str, date_str: str) -> str:
    return f"{_KV_NUDGE_PFX}{episode_guid}:{date_str}"


def _nudge_already_sent(episode_guid: str, date_str: str) -> bool:
    return bool(db.kv_get(_nudge_sent_key(episode_guid, date_str), ""))


def _mark_nudge_sent(episode_guid: str, date_str: str) -> None:
    db.kv_set(_nudge_sent_key(episode_guid, date_str), "1")


def _parse_episode_pub_date(published_str: str):
    """Parse RSS pubDate to a date object. Returns None on failure."""
    if not published_str:
        return None
    from datetime import datetime
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(published_str.strip(), fmt).date()
        except ValueError:
            continue
    # Fallback: take first 10 chars hoping for YYYY-MM-DD
    try:
        from datetime import date
        return date.fromisoformat(published_str.strip()[:10])
    except Exception:
        return None


def check_monday_nudge(now=None, poster=None) -> dict:
    """
    Monday 9am nudge check (Part 5).

    Fires when:
      - AGENT_EPISODE_INBOX_ENABLED is ON
      - today is Monday
      - current time in America/New_York is at or past AGENT_EPISODE_NUDGE_TIME
      - the newest RSS episode published within AGENT_EPISODE_NUDGE_WINDOW_DAYS
        has NOT yet been matched to an inbox file
      - no nudge has been sent for this episode today

    Safe to call every minute from the listener loop; it is idempotent and
    exits in <1ms when none of the conditions are met.
    """
    if not config.episode_inbox_enabled():
        return {"status": "disabled"}

    from datetime import datetime, timezone, timedelta
    if now is None:
        now = datetime.now(timezone.utc)

    # Localize to America/New_York
    try:
        from zoneinfo import ZoneInfo
        local_now = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: rough ET offset (EST = UTC-5, no DST correction)
        local_now = now - timedelta(hours=5)
        local_now = local_now.replace(tzinfo=timezone.utc)

    today = local_now.date()

    # Only Monday (weekday 0)
    if local_now.weekday() != 0:
        return {"status": "not_monday"}

    # Only at or after nudge hour
    nudge_hhmm = config.episode_nudge_time()
    try:
        nh, nm = (int(x) for x in nudge_hhmm.split(":")[:2])
    except Exception:
        nh, nm = 9, 0
    if (local_now.hour, local_now.minute) < (nh, nm):
        return {"status": "not_yet"}

    ep = _latest_episode_from_db()
    if not ep:
        return {"status": "no_episode"}

    ep_guid = ep.get("guid", "")
    ep_num = ep.get("episode")
    ep_title = ep.get("title") or "(unknown)"
    ep_published = ep.get("published", "")
    today_str = today.isoformat()

    # Idempotent: never nudge twice for same episode on same day
    if _nudge_already_sent(ep_guid, today_str):
        return {"status": "already_sent"}

    # Check episode is within the recency window
    pub_date = _parse_episode_pub_date(ep_published)
    if pub_date is None:
        return {"status": "unparseable_date"}

    window = config.episode_nudge_window_days()
    age_days = (today - pub_date).days
    if age_days < 0 or age_days > window:
        return {"status": "outside_window", "age_days": age_days}

    # If an inbox file was already matched, stay silent
    if _is_ep_matched(ep_num):
        return {"status": "already_matched"}

    # Post nudge
    prefix = config.episode_inbox_prefix()
    ep_label = f"Episode {ep_num}" if ep_num else "the newest episode"
    nudge_text = (
        f":bell: *Podcast inbox nudge*\n"
        f"{ep_label}: {ep_title}\n"
        f"Published: {ep_published}\n"
        f"No clip file found yet. Export from Riverside and drop the file in:\n"
        f"`{prefix}`"
    )

    if poster is None:
        from .slack_surface import SlackPoster
        poster = SlackPoster(
            token=os.environ.get(config.SLACK_BOT_TOKEN_ENV, "")
        )
    try:
        poster.post_notice(nudge_text)
        _mark_nudge_sent(ep_guid, today_str)
        return {"status": "nudge_sent", "episode": ep_num, "title": ep_title}
    except Exception as e:
        ops_alerts.alert(
            f"[episode_inbox] Monday nudge failed: {type(e).__name__}: {e}"
        )
        return {"status": "nudge_failed", "error": str(e)}
