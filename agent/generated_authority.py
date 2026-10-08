"""Unwired GENERATED-ONLY canonical Postgres contract.

No environment credentials, local source import, auto approval, provider calls
or feature activation. The integration owner supplies a dedicated idle DB
connection and an authenticated tenant directory. Receipt authenticity and
original-byte provenance remain the caller's responsibility. Failed/ambiguous
writes never become authority and are never retried automatically.
"""
from __future__ import annotations

import hashlib
import json
import re
from types import MappingProxyType


class AuthorityHold(RuntimeError):
    """Static diagnostic; do not expose DB errors or source data."""


def sha256(data):
    if not isinstance(data, bytes) or not data:
        raise AuthorityHold('generated_original_bytes_required')
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise AuthorityHold('generated_authority_input_invalid')
    return value


def source_pending(source_id, *, category, exact_text, citation, origin_ref,
                   intake_revision, original_intake):
    """New content always starts pending, including updates to approved facts."""
    return dict(op='source_upsert', source_id=_text(source_id), category=_text(category),
                exact_text=_text(exact_text), citation=_text(citation), origin_ref=_text(origin_ref),
                intake_revision=_text(intake_revision), intake_sha256=sha256(original_intake),
                content_sha256=sha256(exact_text.encode('utf-8')))


def source_approval(source_id, *, revision, content_sha256, intake_sha256, actor, receipt):
    if type(revision) is not int or revision < 1:
        raise AuthorityHold('generated_exact_revision_required')
    return dict(op='source_approve', source_id=_text(source_id), revision=revision,
                content_sha256=_text(content_sha256), intake_sha256=_text(intake_sha256),
                approval_mode='explicit', approval_actor=_text(actor), approval_evidence=_text(receipt))


def palette_verified(palette_key, *, colors, origin_ref, intake_revision, original_intake,
                     file_revision, original_file, actor, receipt):
    """Digest ORIGINAL verified file bytes, never reconstructed JSON/colors."""
    if not isinstance(colors, (list, tuple)) or not colors:
        raise AuthorityHold('generated_palette_input_invalid')
    return dict(op='palette_upsert', palette_key=_text(palette_key), colors=list(colors),
                origin_ref=_text(origin_ref), intake_revision=_text(intake_revision),
                intake_sha256=sha256(original_intake), file_revision=_text(file_revision),
                file_sha256=sha256(original_file), approval_mode='explicit',
                approval_actor=_text(actor), verification_evidence=_text(receipt))


def revoke_source(source_id):
    return dict(op='source_tombstone', source_id=_text(source_id))


def revoke_palette(palette_key):
    return dict(op='palette_revoke', palette_key=_text(palette_key))


class GeneratedAuthority:
    """Tenant-bound function-only adapter. Caller supplies verified identity.

    Directory must map both canonical tenant and exact aliases to that tenant;
    no suffix stripping, punctuation normalization or inferred alias is allowed.
    The authenticated principal must have that canonical tenant in its grant set.
    A dedicated connection prevents committing another owner's transaction.
    """
    def __init__(self, connection, *, tenant_id, tenant_directory, authorized_tenants):
        self.tenant_id = _text(tenant_id)
        directory = dict(tenant_directory)
        if directory.get(tenant_id) != tenant_id or tenant_id not in authorized_tenants:
            raise AuthorityHold('generated_tenant_not_authorized')
        self.directory = MappingProxyType(directory)
        self.connection = connection

    def _bound(self, alias):
        if self.directory.get(alias) != self.tenant_id:
            raise AuthorityHold('generated_tenant_alias_unverified')
        info = getattr(self.connection, 'info', None)
        if (info is None or int(info.transaction_status) != 0
                or getattr(self.connection, 'autocommit', None) is not False):
            raise AuthorityHold('generated_authority_connection_busy')

    def snapshot(self, alias):
        self._bound(alias)
        try:
            result = self.connection.execute(
                'select public.generated_authority_snapshot_20261007(%s)',
                (self.tenant_id,)).fetchone()[0]
            if (not isinstance(result, dict) or result.get('tenant_id') != self.tenant_id
                    or not isinstance(result.get('sources'), list)
                    or not isinstance(result.get('palettes'), list)
                    or (result.get('epoch') is not None and
                        (type(result['epoch']) is not int or result['epoch'] < 0))
                    or any(not isinstance(r, dict) or r.get('tenant_id') != self.tenant_id
                           for r in result['sources'] + result['palettes'])
                    or (result.get('epoch') is None and (result['sources'] or result['palettes']))):
                raise AuthorityHold('generated_authority_readback_invalid')
            return result
        except AuthorityHold:
            raise
        except Exception:
            raise AuthorityHold('generated_authority_unavailable') from None
        finally:
            try:
                self.connection.rollback()
            except Exception:
                raise AuthorityHold('generated_authority_unavailable') from None

    def current(self, alias, *, expected_epoch, source_id, source_revision,
                palette_key, palette_revision):
        """Fresh canonical read for preparation only, never a publish decision.

        Exact pinned epoch/revisions plus explicit receipts are mandatory. The
        later publisher integration must use DB validation under its own locks.
        """
        if any(type(v) is not int or v < 1 for v in
               (expected_epoch, source_revision, palette_revision)):
            raise AuthorityHold('generated_exact_revision_required')
        snapshot = self.snapshot(alias)
        if snapshot['epoch'] != expected_epoch:
            raise AuthorityHold('generated_authority_epoch_changed')
        source = [r for r in snapshot['sources'] if r.get('source_id') == source_id]
        palette = [r for r in snapshot['palettes'] if r.get('palette_key') == palette_key]
        if len(source) != 1 or len(palette) != 1:
            raise AuthorityHold('generated_canonical_authority_missing')
        source, palette = source[0], palette[0]
        try:
            if (source['status'] != 'approved' or source['revision'] != source_revision
                    or source['approval_revision'] != source_revision
                    or source['approval_mode'] != 'explicit'
                    or source['content_sha256'] != sha256(_text(source['exact_text']).encode())
                    or palette['status'] != 'active' or palette['revision'] != palette_revision
                    or not isinstance(palette['colors'], list) or not palette['colors']
                    or any(not isinstance(c, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', c)
                           for c in palette['colors'])):
                raise ValueError()
            for row in (source, palette):
                for key in ('origin_ref', 'intake_revision', 'approval_actor'):
                    _text(row[key])
                if not re.fullmatch(r'sha256:[0-9a-f]{64}', row['intake_sha256']):
                    raise ValueError()
            _text(source['approval_evidence'])
            _text(source['approved_by'])
            _text(palette['verification_evidence'])
            _text(palette['file_revision'])
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', palette['file_sha256']):
                raise ValueError()
        except Exception:
            raise AuthorityHold('generated_canonical_authority_invalid') from None
        return dict(tenant_id=self.tenant_id, epoch=expected_epoch, source=source, palette=palette)

    def write(self, alias, *, expected_epoch, operations):
        self._bound(alias)
        if type(expected_epoch) is not int or expected_epoch < 0 or not isinstance(operations, list) or not operations:
            raise AuthorityHold('generated_authority_input_invalid')
        try:
            payload = json.dumps(operations, allow_nan=False)
            epoch = self.connection.execute(
                'select public.generated_authority_write_20261007(%s,%s,%s::jsonb)',
                (self.tenant_id, expected_epoch, payload)).fetchone()[0]
            if type(epoch) is not int or epoch != expected_epoch + 1:
                raise AuthorityHold('generated_authority_write_readback_invalid')
        except Exception:
            try:
                self.connection.rollback()
            except Exception:
                raise AuthorityHold('generated_authority_write_uncertain') from None
            raise AuthorityHold('generated_authority_write_rejected') from None
        try:
            self.connection.commit()
        except Exception:
            # Server may already have committed. Reconcile snapshot/audit against
            # frozen epoch+ops on a NEW connection; never replay at a fresh epoch.
            raise AuthorityHold('generated_authority_write_uncertain') from None
        return epoch
