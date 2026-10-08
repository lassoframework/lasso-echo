"""Standalone disposable PG acceptance; run python3 this_file.py.
Uses only stdlib and existing initdb/pg_ctl/psql. No DSN, network, production
connection, installs or persisted cluster; applies real prerequisite drafts.
"""
import concurrent.futures
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    for name in ('initdb', 'pg_ctl', 'psql'):
        if not shutil.which(name):
            raise SystemExit(f'BLOCKED: existing {name} unavailable; no install attempted')
    with tempfile.TemporaryDirectory(prefix='fixer_claim_pg_', dir='/tmp') as tmp:
        work = Path(tmp)
        sock = work / 'sock'
        sock.mkdir()
        data = work / 'data'
        subprocess.run(['initdb', '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(['pg_ctl', '-D', str(data), '-l', str(work / 'pg.log'),
                        '-o', f"-k {sock} -p 55468 -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        base = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(sock),
                '-p', '55468', '-U', 'postgres', '-d', 'postgres']

        def sql(s, ok=True):
            result = subprocess.run(base, input=s, text=True, capture_output=True, timeout=30)
            if ok and result.returncode:
                raise AssertionError(result.stderr)
            if not ok:
                assert result.returncode != 0, 'unexpected SQL success'
            return result.stdout.strip() if ok else result.stderr

        def claim(pair, ok=True, expected_revision=None):
            rid, token, evidence = pair
            expected_revision = expected_revision or sql(
                f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            return sql("set role service_role; select public.fixer_claim_forward_media_20261006"
                       f"('{rid}','{token}','{evidence}','{expected_revision}');", ok)

        def row(tenant, group, url, day='2026-10-10', reservation='2026-10-10', image=None, thumbnail=None):
            rid, token = str(uuid.uuid4()), str(uuid.uuid4())
            image = image or url
            thumb = "null" if thumbnail is None else "'" + thumbnail + "'"
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "image_url,source_media_url,thumbnail_url,visual_group_key,publish_claim_token,publish_reservation_day,source_media_asset_id,render_manifest_digest)"
                f" values('{rid}','{tenant}','{day}','publishing','active',"
                f"'{image}','{url}',{thumb},'{group}','{token}','{reservation}','unprepared_{uuid.uuid4().hex}','sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}');")
            return rid, token

        def prepare(pair, fp, image_fp=None, thumb_fp=None, operation='same_object', clearance=True):
            rid = pair[0]
            # The owner prepares authoritative evidence independently; the
            # attester cannot create it from an arbitrary fetched URL/hash.
            tenant, url, image, thumbnail = sql(f"select gym_id||'|'||source_media_url||'|'||image_url||'|'||coalesce(thumbnail_url,'') from content_calendar where id='{rid}';").split('|')
            existing = sql(f"select source_asset_id from fixer_forward_media_original_registry_20261006 where tenant_id='{tenant}' and source_url='{url}';")
            asset = existing or 'original_' + uuid.uuid4().hex
            if not existing:
                sql("insert into fixer_forward_media_original_registry_20261006 "
                    "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref) "
                    f"values('{tenant}','{asset}','{url}','{fp}',10,'owner-verified-original');")
            if clearance:
                sql("insert into fixer_forward_media_history_clearance_20261006 "
                    "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref) "
                    f"values('{tenant}','{asset}','{url}','{fp}',10,'owner-verified-original','cleared_unused','independent-fleet-history-audit') on conflict do nothing;")
            digest = 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex
            thumb = "null,null,null" if not thumbnail or thumb_fp is None else f"'{thumbnail}','{thumb_fp}',30"
            # Missing thumbnail evidence is rejected by the attester, without
            # manufacturing a partially valid manifest.
            if thumbnail and thumb_fp is None:
                thumb = f"'{thumbnail}','md5:{'a'*32}',30"
            sql("insert into fixer_forward_media_render_manifest_20261006 "
                "(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,"
                "thumbnail_url,thumbnail_fingerprint,thumbnail_length,operation,render_evidence_ref) "
                f"values('{digest}','{tenant}','{asset}','{image}','{image_fp or fp}',10,{thumb},'{operation}','owner-verified-render');")
            sql(f"update content_calendar set source_media_asset_id='{asset}',render_manifest_digest='{digest}' where id='{rid}';")
            return asset, digest

        def attest(pair, fp, image_fp=None, thumb_fp=None, operation='same_object', ok=True, revision=None):
            rid, token = pair
            evidence = str(uuid.uuid4())
            prepare(pair, fp, image_fp, thumb_fp, operation)
            revision = revision or sql("set role fixer_forward_media_attester_20261006; select "
                f"fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            thumb = "null,null" if thumb_fp is None else f"'{thumb_fp}',30"
            sql("set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{rid}','{revision}','{evidence}','{fp}',10,'{image_fp or fp}',10,{thumb},"
                f"'{operation}','controlled-runtime-test');", ok)
            return rid, token, evidence

        def seed(fp=None, day='2026-10-10', tenant=None, group=None, url=None, reservation='2026-10-10'):
            tenant = tenant or 'gym_' + uuid.uuid4().hex
            group = group or 'vg_' + uuid.uuid4().hex
            url = url or 'https://scratch.example/' + uuid.uuid4().hex
            fp = fp or 'md5:' + uuid.uuid4().hex
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{tenant}',true) on conflict do nothing;")
            pair = attest(row(tenant,group,url,day,reservation),fp)
            return pair, tenant, group, url, fp

        def race(pair):
            rid,token,evidence = pair
            expected_revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            return subprocess.run(base, input="begin; set role service_role; select "
                f"fixer_claim_forward_media_20261006('{rid}','{token}','{evidence}','{expected_revision}');"
                "select pg_sleep(0.5); commit;", text=True,capture_output=True,timeout=15)

        try:
            sql("create role anon; create role authenticated; create role service_role;"
                "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                "account text,format text,gbp_location_id text,status text,variant_status text,"
                "published_at timestamptz,publish_claim_token uuid,"
                "publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,"
                "media_not_ready_reason text);")
            # Apply only this self-contained draft. NO #306/#307 dependency.
            sql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
            # Production service_role already has table-level write grants. Preserve
            # that capability here so denial must come from the digest guard.
            sql("grant select,insert,update,delete on public.content_calendar to service_role;")
            # Arbitrary persisted source URLs and hashes have no original
            # authority, even when submitted by the separately trusted attester.
            probe=row('provenance_gym','provenance_group','https://scratch.example/provenance')
            probe_fp='md5:'+uuid.uuid4().hex
            probe_revision=sql(f"select fixer_forward_media_attestation_request_20261006('{probe[0]}')->>'revision';")
            assert 'original registry binding unavailable' in sql(
                "set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{probe[0]}','{probe_revision}','{uuid.uuid4()}','{probe_fp}',10,'{probe_fp}',10,null,null,'same_object','unregistered');",ok=False)
            asset,digest=prepare(probe,probe_fp)
            prepared_revision=sql(f"select fixer_forward_media_attestation_request_20261006('{probe[0]}')->>'revision';")
            assert prepared_revision != probe_revision
            assert sql("set role fixer_forward_media_attester_20261006; select "
                f"fixer_forward_media_provenance_lookup_20261006('{probe[0]}')#>>'{{original,source_asset_id}}';")==asset
            assert sql("set role fixer_forward_media_attester_20261006; select count(*) from "
                "fixer_forward_media_pending_attestations_20261006('provenance_gym',1);")=='1'
            assert sql("set role fixer_forward_media_attester_20261006; select count(*) from "
                f"fixer_forward_media_pending_attestations_20261006('provenance_gym',1,'{probe[0]}');")=='0'
            assert sql("set role fixer_forward_media_attester_20261006; select count(*) from "
                "fixer_forward_media_pending_attestations_20261006('provenance_gym',1,'00000000-0000-0000-0000-000000000000');")=='1'
            assert sql("set role fixer_forward_media_attester_20261006; select count(*) from "
                "fixer_forward_media_pending_attestations_20261006('another_gym',1);")=='0'
            assert 'bounded tenant-scoped' in sql("set role fixer_forward_media_attester_20261006; "
                "select fixer_forward_media_pending_attestations_20261006('provenance_gym',101);",ok=False)
            assert 'permission denied' in sql("set role service_role; select "
                f"fixer_forward_media_provenance_lookup_20261006('{probe[0]}');",ok=False)
            assert 'permission denied' in sql("set role service_role; select "
                "fixer_forward_media_pending_attestations_20261006('provenance_gym',1);",ok=False)
            for table in ('original_registry','render_manifest','history_clearance'):
                assert 'permission denied' in sql(f"set role fixer_forward_media_attester_20261006; select * from fixer_forward_media_{table}_20261006;",ok=False)
                assert 'permission denied' in sql(f"set role service_role; select * from fixer_forward_media_{table}_20261006;",ok=False)
                assert 'immutable' in sql(f"delete from fixer_forward_media_{table}_20261006;",ok=False)
                assert 'immutable' in sql(f"truncate fixer_forward_media_{table}_20261006 cascade;",ok=False)
            assert 'authoritative provenance' in sql("set role fixer_forward_media_attester_20261006; "
                "select fixer_attest_forward_media_20261006"
                f"('{probe[0]}','{prepared_revision}','{uuid.uuid4()}','md5:{'f'*32}',10,'{probe_fp}',10,null,null,'same_object','forged-original');",ok=False)
            sql(f"update content_calendar set render_manifest_digest='sha256:{'0'*64}' where id='{probe[0]}';")
            assert 'manifest binding unavailable' in sql("set role fixer_forward_media_attester_20261006; "
                f"select fixer_forward_media_provenance_lookup_20261006('{probe[0]}');",ok=False)
            sql(f"update content_calendar set render_manifest_digest='{digest}',source_media_asset_id='wrong-original' where id='{probe[0]}';")
            assert 'original registry binding unavailable' in sql("set role fixer_forward_media_attester_20261006; "
                f"select fixer_forward_media_provenance_lookup_20261006('{probe[0]}');",ok=False)
            sql(f"delete from content_calendar where id='{probe[0]}';")
            # A registry/Drive upload does not prove historical eligibility.
            uncleared=row('clearance_gym','clearance_group','https://scratch.example/uncleared')
            sql("insert into fixer_forward_media_claim_gate_20261006 values('clearance_gym',true);")
            clearance_fp='md5:'+uuid.uuid4().hex
            clear_asset,clear_digest=prepare(uncleared,clearance_fp,clearance=False)
            clear_revision=sql(f"select fixer_forward_media_attestation_request_20261006('{uncleared[0]}')->>'revision';")
            def raw_clearance(pair, fingerprint, asset, tenant, url, decision='cleared_unused', role=None, length=10):
                prefix='' if role is None else f'set role {role}; '
                return prefix + "insert into fixer_forward_media_history_clearance_20261006 " +                     "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref) " +                     f"values('{tenant}','{asset}','{url}','{fingerprint}',{length},'owner-verified-original','{decision}','independent-fleet-history-audit');"
            assert 'historical eligibility clearance unavailable' in sql(
                "set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{uncleared[0]}','{clear_revision}','{uuid.uuid4()}','{clearance_fp}',10,'{clearance_fp}',10,null,null,'same_object','new-upload');",ok=False)
            assert 'historical eligibility clearance unavailable' in claim((*uncleared,str(uuid.uuid4())),ok=False)
            for role in ('service_role','fixer_forward_media_attester_20261006','authenticated'):
                assert 'permission denied' in sql(raw_clearance(uncleared,clearance_fp,clear_asset,'clearance_gym',
                    'https://scratch.example/uncleared',role=role),ok=False)
            assert sql("set role fixer_forward_media_attester_20261006; select count(*) from "
                "fixer_forward_media_pending_attestations_20261006('clearance_gym',1);")=='0'
            # A stale byte/version clearance cannot clear the current original.
            sql(raw_clearance(uncleared,clearance_fp,clear_asset,'clearance_gym',
                'https://scratch.example/uncleared',length=11))
            assert 'historical eligibility clearance unavailable' in sql("set role fixer_forward_media_attester_20261006; "
                f"select fixer_forward_media_provenance_lookup_20261006('{uncleared[0]}');",ok=False)
            # The same bytes at a new URL/asset and another tenant need their
            # own clearance; eligibility is never inherited from an upload alias.
            cleared=row('cleared_gym','cleared_group','https://scratch.example/cleared')
            alias_fp='md5:'+uuid.uuid4().hex
            prepare(cleared,alias_fp)
            for tenant_name in ('cleared_gym','other_clearance_gym'):
                alias=row(tenant_name,'clearance_group','https://scratch.example/'+uuid.uuid4().hex)
                prepare(alias,alias_fp,clearance=False)
                assert 'historical eligibility clearance unavailable' in sql("set role fixer_forward_media_attester_20261006; "
                    f"select fixer_forward_media_provenance_lookup_20261006('{alias[0]}');",ok=False)
            # Known historical use/uncertainty follows bytes fleet-wide. Even
            # an otherwise valid new asset clearance cannot erase a quarantine.
            for decision in ('hold_used','hold_uncertain'):
                held_fp='md5:'+uuid.uuid4().hex
                known=row('history_'+decision,'history_group','https://scratch.example/'+uuid.uuid4().hex)
                held_asset,_=prepare(known,held_fp,clearance=False)
                held_url=sql(f"select source_media_url from content_calendar where id='{known[0]}';")
                sql(raw_clearance(known,held_fp,held_asset,'history_'+decision,held_url,decision))
                fresh=row('new_'+decision,'fresh_group','https://scratch.example/'+uuid.uuid4().hex)
                prepare(fresh,held_fp)
                sql(f"insert into fixer_forward_media_claim_gate_20261006 values('new_{decision}',true);")
                assert 'historical eligibility clearance unavailable' in sql("set role fixer_forward_media_attester_20261006; "
                    f"select fixer_forward_media_provenance_lookup_20261006('{fresh[0]}');",ok=False)
                assert 'historical eligibility clearance unavailable' in claim((*fresh,str(uuid.uuid4())),ok=False)
                assert sql(f"select count(*) from fixer_forward_media_use_20261006 where fingerprint='{held_fp}';")=='0'
            first, tenant, group, url, fp = seed()
            sql(f"update fixer_forward_media_claim_gate_20261006 set enabled=false where tenant_id='{tenant}';")
            assert 'OFF' in claim(first,ok=False)
            sql(f"update fixer_forward_media_claim_gate_20261006 set enabled=true where tenant_id='{tenant}';")
            assert claim(first) == 't'
            assert claim(first) == 't'
            # A committed exact retry retains its frozen byte reservation even
            # when another tenant later appends benign render ancestry for the
            # same delivered bytes. The retry must not create new occupancy.
            replay_pair,replay_tenant,replay_group,replay_url,replay_fp=seed()
            assert claim(replay_pair)=='t'
            replay_receipt=sql(f"select to_jsonb(c)::text from fixer_forward_media_claim_receipt_20261006 c where claim_token='{replay_pair[1]}';")
            later_fp='md5:'+uuid.uuid4().hex
            later_tenant='later_edge_'+uuid.uuid4().hex
            later_url='https://scratch.example/'+uuid.uuid4().hex
            attest(row(later_tenant,'later_group',later_url,
                       image='https://scratch.example/'+uuid.uuid4().hex),
                   later_fp,image_fp=replay_fp,operation='render')
            assert claim(replay_pair)=='t'
            assert sql(f"select to_jsonb(c)::text from fixer_forward_media_claim_receipt_20261006 c where claim_token='{replay_pair[1]}';")==replay_receipt
            assert sql(f"select count(*) from fixer_forward_media_use_20261006 where fingerprint='{later_fp}';")=='0'
            assert 'owner attested' in claim((replay_pair[0],replay_pair[1],str(uuid.uuid4())),ok=False)
            assert 'outgoing media revision changed' in claim(replay_pair,ok=False,expected_revision='changed-request')
            # Newly discovered consumed ancestry remains unsafe on an exact
            # retry. It cannot be ignored merely because the receipt predates it.
            later_consumed,*_=seed(fp=later_fp)
            assert claim(later_consumed)=='t'
            assert 'another tenant/date/group' in claim(replay_pair,ok=False)
            # A later negative historical decision on that added ancestor also
            # holds the exact retry, without rewriting its immutable receipt.
            later_hold=row('later_hold','hold_group','https://scratch.example/'+uuid.uuid4().hex)
            later_asset,_=prepare(later_hold,later_fp,clearance=False)
            later_hold_url=sql(f"select source_media_url from content_calendar where id='{later_hold[0]}';")
            sql(raw_clearance(later_hold,later_fp,later_asset,'later_hold',later_hold_url,'hold_uncertain'))
            assert 'original or rendition historical eligibility held' in claim(replay_pair,ok=False)
            assert sql(f"select to_jsonb(c)::text from fixer_forward_media_claim_receipt_20261006 c where claim_token='{replay_pair[1]}';")==replay_receipt
            sibling = attest(row(tenant,group,url),fp)
            assert claim(sibling) == 't'
            other = attest(row(tenant,group,url,'2026-10-11'),fp)
            assert 'another tenant/date/group' in claim(other,ok=False)
            # Fleet uniqueness, including identical URLs under independent gyms.
            other_gym, *_ = seed(fp=fp,day='2026-10-11')
            assert 'another tenant/date/group' in claim(other_gym,ok=False)
            other_group = attest(row(tenant,'vg_unrelated',url),fp)
            assert 'another tenant/date/group' in claim(other_group,ok=False)
            # Catch-up uses today's owned reservation independently of content date.
            catchup, ct, cg, cu, cf = seed(day='2026-10-01',reservation='2026-10-10')
            assert claim(catchup) == 't'
            catch_sibling = attest(row(ct,cg,cu,'2026-10-01','2026-10-11'),cf)
            assert claim(catch_sibling) == 't'
            sql(f"update content_calendar set publish_reservation_day='2026-10-12' where id='{catchup[0]}';")
            assert 'receipt differs' in claim(catchup,ok=False)
            # Ordinary service credentials cannot forge byte/lineage evidence.
            revision=sql(f"select fixer_forward_media_attestation_request_20261006('{first[0]}')->>'revision';")
            assert 'permission denied' in sql("set role service_role; select fixer_attest_forward_media_20261006"
                f"('{first[0]}','{revision}','{uuid.uuid4()}','{fp}',10,'{fp}',10,null,null,'same_object','forged');",ok=False)
            assert 'permission denied' in sql("set role service_role; insert into fixer_forward_media_object_read_20261006"
                f" values('{uuid.uuid4()}','{tenant}','https://scratch.example/forged','{fp}',10,'forged','forged',now());",ok=False)
            assert 'permission denied' in sql("set role authenticated; select fixer_claim_forward_media_20261006"
                f"('{first[0]}','{first[1]}','{first[2]}','revision');",ok=False)
            assert 'permission denied' in sql("set role service_role; update fixer_forward_media_claim_gate_20261006 set enabled=true;",ok=False)
            assert 'permission denied' in sql("set role fixer_forward_media_attester_20261006; delete from fixer_forward_media_use_20261006;",ok=False)
            missing = (*row(tenant,group,url),str(uuid.uuid4()))
            assert 'original registry binding unavailable' in claim(missing,ok=False)
            forged = (*row(tenant,group,url),first[2])
            assert 'original registry binding unavailable' in claim(forged,ok=False)
            assert 'ownership' in claim((first[0],str(uuid.uuid4()),first[2]),ok=False)
            # Evidence is bound to persisted revision and exact outgoing objects.
            changed = attest(row(tenant,group,url),fp)
            sql(f"update content_calendar set image_url='https://scratch.example/changed' where id='{changed[0]}';")
            assert 'manifest binding unavailable' in claim(changed,ok=False)
            attest(row(tenant,group,url),fp,ok=False,revision='stale-revision')
            unknown=row(tenant,group,url)
            sql(f"update content_calendar set source_media_url=null where id='{unknown[0]}';")
            assert 'identity unavailable' in sql(f"select fixer_forward_media_attestation_request_20261006('{unknown[0]}');",ok=False)
            # Same immutable URL cannot be rebound to different fetched bytes.
            assert 'authoritative provenance' in sql("set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{first[0]}','{revision}','{uuid.uuid4()}','md5:{'f'*32}',10,'md5:{'f'*32}',10,null,null,'same_object','test');",ok=False)
            # Distinct rendition reserves original too; rollback entire hash set.
            derivative_url='https://scratch.example/' + uuid.uuid4().hex
            derivative_fp='md5:'+'0'*32
            derivative=attest(row(tenant,group,url,'2026-10-11',image=derivative_url),fp,
                image_fp=derivative_fp,operation='render')
            assert 'another tenant/date/group' in claim(derivative,ok=False)
            assert sql(f"select count(*) from fixer_forward_media_use_20261006 where fingerprint='{derivative_fp}';") == '0'
            # Rehosted derivative relabeled as original still reserves its known
            # original ancestry, even under a new gym and URL.
            relabeled,*_=seed(fp=derivative_fp)
            assert 'another tenant/date/group' in claim(relabeled,ok=False)
            derivative_sibling=attest(row(tenant,group,url,image=derivative_url),fp,
                image_fp=derivative_fp,operation='render')
            assert claim(derivative_sibling) == 't'
            # Thumbnail bytes are required and participate in global authority.
            poster='https://scratch.example/' + uuid.uuid4().hex
            thumb_fp='md5:' + uuid.uuid4().hex
            poster_pair=attest(row(tenant,group,url,image=derivative_url,thumbnail=poster),fp,
                image_fp=derivative_fp,thumb_fp=thumb_fp,operation='render')
            assert claim(poster_pair) == 't'
            poster_reuse,*_=seed(fp=thumb_fp)
            assert 'another tenant/date/group' in claim(poster_reuse,ok=False)
            # Missing thumbnail evidence and falsely claimed same-object ancestry hold.
            attest(row(tenant,group,url,thumbnail=poster),fp,ok=False)
            attest(row(tenant,group,url,image=derivative_url),fp,image_fp=derivative_fp,ok=False)
            # Independent concurrent dates: exactly one succeeds.
            a,rt,rg,ru,rf=seed()
            b=attest(row(rt,rg,ru,'2026-10-11'),rf)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(race,(a,b)))
            assert sum(x.returncode==0 for x in results)==1,[x.stderr for x in results]
            # Independent concurrent gyms: exactly one succeeds globally.
            global_fp='md5:'+uuid.uuid4().hex
            a,*_=seed(fp=global_fp)
            b,*_=seed(fp=global_fp)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(race,(a,b)))
            assert sum(x.returncode==0 for x in results)==1,[x.stderr for x in results]
            assert sql(f"select count(*) from fixer_forward_media_use_20261006 where fingerprint='{global_fp}';")=='1'
            # Attester holds an uncommitted new graph edge original -> X.
            # X's claimant must wait, refresh its snapshot, discover the occupied
            # original and deny; it cannot escape by claiming before edge commit.
            origin,ot,og,ou,ofp=seed()
            assert claim(origin)=='t'
            target,tt,tg,tu,tfp=seed()
            edge_row=row(ot,og,ou,image='https://scratch.example/'+uuid.uuid4().hex)
            prepare(edge_row,ofp,image_fp=tfp,operation='render')
            edge_id=str(uuid.uuid4())
            edge_revision=sql(f"select fixer_forward_media_attestation_request_20261006('{edge_row[0]}')->>'revision';")
            edge_sql=("set application_name='forward_graph_race'; begin; "
                "set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{edge_row[0]}','{edge_revision}','{edge_id}','{ofp}',10,'{tfp}',10,null,null,'render','race');"
                "select pg_sleep(1.5); commit;")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                writing=pool.submit(sql,edge_sql)
                deadline=time.monotonic()+5
                while sql("select count(*) from pg_stat_activity where application_name='forward_graph_race' "
                          "and wait_event='PgSleep';")!='1':
                    assert time.monotonic()<deadline, 'attester did not reach held graph edge'
                    time.sleep(0.02)
                denial=claim(target,ok=False)
                assert 'another tenant/date/group' in denial, denial
                writing.result(timeout=5)
            assert sql(f"select count(*) from fixer_forward_media_lineage_20261006 where evidence_id='{edge_id}';")=='1'
            assert sql(f"select count(*) from fixer_forward_media_use_20261006 where fingerprint='{tfp}';")=='0'
            # Fixed transaction snapshots cannot bypass post-lock graph refresh.
            assert 'read committed isolation' in sql("begin isolation level repeatable read; set role service_role; "
                f"select fixer_claim_forward_media_20261006('{target[0]}','{target[1]}','{target[2]}','revision'); commit;",ok=False)
            # Outgoing revision is locked even if a concurrent writer re-attests.
            stale, _tenant, _group, _url, stale_fp = seed()
            old_revision = sql(f"select fixer_forward_media_attestation_request_20261006('{stale[0]}')->>'revision';")
            sql(f"update content_calendar set image_url='https://scratch.example/revised' where id='{stale[0]}';")
            new_evidence = attest(stale[:2], stale_fp, image_fp='md5:'+uuid.uuid4().hex, operation='render')
            assert 'outgoing media revision changed' in claim(new_evidence, ok=False, expected_revision=old_revision)
            assert sql(f"select count(*) from fixer_forward_media_claim_receipt_20261006 where calendar_row_id='{stale[0]}';") == '0'
            # GBP publisher binds its gym-local attempt day under its token;
            # the authority never substitutes UTC or the old content date.
            gbp_pair, _, _, _, gbp_fp = seed(day='2026-09-01')
            sql(f"update content_calendar set account='googlebusiness',publish_reservation_day=null where id='{gbp_pair[0]}';")
            gbp_pair = attest(gbp_pair[:2], gbp_fp)
            assert 'reservation day unavailable' in claim(gbp_pair, ok=False)
            sql(f"update content_calendar set publish_reservation_day='2026-08-31' "
                f"where id='{gbp_pair[0]}' and publish_claim_token='{gbp_pair[1]}';")
            assert claim(gbp_pair) == 't'
            assert sql(f"select reservation_day from fixer_forward_media_claim_receipt_20261006 where calendar_row_id='{gbp_pair[0]}';") == '2026-08-31'
            assert sql(f"select post_date from fixer_forward_media_claim_receipt_20261006 where calendar_row_id='{gbp_pair[0]}';") == '2026-09-01'
            # An append of historical quarantine races against an already
            # attested/replayed claim. The shared graph lock must wait and then
            # recheck clearance, including permanent receipt replays.
            history_target,ht,hg,hu,hfp=seed()
            assert claim(history_target)=='t'
            hold=row('historical_owner','historical_group','https://scratch.example/'+uuid.uuid4().hex)
            hold_asset,_=prepare(hold,hfp,clearance=False)
            hold_url=sql(f"select source_media_url from content_calendar where id='{hold[0]}';")
            hold_sql=("set application_name='history_clearance_race'; begin; " +
                raw_clearance(hold,hfp,hold_asset,'historical_owner',hold_url,'hold_used') +
                "select pg_sleep(1.5); commit;")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                writing=pool.submit(sql,hold_sql)
                deadline=time.monotonic()+5
                while sql("select count(*) from pg_stat_activity where application_name='history_clearance_race' "
                          "and wait_event='PgSleep';")!='1':
                    assert time.monotonic()<deadline,'history insert did not reach graph lock'
                    time.sleep(0.02)
                assert 'historical eligibility clearance unavailable' in claim(history_target,ok=False)
                writing.result(timeout=5)
            assert sql(f"select count(*) from fixer_forward_media_claim_receipt_20261006 where claim_token='{history_target[1]}';")=='1'
            # Deleting content never frees consumed bytes or receipts.
            sql(f"delete from content_calendar where id='{first[0]}';")
            assert sql(f"select count(*) from fixer_forward_media_use_20261006 where fingerprint='{fp}';")=='1'
            assert 'immutable' in sql('delete from fixer_forward_media_use_20261006;',ok=False)
            assert 'immutable' in sql('truncate fixer_forward_media_use_20261006;',ok=False)
            # Already-delivered first claims are never adopted into forward history.
            sent=attest(row(tenant,group,url),fp)
            sql(f"update content_calendar set late_post_id='provider-id' where id='{sent[0]}';")
            assert 'unsent' in claim(sent,ok=False)
            sql('alter table fixer_forward_media_use_20261006 rename to temporarily_unavailable;')
            assert 'does not exist' in claim(sibling,ok=False)
            # Service-role binding RPC: takes only the row ID and binds exactly
            # one persisted owner manifest, never caller-supplied claims.
            def bind(rid, ok=True):
                return sql(f"set role service_role; select public.fixer_bind_forward_media_manifest_20261006('{rid}');", ok)
            def bindable_row(tenant, asset, url, image=None, thumbnail=None, digest='null'):
                rid = str(uuid.uuid4())
                image = image or url
                thumb = "null" if thumbnail is None else "'" + thumbnail + "'"
                sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                    "image_url,source_media_url,thumbnail_url,visual_group_key,source_media_asset_id,render_manifest_digest)"
                    f" values('{rid}','{tenant}','2026-10-10','draft','active',"
                    f"'{image}','{url}',{thumb},'vg_{uuid.uuid4().hex}','{asset}',{digest});")
                return rid
            def owner_persist(tenant, asset, url, fp, image, thumbnail=None, clearance=True, registry=True):
                if registry:
                    sql("insert into fixer_forward_media_original_registry_20261006 "
                        "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref) "
                        f"values('{tenant}','{asset}','{url}','{fp}',10,'owner-verified-original');")
                if clearance:
                    sql("insert into fixer_forward_media_history_clearance_20261006 "
                        "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref) "
                        f"values('{tenant}','{asset}','{url}','{fp}',10,'owner-verified-original','cleared_unused','independent-fleet-history-audit');")
                thumb = "null,null,null" if thumbnail is None else f"'{thumbnail}','md5:{'b'*32}',30"
                digest = 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex
                sql("insert into fixer_forward_media_render_manifest_20261006 "
                    "(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,"
                    "thumbnail_url,thumbnail_fingerprint,thumbnail_length,operation,render_evidence_ref) "
                    f"values('{digest}','{tenant}','{asset}','{image}','{fp}',10,{thumb},'same_object','owner-verified-render');")
                return digest
            bind_tenant='bind_gym_'+uuid.uuid4().hex
            bind_fp='md5:'+uuid.uuid4().hex
            bind_asset='original_'+uuid.uuid4().hex
            bind_url='https://scratch.example/bind_'+uuid.uuid4().hex
            # Successful bind from a null digest, with unchanged approval/status.
            brid=bindable_row(bind_tenant,bind_asset,bind_url)
            bdigest=owner_persist(bind_tenant,bind_asset,bind_url,bind_fp,bind_url)
            inserted_rid=str(uuid.uuid4())
            assert 'validated binder' in sql("set role service_role; insert into content_calendar "
                f"(id,render_manifest_digest) values('{inserted_rid}','{bdigest}');",ok=False)
            assert sql(f"select count(*) from content_calendar where id='{inserted_rid}';")=='0'
            sql("set role service_role; insert into content_calendar "
                f"(id,render_manifest_digest) values('{inserted_rid}',null);")
            assert sql(f"select render_manifest_digest is null from content_calendar where id='{inserted_rid}';")=='t'
            before=sql(f"select status||'|'||variant_status||'|'||coalesce(publish_claim_token::text,'') from content_calendar where id='{brid}';")
            assert sql("select prosecdef from pg_proc where oid="
                "'public.fixer_guard_forward_media_digest_20261006()'::regprocedure;")=='f'
            assert sql("select prosecdef from pg_proc where oid="
                "'public.fixer_bind_forward_media_manifest_20261006(uuid)'::regprocedure;")=='t'
            assert 'validated binder' in sql(f"set role service_role; update content_calendar "
                f"set render_manifest_digest='{bdigest}' where id='{brid}';",ok=False)
            assert sql(f"select render_manifest_digest is null from content_calendar where id='{brid}';")=='t'
            assert bind(brid)=='t'
            assert sql(f"select render_manifest_digest from content_calendar where id='{brid}';")==bdigest
            assert sql(f"select status||'|'||variant_status||'|'||coalesce(publish_claim_token::text,'') from content_calendar where id='{brid}';")==before
            # Idempotent replay with the already-matching persisted digest.
            assert bind(brid)=='t'
            assert sql(f"select render_manifest_digest from content_calendar where id='{brid}';")==bdigest
            for direct_digest in ('null', "'sha256:"+'0'*64+"'"):
                assert 'validated binder' in sql(f"set role service_role; update content_calendar "
                    f"set render_manifest_digest={direct_digest} where id='{brid}';",ok=False)
                assert sql(f"select render_manifest_digest from content_calendar where id='{brid}';")==bdigest
            # An unchanged digest and ordinary table updates remain permitted.
            sql(f"set role service_role; update content_calendar set render_manifest_digest="
                f"render_manifest_digest, status=status where id='{brid}';")
            # Ambiguous manifests for the same binding fail closed.
            amb_tenant='bind_gym_'+uuid.uuid4().hex
            amb_fp='md5:'+uuid.uuid4().hex
            amb_asset='original_'+uuid.uuid4().hex
            amb_url='https://scratch.example/amb_'+uuid.uuid4().hex
            amb_rid=bindable_row(amb_tenant,amb_asset,amb_url)
            owner_persist(amb_tenant,amb_asset,amb_url,amb_fp,amb_url)
            owner_persist(amb_tenant,amb_asset,amb_url,amb_fp,amb_url,clearance=False,registry=False)  # distinct digest, same binding
            assert 'exactly one matching immutable render manifest' in bind(amb_rid,ok=False)
            assert sql(f"select render_manifest_digest is null from content_calendar where id='{amb_rid}';")=='t'
            # Stale/wrong source URL between row and registry fails closed.
            stale_tenant='bind_gym_'+uuid.uuid4().hex
            stale_fp='md5:'+uuid.uuid4().hex
            stale_asset='original_'+uuid.uuid4().hex
            stale_url='https://scratch.example/stale_'+uuid.uuid4().hex
            stale_rid=bindable_row(stale_tenant,stale_asset,stale_url)
            owner_persist(stale_tenant,stale_asset,'https://scratch.example/different_'+uuid.uuid4().hex,stale_fp,stale_url)
            assert 'original registry binding unavailable' in bind(stale_rid,ok=False)
            # Wrong image URL on the row matches no manifest and fails closed.
            wrong_tenant='bind_gym_'+uuid.uuid4().hex
            wrong_fp='md5:'+uuid.uuid4().hex
            wrong_asset='original_'+uuid.uuid4().hex
            wrong_url='https://scratch.example/wrong_'+uuid.uuid4().hex
            wrong_rid=bindable_row(wrong_tenant,wrong_asset,wrong_url,image='https://scratch.example/other_'+uuid.uuid4().hex)
            owner_persist(wrong_tenant,wrong_asset,wrong_url,wrong_fp,wrong_url)
            assert 'exactly one matching immutable render manifest' in bind(wrong_rid,ok=False)
            # Missing historical clearance refuses even with registry+manifest.
            nc_tenant='bind_gym_'+uuid.uuid4().hex
            nc_fp='md5:'+uuid.uuid4().hex
            nc_asset='original_'+uuid.uuid4().hex
            nc_url='https://scratch.example/nc_'+uuid.uuid4().hex
            nc_rid=bindable_row(nc_tenant,nc_asset,nc_url)
            owner_persist(nc_tenant,nc_asset,nc_url,nc_fp,nc_url,clearance=False)
            assert 'historical eligibility clearance unavailable or held' in bind(nc_rid,ok=False)
            # An already conflicting persisted digest is refused, not rebound.
            conf_rid=bindable_row(bind_tenant,bind_asset,bind_url,digest="'sha256:"+'0'*64+"'")
            assert 'conflicting render manifest digest' in bind(conf_rid,ok=False)
            assert sql(f"select render_manifest_digest from content_calendar where id='{conf_rid}';")=='sha256:'+'0'*64
            # Only service_role may execute; denied roles fail closed.
            for denied in ('anon','authenticated','fixer_forward_media_attester_20261006'):
                assert 'permission denied' in sql(f"set role {denied}; select public.fixer_bind_forward_media_manifest_20261006('{brid}');",ok=False)
            # A sent row is never bound.
            sql(f"update content_calendar set late_post_id='provider-id' where id='{conf_rid}';")
            assert 'unsent active row' in bind(conf_rid,ok=False)
            # An owned send claim or terminal status cannot be revised by a
            # delayed owner preparation, even when its media still matches.
            sql(f"update content_calendar set publish_claim_token='{uuid.uuid4()}' where id='{brid}';")
            assert 'unsent active row' in bind(brid,ok=False)
            sql(f"update content_calendar set publish_claim_token=null,status='published' where id='{brid}';")
            assert 'unsent active row' in bind(brid,ok=False)
            print('PASS: self-contained draft; global cross-gym/cross-date concurrency one-winner; '
                  'same-group idempotency; frozen-receipt retry after benign later ancestry; later consumed/negative ancestry retry hold; catch-up reservation; immutable complete source/image/thumbnail '
                  'evidence; narrow attester auth; forged/missing/stale evidence hold; deletion permanence; '
                  'derivative source reuse denial; atomic rollback; default OFF; sent-row/outage hold; '
                  'attester-vs-claim graph race denial and isolation hold; authoritative original registry and '
                  'versioned render manifest mismatch denial; narrow tenant-scoped discovery and provenance lookup; '
                  'missing/forged/stale historical clearance hold; tenant/asset/URL isolation; fleet quarantine non-bypass; '
                  'service-only exact-manifest bind, direct non-NULL digest INSERT denial and NULL INSERT success, '
                  'direct digest set/replace/clear denial with table UPDATE, '
                  'ambiguity and claimed/sent-row refusal')
        finally:
            subprocess.run(['pg_ctl','-D',str(data),'-m','immediate','-w','stop'],
                           capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
