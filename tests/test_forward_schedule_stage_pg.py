"""Standalone disposable PG acceptance for DRAFT_fixer_forward_schedule_stage_20261008.

Run `python3 tests/test_forward_schedule_stage_pg.py` (also under pytest; the
module-level main() runs in test_schedule_stage_pg). Uses only stdlib and
existing initdb/pg_ctl/psql against a real disposable PostgreSQL cluster.
No DSN, network, production connection, installs or persisted cluster.
Requires PostgreSQL 17 binaries. Skips cleanly only when the binaries are absent.
"""
import hashlib
import json
import sys
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_USED_PHASH = [0]


def _fresh_phash():
    for mask in range(1, 64):
        unsigned = sum((bin(i & mask).count('1') % 2) << i for i in range(64))
        if all(((unsigned ^ (used & ((1 << 64) - 1))).bit_count()) > 30 for used in _USED_PHASH):
            signed = unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned
            _USED_PHASH.append(signed)
            return signed
    raise AssertionError('independent pHash fixture pool exhausted')


def _pg(name):
    bundled = Path('/opt/homebrew/opt/postgresql@17/bin') / name
    return str(bundled) if bundled.is_file() else shutil.which(name)


def _skipped():
    return any(not _pg(name) for name in ('initdb', 'pg_ctl', 'psql'))


def main():
    if _skipped():
        print('SKIP: existing initdb/pg_ctl/psql unavailable; disposable PostgreSQL not provisioned')
        return
    with tempfile.TemporaryDirectory(prefix='fixer_stage_pg_', dir='/tmp') as tmp:
        work = Path(tmp)
        sock = work / 'sock'
        sock.mkdir()
        data = work / 'data'
        subprocess.run([_pg('initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run([_pg('pg_ctl'), '-D', str(data), '-l', str(work / 'pg.log'),
                        '-o', f"-k {sock} -p 55473 -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        base = [_pg('psql'), '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(sock),
                '-p', '55473', '-U', 'postgres', '-d', 'postgres']

        def sql(s, ok=True):
            result = subprocess.run(base, input=s, text=True, capture_output=True, timeout=30)
            if ok and result.returncode:
                raise AssertionError(result.stderr)
            if not ok:
                assert result.returncode != 0, 'unexpected SQL success: ' + s
            return result.stdout.strip() if ok else result.stderr

        def lit(value):
            return "'" + str(value).replace("'", "''") + "'"

        def observation(tenant, row):
            digest_input_obj = {
                'schema_version': 1, 'provenance_status': 'unverified',
                'tenant': tenant, 'source_asset_id': row['source_media_asset_id'],
                'source_exact_url': row['source_media_url'],
                'delivered_exact_url': row['image_url'],
                'source_sha256': uuid.uuid4().hex + uuid.uuid4().hex,
                'delivered_sha256': uuid.uuid4().hex + uuid.uuid4().hex,
                'source_byte_length': 10, 'delivered_byte_length': 10,
                'recipe': {'kind': 'fixture'}, 'hold_reasons': []}
            digest_input = json.dumps(digest_input_obj, sort_keys=True, separators=(',', ':'))
            digest = hashlib.sha256(digest_input.encode()).hexdigest()
            observation_json = json.dumps(dict(digest_input_obj, observation_digest=digest),
                                          sort_keys=True, separators=(',', ':'))
            return {'observation_json': observation_json, 'digest_input': digest_input}

        def member(tenant, day='2026-10-10', logical=None, with_obs=True, url=None):
            rid = str(uuid.uuid4())
            url = url or 'https://scratch.example/' + uuid.uuid4().hex
            row = {'id': rid, 'gym_id': tenant, 'post_date': day, 'status': 'pending',
                   'account': 'instagram', 'format': 'feed',
                   'caption': 'staged caption ' + uuid.uuid4().hex,
                   'logical_post_id': logical or str(uuid.uuid4()),
                   'source_media_url': url, 'image_url': url,
                   'source_media_asset_id': 'staged_' + uuid.uuid4().hex,
                   'visual_group_key': 'vg_' + uuid.uuid4().hex}
            entry = {'row': row,
                     'observation': observation(tenant, row) if with_obs else None}
            return entry

        def stage_command(tenant, batch_id, members, old_rows=None, digest_override=None):
            request = json.dumps({'members': members, 'old_rows': old_rows or []},
                                 sort_keys=True, separators=(',', ':'))
            digest = digest_override or hashlib.sha256(request.encode()).hexdigest()
            return ('set role service_role; select public.stage_forward_schedule_batch_20261008('
                    + lit(tenant) + ',' + lit(batch_id) + ',' + lit(request) + ',' + lit(digest) + ');')

        def stage(tenant, batch_id, members, old_rows=None, ok=True, **kw):
            return sql(stage_command(tenant, batch_id, members, old_rows, **kw), ok)

        def concurrent(cmd_a, cmd_b, delay=0.7):
            """Run two raw psql sessions; B starts `delay` seconds after A."""
            results = {}

            def run(key, cmd):
                results[key] = subprocess.run(base, input=cmd, text=True,
                                              capture_output=True, timeout=90)

            import threading
            import time
            ta = threading.Thread(target=run, args=('a', cmd_a))
            tb = threading.Thread(target=run, args=('b', cmd_b))
            ta.start()
            time.sleep(delay)
            tb.start()
            ta.join()
            tb.join()
            return results['a'], results['b']

        def prepare(rid, fp, lengths=(10, 10, 30)):
            tenant, url, image, thumbnail = sql(
                "select gym_id||'|'||source_media_url||'|'||image_url||'|'||coalesce(thumbnail_url,'')"
                f" from content_calendar where id='{rid}';").split('|')
            existing = sql("select source_asset_id from fixer_forward_media_original_registry_20261006"
                           f" where tenant_id='{tenant}' and source_url='{url}';")
            asset = existing or 'original_' + uuid.uuid4().hex
            if not existing:
                sql("insert into fixer_forward_media_original_registry_20261006"
                    "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)"
                    f" values('{tenant}','{asset}','{url}','{fp}',{lengths[0]},'owner-verified-original');")
            sql("insert into fixer_forward_media_history_clearance_20261006"
                "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref)"
                f" values('{tenant}','{asset}','{url}','{fp}',{lengths[0]},'owner-verified-original','cleared_unused','independent-fleet-history-audit')"
                " on conflict do nothing;")
            digest = 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex
            thumb = 'null,null,null' if not thumbnail else f"'{thumbnail}','{fp}',{lengths[2]}"
            sql("insert into fixer_forward_media_render_manifest_20261006"
                "(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,"
                "thumbnail_url,thumbnail_fingerprint,thumbnail_length,operation,render_evidence_ref)"
                f" values('{digest}','{tenant}','{asset}','{image}','{fp}',{lengths[1]},{thumb},'same_object','owner-verified-render');")
            sql(f"update content_calendar set source_media_asset_id='{asset}',render_manifest_digest='{digest}' where id='{rid}';")

        def attest(rid, fp, lengths=(10, 10, 30)):
            evidence = str(uuid.uuid4())
            prepare(rid, fp, lengths)
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            sql("set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{rid}','{revision}','{evidence}','{fp}',{lengths[0]},'{fp}',{lengths[1]},null,null,"
                "'same_object','controlled-runtime-test');")
            return evidence

        def visual_attest(rid, tenant, sha64, phash):
            rev = sql("select ('x'||substr(fixer_forward_media_attestation_request_20261006"
                      f"('{rid}')->>'revision',1,15))::bit(60)::bigint;")
            ev, src, img, thumb = sql(
                "select evidence_id||'|'||source_read_receipt||'|'||image_read_receipt||'|'||coalesce(thumbnail_read_receipt::text,'')"
                f" from fixer_forward_media_lineage_20261006 where calendar_row_id='{rid}';").split('|')
            surl, iurl, turl = sql(
                f"select source_media_url||'|'||image_url||'|'||coalesce(thumbnail_url,'') from content_calendar where id='{rid}';").split('|')
            ids = []
            for role, receipt, url in (('original', src, surl), ('delivered', img, iurl),
                                       ('thumbnail', thumb or img, turl or iurl)):
                md5 = sql(f"select substring(fingerprint from 5) from fixer_forward_media_object_read_20261006 where receipt_id='{receipt}';")
                byte_length = sql(f"select byte_length from fixer_forward_media_object_read_20261006 where receipt_id='{receipt}';")
                aid = str(uuid.uuid4())
                ids.append(aid)
                sql("set role fixer_forward_media_attester_20261006; insert into forward_media_visual_attestation"
                    "(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id)"
                    f" values('{aid}','{tenant}','{url}','{role}','{sha64}','{md5}',{byte_length},{phash},{rev},'{ev}','{receipt}');")
            return ids

        def planned_old(tenant=None, day='2026-10-10', logical=None):
            """An ordinary active old row with full reservation proof."""
            tenant = tenant or 'gym_' + uuid.uuid4().hex
            rid = str(uuid.uuid4())
            logical = logical or str(uuid.uuid4())
            url = 'https://scratch.example/' + uuid.uuid4().hex
            fp = 'md5:' + uuid.uuid4().hex
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "image_url,source_media_url,visual_group_key,logical_post_id,"
                "source_media_asset_id,render_manifest_digest,account,format)"
                f" values('{rid}','{tenant}','{day}','pending','active',"
                f"'{url}','{url}','vg_{uuid.uuid4().hex}','{logical}',"
                f"'unprepared_{uuid.uuid4().hex}','sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}','instagram','feed');")
            attest(rid, fp)
            ids = visual_attest(rid, tenant, uuid.uuid4().hex + uuid.uuid4().hex, _fresh_phash())
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            arr = 'array[' + ','.join(f"'{i}'" for i in ids) + ']::uuid[]'
            reservation = sql('set role service_role; select public.reserve_forward_slot_20261008('
                              f"'{rid}','{logical}','{revision}',{arr},null);")
            snapshot = json.loads(sql(f"select to_jsonb(r) from content_calendar r where id='{rid}';"))
            return {'id': rid, 'tenant': tenant, 'logical': logical, 'url': url, 'fp': fp,
                    'reservation': reservation, 'snapshot': snapshot}

        def prepare_candidate(rid, fp=None, sha64=None, phash=None):
            fp = fp or 'md5:' + uuid.uuid4().hex
            attest(rid, fp)
            tenant = sql(f"select gym_id from content_calendar where id='{rid}';")
            ids = visual_attest(rid, tenant, sha64 or uuid.uuid4().hex + uuid.uuid4().hex,
                                _fresh_phash() if phash is None else phash)
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            logical = sql(f"select logical_post_id from content_calendar where id='{rid}';")
            return {'calendar_row_id': rid, 'logical_post_id': logical,
                    'expected_revision': revision, 'attestation_ids': ids}

        def finalize_command(tenant, batch_id, candidates, old):
            return ('set role service_role; select public.finalize_forward_schedule_staged_batch_20261008('
                    + lit(tenant) + ',' + lit(batch_id) + ','
                    + lit(json.dumps(candidates)) + '::jsonb,'
                    + lit(json.dumps(old)) + '::jsonb);')

        def eligible(rid):
            return json.loads(sql(f"select public.forward_schedule_preparation_eligible_20261008('{rid}');"))

        def status(batch_id):
            return json.loads(sql('set role service_role; select public.forward_schedule_batch_status_20261008('
                                  + lit(batch_id) + ');'))

        try:
            sql("create role anon; create role authenticated; create role service_role;"
                "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                "account text,format text,caption text,gbp_location_id text,status text not null check(status in ('draft','pending','approved','published','denied','killed','failed','publishing','deleted','coach_review')),variant_status text not null check(variant_status in ('active','candidate','archived')),"
                "published_at timestamptz,publish_claim_token uuid,"
                "publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,"
                "media_not_ready_reason text);")
            sql((ROOT / 'migrations/logical_post_id_20261004.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_visual_index_20261008.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_media_observation_bridge_20261007.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_schedule_reservation_20261008.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_schedule_stage_20261008.sql').read_text())
            sql('grant select,insert,update,delete on public.content_calendar to service_role;')
            assert sql("select current_setting('server_version_num')::integer between 170000 and 179999;") == 't'
            sql('update forward_media_visual_gate_20261008 set enabled=true;')

            tenant = 'gym_' + uuid.uuid4().hex
            batch = str(uuid.uuid4())
            members = [member(tenant), member(tenant, with_obs=False)]
            ids_expected = [m['row']['id'] for m in members]

            # OFF gate holds staging and staged finalization; nothing persists.
            off_err = stage(tenant, batch, members, ok=False)
            assert 'OFF pending review' in off_err
            assert sql('select count(*) from forward_schedule_stage_batch_20261008;') == '0'
            assert sql(f"select count(*) from content_calendar where id in ('{ids_expected[0]}','{ids_expected[1]}');") == '0'
            sql('update forward_schedule_reservation_gate_20261008 set enabled=true;')

            # Digest must match the exact request bytes.
            assert 'digest invalid' in stage(tenant, batch, members, ok=False, digest_override='0' * 64)
            # Malformed bindings hold.
            assert 'binding required' in sql('set role service_role; select public.stage_forward_schedule_batch_20261008(null,null,null,null);', ok=False)

            # Atomic staging: rows, membership and unverified observations in
            # one receipt; candidate rows are inactive, marked and unapproved.
            receipt = json.loads(stage(tenant, batch, members))
            assert receipt['batch_id'] == batch and receipt['state'] == 'staged'
            assert receipt['member_row_ids'] == ids_expected, receipt
            assert receipt['observation_row_ids'] == [ids_expected[0]], receipt
            assert receipt['finalize_receipt'] is None
            assert receipt['old_row_ids'] == [], receipt
            assert sql(f"select variant_status||'|'||status||'|'||media_not_ready_reason from content_calendar where id='{ids_expected[0]}';") == 'candidate|pending|forward_reservation_staged'
            assert sql(f"select provenance_status from fixer_forward_media_observation_20261007 where calendar_row_id='{ids_expected[0]}';") == 'unverified'
            assert sql(f"select count(*) from forward_schedule_stage_member_20261008 where batch_id='{batch}';") == '2'

            # Exact retry returns the same receipt and rewrites nothing.
            retry = json.loads(stage(tenant, batch, members))
            assert retry == receipt, (retry, receipt)
            assert sql(f"select count(*) from fixer_forward_media_observation_20261007 where calendar_row_id='{ids_expected[0]}';") == '1'
            # Same batch UUID with a changed request is refused fail-closed.
            changed = stage(tenant, batch, [member(tenant)], ok=False)
            assert 'stage batch request changed' in changed
            # Lost stage response readback resolves from the durable record.
            readback = status(batch)
            assert readback == receipt, (readback, receipt)
            # A calendar row already staged cannot move to another batch.
            conflict_member = [member(tenant), dict(members[0])]
            assert 'already staged' in stage(tenant, str(uuid.uuid4()), conflict_member, ok=False)

            # Atomic member failure: nothing from the batch persists.
            bad = member(tenant)
            bad['row']['source_media_url'] = 'http://insecure.example/x'
            bad_batch = str(uuid.uuid4())
            assert 'payload invalid' in stage(tenant, bad_batch, [member(tenant), bad], ok=False)
            assert sql(f"select count(*) from forward_schedule_stage_batch_20261008 where batch_id='{bad_batch}';") == '0'
            assert sql(f"select count(*) from forward_schedule_stage_member_20261008 where batch_id='{bad_batch}';") == '0'

            # Observation contract is validated against the staged binding.
            bad_obs = member(tenant)
            bad_obs['observation']['observation_json'] = '{"schema_version":"2"}'
            assert 'observation' in stage(tenant, str(uuid.uuid4()), [bad_obs], ok=False)

            # Old-row set is captured AT STAGE with full-column CAS and bound
            # to the batch + immutable digest. Stage refuses a stale snapshot,
            # a protected (approved/held/claimed/sent) row, a foreign-tenant
            # row, an unknown column key, a missing row, and any duplicate or
            # member-colliding identity -- atomically, nothing persists.
            old_stage = planned_old(tenant=tenant)
            stale = dict(old_stage['snapshot'])
            stale['image_url'] = 'https://scratch.example/stale_' + uuid.uuid4().hex
            st_batch = str(uuid.uuid4())
            assert 'snapshot changed or protected' in stage(tenant, st_batch, [member(tenant)], [stale], ok=False)
            assert sql(f"select count(*) from forward_schedule_stage_batch_20261008 where batch_id='{st_batch}';") == '0'
            approved_old = planned_old(tenant=tenant)
            sql(f"update content_calendar set status='approved' where id='{approved_old['id']}';")
            approved_snap = json.loads(sql(f"select to_jsonb(r) from content_calendar r where id='{approved_old['id']}';"))
            assert 'snapshot changed or protected' in stage(tenant, str(uuid.uuid4()), [member(tenant)], [approved_snap], ok=False)
            held_old = planned_old(tenant=tenant)
            sql(f"update content_calendar set media_not_ready_reason='held' where id='{held_old['id']}';")
            held_snap = json.loads(sql(f"select to_jsonb(r) from content_calendar r where id='{held_old['id']}';"))
            assert 'snapshot changed or protected' in stage(tenant, str(uuid.uuid4()), [member(tenant)], [held_snap], ok=False)
            foreign_old = planned_old()  # different tenant
            assert 'batch tenant mismatch' in stage(tenant, str(uuid.uuid4()), [member(tenant)], [foreign_old['snapshot']], ok=False)
            unknown_col = dict(old_stage['snapshot'])
            unknown_col['not_a_column'] = 1
            assert 'calendar column unavailable' in stage(tenant, str(uuid.uuid4()), [member(tenant)], [unknown_col], ok=False)
            ghost = dict(old_stage['snapshot'])
            ghost['id'] = str(uuid.uuid4())
            assert 'snapshot changed or protected' in stage(tenant, str(uuid.uuid4()), [member(tenant)], [ghost], ok=False)
            dup_member = member(tenant)
            assert 'duplicate or missing calendar batch identity' in stage(
                tenant, str(uuid.uuid4()), [dup_member],
                [old_stage['snapshot'], old_stage['snapshot']], ok=False)
            colliding = dict(old_stage['snapshot'])
            colliding['id'] = dup_member['row']['id']
            assert 'duplicate or missing calendar batch identity' in stage(
                tenant, str(uuid.uuid4()), [dup_member], [colliding], ok=False)

            # Preparation predicate: staged members eligible; ordinary active
            # rows keep their own lane; every tamper fails closed.
            state0 = eligible(ids_expected[0])
            assert state0['eligible'] is True and state0['mode'] == 'staged' and state0['batch_id'] == batch, state0
            active_row = planned_old()
            active_state = eligible(active_row['id'])
            assert active_state['eligible'] is True and active_state['mode'] == 'active', active_state
            assert eligible(str(uuid.uuid4()))['eligible'] is False
            tamper = member(tenant)
            tamper_batch = str(uuid.uuid4())
            stage(tenant, tamper_batch, [tamper])
            trid = tamper['row']['id']
            sql(f"update content_calendar set image_url='https://scratch.example/tampered_{uuid.uuid4().hex}' where id='{trid}';")
            assert eligible(trid)['reason'] == 'content/media binding changed'
            sql(f"update content_calendar set image_url='{tamper['row']['image_url']}' where id='{trid}';")
            assert eligible(trid)['eligible'] is True
            token = str(uuid.uuid4())
            sql(f"update content_calendar set publish_claim_token='{token}' where id='{trid}';")
            assert eligible(trid)['reason'] == 'publish claim exists'
            sql(f"update content_calendar set publish_claim_token=null where id='{trid}';")
            sql(f"update content_calendar set status='approved' where id='{trid}';")
            assert eligible(trid)['reason'] == 'candidate status not preparable'
            sql(f"update content_calendar set status='pending' where id='{trid}';")
            sql(f"update content_calendar set media_not_ready_reason=null where id='{trid}';")
            assert eligible(trid)['reason'] == 'not a staged schedule candidate'
            sql(f"update content_calendar set media_not_ready_reason='forward_reservation_staged' where id='{trid}';")
            # Claim/send remains active-only: a staged candidate can never
            # pass the publication RPC even with a fabricated claim token.
            sql(f"update content_calendar set status='publishing',publish_claim_token='{token}' where id='{trid}';")
            fake_ids = 'array[' + ','.join(f"'{uuid.uuid4()}'" for _ in range(3)) + ']::uuid[]'
            assert 'permission denied' not in sql('set role service_role; select public.fixer_forward_visual_index_claim_20261008('
                                                  f"'{trid}','{token}','{uuid.uuid4()}','x',{fake_ids});", ok=False)
            sql(f"update content_calendar set status='pending',publish_claim_token=null where id='{trid}';")

            # Staged-content binding (P1 regression): a caller edit to ANY
            # planned content/media/tenant field after staging must not
            # inherit the batch. Each mutation flips the predicate fail-closed
            # and holds the whole finalization BEFORE any activation or
            # old-row archive; reverting restores both, and the prepared batch
            # then finalizes normally. Only the trusted preparation output
            # fields (source_media_asset_id, render_manifest_digest) may
            # differ from the persisted staged snapshot.
            tamper2_old = planned_old(tenant=tenant)
            tamper2_batch = str(uuid.uuid4())
            tamper2_member = member(tenant)
            stage(tenant, tamper2_batch, [tamper2_member], [tamper2_old['snapshot']])
            t2 = tamper2_member['row']['id']
            t2_c = prepare_candidate(t2)
            orig = json.loads(sql(f"select to_jsonb(r) from content_calendar r where id='{t2}';"))
            assert eligible(t2)['eligible'] is True and eligible(t2)['mode'] == 'staged'
            for col, val in (('account', 'facebook'),
                             ('format', 'story'),
                             ('caption', 'caller rewritten ' + uuid.uuid4().hex),
                             ('visual_group_key', 'vg_hijack_' + uuid.uuid4().hex),
                             ('post_date', '2026-10-11')):
                sql(f"update content_calendar set {col}=" + lit(val) + f" where id='{t2}';")
                state = eligible(t2)
                assert state['eligible'] is False and 'changed' in state['reason'], (col, state)
                err = sql(finalize_command(tenant, tamper2_batch, [t2_c], [tamper2_old['snapshot']]), ok=False)
                assert 'changed' in err, (col, err)
                # No activation, no old-row archive, batch still staged.
                assert status(tamper2_batch)['state'] == 'staged'
                assert sql(f"select variant_status||'|'||media_not_ready_reason from content_calendar where id='{t2}';") == 'candidate|forward_reservation_staged'
                assert sql(f"select variant_status from content_calendar where id='{tamper2_old['id']}';") == 'active'
                assert sql(f"select state from forward_schedule_reservation where reservation_id='{tamper2_old['reservation']}';") == 'active'
                sql(f"update content_calendar set {col}=" + lit(orig[col]) + f" where id='{t2}';")
                assert eligible(t2)['eligible'] is True, col
            # Every field restored: the prepared batch finalizes normally.
            t2_result = json.loads(sql(finalize_command(tenant, tamper2_batch, [t2_c], [tamper2_old['snapshot']])))
            assert t2_result['state'] == 'finalized' and t2_result['row_ids'] == [t2], t2_result
            assert t2_result['archived_old_row_ids'] == [tamper2_old['id']], t2_result
            assert sql(f"select variant_status from content_calendar where id='{t2}';") == 'active'
            assert sql(f"select variant_status from content_calendar where id='{tamper2_old['id']}';") == 'archived'

            # ACLs: stage/finalize/status are service_role-only; the predicate
            # is available to the isolated owner/attester roles, never public.
            for denied in ('anon', 'authenticated', 'fixer_forward_media_attester_20261006',
                           'fixer_forward_media_owner_20261006'):
                cmd = stage_command(tenant, str(uuid.uuid4()), [member(tenant)]).replace('set role service_role;', f'set role {denied};')
                assert 'permission denied' in sql(cmd, ok=False)
                assert 'permission denied' in sql(f'set role {denied}; select public.forward_schedule_batch_status_20261008(' + lit(batch) + ');', ok=False)
                assert 'permission denied' in sql(f'set role {denied}; select public.finalize_forward_schedule_staged_batch_20261008('
                                                  + lit(tenant) + ',' + lit(batch) + ",'[]'::jsonb,'[]'::jsonb);", ok=False)
            for allowed in ('fixer_forward_media_owner_20261006', 'fixer_forward_media_attester_20261006', 'service_role'):
                out = json.loads(sql(f'set role {allowed}; select public.forward_schedule_preparation_eligible_20261008(' + lit(ids_expected[0]) + ');'))
                assert out['eligible'] is True, (allowed, out)
            assert 'permission denied' in sql('set role anon; select public.forward_schedule_preparation_eligible_20261008(' + lit(ids_expected[0]) + ');', ok=False)
            assert 'permission denied' in sql(f'set role service_role; update forward_schedule_stage_batch_20261008 set state=' + lit('finalized') + ';', ok=False)
            assert 'permission denied' in sql(f'set role service_role; insert into forward_schedule_stage_member_20261008(batch_id,position,calendar_row_id,logical_post_id,post_date,gym_id,tenant_id,source_media_url,image_url,staged_snapshot)'
                                              f" values('{batch}',9,'{uuid.uuid4()}','{uuid.uuid4()}','2026-10-10','x','x','https://scratch.example/x','https://scratch.example/x','{{}}'::jsonb);", ok=False)
            # Even with grants + policies, direct DML is RPC-owner only and the
            # batch identity/digest can never change; membership is immutable.
            sql('grant insert,update on public.forward_schedule_stage_batch_20261008 to service_role;'
                'create policy tmp_batch on public.forward_schedule_stage_batch_20261008 for all to service_role using(true) with check(true);')
            assert 'RPC-managed only' in sql(f"set role service_role; update forward_schedule_stage_batch_20261008 set request_digest='{'1' * 64}' where batch_id='{batch}';", ok=False)
            sql('drop policy tmp_batch on public.forward_schedule_stage_batch_20261008;'
                'revoke insert,update on public.forward_schedule_stage_batch_20261008 from service_role;')
            assert 'durable' in sql(f"delete from forward_schedule_stage_batch_20261008 where batch_id='{batch}';", ok=False)
            assert 'immutable' in sql(f"delete from forward_schedule_stage_member_20261008 where batch_id='{batch}';", ok=False)
            # Persisted old-row evidence: service reads only; no direct writes
            # even with grants + permissive policies; rows are immutable.
            assert 'permission denied' in sql('set role anon; select count(*) from forward_schedule_stage_old_row_20261008;', ok=False)
            assert 'permission denied' in sql('set role service_role; insert into forward_schedule_stage_old_row_20261008(batch_id,position,calendar_row_id,tenant_id,old_snapshot)'
                                              f" values('{batch}',9,'{uuid.uuid4()}','x','{{}}'::jsonb);", ok=False)
            sql('grant insert,update,delete on public.forward_schedule_stage_old_row_20261008 to service_role;'
                'create policy tmp_old on public.forward_schedule_stage_old_row_20261008 for all to service_role using(true) with check(true);')
            assert 'RPC-managed only' in sql('set role service_role; insert into forward_schedule_stage_old_row_20261008(batch_id,position,calendar_row_id,tenant_id,old_snapshot)'
                                             f" values('{batch}',9,'{uuid.uuid4()}','x','{{}}'::jsonb);", ok=False)
            sql('drop policy tmp_old on public.forward_schedule_stage_old_row_20261008;'
                'revoke insert,update,delete on public.forward_schedule_stage_old_row_20261008 from service_role;')

            # Incomplete membership refuses before any activation. This batch
            # was staged with an EMPTY old-row set.
            c0 = prepare_candidate(ids_expected[0])
            c1 = prepare_candidate(ids_expected[1])
            assert 'incomplete staged batch membership' in sql(finalize_command(tenant, batch, [c0], []), ok=False)
            extra = dict(c0, calendar_row_id=str(uuid.uuid4()))
            assert 'incomplete staged batch membership' in sql(finalize_command(tenant, batch, [c0, c1, extra], []), ok=False)

            # Caller old-row substitution refuses: the persisted set for this
            # batch is EMPTY, so any caller-supplied old row mismatches.
            stray = planned_old(tenant=tenant)
            assert 'staged old row set mismatch' in sql(finalize_command(tenant, batch, [c0, c1], [stray['snapshot']]), ok=False)
            assert status(batch)['state'] == 'staged'

            # Old approval race: client approval after STAGING holds the
            # entire batch fail-closed (persisted CAS snapshot differs from
            # the live row) and the batch stays staged.
            raced = planned_old(tenant=tenant)
            race_batch = str(uuid.uuid4())
            race_member = member(tenant)
            stage(tenant, race_batch, [race_member], [raced['snapshot']])
            race_c = prepare_candidate(race_member['row']['id'])
            sql(f"update content_calendar set status='approved' where id='{raced['id']}';")
            assert 'snapshot changed or protected' in sql(finalize_command(tenant, race_batch, [race_c], [raced['snapshot']]), ok=False)
            assert status(race_batch)['state'] == 'staged'
            assert sql(f"select variant_status from content_calendar where id='{race_member['row']['id']}';") == 'candidate'

            # Global conflict: a competing different-source reservation on the
            # same logical slot holds the staged batch without a CAS token.
            conflict_logical = str(uuid.uuid4())
            competitor = planned_old(tenant=tenant, logical=conflict_logical)
            held_batch = str(uuid.uuid4())
            held_member = member(tenant, logical=conflict_logical)
            stage(tenant, held_batch, [held_member])
            held_c = prepare_candidate(held_member['row']['id'])
            assert 'slot conflict' in sql(finalize_command(tenant, held_batch, [held_c], []), ok=False)
            assert status(held_batch)['state'] == 'staged'
            assert sql(f"select variant_status||'|'||media_not_ready_reason from content_calendar where id='{held_member['row']['id']}';") == 'candidate|forward_reservation_staged'
            assert sql(f"select count(*) from forward_schedule_reservation where calendar_row_id='{held_member['row']['id']}';") == '0'
            assert sql(f"select state from forward_schedule_reservation where reservation_id='{competitor['reservation']}';") == 'active'

            # Happy path: atomic activation + reservation + terminal receipt in
            # one transaction; exact finalize retry returns the persisted
            # receipt and a changed request refuses.
            result = json.loads(sql(finalize_command(tenant, batch, [c0, c1], [])))
            assert result['batch_id'] == batch and result['state'] == 'finalized'
            assert result['row_ids'] == ids_expected and len(set(result['reservation_ids'])) == 2
            assert result['archived_old_row_ids'] == []
            assert result['tenant_id'] == tenant
            assert result['request_digest'] == status(batch)['request_digest']
            assert sql(f"select count(*) from content_calendar where id in ('{ids_expected[0]}','{ids_expected[1]}') and variant_status='active' and media_not_ready_reason is null;") == '2'
            persisted = status(batch)
            assert persisted['state'] == 'finalized' and persisted['finalize_receipt'] == result, persisted
            again = json.loads(sql(finalize_command(tenant, batch, [c0, c1], [])))
            assert again == result
            assert 'finalized schedule batch request changed' in sql(finalize_command(tenant, batch, [c1, c0], []), ok=False)
            # Finalized batch is terminal for the preparation lane as well.
            assert eligible(ids_expected[0])['mode'] == 'active'

            # Persisted old-row set finalization: stage binds the exact old
            # snapshot; finalization uses ONLY that persisted set, archives
            # exactly those rows, and never touches a row added after staging.
            old = planned_old(tenant=tenant)
            old_batch = str(uuid.uuid4())
            old_member = member(tenant)
            old_receipt = json.loads(stage(tenant, old_batch, [old_member], [old['snapshot']]))
            assert old_receipt['old_row_ids'] == [old['id']], old_receipt
            assert status(old_batch)['old_row_ids'] == [old['id']]
            assert sql(f"select count(*) from forward_schedule_stage_old_row_20261008 where batch_id='{old_batch}' and calendar_row_id='{old['id']}';") == '1'
            # Persisted old-row evidence is immutable even for the owner.
            assert 'immutable' in sql(f"delete from forward_schedule_stage_old_row_20261008 where batch_id='{old_batch}';", ok=False)
            oc = prepare_candidate(old_member['row']['id'])
            # Caller mismatch: empty set, tampered snapshot and a superset all
            # refuse before any write.
            assert 'staged old row set mismatch' in sql(finalize_command(tenant, old_batch, [oc], []), ok=False)
            tampered_old = dict(old['snapshot'])
            tampered_old['image_url'] = 'https://scratch.example/tampered_' + uuid.uuid4().hex
            assert 'staged old row set mismatch' in sql(finalize_command(tenant, old_batch, [oc], [tampered_old]), ok=False)
            assert 'staged old row set mismatch' in sql(finalize_command(tenant, old_batch, [oc], [old['snapshot'], stray['snapshot']]), ok=False)
            assert status(old_batch)['state'] == 'staged'
            # Post-stage old-row CHANGE holds (persisted CAS vs live row).
            sql(f"update content_calendar set image_url='https://scratch.example/edited_{uuid.uuid4().hex}' where id='{old['id']}';")
            assert 'snapshot changed or protected' in sql(finalize_command(tenant, old_batch, [oc], [old['snapshot']]), ok=False)
            assert status(old_batch)['state'] == 'staged'
            sql(f"update content_calendar set image_url='{old['url']}' where id='{old['id']}';")
            # Post-stage INSERTION: a new active pending/draft row on the same
            # tenant is never part of the persisted set and is never archived.
            added = planned_old(tenant=tenant)
            old_result = json.loads(sql(finalize_command(tenant, old_batch, [oc], [old['snapshot']])))
            assert old_result['archived_old_row_ids'] == [old['id']], old_result
            assert sql(f"select variant_status from content_calendar where id='{old['id']}';") == 'archived'
            assert sql(f"select state from forward_schedule_reservation where reservation_id='{old['reservation']}';") == 'released'
            assert sql(f"select variant_status||'|'||status from content_calendar where id='{added['id']}';") == 'active|pending'
            assert sql(f"select state from forward_schedule_reservation where reservation_id='{added['reservation']}';") == 'active'
            # Lost finalize response: exact retry returns the persisted receipt.
            assert json.loads(sql(finalize_command(tenant, old_batch, [oc], [old['snapshot']]))) == old_result

            # Two-session race, stage: concurrent same-batch requests with
            # DIFFERENT digests. The loser blocks on the in-flight batch row
            # and is refused after the winner commits; exactly one batch
            # identity persists with the winner's digest.
            sr_batch = str(uuid.uuid4())
            sr_a_members = [member(tenant)]
            sr_b_members = [member(tenant)]
            sr_a = stage_command(tenant, sr_batch, sr_a_members)
            sr_b = stage_command(tenant, sr_batch, sr_b_members)
            sr_a_request = json.dumps({'members': sr_a_members, 'old_rows': []},
                                      sort_keys=True, separators=(',', ':'))
            ra, rb = concurrent('begin; ' + sr_a + ' select pg_sleep(2); commit;', sr_b)
            assert ra.returncode == 0, ra.stderr
            assert rb.returncode != 0 and 'stage batch request changed' in rb.stderr, rb.stderr
            sr_status = status(sr_batch)
            assert sr_status['state'] == 'staged'
            assert sr_status['request_digest'] == hashlib.sha256(sr_a_request.encode()).hexdigest()
            assert sr_status['member_row_ids'] == [sr_a_members[0]['row']['id']]

            # Two-session race, finalize: identical concurrent finalize. The
            # loser blocks on the batch row lock, then replays the persisted
            # terminal receipt -- exactly one finalization happens.
            fr_batch = str(uuid.uuid4())
            fr_member = member(tenant)
            stage(tenant, fr_batch, [fr_member])
            fr_c = prepare_candidate(fr_member['row']['id'])
            fr_cmd = finalize_command(tenant, fr_batch, [fr_c], [])
            fa, fb = concurrent(
                'begin; ' + fr_cmd + ' select pg_sleep(2); commit;',
                fr_cmd)
            assert fa.returncode == 0, fa.stderr
            assert fb.returncode == 0, fb.stderr
            fa_receipt = json.loads(next(l for l in fa.stdout.splitlines() if l.strip().startswith('{')))
            fb_receipt = json.loads(next(l for l in fb.stdout.splitlines() if l.strip().startswith('{')))
            assert fa_receipt == fb_receipt and fa_receipt['state'] == 'finalized'
            assert status(fr_batch)['finalize_receipt'] == fa_receipt
            # A changed request against the finalized batch still refuses.
            assert 'finalized schedule batch request changed' in sql(
                finalize_command(tenant, fr_batch, [fr_c], [stray['snapshot']]), ok=False)

            # Failing SECOND candidate rolls back the FIRST activation and
            # reservation; the batch remains staged for retry.
            rb_tenant = 'gym_' + uuid.uuid4().hex
            rb_batch = str(uuid.uuid4())
            shared_url = 'https://scratch.example/' + uuid.uuid4().hex
            shared_fp = 'md5:' + uuid.uuid4().hex
            shared_sha = uuid.uuid4().hex + uuid.uuid4().hex
            shared_phash = _fresh_phash()
            rb_m1 = member(rb_tenant, url=shared_url)
            rb_m2 = member(rb_tenant, url=shared_url)  # same bytes, different logical post
            stage(rb_tenant, rb_batch, [rb_m1, rb_m2])
            rb_c1 = prepare_candidate(rb_m1['row']['id'], fp=shared_fp, sha64=shared_sha, phash=shared_phash)
            rb_c2 = prepare_candidate(rb_m2['row']['id'], fp=shared_fp, sha64=shared_sha, phash=shared_phash)
            assert 'another tenant/date/logical post' in sql(finalize_command(rb_tenant, rb_batch, [rb_c1, rb_c2], []), ok=False)
            assert status(rb_batch)['state'] == 'staged'
            assert sql(f"select count(*) from content_calendar where id in ('{rb_m1['row']['id']}','{rb_m2['row']['id']}') and variant_status='candidate' and media_not_ready_reason='forward_reservation_staged';") == '2'
            assert sql(f"select count(*) from forward_schedule_reservation where calendar_row_id in ('{rb_m1['row']['id']}','{rb_m2['row']['id']}');") == '0'
            # Isolation guard matches the stack.
            assert 'read committed isolation' in sql('begin isolation level repeatable read; set role service_role; '
                                                     + stage_command(rb_tenant, str(uuid.uuid4()), [member(rb_tenant)]).replace('set role service_role; ', '')
                                                     + ' commit;', ok=False)

            print('PASS: PG17 forward schedule stage; OFF gate hold; digest binding; atomic stage of '
                  'inactive candidates + unverified observations + immutable membership; exact retry '
                  'receipt + changed digest refusal + lost-response readback; already-staged conflict; '
                  'atomic member failure; observation contract; stage-time old-row CAS persistence '
                  'with stale/protected/foreign/unknown-column/ghost/duplicate/member-collision holds; '
                  'preparation predicate active/staged lanes '
                  'with tenant/binding/marker/claim/send/approval negatives; claim stays active-only; '
                  'staged full-snapshot binding: post-stage caller edits to account/format/caption/'
                  'visual_group_key/post_date flip the predicate, hold finalization with no activation '
                  'and no old-row archive, and revert restores; trusted preparation output fields '
                  '(source_media_asset_id, render_manifest_digest) remain mutable; '
                  'ACL + RPC-owner write guards + old-row immutability/durability; incomplete membership '
                  'refusal; caller old-row mismatch refusal (empty/tampered/superset); post-stage old-row '
                  'change hold; post-stage inserted row never archived; old approval race hold; global '
                  'slot conflict hold; atomic finalize with terminal receipt, exact retry and '
                  'changed-request refusal; two-session stage race (one winner, loser refused) and '
                  'two-session finalize race (identical receipts, one finalization); '
                  'second-candidate full rollback; isolation guard')
        finally:
            subprocess.run([_pg('pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           capture_output=True, timeout=60)


def test_schedule_stage_pg():
    if _skipped():
        import pytest
        pytest.skip('existing initdb/pg_ctl/psql unavailable; disposable PostgreSQL not provisioned')
    main()


if __name__ == '__main__':
    main()
