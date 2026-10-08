"""Real still renderer replay and owner preparation. No DB/network/provider writes."""
import copy
import io
import json

import pytest
from PIL import Image

from agent import feed_image, gbp, story_image
from agent import forward_media_attester as attester
from agent import forward_media_owner_packet as owner
from agent.forward_media_guard import ForwardMediaVerificationHold

SOURCE_URL = 'https://owned.example/original'
IMAGE_URL = 'https://owned.example/image.jpg'
THUMB_URL = 'https://owned.example/thumbnail.jpg'


def source_bytes(fmt='JPEG', size=(1600, 700)):
    image = Image.new('RGB', size, (48, 97, 142))
    # Nonuniform image catches a crop/contain change that a solid tile would miss.
    for x in range(0, size[0], 80):
        image.paste((x % 255, 120, 90), (x, 50, min(x + 40, size[0]), 200))
    buf = io.BytesIO()
    image.save(buf, fmt)
    return buf.getvalue()


def packet(recipe, *, thumbnail=False, operation='render'):
    return owner.validate_packet({
        'schema_version': 2, 'tenant_id': 'tenant-alpha', 'source_asset_id': 'asset-1',
        'source_url': SOURCE_URL, 'image_url': IMAGE_URL,
        'thumbnail_url': THUMB_URL if thumbnail else None,
        'registry_evidence_ref': 'owner-original-review',
        'render_evidence_ref': 'owner-render-review',
        'decision': 'hold_uncertain', 'history_evidence_ref': 'independent-audit',
        'operation': operation, 'render_recipe': recipe})


class Reader:
    def __init__(self, objects): self.objects = objects
    def read(self, url): return self.objects[url]


@pytest.mark.parametrize('name,size,fmt', [
    ('feed_autofit_4x5', (1600, 700), 'JPEG'),
    ('feed_autofit_4x5', (600, 1600), 'PNG'),
    ('story_photo', (1600, 700), 'WEBP'),
    ('gbp_crop_4x3', (1600, 700), 'PNG'),
])
def test_owner_packet_exact_replay_matches_real_production_renderer(tmp_path, name, size, fmt):
    source = source_bytes(fmt=fmt, size=size)
    recipe = attester.make_still_recipe(name, caption='Train together today.', gym_name='Alpha Gym')
    local = tmp_path / 'original'
    local.write_bytes(source)
    output = tmp_path / 'actual.jpg'
    if name == 'feed_autofit_4x5':
        feed_image.build_feed_image(str(local), str(output))
    elif name == 'story_photo':
        story_image.build_story_image(str(local), str(output), caption='Train together today.',
                                      gym_name='Alpha Gym')
    else:
        gbp.crop_4x3(str(local), str(output))
    delivered = output.read_bytes()
    data = packet(recipe)
    original, clearance, manifest = owner.build_tuples(data,
        Reader({SOURCE_URL: source, IMAGE_URL: delivered}))
    assert original.source_length == len(source)
    assert clearance.decision == 'hold_uncertain'
    assert manifest.render_recipe == recipe and manifest.operation == 'render'
    snapshot = {'tenant_id': 'tenant-alpha', 'source_asset_id': 'asset-1',
                'source_url': SOURCE_URL, 'image_url': IMAGE_URL, 'thumbnail_url': None,
                'render_manifest_digest': manifest.manifest_digest}
    controlled = attester.make_controlled_renderer(lambda *_: manifest.row())
    assert controlled(source, snapshot) == {
        'operation': 'render', 'image_bytes': delivered, 'thumbnail_bytes': None}


def test_distinct_thumbnail_recipe_reproduces_both_objects():
    source = source_bytes()
    recipe = attester.make_still_recipe('story_photo', caption='Keep moving.',
                                      gym_name='Gym', thumbnail_name='gbp_crop_4x3')
    rendered = attester.replay_still_recipe(source, recipe, has_thumbnail=True)
    assert rendered['image_bytes'] != rendered['thumbnail_bytes']
    original, _, manifest = owner.build_tuples(packet(recipe, thumbnail=True), Reader({
        SOURCE_URL: source, IMAGE_URL: rendered['image_bytes'],
        THUMB_URL: rendered['thumbnail_bytes']}))
    assert manifest.thumbnail_fingerprint != manifest.image_fingerprint
    assert original.source_fingerprint not in (manifest.image_fingerprint,
                                               manifest.thumbnail_fingerprint)


def test_delivered_image_thumbnail_alias_and_raw_thumbnail():
    source = source_bytes()
    for name, expected_source in [('delivered_image', False), ('identity', True)]:
        recipe = attester.make_still_recipe('gbp_crop_4x3', thumbnail_name=name)
        rendered = attester.replay_still_recipe(source, recipe, has_thumbnail=True)
        assert rendered['thumbnail_bytes'] == (source if expected_source else rendered['image_bytes'])
        assert owner.build_tuples(packet(recipe, thumbnail=True), Reader({
            SOURCE_URL: source, IMAGE_URL: rendered['image_bytes'],
            THUMB_URL: rendered['thumbnail_bytes']}))[2].thumbnail_url == THUMB_URL


@pytest.mark.parametrize('field', ['image_bytes', 'thumbnail_bytes'])
def test_delivered_object_mismatch_never_prepares(field):
    source = source_bytes()
    recipe = attester.make_still_recipe('gbp_crop_4x3', thumbnail_name='identity')
    rendered = attester.replay_still_recipe(source, recipe, has_thumbnail=True)
    rendered[field] += b'changed after upload'
    with pytest.raises(owner.PacketError, match='render_bytes_mismatch'):
        owner.build_tuples(packet(recipe, thumbnail=True), Reader({
            SOURCE_URL: source, IMAGE_URL: rendered['image_bytes'],
            THUMB_URL: rendered['thumbnail_bytes']}))


def test_runtime_drift_unknown_version_and_injected_fields_hold():
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    variants = []
    changed = copy.deepcopy(recipe)
    changed['runtime']['pillow'] = 'other-version'
    variants.append(changed)
    for version in (2, True):
        changed = copy.deepcopy(recipe)
        changed['version'] = version
        variants.append(changed)
    changed = copy.deepcopy(recipe)
    changed['image']['quality'] = 1
    variants.append(changed)
    changed = copy.deepcopy(recipe)
    changed['image']['name'] = 'heic_to_jpeg'
    variants.append(changed)
    for changed in variants:
        with pytest.raises(owner.PacketError, match='operation_held'):
            packet(changed)
        with pytest.raises(ForwardMediaVerificationHold):
            attester.replay_still_recipe(source_bytes(), changed)


@pytest.mark.parametrize('source', [b'HEIC placeholder', b'video placeholder',
                                   source_bytes('GIF')])
def test_unsupported_source_formats_hold(source):
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    with pytest.raises(ForwardMediaVerificationHold):
        attester.replay_still_recipe(source, recipe)


def test_animated_webp_holds():
    buf = io.BytesIO()
    Image.new('RGB', (700, 500), 'red').save(buf, 'WEBP', save_all=True,
        append_images=[Image.new('RGB', (700, 500), 'blue')], duration=100, loop=0)
    with pytest.raises(ForwardMediaVerificationHold, match='source format'):
        attester.replay_still_recipe(buf.getvalue(), attester.make_still_recipe('gbp_crop_4x3'))


def test_thumbnail_binding_missing_recipe_holds():
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    with pytest.raises(ForwardMediaVerificationHold, match='thumbnail'):
        attester.replay_still_recipe(source_bytes(), recipe, has_thumbnail=True)


def test_feed_preflight_not_in_spec_image_and_gbp_minimum_hold():
    with pytest.raises(ForwardMediaVerificationHold, match='autofit'):
        attester.replay_still_recipe(source_bytes(size=(800, 800)),
                                    attester.make_still_recipe('feed_autofit_4x5'))
    with pytest.raises(ForwardMediaVerificationHold):
        attester.replay_still_recipe(source_bytes(size=(100, 100)),
                                    attester.make_still_recipe('gbp_crop_4x3'))


def test_packet_factory_emits_explicit_v2_contract_and_never_clears_history():
    common = dict(tenant_id='tenant-alpha', source_asset_id='asset-1', source_url=SOURCE_URL,
        registry_evidence_ref='owner-review', render_evidence_ref='render-review',
        decision='hold_uncertain', history_evidence_ref='history-review')
    data = owner.make_still_packet(**common, image_url=IMAGE_URL, image_name='gbp_crop_4x3')
    assert data['schema_version'] == 2 and data['operation'] == 'render'
    assert data['decision'] == 'hold_uncertain' and 'production_evidence_ref' not in data
    data = owner.make_still_packet(**common, image_url=SOURCE_URL)
    assert data['operation'] == 'same_object'
    data = owner.make_still_packet(**common, image_url=IMAGE_URL)
    assert data['operation'] == 'rehost'
    common['decision'] = 'cleared_unused'
    with pytest.raises(owner.PacketError, match='fresh_receipts_required'):
        owner.make_still_packet(**common, image_url=IMAGE_URL)


def test_schema1_and_missing_v2_recipe_remain_held():
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    data = packet(recipe)
    for overrides in ({'schema_version': 1}, {'render_recipe': None},
                      {'render_recipe': {'caption': 'old', 'gym_name': 'old'}}):
        with pytest.raises(owner.PacketError, match='operation_held'):
            owner.validate_packet({**data, **overrides})


def test_new_attester_recipe_cannot_fall_back_to_legacy_caption_burn():
    source = source_bytes()
    recipe = attester.make_still_recipe('story_photo', caption='Hi', gym_name='Gym')
    recipe['name'] = 'unknown_recipe'
    # A named recipe with legacy fields must still hold.
    recipe.update(caption='legacy', gym_name='legacy')
    row = dict(tenant_id='tenant-alpha', source_asset_id='asset-1', image_url=IMAGE_URL,
               thumbnail_url=None, manifest_digest='digest', operation='render', render_recipe=recipe)
    snap = dict(row, source_url=SOURCE_URL, render_manifest_digest='digest')
    called = []
    renderer = attester.make_controlled_renderer(lambda *_: row,
        burn=lambda *args: called.append(args) or b'forged')
    with pytest.raises(ForwardMediaVerificationHold):
        renderer(source, snap)
    assert not called


def test_reburn_only_story_and_render_identity_classification_hold():
    source = source_bytes()
    recipe = attester.make_still_recipe('identity')
    with pytest.raises(owner.PacketError, match='preparation_invalid'):
        owner.build_tuples(packet(recipe), Reader({SOURCE_URL: source, IMAGE_URL: source}))
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    image = attester.replay_still_recipe(source, recipe)['image_bytes']
    with pytest.raises(owner.PacketError, match='operation_held'):
        owner.build_tuples(packet(recipe, operation='reburn'), Reader({
            SOURCE_URL: source, IMAGE_URL: image}))


def test_default_cli_dry_run_and_mismatch_never_open_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_DSN', 'postgresql://owner@localhost/db')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_ROLE', 'forward_media_owner_20261006')
    source = source_bytes()
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    image = attester.replay_still_recipe(source, recipe)['image_bytes']
    path = tmp_path / 'packet.json'
    path.write_text(json.dumps(packet(recipe)))
    opened = []
    reader = Reader({SOURCE_URL: source, IMAGE_URL: image})
    code, result = owner.run(['--packet', str(path)], reader_factory=lambda: reader,
        persistence_factory=lambda: opened.append(True), out=io.StringIO())
    assert code == 0 and result['applied'] is False and not opened
    reader.objects[IMAGE_URL] = b'changed'
    code, result = owner.run(['--packet', str(path), '--apply'], reader_factory=lambda: reader,
        persistence_factory=lambda: opened.append(True), out=io.StringIO())
    assert code == 2 and result == {'ok': False, 'reason': 'render_bytes_mismatch'} and not opened


def test_reburn_replays_story_and_recipe_copy_is_not_mutable_alias():
    source = source_bytes()
    recipe = attester.make_still_recipe('story_photo', caption='A fresh caption.', gym_name='Gym')
    data = packet(recipe, operation='reburn')
    # Structural validation detaches the submitted recipe before replay/persist.
    recipe['image']['caption'] = 'Changed caller input'
    assert data['render_recipe']['image']['caption'] == 'A fresh caption.'
    image = attester.replay_still_recipe(source, data['render_recipe'])['image_bytes']
    _, _, manifest = owner.build_tuples(data, Reader({SOURCE_URL: source, IMAGE_URL: image}))
    assert manifest.operation == 'reburn'
    snap = dict(tenant_id='tenant-alpha', source_asset_id='asset-1', source_url=SOURCE_URL,
        image_url=IMAGE_URL, thumbnail_url=None, render_manifest_digest=manifest.manifest_digest)
    assert attester.make_controlled_renderer(lambda *_: manifest.row())(source, snap)['image_bytes'] == image


def test_attester_reburn_non_story_recipe_holds_and_pixel_bound_holds(monkeypatch):
    source = source_bytes()
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    data = packet(recipe, operation='reburn')
    row = dict(data, manifest_digest='digest')
    snap = dict(row, render_manifest_digest='digest')
    with pytest.raises(ForwardMediaVerificationHold, match='Story'):
        attester.make_controlled_renderer(lambda *_: row)(source, snap)
    monkeypatch.setattr(attester, '_MAX_PIXELS', 100)
    with pytest.raises(ForwardMediaVerificationHold, match='source format'):
        attester.replay_still_recipe(source, recipe)
