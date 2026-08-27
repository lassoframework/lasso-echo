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
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"

_TREE_CACHE_TTL_SEC = 6 * 3600
_TREE_CACHE_KEY = "podcast_drive_tree:{}:{}"
_LIST_FIELDS = "nextPageToken, files(id, name, mimeType, size, parents, modifiedTime)"
_DOWNLOAD_CHUNK = 8 * 1024 * 1024  # 8 MB streaming chunks; a 250 MB clip never sits in RAM


class DriveUnavailable(Exception):
    """No service-account key (or the Google libs are absent). Callers treat
    this as 'lane unarmed', never as a crash."""


@dataclass(frozen=True)
class DriveFile:
    """One Drive object, exactly the fields the spec's client surface names."""
    id: str
    title: str
    mime_type: str
    size_bytes: int
    parent_id: str
    modified_time: str

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


def _to_drive_file(raw) -> DriveFile:
    parents = raw.get("parents") or []
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return DriveFile(
        id=str(raw.get("id") or ""),
        title=str(raw.get("name") or ""),
        mime_type=str(raw.get("mimeType") or ""),
        size_bytes=size,
        parent_id=str(parents[0]) if parents else "",
        modified_time=str(raw.get("modifiedTime") or ""),
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
