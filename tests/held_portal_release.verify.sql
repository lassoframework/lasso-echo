-- Disposable synthetic fixture only. Never a production receipt.
begin;
do $$
declare
  t public.support_tickets;
  proof uuid;
  notice uuid := gen_random_uuid();
  evidence jsonb;
  att jsonb;
  result public.support_tickets;
  rejected boolean := false;
begin
  insert into public.support_tickets(product,source,client_id,raw_text,classification,status,
    escalated,hold_tier,request_version,bot_identity,verification_before)
  values ('echo','website_tab','11111111-1111-1111-1111-111111111111','fixture','code_fix','merged',true,'routine',2,
    'echo','{"fixture_before":true}') returning * into t;
  evidence := jsonb_build_object('verified',true,'ticket_id',t.id::text,
    'request_version',2,'verifier_identity','independent-reviewer',
    'repair_author_identity','builder','repair_revision','fixture-sha',
    'review_receipt','https://example.invalid/review',
    'behavior_receipt','https://example.invalid/behavior');
  update public.support_tickets set verification_after=jsonb_build_object('fixer',
    jsonb_build_object('held_release_review',evidence)) where id=t.id returning * into t;
  proof := public.fixer_create_held_release_proof(t.id,2,notice,'Scoped repair completion',evidence);
  att := jsonb_build_object('kind','status','resolve_notice',true,
    'fixer_release',true,'fixer_release_proof_id',proof::text,'delivery_identity_fence',true,
    'delivery_expected_product',t.product,'delivery_expected_client_id',t.client_id,
    'delivery_expected_status',t.status,'delivery_expected_classification',t.classification,
    'delivery_expected_bot_identity',t.bot_identity,'delivery_expected_slack_user_id',t.slack_user_id,
    'fixer',true,'delivery_expected_slack_channel_id',null,'delivery_expected_slack_thread_ts',null,
    'identity','echo','recipient_kind','client','surface','held_portal_release','request_version',2);
  insert into public.app_users(id,clerk_user_id)
    values ('22222222-2222-2222-2222-222222222222','fixture-client');
  insert into public.gym_assignments(gym_id,app_user_id)
    values ('11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222');
  -- The new helper's exact shape stays invisible before its explicit delivery.
  insert into public.support_messages(id,ticket_id,author_type,author_id,body,direction,
    delivery_status,delivery_request_version,slack_ts,slack_event_id,attachments)
  values(notice,t.id,'ranger',null,'Scoped repair completion','outbound','held',2,null,null,att);
  set local role authenticated;
  if exists(select 1 from public.support_messages where id=notice) then
    raise exception 'client RLS exposed held release draft'; end if;
  reset role;
  select * into result from public.fixer_release_held_delivery(t.id,2,'merged','code_fix',
    'echo','11111111-1111-1111-1111-111111111111','echo',null,null,null,'routine',proof,notice);
  if result.id is not null then raise exception 'held unposted notice resolved ticket'; end if;
  update public.support_messages set delivery_status='posted' where id=notice
    and delivery_status='held' and ticket_id=t.id and delivery_request_version=2
    and attachments=att and body='Scoped repair completion';
  set local role authenticated;
  if not exists(select 1 from public.support_messages where id=notice and
      direction='outbound' and delivery_status='posted' and attachments->>'kind'='status') then
    raise exception 'route-null portal completion was not visible through client RLS';
  end if;
  reset role;
  select * into result from public.fixer_release_held_delivery(t.id,2,'merged','code_fix',
    'echo','11111111-1111-1111-1111-111111111111','echo',null,null,null,'routine',proof,notice);
  if result.id is null or result.status <> 'resolved' or result.escalated
      or result.hold_tier is not null then raise exception 'route-null held release CAS failed'; end if;
  if not exists(select 1 from public.fixer_release_proofs where id=proof and
      consumed_at is not null and consumed_message_id=notice) then
    raise exception 'proof not consumed by exact portal notice'; end if;
  begin
    update public.support_messages set attachments=attachments ||
      '{"fixer_delivery_finalized_at":"unneeded"}'::jsonb where id=notice;
  exception when others then rejected := true; end;
  if not rejected then raise exception 'posted release notice metadata was mutable'; end if;
end;
$$;
rollback;
