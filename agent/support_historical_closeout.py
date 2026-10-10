"""Explicit single-ticket historical receipt preparation. Never dispatches Slack.

0640 is a draft dependency. A ready row is preparation, not delivery or closeout.
A lost reserve/insert response requires reconciliation of the original notice ID.
"""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from .jobs.client_support_reconciler import _guarded_receipt_qualifies, _receipt_is_client_visible

from .slack_convo.outbox import (HISTORICAL_PRECLOSE_BODY,
    _current_fixer_request_key, _historical_receipt_state)


class HistoricalCloseoutError(RuntimeError):
    pass


IDENTITY = ('request_version', 'source', 'product', 'classification', 'client_id',
            'bot_identity', 'slack_user_id', 'slack_channel_id', 'slack_thread_ts',
            'resolved_at')

PRECLOSE_BODY = HISTORICAL_PRECLOSE_BODY


def _identity_matches(left, right):
    for field in IDENTITY:
        a, b = left.get(field), right.get(field)
        if field == 'resolved_at':
            try:
                a = datetime.fromisoformat(a.replace('Z', '+00:00'))
                b = datetime.fromisoformat(b.replace('Z', '+00:00'))
            except (AttributeError, TypeError, ValueError):
                return False
        if a != b:
            return False
    return True


def _reviewed_resolution(ticket, body, review):
    """Bind an independent production review to this exact ticket and message.

    This is an evidence contract, not a proof that a supplied claim is true.
    The separate verifier must check the referenced production artifacts.
    """
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    if (not isinstance(review, dict) or review.get('ticket_id') != ticket['id']
            or review.get('request_version') != ticket['request_version']
            or review.get('body_sha256') != digest
            or not isinstance(review.get('independently_verified_by'), str)
            or not review['independently_verified_by'].strip()
            or not isinstance(review.get('evidence_ref'), str)
            or not review['evidence_ref'].strip()
            or not isinstance(review.get('production_evidence_refs'), list)
            or not review['production_evidence_refs']
            or any(not isinstance(ref, str) or not ref.strip()
                   for ref in review['production_evidence_refs'])
            or not isinstance(review.get('verified_scope'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ,/&()\-]{4,149}', review['verified_scope'])
            or re.search(r'\b(?:clos(?:e|ed|ing)|ticket|published|delivered)\b',
                         review['verified_scope'], re.I)):
        raise HistoricalCloseoutError('exact independent production review missing')
    return review['evidence_ref'], digest


def prepare_historical_receipt(bus, *, expected_ticket, body, review,
                               selected_notice_id):
    """Reserve and create one caller-selected exact ID; no retries or send sweep.

    Caller supplies an independent production review bound to the exact body.
    Completion wording must stay within verified scope; finalization is still pending.
    Existing posted receipts block even when their readback is ambiguous.
    """
    ticket_id = expected_ticket['id']
    try:
        if str(uuid.UUID(selected_notice_id)) != selected_notice_id:
            raise ValueError('noncanonical notice ID')
    except (ValueError, TypeError, AttributeError) as exc:
        raise HistoricalCloseoutError('invalid exact notice ID') from exc
    ticket = bus.ticket(ticket_id)
    if (not _historical_receipt_state(ticket)
            or not _identity_matches(ticket, expected_ticket)
            or ticket.get('product') == 'echo' and ticket.get('source') == 'website_tab'
            or type(ticket.get('request_version')) is not int
            or ticket['request_version'] < 0 or not ticket.get('resolved_at')
            or not ticket.get('slack_channel_id')
            or ticket.get('identity_kind') not in (None, 'client')
            or ticket.get('bot_identity') not in ('ranger', 'echo', 'scout', 'wrangler', 'lainey')):
        raise HistoricalCloseoutError('ineligible or changed ticket identity')
    if (not isinstance(review, dict) or not isinstance(review.get('verified_scope'), str)
            or not isinstance(ticket.get('slack_user_id'), str)
            or not ticket['slack_user_id'].strip()
            or body != PRECLOSE_BODY.format(recipient=ticket['slack_user_id'],
                                           scope=review['verified_scope'])):
        raise HistoricalCloseoutError('preclose body/requester/review mismatch')
    evidence_ref, digest = _reviewed_resolution(ticket, body, review)
    rows = bus.messages(ticket_id, limit=1000)
    if not isinstance(rows, list) or len(rows) >= 1000:
        raise HistoricalCloseoutError('receipt scan incomplete')
    if any((_receipt_is_client_visible(row) if ticket.get("client_delivery_guard_required") is False
            else _guarded_receipt_qualifies(ticket, row)) for row in rows):
        raise HistoricalCloseoutError('existing posted completion receipt')
    if bus.message(selected_notice_id):
        raise HistoricalCloseoutError('notice ID already exists; reconcile without repost')
    key = _current_fixer_request_key(bus, ticket)
    if not key:
        raise HistoricalCloseoutError('request transcript unavailable')
    reservation = bus.reserve_historical_receipt(ticket, request_key=key,
        evidence_ref=evidence_ref, body_sha256=digest, notice_id=selected_notice_id)
    if (not isinstance(reservation, dict) or reservation.get('status') != 'pending'
            or reservation.get('ticket_id') != ticket_id
            or reservation.get('notice_message_id') != selected_notice_id
            or reservation.get('request_key') != key
            or reservation.get('notice_body_sha256') != digest
            or reservation.get('evidence_ref') != evidence_ref
            or not _identity_matches(reservation, ticket)):
        raise HistoricalCloseoutError('reservation refused or ambiguous; no retry')
    fresh = bus.ticket(ticket_id)
    if (not _historical_receipt_state(fresh)
            or not _identity_matches(fresh, ticket)
            or _current_fixer_request_key(bus, fresh) != key):
        raise HistoricalCloseoutError('ticket/transcript changed after reserve; reconcile')
    att = {'fixer': True, 'kind': 'status', 'resolve_notice': True,
           'identity': ticket['bot_identity'],
           'recipient_kind': 'client',
           'delivery_identity_fence': True, 'request_version': ticket['request_version'],
           'request_key': key, 'historical_receipt_recovery': True,
           'historical_receipt_notice_id': selected_notice_id,
           'historical_receipt_body_sha256': digest,
           'historical_receipt_verified_scope': review['verified_scope'],
           # Local correlation only. SQL has no opaque reservation token.
           'historical_receipt_reservation': f'{ticket_id}:{ticket["request_version"]}',
           'historical_receipt_evidence_ref': evidence_ref}
    for field in ('source', 'product', 'classification', 'client_id', 'bot_identity',
                  'slack_user_id', 'slack_channel_id', 'slack_thread_ts'):
        att['delivery_expected_' + field] = ticket.get(field)
    att['delivery_expected_status'] = 'resolved'
    return bus.record_outbound(ticket_id=ticket_id, author_type=ticket['bot_identity'],
        body=body, delivery_status='ready', kind='status', meta=att,
        expected_request_version=ticket['request_version'], message_id=selected_notice_id)


def _close_rpc(bus, name, params):
    """Single transport attempt. No admission RPC is implicitly retried."""
    response = bus._client().post(bus._rest('rpc/' + name),
        data=json.dumps(params), headers=bus._headers(), timeout=30)
    if response.status_code >= 400:
        raise HistoricalCloseoutError('close admission transport refused')
    return response.json()


def _posted_notice(bus, ticket, notice_id):
    row = bus.message(notice_id)
    if not isinstance(row, dict):
        raise HistoricalCloseoutError('exact posted notice missing')
    att = row.get('attachments') or {}
    intent = att.get('fixer_slack_delivery_intent') or {}
    version = ticket.get('request_version')
    key = _current_fixer_request_key(bus, ticket)
    body = row.get('body')
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest() if isinstance(body, str) else None
    root = (ticket.get('slack_thread_ts') is None or
            row.get('slack_ts') == ticket.get('slack_thread_ts') and intent.get('thread_ts') is None)
    expected_thread = None if root or att.get('surface') in ('im','mpim','portal_ticket_bridge') else ticket.get('slack_thread_ts')
    expected = {'request_version': version, 'request_key': key,
        'delivery_readback_channel': ticket.get('slack_channel_id'),
        'delivery_readback_thread_ts': expected_thread,
        'delivery_readback_ts': row.get('slack_ts'),
        'delivery_readback_sender': intent.get('sender'),
        'delivery_readback_body_sha256': digest,
        'delivery_readback_request_version': version, 'delivery_readback_request_key': key}
    for field in ('source','product','classification','client_id','bot_identity',
                  'slack_user_id','slack_channel_id','slack_thread_ts'):
        expected['delivery_expected_' + field] = ticket.get(field)
    expected['delivery_expected_status'] = 'resolved'
    if (not _historical_receipt_state(ticket) or not key
            or row.get('id') != notice_id or row.get('ticket_id') != ticket['id']
            or row.get('author_type') != (ticket.get('bot_identity') or 'ranger')
            or row.get('direction') != 'outbound' or row.get('delivery_status') != 'posted'
            or type(row.get('delivery_request_version')) is not int
            or row['delivery_request_version'] != version or not row.get('slack_ts')
            or any(k not in att or att[k] != value for k,value in expected.items())
            or any(att.get(k) is not True for k in ('fixer','resolve_notice','delivery_identity_fence','delivery_readback_verified'))
            or att.get('kind') != 'status'
            or any(att.get(k) is True for k in ('human_follow_up','hedged','progress','ack','follow_up_close'))
            or not intent.get('sender') or intent.get('channel') != ticket.get('slack_channel_id')
            or 'thread_ts' not in intent or intent['thread_ts'] != expected_thread
            or intent.get('body') != body or intent.get('request_version') != version
            or intent.get('request_key') != key or not _guarded_receipt_qualifies(ticket,row)):
        raise HistoricalCloseoutError('exact posted notice/readback fence unavailable')
    return row


def finalize_historical_receipt(bus, *, expected_ticket, selected_notice_id,
                                note, rpc=None):
    """One admitted 0640 finalization, followed by exact portal-parity readback.

    Deployment/build come from this worker's Railway environment. Unknown
    acquire/effect/finish outcomes permanently fence this Bus instance; reconcile
    durable admission externally before creating another finalization worker.
    This function never sends or creates an outbound notice.
    """
    if getattr(bus, '_historical_close_uncertain', False):
        raise HistoricalCloseoutError('previous close outcome uncertain; reconcile')
    deployment = os.environ.get('RAILWAY_DEPLOYMENT_ID','').strip()
    build = os.environ.get('RAILWAY_GIT_COMMIT_SHA','').strip()
    if not deployment or not build or not isinstance(note,str) or not note.strip():
        raise HistoricalCloseoutError('actual deployment/build/note unavailable')
    ticket = bus.ticket(expected_ticket['id'])
    if not isinstance(ticket,dict) or not _identity_matches(ticket,expected_ticket):
        raise HistoricalCloseoutError('ticket identity changed')
    row = _posted_notice(bus,ticket,selected_notice_id)
    rows = bus.messages(ticket['id'],limit=1000)
    if (not isinstance(rows,list) or len(rows)>=1000 or any(
            m.get('id') != selected_notice_id and
            (_receipt_is_client_visible(m) if ticket.get('client_delivery_guard_required') is False
             else _guarded_receipt_qualifies(ticket,m)) for m in rows)):
        raise HistoricalCloseoutError('competing receipt or incomplete scan')
    call = rpc or (lambda name,params: _close_rpc(bus,name,params))
    lane='support-ticket-close'
    state=call('support_admission_status_lane',{'p_lane':lane})
    if (not isinstance(state,dict) or state.get('lane')!=lane
            or type(state.get('generation')) is not int or state['generation']<0
            or state.get('paused') is not False
            or type(state.get('unresolved')) is not int or state['unresolved']!=0):
        raise HistoricalCloseoutError('close admission paused or denied')
    invocation=str(uuid.uuid4()); generation=state['generation']
    def finish(outcome):
        receipt=call('support_admission_finish_lane',dict(p_lane=lane,
            p_invocation_id=invocation,p_generation=generation,p_outcome=outcome))
        if (not isinstance(receipt,dict) or receipt.get('recorded') is not True
                or receipt.get('lane')!=lane or receipt.get('invocation_id')!=invocation):
            raise HistoricalCloseoutError('close finish unconfirmed')
    def uncertain():
        bus._historical_close_uncertain=True
        try: finish('unknown')
        except Exception: pass
    try:
        admitted=call('support_admission_acquire_lane',dict(p_lane=lane,
            p_expected_generation=generation,p_invocation_id=invocation,
            p_deployment=deployment,p_build=build))
    except Exception:
        uncertain(); raise
    if not isinstance(admitted,dict) or admitted.get('admitted') is not True:
        if not isinstance(admitted,dict) or admitted.get('admitted') is not False:
            uncertain()
        raise HistoricalCloseoutError('close admission denied or unknown')
    if (admitted.get('lane')!=lane or admitted.get('invocation_id')!=invocation
            or admitted.get('generation')!=generation):
        uncertain(); raise HistoricalCloseoutError('close admission identity unknown')
    try:
        latest = bus.ticket(ticket['id'])
        if (not isinstance(latest,dict) or not _identity_matches(latest,ticket)
                or _current_fixer_request_key(bus,latest) != row['attachments']['request_key']):
            raise HistoricalCloseoutError('request changed after close admission')
        _posted_notice(bus,latest,selected_notice_id)
    except Exception:
        uncertain()
        raise
    try:
        result=bus.finalize_historical_receipt(ticket_id=ticket['id'],notice_id=selected_notice_id,
            note=note,invocation_id=invocation,generation=generation,deployment=deployment,build=build)
    except Exception:
        uncertain(); raise
    if not isinstance(result,dict) or result.get('id')!=ticket['id']:
        uncertain()
        raise HistoricalCloseoutError('0640 finalizer denied or ambiguous; reconcile')
    try: finish('completed')
    except Exception:
        bus._historical_close_uncertain=True
        raise
    try:
        fresh=bus.ticket(ticket['id'])
        if not isinstance(fresh,dict) or not _identity_matches(fresh,ticket) or fresh.get('status')!='resolved':
            raise HistoricalCloseoutError('closed ticket readback unconfirmed')
        posted=_posted_notice(bus,fresh,selected_notice_id)
        recovery=((fresh.get('verification_after') or {}).get('fixer') or {}).get('historical_receipt_recovery')
        if (not isinstance(recovery,dict) or recovery.get('notice_message_id')!=selected_notice_id
                or recovery.get('request_version')!=ticket['request_version']
                or recovery.get('request_key')!=row['attachments']['request_key']
                or recovery.get('notice_body_sha256')!=hashlib.sha256(posted['body'].encode()).hexdigest()
                or not recovery.get('evidence_ref') or not _identity_matches(
                    {**fresh,'resolved_at':recovery.get('resolved_at')},ticket)):
            raise HistoricalCloseoutError('portal-visible historical receipt readback unconfirmed')
        return fresh
    except Exception:
        # The effect may have succeeded; a failed readback is never retry authority.
        bus._historical_close_uncertain=True
        raise
