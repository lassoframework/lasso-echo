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

import uuid
from datetime import datetime, timezone

from . import config, gym_media_index as _idx
from . import gym_media_selector as _sel


def _base(account_key):
    return _sel.base_gym_key(account_key)


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
        # Flip any PENDING row using this asset back to needs_media + rollback use.
        _flip_pending_using_asset(gym, asset_id)
        try:
            _sel.rollback_asset(asset_id, store=store)
        except Exception:  # noqa: BLE001
            pass
    return 200, {"ok": True}


def _flip_pending_using_asset(gym_id, asset_id):
    """Flip any PENDING content_calendar row using this asset back to needs_media
    with reject_reason='media_hidden' (§8). Best effort; no creds -> no-op."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return
    import requests  # lazy
    try:
        requests.patch(
            f"{url.rstrip('/')}/rest/v1/content_calendar",
            params={"gym_id": f"eq.{gym_id}", "status": "eq.pending",
                    "source_media_asset_id": f"eq.{asset_id}"},
            json={"status": "needs_media",
                  "media_not_ready_reason": _idx.REJECT_HIDDEN},
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"[gym-media-routes] flip-pending on hide failed: {type(e).__name__}: {e}")


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
