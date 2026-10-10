-- DRAFT additive overlay. Install AFTER the accepted local census producer.
-- No production apply, activation, publication or automatic pending cleanup.
-- DB graph -> census -> per-gym state. Commit begin BEFORE local flock/write;
-- release local flock AFTER fsync/SQLite commit and BEFORE complete RPC.
-- A failed/lost begin ack forbids writes until exact receipt readback. A failed
-- complete ack leaves a durable HOLD until exact readback confirms completion.
-- Completion is a trusted writer attestation, not proof of local disk by SQL.
-- Never complete an abandoned receipt until its writer is fenced and its exact
-- durable outcome independently reconciled. No age-based clean transition.
begin;
do $$ declare t text; begin
 if to_regprocedure('public.fixer_generated_local_census_snapshot_20261008(uuid)') is null
  or to_regprocedure('public.fixer_local_census_latest_private_20261008(text)') is null then
  raise exception 'accepted local census producer must precede inventory protocol' using errcode='55000'; end if;
 foreach t in array array['media_source','media_asset'] loop
  if not exists(select 1 from pg_trigger where tgrelid=('public.'||t)::regclass
   and tgname='generated_inventory_lock' and tgenabled in ('O','A') and tgtype=62
   and tgfoid=to_regprocedure('public.fixer_generated_inventory_lock_20261007()')) then
   raise exception 'enabled inventory graph/census statement guard required for %',t using errcode='55000'; end if;
 end loop;
 if exists(select 1 from pg_roles where rolname='fixer_inventory_mutator_20261008') then
  raise exception 'inventory mutator role already exists; review installation identity' using errcode='55000'; end if;
end $$;
-- Pin accepted producer definitions before replacing them. Future composition
-- drift requires a fresh review rather than silently erasing inherited checks.
do $$ declare guard record; body_hash text; begin
 for guard in select * from (values
 ('public.fixer_generated_local_census_snapshot_20261008(uuid)','447ad36c6c991f87cf4ec4367a8ea83e'),
 ('public.fixer_still_inventory_record_20261007(uuid,uuid,text,boolean,integer,text,uuid,jsonb,timestamptz)','d1b580ca22417dc90723e352b6f2ae07'),
 ('public.fixer_generated_local_census_receipt_20261008(uuid,uuid)','0cfd2ba20a5df2de8ca3f8e40d30fac1'),
 ('public.fixer_local_census_latest_private_20261008(text)','eaf1e28717e1db1515715abdb4e40534'),
 ('public.fixer_generated_local_census_authority_20261008(text,text)','87ff7ad84f5cb72ab44374901d9d5fd5')
,
 ('public.fixer_still_owner_lock_20261007()','cda01b18e78493ddb2eb9582e8f16794'),
 ('public.fixer_still_cutover_control_20261007(boolean,text)','89ebb2b4f4d1303080e89ebac032a33a')
 ) as expected(signature,body_md5) loop
  select md5(prosrc) into body_hash from pg_proc where oid=to_regprocedure(guard.signature);
  if body_hash is distinct from guard.body_md5 then
   raise exception 'accepted census authority drift at %',guard.signature using errcode='55000'; end if;
 end loop;
end $$;
create role fixer_inventory_mutator_20261008 nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
grant usage on schema public to fixer_inventory_mutator_20261008;
create table public.fixer_inventory_protocol_control_20261008 (
 singleton boolean primary key default true check(singleton),
 enabled boolean not null default false,
 all_writers_verified_ref text,
 check(not enabled or (all_writers_verified_ref is not null and length(btrim(all_writers_verified_ref)) between 16 and 1000))
);
insert into public.fixer_inventory_protocol_control_20261008(singleton) values(true);
create table public.fixer_inventory_generation_20261008 (
 gym_id text primary key check(gym_id ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
 generation bigint not null default 0 check(generation>=0)
);
create table public.fixer_inventory_mutation_20261008 (
 mutation_id uuid primary key,
 gym_id text not null check(gym_id ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
 epoch_id uuid not null,
 generation bigint not null check(generation>0),
 kind text not null check(kind ~ '^[a-z][a-z0-9_]{0,63}$'),
 request_digest text not null check(request_digest ~ '^sha256:[0-9a-f]{64}$'),
 begun_at timestamptz not null default clock_timestamp(),
 unique(gym_id,generation)
);
create table public.fixer_inventory_mutation_completion_20261008 (
 mutation_id uuid primary key references public.fixer_inventory_mutation_20261008(mutation_id),
 result_digest text not null check(result_digest ~ '^sha256:[0-9a-f]{64}$'),
 completed_at timestamptz not null default clock_timestamp()
);
create index fixer_inventory_pending_gym_20261008 on public.fixer_inventory_mutation_20261008(gym_id,epoch_id);
alter table public.fixer_still_inventory_20261007 add column inventory_generation bigint check(inventory_generation>=0);

create function public.fixer_inventory_protocol_lock_private_20261008()
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'inventory protocol requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
end $$;
-- Authenticate BOTH the original LOGIN and the effective caller. SET ROLE must
-- never hide mixed/transitive memberships or a superuser runtime session.
create function public.fixer_inventory_runtime_caller_private_20261008(p_lane text)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare effective text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
 principal text; lane text; login_lane text; m boolean; o boolean; forbidden text;
begin
 if p_lane is null or p_lane not in ('either','owner')
  or not exists(select 1 from pg_roles where rolname=session_user and rolcanlogin and not rolsuper) then
  raise exception 'isolated authenticated inventory login required' using errcode='42501'; end if;
 foreach principal in array array[session_user::text,effective] loop
  if exists(select 1 from pg_roles where rolname=principal and rolsuper) then
   raise exception 'superuser inventory runtime forbidden' using errcode='42501'; end if;
  m:=pg_has_role(principal,'fixer_inventory_mutator_20261008','member');
  o:=pg_has_role(principal,'fixer_forward_media_owner_20261006','member');
  if (not m and not o) or (m and o) or (p_lane='owner' and not o) then
   raise exception 'isolated single inventory role lane required' using errcode='42501'; end if;
  foreach forbidden in array array['service_role','anon','authenticated','fixer_forward_media_attester_20261006',
    'generated_authority_publisher_20261007','generated_send_reconciler_20261007'] loop
   if pg_has_role(principal,forbidden,'member') then
    raise exception 'mixed inventory runtime memberships forbidden' using errcode='42501'; end if;
  end loop;
  lane:=case when m then 'mutator' else 'owner' end;
  if login_lane is null then login_lane:=lane;
  elsif lane is distinct from login_lane then
   raise exception 'authenticated and effective inventory lane mismatch' using errcode='42501'; end if;
 end loop;
end $$;
create function public.fixer_inventory_mutator_check_private_20261008()
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform public.fixer_inventory_runtime_caller_private_20261008('either');
end $$;
-- Preserve inherited owner entry semantics while authenticating the session.
-- Owner snapshots take this entry BEFORE observing any calendar/state rows.
create or replace function public.fixer_still_owner_lock_20261007()
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform public.fixer_inventory_runtime_caller_private_20261008('owner');
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'still authority requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
end $$;
-- Control entry is nonblocking even if an inherited caller already owns shared
-- graph authority or rows. The table guard below covers direct DML as well.
create or replace function public.fixer_still_cutover_control_20261007(p_enabled boolean,p_ref text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype;
begin
 perform public.fixer_inventory_runtime_caller_private_20261008('owner');
 if current_setting('transaction_isolation')<>'read committed'
  or not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0))
  or not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0)) then
  raise exception 'still cutover authority busy; retry database transaction only' using errcode='40001'; end if;
 if p_enabled is null or nullif(btrim(p_ref),'') is null then
  raise exception 'explicit cutover ruling required' using errcode='23514'; end if;
 update public.fixer_still_cutover_20261007 set enabled=p_enabled,
  epoch_id=coalesce(epoch_id,gen_random_uuid()),cutover_at=coalesce(cutover_at,clock_timestamp()),activation_ref=p_ref
 where singleton returning * into s;
 return to_jsonb(s);
end $$;
create function public.fixer_inventory_bump_private_20261008(p_gym text)
returns bigint language plpgsql security definer set search_path=pg_catalog,public as $$
declare g bigint;
begin
 if p_gym is null or p_gym !~ '^[a-z0-9][a-z0-9_-]{0,127}$' then
  raise exception 'exact inventory gym required' using errcode='23514'; end if;
 insert into public.fixer_inventory_generation_20261008(gym_id,generation) values(p_gym,1)
 on conflict(gym_id) do update set generation=fixer_inventory_generation_20261008.generation+1 returning generation into g;
 return g;
end $$;
create function public.fixer_inventory_status_private_20261008(p_gym text)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$
 select jsonb_build_object('inventory_generation',coalesce((select generation from public.fixer_inventory_generation_20261008 where gym_id=p_gym),0),
 'pending_mutation_count',(select count(*) from public.fixer_inventory_mutation_20261008 m
   where m.gym_id=p_gym and not exists(select 1 from public.fixer_inventory_mutation_completion_20261008 c where c.mutation_id=m.mutation_id)),
 'inventory_protocol_enabled',coalesce((select enabled from public.fixer_inventory_protocol_control_20261008 where singleton),false));
$$;
create function public.fixer_inventory_receipt_private_20261008(p_id uuid,p_gym text,p_epoch uuid,p_digest text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare m public.fixer_inventory_mutation_20261008%rowtype; c public.fixer_inventory_mutation_completion_20261008%rowtype;
begin
 select * into m from public.fixer_inventory_mutation_20261008 where mutation_id=p_id;
 if not found or m.gym_id is distinct from p_gym or m.epoch_id is distinct from p_epoch
  or m.request_digest is distinct from p_digest then
  raise exception 'exact immutable inventory mutation required' using errcode='23514'; end if;
 select * into c from public.fixer_inventory_mutation_completion_20261008 where mutation_id=p_id;
 return to_jsonb(m)||jsonb_build_object('state',case when c.mutation_id is null then 'pending' else 'complete' end,
  'result_digest',c.result_digest,'completed_at',c.completed_at);
end $$;
create function public.fixer_inventory_mutation_begin_20261008(p_id uuid,p_gym text,p_epoch uuid,p_kind text,p_digest text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype; m public.fixer_inventory_mutation_20261008%rowtype; g bigint;
begin
 perform public.fixer_inventory_mutator_check_private_20261008();
 perform public.fixer_inventory_protocol_lock_private_20261008();
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if p_id is null or p_gym is null or p_gym !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
  or p_epoch is null or not coalesce(s.enabled,false) or s.epoch_id is distinct from p_epoch
  or (p_kind ~ '^[a-z][a-z0-9_]{0,63}$') is distinct from true
  or (p_digest ~ '^sha256:[0-9a-f]{64}$') is distinct from true then
  raise exception 'exact active epoch mutation identity required' using errcode='23514'; end if;
 select * into m from public.fixer_inventory_mutation_20261008 where mutation_id=p_id;
 if found then
  if m.kind is distinct from p_kind then raise exception 'inventory mutation kind conflict' using errcode='23514'; end if;
  return public.fixer_inventory_receipt_private_20261008(p_id,p_gym,p_epoch,p_digest);
 end if;
 g:=public.fixer_inventory_bump_private_20261008(p_gym);
 insert into public.fixer_inventory_mutation_20261008(mutation_id,gym_id,epoch_id,generation,kind,request_digest)
 values(p_id,p_gym,p_epoch,g,p_kind,p_digest);
 return public.fixer_inventory_receipt_private_20261008(p_id,p_gym,p_epoch,p_digest);
end $$;
create function public.fixer_inventory_mutation_complete_20261008(p_id uuid,p_gym text,p_epoch uuid,p_digest text,p_result_digest text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype; r jsonb; c public.fixer_inventory_mutation_completion_20261008%rowtype;
begin
 perform public.fixer_inventory_mutator_check_private_20261008();
 perform public.fixer_inventory_protocol_lock_private_20261008();
 r:=public.fixer_inventory_receipt_private_20261008(p_id,p_gym,p_epoch,p_digest);
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if not coalesce(s.enabled,false) or s.epoch_id is distinct from p_epoch
  or (p_result_digest ~ '^sha256:[0-9a-f]{64}$') is distinct from true then
  raise exception 'exact current durable mutation outcome required' using errcode='23514'; end if;
 select * into c from public.fixer_inventory_mutation_completion_20261008 where mutation_id=p_id;
 if found then
  if c.result_digest is distinct from p_result_digest then raise exception 'immutable mutation completion conflict' using errcode='23514'; end if;
 else
  insert into public.fixer_inventory_mutation_completion_20261008(mutation_id,result_digest) values(p_id,p_result_digest);
  perform public.fixer_inventory_bump_private_20261008(p_gym);
 end if;
 return public.fixer_inventory_receipt_private_20261008(p_id,p_gym,p_epoch,p_digest);
end $$;
create function public.fixer_inventory_mutation_receipt_20261008(p_id uuid,p_gym text,p_epoch uuid,p_digest text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform public.fixer_inventory_mutator_check_private_20261008();
 perform public.fixer_inventory_protocol_lock_private_20261008();
 return public.fixer_inventory_receipt_private_20261008(p_id,p_gym,p_epoch,p_digest);
end $$;

-- Protect authority state even from direct administrator writes. Existing DB
-- source writers own shared graph + exclusive census; never upgrade that graph
-- lock here or wait after owning rows. Consumers own graph/census first.
create function public.fixer_inventory_state_write_lock_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if tg_op in ('DELETE','TRUNCATE') then
  raise exception 'inventory authority state cannot be removed' using errcode='23514'; end if;
 if current_setting('transaction_isolation')<>'read committed'
  or not pg_try_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0))
  or not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0)) then
  raise exception 'inventory authority state busy; retry database transaction only' using errcode='40001'; end if;
 return null;
end $$;
create trigger inventory_state_entry before insert or update or delete or truncate on public.fixer_inventory_generation_20261008
 for each statement execute function public.fixer_inventory_state_write_lock_20261008();
create trigger inventory_state_entry before insert or update or delete or truncate on public.fixer_inventory_protocol_control_20261008
 for each statement execute function public.fixer_inventory_state_write_lock_20261008();
create trigger inventory_state_entry before insert or update or delete or truncate on public.fixer_still_cutover_20261007
 for each statement execute function public.fixer_inventory_state_write_lock_20261008();
-- A protocol release ruling changes authority. Re-enabling cannot resurrect an
-- earlier zero observed while a previous coverage ruling was in force.
create function public.fixer_inventory_control_invalidate_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare gym text;
begin
 for gym in select gym_id from public.fixer_inventory_generation_20261008
  union select gym_id from public.fixer_still_inventory_20261007
  union select gym_id from public.media_source
  union select gym_id from public.media_asset
  order by gym_id loop
  perform public.fixer_inventory_bump_private_20261008(gym);
 end loop;
 return null;
end $$;
create trigger inventory_control_invalidate after update on public.fixer_inventory_protocol_control_20261008
 for each row execute function public.fixer_inventory_control_invalidate_20261008();
create trigger inventory_cutover_invalidate after insert or update on public.fixer_still_cutover_20261007
 for each row execute function public.fixer_inventory_control_invalidate_20261008();

-- Row triggers run AFTER the existing statement graph/census entry, and never
-- obtain local locks. A DB rollback also rolls back every generation advance.
-- Include old/new tenant AND source/asset linked tenant, even inconsistent rows.
create function public.fixer_inventory_database_write_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare gyms text[]:='{}'; ids text[]:='{}'; gym text; row_json jsonb;
begin
 if tg_op<>'INSERT' then row_json:=to_jsonb(old); gyms:=array_append(gyms,row_json->>'gym_id');
  ids:=array_append(ids,case when tg_table_name='media_source' then row_json->>'id' else row_json->>'source_id' end); end if;
 if tg_op<>'DELETE' then row_json:=to_jsonb(new); gyms:=array_append(gyms,row_json->>'gym_id');
  ids:=array_append(ids,case when tg_table_name='media_source' then row_json->>'id' else row_json->>'source_id' end); end if;
 if tg_table_name='media_source' then
  gyms:=gyms||array(select distinct a.gym_id from public.media_asset a where a.source_id=any(ids));
 else
  gyms:=gyms||array(select distinct s.gym_id from public.media_source s where s.id=any(ids));
 end if;
 for gym in select distinct x from unnest(gyms) x where x is not null order by x loop
  perform public.fixer_inventory_bump_private_20261008(gym);
 end loop;
 return null;
end $$;
create trigger inventory_generation_20261008 after insert or update or delete on public.media_source
 for each row execute function public.fixer_inventory_database_write_20261008();
create trigger inventory_generation_20261008 after insert or update or delete on public.media_asset
 for each row execute function public.fixer_inventory_database_write_20261008();
create function public.fixer_inventory_generation_guard_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if tg_op<>'UPDATE' then raise exception 'inventory generations cannot be removed' using errcode='23514'; end if;
 if new.gym_id is distinct from old.gym_id or new.generation<=old.generation then
  raise exception 'inventory generation must strictly advance' using errcode='23514'; end if;
 return new;
end $$;
create trigger inventory_generation_monotonic before update or delete on public.fixer_inventory_generation_20261008
 for each row execute function public.fixer_inventory_generation_guard_20261008();
create trigger inventory_generation_no_truncate before truncate on public.fixer_inventory_generation_20261008
 for each statement execute function public.fixer_inventory_generation_guard_20261008();
do $$ declare t text; begin
 foreach t in array array['fixer_inventory_mutation_20261008','fixer_inventory_mutation_completion_20261008'] loop
  execute format('create trigger immutable_row before update or delete on public.%I for each row execute function public.fixer_forward_media_immutable_20261006()',t);
  execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.fixer_forward_media_immutable_20261006()',t);
 end loop;
end $$;

create or replace function public.fixer_generated_local_census_snapshot_20261008(p_row uuid)
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
  'asset_revision','sha256:'||encode(sha256(convert_to(assets::text,'UTF8')),'hex'))
  ||public.fixer_inventory_status_private_20261008(r.gym_id);
end $$;

create or replace function public.fixer_still_inventory_record_20261007(
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
  -- Complete observations require no pending writes. Complete ZERO additionally
  -- requires operator-verified coverage of ALL local and DB writer paths.
  or (p_complete and ((current_snapshot->>'pending_mutation_count')::bigint<>0
    or (p_available=0 and current_snapshot->'inventory_protocol_enabled' is distinct from 'true'::jsonb)
    or current_snapshot#>'{snapshot,photo_inventory_complete}' is distinct from 'true'::jsonb
    or current_snapshot#>'{snapshot,history_complete}' is distinct from 'true'::jsonb))
  or p_observed_at is null or p_observed_at>clock_timestamp()
  or p_observed_at<clock_timestamp()-interval '5 minutes'
  or (p_ref ~ '^local-census:sha256:[0-9a-f]{64}$') is distinct from true then
  raise exception 'complete fresh epoch-bound census required' using errcode='23514'; end if;
 insert into public.fixer_still_inventory_20261007
  (receipt_id,epoch_id,gym_id,inventory_revision,local_complete,local_available,evidence_ref,observed_at,inventory_generation)
 values(p_id,p_epoch,current_snapshot#>>'{snapshot,gym_id}',p_revision,p_complete,p_available,p_ref,p_observed_at,(current_snapshot->>'inventory_generation')::bigint)
 on conflict do nothing;
 select * into old from public.fixer_still_inventory_20261007 where receipt_id=p_id;
 if old.epoch_id is distinct from p_epoch or old.gym_id is distinct from current_snapshot#>>'{snapshot,gym_id}'
  or old.inventory_revision is distinct from p_revision or old.local_complete is distinct from p_complete
  or old.local_available is distinct from p_available or old.evidence_ref is distinct from p_ref
  or old.observed_at is distinct from p_observed_at
  or old.inventory_generation is distinct from (current_snapshot->>'inventory_generation')::bigint then
  raise exception 'inventory observation immutable conflict' using errcode='23514'; end if;
 return p_id;
end $$;

create or replace function public.fixer_generated_local_census_receipt_20261008(p_id uuid,p_row uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare current_snapshot jsonb; i public.fixer_still_inventory_20261007%rowtype;
begin
 current_snapshot:=public.fixer_generated_local_census_snapshot_20261008(p_row);
 select * into i from public.fixer_still_inventory_20261007 where receipt_id=p_id;
 if not found or current_snapshot->'enabled' is distinct from 'true'::jsonb
  or i.epoch_id::text is distinct from current_snapshot->>'epoch_id'
  or i.gym_id is distinct from current_snapshot#>>'{snapshot,gym_id}'
  or i.inventory_revision is distinct from current_snapshot#>>'{snapshot,inventory_revision}'
  or i.inventory_generation is distinct from (current_snapshot->>'inventory_generation')::bigint
  or (current_snapshot->>'pending_mutation_count')::bigint<>0 then
  raise exception 'current epoch census receipt required' using errcode='23514'; end if;
 return to_jsonb(i);
end $$;

-- Newest first; a mismatched/legacy census NEVER revives an older zero receipt.
-- Existing runtime and still consumers already route through this resolver.
-- The generated reservation selector is adapted in place below as well.
create or replace function public.fixer_local_census_latest_private_20261008(p_gym text)
returns public.fixer_still_inventory_20261007
language plpgsql security definer set search_path=pg_catalog,public as $$
declare i public.fixer_still_inventory_20261007%rowtype; status jsonb;
begin
 -- A consumer may already own calendar rows. Never wait while upgrading
 -- shared graph authority; contention means retry the DB transaction only.
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'inventory consumer requires read committed' using errcode='25000'; end if;
 if not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0))
  or not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0)) then
  raise exception 'inventory consumer authority busy; retry database transaction only' using errcode='40001'; end if;
 select c.* into i from public.fixer_still_inventory_20261007 c
 join public.fixer_still_cutover_20261007 s on s.singleton
 where s.enabled and s.epoch_id is not null and c.epoch_id=s.epoch_id and c.gym_id=p_gym
 order by c.observed_at desc,c.receipt_id desc limit 1;
 status:=public.fixer_inventory_status_private_20261008(p_gym);
 if i.receipt_id is null or i.inventory_generation is distinct from (status->>'inventory_generation')::bigint
  or (status->>'pending_mutation_count')::bigint<>0
  or (i.local_complete and i.local_available=0 and status->'inventory_protocol_enabled' is distinct from 'true'::jsonb) then
  return null;
 end if;
 return i;
end $$;
create or replace function public.fixer_generated_local_census_authority_20261008(p_gym text,p_revision text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype;
 i public.fixer_still_inventory_20261007%rowtype; status jsonb;
begin
 perform public.fixer_still_owner_lock_20261007();
 if p_gym is null or p_gym !~ '^[a-z0-9][a-z0-9_-]{0,127}$' or nullif(btrim(p_revision),'') is null then
  raise exception 'exact current inventory revision required' using errcode='23514'; end if;
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 status:=public.fixer_inventory_status_private_20261008(p_gym);
 if not coalesce(s.enabled,false) or s.epoch_id is null then
  return jsonb_build_object('enabled',false,'receipt_id',null)||status; end if;
 i:=public.fixer_local_census_latest_private_20261008(p_gym);
 if i.receipt_id is null or i.inventory_revision is distinct from p_revision then
  return jsonb_build_object('enabled',true,'receipt_id',null,'epoch_id',s.epoch_id)||status; end if;
 return jsonb_build_object('enabled',true,'receipt_id',i.receipt_id,'epoch_id',s.epoch_id,
  'local_complete',i.local_complete,'local_available',i.local_available,'observed_at',i.observed_at)||status;
end $$;

-- Raw snapshot is PRIVATE SQL plumbing. It remains unchanged for inherited
-- SECURITY DEFINER reserve/final/publisher validators, whose function owner
-- retains implicit EXECUTE. Remove every non-owner ACL (including intermediates
-- that runtime logins could inherit) and PUBLIC; no direct runtime raw RPC.
do $$ declare f record; grantee_role record; runtime_role text; begin
 select p.oid,p.proowner into strict f from pg_proc p
 where p.oid=to_regprocedure('public.fixer_generated_snapshot_20261007(uuid)');
 for grantee_role in select distinct a.grantee from pg_proc p,
  lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
  where p.oid=f.oid and a.grantee<>f.proowner loop
  if grantee_role.grantee=0 then
   revoke all on function public.fixer_generated_snapshot_20261007(uuid) from public cascade;
  else
   execute format('revoke all on function public.fixer_generated_snapshot_20261007(uuid) from %I cascade',
    pg_get_userbyid(grantee_role.grantee));
  end if;
 end loop;
 foreach runtime_role in array array['anon','authenticated','service_role','fixer_forward_media_owner_20261006',
  'fixer_forward_media_attester_20261006','fixer_inventory_mutator_20261008','fixer_forward_media_photo_auditor_20261007',
  'generated_authority_owner_20261007','generated_authority_publisher_20261007','generated_send_reconciler_20261007'] loop
  if has_function_privilege(runtime_role,f.oid,'EXECUTE') then
   raise exception 'inherited raw inventory snapshot access remains for %',runtime_role using errcode='55000'; end if;
 end loop;
end $$;
create function public.fixer_generated_snapshot_guarded_20261008(p_row uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare s public.fixer_still_cutover_20261007%rowtype; r public.content_calendar%rowtype;
 raw jsonb; status jsonb; i public.fixer_still_inventory_20261007%rowtype; current_zero boolean;
begin
 -- Authenticate session and effective owner lane; graph -> census -> row.
 perform public.fixer_still_owner_lock_20261007();
 select * into r from public.content_calendar where id=p_row for share;
 if not found then raise exception 'exact generated snapshot row required' using errcode='23514'; end if;
 raw:=public.fixer_generated_snapshot_20261007(p_row);
 status:=public.fixer_inventory_status_private_20261008(r.gym_id);
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 i:=public.fixer_local_census_latest_private_20261008(r.gym_id);
 current_zero:=coalesce(s.enabled and s.epoch_id is not null
  and status->'inventory_protocol_enabled'='true'::jsonb
  and (status->>'pending_mutation_count')::bigint=0
  and i.receipt_id is not null and i.epoch_id=s.epoch_id
  and i.inventory_revision=raw->>'inventory_revision'
  and i.inventory_generation=(status->>'inventory_generation')::bigint
  and i.local_complete and i.local_available=0
  and raw->'photo_inventory_complete'='true'::jsonb
  and raw->'eligible_photo_count'='0'::jsonb and raw->'history_complete'='true'::jsonb
  and i.observed_at>=clock_timestamp()-interval '10 minutes' and i.observed_at<=clock_timestamp(),false);
 -- Every original raw field/value is preserved. DB photo completeness does
 -- NOT certify local depletion. Only local_census_current grants that fact.
 return raw||status||jsonb_build_object('enabled',coalesce(s.enabled,false),'epoch_id',s.epoch_id,
  'local_census_current',current_zero,'local_census_receipt_id',case when current_zero then i.receipt_id else null end);
end $$;
revoke all on function public.fixer_generated_snapshot_guarded_20261008(uuid)
 from public,anon,authenticated,service_role,fixer_inventory_mutator_20261008,fixer_forward_media_attester_20261006,
 fixer_forward_media_owner_20261006,fixer_forward_media_photo_auditor_20261007,
 generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007;
grant execute on function public.fixer_generated_snapshot_guarded_20261008(uuid) to fixer_forward_media_owner_20261006;

-- Preserve the assembled generated bundle function OID, ACL, and every source,
-- palette, approval, historical/prospective and transport guard. Replace ONLY
-- its one frozen newest-census selector. Drift aborts the whole installation.
do $adapt$
declare f record; marker text:=$marker$  select * into i from public.fixer_still_inventory_20261007 ci
   where ci.epoch_id=s.epoch_id and ci.gym_id=c->>'gym_id'
   order by ci.observed_at desc,ci.receipt_id desc limit 1;$marker$;
begin
 select p.oid,p.prosrc into strict f from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and p.proname='fixer_reserve_generated_bundle_20261007';
 if (length(f.prosrc)-length(replace(f.prosrc,marker,'')))/length(marker)<>1 then
  raise exception 'exact assembled generated reservation census selector required' using errcode='55000'; end if;
 execute replace(pg_get_functiondef(f.oid),f.prosrc,replace(f.prosrc,marker,
  $replacement$  i:=public.fixer_local_census_latest_private_20261008(c->>'gym_id');$replacement$));
end $adapt$;

-- New functions are private by default; only the bounded RPC capability is
-- exposed. No DB media table, control table, receipt DML or generation DML grant.
do $$ declare t text; f regprocedure; begin
 foreach t in array array['fixer_inventory_protocol_control_20261008','fixer_inventory_generation_20261008',
 'fixer_inventory_mutation_20261008','fixer_inventory_mutation_completion_20261008'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on table public.%I from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_inventory_mutator_20261008,fixer_forward_media_photo_auditor_20261007,generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007',t);
 end loop;
 for f in select p.oid::regprocedure from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname like 'fixer_inventory_%_20261008' loop
  execute format('revoke all on function %s from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_inventory_mutator_20261008,fixer_forward_media_photo_auditor_20261007,generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007',f);
 end loop;
end $$;
grant execute on function public.fixer_inventory_mutation_begin_20261008(uuid,text,uuid,text,text),
 public.fixer_inventory_mutation_complete_20261008(uuid,text,uuid,text,text),
 public.fixer_inventory_mutation_receipt_20261008(uuid,text,uuid,text)
 to fixer_inventory_mutator_20261008,fixer_forward_media_owner_20261006;
commit;
