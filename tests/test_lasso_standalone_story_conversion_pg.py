"""Real PG17 atomic conversion tests for the five evidenced legacy LASSO
standalone Stories. Disposable local socket, no live writes. Run directly:
python tests/test_lasso_standalone_story_conversion_pg.py
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
            for name in ('lasso_paired_story_published_legacy_coexistence_20261005.sql',
                         'lasso_paired_story_repair_20261005.sql',
                         'lasso_standalone_story_conversion_20261008.sql'):
                sql((ROOT/'migrations'/name).read_text())

            manifest = sql('select story_id::text, feed_id::text, post_date::text,'
                           ' expected_story, expected_feed'
                           ' from lasso_standalone_story_conversion_manifest_20261008'
                           ' order by post_date')
            assert len(manifest) == 5
            art = {}
            calls = {"count": 0}

            def seed_story(i):
                sid = manifest[i][0]
                sql('delete from content_calendar where id=%s', (sid,))
                sql('insert into content_calendar'
                    ' select (jsonb_populate_record(null::content_calendar, expected_story)).*'
                    ' from lasso_standalone_story_conversion_manifest_20261008'
                    ' where story_id=%s', (sid,))
                return sid

            def seed_feed(i):
                # The evidenced existing feed UUID/preimage, with root-stamped
                # canonical noon schedule, cleared hold and fresh caption-bound
                # PASS artwork for both the feed image and the new 9:16 Story.
                fid, day = manifest[i][1], manifest[i][2]
                caption = manifest[i][4]['caption']
                image = manifest[i][4]['image_url']
                url = f'https://example.test/story916_{i}.png'
                sha = hashlib.sha256(f'story-{i}'.encode()).hexdigest()
                art[i] = (url, sha)
                sql('delete from content_calendar where id=%s', (fid,))
                sql('insert into content_calendar'
                    ' select (jsonb_populate_record(null::content_calendar, expected_feed)).*'
                    ' from lasso_standalone_story_conversion_manifest_20261008'
                    ' where story_id=%s', (manifest[i][0],))
                sql("update content_calendar set media_not_ready_reason=null,"
                    " source_media_url=image_url,"
                    " scheduled_at=((post_date::text || ' 12:00')::timestamp"
                    "  at time zone 'America/New_York') where id=%s", (fid,))
                cap_hash = hashlib.sha256(caption.encode()).hexdigest()
                feed_sha = hashlib.sha256(f'feed-{i}'.encode()).hexdigest()
                sql('delete from echo_infographic_artifacts where image_url in (%s,%s)',
                    (image, url))
                for img, sh, nine16 in ((image, feed_sha, False), (url, sha, True)):
                    ev = {'grade_status': 'PASS', 'image_sha256': sh,
                          'policy_version': POLICY, 'brain_snapshot': BRAIN,
                          'review_response_id': 'resp-1'}
                    if nine16:
                        ev.update({'aspect': '9:16', 'pixels': '1080x1920',
                                   'verified_dimensions': {'width': '1080',
                                       'height': '1920', 'image_sha256': sh}})
                    sql('insert into echo_infographic_artifacts values'
                        "('lasso_ig',%s,%s,%s,%s)",
                        (img, sh,
                         json.dumps({'source_id': f'content_calendar:{fid}:caption',
                                     'source_hash': cap_hash}),
                         json.dumps(ev)))
                return fid

            def expected_feed(fid):
                return sql('select to_jsonb(r) from content_calendar r where id=%s',
                           (fid,))[0][0]

            def call(sid, fid, exp_story, exp_feed, url=None, sha=None,
                     source_hash=None, policy=POLICY, brain=BRAIN, sched=None,
                     conn=None):
                calls["count"] += 1
                if url is None or sha is None:
                    i = next(k for k, v in art.items()
                             if manifest[k][1] == fid)
                    url = art[i][0] if url is None else url
                    sha = art[i][1] if sha is None else sha
                if source_hash is None:
                    i = next(k for k, v in art.items()
                             if manifest[k][1] == fid)
                    source_hash = hashlib.sha256(
                        manifest[i][4]['caption'].encode()).hexdigest()
                if sched is None:
                    sched = sql('select (scheduled_at + cast(%s as interval))::text'
                                ' from content_calendar where id=%s',
                                ('15 minutes', fid))[0][0]
                args = (sid, fid, json.dumps(exp_story), json.dumps(exp_feed),
                        url, sha, 'lasso_ig', source_hash, policy,
                        json.dumps(brain), sched)
                q = ('select convert_lasso_standalone_story_20261008'
                     '(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)')
                c = conn or admin
                with c.cursor() as cur:
                    cur.execute('set role service_role')
                    cur.execute(q, args)
                    out = cur.fetchone()[0]
                    cur.execute('reset role')
                return out

            # ---- refusal battery on story index 4 (Nov 6, never converts) ----
            I = 4
            fid = seed_feed(I)
            sid = seed_story(I)
            exp_story = manifest[I][3]
            exp_feed = expected_feed(fid)
            refusals = []

            r = call(str(uuid.uuid4()), fid, exp_story, exp_feed)
            refusals.append(('unknown_story', r))
            stale = dict(exp_story); stale['caption'] = 'edited'
            r = call(sid, fid, stale, exp_feed)
            refusals.append(('stale_story_preimage', r))
            other_feed = sql('select feed_id::text'
                             ' from lasso_standalone_story_conversion_manifest_20261008'
                             ' where story_id <> %s limit 1', (sid,))[0][0]
            r = call(sid, other_feed, exp_story, exp_feed,
                     url=art[I][0], sha=art[I][1],
                     source_hash=hashlib.sha256(
                         manifest[I][4]['caption'].encode()).hexdigest(),
                     sched='2026-11-01 12:15:00-05')
            refusals.append(('feed_mismatch', r))
            r = call(sid, fid, exp_story, exp_feed, sha='zz')
            refusals.append(('invalid_input', r))
            missing = {k: v for k, v in exp_feed.items() if k != 'caption'}
            r = call(sid, fid, exp_story, missing)
            refusals.append(('feed_key_mismatch', r))
            extra = dict(exp_feed); extra['managed_links'] = None
            r = call(sid, fid, exp_story, extra)
            refusals.append(('feed_key_mismatch', r))
            sql("update gyms set autonomous=false where gym_id='lasso'")
            r = call(sid, fid, exp_story, exp_feed)
            refusals.append(('autonomy_not_confirmed', r))
            sql("update gyms set autonomous=true where gym_id='lasso'")
            sql("update content_calendar set gym_id='other' where id=%s", (sid,))
            r = call(sid, fid, exp_story, exp_feed)
            refusals.append(('story_not_convertible', r))
            seed_story(I)
            sql("update content_calendar set publish_claim_token=%s where id=%s",
                (str(uuid.uuid4()), sid))
            r = call(sid, fid, exp_story, exp_feed)
            refusals.append(('story_not_convertible', r))
            seed_story(I)
            sql("update content_calendar set status='published',"
                " published_at=now(), late_post_id='late-1' where id=%s", (sid,))
            r = call(sid, fid, exp_story, exp_feed)
            refusals.append(('story_not_convertible', r))
            seed_story(I)
            sql('insert into lasso_managed_paired_stories values(%s,%s)', (sid, fid))
            r = call(sid, fid, exp_story, exp_feed)
            refusals.append(('story_already_managed', r))
            sql('delete from lasso_managed_paired_stories where story_id=%s', (sid,))
            # Feed readiness gates.
            sql('update content_calendar set status=null where id=%s', (fid,))
            r = call(sid, fid, exp_story, expected_feed(fid))
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            sql("update content_calendar set publish_reservation_day=%s where id=%s",
                (manifest[I][2], fid))
            r = call(sid, fid, exp_story, expected_feed(fid))
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            sql("update content_calendar set media_not_ready_reason="
                "'cross_date_media_repeat_needs_new_visual' where id=%s", (fid,))
            r = call(sid, fid, exp_story, expected_feed(fid))
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            sql('delete from echo_infographic_artifacts where image_url=%s',
                (manifest[I][4]['image_url'],))
            r = call(sid, fid, exp_story, expected_feed(fid))
            refusals.append(('feed_review_missing', r))
            seed_feed(I)
            # Explicit approval refusal even with an exact supplied preimage.
            approval_values = {'approval_kind': 'human', 'approved_by': 'reviewer',
                               'approved_at': '2026-11-01T17:00:00Z',
                               'approval_digest': 'digest'}
            for field, value in approval_values.items():
                seed_feed(I)
                sql(f'update content_calendar set {field}=%s where id=%s', (value, fid))
                r = call(sid, fid, exp_story, expected_feed(fid))
                refusals.append(('feed_not_ready', r))
            # A PASS label without a real nonblank review receipt is insufficient.
            for value in (None, '', '   '):
                seed_feed(I)
                sql("update echo_infographic_artifacts set evidence="
                    "jsonb_set(evidence,'{review_response_id}',%s::jsonb) where image_url=%s",
                    (json.dumps(value), manifest[I][4]['image_url']))
                r = call(sid, fid, exp_story, expected_feed(fid))
                refusals.append(('feed_review_missing', r))
            seed_feed(I)
            # Timestamp snapshots must be null or explicitly timezone-aware.
            # Non-UTC sessions must never interpret naive inputs or equate '' to NULL.
            sql("set timezone='America/Los_Angeles'")
            for field in ('created_at', 'published_at', 'scheduled_at', 'approved_at'):
                for value in ('', '2026-11-06T17:00:00', 'not-a-timestamp',
                              '2026-99-99T17:00:00Z', '2026-11-06T17:00:00+99:99', 0):
                    bad = expected_feed(fid); bad[field] = value
                    r = call(sid, fid, exp_story, bad)
                    refusals.append(('invalid_snapshot_timestamp', r))
                    bad_story = dict(exp_story); bad_story[field] = value
                    r = call(sid, fid, bad_story, expected_feed(fid))
                    refusals.append(('invalid_snapshot_timestamp', r))
            sql("set timezone='UTC'")
            # Keep JSON types intact, rather than comparing ->> text aliases.
            bad = expected_feed(fid); bad['mentions'] = '[]'
            refusals.append(('feed_changed_or_pairing_mismatch',
                             call(sid, fid, exp_story, bad)))
            for field in ('thumbnail_url', 'approval_digest'):
                bad_story = dict(exp_story); bad_story.pop(field)
                refusals.append(('stale_story_preimage',
                                 call(sid, fid, bad_story, expected_feed(fid))))
            sql('alter table content_calendar add column added_metadata text')
            refusals.append(('story_key_mismatch',
                             call(sid, fid, exp_story, expected_feed(fid))))
            sql('alter table content_calendar drop column added_metadata')
            # Schedule shifts: feed off noon, story off 12:15.
            sql("update content_calendar set scheduled_at="
                "((post_date::text || ' 11:00')::timestamp"
                " at time zone 'America/New_York') where id=%s", (fid,))
            r = call(sid, fid, exp_story, expected_feed(fid))
            refusals.append(('noncanonical_schedule', r))
            seed_feed(I)
            bad_sched = sql('select (scheduled_at + cast(%s as interval))::text'
                            ' from content_calendar where id=%s',
                            ('20 minutes', fid))[0][0]
            r = call(sid, fid, exp_story, expected_feed(fid), sched=bad_sched)
            refusals.append(('noncanonical_schedule', r))
            # Competing slot-2 Story refuses; slot-0/1 Stories coexist (proved
            # positively in the success scenario below).
            rival = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
            sql("insert into content_calendar(id,gym_id,account,post_date,pillar,"
                "format,caption,image_url,status,slot_index,variant_status) values"
                "(%s,'lasso','instagram',%s,'summit','story','','https://example.test/r.png',"
                "'pending',2,'active')", (rival, manifest[I][2]))
            r = call(sid, fid, exp_story, expected_feed(fid))
            refusals.append(('competing_story_present', r))
            sql('delete from content_calendar where id=%s', (rival,))
            changed_feed = expected_feed(fid); changed_feed['caption'] = 'tampered'
            r = call(sid, fid, exp_story, changed_feed)
            refusals.append(('feed_changed_or_pairing_mismatch', r))
            r = call(sid, fid, exp_story, expected_feed(fid), brain={'snapshot': 'old'})
            refusals.append(('feed_review_missing', r))
            r = call(sid, fid, exp_story, expected_feed(fid), policy='policy-old')
            refusals.append(('feed_review_missing', r))
            sql("delete from echo_infographic_artifacts where image_url=%s", (art[I][0],))
            r = call(sid, fid, exp_story, expected_feed(fid))
            refusals.append(('reviewed_source_mismatch', r))
            sql('delete from content_calendar where id=%s', (fid,))
            for want, got in refusals:
                assert got.get('result') == 'conflict' and got.get('reason') == want, \
                    f'expected refusal {want}, got {got}'
            assert sql('select count(*) from lasso_standalone_story_conversion_audit_20261008'
                       ' where story_id=%s', (sid,))[0][0] == 0
            row = sql('select slot_index, logical_post_id, scheduled_at'
                      ' from content_calendar where id=%s', (sid,))[0]
            assert row == (None, None, None)

            # ---- success + replay on story index 0 (Nov 1), with a managed
            # slot-0 Story legitimately coexisting on the same account/date ----
            I = 0
            fid = seed_feed(I)
            sid = seed_story(I)
            exp_story = manifest[I][3]
            slot0_story = 'aaaaaaaa-0000-4000-8000-0000000000aa'
            slot0_feed = 'aaaaaaaa-0000-4000-8000-0000000000bb'
            sql("insert into content_calendar(id,gym_id,account,post_date,pillar,"
                "format,caption,image_url,status,slot_index,variant_status) values"
                "(%s,'lasso','instagram',%s,'summit','story','','https://example.test/s0.png',"
                "'pending',0,'active')", (slot0_story, manifest[I][2]))
            sql('insert into lasso_managed_paired_stories values(%s,%s)',
                (slot0_story, slot0_feed))
            slot1_story = 'aaaaaaaa-0001-4000-8000-0000000000aa'
            sql("insert into content_calendar(id,gym_id,account,post_date,pillar,"
                "format,caption,image_url,status,slot_index,variant_status) values"
                "(%s,'lasso','instagram',%s,'doctrine','story','','https://example.test/s1.png',"
                "'pending',1,'active')", (slot1_story, manifest[I][2]))
            exp_feed = expected_feed(fid)
            assert set(exp_feed) == set(COLUMNS) and len(COLUMNS) == 42
            before = sql('select id::text, created_at::text from content_calendar'
                         ' where id=%s', (sid,))[0]
            r = call(sid, fid, exp_story, exp_feed)
            assert r.get('result') == 'converted' and r.get('feed_id') == fid, r
            row = sql('select slot_index, caption, pillar, image_url, source_media_url,'
                      ' media_not_ready_reason, logical_post_id,'
                      ' (scheduled_at at time zone %s)::time,'
                      ' scheduled_at = (select scheduled_at + cast(%s as interval)'
                      '   from content_calendar where id=%s)'
                      ' from content_calendar where id=%s',
                      ('America/New_York', '15 minutes', fid, sid))[0]
            assert row == (2, '', 'summit', art[I][0], art[I][0],
                           'paired_feed_not_ready', None, datetime.time(12, 15), True), row
            after = sql('select id::text, created_at::text from content_calendar'
                        ' where id=%s', (sid,))[0]
            assert before == after
            assert sql('select slot_index, image_url from content_calendar where id=%s',
                       (slot1_story,))[0] == (1, 'https://example.test/s1.png')
            audit = sql('select feed_id::text, new_image_url, new_image_sha256,'
                        ' hold_reason, before_story->>%s, logical_post_id'
                        ' from lasso_standalone_story_conversion_audit_20261008'
                        ' where story_id=%s', ('image_url', sid))[0]
            assert audit[0] == fid and audit[1] == art[I][0] and audit[2] == art[I][1]
            assert audit[3] == 'paired_feed_not_ready' and audit[5] is None
            assert 'r2.dev' in audit[4]  # old standalone artwork preserved
            assert sql('select feed_id::text from lasso_managed_paired_stories'
                       ' where story_id=%s', (sid,))[0][0] == fid
            assert sql('select public.lasso_story_current_source(%s)', (sid,))[0][0] is True
            # Exact replay reuses the artifact/link only.
            r = call(sid, fid, exp_story, exp_feed)
            assert r.get('result') == 'idempotent' and r.get('feed_id') == fid, r
            assert sql('select count(*) from lasso_standalone_story_conversion_audit_20261008'
                       ' where story_id=%s', (sid,))[0][0] == 1
            assert sql('select count(*) from lasso_managed_paired_stories'
                       ' where story_id=%s', (sid,))[0][0] == 1
            # Immutable audit captures the actual entire post-update Story.
            assert sql('select a.after_story = to_jsonb(c) from '
                       'lasso_standalone_story_conversion_audit_20261008 a '
                       'join content_calendar c on c.id=a.story_id where c.id=%s',
                       (sid,))[0][0] is True
            # Every omitted replay state field must refuse changes, on BOTH rows.
            mutations = dict(approval_values, publish_claim_token=str(uuid.uuid4()),
                             publish_reservation_day='2026-11-01',
                             media_not_ready_reason='changed_hold',
                             thumbnail_url='https://example.test/changed.png',
                             experiment_label='changed metadata',
                             reject_reason='changed reason')
            for target in (sid, fid):
                for field, value in mutations.items():
                    old = sql(f'select {field} from content_calendar where id=%s', (target,))[0][0]
                    sql(f'update content_calendar set {field}=%s where id=%s', (value, target))
                    current_story = expected_feed(sid)
                    current_feed = expected_feed(fid)
                    r = call(sid, fid, exp_story, exp_feed)
                    assert r == {'result': 'conflict', 'reason': 'replay_changed'}, (field, target, r)
                    assert expected_feed(sid) == current_story and expected_feed(fid) == current_feed
                    sql(f'update content_calendar set {field}=%s where id=%s', (old, target))
            # All metadata remains typed; an array JSON value cannot impersonate text.
            for key_change in ('missing', 'extra'):
                bad = dict(exp_feed)
                if key_change == 'missing': bad.pop('thumbnail_url')
                else: bad['unknown_schema_field'] = None
                r = call(sid, fid, exp_story, bad)
                assert r.get('result') == 'conflict', r
            sql('alter table content_calendar add column future_metadata text')
            r = call(sid, fid, exp_story, exp_feed)
            assert r == {'result': 'conflict', 'reason': 'replay_changed'}, r
            sql('alter table content_calendar drop column future_metadata')
            # Equivalent explicitly aware strings and non-UTC readback remain valid.
            sql("set timezone='America/Los_Angeles'")
            equivalent_story = dict(exp_story)
            equivalent_story['created_at'] = datetime.datetime.fromisoformat(
                exp_story['created_at']).astimezone(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
            equivalent_feed = dict(exp_feed)
            equivalent_feed['created_at'] = datetime.datetime.fromisoformat(
                exp_feed['created_at']).astimezone(datetime.timezone(datetime.timedelta(hours=-5))).isoformat()
            r = call(sid, fid, equivalent_story, equivalent_feed)
            assert r.get('result') == 'idempotent', r
            sql("set timezone='UTC'")
            # Replaying with a feed review receipt removed also fails closed.
            sql("update echo_infographic_artifacts set evidence=evidence-'review_response_id' "
                'where image_url=%s', (manifest[I][4]['image_url'],))
            r = call(sid, fid, exp_story, exp_feed)
            assert r.get('result') == 'conflict' and r.get('reason') == 'feed_review_missing', r
            seed_feed(I)
            # Story PASS evidence must also retain a real review receipt on replay.
            for value in (None, '', '   '):
                sql("update echo_infographic_artifacts set evidence="
                    "jsonb_set(evidence,'{review_response_id}',%s::jsonb) where image_url=%s",
                    (json.dumps(value), art[I][0]))
                r = call(sid, fid, exp_story, exp_feed)
                assert r.get('result') == 'conflict' and r.get('reason') == 'reviewed_source_mismatch', r
                seed_feed(I)
            # Changed payload, revoked autonomy and broken source all conflict.
            r = call(sid, fid, exp_story, exp_feed, sha=SHA2)
            assert r.get('result') == 'conflict' and r.get('reason') == 'replay_changed', r
            sql("update gyms set autonomous=false where gym_id='lasso'")
            r = call(sid, fid, exp_story, exp_feed)
            assert r.get('result') == 'conflict' and r.get('reason') == 'autonomy_not_confirmed', r
            sql("update gyms set autonomous=true where gym_id='lasso'")
            sql("delete from echo_infographic_artifacts where image_url=%s", (art[I][0],))
            r = call(sid, fid, exp_story, exp_feed)
            assert r.get('result') == 'conflict' and r.get('reason') == 'replay_changed', r
            seed_feed(I)
            r = call(sid, fid, exp_story, exp_feed)
            assert r.get('result') == 'idempotent', r
            # Replay under race: a concurrent row lock blocks; no silent success.
            blocker = psycopg.connect(dsn+' user=postgres')
            racer = psycopg.connect(dsn+' user=postgres', autocommit=True)
            blocker.execute('begin')
            blocker.execute('update content_calendar set reject_reason=reject_reason'
                            ' where id=%s', (sid,))
            racer.execute("set lock_timeout='1500ms'")
            try:
                call(sid, fid, exp_story, exp_feed, conn=racer)
                raise AssertionError('replay bypassed row lock')
            except psycopg.errors.LockNotAvailable:
                pass
            blocker.rollback()
            r = call(sid, fid, exp_story, exp_feed, conn=racer)
            assert r.get('result') == 'idempotent', r
            racer.close()
            blocker.close()

            # ---- race on story index 1 (Nov 2): loser waits/fails, row intact ----
            I = 1
            fid = seed_feed(I)
            sid = seed_story(I)
            exp_story = manifest[I][3]
            exp_feed = expected_feed(fid)
            blocker = psycopg.connect(dsn+' user=postgres')
            racer = psycopg.connect(dsn+' user=postgres', autocommit=True)
            blocker.execute('begin')
            blocker.execute('update content_calendar set reject_reason=reject_reason'
                            ' where id=%s', (sid,))
            racer.execute("set lock_timeout='1500ms'")
            try:
                call(sid, fid, exp_story, exp_feed, conn=racer)
                raise AssertionError('concurrent conversion bypassed row lock')
            except psycopg.errors.LockNotAvailable:
                pass
            blocker.rollback()
            r = call(sid, fid, exp_story, exp_feed, conn=racer)
            assert r.get('result') == 'converted', r
            racer.close()
            blocker.close()

            # ---- rollback on story index 3 (Nov 5): post-update verifier
            # failure raises and leaves no partial row, audit or link ----
            I = 3
            fid = seed_feed(I)
            sid = seed_story(I)
            exp_story = manifest[I][3]
            exp_feed = expected_feed(fid)
            ghost = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
            sql("insert into content_calendar(id,gym_id,account,post_date,pillar,"
                "format,caption,image_url,status,scheduled_at,slot_index) values"
                "(%s,'lasso','instagram',%s,'summit','feed','ghost',%s,'pending',"
                "now(),2)", (ghost, manifest[I][2], manifest[I][4]['image_url']))
            worker = psycopg.connect(dsn+' user=postgres')
            try:
                with worker.cursor() as cur:
                    cur.execute('set role service_role')
                    cur.execute('select convert_lasso_standalone_story_20261008'
                                '(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                (sid, fid, json.dumps(exp_story), json.dumps(exp_feed),
                                 art[I][0], art[I][1], 'lasso_ig',
                                 hashlib.sha256(manifest[I][4]['caption'].encode()).hexdigest(),
                                 POLICY, json.dumps(BRAIN),
                                 sql('select (scheduled_at + cast(%s as interval))::text'
                                     ' from content_calendar where id=%s',
                                     ('15 minutes', fid))[0][0]))
                raise AssertionError('ambiguous canonical feed passed verification')
            except psycopg.errors.CheckViolation:
                worker.rollback()
            row = sql('select slot_index, image_url, scheduled_at'
                      ' from content_calendar where id=%s', (sid,))[0]
            assert row == (None, manifest[I][3]['image_url'], None), row
            assert sql('select count(*) from lasso_standalone_story_conversion_audit_20261008'
                       ' where story_id=%s', (sid,))[0][0] == 0
            assert sql('select count(*) from lasso_managed_paired_stories'
                       ' where story_id=%s', (sid,))[0][0] == 0
            sql('delete from content_calendar where id=%s', (ghost,))
            worker.close()

            # ---- grants and immutability ----
            for role in ('anon', 'authenticated'):
                sql('set role ' + role)
                try:
                    sql('select convert_lasso_standalone_story_20261008'
                        '(null,null,null,null,null,null,null,null,null,null,null)')
                    raise AssertionError(f'{role} executed conversion RPC')
                except psycopg.errors.InsufficientPrivilege:
                    pass
                finally:
                    sql('reset role')
            for role in ('service_role', 'anon', 'authenticated'):
                sql('set role ' + role)
                for table in ('lasso_standalone_story_conversion_manifest_20261008',
                              'lasso_standalone_story_conversion_audit_20261008'):
                    try:
                        sql('select count(*) from ' + table)
                        raise AssertionError(f'{role} read {table}')
                    except psycopg.errors.InsufficientPrivilege:
                        pass
                sql('reset role')
            for table in ('lasso_standalone_story_conversion_manifest_20261008',
                          'lasso_standalone_story_conversion_audit_20261008'):
                for op in ('truncate ' + table,
                           'update ' + table + ' set post_date=post_date'
                           if 'manifest' in table else
                           'update ' + table + ' set feed_id=feed_id',
                           'delete from ' + table):
                    try:
                        sql(op)
                        raise AssertionError(f'mutable via: {op}')
                    except psycopg.errors.CheckViolation:
                        pass
            admin.close()
            print(f'Executed {calls["count"]} conversion RPC probes; {len(refusals)} fresh refusal cases')
            print('PASS PG17: 42-field after-state replay CAS, aware timestamp snapshots,'
                  ' approval/review receipt refusals, bound feed UUIDs, readiness + noon'
                  ' schedule, ordered replay (race/autonomy/source), slot-0/1'
                  ' coexistence, rollback, immutable manifest/audit, least privilege')
        finally:
            if started:
                subprocess.run([str(pg/'pg_ctl'), '-D', str(data), '-m', 'immediate',
                                '-w', 'stop'], check=True, capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
