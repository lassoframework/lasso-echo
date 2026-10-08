"""Local narrow-role and actual-byte attester acceptance; no network/DB."""
import uuid
from types import SimpleNamespace

import pytest

from agent import forward_media_guard as guard


class Cursor:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, args=None):
        self.conn.calls.append((sql, args))
        self.result = ((self.conn.role,) if 'current_user' in sql else
                       (self.conn.snapshot,) if 'attestation_request' in sql else
                       (args[2],))
    def fetchone(self): return self.result


class Connection:
    def __init__(self, snapshot, role=guard.ROLE):
        self.snapshot, self.role, self.calls = snapshot, role, []
        self.committed = self.rolled_back = self.closed = False
    def cursor(self): return Cursor(self)
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


@pytest.fixture
def lane(monkeypatch):
    from agent import visual_writer_prepare
    monkeypatch.setattr(visual_writer_prepare, '_own_media_url', lambda u: u.startswith('https://owned.example/'))
    row_id = str(uuid.uuid4())
    snapshot = {'calendar_row_id': row_id, 'revision': 'revision',
                'source_url': 'https://owned.example/source',
                'image_url': 'https://owned.example/source', 'thumbnail_url': None}
    conn = Connection(snapshot)
    data = {'https://owned.example/source': b'actual original'}
    return row_id, conn, data


def run(lane, **kwargs):
    row_id, conn, data = lane
    return guard.attest(row_id, 'revision', connection_factory=lambda: conn,
                        read_bytes=data.__getitem__,
                        original_verifier=kwargs.pop('original_verifier', lambda snapshot,data: True), **kwargs)


def test_same_object_computes_hash_and_uses_narrow_rpc(lane):
    result = run(lane)
    assert result['fingerprints'] == ['md5:50c68002746cabbaf1bec7acc3b0dd6c']
    conn = lane[1]
    assert conn.committed and conn.closed
    sql, args = conn.calls[-1]
    assert 'fixer_attest_forward_media_20261006' in sql
    assert args[-2] == 'same_object'
    assert args[3:7] == ('md5:50c68002746cabbaf1bec7acc3b0dd6c', 15,
                        'md5:50c68002746cabbaf1bec7acc3b0dd6c', 15)


@pytest.mark.parametrize('role', ['postgres', 'service_role', 'authenticated'])
def test_broad_roles_rejected(lane, role):
    lane[1].role = role
    with pytest.raises(guard.ForwardMediaVerificationHold, match='role mismatch'): run(lane)
    assert lane[1].rolled_back and not lane[1].committed


def test_rehost_observes_same_bytes_without_fabricated_render(lane):
    lane[1].snapshot['image_url'] = 'https://owned.example/image'
    lane[2]['https://owned.example/image'] = lane[2]['https://owned.example/source']
    run(lane)
    assert lane[1].calls[-1][1][-2] == 'rehost'


def test_distinct_rendition_requires_actual_controlled_render(lane):
    lane[1].snapshot['image_url'] = 'https://owned.example/image'
    lane[2]['https://owned.example/image'] = b'rendered'
    with pytest.raises(guard.ForwardMediaVerificationHold, match='ancestry unavailable'): run(lane)


def test_actual_render_and_thumbnail_match_hosted_bytes(lane):
    lane[1].snapshot.update(image_url='https://owned.example/image', thumbnail_url='https://owned.example/thumb')
    lane[2].update({'https://owned.example/image': b'rendered', 'https://owned.example/thumb': b'poster'})
    def renderer(source, snapshot):
        assert source == b'actual original' and snapshot['revision'] == 'revision'
        return dict(image_bytes=b'rendered', thumbnail_bytes=b'poster', operation='render')
    result = run(lane, controlled_renderer=renderer)
    assert len(result['fingerprints']) == 3
    assert lane[1].committed


@pytest.mark.parametrize('result', [None, {}, {'image_bytes': b'forged', 'thumbnail_bytes': None, 'operation': 'render'}])
def test_unproven_render_rejected(lane, result):
    lane[1].snapshot['image_url'] = 'https://owned.example/image'
    lane[2]['https://owned.example/image'] = b'rendered'
    with pytest.raises(guard.ForwardMediaVerificationHold, match='differs from controlled render'):
        run(lane, controlled_renderer=lambda *args: result)
    assert not lane[1].committed


def test_changed_revision_holds_before_read(lane):
    lane[1].snapshot['revision'] = 'new revision'
    with pytest.raises(guard.ForwardMediaVerificationHold, match='revision changed'): run(lane)


def test_foreign_host_rejected(lane):
    lane[1].snapshot['source_url'] = 'https://unowned.example/object'
    with pytest.raises(guard.ForwardMediaVerificationHold, match='approved media host'): run(lane)


def test_object_overwrite_during_render_holds(lane):
    lane[1].snapshot['image_url'] = 'https://owned.example/image'
    lane[2]['https://owned.example/image'] = b'rendered'
    def renderer(*args):
        lane[2]['https://owned.example/image'] = b'overwritten'
        return dict(image_bytes=b'rendered', thumbnail_bytes=None, operation='render')
    with pytest.raises(guard.ForwardMediaVerificationHold, match='changed bytes'):
        run(lane, controlled_renderer=renderer)


def test_unconfigured_lane_defaults_off(monkeypatch):
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_GUARD', raising=False)
    assert guard.enabled() is False
    with pytest.raises(guard.ForwardMediaVerificationHold, match='not configured'): guard._connect()


@pytest.mark.parametrize('status,payload,error', [
    (200, True, None), (200, 'true', guard.ForwardMediaVerificationHold),
    (200, {}, guard.ForwardMediaVerificationHold),
    (503, {}, guard.ForwardMediaVerificationHold),
    (400, {'code': '23514', 'message': 'original source and delivered bytes must be owner attested'}, guard.ForwardMediaVerificationHold),
    (400, {'code': '23514', 'message': 'source or rendition already consumed by another tenant/date/group'}, guard.ForwardMediaDuplicateHold),
])
def test_claim_only_literal_true_and_distinct_duplicate(status, payload, error):
    response=SimpleNamespace(status_code=status,json=lambda:payload)
    store=SimpleNamespace(_client=lambda:SimpleNamespace(post=lambda *args,**kwargs:response),
                          _rest=lambda url:url, _headers=lambda headers:headers)
    args=(store,*[str(uuid.uuid4()) for _ in range(3)], 'outgoing-revision')
    if error:
        with pytest.raises(error): guard.claim(*args)
    else:
        assert guard.claim(*args) is True


@pytest.mark.parametrize('verifier', [None, lambda *args: False, lambda *args: 'true'])
def test_unknown_original_provenance_is_verification_hold(lane, verifier):
    with pytest.raises(guard.ForwardMediaVerificationHold, match='original asset provenance'):
        run(lane, original_verifier=verifier)
    assert not lane[1].committed


def test_production_lane_refuses_caller_callback_override(lane, monkeypatch):
    monkeypatch.setattr(guard, '_connect', lambda: lane[1])
    with pytest.raises(guard.ForwardMediaVerificationHold, match='callback override refused'):
        guard.attest(lane[0], 'revision', read_bytes=lane[2].__getitem__,
                     original_verifier=lambda *_: True)
    assert not lane[1].committed and lane[1].rolled_back


def test_production_lane_refuses_caller_byte_reader(lane, monkeypatch):
    monkeypatch.setattr(guard, '_connect', lambda: lane[1])
    with pytest.raises(guard.ForwardMediaVerificationHold, match='callback override refused'):
        guard.attest(lane[0], 'revision', read_bytes=lane[2].__getitem__)


def test_remote_reads_follow_read_transaction_end(lane):
    row_id, conn, data = lane
    def read(url):
        assert conn.rolled_back and not conn.committed
        assert not any('fixer_attest_forward_media' in call[0] for call in conn.calls)
        return data[url]
    guard.attest(row_id, 'revision', connection_factory=lambda: conn,
                 read_bytes=read, original_verifier=lambda *_: True)
    assert conn.committed and conn.closed


def test_claim_runs_visual_index_before_claim_rpc_when_armed(monkeypatch):
    from agent import forward_media_visual_index as visual_index
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', '1')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', '1')
    order = []
    def deny(row_id, revision, evidence_id, **kwargs):
        order.append('visual_index')
        raise guard.ForwardMediaVerificationHold('visual index held')
    monkeypatch.setattr(visual_index, 'before_claim', deny)
    posts = []
    store = SimpleNamespace(
        _client=lambda: SimpleNamespace(post=lambda *a, **k: posts.append((a, k))),
        _rest=lambda url: url, _headers=lambda headers: headers)
    with pytest.raises(guard.ForwardMediaVerificationHold, match='visual index held'):
        guard.claim(store, str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), 'rev')
    assert order == ['visual_index'] and posts == []


def test_claim_skips_visual_index_when_off(monkeypatch):
    from agent import forward_media_visual_index as visual_index
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', raising=False)
    monkeypatch.setattr(visual_index, 'attest',
                        lambda *a, **k: pytest.fail('visual index must stay off'))
    response = SimpleNamespace(status_code=200, json=lambda: True)
    store = SimpleNamespace(
        _client=lambda: SimpleNamespace(post=lambda *a, **k: response),
        _rest=lambda url: url, _headers=lambda headers: headers)
    assert guard.claim(store, str(uuid.uuid4()), str(uuid.uuid4()),
                       str(uuid.uuid4()), 'rev') is True
