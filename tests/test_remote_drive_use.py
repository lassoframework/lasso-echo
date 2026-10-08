"""Disposable SQLite and fake transport checks; no production calls."""
from copy import deepcopy
import fcntl
from pathlib import Path
import sqlite3
import uuid

import pytest

from agent import config as settings
from agent.local_inventory_mutation import MutationConfig, MutationHold
from agent import remote_drive_use as remote


@pytest.fixture
def lane(tmp_path, monkeypatch):
    library = tmp_path/'library'
    gym = library/'gym'
    gym.mkdir(parents=True)
    database = tmp_path/'test.db'
    sqlite3.connect(database).close()
    monkeypatch.setattr(settings, 'LIBRARY_PATH', str(library))
    monkeypatch.setenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED', 'true')
    cfg = MutationConfig('gym', str(uuid.uuid4()), gym, database, tmp_path/'journals', tmp_path/'locks')
    asset = dict(id='asset',source_id='source',gym_id='gym',content_hash='md5bytes',drive_use_version=1,
                 used_count=0,last_used_at=None,eligible=True,excluded_by_coach=False)
    source = dict(id='source',gym_id='gym',drive_use_version=2,kind='gym_drive',active=True)
    request = remote.request_for(asset, source, gym_id='gym', epoch_id=cfg.epoch_id,
                                 post_date='2026-10-10', use_id=str(uuid.uuid4()))
    return cfg, request


class Authority:
    def __init__(self, lose=False):
        self.calls=[]
        self.result=None
        self.lose=lose

    def apply_use(self, request):
        self.calls.append('apply')
        after=dict(request['asset_before'], used_count=1, last_used_at='2026-10-08T12:00:00+00:00',drive_use_version=3)
        self.result=dict(state='applied',request=deepcopy(request),asset_after=after,source_after=deepcopy(request['source_before']))
        if self.lose:
            raise OSError('lost commit ack')
        return self.result

    def use_receipt(self, request):
        self.calls.append('receipt')
        if self.result is None:
            raise OSError('no authoritative receipt')
        return self.result


def state(cfg):
    with sqlite3.connect(cfg.sqlite_path) as c:
        return c.execute('select state from remote_drive_use_attempt').fetchall()


def test_replay_and_frozen_request(lane):
    cfg, request=lane
    authority=Authority()
    result=remote.apply(cfg,authority,request)
    assert remote.apply(cfg,authority,request)==result
    assert authority.calls==['apply']
    assert state(cfg)==[('confirmed',)]


def test_unknown_commit_only_reads_exact_receipt(lane):
    cfg,request=lane
    authority=Authority(lose=True)
    with pytest.raises(MutationHold,match='outcome_unknown'):
        remote.apply(cfg,authority,request)
    assert state(cfg)==[('unknown',)]
    assert remote.apply(cfg,authority,request)==authority.result
    assert authority.calls==['apply','receipt']


def test_unknown_nonlanded_is_held_no_repeat_increment(lane):
    cfg,request=lane
    authority=Authority()
    authority.apply_use=lambda req: (_ for _ in ()).throw(OSError())
    for _ in range(2):
        with pytest.raises(MutationHold,match='outcome_unknown'):
            remote.apply(cfg,authority,request)
    assert authority.calls==['receipt']
    assert state(cfg)==[('unknown',)]


def test_same_attempt_payload_conflict(lane):
    cfg,request=lane
    authority=Authority()
    remote.apply(cfg,authority,request)
    altered=dict(request, post_date='2026-10-11')
    with pytest.raises(MutationHold,match='attempt_conflict'):
        remote.apply(cfg,authority,altered)
    assert authority.calls==['apply']


def test_invalid_receipt_remains_unknown(lane):
    cfg,request=lane
    authority=Authority()
    original=authority.apply_use
    def corrupt(req):
        result=original(req)
        result['asset_after']['gym_id']='neighbor'
        return result
    authority.apply_use=corrupt
    with pytest.raises(MutationHold,match='outcome_unknown'):
        remote.apply(cfg,authority,request)
    assert state(cfg)==[('unknown',)]


def test_off_before_local_or_remote_mutation(lane,monkeypatch):
    cfg,request=lane
    monkeypatch.delenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED')
    authority=Authority()
    with pytest.raises(MutationHold,match='disabled'):
        remote.apply(cfg,authority,request)
    assert authority.calls==[]
    with sqlite3.connect(cfg.sqlite_path) as c:
        assert c.execute("select name from sqlite_master where name='remote_drive_use_attempt'").fetchall()==[]


def test_db_never_called_inside_local_flock(lane):
    cfg,request=lane
    cfg.lock_directory.mkdir()
    authority=Authority()
    with (cfg.lock_directory/'gym.lock').open('a+b') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        with pytest.raises(MutationHold,match='local_lock_held'):
            remote.apply(cfg,authority,request)
    assert authority.calls==[]


@pytest.mark.parametrize('change', ['tenant','source','hash','version','used','source_active'])
def test_invalid_snapshot_bindings(lane,change):
    _,request=lane
    if change=='tenant': request['asset_before']['gym_id']='other'
    if change=='source': request['asset_before']['source_id']='other'
    if change=='hash': request['content_hash']='other'
    if change=='version': request['asset_before']['drive_use_version']=True
    if change=='used': request['asset_before']['used_count']=1
    if change=='source_active': request['source_before']['active']=False
    with pytest.raises(MutationHold,match='request_invalid'):
        remote.validate(request)


def test_never_landed_release_unavailable(lane):
    with pytest.raises(MutationHold,match='release_unavailable'):
        remote.release_never_landed(*lane)


def test_unknown_outcome_blocks_new_attempt_identity(lane):
    cfg,request=lane
    authority=Authority(lose=True)
    with pytest.raises(MutationHold,match='outcome_unknown'):
        remote.apply(cfg,authority,request)
    new_request=dict(request,use_id=str(uuid.uuid4()))
    with pytest.raises(MutationHold,match='reconciliation_required'):
        remote.apply(cfg,authority,new_request)
    assert authority.calls==['apply']
    assert state(cfg)==[('unknown',)]
