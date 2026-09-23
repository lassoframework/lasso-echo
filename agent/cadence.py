"""
cadence.py: per-gym posting cadence (posts per day), the SINGLE source of truth.

Blake's 2x-cadence spec (CADENCE_SPEC.md): a gym posts 1x/day (today's behavior,
the default) or 2x/day. The preference is per ACCOUNT (tenant base), set from the
portal toggle, and does NOTHING until the global kill switch is armed by hand:

    ECHO_CADENCE_2X_ENABLED=true

Resolution order (resolve_posts_per_day):
    flag OFF                -> 1, always (byte-for-byte today, preference ignored)
    store.gym_posts_per_day(base) (shared plane: echo_gym_settings.posts_per_day)
    kv portal_cadence_<base> (local fallback, only when the shared plane says nothing)
    -> 1 (the safe default)

The SHARED PLANE leads because it is the only store both services can see: the
portal writes the owner's toggle there, and each service has its own SQLite, so
the worker's kv is not where that write landed.

Every read failure degrades to 1 — a broken settings read must never double a
gym's posting volume. NEVER weakens a gate: cadence only changes how many PAUSED
pending drafts a day carries; approval and publish gates are untouched.
"""

from . import config, db


def _lasso_summit_daily_applies(base_key, day=None):
    """LASSO Summit daily extra (Blake 2026-09-23): True only for the canonical
    LASSO base while AGENT_LASSO_SUMMIT_DAILY_ENABLED is on AND `day` (default
    today) is inside the dated Sep 23 - Nov 8 2026 window. Never raises; any
    failure degrades to the pre-feature resolution."""
    try:
        # Exact canonical match, mirroring the SQL gate (p_gym_id = 'lasso'):
        # case/whitespace variants and lasso_demo do NOT get the extra capacity.
        return (config.lasso_summit_daily_enabled(day)
                and str(base_key or "") == "lasso")
    except Exception:
        return False


def resolve_posts_per_day(base_key, store=None, *, day=None):
    """The EFFECTIVE posts-per-day for one gym base: 1 or 2 (3 for LASSO during
    the dated Summit daily-extra window only).

    Flag off -> 1 unconditionally. Flag on: the SHARED PLANE wins
    (echo_gym_settings.posts_per_day via an injectable store exposing
    gym_posts_per_day), then the local kv as a fallback, then 1. Any error -> 1.

    SHARED PLANE FIRST (was: local kv first). The portal writes the owner's toggle
    to the shared plane, which is the only store both services can see — each
    service has its own SQLite, so the worker's kv is not where the portal wrote.
    Reading kv first meant a single stale local '1' pinned a gym at 1x forever with
    no way to clear it (db.set_posts_per_day only accepts 1 or 2, so there is no
    'unset'), silently overriding what the owner actually chose. The kv stays as the
    offline/degraded fallback for when the shared plane is unconfigured or down."""
    if _lasso_summit_daily_applies(base_key, day):
        return config.LASSO_SUMMIT_DAILY_CAPACITY
    if not config.cadence_2x_enabled():
        return 1
    if not base_key:
        return 1
    reader = getattr(store, "gym_posts_per_day", None)
    if callable(reader):
        try:
            shared = reader(base_key)
            if shared in (1, 2):
                return int(shared)
        except Exception:
            pass
    try:
        local = db.posts_per_day(base_key)
    except Exception:
        local = None
    if local in (1, 2):
        return local
    return 1


def resolve_posts_per_day_live(base_key, *, day=None):
    """resolve_posts_per_day against the LIVE shared plane: constructs the Supabase
    store when the portal-calendar plane is configured (the worker's normal posture),
    else resolves from local kv only. Flag off -> 1 before any I/O. Never raises;
    every failure degrades to the current behavior (1). The LASSO Summit daily extra
    (3, dated window only) resolves before any store I/O, like the flag-off path."""
    if _lasso_summit_daily_applies(base_key, day):
        return config.LASSO_SUMMIT_DAILY_CAPACITY
    if not config.cadence_2x_enabled():
        return 1
    store = None
    try:
        if config.portal_calendar_supabase_enabled():
            from .portal_calendar_store import SupabaseCalendarStore
            store = SupabaseCalendarStore()
    except Exception:
        store = None
    return resolve_posts_per_day(base_key, store)
