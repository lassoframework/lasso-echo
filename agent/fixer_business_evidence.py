"""
fixer_business_evidence.py -- independent business postcondition evidence.

A bounded, tenant-scoped, READ-ONLY observer for the FIXER release loop. A healthy
deploy proves the code is live, not that the owner's symptom is gone; the customer
closure gate (slack_convo/outbox._verified_fix_notice) therefore requires a
business_postcondition record with source 'independent_business_check', verified and
symptom_resolved True, a check id, a short evidence identifier, and the exact
request_key and merged_sha of the release being closed. This module produces those
records: every observation durably binds the request identity (request_key), the
tenant (gym_key) and the release identity (merged_sha) to an outcome, with a
captured_at timestamp. Nothing here writes to that consumer; the field names simply
line up so a future wiring step is a pass-through.

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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType

VERIFIED = 'verified'
UNVERIFIED = 'unverified'
UNKNOWN = 'unknown'

SOURCE = 'independent_business_check'
SCHEMA_VERSION = 1

_GYM_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{2,79}\Z')
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


@dataclass(frozen=True)
class CheckCtx:
    gym_key: str
    request_key: str
    merged_sha: str
    params: dict
    read: object                 # injected reader: (table, params) -> list[dict]


def _code(exc):
    # Reasons are fixed codes from this module; sanitize anyway so a sloppy future
    # raise cannot smuggle prose or secrets into a durable record.
    code = re.sub(r'[^a-z0-9_:-]', '', str(exc).lower())[:_MAX_REASON]
    return code or 'refused'


def _read_rows(ctx, table, params, limit):
    """One bounded read through the injected reader. Any reader fault, shape
    violation, or limit breach is UNAVAILABLE: partial evidence is not evidence.
    The exception text is deliberately not carried into the record."""
    try:
        rows = ctx.read(table, params)
    except (CheckRefused, CheckUnavailable):
        raise
    except Exception:
        raise CheckUnavailable('reader_unavailable')
    if (not isinstance(rows, list) or len(rows) > limit
            or any(not isinstance(r, dict) for r in rows)):
        raise CheckUnavailable('reader_partial')
    return rows


def _check_calendar_row_status(ctx):
    """One content_calendar row for this tenant reads back at the expected status."""
    row_id = ctx.params.get('row_id')
    expected = ctx.params.get('expected_status')
    if not isinstance(row_id, str) or not _ROW_ID.fullmatch(row_id):
        raise CheckRefused('bad_params')
    if not isinstance(expected, str) or not _STATUS.fullmatch(expected):
        raise CheckRefused('bad_params')
    rows = _read_rows(ctx, 'content_calendar',
                      {'id': f'eq.{row_id}', 'gym_id': f'eq.{ctx.gym_key}',
                       'select': 'id,gym_id,status', 'limit': '2'}, 2)
    if not rows:
        return Observation(True, False, f'calendar_row:{row_id}:absent', 'row_not_found')
    if len(rows) != 1 or rows[0].get('id') != row_id:
        raise CheckUnavailable('reader_partial')
    row = rows[0]
    if row.get('gym_id') != ctx.gym_key:
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
    rows = _read_rows(ctx, 'gym_social_grades',
                      {'gym_id': f'eq.{ctx.gym_key}', 'window': 'eq.forward_book',
                       'select': 'gym_id,total,graded_at',
                       'order': 'graded_at.desc', 'limit': '1'}, 1)
    if not rows:
        return Observation(True, False, 'forward_book_grade:none', 'grade_not_found')
    row = rows[0]
    if row.get('gym_id') != ctx.gym_key:
        raise CheckUnavailable('scope_mismatch')
    total = row.get('total')
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 100:
        raise CheckUnavailable('reader_partial')
    if total < minimum:
        return Observation(True, False, f'forward_book_grade:{total}',
                           'grade_below_minimum')
    return Observation(True, True, f'forward_book_grade:{total}')


def _check_media_source_active(ctx):
    """One Drive binding for this tenant reads back active, unrevoked, not failed."""
    folder_id = ctx.params.get('folder_id')
    if not isinstance(folder_id, str) or not _FOLDER_ID.fullmatch(folder_id):
        raise CheckRefused('bad_params')
    rows = _read_rows(ctx, 'media_source',
                      {'folder_id': f'eq.{folder_id}', 'gym_id': f'eq.{ctx.gym_key}',
                       'select': 'id,gym_id,folder_id,active,revoked_externally,sync_status',
                       'limit': '2'}, 2)
    if not rows:
        return Observation(True, False, f'media_source:{folder_id}:absent',
                           'source_not_found')
    if len(rows) != 1:
        raise CheckUnavailable('source_ambiguous')
    row = rows[0]
    if row.get('gym_id') != ctx.gym_key or row.get('folder_id') != folder_id:
        raise CheckUnavailable('scope_mismatch')
    status = row.get('sync_status')
    status = status if isinstance(status, str) and _STATUS.fullmatch(status) else 'unknown'
    evidence = f'media_source:{folder_id}:{status}'
    if row.get('active') is not True:
        return Observation(True, False, evidence, 'source_inactive')
    if row.get('revoked_externally') is True:
        return Observation(True, False, evidence, 'source_revoked')
    if status == 'failed':
        return Observation(True, False, evidence, 'source_sync_failed')
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
            now=None):
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
    if not _GYM_KEY.fullmatch(gym_key):
        return record(UNVERIFIED, reason='bad_gym_key')
    if not _REQUEST_KEY.fullmatch(request_key):
        return record(UNVERIFIED, reason='bad_request_key')
    if not _RELEASE_SHA.fullmatch(merged_sha):
        return record(UNVERIFIED, reason='bad_release_id')
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return record(UNVERIFIED, reason='bad_params')
    read = deps.get('read') if isinstance(deps, dict) else None
    if not callable(read):
        return record(UNKNOWN, reason='reader_unavailable')
    ctx = CheckCtx(gym_key=gym_key, request_key=request_key, merged_sha=merged_sha,
                   params=params, read=read)
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
        return record(VERIFIED, seen.evidence, symptom=True)
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
