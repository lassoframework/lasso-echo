-- DRAFT / UNAPPLIED / DEFAULT OFF. Requires GBP final authority draft.
-- COMMIT of this RPC is the irrevocable grant for ONE provider invocation using
-- the recorded creative and native destination. Later gate/connection changes
-- govern future attempts; they cannot revoke this already committed grant.
-- Lost response or crash holds the publishing lease for operator reconciliation;
-- retries of this RPC NEVER grant another invocation. No timeout releases it.
-- Privileged DB owners can disable triggers; this is not a superuser boundary.
begin;
do $$ begin
 if to_regprocedure('public.fixer_authorize_gbp_forward_send_20261008(uuid,uuid,jsonb,text,text,text)') is null
  or to_regclass('public.fixer_forward_media_claim_receipt_20261006') is null then
  raise exception 'GBP final authority and forward media claim drafts required'; end if;
end; $$;
create table public.fixer_gbp_provider_attempt_20261009 (
 claim_token uuid primary key,
 calendar_row_id uuid not null,
 creative jsonb not null check(jsonb_typeof(creative)='object'),
 native_account_id text not null,
 location_id text not null,
 send_kind text not null check(send_kind in ('post','gallery')),
 authorized_at timestamptz not null default clock_timestamp()
);
create trigger immutable_row before update or delete on public.fixer_gbp_provider_attempt_20261009
 for each row execute function public.fixer_forward_media_immutable_20261006();
create trigger immutable_truncate before truncate on public.fixer_gbp_provider_attempt_20261009
 for each statement execute function public.fixer_forward_media_immutable_20261006();
revoke all on public.fixer_gbp_provider_attempt_20261009 from public,anon,authenticated,
 service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;

-- Preserve the full existing authority checks as an internal function. Only the
-- wrapper grants send authority; direct replay of the internal check cannot send.
alter function public.fixer_authorize_gbp_forward_send_20261008(uuid,uuid,jsonb,text,text,text)
 rename to fixer_check_gbp_forward_send_20261009;
revoke all on function public.fixer_check_gbp_forward_send_20261009(uuid,uuid,jsonb,text,text,text)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
create function public.fixer_authorize_gbp_forward_send_20261008(
 p_calendar_row_id uuid,p_claim_token uuid,p_expected_creative jsonb,
 p_native_account_id text,p_expected_location_id text,p_send_kind text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 -- Existing checker obtains graph/calendar/destination locks through COMMIT.
 if public.fixer_check_gbp_forward_send_20261009(p_calendar_row_id,p_claim_token,
   p_expected_creative,p_native_account_id,p_expected_location_id,p_send_kind) is distinct from true then
  return false; end if;
 insert into public.fixer_gbp_provider_attempt_20261009
  (claim_token,calendar_row_id,creative,native_account_id,location_id,send_kind)
 values(p_claim_token,p_calendar_row_id,p_expected_creative,p_native_account_id,p_expected_location_id,p_send_kind)
 on conflict(claim_token) do nothing;
 -- A lost committed response may have sent. Exact replay is held, not success.
 if not found then return false; end if;
 -- Write a calendar tuple version under the existing row lock. A concurrent
 -- repeatable-read editor with an older snapshot then raises serialization
 -- failure instead of observing no attempt and bypassing the mutation guard.
 update public.content_calendar set status=status where id=p_calendar_row_id;
 return true;
end; $$;
revoke all on function public.fixer_authorize_gbp_forward_send_20261008(uuid,uuid,jsonb,text,text,text)
 from public,anon,authenticated,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_authorize_gbp_forward_send_20261008(uuid,uuid,jsonb,text,text,text) to service_role;

create function public.fixer_guard_gbp_provider_attempt_20261009()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare a public.fixer_gbp_provider_attempt_20261009%rowtype; expected jsonb;
begin
 select * into a from public.fixer_gbp_provider_attempt_20261009
  where calendar_row_id=old.id and claim_token=old.publish_claim_token;
 if not found then
  if tg_op='DELETE' then return old; end if;
  return new;
 end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'GBP attempt mutation guard requires read committed isolation' using errcode='25000'; end if;
 if tg_op='DELETE' then
  raise exception 'committed GBP provider attempt requires reconciliation' using errcode='23514'; end if;
 select jsonb_object_agg(key,value) into expected from jsonb_each(to_jsonb(new))
  where key in (select jsonb_object_keys(a.creative));
 if expected is distinct from a.creative
  or new.status not in ('publishing','published','failed') or new.status is null
  or (new.status='publishing' and (new.publish_claim_token is distinct from old.publish_claim_token
    or new.publish_reservation_day is distinct from old.publish_reservation_day))
  or (new.status in ('published','failed') and new.publish_claim_token is not null) then
  raise exception 'committed GBP provider attempt cannot be revoked or edited' using errcode='23514'; end if;
 -- Existing token-CAS provider terminal writes may settle success/definite failure.
 -- Ambiguous outcomes remain publishing; no automatic release is permitted.
 return new;
end; $$;
revoke all on function public.fixer_guard_gbp_provider_attempt_20261009() from public,anon,authenticated,service_role;
create trigger fixer_gbp_provider_attempt_guard_20261009 before update or delete on public.content_calendar
 for each row execute function public.fixer_guard_gbp_provider_attempt_20261009();
-- TRUNCATE would evade row guards and destroy unresolved leases.
create trigger fixer_gbp_provider_attempt_truncate_20261009 before truncate on public.content_calendar
 for each statement execute function public.fixer_forward_media_immutable_20261006();
commit;
