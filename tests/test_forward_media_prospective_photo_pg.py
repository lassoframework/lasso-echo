"""Standalone disposable PG acceptance for DRAFT_fixer_prospective_photo_authority_20261008.

Run `python3 tests/test_forward_media_prospective_photo_pg.py` (also under pytest;
the module-level main() runs in test_prospective_photo_pg). Uses only stdlib and
existing initdb/pg_ctl/psql against a real disposable PostgreSQL cluster.
No DSN, network, production connection, installs or persisted cluster.
Requires PostgreSQL 17 binaries. Skips cleanly only when the binaries are absent.

All bytes, signing material, corpus judgments and roles are SYNTHETIC. The SQL
certificate record path cannot verify Ed25519 natively (the production contract
requires the isolated Python verifier AND the owner re-verification BEFORE any
record attempt); this acceptance exercises only the composed SQL authority with
synthetic signature-shaped hex, which is exactly what the record RPC validates.
"""
import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import sys
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


def _signed(unsigned):
    unsigned &= (1 << 64) - 1
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def _pg(name):
    bundled = Path('/opt/homebrew/opt/postgresql@17/bin') / name
    return str(bundled) if bundled.is_file() else shutil.which(name)


def _skipped():
    return any(not _pg(name) for name in ('initdb', 'pg_ctl', 'psql'))


MIGRATIONS = (
    'logical_post_id_20261004.sql',
    'DRAFT_fixer_forward_media_claim_20261006.sql',
    'DRAFT_fixer_forward_visual_index_20261008.sql',
    'DRAFT_fixer_forward_media_source_history_20261007.sql',
    'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
    'DRAFT_fixer_forward_schedule_reservation_20261008.sql',
    'DRAFT_fixer_photo_historical_clearance_20261008.sql',
    'DRAFT_fixer_prospective_photo_authority_20261008.sql',
    # Reapplied LAST so the guard's apply-last invariant still holds after the
    # prospective draft composes on top (idempotent, retains OIDs/ACLs).
    'DRAFT_fixer_photo_historical_clearance_20261008.sql',
)


def main():
    if _skipped():
        print('SKIP: existing initdb/pg_ctl/psql unavailable; disposable PostgreSQL not provisioned')
        return
    with tempfile.TemporaryDirectory(prefix='fixer_prospective_pg_', dir='/tmp') as tmp:
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

        def planned_row(tenant, logical, url, day='2026-10-10', group=None):
            rid = str(uuid.uuid4())
            group = group or 'vg_' + uuid.uuid4().hex
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "image_url,source_media_url,thumbnail_url,visual_group_key,logical_post_id,"
                "source_media_asset_id,render_manifest_digest,account,format)"
                f" values('{rid}','{tenant}','{day}','approved','active',"
                f"'{url}','{url}',null,'{group}','{logical}',"
                f"'unprepared_{uuid.uuid4().hex}','sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}','instagram','feed');")
            return rid, group

        def prepare(rid, fp, lengths=(10, 10, 30)):
            tenant, url = sql(f"select gym_id||'|'||source_media_url from content_calendar where id='{rid}';").split('|')
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
            sql("insert into fixer_forward_media_render_manifest_20261006"
                "(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,"
                "operation,render_evidence_ref)"
                f" values('{digest}','{tenant}','{asset}','{url}','{fp}',{lengths[1]},'same_object','owner-verified-render');")
            sql(f"update content_calendar set source_media_asset_id='{asset}',render_manifest_digest='{digest}' where id='{rid}';")
            return asset

        def attest(rid, fp, lengths=(10, 10, 30)):
            evidence = str(uuid.uuid4())
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            sql("set role fixer_forward_media_attester_20261006; select fixer_attest_forward_media_20261006"
                f"('{rid}','{revision}','{evidence}','{fp}',{lengths[0]},'{fp}',{lengths[1]},null,null,"
                "'same_object','controlled-runtime-test');")
            return evidence

        def visual_attest(rid, tenant, sha64, phash):
            rev = sql("select ('x'||substr(fixer_forward_media_attestation_request_20261006"
                      f"('{rid}')->>'revision',1,15))::bit(60)::bigint;")
            ev, src, img = sql(
                "select evidence_id||'|'||source_read_receipt||'|'||image_read_receipt"
                f" from fixer_forward_media_lineage_20261006 where calendar_row_id='{rid}';").split('|')
            url = sql(f"select source_media_url from content_calendar where id='{rid}';")
            ids = []
            for role, receipt in (('original', src), ('delivered', img), ('thumbnail', img)):
                md5, byte_length = sql(
                    "select substring(fingerprint from 5)||'|'||byte_length"
                    f" from fixer_forward_media_object_read_20261006 where receipt_id='{receipt}';").split('|')
                aid = str(uuid.uuid4())
                ids.append(aid)
                sql("set role fixer_forward_media_attester_20261006; insert into forward_media_visual_attestation"
                    "(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id)"
                    f" values('{aid}','{tenant}','{url}','{role}','{sha64}','{md5}',{byte_length},{phash},{rev},'{ev}','{receipt}');")
            return ids

        def certificate(rid, tenant, url, fp, sha64, lengths=(10, 10, 30)):
            asset = sql(f"select source_media_asset_id from content_calendar where id='{rid}';")
            sql("insert into media_source values"
                f"('src_{tenant}','{tenant}','gym_drive','folder',true) on conflict do nothing;")
            sql(f"insert into media_asset values('{asset}','src_{tenant}','{tenant}') on conflict do nothing;")
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            receipt_ref = 'source-receipt:sha256:' + uuid.uuid4().hex + uuid.uuid4().hex
            sql("insert into fixer_forward_media_source_receipt_20261007"
                "(receipt_ref,calendar_row_id,row_revision,binding_revision,tenant_id,source_asset_id,"
                "source_id,folder_id,exact_source_url,source_fingerprint,source_sha256,source_length,evidence_json)"
                f" values('{receipt_ref}','{rid}','{revision}','{revision}','{tenant}','{asset}',"
                f"'src_{tenant}','folder','{url}','{fp}','sha256:{sha64}',{lengths[0]},'{{}}');")
            return receipt_ref

        def record_certificate(rid, tenant, group, url, fp, receipt_ref, sha64, day='2026-10-10', lengths=(10, 10, 30)):
            content_digest = sql("select 'sha256:'||encode(sha256(convert_to("
                                 f"fixer_forward_media_photo_content_20261007('{rid}')::text,'UTF8')),'hex');")
            snapshot = json.loads(sql('select fixer_forward_media_photo_snapshot_20261007();'))
            assert snapshot['policy_approved'] is True and snapshot['scope_complete'] is True, snapshot
            dispositions = [{'history_key': h['history_key'], 'disposition': 'reviewed_visual_nonmatch',
                             'inspected_sha256': h['visual_sha256'],
                             'published_binding_ref': h['published_binding_ref'],
                             'review_evidence_ref': 'SYNTHETIC independent per-object visual inspection'}
                            for h in snapshot['rows']]
            audit_id = str(uuid.uuid4())
            payload = {'schema_version': 1, 'audit_id': audit_id,
                       'auditor_id': 'synthetic-independent-reviewer', 'key_id': 'synthetic-key',
                       'policy_id': snapshot['policy_id'], 'baseline_id': snapshot['baseline_id'],
                       'generation': snapshot['generation'], 'spine_digest': snapshot['spine_digest'],
                       'candidate': {'calendar_row_id': rid, 'tenant_id': tenant, 'group_key': group,
                                     'post_date': day, 'source_asset_id': sql(
                                         f"select source_media_asset_id from content_calendar where id='{rid}';"),
                                     'source_url': url, 'image_url': url,
                                     'source_fingerprint': fp, 'source_sha256': 'sha256:' + sha64,
                                     'source_length': lengths[0], 'image_fingerprint': fp,
                                     'image_sha256': 'sha256:' + sha64, 'image_length': lengths[1],
                                     'source_receipt_ref': receipt_ref,
                                     'render_recipe_digest': 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex,
                                     'content_digest': content_digest},
                       'dispositions': dispositions,
                       'decision': 'reviewed_no_prior_visual_use',
                       'stated_visual_uncertainty': 'SYNTHETIC visual review judgment'}
            payload_text = json.dumps(payload, sort_keys=True)
            signature = uuid.uuid4().hex + uuid.uuid4().hex + uuid.uuid4().hex + uuid.uuid4().hex
            out = sql("set role photo_auditor; select fixer_forward_media_photo_record_20261007("
                      f"$json${payload_text}$json$,'{signature}',"
                      f"'photo-audit:sha256:'||encode(sha256(convert_to($json${payload_text}$json$||E'\\n'||'{signature}','UTF8')),'hex'));")
            assert out == 't', out
            return audit_id

        def admit_args(rid, logical, ids, audit_id):
            revision = sql(f"select fixer_forward_media_attestation_request_20261006('{rid}')->>'revision';")
            arr = 'array[' + ','.join(f"'{i}'" for i in ids) + ']::uuid[]'
            return f"'{rid}','{logical}','{revision}',{arr},'{audit_id}'"

        def admit(rid, logical, ids, audit_id, ok=True, role='fixer_forward_media_owner_20261006'):
            return sql(f'set role {role}; select public.admit_prospective_photo_occupancy_20261008('
                       + admit_args(rid, logical, ids, audit_id) + ');', ok)

        def admit_raw(rid, logical, ids, audit_id):
            # Race harness: returns (rc, stdout, stderr) without asserting.
            result = subprocess.run(base + ['-c', 'set role fixer_forward_media_owner_20261006;'
                                    ' select public.admit_prospective_photo_occupancy_20261008('
                                    + admit_args(rid, logical, ids, audit_id) + ');'],
                                    text=True, capture_output=True, timeout=60)
            return result.returncode, result.stdout.strip(), result.stderr

        def seed(tenant=None, day='2026-10-10', logical=None, sha64=None, phash=None, url=None, fp=None):
            tenant = tenant or 'gym_' + uuid.uuid4().hex
            logical = logical or str(uuid.uuid4())
            url = url or 'https://scratch.example/' + uuid.uuid4().hex
            fp = fp or 'md5:' + uuid.uuid4().hex
            sha64 = sha64 or uuid.uuid4().hex + uuid.uuid4().hex
            phash = _fresh_phash() if phash is None else phash
            rid, group = planned_row(tenant, logical, url, day)
            prepare(rid, fp)
            attest(rid, fp)
            ids = visual_attest(rid, tenant, sha64, phash)
            receipt_ref = certificate(rid, tenant, url, fp, sha64)
            audit_id = record_certificate(rid, tenant, group, url, fp, receipt_ref, sha64, day)
            return rid, tenant, logical, url, fp, sha64, phash, ids, audit_id

        try:
            sql("create role anon; create role authenticated; create role service_role;"
                "create role photo_auditor nologin;"
                "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                "account text,format text,gbp_location_id text,status text not null check(status in ('draft','pending','approved','published','denied','killed','failed','publishing','deleted','coach_review')),variant_status text not null check(variant_status in ('active','candidate','archived')),"
                "published_at timestamptz,publish_claim_token uuid,"
                "publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,"
                "media_not_ready_reason text,caption text);"
                "create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);"
                "create table media_asset(id text primary key,source_id text,gym_id text);")
            for name in MIGRATIONS:
                sql((ROOT / 'migrations' / name).read_text())
            sql('grant select,insert,update,delete on public.content_calendar to service_role;'
                'grant fixer_forward_media_photo_auditor_20261007 to photo_auditor;')
            assert sql("select current_setting('server_version_num')::integer between 170000 and 179999;") == 't'

            # Independent authority provisioning: approved policy/key, complete
            # zero-exclusion baseline selected as current state.
            sql("insert into fixer_forward_media_photo_policy_20261007 values"
                "('synthetic-policy',true,'complete_fleet_still_photo_history',"
                "'SYNTHETIC reviewed video ruling','SYNTHETIC cutover','SYNTHETIC admin');")
            sql("insert into fixer_forward_media_photo_key_20261007 values"
                "('synthetic-key','synthetic-independent-reviewer','photo_auditor','synthetic-policy',"
                f"'{uuid.uuid4().hex + uuid.uuid4().hex}',true);")
            baseline = str(uuid.uuid4())
            history = json.dumps([{'history_key': 'SYNTHETIC full historical photo', 'resolved': True,
                                   'media_kind': 'still_photo',
                                   'visual_sha256': 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex,
                                   'published_binding_ref': 'SYNTHETIC preserved complete fleet history'}])
            sql("insert into fixer_forward_media_photo_baseline_20261007"
                "(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count)"
                f" values('{baseline}','synthetic-policy',true,'{history}'::jsonb,'SYNTHETIC complete corpus',1);")
            sql(f"update fixer_forward_media_photo_state_20261007 set baseline_id='{baseline}' where singleton;")

            # Privilege envelope: only the owner role may admit; service_role
            # may read the protected gate but never mutate it or admit.
            assert sql('set role service_role; select enabled from forward_prospective_photo_gate_20261008;') == 'f'
            off = seed()
            assert 'permission denied' in admit(off[0], off[2], off[7], off[8], role='service_role', ok=False)
            assert 'permission denied' in admit(off[0], off[2], off[7], off[8], role='authenticated', ok=False)
            # OFF gate: admission holds; nothing persists.
            assert 'OFF pending review' in admit(off[0], off[2], off[7], off[8], ok=False)
            assert sql('select count(*) from forward_prospective_photo_occupancy_20261008;') == '0'
            sql('update forward_prospective_photo_gate_20261008 set enabled=true;')

            # Positive admit + idempotent exact replay.
            a = off
            occ_a = admit(a[0], a[2], a[7], a[8])
            assert occ_a == admit(a[0], a[2], a[7], a[8])
            assert sql(f"select state from forward_prospective_photo_occupancy_20261008 where occupancy_id='{occ_a}';") == 'active'
            assert sql(f"select count(*) from forward_prospective_photo_binding_20261008 where occupancy_id='{occ_a}';") == '1'
            # Exact proof resolves and is explicitly lane-tagged.
            proof = json.loads(sql('set role service_role; select public.forward_prospective_photo_proof_20261008('
                                   f"'{a[0]}','{a[5]}');"))
            assert proof['lane'] == 'prospective_occupancy' and proof['occupancy_id'] == occ_a, proof
            assert proof['audit_id'] == a[8] and proof['tenant_id'] == a[1], proof

            # Same-day sibling row of the same logical post reuses its own visual.
            # The sibling carries its OWN row-bound certificate and proof.
            sib, sib_group = planned_row(a[1], a[2], a[3])
            prepare(sib, a[4])
            attest(sib, a[4])
            sib_ids = visual_attest(sib, a[1], a[5], a[6])
            sib_receipt = certificate(sib, a[1], a[3], a[4], a[5])
            sib_audit = record_certificate(sib, a[1], sib_group, a[3], a[4], sib_receipt, a[5])
            assert admit(sib, a[2], sib_ids, sib_audit) == occ_a
            assert sql(f"select count(*) from forward_prospective_photo_occupancy_20261008 where tenant_id='{a[1]}';") == '1'

            # Cross-tenant, cross-date and cross-logical-post originals reject.
            other_logical = str(uuid.uuid4())
            b = seed(tenant=a[1], logical=other_logical, sha64=a[5], phash=a[6], url=a[3], fp=a[4])
            assert 'another tenant/date/logical post' in admit(b[0], b[2], b[7], b[8], ok=False)
            c = seed(tenant=a[1], day='2026-10-11', sha64=a[5], phash=a[6], url=a[3], fp=a[4])
            assert 'another tenant/date/logical post' in admit(c[0], c[2], c[7], c[8], ok=False)
            d = seed(sha64=a[5], phash=a[6])
            assert 'another tenant/date/logical post' in admit(d[0], d[2], d[7], d[8], ok=False)

            # Known derivative ancestry rejects: pHash distance <= 6 blocks,
            # 7-30 holds for review, even for never-occupied bytes.
            blocked = seed(tenant=a[1], logical=other_logical,
                           phash=_signed((a[6] & ((1 << 64) - 1)) ^ 0x7))
            assert 'visual byte ancestry' in admit(blocked[0], blocked[2], blocked[7], blocked[8], ok=False)
            held = seed(tenant=a[1], logical=other_logical,
                        phash=_signed((a[6] & ((1 << 64) - 1)) ^ 0x3FF))
            assert 'held for review' in admit(held[0], held[2], held[7], held[8], ok=False)

            # Permanent use ledger rejects exact source and derivative bytes.
            used = seed()
            sql("insert into fixer_forward_media_use_20261006"
                f" values('{used[4]}','{used[1]}','2026-10-09','vg_historical','{uuid.uuid4()}','{uuid.uuid4()}');")
            assert 'permanent use ledger' in admit(used[0], used[2], used[7], used[8], ok=False)
            # Historical-original ancestry rejects the exact audited bytes. The
            # historical binding trigger requires a real published row; publish
            # a synthetic historical row, then RE-BASELINE so the complete
            # current corpus (now including that published row) stays resolved
            # for every later certificate.
            hist = seed()
            hist_pub = str(uuid.uuid4())
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "image_url,source_media_url,thumbnail_url,visual_group_key,"
                "source_media_asset_id,account,format,published_at)"
                f" values('{hist_pub}','{hist[1]}','2026-10-01','published','active',"
                f"'{hist[3]}','{hist[3]}',null,'vg_historical','asset_historical','instagram','feed',now());")
            hist_rev = sql(f"select md5(to_jsonb(r)::text) from content_calendar r where id='{hist_pub}';")
            sql("insert into fixer_forward_media_historical_original_20261007"
                "(calendar_row_id,calendar_revision,tenant_id,source_asset_id,exact_source_url,"
                "source_fingerprint,source_sha256,source_length,historical_evidence_ref)"
                f" values('{hist_pub}','{hist_rev}','{hist[1]}','asset_historical','{hist[3]}',"
                f"'{hist[4]}','sha256:{hist[5]}',10,'SYNTHETIC audited historical original');")
            assert 'historical byte ancestry' in admit(hist[0], hist[2], hist[7], hist[8], ok=False)
            hist_digest = sql("select 'sha256:'||encode(sha256(convert_to("
                              f"fixer_forward_media_photo_content_20261007('{hist_pub}')::text,'UTF8')),'hex');")
            resolved = json.loads(history)
            resolved.append({'history_key': 'calendar:' + hist_pub, 'resolved': True,
                             'media_kind': 'still_photo',
                             'visual_sha256': 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex,
                             'published_binding_ref': 'calendar:' + hist_pub,
                             'calendar_visual_digest': hist_digest})
            baseline2 = str(uuid.uuid4())
            sql("insert into fixer_forward_media_photo_baseline_20261007"
                "(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count)"
                f" values('{baseline2}','synthetic-policy',true,'{json.dumps(resolved)}'::jsonb,"
                "'SYNTHETIC complete corpus v2',2);")
            sql(f"update fixer_forward_media_photo_state_20261007 set baseline_id='{baseline2}',generation=generation+1 where singleton;")
            # Negative evidence rejects.
            neg = seed()
            sql("insert into forward_media_visual_negative(tenant_key,source_sha256,reason)"
                f" values('{neg[1]}','{neg[5]}','SYNTHETIC late negative');")
            assert 'negative evidence' in admit(neg[0], neg[2], neg[7], neg[8], ok=False)

            # A different source on an occupied slot conflicts permanently.
            e = seed(tenant=a[1], logical=a[2])
            assert 'prospective occupancy slot conflict' in admit(e[0], e[2], e[7], e[8], ok=False)

            # Concurrency: identical exact replays converge on one occupancy.
            f = seed()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: admit(f[0], f[2], f[7], f[8]), range(2)))
            assert results[0] == results[1], results
            # Concurrent cross-slot admissions of the same source: exactly one wins.
            g1 = seed(tenant='gym_race', logical=str(uuid.uuid4()), sha64=None)
            shared_sha, shared_phash = g1[5], g1[6]
            g2 = seed(tenant='gym_race', logical=str(uuid.uuid4()), sha64=shared_sha, phash=shared_phash,
                      url=g1[3], fp=g1[4])
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                raced = list(pool.map(lambda g: admit_raw(g[0], g[2], g[7], g[8]), (g1, g2)))
            winners = [out for rc, out, err in raced if rc == 0]
            losers = [err for rc, out, err in raced if rc != 0]
            assert len(winners) == 1 and len(losers) == 1, raced
            assert ('another tenant/date/logical post' in losers[0]
                    or 'duplicate key' in losers[0] or 'slot conflict' in losers[0]), raced
            assert sql("select count(*) from forward_prospective_photo_occupancy_20261008"
                       f" where source_sha256='{shared_sha}';") == '1'

            # Rollback: a failing admission aborts its whole transaction and
            # persists nothing (no occupancy, no binding, no state change).
            h = seed()
            before = sql('select count(*) from forward_prospective_photo_occupancy_20261008;')
            assert 'logical post binding invalid' in sql(
                'set role fixer_forward_media_owner_20261006; select public.admit_prospective_photo_occupancy_20261008('
                + admit_args(h[0], str(uuid.uuid4()), h[7], h[8]) + ');', ok=False)
            assert sql('select count(*) from forward_prospective_photo_occupancy_20261008;') == before
            assert sql(f"select count(*) from forward_prospective_photo_binding_20261008 where calendar_row_id='{h[0]}';") == '0'

            # Persistence after schedule release: a reservation on the SAME slot
            # can be taken and terminally released; the prospective occupancy
            # survives, still proves, and still fences every other slot.
            sql('update forward_schedule_reservation_gate_20261008 set enabled=true;')
            r1 = seed()
            arr = 'array[' + ','.join(f"'{i}'" for i in r1[7]) + ']::uuid[]'
            reservation = sql('set role service_role; select public.reserve_forward_slot_20261008('
                              f"'{r1[0]}','{r1[2]}','"
                              + sql(f"select fixer_forward_media_attestation_request_20261006('{r1[0]}')->>'revision';")
                              + f"',{arr},null);")
            occ_r = admit(r1[0], r1[2], r1[7], r1[8])
            assert sql('set role service_role; select public.release_forward_slot_20261008('
                       f"'{reservation}','schedule-released');") == 't'
            assert sql(f"select state from forward_schedule_reservation where reservation_id='{reservation}';") == 'released'
            assert sql(f"select state from forward_prospective_photo_occupancy_20261008 where occupancy_id='{occ_r}';") == 'active'
            proof = json.loads(sql('set role service_role; select public.forward_prospective_photo_proof_20261008('
                                   f"'{r1[0]}','{r1[5]}');"))
            assert proof['occupancy_id'] == occ_r, proof
            late = seed(tenant=r1[1], logical=str(uuid.uuid4()), sha64=r1[5], phash=r1[6], url=r1[3], fp=r1[4])
            assert 'another tenant/date/logical post' in admit(late[0], late[2], late[7], late[8], ok=False)

            # Revocation is terminal, durable and fenced; occupancy rows persist.
            assert sql('set role fixer_forward_media_owner_20261006;'
                       ' select public.revoke_prospective_photo_occupancy_20261008('
                       f"'{r1[1]}','{r1[5]}','SYNTHETIC source withdrawal');") == '1'
            assert sql(f"select state from forward_prospective_photo_occupancy_20261008 where occupancy_id='{occ_r}';") == 'revoked'
            revived = seed(tenant=r1[1], logical=str(uuid.uuid4()), sha64=r1[5], phash=r1[6], url=r1[3], fp=r1[4])
            assert 'prospective source revoked' in admit(revived[0], revived[2], revived[7], revived[8], ok=False)

            # Direct ledger writes are denied to every runtime role (ACL denies
            # before the RPC-managed trigger is even reached).
            assert 'permission denied' in sql('set role service_role; insert into forward_prospective_photo_occupancy_20261008'
                "(tenant_id,calendar_row_id,logical_post_id,post_date,source_asset_id,source_url,source_sha256,"
                "phash_v1,source_read_receipt,image_read_receipt,source_md5,source_length,image_md5,image_length,"
                f"row_revision,lineage_evidence_id,attestation_ids,audit_id) select tenant_id,calendar_row_id,"
                "logical_post_id,post_date,source_asset_id,source_url,source_sha256,phash_v1,source_read_receipt,"
                "image_read_receipt,source_md5,source_length,image_md5,image_length,row_revision,lineage_evidence_id,"
                f"attestation_ids,audit_id from forward_prospective_photo_occupancy_20261008 limit 1;", ok=False)
            print('PASS: PG17 prospective photo occupancy admit/replay/reject/derivative/'
                  'persistence/concurrency/rollback acceptance')
        finally:
            subprocess.run([_pg('pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, timeout=30)


def test_prospective_photo_pg():
    main()


if __name__ == '__main__':
    main()
