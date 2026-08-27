"""
media_source_store.py — PostgREST access to the media_source + media_asset
tables (gym_media_drive spec §2), mirroring SupabasePodcastStore exactly: service
key read lazily, scrubbed errors, injectable http, explicit paging past the
1000-row PostgREST cap. Every test runs offline against a fake http.

The tables live in the Echo Supabase project (migrations/
media_source_media_asset_20260827.sql — applied BY HAND, an arming step). Until
the tables exist, every call raises a clear MediaStoreError; nothing crashes.

TENANT ISOLATION (spec §1.5d) is defense-in-depth and starts HERE: the asset
reads REQUIRE a gym_id and filter on it in the query. The selector adds a second
gym_id filter; the publish path adds a stage-time assertion. Three independent
gates, so a single missing filter cannot leak another gym's media.
"""
from __future__ import annotations

from . import config

_SOURCE_TABLE = "media_source"
_ASSET_TABLE = "media_asset"


class MediaStoreError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"media store {status}: {detail}")


class SupabaseMediaStore:
    """media_source / media_asset reads + writes over PostgREST. Same shape as
    SupabasePodcastStore (agent/podcast_index.py) so the two lanes stay parallel;
    the podcast-unification follow-up can later fold podcast_asset into this store."""

    _PAGE = 1000  # PostgREST caps responses; page explicitly, never assume one shot

    def __init__(self, url=None, service_key=None, http=None):
        self.url = (url if url is not None else config.supabase_url()).rstrip("/")
        self.key = service_key if service_key is not None else config.supabase_service_key()
        self._http = http

    def available(self):
        return bool(self.url and self.key)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, repo pattern
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if extra:
            h.update(extra)
        return h

    def _rest(self, table):
        return f"{self.url}/rest/v1/{table}"

    def _scrubbed(self, r):
        from . import ops_alerts
        return ops_alerts.scrub((getattr(r, "text", "") or "")[:200])

    def _get_all(self, table, params):
        """A paged GET returning every matching row (never silently truncated at
        the 1000-row PostgREST cap)."""
        out, offset = [], 0
        while True:
            q = dict(params)
            q.update({"limit": str(self._PAGE), "offset": str(offset)})
            r = self._client().get(self._rest(table), params=q,
                                   headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                raise MediaStoreError(r.status_code, self._scrubbed(r))
            batch = r.json() or []
            out.extend(batch)
            if len(batch) < self._PAGE:
                return out
            offset += self._PAGE

    # ---- media_source ---------------------------------------------------------
    def list_sources(self, gym_id=None, include_inactive=False):
        """Sources, optionally scoped to one gym. active-only unless
        include_inactive (disconnect marks inactive, never deletes)."""
        params = {"select": "*", "order": "connected_at.desc"}
        if gym_id is not None:
            params["gym_id"] = f"eq.{gym_id}"
        if not include_inactive:
            params["active"] = "eq.true"
        return self._get_all(_SOURCE_TABLE, params)

    def find_source_by_folder(self, folder_id):
        """The source bound to this folder_id ANYWHERE (any gym, active or not),
        or None. The hijack check (spec §1.5a) reads this before a bind."""
        rows = self._get_all(_SOURCE_TABLE,
                             {"select": "*", "folder_id": f"eq.{folder_id}"})
        return rows[0] if rows else None

    def insert_source(self, row):
        r = self._client().post(
            self._rest(_SOURCE_TABLE), json=[dict(row)],
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            timeout=30)
        if r.status_code >= 400:
            raise MediaStoreError(r.status_code, self._scrubbed(r))
        return True

    def update_source(self, source_id, fields):
        r = self._client().patch(
            self._rest(_SOURCE_TABLE), params={"id": f"eq.{source_id}"},
            json=dict(fields),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            timeout=30)
        if r.status_code >= 400:
            raise MediaStoreError(r.status_code, self._scrubbed(r))
        return True

    # ---- media_asset ----------------------------------------------------------
    def list_assets(self, gym_id, source_id=None):
        """Every asset for ONE gym (gym_id is REQUIRED — tenant isolation starts
        here). Optionally scoped to one source."""
        if not gym_id:
            raise MediaStoreError(400, "list_assets requires a gym_id (tenant isolation)")
        params = {"select": "*", "gym_id": f"eq.{gym_id}", "order": "id.asc"}
        if source_id is not None:
            params["source_id"] = f"eq.{source_id}"
        return self._get_all(_ASSET_TABLE, params)

    def get_asset(self, asset_id):
        """One asset row by id (any gym), or None. Used by the thumbnail proxy +
        the hide/unhide routes, which then assert the gym themselves."""
        rows = self._get_all(_ASSET_TABLE,
                            {"select": "*", "id": f"eq.{asset_id}"})
        return rows[0] if rows else None

    def insert_assets(self, rows):
        """Bulk-insert NEW asset rows. Insert-only (no upsert): existing rows are
        updated field-by-field via update_asset so probe/vision/used_count are
        never clobbered by a re-sync."""
        if not rows:
            return 0
        r = self._client().post(
            self._rest(_ASSET_TABLE), json=list(rows),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            timeout=30)
        if r.status_code >= 400:
            raise MediaStoreError(r.status_code, self._scrubbed(r))
        return len(rows)

    def update_asset(self, asset_id, fields):
        r = self._client().patch(
            self._rest(_ASSET_TABLE), params={"id": f"eq.{asset_id}"},
            json=dict(fields),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            timeout=30)
        if r.status_code >= 400:
            raise MediaStoreError(r.status_code, self._scrubbed(r))
        return True


def default_store():
    return SupabaseMediaStore()
