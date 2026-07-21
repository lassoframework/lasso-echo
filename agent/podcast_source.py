"""
Headless podcast episode source: pull the newest episode video from a Google
Drive folder that Riverside auto-exports into.

This is the HEADLESS path for the Railway Monday cron. It uses a Google
service-account key (AGENT_GDRIVE_SA_JSON) via the Google Drive API, NOT the
claude.ai Google Drive connector (which is interactive-auth and unavailable in a
headless cron). Set the service account up once with read access to the folder.

Everything is lazy-imported and fails LOUD with setup instructions when a piece
is missing, so a misconfigured cron never silently no-ops.
"""

import io
import json
import os

from . import config

_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")


class PodcastSourceError(Exception):
    """The newest episode could not be fetched (config/credential/dep missing)."""


def _drive_service():
    sa = config.gdrive_service_account_json()
    if not sa:
        raise PodcastSourceError(
            "AGENT_GDRIVE_SA_JSON is not set. Create a Google service account with "
            "read access to the podcast Drive folder, download its JSON key, and set "
            "AGENT_GDRIVE_SA_JSON to the file path (or the inline JSON) in Railway.")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception:
        raise PodcastSourceError(
            "google-api-python-client / google-auth not installed. Add "
            "'google-api-python-client' and 'google-auth' to requirements.")
    info = json.loads(sa) if sa.strip().startswith("{") else json.load(open(sa))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def newest_episode(dest_dir, folder_id=None):
    """Download the most-recently-created video file from the podcast Drive folder
    to dest_dir. Returns the local path. Raises PodcastSourceError on any missing
    config/credential so the cron surfaces the problem instead of no-opping."""
    folder_id = folder_id or config.podcast_drive_folder_id()
    if not folder_id:
        raise PodcastSourceError(
            "AGENT_PODCAST_DRIVE_FOLDER_ID is not set. Point it at the Google Drive "
            "folder Riverside auto-exports episodes into.")
    svc = _drive_service()
    q = (f"'{folder_id}' in parents and trashed = false and "
         "(mimeType contains 'video/')")
    resp = svc.files().list(
        q=q, orderBy="createdTime desc", pageSize=10,
        fields="files(id,name,mimeType,createdTime,size)").execute()
    files = resp.get("files", [])
    files = [f for f in files
             if os.path.splitext(f["name"])[1].lower() in _VIDEO_EXTS
             or "video/" in f.get("mimeType", "")]
    if not files:
        raise PodcastSourceError(
            f"No video files found in Drive folder {folder_id}. Confirm Riverside "
            "is exporting there and the service account can see it.")
    newest = files[0]
    os.makedirs(dest_dir, exist_ok=True)
    out = os.path.join(dest_dir, newest["name"])
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out   # already pulled this episode
    from googleapiclient.http import MediaIoBaseDownload
    req = svc.files().get_media(fileId=newest["id"])
    with open(out, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _status, done = dl.next_chunk()
    print(f"[podcast-source] pulled newest episode: {newest['name']} "
          f"(created {newest.get('createdTime')})", flush=True)
    return out
