"""Disposable PG17 integration. Run standalone; only local synthetic fixtures.

Uses the real authority and bridge drafts, no production DSN or dependency
installation. Cluster listens only on its private Unix socket and is removed.
"""
import concurrent.futures
import json
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import time
import uuid

from agent.forward_media_observation_bridge import prepare
from agent.gym_media_index import materialization_observation

ROOT = Path(__file__).resolve().parents[1]


def literal(value):
    if value is None:
        return 'null'
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return "'" + value.replace("'", "''") + "'"


def main():
    for tool in ('initdb', 'pg_ctl', 'psql'):
        if not shutil.which(tool):
            raise SystemExit(f'BLOCKED: {tool} unavailable; no install attempted')
    version = subprocess.check_output(['postgres', '--version'], text=True)
    assert ' 17.' in version, f'PG17 required, got {version.strip()}'
    with tempfile.TemporaryDirectory(prefix='forward_observation_pg_') as temp:
        root = Path(temp)
        sock, data = root / 'sock', root / 'data'
        sock.mkdir()
        port = random.randint(41000, 59000)
        subprocess.run(['initdb', '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        started = False
        try:
            subprocess.run(['pg_ctl', '-D', str(data), '-l', str(root / 'pg.log'),
                '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                check=True, capture_output=True, timeout=60)
            started = True
            base = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(sock),
                    '-p', str(port), '-U', 'postgres', '-d', 'postgres']

            def sql(query, ok=True):
                result = subprocess.run(base, input=query, text=True,
                                        capture_output=True, timeout=30)
                if ok:
                    assert result.returncode == 0, result.stderr
                    return result.stdout.strip()
                assert result.returncode != 0, 'unexpected SQL success'
                return result.stderr

            sql('create role anon; create role authenticated; create role service_role;'
                'create table public.content_calendar(id uuid primary key,gym_id text,'
                'post_date date,account text,format text,gbp_location_id text,status text,'
                "variant_status text default 'active',published_at timestamptz,"
                'publish_claim_token uuid,publish_reservation_day date,late_post_id text,'
                'image_url text,thumbnail_url text,media_not_ready_reason text,caption text);')
            sql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_media_observation_bridge_20261007.sql').read_text())
            sql('grant select,insert,update,delete on public.content_calendar to service_role;')
            assert sql('set role service_role; select '
                       'fixer_forward_media_observation_bridge_ready_20261007();') == 't'

            def make_row(**overrides):
                row_id = str(uuid.uuid4())
                values = {'id': row_id, 'gym_id': 'synthetic_gym',
                    'post_date': '2026-10-10', 'status': 'pending',
                    'source_media_asset_id': 'synthetic_asset',
                    'source_media_url': 'https://media.example.test/source.jpg',
                    'image_url': 'https://media.example.test/render.jpg'}
                values.update(overrides)
                sql('insert into content_calendar(' + ','.join(values) + ') values('
                    + ','.join(literal(v) for v in values.values()) + ');')
                return json.loads(sql(f'select to_jsonb(r) from content_calendar r where id={literal(row_id)};'))

            def observed(row, **changes):
                item = materialization_observation(b'source', b'render', row['image_url'],
                    tenant='synthetic_gym', source_asset_id=row['source_media_asset_id'],
                    source_url=row['source_media_url'], recipe={'runtime_verified': False},
                    bytes_fn=lambda url: b'source' if url == row['source_media_url'] else b'render')
                if changes:
                    import hashlib
                    item.update(changes)
                    item['observation_digest'] = hashlib.sha256(json.dumps(
                        {k: v for k, v in item.items() if k != 'observation_digest'},
                        sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
                return item

            def command(row, observation=None, raw=None, digest_input=None):
                observation = observed(row) if observation is None else observation
                # Use the production Python encoder, not a fixture-owned digest.
                candidate = prepare(dict(row, status='pending', publish_claim_token=None,
                    published_at=None, late_post_id=None, render_manifest_digest=None), [observation])
                return ('set role service_role; select '
                    'fixer_record_forward_media_observation_20261007('
                    + ','.join(literal(v) for v in (row['id'], row,
                        candidate['observation_json'] if raw is None else raw,
                        candidate['digest_input'] if digest_input is None else digest_input)) + ');')

            first = make_row()
            request = command(first)
            receipt = json.loads(sql(request))
            assert receipt['calendar_row_id'] == first['id']
            assert receipt['provenance_status'] == 'unverified'
            assert receipt == json.loads(sql(request))
            assert sql('select count(*) from fixer_forward_media_observation_20261007;') == '1'
            snapshot = json.loads(sql('select calendar_snapshot from '
                'fixer_forward_media_observation_20261007;'))
            assert snapshot == first
            # No authority is created even though real bytes/digests were observed.
            for name in ('original_registry', 'history_clearance', 'render_manifest', 'lineage'):
                assert sql(f'select count(*) from fixer_forward_media_{name}_20261006;') == '0'
                denied = sql(f'set role service_role; insert into '
                    f'fixer_forward_media_{name}_20261006 default values;', ok=False)
                assert 'permission denied' in denied.lower()
            for role in ('service_role', 'anon', 'authenticated',
                         'fixer_forward_media_attester_20261006', 'fixer_forward_media_owner_20261006'):
                assert 'permission denied' in sql(f'set role {role}; insert into '
                    'fixer_forward_media_observation_20261007 default values;', ok=False).lower()
            for role in ('anon', 'authenticated', 'fixer_forward_media_attester_20261006',
                         'fixer_forward_media_owner_20261006'):
                assert 'permission denied' in sql(request.replace('set role service_role',
                    'set role ' + role), ok=False).lower()
            for action in ('update fixer_forward_media_observation_20261007 set tenant_id=tenant_id',
                           'delete from fixer_forward_media_observation_20261007',
                           'truncate fixer_forward_media_observation_20261007'):
                assert 'immutable' in sql(action + ';', ok=False).lower()
            # Same parsed JSON but different wire bytes cannot replay an existing key.
            raw = prepare(first, [observed(first)])['observation_json']
            assert 'immutable observation conflict' in sql(command(first, raw=' ' + raw), ok=False)
            assert 'immutable observation conflict' in sql(command(first,
                observed(first, hold_reasons=['different untrusted assertion'])), ok=False)
            assert 'contract invalid' in sql(command(first, digest_input='{}'), ok=False)
            # Exact row digest includes metadata beyond the URLs/date (e.g. caption).
            changed = make_row()
            changed_request = command(changed)
            sql(f"update content_calendar set caption='changed' where id={literal(changed['id'])};")
            assert 'revision required' in sql(changed_request, ok=False)
            for status in ('approved', 'publishing', 'published', 'denied'):
                protected = make_row(status=status)
                assert 'revision required' in sql(command(protected), ok=False)
            for field, value in (('publish_claim_token', str(uuid.uuid4())),
                ('late_post_id', 'synthetic-provider'), ('published_at', '2026-10-10T00:00:00Z'),
                ('render_manifest_digest', 'sha256:' + 'a' * 64)):
                protected = make_row(**{field: value})
                assert 'revision required' in sql(command(protected), ok=False)
            foreign = make_row()
            assert 'canonical observation media binding invalid' in sql(command(foreign,
                observed(foreign, tenant='other_gym')), ok=False)
            # Only a pre-existing owner-controlled alias can canonicalize gym identity.
            aliased = make_row(gym_id='synthetic_gym_ig')
            assert 'canonical observation media binding invalid' in sql(command(aliased), ok=False)
            sql("insert into fixer_forward_media_tenant_alias_20261006 values"
                "('synthetic_gym_ig','synthetic_gym');")
            assert json.loads(sql(command(aliased)))['provenance_status'] == 'unverified'
            # Concurrent exact replay is idempotent, while stale responses block
            # after a current-row lock held by a real concurrent writer is released.
            parallel = make_row()
            parallel_request = command(parallel)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(sql, [parallel_request, parallel_request]))
            assert json.loads(results[0]) == json.loads(results[1])
            stale = make_row()
            stale_request = command(stale)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(sql, 'begin; update content_calendar set caption=\'raced\' '
                    f'where id={literal(stale["id"])}; select pg_sleep(0.4); commit;')
                time.sleep(0.1)
                assert 'revision required' in sql(stale_request, ok=False)
                future.result()
            # A process dying after INSERT cannot acquire publish permission.
            crash = make_row()
            token = str(uuid.uuid4())
            sql("insert into fixer_forward_media_claim_gate_20261006 values('synthetic_gym',true);")
            sql(f"update content_calendar set status='publishing',publish_claim_token={literal(token)},"
                f"publish_reservation_day=post_date where id={literal(crash['id'])};")
            assert sql('set role service_role; select fixer_claim_forward_media_20261006('
                + ','.join(literal(v) for v in (crash['id'], token, str(uuid.uuid4()), 'a' * 32))
                + ');', ok=False)
            assert sql('select count(*) from fixer_forward_media_claim_receipt_20261006;') == '0'
            # Deletion keeps observations but cannot associate them with a new UUID.
            sql(f'delete from content_calendar where id={literal(first["id"])};')
            assert 'row unavailable' in sql(request, ok=False)
            assert sql('select count(*) from fixer_forward_media_observation_20261007;') == '3'
            print('PASS PG17: exact row/revision, immutable raw replay, concurrent locks, '
                  'protected rows, canonical alias, crash publish hold, role isolation; '
                  'no registry/clearance/manifest/lineage authority created')
        finally:
            if started:
                subprocess.run(['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                               check=True, capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
