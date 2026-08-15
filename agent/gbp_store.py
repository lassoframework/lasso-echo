"""
GBP DB lane — the thin Supabase reads/writes the publish worker + reconcile need,
layered over the existing SupabaseCalendarStore client (same creds, same PostgREST
pattern, same scrub-on-error). Kept separate from portal_calendar_store so the GBP rail
is self-contained. Everything here is content_calendar (read-side mirror) + the
gym_gbp_connections table; NOTHING publishes.
"""

from . import config
from .portal_calendar_store import SupabaseCalendarStore, PortalStoreError, _scrub

_CAL = "content_calendar"
_CONN = "gym_gbp_connections"
PLATFORM = "googlebusiness"


class GbpStore:
    """Reads/writes for the GBP worker. Wraps a SupabaseCalendarStore for its live
    httpx client + auth. available() is False without portal creds (worker no-ops)."""

    def __init__(self, base=None):
        self._s = base or SupabaseCalendarStore()

    def available(self):
        return getattr(self._s, "available", lambda: True)()

    # ---- reads --------------------------------------------------------------
    def approved_gbp_rows(self, run_date):
        """APPROVED googlebusiness rows due on/before run_date, not yet published. The
        human tap set status='approved'; the worker never approves. image_url present."""
        params = {
            "account": f"eq.{PLATFORM}",
            "status": "eq.approved",
            "post_date": f"lte.{run_date}",
            "published_at": "is.null",
            "image_url": "not.is.null",
            "order": "post_date",
        }
        return self._get(_CAL, params)

    def recent_published_gbp(self, since_iso):
        """PUBLISHED googlebusiness rows with a late_post_id whose published_at is within
        the reconcile window (>= since_iso). Feeds the hourly-for-48h §7.2 poll."""
        params = {
            "account": f"eq.{PLATFORM}",
            "status": "eq.published",
            "published_at": f"gte.{since_iso}",
            "late_post_id": "not.is.null",
            "order": "published_at",
        }
        return self._get(_CAL, params)

    def connections_for(self, portal_gym_key):
        """gym_gbp_connections rows for a gym (any status). The worker filters to
        status='connected' and routes by gbp_location_id."""
        return self._get(_CONN, {"portal_gym_key": f"eq.{portal_gym_key}",
                                 "order": "connected_at"})

    def all_connections(self):
        """Every connection row (for the nightly token-health sweep, Phase 1 §3.3, which
        the portal owns — exposed here read-only for staff tooling)."""
        return self._get(_CONN, {"order": "portal_gym_key"})

    # ---- writes (content_calendar status only; never an approval) -----------
    def mark_published(self, row_id, late_post_id, published_at):
        return self._patch(row_id, {"status": "published",
                                    "late_post_id": late_post_id or "",
                                    "published_at": published_at})

    def mark_failed(self, row_id, reject_reason):
        """§7.1/§7.2: a failed GBP post -> status='failed' + plain-English reason. NEVER
        auto-requeued; a coach fixes and requeues through the Echo write path."""
        return self._patch(row_id, {"status": "failed",
                                    "reject_reason": (reject_reason or "")[:500]})

    def mark_status(self, row_id, status):
        return self._patch(row_id, {"status": status})

    # ---- plumbing -----------------------------------------------------------
    def _get(self, table, params):
        r = self._s._client().get(self._s._rest(table), params=params,
                                  headers=self._s._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def _patch(self, row_id, fields):
        r = self._s._client().patch(
            self._s._rest(_CAL), params={"id": f"eq.{row_id}"},
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json=fields, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None
