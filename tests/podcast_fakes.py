"""Shared offline fakes for the podcast library tests (never any network)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.integrations.drive_client import DriveFile, DOC_MIME, FOLDER_MIME  # noqa: E402


class FakeStore:
    """In-memory podcast_asset store with the SupabasePodcastStore interface."""

    def __init__(self, assets=None, up=True):
        self.assets = {a["id"]: dict(a) for a in (assets or [])}
        self._up = up
        self.updates = []  # (asset_id, fields) log for assertions

    def available(self):
        return self._up

    def list_assets(self):
        return [dict(a) for a in self.assets.values()]

    def insert_assets(self, rows):
        for r in rows:
            self.assets[r["id"]] = dict(r)
        return len(rows)

    def update_asset(self, asset_id, fields):
        self.assets.setdefault(asset_id, {"id": asset_id}).update(fields)
        self.updates.append((asset_id, dict(fields)))
        return True


class FakeDrive:
    """DriveClient stand-in: a fixed walked tree, doc texts, and file bytes."""

    def __init__(self, files=(), docs=None, blobs=None, up=True):
        self._files = list(files)
        self._docs = dict(docs or {})
        self._blobs = dict(blobs or {})
        self._up = up
        self.downloads = []

    def available(self):
        return self._up

    def walk(self, folder_id, max_depth=3, use_cache=True):
        return list(self._files)

    def list_children(self, folder_id):
        return [f for f in self._files if f.parent_id == folder_id]

    def export_doc_text(self, file_id):
        return self._docs.get(file_id, "")

    def download(self, file_id, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._blobs.get(file_id, b"x" * 2048))
        self.downloads.append(file_id)
        return dest


class FakeZernio:
    """ZernioClient stand-in for the presign -> PUT -> ready flow."""

    def __init__(self, ready=True):
        self.ready = ready
        self.uploads = []

    def media_generate_upload_link(self, filename, content_type):
        return {"uploadUrl": f"https://upload.fake/{filename}",
                "publicUrl": f"https://cdn.fake/{filename}"}

    def media_upload_file(self, upload_url, path, content_type):
        self.uploads.append((upload_url, str(path), content_type))
        return True

    def media_check_upload_status(self, public_url, **kwargs):
        return self.ready


def folder(fid, title, parent=""):
    return DriveFile(id=fid, title=title, mime_type=FOLDER_MIME,
                     size_bytes=0, parent_id=parent, modified_time="")


def video(fid, title, parent, size=100_000_000):
    return DriveFile(id=fid, title=title, mime_type="video/mp4",
                     size_bytes=size, parent_id=parent, modified_time="")


def doc(fid, title, parent):
    return DriveFile(id=fid, title=title, mime_type=DOC_MIME,
                     size_bytes=0, parent_id=parent, modified_time="")


def audio(fid, title, parent, size=50_000_000):
    return DriveFile(id=fid, title=title, mime_type="audio/mpeg",
                     size_bytes=size, parent_id=parent, modified_time="")


NOTES_DOC_TEXT = """GMMS 140: How A Small Town Gym Added 60 Members In 90 Days
Guest: Casey Example

Episode notes
- The gym rebuilt its intro offer around a 6 week onboarding path instead of a drop in pass
- Casey walks through the exact follow up cadence her front desk uses for every new lead
- Referral asks moved from the front desk to the coaches and referrals tripled in one quarter
- The team stopped discounting and raised close rates by fixing the tour script instead
- Why the owner tracks show rate before spend every single week

Find Casey at @caseyexample
"""


def make_asset(fid="clip140s1", episode=140, kind="clip", clip_index=1,
               title="GMMS-140-S1.mp4", size=248_000_000, duration=42.0,
               width=1080, height=1920, aspect="9:16", postable=True,
               reject=None, used_count=0, last_used_at=None,
               notes_doc_id="doc140"):
    return {"id": fid, "episode": episode, "kind": kind,
            "clip_index": clip_index, "title": title, "size_bytes": size,
            "duration_sec": duration, "width": width, "height": height,
            "aspect": aspect, "postable": postable, "reject_reason": reject,
            "used_count": used_count, "last_used_at": last_used_at,
            "notes_doc_id": notes_doc_id, "indexed_at": "2026-08-27T00:00:00+00:00"}
