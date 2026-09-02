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
import time as _time

from . import config

# base -> (gyms.id uuid, expires_at). POSITIVE resolutions only; see
# SupabaseCalendarStore.resolve_gym_uuid for why a miss is deliberately never cached.
# Six hours: a gym's uuid is effectively immutable, and re-slugging or archiving one is
# a rare, human-driven act, so this bounds staleness without paying the read every tick.
_UUID_CACHE = {}
_UUID_CACHE_TTL_SECONDS = 6 * 60 * 60

# Wave 3: caption cooldown ledger. Imported lazily inside insert_rows when
# AGENT_CAPTION_COOLDOWN is ON so the flag-off path has zero cost.

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
    same slot key are the same calendar cell (a rebuild must not create a second one).

    DELIBERATELY NOT slot-aware. Adding slot_index here would let a 2x day keep its PM
    row when the AM row is human-owned — but it also makes a 2x->1x rebuild (which
    stamps NO slot_index) stop matching an approved slot-1 row, so the planner lanes
    that rely on preserve_and_prune as their ONLY collision guard (real_month_planner,
    real_calendar_mirror — LASSO's own, with auto-approve armed) would insert a fresh
    row beside an approved one and publish it. The client lane never needs the slot
    dimension: it skips locked DATES wholesale (client_month_run covered_days). Revisit
    only together with the locked-day question in PROGRESS.md."""
    return (
        str((row or {}).get("post_date") or "")[:10],
        str((row or {}).get("account") or "").lower(),
        str((row or {}).get("format") or "").lower(),
    )


# A canonical account_key is "<name-slug><6 hex chars of sha256(gym_id)>" (account_key.py),
# optionally followed by a collision disambiguator (2, 3, ...) and optionally by an _ig/_fb
# lane suffix, which normalisation strips to a bare "ig"/"fb". That exact shape is the ONLY
# thing allowed to sit between a gym's slug and its base key in the reverse direction below.
_CANONICAL_TAIL = __import__("re").compile(r"^[0-9a-f]{6}[0-9]*(ig|fb)?$")


def _slug_boundary_prefixes(slug_norm, slug_raw):
    """Every normalised prefix of `slug_raw` that ends on a WORD boundary, e.g.
    'district-h-strength-fitness' -> {'district', 'districth', 'districthstrength',
    'districthstrengthfitness'}. A base may only match a slug at one of these, so
    'eng' can never be a "prefix" of 'engage-fitness-denver': 'eng' is mid-word."""
    out = set()
    acc = ""
    for token in str(slug_raw or "").replace("_", "-").split("-"):
        token_norm = "".join(c for c in token.lower() if c.isalnum())
        if not token_norm:
            continue
        acc += token_norm
        out.add(acc)
    if slug_norm:
        out.add(slug_norm)
    return out


def _containment_match(target, slug_norm, slug_raw="", name_raw=""):
    """True iff normalised base `target` may be treated as the same gym as the row with
    slug `slug_raw` / name `name_raw`. Deliberately narrow — see the cross-tenant note
    in resolve_gym_uuid. Both the slug AND the display name are consulted, because a
    registry string is often built from the NAME, not the slug ('hillcountrymvmt' for
    the gym slugged 'hill-country' and named 'Hill Country MVMT').

    FORWARD ('district_h' -> 'district-h-strength-fitness'): the base must equal a
    prefix of the slug or the name that ends on a WORD boundary. Requiring the boundary
    is what stops a short identifier swallowing an unrelated longer gym mid-word: with
    a gym slugged 'eng', a bare startswith resolved 'engagefitnessdenver', 'england' and
    'engine' onto it, and a one-letter slug swallowed the entire fleet.

    REVERSE ('swiftrivercrossfitd23567' -> 'swift-river-crossfit'): the base must be the
    slug or name PLUS a canonical-key tail and nothing else. That is the only reason the
    reverse direction exists at all — every canonically minted key is its name-slug plus
    a fingerprint — so pinning the tail shape keeps it while killing the false hits."""
    if not target:
        return False
    forms = []
    for raw, norm in ((slug_raw or slug_norm, slug_norm),
                      (name_raw, "".join(c for c in (name_raw or "").lower()
                                         if c.isalnum()))):
        if norm:
            forms.append((raw, norm))
    for raw, norm in forms:
        if target == norm:
            return True
        # Forward: a word-boundary prefix only.
        if norm.startswith(target) and target in _slug_boundary_prefixes(norm, raw):
            return True
        # Reverse: the canonical <name-slug><fingerprint> shape only.
        if target.startswith(norm) and _CANONICAL_TAIL.match(target[len(norm):]):
            return True
    return False


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

        SERVER-SIDE RACE GUARD (audit 2026-08-25 MAJOR): also filtered by
        status NOT IN (publishing, published) — every caller is a portal action
        (approve/deny/kill/coach-release), and none may overwrite a row the publisher
        has claimed (seconds-wide window) or already published. The handlers 409 these
        states from their own pre-read; this makes the WRITE itself refuse when the
        state changed between that read and this patch (deny-during-publish used to be
        silently swallowed by the later mark_published, or worse, re-arm a claim).
        """
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": "not.in.(publishing,published)",
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

    def patch_image_url(self, account_key, row_id, new_image_url):
        """Task #28 (§5c): swap a story row's image_url to freshly re-burned media after a
        caption edit. STATUS-preserving (the edit already reset it to 'pending'); this only
        updates the media. id+gym_id isolation. Returns the updated row or None."""
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"image_url": new_image_url}, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    def patch_media(self, account_key, row_id, image_url, source_media_asset_id=""):
        """Backfill a row's image_url (+ source_media_asset_id) that was staged with NO
        image, WITHOUT touching status or caption (Pete/CrossFit Zanshin, 2026-08-31: an
        event arc row inserted before event_calendar's media-attach guard existed sat
        forever with image_url=''). A prefetch (get_row) confirms the row STILL has no
        image before this ever writes, so a row a human or a later pass already gave a
        real photo is never clobbered — the same never-cross-date, never-overwrite
        discipline as gym_media_selector's rollback path. id+gym_id isolation like every
        other write here. Returns the updated row, or None when the row does not exist,
        belongs to another gym, or already carries an image (nothing to backfill)."""
        current = self.get_row(account_key, row_id)
        if current is None:
            return None
        if (current.get("image_url") or "").strip():
            return None  # already has a real image; never overwrite
        payload = {"image_url": image_url}
        if source_media_asset_id:
            payload["source_media_asset_id"] = source_media_asset_id
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

    def swap_media(self, account_key, row_id, image_url, source_media_url=None):
        """CROSS-DAY MEDIA GUARD sweep (Blake, 2026-08-31): re-point a WAITING row's
        media to a fresh photo because its current photo already sits on another day
        of the gym's book. STATUS-GUARDED SERVER-SIDE: the PATCH itself is filtered to
        status in (pending, coach_review), so an approved / publishing / published row
        can NEVER be swapped through this method — the gym's approval and anything
        live keep exactly the pixels they had. Caption, status and date are untouched.
        source_media_url (when given) is updated too, so a later edited-caption story
        re-burn burns onto the NEW photo, not the replaced duplicate. id+gym_id
        isolation. Returns the updated row, or None when nothing matched."""
        payload = {"image_url": image_url}
        if source_media_url is not None:
            payload["source_media_url"] = source_media_url
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}",
                    "status": "in.(pending,coach_review)"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=payload, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
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
        Returns the updated row dict, or None when zero rows matched (treated as 404).

        SERVER-SIDE RACE GUARD (audit 2026-08-25 MAJOR): status NOT IN (publishing,
        published) — an edit landing in the seconds the publisher owns the row would
        reset it to 'pending', making it claimable AGAIN next tick (the same creative
        publishes twice, with different words so Zernio's dedup cannot save it). The
        handler 409s from its pre-read; this makes the write itself refuse the race."""
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": "not.in.(publishing,published)",
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

    def patch_caption_preserve_status(self, account_key, row_id, new_caption):
        """PATCH the row's caption WITHOUT touching status — the hygiene twin of
        patch_caption, mirroring patch_image_url's status-preserving style. Exists for
        Echo's OWN cleanup writes (stripping an internal edit-rationale block off a
        caption, the CrossFit ENG '[why]' leak of 2026-08-23): the human approved the
        real caption body, so cleaning scaffolding off it must not un-approve the row
        the way patch_caption's pending reset would (that reset exists for HUMAN edits,
        where re-approval is the point). Same id+gym_id isolation and the same
        server-side race guard as patch_caption (never touches a row mid-publish or
        already published). Returns the updated row dict, or None when zero rows
        matched."""
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": "not.in.(publishing,published)",
        }
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"caption": new_caption},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
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
          - account IN (instagram, facebook)  (this is the IG/FB lane ONLY; a
            googlebusiness row is published by the SEPARATE GBP worker and must never
            enter this lane — without this filter the IG/FB lane grabbed a GBP row and
            posted its Google caption to Instagram, Dale/ENG 2026-08-22)
        `run_date` is 'YYYY-MM-DD' (validated by the caller). Returns a list of dicts.
        Gym scoped by the gym_id=eq filter so another gym's row is never returned.

        catchup_days exists for the CLIENT lane: a gym owner who approves yesterday's
        post this morning used to strand it forever (post_date=eq.<today> could never
        see it again). With a small catch-up window the approved row is picked up and
        published immediately (approved_only still guards: a pending past row is never
        touched).

        ORDER (2026-08-30 fix): TODAY's rows (post_date DESC puts run_date, the max
        value the catch-up window ever contains, first) are served before older
        catch-up backlog, tie-broken by created_at (STAGE time) for determinism within
        a day. Without this, a whole month is staged in ONE insert_rows() batch, so an
        OLD approved backlog row (staged in an earlier build, weeks ago) sorted AHEAD
        of this month's freshly staged same-day cadence rows under a plain
        `order=created_at` -- and publish_due's per-day cap (AGENT_CLIENT_DAILY_PUBLISH_CAP)
        processes rows in due_rows() order, spending its whole budget on backlog before
        ever reaching today's rows (a client gym's normal cadence looked "stopped" while
        old backlog silently ate the cap). This only changes SERVE ORDER, never which
        rows are eligible; with catchup_days=0 every returned row already shares the
        same post_date so the ordering is unchanged from before."""
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
            "account": "in.(instagram,facebook)",
            "order": "post_date.desc,created_at",
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

    def patch_post_date(self, row_id, new_post_date):
        """RE-DATE one waiting row (expired-row self-heal, Blake 2026-08-31: no human
        should have to re-date dead posts). Moves post_date forward and CLEARS
        scheduled_at so the publish lane re-stamps the new slot time. Race-guarded:
        only a row still waiting (pending/approved, never published) may move — a row
        mid-claim or already live is refused (zero rows -> None). Status untouched, so
        an approved row stays approved (the gym's approval is preserved)."""
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}",
                    "status": "in.(pending,approved)",
                    "published_at": "is.null"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"post_date": new_post_date, "scheduled_at": None},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

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
        base -> gyms.id -> echo_gym_settings.autonomous. Returns True/False, or None
        when the gym or its settings row is absent (caller treats None as NOT
        autonomous — approval required is always the safe default). Read-only.

        Resolves through resolve_gym_uuid for the same base != slug reason as the
        cadence pair: the worker asks with an account-registry BASE (piercefitness,
        topfuel) while gyms.slug is hyphenated (pierce-fitness, top-fuel), so the old
        exact-slug match returned None for those gyms and their portal Autonomous
        toggle was silently a no-op in BOTH directions. Verified before changing this:
        lasso-framework-llc is the ONLY gym with autonomous=true, and it already reads
        autonomous from the worker's local kv (which is consulted first), so no gym's
        effective autonomy changes here. A gym that is False still reads False."""
        gym_uuid = self.resolve_gym_uuid(gym_slug)
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
        """UPSERT the portal's per-gym Autonomous toggle: base -> gyms.id ->
        echo_gym_settings.autonomous. This is the SHARED persistence plane the publish
        lane reads (gym_autonomy) — the local SQLite kv alone is ephemeral and invisible
        across services, so the toggle must land here to actually change publishing.
        Returns True on write, False when the gym is unknown (caller surfaces it).

        Resolves through resolve_gym_uuid for the same base != slug reason as the
        reader above: without it every gym whose base differs from its slug got a False
        here, so its Autonomous toggle could not be saved at all — in either direction,
        including turning autonomy OFF."""
        gym_uuid = self.resolve_gym_uuid(gym_slug)
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

    def gym_posts_per_day(self, gym_slug):
        """The portal's per-gym posting cadence for one gym, read from Supabase:
        base -> gyms.id -> echo_gym_settings.posts_per_day. Returns 1 or 2, or None
        when the gym or its settings row is absent / carries no valid value (caller
        treats None as 1 — today's cadence is always the safe default). Read-only,
        gym-scoped.

        Resolves through resolve_gym_uuid, NOT a raw `slug=eq.<base>` match: the
        worker asks with an account-registry BASE (piercefitness, topfuel) while
        gyms.slug is a hyphenated human slug (pierce-fitness, top-fuel). The old
        exact-slug lookup silently returned None for every gym whose base differs
        from its slug, so their owners' 2x toggle saved to the shared plane and the
        worker never saw it (LASSO and Pierce were both sitting at 2 and building
        at 1)."""
        gym_uuid = self.resolve_gym_uuid(gym_slug)
        if not gym_uuid:
            return None
        r2 = self._client().get(
            self._rest("echo_gym_settings"),
            params={"gym_id": f"eq.{gym_uuid}", "select": "posts_per_day"},
            headers=self._headers(),
            timeout=30,
        )
        if r2.status_code >= 400:
            raise PortalStoreError(r2.status_code, _scrub((r2.text or "")[:200]))
        srows = r2.json() or []
        if not srows:
            return None
        val = srows[0].get("posts_per_day")
        return int(val) if val in (1, 2) else None

    def set_gym_posts_per_day(self, gym_slug, posts_per_day, actor=""):
        """UPSERT the portal's per-gym posting cadence: base -> gyms.id ->
        echo_gym_settings.posts_per_day. This is the SHARED persistence plane the
        worker's planners read (gym_posts_per_day) — the intake-web kv alone is a
        different service's SQLite and invisible to the worker. Only 1 or 2 is a
        valid cadence (refused otherwise: returns False, writes nothing). Returns
        True on write, False when the gym is unknown.

        Resolves through resolve_gym_uuid for the same base != slug reason as the
        reader above: without it, every gym whose base differs from its slug got a
        False here, and portal_social.handle_cadence turns that into a 503 — so
        those owners could not save a cadence at all."""
        try:
            posts_per_day = int(posts_per_day)
        except (TypeError, ValueError):
            return False
        if posts_per_day not in (1, 2):
            return False
        gym_uuid = self.resolve_gym_uuid(gym_slug)
        if not gym_uuid:
            return False
        payload = {"gym_id": gym_uuid, "posts_per_day": posts_per_day}
        # Only stamp the audit column when we actually know the actor: an empty
        # actor must not clobber the portal's own cadence_updated_by (the portal
        # writes first with the user's role; Echo's mirror write follows).
        if (actor or "").strip():
            payload["cadence_updated_by"] = actor.strip()[:120]
        r2 = self._client().post(
            self._rest("echo_gym_settings"),
            params={"on_conflict": "gym_id"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }),
            json=[payload],
            timeout=30,
        )
        if r2.status_code >= 400:
            raise PortalStoreError(r2.status_code, _scrub((r2.text or "")[:200]))
        return True

    def available(self):
        """True iff Supabase creds are present (URL + service key), so both the
        status resolver and the profile-id writer can go DURABLE-OR-SKIP: no creds
        means fall back to the local db / find-by-name, never raise. Mirrors the
        creds check config.portal_calendar_supabase_enabled uses."""
        return bool(self._url) and bool(self._key)

    # ---- Zernio profile binding (the SHARED plane the STATUS path reads) ---------
    # THE CONNECTION-STATUS BUG (gritx/hillcountry, 2026-08-28): status runs on the
    # echo-intake-web service, which has NO /data volume — its SQLite echo.db is empty
    # on every deploy, so _resolve_profile_id (db-only) returned None and status came
    # back not_connected for every platform even though Zernio held the live account.
    # These two methods persist the authoritative gym -> zernio_profile_id (+ the chosen
    # FB page) to echo_gym_settings, which BOTH services read. Written at connect/finalize
    # time; read first in _resolve_profile_id, so status no longer depends on the ephemeral
    # web volume. Mirrors gym_autonomy exactly (gyms.slug -> id -> echo_gym_settings).

    def list_gyms_min(self):
        """Read-only: the minimal gyms list (id, slug, name) for every gym. Used by the
        account-key doctor to CLASSIFY an unresolved base (AMBIGUOUS vs ARCHIVED_ONLY vs a
        true no-match) without ever binding or writing. Returns [] on no creds / a read
        failure (an honest 'no data', never a crash). Never raises out."""
        try:
            r = self._client().get(
                self._rest("gyms"),
                params={"select": "id,slug,name"},
                headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                return []
            return r.json() or []
        except Exception:  # noqa: BLE001 - a read failure is 'no data', never a crash
            return []

    def resolve_gym_uuid(self, base):
        """CACHED. See _resolve_gym_uuid_uncached for the resolution rules.

        WHY A CACHE (scale audit, 2026-08-30): this is the hottest read in the publish
        path. publish_client_gyms asks gym_autonomy for EVERY gym on the listener's
        ~1 minute tick, and gym_autonomy resolves the base first. For any gym whose
        base is not identical to its hyphenated slug (topfuel, district_h, hillcountry
        and friends, i.e. the common case, not the exception) that costs three gyms
        reads, the third of which pulls the ENTIRE gyms table with no filter, plus the
        settings read. At 100 gyms that is roughly 24,000 Supabase calls an hour for a
        value that essentially never changes, issued SERIALLY inside one tick, which
        can push a tick past its own cadence and back the queue up.

        ONLY SUCCESSFUL resolutions are cached. A None is never cached, so a gym that
        registers a moment from now resolves on its very next tick rather than waiting
        out a TTL: caching the miss would reintroduce the stranding this resolver was
        written to kill. Process-local and cold after a restart."""
        key = (str(base) if base is not None else "").strip()
        if not key:
            return None
        hit = _UUID_CACHE.get(key)
        if hit is not None:
            uuid, expires = hit
            if expires > _time.time():
                return uuid
            _UUID_CACHE.pop(key, None)
        uuid = self._resolve_gym_uuid_uncached(key)
        if uuid:
            _UUID_CACHE[key] = (uuid, _time.time() + _UUID_CACHE_TTL_SECONDS)
        return uuid

    def _resolve_gym_uuid_uncached(self, base):
        """The gyms.id UUID for an account-registry BASE, or None. THE base != slug bug
        (topfuel/district_h/hillcountry, live 2026-08-28): the account registry keys by a
        base STRING (topfuel), but gyms.slug is a hyphenated human slug (top-fuel), so the
        old `gyms?slug=eq.<base>` string-identity lookup silently missed and every
        base->settings/snapshot op no-op'd. This is the ONE canonical base->uuid resolver
        every shared-plane path (status resolve, profile-id writer, reverify) shares.

        Match order (first hit wins):
          1. exact slug == base (eng/gritx, where base == slug by luck);
          2. NORMALISED slug == normalised base (strip hyphens/underscores, lowercase):
             'topfuel' matches 'top-fuel', 'district_h' matches 'district-h', 'hillcountry'
             matches 'hill-country';
          3. exact id == base (a base that is already a UUID).
        ARCHIVED / DUP rows are NEVER returned: any gym whose slug ends '-archived-dup' or
        whose name notes 'archived'/'do not use' is skipped, so a bind can never land on the
        district-h-archived-dup ghost. Returns None (never guesses) when nothing clean matches.
        Read-only. Never raises out (a lookup failure is an honest None)."""
        base = (str(base) if base is not None else "").strip()
        if not base:
            return None

        def _norm(s):
            return "".join(c for c in (s or "").lower() if c.isalnum())

        def _is_archived(row):
            slug = (row.get("slug") or "").lower()
            name = (row.get("name") or "").lower()
            return ("archived" in slug or "-dup" in slug or "archived" in name
                    or "do not use" in name)

        try:
            # exact slug, then exact id — cheap direct hits.
            for column in ("slug", "id"):
                r = self._client().get(
                    self._rest("gyms"),
                    params={column: f"eq.{base}", "select": "id,slug,name"},
                    headers=self._headers(), timeout=30)
                if r.status_code >= 400:
                    continue
                rows = [x for x in (r.json() or []) if not _is_archived(x)]
                if rows and rows[0].get("id"):
                    return rows[0]["id"]
            # normalised slug match: pull the (small) gyms list and compare normalised.
            r = self._client().get(
                self._rest("gyms"),
                params={"select": "id,slug,name"},
                headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                return None
            target = _norm(base)
            clean = [x for x in (r.json() or []) if not _is_archived(x) and x.get("id")]
            # tier 2a: EXACT normalised slug match ('topfuel' == norm('top-fuel')).
            exact = [x for x in clean if _norm(x.get("slug")) == target]
            if len(exact) == 1:
                return exact[0]["id"]
            if len(exact) > 1:
                return None  # ambiguous -> refuse to guess
            # tier 2b: PREFIX/containment ('district_h' -> 'district-h-strength-fitness',
            # 'birddog' -> 'bird-dog-crossfit', 'swiftrivercrossfitd23567' ->
            # 'swift-river-crossfit'). Only when EXACTLY ONE clean gym matches. A unique
            # containment is a confident map; anything else is left None (never a guess).
            #
            # WHY THIS IS NARROW NOW: a bare `startswith` in BOTH directions silently
            # resolved unrelated gyms onto each other. Measured against the live fleet:
            # with a gym slugged 'eng', the bases 'engagefitnessdenver', 'england' and
            # 'engine' ALL resolved to ENG's uuid, and a single-letter slug swallowed
            # everything. That is a cross-tenant resolver — the wrong gym's Zernio
            # profile, settings, GBP connection and calendar, with no error and no alert.
            # Each direction is now allowed only in the shape it actually exists to serve.
            if target:
                contain = [x for x in clean
                           if _containment_match(target, _norm(x.get("slug")),
                                                 x.get("slug") or "", x.get("name") or "")]
                if len(contain) == 1:
                    return contain[0]["id"]
            return None
        except Exception:  # noqa: BLE001 - a resolver failure is an honest None, never a crash
            return None

    def gym_zernio_profile_id(self, gym_slug):
        """The gym's stored zernio_profile_id from the shared plane, or None when the
        gym or its settings row is absent / carries no id. Read-only, gym-scoped.
        Resolves the base->uuid via resolve_gym_uuid so base != slug gyms are found."""
        gym_uuid = self.resolve_gym_uuid(gym_slug)
        if not gym_uuid:
            return None
        r2 = self._client().get(
            self._rest("echo_gym_settings"),
            params={"gym_id": f"eq.{gym_uuid}", "select": "zernio_profile_id"},
            headers=self._headers(),
            timeout=30,
        )
        if r2.status_code >= 400:
            raise PortalStoreError(r2.status_code, _scrub((r2.text or "")[:200]))
        srows = r2.json() or []
        if not srows:
            return None
        pid = srows[0].get("zernio_profile_id")
        return str(pid) if pid else None

    def set_gym_zernio_profile_id(self, gym_slug, zernio_profile_id,
                                  zernio_default_fb_page_id=None):
        """UPSERT the gym's authoritative zernio_profile_id (and optionally its chosen
        default FB page) into the shared echo_gym_settings row. This is the persistence
        both services read, so the status path resolves the profile even on the volume-
        less web service. Never writes an empty profile id (a no-op guard). The FB page
        is only stamped when a non-empty value is supplied (an omitted page must not blank
        a stored one). Returns True on write, False when the gym slug is unknown / the
        profile id is empty. Mirrors set_gym_autonomy."""
        pid = str(zernio_profile_id or "").strip()
        if not pid:
            return False
        gym_uuid = self.resolve_gym_uuid(gym_slug)
        if not gym_uuid:
            return False
        payload = {"gym_id": gym_uuid, "zernio_profile_id": pid}
        page = str(zernio_default_fb_page_id or "").strip()
        if page:
            payload["zernio_default_fb_page_id"] = page
        r2 = self._client().post(
            self._rest("echo_gym_settings"),
            params={"on_conflict": "gym_id"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }),
            json=[payload],
            timeout=30,
        )
        if r2.status_code >= 400:
            raise PortalStoreError(r2.status_code, _scrub((r2.text or "")[:200]))
        return True

    def first_calendar_date(self, account_key, status=None):
        """READ-ONLY (social before/after): the gym's earliest content_calendar
        post_date, optionally filtered to one status ('published' finds the true
        Echo start; no filter finds the first planned row as the honest
        fallback). Returns 'YYYY-MM-DD' or None — never guessed. Gym-scoped by
        the gym_id filter."""
        params = {
            "gym_id": f"eq.{account_key}",
            "select": "post_date",
            "order": "post_date.asc",
            "limit": "1",
        }
        if status:
            params["status"] = f"eq.{status}"
        r = self._client().get(
            self._rest(_TABLE), params=params, headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not rows:
            return None
        d = str((rows[0] or {}).get("post_date") or "")[:10]
        return d or None

    def social_connection_handle(self, gym_slug, platform="instagram"):
        """READ-ONLY: the gym's stamped handle from echo_social_connections for
        one platform — the live truth the reverify sweep writes. Resolves
        base->uuid via resolve_gym_uuid (base != slug gyms included). Returns
        the handle string or None; NEVER guessed. Gym-scoped."""
        gym_uuid = self.resolve_gym_uuid(gym_slug)
        if not gym_uuid:
            return None
        r = self._client().get(
            self._rest("echo_social_connections"),
            params={"gym_id": f"eq.{gym_uuid}", "platform": f"eq.{platform}",
                    "select": "handle", "limit": "1"},
            headers=self._headers(), timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not rows:
            return None
        h = str((rows[0] or {}).get("handle") or "").strip()
        return h or None

    def rewrite_social_connection(self, gym_slug, platform, state, handle=None,
                                  mark_ever_connected=False):
        """RE-VERIFY SWEEP writer: set echo_social_connections.state (+ handle) for a
        gym's platform to the TRUE Zernio state, overwriting the poisoned not_connected
        the 6h cron wrote, and bump last_verified_at. When a platform is genuinely
        connected now (mark_ever_connected), ensure first_connected_at is set — that is
        the durable "was connected" signal this schema actually carries (there is NO
        ever_connected column; writing one 400s). first_connected_at is stamped ONLY when
        currently null, so the ORIGINAL connect time is never overwritten. Resolves
        base->uuid via resolve_gym_uuid so base != slug gyms (topfuel, district_h,
        hillcountry) resolve instead of silently missing on the string-identity lookup.
        No-op (returns None) when the gym is unknown. Gym-scoped."""
        from datetime import datetime, timezone
        gym_uuid = self.resolve_gym_uuid(gym_slug)
        if not gym_uuid:
            return None
        now_iso = datetime.now(timezone.utc).isoformat()
        # Read the current row (if any) so we (a) preserve the ORIGINAL first_connected_at
        # and (b) know whether a row exists at all. A PATCH-only writer silently no-oped for
        # gyms that were never seeded a connection row (topfuel), leaving a genuinely
        # connected gym reading not_connected — so this is an UPSERT keyed on the table's
        # UNIQUE (gym_id, platform).
        cur = self._client().get(
            self._rest("echo_social_connections"),
            params={"gym_id": f"eq.{gym_uuid}", "platform": f"eq.{platform}",
                    "select": "first_connected_at"},
            headers=self._headers(), timeout=30,
        )
        if cur.status_code >= 400:
            raise PortalStoreError(cur.status_code, _scrub((cur.text or "")[:200]))
        crows = cur.json() or []
        had_first = bool(crows and (crows[0] or {}).get("first_connected_at"))
        body = {"gym_id": gym_uuid, "platform": platform, "state": state,
                "handle": handle, "last_verified_at": now_iso}
        # Stamp first_connected_at only for a genuinely-connected platform that has none yet;
        # never overwrite an existing original connect time (omitted -> merge-duplicates
        # leaves it untouched).
        if mark_ever_connected and not had_first:
            body["first_connected_at"] = now_iso
        r = self._client().post(
            self._rest("echo_social_connections"),
            params={"on_conflict": "gym_id,platform"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }),
            json=body,
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

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

    def delete_rows(self, account_key, row_ids):
        """Hard-delete specific rows for ONE gym. Filtered by BOTH id AND gym_id so a
        row belonging to another gym can never be removed. Returns the number deleted.

        Used by the onboarding-sample clear. Note the normal rebuild already removes
        samples for free (they are status='draft', which is wipeable, so delete_month
        takes them), so this is the explicit/ops path, not the main one."""
        ids = [str(i) for i in (row_ids or []) if i]
        if not account_key or not ids:
            return 0
        r = self._client().delete(
            self._rest(_TABLE),
            params={"gym_id": f"eq.{account_key}",
                    "id": f"in.({','.join(ids)})"},
            headers=self._headers({"Prefer": "return=representation"}),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        try:
            return len(r.json() or [])
        except Exception:  # noqa: BLE001 - a 2xx with an unreadable body still deleted
            return len(ids)

    def expired_rows(self, before_date, statuses=("approved", "pending")):
        """Rows whose post_date is BEFORE `before_date` and that are still waiting to
        publish — i.e. already outside the catch-up window, so due_rows will never
        return them again and they can never go out.

        Read-only; feeds the expired-row ALERT sweep. This is the state that ate 11
        approved LASSO posts and 26 GritX posts silently: nothing read, nothing
        claimed, nothing logged, no reject_reason — they simply stopped existing as
        far as the publisher was concerned."""
        params = {
            "post_date": f"lt.{before_date}",
            "status": f"in.({','.join(statuses)})",
            "published_at": "is.null",
            # GOOGLE BUSINESS IS EXCLUDED. GBP rows publish through their own lane
            # (gbp_store.approved_gbp_rows), which has NO age cutoff at all — an aged
            # approved GBP row is still perfectly publishable. due_rows excludes them
            # for the same reason, so counting them here would fire false "can never
            # publish" alerts on healthy rows.
            "account": "neq.googlebusiness",
            "select": "id,gym_id,account,post_date,status",
            "order": "post_date.asc",
        }
        r = self._client().get(
            self._rest(_TABLE), params=params, headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def mark_published(self, row_id, media_id, published_at,
                       allow_missing_post_id=False):
        """
        Record a successful publish: status='published', published_at=<now iso>,
        late_post_id=<media_id>. Filtered by id AND status='publishing' (audit
        2026-08-25 MAJOR): only the row THIS worker claimed may be stamped. If an
        out-of-band write somehow changed the status mid-flight (a denied/killed row
        must never be flipped to published), zero rows match and we RAISE so the
        caller reports it loudly (the post may be live; a human reconciles) instead
        of silently overwriting the client's decision. Returns the updated row.

        NO POST ID = NOT PUBLISHED (published-but-not-posted, the recurring class).
        This is the ONE write every publish lane funnels through, so the rule lives
        here and no lane can route around it: a blank late_post_id means nothing can
        ever verify, reconcile or link the post, and the portal shows "Published" for
        something that may not exist. Refused by default.

        allow_missing_post_id is the single documented exception: a Zernio 409
        content-hash dedup, where Zernio itself told us this exact content is already
        on the account but named no id. Callers pass it ONLY from that branch (see
        zernio_publisher.PublishResult.dedup). It is never a general escape hatch.
        """
        if not str(media_id or "").strip() and not allow_missing_post_id:
            raise PortalStoreError(
                422, f"row {row_id}: refusing to mark published with no platform post "
                     "id. A post we cannot identify cannot be verified or reconciled; "
                     "the row stays claimed and the caller reverts it for retry.")
        params = {"id": f"eq.{row_id}", "status": "eq.publishing"}
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
        if not rows:
            # The claimed row is no longer 'publishing' — an out-of-band write raced us.
            # Raise (never silently overwrite a deny/kill): the caller's except branch
            # reports "published but the mark_published write failed" loudly, and a human
            # reconciles the live post against the client's decision.
            raise PortalStoreError(
                409, f"row {row_id} is not in 'publishing'; refusing to stamp published "
                     "over an out-of-band status change")
        # Wave 3 (AGENT_CAPTION_COOLDOWN): stamp the published caption in the
        # ledger so future cooldown checks include this post. Read straight off
        # the returned representation row; failure is NEVER fatal (the publish
        # already landed; the ledger is a best-effort cache).
        if config.caption_cooldown_enabled():
            try:
                from . import caption_ledger as _ledger
                _row = rows[0]
                _gid = str(_row.get("gym_id") or "")
                _cap = _row.get("caption") or ""
                _date = str(_row.get("post_date") or (published_at or "")[:10])
                if _gid and _cap and _date:
                    _ledger.record_published(_gid, _cap, _date)
            except Exception:
                pass  # ledger stamp failure is never fatal
        return rows[0]

    def mark_publish_failed(self, row_id, revert_status="pending",
                            reject_reason=None):
        """
        REVERT a claim after a publish failure (or a would_publish result): status
        back to `revert_status` so the row is retried on the next run. LASSO rows
        revert to 'pending' (the default, unchanged). A CLIENT row that was APPROVED
        before the claim reverts to 'approved' so a transient Zernio failure never
        forces the client to re-approve. Records NOTHING else (no media id, no
        published_at), so a failed attempt never looks published. Filtered by id
        only. Returns the updated row or None.

        reject_reason (publish_guard wiring, 2026-08-27): when the publish guard
        blocks a row, its violation codes land on the row so the portal/human can
        see WHY it went back to pending. None (the default) leaves the column
        untouched — a transient network failure never overwrites a guard reason.
        """
        if revert_status not in ("pending", "approved"):
            revert_status = "pending"
        body = {"status": revert_status}
        if reject_reason is not None:
            body["reject_reason"] = str(reject_reason)[:500]
        params = {"id": f"eq.{row_id}"}
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json=body,
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
        # STAGE-TIME BELTS (report-card build, 2026-08-28; both flags default OFF,
        # account-agnostic — LASSO and gyms share the bug class):
        #   1. AGENT_EMPTY_CAPTION_GUARD: a FEED row with zero visible characters
        #      in its caption is never staged.
        #   2. AGENT_CAPTION_COOLDOWN: a FEED row whose caption is a VERBATIM
        #      duplicate (180-day rule, caption_ledger.is_verbatim_blocked) of a
        #      previously staged/published caption for this gym — or of an
        #      earlier row in this same batch on a DIFFERENT date — is never
        #      staged.
        # A blocked row is DROPPED from the batch with a loud, honest alert
        # (never silently shipped); the slot refills on the next plan pass (the
        # planner re-drafts under the same rule, so cadence never gaps). STORY
        # rows are exempt from both (empty body / shared caption by design).
        # HARD PLANNING HORIZON BELT (Blake, 2026-08-28; default ON — it PREVENTS
        # spend): no lane may STAGE a row more than one month past today, because
        # Echo's monthly relearn rebuilds it before it ever posts (pure token waste).
        # Runs even when a caller forgot the span clamp. Dated real-world rows are
        # EXEMPT (event_id set, or the LASSO summit/book/welcome dated lanes — narrow
        # by design). Dropped rows get ONE summary log/alert line per batch (digest
        # pattern), never per-row spam, never silent. Existing rows are untouched:
        # this filters the incoming batch only, it never deletes anything.
        # AGENT_PLAN_HORIZON_DAYS=0 disables (emergency escape hatch).
        from .plan_horizon import belt_filter as _horizon_belt
        payload, _ = _horizon_belt(account_key, payload)
        payload = _stage_belts(account_key, payload)
        # CROSS-DAY MEDIA BELT (fleet audit, 2026-08-31; flag AGENT_MEDIA_CROSS_DAY_GUARD,
        # the SAME flag media_guard already ships armed on). agent/media_guard.py calls
        # itself "the shared cross-day media guard for every photo-assigning lane" and was
        # wired into exactly TWO of them. This door is the one every staging lane walks
        # through, so the rule lives here too. See _media_stage_belt.
        payload = _media_stage_belt(self, account_key, payload)
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
        inserted = [x for x in out if str(x.get("gym_id")) == str(account_key)]
        # Wave 3 (AGENT_CAPTION_COOLDOWN): stamp each successfully staged row in
        # the caption ledger so future planner runs see the cooldown. Failure is
        # non-fatal (the rows were already inserted; the ledger is a best-effort
        # cache). Only fires when the flag is ON; the flag-off path is a no-op.
        if config.caption_cooldown_enabled():
            try:
                from . import caption_ledger as _ledger
                for row in inserted:
                    caption = row.get("caption") or ""
                    post_date = row.get("post_date") or ""
                    if caption and post_date:
                        _ledger.record_staged(account_key, caption, post_date)
            except Exception:
                pass  # ledger stamp failure is never fatal
        return inserted

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

    def deny_with_reason(self, account_key, row_id, reject_reason):
        """PATCH one row to status='denied' and reject_reason=<reject_reason>,
        filtered by BOTH id AND gym_id so a row belonging to another gym is
        never touched. Used by the dedupe_forward_book job (Wave 0.2).
        Returns the updated row dict, or None when zero rows matched."""
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
            json={"status": "denied", "reject_reason": reject_reason},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        for row in rows:
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    def patch_pending_plan(self, account_key, row_id, *, caption=None, pillar=None):
        """PATCH a WIPEABLE row's caption and/or pillar (the grade self-fix lane,
        AGENT_GRADE_SELF_FIX), filtered by id AND gym_id AND a server-side
        status IN (pending,draft,queued) guard, so a human-owned row (approved /
        published / publishing / denied / killed / failed) can NEVER be modified
        through this method no matter what the caller passes — the hard guarantee
        that self-remediation only ever rewrites fresh machine drafts. Status
        stays 'pending' (the approval gate is untouched: the row remains in the
        owner's approval queue; nothing is auto-approved). Returns the updated
        row dict, or None when zero rows matched."""
        fields = {}
        if caption is not None:
            fields["caption"] = caption
        if pillar is not None:
            fields["pillar"] = pillar
        if not fields:
            return None
        fields["status"] = "pending"
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": f"in.({','.join(_WIPEABLE_STATUSES)})",
        }
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json=fields,
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    def list_pending_future(self, account_key, today_iso):
        """Return all content_calendar rows for account_key where status='pending'
        and post_date > today_iso. Used by the dedupe_forward_book job."""
        params = {
            "gym_id": f"eq.{account_key}",
            "status": "eq.pending",
            "post_date": f"gt.{today_iso}",
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

    def rows_in_range(self, account_key, start_iso, end_iso):
        """Return all non-denied content_calendar rows for account_key with
        post_date in [start_iso, end_iso] inclusive, ordered by post_date.
        Used by the grade_sweep job (Wave 6): trailing-30 and forward-book
        windows are both graded from this read. Denied rows (e.g. the
        duplicate purge) never count for or against a grade."""
        params = {
            "gym_id": f"eq.{account_key}",
            # ONLY FEED-REACHABLE ROWS GRADE (2026-08-31): the grade is a promise about
            # what the gym's audience will actually see. Dead rows (denied/killed) and
            # placeholder 'draft' sample books (294 boilerplate rows shared across 8
            # template gyms — the cross-gym duplicate-hash alerts) were graded as the
            # forward book and held every one of those gyms at F on content that can
            # never publish. Positive allowlist, not exclusions, so a future status
            # never leaks into grading by default.
            "status": "in.(pending,approved,publishing,published,coach_review)",
            "post_date": f"gte.{start_iso}",
            "order": "post_date",
            "limit": "1000",
        }
        r = self._client().get(
            self._rest(_TABLE),
            params={**params, "and": f"(post_date.lte.{end_iso})"},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def list_event_rows(self, account_key, event_id):
        """Every content_calendar row carrying this event_id for the gym (the event's
        whole arc). Gym-scoped by gym_id=eq so another gym's rows are never returned.
        Used by the cancel/ended sweep, the status job's publish gate, and the dead-link
        guard (all event-scoped). Returns a list of dicts (empty when none)."""
        params = {
            "gym_id": f"eq.{account_key}",
            "event_id": f"eq.{event_id}",
            "order": "post_date",
            "limit": "1000",
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

def _stage_belts(account_key, payload):
    """Apply the stage-time empty-caption + verbatim-dedup belts to an
    insert_rows batch (see the insert_rows comment). Returns the rows that may
    stage. Both belts are flag-gated (default OFF -> the batch passes through
    byte-for-byte). A belt failure NEVER blocks staging (fails open, matching
    the ledger's posture) — the always-on publish-time belts still stand."""
    empty_guard = False
    dedup_guard = False
    try:
        empty_guard = config.empty_caption_guard_enabled()
        dedup_guard = config.caption_cooldown_enabled()
    except Exception:
        return payload
    if not (empty_guard or dedup_guard):
        return payload

    def _is_story(row):
        return str((row or {}).get("format") or "").strip().lower() == "story"

    def _is_gbp_photo_drop(row):
        """True for a Google Business PHOTO drop, which is image-only BY DESIGN.

        2026-09-02: the empty-caption guard below exists because "a feed post may not
        ship without real words". A GBP photo drop is not a feed post: gbp_planner
        builds it with caption="" deliberately (format='photo', 4 per month per the
        §5.1 cadence) because Google takes it as a photo upload on the listing. The
        guard was dropping every one of them at stage time -- the first real fleet run
        planned 12 rows for ENG and persisted 8, silently losing a third of the month.
        Exempt it exactly the way a story already is: both are legitimate caption-less
        post types, and nothing else about the guard changes."""
        r = row or {}
        return (str(r.get("account") or "").strip().lower() == "googlebusiness"
                and str(r.get("format") or "").strip().lower() == "photo")

    def _alert(msg):
        print(f"[portal-calendar-store] {msg}")
        try:
            from . import ops_alerts
            ops_alerts.alert(msg)
        except Exception:
            pass  # alerting never blocks staging

    kept = []
    batch_dates_by_hash = {}   # verbatim hash -> set of post_dates staged in THIS batch
    for row in payload:
        caption = str(row.get("caption") or "")
        post_date = str(row.get("post_date") or "")[:10]
        if _is_story(row) or _is_gbp_photo_drop(row):
            kept.append(row)
            continue
        if empty_guard:
            try:
                from .publish_guard import visible_len
                if visible_len(caption) == 0:
                    _alert(
                        f"empty caption guard: dropped a {account_key} feed row for "
                        f"{post_date or 'unknown date'} at stage time (a feed post may "
                        "not ship without real words); the slot refills on the next "
                        "plan pass")
                    continue
            except Exception:
                pass
        if dedup_guard and caption.strip() and post_date:
            try:
                from . import caption_ledger as _ledger
                h = _ledger.verbatim_hash(caption)
                seen_dates = batch_dates_by_hash.get(h, set())
                in_batch_dup = any(d != post_date for d in seen_dates)
                if in_batch_dup or _ledger.is_verbatim_blocked(
                        account_key, caption, post_date):
                    _alert(
                        f"caption dedup: dropped a {account_key} feed row for "
                        f"{post_date} at stage time (verbatim duplicate of a caption "
                        f"used within {_ledger.VERBATIM_BLOCK_DAYS} days); the slot "
                        "refills on the next plan pass with a fresh caption")
                    continue
                if h:
                    batch_dates_by_hash.setdefault(h, set()).add(post_date)
            except Exception:
                pass
        kept.append(row)
    return kept


# ---- CROSS-DAY MEDIA BELT ------------------------------------------------------
# ONE PHOTO ONE DAY, enforced at the ONE DOOR every staging lane walks through.
#
# WHY IT LIVES HERE (fleet audit, 2026-08-31): agent/media_guard.py shipped with the
# docstring "the shared cross-day media guard" for "every photo-assigning lane" and was
# actually wired into TWO — client_month_run's day loop and calendar_autopublish's
# expired auto-redate. Roughly eight other lanes assign an image and never consult it:
# client_month_run.append_gym_drive_drafts (the Drive lane gets covered_days but no
# guard_state), real_month_run -> rotation.choose (the LASSO month lane; rotation.choose
# has no exclude parameter at all), event_calendar's event arc, story_studio,
# client_infographic_fill, onboarding_demo, real_calendar_mirror. Every one of them ends
# at SupabaseCalendarStore.insert_rows. Guarding THIS door turns "the same photo on two
# different days" from "guarded on 2 lanes" into structurally impossible, and any lane
# added tomorrow inherits the rule for free.
#
# The keying is NOT reimplemented here. media_guard.row_media_key (source_media_url
# first, so an edited story keys by its RAW photo and not by its burned caption card),
# media_guard.book_state and media_guard.blocked_keys are the primitives; blocked_keys
# is also what carries the SAME-DATE SIBLING EXEMPTION — a feed, its FB mirror and its
# paired story are ONE post and legitimately share the photo. Getting that wrong would
# break every 2x day, so it is borrowed rather than restated.
_MEDIA_REFRAME_SUFFIX = "__feed.jpg"


def _media_alert(msg):
    """One loud line: local log always, ops alert best effort (the digest posture the
    horizon belt uses — a count and a span, never a line per row)."""
    print(f"[portal-calendar-store] {msg}")
    try:
        from . import ops_alerts
        ops_alerts.alert(msg)
    except Exception:  # noqa: BLE001 - alerting never blocks staging
        pass


def _media_library_path(account_key):
    """The gym's OWN media folder, used for one narrow purpose: resolving autofit
    reframe names ('<sha12>__feed.jpg') back to the raw library basename so a reframed
    feed card and its own raw photo are seen as the same photo (the zanshin repeats).

    STRICT lookup only — the exact registry keys for this gym, and `library_prefix`
    read DIRECTLY rather than through Account.library_path(), which falls back to the
    shared LIBRARY_PATH parent when a gym's prefix is empty (the LASSO empty-prefix
    client-photo leak). A wrong library here would hash another gym's photos and could
    manufacture a false collision, so a miss returns '' and the belt simply matches
    reframe-name to reframe-name, which is still exact for same-lane rows."""
    try:
        from . import accounts as _accounts
        base = str(account_key or "")
        for key in (base, f"{base}_ig", f"{base}_fb"):
            acct = _accounts.get_account(key)
            prefix = str(getattr(acct, "library_prefix", "") or "") if acct else ""
            if prefix:
                return prefix
    except Exception:  # noqa: BLE001 - resolution is an optimization, never a gate
        return ""
    return ""


class _ReadProbe:
    """A read-only pass-through around the store that REMEMBERS whether any month read
    raised. media_guard.book_state deliberately swallows a failed month and returns the
    partial state it could gather — right for a planner that still has a rotation window
    behind it, wrong for a belt that would then drop rows while blind. This is how the
    belt learns it never saw the book. Exposes list_month only: the belt must not be
    able to write through it."""

    def __init__(self, store):
        self._store = store
        self.failed = ""            # the exception type name of the FIRST failure

    def list_month(self, account_key, month):
        try:
            return self._store.list_month(account_key, month)
        except Exception as exc:    # noqa: BLE001 - recorded, then re-raised to book_state
            self.failed = self.failed or type(exc).__name__
            raise


def _media_stage_belt(store, account_key, payload, *, alert=None):
    """Drop any incoming row whose photo already sits on a DIFFERENT day of this gym's
    book. Returns the rows that may stage.

    Gated on media_guard.enabled() (AGENT_MEDIA_CROSS_DAY_GUARD, already default ON —
    no new flag, no changed default). Flag OFF => the batch passes through byte-for-byte
    and NOT ONE extra read is issued.

    IN-BATCH COLLISIONS COUNT. A month rebuild stages the whole month in one call, so
    the rows that would repeat a photo are usually siblings inside THIS payload and not
    yet in the book at all. Each accepted placement is folded into the state with
    media_guard.note_placed, exactly as client_month_run does inside its day loop, so
    photo_07 on the 3rd blocks photo_07 on the 17th of the same batch.

    ONE READ PER CALL. book_state is fetched once for the batch's whole date span (a
    month build is one call, so a per-row read would be ~90 month queries). The read is
    skipped entirely when no row in the batch even has a guarded image.

    FAILS OPEN, LOUDLY, AND COMPLETELY. A lookup hiccup must never sink a whole month's
    staging. book_state swallows a failed month read and returns what it could get, so
    the belt watches the reads through _ReadProbe: if ANY month read failed, the belt
    has no ground truth for this gym and STANDS DOWN for the whole batch — including
    the in-batch check, which would otherwise keep judging on a book it never saw. That
    is media_guard's own stated posture ("the guard degrades open, never blocks planning
    on a flaky read"), and the alternative — a belt that quietly drops half a month
    because Supabase blinked — is strictly worse than the repeat it prevents. Every
    stand-down prints a line; the per-lane guard and the cross-day repeat sweep remain
    the backstop.

    NOT this belt's business: a row with NO image at all (a different concern, owned
    elsewhere) and GBP rows, which keep their own deliberate §3 reuse windows
    (rotation.reuse_blocked) and are outside media_guard's scope by design."""
    if not payload:
        return payload
    _say = alert or _media_alert
    try:
        from . import media_guard
        if not media_guard.enabled():
            return payload
    except Exception:  # noqa: BLE001 - no flag read, no belt; staging is never blocked
        return payload
    from datetime import date as _date
    try:
        # Pass 1: the rows this belt may judge. A row with no key (no image) or no
        # post_date is kept and never recorded — only same-photo-different-day
        # collisions are this belt's concern.
        keyed = []          # (index, media_key, post_date)
        for idx, row in enumerate(payload):
            acct = str((row or {}).get("account") or "").strip().lower()
            if acct not in media_guard._GUARDED_ACCOUNTS:
                continue    # GBP keeps its own reuse windows
            key = media_guard.row_media_key(row)
            pd = str((row or {}).get("post_date") or "")[:10]
            if not key or not pd:
                continue
            keyed.append((idx, key, pd))
        if not keyed:
            return payload  # nothing to guard -> not one extra read

        parsed = []
        for _idx, _key, pd in keyed:
            try:
                parsed.append(_date.fromisoformat(pd))
            except ValueError:
                pass
        if not parsed:
            return payload
        start, last = min(parsed), max(parsed)

        # Reframe resolution costs a library hash walk, so pay for it ONLY when the
        # batch actually carries autofit-named cards.
        library_path = ""
        if any(k.endswith(_MEDIA_REFRAME_SUFFIX) for _i, k, _d in keyed):
            library_path = _media_library_path(account_key)

        probe = _ReadProbe(store)
        state = media_guard.book_state(
            account_key, probe, start, (last - start).days + 1,
            log=lambda m: print(f"[portal-calendar-store] media belt: {m}"),
            library_path=library_path or None)
        if probe.failed:
            _say(f"cross-day media belt STOOD DOWN for {account_key}: the book read "
                 f"failed ({probe.failed}), so the belt has no ground truth and this "
                 "batch stages UNGUARDED rather than risk gutting a month; the "
                 "per-lane guard and the cross-day repeat sweep remain the backstop")
            return payload
        rmap = (media_guard.reframe_map(library_path, [k for _i, k, _d in keyed])
                if library_path else {})

        dropped = {}        # index -> (post_date, media_key)
        for idx, key, pd in keyed:
            key = rmap.get(key, key)
            if key in media_guard.blocked_keys(state, pd):
                dropped[idx] = (pd, key)
                continue
            media_guard.note_placed(state, key, pd)
        if not dropped:
            return payload

        # SMALL LIBRARIES NEVER BLOCK A CALENDAR (media_guard's own stated posture: the
        # thin-library case falls back to maximum spacing and one digest, it does not
        # stop posts). If EVERY guarded row of a MULTI-DAY batch collides, the gym has
        # fewer photos than it has days, and dropping them all would hand the client an
        # empty month — worse than a spaced repeat. Stage it and say so once, kv-deduped,
        # in the needs-media language family.
        # The multi-day condition is what keeps this from becoming the belt's loophole:
        # a single-day insert (story_studio, client_infographic_fill, a deny backfill —
        # exactly the one-row lanes this belt exists to cover) is NOT a month at risk,
        # its slot refills on the next plan pass, and it obeys the rule like everyone
        # else. A partial drop always drops: the days that keep a unique photo keep it.
        days = sorted({pd for pd, _k in dropped.values()})
        if len(dropped) == len(keyed) and len(days) > 1:
            media_guard.alert_small_library(account_key, start.isoformat())
            return payload

        kept = [row for i, row in enumerate(payload) if i not in dropped]
        span = days[0] if len(days) == 1 else f"{days[0]} to {days[-1]}"
        sample = ", ".join(sorted({k for _d, k in dropped.values()})[:3])
        _say(f"cross-day media belt: dropped {len(dropped)} {account_key} row(s) "
             f"({span}) at stage time — that photo already sits on a DIFFERENT day of "
             f"this gym's book (e.g. {sample}). ONE PHOTO ONE DAY; the day refills on "
             "the next plan pass with another photo.")
        return kept
    except Exception as exc:  # noqa: BLE001 - a lookup hiccup never sinks a month
        _say(f"cross-day media belt SKIPPED for {account_key} "
             f"({type(exc).__name__}) — staging continued UNGUARDED; the per-lane "
             "guard and the cross-day repeat sweep remain the backstop")
        return payload


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
