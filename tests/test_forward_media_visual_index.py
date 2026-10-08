"""Actual-byte visual index attester acceptance; fake bytes and DB, no network."""
import hashlib
import io
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image

from agent import forward_media_guard as guard
from agent import forward_media_visual_index as index
from agent import visual_scene

REVISION = '0123456789abcdeffedcba9876543210'
ROW_REVISION = int(REVISION[:15], 16)
LINEAGE = str(uuid.uuid4())
RECEIPTS = (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))


def image_bytes(color):
    buf = io.BytesIO()
    Image.new('RGB', (64, 64), color).save(buf, 'PNG')
    return buf.getvalue()


ORIGINAL = image_bytes((120, 40, 200))
DELIVERED = image_bytes((10, 200, 90))
THUMBNAIL = image_bytes((250, 250, 30))


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, args=None):
        self.conn.calls.append((sql, args))
        if 'current_user' in sql:
            self.result = (self.conn.role,)
        elif 'attestation_request' in sql:
            self.result = (self.conn.snapshot,)
        elif 'fixer_forward_visual_receipts' in sql:
            self.result = (RECEIPTS[0], RECEIPTS[1], RECEIPTS[1] if self.conn.snapshot.get('thumbnail_url') is None else RECEIPTS[2])
        elif 'forward_media_visual_attestation' in sql:
            self.conn.inserted.append(args)
            self.result = (uuid.uuid4(),)
        elif 'fixer_forward_visual_negative_append' in sql:
            self.conn.negatives.append(args)
            self.result = None
        else:
            raise AssertionError('unexpected SQL: ' + sql)

    def fetchone(self):
        return self.result


class Connection:
    def __init__(self, snapshot, role=index.ROLE):
        self.snapshot, self.role = snapshot, role
        self.calls, self.inserted, self.negatives = [], [], []
        self.committed = self.rolled_back = self.closed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', '1')
    from agent import visual_writer_prepare
    monkeypatch.setattr(visual_writer_prepare, '_own_media_url',
                        lambda url: url.startswith('https://owned.example/'))
    row_id = str(uuid.uuid4())
    snapshot = {'calendar_row_id': row_id, 'revision': REVISION,
                'tenant_id': 'tenant-1', 'gym_id': 'gym-1',
                'post_date': '2026-10-07',
                'source_url': 'https://owned.example/original',
                'image_url': 'https://owned.example/delivered',
                'thumbnail_url': 'https://owned.example/thumbnail'}
    conn = Connection(snapshot)
    data = {'https://owned.example/original': ORIGINAL,
            'https://owned.example/delivered': DELIVERED,
            'https://owned.example/thumbnail': THUMBNAIL}
    return row_id, conn, data


def run(lane, **kwargs):
    row_id, conn, data = lane
    return index.attest(row_id, REVISION, LINEAGE,
                        connection_factory=lambda: conn,
                        read_bytes=data.get, **kwargs)


def expected_phash(data):
    value = int(visual_scene.scene_fingerprint(data).rsplit(':', 1)[-1], 16)
    return value - (1 << 64) if value >= (1 << 63) else value


def test_actual_bytes_produce_expected_sha256_md5_length_phash(lane):
    result = run(lane)
    conn = lane[1]
    assert conn.committed and conn.closed
    assert result['row_revision'] == ROW_REVISION
    assert result['tenant_key'] == 'tenant-1'
    assert set(result['attestation_ids']) == set(index.ROLES)
    expected = {'original': ORIGINAL, 'delivered': DELIVERED, 'thumbnail': THUMBNAIL}
    urls = {'original': 'https://owned.example/original',
            'delivered': 'https://owned.example/delivered',
            'thumbnail': 'https://owned.example/thumbnail'}
    by_role = {args[2]: args for args in conn.inserted}
    assert set(by_role) == set(index.ROLES)
    for role, data in expected.items():
        args = by_role[role]
        assert args[0] == 'tenant-1' and args[1] == urls[role]
        assert args[3] == hashlib.sha256(data).hexdigest()
        assert args[4] == hashlib.md5(data).hexdigest()
        assert args[5] == len(data)
        assert args[6] == 1 and args[7] == expected_phash(data)
        assert args[8] == ROW_REVISION and args[9] == LINEAGE
        assert args[10] == RECEIPTS[index.ROLES.index(role)]
    assert not conn.negatives


def test_snapshot_read_transaction_ends_before_fetches(lane):
    row_id, conn, data = lane
    def read(url):
        assert conn.rolled_back and not conn.committed
        assert not any('forward_media_visual_attestation' in call[0] for call in conn.calls)
        return data[url]
    index.attest(row_id, REVISION, LINEAGE, connection_factory=lambda: conn,
                 read_bytes=read)
    assert conn.committed


def test_revision_change_holds_before_any_fetch(lane):
    lane[1].snapshot['revision'] = 'f' * 32
    with pytest.raises(guard.ForwardMediaVerificationHold, match='revision changed'):
        run(lane)


@pytest.mark.parametrize('field', ['source_url', 'image_url', 'thumbnail_url'])
def test_incomplete_evidence_holds_without_blacklisting_other_roles(lane, field):
    lane[1].snapshot[field] = ['invalid'] if field == 'thumbnail_url' else ''
    with pytest.raises(guard.ForwardMediaVerificationHold, match='requires original'):
        run(lane)
    conn = lane[1]
    assert not conn.inserted
    assert not conn.negatives and not conn.committed


def test_spoofed_url_outside_host_holds_without_blacklisting_other_roles(lane):
    lane[1].snapshot['image_url'] = 'https://unowned.example/delivered'
    with pytest.raises(guard.ForwardMediaVerificationHold, match='object evidence unavailable'):
        run(lane)
    conn = lane[1]
    assert not conn.inserted
    assert not conn.negatives and not conn.committed


def test_undecodable_bytes_hold_and_record_negative(lane):
    lane[2]['https://owned.example/delivered'] = b'not an image'
    with pytest.raises(guard.ForwardMediaVerificationHold, match='undecodable'):
        run(lane)
    conn = lane[1]
    assert not conn.inserted
    assert any(args[4] == 'undecodable media bytes'
               and args[2] == hashlib.sha256(b'not an image').hexdigest()
               and args[3] is None for args in conn.negatives)


@pytest.mark.parametrize('failure', ['missing_fingerprint', 'hash_exception'])
def test_hash_runtime_failure_holds_without_blacklisting_valid_bytes(lane, monkeypatch, failure):
    if failure == 'missing_fingerprint':
        monkeypatch.setattr(index, 'phash_v1', lambda _: None)
    else:
        from agent import vision
        monkeypatch.setattr(vision, 'dct_phash', lambda _: (_ for _ in ()).throw(RuntimeError('hash runtime failure')))
    with pytest.raises(guard.ForwardMediaVerificationHold, match='fingerprint unavailable'):
        run(lane)
    assert not lane[1].negatives and not lane[1].inserted


def test_decode_runtime_failure_is_not_negative_evidence(lane, monkeypatch):
    monkeypatch.setattr(index, 'phash_v1', lambda _: None)
    monkeypatch.setattr(Image, 'open', lambda _: (_ for _ in ()).throw(MemoryError('decoder unavailable')))
    with pytest.raises(guard.ForwardMediaVerificationHold, match='fingerprint unavailable'):
        run(lane)
    assert not lane[1].negatives and not lane[1].inserted


def test_contradictory_tenant_holds_without_blacklisting_valid_media(lane):
    with pytest.raises(guard.ForwardMediaVerificationHold, match='contradicts'):
        run(lane, tenant_key='other-tenant')
    assert not lane[1].negatives and not lane[1].committed


@pytest.mark.parametrize('field', ['source_url', 'image_url', 'thumbnail_url'])
@pytest.mark.parametrize('on_recheck', [False, True])
def test_object_read_failure_holds_without_blacklisting_successful_role(lane, field, on_recheck):
    row_id, conn, data = lane
    reads = {}
    failed_url = conn.snapshot[field]
    def read(url):
        reads[url] = reads.get(url, 0) + 1
        if url == failed_url and reads[url] == (2 if on_recheck else 1):
            raise OSError('temporary storage outage')
        return data[url]
    with pytest.raises(guard.ForwardMediaVerificationHold):
        index.attest(row_id, REVISION, LINEAGE, connection_factory=lambda: conn, read_bytes=read)
    assert not conn.negatives and not conn.inserted and not conn.committed


@pytest.mark.parametrize('field', ['source_url', 'image_url', 'thumbnail_url'])
def test_changed_object_holds_without_blacklisting_either_valid_sample(lane, field):
    row_id, conn, data = lane
    reads = {}
    changed_url = conn.snapshot[field]
    def read(url):
        reads[url] = reads.get(url, 0) + 1
        if url == changed_url and reads[url] == 2:
            return image_bytes((25, 50, 75))
        return data[url]
    with pytest.raises(guard.ForwardMediaVerificationHold, match='changed bytes'):
        index.attest(row_id, REVISION, LINEAGE, connection_factory=lambda: conn, read_bytes=read)
    assert not conn.negatives and not conn.inserted and not conn.committed


@pytest.mark.parametrize('stage', ['snapshot', 'receipts', 'insert', 'commit'])
def test_database_failure_holds_without_blacklisting_observed_media(lane, monkeypatch, stage):
    conn = lane[1]
    original_execute = Cursor.execute
    def execute(cur, sql, args=None):
        if ((stage == 'snapshot' and 'attestation_request' in sql)
                or (stage == 'receipts' and 'fixer_forward_visual_receipts' in sql)
                or (stage == 'insert' and 'forward_media_visual_attestation' in sql)):
            raise RuntimeError('database unavailable')
        return original_execute(cur, sql, args)
    monkeypatch.setattr(Cursor, 'execute', execute)
    if stage == 'commit':
        monkeypatch.setattr(conn, 'commit', lambda: (_ for _ in ()).throw(RuntimeError('commit lost')))
    with pytest.raises(guard.ForwardMediaVerificationHold, match='transaction failed'):
        run(lane)
    assert not conn.negatives and conn.rolled_back and conn.closed


def test_database_connect_failure_is_hold_without_object_reads(lane):
    def connect():
        raise RuntimeError('database unavailable')
    with pytest.raises(guard.ForwardMediaVerificationHold, match='database unavailable'):
        index.attest(lane[0], REVISION, LINEAGE, connection_factory=connect,
                     read_bytes=lambda _: pytest.fail('connection failure must not fetch media'))
    assert not lane[1].negatives


def test_attributable_bad_bytes_negative_failure_preserves_hold(lane, monkeypatch):
    lane[2]['https://owned.example/delivered'] = b'not an image'
    original_execute = Cursor.execute
    def execute(cur, sql, args=None):
        if 'fixer_forward_visual_negative_append' in sql:
            raise RuntimeError('negative append unavailable')
        return original_execute(cur, sql, args)
    monkeypatch.setattr(Cursor, 'execute', execute)
    with pytest.raises(guard.ForwardMediaVerificationHold, match='undecodable'):
        run(lane)
    assert not lane[1].inserted and not lane[1].committed


@pytest.mark.parametrize('role', ['postgres', 'service_role', 'authenticated'])
def test_broad_roles_rejected(lane, role):
    lane[1].role = role
    with pytest.raises(guard.ForwardMediaVerificationHold, match='role mismatch'):
        run(lane)
    assert not lane[1].committed and not lane[1].inserted


def _claim_store(payload=True, status=200, calls=None):
    def post(*args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        return SimpleNamespace(status_code=status, json=lambda: payload)
    return SimpleNamespace(_client=lambda: SimpleNamespace(post=post),
                           _rest=lambda url: url, _headers=lambda headers: headers)


def test_off_mode_pass_through_no_visual_index_work(lane, monkeypatch):
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', raising=False)
    assert index.enabled() is False
    assert index.before_claim(lane[0], REVISION, LINEAGE) is None
    calls = []
    store = _claim_store(calls=calls)
    assert guard.claim(store, lane[0], str(uuid.uuid4()), str(uuid.uuid4()), REVISION) is True
    assert len(calls) == 1 and 'fixer_claim_forward_media_20261006' in calls[0][0][0]


def test_armed_claim_reads_persisted_proof_without_attester_credentials(lane, monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', '1')
    monkeypatch.setattr(index, '_connect', lambda: pytest.fail('publisher must never open attester connection'))
    ids = [str(uuid.uuid4()) for _ in range(3)]
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        payload = {'attestation_ids': ids} if 'visual_proof' in url else True
        return SimpleNamespace(status_code=200, json=lambda: payload)
    store = SimpleNamespace(_client=lambda: SimpleNamespace(post=post),
                            _rest=lambda u:u, _headers=lambda h:h)
    assert guard.claim(store, lane[0], str(uuid.uuid4()), LINEAGE, REVISION) is True
    assert [c[0] for c in calls] == ['rpc/fixer_forward_visual_proof_20261008', 'rpc/fixer_forward_visual_index_claim_20261008']
    assert calls[1][1]['json']['p_attestation_ids'] == ids
    assert not lane[1].inserted


def test_armed_missing_proof_blocks_provider(monkeypatch, lane):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', '1')
    calls, provider = [], []
    store = _claim_store(payload={'code': '23514'}, status=400, calls=calls)
    with pytest.raises(guard.ForwardMediaVerificationHold):
        guard.claim(store, lane[0], str(uuid.uuid4()), LINEAGE, REVISION)
        provider.append('send')
    assert len(calls)==1 and provider==[]


def test_null_thumbnail_reuses_trusted_delivered_receipt(lane):
    lane[1].snapshot['thumbnail_url'] = None
    result = run(lane)
    assert len(result['attestation_ids']) == 3
    assert lane[1].inserted[-1][1] == lane[1].snapshot['image_url']


def test_aliased_shared_url_fetched_once_with_identical_bytes(lane):
    """thumbnail_url NULL aliases image_url; a reader that mutates bytes
    between per-role fetches must not produce inconsistent attestations."""
    row_id, conn, data = lane
    conn.snapshot['thumbnail_url'] = None
    shared = conn.snapshot['image_url']
    swapped = image_bytes((25, 50, 75))
    reads = {}
    def read(url):
        reads[url] = reads.get(url, 0) + 1
        # Simulate the object changing between earlier per-role fetches: any
        # read past the initial fetch and verification reread (which only
        # happens if aliased roles re-fetch) returns different bytes.
        if url == shared and reads[url] not in (1, 2):
            return swapped
        return data[url]
    result = index.attest(row_id, REVISION, LINEAGE,
                          connection_factory=lambda: conn, read_bytes=read)
    assert set(result['attestation_ids']) == set(index.ROLES)
    # Exactly one initial read plus one verification reread for the shared URL.
    assert reads[shared] == 2
    by_role = {args[2]: args for args in conn.inserted}
    delivered, thumbnail = by_role['delivered'], by_role['thumbnail']
    # Both aliased roles attest the identical observed bytes.
    assert delivered[1] == thumbnail[1] == shared
    assert delivered[3] == thumbnail[3]
    assert delivered[4] == thumbnail[4]
    assert delivered[5] == thumbnail[5]
    assert delivered[7] == thumbnail[7]
    # No stale or swapped bytes attested for any role.
    for role, blob in (('original', ORIGINAL), ('delivered', DELIVERED)):
        assert by_role[role][3] == hashlib.sha256(blob).hexdigest()
    assert conn.committed and not conn.negatives


def test_aliased_url_mutation_between_old_role_reads_cannot_attest(lane):
    """Old per-role fetching would accept delivered-old/thumbnail-new bytes
    because its final reread only compared the latter URL-keyed observation."""
    row_id, conn, data = lane
    conn.snapshot['thumbnail_url'] = None
    shared = conn.snapshot['image_url']
    changed = image_bytes((9, 19, 29))
    reads = {}
    def read(url):
        reads[url] = reads.get(url, 0) + 1
        return data[url] if url != shared or reads[url] == 1 else changed
    with pytest.raises(guard.ForwardMediaVerificationHold, match='changed bytes'):
        index.attest(row_id, REVISION, LINEAGE,
                     connection_factory=lambda: conn, read_bytes=read)
    assert reads[shared] == 2
    assert not conn.inserted


def test_distinct_urls_still_reread_and_hold_on_mutation(lane):
    """Unshared URLs keep one initial read plus one reread each; a real
    mutation between fetch and reread still holds."""
    row_id, conn, data = lane
    reads = {}
    def read(url):
        reads[url] = reads.get(url, 0) + 1
        return data[url]
    run_attest = lambda: index.attest(row_id, REVISION, LINEAGE,
                                      connection_factory=lambda: conn, read_bytes=read)
    run_attest()
    assert all(count == 2 for count in reads.values())
    changed = image_bytes((1, 2, 3))
    def mutating(url):
        reads[url] = reads.get(url, 0) + 1
        return changed if reads[url] % 2 == 0 else data[url]
    conn.inserted.clear()
    with pytest.raises(guard.ForwardMediaVerificationHold, match='changed bytes'):
        index.attest(row_id, REVISION, LINEAGE,
                     connection_factory=lambda: conn, read_bytes=mutating)
    assert not conn.inserted


# Reuse existing fake-vendor fixtures while invoking the actual Meta,
# SocialAPI and Zernio publisher wrappers and actual calendar bridge.
from test_forward_media_lower_publishers import lane as publisher_lane


@pytest.mark.parametrize('base_flag', [None, '', 'false', '0', 'off'])
@pytest.mark.parametrize('provider', ['meta', 'socialapi', 'zernio'])
def test_visual_only_flag_holds_real_bridge_and_provider_wrappers(publisher_lane, monkeypatch, base_flag, provider):
    from agent import forward_media_publish as bridge, forward_media_send_context as scope
    row, token, authority, draft, account, vendor, publish, requests = publisher_lane(provider)
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', '1')
    if base_flag is None: monkeypatch.delenv('AGENT_FORWARD_MEDIA_GUARD', raising=False)
    else: monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', base_flag)
    assert guard.enabled() is True
    with pytest.raises(guard.ForwardMediaVerificationHold, match='requires forward media guard configuration'):
        bridge.authorize(authority, row, token)
    with pytest.raises(scope.ProviderSendHold):
        with bridge.authorized_send(authority, row, token):
            publish()
    with pytest.raises(scope.ProviderSendHold, match='scope required'):
        publish()
    assert requests == []
    assert not any('claim_forward_media' in rpc or 'visual_index_claim' in rpc for rpc in authority.calls)


@pytest.mark.parametrize('provider', ['meta', 'socialapi', 'zernio'])
def test_both_flags_off_preserves_real_provider_behavior(publisher_lane, monkeypatch, provider):
    from agent import forward_media_publish as bridge
    row, token, authority, draft, account, vendor, publish, requests = publisher_lane(provider)
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', raising=False)
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_GUARD', raising=False)
    assert guard.enabled() is False and bridge.authorize(None, {}, None) is True
    assert publish().ok is True
    assert requests and authority.calls == []
