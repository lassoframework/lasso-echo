"""Real PG17 atomic feed-preparation tests for the five evidenced existing
November LASSO Summit feeds. Disposable local socket, no live writes. Run
directly: python tests/test_lasso_standalone_feed_preparation_pg.py
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
POLICY = 'lasso-astra-2026-09-14-punctuation-v1'
BRAIN = {'snapshot': 'nov-brain-1'}
RECEIPT = 'resp-native-1'
CATALOG = 'f6424e0531e23cc2b1f0a9a6d990ba1698c713ccbb479bf649576605697daf3f'
NOV5_OLD = ('Seats are selling fast.\n\nGet your tickets now. The calendar is '
            'about to turn. Give your gym a plan with enough detail to use on '
            'Monday. Register at lassoframework.com/summit')
# FINAL reviewed corrective candidate (nov5-approved-corrective-caption.json,
# sha256 bed97dac...). The earlier dated-catalog candidate is strictly BLOCKED.
NOV5_NEW = ('Give your gym a plan with enough detail to use on Monday.\n\n'
            'Build your complete 2027 growth playbook at the LASSO Growth '
            'Summit on November 7 and 8 at Virgin Hotels Nashville.\n\n'
            'Register at lassoframework.com/summit')
NOV5_BLOCKED = ('Two days until the LASSO Growth Summit. Make Nashville the '
                'place where your 2027 growth playbook gets built. Register '
                'at lassoframework.com/summit')


def main():
    import psycopg
    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    assert shutil.disk_usage('/tmp').free > 5*1024**3
    with tempfile.TemporaryDirectory(prefix='lasso_feed_prep_pg_', dir='/tmp') as tmp:
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
                'pillar text, format text, caption text, image_url text,'
                'status text not null,'
                'created_at timestamptz not null, published_at timestamptz, late_post_id text,'
                'scheduled_at timestamptz, thumbnail_url text, gbp_topic_type text,'
                'gbp_cta_type text, gbp_cta_url text, gbp_event jsonb, gbp_offer jsonb,'
                'gbp_location_id text, reject_reason text, source_media_url text,'
                'mentions jsonb not null, hook_family text, ask_type text, time_slot text,'
                'caption_len_band text, has_member_face boolean, experiment_label text,'
                'slot_index integer, source_media_asset_id text, media_not_ready_reason text,'
                'event_id text, variant_of uuid, variant_status text not null,'
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
                         'lasso_standalone_feed_preparation_20261008.sql'):
                sql((ROOT/'migrations'/name).read_text())

            manifest = sql('select feed_id::text, story_id::text, post_date::text,'
                           ' allowed_caption, allowed_caption_sha256, action,'
                           ' expected_feed, expected_story'
                           ' from lasso_feed_preparation_manifest_20261008'
                           ' order by post_date')
            assert len(manifest) == 5
            # Manifest self-consistency: every allowed caption hashes to its
            # recorded SHA; preserve actions keep the old caption; only Nov 5
            # replaces.
            for fid, sid, day, cap, cap_sha, action, ef, es in manifest:
                assert hashlib.sha256(cap.encode()).hexdigest() == cap_sha
                old_sha = hashlib.sha256(ef['caption'].encode()).hexdigest()
                if action == 'preserve_exact':
                    assert cap == ef['caption'] and cap_sha == old_sha
                else:
                    assert (fid, day) == ('96ef0beb-bd99-5358-bb75-820085ecc92e',
                                          '2026-11-05')
                    assert ef['caption'] == NOV5_OLD and cap == NOV5_NEW
                    assert cap != ef['caption'] and cap_sha != old_sha
                assert set(ef) == set(COLUMNS) and set(es) == set(COLUMNS)
            assert 150 <= len(NOV5_NEW) <= 500  # corrective band is 'mid'
            assert hashlib.sha256(NOV5_NEW.encode()).hexdigest() == \
                'bed97dac7b41b5e3409761dbe4cebe797211ac127376c35c5e5bba1aa4fdc7ed'
            art = {}

            def seed_feed(i):
                fid = manifest[i][0]
                sql('delete from content_calendar where id=%s', (fid,))
                sql('insert into content_calendar'
                    ' select (jsonb_populate_record(null::content_calendar, expected_feed)).*'
                    ' from lasso_feed_preparation_manifest_20261008'
                    ' where feed_id=%s', (fid,))
                return fid

            def seed_story(i):
                sid = manifest[i][1]
                sql('delete from content_calendar where id=%s', (sid,))
                sql('insert into content_calendar'
                    ' select (jsonb_populate_record(null::content_calendar, expected_story)).*'
                    ' from lasso_feed_preparation_manifest_20261008'
                    ' where feed_id=%s', (manifest[i][0],))
                return sid

            def seed_artifact(i, url=None, sha=None, caption=None,
                              policy=POLICY, brain=BRAIN, tenant='lasso_ig',
                              receipt=RECEIPT, grade='PASS'):
                fid = manifest[i][0]
                url = url or f'https://example.test/feed_new_{i}.png'
                sha = sha or hashlib.sha256(f'newimg-{i}'.encode()).hexdigest()
                caption = manifest[i][3] if caption is None else caption
                cap_hash = hashlib.sha256(caption.encode()).hexdigest()
                art[i] = (url, sha, cap_hash)
                sql('delete from echo_infographic_artifacts where image_url=%s', (url,))
                sql('insert into echo_infographic_artifacts values(%s,%s,%s,%s,%s)',
                    (tenant, url, sha,
                     json.dumps({'source_id': f'content_calendar:{fid}:caption',
                                 'source_hash': cap_hash}),
                     json.dumps({'grade_status': grade, 'image_sha256': sha,
                                 'policy_version': policy,
                                 'brain_snapshot': brain,
                                 'review_response_id': receipt,
                                 'aspect': '4:5', 'pixels': '1080x1350',
                                 'verified_dimensions': {'width': 1080,
                                     'height': 1350, 'image_sha256': sha}})))
                return art[i]

            def current_book():
                return sql("select coalesce(jsonb_agg(to_jsonb(r) order by r.id),"
                           " '[]'::jsonb) from content_calendar r"
                           " where r.gym_id='lasso' and r.post_date between"
                           " date '2026-10-08' and date '2026-11-08'")[0][0]

            def call(i, caption=None, cap_sha=None, url=None, sha=None,
                     tenant='lasso_ig', policy=POLICY, brain=BRAIN,
                     receipt=RECEIPT, exp_feed=None, exp_story=None,
                     feed_id=None, story_id=None, conn=None,
                     book=None, catalog=CATALOG):
                caption = manifest[i][3] if caption is None else caption
                cap_sha = cap_sha or hashlib.sha256(caption.encode()).hexdigest()
                url = url or art[i][0]
                sha = sha or art[i][1]
                if book is None:
                    book = current_book()
                if not isinstance(book, str):
                    book = json.dumps(book)
                args = (feed_id or manifest[i][0], story_id or manifest[i][1],
                        json.dumps(exp_feed or manifest[i][6]),
                        json.dumps(exp_story or manifest[i][7]),
                        caption, cap_sha, url, sha, tenant, policy,
                        json.dumps(brain), receipt, book, catalog)
                q = ('select prepare_lasso_summit_feed_20261008'
                     '(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)')
                c = conn or admin
                with c.cursor() as cur:
                    cur.execute('set role service_role')
                    cur.execute(q, args)
                    out = cur.fetchone()[0]
                    cur.execute('reset role')
                return out

            # ---- refusal battery on feed index 4 (Nov 6, never prepared) ----
            I = 4
            fid = seed_feed(I)
            sid = seed_story(I)
            seed_artifact(I)
            refusals = []

            r = call(I, feed_id=str(uuid.uuid4()))
            refusals.append(('unknown_feed', r))
            other_story = manifest[0][1]
            r = call(I, story_id=other_story)
            refusals.append(('story_mismatch', r))
            r = call(I, caption='Some other caption entirely.')
            refusals.append(('caption_not_allowed', r))
            r = call(I, cap_sha='z'*64)
            refusals.append(('invalid_input', r))
            # Catalog SHA is literal manifest proof, not caller-negotiable.
            r = call(I, catalog='9'*64)
            refusals.append(('catalog_mismatch', r))
            # Protected-book CAS: stale snapshot, drifted row, naive/empty
            # timestamp element and stringified-JSON coercion all refuse.
            stale_book = current_book()
            sql("update content_calendar set reject_reason='drift' where id=%s", (sid,))
            r = call(I, book=stale_book)
            refusals.append(('book_mismatch', r))
            r = call(I)
            refusals.append(('story_changed_or_claimed', r))
            seed_story(I)
            naive_book = current_book()
            naive_book[0]['created_at'] = '2026-11-01 12:00:00'
            r = call(I, book=naive_book)
            refusals.append(('invalid_input', r))
            empty_ts_book = current_book()
            empty_ts_book[0]['created_at'] = ''
            r = call(I, book=empty_ts_book)
            refusals.append(('invalid_input', r))
            r = call(I, book=json.dumps('stringified-book'))
            refusals.append(('invalid_input', r))
            r = call(I, book='[]')
            refusals.append(('book_mismatch', r))
            missing_row_book = [b for b in current_book() if b['id'] != sid]
            r = call(I, book=missing_row_book)
            refusals.append(('book_mismatch', r))
            # Stringified-JSON and naive timestamps in the row preimages.
            r = call(I, exp_feed=json.dumps(manifest[I][6]))
            refusals.append(('invalid_input', r))
            naive_feed = dict(manifest[I][6])
            naive_feed['created_at'] = '2026-09-23 20:03:22'
            r = call(I, exp_feed=naive_feed)
            refusals.append(('stale_feed_preimage', r))
            empty_ts_feed = dict(manifest[I][6])
            empty_ts_feed['scheduled_at'] = ''
            r = call(I, exp_feed=empty_ts_feed)
            refusals.append(('stale_feed_preimage', r))
            stale = dict(manifest[I][6]); stale['caption'] = 'edited'
            r = call(I, exp_feed=stale)
            refusals.append(('stale_feed_preimage', r))
            missing = {k: v for k, v in manifest[I][6].items() if k != 'caption'}
            r = call(I, exp_feed=missing)
            refusals.append(('stale_feed_preimage', r))
            extra = dict(manifest[I][6]); extra['managed_links'] = None
            r = call(I, exp_feed=extra)
            refusals.append(('stale_feed_preimage', r))
            sql("update gyms set autonomous=false where gym_id='lasso'")
            r = call(I)
            refusals.append(('autonomy_not_confirmed', r))
            sql("update gyms set autonomous=true where gym_id='lasso'")
            # Feed gates.
            sql("update content_calendar set publish_claim_token=%s where id=%s",
                (str(uuid.uuid4()), fid))
            r = call(I)
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            sql("update content_calendar set publish_reservation_day=%s where id=%s",
                (manifest[I][2], fid))
            r = call(I)
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            sql("update content_calendar set approval_digest='abc',"
                " approval_kind='manual', approved_by='op', approved_at=now()"
                " where id=%s", (fid,))
            r = call(I)
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            sql("update content_calendar set status='published',"
                " published_at=now(), late_post_id='late-1' where id=%s", (fid,))
            r = call(I)
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            sql("update content_calendar set media_not_ready_reason=null where id=%s",
                (fid,))
            r = call(I)
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            sql("update content_calendar set scheduled_at=now() where id=%s", (fid,))
            r = call(I)
            refusals.append(('feed_not_ready', r))
            seed_feed(I)
            # Story gates: claim, registry link, occupied slot.
            sql("update content_calendar set publish_claim_token=%s where id=%s",
                (str(uuid.uuid4()), sid))
            r = call(I)
            refusals.append(('story_not_eligible', r))
            seed_story(I)
            sql('insert into lasso_managed_paired_stories values(%s,%s)', (sid, fid))
            r = call(I)
            refusals.append(('registry_link_present', r))
            sql('delete from lasso_managed_paired_stories where story_id=%s', (sid,))
            sql('update content_calendar set slot_index=2 where id=%s', (sid,))
            r = call(I)
            refusals.append(('story_not_eligible', r))
            seed_story(I)
            # Row drift after preimage: CAS refusal.
            sql("update content_calendar set hook_family='question' where id=%s", (fid,))
            r = call(I)
            refusals.append(('feed_changed_or_claimed', r))
            seed_feed(I)
            sql("update content_calendar set caption='tampered' where id=%s", (sid,))
            r = call(I)
            refusals.append(('story_changed_or_claimed', r))
            seed_story(I)
            # Duplicate slot-2 occupant.
            rival = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
            sql("insert into content_calendar(id,gym_id,account,post_date,pillar,"
                "format,caption,image_url,status,created_at,mentions,slot_index,variant_status) values"
                "(%s,'lasso','instagram',%s,'summit','feed','x','https://example.test/r.png',"
                "'pending',now(),'[]',2,'active')", (rival, manifest[I][2]))
            r = call(I)
            refusals.append(('ambiguous_feed_slot', r))
            sql('delete from content_calendar where id=%s', (rival,))
            # Artifact gates: wrong caption binding, policy, Brain, digest,
            # tenant, receipt, grade.
            r = call(I, caption=manifest[I][3], sha='zz')
            refusals.append(('invalid_input', r))
            sql("update echo_infographic_artifacts set evidence ="
                " jsonb_set(evidence,'{grade_status}','\"FAIL\"') where image_url=%s",
                (art[I][0],))
            r = call(I)
            refusals.append(('image_review_missing', r))
            seed_artifact(I)
            r = call(I, policy='policy-old')
            refusals.append(('image_review_missing', r))
            r = call(I, brain={'snapshot': 'old'})
            refusals.append(('image_review_missing', r))
            r = call(I, receipt='resp-other')
            refusals.append(('image_review_missing', r))
            seed_artifact(I, tenant='lasso_fb')
            r = call(I)
            refusals.append(('image_review_missing', r))
            seed_artifact(I)
            # Artifact bound to the OLD caption hash is not proof for the new
            # image source.
            sql("update echo_infographic_artifacts set source_identity="
                " jsonb_set(source_identity,'{source_hash}',%s) where image_url=%s",
                (json.dumps('f'*64), art[I][0]))
            r = call(I)
            refusals.append(('image_review_missing', r))
            seed_artifact(I)
            # Old image URL reused (not a genuinely new visual).
            r = call(I, url=manifest[I][6]['image_url'])
            refusals.append(('feed_not_ready', r))
            # New image already bound to another active LASSO row.
            reuser = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
            sql("insert into content_calendar(id,gym_id,account,post_date,pillar,"
                "format,caption,image_url,status,created_at,mentions,slot_index,"
                "variant_status) values"
                "(%s,'lasso','instagram',%s,'summit','feed','x',%s,"
                "'pending',now(),'[]',1,'active')",
                (reuser, manifest[I][2], art[I][0]))
            r = call(I)
            refusals.append(('image_already_in_use', r))
            sql('delete from content_calendar where id=%s', (reuser,))
            # Declared-only pixels (no measured dimensions), blank review id
            # and blank policy in evidence all refuse.
            sql("update echo_infographic_artifacts set evidence ="
                " evidence - 'verified_dimensions' where image_url=%s", (art[I][0],))
            r = call(I)
            refusals.append(('image_review_missing', r))
            seed_artifact(I)
            sql("update echo_infographic_artifacts set evidence ="
                " jsonb_set(evidence,'{pixels}','\"800x1000\"') where image_url=%s",
                (art[I][0],))
            r = call(I)
            refusals.append(('image_review_missing', r))
            seed_artifact(I)
            # Exact attested dimensions only: width/height as strings, zero,
            # 9:16 size, wrong aspect and wrong/missing nested digest refuse.
            for patch in (
                "jsonb_set(evidence,'{verified_dimensions,width}','\"1080\"')",
                "jsonb_set(evidence,'{verified_dimensions,height}','\"1350\"')",
                "jsonb_set(evidence,'{verified_dimensions,width}','0')",
                "jsonb_set(evidence,'{verified_dimensions,height}','1920')",
                "jsonb_set(evidence,'{aspect}','\"9:16\"')",
                "jsonb_set(evidence,'{verified_dimensions,image_sha256}','\""
                + '0'*64 + "\"')",
                "jsonb_set(evidence,'{verified_dimensions}','{}')",
            ):
                sql('update echo_infographic_artifacts set evidence = ' + patch +
                    ' where image_url=%s', (art[I][0],))
                r = call(I)
                refusals.append(('image_review_missing', r))
                seed_artifact(I)
            seed_artifact(I, receipt='')
            r = call(I, receipt='')
            refusals.append(('invalid_input', r))
            seed_artifact(I, receipt='resp-native-1')
            sql("update echo_infographic_artifacts set evidence ="
                " jsonb_set(evidence,'{review_response_id}','\"\"') where image_url=%s",
                (art[I][0],))
            r = call(I)
            refusals.append(('image_review_missing', r))
            seed_artifact(I)
            # Whitespace-only review receipt refuses on BOTH input and
            # evidence.
            r = call(I, receipt='   ')
            refusals.append(('invalid_input', r))
            sql("update echo_infographic_artifacts set evidence ="
                " jsonb_set(evidence,'{review_response_id}','\"  \"') where image_url=%s",
                (art[I][0],))
            r = call(I)
            refusals.append(('image_review_missing', r))
            seed_artifact(I)
            sql("update echo_infographic_artifacts set evidence ="
                " jsonb_set(evidence,'{policy_version}','\"\"') where image_url=%s",
                (art[I][0],))
            r = call(I)
            refusals.append(('image_review_missing', r))
            seed_artifact(I)
            for want, got in refusals:
                assert got.get('result') == 'conflict' and got.get('reason') == want, \
                    f'expected refusal {want}, got {got}'
            assert sql('select count(*) from lasso_feed_preparation_audit_20261008'
                       ' where feed_id=%s', (fid,))[0][0] == 0
            row = sql('select caption, media_not_ready_reason, scheduled_at'
                      ' from content_calendar where id=%s', (fid,))[0]
            assert row == (manifest[I][6]['caption'],
                           'cross_date_media_repeat_needs_new_visual', None)

            # ---- success: Nov 5 correction (index 3) ----
            I = 3
            fid = seed_feed(I)
            sid = seed_story(I)
            seed_artifact(I)
            # The unsafe original is refused as a candidate even though it is
            # the exact corrective target preimage.
            r = call(I, caption=NOV5_OLD)
            assert r.get('result') == 'conflict' and r.get('reason') == 'caption_not_allowed', r
            # The superseded dated-catalog candidate (sha e5e72f67...) is
            # strictly BLOCKED even though it was once reviewed.
            r = call(I, caption=NOV5_BLOCKED)
            assert r.get('result') == 'conflict' and r.get('reason') == 'caption_not_allowed', r
            before_story = sql('select to_jsonb(r) from content_calendar r where id=%s',
                               (sid,))[0][0]
            other_caps = dict(sql('select id::text, caption from content_calendar'
                                  ' where id <> %s and id <> %s', (fid, sid)))
            r = call(I)
            assert r.get('result') == 'prepared' and r.get('caption_changed') is True, r
            row = sql('select caption, image_url, source_media_url, thumbnail_url,'
                      ' source_media_asset_id, media_not_ready_reason, hook_family,'
                      ' ask_type, caption_len_band, pillar, status, slot_index,'
                      ' (scheduled_at at time zone %s)::time,'
                      ' (scheduled_at at time zone %s)::date,'
                      ' scheduled_at > now(), time_slot, logical_post_id'
                      ' from content_calendar where id=%s',
                      ('America/New_York', 'America/New_York', fid))[0]
            assert row == (NOV5_NEW, art[I][0], art[I][0], None, None, None,
                           'bold_claim', 'none', 'mid', 'summit', 'pending', 2,
                           datetime.time(12, 0), datetime.date(2026, 11, 5),
                           True, None, None), row
            # Story byte-identical; the other four captions provably unchanged.
            after_story = sql('select to_jsonb(r) from content_calendar r where id=%s',
                              (sid,))[0][0]
            assert json.dumps(before_story, sort_keys=True) == \
                json.dumps(after_story, sort_keys=True)
            for cid, cap in other_caps.items():
                now_cap = sql('select caption from content_calendar where id=%s',
                              (cid,))[0][0]
                assert now_cap == cap
            audit = sql('select caption_changed, old_caption, new_caption,'
                        ' new_image_sha256, review_response_id, review_policy,'
                        ' catalog_sha256, action'
                        ' from lasso_feed_preparation_audit_20261008 where feed_id=%s',
                        (fid,))[0]
            assert audit[0] is True and audit[1] == NOV5_OLD and audit[2] == NOV5_NEW
            assert audit[3] == art[I][1] and audit[4] == RECEIPT
            assert audit[5] == 'lasso-summit-source-correction-20261008-v1'
            assert audit[6] == 'f6424e0531e23cc2b1f0a9a6d990ba1698c713ccbb479bf649576605697daf3f'
            assert audit[7] == 'replace_unsupported_scarcity'
            # Exact replay after fresh locks/autonomy/source proof: idempotent.
            r = call(I)
            assert r.get('result') == 'idempotent' and r.get('caption_changed') is True, r
            assert sql('select count(*) from lasso_feed_preparation_audit_20261008'
                       ' where feed_id=%s', (fid,))[0][0] == 1
            # Audit carries the complete 42-column after-feed; replay compares
            # it against the full normalized current feed. Drift ANY single
            # after-field (even an allowlisted-null one) and replay refuses.
            after = sql('select after_feed from lasso_feed_preparation_audit_20261008'
                        ' where feed_id=%s', (fid,))[0][0]
            assert set(after) == set(COLUMNS)
            assert after['caption'] == NOV5_NEW and after['scheduled_at'] is not None
            assert sql('select count(*) from lasso_feed_preparation_audit_20261008'
                       " where feed_id=%s and after_feed->>'caption'=%s",
                       (fid, NOV5_NEW))[0][0] == 1
            for col, val, back in (('reject_reason', 'drift', None),
                                   ('thumbnail_url', 'https://x.test/t.png', None),
                                   ('approval_digest', 'abc', None),
                                   ('publish_claim_token', str(uuid.uuid4()), None),
                                   ('hook_family', 'question', 'bold_claim'),
                                   ('media_not_ready_reason', 'held', None)):
                sql('update content_calendar set ' + col + '=%s where id=%s', (val, fid))
                r = call(I)
                assert r.get('result') == 'conflict' and r.get('reason') == 'replay_changed', \
                    (col, r)
                sql('update content_calendar set ' + col + '=%s where id=%s', (back, fid))
                r = call(I)
                assert r.get('result') == 'idempotent', (col, r)
            # Replay re-runs the full artifact proof: degraded dimensions or a
            # whitespace-only evidence receipt refuse even with exact inputs.
            sql("update echo_infographic_artifacts set evidence ="
                " jsonb_set(evidence,'{verified_dimensions,width}','\"1080\"')"
                " where image_url=%s", (art[I][0],))
            r = call(I)
            assert r.get('result') == 'conflict' and r.get('reason') == 'replay_changed', r
            seed_artifact(I)
            sql("update echo_infographic_artifacts set evidence ="
                " jsonb_set(evidence,'{aspect}','\"9:16\"') where image_url=%s",
                (art[I][0],))
            r = call(I)
            assert r.get('result') == 'conflict' and r.get('reason') == 'replay_changed', r
            seed_artifact(I)
            sql("update echo_infographic_artifacts set evidence ="
                " jsonb_set(evidence,'{review_response_id}','\"  \"') where image_url=%s",
                (art[I][0],))
            r = call(I)
            assert r.get('result') == 'conflict' and r.get('reason') == 'replay_changed', r
            seed_artifact(I)
            r = call(I)
            assert r.get('result') == 'idempotent', r
            # Changed payload, revoked autonomy and broken source all conflict.
            r = call(I, sha='zz')
            assert r.get('result') == 'conflict' and r.get('reason') == 'invalid_input', r
            r = call(I, policy='policy-old')
            assert r.get('result') == 'conflict' and r.get('reason') == 'replay_changed', r
            sql("update gyms set autonomous=false where gym_id='lasso'")
            r = call(I)
            assert r.get('result') == 'conflict' and r.get('reason') == 'autonomy_not_confirmed', r
            sql("update gyms set autonomous=true where gym_id='lasso'")
            sql('delete from echo_infographic_artifacts where image_url=%s', (art[I][0],))
            r = call(I)
            assert r.get('result') == 'conflict' and r.get('reason') == 'replay_changed', r
            seed_artifact(I)
            r = call(I)
            assert r.get('result') == 'idempotent', r

            # ---- success: Nov 1 preservation (index 0) ----
            I = 0
            fid = seed_feed(I)
            sid = seed_story(I)
            seed_artifact(I)
            r = call(I)
            assert r.get('result') == 'prepared' and r.get('caption_changed') is False, r
            row = sql('select caption, hook_family, ask_type, caption_len_band,'
                      ' media_not_ready_reason, image_url, source_media_url,'
                      ' (scheduled_at at time zone %s)::time'
                      ' from content_calendar where id=%s',
                      ('America/New_York', fid))[0]
            assert row == (manifest[I][3], 'bold_claim', 'none', 'short', None,
                           art[I][0], art[I][0], datetime.time(12, 0)), row
            # time_slot display metadata preserved.
            assert sql('select time_slot from content_calendar where id=%s',
                       (fid,))[0][0] == 'evening'

            # ---- DST canonical noon for all five dates (real clock) ----
            for _, _, day, *_ in manifest:
                got = sql("select (((%s::date)::text || ' 12:00')::timestamp"
                          " at time zone 'America/New_York') at time zone 'UTC'",
                          (day,))[0][0]
                assert str(got) == day + ' 17:00:00', (day, got)  # post-DST EST noon

            # ---- concurrency race on feed index 1 (Nov 2) ----
            I = 1
            fid = seed_feed(I)
            sid = seed_story(I)
            seed_artifact(I)
            blocker = psycopg.connect(dsn+' user=postgres')
            racer = psycopg.connect(dsn+' user=postgres', autocommit=True)
            blocker.execute('begin')
            blocker.execute('update content_calendar set reject_reason=reject_reason'
                            ' where id=%s', (fid,))
            racer.execute("set lock_timeout='1500ms'")
            try:
                call(I, conn=racer)
                raise AssertionError('concurrent preparation bypassed row lock')
            except psycopg.errors.LockNotAvailable:
                pass
            blocker.rollback()
            r = call(I, conn=racer)
            assert r.get('result') == 'prepared', r
            racer.close()
            blocker.close()

            # ---- book write fence (feed index 2, Nov 4): phantom INSERT
            # blocks, ordinary reads pass, no lock-order deadlock ----
            I = 2
            fid = seed_feed(I)
            sid = seed_story(I)
            seed_artifact(I)
            other_gym = 'abababab-abab-4bab-8bab-abababababab'
            sql("insert into content_calendar(id,gym_id,status,created_at,mentions,"
                "variant_status) values(%s,'other','pending',now(),'[]','active')",
                (other_gym,))
            day_key = "'lasso|instagram|2026-11-04'"
            worker = psycopg.connect(dsn+' user=postgres')
            r = call(I, conn=worker)  # prepared but UNCOMMITTED: fence held
            assert r.get('result') == 'prepared', r
            racer = psycopg.connect(dsn+' user=postgres', autocommit=True)
            racer.execute("set lock_timeout='1500ms'")
            phantom = 'bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc'
            try:
                racer.execute("insert into content_calendar(id,gym_id,account,"
                              "post_date,pillar,format,caption,image_url,status,"
                              "created_at,mentions,slot_index,variant_status) values"
                              "(%s,'lasso','instagram',date '2026-11-03','summit','feed',"
                              "'x','https://example.test/phantom.png','pending',now(),"
                              "'[]',0,'active')", (phantom,))
                raise AssertionError('phantom INSERT bypassed the book fence')
            except psycopg.errors.LockNotAvailable:
                pass
            # Ordinary (non-row-locking) reads still pass under EXCLUSIVE.
            assert racer.execute('select gym_id from content_calendar where id=%s',
                                 (other_gym,)).fetchone()[0] == 'other'
            worker.commit()  # fence released
            racer.execute("insert into content_calendar(id,gym_id,account,"
                          "post_date,pillar,format,caption,image_url,status,"
                          "created_at,mentions,slot_index,variant_status) values"
                          "(%s,'lasso','instagram',date '2026-11-03','summit','feed',"
                          "'x','https://example.test/phantom.png','pending',now(),"
                          "'[]',0,'active')", (phantom,))
            racer.execute('delete from content_calendar where id=%s', (phantom,))
            racer.close()
            worker.close()

            # Insert committed BEFORE the fence: stale supplied book refuses,
            # no new audit row, no update.
            stale_book = current_book()
            sql("insert into content_calendar(id,gym_id,account,post_date,pillar,"
                "format,caption,image_url,status,created_at,mentions,slot_index,"
                "variant_status) values"
                "(%s,'lasso','instagram',date '2026-11-03','summit','feed','x',"
                "'https://example.test/phantom.png','pending',now(),'[]',0,'active')",
                (phantom,))
            r = call(I, book=stale_book)
            assert r.get('result') == 'conflict' and r.get('reason') == 'book_mismatch', r
            assert sql('select count(*) from lasso_feed_preparation_audit_20261008'
                       ' where feed_id=%s', (fid,))[0][0] == 1
            sql('delete from content_calendar where id=%s', (phantom,))
            r = call(I)
            assert r.get('result') == 'idempotent', r

            # Lock-order proof vs an ordinary account/day Story writer:
            # writer holding the day advisory first blocks the RPC BEFORE it
            # takes the table lock (no cycle).
            writer = psycopg.connect(dsn+' user=postgres')
            writer.execute('select pg_advisory_xact_lock(hashtextextended('
                           + day_key + ',0))')
            racer = psycopg.connect(dsn+' user=postgres', autocommit=True)
            racer.execute("set lock_timeout='1500ms'")
            try:
                call(I, conn=racer)
                raise AssertionError('RPC bypassed day advisory lock')
            except psycopg.errors.LockNotAvailable:
                pass
            writer.rollback()
            r = call(I, conn=racer)
            assert r.get('result') == 'idempotent', r
            racer.close()
            writer.close()
            # Forward order: RPC holds day advisory + fence; the day writer
            # blocks on the advisory (blocking, not deadlock), then proceeds.
            worker = psycopg.connect(dsn+' user=postgres')
            r = call(I, conn=worker)
            assert r.get('result') == 'idempotent', r
            writer = psycopg.connect(dsn+' user=postgres')
            writer.execute("set lock_timeout='1500ms'")
            try:
                writer.execute('select pg_advisory_xact_lock(hashtextextended('
                               + day_key + ',0))')
                raise AssertionError('day writer bypassed RPC advisory hold')
            except psycopg.errors.LockNotAvailable:
                writer.rollback()
            worker.commit()
            writer.execute('select pg_advisory_xact_lock(hashtextextended('
                           + day_key + ',0))')
            writer.execute('update content_calendar set reject_reason=reject_reason'
                           ' where id=%s', (fid,))
            writer.commit()
            writer.close()
            worker.close()
            # Non-LASSO row unaffected through all of it.
            assert sql('select status from content_calendar where id=%s',
                       (other_gym,))[0][0] == 'pending'
            sql('delete from content_calendar where id=%s', (other_gym,))

            # ---- grants and manifest/audit immutability ----
            for role in ('anon', 'authenticated'):
                sql('set role ' + role)
                try:
                    sql('select prepare_lasso_summit_feed_20261008'
                        '(null,null,null,null,null,null,null,null,null,null,null,null,null,null)')
                    raise AssertionError(f'{role} executed preparation RPC')
                except psycopg.errors.InsufficientPrivilege:
                    pass
                finally:
                    sql('reset role')
            for role in ('service_role', 'anon', 'authenticated'):
                sql('set role ' + role)
                for table in ('lasso_feed_preparation_manifest_20261008',
                              'lasso_feed_preparation_audit_20261008'):
                    try:
                        sql('select count(*) from ' + table)
                        raise AssertionError(f'{role} read {table}')
                    except psycopg.errors.InsufficientPrivilege:
                        pass
                sql('reset role')
            for table, col in (('lasso_feed_preparation_manifest_20261008', 'post_date'),
                               ('lasso_feed_preparation_audit_20261008', 'story_id')):
                for op in ('truncate ' + table,
                           'update ' + table + ' set ' + col + '=' + col,
                           'delete from ' + table):
                    try:
                        sql(op)
                        raise AssertionError(f'mutable via: {op}')
                    except psycopg.errors.CheckViolation:
                        pass
            admin.close()
            print('PASS PG17: full-key CAS both rows, caption allowlist (Nov 5'
                  ' corrective only), feed/Story/claims/approval/registry'
                  ' gates, artifact tenant/caption/policy/Brain/digest/receipt'
                  ' proof, DST canonical noon, Story byte-unchanged, other'
                  ' captions unchanged, ordered replay, race, immutable'
                  ' manifest/audit, least privilege')
        finally:
            if started:
                subprocess.run([str(pg/'pg_ctl'), '-D', str(data), '-m', 'immediate',
                                '-w', 'stop'], check=True, capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
