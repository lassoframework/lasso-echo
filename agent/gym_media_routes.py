"""
gym_media_routes.py — the Echo API surface for Connect Google Drive
(gym_media_drive spec §8). The PORTAL calls these; the portal NEVER holds the
Drive service-account key — Echo does every Drive read behind the scenes.

Every handler is gym-scoped: intake_web resolves the caller's signed token to an
account_key and passes it in, so a gym can only ever see or touch ITS OWN media.
The thumbnail proxy re-asserts gym ownership before serving a byte.

Handlers return (status, body_dict) exactly like portal_routes, except the
thumbnail proxy returns (status, content_type, bytes) so the HTTP layer can stream
image bytes.

RAILS (spec §1.5) enforced here:
  (a) hijack: bind refuses a folder_id already bound anywhere + fires ONE ops alert
      naming both gyms.
  (b) show-and-confirm: check-connection returns folder name + owner + counts for
      the coach to confirm BEFORE bind.
  (c) ownership sanity: a folder owned by a DIFFERENT connected gym's known domain
      is blocked + alerted.
  (f) revocation is handled by the sync job; disconnect here marks inactive, never
      deletes.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from . import config, gym_media_index as _idx
from . import gym_media_selector as _sel

# <name-slug><6-hex-fingerprint> shape, matching account_key.py's canonical_account_key
# output. Used only to detect a STALE fingerprint (see _resolve_stale_fingerprint) --
# never to derive or mint a key.
_FINGERPRINT_RE = re.compile(r"^([a-z0-9]+)([0-9a-f]{6})$")


def _name_slug_of(key):
    """The bare name-slug portion of a <slug><6-hex-fingerprint> shaped account key
    ('crossfitreverb6cdf33' -> 'crossfitreverb'), or '' when the key does not look
    fingerprinted (too short, or its tail is not 6 hex chars) -- such keys are left
    alone, never guessed at."""
    m = _FINGERPRINT_RE.match((key or "").strip().lower())
    return m.group(1) if m else ""


_PLANE_BASES_TTL = 300  # seconds; this list changes at onboarding speed, not request speed
_plane_bases_cache = {"at": 0.0, "bases": frozenset()}


def _shared_plane_bases(now_fn=None, get=None):
    """Every account key the PORTAL considers real, read from the shared plane
    (echo_intake_tokens.echo_account_key) rather than from worker-local state.

    Both the worker and echo-intake-web can read this; only the worker can read the
    account registry file. Cached for _PLANE_BASES_TTL so a per-request resolution never
    turns into a per-request round trip.

    Fails to an EMPTY set, never raises: an unreadable plane must leave the caller in
    exactly today's behaviour (return the key unchanged), never remap on a guess."""
    import time
    now = (now_fn or time.time)()
    if now - _plane_bases_cache["at"] < _PLANE_BASES_TTL and _plane_bases_cache["bases"]:
        return set(_plane_bases_cache["bases"])
    try:
        if get is None:
            from .account_key_split_watch import _supabase_get as get  # noqa: PLC0415
        rows = get("echo_intake_tokens", {"select": "echo_account_key"}) or []
        bases = {str(r.get("echo_account_key") or "").strip() for r in rows}
        bases = {b for b in bases if b}
    except Exception as e:  # noqa: BLE001 - never let a plane read break a media request
        print(f"[gym-media] shared-plane base read failed: {type(e).__name__}: {e}")
        return set()
    if bases:
        _plane_bases_cache["at"] = now
        _plane_bases_cache["bases"] = frozenset(bases)
    return set(bases)


def _resolve_stale_fingerprint(gym, *, bases_fn=None):
    """Resolve a STALE account-key fingerprint onto the currently-registered gym it
    belongs to, or return `gym` unchanged.

    WHY (live 2026-08-31, CrossFit Reverb): a signed portal connect-link self-decodes
    its OWN account_key from its HMAC payload -- by design, re-canonicalizing a gym's
    key (account_key_mint.py) never invalidates an already-issued link, so the link
    keeps validating under whatever key it was ORIGINALLY minted with ("WHY THIS
    CANNOT BREAK EXISTING LINKS" in that module). Dean Holcomb used a connect link
    still carrying 'crossfitreverb6cdf33' to bind his Google Drive folder 12 minutes
    after a fresh mint moved Reverb onto 'crossfitreverb30b5b2'; Echo wrote his
    media_source row (and 190 media_asset rows behind it) under the stale key, which
    nothing else in the system reads (client_sources, the account registry, and the
    Zernio profile all live under the new key) -- his photos were indexed and
    invisible.

    If `gym` is not itself a currently REGISTERED gym base, but its name-slug (the
    part before the trailing 6-hex fingerprint) matches the name-slug of EXACTLY ONE
    registered base, that registered base is returned instead. Zero or more-than-one
    matches leave `gym` untouched -- never guess across an ambiguity: two different
    gyms can legitimately share a name-slug (the fingerprint is what tells them
    apart), and a brand-new gym that has not registered yet is legitimately absent,
    not a stale fingerprint. A key with no 6-hex tail (a bare slug like 'pierce', or
    an ad-hoc key with no fingerprint) never matches the shape and is always returned
    unchanged. Never writes anything itself -- purely a read-time/write-time key
    resolution, one throttled ops alert per stale key so the real fix (re-issue the
    gym's portal link) does not go unnoticed."""
    if bases_fn is None:
        def bases_fn():
            from .calendar_autopublish import client_gym_bases
            return client_gym_bases()
    try:
        bases = set(bases_fn())
    except Exception:  # noqa: BLE001 - a registry read failure just skips the remap
        bases = set()
    # SHARED-PLANE FALLBACK (Dean Holcomb again, 2026-09-04). Everything above is
    # correct and it still did not work for him, because client_gym_bases() reads
    # accounts.all_accounts(), which reads the account registry FILE
    # (config.gym_registry_path(), /data/gym_accounts.json). That file lives on the
    # WORKER's Railway volume. echo-intake-web -- the service that actually serves
    # /portal/<token>/media/* to the gym -- has no such file: os.path.exists() is
    # False there, so client_gym_bases() returned 5 built-in bases with no client gym
    # among them, no name-slug ever matched, and this resolver handed back the stale
    # key unchanged on the ONE service where a gym's request actually lands.
    #
    # Live proof on 2026-09-04: GET /portal/<6cdf33 token>/media/sources returned
    # {"sources": []} while the same call under crossfitreverb30b5b2 returned his
    # "Reverb LASSO Content" folder. Dean saw "no connected folders", and re-connecting
    # said "already connected" because the hijack rail (find_source_by_folder) looks the
    # folder up GLOBALLY and does find it. Two contradictory answers, one cause.
    #
    # So the base list can never come from worker-local state alone. echo_intake_tokens
    # is the portal's own key column and BOTH services can read it, so it is the source
    # of truth for "which keys are real" -- the registry is now an optimisation, not the
    # only answer.
    bases |= _shared_plane_bases()
    if not bases or gym in bases:
        return gym
    slug = _name_slug_of(gym)
    if not slug:
        return gym
    matches = {b for b in bases if _name_slug_of(b) == slug}
    if len(matches) != 1:
        return gym
    remapped = next(iter(matches))
    _idx.dedup_alert(
        f"stale_key_remap:{gym}",
        f"Connect Google Drive: a request carried account key {gym!r}, which is not "
        f"a currently registered gym but looks like a STALE fingerprint of "
        f"{remapped!r} (a portal link minted before a re-key). Resolved to "
        f"{remapped!r} for this request so it is not silently invisible. Re-issue "
        f"{gym!r}'s portal link so this stops being needed: python -m agent "
        f"intake-link --account {remapped}")
    return remapped


def _base(account_key):
    return _resolve_stale_fingerprint(_sel.base_gym_key(account_key))


def _armed(account_key):
    """The Connect Google Drive lane must be armed for THIS gym (global flag OR the
    pilot allowlist). A disabled lane returns 403 uniformly."""
    return config.gym_drive_connect_active_for(_base(account_key))


def _store():
    return _idx.default_store()


def _drive():
    from .integrations import drive_client as _dc
    return _dc.DriveClient()


# ---- POST /media/check-connection --------------------------------------------
def handle_check_connection(account_key, folder_url, *, drive=None, store=None):
    """POST /media/check-connection {folder_url}

    Parse the pasted link/id, read the folder via the SA, and return the
    show-and-confirm payload. NEVER binds. Response:
      {ok, folder_name, owner_email, photos, videos, case}
    case: my_drive | shared_drive | not_shared | admin_blocked | already_bound.

    A 403 from Drive reads as 'not_shared' (the coach has not shared the folder to
    the SA yet) — NOT a 'bad link'. Garbage in the url -> a clear ok:false with a
    'bad link' message (never a 500)."""
    if not _armed(account_key):
        return 403, {"ok": False, "error": "media connect is not enabled for this gym"}
    from .integrations import drive_client as _dc
    drive = drive or _drive()
    store = store or _store()

    try:
        folder_id = _dc.parse_folder_id(folder_url)
    except _dc.DriveUrlError as e:
        return 200, {"ok": False, "case": "bad_link", "error": str(e)}

    if not drive.available():
        return 200, {"ok": False, "case": "admin_blocked",
                     "error": "Drive access is not configured on the server"}

    # Already bound anywhere? Surface it here so the coach sees it before they even
    # try to bind (the hard refuse is at bind time, this is a friendly heads-up).
    try:
        existing = store.find_source_by_folder(folder_id) if store.available() else None
    except Exception:
        existing = None
    if existing and existing.get("active"):
        same = _base(account_key) == str(existing.get("gym_id") or "")
        return 200, {"ok": bool(same), "case": "already_bound",
                     "folder_name": existing.get("folder_name") or "",
                     "owner_email": "", "photos": 0, "videos": 0,
                     "error": "" if same else "this folder is already connected to another gym"}

    try:
        meta = drive.get_folder_meta(folder_id)
    except Exception as e:  # noqa: BLE001 - any transport error is a soft admin_blocked
        from . import ops_alerts
        return 200, {"ok": False, "case": "admin_blocked",
                     "error": f"could not read the folder ({type(e).__name__})",
                     "detail": ops_alerts.scrub(str(e))[:120]}

    case = meta.get("case") or "my_drive"
    if case == "not_shared":
        return 200, {"ok": False, "case": "not_shared", "folder_name": "",
                     "owner_email": "", "photos": 0, "videos": 0,
                     "error": "that folder is not shared with Echo yet — share it "
                              "as Viewer to the service account, then retry"}

    # Count photos vs videos among the immediate children for the confirm card.
    photos = videos = 0
    try:
        for child in drive.list_children(folder_id):
            if child.is_folder:
                continue
            k = _idx.classify(child.title, child.mime_type)
            if k == _idx.KIND_PHOTO:
                photos += 1
            elif k == _idx.KIND_VIDEO:
                videos += 1
    except Exception:  # noqa: BLE001 - a count failure still lets the coach confirm
        pass

    return 200, {"ok": True, "case": case, "folder_name": meta.get("name") or "",
                 "owner_email": meta.get("owner_email") or "",
                 "folder_id": folder_id, "photos": photos, "videos": videos}


# ---- POST /media/sources (bind after confirm) --------------------------------
def handle_bind_source(account_key, folder_url, actor_id="", *, drive=None,
                       store=None):
    """POST /media/sources {folder_url, actor_id?}

    Bind a confirmed folder to this gym. Enforces the hijack rail (§1.5a: a
    folder_id bound ANYWHERE = hard refuse + ONE ops alert naming both gyms) and
    the ownership-sanity rail (§1.5c). Response: {ok, source_id, folder_name} or an
    error with case."""
    if not _armed(account_key):
        return 403, {"ok": False, "error": "media connect is not enabled for this gym"}
    from .integrations import drive_client as _dc
    from . import ops_alerts
    drive = drive or _drive()
    store = store or _store()
    gym = _base(account_key)

    try:
        folder_id = _dc.parse_folder_id(folder_url)
    except _dc.DriveUrlError as e:
        return 400, {"ok": False, "case": "bad_link", "error": str(e)}
    if not store.available():
        return 503, {"ok": False, "error": "media store unavailable"}

    # (a) HIJACK: refuse a folder bound anywhere; alert naming both gyms.
    try:
        existing = store.find_source_by_folder(folder_id)
    except Exception as e:  # noqa: BLE001
        return 503, {"ok": False, "error": f"store read failed ({type(e).__name__})"}
    if existing:
        other = str(existing.get("gym_id") or "")
        if other != gym:
            _idx.dedup_alert(
                f"hijack:{folder_id}",
                f"Connect Google Drive HIJACK refused: gym {gym!r} tried to bind "
                f"Drive folder {folder_id} which is already connected to gym "
                f"{other!r}. The bind was refused.")
            return 409, {"ok": False, "case": "already_bound",
                         "error": "this folder is already connected to another gym"}
        # Same gym re-binding the same folder: idempotent success.
        if existing.get("active"):
            return 200, {"ok": True, "source_id": existing.get("id"),
                         "folder_name": existing.get("folder_name") or "",
                         "already": True}

    # Read meta for name/owner + the ownership-sanity rail.
    try:
        meta = drive.get_folder_meta(folder_id)
    except Exception as e:  # noqa: BLE001
        return 502, {"ok": False, "error": f"could not read the folder ({type(e).__name__})"}
    if meta.get("case") == "not_shared":
        return 200, {"ok": False, "case": "not_shared",
                     "error": "that folder is not shared with Echo yet"}
    owner_email = str(meta.get("owner_email") or "").strip().lower()

    # (c) OWNERSHIP SANITY: if the owner's domain matches a DIFFERENT connected
    # gym's known owner domain, block + alert (a likely wrong-gym folder).
    blocked_gym = _owner_domain_conflict(store, gym, owner_email)
    if blocked_gym:
        _idx.dedup_alert(
            f"owner_conflict:{folder_id}",
            f"Connect Google Drive ownership-sanity BLOCK: gym {gym!r} tried to "
            f"bind a folder owned by {owner_email!r}, whose domain matches the "
            f"already-connected gym {blocked_gym!r}. The bind was blocked.")
        return 409, {"ok": False, "case": "owner_conflict",
                     "error": "the folder owner looks like a different gym; blocked"}

    source_id = uuid.uuid4().hex
    row = {
        "id": source_id, "gym_id": gym, "kind": "gym_drive",
        "folder_id": folder_id, "folder_name": meta.get("name") or "",
        "owner_email": owner_email, "sync_mode": "all", "active": True,
        "revoked_externally": False, "connected_by": actor_id or "",
        "connected_at": datetime.now(timezone.utc).isoformat()}
    try:
        store.insert_source(row)
    except Exception as e:  # noqa: BLE001 - a UNIQUE(folder_id) race lands here too
        # The DB-level global UNIQUE is the last-resort hijack guard: if two binds
        # race, the loser gets a store error here rather than a duplicate row.
        return 409, {"ok": False, "case": "already_bound",
                     "error": "this folder is already connected",
                     "detail": ops_alerts.scrub(str(e))[:120]}
    return 200, {"ok": True, "source_id": source_id,
                 "folder_name": meta.get("name") or ""}


def _owner_domain_conflict(store, gym, owner_email):
    """The base key of a DIFFERENT connected gym whose owner-email domain matches
    `owner_email`'s domain, or None. Personal-mail domains never conflict (every
    gym owner might use gmail); only shared business domains trip the rail."""
    if "@" not in (owner_email or ""):
        return None
    domain = owner_email.split("@", 1)[1].strip().lower()
    if not domain or domain in _PERSONAL_DOMAINS:
        return None
    try:
        sources = store.list_sources()
    except Exception:
        return None
    for s in sources:
        other_gym = str(s.get("gym_id") or "")
        if other_gym == gym:
            continue
        other_owner = str(s.get("owner_email") or "").lower()
        if "@" in other_owner and other_owner.split("@", 1)[1] == domain:
            return other_gym
    return None


_PERSONAL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com",
                     "outlook.com", "icloud.com", "aol.com", "me.com", "proton.me"}


# ---- GET /media/sources?gym --------------------------------------------------
def handle_list_sources(account_key, *, store=None):
    """GET /media/sources — this gym's connected sources. Response:
      {sources: [{id, folder_id, folder_name, owner_email, active,
                  revoked_externally, sync_mode, connected_at}]}"""
    if not _armed(account_key):
        return 403, {"error": "media connect is not enabled for this gym"}
    store = store or _store()
    if not store.available():
        return 200, {"sources": []}
    try:
        sources = store.list_sources(_base(account_key), include_inactive=True)
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"store read failed ({type(e).__name__})"}
    return 200, {"sources": [
        {"id": s.get("id"), "folder_id": s.get("folder_id"),
         "folder_name": s.get("folder_name"), "owner_email": s.get("owner_email"),
         "active": bool(s.get("active")),
         "revoked_externally": bool(s.get("revoked_externally")),
         "sync_mode": s.get("sync_mode"), "connected_at": s.get("connected_at")}
        for s in sources]}


# ---- POST /media/sources/<id>/disconnect -------------------------------------
def handle_disconnect_source(account_key, source_id, *, store=None):
    """POST /media/sources/<id>/disconnect — mark the source inactive AND its
    assets inactive (excluded). NEVER deletes a row (§1.5). A source belonging to
    another gym 404s. Response: {ok}."""
    if not _armed(account_key):
        return 403, {"error": "media connect is not enabled for this gym"}
    store = store or _store()
    if not store.available():
        return 503, {"error": "media store unavailable"}
    gym = _base(account_key)
    try:
        sources = {s.get("id"): s for s in store.list_sources(gym, include_inactive=True)}
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"store read failed ({type(e).__name__})"}
    src = sources.get(source_id)
    if src is None:
        return 404, {"error": "unknown source"}   # cross-gym or missing: same 404
    try:
        store.update_source(source_id, {"active": False})
        # Mark this source's assets excluded so they leave the pool without deletion.
        for a in store.list_assets(gym, source_id=source_id):
            if not a.get("excluded_by_coach"):
                store.update_asset(a["id"], {"excluded_by_coach": True,
                                             "reject_reason": "source_disconnected"})
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"disconnect failed ({type(e).__name__})"}
    return 200, {"ok": True}


# ---- GET /media/assets?gym ---------------------------------------------------
def handle_list_assets(account_key, *, store=None):
    """GET /media/assets — this gym's assets for the portal media tab. Response:
      {assets: [{id, kind, title, eligible, excluded_by_coach, reject_reason,
                 aspect, crop_hint, used_count, last_used_at,
                 thumb_url}]}"""
    if not _armed(account_key):
        return 403, {"error": "media connect is not enabled for this gym"}
    store = store or _store()
    if not store.available():
        return 200, {"assets": []}
    gym = _base(account_key)
    try:
        assets = store.list_assets(gym)
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"store read failed ({type(e).__name__})"}
    return 200, {"assets": [
        {"id": a.get("id"), "kind": a.get("kind"), "title": a.get("title"),
         "eligible": a.get("eligible"),
         "excluded_by_coach": bool(a.get("excluded_by_coach")),
         "reject_reason": a.get("reject_reason"), "aspect": a.get("aspect"),
         "crop_hint": a.get("crop_hint"), "used_count": a.get("used_count"),
         "last_used_at": a.get("last_used_at"),
         "thumb_url": f"/media/thumb/{a.get('id')}"}
        for a in assets]}


# ---- POST /media/assets/<id>/hide|unhide -------------------------------------
def handle_hide_asset(account_key, asset_id, hide=True, *, store=None):
    """POST /media/assets/<id>/hide|unhide — toggle excluded_by_coach. A hidden
    asset is never selectable. Hiding an asset a PENDING row is currently using
    flips that row back with reject_reason='media_hidden' and returns the asset's
    usage stamp to the pool (§8). A cross-gym asset 404s. Response: {ok}."""
    if not _armed(account_key):
        return 403, {"error": "media connect is not enabled for this gym"}
    store = store or _store()
    if not store.available():
        return 503, {"error": "media store unavailable"}
    gym = _base(account_key)
    try:
        asset = store.get_asset(asset_id)
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"store read failed ({type(e).__name__})"}
    if asset is None or str(asset.get("gym_id") or "") != gym:
        return 404, {"error": "unknown asset"}     # cross-gym or missing: same 404
    try:
        store.update_asset(asset_id, {"excluded_by_coach": bool(hide)})
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"update failed ({type(e).__name__})"}
    if hide:
        # Pull any PENDING row off this asset and return the photo to the pool.
        # HONEST: if the calendar flip fails we say so, because "hidden" that leaves
        # the post scheduled is the worst possible answer for a client who just told
        # us not to publish that photo.
        flipped, flip_err = _flip_pending_using_asset(gym, asset_id)
        try:
            _sel.rollback_asset(asset_id, store=store)
        except Exception:  # noqa: BLE001 - the pool stamp is best effort
            pass
        if flip_err:
            return 502, {"error": "the photo is hidden from future posts, but a "
                                  "scheduled post still using it could not be pulled "
                                  f"back ({flip_err}). Tell your coach before it goes "
                                  "out.", "hidden": True, "pulled": 0}
        return 200, {"ok": True, "pulled": flipped}
    return 200, {"ok": True}


def _flip_pending_using_asset(gym_id, asset_id):
    """Pull any PENDING content_calendar row off a just-hidden asset. Returns
    (rows_pulled, error_string_or_None).

    Writes status='denied' with reject_reason='media_hidden', NOT 'needs_media':
    'needs_media' is not in the content_calendar status CHECK constraint, so every
    one of these PATCHes was rejected 400 by Postgres — and because the response was
    never inspected and the caller returned 200 regardless, hiding a photo silently
    did nothing while the client was told it worked. 'denied' is a real status, and
    it is the one the armed deny-backfill lane watches, so the day gets a fresh
    caption on a DIFFERENT photo instead of a hole.

    media_not_ready_reason carries the same marker for the media lane. Never raises."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return 0, "the calendar store is not configured"
    import requests  # lazy
    try:
        r = requests.patch(
            f"{url.rstrip('/')}/rest/v1/content_calendar",
            params={"gym_id": f"eq.{gym_id}", "status": "eq.pending",
                    "source_media_asset_id": f"eq.{asset_id}"},
            json={"status": "denied",
                  "reject_reason": _idx.REJECT_HIDDEN,
                  "media_not_ready_reason": _idx.REJECT_HIDDEN},
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "return=representation"},
            timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"[gym-media-routes] flip-pending on hide failed: {type(e).__name__}: {e}")
        return 0, type(e).__name__
    if r.status_code >= 400:
        # The exact failure that hid itself for months. Say it out loud.
        print(f"[gym-media-routes] flip-pending on hide REJECTED {r.status_code}: "
              f"{(r.text or '')[:200]}")
        return 0, f"calendar store returned {r.status_code}"
    try:
        return len(r.json() or []), None
    except Exception:  # noqa: BLE001 - a 2xx with an unreadable body still succeeded
        return 0, None


# ---- GET /media/thumb/<asset_id> (gym-scoped proxy) --------------------------
def handle_thumbnail(account_key, asset_id, *, store=None, drive=None,
                     host=None):
    """GET /media/thumb/<asset_id> — Echo fetches the Drive thumbnail via the SA,
    caches it by content_hash, and streams it. REFUSES a gym requesting another
    gym's asset (§8 tenant isolation). Returns (status, content_type, bytes)."""
    if not _armed(account_key):
        return 403, "text/plain", b"media connect is not enabled"
    store = store or _store()
    if not store.available():
        return 404, "text/plain", b"not found"
    gym = _base(account_key)
    try:
        asset = store.get_asset(asset_id)
    except Exception:  # noqa: BLE001
        return 502, "text/plain", b"store error"
    # TENANT ISOLATION: a mismatch is a flat 404 (never reveal another gym's asset).
    if asset is None or str(asset.get("gym_id") or "") != gym:
        return 404, "text/plain", b"not found"

    drive = drive or _drive()
    if not drive.available():
        return 404, "text/plain", b"not found"

    # Prefer Drive's server-side thumbnail: a small, downsized JPEG rendition Google
    # generates for images AND video frames. Serving THAT (correctly typed image/jpeg)
    # avoids streaming the full multi-MB original with a mislabeled content-type — the
    # thumbnail proxy is a preview, not the asset download path (audit #7).
    try:
        thumb = drive.thumbnail(asset_id)
    except Exception:  # noqa: BLE001
        thumb = None
    if thumb:
        data, ctype = thumb
        return 200, ctype, data

    # Fallback: Drive made no thumbnail (rare). Stream the original, but label it with
    # the asset's OWN mime type so the bytes are never mislabeled as something else.
    try:
        import tempfile
        import os as _os
        tmp_dir = tempfile.mkdtemp(prefix="gymthumb_")
        tmp = _os.path.join(tmp_dir, "thumb.bin")
        try:
            drive.download(asset_id, tmp)
            with open(tmp, "rb") as fh:
                data = fh.read()
        finally:
            try:
                _os.unlink(tmp)
                _os.rmdir(tmp_dir)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return 404, "text/plain", b"not found"
    ctype = asset.get("mime_type") or "application/octet-stream"
    return 200, ctype, data
