"""Standalone disposable PG acceptance; run python3 this_file.py.
Uses only stdlib and existing initdb/pg_ctl/psql. No DSN, network, production
connection, installs or persisted cluster; applies real prerequisite drafts.
"""
import concurrent.futures
from pathlib import Path
import shutil
import subprocess
import tempfile
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

        def claim(row, token, ok=True, prefix=''):
            return sql(prefix + "set role service_role; select public.fixer_claim_forward_media_20261006"
                       f"('{row}','{token}');", ok)

        def row(tenant, group, url, day='2026-10-10'):
            rid, token = str(uuid.uuid4()), str(uuid.uuid4())
            sql("insert into content_calendar(id,gym_id,account,post_date,status,variant_status,"
                "image_url,source_media_url,visual_group_key,publish_claim_token,publish_reservation_day)"
                f" values('{rid}','{tenant}','instagram','{day}','publishing','active',"
                f"'{url}','{url}','{group}','{token}','{day}');")
            return rid, token

        def seed(fp=None):
            tenant = str(uuid.uuid4())
            group = 'vg_' + uuid.uuid4().hex
            url = 'https://scratch.example/' + uuid.uuid4().hex
            fp = fp or 'md5:' + uuid.uuid4().hex
            rec = str(uuid.uuid4())
            sql(f"insert into tenant_alias values('{tenant}','{tenant}',now());"
                f"insert into visual_group(gym_id,group_key) values('{tenant}','{group}');"
                "insert into visual_global_object_read_receipt"
                "(receipt_id,tenant_id,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,observed_by)"
                f"values('{rec}','{tenant}','{url}','{fp}',10,'verified_object_read','scratch','test');"
                "insert into visual_global_object_attestation"
                "(tenant_id,group_key,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,read_receipt,attested_by)"
                f"values('{tenant}','{group}','{url}','{fp}',10,'verified_object_read','scratch','{rec}','test');"
                "insert into visual_global_scene_object_member(tenant_id,group_key,exact_url,fingerprint,object_role)"
                f"values('{tenant}','{group}','{url}','{fp}','source'),"
                f"('{tenant}','{group}','{url}','{fp}','delivered');")
            return tenant, group, url, fp

        try:
            sql("create role anon; create role authenticated; create role service_role;"
                "create table content_calendar(id uuid primary key,gym_id text,account text,post_date date,"
                "status text,variant_status text,published_at timestamptz,publish_claim_token uuid,"
                "publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,"
                "source_media_url text,source_media_asset_id text,drive_file_id text,byte_hash text,"
                "r2_key text,media_not_ready_reason text);"
                "create table media_asset(id text primary key,gym_id text,content_hash text);"
                "create function visual_group_row_active(content_calendar) returns boolean language sql as $$select false$$;"
                "create function visual_group_row_ambiguous(content_calendar) returns boolean language sql as $$select false$$;")
            for migration in ('DRAFT_visual_group_schema_20261002.sql',
                              'DRAFT_visual_group_claim_trigger_20261002.sql',
                              'DRAFT_visual_global_history_20261002.sql',
                              'DRAFT_fixer_forward_media_claim_20261006.sql'):
                sql((ROOT / 'migrations' / migration).read_text())
            tenant, group, url, fp = seed()
            first = row(tenant, group, url)
            assert 'OFF' in claim(*first, ok=False)
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{tenant}',true);")
            assert claim(*first) == 't'
            assert claim(*first) == 't'
            sibling = row(tenant, group, url)
            assert claim(*sibling) == 't'
            other_date = row(tenant, group, url, '2026-10-11')
            assert 'another date/group' in claim(*other_date, ok=False)
            # A newly rendered derivative still consumes the original bytes.
            delivered_url = 'https://scratch.example/' + uuid.uuid4().hex
            delivered_fp = 'md5:' + '0' * 32  # sorts before original: exercises rollback after insert
            read = str(uuid.uuid4())
            render = str(uuid.uuid4())
            source_read = sql(f"select read_receipt from visual_global_object_attestation where exact_url='{url}';")
            sql("insert into visual_global_object_read_receipt"
                "(receipt_id,tenant_id,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,observed_by)"
                f"values('{read}','{tenant}','{delivered_url}','{delivered_fp}',20,'verified_object_read','scratch','test');"
                "insert into visual_global_object_attestation"
                "(tenant_id,group_key,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,read_receipt,attested_by)"
                f"values('{tenant}','{group}','{delivered_url}','{delivered_fp}',20,'verified_object_read','scratch','{read}','test');"
                "insert into visual_global_scene_object_member(tenant_id,group_key,exact_url,fingerprint,object_role)"
                f"values('{tenant}','{group}','{delivered_url}','{delivered_fp}','delivered');"
                "insert into visual_global_render_receipt"
                "(receipt_id,tenant_id,source_read_receipt,delivered_read_receipt,source_exact_url,delivered_exact_url,"
                "source_fingerprint,delivered_fingerprint,operation,evidence_ref,rendered_by)"
                f"values('{render}','{tenant}','{source_read}','{read}','{url}','{delivered_url}',"
                f"'{fp}','{delivered_fp}','render','scratch','test');"
                "insert into visual_global_object_lineage"
                "(tenant_id,group_key,source_exact_url,delivered_exact_url,source_fingerprint,delivered_fingerprint,render_receipt)"
                f"values('{tenant}','{group}','{url}','{delivered_url}','{fp}','{delivered_fp}','{render}');")
            derived = row(tenant,group,url,'2026-10-11')
            sql(f"update content_calendar set image_url='{delivered_url}' where id='{derived[0]}';")
            assert 'another date/group' in claim(*derived,ok=False)
            # Failure must roll back the new rendition byte as well.
            assert sql(f"select count(*) from fixer_forward_media_use_20261006 where fingerprint='{delivered_fp}';") == '0'
            derived_sibling = row(tenant,group,url)
            sql(f"update content_calendar set image_url='{delivered_url}' where id='{derived_sibling[0]}';")
            assert claim(*derived_sibling) == 't'
            assert 'ownership' in claim(first[0], str(uuid.uuid4()), ok=False)
            assert 'permission denied' in sql("set role authenticated; select fixer_claim_forward_media_20261006"
                                             f"('{first[0]}','{first[1]}');", ok=False)
            assert 'permission denied' in sql("set role service_role; insert into fixer_forward_media_claim_gate_20261006"
                                             f" values('{uuid.uuid4()}',true);", ok=False)
            assert 'permission denied' in sql("set role service_role; delete from fixer_forward_media_use_20261006;", ok=False)
            assert 'immutable' in sql("delete from fixer_forward_media_use_20261006;", ok=False)
            # Unknown source still fails even when the displayed object is attested.
            unknown = row(tenant, group, url)
            sql(f"update content_calendar set source_media_url=null where id='{unknown[0]}';")
            assert 'owner attested' in claim(*unknown, ok=False)
            # Persisted token alone cannot repair altered receipt identity.
            sql(f"update content_calendar set post_date='2026-10-12',publish_reservation_day='2026-10-12' where id='{first[0]}';")
            assert 'receipt differs' in claim(*first, ok=False)
            # Cross-tenant byte use is isolated; raw aliases resolve canonically.
            t2, g2, u2, _ = seed(fp)
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{t2}',true);")
            assert claim(*row(t2, g2, u2, '2026-10-11')) == 't'
            sql(f"insert into tenant_alias values('old-gym-key','{tenant}',now());")
            assert 'another date/group' in claim(*row('old-gym-key',group,url,'2026-10-13'), ok=False)
            # Same-day unrelated logical group is not a permitted sibling.
            t3, g3, u3, fp3 = seed()
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{t3}',true);")
            assert claim(*row(t3,g3,u3)) == 't'
            g4 = 'vg_' + uuid.uuid4().hex
            u4 = 'https://scratch.example/' + uuid.uuid4().hex
            rec4 = str(uuid.uuid4())
            sql(f"insert into visual_group(gym_id,group_key) values('{t3}','{g4}');"
                "insert into visual_global_object_read_receipt"
                "(receipt_id,tenant_id,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,observed_by)"
                f"values('{rec4}','{t3}','{u4}','{fp3}',10,'verified_object_read','scratch','test');"
                "insert into visual_global_object_attestation"
                "(tenant_id,group_key,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,read_receipt,attested_by)"
                f"values('{t3}','{g4}','{u4}','{fp3}',10,'verified_object_read','scratch','{rec4}','test');"
                "insert into visual_global_scene_object_member(tenant_id,group_key,exact_url,fingerprint,object_role)"
                f"values('{t3}','{g4}','{u4}','{fp3}','source'),('{t3}','{g4}','{u4}','{fp3}','delivered');")
            assert 'another date/group' in claim(*row(t3,g4,u4), ok=False)
            # Deterministic overlap: first txn holds byte occupancy for 1s while
            # second date races it. Exactly one transaction can consume the byte.
            tr, gr, ur, _ = seed()
            sql(f"insert into fixer_forward_media_claim_gate_20261006 values('{tr}',true);")
            a, b = row(tr,gr,ur), row(tr,gr,ur,'2026-10-11')
            def race(pair):
                return subprocess.run(base, input="begin; set role service_role; select "
                    f"fixer_claim_forward_media_20261006('{pair[0]}','{pair[1]}');"
                    "select pg_sleep(1); commit;", text=True,capture_output=True,timeout=15)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(race,(a,b)))
            assert sum(r.returncode == 0 for r in results) == 1, [r.stderr for r in results]
            assert sql(f"select count(*) from fixer_forward_media_claim_receipt_20261006 where tenant_id='{tr}';") == '1'
            # A first forward claim cannot adopt an already delivered row.
            delivered = row(tenant,group,url)
            sql(f"update content_calendar set late_post_id='provider-id' where id='{delivered[0]}';")
            assert 'unsent' in claim(*delivered,ok=False)
            # Outage is an exception, never a silent allowed decision.
            sql("alter table fixer_forward_media_use_20261006 rename to temporarily_unavailable;")
            assert 'does not exist' in claim(*sibling,ok=False)
            fresh = row(tenant,group,url)
            assert 'does not exist' in claim(*fresh,ok=False)
            print('PASS: default OFF, ownership, idempotency, siblings, cross-date/group denial, tenant aliases/isolation, ACL, permanence, unknown source, changed receipt, concurrent single winner, sent-row refusal, ledger outage, original-source render reuse and atomic multi-byte rollback')
        finally:
            subprocess.run(['pg_ctl','-D',str(data),'-m','immediate','-w','stop'],
                           capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
