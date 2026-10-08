"""Permanent LASSO-only cadence recovery lane (AGENT_LASSO_CADENCE_RECOVERY).

The once-per-day draw stamps its day BEFORE it runs (listener claim-first guard),
so a draw interrupted before its tail never retries the LASSO held-media repair
and paired-Story preparation that live at that tail. This module is the
independent backstop: two per-account prep workers and one publisher worker on
their own daemon threads, each behind a stable per-worker process flock beside
the durable DB, re-reading every gate each cycle and failing closed.

Safety contract:
  - Default OFF (AGENT_LASSO_CADENCE_RECOVERY). Composes with, never replaces,
    the existing gates: calendar autopublish + publish flags (inside
    publish_due), AGENT_LASSO_3X_ENABLED (inside both prep jobs), and the
    authoritative store.gym_autonomy('lasso') is True read EVERY cycle — None
    or False holds the cycle.
  - Never calls run_daily and never touches a client gym: account coverage is
    exactly lasso_ig/lasso_fb and gym_id='lasso'.
  - Prep reuses jobs/lasso_held_media_repair.run and
    jobs/lasso_daily_paired_stories.run unchanged: artifact leases, per-run
    caps, exact-row CAS and the guarded Story RPCs stay the only mutators.
  - The publisher calls stock calendar_autopublish.publish_due
    (catch_all=False, approved_only=False, dynamic _client_publish_limits);
    the per-row SQL claim remains the final exactly-once authority. No
    direct-provider calls, no claim bypass, no build_client_month lock.
  - KV receipts carry counts and timestamps only — never captions, tokens, or
    customer data.
"""

import contextlib
import fcntl
import json
import os
import time
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from agent import config

# (account_key for held_media_repair, account for lasso_daily_paired_stories)
ACCOUNTS = (("lasso_ig", "instagram"), ("lasso_fb", "facebook"))

PUBLISH_INTERVAL_SECONDS = 60
PREP_INTERVAL_SECONDS = 300
_MIN_INTERVAL_SECONDS = 15
_MAX_INTERVAL_SECONDS = 3600
# Bounded failure backoff: a failing cycle retries at the interval, then 2x/4x/8x
# up to this ceiling. Paid-render spend stays bounded by the jobs' own artifact
# lease cooldown and per-run caps, which this lane never widens.
_BACKOFF_CEILING_SECONDS = 900

_KV_PREFIX = "lasso_cadence_recovery_v1:"


def enabled():
    return str(os.environ.get("AGENT_LASSO_CADENCE_RECOVERY", "false")
               ).strip().lower() in {"1", "true", "yes", "on"}


def _interval(env_name, default):
    try:
        value = int(os.environ.get(env_name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return max(_MIN_INTERVAL_SECONDS, min(_MAX_INTERVAL_SECONDS, value))


def publish_interval_seconds():
    return _interval("AGENT_LASSO_CADENCE_RECOVERY_PUB_SECONDS",
                     PUBLISH_INTERVAL_SECONDS)


def prep_interval_seconds():
    return _interval("AGENT_LASSO_CADENCE_RECOVERY_PREP_SECONDS",
                     PREP_INTERVAL_SECONDS)


def _local_now(now=None):
    """Fresh actual now in the posting timezone (never a caller-cached day)."""
    instant = now or datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    if isinstance(instant, str):
        instant = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if isinstance(instant, date) and not isinstance(instant, datetime):
        instant = datetime.combine(instant, dtime.min,
                                   ZoneInfo(config.POSTING_TIMEZONE))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=ZoneInfo(config.POSTING_TIMEZONE))
    return instant.astimezone(ZoneInfo(config.POSTING_TIMEZONE))


def _lasso_autonomous(store):
    """Authoritative shared autonomy for 'lasso', re-read every call.

    Unknown (None) or unreadable is NOT autonomous: fail closed.
    """
    try:
        if store is None:
            from agent.portal_calendar_store import SupabaseCalendarStore
            store = SupabaseCalendarStore()
        return store.gym_autonomy("lasso") is True
    except Exception:
        return False


@contextlib.contextmanager
def _worker_flock(name):
    """Stable nonblocking flock on a permanent inode beside the durable DB.

    Yields True with the lock held, False when the lane must skip this cycle:
    kv not durable, the lock file cannot open, or another worker holds it.
    The inode is never unlinked; the lock is always released on exit.
    """
    from agent import db
    lock_file = None
    held = False
    try:
        try:
            if not db.kv_is_durable():
                yield False
                return
            lock_file = open(os.path.realpath(db.db_path())
                             + f".lasso-cadence-{name}.lock", "a")
        except OSError:
            yield False
            return
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError:
            yield False
            return
        yield True
    finally:
        if lock_file is not None:
            if held:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            lock_file.close()


def _record(worker, receipt):
    """Best-effort durable observability: counts/timestamps only."""
    try:
        from agent import db
        db.kv_set(_KV_PREFIX + worker, json.dumps(receipt, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - observability never blocks a cycle
        print(f"[lasso-cadence] state record failed for {worker}: "
              f"{type(exc).__name__}")


def _publish_limits(day):
    from agent import calendar_autopublish
    return calendar_autopublish._client_publish_limits(
        "lasso", day, config.client_daily_publish_cap())


def run_prep_once(account_key, *, now=None, store=None, artifact_store=None):
    """One bounded prep cycle for ONE LASSO account. Never publishes."""
    account = dict(ACCOUNTS).get(account_key)
    summary = {"ok": False, "worker": f"prep-{account_key}"}
    if account is None:
        summary["reason"] = "unknown account"
        return summary
    if not enabled():
        summary["reason"] = "lane disabled"
        return summary
    if not _lasso_autonomous(store):
        summary["reason"] = "lasso autonomy off or unknown"
        return summary
    with _worker_flock(f"prep-{account_key}") as held:
        if not held:
            summary["reason"] = "prep lock unavailable or kv not durable"
            return summary
        instant = _local_now(now)
        day = instant.date().isoformat()
        receipt = {"begun_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                   "day": day}
        _record(summary["worker"], dict(receipt, state="begun"))
        from agent.jobs import lasso_daily_paired_stories, lasso_held_media_repair
        repair = {}
        try:
            repair = lasso_held_media_repair.run(
                now=instant, account_key=account_key, store=store,
                artifact_store=artifact_store) or {}
        except Exception as exc:  # noqa: BLE001 - one account never stalls the other
            repair = {"ok": False, "reason": type(exc).__name__}
        stories = {}
        try:
            catchup_days, _ = _publish_limits(day)
            stories = lasso_daily_paired_stories.run(
                now=instant, account=account, store=store,
                artifact_store=artifact_store, catchup_days=catchup_days) or {}
        except Exception as exc:  # noqa: BLE001
            stories = {"ok": False, "reason": type(exc).__name__}
        summary.update({
            "ok": (repair.get("ok") is True and stories.get("ok") is True
                   and not repair.get("errors") and not stories.get("blocked")),
            "day": day,
            "repair": {k: repair.get(k) for k in
                       ("ok", "attempted", "generated", "reused", "repaired",
                        "skipped", "errors", "reason") if k in repair},
            "stories": {k: stories.get(k) for k in
                        ("ok", "eligible", "generated", "reused", "staged",
                         "repaired", "occupied", "blocked", "reason")
                        if k in stories},
        })
        receipt.update(state="completed", summary=summary,
                       finished_at=datetime.now(ZoneInfo("UTC")).isoformat())
        _record(summary["worker"], receipt)
        return summary


def run_publish_once(*, now=None, store=None):
    """One slot-gated LASSO publish tick through stock publish_due."""
    summary = {"ok": False, "worker": "publish"}
    if not enabled():
        summary["reason"] = "lane disabled"
        return summary
    gates = (("publish disabled", config.publish_enabled),
             ("calendar autopublish disabled", config.calendar_autopublish_enabled),
             ("three feed cadence disabled", config.lasso_three_feed_enabled),
             ("LASSO Zernio routing disabled", config.lasso_via_zernio_enabled),
             ("Zernio publishing disabled", config.zernio_publish_enabled))
    for reason, gate in gates:
        if not gate():
            summary["reason"] = reason
            return summary
    if not _lasso_autonomous(store):
        summary["reason"] = "lasso autonomy off or unknown"
        return summary
    with _worker_flock("publish") as held:
        if not held:
            summary["reason"] = "publish lock unavailable or kv not durable"
            return summary
        instant = _local_now(now)
        day = instant.date().isoformat()
        receipt = {"begun_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                   "day": day}
        _record(summary["worker"], dict(receipt, state="begun"))
        from agent import calendar_autopublish
        catchup_days, daily_cap = _publish_limits(day)
        result = calendar_autopublish.publish_due(
            day, gym_id="lasso", store=store, now=instant,
            catch_all=False, approved_only=False,
            catchup_days=catchup_days, daily_cap=daily_cap)
        summary.update({
            "ok": bool((result or {}).get("ok")),
            "day": day,
            "published": len((result or {}).get("published") or []),
            "failed": len((result or {}).get("failed") or []),
            "waiting": len((result or {}).get("waiting") or []),
            "reason": (result or {}).get("reason", "ok"),
        })
        receipt.update(state="completed", summary=summary,
                       finished_at=datetime.now(ZoneInfo("UTC")).isoformat())
        _record(summary["worker"], receipt)
        return summary


def _worker_loop(step, interval_fn, stop, sleep, label):
    """Bounded loop: retry exceptions without dying, back off on repeats."""
    failures = 0
    while True:
        if stop is not None and stop():
            return
        try:
            result = step()
            if isinstance(result, dict) and result.get("ok") is False:
                failures = min(failures + 1, 4)
                delay = min(interval_fn() * (2 ** failures),
                            _BACKOFF_CEILING_SECONDS)
            else:
                failures = 0
                delay = interval_fn()
        except Exception as exc:  # noqa: BLE001 - a worker thread must never die
            failures = min(failures + 1, 4)
            delay = min(interval_fn() * (2 ** failures), _BACKOFF_CEILING_SECONDS)
            print(f"[lasso-cadence] {label} cycle failed "
                  f"({failures} consecutive): {type(exc).__name__}")
        sleep(delay)


def prep_worker(account_key, *, stop=None, sleep=time.sleep, step=None):
    """Daemon body for one account's prep lane (own flock, own cadence)."""
    _worker_loop(step or (lambda: run_prep_once(account_key)),
                 prep_interval_seconds, stop, sleep, f"prep-{account_key}")


def publish_worker(*, stop=None, sleep=time.sleep, step=None):
    """Daemon body for the LASSO publisher lane (separate flock + cadence)."""
    _worker_loop(step or run_publish_once,
                 publish_interval_seconds, stop, sleep, "publish")
