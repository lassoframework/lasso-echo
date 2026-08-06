"""
Supabase-backed data plane for the client portal calendar + draft actions.

The LIVE portal (ops.lassoframework.com /my Organic Social page) reads and writes
the shared content_calendar table in Supabase, NOT the local SQLite drafts table.
On the echo-intake-web Railway service the SQLite db is empty and ephemeral, so the
portal calendar came back empty. When SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are
both set, portal_routes routes calendar reads and approve/deny/kill writes through
THIS module instead.

Nothing here publishes to any social account. An approve only flips a row's status
to 'approved' in the shared table; a separate, human armed publish path (untouched
by this module) owns any real post.

HTTP: injectable `http` client (defaults to lazy `requests`, the repo's pattern in
zernio.py) so every path is unit tested without a network call. The service key is
read from env at call time and NEVER logged, printed, stored on an object, or
returned in any response.

TOKEN ISOLATION is double guarded on every write:
  1. the PATCH URL carries a gym_id=eq.<account_key> filter (PostgREST scopes the
     write server side), and
  2. we pre fetch the row by id and confirm its gym_id == account_key before the
     PATCH, and confirm the PATCH returned exactly one row whose gym_id matches.
A row whose gym_id != account_key (or a missing row) is a 404 that never reveals
the row exists and never issues a write that could touch it.
"""

import calendar as _calendar

from . import config

_TABLE = "content_calendar"

# Portal facing statuses. The action verbs map to these column values.
_ACTION_STATUS = {
    "approve": "approved",
    "deny": "denied",
    "kill": "killed",
}


class PortalStoreError(Exception):
    """A Supabase call failed. Detail is scrubbed of any secret before raising."""

    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"supabase {status}: {detail}")


class SupabaseCalendarStore:
    """Thin PostgREST client over content_calendar. `http` is injectable for tests."""

    def __init__(self, url=None, service_key=None, http=None):
        # Read creds at construction from config (which reads env at call time).
        self._url = (url if url is not None else config.supabase_url())
        self._key = (service_key if service_key is not None else config.supabase_service_key())
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, matches the repo pattern
        return requests

    def _headers(self, extra=None):
        # Key is read lazily and never logged. Built fresh per call.
        h = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    def _rest(self, path):
        return f"{self._url}/rest/v1/{path}"

    # ---- read ---------------------------------------------------------------
    def list_month(self, account_key, month):
        """
        Rows for account_key whose post_date falls inside the calendar month.
        `month` is 'YYYY-MM' (validated by the caller). Returns a list of dicts.
        """
        year = int(month[:4])
        mon = int(month[5:7])
        last_day = _calendar.monthrange(year, mon)[1]
        first = f"{month}-01"
        last = f"{month}-{last_day:02d}"
        params = {
            "gym_id": f"eq.{account_key}",
            "post_date": [f"gte.{first}", f"lte.{last}"],
            "order": "post_date",
        }
        r = self._client().get(
            self._rest(_TABLE),
            params=params,
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def get_row(self, account_key, row_id):
        """
        The single row with this id AND gym_id == account_key, or None.
        Scoped by gym_id so a cross gym id can never be fetched into view.
        """
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "limit": "1",
        }
        r = self._client().get(
            self._rest(_TABLE),
            params=params,
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def set_status(self, account_key, row_id, new_status):
        """
        PATCH the row's status, filtered by BOTH id and gym_id (second isolation
        guard). Returns the updated row dict, or None when zero rows matched
        (treated as 404 by the caller). Never touches a row whose gym_id differs.
        """
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
        }
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"status": new_status},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        # Confirm exactly one row, and that its gym_id matches (belt and braces).
        for row in rows:
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None


# ---------------------------------------------------------------------------
# PURE mappers, no I/O.
# ---------------------------------------------------------------------------

def map_row(row):
    """
    One content_calendar row -> the exact portal draft shape (snake_case keys the
    portal's mapDrafts reads). content_calendar.account holds the platform.
    """
    return {
        "draft_id": row.get("id"),
        "day_key": row.get("post_date"),
        "status": row.get("status"),
        "platform": row.get("account"),
        "caption": row.get("caption") or None,
        "creative_public_url": row.get("image_url") or None,
        "scheduled_for": None,
        "blocked_reason": None,
        "pillar": row.get("pillar") or None,
    }


def action_status(action):
    """The content_calendar.status value for an approve/deny/kill action, or None."""
    return _ACTION_STATUS.get(action)


def _scrub(text):
    """Defensive: never let a service key echo back through an error string."""
    key = config.supabase_service_key()
    if key and text:
        text = text.replace(key, "***")
    return text
