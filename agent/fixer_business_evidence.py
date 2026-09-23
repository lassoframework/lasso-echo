"""
fixer_business_evidence.py -- independent business postcondition evidence.

A bounded, tenant-scoped, READ-ONLY observer for the FIXER release loop and
keyed media swaps. A healthy deploy proves the code is live, not that the
owner's symptom is gone; the customer
closure gate (slack_convo/outbox._verified_fix_notice) therefore requires a
business_postcondition record with source 'independent_business_check', verified and
symptom_resolved True, a check id, a short evidence identifier, and the exact
request_key and merged_sha of the release being closed. This module produces those
records: every observation durably binds the request identity (request_key), the
tenant (gym_key) and the release identity (merged_sha) to an outcome, with a
captured_at timestamp. Nothing here writes to that consumer; the field names simply
line up with the outbox contract. Direct media swaps use a separate receipt-bound
entry point and do not claim a code release SHA.

Hard rules (fail closed, all of them):
  - Only checks explicitly registered in CHECKS can run. An unknown check id returns
    an UNVERIFIED record before any identity is inspected and nothing dynamic is
    ever executed: no eval, no import by string, no caller-supplied callables as
    checks. Checks are fixed module functions.
  - No free-text proof. Callers supply expectations (params: a row id and the
    status it should now hold, a minimum grade, a folder id), never proof. There is
    no input field through which prose could mark anything verified; `verified` is
    True ONLY when the check's own bounded readback independently matches the
    declared expectation.
  - No writes, no network, no env reads, no global state beyond the frozen
    registry. Every storage read goes through the injected deps['read'] reader,
    always with an explicit limit; a missing, failing, or out-of-bounds reader
    yields UNKNOWN, never an exception-as-proof and never a guess.
  - Tenant scope is re-asserted on every row read back. A row stamped for another
    gym makes the observation UNKNOWN (a reader that crosses tenants cannot be
    trusted in either direction), never evidence for or against the tenant asked.

Three outcomes, distinguishable on every record: VERIFIED (readback matched),
UNVERIFIED (readback succeeded and contradicts the expectation, or the request was
refused before any read), UNKNOWN (evidence unavailable or untrustworthy).

The module carries no feature flag by design: it performs no I/O of its own and is
inert without an injected reader, the same posture as agent/fixer_evidence.py.
Whatever transport mounts it owns the auth gate, as fixer_ops does for evidence.
"""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from . import gym_media_selector

VERIFIED = 'verified'
UNVERIFIED = 'unverified'
UNKNOWN = 'unknown'

SOURCE = 'independent_business_check'
SCHEMA_VERSION = 1

_GYM_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{2,79}\Z')
_PORTAL_GYM_ID = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
_REQUEST_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z')
# A business observation is only usable to close a deployed code fix.  Keep the
# deployment binding as a real Git object id, not a generic release label: a
# short ref, branch name, UUID, or prose-like token could otherwise be rebound
# to a later deployment while still passing the identity comparison.
_RELEASE_SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_ROW_ID = re.compile(r'[A-Za-z0-9_-]{6,80}\Z')
_STATUS = re.compile(r'[a-z_]{2,32}\Z')
_FOLDER_ID = re.compile(r'[A-Za-z0-9_-]{3,200}\Z')
_MAX_EVIDENCE = 160
_MAX_REASON = 80
_FORWARD_BOOK_MAX_AGE = timedelta(hours=24)
# media_source_sync_request_20260917.sql defines the source lifecycle as
# idle -> queued -> indexing -> ready (or failed).  Only the terminal success
# state is business proof that the source is usable; every other value is
# incomplete, failed, missing, or outside the known schema.
_HEALTHY_MEDIA_SYNC_STATUSES = frozenset({'ready'})
_MEDIA_PAGE_SIZE = 100
_MEDIA_MAX_ROWS = 1000
_MEDIA_ID = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
_RECEIPT_KEY = re.compile(r'[A-Za-z0-9_-]{8,128}\Z')
_HEX_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_MEDIA_FIELDS = ('id', 'gym_id', 'kind', 'eligible', 'excluded_by_coach',
                 'review_status', 'reviewed_by', 'reviewed_at', 'content_hash',
                 'review_content_hash', 'moderation_status', 'moderation_json',
                 'people_detected', 'consent_status', 'consent_member_ref',
                 'release_ref', 'consent_expires_at', 'last_used_at', 'used_count')


class CheckRefused(Exception):
    """The observation request itself is invalid -> UNVERIFIED (nothing was read)."""


class CheckUnavailable(Exception):
    """The readback could not be completed or trusted -> UNKNOWN (never a guess)."""


@dataclass(frozen=True)
class Observation:
    """What one registered check saw. available=False means the readback did not
    happen; matched is then meaningless and the runner records UNKNOWN. evidence is
    a short observation identifier (row id + state), never prose."""
    available: bool
    matched: bool
    evidence: str = ''
    reason: str = ''


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    run: object                  # (CheckCtx) -> Observation; fixed module functions only
    params: dict = field(default_factory=dict)
    description: str = ''
    proves_resolution: bool = True


@dataclass(frozen=True)
class CheckCtx:
    gym_key: str
    request_key: str
    merged_sha: str
    params: dict
    read: object                 # injected reader: (table, params) -> list[dict]
    observed_at: datetime        # timezone-aware clock owned by the observer
    ticket_id: str = ''
    receipt_read: object = None


def _parse_utc_timestamp(value):
    """Parse an ISO timestamp without guessing a timezone.

    Stored grades are trusted only when they carry an explicit offset.  Accept
    the Postgres-style trailing Z but reject naive timestamps: interpreting a
    server-local value as UTC could turn stale or future evidence into proof.
    """
    if not isinstance(value, str) or not value.strip():
        raise CheckUnavailable('grade_timestamp_unreadable')
    raw = value.strip()
    if raw.endswith(('Z', 'z')):
        raw = raw[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise CheckUnavailable('grade_timestamp_unreadable')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CheckUnavailable('grade_timestamp_unreadable')
    return parsed.astimezone(timezone.utc)


def _code(exc):
    # Reasons are fixed codes from this module; sanitize anyway so a sloppy future
    # raise cannot smuggle prose or secrets into a durable record.
    code = re.sub(r'[^a-z0-9_:-]', '', str(exc).lower())[:_MAX_REASON]
    return code or 'refused'


def _bounded_read(read, table, params, limit):
    """One bounded read through the injected reader. Any reader fault, shape
    violation, or limit breach is UNAVAILABLE: partial evidence is not evidence.
    The exception text is deliberately not carried into the record."""
    try:
        rows = read(table, params)
    except (CheckRefused, CheckUnavailable):
        raise
    except Exception:
        raise CheckUnavailable('reader_unavailable')
    if (not isinstance(rows, list) or len(rows) > limit
            or any(not isinstance(r, dict) for r in rows)):
        raise CheckUnavailable('reader_partial')
    return rows


def _read_rows(ctx, table, params, limit):
    return _bounded_read(ctx.read, table, params, limit)


def _resolve_echo_gym_key(read, portal_gym_id):
    """Resolve the caller's portal UUID through the authoritative alias plane.

    Both directions must be unique and agree.  The second read prevents a
    duplicated/collided Echo key from being used as proof for either tenant.
    The caller never supplies the evidence-table key directly.
    """
    rows = _bounded_read(read, 'echo_intake_tokens', {
        'gym_id': f'eq.{portal_gym_id}',
        'select': 'gym_id,echo_account_key', 'limit': '2'}, 2)
    if not rows:
        raise CheckUnavailable('tenant_binding_missing')
    if len(rows) != 1:
        raise CheckUnavailable('tenant_binding_ambiguous')
    row = rows[0]
    echo_key = row.get('echo_account_key')
    if (row.get('gym_id') != portal_gym_id or not isinstance(echo_key, str)
            or not _GYM_KEY.fullmatch(echo_key)):
        raise CheckUnavailable('tenant_binding_invalid')
    reverse = _bounded_read(read, 'echo_intake_tokens', {
        'echo_account_key': f'eq.{echo_key}',
        'select': 'gym_id,echo_account_key', 'limit': '2'}, 2)
    if len(reverse) != 1:
        raise CheckUnavailable(
            'tenant_binding_missing' if not reverse else 'tenant_binding_ambiguous')
    if (reverse[0].get('gym_id') != portal_gym_id
            or reverse[0].get('echo_account_key') != echo_key):
        raise CheckUnavailable('tenant_binding_mismatch')
    return echo_key


def _check_calendar_row_status(ctx):
    """One content_calendar row for this tenant reads back at the expected status."""
    row_id = ctx.params.get('row_id')
    expected = ctx.params.get('expected_status')
    if not isinstance(row_id, str) or not _ROW_ID.fullmatch(row_id):
        raise CheckRefused('bad_params')
    if not isinstance(expected, str) or not _STATUS.fullmatch(expected):
        raise CheckRefused('bad_params')
    echo_gym_key = _resolve_echo_gym_key(ctx.read, ctx.gym_key)
    rows = _read_rows(ctx, 'content_calendar',
                      {'id': f'eq.{row_id}', 'gym_id': f'eq.{echo_gym_key}',
                       'select': 'id,gym_id,status', 'limit': '2'}, 2)
    if not rows:
        return Observation(True, False, f'calendar_row:{row_id}:absent', 'row_not_found')
    if len(rows) != 1 or rows[0].get('id') != row_id:
        raise CheckUnavailable('reader_partial')
    row = rows[0]
    if row.get('gym_id') != echo_gym_key:
        raise CheckUnavailable('scope_mismatch')
    status = row.get('status')
    if not isinstance(status, str) or not _STATUS.fullmatch(status):
        return Observation(True, False, f'calendar_row:{row_id}:unreadable',
                           'status_unreadable')
    if status != expected:
        return Observation(True, False, f'calendar_row:{row_id}:{status}',
                           'status_mismatch')
    return Observation(True, True, f'calendar_row:{row_id}:{status}')


def _check_forward_book_grade_at_least(ctx):
    """The latest stored forward_book grade for this tenant meets a minimum total."""
    minimum = ctx.params.get('min_total')
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 0 <= minimum <= 100:
        raise CheckRefused('bad_params')
    echo_gym_key = _resolve_echo_gym_key(ctx.read, ctx.gym_key)
    rows = _read_rows(ctx, 'gym_social_grades',
                      {'gym_id': f'eq.{echo_gym_key}', 'window': 'eq.forward_book',
                       'select': 'gym_id,total,graded_at',
                       'order': 'graded_at.desc', 'limit': '1'}, 1)
    if not rows:
        return Observation(True, False, 'forward_book_grade:none', 'grade_not_found')
    row = rows[0]
    if row.get('gym_id') != echo_gym_key:
        raise CheckUnavailable('scope_mismatch')
    total = row.get('total')
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 100:
        raise CheckUnavailable('reader_partial')
    graded_at = _parse_utc_timestamp(row.get('graded_at'))
    observed_at = ctx.observed_at
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise CheckUnavailable('observer_clock_unreadable')
    age = observed_at.astimezone(timezone.utc) - graded_at
    if age < timedelta(0):
        return Observation(True, False, f'forward_book_grade:{total}',
                           'grade_from_future')
    if age > _FORWARD_BOOK_MAX_AGE:
        return Observation(True, False, f'forward_book_grade:{total}',
                           'grade_stale')
    if total < minimum:
        return Observation(True, False, f'forward_book_grade:{total}',
                           'grade_below_minimum')
    return Observation(True, True, f'forward_book_grade:{total}')


def _check_media_source_active(ctx):
    """One Drive binding for this tenant reads back active, unrevoked, not failed."""
    folder_id = ctx.params.get('folder_id')
    if not isinstance(folder_id, str) or not _FOLDER_ID.fullmatch(folder_id):
        raise CheckRefused('bad_params')
    echo_gym_key = _resolve_echo_gym_key(ctx.read, ctx.gym_key)
    rows = _read_rows(ctx, 'media_source',
                      {'folder_id': f'eq.{folder_id}', 'gym_id': f'eq.{echo_gym_key}',
                       'select': 'id,gym_id,folder_id,active,revoked_externally,sync_status',
                       'limit': '2'}, 2)
    if not rows:
        return Observation(True, False, f'media_source:{folder_id}:absent',
                           'source_not_found')
    if len(rows) != 1:
        raise CheckUnavailable('source_ambiguous')
    row = rows[0]
    if row.get('gym_id') != echo_gym_key or row.get('folder_id') != folder_id:
        raise CheckUnavailable('scope_mismatch')
    raw_status = row.get('sync_status')
    status = (raw_status if isinstance(raw_status, str)
              and _STATUS.fullmatch(raw_status) else 'unknown')
    evidence = f'media_source:{folder_id}:{status}'
    if row.get('active') is not True:
        return Observation(True, False, evidence, 'source_inactive')
    if row.get('revoked_externally') is True:
        return Observation(True, False, evidence, 'source_revoked')
    if status == 'failed':
        return Observation(True, False, evidence, 'source_sync_failed')
    if status not in _HEALTHY_MEDIA_SYNC_STATUSES:
        return Observation(True, False, evidence, 'source_sync_not_ready')
    return Observation(True, True, evidence)


def _tenant_snapshot(ctx, table, gym_key, fields):
    """Read every row in bounded keyset pages; a truncated page cannot prove a pool.

    The reader is required to honor the id cursor and sort order. A repeated or
    out-of-order row, cross-tenant row, or exhausted safety cap is UNKNOWN.
    """
    result = []
    last_id = None
    while len(result) < _MEDIA_MAX_ROWS:
        params = {'gym_id': f'eq.{gym_key}', 'select': ','.join(fields),
                  'order': 'id.asc', 'limit': str(_MEDIA_PAGE_SIZE)}
        if last_id is not None:
            params['id'] = f'gt.{last_id}'
        page = _read_rows(ctx, table, params, _MEDIA_PAGE_SIZE)
        for row in page:
            row_id = row.get('id')
            if (row.get('gym_id') != gym_key or not isinstance(row_id, str)
                    or not _MEDIA_ID.fullmatch(row_id)
                    or (last_id is not None and row_id <= last_id)):
                raise CheckUnavailable('reader_partial')
            last_id = row_id
            result.append(row)
        if len(page) < _MEDIA_PAGE_SIZE:
            return result
    raise CheckUnavailable('reader_partial')


class _MediaSnapshotStore:
    """In-memory adapter for the production selector; never accesses the default store."""

    def __init__(self, rows):
        self.rows = rows

    def available(self):
        return True

    def list_assets(self, _gym_key):
        return self.rows


def _check_media_swap_candidate_available(ctx):
    """Prove Drive candidate readiness, not a completed portal swap.

    Every book reference is excluded, including historical/denied rows. This
    conservative superset of media_swap's book exclusion cannot create a false
    positive for a pending post whose exact row is not part of this check.
    """
    if (set(ctx.params) != {'min_count'} or type(ctx.params.get('min_count')) is not int
            or ctx.params['min_count'] != 1):
        raise CheckRefused('bad_params')
    if ctx.observed_at.tzinfo is None or ctx.observed_at.utcoffset() is None:
        raise CheckUnavailable('observer_clock_unreadable')
    echo_key = _resolve_echo_gym_key(ctx.read, ctx.gym_key)
    assets = _tenant_snapshot(ctx, 'media_asset', echo_key, _MEDIA_FIELDS)
    for asset in assets:
        if any(field not in asset for field in _MEDIA_FIELDS):
            raise CheckUnavailable('reader_partial')
        used_at = asset['last_used_at']
        if used_at is not None:
            _parse_utc_timestamp(used_at)
        count = asset['used_count']
        if type(count) is not int or count < 0:
            raise CheckUnavailable('reader_partial')
        if asset['kind'] not in ('photo', 'video', 'other'):
            raise CheckUnavailable('reader_partial')
    book = _tenant_snapshot(ctx, 'content_calendar', echo_key,
                            ('id', 'gym_id', 'source_media_asset_id'))
    blocked = set()
    for row in book:
        if 'source_media_asset_id' not in row:
            raise CheckUnavailable('reader_partial')
        asset_id = row['source_media_asset_id']
        if asset_id is not None:
            if not isinstance(asset_id, str):
                raise CheckUnavailable('reader_partial')
            if asset_id:
                blocked.add(asset_id)
    candidates = gym_media_selector.pickable(
        echo_key, store=_MediaSnapshotStore(assets), now=ctx.observed_at,
        exclude_ids=blocked)
    # The portal's Drive swap accepts photos and videos only.
    candidates = [row for row in candidates if row['kind'] in ('photo', 'video')]
    if not candidates:
        return Observation(True, False, 'media_swap_candidates:0', 'candidate_unavailable')
    return Observation(True, True, f'media_swap_candidates:{len(candidates)}')


def _check_media_swap_completed(ctx):
    """Prove a keyed FIXER swap landed and remains on an approved Drive asset.

    A current row by itself has no before-state. The durable FIXER receipt must
    bind this ticket, tenant, and row and carry before/after hashes captured from
    independent calendar reads around the actual action.
    """
    if set(ctx.params) != {'reservation_key', 'row_id'}:
        raise CheckRefused('bad_params')
    key = ctx.params.get('reservation_key')
    row_id = ctx.params.get('row_id')
    if (not isinstance(key, str) or not _RECEIPT_KEY.fullmatch(key)
            or not isinstance(row_id, str) or not _ROW_ID.fullmatch(row_id)):
        raise CheckRefused('bad_params')
    if not _PORTAL_GYM_ID.fullmatch(ctx.ticket_id):
        raise CheckUnavailable('ticket_binding_missing')
    if not callable(ctx.receipt_read):
        raise CheckUnavailable('receipt_unavailable')
    echo_key = _resolve_echo_gym_key(ctx.read, ctx.gym_key)
    try:
        receipt = ctx.receipt_read(key, echo_key)
    except Exception:
        raise CheckUnavailable('receipt_unavailable')
    if not isinstance(receipt, dict):
        raise CheckUnavailable('receipt_unavailable')
    if (receipt.get('schema_version') != 1 or receipt.get('key') != key
            or receipt.get('gym_key') != echo_key
            or receipt.get('ticket_id') != ctx.ticket_id
            or receipt.get('action') != 'swap_media'):
        raise CheckUnavailable('receipt_binding_mismatch')
    if receipt.get('request_key') != ctx.request_key:
        return Observation(True, False, f'media_swap:{row_id}:stale',
                           'receipt_request_mismatch')
    # A new inbound complaint changes the current request. A receipt from before
    # that complaint cannot close it merely because the row still carries old
    # swapped media. Include all inbound rows conservatively, including staff
    # mentions, and refuse partial histories.
    inbound = _read_rows(ctx, 'support_messages', {
        'ticket_id': f'eq.{ctx.ticket_id}', 'direction': 'eq.inbound',
        'select': 'ticket_id,direction,created_at', 'limit': '1000'}, 1000)
    if not inbound or len(inbound) >= 1000:
        raise CheckUnavailable('request_history_unreadable')
    latest_inbound = None
    for message in inbound:
        if (message.get('ticket_id') != ctx.ticket_id
                or message.get('direction') != 'inbound'):
            raise CheckUnavailable('request_history_unreadable')
        sent_at = _parse_utc_timestamp(message.get('created_at'))
        latest_inbound = max(latest_inbound, sent_at) if latest_inbound else sent_at
    started = _parse_utc_timestamp(receipt.get('created_at'))
    if started < latest_inbound:
        return Observation(True, False, f'media_swap:{row_id}:stale',
                           'receipt_before_request')
    if receipt.get('status') != 'done':
        return Observation(True, False, f'media_swap:{row_id}:incomplete',
                           'receipt_not_done')
    result = receipt.get('result')
    if (not isinstance(result, dict) or result.get('postcondition_verified') is not True
            or result.get('row_id') != row_id):
        return Observation(True, False, f'media_swap:{row_id}:unproven',
                           'receipt_unproven')
    proof = result.get('swap_proof')
    if not isinstance(proof, dict) or proof.get('row_id') != row_id:
        return Observation(True, False, f'media_swap:{row_id}:unproven',
                           'receipt_unproven')
    before_hash = proof.get('before_image_sha256')
    after_hash = proof.get('after_image_sha256')
    caption_hash = proof.get('caption_sha256')
    asset_id = proof.get('after_asset_id')
    if (any(not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest)
            for digest in (before_hash, after_hash, caption_hash))
            or before_hash == after_hash
            or not isinstance(asset_id, str) or not _MEDIA_ID.fullmatch(asset_id)
            or asset_id == proof.get('before_asset_id')):
        raise CheckUnavailable('receipt_proof_invalid')
    finished = _parse_utc_timestamp(receipt.get('finished_at'))
    observed = ctx.observed_at
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise CheckUnavailable('observer_clock_unreadable')
    if finished < started or finished > observed.astimezone(timezone.utc):
        raise CheckUnavailable('receipt_clock_invalid')
    rows = _read_rows(ctx, 'content_calendar', {
        'id': f'eq.{row_id}', 'gym_id': f'eq.{echo_key}',
        'select': 'id,gym_id,status,caption,image_url,source_media_asset_id',
        'limit': '2'}, 2)
    if len(rows) != 1 or rows[0].get('id') != row_id:
        raise CheckUnavailable('calendar_row_unreadable')
    row = rows[0]
    if row.get('gym_id') != echo_key:
        raise CheckUnavailable('scope_mismatch')
    caption = row.get('caption')
    image_url = row.get('image_url')
    if not isinstance(caption, str) or not isinstance(image_url, str):
        raise CheckUnavailable('calendar_row_unreadable')
    digest = lambda value: hashlib.sha256(value.encode('utf-8')).hexdigest()
    if (row.get('status') not in ('pending', 'coach_review')
            or row.get('source_media_asset_id') != asset_id
            or digest(image_url) != after_hash or digest(caption) != caption_hash):
        return Observation(True, False, f'media_swap:{row_id}:changed',
                           'swap_state_mismatch')
    assets = _read_rows(ctx, 'media_asset', {
        'id': f'eq.{asset_id}', 'gym_id': f'eq.{echo_key}',
        'select': ','.join(_MEDIA_FIELDS), 'limit': '2'}, 2)
    if len(assets) != 1 or assets[0].get('id') != asset_id:
        raise CheckUnavailable('asset_unreadable')
    asset = assets[0]
    if asset.get('gym_id') != echo_key or any(field not in asset for field in _MEDIA_FIELDS):
        raise CheckUnavailable('scope_mismatch')
    if asset.get('kind') not in ('photo', 'video') or not gym_media_selector.is_usable(asset):
        return Observation(True, False, f'media_swap:{row_id}:asset_blocked',
                           'asset_not_approved')
    evidence = f'media_swap:{row_id}:{asset_id}'
    if len(evidence) > _MAX_EVIDENCE:
        evidence = f'media_swap:{row_id}:{hashlib.sha256(asset_id.encode()).hexdigest()[:16]}'
    return Observation(True, True, evidence)


CHECKS = MappingProxyType({
    'calendar_row_status': CheckSpec(
        'calendar_row_status', _check_calendar_row_status,
        params={'row_id': 'content_calendar row id',
                'expected_status': 'the postcondition status, e.g. published'},
        description='one calendar row for this tenant reads back at the expected status'),
    'forward_book_grade_at_least': CheckSpec(
        'forward_book_grade_at_least', _check_forward_book_grade_at_least,
        params={'min_total': 'integer 0..100'},
        description='latest stored forward_book grade for this tenant meets a minimum'),
    'media_source_active': CheckSpec(
        'media_source_active', _check_media_source_active,
        params={'folder_id': 'Drive folder id bound to this tenant'},
        description='one media source for this tenant reads back active and healthy'),
    'media_swap_candidate_available': CheckSpec(
        'media_swap_candidate_available', _check_media_swap_candidate_available,
        params={'min_count': 'integer 1'},
        description='at least one tenant-scoped Drive asset is currently selectable for a swap; does not prove a swap completed',
        proves_resolution=False),
    'media_swap_completed': CheckSpec(
        'media_swap_completed', _check_media_swap_completed,
        params={'reservation_key': 'durable keyed FIXER swap receipt',
                'row_id': 'content_calendar row id swapped by that receipt'},
        description='a keyed FIXER swap receipt and current tenant row prove a changed, still-waiting approved Drive asset with unchanged caption'),
})


def catalog():
    """The registered checks, as plain JSON. Unknown ids are never runnable."""
    return {'ok': True, 'checks': [
        {'check_id': s.check_id, 'params': dict(s.params), 'description': s.description}
        for s in CHECKS.values()]}


def _record(check_id, gym_key, request_key, merged_sha, captured_at, outcome,
            evidence, reason, symptom):
    record = {'schema_version': SCHEMA_VERSION, 'source': SOURCE,
              'check_id': check_id, 'gym_key': gym_key,
              'request_key': request_key, 'merged_sha': merged_sha,
              'captured_at': captured_at, 'outcome': outcome,
              'verified': outcome == VERIFIED, 'symptom_resolved': symptom,
              'evidence': evidence, 'reason': reason}
    try:
        json.dumps(record)
    except (TypeError, ValueError):
        # A record that cannot be persisted must degrade to a safe unknown, never
        # escape half-built with a verified flag on it.
        record = {'schema_version': SCHEMA_VERSION, 'source': SOURCE,
                  'check_id': str(check_id)[:80], 'gym_key': str(gym_key)[:80],
                  'request_key': str(request_key)[:200],
                  'merged_sha': str(merged_sha)[:128],
                  'captured_at': captured_at if isinstance(captured_at, str) else '',
                  'outcome': UNKNOWN, 'verified': False, 'symptom_resolved': None,
                  'evidence': '', 'reason': 'result_invalid'}
    return record


def observe(check_id, *, gym_key, request_key, merged_sha, params=None, deps=None,
            ticket_id=None, now=None):
    return _observe(check_id, gym_key=gym_key, request_key=request_key,
                    merged_sha=merged_sha, params=params, deps=deps,
                    ticket_id=ticket_id, now=now, ops_swap=False)


def observe_ops_media_swap(*, gym_key, request_key, params, deps, ticket_id,
                           now=None):
    """Read back a keyed swap without inventing a code release SHA.

    Only the registered media_swap_completed check may use this route. Its durable
    receipt binds the ticket, tenant, request, and row; the ordinary code-fix
    observe() route retains its mandatory merged release identity.
    """
    return _observe('media_swap_completed', gym_key=gym_key,
                    request_key=request_key, merged_sha='', params=params,
                    deps=deps, ticket_id=ticket_id, now=now, ops_swap=True)


def _observe(check_id, *, gym_key, request_key, merged_sha, params=None, deps=None,
             ticket_id=None, now=None, ops_swap=False):
    """Run one registered check and return the durable evidence dict.

    Never raises into the caller: every failure mode is recorded in the object
    itself, and `verified` is True only on a genuine independent readback match.
    """
    gym_key = gym_key if isinstance(gym_key, str) else ''
    request_key = request_key if isinstance(request_key, str) else ''
    merged_sha = merged_sha if isinstance(merged_sha, str) else ''
    check_id = check_id.strip() if isinstance(check_id, str) else ''
    captured = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    captured_at = captured.isoformat()

    def record(outcome, evidence='', reason='', symptom=None):
        return _record(check_id, gym_key, request_key, merged_sha, captured_at,
                       outcome, evidence, reason, symptom)

    spec = CHECKS.get(check_id)
    if spec is None:
        # Refused before any identity handling: nothing dynamic ever runs.
        return record(UNVERIFIED, reason='unknown_check')
    if not _PORTAL_GYM_ID.fullmatch(gym_key):
        return record(UNVERIFIED, reason='bad_gym_key')
    if not _REQUEST_KEY.fullmatch(request_key):
        return record(UNVERIFIED, reason='bad_request_key')
    if not ops_swap and not _RELEASE_SHA.fullmatch(merged_sha):
        return record(UNVERIFIED, reason='bad_release_id')
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return record(UNVERIFIED, reason='bad_params')
    read = deps.get('read') if isinstance(deps, dict) else None
    if not callable(read):
        return record(UNKNOWN, reason='reader_unavailable')
    ctx = CheckCtx(gym_key=gym_key, request_key=request_key, merged_sha=merged_sha,
                   params=params, read=read, observed_at=captured,
                   ticket_id=ticket_id if isinstance(ticket_id, str) else '',
                   receipt_read=deps.get('receipt_read'))
    try:
        seen = spec.run(ctx)
    except CheckRefused as refused:
        return record(UNVERIFIED, reason=_code(refused))
    except CheckUnavailable as unavailable:
        return record(UNKNOWN, reason=_code(unavailable))
    except Exception:
        # A check fault is not evidence in either direction.
        return record(UNKNOWN, reason='check_fault')
    if (not isinstance(seen, Observation)
            or not isinstance(seen.available, bool) or not isinstance(seen.matched, bool)
            or not isinstance(seen.evidence, str) or len(seen.evidence) > _MAX_EVIDENCE
            or not isinstance(seen.reason, str) or len(seen.reason) > _MAX_REASON):
        return record(UNKNOWN, reason='result_invalid')
    if not seen.available:
        return record(UNKNOWN, seen.evidence, seen.reason or 'evidence_unavailable')
    if seen.matched:
        if not seen.evidence.strip():
            # A match without its observation identifier cannot close anything.
            return record(UNKNOWN, reason='result_invalid')
        return record(VERIFIED, seen.evidence,
                      symptom=True if spec.proves_resolution else None)
    return record(UNVERIFIED, seen.evidence,
                  seen.reason or 'postcondition_unconfirmed', symptom=False)


def binding_matches(evidence, *, gym_key, request_key, merged_sha):
    """Exact identity match between a durable evidence object and the release a
    caller wants to close. Identity only; the outcome fields speak for themselves.
    A record produced for one request, tenant, or release can never stand in for
    another."""
    return (isinstance(evidence, dict)
            and evidence.get('schema_version') == SCHEMA_VERSION
            and evidence.get('source') == SOURCE
            and bool(gym_key) and evidence.get('gym_key') == gym_key
            and bool(request_key) and evidence.get('request_key') == request_key
            and bool(merged_sha) and evidence.get('merged_sha') == merged_sha)
