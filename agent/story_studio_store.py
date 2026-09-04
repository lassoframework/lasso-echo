"""
story_studio_store.py — PostgREST access to story_request + story_render (spec §4),
mirroring SupabaseMediaStore: service key read lazily, scrubbed errors, injectable
http. Every test runs offline against a fake http.

The tables live in the Echo Supabase project (migrations/
story_studio_20260828.sql — applied BY HAND, an arming step). Until they exist,
every call raises a clear StoryStudioStoreError; nothing crashes.

TENANT ISOLATION: story_request + story_render both carry gym_id; reads REQUIRE a
gym_id and filter on it (same three-gate model as the media store). Single-row reads
and single-row writes take that gym_id too (added 2026-09-04 with the read lane: they
previously addressed a row by id ALONE, so a request id from another gym resolved --
and, through deny, could be PATCHED. The docstring above claimed the filter; only the
list read actually had it).
"""
from __future__ import annotations

from . import config

_REQUEST_TABLE = "story_request"
_RENDER_TABLE = "story_render"


class StoryStudioStoreError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"story studio store {status}: {detail}")


class SupabaseStoryStudioStore:
    _PAGE = 1000

    def __init__(self, url=None, service_key=None, http=None):
        self.url = (url if url is not None else config.supabase_url()).rstrip("/")
        self.key = service_key if service_key is not None else config.supabase_service_key()
        self._http = http

    def available(self):
        return bool(self.url and self.key)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy
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
        out, offset = [], 0
        while True:
            q = dict(params)
            q.update({"limit": str(self._PAGE), "offset": str(offset)})
            r = self._client().get(self._rest(table), params=q,
                                   headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                raise StoryStudioStoreError(r.status_code, self._scrubbed(r))
            batch = r.json() or []
            out.extend(batch)
            if len(batch) < self._PAGE:
                return out
            offset += self._PAGE

    # ---- story_request --------------------------------------------------------
    def insert_request(self, row):
        r = self._client().post(
            self._rest(_REQUEST_TABLE), json=[dict(row)],
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            timeout=30)
        if r.status_code >= 400:
            raise StoryStudioStoreError(r.status_code, self._scrubbed(r))
        body = r.json() or []
        return body[0] if body else dict(row)

    def get_request(self, request_id, gym_id=None):
        """One story_request by id, scoped to a gym. gym_id is REQUIRED: a request id
        is opaque but guessable enough that a row must never resolve across tenants."""
        if not gym_id:
            raise StoryStudioStoreError(400, "get_request requires a gym_id")
        rows = self._get_all(_REQUEST_TABLE,
                             {"select": "*", "id": f"eq.{request_id}",
                              "gym_id": f"eq.{gym_id}"})
        return rows[0] if rows else None

    def update_request(self, request_id, fields, gym_id=None):
        """Patch one story_request. gym_id is optional for back-compat with callers
        that already resolved the row, but PASS IT: without it the PATCH addresses the
        row by id alone and a foreign gym's request would be updated."""
        params = {"id": f"eq.{request_id}"}
        if gym_id:
            params["gym_id"] = f"eq.{gym_id}"
        r = self._client().patch(
            self._rest(_REQUEST_TABLE), params=params,
            json=dict(fields),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            timeout=30)
        if r.status_code >= 400:
            raise StoryStudioStoreError(r.status_code, self._scrubbed(r))
        return True

    def list_requests(self, gym_id, status=None):
        if not gym_id:
            raise StoryStudioStoreError(400, "list_requests requires a gym_id")
        params = {"select": "*", "gym_id": f"eq.{gym_id}",
                  "order": "created_at.desc"}
        if status is not None:
            params["status"] = f"eq.{status}"
        return self._get_all(_REQUEST_TABLE, params)

    # ---- story_render ---------------------------------------------------------
    def insert_render(self, row):
        r = self._client().post(
            self._rest(_RENDER_TABLE), json=[dict(row)],
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            timeout=30)
        if r.status_code >= 400:
            raise StoryStudioStoreError(r.status_code, self._scrubbed(r))
        body = r.json() or []
        return body[0] if body else dict(row)

    def get_render(self, render_id, gym_id=None):
        """One story_render by id, scoped to a gym (see get_request)."""
        if not gym_id:
            raise StoryStudioStoreError(400, "get_render requires a gym_id")
        rows = self._get_all(_RENDER_TABLE,
                            {"select": "*", "id": f"eq.{render_id}",
                             "gym_id": f"eq.{gym_id}"})
        return rows[0] if rows else None

    def render_for_request(self, request_id, gym_id):
        """The story_render written for one request, scoped to a gym. The render id
        equals the request id in this build, but a render is looked up by its
        request_id FK so that stays an implementation detail rather than a contract
        the read lane depends on."""
        if not gym_id:
            raise StoryStudioStoreError(400, "render_for_request requires a gym_id")
        rows = self._get_all(_RENDER_TABLE,
                             {"select": "*", "request_id": f"eq.{request_id}",
                              "gym_id": f"eq.{gym_id}",
                              "order": "created_at.desc"})
        return rows[0] if rows else None

    def list_renders(self, gym_id, status=None):
        """This gym's renders, newest first. The staff Story Studio surface reads
        through here, so it can never see another gym's renders."""
        if not gym_id:
            raise StoryStudioStoreError(400, "list_renders requires a gym_id")
        params = {"select": "*", "gym_id": f"eq.{gym_id}",
                  "order": "created_at.desc"}
        if status is not None:
            params["status"] = f"eq.{status}"
        return self._get_all(_RENDER_TABLE, params)

    def update_render(self, render_id, fields, gym_id=None):
        """Patch one story_render (see update_request on the gym_id)."""
        params = {"id": f"eq.{render_id}"}
        if gym_id:
            params["gym_id"] = f"eq.{gym_id}"
        r = self._client().patch(
            self._rest(_RENDER_TABLE), params=params,
            json=dict(fields),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            timeout=30)
        if r.status_code >= 400:
            raise StoryStudioStoreError(r.status_code, self._scrubbed(r))
        return True


def default_store():
    return SupabaseStoryStudioStore()
