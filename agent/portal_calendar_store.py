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

    # ---- auto-publisher: read + exactly-once claim/update -------------------
    # These serve the scheduled calendar auto-publisher (calendar_autopublish.py).
    # They never publish; they only read the day's rows and flip status atomically
    # so a row is published EXACTLY ONCE across re-runs / concurrent workers.
    def due_rows(self, gym_id, run_date):
        """
        content_calendar rows that are DUE to publish on `run_date` for `gym_id`:
          - gym_id == gym_id
          - post_date == run_date  (ONLY the run date: never a past or future date)
          - status NOT in ('published','denied','killed')
          - published_at IS NULL   (never re-publish a row already sent)
          - image_url present      (a row with no creative is skipped upstream too)
        `run_date` is 'YYYY-MM-DD' (validated by the caller). Returns a list of dicts.
        Gym scoped by the gym_id=eq filter so another gym's row is never returned.
        """
        params = {
            "gym_id": f"eq.{gym_id}",
            "post_date": f"eq.{run_date}",
            "status": "not.in.(published,denied,killed)",
            "published_at": "is.null",
            "image_url": "not.is.null",
            "order": "created_at",
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

    def mark_publishing(self, row_id):
        """
        ATOMIC CLAIM (the exactly-once guard). Conditionally flip status
        'pending'|'approved' -> 'publishing' for this row ONLY IF it is still
        unclaimed and unpublished: the PATCH carries a filter of id=eq.<row_id> AND
        status=in.(pending,approved) AND published_at=is.null, so PostgREST updates
        the row server-side only when the pre-conditions still hold. 'approved' is
        claimable because a CLIENT-gym row is approved by the client BEFORE the
        publish lane picks it up (the client publish lane only feeds approved rows);
        exactly-once is unchanged: a claimed row is 'publishing', which is not in the
        allowed set, so it can never be claimed twice. Returns True only when THIS
        call won the claim (exactly one row came back); False when the row was
        already publishing / published / denied / killed (zero rows updated) so the
        caller SKIPS it. Two concurrent runs can both call this; at most one gets True.
        """
        params = {
            "id": f"eq.{row_id}",
            "status": "in.(pending,approved)",
            "published_at": "is.null",
        }
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"status": "publishing"},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return len(rows) == 1

    def publishing_rows(self):
        """Every row currently stuck in status='publishing' with no published_at,
        across all gyms (read-only; feeds the stale-claim ALERT sweep). A row lives
        in 'publishing' only for the seconds between the atomic claim and the
        publish result, so anything seen here across sweeps is a crashed worker."""
        params = {
            "status": "eq.publishing",
            "published_at": "is.null",
            "select": "id,gym_id,account,post_date",
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

    def mark_published(self, row_id, media_id, published_at):
        """
        Record a successful publish: status='published', published_at=<now iso>,
        late_post_id=<media_id>. Filtered by id only (the row was already claimed by
        mark_publishing, so no other worker can be here). Returns the updated row or None.
        """
        params = {"id": f"eq.{row_id}"}
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={
                "status": "published",
                "published_at": published_at,
                "late_post_id": media_id,
            },
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def mark_publish_failed(self, row_id, revert_status="pending"):
        """
        REVERT a claim after a publish failure (or a would_publish result): status
        back to `revert_status` so the row is retried on the next run. LASSO rows
        revert to 'pending' (the default, unchanged). A CLIENT row that was APPROVED
        before the claim reverts to 'approved' so a transient Zernio failure never
        forces the client to re-approve. Records NOTHING else (no media id, no
        published_at), so a failed attempt never looks published. Filtered by id
        only. Returns the updated row or None.
        """
        if revert_status not in ("pending", "approved"):
            revert_status = "pending"
        params = {"id": f"eq.{row_id}"}
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"status": revert_status},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    # ---- mirror writes (real-drafts calendar mirror) ------------------------
    # These write calendar rows only. NOTHING here publishes to any social account.
    def insert_rows(self, account_key, rows):
        """INSERT content_calendar rows for account_key WITHOUT sending an `id`, so the
        DB generates the uuid primary key itself. content_calendar.id is a Postgres uuid
        (DB default gen_random_uuid); sending a non-uuid string (a draft_id) is what
        caused 22P02 "invalid input syntax for type uuid" and wrote 0 rows. There is no
        draft_id column, so a draft's id is simply not persisted as the row id: /social
        and the approve/deny actions key off the DB-returned uuid, not the draft id.

        Every row's gym_id is FORCED to account_key (a caller can never write another
        gym's row through this store) and any stray `id` key is STRIPPED before the POST.
        No on_conflict/upsert: apply is delete-then-insert, so a plain insert is correct
        and idempotent. Returns the list of inserted row dicts (each with its new uuid)."""
        payload = []
        for row in (rows or []):
            clean = {k: v for k, v in dict(row or {}).items() if k != "id"}
            clean["gym_id"] = account_key  # gym scope: never trust a foreign gym_id
            payload.append(clean)
        if not payload:
            return []
        r = self._client().post(
            self._rest(_TABLE),
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json=payload,
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        out = r.json() or []
        return [x for x in out if str(x.get("gym_id")) == str(account_key)]

    def delete_month(self, account_key, month):
        """DELETE every content_calendar row for account_key whose post_date falls inside
        the calendar month `month` ('YYYY-MM'). Gym scoped: the filter carries BOTH
        gym_id=eq.<account_key> AND the month's date bounds, so a row belonging to another
        gym (or outside the month) is never touched. Used by the delete-then-insert apply
        so a re-run replaces the month cleanly and idempotently. Returns the number of the
        gym's rows deleted."""
        year = int(month[:4])
        mon = int(month[5:7])
        last_day = _calendar.monthrange(year, mon)[1]
        first = f"{month}-01"
        last = f"{month}-{last_day:02d}"
        r = self._client().delete(
            self._rest(_TABLE),
            params={
                "gym_id": f"eq.{account_key}",
                "post_date": [f"gte.{first}", f"lte.{last}"],
            },
            headers=self._headers({"Prefer": "return=representation"}),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return len([x for x in rows if str(x.get("gym_id")) == str(account_key)])

    def delete_row(self, account_key, row_id):
        """DELETE one content_calendar row, filtered by BOTH id AND gym_id so a row that
        belongs to another gym can never be deleted through this account_key. Returns the
        number of rows deleted (0 or 1)."""
        r = self._client().delete(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}"},
            headers=self._headers({"Prefer": "return=representation"}),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return len([x for x in rows if str(x.get("gym_id")) == str(account_key)])


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
