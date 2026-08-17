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

# A row is WIPEABLE only while no human and no publisher has ever touched it: a fresh
# machine draft. Every other status is HUMAN OWNED and a calendar rebuild (the daily
# delete-then-insert) must NEVER destroy it. This is the fix for approvals not holding:
# a nightly re-plan used to delete the whole month (including a client's approved posts)
# and re-insert fresh 'pending' rows, silently reverting every approval. Now the rebuild
# leaves anything approved / denied / killed / published / publishing / failed in place.
_WIPEABLE_STATUSES = ("pending", "draft", "queued")


def _slot_key(row):
    """The (post_date, account, format) a row occupies, normalized. Two rows with the
    same slot key are the same calendar cell (a rebuild must not create a second one)."""
    return (
        str((row or {}).get("post_date") or "")[:10],
        str((row or {}).get("account") or "").lower(),
        str((row or {}).get("format") or "").lower(),
    )


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

    def has_owner_visible_rows(self, account_key):
        """GATE 2 (coach-screens-first-month): True if the gym has EVER had an owner-visible
        content_calendar row (any status EXCEPT 'coach_review', any account, any date). A
        gym with none is in its FIRST, not-yet-released month; a gym with any is established
        and grandfathered (never re-withheld on a rebuild)."""
        params = {"gym_id": f"eq.{account_key}", "status": "neq.coach_review",
                  "select": "id", "limit": "1"}
        r = self._client().get(self._rest(_TABLE), params=params,
                               headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return bool(r.json() or [])

    def release_coach_review(self, account_key):
        """GATE 2 coach release: flip ALL of this gym's withheld 'coach_review' rows (every
        account/platform) to 'pending' in one PATCH, so the owner can see and approve their
        first month after the coach walks them through it. Returns the released rows."""
        r = self._client().patch(
            self._rest(_TABLE),
            params={"gym_id": f"eq.{account_key}", "status": "eq.coach_review"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"status": "pending"}, timeout=30)
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

    def patch_gbp_fields(self, account_key, row_id, fields):
        """G1: persist edited GBP structured columns (already normalized to gbp_* names)
        and revert status to 'pending' — an edit to CTA/event/offer/location resets the
        approval exactly like a caption edit, so the owner re-approves what actually ships.
        id+gym_id isolation. Returns the updated row, or None when zero rows matched."""
        payload = {k: v for k, v in (fields or {}).items()}
        if not payload:
            return None
        payload["status"] = "pending"
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=payload, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    def requeue(self, account_key, row_id, *, new_status, new_caption=None):
        """G2 requeue: move a FAILED row back into the flow, CLEARING reject_reason.
        When new_caption is given (the coach changed the words) it is written and
        new_status should be 'pending' (owner re-approval); otherwise new_status is
        'approved' (straight back to the publish queue). id+gym_id isolation. Returns the
        updated row, or None when zero rows matched."""
        fields = {"status": new_status, "reject_reason": ""}
        if new_caption is not None:
            fields["caption"] = new_caption
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=fields, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    def patch_caption(self, account_key, row_id, new_caption):
        """PATCH the row's caption AND revert status to 'pending', filtered by BOTH id
        and gym_id. Editing a caption resets the approval so the owner re-approves the
        new wording — this prevents approving one text then silently posting another.
        Returns the updated row dict, or None when zero rows matched (treated as 404)."""
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
            json={"caption": new_caption, "status": "pending"},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        for row in rows:
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    # ---- auto-publisher: read + exactly-once claim/update -------------------
    # These serve the scheduled calendar auto-publisher (calendar_autopublish.py).
    # They never publish; they only read the day's rows and flip status atomically
    # so a row is published EXACTLY ONCE across re-runs / concurrent workers.
    def due_rows(self, gym_id, run_date, catchup_days=0):
        """
        content_calendar rows that are DUE to publish on `run_date` for `gym_id`:
          - gym_id == gym_id
          - post_date == run_date  (or, with catchup_days=N, any date in the last N
            days through run_date — never a future date)
          - status NOT in ('published','denied','killed')
          - published_at IS NULL   (never re-publish a row already sent)
          - image_url present      (a row with no creative is skipped upstream too)
        `run_date` is 'YYYY-MM-DD' (validated by the caller). Returns a list of dicts.
        Gym scoped by the gym_id=eq filter so another gym's row is never returned.

        catchup_days exists for the CLIENT lane: a gym owner who approves yesterday's
        post this morning used to strand it forever (post_date=eq.<today> could never
        see it again). With a small catch-up window the approved row is picked up and
        published immediately (approved_only still guards: a pending past row is never
        touched)."""
        if catchup_days and int(catchup_days) > 0:
            from datetime import date as _date, timedelta as _td
            start = (_date.fromisoformat(run_date)
                     - _td(days=int(catchup_days))).isoformat()
            post_date_filter = [f"gte.{start}", f"lte.{run_date}"]
        else:
            post_date_filter = f"eq.{run_date}"
        params = {
            "gym_id": f"eq.{gym_id}",
            "post_date": post_date_filter,
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

    def stamp_scheduled(self, row_id, scheduled_at_iso):
        """Record the row's planned go-live time (content_calendar.scheduled_at) so the
        portal can SHOW the client when the post publishes. Display metadata only:
        never touches status/published_at, never publishes. Idempotent by nature (the
        slot is deterministic per row). Returns the updated row or None."""
        if not scheduled_at_iso:
            return None
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"scheduled_at": scheduled_at_iso},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def clear_social_connection(self, gym_slug, platform):
        """Mark a gym's platform connection 'not_connected' (handle null) in the portal
        snapshot (echo_social_connections), keyed by the gym's Supabase uuid resolved
        from its slug. Used by the disconnect flow so the dashboard reflects the change
        immediately. No-op when the gym/row is absent. Returns the updated row or None."""
        g = self._client().get(
            self._rest("gyms"),
            params={"slug": f"eq.{gym_slug}", "select": "id"},
            headers=self._headers(), timeout=30,
        )
        if g.status_code >= 400:
            raise PortalStoreError(g.status_code, _scrub((g.text or "")[:200]))
        grows = g.json() or []
        if not grows or not grows[0].get("id"):
            return None
        gym_uuid = grows[0]["id"]
        r = self._client().patch(
            self._rest("echo_social_connections"),
            params={"gym_id": f"eq.{gym_uuid}", "platform": f"eq.{platform}"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"state": "not_connected", "handle": None},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def gym_autonomy(self, gym_slug):
        """The portal's per-gym Autonomous toggle for one gym, read from Supabase:
        gyms.slug -> echo_gym_settings.autonomous. Returns True/False, or None when
        the gym or its settings row is absent (caller treats None as NOT autonomous —
        approval required is always the safe default). Read-only, gym-scoped."""
        r = self._client().get(
            self._rest("gyms"),
            params={"slug": f"eq.{gym_slug}", "select": "id"},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not rows:
            return None
        gym_uuid = rows[0].get("id")
        if not gym_uuid:
            return None
        r2 = self._client().get(
            self._rest("echo_gym_settings"),
            params={"gym_id": f"eq.{gym_uuid}", "select": "autonomous"},
            headers=self._headers(),
            timeout=30,
        )
        if r2.status_code >= 400:
            raise PortalStoreError(r2.status_code, _scrub((r2.text or "")[:200]))
        srows = r2.json() or []
        if not srows:
            return None
        return bool(srows[0].get("autonomous"))

    def set_gym_autonomy(self, gym_slug, autonomous, actor=""):
        """UPSERT the portal's per-gym Autonomous toggle: gyms.slug ->
        echo_gym_settings.autonomous. This is the SHARED persistence plane the publish
        lane reads (gym_autonomy) — the local SQLite kv alone is ephemeral and invisible
        across services, so the toggle must land here to actually change publishing.
        Returns True on write, False when the gym slug is unknown (caller surfaces it)."""
        r = self._client().get(
            self._rest("gyms"),
            params={"slug": f"eq.{gym_slug}", "select": "id"},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        gym_uuid = (rows[0].get("id") if rows else None)
        if not gym_uuid:
            return False
        r2 = self._client().post(
            self._rest("echo_gym_settings"),
            params={"on_conflict": "gym_id"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }),
            json=[{"gym_id": gym_uuid, "autonomous": bool(autonomous),
                   "autonomy_updated_by": (actor or "")[:120]}],
            timeout=30,
        )
        if r2.status_code >= 400:
            raise PortalStoreError(r2.status_code, _scrub((r2.text or "")[:200]))
        return True

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
        and idempotent. Returns the list of inserted row dicts (each with its new uuid).

        KEY NORMALIZATION: PostgREST rejects a heterogeneous batch with PGRST102 "All
        object keys must match". Our rows are NOT uniform — a video row carries
        thumbnail_url, a photo row doesn't — so a mixed batch (any gym with both photo
        and video posts) used to 400 the ENTIRE insert and write 0 rows (GritX rebuild
        stuck at 1 day). We normalize every row to the UNION of keys across the batch,
        filling missing keys with None, so the batch is always uniform."""
        payload = []
        for row in (rows or []):
            clean = {k: v for k, v in dict(row or {}).items() if k != "id"}
            clean["gym_id"] = account_key  # gym scope: never trust a foreign gym_id
            payload.append(clean)
        if not payload:
            return []
        all_keys = set()
        for r in payload:
            all_keys.update(r.keys())
        payload = [{k: r.get(k) for k in all_keys} for r in payload]
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

    def delete_month(self, account_key, month, *, preserve_human=True,
                     preserve_dates=()):
        """DELETE content_calendar rows for account_key whose post_date falls inside the
        calendar month `month` ('YYYY-MM'). Gym scoped: the filter carries BOTH
        gym_id=eq.<account_key> AND the month's date bounds, so a row belonging to another
        gym (or outside the month) is never touched. Used by the delete-then-insert apply
        so a re-run replaces the month cleanly and idempotently. Returns the number of the
        gym's rows deleted.

        preserve_human (default True): only WIPEABLE rows (fresh machine drafts:
        pending / draft / queued / NULL status) are deleted. Any row a human or the
        publisher has touched (approved, denied, killed, published, publishing, failed)
        is LEFT IN PLACE, so a nightly rebuild can never revert a client's approval. Pass
        preserve_human=False only for a deliberate full wipe of a gym's month.

        preserve_dates: post_dates whose rows are NOT deleted at all (even wipeable
        ones). The client builder passes its LOCKED days here: a day whose feed the
        client approved keeps its still-pending siblings (the FB mirror + paired story
        built from the same photo/caption) — the builder skips planning locked days, so
        deleting their siblings would orphan the approved post's cross-post forever."""
        year = int(month[:4])
        mon = int(month[5:7])
        last_day = _calendar.monthrange(year, mon)[1]
        first = f"{month}-01"
        last = f"{month}-{last_day:02d}"
        post_date_filter = [f"gte.{first}", f"lte.{last}"]
        keep = sorted({str(d)[:10] for d in (preserve_dates or ()) if d})
        if keep:
            post_date_filter.append(f"not.in.({','.join(keep)})")
        params = {
            "gym_id": f"eq.{account_key}",
            "post_date": post_date_filter,
        }
        if preserve_human:
            # delete only the never-touched drafts: status IS NULL OR status IN wipeable.
            in_list = ",".join(_WIPEABLE_STATUSES)
            params["or"] = f"(status.is.null,status.in.({in_list}))"
        r = self._client().delete(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({"Prefer": "return=representation"}),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return len([x for x in rows if str(x.get("gym_id")) == str(account_key)])

    def locked_slots(self, account_key, month):
        """The set of (post_date, account, format) slots in `month` already occupied by a
        HUMAN OWNED row (any status not in wipeable). A rebuild must not insert a second
        row into one of these cells, or the client would see a duplicate next to the post
        they already approved. Returns a set of slot-key tuples (empty on a clean month)."""
        locked = set()
        for row in self.list_month(account_key, month) or []:
            status = str((row or {}).get("status") or "").lower()
            if status and status not in _WIPEABLE_STATUSES:
                locked.add(_slot_key(row))
        return locked

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


def preserve_and_prune(store, account_key, months, rows):
    """Shared guard for every delete-then-insert rebuild lane (client month, real month,
    demo->real mirror). Reads the HUMAN OWNED slots the gym already has across `months`
    and drops any incoming row that would land on one of them, so a rebuild that keeps a
    client's approved post never also inserts a duplicate draft into the same cell.

    Returns (kept_rows, locked_slot_count). Safe when the store lacks locked_slots (a test
    fake): then nothing is locked and every row is kept. Never raises out (a read failure
    falls back to keeping all rows, matching the old behavior)."""
    getter = getattr(store, "locked_slots", None)
    if getter is None:
        return list(rows or []), 0
    locked = set()
    for month in months:
        try:
            locked |= getter(account_key, month) or set()
        except Exception:  # noqa: BLE001 - a read failure must not block the rebuild
            return list(rows or []), 0
    if not locked:
        return list(rows or []), 0
    kept = [r for r in (rows or []) if _slot_key(r) not in locked]
    return kept, len(locked)


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
        "scheduled_for": row.get("scheduled_at"),
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
