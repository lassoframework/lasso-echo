"""DRAFT read-only generated owner API adapter, default OFF and always HOLD.

Current owner tables have no authenticated generated job/copy/palette binding,
complete Drive inventory or full generated exact/perceptual history authority.
The read RPC supplies DB census diagnostics, including undated calendar rows,
claims and photo reservations. Those revisions are observations of this DB only.
They are never completeness or photo depletion attestations. A published-only
1,398-row census, empty local pool or photo-only audit cannot supply that proof.

This isolated adapter is compatible with OwnerEvidenceClient's POST /snapshot
and /history-check contract. It is not registered on the publisher intake server.
An independently deployed TLS service may call handle() with manually provisioned
read credentials/tokens. No connection factory, token generation, IAM/storage,
provider calls or generated reservation/grant path is supplied here.
"""
import base64
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hmac
import io
import json
import re
import uuid

from .forward_media_generated_prepare import (
    GenerationRequest, GeneratedReceiptHold, MAX_BYTES, PreparedGeneratedOriginal,
    canonical, sha256,
)
from .forward_media_generated_issuer import POLICY

READ_ROLE = 'fixer_generated_owner_reader_20261007'
MAX_BODY_BYTES = 4 * ((MAX_BYTES + 2) // 3) + 32768
MISSING_AUTHORITIES = (
    'trusted_generated_job_binding', 'versioned_approved_palette_and_copy',
    'complete_authenticated_photo_inventory',
    'complete_exact_perceptual_undated_unresolved_history',
    'generated_owner_reservation_and_claim_fence',
)
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')


def hold(reason):
    raise GeneratedReceiptHold(reason)


def validated_request(value):
    """Shape validation only. A request echo never authenticates a job/source."""
    try:
        if not isinstance(value, dict) or set(value) != set(GenerationRequest.__dataclass_fields__):
            hold('generated_owner_request_invalid')
        for v in value.values():
            if (not isinstance(v, str) or not v or v != v.strip() or len(v) > 2048
                    or any(ord(c) < 32 for c in v)):
                hold('generated_owner_request_invalid')
        for name in ('job_id', 'request_id'):
            if str(uuid.UUID(value[name])) != value[name]:
                hold('generated_owner_request_invalid')
        if date.fromisoformat(value['post_date']).isoformat() != value['post_date']:
            hold('generated_owner_request_invalid')
        when = datetime.fromisoformat(value['requested_at'].replace('Z', '+00:00'))
        if not value['requested_at'].endswith('Z') or when.tzinfo != timezone.utc:
            hold('generated_owner_request_invalid')
        if (not _SHA.fullmatch(value['palette_digest'])
                or not _SHA.fullmatch(value['source_copy_digest'])
                or value['pixel_policy_id'] != POLICY):
            hold('generated_owner_request_invalid')
        return GenerationRequest(**value)
    except GeneratedReceiptHold:
        raise
    except Exception:
        hold('generated_owner_request_invalid')


class OwnerEvidenceReadStore:
    """Dedicated read role only. Each query closes its read transaction.

    Caller supplies a separately configured connection, never the owner writer
    or a generic service DSN. Idle, READ ONLY REPEATABLE READ transactions keep
    the census consistent without taking authority locks across remote I/O.
    """
    def __init__(self, connection, expected_role):
        self.conn, self.expected_role = connection, expected_role

    def diagnostics(self, request):
        from psycopg.pq import TransactionStatus
        if (type(request) is not GenerationRequest or self.conn.autocommit
                or self.conn.info.transaction_status != TransactionStatus.IDLE):
            hold('generated_owner_read_transaction_required')
        try:
            with self.conn.cursor() as cur:
                cur.execute('set transaction isolation level repeatable read read only')
                cur.execute("select current_user,session_user,pg_has_role(current_user,%s,'member'),"
                    "pg_has_role(current_user,'fixer_forward_media_owner_20261006','member'),"
                    "pg_has_role(current_user,'fixer_forward_media_attester_20261006','member'),"
                    "pg_has_role(current_user,'fixer_forward_media_photo_auditor_20261007','member'),"
                    "pg_has_role(current_user,'fixer_forward_media_history_auditor_20261007','member'),"
                    "pg_has_role(current_user,'service_role','member'),"
                    "(select rolsuper or rolbypassrls from pg_roles where rolname=current_user)", (READ_ROLE,))
                identity = cur.fetchone()
                if (not isinstance(self.expected_role, str)
                        or self.expected_role in ('anon', 'authenticated', 'service_role')
                        or identity != (self.expected_role, self.expected_role, True, False, False, False, False, False, False)):
                    hold('generated_owner_read_identity_required')
                cur.execute('select public.fixer_generated_owner_diagnostics_20261007(%s)',
                            (request.tenant_id,))
                result = cur.fetchone()[0]
            if (not isinstance(result, dict) or result.get('tenant_id') != request.tenant_id
                    or result.get('diagnostic_only') is not True
                    or result.get('missing_authorities') != list(MISSING_AUTHORITIES)
                    or any(result.get(k) is not False for k in (
                        'history_complete', 'photo_inventory_complete',
                        'brand_source_verified', 'reviewed_no_match'))
                    or result.get('eligible_photo_count') is not None
                    or any(not _SHA.fullmatch(str(result.get(k, ''))) for k in (
                        'history_revision', 'photo_inventory_revision'))):
                hold('generated_owner_diagnostics_invalid')
            return json.loads(canonical(result))
        except GeneratedReceiptHold:
            raise
        except Exception:
            hold('generated_owner_read_unavailable')
        finally:
            self.conn.rollback()


@dataclass(frozen=True)
class ReadTokenBinding:
    """Manually supplied isolated read token and authorized tenant allowlist."""
    token: str
    role: str
    tenants: frozenset


class OwnerEvidenceReadAPI:
    """Transport-neutral POST handlers; default OFF, no positive authority.

    Authentication precedes parsing/pixel work/DB queries. Responses contain
    counts and static holds only, never raw calendar, source or historical rows.
    Caller serves through isolated TLS with request size/time/concurrency limits;
    the existing service/publisher credentials must not reach that process.
    """
    def __init__(self, store, bindings, *, enabled=False):
        self.store, self.bindings, self.enabled = store, tuple(bindings), enabled

    def handle(self, method, path, authorization, body):
        try:
            if self.enabled is not True:
                return 503, {'status': 'hold', 'reason': 'generated_owner_read_disabled'}
            if method != 'POST' or path not in ('/snapshot', '/history-check'):
                return 404, {'status': 'hold', 'reason': 'generated_owner_route_unavailable'}
            token = authorization[7:] if isinstance(authorization, str) and authorization.startswith('Bearer ') else ''
            bindings = [b for b in self.bindings if type(b) is ReadTokenBinding
                        and isinstance(b.token, str) and len(b.token) >= 32
                        and b.role in ('issuer', 'verifier') and type(b.tenants) is frozenset
                        and token and hmac.compare_digest(token.encode(), b.token.encode())]
            if len(bindings) != 1:
                return 401, {'status': 'hold', 'reason': 'generated_owner_read_unauthorized'}
            bound = 32768 if path == '/snapshot' else MAX_BODY_BYTES
            if type(body) is not bytes or not 0 < len(body) <= bound:
                hold('generated_owner_body_invalid')
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        hold('generated_owner_body_invalid')
                    result[key] = value
                return result
            packet = json.loads(body, object_pairs_hook=unique_object)
            required = {'request'} if path == '/snapshot' else {'request', 'image_sha256', 'image_base64'}
            if not isinstance(packet, dict) or set(packet) != required:
                hold('generated_owner_body_invalid')
            request = validated_request(packet['request'])
            if request.tenant_id not in bindings[0].tenants:
                return 403, {'status': 'hold', 'reason': 'generated_owner_tenant_unauthorized'}
            image_sha = None
            if path == '/history-check':
                image_sha = self._validate_pixels(packet)
            if type(self.store) is not OwnerEvidenceReadStore:
                hold('generated_owner_read_store_required')
            diagnostics = self.store.diagnostics(request)
            # Explicit negatives, even if an adapter changes its diagnostics.
            # Echoing a supplied request is not verified source/job binding.
            result = {'request': asdict(request), 'status': 'hold',
                'reason': 'generated_owner_authority_incomplete',
                'brand_source_verified': False, 'palette': None, 'copy': None,
                'photo_inventory_complete': False, 'eligible_photo_count': None,
                'photo_inventory_revision': diagnostics['photo_inventory_revision'],
                'history_complete': False, 'reviewed_no_match': False,
                'history_revision': diagnostics['history_revision'],
                'diagnostics': diagnostics}
            if image_sha is not None:
                result['image_sha256'] = image_sha
            return 200, result
        except GeneratedReceiptHold as exc:
            return 422, {'status': 'hold', 'reason': str(exc)}
        except Exception:
            return 422, {'status': 'hold', 'reason': 'generated_owner_body_invalid'}

    @staticmethod
    def _validate_pixels(packet):
        try:
            digest, encoded = packet['image_sha256'], packet['image_base64']
            if not isinstance(digest, str) or not _SHA.fullmatch(digest) or not isinstance(encoded, str):
                hold('generated_owner_pixels_invalid')
            data = base64.b64decode(encoded, validate=True)
            if not 0 < len(data) <= MAX_BYTES or sha256(data) != digest:
                hold('generated_owner_pixels_invalid')
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                if image.format != 'PNG' or not 100 <= image.width <= 8192 or not 100 <= image.height <= 8192:
                    hold('generated_owner_pixels_invalid')
                image.verify()
            return digest
        except Exception:
            hold('generated_owner_pixels_invalid')


def stage_prepared_generated_original(persistence, prepared, *, enabled=False):
    """Fail-closed owner boundary, before any cursor/lock/write/remote read.

    Existing positive clearance triggers require a certified same-gym Drive
    photo reservation. Generated originals cannot be relabeled as that source.
    A new authority must independently reverify the signed receipt, bind the
    trusted job, recheck full inventory/history/palette/copy under graph locks,
    include every generated reservation in the next review, and fence claims.
    Those contracts are absent, so this boundary has no grant or replay path.
    """
    if enabled is not True:
        hold('generated_owner_reservation_disabled')
    from .forward_media_owner import ForwardMediaOwnerPersistence
    if type(persistence) is not ForwardMediaOwnerPersistence or type(prepared) is not PreparedGeneratedOriginal:
        hold('generated_owner_prepared_contract_required')
    hold('generated_owner_reservation_authority_unavailable')
