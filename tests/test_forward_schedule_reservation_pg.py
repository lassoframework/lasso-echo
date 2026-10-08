"""Standalone disposable PG acceptance for DRAFT_fixer_forward_schedule_reservation_20261008.

Run `python3 tests/test_forward_schedule_reservation_pg.py` (also under pytest;
the module-level main() runs in test_schedule_reservation_pg). Uses only stdlib
and existing initdb/pg_ctl/psql against a real disposable PostgreSQL cluster.
No DSN, network, production connection, installs or persisted cluster.
Requires PostgreSQL 17 binaries. Skips cleanly only when the binaries are absent.
"""
import concurrent.futures
import json
import sys
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_USED_PHASH = [0, 0x3f, (1 << 28) - 1, 0x7FFFFFFF00000000]


def _fresh_phash():
    for mask in range(1, 64):
        for flip in (0, 1):
            unsigned = sum(((bin(i & mask).count('1') % 2) ^ flip) << i for i in range(64))
            if all(((unsigned ^ (used & ((1 << 64) - 1))).bit_count()) > 30 for used in _USED_PHASH):
                signed = unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned
                _USED_PHASH.append(signed)
                return signed
    raise AssertionError('independent pHash fixture pool exhausted')


def _skipped():
    return any(not shutil.which(name) for name in ('initdb', 'pg_ctl', 'psql'))


def main():
    if _skipped():
        print('SKIP: existing initdb/pg_ctl/psql unavailable; disposable PostgreSQL not provisioned')
        return
    with tempfile.TemporaryDirectory(prefix='fixer_reservation_pg_', dir='/tmp') as tmp:
        work = Path(tmp)
        sock = work / 'sock'
        sock.mkdir()
        data = work / 'data'
        subprocess.run(['initdb', '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(['pg_ctl', '-D', str(data), '-l', str(work / 'pg.log'),
                        '-o', f"-k {sock} -p 55471 -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        base = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(sock),
                '-p', '55471', '-U', 'postgres', '-d', 'postgres']

        def sql(s, ok=True):
            result = subprocess.run(base, input=s, text=True, capture_output=True, timeout=30)
            if ok and result.returncode:
                raise AssertionError(result.stderr)
            if not ok:
                assert result.returncode != 0, 'unexpected SQL success: ' + s
            return result.stdout.strip() if ok else result.stderr

        def planned_row(tenant, logical, url, day='2026-10-10', image=None, thumbnail=None):
            rid = str(uuid.uuid4())
            image = image or url
            thumb = 'null' if thumbnail is None else "'" + thumbnail + "'"
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "image_url,source_media_url,thumbnail_url,visual_group_key,logical_post_id,"
                "source_media_asset_id,render_manifest_digest,account,format)"
                f" values('{rid}','{tenant}','{day}','approved','active',"
                f"'{image}','{url}',{thumb},'vg_{uuid.uuid4().hex}','{logical}',"
                f"'unprepared_{uuid.uuid4().hex}','sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}','instagram','feed');")
            return rid

        def claimable_row(tenant, group, url, day='2026-10-10', logical=None):
            rid, token = str(uuid.uuid4()), str(uuid.uuid4())
            logical = logical or str(uuid.uuid4())
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "image_url,source_media_url,thumbnail_url,visual_group_key,logical_post_id,"
                "publish_claim_token,publish_reservation_day,source_media_asset_id,render_manifest_digest,account,format)"
                f" values('{rid}','{tenant}','{day}','publishing','active',"
                f"'{url}','{url}',null,'{group}','{logical}','{token}','{day}',"
                f"'unprepared_{uuid.uuid4().hex}','sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}','instagram','feed');")
            return rid, token, logical

        def prepare(rid, fp, image_fp=None, lengths=(10, 10, 30)):
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
            thumb = 'null,null,null' if not thumbnail else f"'{thumbnail}','{image_fp or fp}',{lengths[2]}"
            sql("insert into fixer_forward_media_render_manifest_20261006"
                "(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,"
                "thumbnail_url,thumbnail_fingerprint,thumbnail_length,operation,render_evidence_ref)"
                f" values('{digest}','{tenant}','{asset}','{image}','{image_fp or fp}',{lengths[1]},{thumb},'same_object','owner-verified-render');")
            sql(f"update content_calendar set source_media_asset_id='{asset}',render_manifest_digest='{digest}' where id='{rid}';")

        def attest(rid, fp, image_fp=None, lengths=(10, 10, 30)):
            evidence = str(uuid.uuid4())
            prepare(rid, fp, image_fp, lengths)
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            sql("set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{rid}','{revision}','{evidence}','{fp}',{lengths[0]},'{image_fp or fp}',{lengths[1]},null,null,"
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

        def reserve_args(rid, logical, ids, expected='null'):
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            arr = 'array[' + ','.join(f"'{i}'" for i in ids) + ']::uuid[]'
            return f"'{rid}','{logical}','{revision}',{arr},{expected}"

        def reserve(rid, logical, ids, ok=True, expected='null'):
            return sql('set role service_role; select public.reserve_forward_slot_20261008('
                       + reserve_args(rid, logical, ids, expected) + ');', ok)

        def planned_seed(tenant=None, day='2026-10-10', logical=None, sha64=None, phash=None, url=None, fp=None):
            tenant = tenant or 'gym_' + uuid.uuid4().hex
            logical = logical or str(uuid.uuid4())
            url = url or 'https://scratch.example/' + uuid.uuid4().hex
            fp = fp or 'md5:' + uuid.uuid4().hex
            sha64 = sha64 or uuid.uuid4().hex + uuid.uuid4().hex
            phash = _fresh_phash() if phash is None else phash
            rid = planned_row(tenant, logical, url, day)
            attest(rid, fp)
            ids = visual_attest(rid, tenant, sha64, phash)
            return rid, tenant, logical, url, fp, sha64, phash, ids

        def committed_claim(tenant, day, sha64, phash, url=None, fp=None):
            """Full visual-index claim so committed occupancy exists."""
            url = url or 'https://scratch.example/' + uuid.uuid4().hex
            fp = fp or 'md5:' + uuid.uuid4().hex
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{tenant}',true) on conflict do nothing;")
            rid, token, logical = claimable_row(tenant, 'vg_' + uuid.uuid4().hex, url, day)
            attest(rid, fp)
            ids = visual_attest(rid, tenant, sha64, phash)
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            evidence = sql(f"select evidence_id from fixer_forward_media_lineage_20261006 where calendar_row_id='{rid}';")
            arr = 'array[' + ','.join(f"'{i}'" for i in ids) + ']::uuid[]'
            out = sql('set role service_role; select public.fixer_forward_visual_index_claim_20261008('
                      f"'{rid}','{token}','{evidence}','{revision}',{arr});")
            assert out == 't', out
            return rid, token, logical

        try:
            sql("create role anon; create role authenticated; create role service_role;"
                "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                "account text,format text,gbp_location_id text,status text,variant_status text,"
                "published_at timestamptz,publish_claim_token uuid,"
                "publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,"
                "media_not_ready_reason text);")
            sql((ROOT / 'migrations/logical_post_id_20261004.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_visual_index_20261008.sql').read_text())
            sql((ROOT / 'migrations/DRAFT_fixer_forward_schedule_reservation_20261008.sql').read_text())
            sql('grant select,insert,update,delete on public.content_calendar to service_role;')
            assert sql("select current_setting('server_version_num')::integer between 170000 and 179999;") == 't'

            # OFF gate: reserve holds; nothing persists.
            off = planned_seed()
            assert 'OFF pending review' in reserve(off[0], off[2], off[7], ok=False)
            assert sql('select count(*) from forward_schedule_reservation;') == '0'
            sql('update forward_schedule_reservation_gate_20261008 set enabled=true;')

            # Happy path + idempotent exact retry.
            a = planned_seed()
            rid_a = reserve(a[0], a[2], a[7])
            assert rid_a == reserve(a[0], a[2], a[7])
            assert sql(f"select state from forward_schedule_reservation where reservation_id='{rid_a}';") == 'active'
            # Same-day sibling row of the same logical post reuses its own visual.
            sib = planned_row(a[1], a[2], a[3])
            attest(sib, a[4])
            sib_ids = visual_attest(sib, a[1], a[5], a[6])
            assert reserve(sib, a[2], sib_ids) == rid_a
            assert sql(f"select count(*) from forward_schedule_reservation where tenant_id='{a[1]}';") == '1'
            # Advisory screen is clean for the occupied own slot and flags a
            # different slot trying the same source.
            other_logical = str(uuid.uuid4())
            screen = json.loads(sql('set role service_role; select public.check_reservation_conflicts_20261008('
                                    f"'{a[1]}','2026-10-10','{a[2]}','{a[5]}',{a[6]});"))
            assert screen['allowed'] is True and screen['conflicts'] == [], screen
            screen = json.loads(sql('set role service_role; select public.check_reservation_conflicts_20261008('
                                    f"'{a[1]}','2026-10-10','{other_logical}','{a[5]}',{a[6]});"))
            assert screen['allowed'] is False
            assert any(c['kind'] == 'active_reservation' for c in screen['conflicts']), screen

            # Same source, different logical post same day: denied.
            b = planned_seed(tenant=a[1], logical=other_logical, sha64=a[5], phash=a[6], url=a[3], fp=a[4])
            assert 'another tenant/date/logical post' in reserve(b[0], b[2], b[7], ok=False)
            # Cross-day same source: denied.
            c = planned_seed(tenant=a[1], day='2026-10-11', sha64=a[5], phash=a[6], url=a[3], fp=a[4])
            assert 'another tenant/date/logical post' in reserve(c[0], c[2], c[7], ok=False)
            # Cross-gym same source: denied fleet-wide.
            d = planned_seed(sha64=a[5], phash=a[6])
            assert 'another tenant/date/logical post' in reserve(d[0], d[2], d[7], ok=False)
            # Distinct source is reservable.
            d2 = planned_seed()
            rid_d = reserve(d2[0], d2[2], d2[7])
            assert rid_d and rid_d != rid_a

            # CAS replacement: wrong/missing token conflicts; exact incumbent
            # supersedes atomically and the new reservation becomes active.
            e = planned_seed(tenant=a[1], logical=a[2])
            assert 'slot conflict' in reserve(e[0], e[2], e[7], ok=False)
            assert 'slot conflict' in reserve(e[0], e[2], e[7], ok=False, expected=f"'{uuid.uuid4()}'")
            rid_e = reserve(e[0], e[2], e[7], expected=f"'{rid_a}'")
            assert rid_e != rid_a
            assert sql(f"select state from forward_schedule_reservation where reservation_id='{rid_a}';") == 'superseded'
            assert sql("select count(*) from forward_schedule_reservation where state='active'"
                       f" and tenant_id='{a[1]}' and logical_post_id='{a[2]}';") == '1'
            # A superseded source is not blocked as occupancy: with the exact
            # incumbent CAS token it may reserve the slot again atomically.
            f = planned_seed(tenant=a[1], logical=a[2], sha64=a[5], phash=a[6], url=a[3], fp=a[4])
            assert 'slot conflict' in reserve(f[0], f[2], f[7], ok=False)
            rid_f = reserve(f[0], f[2], f[7], expected=f"'{rid_e}'")
            assert rid_f not in (rid_a, rid_e)
            assert sql(f"select state from forward_schedule_reservation where reservation_id='{rid_e}';") == 'superseded'

            # Release frees the slot; idempotent on terminal rows; never deletes.
            assert sql(f"set role service_role; select public.release_forward_slot_20261008('{rid_f}','planner-reselect');") == 't'
            assert sql(f"set role service_role; select public.release_forward_slot_20261008('{rid_f}','again');") == 't'
            assert 'unavailable' in sql(f"set role service_role; select public.release_forward_slot_20261008('{uuid.uuid4()}','x');", ok=False)
            rid_f2 = reserve(f[0], f[2], f[7])
            assert rid_f2 not in (rid_a, rid_e, rid_f)
            assert sql(f"select count(*) from forward_schedule_reservation where tenant_id='{a[1]}';") == '4'
            assert sql("select count(*) from forward_schedule_reservation where state='active'"
                       f" and tenant_id='{a[1]}' and logical_post_id='{a[2]}';") == '1'

            # Source revocation: terminal for that (tenant, source); slot frees.
            g = planned_seed()
            rid_g = reserve(g[0], g[2], g[7])
            assert sql(f"set role service_role; select public.revoke_source_reservations_20261008('{g[1]}','{g[5]}','provider-error');") == '1'
            assert sql(f"select state from forward_schedule_reservation where reservation_id='{rid_g}';") == 'revoked'
            g2 = planned_seed(tenant=g[1], logical=str(uuid.uuid4()), sha64=g[5], phash=g[6], url=g[3], fp=g[4])
            assert 'revoked' in reserve(g2[0], g2[2], g2[7], ok=False)
            # A different source takes the freed slot.
            h = planned_seed(tenant=g[1], logical=g[2])
            assert reserve(h[0], h[2], h[7]) != rid_g

            # Committed claim occupancy (planner vs publisher): a visual claim
            # committed for another date blocks reserving the same bytes.
            # Threshold phashes are pre-seeded in the pool so every other
            # fixture stays more than 30 bits away.
            pub_tenant = 'gym_' + uuid.uuid4().hex
            pub_sha = uuid.uuid4().hex + uuid.uuid4().hex
            sql('update forward_media_visual_gate_20261008 set enabled=true;')
            _pub_rid, _pub_token, pub_logical = committed_claim(pub_tenant, '2026-10-12', pub_sha, 0)
            i = planned_seed(tenant=pub_tenant, day='2026-10-13', sha64=pub_sha, phash=0)
            assert 'consumed by another tenant/date/logical post' in reserve(i[0], i[2], i[7], ok=False)
            # Same tenant and SAME date as the committed claim but a different
            # logical post: denied (tenant/date alone is never sufficient).
            idl = planned_seed(tenant=pub_tenant, day='2026-10-12', sha64=pub_sha, phash=0)
            assert 'consumed by another tenant/date/logical post' in reserve(idl[0], idl[2], idl[7], ok=False)
            # Exact same-logical-post sibling of the committed claim on the
            # same tenant/date may reserve its own visual.
            sib_day = planned_seed(tenant=pub_tenant, day='2026-10-12', logical=pub_logical, sha64=pub_sha, phash=0)
            assert reserve(sib_day[0], sib_day[2], sib_day[7])
            # Advisory screen agrees: same logical post clean, different
            # logical post on the same tenant/date conflicts.
            screen = json.loads(sql('set role service_role; select public.check_reservation_conflicts_20261008('
                                    f"'{pub_tenant}','2026-10-12','{pub_logical}','{pub_sha}',0);"))
            assert screen['allowed'] is True and screen['conflicts'] == [], screen
            screen = json.loads(sql('set role service_role; select public.check_reservation_conflicts_20261008('
                                    f"'{pub_tenant}','2026-10-12','{uuid.uuid4()}','{pub_sha}',0);"))
            assert screen['allowed'] is False
            assert any(c['kind'] == 'committed_claim' for c in screen['conflicts']), screen
            # Deleted claim row: the receipt's logical post is no longer
            # identifiable, so even the exact sibling scope fails closed.
            pub2_tenant = 'gym_' + uuid.uuid4().hex
            pub2_sha = uuid.uuid4().hex + uuid.uuid4().hex
            pub2_rid, _t2, pub2_logical = committed_claim(pub2_tenant, '2026-10-12', pub2_sha, _fresh_phash())
            sql(f"delete from content_calendar where id='{pub2_rid}';")
            orphan = planned_seed(tenant=pub2_tenant, day='2026-10-12', logical=pub2_logical, sha64=pub2_sha)
            assert 'consumed by another tenant/date/logical post' in reserve(orphan[0], orphan[2], orphan[7], ok=False)
            # Near-identical pHash against committed occupancy: <=6 blocks,
            # 7-30 holds for review, >30 is clean.
            j = planned_seed(tenant=pub_tenant, day='2026-10-13', phash=0x3f)  # distance 6
            assert 'consumed by another tenant/date' in reserve(j[0], j[2], j[7], ok=False)
            k = planned_seed(tenant=pub_tenant, day='2026-10-13', phash=(1 << 28) - 1)  # distance 28
            assert 'held for review' in reserve(k[0], k[2], k[7], ok=False)
            far = planned_seed(tenant=pub_tenant, day='2026-10-13', phash=0x7FFFFFFF00000000)  # distance 31
            assert reserve(far[0], far[2], far[7])
            screen = json.loads(sql('set role service_role; select public.check_reservation_conflicts_20261008('
                                    f"'{pub_tenant}','2026-10-13','{uuid.uuid4()}','{pub_sha}',0);"))
            assert screen['allowed'] is False
            assert any(c['kind'] == 'committed_claim' for c in screen['conflicts']), screen

            # Late negative evidence blocks reservation.
            n = planned_seed()
            sql("insert into forward_media_visual_negative(tenant_key,source_sha256,reason)"
                f" values('{n[1]}','{n[5]}','provider-error-ambiguous-send');")
            assert 'negative evidence blocks' in reserve(n[0], n[2], n[7], ok=False)
            assert sql(f"select count(*) from forward_schedule_reservation where tenant_id='{n[1]}';") == '0'
            screen = json.loads(sql('set role service_role; select public.check_reservation_conflicts_20261008('
                                    f"'{n[1]}','2026-10-10','{uuid.uuid4()}','{n[5]}',{n[6]});"))
            assert screen['allowed'] is False and any(c['kind'] == 'negative' for c in screen['conflicts'])

            # Late fleet history hold: attested while clear, then an explicit
            # uncertain hold for the same source fingerprint fails closed.
            u = planned_seed()
            sql("insert into fixer_forward_media_original_registry_20261006"
                "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)"
                " select tenant_id,source_asset_id||'_historical',source_url||'/history',source_fingerprint,source_length,registry_evidence_ref"
                f" from fixer_forward_media_original_registry_20261006 where tenant_id='{u[1]}';")
            sql("insert into fixer_forward_media_history_clearance_20261006"
                "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref)"
                " select tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,'hold_uncertain','history-census-unknown'"
                f" from fixer_forward_media_original_registry_20261006 where tenant_id='{u[1]}' and source_asset_id like '%_historical';")
            assert 'historical eligibility clearance unavailable or held' in reserve(u[0], u[2], u[7], ok=False)
            assert sql(f"select count(*) from forward_schedule_reservation where tenant_id='{u[1]}';") == '0'

            # Changed media after attestation invalidates the reservation.
            v = planned_seed()
            sql(f"update content_calendar set image_url='https://scratch.example/revised_{uuid.uuid4().hex}' where id='{v[0]}';")
            assert reserve(v[0], v[2], v[7], ok=False)
            # Logical post forgery: caller cannot invent or borrow an identity.
            w = planned_seed()
            assert 'logical post binding invalid' in reserve(w[0], str(uuid.uuid4()), w[7], ok=False)
            # Ready-row bypass: publishing rows can never reserve a future slot.
            rb_tenant = 'gym_' + uuid.uuid4().hex
            rb_sha = uuid.uuid4().hex + uuid.uuid4().hex
            rb_rid, rb_token, _rb_lp = claimable_row(rb_tenant, 'vg_' + uuid.uuid4().hex,
                                                     'https://scratch.example/' + uuid.uuid4().hex)
            attest(rb_rid, 'md5:' + uuid.uuid4().hex)
            rb_ids = visual_attest(rb_rid, rb_tenant, rb_sha, _fresh_phash())
            rb_logical = sql(f"select logical_post_id from content_calendar where id='{rb_rid}';")
            assert 'unsent planned calendar row' in reserve(rb_rid, rb_logical, rb_ids, ok=False)

            # Reservation proof consult: exact slot+source resolves; wrong
            # source, borrowed slot or terminal state holds.
            proof = json.loads(sql('set role service_role; select public.forward_reservation_proof_20261008('
                                   f"'{h[0]}','{h[5]}');"))
            assert proof['reservation_id'] and proof['logical_post_id'] == h[2] and proof['source_sha256'] == h[5]
            assert 'proof unavailable' in sql('set role service_role; select public.forward_reservation_proof_20261008('
                                              f"'{h[0]}','{uuid.uuid4().hex + uuid.uuid4().hex}');", ok=False)
            assert 'proof unavailable' in sql('set role service_role; select public.forward_reservation_proof_20261008('
                                              f"'{c[0]}','{h[5]}');", ok=False)
            sql(f"set role service_role; select public.release_forward_slot_20261008('{proof['reservation_id']}','done');")
            assert 'proof unavailable' in sql('set role service_role; select public.forward_reservation_proof_20261008('
                                              f"'{h[0]}','{h[5]}');", ok=False)

            # Concurrency: two different sources race one slot. Exactly one
            # commits; the loser raises and leaves no partial occupancy.
            race_tenant = 'gym_' + uuid.uuid4().hex
            race_logical = str(uuid.uuid4())
            r1 = planned_seed(tenant=race_tenant, logical=race_logical)
            r2 = planned_seed(tenant=race_tenant, logical=race_logical)

            def race(entry):
                rid, _t, logical, _u, _fp, _s, _p, ids = entry
                return subprocess.run(base, input='begin; set role service_role; select '
                                      'public.reserve_forward_slot_20261008(' + reserve_args(rid, logical, ids) + ');'
                                      'select pg_sleep(0.5); commit;', text=True, capture_output=True, timeout=30)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(race, (r1, r2)))
            assert sum(x.returncode == 0 for x in results) == 1, [x.stderr for x in results]
            assert sql("select count(*) from forward_schedule_reservation where state='active'"
                       f" and tenant_id='{race_tenant}' and logical_post_id='{race_logical}';") == '1'
            assert sql(f"select count(*) from forward_schedule_reservation where tenant_id='{race_tenant}';") == '1'

            # Winner stays durable: no delete path and terminal rows persist.
            winner = sql(f"select reservation_id from forward_schedule_reservation where tenant_id='{race_tenant}';")
            assert 'durable' in sql(f"delete from forward_schedule_reservation where reservation_id='{winner}';", ok=False)
            assert 'permission denied' in sql(f"set role service_role; delete from forward_schedule_reservation where reservation_id='{winner}';", ok=False)
            assert 'immutable' in sql('truncate forward_schedule_reservation;', ok=False)
            # Guard trigger: even with table grants AND row visibility, direct
            # DML outside the RPC owner is refused; one-way transitions only.
            sql('grant insert,update on public.forward_schedule_reservation to service_role;')
            sql('create policy tmp_update on public.forward_schedule_reservation for update to service_role using(true) with check(true);'
                'create policy tmp_insert on public.forward_schedule_reservation for insert to service_role with check(true);')
            assert 'RPC-managed only' in sql("set role service_role; update forward_schedule_reservation set source_sha256='0'*64;", ok=False)
            assert 'RPC-managed only' in sql('set role service_role; insert into forward_schedule_reservation'
                                             "(tenant_id,calendar_row_id,logical_post_id,post_date,source_asset_id,source_url,source_sha256,phash_v1,row_revision,lineage_evidence_id,attestation_ids)"
                                             f" select tenant_id,calendar_row_id,logical_post_id,post_date,source_asset_id,source_url,source_sha256,phash_v1,row_revision,lineage_evidence_id,attestation_ids from forward_schedule_reservation limit 1;", ok=False)
            sql('drop policy tmp_update on public.forward_schedule_reservation;'
                'drop policy tmp_insert on public.forward_schedule_reservation;')
            sql('revoke insert,update on public.forward_schedule_reservation from service_role;')
            assert 'permission denied' in sql("set role service_role; update forward_schedule_reservation set source_sha256='0'*64;", ok=False)

            # ACLs: RPCs are service_role-only; nothing is publicly callable.
            for fn, args in (('reserve_forward_slot_20261008', reserve_args(w[0], w[2], w[7])),
                             ('release_forward_slot_20261008', f"'{uuid.uuid4()}','x'"),
                             ('revoke_source_reservations_20261008', f"'x','{'0' * 64}','x'"),
                             ('check_reservation_conflicts_20261008', f"'x','2026-10-10','{uuid.uuid4()}','{'0' * 64}',0"),
                             ('forward_reservation_proof_20261008', f"'{uuid.uuid4()}','{'0' * 64}'")):
                for denied in ('anon', 'authenticated', 'fixer_forward_media_attester_20261006',
                               'fixer_forward_media_owner_20261006'):
                    assert 'permission denied' in sql(f'set role {denied}; select public.{fn}({args});', ok=False)
            assert 'permission denied' in sql('set role service_role; insert into forward_schedule_reservation'
                                              "(tenant_id,calendar_row_id,logical_post_id,post_date,source_asset_id,source_url,source_sha256,phash_v1,row_revision,lineage_evidence_id,attestation_ids)"
                                              f" values('x','{uuid.uuid4()}','{uuid.uuid4()}','2026-10-10','x','https://scratch.example/x','{'0' * 64}',0,'x','{uuid.uuid4()}',array['{uuid.uuid4()}','{uuid.uuid4()}','{uuid.uuid4()}']::uuid[]);", ok=False)
            assert 'permission denied' in sql('set role anon; select * from forward_schedule_reservation;', ok=False)
            # Isolation guard matches the stack.
            assert 'read committed isolation' in sql('begin isolation level repeatable read; set role service_role; '
                                                     'select public.reserve_forward_slot_20261008(' + reserve_args(w[0], w[2], w[7]) + '); commit;', ok=False)
            # Malformed arguments hold.
            assert 'binding required' in sql('set role service_role; select public.reserve_forward_slot_20261008(null,null,null,null);', ok=False)

            # Rollback-only installation: install into a second disposable
            # cluster, verify objects, destroy entirely.
            rb_sock = work / 'rb_sock'
            rb_sock.mkdir()
            rb_data = work / 'rb_data'
            subprocess.run(['initdb', '-D', str(rb_data), '-U', 'postgres', '--no-sync'],
                           check=True, capture_output=True, timeout=60)
            subprocess.run(['pg_ctl', '-D', str(rb_data), '-l', str(work / 'rb_pg.log'),
                            '-o', f"-k {rb_sock} -p 55472 -c listen_addresses=''", '-w', 'start'],
                           check=True, capture_output=True, timeout=60)
            try:
                rb = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(rb_sock),
                      '-p', '55472', '-U', 'postgres', '-d', 'postgres']

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
                rsql((ROOT / 'migrations/logical_post_id_20261004.sql').read_text())
                rsql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
                rsql((ROOT / 'migrations/DRAFT_fixer_forward_visual_index_20261008.sql').read_text())
                rsql((ROOT / 'migrations/DRAFT_fixer_forward_schedule_reservation_20261008.sql').read_text())
                assert rsql("select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace"
                            " where n.nspname='public' and p.proname in ('reserve_forward_slot_20261008','release_forward_slot_20261008','revoke_source_reservations_20261008','check_reservation_conflicts_20261008','forward_reservation_proof_20261008');") == '5'
                assert rsql("select count(*) from pg_tables where schemaname='public' and tablename in"
                            " ('forward_schedule_reservation','forward_schedule_reservation_gate_20261008');") == '2'
                assert rsql('select enabled from forward_schedule_reservation_gate_20261008;') == 'f'
            finally:
                subprocess.run(['pg_ctl', '-D', str(rb_data), '-m', 'immediate', '-w', 'stop'],
                               capture_output=True, timeout=60)
            assert not subprocess.run(['pg_ctl', '-D', str(rb_data), 'status'],
                                      capture_output=True, timeout=30).returncode == 0

            print('PASS: PG17 forward schedule reservation; OFF gate hold; happy path + idempotent retry; '
                  'exact-logical-post sibling reuse; cross-logical-post (same date)/cross-day/cross-gym '
                  'denial for active reservations and committed claims; deleted-claim fail-closed; '
                  'CAS supersede with wrong-token conflict; release/re-reserve; terminal revocation fence; '
                  'committed claim occupancy; pHash <=6 block, 7-30 review, '
                  '>30 clean; late negative; late fleet history hold; revision drift; logical-post '
                  'forgery; ready-row bypass denial; reservation proof consult and borrow denial; '
                  'advisory conflict screen; concurrent one-winner slot race with no loser occupancy; '
                  'durability (no delete/truncate); RPC-owner write guard; ACL denials; isolation '
                  'guard; rollback-only installation')
        finally:
            subprocess.run(['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           capture_output=True, timeout=60)


def test_schedule_reservation_pg():
    if _skipped():
        import pytest
        pytest.skip('existing initdb/pg_ctl/psql unavailable; disposable PostgreSQL not provisioned')
    main()


if __name__ == '__main__':
    main()
