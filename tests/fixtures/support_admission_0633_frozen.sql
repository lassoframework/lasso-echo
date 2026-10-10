-- DRAFT ONLY. Do not activate under deploy-migrate. Initial state is paused, allowlist empty.
-- Admission, pause, resume, finish and status serialize on the SAME control row for each named lane.
-- The two support-effect lanes (support-resolution-send, support-ticket-close) additionally bind each
-- admitted invocation durably to support_tickets.id, the observed support_tickets.request_version and
-- support_messages.id, validated inside the same serializing transaction. Legacy unbound acquisition
-- for those lanes fails closed.
begin;
create table public.support_admission_control (
 lane text primary key check (lane in ('ranger-lane','support-resolution-send','support-ticket-close')),
 generation bigint not null default 0 check (generation >= 0),
 paused boolean not null default true,
 operation_id uuid not null,
 allowed_builds jsonb not null default '[]'::jsonb check (jsonb_typeof(allowed_builds) = 'array')
);
create table public.support_admission_operations (
 operation_id uuid primary key,
 lane text not null references public.support_admission_control(lane),
 generation bigint not null,
 unique(lane,generation),
 paused_at timestamptz not null default clock_timestamp()
);
create table public.support_admission_invocations (
 invocation_id uuid primary key,
 lane text not null references public.support_admission_control(lane),
 generation bigint not null,
 deployment text not null,
 build text not null,
 started_at timestamptz not null default clock_timestamp(),
 ended_at timestamptz,
 outcome text not null default 'running' check (outcome in ('running', 'completed', 'unknown')),
 unresolved boolean not null default true,
 check ((outcome = 'running' and ended_at is null and unresolved) or
        (outcome = 'completed' and ended_at is not null and not unresolved) or
        (outcome = 'unknown' and ended_at is not null and unresolved))
);
create index support_admission_unresolved on public.support_admission_invocations(lane) where unresolved;
create index support_admission_invocations_inventory on public.support_admission_invocations(lane, started_at, invocation_id);
-- Immutable effect binding: one message binds at most one admission per lane, forever.
-- No function updates or deletes this table; reassignment and replay are structurally denied.
create table public.support_admission_bindings (
 invocation_id uuid primary key references public.support_admission_invocations(invocation_id),
 lane text not null check (lane in ('support-resolution-send','support-ticket-close')),
 generation bigint not null,
 ticket_id uuid not null references public.support_tickets(id),
 request_version bigint not null check (request_version >= 0),
 message_id uuid not null references public.support_messages(id),
 bound_at timestamptz not null default clock_timestamp(),
 unique(lane, message_id),
 unique(invocation_id, lane, generation)
);
create index support_admission_bindings_ticket on public.support_admission_bindings(ticket_id);
-- A privileged support writer can still edit ordinary message metadata. Once a
-- message has driven an effect admission, its ticket identity cannot move.
create function public.support_admission_guard_bound_message()
returns trigger language plpgsql security definer set search_path = pg_catalog, public as $$
begin
 if tg_op = 'DELETE' then
  if exists (select 1 from public.support_admission_bindings where message_id = old.id) then
   raise exception 'Bound support message identity is immutable';
  end if;
  return old;
 end if;
 if (new.id is distinct from old.id or new.ticket_id is distinct from old.ticket_id)
    and exists (select 1 from public.support_admission_bindings where message_id = old.id) then
  raise exception 'Bound support message identity is immutable';
 end if;
 return new;
end $$;
create trigger support_admission_bound_message_identity
before update of id, ticket_id or delete on public.support_messages
for each row execute function public.support_admission_guard_bound_message();
revoke all on function public.support_admission_guard_bound_message() from public, anon, authenticated, service_role;
insert into public.support_admission_control(lane, operation_id) values ('ranger-lane', gen_random_uuid()), ('support-resolution-send', gen_random_uuid()), ('support-ticket-close', gen_random_uuid());
insert into public.support_admission_operations(operation_id,lane,generation) select operation_id,lane,generation from public.support_admission_control;
alter table public.support_admission_operations enable row level security;
alter table public.support_admission_control enable row level security;
alter table public.support_admission_invocations enable row level security;
alter table public.support_admission_bindings enable row level security;
revoke all on public.support_admission_control, public.support_admission_invocations, public.support_admission_operations, public.support_admission_bindings from public, anon, authenticated, service_role;

-- Supabase default ACLs grant service_role raw table privileges. RLS cannot
-- fence a BYPASSRLS principal: revoke direct grants and reject inherited/SET ROLE
-- authority instead of changing shared roles or trusting row policies.
do $$
declare r record; t regclass; priv text; q record;
begin
 for r in select oid,rolname,rolsuper,rolcreaterole from pg_roles
   where oid='service_role'::regrole or pg_has_role('service_role',oid,'USAGE')
     or pg_has_role('service_role',oid,'SET') loop
   if r.rolsuper or r.rolcreaterole then
     raise exception 'Support admission runtime role retains role escalation authority';
   end if;
   foreach t in array array['public.support_admission_control'::regclass,
       'public.support_admission_operations'::regclass,
       'public.support_admission_invocations'::regclass,
       'public.support_admission_bindings'::regclass] loop
     if (select relowner=r.oid from pg_class where oid=t) then
       raise exception 'Support admission runtime can assume table owner';
     end if;
     foreach priv in array array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN'] loop
       if has_table_privilege(r.oid,t,priv) then
         raise exception 'Support admission runtime retains raw table authority';
       end if;
     end loop;
     foreach priv in array array['SELECT','INSERT','UPDATE','REFERENCES'] loop
       if has_any_column_privilege(r.oid,t,priv) then
         raise exception 'Support admission runtime retains raw column authority';
       end if;
     end loop;
   end loop;
   for q in select seq.oid,seq.relowner from pg_class seq join pg_depend d on d.objid=seq.oid
     where seq.relkind='S' and d.refobjid in ('public.support_admission_control'::regclass,
       'public.support_admission_operations'::regclass,'public.support_admission_invocations'::regclass,
       'public.support_admission_bindings'::regclass) loop
     if q.relowner=r.oid or has_sequence_privilege(r.oid,q.oid,'SELECT,UPDATE,USAGE') then
       raise exception 'Support admission runtime retains raw sequence authority';
     end if;
   end loop;
 end loop;
end $$;

create function public.support_admission_acquire_lane(p_lane text,p_expected_generation bigint,p_invocation_id uuid, p_deployment text, p_build text)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare c public.support_admission_control%rowtype;
begin
 -- Fail closed: support-effect lanes may only enter through the bound acquisition.
 if p_lane in ('support-resolution-send','support-ticket-close') then
  raise exception 'Unbound acquisition denied for support effect lane';
 end if;
 select * into strict c from public.support_admission_control where lane = p_lane for update;
 if p_invocation_id is null or p_deployment is null or btrim(p_deployment) = '' or p_build is null or btrim(p_build) = '' then
  raise exception 'Invalid admission identity';
 end if;
 -- Replay never reopens an admitted invocation, even after its completion.
 if p_expected_generation is distinct from c.generation or c.paused or not c.allowed_builds @> jsonb_build_array(jsonb_build_object('deployment', p_deployment, 'build', p_build))
    or exists(select 1 from public.support_admission_invocations where invocation_id = p_invocation_id) then
  return jsonb_build_object('admitted', false);
 end if;
 insert into public.support_admission_invocations(invocation_id,lane,generation,deployment,build)
 values(p_invocation_id,c.lane,c.generation,p_deployment,p_build);
 return jsonb_build_object('admitted',true,'lane',c.lane,'invocation_id',p_invocation_id,'generation',c.generation);
end $$;

-- Strict bound acquisition for the support-effect lanes. Validates the current
-- ticket identity, request_version and message identity inside the same
-- transaction that holds the lane control lock, then records an immutable binding.
create function public.support_admission_acquire_bound(p_lane text,p_expected_generation bigint,p_invocation_id uuid,p_deployment text,p_build text,p_ticket_id uuid,p_expected_request_version bigint,p_message_id uuid)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare c public.support_admission_control%rowtype;
 t public.support_tickets%rowtype;
 m public.support_messages%rowtype;
begin
 if p_lane not in ('support-resolution-send','support-ticket-close') then
  raise exception 'Bound admission requires a support effect lane';
 end if;
 if p_invocation_id is null or p_deployment is null or btrim(p_deployment) = '' or p_build is null or btrim(p_build) = ''
    or p_ticket_id is null or p_message_id is null or p_expected_request_version is null or p_expected_request_version < 0 then
  raise exception 'Invalid bound admission identity';
 end if;
 select * into strict c from public.support_admission_control where lane = p_lane for update;
 -- A denied acquisition records nothing and returns no lease.
 if p_expected_generation is distinct from c.generation or c.paused or not c.allowed_builds @> jsonb_build_array(jsonb_build_object('deployment', p_deployment, 'build', p_build))
    or exists(select 1 from public.support_admission_invocations where invocation_id = p_invocation_id) then
  return jsonb_build_object('admitted', false);
 end if;
 -- Lock the ticket row so a concurrent request_version change cannot interleave
 -- between validation and the durable admission. Missing ticket raises.
 select * into strict t from public.support_tickets where id = p_ticket_id for update;
 if t.request_version is distinct from p_expected_request_version then
  raise exception 'Stale ticket request version';
 end if;
 -- An older reply on this ticket is not a reply to the current request.
 -- 0381 stamps outbound messages and makes the stamp/direction immutable.
 select * into strict m from public.support_messages where id = p_message_id for share;
 if m.ticket_id is distinct from p_ticket_id then
  raise exception 'Bound message does not belong to ticket';
 end if;
 if m.direction is distinct from 'outbound'
    or m.delivery_request_version is distinct from p_expected_request_version then
  raise exception 'Bound message is not for the current outbound request';
 end if;
 -- A message already bound in this lane can never drive a second admission.
 if exists(select 1 from public.support_admission_bindings where lane = c.lane and message_id = p_message_id) then
  raise exception 'Conflicting admission binding';
 end if;
 insert into public.support_admission_invocations(invocation_id,lane,generation,deployment,build)
 values(p_invocation_id,c.lane,c.generation,p_deployment,p_build);
 insert into public.support_admission_bindings(invocation_id,lane,generation,ticket_id,request_version,message_id)
 values(p_invocation_id,c.lane,c.generation,p_ticket_id,p_expected_request_version,p_message_id);
 return jsonb_build_object('admitted',true,'lane',c.lane,'invocation_id',p_invocation_id,'generation',c.generation,
   'ticket_id',p_ticket_id,'request_version',p_expected_request_version,'message_id',p_message_id);
end $$;

create function public.support_admission_pause_lane(p_lane text,p_operation_id uuid, p_expected_generation bigint)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare c public.support_admission_control%rowtype;
begin
 select * into strict c from public.support_admission_control where lane = p_lane for update;
 if p_operation_id is null then raise exception 'Operation required'; end if;
 if c.paused and c.operation_id = p_operation_id and c.generation = p_expected_generation then
  return jsonb_build_object('paused',true,'generation',c.generation,'operation_id',c.operation_id);
 end if;
 if c.paused or c.generation is distinct from p_expected_generation or c.operation_id = p_operation_id then
  raise exception 'Stale pause or operation replay';
 end if;
 insert into public.support_admission_operations(operation_id,lane,generation) values(p_operation_id,c.lane,c.generation+1);
 update public.support_admission_control set paused=true,generation=generation+1,operation_id=p_operation_id
 where lane=c.lane returning * into c;
 return jsonb_build_object('paused',true,'generation',c.generation,'operation_id',c.operation_id);
end $$;

create function public.support_admission_resume_lane(p_lane text,p_operation_id uuid, p_expected_generation bigint, p_allowed_builds jsonb)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare c public.support_admission_control%rowtype;
begin
 select * into strict c from public.support_admission_control where lane = p_lane for update;
 if p_operation_id is distinct from c.operation_id or p_expected_generation is distinct from c.generation then raise exception 'Stale resume'; end if;
 if p_allowed_builds is null or jsonb_typeof(p_allowed_builds) <> 'array' or jsonb_array_length(p_allowed_builds) = 0 then raise exception 'Exact build allowlist required'; end if;
 if exists(select 1 from jsonb_array_elements(p_allowed_builds) b where
  jsonb_typeof(b) <> 'object' or jsonb_typeof(b->'deployment') is distinct from 'string' or
  jsonb_typeof(b->'build') is distinct from 'string' or btrim(b->>'deployment') = '' or btrim(b->>'build') = '') then
  raise exception 'Invalid build allowlist';
 end if;
 if not c.paused then
  if c.allowed_builds is distinct from p_allowed_builds then raise exception 'Resume replay changed allowlist'; end if;
  return jsonb_build_object('resumed',true,'generation',c.generation);
 end if;
 if exists(select 1 from public.support_admission_invocations where lane=c.lane and unresolved) then raise exception 'Not drained'; end if;
 update public.support_admission_control set paused=false,allowed_builds=p_allowed_builds where lane=c.lane;
 return jsonb_build_object('resumed',true,'generation',c.generation);
end $$;

create function public.support_admission_finish_lane(p_lane text,p_invocation_id uuid,p_generation bigint,p_outcome text)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare i public.support_admission_invocations%rowtype;
begin
 perform 1 from public.support_admission_control where lane = p_lane for update;
 if not found then raise exception 'Admission control missing'; end if;
 select * into strict i from public.support_admission_invocations where invocation_id=p_invocation_id for update;
 if i.lane is distinct from p_lane or i.generation is distinct from p_generation or p_outcome is null or p_outcome not in ('completed','unknown') then raise exception 'Invalid completion'; end if;
 if i.outcome <> 'running' then
  if i.outcome <> p_outcome then raise exception 'Conflicting completion'; end if;
 else
  update public.support_admission_invocations set ended_at=clock_timestamp(),outcome=p_outcome,unresolved=(p_outcome <> 'completed') where invocation_id=p_invocation_id;
 end if;
 return jsonb_build_object('recorded',true,'lane',i.lane,'invocation_id',p_invocation_id);
end $$;

create function public.support_admission_status_lane(p_lane text)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare c public.support_admission_control%rowtype; n bigint;
begin
 select * into strict c from public.support_admission_control where lane = p_lane for update;
 select count(*) into n from public.support_admission_invocations where lane=c.lane and unresolved;
 return jsonb_build_object('lane',c.lane,'paused',c.paused,'drained',c.paused and n=0,'generation',c.generation,'operation_id',c.operation_id,'unresolved',n);
end $$;

-- Bounded, deterministic, read-only invocation inventory for independent Echo
-- reconciliation. Keyset pagination over (started_at, invocation_id): no offset
-- drift, no silent truncation (has_more and the next cursor are always returned),
-- no message body or ticket/client content. The envelope repeats the lane's
-- current generation and operation so the reader can detect mid-scan changes.
create function public.support_admission_inventory(p_lane text,p_limit integer,p_after_started timestamptz default null,p_after_invocation uuid default null)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare c public.support_admission_control%rowtype; items jsonb; more boolean;
begin
 select * into strict c from public.support_admission_control where lane = p_lane;
 if p_limit is null or p_limit < 1 or p_limit > 500 then raise exception 'Inventory limit must be between 1 and 500'; end if;
 if (p_after_started is null) <> (p_after_invocation is null) then raise exception 'Inventory cursor requires started and invocation together'; end if;
 select coalesce(jsonb_agg(jsonb_build_object(
     'invocation_id',page.invocation_id,'lane',page.lane,'generation',page.generation,
     'deployment',page.deployment,'build',page.build,
     'started_at',page.started_at,'ended_at',page.ended_at,'outcome',page.outcome,'unresolved',page.unresolved,
     'ticket_id',page.ticket_id,'request_version',page.request_version,'message_id',page.message_id
   ) order by page.started_at, page.invocation_id), '[]'::jsonb) into items
 from (
   select i.*, b.ticket_id, b.request_version, b.message_id
   from public.support_admission_invocations i
   left join public.support_admission_bindings b on b.invocation_id = i.invocation_id
   where i.lane = p_lane
     and (p_after_started is null or (i.started_at, i.invocation_id) > (p_after_started, p_after_invocation))
   order by i.started_at, i.invocation_id
   limit p_limit + 1
 ) page;
 more := jsonb_array_length(items) > p_limit;
 if more then
  items := (select jsonb_agg(e.value order by e.idx) from jsonb_array_elements(items) with ordinality e(value,idx) where e.idx <= p_limit);
 end if;
 return jsonb_build_object(
  'lane',c.lane,'generation',c.generation,'operation_id',c.operation_id,'paused',c.paused,
  'limit',p_limit,'returned',jsonb_array_length(items),'has_more',more,
  'next_after_started', case when jsonb_array_length(items) > 0 then items->(jsonb_array_length(items)-1)->>'started_at' end,
  'next_after_invocation', case when jsonb_array_length(items) > 0 then items->(jsonb_array_length(items)-1)->>'invocation_id' end,
  'invocations',items);
end $$;
-- Compatibility wrappers retain the original Ranger contract. New send/close callers
-- must read status then acquire their exact lane with that observed generation
-- through the bound acquisition.
create function public.support_admission_acquire(p_invocation_id uuid,p_deployment text,p_build text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare g bigint;
begin
 select generation into strict g from public.support_admission_control where lane='ranger-lane' for update;
 return public.support_admission_acquire_lane('ranger-lane',g,p_invocation_id,p_deployment,p_build);
end $$;
create function public.support_admission_pause(p_operation_id uuid,p_expected_generation bigint)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$ select public.support_admission_pause_lane('ranger-lane',p_operation_id,p_expected_generation) $$;
create function public.support_admission_resume(p_operation_id uuid,p_expected_generation bigint,p_allowed_builds jsonb)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$ select public.support_admission_resume_lane('ranger-lane',p_operation_id,p_expected_generation,p_allowed_builds) $$;
create function public.support_admission_finish(p_invocation_id uuid,p_generation bigint,p_outcome text)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$ select public.support_admission_finish_lane('ranger-lane',p_invocation_id,p_generation,p_outcome) $$;
create function public.support_admission_status()
returns jsonb language sql security definer set search_path=pg_catalog,public as $$ select public.support_admission_status_lane('ranger-lane') $$;
revoke all on function public.support_admission_acquire(uuid,text,text), public.support_admission_pause(uuid,bigint), public.support_admission_resume(uuid,bigint,jsonb), public.support_admission_finish(uuid,bigint,text), public.support_admission_status() from public,anon,authenticated;
grant execute on function public.support_admission_acquire(uuid,text,text), public.support_admission_pause(uuid,bigint), public.support_admission_resume(uuid,bigint,jsonb), public.support_admission_finish(uuid,bigint,text), public.support_admission_status() to service_role;
revoke all on function public.support_admission_acquire_lane(text,bigint,uuid,text,text), public.support_admission_pause_lane(text,uuid,bigint), public.support_admission_resume_lane(text,uuid,bigint,jsonb), public.support_admission_finish_lane(text,uuid,bigint,text), public.support_admission_status_lane(text), public.support_admission_acquire_bound(text,bigint,uuid,text,text,uuid,bigint,uuid), public.support_admission_inventory(text,integer,timestamptz,uuid) from public,anon,authenticated;
grant execute on function public.support_admission_acquire_lane(text,bigint,uuid,text,text), public.support_admission_pause_lane(text,uuid,bigint), public.support_admission_resume_lane(text,uuid,bigint,jsonb), public.support_admission_finish_lane(text,uuid,bigint,text), public.support_admission_status_lane(text), public.support_admission_acquire_bound(text,bigint,uuid,text,text,uuid,bigint,uuid), public.support_admission_inventory(text,integer,timestamptz,uuid) to service_role;
commit;
