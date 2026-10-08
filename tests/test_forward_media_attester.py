"""Production trusted attester callbacks: registry-bound provenance and
deterministic render replay. No DB, network or provider sends."""
import hashlib
import os

import pytest

from agent import forward_media_attester as attester
from agent.forward_media_guard import ForwardMediaVerificationHold


def test_story_replay_removes_nested_render_tree(monkeypatch):
    from agent import story_image
    observed = {}
    def render(_src, _caption, _gym, out_dir):
        observed['out_dir'] = out_dir
        nested = os.path.join(out_dir, 'reels')
        os.mkdir(nested)
        output = os.path.join(nested, 'render.jpg')
        with open(output, 'wb') as fh:
            fh.write(b'replayed')
        return output
    monkeypatch.setattr(story_image, 'get_or_make_story_image', render)
    assert attester._story_burn(b'source', 'caption', 'gym', 'photo.jpg') == b'replayed'
    assert not os.path.exists(observed['out_dir'])

TENANT = 'tenant-alpha'
ASSET = 'asset-1'
SOURCE_URL = 'https://owned.example/media/source-v1.jpg'
IMAGE_URL = 'https://owned.example/media/story-v1.jpg'
THUMB_URL = 'https://owned.example/media/thumb-v1.jpg'
DIGEST = 'sha256:' + 'ab' * 32
SOURCE = b'exact original bytes'
IMAGE = b'deterministic burn output'


def fp(data):
    return 'md5:' + hashlib.md5(data).hexdigest()


def registry_row(**over):
    row = {'tenant_id': TENANT, 'source_asset_id': ASSET, 'source_url': SOURCE_URL,
           'source_fingerprint': fp(SOURCE), 'source_length': len(SOURCE)}
    row.update(over)
    return row


def manifest_row(**over):
    row = {'tenant_id': TENANT, 'source_asset_id': ASSET, 'image_url': IMAGE_URL,
           'manifest_digest': DIGEST,
           'thumbnail_url': None, 'operation': 'reburn',
           'render_recipe': {'caption': 'Big day', 'gym_name': 'Alpha Gym'}}
    row.update(over)
    return row


def snapshot(**over):
    snap = {'tenant_id': TENANT, 'source_asset_id': ASSET,
            'source_url': SOURCE_URL, 'image_url': IMAGE_URL,
            'thumbnail_url': None, 'render_manifest_digest': DIGEST,
            'revision': 'rev-1'}
    snap.update(over)
    return snap


def lookup_of(row):
    return lambda *args: row


def make_verifier(row=registry_row(), **kw):
    return attester.make_original_verifier(lookup_of(row), **kw)


def make_renderer(row=manifest_row(), **kw):
    kw.setdefault('burn', lambda source, caption, gym, url: IMAGE)
    return attester.make_controlled_renderer(lookup_of(row), **kw)


# --- original provenance ----------------------------------------------------

def test_valid_original_returns_literal_true():
    assert make_verifier()(snapshot(), SOURCE) is True


def test_missing_registry_row_holds():
    with pytest.raises(ForwardMediaVerificationHold, match='registry'):
        make_verifier(None)(snapshot(), SOURCE)


def test_forged_registry_fingerprint_holds():
    forged = registry_row(source_fingerprint='md5:' + '0' * 32)
    with pytest.raises(ForwardMediaVerificationHold, match='bytes changed'):
        make_verifier(forged)(snapshot(), SOURCE)


def test_registry_url_mismatch_holds():
    other = registry_row(source_url='https://owned.example/media/other.jpg')
    with pytest.raises(ForwardMediaVerificationHold, match='immutable original'):
        make_verifier(other)(snapshot(), SOURCE)


def test_changed_object_bytes_hold():
    with pytest.raises(ForwardMediaVerificationHold):
        make_verifier()(snapshot(), SOURCE + b'tampered')


def test_tenant_mismatch_holds():
    with pytest.raises(ForwardMediaVerificationHold, match='tenant'):
        make_verifier(registry_row(tenant_id='tenant-beta'))(snapshot(), SOURCE)
    with pytest.raises(ForwardMediaVerificationHold, match='tenant'):
        make_verifier()(snapshot(tenant_id='tenant-beta'), SOURCE)


def test_stale_revision_holds():
    verifier = make_verifier(expected_revision='rev-1')
    assert verifier(snapshot(revision='rev-1'), SOURCE) is True
    with pytest.raises(ForwardMediaVerificationHold, match='stale'):
        verifier(snapshot(revision='rev-2'), SOURCE)


def test_missing_asset_identity_holds():
    with pytest.raises(ForwardMediaVerificationHold, match='asset identity'):
        make_verifier()(snapshot(source_asset_id=None), SOURCE)


def test_manifest_digest_or_asset_mismatch_holds():
    with pytest.raises(ForwardMediaVerificationHold, match='source or revision'):
        make_renderer(manifest_row(manifest_digest='sha256:' + '0' * 64))(SOURCE, snapshot())
    with pytest.raises(ForwardMediaVerificationHold, match='source or revision'):
        make_renderer(manifest_row(source_asset_id='other'))(SOURCE, snapshot())


# --- rehost / same-object need no renderer -----------------------------------

def test_rehost_still_requires_valid_original():
    # Identical bytes at a new URL attest without any render claim, but the
    # source object must still be the registered original.
    assert make_verifier()(snapshot(image_url='https://owned.example/copy.jpg'),
                           SOURCE) is True


# --- controlled renderer ------------------------------------------------------

def test_supported_reburn_replays_deterministically():
    seen = {}

    def burn(source, caption, gym, url):
        seen.update(source=source, caption=caption, gym=gym, url=url)
        return IMAGE

    renderer = make_renderer(burn=burn)
    result = renderer(SOURCE, snapshot())
    assert result == {'operation': 'reburn', 'image_bytes': IMAGE,
                      'thumbnail_bytes': None}
    assert seen == {'source': SOURCE, 'caption': 'Big day', 'gym': 'Alpha Gym',
                    'url': SOURCE_URL}


def test_same_object_thumbnail_replays_to_same_bytes():
    renderer = make_renderer(manifest_row(thumbnail_url=IMAGE_URL))
    result = renderer(SOURCE, snapshot(thumbnail_url=IMAGE_URL))
    assert result['thumbnail_bytes'] == IMAGE


def test_missing_manifest_holds():
    with pytest.raises(ForwardMediaVerificationHold, match='no persisted manifest'):
        make_renderer(None)(SOURCE, snapshot())


def test_missing_digest_in_snapshot_holds():
    with pytest.raises(ForwardMediaVerificationHold, match='manifest digest'):
        make_renderer()(SOURCE, snapshot(render_manifest_digest=None))


def test_manifest_tenant_mismatch_holds():
    with pytest.raises(ForwardMediaVerificationHold, match='tenant'):
        make_renderer(manifest_row(tenant_id='tenant-beta'))(SOURCE, snapshot())


def test_manifest_image_version_mismatch_holds():
    row = manifest_row(image_url='https://owned.example/media/other.jpg')
    with pytest.raises(ForwardMediaVerificationHold, match='manifest version'):
        make_renderer(row)(SOURCE, snapshot())


def test_missing_recipe_holds_and_documents_fields():
    row = manifest_row(render_recipe=None)
    with pytest.raises(ForwardMediaVerificationHold, match='caption'):
        make_renderer(row)(SOURCE, snapshot())


def test_unsupported_operation_holds_with_recipe_note():
    row = manifest_row(operation='feed_card_autofit')
    with pytest.raises(ForwardMediaVerificationHold, match='no deterministic replay'):
        make_renderer(row)(SOURCE, snapshot())


def test_transformed_thumbnail_holds_without_recipe():
    row = manifest_row(thumbnail_url=THUMB_URL)
    with pytest.raises(ForwardMediaVerificationHold, match='thumbnail'):
        make_renderer(row)(SOURCE, snapshot(thumbnail_url=THUMB_URL))


def test_failed_replay_holds():
    renderer = make_renderer(burn=lambda *args: None)
    with pytest.raises(ForwardMediaVerificationHold, match='determinism'):
        renderer(SOURCE, snapshot())


def test_production_callbacks_use_narrow_exact_row_rpc():
    calls = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def execute(self, sql, args): calls.append((sql, args))
        def fetchone(self):
            return ({'original': registry_row(),
                     'manifest': manifest_row()},)

    class Conn:
        def cursor(self): return Cursor()

    row_id = '47b0460a-e931-4492-a6b0-8b7d4e0af739'
    verifier, renderer = attester.production_callbacks(
        Conn(), row_id, expected_revision='rev-1')
    assert verifier(snapshot(), SOURCE) is True
    assert callable(renderer)
    assert len(calls) == 1
    assert 'fixer_forward_media_provenance_lookup_20261006' in calls[0][0]
    assert 'from public.' not in calls[0][0]
    assert calls[0][1] == (row_id,)


def alias_callbacks(*, mutate=None, binding=True):
    row_id = '47b0460a-e931-4492-a6b0-8b7d4e0af739'
    raw = 'raw-alpha'
    captured = snapshot(calendar_row_id=row_id, gym_id=raw)
    provenance = {'original': registry_row(tenant_id=raw),
                  'manifest': manifest_row(tenant_id=raw)}
    if binding:
        provenance['staged_alias_binding'] = {
            'authority_tenant_id': raw, 'snapshot': dict(captured)}
    if mutate:
        mutate(provenance)

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def execute(self, sql, args):
            assert sql == 'select public.fixer_forward_media_provenance_lookup_20261006(%s)'
            assert args == (row_id,)
        def fetchone(self): return (provenance,)

    class Conn:
        def cursor(self): return Cursor()

    return captured, provenance, attester.production_callbacks(
        Conn(), row_id, expected_revision='rev-1')


def test_production_alias_bridge_preserves_raw_authority_and_replays(monkeypatch):
    monkeypatch.setattr(attester, '_story_burn', lambda *args: IMAGE)
    captured, provenance, (verify, render) = alias_callbacks()
    assert verify(captured, SOURCE) is True
    assert render(SOURCE, captured)['image_bytes'] == IMAGE
    assert captured['tenant_id'] == TENANT
    assert provenance['original']['tenant_id'] == 'raw-alpha'
    assert provenance['manifest']['tenant_id'] == 'raw-alpha'
    with pytest.raises(ForwardMediaVerificationHold, match='bytes changed'):
        verify(captured, b'X' + SOURCE[1:])


@pytest.mark.parametrize('field,value', [
    ('tenant_id', 'foreign'), ('gym_id', 'foreign'), ('revision', 'rev-2'),
    ('calendar_row_id', 'f696efc9-6087-4222-bd42-09222e65bede'),
    ('source_asset_id', 'foreign'), ('source_url', 'https://other/source'),
    ('image_url', 'https://other/image'), ('thumbnail_url', THUMB_URL),
    ('render_manifest_digest', 'sha256:' + '0' * 64),
])
def test_production_alias_bridge_rejects_any_snapshot_drift(field, value):
    captured, _, (verify, render) = alias_callbacks()
    captured[field] = value
    with pytest.raises(ForwardMediaVerificationHold, match='alias snapshot changed'):
        verify(captured, SOURCE)
    with pytest.raises(ForwardMediaVerificationHold, match='alias snapshot changed'):
        render(SOURCE, captured)


@pytest.mark.parametrize('mutate', [
    lambda p: p['original'].update(tenant_id='foreign'),
    lambda p: p['manifest'].update(tenant_id='foreign'),
    lambda p: p['staged_alias_binding'].update(authority_tenant_id='foreign'),
    lambda p: p['staged_alias_binding']['snapshot'].update(revision='rev-2'),
    lambda p: p['staged_alias_binding']['snapshot'].update(calendar_row_id='foreign'),
    lambda p: p['staged_alias_binding']['snapshot'].update(tenant_id='raw-alpha'),
    lambda p: p['staged_alias_binding'].update(unverified=True),
    lambda p: p.update(staged_alias_binding=None),
])
def test_production_alias_bridge_rejects_malformed_rpc_authority(mutate):
    with pytest.raises(ForwardMediaVerificationHold, match='alias authority binding'):
        alias_callbacks(mutate=mutate)


def test_production_raw_authority_without_verified_alias_bridge_holds():
    captured, _, (verify, _) = alias_callbacks(binding=False)
    with pytest.raises(ForwardMediaVerificationHold, match='registry'):
        verify(captured, SOURCE)
