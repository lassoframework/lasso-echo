"""
GBP DB lane — the thin Supabase reads/writes the publish worker + reconcile need,
layered over the existing SupabaseCalendarStore client (same creds, same PostgREST
pattern, same scrub-on-error). Kept separate from portal_calendar_store so the GBP rail
is self-contained. Everything here is content_calendar (read-side mirror) + the
gym_gbp_connections table; NOTHING publishes.
"""

import uuid
from datetime import datetime, timezone

from . import config
from .portal_calendar_store import SupabaseCalendarStore, PortalStoreError, _scrub


def _now_iso():
    """Current UTC time as an ISO-8601 'Z' string (updated_at/last_ok_at stamps)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

_CAL = "content_calendar"
_CONN = "gym_gbp_connections"
_METRICS = "gym_gbp_metrics"
PLATFORM = "googlebusiness"


class GbpStore:
    """Reads/writes for the GBP worker. Wraps a SupabaseCalendarStore for its live
    httpx client + auth. available() is False without portal creds (worker no-ops)."""

    def __init__(self, base=None):
        self._s = base or SupabaseCalendarStore()

    def available(self):
        """True only when the wrapped store has real portal creds. The base
        SupabaseCalendarStore has no available(), so we check its _url/_key directly —
        otherwise a creds-less run would sail past the guard and crash on the first HTTP
        call instead of no-opping cleanly."""
        base_avail = getattr(self._s, "available", None)
        if callable(base_avail):
            return base_avail()
        return bool(getattr(self._s, "_url", "") and getattr(self._s, "_key", ""))

    # ---- reads --------------------------------------------------------------
    def approved_gbp_rows(self, run_date):
        """APPROVED googlebusiness rows due on/before run_date, not yet published. The
        human tap set status='approved'; the worker never approves. image_url present."""
        params = {
            "account": f"eq.{PLATFORM}",
            "status": "eq.approved",
            "post_date": f"lte.{run_date}",
            "published_at": "is.null",
            "late_post_id": "is.null",
            "publish_claim_token": "is.null",
            "image_url": "not.is.null",
            # MEDIA HOLD + VARIANT GUARD (2026-10-02, Sol review): a needs-media row,
            # a claimed/published row, or a candidate/archived variant beside the real
            # active slot row is never due for the publish lane.
            "media_not_ready_reason": "is.null",
            "variant_status": "eq.active",
            "order": "post_date",
        }
        rows = self._get(_CAL, params)
        # PostgREST has no non-empty-string operator and cannot trim, so blank or
        # whitespace image_url slips past not.is.null — drop those client-side, and
        # re-drop any row that still carries a media hold (same fail-closed pattern
        # as the IG/FB lane's due-rows prefetch).
        return [row for row in rows
                if (row.get("image_url") or "").strip()
                and not (row.get("media_not_ready_reason") or "").strip()]

    def recent_published_gbp(self, since_iso):
        """PUBLISHED googlebusiness rows with a late_post_id whose published_at is within
        the reconcile window (>= since_iso). Feeds the hourly-for-48h §7.2 poll."""
        params = {
            "account": f"eq.{PLATFORM}",
            "status": "eq.published",
            "published_at": f"gte.{since_iso}",
            "late_post_id": "not.is.null",
            "order": "published_at",
        }
        return self._get(_CAL, params)

    def connections_for(self, portal_gym_key):
        """gym_gbp_connections rows for a gym (any status). The worker filters to
        status='connected' and routes by gbp_location_id."""
        return self._get(_CONN, {"portal_gym_key": f"eq.{portal_gym_key}",
                                 "order": "connected_at"})

    def upsert_connection(self, conn):
        """Insert or update one gym_gbp_connections row, keyed by the table's
        unique (portal_gym_key, gbp_location_id). `conn` must carry portal_gym_key +
        gbp_location_id (the key) plus zernio_profile_id, zernio_account_id, status, and
        optionally location_name / timezone / connected_at / connected_by.

        PRESERVES a timezone already on the row: the sync never knows the real tz (Zernio
        does not return it), so an existing row's tz — which a human may have corrected —
        is left untouched; tz is written ONLY when the row is created. Returns the row.
        Idempotent: re-running with the same live connection is a no-op update."""
        key = str(conn.get("portal_gym_key") or "")
        loc = str(conn.get("gbp_location_id") or "")
        if not key or not loc:
            raise PortalStoreError(0, "upsert_connection needs portal_gym_key + gbp_location_id")
        existing = self._get(_CONN, {"portal_gym_key": f"eq.{key}",
                                     "gbp_location_id": f"eq.{loc}", "limit": "1"})
        if existing:
            # UPDATE the volatile fields only; keep timezone + connected_at + connected_by.
            fields = {k: conn[k] for k in ("zernio_profile_id", "zernio_account_id",
                                           "status", "location_name")
                      if conn.get(k) is not None}
            fields["updated_at"] = conn.get("updated_at") or _now_iso()
            if str(conn.get("status") or "").lower() == "connected":
                fields["last_ok_at"] = fields["updated_at"]
            r = self._s._client().patch(
                self._s._rest(_CONN),
                params={"portal_gym_key": f"eq.{key}", "gbp_location_id": f"eq.{loc}"},
                headers=self._s._headers({"Content-Type": "application/json",
                                          "Prefer": "return=representation"}),
                json=fields, timeout=30)
            if r.status_code >= 400:
                raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
            rows = r.json() or []
            return rows[0] if rows else None
        # INSERT a new row (timezone written once, on create).
        r = self._s._client().post(
            self._s._rest(_CONN),
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json=conn, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def all_connections(self):
        """Every connection row (for the nightly token-health sweep, Phase 1 §3.3, which
        the portal owns — exposed here read-only for staff tooling)."""
        return self._get(_CONN, {"order": "portal_gym_key"})

    def future_gbp_rows(self, portal_gym_key, on_or_after):
        """ACTIVE googlebusiness rows for a gym dated on/after `on_or_after`. The
        dogfood/planner idempotency guard: a gym that already has a future GBP month in
        flight is left untouched (never a duplicate plan). TERMINAL rows (failed / denied /
        deleted) are EXCLUDED so a single stale cleanup row can never block a fresh plan
        forever — only a real in-flight month (pending/approved/scheduled/published)
        counts."""
        return self._get(_CAL, {"account": f"eq.{PLATFORM}",
                                "gym_id": f"eq.{portal_gym_key}",
                                "post_date": f"gte.{on_or_after}",
                                "status": "not.in.(failed,denied,deleted)",
                                "order": "post_date"})

    def any_gbp_rows(self, portal_gym_key):
        """True if the gym has ANY googlebusiness row ever (any date, any status). GATE 2
        month-1 detection: no prior rows => this is the gym's first GBP month, so it is
        written withheld ('coach_review')."""
        rows = self._get(_CAL, {"account": f"eq.{PLATFORM}",
                                "gym_id": f"eq.{portal_gym_key}",
                                "select": "id", "limit": "1"})
        return bool(rows)

    def release_coach_review(self, portal_gym_key):
        """GATE 2 coach release: flip this gym's withheld googlebusiness 'coach_review'
        rows to 'pending' (owner-visible) in one PATCH. Returns the released rows. A coach
        runs this after screening the gym's first month."""
        r = self._s._client().patch(
            self._s._rest(_CAL),
            params={"account": f"eq.{PLATFORM}", "gym_id": f"eq.{portal_gym_key}",
                    "status": "eq.coach_review"},
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json={"status": "pending"}, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def onboarding_intake(self, portal_gym_key):
        """The gym's onboarding_intake offer fields ({business_name, offers, ghl_link}) or
        None. LASSO's own record is keyed by business_name; client gyms match the base key.
        Offers/ghl_link ONLY — the planner never invents an offer. Reads through this
        store's own _get (the base SupabaseCalendarStore has none)."""
        base = portal_gym_key.rsplit("_", 1)[0] if portal_gym_key.endswith(("_ig", "_fb")) \
            else portal_gym_key
        rows = self._get("onboarding_intake",
                         {"select": "business_name,offers,ghl_link",
                          "business_name": f"ilike.*{base}*", "limit": "1"})
        return rows[0] if rows else None

    # ---- writes -------------------------------------------------------------
    def insert_rows(self, portal_gym_key, rows):
        """Passthrough to the calendar store's insert path so the planner can write its
        PENDING rows through one GBP store object. Rows are already gym-scoped dicts."""
        return self._s.insert_rows(portal_gym_key, rows)

    # ---- writes (content_calendar status only; never an approval) -----------
    def mark_published(self, row_id, late_post_id, published_at, gbp_location_id=None):
        """Stamp the row published. gbp_location_id (the CONNECTION's location, bound at
        publish) is written back onto the row when given, so a row planned before Connect
        (null location) now carries the real location — this keeps the reconcile top-post
        ranker keyed on the SAME (gym, location, month) as the publish-time posts_published
        bump (G3)."""
        fields = {"status": "published", "late_post_id": late_post_id or "",
                  "published_at": published_at}
        if gbp_location_id:
            fields["gbp_location_id"] = gbp_location_id
        return self._patch(row_id, fields)

    def mark_failed(self, row_id, reject_reason):
        """§7.1/§7.2: a failed GBP post -> status='failed' + plain-English reason. NEVER
        auto-requeued; a coach fixes and requeues through the Echo write path."""
        return self._patch(row_id, {"status": "failed",
                                    "reject_reason": (reject_reason or "")[:500]})

    def mark_status(self, row_id, status):
        return self._patch(row_id, {"status": status})

    def claim_publishing(self, row_id):
        """EXACTLY-ONCE claim for the GBP lane, now carrying a durable logical-attempt
        UUID (2026-10-02): one conditional PATCH flips approved -> publishing AND writes
        a fresh publish_claim_token in the same write, so the token is the DB-persisted
        identity of THIS attempt. The returned token (not an invented one) is what the
        live Zernio create uses as its Idempotency-Key; an ambiguous send later keeps
        the row in 'publishing' WITH this token for manual provider readback.

        Returns the persisted token string on a won claim, None on a lost claim (zero
        rows updated: another worker owns it or its status changed). Raises
        PortalStoreError when the returned row does not carry the exact token we wrote
        (schema/API drift) — never proceed on an unverified token."""
        token = str(uuid.uuid4())
        # MEDIA HOLD GUARD (2026-10-02, Sol review release-blocker): the claim PATCH
        # carries every precondition server-side so a held/archived/claimed/published
        # row can never be claimed: still approved, still googlebusiness, the ACTIVE
        # variant for its slot, unpublished, unclaimed, carrying a non-null image and
        # NO media_not_ready_reason. Mirrors the IG/FB lane's conditional PATCH.
        r = self._s._client().patch(
            self._s._rest(_CAL),
            params={"id": f"eq.{row_id}",
                    "status": "eq.approved",
                    "account": f"eq.{PLATFORM}",
                    "variant_status": "eq.active",
                    "published_at": "is.null",
                    "late_post_id": "is.null",
                    "publish_claim_token": "is.null",
                    "image_url": "not.is.null",
                    "media_not_ready_reason": "is.null"},
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json={"status": "publishing", "publish_claim_token": token}, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not rows:
            return None                      # lost claim: another owner; send nothing
        row = rows[0]
        if (str(row.get("id")) != str(row_id)
                or str(row.get("status") or "") != "publishing"
                or str(row.get("publish_claim_token") or "") != token):
            raise PortalStoreError(
                409, "publish claim returned an inconsistent row/token; "
                     "treating the claim as unverified and sending nothing")
        # The PATCH predicates can exclude a hold but cannot trim: a blank/whitespace
        # image_url passes not.is.null. The returned row is the last client-side gate
        # before the worker sends. On failure the claim is RETAINED (never released:
        # releasing would let a second attempt send an ambiguous provider post) and
        # the caller is told loudly to send nothing.
        if (not (row.get("image_url") or "").strip()
                or (row.get("media_not_ready_reason") or "").strip()):
            raise PortalStoreError(
                409, "publish claim won but the claimed row carries a blank image "
                     "or a media hold; claim retained in 'publishing' and nothing "
                     "sent — manual reconciliation required")
        return token

    def _transition_claimed(self, row_id, claim_token, fields, expect_status):
        """Conditional terminal transition for a claimed row: matches on id + still
        'publishing' + the EXACT claim token, so a changed/lost/re-claimed row can never
        be overwritten by a stale attempt. Clears publish_claim_token only as part of
        this proven terminal write. Zero matched rows (or a row that comes back with an
        unexpected status) raises PortalStoreError visibly — the caller must NOT fall
        back to an unconditional write."""
        if not claim_token:
            raise PortalStoreError(422, "claimed transition requires the claim token")
        r = self._s._client().patch(
            self._s._rest(_CAL),
            params={"id": f"eq.{row_id}", "status": "eq.publishing",
                    "publish_claim_token": f"eq.{claim_token}"},
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json=dict(fields, publish_claim_token=None), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        row = rows[0] if rows else None
        if row is None or str(row.get("status") or "") != expect_status:
            raise PortalStoreError(
                409, f"conditional {expect_status} transition matched no row for "
                     f"{row_id}; the claim changed or was lost — NOT overwritten")
        return row

    def mark_published_claimed(self, row_id, claim_token, late_post_id, published_at,
                               gbp_location_id=None):
        """Confirmed-success terminal stamp for the exact claimed attempt (token CAS)."""
        fields = {"status": "published", "late_post_id": late_post_id or "",
                  "published_at": published_at}
        if gbp_location_id:
            fields["gbp_location_id"] = gbp_location_id
        return self._transition_claimed(row_id, claim_token, fields, "published")

    def mark_failed_claimed(self, row_id, claim_token, reject_reason):
        """Definite no-post terminal stamp for the exact claimed attempt (token CAS)."""
        return self._transition_claimed(
            row_id, claim_token,
            {"status": "failed", "reject_reason": (reject_reason or "")[:500]},
            "failed")

    def release_publishing_claim(self, row_id, claim_token, status="approved"):
        """Release a claimed row back to a non-terminal status (a HOLD, not an outcome:
        needs_reconnect, outside the publish window, a lapsed offer). Token-scoped CAS;
        clears the token because no provider attempt was made. Returns the updated row,
        or None when the claim no longer matches (visible to the caller, never
        overwritten unconditionally)."""
        if not claim_token:
            raise PortalStoreError(422, "claim release requires the claim token")
        r = self._s._client().patch(
            self._s._rest(_CAL),
            params={"id": f"eq.{row_id}", "status": "eq.publishing",
                    "publish_claim_token": f"eq.{claim_token}"},
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json={"status": status, "publish_claim_token": None}, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    # ---- G3 metrics (posts_published at publish; top_post_id by clicks at reconcile) --
    def bump_posts_published(self, portal_gym_key, gbp_location_id, month_iso, *,
                             now_iso, seed_top_post_id=None):
        """G3: increment gym_gbp_metrics.posts_published for (gym, location, month) on each
        publish. The portal cron owns impressions/clicks but OMITS this count, so the
        publish rail owns it. Read-modify-write (no RPC): a missing row is INSERTed with
        posts_published=1; an existing row is PATCHed to current+1. seed_top_post_id (the
        just-published post's late_post_id) seeds top_post_id ONLY when it is still null, so
        the column is never blank once a gym has published; reconcile later refines it to
        the top post BY CLICKS. Best-effort — a metrics write must never fail a publish."""
        loc = gbp_location_id or ""
        existing = self._get(_METRICS, {
            "portal_gym_key": f"eq.{portal_gym_key}", "gbp_location_id": f"eq.{loc}",
            "month": f"eq.{month_iso}", "select": "id,posts_published,top_post_id",
            "limit": "1"})
        if existing:
            cur = existing[0].get("posts_published") or 0
            fields = {"posts_published": cur + 1, "synced_at": now_iso}
            if seed_top_post_id and not existing[0].get("top_post_id"):
                fields["top_post_id"] = seed_top_post_id
            self._patch_metrics(existing[0]["id"], fields)
        else:
            self._insert_metrics({
                "portal_gym_key": portal_gym_key, "gbp_location_id": loc,
                "month": month_iso, "posts_published": 1,
                "top_post_id": seed_top_post_id or None, "synced_at": now_iso})

    def top_post_by_clicks(self, portal_gym_key, gbp_location_id, month_iso):
        """The current gym_gbp_metrics row's top_post_id for (gym, location, month), or
        None. Used by the reconcile ranker to compare a post's clicks against the record."""
        rows = self._get(_METRICS, {
            "portal_gym_key": f"eq.{portal_gym_key}", "gbp_location_id": f"eq.{gbp_location_id or ''}",
            "month": f"eq.{month_iso}", "select": "id,top_post_id", "limit": "1"})
        return rows[0] if rows else None

    def set_top_post(self, metrics_row_id, top_post_id, now_iso):
        """G3: set gym_gbp_metrics.top_post_id (the top post BY CLICKS, ranked during
        reconcile from per-post engagement — never fabricated; only set when real click
        data ranked a post)."""
        return self._patch_metrics(metrics_row_id,
                                   {"top_post_id": top_post_id, "synced_at": now_iso})

    def _patch_metrics(self, row_id, fields):
        r = self._s._client().patch(
            self._s._rest(_METRICS), params={"id": f"eq.{row_id}"},
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json=fields, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def _insert_metrics(self, fields):
        r = self._s._client().post(
            self._s._rest(_METRICS),
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json=fields, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    # ---- plumbing -----------------------------------------------------------
    def _get(self, table, params):
        r = self._s._client().get(self._s._rest(table), params=params,
                                  headers=self._s._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def _patch(self, row_id, fields):
        r = self._s._client().patch(
            self._s._rest(_CAL), params={"id": f"eq.{row_id}"},
            headers=self._s._headers({"Content-Type": "application/json",
                                      "Prefer": "return=representation"}),
            json=fields, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None
