"""
gym_shared_store.py — the CROSS SERVICE record for Echo's per gym row.

WHY THIS EXISTS (the echo.db split brain, 2026-09-10)
-----------------------------------------------------
Echo runs as TWO Railway services out of ONE repo:

  * `echo`            the worker. Generates, grades, schedules and publishes.
  * `echo-intake-web` the HTTP service. Serves /portal/... including the
                      self serve POST /portal/onboard that stands a gym up.

Each service has its OWN Railway volume mounted at /data, and Railway cannot
mount one volume on two services. So each has its OWN /data/echo.db. Anything
`onboard.run` writes to the local SQLite `gyms` table lands on the WEB service's
volume, and the worker, which is where every read of that row actually happens,
opens a completely different file and sees nothing. Measured on 2026-09-10:
the web service held 113 gym rows, the worker held 21.

The worker reads the gyms row in at least six live paths:
  zernio_publisher._default_profile_resolver / _default_page_resolver,
  config.posting_timezone_for, publish_billing_gate._live_state,
  portal_social._stripe_customer_id, zernio_profile_link.link_profiles,
  gym_media_selector, website_intake, welcome_posts.backfill (gym_list).
Every one of those saw an absent row for a portal onboarded gym.

THE FIX: Supabase is the ONE store both services already reach (SUPABASE_URL +
SUPABASE_SERVICE_ROLE_KEY are set on both). `echo_gyms` here is the shared,
durable record of the gym row, keyed by account_key exactly like the local
table. db.gym_upsert dual writes to it; db.gym_get read through hydrates the
local row on a miss. Local SQLite stays the fast path and the offline / dev
fallback, so nothing changes on a host with no Supabase creds (every test).

WHAT IS DELIBERATELY NOT MIRRORED
---------------------------------
`upload_link` holds the gym's RAW capability token in the URL, and
`intake_token_encrypted` / `intake_token_hash` / `token_sha256` are token
material. None of them is read cross service (the worker's gyms table does not
even have an upload_link column), and every token is deterministically
re-mintable from the shared AGENT_INTAKE_SIGNING_SECRET both services already
hold. So they stay OUT of this table: mirroring them would create a new place a
live token sits, for zero benefit. See MIRRORED_COLUMNS.

CONFLICTS: last write wins PER FIELD, which is exactly the local table's
existing `ON CONFLICT(account_key) DO UPDATE SET <only the passed columns>`
semantics. A PATCH carries only the fields the caller passed, so a service
writing zernio_profile_id can never blank a display_name it did not touch. No
new conflict model is introduced.

The service key is read lazily from env at call time and NEVER logged, printed,
stored on an object or returned. `http` is injectable so every path is unit
tested with no network.
"""

from . import config

_TABLE = "echo_gyms"

# Short by design; see SharedGymStore.upsert. The full-table LIST used by the
# reconcile job keeps a longer budget because it is an operator command, not a
# publish-path call.
WRITE_TIMEOUT_SECONDS = 10

# The columns this table mirrors. Deliberately a SUBSET of the local gyms table:
# see the module docstring for why token material and upload_link are excluded.
# `account_key` is the primary key and is always sent; it is not listed here
# because it is never an updatable field.
MIRRORED_COLUMNS = (
    "display_name",
    "gym_name",
    "token_revoked",
    "token_status",
    "token_rotated_at",
    "publish_flag",
    "publish_creds_status",
    "zernio_profile_id",
    "zernio_default_fb_page_id",
    "posting_timezone",
    "baseline_posts_per_week",
    "baseline_captured_at",
    "stripe_customer_id",
)


class SharedGymStoreError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"supabase {status}: {detail}")


def mirrorable(fields):
    """The subset of `fields` this table stores, dropping empty display_name so the
    local table's PRESERVE-display_name contract (db.gym_upsert) holds on the shared
    row too: a caller that upserts only zernio_profile_id must never blank the name."""
    out = {}
    for key, value in (fields or {}).items():
        if key not in MIRRORED_COLUMNS:
            continue
        if key == "display_name" and not str(value or "").strip():
            continue
        out[key] = value
    return out


class SharedGymStore:
    """PostgREST client over echo_gyms. `http` injectable for offline tests."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = (url if url is not None else config.supabase_url())
        self._key = (service_key if service_key is not None
                     else config.supabase_service_key())
        self._http = http

    def available(self):
        """True when both creds are present. A store that is not available NEVER
        raises and never writes; the caller keeps its purely local behaviour."""
        return bool(self._url) and bool(self._key)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
             "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def _rest(self, path):
        return f"{self._url}/rest/v1/{path}"

    # ---- writes ------------------------------------------------------------
    def upsert(self, account_key, fields, timeout=None):
        """PARTIAL upsert of one gym row. PATCHes only the given fields (so an
        untouched column is never clobbered); when no row matched, INSERTs.

        Returns the stored row dict. Raises SharedGymStoreError on any HTTP error
        so the caller can surface it LOUDLY rather than dropping the write.

        timeout defaults to WRITE_TIMEOUT_SECONDS, deliberately SHORTER than the 30s
        the primary-path Supabase stores use: this is a best-effort MIRROR whose local
        write has already committed, and db.gym_upsert sits on the publish path
        (zernio_routes, zernio_profile_link). A slow Supabase must cost a post a few
        seconds, never a minute.
        """
        account_key = str(account_key or "").strip()
        if not account_key:
            raise SharedGymStoreError(400, "account_key is required")
        payload = mirrorable(fields)
        # Stamp updated_at with an explicit UTC ISO value rather than leaning on
        # Postgres' lenient parsing of a "now()" string (verified 2026-09-10: Postgres
        # DOES accept it, so this is not a bug fix - it is a deliberate choice not to
        # depend on that leniency, and to make the stamp deterministic and assertable
        # in a test instead of whatever the database's clock happened to be).
        stamp = _utc_now_iso()

        if payload:
            r = self._client().patch(
                self._rest(_TABLE),
                params={"account_key": f"eq.{account_key}"},
                headers=self._headers({
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                }),
                json=dict(payload, updated_at=stamp),
                timeout=(timeout or WRITE_TIMEOUT_SECONDS),
            )
            if r.status_code >= 400:
                raise SharedGymStoreError(r.status_code, _scrub((r.text or "")[:200]))
            rows = r.json() or []
            if rows:
                return rows[0]

        # No row matched (or there was nothing to PATCH): INSERT. on_conflict makes
        # this idempotent against a racing writer that inserted between our PATCH
        # and this POST, so two concurrent onboards can never 409 each other.
        r = self._client().post(
            self._rest(_TABLE),
            params={"on_conflict": "account_key"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }),
            json=[dict(payload, account_key=account_key, updated_at=stamp)],
            timeout=(timeout or WRITE_TIMEOUT_SECONDS),
        )
        if r.status_code >= 400:
            raise SharedGymStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    # ---- reads -------------------------------------------------------------
    def get(self, account_key, timeout=None):
        """One gym row by account_key, or None. Raises on a real HTTP error.

        Same short timeout as the write, and for the same reason: gym_get's
        read-through fires from the publish path on a local miss."""
        account_key = str(account_key or "").strip()
        if not account_key:
            return None
        r = self._client().get(
            self._rest(_TABLE),
            params={"account_key": f"eq.{account_key}", "limit": "1"},
            headers=self._headers(),
            timeout=(timeout or WRITE_TIMEOUT_SECONDS),
        )
        if r.status_code >= 400:
            raise SharedGymStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def list_all(self, limit=5000):
        """Every mirrored gym row, ordered by account_key. Raises on an HTTP error."""
        r = self._client().get(
            self._rest(_TABLE),
            params={"order": "account_key", "limit": str(int(limit))},
            headers=self._headers(),
            timeout=60,
        )
        if r.status_code >= 400:
            raise SharedGymStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def delete(self, account_key):
        """Remove one mirrored row. Used ONLY by the synthetic end to end cleanup
        and by an operator; no product path deletes a gym."""
        account_key = str(account_key or "").strip()
        if not account_key:
            return False
        r = self._client().delete(
            self._rest(_TABLE),
            params={"account_key": f"eq.{account_key}"},
            headers=self._headers({"Prefer": "return=representation"}),
            timeout=30,
        )
        if r.status_code >= 400:
            raise SharedGymStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return True


def _utc_now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _scrub(text):
    """Never let the service key reach a log line or an exception message."""
    key = config.supabase_service_key()
    if key and text:
        text = text.replace(key, "<redacted>")
    return text
