import hashlib
import io

import pytest

from agent.forward_media_attester import make_still_recipe
from agent.forward_media_guard import ForwardMediaVerificationHold
from agent.forward_media_thumbnail_candidate import prepare_thumbnail_candidate


SOURCE = b'original source image bytes'
IMAGE = b'delivered image bytes'
THUMB = b'deterministic thumbnail bytes'
THUMB_URL = 'https://media.example/thumb.jpg'
IMAGE_URL = 'https://media.example/image.jpg'


def bound(data):
    return {'thumbnail_sha256': hashlib.sha256(data).hexdigest(),
            'thumbnail_fingerprint': 'md5:' + hashlib.md5(data).hexdigest(),
            'thumbnail_length': len(data)}


def recipe(name='identity', thumb='identity'):
    # Inject replay only at the existing replay boundary for deterministic,
    # offline behavior. Runtime/version contract remains validated by helper.
    return make_still_recipe(name, thumbnail_name=thumb)


def manifest(data=THUMB, url=THUMB_URL, stage='identity', *, image_url=IMAGE_URL):
    return {'thumbnail_url': url, **bound(data), 'image_url': image_url,
            'render_recipe': recipe(thumb=stage)}


def snapshot(url=THUMB_URL, image_url=IMAGE_URL):
    return {'thumbnail_url': url, 'image_url': image_url}


def png(color):
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (3, 2), color).save(output, format='PNG')
    return output.getvalue()


def run(row=None, snap=None, *, returned=THUMB, source=SOURCE, image=IMAGE):
    calls = []
    def reader(url):
        calls.append(url)
        return returned
    result = prepare_thumbnail_candidate(snapshot=snap or snapshot(),
        manifest=row or manifest(), source_bytes=source, image_bytes=image,
        read_bytes=reader)
    return result, calls


def test_transformed_thumbnail_retains_verified_hosted_bytes(monkeypatch):
    monkeypatch.setattr('agent.forward_media_thumbnail_candidate.replay_still_recipe',
                        lambda source, recipe, has_thumbnail: {'thumbnail_bytes': THUMB})
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    result, calls = run()
    assert calls == [THUMB_URL]
    assert result.thumbnail_bytes == THUMB
    assert result.thumbnail_url == THUMB_URL
    assert result.thumbnail_sha256 == bound(THUMB)['thumbnail_sha256']


def test_delivered_image_alias_reuses_existing_bytes_without_second_fetch(monkeypatch):
    alias = manifest(data=IMAGE, url=IMAGE_URL, stage='delivered_image', image_url=IMAGE_URL)
    monkeypatch.setattr('agent.forward_media_thumbnail_candidate.replay_still_recipe',
                        lambda source, recipe, has_thumbnail: {'thumbnail_bytes': IMAGE})
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    result, calls = run(row=alias, snap=snapshot(IMAGE_URL, IMAGE_URL), returned=b'unused')
    assert calls == []
    assert result.thumbnail_bytes == IMAGE


def test_candidate_recipe_is_detached_and_immutable_after_preparation(monkeypatch):
    monkeypatch.setattr('agent.forward_media_thumbnail_candidate.replay_still_recipe',
                        lambda source, recipe, has_thumbnail: {'thumbnail_bytes': THUMB})
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    source_manifest = manifest()
    candidate, _ = run(row=source_manifest)
    before = candidate.thumbnail_recipe['name']
    source_manifest['render_recipe']['thumbnail']['name'] = 'delivered_image'
    assert candidate.thumbnail_recipe['name'] == before == 'identity'
    with pytest.raises(TypeError):
        candidate.thumbnail_recipe['name'] = 'delivered_image'


def test_replay_mutation_cannot_rebind_candidate_recipe_to_verified_bytes(monkeypatch):
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    source_manifest = manifest()
    def mutate_recipe(source, recipe, has_thumbnail):
        recipe['thumbnail']['name'] = 'delivered_image'
        return {'thumbnail_bytes': THUMB}
    monkeypatch.setattr('agent.forward_media_thumbnail_candidate.replay_still_recipe',
                        mutate_recipe)
    with pytest.raises(ForwardMediaVerificationHold, match='recipe changed during'):
        run(row=source_manifest)
    assert source_manifest['render_recipe']['thumbnail']['name'] == 'identity'


def test_wrong_original_cannot_produce_replay_match(monkeypatch):
    monkeypatch.setattr('agent.forward_media_thumbnail_candidate.replay_still_recipe',
                        lambda source, recipe, has_thumbnail: {'thumbnail_bytes': source})
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    with pytest.raises(ForwardMediaVerificationHold, match='deterministic recipe replay'):
        run(source=b'wrong original')


def test_swapped_hosted_bytes_fail_digest_binding(monkeypatch):
    monkeypatch.setattr('agent.forward_media_thumbnail_candidate.replay_still_recipe',
                        lambda source, recipe, has_thumbnail: {'thumbnail_bytes': THUMB})
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    with pytest.raises(ForwardMediaVerificationHold, match='persisted binding'):
        run(returned=b'swapped bytes')


def test_partial_nullable_tuple_holds():
    partial = manifest()
    partial['thumbnail_fingerprint'] = None
    # Validation stops on the incomplete tuple before any URL policy or read.
    with pytest.raises(ForwardMediaVerificationHold, match='partial or invalid'):
        run(row=partial)


def test_null_tuple_is_valid_only_without_thumbnail_recipe():
    empty = {'thumbnail_url': None, 'thumbnail_sha256': None,
             'thumbnail_fingerprint': None, 'thumbnail_length': None,
             'image_url': IMAGE_URL,
             'render_recipe': {'thumbnail': None}}
    result, calls = run(row=empty, snap=snapshot(None, IMAGE_URL))
    assert result.thumbnail_bytes is None
    assert calls == []


def test_real_still_replay_accepts_exact_original_thumbnail(monkeypatch):
    original = png((12, 34, 56))
    row = manifest(data=original, stage='identity')
    row['render_recipe'] = make_still_recipe('identity', thumbnail_name='identity')
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    calls = []
    result = prepare_thumbnail_candidate(snapshot=snapshot(), manifest=row,
        source_bytes=original, image_bytes=IMAGE,
        read_bytes=lambda url: calls.append(url) or original)
    assert result.thumbnail_bytes == original
    assert calls == [THUMB_URL]


def test_real_still_replay_rejects_wrong_original_bytes(monkeypatch):
    original = png((12, 34, 56))
    row = manifest(data=original, stage='identity')
    row['render_recipe'] = make_still_recipe('identity', thumbnail_name='identity')
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    with pytest.raises(ForwardMediaVerificationHold, match='deterministic recipe replay'):
        prepare_thumbnail_candidate(snapshot=snapshot(), manifest=row,
            source_bytes=png((56, 34, 12)), image_bytes=IMAGE,
            read_bytes=lambda url: original)


def test_real_still_replay_rejects_stale_renderer_runtime(monkeypatch):
    original = png((12, 34, 56))
    row = manifest(data=original, stage='identity')
    row['render_recipe'] = make_still_recipe('identity', thumbnail_name='identity')
    row['render_recipe']['runtime']['pillow'] = 'stale'
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    with pytest.raises(ForwardMediaVerificationHold, match='runtime changed'):
        prepare_thumbnail_candidate(snapshot=snapshot(), manifest=row,
            source_bytes=original, image_bytes=IMAGE,
            read_bytes=lambda url: original)


def test_manifest_snapshot_url_swap_holds():
    with pytest.raises(ForwardMediaVerificationHold, match='URL differs'):
        run(snap=snapshot('https://media.example/swapped.jpg'))


def test_stale_renderer_holds_before_fetch(monkeypatch):
    monkeypatch.setattr('agent.forward_media_thumbnail_candidate.replay_still_recipe',
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            ForwardMediaVerificationHold('still renderer runtime changed')))
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url', lambda url: True)
    with pytest.raises(ForwardMediaVerificationHold, match='runtime changed'):
        run()
