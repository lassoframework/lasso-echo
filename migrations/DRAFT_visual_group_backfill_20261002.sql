-- DRAFT / UNAPPLIED. Apply schema + trigger before these service-role RPCs.
-- Backfill is default dry-run, refuses enforced gyms, locks source rows, and
-- rolls back ALL dry-run writes. Production application requires approval.
-- Rollback: DROP visual_group_backfill_gym(text,boolean) and
-- visual_group_conflict_report(text). Preserve additive permanent usage/history.
-- Do not delete historical published usage as a data rollback.

create or replace function public.visual_group_backfill_gym(
  p_gym_id text,p_dry_run boolean default true
) returns jsonb language plpgsql security definer set search_path = public as $$
declare r public.content_calendar; a record; g text; n integer; l public.visual_group_usage_ledger%rowtype;
  day date; is_pub boolean; is_ambiguous boolean; blocked boolean; v_reason text;
  report jsonb:='[]'; grouped integer:=0; held integer:=0; finalized integer:=0; reserved integer:=0;
begin
  if nullif(btrim(p_gym_id),'') is null or p_dry_run is null then raise exception 'gym and dry-run required'; end if;
  -- One backfill owner per gym. Enforcement remains OFF; never toggle it here.
  perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array('visual_backfill',p_gym_id)::text,0));
  if public.visual_group_enforcement_on(p_gym_id) then raise exception 'backfill requires enforcement OFF'; end if;
  begin
    for r in select * from public.content_calendar where gym_id=p_gym_id
      and (status='published' or published_at is not null or public.visual_group_row_active(content_calendar) or public.visual_group_row_ambiguous(content_calendar))
      order by (status='published' or published_at is not null) desc,
        published_at nulls last,post_date nulls last,id for update loop
      is_pub:=r.status='published' or r.published_at is not null;
      is_ambiguous:=public.visual_group_row_ambiguous(r);
      day:=coalesce(r.post_date,(r.published_at at time zone 'UTC')::date);
      v_reason:=null; blocked:=false; g:=null;
      -- Per-row savepoint: an alias conflict rolls back ALL new bindings/groups
      -- from this row before writing its review event/hold.
      begin
        select count(distinct x.group_key),min(x.group_key) into n,g
          from public.visual_group_row_aliases(r) x1 join public.visual_group_alias x
          on x.gym_id=p_gym_id and x.alias_kind=x1.alias_kind and x.alias_value=x1.alias_value;
        if n>1 then raise exception 'visual_group_alias_conflict' using errcode='23514'; end if;
        if not exists(select 1 from public.visual_group_row_aliases(r)) then
          raise exception 'visual_group_identity_unresolved' using errcode='23514';
        end if;
        for a in select * from public.visual_group_row_aliases(r) order by alias_kind,alias_value loop
          g:=public.visual_group_register_alias(p_gym_id,a.alias_kind,a.alias_value,g);
        end loop;
        if public.visual_group_resolve_row(r) is null then
          raise exception 'visual_group_identity_unresolved' using errcode='23514';
        end if;
      exception when check_violation then
        v_reason:=sqlerrm; g:=null; blocked:=true;
      end;
      if not blocked and public.visual_group_row_review_pending(r) and not is_pub then
        blocked:=true; v_reason:='visual_group_scene_review_required';
      end if;
      if not blocked then
        perform 1 from public.visual_group where gym_id=p_gym_id and group_key=g for update;
        select * into l from public.visual_group_usage_ledger where gym_id=p_gym_id and group_key=g for update;
        if is_pub then
          -- A status-only historical publication still becomes permanent. If
          -- both dates are unknown, NULL pins published usage to NO reusable
          -- date; the guard's IS DISTINCT FROM blocks every dated claim.
          if not found then
            insert into public.visual_group_usage_ledger
              (gym_id,group_key,reserved_date,calendar_row_id,channel,state,published_at)
              values(p_gym_id,g,day,r.id,r.account,'published',r.published_at);
            finalized:=finalized+1;
          elsif l.state<>'published' then
            update public.visual_group_usage_ledger set state='published',reserved_date=day,
              published_at=r.published_at,released_at=null where gym_id=p_gym_id and group_key=g;
            finalized:=finalized+1;
          elsif l.reserved_date is distinct from day then
            v_reason:='historical_cross_date_visual_repeat';
          end if;
          -- Persist historical row/date evidence without editing published
          -- calendar history. Distinct history survives future row deletion.
          if not exists(select 1 from public.visual_group_member_event where gym_id=p_gym_id and group_key=g
            and action='confirmed' and actor='backfill_published' and alias_value=r.id::text) then
            insert into public.visual_group_member_event(gym_id,group_key,alias_value,action,actor,reason)
              values(p_gym_id,g,r.id::text,'confirmed','backfill_published',
                jsonb_build_object('calendar_row_id',r.id,'post_date',day,'published_at',r.published_at)::text);
          end if;
        elsif day is null then
          blocked:=true; v_reason:='visual_group_date_unresolved';
        elsif found and l.state<>'released' and l.reserved_date is distinct from day then
          blocked:=true; v_reason:='cross_date_media_repeat_needs_new_visual';
        else
          if not found then
            insert into public.visual_group_usage_ledger
              (gym_id,group_key,reserved_date,calendar_row_id,channel,state,ambiguous)
              values(p_gym_id,g,day,r.id,r.account,'reserved',is_ambiguous); reserved:=reserved+1;
          elsif l.state='released' then
            update public.visual_group_usage_ledger set state='reserved',reserved_date=day,
              released_at=null,reserved_at=now(),ambiguous=ambiguous or is_ambiguous where gym_id=p_gym_id and group_key=g;
            reserved:=reserved+1;
          end if;
          insert into public.visual_group_usage_sibling(gym_id,group_key,calendar_row_id,channel,ambiguous)
            values(p_gym_id,g,r.id,r.account,is_ambiguous) on conflict(gym_id,group_key,calendar_row_id)
            do update set state='active',released_at=null,channel=excluded.channel,
              ambiguous=public.visual_group_usage_sibling.ambiguous or excluded.ambiguous;
          if is_ambiguous then
            update public.visual_group_usage_ledger set ambiguous=true
              where gym_id=p_gym_id and group_key=g and state<>'published' and not ambiguous;
          end if;
        end if;
        grouped:=grouped+1;
        if not is_pub then
          update public.content_calendar set visual_group_key=g where id=r.id and visual_group_key is distinct from g;
        end if;
      end if;
      if blocked then
        held:=held+1;
        -- Never touch published rows; unresolved history is persistent review
        -- evidence. Unknown groups are legal ONLY for review_hold events.
        if not is_pub then
          update public.content_calendar set
            visual_group_key=g,
            media_not_ready_reason=coalesce(media_not_ready_reason,v_reason),
            status=case when status='approved' then 'pending' else status end
          where id=r.id;
        end if;
        if not exists(select 1 from public.visual_group_member_event where gym_id=p_gym_id
          and action='review_hold' and actor=case when is_pub then 'backfill_published_review' when is_ambiguous then 'backfill_ambiguous_review' else 'backfill' end and alias_value=r.id::text and reason=v_reason) then
          insert into public.visual_group_member_event(gym_id,group_key,alias_value,action,actor,reason)
            values(p_gym_id,g,r.id::text,'review_hold',case when is_pub then 'backfill_published_review' when is_ambiguous then 'backfill_ambiguous_review' else 'backfill' end,v_reason);
        end if;
      end if;
      if v_reason is not null then
        report:=report||jsonb_build_array(jsonb_build_object('row_id',r.id,'group_key',g,'reason',v_reason,'published',is_pub));
      end if;
    end loop;
    if p_dry_run then raise exception 'visual_backfill_dry_run_rollback' using errcode='P0002'; end if;
  exception when no_data_found then
    if sqlerrm<>'visual_backfill_dry_run_rollback' then raise; end if;
  end;
  return jsonb_build_object('gym_id',p_gym_id,'dry_run',p_dry_run,'rows_grouped',grouped,
    'rows_held_for_review',held,'published_rows_finalized',finalized,'active_rows_reserved',reserved,
    'conflict_rows',report);
end;
$$;

create or replace function public.visual_group_conflict_report(p_gym_id text default null)
returns jsonb language sql stable security definer set search_path = public as $$
  with rows as (
    select c.*,public.visual_group_resolve_row(c) as resolved_group,
      (c.status='published' or c.published_at is not null) as is_pub,
      (public.visual_group_row_active(c) or public.visual_group_row_ambiguous(c)) as active
    from public.content_calendar c where p_gym_id is null or c.gym_id=p_gym_id
  ), coverage as (
    select gym_id,count(*) filter(where active or is_pub) as live_rows,
      count(*) filter(where (active or is_pub) and resolved_group is not null) as grouped_rows,
      count(*) filter(where active and media_not_ready_reason is not null) as held_rows,
      count(*) filter(where active and media_not_ready_reason is null and
        (resolved_group is null or post_date is null or not exists(select 1 from public.visual_group_usage_ledger l
          where l.gym_id=rows.gym_id and l.group_key=rows.resolved_group and l.state<>'released' and l.reserved_date=rows.post_date))) as uncovered_unheld_rows
    from rows group by gym_id
  ), conflicts as (
    select gym_id,resolved_group,array_agg(distinct post_date order by post_date) as dates,
      array_agg(id order by post_date,id) as row_ids from rows
    where (active or is_pub) and resolved_group is not null group by gym_id,resolved_group
    having count(distinct post_date)>1
  ) select jsonb_build_object('per_gym',coalesce((select jsonb_agg(to_jsonb(c) order by gym_id) from coverage c),'[]'::jsonb),
    'cross_date_conflicts',coalesce((select jsonb_agg(to_jsonb(c) order by gym_id,resolved_group) from conflicts c),'[]'::jsonb),
    'unresolved_published_history',coalesce((select jsonb_agg(to_jsonb(e) order by id) from public.visual_group_member_event e
      where action='review_hold' and actor='backfill_published_review' and (p_gym_id is null or gym_id=p_gym_id)
      and not exists(select 1 from public.visual_group_member_event resolved where resolved.gym_id=e.gym_id
        and resolved.alias_value=e.alias_value and resolved.actor='backfill_published' and resolved.action='confirmed' and resolved.id>e.id)),'[]'::jsonb),
    'unresolved_ambiguous_history',coalesce((select jsonb_agg(to_jsonb(e) order by id) from public.visual_group_member_event e
      where action='review_hold' and actor in ('backfill_ambiguous_review','runtime_ambiguous_review') and (p_gym_id is null or gym_id=p_gym_id)
      and not exists(select 1 from public.visual_group_member_event resolved where resolved.gym_id=e.gym_id
        and resolved.alias_value=e.alias_value and resolved.actor=case when e.actor='runtime_ambiguous_review' then 'runtime_ambiguous_reconciled' else 'backfill_ambiguous' end and resolved.action='confirmed' and resolved.id>e.id)),'[]'::jsonb),
    'activation_ready',false,
    'activation_blockers',jsonb_build_array('canonical tenant integration','safe scene union/redirect','per-sibling derivative payloads','transactional activation write barrier and locked coverage recheck'),
    'policy','Exact identities only. pHash 7-30 requires review. JCK_6328/JCK_6331 distance 28 may share a group only after manual confirmation. Published history is immutable.');
$$;
revoke all on function public.visual_group_backfill_gym(text,boolean) from public,anon,authenticated;
grant execute on function public.visual_group_backfill_gym(text,boolean) to service_role;
revoke all on function public.visual_group_conflict_report(text) from public,anon,authenticated;
grant execute on function public.visual_group_conflict_report(text) to service_role;
