"""Offline historical-photo corpus review evidence packet (read-only).

Builds an immutable evidence packet for INDEPENDENT human/auditor review of one
historical photo candidate against the complete fleet send corpus. This tool
never decides clearance, signs nothing, writes no database, contacts no service
and imports no agent modules. Input is an authoritative offline export:

{
  "cutover": {"cutover_id": str, "cutover_at": iso ts, "evidence_refs": [str]},
  "scope_manifest": {signed cutover scope manifest, see validate_scope_manifest},
  "candidate": {authenticated source/version block, see validate_candidate},
  "pages": [stable paginated send-corpus pages, cursor-chained, page-digested],
  "dispositions": [per-history_key independent review rulings]
}

Scope is proven against an independent, SIGNED cutover scope manifest
(Ed25519); the approved public keys are supplied by the caller at run time and
are NEVER embedded in this tool, the export, or the manifest. The manifest
binds the cutover identity, the query/source identity, the complete expected
fleet history-key set (cryptographic digest over the sorted key list plus an
independently attested count), and per-tenant/per-route expected counts. The
exported rows are reconciled exactly against the manifest: key set, count,
tenant/route coverage and cutover must all match. A self-reported null-to-null
page chain alone NEVER proves completeness — pagination evidence without the
signed manifest reconciliation holds.

Pagination must additionally prove un-truncated transport: pages are
cursor-chained from a null start cursor to a null end cursor, each page
carries its own sha256 digest over its rows, and any truncation flag
(2,500-row caps, 4 MiB caps, row_limit or byte_limit applied) is a hard
failure — never silently absorbed. Missing or ambiguous candidate bytes hold
the candidate. Every corpus visual gets one of exact_match / visual_match /
reviewed_nonmatch / unresolved; unresolved always carries an explicit reason.
Every non-unresolved disposition binds, per object, the exact inspected byte
SHA (source, delivered, thumbnail, every snapshot, every frame) to an
immutable evidence ref. Video/carousel visuals need COMPLETE frame coverage:
unique frame indices exactly 0..frames_total-1 attested by a frames coverage
ref, or an explicit frames_hold reason — one frame of a multi-frame video is
never sufficient. The packet's decision is ALWAYS "review_packet_only": no
automatic positive decision is possible here.
"""
import argparse
from datetime import date, datetime
import hashlib
import json
import os
import re
import uuid

SCHEMA_VERSION = 2
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_HTTPS = re.compile(r'https://[^\s]+\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_HEX_SIG = re.compile(r'[0-9a-f]{128}\Z')
_MEDIA_KINDS = frozenset({'still_photo', 'video', 'carousel', 'other'})
_DISPOSITIONS = frozenset(
    {'exact_match', 'visual_match', 'reviewed_nonmatch', 'unresolved'})
_SEND_STATUSES = frozenset(
    {'draft', 'pending', 'approved', 'queued', 'publishing', 'published',
     'failed', 'canceled'})
_SCOPE_MANIFEST_KIND = 'historical_photo_cutover_scope'


class PacketHold(RuntimeError):
    """Static reasons only; never expose URLs, bytes or export contents."""


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise PacketHold('packet_shape_invalid') from None


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value).encode()).hexdigest()


def _sha(value, field):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise PacketHold('packet_shape_invalid:' + field)
    return value


def _url(value, field):
    if not isinstance(value, str) or not _HTTPS.fullmatch(value):
        raise PacketHold('packet_shape_invalid:' + field)
    return value


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PacketHold('packet_shape_invalid:' + field)
    return value


def _ts(value, field):
    if not isinstance(value, str):
        raise PacketHold('packet_shape_invalid:' + field)
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise PacketHold('packet_shape_invalid:' + field)
    return value


def _date(value, field):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise PacketHold('packet_shape_invalid:' + field)
    return value


def _object_ref(value, field):
    """One reachable object: https URL plus authenticated byte sha/length."""
    if not isinstance(value, dict) or set(value) != {'url', 'sha256', 'length'}:
        raise PacketHold('packet_shape_invalid:' + field)
    _url(value['url'], field)
    _sha(value['sha256'], field)
    if type(value['length']) is not int or not 0 < value['length'] <= 134217728:
        raise PacketHold('packet_shape_invalid:' + field)
    return {'url': value['url'], 'sha256': value['sha256'], 'length': value['length']}


def validate_scope_manifest(manifest, approved_keys):
    """Independent, SIGNED cutover scope manifest. approved_keys maps
    key_id -> Ed25519 public key hex and is supplied by the caller at run
    time — never embedded here, in the export, or in the manifest. The
    payload binds the cutover identity, the query/source identity, the
    complete expected history-key set digest plus attested count, and
    per-tenant/route expected counts. Anything missing, unsigned, altered or
    signed by an unapproved key holds."""
    if approved_keys is None:
        raise PacketHold('scope_manifest_key_unapproved')
    if not isinstance(approved_keys, dict) or not approved_keys:
        raise PacketHold('scope_manifest_key_unapproved')
    if manifest is None:
        raise PacketHold('scope_manifest_missing')
    if not isinstance(manifest, dict) or set(manifest) != {'payload', 'key_id',
                                                           'signature_hex'}:
        raise PacketHold('scope_manifest_shape_invalid')
    _text(manifest['key_id'], 'scope_manifest.key_id')
    if not isinstance(manifest['signature_hex'], str) \
            or not _HEX_SIG.fullmatch(manifest['signature_hex']):
        raise PacketHold('scope_manifest_unsigned')
    key_hex = approved_keys.get(manifest['key_id'])
    if not isinstance(key_hex, str) or not _HEX64.fullmatch(key_hex):
        raise PacketHold('scope_manifest_key_unapproved')
    payload = manifest['payload']
    required = {'manifest_kind', 'cutover_id', 'cutover_at', 'generated_at',
                'source_query_id', 'expected_history_keys_digest',
                'expected_history_keys_count', 'tenant_route_counts'}
    if not isinstance(payload, dict) or set(payload) != required:
        raise PacketHold('scope_manifest_shape_invalid')
    if payload['manifest_kind'] != _SCOPE_MANIFEST_KIND:
        raise PacketHold('scope_manifest_shape_invalid')
    _text(payload['cutover_id'], 'scope_manifest.cutover_id')
    _ts(payload['cutover_at'], 'scope_manifest.cutover_at')
    _ts(payload['generated_at'], 'scope_manifest.generated_at')
    _text(payload['source_query_id'], 'scope_manifest.source_query_id')
    _sha(payload['expected_history_keys_digest'],
         'scope_manifest.expected_history_keys_digest')
    if (type(payload['expected_history_keys_count']) is not int
            or payload['expected_history_keys_count'] < 0):
        raise PacketHold('scope_manifest_shape_invalid')
    trc = payload['tenant_route_counts']
    if not isinstance(trc, list):
        raise PacketHold('scope_manifest_shape_invalid')
    seen_pairs = set()
    for entry in trc:
        if (not isinstance(entry, dict)
                or set(entry) != {'tenant_id', 'send_route', 'expected_count'}):
            raise PacketHold('scope_manifest_shape_invalid')
        _text(entry['tenant_id'], 'scope_manifest.tenant_route_counts')
        _text(entry['send_route'], 'scope_manifest.tenant_route_counts')
        if type(entry['expected_count']) is not int or entry['expected_count'] < 0:
            raise PacketHold('scope_manifest_shape_invalid')
        pair = (entry['tenant_id'], entry['send_route'])
        if pair in seen_pairs:
            raise PacketHold('scope_manifest_shape_invalid')
        seen_pairs.add(pair)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
    except ImportError:
        raise PacketHold('scope_manifest_unsigned') from None
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex)).verify(
            bytes.fromhex(manifest['signature_hex']),
            canonical(payload).encode())
    except (InvalidSignature, ValueError):
        raise PacketHold('scope_manifest_altered') from None
    return {'cutover_id': payload['cutover_id'],
            'cutover_at': payload['cutover_at'],
            'generated_at': payload['generated_at'],
            'source_query_id': payload['source_query_id'],
            'expected_history_keys_digest':
                payload['expected_history_keys_digest'],
            'expected_history_keys_count': payload['expected_history_keys_count'],
            'tenant_route_counts': [dict(e) for e in trc],
            'key_id': manifest['key_id'],
            'manifest_digest': digest(payload)}


def reconcile_scope(manifest, cutover, visuals):
    """Exact reconciliation of the exported corpus against the signed scope
    manifest. A self-reported page chain is never enough: key set digest,
    attested count, per-tenant/route counts and the cutover identity must all
    match the signed manifest or the build holds."""
    if (manifest['cutover_id'] != cutover['cutover_id']
            or manifest['cutover_at'] != cutover['cutover_at']):
        raise PacketHold('scope_manifest_stale')
    keys = sorted(v['history_key'] for v in visuals)
    if len(keys) != manifest['expected_history_keys_count']:
        raise PacketHold('scope_manifest_count_mismatch')
    if digest(keys) != manifest['expected_history_keys_digest']:
        raise PacketHold('scope_manifest_key_set_mismatch')
    actual = {}
    for v in visuals:
        pair = (v['tenant_id'], v['send_route'])
        actual[pair] = actual.get(pair, 0) + 1
    expected = {(e['tenant_id'], e['send_route']): e['expected_count']
                for e in manifest['tenant_route_counts']}
    if actual != expected:
        raise PacketHold('scope_manifest_tenant_route_mismatch')


def validate_candidate(candidate):
    """Authenticated source/version block for ONE candidate. Holds on missing
    or ambiguous bytes: source/render/thumbnail refs are all-or-nothing per
    object, and any declared hold_reasons short-circuit packet status."""
    required = {'calendar_row_id', 'tenant_id', 'group_key', 'post_date',
                'source_asset_id', 'source_version_id', 'source_receipt_ref',
                'source', 'delivered', 'evidence_refs'}
    allowed = required | {'thumbnail', 'hold_reasons'}
    if not isinstance(candidate, dict) or not required <= set(candidate) or not set(candidate) <= allowed:
        raise PacketHold('candidate_shape_invalid')
    if str(uuid.UUID(candidate['calendar_row_id'])) != candidate['calendar_row_id']:
        raise PacketHold('candidate_shape_invalid')
    for k in ('tenant_id', 'group_key', 'source_asset_id', 'source_version_id',
              'source_receipt_ref'):
        _text(candidate[k], 'candidate.' + k)
    _date(candidate['post_date'], 'candidate.post_date')
    out = {k: candidate[k] for k in ('calendar_row_id', 'tenant_id', 'group_key',
                                     'post_date', 'source_asset_id',
                                     'source_version_id', 'source_receipt_ref')}
    out['source'] = _object_ref(candidate['source'], 'candidate.source')
    out['delivered'] = _object_ref(candidate['delivered'], 'candidate.delivered')
    if 'thumbnail' in candidate:
        if candidate['thumbnail'] is not None:
            out['thumbnail'] = _object_ref(candidate['thumbnail'], 'candidate.thumbnail')
        else:
            out['thumbnail'] = None
    refs = candidate['evidence_refs']
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(r, str) or not r.strip() for r in refs)):
        raise PacketHold('candidate_shape_invalid')
    out['evidence_refs'] = list(refs)
    holds = candidate.get('hold_reasons', [])
    if not isinstance(holds, list) or any(not isinstance(h, str) or not h.strip() for h in holds):
        raise PacketHold('candidate_shape_invalid')
    out['hold_reasons'] = sorted(set(holds))
    return out


def _validate_frame(frame, field):
    if not isinstance(frame, dict) or set(frame) != {'frame_index', 'sha256', 'length'}:
        raise PacketHold('packet_shape_invalid:' + field)
    if type(frame['frame_index']) is not int or frame['frame_index'] < 0:
        raise PacketHold('packet_shape_invalid:' + field)
    _sha(frame['sha256'], field)
    if type(frame['length']) is not int or not 0 < frame['length'] <= 134217728:
        raise PacketHold('packet_shape_invalid:' + field)
    return dict(frame)


def validate_visual(row):
    """One corpus visual with stable history_key and complete send-route
    reconciliation fields. Reachability is explicit: an object is either a
    full {url, sha256, length} ref or {"reachable": false, "url": ...}; never
    silently dropped. Video/carousel visuals need COMPLETE frame coverage:
    frames_total attested by frames_coverage_ref, with unique sorted frame
    indices exactly 0..frames_total-1 — a single frame of a multi-frame item
    fails. The only alternative is an explicit frames_hold_reason."""
    required = {'history_key', 'tenant_id', 'send_route', 'status', 'published_at',
                'late_post_id', 'provider_receipt_refs', 'claim_ref',
                'reservation_ref', 'media_kind', 'source', 'delivered',
                'thumbnail', 'snapshots'}
    allowed = required | {'frames', 'frames_total', 'frames_coverage_ref',
                          'frames_hold_reason'}
    if not isinstance(row, dict) or not required <= set(row) or not set(row) <= allowed:
        raise PacketHold('visual_shape_invalid')
    _text(row['history_key'], 'visual.history_key')
    _text(row['tenant_id'], 'visual.tenant_id')
    _text(row['send_route'], 'visual.send_route')
    if row['status'] not in _SEND_STATUSES:
        raise PacketHold('visual_shape_invalid:status')
    if row['published_at'] is not None:
        _ts(row['published_at'], 'visual.published_at')
    if row['late_post_id'] is not None:
        _text(row['late_post_id'], 'visual.late_post_id')
    receipts = row['provider_receipt_refs']
    if (not isinstance(receipts, list)
            or any(not isinstance(r, str) or not r.strip() for r in receipts)):
        raise PacketHold('visual_shape_invalid:provider_receipt_refs')
    for k in ('claim_ref', 'reservation_ref'):
        if row[k] is not None:
            _text(row[k], 'visual.' + k)
    if row['media_kind'] not in _MEDIA_KINDS:
        raise PacketHold('visual_shape_invalid:media_kind')
    out = {k: row[k] for k in ('history_key', 'tenant_id', 'send_route', 'status',
                               'published_at', 'late_post_id',
                               'provider_receipt_refs', 'claim_ref',
                               'reservation_ref', 'media_kind')}

    def obj(value, field):
        if isinstance(value, dict) and set(value) == {'url', 'reachable'} and value['reachable'] is False:
            return {'url': _url(value['url'], field), 'reachable': False}
        return dict(_object_ref(value, field), reachable=True)

    out['source'] = obj(row['source'], 'visual.source')
    out['delivered'] = obj(row['delivered'], 'visual.delivered')
    out['thumbnail'] = None if row['thumbnail'] is None else obj(row['thumbnail'], 'visual.thumbnail')
    snaps = row['snapshots']
    if not isinstance(snaps, list) or len(snaps) > 64:
        raise PacketHold('visual_shape_invalid:snapshots')
    out['snapshots'] = [obj(s, 'visual.snapshots') for s in snaps]
    if row['media_kind'] in ('video', 'carousel'):
        frames = row.get('frames')
        hold = row.get('frames_hold_reason')
        if frames is None:
            if not isinstance(hold, str) or not hold.strip():
                raise PacketHold('visual_frames_coverage_required')
            out['frames'] = None
            out['frames_total'] = None
            out['frames_coverage_ref'] = None
            out['frames_hold_reason'] = hold
        else:
            if not isinstance(frames, list) or not frames or len(frames) > 256:
                raise PacketHold('visual_frames_coverage_required')
            if (type(row.get('frames_total')) is not int
                    or row['frames_total'] < 1):
                raise PacketHold('visual_frames_coverage_required')
            if not isinstance(row.get('frames_coverage_ref'), str) \
                    or not row['frames_coverage_ref'].strip():
                raise PacketHold('visual_frames_coverage_required')
            out['frames'] = [_validate_frame(f, 'visual.frames') for f in frames]
            indices = [f['frame_index'] for f in out['frames']]
            if indices != sorted(indices) or len(set(indices)) != len(indices):
                raise PacketHold('visual_frames_coverage_required')
            if indices != list(range(row['frames_total'])):
                raise PacketHold('visual_frames_incomplete')
            out['frames_total'] = row['frames_total']
            out['frames_coverage_ref'] = row['frames_coverage_ref']
            out['frames_hold_reason'] = None
    return out


def validate_pages(pages):
    """Stable pagination ledger proving un-truncated transport. No truncation
    at any row or byte budget is acceptable: any limit/truncation flag fails.
    NOTE: a self-reported null-to-null page chain alone never proves
    completeness — see reconcile_scope for the signed-manifest gate."""
    if not isinstance(pages, list) or not pages:
        raise PacketHold('pagination_incomplete')
    cursor = None
    rows, ledger = [], []
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise PacketHold('pagination_incomplete')
        required = {'cursor', 'next_cursor', 'row_count', 'page_digest', 'rows'}
        if not required <= set(page):
            raise PacketHold('pagination_incomplete')
        for flag in ('truncated', 'row_limit_applied', 'byte_limit_applied',
                     'limit_applied', 'has_more'):
            if page.get(flag):
                raise PacketHold('pagination_truncated:' + flag)
        if page['cursor'] != cursor:
            raise PacketHold('pagination_cursor_chain_broken')
        cursor = page['next_cursor']
        if cursor is not None:
            _text(cursor, 'page.next_cursor')
        prows = page['rows']
        if (not isinstance(prows, list) or not prows
                or page['row_count'] != len(prows)):
            raise PacketHold('pagination_row_count_mismatch')
        if page['page_digest'] != digest(prows):
            raise PacketHold('pagination_page_digest_mismatch')
        rows.extend(prows)
        ledger.append({'page_index': index, 'cursor': page['cursor'],
                       'next_cursor': page['next_cursor'],
                       'row_count': len(prows), 'page_digest': page['page_digest']})
    if cursor is not None:
        raise PacketHold('pagination_incomplete')
    return rows, ledger


def validate_dispositions(dispositions, history_keys):
    if not isinstance(dispositions, list):
        raise PacketHold('dispositions_incomplete')
    seen = {}
    for d in dispositions:
        if (not isinstance(d, dict) or set(d) != {'history_key', 'disposition',
                'unresolved_reason', 'review_evidence_ref',
                'inspected_objects'}):
            raise PacketHold('dispositions_incomplete')
        key = d['history_key']
        if key in seen:
            raise PacketHold('dispositions_incomplete')
        if d['disposition'] not in _DISPOSITIONS:
            raise PacketHold('dispositions_incomplete')
        if d['disposition'] == 'unresolved':
            if (not isinstance(d['unresolved_reason'], str) or not d['unresolved_reason'].strip()
                    or d['review_evidence_ref'] is not None):
                raise PacketHold('dispositions_incomplete')
            if d['inspected_objects'] is not None:
                raise PacketHold('dispositions_incomplete')
        else:
            if d['unresolved_reason'] is not None:
                raise PacketHold('dispositions_incomplete')
            if (not isinstance(d['review_evidence_ref'], str)
                    or not d['review_evidence_ref'].strip()):
                raise PacketHold('dispositions_incomplete')
            inspected = d['inspected_objects']
            if not isinstance(inspected, list) or not inspected:
                raise PacketHold('disposition_evidence_binding_invalid')
            for entry in inspected:
                if (not isinstance(entry, dict)
                        or set(entry) != {'sha256', 'evidence_ref'}):
                    raise PacketHold('disposition_evidence_binding_invalid')
                _sha(entry['sha256'], 'disposition.inspected_objects')
                if (not isinstance(entry['evidence_ref'], str)
                        or not entry['evidence_ref'].strip()):
                    raise PacketHold('disposition_evidence_binding_invalid')
        seen[key] = d
    if set(seen) != set(history_keys):
        raise PacketHold('dispositions_incomplete')
    return seen


def _inspected_sha_set(visual):
    """Every reachable inspected byte SHA of one visual: source, delivered,
    thumbnail, every snapshot and every frame. Unreachable objects carry no
    authenticated bytes and are excluded (they hold the packet separately)."""
    shas = set()
    objects = [visual['source'], visual['delivered']] + list(visual['snapshots'])
    if visual['thumbnail'] is not None:
        objects.append(visual['thumbnail'])
    for o in objects:
        if o.get('reachable') is True:
            shas.add(o['sha256'])
    for f in visual.get('frames') or []:
        shas.add(f['sha256'])
    return shas


def bind_disposition_evidence(disposition, visual):
    """A non-unresolved disposition must bind EVERY inspected object and frame
    byte SHA of its visual to an immutable evidence ref — exact set match, no
    omissions, no extras."""
    if disposition['disposition'] == 'unresolved':
        return
    bound = {e['sha256'] for e in disposition['inspected_objects']}
    if len(bound) != len(disposition['inspected_objects']):
        raise PacketHold('disposition_evidence_binding_invalid')
    if bound != _inspected_sha_set(visual):
        raise PacketHold('disposition_evidence_binding_invalid')


def build_packet(export, approved_keys=None):
    """Validate an authoritative offline export and produce the review packet.

    approved_keys maps key_id -> Ed25519 public key hex and is REQUIRED; it is
    supplied by the caller at run time and never embedded. decision is always
    'review_packet_only'. packet_status is 'hold' whenever the candidate has
    hold reasons or any visual is unresolved / byte-unreachable / frame-held;
    otherwise 'complete_for_independent_review'. Neither value is a clearance
    and neither signs anything.
    """
    if not isinstance(export, dict) or not {'cutover', 'candidate', 'pages',
                                            'dispositions'} <= set(export):
        raise PacketHold('packet_shape_invalid')
    cutover = export['cutover']
    if (not isinstance(cutover, dict)
            or not {'cutover_id', 'cutover_at', 'evidence_refs'} <= set(cutover)):
        raise PacketHold('cutover_shape_invalid')
    _text(cutover['cutover_id'], 'cutover.cutover_id')
    _ts(cutover['cutover_at'], 'cutover.cutover_at')
    if (not isinstance(cutover['evidence_refs'], list)
            or any(not isinstance(r, str) or not r.strip()
                   for r in cutover['evidence_refs'])):
        raise PacketHold('cutover_shape_invalid')

    candidate = validate_candidate(export['candidate'])
    rows, ledger = validate_pages(export['pages'])
    visuals = [validate_visual(r) for r in rows]
    keys = [v['history_key'] for v in visuals]
    if len(set(keys)) != len(keys):
        raise PacketHold('duplicate_history_key')
    manifest = validate_scope_manifest(export.get('scope_manifest'),
                                       approved_keys)
    reconcile_scope(manifest, cutover, visuals)
    dispositions = validate_dispositions(export['dispositions'], keys)
    for v in visuals:
        bind_disposition_evidence(dispositions[v['history_key']], v)

    def reachable(obj):
        return obj is not None and obj.get('reachable') is True

    packet_visuals, by_disposition, by_kind = [], {}, {}
    unreachable_bytes = frames_held = 0
    for v in visuals:
        d = dispositions[v['history_key']]
        objects = [v['source'], v['delivered']] + list(v['snapshots'])
        if v['thumbnail'] is not None:
            objects.append(v['thumbnail'])
        unresolved_reasons = []
        if any(not reachable(o) for o in objects):
            unreachable_bytes += 1
            unresolved_reasons.append('object_bytes_unreachable')
        if v['media_kind'] in ('video', 'carousel') and v['frames'] is None:
            frames_held += 1
            unresolved_reasons.append('frames_hold:' + v['frames_hold_reason'])
        if d['disposition'] == 'unresolved':
            unresolved_reasons.append(d['unresolved_reason'])
        by_disposition[d['disposition']] = by_disposition.get(d['disposition'], 0) + 1
        by_kind[v['media_kind']] = by_kind.get(v['media_kind'], 0) + 1
        packet_visuals.append({
            **v, 'disposition': d['disposition'],
            'review_evidence_ref': d['review_evidence_ref'],
            'inspected_objects': d['inspected_objects'],
            'unresolved_reasons': sorted(set(unresolved_reasons)) or None,
        })

    corpus_digest = digest(packet_visuals)
    cutover_digest = digest({'cutover_id': cutover['cutover_id'],
                             'cutover_at': cutover['cutover_at'],
                             'candidate': candidate,
                             'corpus_digest': corpus_digest,
                             'scope_manifest_digest':
                                 manifest['manifest_digest']})
    unresolved_total = sum(1 for v in packet_visuals if v['unresolved_reasons'])
    hold = bool(candidate['hold_reasons']) or unresolved_total > 0
    evidence_refs = sorted(set(cutover['evidence_refs']) | set(candidate['evidence_refs']))
    packet = {
        'schema_version': SCHEMA_VERSION,
        'packet_kind': 'historical_photo_review_evidence',
        'decision': 'review_packet_only',
        'clearance': False,
        'no_automatic_positive_decision': True,
        'packet_status': 'hold' if hold else 'complete_for_independent_review',
        'candidate': candidate,
        'scope_manifest': manifest,
        'coverage': {
            'total_visuals': len(packet_visuals),
            'by_disposition': by_disposition,
            'by_media_kind': by_kind,
            'unresolved_visuals': unresolved_total,
            'byte_unreachable_visuals': unreachable_bytes,
            'frames_held_visuals': frames_held,
            'pages': len(ledger),
        },
        'pagination_ledger': ledger,
        'visuals': packet_visuals,
        'corpus_digest': corpus_digest,
        'cutover_digest': cutover_digest,
        'evidence_refs': evidence_refs,
    }
    packet['packet_digest'] = digest(packet)
    return packet


def _parse_approved_keys(pairs):
    keys = {}
    for pair in pairs or []:
        if ':' not in pair:
            raise PacketHold('scope_manifest_key_unapproved')
        key_id, _, key_hex = pair.partition(':')
        keys[key_id.strip()] = key_hex.strip()
    return keys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('export', help='authoritative offline export JSON')
    parser.add_argument('-o', '--out', required=True, help='packet output path (0600)')
    parser.add_argument('--approved-key', action='append', required=True,
                        metavar='KEY_ID:ED25519_PUB_HEX',
                        help='approved scope-manifest signing key (repeatable); '
                             'supplied at run time, never embedded')
    args = parser.parse_args(argv)
    with open(args.export, encoding='utf-8') as fh:
        export = json.load(fh)
    packet = build_packet(export, approved_keys=_parse_approved_keys(args.approved_key))
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(packet, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write('\n')
    print(json.dumps({'packet_status': packet['packet_status'],
                      'packet_digest': packet['packet_digest'],
                      'coverage': packet['coverage']}, sort_keys=True))


if __name__ == '__main__':
    main()
