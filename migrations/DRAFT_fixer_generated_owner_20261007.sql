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
 receipt_ref text unique not null, history_epoch jsonb not null,
 approved_source_revision text not null
  check(approved_source_revision ~ '^client-source:sha256:[0-9a-f]{64}$'),
 reserved_at timestamptz not null default clock_timestamp()
);
-- Exact historical byte reads by the existing trusted owner, bound to the
-- existing independent corpus judgment. No URL-only or metadata-only proof.
create table public.fixer_generated_history_visual_20261007 (
 history_key text not null, visual_sha256 text not null,
 published_binding_ref text not null, phash text not null
   check(phash ~ '^scene:phash64:[0-9a-f]{16}$'),
 visual_url text not null, tenant_id text, post_date date, group_key text,
 primary key(history_key,visual_sha256,published_binding_ref)
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
 -- One freshness rule shared by every consumer. The removed ad-hoc
 -- reservation-path clause policed sync staleness for ALL sources (including
 -- inactive ones) while the snapshot keyed on active: the confirmed asymmetry.
 -- An inactive/disconnected source is never supply evidence and simply holds
 -- the inventory; an ACTIVE source must prove a successful recent sync.
 -- Time-relative freshness cannot be part of the revision hash, so census
 -- receipts still expire and final send re-observes (see final check below).
 complete:=not exists(select 1 from jsonb_array_elements(sources) s
  where s->'active' is distinct from 'true'::jsonb)
  and not exists(select 1 from jsonb_array_elements(sources) s
  where s->'active'='true'::jsonb
   and (coalesce(s->>'revoked_externally','false')<>'false'
    or s->>'sync_status' is distinct from 'ready' or nullif(s->>'sync_finished_at','') is null
    or (s->>'sync_finished_at')::timestamptz<clock_timestamp()-interval '30 minutes'
    or (s->>'sync_finished_at')::timestamptz>clock_timestamp()))
  and not exists(select 1 from jsonb_array_elements(assets) a
   where a->>'kind'='photo' and a->'eligible' is distinct from 'false'::jsonb
    and coalesce(a->>'excluded_by_coach','false')='false'
    and a->>'review_status'='pending_review' and a->>'moderation_status'='pending')
  and not exists(select 1 from jsonb_array_elements(assets) a
    where a->>'kind'='photo' and a->'eligible'='true'::jsonb
      and not exists(select 1 from jsonb_array_elements(sources) s where s->>'id'=a->>'source_id'))
  and not exists(select 1 from public.media_asset a where a.gym_id=r.gym_id
   and to_jsonb(a)->>'kind'='photo' and to_jsonb(a)->'eligible'='true'::jsonb
   and coalesce(to_jsonb(a)->>'excluded_by_coach','false')='false'
   and to_jsonb(a)->>'review_status'='approved'
   and not public.fixer_owner_photo_source_ready_20261007(a.id,'sha256:'||(to_jsonb(a)#>>'{moderation_json,sha256}')));
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
  'account',r.account,'format',r.format,
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
declare rows jsonb; spine text; epoch jsonb; authority_complete boolean;
 state public.fixer_forward_media_photo_state_20261007%rowtype;
 baseline public.fixer_forward_media_photo_baseline_20261007%rowtype;
 policy public.fixer_forward_media_photo_policy_20261007%rowtype;
begin
 select * into state from public.fixer_forward_media_photo_state_20261007 where singleton;
 select * into baseline from public.fixer_forward_media_photo_baseline_20261007 where baseline_id=state.baseline_id;
 select * into policy from public.fixer_forward_media_photo_policy_20261007 where policy_id=baseline.policy_id;
 epoch:=jsonb_build_object('baseline_id',state.baseline_id,'generation',state.generation,'policy_id',baseline.policy_id);
 -- Sealed visuals survive calendar deletion/redating and can never silently
 -- disappear from a generated candidate's comparison set. An unknown sealed
 -- visual or partial corpus cannot certify non-repetition. Video exclusions
 -- follow the existing independently approved photo-certificate policy.
 authority_complete:=state.enabled and nullif(btrim(state.routes_reconciled_ref),'') is not null
  and policy.approved and baseline.scope_complete
  and baseline.declared_full_fleet_row_count=jsonb_array_length(baseline.rows_json)+jsonb_array_length(baseline.excluded_rows_json)
  and (jsonb_array_length(baseline.excluded_rows_json)=0
    or (nullif(btrim(policy.video_exclusion_ruling_ref),'') is not null
      and nullif(btrim(baseline.excluded_video_manifest_ref),'') is not null
      and not exists(select 1 from jsonb_array_elements(baseline.excluded_rows_json) e
        where e->>'media_kind' is distinct from 'reviewed_video_scope_exclusion'
          or nullif(e->>'published_binding_ref','') is null)))
  and not exists(select 1 from jsonb_array_elements(baseline.rows_json) h
    where h->'resolved' is distinct from 'true'::jsonb or h->>'media_kind' is distinct from 'still_photo'
      or (h->>'visual_sha256' ~ '^sha256:[0-9a-f]{64}$') is distinct from true
      or nullif(btrim(h->>'history_key'),'') is null or nullif(btrim(h->>'published_binding_ref'),'') is null)
  and (select count(*)=count(distinct value->>'history_key') from jsonb_array_elements(baseline.rows_json));
 with raw as (
  select 'calendar-image:'||r.id::text key,r.gym_id tenant,r.post_date content_day,r.visual_group_key grp,
   r.image_url url,md5(jsonb_build_array(r.gym_id,r.post_date,r.visual_group_key,r.image_url,r.thumbnail_url)::text) binding,
   null::text sealed_sha
  from public.content_calendar r where r.status='published' or r.published_at is not null or r.late_post_id is not null
  union all
  select 'calendar-thumbnail:'||r.id::text,r.gym_id,r.post_date,r.visual_group_key,r.thumbnail_url,
   md5(jsonb_build_array(r.gym_id,r.post_date,r.visual_group_key,r.image_url,r.thumbnail_url)::text),null
  from public.content_calendar r where (r.status='published' or r.published_at is not null or r.late_post_id is not null) and r.thumbnail_url is not null
  union all
  select 'claim-'||object.kind||':'||claim.claim_token::text,claim.tenant_id,claim.post_date,claim.group_key,object.url,
   md5(to_jsonb(claim)::text),null from public.fixer_forward_media_claim_receipt_20261006 claim
  cross join lateral (values('source',claim.source_url),('image',claim.image_url),('thumbnail',claim.thumbnail_url)) object(kind,url) where object.url is not null
  union all
  select 'photo-reserved-'||object.kind||':'||r.audit_id::text,r.candidate_json->>'tenant_id',
   (r.candidate_json->>'post_date')::date,r.candidate_json->>'group_key',object.url,r.receipt_ref,null
  from public.fixer_owner_photo_reservation_20261007 r cross join lateral
   (values('source',r.candidate_json->>'source_url'),('image',r.candidate_json->>'image_url'),('thumbnail',r.candidate_json->>'thumbnail_url')) object(kind,url) where object.url is not null
  union all
  select 'generated-reserved:'||r.job_id::text,r.candidate_json->>'gym_id',(r.candidate_json->>'local_date')::date,
   r.group_key,r.candidate_json->>'original_url',r.receipt_ref,null from public.fixer_generated_reservation_20261007 r
  union all
  select 'sealed:'||sealed.baseline_id::text||':'||(h->>'history_key'),h->>'tenant_id',
   case when h->>'local_date' ~ '^\d{4}-\d{2}-\d{2}$' then (h->>'local_date')::date else null end,
   h->>'group_key',h->>'visual_url',h->>'published_binding_ref',h->>'visual_sha256'
  from public.fixer_forward_media_photo_baseline_20261007 sealed
  join public.fixer_forward_media_photo_policy_20261007 approved_policy on approved_policy.policy_id=sealed.policy_id
  cross join lateral jsonb_array_elements(sealed.rows_json) h
  where approved_policy.approved and sealed.scope_complete
    and sealed.declared_full_fleet_row_count=jsonb_array_length(sealed.rows_json)+jsonb_array_length(sealed.excluded_rows_json)
    and (jsonb_array_length(sealed.excluded_rows_json)=0
      or (nullif(btrim(approved_policy.video_exclusion_ruling_ref),'') is not null
        and nullif(btrim(sealed.excluded_video_manifest_ref),'') is not null
        and not exists(select 1 from jsonb_array_elements(sealed.excluded_rows_json) e
          where e->>'media_kind' is distinct from 'reviewed_video_scope_exclusion'
            or nullif(e->>'published_binding_ref','') is null)))
    and (select count(*)=count(distinct j->>'history_key') from jsonb_array_elements(sealed.rows_json) j)
    and not exists(select 1 from jsonb_array_elements(sealed.rows_json) j
      where j->'resolved' is distinct from 'true'::jsonb or j->>'media_kind' is distinct from 'still_photo'
       or (j->>'visual_sha256' ~ '^sha256:[0-9a-f]{64}$') is distinct from true
       or nullif(btrim(j->>'history_key'),'') is null or nullif(btrim(j->>'published_binding_ref'),'') is null)
 ), bound as (
  select jsonb_build_object('history_key',key,'tenant_id',tenant,'local_date',content_day,'group_key',grp,
   'published_binding_ref',binding,'visual_url',url,'visual_sha256',coalesce(raw.sealed_sha,v.visual_sha256),
   'origin_history_key',key,
   'phash',v.phash,'history_proof_ref',case when v.history_key is not null then
    'generated-history:sha256:'||encode(sha256(convert_to(to_jsonb(v)::text,'UTF8')),'hex') else null end) item
  from raw left join public.fixer_generated_history_visual_20261007 v
    on v.history_key=raw.key and v.published_binding_ref=raw.binding and v.visual_url=raw.url
     and (raw.sealed_sha is null or raw.sealed_sha=v.visual_sha256)
  union all
  -- Observed live deltas remain permanent evidence after a later row deletion
  -- or binding edit. Reuse the original immutable proof, without recursively
  -- creating another receipt for this retained view.
  select jsonb_build_object('history_key','retained:'||v.history_key||':'||encode(sha256(convert_to(to_jsonb(v)::text,'UTF8')),'hex'),
    'tenant_id',v.tenant_id,'local_date',v.post_date,'group_key',v.group_key,'published_binding_ref',v.published_binding_ref,
    'origin_history_key',v.history_key,
    'visual_url',v.visual_url,'visual_sha256',v.visual_sha256,'phash',v.phash,
    'history_proof_ref','generated-history:sha256:'||encode(sha256(convert_to(to_jsonb(v)::text,'UTF8')),'hex'))
  from public.fixer_generated_history_visual_20261007 v
  where not exists(select 1 from raw where raw.key=v.history_key and raw.binding=v.published_binding_ref
     and raw.url=v.visual_url and (raw.sealed_sha is null or raw.sealed_sha=v.visual_sha256))
 ) select coalesce(jsonb_agg(item order by item->>'history_key'),'[]'::jsonb) into rows from bound;
 -- Remove observed byte SHA from the spine: recording exact current visuals
 -- cannot invalidate the stable calendar/claim/reservation tuple revision.
 select 'sha256:'||encode(sha256(convert_to(jsonb_build_object('epoch',epoch,'sealed_rows',baseline.rows_json,
  'rows',coalesce(jsonb_agg(value-'visual_sha256'-'phash'-'history_proof_ref' order by value->>'history_key'),'[]'::jsonb))::text,'UTF8')),'hex')
 into spine from jsonb_array_elements(rows);
 return jsonb_build_object('rows',rows,'spine_digest',spine,'epoch',epoch,
  'scope_complete',coalesce(authority_complete,false) and jsonb_array_length(rows)<=10000 and not exists(select 1 from jsonb_array_elements(rows) h
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

-- A client-approved row keeps its approved visual for every generated
-- reservation, first or sibling replay: rebinding is allowed only when no
-- visual was approved yet (or the identical grant is already bound). Anything
-- else holds for re-approval; approval status itself is never reset, bypassed
-- or weakened, and an identical grant still passes every other gate below.
create function public.fixer_generated_approved_visual_guard_20261007(r public.content_calendar,c jsonb,m jsonb)
returns void language plpgsql stable set search_path=pg_catalog,public as $$
begin
 if r.status='approved'
  and (r.source_media_asset_id is not null or r.source_media_url is not null or r.image_url is not null
    or r.thumbnail_url is not null or r.render_manifest_digest is not null)
  and (r.source_media_asset_id is distinct from 'generated-astra:'||(c->>'job_id')
    or r.source_media_url is distinct from c->>'original_url' or r.image_url is distinct from c->>'original_url'
    or r.thumbnail_url is not null or r.render_manifest_digest is distinct from m->>'manifest_digest') then
  raise exception 'approved visual cannot change without re-approval or hold' using errcode='23514'; end if;
end; $$;

-- Existing owner is the trust boundary for authenticated generation + exact
-- copy/palette + historical byte reads. No service/auditor self-approval API.
create function public.fixer_reserve_generated_20261007(p_id uuid,c jsonb,visuals jsonb,m jsonb,
 p_approved_source_revision text default null)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; h jsonb; proof jsonb; prior public.fixer_generated_reservation_20261007%rowtype;
 r public.content_calendar%rowtype; receipt text; original jsonb; clearance jsonb; fp text; asset text;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
  or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 -- Only the authenticated owner supplies the independently verified client-source
 -- revision. It is separate from the DB row-copy digest and immutable on replay.
 if (p_approved_source_revision ~ '^client-source:sha256:[0-9a-f]{64}$') is distinct from true then
  raise exception 'verified approved source revision required' using errcode='23514'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated reservation requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into r from public.content_calendar where id=p_id for update;
 perform public.fixer_generated_approved_visual_guard_20261007(r,c,m);
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
 perform public.fixer_still_negative_check_20261007(jsonb_build_object('gym_id',c->>'gym_id','source_asset_id','generated-astra:'||(c->>'job_id'),
  'source_url',c->>'original_url','sha256','sha256:'||(c->>'original_sha256'),'md5','md5:'||(c->>'original_md5'),'phash',c->>'original_phash'));
 if exists(select 1 from public.fixer_still_reservation_20261007 still where
   still.original->>'sha256'='sha256:'||(c->>'original_sha256') or still.original->>'md5'='md5:'||(c->>'original_md5')
   or still.original->>'source_url'=c->>'original_url'
   or public.fixer_generated_hamming_20261007(still.original->>'phash',c->>'original_phash')<=6) then
  raise exception 'generated visual already held by common still reservation' using errcode='23514'; end if;
 select * into prior from public.fixer_generated_reservation_20261007 where job_id=(c->>'job_id')::uuid;
 if found then
  if prior.candidate_json is distinct from c or prior.manifest_json is distinct from m
   or prior.approved_source_revision is distinct from p_approved_source_revision then
   raise exception 'generated job immutable identity conflict' using errcode='23514'; end if;
  if prior.group_key is distinct from r.visual_group_key then
   raise exception 'generated sibling group changed' using errcode='23514'; end if;
  if prior.calendar_row_id is distinct from p_id then
   if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'active'
    or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null then
    raise exception 'generated sibling must be unsent' using errcode='23514'; end if;
   -- Approved-visual guard already ran right after the row lock, before
   -- any generated authority checks, for both first reserves and replays.
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
  if h->>'history_key' not like 'retained:%' then
  insert into public.fixer_generated_history_visual_20261007
    (history_key,visual_sha256,published_binding_ref,phash,visual_url,tenant_id,post_date,group_key)
  values(h->>'history_key',proof->>'visual_sha256',h->>'published_binding_ref',proof->>'phash',proof->>'visual_url',
    h->>'tenant_id',(h->>'local_date')::date,h->>'group_key') on conflict do nothing;
  if not exists(select 1 from public.fixer_generated_history_visual_20261007 v
    where v.history_key=h->>'history_key' and v.visual_sha256=proof->>'visual_sha256'
     and v.published_binding_ref=h->>'published_binding_ref' and v.phash=proof->>'phash'
     and v.visual_url=proof->>'visual_url' and v.tenant_id is not distinct from h->>'tenant_id'
     and v.post_date is not distinct from (h->>'local_date')::date
     and v.group_key is not distinct from h->>'group_key') then
   raise exception 'historical perceptual receipt identity conflict' using errcode='23514'; end if;
  elsif proof->>'phash' is distinct from h->>'phash' then
   raise exception 'retained perceptual receipt identity conflict' using errcode='23514';
  end if;
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
 insert into public.fixer_generated_reservation_20261007(job_id,calendar_row_id,group_key,candidate_json,manifest_json,receipt_ref,history_epoch,approved_source_revision)
 values((c->>'job_id')::uuid,p_id,r.visual_group_key,c,m,receipt,snap#>'{history,epoch}',p_approved_source_revision);
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
declare r public.content_calendar%rowtype; g public.fixer_generated_reservation_20261007%rowtype;
 s record; snap jsonb; h jsonb; ph text;
begin
 select * into r from public.content_calendar where id=p_id;
 select * into g from public.fixer_generated_reservation_20261007
  where 'generated-astra:'||(candidate_json->>'job_id')=r.source_media_asset_id and candidate_json->>'gym_id'=r.gym_id;
 if not found then
  if r.source_media_asset_id like 'generated-astra:%' then raise exception 'generated source reservation missing' using errcode='23514'; end if;
  return true;
 end if;
 snap:=public.fixer_generated_snapshot_20261007(p_id);
 perform public.fixer_still_negative_check_20261007(jsonb_build_object('gym_id',g.candidate_json->>'gym_id','source_asset_id','generated-astra:'||g.job_id::text,
  'source_url',g.candidate_json->>'original_url','sha256','sha256:'||(g.candidate_json->>'original_sha256'),
  'md5','md5:'||(g.candidate_json->>'original_md5'),'phash',g.candidate_json->>'original_phash'));
 if exists(select 1 from public.fixer_still_reservation_20261007 still where
   still.original->>'sha256'='sha256:'||(g.candidate_json->>'original_sha256')
   or public.fixer_generated_hamming_20261007(still.original->>'phash',g.candidate_json->>'original_phash')<=6) then
  raise exception 'generated visual already held by common still reservation' using errcode='23514'; end if;
 if g.candidate_json->>'local_date' is distinct from r.post_date::text
  or g.candidate_json->>'logical_post_id' is distinct from to_jsonb(r)->>'logical_post_id'
  or g.group_key is distinct from r.visual_group_key
  or g.candidate_json->>'copy_revision' is distinct from snap->>'copy_revision'
  or g.candidate_json->>'inventory_revision' is distinct from snap->>'inventory_revision'
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or snap->'eligible_photo_count' is distinct from '0'::jsonb
  or snap->'history_complete' is distinct from 'true'::jsonb
  or g.history_epoch is distinct from snap#>'{history,epoch}'
  or g.candidate_json->>'original_url' is distinct from r.source_media_url
  or r.source_media_url is distinct from r.image_url or r.thumbnail_url is not null
  or g.manifest_json->>'manifest_digest' is distinct from r.render_manifest_digest then
  raise exception 'generated current content/depletion/history binding changed' using errcode='23514'; end if;
 -- Final send re-observes local depletion: reserve-time revisions were
 -- identity, not freshness. Without a current-revision, complete, zero-supply
 -- owner observation inside ten minutes the send holds, exactly as for stills.
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if not coalesce(s.enabled,false) or s.epoch_id is null or not exists(select 1 from public.fixer_still_inventory_20261007 i
   where i.epoch_id=s.epoch_id and i.gym_id=r.gym_id and i.inventory_revision=snap->>'inventory_revision'
    and i.local_complete and i.local_available=0 and i.observed_at>=clock_timestamp()-interval '10 minutes') then
  raise exception 'generated final send requires fresh local depletion authority' using errcode='23514'; end if;
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
        where (coalesce(h->>'origin_history_key',h->>'history_key') in ('claim-source:'||claim.claim_token::text,'claim-image:'||claim.claim_token::text)
          or (h->>'history_key' like 'retained:%'
            and h->>'origin_history_key'='calendar-image:'||claim.calendar_row_id::text
            and nullif(h->>'history_proof_ref','') is not null
            and h->>'visual_sha256'='sha256:'||(sibling.candidate_json->>'original_sha256')))
         and claim.tenant_id=sibling.candidate_json->>'gym_id'
         and claim.post_date::text=sibling.candidate_json->>'local_date'
         and claim.group_key=sibling.group_key
         and claim.source_url=sibling.candidate_json->>'original_url'
         and claim.image_url=claim.source_url and claim.thumbnail_url is null
         and claim.fingerprints=array['md5:'||(sibling.candidate_json->>'original_md5')])
      or exists(select 1 from public.content_calendar live where h->>'history_key'='calendar-image:'||live.id::text
        and live.gym_id=sibling.candidate_json->>'gym_id' and live.post_date::text=sibling.candidate_json->>'local_date'
        and live.visual_group_key=sibling.group_key and live.source_media_asset_id='generated-astra:'||sibling.job_id::text
        and live.image_url=sibling.candidate_json->>'original_url' and live.thumbnail_url is null)
      or (h->>'history_key' like 'retained:%'
        and h->>'origin_history_key'='calendar-image:'||sibling.calendar_row_id::text
        and nullif(h->>'history_proof_ref','') is not null
        and h->>'visual_sha256'='sha256:'||(sibling.candidate_json->>'original_sha256')))) then continue; end if;
  if h->>'history_key' like 'retained:%' then ph:=h->>'phash'; else
  select phash into ph from public.fixer_generated_history_visual_20261007 v where v.history_key=h->>'history_key'
   and v.visual_sha256=h->>'visual_sha256' and v.published_binding_ref=h->>'published_binding_ref';
  if not found then
   select candidate_json->>'original_phash' into ph from public.fixer_generated_reservation_20261007 sibling
    where h->>'history_key'='generated-reserved:'||sibling.job_id::text;
  end if;
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
 'fixer_reserve_generated_20261007(uuid,jsonb,jsonb,jsonb,text)','fixer_generated_runtime_check_20261007(uuid)',
 'fixer_pre_generated_snapshot_20261007()','fixer_pre_generated_provenance_20261007(uuid)',
 'fixer_generated_inventory_lock_20261007()','fixer_forward_media_photo_snapshot_20261007()',
 'fixer_forward_media_provenance_lookup_20261006(uuid)'] loop
  execute 'revoke all on function public.'||f||' from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007';
 end loop;
end; $$;
grant execute on function public.fixer_generated_snapshot_20261007(uuid),
 public.fixer_reserve_generated_20261007(uuid,jsonb,jsonb,jsonb,text) to fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_media_photo_snapshot_20261007() to fixer_forward_media_owner_20261006,fixer_forward_media_photo_auditor_20261007;
grant execute on function public.fixer_forward_media_provenance_lookup_20261006(uuid) to fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;

-- INCREMENTAL FORWARD CONTRACT. OFF until the existing isolated owner has an
-- authenticated fresh-original and complete local-inventory adapter. These
-- RPCs record trusted owner observations, never producer claims. They do not
-- stage registry/clearance/manifest rows or create an alternate publish path.
create table public.fixer_still_cutover_20261007 (
 singleton boolean primary key default true check(singleton), enabled boolean not null default false,
 epoch_id uuid, cutover_at timestamptz, activation_ref text,
 check(not enabled or (epoch_id is not null and cutover_at is not null and nullif(btrim(activation_ref),'') is not null))
);
insert into public.fixer_still_cutover_20261007(singleton) values(true);
create table public.fixer_still_known_20261007 (
 receipt_id uuid primary key, epoch_id uuid, decision text not null
   check(decision in ('deny','quarantine','cleared_fresh','cleared_certificate')),
 original jsonb not null, evidence_ref text not null check(nullif(btrim(evidence_ref),'') is not null),
 observed_at timestamptz not null default clock_timestamp()
);
create table public.fixer_still_inventory_20261007 (
 receipt_id uuid primary key, epoch_id uuid not null, gym_id text not null,
 inventory_revision text not null, local_complete boolean not null, local_available integer not null check(local_available>=0),
 evidence_ref text not null check(nullif(btrim(evidence_ref),'') is not null),
 observed_at timestamptz not null default clock_timestamp()
);
create table public.fixer_still_reservation_20261007 (
 receipt_id uuid primary key, epoch_id uuid not null, calendar_row_id uuid not null,
 gym_id text not null, local_date date not null, logical_post_id uuid not null, group_key text not null,
 media_kind text not null check(media_kind in ('photo','graphic')), original jsonb not null,
 inventory_receipt uuid not null references public.fixer_still_inventory_20261007(receipt_id),
 clearance_receipt uuid not null references public.fixer_still_known_20261007(receipt_id),
 reserved_at timestamptz not null default clock_timestamp()
);
do $$ declare t text; begin
 foreach t in array array['fixer_still_cutover_20261007','fixer_still_known_20261007','fixer_still_inventory_20261007','fixer_still_reservation_20261007'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007',t);
  if t<>'fixer_still_cutover_20261007' then
   execute format('create trigger immutable_row before update or delete on public.%I for each row execute function public.fixer_forward_media_immutable_20261006()',t);
   execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.fixer_forward_media_immutable_20261006()',t);
  end if;
 end loop;
end; $$;

create function public.fixer_still_owner_lock_20261007()
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
   or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'still authority requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
end; $$;
create function public.fixer_still_cutover_control_20261007(p_enabled boolean,p_ref text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype;
begin
 perform public.fixer_still_owner_lock_20261007();
 if p_enabled is null or nullif(btrim(p_ref),'') is null then raise exception 'explicit cutover ruling required' using errcode='23514'; end if;
 update public.fixer_still_cutover_20261007 set enabled=p_enabled,
  epoch_id=coalesce(epoch_id,gen_random_uuid()),cutover_at=coalesce(cutover_at,clock_timestamp()),activation_ref=p_ref where singleton returning * into s;
 return to_jsonb(s);
end; $$;
create function public.fixer_still_tuple_valid_20261007(o jsonb)
returns boolean language sql immutable set search_path=pg_catalog as $$
 select coalesce(jsonb_typeof(o)='object' and
  o ?& array['gym_id','source_asset_id','source_url','sha256','md5','phash','length']
  and nullif(btrim(o->>'gym_id'),'') is not null and nullif(btrim(o->>'source_asset_id'),'') is not null
  and o->>'source_url' ~ '^https://[^[:space:]]+$' and o->>'sha256' ~ '^sha256:[0-9a-f]{64}$'
  and o->>'md5' ~ '^md5:[0-9a-f]{32}$' and o->>'phash' ~ '^scene:phash64:[0-9a-f]{16}$'
  and jsonb_typeof(o->'length')='number' and o->>'length' ~ '^[1-9][0-9]{0,8}$'
  and (o->>'length')::bigint<=134217728,false);
$$;
create function public.fixer_still_known_record_20261007(p_id uuid,p_decision text,o jsonb,p_ref text)
returns uuid language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype; old public.fixer_still_known_20261007%rowtype;
begin
 perform public.fixer_still_owner_lock_20261007();
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if nullif(btrim(p_ref),'') is null or
  (p_decision in ('cleared_fresh','cleared_certificate') and not public.fixer_still_tuple_valid_20261007(o))
  or (p_decision in ('deny','quarantine') and (jsonb_typeof(o) is distinct from 'object'
    or nullif(btrim(o->>'gym_id'),'') is null or nullif(btrim(o->>'source_asset_id'),'') is null
    or (o ? 'sha256' and (o->>'sha256' ~ '^sha256:[0-9a-f]{64}$') is distinct from true)
    or (o ? 'md5' and (o->>'md5' ~ '^md5:[0-9a-f]{32}$') is distinct from true)
    or (o ? 'phash' and (o->>'phash' ~ '^scene:phash64:[0-9a-f]{16}$') is distinct from true))) then
  raise exception 'exact owner-observed original required' using errcode='23514'; end if;
 if p_decision='cleared_fresh' and (not s.enabled or p_ref not like 'authenticated-post-epoch:%') then
  raise exception 'fresh clearance requires active epoch and owner authentication' using errcode='23514'; end if;
 if p_decision='cleared_certificate' and not exists(select 1 from public.fixer_owner_photo_reservation_20261007 g
  where g.receipt_ref=p_ref and g.candidate_json->>'tenant_id'=o->>'gym_id'
   and g.candidate_json->>'source_asset_id'=o->>'source_asset_id' and g.candidate_json->>'source_url'=o->>'source_url'
   and g.candidate_json->>'source_sha256'=o->>'sha256' and g.candidate_json->>'source_fingerprint'=o->>'md5') then
  raise exception 'older asset requires exact signed photo authority' using errcode='23514'; end if;
 insert into public.fixer_still_known_20261007(receipt_id,epoch_id,decision,original,evidence_ref)
 values(p_id,s.epoch_id,p_decision,o,p_ref) on conflict do nothing;
 select * into old from public.fixer_still_known_20261007 where receipt_id=p_id;
 if old.original is distinct from o or old.decision is distinct from p_decision or old.evidence_ref is distinct from p_ref then
  raise exception 'known observation immutable conflict' using errcode='23514'; end if;
 return p_id;
end; $$;

-- Called only after the owner freshly inspects local library/rotation and all
-- connected sources. SQL binds that observation to current DB inventory. A
-- missing, incomplete, expired or stale observation is never zero supply.
create function public.fixer_still_inventory_record_20261007(p_id uuid,p_row uuid,p_revision text,p_complete boolean,p_available integer,p_ref text)
returns uuid language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; s public.fixer_still_cutover_20261007%rowtype; old public.fixer_still_inventory_20261007%rowtype;
begin
 perform public.fixer_still_owner_lock_20261007();
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 snap:=public.fixer_generated_snapshot_20261007(p_row);
 if not s.enabled or p_revision is distinct from snap->>'inventory_revision'
  or p_complete is null or p_available is null or p_available<0 or nullif(btrim(p_ref),'') is null then
  raise exception 'current complete local inventory authority required' using errcode='23514'; end if;
 insert into public.fixer_still_inventory_20261007(receipt_id,epoch_id,gym_id,inventory_revision,local_complete,local_available,evidence_ref)
 values(p_id,s.epoch_id,snap->>'gym_id',p_revision,p_complete,p_available,p_ref) on conflict do nothing;
 select * into old from public.fixer_still_inventory_20261007 where receipt_id=p_id;
 if old.epoch_id is distinct from s.epoch_id or old.gym_id is distinct from snap->>'gym_id'
  or old.inventory_revision is distinct from p_revision or old.local_complete is distinct from p_complete
  or old.local_available is distinct from p_available or old.evidence_ref is distinct from p_ref then
  raise exception 'inventory observation immutable conflict' using errcode='23514'; end if;
 return p_id;
end; $$;

create function public.fixer_still_negative_check_20261007(o jsonb)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if exists(select 1 from public.fixer_still_known_20261007 k where k.decision in ('deny','quarantine')
  and ((k.original->>'gym_id'=o->>'gym_id' and k.original->>'source_asset_id'=o->>'source_asset_id')
   or k.original->>'sha256'=o->>'sha256' or k.original->>'md5'=o->>'md5' or k.original->>'source_url'=o->>'source_url'
   or (o->>'phash' is not null and public.fixer_generated_hamming_20261007(k.original->>'phash',o->>'phash')<=6))) then
  raise exception 'known still history denied or quarantined' using errcode='23514'; end if;
end; $$;
-- Exact AND perceptual occupancy across every retained published/delivered
-- visual authority: forward claims, owner photo reservations, the approved
-- fleet photo baseline, generated reservations and perceptual receipts, known
-- negative history and staged originals. A resolved delivered baseline row
-- carrying a pHash also denies near duplicates (not only exact bytes); a
-- resolved row with neither exact bytes nor a valid pHash fails CLOSED.
-- Novelty is never inferred from missing or unknown perceptual evidence.
create function public.fixer_still_occupancy_check_20261007(p_row uuid,o jsonb)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype;
begin
 select * into r from public.content_calendar where id=p_row;
 perform public.fixer_still_negative_check_20261007(o);
 if exists(select 1 from public.fixer_still_reservation_20261007 g where
  (g.original->>'sha256'=o->>'sha256' or g.original->>'md5'=o->>'md5' or g.original->>'source_url'=o->>'source_url'
   or public.fixer_generated_hamming_20261007(g.original->>'phash',o->>'phash')<=6)
  and not(g.gym_id=r.gym_id and g.local_date=r.post_date and g.logical_post_id=(to_jsonb(r)->>'logical_post_id')::uuid
   and g.group_key=r.visual_group_key and g.original=o))
  or exists(select 1 from public.fixer_generated_reservation_20261007 g where
   (g.candidate_json->>'original_sha256'=right(o->>'sha256',64)
     or public.fixer_generated_hamming_20261007(g.candidate_json->>'original_phash',o->>'phash')<=6))
  or exists(select 1 from public.fixer_generated_history_visual_20261007 h where h.visual_sha256=o->>'sha256'
     or public.fixer_generated_hamming_20261007(h.phash,o->>'phash')<=6)
  or exists(select 1 from public.fixer_owner_photo_reservation_20261007 g where
     g.candidate_json->>'source_sha256'=o->>'sha256' or g.candidate_json->>'image_sha256'=o->>'sha256'
     or g.candidate_json->>'thumbnail_sha256'=o->>'sha256')
  or exists(select 1 from public.fixer_forward_media_photo_baseline_20261007 b
    join public.fixer_forward_media_photo_policy_20261007 policy on policy.policy_id=b.policy_id
    cross join lateral jsonb_array_elements(b.rows_json) h
    where policy.approved and h->'resolved'='true'::jsonb and h->>'media_kind'='still_photo'
     and (h->>'visual_sha256'=o->>'sha256'
      or (h->>'phash' ~ '^scene:phash64:[0-9a-f]{16}$'
        and public.fixer_generated_hamming_20261007(h->>'phash',o->>'phash')<=6)
      or (h->>'phash' ~ '^scene:phash64:[0-9a-f]{16}$') is not true))
  or exists(select 1 from public.fixer_forward_media_historical_original_20261007 h where h.source_fingerprint=o->>'md5')
  or exists(select 1 from public.fixer_forward_media_use_20261006 u where u.fingerprint=o->>'md5'
    and not(u.tenant_id=r.gym_id and u.post_date=r.post_date and u.group_key=r.visual_group_key)) then
  raise exception 'still original or delivered visual already reserved' using errcode='23514'; end if;
end; $$;

-- RESERVE-TIME authority. The submitted census receipt must be fresh HERE.
-- Source freshness lives in the snapshot itself (one shared rule for active
-- sources; inactive sources hold rather than deplete). The final-send path
-- below never trusts this stored, later-stale receipt for freshness.
create function public.fixer_still_reservation_check_20261007(p_row uuid,o jsonb,p_kind text,p_inventory uuid,p_clearance uuid)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; s public.fixer_still_cutover_20261007%rowtype; snap jsonb;
begin
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 select * into r from public.content_calendar where id=p_row;
 snap:=public.fixer_generated_snapshot_20261007(p_row);
 if not s.enabled or not public.fixer_still_tuple_valid_20261007(o) or o->>'gym_id' is distinct from r.gym_id
  or p_kind not in ('photo','graphic') or p_kind is null
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or not exists(select 1 from public.fixer_still_inventory_20261007 i where i.receipt_id=p_inventory
    and i.epoch_id=s.epoch_id and i.gym_id=r.gym_id and i.inventory_revision=snap->>'inventory_revision'
    and i.local_complete and i.observed_at>=clock_timestamp()-interval '10 minutes'
    and (p_kind='photo' or (i.local_available=0 and snap->'eligible_photo_count'='0'::jsonb)))
  or not exists(select 1 from public.fixer_still_known_20261007 k where k.receipt_id=p_clearance and k.original=o
    and k.decision in ('cleared_fresh','cleared_certificate') and k.epoch_id=s.epoch_id
    and (k.decision='cleared_certificate' or k.observed_at>=s.cutover_at)) then
  raise exception 'still cutover source or inventory authority unavailable' using errcode='23514'; end if;
 if p_kind='photo' and not exists(select 1 from public.media_asset a join public.media_source src on src.id=a.source_id
  where a.id=o->>'source_asset_id' and a.gym_id=r.gym_id and src.gym_id=r.gym_id and src.active
   and src.kind='gym_drive' and src.sync_status='ready' and a.content_hash=right(o->>'md5',32)
   and public.fixer_owner_photo_source_ready_20261007(a.id,o->>'sha256')) then
  raise exception 'current approved photo original required' using errcode='23514'; end if;
 perform public.fixer_still_occupancy_check_20261007(p_row,o);
end; $$;

-- FINAL-SEND authority for a permanent still reservation. Separates
-- reserve-time freshness from final-send freshness: the immutable reservation
-- identity (original, receipts, bindings) is never rewritten, but the stored
-- reserve-time receipt is never re-trusted for freshness either, so a
-- reservation older than ten minutes stays sendable instead of self-locking.
-- A FRESH authoritative census (same epoch, matching the CURRENT live
-- revision, observed within ten minutes) must exist at send time, and the
-- reservation's bound gym/date/logical_post_id/group_key must still match the
-- current calendar row, so redating or retargeting holds. Any new inventory,
-- photo or moderation change since reservation changes the live snapshot and
-- holds the send (photo-first denial preserved); a still-true fresh census
-- keeps an old reservation sendable.
create function public.fixer_still_final_check_20261007(p_row uuid,p_receipt uuid)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; s public.fixer_still_cutover_20261007%rowtype;
 g public.fixer_still_reservation_20261007%rowtype; snap jsonb;
begin
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 select * into r from public.content_calendar where id=p_row;
 select * into g from public.fixer_still_reservation_20261007 where receipt_id=p_receipt;
 snap:=public.fixer_generated_snapshot_20261007(p_row);
 if not found or r.id is null or not s.enabled or s.epoch_id is distinct from g.epoch_id
  or g.gym_id is distinct from r.gym_id or g.local_date is distinct from r.post_date
  or g.logical_post_id is distinct from (to_jsonb(r)->>'logical_post_id')::uuid
  or g.group_key is distinct from r.visual_group_key
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or not exists(select 1 from public.fixer_still_inventory_20261007 i
    where i.epoch_id=s.epoch_id and i.gym_id=r.gym_id and i.inventory_revision=snap->>'inventory_revision'
     and i.local_complete and i.observed_at>=clock_timestamp()-interval '10 minutes'
     and (g.media_kind='photo' or (i.local_available=0 and snap->'eligible_photo_count'='0'::jsonb))) then
  raise exception 'still final send requires current binding and fresh inventory authority' using errcode='23514'; end if;
 if g.media_kind='photo' and not exists(select 1 from public.media_asset a join public.media_source src on src.id=a.source_id
  where a.id=g.original->>'source_asset_id' and a.gym_id=r.gym_id and src.gym_id=r.gym_id and src.active
   and src.kind='gym_drive' and src.sync_status='ready' and a.content_hash=right(g.original->>'md5',32)
   and public.fixer_owner_photo_source_ready_20261007(a.id,g.original->>'sha256')) then
  raise exception 'current approved photo original required' using errcode='23514'; end if;
 perform public.fixer_still_occupancy_check_20261007(p_row,g.original);
end; $$;
create function public.fixer_still_reserve_20261007(p_id uuid,p_row uuid,o jsonb,p_kind text,p_inventory uuid,p_clearance uuid)
returns uuid language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; s public.fixer_still_cutover_20261007%rowtype; old public.fixer_still_reservation_20261007%rowtype;
begin
 perform public.fixer_still_owner_lock_20261007();
 select * into r from public.content_calendar where id=p_row for update;
 if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'active'
   or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null then
  raise exception 'unsent still row required' using errcode='23514'; end if;
 perform public.fixer_still_reservation_check_20261007(p_row,o,p_kind,p_inventory,p_clearance);
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 insert into public.fixer_still_reservation_20261007(receipt_id,epoch_id,calendar_row_id,gym_id,local_date,logical_post_id,group_key,media_kind,original,inventory_receipt,clearance_receipt)
 values(p_id,s.epoch_id,p_row,r.gym_id,r.post_date,(to_jsonb(r)->>'logical_post_id')::uuid,r.visual_group_key,p_kind,o,p_inventory,p_clearance) on conflict do nothing;
 select * into old from public.fixer_still_reservation_20261007 where receipt_id=p_id;
 if old.calendar_row_id is distinct from p_row or old.original is distinct from o or old.media_kind is distinct from p_kind
  or old.inventory_receipt is distinct from p_inventory or old.clearance_receipt is distinct from p_clearance then
  raise exception 'still reservation immutable conflict' using errcode='23514'; end if;
 return p_id;
end; $$;

-- Negative knowledge also governs already-staged legacy rows. Existing signed
-- photo and B630 generated checks are retained, including all sibling rules.
alter function public.fixer_forward_media_provenance_lookup_20261006(uuid) rename to fixer_pre_still_provenance_20261007;
create function public.fixer_forward_media_provenance_lookup_20261006(p_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare p jsonb; g public.fixer_still_reservation_20261007%rowtype;
begin
 p:=public.fixer_pre_still_provenance_20261007(p_id);
 perform public.fixer_still_negative_check_20261007(jsonb_build_object('gym_id',p#>>'{original,tenant_id}',
  'source_asset_id',p#>>'{original,source_asset_id}','source_url',p#>>'{original,source_url}','md5',p#>>'{original,source_fingerprint}'));
 select * into g from public.fixer_still_reservation_20261007 where gym_id=p#>>'{original,tenant_id}'
  and original->>'source_asset_id'=p#>>'{original,source_asset_id}' order by reserved_at limit 1;
 if found then
  if g.original->>'source_url' is distinct from p#>>'{original,source_url}'
   or g.original->>'md5' is distinct from p#>>'{original,source_fingerprint}'
   or g.original->>'source_url' is distinct from p#>>'{manifest,image_url}' or p#>>'{manifest,thumbnail_url}' is not null then
   raise exception 'still reservation requires exact original delivery' using errcode='23514'; end if;
  -- Final claim re-verifies the bound row identity and requires a fresh
  -- census; the immutable reserve-time receipt is identity, not freshness.
  perform public.fixer_still_final_check_20261007(p_id,g.receipt_id);
 end if;
 return p;
end; $$;
do $$ declare f text; begin
 foreach f in array array['fixer_still_owner_lock_20261007()','fixer_still_cutover_control_20261007(boolean,text)',
 'fixer_still_tuple_valid_20261007(jsonb)','fixer_still_known_record_20261007(uuid,text,jsonb,text)',
 'fixer_still_inventory_record_20261007(uuid,uuid,text,boolean,integer,text)','fixer_still_negative_check_20261007(jsonb)',
 'fixer_still_occupancy_check_20261007(uuid,jsonb)','fixer_still_reservation_check_20261007(uuid,jsonb,text,uuid,uuid)',
 'fixer_still_final_check_20261007(uuid,uuid)','fixer_still_reserve_20261007(uuid,uuid,jsonb,text,uuid,uuid)',
 'fixer_generated_approved_visual_guard_20261007(public.content_calendar,jsonb,jsonb)',
 'fixer_pre_still_provenance_20261007(uuid)','fixer_forward_media_provenance_lookup_20261006(uuid)'] loop
  execute 'revoke all on function public.'||f||' from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007';
 end loop;
end; $$;
grant execute on function public.fixer_still_cutover_control_20261007(boolean,text),
 public.fixer_still_known_record_20261007(uuid,text,jsonb,text),public.fixer_still_inventory_record_20261007(uuid,uuid,text,boolean,integer,text),
 public.fixer_still_reserve_20261007(uuid,uuid,jsonb,text,uuid,uuid),
 public.fixer_still_final_check_20261007(uuid,uuid) to fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_media_provenance_lookup_20261006(uuid) to fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;

-- Cross-volume publish readback (smallest safe release item 4). The isolated
-- owner's local SQLite job journal lives on the owner's own /data volume and
-- is never publisher authority. This is the one durable prepared-job read
-- source at publisher boundaries: a read-only, minimal binding projected from
-- the immutable generated reservation. It carries the owner-approved source
-- revision and copy/palette refs, never provider response/output/storage
-- internals. A row whose persisted identity no longer matches its reservation
-- reads back NULL (fail closed); the publisher still re-derives the CURRENT
-- approved source revision and controlled palette and compares them itself.
create function public.fixer_generated_publish_readback_20261007(p_id uuid)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; g public.fixer_generated_reservation_20261007%rowtype;
 c jsonb; m jsonb;
begin
 select * into r from public.content_calendar where id=p_id;
 if not found or r.source_media_asset_id is null
  or r.source_media_asset_id !~ '^generated-astra:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  return null; end if;
 select * into g from public.fixer_generated_reservation_20261007
  where job_id=substr(r.source_media_asset_id,17)::uuid;
 if not found then return null; end if;
 c:=g.candidate_json; m:=g.manifest_json;
 -- The leased row must still be exactly the reserved identity. Sibling rows
 -- share the same job/logical/original; anything drifted reads back NULL.
 if r.gym_id is distinct from c->>'gym_id' or r.post_date::text is distinct from c->>'local_date'
  or (to_jsonb(r)->>'logical_post_id') is distinct from c->>'logical_post_id'
  or r.account not in ('instagram','facebook')
  or r.format is distinct from 'feed'
  or r.visual_group_key is distinct from g.group_key
  or r.image_url is distinct from c->>'original_url'
  or r.source_media_url is distinct from c->>'original_url'
  or r.thumbnail_url is not null
  or r.render_manifest_digest is distinct from m->>'manifest_digest'
  or m->>'tenant_id' is distinct from c->>'gym_id'
  or m->>'source_asset_id' is distinct from r.source_media_asset_id
  or m->>'image_url' is distinct from c->>'original_url' then
  return null; end if;
 return jsonb_build_object('job_id',g.job_id,'calendar_row_id',r.id,'gym_id',c->>'gym_id',
  'account',r.account,'local_date',c->>'local_date','logical_post_id',c->>'logical_post_id',
  'group_key',g.group_key,'original_url',c->>'original_url','manifest_digest',m->>'manifest_digest',
  'source_revision',g.approved_source_revision,'copy_digest',c->>'copy_digest',
  'palette_revision',c->>'palette_revision','palette_digest',c->>'palette_digest',
  'receipt_ref',g.receipt_ref);
end; $$;
revoke all on function public.fixer_generated_publish_readback_20261007(uuid)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,
 fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;
-- Publisher read-only only. The isolated owner and attester already reach this
-- evidence through their own lanes; neither gains this RPC, and no role gains
-- reservation mutation or provider-secret exposure through it.
grant execute on function public.fixer_generated_publish_readback_20261007(uuid) to service_role;
commit;
