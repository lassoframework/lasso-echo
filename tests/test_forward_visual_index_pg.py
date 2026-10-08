"""Standalone disposable PG acceptance for DRAFT_fixer_forward_visual_index_20261008.

Run `python3 tests/test_forward_visual_index_pg.py`. Uses only stdlib and
existing initdb/pg_ctl/psql against a real disposable PostgreSQL cluster.
No DSN, network, production connection, installs or persisted cluster.
Requires PostgreSQL 17 binaries and an existing psycopg/Pillow runtime.
Skips cleanly only when the binaries are absent. Does not install dependencies.
"""
import concurrent.futures
import json
import os
import sys
from types import SimpleNamespace
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.forward_media_visual_index import row_revision_from

# Explicit threshold vectors plus deterministic independent codewords; every
# allocated value remains more than 30 bits from every previous fixture.
_USED_PHASH = [0, 0x3f, (1 << 28) - 1, 0x7FFFFFFF00000000]


def _fresh_phash():
    # First-order Reed-Muller codewords have pairwise Hamming distance 32.
    # Deterministic bounded allocation avoids an exponentially slow random
    # rejection loop as the real acceptance matrix grows.
    for mask in range(1,64):
        for flip in (0,1):
            unsigned=sum(((bin(i & mask).count('1') % 2) ^ flip) << i for i in range(64))
            if all(((unsigned ^ (used & ((1<<64)-1))).bit_count()) > 30 for used in _USED_PHASH):
                signed=unsigned-(1<<64) if unsigned >= (1<<63) else unsigned
                _USED_PHASH.append(signed)
                return signed
    raise AssertionError('independent pHash fixture pool exhausted')


def main():
    for name in ('initdb', 'pg_ctl', 'psql'):
        if not shutil.which(name):
            print(f'SKIP: existing {name} unavailable; disposable PostgreSQL not provisioned')
            return
    with tempfile.TemporaryDirectory(prefix='fixer_visual_index_pg_', dir='/tmp') as tmp:
        work = Path(tmp)
        sock = work / 'sock'
        sock.mkdir()
        data = work / 'data'
        subprocess.run(['initdb', '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(['pg_ctl', '-D', str(data), '-l', str(work / 'pg.log'),
                        '-o', f"-k {sock} -p 55469 -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        base = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(sock),
                '-p', '55469', '-U', 'postgres', '-d', 'postgres']

        def sql(s, ok=True):
            result = subprocess.run(base, input=s, text=True, capture_output=True, timeout=30)
            if ok and result.returncode:
                raise AssertionError(result.stderr)
            if not ok:
                assert result.returncode != 0, 'unexpected SQL success'
            return result.stdout.strip() if ok else result.stderr

        def row(tenant, group, url, day='2026-10-10', reservation='2026-10-10', image=None, thumbnail=None):
            rid, token = str(uuid.uuid4()), str(uuid.uuid4())
            image = image or url
            thumb = "null" if thumbnail is None else "'" + thumbnail + "'"
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "image_url,source_media_url,thumbnail_url,visual_group_key,publish_claim_token,publish_reservation_day,source_media_asset_id,render_manifest_digest,account,format)"
                f" values('{rid}','{tenant}','{day}','publishing','active',"
                f"'{image}','{url}',{thumb},'{group}','{token}','{reservation}','unprepared_{uuid.uuid4().hex}','sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}','instagram','feed');")
            return rid, token

        def prepare(pair, fp, image_fp=None, thumb_fp=None, operation='same_object', lengths=(10,10,30)):
            rid = pair[0]
            tenant, url, image, thumbnail = sql(f"select gym_id||'|'||source_media_url||'|'||image_url||'|'||coalesce(thumbnail_url,'') from content_calendar where id='{rid}';").split('|')
            existing = sql(f"select source_asset_id from fixer_forward_media_original_registry_20261006 where tenant_id='{tenant}' and source_url='{url}';")
            asset = existing or 'original_' + uuid.uuid4().hex
            if not existing:
                sql("insert into fixer_forward_media_original_registry_20261006 "
                    "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref) "
                    f"values('{tenant}','{asset}','{url}','{fp}',{lengths[0]},'owner-verified-original');")
            sql("insert into fixer_forward_media_history_clearance_20261006 "
                "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref) "
                f"values('{tenant}','{asset}','{url}','{fp}',{lengths[0]},'owner-verified-original','cleared_unused','independent-fleet-history-audit') on conflict do nothing;")
            digest = 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex
            thumb = "null,null,null" if not thumbnail or thumb_fp is None else f"'{thumbnail}','{thumb_fp}',{lengths[2]}"
            sql("insert into fixer_forward_media_render_manifest_20261006 "
                "(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,"
                "thumbnail_url,thumbnail_fingerprint,thumbnail_length,operation,render_evidence_ref) "
                f"values('{digest}','{tenant}','{asset}','{image}','{image_fp or fp}',{lengths[1]},{thumb},'{operation}','owner-verified-render');")
            sql(f"update content_calendar set source_media_asset_id='{asset}',render_manifest_digest='{digest}' where id='{rid}';")

        def attest(pair, fp, image_fp=None, thumb_fp=None, operation='same_object', lengths=(10,10,30)):
            rid, token = pair
            evidence = str(uuid.uuid4())
            prepare(pair, fp, image_fp, thumb_fp, operation, lengths)
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            thumb = "null,null" if thumb_fp is None else f"'{thumb_fp}',{lengths[2]}"
            sql("set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{rid}','{revision}','{evidence}','{fp}',{lengths[0]},'{image_fp or fp}',{lengths[1]},{thumb},"
                f"'{operation}','controlled-runtime-test');")
            return rid, token, evidence

        def revision_bigint(rid):
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            actual = row_revision_from(revision)
            sql_value = sql("select ('x'||substr("
                            f"fixer_forward_media_attestation_request_20261006('{rid}')->>'revision',1,15))::bit(60)::bigint;")
            assert actual == int(sql_value), 'Python/SQL row revision parity drift'
            return actual

        def visual_attest(rid, tenant, sha64, phash, ok=True):
            """Trusted-attester visual evidence for every role of a row."""
            rev = revision_bigint(rid)
            ev, src, img, thumb = sql("select evidence_id||'|'||source_read_receipt||'|'||image_read_receipt||'|'"
                f"||coalesce(thumbnail_read_receipt::text,'') from fixer_forward_media_lineage_20261006 where calendar_row_id='{rid}';").split('|')
            surl, iurl, turl = sql(f"select source_media_url||'|'||image_url||'|'||coalesce(thumbnail_url,'') from content_calendar where id='{rid}';").split('|')
            thumb_receipt = thumb or img
            thumb_url = turl or iurl
            ids = []
            for role, receipt, url in (('original', src, surl), ('delivered', img, iurl), ('thumbnail', thumb_receipt, thumb_url)):
                md5 = sql(f"select substring(fingerprint from 5) from fixer_forward_media_object_read_20261006 where receipt_id='{receipt}';")
                byte_length = sql(f"select byte_length from fixer_forward_media_object_read_20261006 where receipt_id='{receipt}';")
                aid = str(uuid.uuid4())
                ids.append(aid)
                sql("set role fixer_forward_media_attester_20261006; insert into forward_media_visual_attestation"
                    "(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id)"
                    f" values('{aid}','{tenant}','{url}','{role}','{sha64}','{md5}',{byte_length},{phash},{rev},'{ev}','{receipt}');", ok)
            return ids

        def claim_args(rid, token, ids):
            evidence = sql(f"select evidence_id from fixer_forward_media_lineage_20261006 where calendar_row_id='{rid}' order by verified_at desc limit 1;")
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            arr = 'array[' + ','.join(f"'{i}'" for i in ids) + ']::uuid[]'
            return f"'{rid}','{token}','{evidence}','{revision}',{arr}"

        def vclaim(rid, token, tenant, day, ids, ok=True, gym=None):
            return sql("set role service_role; select public.fixer_forward_visual_index_claim_20261008("
                       + claim_args(rid, token, ids) + ");", ok)

        def vseed(sha64=None, phash=None, day='2026-10-10', tenant=None, group=None, url=None, fp=None):
            tenant = tenant or 'gym_' + uuid.uuid4().hex
            group = group or 'vg_' + uuid.uuid4().hex
            url = url or 'https://scratch.example/' + uuid.uuid4().hex
            fp = fp or 'md5:' + uuid.uuid4().hex
            sha64 = sha64 or uuid.uuid4().hex + uuid.uuid4().hex
            phash = _fresh_phash() if phash is None else phash
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{tenant}',true) on conflict do nothing;")
            rid, token, _ = attest(row(tenant, group, url, day), fp)
            ids = visual_attest(rid, tenant, sha64, phash)
            return rid, token, tenant, group, url, fp, sha64, ids

        try:
            sql("create role anon; create role authenticated; create role service_role;"
                "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                "account text,format text,gbp_location_id text,status text,variant_status text,"
                "published_at timestamptz,publish_claim_token uuid,"
                "publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,"
                "media_not_ready_reason text);")
            sql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_visual_index_20261008.sql').read_text())
            sql("grant select,insert,update,delete on public.content_calendar to service_role;")

            assert sql("select current_setting('server_version_num')::integer between 170000 and 179999;") == 't'
            # OFF install preserves the existing base RPC and original caller binding.
            off = vseed()
            off_args = claim_args(off[0], off[1], off[7]).rsplit(',array[', 1)[0]
            assert sql('set role service_role; select fixer_claim_forward_media_20261006(' + off_args + ');') == 't'
            sql("update forward_media_visual_gate_20261008 set enabled=true;")
            assert 'protected claim proof required' in sql('set role service_role; select fixer_claim_forward_media_20261006(' + off_args + ');', ok=False)
            assert 'permission denied' in sql('set role service_role; select fixer_claim_forward_media_internal_20261008(' + off_args + ');', ok=False)
            # Full assembled claim path: base attestation + visual attestations
            # + atomic RPC extension commits in one transaction.
            rid, token, tenant, group, url, fp, sha, ids = vseed(phash=0)
            result = vclaim(rid, token, tenant, '2026-10-10', ids)
            assert result == 't', result
            # Exact-token retry replays with the same attestation set.
            result = vclaim(rid, token, tenant, '2026-10-10', ids)
            assert result == 't', result
            # Authorized sibling: same tenant/date/logical-post, own visual.
            sib_rid, sib_token, _ = attest(row(tenant, group, url), fp)
            sib_ids = visual_attest(sib_rid, tenant, sha, 0)
            assert 't' == vclaim(sib_rid, sib_token, tenant, '2026-10-10', sib_ids)
            # Same visual, another content date: blocked.
            # Same visual (SHA), different bytes/URL, another content date:
            # the base byte claim passes but the visual index blocks.
            other_rid, other_token, _ = attest(row(tenant, group, 'https://scratch.example/' + uuid.uuid4().hex, '2026-10-11'), 'md5:' + uuid.uuid4().hex)
            other_ids = visual_attest(other_rid, tenant, sha, 0)
            assert 'another tenant/date/group' in vclaim(other_rid, other_token, tenant, '2026-10-11', other_ids, ok=False)
            # Same visual, another gym (URL alias): blocked fleet-wide.
            g_rid, g_token, g_tenant, _, _, _, _, g_ids = vseed(sha64=sha, phash=0, day='2026-10-12')
            assert 'another tenant/date/group' in vclaim(g_rid, g_token, g_tenant, '2026-10-12', g_ids, ok=False)

            # pHash v1 thresholds against the committed first claim (phash 0):
            # <=6 blocks, 7-30 holds for review (incident pair at 28), >30 no match.
            b_rid, b_token, b_tenant, _, _, _, _, b_ids = vseed(phash=0x3f)  # distance 6
            assert 'another tenant/date/group' in vclaim(b_rid, b_token, b_tenant, '2026-10-10', b_ids, ok=False)
            h_rid, h_token, h_tenant, _, _, _, _, h_ids = vseed(phash=(1 << 28) - 1)  # distance 28
            assert 'held for review' in vclaim(h_rid, h_token, h_tenant, '2026-10-10', h_ids, ok=False)
            f_rid, f_token, f_tenant, _, _, _, _, f_ids = vseed(phash=0x7FFFFFFF00000000)  # distance 31+
            assert 't' == vclaim(f_rid, f_token, f_tenant, '2026-10-10', f_ids)

            # Real isolated attester connection reads actual PNG bytes using
            # bounded receipt RPC. No elevated role or table read is supplied.
            import hashlib
            import io
            import psycopg
            from PIL import Image
            from agent import forward_media_visual_index as runtime_index
            buf=io.BytesIO();Image.new('RGB',(64,64),(17,80,230)).save(buf,'PNG');actual=buf.getvalue()
            actual_tenant='runtime_'+uuid.uuid4().hex
            actual_url='https://scratch.example/'+uuid.uuid4().hex
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{actual_tenant}',true);")
            actual_image_url=actual_url+'/image'
            actual_thumbnail_url=actual_url+'/thumbnail'
            ar,at,ae=attest(row(actual_tenant,'runtime_group',actual_url, image=actual_image_url, thumbnail=actual_thumbnail_url),
                'md5:'+hashlib.md5(actual).hexdigest(), thumb_fp='md5:'+hashlib.md5(actual).hexdigest(), operation='render',
                lengths=(len(actual),len(actual),len(actual)))
            arev=sql(f"select fixer_forward_media_attestation_request_20261006('{ar}')->>'revision';")
            def attester_connection():
                conn=psycopg.connect(host=str(sock),port=55469,user='postgres',dbname='postgres')
                conn.execute('set role fixer_forward_media_attester_20261006');conn.commit()
                return conn
            from agent import visual_writer_prepare
            saved_host=visual_writer_prepare._own_media_url
            try:
                visual_writer_prepare._own_media_url=lambda url:url in (actual_url,actual_image_url,actual_thumbnail_url)
                negative_count=sql('select count(*) from forward_media_visual_negative;')
                for unreadable_url in (actual_image_url,actual_thumbnail_url):
                    def outage_read(url):
                        if url==unreadable_url: raise OSError('temporary storage outage')
                        return actual
                    try:
                        runtime_index.attest(ar,arev,ae,connection_factory=attester_connection,read_bytes=outage_read)
                        raise AssertionError('unreadable role authorized')
                    except runtime_index.ForwardMediaVerificationHold: pass
                    assert sql('select count(*) from forward_media_visual_negative;')==negative_count
                    assert sql(f"select count(*) from forward_media_visual_attestation where tenant_key='{actual_tenant}';")=='0'
                # Recovery of the same valid bytes must remain possible; no
                # successful sibling hash was persisted as a global negative.
                prepared=runtime_index.attest(ar,arev,ae,connection_factory=attester_connection,read_bytes=lambda _:actual)
                assert len(prepared['attestation_ids'])==3
                assert sql('select count(*) from forward_media_visual_negative;')==negative_count
                assert sql("select count(*) from forward_media_visual_negative where source_sha256='"+hashlib.sha256(actual).hexdigest()+"';")=='0'
                # Corrupt bytes holds and persists an actual negative via the
                # attester RPC; write privilege is not silently discarded.
                try:
                    runtime_index.attest(ar,arev,ae,connection_factory=attester_connection,read_bytes=lambda _:b'undecodable')
                    raise AssertionError('undecodable evidence authorized')
                except runtime_index.ForwardMediaVerificationHold: pass
                assert sql(f"select count(*) from forward_media_visual_negative where tenant_key='{actual_tenant}';")=='1'
            finally:
                visual_writer_prepare._own_media_url=saved_host

            # Actual Python publisher uses service REST only; attester role
            # reads only its bounded receipts RPC and cannot SELECT lineage.
            from agent import forward_media_guard as guard, forward_media_visual_index as index
            probe = vseed()
            probe_ev = sql(f"select evidence_id from fixer_forward_media_lineage_20261006 where calendar_row_id='{probe[0]}';")
            probe_rev = sql(f"select fixer_forward_media_attestation_request_20261006('{probe[0]}')->>'revision';")
            assert sql("set role fixer_forward_media_attester_20261006; select count(*) from fixer_forward_visual_receipts_20261008("
                       f"'{probe[0]}','{probe_rev}','{probe_ev}');") == '1'
            assert 'permission denied' in sql('set role fixer_forward_media_attester_20261006; select * from fixer_forward_media_lineage_20261006;', ok=False)
            assert sql("set role fixer_forward_media_attester_20261006; select count(*) from fixer_forward_visual_receipts_20261008("
                       f"'{rid}','{probe_rev}','{probe_ev}');", ok=False)
            saved_connect, saved_flag = index._connect, os.environ.get('AGENT_FORWARD_MEDIA_VISUAL_INDEX')
            saved_base_flag = os.environ.get('AGENT_FORWARD_MEDIA_GUARD')
            calls, providers = [], []
            def http_post(url, **kwargs):
                calls.append(url)
                args = kwargs['json']
                keys = ('p_calendar_row_id',) if url.endswith('fixer_forward_media_attestation_request_20261006') else ('p_calendar_row_id','p_claim_token','p_evidence_id','p_expected_revision')
                values = [args[k] for k in keys]
                params = ','.join("'" + str(v).replace("'", "''") + "'" for v in values)
                if 'p_attestation_ids' in args:
                    params += ',array[' + ','.join("'"+v+"'" for v in args['p_attestation_ids']) + ']::uuid[]'
                command = 'set role service_role; select public.' + url.rsplit('/',1)[-1] + '(' + params + ');'
                result = subprocess.run(base, input=command, text=True, capture_output=True, timeout=30)
                payload = ({'code':'23514', 'message':result.stderr} if result.returncode else
                           True if result.stdout.strip() == 't' else json.loads(result.stdout.strip()))
                return SimpleNamespace(status_code=400 if result.returncode else 200, json=lambda:payload)
            def http_get(url, **kwargs):
                allowed={'fixer_forward_media_lineage_20261006','fixer_forward_media_claim_receipt_20261006','fixer_forward_media_object_read_20261006'}
                assert url in allowed
                predicates=[]
                for key,value in kwargs['params'].items():
                    if key in ('select','order','limit'):continue
                    assert value.startswith('eq.') and key.replace('_','').isalnum()
                    predicates.append(key+"='"+value[3:].replace("'","''")+"'")
                command="set role service_role; select coalesce(jsonb_agg(to_jsonb(r)),'[]'::jsonb) from (select * from "+url+' where '+ ' and '.join(predicates)+') r;'
                payload=json.loads(sql(command))
                return SimpleNamespace(status_code=200,json=lambda:payload)
            def get_row(gym,row_id):
                return json.loads(sql("set role service_role; select to_jsonb(r) from content_calendar r where id='"+row_id+"' and gym_id='"+gym+"';"))
            store = SimpleNamespace(_client=lambda:SimpleNamespace(post=http_post,get=http_get),
                                    get_row=get_row, _rest=lambda u:u, _headers=lambda h=None:h)

            try:
                os.environ['AGENT_FORWARD_MEDIA_VISUAL_INDEX']='1'
                os.environ['AGENT_FORWARD_MEDIA_GUARD']='1'
                index._connect=lambda: (_ for _ in ()).throw(AssertionError('publisher accessed attester credentials'))
                assert guard.claim(store, probe[0], probe[1], probe_ev, probe_rev) is True
                providers.append('synthetic-authorized')
                assert calls == ['rpc/fixer_forward_visual_proof_20261008','rpc/fixer_forward_visual_index_claim_20261008']
                # Bounded real-role negative append now persists and denies replay.
                sql("set role fixer_forward_media_attester_20261006; select fixer_forward_visual_negative_append_20261008("
                    f"'{probe_ev}','{probe[2]}','{probe[6]}',null,'undecodable-runtime-evidence');")
                try:
                    guard.claim(store, probe[0], probe[1], probe_ev, probe_rev)
                    providers.append('unexpected-send')
                    raise AssertionError('negative replay authorized')
                except guard.ForwardMediaVerificationHold:
                    pass
                assert providers == ['synthetic-authorized']
                # Real calendar bridge + the actual lower provider decorator
                # checks mismatched env flags against both DB gate states.
                from agent import forward_media_publish as bridge, forward_media_send_context as send_scope, config
                coherence=vseed()
                c_row=get_row(coherence[2],coherence[0])
                account=SimpleNamespace(key=coherence[2]+'_ig',platform='instagram',get_target_id=lambda:'synthetic-native-target')
                draft=SimpleNamespace(draft_id=c_row['id'],account_key=account.key,platform='instagram',
                    day_key=c_row['post_date'],creative_public_url=c_row['image_url'],caption=c_row.get('caption'),
                    is_story=False,slide_urls=[],creative_path='')
                boundary=[]
                @send_scope.guarded_publisher('meta')
                def provider(draft,account):
                    boundary.append('synthetic-provider')
                    return SimpleNamespace(ok=True)
                saved_publish=config.publish_enabled
                config.publish_enabled=lambda:True
                try:
                    for db_gate in (False,True):
                        sql(f"update forward_media_visual_gate_20261008 set enabled={str(db_gate).lower()};")
                        for base_flag in (None,'false','0'):
                            if base_flag is None:os.environ.pop('AGENT_FORWARD_MEDIA_GUARD',None)
                            else:os.environ['AGENT_FORWARD_MEDIA_GUARD']=base_flag
                            try:
                                bridge.authorize(store,c_row,coherence[1])
                                raise AssertionError('incoherent flags authorized calendar publication')
                            except guard.ForwardMediaVerificationHold:pass
                            try:
                                with bridge.authorized_send(store,c_row,coherence[1]):provider(draft,account)
                                raise AssertionError('incoherent flags reached provider')
                            except send_scope.ProviderSendHold:pass
                            try:
                                provider(draft,account)
                                raise AssertionError('incoherent flags bypassed lower provider scope')
                            except send_scope.ProviderSendHold:pass
                            assert boundary==[]
                        os.environ['AGENT_FORWARD_MEDIA_GUARD']='1'
                        if not db_gate:
                            try:
                                with bridge.authorized_send(store,c_row,coherence[1]):provider(draft,account)
                                raise AssertionError('visual env with DB OFF reached provider')
                            except send_scope.ProviderSendHold:pass
                            assert boundary==[]
                        else:
                            with bridge.authorized_send(store,c_row,coherence[1]):assert provider(draft,account).ok
                            assert boundary==['synthetic-provider']
                finally:
                    config.publish_enabled=saved_publish
            finally:
                index._connect=saved_connect
                if saved_base_flag is None: os.environ.pop('AGENT_FORWARD_MEDIA_GUARD',None)
                else: os.environ['AGENT_FORWARD_MEDIA_GUARD']=saved_base_flag
                if saved_flag is None: os.environ.pop('AGENT_FORWARD_MEDIA_VISUAL_INDEX',None)
                else: os.environ['AGENT_FORWARD_MEDIA_VISUAL_INDEX']=saved_flag

            # Distinct original/render/thumbnail SHA and pHash must permit own
            # exact-token replay and an authorized logical sibling. Self roles
            # are not separate posts and never compete with one another.
            derived_tenant='derived_'+uuid.uuid4().hex
            derived_group='derived_group_'+uuid.uuid4().hex
            surl='https://scratch.example/'+uuid.uuid4().hex
            iurl='https://scratch.example/'+uuid.uuid4().hex
            turl='https://scratch.example/'+uuid.uuid4().hex
            fps=['md5:'+uuid.uuid4().hex for _ in range(3)]
            shas=[uuid.uuid4().hex+uuid.uuid4().hex for _ in range(3)]
            dph=_fresh_phash()
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{derived_tenant}',true);")
            def derivative_seed():
                pair=row(derived_tenant,derived_group,surl,image=iurl,thumbnail=turl)
                dr, dt, _ = attest(pair,fps[0],fps[1],fps[2],'render')
                di=visual_attest(dr,derived_tenant,shas[0],dph)
                # Replace prep with three independently trusted role hashes.
                new=[]
                for j, old in enumerate(di):
                    ident=str(uuid.uuid4());new.append(ident)
                    sql("set role fixer_forward_media_attester_20261006; insert into forward_media_visual_attestation"
                        "(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id) "
                        f"select '{ident}',tenant_key,media_url,role,'{shas[j]}',source_md5,byte_length,{dph},row_revision,lineage_receipt_id,object_read_receipt_id "
                        f"from forward_media_visual_attestation where attestation_id='{old}';")
                return dr,dt,new
            dr,dt,di=derivative_seed()
            assert vclaim(dr,dt,derived_tenant,'2026-10-10',di)=='t'
            assert vclaim(dr,dt,derived_tenant,'2026-10-10',di)=='t'
            sr,st,si=derivative_seed()
            assert vclaim(sr,st,derived_tenant,'2026-10-10',si)=='t'
            # Frozen proof prevents replacement on replay; later prep cannot
            # pollute occupancy even when it supplies a contradictory pHash.
            polluted=visual_attest(dr,derived_tenant,uuid.uuid4().hex+uuid.uuid4().hex,0)
            assert 'frozen proof changed' in vclaim(dr,dt,derived_tenant,'2026-10-10',polluted,ok=False)
            assert vclaim(dr,dt,derived_tenant,'2026-10-10',di)=='t'
            changed_token=str(uuid.uuid4())
            sql(f"update content_calendar set publish_claim_token='{changed_token}' where id='{dr}';")
            assert 'owned claim token drift' in vclaim(dr,changed_token,derived_tenant,'2026-10-10',di,ok=False)

            # Two conflicting concurrent claims: at most one success and no
            # partial occupancy for the loser (atomic rollback).
            csha = uuid.uuid4().hex + uuid.uuid4().hex
            a = vseed(sha64=csha, day='2026-10-10')
            b = vseed(sha64=csha, day='2026-10-11')

            def race(entry):
                rid, token, tenant, _g, _u, _fp, _s, ids = entry
                day = sql(f"select post_date from content_calendar where id='{rid}';")
                return subprocess.run(base, input="begin; set role service_role; select "
                    "fixer_forward_visual_index_claim_20261008(" + claim_args(rid, token, ids) + ");"
                    "select pg_sleep(0.5); commit;", text=True, capture_output=True, timeout=15)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(race, (a, b)))
            assert sum(x.returncode == 0 for x in results) == 1, [x.stderr for x in results]
            loser = b if results[0].returncode == 0 else a
            assert sql("select count(*) from fixer_forward_media_use_20261006 "
                       f"where fingerprint='{loser[5]}';") == '0'
            assert sql("select count(*) from fixer_forward_media_claim_receipt_20261006 "
                       f"where claim_token='{loser[1]}';") == '0'

            # Role and receipt forgery hold.
            f2_rid, f2_token, f2_tenant, _, _, _, _, f2_ids = vseed()
            dup = [f2_ids[0], f2_ids[1], f2_ids[1]]  # duplicate role set
            assert 'evidence invalid' in vclaim(f2_rid, f2_token, f2_tenant, '2026-10-10', dup, ok=False)
            other_lineage = sql("select l.evidence_id from fixer_forward_media_lineage_20261006 l "
                                f"where l.calendar_row_id<>'{f2_rid}' and not exists(select 1 from "
                                "fixer_forward_media_claim_receipt_20261006 rec where rec.calendar_row_id=l.calendar_row_id) "
                                "limit 1;")
            forged_rev = revision_bigint(f2_rid)
            f2_url = sql(f"select source_media_url from content_calendar where id='{f2_rid}';")
            f2_md5 = sql("select substring(fingerprint from 5) from fixer_forward_media_object_read_20261006 o "
                         "join fixer_forward_media_lineage_20261006 l on l.source_read_receipt=o.receipt_id "
                         f"where l.calendar_row_id='{f2_rid}';")
            forged_id = str(uuid.uuid4())
            sql("set role fixer_forward_media_attester_20261006; insert into forward_media_visual_attestation"
                "(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id)"
                f" select '{forged_id}',tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,"
                f"'{other_lineage}',object_read_receipt_id from forward_media_visual_attestation "
                f"where attestation_id='{f2_ids[0]}';")
            assert 'evidence invalid' in vclaim(f2_rid, f2_token, f2_tenant, '2026-10-10',
                                                [forged_id, f2_ids[1], f2_ids[2]], ok=False)
            # Missing fingerprint: unknown attestation ids hold.
            missing = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
            assert 'evidence invalid' in vclaim(f2_rid, f2_token, f2_tenant, '2026-10-10', missing, ok=False)
            # Caller row/token/revision bindings remain authoritative.
            bad_args = claim_args(f2_rid, f2_token, f2_ids).replace(f2_token, str(uuid.uuid4()))
            assert 'ownership binding invalid' in sql('set role service_role; select fixer_forward_visual_index_claim_20261008(' + bad_args + ');', ok=False)
            # Producer-asserted md5 that differs from the trusted receipt holds.
            prod_id = str(uuid.uuid4())
            sql("set role fixer_forward_media_attester_20261006; insert into forward_media_visual_attestation"
                "(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id)"
                f" select '{prod_id}',tenant_key,media_url,role,source_sha256,'{'0'*32}',byte_length,phash_v1,row_revision,"
                f"lineage_receipt_id,object_read_receipt_id from forward_media_visual_attestation "
                f"where attestation_id='{f2_ids[0]}';")
            assert 'evidence invalid' in vclaim(f2_rid, f2_token, f2_tenant, '2026-10-10',
                                                [prod_id, f2_ids[1], f2_ids[2]], ok=False)
            # Changed media revision invalidates earlier attestations.
            sql(f"update content_calendar set image_url='https://scratch.example/revised_{uuid.uuid4().hex}' where id='{f2_rid}';")
            assert 'versioned render manifest binding unavailable' in vclaim(f2_rid, f2_token, f2_tenant, '2026-10-10', f2_ids, ok=False)

            # Explicit uncertain history added after preparation denies the
            # real final claim; an empty/new index cannot erase history holds.
            uncertain=vseed()
            sql("insert into fixer_forward_media_original_registry_20261006 "
                "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref) "
                "select tenant_id,source_asset_id||'_historical',source_url||'/history',source_fingerprint,source_length,registry_evidence_ref "
                f"from fixer_forward_media_original_registry_20261006 where tenant_id='{uncertain[2]}';")
            sql("insert into fixer_forward_media_history_clearance_20261006 "
                "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref) "
                "select tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,'hold_uncertain','history-census-unknown' "
                f"from fixer_forward_media_original_registry_20261006 where tenant_id='{uncertain[2]}' and source_asset_id like '%_historical';")
            assert 'historical eligibility clearance unavailable or held' in vclaim(uncertain[0],uncertain[1],uncertain[2],'2026-10-10',uncertain[7],ok=False)
            assert sql(f"select count(*) from forward_media_visual_claim_proof_20261008 where claim_token='{uncertain[1]}';")=='0'

            # Late negative: appended between preparation and claim blocks;
            # appended before replay also blocks the replay.
            n_rid, n_token, n_tenant, _, _, _, n_sha, n_ids = vseed()
            sql("insert into forward_media_visual_negative(tenant_key,source_sha256,reason) "
                f"values('{n_tenant}','{n_sha}','provider-error-ambiguous-send');")
            assert 'negative evidence blocks' in vclaim(n_rid, n_token, n_tenant, '2026-10-10', n_ids, ok=False)
            assert sql("select count(*) from fixer_forward_media_claim_receipt_20261006 "
                       f"where claim_token='{n_token}';") == '0'
            r_rid, r_token, r_tenant, _, _, _, r_sha, r_ids = vseed()
            assert 't' == vclaim(r_rid, r_token, r_tenant, '2026-10-10', r_ids)
            sql("insert into forward_media_visual_negative(tenant_key,source_sha256,reason) "
                f"values('{r_tenant}','{r_sha}','deleted-replaced-after-claim');")
            assert 'negative evidence blocks' in vclaim(r_rid, r_token, r_tenant, '2026-10-10', r_ids, ok=False)

            # Deletion/replacement permanence: deleting the calendar row never
            # frees the visual; evidence and negatives are immutable.
            d_rid, d_token, d_tenant, _, _, _, d_sha, d_ids = vseed()
            assert 't' == vclaim(d_rid, d_token, d_tenant, '2026-10-10', d_ids)
            sql(f"delete from content_calendar where id='{d_rid}';")
            e_rid, e_token, e_tenant, _, _, _, _, e_ids = vseed(sha64=d_sha)
            assert 'another tenant/date/group' in vclaim(e_rid, e_token, e_tenant, '2026-10-10', e_ids, ok=False)
            for table in ('forward_media_visual_attestation', 'forward_media_visual_negative', 'forward_media_visual_claim_proof_20261008'):
                assert 'immutable' in sql(f"update {table} set reason='x';" if table.endswith('negative')
                                          else f"update {table} set claim_token=gen_random_uuid();" if table.endswith('20261008') else f"update {table} set media_url='x';", ok=False)
                assert 'immutable' in sql(f"delete from {table};", ok=False)
                assert 'immutable' in sql(f"truncate {table};", ok=False)

            # ACLs: the RPC is not publicly callable; only the existing claim
            # executor role may run it. Attester holds INSERT/SELECT only.
            for denied in ('anon', 'authenticated', 'fixer_forward_media_attester_20261006',
                           'fixer_forward_media_owner_20261006'):
                assert 'permission denied' in sql(f"set role {denied}; select "
                    'public.fixer_forward_visual_index_claim_20261008(' + claim_args(r_rid, r_token, r_ids) + ');', ok=False)
            assert sql("select has_function_privilege('public',"
                       "'public.fixer_forward_visual_index_claim_20261008(uuid,uuid,uuid,text,uuid[])','execute');") == 'f'
            assert 'permission denied' in sql("set role fixer_forward_media_attester_20261006; "
                "insert into forward_media_visual_negative(tenant_key,source_sha256,reason) "
                f"values('x','{'0'*64}','forged');", ok=False)
            assert 'permission denied' in sql("set role service_role; insert into forward_media_visual_attestation"
                "(tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id)"
                f" values('x','https://scratch.example/x','original','{'0'*64}','{'0'*32}',10,0,0,"
                f"'{uuid.uuid4()}','{uuid.uuid4()}');", ok=False)
            # Base guard OFF: the extension preserves the gate and fails closed.
            off_rid, off_token, off_tenant, _, _, _, _, off_ids = vseed()
            sql(f"update fixer_forward_media_claim_gate_20261006 set enabled=false where tenant_id='{off_tenant}';")
            assert 'OFF' in vclaim(off_rid, off_token, off_tenant, '2026-10-10', off_ids, ok=False)
            # Isolation guard matches the base stack.
            assert 'read committed isolation' in sql("begin isolation level repeatable read; set role service_role; "
                'select public.fixer_forward_visual_index_claim_20261008(' + claim_args(r_rid, r_token, r_ids) + '); commit;', ok=False)

            # Rollback-only installation: install into a second fully
            # disposable cluster, verify objects exist, then destroy the
            # cluster entirely (nothing persists).
            rb_sock = work / 'rb_sock'
            rb_sock.mkdir()
            rb_data = work / 'rb_data'
            subprocess.run(['initdb', '-D', str(rb_data), '-U', 'postgres', '--no-sync'],
                           check=True, capture_output=True, timeout=60)
            subprocess.run(['pg_ctl', '-D', str(rb_data), '-l', str(work / 'rb_pg.log'),
                            '-o', f"-k {rb_sock} -p 55470 -c listen_addresses=''", '-w', 'start'],
                           check=True, capture_output=True, timeout=60)
            try:
                rb = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(rb_sock),
                      '-p', '55470', '-U', 'postgres', '-d', 'postgres']

                def rsql(s):
                    result = subprocess.run(rb, input=s, text=True, capture_output=True, timeout=30)
                    if result.returncode:
                        raise AssertionError(result.stderr)
                    return result.stdout.strip()

                rsql("create role anon; create role authenticated; create role service_role;"
                     "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                     "account text,format text,gbp_location_id text,status text,variant_status text,"
                     "published_at timestamptz,publish_claim_token uuid,"
                     "publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,"
                     "media_not_ready_reason text);")
                rsql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
                rsql((ROOT / 'migrations/DRAFT_fixer_forward_visual_index_20261008.sql').read_text())
                assert rsql("select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
                            "where n.nspname='public' and p.proname='fixer_forward_visual_index_claim_20261008';") == '1'
                assert rsql("select count(*) from pg_tables where schemaname='public' and tablename in "
                            "('forward_media_visual_attestation','forward_media_visual_negative');") == '2'
            finally:
                subprocess.run(['pg_ctl', '-D', str(rb_data), '-m', 'immediate', '-w', 'stop'],
                               capture_output=True, timeout=60)
            assert not subprocess.run(['pg_ctl', '-D', str(rb_data), 'status'],
                                      capture_output=True, timeout=30).returncode == 0

            print('PASS: PG17 real calendar bridge/provider wrapper; mismatched base/visual env with DB gate ON/OFF denied; '
                  'coherent ON one synthetic provider boundary, coherent env with DB OFF held; '
                  'real attester-role Python and service guard integration; OFF legacy preserved; '
                  'armed legacy/private bypass denied; frozen proof, token drift and prep pollution; '
                  'distinct original/render/thumbnail siblings; persisted attester negatives and unknown history holds; '
                  'atomic extension of base claim; one-transaction frozen proof+occupancy; '
                  'conflicting concurrent claims one-winner with no partial occupancy; URL alias and '
                  'derivative cross-tenant/date blocking; role/receipt/producer-hash forgery hold; '
                  'missing fingerprint hold; late negative blocks claim and replay; exact-token retry '
                  'and same-group sibling reuse; deletion/replacement permanence; immutable evidence; '
                  'pHash <=6 block, 7-30 review hold (28 included), >30 no match; ACL denial and no '
                  'public execute; base OFF gate preserved; rollback-only installation')
        finally:
            subprocess.run(['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
