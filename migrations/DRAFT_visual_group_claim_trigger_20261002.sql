-- DRAFT / UNAPPLIED. Per-gym enforcement defaults OFF. Apply schema first.
-- Calendar + reservation changes commit in one transaction. Stable group rows
-- exist before a first claim and serialize absent-ledger races as well as swaps.
-- Rollback: disable gyms, drop content_calendar_visual_group_guard, RPCs and
-- helper functions, then sibling table; preserve permanent ledger history.

create table if not exists public.visual_group_usage_sibling (
  gym_id text not null, group_key text not null, calendar_row_id uuid not null,
  channel text, state text not null default 'active' check(state in ('active','released')),
  created_at timestamptz not null default now(), released_at timestamptz,
  ambiguous boolean not null default false,
  attempt_id uuid not null default gen_random_uuid(),
  original_claim_token uuid, original_provider_post_id text, original_image_url text,
  primary key(gym_id,group_key,calendar_row_id),
  foreign key(gym_id,group_key) references public.visual_group_usage_ledger(gym_id,group_key)
);
alter table public.visual_group_usage_sibling add column if not exists ambiguous boolean not null default false;
alter table public.visual_group_usage_sibling add column if not exists attempt_id uuid not null default gen_random_uuid();
alter table public.visual_group_usage_sibling add column if not exists original_claim_token uuid;
alter table public.visual_group_usage_sibling add column if not exists original_provider_post_id text;
alter table public.visual_group_usage_sibling add column if not exists original_image_url text;
alter table public.visual_group_usage_sibling enable row level security;
drop policy if exists service_role_all on public.visual_group_usage_sibling;
create policy service_role_all on public.visual_group_usage_sibling
  for all to service_role using(true) with check(true);
revoke all on public.visual_group_usage_sibling from public,anon,authenticated,service_role;
grant select,insert,update,delete on public.visual_group_usage_sibling to service_role;

-- Receipts bind exact attempt UUIDs, not merely a reused calendar row ID.
create or replace function public.visual_group_sibling_reconciled(p_s public.visual_group_usage_sibling)
returns boolean language sql stable security definer set search_path = public as $$
  select exists(select 1 from public.visual_group_reconciliation r
    where r.gym_id=p_s.gym_id and r.calendar_row_id=p_s.calendar_row_id
      and r.attempts @> jsonb_build_array(jsonb_build_object(
        'group_key',p_s.group_key,'attempt_id',p_s.attempt_id::text)));
$$;
create or replace function public.visual_group_group_reconciled(p_gym_id text,p_group_key text)
returns boolean language sql stable security definer set search_path = public as $$
  select exists(select 1 from public.visual_group_reconciliation r
      where r.gym_id=p_gym_id and p_group_key=any(r.reserved_groups))
    and not exists(select 1 from public.visual_group_usage_sibling s
      where s.gym_id=p_gym_id and s.group_key=p_group_key and s.ambiguous
        and not public.visual_group_sibling_reconciled(s));
$$;
-- Calendar transition exemption is scoped to the RPC's current transaction,
-- original claim snapshot and every current attempt. No user-set GUC bypass.
create or replace function public.visual_group_row_reconciled_here(p_row public.content_calendar)
returns boolean language sql stable security definer set search_path = public as $$
  select exists(select 1 from public.visual_group_reconciliation r
    where r.gym_id=p_row.gym_id and r.calendar_row_id=p_row.id and r.transaction_id=txid_current()
      and r.original_claim->>'publish_claim_token' is not distinct from p_row.publish_claim_token::text
      and r.original_claim->>'late_post_id' is not distinct from p_row.late_post_id
      and not exists(select 1 from public.visual_group_usage_sibling s
        where s.gym_id=p_row.gym_id and s.calendar_row_id=p_row.id and s.ambiguous
          and not public.visual_group_sibling_reconciled(s))
      and not exists(select 1 from public.visual_group_member_event e
        where e.gym_id=p_row.gym_id and e.alias_value=p_row.id::text and e.action='review_hold'
          and e.actor in ('backfill_ambiguous_review','runtime_ambiguous_review')
          and not exists(select 1 from public.visual_group_reconciliation covered
            where covered.gym_id=e.gym_id and e.id=any(covered.hold_event_ids))));
$$;

-- Ordinary writes cannot erase sticky uncertainty or free its membership.
-- A confirmed published group is permanently occupied, so retiring a sibling
-- after confirmation is safe while its ambiguity evidence stays retained.
create or replace function public.visual_group_sibling_keep_ambiguity()
returns trigger language plpgsql set search_path = public as $$
begin
  if old.ambiguous and tg_op='UPDATE' and
      (new.attempt_id<>old.attempt_id or
       (old.original_claim_token is not null and new.original_claim_token is distinct from old.original_claim_token) or
       (old.original_provider_post_id is not null and new.original_provider_post_id is distinct from old.original_provider_post_id) or
       (old.original_image_url is not null and new.original_image_url is distinct from old.original_image_url)) then
    raise exception 'original ambiguous attempt binding is immutable' using errcode='23514';
  end if;
  if old.ambiguous and (tg_op='DELETE' or
      ((not new.ambiguous or new.state='released') and
       not public.visual_group_sibling_reconciled(old) and
       not (new.ambiguous and new.state='released' and exists(
         select 1 from public.visual_group_usage_ledger l where l.gym_id=old.gym_id
           and l.group_key=old.group_key and l.state='published')))) then
    raise exception 'ambiguous sibling requires evidence-based reconciliation' using errcode='23514';
  end if;
  return coalesce(new,old);
end;
$$;
drop trigger if exists visual_group_sibling_sticky_ambiguity on public.visual_group_usage_sibling;
create trigger visual_group_sibling_sticky_ambiguity before update or delete
  on public.visual_group_usage_sibling for each row execute function public.visual_group_sibling_keep_ambiguity();

create or replace function public.visual_group_enforcement_on(p_gym_id text)
returns boolean language sql stable security definer set search_path = public as $$
  select coalesce((select enforce from public.gym_visual_guard_settings where gym_id=p_gym_id),false);
$$;

-- Exact identity only. Keep URL query strings: Drive id= queries identify
-- different files. No perceptual/pHash inference. Optional fields use row JSON.
create or replace function public.visual_group_row_aliases(p_row public.content_calendar)
returns table(alias_kind text,alias_value text)
language sql immutable set search_path = public as $$
  select distinct k, btrim(v) from (values
    ('canonical_url',p_row.image_url),
    ('canonical_url',to_jsonb(p_row)->>'source_media_url'),
    ('source_asset',to_jsonb(p_row)->>'source_media_asset_id'),
    ('drive_id',to_jsonb(p_row)->>'drive_file_id'),
    ('byte_hash',to_jsonb(p_row)->>'byte_hash'),
    ('r2_key',to_jsonb(p_row)->>'r2_key')
  ) a(k,v) where nullif(btrim(v),'') is not null;
$$;
create or replace function public.visual_group_resolve_row(p_row public.content_calendar)
returns text language sql stable security definer set search_path = public as $$
  -- Delivered media must be registered independently. A known source asset
  -- cannot bless an unknown image on INSERT or UPDATE. Every supplied exact
  -- identity must be known and agree with delivered identity/verified lineage.
  select delivered.group_key from public.visual_group_alias delivered
    where delivered.gym_id=p_row.gym_id and delivered.alias_kind='canonical_url'
      and delivered.alias_value=nullif(btrim(p_row.image_url),'')
      and not exists(
        select 1 from public.visual_group_row_aliases(p_row) r
        left join public.visual_group_alias a on a.gym_id=p_row.gym_id
          and a.alias_kind=r.alias_kind and a.alias_value=r.alias_value
        where a.group_key is null or a.group_key<>delivered.group_key
      );
$$;
-- A pending perceptual/manual scene decision is not resolved by an exact URL.
-- Latest confirm/reject of that candidate closes its review hold. Unknown
-- historical publications remain a gym-wide activation blocker after deletion.
create or replace function public.visual_group_row_review_pending(p_row public.content_calendar)
returns boolean language sql stable security definer set search_path = public as $$
  select exists(
    select 1 from public.visual_group_member_event e
    where e.gym_id=p_row.gym_id and e.action='review_hold'
      and not exists(select 1 from public.visual_group_reconciliation r where r.gym_id=e.gym_id and e.id=any(r.hold_event_ids))
      and (exists(select 1 from public.visual_group_row_aliases(p_row) a
        where a.alias_kind=e.alias_kind and a.alias_value=e.alias_value)
        or (e.alias_kind is null and e.alias_value=p_row.id::text
          and e.actor not in ('backfill','backfill_published_review','backfill_ambiguous_review')))
      and not exists(select 1 from public.visual_group_member_event newer
        where newer.gym_id=e.gym_id and newer.alias_kind is not distinct from e.alias_kind
          and newer.alias_value=e.alias_value and newer.id>e.id and newer.action in ('confirmed','rejected'))
  );
$$;
create or replace function public.visual_group_row_active(p_row public.content_calendar)
returns boolean language sql immutable set search_path = public as $$
  select coalesce(p_row.variant_status='active' and
    p_row.status in ('draft','pending','approved','coach_review','publishing','failed'),false);
$$;
create or replace function public.visual_group_row_ambiguous(p_row public.content_calendar)
returns boolean language sql stable security definer set search_path = public as $$
  select coalesce(p_row.published_at is null and p_row.status <> 'published' and
    (p_row.status in ('publishing','failed') or p_row.publish_claim_token is not null
     or p_row.late_post_id is not null or exists(
       select 1 from public.visual_group_usage_sibling s where s.gym_id=p_row.gym_id
         and s.calendar_row_id=p_row.id and s.ambiguous)),false);
$$;

create or replace function public.visual_group_sync_row(
  p_old public.content_calendar,p_new public.content_calendar,p_op text
) returns void language plpgsql security definer set search_path = public as $$
declare l public.visual_group_usage_ledger%rowtype; k record; need_claim boolean; finalized boolean;
begin
  -- Lock ALL affected existing stable groups in order, including cross-gym
  -- moves. A group missing from visual_group is never accepted as identity.
  for k in select g.gym_id,g.group_key from public.visual_group g
    where (g.gym_id=p_old.gym_id and g.group_key=p_old.visual_group_key)
       or (g.gym_id=p_new.gym_id and g.group_key=p_new.visual_group_key)
    order by g.gym_id,g.group_key for update loop null; end loop;

  if p_op='update' and public.visual_group_row_ambiguous(p_new) and p_old.visual_group_key is not null
     and public.visual_group_enforcement_on(p_old.gym_id) then
    update public.visual_group_usage_sibling set ambiguous=true,attempt_id=gen_random_uuid(),
      original_claim_token=p_new.publish_claim_token,original_provider_post_id=p_new.late_post_id,original_image_url=p_new.image_url where gym_id=p_old.gym_id
      and group_key=p_old.visual_group_key and calendar_row_id=p_old.id and not ambiguous;
    update public.visual_group_usage_ledger set ambiguous=true where gym_id=p_old.gym_id
      and group_key=p_old.visual_group_key and state<>'published' and not ambiguous;
  end if;
  if p_op in ('update','delete') and p_old.visual_group_key is not null
     and public.visual_group_enforcement_on(p_old.gym_id)
     and not public.visual_group_row_ambiguous(p_old)
     and (p_op='delete' or not public.visual_group_row_ambiguous(p_new))
     and (p_op='delete' or (not public.visual_group_row_active(p_new) and p_new.status is distinct from 'published' and p_new.published_at is null)
       or p_old.gym_id is distinct from p_new.gym_id
       or p_old.visual_group_key is distinct from p_new.visual_group_key
       or p_old.post_date is distinct from p_new.post_date) then
    update public.visual_group_usage_sibling set state='released',released_at=now()
      where gym_id=p_old.gym_id and group_key=p_old.visual_group_key
        and calendar_row_id=p_old.id and state='active';
    update public.visual_group_usage_ledger set state='released',released_at=now()
      where gym_id=p_old.gym_id and group_key=p_old.visual_group_key and state='reserved' and not ambiguous
        and not exists(select 1 from public.visual_group_usage_sibling s
          where s.gym_id=p_old.gym_id and s.group_key=p_old.visual_group_key and s.state='active');
  end if;
  if p_op='delete' or not public.visual_group_enforcement_on(p_new.gym_id) then return; end if;
  finalized := p_new.status='published' or p_new.published_at is not null;
  need_claim := public.visual_group_row_active(p_new) or public.visual_group_row_ambiguous(p_new) or finalized;
  if not need_claim or p_new.visual_group_key is null or p_new.post_date is null then return; end if;

  select * into l from public.visual_group_usage_ledger
    where gym_id=p_new.gym_id and group_key=p_new.visual_group_key for update;
  if found and l.state <> 'released' and l.reserved_date is distinct from p_new.post_date then
    raise exception 'visual group reserved on another date' using errcode='23514';
  end if;
  if not found then
    insert into public.visual_group_usage_ledger
      (gym_id,group_key,reserved_date,calendar_row_id,channel,state,published_at,ambiguous)
      values(p_new.gym_id,p_new.visual_group_key,p_new.post_date,p_new.id,p_new.account,
        case when finalized then 'published' else 'reserved' end,
        case when finalized then coalesce(p_new.published_at,now()) end,
        public.visual_group_row_ambiguous(p_new));
  elsif l.state <> 'published' then
    update public.visual_group_usage_ledger set
      reserved_date=p_new.post_date, state=case when finalized then 'published' else 'reserved' end,
      released_at=null, reserved_at=case when l.state='released' then now() else reserved_at end,
      published_at=case when finalized then coalesce(p_new.published_at,now()) end,
      ambiguous=ambiguous or public.visual_group_row_ambiguous(p_new)
      where gym_id=p_new.gym_id and group_key=p_new.visual_group_key;
  end if;
  -- Published ledger is never updated. Any legitimate same-date channel
  -- sibling may join, even if its first creation follows another publish.
  insert into public.visual_group_usage_sibling(gym_id,group_key,calendar_row_id,channel,state,ambiguous,original_claim_token,original_provider_post_id,original_image_url)
    values(p_new.gym_id,p_new.visual_group_key,p_new.id,p_new.account,'active',public.visual_group_row_ambiguous(p_new),p_new.publish_claim_token,p_new.late_post_id,p_new.image_url)
    on conflict(gym_id,group_key,calendar_row_id) do update set
      state='active',released_at=null,channel=excluded.channel,
      ambiguous=public.visual_group_usage_sibling.ambiguous or excluded.ambiguous,
      attempt_id=case when not public.visual_group_usage_sibling.ambiguous and excluded.ambiguous
        then gen_random_uuid() else public.visual_group_usage_sibling.attempt_id end,
      original_claim_token=case when not public.visual_group_usage_sibling.ambiguous and excluded.ambiguous
        then excluded.original_claim_token else coalesce(public.visual_group_usage_sibling.original_claim_token,excluded.original_claim_token) end,
      original_provider_post_id=case when not public.visual_group_usage_sibling.ambiguous and excluded.ambiguous
        then excluded.original_provider_post_id else coalesce(public.visual_group_usage_sibling.original_provider_post_id,excluded.original_provider_post_id) end,
      original_image_url=case when not public.visual_group_usage_sibling.ambiguous and excluded.ambiguous
        then excluded.original_image_url else coalesce(public.visual_group_usage_sibling.original_image_url,excluded.original_image_url) end;
end;
$$;

create or replace function public.visual_group_guard_trigger()
returns trigger language plpgsql security definer set search_path = public as $$
declare resolved text; need_claim boolean; finalized boolean; identity_changed boolean; media_changed boolean;
begin
  if tg_op='DELETE' then
    if public.visual_group_enforcement_on(old.gym_id) then
      perform public.visual_group_sync_row(old,null,'delete');
    end if;
    return old;
  end if;
  if not public.visual_group_enforcement_on(new.gym_id)
     and (tg_op='INSERT' or not public.visual_group_enforcement_on(old.gym_id)) then return new; end if;
  if tg_op='UPDATE' then
    media_changed := new.image_url is distinct from old.image_url or
      exists(select 1 from (values('source_media_url'),('source_media_asset_id'),('drive_file_id'),('byte_hash'),('r2_key')) x(k)
        where to_jsonb(new)->>k is distinct from to_jsonb(old)->>k);
    identity_changed := new.gym_id is distinct from old.gym_id or
      new.post_date is distinct from old.post_date or new.visual_group_key is distinct from old.visual_group_key or
      new.image_url is distinct from old.image_url or
      exists(select 1 from (values('source_media_url'),('source_media_asset_id'),('drive_file_id'),('byte_hash'),('r2_key')) x(k)
        where to_jsonb(new)->>k is distinct from to_jsonb(old)->>k);
    if identity_changed and (new.status='published' or new.published_at is not null or old.status='published' or old.published_at is not null
       or (public.visual_group_row_ambiguous(old) and not public.visual_group_row_reconciled_here(old))) then
      raise exception 'confirmed or ambiguous send identity/date cannot change' using errcode='23514';
    end if;
    if public.visual_group_row_ambiguous(old) and not public.visual_group_row_reconciled_here(old) and exists(
      select 1 from public.visual_group_usage_sibling s where s.gym_id=old.gym_id and s.calendar_row_id=old.id and s.ambiguous
        and ((new.publish_claim_token is not null and s.original_claim_token is not null and new.publish_claim_token<>s.original_claim_token)
          or (new.late_post_id is not null and s.original_provider_post_id is not null and new.late_post_id<>s.original_provider_post_id))) then
      raise exception 'ambiguous original claim/provider IDs cannot be replaced' using errcode='23514';
    end if;
    if public.visual_group_row_ambiguous(old) and not public.visual_group_row_reconciled_here(old) and
       (new.variant_status is distinct from old.variant_status or
        (new.published_at is null and new.status not in ('publishing','failed','published'))) then
      raise exception 'ambiguous send needs explicit reconciliation' using errcode='23514';
    end if;
  end if;
  if public.visual_group_enforcement_on(new.gym_id) then
    finalized := new.status='published' or new.published_at is not null;
    need_claim := public.visual_group_row_active(new) or public.visual_group_row_ambiguous(new) or finalized;
    if need_claim then
      -- Take stable identity locks BEFORE readiness reads, not just before
      -- writing the ledger. A concurrent decision event's FK key-share lock
      -- must commit before we re-read whether the candidate remains held.
      resolved:=public.visual_group_resolve_row(new);
      perform 1 from public.visual_group g where
        (g.gym_id=new.gym_id and g.group_key in (new.visual_group_key,resolved)) or
        (tg_op='UPDATE' and g.gym_id=old.gym_id and g.group_key=old.visual_group_key)
        order by g.gym_id,g.group_key for update;
      resolved := public.visual_group_resolve_row(new);
      if tg_op='UPDATE' and media_changed and resolved=old.visual_group_key
         and not exists(
           select 1 from (
             select * from public.visual_group_row_aliases(new)
             except select * from public.visual_group_row_aliases(old)
           ) changed join public.visual_group_alias a
           on a.gym_id=new.gym_id and a.alias_kind=changed.alias_kind
              and a.alias_value=changed.alias_value and a.group_key=resolved
         ) then
        -- Carried-forward source aliases are insufficient evidence for newly
        -- changed media. A changed exact alias must independently confirm it.
        resolved:=null;
      end if;
      -- Caller-provided keys are hints, never authority. An unchanged old key
      -- cannot survive a new media URL unless exact aliases still resolve it.
      if new.visual_group_key is not null and new.visual_group_key is distinct from resolved then
        new.visual_group_key := null;
      else new.visual_group_key := resolved; end if;
      if public.visual_group_row_review_pending(new) then
        if new.media_not_ready_reason is null or new.media_not_ready_reason in
          ('visual_group_identity_unresolved','visual_group_date_unresolved','visual_group_scene_review_required') then
          new.media_not_ready_reason:='visual_group_scene_review_required';
        end if;
      elsif new.visual_group_key is null then
        if new.media_not_ready_reason is null or new.media_not_ready_reason in
          ('visual_group_identity_unresolved','visual_group_date_unresolved') then
          new.media_not_ready_reason := 'visual_group_identity_unresolved';
        end if;
      elsif new.post_date is null then
        if new.media_not_ready_reason is null or new.media_not_ready_reason in
          ('visual_group_identity_unresolved','visual_group_date_unresolved') then
          new.media_not_ready_reason := 'visual_group_date_unresolved';
        end if;
      elsif new.media_not_ready_reason in ('visual_group_identity_unresolved','visual_group_date_unresolved','visual_group_scene_review_required') then
        new.media_not_ready_reason := null;
      end if;
      if public.visual_group_row_ambiguous(new) and
         (new.visual_group_key is null or new.post_date is null) and not exists(
           select 1 from public.visual_group_member_event e where e.gym_id=new.gym_id
             and e.actor='runtime_ambiguous_review' and e.alias_value=new.id::text
             and not exists(select 1 from public.visual_group_reconciliation r
               where r.gym_id=e.gym_id and r.calendar_row_id=new.id and e.id=any(r.hold_event_ids))) then
        insert into public.visual_group_member_event(gym_id,group_key,alias_value,action,actor,reason)
          values(new.gym_id,new.visual_group_key,new.id::text,'review_hold','runtime_ambiguous_review',
            jsonb_build_object('reason','ambiguous_send_identity_or_date_unresolved',
              'calendar_row_id',new.id,'image_url',new.image_url,'post_date',new.post_date,
              'status',new.status,'late_post_id',new.late_post_id,'publish_claim_token',new.publish_claim_token)::text);
      end if;
      -- BEFORE trigger holds must not allow PR230's claim RPC to return a
      -- token after its pre-read. Refuse the entire approval/claim/finalize.
      if (new.status in ('approved','publishing') or finalized) and
        (new.visual_group_key is null or new.post_date is null or
         nullif(btrim(new.image_url),'') is null or new.media_not_ready_reason is not null) then
        raise exception 'visual media not ready for approval, claim or finalize' using errcode='23514';
      end if;
    end if;
  end if;
  perform public.visual_group_sync_row(case when tg_op='UPDATE' then old end,new,lower(tg_op));
  return new;
end;
$$;
drop trigger if exists content_calendar_visual_group_guard on public.content_calendar;
create trigger content_calendar_visual_group_guard before insert or update or delete
  on public.content_calendar for each row execute function public.visual_group_guard_trigger();

-- Shared atomic operation for complete, unsent sibling sets. NULL p_media is
-- date-only. Replacement media clears omitted optional identities, preventing
-- an old source/byte alias from blessing an unrelated new image.
create or replace function public.visual_group_swap_siblings(
  p_gym_id text,p_row_ids uuid[],p_new_date date,p_media jsonb default null
) returns integer language plpgsql security definer set search_path = public as $$
declare n integer; g text; old_date date; l public.visual_group_usage_ledger%rowtype;
  assignments text:=''; c text; r public.content_calendar; replacement public.content_calendar;
begin
  if not public.visual_group_enforcement_on(p_gym_id) or p_new_date is null
     or cardinality(p_row_ids) is null or cardinality(p_row_ids)=0
     or exists(select 1 from unnest(p_row_ids) a(id) where id is null)
     or cardinality(p_row_ids)<>(select count(distinct id) from unnest(p_row_ids) a(id)) then
    raise exception 'invalid sibling operation' using errcode='23514';
  end if;
  perform 1 from public.content_calendar where gym_id=p_gym_id and id=any(p_row_ids)
    order by id for update;
  select count(*),min(visual_group_key),min(post_date) into n,g,old_date
    from public.content_calendar where gym_id=p_gym_id and id=any(p_row_ids);
  if n<>cardinality(p_row_ids) or g is null or old_date is null or exists(
    select 1 from public.content_calendar where id=any(p_row_ids) and
      (gym_id<>p_gym_id or visual_group_key is distinct from g or post_date is distinct from old_date
       or not public.visual_group_row_active(content_calendar) or public.visual_group_row_ambiguous(content_calendar)
       or published_at is not null or status='published')) then
    raise exception 'requires one complete unsent same-date sibling group' using errcode='23514';
  end if;
  if p_media is not null and (
    select count(distinct image_url)>1 or
      count(distinct coalesce(nullif(lower(btrim(format)),''),'feed'))>1
    from public.content_calendar where gym_id=p_gym_id and id=any(p_row_ids)
  ) then
    raise exception 'distinct sibling derivatives require per-row replacement payloads; unsupported until integration'
      using errcode='23514';
  end if;
  -- Take old + replacement group locks BEFORE validating the full membership.
  -- Replacement is resolved from aliases, not a caller's arbitrary key.
  if p_media is not null then
    if jsonb_typeof(p_media)<>'object' or exists(select 1 from jsonb_object_keys(p_media) k
      where k not in ('image_url','source_media_url','source_media_asset_id','drive_file_id','byte_hash','r2_key','visual_group_key')) then
      raise exception 'invalid media fields' using errcode='22023';
    end if;
    select * into r from public.content_calendar where id=p_row_ids[1];
    replacement := jsonb_populate_record(r,
      jsonb_build_object('image_url',null,'source_media_url',null,'source_media_asset_id',null,
        'drive_file_id',null,'byte_hash',null,'r2_key',null,'visual_group_key',null)||p_media);
    replacement.visual_group_key := public.visual_group_resolve_row(replacement);
    if replacement.visual_group_key is null or nullif(btrim(replacement.image_url),'') is null
       or (p_media->>'visual_group_key' is not null and p_media->>'visual_group_key'<>replacement.visual_group_key) then
      raise exception 'replacement identity is unresolved' using errcode='23514';
    end if;
  end if;
  perform 1 from public.visual_group where gym_id=p_gym_id and
    group_key in (g,replacement.visual_group_key) order by gym_id,group_key for update;
  select * into l from public.visual_group_usage_ledger where gym_id=p_gym_id and group_key=g for update;
  if not found or l.state<>'reserved' or l.ambiguous or l.reserved_date<>old_date or exists(
    select 1 from public.visual_group_usage_sibling where gym_id=p_gym_id and group_key=g
      and state='active' and not(calendar_row_id=any(p_row_ids))) or exists(
    select 1 from public.content_calendar where gym_id=p_gym_id and visual_group_key=g
      and public.visual_group_row_active(content_calendar) and not(id=any(p_row_ids))) then
    raise exception 'partial, stale, published or ambiguous sibling set' using errcode='23514';
  end if;
  if p_media is null or replacement.visual_group_key=g then
    update public.visual_group_usage_ledger set reserved_date=p_new_date where gym_id=p_gym_id and group_key=g;
  end if;
  if p_media is null then
    update public.content_calendar set post_date=p_new_date where gym_id=p_gym_id and id=any(p_row_ids);
  else
    foreach c in array array['image_url','source_media_url','source_media_asset_id','drive_file_id','byte_hash','r2_key'] loop
      if exists(select 1 from information_schema.columns where table_schema='public' and table_name='content_calendar' and column_name=c) then
        assignments:=assignments||format(', %I = (jsonb_populate_record(null::public.content_calendar,$3)).%I',c,c);
      end if;
    end loop;
    execute 'update public.content_calendar set post_date=$1,visual_group_key=$2'||assignments||
      ' where gym_id=$4 and id=any($5)' using p_new_date,replacement.visual_group_key,to_jsonb(replacement),p_gym_id,p_row_ids;
  end if;
  return n;
end;
$$;
create or replace function public.visual_group_swap_redate(p_gym_id text,p_row_ids uuid[],p_new_date date)
returns integer language sql security definer set search_path = public as $$
  select public.visual_group_swap_siblings(p_gym_id,p_row_ids,p_new_date,null);
$$;

-- Explicit terminal-provider evidence only. This RPC records a trusted service
-- verifier's immutable receipt; it does not contact a provider. Timeouts,
-- not-found responses and possible retries are never non-delivery evidence.
create or replace function public.visual_group_reconcile_ambiguous(
  p_gym_id text,p_row_id uuid,p_outcome text,p_group_key text,p_date date,
  p_evidence jsonb,p_actor text
) returns jsonb language plpgsql security definer set search_path = public as $$
declare c public.content_calendar; live boolean; expected jsonb; v_attempts jsonb;
  groups text[]; holds bigint[]; receipt_id uuid; k text; v_ledger public.visual_group_usage_ledger%rowtype;
  pub_at timestamptz; checked_at timestamptz; provider_id text; prior public.visual_group_reconciliation%rowtype;
  calendar_updated boolean:=false;
begin
  if p_row_id is null or nullif(btrim(p_gym_id),'') is null or nullif(btrim(p_actor),'') is null
    or p_outcome not in ('confirmed_not_sent','confirmed_published') or p_outcome is null
    or jsonb_typeof(p_evidence) is distinct from 'object'
    or p_evidence->>'source' is distinct from 'provider_terminal_readback'
    or p_evidence->>'gym_id' is distinct from p_gym_id
    or p_evidence->>'calendar_row_id' is distinct from p_row_id::text
    or p_evidence->>'group_key' is distinct from p_group_key
    or p_evidence->>'calendar_date' is distinct from p_date::text
    or nullif(btrim(p_evidence->>'provider'),'') is null
    or nullif(btrim(p_evidence->>'request_id'),'') is null
    or nullif(btrim(p_evidence->>'receipt_ref'),'') is null
    or p_evidence->'terminal' is distinct from 'true'::jsonb
    or p_evidence->'will_retry' is distinct from 'false'::jsonb
    or (p_outcome='confirmed_not_sent' and
      (p_evidence->>'delivery' is distinct from 'not_delivered' or
       p_evidence->>'provider_status' not in ('failed_before_delivery','canceled_before_delivery') or
       p_evidence->>'provider_status' is null))
    or (p_outcome='confirmed_published' and
      (p_evidence->>'delivery' is distinct from 'delivered' or
       p_evidence->>'provider_status' is distinct from 'published' or
       nullif(btrim(p_evidence->>'provider_post_id'),'') is null or nullif(btrim(p_evidence->>'delivered_url'),'') is null or p_group_key is null or p_date is null)) then
    raise exception 'conclusive terminal provider evidence and exact claim binding required' using errcode='23514';
  end if;
  checked_at:=(p_evidence->>'checked_at')::timestamptz;
  if checked_at is null or checked_at>clock_timestamp()+interval '5 minutes' then
    raise exception 'valid provider checked_at required' using errcode='23514';
  end if;
  if p_outcome='confirmed_published' then
    pub_at:=(p_evidence->>'published_at')::timestamptz;
    if pub_at is null or pub_at>checked_at then raise exception 'confirmed publish time required'; end if;
  end if;
  -- Serialize terminal receipts even for deleted, identity-unknown orphans
  -- that have neither a calendar row nor a stable group to lock.
  perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array('visual_reconcile',p_gym_id,p_row_id)::text,0));
  -- Calendar row lock precedes stable group locks, matching normal writes.
  select * into c from public.content_calendar where id=p_row_id for update;
  live:=found;
  if live and c.gym_id is distinct from p_gym_id then raise exception 'foreign calendar row'; end if;
  select coalesce(array_agg(distinct group_key order by group_key),'{}'::text[]) into groups
    from public.visual_group_usage_sibling where gym_id=p_gym_id and calendar_row_id=p_row_id;
  if p_outcome='confirmed_not_sent' and p_group_key is not null and not p_group_key=any(groups) then
    raise exception 'non-delivery group does not belong to original reservation';
  end if;
  if p_group_key is not null and not p_group_key=any(groups) then groups:=array_append(groups,p_group_key); end if;
  perform 1 from public.visual_group where gym_id=p_gym_id and group_key=any(groups)
    order by gym_id,group_key for update;
  if p_group_key is not null and not exists(select 1 from public.visual_group where gym_id=p_gym_id and group_key=p_group_key) then
    raise exception 'unknown delivered group';
  end if;
  select * into prior from public.visual_group_reconciliation
    where gym_id=p_gym_id and calendar_row_id=p_row_id and evidence=p_evidence;
  if found then
    if exists(select 1 from public.visual_group_usage_sibling s where s.gym_id=p_gym_id and s.calendar_row_id=p_row_id and s.ambiguous
        and not prior.attempts @> jsonb_build_array(jsonb_build_object('group_key',s.group_key,'attempt_id',s.attempt_id::text)))
      or exists(select 1 from public.visual_group_member_event e where e.gym_id=p_gym_id and e.alias_value=p_row_id::text
        and e.action='review_hold' and e.actor in ('backfill_ambiguous_review','runtime_ambiguous_review')
        and e.id>all(prior.hold_event_ids)) then
      raise exception 'receipt belongs to an earlier ambiguous attempt';
    end if;
    return jsonb_build_object('receipt_id',prior.id,'outcome',prior.outcome,'idempotent',true);
  end if;
  select coalesce(jsonb_agg(jsonb_build_object('group_key',s.group_key,'attempt_id',s.attempt_id::text,
    'claim_token',s.original_claim_token::text,'provider_post_id',s.original_provider_post_id,'image_url',s.original_image_url)
    order by s.group_key),'[]'::jsonb) into v_attempts
    from public.visual_group_usage_sibling s where gym_id=p_gym_id and calendar_row_id=p_row_id and ambiguous;
  select coalesce(array_agg(id order by id),'{}'::bigint[]) into holds from public.visual_group_member_event e
    where gym_id=p_gym_id and alias_value=p_row_id::text and action='review_hold'
      and actor in ('backfill_ambiguous_review','runtime_ambiguous_review')
      and not exists(select 1 from public.visual_group_reconciliation r where r.gym_id=e.gym_id and e.id=any(r.hold_event_ids));
  if v_attempts='[]'::jsonb and cardinality(holds)=0 then raise exception 'no unresolved ambiguous attempt'; end if;
  if p_evidence->'claims' is distinct from v_attempts then raise exception 'original claim/provider IDs or attempt UUID mismatch'; end if;
  if exists(select 1 from jsonb_array_elements(v_attempts) a
      where a->>'claim_token' is null and a->>'provider_post_id' is null) then
    raise exception 'attempt has no recoverable original provider claim; preserve for manual evidence recovery';
  end if;
  if exists(select 1 from public.visual_group_reconciliation r where r.gym_id=p_gym_id
    and r.calendar_row_id=p_row_id and exists(select 1 from jsonb_array_elements(v_attempts) a
      where r.attempts @> jsonb_build_array(a))) then raise exception 'attempt already has terminal evidence'; end if;
  if live and p_group_key is not null and c.visual_group_key is not null and c.visual_group_key<>p_group_key then
    raise exception 'calendar identity differs from evidence';
  end if;
  if live and c.post_date is not null and c.post_date is distinct from p_date then raise exception 'calendar date differs from evidence'; end if;
  if exists(select 1 from public.visual_group_usage_sibling s join public.visual_group_usage_ledger l using(gym_id,group_key)
      where s.gym_id=p_gym_id and s.calendar_row_id=p_row_id and s.ambiguous
        and l.reserved_date is distinct from p_date) then raise exception 'reserved date differs from evidence'; end if;
  if p_outcome='confirmed_published' and (not exists(
      select 1 from public.visual_group_alias a where a.gym_id=p_gym_id and a.group_key=p_group_key
        and a.alias_kind='canonical_url' and a.alias_value=p_evidence->>'delivered_url')
      or (live and c.image_url is not null and c.image_url<>p_evidence->>'delivered_url')
      or exists(select 1 from jsonb_array_elements(v_attempts) a where
        (a->>'image_url' is not null and a->>'image_url'<>p_evidence->>'delivered_url') or
        (a->>'provider_post_id' is not null and a->>'provider_post_id'<>p_evidence->>'provider_post_id'))) then
    raise exception 'confirmed delivery URL/provider ID differs from original claim or verified scene';
  end if;
  if p_outcome='confirmed_not_sent' and ((live and (c.status='published' or c.published_at is not null)) or
    exists(select 1 from public.visual_group_usage_sibling s join public.visual_group_usage_ledger l using(gym_id,group_key)
      where s.gym_id=p_gym_id and s.calendar_row_id=p_row_id and l.state='published')) then
    raise exception 'published usage cannot be reconciled as not sent';
  end if;
  -- For unknown orphan identity, the receipt must include captured hold IDs
  -- and claim markers; knowing only an unrelated provider request is insufficient.
  select coalesce(jsonb_agg(jsonb_build_object('hold_event_id',e.id,
    'claim_token',case when left(btrim(e.reason),1)='{' then e.reason::jsonb->>'publish_claim_token' end,
    'provider_post_id',case when left(btrim(e.reason),1)='{' then e.reason::jsonb->>'late_post_id' end,
    'calendar_date',case when left(btrim(e.reason),1)='{' then e.reason::jsonb->>'post_date' end,
    'image_url',case when left(btrim(e.reason),1)='{' then e.reason::jsonb->>'image_url' end) order by e.id),'[]'::jsonb)
    into expected from public.visual_group_member_event e where e.id=any(holds);
  if p_evidence->'hold_claims' is distinct from expected then raise exception 'original unknown hold claim IDs mismatch'; end if;
  if exists(select 1 from jsonb_array_elements(expected) a where
    (a->>'calendar_date' is not null and a->>'calendar_date' is distinct from p_date::text) or
    (p_outcome='confirmed_published' and
      ((a->>'image_url' is not null and a->>'image_url' is distinct from p_evidence->>'delivered_url') or
       (a->>'provider_post_id' is not null and a->>'provider_post_id' is distinct from p_evidence->>'provider_post_id')))) then
    raise exception 'original hold date/delivered media/provider ID differs from evidence';
  end if;
  if v_attempts='[]'::jsonb and not exists(select 1 from jsonb_array_elements(expected) a
      where a->>'claim_token' is not null or a->>'provider_post_id' is not null) then
    raise exception 'unknown identity has no recoverable original provider claim; preserve for manual evidence recovery';
  end if;
  insert into public.visual_group_reconciliation(gym_id,calendar_row_id,outcome,delivered_group_key,
    reserved_groups,attempts,hold_event_ids,original_claim,evidence,actor)
    values(p_gym_id,p_row_id,p_outcome,case when p_outcome='confirmed_published' then p_group_key end,
      groups,v_attempts,holds,jsonb_build_object('publish_claim_token',c.publish_claim_token,'late_post_id',c.late_post_id),p_evidence,p_actor)
    returning id into receipt_id;
  -- A definitive delivery is permanent even if its calendar row was deleted.
  if p_outcome='confirmed_published' then
    select * into v_ledger from public.visual_group_usage_ledger where gym_id=p_gym_id and group_key=p_group_key for update;
    if found and v_ledger.state<>'released' and v_ledger.reserved_date is distinct from p_date then raise exception 'confirmed scene date conflict'; end if;
    if not found then
      insert into public.visual_group_usage_ledger(gym_id,group_key,reserved_date,calendar_row_id,state,published_at)
        values(p_gym_id,p_group_key,p_date,p_row_id,'published',pub_at);
    elsif v_ledger.state<>'published' then
      update public.visual_group_usage_ledger set state='published',reserved_date=p_date,published_at=pub_at,released_at=null
        where gym_id=p_gym_id and group_key=p_group_key;
    end if;
  end if;
  update public.visual_group_usage_sibling set ambiguous=false,state='released',released_at=now()
    where gym_id=p_gym_id and calendar_row_id=p_row_id and ambiguous;
  if live and p_outcome='confirmed_not_sent' then
    update public.content_calendar set status='killed',publish_claim_token=null,late_post_id=null where id=p_row_id;
    calendar_updated:=true;
  elsif live and public.visual_group_resolve_row(c)=p_group_key and not public.visual_group_row_review_pending(c) then
    update public.content_calendar set status='published',published_at=pub_at,visual_group_key=p_group_key,
      late_post_id=p_evidence->>'provider_post_id' where id=p_row_id;
    calendar_updated:=true;
  end if;
  foreach k in array groups loop
    if public.visual_group_group_reconciled(p_gym_id,k) then
      update public.visual_group_usage_ledger set ambiguous=false,
        state=case when exists(select 1 from public.visual_group_usage_sibling s where s.gym_id=p_gym_id and s.group_key=k and s.state='active')
          then 'reserved' else 'released' end,
        released_at=case when not exists(select 1 from public.visual_group_usage_sibling s where s.gym_id=p_gym_id and s.group_key=k and s.state='active') then now() end
        where gym_id=p_gym_id and group_key=k and state<>'published';
    end if;
  end loop;
  return jsonb_build_object('receipt_id',receipt_id,'outcome',p_outcome,'calendar_updated',calendar_updated,'idempotent',false);
end;
$$;

-- All RPCs/helpers are private to service_role; trigger runs as its owner.
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname in
      ('visual_group_enforcement_on','visual_group_row_aliases','visual_group_resolve_row',
       'visual_group_row_active','visual_group_row_review_pending','visual_group_row_ambiguous','visual_group_sync_row',
       'visual_group_guard_trigger','visual_group_sibling_keep_ambiguity',
       'visual_group_sibling_reconciled','visual_group_group_reconciled','visual_group_row_reconciled_here','visual_group_reconcile_ambiguous','visual_group_swap_siblings','visual_group_swap_redate') loop
    execute format('revoke all on function %s from public,anon,authenticated',f.signature);
    execute format('grant execute on function %s to service_role',f.signature);
  end loop;
end;
$$;
