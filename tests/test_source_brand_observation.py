"""Synthetic tests only; no network, activation, credentials or production SQL."""
import base64
import copy
import hashlib
import json
import traceback
from datetime import datetime, timedelta, timezone

import pytest

from agent.source_brand_ingest import VerifiedMapping
from agent.source_brand_observation import (AstraSemanticAssessor, ObservationHold,
    SourceObservationStore, TrustedSourceObservationProducer, VALIDATOR_REVISION,
    MAX_EVIDENCE_BYTES)

GYM = '4893c289-eaa5-416f-b2e2-bcdd83c1cba1'
WEB = ' https://gym.example/'.strip()
SOCIAL = 'https://www.instagram.com/mapped/'
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
ENV = {'ECHO_SOURCE_OBSERVATION_ENABLED': 'true',
       'ECHO_SOURCE_OBSERVATION_OPENAI_API_KEY': 'synthetic-assessor-secret'}
RAW = b'New text. Train here. :root{--a:#112233;--b:#445566}'


def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(x):
    return hashlib.sha256(x).hexdigest()


def mapping():
    return VerifiedMapping(GYM, 'gym_exact', 'mapping-1', {'receipt': 'mapping'},
                           (WEB,), (SOCIAL,), '123')


def capture(kind, raw, id):
    return dict(id=id, gym_id=GYM, echo_account_key='gym_exact', source_kind=kind,
        source_url=WEB if kind == 'website' else 'https://api.apify.com/v2/datasets/current/items',
        source_locator=None if kind == 'website' else SOCIAL,
        provider_account_id=None if kind == 'website' else '123',
        capture_provider='direct' if kind == 'website' else 'apify',
        provider_response_id=None if kind == 'website' else 'dataset-current',
        source_revision='rev-current', mapping_revision='mapping-1', mapping_evidence={'receipt': 'mapping'},
        fetched_at=(NOW - timedelta(hours=1)).isoformat(), captured_at=NOW.isoformat(),
        bytes_sha256=sha(raw), raw_bytes='\\x' + raw.hex())


def snapshot(rows):
    cs = [{**{k: v for k, v in c.items() if k not in ('raw_bytes', 'captured_at')},
           'bytes_base64': base64.b64encode(bytes.fromhex(c['raw_bytes'][2:])).decode()} for c in rows]
    c = rows[0]; raw = bytes.fromhex(c['raw_bytes'][2:])
    return dict(schema_version=1, gym_id=GYM, echo_account_key='gym_exact',
        fact_policy='delegated_supported_facts', captures=cs,
        selected_facts=[dict(key='offer', capture_id=c['id'], bytes_sha256=c['bytes_sha256'],
            source_locator=WEB, byte_offset=raw.index(b'Train here'), byte_length=10, text='Train here')],
        palette=dict(capture_id=c['id'], bytes_sha256=c['bytes_sha256'], primary='#112233',
            secondary='#445566', primary_byte_offset=raw.index(b'#112233'), secondary_byte_offset=raw.index(b'#445566')))


class Store:
    def __init__(self):
        self.rows = [capture('website', RAW, '051a189e-5f91-44f4-bf89-a1549f7b9442'),
                     capture('social', b'{"caption":"Our gym is open", "ownerId":"123"}', 'b4d4d171-fd4d-4393-8602-bc99960b05cd')]
        old = snapshot(self.rows)
        raw = canonical(old)
        b = dict(id='1bbcd64f-362d-4b6d-8bc5-b34f9b113d41', gym_id=GYM, echo_account_key='gym_exact',
                 version=1, capture_ids=[c['id'] for c in self.rows], snapshot_bytes=raw,
                 content_sha256=sha(raw.encode()), created_at=(NOW-timedelta(days=20)).isoformat())
        r = dict(id=1, gym_id=GYM, bundle_id=b['id'], bundle_version=1, content_sha256=b['content_sha256'],
                 request_id='2cd99b15-ab2b-4550-8b5f-fd10f7321b96', purpose='echo_source_brand_configuration',
                 action='approve', actor_authority='blake', actor_clerk_user_id='server-user',
                 created_at=(NOW-timedelta(days=19)).isoformat())
        self.config = dict(bundle=b, approval_receipt=r)
        self.calls, self.observation = [], None
        self.active_mutation = lambda x: x
        self.latest_mutation = lambda x: x

    def latest(self, gym, prior):
        assert gym == GYM
        c = next(c for c in self.rows if c['source_kind'] == prior['source_kind'])
        return self.latest_mutation(copy.deepcopy(c))

    def rpc(self, name, params):
        self.calls.append((name, copy.deepcopy(params)))
        if name == 'echo_source_brand_configuration':
            return copy.deepcopy(self.config)
        if name == 'echo_source_brand_revalidate':
            positive = params['p_validation_report']['selected_facts_status'] == 'supported_uncontradicted'
            raw = canonical(snapshot(self.rows)) if positive else self.config['bundle']['snapshot_bytes']
            self.observation = dict(id=1, gym_id=GYM, bundle_id=self.config['bundle']['id'],
                configuration_sha256=self.config['bundle']['content_sha256'], snapshot_bytes=raw,
                content_sha256=sha(raw.encode()), validator_revision=params['p_validator_revision'],
                validation_report=params['p_validation_report'], created_at=NOW.isoformat())
            return copy.deepcopy(self.observation)
        if self.observation['validation_report']['selected_facts_status'] != 'supported_uncontradicted':
            return self.active_mutation(None)
        return self.active_mutation(dict(**copy.deepcopy(self.config),
            observation=copy.deepcopy(self.observation), fact_validation='supported_uncontradicted',
            fact_approval_mode='delegated_policy'))


class Model:
    def __init__(self, status='supported_uncontradicted'):
        self.status = status
        self.calls = []
        self.mutate = lambda x: x
        self.response_mutate = lambda x: x

    def __call__(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        assert url == 'https://api.openai.com/v1/responses'
        assert headers['Authorization'] == 'Bearer synthetic-assessor-secret'
        evidence = json.loads(payload['input'][1]['content'])
        fact = evidence['selected_facts'][0]
        witness = fact['witness']
        citation = dict(capture_id=witness['capture_id'], byte_offset=witness['byte_offset'],
                        byte_length=witness['byte_length'], text=fact['text']) if witness else None
        verdict = dict(evidence_sha256=evidence['evidence_sha256'],
            inspected_capture_ids=[c['id'] for c in evidence['captures']], facts=[dict(key='offer',
            status=self.status, certain=True, support=[citation] if citation else [],
            contradictions=[citation] if self.status == 'contradicted' and citation else [])])
        response = dict(id='resp-synthetic-1', model='gpt-6-astra', status='completed',
            metadata=payload['metadata'], output=[dict(type='message', role='assistant', status='completed',
            content=[dict(type='output_text', text=canonical(self.mutate(verdict)))])])
        return 200, canonical(self.response_mutate(response))


def producer(store=None, model=None, auth=lambda *args: True, env=ENV, resolve=lambda gym: mapping()):
    return TrustedSourceObservationProducer(resolve_mapping=resolve, authenticate_capture=auth,
        store=store or Store(), assessor=AstraSemanticAssessor(environ=env, transport=model or Model()),
        environ=env, now=lambda: NOW)


def writes(store):
    return [params for name, params in store.calls if name == 'echo_source_brand_revalidate']


def test_default_off_and_no_hook_fail_without_calls():
    store, model = Store(), Model()
    with pytest.raises(ObservationHold, match='disabled'):
        producer(store, model, env={}).observe(GYM)
    assert not store.calls and not model.calls
    with pytest.raises(ObservationHold, match='hooks_required'):
        TrustedSourceObservationProducer(resolve_mapping=lambda gym: mapping(), authenticate_capture=None)


def test_verified_exact_full_evidence_and_scoped_rpc_readback():
    store, model = Store(), Model()
    result = producer(store, model).observe(GYM)
    assert result['state'] == 'verified' and result['observation_id'] == 1
    params = writes(store)[0]
    assert params['p_validator_revision'] == VALIDATOR_REVISION
    assert params['p_fact_spans'][0]['byte_offset'] == RAW.index(b'Train here')
    assert params['p_primary_offset'] == RAW.index(b'#112233')
    evidence = json.loads(model.calls[0][2]['input'][1]['content'])
    assert evidence['captures'][0]['text'].encode() == RAW
    assert len(evidence['captures']) == 2
    assert 'untrusted evidence' in model.calls[0][2]['input'][0]['content']
    assert model.calls[0][2]['store'] is False
    assert store.calls[-1][0] == 'echo_source_brand_active'


@pytest.mark.parametrize('field,value', [
    ('gym_id', 'foreign'), ('echo_account_key', 'wrong'), ('bytes_sha256', '0'*64),
    ('mapping_revision', 'forged'), ('mapping_evidence', {'identity_verified': True}),
    ('fetched_at', '2020-01-01T00:00:00Z'), ('captured_at', '2027-01-01T00:00:00Z')])
def test_capture_drift_holds_before_assessment(field, value):
    store, model = Store(), Model()
    store.rows[0][field] = value
    with pytest.raises(ObservationHold):
        producer(store, model).observe(GYM)
    assert not writes(store) and not model.calls


def test_forged_transport_labels_do_not_authenticate():
    store = Store()
    store.rows[0]['identity_verified'] = True
    with pytest.raises(ObservationHold, match='transport_authentication_required'):
        producer(store, auth=lambda *args: False).observe(GYM)
    assert not writes(store)


def test_missing_fact_negative_report_persists_hold_without_human_reapproval():
    store, model = Store(), Model('missing')
    raw = RAW.replace(b'Train here', b'Closed now')
    store.rows[0]['raw_bytes'], store.rows[0]['bytes_sha256'] = '\\x'+raw.hex(), sha(raw)
    result = producer(store, model).observe(GYM)
    assert result['state'] == 'held'
    assert writes(store)[0]['p_fact_spans'] == []
    assert result['report']['selected_facts_status'] == 'missing'


def test_identical_bytes_semantic_contradiction_creates_negative_hold():
    store, model = Store(), Model('contradicted')
    result = producer(store, model).observe(GYM)
    assert result['state'] == 'held' and result['report']['selected_facts_status'] == 'contradicted'


@pytest.mark.parametrize('mutation', [
    lambda v: {**v, 'inspected_capture_ids': v['inspected_capture_ids'][:1]},
    lambda v: {**v, 'evidence_sha256': '0'*64},
    lambda v: {**v, 'facts': [{**v['facts'][0], 'certain': False}]},
    lambda v: {**v, 'facts': [{**v['facts'][0], 'status': 'uncertain'}]},
    lambda v: {**v, 'facts': [{**v['facts'][0], 'support': []}]},
    lambda v: {**v, 'facts': [{**v['facts'][0], 'support': [{**v['facts'][0]['support'][0], 'byte_offset': 0}]}]},
])
def test_incomplete_uncertain_or_forged_model_verdict_holds(mutation):
    store, model = Store(), Model()
    model.mutate = mutation
    with pytest.raises(ObservationHold, match='semantic_assessor_unavailable_or_uncertain'):
        producer(store, model).observe(GYM)
    assert not writes(store)


@pytest.mark.parametrize('mutation', [lambda r: {**r, 'status': 'incomplete'},
    lambda r: {**r, 'model': 'wrong'}, lambda r: {**r, 'metadata': {}},
    lambda r: {**r, 'output': [{'content': [{'type': 'refusal'}]}]}])
def test_provider_response_identity_and_completion_required(mutation):
    store, model = Store(), Model()
    model.response_mutate = mutation
    with pytest.raises(ObservationHold):
        producer(store, model).observe(GYM)
    assert not writes(store)


def test_palette_missing_holds_and_oversize_evidence_never_truncated():
    store = Store()
    raw = RAW.replace(b'#112233', b'#112234')
    store.rows[0]['raw_bytes'], store.rows[0]['bytes_sha256'] = '\\x'+raw.hex(), sha(raw)
    with pytest.raises(ObservationHold, match='palette_missing_or_changed'):
        producer(store).observe(GYM)
    assert not writes(store)
    store = Store()
    raw = RAW + b'x' * MAX_EVIDENCE_BYTES
    store.rows[0]['raw_bytes'], store.rows[0]['bytes_sha256'] = '\\x'+raw.hex(), sha(raw)
    with pytest.raises(ObservationHold, match='exceeds_assessor_bound'):
        producer(store).observe(GYM)
    assert not writes(store)


def test_active_readback_wrong_observation_holds_after_write():
    store = Store()
    def wrong(active):
        active['observation']['id'] = 999
        return active
    store.active_mutation = wrong
    with pytest.raises(ObservationHold, match='active_observation_readback_mismatch'):
        producer(store).observe(GYM)
    assert len(writes(store)) == 1


def test_capture_or_mapping_race_prevents_rpc():
    store, model = Store(), Model()
    original = model.__call__
    def drift(url, headers, payload):
        result = original(url, headers, payload)
        store.rows[0]['source_revision'] = 'newer'
        return result
    assessor = AstraSemanticAssessor(environ=ENV, transport=drift)
    p = producer(store, model)
    p.assessor = assessor
    with pytest.raises(ObservationHold, match='changed_during_assessment'):
        p.observe(GYM)
    assert not writes(store)


def test_no_credential_or_exception_body_disclosure():
    def error(*args):
        raise RuntimeError('synthetic-secret-and-source-body')
    p = producer()
    p.assessor = AstraSemanticAssessor(environ=ENV, transport=error)
    with pytest.raises(ObservationHold) as e:
        p.observe(GYM)
    assert 'secret' not in str(e.value) and 'body' not in str(e.value)


def test_service_store_rejects_disabled_and_redirects():
    with pytest.raises(ObservationHold, match='disabled'):
        SourceObservationStore(environ={}).rpc('echo_source_brand_active', {'p_gym': GYM})
    class HTTP:
        def post(self, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            assert kwargs['headers']['Authorization'] == 'Bearer synthetic-service-secret'
            return type('Response', (), {'status_code': 302})()
    env = dict(ENV, ECHO_SOURCE_CAPTURE_SUPABASE_URL='https://database.example',
               ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY='synthetic-service-secret')
    with pytest.raises(ObservationHold, match='service_unavailable'):
        SourceObservationStore(environ=env, http=HTTP()).rpc('echo_source_brand_active', {'p_gym': GYM})


def test_shifted_unicode_bytes_preserve_approval_and_exact_offsets():
    store, model = Store(), Model()
    raw = 'é New '.encode() + RAW
    store.rows[0]['raw_bytes'], store.rows[0]['bytes_sha256'] = '\\x'+raw.hex(), sha(raw)
    result = producer(store, model).observe(GYM)
    assert result['state'] == 'verified'
    assert writes(store)[0]['p_fact_spans'][0]['byte_offset'] == raw.index(b'Train here')
    assert writes(store)[0]['p_fact_spans'][0]['byte_offset'] != raw.decode().index('Train here')
    assert store.config['bundle']['version'] == 1


def test_positive_citation_from_wrong_location_cannot_replace_selected_witness():
    store, model = Store(), Model()
    raw = b'Train here'
    store.rows[1]['raw_bytes'], store.rows[1]['bytes_sha256'] = '\\x'+raw.hex(), sha(raw)
    def wrong(v):
        v['facts'][0]['support'] = [dict(capture_id=store.rows[1]['id'], byte_offset=0,
            byte_length=len(raw), text=raw.decode())]
        return v
    model.mutate = wrong
    with pytest.raises(ObservationHold, match='semantic_assessor_unavailable_or_uncertain'):
        producer(store, model).observe(GYM)
    assert not writes(store)


def test_palette_duplicate_or_invalid_utf8_held_without_truncation():
    for raw in (RAW+b' another #112233', RAW+b'\xff'):
        store, model = Store(), Model()
        store.rows[0]['raw_bytes'], store.rows[0]['bytes_sha256'] = '\\x'+raw.hex(), sha(raw)
        with pytest.raises(ObservationHold):
            producer(store, model).observe(GYM)
        assert not writes(store) and not model.calls


def test_current_mapping_race_and_configuration_race_hold_before_rpc():
    for change_mapping in (True, False):
        store, model = Store(), Model()
        current_mapping = [mapping()]
        def transport(url, headers, payload):
            result = model(url, headers, payload)
            if change_mapping:
                current_mapping[0] = VerifiedMapping(GYM, 'gym_changed', 'rev', {'a': 1}, (WEB,), (SOCIAL,), '123')
            else:
                store.config['bundle']['version'] = 2
            return result
        p = producer(store, resolve=lambda gym: current_mapping[0])
        p.assessor = AstraSemanticAssessor(environ=ENV, transport=transport)
        with pytest.raises(ObservationHold, match='configuration_or_mapping_changed_during_assessment'):
            p.observe(GYM)
        assert not writes(store)


def test_negative_report_requires_active_hold_readback():
    store, model = Store(), Model('contradicted')
    store.active_mutation = lambda active: {'observation': 'wrong-active'}
    with pytest.raises(ObservationHold, match='negative_observation_not_held'):
        producer(store, model).observe(GYM)
    assert len(writes(store)) == 1


def test_assessor_credentials_absent_no_model_call():
    store, model = Store(), Model()
    env = {'ECHO_SOURCE_OBSERVATION_ENABLED': 'true'}
    with pytest.raises(ObservationHold, match='semantic_assessor_credentials_missing'):
        producer(store, model, env=env).observe(GYM)
    assert not writes(store) and not model.calls


def test_incomplete_mapping_source_set_holds():
    m = mapping()
    incomplete = VerifiedMapping(m.gym_id, m.echo_account_key, m.mapping_revision,
        m.mapping_evidence, m.website_response_urls + ('https://gym.example/pricing',),
        m.social_locators, m.provider_account_id)
    store, model = Store(), Model()
    with pytest.raises(ObservationHold, match='complete_mapping_evidence_required'):
        producer(store, model, resolve=lambda gym: incomplete).observe(GYM)
    assert not writes(store) and not model.calls


@pytest.mark.parametrize('boundary', ['model', 'mapping', 'authentication', 'store'])
def test_sensitive_external_exception_context_removed_at_every_trust_boundary(boundary):
    marker = 'synthetic-secret-and-source-body'
    def error(*args, **kwargs):
        raise RuntimeError(marker)
    store = Store()
    p = producer(store)
    if boundary == 'model':
        p.assessor = AstraSemanticAssessor(environ=ENV, transport=error)
    elif boundary == 'mapping':
        p.resolve = error
    elif boundary == 'authentication':
        p.authenticate = error
    else:
        store.rpc = error
    with pytest.raises(ObservationHold) as caught:
        p.observe(GYM)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    rendered = ''.join(traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__))
    assert marker not in rendered
    assert not writes(store)


def test_service_transport_exception_context_removed():
    class HTTP:
        def post(self, *args, **kwargs):
            raise RuntimeError('synthetic-service-token-and-response-body')
    env = dict(ENV, ECHO_SOURCE_CAPTURE_SUPABASE_URL='https://database.example',
               ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY='synthetic-service-secret')
    store = SourceObservationStore(environ=env, http=HTTP())
    with pytest.raises(ObservationHold) as caught:
        store.rpc('echo_source_brand_active', {'p_gym': GYM})
    assert caught.value.__context__ is None
    assert 'synthetic-service-token-and-response-body' not in ''.join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__))


def test_same_mapping_instance_mutated_in_assessor_is_held():
    store, model, m = Store(), Model(), mapping()
    def transport(url, headers, payload):
        response = model(url, headers, payload)
        m.mapping_evidence['receipt'] = 'drifted'
        return response
    p = producer(store, resolve=lambda gym: m)
    p.assessor = AstraSemanticAssessor(environ=ENV, transport=transport)
    with pytest.raises(ObservationHold, match='configuration_or_mapping_changed_during_assessment'):
        p.observe(GYM)
    assert m.mapping_evidence != store.rows[0]['mapping_evidence']
    assert not writes(store)


def test_authenticator_cannot_mutate_detached_mapping_evidence():
    store, m = Store(), mapping()
    def auth(capture, raw, detached_mapping):
        detached_mapping.mapping_evidence['receipt'] = 'changed'
        return True
    with pytest.raises(ObservationHold, match='capture_transport_authentication_required'):
        producer(store, auth=auth, resolve=lambda gym: m).observe(GYM)
    assert m.mapping_evidence == {'receipt': 'mapping'}
    assert not writes(store)


@pytest.mark.parametrize('fields', [
    {'role': 'user'}, {'role': 'system'}, {'status': 'incomplete'},
    {'status': 'in_progress'}, {'type': 'function_call'},
    {'role': 'user', 'status': 'incomplete'}, {'type': 'reasoning'},
])
def test_noncompleted_or_nonassistant_output_message_cannot_attest(fields):
    store, model = Store(), Model()
    def mutate(response):
        response['output'][0].update(fields)
        return response
    model.response_mutate = mutate
    with pytest.raises(ObservationHold, match='semantic_assessor_unavailable_or_uncertain'):
        producer(store, model).observe(GYM)
    assert not writes(store)


def test_actual_reasoning_item_and_completed_assistant_verdict_are_accepted():
    store, model = Store(), Model()
    def mutate(response):
        response['output'].insert(0, {'id': 'rs-synthetic', 'type': 'reasoning', 'summary': []})
        return response
    model.response_mutate = mutate
    assert producer(store, model).observe(GYM)['state'] == 'verified'


def test_two_assistant_messages_are_ambiguous_and_held():
    store, model = Store(), Model()
    def mutate(response):
        response['output'].append(copy.deepcopy(response['output'][0]))
        return response
    model.response_mutate = mutate
    with pytest.raises(ObservationHold, match='semantic_assessor_unavailable_or_uncertain'):
        producer(store, model).observe(GYM)
    assert not writes(store)
