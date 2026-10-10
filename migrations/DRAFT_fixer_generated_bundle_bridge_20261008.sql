-- INERT DRAFT / UNAPPLIED. Requires the assembled generated owner, gap, send
-- lease and portal source-brand drafts in the SAME database. No provider calls,
-- role logins, configuration approval, client copy approval or activation.
-- Install as the existing trusted migration owner, never service_role. The
-- isolated owner gets only bounded wrappers, never portal service credentials.
-- Graph -> census -> canonical send -> portal gym -> gap -> calendar row.
begin;
do $$
begin
 if current_user in ('service_role','anon','authenticated') then
  raise exception 'trusted existing migration owner required' using errcode='42501'; end if;
 -- Supabase installs pgcrypto in extensions. Require that exact dependency
 -- before changing bridge objects; never install or relocate it implicitly.
 if to_regprocedure('extensions.digest(bytea,text)') is null then
  raise exception 'extensions pgcrypto digest dependency unavailable' using errcode='55000'; end if;
 if not has_schema_privilege(current_user,'extensions','USAGE')
  or not has_function_privilege(current_user,'extensions.digest(bytea,text)','EXECUTE') then
  raise exception 'trusted migration owner lacks extensions digest privileges' using errcode='42501'; end if;
 if to_regprocedure('public.echo_source_brand_active(uuid)') is null
  or to_regclass('public.echo_intake_tokens') is null
  or to_regprocedure('public.fixer_owner_photo_canonical_20261007(jsonb)') is null then
  raise exception 'assembled same-database source-brand dependencies unavailable' using errcode='55000'; end if;
 if not has_function_privilege(current_user,'public.echo_source_brand_active(uuid)','EXECUTE')
  or not has_table_privilege(current_user,'public.echo_source_captures','SELECT')
  or not has_table_privilege(current_user,'public.echo_intake_tokens','SELECT') then
  raise exception 'trusted migration owner lacks bounded portal read privileges' using errcode='42501'; end if;
end $$;

-- This registry belongs only to the generated portal bridge. It does not
-- depend on visual-group tenant_alias or treat shared intake tokens as Echo
-- enrollment. EMPTY BY DEFAULT: an authorized release must independently review
-- and explicitly populate each exact calendar gym text <-> portal gym UUID pair,
-- with its approval evidence and actor. No backfill from echo_intake_tokens.
create table public.fixer_generated_portal_tenant_map_20261008 (
 echo_account_key text primary key check(length(echo_account_key) between 1 and 200
  and echo_account_key=btrim(echo_account_key)),
 gym_id uuid not null unique references public.gyms(id),
 approval_evidence_ref text not null check(length(btrim(approval_evidence_ref))>0),
 approved_by text not null check(length(btrim(approved_by))>0),
 approved_at timestamptz not null default clock_timestamp()
);
alter table public.fixer_generated_portal_tenant_map_20261008 enable row level security;
-- Only the trusted migration/table owner can populate or inspect the registry.
-- Runtime service and isolated owner roles consume bounded SECURITY DEFINER RPCs.
revoke all on table public.fixer_generated_portal_tenant_map_20261008
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,
 fixer_forward_media_attester_20261006,generated_authority_owner_20261007,
 generated_authority_publisher_20261007,generated_send_reconciler_20261007;

-- Retain historical local-source rows unchanged; v2 rows carry a distinct
-- configuration/observation derivation reference and cannot use legacy refs.
do $$ declare name text; begin
 for name in select conname from pg_constraint where conrelid='public.fixer_generated_reservation_20261007'::regclass
  and contype='c' and conkey=array[(select attnum from pg_attribute where attrelid='public.fixer_generated_reservation_20261007'::regclass and attname='approved_source_revision')] loop
  execute format('alter table public.fixer_generated_reservation_20261007 drop constraint %I',name);
 end loop;
end $$;
alter table public.fixer_generated_reservation_20261007 add constraint generated_bundle_source_reference_20261008
 check(case when candidate_json->'schema_version'='2'::jsonb
  then approved_source_revision ~ '^source-brand:sha256:[0-9a-f]{64}$'
  else approved_source_revision ~ '^client-source:sha256:[0-9a-f]{64}$' end);

create function public.fixer_generated_bundle_digest_20261007(v jsonb)
returns text language sql immutable set search_path=pg_catalog,public as $$
 select encode(sha256(convert_to(public.fixer_owner_photo_canonical_20261007(v),'UTF8')),'hex');
$$;
create function public.fixer_generated_bundle_job_20261007(c jsonb)
returns uuid language plpgsql immutable set search_path=pg_catalog,public as $$
declare binding jsonb; bytes bytea; hex text;
begin
 select jsonb_object_agg(key,value) into binding from jsonb_each(c)
 where key=any(array['gym_id','local_date','logical_post_id','copy_revision','inventory_revision',
  'history_revision','palette_revision','copy_digest','palette_digest','review_policy_id','authority_pins','copy_derivation_receipt']);
 bytes:=substring(extensions.digest(uuid_send('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid)||
  convert_to('echo-astra:'||public.fixer_owner_photo_canonical_20261007(binding),'UTF8'),'sha1') from 1 for 16);
 bytes:=set_byte(bytes,6,(get_byte(bytes,6)&15)|80);
 bytes:=set_byte(bytes,8,(get_byte(bytes,8)&63)|128);
 hex:=encode(bytes,'hex'); return hex::uuid;
end $$;

-- Resolve the approved bridge registry and independent intake registry before returning B
-- consumer contract. No public-active call grants a collector/producer role.
create function public.fixer_generated_source_brand_active_20261007(p_base text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public set timezone='UTC' as $$
declare gym uuid; active jsonb;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'bundle bridge requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(p_base));
 select gym_id into gym from public.fixer_generated_portal_tenant_map_20261008 where echo_account_key=p_base;
 if gym is null then raise exception 'approved generated portal tenant mapping missing' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtextextended(gym::text,0));
 if (select count(*) from public.echo_intake_tokens where gym_id=gym and echo_account_key=p_base)<>1
  or (select count(*) from public.echo_intake_tokens where gym_id=gym or echo_account_key=p_base)<>1
  or (select count(*) from public.fixer_generated_portal_tenant_map_20261008
    where gym_id=gym or echo_account_key=p_base)<>1
  or not exists(select 1 from public.fixer_generated_portal_tenant_map_20261008
    where echo_account_key=p_base and gym_id=gym) then
  raise exception 'exact source-brand tenant bijection required' using errcode='23514'; end if;
 active:=public.echo_source_brand_active(gym);
 if active is null or active->'bundle'->>'gym_id' is distinct from gym::text
  or active->'bundle'->>'echo_account_key' is distinct from p_base
  or active->>'fact_approval_mode' is distinct from 'delegated_policy'
  or active->>'fact_validation' is distinct from 'supported_uncontradicted'
  or jsonb_typeof(active->'observation') is distinct from 'object' then
  raise exception 'current supported source-brand observation required' using errcode='23514'; end if;
 return jsonb_build_object('consumer_contract','echo-source-brand-delegated-v1',
  'echo_account_key',p_base,'gym_id',gym,'active',active);
end $$;

create function public.fixer_generated_bundle_validate_20261007(p_base text,p_pins jsonb,p_derivation jsonb,
 p_caption text,p_copy_digest text,p_palette_digest text,p_palette_revision text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare active jsonb; b jsonb; o jsonb; receipt jsonb; snap jsonb; witness jsonb;
 copy jsonb; palette jsonb; cap public.echo_source_captures%rowtype; keys text[];
begin
 if jsonb_typeof(p_pins) is distinct from 'object' then raise exception 'delegated bundle pins required' using errcode='23514'; end if;
 select array_agg(key order by key) into keys from jsonb_object_keys(p_pins) key;
 if keys is distinct from array['bundle_id','bundle_version','configuration_receipt_sha256','configuration_sha256',
  'derivation_sha256','echo_account_key','gym_id','mode','observation_id','observation_sha256','validator_revision']
  or p_pins->>'mode' is distinct from 'delegated_policy' or p_pins->>'echo_account_key' is distinct from p_base
  or jsonb_typeof(p_pins->'bundle_version') is distinct from 'number'
  or coalesce(p_pins->>'bundle_version','') !~ '^[1-9][0-9]*$'
  or jsonb_typeof(p_pins->'observation_id') is distinct from 'number'
  or coalesce(p_pins->>'observation_id','') !~ '^[1-9][0-9]*$'
  or exists(select 1 from unnest(array['configuration_receipt_sha256','configuration_sha256','derivation_sha256','observation_sha256']) k
    where coalesce(p_pins->>k,'') !~ '^[0-9a-f]{64}$') then
  raise exception 'strict delegated bundle pins required' using errcode='23514'; end if;
 active:=public.fixer_generated_source_brand_active_20261007(p_base)->'active';
 b:=active->'bundle'; o:=active->'observation'; receipt:=active->'approval_receipt';
 if p_pins->>'gym_id' is distinct from b->>'gym_id' or p_pins->>'bundle_id' is distinct from b->>'id'
  or p_pins->'bundle_version' is distinct from b->'version'
  or p_pins->>'configuration_sha256' is distinct from b->>'content_sha256'
  or p_pins->>'configuration_receipt_sha256' is distinct from public.fixer_generated_bundle_digest_20261007(receipt)
  or p_pins->'observation_id' is distinct from o->'id'
  or p_pins->>'observation_sha256' is distinct from o->>'content_sha256'
  or p_pins->>'validator_revision' is distinct from o->>'validator_revision'
  or coalesce(btrim(p_pins->>'validator_revision'),'')=''
  or (o->>'created_at')::timestamptz < (receipt->>'created_at')::timestamptz
  or (o->>'created_at')::timestamptz > clock_timestamp()
  or o->'validation_report'->>'selected_facts_status' is distinct from 'supported_uncontradicted'
  or o->'validation_report'->>'identity_status' is distinct from 'verified'
  or o->>'configuration_sha256' is distinct from b->>'content_sha256'
  or encode(sha256(convert_to(b->>'snapshot_bytes','UTF8')),'hex') is distinct from b->>'content_sha256'
  or encode(sha256(convert_to(o->>'snapshot_bytes','UTF8')),'hex') is distinct from o->>'content_sha256' then
  raise exception 'current source-brand immutable pins changed' using errcode='23514'; end if;
 snap:=(o->>'snapshot_bytes')::jsonb;
 if jsonb_typeof(p_derivation) is distinct from 'object'
  or p_derivation is distinct from jsonb_build_object('policy','verbatim_selected_fact_v1','caption',p_caption,
    'copy_digest',p_copy_digest,'fact_witness',p_derivation->'fact_witness')
  or p_caption is null or length(btrim(p_caption)) not between 1 and 4000 or p_caption ~ '[-–—:;]'
  or public.fixer_generated_bundle_digest_20261007(p_derivation) is distinct from p_pins->>'derivation_sha256' then
  raise exception 'immutable verbatim derivation required' using errcode='23514'; end if;
 witness:=p_derivation->'fact_witness';
 if witness->>'text' is distinct from p_caption
  or not exists(select 1 from jsonb_array_elements(snap->'selected_facts') fact where fact=witness) then
  raise exception 'exact current selected fact witness required' using errcode='23514'; end if;
 select * into cap from public.echo_source_captures where id=(witness->>'capture_id')::uuid
  and gym_id=(p_pins->>'gym_id')::uuid and echo_account_key=p_base;
 if not found or cap.bytes_sha256 is distinct from witness->>'bytes_sha256'
  or coalesce(cap.source_locator,cap.source_url) is distinct from witness->>'source_locator'
  or coalesce(witness->>'byte_offset','') !~ '^[0-9]+$'
  or coalesce(witness->>'byte_length','') !~ '^[1-9][0-9]*$'
  or (witness->>'byte_length')::integer not between 1 and 2000
  or (witness->>'byte_offset')::bigint+(witness->>'byte_length')::bigint>octet_length(cap.raw_bytes)
  or convert_from(substring(cap.raw_bytes from (witness->>'byte_offset')::integer+1 for (witness->>'byte_length')::integer),'UTF8') is distinct from p_caption then
  raise exception 'exact fact bytes unavailable' using errcode='23514'; end if;
 copy:=jsonb_build_object('headline',p_caption,'facts',jsonb_build_array(p_caption),'cta','','footer','');
 palette:=jsonb_build_object('gym_id',p_base,'verified',true,'colors',jsonb_build_array(snap->'palette'->>'primary',snap->'palette'->>'secondary'),
  'evidence_ref','source-brand-observation:sha256:'||(o->>'content_sha256'));
 if p_copy_digest is distinct from public.fixer_generated_bundle_digest_20261007(copy)
  or p_palette_digest is distinct from public.fixer_generated_bundle_digest_20261007(palette)
  or p_palette_revision is distinct from 'source-brand-palette:sha256:'||public.fixer_generated_bundle_digest_20261007(snap->'palette') then
  raise exception 'delegated copy or palette digest changed' using errcode='23514'; end if;
 return active;
end $$;

-- Latest-authority local census. Exactly one newest trusted current-epoch,
-- current-gym census governs pre-generation,
-- reservation and final send, resolved by deterministic observed_at/receipt
-- ordering. An older zero row can never override a newer positive or
-- incomplete census, including after an inventory revision changes and returns.
-- Select newest first, then require its exact current revision. Missing rows
-- return a null receipt and every consumer holds.
create function public.fixer_generated_local_census_authority_20261008(p_gym text,p_revision text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype;
 i public.fixer_still_inventory_20261007%rowtype;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
  or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 if nullif(btrim(p_gym),'') is null or nullif(btrim(p_revision),'') is null then
  raise exception 'exact current inventory revision required' using errcode='23514'; end if;
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if not coalesce(s.enabled,false) or s.epoch_id is null then
  return jsonb_build_object('enabled',false,'receipt_id',null); end if;
 select * into i from public.fixer_still_inventory_20261007 c
  where c.epoch_id=s.epoch_id and c.gym_id=p_gym
  order by c.observed_at desc,c.receipt_id desc limit 1;
 if not found or i.inventory_revision is distinct from p_revision then
  return jsonb_build_object('enabled',true,'receipt_id',null); end if;
 return jsonb_build_object('enabled',true,'receipt_id',i.receipt_id,'epoch_id',s.epoch_id,
  'local_complete',i.local_complete,'local_available',i.local_available,'observed_at',i.observed_at);
end $$;

-- Final send applies the same exact URL duplicate fence as reservation.
-- Existing verified same-date/logical sibling exception remains above the
-- comparison; unrelated historical rows never inherit that exception.
create or replace function public.fixer_generated_runtime_check_20261007(p_id uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; g public.fixer_generated_reservation_20261007%rowtype;
 s record; snap jsonb; h jsonb; ph text; i public.fixer_still_inventory_20261007%rowtype;
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
   or still.original->>'md5'='md5:'||(g.candidate_json->>'original_md5')
   or still.original->>'source_url'=g.candidate_json->>'original_url'
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
 -- identity, not freshness. The single NEWEST trusted current-epoch census at
 -- newest row must match the live inventory revision, be complete, zero-supply and fresh
 -- inside ten minutes. An older zero row never overrides a newer positive or
 -- incomplete census; a missing, stale, noncomplete or nonzero latest row
 -- holds the send, exactly as for stills.
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if coalesce(s.enabled,false) and s.epoch_id is not null then
  select * into i from public.fixer_still_inventory_20261007 c
   where c.epoch_id=s.epoch_id and c.gym_id=r.gym_id
   order by c.observed_at desc,c.receipt_id desc limit 1;
 end if;
 if not coalesce(s.enabled,false) or s.epoch_id is null or i.receipt_id is null
  or i.inventory_revision is distinct from snap->>'inventory_revision'
  or not i.local_complete or i.local_available<>0
  or i.observed_at<clock_timestamp()-interval '10 minutes'
  or i.observed_at>clock_timestamp() then
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
   and v.visual_sha256=h->>'visual_sha256' and v.published_binding_ref=h->>'published_binding_ref'
   and v.visual_url=h->>'visual_url';
  if not found then
   select candidate_json->>'original_phash' into ph from public.fixer_generated_reservation_20261007 sibling
    where h->>'history_key'='generated-reserved:'||sibling.job_id::text;
  end if;
  end if;
  if ph is null or public.fixer_generated_hamming_20261007(ph,g.candidate_json->>'original_phash')<=6
   or h->>'visual_sha256'='sha256:'||(g.candidate_json->>'original_sha256')
   or h->>'visual_url'=g.candidate_json->>'original_url' then
   raise exception 'generated historical perceptual identity unresolved or repeated' using errcode='23514'; end if;
 end loop;
 return true;
end; $$;

create function public.fixer_reserve_generated_bundle_20261007(p_id uuid,c jsonb,visuals jsonb,m jsonb,
 p_source_revision text default null)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; h jsonb; proof jsonb; prior public.fixer_generated_reservation_20261007%rowtype;
 r public.content_calendar%rowtype; receipt text; original jsonb; clearance jsonb; fp text; asset text;
 s public.fixer_still_cutover_20261007%rowtype; i public.fixer_still_inventory_20261007%rowtype;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
  or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 -- Only the isolated owner supplies a current delegated bundle derivation.
 -- SQL rechecks it against the authenticated portal contract under its locks.
 if (p_source_revision ~ '^source-brand:sha256:[0-9a-f]{64}$') is distinct from true then
  raise exception 'verified approved source revision required' using errcode='23514'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated reservation requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 if jsonb_typeof(c) is distinct from 'object' or not (c ?& array['schema_version','source_type','gym_id','local_date','logical_post_id','copy_revision','palette_revision','inventory_revision','history_revision','copy_digest','palette_digest','review_policy_id','job_id','provider','model','provider_response_id','provider_output_id','original_sha256','original_md5','original_length','original_phash','width','height','review_response_id','storage_key','original_url','storage_readback_sha256','authority_pins','copy_derivation_receipt'])
  or c-array['schema_version','source_type','gym_id','local_date','logical_post_id','copy_revision','palette_revision','inventory_revision','history_revision','copy_digest','palette_digest','review_policy_id','job_id','provider','model','provider_response_id','provider_output_id','original_sha256','original_md5','original_length','original_phash','width','height','review_response_id','storage_key','original_url','storage_readback_sha256','authority_pins','copy_derivation_receipt'] is distinct from '{}'::jsonb
  or c->'width' is distinct from '1024'::jsonb or c->'height' is distinct from '1280'::jsonb
  or c->>'storage_key' is distinct from 'echo-generated-originals/'||(c->>'gym_id')||'/'||(c->>'original_sha256')||'.png' then
  raise exception 'strict complete v2 generated candidate required' using errcode='23514'; end if;
 perform public.fixer_generated_bundle_validate_20261007(c->>'gym_id',c->'authority_pins',c->'copy_derivation_receipt',
  c->'copy_derivation_receipt'->>'caption',c->>'copy_digest',c->>'palette_digest',c->>'palette_revision');
 if p_source_revision is distinct from 'source-brand:sha256:'||public.fixer_generated_bundle_digest_20261007(c->'authority_pins')
  or c->>'review_policy_id' is distinct from 'gym-infographic-copy-palette-v1'
  or c->>'job_id' is distinct from public.fixer_generated_bundle_job_20261007(c)::text then
  raise exception 'exact v2 generated job identity required' using errcode='23514'; end if;
 select * into r from public.content_calendar where id=p_id for update;
 if r.caption is distinct from c->'copy_derivation_receipt'->>'caption' then
  raise exception 'reserved caption differs from verbatim derivation' using errcode='23514'; end if;
 perform public.fixer_generated_approved_visual_guard_20261007(r,c,m);
 snap:=public.fixer_generated_snapshot_20261007(p_id);
 if jsonb_typeof(c)<>'object' or c->'schema_version' is distinct from '2'::jsonb
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
   or prior.approved_source_revision is distinct from p_source_revision then
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
 -- A first generated reservation also requires the newest trusted
 -- current-epoch/current-gym census, observed under the
 -- census advisory lock already held above: exact current revision, complete, zero-supply and fresh.
 -- A late local photo arrival between preflight and reserve leaves a newer
 -- positive/incomplete census that an older zero row can never override.
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if coalesce(s.enabled,false) and s.epoch_id is not null then
  select * into i from public.fixer_still_inventory_20261007 ci
   where ci.epoch_id=s.epoch_id and ci.gym_id=c->>'gym_id'
   order by ci.observed_at desc,ci.receipt_id desc limit 1;
 end if;
 if not coalesce(s.enabled,false) or s.epoch_id is null or i.receipt_id is null
  or i.inventory_revision is distinct from snap->>'inventory_revision'
  or not i.local_complete or i.local_available<>0
  or i.observed_at<clock_timestamp()-interval '10 minutes'
  or i.observed_at>clock_timestamp() then
  raise exception 'generated reservation requires fresh local depletion authority' using errcode='23514'; end if;
 for h in select value from jsonb_array_elements(snap#>'{history,rows}') loop
  select value into proof from jsonb_array_elements(visuals) v(value)
   where value->>'history_key'=h->>'history_key'
     and (h->>'visual_sha256' is null or value->>'visual_sha256'=h->>'visual_sha256')
     and value->>'published_binding_ref'=h->>'published_binding_ref';
  if not found or (proof->>'phash' ~ '^scene:phash64:[0-9a-f]{16}$') is distinct from true
   or (proof->>'visual_sha256' ~ '^sha256:[0-9a-f]{64}$') is distinct from true
   or proof->>'visual_url' is distinct from h->>'visual_url'
   or public.fixer_generated_hamming_20261007(c->>'original_phash',proof->>'phash')<=6
   or 'sha256:'||(c->>'original_sha256')=proof->>'visual_sha256'
   or c->>'original_url'=h->>'visual_url' then
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
 values((c->>'job_id')::uuid,p_id,r.visual_group_key,c,m,receipt,snap#>'{history,epoch}',p_source_revision);
 insert into public.fixer_forward_media_original_registry_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)
 values(c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'astra-job:'||(c->>'job_id'));
 insert into public.fixer_forward_media_history_clearance_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref)
 values(c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'astra-job:'||(c->>'job_id'),'cleared_unused',receipt);
 insert into public.fixer_forward_media_render_manifest_20261006(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,operation,render_recipe,render_evidence_ref)
 values(m->>'manifest_digest',c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'same_object',null,asset);
 update public.content_calendar set source_media_asset_id=asset,source_media_url=c->>'original_url',image_url=c->>'original_url',thumbnail_url=null,render_manifest_digest=m->>'manifest_digest' where id=p_id;
 return jsonb_build_object('reserved',true,'replayed',false,'receipt_ref',receipt,'manifest',m);
end; $$;

alter table public.fixer_generated_gap_request_20261007 add column bundle_pins jsonb, add column copy_derivation_receipt jsonb;

create function public.fixer_generated_gap_bind_bundle_20261007(
 p_request_id uuid,p_row_id uuid,p_logical_post_id uuid,p_group_key text,
 p_caption text,p_copy_ref text,p_palette_revision text,p_palette_digest text,p_palette_ref text,p_pins text,p_derivation text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.fixer_generated_gap_request_20261007%rowtype;
 r public.content_calendar%rowtype; snap jsonb;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
  or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'gap bind requires read committed' using errcode='25000'; end if;
 if p_request_id is null or p_row_id is null or p_logical_post_id is null
  or p_group_key is null or p_group_key !~ '^vg_[a-z0-9_]{1,120}$'
  or p_caption is null or length(btrim(p_caption)) not between 1 and 4000
  or p_caption ~ '[-–—:;]'
  or p_copy_ref is null or p_copy_ref !~ '^source-brand:sha256:[0-9a-f]{64}$'
  or p_palette_revision is null or p_palette_revision !~ '^source-brand-palette:sha256:[0-9a-f]{64}$'
  or p_palette_digest is null or p_palette_digest !~ '^[0-9a-f]{64}$'
  or p_palette_ref is null or p_palette_ref !~ '^source-brand-observation:sha256:[0-9a-f]{64}$' then
  raise exception 'approved current generated content refs required' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into q from public.fixer_generated_gap_request_20261007 where request_id=p_request_id;
 if not found or q.local_date not between current_date-1 and current_date+3 then
  raise exception 'current bounded gap request required' using errcode='23514'; end if;
 perform public.fixer_generated_bundle_validate_20261007(q.gym_id,p_pins::jsonb,p_derivation::jsonb,p_caption,
  p_derivation::jsonb->>'copy_digest',p_palette_digest,p_palette_revision);
 if p_copy_ref is distinct from 'source-brand:sha256:'||public.fixer_generated_bundle_digest_20261007(p_pins::jsonb)
  or p_palette_ref is distinct from 'source-brand-observation:sha256:'||(p_pins::jsonb->>'observation_sha256') then
  raise exception 'exact delegated gap refs required' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtextextended('generated-gap|'||q.gym_id||'|'||q.local_date::text||'|'||q.account||'|feed',0));
 select * into q from public.fixer_generated_gap_request_20261007 where request_id=p_request_id for update;
 if q.calendar_row_id is not null then
  if q.calendar_row_id is distinct from p_row_id or q.logical_post_id is distinct from p_logical_post_id
   or q.group_key is distinct from p_group_key or q.caption is distinct from p_caption
   or q.copy_ref is distinct from p_copy_ref or q.palette_revision is distinct from p_palette_revision
   or q.palette_digest is distinct from p_palette_digest or q.palette_ref is distinct from p_palette_ref
   or q.bundle_pins is distinct from p_pins::jsonb or q.copy_derivation_receipt is distinct from p_derivation::jsonb then
   raise exception 'gap binding immutable identity conflict' using errcode='23514'; end if;
  select * into r from public.content_calendar where id=q.calendar_row_id for update;
  if not found or r.gym_id is distinct from q.gym_id or r.post_date is distinct from q.local_date
   or r.account is distinct from q.account or r.format is distinct from q.format
   or r.logical_post_id is distinct from q.logical_post_id or r.visual_group_key is distinct from q.group_key
   or r.caption is distinct from q.caption or r.variant_status is distinct from 'active'
   or r.status not in ('pending','approved','publishing','published') then
   raise exception 'gap bound calendar changed or removed' using errcode='23514'; end if;
  return jsonb_build_object('bound',true,'replayed',true,'calendar_row_id',r.id,'logical_post_id',r.logical_post_id,
   'gym_id',r.gym_id,'local_date',r.post_date,'account',r.account,'format',r.format);
 end if;
 if exists(select 1 from public.content_calendar c where c.gym_id=q.gym_id and c.post_date=q.local_date
  and (case lower(btrim(coalesce(c.account,''))) when '' then 'instagram' when 'ig' then 'instagram' when 'facebook_page' then 'facebook' when 'fb' then 'facebook' else lower(btrim(c.account)) end)=q.account and lower(btrim(coalesce(c.format,'feed')))='feed'
  and coalesce(c.variant_status,'active')='active' and coalesce(c.status,'pending') not in ('denied','killed','deleted')) then
  raise exception 'active feed slot already occupied' using errcode='23514'; end if;
 insert into public.content_calendar(id,gym_id,post_date,account,format,logical_post_id,visual_group_key,caption,
  status,variant_status,image_url,thumbnail_url,source_media_url,source_media_asset_id,render_manifest_digest)
 values(p_row_id,q.gym_id,q.local_date,q.account,'feed',p_logical_post_id,p_group_key,p_caption,
  'pending','active',null,null,null,null,null);
 -- The row is an unsent null-image placeholder. Under graph/census locks the
 -- generated snapshot now proves inventory and the sealed history epoch.
 snap:=public.fixer_generated_snapshot_20261007(p_row_id);
 if snap->'photo_inventory_complete' is distinct from 'true'::jsonb or snap->'eligible_photo_count' is distinct from '0'::jsonb
  or snap->'history_complete' is distinct from 'true'::jsonb then
  raise exception 'photo depletion or sealed history unavailable' using errcode='23514'; end if;
 update public.fixer_generated_gap_request_20261007 set state='bound',calendar_row_id=p_row_id,
  logical_post_id=p_logical_post_id,group_key=p_group_key,caption=p_caption,copy_ref=p_copy_ref,
  palette_revision=p_palette_revision,palette_digest=p_palette_digest,palette_ref=p_palette_ref,bundle_pins=p_pins::jsonb,copy_derivation_receipt=p_derivation::jsonb,last_hold=null
  where request_id=p_request_id;
 return jsonb_build_object('bound',true,'replayed',false,'calendar_row_id',p_row_id,'logical_post_id',p_logical_post_id,
  'gym_id',q.gym_id,'local_date',q.local_date,'account',q.account,'format','feed');
end; $$;

alter function public.fixer_generated_publish_readback_20261007(uuid) rename to fixer_generated_bundle_pre_readback_20261007;
revoke all on function public.fixer_generated_bundle_pre_readback_20261007(uuid) from public,anon,authenticated,service_role;
create function public.fixer_generated_publish_readback_20261007(p_id uuid)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare binding jsonb; c jsonb;
begin
 binding:=public.fixer_generated_bundle_pre_readback_20261007(p_id);
 if binding is null then return null; end if;
 select candidate_json into c from public.fixer_generated_reservation_20261007 where job_id=(binding->>'job_id')::uuid;
 if c->'schema_version' is distinct from '2'::jsonb then return null; end if;
 return binding||jsonb_build_object('schema_version',2,'copy_derivation_receipt',c->'copy_derivation_receipt');
end $$;

create or replace function public.generated_approval_pins_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare p jsonb; c jsonb;
begin
 if new.source_media_asset_id not like 'generated-astra:%' or new.source_media_asset_id is null then
  new.generated_authority_pins:=null; return new; end if;
 if tg_op='UPDATE' and old.source_media_asset_id like 'generated-astra:%'
  and new.status in ('publishing','published') then
  if new.generated_authority_pins is distinct from old.generated_authority_pins then
   raise exception 'generated approval pins cannot change during send' using errcode='23514'; end if;
  return new;
 end if;
 if new.status is distinct from 'approved' then
  new.generated_authority_pins:=null; return new; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated approval requires read committed' using errcode='25000'; end if;
 -- A row trigger already owns a row lock. Never wait in reverse lock order.
 if not pg_try_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(new.gym_id)) then
  raise exception 'generated approval authority busy' using errcode='55000'; end if;
 if exists(select 1 from public.generated_revocation_request_20261007 where tenant_id=new.gym_id and state='pending') then
  raise exception 'generated revocation pending' using errcode='55000'; end if;
 select candidate_json into c from public.fixer_generated_reservation_20261007
  where 'generated-astra:'||job_id::text=new.source_media_asset_id;
 p:=c->'authority_pins';
 if c->'schema_version' is distinct from '2'::jsonb then
  raise exception 'owner delegated v2 pins required at approval' using errcode='23514'; end if;
 -- Portal lock is later in the established order. A row trigger never waits
 -- in reverse order; all nested acquisitions are already owned or try-locked.
 if not pg_try_advisory_xact_lock(hashtextextended(p->>'gym_id',0)) then
  raise exception 'generated approval bundle busy' using errcode='55000'; end if;
 perform public.fixer_generated_bundle_validate_20261007(new.gym_id,p,c->'copy_derivation_receipt',new.caption,
  c->>'copy_digest',c->>'palette_digest',c->>'palette_revision');
 new.generated_authority_pins:=p;
 return new;
end; $$;

create or replace function public.generated_send_acquire_20261007(p_attempt uuid,p_tenant text,p_row uuid,p_claim uuid,p_job uuid,p_pins jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; g public.fixer_generated_reservation_20261007%rowtype;
 claim public.fixer_forward_media_claim_receipt_20261006%rowtype; prior public.generated_send_lease_20261007%rowtype;
 binding jsonb;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 if p_attempt is null or p_row is null or p_claim is null or p_job is null
  or coalesce(p_tenant,'') !~ '[^[:space:]]' or jsonb_typeof(p_pins) is distinct from 'object'
  or p_pins->>'echo_account_key' is distinct from p_tenant or p_pins->>'mode' is distinct from 'delegated_policy' then
  raise exception 'exact delegated send identity and pins required' using errcode='23514'; end if;
 -- Same graph -> census -> canonical send -> portal gym -> calendar row order.
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(p_tenant));
 select * into prior from public.generated_send_lease_20261007 where attempt_token=p_attempt;
 if found then
  if prior.tenant_id is distinct from p_tenant or prior.calendar_row_id is distinct from p_row
   or prior.claim_token is distinct from p_claim or prior.job_id is distinct from p_job
   or prior.authority_pins is distinct from p_pins then
   raise exception 'attempt token immutable binding conflict' using errcode='23514'; end if;
  return to_jsonb(prior)||jsonb_build_object('replayed',true,'authorize_send',false);
 end if;
 if exists(select 1 from public.generated_revocation_request_20261007 where tenant_id=p_tenant and state='pending') then
  raise exception 'generated revocation pending; new send lease blocked' using errcode='55000'; end if;
 perform public.fixer_generated_source_brand_active_20261007(p_tenant);
 select * into r from public.content_calendar where id=p_row for update;
 if not found or r.gym_id is distinct from p_tenant or r.publish_claim_token is distinct from p_claim
  or r.status is distinct from 'publishing' or r.published_at is not null or r.late_post_id is not null
  or r.account not in ('instagram','facebook') or r.format is distinct from 'feed'
  or r.source_media_asset_id is distinct from 'generated-astra:'||p_job::text then
  raise exception 'owned unsent generated publishing row required' using errcode='23514'; end if;
 select * into g from public.fixer_generated_reservation_20261007 where job_id=p_job;
 if not found or g.candidate_json->'authority_pins' is distinct from p_pins then
  raise exception 'canonical pins missing from immutable generated reservation' using errcode='23514'; end if;
 if r.generated_authority_pins is distinct from p_pins then
  raise exception 'exact canonical approval pins required' using errcode='23514'; end if;
 perform public.fixer_generated_runtime_check_20261007(p_row);
 binding:=public.fixer_generated_publish_readback_20261007(p_row);
 if binding is null or binding->>'job_id' is distinct from p_job::text or binding->>'gym_id' is distinct from p_tenant then
  raise exception 'generated reservation readback unavailable' using errcode='23514'; end if;
 select * into claim from public.fixer_forward_media_claim_receipt_20261006 where claim_token=p_claim;
 if not found or claim.calendar_row_id is distinct from p_row or claim.tenant_id is distinct from p_tenant
  or claim.post_date is distinct from r.post_date or claim.group_key is distinct from r.visual_group_key
  or claim.source_url is distinct from r.source_media_url or claim.image_url is distinct from r.image_url
  or claim.thumbnail_url is distinct from r.thumbnail_url or claim.reservation_day is distinct from r.publish_reservation_day then
  raise exception 'committed exact forward claim receipt required' using errcode='23514'; end if;
 if g.candidate_json->'schema_version' is distinct from '2'::jsonb then
  raise exception 'delegated v2 reservation required' using errcode='23514'; end if;
 perform public.fixer_generated_bundle_validate_20261007(p_tenant,p_pins,g.candidate_json->'copy_derivation_receipt',r.caption,
  g.candidate_json->>'copy_digest',g.candidate_json->>'palette_digest',g.candidate_json->>'palette_revision');
 if exists(select 1 from public.generated_send_lease_20261007 where calendar_row_id=p_row and state='sent') then
  raise exception 'generated row already has a sent attempt' using errcode='23514'; end if;
 insert into public.generated_send_lease_20261007 values(p_attempt,p_tenant,p_row,p_claim,p_job,p_pins,'reserved',null,clock_timestamp(),clock_timestamp());
 insert into public.generated_send_audit_20261007(tenant_id,event,identity_token,payload) values(p_tenant,'reserve',p_attempt,p_pins);
 return jsonb_build_object('attempt_token',p_attempt,'state','reserved','replayed',false,'authorize_send',false);
end; $$;

create function public.generated_send_validate_20261007(p_attempt uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare l public.generated_send_lease_20261007%rowtype; r public.content_calendar%rowtype;
 g public.fixer_generated_reservation_20261007%rowtype; binding jsonb;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into l from public.generated_send_lease_20261007 where attempt_token=p_attempt;
 if not found then raise exception 'unknown generated attempt' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(l.tenant_id));
 perform public.fixer_generated_source_brand_active_20261007(l.tenant_id);
 select * into l from public.generated_send_lease_20261007 where attempt_token=p_attempt for update;
 if l.state not in ('reserved','inflight') then
  return to_jsonb(l)||jsonb_build_object('authorize_send',false); end if;
 if exists(select 1 from public.generated_revocation_request_20261007 where tenant_id=l.tenant_id and state='pending') then
  raise exception 'pending revocation blocks provider mutation' using errcode='55000'; end if;
 select * into r from public.content_calendar where id=l.calendar_row_id for update;
 if not found or r.gym_id is distinct from l.tenant_id or r.status is distinct from 'publishing'
  or r.publish_claim_token is distinct from l.claim_token or r.published_at is not null or r.late_post_id is not null
  or r.generated_authority_pins is distinct from l.authority_pins then
  raise exception 'reserved publishing identity changed before mutation' using errcode='23514'; end if;
 perform public.fixer_generated_runtime_check_20261007(l.calendar_row_id);
 binding:=public.fixer_generated_publish_readback_20261007(l.calendar_row_id);
 if binding is null or binding->>'job_id' is distinct from l.job_id::text
  or binding->'authority_pins' is distinct from l.authority_pins then
  raise exception 'immutable delegated binding unavailable' using errcode='23514'; end if;
 select * into g from public.fixer_generated_reservation_20261007 where job_id=l.job_id;
 perform public.fixer_generated_bundle_validate_20261007(l.tenant_id,l.authority_pins,g.candidate_json->'copy_derivation_receipt',
  r.caption,g.candidate_json->>'copy_digest',g.candidate_json->>'palette_digest',g.candidate_json->>'palette_revision');
 return jsonb_build_object('attempt_token',p_attempt,'state',l.state,'authorize_send',l.state='inflight');
end $$;
create or replace function public.generated_send_begin_20261007(p_attempt uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare result jsonb; l public.generated_send_lease_20261007%rowtype;
begin
 result:=public.generated_send_validate_20261007(p_attempt);
 select * into l from public.generated_send_lease_20261007 where attempt_token=p_attempt for update;
 if l.state<>'reserved' then return to_jsonb(l)||jsonb_build_object('authorize_send',false); end if;
 update public.generated_send_lease_20261007 set state='inflight',updated_at=clock_timestamp() where attempt_token=p_attempt;
 insert into public.generated_send_audit_20261007(tenant_id,event,identity_token,payload) values(l.tenant_id,'begin',p_attempt,'{}');
 return jsonb_build_object('attempt_token',p_attempt,'state','inflight','authorize_send',true);
end $$;

-- Appended captures, validation, config, capability and mapping changes share
-- the portal lock. Fail closed during outstanding decisions; no TTL, synthetic
-- approval, auto reconciliation or forged effective-revocation receipt.
create function public.generated_bundle_portal_fence_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare gym uuid; old_gym uuid; alias text; old_alias text;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated bundle mutation requires read committed' using errcode='25000'; end if;
 gym:=case when tg_op='DELETE' then old.gym_id else new.gym_id end;
 if tg_op='UPDATE' then old_gym:=old.gym_id; end if;
 if tg_table_name in ('fixer_generated_portal_tenant_map_20261008','echo_intake_tokens') then
  alias:=case when tg_op='DELETE' then old.echo_account_key else new.echo_account_key end;
  if tg_op='UPDATE' then old_alias:=old.echo_account_key; end if;
  -- Mapping writers already hold row locks. Never wait in reverse order:
  -- canonical account precedes portal UUID, including both old and new pairs.
  if not pg_try_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(alias))
   or (old_alias is not null and not pg_try_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(old_alias))) then
   raise exception 'generated bundle send decision busy; account mapping blocked' using errcode='55000'; end if;
 end if;
 if not pg_try_advisory_xact_lock(hashtextextended(gym::text,0))
  or (old_gym is not null and not pg_try_advisory_xact_lock(hashtextextended(old_gym::text,0))) then
  raise exception 'generated bundle send decision busy' using errcode='55000'; end if;
 if exists(select 1 from public.generated_send_lease_20261007 where state in ('reserved','inflight','unknown')
  and authority_pins->>'mode'='delegated_policy' and authority_pins->>'gym_id'=any(array[gym::text,old_gym::text])) then
  raise exception 'outstanding generated attempt freezes portal authority' using errcode='55000'; end if;
 if alias is not null and exists(select 1 from public.generated_send_lease_20261007
  where state in ('reserved','inflight','unknown') and tenant_id=any(array[alias,old_alias])) then
  raise exception 'outstanding generated attempt freezes account mapping' using errcode='55000'; end if;
 if tg_op='DELETE' then return old; end if; return new;
end $$;
create function public.generated_bundle_actor_fence_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0)) then
  raise exception 'generated send decision busy; actor mutation blocked' using errcode='55000'; end if;
 if exists(select 1 from public.generated_send_lease_20261007 l
  join public.echo_source_brand_receipts receipt on receipt.bundle_id::text=l.authority_pins->>'bundle_id'
  where l.state in ('reserved','inflight','unknown') and receipt.actor_clerk_user_id=old.clerk_user_id) then
  raise exception 'outstanding generated attempt freezes actor authority' using errcode='55000'; end if;
 if tg_op='DELETE' then return old; end if; return new;
end $$;
create function public.generated_bundle_truncate_fence_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0)) then
  raise exception 'generated send decision busy; authority truncate blocked' using errcode='55000'; end if;
 if exists(select 1 from public.generated_send_lease_20261007 where state in ('reserved','inflight','unknown')
  and authority_pins->>'mode'='delegated_policy') then
  raise exception 'outstanding generated attempt freezes portal authority' using errcode='55000'; end if;
 return null;
end $$;
do $$ declare t text; begin
 foreach t in array array['echo_source_captures','echo_source_brand_provider_status','echo_source_brand_receipts','echo_source_brand_observations',
  'echo_source_brand_capability_events','echo_intake_tokens','fixer_generated_portal_tenant_map_20261008'] loop
  execute format('create trigger generated_bundle_send_fence before insert or update or delete on public.%I for each row execute function public.generated_bundle_portal_fence_20261007()',t);
 end loop;
 foreach t in array array['echo_source_captures','echo_source_brand_provider_status','echo_source_brand_bundles','echo_source_brand_receipts','echo_source_brand_observations',
  'echo_source_brand_capability_events','echo_intake_tokens','fixer_generated_portal_tenant_map_20261008','app_users'] loop
  execute format('create trigger generated_bundle_truncate_fence before truncate on public.%I for each statement execute function public.generated_bundle_truncate_fence_20261007()',t);
 end loop;
end $$;
create trigger generated_bundle_actor_fence before update or delete on public.app_users
 for each row execute function public.generated_bundle_actor_fence_20261007();

-- Legacy B/gap adapters cannot create new local-hash reservations alongside
-- the installed delegated route. No new privilege to approve configuration.
revoke all on function public.fixer_reserve_generated_20261007(uuid,jsonb,jsonb,jsonb,text),
 public.fixer_generated_gap_bind_20261007(uuid,uuid,uuid,text,text,text,text,text,text)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006;
do $$ declare f text; begin
 foreach f in array array['fixer_generated_bundle_digest_20261007(jsonb)','fixer_generated_bundle_job_20261007(jsonb)',
  'fixer_generated_local_census_authority_20261008(text,text)',
  'fixer_generated_source_brand_active_20261007(text)','fixer_generated_bundle_validate_20261007(text,jsonb,jsonb,text,text,text,text)',
  'fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)',
  'fixer_generated_gap_bind_bundle_20261007(uuid,uuid,uuid,text,text,text,text,text,text,text,text)',
  'fixer_generated_publish_readback_20261007(uuid)','generated_send_validate_20261007(uuid)',
  'generated_bundle_portal_fence_20261007()','generated_bundle_actor_fence_20261007()','generated_bundle_truncate_fence_20261007()'] loop
  execute format('revoke all on function public.%s from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006',f);
 end loop;
end $$;
grant execute on function public.fixer_generated_source_brand_active_20261007(text),
 public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text),
 public.fixer_generated_gap_bind_bundle_20261007(uuid,uuid,uuid,text,text,text,text,text,text,text,text),
 public.fixer_generated_local_census_authority_20261008(text,text)
 to fixer_forward_media_owner_20261006;
grant execute on function public.fixer_generated_source_brand_active_20261007(text),
 public.fixer_generated_publish_readback_20261007(uuid),public.generated_send_validate_20261007(uuid) to service_role;
commit;
