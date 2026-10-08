-- DRAFT: requires the reviewed generated owner authority migration. Default-OFF
-- caller code; this migration grants no media clearance or publication permission.
begin;

create table public.fixer_generated_gap_request_20261007 (
 request_id uuid primary key,
 gym_id text not null,
 local_date date not null,
 account text not null check(account in ('instagram','facebook')),
 format text not null check(format='feed'),
 state text not null default 'pending' check(state in ('pending','bound','complete')),
 calendar_row_id uuid,
 logical_post_id uuid,
 group_key text,
 caption text,
 copy_ref text,
 palette_revision text,
 palette_digest text,
 palette_ref text,
 last_hold text,
 created_at timestamptz not null default clock_timestamp(),
 unique(gym_id,local_date,account,format),
 unique(calendar_row_id)
);
revoke all on public.fixer_generated_gap_request_20261007 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;

-- Queue-only publisher capability. The request conveys no trusted content,
-- depletion, approval, palette or generated source authorization.
create function public.fixer_generated_gap_dispatch_20261007(
 p_request_id uuid,p_gym text,p_local_date date,p_account text,p_format text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.fixer_generated_gap_request_20261007%rowtype;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'service_role','member') then
  raise exception 'publisher dispatch role required' using errcode='42501'; end if;
 if p_request_id is null or p_gym is null or p_gym !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
  or p_local_date is null or p_local_date < current_date-1 or p_local_date > current_date+3
  or p_account is null or p_account not in ('instagram','facebook') or p_format is distinct from 'feed' then
  raise exception 'bounded exact gap request required' using errcode='23514'; end if;
 insert into public.fixer_generated_gap_request_20261007(request_id,gym_id,local_date,account,format)
 values(p_request_id,p_gym,p_local_date,p_account,p_format) on conflict do nothing;
 select * into r from public.fixer_generated_gap_request_20261007
  where gym_id=p_gym and local_date=p_local_date and account=p_account and format=p_format;
 if not found or r.request_id is distinct from p_request_id then
  raise exception 'gap dispatch immutable identity conflict' using errcode='23514'; end if;
 return jsonb_build_object('dispatched',true,'request_id',r.request_id,'gym_id',r.gym_id,
  'local_date',r.local_date,'account',r.account,'format',r.format,'state',r.state);
end; $$;

create function public.fixer_generated_gap_pending_20261007(p_tenants text[],p_limit integer,p_local_windows jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public set timezone='UTC' as $$
declare result jsonb; tenant text; dates jsonb; first_date date; second_date date; caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
  or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 if p_tenants is null or cardinality(p_tenants) not between 1 and 32
  or exists(select 1 from unnest(p_tenants) t where t is null or t !~ '^[a-z0-9][a-z0-9_-]{0,127}$')
  or p_limit is null or p_limit not between 1 and 100 then
  raise exception 'bounded owner discovery required' using errcode='23514'; end if;
 -- The trusted owner computes tomorrow/+2 in each gym's configured timezone.
 -- Filter those exact dates BEFORE LIMIT. Expired pending/bound requests remain
 -- intact for reconciliation, but cannot consume current discovery capacity.
 if jsonb_typeof(p_local_windows) is distinct from 'object' then
  raise exception 'exact local discovery windows required' using errcode='23514'; end if;
 if (select count(*) from jsonb_object_keys(p_local_windows))<>cardinality(p_tenants)
  or exists(select 1 from jsonb_object_keys(p_local_windows) k where not k=any(p_tenants)) then
  raise exception 'exact local discovery windows required' using errcode='23514'; end if;
 foreach tenant in array p_tenants loop
  dates:=p_local_windows->tenant;
  if jsonb_typeof(dates) is distinct from 'array' then
   raise exception 'exact local discovery windows required' using errcode='23514'; end if;
  if jsonb_array_length(dates)<>2 or jsonb_typeof(dates->0) is distinct from 'string'
   or jsonb_typeof(dates->1) is distinct from 'string'
   or (dates->>0 ~ '^\d{4}-\d{2}-\d{2}$') is distinct from true
   or (dates->>1 ~ '^\d{4}-\d{2}-\d{2}$') is distinct from true then
   raise exception 'exact local discovery windows required' using errcode='23514'; end if;
  first_date:=(dates->>0)::date; second_date:=(dates->>1)::date;
  -- A gym's local today can differ from UTC by one day. Even the trusted
  -- owner cannot turn discovery into an unbounded historical replay.
  if first_date not between current_date and current_date+2 or second_date<>first_date+1 then
   raise exception 'bounded local discovery dates required' using errcode='23514'; end if;
 end loop;
 select coalesce(jsonb_agg(to_jsonb(q) order by q.local_date,q.request_id),'[]'::jsonb) into result
 from (select * from public.fixer_generated_gap_request_20261007 r
  where r.gym_id=any(p_tenants) and r.state in ('pending','bound')
   and (p_local_windows->r.gym_id) ? r.local_date::text
  order by r.local_date,r.request_id limit p_limit) q;
 return result;
end; $$;

-- Graph -> census -> slot -> queue -> calendar ordering. No lock crosses any
-- provider/storage read. The dedicated owner supplies already independently
-- loaded approved copy/palette refs; publisher request fields cannot supply them.
create function public.fixer_generated_gap_bind_20261007(
 p_request_id uuid,p_row_id uuid,p_logical_post_id uuid,p_group_key text,
 p_caption text,p_copy_ref text,p_palette_revision text,p_palette_digest text,p_palette_ref text)
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
  or p_copy_ref is null or p_copy_ref !~ '^client-source:sha256:[0-9a-f]{64}$'
  or p_palette_revision is null or p_palette_revision !~ '^sha256:[0-9a-f]{64}$'
  or p_palette_digest is null or p_palette_digest !~ '^[0-9a-f]{64}$'
  or p_palette_ref is null or p_palette_ref !~ '^brand-colors:sha256:[0-9a-f]{64}$' then
  raise exception 'approved current generated content refs required' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into q from public.fixer_generated_gap_request_20261007 where request_id=p_request_id;
 if not found or q.local_date not between current_date-1 and current_date+3 then
  raise exception 'current bounded gap request required' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtextextended('generated-gap|'||q.gym_id||'|'||q.local_date::text||'|'||q.account||'|feed',0));
 select * into q from public.fixer_generated_gap_request_20261007 where request_id=p_request_id for update;
 if q.calendar_row_id is not null then
  if q.calendar_row_id is distinct from p_row_id or q.logical_post_id is distinct from p_logical_post_id
   or q.group_key is distinct from p_group_key or q.caption is distinct from p_caption
   or q.copy_ref is distinct from p_copy_ref or q.palette_revision is distinct from p_palette_revision
   or q.palette_digest is distinct from p_palette_digest or q.palette_ref is distinct from p_palette_ref then
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
  palette_revision=p_palette_revision,palette_digest=p_palette_digest,palette_ref=p_palette_ref,last_hold=null
  where request_id=p_request_id;
 return jsonb_build_object('bound',true,'replayed',false,'calendar_row_id',p_row_id,'logical_post_id',p_logical_post_id,
  'gym_id',q.gym_id,'local_date',q.local_date,'account',q.account,'format','feed');
end; $$;

create function public.fixer_generated_gap_record_20261007(p_request_id uuid,p_row_id uuid,p_reserved boolean,p_hold text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.fixer_generated_gap_request_20261007%rowtype; r public.content_calendar%rowtype;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
  or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 select * into q from public.fixer_generated_gap_request_20261007 where request_id=p_request_id for update;
 if not found or q.calendar_row_id is distinct from p_row_id or p_reserved is null then
  raise exception 'exact owner gap outcome required' using errcode='23514'; end if;
 if p_reserved then
  select * into r from public.content_calendar where id=p_row_id;
  if not found or not exists(select 1 from public.fixer_generated_reservation_20261007 g
   where g.candidate_json->>'job_id'=replace(r.source_media_asset_id,'generated-astra:','')
    and g.candidate_json->>'gym_id'=q.gym_id and g.candidate_json->>'local_date'=q.local_date::text
    and g.candidate_json->>'logical_post_id'=q.logical_post_id::text and g.group_key=q.group_key
    and g.candidate_json->>'original_url'=r.image_url and r.source_media_url=r.image_url) then
   raise exception 'committed generated reservation required' using errcode='23514'; end if;
  update public.fixer_generated_gap_request_20261007 set state='complete',last_hold=null where request_id=p_request_id;
 else
  if p_hold is null or p_hold !~ '^generated_[a-z_]{1,100}$' then
   raise exception 'static bounded owner hold required' using errcode='23514'; end if;
  update public.fixer_generated_gap_request_20261007 set last_hold=p_hold where request_id=p_request_id and state<>'complete';
 end if;
 return true;
end; $$;

-- Competing ordinary writers must respect an existing active gap placeholder.
-- Shared graph/census statement locks already precede calendar row triggers.
-- Never wait for a slot while owning other row locks; retry the DB mutation.
create function public.fixer_generated_gap_slot_guard_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare platform text:=case lower(btrim(coalesce(new.account,''))) when '' then 'instagram' when 'ig' then 'instagram' when 'facebook_page' then 'facebook' when 'fb' then 'facebook' else lower(btrim(new.account)) end;
begin
 if lower(btrim(coalesce(new.format,'feed')))<>'feed' or new.status in ('denied','killed','deleted')
  or coalesce(new.variant_status,'active')<>'active' then return new; end if;
 if not pg_try_advisory_xact_lock(hashtextextended('generated-gap|'||new.gym_id||'|'||new.post_date::text||'|'||platform||'|feed',0)) then
  raise exception 'generated gap slot busy; retry database mutation only' using errcode='40001'; end if;
 if exists(select 1 from public.fixer_generated_gap_request_20261007 q join public.content_calendar c on c.id=q.calendar_row_id
  where q.gym_id=new.gym_id and q.local_date=new.post_date and q.account=platform and q.format='feed'
   and c.id<>new.id and coalesce(c.variant_status,'active')='active' and coalesce(c.status,'pending') not in ('denied','killed','deleted')) then
  raise exception 'active generated gap slot already bound' using errcode='23514'; end if;
 return new;
end; $$;
create trigger generated_gap_slot_guard before insert or update on public.content_calendar
 for each row execute function public.fixer_generated_gap_slot_guard_20261007();

revoke all on function public.fixer_generated_gap_dispatch_20261007(uuid,text,date,text,text),
 public.fixer_generated_gap_pending_20261007(text[],integer,jsonb),
 public.fixer_generated_gap_bind_20261007(uuid,uuid,uuid,text,text,text,text,text,text),
 public.fixer_generated_gap_record_20261007(uuid,uuid,boolean,text),
 public.fixer_generated_gap_slot_guard_20261007() from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_generated_gap_dispatch_20261007(uuid,text,date,text,text) to service_role;
grant execute on function public.fixer_generated_gap_pending_20261007(text[],integer,jsonb),
 public.fixer_generated_gap_bind_20261007(uuid,uuid,uuid,text,text,text,text,text,text),
 public.fixer_generated_gap_record_20261007(uuid,uuid,boolean,text) to fixer_forward_media_owner_20261006;
commit;
