-- Atomic append-only owned LASSO reviewed feed top-up. No existing row mutates.
begin;
create unique index if not exists lasso_runway_feed_slot_unique
 on public.content_calendar (gym_id, account, post_date, slot_index)
 where gym_id='lasso' and account in ('instagram','facebook') and format='feed'
   and variant_status='active' and coalesce(status,'') not in ('denied','killed');

create or replace function public.stage_lasso_runway_feed(
 p_row jsonb, p_policy_version text, p_brain_snapshot jsonb
) returns jsonb language plpgsql security definer set search_path=public as $$
declare
 v public.content_calendar%rowtype;
 prior public.content_calendar%rowtype;
 v_source text;
 v_hash text;
 v_time time;
begin
 -- Strict allowlist excludes claim/receipt/approval/reservation and source
 -- authority fields. The caller cannot publish or introduce a different tenant.
 if p_row is null or jsonb_typeof(p_row) is distinct from 'object' then
  return jsonb_build_object('result','conflict','reason','invalid_input');
 end if;
 if exists (
   select 1 from jsonb_object_keys(p_row) k where k not in
    ('id','gym_id','account','post_date','slot_index','format','caption','image_url',
     'status','variant_status','logical_post_id','scheduled_at','pillar','media_not_ready_reason')
 ) or p_row->>'gym_id' is distinct from 'lasso'
   or coalesce(p_row->>'account','') not in ('instagram','facebook')
   or p_row->>'format' is distinct from 'feed'
   or p_row->>'status' is distinct from 'pending'
   or p_row->>'variant_status' is distinct from 'active'
   or not (p_row ? 'logical_post_id')
   or jsonb_typeof(p_row->'logical_post_id') not in ('null','string')
   or jsonb_typeof(p_row->'slot_index') is distinct from 'number'
   or p_row->>'slot_index' not in ('0','1','2')
   or nullif(btrim(p_row->>'caption'),'') is null
   or coalesce(p_row->>'image_url','') !~ '^https://'
   or nullif(btrim(p_row->>'pillar'),'') is null
   or p_row->>'media_not_ready_reason' is not null
   or nullif(p_policy_version,'') is null or p_brain_snapshot is null
   or p_brain_snapshot = '{}'::jsonb then
  return jsonb_build_object('result','conflict','reason','invalid_input');
 end if;
 begin
  v := jsonb_populate_record(null::public.content_calendar,p_row);
 exception when others then
  return jsonb_build_object('result','conflict','reason','invalid_row_types');
 end;
 if v.id is null or v.scheduled_at is null
    or v.post_date is null or v.post_date not between
      (now() at time zone 'America/New_York')::date and
      (now() at time zone 'America/New_York')::date+29 then
  return jsonb_build_object('result','conflict','reason','outside_runway');
 end if;
 v_time := case v.slot_index when 0 then time '07:30' when 1 then time '18:30' else time '12:00' end;
 if (v.scheduled_at at time zone 'America/New_York')::date <> v.post_date
    or (v.scheduled_at at time zone 'America/New_York')::time <> v_time then
  return jsonb_build_object('result','conflict','reason','noncanonical_schedule');
 end if;
 -- Same tenant lock as owned publisher claims; no row lock precedes it.
 perform pg_advisory_xact_lock(hashtextextended('lasso',0));
 if public.calendar_gym_is_autonomous('lasso') is not true then
  return jsonb_build_object('result','conflict','reason','autonomy_not_confirmed');
 end if;
 v_source := 'content_calendar:'||v.id::text||':caption';
 v_hash := encode(sha256(convert_to(v.caption,'UTF8')),'hex');
 if not exists (
  select 1 from public.echo_infographic_artifacts a
   where a.tenant = case v.account when 'instagram' then 'lasso_ig' else 'lasso_fb' end
    and a.image_url=v.image_url
    and a.source_identity = jsonb_build_object('source_id',v_source,'source_hash',v_hash)
    and a.evidence->>'grade_status'='PASS'
    and a.evidence->>'image_sha256'=a.image_sha256
    and a.image_sha256 ~ '^[0-9a-f]{64}$'
    and a.evidence->>'policy_version'=p_policy_version
    and a.evidence->'brain_snapshot'=p_brain_snapshot
    and a.evidence->>'brief_model'='gpt-6-astra'
    and nullif(a.evidence->>'review_response_id','') is not null
    and coalesce(a.evidence->>'aspect','') in ('','4:5','1:1','1.91:1')
 ) then
  return jsonb_build_object('result','conflict','reason','exact_caption_review_missing');
 end if;
 select * into prior from public.content_calendar where id=v.id;
 if found then
  if prior.gym_id=v.gym_id and prior.account=v.account and prior.post_date=v.post_date
    and prior.slot_index=v.slot_index and prior.format='feed'
    and prior.caption=v.caption and prior.image_url=v.image_url
    and prior.logical_post_id is not distinct from v.logical_post_id and prior.scheduled_at=v.scheduled_at
    and prior.variant_status='active' and prior.status in ('pending','approved','publishing','published') then
   return jsonb_build_object('result','idempotent','id',prior.id);
  end if;
  return jsonb_build_object('result','conflict','reason','id_source_changed');
 end if;
 -- A null slot in this account/day is ambiguous: never fill under legacy rows.
 -- A Story occupant requires exact restoration, which this append-only RPC
 -- deliberately does not infer from its date or URL.
 if exists (select 1 from public.content_calendar c
   where c.gym_id='lasso' and (lower(btrim(coalesce(c.account,''))) in
      (v.account, case v.account when 'instagram' then 'ig' else 'fb' end)
      or nullif(btrim(c.account),'') is null)
    and c.post_date=v.post_date and coalesce(c.status,'') not in ('denied','killed')
    and (c.variant_status is null or c.variant_status='active')
    and (c.slot_index is null or c.slot_index not in (0,1,2) or c.slot_index=v.slot_index)) then
  return jsonb_build_object('result','occupied','reason','target_slot_or_story_occupied');
 end if;
 insert into public.content_calendar
  (id,gym_id,account,post_date,slot_index,format,caption,image_url,status,
   variant_status,logical_post_id,scheduled_at,pillar)
 values (v.id,'lasso',v.account,v.post_date,v.slot_index,'feed',v.caption,v.image_url,
         'pending','active',v.logical_post_id,v.scheduled_at,v.pillar);
 return jsonb_build_object('result','inserted','id',v.id);
end $$;
revoke all on function public.stage_lasso_runway_feed(jsonb,text,jsonb) from public,anon,authenticated;
grant execute on function public.stage_lasso_runway_feed(jsonb,text,jsonb) to service_role;
commit;
