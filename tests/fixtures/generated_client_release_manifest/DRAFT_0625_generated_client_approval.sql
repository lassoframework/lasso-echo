-- DRAFT / UNAPPLIED / RELEASE HELD. Portal ca21d05f, Echo #379 b9769b02.
-- Apply LAST after approval provenance, full #379 entry/admission/cutover stack,
-- and latest generated bundle bridge. Later replacement bodies can erase CAS.
-- No live receipt writer is provided: immutable delivered-byte evidence must be
-- issued by a separately reviewed trusted hosted-readback writer.
begin;
do $$
begin
 if current_user <> 'postgres' or to_regprocedure('public.fixer_forward_calendar_entry_lock_20261008()') is null
  or to_regprocedure('public.calendar_stamp_verified_approval(uuid,uuid,text,text)') is null then
  raise exception 'generated approval requires composed #379 approval stack' using errcode='23514'; end if;
end $$;

-- BEGIN COMPOSED STACK PREFLIGHT
do $$
declare expected record;
begin
 if not exists(select 1 from pg_trigger where tgrelid='public.content_calendar'::regclass
   and tgname='000_fixer_forward_corpus_entry_20261008' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.content_calendar'::regclass
   and tgname='generated_approval_pins' and tgenabled='O') then
  raise exception 'generated approval requires full #379 cutover and generated lease triggers' using errcode='23514'; end if;
 for expected in select * from (values
  ('generated_approval_pins_20261007','e9589fe6d3bfbf9301eb3b76d2d71ac9'),
  ('generated_send_acquire_20261007','be844ea644d476841403abe4adf38408')) as target(name,hash) loop
  if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname=expected.name) <> 1
   or (select md5(prosrc) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname=expected.name) is distinct from expected.hash then
   raise exception 'generated approval requires latest bundle lease body: %',expected.name using errcode='23514'; end if;
 end loop;
end $$;
-- END COMPOSED STACK PREFLIGHT

create table public.calendar_generated_artifact_versions (
 id uuid primary key, gym_id text not null, image_url text not null,
 delivered_sha256 text not null check (delivered_sha256 ~ '^[0-9a-f]{64}$'),
 render_manifest_digest text not null check (render_manifest_digest ~ '^[0-9a-f]{64}$'),
 delivery_receipt jsonb not null check (jsonb_typeof(delivery_receipt) = 'object'),
 verified_at timestamptz not null default now(),
 check (btrim(gym_id) <> '' and btrim(image_url) <> '')
);
alter table public.calendar_generated_artifact_versions enable row level security;
revoke all on public.calendar_generated_artifact_versions from public,anon,authenticated,service_role;
-- Service role may only read evidence. No producer-supplied SHA can issue it.
grant select on public.calendar_generated_artifact_versions to service_role;
create function public.calendar_generated_version_immutable() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin raise exception 'generated artifact versions are immutable' using errcode='23514'; end $$;
revoke all on function public.calendar_generated_version_immutable() from public,anon,authenticated,service_role;
create trigger generated_version_immutable before update or delete on public.calendar_generated_artifact_versions
 for each row execute function public.calendar_generated_version_immutable();
create trigger generated_version_no_truncate before truncate on public.calendar_generated_artifact_versions
 for each statement execute function public.calendar_generated_version_immutable();

alter table public.content_calendar
 add column creative_origin text check (creative_origin is null or creative_origin = 'generated'),
 add column generated_artifact_version_id uuid references public.calendar_generated_artifact_versions(id),
 add column generated_artifact_sha256 text check (generated_artifact_sha256 is null or generated_artifact_sha256 ~ '^[0-9a-f]{64}$');

create function public.calendar_generated_snapshot_matches(p_row public.content_calendar,p_expected jsonb)
returns boolean language sql stable set search_path=pg_catalog,public as $$
 select case when p_row.creative_origin = 'generated' or p_row.source_media_asset_id like 'generated-astra:%'
  or p_row.generated_artifact_version_id is not null or p_row.generated_artifact_sha256 is not null then
  coalesce(p_row.creative_origin = 'generated' and p_row.generated_artifact_version_id is not null
  and p_row.generated_artifact_sha256 is not null and p_expected is not null
  and p_expected->>'creative_origin' = 'generated'
  and p_expected->>'generated_artifact_version_id' = p_row.generated_artifact_version_id::text
  and p_expected->>'generated_artifact_sha256' = p_row.generated_artifact_sha256, false)
 else true end;
$$;
revoke all on function public.calendar_generated_snapshot_matches(public.content_calendar,jsonb) from public,anon,authenticated;
grant execute on function public.calendar_generated_snapshot_matches(public.content_calendar,jsonb) to service_role;

create function public.calendar_generated_approval_guard() returns trigger
language plpgsql security definer set search_path=pg_catalog,public as $$
declare artifact public.calendar_generated_artifact_versions%rowtype; changed boolean := false;
begin
 if tg_op='UPDATE' and (old.creative_origin='generated' or old.source_media_asset_id like 'generated-astra:%' or old.generated_artifact_version_id is not null or old.generated_artifact_sha256 is not null)
  and new.creative_origin is distinct from 'generated' then
  raise exception 'generated marker cannot be cleared' using errcode='23514'; end if;
 if new.creative_origin is null and new.source_media_asset_id not like 'generated-astra:%'
  and new.generated_artifact_version_id is null and new.generated_artifact_sha256 is null then return new; end if;
 -- SQL NULL does not mean ordinary: explicit source identity also requires the marker.
 if new.creative_origin is null and new.source_media_asset_id is null
  and new.generated_artifact_version_id is null and new.generated_artifact_sha256 is null then return new; end if;
 if new.creative_origin is distinct from 'generated' or new.generated_artifact_version_id is null
  or new.generated_artifact_sha256 is null then
  raise exception 'generated immutable delivered artifact binding required' using errcode='23514'; end if;
 select * into artifact from public.calendar_generated_artifact_versions where id=new.generated_artifact_version_id;
 if not found or artifact.gym_id is distinct from new.gym_id or artifact.image_url is distinct from new.image_url
  or artifact.delivered_sha256 is distinct from new.generated_artifact_sha256 then
  raise exception 'generated artifact receipt does not match tenant and delivered bytes' using errcode='23514'; end if;
 if tg_op='INSERT' then
  if new.status is distinct from 'pending' then raise exception 'generated insertion must await human approval' using errcode='23514'; end if;
  new.approval_kind := null; new.approved_by := null; new.approved_at := null; new.approval_digest := null;
 end if;
 if tg_op='UPDATE' then
  changed := row(new.id,new.gym_id,new.caption,new.image_url,new.byte_hash,new.source_media_asset_id,new.source_media_url,
    new.post_date,new.account,new.format,new.generated_artifact_version_id,new.generated_artifact_sha256,
    new.gbp_topic_type,new.gbp_cta_type,new.gbp_cta_url,new.gbp_event,new.gbp_offer,new.gbp_location_id)
   is distinct from row(old.id,old.gym_id,old.caption,old.image_url,old.byte_hash,old.source_media_asset_id,old.source_media_url,
    old.post_date,old.account,old.format,old.generated_artifact_version_id,old.generated_artifact_sha256,
    old.gbp_topic_type,old.gbp_cta_type,old.gbp_cta_url,old.gbp_event,old.gbp_offer,old.gbp_location_id);
  if changed and (old.status in ('publishing','published') or old.publish_claim_token is not null) then
   raise exception 'generated creative frozen during claimed send' using errcode='23514'; end if;
  if changed then
   new.status := 'pending'; new.approval_kind := null; new.approved_by := null;
   new.approved_at := null; new.approval_digest := null;
  end if;
 end if;
 -- A row-level final guard covers every legacy/proven/GBP claim entry point,
 -- regardless of autonomy and caller flags. It takes no new advisory/row locks.
 if new.status in ('publishing','published') then
  if tg_op='INSERT' or old.status not in ('approved','publishing','published')
   or (old.approval_kind is distinct from 'human' or new.approval_kind is distinct from 'human'
    or row(new.approved_by,new.approved_at,new.approval_digest) is distinct from row(old.approved_by,old.approved_at,old.approval_digest)
    or old.status is null
    or nullif(btrim(old.approved_by),'') is null or old.approved_at is null
    or old.approval_digest is distinct from public.calendar_approval_digest(new)) then
   raise exception 'generated publish requires exact row human approval' using errcode='23514'; end if;
 end if;
 return new;
end $$;
revoke all on function public.calendar_generated_approval_guard() from public,anon,authenticated,service_role;
-- Final alphabetical guard runs after generated source-policy pins and corpus guards.
create trigger zzz_calendar_generated_approval_guard before insert or update on public.content_calendar
 for each row execute function public.calendar_generated_approval_guard();

do $$
declare pair record;
begin
 for pair in select * from (values
 ('approve_calendar_row_if_media_ready','8124b945c06d8adeaa5c63cf4712a73f'),
 ('calendar_recover_unproved_approval','4d0ac4fd7d68e60e78850dbd7f2a1876'),
 ('calendar_stamp_verified_approval','efba82fc5f34a108c7ad96c42dba48ee')) as target(name,hash) loop
  if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname=pair.name) <> 1
   or (select md5(prosrc) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname=pair.name) is distinct from pair.hash then
   raise exception 'generated approval prerequisite body drift: %',pair.name using errcode='23514'; end if;
 end loop;
end $$;

create or replace function public.calendar_approval_digest(
  p_row public.content_calendar
) returns text
language sql
stable
set search_path = public
as $$
  select case when p_row.creative_origin = 'generated' then
    md5(concat_ws(chr(31), 'generated-approval-v1', p_row.id::text, p_row.gym_id,
      p_row.generated_artifact_version_id::text, p_row.generated_artifact_sha256,
      md5(concat_ws(chr(31),
    coalesce(nullif(lower(btrim(p_row.account)), ''), ''),
    coalesce(nullif(lower(btrim(p_row.format)), ''), 'feed'),
    coalesce(p_row.post_date::text, ''),
    coalesce(p_row.caption, ''),
    coalesce(nullif(btrim(p_row.image_url), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'byte_hash'), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'source_media_asset_id'), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'source_media_url'), ''), ''),
    -- Preserve existing IG/FB digests. GBP proof also binds provider fields,
    -- including the destination and structured offers/events. jsonb::text has
    -- canonical key ordering, independent of input JSON key order.
    case when lower(btrim(p_row.account)) = 'googlebusiness' then
      public.calendar_gbp_approval_snapshot(p_row)::text else null end
  ))))
    else md5(concat_ws(chr(31),
    coalesce(nullif(lower(btrim(p_row.account)), ''), ''),
    coalesce(nullif(lower(btrim(p_row.format)), ''), 'feed'),
    coalesce(p_row.post_date::text, ''),
    coalesce(p_row.caption, ''),
    coalesce(nullif(btrim(p_row.image_url), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'byte_hash'), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'source_media_asset_id'), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'source_media_url'), ''), ''),
    -- Preserve existing IG/FB digests. GBP proof also binds provider fields,
    -- including the destination and structured offers/events. jsonb::text has
    -- canonical key ordering, independent of input JSON key order.
    case when lower(btrim(p_row.account)) = 'googlebusiness' then
      public.calendar_gbp_approval_snapshot(p_row)::text else null end
  )) end;
$$;

CREATE OR REPLACE FUNCTION public.approve_calendar_row_if_media_ready(p_row_id uuid, p_gym_id text, p_expected jsonb DEFAULT NULL::jsonb)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the UPDATE below takes any row lock.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  return query
  update public.content_calendar c
     set status = 'approved',
         approval_kind = null,
         approved_by = null,
         approved_at = null,
         approval_digest = public.calendar_approval_digest(c)
   where id = p_row_id and gym_id = p_gym_id
     and status = 'pending' and published_at is null
     and late_post_id is null and variant_status = 'active'
     and nullif(btrim(coalesce(image_url, '')), '') is not null
     and media_not_ready_reason is null
     and public.calendar_generated_snapshot_matches(c, p_expected)
     and (
       p_expected is null
       or (
         -- PORTAL VISIBLE-CARD SNAPSHOT COMPARE (ECHO_VERIFIED_APPROVAL_PROOF
         -- CONTRACT): the exact card the human tapped. caption is compared
         -- null-safely and without trimming; media_url is the FINAL image_url
         -- string (no fetching -- the bound URL IS the
         -- identity); day_key is the visible post_date; format is the
         -- effective format ('feed' when unset); platform is the canonical
         -- account platform. GBP also compares every raw digest-bound field.
         -- ALL of it is checked in THIS SAME UPDATE that
         -- stamps status+digest: a stale snapshot matches zero rows, flips
         -- nothing and stamps nothing -- Echo answers 409
         -- review_refresh_required and the card re-enters review.
         c.caption is not distinct from p_expected->>'caption'
         and c.image_url = p_expected->>'media_url'
         and c.post_date::text
           = coalesce(p_expected->>'day_key', '')
         and coalesce(nullif(lower(btrim(c.format)), ''), 'feed')
           = coalesce(nullif(lower(btrim(coalesce(p_expected->>'format', ''))), ''), 'feed')
         and coalesce(nullif(lower(btrim(c.account)), ''), '')
           = lower(btrim(coalesce(p_expected->>'platform', '')))
         and (lower(btrim(c.account)) <> 'googlebusiness'
              or public.calendar_gbp_approval_snapshot(c) = p_expected->'gbp_proof')
       )
     )
  returning *;
end;
$function$
;

CREATE OR REPLACE FUNCTION public.calendar_recover_unproved_approval(p_row_id uuid, p_gym_id text, p_expected jsonb)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_row public.content_calendar%rowtype;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before any row lock (the FOR UPDATE below).
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_expected is null then return; end if;
  select * into v_row from public.content_calendar c
   where c.id = p_row_id and c.gym_id = p_gym_id
   for update of c;
  if not found or not public.calendar_generated_snapshot_matches(v_row, p_expected)
      or v_row.status is distinct from 'approved'
      or v_row.published_at is not null or v_row.late_post_id is not null
      or v_row.variant_status is distinct from 'active'
      or v_row.approval_kind is not null or v_row.approved_by is not null
      or v_row.approved_at is not null
      or v_row.media_not_ready_reason is not null
      or nullif(btrim(coalesce(v_row.image_url, '')), '') is null
      or (v_row.approval_digest is not null and
          v_row.approval_digest is distinct from public.calendar_approval_digest(v_row))
      or v_row.caption is distinct from p_expected->>'caption'
      or v_row.image_url is distinct from p_expected->>'media_url'
      or v_row.post_date::text is distinct from p_expected->>'day_key'
      or coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
         is distinct from coalesce(nullif(lower(btrim(coalesce(p_expected->>'format', ''))), ''), 'feed')
      or coalesce(nullif(lower(btrim(v_row.account)), ''), '')
         is distinct from lower(btrim(coalesce(p_expected->>'platform', '')))
      or (lower(btrim(v_row.account)) = 'googlebusiness' and
          public.calendar_gbp_approval_snapshot(v_row)
            is distinct from p_expected->'gbp_proof') then
    return;
  end if;
  if v_row.approval_digest is null then
    return query
      update public.content_calendar c
         set approval_digest = public.calendar_approval_digest(c)
       where c.id = p_row_id
      returning c.*;
  else
    return next v_row;
  end if;
end;
$function$
;
CREATE OR REPLACE FUNCTION public.calendar_stamp_verified_approval(p_gym_id uuid, p_calendar_id uuid, p_clerk_actor_id text, p_echo_approval_digest text)
 RETURNS TABLE(id uuid, gym_id uuid, clerk_actor_id text, approval_digest text)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_actor text := nullif(btrim(coalesce(p_clerk_actor_id, '')), '');
  v_digest text := nullif(btrim(coalesce(p_echo_approval_digest, '')), '');
  v_gym_count integer;
  v_mapping_count integer;
  v_mapped_gym uuid;
  v_row public.content_calendar%rowtype;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the FOR SHARE mapping lock and the FOR UPDATE row
  -- lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_gym_id is null or p_calendar_id is null
      or v_actor is null or v_digest is null then
    return;  -- no trusted actor or no Echo digest: zero rows, no stamp
  end if;

-- Exact gym resolution: the gyms row by id, excluding archived/dup rows.
  select count(*) into v_gym_count
    from public.gyms g
   where g.id = p_gym_id
     and lower(coalesce(g.slug, '')) not like '%archived%'
     and lower(coalesce(g.slug, '')) not like '%-dup%'
     and lower(coalesce(g.name, '')) not like '%archived%'
     and lower(coalesce(g.name, '')) not like '%do not use%';
  if v_gym_count is distinct from 1 then
    return;
  end if;

  -- The server-owned token mapping is the only account-key authority. Count
  -- all rows for this key, so duplicates and cross-tenant aliases fail closed.
  select count(*) into v_mapping_count
    from public.echo_intake_tokens t
    join public.content_calendar c on c.gym_id = t.echo_account_key
   where c.id = p_calendar_id and t.gym_id = p_gym_id;
  if v_mapping_count is distinct from 1 or
      (select count(*) from public.echo_intake_tokens t
        join public.content_calendar c on c.gym_id = t.echo_account_key
       where c.id = p_calendar_id) is distinct from 1 then
    return;
  end if;
  -- Hold the authoritative mapping through the stamp. The unique index above
  -- prevents another token row from taking this key while this row is locked.
  select t.gym_id into v_mapped_gym
    from public.echo_intake_tokens t
    join public.content_calendar c on c.gym_id = t.echo_account_key
   where c.id = p_calendar_id and t.gym_id = p_gym_id
   for share of t;
  if v_mapped_gym is distinct from p_gym_id then
    return;
  end if;

  -- Lock the exact row and require it to belong to THIS gym's canonical
  -- account key, be active/unpublished/approved.
  select * into v_row from public.content_calendar c
   where c.id = p_calendar_id
     and c.status = 'approved' and c.published_at is null
     and c.late_post_id is null and c.variant_status = 'active'
     and exists (select 1 from public.echo_intake_tokens t
                  where t.gym_id = p_gym_id
                    and t.echo_account_key = c.gym_id)
   for update of c;
  if not found then
    return;
  end if;

  -- Generated approvals belong to an authenticated client owner, never staff.
  -- The SQL boundary rechecks the actor's persisted role and exact gym relationship
  -- so another trusted service-role route cannot stamp its own coach/staff actor.
  -- Session authentication still belongs to the calling server; SQL cannot verify
  -- a Clerk session from a service-role request or prevent privileged impersonation.
  if v_row.creative_origin = 'generated' then
    if (select count(*) from public.app_users u
        join public.gym_assignments ga on ga.app_user_id = u.id
        where u.clerk_user_id = v_actor and u.role = 'client'
          and ga.gym_id = p_gym_id and ga.relationship = 'client_owner') <> 1 then
      return;
    end if;
    -- Hold role and relationship until the stamp completes. No advisory lock
    -- is added here; the existing #379 G/C entry sequence has already run.
    perform u.id from public.app_users u
      join public.gym_assignments ga on ga.app_user_id = u.id
      where u.clerk_user_id = v_actor and u.role = 'client'
        and ga.gym_id = p_gym_id and ga.relationship = 'client_owner'
      for share of u, ga;
    if not found then return; end if;
  end if;

  -- Exact digest match: stored = recomputed from the locked row = Echo's.
  -- Only an unproved approval may be stamped. Concurrent portal taps must
  -- never replace the first authenticated approver's actor attribution.
  if v_row.approval_kind is not null or v_row.approved_by is not null
      or v_row.approved_at is not null
      or v_row.approval_digest is null
      or v_row.approval_digest is distinct from
         public.calendar_approval_digest(v_row)
      or v_row.approval_digest is distinct from v_digest then
    return;
  end if;

  return query
    update public.content_calendar c
       set approval_kind = 'human',
           approved_by = v_actor,
           approved_at = now()
     where c.id = p_calendar_id
    returning c.id, p_gym_id, v_actor, c.approval_digest;
end;
$function$
;
commit;
