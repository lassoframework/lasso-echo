"""Exact local reservation mutation bridge checks; no remote calls."""
import json
import sqlite3
import uuid

import pytest

from agent import config, db, local_inventory_mutation as mutation, rotation


class Authority:
    def __init__(self, fail_complete=False):
        self.events = []
        self.fail_complete = fail_complete

    def begin(self, request):
        self.events.append('begin')
        self.receipt = dict(request, state='pending', generation=1,
                            result_digest=None, begun_at='now')
        return dict(self.receipt)

    def complete(self, request, result_digest):
        self.events.append('complete')
        if self.fail_complete:
            raise mutation.MutationHold('uncertain')
        return dict(self.receipt, state='complete', result_digest=result_digest,
                    completed_at='later')

    def close(self):
        pass


@pytest.fixture
def armed(tmp_path, monkeypatch):
    library = tmp_path / 'library'
    library.mkdir()
    monkeypatch.setattr(config, 'LIBRARY_PATH', str(library))
    monkeypatch.setenv('AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED', 'true')
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setenv('AGENT_ROTATION_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('LOCAL_INVENTORY_MUTATION_EPOCH', str(uuid.uuid4()))
    conn = db.connect()
    conn.close()
    gym = library / 'gymx'
    gym.mkdir()
    asset = gym / 'photo.jpg'
    asset.write_bytes(b'photo bytes')
    auth = Authority()
    monkeypatch.setattr(mutation.MutationAuthority, 'from_environment', lambda: auth)
    return asset, auth, tmp_path / 'echo.db'


def reserve(asset, account='gymx_ig'):
    return rotation.reserve_local_media_once(account, asset.name, 'p1', '2026-10-08',
                                            path=str(asset))


def rows(database):
    with sqlite3.connect(database) as conn:
        return conn.execute('SELECT id,account_key,key,content_hash FROM served').fetchall()


def test_exact_row_commit_and_same_gym_lane_hash_alias_hold(armed):
    asset, auth, database = armed
    reservation = reserve(asset)
    assert rows(database) == [(reservation, 'gymx_ig', asset.name,
                               rotation.local_content_hash(asset))]
    alias = asset.with_name('alias.jpg')
    alias.write_bytes(asset.read_bytes())
    assert reserve(alias, 'gymx_fb') is None
    assert len(rows(database)) == 1
    assert auth.events == ['begin', 'complete', 'begin', 'complete']


def test_neighbor_ledger_survives_and_cross_gym_binding_refused(armed):
    asset, auth, database = armed
    with sqlite3.connect(database) as conn:
        conn.execute('INSERT INTO served(account_key,key,content_hash) VALUES (?,?,?)',
                     ('neighbor_ig', asset.name, rotation.local_content_hash(asset)))
    assert reserve(asset)
    with pytest.raises(mutation.MutationHold, match='binding_invalid'):
        reserve(asset, 'neighbor_ig')
    assert len(rows(database)) == 2


def test_completion_uncertainty_commits_history_and_blocks_retry(armed):
    asset, auth, database = armed
    auth.fail_complete = True
    with pytest.raises(mutation.MutationHold, match='pending_reconciliation'):
        reserve(asset)
    assert len(rows(database)) == 1
    with pytest.raises(mutation.MutationHold, match='reconciliation_required'):
        reserve(asset)
    assert auth.events == ['begin', 'complete']


def test_release_requires_bound_identity_and_reads_exact_deletion(armed):
    asset, auth, database = armed
    reservation = reserve(asset)
    with pytest.raises(mutation.MutationHold, match='binding_invalid'):
        rotation.release_served(reservation)
    with pytest.raises(mutation.MutationHold, match='binding_invalid'):
        rotation.release_served(reservation, account_key='neighbor_ig', key=asset.name,
                                path=str(asset), content_hash=rotation.local_content_hash(asset))
    assert len(rows(database)) == 1
    assert rotation.release_served(reservation, account_key='gymx_ig', key=asset.name,
                                   path=str(asset), content_hash=rotation.local_content_hash(asset))
    assert rows(database) == []


def test_corrupt_sidecar_holds_before_authority(armed):
    asset, auth, database = armed
    asset.with_suffix('.json').write_text('broken')
    with pytest.raises(mutation.MutationHold, match='sidecar_invalid'):
        reserve(asset)
    assert auth.events == []
    assert rows(database) == []


def test_flag_off_preserves_legacy_missing_path_and_id_release(armed, monkeypatch):
    asset, auth, database = armed
    monkeypatch.setenv('AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED', 'false')
    reservation = rotation.reserve_local_media_once('gymx_ig', 'legacy.jpg', 'p1',
                                                    '2026-10-08')
    assert reservation
    assert rotation.release_served(reservation)
    assert auth.events == []
    assert rows(database) == []


def test_armed_reservation_imports_prior_legacy_history_without_reuse(armed):
    asset, auth, database = armed
    legacy = database.parent / 'rotation_served.json'
    legacy.write_text(json.dumps({
        'gymx_fb': [{'key': asset.name, 'pillar': 'old', 'date': '2020-01-01'}],
        'neighbor_ig': [{'key': 'neighbor.jpg', 'date': '2020-01-01'}],
    }))
    before = legacy.read_bytes()
    assert reserve(asset) is None
    assert rows(database) == [(1, 'gymx_fb', asset.name, '')]
    assert legacy.read_bytes() == before
    assert reserve(asset) is None
    assert len(rows(database)) == 1
    assert auth.events == ['begin', 'complete', 'begin', 'complete']


def test_legacy_history_merges_without_erasing_existing_sqlite_or_hash_history(armed):
    asset, auth, database = armed
    with sqlite3.connect(database) as conn:
        conn.execute('INSERT INTO served(account_key,key,content_hash) VALUES (?,?,?)',
                     ('neighbor_ig', 'neighbor.jpg', 'neighbor-hash'))
    legacy = database.parent / 'rotation_served.json'
    legacy.write_text(json.dumps({'gymx': [{'key': 'old-name.jpg',
        'date': '2020-01-01', 'content_hash': rotation.local_content_hash(asset)}]}))
    assert reserve(asset) is None
    assert len(rows(database)) == 2
    assert rows(database)[0] == (1, 'neighbor_ig', 'neighbor.jpg', 'neighbor-hash')


def test_invalid_legacy_history_holds_before_begin(armed):
    asset, auth, database = armed
    (database.parent / 'rotation_served.json').write_text('broken')
    with pytest.raises(mutation.MutationHold, match='legacy_history_unavailable'):
        reserve(asset)
    assert auth.events == []
    assert rows(database) == []


def test_legacy_import_rolls_back_on_asset_change_and_remains_pending(armed, monkeypatch):
    asset, auth, database = armed
    legacy = database.parent / 'rotation_served.json'
    legacy.write_text(json.dumps({'gymx': [{'key': asset.name, 'date': '2020-01-01'}]}))
    original_begin = auth.begin
    def begin(request):
        receipt = original_begin(request)
        legacy.write_text('{}')
        return receipt
    monkeypatch.setattr(auth, 'begin', begin)
    with pytest.raises(mutation.MutationHold, match='pending_reconciliation'):
        reserve(asset)
    assert rows(database) == []
    assert auth.events == ['begin']
    with pytest.raises(mutation.MutationHold, match='reconciliation_required'):
        reserve(asset)
