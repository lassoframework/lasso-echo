"""Offline PG17 composition of GBP landing journal, byte claim and Drive CAS.

Reuses the repository's disposable *narrow* CAS schema fixture, not a production
schema clone. Real caller/journal/selector/remote RPC code; PG-backed store is a
transport adapter. This does not certify the held global staged writer lane.
"""
import hashlib
from types import SimpleNamespace

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from test_remote_drive_use_pg import pg  # noqa: F401 - real disposable PG fixture
from agent import config, db, gbp, gbp_planner as planner
from agent import gbp_drive_use_journal as journal, gym_media_selector as selector
from agent import gym_media_index, remote_drive_use as remote
from tests.gym_media_fakes import make_asset


@pytest.fixture
def composed(pg, tmp_path, monkeypatch):
    admin, connect, request, rpc, psycopg = pg
    library = tmp_path / 'library'; (library / 'gym').mkdir(parents=True)
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setenv('LOCAL_INVENTORY_MUTATION_EPOCH', request()['epoch_id'])
    monkeypatch.setenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED', 'true')
    # Explicit separation from global authority: this test must never imply that
    # its narrow CAS fixture supplied signed corpus/owner/reservation authority.
    for name in ('AGENT_VISUAL_GLOBAL_WRITER_PREP', 'AGENT_FORWARD_SCHEDULE_RESERVATION_ENABLED',
                 'AGENT_FORWARD_MEDIA_CLAIM_GUARD', 'AGENT_VISUAL_GLOBAL_LEDGER'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, 'LIBRARY_PATH', str(library))
    from PIL import Image
    raw = tmp_path / 'original.jpg'
    Image.new('RGB', (1600, 1200), color=(42, 87, 115)).save(raw)
    rendered = tmp_path / 'rendered.jpg'; gbp.crop_4x3(raw, rendered)
    original_bytes, rendered_bytes = raw.read_bytes(), rendered.read_bytes()
    assert original_bytes != rendered_bytes
    from agent import visual_writer_prepare
    objects = {"https://media.example.test/original.jpg": original_bytes,
               "https://media.example.test/rendered.jpg": rendered_bytes}
    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", lambda url: objects[url])
    md5 = hashlib.md5(original_bytes).hexdigest()
    admin.execute('update media_asset set content_hash=%s', (md5,))
    # Byte-bound safety columns are real PG columns, not fabricated at readback.
    safety = make_asset('asset', gym_id='gym', source_id='source', content_hash=md5)
    current = request()['asset_before']
    for field, value in safety.items():
        if field in current:
            continue
        kind = 'jsonb' if isinstance(value, (dict, list)) else ('boolean' if isinstance(value, bool)
                else 'integer' if isinstance(value, int) else 'text')
        admin.execute(sql.SQL('alter table media_asset add column {} {}').format(sql.Identifier(field), sql.SQL(kind)))
        admin.execute(sql.SQL('update media_asset set {}=%s').format(sql.Identifier(field)),
                      (Jsonb(value) if isinstance(value, (dict, list)) else value,))
    admin.execute('''create table content_calendar(
      id uuid primary key default gen_random_uuid(), gym_id text, logical_post_id uuid,
      post_date date, account text, format text, caption text, pillar text,status text,
      image_url text,source_media_url text,source_media_asset_id text,
      gbp_topic_type text,gbp_cta_type text,gbp_cta_url text,gbp_event jsonb,gbp_offer jsonb,
      gbp_location_id text,thumbnail_url text,variant_status text default 'active',
      variant_of uuid,time_slot text,slot_index integer,media_not_ready_reason text,
      scheduled_at timestamptz,published_at timestamptz,late_post_id text,
      publish_claim_token uuid,publish_reservation_day date,reject_reason text,
      hook_family text,ask_type text,caption_len_band text,has_member_face boolean,
      experiment_label text,event_id uuid,approval_kind text,approved_by text,
      approved_at timestamptz,approval_digest text,mentions jsonb default '[]',
      created_at timestamptz default now(),updated_at timestamptz default now());''')

    class PgStore:
        def available(self): return True
        def get_asset(self, aid):
            row = admin.execute('select to_jsonb(a) from media_asset a where id=%s', (aid,)).fetchone()
            return row[0] if row else None
        def get_source(self, sid):
            row = admin.execute('select to_jsonb(s) from media_source s where id=%s', (sid,)).fetchone()
            return row[0] if row else None
        def list_assets(self, gym):
            return [r[0] for r in admin.execute('select to_jsonb(a) from media_asset a where gym_id=%s', (gym,))]
        def list_sources(self, gym, include_inactive=False):
            return [r[0] for r in admin.execute('select to_jsonb(s) from media_source s where gym_id=%s and active', (gym,))]
        def insert_rows(self, gym, rows):
            for row in rows:
                assert row['gym_id'] == gym
                keys = list(row)
                values = [Jsonb(row[k]) if isinstance(row[k], (dict, list)) else row[k] for k in keys]
                admin.execute(sql.SQL('insert into content_calendar ({}) values ({})').format(
                    sql.SQL(',').join(map(sql.Identifier, keys)), sql.SQL(',').join(sql.Placeholder() for _ in keys)), values)
            return self.authoritative_rows_for_keys(gym, rows)
        def authoritative_rows_for_keys(self, gym, proposed):
            return [r[0] for row in proposed for r in admin.execute(
                'select to_jsonb(c) from content_calendar c where gym_id=%s and logical_post_id=%s',
                (gym, row['logical_post_id']))]
    store = PgStore()
    monkeypatch.setattr(gym_media_index, 'default_store', lambda: store)
    losing = {'ack': False}
    class Authority(remote.DriveUseAuthority):
        def apply_use(self, req):
            receipt = super().apply_use(req)
            if losing['ack']:
                raise OSError('synthetic lost ack AFTER PG commit')
            return receipt
    monkeypatch.setattr(remote.DriveUseAuthority, 'from_environment',
                        classmethod(lambda cls: Authority(connect('writer', autocommit=False), 'writer')))
    return SimpleNamespace(admin=admin, store=store, request=request, rpc=rpc,
        connect=connect, psycopg=psycopg, losing=losing, raw=raw, rendered=rendered,
        original_bytes=original_bytes, rendered_bytes=rendered_bytes, objects=objects, tmp=tmp_path)


def freeze(c, caption='Synthetic approved caption.'):
    asset = c.store.get_asset('asset')
    assert selector.is_usable(asset)
    assert selector.asset_source_ok(asset, 'gym', c.store)
    claim = selector.claim_drive_content('gym', asset, c.store)
    assert claim
    pick = dict(kind='drive', base='gym', asset=asset, store=c.store,
                day_key='2026-10-10', claim_id=claim, claim_account='gym_gbp')
    row = planner._row('gym', 'gym_ig', pick['day_key'], caption,
        'https://media.example.test/rendered.jpg', topic_type='STANDARD',pillar='local',
        source_media_url='https://media.example.test/original.jpg',source_media_asset_id='asset')
    entry = planner._prepare_drive_use_journal('gym', row, pick)
    assert entry is not None
    pick['journal_entry'] = journal.record_write_intent(entry['use_id'])
    return row, pick, entry


def arm(c):
    c.admin.execute("update fixer_remote_drive_use_control_20261008 set enabled=true,writers_verified_ref='SYNTHETIC composition only'")


def test_full_pg_landing_receipt_retry_restart_and_byte_occupancy(composed):
    c = composed; arm(c)
    row, pick, frozen = freeze(c)
    persisted = c.store.insert_rows('gym', [row])[0]
    assert persisted['mentions'] == [] and persisted['variant_status'] == 'active'
    assert persisted['image_url'] != persisted['source_media_url']
    assert planner._settle_armed_drive_landing('gym', row, pick, persisted, lambda _: None)
    settled = journal.get(frozen['use_id'])
    assert settled['state'] == 'claim_done'
    assert settled['landed_proof']['calendar_row'] == persisted
    receipt = c.rpc(c.connect('writer'), 'fixer_remote_drive_use_receipt_20261008', journal._remote_request(settled,settled['use_id']))
    assert receipt == settled['receipt']
    assert receipt['asset_after']['used_count'] == 1
    assert c.admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone() == (1,)
    assert planner._settle_armed_drive_landing('gym', row, pick, persisted, lambda _: None)
    assert planner._recover_armed_drive_uses('gym', 'gym_ig', c.store, lambda _: None) is None
    assert selector.claim_drive_content('gym', pick['asset'], c.store) is None
    assert selector.pickable('gym', store=c.store, post_date='2026-10-11') == []
    request = journal._remote_request(settled, settled['use_id'])
    for bad in (dict(request, gym_id='neighbor'),dict(request,post_date='2026-10-11'),
                dict(request,content_hash=hashlib.md5(c.rendered_bytes).hexdigest())):
        with pytest.raises(c.psycopg.Error):
            c.rpc(c.connect('writer'),'fixer_remote_drive_use_apply_20261008',bad)
    evidence = planner._render_evidence_dict(persisted['source_media_url'],persisted['image_url'],
            c.raw,c.rendered,tenant='gym',source_asset_id='asset')
    assert evidence['source_fingerprint'] == 'md5:' + hashlib.md5(c.original_bytes).hexdigest()
    assert evidence['delivered_fingerprint'] == 'md5:' + hashlib.md5(c.rendered_bytes).hexdigest()
    assert evidence['source_byte_length'] == len(c.original_bytes)
    assert evidence['delivered_byte_length'] == len(c.rendered_bytes)
    assert evidence["materialization_observation"]["provenance_status"] == "unverified"
    # Corrupt hosted derivative: the actual producer must hold, not attest.
    c.objects[persisted["image_url"]] = b"different derivative"
    assert planner._render_evidence_dict(persisted["source_media_url"],persisted["image_url"],
        c.raw,c.rendered,tenant="gym",source_asset_id="asset") is None
    # This producer observation is NOT owner-verified byte authority.


def test_lost_pg_ack_restart_reuses_one_receipt(composed):
    c = composed; arm(c)
    row,pick,frozen = freeze(c); persisted = c.store.insert_rows('gym',[row])[0]
    c.losing['ack'] = True
    assert not planner._settle_armed_drive_landing('gym',row,pick,persisted,lambda _: None)
    assert journal.get(frozen['use_id'])['state'] == 'consumption_pending'
    assert c.admin.execute('select used_count from media_asset').fetchone() == (1,)
    # Reopen fresh PG connections through the real environment factory, with
    # durable SQLite journal/attempt files surviving the caller restart.
    c.losing['ack'] = False
    recovered = planner._recover_armed_drive_uses('gym','gym_ig',c.store,lambda _: None)
    assert recovered['ok'] and recovered['recovered'] == 1
    assert journal.get(frozen['use_id'])['state'] == 'claim_done'
    assert c.admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone() == (1,)


def test_zero_readback_and_unknown_global_fields_hold(composed):
    c = composed; arm(c)
    row,pick,frozen = freeze(c)
    journal.mark_unknown(frozen['use_id']); journal.record_zero_readback(frozen['use_id'])
    assert not planner._settle_armed_drive_landing('gym',row,pick,None,lambda _: None)
    assert journal.get(frozen['use_id'])['state'] == 'unknown_result'
    persisted = c.store.insert_rows('gym',[row])[0]
    # Global prepared/staged writer metadata is not an accepted omitted default.
    # This records the composition boundary; these synthetic additions do not
    # pretend to execute the global staging/reservation migration.
    for extra in ({'visual_group_key':'vg_synthetic'}, {'render_manifest_digest':'sha256:'+'a'*64},
                  {'variant_status':'candidate'}, {'caption':'Changed after proposal.'}):
        assert not planner._settle_armed_drive_landing('gym',row,pick,dict(persisted,**extra),lambda _: None)
        assert journal.get(frozen['use_id'])['state'] == 'unknown_result'
    assert c.admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone() == (0,)
    assert planner._settle_armed_drive_landing('gym',row,pick,persisted,lambda _: None)


def test_default_off_local_and_pg_gate(composed, monkeypatch):
    c = composed
    monkeypatch.delenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED')
    assert not remote.enabled() and not planner._remote_drive_cas_enabled()
    request = c.request()
    with pytest.raises(c.psycopg.Error,match='disabled'):
        c.rpc(c.connect('writer'),'fixer_remote_drive_use_apply_20261008',request)
    assert c.admin.execute('select used_count from media_asset').fetchone() == (0,)
    assert c.admin.execute('select count(*) from fixer_remote_drive_use_20261008').fetchone() == (0,)


def test_calendar_caption_normalization_is_exact_landing_proof(composed):
    from agent.portal_calendar_store import prepare_calendar_caption_payload
    c = composed; arm(c)
    raw_caption = "First approved sentence. Second approved sentence."
    row,pick,frozen = freeze(c, raw_caption)
    assert row["caption"] != raw_caption
    assert prepare_calendar_caption_payload(row) == row
    assert frozen["calendar_row"] == row
    persisted = c.store.insert_rows("gym",[row])[0]
    assert planner._settle_armed_drive_landing("gym",row,pick,persisted,lambda _: None)
    assert journal.get(frozen["use_id"])["state"] == "claim_done"
    assert c.admin.execute("select count(*) from fixer_remote_drive_use_20261008").fetchone() == (1,)
