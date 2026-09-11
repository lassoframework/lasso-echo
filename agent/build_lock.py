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
self-clears after STALE_SECONDS so a rebuild can never be wedged forever by a
lock nobody will ever release.

Pure except for the kv reads/writes; never raises. A kv failure is treated as
"could not confirm the lock is free" and refuses acquisition (fail CLOSED --
better to skip one rebuild pass than let two race), except release, which is
always best-effort.
"""
from __future__ import annotations

import os
import time

_KEY_PREFIX = "build_lock:"
STALE_SECONDS = 15 * 60  # a rebuild that holds the lock this long is presumed dead


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
