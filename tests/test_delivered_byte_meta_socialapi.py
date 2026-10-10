"""Default-OFF exact-byte send fence at the Meta and SocialAPI provider boundaries.

Offline fake-provider tests: flag OFF parity, flag ON no-scope zero-mutation
holds, one-permit-one-attempt consumption, byte pinning, and unsupported-shape
holds. No network, no database.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from types import SimpleNamespace
import uuid

import pytest
from test_delivered_byte_send_guard import immutable_evidence

from agent import delivered_byte_send_guard as exact
from test_delivered_byte_send_guard import immutable_evidence
from agent import meta_publisher, socialapi_client, socialapi_publisher
from agent.accounts import Platform
from agent.forward_media_send_context import ProviderSendHold

URL = 'https://owned.example/card.jpg'
VIDEO_URL = 'https://owned.example/clip.mp4'
META_IG_TARGET = {'provider': 'meta', 'platform': 'instagram', 'account_id': 'ig-123'}
META_FB_TARGET = {'provider': 'meta', 'platform': 'facebook_page', 'account_id': 'pg-123'}
SAPI_TARGET = {'provider': 'socialapi', 'platform': 'instagram', 'account_id': 'sa-1'}
DATA = b'exact-image-bytes'


class Resp:
    status_code = 200
    text = '{}'

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class Http:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(('post', url, deepcopy(kwargs)))
        return Resp({'id': 'm1', 'post_id': 'p1', 'media_id': 'mid'})

    def get(self, url, **kwargs):
        self.calls.append(('get', url, deepcopy(kwargs)))
        return Resp({'status_code': 'FINISHED'})


class Account:
    def __init__(self, platform, key, target):
        self.platform = platform
        self.key = key
        self._target = target

    def get_target_id(self):
        return self._target

    def get_token(self):
        return 'tok'


def draft(**over):
    base = dict(caption='Approved words', hashtags=[], creative_public_url=URL,
                creative_path='', slide_urls=[], is_story=False,
                platform=Platform.INSTAGRAM, draft_id='d1', account_key='gym_ig',
                day_key='2026-10-10')
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(exact.FLAG, raising=False)
    monkeypatch.delenv('AGENT_FORWARD_MEDIA_GUARD', raising=False)
    monkeypatch.setattr(meta_publisher, 'POST_FINISH_GRACE_SEC', 0)
    yield


@contextmanager
def permit(target, url=URL, data=None, post_format="feed", caption=None):
    # Install a committed permit for isolated boundary tests; authority
    # acquisition and transaction semantics have separate runtime/PG tests.
    data = data if data is not None else DATA
    image = {'role': 'image', 'ordinal': 0, 'url': url,
             'sha256': 'sha256:' + hashlib.sha256(data).hexdigest(),
             'byte_length': len(data)}
    value = exact.SendPermit({'provider_target': deepcopy(target), 'format': post_format,
                             'row_snapshot': {'caption': caption if caption is not None else
                                              ('text' if target['provider'] == 'socialapi' else 'cap')}}, [image],
                             str(uuid.uuid4()), str(uuid.uuid4()),
                             retention=exact.ProviderFetchRetention(immutable_evidence(url, data).proof.retention_until, 600))
    token = exact._active.set(value)
    try:
        yield value
    finally:
        exact._active.reset(token)


# ---- Meta: flag OFF parity -------------------------------------------------

def test_meta_off_ig_image_feed_publishes_unchanged(monkeypatch):
    http = Http()
    account = Account(Platform.INSTAGRAM, 'gym_ig', 'ig-123')
    result = meta_publisher._publish_instagram(http, account, draft(), 'cap', 'tok')
    assert result.ok and result.mode == 'published' and result.media_id == 'm1'
    posts = [c for c in http.calls if c[0] == 'post']
    assert len(posts) == 2                      # create container + media_publish
    assert posts[0][2]['data']['image_url'] == URL
    assert 'creation_id' in posts[1][2]['data']


def test_meta_off_fb_page_photo_and_text_unchanged():
    account = Account(Platform.FACEBOOK_PAGE, 'gym_fb', 'pg-123')
    http = Http()
    result = meta_publisher._publish_fb_page(http, account, draft(
        platform=Platform.FACEBOOK_PAGE), 'cap', 'tok')
    assert result.ok and result.media_id == 'p1'
    assert http.calls[0][2]['data']['url'] == URL
    http2 = Http()
    result = meta_publisher._publish_fb_page(http2, account, draft(
        platform=Platform.FACEBOOK_PAGE, creative_public_url=''), 'cap', 'tok')
    assert result.ok
    assert http2.calls[0][2]['data'] == {'message': 'cap', 'access_token': 'tok'}


def test_meta_off_carousel_reel_and_stories_unchanged():
    ig = Account(Platform.INSTAGRAM, 'gym_ig', 'ig-123')
    http = Http()
    car = draft(slide_urls=[URL, URL])
    result = meta_publisher._publish_instagram(http, ig, car, 'cap', 'tok')
    assert result.ok
    assert len([c for c in http.calls if c[0] == 'post']) == 4  # 2 children+parent+publish
    http = Http()
    result = meta_publisher._publish_instagram(http, ig, draft(
        creative_public_url=VIDEO_URL), 'cap', 'tok')
    assert result.ok                            # reel flow untouched when OFF
    http = Http()
    result = meta_publisher._publish_instagram_story(http, ig, draft(), 'tok')
    assert result.ok
    assert http.calls[0][2]['data']['image_url'] == URL
    fb = Account(Platform.FACEBOOK_PAGE, 'gym_fb', 'pg-123')
    http = Http()
    result = meta_publisher._publish_fb_page_story(http, fb, draft(
        platform=Platform.FACEBOOK_PAGE), 'tok')
    assert result.ok and result.media_id == 'p1'
    assert len([c for c in http.calls if c[0] == 'post']) == 2  # photos+photo_stories


# ---- Meta: flag ON, no scope -> hold with ZERO mutations -------------------

def _meta_routes():
    ig = Account(Platform.INSTAGRAM, 'gym_ig', 'ig-123')
    fb = Account(Platform.FACEBOOK_PAGE, 'gym_fb', 'pg-123')
    return [
        lambda http: meta_publisher._publish_instagram(http, ig, draft(), 'cap', 'tok'),
        lambda http: meta_publisher._publish_instagram(http, ig, draft(
            slide_urls=[URL, URL]), 'cap', 'tok'),
        lambda http: meta_publisher._publish_instagram(http, ig, draft(
            creative_public_url=VIDEO_URL), 'cap', 'tok'),
        lambda http: meta_publisher._publish_instagram_story(http, ig, draft(), 'tok'),
        lambda http: meta_publisher._publish_instagram_story(http, ig, draft(
            creative_public_url=VIDEO_URL), 'tok'),
        lambda http: meta_publisher._publish_fb_page(http, fb, draft(
            platform=Platform.FACEBOOK_PAGE), 'cap', 'tok'),
        lambda http: meta_publisher._publish_fb_page(http, fb, draft(
            platform=Platform.FACEBOOK_PAGE, creative_public_url=''), 'cap', 'tok'),
        lambda http: meta_publisher._publish_fb_page(http, fb, draft(
            platform=Platform.FACEBOOK_PAGE, creative_public_url=VIDEO_URL,
            creative_path=''), 'cap', 'tok'),
        lambda http: meta_publisher._publish_fb_page_story(http, fb, draft(
            platform=Platform.FACEBOOK_PAGE), 'tok'),
        lambda http: meta_publisher._publish_fb_page_story(http, fb, draft(
            platform=Platform.FACEBOOK_PAGE, creative_public_url=VIDEO_URL), 'tok'),
    ]


@pytest.mark.parametrize('route', range(len(_meta_routes())))
def test_meta_on_no_scope_holds_before_any_mutation(monkeypatch, route):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with pytest.raises((exact.ExactByteSendHold, ProviderSendHold)):
        _meta_routes()[route](http)
    assert http.calls == []


def test_meta_on_scoped_permit_direct_helper_holds_unbound_followup(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    ig = Account(Platform.INSTAGRAM, 'gym_ig', 'ig-123')
    with permit(META_IG_TARGET) as committed:
        with pytest.raises(ProviderSendHold, match='already consumed'):
            meta_publisher._publish_instagram(http, ig, draft(), 'cap', 'tok')
        assert committed.consumed
    assert len([c for c in http.calls if c[0] == 'post']) == 1


def test_meta_wrapped_image_attempt_binds_container_and_consumes_once(monkeypatch):
    from agent.forward_media_send_context import guarded_publisher
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    ig = Account(Platform.INSTAGRAM, 'gym_ig', 'ig-123')
    @guarded_publisher('meta')
    def send(draft, account):
        return meta_publisher._publish_instagram(http, account, draft, 'cap', 'tok')
    with permit(META_IG_TARGET) as committed:
        assert send(draft(), ig).ok
        continuation = exact.active_continuation()
        assert continuation.object_id == 'm1' and continuation.used
        with pytest.raises((ProviderSendHold, exact.ExactByteSendHold)):
            send(draft(), ig)
    assert len([c for c in http.calls if c[0] == 'post']) == 2


# ---- Meta helpers: exact require semantics (direct, no wrapper) ------------

def test_meta_exact_require_consumes_once_and_matches_target(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    with permit(META_IG_TARGET) as committed:
        meta_publisher._exact_require(dict(META_IG_TARGET), [URL])
        assert committed.consumed
        with pytest.raises(exact.ExactByteSendHold, match='consumed'):
            meta_publisher._exact_require(dict(META_IG_TARGET), [URL])


def test_meta_exact_unsupported_holds_under_fence(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    with pytest.raises(exact.ExactByteSendHold):
        meta_publisher._exact_unsupported()          # no scope
    with permit(META_IG_TARGET) as committed:
        with pytest.raises(exact.ExactByteSendHold, match='differs'):
            meta_publisher._exact_unsupported()      # never matches a permit
        assert not committed.consumed
    monkeypatch.delenv(exact.FLAG)
    assert meta_publisher._exact_unsupported() is None   # OFF: no-op


# ---- SocialAPI client: direct-call holds -----------------------------------

@pytest.fixture
def sapi_env(monkeypatch):
    monkeypatch.setenv('AGENT_SOCIALAPI_KEY', 'offline-key')
    monkeypatch.setenv('AGENT_SOCIALAPI_BASE_URL', 'https://sapi.test')


def test_socialapi_client_off_parity(sapi_env):
    http = Http()
    assert socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http) == 'mid'
    body = socialapi_client.create_post('sa-1', 'text', ['mid'], http=http)
    assert body == {'id': 'm1', 'post_id': 'p1', 'media_id': 'mid'}
    assert [c[1] for c in http.calls] == ['https://sapi.test/media/upload',
                                          'https://sapi.test/posts']


def test_socialapi_client_on_no_scope_holds(sapi_env, monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with pytest.raises(exact.ExactByteSendHold):
        socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http)
    with pytest.raises(exact.ExactByteSendHold):
        socialapi_client.create_post('sa-1', 'text', ['mid'], http=http)
    assert http.calls == []


def test_socialapi_client_bound_upload_then_exact_create_passes_once(sapi_env, monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with permit(SAPI_TARGET, data=DATA) as committed:
        assert socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http,
                                             exact_target=SAPI_TARGET, image_url=URL, post_text='text') == 'mid'
        assert committed.consumed
        continuation = exact.active_continuation()
        socialapi_client.create_post('sa-1', 'text', ['mid'], http=http,
                                     exact_continuation=continuation)
        with pytest.raises(exact.ExactByteSendHold):
            socialapi_client.create_post('sa-1', 'text', ['mid'], http=http,
                                         exact_continuation=continuation)
    assert len(http.calls) == 2


@pytest.mark.parametrize('changes', [dict(exact_target=None), dict(image_url='wrong'),
    dict(data_bytes=b'wrong'), dict(exact_target=META_IG_TARGET),
    dict(exact_target={**SAPI_TARGET, 'account_id': 'wrong'}), dict(content_type='video/mp4')])
def test_socialapi_raw_upload_requires_exact_target_url_and_bytes(sapi_env, monkeypatch, changes):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with permit(SAPI_TARGET, data=DATA) as committed:
        kwargs = dict(data_bytes=DATA, filename='card.jpg', content_type='image/jpeg',
                      http=http, exact_target=SAPI_TARGET, image_url=URL, post_text='text')
        kwargs.update(changes)
        with pytest.raises(exact.ExactByteSendHold):
            socialapi_client.upload_media(**kwargs)
        assert not committed.consumed
    assert http.calls == []


@pytest.mark.parametrize('kind', ['account', 'media', 'multi', 'format', 'missing_token'])
def test_socialapi_raw_create_cannot_substitute_or_add_media(sapi_env, monkeypatch, kind):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with permit(SAPI_TARGET, data=DATA):
        socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http,
                                     exact_target=SAPI_TARGET, image_url=URL, post_text='text')
        kwargs = dict(account_id='sa-1', text='text', media_ids=['mid'],
                      http=http, exact_continuation=exact.active_continuation())
        if kind == 'account': kwargs['account_id'] = 'wrong'
        if kind == 'media': kwargs['media_ids'] = ['wrong']
        if kind == 'multi': kwargs['media_ids'] = ['mid', 'extra']
        if kind == 'format': kwargs['content_type'] = 'stories'
        if kind == 'missing_token': kwargs['exact_continuation'] = None
        with pytest.raises(exact.ExactByteSendHold):
            socialapi_client.create_post(**kwargs)
    assert len(http.calls) == 1


def test_socialapi_consumed_permit_and_flag_flip_stay_fenced(sapi_env, monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with permit(SAPI_TARGET, data=DATA):
        socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http,
                                     exact_target=SAPI_TARGET, image_url=URL, post_text='text')
        continuation = exact.active_continuation()
        monkeypatch.delenv(exact.FLAG)
        with pytest.raises(exact.ExactByteSendHold):
            socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http,
                                         exact_target=SAPI_TARGET, image_url=URL, post_text='text')
        with pytest.raises(exact.ExactByteSendHold):
            socialapi_client.create_post('wrong', 'text', ['mid'], http=http,
                                         exact_continuation=continuation)
        socialapi_client.create_post('sa-1', 'text', ['mid'], http=http,
                                     exact_continuation=continuation)
    assert len(http.calls) == 2


# ---- SocialAPI publisher fence: bytes pinned, one permit per attempt --------

def test_socialapi_fence_off_is_noop():
    assert socialapi_publisher._exact_byte_fence('instagram', 'sa-1', URL, DATA,
                                                 'image/jpeg') is None
    assert socialapi_publisher._exact_byte_fence('instagram', 'sa-1', VIDEO_URL,
                                                 b'v', 'video/mp4') is None


def test_socialapi_fence_on_no_scope_holds(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    with pytest.raises(exact.ExactByteSendHold):
        socialapi_publisher._exact_byte_fence('instagram', 'sa-1', URL, DATA,
                                              'image/jpeg')


def test_socialapi_fence_preflights_and_raw_upload_consumes(sapi_env, monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    with permit(SAPI_TARGET, data=DATA) as committed:
        target = socialapi_publisher._exact_byte_fence('instagram', 'sa-1', URL, DATA,
                                                       'image/jpeg')
        assert target == SAPI_TARGET and not committed.consumed
        socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=Http(),
                                     exact_target=target, image_url=URL, post_text='text')
        assert committed.consumed
        with pytest.raises(exact.ExactByteSendHold, match='consumed'):
            socialapi_publisher._exact_byte_fence('instagram', 'sa-1', URL, DATA,
                                                  'image/jpeg')


def test_socialapi_fence_byte_mismatch_holds_before_consume(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    with permit(SAPI_TARGET, data=b'other-bytes') as committed:
        with pytest.raises(exact.ExactByteSendHold, match='differ'):
            socialapi_publisher._exact_byte_fence('instagram', 'sa-1', URL, DATA,
                                                  'image/jpeg')
        assert not committed.consumed


def test_socialapi_fence_target_drift_and_video_hold(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    with permit(SAPI_TARGET, data=DATA) as committed:
        with pytest.raises(exact.ExactByteSendHold, match='differs'):
            socialapi_publisher._exact_byte_fence('instagram', 'sa-OTHER', URL, DATA,
                                                  'image/jpeg')
        with pytest.raises(exact.ExactByteSendHold, match='differs'):
            socialapi_publisher._exact_byte_fence('instagram', 'sa-1', VIDEO_URL,
                                                  DATA, 'video/mp4')
        assert not committed.consumed


@pytest.mark.parametrize('flag_flip', [False, True])
def test_meta_raw_container_publish_requires_bound_attempt(monkeypatch, flag_flip):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with permit(META_IG_TARGET):
        if flag_flip:
            monkeypatch.delenv(exact.FLAG)
        with pytest.raises(exact.ExactByteSendHold, match='continuation'):
            meta_publisher._publish_container(http, 'https://meta.test', 'ig-123', 'random', 'tok')
    assert http.calls == []


@pytest.mark.parametrize('kind', ['target', 'id', 'used', 'closed', 'copied'])
def test_meta_container_continuation_cannot_change_attempt(monkeypatch, kind):
    from copy import copy
    monkeypatch.setenv(exact.FLAG, 'true')
    # Isolate exact guard from the independently tested wrapper/broad gate.
    monkeypatch.setattr(meta_publisher, 'boundary', lambda *a, **kw: None)
    http = Http()
    with permit(META_IG_TARGET) as committed:
        exact.require(META_IG_TARGET, [URL])
        cont = exact.bind_continuation(committed, META_IG_TARGET, 'returned-id', 'meta:media_publish')
        target, object_id = 'ig-123', 'returned-id'
        if kind == 'target': target = 'other'
        if kind == 'id': object_id = 'other'
        if kind == 'closed': committed.closed = True
        if kind == 'copied': cont = copy(cont)
        if kind == 'used':
            exact.require_continuation(cont, META_IG_TARGET, object_id, 'meta:media_publish')
        with pytest.raises(exact.ExactByteSendHold):
            meta_publisher._publish_container(http, 'https://meta.test', target, object_id, 'tok',
                                              exact_continuation=cont)
    assert http.calls == []


def test_meta_bound_not_ready_response_holds_without_retry(monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setattr(meta_publisher, 'boundary', lambda *a, **kw: None)
    class NotReadyHttp(Http):
        def post(self, url, **kwargs):
            super().post(url, **kwargs)
            response = Resp({'error': {'code': 9007}})
            response.status_code = 400
            return response
    http = NotReadyHttp()
    with permit(META_IG_TARGET) as committed:
        exact.require(META_IG_TARGET, [URL])
        cont = exact.bind_continuation(committed, META_IG_TARGET, 'returned-id', 'meta:media_publish')
        with pytest.raises(exact.ExactByteSendHold, match='reconciliation'):
            meta_publisher._publish_container(http, 'https://meta.test', 'ig-123', 'returned-id', 'tok',
                                              _sleep=lambda _: pytest.fail('must not retry'),
                                              exact_continuation=cont)
    assert len(http.calls) == 1


@pytest.mark.parametrize('route,target', [('feed', META_IG_TARGET),
                                        ('story', META_IG_TARGET),
                                        ('fb_story', META_FB_TARGET)])
def test_meta_final_boundary_expiry_blocks_continuation_post(monkeypatch, route, target):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    platform = Platform.FACEBOOK_PAGE if route == 'fb_story' else Platform.INSTAGRAM
    account = Account(platform, 'gym', target['account_id'])
    final_boundaries = []

    def expire_after_revalidation(*args, **kwargs):
        continuation = exact.active_continuation()
        if kwargs.get('attempt') and continuation is not None:
            final_boundaries.append(kwargs)
            # Simulate DB/network revalidation consuming the remaining retention.
            assert not continuation.used
            object.__setattr__(continuation.permit.retention, '_monotonic_until', 0)

    monkeypatch.setattr(meta_publisher, 'boundary', expire_after_revalidation)
    with permit(target, post_format='feed' if route == 'feed' else 'story'):
        with pytest.raises(exact.ExactByteSendHold, match='retention expired or insufficient'):
            if route == 'feed':
                meta_publisher._publish_instagram(http, account, draft(), 'cap', 'tok')
            elif route == 'story':
                meta_publisher._publish_instagram_story(http, account, draft(is_story=True), 'tok')
            else:
                meta_publisher._publish_fb_page_story(
                    http, account, draft(platform=platform, is_story=True), 'tok')
        assert not exact.active_continuation().used
    assert len(final_boundaries) == 1
    posts = [call for call in http.calls if call[0] == 'post']
    assert len(posts) == 1
    assert posts[0][1].endswith('/photos' if route == 'fb_story' else '/media')


@pytest.mark.parametrize('route,target', [('feed', META_IG_TARGET), ('story', META_IG_TARGET),
                                          ('fb_story', META_FB_TARGET)])
def test_meta_wrapped_image_shapes_continue_only_returned_id(monkeypatch, route, target):
    from agent.forward_media_send_context import guarded_publisher
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    platform = Platform.FACEBOOK_PAGE if route == 'fb_story' else Platform.INSTAGRAM
    account = Account(platform, 'gym', target['account_id'])
    @guarded_publisher('meta')
    def send(draft, account):
        if route == 'feed':
            return meta_publisher._publish_instagram(http, account, draft, 'cap', 'tok')
        if route == 'story':
            return meta_publisher._publish_instagram_story(http, account, draft, 'tok')
        return meta_publisher._publish_fb_page_story(http, account, draft, 'tok')
    with permit(target, post_format='feed' if route == 'feed' else 'story'):
        assert send(draft(platform=platform, is_story=route != 'feed'), account).ok
        assert exact.active_continuation().used
    posts = [c for c in http.calls if c[0] == 'post']
    assert len(posts) == 2
    assert posts[1][2]['data'].get('creation_id', posts[1][2]['data'].get('photo_id')) == 'm1'


def test_socialapi_missing_upload_receipt_never_allows_post(sapi_env, monkeypatch):
    monkeypatch.setenv(exact.FLAG, 'true')
    class NoReceiptHttp(Http):
        def post(self, url, **kwargs):
            super().post(url, **kwargs)
            return Resp({})
    http = NoReceiptHttp()
    with permit(SAPI_TARGET, data=DATA) as committed:
        with pytest.raises(exact.ExactByteSendHold, match='continuation unavailable'):
            socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http,
                                         exact_target=SAPI_TARGET, image_url=URL, post_text='text')
        assert committed.consumed
        with pytest.raises(exact.ExactByteSendHold):
            socialapi_client.create_post('sa-1', 'text', ['anything'], http=http)
    assert len(http.calls) == 1


@pytest.mark.parametrize('text', ['unapproved text', None, '', 'text '])
def test_socialapi_raw_create_cannot_change_approved_caption(sapi_env, monkeypatch, text):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with permit(SAPI_TARGET, data=DATA):
        socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http,
                                     exact_target=SAPI_TARGET, image_url=URL, post_text='text')
        with pytest.raises(exact.ExactByteSendHold):
            socialapi_client.create_post('sa-1', text, ['mid'], http=http,
                                         exact_continuation=exact.active_continuation())
    assert len(http.calls) == 1


@pytest.mark.parametrize('text,kind', [('wrong', 'feed'), ('text', 'stories')])
def test_socialapi_upload_must_pin_intended_content(sapi_env, monkeypatch, text, kind):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with permit(SAPI_TARGET, data=DATA) as committed:
        with pytest.raises(exact.ExactByteSendHold, match='content differs'):
            socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http,
                                         exact_target=SAPI_TARGET, image_url=URL,
                                         post_text=text, post_content_type=kind)
        assert not committed.consumed
    assert http.calls == []


@pytest.mark.parametrize('platform,target', [(Platform.INSTAGRAM, META_IG_TARGET),
                                            (Platform.FACEBOOK_PAGE, META_FB_TARGET)])
def test_meta_first_mutation_pins_approved_caption(monkeypatch, platform, target):
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    account = Account(platform, 'gym', target['account_id'])
    with permit(target) as committed:
        method = (meta_publisher._publish_instagram if platform == Platform.INSTAGRAM
                  else meta_publisher._publish_fb_page)
        with pytest.raises(exact.ExactByteSendHold, match='content differs'):
            method(http, account, draft(platform=platform), 'unapproved', 'tok')
        assert not committed.consumed
    assert http.calls == []


def test_socialapi_inherited_thread_context_cannot_use_continuation(sapi_env, monkeypatch):
    from contextvars import copy_context
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv(exact.FLAG, 'true')
    http = Http()
    with permit(SAPI_TARGET, data=DATA):
        socialapi_client.upload_media(DATA, 'card.jpg', 'image/jpeg', http=http,
                                     exact_target=SAPI_TARGET, image_url=URL, post_text='text')
        continuation = exact.active_continuation()
        inherited = copy_context()
        def steal():
            with pytest.raises(exact.ExactByteSendHold):
                socialapi_client.create_post('sa-1', 'text', ['mid'], http=http,
                                             exact_continuation=continuation)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(inherited.run, steal).result(timeout=5)
        assert not continuation.used
        socialapi_client.create_post('sa-1', 'text', ['mid'], http=http,
                                     exact_continuation=continuation)
    assert len(http.calls) == 2


def test_meta_inherited_async_task_cannot_publish_bound_container(monkeypatch):
    import asyncio
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setattr(meta_publisher, 'boundary', lambda *a, **kw: None)
    http = Http()
    async def owner():
        with permit(META_IG_TARGET) as committed:
            exact.require(META_IG_TARGET, [URL])
            cont = exact.bind_continuation(committed, META_IG_TARGET, 'returned-id', 'meta:media_publish')
            async def steal():
                with pytest.raises(exact.ExactByteSendHold):
                    meta_publisher._publish_container(http, 'https://meta.test', 'ig-123',
                                                      'returned-id', 'tok', exact_continuation=cont)
            await asyncio.create_task(steal())
            assert not cont.used
            meta_publisher._publish_container(http, 'https://meta.test', 'ig-123',
                                              'returned-id', 'tok', exact_continuation=cont)
    asyncio.run(owner())
    assert len(http.calls) == 1


@pytest.mark.parametrize('story', [False, True])
def test_actual_socialapi_publisher_threads_exact_upload_continuation(monkeypatch, story):
    from agent import socialapi_store, publish_billing_gate
    from test_socialapi_lane import _draft, _socialapi_account, _happy_http
    monkeypatch.setenv(exact.FLAG, 'true')
    monkeypatch.setenv('AGENT_PUBLISH_ENABLED', 'true')
    monkeypatch.setenv('AGENT_STORIES_ENABLED', 'true')
    monkeypatch.setenv('AGENT_SOCIALAPI_KEY', 'offline')
    monkeypatch.setattr(publish_billing_gate, 'publishing_blocked', lambda key: False)
    account = _socialapi_account()
    outgoing = _draft(is_story=story)
    socialapi_store.set_account_id(account.key, 'instagram', 'acc_1')
    http = _happy_http()
    target = {**SAPI_TARGET, 'account_id': 'acc_1'}
    with permit(target, url=outgoing.creative_public_url, data=b'PNGBYTES',
                post_format='story' if story else 'feed',
                caption=socialapi_publisher._compose_caption(outgoing)) as committed:
        assert socialapi_publisher.publish(outgoing, account, http=http).ok
        assert committed.consumed and exact.active_continuation().used
        assert exact.active_continuation().object_id == 'm_123'
        with pytest.raises((ProviderSendHold, exact.ExactByteSendHold)):
            socialapi_publisher.publish(outgoing, account, http=http)
    assert len([c for c in http.calls if c['method'] == 'POST']) == 2
