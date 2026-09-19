"""Server-only shared media-runway projection for Echo's two Railway services.

The ``gym_id`` column intentionally holds Echo's canonical tenant base/account
key, rather than a portal UUID.  Callers already hold that key and every query
includes it, so this store never needs an unscoped UUID lookup.

This is a small read model, not a media or Slack outbox.  It persists only the
portal-safe fallback episode and notice-delivery projection.  In particular it
never stores a Slack channel, Slack timestamp, notice text, upload URL, token,
or credential.  The migration enables RLS with grants only for ``service_role``;
this module uses only ``SUPABASE_SERVICE_ROLE_KEY`` and is never imported by a
browser client.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from . import config


_TABLE = "media_runway_state"
_TIMEOUT_SECONDS = 10
_EPISODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NOTICE_STATUSES = frozenset({"none", "unresolved", "ready", "sent", "unknown"})
_FALLBACK_STATUSES = frozenset({"unknown"})
_CLEAR = object()
_INVALID = object()


def _tenant(gym_id):
    """Return the caller's canonical base/account key, or an empty invalid key."""
    value = str(gym_id or "").strip()
    # PostgREST receives query parameters separately, but reject control characters
    # anyway: a tenant identifier is a key, never a free-form label.
    return value if value and len(value) <= 160 and not any(c.isspace() for c in value) else ""


def _episode_id(value):
    if value is None:
        return None
    value = str(value).strip()
    return value if _EPISODE_ID.fullmatch(value) else None


def _date(value):
    if value is None:
        return None
    value = str(value).strip()
    return value if _DATE.fullmatch(value) else None


def _created_at(value):
    if value is None:
        return None
    value = str(value).strip()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return value


def _fallback_projection(value):
    """Validate the only fallback fields that may cross service boundaries."""
    # A replenished media runway has no episode.  JSON null is the explicit
    # cross-service representation of that clear, distinct from an unavailable
    # read (which is always the method-level ``None`` return).
    if value is None:
        return _CLEAR
    if not isinstance(value, dict):
        return _INVALID
    allowed = {"active", "episode_id", "depleted_on", "dates",
               "drafts_need_review", "status"}
    if set(value).difference(allowed):
        return _INVALID
    active = value.get("active")
    review = value.get("drafts_need_review")
    if active is not None and not isinstance(active, bool):
        return _INVALID
    if review is not None and not isinstance(review, bool):
        return _INVALID
    episode_id = _episode_id(value.get("episode_id"))
    if value.get("episode_id") is not None and episode_id is None:
        return _INVALID
    depleted_on = _date(value.get("depleted_on"))
    if value.get("depleted_on") is not None and depleted_on is None:
        return _INVALID
    dates = value.get("dates", [])
    if not isinstance(dates, list) or len(dates) > 2:
        return _INVALID
    dates = [_date(day) for day in dates]
    if any(day is None for day in dates):
        return _INVALID
    status = value.get("status")
    if status is not None and status not in _FALLBACK_STATUSES:
        return _INVALID
    if active is True and (episode_id is None or depleted_on is None
                           or len(dates) != 2 or review is not True or status is not None):
        return _INVALID
    if active is False and (dates or review not in (False, None)):
        return _INVALID
    if active is None and status != "unknown":
        return _INVALID
    return {"active": active, "episode_id": episode_id, "depleted_on": depleted_on,
            "dates": dates, "drafts_need_review": review, "status": status}


def _notice_projection(value):
    """Validate the portal-safe notice state, deliberately excluding transport data."""
    if not isinstance(value, dict):
        return _INVALID
    allowed = {"status", "episode_id", "created_at", "delivery_confirmed"}
    if set(value).difference(allowed):
        return _INVALID
    status = value.get("status")
    if status not in _NOTICE_STATUSES:
        return _INVALID
    episode_id = _episode_id(value.get("episode_id"))
    if value.get("episode_id") is not None and episode_id is None:
        return _INVALID
    created_at = _created_at(value.get("created_at"))
    if value.get("created_at") is not None and created_at is None:
        return _INVALID
    confirmed = value.get("delivery_confirmed")
    if not isinstance(confirmed, bool):
        return _INVALID
    if confirmed != (status == "sent"):
        return _INVALID
    return {"status": status, "episode_id": episode_id,
            "created_at": created_at, "delivery_confirmed": confirmed}


class SharedMediaRunwayStore:
    """Tenant-scoped PostgREST access to the shared portal-safe runway state.

    A failed or unavailable read returns ``None``.  Callers must retain their
    local projection in that case and render its existing neutral/unknown state;
    this class never mistakes a failed read for a clear runway or delivered notice.
    Writes return ``False`` on unavailable/failed transport and never raise a
    response body that could contain credentials.
    """

    def __init__(self, url=None, service_key=None, http=None):
        self._url = (url if url is not None else config.supabase_url()).rstrip("/")
        self._key = (service_key if service_key is not None
                     else config.supabase_service_key())
        self._http = http

    def available(self):
        return bool(self._url and self._key)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests
        return requests

    def _headers(self, extra=None):
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
                   "Accept": "application/json"}
        if extra:
            headers.update(extra)
        return headers

    def _rest(self):
        return f"{self._url}/rest/v1/{_TABLE}"

    def read(self, gym_id):
        """Read one tenant row, returning ``None`` for missing/unavailable/error."""
        tenant = _tenant(gym_id)
        if not tenant or not self.available():
            return None
        try:
            response = self._client().get(
                self._rest(),
                params={"gym_id": f"eq.{tenant}",
                        "select": "gym_id,fallback_episode,notice_state,updated_at",
                        "limit": "1"},
                headers=self._headers(), timeout=_TIMEOUT_SECONDS)
            if getattr(response, "status_code", 500) >= 400:
                return None
            rows = response.json() or []
            if len(rows) != 1 or rows[0].get("gym_id") != tenant:
                return None
            fallback = _fallback_projection(rows[0].get("fallback_episode"))
            notice = _notice_projection(rows[0].get("notice_state"))
            if fallback is _INVALID or notice is _INVALID:
                return None
            return {"gym_id": tenant,
                    "fallback_episode": None if fallback is _CLEAR else fallback,
                    "notice_state": notice, "updated_at": rows[0].get("updated_at")}
        except Exception:
            return None

    def upsert(self, gym_id, fallback_episode, notice_state):
        """Atomically replace one tenant's two safe projections.

        Both values are validated before any HTTP call.  ``on_conflict=gym_id``
        keeps retries idempotent and the represented row must echo the same tenant
        key before this method reports success.
        """
        tenant = _tenant(gym_id)
        fallback = _fallback_projection(fallback_episode)
        notice = _notice_projection(notice_state)
        if (not tenant or fallback is _INVALID or notice is _INVALID
                or not self.available()):
            return False
        try:
            response = self._client().post(
                self._rest(), params={"on_conflict": "gym_id"},
                headers=self._headers({
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=representation",
                }),
                json=[{"gym_id": tenant,
                       "fallback_episode": None if fallback is _CLEAR else fallback,
                       "notice_state": notice,
                       "updated_at": datetime.now(timezone.utc).isoformat()}],
                timeout=_TIMEOUT_SECONDS)
            if getattr(response, "status_code", 500) >= 400:
                return False
            rows = response.json() or []
            return len(rows) == 1 and rows[0].get("gym_id") == tenant
        except Exception:
            return False


def default_store():
    return SharedMediaRunwayStore()
