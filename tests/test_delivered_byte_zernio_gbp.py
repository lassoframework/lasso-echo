"""Offline final HTTP fence and intentionally held GBP worker lanes."""
from contextlib import contextmanager
import hashlib
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest
from test_delivered_byte_send_guard import immutable_evidence

from agent import delivered_byte_send_guard as exact
from agent import gbp, gbp_publisher, gbp_worker, zernio
from test_gbp_worker import _row, _conn

URL = 'https://owned.example/card.jpg'
TARGET = {'provider': 'zernio', 'platform': 'instagram', 'account_id': 'account'}


class Http:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        return SimpleNamespace(status_code=200, json=lambda: {'_id': 'accepted'})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv(exact.FLAG, raising=False)
    http = Http()
    return zernio.ZernioClient(api_key='offline', http=http), http


@contextmanager
def permit(target=TARGET, url=URL, snapshot=None, context_format=None):
    # Install a committed permit for isolated boundary tests; authority acquisition
    # and transaction semantics have separate runtime and PostgreSQL tests.
    context = {'provider_target': deepcopy(target), 'row_snapshot': deepcopy(
        snapshot if snapshot is not None else {'caption': 'Approved', 'format': 'feed'})}
    if context_format is not None:
        context['format'] = context_format
    data = b'provider fixture image'
    value = exact.SendPermit(context,
                            [{'role': 'image', 'ordinal': 0, 'url': url,
                              'sha256': 'sha256:' + hashlib.sha256(data).hexdigest(),
                              'byte_length': len(data)}], str(uuid.uuid4()),
                            str(uuid.uuid4()),
                            retention=exact.ProviderFetchRetention(immutable_evidence(url, data).proof.retention_until, 600))
    token = exact._active.set(value)
    try:
        yield value
    finally:
        exact._active.reset(token)


def payload():
    return {'platforms': [{'platform': 'instagram', 'accountId': 'account'}],
            'content': 'Approved', 'mediaItems': [{'type': 'image', 'url': URL}]}


@pytest.mark.parametrize('route', ['post', 'raw', 'gallery'])
def test_on_missing_scope_refuses_before_http(client, monkeypatch, route):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    with pytest.raises(exact.ExactByteSendHold):
        if route == 'post':
            api.create_post('account', 'Approved', [URL], platform='instagram')
        elif route == 'raw':
            api.create_post_raw(payload())
        else:
            api.create_gmb_media('account', URL)
    assert http.calls == []


@pytest.mark.parametrize('route', ['post', 'raw', 'gallery'])
def test_permit_consumed_at_actual_http_body_and_only_once(client, monkeypatch, route):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    target = dict(TARGET, platform='googlebusiness') if route == 'gallery' else TARGET
    def send():
        if route == 'post':
            return api.create_post('account', 'Approved', [URL], platform='instagram',
                                   idempotency_key='11111111-1111-4111-8111-111111111111')
        if route == 'raw':
            return api.create_post_raw(payload(), idempotency_key='11111111-1111-4111-8111-111111111111')
        return api.create_gmb_media('account', URL)
    with permit(target) as committed:
        assert send() == {'_id': 'accepted'}
        assert committed.consumed
        with pytest.raises(exact.ExactByteSendHold, match='consumed'):
            send()
    assert len(http.calls) == 1
    body = http.calls[0][1]['json']
    assert body.get('sourceUrl', body.get('mediaItems', [{}])[0].get('url')) == URL
    if route != 'gallery':
        assert http.calls[0][1]['headers']['Idempotency-Key'] == '11111111-1111-4111-8111-111111111111'


@pytest.mark.parametrize('change', ['account', 'platform', 'image', 'carousel', 'video',
                                    'thumbnail', 'extra_media', 'page', 'location'])
def test_final_payload_drift_and_unsupported_shapes_hold(client, monkeypatch, change):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    body = payload()
    if change in ('account', 'platform'):
        body['platforms'][0]['accountId' if change == 'account' else 'platform'] = 'other'
    elif change == 'image':
        body['mediaItems'][0]['url'] = URL + '?changed'
    elif change == 'carousel':
        body['mediaItems'].append({'type': 'image', 'url': URL})
    elif change == 'video':
        body['mediaItems'][0]['type'] = 'video'
    elif change == 'thumbnail':
        body['mediaItems'][0]['thumbnailUrl'] = URL
    elif change == 'extra_media':
        body['media'] = [URL]
    else:
        body['platforms'][0]['platformSpecificData'] = {
            'pageId' if change == 'page' else 'locationId': 'other'}
    with permit():
        with pytest.raises(exact.ExactByteSendHold, match='differs'):
            api.create_post_raw(body)
    assert http.calls == []


def test_verified_page_and_location_ids_must_match(client, monkeypatch):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    body = payload()
    body['platforms'][0]['platformSpecificData'] = {'pageId': 'destination'}
    with permit(dict(TARGET, page_id='destination')):
        api.create_post_raw(body)
    target = dict(TARGET, platform='googlebusiness', location_id='destination')
    row = _row(caption='Approved', format='feed', image_url=URL)
    body = gbp_worker.build_gbp_payload_for_row(row, {
        'zernio_account_id': 'account', 'gbp_location_id': 'destination'})
    with permit(target, snapshot=row):
        api.create_post_raw(body)
    assert len(http.calls) == 2


@pytest.mark.parametrize('change', ['content', 'missing_content', 'story', 'reel',
    'feed_type', 'publish_false', 'publish_string', 'schedule', 'timezone', 'draft_string',
    'event', 'offer', 'cta', 'topic'])
def test_raw_cannot_change_committed_feed_semantics(client, monkeypatch, change):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    body = payload()
    if change == 'content':
        body['content'] = 'Unapproved'
    elif change == 'missing_content':
        del body['content']
    elif change in ('story', 'reel', 'feed_type'):
        body['platforms'][0]['platformSpecificData'] = {
            'contentType': 'feed' if change == 'feed_type' else change}
    elif change in ('publish_false', 'publish_string'):
        body['publishNow'] = False if change == 'publish_false' else 'true'
    elif change == 'schedule':
        body['scheduledFor'] = '2026-10-11T12:00:00Z'
    elif change == 'timezone':
        body['timezone'] = 'UTC'
    elif change == 'draft_string':
        body['isDraft'] = 'false'
    else:
        key = {'event': 'event', 'offer': 'offer', 'cta': 'callToAction', 'topic': 'topicType'}[change]
        body['platforms'][0]['platformSpecificData'] = {key: 'unapproved'}
    with permit() as committed:
        with pytest.raises(exact.ExactByteSendHold):
            # Avoid raw's publishNow overwrite: test the final send representation.
            api.create_post_raw(body, publish_now=False)
        assert not committed.consumed
    assert http.calls == []


@pytest.mark.parametrize('raw', [False, True])
def test_legitimate_story_empty_vendor_caption(client, monkeypatch, raw):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    with permit(snapshot={'caption': 'Approved burned copy', 'format': 'story'}):
        if raw:
            body = payload()
            body['content'] = ''
            body['platforms'][0]['platformSpecificData'] = {'contentType': 'story'}
            api.create_post_raw(body)
        else:
            api.create_post('account', '', [URL], platform='instagram', story=True)
    assert len(http.calls) == 1


@pytest.mark.parametrize('change', ['caption', 'missing_type', 'feed', 'missing_caption'])
def test_story_cannot_carry_arbitrary_text_or_change_format(client, monkeypatch, change):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    body = payload()
    body['content'] = 'Approved burned copy' if change == 'caption' else ''
    if change != 'missing_type':
        body['platforms'][0]['platformSpecificData'] = {
            'contentType': 'feed' if change == 'feed' else 'story'}
    if change == 'missing_caption':
        del body['content']
    with permit(snapshot={'caption': 'Approved burned copy', 'format': 'story'}):
        with pytest.raises(exact.ExactByteSendHold):
            api.create_post_raw(body)
    assert http.calls == []


@pytest.mark.parametrize('topic', ['STANDARD', 'EVENT', 'OFFER'])
def test_legitimate_gbp_snapshot_semantics(client, monkeypatch, topic):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    row = _row(caption='Approved', format='feed', image_url=URL, gbp_topic_type=topic,
        gbp_event={'title': 'Approved event', 'schedule': {
            'startDate': '2026-10-10', 'endDate': '2026-10-11'}},
        gbp_offer={'redeemOnlineUrl': 'https://gym.com/join', 'termsConditions': 'Approved terms'})
    body = gbp_worker.build_gbp_payload_for_row(row, _conn())
    target = dict(TARGET, platform='googlebusiness', account_id='acc_gbp_1',
                  location_id='locations/123')
    with permit(target, snapshot=row):
        api.create_post_raw(body)
    assert len(http.calls) == 1


@pytest.mark.parametrize('change', ['caption', 'cta', 'event', 'offer', 'topic',
    'missing_semantic', 'incomplete_snapshot', 'format_conflict'])
def test_gbp_raw_semantic_drift_holds(client, monkeypatch, change):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    row = _row(caption='Approved', format='feed', image_url=URL, gbp_topic_type='EVENT',
               gbp_event={'title': 'Approved', 'schedule': {'startDate': '2026-10-10'}})
    body = gbp_worker.build_gbp_payload_for_row(row, _conn())
    psd = body['platforms'][0]['platformSpecificData']
    if change == 'caption':
        body['content'] = 'Unapproved'
    elif change == 'cta':
        psd['callToAction']['url'] = 'https://other.example'
    elif change == 'event':
        psd['event'] = {'title': 'Unapproved'}
    elif change == 'offer':
        psd['offer'] = {'termsConditions': 'Unapproved'}
    elif change == 'topic':
        psd['topicType'] = 'OFFER'
    elif change == 'missing_semantic':
        del psd['event']
    elif change == 'incomplete_snapshot':
        del row['gbp_cta_url']
    target = dict(TARGET, platform='googlebusiness', account_id='acc_gbp_1',
                  location_id='locations/123')
    with permit(target, snapshot=row,
                context_format='story' if change == 'format_conflict' else None) as committed:
        with pytest.raises(exact.ExactByteSendHold):
            api.create_post_raw(body)
        assert not committed.consumed
    assert http.calls == []


def test_active_permit_keeps_semantic_fence_after_flag_off(client):
    api, http = client
    body = payload()
    body['content'] = 'Unapproved'
    with permit():
        with pytest.raises(exact.ExactByteSendHold):
            api.create_post_raw(body)
    assert http.calls == []


@pytest.mark.parametrize('change', ['caption', 'story', 'missing_mode', 'schedule', 'incomplete_snapshot'])
def test_direct_http_helper_cannot_bypass_content_binding(client, monkeypatch, change):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    body = payload()
    body['publishNow'] = True
    row = {'caption': 'Approved', 'format': 'feed'}
    if change == 'caption':
        body['content'] = 'Unapproved'
    elif change == 'story':
        body['platforms'][0]['platformSpecificData'] = {'contentType': 'story'}
    elif change == 'missing_mode':
        del body['publishNow']
    elif change == 'schedule':
        # An arbitrary timestamp even copied into a row does not establish
        # vendor scheduling authority under the current calendar schema.
        row['scheduled_at'] = body['scheduledFor'] = '2026-10-10T12:00:00Z'
    else:
        del row['caption']
    with permit(snapshot=row) as committed:
        with pytest.raises(exact.ExactByteSendHold):
            api._post('/v1/posts', body)
        assert not committed.consumed
    assert http.calls == []


def test_off_without_permit_preserves_raw_content_and_scheduling(client):
    api, http = client
    body = payload()
    body.update(content='Legacy content', scheduledFor='2026-10-11T12:00:00Z',
                timezone='UTC', publishNow=False)
    body['platforms'][0]['platformSpecificData'] = {'contentType': 'reel'}
    api.create_post_raw(body, publish_now=False)
    assert http.calls[0][1]['json'] == body


@pytest.mark.parametrize('field', ['termsConditions', 'redeemOnlineUrl', 'couponCode'])
def test_gbp_offer_nested_content_cannot_drift(client, monkeypatch, field):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    row = _row(caption='Approved', format='feed', image_url=URL, gbp_topic_type='OFFER',
               gbp_offer={'redeemOnlineUrl': 'https://gym.com/join', 'termsConditions': 'Approved'})
    body = gbp_worker.build_gbp_payload_for_row(row, _conn())
    body['platforms'][0]['platformSpecificData']['offer'][field] = 'Unapproved'
    target = dict(TARGET, platform='googlebusiness', account_id='acc_gbp_1',
                  location_id='locations/123')
    with permit(target, snapshot=row):
        with pytest.raises(exact.ExactByteSendHold):
            api.create_post_raw(body)
    assert http.calls == []


def test_gbp_nested_json_types_are_pinned(client, monkeypatch):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    row = _row(caption='Approved', format='feed', image_url=URL, gbp_topic_type='EVENT',
               gbp_event={'title': 'Approved', 'schedule': {'startDate': {'day': 1}}})
    body = gbp_worker.build_gbp_payload_for_row(row, _conn())
    body = deepcopy(body)
    body['platforms'][0]['platformSpecificData']['event']['schedule']['startDate']['day'] = True
    target = dict(TARGET, platform='googlebusiness', account_id='acc_gbp_1',
                  location_id='locations/123')
    with permit(target, snapshot=row):
        with pytest.raises(exact.ExactByteSendHold):
            api.create_post_raw(body)
    assert http.calls == []


def test_off_unsupported_legacy_shapes_and_draft_on_unchanged(client, monkeypatch):
    api, http = client
    body = payload()
    body['mediaItems'].append({'type': 'video', 'url': 'https://other/movie.mp4'})
    api.create_post_raw(body)
    monkeypatch.setenv(exact.FLAG, 'true')
    api.create_post_raw(body, draft=True)
    assert len(http.calls) == 2
    assert http.calls[1][1]['json']['isDraft'] is True


@pytest.mark.parametrize('gallery', [False, True])
def test_gbp_worker_live_holds_even_with_fake_provider(client, monkeypatch, gallery):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setattr('agent.publish_billing_gate.publishing_blocked', lambda gym: False)
    method = gbp_worker.publish_photo_drop if gallery else gbp_worker.publish_gbp_row
    result = method(_row(image_url=URL), _conn(), client=api, draft=False)
    assert result['held'] == 'exact_byte_verification'
    assert result['status'] == 'approved'
    assert http.calls == []


@pytest.mark.parametrize('uncertain', [False, True])
def test_gbp_scope_unknown_authority_keeps_publishing_claim(monkeypatch, uncertain):
    monkeypatch.setenv(exact.FLAG, 'true')
    @contextmanager
    def hold(*args):
        raise exact.ExactByteSendHold('authority unavailable', definitive_no_post=not uncertain)
        yield
    monkeypatch.setattr(exact, 'authorized_send', hold)
    result = gbp_worker._exact_gbp_hold(object(), _row(), 'persisted-token')
    assert result['held'] == ('ambiguous_send' if uncertain else 'exact_byte_verification')
    assert result['status'] == ('publishing' if uncertain else 'approved')


def test_legacy_direct_v4_holds_before_token_or_request(client, monkeypatch):
    _, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setattr(gbp_publisher.config, 'publish_enabled', lambda: True)
    monkeypatch.setattr(gbp_publisher.config, 'gbp_enabled', lambda: True)
    monkeypatch.setattr('agent.publish_billing_gate.publishing_blocked', lambda gym: False)
    with pytest.raises(exact.ExactByteSendHold, match='legacy GBP'):
        gbp_publisher.publish(SimpleNamespace(caption='Approved'),
                              SimpleNamespace(key='gym_gbp'), http=http)
    assert http.calls == []


def test_gallery_video_and_extra_media_hold_even_with_permit(client, monkeypatch):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    for body in [
        {'mediaFormat': 'PHOTO', 'sourceUrl': 'https://owned.example/movie.mp4'},
        {'mediaFormat': 'PHOTO', 'sourceUrl': URL, 'mediaItems': [{'url': URL}]},
    ]:
        with permit(dict(TARGET, platform='googlebusiness')):
            with pytest.raises(exact.ExactByteSendHold):
                api._post('/v1/accounts/account/gmb-media', body)
    assert http.calls == []


def test_flag_off_does_not_bypass_an_active_permit(client):
    api, http = client
    with permit():
        body = payload()
        body['mediaItems'][0]['url'] += '?drift'
        with pytest.raises(exact.ExactByteSendHold):
            api.create_post_raw(body)
    assert http.calls == []


@pytest.mark.parametrize('gallery', [False, True])
def test_valid_gbp_claim_without_immutable_storage_proof_holds_before_reads(client, monkeypatch, gallery):
    api, http = client
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setenv('AGENT_S3_PUBLIC_BASE_URL', 'https://owned.example')
    monkeypatch.setattr('agent.publish_billing_gate.publishing_blocked', lambda gym: False)
    row = _row(id=str(uuid.uuid4()), image_url=URL)
    claim = str(uuid.uuid4())
    context = dict(enabled=True, calendar_row_id=row['id'], publish_claim_token=claim,
        row_revision='revision', canonical_tenant='tenant', post_date='2026-10-10',
        reservation_day='2026-10-10', posting_timezone='UTC', row_snapshot=deepcopy(row),
        provider_target=dict(TARGET, platform='googlebusiness'), shared_posting_identity=None,
        corpus_sha256='sha256:' + 'a' * 64, cutover_id=str(uuid.uuid4()),
        images=[dict(ordinal=0, role='image', url=URL)])
    calls = []
    class Store:
        def _reservation_rpc(self, name, args, timeout):
            calls.append((name, args))
            return context
    method = gbp_worker.publish_photo_drop if gallery else gbp_worker.publish_gbp_row
    result = method(row, _conn(), client=api, draft=False,
                    authority_store=Store(), idempotency_key=claim)
    assert result['held'] == 'exact_byte_verification'
    assert 'immutable' in result['reject_reason']
    assert [name for name, _ in calls] == ['exact_byte_send_context_20261010']
    assert http.calls == []
