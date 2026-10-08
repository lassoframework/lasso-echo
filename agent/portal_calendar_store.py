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
import re as _re
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

# Preserve the historical meaning of omitting source_media_url while allowing an
# explicit None to clear stale source provenance during a media replacement.
_SOURCE_MEDIA_UNSET = object()

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

# The only columns swap_media may carry besides image_url / source_media_url: the
# media identity that must travel with a swapped creative (see swap_media).
_SWAP_EXTRA_COLUMNS = ("thumbnail_url", "source_media_asset_id")

# Preparation and persistence must observe the same media, slot and release state.
_VISUAL_MEDIA_CAS_COLUMNS = (
    "status", "format", "image_url", "thumbnail_url", "source_media_url", "caption", "account",
    "post_date", "time_slot", "slot_index", "variant_status", "source_media_asset_id",
    "drive_file_id", "visual_group_key", "byte_hash", "r2_key", "media_not_ready_reason",
    "scheduled_at", "published_at", "late_post_id", "publish_claim_token",
    "publish_reservation_day", "created_at",
)

# Draft scene-migration columns. The 2026-10-04 read-only production probe of
# Zanshin row 2d188069-db62-45f6-a80f-6fedc1483dc5 (logs
# /tmp/fixer_zanshin_readonly_cas_probe_20261004.log) showed select=* on live
# content_calendar LACKS exactly these four _VISUAL_MEDIA_CAS_COLUMNS fields
# (the other 19 are applied). They are REQUIRED once the migration
# lands, but until then a row legitimately lacks them: the repeat hold CAS
# must pin them when the before image carries them and must not refuse the
# hold when it does not.
_DRAFT_SCENE_CAS_COLUMNS = frozenset(
    ("drive_file_id", "visual_group_key", "byte_hash", "r2_key"))
_CORE_VISUAL_MEDIA_CAS_COLUMNS = tuple(
    key for key in _VISUAL_MEDIA_CAS_COLUMNS
    if key not in _DRAFT_SCENE_CAS_COLUMNS)

# Characters for which a safe bare PostgREST eq encoding is NOT established.
# Live probes 2026-10-04: the quoted form eq."pending" matched ZERO rows against
# the real API (/tmp/fixer_zanshin_readonly_cas_probe_unquoted_20261004.log),
# while bare eq.<value> matched exactly the live row for all 19 applied fields
# -- including '.' and ':' inside image_url/created_at values. Follow-up probes
# of live pending/approved rows whose captions contain a comma, a double quote,
# and parentheses (/tmp/fixer_postgrest_comma_probe_20261004.log,
# /tmp/fixer_postgrest_quote_probe_20261004.log,
# /tmp/fixer_postgrest_paren_probe_20261004.log) returned HTTP 200 with the ONE
# exact row for the bare caption=eq.<exact caption> form and ZERO rows for the
# quoted form. Top-level eq filters here are independently URL-encoded by the
# HTTP client, so comma/quote/paren pass through bare. Backslash still carries
# no live-match evidence and can flip the parser into escape/quoted-literal
# mode, so it (plus non-scalars) fails closed with a precise
# blocker instead of a weakened CAS.
_EQ_FILTER_RESERVED = frozenset("\\")


def _eq_filter(value):
    """Encode one scalar as a PostgREST equality filter, or None if safe
    equality encoding is not established for it.

    The bare eq.<value> form is the ONLY form with live-match evidence (probe
    logs above); quoting is disproven by that probe for all scalar text,
    including values containing ',', '"', '(' and ')' (2026-10-04 comma /
    quote / paren probes). None is returned only for values containing a
    backslash and non-scalars: the caller must fail closed
    rather than issue a partial or weakened predicate that could hold a row
    whose real value differs."""
    if value is None:
        return "is.null"
    if isinstance(value, bool):
        return f"eq.{str(value).lower()}"
    if isinstance(value, (int, float)):
        return f"eq.{value}"
    if not isinstance(value, str):
        return None
    if any(ch in _EQ_FILTER_RESERVED for ch in value):
        return None
    return f"eq.{value}"


def _paired_story_hold_source_link(expected_row, feed):
    """True when `expected_row` is an unclaimed pending active LASSO Story and
    `feed` identifies its exact source feed row.

    A pending Story holding 'paired_feed_not_ready' is waiting on its paired
    feed's visual, so a caption-driven visual hold on it is legitimate — but
    ONLY through an exact CAS whose caller proves the story/feed linkage with
    the same identity the pairing lanes use (gym, account, post_date,
    slot_index, logical_post_id). Anything less fails closed: the hold reason
    stays in the rejected set and patch_pending_plan returns None.
    """
    if not isinstance(feed, dict):
        return False
    if (str(expected_row.get("format") or "").lower() != "story"
            or expected_row.get("status") != "pending"
            or expected_row.get("variant_status") != "active"
            or expected_row.get("published_at") is not None
            or expected_row.get("late_post_id") is not None
            or expected_row.get("publish_claim_token") is not None):
        return False
    if (str(feed.get("format") or "").lower() != "feed"
            or str(feed.get("gym_id") or "") != str(expected_row.get("gym_id") or "")
            or str(feed.get("post_date") or "")[:10]
            != str(expected_row.get("post_date") or "")[:10]):
        return False
    return all(feed.get(column) == expected_row.get(column)
               for column in ("account", "slot_index", "logical_post_id"))


_PREPARED_BACKLOG_HOLD = "prepared_backlog_waiting_for_story_and_capacity"


def _prepared_backlog_caption_hold_transition_ok(expected_row):
    """Narrow gate for the Oct 2-5 2026 prepared-backlog caption swap.

    Four LASSO feeds hold 'prepared_backlog_waiting_for_story_and_capacity'
    while waiting on reviewed Story media and capacity; their approved copy
    apply must move them to 'caption_changed_needs_new_visual' atomically with
    the new caption CAS. True ONLY for a canonical owned LASSO FEED: gym
    lasso, IG/FB account, post_date inside the incident window and not future
    dated, slot 0-2, pending/active, and completely unclaimed (no claim token,
    no late post id, no publish receipt). The exact-row CAS pins every one of
    these server-side; this check decides whether the hold may TRANSITION
    (never clear) at all. Stories, other tenants, foreign holds and stale
    snapshots fail closed.
    """
    from datetime import date as _date
    day = str(expected_row.get("post_date") or "")[:10]
    try:
        d = _date.fromisoformat(day)
    except ValueError:
        return False
    return (
        str(expected_row.get("gym_id") or "") == "lasso"
        and str(expected_row.get("account") or "").strip().lower()
        in ("instagram", "facebook")
        and _date(2026, 10, 2) <= d <= _date(2026, 10, 5)
        and d <= _date.today()
        and expected_row.get("slot_index") in (0, 1, 2)
        and str(expected_row.get("format") or "").lower() == "feed"
        and expected_row.get("status") == "pending"
        and expected_row.get("variant_status") == "active"
        and expected_row.get("publish_claim_token") is None
        and expected_row.get("late_post_id") is None
        and expected_row.get("published_at") is None)


def _slot_key(row):
    """The (post_date, account, format) a row occupies, normalized.

    Collision occupancy for 2x days lives in preserve_and_prune (per slot_index
    up to cadence capacity). This key is the cell identity: two IG feeds on the
    same date share it and are distinguished by slot_index there. Do not add
    slot_index here: planner lanes that only have locked_slots (no list_month)
    still treat the triple as the cell, which is the conservative 1x guard.
    """
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


#: The content_calendar `account` value for the Google Business lane.
_GBP_ACCOUNT = "googlebusiness"


def _column_missing(resp, column):
    """True iff `resp` is PostgREST refusing a write because `column` does not exist.

    Deliberately narrow: it must match the undefined-column error and NOTHING else, so a
    permissions failure, a constraint violation or an outage is never silently downgraded
    into "the column is missing" and retried into a quiet data loss. PostgREST surfaces
    Postgres SQLSTATE 42703 with a message of the form:
        column "late_account_id" of relation "echo_social_connections" does not exist
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    text = ""
    if isinstance(body, dict):
        text = " ".join(str(body.get(k) or "") for k in ("message", "code", "details", "hint"))
    if not text:
        try:
            text = str(resp.text or "")
        except Exception:  # noqa: BLE001
            text = ""
    low = text.lower()
    if "42703" in low:
        return True
    return f'"{column}"' in low and "does not exist" in low and "column" in low


class PortalStoreError(Exception):
    """A Supabase call failed. Detail is scrubbed of any secret before raising."""

    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"supabase {status}: {detail}")


class PreWriteCASError(PortalStoreError):
    """CAS encoding refused before the calendar PATCH was attempted."""


class CalendarInsertNotStartedError(PortalStoreError):
    """The calendar POST definitely did not start; deleted rows may be restored."""


class CadencePreconditionError(CalendarInsertNotStartedError):
    """A required feed disappeared after preflight but before calendar POST."""


_UUID_RE = _re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ReceiptStoreError(PortalStoreError):
    """A receipt RPC failed or returned something that fails strict parsing.
    The swap outcome is UNKNOWN: fail closed, never guess from a mutable row."""


class ReceiptConflictError(ReceiptStoreError):
    """SQLSTATE 23514: the action_id is bound to a different request tuple, or
    the frozen swap group went stale / drifted. Definite: nothing was written
    by THIS call (the RPC raises inside its transaction, which rolls back)."""


class ReceiptHoldError(ReceiptConflictError):
    """SQLSTATE 23514 manual-review hold: the primary row is missing or carries
    a historical NULL logical_post_id. No receipt persists; route to a human."""


class ReceiptSelectionError(ReceiptStoreError):
    """SQLSTATE 22023: the selection / prepared payload failed the allowlist.
    Definite no-write, and it will fail identically on replay."""


# The learning-lever columns patch_pending_plan is allowed to re-stamp when a
# repair changes a caption. An explicit allowlist, so this lane can never be
# used to write an arbitrary column.
_LEVER_COLUMNS = ("hook_family", "ask_type", "caption_len_band")


class SupabaseCalendarStore:
    """Thin PostgREST client over content_calendar. `http` is injectable for tests."""

    def __init__(self, url=None, service_key=None, http=None):
        # Read creds at construction from config (which reads env at call time).
        self._url = (url if url is not None else config.supabase_url())
        self._key = (service_key if service_key is not None else config.supabase_service_key())
        self._http = http

    #: Set once when a write proves echo_social_connections.late_account_id is not
    #: deployed on this environment, so the sweep stops re-attempting it every gym.
    #: Class level (not instance) because the store is constructed per call.
    _late_account_id_column_absent = False

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

    def managed_lasso_paired_story_ids(self, story_ids):
        """Read exact Story IDs protected by the paired-feed registry.

        A failed or partial read raises; calendar reconciliation must then keep
        every retained LASSO Story instead of overwriting an unknown managed
        row with a planner rerender.
        """
        from uuid import UUID
        if not isinstance(story_ids, (list, tuple, set)) or len(story_ids) > 1000:
            raise ValueError("invalid managed Story lookup size")
        ids = sorted({str(UUID(str(value))) for value in story_ids})
        found = set()
        for start in range(0, len(ids), 100):
            group = ids[start:start + 100]
            response = self._client().get(
                self._rest("lasso_managed_paired_stories"),
                params={"story_id": "in.(" + ",".join(group) + ")",
                        "select": "story_id,feed_id", "limit": str(len(group) + 1)},
                headers=self._headers(), timeout=30)
            if response.status_code >= 400:
                raise RuntimeError("managed LASSO Story registry read failed")
            rows = response.json()
            if not isinstance(rows, list) or len(rows) > len(group):
                raise RuntimeError("managed LASSO Story registry read incomplete")
            for row in rows:
                if (not isinstance(row, dict)
                        or str(row.get("story_id")) not in group
                        or not row.get("feed_id")
                        or row["story_id"] in found):
                    raise RuntimeError("managed LASSO Story registry response malformed")
                found.add(row["story_id"])
        return found

    def lasso_paired_story_ready_for_feed(self, feed_id):
        """Database source proof immediately before claiming a LASSO feed."""
        from uuid import UUID
        canonical = str(UUID(str(feed_id)))
        response = self._client().post(
            self._rest("rpc/lasso_paired_story_ready_for_feed"),
            headers=self._headers({"Content-Type": "application/json"}),
            json={"p_feed_id": canonical}, timeout=30)
        if response.status_code != 200:
            raise RuntimeError("LASSO paired Story preflight unavailable")
        ready = response.json()
        if type(ready) is not bool:
            raise RuntimeError("LASSO paired Story preflight malformed")
        return ready

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
            # 0318 (variant rows): a candidate/archived row shares a slot with its
            # group's active row. Every caller of list_month treats the return as
            # "one row per logical post" (counts, slot-locking, the client feed) --
            # without this filter a pending Astra-v2 candidate would double-count
            # the slot and could even get published by a rebuild that doesn't know
            # to skip it. See PROGRESS.md / the variant-pairing audit.
            "variant_status": "eq.active",
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

    def list_month_strict(self, account_key, month):
        """Complete counted active month snapshot for the final ownership barrier.

        Unlike best-effort planner reads, a missing/changing count, short page,
        repeated ID or malformed/foreign row cannot certify an available slot.
        """
        year, number = map(int, month.split("-"))
        first = f"{month}-01"
        last = f"{month}-{_calendar.monthrange(year, number)[1]:02d}"
        fields = {"id", "gym_id", "post_date", "account", "format",
                  "status", "slot_index", "variant_status"}
        rows, seen, expected_total = [], set(), None
        while True:
            response = self._client().get(
                self._rest(_TABLE),
                params={"gym_id": f"eq.{account_key}",
                        "post_date": [f"gte.{first}", f"lte.{last}"],
                        "variant_status": "eq.active", "order": "id",
                        "select": ",".join(sorted(fields)),
                        "limit": "500", "offset": str(len(rows))},
                headers=self._headers({"Prefer": "count=exact"}), timeout=30)
            if response.status_code >= 400:
                raise PortalStoreError(response.status_code, "live cadence read unavailable")
            page = response.json()
            total = (getattr(response, "headers", {}) or {}).get(
                "Content-Range", "").rsplit("/", 1)[-1]
            if not isinstance(page, list) or not total.isdigit():
                raise PortalStoreError(502, "live cadence read count unavailable")
            total = int(total)
            if expected_total is None:
                expected_total = total
            if total != expected_total or len(page) != min(500, total - len(rows)):
                raise PortalStoreError(502, "live cadence read incomplete or changed")
            for row in page:
                if (not isinstance(row, dict) or not fields.issubset(row)
                        or not isinstance(row["id"], str) or not row["id"]
                        or row["id"] in seen or row["gym_id"] != account_key
                        or not isinstance(row["post_date"], str)
                        or not first <= row["post_date"] <= last
                        or row["variant_status"] != "active"
                        or not isinstance(row["account"], str)
                        or not isinstance(row["format"], str)
                        or (row["status"] is not None and not isinstance(row["status"], str))
                        or (row["slot_index"] is not None
                            and (type(row["slot_index"]) is not int or row["slot_index"] < 0))):
                    raise PortalStoreError(502, "live cadence read scope invalid")
                seen.add(row["id"])
            rows.extend(page)
            if len(rows) == total:
                return rows

    def list_media_publish_history(self, account_key, since):
        """Complete cross-platform send history for a strict reuse decision.

        Include archived variants and in-flight claims. Pagination avoids a
        silently truncated nine-month history under PostgREST's response cap.

        A row already stamped 'published' but lacking published_at CONSERVATIVELY
        participates: PostgREST's gte filter silently drops NULLs, which used to
        let a published row slip the reuse window and re-send a nine-month-old
        asset. The and(status.eq.published,published_at.is.null) arm admits only
        actually-published rows with a NULL stamp — a pending/draft row has no
        published_at either and must never count as send history.
        """
        rows = []
        while True:
            params = {"gym_id": f"eq.{account_key}",
                      "or": f"(published_at.gte.{since},"
                            f"and(status.eq.published,published_at.is.null),"
                            f"status.eq.publishing)",
                      "order": "id", "limit": "500", "offset": str(len(rows))}
            r = self._client().get(self._rest(_TABLE), params=params,
                                   headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                raise PortalStoreError(r.status_code, "media history unavailable")
            page = r.json()
            if not isinstance(page, list):
                raise PortalStoreError(502, "invalid media history")
            rows.extend(page)
            if len(page) < 500:
                return rows
            if len(rows) >= 10000:
                raise PortalStoreError(502, "media history exceeds safe read bound")

    def list_variant_candidates(self, account_key, month):
        """The gym's 'candidate' rows (variant_status='candidate') whose
        post_date falls inside `month` — the complement of list_month's
        variant_status=active filter. Exists because list_month deliberately
        NEVER returns a candidate row (see its own comment: a caller treating
        the month as 'one row per logical post' must not double-count a
        pending variant), so any caller that needs to know "does this anchor
        already have a linked candidate" (e.g. lasso_astra_rework's dedup)
        cannot get that from list_month at all. Real production bug found
        2026-09-11: a first cut of that dedup silently no-op'd because it
        tried to find candidate rows inside list_month's own results."""
        year = int(month[:4])
        mon = int(month[5:7])
        last_day = _calendar.monthrange(year, mon)[1]
        first = f"{month}-01"
        last = f"{month}-{last_day:02d}"
        params = {
            "gym_id": f"eq.{account_key}",
            "post_date": [f"gte.{first}", f"lte.{last}"],
            "variant_status": "eq.candidate",
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
                  # 0318: a 'candidate' row (an unchosen Astra v2, never itself
                  # owner-visible in the review sense this gate cares about)
                  # must not count as "the gym already has a released month".
                  "variant_status": "eq.active",
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
            # Archived rows are audit records, never portal-actionable cards.
            "variant_status": "eq.active",
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

    def approve_ready(self, account_key, row_id, expected_creative=None):
        """Approve only when this gym's row still has publishable media.

        The RPC checks status, media URL, and needs-media reason in one database
        UPDATE. A portal pre-read alone cannot protect against a concurrent media
        removal between the check and the approval write. An unapplied migration
        fails closed instead of falling back to the generic status PATCH.

        APPROVAL PROVENANCE (draft, 2026-10-05, repair pass 2): the same atomic
        UPDATE records status='approved' plus a canonical digest of the row's
        exact publish-relevant fields (caption/account/format/date, the FINAL
        image_url, and the rendered/source identity fields byte_hash /
        source_media_asset_id / source_media_url; the publisher-stamped
        scheduled_at is never bound). REVIEW DEFECT 1: an Echo bearer token is
        NOT a verified human identity, so this RPC leaves approval_kind /
        approved_by / approved_at UNPROVED (NULL) and takes NO actor parameter
        -- no body-supplied identity is ever forwarded. Human provenance is
        stamped only by calendar_stamp_verified_approval, called by the PORTAL
        with its service role and an authenticated Clerk actor after this
        approval succeeds (portal-side contract; Echo never calls it). The
        claim-side proof gate requires a nonempty trusted approved_by, so an
        Echo-only approval cannot publish in Manual mode while armed.
        """
        payload = {"p_row_id": row_id, "p_gym_id": account_key}
        # VISIBLE-CARD SNAPSHOT (portal ECHO_VERIFIED_APPROVAL_PROOF contract,
        # Echo half 2026-10-05): when the portal sends its expected_creative,
        # the SAME atomic RPC UPDATE also compares caption/media_url/day_key/
        # format/platform against the locked row; a stale snapshot returns
        # zero rows and nothing is stamped. Sent ONLY when present, so the
        # flag-OFF wire shape is byte-for-byte the legacy 2-arg call (the
        # migration's p_expected DEFAULTs to NULL = legacy behavior). No
        # actor parameter: an Echo bearer token never mints human proof.
        if expected_creative is not None:
            payload["p_expected"] = expected_creative
        r = self._client().post(
            self._rest("rpc/approve_calendar_row_if_media_ready"),
            headers=self._headers({"Content-Type": "application/json"}),
            json=payload, timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if len(rows) != 1 or str(rows[0].get("gym_id")) != str(account_key):
            return None
        return rows[0]

    def recover_unproved_approval(self, account_key, row_id, expected_creative):
        """Return a fresh locked-row digest only for an exact unproved retry.

        The RPC checks the current creative and digest atomically. It never
        accepts an actor or changes proof state; the portal stamps that later.
        """
        r = self._client().post(
            self._rest("rpc/calendar_recover_unproved_approval"),
            headers=self._headers({"Content-Type": "application/json"}),
            json={"p_row_id": row_id, "p_gym_id": account_key,
                  "p_expected": expected_creative}, timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if len(rows) != 1 or str(rows[0].get("gym_id")) != str(account_key):
            return None
        return rows[0]

    def _prepare_visual_media(self, account_key, row_id, payload, *, current=None,
                              render_evidence=None, poster_render_evidence=None):
        """Prepare a replacement against the actual scoped row before its PATCH."""
        from . import visual_writer_prepare
        if not visual_writer_prepare.enabled():
            return payload
        current = current if current is not None else self.get_row(account_key, row_id)
        if current is None:
            raise visual_writer_prepare.VisualPreparationError("calendar row is unavailable for visual preparation")
        patch = dict(payload)
        is_story = str(current.get("format") or "").lower() == "story"
        if render_evidence is not None and not is_story:
            if (current.get("format") != "feed" or not isinstance(render_evidence, dict)
                    or render_evidence.get("operation") != "rehost"
                    or render_evidence.get("source_exact_url") != current.get("image_url")
                    or render_evidence.get("delivered_exact_url") != patch.get("image_url")):
                raise visual_writer_prepare.VisualPreparationError(
                    "render evidence does not bind the scoped feed replacement")
            # A feed may already be a rendition B of raw source A. Register
            # the observed B -> C operation, while the calendar keeps A. The
            # guarded PATCH verifies A/B/C belong to the attested scene; never
            # invent an A -> C render receipt or discard A to pass preparation.
            patch["source_media_url"] = current.get("source_media_url") or current["image_url"]
            patch["r2_key"] = None
            candidate = dict(current)
            candidate.update(patch)
            candidate["source_media_url"] = current["image_url"]
            candidate["byte_hash"] = None
            candidate.pop("visual_group_key", None)
            if patch["source_media_url"] != current["image_url"]:
                # The asset/Drive identity attests A, not input rendition B.
                candidate["source_media_asset_id"] = None
                candidate["drive_file_id"] = None
            prepared = visual_writer_prepare.prepare(
                self, account_key, candidate, render_evidence=render_evidence,
                poster_render_evidence=poster_render_evidence)
            if (current.get("visual_group_key")
                    and prepared["visual_group_key"] != current["visual_group_key"]):
                raise visual_writer_prepare.VisualPreparationError(
                    "feed replacement conflicts with the current source scene")
            patch["visual_group_key"] = prepared["visual_group_key"]
            patch["byte_hash"] = prepared["byte_hash"]
            return patch
        if (is_story
                and current.get("source_media_url")
                and current.get("source_media_url") != patch.get("image_url", current.get("image_url"))
                and "source_media_url" not in patch and render_evidence is None):
            raise visual_writer_prepare.VisualPreparationError(
                "story raw source requires verified rendition lineage")
        if render_evidence is not None:
            if (not is_story or not isinstance(render_evidence, dict)
                    or render_evidence.get("source_exact_url") != current.get("source_media_url")
                    or render_evidence.get("delivered_exact_url") != patch.get("image_url")):
                raise visual_writer_prepare.VisualPreparationError(
                    "render evidence does not bind the scoped story replacement")
        # A replacement must not carry a stale source identity from the old
        # image. Callers that know the replacement asset supply it explicitly.
        for field in ("source_media_url", "source_media_asset_id", "drive_file_id", "byte_hash", "r2_key"):
            if (field == "source_media_url" and is_story
                    and (current.get(field) == patch.get("image_url")
                         or render_evidence is not None)):
                continue
            if field not in patch and current.get(field):
                patch[field] = None
        candidate = dict(current)
        candidate.update(patch)
        candidate.pop("visual_group_key", None)
        prepared = visual_writer_prepare.prepare(
            self, account_key, candidate, render_evidence=render_evidence,
            poster_render_evidence=poster_render_evidence)
        patch["visual_group_key"] = prepared["visual_group_key"]
        patch["byte_hash"] = prepared["byte_hash"]
        return patch

    def patch_image_url(self, account_key, row_id, new_image_url, *, expected_row=None,
                        render_evidence=None, poster_render_evidence=None):
        """Persist a Story reburn or feed autofit without changing status.

        Publish-time replacements supply expected_row, so approved media changes
        only while its render input, source, slot and publish state still match.
        Returns exactly one verified updated row or None.
        """
        if not (new_image_url or "").strip():
            return None
        from . import visual_writer_prepare
        visual_guard = visual_writer_prepare.enabled()
        current = expected_row
        if visual_guard and current is None:
            current = self.get_row(account_key, row_id)
            if (not isinstance(current, dict)
                    or str(current.get("id")) != str(row_id)
                    or str(current.get("gym_id")) != str(account_key)
                    or current.get("status") not in ("pending", "coach_review")):
                return None
        params = {"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}"}
        if expected_row is None:
            params["status"] = "in.(pending,coach_review)"
        else:
            required = ("id", "gym_id", "status", "format", "image_url",
                        "caption", "source_media_url", "published_at", "late_post_id",
                        "account", "post_date")
            if (not isinstance(expected_row, dict)
                    or any(key not in expected_row for key in required)
                    or str(expected_row["id"]) != str(row_id)
                    or str(expected_row["gym_id"]) != str(account_key)
                    or expected_row["status"] not in ("pending", "approved")
                    or expected_row["format"] not in ("story", "feed")
                    or not expected_row["image_url"]
                    or (expected_row["format"] == "story"
                        and not expected_row.get("source_media_url"))
                    or expected_row["published_at"] is not None
                    or expected_row["late_post_id"] is not None):
                return None
            def unchanged(key, value):
                encoded = _eq_filter(value)
                if encoded is None:
                    raise PortalStoreError(
                        422, f"image patch CAS blocked: field {key!r} has no safe equality encoding")
                return encoded
            for key in ("status", "format", "image_url", "caption", "source_media_url"):
                params[key] = unchanged(key, expected_row.get(key))
            for key in ("account", "post_date", "visual_group_key", "byte_hash", "r2_key",
                        "source_media_asset_id", "drive_file_id", "variant_status",
                        "scheduled_at", "slot_index", "publish_claim_token", "publish_reservation_day"):
                if key in expected_row:
                    params[key] = unchanged(key, expected_row[key])
            params["published_at"] = "is.null"
            params["late_post_id"] = "is.null"
            params["media_not_ready_reason"] = "is.null"
        payload = {"image_url": new_image_url, "media_not_ready_reason": None}
        payload = self._prepare_visual_media(account_key, row_id, payload,
                                             current=current,
                                             render_evidence=render_evidence,
                                             poster_render_evidence=poster_render_evidence)
        if visual_guard:
            params = self._visual_media_cas(current, params)
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=payload, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if visual_guard and self._visual_media_result(
                rows, account_key, current, payload) is None:
            return None
        if not isinstance(rows, list) or len(rows) != 1:
            return None
        row = rows[0]
        if (not isinstance(row, dict) or str(row.get("gym_id")) != str(account_key)
                or str(row.get("id")) != str(row_id)):
            return None
        if any(key not in row for key in payload):
            return None
        if expected_row is not None and any(key not in row for key in required):
            return None
        if expected_row is not None and any(
                row.get(key) != payload.get(key, expected_row.get(key))
                for key in ("status", "format", "caption", "account", "post_date",
                            "source_media_asset_id", "drive_file_id", "variant_status",
                            "scheduled_at", "slot_index", "publish_claim_token", "publish_reservation_day",
                            "published_at", "late_post_id")):
            return None
        if expected_row is not None and row.get("source_media_url") != payload.get(
                "source_media_url", expected_row.get("source_media_url")):
            return None
        if all(row.get(key) == value for key, value in payload.items()):
            return row
        return None

    @staticmethod
    def _visual_media_cas(current, params):
        """Bind preparation to its observed source, slot and publish state."""
        result = dict(params)
        for key in _VISUAL_MEDIA_CAS_COLUMNS:
            encoded = _eq_filter(current.get(key))
            if encoded is None:
                raise PreWriteCASError(
                    422, f"visual media CAS blocked: field {key!r} has no safe equality encoding")
            result[key] = encoded
        return result

    @staticmethod
    def _visual_media_result(rows, account_key, current, payload):
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        row = rows[0]
        if str(row.get("gym_id")) != str(account_key) or row.get("id") != current.get("id"):
            return None
        expected = {key: current[key] for key in _VISUAL_MEDIA_CAS_COLUMNS if key in current}
        expected.update(payload)
        if any(key not in row or row[key] != value for key, value in expected.items()):
            return None
        return row

    def _prepare_visual_row(self, account_key, row, render_evidence=None,
                            poster_render_evidence=None):
        """Render evidence must describe the exact source retained on the row."""
        from . import visual_writer_prepare
        if render_evidence is not None:
            evidence = (render_evidence.as_dict() if hasattr(render_evidence, "as_dict")
                        else render_evidence)
            if (not isinstance(evidence, dict)
                    or evidence.get("source_exact_url") != (row.get("source_media_url") or row.get("image_url"))
                    or evidence.get("delivered_exact_url") != row.get("image_url")):
                raise visual_writer_prepare.VisualPreparationError(
                    "render evidence does not bind the scoped media replacement")
        prepared = visual_writer_prepare.prepare(
            self, account_key, row, render_evidence=render_evidence,
            poster_render_evidence=poster_render_evidence)
        # Preparation may emit/stage advisory evidence internally. It is not a
        # content_calendar column; retain the original preparation result while
        # returning only its calendar fields to every INSERT/PATCH caller.
        return {key: value for key, value in prepared.items()
                if key != "scene_candidate"}

    def _prepare_visual_replacement(self, account_key, current, payload,
                                    render_evidence=None, poster_render_evidence=None):
        """A swap's new source is explicit; never erase an old rendition source."""
        from . import visual_writer_prepare
        patch = dict(payload)
        source = patch.get("source_media_url")
        if (not source and current.get("source_media_url")
                and current.get("source_media_url") != current.get("image_url")):
            raise visual_writer_prepare.VisualPreparationError(
                "replacement requires an explicit source; existing raw source cannot be discarded")
        for field in ("source_media_url", "source_media_asset_id", "drive_file_id", "byte_hash", "r2_key"):
            if field not in patch and current.get(field):
                patch[field] = None
        candidate = {**current, **patch}
        candidate.pop("visual_group_key", None)
        prepared = self._prepare_visual_row(
            account_key, candidate, render_evidence, poster_render_evidence)
        patch["visual_group_key"] = prepared["visual_group_key"]
        patch["byte_hash"] = prepared["byte_hash"]
        return patch

    def patch_media(self, account_key, row_id, image_url, source_media_asset_id="", *,
                    source_media_url=None, render_evidence=None,
                    poster_render_evidence=None):
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
        if not (image_url or "").strip():
            return None
        # A real replacement resolves the explicit hold in the SAME scoped write.
        # Do not change status: it remains pending / coach_review and must pass the
        # ordinary approval gate before it can publish.
        payload = {"image_url": image_url, "media_not_ready_reason": None}
        if source_media_asset_id:
            payload["source_media_asset_id"] = source_media_asset_id
        from . import visual_writer_prepare
        prepared_write = visual_writer_prepare.enabled()
        if prepared_write:
            if (str(current.get("gym_id")) != str(account_key)
                    or str(current.get("id")) != str(row_id)
                    or current.get("status") not in ("pending", "coach_review")
                    or any(current.get(key) is not None for key in
                           ("published_at", "late_post_id", "publish_claim_token"))):
                return None
            if source_media_url is not None:
                payload["source_media_url"] = source_media_url
            elif current.get("source_media_url"):
                payload["source_media_url"] = current["source_media_url"]
            if not source_media_asset_id and current.get("source_media_asset_id"):
                payload["source_media_asset_id"] = current["source_media_asset_id"]
            if (current.get("drive_file_id")
                    and current.get("source_media_asset_id") == payload.get("source_media_asset_id")):
                payload["drive_file_id"] = current["drive_file_id"]
            payload = self._prepare_visual_replacement(
                account_key, current, payload, render_evidence, poster_render_evidence)
        # Keep the no-overwrite promise server-side too: another worker can attach
        # media after the prefetch but before this write. PostgREST's OR predicate
        # permits only a still-null or still-empty image_url to be recovered.
        params = {"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}",
                  "status": "in.(pending,coach_review)",
                  "or": "(image_url.is.null,image_url.eq.)"}
        if prepared_write:
            params = self._visual_media_cas(current, params)
        r = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=payload, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if prepared_write:
            return self._visual_media_result(rows, account_key, current, payload)
        for row in rows:
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    def swap_media(self, account_key, row_id, image_url,
                   source_media_url=_SOURCE_MEDIA_UNSET,
                   extra_fields=None, *, render_evidence=None,
                   poster_render_evidence=None, expected_row=None):
        """CROSS-DAY MEDIA GUARD sweep (Blake, 2026-08-31): re-point a WAITING row's
        media to a fresh photo because its current photo already sits on another day
        of the gym's book. STATUS-GUARDED SERVER-SIDE: the PATCH itself is filtered to
        status in (pending, coach_review), so an approved / publishing / published row
        can NEVER be swapped through this method — the gym's approval and anything
        live keep exactly the pixels they had. Caption, status and date are untouched.
        source_media_url (when given) is updated too, so a later edited-caption story
        re-burn burns onto the NEW photo, not the replaced duplicate. id+gym_id
        isolation. Returns the updated row, or None when nothing matched.

        extra_fields (2026-09-10, the video-capable portal swap): the media identity
        columns that must move WITH the pixels, limited to thumbnail_url (a video's
        poster frame; None clears a stale poster when a video row becomes a photo)
        and source_media_asset_id (the Drive asset now on the row; None clears it when
        a Drive row becomes a local-library row, so the hide / removed-from-Drive
        sweeps stop tracking an asset the row no longer carries). Any other key is
        ignored: this method never becomes a general row editor.

        Ordinary self-service supplies expected_row from the clicked snapshot.
        Its CAS runs regardless of global visual preparation; it preserves the
        existing hold and pins caption, approval, claim and schedule state.
        Completion still requires an independent readback by the handler."""
        if not (image_url or "").strip():
            return None
        # This is a real replacement, so release any earlier needs-media hold in
        # the same pending / coach_review-scoped write. Status itself is unchanged.
        payload = {"image_url": image_url}
        if expected_row is None:
            payload["media_not_ready_reason"] = None
        if source_media_url is not _SOURCE_MEDIA_UNSET:
            payload["source_media_url"] = source_media_url
        for col in _SWAP_EXTRA_COLUMNS:
            if col in (extra_fields or {}):
                payload[col] = extra_fields[col]
        from . import visual_writer_prepare
        prepared_write = visual_writer_prepare.enabled()
        current = None
        params = {"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}",
                  "status": "in.(pending,coach_review)"}
        if prepared_write or expected_row is not None:
            current = dict(expected_row) if expected_row is not None else self.get_row(account_key, row_id)
            if (current is None or str(current.get("gym_id")) != str(account_key)
                    or str(current.get("id")) != str(row_id)
                    or current.get("status") not in ("pending", "coach_review")
                    or any(current.get(key) is not None for key in
                           ("published_at", "late_post_id", "publish_claim_token"))):
                return None
            if prepared_write:
                payload = self._prepare_visual_replacement(
                    account_key, current, payload, render_evidence, poster_render_evidence)
            if expected_row is not None:
                if not prepared_write:
                    # Optional scene columns describe the replaced object. Never
                    # retain that object's aliases on the new original.
                    for field in _DRAFT_SCENE_CAS_COLUMNS:
                        if field in current:
                            payload[field] = None
                required = ("id", "gym_id", *_CORE_VISUAL_MEDIA_CAS_COLUMNS)
                if any(key not in current for key in required):
                    raise PreWriteCASError(422, "swap snapshot incomplete")
                for key in (*_VISUAL_MEDIA_CAS_COLUMNS, "variant_of", "approval_kind", "approved_by", "approved_at", "approval_digest"):
                    if key not in current:
                        continue
                    encoded = _eq_filter(current[key])
                    if encoded is None:
                        raise PreWriteCASError(422, "swap snapshot cannot be encoded")
                    params[key] = encoded
            else:
                params = self._visual_media_cas(current, params)
        r = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=payload, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if prepared_write or expected_row is not None:
            return self._visual_media_result(rows, account_key, current, payload)
        for row in rows:
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    # ---- durable portal action receipts (DRAFT, flag-gated, v3 2026-10-04) ----
    #
    # Backing table + RPCs: public.portal_action_receipt and the three SECURITY
    # DEFINER functions in migrations/portal_action_receipt_draft_20261004.sql
    # (DRAFT, applied by the database operator only; EXECUTE is service_role
    # only and the table itself has NO direct PostgREST write grant at all).
    # Every method here is an RPC wrapper: this client NEVER reads, inserts,
    # updates or patches the receipt table directly. Callers must gate behind
    # portal_social._swap_receipts_enabled() (ECHO_SWAP_ACTION_RECEIPT, default
    # OFF); with the flag off none of these are ever invoked.
    #
    # Frozen RPC contract (see the migration header):
    #   begin(gym, action_id, action, row_uuid, actor, fingerprint)
    #     -- binds the request tuple BEFORE any row read and captures the
    #     -- before_state ITSELF; no caller-provided before_state exists.
    #   claim_selection(gym, action_id, fingerprint, selected_asset,
    #     planned_siblings) -- CAS-freezes the winner's selection and the exact
    #     active member manifest (gym, logical_post_id, variant_status=active).
    #   apply(gym, action_id, fingerprint, prepared) -- ONE transaction that
    #     writes the exact frozen group and persists the terminal receipt.
    #
    # All wrappers fail closed: a non-2xx, an unparseable body, or a returned
    # row whose tenant / action_id / fingerprint / status does not match the
    # request exactly raises ReceiptStoreError (never a guessed outcome).

    _RECEIPT_STATUSES = ("started", "selected", "succeeded", "failed", "uncertain")

    def _receipt_rpc(self, fn, args, account_key, action_id,
                     expect_fingerprint=None, timeout=60):
        """POST one receipt RPC and strictly parse the typed receipt row back."""
        try:
            r = self._client().post(
                self._rest(f"rpc/{fn}"),
                headers=self._headers({"Content-Type": "application/json"}),
                json=args, timeout=timeout)
        except PortalStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failure: unknown outcome
            raise ReceiptStoreError(0, f"rpc {fn} transport: {type(exc).__name__}")
        if r.status_code >= 400:
            code, message = "", ""
            try:
                err = r.json()
                if isinstance(err, dict):
                    code = str(err.get("code") or "")
                    message = str(err.get("message") or "")
            except Exception:  # noqa: BLE001 - fall through to generic below
                pass
            detail = _scrub((message or r.text or "")[:200])
            if code == "23514" and "held for manual review" in message:
                raise ReceiptHoldError(r.status_code, detail)
            if code == "23514":
                raise ReceiptConflictError(r.status_code, detail)
            if code == "22023":
                raise ReceiptSelectionError(r.status_code, detail)
            raise ReceiptStoreError(r.status_code, detail)
        try:
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise ReceiptStoreError(r.status_code,
                                    f"rpc {fn} unparseable response: {type(exc).__name__}")
        if not isinstance(data, dict):
            raise ReceiptStoreError(r.status_code, f"rpc {fn} did not return a receipt row")
        if str(data.get("gym_id") or "") != str(account_key):
            raise ReceiptStoreError(r.status_code, f"rpc {fn} returned a foreign-tenant receipt")
        if str(data.get("action_id") or "") != str(action_id):
            raise ReceiptStoreError(r.status_code, f"rpc {fn} returned a different action_id")
        if data.get("status") not in self._RECEIPT_STATUSES:
            raise ReceiptStoreError(r.status_code, f"rpc {fn} returned an unknown receipt status")
        if expect_fingerprint is not None and \
                str(data.get("request_fingerprint") or "") != str(expect_fingerprint):
            raise ReceiptStoreError(r.status_code, f"rpc {fn} returned a mismatched binding")
        return data

    def action_receipt_begin(self, account_key, action_id, action, row_id,
                             actor_id, fingerprint):
        """Bind (gym, action_id) to the immutable request tuple and return the
        receipt. SQL captures before_state itself; there is no before_state
        argument here by contract. Raises ReceiptConflictError on a conflicting
        reuse, ReceiptHoldError when the row is missing or historical-NULL
        (manual review), ReceiptStoreError on anything unexpected."""
        return self._receipt_rpc(
            "portal_action_receipt_begin",
            {"p_gym_id": str(account_key), "p_action_id": str(action_id),
             "p_action": str(action), "p_row_id": str(row_id),
             "p_actor_id": str(actor_id or ""),
             "p_request_fingerprint": str(fingerprint)},
            account_key, action_id, expect_fingerprint=fingerprint, timeout=30)

    def action_receipt_claim(self, account_key, action_id, fingerprint,
                             selected_asset, planned_siblings):
        """CAS-freeze the selection and exact active member manifest. The
        returned row carries the WINNER's frozen selection (a concurrent loser
        must govern itself by it, never by its own candidate)."""
        return self._receipt_rpc(
            "portal_action_receipt_claim_selection",
            {"p_gym_id": str(account_key), "p_action_id": str(action_id),
             "p_request_fingerprint": str(fingerprint),
             "p_selected_asset": dict(selected_asset or {}),
             "p_planned_siblings": dict(planned_siblings or {})},
            account_key, action_id, expect_fingerprint=fingerprint, timeout=30)

    def action_receipt_apply(self, account_key, action_id, fingerprint, prepared):
        """Atomically write the exact frozen group and persist the terminal
        receipt in the same transaction. Success is ONLY the returned persisted
        terminal receipt; a timeout or lost response is reconciled by replaying
        this same call, never by reading a mutable calendar row."""
        return self._receipt_rpc(
            "portal_action_receipt_apply",
            {"p_gym_id": str(account_key), "p_action_id": str(action_id),
             "p_request_fingerprint": str(fingerprint),
             "p_prepared": dict(prepared or {})},
            account_key, action_id, expect_fingerprint=fingerprint, timeout=60)

    def list_active_logical_post_rows(self, account_key, logical_post_id):
        """Every own-tenant active content_calendar row of one logical post,
        ascending id. This is a READ scoped exactly like the claim RPC's frozen
        member manifest (gym_id + logical_post_id + variant_status=active) -- it
        exists only so a FIRST selection can plan per-row variants; the claim's
        frozen manifest remains the sole authority on the swap group."""
        if not _UUID_RE.match(str(logical_post_id or "")):
            raise PortalStoreError(400, "logical_post_id must be a uuid")
        params = {"gym_id": f"eq.{account_key}",
                  "logical_post_id": f"eq.{logical_post_id}",
                  "variant_status": "eq.active",
                  "order": "id.asc"}
        r = self._client().get(self._rest(_TABLE), params=params,
                               headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not isinstance(rows, list):
            raise PortalStoreError(500, "logical-post row listing was not a list")
        out = []
        for row in rows:
            if not isinstance(row, dict):
                raise PortalStoreError(500, "logical-post row was not an object")
            if str(row.get("gym_id") or "") != str(account_key):
                raise PortalStoreError(500, "logical-post listing leaked a foreign row")
            out.append(row)
        return out

    def list_future_media_maintenance_rows(self, start_iso, end_iso):
        """Complete cross-gym active future book for a maintenance dry run.

        Page by id rather than relying on PostgREST's default 1000-row cap. A
        failed page aborts the whole scan, so no partial book is considered safe.
        """
        rows = []
        last_id = None
        while True:
            params = {"post_date": [f"gte.{start_iso}", f"lte.{end_iso}"],
                      "variant_status": "eq.active",
                      "status": "in.(pending,coach_review,approved,publishing,published)",
                      "order": "id", "limit": "500"}
            if last_id is not None:
                params["id"] = f"gt.{last_id}"
            response = self._client().get(
                self._rest(_TABLE),
                params=params,
                headers=self._headers(), timeout=30)
            if response.status_code >= 400:
                raise PortalStoreError(response.status_code, "maintenance calendar read failed")
            page = response.json()
            if not isinstance(page, list):
                raise PortalStoreError(502, "invalid maintenance calendar page")
            ids = [str(row.get("id") or "") for row in page if isinstance(row, dict)]
            if (len(ids) != len(page) or not all(ids)
                    or any(left >= right for left, right in zip(ids, ids[1:]))
                    or (ids and last_id is not None and ids[0] <= str(last_id))):
                raise PortalStoreError(502, "maintenance calendar pagination stalled")
            rows.extend(page)
            if len(page) < 500:
                return rows
            last_id = page[-1]["id"]
            if len(rows) >= 20000:
                raise PortalStoreError(502, "maintenance calendar exceeds safe read bound")

    def restage_held_media(self, account_key, current, *, image_url=None,
                           source_media_url=None, extra_fields=None, release=False,
                           render_evidence=None, poster_render_evidence=None):
        """Compare-and-swap one Swift held row; stage pixels while retaining its hold.

        Release is a separate CAS after the operator independently reads every staged
        row. This method is deliberately separate from the portal's general swap.
        """
        if (current.get("gym_id") != account_key or current.get("status") != "pending"
                or current.get("media_not_ready_reason") is None
                or not current.get("id") or not current.get("post_date")):
            return None
        def expected(value):
            return "is.null" if value is None else f"eq.{value}"
        params = {"id": expected(current["id"]), "gym_id": expected(account_key),
                  "status": "eq.pending", "post_date": expected(current["post_date"]),
                  "image_url": expected(current.get("image_url")),
                  "source_media_url": expected(current.get("source_media_url")),
                  "source_media_asset_id": expected(current.get("source_media_asset_id")),
                  "media_not_ready_reason": expected(current["media_not_ready_reason"])}
        for key in ("account", "format", "variant_status", "created_at"):
            if key in current:
                params[key] = expected(current[key])
        if release:
            payload = {"media_not_ready_reason": None}
        else:
            if not isinstance(image_url, str) or not image_url.startswith("https://"):
                return None
            payload = {"image_url": image_url, "source_media_url": source_media_url}
            for col in _SWAP_EXTRA_COLUMNS:
                if col in (extra_fields or {}):
                    payload[col] = extra_fields[col]
        from . import visual_writer_prepare
        visual_guard = visual_writer_prepare.enabled()
        prepared_write = not release and visual_guard
        if prepared_write:
            payload = self._prepare_visual_replacement(
                account_key, current, payload, render_evidence, poster_render_evidence)
            params = self._visual_media_cas(current, params)
        elif release and visual_guard:
            if not current.get("visual_group_key") or not current.get("byte_hash"):
                return None
            params = self._visual_media_cas(current, params)
        response = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=payload, timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, _scrub((response.text or "")[:200]))
        rows = response.json() or []
        if prepared_write or (release and visual_guard):
            return self._visual_media_result(rows, account_key, current, payload)
        if len(rows) != 1:
            return None
        row = rows[0]
        if (row.get("id") != current["id"] or row.get("gym_id") != account_key
                or row.get("status") != "pending"
                or row.get("post_date") != current["post_date"]
                or row.get("media_not_ready_reason") != (None if release else current["media_not_ready_reason"])):
            return None
        if not release and any(row.get(key) != value for key, value in payload.items()):
            return None
        return row

    def hold_pending_media(self, account_key, current, reason):
        """Set one pending row's media hold with a complete row compare-and-swap.

        This deliberately changes only ``media_not_ready_reason``.  The caller
        supplies its before image, which is also carried in the predicate so an
        operator hold cannot overwrite a concurrent client edit or approval.
        """
        if (not isinstance(reason, str) or not reason.strip()
                or current.get("gym_id") != account_key
                or current.get("status") != "pending"
                or not current.get("id") or not current.get("post_date")):
            return None

        def expected(value):
            return "is.null" if value is None else f"eq.{value}"

        params = {
            "id": expected(current["id"]), "gym_id": expected(account_key),
            "status": "eq.pending", "post_date": expected(current["post_date"]),
            "image_url": expected(current.get("image_url")),
            "source_media_url": expected(current.get("source_media_url")),
            "source_media_asset_id": expected(current.get("source_media_asset_id")),
            "media_not_ready_reason": expected(current.get("media_not_ready_reason")),
        }
        for key in ("account", "format", "variant_status", "created_at"):
            if key in current:
                params[key] = expected(current[key])
        response = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"media_not_ready_reason": reason}, timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, _scrub((response.text or "")[:200]))
        rows = response.json() or []
        if len(rows) != 1:
            return None
        row = rows[0]
        if (row.get("id") != current["id"] or row.get("gym_id") != account_key
                or row.get("status") != "pending"
                or row.get("post_date") != current["post_date"]
                or row.get("media_not_ready_reason") != reason):
            return None
        return row

    def hold_future_infographic_media(self, account_key, current, reason):
        """Exact row CAS for an active pending or approved infographic placeholder.

        Only media_not_ready_reason changes. Approved stays approved; a concurrent
        client edit, approval, publish claim, media swap, or variant transition
        makes the PATCH match zero rows. No image or caption is rewritten.
        """
        required = ("id", "gym_id", "post_date", "status", "variant_status",
                    "account", "format", "caption", "image_url", "source_media_url",
                    "source_media_asset_id", "media_not_ready_reason", "created_at",
                    "published_at", "late_post_id")
        if (not isinstance(current, dict) or any(key not in current for key in required)
                or current["gym_id"] != account_key
                or current["status"] not in ("pending", "approved")
                or current["variant_status"] != "active"
                or current["published_at"] is not None
                or current["late_post_id"] is not None
                or current["media_not_ready_reason"] is not None
                or not isinstance(reason, str) or not reason.strip()):
            return None

        def expected(value):
            return "is.null" if value is None else f"eq.{value}"

        params = {key: expected(current[key]) for key in required}
        response = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"media_not_ready_reason": reason}, timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, "future infographic hold CAS failed")
        data = response.json()
        if not isinstance(data, list) or len(data) != 1:
            return None
        after = data[0]
        if (after.get("id") != current["id"]
                or any(after.get(key) != current[key] for key in required
                       if key != "media_not_ready_reason")
                or after.get("media_not_ready_reason") != reason):
            return None
        return after

    def hold_repeat_media(self, account_key, current, reason):
        """Repeat-specific exact-row CAS for the nightly cross-date repeat hold.

        Pins id + gym_id + EVERY observed _VISUAL_MEDIA_CAS_COLUMNS field of
        the before image (status, thumbnail_url, byte_hash, r2_key,
        drive_file_id, slot_index, time_slot, source identity, ...). All
        APPLIED core fields must be present; the four draft scene columns are
        pinned only when the row carries them (2026-10-04 live probe: not yet
        migrated). A before image missing any core key is refused (None),
        never a partial predicate. Equality filters use the bare eq.<value>
        form -- the only encoding with live-match evidence; a value whose
        safe encoding is not established raises a precise PortalStoreError
        blocker and the row is left untouched. Also
        requires the publish-safety fields
        (claim / reservation / schedule / publish / late post / existing hold)
        to be NULL, and writes ONLY media_not_ready_reason. A concurrent
        thumbnail/slot/claim mutation makes the predicate match zero rows and
        this returns None -- the row is reported, never corrupted. Returns the
        updated row, or None when the CAS matched no row or the returned row
        disagrees. Never raises on a lost race; a transport/4xx+ failure
        raises PortalStoreError so the caller reports instead of claiming
        success.
        """
        required_null = ("published_at", "late_post_id", "publish_claim_token",
                         "publish_reservation_day", "scheduled_at",
                         "media_not_ready_reason")
        # The predicate must pin the COMPLETE observed before image. Every
        # APPLIED core CAS field must be present; the four draft scene columns
        # (_DRAFT_SCENE_CAS_COLUMNS, not yet migrated to live
        # content_calendar per the 2026-10-04 Zanshin probe) are pinned when
        # the row actually carries them and never required. A core key
        # missing from the before image would silently drop that predicate
        # and could hold a row whose unseen field changed concurrently.
        if (not isinstance(current, dict)
                or str(current.get("gym_id")) != str(account_key)
                or current.get("id") is None
                or current.get("status") not in ("pending", "approved")
                or current.get("variant_status") != "active"
                or any(key not in current for key in _CORE_VISUAL_MEDIA_CAS_COLUMNS)
                or any(current[key] is not None for key in required_null)
                or not isinstance(reason, str) or not reason.strip()):
            return None

        params = {"id": f'eq.{current["id"]}',
                  "gym_id": f"eq.{account_key}"}
        for key in _VISUAL_MEDIA_CAS_COLUMNS:
            if key not in current:
                continue
            # Fail closed BEFORE any write: a value with no evidenced-safe
            # equality encoding (reserved characters or non-
            # scalar) raises a precise blocker; it is never dropped from the
            # predicate and never sent in a weakened form.
            encoded = _eq_filter(current[key])
            if encoded is None:
                raise PortalStoreError(
                    422,
                    "repeat media hold CAS blocked: field "
                    f"{key!r} value has no evidenced-safe PostgREST "
                    "equality encoding (reserved characters or malformed "
                    "value); row left untouched, hold by hand")
            params[key] = encoded
        response = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"media_not_ready_reason": reason}, timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, "repeat media hold CAS failed")
        data = response.json()
        if not isinstance(data, list) or len(data) != 1:
            return None
        after = data[0]
        if (not isinstance(after, dict)
                or after.get("id") != current["id"]
                or any(after.get(key) != current[key]
                       for key in _VISUAL_MEDIA_CAS_COLUMNS
                       if key in current and key != "media_not_ready_reason")
                or after.get("media_not_ready_reason") != reason):
            return None
        return after

    def replace_future_infographic_media(self, account_key, current, *, image_url,
                                         source_media_url, source_media_asset_id,
                                         reason, thumbnail_url=None, render_evidence=None,
                                         poster_render_evidence=None):
        """Replace one receipt-owned placeholder and clear its hold in one exact CAS.

        Unlike a generic portal swap, this preserves approved rows as approved.
        The old approved placeholder never becomes unheld before its replacement.
        The operator owns byte approval, unique-photo reservation and receipt checks.
        """
        import re
        from urllib.parse import urlsplit
        hold_reason = "Photo-first hold: unverified infographic placeholder; approved gym photo required"
        fill = re.compile(r"(?:^|/)igfill_\d{4}-\d{2}-\d{2}(?:[_-]|\.)", re.I)
        def is_fill(row):
            return any(fill.search(urlsplit(str(row.get(key) or "")).path)
                       for key in ("image_url", "source_media_url"))
        required = ("id", "gym_id", "post_date", "status", "variant_status", "account",
                    "format", "caption", "image_url", "source_media_url",
                    "source_media_asset_id", "media_not_ready_reason", "created_at",
                    "published_at", "late_post_id", "thumbnail_url", "publish_claim_token",
                    "publish_reservation_day", "slot_index", "scheduled_at")
        if (not isinstance(current, dict) or any(key not in current for key in required)
                or current["gym_id"] != account_key
                or current["status"] not in ("pending", "approved")
                or current["variant_status"] != "active"
                or current["published_at"] is not None
                or current["late_post_id"] is not None
                or current["publish_claim_token"] is not None
                or current["publish_reservation_day"] is not None
                or reason != hold_reason or current["media_not_ready_reason"] != hold_reason
                or not is_fill(current)
                or not isinstance(image_url, str) or not image_url.startswith("https://")
                or not isinstance(source_media_asset_id, str) or not source_media_asset_id.strip()
                or (source_media_url is not None and
                    (not isinstance(source_media_url, str)
                     or not source_media_url.startswith("https://")))
                or is_fill({"image_url": image_url, "source_media_url": source_media_url})):
            return None
        params = {key: "is.null" if current[key] is None else f"eq.{current[key]}"
                  for key in required}
        payload = {"image_url": image_url, "source_media_url": source_media_url,
                   "source_media_asset_id": source_media_asset_id,
                   "thumbnail_url": thumbnail_url, "media_not_ready_reason": None}
        from . import visual_writer_prepare
        prepared_write = visual_writer_prepare.enabled()
        if prepared_write:
            payload = self._prepare_visual_replacement(
                account_key, current, payload, render_evidence, poster_render_evidence)
            params = self._visual_media_cas(current, params)
        response = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=payload, timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, "future infographic replacement CAS failed")
        data = response.json()
        if prepared_write:
            return self._visual_media_result(data, account_key, current, payload)
        if not isinstance(data, list) or len(data) != 1:
            return None
        after = data[0]
        expected = {**current, **payload}
        if any(after.get(key) != expected[key] for key in required):
            return None
        return after

    def list_photo_restage_book(self, account_key):
        """Read every retained gym row for a permanent no-repeat operator decision.

        Include archived, denied and future rows; unknown/partial reads must abort.
        """
        rows, last_id = [], None
        while True:
            params = {"gym_id": f"eq.{account_key}", "order": "id", "limit": "500"}
            if last_id is not None:
                params["id"] = f"gt.{last_id}"
            response = self._client().get(self._rest(_TABLE), params=params,
                                          headers=self._headers(), timeout=30)
            if response.status_code >= 400:
                raise PortalStoreError(response.status_code, "photo restage book unavailable")
            page = response.json()
            if not isinstance(page, list):
                raise PortalStoreError(502, "invalid photo restage book")
            ids = [str(row.get("id") or "") for row in page if isinstance(row, dict)]
            if (len(ids) != len(page) or not all(ids)
                    or any(row.get("gym_id") != account_key for row in page)
                    or any(a >= b for a, b in zip(ids, ids[1:]))
                    or (ids and last_id is not None and ids[0] <= str(last_id))):
                raise PortalStoreError(502, "photo restage book pagination stalled")
            rows.extend(page)
            if len(page) < 500:
                return rows
            last_id = page[-1]["id"]
            if len(rows) >= 20000:
                raise PortalStoreError(502, "photo restage book exceeds safe read bound")

    def release_future_infographic_media(self, account_key, current, reason):
        """Clear one receipt-owned hold by exact CAS, preserving approved state.

        The caller verifies the original private hold receipt before calling.
        This method accepts only an active unpublished row with the exact hold
        reason and changes no field except media_not_ready_reason.
        """
        required = ("id", "gym_id", "post_date", "status", "variant_status",
                    "account", "format", "caption", "image_url", "source_media_url",
                    "source_media_asset_id", "media_not_ready_reason", "created_at",
                    "published_at", "late_post_id")
        if (not isinstance(current, dict) or any(key not in current for key in required)
                or current["gym_id"] != account_key
                or current["status"] not in ("pending", "approved")
                or current["variant_status"] != "active"
                or current["published_at"] is not None
                or current["late_post_id"] is not None
                or not isinstance(reason, str) or not reason.strip()
                or current["media_not_ready_reason"] != reason):
            return None

        def expected(value):
            return "is.null" if value is None else f"eq.{value}"

        params = {key: expected(current[key]) for key in required}
        response = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"media_not_ready_reason": None}, timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, "future infographic release CAS failed")
        data = response.json()
        if not isinstance(data, list) or len(data) != 1:
            return None
        after = data[0]
        if (after.get("id") != current["id"]
                or any(after.get(key) != current[key] for key in required
                       if key != "media_not_ready_reason")
                or after.get("media_not_ready_reason") is not None):
            return None
        return after

    def archive_pending_media(self, account_key, current):
        """CAS one historical pending card from active to archived.

        Archiving is an audit-preserving variant transition, not a status action:
        it changes *only* ``variant_status``. The full relevant before image is
        carried in the server-side predicate, so an approval, media/source edit,
        sibling-variant change, or tenant mismatch makes this a no-op rather than
        overwriting somebody else's decision.
        """
        if (current.get("gym_id") != account_key or current.get("status") != "pending"
                or current.get("variant_status", "active") != "active"
                or current.get("published_at") is not None
                or current.get("late_post_id") is not None
                or not current.get("id") or not current.get("post_date")):
            return None

        def expected(value):
            return "is.null" if value is None else f"eq.{value}"

        params = {
            "id": expected(current["id"]), "gym_id": expected(account_key),
            "status": "eq.pending", "variant_status": "eq.active",
            "post_date": expected(current["post_date"]),
            "caption": expected(current.get("caption")),
            "image_url": expected(current.get("image_url")),
            "source_media_url": expected(current.get("source_media_url")),
            "source_media_asset_id": expected(current.get("source_media_asset_id")),
            "media_not_ready_reason": expected(current.get("media_not_ready_reason")),
            "variant_of": expected(current.get("variant_of")),
            "published_at": expected(current.get("published_at")),
            "late_post_id": expected(current.get("late_post_id")),
            "scheduled_at": expected(current.get("scheduled_at")),
            "slot_index": expected(current.get("slot_index")),
        }
        for key in ("account", "format", "created_at"):
            if key in current:
                params[key] = expected(current[key])
        response = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"variant_status": "archived"}, timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, _scrub((response.text or "")[:200]))
        rows = response.json() or []
        if len(rows) != 1:
            return None
        row = rows[0]
        preserved = ("id", "gym_id", "post_date", "account", "format", "status", "caption",
                     "image_url", "source_media_url", "source_media_asset_id",
                     "media_not_ready_reason", "variant_of", "created_at", "published_at",
                     "late_post_id", "scheduled_at", "slot_index")
        if (row.get("variant_status") != "archived"
                or any(row.get(key) != current.get(key) for key in preserved)):
            return None
        return row

    def list_pending_media_between(self, account_key, first, last):
        """Read every pending row in a bounded date window, including variants.

        The exact count check makes a truncated PostgREST page a hard failure.
        This is intentionally separate from list_month's active-variant view.
        """
        response = self._client().get(
            self._rest(_TABLE),
            params={"gym_id": f"eq.{account_key}", "status": "eq.pending",
                    "post_date": [f"gte.{first}", f"lte.{last}"],
                    "select": "*", "limit": "1000", "order": "post_date,id"},
            headers=self._headers({"Prefer": "count=exact"}), timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, _scrub((response.text or "")[:200]))
        rows = response.json()
        total = (getattr(response, "headers", {}) or {}).get("Content-Range", "").rsplit("/", 1)[-1]
        if not isinstance(rows, list) or not total.isdigit() or int(total) != len(rows):
            raise ValueError("pending media read incomplete")
        if any(not isinstance(row, dict) or row.get("gym_id") != account_key
               or row.get("status") != "pending"
               or not first <= str(row.get("post_date") or "")[:10] <= last
               for row in rows):
            raise ValueError("pending media read scope mismatch")
        return rows

    def active_rows_on_day_complete(self, account_key, day):
        """Exact-count read of every active status for a caption/Story swap.

        The general forward-book read omits draft and queued rows. A stale
        paired Story in either state must still receive a media hold before
        its feed caption changes.
        """
        response = self._client().get(
            self._rest(_TABLE),
            params={"gym_id": f"eq.{account_key}", "post_date": f"eq.{day}",
                    "variant_status": "eq.active", "select": "*",
                    "limit": "1000", "order": "id"},
            headers=self._headers({"Prefer": "count=exact"}), timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code,
                                   _scrub((response.text or "")[:200]))
        rows = response.json()
        total = (getattr(response, "headers", {}) or {}).get(
            "Content-Range", "").rsplit("/", 1)[-1]
        if (not isinstance(rows, list) or not total.isdigit()
                or int(total) != len(rows)
                or any(not isinstance(row, dict)
                       or row.get("gym_id") != account_key
                       or str(row.get("post_date") or "")[:10] != day
                       or row.get("variant_status") != "active" for row in rows)):
            raise ValueError("paired Story read incomplete")
        return rows

    # ---- variant pairing (0318): v2 creative candidates -----------------------
    # A "logical post" can have MORE THAN ONE content_calendar row once this
    # ships: exactly one 'active' row (the live/publishing creative) plus zero
    # or more 'candidate' rows (alternate not-yet-picked creative, e.g. an
    # Astra v2 regen) and 'archived' rows (superseded actives / rejected
    # candidates, kept for audit, never deleted). Group membership for row R
    # is coalesce(R.variant_of, R.id) -- see the migration's header comment.

    def get_variant_group(self, account_key, row_id):
        """The full variant group (active + candidates, NOT archived) for the
        logical post `row_id` belongs to, gym-scoped. `row_id` may be the
        anchor (original) row OR any candidate/active row in the group --
        the anchor is resolved from whichever row is fetched first. Returns
        [] when the row does not exist / belongs to another gym. The list is
        NOT itself the 'one row per post' read path (list_month is); this is
        the review-surface read that WANTS to see every candidate."""
        seed = self.get_row(account_key, row_id)
        if seed is None:
            return []
        anchor = seed.get("variant_of") or seed.get("id")
        r = self._client().get(
            self._rest(_TABLE),
            params={
                "gym_id": f"eq.{account_key}",
                "or": f"(id.eq.{anchor},variant_of.eq.{anchor})",
                "variant_status": "in.(active,candidate)",
                "order": "variant_status.desc,created_at",
            },
            headers=self._headers(), timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return [row for row in (r.json() or [])
                if str(row.get("gym_id")) == str(account_key)]

    def create_variant_candidate(self, account_key, anchor_row, image_url,
                                 caption=None, thumbnail_url=None,
                                 source_media_asset_id=None, prompt_used=None, *,
                                 source_media_url=None, render_evidence=None,
                                 poster_render_evidence=None):
        """INSERT a new 'candidate' row linked to `anchor_row` (a dict, the row the
        candidate is an alternate FOR). Copies the slot identity (post_date,
        account, format, pillar, gbp_* fields) so the candidate is a genuine
        alternate for the SAME logical post, never a floating duplicate. status
        is always 'pending' (a candidate is never pre-approved by existing) --
        it must clear the same review gate as any post once/if it becomes
        active. variant_of is the ANCHOR's own id: if `anchor_row` is itself
        already a candidate/archived member of a group, its OWN variant_of
        (never re-derived) is used so every candidate in a group points at the
        same stable anchor. gym_id is always account_key (never trusted from
        the caller-supplied anchor_row) -- this is the ownership guarantee.
        Returns the inserted row."""
        anchor_id = anchor_row.get("variant_of") or anchor_row.get("id")
        payload = {
            "gym_id": account_key,
            "account": anchor_row.get("account"),
            "post_date": anchor_row.get("post_date"),
            "format": anchor_row.get("format"),
            "pillar": anchor_row.get("pillar"),
            "caption": caption if caption is not None else anchor_row.get("caption"),
            "image_url": image_url,
            "status": "pending",
            "variant_of": anchor_id,
            "variant_status": "candidate",
        }
        # Identity inheritance depends on the rollout flag. OFF (default): the
        # generic store keeps honoring the caller-supplied anchor snapshot (with a
        # loud rejection of a malformed id), as it always has. ON: the caller's
        # anchor_row is only a SNAPSHOT -- the candidate's logical_post_id is taken
        # from the registered anchor row fetched in THIS tenant below (never from
        # the caller), and any identity the caller asserted that does not match the
        # registered row rejects the write before INSERT.
        logical_write = config.logical_post_id_enabled()
        if not logical_write and anchor_row.get("logical_post_id") is not None:
            import uuid as _uuid
            try:
                payload["logical_post_id"] = str(
                    _uuid.UUID(str(anchor_row["logical_post_id"])))
            except (ValueError, AttributeError, TypeError):
                raise ValueError(
                    "anchor logical_post_id must be a UUID; got "
                    f"{anchor_row['logical_post_id']!r}")
        if thumbnail_url is not None:
            payload["thumbnail_url"] = thumbnail_url
        if source_media_asset_id is not None:
            payload["source_media_asset_id"] = source_media_asset_id
        from . import visual_writer_prepare
        prepared_write = visual_writer_prepare.enabled()
        if prepared_write or logical_write:
            def _anchor_reject(detail):
                if prepared_write:
                    raise visual_writer_prepare.VisualPreparationError(detail)
                raise PortalStoreError(404, detail)

            if str(anchor_row.get("gym_id")) != str(account_key) or not anchor_id:
                _anchor_reject(
                    "variant anchor is not registered to the calendar tenant")
            registered_anchor = self.get_row(account_key, anchor_row.get("id"))
            if (registered_anchor is None
                    or str(registered_anchor.get("gym_id")) != str(account_key)
                    or str(registered_anchor.get("id")) != str(anchor_row.get("id"))):
                _anchor_reject(
                    "variant anchor is not registered to the calendar tenant")
            compared_keys = ["account", "post_date", "format", "pillar", "caption",
                             "variant_of"]
            if logical_write:
                # ON, the anchor's logical identity is part of the snapshot: a
                # caller asserting an id the registered row does not carry (a
                # forged or stale identity) is an anchor-change, rejected here.
                compared_keys.append("logical_post_id")
            if any(registered_anchor.get(key) != anchor_row.get(key) for key in
                   compared_keys):
                _anchor_reject("variant anchor changed before visual preparation"
                               if prepared_write else
                               "variant anchor changed before variant creation")
            if str(anchor_id) != str(registered_anchor["id"]):
                group_anchor = self.get_row(account_key, anchor_id)
                if (group_anchor is None
                        or str(group_anchor.get("gym_id")) != str(account_key)
                        or str(group_anchor.get("id")) != str(anchor_id)):
                    _anchor_reject(
                        "variant anchor is not registered to the calendar tenant")
            if logical_write:
                # The candidate is an alternate creative for the SAME logical post:
                # its id is the REGISTERED anchor's trusted id (never the caller's,
                # never re-minted). A historical anchor (NULL) leaves the
                # candidate NULL -- the caller cannot inject identity into it.
                trusted_id = registered_anchor.get("logical_post_id")
                if trusted_id is None:
                    payload.pop("logical_post_id", None)
                else:
                    import uuid as _uuid
                    try:
                        payload["logical_post_id"] = str(
                            _uuid.UUID(str(trusted_id)))
                    except (ValueError, AttributeError, TypeError):
                        raise ValueError(
                            "registered anchor logical_post_id must be a UUID; got "
                            f"{trusted_id!r}")
            if prepared_write:
                if source_media_url is not None:
                    payload["source_media_url"] = source_media_url
                payload = self._prepare_visual_row(
                    account_key, payload, render_evidence, poster_render_evidence)
        r = self._client().post(
            self._rest(_TABLE),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=[payload], timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if prepared_write:
            if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                    or not rows[0].get("id")
                    or any(key not in rows[0] or rows[0][key] != value
                           for key, value in payload.items())):
                return None
        return rows[0] if rows else None

    def swap_variant(self, account_key, candidate_id, actor=""):
        """THE atomic pick: promote `candidate_id` to 'active' for its group,
        archiving the previously-active row and every other candidate in the
        SAME transaction (content_calendar_swap_variant, migration 0318).
        Calls the Postgres function via PostgREST rpc/ rather than issuing
        the reads+writes from here, because the atomicity guarantee (no
        window where two rows are both active, no window a publisher could
        observe an inconsistent group) requires ONE transaction with the
        whole group row-locked -- something a sequence of separate PostgREST
        calls from Python cannot provide. Returns the function's jsonb result
        dict: {"ok": true, "active_id": ..., ...} or {"ok": false, "error":
        one of "not_found"/"not_a_candidate"/"published_final"}."""
        r = self._client().post(
            self._rest("rpc/content_calendar_swap_variant"),
            headers=self._headers({"Content-Type": "application/json"}),
            json={"p_gym_id": account_key, "p_candidate_id": candidate_id,
                 "p_actor": (actor or None)},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or {"ok": False, "error": "empty_response"}

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
            from .copy_gate import format_caption
            fields["caption"] = format_caption(new_caption)
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
        from .copy_gate import format_caption
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": "not.in.(publishing,published)",
            "variant_status": "eq.active",
        }
        r = self._client().patch(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json={"caption": format_caption(new_caption), "status": "pending"},
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
        from .copy_gate import format_caption
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
            json={"caption": format_caption(new_caption)},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    def patch_caption_for_meta_sweep(self, account_key, row_id, new_caption, *,
                                    expected_status, expected_caption):
        """Clean a caption while invalidating its approval proof atomically.

        The status allowlist excludes rows already claimed or published. Pending
        rows remain pending; approved rows return to pending for fresh human
        approval. A schema with durable approval proof must provide these columns.
        """
        expected = str(expected_status or "").strip().lower()
        if expected not in ("pending", "approved", "draft", "queued", "failed"):
            return None
        from .copy_gate import format_caption
        caption_filter = _eq_filter(expected_caption)
        if caption_filter is None:
            raise PortalStoreError(
                422,
                "caption meta sweep CAS blocked: caption has no evidenced-safe "
                "PostgREST equality encoding; row left untouched")
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": f"eq.{expected}",
            # Exact observed creative CAS. A status-only guard would permit a
            # stale sweep read to overwrite a concurrent same-status human edit.
            "caption": caption_filter,
            "published_at": "is.null",
            "late_post_id": "is.null",
            "variant_status": "eq.active",
        }
        payload = {
            "caption": format_caption(new_caption),
            "approval_kind": None,
            "approved_by": None,
            "approved_at": None,
            "approval_digest": None,
        }
        if expected == "approved":
            payload["status"] = "pending"
        r = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }), json=payload, timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        for row in (r.json() or []):
            if str(row.get("gym_id")) == str(account_key):
                return row
        return None

    def patch_caption_for_gbp_auto_cleanup(self, account_key, row_id, new_caption, *,
                                          expected_caption):
        """Clean an Auto GBP caption after releasing its publish claim.

        The security-definer RPC locks the gym autonomy setting and compares the
        exact caption and approved status in the same transaction. A flip to
        Manual therefore prevents the cleanup write; the caller holds the row.
        """
        from .copy_gate import format_caption
        clean = format_caption(new_caption)
        if not clean or clean == expected_caption:
            return None
        r = self._client().post(
            self._rest("rpc/calendar_patch_caption_autonomous_clean"),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"p_row_id": row_id, "p_gym_id": account_key,
                  "p_expected_status": "approved",
                  "p_expected_caption": expected_caption,
                  "p_clean_caption": clean}, timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not isinstance(rows, list):
            raise PortalStoreError(502, "GBP Auto caption cleanup returned an invalid result")
        if not rows:
            return None
        if len(rows) != 1:
            raise PortalStoreError(502, "GBP Auto caption cleanup matched multiple rows")
        row = rows[0]
        if (not isinstance(row, dict)
                or str(row.get("gym_id")) != str(account_key)
                or str(row.get("id")) != str(row_id)
                or row.get("account") != "googlebusiness"
                or row.get("status") != "approved"
                or row.get("caption") != clean
                or row.get("publish_claim_token") is not None):
            raise PortalStoreError(502, "GBP Auto caption cleanup returned an inconsistent row")
        return row

    def patch_caption_for_hashtag_backfill(self, account_key, row_id, new_caption,
                                           *, expected_status):
        """Atomically patch a safe future IG row during the one-off hashtag backfill.

        ``expected_status`` is intentionally a positive allowlist. A pending row
        stays pending; an approved row becomes pending because its client-visible
        copy changed and requires a fresh approval. The REST filters make a stale
        read harmless if another worker has claimed, published, denied, killed,
        failed, or otherwise changed the row before this request reaches Supabase.
        """
        expected = str(expected_status or "").strip().lower()
        if expected not in ("pending", "approved"):
            return None
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": f"eq.{expected}",
            "published_at": "is.null",
            "late_post_id": "is.null",
            "variant_status": "eq.active",
        }
        from .copy_gate import format_caption
        payload = {"caption": format_caption(new_caption)}
        if expected == "approved":
            payload["status"] = "pending"
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
            # Needs-media holds are never due: the reason column is set on a staged
            # hold (and blank image_url strings slip past not.is.null, so the
            # client-side guard below drops those too).
            "media_not_ready_reason": "is.null",
            "account": "in.(instagram,facebook)",
            # 0318: never let a candidate variant (an unchosen Astra v2 sitting
            # beside its slot's real active row) get claimed and published --
            # only the active row for a slot is ever eligible.
            "variant_status": "eq.active",
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
        rows = r.json() or []
        # Fail-closed client-side guard: never serve a row with blank/whitespace
        # media or a media-not-ready reason, whatever the filters missed.
        return [row for row in rows
                if (row.get("image_url") or "").strip()
                and not (row.get("media_not_ready_reason") or "").strip()]

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
        # MEDIA HOLD GUARD: a needs-media or blank-media row is never claimable,
        # even when a stale status says pending/approved. The atomic PATCH carries
        # media_not_ready_reason=is.null so the refusal is enforced server-side in
        # the same statement; the prefetch refuses a blank image_url (PostgREST has
        # no non-empty-string operator) before the claim is attempted.
        try:
            cur = self._client().get(
                self._rest(_TABLE),
                params={"id": f"eq.{row_id}",
                        "select": "id,image_url,media_not_ready_reason"},
                headers=self._headers(), timeout=30)
            if cur.status_code >= 400:
                return False
            rows0 = cur.json() or []
            if len(rows0) != 1:
                return False
            row0 = rows0[0]
            if (not (row0.get("image_url") or "").strip()
                    or (row0.get("media_not_ready_reason") or "").strip()):
                return False
        except Exception:
            return False  # fail closed: an unreadable row is never claimed
        params = {
            "id": f"eq.{row_id}",
            "status": "in.(pending,approved)",
            # Defence in depth: due_rows already filters active variants, but a
            # direct/stale caller must never claim an archived audit row.
            "variant_status": "eq.active",
            "published_at": "is.null",
            "image_url": "not.is.null",
            "media_not_ready_reason": "is.null",
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

    def claim_publish_slot(self, row_id, gym_id, local_day, timezone_name,
                           capacity, approved_only, require_proof=False):
        """Atomically reserve a platform slot and return this claim's UUID token.

        No split count/claim fallback: an unavailable RPC holds the post. The SQL
        function serializes all workers for this gym with an advisory lock. A
        distinct token on each successful claim prevents a stale worker from
        reverting a later worker's claim of the same row.

        APPROVAL PROOF GATE (draft, 2026-10-05): ``require_proof`` asks the RPC
        to re-read the gym's CURRENT autonomy from the DB inside the claim
        transaction and, unless the gym is definitively autonomous right now,
        atomically reject a row lacking a fresh VERIFIED human approval
        (approval_kind='human' + nonempty trusted approved_by, stamped only by
        the portal's calendar_stamp_verified_approval) whose canonical digest
        -- including the FINAL image_url -- matches the row's current
        publish-relevant fields. An
        Auto->Manual flip is therefore enforced at the claim itself, not from
        this worker's earlier snapshot. The parameter is sent ONLY when True so
        a pre-provenance migration keeps today's behavior; when True and the
        migration is unapplied the RPC errors and the claim fails closed
        (nothing is published).
        """
        payload = {"p_row_id": row_id, "p_gym_id": gym_id,
                   "p_day": local_day, "p_timezone": timezone_name,
                   "p_capacity": capacity, "p_approved_only": approved_only}
        if require_proof:
            rpc = "rpc/claim_calendar_publish_slot_proven_owned"
        else:
            rpc = "rpc/claim_calendar_publish_slot_owned"
        r = self._client().post(
            self._rest(rpc),
            headers=self._headers({"Content-Type": "application/json"}),
            json=payload,
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        token = r.json()
        if token is None:
            return None
        if require_proof:
            # The gated RPC returns the exact row read under its claim lock.
            # Any malformed result holds the claim and must never become an
            # outbound payload assembled from a stale prefetched row.
            if not isinstance(token, dict) or not isinstance(token.get("row"), dict) \
                    or type(token.get("autonomous_at_claim")) is not bool:
                raise PortalStoreError(502, "calendar claim returned invalid locked creative")
            claimed = token["row"]
            required = ("id", "gym_id", "status", "publish_claim_token",
                        "account", "format", "post_date", "caption", "image_url")
            # These fields are optional in supported legacy schemas; to_jsonb
            # omits them when the underlying columns do not exist.
            claimed.setdefault("source_media_asset_id", None)
            claimed.setdefault("source_media_url", None)
            if (any(k not in claimed for k in required)
                    or str(claimed["id"]) != str(row_id)
                    or claimed["gym_id"] != gym_id
                    or claimed["status"] != "publishing"
                    or not str(claimed["image_url"] or "").strip()):
                raise PortalStoreError(502, "calendar claim returned invalid locked creative")
            try:
                from uuid import UUID
                claimed["publish_claim_token"] = str(UUID(str(claimed["publish_claim_token"])))
            except (TypeError, ValueError, AttributeError):
                raise PortalStoreError(502, "calendar claim returned invalid ownership token")
            # The locked row is to_jsonb(content_calendar). Production schemas
            # without the optional byte_hash column omit its key altogether;
            # retain a real value when the column is present. The publisher
            # still requires this normalized key in its locked creative check.
            claimed.setdefault("byte_hash", None)
            claimed["autonomous_at_claim"] = token["autonomous_at_claim"]
            return claimed
        try:
            from uuid import UUID
            return str(UUID(str(token)))
        except (TypeError, ValueError, AttributeError):
            raise PortalStoreError(502, "calendar claim returned an invalid ownership token")

    def patch_caption_autonomous_clean(self, gym_id, row_id, expected_status,
                                       expected_caption, clean_caption):
        """Atomically clean a prefetched caption only while the gym is Auto.

        The RPC checks current DB autonomy and exact status/caption, then clears
        approval proof in the same transaction. An Auto-to-Manual race is held
        by this RPC or by the subsequent proof-gated publish claim.
        """
        payload = {"p_row_id": row_id, "p_gym_id": gym_id,
                   "p_expected_status": expected_status,
                   "p_expected_caption": expected_caption,
                   "p_clean_caption": clean_caption}
        r = self._client().post(
            self._rest("rpc/calendar_patch_caption_autonomous_clean"),
            headers=self._headers({"Content-Type": "application/json"}),
            json=payload, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not isinstance(rows, list) or len(rows) > 1:
            raise PortalStoreError(502, "autonomous caption cleanup returned invalid rows")
        return rows[0] if rows and isinstance(rows[0], dict) \
            and str(rows[0].get("id")) == str(row_id) \
            and rows[0].get("gym_id") == gym_id \
            and rows[0].get("caption") == clean_caption else None

    def patch_caption_manual_format(self, gym_id, row_id, expected_status,
                                    expected_caption, clean_caption):
        """Format a due caption only while the gym is currently Manual.

        The RPC locks current gym mode and exact row state, clears approval
        provenance, and demotes approved rows to pending. This ensures a
        publish-boundary wording change cannot keep stale human approval.
        A null result means the row/mode did not match and must be rechecked
        through the separate Auto-only cleanup RPC or held.
        """
        payload = {"p_row_id": row_id, "p_gym_id": gym_id,
                   "p_expected_status": expected_status,
                   "p_expected_caption": expected_caption,
                   "p_clean_caption": clean_caption}
        r = self._client().post(
            self._rest("rpc/calendar_patch_caption_manual_format"),
            headers=self._headers({"Content-Type": "application/json"}),
            json=payload, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not isinstance(rows, list) or len(rows) > 1:
            raise PortalStoreError(502, "manual caption formatting returned invalid rows")
        return rows[0] if rows and isinstance(rows[0], dict) \
            and str(rows[0].get("id")) == str(row_id) \
            and rows[0].get("gym_id") == gym_id \
            and rows[0].get("caption") == clean_caption \
            and rows[0].get("status") == "pending" else None

    def patch_post_date(self, row_id, new_post_date):
        """RE-DATE one waiting row (expired-row self-heal, Blake 2026-08-31: no human
        should have to re-date dead posts). Moves post_date forward and CLEARS
        scheduled_at so the publish lane re-stamps the new slot time. Race-guarded:
        only a row still waiting (pending/approved, never published) may move — a row
        mid-claim or already live is refused (zero rows -> None). In approval-proof
        mode, changing the date invalidates proof atomically and demotes approved rows
        to pending; with the flag off, legacy status behavior is preserved."""
        require_proof = config.approval_proof_enabled()
        before = self._client().get(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}",
                    "status": "in.(pending,approved)",
                    "published_at": "is.null",
                    "select": "id,gym_id,post_date,caption,status"},
            headers=self._headers(), timeout=30)
        if before.status_code >= 400:
            raise PortalStoreError(before.status_code,
                                   _scrub((before.text or "")[:200]))
        current = before.json() or []
        if len(current) != 1:
            return None
        old = current[0]
        old_post_date = str(old.get("post_date") or "")[:10]
        status_filter = str(old.get("status") or "").strip().lower()
        if status_filter not in ("pending", "approved"):
            return None

        payload = {"post_date": new_post_date, "scheduled_at": None}
        if require_proof:
            # The date is part of the approval digest. A self-heal re-date must
            # not leave a proof that still authorizes the previous date.
            payload.update({
                "approval_kind": None,
                "approved_by": None,
                "approved_at": None,
                "approval_digest": None,
            })
            if status_filter == "approved":
                payload["status"] = "pending"

        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}",
                    "status": f"eq.{status_filter}",
                    "published_at": "is.null",
                    "post_date": f"eq.{old_post_date}"},
            headers=self._headers({
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }),
            json=payload,
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not rows:
            return None
        if config.caption_cooldown_enabled():
            try:
                from . import caption_ledger
                # A caption/date stamp is shared by the IG feed, FB mirror and
                # paired story, and it may also represent a real historical use.
                # Move the old stamp only after the LAST matching row leaves that
                # date.  Otherwise re-dating one sibling would erase duplicate
                # evidence owned by another row.  A failed evidence read preserves
                # the old stamp (safe hold) rather than weakening the guard.
                peers = self._client().get(
                    self._rest(_TABLE),
                    params={"gym_id": f"eq.{old.get('gym_id')}",
                            "post_date": f"eq.{old_post_date}",
                            "id": f"neq.{row_id}",
                            "select": "caption"},
                    headers=self._headers(), timeout=30)
                if peers.status_code >= 400:
                    raise PortalStoreError(
                        peers.status_code, _scrub((peers.text or "")[:200]))
                other_captions = [str(x.get("caption") or "")
                                  for x in (peers.json() or [])]
                caption = str(old.get("caption") or "")
                fuzzy = caption_ledger.caption_hash(caption)
                verbatim = caption_ledger.verbatim_hash(caption)
                caption_ledger.move_staged_date(
                    str(old.get("gym_id") or ""), caption,
                    old_post_date, str(new_post_date)[:10],
                    preserve_old_fuzzy=any(
                        caption_ledger.caption_hash(c) == fuzzy
                        for c in other_captions),
                    preserve_old_verbatim=bool(verbatim) and any(
                        caption_ledger.verbatim_hash(c) == verbatim
                        for c in other_captions))
            except Exception:
                pass
        return rows[0]

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

    def social_baseline_posts_per_week(self, account_key):
        """The gym's PRE-ECHO posting cadence, read from the SHARED plane.

        social_baseline is written by the Apify capture (agent/social_baseline.py) over
        PostGREST and is keyed by the ACCOUNT KEY (gym_id is text here: 'eng', 'topfuel'),
        not the gyms UUID. Returns (posts_per_week, captured_at) or (None, None).

        Why this exists: Part D's before/after reads the baseline through
        db.get_baseline_posts_per_week, which only ever consulted the worker's LOCAL
        SQLite -- and the only writer of that column is a test helper. So every gym's
        measured baseline sat in Supabase while the portal read an empty local table and
        rendered "capturing your baseline" forever, and every before/after pair came back
        {before: null} so the cards said "with Echo so far" instead of the real
        "Before Echo -> With Echo" story. The intake-web service has no volume at all,
        so a SQLite-only write could never have fixed it there; the shared plane is the
        only store both services can see. Same reasoning as _shared_profile_id.
        Read-only.
        """
        r = self._client().get(
            self._rest("social_baseline"),
            params={
                "gym_id": f"eq.{account_key}",
                "select": "captured_at,measures",
                "order": "captured_at.desc",
                "limit": "1",
            },
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        if not rows:
            return None, None
        measures = rows[0].get("measures") or {}
        ppw = measures.get("posts_per_week")
        try:
            ppw = float(ppw) if ppw is not None else None
        except (TypeError, ValueError):
            ppw = None
        return ppw, rows[0].get("captured_at")

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
            # 0318: a candidate row must never set the "before Echo" reference
            # date — it is an unchosen alternate, not a real planned/published post.
            "variant_status": "eq.active",
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
                                  mark_ever_connected=False, late_account_id=None):
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
        # AUD-005: stamp the Zernio account id on the SOURCE OF TRUTH row. This is the one
        # thing the legacy gym_social_accounts table still carried that this table did not,
        # and the only reason anything had to read two disagreeing pictures of the same
        # fact. Omitted (not written as null) when we do not have one, so a transient read
        # that could not resolve the id never erases a good one already stamped.
        if late_account_id and not type(self)._late_account_id_column_absent:
            body["late_account_id"] = str(late_account_id)
        # Stamp first_connected_at only for a genuinely-connected platform that has none yet;
        # never overwrite an existing original connect time (omitted -> merge-duplicates
        # leaves it untouched).
        if mark_ever_connected and not had_first:
            body["first_connected_at"] = now_iso
        def _write(payload):
            return self._client().post(
                self._rest("echo_social_connections"),
                params={"on_conflict": "gym_id,platform"},
                headers=self._headers({
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=representation",
                }),
                json=payload,
                timeout=30,
            )

        r = _write(body)
        # DEGRADE, DO NOT BREAK, when late_account_id is not deployed yet. This writer runs
        # on the 6h reverify sweep, which is the ONLY thing keeping the connection cache
        # true. Migration social_metrics_daily_provenance_20260905 adds the column, but code
        # and migrations do not land in the same instant, and this repo has been burned
        # before by writing a column that did not exist (the ever_connected regression, and
        # the echo_gym_settings.zernio_profile_id phantom). If the column is missing, the
        # unstamped write still has to land: a stale connection cache is a client ticket per
        # gym. Retried ONCE, without the new field, and remembered for this process so the
        # fleet does not pay a doubled request per gym per sweep.
        if (r.status_code >= 400 and "late_account_id" in body
                and _column_missing(r, "late_account_id")):
            type(self)._late_account_id_column_absent = True
            body.pop("late_account_id", None)
            r = _write(body)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if rows else None

    def social_connection_rows(self, state="connected"):
        """READ-ONLY: every echo_social_connections row, optionally filtered to one state.

        This is the AUD-005 source of truth for who is connected on what. Returns raw rows
        [{gym_id, platform, state, handle, late_account_id, last_verified_at}]. Fleet-wide
        (not gym-scoped) on purpose: the daily metrics pull is a fleet sweep, and paging it
        per gym would turn one read into one per gym.

        PAGED EXPLICITLY: PostgREST caps a response at 1000 rows and does so SILENTLY, so a
        single unpaged read would quietly truncate the fleet the day it grew past the cap.
        """
        out = []
        offset, page = 0, 1000
        while True:
            params = {"select": "gym_id,platform,state,handle,late_account_id,"
                                "last_verified_at",
                      "limit": str(page), "offset": str(offset),
                      "order": "gym_id.asc"}
            if state:
                params["state"] = f"eq.{state}"
            r = self._client().get(self._rest("echo_social_connections"), params=params,
                                   headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
            rows = r.json() or []
            out.extend(rows)
            if len(rows) < page:
                return out
            offset += page

    def upsert_social_metric_days(self, rows):
        """Write daily metric rows into gym_social_metrics_daily. Returns the count written.

        UPSERT on the LIVE unique constraint (late_account_id, metric_date), so a re-pull
        of the same day UPDATES rather than appending a second point. Without that the
        series silently doubles and every growth number computed off it is wrong.

        That target is not a guess: it was probed against production on 2026-09-05.
        (late_account_id, metric_date) resolved; (gym_id, late_account_id, metric_date)
        came back 42P10 "there is no unique or exclusion constraint matching the ON
        CONFLICT specification", which would have been a 400 on every write.

        NULL MEANS NULL, enforced here and not merely documented: a metric key whose value
        is None is REMOVED from the payload rather than sent. PostgREST merge-duplicates
        would otherwise overwrite a real measurement with an explicit null on a later
        partial pull, and no caller can turn a missing metric into a 0 through this method
        because a 0 has to be an actual int to survive the filter below.
        """
        if not rows:
            return 0
        _METRICS = ("followers", "reach", "impressions", "engagement", "profile_views")
        payload = []
        for row in rows:
            body = {k: row[k] for k in ("gym_id", "late_account_id", "metric_date",
                                        "platform", "source", "raw", "pulled_at")
                    if row.get(k) is not None}
            for k in _METRICS:
                v = row.get(k)
                if v is None:
                    continue  # not measured. NEVER written as 0.
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                body[k] = int(v)
            if body.get("gym_id") and body.get("metric_date"):
                payload.append(body)
        if not payload:
            return 0
        written = 0
        for i in range(0, len(payload), 500):
            chunk = payload[i:i + 500]
            r = self._client().post(
                self._rest("gym_social_metrics_daily"),
                params={"on_conflict": "late_account_id,metric_date"},
                headers=self._headers({
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                }),
                json=chunk, timeout=60,
            )
            if r.status_code >= 400:
                raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
            written += len(chunk)
        return written

    def failed_gbp_rows(self):
        """READ-ONLY: every content_calendar row stuck in status='failed' on the
        googlebusiness lane (AUD-003). Fleet wide; the retry sweep is a fleet sweep.

        Selects only columns that exist. A select naming a column the table does not
        have returns a 400, and the shared reader in this repo swallows a failure into
        an empty list, so a typo here would read as "no failed rows" forever. That is
        exactly how this defect stayed invisible.
        """
        params = {"status": "eq.failed", "account": f"eq.{_GBP_ACCOUNT}",
                  "select": "id,gym_id,account,post_date,status,reject_reason,"
                            "late_post_id,updated_at",
                  "limit": "1000", "order": "post_date.asc"}
        r = self._client().get(self._rest(_TABLE), params=params,
                               headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def requeue_failed_row(self, row_id):
        """Move ONE failed googlebusiness row back to 'approved' so the ordinary
        gbp_worker picks it up. Returns the updated row, or None if nothing matched.

        THE GUARD IS IN THE FILTER, not in the caller. The PATCH matches on
        status='failed' AND account='googlebusiness' AND late_post_id is null as well as
        the id, so even a mistaken call cannot move a published row, a row on another
        lane, or a row that already carries a post id. If the row changed underneath us
        between the read and this write, the filter simply matches nothing and the
        method reports that honestly instead of forcing the transition.
        """
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}", "status": "eq.failed",
                    "account": f"eq.{_GBP_ACCOUNT}", "late_post_id": "is.null"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"status": "approved", "reject_reason": None},
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
            # 0318: a candidate/archived row can never legitimately be
            # 'publishing' (only an active row is ever claimed by mark_publishing),
            # but the filter is added anyway so a future bug elsewhere can never
            # turn this into a false stale-claim alert on a variant row.
            "variant_status": "eq.active",
            "select": "id,gym_id,account,format,post_date,scheduled_at,caption,"
                      "image_url,publish_claim_token,publish_reservation_day",
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

    def reconcile_stale_published(self, account_key, row_id, claim_token,
                                  media_id, published_at):
        """Stamp one provider-confirmed live stale claim with a tenant-scoped CAS."""
        if not all(str(v or "").strip() for v in
                   (account_key, row_id, claim_token, media_id, published_at)):
            raise PortalStoreError(422, "stale publish reconciliation identity is incomplete")
        try:
            from uuid import UUID
            claim_token = str(UUID(str(claim_token)))
        except (TypeError, ValueError, AttributeError):
            raise PortalStoreError(422, "stale publish reconciliation token is invalid")
        r = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}",
                    "status": "eq.publishing", "published_at": "is.null",
                    "late_post_id": "is.null",
                    "publish_claim_token": f"eq.{claim_token}"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"status": "published", "published_at": str(published_at),
                  "late_post_id": str(media_id), "publish_claim_token": None},
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return next((row for row in rows
                     if str(row.get("id")) == str(row_id)
                     and str(row.get("gym_id")) == str(account_key)
                     and row.get("status") == "published"
                     and str(row.get("late_post_id") or "") == str(media_id)), None)

    def release_stale_publish_claim(self, account_key, row_id, claim_token, reason):
        """Release one provider-confirmed absent claim with a tenant-scoped CAS.

        ``approved`` is the conservative retry state: it preserves manual client
        approval and remains eligible for an autonomous account.
        """
        return self._transition_unpublished_claim(
            account_key, row_id, "approved", reason, claim_token)

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
            # 0318: an unchosen candidate must never fire a false "N approved
            # posts can never publish" alert — only the group's active row is
            # actually due to publish.
            "variant_status": "eq.active",
            "select": "id,gym_id,account,post_date,status",
            "order": "post_date.asc",
        }
        r = self._client().get(
            self._rest(_TABLE), params=params, headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        return r.json() or []

    def mark_published(self, row_id, media_id, published_at,
                       allow_missing_post_id=False, expected_claim_token=None):
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
        if expected_claim_token:
            try:
                from uuid import UUID
                expected_claim_token = str(UUID(str(expected_claim_token)))
            except (TypeError, ValueError, AttributeError):
                raise PortalStoreError(422, "publish claim token is invalid")
            params["publish_claim_token"] = f"eq.{expected_claim_token}"
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
                "publish_claim_token": None,
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

    def mark_duplicate_content(self, account_key, row_id, reason,
                               expected_claim_token=None):
        """Retire a duplicate rejected BEFORE the publisher's network call.

        Only the publisher-owned claim is eligible. Published rows and any row
        with a provider id or publication timestamp remain untouched. This is a
        reversible calendar soft delete, never deletion on a social platform.
        """
        return self._transition_unpublished_claim(
            account_key, row_id, "deleted", reason, expected_claim_token)

    def release_content_ledger_claim(self, account_key, row_id, previous_status, reason,
                                     expected_claim_token=None):
        """A ledger read/write failed before any network call: retry the owned row."""
        if previous_status not in ("pending", "approved"):
            return None
        return self._transition_unpublished_claim(
            account_key, row_id, previous_status, reason, expected_claim_token)

    def _transition_unpublished_claim(self, account_key, row_id, status, reason,
                                      expected_claim_token):
        """Change only the exact unpublished claim owned by this worker.

        A row can be released and reclaimed while an older worker is still running.
        The claim UUID is therefore part of the compare-and-swap identity; row id,
        tenant and ``publishing`` status alone do not prevent an ABA overwrite.
        """
        if not str(account_key or "").strip():
            raise PortalStoreError(422, "claim transition requires a gym id")
        if not expected_claim_token:
            raise PortalStoreError(422, "claim transition requires a claim token")
        try:
            from uuid import UUID
            expected_claim_token = str(UUID(str(expected_claim_token)))
        except (TypeError, ValueError, AttributeError):
            raise PortalStoreError(422, "claim transition token is invalid")
        response = self._client().patch(
            self._rest(_TABLE),
            params={"id": f"eq.{row_id}", "gym_id": f"eq.{account_key}",
                    "status": "eq.publishing", "published_at": "is.null",
                    "late_post_id": "is.null",
                    "publish_claim_token": f"eq.{expected_claim_token}"},
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"status": status, "reject_reason": str(reason)[:500],
                  "publish_reservation_day": None, "publish_claim_token": None},
            timeout=30,
        )
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, _scrub((response.text or "")[:200]))
        rows = response.json() or []
        return next((row for row in rows if str(row.get("id")) == str(row_id)
                     and str(row.get("gym_id")) == str(account_key)
                     and row.get("status") == status), None)

    def mark_publish_failed(self, row_id, revert_status="pending",
                            reject_reason=None, gym_id=None,
                            expected_claim_token=None):
        """
        REVERT a claim after a PRE-NETWORK block (including a would_publish result)
        or a provider result that explicitly proves no post exists. LASSO rows
        revert to 'pending'. A CLIENT row that was APPROVED before the claim reverts
        to 'approved', so a safe retry never forces the client to re-approve.
        Ambiguous outcomes after a network call must not use this method because an
        automatic retry could duplicate a live post. The update records no media id
        or publication timestamp and matches only the exact tenant, row, publishing
        status, and owned claim UUID. The UUID prevents a stale worker from changing
        the row after it was released and claimed again. Returns the updated row or
        None when the claim no longer matches.

        reject_reason (publish_guard wiring, 2026-08-27): when the publish guard
        blocks a row, its violation codes land on the row so the portal/human can
        see WHY it went back to pending. None (the default) leaves the column
        untouched — a transient network failure never overwrites a guard reason.
        """
        if revert_status not in ("pending", "approved"):
            revert_status = "pending"
        if gym_id is None:
            raise PortalStoreError(422, "publish rollback requires a gym id")
        if not expected_claim_token:
            raise PortalStoreError(422, "publish rollback requires a claim token")
        try:
            from uuid import UUID
            expected_claim_token = str(UUID(str(expected_claim_token)))
        except (TypeError, ValueError, AttributeError):
            raise PortalStoreError(422, "rollback claim token is invalid")
        body = {"status": revert_status, "publish_reservation_day": None,
                "publish_claim_token": None}
        if reject_reason is not None:
            body["reject_reason"] = str(reject_reason)[:500]
        params = {"id": f"eq.{row_id}", "status": "eq.publishing",
                  "published_at": "is.null", "late_post_id": "is.null"}
        params["gym_id"] = f"eq.{gym_id}"
        params["publish_claim_token"] = f"eq.{expected_claim_token}"
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
        return next((row for row in rows if str(row.get("id")) == str(row_id)
                     and row.get("status") == revert_status
                     and str(row.get("gym_id")) == str(gym_id)),
                    None)

    # ---- mirror writes (real-drafts calendar mirror) ------------------------
    # These write calendar rows only. NOTHING here publishes to any social account.
    def preflight_cadence_rows(self, account_key, rows, *, replace_dates=()):
        """Read-only cadence admission check used before a month is deleted.

        Run every row-dropping belt against the still-intact book. Live-slot dedupe
        is limited to this batch because the old wipeable rows are about to be
        replaced. The admitted payload is then inserted with those belts frozen, so
        no deterministic filter can first run after the delete.
        """
        planned = [dict(row or {}, gym_id=account_key) for row in (rows or ())]
        normalized_pairs = []
        from .copy_gate import bound_opening_hook, format_caption
        for row in planned:
            clean = dict(row)
            try:
                if "caption" in clean and clean["caption"] is not None:
                    clean["caption"] = bound_opening_hook(
                        format_caption(clean["caption"]))
            except ValueError:
                # The write path would retain this as a media hold. A required
                # feed on hold cannot certify cadence, so leave it out and let
                # the companion/required-slot checks refuse the rebuild.
                continue
            normalized_pairs.append((row, clean))
        from .plan_horizon import belt_filter as _horizon_belt
        planned, _ = _horizon_belt(account_key, planned)
        admitted_planned_ids = {id(row) for row in planned}
        filtered = [clean for original, clean in normalized_pairs
                    if id(original) in admitted_planned_ids]
        filtered = _stage_belts(account_key, filtered)
        filtered = _media_stage_belt(
            self, account_key, filtered,
            skip_wipeable_dates=replace_dates)
        # Held non-Story slots block cadence admission. Story proposals must remain:
        # insert_rows' reconciliation path needs the new ready Story in order to
        # recover the retained held UUID rather than leaving the hold stranded.
        non_story_rows = [row for row in filtered
                          if str(row.get("format") or "").strip().lower() != "story"]
        admitted_non_story_ids = {
            id(row) for row in _preserve_held_slots(
                self, account_key, non_story_rows)}
        filtered = [row for row in filtered
                    if str(row.get("format") or "").strip().lower() == "story"
                    or id(row) in admitted_non_story_ids]
        filtered = _dedupe_slots(self, account_key, filtered, existing=set())
        return _drop_companions_missing_instagram_feed(planned, filtered)

    def insert_rows(self, account_key, rows, *, preserve_ids=False,
                    render_evidence_by_url=None, poster_render_evidence_by_url=None,
                    required_feed_slots=None, prevalidated_cadence=False,
                    return_write_receipt=False):
        """INSERT content_calendar rows for account_key WITHOUT sending an `id`, so the
        DB generates the uuid primary key itself. content_calendar.id is a Postgres uuid
        (DB default gen_random_uuid); sending a non-uuid string (a draft_id) is what
        caused 22P02 "invalid input syntax for type uuid" and wrote 0 rows. There is no
        draft_id column, so a draft's id is simply not persisted as the row id: /social
        and the approve/deny actions key off the DB-returned uuid, not the draft id.

        Every row's gym_id is FORCED to account_key (a caller can never write another
        gym's row through this store). IDs are stripped by default. The explicit
        preserve_ids option accepts validated UUIDs for crash-safe automatic jobs.
        No on_conflict/upsert: apply is delete-then-insert, so a plain insert is correct
        and idempotent. Returns the list of inserted row dicts (each with its new uuid).
        With ``return_write_receipt``, returns those rows plus the exact normalized
        post-belt payload the store intended to write; event edits use that receipt to
        distinguish legitimate filtering from an incomplete durable insert.

        KEY NORMALIZATION: PostgREST rejects a heterogeneous batch with PGRST102 "All
        object keys must match". Our rows are NOT uniform — a video row carries
        thumbnail_url, a photo row doesn't — so a mixed batch (any gym with both photo
        and video posts) used to 400 the ENTIRE insert and write 0 rows (GritX rebuild
        stuck at 1 day). We normalize every row to the UNION of keys across the batch,
        filling missing keys with None, so the batch is always uniform."""
        payload = []
        from .copy_gate import bound_opening_hook, format_caption
        for row in (rows or []):
            clean = {k: v for k, v in dict(row or {}).items()
                     if k not in ("id", "scene_candidate")}
            if "caption" in clean and clean["caption"] is not None:
                # Every calendar-building lane converges here. Prompts and individual
                # generators can miss the hook limit, so enforce the grader's exact
                # first-line rule at the persistence boundary without dropping words.
                # A semicolon glued inside a protected URL/handle span makes
                # format_caption raise; ONE bad caption must not abort the whole
                # batch. Retain an unpublishable hold so a month rebuild does
                # not leave an invisible gap after it deleted the old month.
                try:
                    clean["caption"] = bound_opening_hook(
                        format_caption(clean["caption"]))
                except ValueError:
                    print(f"[portal-calendar-store] insert_rows: {account_key} "
                          f"{clean.get('post_date')} row held — semicolon "
                          "inside a protected URL")
                    clean["media_not_ready_reason"] = "caption_url_semicolon"
                    try:
                        from . import ops_alerts
                        ops_alerts.alert(
                            f"{account_key}: calendar row for {clean.get('post_date')} "
                            "was staged on hold because its caption has a semicolon "
                            "inside a URL. Edit the link and release the hold.")
                    except Exception:
                        pass
            if preserve_ids:
                import uuid
                # Explicit stable UUIDs support crash-safe automatic render retries.
                clean["id"] = str(uuid.UUID(str((row or {}).get("id") or "")))
            clean["gym_id"] = account_key  # gym scope: never trust a foreign gym_id
            # LOGICAL POST IDENTITY (2026-10-04): a caller may stamp each row with
            # the ONE logical_post_id minted upstream for the sibling group (IG
            # feed + FB mirror + paired Story share one UUID). We pass it through
            # verbatim after strict UUID validation; we NEVER mint one here,
            # because per-row minting would give siblings different IDs. Existing
            # callers that send no logical_post_id leave the column NULL.
            if clean.get("logical_post_id") is not None:
                import uuid as _uuid
                try:
                    clean["logical_post_id"] = str(
                        _uuid.UUID(str(clean["logical_post_id"])))
                except (ValueError, AttributeError, TypeError):
                    raise ValueError(
                        "logical_post_id must be a UUID shared by the sibling "
                        f"post group; got {clean['logical_post_id']!r}")
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
        planned_companions = list(payload)
        if not prevalidated_cadence:
            payload = _stage_belts(account_key, payload)
        # CROSS-DAY MEDIA BELT (fleet audit, 2026-08-31; flag AGENT_MEDIA_CROSS_DAY_GUARD,
        # the SAME flag media_guard already ships armed on). agent/media_guard.py calls
        # itself "the shared cross-day media guard for every photo-assigning lane" and was
        # wired into exactly TWO of them. This door is the one every staging lane walks
        # through, so the rule lives here too. See _media_stage_belt.
            payload = _media_stage_belt(self, account_key, payload)
        # SLOT IDEMPOTENCY BELT (AUD-001, 2026-09-05; default ON because it PREVENTS
        # damage, same posture as the plan-horizon belt. AGENT_SLOT_DEDUPE=false is the
        # escape hatch).
        #
        # The docstring above says "apply is delete-then-insert, so a plain insert is
        # correct and idempotent". Production disagreed: on 2026-09-05 the fleet carried
        # 155 genuine duplicate slots across 14 gyms, and the forward book was still
        # growing hour over hour. Two distinct causes, both closed here because this is
        # the single door every staging lane walks through:
        #
        #   1. 94 of them were written TWICE IN THE SAME SECOND by one run, so the batch
        #      itself already held the row twice before the POST. No caller-side fix
        #      catches every lane; a batch that contains one slot twice is never correct.
        #   2. 61 spanned different runs. delete_month deliberately PRESERVES human-owned
        #      rows (an approved row is a client decision), so a re-plan legitimately
        #      skips deleting that slot and then inserts a fresh row on top of it.
        #
        # The slot key is (account, post_date, time_slot, format), NOT (account,
        # post_date). That distinction is the whole point: a gym posting 2x a day plus a
        # story has three legitimate rows on one date. Keying on the date alone called
        # 441 rows duplicates when only 155 were, and superseding on it would have
        # destroyed live client content.
        # A previously inserted hold is the durable retry signal when its
        # support seed failed. Retry confirmed held rows before slot dedupe can
        # drop a repeated planner proposal; READY preserved rows never emit.
        _retry_story_hold_provenance(self, account_key, payload)
        payload, recovered = _reconcile_story_media_holds(self, account_key, payload)
        # Retained holds own their exact slot until explicit recovery or wipe.
        # Story recovery above uses its retained UUID; this barrier governs NEW
        # rows of every format. Caption/image changes cannot bypass it, and it
        # does not collapse numbered slots or channel siblings.
        if not prevalidated_cadence:
            payload = _preserve_held_slots(self, account_key, payload)
            payload = _dedupe_slots(self, account_key, payload)
        else:
            # Re-read only the durable/concurrent ownership barriers immediately
            # before POST. Deterministic caption/media/in-batch filtering was frozen
            # by preflight; these two checks must remain live so an approval or hold
            # created after preflight is never stacked with a new row.
            live_months = sorted({str(row.get("post_date") or "")[:7]
                                  for row in payload
                                  if str(row.get("post_date") or "")[:7]})
            try:
                payload, _ = _preserve_and_prune_strict(
                    self, account_key, live_months, payload)
            except Exception as exc:
                raise CalendarInsertNotStartedError(
                    503, "live human-owned slot read failed before calendar insert") from exc
            payload = _preserve_held_slots(self, account_key, payload)
            payload = _dedupe_slots(self, account_key, payload)
        # A recovered Story was patched in place under its retained UUID and is
        # deliberately absent from the POST payload. Count the exact planned
        # Story slot as satisfied for companion atomicity without re-inserting it.
        recovered_story_slots = {_story_slot(row) for row in recovered
                                 if (row or {}).get("format") == "story"}
        recovered_companions = [
            row for row in planned_companions
            if (row or {}).get("format") == "story"
            and _story_slot(row) in recovered_story_slots]
        payload = _drop_companions_missing_instagram_feed(
            planned_companions, payload, satisfied=recovered_companions)
        if prevalidated_cadence and required_feed_slots is not None:
            required = {tuple(slot) for slot in required_feed_slots}
            actual = _instagram_feed_slots(payload)
            if not required.issubset(actual):
                raise CadencePreconditionError(
                    409, "calendar cadence precondition failed before insert")
        if not payload:
            if return_write_receipt:
                return {"inserted_rows": recovered, "expected_rows": []}
            return recovered
        from . import visual_writer_prepare
        prepared_write = visual_writer_prepare.enabled()
        if prepared_write:
            # Poster evidence is scoped to the whole media edge, not only to
            # the poster URL. Two videos can intentionally share a poster URL
            # while each requires a different image -> poster rendition proof.
            # Accepting a thumbnail-only key would attach one video's proof to
            # another video's image, so use the strict composite key and never
            # fall back to a thumbnail-only lookup.
            try:
                payload = [self._prepare_visual_row(
                    account_key, row,
                    render_evidence=(render_evidence_by_url or {}).get(row.get("image_url")),
                    poster_render_evidence=(poster_render_evidence_by_url or {}).get(
                        (row.get("image_url"), row.get("thumbnail_url"))))
                    for row in payload]
            except Exception as exc:
                if not prevalidated_cadence:
                    raise
                raise CalendarInsertNotStartedError(
                    409, f"visual preparation failed before calendar insert: "
                    f"{type(exc).__name__}") from exc
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
        if prepared_write:
            if not isinstance(out, list) or len(out) != len(payload):
                raise PortalStoreError(502, "calendar insert returned unverified visual rows")
            unmatched = list(payload)
            seen_ids = set()
            for row in out:
                if not isinstance(row, dict) or not row.get("id") or row["id"] in seen_ids:
                    raise PortalStoreError(502, "calendar insert returned unverified visual rows")
                matches = [candidate for candidate in unmatched
                           if all(key in row and row[key] == value for key, value in candidate.items())]
                if not matches:
                    raise PortalStoreError(502, "calendar insert returned unverified visual rows")
                unmatched.remove(matches[0])
                seen_ids.add(row["id"])
        inserted = [x for x in out if str(x.get("gym_id")) == str(account_key)]
        _record_confirmed_story_holds(account_key, inserted)
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
        written = recovered + inserted
        if return_write_receipt:
            # Event edits need the authoritative write set after every persistence
            # belt.  A pre-store count cannot distinguish a legitimate held/caption/
            # cross-day-media refusal from a partial or ambiguous database insert.
            return {"inserted_rows": written,
                    "expected_rows": [dict(row) for row in payload]}
        return written

    def recover_story_media_hold(self, account_key, current, proposed, *,
                                 poster_render_evidence=None):
        """Atomically recover the retained row, preserving its incident UUID.

        This updates a retained pending machine Story. Human/publisher states,
        variant ownership, tenant, slot, media and generation are compared in the
        PATCH; human-owned states never become pending again.
        """
        from urllib.parse import urlsplit
        media = proposed.get("image_url")
        try:
            ready = (isinstance(media, str) and urlsplit(media).scheme == "https"
                     and bool(urlsplit(media).netloc)
                     and proposed.get("media_not_ready_reason") is None)
        except ValueError:
            ready = False
        if (current.get("format") != "story" or current.get("status") != "pending" or not ready
                or proposed.get("format") != "story" or proposed.get("status") != "pending"
                or current.get("gym_id") != account_key
                or current.get("variant_status") != "active"
                or _story_slot(current) != _story_slot(proposed)):
            return None
        # Recovery changes media only; retain client copy and calendar decisions.
        columns = {"image_url", "thumbnail_url", "source_media_url", "source_media_asset_id"}
        patch = {key: value for key, value in proposed.items() if key in columns}
        patch["media_not_ready_reason"] = None
        patch = self._prepare_visual_media(
            account_key, current["id"], patch, current=current,
            poster_render_evidence=poster_render_evidence)
        params = {"id": f"eq.{current['id']}", "gym_id": f"eq.{account_key}",
                  "account": f"eq.{current['account']}", "post_date": f"eq.{current['post_date']}",
                  "format": "eq.story", "status": "eq.pending", "variant_status": "eq.active",
                  "created_at": f"eq.{current['created_at']}",
                  "media_not_ready_reason": ("is.null" if current.get("media_not_ready_reason") is None
                                             else f"eq.{current['media_not_ready_reason']}"),
                  "image_url": ("is.null" if current.get("image_url") is None
                                else f"eq.{current['image_url']}")}
        from . import visual_writer_prepare
        visual_guard = visual_writer_prepare.enabled()
        if visual_guard:
            params = self._visual_media_cas(current, params)
        response = self._client().patch(self._rest(_TABLE), params=params, json=patch,
                         headers=self._headers({"Prefer": "return=representation"}), timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(response.status_code, _scrub((response.text or "")[:200]))
        rows = response.json() or []
        if visual_guard and self._visual_media_result(
                rows, account_key, current, patch) is None:
            return None
        if (len(rows) != 1 or rows[0].get("id") != current["id"]
                or rows[0].get("gym_id") != account_key or _story_slot(rows[0]) != _story_slot(current)
                or rows[0].get("created_at") != current["created_at"]
                or rows[0].get("image_url") != media or rows[0].get("media_not_ready_reason") is not None
                or rows[0].get("status") != "pending" or rows[0].get("variant_status") != "active"):
            return None
        return rows[0]

    def delete_month(self, account_key, month, *, preserve_human=True,
                     preserve_dates=(), preserve_slots=(), preserve_gbp=None, return_rows=False):
        """DELETE content_calendar rows for account_key whose post_date falls inside the
        calendar month `month` ('YYYY-MM'). Gym scoped: the filter carries BOTH
        gym_id=eq.<account_key> AND the month's date bounds, so a row belonging to another
        gym (or outside the month) is never touched. Used by the delete-then-insert apply
        so a re-run replaces the month cleanly and idempotently. Returns the number of the
        gym's rows deleted.

        preserve_human (default True): only WIPEABLE rows (fresh machine drafts:
        pending / draft / queued / NULL status) are deleted. Any row a human or the
        publisher has touched (approved, denied, killed, published, publishing, failed)
        is LEFT IN PLACE, so a nightly rebuild can never revert a client's approval.
        The same guard preserves an active row carrying a recorded
        media_not_ready_reason (a media hold such as
        cross_date_media_repeat_needs_new_visual). Wiping the row would erase
        the hold and allow a rebuild to stage the rejected visual again.
        Pass preserve_human=False only for a deliberate full wipe of a gym's
        month (which also deletes media-hold rows).

        preserve_slots: (post_date, slot_index) pairs whose pending siblings are
        retained while other slots on that date are replaced. Null ordinals belong
        to slot 0. These predicates compose with all human/variant/hold guards.

        preserve_gbp: mapping of partially rebuilt dates to GBP formats explicitly
        replaced by this build. All other GBP formats on those dates survive,
        regardless of their independent slot_index.

        preserve_dates: post_dates whose rows are NOT deleted at all (even wipeable
        ones). Fully locked days keep their still-pending FB mirrors and paired
        Stories. Partially locked days instead use preserve_slots so rebuilding
        an open cadence slot cannot duplicate its old drafts."""
        # Slot predicates protect pending mirrors and Stories in the same DELETE
        # as status/hold guards. Legacy null ordinals are the first cadence slot.
        protected_slots = []
        from datetime import date as _date
        for day, ordinal in sorted(set(preserve_slots)):
            if (_date.fromisoformat(day).isoformat() != day
                    or type(ordinal) is not int or ordinal < 0):
                raise ValueError("invalid preserved cadence slot")
            slot_filter = ("or(slot_index.eq.0,slot_index.is.null)" if ordinal == 0
                           else f"slot_index.eq.{ordinal}")
            protected_slots.append(
                f"and(post_date.eq.{day},{slot_filter},"
                "or(account.neq.googlebusiness,account.is.null))")
        for day, replaced_formats in sorted((preserve_gbp or {}).items()):
            if _date.fromisoformat(day).isoformat() != day:
                raise ValueError("invalid preserved GBP date")
            if any(fmt not in ("update", "photo", "event", "offer")
                   for fmt in replaced_formats):
                raise ValueError("invalid replacement GBP format")
            format_guard = (f",or(format.is.null,format.not.in.({','.join(replaced_formats)}))"
                            if replaced_formats else "")
            protected_slots.append(
                f"and(post_date.eq.{day},account.eq.googlebusiness{format_guard})")
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
            # 0318: NEVER delete a candidate (an unchosen alternate awaiting a
            # human pick) or an archived row (kept for audit, by design, not
            # deletable). A rebuild must only ever touch the plain active row
            # a slot already had -- deleting an archived row would silently
            # break the "kept, not deleted" guarantee the swap feature makes,
            # and deleting a pending candidate would destroy an in-flight pick
            # the moment the nightly job ran.
            "variant_status": "eq.active",
        }
        if preserve_human:
            # Protect actual incidents, including recovered exact generations;
            # unrelated READY Stories retain normal replace/omission behavior.
            try:
                month_rows = self.rows_in_range(account_key, first, last)
                if not isinstance(month_rows, list) or len(month_rows) >= 1000:
                    raise ValueError("partial Story read")
                _record_confirmed_story_holds(account_key, month_rows)
                incident_targets = _story_incident_targets(self, account_key, first, last)
                from .fixer_business_seed import validate_story_created_at
                protected = [r["id"] for r in month_rows
                             if r.get("format") == "story" and
                             (_is_story_media_hold(r) or
                              (r.get("id"), validate_story_created_at(r.get("created_at"))) in incident_targets)]
                # Also protect a hold inserted concurrently after this read.
                params["and"] = "(or(format.neq.story,format.is.null,media_not_ready_reason.is.null))"
                if protected:
                    params["id"] = f"not.in.({','.join(protected)})"
            except Exception:
                # Never delete an unknown Story generation after a failed or
                # partial shared/provenance read. Feed rebuild remains bounded.
                params["and"] = "(or(format.neq.story,format.is.null))"
                print("[calendar] Story protection read unavailable; Story rows retained")
            # delete only the never-touched drafts: status IS NULL OR status IN wipeable.
            in_list = ",".join(_WIPEABLE_STATUSES)
            params["or"] = f"(status.is.null,status.in.({in_list}))"
            # MEDIA-HOLD GUARD: a wipeable-status row with a recorded reason
            # remains held across rebuilds. Server-side is.null protects the
            # decision in the same DELETE statement; a read-then-delete could
            # race a newly applied media hold.
            params["media_not_ready_reason"] = "is.null"
        if protected_slots:
            slot_guard = f"not.or({','.join(protected_slots)})"
            prior_guard = params.get("and", "()")[1:-1]
            params["and"] = f"({prior_guard + ',' if prior_guard else ''}{slot_guard})"
        r = self._client().delete(
            self._rest(_TABLE),
            params=params,
            headers=self._headers({"Prefer": "return=representation"}),
            timeout=30,
        )
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = [x for x in (r.json() or [])
                if str(x.get("gym_id")) == str(account_key)]
        return rows if return_rows else len(rows)

    def restore_deleted_rows(self, account_key, rows):
        """Restore exact rows deleted by this rebuild after a pre-POST refusal."""
        import uuid
        payload = []
        for row in rows or ():
            clean = dict(row or {})
            if str(clean.get("gym_id")) != str(account_key):
                raise ValueError("rollback row belongs to another gym")
            clean["id"] = str(uuid.UUID(str(clean.get("id") or "")))
            payload.append(clean)
        if not payload:
            return []
        all_keys = set().union(*(row.keys() for row in payload))
        payload = [{key: row.get(key) for key in all_keys} for row in payload]
        response = self._client().post(
            self._rest(_TABLE),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=payload, timeout=30)
        if response.status_code >= 400:
            raise PortalStoreError(
                response.status_code, _scrub((response.text or "")[:200]))
        restored = response.json() or []
        expected_ids = {row["id"] for row in payload}
        actual_ids = {row.get("id") for row in restored
                      if str(row.get("gym_id")) == str(account_key)}
        if actual_ids != expected_ids:
            raise PortalStoreError(502, "calendar rollback was incomplete")
        return restored

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

    def deny_wipeable_with_reason(self, account_key, row_id, reject_reason):
        """Compensate one machine-owned row only while it remains wipeable.

        The status predicate is part of the PATCH, so an approval, publish claim,
        publication, denial, kill, failure, or hold that wins after the caller's read
        cannot be overwritten by cleanup from a stale event edit.
        """
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": f"in.({','.join(_WIPEABLE_STATUSES)})",
            # A media hold is an operator/system decision even while the row remains
            # pending. Preserve a hold that races stale event cleanup just as we
            # preserve an approval or publish claim.
            "media_not_ready_reason": "is.null",
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
        for row in (r.json() or []):
            if (str(row.get("id")) == str(row_id)
                    and str(row.get("gym_id")) == str(account_key)
                    and row.get("status") == "denied"):
                return row
        return None

    def deny_event_wipeable_with_reason(self, account_key, event_id, row_id,
                                        reject_reason):
        """Deny one exact event row after its event becomes terminal.

        Unlike stale edit compensation, terminal cleanup intentionally includes
        media-held pending/draft/queued rows. The server-side status CAS preserves a
        concurrent approval, publish claim, publication, or other protected state;
        event_id prevents a stale event sweep from touching a row outside its arc.
        """
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "event_id": f"eq.{event_id}",
            "status": f"in.({','.join(_WIPEABLE_STATUSES)})",
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
        for row in (r.json() or []):
            if (str(row.get("id")) == str(row_id)
                    and str(row.get("gym_id")) == str(account_key)
                    and str(row.get("event_id")) == str(event_id)
                    and row.get("status") == "denied"):
                return row
        return None

    def list_rows_by_ids(self, account_key, row_ids):
        """Read exact calendar UUIDs within one gym for ambiguous-write reconciliation."""
        import uuid

        ids = sorted({str(uuid.UUID(str(row_id))) for row_id in (row_ids or [])})
        if not ids:
            return []
        if len(ids) > 200:
            raise ValueError("operation row reconciliation exceeds 200 ids")
        r = self._client().get(
            self._rest(_TABLE),
            params={"gym_id": f"eq.{account_key}",
                    "id": f"in.({','.join(ids)})", "limit": str(len(ids))},
            headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        expected = set(ids)
        return [row for row in (r.json() or [])
                if str(row.get("gym_id")) == str(account_key)
                and str(row.get("id")) in expected]

    def patch_pending_plan(self, account_key, row_id, *, caption=None, pillar=None,
                           levers=None, expected_row=None,
                           force_caption_visual_hold=False,
                           caption_hold_source_feed=None):
        """PATCH a WIPEABLE row's caption and/or pillar (the grade self-fix lane,
        AGENT_GRADE_SELF_FIX), filtered by id AND gym_id AND a server-side
        status IN (pending,draft,queued) guard, so a human-owned row (approved /
        published / publishing / denied / killed / failed) can NEVER be modified
        through this method no matter what the caller passes — the hard guarantee
        that self-remediation only ever rewrites fresh machine drafts. Status
        stays 'pending' (the approval gate is untouched: the row remains in the
        owner's approval queue; nothing is auto-approved). Returns the updated
        row dict, or None when zero rows matched.

        LEVER RE-STAMP (Dean Holcomb / CrossFit Reverb, 2026-09-05). The learning
        levers (hook_family, ask_type, caption_len_band) are stamped at STAGE time
        against the SB7 body, which by design carries no CTA. This lane then
        mutates the caption afterwards, and because only caption/pillar were ever
        written, the levers stayed frozen at their pre-repair values. Measured on
        Reverb's live book: ask_type='none' on 93 of 93 rows while 90 of them
        ended in an ask, and caption_len_band='mid' on 100% of rows. Nothing could
        correct it either, because jobs/backfill_levers only selects rows WHERE
        hook_family IS NULL and these were already stamped. That is a lie the
        learner reads: metrics_sync copies these columns onto post_metrics and
        monthly_retro compares on them. A caller that changes the caption must
        pass the re-stamped levers so the label keeps telling the truth.

        `caption_hold_source_feed` is the paired feed row dict, accepted only
        alongside `force_caption_visual_hold` on a LASSO Story whose current
        hold is 'paired_feed_not_ready' (a normal Story waiting on its feed's
        visual). Without that source link the widened hold stays refused.
        One further transition is allowed for a canonical owned Oct 2-5 2026
        LASSO feed: 'prepared_backlog_waiting_for_story_and_capacity' moves to
        'caption_changed_needs_new_visual' atomically with the new caption
        (_prepared_backlog_caption_hold_transition_ok; never a clear).
        """
        fields = {}
        if caption is not None:
            from .copy_gate import format_caption
            fields["caption"] = format_caption(caption)
        if pillar is not None:
            fields["pillar"] = pillar
        for key, value in (levers or {}).items():
            if key in _LEVER_COLUMNS and value is not None:
                fields[key] = value
        caption_visual_hold = (
            account_key == "lasso" and expected_row is not None
            # PR29705 review: the caption visual hold belongs to the autonomous
            # LASSO lane. With AGENT_LASSO_3X_ENABLED OFF this PATCH keeps the
            # old mechanical CAS behavior and sets NO new hold; every condition
            # below (and the paired-Story source guard) is unchanged when ON.
            and config.lasso_three_feed_enabled()
            and ((caption is not None and caption != expected_row.get("caption"))
                 or force_caption_visual_hold))
        if (expected_row is not None
                and expected_row.get("media_not_ready_reason")
                == _PREPARED_BACKLOG_HOLD
                and ((caption is not None
                      and caption != expected_row.get("caption"))
                     or force_caption_visual_hold)):
            # The prepared-backlog hold may only ever TRANSITION (never be
            # cleared, never be left behind by a caption change), and only
            # through the gated autonomous path. Flag OFF, a non-canonical row,
            # a claimed/receipt row or a stale snapshot all fail CLOSED here --
            # before any write -- so the copy apply can never partially reserve.
            if not (caption_visual_hold
                    and _prepared_backlog_caption_hold_transition_ok(expected_row)):
                return None
        if caption_visual_hold:
            hold_reason = expected_row.get("media_not_ready_reason")
            if hold_reason == "paired_feed_not_ready":
                # The ONLY widened case: an exact, unclaimed, pending, active
                # LASSO Story CAS whose caller names the paired source feed
                # (_paired_story_hold_source_link). The CAS params below already
                # pin status/variant/claim columns server-side, so the link
                # check is what keeps this from broadening to unrelated holds.
                if not (force_caption_visual_hold
                        and _paired_story_hold_source_link(
                            expected_row, caption_hold_source_feed)):
                    return None
            elif hold_reason == _PREPARED_BACKLOG_HOLD:
                # The Oct 2-5 prepared-backlog copy apply: the hold TRANSITIONS
                # to caption_changed_needs_new_visual atomically with the new
                # caption, only for the canonical owned LASSO feed the gate
                # proves. The old hold is never CLEARED here -- the CAS params
                # below pin it server-side and the payload replaces it.
                if not _prepared_backlog_caption_hold_transition_ok(expected_row):
                    return None
            elif hold_reason not in (None, "caption_changed_needs_new_visual",
                                     "cross_date_media_repeat_needs_new_visual"):
                return None
            # Set in the SAME PostgREST PATCH as the new caption. The old image
            # is never publishable even if the runner sees this row immediately.
            fields["media_not_ready_reason"] = "caption_changed_needs_new_visual"
        if not fields:
            return None
        fields["status"] = "pending"
        params = {
            "id": f"eq.{row_id}",
            "gym_id": f"eq.{account_key}",
            "status": f"in.({','.join(_WIPEABLE_STATUSES)})",
        }
        if expected_row is not None:
            # Autonomous caption repair must target the exact active generation.
            # A late publisher, visual swap, or concurrent rewrite makes the
            # compare-and-swap return zero rows instead of clobbering its work.
            if (str(expected_row.get("id")) != str(row_id)
                    or str(expected_row.get("gym_id")) != str(account_key)
                    or expected_row.get("status") not in _WIPEABLE_STATUSES
                    or expected_row.get("variant_status") != "active"
                    or not expected_row.get("created_at")):
                return None
            for column in ("post_date", "account", "format", "slot_index",
                           "logical_post_id", "created_at", "caption", "image_url",
                           "source_media_url", "thumbnail_url",
                           "media_not_ready_reason", "source_media_asset_id",
                           "published_at", "late_post_id", "publish_claim_token",
                           "publish_reservation_day", "scheduled_at"):
                value = expected_row.get(column)
                encoded = _eq_filter(value)
                if encoded is None:
                    return None
                params[column] = encoded
            params["status"] = f"eq.{expected_row['status']}"
            params["variant_status"] = "eq.active"
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
            if (str(row.get("gym_id")) == str(account_key)
                    and (expected_row is None or (
                        str(row.get("id")) == str(row_id)
                        and ("caption" not in fields or
                             row.get("caption") == fields["caption"])
                        and (not caption_visual_hold or
                             row.get("media_not_ready_reason") ==
                             "caption_changed_needs_new_visual")
                        and row.get("status") == "pending"))):
                return row
        return None

    def format_pending_feed_caption_cas(self, account_key, current):
        """Reformat one unapproved feed row from a frozen read, without touching media.

        Intended for a reviewed correction of an existing calendar. A concurrent
        coach edit, approval, date move, or hold makes the exact PATCH match zero
        rows. Stories are excluded because caption text can be burned into pixels.
        """
        from .copy_gate import format_caption
        if (not isinstance(current, dict)
                or current.get("gym_id") != account_key
                or current.get("status") != "pending"
                or current.get("variant_status") != "active"
                or current.get("format") != "feed"
                or current.get("media_not_ready_reason") is not None
                or current.get("published_at") is not None
                or current.get("late_post_id") is not None
                or current.get("publish_claim_token") is not None
                or not current.get("id") or not current.get("post_date")):
            return None
        before = current.get("caption")
        if not isinstance(before, str) or not before.strip():
            return None
        after = format_caption(before)
        if after == before:
            return None
        params = {}
        for key, value in (("id", current["id"]), ("gym_id", account_key),
                           ("status", "pending"), ("variant_status", "active"),
                           ("format", "feed"), ("post_date", current["post_date"]),
                           ("caption", before), ("media_not_ready_reason", None),
                           ("published_at", None), ("late_post_id", None),
                           ("publish_claim_token", None),
                           *((key, current[key]) for key in (
                               "account", "image_url", "source_media_url",
                               "source_media_asset_id", "created_at")
                             if key in current)):
            encoded = _eq_filter(value)
            if encoded is None:
                raise ValueError(f"cannot safely compare {key} for caption correction")
            params[key] = encoded
        r = self._client().patch(
            self._rest(_TABLE), params=params,
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json={"caption": after}, timeout=30)
        if r.status_code >= 400:
            raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
        rows = r.json() or []
        return rows[0] if (len(rows) == 1
                           and str(rows[0].get("id")) == str(current["id"])
                           and rows[0].get("gym_id") == account_key
                           and rows[0].get("caption") == after) else None

    def list_pending_future(self, account_key, today_iso):
        """Return all content_calendar rows for account_key where status='pending'
        and post_date > today_iso. Used by the dedupe_forward_book job."""
        params = {
            "gym_id": f"eq.{account_key}",
            "status": "eq.pending",
            "post_date": f"gt.{today_iso}",
            # 0318: a pending CANDIDATE is not the forward book's real post and
            # must never be denied/mutated as if it were.
            "variant_status": "eq.active",
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

    # Repeat-hold lane pagination bounds: 500-row id-cursor pages, hard page cap.
    _REPEAT_HOLD_PAGE_SIZE = 500
    _REPEAT_HOLD_MAX_PAGES = 100        # 50k rows: a hard tripwire, far past any real book

    def rows_in_range_complete(self, account_key, start_iso, end_iso, *,
                               all_statuses=False):
        """Complete tenant-scoped read of active rows in a date range.

        Unlike rows_in_range, this read does not cap at 1000 rows. Id-cursor
        pagination covers active variants and the requested date range. By
        default it includes forward-book statuses only; all_statuses=True is
        for additive LASSO refill, where draft and queued rows own slots too.
        Every page is
        validated; ANY error, malformed row, out-of-scope row, duplicate or
        non-ascending id, or a runaway page count fails the ENTIRE read --
        never a partial result."""
        rows = []
        seen_ids = set()
        last_id = None
        for _ in range(self._REPEAT_HOLD_MAX_PAGES):
            params = {
                "gym_id": f"eq.{account_key}",
                "variant_status": "eq.active",
                "post_date": f"gte.{start_iso}",
                "and": f"(post_date.lte.{end_iso})",
                "order": "id",
                "limit": str(self._REPEAT_HOLD_PAGE_SIZE),
            }
            if not all_statuses:
                params["status"] = "in.(pending,approved,publishing,published,coach_review)"
            if last_id is not None:
                params["id"] = f"gt.{last_id}"
            r = self._client().get(
                self._rest(_TABLE), params=params, headers=self._headers(),
                timeout=30,
            )
            if r.status_code >= 400:
                raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
            page = r.json()
            if not isinstance(page, list):
                raise PortalStoreError(0, "repeat hold page malformed")
            if not page:
                return rows
            prev_in_page = last_id
            for row in page:
                rid = row.get("id") if isinstance(row, dict) else None
                pd = str(row.get("post_date") or "")[:10] if isinstance(row, dict) else ""
                # Monotonic strictly-ascending ids WITHIN every page as well
                # as across pages: a re-ordered or repeated page is malformed.
                if (rid is None or not isinstance(row, dict)
                        or str(row.get("gym_id")) != str(account_key)
                        or not start_iso <= pd <= end_iso
                        or str(rid) in seen_ids
                        or (prev_in_page is not None and not str(rid) > prev_in_page)):
                    raise PortalStoreError(
                        0, "repeat hold page malformed/out-of-scope/duplicate-id")
                seen_ids.add(str(rid))
                prev_in_page = str(rid)
            rows.extend(page)
            last_id = str(page[-1]["id"])
            if len(page) < self._REPEAT_HOLD_PAGE_SIZE:
                return rows
        raise PortalStoreError(0, "repeat hold read exceeded maximum page count")

    def rows_in_range_repeat_hold(self, account_key, start_iso, end_iso):
        """Compatibility entry point for the cross-date repeat hold lane."""
        return SupabaseCalendarStore.rows_in_range_complete(
            self, account_key, start_iso, end_iso)

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
            # 0318: a candidate sitting beside its slot's active row is not a
            # second post the audience will see -- grading it would inflate
            # cadence/content-mix and double-count the slot.
            "variant_status": "eq.active",
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
            # 0318: a candidate variant of an arc row is not itself part of the
            # live arc until picked -- the cancel/ended sweep and publish gate
            # must only ever see the arc's active rows.
            "variant_status": "eq.active",
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

def _held_slot_key(row):
    """Exact persisted slot; nullable fields have no database defaults."""
    return tuple(row.get(k) for k in
                 ("gym_id", "post_date", "account", "format", "time_slot", "slot_index"))


def _preserve_held_slots(store, account_key, payload):
    """Refuse new active rows over a retained held slot, in every format.

    Story recovery already used its retained-UUID/CAS path. Query the hold
    column directly, including draft/NULL-status holds omitted by grade reads.
    A complete count is required: a partial/unreadable response cannot certify
    that any proposed slot is free. This is a read barrier, not a database
    concurrency guarantee; a newly created hold after this read needs the
    enduring transactional ledger.
    """
    candidates = [r for r in payload if r.get("variant_status", "active") == "active"
                  and r.get("post_date")]
    if not candidates:
        return payload
    dates = sorted({str(r["post_date"]) for r in candidates})
    fields = {"gym_id", "post_date", "account", "format", "time_slot", "slot_index",
              "variant_status", "media_not_ready_reason"}
    try:
        response = store._client().get(
            store._rest(_TABLE),
            params={"gym_id": f"eq.{account_key}",
                    "post_date": f"in.({','.join(dates)})",
                    "variant_status": "eq.active",
                    "media_not_ready_reason": "not.is.null",
                    "select": ",".join(sorted(fields)), "limit": "1000"},
            headers=store._headers({"Prefer": "count=exact"}), timeout=30)
        if response.status_code >= 400:
            raise ValueError("held-slot read failed")
        retained = response.json()
        total = getattr(response, "headers", {}).get("Content-Range", "").rsplit("/", 1)[-1]
        if not isinstance(retained, list) or not total.isdigit() or int(total) != len(retained):
            raise ValueError("incomplete held-slot read")
        if any(not isinstance(r, dict) or not fields.issubset(r)
               or r["gym_id"] != account_key or r["post_date"] not in dates
               or r["variant_status"] != "active" or r["media_not_ready_reason"] is None
               for r in retained):
            raise ValueError("invalid held-slot scope")
        held = {_held_slot_key(r) for r in retained}
        kept = [r for r in payload if r not in candidates or _held_slot_key(r) not in held]
    except Exception as exc:
        print(f"[calendar] held-slot read unconfirmed: {type(exc).__name__}; "
              "active slot staging refused; retry required")
        return [r for r in payload if r not in candidates]
    dropped = len(payload) - len(kept)
    if dropped:
        print(f"[calendar] retained media holds refused {dropped} exact-slot proposal(s)")
    return kept


def _dedupe_slot_key(row):
    """The identity of a calendar row for duplicate purposes, or None when it cannot be
    established. A row is a duplicate ONLY of a row in the same slot carrying the same
    content.

    THIS KEY WAS WRONG TWICE AND BOTH MISTAKES DESTROYED CLIENT CONTENT (2026-09-05).

    First it was (gym, account, post_date), which treats a gym posting 2x a day plus a
    story as duplication. That would have removed roughly 286 legitimate rows.

    Then it was (account, post_date, time_slot, format), which STILL cannot represent two
    posts inside one time_slot. ENG runs posts_per_day=2 and both posts can land in the
    same time_slot bucket, separated only by slot_index. A dedupe on that key deleted 140
    rows across five gyms, and when the survivors were compared against the deletions,
    ZERO were actually duplicates: 131 differed by image, 123 by caption, 75 by slot_index.
    All 140 were restored.

    So the identity now carries slot_index AND the content itself. Two rows are the same
    row only if they occupy the same slot and say the same thing with the same picture.
    Anything else is a distinct post that a gym owner is entitled to see. slot_index is
    null on most rows, and a null slot_index is a real value here (the single post of the
    day) rather than a missing one, so it participates in the key instead of voiding it."""
    account = str(row.get("account") or "").strip().lower()
    date = str(row.get("post_date") or "")[:10]
    slot = str(row.get("time_slot") or "").strip().lower()
    fmt = str(row.get("format") or "").strip().lower()
    if not (account and date and slot and fmt):
        # RULING (AUD-104): a row missing time_slot or format has no identifiable slot, so
        # it is never deduped. That under-blocks, and under-blocking is the only safe
        # direction for a filter that can drop a client's content.
        return None
    idx = row.get("slot_index")
    caption = " ".join(str(row.get("caption") or "").split()).lower()
    image = str(row.get("image_url") or "").strip()
    return (account, date, slot, fmt, idx, caption, image)


def _dedupe_slots(store, account_key, payload, *, existing=None):
    """Drop rows whose slot is already taken, in the batch or already live in the DB.

    Two passes, because production showed two distinct duplicate sources (see the caller):
    an in-batch pass that keeps the FIRST row for a slot, then a live pass that drops any
    slot this gym already holds in a live status. Never deletes anything and never
    rewrites a row: a duplicate is simply not inserted.

    Fails OPEN on an unreadable live read (returns the in-batch-deduped payload). A
    staging lane must not stop because a dedupe lookup failed; the worst case is the
    behaviour that shipped before this belt existed."""
    if not payload or not config.slot_dedupe_enabled():
        return payload
    seen, deduped, in_batch = set(), [], 0
    for row in payload:
        if row.get("event_id"):
            # DATED EVENT ROWS ARE NEVER DROPPED (AUD-102). plan_horizon exempts event_id
            # for the same reason: an event arc is a deliberate, dated override of the
            # evergreen plan, so "keep the first row for this slot" is exactly backwards
            # for it. This is not hypothetical -- the first version of this belt discarded
            # The Bolton Club's "Bring A Friend Week is on / Day is here / Last day" rows
            # in favour of the generic rows that happened to be created a day earlier.
            #
            # Exempting them can leave a transient duplicate on a slot that already holds
            # a generic row. That is the correct trade: a stray extra row is recoverable,
            # a silently missing event post is not, and resolving generic-versus-event on
            # one slot belongs to the event lane, not to a staging filter that is only
            # allowed to drop rows and never to delete them.
            deduped.append(row)
            continue
        k = _dedupe_slot_key(row)
        if k is None:          # unidentifiable slot: never guess, always stage
            deduped.append(row)
            continue
        if k in seen:
            in_batch += 1
            continue
        seen.add(k)
        deduped.append(row)
    dropped_live = 0
    if existing is None:
        existing = _live_slots_for(store, account_key, {k[1] for k in seen})
    if existing:
        kept = []
        for row in deduped:
            if row.get("event_id"):
                kept.append(row)      # AUD-102: never blocked by an existing generic row
                continue
            k = _dedupe_slot_key(row)
            if k is not None and k in existing:
                dropped_live += 1
                continue
            kept.append(row)
        deduped = kept
    if in_batch or dropped_live:
        print(f"[slot-dedupe] {account_key}: dropped {in_batch} in-batch duplicate(s) "
              f"and {dropped_live} slot(s) already live; staged {len(deduped)}")
    return deduped


def _story_incident_targets(store, calendar_gym_key, first, last):
    """Read validated persisted targets; no prose or fuzzy slot substitution."""
    from .fixer_business_seed import STORY_SOURCE, story_pointer_matches
    response = store._client().get(store._rest("support_tickets"), params={
        "product": "eq.echo", "source": "eq.ops_fix",
        "verification_before->fixer->source_event->>source": f"eq.{STORY_SOURCE}",
        "verification_before->fixer->source_event->>calendar_gym_key": f"eq.{calendar_gym_key}",
        "verification_before->fixer->source_event->>post_date": [f"gte.{first}", f"lte.{last}"],
        "select": "id,client_id,raw_text,verification_before", "limit": "1000"},
        headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise ValueError("Story provenance unavailable")
    tickets = response.json()
    if not isinstance(tickets, list) or len(tickets) >= 1000:
        raise ValueError("partial Story provenance")
    targets = set()
    for ticket in tickets:
        plan = ticket["verification_before"]["fixer"]["business_check"]
        if not story_pointer_matches(ticket, plan["request_key"], plan["params"]):
            raise ValueError("invalid Story provenance")
        p = plan["params"]
        if p["calendar_gym_key"] != calendar_gym_key or not first <= p["post_date"] <= last:
            raise ValueError("Story provenance scope mismatch")
        targets.add((p["row_id"], p["created_at"]))
    return targets


def _story_slot(row):
    # Calendar defaults a missing time_slot to morning. Null slot_index is a
    # genuine single-slot identity; never collapse a numbered second Story.
    return (row.get("account"), row.get("post_date"), row.get("format"),
            row.get("time_slot") or "morning", row.get("slot_index"))


def _reconcile_story_media_holds(store, calendar_gym_key, proposed):
    """Use the retained UUID for ordinary rerenders, never a READY sibling."""
    stories = [row for row in proposed if row.get("format") == "story"]
    if not stories:
        return proposed, []
    try:
        dates = [row["post_date"] for row in stories]
        existing = store.rows_in_range(calendar_gym_key, min(dates), max(dates))
        if (not isinstance(existing, list) or len(existing) >= 1000
                or any(not isinstance(r, dict) or r.get("gym_id") != calendar_gym_key
                       or (r.get("format") == "story" and not
                           {"id", "account", "post_date", "status", "variant_status", "created_at",
                            "image_url", "media_not_ready_reason"}.issubset(r)) for r in existing)):
            raise ValueError("partial Story hold read")
    except Exception as exc:
        print(f"[calendar] Story hold reconciliation unconfirmed: {type(exc).__name__}; existing rows retained")
        return [row for row in proposed if row.get("format") != "story"], []
    retained = [row for row in existing if isinstance(row, dict) and row.get("format") == "story"
                and row.get("gym_id") == calendar_gym_key
                and row.get("variant_status") == "active"]
    protected_ids = set()
    if calendar_gym_key == "lasso":
        pending_ids = [row["id"] for row in retained if row.get("status") == "pending"]
        try:
            reader = getattr(store, "managed_lasso_paired_story_ids")
            protected_ids = reader(pending_ids)
            if (not isinstance(protected_ids, set)
                    or not protected_ids.issubset(set(pending_ids))):
                raise ValueError("managed Story registry returned invalid IDs")
        except Exception as exc:
            print(f"[calendar] LASSO Story registry unconfirmed: {type(exc).__name__}; "
                  "retained pending rows protected")
            protected_ids = set(pending_ids)
    output, recovered = [], []
    for row in proposed:
        if row.get("format") == "story" and sum(_story_slot(r) == _story_slot(row) for r in stories) != 1:
            print("[calendar] ambiguous proposed Story slot; no replacement inserted")
            continue
        targets = [old for old in retained if _story_slot(old) == _story_slot(row)]
        if not targets:
            output.append(row)
            continue
        _record_confirmed_story_holds(calendar_gym_key, targets)
        if len(targets) != 1:
            print("[calendar] ambiguous retained Story target; no replacement inserted")
            continue
        if targets[0].get("status") != "pending":
            # The planner never creates a sibling over a human/publisher row,
            # even with the optional content-dedupe belt disabled.
            continue
        if targets[0]["id"] in protected_ids:
            # This exact Story/feed pair was independently reviewed and staged
            # through the guarded RPC. A later planner rerender may not replace
            # its image or clear its feed hold, even if the hold already lifted.
            continue
        if _is_story_media_hold(row):
            # Same failed slot: keep its source generation and retry seed, even
            # if the latest error wording differs.
            recovered.append(targets[0])
            continue
        try:
            saved = store.recover_story_media_hold(calendar_gym_key, targets[0], row)
        except Exception as exc:
            print(f"[calendar] retained Story recovery unconfirmed: {type(exc).__name__}")
            saved = None
        if saved:
            recovered.append(saved)
        # A refused/raced recovery never inserts a second candidate over the
        # retained target or changes a human's decision.
    return output, recovered


def _is_story_media_hold(row):
    return (isinstance(row, dict) and row.get("format") == "story"
            and row.get("status") == "pending" and row.get("image_url") in (None, "")
            and isinstance(row.get("media_not_ready_reason"), str)
            and row["media_not_ready_reason"].startswith("Story media not ready:"))


def _record_confirmed_story_holds(calendar_gym_key, rows):
    """Only confirmed shared rows may originate a support incident."""
    try:
        if not config.ops_fix_triage_enabled() or not config.ops_alerts_enabled():
            return
        from . import ops_alerts
        for row in rows:
            if _is_story_media_hold(row) and row.get("gym_id") == calendar_gym_key:
                ops_alerts.record_story_hold(row)
    except Exception as exc:
        # The primary portal-visible held row is already durable. Never turn a
        # failed support seed into a failed calendar insertion or a fake ticket.
        print(f"[calendar] Story support provenance unconfirmed: {type(exc).__name__}; "
              "shared media holds retained for next planner retry")


def _retry_story_hold_provenance(store, calendar_gym_key, proposed):
    """Retry only persisted holds for slots a planner is actually revisiting.

    No new outbox table or worker-volume dependency: the shared held calendar
    row/reason is the actionable queue. Read failure leaves it intact. This also
    covers a restart after calendar insert but before support seed confirmation.
    """
    try:
        if not config.ops_fix_triage_enabled() or not config.ops_alerts_enabled():
            return
        held = [row for row in proposed if _is_story_media_hold(row)]
        if not held:
            return
        dates = [row["post_date"] for row in held]
        existing = store.rows_in_range(calendar_gym_key, min(dates), max(dates))
        if not isinstance(existing, list) or len(existing) >= 1000:
            raise ValueError("unconfirmed hold read")
        slot = lambda r: (r.get("account"), r.get("post_date"), r.get("format"),
                          r.get("time_slot"), r.get("slot_index"))
        wanted = {slot(row) for row in held}
        actual = [row for row in existing if isinstance(row, dict)
                  and row.get("gym_id") == calendar_gym_key and slot(row) in wanted]
        _record_confirmed_story_holds(calendar_gym_key, actual)
    except Exception as exc:
        print(f"[calendar] Story support retry read unconfirmed: {type(exc).__name__}; "
              "shared media holds retained")


def _live_slots_for(store, account_key, dates):
    """Slot keys this gym already holds in a LIVE status on those dates, or None when the
    read fails. 'deleted', 'denied' and 'killed' rows free their slot by design."""
    if not dates:
        return set()
    try:
        rows = store.rows_in_range(account_key, min(dates), max(dates))
    except Exception as e:  # noqa: BLE001 - never block staging on a lookup
        print(f"[slot-dedupe] live slot read failed for {account_key}: "
              f"{type(e).__name__}: {e}")
        return None
    # EXACTLY the statuses rows_in_range can return (its own positive allowlist at the
    # top of this file). "draft" was dead code here -- that reader never returns it -- and
    # "coach_review" WAS being returned while missing from this set, so a coach-review slot
    # read as free and a re-plan stacked on top of it. 105 forward draft rows and every
    # coach_review row were invisible to this pass.
    live = {"pending", "approved", "publishing", "published", "coach_review"}
    out = set()
    for r in (rows or []):
        if str(r.get("status") or "").strip().lower() in live:
            k = _dedupe_slot_key(r)
            if k is not None:
                out.add(k)
    return out


def _companion_group_key(row):
    """Stable sibling identity, with platform-aware fallback for legacy rows."""
    r = row or {}
    logical_post_id = str(r.get("logical_post_id") or "").strip()
    if logical_post_id:
        return ("logical_post_id", logical_post_id)
    account = str(r.get("account") or "").strip().lower()
    fmt = str(r.get("format") or "").strip().lower()
    day = str(r.get("post_date") or "")[:10]
    slot = r.get("slot_index")
    if ((account in ("instagram", "ig", "") and fmt in ("feed", "story"))
            or (account in ("facebook", "fb") and fmt == "feed")):
        return ("legacy_meta_companions", day, slot)
    return ("legacy_singleton", account, fmt, day, slot)


def _drop_companions_missing_instagram_feed(planned, filtered, *, satisfied=()):
    """Keep the Instagram feed and its planned Story coupled.

    Facebook is a best-effort mirror, not a cadence unit.  A Facebook-only belt
    refusal must not erase a valid Instagram feed (or its paired Story).  The
    Instagram feed remains the group anchor, while a Story that was planned for
    that anchor is required either in ``filtered`` or in ``satisfied`` (for an
    in-place recovered hold).
    """
    from collections import defaultdict

    def _member(row):
        r = row or {}
        return (str(r.get("format") or "").strip().lower(),
                str(r.get("account") or "").strip().lower())

    planned_members = defaultdict(set)
    kept_members = defaultdict(set)
    for row in planned or ():
        planned_members[_companion_group_key(row)].add(_member(row))
    for row in list(filtered or ()) + list(satisfied or ()):
        kept_members[_companion_group_key(row)].add(_member(row))
    instagram_accounts = {"instagram", "ig", ""}

    def _has_instagram_feed(members):
        return any(fmt == "feed" and account in instagram_accounts
                   for fmt, account in members)

    def _has_story(members):
        return any(fmt == "story" for fmt, _account in members)

    missing = set()
    for key, members in planned_members.items():
        # Non-Instagram groups are outside this companion contract.
        if not _has_instagram_feed(members):
            continue
        kept = kept_members.get(key, set())
        if not _has_instagram_feed(kept):
            missing.add(key)
            continue
        if _has_story(members) and not _has_story(kept):
            missing.add(key)
    if not missing:
        return filtered
    return [row for row in (filtered or ())
            if _companion_group_key(row) not in missing]


def _instagram_feed_slots(rows):
    return {(str((row or {}).get("post_date") or "")[:10],
             (row or {}).get("slot_index"))
            for row in (rows or ())
            if str((row or {}).get("post_date") or "")[:10]
            and str((row or {}).get("format") or "").strip().lower() == "feed"
            and str((row or {}).get("account") or "").strip().lower()
            in ("instagram", "ig", "")}


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

    # Instagram feed + paired Story are the required generated companion set.
    # Facebook is an independently filtered mirror: losing only that mirror must not
    # erase a valid Instagram cadence unit. Decide the Instagram feed first, then
    # remove every sibling only when that primary feed is blocked.
    # Production ENG proved why this must be atomic: the verbatim belt removed the IG
    # and FB feeds for Oct 19/28/29 while their exempt Stories survived, leaving 27 of
    # 30 feed slots and a misleadingly full-looking calendar.
    decisions = []
    blocked_primary_groups = set()

    def _is_instagram_feed(row):
        r = row or {}
        return (str(r.get("format") or "").strip().lower() == "feed"
                and str(r.get("account") or "").strip().lower()
                in ("instagram", "ig", ""))

    batch_dates_by_hash = {}   # verbatim hash -> set of post_dates staged in THIS batch
    for row in payload:
        caption = str(row.get("caption") or "")
        post_date = str(row.get("post_date") or "")[:10]
        if _is_story(row) or _is_gbp_photo_drop(row):
            decisions.append((row, True))
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
                    if _is_instagram_feed(row):
                        blocked_primary_groups.add(_companion_group_key(row))
                    decisions.append((row, False))
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
                    if _is_instagram_feed(row):
                        blocked_primary_groups.add(_companion_group_key(row))
                    decisions.append((row, False))
                    continue
                if h:
                    batch_dates_by_hash.setdefault(h, set()).add(post_date)
            except Exception:
                pass
        decisions.append((row, True))
    allowed_primary_groups = {
        _companion_group_key(row) for row, allowed in decisions
        if allowed and _is_instagram_feed(row)
    }
    fully_blocked_primary_groups = blocked_primary_groups - allowed_primary_groups
    return [row for row, allowed in decisions
            if allowed
            and _companion_group_key(row) not in fully_blocked_primary_groups]


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


def _media_stage_belt(store, account_key, payload, *, alert=None,
                      skip_wipeable_dates=()):
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
            skip_wipeable_dates=skip_wipeable_dates,
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


def _prune_by_cadence_occupancy(store, account_key, months, rows):
    """Drop incoming rows that collide with a human-owned cadence slot.

    Occupancy is per (post_date, account, format) up to resolve_posts_per_day
    slots. A 2x day with an approved morning post can still receive an evening
    post; a 1x day (or 2x->1x reshape, capacity 1) treats any owned feed as
    filling the cell. Approved/published rows are never replaced.     Denied/killed
    rows do not occupy capacity so a replacement may land.
    """
    from .cadence import resolve_posts_per_day
    from collections import defaultdict

    def capacity_for(row):
        """Third capacity exists only for LASSO feed/story rows in the dated window."""
        day_key = str(row.get("post_date") or "")[:10]
        try:
            base_capacity = resolve_posts_per_day(account_key, store, day=day_key)
        except TypeError:
            # Compatibility for injected legacy resolvers in offline callers.
            base_capacity = resolve_posts_per_day(account_key, store)
        if (str(account_key).strip().lower() == "lasso"
                and str(row.get("format") or "feed").strip().lower() in ("feed", "story")):
            try:
                if config.lasso_three_feed_enabled() or \
                        config.lasso_summit_daily_enabled(day_key):
                    return max(base_capacity, 3)
            except (TypeError, ValueError):
                pass
        return min(int(base_capacity or 1), 2)

    existing = []
    for month in months:
        existing.extend(store.list_month(account_key, month) or [])
    occupied = defaultdict(set)
    active_count = defaultdict(int)
    prior = defaultdict(list)
    for row in existing:
        status = str(row.get("status") or "").lower()
        if not status or status in _WIPEABLE_STATUSES:
            continue
        key = _slot_key(row)
        prior[key].append(row)
        if status in ("denied", "killed"):
            continue
        active_count[key] += 1
        ordinal = row.get("slot_index")
        capacity = capacity_for(row)
        if ordinal not in range(capacity):
            ordinal = next((i for i in range(capacity) if i not in occupied[key]), 0)
        occupied[key].add(ordinal)
    kept = []
    for row in rows or []:
        key = _slot_key(row)
        capacity = capacity_for(row)
        ordinal = row.get("slot_index")
        if ordinal is None:
            ordinal = next((i for i in range(capacity) if i not in occupied[key]), 0)
            # LASSO editorial still assigns a concrete ordinal to legacy nulls
            # (two None feeds on a 2x day become 0 then 1). Client None stays
            # None on the default cell so _held_slot_key (None != 0) still
            # blocks a pending media-hold replacement at insert time.
            if ordinal != 0 or str(account_key).strip().lower() == "lasso":
                row = dict(row, slot_index=ordinal)
        if (ordinal not in range(capacity) or active_count[key] >= capacity
                or ordinal in occupied[key]):
            continue
        if any((row.get("caption") and row.get("caption") == old.get("caption"))
               or (row.get("image_url") and row.get("image_url") == old.get("image_url"))
               for old in prior.get(key, ())):
            continue
        kept.append(row)
        occupied[key].add(ordinal)
        active_count[key] += 1
    return kept, len(prior)


def preserve_and_prune(store, account_key, months, rows):
    """Shared guard for every delete-then-insert rebuild lane (client month, real month,
    demo->real mirror). Reads the HUMAN OWNED slots the gym already has across `months`
    and drops any incoming row that would land on one of them, so a rebuild that keeps a
    client's approved post never also inserts a duplicate draft into the same cell.

    When the store can list_month, occupancy is per cadence slot so a 2x day with an
    approved morning post can still receive an evening post. Stores that only expose
    locked_slots keep the conservative (post_date, account, format) cell lock.
    Returns (kept_rows, locked_slot_count). Safe when the store lacks locked_slots (a test
    fake): then nothing is locked and every row is kept. Never raises out (a read failure
    falls back to keeping all rows, matching the old behavior)."""
    if callable(getattr(store, "list_month", None)):
        try:
            return _prune_by_cadence_occupancy(store, account_key, months, rows)
        except Exception:  # noqa: BLE001 - a read failure must not block the rebuild
            pass
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


def _preserve_and_prune_strict(store, account_key, months, rows):
    """Live, fail-closed ownership check at the final prevalidated write barrier.

    At one post/day any owned cell retains the legacy whole-cell lock. At
    multi-slot cadence only the exact ordinal is occupied; a legacy null owns
    ordinal zero. No content, caption or time bucket can bypass ownership.
    """
    reader = getattr(store, "list_month_strict", None)
    if not callable(reader):
        raise RuntimeError("authoritative cadence reader unavailable")
    from .cadence import resolve_posts_per_day
    locked = set()
    capacities = {}

    def capacity(day):
        if day not in capacities:
            value = int(resolve_posts_per_day(account_key, store, day=day) or 1)
            if value < 1:
                raise RuntimeError("invalid cadence capacity")
            capacities[day] = value
        return capacities[day]

    for month in months:
        retained = reader(account_key, month)
        if not isinstance(retained, list):
            raise RuntimeError("live cadence read was not authoritative")
        for row in retained:
            status = str(row.get("status") or "").lower()
            if not status or status in _WIPEABLE_STATUSES:
                continue
            key = _slot_key(row)
            slots = capacity(key[0])
            ordinal = row.get("slot_index")
            if slots > 1 and ordinal is not None and ordinal >= slots:
                raise RuntimeError("owned cadence ordinal invalid")
            locked.add((key, (ordinal or 0) if slots > 1 else None))
    kept = []
    for row in rows:
        key = _slot_key(row)
        slots = capacity(key[0])
        ordinal = row.get("slot_index")
        if slots > 1 and ordinal is not None and (
                type(ordinal) is not int or ordinal not in range(slots)):
            raise RuntimeError("proposed cadence ordinal invalid")
        if (key, (ordinal or 0) if slots > 1 else None) not in locked:
            kept.append(row)
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
