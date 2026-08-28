"""Shared offline fakes for the gym_media_drive tests (never any network)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.integrations.drive_client import DriveFile, FOLDER_MIME  # noqa: E402


class FakeMediaStore:
    """In-memory media_source + media_asset store with the SupabaseMediaStore
    interface. list_assets REQUIRES a gym_id (tenant isolation)."""

    def __init__(self, sources=None, assets=None, up=True):
        self.sources = {s["id"]: dict(s) for s in (sources or [])}
        self.assets = {a["id"]: dict(a) for a in (assets or [])}
        self._up = up
        self.updates = []
        self.source_updates = []

    def available(self):
        return self._up

    # sources
    def list_sources(self, gym_id=None, include_inactive=False):
        out = []
        for s in self.sources.values():
            if gym_id is not None and s.get("gym_id") != gym_id:
                continue
            if not include_inactive and not s.get("active", True):
                continue
            out.append(dict(s))
        return out

    def find_source_by_folder(self, folder_id):
        for s in self.sources.values():
            if s.get("folder_id") == folder_id:
                return dict(s)
        return None

    def insert_source(self, row):
        # Enforce the GLOBAL UNIQUE(folder_id) like the DB does.
        for s in self.sources.values():
            if s.get("folder_id") == row.get("folder_id"):
                raise Exception("duplicate key value violates unique constraint "
                                "media_source_folder_unique")
        self.sources[row["id"]] = dict(row)
        return True

    def update_source(self, source_id, fields):
        self.sources.setdefault(source_id, {"id": source_id}).update(fields)
        self.source_updates.append((source_id, dict(fields)))
        return True

    # assets
    def list_assets(self, gym_id, source_id=None):
        if not gym_id:
            raise Exception("list_assets requires a gym_id (tenant isolation)")
        out = []
        for a in self.assets.values():
            if a.get("gym_id") != gym_id:
                continue
            if source_id is not None and a.get("source_id") != source_id:
                continue
            out.append(dict(a))
        return out

    def get_asset(self, asset_id):
        a = self.assets.get(asset_id)
        return dict(a) if a else None

    def insert_assets(self, rows):
        for r in rows:
            self.assets[r["id"]] = dict(r)
        return len(rows)

    def update_asset(self, asset_id, fields):
        self.assets.setdefault(asset_id, {"id": asset_id}).update(fields)
        self.updates.append((asset_id, dict(fields)))
        return True


class _Resp(Exception):
    """A googleapiclient.HttpError stand-in: carries .resp.status so
    drive_client._http_status classifies it (403 = not shared, 404 = gone)."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("R", (), {"status": status})()
        self.status = status


class FakeDriveTransport:
    """A drive_client transport stand-in for get_meta/list_files. Raises a
    status-carrying exception for the not-shared (403) path."""

    def __init__(self, meta=None, children=None, meta_status=None):
        self._meta = meta or {}
        self._children = list(children or [])
        self._meta_status = meta_status

    def get_meta(self, file_id):
        if self._meta_status:
            raise _Resp(self._meta_status)
        return dict(self._meta)

    def list_files(self, query, page_token=None):
        return {"files": [_raw(f) for f in self._children], "nextPageToken": None}

    def download_to(self, file_id, fh):
        fh.write(b"x" * 2048)


def _raw(f):
    return {"id": f.id, "name": f.title, "mimeType": f.mime_type,
            "size": str(f.size_bytes), "parents": [f.parent_id] if f.parent_id else [],
            "modifiedTime": f.modified_time, "md5Checksum": f.content_hash,
            "imageMediaMetadata": {"width": f.width, "height": f.height}}


class FakeDrive:
    """DriveClient stand-in for the sync/builder paths."""

    def __init__(self, files=(), blobs=None, up=True, meta=None, walk_raises=None,
                 thumbs=None):
        self._files = list(files)
        self._blobs = dict(blobs or {})
        self._up = up
        self._meta = meta or {}
        self._walk_raises = walk_raises
        self._thumbs = dict(thumbs or {})   # file_id -> thumb_bytes (None -> no thumb)
        self.downloads = []

    def available(self):
        return self._up

    def thumbnail(self, file_id):
        """(thumb_bytes, 'image/jpeg') when a thumbnail was seeded for this id, else
        None (Drive made none)."""
        data = self._thumbs.get(file_id)
        return (data, "image/jpeg") if data else None

    def walk(self, folder_id, max_depth=4, use_cache=True):
        if self._walk_raises is not None:
            raise self._walk_raises
        return list(self._files)

    def list_children(self, folder_id):
        return [f for f in self._files if f.parent_id == folder_id]

    def get_folder_meta(self, folder_id):
        return dict(self._meta)

    def download(self, file_id, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._blobs.get(file_id, b"x" * 2048))
        self.downloads.append(file_id)
        return dest


def folder(fid, title, parent=""):
    return DriveFile(id=fid, title=title, mime_type=FOLDER_MIME,
                     size_bytes=0, parent_id=parent, modified_time="")


def photo(fid, title="team.jpg", parent="root", size=2_000_000, w=1080, h=1350,
          mime="image/jpeg", md5=None, modified="2026-08-01T00:00:00Z"):
    return DriveFile(id=fid, title=title, mime_type=mime, size_bytes=size,
                     parent_id=parent, modified_time=modified,
                     content_hash=md5 if md5 is not None else fid + "hash",
                     width=w, height=h)


def video(fid, title="clip.mp4", parent="root", size=50_000_000,
          mime="video/mp4", md5=None, modified="2026-08-01T00:00:00Z"):
    return DriveFile(id=fid, title=title, mime_type=mime, size_bytes=size,
                     parent_id=parent, modified_time=modified,
                     content_hash=md5 if md5 is not None else fid + "hash")


def make_asset(fid="a1", gym_id="pierce", source_id="src1", kind="photo",
               title="team.jpg", size=2_000_000, eligible=True,
               excluded_by_coach=False, used_count=0, last_used_at=None,
               content_hash="h1", reject=None, mime="image/jpeg"):
    return {"id": fid, "source_id": source_id, "gym_id": gym_id, "kind": kind,
            "title": title, "mime_type": mime, "size_bytes": size,
            "content_hash": content_hash, "duration_sec": None, "width": 1080,
            "height": 1350, "aspect": "4:5", "crop_hint": None, "vision_json": None,
            "rendition_key": None, "rendition_url": None, "eligible": eligible,
            "excluded_by_coach": excluded_by_coach, "reject_reason": reject,
            "used_count": used_count, "last_used_at": last_used_at,
            "drive_modified": "2026-08-01T00:00:00Z",
            "indexed_at": "2026-08-27T00:00:00+00:00"}


def make_source(sid="src1", gym_id="pierce", folder_id="fold1", active=True,
                revoked=False):
    return {"id": sid, "gym_id": gym_id, "kind": "gym_drive",
            "folder_id": folder_id, "folder_name": "Team Photos",
            "owner_email": "owner@piercewellness.com", "sync_mode": "all",
            "active": active, "revoked_externally": revoked, "connected_by": "u1",
            "connected_at": "2026-08-20T00:00:00Z"}
