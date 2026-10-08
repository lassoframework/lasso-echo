"""DRAFT / DEFAULT-OFF trusted attester-side visual index for forward media.

Follows ``agent/forward_media_guard.py`` patterns: the same dedicated attester
credential lane (``AGENT_FORWARD_MEDIA_ATTESTER_DSN``, role
``fixer_forward_media_attester_20261006``), an independent default-OFF env flag
(``AGENT_FORWARD_MEDIA_VISUAL_INDEX``), and ``ForwardMediaVerificationHold``
semantics.

For the three roles (original, delivered, thumbnail) this module fetches the
ACTUAL served bytes (validated by ``visual_writer_prepare._own_media_url``;
never producer-asserted hashes or URLs), computes SHA256, MD5, byte length and
the versioned pHash v1 (the existing 64-bit DCT pHash from
``agent/visual_scene.py``) from those fetched bytes, and appends attestation
rows to ``forward_media_visual_attestation`` linked to the exact URL, tenant,
row revision, lineage_receipt_id and object_read_receipt_id.

Database protocol: the snapshot/lineage reads happen first, the read-only
transaction is ended (rollback) BEFORE any object fetch so no DB lock or
snapshot is held across network work, then a fresh transaction appends the
attestation rows. Incomplete, spoofed, contradictory or undecodable evidence
holds when armed. Only attributable offending bytes append negative evidence to
``forward_media_visual_negative`` (which survives deletion/replacement).
When the flag is OFF the publisher claim path remains a no-op pass-through;
explicit isolated attester preparation never grants claim eligibility.

Row revision: the existing guard stack identifies the exact persisted media
snapshot by an md5 text revision (``fixer_forward_media_attestation_request_
20261006``). The visual index table stores ``row_revision bigint``; v1 derives
it deterministically as the top 60 bits of that md5 hex
(``int(revision[:15], 16)``) so both sides recompute the identical value from
the same persisted snapshot.
"""
from __future__ import annotations

import hashlib
import os
import uuid

from .forward_media_guard import (
    ROLE,
    ForwardMediaVerificationHold,
    _read,
    _uuid,
)

PHASH_VERSION = 1
ROLES = ('original', 'delivered', 'thumbnail')
_SNAPSHOT_RPC = 'public.fixer_forward_media_attestation_request_20261006(%s)'
_LINEAGE_QUERY = 'select * from public.fixer_forward_visual_receipts_20261008(%s,%s,%s)'
_ATTEST_INSERT = ('insert into public.forward_media_visual_attestation'
                  '(tenant_key,media_url,role,source_sha256,source_md5,byte_length,'
                  'phash_version,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id)'
                  ' values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning attestation_id')
_NEGATIVE_INSERT = 'select public.fixer_forward_visual_negative_append_20261008(%s,%s,%s,%s,%s)'
# Recovery read: the attester role holds SELECT on its own evidence table. A
# crash after lineage commit leaves the row outside pending discovery; the
# existing role attestations under the SAME immutable lineage are reused, and
# only missing roles are appended. No second lineage is ever created here.
_EXISTING_ROLES_SELECT = ('select role,attestation_id,media_url from '
                          'public.forward_media_visual_attestation '
                          'where lineage_receipt_id=%s and row_revision=%s and tenant_key=%s')


def enabled():
    return os.getenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', '').lower() in ('1', 'true', 'yes', 'on')


def row_revision_from(revision):
    """Deterministic bigint row revision v1: top 60 bits of the md5 revision."""
    if not isinstance(revision, str) or not revision:
        raise ForwardMediaVerificationHold('expected media revision required')
    try:
        return int(revision[:15], 16)
    except ValueError as exc:
        raise ForwardMediaVerificationHold('persisted media revision invalid') from exc


def phash_v1(data):
    """Versioned pHash v1 as a signed bigint from ACTUAL bytes, or None.

    None means undecodable/non-image bytes; callers hold when armed. The raw
    64-bit DCT digest is mapped into signed bigint range for storage.
    """
    from . import visual_scene
    fingerprint = visual_scene.scene_fingerprint(data)
    if fingerprint is None:
        return None
    value = int(fingerprint.rsplit(':', 1)[-1], 16)
    return value - (1 << 64) if value >= (1 << 63) else value



def _proven_non_image(data):
    """Only a recognized invalid-image error supports a permanent byte negative.

    The pHash helper returns None for both decode and runtime failures. Missing
    dependencies or failed hashing must not blacklist valid bytes.
    """
    try:
        import io
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except UnidentifiedImageError:
        return True
    except Exception:
        return False
    return False

def _connect():
    from . import forward_media_guard as guard
    return guard._connect()


def _record_negative(conn, tenant_key, reason, *, lineage_id=None, source_sha256=None, phash=None):
    """Best-effort immutable negative evidence; never masks the hold.

    The negative contract requires byte-derived evidence (sha256 or pHash), so
    a hold with no readable bytes records nothing here; the hold itself is the
    authority. The dedicated attester uses a lineage/tenant-bound append RPC; a refused
    append never clears the hold.
    """
    if source_sha256 is None and phash is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(_NEGATIVE_INSERT,
                        (lineage_id, tenant_key or 'unknown', source_sha256, phash, reason))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _snapshot_and_receipts(cur, row_id, expected_revision, lineage_receipt_id):
    cur.execute('select ' + _SNAPSHOT_RPC, (row_id,))
    snapshot = cur.fetchone()[0]
    if not isinstance(snapshot, dict) or snapshot.get('revision') != expected_revision:
        raise ForwardMediaVerificationHold('persisted media revision changed')
    cur.execute(_LINEAGE_QUERY, (row_id, expected_revision, lineage_receipt_id))
    receipts = cur.fetchone()
    if not receipts or not receipts[0] or not receipts[1]:
        raise ForwardMediaVerificationHold('trusted lineage object receipts unavailable')
    return snapshot, receipts


def attest(calendar_row_id, expected_revision, lineage_receipt_id, *,
           tenant_key=None, content_date=None, gym_key=None,
           connection_factory=None, read_bytes=None):
    """Fetch actual bytes for all three roles and append visual attestations.

    Raises ``ForwardMediaVerificationHold`` on incomplete, spoofed,
    contradictory or undecodable evidence. Only attributable offending bytes
    produce best-effort negative evidence. Returns the appended attestation ids.
    """
    row_id = _uuid(calendar_row_id)
    lineage_id = _uuid(lineage_receipt_id)
    row_revision = row_revision_from(expected_revision)
    try:
        conn = connection_factory() if connection_factory else _connect()
    except ForwardMediaVerificationHold:
        raise
    except Exception as exc:
        raise ForwardMediaVerificationHold(
            'visual index attester database unavailable') from exc
    tenant = tenant_key
    try:
        with conn.cursor() as cur:
            cur.execute('select current_user')
            if cur.fetchone() != (ROLE,):
                raise ForwardMediaVerificationHold('trusted attester role mismatch')
            snapshot, receipts = _snapshot_and_receipts(
                cur, row_id, expected_revision, lineage_id)
        urls = [snapshot.get('source_url'), snapshot.get('image_url'),
                snapshot.get('thumbnail_url') or snapshot.get('image_url')]
        tenant = snapshot.get('tenant_id')
        # End the read-only transaction BEFORE any object fetch (including
        # negative-evidence reads): no DB lock or snapshot is held across
        # network work.
        conn.rollback()
        # Missing/read-failed roles, caller contradictions and infrastructure
        # failures establish a hold, not globally bad media. Never substitute
        # another readable role's hash or blacklist otherwise valid bytes.
        # Contradictory caller-supplied identity can never attest the snapshot.
        if ((tenant_key is not None and tenant_key != snapshot.get('tenant_id'))
                or (gym_key is not None and gym_key != snapshot.get('gym_id'))
                or (content_date is not None
                    and str(content_date) != str(snapshot.get('post_date')))):
            raise ForwardMediaVerificationHold('visual index evidence contradicts persisted snapshot')
        # All three roles are required; a missing object is incomplete evidence.
        if any(not isinstance(url, str) or not url for url in urls):
            raise ForwardMediaVerificationHold('visual index requires original, delivered and thumbnail objects')
        observations = []
        actual_bytes = {}
        # Fetch each distinct URL exactly once so aliased roles (a NULL
        # thumbnail_url reuses image_url) observe identical bytes; the final
        # reread below then validates the same bytes every role attested.
        def fetch_once(url):
            if url not in actual_bytes:
                try:
                    actual_bytes[url] = _read(url, read_bytes)
                except ForwardMediaVerificationHold as exc:
                    raise ForwardMediaVerificationHold('visual index object evidence unavailable') from exc
            return actual_bytes[url]
        for role, url, receipt in zip(ROLES, urls, receipts):
            data = fetch_once(url)
            sha = hashlib.sha256(data).hexdigest()
            phash = phash_v1(data)
            if phash is None:
                if _proven_non_image(data):
                    _record_negative(conn, tenant, 'undecodable media bytes', lineage_id=lineage_id, source_sha256=sha)
                    raise ForwardMediaVerificationHold('visual index media bytes are undecodable')
                raise ForwardMediaVerificationHold('visual index media fingerprint unavailable')
            observations.append((role, url, str(uuid.UUID(str(receipt))), sha,
                                 hashlib.md5(data).hexdigest(), len(data), phash))
        for url, observed in actual_bytes.items():
            if _read(url, read_bytes) != observed:
                raise ForwardMediaVerificationHold('observed media object changed bytes')
        with conn.cursor() as cur:
            ids = {}
            for role, url, receipt, sha, md5, length, phash in observations:
                cur.execute(_ATTEST_INSERT,
                            (tenant, url, role, sha, md5, length,
                             PHASH_VERSION, phash, row_revision, lineage_id, receipt))
                ids[role] = str(cur.fetchone()[0])
        conn.commit()
        return {'attestation_ids': ids, 'row_revision': row_revision, 'tenant_key': tenant}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if isinstance(exc, ForwardMediaVerificationHold):
            raise
        raise ForwardMediaVerificationHold('visual index attestation transaction failed') from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _observe_role(conn, tenant, lineage_id, row_revision, role, url, receipt, read_bytes, cache):
    """Fetch actual bytes for one role and build its attestation tuple.

    ``cache`` deduplicates aliased URLs so aliased roles observe identical
    bytes; the caller rereads every fetched URL before committing."""
    if url not in cache:
        try:
            cache[url] = _read(url, read_bytes)
        except ForwardMediaVerificationHold as exc:
            raise ForwardMediaVerificationHold('visual index object evidence unavailable') from exc
    data = cache[url]
    sha = hashlib.sha256(data).hexdigest()
    phash = phash_v1(data)
    if phash is None:
        if _proven_non_image(data):
            _record_negative(conn, tenant, 'undecodable media bytes', lineage_id=lineage_id, source_sha256=sha)
            raise ForwardMediaVerificationHold('visual index media bytes are undecodable')
        raise ForwardMediaVerificationHold('visual index media fingerprint unavailable')
    return (role, url, str(uuid.UUID(str(receipt))), sha,
            hashlib.md5(data).hexdigest(), len(data), phash)


def recover(calendar_row_id, expected_revision, lineage_receipt_id, *,
            tenant_key=None, connection_factory=None, read_bytes=None):
    """Resume the MISSING visual roles after a crash, reusing the same lineage.

    Recovery contract for the documented gap (attester discovery drops a row
    once lineage exists): the caller supplies the exact persisted lineage
    identity; ``fixer_forward_visual_receipts_20261008`` revalidates it against
    the current revision, existing role attestations under that SAME lineage
    are reused verbatim, and only missing roles are fetched and appended in one
    transaction. A second lineage is never created, nothing is claimed,
    approved or published, and an existing attestation whose URL contradicts
    the current persisted binding holds the row. Returns the complete role ->
    attestation id map with ``appended`` naming the roles this call wrote."""
    row_id = _uuid(calendar_row_id)
    lineage_id = _uuid(lineage_receipt_id)
    row_revision = row_revision_from(expected_revision)
    try:
        conn = connection_factory() if connection_factory else _connect()
    except ForwardMediaVerificationHold:
        raise
    except Exception as exc:
        raise ForwardMediaVerificationHold(
            'visual index attester database unavailable') from exc
    try:
        with conn.cursor() as cur:
            cur.execute('select current_user')
            if cur.fetchone() != (ROLE,):
                raise ForwardMediaVerificationHold('trusted attester role mismatch')
            snapshot, receipts = _snapshot_and_receipts(
                cur, row_id, expected_revision, lineage_id)
            tenant = snapshot.get('tenant_id')
            cur.execute(_EXISTING_ROLES_SELECT, (lineage_id, row_revision, tenant))
            existing_rows = cur.fetchall()
        urls = dict(zip(ROLES, (snapshot.get('source_url'), snapshot.get('image_url'),
                                snapshot.get('thumbnail_url') or snapshot.get('image_url'))))
        receipt_by_role = dict(zip(ROLES, receipts))
        if tenant_key is not None and tenant_key != tenant:
            raise ForwardMediaVerificationHold('visual index evidence contradicts persisted snapshot')
        if any(not isinstance(url, str) or not url for url in urls.values()):
            raise ForwardMediaVerificationHold('visual index requires original, delivered and thumbnail objects')
        existing = {}
        for item in existing_rows or []:
            if not isinstance(item, (tuple, list)) or len(item) != 3:
                raise ForwardMediaVerificationHold('persisted visual evidence shape invalid')
            role, attestation_id, media_url = item
            if role not in ROLES or role in existing:
                raise ForwardMediaVerificationHold('persisted visual evidence ambiguous')
            if media_url != urls[role]:
                raise ForwardMediaVerificationHold(
                    'existing visual attestation contradicts persisted media binding')
            existing[role] = _uuid(attestation_id)
        # End the read-only transaction BEFORE any object fetch.
        conn.rollback()
        missing = [role for role in ROLES if role not in existing]
        if not missing:
            return {'attestation_ids': existing, 'row_revision': row_revision,
                    'tenant_key': tenant, 'appended': [], 'recovered': False}
        cache = {}
        observations = [_observe_role(conn, tenant, lineage_id, row_revision, role,
                                      urls[role], receipt_by_role[role], read_bytes, cache)
                        for role in missing]
        for url, observed in cache.items():
            if _read(url, read_bytes) != observed:
                raise ForwardMediaVerificationHold('observed media object changed bytes')
        with conn.cursor() as cur:
            appended = {}
            for role, url, receipt, sha, md5, length, phash in observations:
                cur.execute(_ATTEST_INSERT,
                            (tenant, url, role, sha, md5, length,
                             PHASH_VERSION, phash, row_revision, lineage_id, receipt))
                appended[role] = str(cur.fetchone()[0])
        conn.commit()
        return {'attestation_ids': {**existing, **appended}, 'row_revision': row_revision,
                'tenant_key': tenant, 'appended': missing, 'recovered': True}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if isinstance(exc, ForwardMediaVerificationHold):
            raise
        raise ForwardMediaVerificationHold('visual index attestation transaction failed') from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _reservation_proof_for_claim(store, calendar_row_id, source_sha256, expected_revision, attestation_ids):
    """Consult the EXACT active forward reservation for this row + source bytes
    (DRAFT, 2026-10-08; only consulted when the reservation gate is armed or
    unreadable -- fail closed either way).

    Returns the strictly parsed proof dict. Any hold, mismatch or failure
    raises ForwardMediaVerificationHold: a publication that cannot prove its
    reservation never proceeds while the gate is anything but cleanly OFF."""
    from .portal_calendar_store import forward_reservation_flag
    flag = forward_reservation_flag()
    if flag is False:
        return None
    if flag is None:
        raise ForwardMediaVerificationHold(
            'forward reservation gate unreadable; claim held')
    if store is None:
        raise ForwardMediaVerificationHold('reservation proof store required')
    if not isinstance(source_sha256, str) or not source_sha256:
        # The publisher integration that passes the exact source bytes is a
        # separate handoff; without them the exact reservation cannot resolve.
        raise ForwardMediaVerificationHold(
            'exact source bytes required to consult the reservation proof')
    try:
        response = store._client().post(
            store._rest('rpc/forward_reservation_proof_20261008'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json={'p_calendar_row_id': _uuid(calendar_row_id),
                  'p_source_sha256': source_sha256}, timeout=30)
        proof = response.json()
    except ForwardMediaVerificationHold:
        raise
    except Exception as exc:
        raise ForwardMediaVerificationHold(
            'persisted reservation proof unavailable') from exc
    if (not 200 <= response.status_code < 300 or not isinstance(proof, dict)
            or proof.get('source_sha256') != source_sha256
            or not isinstance(proof.get('reservation_id'), str)
            or not proof['reservation_id']
            or not isinstance(proof.get('tenant_id'), str)
            or not proof['tenant_id']
            or not isinstance(proof.get('row_revision'), str)
            or proof['row_revision'] != expected_revision
            or not isinstance(proof.get('attestation_ids'), list)
            or len(proof['attestation_ids']) != 3
            or set(proof['attestation_ids']) != set(attestation_ids)):

        raise ForwardMediaVerificationHold(
            'persisted reservation proof unavailable')
    return {'reservation_id': _uuid(proof['reservation_id']),
            'tenant_id': proof['tenant_id'],
            'post_date': str(proof.get('post_date') or ''),
            'logical_post_id': _uuid(proof.get('logical_post_id')),
            'row_revision': proof['row_revision'],
            'source_sha256': source_sha256,
            'attestation_ids': [_uuid(a) for a in proof['attestation_ids']]}


def before_claim(calendar_row_id, expected_revision, lineage_receipt_id, *, store=None,
                 claim_token=None, source_sha256=None):
    """Publisher reads persisted proof; no attester credentials or object I/O.

    The isolated trusted lane calls ``attest`` ahead of publication. Missing
    proof holds; final SQL atomically rechecks the exact proof and occupancy.
    When the forward reservation draft gate is armed, the exact active
    reservation for this row + source bytes must also resolve
    (``forward_reservation_proof_20261008``); anything less holds."""
    from .portal_calendar_store import forward_reservation_flag, _SHA256_RE
    reservation_flag = forward_reservation_flag()
    if reservation_flag is not False and not enabled():
        raise ForwardMediaVerificationHold('forward reservation requires visual claim proof')
    if not enabled():
        return None
    if reservation_flag is None:
        raise ForwardMediaVerificationHold('forward reservation gate unreadable; claim held')
    if store is None or claim_token is None:
        raise ForwardMediaVerificationHold('persisted visual proof store required')
    arguments = {'p_calendar_row_id': _uuid(calendar_row_id),
                 'p_claim_token': _uuid(claim_token),
                 'p_evidence_id': _uuid(lineage_receipt_id),
                 'p_expected_revision': expected_revision}
    try:
        response = store._client().post(
            store._rest('rpc/fixer_forward_visual_proof_20261008'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json=arguments, timeout=30)
        proof = response.json()
        if not 200 <= response.status_code < 300 or not isinstance(proof, dict):
            raise ForwardMediaVerificationHold('persisted visual claim proof unavailable')
        ids = proof.get('attestation_ids')
        if not isinstance(ids, list) or len(ids) != 3 or len(set(ids)) != 3:
            raise ForwardMediaVerificationHold('persisted visual claim proof unavailable')
        result = {'attestation_ids': [_uuid(ident) for ident in ids]}
        if reservation_flag:
            # SQL derives this hash from the current row's original attestation;
            # the publisher must never invent bytes from a URL or planner stamp.
            observed_sha = proof.get('source_sha256')
            if (not isinstance(observed_sha, str) or not _SHA256_RE.fullmatch(observed_sha)
                    or (source_sha256 is not None and source_sha256 != observed_sha)):
                raise ForwardMediaVerificationHold('exact attested source bytes unavailable')
            source_sha256 = observed_sha
        reservation = _reservation_proof_for_claim(
            store, calendar_row_id, source_sha256, expected_revision, result['attestation_ids'])
        if reservation is not None:
            result['reservation'] = reservation
        return result
    except ForwardMediaVerificationHold:
        raise
    except Exception as exc:
        raise ForwardMediaVerificationHold('persisted visual claim proof unavailable') from exc
