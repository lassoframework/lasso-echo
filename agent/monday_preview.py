"""
monday-preview (readiness Part C): one read only command, one GO / NO GO.

    python -m agent monday-preview

Runs every Monday preflight in one pass and prints ONE line per check plus a
final verdict:
  - podcast feed reachable + the next episode forecast (number, title, the
    template letter per the mod 4 rotation), via the same read only probe
  - runway days per account, via the SHARED explain implementation
  - token watchdog days remaining per account (a quiet read of debug_token;
    no alert fires from here)
  - scheduler heartbeat freshness per account
  - pending approvals count in the approval channel queue
  - flags snapshot: anything armed that should not be, anything off that
    Monday needs

ZERO SIDE EFFECTS: no store write, no kv stamp, no audit row, no alert, no
file. The store dumps byte identical after a run (tested). Network reads only
(the feed and debug_token), both injectable. Output is dash free.
"""

import io
import time
from contextlib import redirect_stdout

from . import config, db, runway
from .accounts import active_accounts

# what Monday needs armed, and what must not be silently armed
MONDAY_NEEDS = (
    ("master", "AGENT_ENABLED", lambda: config.master_enabled()),
    ("podcast", "AGENT_PODCAST_ENABLED", lambda: config.podcast_enabled()),
    ("creative studio", "AGENT_NANO_ENABLED",
     lambda: config.creative_studio_enabled()),
    ("hosting", "AGENT_HOSTING_ENABLED", lambda: config.hosting_enabled()),
)


def _feed_check(fetch=None):
    """(line, blocker). The podcast probe is already proven side effect free."""
    if not config.podcast_enabled():
        return ("podcast: OFF (AGENT_PODCAST_ENABLED); no release card Monday",
                "podcast pipeline is OFF")
    from . import podcast_feed
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = podcast_feed.status_cli(fetch=fetch)
    if not out.get("reachable"):
        return ("podcast feed: UNREACHABLE", "podcast feed unreachable")
    if out.get("parsed") is False:
        return ("podcast feed: reachable but MALFORMED", "podcast feed malformed")
    return (f"podcast feed: reachable, {out.get('items', 0)} item(s); "
            f"{out.get('forecast', 'no forecast')}", None)


def _runway_check():
    lines, blockers = [], []
    for acct in active_accounts():
        eligible, _excluded = runway.classify_creatives(acct.key,
                                                        acct.library_path())
        days = runway.runway_days(acct.key, acct.library_path())
        lines.append(f"runway {acct.key}: {days} day(s), "
                     f"{len(eligible)} eligible creative(s)")
        if days <= 0:
            blockers.append(f"runway is ZERO for {acct.key}")
    return lines, blockers


def _token_days(account, http, now):
    """Days remaining for one account's token, QUIETLY: same read the watchdog
    makes, but no alert and no audit can fire from here."""
    token = account.get_token()
    if not token:
        return None, "no token set"
    try:
        r = http.get(f"{config.GRAPH_API_BASE}/debug_token",
                     params={"input_token": token, "access_token": token},
                     timeout=30)
        if getattr(r, "status_code", 200) >= 400:
            return None, f"debug_token HTTP {r.status_code}"
        data = (r.json() or {}).get("data") or {}
    except Exception as e:
        return None, f"unreachable ({type(e).__name__})"
    expires_at = data.get("expires_at") or 0
    if not expires_at:
        return None, "never expires"
    return int((expires_at - now) // 86400), None


def _token_check(http, now):
    lines, blockers = [], []
    if http is None:
        try:
            import requests as http  # noqa: F811
        except ImportError:
            return ["tokens: requests unavailable; skipped"], []
    for acct in active_accounts():
        days, note = _token_days(acct, http, now)
        if days is None:
            lines.append(f"token {acct.key}: {note}")
            if note == "no token set":
                blockers.append(f"no token for {acct.key}")
        else:
            lines.append(f"token {acct.key}: {days} day(s) remaining")
            if days <= 3:
                blockers.append(f"token for {acct.key} expires in {days} day(s)")
    return lines, blockers


def _heartbeat_check(now_dt):
    from datetime import timedelta
    lines = []
    for acct in active_accounts():
        found = ""
        for back in range(0, 4):
            day = (now_dt.date() - timedelta(days=back)).isoformat()
            ts = db.kv_get(f"heartbeat_{acct.key}_{day}", "")
            if ts:
                found = ("today" if back == 0 else f"{back} day(s) ago")
                break
        lines.append(f"heartbeat {acct.key}: "
                     + (f"last run {found}" if found
                        else "none in the last 4 days"))
    return lines


def _pending_check():
    try:
        from .store import PendingStore
        n = len(PendingStore().list_pending())
    except Exception as e:
        return f"pending approvals: unreadable ({type(e).__name__})"
    return f"pending approvals: {n} card(s) waiting in the channel"


def _flags_check():
    lines, blockers = [], []
    for label, env, on in MONDAY_NEEDS:
        if on():
            lines.append(f"flag {label}: on ({env})")
        else:
            lines.append(f"flag {label}: OFF ({env}); Monday needs it")
            blockers.append(f"{env} is off")
    if config.publish_enabled():
        # not a blocker (arming is a human act) but never a silent surprise
        lines.append("flag publish: ARMED (AGENT_PUBLISH_ENABLED). Verify this "
                     "is deliberate; the first post is never automated.")
    else:
        lines.append("flag publish: off (draft only, the default)")
    return lines, blockers


def run(fetch=None, http=None, now=None):
    """The preflight pass. Prints one line per check + the verdict; returns
    {"go": bool, "blockers": [...], "lines": [...]}."""
    from datetime import datetime, timezone
    now_dt = datetime.now(timezone.utc)
    now_ts = now if now is not None else time.time()
    lines, blockers = [], []

    feed_line, feed_blocker = _feed_check(fetch=fetch)
    lines.append(feed_line)
    if feed_blocker:
        blockers.append(feed_blocker)

    r_lines, r_blockers = _runway_check()
    lines.extend(r_lines)
    blockers.extend(r_blockers)

    t_lines, t_blockers = _token_check(http, now_ts)
    lines.extend(t_lines)
    blockers.extend(t_blockers)

    lines.extend(_heartbeat_check(now_dt))
    lines.append(_pending_check())

    f_lines, f_blockers = _flags_check()
    lines.extend(f_lines)
    blockers.extend(f_blockers)

    for line in lines:
        print(f"  {line}")
    if blockers:
        verdict = "MONDAY: NO GO. " + "; ".join(blockers)
    else:
        verdict = "MONDAY: GO"
    print(verdict)
    assert "—" not in verdict and "–" not in verdict
    return {"go": not blockers, "blockers": blockers, "lines": lines}
