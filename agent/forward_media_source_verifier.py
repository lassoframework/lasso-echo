"""DRAFT owner original-byte verification. No side effects on import or history clearance.

Called outside ALL database transactions/locks. Canonical binding comes only
from the owner SQL snapshot, never producer observations. A fresh Drive metadata
read, bounded original download and second metadata read prove current bytes;
the exact persisted source URL must serve byte-identical bytes. Today's bytes
do not prove historical publication bytes. Receipts are staged only after the
owner reacquires graph/row locks and revalidates the complete binding snapshot.
"""
from dataclasses import dataclass
import hashlib
import io
import json
import re
import time

from .forward_media_prepare import MAX_OBJECT_LENGTH, register_original

_ID = re.compile(r'[A-Za-z0-9_-]{16,128}\Z')
_MD5 = re.compile(r'[0-9a-f]{32}\Z')
_META_FIELDS = 'id,mimeType,parents,trashed,version,size,md5Checksum'
_FOLDER_MIME = 'application/vnd.google-apps.folder'


class SourceVerificationHold(RuntimeError):
    """Only static reason codes escape this read boundary."""


class _BoundedBuffer(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.deadline = time.monotonic() + 120

    def write(self, data):
        if time.monotonic() > self.deadline:
            raise SourceVerificationHold('source_read_deadline_exceeded')
        if self.tell() + len(data) > MAX_OBJECT_LENGTH:
            raise SourceVerificationHold('source_object_exceeds_bound')
        return super().write(data)


class OriginalDriveReader:
    """Existing read-only Drive credential route; no thumbnail/export fallback.

    Instantiation does not fetch credentials. The existing dedicated transport
    uses a 30-second request timeout; downloads also have a 120-second streaming
    deadline. No thumbnail/export fallback or network retries.
    """
    def __init__(self, transport=None):
        self._transport = transport

    def _t(self):
        if self._transport is None:
            from .integrations.drive_client import GoogleDriveTransport
            self._transport = GoogleDriveTransport()
        return self._transport

    def metadata(self, file_id):
        service = self._bounded_service()
        return service.files().get(
            fileId=file_id, fields=_META_FIELDS, supportsAllDrives=True).execute(num_retries=0)

    def _bounded_service(self):
        service = self._t()._service()
        # Google-auth AuthorizedHttp wraps httplib2.Http in .http. This is a
        # dedicated reader/transport instance, never a shared mutable session.
        request_http = getattr(service._http, 'http', service._http)
        if not hasattr(request_http, 'timeout'):
            raise SourceVerificationHold('drive_timeout_contract_unavailable')
        request_http.timeout = 30
        return service

    def original_bytes(self, file_id):
        self._bounded_service()
        buf = _BoundedBuffer()
        self._t().download_to(file_id, buf)
        return buf.getvalue()


def _meta(drive, file_id):
    if not isinstance(file_id, str) or not _ID.fullmatch(file_id):
        raise SourceVerificationHold('drive_identity_invalid')
    value = drive.metadata(file_id)
    if (not isinstance(value, dict) or value.get('id') != file_id
            or value.get('trashed') is not False):
        raise SourceVerificationHold('drive_metadata_unavailable')
    return value


def _path(drive, meta, folder_id):
    """Single unambiguous current parent chain, max eight edges, no cached tree."""
    seen, path = {meta['id']}, []
    for _ in range(8):
        parents = meta.get('parents')
        if not isinstance(parents, list) or len(parents) != 1:
            raise SourceVerificationHold('drive_folder_membership_unknown')
        parent = parents[0]
        if parent in seen:
            raise SourceVerificationHold('drive_folder_membership_unknown')
        seen.add(parent)
        meta = _meta(drive, parent)
        if meta.get('mimeType') != _FOLDER_MIME:
            raise SourceVerificationHold('drive_folder_membership_unknown')
        path.append({'id': parent, 'version': str(meta.get('version') or ''),
                     'parents': meta.get('parents')})
        if parent == folder_id:
            return path
    raise SourceVerificationHold('drive_folder_membership_unknown')


@dataclass(frozen=True)
class VerifiedSource:
    original: object
    source_bytes: bytes
    evidence: dict
    receipt_ref: str


def verify_source(snapshot, drive, hosted_reader):
    """Independent current original and hosted exact-byte check, no DB writes.

    Caller must use the trusted OriginalDriveReader and HostedObjectReader in
    the dedicated owner runtime. Injectable readers are offline fixture seams,
    never production factories or caller-provided clearance callbacks.
    """
    try:
        row, asset, source = snapshot['calendar'], snapshot['asset'], snapshot['source']
        tenant, file_id, exact_url = row['gym_id'], asset['id'], row['source_media_url']
        if (row['source_media_asset_id'] != file_id or asset['gym_id'] != tenant
                or asset['source_id'] != source['id'] or source['gym_id'] != tenant
                or source['active'] is not True or source['kind'] != 'gym_drive'
                or not isinstance(snapshot['revision'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', snapshot['binding_revision'])):
            raise SourceVerificationHold('source_binding_invalid')
        before = _meta(drive, file_id)
        if (str(before.get('mimeType') or '').startswith('application/vnd.google-apps.')
                or not str(before.get('version') or '')
                or not _MD5.fullmatch(str(before.get('md5Checksum') or ''))):
            raise SourceVerificationHold('drive_original_unavailable')
        length = int(before['size'])
        if not 0 < length <= MAX_OBJECT_LENGTH:
            raise SourceVerificationHold('source_object_exceeds_bound')
        path_before = _path(drive, before, source['folder_id'])
        original_bytes = drive.original_bytes(file_id)
        if (not isinstance(original_bytes, bytes) or len(original_bytes) != length
                or hashlib.md5(original_bytes).hexdigest() != before['md5Checksum']):
            raise SourceVerificationHold('drive_original_bytes_mismatch')
        after = _meta(drive, file_id)
        if before != after or path_before != _path(drive, after, source['folder_id']):
            raise SourceVerificationHold('drive_original_changed_during_read')
        # Never substitute rendition_url, derive a URL or normalize signed query strings.
        hosted = hosted_reader.read(exact_url)
        if not isinstance(hosted, bytes) or hosted != original_bytes:
            raise SourceVerificationHold('hosted_source_differs_from_drive_original')
        evidence = {
            'schema_version': 1, 'calendar_row_id': row['id'],
            'row_revision': snapshot['revision'], 'binding_revision': snapshot['binding_revision'],
            'tenant_id': tenant, 'source_asset_id': file_id, 'source_id': source['id'],
            'folder_id': source['folder_id'], 'exact_source_url': exact_url,
            'source_fingerprint': 'md5:' + hashlib.md5(original_bytes).hexdigest(),
            'source_sha256': 'sha256:' + hashlib.sha256(original_bytes).hexdigest(),
            'source_length': length, 'drive_version': str(before['version']),
            'drive_parent_path': path_before,
        }
        canonical = json.dumps(evidence, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        ref = 'source-receipt:sha256:' + hashlib.sha256(canonical.encode()).hexdigest()
        original = register_original(tenant, file_id, exact_url, original_bytes, ref)
        return VerifiedSource(original, original_bytes, evidence, ref)
    except SourceVerificationHold:
        raise
    except Exception:
        raise SourceVerificationHold('source_verification_unavailable') from None
