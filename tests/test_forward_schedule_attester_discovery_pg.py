"""Standalone disposable PG17 acceptance for DRAFT_fixer_forward_schedule_attester_discovery_20261008.

Run `python3 tests/test_forward_schedule_attester_discovery_pg.py` (also under
pytest; module-level main() runs in test_attester_discovery_pg). Uses a real
disposable PostgreSQL 17 cluster on a Unix socket; no DSN, network, production
connection, installs or persisted cluster. Requires the stage draft SQL from
the sibling reservation checkout (read-only; override with FORWARD_STAGE_SQL).
Skips cleanly when PG17 binaries or the stage draft are unavailable.
"""
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STAGE_SQL = Path(os.environ.get(
    'FORWARD_STAGE_SQL',
    ROOT.parent / 'echo-schedule-reservation-20261008'
    / 'migrations/DRAFT_fixer_forward_schedule_stage_20261008.sql'))


def _pg(name):
    bundled = Path('/opt/homebrew/opt/postgresql@17/bin') / name
    return str(bundled) if bundled.is_file() else shutil.which(name)


def _skipped():
    if any(not _pg(n) for n in ('initdb', 'pg_ctl', 'postgres')):
        return 'existing initdb/pg_ctl unavailable; disposable PostgreSQL not provisioned'
    if not STAGE_SQL.is_file():
        return f'stage draft unavailable at {STAGE_SQL}; set FORWARD_STAGE_SQL'
    return None


def main():
    skip = _skipped()
    if skip:
        print('SKIP: ' + skip)
        return
    import psycopg
    assert ' 17.' in subprocess.check_output([_pg('postgres'), '--version'], text=True)
    with tempfile.TemporaryDirectory(prefix='fwd_attester_pg_', dir='/tmp') as tmp:
        work = Path(tmp)
        sock = work / 'sock'
        sock.mkdir()
        data = work / 'data'
        port = random.randint(41000, 59000)
        subprocess.run([_pg('initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run([_pg('pg_ctl'), '-D', str(data), '-l', str(work / 'pg.log'),
                        '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        try:
            def dsn(role='postgres'):
                return f'host={sock} port={port} dbname=postgres user={role}'
            admin = psycopg.connect(dsn(), autocommit=True)

            def sql(query, args=None, role=None):
                conn = admin
                if role:
                    conn = psycopg.connect(dsn(), autocommit=True)
                    conn.execute('set role ' + role)
                try:
                    with conn.cursor() as cur:
                        cur.execute(query, args)
                        return cur.fetchall() if cur.description else None
                finally:
                    if role:
                        conn.close()

            def denied(query, args=None, role='anon', fragment='permission denied'):
                try:
                    sql(query, args, role=role)
                    raise AssertionError(f'unsafe grant unexpectedly accepted for {role}')
                except psycopg.Error as exc:
                    assert fragment in str(exc), str(exc)

            sql('create role anon; create role authenticated; create role service_role;'
                "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                "account text,format text,gbp_location_id text,"
                "status text not null check(status in ('draft','pending','approved','published','denied','killed','failed','publishing','deleted','coach_review')),"
                "variant_status text not null check(variant_status in ('active','candidate','archived')),"
                "published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,"
                "late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);"
                'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
                'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text);')
            for name in ('logical_post_id_20261004.sql',
                         'DRAFT_fixer_forward_media_claim_20261006.sql',
                         'DRAFT_fixer_forward_visual_index_20261008.sql',
                         'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                         'DRAFT_fixer_forward_media_source_history_20261007.sql',
                         'DRAFT_fixer_forward_media_owner_transport_20261007.sql',
                         'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
                         'DRAFT_fixer_owner_photo_clearance_20261007.sql',
                         'DRAFT_fixer_forward_schedule_reservation_20261008.sql'):
                sql((ROOT / 'migrations' / name).read_text())
            sql(STAGE_SQL.read_text())
            sql((ROOT / 'migrations'
                 / 'DRAFT_fixer_forward_schedule_attester_discovery_20261008.sql').read_text())
            sql('grant select,insert,update,delete on public.content_calendar to service_role;')
            sql('create role discovery_attester login;'
                ' grant fixer_forward_media_attester_20261006 to discovery_attester;')
            sql('create role discovery_owner login; grant fixer_forward_media_owner_20261006 to discovery_owner;')
            sql('update forward_media_visual_gate_20261008 set enabled=true;')
            sql('update forward_schedule_reservation_gate_20261008 set enabled=true;')

            def member(tenant, gym=None):
                url = 'https://scratch.example/' + uuid.uuid4().hex
                return {'row': {'id': str(uuid.uuid4()), 'gym_id': gym or tenant,
                                'post_date': '2026-10-10', 'status': 'pending',
                                'account': 'instagram', 'format': 'feed',
                                'logical_post_id': str(uuid.uuid4()),
                                'source_media_url': url, 'image_url': url,
                                'source_media_asset_id': 'staged_' + uuid.uuid4().hex,
                                'visual_group_key': 'vg_' + uuid.uuid4().hex},
                        'observation': None}

            def stage(tenant, members, batch_id=None):
                batch_id = batch_id or str(uuid.uuid4())
                request = json.dumps({'members': members, 'old_rows': []},
                                     sort_keys=True, separators=(',', ':'))
                digest = hashlib.sha256(request.encode()).hexdigest()
                receipt = sql('select public.stage_forward_schedule_batch_20261008(%s,%s,%s,%s)',
                              (tenant, batch_id, request, digest), role='service_role')[0][0]
                return batch_id, receipt

            def eligible(rid):
                return sql('select public.forward_schedule_preparation_eligible_20261008(%s)',
                           (rid,))[0][0]

            def revision_of(rid):
                return sql("select public.fixer_forward_media_attestation_request_20261006(%s)->>'revision'",
                           (rid,))[0][0]

            def pending(tenant, limit=25, after=None, role='discovery_attester'):
                return [r[0] for r in sql(
                    'select public.fixer_forward_schedule_staged_attester_pending_20261008(%s,%s,%s)',
                    (tenant, limit, after), role=role)]

            def recovery(tenant, limit=25, after=None, role='discovery_attester'):
                return [r[0] for r in sql(
                    'select public.fixer_forward_schedule_staged_visual_recovery_pending_20261008(%s,%s,%s)',
                    (tenant, limit, after), role=role)]

            def prepare(rid, tenant):
                """Trusted owner/binder preparation output: the exact manifest
                fixture plus the render_manifest_digest preparation field."""
                digest = 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex
                row = sql('select source_media_asset_id,image_url,source_media_url from content_calendar where id=%s',
                          (rid,))[0]
                sql('insert into fixer_forward_media_original_registry_20261006'
                    '(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)'
                    " values(%s,%s,%s,%s,10,'SYNTHETIC owner-verified original') on conflict do nothing",
                    (tenant, row[0], row[2], 'md5:' + uuid.uuid4().hex))
                sql('insert into fixer_forward_media_render_manifest_20261006'
                    '(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,'
                    "operation,render_evidence_ref) values(%s,%s,%s,%s,%s,10,'same_object','SYNTHETIC render')",
                    (digest, tenant, row[0], row[1], 'md5:' + uuid.uuid4().hex))
                sql('update content_calendar set render_manifest_digest=%s where id=%s', (digest, rid))
                return digest

            def plant_lineage(rid, tenant):
                """Crash point: lineage committed, visual roles not yet appended."""
                rev = revision_of(rid)
                row = sql('select source_media_url,image_url,thumbnail_url,source_media_asset_id,'
                          'visual_group_key,render_manifest_digest from content_calendar where id=%s',
                          (rid,))[0]
                receipts = []
                for url in (row[0], row[1], row[2] or row[1]):
                    existing = sql('select receipt_id from fixer_forward_media_object_read_20261006'
                                   ' where tenant_id=%s and exact_url=%s', (tenant, url))
                    if existing:
                        receipts.append(str(existing[0][0]))
                        continue
                    rid_receipt = str(uuid.uuid4())
                    sql('insert into fixer_forward_media_object_read_20261006'
                        '(receipt_id,tenant_id,exact_url,fingerprint,byte_length,evidence_ref,verified_by)'
                        " values(%s,%s,%s,%s,10,'SYNTHETIC read','trusted_attester')",
                        (rid_receipt, tenant, url, 'md5:' + uuid.uuid4().hex))
                    receipts.append(rid_receipt)
                evidence = str(uuid.uuid4())
                sql('insert into fixer_forward_media_lineage_20261006'
                    '(evidence_id,calendar_row_id,row_revision,operation,tenant_id,group_key,'
                    'source_read_receipt,image_read_receipt,thumbnail_read_receipt,source_asset_id,'
                    "manifest_digest,verified_by) values(%s,%s,%s,'same_object',%s,%s,%s,%s,%s,%s,%s,"
                    "'trusted_attester')",
                    (evidence, rid, rev, tenant, row[4], receipts[0], receipts[1], receipts[2],
                     row[3], row[5]))
                return evidence, rev, receipts

            def attest_role(rid, tenant, evidence, rev, receipt, role, url):
                row_rev = sql("select ('x'||substr(%s,1,15))::bit(60)::bigint", (rev,))[0][0]
                sql('insert into forward_media_visual_attestation'
                    '(tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_version,'
                    'phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id)'
                    ' values(%s,%s,%s,%s,%s,10,1,1,%s,%s,%s)',
                    (tenant, url, role, uuid.uuid4().hex + uuid.uuid4().hex,
                     uuid.uuid4().hex, row_rev, evidence, receipt))

            tenant = 'gym_' + uuid.uuid4().hex
            members = [member(tenant), member(tenant)]
            batch, receipt = stage(tenant, members)
            row_a, row_b = receipt['member_row_ids']

            # Rows awaiting owner/binder preparation (no render_manifest_digest)
            # fail closed OUT of staged attester discovery instead of raising.
            assert pending(tenant) == [] and recovery(tenant) == []

            # DISCOVERY: exact prepared staged member appears with batch and
            # canonical tenant; the predicate agrees in staged mode.
            prepare(row_a, tenant)
            found = pending(tenant)
            assert [c['calendar_row_id'] for c in found] == [row_a], found
            assert found[0]['batch_id'] == batch and found[0]['tenant_id'] == tenant
            assert len(found[0]['revision']) == 32
            assert eligible(row_a)['mode'] == 'staged'
            assert recovery(tenant) == []

            # CRASH after lineage commit: the row leaves attester pending and
            # recovery exposes the SAME persisted lineage with all roles missing.
            evidence, rev, receipts = plant_lineage(row_a, tenant)
            assert pending(tenant) == []
            rec = recovery(tenant)
            assert [c['calendar_row_id'] for c in rec] == [row_a]
            assert rec[0]['lineage_receipt_id'] == evidence
            assert rec[0]['revision'] == rev and rec[0]['batch_id'] == batch
            assert sorted(rec[0]['missing_roles']) == ['delivered', 'original', 'thumbnail']

            # NO DUPLICATE LINEAGE: a second lineage for the same row/revision
            # is refused by the unique constraint; retry reuses the same one.
            try:
                plant_lineage(row_a, tenant)
                raise AssertionError('duplicate lineage unexpectedly accepted')
            except psycopg.Error as exc:
                assert 'duplicate key' in str(exc)

            # RETRY appends missing roles only: original+delivered present,
            # recovery still returns the row naming exactly thumbnail.
            row_urls = sql('select source_media_url,image_url from content_calendar where id=%s',
                           (row_a,))[0]
            attest_role(row_a, tenant, evidence, rev, receipts[0], 'original', row_urls[0])
            attest_role(row_a, tenant, evidence, rev, receipts[1], 'delivered', row_urls[1])
            rec = recovery(tenant)
            assert [c['calendar_row_id'] for c in rec] == [row_a]
            assert rec[0]['missing_roles'] == ['thumbnail']
            assert rec[0]['lineage_receipt_id'] == evidence

            # THREE-ROLE COMPLETION: once all roles persist under the same
            # lineage, the row leaves both lanes.
            attest_role(row_a, tenant, evidence, rev, receipts[2], 'thumbnail', row_urls[1])
            assert recovery(tenant) == [] and pending(tenant) == []

            # STALE REVISION: a new trusted preparation output changes the
            # persisted revision; the old lineage no longer matches, so the row
            # is an ATTESTATION candidate again and never a recovery candidate.
            prepare(row_a, tenant)
            new_rev = revision_of(row_a)
            assert new_rev != rev
            assert [c['calendar_row_id'] for c in pending(tenant)] == [row_a]
            assert recovery(tenant) == []

            # WRONG TENANT: another tenant's scope never sees the row.
            other = 'other_' + uuid.uuid4().hex
            assert pending(other) == [] and recovery(other) == []
            # Canonical tenant via alias: staged under an alias gym, discovered
            # only under the canonical tenant.
            alias_gym = 'alias_' + uuid.uuid4().hex
            canonical = 'gym_' + uuid.uuid4().hex
            sql('insert into fixer_forward_media_tenant_alias_20261006(alias_key,tenant_id) values(%s,%s)',
                (alias_gym, canonical))
            alias_batch, alias_receipt = stage(canonical, [member(canonical, gym=alias_gym)])
            alias_id = alias_receipt['member_row_ids'][0]
            prepare(alias_id, canonical)
            assert pending(alias_gym) == [] and recovery(alias_gym) == []
            assert [c['calendar_row_id'] for c in pending(canonical)] == [alias_id]

            # FORGED MARKER: candidate + marker but no registered membership is
            # never discovered and the predicate refuses it as unregistered.
            forged_id = str(uuid.uuid4())
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "media_not_ready_reason,visual_group_key,source_media_asset_id,"
                "source_media_url,image_url,logical_post_id,render_manifest_digest)"
                " values(%s,%s,'2026-10-12','pending','candidate','forward_reservation_staged',"
                "'group','staged_forged','https://scratch.example/f','https://scratch.example/f',%s,%s)",
                (forged_id, tenant, str(uuid.uuid4()),
                 sql('select render_manifest_digest from content_calendar where id=%s', (row_a,))[0][0]))
            assert eligible(forged_id)['reason'] == 'unregistered staged row'
            assert all(c['calendar_row_id'] != forged_id for c in pending(tenant))
            assert all(c['calendar_row_id'] != forged_id for c in recovery(tenant))

            # BINDING DRIFT and claim exclusion remove candidates.
            sql("update content_calendar set image_url='https://scratch.example/drifted' where id=%s",
                (alias_id,))
            assert pending(canonical) == [] and recovery(canonical) == []
            assert eligible(alias_id)['reason'] == 'content/media binding changed'
            sql('update content_calendar set image_url=%s where id=%s',
                (sql('select source_media_url from content_calendar where id=%s', (alias_id,))[0][0],
                 alias_id))
            sql('update content_calendar set publish_claim_token=%s where id=%s',
                (str(uuid.uuid4()), alias_id))
            assert pending(canonical) == []
            sql('update content_calendar set publish_claim_token=null where id=%s', (alias_id,))
            assert [c['calendar_row_id'] for c in pending(canonical)] == [alias_id]

            # NO DUPLICATE DISCOVERY and bounds: each row appears once across
            # tenants, keyset cursor paginates, and the limit is enforced.
            prepare(row_b, tenant)
            ids = [c['calendar_row_id'] for c in pending(tenant)]
            assert sorted(ids) == sorted([row_a, row_b]) and len(set(ids)) == 2
            first = pending(tenant, limit=1)
            assert len(first) == 1
            rest = pending(tenant, after=first[0]['calendar_row_id'])
            assert len(rest) == 1 and rest[0]['calendar_row_id'] != first[0]['calendar_row_id']
            for bad_limit in (0, 101):
                try:
                    pending(tenant, limit=bad_limit)
                    raise AssertionError('unbounded attestation request accepted')
                except psycopg.Error as exc:
                    assert 'bounded tenant-scoped attestation request required' in str(exc)
            try:
                pending('', limit=1)
                raise AssertionError('empty tenant scope accepted')
            except psycopg.Error as exc:
                assert 'bounded tenant-scoped attestation request required' in str(exc)

            # FAIRNESS: ORDER BY/LIMIT applies AFTER the lane predicates, never
            # to raw registered membership. Two members, first has a complete
            # same-revision lineage (attested) and second owes attestation:
            # pending(limit=1) must return the second member, not an empty page.
            fair_a = 'gym_' + uuid.uuid4().hex
            _, fair_a_receipt = stage(fair_a, [member(fair_a), member(fair_a)])
            fa1, fa2 = sorted(fair_a_receipt['member_row_ids'])
            prepare(fa1, fair_a)
            prepare(fa2, fair_a)
            fa_ev, fa_rev, fa_receipts = plant_lineage(fa1, fair_a)
            fa_urls = sql('select source_media_url,image_url from content_calendar where id=%s',
                          (fa1,))[0]
            attest_role(fa1, fair_a, fa_ev, fa_rev, fa_receipts[0], 'original', fa_urls[0])
            attest_role(fa1, fair_a, fa_ev, fa_rev, fa_receipts[1], 'delivered', fa_urls[1])
            attest_role(fa1, fair_a, fa_ev, fa_rev, fa_receipts[2], 'thumbnail', fa_urls[1])
            assert [c['calendar_row_id'] for c in pending(fair_a, limit=1)] == [fa2]
            assert [c['calendar_row_id'] for c in pending(fair_a, limit=2)] == [fa2]
            assert recovery(fair_a) == []

            # FAIRNESS (recovery lane): two members, first fully attested and
            # second has a same-revision lineage missing roles:
            # recovery(limit=1) must return the second member, not empty.
            fair_b = 'gym_' + uuid.uuid4().hex
            _, fair_b_receipt = stage(fair_b, [member(fair_b), member(fair_b)])
            fb1, fb2 = sorted(fair_b_receipt['member_row_ids'])
            prepare(fb1, fair_b)
            prepare(fb2, fair_b)
            fb_ev1, fb_rev1, fb_receipts1 = plant_lineage(fb1, fair_b)
            fb_urls1 = sql('select source_media_url,image_url from content_calendar where id=%s',
                           (fb1,))[0]
            attest_role(fb1, fair_b, fb_ev1, fb_rev1, fb_receipts1[0], 'original', fb_urls1[0])
            attest_role(fb1, fair_b, fb_ev1, fb_rev1, fb_receipts1[1], 'delivered', fb_urls1[1])
            attest_role(fb1, fair_b, fb_ev1, fb_rev1, fb_receipts1[2], 'thumbnail', fb_urls1[1])
            fb_ev2, fb_rev2, fb_receipts2 = plant_lineage(fb2, fair_b)
            assert [c['calendar_row_id'] for c in recovery(fair_b, limit=1)] == [fb2]
            assert [c['calendar_row_id'] for c in recovery(fair_b, limit=2)] == [fb2]
            assert recovery(fair_b, limit=1)[0]['lineage_receipt_id'] == fb_ev2
            assert pending(fair_b) == []

            # TERMINAL BATCH: finalized members leave both lanes.
            sql("update forward_schedule_stage_batch_20261008 set state='finalized',"
                " finalize_request='{}'::jsonb, finalize_receipt='{}'::jsonb, finalized_at=now()"
                ' where batch_id=%s', (alias_batch,))
            assert pending(canonical) == [] and recovery(canonical) == []
            assert eligible(alias_id)['reason'] == 'stage batch terminal or unavailable'

            # ACLs: staged attester discovery is limited to the isolated
            # attester role; service-role isolation is preserved.
            for role in ('anon', 'authenticated', 'service_role', 'discovery_owner'):
                denied('select public.fixer_forward_schedule_staged_attester_pending_20261008(%s,%s,%s)',
                       (tenant, 25, None), role=role)
                denied('select public.fixer_forward_schedule_staged_visual_recovery_pending_20261008(%s,%s,%s)',
                       (tenant, 25, None), role=role)
            assert pending(tenant, role='discovery_attester') is not None

            print('PASS: PG17 staged attester discovery; staged pending binds exact '
                  'membership/batch/canonical tenant (incl. alias) and excludes unprepared rows; '
                  'crash/retry reuses the SAME lineage and names only missing roles; three-role '
                  'completion empties both lanes; duplicate lineage refused by unique constraint; '
                  'stale revision returns the row to attester pending (never recovery); wrong '
                  'tenant and forged marker rejected; binding drift, claim and terminal batch '
                  'exclusions with exact predicate reasons; keyset cursor, dedupe and bounds; '
                  'ORDER BY/LIMIT applies after lane predicates, so a fully attested '
                  'first member never starves a pending or recovery second member; '
                  'role ACLs fail closed with service-role isolation preserved')
        finally:
            subprocess.run([_pg('pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           capture_output=True, timeout=60)


def test_attester_discovery_pg():
    skip = _skipped()
    if skip:
        import pytest
        pytest.skip(skip)
    main()


if __name__ == '__main__':
    main()
