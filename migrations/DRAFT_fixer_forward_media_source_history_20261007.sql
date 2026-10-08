-- DRAFT / UNAPPLIED / DEFAULT OFF. No production writes/provisioning authorized.
-- Requires forward authority draft + ACTUAL media_asset/media_source columns.
-- Source owner verifies original Drive bytes and exact hosted source bytes.
-- Historical receipts require a separately trusted independent byte audit.
-- No automatic historical importer and NO cleared_unused path for old assets.
-- Today's download, content_hash, used_count and producer observations are never
-- historical proof. Missing/changed historical snapshots remain unknown/held.
-- Snapshot RPC takes NO graph/row locks. END read transaction before remote I/O.
-- Final source append: graph EXCLUSIVE before calendar/asset/source row locks;
-- recheck revision/binding, append source receipt + authority/outcome atomically.
-- Rollback before use: remove new empty objects. After use preserve all receipts.
begin;

create table public.fixer_forward_media_source_receipt_20261007 (
 receipt_ref text primary key check(receipt_ref ~ '^source-receipt:sha256:[0-9a-f]{64}$'),
 calendar_row_id uuid not null, row_revision text not null check(row_revision ~ '^[0-9a-f]{32}$'),
 binding_revision text not null check(binding_revision ~ '^[0-9a-f]{32}$'), tenant_id text not null, source_asset_id text not null,
 source_id text not null, folder_id text not null, exact_source_url text not null check(exact_source_url ~ '^https://[^[:space:]]+$'),
 source_fingerprint text not null check(source_fingerprint ~ '^md5:[0-9a-f]{32}$'),
 source_sha256 text not null check(source_sha256 ~ '^sha256:[0-9a-f]{64}$'),
 source_length bigint not null check(source_length between 1 and 134217728),
 evidence_json text not null, recorded_at timestamptz not null default clock_timestamp()
);
-- The audit role is UNPROVISIONED and has no runtime/factory. An independent
-- reviewer must verify durable evidence proving these were the actual original
-- bytes at publication, then bind the exact published row snapshot. Fresh Drive
-- downloads and current metadata cannot satisfy that historical evidence.
create role fixer_forward_media_history_auditor_20261007 nologin;
grant usage on schema public to fixer_forward_media_history_auditor_20261007;
create table public.fixer_forward_media_historical_original_20261007 (
 calendar_row_id uuid primary key,
 calendar_revision text not null check(calendar_revision ~ '^[0-9a-f]{32}$'),
 tenant_id text not null, source_asset_id text not null, exact_source_url text not null,
 source_fingerprint text not null check(source_fingerprint ~ '^md5:[0-9a-f]{32}$'),
 source_sha256 text not null check(source_sha256 ~ '^sha256:[0-9a-f]{64}$'),
 source_length bigint not null check(source_length between 1 and 134217728),
 historical_evidence_ref text not null check(btrim(historical_evidence_ref)<>''),
 audited_at timestamptz not null default clock_timestamp()
);
create index fixer_forward_historical_original_hash_20261007
 on public.fixer_forward_media_historical_original_20261007(source_fingerprint);
create table public.fixer_forward_media_history_query_receipt_20261007 (
 evidence_ref text primary key check(evidence_ref ~ '^history-query:sha256:[0-9a-f]{64}$'),
 source_receipt_ref text not null references public.fixer_forward_media_source_receipt_20261007(receipt_ref),
 result_json jsonb not null,
 recorded_at timestamptz not null default clock_timestamp()
);

do $$ declare t text; begin
 foreach t in array array['fixer_forward_media_source_receipt_20261007',
                         'fixer_forward_media_historical_original_20261007',
                         'fixer_forward_media_history_query_receipt_20261007'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006',t);
  execute format('create trigger immutable_row before update or delete on public.%I for each row execute function public.fixer_forward_media_immutable_20261006()',t);
  execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.fixer_forward_media_immutable_20261006()',t);
 end loop;
end; $$;
grant select,insert on public.fixer_forward_media_historical_original_20261007
 to fixer_forward_media_history_auditor_20261007;
create policy historical_auditor_read on public.fixer_forward_media_historical_original_20261007
 for select to fixer_forward_media_history_auditor_20261007 using(true);
create policy historical_auditor_insert on public.fixer_forward_media_historical_original_20261007
 for insert to fixer_forward_media_history_auditor_20261007 with check(true);

create function public.fixer_forward_media_historical_bind_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype;
begin
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 select * into r from public.content_calendar where id=new.calendar_row_id for share;
 if not found or (r.status is distinct from 'published' and r.published_at is null and r.late_post_id is null)
   or new.calendar_revision is distinct from md5(to_jsonb(r)::text)
   or new.tenant_id is distinct from r.gym_id
   or new.source_asset_id is distinct from r.source_media_asset_id
   or new.exact_source_url is distinct from r.source_media_url then
  raise exception 'exact published historical binding required' using errcode='23514';
 end if;
 return new;
end; $$;
revoke all on function public.fixer_forward_media_historical_bind_20261007() from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006;
create trigger historical_binding before insert on public.fixer_forward_media_historical_original_20261007
 for each row execute function public.fixer_forward_media_historical_bind_20261007();

create function public.fixer_forward_media_source_snapshot_20261007(p_id uuid,p_revision text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; a public.media_asset%rowtype; s public.media_source%rowtype;
begin
 select * into r from public.content_calendar where id=p_id;
 if not found or md5(to_jsonb(r)::text) is distinct from p_revision then
  raise exception 'source canonical revision changed' using errcode='23514'; end if;
 select * into a from public.media_asset where id=r.source_media_asset_id;
 if not found or a.gym_id is distinct from r.gym_id then
  raise exception 'source asset tenant mismatch' using errcode='23514'; end if;
 select * into s from public.media_source where id=a.source_id;
 if not found or s.gym_id is distinct from r.gym_id or s.active is distinct from true
   or s.kind is distinct from 'gym_drive' or nullif(btrim(s.folder_id),'') is null
   or r.source_media_url is null or r.source_media_url !~ '^https://[^[:space:]]+$' then
  raise exception 'same gym active Drive source required' using errcode='23514'; end if;
 return jsonb_build_object('calendar',to_jsonb(r),'asset',to_jsonb(a),'source',to_jsonb(s),
   'revision',p_revision,'binding_revision',md5(jsonb_build_array(to_jsonb(a),to_jsonb(s))::text));
end; $$;

create function public.fixer_forward_media_source_record_20261007(
 p_id uuid,p_revision text,p_binding text,p_ref text,p_evidence_json text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare snapshot jsonb; e jsonb; old public.fixer_forward_media_source_receipt_20261007%rowtype;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'source authority requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform 1 from public.content_calendar where id=p_id for update;
 perform 1 from public.media_asset a join public.content_calendar r on r.source_media_asset_id=a.id where r.id=p_id for update of a;
 perform 1 from public.media_source s join public.media_asset a on a.source_id=s.id join public.content_calendar r on r.source_media_asset_id=a.id where r.id=p_id for share of s;
 snapshot:=public.fixer_forward_media_source_snapshot_20261007(p_id,p_revision);
 e:=p_evidence_json::jsonb;
 if p_evidence_json is null or octet_length(p_evidence_json)>16384 or jsonb_typeof(e)<>'object'
  or p_ref is distinct from 'source-receipt:sha256:'||encode(sha256(convert_to(p_evidence_json,'UTF8')),'hex')
  or snapshot->>'binding_revision' is distinct from p_binding
  or e->'schema_version' is distinct from '1'::jsonb or e->>'calendar_row_id' is distinct from p_id::text
  or e->>'row_revision' is distinct from p_revision or e->>'binding_revision' is distinct from p_binding
  or e->>'tenant_id' is distinct from snapshot#>>'{calendar,gym_id}'
  or e->>'source_asset_id' is distinct from snapshot#>>'{asset,id}'
  or e->>'source_id' is distinct from snapshot#>>'{source,id}'
  or e->>'folder_id' is distinct from snapshot#>>'{source,folder_id}'
  or e->>'exact_source_url' is distinct from snapshot#>>'{calendar,source_media_url}'
  or nullif(e->>'drive_version','') is null
  or jsonb_typeof(e->'drive_parent_path') is distinct from 'array'
  or jsonb_array_length(e->'drive_parent_path') not between 1 and 8
  or e#>>'{drive_parent_path,-1,id}' is distinct from snapshot#>>'{source,folder_id}' then
  raise exception 'exact independently verified source binding required' using errcode='23514'; end if;
 insert into public.fixer_forward_media_source_receipt_20261007
  (receipt_ref,calendar_row_id,row_revision,binding_revision,tenant_id,source_asset_id,source_id,folder_id,
   exact_source_url,source_fingerprint,source_sha256,source_length,evidence_json)
 values(p_ref,p_id,p_revision,p_binding,e->>'tenant_id',e->>'source_asset_id',e->>'source_id',e->>'folder_id',
  e->>'exact_source_url',e->>'source_fingerprint',e->>'source_sha256',(e->>'source_length')::bigint,p_evidence_json)
 on conflict(receipt_ref) do nothing;
 select * into old from public.fixer_forward_media_source_receipt_20261007 where receipt_ref=p_ref;
 if old.evidence_json is distinct from p_evidence_json then
  raise exception 'source receipt immutable conflict' using errcode='23514'; end if;
 return true;
end; $$;

create function public.fixer_forward_media_source_history_20261007(p_original jsonb,p_limit integer)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare receipt public.fixer_forward_media_source_receipt_20261007%rowtype;
 decision text; known_matches integer; unknown_rows integer; bounded_rows integer;
 evidence text; result jsonb; corpus_digest text; known_digest text;
begin
 if p_limit is null or p_limit not between 1 and 10000 then
  raise exception 'bounded historical lookup required' using errcode='23514'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'source history requires read committed' using errcode='25000'; end if;
 -- Final phase only; trusted history append and claims serialize here.
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 select * into receipt from public.fixer_forward_media_source_receipt_20261007
  where receipt_ref=p_original->>'registry_evidence_ref';
 if not found or p_original is distinct from jsonb_build_object('tenant_id',receipt.tenant_id,
    'source_asset_id',receipt.source_asset_id,'source_url',receipt.exact_source_url,
    'source_fingerprint',receipt.source_fingerprint,'source_length',receipt.source_length,
    'registry_evidence_ref',receipt.receipt_ref) then
  raise exception 'durable exact source receipt required' using errcode='23514'; end if;
 -- An independently audited original-byte match is a permanent USED signal,
 -- even if the corresponding calendar row was later edited/deleted. Metadata
 -- MD5 is absent from this index; all entries are trusted byte audit receipts.
 select count(*),md5(coalesce(string_agg(to_jsonb(matches)::text,'' order by calendar_row_id),''))
 into known_matches,known_digest from (select * from public.fixer_forward_media_historical_original_20261007
   where source_fingerprint=receipt.source_fingerprint order by calendar_row_id limit p_limit+1) matches;
 with bounded as (select * from public.content_calendar where status='published' or published_at is not null or late_post_id is not null order by id limit p_limit+1)
 select count(*),count(*) filter(where not exists(select 1 from public.fixer_forward_media_historical_original_20261007 h
    where h.calendar_row_id=r.id and h.calendar_revision=md5(to_jsonb(r)::text))),
    md5(coalesce(string_agg(r.id::text||md5(to_jsonb(r)::text),'' order by r.id),''))
 into bounded_rows,unknown_rows,corpus_digest from bounded r;
 decision:=case when known_matches>0 or exists(select 1 from public.fixer_forward_media_use_20261006
   where fingerprint=receipt.source_fingerprint) then 'hold_used' else 'hold_uncertain' end;
 evidence:='history-query:sha256:'||encode(sha256(convert_to(jsonb_build_object(
   'source_receipt',receipt.receipt_ref,'decision',decision,'known_matches',known_matches,
   'unknown_rows_in_bound',unknown_rows,'row_limit',p_limit,'limit_exceeded',bounded_rows>p_limit,
   'corpus_digest',corpus_digest,'known_digest',known_digest)::text,'UTF8')),'hex');
 result:=jsonb_build_object('original',p_original,'decision',decision,'history_evidence_ref',evidence,
   'known_matches',known_matches,'unknown_rows_in_bound',unknown_rows,'corpus_digest',corpus_digest,
   'limit_exceeded',bounded_rows>p_limit,'reason',case when decision='hold_used' then 'trusted_original_bytes_previously_used'
    when bounded_rows>p_limit then 'historical_scan_bound_exceeded'
    when unknown_rows>0 then 'historical_original_bytes_unknown'
    else 'preexisting_original_has_no_fresh_production_proof' end);
 insert into public.fixer_forward_media_history_query_receipt_20261007(evidence_ref,source_receipt_ref,result_json)
 values(evidence,receipt.receipt_ref,result) on conflict(evidence_ref) do nothing;
 return result;
end; $$;
revoke all on function public.fixer_forward_media_source_snapshot_20261007(uuid,text),
 public.fixer_forward_media_source_record_20261007(uuid,text,text,text,text),
 public.fixer_forward_media_source_history_20261007(jsonb,integer)
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_history_auditor_20261007;
grant execute on function public.fixer_forward_media_source_snapshot_20261007(uuid,text),
 public.fixer_forward_media_source_record_20261007(uuid,text,text,text,text),
 public.fixer_forward_media_source_history_20261007(jsonb,integer)
 to fixer_forward_media_owner_20261006;
commit;
