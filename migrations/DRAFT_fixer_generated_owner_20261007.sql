-- DRAFT / UNAPPLIED. Extends assembled forward/owner-photo authority only.
-- Existing isolated owner verifies Astra provider/review/storage receipts,
-- current verified palette/copy and local depletion; service cannot self-certify.
-- No new credentials, provider call, historical clearance or activation.
-- Remote reads finish before graph -> census -> row authority. Reservations are
-- permanent across deletion, failures and edits; same gym/date/logical siblings
-- may share an original, never another day or another tenant.
begin;
create table public.fixer_generated_reservation_20261007 (
 job_id uuid primary key, calendar_row_id uuid not null, group_key text not null,
 candidate_json jsonb not null, manifest_json jsonb not null,
 receipt_ref text unique not null,
 reserved_at timestamptz not null default clock_timestamp()
);
-- Exact historical byte reads by the existing trusted owner, bound to the
-- existing independent corpus judgment. No URL-only or metadata-only proof.
create table public.fixer_generated_history_visual_20261007 (
 history_key text not null, visual_sha256 text not null,
 published_binding_ref text not null, phash text not null
   check(phash ~ '^scene:phash64:[0-9a-f]{16}$'),
 visual_url text not null, primary key(history_key,visual_sha256,published_binding_ref)
);
do $$ declare t text; begin
 foreach t in array array['fixer_generated_reservation_20261007','fixer_generated_history_visual_20261007'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007',t);
  execute format('create trigger immutable_row before update or delete on public.%I for each row execute function public.fixer_forward_media_immutable_20261006()',t);
  execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.fixer_forward_media_immutable_20261006()',t);
 end loop;
end; $$;

create function public.fixer_generated_hamming_20261007(a text,b text)
returns integer language sql immutable strict set search_path=pg_catalog as $$
 select bit_count(('x'||right(a,16))::bit(64) # ('x'||right(b,16))::bit(64))::integer;
$$;

-- Add generated originals to the SAME corpus used by the photo auditor. Photos
-- cannot ignore a reserved/failed/published generated image. Forward claims are
-- resolved only when their entire persisted tuple matches a generated grant.
alter function public.fixer_forward_media_photo_snapshot_20261007() rename to fixer_pre_generated_snapshot_20261007;
create function public.fixer_forward_media_photo_snapshot_20261007()
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s jsonb; rows jsonb; grants jsonb;
begin
 s:=public.fixer_pre_generated_snapshot_20261007();
 select coalesce(jsonb_agg(to_jsonb(g) order by job_id),'[]'::jsonb) into grants
 from public.fixer_generated_reservation_20261007 g;
 select coalesce(jsonb_agg(item order by item->>'history_key'),'[]'::jsonb) into rows from (
  select case when g.job_id is not null then j||jsonb_build_object('resolved',true,'media_kind','still_photo',
    'visual_sha256','sha256:'||(g.candidate_json->>'original_sha256'),
    'visual_url',g.candidate_json->>'original_url') else j end item
  from jsonb_array_elements(s->'rows') j
  left join public.fixer_forward_media_claim_receipt_20261006 claim on j->>'history_key'='claim:'||claim.claim_token::text
  left join public.fixer_generated_reservation_20261007 g
    on claim.tenant_id=g.candidate_json->>'gym_id' and claim.post_date::text=g.candidate_json->>'local_date'
    and claim.group_key=g.group_key
    and claim.source_url=g.candidate_json->>'original_url' and claim.image_url=claim.source_url
    and claim.thumbnail_url is null
    and claim.fingerprints=array['md5:'||(g.candidate_json->>'original_md5')]
  union all
  select jsonb_build_object('history_key','generated-reserved:'||g.job_id::text,
   'resolved',true,'media_kind','still_photo','visual_sha256','sha256:'||(g.candidate_json->>'original_sha256'),
   'published_binding_ref',g.receipt_ref,'visual_url',g.candidate_json->>'original_url')
  from public.fixer_generated_reservation_20261007 g
 ) all_rows;
 return s||jsonb_build_object('rows',rows,
  'scope_complete',s->'scope_complete'='true'::jsonb and jsonb_array_length(rows)<=2500,
  'spine_digest','sha256:'||encode(sha256(convert_to(jsonb_build_object('spine',s->>'spine_digest','generated',grants)::text,'UTF8')),'hex'));
end; $$;

create function public.fixer_generated_snapshot_20261007(p_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; sources jsonb; assets jsonb; h jsonb;
 complete boolean; available integer; copy jsonb;
begin
 select * into r from public.content_calendar where id=p_id;
 if not found or nullif(btrim(r.gym_id),'') is null or r.post_date is null
  or nullif(btrim(r.visual_group_key),'') is null
  or (to_jsonb(r)->>'logical_post_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') is distinct from true then
  raise exception 'canonical generated content identity required' using errcode='23514'; end if;
 select coalesce(jsonb_agg(to_jsonb(s) order by id),'[]'::jsonb) into sources
 from public.media_source s where s.gym_id=r.gym_id and s.kind='gym_drive';
 select coalesce(jsonb_agg(to_jsonb(a) order by id),'[]'::jsonb) into assets
 from public.media_asset a where a.gym_id=r.gym_id;
 -- All connected sources must prove a successful, non-revoked, complete sync.
 -- A pending client photo is supply waiting for moderation, not depletion.
 complete:=not exists(select 1 from jsonb_array_elements(sources) s
  where s->'active' is distinct from 'true'::jsonb or coalesce(s->>'revoked_externally','false')<>'false'
    or s->>'sync_status' is distinct from 'ready' or nullif(s->>'sync_finished_at','') is null)
  and not exists(select 1 from jsonb_array_elements(assets) a
   where a->>'kind'='photo' and a->'eligible' is distinct from 'false'::jsonb
    and coalesce(a->>'excluded_by_coach','false')='false'
    and a->>'review_status'='pending_review' and a->>'moderation_status'='pending')
  and not exists(select 1 from jsonb_array_elements(assets) a
    where a->>'kind'='photo' and a->'eligible'='true'::jsonb
      and not exists(select 1 from jsonb_array_elements(sources) s where s->>'id'=a->>'source_id'));
 -- Conservative photo-first count: an approved usable photo with no proven
 -- permanent use blocks fallback. used_count alone never proves consumption.
 select count(*) into available from public.media_asset a
 join public.media_source s on s.id=a.source_id and s.gym_id=a.gym_id
 where a.gym_id=r.gym_id and to_jsonb(a)->>'kind'='photo'
  and public.fixer_owner_photo_source_ready_20261007(a.id,'sha256:'||(to_jsonb(a)#>>'{moderation_json,sha256}'))
  and not exists(select 1 from public.fixer_forward_media_use_20261006 u where u.fingerprint='md5:'||a.content_hash)
  and not exists(select 1 from public.fixer_forward_media_historical_original_20261007 u where u.source_fingerprint='md5:'||a.content_hash)
  and not exists(select 1 from public.fixer_owner_photo_reservation_20261007 g
   where g.candidate_json->>'source_fingerprint'='md5:'||a.content_hash);
 h:=public.fixer_generated_history_snapshot_20261007();
 copy:=jsonb_build_object('gym_id',r.gym_id,'local_date',r.post_date,'logical_post_id',to_jsonb(r)->>'logical_post_id','group_key',r.visual_group_key,'caption',r.caption);
 return jsonb_build_object('gym_id',r.gym_id,'local_date',r.post_date,'logical_post_id',to_jsonb(r)->>'logical_post_id','group_key',r.visual_group_key,
  'copy',copy,'copy_revision','sha256:'||encode(sha256(convert_to(copy::text,'UTF8')),'hex'),
  'inventory_revision','sha256:'||encode(sha256(convert_to(jsonb_build_object('sources',sources,'assets',assets)::text,'UTF8')),'hex'),
  'photo_inventory_complete',complete,'eligible_photo_count',available,
  'history_revision',h->>'spine_digest','history',h,
  -- This proves the complete delivered-visual inventory, NOT historical source
  -- originals. The owner must fetch every entry and submit exact byte/pHash
  -- evidence before reservation. Missing source IDs do not starve fresh art.
  'history_complete',h->'scope_complete');
end; $$;

-- Generated originals are newly authenticated provider outputs. Their relevant
-- historical similarity corpus is actual delivered visuals, not unavailable
-- source originals of old photos. This never grants old-photo clearance.
create function public.fixer_generated_history_snapshot_20261007()
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare rows jsonb; spine text;
begin
 with raw as (
  select 'calendar-image:'||r.id::text key,r.gym_id tenant,r.post_date content_day,r.visual_group_key grp,
   r.image_url url,md5(jsonb_build_array(r.gym_id,r.post_date,r.visual_group_key,r.image_url,r.thumbnail_url)::text) binding
  from public.content_calendar r where r.status='published' or r.published_at is not null or r.late_post_id is not null
  union all
  select 'calendar-thumbnail:'||r.id::text,r.gym_id,r.post_date,r.visual_group_key,r.thumbnail_url,
   md5(jsonb_build_array(r.gym_id,r.post_date,r.visual_group_key,r.image_url,r.thumbnail_url)::text)
  from public.content_calendar r where (r.status='published' or r.published_at is not null or r.late_post_id is not null) and r.thumbnail_url is not null
  union all
  select 'claim-'||object.kind||':'||claim.claim_token::text,claim.tenant_id,claim.post_date,claim.group_key,object.url,
   md5(to_jsonb(claim)::text) from public.fixer_forward_media_claim_receipt_20261006 claim
  cross join lateral (values('source',claim.source_url),('image',claim.image_url),('thumbnail',claim.thumbnail_url)) object(kind,url) where object.url is not null
  union all
  select 'photo-reserved-'||object.kind||':'||r.audit_id::text,r.candidate_json->>'tenant_id',
   (r.candidate_json->>'post_date')::date,r.candidate_json->>'group_key',object.url,r.receipt_ref
  from public.fixer_owner_photo_reservation_20261007 r cross join lateral
   (values('source',r.candidate_json->>'source_url'),('image',r.candidate_json->>'image_url'),('thumbnail',r.candidate_json->>'thumbnail_url')) object(kind,url) where object.url is not null
  union all
  select 'generated-reserved:'||r.job_id::text,r.candidate_json->>'gym_id',(r.candidate_json->>'local_date')::date,
   r.group_key,r.candidate_json->>'original_url',r.receipt_ref from public.fixer_generated_reservation_20261007 r
 ), bound as (
  select jsonb_build_object('history_key',key,'tenant_id',tenant,'local_date',content_day,'group_key',grp,
   'published_binding_ref',binding,'visual_url',url,'visual_sha256',v.visual_sha256) item
  from raw left join public.fixer_generated_history_visual_20261007 v
    on v.history_key=raw.key and v.published_binding_ref=raw.binding and v.visual_url=raw.url
 ) select coalesce(jsonb_agg(item order by item->>'history_key'),'[]'::jsonb) into rows from bound;
 -- Remove observed byte SHA from the spine: recording exact current visuals
 -- cannot invalidate the stable calendar/claim/reservation tuple revision.
 select 'sha256:'||encode(sha256(convert_to(coalesce(jsonb_agg(value-'visual_sha256' order by value->>'history_key'),'[]'::jsonb)::text,'UTF8')),'hex')
 into spine from jsonb_array_elements(rows);
 return jsonb_build_object('rows',rows,'spine_digest',spine,
  'scope_complete',jsonb_array_length(rows)<=10000 and not exists(select 1 from jsonb_array_elements(rows) h
    where (h->>'visual_url' ~ '^https://[^[:space:]]+$') is distinct from true));
end; $$;

-- Use nonblocking shared graph/census acquisition on ordinary inventory DML:
-- do not deadlock a writer which already holds unrelated calendar row locks.
-- Owner reserve has graph exclusive FIRST; inventory cannot change beneath it.
create function public.fixer_generated_inventory_lock_20261007()
returns trigger language plpgsql set search_path=pg_catalog,public as $$
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated inventory requires read committed' using errcode='25000'; end if;
 if not pg_try_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0))
  or not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0)) then
  raise exception 'generated inventory authority busy; retry database mutation only' using errcode='40001'; end if;
 return null;
end; $$;
create trigger generated_inventory_lock before insert or update or delete or truncate on public.media_asset
 for each statement execute function public.fixer_generated_inventory_lock_20261007();
create trigger generated_inventory_lock before insert or update or delete or truncate on public.media_source
 for each statement execute function public.fixer_generated_inventory_lock_20261007();

-- Existing owner is the trust boundary for authenticated generation + exact
-- copy/palette + historical byte reads. No service/auditor self-approval API.
create function public.fixer_reserve_generated_20261007(p_id uuid,c jsonb,visuals jsonb,m jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; h jsonb; proof jsonb; prior public.fixer_generated_reservation_20261007%rowtype;
 r public.content_calendar%rowtype; receipt text; original jsonb; clearance jsonb; fp text; asset text;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
  or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated reservation requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into r from public.content_calendar where id=p_id for update;
 snap:=public.fixer_generated_snapshot_20261007(p_id);
 if jsonb_typeof(c)<>'object' or c->'schema_version' is distinct from '1'::jsonb
  or c->>'source_type' is distinct from 'generated_astra_infographic' or c->>'provider' is distinct from 'astra'
  or c->>'model' is distinct from 'gpt-6-astra' or c->>'job_id' is null
  or c->>'gym_id' is distinct from snap->>'gym_id' or c->>'local_date' is distinct from snap->>'local_date'
  or c->>'logical_post_id' is distinct from snap->>'logical_post_id'
  or c->>'copy_revision' is distinct from snap->>'copy_revision'
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or snap->'eligible_photo_count' is distinct from '0'::jsonb
  or snap->'history_complete' is distinct from 'true'::jsonb
  or (c->>'original_sha256' ~ '^[0-9a-f]{64}$') is distinct from true
  or (c->>'original_md5' ~ '^[0-9a-f]{32}$') is distinct from true
  or (c->>'original_phash' ~ '^scene:phash64:[0-9a-f]{16}$') is distinct from true
  or c->>'storage_readback_sha256' is distinct from c->>'original_sha256'
  or (c->>'original_url' ~ '^https://[^[:space:]]+$') is distinct from true
  or jsonb_typeof(c->'original_length') is distinct from 'number'
  or (c->>'original_length')::bigint not between 1 and 134217728
  or exists(select 1 from unnest(array['palette_revision','copy_digest','palette_digest','provider_response_id','provider_output_id','storage_key','review_response_id','review_policy_id']) key where nullif(btrim(c->>key),'') is null) then
  raise exception 'complete current generated original authority required' using errcode='23514'; end if;
 select * into prior from public.fixer_generated_reservation_20261007 where job_id=(c->>'job_id')::uuid;
 if found then
  if prior.candidate_json is distinct from c or prior.manifest_json is distinct from m then
   raise exception 'generated job immutable identity conflict' using errcode='23514'; end if;
  if prior.group_key is distinct from r.visual_group_key then
   raise exception 'generated sibling group changed' using errcode='23514'; end if;
  if prior.calendar_row_id is distinct from p_id then
   if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'active'
    or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null then
    raise exception 'generated sibling must be unsent' using errcode='23514'; end if;
   update public.content_calendar set source_media_asset_id='generated-astra:'||(c->>'job_id'),source_media_url=c->>'original_url',image_url=c->>'original_url',thumbnail_url=null,render_manifest_digest=m->>'manifest_digest' where id=p_id;
  elsif r.source_media_asset_id is distinct from 'generated-astra:'||(c->>'job_id')
    or r.source_media_url is distinct from c->>'original_url' or r.image_url is distinct from c->>'original_url'
    or r.thumbnail_url is not null or r.render_manifest_digest is distinct from m->>'manifest_digest' then
   raise exception 'generated replay binding changed' using errcode='23514'; end if;
  perform public.fixer_generated_runtime_check_20261007(p_id);
  return jsonb_build_object('reserved',true,'replayed',true,'receipt_ref',prior.receipt_ref,'manifest',m);
 end if;
 if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'active'
  or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null
  or c->>'inventory_revision' is distinct from snap->>'inventory_revision'
  or c->>'history_revision' is distinct from snap->>'history_revision'
  or jsonb_typeof(visuals) is distinct from 'array' or jsonb_array_length(visuals)<>jsonb_array_length(snap#>'{history,rows}') then
  raise exception 'generated candidate stale or unsent depletion unavailable' using errcode='23514'; end if;
 for h in select value from jsonb_array_elements(snap#>'{history,rows}') loop
  select value into proof from jsonb_array_elements(visuals) v(value)
   where value->>'history_key'=h->>'history_key'
     and (h->>'visual_sha256' is null or value->>'visual_sha256'=h->>'visual_sha256')
     and value->>'published_binding_ref'=h->>'published_binding_ref';
  if not found or (proof->>'phash' ~ '^scene:phash64:[0-9a-f]{16}$') is distinct from true
   or (proof->>'visual_sha256' ~ '^sha256:[0-9a-f]{64}$') is distinct from true
   or proof->>'visual_url' is distinct from h->>'visual_url'
   or public.fixer_generated_hamming_20261007(c->>'original_phash',proof->>'phash')<=6
   or 'sha256:'||(c->>'original_sha256')=proof->>'visual_sha256' then
   -- Explicit same-date logical siblings can use their already-reserved source.
   if not exists(select 1 from public.fixer_generated_reservation_20261007 g
    where h->>'history_key'='generated-reserved:'||g.job_id::text
     and g.candidate_json->>'gym_id'=c->>'gym_id' and g.candidate_json->>'local_date'=c->>'local_date'
     and g.candidate_json->>'logical_post_id'=c->>'logical_post_id'
     and g.candidate_json->>'original_sha256'=c->>'original_sha256'
     and g.candidate_json->>'original_url'=c->>'original_url') then
    raise exception 'unresolved or repeated historical generated visual' using errcode='23514'; end if;
  end if;
  insert into public.fixer_generated_history_visual_20261007 values(h->>'history_key',proof->>'visual_sha256',h->>'published_binding_ref',proof->>'phash',proof->>'visual_url') on conflict do nothing;
  if not exists(select 1 from public.fixer_generated_history_visual_20261007 v
    where v.history_key=h->>'history_key' and v.visual_sha256=proof->>'visual_sha256'
     and v.published_binding_ref=h->>'published_binding_ref' and v.phash=proof->>'phash'
     and v.visual_url=proof->>'visual_url') then
   raise exception 'historical perceptual receipt identity conflict' using errcode='23514'; end if;
 end loop;
 if exists(select 1 from public.fixer_generated_reservation_20261007 g
  where (g.candidate_json->>'original_sha256'=c->>'original_sha256'
   or g.candidate_json->>'original_md5'=c->>'original_md5'
   or g.candidate_json->>'original_url'=c->>'original_url'
   or public.fixer_generated_hamming_20261007(g.candidate_json->>'original_phash',c->>'original_phash')<=6)
   and not(g.candidate_json->>'gym_id'=c->>'gym_id' and g.candidate_json->>'local_date'=c->>'local_date'
    and g.candidate_json->>'logical_post_id'=c->>'logical_post_id'))
  or exists(select 1 from public.fixer_forward_media_history_clearance_20261006 negative
    where negative.source_fingerprint='md5:'||(c->>'original_md5') and negative.decision<>'cleared_unused')
  or exists(select 1 from public.fixer_forward_media_use_20261006 u where u.fingerprint='md5:'||(c->>'original_md5')
    and not(u.tenant_id=c->>'gym_id' and u.post_date::text=c->>'local_date' and u.group_key=r.visual_group_key)) then
  raise exception 'generated source already reserved by another tenant/date/group' using errcode='23514'; end if;
 fp:='md5:'||(c->>'original_md5'); asset:='generated-astra:'||(c->>'job_id');
 receipt:='generated-reservation:sha256:'||encode(sha256(convert_to(c::text,'UTF8')),'hex');
 if m is distinct from jsonb_build_object('manifest_digest',m->>'manifest_digest','tenant_id',c->>'gym_id','source_asset_id',asset,
  'image_url',c->>'original_url','image_fingerprint',fp,'image_length',(c->>'original_length')::bigint,
  'thumbnail_url',null,'thumbnail_fingerprint',null,'thumbnail_length',null,'operation','same_object','render_recipe',null,'render_evidence_ref',asset)
  or m->>'manifest_digest' is distinct from 'sha256:'||encode(sha256(convert_to(public.fixer_owner_photo_canonical_20261007(m-'manifest_digest'),'UTF8')),'hex') then
  raise exception 'generated original requires exact same-object manifest' using errcode='23514'; end if;
 insert into public.fixer_generated_reservation_20261007(job_id,calendar_row_id,group_key,candidate_json,manifest_json,receipt_ref)
 values((c->>'job_id')::uuid,p_id,r.visual_group_key,c,m,receipt);
 insert into public.fixer_forward_media_original_registry_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)
 values(c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'astra-job:'||(c->>'job_id'));
 insert into public.fixer_forward_media_history_clearance_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref)
 values(c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'astra-job:'||(c->>'job_id'),'cleared_unused',receipt);
 insert into public.fixer_forward_media_render_manifest_20261006(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,operation,render_recipe,render_evidence_ref)
 values(m->>'manifest_digest',c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'same_object',null,asset);
 update public.content_calendar set source_media_asset_id=asset,source_media_url=c->>'original_url',image_url=c->>'original_url',thumbnail_url=null,render_manifest_digest=m->>'manifest_digest' where id=p_id;
 return jsonb_build_object('reserved',true,'replayed',false,'receipt_ref',receipt,'manifest',m);
end; $$;

-- Extend positive clearance guard without granting table insert to a producer.
create or replace function public.fixer_guard_positive_owner_photo_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if new.decision='cleared_unused' and not exists(select 1 from public.fixer_owner_photo_reservation_20261007 r
  where r.receipt_ref=new.history_evidence_ref and r.original_json=jsonb_build_object('tenant_id',new.tenant_id,'source_asset_id',new.source_asset_id,'source_url',new.source_url,'source_fingerprint',new.source_fingerprint,'source_length',new.source_length,'registry_evidence_ref',new.registry_evidence_ref))
  and not exists(select 1 from public.fixer_generated_reservation_20261007 g where g.receipt_ref=new.history_evidence_ref
   and new.tenant_id=g.candidate_json->>'gym_id' and new.source_asset_id='generated-astra:'||(g.candidate_json->>'job_id')
   and new.source_url=g.candidate_json->>'original_url' and new.source_fingerprint='md5:'||(g.candidate_json->>'original_md5')
   and new.source_length=(g.candidate_json->>'original_length')::bigint and new.registry_evidence_ref='astra-job:'||(g.candidate_json->>'job_id')) then
  raise exception 'positive clearance requires exact owner reservation' using errcode='23514'; end if;
 return new;
end; $$;

create function public.fixer_generated_runtime_check_20261007(p_id uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; g public.fixer_generated_reservation_20261007%rowtype; snap jsonb; h jsonb; ph text;
begin
 select * into r from public.content_calendar where id=p_id;
 select * into g from public.fixer_generated_reservation_20261007
  where 'generated-astra:'||(candidate_json->>'job_id')=r.source_media_asset_id and candidate_json->>'gym_id'=r.gym_id;
 if not found then
  if r.source_media_asset_id like 'generated-astra:%' then raise exception 'generated source reservation missing' using errcode='23514'; end if;
  return true;
 end if;
 snap:=public.fixer_generated_snapshot_20261007(p_id);
 if g.candidate_json->>'local_date' is distinct from r.post_date::text
  or g.candidate_json->>'logical_post_id' is distinct from to_jsonb(r)->>'logical_post_id'
  or g.group_key is distinct from r.visual_group_key
  or g.candidate_json->>'copy_revision' is distinct from snap->>'copy_revision'
  or g.candidate_json->>'inventory_revision' is distinct from snap->>'inventory_revision'
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or snap->'eligible_photo_count' is distinct from '0'::jsonb
  or snap->'history_complete' is distinct from 'true'::jsonb
  or g.candidate_json->>'original_url' is distinct from r.source_media_url
  or r.source_media_url is distinct from r.image_url or r.thumbnail_url is not null
  or g.manifest_json->>'manifest_digest' is distinct from r.render_manifest_digest then
  raise exception 'generated current content/depletion/history binding changed' using errcode='23514'; end if;
 for h in select value from jsonb_array_elements(snap#>'{history,rows}') loop
  -- Known same logical generated siblings, including their committed claim.
  if exists(select 1 from public.fixer_generated_reservation_20261007 sibling
    where sibling.candidate_json->>'gym_id'=g.candidate_json->>'gym_id'
     and sibling.candidate_json->>'local_date'=g.candidate_json->>'local_date'
     and sibling.candidate_json->>'logical_post_id'=g.candidate_json->>'logical_post_id'
     and (h->>'visual_sha256' is null or 'sha256:'||(sibling.candidate_json->>'original_sha256')=h->>'visual_sha256')
     and h->>'tenant_id'=sibling.candidate_json->>'gym_id'
     and h->>'local_date'=sibling.candidate_json->>'local_date' and h->>'group_key'=sibling.group_key
     and h->>'visual_url'=sibling.candidate_json->>'original_url'
     and (h->>'history_key'='generated-reserved:'||sibling.job_id::text
      or exists(select 1 from public.fixer_forward_media_claim_receipt_20261006 claim
        where h->>'history_key' in ('claim-source:'||claim.claim_token::text,'claim-image:'||claim.claim_token::text)
         and claim.tenant_id=sibling.candidate_json->>'gym_id'
         and claim.post_date::text=sibling.candidate_json->>'local_date'
         and claim.group_key=sibling.group_key
         and claim.source_url=sibling.candidate_json->>'original_url'
         and claim.image_url=claim.source_url and claim.thumbnail_url is null)
      or exists(select 1 from public.content_calendar live where h->>'history_key'='calendar-image:'||live.id::text
        and live.gym_id=sibling.candidate_json->>'gym_id' and live.post_date::text=sibling.candidate_json->>'local_date'
        and live.visual_group_key=sibling.group_key and live.source_media_asset_id='generated-astra:'||sibling.job_id::text
        and live.image_url=sibling.candidate_json->>'original_url' and live.thumbnail_url is null))) then continue; end if;
  select phash into ph from public.fixer_generated_history_visual_20261007 v where v.history_key=h->>'history_key'
   and v.visual_sha256=h->>'visual_sha256' and v.published_binding_ref=h->>'published_binding_ref';
  if not found then
   select candidate_json->>'original_phash' into ph from public.fixer_generated_reservation_20261007 sibling
    where h->>'history_key'='generated-reserved:'||sibling.job_id::text;
  end if;
  if ph is null or public.fixer_generated_hamming_20261007(ph,g.candidate_json->>'original_phash')<=6
   or h->>'visual_sha256'='sha256:'||(g.candidate_json->>'original_sha256') then
   raise exception 'generated historical perceptual identity unresolved or repeated' using errcode='23514'; end if;
 end loop;
 return true;
end; $$;
alter function public.fixer_forward_media_provenance_lookup_20261006(uuid) rename to fixer_pre_generated_provenance_20261007;
create function public.fixer_forward_media_provenance_lookup_20261006(p_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare p jsonb;
begin
 -- Existing lookup acquires correct graph/census locks before generated check.
 p:=public.fixer_pre_generated_provenance_20261007(p_id);
 perform public.fixer_generated_runtime_check_20261007(p_id);
 return p;
end; $$;

do $$ declare f text; begin
 foreach f in array array['fixer_generated_hamming_20261007(text,text)','fixer_generated_snapshot_20261007(uuid)','fixer_generated_history_snapshot_20261007()',
 'fixer_reserve_generated_20261007(uuid,jsonb,jsonb,jsonb)','fixer_generated_runtime_check_20261007(uuid)',
 'fixer_pre_generated_snapshot_20261007()','fixer_pre_generated_provenance_20261007(uuid)',
 'fixer_generated_inventory_lock_20261007()','fixer_forward_media_photo_snapshot_20261007()',
 'fixer_forward_media_provenance_lookup_20261006(uuid)'] loop
  execute 'revoke all on function public.'||f||' from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007';
 end loop;
end; $$;
grant execute on function public.fixer_generated_snapshot_20261007(uuid),
 public.fixer_reserve_generated_20261007(uuid,jsonb,jsonb,jsonb) to fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_media_photo_snapshot_20261007() to fixer_forward_media_owner_20261006,fixer_forward_media_photo_auditor_20261007;
grant execute on function public.fixer_forward_media_provenance_lookup_20261006(uuid) to fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
commit;
