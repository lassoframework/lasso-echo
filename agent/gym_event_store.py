"""
gym_event_store.py — Supabase-backed data plane for the gym_event table.

A thin PostgREST client over gym_event, mirroring portal_calendar_store's isolation
discipline: every read/write carries a gym_id=eq.<gym_id> filter AND we confirm the
returned row's gym_id before trusting it, so a cross-tenant id can never be read or
written. The service key is read lazily from env at call time and NEVER logged.

Nothing here publishes. An event is an OFFER RECORD: its status (draft/scheduled/
live/ended/cancelled) gates arc publishing. The status flips (scheduled->live->ended)
are driven by the nightly date job in event_status.py, in the GYM'S timezone.
"""

import json

from . import config

_TABLE = "gym_event"
_EVENT_SNAPSHOT_FIELDS = (
    "name", "type", "starts_on", "ends_on", "tz", "offer_text", "link",
    "brief", "media_ids", "status", "created_by", "created_at", "audit",
)


class GymEventStoreError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"supabase {status}: {detail}")


class SupabaseGymEventStore:
    """PostgREST client over gym_event. `http` injectable for offline tests."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = (url if url is not None else config.supabase_url())
        self._key = (service_key if service_key is not None else config.supabase_service_key())
        self._http = http

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
    def upsert_event(self, row):
        """INSERT or UPDATE one gym_event (upsert on id). gym_id is trusted from the
        row (the caller resolves it from the token, never the client). Returns the
        stored row dict. Idempotent: a re-submit with the same id replaces it."""
        payload = dict(row or {})
        if not payload.get("id") or not payload.get("gym_id"):
            raise GymEventStoreError(400, "gym_event requires id and gym_id")
        r = self._client().post(
            self._rest(_TABLE),
            params={"on_conflict": "id"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }),
            json=[payload],
            timeout=30,
        )
        if r.status_code >= 400:
            raise GymEventStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def set_status(self, gym_id, event_id, new_status):
        """Flip an event's status, filtered by BOTH id AND gym_id (isolation). Returns
        the updated row or None when zero rows matched (cross-gym or missing)."""
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{event_id}", "gym_id": f"eq.{gym_id}"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"status": new_status},
            timeout=30,
        )
        if r.status_code >= 400:
            raise GymEventStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
            if str(row.get("gym_id")) == str(gym_id):
                return row
        return None

    def update_event_if_status(self, gym_id, event_id, expected_status,
                               expected_row, row):
        """Atomically replace one gym-scoped event only while its stored source row
        still exactly matches ``expected_row``. Returns the updated row, or None when
        the event disappeared or any source field changed before this write.

        ``gym_event`` has no updated_at/version column, so the edit path uses the
        complete mutable source snapshot (including exact status and audit JSON) as
        its compare-and-set revision. A concurrent edit, cancel, or nightly transition
        must win instead of being overwritten by a stale merged row.
        """
        payload = dict(row or {})
        expected = dict(expected_row or {})
        if (not gym_id or not event_id or not expected_status
                or str(payload.get("id")) != str(event_id)
                or str(payload.get("gym_id")) != str(gym_id)
                or str(expected.get("id")) != str(event_id)
                or str(expected.get("gym_id")) != str(gym_id)
                or expected.get("status") != expected_status
                or payload.get("status") != expected_status):
            raise GymEventStoreError(400, "conditional gym_event update identity mismatch")
        params = {"id": f"eq.{event_id}", "gym_id": f"eq.{gym_id}"}
        for field in _EVENT_SNAPSHOT_FIELDS:
            params[field] = _exact_filter(expected.get(field))
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json=payload,
            timeout=30,
        )
        if r.status_code >= 400:
            raise GymEventStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for stored in (r.json() or []):
            if (str(stored.get("id")) == str(event_id)
                    and str(stored.get("gym_id")) == str(gym_id)):
                return stored
        return None

    # ---- reads -------------------------------------------------------------
    def get_event(self, gym_id, event_id):
        """One event by id AND gym_id, or None. Cross-gym id -> None (never revealed)."""
        r = self._client().get(
            self._rest(_TABLE),
            params={"id": f"eq.{event_id}", "gym_id": f"eq.{gym_id}", "limit": "1"},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise GymEventStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def list_events(self, gym_id, *, statuses=None):
        """All events for a gym (optionally filtered to a set of statuses), newest
        window first. Gym-scoped. Returns a list of dicts."""
        params = {"gym_id": f"eq.{gym_id}", "order": "starts_on.desc", "limit": "500"}
        if statuses:
            params["status"] = f"in.({','.join(statuses)})"
        r = self._client().get(
            self._rest(_TABLE),
            params=params,
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise GymEventStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def list_active(self):
        """Every event still in a live-cycle status (draft/scheduled/live) across ALL
        gyms — the nightly status job reads this to decide flips. Read-only. Ordered."""
        r = self._client().get(
            self._rest(_TABLE),
            params={"status": "in.(draft,scheduled,live)", "order": "gym_id",
                    "limit": "1000"},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise GymEventStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []


def _scrub(text):
    key = config.supabase_service_key()
    if key and text:
        text = text.replace(key, "***")
    return text


def _exact_filter(value):
    """PostgREST equality filter for a server-read snapshot value."""
    if value is None:
        return "is.null"
    if isinstance(value, (dict, list)):
        return "eq." + json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "eq.true" if value else "eq.false"
    return f"eq.{value}"
