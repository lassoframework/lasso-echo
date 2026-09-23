"""A FIXER swap closes only on a durable receipt plus fresh independent readback."""
import hashlib
from datetime import datetime, timezone

import pytest

from agent import fixer_business_evidence as be
from tests.gym_media_fakes import make_asset

PORTAL_GYM = '11111111-1111-4111-8111-111111111111'
ECHO_GYM = 'testgym'
TICKET = '22222222-2222-4222-8222-222222222222'
ROW_ID = 'calendar_row_99'
ASSET_ID = 'drive_asset_1'
KEY = 'swap-aimee-001'
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def approved_asset(**changes):
    row = make_asset(ASSET_ID, gym_id=ECHO_GYM)
    row.update(consent_member_ref=None, release_ref=None, consent_expires_at=None)
    row.update(changes)
    return row


def proof_receipt(**changes):
    value = {
        'schema_version': 1, 'key': KEY, 'action': 'swap_media',
        'gym_key': ECHO_GYM, 'ticket_id': TICKET, 'status': 'done',
        'created_at': '2026-09-23T11:25:00Z',
        'finished_at': '2026-09-23T11:30:00Z',
        'result': {'row_id': ROW_ID, 'postcondition_verified': True,
                   'swap_proof': {
                       'row_id': ROW_ID,
                       'before_image_sha256': sha('https://img/old.jpg'),
                       'after_image_sha256': sha('https://img/new.jpg'),
                       'caption_sha256': sha('Keep this caption'),
                       'before_asset_id': None, 'after_asset_id': ASSET_ID}},
    }
    value.update(changes)
    return value


class Reader:
    def __init__(self, *, asset=None, row=None, extra_assets=(), extra_book=(), fail=None):
        self.asset = approved_asset() if asset is None else asset
        self.row = row or {'id': ROW_ID, 'gym_id': ECHO_GYM, 'status': 'pending',
                           'caption': 'Keep this caption',
                           'image_url': 'https://img/new.jpg',
                           'source_media_asset_id': ASSET_ID}
        self.extra_assets = list(extra_assets)
        self.extra_book = list(extra_book)
        self.fail = fail
        self.calls = []

    def __call__(self, table, params):
        self.calls.append((table, dict(params)))
        if table == self.fail:
            raise RuntimeError('read failed')
        tables = {
            'echo_intake_tokens': [{'gym_id': PORTAL_GYM,
                                    'echo_account_key': ECHO_GYM}],
            'support_messages': [{'ticket_id': TICKET, 'direction': 'inbound',
                                  'created_at': '2026-09-23T11:00:00Z'}],
            'content_calendar': [self.row, *self.extra_book],
            'media_asset': [self.asset, *self.extra_assets],
        }
        rows = list(tables[table])
        for name, condition in params.items():
            if isinstance(condition, str) and condition.startswith('eq.'):
                rows = [row for row in rows if row.get(name) == condition[3:]]
            elif isinstance(condition, str) and condition.startswith('gt.'):
                rows = [row for row in rows if str(row.get(name) or '') > condition[3:]]
        if params.get('order') == 'id.asc':
            rows.sort(key=lambda row: row['id'])
        return [dict(row) for row in rows[:int(params.get('limit', 1000))]]


def observe(check_id, reader, *, receipt=None, params=None, ticket_id=TICKET):
    if params is None:
        params = {'reservation_key': KEY, 'row_id': ROW_ID}
    return be.observe(check_id, gym_key=PORTAL_GYM, request_key='r' * 64,
                      merged_sha='b' * 40, params=params,
                      deps={'read': reader,
                            'receipt_read': lambda key, gym: receipt or proof_receipt()},
                      ticket_id=ticket_id, now=NOW)


def test_completed_swap_requires_receipt_and_fresh_approved_drive_row():
    reader = Reader()
    result = observe('media_swap_completed', reader)
    assert result['outcome'] == be.VERIFIED
    assert result['symptom_resolved'] is True
    assert result['evidence'] == f'media_swap:{ROW_ID}:{ASSET_ID}'
    assert [table for table, _ in reader.calls] == [
        'echo_intake_tokens', 'echo_intake_tokens', 'support_messages',
        'content_calendar', 'media_asset']
    assert all(params.get('gym_id') == f'eq.{ECHO_GYM}'
               for table, params in reader.calls if table in ('content_calendar', 'media_asset'))


@pytest.mark.parametrize('patch,reason', [
    ({'ticket_id': '33333333-3333-4333-8333-333333333333'}, 'receipt_binding_mismatch'),
    ({'gym_key': 'othergym'}, 'receipt_binding_mismatch'),
    ({'status': 'unknown'}, 'receipt_not_done'),
    ({'result': {'row_id': ROW_ID, 'postcondition_verified': True}}, 'receipt_unproven'),
])
def test_completed_swap_rejects_unbound_or_unproved_receipts(patch, reason):
    result = observe('media_swap_completed', Reader(), receipt=proof_receipt(**patch))
    assert result['verified'] is False and result['reason'] == reason


@pytest.mark.parametrize('row_patch', [
    {'caption': 'Changed caption'}, {'image_url': 'https://img/third.jpg'},
    {'source_media_asset_id': 'other_asset'}, {'status': 'approved'},
])
def test_completed_swap_requires_unchanged_current_row(row_patch):
    row = {**Reader().row, **row_patch}
    result = observe('media_swap_completed', Reader(row=row))
    assert result['outcome'] == be.UNVERIFIED
    assert result['reason'] == 'swap_state_mismatch'


@pytest.mark.parametrize('asset_patch', [
    {'review_status': 'pending_review'}, {'moderation_status': 'pending'},
    {'consent_status': 'pending'}, {'review_content_hash': 'old-bytes'},
])
def test_completed_swap_requires_current_selector_approval(asset_patch):
    result = observe('media_swap_completed', Reader(asset=approved_asset(**asset_patch)))
    assert result['outcome'] == be.UNVERIFIED
    assert result['reason'] == 'asset_not_approved'


def test_completed_swap_read_failure_and_missing_ticket_identity_fail_closed():
    assert observe('media_swap_completed', Reader(fail='media_asset'))['outcome'] == be.UNKNOWN
    result = observe('media_swap_completed', Reader(), ticket_id='')
    assert result['outcome'] == be.UNKNOWN and result['reason'] == 'ticket_binding_missing'


def test_completed_swap_cannot_close_a_newer_inbound_complaint():
    receipt = proof_receipt(created_at='2026-09-23T10:00:00Z')
    result = observe('media_swap_completed', Reader(), receipt=receipt)
    assert result['outcome'] == be.UNVERIFIED
    assert result['reason'] == 'receipt_before_request'


def test_candidate_diagnostic_obeys_book_cooldown_and_cannot_close():
    reader = Reader()
    # The current row already owns this asset, so the book exclusion blocks it.
    unavailable = observe('media_swap_candidate_available', reader,
                          params={'min_count': 1})
    assert unavailable['outcome'] == be.UNVERIFIED
    fresh = approved_asset(id='drive_asset_2', used_count=0, last_used_at=None)
    fresh['moderation_json'] = {**fresh['moderation_json'], 'asset_id': 'drive_asset_2'}
    available = observe('media_swap_candidate_available',
                        Reader(extra_assets=[fresh]), params={'min_count': 1})
    assert available['outcome'] == be.VERIFIED
    assert available['symptom_resolved'] is None
    cooling = {**fresh, 'last_used_at': '2026-09-22T00:00:00Z'}
    blocked = observe('media_swap_candidate_available',
                      Reader(extra_assets=[cooling]), params={'min_count': 1})
    assert blocked['outcome'] == be.UNVERIFIED
    pending = {**fresh, 'review_status': 'pending_review'}
    blocked = observe('media_swap_candidate_available',
                      Reader(extra_assets=[pending]), params={'min_count': 1})
    assert blocked['outcome'] == be.UNVERIFIED
    cross_tenant = {**fresh, 'gym_id': 'othergym'}
    leaked = observe('media_swap_candidate_available',
                     Reader(extra_assets=[cross_tenant]), params={'min_count': 1})
    assert leaked['outcome'] == be.UNVERIFIED


def test_candidate_diagnostic_fails_closed_on_partial_page():
    class Partial(Reader):
        def __call__(self, table, params):
            if table == 'media_asset':
                return [approved_asset(id='same') for _ in range(100)]
            return super().__call__(table, params)

    result = observe('media_swap_candidate_available', Partial(), params={'min_count': 1})
    assert result['outcome'] == be.UNKNOWN
    assert result['reason'] == 'reader_partial'
