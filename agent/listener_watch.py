"""
listener_watch.py — Echo notices when a DESKTOP service that Echo depends on has died.

WHY THIS EXISTS (2026-09-02): scout-listener, the process that picks Echo's support tickets
and ops-fix requests out of #echosupport and relays them to Claude Code, crash-looped 47
times on a MODULE_NOT_FOUND and nobody knew. Client support tickets sat untriaged for hours.
The only evidence was a stderr file no human reads. Echo alerts loudly when a GYM's calendar
breaks; nothing alerted when the thing that reads those alerts was itself face down.

The asymmetry that makes this a cloud job: a dead process cannot report its own death, and a
sleeping Mac cannot alert anyone. So the desktop service PINGS Echo (which is always up on
Railway) on a schedule, Echo records the last-seen time, and Echo's own periodic lane alerts
when a ping goes stale. Absence of a ping IS the signal.

DELIBERATELY NOT A LIVENESS PROBE FROM ECHO OUTWARD: the desktop has no public address, no
stable IP, and is often asleep. Only the inward direction works.

Rails:
  - a stale heartbeat alerts ONCE per staleness episode, not every pass (kv-stamped), so a
    Mac that is off for a weekend does not produce hundreds of alerts.
  - recovery is announced once too, so "it is back" is as visible as "it is down".
  - a source that has NEVER pinged is silent by design (it may simply not be deployed yet);
    only a source that pinged and then stopped is a failure. A never-armed watch that
    screams on day one gets muted, and a muted watch is worse than none.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# How long a source may go silent before Echo calls it down. The listener pings every 5
# minutes (see scout-listener), so 20 minutes is four missed pings: long enough that a
# restart, a reconnect storm or a laptop lid never trips it, short enough that a real death
# is caught inside a coffee break.
DEFAULT_STALE_AFTER = 20 * 60

# Known desktop sources Echo depends on. A source not in here is still recordable (the
# endpoint is generic) but is never swept, so a typo cannot invent a permanent alert.
SOURCES = ("scout-listener",)

_SEEN_KEY = "listener_seen_{source}"
_ALERTED_KEY = "listener_down_alerted_{source}"


def _now(now=None):
    return now or datetime.now(timezone.utc)


# ---- authentication without distributing a new secret --------------------------------
# The desktop service and Echo ALREADY share exactly one value: Echo's Slack bot token
# (the listener holds it as ECHO_SLACK_BOT_TOKEN to post as Echo; Echo holds it as
# AGENT_SLACK_BOT_TOKEN). So the heartbeat is signed with an HMAC keyed on that shared
# value rather than inventing a second secret that would have to be copied onto a machine
# whose .env this process cannot write. The token itself never travels: only
# HMAC(key, "source:timestamp") does.
#
# Replay window: a signature is only accepted for SKEW_TOLERANCE around now, so a captured
# heartbeat cannot be replayed tomorrow to mask a dead listener. Suppressing a
# down-alert is the only thing a forged heartbeat could achieve, which is precisely why
# this is signed rather than open.
SKEW_TOLERANCE = 10 * 60


def sign(source, ts, key) -> str:
    """HMAC-SHA256 hex of "<source>:<ts>" under `key`. Empty string when key is missing,
    which the verifier treats as "cannot authenticate" and refuses."""
    import hashlib
    import hmac as _hmac
    if not key:
        return ""
    msg = f"{str(source or '')}:{str(ts or '')}".encode("utf-8")
    return _hmac.new(str(key).encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify(source, ts, signature, key, *, now=None, tolerance=SKEW_TOLERANCE) -> bool:
    """Constant-time signature check plus a freshness window. False on anything odd —
    missing key, unparseable ts, stale/future timestamp, bad signature."""
    import hmac as _hmac
    expected = sign(source, ts, key)
    if not expected or not signature:
        return False
    if not _hmac.compare_digest(str(signature), expected):
        return False
    try:
        sent = float(ts)
    except (TypeError, ValueError):
        return False
    delta = abs(_now(now).timestamp() - sent)
    return delta <= tolerance


def record(source, *, db=None, now=None) -> bool:
    """Stamp `source` as alive right now. True when recorded. Never raises."""
    src = str(source or "").strip()
    if not src:
        return False
    try:
        if db is None:
            from . import db as db
        db.kv_set(_SEEN_KEY.format(source=src), _now(now).isoformat())
        return True
    except Exception as exc:  # noqa: BLE001 - a heartbeat must never 500 the endpoint
        print(f"[listener-watch] could not record {src}: {type(exc).__name__}")
        return False


def last_seen(source, *, db=None):
    """The datetime `source` was last seen, or None when it has never pinged."""
    try:
        if db is None:
            from . import db as db
        raw = db.kv_get(_SEEN_KEY.format(source=str(source or "").strip())) or ""
        if not raw:
            return None
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001 - unreadable/corrupt stamp reads as never-seen
        return None


def sweep(*, db=None, alert=None, stale_after=DEFAULT_STALE_AFTER, now=None, logger=None):
    """Alert on any known source that pinged before and has now gone quiet, and announce a
    recovery once. Returns a summary dict. Never raises out.

    Three-state per source, the same kv idiom the rest of this repo uses for
    alert-once-then-shut-up watches (see zernio_profile_link's grace clock):
      never seen        -> silent (may simply not be deployed)
      seen recently     -> healthy; if it had been alerted, announce recovery and clear
      seen but stale    -> alert ONCE, stamp, stay quiet until it recovers
    """
    log = logger or (lambda m: print(f"[listener-watch] {m}"))
    if db is None:
        from . import db as db
    if alert is None:
        from . import ops_alerts
        alert = ops_alerts.alert
    t = _now(now)
    out = {"checked": 0, "down": 0, "recovered": 0, "healthy": 0, "never_seen": 0}
    for src in SOURCES:
        out["checked"] += 1
        seen = last_seen(src, db=db)
        akey = _ALERTED_KEY.format(source=src)
        try:
            already = bool(db.kv_get(akey))
        except Exception:  # noqa: BLE001
            already = False
        if seen is None:
            out["never_seen"] += 1
            continue
        stale_for = (t - seen).total_seconds()
        if stale_for <= stale_after:
            out["healthy"] += 1
            if already:
                out["recovered"] += 1
                try:
                    db.kv_set(akey, "")
                except Exception:  # noqa: BLE001
                    pass
                alert(f"{src} is back: it checked in "
                      f"{int(stale_for // 60)} minute(s) ago. Support tickets and ops-fix "
                      "requests in #echosupport are being picked up again.")
            continue
        if already:
            log(f"{src} still down ({int(stale_for // 60)}m); already alerted")
            continue
        out["down"] += 1
        try:
            db.kv_set(akey, t.isoformat())
        except Exception:  # noqa: BLE001
            pass
        alert(f"{src} has not checked in for {int(stale_for // 60)} minute(s). While it is "
              "down NOTHING in #echosupport gets picked up: gym support tickets go "
              "untriaged and ops-fix requests are ignored (Echo keeps alerting normally, "
              "but no one is reading them). It runs on Blake's Mac under launchd, so check "
              "that the machine is awake, then: launchctl print "
              "gui/501/com.lasso.scout-listener | grep -E 'state|last exit' and "
              "tail ~/scout-listener/logs/launchd.err.log")
    return out
