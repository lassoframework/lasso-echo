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

        def claim(pair, ok=True):
            rid, token, evidence = pair
            return sql("set role service_role; select public.fixer_claim_forward_media_20261006"
                       f"('{rid}','{token}','{evidence}');", ok)

        def row(tenant, group, url, day='2026-10-10', reservation='2026-10-10', image=None, thumbnail=None):
            rid, token = str(uuid.uuid4()), str(uuid.uuid4())
            image = image or url
            thumb = "null" if thumbnail is None else "'" + thumbnail + "'"
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "image_url,source_media_url,thumbnail_url,visual_group_key,publish_claim_token,publish_reservation_day)"
                f" values('{rid}','{tenant}','{day}','publishing','active',"
                f"'{image}','{url}',{thumb},'{group}','{token}','{reservation}');")
            return rid, token

        def attest(pair, fp, image_fp=None, thumb_fp=None, operation='same_object', ok=True, revision=None):
            rid, token = pair
            evidence = str(uuid.uuid4())
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
            return subprocess.run(base, input="begin; set role service_role; select "
                f"fixer_claim_forward_media_20261006('{rid}','{token}','{evidence}');"
                "select pg_sleep(0.5); commit;", text=True,capture_output=True,timeout=15)

        try:
            sql("create role anon; create role authenticated; create role service_role;"
                "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                "status text,variant_status text,published_at timestamptz,publish_claim_token uuid,"
                "publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,"
                "media_not_ready_reason text);")
            # Apply only this self-contained draft. NO #306/#307 dependency.
            sql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
            first, tenant, group, url, fp = seed()
            sql(f"update fixer_forward_media_claim_gate_20261006 set enabled=false where tenant_id='{tenant}';")
            assert 'OFF' in claim(first,ok=False)
            sql(f"update fixer_forward_media_claim_gate_20261006 set enabled=true where tenant_id='{tenant}';")
            assert claim(first) == 't'
            assert claim(first) == 't'
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
                f"('{first[0]}','{first[1]}','{first[2]}');",ok=False)
            assert 'permission denied' in sql("set role service_role; update fixer_forward_media_claim_gate_20261006 set enabled=true;",ok=False)
            assert 'permission denied' in sql("set role fixer_forward_media_attester_20261006; delete from fixer_forward_media_use_20261006;",ok=False)
            missing = (*row(tenant,group,url),str(uuid.uuid4()))
            assert 'owner attested' in claim(missing,ok=False)
            forged = (*row(tenant,group,url),first[2])
            assert 'owner attested' in claim(forged,ok=False)
            assert 'ownership' in claim((first[0],str(uuid.uuid4()),first[2]),ok=False)
            # Evidence is bound to persisted revision and exact outgoing objects.
            changed = attest(row(tenant,group,url),fp)
            sql(f"update content_calendar set image_url='https://scratch.example/changed' where id='{changed[0]}';")
            assert 'owner attested' in claim(changed,ok=False)
            attest(row(tenant,group,url),fp,ok=False,revision='stale-revision')
            unknown=row(tenant,group,url)
            sql(f"update content_calendar set source_media_url=null where id='{unknown[0]}';")
            assert 'identity unavailable' in sql(f"select fixer_forward_media_attestation_request_20261006('{unknown[0]}');",ok=False)
            # Same immutable URL cannot be rebound to different fetched bytes.
            assert 'changed bytes' in sql("set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
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
                f"select fixer_claim_forward_media_20261006('{target[0]}','{target[1]}','{target[2]}'); commit;",ok=False)
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
            print('PASS: self-contained draft; global cross-gym/cross-date concurrency one-winner; '
                  'same-group idempotency; catch-up reservation; immutable complete source/image/thumbnail '
                  'evidence; narrow attester auth; forged/missing/stale evidence hold; deletion permanence; '
                  'derivative source reuse denial; atomic rollback; default OFF; sent-row/outage hold; '
                  'attester-vs-claim graph race denial and isolation hold')
        finally:
            subprocess.run(['pg_ctl','-D',str(data),'-m','immediate','-w','stop'],
                           capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
