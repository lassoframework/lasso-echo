"""
portal_gyms.py: read the portal's `gyms` table so PORTAL-added clients get welcomed.

The Stripe scan (welcome_posts.backfill) only ever sees clients who have a Stripe
subscription. A gym onboarded through the LASSO portal lives in the portal Supabase
`gyms` table (project ooqcvmcjspeltuuhcvlh) and may have NO Stripe record at all, so
the Stripe-only welcome trigger never welcomes it. This module is the SECOND source
for the welcome trigger: it lists recently created portal gyms so scan_portal_and_enqueue
(in welcome_queue) can welcome them the same way Stripe clients are welcomed.

Auth reuses the exact PostgREST-over-httpx pattern in portal_calendar_store.py:
SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY, read lazily by name, NEVER logged, never
stored beyond the object, never returned in any response. `http` is injectable so every
path is unit tested without a network call.

Column discovery: the REAL `gyms` columns are read FIRST (a single select=*&limit=1
probe, the PostgREST-native way to see the actual columns without an information_schema
grant). We then read ONLY columns that actually exist. The live table has id, name,
slug, created_at, gym_brand and status but has NO website / domain / site / url column
and NO stripe_customer_id, so a portal gym carries no scrapable domain: the caller
relies on a human-dropped logo override and otherwise marks the gym needs_logo.

Creds absent -> list_recent_portal_gyms returns [] and NOTHING is read, so a worker
without SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY is byte-for-byte unchanged (Stripe-only).
"""

import datetime

from . import config

_TABLE = "gyms"

# The columns we ask for, IF they exist on the live table (intersected with the real
# columns discovered at call time). id/name/created_at are the load-bearing three;
# slug + gym_brand are enrichment. A domain-bearing column is included opportunistically
# so that if the portal ever adds one, the reader picks it up without a code change.
_WANT_COLS = ("id", "name", "slug", "created_at", "gym_brand",
              "domain", "website", "site", "url", "status",
              "is_demo", "load_test", "is_verification")

# Candidate columns that, if present, could carry a scrapable domain. First non-empty
# one wins. None exist on the live table today; this future-proofs a later portal column.
_DOMAIN_COLS = ("domain", "website", "site", "url")

# gyms.status values that are NOT a welcomeable client. Evidence (live gyms table,
# project ooqcvmcjspeltuuhcvlh, queried 2026-08-27): the table holds four statuses —
# active (119), onboarding (28), inactive (5), archived (3). EVERY person-name lead
# stub in the 45-day window ('Joe Floria', 'Dean Holcomb', 'Ryan Brack', 'Juan
# Martinez', 'Nora M Matthew', ...) carries status='onboarding' (a lead who started
# the funnel, not a client), and the duplicate rows behind double alerts ('Bird Dog
# CrossFit', 'The Bolton Club') carry status='archived'. Every known real client is
# status='active'. Excluding these three states removed all of the 2026-08-27 junk
# alerts. A NULL, empty, or unrecognized future status is NOT excluded (fail open:
# never drop a possibly-real gym on a status we have not seen).
_EXCLUDED_STATUSES = {"onboarding", "inactive", "archived"}


class PortalGymsError(Exception):
    """A Supabase call failed. Detail is scrubbed of any secret before raising."""

    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"supabase gyms {status}: {detail}")


class PortalGymsReader:
    """Thin PostgREST client over the portal `gyms` table. `http` is injectable."""

    def __init__(self, url=None, service_key=None, http=None):
        # Read creds at construction from config (which reads env at call time).
        self._url = (url if url is not None else config.supabase_url())
        self._key = (service_key if service_key is not None
                     else config.supabase_service_key())
        self._http = http

    def available(self):
        return bool(self._url) and bool(self._key)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, matches portal_calendar_store's pattern
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

    def owner_names(self, gym_ids):
        """{gym_id: owner_name} from the onboarding_intake table for the given gyms.
        The gyms table has NO owner column (only owner_client_id, often null for a
        portal onboard), so the owner a client typed lives in onboarding_intake. Empty
        dict on any error / no creds / no rows — a missing owner never blocks a welcome,
        the card just renders without the owner line."""
        ids = [str(g) for g in gym_ids if g]
        if not ids or not self.available():
            return {}
        try:
            r = self._client().get(
                self._rest("onboarding_intake"),
                params={"select": "gym_id,owner_name",
                        "gym_id": f"in.({','.join(ids)})"},
                headers=self._headers(),
                timeout=30,
            )
            if r.status_code >= 400:
                return {}
            out = {}
            for row in (r.json() or []):
                name = (row.get("owner_name") or "").strip()
                if row.get("gym_id") and name:
                    out[str(row["gym_id"])] = name
            return out
        except Exception:
            return {}

    def real_columns(self):
        """The ACTUAL columns on the live `gyms` table, discovered by a single
        select=*&limit=1 probe (PostgREST returns every column key even on a one-row
        sample; on a truly empty table it returns [] and we can discover nothing). This
        is the information_schema-driven column use: we never SELECT a column that does
        not exist. Returns a set of column names (possibly empty)."""
        r = self._client().get(
            self._rest(_TABLE),
            params={"select": "*", "limit": "1"},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalGymsError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not rows:
            return set()
        return set(rows[0].keys())

    def list_recent_portal_gyms(self, days=45):
        """Portal gyms created within the last `days`, newest-usable first. Returns a
        list of dicts: {gym_id, name, created_at, domain, slug}. `domain` is the first
        non-empty domain-bearing column that ACTUALLY exists on the table (none today,
        so it is ""). Creds absent -> [] (nothing read, worker unchanged).

        Demo / load-test / verification gyms are excluded when those flag columns exist,
        and so are rows in a known non-client status (onboarding lead stubs, inactive,
        archived — see _EXCLUDED_STATUSES), so a welcome is never generated for a
        non-real gym or a lead who merely started the funnel. A gym with no name is
        skipped (it cannot make a card)."""
        if not self.available():
            return []
        cols = self.real_columns()
        # id, name, created_at are load-bearing; if any is missing the table is not the
        # portal gyms table we expect, so read nothing rather than guess.
        for required in ("id", "name", "created_at"):
            if required not in cols:
                return []
        # Only ask for columns that truly exist (information_schema-driven).
        select_cols = [c for c in _WANT_COLS if c in cols]
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days))
        params = {
            "select": ",".join(select_cols),
            "created_at": f"gte.{cutoff.isoformat()}",
            "order": "created_at.desc",
        }
        r = self._client().get(
            self._rest(_TABLE),
            params=params,
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalGymsError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []

        domain_cols = [c for c in _DOMAIN_COLS if c in cols]
        out = []
        for row in rows:
            if _is_excluded(row):
                continue
            name = (row.get("name") or "").strip()
            if not name:
                continue  # no name = no card
            domain = ""
            for dc in domain_cols:
                val = (row.get(dc) or "").strip()
                if val:
                    domain = val
                    break
            out.append({
                "gym_id": row.get("id"),
                "name": name,
                "created_at": row.get("created_at"),
                "domain": domain,
                "slug": (row.get("slug") or "").strip(),
            })
        # Enrich with the owner name the client typed at onboarding (from
        # onboarding_intake — the gyms table has none). Best effort: a lookup failure
        # just leaves owner_name "".
        owners = self.owner_names([o["gym_id"] for o in out])
        for o in out:
            o["owner_name"] = owners.get(str(o["gym_id"]), "")
        return out

    def gyms_by_ids(self, gym_ids):
        """RAW gyms rows for the given ids (no window, no exclusion filter): the prune
        lane needs to SEE a row's current status/flags to judge it, so this returns the
        rows as-is, selecting only columns that actually exist. RAISES on any failure —
        no creds, missing load-bearing columns, an HTTP error — so a caller can never
        mistake an outage for 'row gone' and prune a real gym on bad data."""
        ids = [str(g) for g in gym_ids if g]
        if not ids:
            return []
        if not self.available():
            raise PortalGymsError(0, "no supabase creds; cannot judge portal rows")
        cols = self.real_columns()
        for required in ("id", "name"):
            if required not in cols:
                raise PortalGymsError(0, f"gyms table missing column {required!r}")
        select_cols = [c for c in _WANT_COLS if c in cols]
        r = self._client().get(
            self._rest(_TABLE),
            params={"select": ",".join(select_cols), "id": f"in.({','.join(ids)})"},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalGymsError(r.status_code, _scrub((r.text or "")[:200]))
        return list(r.json() or [])


def is_excluded(row):
    """True when a gyms row is NOT a real client to welcome: a demo / load-test /
    verification gym, or a row whose status is a known non-client state (onboarding
    lead stub, inactive, archived — see _EXCLUDED_STATUSES for the live-data
    evidence). Flag/status columns may be absent on some rows (older schema);
    absent, NULL, or an unrecognized status = not excluded (never drop a
    possibly-real gym on missing data)."""
    for flag in ("is_demo", "load_test", "is_verification"):
        if bool(row.get(flag)):
            return True
    status = str(row.get("status") or "").strip().lower()
    return status in _EXCLUDED_STATUSES


_is_excluded = is_excluded  # internal alias (kept for existing callers)


def list_recent_portal_gyms(days=45, reader=None):
    """Module-level convenience: recently created portal gyms as
    [{gym_id, name, created_at, domain, slug}]. Creds absent -> [] (nothing read).
    `reader` is injectable for tests; the default constructs a PortalGymsReader that
    reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY lazily."""
    reader = reader or PortalGymsReader()
    return reader.list_recent_portal_gyms(days=days)


def gyms_by_ids(gym_ids, reader=None):
    """Module-level convenience: RAW gyms rows for the given ids (see
    PortalGymsReader.gyms_by_ids). Raises on any read failure; never guesses."""
    reader = reader or PortalGymsReader()
    return reader.gyms_by_ids(gym_ids)


def _scrub(text):
    """Defensive: never let a service key echo back through an error string."""
    key = config.supabase_service_key()
    if key and text:
        text = text.replace(key, "***")
    return text
