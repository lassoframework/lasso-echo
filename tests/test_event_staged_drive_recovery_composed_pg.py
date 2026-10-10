"""Offline PG17 staged EVENT -> separate real finalizer -> listener CAS recovery.

Event twin of tests/test_feed_staged_drive_recovery_composed_pg.py /
tests/test_gbp_staged_drive_recovery_composed_pg.py: the armed event photo lane
(agent.event_calendar.stage_arc -> agent.event_drive_use.stage_rows) stages one
NEW-event pending-approval arc member through the REAL
SupabaseCalendarStore.insert_rows and the real stage RPC against a disposable
PostgreSQL 17 with the frozen test-only portal 0611 fixture plus the repo's real
content_calendar_event_id_20260828.sql (the composed fixture declares only the
columns it exercises; without the real event_id column the stage SQL refuses the
row with 'staged calendar column unavailable'). The producer-side Drive bytes,
hosted media URLs and the still-certificate signing key are SYNTHETIC fixtures;
media_source gains the real portal sync_status/sync_finished_at columns so the
armed eligibility reads run against real PG rows. Nothing fabricates the stage
RPC, its receipt, the batch status readback or terminal settlement: a real lost
stage acknowledgment leaves the frozen SQLite operation/use/claim behind, the
pinned original listener replays the exact frozen request bytes through
event_drive_use_recovery, the ACTUAL separate finalizer process activates the
batch, and the pinned listener settles exactly one remote Drive use through the
real CAS. Legacy media-held backfill stays HELD (SQL milestone 2); the hold is
recorded as evidence only, never as backfill acceptance. Local composition
evidence only; all flags are armed inside temporary test/subprocess
environments and no production, provider or network authority is asserted.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql as pgsql

from agent import config, db, event_calendar as ec, event_drive_use
from agent import forward_media_attester, gym_event as ge, gym_media_index
from agent import gbp_drive_use_journal as journal, media_host, visual_writer_prepare
from agent import portal_calendar_store as pcs
from agent.integrations import drive_client
from agent.jobs import gbp_drive_use_recovery as recovery
from agent.jobs import event_drive_use_recovery as event_recovery
from tests.test_forward_media_owner_two_phase_pg import png
from tests.test_forward_media_prospective_still_v2_pg import ROOT, main as composed_pg
from tests.test_gbp_staged_drive_recovery_composed_pg import (
    SyntheticPgRest, install_remaining, portal_0611_fixture)
from tests.test_feed_staged_drive_recovery_composed_pg import (
    arm_listener, attest_row, prepare_candidate, run_finalizer, FOLDER)


class EventPgRest(SyntheticPgRest):
    """Feed-twin transport plus the gte/lte date window list_month needs.

    Same deliberately small PostgREST subset over real SQL; unsupported API
    behavior still fails rather than inventing a successful response.
    """

    def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        if not any(isinstance(value, list) for value in params.values()):
            return super().get(url, params=params, headers=headers, timeout=timeout)
        table = url.rsplit('/', 1)[1]
        assert re.fullmatch(r'[a-z0-9_]+', table)
        self.calls.append(('get', table, json.loads(json.dumps(params))))
        columns = params.pop('select', '*')
        order = params.pop('order', None)
        limit = int(params.pop('limit', '1000'))
        assert 1 <= limit <= 1000
        predicates, values = [], []
        for name, expression in params.items():
            assert re.fullmatch(r'[a-z0-9_]+', name)
            expressions = expression if isinstance(expression, list) else [expression]
            for item in expressions:
                operator, _, operand = item.partition('.')
                assert operator in ('eq', 'gte', 'lte') and operand
                predicates.append(pgsql.SQL('{}::text {} %s').format(
                    pgsql.Identifier(name),
                    pgsql.SQL({'eq': '=', 'gte': '>=', 'lte': '<='}[operator])))
                values.append(operand)
        where = (pgsql.SQL(' where ') + pgsql.SQL(' and ').join(predicates)
                 if predicates else pgsql.SQL(''))
        query = (pgsql.SQL('select to_jsonb(t) from public.{} t').format(
            pgsql.Identifier(table)) + where)
        if order:
            items = []
            for item in order.split(','):
                name, _, direction = item.partition('.')
                direction = direction or 'asc'
                assert re.fullmatch(r'[a-z0-9_]+', name) and direction in ('asc', 'desc')
                items.append(pgsql.SQL('{} {}').format(
                    pgsql.Identifier(name), pgsql.SQL(direction)))
            query += pgsql.SQL(' order by ') + pgsql.SQL(',').join(items)
        query += pgsql.SQL(' limit %s')
        with psycopg.connect(self.dsn) as conn:
            assert conn.execute('select current_user').fetchone()[0] == 'service_role'
            rows = [r[0] for r in conn.execute(query, [*values, limit])]
        if columns != '*':
            names = columns.split(',')
            assert all(re.fullmatch(r'[a-z0-9_]+', name) for name in names)
            rows = [{name: row[name] for name in names} for row in rows]
        from tests.test_gbp_staged_drive_recovery_composed_pg import PgRestResponse
        return PgRestResponse(rows, count=len(rows))


class LostEventStageAckOnce(EventPgRest):
    """Executes the real stage RPC once, then loses the HTTP response."""

    def __init__(self, dsn):
        super().__init__(dsn)
        self.armed = True

    def post(self, url, json=None, headers=None, timeout=None):
        response = super().post(url, json=json, headers=headers, timeout=timeout)
        if self.armed and '/rpc/' in url and url.rsplit('/', 1)[1] == pcs._STAGE_RPC:
            self.armed = False
            raise TimeoutError('SYNTHETIC lost stage acknowledgment')
        return response


def event_pg_store(dsn):
    transport = EventPgRest(dsn)
    return (pcs.SupabaseCalendarStore(url='http://synthetic.invalid',
                                      service_key='synthetic', http=transport),
            transport)


class PgEventMediaStore:
    """Authoritative media inventory reader over the real disposable PG rows."""

    def __init__(self, dsn):
        self.dsn = dsn

    def available(self):
        return True

    def _rows(self, table, gym_id):
        assert table in ('media_asset', 'media_source')
        with psycopg.connect(self.dsn) as conn:
            return [r[0] for r in conn.execute(
                f'select to_jsonb(t) from {table} t where gym_id=%s', (gym_id,))]

    def _row(self, table, row_id):
        assert table in ('media_asset', 'media_source')
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                f'select to_jsonb(t) from {table} t where id=%s', (row_id,)).fetchone()
        return row[0] if row else None

    def list_assets(self, gym_id):
        return self._rows('media_asset', gym_id)

    def list_sources(self, gym_id, include_inactive=False):
        return self._rows('media_source', gym_id)

    def get_asset(self, asset_id):
        return self._row('media_asset', asset_id)

    def get_source(self, source_id):
        return self._row('media_source', source_id)


def test_event_staged_drive_recovery_composed_pg(tmp_path):
    portal_0611_fixture()
    listener = tmp_path / 'listener'
    listener.mkdir()
    library = listener / 'library'
    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'PYTHONPATH', 'DYLD_LIBRARY_PATH')}
    env.update({'LC_ALL': 'en_US.UTF-8',
        'AGENT_DB_PATH': str(listener / 'original.db'),
        pcs.GBP_STAGED_JOURNAL_FLAG_ENV: 'true',
        pcs.FORWARD_RESERVATION_FLAG_ENV: 'true',
        'ECHO_LOGICAL_POST_ID_ENABLED': 'true',
        'AGENT_FORWARD_MEDIA_OBSERVATION_BRIDGE': 'true',
        recovery.FLAG: 'true',
        'AGENT_REMOTE_DRIVE_USE_CAS_ENABLED': 'true',
        'AGENT_MEDIA_CROSS_DAY_GUARD': 'false'})
    with patch.dict(os.environ, env, clear=True), \
            patch.object(config, 'LIBRARY_PATH', str(library)), \
            patch.object(recovery, 'EPHEMERAL_ROOTS', ()):
        def exercise(*, sql, denied, seed, attest, dsn):
            install_remaining(sql)
            # Real portal inventory columns the composed fixture never declared.
            sql('alter table media_source add column sync_status text')
            sql('alter table media_source add column sync_finished_at timestamptz')
            epoch = arm_listener(sql, dsn, library, 'gym')
            closure = dict(zip(seed.__code__.co_freevars,
                               (c.cell_contents for c in seed.__closure__)))
            key, private = closure['key'], closure['private']
            raw = 'gym'
            data = png('blue')  # SYNTHETIC Drive bytes; autofit 4x5 derivative.
            md5, sha = hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()
            asset_id, source_id = 'asset_' + uuid.uuid4().hex, 'source_' + uuid.uuid4().hex
            sql("insert into media_source(id,gym_id,kind,folder_id,active,sync_status,"
                "sync_finished_at) values(%s,%s,'gym_drive',%s,true,'ready',"
                "'2026-10-09T00:00:00+00:00')", (source_id, raw, FOLDER))
            moderation = dict(verdict='clean', provider='SYNTHETIC scanner',
                              content_hash=md5, asset_id=asset_id, gym_id=raw,
                              people_detected=False, observed_at='2026-10-08T00:00:00Z',
                              sha256=sha)
            sql('insert into media_asset(id,source_id,gym_id,content_hash,'
                'review_content_hash,moderation_json) values(%s,%s,%s,%s,%s,%s::jsonb)',
                (asset_id, source_id, raw, md5, md5, json.dumps(moderation)))
            service_dsn = dsn.replace('user=postgres', 'user=service_role')
            media = PgEventMediaStore(dsn)
            hosted = {}

            def synthetic_host(path, gym):
                blob = Path(path).read_bytes()
                url = ('https://scratch.example/' + gym + '/'
                       + hashlib.sha256(blob).hexdigest() + '.jpg')
                hosted[url] = blob  # SYNTHETIC hosted bytes, identified.
                return url

            class SyntheticDrive:
                def available(self):
                    return True

                def download(self, file_id, path):
                    assert file_id == asset_id
                    Path(path).write_bytes(data)

            recipe = forward_media_attester.make_still_recipe('feed_autofit_4x5')
            rendered = forward_media_attester.replay_still_recipe(data, recipe)
            assert rendered['thumbnail_bytes'] is None
            r_data = rendered['image_bytes']
            assert r_data != data
            source_url = ('https://scratch.example/' + raw + '/'
                          + hashlib.sha256(data).hexdigest() + '.jpg')
            delivered_url = ('https://scratch.example/' + raw + '/'
                             + hashlib.sha256(r_data).hexdigest() + '.jpg')
            rendition = dict(url=delivered_url, data=r_data,
                             md5=hashlib.md5(r_data).hexdigest(),
                             sha=hashlib.sha256(r_data).hexdigest(), recipe=recipe)

            event = ge.GymEvent.from_row(dict(
                id='evt_' + uuid.uuid4().hex[:12], gym_id=raw,
                name='SYNTHETIC Bring a Friend Week', type='bring_a_friend',
                starts_on='2026-10-01', ends_on='2026-10-07', tz='America/New_York',
                offer_text='Partner trains free', link='https://gym.test/baf',
                brief='SYNTHETIC brief', media_ids=(asset_id,), status='scheduled'))
            arc_row = dict(event_id=event.id, post_date='2026-10-03',
                           account='instagram', format='feed', status='pending',
                           caption='SYNTHETIC event arc caption.')
            transport = LostEventStageAckOnce(service_dsn)
            store = pcs.SupabaseCalendarStore(url='http://synthetic.invalid',
                                              service_key='synthetic', http=transport)
            logs = []
            insert_errors = []
            original_insert = store.insert_rows
            original_materialize = event_drive_use.materialize
            def trace_materialize(*args, **kwargs):
                try:
                    return original_materialize(*args, **kwargs)
                except Exception as exc:
                    insert_errors.append('materialize: ' + str(exc))
                    raise
            def trace_insert(*args, **kwargs):
                try:
                    return original_insert(*args, **kwargs)
                except Exception as exc:
                    insert_errors.append(str(exc))
                    raise
            with patch.object(gym_media_index, 'default_store', lambda: media), \
                    patch.object(drive_client, 'DriveClient', lambda: SyntheticDrive()), \
                    patch.object(media_host, 'host_media', synthetic_host), \
                    patch.object(visual_writer_prepare, '_bytes_for_url', lambda url: hosted[url]), \
                    patch.object(event_drive_use, 'materialize', trace_materialize), \
                    patch.object(store, 'insert_rows', trace_insert):
                result = ec.stage_arc(store, event, [arc_row], logger=logs.append)
            # The server COMMITTED the stage; only the acknowledgment was lost.
            assert result['ok'] is True and result['staged'] == 0
            assert result['held_media'] == 1, (result, logs)
            assert transport.armed is False, (result, logs, insert_errors)
            assert sql('select state from forward_schedule_stage_batch_20261008')[0][0] == 'staged'
            # Frozen original SQLite operation, use and claim survived intact.
            operations = journal.event_operations(raw)
            assert len(operations) == 1
            operation = operations[0]
            assert operation['batch_id'] and operation['gym_id'] == raw
            assert operation['identity']['event_id'] == event.id
            assert operation['identity']['generation'] == 0
            assert operation['identity']['old_row_id'] is None
            batch = operation['batch_id']
            entry = journal.unsettled()[0]
            assert entry['state'] == 'write_intent'
            assert entry['calendar_row']['event_id'] == event.id
            assert entry['calendar_row']['status'] == 'pending'
            pending = journal.pending_forward_stages()
            assert len(pending) == 1 and pending[0]['state'] == 'stage_intent'
            with db.connect() as conn:
                claims = conn.execute(
                    "select draft_id,account_key,status,post_id from socialapi_claims").fetchall()
            assert len(claims) == 1 and claims[0][1] == raw + '_gbp'
            assert claims[0][2] == 'in_flight' and claims[0][3] == asset_id
            rid = pending[0]['member_row_ids'][0]
            staged_row = sql('select to_jsonb(c) from content_calendar c where id=%s',
                             (rid,))[0][0]
            assert staged_row['variant_status'] == 'candidate'
            assert staged_row['event_id'] == event.id
            assert staged_row['image_url'] == delivered_url
            assert staged_row['source_media_url'] == source_url
            assert staged_row['source_media_asset_id'] == asset_id
            # Pinned listener replay resends the exact frozen bytes; the
            # idempotent SQL stage returns the same batch, never a duplicate.
            replay_store, replay_transport = event_pg_store(service_dsn)
            replayed = event_recovery.run(store=replay_store, logger=logs.append,
                                          tenant_id=raw)
            assert replayed['replayed'] == 1 and replayed['replay_held'] == 0, replayed
            assert sum(1 for call in replay_transport.calls
                       if call[0] == 'post' and call[1] == pcs._STAGE_RPC) == 1
            assert journal.get_forward_stage(batch)['state'] == 'staged_pending'
            with db.connect() as conn:
                assert [tuple(row) for row in conn.execute(
                    'select operation_key,batch_id from event_drive_operations')] == [
                        (operation['operation_key'], batch)]
            # Staged but never activated: the listener holds, no remote use.
            assert recovery.run(store=replay_store, media_store=object(),
                                logger=logs.append, tenant_id=raw)['held'] == 1
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
            assert sql('select used_count from media_asset where id=%s',
                       (asset_id,))[0][0] == 0
            # Real owner staged preparation and canonical attestation agree.
            ctx = dict(raw=raw, data=data, md5=md5, sha=sha, url=source_url,
                       asset=asset_id, source=source_id, key=key, private=private,
                       day='2026-10-03', logical=operation['logical_post_id'],
                       recipe=forward_media_attester.make_still_recipe('identity'))
            candidate = prepare_candidate(sql, dsn, ctx, rid,
                                          staged_row.get('visual_group_key'),
                                          rendition=rendition)
            attest_row(sql, candidate)
            sql('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
                (candidate['rid'], operation['logical_post_id'], candidate['rev'],
                 candidate['ids'], candidate['audit_id']), 'photo_owner')
            # The ACTUAL finalizer runs in a separate process with no listener
            # SQLite; the original journal file is byte-identical afterwards.
            before_journal = Path(os.environ['AGENT_DB_PATH']).read_bytes()
            finalized = run_finalizer(batch, raw, service_dsn)
            assert finalized['report']['status'] == 'complete', finalized
            assert finalized['report']['batches'][0]['status'] == 'finalized', finalized
            assert Path(os.environ['AGENT_DB_PATH']).read_bytes() == before_journal
            assert journal.get_forward_stage(batch)['state'] == 'staged_pending'
            active = sql('select to_jsonb(c) from content_calendar c where id=%s',
                         (rid,))[0][0]
            # The new-event arc member activates behind the client approval
            # gate: pending, never published, never approved by this flow.
            assert active['variant_status'] == 'active' and active['status'] == 'pending'
            assert active['event_id'] == event.id and active['gym_id'] == raw
            assert active['published_at'] is None and active['late_post_id'] is None
            assert active['logical_post_id'] == operation['logical_post_id']
            assert active['image_url'] == delivered_url
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            # Pinned listener one-use settlement through the real CAS.
            restarted, _ = event_pg_store(service_dsn)
            assert restarted.last_forward_stage_attempt is None
            settled_report = recovery.run(store=restarted, media_store=object(),
                                          logger=logs.append, tenant_id=raw)
            assert settled_report['recovered'] == 1 and settled_report['held'] == 0, (
                settled_report, logs)
            settled = journal.get(entry['use_id'])
            assert settled['state'] == 'claim_done'
            assert settled['landed_proof']['calendar_row'] == active
            assert journal.get_forward_stage(batch)['state'] == 'finalized'
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 1
            assert sql('select used_count from media_asset where id=%s',
                       (asset_id,))[0][0] == 1
            with db.connect() as conn:
                claim = conn.execute(
                    "select status,post_id from socialapi_claims where account_key=?",
                    (raw + '_gbp',)).fetchone()
            assert tuple(claim) == ('done', asset_id)
            assert recovery.run(store=event_pg_store(service_dsn)[0],
                                media_store=object(), logger=logs.append,
                                tenant_id=raw)['examined'] == 0
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 1
            # KNOWN HOLD EVIDENCE (not acceptance): a legacy media-held backfill
            # replacement for an already-approved old row stays held by SQL
            # milestone 2; no new operation, use, claim or staged row appears.
            old_row_id = str(uuid.uuid4())
            backfill = dict(id=str(uuid.uuid4()), event_id=event.id,
                            post_date='2026-10-04', account='instagram',
                            format='feed', status='pending',
                            caption='SYNTHETIC event backfill caption.')
            held = event_drive_use.stage_rows(
                replay_store, raw, event.id, [backfill], logs.append,
                old_rows={backfill['id']: dict(id=old_row_id, status='approved',
                                               media_not_ready_reason=None)})
            assert held['staged'] == 0 and held['held'] == 1, held
            with db.connect() as conn:
                assert conn.execute('select count(*) from event_drive_operations').fetchone()[0] == 1
            assert len(journal.unsettled()) == 0
            with db.connect() as conn:
                assert conn.execute('select count(*) from socialapi_claims').fetchone()[0] == 1
        composed_pg(runtime_check=exercise, genuine_sources=True,
                    extra_migrations=('content_calendar_event_id_20260828.sql',))
