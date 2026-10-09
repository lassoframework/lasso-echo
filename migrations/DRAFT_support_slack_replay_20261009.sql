-- DRAFT / UNAPPLIED. Apply only through the portal's normal migration/release gate,
-- before deploying mandatory atomic Slack intake, whether
-- SUPPORT_MESSAGES_FENCE_ENABLED is false or true. No historical backfill/replay.
-- service_role only. Inbound capture remains available while the local sender pauses.
-- Capture and commit lock the ticket before queue/message rows, preserving the
-- support request-cycle trigger's lock order. External sends NEVER occur here.
begin;

create table public.support_slack_replay (
  id uuid primary key default gen_random_uuid(),
  ticket_id uuid not null references public.support_tickets(id),
  inbound_id uuid not null references public.support_messages(id),
  event_key text not null unique check (length(event_key)>0),
  bot_identity text not null check (length(bot_identity)>0),
  context jsonb not null check (jsonb_typeof(context)='object'),
  ticket_snapshot jsonb not null,
  messages_snapshot jsonb not null,
  state text not null default 'pending' check (state in ('pending','planning','committed','held')),
  claim_token uuid,
  claim_until timestamptz,
  reason text,
  committed_plan jsonb,
  committed_ticket jsonb,
  delivery_ticket_snapshot jsonb,
  delivery_messages_snapshot jsonb,
  resolution_receipts jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default clock_timestamp(),
  committed_at timestamptz,
  check ((state='planning') = (claim_until is not null)),
  check (state not in ('planning','committed') or claim_token is not null)
);
alter table public.support_slack_replay enable row level security;
revoke all on public.support_slack_replay from public, anon, authenticated;
grant select on public.support_slack_replay to service_role;
create index support_slack_replay_pending on public.support_slack_replay
  (bot_identity,state,created_at,id);

create function public.support_slack_replay_messages(p_ticket_id uuid)
returns jsonb language sql stable security definer set search_path=pg_catalog,public as $$
  select coalesce(jsonb_agg(to_jsonb(m) order by m.created_at,m.id),'[]'::jsonb)
  from public.support_messages m where m.ticket_id=p_ticket_id;
$$;

create function public.support_slack_replay_safety(p_ticket jsonb)
returns boolean language sql stable security definer set search_path=pg_catalog,public as $$
  -- Production support_tickets has no STOP/DND/takeover booleans. Authority
  -- is a verified human inbound and its immutable replay context, not imagined
  -- columns or an unavailable external SMS/provider preference.
  select exists (select 1 from public.support_slack_replay q
      where q.bot_identity=p_ticket->>'bot_identity'
        and q.context->'event'->>'channel'=p_ticket->>'slack_channel_id'
        and (q.context->'who'->>'slack_user_id'=p_ticket->>'slack_user_id'
          or q.ticket_id=(p_ticket->>'id')::uuid
            and q.context->'who'->>'kind' in ('staff','coach')
            and q.context->>'safety_hold'='human_takeover')
        and q.ticket_snapshot->'client_id'=p_ticket->'client_id'
        and q.ticket_snapshot->>'source'=p_ticket->>'source'
        and q.context->>'safety_hold' in ('STOP','human_takeover'))
    or exists (select 1 from public.support_messages m
      join public.support_tickets prior on prior.id=m.ticket_id
      where prior.bot_identity=p_ticket->>'bot_identity'
        and prior.slack_channel_id=p_ticket->>'slack_channel_id'
        and to_jsonb(prior)->'client_id'=p_ticket->'client_id'
        and prior.source=p_ticket->>'source' and m.direction='inbound'
        and (m.author_id=p_ticket->>'slack_user_id'
          or m.ticket_id=(p_ticket->>'id')::uuid and m.author_type in ('staff','blake','coach'))
        and (m.body ~* '^\s*(stop|unsubscribe|dnd|do not disturb|do not (contact|message|reply to) me|don''t (contact|message|reply to) me)[.!\s]*$'
             or m.body ~* '(speak|talk) (to|with) (a |an )?(human|person)|human takeover|let (me|us|a human|the team) handle'));
$$;

create function public.support_slack_replay_capture(
  p_ticket_id uuid,p_event_key text,p_identity text,p_context jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare t public.support_tickets%rowtype; q public.support_slack_replay%rowtype;
  mid uuid; who jsonb; ev jsonb; first_context jsonb; combined text;
  prior_ticket jsonb; prior_messages jsonb; renewed_ids uuid[]; prior public.support_slack_replay%rowtype;
begin
  select * into strict t from public.support_tickets where id=p_ticket_id for update;
  who:=p_context->'who'; ev:=p_context->'event';
  if p_context->>'contract' is distinct from 'support-slack-replay-v1'
     or t.source is distinct from 'slack_conversation'
     or t.bot_identity is distinct from p_identity
     or p_context->>'identity' is distinct from p_identity
     or p_context->>'product' is distinct from t.product
     or p_context->>'event_key' is distinct from p_event_key
     or p_event_key is distinct from concat(ev->>'channel',':',ev->>'ts')
     -- A capped user may reuse an owned ticket from another channel. Preserve
     -- that inbound without allowing a reply to the ticket's original channel.
     or (ev->>'channel' is distinct from t.slack_channel_id
         and not (p_context->>'rate_limited'='true'
                  and who->>'kind' in ('client','unknown')))
     or ev->>'user' is distinct from who->>'slack_user_id'
     or coalesce(ev->>'user','')=''
     or coalesce(ev->>'channel','')=''
     or coalesce(ev->>'ts','')=''
     or coalesce(who->>'kind','') not in ('client','staff','coach','unknown')
     or jsonb_typeof(who) is distinct from 'object'
     or jsonb_typeof(ev) is distinct from 'object'
     or coalesce(p_context->>'text','')=''
     or t.request_version is null
     or (ev->>'user' is distinct from t.slack_user_id and who->>'kind' not in ('staff','coach'))
     or (who->>'kind'='client' and (who->>'gym_id') is distinct from t.client_id::text)
  then raise exception 'Slack replay capture identity/source mismatch'; end if;
  if p_context->>'chatter'='true' and (length(p_context->>'text')>60
     or not (p_context->>'text' ~* '^\s*(hey|hi|hello|yo|thanks|thank you|thx|ty|ok|okay|k|got it|sounds good|great|perfect|awesome|cool|nice|yep|yes|sure|np|no problem|lol|haha|👍|🙏|✅|got it[,\s]+(thanks|thank you))[\s!.,]*$'))
  then raise exception 'Slack replay substantive message is not benign chatter'; end if;
  select * into q from public.support_slack_replay where event_key=p_event_key;
  if found then
    -- message and app_mention envelopes have distinct raw Slack IDs. Their
    -- immutable human-message fields must agree; the first exact envelope stays.
    if q.ticket_id<>p_ticket_id or q.bot_identity<>p_identity
       or q.context->'event'->'user' is distinct from ev->'user'
       or q.context->'event'->'channel' is distinct from ev->'channel'
       or q.context->'event'->'ts' is distinct from ev->'ts'
       or q.context->'event'->'text' is distinct from ev->'text'
       or q.context->'event'->'files' is distinct from ev->'files'
    then raise exception 'Slack replay event collision'; end if;
    return jsonb_build_object('item',to_jsonb(q),'duplicate',true);
  end if;
  -- Capture never adopts a historical inbound without a matching durable plan.
  if exists(select 1 from public.support_messages where slack_event_id=p_event_key)
  then raise exception 'Slack replay historical event requires reconciliation'; end if;
  p_context:=p_context||jsonb_build_object('dispatch',jsonb_build_object(
    'classification',p_context->'classification','request_type',p_context->'request_type',
    'text',p_context->>'text','created',p_context->'created',
    'who',who,'event',ev,'surface',p_context->'surface',
    'unknown_in_channel',p_context->'unknown_in_channel','rate_limited',p_context->'rate_limited',
    'noop',coalesce((p_context->>'chatter')::boolean,false)));
  -- A capped message from another channel advances the ticket snapshot but
  -- must neither erase an uncommitted request from the ticket's own channel
  -- nor disclose this other channel's text in a reply there. Carry only the
  -- original dispatch; the new inbound remains durable on the ticket.
  if p_context->>'rate_limited'='true' and ev->>'channel' is distinct from t.slack_channel_id then
    select context into first_context from public.support_slack_replay
      where ticket_id=p_ticket_id and bot_identity=p_identity and state<>'committed'
        and context->'dispatch'->'event'->>'channel'=t.slack_channel_id
        and context->'dispatch'->'who'->>'slack_user_id'=t.slack_user_id
      order by created_at desc,id desc limit 1;
    if found then
      p_context:=p_context||jsonb_build_object('dispatch',first_context->'dispatch');
    end if;
  end if;
  -- A pause can collect multiple messages before the initial request was ever
  -- routed. Only the newest snapshot may commit. Compose its source transcript
  -- once, preserving every original event separately; do not dispatch stale
  -- initial prompts, or turn its newest follow-up into an inert `new` ticket.
  if p_context->>'classification'='follow_up'
     and ev->>'channel'=t.slack_channel_id
     and not exists(select 1 from public.support_slack_replay where ticket_id=p_ticket_id and state='committed')
  then
    select context into first_context from public.support_slack_replay
      where ticket_id=p_ticket_id and bot_identity=p_identity
        and context->>'created'='true'
        and ticket_snapshot->'client_id'=to_jsonb(t)->'client_id'
        and ticket_snapshot->>'source'=t.source
        and ticket_snapshot->>'product'=t.product
        and ticket_snapshot->>'slack_user_id'=t.slack_user_id
        and ticket_snapshot->>'slack_channel_id'=t.slack_channel_id
        and ticket_snapshot->>'slack_thread_ts'=t.slack_thread_ts
        -- A staff note can advance the latest snapshot without becoming the
        -- origin of the undispatched client request or granting staff gates.
        and context->'who'->>'slack_user_id'=t.slack_user_id
        and context->'who'->>'kind'=t.identity_kind
      order by created_at,id limit 1;
    if found then
      select string_agg(context->>'text',E'\n\n' order by created_at,id) into combined
        from public.support_slack_replay where ticket_id=p_ticket_id and bot_identity=p_identity
          and context->'event'->>'channel'=t.slack_channel_id;
      p_context:=p_context||jsonb_build_object('dispatch',jsonb_build_object(
        'classification',first_context->'classification','request_type',first_context->'request_type',
        'who',first_context->'who','event',first_context->'event','surface',first_context->'surface',
        'unknown_in_channel',first_context->'unknown_in_channel',
        'rate_limited',first_context->'rate_limited',
        'created',true,'text',combined||E'\n\n'||(p_context->>'text'),'noop',false));
    end if;
  end if;
  prior_ticket:=to_jsonb(t);
  prior_messages:=public.support_slack_replay_messages(p_ticket_id);
  insert into public.support_messages(ticket_id,author_type,author_id,body,attachments,
    direction,slack_event_id,slack_ts)
    values(p_ticket_id,case who->>'kind' when 'staff' then 'staff' when 'coach' then 'coach'
      else 'client' end,ev->>'user',left(p_context->>'text',8000),
      jsonb_build_object('surface',p_context->>'surface',
        'identity_reason',who->>'reason','raw_event_id',coalesce(ev->>'_raw_event_id',''),
        'chatter',coalesce((p_context->>'chatter')::boolean,false),
        'slack_replay_contract','support-slack-replay-v1'),
      'inbound',p_event_key,ev->>'ts') returning id into mid;
  -- Request-version triggers have completed before freezing the snapshot.
  select * into strict t from public.support_tickets where id=p_ticket_id;
  insert into public.support_slack_replay(ticket_id,inbound_id,event_key,bot_identity,
    context,ticket_snapshot,messages_snapshot)
    values(p_ticket_id,mid,p_event_key,p_identity,p_context,to_jsonb(t),
      public.support_slack_replay_messages(p_ticket_id)) returning * into q;
  -- A benign note may advance the request-cycle trigger. Renew delivery
  -- authority only for exact-body never-attempted rows, preserving each row's
  -- ID, original plan, approval hold, and immutable commit snapshots. A claim,
  -- timestamp, uncertainty, safety hold or changed prior identity prevents it.
  if p_context->'dispatch'->>'noop'='true'
     and not public.support_slack_replay_safety(to_jsonb(t)) then
    for prior in select * from public.support_slack_replay old
      where old.ticket_id=p_ticket_id and old.bot_identity=p_identity and old.state='committed'
        and coalesce(old.delivery_ticket_snapshot,old.committed_ticket)=prior_ticket
        and not exists(select 1 from jsonb_each(old.context->'who') kv
          where kv.key in ('kind','slack_user_id','email','account_key','gym_id')
            and who->kv.key is distinct from kv.value)
        and (select coalesce(jsonb_agg(value order by value->>'created_at',value->>'id'),'[]'::jsonb)
          from jsonb_array_elements(coalesce(old.delivery_messages_snapshot,old.messages_snapshot))
          where value->>'direction'='inbound')
          = (select coalesce(jsonb_agg(value order by value->>'created_at',value->>'id'),'[]'::jsonb)
            from jsonb_array_elements(prior_messages) where value->>'direction'='inbound')
    loop
      with renewed as (
        update public.support_messages m set delivery_request_version=t.request_version,
          attachments=m.attachments||jsonb_build_object('request_version',t.request_version,
            'slack_replay_chatter_authority',q.id)
          where m.ticket_id=p_ticket_id and m.direction='outbound'
            and m.attachments->>'slack_replay_id'=prior.id::text
            and m.delivery_status in ('ready','held') and m.slack_ts is null
            and not (m.attachments ?| array['claimed_at','slack_replay_delivery_token',
              'slack_replay_delivery_uncertain','fixer_slack_delivery_intent',
              'fixer_slack_delivery_uncertain','outreach_delivery_uncertain'])
            and m.delivery_request_version=(prior_ticket->>'request_version')::bigint
            and exists(select 1 from jsonb_array_elements(prior.committed_plan->'rows') r
              where r->>'id'=m.id::text and r->>'body'=m.body
                and r->>'author_type'=m.author_type
                and not exists(select 1 from jsonb_each(r->'attachments') kv
                  where m.attachments->kv.key is distinct from kv.value))
          returning id)
      select array_agg(id) into renewed_ids from renewed;
      if renewed_ids is not null then
        update public.support_slack_replay set delivery_ticket_snapshot=to_jsonb(t),
          delivery_messages_snapshot=public.support_slack_replay_messages(p_ticket_id)
          where id=prior.id;
      end if;
    end loop;
    -- Freeze the no-op's complete current message snapshot after renewal.
    update public.support_slack_replay set messages_snapshot=public.support_slack_replay_messages(p_ticket_id)
      where id=q.id returning * into q;
  end if;
  return jsonb_build_object('item',to_jsonb(q),'duplicate',false);
end $$;

create function public.support_slack_replay_claim(p_id uuid,p_identity text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.support_slack_replay%rowtype; t public.support_tickets%rowtype;
  tid uuid; why text;
begin
  select ticket_id into strict tid from public.support_slack_replay where id=p_id;
  select * into strict t from public.support_tickets where id=tid for update;
  select * into strict q from public.support_slack_replay where id=p_id for update;
  if q.bot_identity is distinct from p_identity or p_token is null
  then raise exception 'Slack replay claim identity mismatch'; end if;
  if q.state in ('held','committed') then
    return jsonb_build_object('state',q.state,'reason',q.reason,
      'decision',q.committed_plan->'decision');
  end if;
  if q.state='planning' and q.claim_until>clock_timestamp() then
    if q.claim_token=p_token then return jsonb_build_object('state','planning','item',to_jsonb(q));
    else return jsonb_build_object('state','busy'); end if;
  end if;
  if to_jsonb(t) is distinct from q.ticket_snapshot then why:='ticket_snapshot_changed';
  elsif public.support_slack_replay_messages(tid) is distinct from q.messages_snapshot
    then why:='messages_snapshot_changed';
  elsif public.support_slack_replay_safety(to_jsonb(t)) then why:='STOP_DND_or_human_takeover';
  end if;
  if why is not null then
    update public.support_slack_replay set state='held',reason=why,claim_until=null
      where id=p_id;
    return jsonb_build_object('state','held','reason',why);
  end if;
  update public.support_slack_replay set state='planning',claim_token=p_token,
    claim_until=clock_timestamp()+interval '5 minutes',reason=null where id=p_id
    returning * into q;
  return jsonb_build_object('state','planning','item',to_jsonb(q));
end $$;

create function public.support_slack_replay_hold(p_id uuid,p_identity text,p_token uuid,p_reason text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.support_slack_replay%rowtype;
begin
  select * into strict q from public.support_slack_replay where id=p_id for update;
  if q.bot_identity is distinct from p_identity or q.claim_token is distinct from p_token
  then raise exception 'Slack replay hold ownership mismatch'; end if;
  if q.state='planning' then
    update public.support_slack_replay set state='held',reason=left(p_reason,200),claim_until=null
      where id=p_id returning * into q;
  end if;
  return jsonb_build_object('state',q.state,'reason',q.reason);
end $$;

create function public.support_slack_replay_commit(p_id uuid,p_identity text,p_token uuid,p_plan jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.support_slack_replay%rowtype; t public.support_tickets%rowtype;
  nt public.support_tickets%rowtype; tid uuid; row jsonb; fields jsonb; why text;
begin
  select ticket_id into strict tid from public.support_slack_replay where id=p_id;
  select * into strict t from public.support_tickets where id=tid for update;
  select * into strict q from public.support_slack_replay where id=p_id for update;
  if q.bot_identity is distinct from p_identity or q.claim_token is distinct from p_token
     or p_token is null then raise exception 'Slack replay commit ownership mismatch'; end if;
  if q.state='committed' then
    if q.committed_plan is distinct from p_plan then raise exception 'Slack replay changed committed plan'; end if;
    return jsonb_build_object('state','committed','decision',q.committed_plan->'decision');
  end if;
  if q.state<>'planning' or q.claim_until<=clock_timestamp() then
    return jsonb_build_object('state',q.state,'reason',coalesce(q.reason,'claim_expired'));
  end if;
  if to_jsonb(t) is distinct from q.ticket_snapshot then why:='ticket_snapshot_changed';
  elsif public.support_slack_replay_messages(tid) is distinct from q.messages_snapshot
    then why:='messages_snapshot_changed';
  elsif public.support_slack_replay_safety(to_jsonb(t)) then why:='STOP_DND_or_human_takeover';
  end if;
  if why is not null then
    update public.support_slack_replay set state='held',reason=why,claim_until=null where id=p_id;
    return jsonb_build_object('state','held','reason',why);
  end if;
  fields:=p_plan->'ticket_fields';
  if jsonb_typeof(fields) is distinct from 'object'
     or jsonb_typeof(p_plan->'rows') is distinct from 'array'
     or jsonb_typeof(p_plan->'decision') is distinct from 'object'
     or p_plan->'decision'->>'ticket_id' is distinct from tid::text
     or p_plan->'decision'->>'identity_kind' is distinct from q.context->'dispatch'->'who'->>'kind'
     or jsonb_array_length(p_plan->'rows')>20
     or exists(select 1 from jsonb_object_keys(fields) k where k not in
       ('status','classification','lane','hold_tier','request_type','verification_before',
        'verification_after','escalated'))
  then raise exception 'Slack replay invalid plan'; end if;
  select * into nt from jsonb_populate_record(null::public.support_tickets,to_jsonb(t)||fields);
  update public.support_tickets set status=nt.status,classification=nt.classification,lane=nt.lane,
    hold_tier=nt.hold_tier,request_type=nt.request_type,verification_before=nt.verification_before,
    verification_after=nt.verification_after,escalated=nt.escalated where id=tid;
  select * into strict t from public.support_tickets where id=tid;
  for row in select value from jsonb_array_elements(p_plan->'rows') loop
    if row->>'ticket_id' is distinct from tid::text
       or row->>'direction' is distinct from 'outbound'
       or coalesce(row->>'author_type','') not in ('system',p_identity)
       or coalesce(row->>'delivery_status','') not in ('ready','held')
       or row->'attachments'->>'identity' is distinct from p_identity
       or row->'attachments'->>'surface' is distinct from q.context->'dispatch'->>'surface'
       or row->'attachments'->>'recipient_kind' is distinct from q.context->'dispatch'->'who'->>'kind'
       or coalesce(row->'attachments'->>'kind','') not in ('ack','answer','template','status','escalation','fixer_request','hold_notice')
       or coalesce(row->>'body','')=''
    then raise exception 'Slack replay invalid outbound'; end if;
    insert into public.support_messages(id,ticket_id,direction,author_type,author_id,body,
      attachments,delivery_status,delivery_request_version)
      values((row->>'id')::uuid,tid,'outbound',row->>'author_type',null,left(row->>'body',8000),
        (row->'attachments')||jsonb_build_object('slack_replay_id',p_id,
          'slack_replay_contract','support-slack-replay-v1','request_version',t.request_version),
        row->>'delivery_status',t.request_version);
  end loop;
  update public.support_slack_replay set state='committed',committed_plan=p_plan,
    committed_ticket=to_jsonb(t),committed_at=clock_timestamp(),claim_until=null where id=p_id;
  return jsonb_build_object('state','committed','decision',p_plan->'decision');
end $$;

create function public.support_slack_replay_delivery_current(
  p_message_id uuid,p_identity text,p_posted boolean)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare m public.support_messages%rowtype; q public.support_slack_replay%rowtype;
  t public.support_tickets%rowtype;
  delivery_ticket jsonb; delivery_messages jsonb;
begin
  select * into m from public.support_messages where id=p_message_id;
  if not found then return jsonb_build_object('allowed',false); end if;
  select * into q from public.support_slack_replay
    where id=(m.attachments->>'slack_replay_id')::uuid;
  delivery_ticket:=coalesce(q.delivery_ticket_snapshot,q.committed_ticket);
  delivery_messages:=coalesce(q.delivery_messages_snapshot,q.messages_snapshot);
  if not found or q.state<>'committed' or q.bot_identity is distinct from p_identity
    or m.ticket_id<>q.ticket_id or m.direction<>'outbound'
    or (p_posted and m.delivery_status<>'posted')
    or (not p_posted and m.delivery_status not in ('ready','posting'))
    or coalesce((m.attachments->>'slack_replay_delivery_uncertain')::boolean,false)
    or m.attachments->>'identity' is distinct from p_identity
    or m.attachments->>'slack_replay_contract' is distinct from 'support-slack-replay-v1'
    or m.delivery_request_version is distinct from (delivery_ticket->>'request_version')::bigint
    or not exists(select 1 from jsonb_array_elements(q.committed_plan->'rows') r
      where r->>'id'=m.id::text and r->>'body'=m.body
        and r->>'author_type'=m.author_type
        and not exists(select 1 from jsonb_each(r->'attachments') kv
          where m.attachments->kv.key is distinct from kv.value))
  then return jsonb_build_object('allowed',false); end if;
  select * into t from public.support_tickets where id=q.ticket_id;
  return jsonb_build_object('allowed',found
    and t.source='slack_conversation' and t.bot_identity=p_identity
    and not public.support_slack_replay_safety(to_jsonb(t))
    -- Staff notes on some Slack surfaces do not advance request_version in
    -- the existing trigger. Bind the complete inbound snapshot as well.
    and (select coalesce(jsonb_agg(to_jsonb(mi) order by mi.created_at,mi.id),'[]'::jsonb)
      from public.support_messages mi where mi.ticket_id=q.ticket_id and mi.direction='inbound')
      = (select coalesce(jsonb_agg(value order by value->>'created_at',value->>'id'),'[]'::jsonb)
        from jsonb_array_elements(delivery_messages) where value->>'direction'='inbound')
    and not exists(select 1 from jsonb_each(delivery_ticket) kv
      where kv.key in ('id','client_id','product','source','bot_identity','slack_user_id',
        'slack_channel_id','slack_thread_ts','request_version','classification',
        'status','hold_tier','escalated','verification_before','verification_after')
        -- A human release may advance a held ticket; its original body,
        -- request identity and normal outbox approval gates still apply.
        and not (kv.key in ('status','hold_tier','escalated')
          and coalesce(m.attachments->>'released_by','')<>''
          and exists(select 1 from jsonb_array_elements(q.committed_plan->'rows') r
            where r->>'id'=m.id::text and r->>'delivery_status'='held'))
        and to_jsonb(t)->kv.key is distinct from kv.value));
end $$;

create function public.support_slack_replay_dispatch_allowed(p_message_id uuid,p_identity text)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$
  select public.support_slack_replay_delivery_current(p_message_id,p_identity,false);
$$;

create function public.support_slack_replay_delivery_claim(p_message_id uuid,p_identity text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare m public.support_messages%rowtype; tid uuid;
begin
  select ticket_id into strict tid from public.support_messages where id=p_message_id;
  perform 1 from public.support_tickets where id=tid for update;
  select * into strict m from public.support_messages where id=p_message_id for update;
  if p_token is null or m.attachments->>'identity' is distinct from p_identity
     or not (m.attachments ? 'slack_replay_id')
  then raise exception 'Slack replay delivery claim ownership mismatch'; end if;
  if m.delivery_status='posting' and m.attachments->>'slack_replay_delivery_token'=p_token::text
  then return jsonb_build_object('row',to_jsonb(m)); end if;
  if m.delivery_status<>'ready'
     or not coalesce((public.support_slack_replay_dispatch_allowed(p_message_id,p_identity)->>'allowed')::boolean,false)
  then return jsonb_build_object('row',null); end if;
  update public.support_messages set delivery_status='posting',attachments=attachments||
    jsonb_build_object('slack_replay_delivery_token',p_token,'claimed_at',clock_timestamp())
    where id=p_message_id returning * into m;
  return jsonb_build_object('row',to_jsonb(m));
end $$;

create function public.support_slack_replay_delivery_hold(
  p_message_id uuid,p_identity text,p_token uuid,p_reason text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare m public.support_messages%rowtype;
begin
  select * into strict m from public.support_messages where id=p_message_id for update;
  if m.attachments->>'identity' is distinct from p_identity
     or not (m.attachments ? 'slack_replay_id')
     or m.attachments->>'slack_replay_delivery_token' is distinct from p_token::text
  then raise exception 'Slack replay delivery hold ownership mismatch'; end if;
  if m.delivery_status='posting' then
    update public.support_messages set delivery_status='held',attachments=attachments||
      jsonb_build_object('slack_replay_delivery_uncertain',true,'held_why',left(p_reason,200))
      where id=p_message_id returning * into m;
  end if;
  return jsonb_build_object('row',to_jsonb(m));
end $$;

create function public.support_slack_replay_delivery_finish(
  p_message_id uuid,p_identity text,p_token uuid,p_ts text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare m public.support_messages%rowtype; tid uuid; allowed boolean;
begin
  select ticket_id into strict tid from public.support_messages where id=p_message_id;
  perform 1 from public.support_tickets where id=tid for update;
  select * into strict m from public.support_messages where id=p_message_id for update;
  if p_token is null or coalesce(p_ts,'') !~ '^[0-9]+\.[0-9]+$'
     or m.attachments->>'identity' is distinct from p_identity
     or not (m.attachments ? 'slack_replay_id')
     or m.attachments->>'slack_replay_delivery_token' is distinct from p_token::text
  then raise exception 'Slack replay delivery finish ownership mismatch'; end if;
  if m.delivery_status='posted' then
    if m.slack_ts is distinct from p_ts then raise exception 'Slack replay changed delivery receipt'; end if;
    return jsonb_build_object('row',to_jsonb(m));
  end if;
  if m.attachments->>'slack_replay_provider_ts' is not null
     and m.attachments->>'slack_replay_provider_ts' is distinct from p_ts
  then raise exception 'Slack replay changed quarantined provider receipt'; end if;
  allowed:=coalesce((public.support_slack_replay_dispatch_allowed(p_message_id,p_identity)->>'allowed')::boolean,false);
  if m.delivery_status='posting' and allowed then
    update public.support_messages set delivery_status='posted',slack_ts=p_ts
      where id=p_message_id returning * into m;
  elsif m.delivery_status in ('posting','held') then
    -- Recovery may have won while the provider was still in flight. Retain
    -- the late response reference, never convert a quarantine into delivery.
    update public.support_messages set delivery_status='held',attachments=attachments||
      jsonb_build_object('slack_replay_delivery_uncertain',true,
        'slack_replay_provider_ts',p_ts,'held_why','Replay completion requires independent reconciliation')
      where id=p_message_id returning * into m;
  end if;
  return jsonb_build_object('row',to_jsonb(m));
end $$;

create function public.support_slack_replay_delivery_resolve(
  p_message_id uuid,p_identity text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare m public.support_messages%rowtype; t public.support_tickets%rowtype;
  q public.support_slack_replay%rowtype; tid uuid; inbound jsonb; proof jsonb;
begin
  select ticket_id into strict tid from public.support_messages where id=p_message_id;
  select * into strict t from public.support_tickets where id=tid for update;
  select * into strict m from public.support_messages where id=p_message_id for update;
  if p_token is null or m.attachments->>'identity' is distinct from p_identity
     or not (m.attachments ? 'slack_replay_id')
     or m.attachments->>'slack_replay_delivery_token' is distinct from p_token::text
  then raise exception 'Slack replay resolution ownership mismatch'; end if;
  select * into strict q from public.support_slack_replay
    where id=(m.attachments->>'slack_replay_id')::uuid for update;
  if q.state is distinct from 'committed' or q.bot_identity is distinct from p_identity
     or q.ticket_id<>tid then raise exception 'Slack replay resolution source mismatch'; end if;
  proof:=q.resolution_receipts->p_message_id::text;
  select coalesce(jsonb_agg(to_jsonb(mi) order by mi.created_at,mi.id),'[]'::jsonb)
    into inbound from public.support_messages mi where mi.ticket_id=tid and mi.direction='inbound';
  if m.delivery_status='posted' and m.slack_ts is not null
     and not coalesce((m.attachments->>'slack_replay_delivery_uncertain')::boolean,false)
     and to_jsonb(t)=proof->'ticket' and inbound=proof->'inbound'
     and m.body=proof->>'body' and m.slack_ts=proof->>'slack_ts'
  then return jsonb_build_object('resolved',true,'ticket',to_jsonb(t)); end if;
  -- Only a current direct grounded answer may close. Holds, promised follow-up,
  -- code-fix notices and all ordinary rows retain their existing release gates.
  if t.status is distinct from 'verification'
     or t.classification is distinct from 'answerable_question'
     or t.escalated is distinct from false or t.hold_tier is not null
     or m.attachments->>'kind' is distinct from 'answer'
     or m.slack_ts is null
     or not coalesce((public.support_slack_replay_delivery_current(
          p_message_id,p_identity,true)->>'allowed')::boolean,false)
  then return jsonb_build_object('resolved',false); end if;
  update public.support_tickets set status='resolved',resolved_at=clock_timestamp()
    where id=tid returning * into t;
  -- Complete snapshots stay in the private queue, never in client-visible
  -- outbound attachment metadata.
  update public.support_slack_replay set resolution_receipts=resolution_receipts||
    jsonb_build_object(p_message_id::text,jsonb_build_object(
      'ticket',to_jsonb(t),'inbound',inbound,'body',m.body,'slack_ts',m.slack_ts))
    where id=q.id;
  return jsonb_build_object('resolved',true,'ticket',to_jsonb(t));
end $$;

revoke all on function public.support_slack_replay_messages(uuid) from public,anon,authenticated,service_role;
revoke all on function public.support_slack_replay_safety(jsonb) from public,anon,authenticated,service_role;
revoke all on function public.support_slack_replay_delivery_current(uuid,text,boolean) from public,anon,authenticated,service_role;
revoke all on function public.support_slack_replay_capture(uuid,text,text,jsonb) from public,anon,authenticated;
revoke all on function public.support_slack_replay_claim(uuid,text,uuid) from public,anon,authenticated;
revoke all on function public.support_slack_replay_hold(uuid,text,uuid,text) from public,anon,authenticated;
revoke all on function public.support_slack_replay_commit(uuid,text,uuid,jsonb) from public,anon,authenticated;
revoke all on function public.support_slack_replay_dispatch_allowed(uuid,text) from public,anon,authenticated;
grant execute on function public.support_slack_replay_capture(uuid,text,text,jsonb) to service_role;
grant execute on function public.support_slack_replay_claim(uuid,text,uuid) to service_role;
grant execute on function public.support_slack_replay_hold(uuid,text,uuid,text) to service_role;
grant execute on function public.support_slack_replay_commit(uuid,text,uuid,jsonb) to service_role;
grant execute on function public.support_slack_replay_dispatch_allowed(uuid,text) to service_role;
revoke all on function public.support_slack_replay_delivery_claim(uuid,text,uuid) from public,anon,authenticated;
revoke all on function public.support_slack_replay_delivery_hold(uuid,text,uuid,text) from public,anon,authenticated;
revoke all on function public.support_slack_replay_delivery_finish(uuid,text,uuid,text) from public,anon,authenticated;
grant execute on function public.support_slack_replay_delivery_claim(uuid,text,uuid) to service_role;
grant execute on function public.support_slack_replay_delivery_hold(uuid,text,uuid,text) to service_role;
grant execute on function public.support_slack_replay_delivery_finish(uuid,text,uuid,text) to service_role;
revoke all on function public.support_slack_replay_delivery_resolve(uuid,text,uuid) from public,anon,authenticated;
grant execute on function public.support_slack_replay_delivery_resolve(uuid,text,uuid) to service_role;
commit;
