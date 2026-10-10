-- DRAFT only. No activation, source mutation, review or calendar approval.
-- Install LAST after DRAFT_fixer_generated_bundle_bridge_20261008.sql and its
-- assembled prerequisites. Existing receipts
-- remain immutable. The old unbound producer entry is revoked for owners.
begin;
do $$ begin
 if to_regprocedure('public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)') is null
  or to_regprocedure('public.generated_send_validate_20261007(uuid)') is null
  or to_regprocedure('public.fixer_generated_local_census_authority_20261008(text,text)') is null then
  raise exception 'assembled bundle bridge must precede local census producer' using errcode='55000'; end if;
end $$;

-- PRIVATE consumer resolver: choose newest in the active epoch/gym BEFORE
-- revision, completeness, freshness or supply checks. Never revive an old zero.
create function public.fixer_local_census_latest_private_20261008(p_gym text)
returns public.fixer_still_inventory_20261007
language sql security definer set search_path=pg_catalog,public as $$
 select i from public.fixer_still_inventory_20261007 i
 join public.fixer_still_cutover_20261007 s on s.singleton
 where s.enabled and s.epoch_id is not null and i.epoch_id=s.epoch_id and i.gym_id=p_gym
 order by i.observed_at desc,i.receipt_id desc limit 1;
$$;
revoke all on function public.fixer_local_census_latest_private_20261008(text)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,
 fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007,
 generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007;

create function public.fixer_generated_local_census_snapshot_20261008(p_row uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype; r public.content_calendar%rowtype;
 snap jsonb; sources jsonb; assets jsonb;
begin
 perform public.fixer_still_owner_lock_20261007();
 select * into r from public.content_calendar where id=p_row for update;
 if not found or r.status is null or r.status not in ('draft','pending','queued','approved')
  or r.variant_status is distinct from 'active' or r.format is distinct from 'feed'
  or r.published_at is not null or r.late_post_id is not null or r.publish_claim_token is not null then
  raise exception 'unsent current census row required' using errcode='23514'; end if;
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 snap:=public.fixer_generated_snapshot_20261007(p_row);
 -- Include every source kind, even a disconnected or unsupported connection.
 -- Python must HOLD unsupported/inaccessible sources rather than omit them.
 select coalesce(jsonb_agg(to_jsonb(x) order by id),'[]'::jsonb) into sources
  from public.media_source x where x.gym_id=r.gym_id;
 select coalesce(jsonb_agg(to_jsonb(x) order by id),'[]'::jsonb) into assets
  from public.media_asset x where x.gym_id=r.gym_id;
 return jsonb_build_object('calendar_row_id',p_row,'enabled',s.enabled,'epoch_id',s.epoch_id,
  'snapshot',snap,'sources',sources,'assets',assets,
  'source_revision','sha256:'||encode(sha256(convert_to(sources::text,'UTF8')),'hex'),
  'asset_revision','sha256:'||encode(sha256(convert_to(assets::text,'UTF8')),'hex'));
end $$;

create function public.fixer_still_inventory_record_20261007(
 p_id uuid,p_row uuid,p_revision text,p_complete boolean,p_available integer,p_ref text,
 p_epoch uuid,p_expected jsonb,p_observed_at timestamptz)
returns uuid language plpgsql security definer set search_path=pg_catalog,public as $$
declare current_snapshot jsonb; old public.fixer_still_inventory_20261007%rowtype;
begin
 -- Global graph -> census -> calendar row, exactly the established owner order.
 current_snapshot:=public.fixer_generated_local_census_snapshot_20261008(p_row);
 if current_snapshot is distinct from p_expected
  or current_snapshot->'enabled' is distinct from 'true'::jsonb
  or p_epoch is null or p_epoch::text is distinct from current_snapshot->>'epoch_id'
  or p_revision is distinct from current_snapshot#>>'{snapshot,inventory_revision}'
  or p_complete is null or p_available is null or p_available<0
  or p_available<(current_snapshot#>>'{snapshot,eligible_photo_count}')::integer
  -- A producer-only recheck cannot fence library/rotation writers through the
  -- later generation decision. Zero is ALWAYS incomplete on this seam until
  -- an independently verified all-writer fence/invalidation package exists.
  or (p_complete and (p_available=0
    or current_snapshot#>'{snapshot,photo_inventory_complete}' is distinct from 'true'::jsonb
    or current_snapshot#>'{snapshot,history_complete}' is distinct from 'true'::jsonb))
  or p_observed_at is null or p_observed_at>clock_timestamp()
  or p_observed_at<clock_timestamp()-interval '5 minutes'
  or (p_ref ~ '^local-census:sha256:[0-9a-f]{64}$') is distinct from true then
  raise exception 'complete fresh epoch-bound census required' using errcode='23514'; end if;
 insert into public.fixer_still_inventory_20261007
  (receipt_id,epoch_id,gym_id,inventory_revision,local_complete,local_available,evidence_ref,observed_at)
 values(p_id,p_epoch,current_snapshot#>>'{snapshot,gym_id}',p_revision,p_complete,p_available,p_ref,p_observed_at)
 on conflict do nothing;
 select * into old from public.fixer_still_inventory_20261007 where receipt_id=p_id;
 if old.epoch_id is distinct from p_epoch or old.gym_id is distinct from current_snapshot#>>'{snapshot,gym_id}'
  or old.inventory_revision is distinct from p_revision or old.local_complete is distinct from p_complete
  or old.local_available is distinct from p_available or old.evidence_ref is distinct from p_ref
  or old.observed_at is distinct from p_observed_at then
  raise exception 'inventory observation immutable conflict' using errcode='23514'; end if;
 return p_id;
end $$;

create function public.fixer_generated_local_census_receipt_20261008(p_id uuid,p_row uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare current_snapshot jsonb; i public.fixer_still_inventory_20261007%rowtype;
begin
 current_snapshot:=public.fixer_generated_local_census_snapshot_20261008(p_row);
 select * into i from public.fixer_still_inventory_20261007 where receipt_id=p_id;
 if not found or current_snapshot->'enabled' is distinct from 'true'::jsonb
  or i.epoch_id::text is distinct from current_snapshot->>'epoch_id'
  or i.gym_id is distinct from current_snapshot#>>'{snapshot,gym_id}'
  or i.inventory_revision is distinct from current_snapshot#>>'{snapshot,inventory_revision}' then
  raise exception 'current epoch census receipt required' using errcode='23514'; end if;
 return to_jsonb(i);
end $$;

revoke all on function public.fixer_still_inventory_record_20261007(uuid,uuid,text,boolean,integer,text)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
revoke all on function public.fixer_generated_local_census_snapshot_20261008(uuid),
 public.fixer_still_inventory_record_20261007(uuid,uuid,text,boolean,integer,text,uuid,jsonb,timestamptz),
 public.fixer_generated_local_census_receipt_20261008(uuid,uuid)
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_generated_local_census_snapshot_20261008(uuid),
 public.fixer_still_inventory_record_20261007(uuid,uuid,text,boolean,integer,text,uuid,jsonb,timestamptz),
 public.fixer_generated_local_census_receipt_20261008(uuid,uuid)
 to fixer_forward_media_owner_20261006;
-- CREATE OR REPLACE preserves the existing consumer ACLs and complete bridge
-- validation. Generated final-send requires latest zero; photos retain their
-- approved-source checks and may use positive supply. Still reservations bind
-- EXACT newest IDs at both reserve and send. A superseded immutable reservation
-- holds until an explicit reservation under the new receipt is created.
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
 i:=public.fixer_local_census_latest_private_20261008(r.gym_id);
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

create or replace function public.fixer_still_reservation_check_20261007(p_row uuid,o jsonb,p_kind text,p_inventory uuid,p_clearance uuid)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; s public.fixer_still_cutover_20261007%rowtype; snap jsonb; i public.fixer_still_inventory_20261007%rowtype;
begin
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 select * into r from public.content_calendar where id=p_row;
 snap:=public.fixer_generated_snapshot_20261007(p_row);
 i:=public.fixer_local_census_latest_private_20261008(r.gym_id);
 if not s.enabled or not public.fixer_still_tuple_valid_20261007(o) or o->>'gym_id' is distinct from r.gym_id
  or p_kind not in ('photo','graphic') or p_kind is null
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or i.receipt_id is null or i.receipt_id is distinct from p_inventory
  or i.inventory_revision is distinct from snap->>'inventory_revision'
  or not i.local_complete or i.observed_at<clock_timestamp()-interval '10 minutes'
  or i.observed_at>clock_timestamp()
  or (p_kind='graphic' and (i.local_available<>0 or snap->'eligible_photo_count' is distinct from '0'::jsonb))
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

create or replace function public.fixer_still_final_check_20261007(p_row uuid,p_receipt uuid)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; s public.fixer_still_cutover_20261007%rowtype;
 g public.fixer_still_reservation_20261007%rowtype; snap jsonb; i public.fixer_still_inventory_20261007%rowtype;
begin
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 select * into r from public.content_calendar where id=p_row;
 select * into g from public.fixer_still_reservation_20261007 where receipt_id=p_receipt;
 snap:=public.fixer_generated_snapshot_20261007(p_row);
 i:=public.fixer_local_census_latest_private_20261008(r.gym_id);
 if g.receipt_id is null or r.id is null or not s.enabled or s.epoch_id is distinct from g.epoch_id
  or g.gym_id is distinct from r.gym_id or g.local_date is distinct from r.post_date
  or g.logical_post_id is distinct from (to_jsonb(r)->>'logical_post_id')::uuid
  or g.group_key is distinct from r.visual_group_key
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or i.receipt_id is null or i.receipt_id is distinct from g.inventory_receipt
  or i.inventory_revision is distinct from snap->>'inventory_revision'
  or not i.local_complete or i.observed_at<clock_timestamp()-interval '10 minutes'
  or i.observed_at>clock_timestamp()
  or (g.media_kind='graphic' and (i.local_available<>0 or snap->'eligible_photo_count' is distinct from '0'::jsonb)) then
  raise exception 'still final send requires current binding and fresh inventory authority' using errcode='23514'; end if;
 if g.media_kind='photo' and not exists(select 1 from public.media_asset a join public.media_source src on src.id=a.source_id
  where a.id=g.original->>'source_asset_id' and a.gym_id=r.gym_id and src.gym_id=r.gym_id and src.active
   and src.kind='gym_drive' and src.sync_status='ready' and a.content_hash=right(g.original->>'md5',32)
   and public.fixer_owner_photo_source_ready_20261007(a.id,g.original->>'sha256')) then
  raise exception 'current approved photo original required' using errcode='23514'; end if;
 perform public.fixer_still_occupancy_check_20261007(p_row,g.original);
end; $$;

commit;
