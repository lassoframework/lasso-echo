-- DRAFT / UNAPPLIED. Depends on generated source/palette authority and assembled
-- generated owner + forward claim drafts. No runtime, role login or activation.
-- Durable lease is a SEND DECISION, not a publication receipt. No TTL/auto retry.
-- Receipt authenticity is verified by trusted publisher/reconciler callers.
-- Existing reservations lacking candidate_json.authority_pins cannot acquire.
-- Future owner integration must bind those pins under canonical locks before
-- generating/reserving. Legacy local approvals never supply these pins.
begin;
do $$ begin
 if not exists(select 1 from pg_roles where rolname='generated_send_reconciler_20261007') then
  create role generated_send_reconciler_20261007 nologin; end if;
end; $$;
create table public.generated_send_lease_20261007 (
 attempt_token uuid primary key, tenant_id text not null, calendar_row_id uuid not null,
 claim_token uuid not null unique, job_id uuid not null, authority_pins jsonb not null,
 state text not null check(state in ('reserved','inflight','unknown','sent','not_sent')),
 terminal_evidence jsonb, created_at timestamptz not null default clock_timestamp(),
 updated_at timestamptz not null default clock_timestamp(),
 check((state in ('sent','not_sent')) = (terminal_evidence is not null))
);
create unique index generated_send_one_open_row_20261007
 on public.generated_send_lease_20261007(calendar_row_id) where state in ('reserved','inflight','unknown');
create table public.generated_revocation_request_20261007 (
 request_id uuid primary key, tenant_id text not null, entity_type text not null check(entity_type in ('source','palette')),
 entity_key text not null, expected_epoch bigint not null, request_evidence jsonb not null,
 state text not null check(state in ('pending','effective')), effective_epoch bigint,
 created_at timestamptz not null default clock_timestamp(),
 check((state='effective')=(effective_epoch is not null))
);
create unique index generated_revocation_one_pending_20261007
 on public.generated_revocation_request_20261007(tenant_id,entity_type,entity_key) where state='pending';
create table public.generated_send_audit_20261007 (
 audit_id bigint generated always as identity primary key, tenant_id text not null,
 event text not null, identity_token uuid not null, payload jsonb not null,
 actor text not null default session_user, created_at timestamptz not null default clock_timestamp()
);
do $$ declare t text; begin
 foreach t in array array['generated_send_lease_20261007','generated_revocation_request_20261007','generated_send_audit_20261007'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role,generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007',t);
  execute format('create trigger immutable_delete before delete on public.%I for each row execute function public.generated_authority_immutable_20261007()',t);
  execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.generated_authority_immutable_20261007()',t);
 end loop;
end; $$;
create trigger immutable_update before update on public.generated_send_audit_20261007
 for each row execute function public.generated_authority_immutable_20261007();
revoke all on sequence public.generated_send_audit_20261007_audit_id_seq from public,anon,authenticated,service_role,generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007;

-- Approval captures only pins already persisted by the trusted generation owner.
-- This is an additional gate, never a new source approval or human identity API.
-- Legacy reservations and local hashes cannot bootstrap canonical authority.
alter table public.content_calendar add column generated_authority_pins jsonb;
create function public.generated_approval_pins_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare p jsonb;
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
 select candidate_json->'authority_pins' into p from public.fixer_generated_reservation_20261007
  where 'generated-astra:'||job_id::text=new.source_media_asset_id;
 if jsonb_typeof(p) is distinct from 'object' or p->>'tenant_id' is distinct from new.gym_id
  or jsonb_typeof(p->'epoch') is distinct from 'number'
  or jsonb_typeof(p->'source_revision') is distinct from 'number'
  or jsonb_typeof(p->'palette_revision') is distinct from 'number'
  or coalesce(p->>'epoch','') !~ '^[1-9][0-9]*$'
  or coalesce(p->>'source_revision','') !~ '^[1-9][0-9]*$'
  or coalesce(p->>'palette_revision','') !~ '^[1-9][0-9]*$' then
  raise exception 'owner canonical pins required at approval' using errcode='23514'; end if;
 perform public.generated_authority_validate_publish_20261007(new.gym_id,(p->>'epoch')::bigint,
  array[p->>'source_id'],array[(p->>'source_revision')::bigint],p->>'palette_key',(p->>'palette_revision')::bigint);
 if not exists(select 1 from public.generated_source_authority_20261007
  where tenant_id=new.gym_id and source_id=p->>'source_id' and exact_text=new.caption) then
  raise exception 'canonical source differs from approved caption' using errcode='23514'; end if;
 new.generated_authority_pins:=p;
 return new;
end; $$;
revoke all on function public.generated_approval_pins_20261007() from public,anon,authenticated,service_role;
create trigger generated_approval_pins before insert or update on public.content_calendar
 for each row execute function public.generated_approval_pins_20261007();

-- One lock shared by every lease transition and canonical authority mutation.
-- Frozen authority cannot change while any reserved/inflight/unknown attempt
-- exists. Revocation requests stay visible and block new decisions immediately.
alter function public.generated_authority_write_20261007(text,bigint,jsonb)
 rename to generated_authority_pre_lease_write_20261007;
revoke all on function public.generated_authority_pre_lease_write_20261007(text,bigint,jsonb)
 from public,anon,authenticated,service_role,generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007;
create function public.generated_authority_write_20261007(p_tenant_id text,p_expected_epoch bigint,p_ops jsonb)
returns bigint language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(p_tenant_id));
 if exists(select 1 from public.generated_send_lease_20261007 where tenant_id=p_tenant_id and state in ('reserved','inflight','unknown'))
  or exists(select 1 from public.generated_revocation_request_20261007 where tenant_id=p_tenant_id and state='pending') then
  raise exception 'outstanding generated send decision; request deferred revocation' using errcode='55000'; end if;
 return public.generated_authority_pre_lease_write_20261007(p_tenant_id,p_expected_epoch,p_ops);
end; $$;

create function public.generated_send_acquire_20261007(p_attempt uuid,p_tenant text,p_row uuid,p_claim uuid,p_job uuid,p_pins jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; g public.fixer_generated_reservation_20261007%rowtype;
 claim public.fixer_forward_media_claim_receipt_20261006%rowtype; prior public.generated_send_lease_20261007%rowtype;
 binding jsonb;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 if p_attempt is null or p_row is null or p_claim is null or p_job is null
  or coalesce(p_tenant,'') !~ '[^[:space:]]' or jsonb_typeof(p_pins) is distinct from 'object'
  or p_pins->>'tenant_id' is distinct from p_tenant
  or coalesce(p_pins->>'source_id','') !~ '[^[:space:]]'
  or coalesce(p_pins->>'palette_key','') !~ '[^[:space:]]'
  or coalesce(p_pins->>'epoch','') !~ '^[1-9][0-9]*$'
  or coalesce(p_pins->>'source_revision','') !~ '^[1-9][0-9]*$'
  or coalesce(p_pins->>'palette_revision','') !~ '^[1-9][0-9]*$'
  or jsonb_typeof(p_pins->'epoch') is distinct from 'number'
  or jsonb_typeof(p_pins->'source_revision') is distinct from 'number'
  or jsonb_typeof(p_pins->'palette_revision') is distinct from 'number' then
  raise exception 'exact canonical send identity and pins required' using errcode='23514'; end if;
 -- Same graph -> canonical -> calendar row order as future owner integration.
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
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
 binding:=public.fixer_generated_publish_readback_20261007(p_row);
 if binding is null or binding->>'job_id' is distinct from p_job::text or binding->>'gym_id' is distinct from p_tenant then
  raise exception 'generated reservation readback unavailable' using errcode='23514'; end if;
 select * into claim from public.fixer_forward_media_claim_receipt_20261006 where claim_token=p_claim;
 if not found or claim.calendar_row_id is distinct from p_row or claim.tenant_id is distinct from p_tenant
  or claim.post_date is distinct from r.post_date or claim.group_key is distinct from r.visual_group_key
  or claim.source_url is distinct from r.source_media_url or claim.image_url is distinct from r.image_url
  or claim.thumbnail_url is distinct from r.thumbnail_url or claim.reservation_day is distinct from r.publish_reservation_day then
  raise exception 'committed exact forward claim receipt required' using errcode='23514'; end if;
 perform public.generated_authority_validate_publish_20261007(p_tenant,(p_pins->>'epoch')::bigint,
  array[p_pins->>'source_id'],array[(p_pins->>'source_revision')::bigint],p_pins->>'palette_key',(p_pins->>'palette_revision')::bigint);
 if not exists(select 1 from public.generated_source_authority_20261007 s where s.tenant_id=p_tenant
  and s.source_id=p_pins->>'source_id' and s.exact_text=r.caption) then
  raise exception 'canonical source differs from outgoing caption' using errcode='23514'; end if;
 if exists(select 1 from public.generated_send_lease_20261007 where calendar_row_id=p_row and state='sent') then
  raise exception 'generated row already has a sent attempt' using errcode='23514'; end if;
 insert into public.generated_send_lease_20261007 values(p_attempt,p_tenant,p_row,p_claim,p_job,p_pins,'reserved',null,clock_timestamp(),clock_timestamp());
 insert into public.generated_send_audit_20261007(tenant_id,event,identity_token,payload) values(p_tenant,'reserve',p_attempt,p_pins);
 return jsonb_build_object('attempt_token',p_attempt,'state','reserved','replayed',false,'authorize_send',false);
end; $$;

create function public.generated_send_begin_20261007(p_attempt uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare l public.generated_send_lease_20261007%rowtype; r public.content_calendar%rowtype; binding jsonb;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 select * into l from public.generated_send_lease_20261007 where attempt_token=p_attempt;
 if not found then raise exception 'unknown generated attempt' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(l.tenant_id));
 select * into l from public.generated_send_lease_20261007 where attempt_token=p_attempt for update;
 if l.state<>'reserved' then return to_jsonb(l)||jsonb_build_object('authorize_send',false); end if;
 if exists(select 1 from public.generated_revocation_request_20261007 where tenant_id=l.tenant_id and state='pending') then
  raise exception 'pending revocation blocks unstarted attempt; reconcile cancellation' using errcode='55000'; end if;
 select * into r from public.content_calendar where id=l.calendar_row_id for update;
 binding:=public.fixer_generated_publish_readback_20261007(l.calendar_row_id);
 if not found or r.gym_id is distinct from l.tenant_id or r.status is distinct from 'publishing'
  or r.publish_claim_token is distinct from l.claim_token or r.published_at is not null or r.late_post_id is not null
  or binding is null or binding->>'job_id' is distinct from l.job_id::text then
  raise exception 'reserved publishing identity changed before attempt' using errcode='23514'; end if;
 update public.generated_send_lease_20261007 set state='inflight',updated_at=clock_timestamp() where attempt_token=p_attempt;
 insert into public.generated_send_audit_20261007(tenant_id,event,identity_token,payload) values(l.tenant_id,'begin',p_attempt,'{}');
 return jsonb_build_object('attempt_token',p_attempt,'state','inflight','authorize_send',true);
end; $$;

-- Request commits even while attempt outcome is unknown. "pending" is never
-- represented as effective revocation. All tenant leases are blocked until it
-- is effective, including leases for unrelated sources (conservative fence).
create function public.generated_revoke_request_20261007(p_request uuid,p_tenant text,p_epoch bigint,p_type text,p_key text,p_evidence jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.generated_revocation_request_20261007%rowtype; e bigint; entity_status text;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 if p_request is null or coalesce(p_tenant,'') !~ '[^[:space:]]' or p_type is null or p_type not in ('source','palette')
  or coalesce(p_key,'') !~ '[^[:space:]]' or jsonb_typeof(p_evidence) is distinct from 'object'
  or jsonb_typeof(p_evidence->'receipt_ref') is distinct from 'string'
  or jsonb_typeof(p_evidence->'actor') is distinct from 'string'
  or coalesce(p_evidence->>'receipt_ref','') !~ '[^[:space:]]' or coalesce(p_evidence->>'actor','') !~ '[^[:space:]]' then
  raise exception 'exact revocation request and evidence required' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(p_tenant));
 select * into q from public.generated_revocation_request_20261007 where request_id=p_request;
 if found then
  if q.tenant_id is distinct from p_tenant or q.expected_epoch is distinct from p_epoch or q.entity_type is distinct from p_type
   or q.entity_key is distinct from p_key or q.request_evidence is distinct from p_evidence then
   raise exception 'revocation request immutable identity conflict' using errcode='23514'; end if;
  return to_jsonb(q);
 end if;
 select epoch into e from public.generated_tenant_epoch_20261007 where tenant_id=p_tenant for update;
 if e is null or e is distinct from p_epoch then raise exception 'stale revocation epoch' using errcode='40001'; end if;
 if p_type='source' then select status into entity_status from public.generated_source_authority_20261007 where tenant_id=p_tenant and source_id=p_key;
 else select status into entity_status from public.generated_palette_authority_20261007 where tenant_id=p_tenant and palette_key=p_key; end if;
 if entity_status is null or entity_status='revoked' then raise exception 'unknown or effective revoked entity' using errcode='23514'; end if;
 insert into public.generated_revocation_request_20261007 values(p_request,p_tenant,p_type,p_key,p_epoch,p_evidence,'pending',null,clock_timestamp());
 insert into public.generated_send_audit_20261007(tenant_id,event,identity_token,payload) values(p_tenant,'revoke_requested',p_request,p_evidence);
 return jsonb_build_object('request_id',p_request,'state','pending','effective_epoch',null);
end; $$;

create function public.generated_revoke_drain_20261007(p_tenant text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.generated_revocation_request_20261007%rowtype; e bigint;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(p_tenant));
 if exists(select 1 from public.generated_send_lease_20261007 where tenant_id=p_tenant and state in ('reserved','inflight','unknown')) then
  return jsonb_build_object('tenant_id',p_tenant,'state','pending'); end if;
 for q in select * from public.generated_revocation_request_20261007 where tenant_id=p_tenant and state='pending' order by created_at,request_id for update loop
  select epoch into e from public.generated_tenant_epoch_20261007 where tenant_id=p_tenant for update;
  e:=public.generated_authority_pre_lease_write_20261007(p_tenant,e,jsonb_build_array(
   case q.entity_type when 'source' then jsonb_build_object('op','source_tombstone','source_id',q.entity_key)
   else jsonb_build_object('op','palette_revoke','palette_key',q.entity_key) end));
  update public.generated_revocation_request_20261007 set state='effective',effective_epoch=e where request_id=q.request_id;
  insert into public.generated_send_audit_20261007(tenant_id,event,identity_token,payload) values(p_tenant,'revoke_effective',q.request_id,jsonb_build_object('epoch',e));
 end loop;
 return jsonb_build_object('tenant_id',p_tenant,'state','effective');
end; $$;

create function public.generated_send_outcome_20261007(p_attempt uuid,p_outcome text,p_evidence jsonb,p_reconcile boolean default false)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare l public.generated_send_lease_20261007%rowtype;
 actor_role text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 if p_outcome is null or p_outcome not in ('unknown','sent','not_sent')
  or jsonb_typeof(p_evidence) is distinct from 'object' or jsonb_typeof(p_evidence->'receipt_ref') is distinct from 'string'
  or jsonb_typeof(p_evidence->'actor') is distinct from 'string'
  or coalesce(p_evidence->>'receipt_ref','') !~ '[^[:space:]]'
  or coalesce(p_evidence->>'actor','') !~ '[^[:space:]]' then
  raise exception 'trusted outcome evidence required' using errcode='23514'; end if;
 select * into l from public.generated_send_lease_20261007 where attempt_token=p_attempt;
 if not found then raise exception 'unknown generated attempt' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(l.tenant_id));
 select * into l from public.generated_send_lease_20261007 where attempt_token=p_attempt for update;
 if l.state in ('sent','not_sent') then
  if l.state is distinct from p_outcome or l.terminal_evidence is distinct from p_evidence then
   raise exception 'terminal attempt evidence immutable' using errcode='23514'; end if;
  return to_jsonb(l)||jsonb_build_object('authorize_send',false);
 end if;
 if p_reconcile is null or (l.state='unknown' or p_reconcile) and
  (p_reconcile is distinct from true or not pg_has_role(actor_role,'generated_send_reconciler_20261007','member')) then
  raise exception 'explicit independent outcome reconciliation required' using errcode='42501'; end if;
 if l.state='reserved' and p_outcome<>'not_sent' then raise exception 'unstarted attempt can only cancel without send' using errcode='23514'; end if;
 update public.generated_send_lease_20261007 set state=p_outcome,
  terminal_evidence=case when p_outcome='unknown' then null else p_evidence end,updated_at=clock_timestamp() where attempt_token=p_attempt;
 insert into public.generated_send_audit_20261007(tenant_id,event,identity_token,payload) values(l.tenant_id,'outcome_'||p_outcome,p_attempt,p_evidence);
 if p_outcome<>'unknown' then perform public.generated_revoke_drain_20261007(l.tenant_id); end if;
 return jsonb_build_object('attempt_token',p_attempt,'state',p_outcome,'authorize_send',false);
end; $$;

-- Keep the calendar identity frozen through a durable outstanding attempt.
-- Try-lock fails closed instead of reversing canonical -> row lock order.
create function public.generated_send_calendar_fence_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 if old.source_media_asset_id like 'generated-astra:%' then
  if not pg_try_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(old.gym_id)) then
   raise exception 'generated authority decision busy; calendar mutation blocked' using errcode='55000'; end if;
  if exists(select 1 from public.generated_send_lease_20261007 where calendar_row_id=old.id and state in ('reserved','inflight','unknown')) then
   raise exception 'outstanding generated attempt freezes calendar identity' using errcode='55000'; end if;
 end if;
 if tg_op='DELETE' then return old; end if;
 return new;
end; $$;
revoke all on function public.generated_send_calendar_fence_20261007() from public,anon,authenticated,service_role,generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007;
create trigger generated_send_calendar_fence before update or delete on public.content_calendar
 for each row execute function public.generated_send_calendar_fence_20261007();

create function public.generated_send_calendar_truncate_fence_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated send protocol requires read committed' using errcode='25000'; end if;
 if not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0)) then
  raise exception 'generated send decision busy; calendar truncate blocked' using errcode='55000'; end if;
 if exists(select 1 from public.generated_send_lease_20261007 where state in ('reserved','inflight','unknown')) then
  raise exception 'outstanding generated attempt freezes calendar identity' using errcode='55000'; end if;
 return null;
end; $$;
revoke all on function public.generated_send_calendar_truncate_fence_20261007() from public,anon,authenticated,service_role,generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007;
create trigger generated_send_calendar_truncate_fence before truncate on public.content_calendar
 for each statement execute function public.generated_send_calendar_truncate_fence_20261007();

create function public.generated_send_status_20261007(p_tenant text)
returns jsonb language sql stable security definer set search_path=pg_catalog,public as $$
 select jsonb_build_object('tenant_id',p_tenant,
 'leases',(select coalesce(jsonb_agg(to_jsonb(l) order by created_at,attempt_token),'[]') from public.generated_send_lease_20261007 l where tenant_id=p_tenant),
 'revocations',(select coalesce(jsonb_agg(to_jsonb(q) order by created_at,request_id),'[]') from public.generated_revocation_request_20261007 q where tenant_id=p_tenant));
$$;
do $$ declare f text; begin
 foreach f in array array['generated_authority_write_20261007(text,bigint,jsonb)',
 'generated_send_acquire_20261007(uuid,text,uuid,uuid,uuid,jsonb)','generated_send_begin_20261007(uuid)',
 'generated_revoke_request_20261007(uuid,text,bigint,text,text,jsonb)','generated_revoke_drain_20261007(text)',
 'generated_send_outcome_20261007(uuid,text,jsonb,boolean)','generated_send_status_20261007(text)'] loop
  execute format('revoke all on function public.%s from public,anon,authenticated,service_role,generated_authority_owner_20261007,generated_authority_publisher_20261007,generated_send_reconciler_20261007',f);
  execute format('grant execute on function public.%s to service_role',f);
 end loop;
end; $$;
grant usage on schema public to generated_send_reconciler_20261007;
grant execute on function public.generated_send_outcome_20261007(uuid,text,jsonb,boolean),public.generated_send_status_20261007(text)
 to generated_send_reconciler_20261007;
commit;
