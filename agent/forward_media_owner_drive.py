"""Dedicated owner original-reader credentials, never shared publisher auth.

The service account must receive Viewer access to the approved source folders.
Only inline service-account JSON and the fixed Drive read-only OAuth scope are
accepted. No credential file, ADC, shared alias or delegated user fallback.
"""
import json
import os

DRIVE_CREDENTIAL_ENV = 'FORWARD_MEDIA_OWNER_DRIVE_SA_JSON'
DRIVE_READ_SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
TOKEN_URI = 'https://oauth2.googleapis.com/token'


class OwnerDriveHold(RuntimeError):
    """Static credential/transport failures only."""


class DedicatedOwnerDriveTransport:
    def __init__(self):
        self._svc = None

    def _service(self):
        # Unknown credentials added after construction must also fail closed.
        from .forward_media_owner import check_environment
        check_environment()
        if self._svc is not None:
            return self._svc
        try:
            raw = os.environ.get(DRIVE_CREDENTIAL_ENV, '')
            if not raw or len(raw.encode('utf-8')) > 65536:
                raise ValueError('missing or oversized credential')
            info = json.loads(raw)
            if (not isinstance(info, dict) or info.get('type') != 'service_account'
                    or info.get('token_uri') != TOKEN_URI or 'subject' in info
                    or ('universe_domain' in info and info['universe_domain'] != 'googleapis.com')):
                raise ValueError('unsupported credential contract')
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from google_auth_httplib2 import AuthorizedHttp
            import httplib2
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[DRIVE_READ_SCOPE])
            http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=30),
                                  max_refresh_attempts=0)
            self._svc = build('drive', 'v3', http=http, cache_discovery=False)
            return self._svc
        except Exception:
            raise OwnerDriveHold('owner_drive_read_transport_unavailable') from None

    def download_to(self, file_id, output):
        try:
            from googleapiclient.http import MediaIoBaseDownload
            request = self._service().files().get_media(
                fileId=file_id, supportsAllDrives=True)
            download = MediaIoBaseDownload(output, request, chunksize=1024 * 1024)
            done = False
            while not done:
                _, done = download.next_chunk(num_retries=0)
        except Exception:
            raise OwnerDriveHold('owner_drive_original_read_unavailable') from None
