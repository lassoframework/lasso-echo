"""
drive_client.py — read-only Google Drive access for the podcast library lane
(PODCAST_LIBRARY_BUILD_SPEC.md Wave 1).

Auth is a Google SERVICE ACCOUNT key (env GOOGLE_DRIVE_SA_JSON: a file path or the
inline JSON; falls back to the existing AGENT_GDRIVE_SA_JSON convention from
podcast_source.py), scope drive.readonly ONLY — Echo must never be able to modify
or delete the podcast library. Blake shares the `Podcast Episodes` folder to the
service-account email as Viewer; that is the whole grant.

DEGRADES CLEANLY: no key -> available() is False and every caller (the nightly
indexer, the stage-time builder) no-ops with one log line. Nothing Google is
imported at module import time, so the module loads fine offline and in tests.

OFFLINE-TESTABLE: all network lives behind an injectable `transport` with three
methods (list_files / download_to / export_text); tests inject a fake and never
touch the wire. The real transport (GoogleDriveTransport) wraps
google-api-python-client exactly like agent/podcast_source.py does.

Rate limits: Drive allows ~1,000 req/100s/user; a cold walk of ~100 episode
folders is ~110 requests. The folder tree is cached for 6 hours in the shared kv
store so repeated walks inside a run window cost zero requests.

SECRETS: the SA key is read from env, handed to google-auth, and never logged,
printed, returned, or written anywhere by this module.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"

_TREE_CACHE_TTL_SEC = 6 * 3600
_TREE_CACHE_KEY = "podcast_drive_tree:{}:{}"
# md5Checksum (content dedupe) + imageMediaMetadata (photo dims for the gym-media
# gate) ride along at no extra request cost. Google returns them null for objects
# that lack them (Google-native docs, a video with no probe) — callers handle that.
_LIST_FIELDS = ("nextPageToken, files(id, name, mimeType, size, parents, "
                "modifiedTime, md5Checksum, "
                "imageMediaMetadata(width, height))")
_DOWNLOAD_CHUNK = 8 * 1024 * 1024  # 8 MB streaming chunks; a 250 MB clip never sits in RAM


class DriveUnavailable(Exception):
    """No service-account key (or the Google libs are absent). Callers treat
    this as 'lane unarmed', never as a crash."""


class DriveUrlError(ValueError):
    """The pasted text is not a recognizable Drive folder link or id. Callers
    surface this as a clear 'that does not look like a Drive folder link' — never
    a stack trace, never a 500."""


# A Drive folder/file id is url-safe base64-ish: letters, digits, - and _. Real
# ids run ~19-44 chars; keep the floor low but non-trivial so a stray word does
# not read as an id.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")
# The id embedded in a /folders/<id> or /d/<id> path segment.
_PATH_ID_RE = re.compile(r"/(?:folders|d|file/d|drive/folders)/([A-Za-z0-9_-]{16,})")


def parse_folder_id(text):
    """Extract a Drive folder id from whatever a coach pastes, or raise
    DriveUrlError on garbage. Accepts every real-world shape (spec §1):

      * a full folder link      https://drive.google.com/drive/folders/<id>
      * with a share suffix     .../folders/<id>?usp=sharing
      * an open/?id= form       https://drive.google.com/open?id=<id>
      * a /file/d/<id>/ link     (a file link the coach grabbed by mistake)
      * a bare id               1AbC...  (pasted from the URL bar)

    Trailing slashes, query strings, and surrounding whitespace are tolerated.
    Anything that is not a link and is not a bare id -> DriveUrlError (the route
    turns this into a plain 'bad link' message, never a crash)."""
    s = str(text or "").strip()
    if not s:
        raise DriveUrlError("no folder link or id provided")
    # A bare id pasted straight from the URL bar.
    if _ID_RE.match(s) and "/" not in s and "." not in s and " " not in s:
        return s
    # Anything URL-ish: pull the id from the path first, then from ?id=.
    m = _PATH_ID_RE.search(s)
    if m:
        return m.group(1)
    try:
        qs = parse_qs(urlparse(s).query)
    except Exception:  # noqa: BLE001 - a malformed url is a bad link, not a crash
        qs = {}
    for key in ("id", "resourcekey"):
        vals = qs.get(key) or []
        for v in vals:
            if _ID_RE.match(v):
                return v
    raise DriveUrlError(
        "that does not look like a Google Drive folder link or id")


@dataclass(frozen=True)
class DriveFile:
    """One Drive object, exactly the fields the spec's client surface names. The
    podcast lane uses id/title/mime_type/size_bytes/parent_id/modified_time; the
    gym-media lane additionally uses content_hash (dedupe) and width/height (the
    photo gate) — all default-empty so old callers and fakes are unaffected."""
    id: str
    title: str
    mime_type: str
    size_bytes: int
    parent_id: str
    modified_time: str
    content_hash: str = ""
    width: int = 0
    height: int = 0

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


def _sa_json() -> str:
    from .. import config
    return config.google_drive_sa_json()


def available() -> bool:
    """True when a service-account key is configured. False -> the whole podcast
    library lane is inert (indexer no-ops with one log line)."""
    return bool(_sa_json())


class GoogleDriveTransport:
    """The REAL transport: google-api-python-client with SA creds. Everything is
    lazy-imported on first use so importing this module never needs the libs."""

    def __init__(self, sa_json=None):
        self._sa = sa_json
        self._svc = None

    def _service(self):
        if self._svc is None:
            sa = self._sa if self._sa is not None else _sa_json()
            if not sa:
                raise DriveUnavailable("GOOGLE_DRIVE_SA_JSON is not set")
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build
            except Exception as e:  # noqa: BLE001 - missing dep reads as unavailable
                raise DriveUnavailable(
                    f"google drive libs unavailable: {type(e).__name__}") from e
            info = json.loads(sa) if sa.strip().startswith("{") else json.load(open(sa))
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[SCOPE])
            self._svc = build("drive", "v3", credentials=creds,
                              cache_discovery=False)
        return self._svc

    def list_files(self, query, page_token=None):
        """One files().list page: {'files': [...], 'nextPageToken': ...}."""
        return self._service().files().list(
            q=query, fields=_LIST_FIELDS, pageSize=1000, pageToken=page_token,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()

    def download_to(self, file_id, fh):
        """Stream the file's bytes into the open binary file handle."""
        from googleapiclient.http import MediaIoBaseDownload
        req = self._service().files().get_media(fileId=file_id)
        dl = MediaIoBaseDownload(fh, req, chunksize=_DOWNLOAD_CHUNK)
        done = False
        while not done:
            _status, done = dl.next_chunk()

    def export_text(self, file_id):
        """Export a Google Doc as text/plain."""
        data = self._service().files().export(
            fileId=file_id, mimeType="text/plain").execute()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data or "")

    def get_meta(self, file_id):
        """files().get for one object: name, mimeType, and the owner's email
        (My Drive files carry owners; Shared Drive items do not, which is exactly
        how the route distinguishes the two cases). Raises the underlying Google
        HttpError on a 403/404 so the client can classify not-shared vs bad-id."""
        return self._service().files().get(
            fileId=file_id,
            fields="id, name, mimeType, owners(emailAddress), driveId",
            supportsAllDrives=True).execute()


def _http_status(exc):
    """The HTTP status code of a googleapiclient HttpError (or any exception that
    carries resp.status / status_code), else None. Lets callers classify a Drive
    403 (not shared) apart from a 404 (gone) apart from anything else, without
    importing googleapiclient at module load."""
    resp = getattr(exc, "resp", None)
    if resp is not None:
        try:
            return int(getattr(resp, "status", None) or resp.get("status"))
        except (TypeError, ValueError, AttributeError):
            pass
    for attr in ("status_code", "status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


def _to_drive_file(raw) -> DriveFile:
    parents = raw.get("parents") or []
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    meta = raw.get("imageMediaMetadata") or {}
    try:
        width = int(meta.get("width") or 0)
    except (TypeError, ValueError):
        width = 0
    try:
        height = int(meta.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    return DriveFile(
        id=str(raw.get("id") or ""),
        title=str(raw.get("name") or ""),
        mime_type=str(raw.get("mimeType") or ""),
        size_bytes=size,
        parent_id=str(parents[0]) if parents else "",
        modified_time=str(raw.get("modifiedTime") or ""),
        content_hash=str(raw.get("md5Checksum") or ""),
        width=width,
        height=height,
    )


class DriveClient:
    """The spec's client surface. `transport` is injectable (tests pass a fake);
    kv_get/kv_set default to the shared sqlite kv store for the 6h tree cache."""

    def __init__(self, transport=None, kv_get=None, kv_set=None, now=None):
        self._transport = transport
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._now = now or time.time

    # -- plumbing ------------------------------------------------------------
    def _t(self):
        if self._transport is None:
            self._transport = GoogleDriveTransport()
        return self._transport

    def available(self) -> bool:
        return self._transport is not None or available()

    def _kv(self):
        if self._kv_get is not None and self._kv_set is not None:
            return self._kv_get, self._kv_set
        from .. import db
        return db.kv_get, db.kv_set

    # -- reads ---------------------------------------------------------------
    def list_children(self, folder_id: str) -> list:
        """Every non-trashed child of `folder_id` (one level), as DriveFile."""
        out, token = [], None
        while True:
            resp = self._t().list_files(
                f"'{folder_id}' in parents and trashed = false", page_token=token)
            out.extend(_to_drive_file(f) for f in (resp.get("files") or []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return out

    def walk(self, folder_id: str, max_depth: int = 3, use_cache: bool = True) -> list:
        """The full recursive listing under `folder_id` (folders included), depth
        capped at `max_depth`. Cached in kv for 6 hours (spec §1.2 rate-limit
        budget); pass use_cache=False to force a fresh walk (the nightly indexer
        does, so the index never runs on a stale tree)."""
        kv_get, kv_set = self._kv()
        cache_key = _TREE_CACHE_KEY.format(folder_id, max_depth)
        if use_cache:
            try:
                raw = kv_get(cache_key, "")
                if raw:
                    blob = json.loads(raw)
                    if self._now() - float(blob.get("ts", 0)) < _TREE_CACHE_TTL_SEC:
                        return [DriveFile(**f) for f in blob.get("files", [])]
            except Exception:
                pass  # unreadable cache is a miss, never a crash

        files: list = []
        frontier = [(folder_id, 0)]
        seen = {folder_id}
        while frontier:
            fid, depth = frontier.pop(0)
            for child in self.list_children(fid):
                files.append(child)
                if child.is_folder and depth + 1 < max_depth and child.id not in seen:
                    seen.add(child.id)
                    frontier.append((child.id, depth + 1))

        try:
            kv_set(cache_key, json.dumps(
                {"ts": self._now(), "files": [asdict(f) for f in files]}))
        except Exception:
            pass  # a cache write failure never blocks the walk result
        return files

    def download(self, file_id: str, dest) -> Path:
        """Stream the file to `dest` (a temp path). Writes to `<dest>.part` first
        and renames on success so a torn download never masquerades as a whole
        file. Returns the final Path."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            with open(part, "wb") as fh:
                self._t().download_to(file_id, fh)
            os.replace(part, dest)
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
        return dest

    def export_doc_text(self, file_id: str) -> str:
        """A Google Doc's plain-text export ('' when the doc exports empty)."""
        return self._t().export_text(file_id) or ""

    def get_folder_meta(self, folder_id: str) -> dict:
        """The connect-confirm payload for a folder the coach is about to bind:
        {name, owner_email, file_count, case}.

          * name         the folder's own title (so the coach confirms the RIGHT
                         folder before Echo binds it).
          * owner_email  the folder owner's email — '' for a Shared Drive item
                         (Shared Drive objects have no per-file owner). The route
                         uses this for the ownership-sanity rail (§1.5c).
          * file_count   how many non-folder children it holds (a first sanity
                         signal that the folder actually has media in it).
          * case         'my_drive' | 'shared_drive' | 'not_shared' — how the SA
                         sees it. not_shared is inferred from a 403/404 raised by
                         get_meta (the SA can see the object exists but cannot read
                         it, i.e. it was never shared to the SA email).

        NEVER raises for the ordinary not-shared path: a 403/404 becomes
        {case:'not_shared'} with empty fields, so the route can say 'not shared
        yet' rather than 'bad link'. Only a truly unusable id (already screened by
        parse_folder_id) reaches here."""
        try:
            meta = self._t().get_meta(folder_id)
        except DriveUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - a Google 403/404 = not shared, not a crash
            status = _http_status(e)
            if status in (403, 404):
                return {"name": "", "owner_email": "", "file_count": 0,
                        "case": "not_shared"}
            # An unexpected transport error is surfaced (the route logs + 502s),
            # never a silent success.
            raise
        owners = meta.get("owners") or []
        owner_email = ""
        if owners and isinstance(owners[0], dict):
            owner_email = str(owners[0].get("emailAddress") or "").strip().lower()
        # Shared Drive items report a driveId and no owner; My Drive items carry an
        # owner. This is the reliable my_drive vs shared_drive discriminator.
        case = "shared_drive" if meta.get("driveId") else "my_drive"
        try:
            children = self.list_children(folder_id)
            file_count = sum(1 for c in children if not c.is_folder)
        except Exception:  # noqa: BLE001 - count is best-effort; readable folder still binds
            file_count = 0
        return {"name": str(meta.get("name") or ""), "owner_email": owner_email,
                "file_count": file_count, "case": case}


# ---- module-level convenience (the spec's flat surface) ---------------------
_default_client = None


def _client() -> DriveClient:
    global _default_client
    if _default_client is None:
        _default_client = DriveClient()
    return _default_client


def list_children(folder_id: str) -> list:
    return _client().list_children(folder_id)


def walk(folder_id: str, max_depth: int = 3) -> list:
    return _client().walk(folder_id, max_depth=max_depth)


def download(file_id: str, dest) -> Path:
    return _client().download(file_id, dest)


def export_doc_text(file_id: str) -> str:
    return _client().export_doc_text(file_id)


def get_folder_meta(folder_id: str) -> dict:
    return _client().get_folder_meta(folder_id)
