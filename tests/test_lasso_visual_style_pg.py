"""Real PG17 full42 CAS tests for additive owned visual style repair. Disposable local socket, no live writes. Run directly:
python tests/test_lasso_visual_style_pg.py
Requires postgresql@17 binaries and psycopg."""
import datetime
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]

# Full production content_calendar column set (per the repo migrations and the
# verified read-only production preimages). No synthetic diagnostic columns.
COLUMNS = ('id', 'gym_id', 'account', 'post_date', 'pillar', 'format',
           'caption', 'image_url', 'status', 'created_at', 'published_at',
           'late_post_id', 'scheduled_at', 'thumbnail_url', 'gbp_topic_type',
           'gbp_cta_type', 'gbp_cta_url', 'gbp_event', 'gbp_offer',
           'gbp_location_id', 'reject_reason', 'source_media_url', 'mentions',
           'hook_family', 'ask_type', 'time_slot', 'caption_len_band',
           'has_member_face', 'experiment_label', 'slot_index',
           'source_media_asset_id', 'media_not_ready_reason', 'event_id',
           'variant_of', 'variant_status', 'publish_reservation_day',
           'publish_claim_token', 'logical_post_id', 'approval_kind',
           'approved_by', 'approved_at', 'approval_digest')
SHA2 = hashlib.sha256(b'y').hexdigest()
BRAIN = {'snapshot': 'nov-brain-1'}
POLICY = 'policy-2026-11'


def main():
    import psycopg
    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    assert shutil.disk_usage('/tmp').free > 5*1024**3
    with tempfile.TemporaryDirectory(prefix='lasso_story_conv_pg_', dir='/tmp') as tmp:
        root = Path(tmp)
        sock = root/'sock'
        sock.mkdir()
        data = root/'data'
        port = random.randint(41000, 59000)
        subprocess.run([str(pg/'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        started = False
        try:
            subprocess.run([str(pg/'pg_ctl'), '-D', str(data), '-l', str(root/'pg.log'),
                '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                check=True, capture_output=True, timeout=60)
            started = True
            dsn = f'host={sock} port={port} dbname=postgres'
            admin = psycopg.connect(dsn+' user=postgres', autocommit=True)

            def sql(query, values=None):
                with admin.cursor() as cur:
                    cur.execute(query, values)
                    return cur.fetchall() if cur.description else None

            sql('create role anon; create role authenticated; create role service_role;')
            sql('create table gyms(gym_id text primary key, autonomous boolean);'
                "insert into gyms values('lasso', true);"
                'create or replace function public.calendar_gym_is_autonomous(p_gym_id text)'
                ' returns boolean language sql stable as $$'
                ' select coalesce((select autonomous from gyms where gym_id = p_gym_id), false) $$;')
            sql('create table content_calendar('
                'id uuid primary key, gym_id text, account text, post_date date,'
                'pillar text, format text, caption text, image_url text, status text,'
                'created_at timestamptz, published_at timestamptz, late_post_id text,'
                'scheduled_at timestamptz, thumbnail_url text, gbp_topic_type text,'
                'gbp_cta_type text, gbp_cta_url text, gbp_event jsonb, gbp_offer jsonb,'
                'gbp_location_id text, reject_reason text, source_media_url text,'
                'mentions jsonb, hook_family text, ask_type text, time_slot text,'
                'caption_len_band text, has_member_face boolean, experiment_label text,'
                'slot_index integer, source_media_asset_id text, media_not_ready_reason text,'
                'event_id text, variant_of uuid, variant_status text,'
                'publish_reservation_day date, publish_claim_token uuid,'
                'logical_post_id uuid, approval_kind text, approved_by text,'
                'approved_at timestamptz, approval_digest text);')
            sql('create table echo_infographic_artifacts('
                'tenant text, image_url text, image_sha256 text,'
                'source_identity jsonb, evidence jsonb);')
            sql('create table lasso_managed_paired_stories('
                'story_id uuid primary key, feed_id uuid not null);')
            sql((ROOT/'migrations/lasso_visual_style_hold_20261009.sql').read_text())
            from psycopg.types.json import Jsonb
            today=sql("select (now() at time zone 'America/New_York')::date::text")[0][0]
            rid=str(uuid.uuid4()); sha='a'*64
            brain={'brand_voice/lasso_visual_standard.md':'b'*64}
            policy='lasso-astra-2026-10-09-grounded-editorial-v2'
            seed={k:None for k in COLUMNS}
            seed.update(id=rid,gym_id='lasso',account='instagram',post_date=today,pillar='book',format='feed',
                        caption='Protected book copy remains exact.',image_url='https://old.example/feed.png',
                        status='pending',variant_status='active',slot_index=0,created_at=today+'T00:00:00Z')
            def reset(row=None):
                sql('delete from content_calendar')
                sql('insert into content_calendar select (jsonb_populate_record(null::content_calendar,%s)).*',(Jsonb(row or seed),))
                return sql('select to_jsonb(c) from content_calendar c')[0][0]
            def hold(expected):
                return sql('select hold_lasso_style_feed_20261009(%s,%s,%s,%s)',(rid,Jsonb(expected),today,today))[0][0]
            def replace(expected, ev=None):
                if ev is not None:
                    sql('delete from echo_infographic_artifacts')
                    source={'source_id':'content_calendar:'+rid+':caption','source_hash':hashlib.sha256(seed['caption'].encode()).hexdigest()}
                    sql('insert into echo_infographic_artifacts values(%s,%s,%s,%s,%s)',('lasso_ig',seed['image_url'],sha,Jsonb(source),Jsonb(ev)))
                return sql('select replace_lasso_style_feed_media_20261009(%s,%s,%s,%s,%s,%s,%s)',
                   (rid,Jsonb(expected),seed['image_url'],policy,Jsonb(brain),sha,'real-review'))[0][0]
            evidence=dict(policy_version=policy,brain_snapshot=brain,image_sha256=sha,review_response_id='real-review',
                          grade_status='PASS',style_conformant=True,style_violations=[],
                          visual_standard_version='lasso-grounded-editorial-2026-10-09-v1',aspect='4:5')
            count=0
            # Every exact protected field, including non-generation metadata, is CAS authority.
            for key,val in [('caption','changed'),('thumbnail_url','https://changed'),('gbp_event',{'name':'changed'}),
                            ('publish_claim_token',str(uuid.uuid4())),('publish_reservation_day',today),
                            ('approval_kind','manual'),('approved_by','human'),('approved_at',today+'T00:00:00Z'),('approval_digest','digest')]:
                before=reset(); changed=dict(before);changed[key]=val
                reset(changed); assert hold(before)['result']=='conflict';count+=1
                assert sql('select to_jsonb(c) from content_calendar c')[0][0]==reset(changed)
            for key,val in [('gym_id','client'),('status','published'),('variant_status','inactive'),('format','story'),('slot_index',None)]:
                row=dict(seed);row[key]=val;before=reset(row);assert hold(before)['result']=='conflict';count+=1
            before=reset()
            for key in ['created_at','scheduled_at']:
                for stamp in ['',today+'T00:00:00','bad timestamp']:
                    expected=dict(before);expected[key]=stamp
                    sql("set timezone='Pacific/Honolulu'")
                    assert hold(expected)['result']=='conflict';count+=1
            for expected in [dict(before,extra='schema drift'),{k:v for k,v in before.items() if k!='thumbnail_url'}]:
                assert hold(expected)['result']=='conflict';count+=1
            # Equivalent aware timestamps are accepted even under a non-UTC session.
            before=reset();before['created_at']=today+'T00:00:00Z'
            receipt=hold(before);assert receipt['result']=='held';held=receipt['row']
            assert held['media_not_ready_reason']=='lasso_visual_style_review_required'
            assert held['caption']==seed['caption']
            for key,val in [('review_response_id',''),('policy_version','old'),('style_conformant',False),
                            ('style_violations',['neon']),('visual_standard_version','old'),('brain_snapshot',{}),('aspect','9:16')]:
                ev=dict(evidence);ev[key]=val
                assert replace(held,ev)['result']=='conflict';count+=1
            for key,val in [('thumbnail_url','https://changed'),('gbp_event',{'changed':True}),
                            ('publish_claim_token',str(uuid.uuid4())),('publish_reservation_day',today),
                            ('approval_kind','manual'),('approved_by','human'),('approved_at',today+'T00:00:00Z'),
                            ('approval_digest','digest'),('media_not_ready_reason','another hold')]:
                changed=dict(held);changed[key]=val;reset(changed)
                assert replace(held,evidence)['result']=='conflict';count+=1
                # Fresh snapshots still cannot bypass protected eligibility.
                fresh=sql('select to_jsonb(c) from content_calendar c')[0][0]
                if key in ('publish_claim_token','publish_reservation_day','approval_kind','approved_by','approved_at','approval_digest','media_not_ready_reason'):
                    assert replace(fresh,evidence)['result']=='conflict';count+=1
            reset(held)
            assert replace(held,evidence)['result']=='replaced'
            after=sql('select to_jsonb(c) from content_calendar c')[0][0]
            assert after['caption']==seed['caption'] and after['image_url']==seed['image_url'] and after['media_not_ready_reason'] is None
            assert replace(held,evidence)['result']=='conflict' # no stale replay mutation
            before=reset();sql("update gyms set autonomous=false where gym_id='lasso'")
            assert hold(before)['result']=='conflict';count+=1
            sql("update gyms set autonomous=true where gym_id='lasso'")
            assert sql("select has_function_privilege('anon','hold_lasso_style_feed_20261009(uuid,jsonb,date,date)','EXECUTE')")[0][0] is False
            assert sql("select has_function_privilege('service_role','hold_lasso_style_feed_20261009(uuid,jsonb,date,date)','EXECUTE')")[0][0] is True
            admin.close()
            print(f'PASS PG17: {count} negative probes; full42 CAS, approval/claim/reservation/tenant guards, aware timestamps, current style review, same-byte genuine review, autonomy and least privilege')
        finally:
            if started:
                subprocess.run([str(pg/'pg_ctl'), '-D', str(data), '-m', 'immediate',
                                '-w', 'stop'], check=True, capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
