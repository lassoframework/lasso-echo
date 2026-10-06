-- DRAFT / UNAPPLIED / OFF. Read-only fleet history diagnostic, not activation.
-- Apply after the A2 scene authority and calendar transaction drafts.
-- A snapshot is not a clearance receipt: backfill/activation remain unavailable.
begin;
do $$
begin
  if current_database() <> 'echo_scene_ledger_test' or inet_server_addr() is not null then
    raise exception 'SCRATCH ONLY: scene historical backfill and activation remain unresolved';
  end if;
  if to_regprocedure('public.visual_scene_enforcement_on(text)') is null
     or to_regprocedure('public.visual_global_history_coverage()') is null then
    raise exception 'Apply scene authority and exact-byte history prerequisites first';
  end if;
end;
$$;

-- All tenants and dates are included. Archived published/ambiguous rows remain
-- history; released/orphan members retain their permanent once-used obligation.
-- This function writes no holds or receipts and cannot clear any existing hold.
create function public.visual_scene_history_preflight()
-- pg_temp is explicitly last: SECURITY DEFINER must not inherit implicit
-- temporary-schema precedence. Persistent relations/functions are also qualified.
returns jsonb language sql stable security definer set search_path=pg_catalog,public,pg_temp as $$
with rows as materialized (
  select c.*, public.visual_group_tenant_id(c.gym_id)::text as tenant,
    coalesce(c.visual_group_key, public.visual_group_resolve_row(c)) as resolved_group
  from public.content_calendar c
  where c.status='published' or c.published_at is not null
    or public.visual_group_row_active(c) or public.visual_group_row_ambiguous(c)
), bound as materialized (
  select r.id,r.tenant,r.resolved_group,r.post_date,r.source_media_url,
    r.status,r.published_at,r.image_url,r.thumbnail_url,d.object_role,d.exact_url,
    k.candidate_id,k.fingerprint,k.phash,k.evidence,
    exists(select 1 from public.visual_scene_owner_phash_receipt p
      join public.visual_global_object_attestation a
        on a.tenant_id=p.tenant_id and a.group_key=p.group_key
        and a.exact_url=p.exact_url and a.fingerprint=p.fingerprint
      where p.receipt_id::text=k.evidence->>'owner_phash_receipt'
        and p.tenant_id=r.tenant and p.group_key=r.resolved_group
        and p.object_role=d.object_role and p.exact_url=d.exact_url
        and p.fingerprint=k.fingerprint and p.phash=k.phash
        and p.byte_length=a.byte_length and p.algorithm='echo-dct-phash64-v1') as owner_bound
  from rows r
  join public.content_calendar original on original.id=r.id
  left join lateral public.visual_scene_row_delivered_object(original) d on true
  left join public.visual_scene_candidate k on k.tenant_id=r.tenant
    and k.group_key=r.resolved_group and k.object_role=d.object_role
    and k.exact_url=d.exact_url
), issues as (
  select 'calendar'::text as scope, c.calendar_row_id::text as identity,
    c.issue as reason from public.visual_global_coverage() c where c.issue<>'ready'
  union all
  -- The stored-object adapter picks a distinct poster, but a calendar row
  -- alone cannot prove which image/poster the provider actually delivered.
  -- Never infer photo-first precedence or clear mixed-object history here.
  select 'calendar',b.id::text,'delivery_precedence_review_required'
    from bound b where nullif(btrim(b.image_url),'') is not null
      and nullif(btrim(b.thumbnail_url),'') is not null
      and btrim(b.thumbnail_url) is distinct from btrim(b.image_url)
  union all
  select 'occupied',concat_ws('/',o.tenant_id,o.group_key,o.used_date,o.phash),
    'occupied_scene_without_permanent_member'
    from public.visual_scene_phash_occupied o where not exists(
      select 1 from public.visual_global_usage_member m
      where m.tenant_id=o.tenant_id and m.group_key=o.group_key
        and m.fingerprint=o.fingerprint and m.used_date=o.used_date)
  union all
  select 'exact_history',concat_ws('/',h.tenant_id,h.group_key,h.fingerprint),h.issue
    from public.visual_global_history_coverage() h where h.issue<>'ready'
  union all
  select 'calendar',b.id::text, 'historical_source_review_required'
    from bound b where nullif(btrim(b.source_media_url),'') is null
  union all
  select 'calendar',b.id::text,'displayed_scene_owner_evidence_missing'
    from bound b where b.candidate_id is null or not b.owner_bound
  union all
  select 'calendar',b.id::text,'scene_usage_date_unknown'
    from bound b where b.post_date is null
  union all
  select 'calendar',b.id::text,'displayed_scene_occupancy_missing'
    from bound b where b.candidate_id is not null and not exists(
      select 1 from public.visual_scene_phash_occupied o
      where o.tenant_id=b.tenant and o.group_key=b.resolved_group
        and o.used_date=b.post_date and o.fingerprint=b.fingerprint and o.phash=b.phash)
  union all
  -- Conservatively require each permanent exact-byte member to retain matching
  -- owner-computed scene occupancy. A source-only member without verified
  -- displayed lineage blocks; no candidate or source alias becomes use here.
  select 'member',concat_ws('/',m.tenant_id,m.group_key,m.fingerprint),
    'permanent_member_scene_coverage_missing'
    from public.visual_global_usage_member m where not exists(
      select 1 from public.visual_scene_phash_occupied o
      join public.visual_scene_candidate k on k.tenant_id=o.tenant_id
        and k.group_key=o.group_key and k.fingerprint=o.fingerprint and k.phash=o.phash
      join public.visual_scene_owner_phash_receipt p
        on p.receipt_id::text=k.evidence->>'owner_phash_receipt'
        and p.tenant_id=k.tenant_id and p.group_key=k.group_key
        and p.object_role=k.object_role and p.exact_url=k.exact_url
        and p.fingerprint=k.fingerprint and p.phash=k.phash
      join public.visual_global_object_attestation a
        on a.tenant_id=p.tenant_id and a.group_key=p.group_key
        and a.exact_url=p.exact_url and a.fingerprint=p.fingerprint
      where o.tenant_id=m.tenant_id and o.group_key=m.group_key
        and o.fingerprint=m.fingerprint and o.used_date=m.used_date
        and p.byte_length=a.byte_length and p.algorithm='echo-dct-phash64-v1')
  union all
  -- Full pair scan uses the same <=6 near-frame / 7..30 uncertain bands as
  -- admission. Only exact tenant/group/date siblings are exempt. Approved
  -- holds are never general history clearance and cannot hide these conflicts.
  select 'occupied_pair', concat_ws('/',a.tenant_id,a.group_key,a.used_date,a.phash,
    b.tenant_id,b.group_key,b.used_date,b.phash),
    case when public.visual_scene_hamming(a.phash,b.phash)<=6
      then 'near_frame_history_conflict' else 'uncertain_history_review_required' end
    from public.visual_scene_phash_occupied a
    join public.visual_scene_phash_occupied b
      on (a.tenant_id,a.group_key,a.used_date,a.phash) <
         (b.tenant_id,b.group_key,b.used_date,b.phash)
    where not (a.tenant_id=b.tenant_id and a.group_key=b.group_key and a.used_date=b.used_date)
      and public.visual_scene_hamming(a.phash,b.phash)<=30
  union all
  select 'hold',h.hold_id::text,'unresolved_scene_review_hold'
    from public.visual_scene_review_hold h where h.state='open'
), distinct_issues as (select distinct scope,identity,reason from issues), tenants as (
  select tenant from bound union select tenant_id from public.visual_global_usage_member
  union select tenant_id from public.visual_scene_phash_occupied
), tenant_counts as (
  select t.tenant,
    (select count(*) from bound b where b.tenant is not distinct from t.tenant) as calendar_rows,
    (select count(*) from bound b where b.tenant is not distinct from t.tenant
      and nullif(btrim(b.source_media_url),'') is null) as unknown_source_rows,
    (select count(*) from bound b where b.tenant is not distinct from t.tenant
      and (b.candidate_id is null or not b.owner_bound)) as missing_display_evidence_rows,
    (select count(*) from public.visual_global_usage_member m where m.tenant_id=t.tenant) as permanent_members,
    (select count(*) from public.visual_global_usage_member m where m.tenant_id=t.tenant
      and m.state='released') as retained_released_members,
    (select count(*) from public.visual_global_usage_member m where m.tenant_id=t.tenant
      and not exists(select 1 from public.content_calendar c where c.id=m.calendar_row_id)) as orphan_members
  from tenants t
)
select jsonb_build_object(
  'kind','scene_history_preflight_snapshot',
  'snapshot_at',statement_timestamp(),
  'activation_available',false,'clearance_authorized',false,
  'coverage_complete', not exists(select 1 from distinct_issues)
    and (exists(select 1 from rows) or exists(select 1 from public.visual_global_usage_member)),
  'calendar_rows',(select count(*) from rows),
  'permanent_members',(select count(*) from public.visual_global_usage_member),
  'tenant_counts',coalesce((select jsonb_agg(to_jsonb(t) order by t.tenant nulls first)
    from tenant_counts t),'[]'::jsonb),
  'issues',coalesce((select jsonb_agg(jsonb_build_object(
    'scope',scope,'identity',identity,'reason',reason) order by scope,identity,reason)
    from distinct_issues),'[]'::jsonb),
  'activation_blocker','verified historical occupied backfill and transactional activation remain unavailable',
  'note','Read-only stored-object diagnostic snapshot, not provider delivery precedence proof. Recheck under a writer barrier before any future activation; this report never clears history or review holds.')
$$;
revoke all on function public.visual_scene_history_preflight() from public,anon,authenticated;
grant execute on function public.visual_scene_history_preflight() to service_role;
commit;
