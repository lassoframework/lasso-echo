-- DRAFT / UNAPPLIED. Install LAST after local census producer, staged
-- preparation, historical/prospective guards and still-v2 owner transport.
-- No census/reservation mutation, grants, activation, provider or production I/O.
-- Adapt composed OIDs IN PLACE so inherited callers keep every guard/ACL.
begin;
do $$ begin
 if to_regprocedure('public.fixer_local_census_latest_private_20261008(text)') is null
  or to_regprocedure('public.fixer_still_v2_owner_pending_20261008(text[],integer)') is null
  or to_regprocedure('public.fixer_pre_staged_provenance_20261008(uuid)') is null then
  raise exception 'current census, staged provenance and still-v2 transport required' using errcode='55000'; end if;
end $$;

-- No reservation is required for a source never enrolled in the still
-- contract. Once that source has a retained reservation, absence of an exact
-- current binding HOLDS; it never falls through to legacy source eligibility.
-- Canonical legacy originals remain eligible under the immutable alias map,
-- but retained raw OR verified canonical reservations enroll the source.
-- Same date/logical post/group siblings may share an immutable original.
create function public.fixer_current_census_reservation_private_20261008(p_row uuid,p jsonb)
returns setof public.fixer_still_reservation_20261007
language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; i public.fixer_still_inventory_20261007%rowtype;
 g public.fixer_still_reservation_20261007%rowtype; canonical_tenant text; source_tenant text;
begin
 select * into r from public.content_calendar where id=p_row;
 select a.tenant_id into canonical_tenant from public.fixer_forward_media_tenant_alias_20261006 a
  where a.alias_key=btrim(r.gym_id);
 source_tenant:=p#>>'{original,tenant_id}';
 if r.id is null or nullif(btrim(r.gym_id),'') is null
  or nullif(btrim(source_tenant),'') is null
  or (source_tenant is distinct from r.gym_id and source_tenant is distinct from canonical_tenant) then
  raise exception 'still reservation requires exact current source tenant' using errcode='23514'; end if;
 if not exists(select 1 from public.fixer_still_reservation_20261007 x
  where (x.gym_id=r.gym_id or x.gym_id=canonical_tenant)
   and x.original->>'source_asset_id'=p#>>'{original,source_asset_id}') then
  return; end if;
 i:=public.fixer_local_census_latest_private_20261008(r.gym_id);
 select * into g from public.fixer_still_reservation_20261007 x
  where x.epoch_id=i.epoch_id and x.inventory_receipt=i.receipt_id
   and x.gym_id=r.gym_id and x.local_date=r.post_date and x.logical_post_id=r.logical_post_id
   and x.group_key=r.visual_group_key
   and x.original->>'gym_id'=r.gym_id
   and x.original->>'source_asset_id'=p#>>'{original,source_asset_id}'
   and x.original->>'source_url'=p#>>'{original,source_url}'
   and x.original->>'md5'=p#>>'{original,source_fingerprint}'
   and x.original->>'length'=p#>>'{original,source_length}'
   and x.original->>'source_url'=p#>>'{manifest,image_url}'
   and p#>>'{manifest,thumbnail_url}' is null
  -- Only after ALL identities match may deterministic ordering choose a row.
  order by x.reserved_at desc,x.receipt_id desc limit 1;
 if not found then
  raise exception 'still reservation requires exact current census binding' using errcode='23514'; end if;
 return next g;
end $$;
revoke all on function public.fixer_current_census_reservation_private_20261008(uuid,jsonb)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,
 fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007,
 generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007;

-- The producer strict ID check holds old reservations; this overlay chooses
-- an explicitly restaged reservation already bound to the newest census. It
-- never substitutes that census ID into a reservation or changes its original.
-- Retain every historical/prospective/v2 injection and complete alias wrapper.
do $adapt$
declare f record; body text; changed integer:=0; normal_count integer:=0; alias_count integer:=0;
 normal_marker text:=$normal$select * into g from public.fixer_still_reservation_20261007 where gym_id=p#>>'{original,tenant_id}'
  and original->>'source_asset_id'=p#>>'{original,source_asset_id}' order by reserved_at limit 1;$normal$;
 alias_marker text:=$alias$select * into still_reservation from public.fixer_still_reservation_20261007
    where gym_id=staged_original.tenant_id
     and original->>'source_asset_id'=staged_original.source_asset_id
    order by reserved_at limit 1;$alias$;
begin
 for f in select p.oid,p.proname,p.prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname=any(array[
   'fixer_forward_media_provenance_lookup_20261006','fixer_pre_staged_provenance_20261008',
   'fixer_pre_still_provenance_20261007','fixer_pre_generated_provenance_20261007',
   'fixer_photo_base_provenance_20261007']) loop
  body:=f.prosrc;
  if position(normal_marker in body)>0 then
   normal_count:=normal_count+1;
   body:=replace(body,normal_marker,
    'select * into g from public.fixer_current_census_reservation_private_20261008(p_id,p);');
  end if;
  if position(alias_marker in body)>0 then
   alias_count:=alias_count+1;
   body:=replace(body,alias_marker,
    'select * into still_reservation from public.fixer_current_census_reservation_private_20261008(p_calendar_row_id,provenance);');
  end if;
  if body is distinct from f.prosrc then
   if position('fixer_prospective_conflict_fence_20261008' in body)=0
    or position('fixer_assert_still_row_v2_or_v1_20261008' in body)=0 then
    raise exception 'composed prospective/v2 guards missing at %',f.proname using errcode='55000'; end if;
   execute replace(pg_get_functiondef(f.oid),f.prosrc,body);
   changed:=changed+1;
  end if;
 end loop;
 if normal_count<>1 or alias_count<>1 or changed<>2 then
  raise exception 'exact ordinary and staged-alias reservation selectors required; found %,%,%',normal_count,alias_count,changed using errcode='55000'; end if;
 if exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='public' and p.proname like '%provenance%'
    and (position(normal_marker in p.prosrc)>0 or position(alias_marker in p.prosrc)>0)) then
  raise exception 'earliest still reservation selector remains reachable' using errcode='55000'; end if;
end $adapt$;
-- media_use is canonical because the claim writer resolves this SAME immutable
-- alias map. Normalize only its comparison to the current calendar row; raw
-- original/reservation ownership remains unchanged everywhere else.
do $alias_use$
declare f record; body text;
 marker text:='not(u.tenant_id=r.gym_id and u.post_date=r.post_date and u.group_key=r.visual_group_key)';
 replacement text:=$binding$not(u.tenant_id=coalesce(
   (select a.tenant_id from public.fixer_forward_media_tenant_alias_20261006 a where a.alias_key=btrim(r.gym_id)),
   nullif(btrim(r.gym_id),'')) and u.post_date=r.post_date and u.group_key=r.visual_group_key)$binding$;
begin
 select oid,prosrc into f from pg_proc
  where oid='public.fixer_still_occupancy_check_20261007(uuid,jsonb)'::regprocedure;
 if position(marker in f.prosrc)=0
  or (length(f.prosrc)-length(replace(f.prosrc,marker,'')))/length(marker)<>1 then
  raise exception 'exact still committed-use tenant predicate required' using errcode='55000'; end if;
 body:=replace(f.prosrc,marker,replacement);
 execute replace(pg_get_functiondef(f.oid),f.prosrc,body);
end $alias_use$;

commit;
