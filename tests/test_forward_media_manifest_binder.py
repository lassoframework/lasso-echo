"""Offline tests for the default-OFF forward-media manifest binder lane."""
import uuid

import pytest

from agent import forward_media_manifest_binder as binder
from agent.portal_calendar_store import SupabaseCalendarStore


# ---- fakes ---------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHTTP:
    """Injectable PostgREST double recording every call."""

    def __init__(self, pages=None, rpc_status=200, rpc_payload=True):
        self.pages = list(pages or [])  # queued GET payloads per call
        self.rpc_status = rpc_status
        self.rpc_payload = rpc_payload
        self.gets = []
        self.posts = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append({'url': url, 'params': dict(params or {})})
        payload = self.pages.pop(0) if self.pages else []
        return FakeResponse(200, payload)

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({'url': url, 'json': json})
        return FakeResponse(self.rpc_status, self.rpc_payload)


def make_store(http):
    return SupabaseCalendarStore(url='https://supabase.test',
                                 service_key='test-key', http=http)


def make_row(tenant='gym-a', row_id=None, **overrides):
    row = {
        'id': row_id or str(uuid.uuid4()),
        'gym_id': tenant,
        'status': 'pending',
        'variant_status': 'active',
        'post_date': '2026-10-06',
        'visual_group_key': 'group-1',
        'source_media_asset_id': 'asset-1',
        'source_media_url': 'https://media.test/source.mp4',
        'image_url': 'https://media.test/image.jpg',
        'thumbnail_url': None,
        'render_manifest_digest': None,
        'publish_claim_token': None,
        'published_at': None,
        'late_post_id': None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    monkeypatch.setenv(binder.WORKER_ENV, 'true')
    monkeypatch.setenv(binder.TENANTS_ENV, 'gym-a,gym-b')
    return monkeypatch


# ---- gate ----------------------------------------------------------------

def test_lane_is_off_without_guard_and_worker(monkeypatch):
    for var in ('AGENT_FORWARD_MEDIA_GUARD', binder.WORKER_ENV, binder.TENANTS_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(binder.TENANTS_ENV, 'gym-a')
    assert not binder.binder_enabled()
    with pytest.raises(binder.ForwardMediaBinderHold) as exc:
        binder.run_once(store=make_store(FakeHTTP()))
    assert 'binder_lane_disabled' in str(exc.value)


def test_worker_flag_alone_is_not_enough(monkeypatch):
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_GUARD', raising=False)
    monkeypatch.setenv(binder.WORKER_ENV, 'true')
    monkeypatch.setenv(binder.TENANTS_ENV, 'gym-a')
    assert not binder.binder_enabled()


def test_missing_allowlist_is_a_static_hold(enabled):
    enabled.setenv(binder.TENANTS_ENV, '')
    with pytest.raises(binder.ForwardMediaBinderHold) as exc:
        binder.run_once(store=make_store(FakeHTTP()))
    assert 'explicit_tenant_allowlist_required' in str(exc.value)
    enabled.delenv(binder.TENANTS_ENV, raising=False)
    with pytest.raises(binder.ForwardMediaBinderHold):
        binder.run_once(store=make_store(FakeHTTP()))


def test_allowlist_rejects_bad_tenant_syntax(enabled):
    enabled.setenv(binder.TENANTS_ENV, 'gym-a,bad tenant!')
    with pytest.raises(binder.ForwardMediaBinderHold):
        binder.run_once(store=make_store(FakeHTTP()))


# ---- happy path ------------------------------------------------------------

def test_successful_bind_sends_only_persisted_row_id(enabled):
    rid = str(uuid.uuid4())
    http = FakeHTTP(pages=[[make_row('gym-a', rid)]])
    summary = binder.run_once(store=make_store(http))
    assert summary['bound'] == 1 and summary['held'] == 0
    assert len(http.posts) == 1
    call = http.posts[0]
    assert call['url'].endswith(
        '/rest/v1/rpc/fixer_bind_forward_media_manifest_20261006')
    # The ONLY key the caller may send; no digest/hash/URL/token from here.
    assert call['json'] == {'p_calendar_row_id': rid}
    # Discovery is keyset over unsent active rows with null digest.
    params = http.gets[0]['params']
    assert params['render_manifest_digest'] == 'is.null'
    assert params['variant_status'] == 'eq.active'
    assert params['gym_id'] == 'eq.gym-a'
    assert params['order'] == 'id'


def test_rpc_refusal_is_a_visible_hold_without_writes(enabled):
    rid = str(uuid.uuid4())
    http = FakeHTTP(pages=[[make_row('gym-a', rid)]],
                    rpc_status=409, rpc_payload={'message': 'held'})
    summary = binder.run_once(store=make_store(http))
    assert summary['bound'] == 0 and summary['held'] == 1
    # Refusal is a hold, not an exception that kills the lane.
    assert summary['attempted'] == 1


def test_rpc_false_and_transport_errors_are_holds(enabled):
    rid1, rid2 = str(uuid.uuid4()), str(uuid.uuid4())
    http = FakeHTTP(pages=[[make_row('gym-a', rid1)]], rpc_payload=False)
    assert binder.run_once(store=make_store(http))['held'] == 1

    class BoomHTTP(FakeHTTP):
        def post(self, *a, **k):
            raise ConnectionError('offline')
    http2 = BoomHTTP(pages=[[make_row('gym-a', rid2)]])
    assert binder.run_once(store=make_store(http2))['held'] == 1


# ---- pagination / fairness -------------------------------------------------

def test_keyset_pagination_and_tenant_fairness(enabled):
    a1, a2 = sorted(str(uuid.uuid4()) for _ in range(2))
    b1 = str(uuid.uuid4())
    # First GET (gym-a page 1) returns a1; page 2 (cursor>a1) returns a2, then
    # empty ends tenant A; gym-b page returns b1.
    enabled.setenv('AGENT_FORWARD_MEDIA_BINDER_BATCH_SIZE', '10')
    http = FakeHTTP(pages=[
        [make_row('gym-a', a1)],
        [make_row('gym-a', a2)],
        [],
        [make_row('gym-b', b1)],
        [],
    ])
    summary = binder.run_once(store=make_store(http))
    assert summary['bound'] == 3
    # Keyset cursor was applied on the second gym-a page.
    assert http.gets[1]['params'].get('id') == f'gt.{a1}'
    # Both tenants were scanned (round-robin order over the allowlist).
    gyms = [g['params']['gym_id'] for g in http.gets]
    assert 'eq.gym-b' in gyms and 'eq.gym-a' in gyms


def test_batch_limit_keeps_cursor_at_last_processed_row(enabled):
    enabled.setenv(binder.TENANTS_ENV, 'gym-a')
    enabled.setenv('AGENT_FORWARD_MEDIA_BINDER_BATCH_SIZE', '1')
    first, second = sorted(str(uuid.uuid4()) for _ in range(2))
    http = FakeHTTP(pages=[[make_row('gym-a', first), make_row('gym-a', second)]])
    cursors = {}
    assert binder.run_once(store=make_store(http), cursors=cursors)['bound'] == 1
    assert cursors['gym-a'] == first
    assert [call['json']['p_calendar_row_id'] for call in http.posts] == [first]


def test_held_rows_are_not_retried_and_do_not_starve_later_rows(enabled):
    held_id, ok_id = sorted(str(uuid.uuid4()) for _ in range(2))
    rows = [make_row('gym-a', held_id), make_row('gym-a', ok_id)]

    class RefuseFirstHTTP(FakeHTTP):
        def post(self, url, headers=None, json=None, timeout=None):
            self.posts.append({'url': url, 'json': json})
            if json['p_calendar_row_id'] == held_id:
                return FakeResponse(409, {'message': 'held'})
            return FakeResponse(200, True)

    http = RefuseFirstHTTP(pages=[rows, []])
    held = {}
    summary = binder.run_once(store=make_store(http), held=held)
    assert summary['bound'] == 1 and summary['held'] == 1
    assert held[held_id] == 'rpc_bind_refused'
    # Second pass: held row is skipped without another RPC call.
    posts_before = len(http.posts)
    http.pages = [rows, []]
    summary2 = binder.run_once(store=make_store(http), held=held)
    assert len(http.posts) == posts_before + 1  # only the good row re-attempted
    assert summary2['attempted'] <= 1


def test_refused_row_retries_after_owner_authority_arrives(enabled):
    enabled.setenv(binder.TENANTS_ENV, 'gym-a')
    rid = str(uuid.uuid4())
    row = make_row('gym-a', rid)

    class RecoveringHTTP(FakeHTTP):
        def post(self, url, headers=None, json=None, timeout=None):
            self.posts.append({'url': url, 'json': json})
            return FakeResponse(409 if len(self.posts) == 1 else 200,
                                False if len(self.posts) == 1 else True)

    http = RecoveringHTTP(pages=[[row], [], [row], []])
    cursors = {}
    first = binder.run_once(store=make_store(http), cursors=cursors, held={})
    second = binder.run_once(store=make_store(http), cursors=cursors, held={})
    assert first['held'] == 1
    assert second['bound'] == 1
    assert len(http.posts) == 2


def test_malformed_discovery_is_static_hold(enabled):
    http = FakeHTTP(pages=[[None]])
    with pytest.raises(binder.ForwardMediaBinderHold,
                       match='candidate_discovery_malformed'):
        binder.run_once(store=make_store(http))
    http = FakeHTTP(pages=[[make_row('gym-b')]])
    with pytest.raises(binder.ForwardMediaBinderHold,
                       match='candidate_discovery_malformed'):
        binder.run_once(store=make_store(http))


def test_incomplete_identity_never_reaches_rpc(enabled):
    bad = make_row('gym-a', str(uuid.uuid4()), source_media_url='http://insecure')
    http = FakeHTTP(pages=[[bad], []])
    summary = binder.run_once(store=make_store(http))
    assert summary['held'] == 1 and summary['attempted'] == 0
    assert http.posts == []


# ---- caller-credential / digest hygiene ------------------------------------

def test_no_digest_or_owner_credentials_anywhere(enabled, monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_ATTESTER_DSN',
                       'postgres://attester@db/x')
    rid = str(uuid.uuid4())
    http = FakeHTTP(pages=[[make_row('gym-a', rid, render_manifest_digest=None)]])
    binder.run_once(store=make_store(http))
    payload = http.posts[0]['json']
    assert set(payload) == {'p_calendar_row_id'}
    assert uuid.UUID(payload['p_calendar_row_id'])
    for forbidden in ('digest', 'hash', 'url', 'token', 'fingerprint',
                      'dsn', 'key', 'secret'):
        assert not any(forbidden in k.lower() for k in payload)


def test_module_import_has_no_side_effects(monkeypatch):
    # Importing must not require flags, allowlist or a store.
    for var in ('AGENT_FORWARD_MEDIA_GUARD', binder.WORKER_ENV, binder.TENANTS_ENV):
        monkeypatch.delenv(var, raising=False)
    import importlib
    importlib.reload(binder)
    assert not binder.binder_enabled()


# ---- staged discovery (DRAFT worker discovery 20261008) --------------------

def staged_row(tenant='gym-a'):
    return {'calendar_row_id': str(uuid.uuid4()), 'batch_id': str(uuid.uuid4()),
            'tenant_id': tenant, 'manifest_digest': 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex}


def test_staged_discovery_is_off_without_lane(enabled, monkeypatch):
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_GUARD', raising=False)
    with pytest.raises(binder.ForwardMediaBinderHold) as exc:
        binder.staged_candidates(make_store(FakeHTTP()))
    assert 'binder_lane_disabled' in str(exc.value)


def test_staged_discovery_sends_only_allowlist_and_bound(enabled):
    http = FakeHTTP(rpc_payload=[staged_row()])
    rows = binder.staged_candidates(make_store(http))
    assert len(rows) == 1
    call = http.posts[0]
    assert call['url'].endswith(f"rpc/{binder.STAGED_PENDING_RPC}")
    assert set(call['json']) == {'p_tenants', 'p_limit'}
    assert call['json']['p_tenants'] == ['gym-a', 'gym-b']
    assert isinstance(call['json']['p_limit'], int)
    # Discovery never binds: the active-only bind RPC is never called.
    assert all(binder.RPC_NAME not in p['url'] for p in http.posts)


def test_staged_discovery_rejects_wrong_tenant_and_bad_digest(enabled):
    with pytest.raises(binder.ForwardMediaBinderHold) as exc:
        binder.staged_candidates(make_store(FakeHTTP(rpc_payload=[staged_row('foreign')])))
    assert 'candidate_discovery_malformed' in str(exc.value)
    bad = staged_row()
    bad['manifest_digest'] = 'sha256:not-hex'
    with pytest.raises(binder.ForwardMediaBinderHold):
        binder.staged_candidates(make_store(FakeHTTP(rpc_payload=[bad])))
    broken = staged_row()
    broken['batch_id'] = 'not-a-uuid'
    with pytest.raises(binder.ForwardMediaBinderHold):
        binder.staged_candidates(make_store(FakeHTTP(rpc_payload=[broken])))


def test_staged_discovery_transport_and_status_failures_are_holds(enabled):
    with pytest.raises(binder.ForwardMediaBinderHold) as exc:
        binder.staged_candidates(make_store(FakeHTTP(rpc_status=500, rpc_payload=[])))
    assert 'candidate_discovery_unavailable' in str(exc.value)
