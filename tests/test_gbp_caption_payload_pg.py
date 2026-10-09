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
from agent.portal_calendar_store import prepare_calendar_caption_payload
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
            for original in rows:
                row = prepare_calendar_caption_payload(original)
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


@pytest.mark.parametrize("caption", [
    "First approved sentence. Second approved sentence; book a class today.",
    "Training with our coaches helps you build strength and confidence while keeping your effort steady through every class and every week.",
])
def test_normalized_caption_freezes_and_settles_exact_pg_payload(composed, caption):
    c = composed; arm(c)
    row, pick, frozen = freeze(c, caption)
    assert row["caption"] != caption
    assert ";" not in row["caption"] and "\n" in row["caption"]
    assert frozen["calendar_row"] == row == frozen["payload"]
    assert prepare_calendar_caption_payload(row) == row
    persisted = c.store.insert_rows("gym", [row])[0]
    assert persisted["caption"] == row["caption"]
    assert planner._settle_armed_drive_landing("gym", row, pick, persisted, lambda _: None)
    assert journal.get(frozen["use_id"])["state"] == "claim_done"
    assert c.admin.execute("select used_count from media_asset").fetchone() == (1,)
    # Exact comparison is still enforced for any post-freeze content change.
    assert not journal._landed_row_matches(dict(persisted, caption=caption), row)


def test_protected_url_caption_remains_held_through_pg_insert(composed):
    c = composed; arm(c)
    caption = "Visit https://example.com/a;b for details."
    row, pick, frozen = freeze(c, caption)
    assert row["caption"] == caption
    assert row["media_not_ready_reason"] == "caption_url_semicolon"
    assert prepare_calendar_caption_payload(row) == row
    assert frozen["calendar_row"] == row == frozen["payload"]
    persisted = c.store.insert_rows("gym", [row])[0]
    assert persisted["caption"] == caption
    assert persisted["status"] == "pending"
    assert persisted["media_not_ready_reason"] == "caption_url_semicolon"
    # Clearing or changing the safety hold cannot masquerade as the frozen row.
    assert not planner._settle_armed_drive_landing("gym", row, pick,
        dict(persisted, media_not_ready_reason=None), lambda _: None)
    assert journal.get(frozen["use_id"])["state"] == "write_intent"
    assert c.admin.execute("select count(*) from fixer_remote_drive_use_20261008").fetchone() == (0,)
