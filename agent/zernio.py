"""
Zernio API client + pure response mappers.

Echo brokers Zernio (the social-posting vendor); the portal NEVER sees the Zernio key. All external
HTTP lives in ZernioClient (mirrors the gbp_check/opus_ingest requests pattern: Bearer auth, 30s
timeout, scrub-on-error). The mappers (map_status, map_pages) are PURE and unit-tested against
real-shape fixtures captured from the live API, so the fold + field-rename logic is provable without
a network call.

Zernio model: Team -> Profile (tenant boundary; one per gym) -> Account (an IG/FB connection).
Field renames Echo owns (portal contract never changes): Zernio `authUrl`->`oauth_url`,
`metadata.profileData.username`->`handle`, page `_id`->`id`. Expiry is DERIVED from
`connectedAt` + `metadata.expires_in`, or an explicit `intentionalDisconnectAt` / `isActive:false`.
"""

import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from . import config

PLATFORMS = ("instagram", "facebook")
# Platforms a gym may CONNECT via the OAuth flow. Broader than PLATFORMS (which drives status
# mapping + posting, IG/FB only): Google Business connects + publishes through Zernio but has no
# IG/FB-style status row, so it lives here, not in PLATFORMS. Zernio's key is one word, lowercase
# ('googlebusiness'), verified live against api.zernio.com 2026-08-18.
CONNECT_PLATFORMS = ("instagram", "facebook", "googlebusiness")
# Platforms the portal reflects a CONNECTED/EXPIRED status for. Includes googlebusiness so the
# self-serve connect page can show it connected on a return visit (Zernio's accounts[] carries
# the googlebusiness account once linked). Posting still keys off PLATFORMS.
STATUS_PLATFORMS = ("instagram", "facebook", "googlebusiness")

_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".avi")


def match_profile_id(profiles, name):
    """The `_id` of the profile in `profiles` whose name matches `name`, or None.

    Pure: exact match wins, then a case-insensitive fallback (Zernio names are user-set). Kept a
    module function so both find_profile_id and find_profile_id_any share one matcher and it stays
    unit-testable over a plain list.
    """
    if not name:
        return None
    want = str(name).strip()
    if not want:
        return None
    want_lower = want.lower()
    fallback = None
    for p in profiles or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("_id") or p.get("id")
        pname = p.get("name")
        if not pid or not pname:
            continue
        if str(pname) == want:
            return str(pid)
        if fallback is None and str(pname).strip().lower() == want_lower:
            fallback = str(pid)
    return fallback


def _media_type(url):
    """The Zernio MediaItem `type` for a media URL: video/gif by extension, else image."""
    path = (url or "").split("?", 1)[0].lower()
    if path.endswith(_VIDEO_EXTS):
        return "video"
    if path.endswith(".gif"):
        return "gif"
    return "image"


def _to_utc_iso(iso_ts):
    """The timestamp re-expressed in UTC ('...Z' suffix). A naive timestamp is assumed
    UTC; an unparseable one is passed through untouched (Zernio validates)."""
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return iso_ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ZernioError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"zernio {status}: {detail}")


class ZernioClient:
    """Thin Zernio v1 client. `http` is injectable for tests (defaults to lazy `requests`)."""

    def __init__(self, api_key=None, base=None, http=None):
        self.api_key = api_key if api_key is not None else os.environ.get(config.ZERNIO_API_KEY_ENV, "")
        self.base = (base or config.zernio_api_base()).rstrip("/")
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, matches the repo pattern
        return requests

    def _get(self, path, params=None, headers=None):
        hdrs = {"Authorization": f"Bearer {self.api_key}"}
        if headers:
            hdrs.update(headers)
        r = self._client().get(
            self.base + path,
            params=params or {},
            headers=hdrs,
            timeout=30,
        )
        if r.status_code >= 400:
            raise ZernioError(r.status_code, (r.text or "")[:200])
        return r.json()

    def _post(self, path, payload, headers=None):
        hdrs = {"Authorization": f"Bearer {self.api_key}"}
        if headers:
            hdrs.update(headers)
        r = self._client().post(
            self.base + path,
            json=payload,
            headers=hdrs,
            timeout=30,
        )
        if r.status_code >= 400:
            raise ZernioError(r.status_code, (r.text or "")[:200])
        return r.json()

    def _delete(self, path):
        r = self._client().delete(
            self.base + path,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        )
        if r.status_code >= 400:
            raise ZernioError(r.status_code, (r.text or "")[:200])
        # a 204/empty body is a valid success for a delete
        try:
            return r.json()
        except Exception:
            return {}

    def disconnect_account(self, account_id):
        """DELETE /v1/accounts/{id}: disconnect AND remove a connected social account
        from its profile (verified against docs.zernio.com — one call, full removal).
        Lets a gym owner who connected the wrong account (e.g. a personal/other IG)
        clear it and reconnect the right one."""
        return self._delete(f"/v1/accounts/{account_id}")

    # ---- reads --------------------------------------------------------------
    def connect_url(self, profile_id, platform, redirect_url=None, headless=True):
        """GET /v1/connect/{platform}?profileId=...&headless=true&redirect_url=... -> {authUrl}.

        redirect_url is the post-OAuth return target: after the gym owner approves, Zernio
        redirects the browser THERE. When it is OMITTED, Zernio sends them to its OWN
        dashboard (zernio.com/dashboard) with billing prompts — a client must never see
        that, so the caller (portal or an env fallback) always supplies the LASSO portal's
        return URL. headless=true keeps the flow inside our own chrome. Same call for
        googlebusiness (it rides this path). Return shape ({authUrl}) is unchanged."""
        params = {"profileId": profile_id}
        if headless:
            params["headless"] = "true"
        if redirect_url:
            params["redirect_url"] = redirect_url
        return self._get(f"/v1/connect/{platform}", params)

    def list_accounts(self, profile_id):
        """GET /v1/accounts?profileId=... -> {accounts:[...]}."""
        return self._get("/v1/accounts", {"profileId": profile_id})

    #: Zernio's page size for /v1/profiles.
    _PROFILE_PAGE = 100
    #: Hard stop. Bounded by REQUESTS, not by a server-supplied count, because the stop
    #: condition must never depend on a field the API may not send. 50 pages = 5000
    #: profiles, far past any real LASSO org, and at worst 50 x 30s rather than a hang.
    _PROFILE_MAX_PAGES = 50

    def list_profiles(self):
        """GET /v1/profiles -> {profiles:[{_id,name,...}], total, skip, limit}, ALL pages.

        LASSO is already provisioned in Zernio, so profiles pre-exist; the connect path must
        REUSE an existing profile, never re-create it (Zernio 409s on a duplicate name). Verified
        live 2026-08-06 against api.zernio.com: `{"profiles":[{"_id"(24 char),"name",...}]}`.

        PAGINATED (2026-08-28): this used to send a bare {"limit": 100} and read one page. Every
        alias lookup runs through here, so the moment the org passed 100 profiles every gym whose
        profile sorted past the first page would silently miss and get a duplicate created under
        its account_key — the Zanshin bug, re-opened for the whole roster at once.

        The stop condition is A SHORT PAGE, never `total`. The live 2026-08-06 response recorded
        above carries no `total` field, so a total-driven loop would read one page and quietly do
        nothing — an inert fix that still looks correct. A full page means "there may be more",
        a short or empty page means "that was the end", and that holds whether or not the server
        sends a count. `total` is used ONLY as an early exit once we already have that many rows.

        Also guards a server that ignores `skip`: if a page repeats ids we have already seen, we
        stop rather than looping to the page cap collecting duplicates. Same return shape.
        """
        first = self._get("/v1/profiles", {"limit": self._PROFILE_PAGE}) or {}
        rows = list(first.get("profiles") or [])
        try:
            total = int(first.get("total") or 0)
        except (TypeError, ValueError):
            total = 0

        def _ids(items):
            return {str(p.get("_id")) for p in items
                    if isinstance(p, dict) and p.get("_id")}

        seen = _ids(rows)
        pages = 1
        # A FULL page is the only reason to ask for another one.
        while len(rows) >= self._PROFILE_PAGE * pages and pages < self._PROFILE_MAX_PAGES:
            if total and len(rows) >= total:
                break
            nxt = self._get("/v1/profiles", {"limit": self._PROFILE_PAGE,
                                             "skip": len(rows)}) or {}
            page = nxt.get("profiles") or []
            fresh = [p for p in page
                     if not (isinstance(p, dict) and str(p.get("_id")) in seen)]
            if not fresh:
                # Empty page, or a server ignoring `skip` and replaying page one.
                break
            seen |= _ids(fresh)
            rows.extend(fresh)
            pages += 1
        if pages >= self._PROFILE_MAX_PAGES:
            print(f"[zernio] list_profiles hit the {self._PROFILE_MAX_PAGES} page cap at "
                  f"{len(rows)} profiles; alias lookups may be matching an incomplete set.")
        out = dict(first)
        out["profiles"] = rows
        return out

    def find_profile_id(self, name):
        """The `_id` of the existing Zernio profile whose name matches `name`, or None.

        Match is exact first, then case-insensitive (Zernio names are user-set, e.g. "lasso").
        Pure over the list response so it stays testable with a fake http client.
        """
        return match_profile_id((self.list_profiles() or {}).get("profiles") or [], name)

    def find_profile_id_any(self, *names):
        """The `_id` of the first existing Zernio profile matching ANY of `names`, or None.

        WHY (Zanshin/Pete, 2026-08-27): a gym's Zernio profile is often pre-created by ops under a
        HUMAN name ("Zanshin Fitness"), but Echo's connect path looked it up by the account_key only
        ("zanshinfitness630e22"). No match -> Echo CREATED a second, empty profile under the account_key
        name, and social connections could strand on the wrong profile (the portal shows not-connected
        while Zernio is healthy under the other one). Trying every known alias (account_key, the gym's
        display name, its gym name) finds the real, populated profile FIRST, so no duplicate is ever
        created. One `/v1/profiles` read serves all the candidates. Exact match wins over a
        case-insensitive one, and earlier candidates win over later ones.
        """
        profiles = (self.list_profiles() or {}).get("profiles") or []
        for n in names:
            pid = match_profile_id(profiles, n)
            if pid:
                return pid
        return None

    def list_facebook_pages(self, account_id):
        """GET /v1/accounts/{id}/facebook-page -> {pages:[{_id,name}]}."""
        return self._get(f"/v1/accounts/{account_id}/facebook-page")

    # ---- headless OAuth finalization (docs: "Standard vs Headless Mode") -----
    # In headless mode Zernio does NOT auto-create the account after OAuth: it
    # redirects the browser back to our redirect_url with tempToken/userProfile/
    # step/connect_token (GBP: pendingDataToken, step=select_location) and the
    # integrator must call the selection endpoints below to finalize. Without
    # them every Facebook/Google grant is silently dropped (Hill Country,
    # 2026-08-26). Tokens are FORWARDED, never logged.

    def _connect_headers(self, connect_token):
        """The X-Connect-Token header dict for a headless selection call, or None.
        Docs: 'Use the X-Connect-Token header if connecting via API key' — Echo
        always connects via API key, so the token is forwarded when present."""
        return {"X-Connect-Token": str(connect_token)} if connect_token else None

    def fb_pages_after_oauth(self, profile_id, temp_token, user_profile=None,
                             connect_token=None):
        """GET /v1/connect/facebook/select-page?profileId=..&tempToken=.. ->
        {pages:[{id,name}]}: the FB Pages the user can manage after OAuth.
        temp_token/user_profile come from the OAuth redirect params; user_profile
        is passed through as the (decoded) JSON string when provided."""
        params = {"profileId": str(profile_id), "tempToken": str(temp_token)}
        if user_profile:
            params["userProfile"] = user_profile
        return self._get("/v1/connect/facebook/select-page", params,
                         headers=self._connect_headers(connect_token))

    def fb_select_page(self, profile_id, page_id, temp_token, user_profile=None,
                       connect_token=None):
        """POST /v1/connect/facebook/select-page: finalize the headless flow by
        saving the selected Page — THIS is the call that creates the account on
        the profile. Body per docs: {profileId, pageId, tempToken, userProfile}
        where userProfile is the DECODED JSON object from the redirect param."""
        payload = {"profileId": str(profile_id), "pageId": str(page_id),
                   "tempToken": str(temp_token)}
        if user_profile is not None:
            payload["userProfile"] = user_profile
        return self._post("/v1/connect/facebook/select-page", payload,
                          headers=self._connect_headers(connect_token))

    def gbp_locations_after_oauth(self, profile_id, pending_data_token,
                                  connect_token=None):
        """GET /v1/connect/googlebusiness/locations: the GBP locations the user
        can manage after OAuth. Uses pendingDataToken (from the OAuth callback
        redirect, step=select_location) WITHOUT consuming it, so it remains
        valid for select-location."""
        params = {"profileId": str(profile_id),
                  "pendingDataToken": str(pending_data_token)}
        return self._get("/v1/connect/googlebusiness/locations", params,
                         headers=self._connect_headers(connect_token))

    def gbp_select_location(self, profile_id, location_id, pending_data_token,
                            account_id=None, connect_token=None):
        """POST /v1/connect/googlebusiness/select-location: finalize the headless
        GBP flow by saving the selected location (creates the account). Body per
        docs: {profileId, locationId, pendingDataToken, accountId?} — tokens and
        profile data are stored server-side, so no userProfile is sent. accountId
        (the owning 'accounts/123' resource, returned per-location by the list
        call) is recommended for accounts owning many locations."""
        payload = {"profileId": str(profile_id), "locationId": str(location_id),
                   "pendingDataToken": str(pending_data_token)}
        if account_id:
            payload["accountId"] = str(account_id)
        return self._post("/v1/connect/googlebusiness/select-location", payload,
                          headers=self._connect_headers(connect_token))

    def create_post(self, account_id, body, media_urls=None, scheduled_for=None,
                    page_id=None, platform=None, story=False):
        """POST /v1/posts: publish (or schedule) ONE post to one connected account.

        Payload verified against the Zernio OpenAPI spec (docs.zernio.com/api/openapi,
        createPost). The 2026-08-13 shape:
          * content            the caption (NOT `body`)
          * platforms          REQUIRED for non-draft posts: [{platform, accountId,
                               platformSpecificData?}] (a top-level accountId is ignored
                               and the API 400s "Missing required field: platforms")
          * mediaItems         [{type: image|video|gif, url}] (NOT `media`)
          * scheduledFor       ISO8601 to schedule; when ABSENT the post becomes a DRAFT
                               unless publishNow=true — so immediate sends set publishNow
          * platformSpecificData.contentType='story' publishes an IG/FB Story;
            platformSpecificData.pageId targets a specific Facebook Page.
        Idempotency: every call carries a fresh x-request-id (UUID4) so a same-request
        retry returns the original post instead of double-posting; Zernio additionally
        409s exact duplicates within 24h (the caller maps that to already-posted).
        Returns the created post JSON (carries the post id)."""
        entry = {"accountId": str(account_id)}
        if platform:
            entry["platform"] = str(platform)
        psd = {}
        if story:
            psd["contentType"] = "story"
        if page_id:
            psd["pageId"] = str(page_id)
        if psd:
            entry["platformSpecificData"] = psd
        payload = {"content": body or "", "platforms": [entry]}
        urls = [u for u in (media_urls or []) if u]
        if urls:
            payload["mediaItems"] = [{"type": _media_type(u), "url": u} for u in urls]
        if scheduled_for:
            # Normalize to UTC so the ISO offset and the `timezone` field can never
            # disagree (probed live: Zernio parses the offset correctly, but sending
            # both consistent removes the ambiguity entirely).
            payload["scheduledFor"] = _to_utc_iso(scheduled_for)
            payload["timezone"] = "UTC"
        else:
            payload["publishNow"] = True
        headers = {"x-request-id": str(_uuid.uuid4())}
        return self._post("/v1/posts", payload, headers=headers)

    def create_post_raw(self, payload, *, draft=False, publish_now=True):
        """POST /v1/posts with a FULLY-BUILT body (content + mediaItems + platforms),
        for platforms whose platformSpecificData the caller assembles itself (GBP). The
        GBP payload builder (agent/gbp.build_post_payload) produces `payload`; this only
        adds send-mode + a fresh idempotency id, never reshapes the platforms entry.

        draft=True forces isDraft (Zernio saves it, publishes NOTHING) — used by the
        autonomous build + validation so no live post is ever created. draft=False +
        publish_now sends immediately (the real armed worker path, human-tap gated
        upstream). Returns the created post JSON (carries the post id)."""
        body = dict(payload or {})
        if draft:
            body["isDraft"] = True
        elif publish_now:
            body["publishNow"] = True
        headers = {"x-request-id": str(_uuid.uuid4())}
        return self._post("/v1/posts", body, headers=headers)

    def get_post(self, post_id):
        """GET /v1/posts/{id} -> the post JSON (status + per-platform state). Read-only;
        the GBP reconcile poll (§7.2) reads this hourly for 48h after publish."""
        return self._get(f"/v1/posts/{post_id}")

    def create_gmb_media(self, account_id, image_url):
        """POST /v1/accounts/{accountId}/gmb-media — add a photo to a GBP location's
        gallery (§6.4 photo drop). SYNCHRONOUS: no webhook, no draft mode; a 2xx means
        the photo is live. The GBP worker only calls this in ARMED live mode; in the
        draft build the worker simulates it and never touches this endpoint."""
        payload = {"mediaFormat": "PHOTO", "sourceUrl": image_url}
        headers = {"x-request-id": str(_uuid.uuid4())}
        return self._post(f"/v1/accounts/{account_id}/gmb-media", payload,
                          headers=headers)

    def list_posts(self, profile_id, page=1, limit=50):
        """GET /v1/posts?profileId=... -> {posts:[...], pagination:{page,limit,total,pages}}.

        The profile's Zernio-CREATED posts (what Echo scheduled/published through
        Zernio), newest-first by scheduledFor. Pagination on THIS endpoint is
        PAGE-based (`skip` is accepted but ignored — probed live 2026-08-26).
        Each post's platforms[] entries carry the per-platform platformPostId
        (the id the metrics join keys on) alongside the Zernio post `_id` that
        content_calendar.late_post_id stores. Read-only."""
        return self._get("/v1/posts", {"profileId": profile_id,
                                       "page": int(page), "limit": int(limit)})

    def posts_window(self, profile_id, days, page_limit=50, max_pages=20):
        """The profile's Zernio-created posts covering the last `days`, as a list.

        Pages newest-first (list_posts), accumulating until a page's OLDEST
        scheduledFor is already before the window start, the pagination total is
        reached, a page comes back empty, or max_pages is hit (defensive cap).
        Mirrors analytics_window's stop rules on the page-based posts endpoint.
        A post with no parseable scheduledFor is KEPT (never dropped by the
        pager). Read-only: no writes ever issued."""
        cutoff = None
        if isinstance(days, (int, float)) and days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=float(days))
        posts = []
        page = 1
        while page <= max_pages:
            resp = self.list_posts(profile_id, page=page, limit=page_limit) or {}
            batch = [p for p in (resp.get("posts") or []) if isinstance(p, dict)]
            if not batch:
                break
            posts.extend(batch)
            if cutoff is not None:
                oldest = None
                for p in batch:
                    ts = _parse_iso(p.get("scheduledFor") or p.get("createdAt"))
                    if ts is not None and (oldest is None or ts < oldest):
                        oldest = ts
                if oldest is not None and oldest < cutoff:
                    break
            total = (resp.get("pagination") or {}).get("total")
            if isinstance(total, (int, float)) and len(posts) >= int(total):
                break
            page += 1
        return posts

    def analytics(self, profile_id, skip=0, limit=50, source=None):
        """GET /v1/analytics?profileId=... -> the analytics JSON (read-only add-on).

        Shape (probed live): {hasAnalyticsAccess, overview, accounts:[...], posts:[...],
        pagination}. `posts` is a page of up to `limit` (newest first); pass `skip` to page.
        `source` (optional, e.g. "all") asks Zernio to include EXTERNAL posts too
        (isExternal: true — posts Echo did not publish). Omitted by default so every
        existing caller's request is byte-identical to before Wave 7.
        """
        params = {"profileId": profile_id, "skip": int(skip), "limit": int(limit)}
        if source:
            params["source"] = str(source)
        return self._get("/v1/analytics", params)

    def analytics_window(self, profile_id, days, page_limit=50, max_pages=20, source=None):
        """Fetch ONE merged analytics JSON whose `posts` cover the last `days`.

        Pages through `posts` (newest first) accumulating until a page's OLDEST post is
        already before the window start, or `pagination.total` is reached, or a page comes
        back empty, or `max_pages` is hit (defensive cap; flagged as `_pages_capped`). The
        first page's top-level fields (hasAnalyticsAccess, overview, accounts, pagination)
        are kept as is; only `posts` accumulate. Read-only: no writes ever issued.

        A post with no parseable publishedAt is KEPT (never dropped by the pager) so the
        pure mapper's in-window filter is the single place inclusion is decided.
        """
        cutoff = None
        if isinstance(days, (int, float)) and days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=float(days))

        first = self.analytics(profile_id, skip=0, limit=page_limit, source=source) or {}
        merged = dict(first)
        posts = list(first.get("posts") or [])
        pagination = first.get("pagination") or {}
        pages_capped = False

        def _page_oldest(page_posts):
            oldest = None
            for p in page_posts:
                if not isinstance(p, dict):
                    continue
                ts = _parse_iso(p.get("publishedAt"))
                if ts is not None and (oldest is None or ts < oldest):
                    oldest = ts
            return oldest

        last_posts = posts
        page = 1
        while True:
            # Stop once the newest-first stream has crossed the window boundary.
            if cutoff is not None:
                oldest = _page_oldest(last_posts)
                if oldest is not None and oldest < cutoff:
                    break
            total = pagination.get("total")
            if not isinstance(total, (int, float)) or len(posts) >= int(total):
                break
            if page >= max_pages:
                pages_capped = True
                break
            nxt = self.analytics(profile_id, skip=len(posts), limit=page_limit, source=source) or {}
            more = list(nxt.get("posts") or [])
            if not more:
                break
            posts.extend(more)
            last_posts = more
            pagination = nxt.get("pagination") or pagination
            page += 1

        merged["posts"] = posts
        merged["_pages_capped"] = pages_capped
        return merged

    # ---- inbox + demographics reads (Wave 8; READ ONLY — nothing in this
    # section ever replies, hides, deletes, likes, or writes anything) --------
    def list_inbox_comments(self, profile_id, limit=50, platform=None):
        """GET /v1/inbox/comments?profileId=... -> {data:[{id, accountId,
        accountUsername, platform, content(post caption), createdTime,
        permalink, commentCount, likeCount}], pagination, meta}. Verified live
        2026-08-26 against topfuel. Posts WITH comments carry commentCount>0;
        the thread itself comes from inbox_post_comments. Cached ~10 min server
        side (fine for a daily sweep)."""
        params = {"profileId": profile_id, "limit": int(limit)}
        if platform:
            params["platform"] = str(platform)
        return self._get("/v1/inbox/comments", params)

    def inbox_post_comments(self, post_id, account_id, limit=25):
        """GET /v1/inbox/comments/{postId}?accountId=... -> {comments:[{id,
        message, createdTime, from:{name, username, isOwner}, replyCount,
        replies:[...], isHidden, url, platform}], pagination, meta}. Verified
        live 2026-08-26. READ ONLY."""
        return self._get(f"/v1/inbox/comments/{post_id}",
                         {"accountId": account_id, "limit": int(limit)})

    def list_inbox_mentions(self, profile_id, limit=25):
        """GET /v1/inbox/mentions?profileId=... -> {data:[...], pagination,
        meta}. READ ONLY."""
        return self._get("/v1/inbox/mentions",
                         {"profileId": profile_id, "limit": int(limit)})

    def list_inbox_reviews(self, profile_id, limit=25):
        """GET /v1/inbox/reviews?profileId=... -> {data:[{id, platform,
        accountId, reviewer:{name}, text, created, hasReply, reply?, rating?,
        reviewUrl}], pagination, summary}. Facebook + Google Business reviews
        aggregated. Verified live 2026-08-26. READ ONLY."""
        return self._get("/v1/inbox/reviews",
                         {"profileId": profile_id, "limit": int(limit)})

    def instagram_demographics(self, account_id, metric="follower_demographics",
                               timeframe="this_month", breakdown=None):
        """GET /v1/analytics/instagram/demographics?accountId=... ->
        {success, accountId, platform, metric, timeframe, demographics:{age,
        city, country, gender}, note}. `metric` is follower_demographics or
        engaged_audience_demographics. Requires 100+ followers and the
        Analytics add-on. READ ONLY."""
        params = {"accountId": account_id, "metric": str(metric),
                  "timeframe": str(timeframe)}
        if breakdown:
            params["breakdown"] = str(breakdown)
        return self._get("/v1/analytics/instagram/demographics", params)

    # ---- media uploads (podcast library lane; docs: /guides/media-uploads) ---
    def media_generate_upload_link(self, filename, content_type):
        """POST /v1/media/presign {filename, contentType} -> {uploadUrl,
        publicUrl}. The presigned-upload flow from the Zernio media guide:
        PUT the bytes to uploadUrl, then reference publicUrl in mediaItems.
        Uploads sit in temporary storage ~7 days until a post using them
        publishes, so callers presign near stage/publish time, never weeks
        ahead."""
        return self._post("/v1/media/presign",
                          {"filename": str(filename),
                           "contentType": str(content_type)})

    def media_upload_file(self, upload_url, path, content_type):
        """Streamed PUT of a local file to a presigned uploadUrl (up to 5 GB per
        the media guide; a podcast clip is ~250 MB, streamed so it never sits in
        RAM). The presigned URL carries its own auth — no Bearer header. Raises
        ZernioError on a non-2xx."""
        with open(path, "rb") as fh:
            r = self._client().put(upload_url, data=fh,
                                   headers={"Content-Type": str(content_type)},
                                   timeout=600)
        if r.status_code >= 400:
            raise ZernioError(r.status_code, (r.text or "")[:200])
        return True

    def media_check_upload_status(self, public_url, tries=5, wait=2.0,
                                  sleeper=None):
        """True once the uploaded object is fetchable at its publicUrl (HEAD
        2xx/3xx), polling up to `tries` with `wait`s between. False when it
        never reports ready — the caller must NOT stage a post around media
        that is not ready (publish_guard's media_missing rail backs this up at
        publish time)."""
        import time as _time
        sleeper = sleeper or _time.sleep
        for attempt in range(max(1, int(tries))):
            try:
                r = self._client().head(public_url, timeout=30)
                if r.status_code < 400:
                    return True
            except Exception:  # noqa: BLE001 - a transient HEAD error is a retry
                pass
            if attempt < tries - 1:
                sleeper(wait)
        return False

    # ---- writes (provisioning) ---------------------------------------------
    def create_profile(self, name):
        """POST /v1/profiles {name} -> {..._id}. Per-gym provisioning.

        Only reached when find_profile_id found NO existing profile of that name — Zernio 409s
        ("profile_name_conflict") on a duplicate, so the connect path finds-before-create and
        falls back to find on a 409. Never a read path.
        """
        return self._post("/v1/profiles", {"name": name})


# ---------------------------------------------------------------------------
# PURE mappers — no I/O. Provable against real-shape fixtures.
# ---------------------------------------------------------------------------

def _parse_iso(s):
    """Parse a Zernio ISO8601 timestamp (e.g. '2026-07-29T13:14:04.205Z') to aware UTC, or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def account_state(acct, now=None):
    """
    Reduce one Zernio account dict to 'connected' | 'expired' | 'not_connected'.
    'expired' == our amber "Needs Reconnect". Precedence: an explicit disconnect or inactive flag
    wins; then a computed token expiry (connectedAt + expires_in); else connected.
    """
    if not isinstance(acct, dict):
        return "not_connected"
    if acct.get("intentionalDisconnectAt"):
        return "expired"
    if acct.get("isActive") is False or acct.get("enabled") is False:
        return "expired"
    md = acct.get("metadata") or {}
    # A DEAD-TOKEN signal is a NEGATIVE and takes precedence over the optimistic "a row = connected"
    # rule below (audit 2026-08-28): Zernio marks a revoked/expired grant with an explicit flag or
    # an error string, and reading that as connected pushes posts at a token the vendor will reject.
    # We only trip on an UNAMBIGUOUS truthy signal (never on absence), so a list that merely omits
    # these fields still reads connected — the anti-flap rule the IG fix depends on is preserved.
    if acct.get("tokenExpired") is True or acct.get("needsReconnect") is True \
            or md.get("tokenExpired") is True or md.get("needsReconnect") is True:
        return "expired"
    _st = str(acct.get("status") or acct.get("connectionStatus")
              or md.get("status") or "").strip().lower()
    if _st in ("expired", "disconnected", "revoked", "error", "reconnect_required",
               "needs_reconnect"):
        return "expired"
    # An ABSOLUTE expiry timestamp in the past (expires_at / expiresAt), when present, is expired.
    exp_at = _parse_iso(acct.get("expires_at") or acct.get("expiresAt")
                        or md.get("expires_at") or md.get("expiresAt"))
    if exp_at is not None:
        now = now or datetime.now(timezone.utc)
        if exp_at < now:
            return "expired"
    # Token expiry is a NEGATIVE and takes precedence: connectedAt + expires_in in the past -> expired.
    exp = md.get("expires_in")
    connected_at = _parse_iso(acct.get("connectedAt") or md.get("connectedAt"))
    if isinstance(exp, (int, float)) and connected_at is not None:
        now = now or datetime.now(timezone.utc)
        if (now - connected_at).total_seconds() > float(exp):
            return "expired"
    # A real account ROW present in Zernio's list IS the connection: the OAuth callback wrote it, and
    # an intentional disconnect / inactive flag / token expiry (all handled above) are the only things
    # that make it not connected. We do NOT require a positive signal like profileData/connectedAt/
    # isActive: Zernio's list momentarily omits those fields, which used to flap a live connection
    # (especially Instagram) to "not_connected" and force a reconnect every session. This matches the
    # bar facebook_account_id() uses (platform + _id). The only guard is a bare or malformed payload
    # with no account id, which is never optimistically called connected.
    if not acct.get("_id"):
        return "not_connected"
    return "connected"


#: Key spellings Zernio has been seen to use for a Google Business listing name. The live
#: googlebusiness payload has never been captured, and six different spellings are guessed
#: across this module, zernio_routes and the portal, so read them all rather than pick one.
_GBP_NAME_KEYS = ("selectedLocationName", "locationName", "location_name", "title")


def _handle_of(acct):
    """The human label for a connected account, or None.

    IG and FB label with the @username. GOOGLE BUSINESS HAS NO USERNAME — its label is the
    listing name. Returning None for it is not "honest", it is fatal: the portal's
    withoutPhantomConnections downgrades any connected row with a null handle to
    not_connected, so a real, working Google connection renders as "Not connected yet"
    forever and the owner reconnects on a loop. That is the single reason Google never shows
    as connected in the portal.

    Falls back to the location id, which is real data we hold, never a fabricated name.
    """
    md = acct.get("metadata") or {}
    pd = md.get("profileData") or {}
    if (acct.get("platform") or "").lower() == "googlebusiness":
        for k in _GBP_NAME_KEYS:
            v = md.get(k) or pd.get(k)
            if v:
                return str(v)
        for v in (acct.get("displayName"), acct.get("name"),
                  md.get("selectedLocationId"), md.get("locationId")):
            if v:
                return str(v)
        return None
    # IG/FB: prefer the @username. A live connection whose list momentarily omits the username
    # must NOT return None — the portal's withoutPhantomConnections downgrades a null-handle
    # connected row to not_connected, the exact GBP failure extended to IG/FB (audit 2026-08-28).
    # Fall back to any real label we hold (displayName/name), then to the account id — real data
    # we hold, never a fabricated handle — so a working IG/FB connection never renders "Not
    # connected" over a transient missing username.
    h = (pd.get("username") or acct.get("displayName") or acct.get("name")
         or acct.get("_id") or acct.get("id"))
    return str(h) if h else None


def map_status(accounts_json, now=None):
    """
    Fold Zernio's flat `accounts[]` into the portal's per-platform shape:
      {platforms: {instagram: {connected, handle, expired}, facebook: {...}, googlebusiness: {...}}}
    Missing platform -> not connected, no handle (never fabricated). When more than one account of a
    platform exists, a connected one wins over an expired one.
    """
    out = {p: {"connected": False, "handle": None, "expired": False} for p in STATUS_PLATFORMS}
    for acct in (accounts_json or {}).get("accounts") or []:
        if not isinstance(acct, dict):
            continue
        plat = acct.get("platform")
        if plat not in STATUS_PLATFORMS:
            continue
        state = account_state(acct, now)
        cur = out[plat]
        # A connected account beats an already-recorded expired/none one.
        if cur["connected"]:
            continue
        if state == "connected":
            out[plat] = {"connected": True, "handle": _handle_of(acct), "expired": False}
        elif state == "expired" and not cur["expired"]:
            out[plat] = {"connected": False, "handle": _handle_of(acct), "expired": True}
    return {"platforms": out}


def map_pages(pages_json):
    """Zernio {pages:[{_id,name}]} -> portal {pages:[{id,name}]}. Drops entries missing id or name."""
    out = []
    for p in (pages_json or {}).get("pages") or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("_id") or p.get("id")
        name = p.get("name")
        if pid and name:
            out.append({"id": str(pid), "name": str(name)})
    return {"pages": out}


def map_locations(locations_json):
    """Zernio GBP {locations:[...]} -> portal {locations:[{id,name,account_id}]}.

    Tolerant of key spellings (probing the live shape needs a real OAuth grant, so
    the mapper accepts the documented variants): id from id/_id/locationId/name
    (GBP resource names look like 'locations/123'), display name from
    title/displayName/locationName/name, owning account from accountId/account.
    Drops entries with no id. Never fabricates a name (falls back to the id)."""
    raw = locations_json or {}
    entries = raw.get("locations") if isinstance(raw, dict) else raw
    if entries is None and isinstance(raw, dict):
        entries = raw.get("data")
    out = []
    for loc in entries or []:
        if not isinstance(loc, dict):
            continue
        lid = (loc.get("id") or loc.get("_id") or loc.get("locationId")
               or loc.get("name"))
        if not lid:
            continue
        name = (loc.get("title") or loc.get("displayName")
                or loc.get("locationName"))
        if not name and loc.get("name") and str(loc.get("name")) != str(lid):
            name = loc.get("name")
        acct = loc.get("accountId") or loc.get("account")
        out.append({"id": str(lid), "name": str(name or lid),
                    "account_id": str(acct) if acct else ""})
    return {"locations": out}


def facebook_account_id(accounts_json):
    """The Zernio account _id of the connected Facebook account, or None."""
    for acct in (accounts_json or {}).get("accounts") or []:
        if acct.get("platform") == "facebook" and acct.get("_id"):
            return str(acct["_id"])
    return None


def instagram_account_id(accounts_json):
    """The Zernio account _id of the connected Instagram account, or None."""
    for acct in (accounts_json or {}).get("accounts") or []:
        if acct.get("platform") == "instagram" and acct.get("_id"):
            return str(acct["_id"])
    return None


def account_id_for(accounts_json, platform):
    """The connected Zernio account _id for 'facebook' or 'instagram', or None. Only
    a CONNECTED account (account_state) is returned, so an expired/disconnected
    account never publishes."""
    plat = (platform or "").strip().lower()
    for acct in (accounts_json or {}).get("accounts") or []:
        if acct.get("platform") == plat and acct.get("_id") \
                and account_state(acct) == "connected":
            return str(acct["_id"])
    return None


def post_id_of(post_json):
    """The created post's id from a POST /v1/posts response, tolerant of shape
    ({_id} | {id} | {post:{_id}} | {data:{_id}}). '' when none is present."""
    if not isinstance(post_json, dict):
        return ""
    for holder in (post_json, post_json.get("post"), post_json.get("data")):
        if isinstance(holder, dict):
            pid = holder.get("_id") or holder.get("id")
            if pid:
                return str(pid)
    return ""
