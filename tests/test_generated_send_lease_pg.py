"""Disposable PG17 lease contract; synthetic prerequisite rows, no providers.

Uses the real canonical migration and real generated publish-readback function.
Prerequisite fixture tables model the existing claim/reservation schema; this
is not proof of the whole assembled claim/approval stack or runtime delivery.
"""
import json
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import time
import uuid

from test_generated_source_palette_authority_pg import PG, ROOT, source_op, approval_op, palette_op


def main():
    import psycopg
    with tempfile.TemporaryDirectory(prefix='generated_lease_pg_', dir='/tmp') as tmp:
        root = Path(tmp)
        (root/'sock').mkdir()
        port = random.randint(41000, 59000)
        subprocess.run([str(PG/'initdb'), '-D', str(root/'data'), '-U', 'postgres', '--no-sync'], check=True, capture_output=True, timeout=60)
        subprocess.run([str(PG/'pg_ctl'), '-D', str(root/'data'), '-l', str(root/'pg.log'), '-o', f"-k {root/'sock'} -p {port} -c listen_addresses=''", '-w', 'start'], check=True, capture_output=True, timeout=60)
        connections = []
        try:
            def connect(role='postgres'):
                c = psycopg.connect(f"host={root/'sock'} port={port} dbname=postgres user={role}", autocommit=True)
                c.execute("set statement_timeout='8s'")
                connections.append(c)
                return c
            admin = connect()
            admin.execute('create role anon login; create role authenticated login; create role service_role login bypassrls')
            admin.execute('alter default privileges in schema public grant all on tables to public,anon,authenticated,service_role')
            admin.execute('alter default privileges in schema public grant all on sequences to public,anon,authenticated,service_role')
            admin.execute('alter default privileges in schema public grant all on functions to public,anon,authenticated,service_role')
            admin.execute((ROOT/'migrations/DRAFT_generated_source_palette_authority_20261007.sql').read_text())
            # Synthetic fixtures ONLY, matching the exact production draft's
            # readback/claim fields; no actual provider or human receipts exist.
            admin.execute('''create table public.content_calendar (
              id uuid primary key, gym_id text, publish_claim_token uuid, status text,
              published_at timestamptz, late_post_id text, account text, format text,
              source_media_asset_id text, post_date date, logical_post_id uuid,
              visual_group_key text, image_url text, source_media_url text,
              thumbnail_url text, render_manifest_digest text, publish_reservation_day date, caption text)''')
            owner = (ROOT/'migrations/DRAFT_fixer_generated_owner_20261007.sql').read_text()
            schema = owner[owner.index('create table public.fixer_generated_reservation_20261007'):owner.index('-- Exact historical')]
            admin.execute(schema)
            claim = (ROOT/'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text()
            schema = claim[claim.index('create table public.fixer_forward_media_claim_receipt_20261006'):claim.index('-- Both row mutation')]
            admin.execute('create table public.fixer_forward_media_lineage_20261006(evidence_id uuid primary key)')
            admin.execute(schema)
            fn = owner[owner.index('create function public.fixer_generated_publish_readback_20261007'):owner.index('revoke all on function public.fixer_generated_publish_readback_20261007')]
            admin.execute(fn)
            admin.execute((ROOT/'migrations/DRAFT_generated_send_lease_20261007.sql').read_text())
            admin.execute('create role reconciler_user login; grant generated_send_reconciler_20261007 to reconciler_user')
            service, reconciler = connect('service_role'), connect('reconciler_user')

            def rpc(c, name, *args):
                return c.execute('select public.'+name+'_20261007('+','.join(['%s']*len(args))+')', args).fetchone()[0]
            def denied(fn, phrase=None, code=None):
                try: fn()
                except psycopg.Error as e:
                    if phrase: assert phrase in str(e), str(e)
                    if code: assert e.sqlstate == code, (e.sqlstate, str(e))
                    return
                raise AssertionError('unsafe operation accepted')
            def evidence(label):
                return json.dumps(dict(actor='synthetic:test-fixture', receipt_ref='synthetic:'+label))
            def seed(tenant, pins=True):
                assert rpc(service,'generated_authority_write',tenant,0,json.dumps([source_op('src'),approval_op('src'),palette_op()])) == 1
                row, claim, job, logical, eid = [uuid.uuid4() for _ in range(5)]
                identity = dict(tenant_id=tenant, epoch=1, source_id='src', source_revision=1, palette_key='brand', palette_revision=1)
                candidate = dict(gym_id=tenant, local_date='2026-10-08', logical_post_id=str(logical), original_url='https://synthetic.test/original', copy_digest='copy', palette_revision='palette',palette_digest='palette-digest')
                if pins: candidate['authority_pins'] = identity
                manifest = dict(manifest_digest='manifest',tenant_id=tenant,source_asset_id='generated-astra:'+str(job),image_url=candidate['original_url'])
                admin.execute('insert into public.content_calendar values(%s,%s,%s,%s,null,null,%s,%s,%s,%s,%s,%s,%s,%s,null,%s,%s,%s)',
                    (row,tenant,claim,'publishing','instagram','feed','generated-astra:'+str(job),'2026-10-08',logical,'group',candidate['original_url'],candidate['original_url'],'manifest','2026-10-08','exact source text'))
                admin.execute('insert into public.fixer_generated_reservation_20261007(job_id,calendar_row_id,group_key,candidate_json,manifest_json,receipt_ref,history_epoch,approved_source_revision) values(%s,%s,%s,%s,%s,%s,%s,%s)',
                    (job,row,'group',json.dumps(candidate),json.dumps(manifest),'synthetic:reservation:'+tenant,'{}','client-source:sha256:'+'a'*64))
                admin.execute('insert into public.fixer_forward_media_lineage_20261006 values(%s)',(eid,))
                admin.execute('insert into public.fixer_forward_media_claim_receipt_20261006(claim_token,calendar_row_id,tenant_id,post_date,reservation_day,group_key,evidence_id,fingerprints,source_url,image_url) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (claim,row,tenant,'2026-10-08','2026-10-08','group',eid,['md5:'+'a'*32],candidate['original_url'],candidate['original_url']))
                return dict(tenant=tenant,row=row,claim=claim,job=job,pins=identity,token=uuid.uuid4())
            def acquire(s,c=service):
                return rpc(c,'generated_send_acquire',s['token'],s['tenant'],s['row'],s['claim'],s['job'],json.dumps(s['pins']))
            def request(s,kind='source'):
                return rpc(service,'generated_revoke_request',uuid.uuid4(),s['tenant'],1,kind,'src' if kind=='source' else 'brand',evidence('revoke'))
            def status(s): return rpc(service,'generated_send_status',s['tenant'])
            def outcome(s,value,c=service,reconcile=False):
                return rpc(c,'generated_send_outcome',s['token'],value,evidence(value),reconcile)

            # Required canonical pins and exact row/claim identity; no bootstrap
            # from the legacy local-source hash in an old reservation.
            denied(lambda: acquire(seed('missing-pins',False)), 'pins missing')
            s=seed('identity')
            for field,value in [('tenant','other'),('row',uuid.uuid4()),('claim',uuid.uuid4()),('job',uuid.uuid4())]:
                denied(lambda field=field,value=value: acquire({**s,field:value}))
            for field,value in [('epoch',2),('source_id','other'),('source_revision',2),('palette_revision',2),('epoch',True)]:
                denied(lambda field=field,value=value: acquire({**s,'pins':{**s['pins'],field:value}}))
            assert acquire(s)['state']=='reserved'
            assert acquire(s)['authorize_send'] is False
            denied(lambda: acquire({**s,'job':uuid.uuid4()}),'immutable binding')
            denied(lambda: acquire({**s,'token':uuid.uuid4()}))
            denied(lambda: rpc(service,'generated_authority_write',s['tenant'],1,json.dumps([{'op':'source_tombstone','source_id':'src'}])),'outstanding')
            denied(lambda: admin.execute('update public.content_calendar set caption=%s where id=%s',('changed',s['row'])),'freezes')
            denied(lambda: admin.execute('delete from public.content_calendar where id=%s',(s['row'],)),'freezes')
            denied(lambda: admin.execute('truncate public.content_calendar'),'freezes')
            assert rpc(service,'generated_send_begin',s['token'])['authorize_send'] is True
            assert rpc(service,'generated_send_begin',s['token'])['authorize_send'] is False
            assert outcome(s,'sent')['state']=='sent'
            assert outcome(s,'sent')['authorize_send'] is False
            denied(lambda: outcome(s,'not_sent'),'immutable')
            denied(lambda: acquire({**s,'token':uuid.uuid4()}),'already has a sent')

            # Revocation is pending immediately; no new or unstarted send.
            s=seed('reserved-revoke'); acquire(s)
            q=request(s)
            assert q['state']=='pending' and q['effective_epoch'] is None
            assert status(s)['revocations'][0]['state']=='pending'
            denied(lambda: rpc(service,'generated_send_begin',s['token']),'pending revocation')
            denied(lambda: acquire({**s,'token':uuid.uuid4()}),'revocation pending')
            denied(lambda: rpc(service,'generated_authority_write',s['tenant'],1,json.dumps([source_op('edit')])),'outstanding')
            assert rpc(service,'generated_revoke_drain',s['tenant'])['state']=='pending'
            assert rpc(service,'generated_authority_snapshot',s['tenant'])['sources'][0]['status']=='approved'
            assert outcome(s,'not_sent')['state']=='not_sent'
            assert status(s)['revocations'][0]['state']=='effective'
            assert rpc(service,'generated_authority_snapshot',s['tenant'])['sources'][0]['status']=='revoked'

            # Unknown transport outcome never expires or auto retries. Neither
            # publisher status report nor revocation can manufacture terminality.
            for kind in ('source','palette'):
                s=seed('unknown-'+kind); acquire(s); rpc(service,'generated_send_begin',s['token'])
                outcome(s,'unknown'); request(s,kind)
                admin.execute("update public.generated_send_lease_20261007 set created_at=clock_timestamp()-interval '2 years',updated_at=clock_timestamp()-interval '2 years' where attempt_token=%s",(s['token'],))
                assert rpc(service,'generated_send_begin',s['token'])['authorize_send'] is False
                assert acquire(s)['state']=='unknown' and acquire(s)['authorize_send'] is False
                denied(lambda: outcome(s,'not_sent'),'reconciliation')
                denied(lambda: outcome(s,'not_sent',reconcile=True),'reconciliation')
                assert rpc(service,'generated_revoke_drain',s['tenant'])['state']=='pending'
                assert outcome(s,'not_sent',reconciler,True)['state']=='not_sent'
                assert status(s)['revocations'][0]['state']=='effective'

            # Inflight send can finish after a request, but revocation cannot
            # be called effective before that terminal observation is recorded.
            s=seed('inflight-sent'); acquire(s); rpc(service,'generated_send_begin',s['token']); request(s)
            assert status(s)['revocations'][0]['state']=='pending'
            assert outcome(s,'sent')['state']=='sent'
            assert status(s)['revocations'][0]['state']=='effective'

            # Old RR snapshots cannot miss a committed lease with unchanged
            # canonical epoch and bypass its fence. Reject mutating RPCs in RR.
            service.execute('begin isolation level repeatable read')
            denied(lambda: rpc(service,'generated_authority_write','identity',1,json.dumps([source_op('rr-bypass')])),'requires read committed','25000')
            service.execute('rollback')

            # Revoke without outstanding attempt: request remains explicit,
            # drain makes it effective; same request is idempotent and immutable.
            s=seed('immediate'); rid=uuid.uuid4(); ev=evidence('explicit-revoke')
            assert rpc(service,'generated_revoke_request',rid,s['tenant'],1,'source','src',ev)['state']=='pending'
            assert rpc(service,'generated_revoke_drain',s['tenant'])['state']=='effective'
            assert rpc(service,'generated_revoke_request',rid,s['tenant'],1,'source','src',ev)['state']=='effective'
            denied(lambda: rpc(service,'generated_revoke_request',rid,s['tenant'],1,'palette','brand',ev),'immutable identity')
            denied(lambda: acquire(s))

            # Serialized overlap: request waits for committed lease acquisition,
            # then becomes pending rather than reporting effective revocation.
            s=seed('race'); contender=connect('service_role'); service.execute('begin')
            acquire(s)
            result={}
            def run_request():
                try: result['value']=rpc(contender,'generated_revoke_request',uuid.uuid4(),s['tenant'],1,'source','src',evidence('race'))
                except Exception as exc: result['error']=exc
            thread=threading.Thread(target=run_request,daemon=True); thread.start()
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                wait=admin.execute('select wait_event from pg_stat_activity where pid=%s',(contender.info.backend_pid,)).fetchone()[0]
                if wait=='advisory': break
                time.sleep(.01)
            else: raise AssertionError('contender did not overlap tenant lock')
            service.execute('commit'); thread.join(5)
            assert not thread.is_alive() and result['value']['state']=='pending',result
            outcome(s,'not_sent')

            # Reverse ordering: uncommitted revocation request wins the fence;
            # contender cannot acquire after the request commits.
            s=seed('race-revoke-first'); service.execute('begin'); request(s)
            result={}
            def run_acquire():
                try: result['value']=acquire(s,contender)
                except Exception as exc: result['error']=exc
            thread=threading.Thread(target=run_acquire,daemon=True); thread.start()
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                wait=admin.execute('select wait_event from pg_stat_activity where pid=%s',(contender.info.backend_pid,)).fetchone()[0]
                if wait=='advisory': break
                time.sleep(.01)
            else: raise AssertionError('lease contender did not overlap tenant lock')
            service.execute('commit'); thread.join(5)
            assert not thread.is_alive() and 'value' not in result and result['error'].sqlstate=='55000',result
            assert rpc(service,'generated_revoke_drain',s['tenant'])['state']=='effective'

            # Function-only ACL and immutable audit, even under BYPASSRLS and
            # realistic inherited default privileges. Raw writer cannot bypass.
            for role in ('service_role','anon','authenticated','reconciler_user'):
                c=connect(role)
                for table in ('generated_send_lease_20261007','generated_revocation_request_20261007','generated_send_audit_20261007'):
                    for statement in ('select * from ','delete from ','truncate '):
                        denied(lambda statement=statement,table=table: c.execute(statement+'public.'+table),'permission denied')
                denied(lambda: rpc(c,'generated_authority_pre_lease_write','identity',1,json.dumps([source_op('bypass')])),'permission denied')
            denied(lambda: admin.execute("update public.generated_send_audit_20261007 set event='forged'"),'append-only')
            denied(lambda: admin.execute('truncate public.generated_send_lease_20261007'),'append-only')
            print('PASS: durable generated lease exact identity, one begin, pending revocation, unknown quarantine, explicit reconciliation, calendar fence, ACLs and two overlapping races')
        finally:
            for c in reversed(connections): c.close()
            subprocess.run([str(PG/'pg_ctl'),'-D',str(root/'data'),'-m','fast','stop'],capture_output=True,timeout=60)


if __name__=='__main__':
    main()
