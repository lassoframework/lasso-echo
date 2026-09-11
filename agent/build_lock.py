"""
build_lock.py: a per-gym advisory lock around build_client_month (the calendar
rebuild path), so two overlapping invocations for the SAME gym can never race.

WHY THIS EXISTS
---------------
Support ticket 4941e162-2923-495f-8efb-d2554dea5aec, CrossFit Reverb / Dean
Holcomb, 2026-09-11. The ticket sat stuck in `fixing`, recovered twice by the
FIXER's stale-run recovery. Ground truth in content_calendar showed FIVE
separate insert timestamps within about two hours (02:28, 02:31, 02:38, 04:38,
04:44 UTC), several of them landing near-duplicate captions on the SAME
post_date as PENDING rows side by side (e.g. two near-identical "You started
CrossFit thinking you'd hate it..." feeds both dated 2026-09-13). That is only
possible if build_client_month ran more than once concurrently for the same
gym: delete_month (portal_calendar_store.delete_month) deletes-then-inserts
per invocation, so a clean serial sequence of reruns replaces the month; only
an OVERLAPPING pair (nightly client_media_sync scan racing a FIXER-triggered
manual rebuild, or two stale-run retries that were never actually both killed)
can leave both runs' rows standing side by side.

There is no scheduler-level mutex anywhere in this codebase for the rebuild
path (client_media_sync's nightly scan, a manual restage script, and any
future ops trigger all call build_client_month directly with no
coordination). This module is that mutex, backed by the SAME durable kv store
already used for build-state stamps (agent.db.kv_get/kv_set, the /data volume
in production), so it holds across process boundaries on one deployed
instance without a new migration or new dependency.

Not a strict distributed lock (no fencing token, no cross-instance quorum):
this is Railway's single-instance `echo` service, and the goal is "the same
gym's rebuild never overlaps itself locally", not general distributed mutual
exclusion. A stale lock (holder crashed / process died without releasing)
self-clears after HEARTBEAT_STALE_SECONDS so a rebuild can never be wedged
forever by a lock nobody will ever release.

Pure except for the kv reads/writes; never raises. A kv failure is treated as
"could not confirm the lock is free" and refuses acquisition (fail CLOSED --
better to skip one rebuild pass than let two race), except release, which is
always best-effort.

HEARTBEAT, NOT A BIGGER STATIC TIMEOUT (Blake, 2026-09-11, follow-up audit)
---------------------------------------------------------------------------
The original static STALE_SECONDS = 15 * 60 was picked without checking it
against the actual worst-case LEGITIMATE runtime of a single
build_client_month call. Auditing that number turned up:
  * agent/config.py rendition_max_per_build() defaults to 8, and
    agent/gym_media_index.py RENDITION_TIMEOUT_SEC = 180 (per clip) --
    8 x 180s = 1440s = 24 minutes of transcoding budget ALONE, already past
    15 minutes.
  * On top of that, agent/drafter.py's SB7 caption generation calls Claude
    (agent/drafter.py _call_llm_caption, no per-call timeout override, so it
    inherits the anthropic SDK's default ~600s request timeout) up to FOUR
    times per caption (initial compose + up to three conditional retries:
    opening-collision, dropped-name, figure-gate -- agent/drafter.py
    build(), lines ~941-994), for up to 31 days x 2 cadence slots
    (agent/plan_horizon.py plan_horizon_days(), config AGENT_PLAN_HORIZON_DAYS,
    default 31; ECHO_CADENCE_2X_ENABLED cadence) = up to 62 captions, i.e. up
    to ~248 LLM calls in one build.
  * The Drive media lane (agent/gym_media_builder.py, "trying the next
    asset" loop ~lines 260-333) can also call agent/vision.py's Gemini
    reader (_vision_reader, _verify_reader) with NO explicit timeout kwarg
    at all, once or twice per candidate photo it tries, for every day the
    Drive lane fills -- an unbounded-by-code multiplier on top of the two
    numbers above.
  A precise "worst legitimate runtime" number is therefore NOT knowable from
  a static read of the code: it depends on how many of those paths run slow-
  but-successful in the same build, and at least two of them (the Claude and
  Gemini calls) have no explicit timeout in this codebase at all, only
  whatever the SDK defaults to. Picking a bigger static number just moves the
  goalpost to the next unaudited slow step (a new rendition budget, a new
  vision pass, a longer caption loop) with the exact same failure mode.

  The fix is to stop measuring staleness from "time since acquisition" and
  measure it from "time since last sign of life" instead: start_heartbeat()
  spawns a lightweight background thread that re-stamps the lock's timestamp
  on HEARTBEAT_INTERVAL_SECONDS while the holder is still actively working,
  regardless of which phase of the build is running or how long it takes. A
  live build (however long its captions/transcodes/vision calls take) just
  keeps proving it is alive and never goes stale. A genuinely crashed/silent
  holder (the heartbeat thread dies with the process) stops heartbeating and
  is reclaimed within HEARTBEAT_STALE_SECONDS -- a SHORT window (a few
  minutes) instead of a long fixed timeout, because detecting a dead holder
  no longer has to out-wait the slowest possible legitimate build.

  Degrades safely: a single heartbeat write failure (a kv hiccup) never
  raises and never releases the lock early -- see heartbeat() below. It
  simply fails to renew that one time; as long as a LATER heartbeat succeeds
  before HEARTBEAT_STALE_SECONDS elapses, the lock never goes stale. Only a
  holder that fails to renew for the ENTIRE stale window (i.e. is actually
  dead, or the kv store is down for minutes on end -- itself an ops
  emergency this lock cannot paper over) loses the lock.
"""
from __future__ import annotations

import os
import threading
import time

_KEY_PREFIX = "build_lock:"

# Backward-compatible alias: some callers/tests referred to STALE_SECONDS.
# Semantics have changed (see module docstring): staleness is now measured
# from the last HEARTBEAT, not from acquisition time, so this window can be
# (and is) much shorter than the old 15-minute static timeout.
HEARTBEAT_STALE_SECONDS = 4 * 60   # a holder that hasn't heartbeat in this long is presumed dead
STALE_SECONDS = HEARTBEAT_STALE_SECONDS  # legacy name, kept for any external reference

# How often a live holder re-stamps its own lock while it still holds it.
# Comfortably smaller than HEARTBEAT_STALE_SECONDS (about a 5x margin) so a
# handful of missed/failed heartbeat writes in a row still cannot let the
# lock go stale out from under a live build.
HEARTBEAT_INTERVAL_SECONDS = 45


def _key(base_key: str) -> str:
    return f"{_KEY_PREFIX}{str(base_key or '').strip().lower()}"


def _now() -> float:
    return time.time()


def acquire(base_key: str, *, holder: str = "") -> bool:
    """True when the per-gym build lock was acquired (or is already held by
    THIS holder token, so a retry from the same caller-issued token is not
    refused). False when another live holder has it, or the kv store could
    not be read (fail closed). Never raises."""
    base = str(base_key or "").strip().lower()
    if not base:
        return False
    holder = str(holder or f"{os.getpid()}")
    try:
        from . import db
        raw = db.kv_get(_key(base), "")
        if raw:
            try:
                held_holder, held_at = raw.split(":", 1)
                held_at = float(held_at)
            except (ValueError, TypeError):
                held_holder, held_at = "", 0.0
            fresh = (_now() - held_at) < STALE_SECONDS
            if fresh and held_holder != holder:
                return False  # another live build owns this gym right now
        db.kv_set(_key(base), f"{holder}:{_now()}")
        return True
    except Exception:  # noqa: BLE001 - an unreadable lock store refuses, never races
        return False


def release(base_key: str, *, holder: str = "") -> None:
    """Best-effort release. Only clears the lock when it still belongs to
    `holder` (or holder is not supplied), so a stale-run recovery that kills
    the old process and starts a new one never has the old process's delayed
    release clobber the new run's live lock. Never raises."""
    base = str(base_key or "").strip().lower()
    if not base:
        return
    try:
        from . import db
        raw = db.kv_get(_key(base), "")
        if not raw:
            return
        held_holder = raw.split(":", 1)[0]
        if holder and held_holder != str(holder):
            return  # not ours to clear
        db.kv_set(_key(base), "")
    except Exception:  # noqa: BLE001 - release is always best-effort
        pass


def is_locked(base_key: str) -> bool:
    """True when a live (non-stale) lock is currently held for this gym.
    Read-only convenience for callers that want to log/report without
    attempting acquisition. Never raises."""
    base = str(base_key or "").strip().lower()
    if not base:
        return False
    try:
        from . import db
        raw = db.kv_get(_key(base), "")
        if not raw:
            return False
        _held_holder, held_at = raw.split(":", 1)
        return (_now() - float(held_at)) < STALE_SECONDS
    except Exception:  # noqa: BLE001
        return False


def heartbeat(base_key: str, *, holder: str = "") -> bool:
    """Re-stamp the lock's timestamp so a live holder keeps proving it is
    alive, independent of how long its own work takes. This is the ONLY
    thing that should ever make HEARTBEAT_STALE_SECONDS staleness detection
    NOT fire on a long-running but legitimate build.

    Renews (returns True) when the lock is unheld, or already held by
    `holder`, exactly like acquire() -- a heartbeat from the current holder
    is just a renewal, never a re-negotiation. Returns False, WITHOUT
    raising and WITHOUT touching the store, when another holder now owns a
    fresh lock (this holder lost the lock to a takeover, e.g. it went quiet
    long enough to be reclaimed and should stop working) or the kv store
    could not be read/written (fail-open for heartbeats specifically: a
    transient kv hiccup must never crash the build or force an early
    release -- the NEXT heartbeat attempt gets another chance before
    HEARTBEAT_STALE_SECONDS actually elapses; only a holder that fails to
    renew for the WHOLE stale window is ever reclaimed). Never raises."""
    base = str(base_key or "").strip().lower()
    if not base:
        return False
    holder = str(holder or f"{os.getpid()}")
    try:
        from . import db
        raw = db.kv_get(_key(base), "")
        if raw:
            try:
                held_holder, held_at = raw.split(":", 1)
                held_at = float(held_at)
            except (ValueError, TypeError):
                held_holder, held_at = "", 0.0
            fresh = (_now() - held_at) < HEARTBEAT_STALE_SECONDS
            if fresh and held_holder != holder:
                return False  # someone else now legitimately owns this gym's lock
        db.kv_set(_key(base), f"{holder}:{_now()}")
        return True
    except Exception:  # noqa: BLE001 - a heartbeat hiccup never crashes/races the build
        return False


class HeartbeatHandle:
    """A background thread that calls heartbeat() on HEARTBEAT_INTERVAL_SECONDS
    for as long as the caller still legitimately holds the lock. Returned by
    start_heartbeat(); the caller MUST call .stop() in the SAME finally block
    that calls release(), before releasing (so the thread never fires a
    heartbeat write after the lock has already been given up).

    A build that never calls start_heartbeat (or ignores the handle) behaves
    exactly like the pre-heartbeat lock: fine for short-lived callers, but a
    genuinely long build needs the handle running for HEARTBEAT_STALE_SECONDS
    detection to ever see it as "alive" past the old static window."""

    def __init__(self, base_key: str, *, holder: str = "",
                 interval: float = HEARTBEAT_INTERVAL_SECONDS):
        self._base_key = base_key
        self._holder = holder
        # A tiny floor (not 1s+) so tests can drive this with a fast interval;
        # production callers always pass the real HEARTBEAT_INTERVAL_SECONDS.
        self._interval = max(0.01, float(interval or HEARTBEAT_INTERVAL_SECONDS))
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"build-lock-heartbeat:{base_key}", daemon=True)

    def _run(self) -> None:
        # Wait FIRST: acquire() already stamped "now" the moment the lock was
        # taken, so the first renewal is due one interval later, not instantly.
        while not self._stop_event.wait(self._interval):
            try:
                heartbeat(self._base_key, holder=self._holder)
            except Exception:  # noqa: BLE001 - the thread must never die loudly
                pass

    def start(self) -> "HeartbeatHandle":
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the background thread. Idempotent; never raises."""
        try:
            self._stop_event.set()
            if self._thread.is_alive():
                self._thread.join(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


def start_heartbeat(base_key: str, *, holder: str = "",
                     interval: float = HEARTBEAT_INTERVAL_SECONDS) -> HeartbeatHandle:
    """Start a background heartbeat for a lock this caller already holds
    (i.e. call this right after a successful acquire()). Returns a
    HeartbeatHandle; the caller MUST call .stop() on it in the finally block
    that also calls release(), stopping the heartbeat BEFORE releasing so no
    heartbeat can fire after the lock is given up. Never raises (the thread
    itself swallows every heartbeat error; only Thread creation could raise,
    and that is a fatal interpreter-level condition this module does not
    try to paper over)."""
    handle = HeartbeatHandle(base_key, holder=holder, interval=interval)
    handle.start()
    return handle
