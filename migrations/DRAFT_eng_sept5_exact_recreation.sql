-- DRAFT. Requires the reviewed forward-stage and reservation migrations.
-- This admits one historical ENG support request into the existing attested
-- candidate/finalizer lane. Admission defaults OFF. No old row is rewritten.
begin;

create table public.eng_sept5_recreation_gate (
  singleton boolean primary key default true check (singleton),
  enabled boolean not null default false
);
insert into public.eng_sept5_recreation_gate(singleton,enabled) values(true,false);
revoke all on public.eng_sept5_recreation_gate from public,anon,authenticated,service_role;
grant select on public.eng_sept5_recreation_gate to service_role;

create table public.eng_sept5_recreation_receipt (
  ticket_id uuid primary key references public.support_tickets(id)
    check (ticket_id='35e066d0-d9bc-40e6-aef8-86719a010590'),
  tenant_id text not null check (tenant_id='eng'),
  ticket_snapshot jsonb not null,
  inbound_sha256 text not null check (inbound_sha256 ~ '^[0-9a-f]{64}$'),
  original_snapshots jsonb not null,
  target_date date not null,
  logical_post_id uuid not null unique
    check (logical_post_id='6d21c00f-349c-5a06-9328-5a570a81babb'),
  batch_id uuid unique,
  state text not null default 'reserved' check (state in ('reserved','staged','finalized')),
  finalize_request jsonb,
  finalize_receipt jsonb,
  created_at timestamptz not null default clock_timestamp(),
  finalized_at timestamptz
);
alter table public.eng_sept5_recreation_receipt enable row level security;
revoke all on public.eng_sept5_recreation_receipt from public,anon,authenticated,service_role;
grant select on public.eng_sept5_recreation_receipt to service_role;

create function public.eng_sept5_inbound_sha256()
returns text language sql stable security definer set search_path=pg_catalog,public as $$
 select encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(m) order by m.created_at,m.id)::text,'[]'),'UTF8')),'hex')
 from public.support_messages m
 where m.ticket_id='35e066d0-d9bc-40e6-aef8-86719a010590'
   and m.direction='inbound'
$$;
revoke all on function public.eng_sept5_inbound_sha256() from public,anon,authenticated,service_role;

create function public.eng_sept5_originals_current(p_snapshots jsonb)
returns boolean language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare v_ids uuid[]; v_row jsonb; v_calendar public.content_calendar;
begin
 if jsonb_typeof(p_snapshots) is distinct from 'array'
    or jsonb_array_length(p_snapshots)<>3 then return false; end if;
 select array_agg((x->>'id')::uuid order by (x->>'id')::uuid) into v_ids
 from jsonb_array_elements(p_snapshots) x;
 if v_ids is distinct from array[
  '2ad9f097-e7cc-49a2-b348-30c8e1cda80d'::uuid,
  'b1bb4d63-fda7-4b1e-9303-dff3483f4387'::uuid,
  'ff792b3e-be08-4c4e-b0f5-0347e075a9ba'::uuid] then return false; end if;
 for v_row in select value from jsonb_array_elements(p_snapshots) loop
   select * into v_calendar from public.content_calendar where id=(v_row->>'id')::uuid;
   if not found or to_jsonb(v_calendar) is distinct from v_row
      or v_calendar.gym_id is distinct from 'eng'
      or v_calendar.post_date is distinct from '2026-09-04'::date
      or v_calendar.status is distinct from 'denied'
      or v_calendar.variant_status is distinct from 'active'
      or v_calendar.source_media_asset_id is distinct from '1BNl4pJqTFF21JYk-RIDS9-YHz9YYXLKt'
      or v_calendar.published_at is not null or v_calendar.late_post_id is not null
      or v_calendar.publish_claim_token is not null then return false; end if;
   if (v_calendar.id='2ad9f097-e7cc-49a2-b348-30c8e1cda80d'::uuid
       and (v_calendar.account,v_calendar.format) is distinct from ('facebook','feed'))
      or (v_calendar.id='b1bb4d63-fda7-4b1e-9303-dff3483f4387'::uuid
       and (v_calendar.account,v_calendar.format) is distinct from ('instagram','feed'))
      or (v_calendar.id='ff792b3e-be08-4c4e-b0f5-0347e075a9ba'::uuid
       and (v_calendar.account,v_calendar.format) is distinct from ('instagram','story')) then
     return false;
   end if;
 end loop;
 return true;
exception when invalid_text_representation then return false;
end;
$$;
revoke all on function public.eng_sept5_originals_current(jsonb) from public,anon,authenticated,service_role;

create function public.eng_sept5_recreation_begin(p_target_date date)
returns public.eng_sept5_recreation_receipt language plpgsql security definer
 set search_path=pg_catalog,public as $$
declare t public.support_tickets; r public.eng_sept5_recreation_receipt;
 v_originals jsonb; v_inbound text;
begin
 if not exists(select 1 from public.eng_sept5_recreation_gate where singleton and enabled) then
   raise exception 'ENG recreation admission is OFF' using errcode='55000'; end if;
 if p_target_date<=current_date or p_target_date>current_date+31 then
   raise exception 'future target required' using errcode='22023'; end if;
 -- Replay must take the same receipt -> ticket -> originals order as finalize.
 -- On the initial call no receipt exists yet; the ticket lock serializes the
 -- insert and a concurrent finalizer cannot see that receipt until commit.
 select * into r from public.eng_sept5_recreation_receipt
  where ticket_id='35e066d0-d9bc-40e6-aef8-86719a010590' for update;
 select * into t from public.support_tickets
  where id='35e066d0-d9bc-40e6-aef8-86719a010590' for update;
 if not found or t.client_id is distinct from '6ee04ee4-13a5-47db-8416-7b8ee3e61ab8'::uuid
    or t.product is distinct from 'echo' or t.source is distinct from 'website_tab'
    or t.request_version is distinct from 0 or t.status is distinct from 'resolved'
    or t.resolved_at is not null
    or (select count(*) from public.support_messages m
        where m.ticket_id=t.id and m.direction='inbound')<>1 then
   raise exception 'ENG support request changed' using errcode='23514'; end if;
 v_inbound:=public.eng_sept5_inbound_sha256();
 perform 1 from public.content_calendar where id=any(array[
  '2ad9f097-e7cc-49a2-b348-30c8e1cda80d'::uuid,
  'b1bb4d63-fda7-4b1e-9303-dff3483f4387'::uuid,
  'ff792b3e-be08-4c4e-b0f5-0347e075a9ba'::uuid]) order by id for update;
 select jsonb_agg(to_jsonb(c) order by c.id) into v_originals
 from public.content_calendar c where c.id=any(array[
  '2ad9f097-e7cc-49a2-b348-30c8e1cda80d'::uuid,
  'b1bb4d63-fda7-4b1e-9303-dff3483f4387'::uuid,
  'ff792b3e-be08-4c4e-b0f5-0347e075a9ba'::uuid]);
 if not public.eng_sept5_originals_current(v_originals) then
   raise exception 'ENG denied originals changed' using errcode='23514'; end if;
 if r.ticket_id is not null then
   if r.ticket_snapshot is distinct from to_jsonb(t)
      or r.inbound_sha256 is distinct from v_inbound
      or r.original_snapshots is distinct from v_originals
      or r.target_date is distinct from p_target_date then
     raise exception 'ENG recreation already reserved for another request' using errcode='23514'; end if;
   return r;
 end if;
 insert into public.eng_sept5_recreation_receipt
  (ticket_id,tenant_id,ticket_snapshot,inbound_sha256,original_snapshots,target_date,logical_post_id)
 values(t.id,'eng',to_jsonb(t),v_inbound,v_originals,p_target_date,
  '6d21c00f-349c-5a06-9328-5a570a81babb') returning * into r;
 return r;
end;
$$;
revoke all on function public.eng_sept5_recreation_begin(date)
 from public,anon,authenticated,service_role;
grant execute on function public.eng_sept5_recreation_begin(date) to service_role;

create function public.eng_sept5_recreation_bind(p_batch_id uuid)
returns public.eng_sept5_recreation_receipt language plpgsql security definer
 set search_path=pg_catalog,public as $$
declare r public.eng_sept5_recreation_receipt; b public.forward_schedule_stage_batch_20261008;
 v_count integer; v_ig integer; v_fb integer; v_story integer;
 v_sources integer; v_feed_captions integer;
begin
 if not exists(select 1 from public.eng_sept5_recreation_gate where singleton and enabled) then
   raise exception 'ENG recreation admission is OFF' using errcode='55000'; end if;
 select * into r from public.eng_sept5_recreation_receipt
  where ticket_id='35e066d0-d9bc-40e6-aef8-86719a010590' for update;
 if not found or r.state not in ('reserved','staged') then
   raise exception 'ENG recreation not reserved' using errcode='23514'; end if;
 if r.batch_id is not null then
   if r.batch_id is distinct from p_batch_id then
     raise exception 'ENG recreation batch changed' using errcode='23514'; end if;
   return r;
 end if;
 select * into b from public.forward_schedule_stage_batch_20261008
  where batch_id=p_batch_id for update;
 if not found or b.state is distinct from 'staged' or b.tenant_id is distinct from 'eng' then
   raise exception 'exact ENG staged batch unavailable' using errcode='23514'; end if;
 if exists(select 1 from public.forward_schedule_stage_old_row_20261008
           where batch_id=p_batch_id) then
   raise exception 'ENG recreation must append, never replace' using errcode='23514'; end if;
 select count(*),
  count(*) filter(where c.account='instagram' and c.format='feed'),
  count(*) filter(where c.account='facebook' and c.format='feed'),
  count(*) filter(where c.account='instagram' and c.format='story'),
  count(distinct c.source_media_url),
  count(distinct c.caption) filter(where c.format='feed')
 into v_count,v_ig,v_fb,v_story,v_sources,v_feed_captions
 from public.forward_schedule_stage_member_20261008 m
 join public.content_calendar c on c.id=m.calendar_row_id
 where m.batch_id=p_batch_id
  and c.gym_id='eng' and c.logical_post_id=r.logical_post_id
  and c.post_date=r.target_date and c.status='pending'
  and c.variant_status='candidate'
  and c.media_not_ready_reason='forward_reservation_staged'
  and m.observation_digest is not null
  and c.approved_at is null and c.approved_by is null
  and c.publish_claim_token is null and c.published_at is null and c.late_post_id is null;
 if v_count<>3 or v_ig<>1 or v_fb<>1 or v_story<>1
   or v_sources<>1 or v_feed_captions<>1
   or (select count(*) from public.forward_schedule_stage_member_20261008 where batch_id=p_batch_id)<>3
   or r.target_date<=current_date then
   raise exception 'ENG staged candidate group does not match ticket' using errcode='23514'; end if;
 update public.eng_sept5_recreation_receipt set batch_id=p_batch_id,state='staged'
  where ticket_id=r.ticket_id returning * into r;
 return r;
end;
$$;
revoke all on function public.eng_sept5_recreation_bind(uuid)
 from public,anon,authenticated,service_role;
grant execute on function public.eng_sept5_recreation_bind(uuid) to service_role;

create function public.eng_sept5_recreation_finalize(p_batch_id uuid,p_candidates jsonb)
returns public.eng_sept5_recreation_receipt language plpgsql security definer
 set search_path=pg_catalog,public as $$
declare r public.eng_sept5_recreation_receipt; t public.support_tickets;
 v_request jsonb; v_result jsonb; v_ids uuid[]; v_expected uuid[]; v_receipt_ids uuid[];
begin
 if not exists(select 1 from public.eng_sept5_recreation_gate where singleton and enabled) then
   raise exception 'ENG recreation admission is OFF' using errcode='55000'; end if;
 -- Match the existing forward finalizer lock order before ticket/calendar locks.
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into r from public.eng_sept5_recreation_receipt
  where ticket_id='35e066d0-d9bc-40e6-aef8-86719a010590' for update;
 v_request:=jsonb_build_object('batch_id',p_batch_id,'candidates',p_candidates);
 if r.state='finalized' then
   if r.finalize_request is distinct from v_request then
     raise exception 'ENG recreation finalization request changed' using errcode='23514'; end if;
   return r;
 end if;
 if not found or r.state is distinct from 'staged' or r.batch_id is distinct from p_batch_id
    or r.target_date<=current_date or jsonb_typeof(p_candidates) is distinct from 'array'
    or jsonb_array_length(p_candidates)<>3 then
   raise exception 'ENG recreation finalization not current' using errcode='23514'; end if;
 select * into t from public.support_tickets where id=r.ticket_id for update;
 -- Freeze the three historical denied rows through the downstream activation
 -- transaction. A concurrent edit that wins first must complete before this
 -- request rechecks the original snapshots and then be refused.
 perform 1 from public.content_calendar where id=any(array[
  '2ad9f097-e7cc-49a2-b348-30c8e1cda80d'::uuid,
  'b1bb4d63-fda7-4b1e-9303-dff3483f4387'::uuid,
  'ff792b3e-be08-4c4e-b0f5-0347e075a9ba'::uuid]) order by id for update;
 if t.id is null or to_jsonb(t) is distinct from r.ticket_snapshot
    or public.eng_sept5_inbound_sha256() is distinct from r.inbound_sha256
    or not public.eng_sept5_originals_current(r.original_snapshots) then
   raise exception 'ENG request or denied originals changed' using errcode='23514'; end if;
 select array_agg((x->>'calendar_row_id')::uuid order by (x->>'calendar_row_id')::uuid)
 into v_ids from jsonb_array_elements(p_candidates) x;
 select array_agg(calendar_row_id order by calendar_row_id) into v_expected
 from public.forward_schedule_stage_member_20261008 where batch_id=p_batch_id;
 if v_ids is distinct from v_expected or cardinality(v_ids)<>3
    or (select count(distinct x) from unnest(v_ids) x)<>3 then
   raise exception 'ENG finalization candidates changed' using errcode='23514'; end if;
 -- The existing finalizer atomically rechecks attested bytes, reserved slots,
 -- every staged row, and approval/publish state. Empty old set is mandatory.
 v_result:=public.finalize_forward_schedule_staged_batch_20261008(
  'eng',p_batch_id,p_candidates,'[]'::jsonb);
 select array_agg(x::uuid order by x::uuid) into v_receipt_ids
 from jsonb_array_elements_text(v_result->'row_ids') x;
 if v_result->>'state' is distinct from 'finalized'
    or v_result->>'batch_id' is distinct from p_batch_id::text
    or v_result->>'tenant_id' is distinct from 'eng'
    or v_result->'archived_old_row_ids' is distinct from '[]'::jsonb
    or v_receipt_ids is distinct from v_ids then
   raise exception 'ENG forward batch finalizer receipt mismatch' using errcode='23514'; end if;
 update public.eng_sept5_recreation_receipt set state='finalized',
   finalize_request=v_request,finalize_receipt=v_result,finalized_at=clock_timestamp()
  where ticket_id=r.ticket_id and state='staged' returning * into r;
 if not found then raise exception 'ENG receipt finalization raced' using errcode='23514'; end if;
 return r;
end;
$$;
revoke all on function public.eng_sept5_recreation_finalize(uuid,jsonb)
 from public,anon,authenticated,service_role;
grant execute on function public.eng_sept5_recreation_finalize(uuid,jsonb) to service_role;
commit;
