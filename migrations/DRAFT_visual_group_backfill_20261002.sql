-- DRAFT / UNAPPLIED. Apply schema + trigger before these service-role RPCs.
-- Backfill is default dry-run, refuses enforced gyms, locks source rows, and
-- rolls back ALL dry-run writes. Production application requires approval.
-- Per-row savepoints turn trigger conflicts (sticky ambiguity, immutable
-- history) into held review events; ambiguous reservations are NEVER
-- promoted to published without evidence-based reconciliation.
-- Rollback: DROP visual_group_backfill_gym(text,boolean) and
-- visual_group_conflict_report(text). The scene-link table, component helper
-- and union RPC roll back with the schema migration; link rows are permanent
-- evidence after activation, never a data-rollback target. Preserve additive permanent usage/history.
-- Do not delete historical published usage as a data rollback.

create or replace function public.visual_group_backfill_gym(
  p_gym_id text,p_dry_run boolean default true
) returns jsonb language plpgsql security definer set search_path = public as $$
declare r public.content_calendar; a record; g text; n integer; l public.visual_group_usage_ledger%rowtype;
  day date; is_pub boolean; is_ambiguous boolean; blocked boolean; v_reason text; review_details text;
  v_amb_hold boolean; l_amb boolean;
  report jsonb:='[]'; grouped integer:=0; held integer:=0; finalized integer:=0; reserved integer:=0;
  v_tenant text;
begin
  if nullif(btrim(p_gym_id),'') is null or p_dry_run is null then raise exception 'gym and dry-run required'; end if;
  -- Normalize the key once: the tenant resolution above tolerated surrounding
  -- whitespace, but the calendar scan below previously compared the raw
  -- argument and silently backfilled zero rows while reporting success.
  p_gym_id:=btrim(p_gym_id);
  -- Fail closed on unmapped calendar keys (e.g. retired key
  -- zz-retired-20260904-f574c06c): no groups, aliases, ledger rows or events
  -- are ever minted for a key with no canonical tenant mapping.
  v_tenant := public.visual_group_tenant_strict(p_gym_id)::text;
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
      v_reason:=null; blocked:=false; g:=null; v_amb_hold:=false; l_amb:=false;
      -- Per-row savepoint: an alias conflict rolls back ALL new bindings/groups
      -- from this row before writing its review event/hold.
      begin
        select count(distinct x.group_key),min(x.group_key) into n,g
          from public.visual_group_row_aliases(r) x1 join public.visual_group_alias x
          on x.gym_id=v_tenant and x.alias_kind=x1.alias_kind and x.alias_value=x1.alias_value;
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
      exception when check_violation or unique_violation then
        v_reason:=sqlerrm; blocked:=true;
        -- A conflicting or unknown identity behind an ambiguous reservation
        -- is still an ambiguous hold: keep the review event in the report's
        -- ambiguous queue (with claim evidence) instead of a generic
        -- 'backfill' hold that the unresolved-history filter cannot see.
        v_amb_hold:=is_ambiguous or exists(select 1 from public.visual_group_usage_ledger la
          where la.gym_id=v_tenant and la.group_key=g and la.state<>'released' and la.ambiguous);
        g:=null;
      end;
      -- An unresolved row or scene review is not publication evidence merely
      -- because an older writer stamped status=published. This includes an
      -- identity-unknown runtime ambiguity later hydrated with exact aliases.
      if not blocked and public.visual_group_row_review_pending(r) then
        blocked:=true; v_reason:='visual_group_scene_review_required';
      end if;
      if not blocked then
      -- P1: per-row savepoint around ALL ledger/sibling/calendar mutations.
      -- A sticky-ambiguity or immutable-history trigger raise for one row
      -- becomes a held review event; preceding rows are never lost.
      begin
        perform 1 from public.visual_group where gym_id=v_tenant and group_key=g for update;
        -- Linked same-scene authority: a human-confirmed union shares one date
        -- across the whole component. A backfilled row whose date disagrees
        -- with a linked member's active/published claim is unsafe and held
        -- for review; existing history is reported, never rewritten. The
        -- per-row savepoint turns this raise into one held review event.
        if exists(select 1 from public.visual_group_usage_ledger sl
          where sl.gym_id=v_tenant and sl.group_key<>g and sl.state<>'released'
            and sl.reserved_date is distinct from day
            and sl.group_key in (select public.visual_group_scene_members(v_tenant,g))) then
          raise exception 'linked_scene_cross_date_hold' using errcode='23514';
        end if;
        select * into l from public.visual_group_usage_ledger where gym_id=v_tenant and group_key=g for update;
        l_amb:=found and l.ambiguous;
        if is_pub then
          -- A status-only historical publication still becomes permanent. If
          -- both dates are unknown, NULL pins published usage to NO reusable
          -- date; the guard's IS DISTINCT FROM blocks every dated claim.
          if not found then
            insert into public.visual_group_usage_ledger
              (gym_id,group_key,reserved_date,calendar_row_id,channel,state,published_at)
              values(v_tenant,g,day,r.id,r.account,'published',r.published_at);
            finalized:=finalized+1;
          elsif l.state<>'published' then
            -- P1: never promote an ambiguous reservation to permanently
            -- published without evidence-based reconciliation, and never
            -- re-date it (the immutable ambiguous identity/date trigger
            -- would raise on a cross-day UPDATE). Preserve the original
            -- ambiguous reservation and hold this row for review instead of
            -- fabricating publication or aborting the whole gym backfill.
            if l.ambiguous and not public.visual_group_group_reconciled(v_tenant,g) then
              blocked:=true; v_amb_hold:=true;
              v_reason:='ambiguous_reservation_needs_provider_confirmation';
            else
              update public.visual_group_usage_ledger set state='published',reserved_date=day,
                published_at=r.published_at,released_at=null where gym_id=v_tenant and group_key=g;
              finalized:=finalized+1;
            end if;
          elsif l.reserved_date is distinct from day then
            v_reason:='historical_cross_date_visual_repeat';
          end if;
          -- Persist historical row/date evidence without editing published
          -- calendar history. Distinct history survives future row deletion.
          -- A row held for ambiguous-provider confirmation is NOT evidence.
          if not blocked and not exists(select 1 from public.visual_group_member_event where gym_id=v_tenant and group_key=g
            and action='confirmed' and actor='backfill_published' and alias_value=r.id::text) then
            insert into public.visual_group_member_event(gym_id,group_key,alias_value,action,actor,reason)
              values(v_tenant,g,r.id::text,'confirmed','backfill_published',
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
              values(v_tenant,g,day,r.id,r.account,'reserved',is_ambiguous); reserved:=reserved+1;
          elsif l.state='released' then
            update public.visual_group_usage_ledger set state='reserved',reserved_date=day,
              released_at=null,reserved_at=now(),ambiguous=ambiguous or is_ambiguous where gym_id=v_tenant and group_key=g;
            reserved:=reserved+1;
          end if;
          insert into public.visual_group_usage_sibling(gym_id,group_key,calendar_row_id,channel,ambiguous,original_claim_token,original_provider_post_id,original_image_url)
            values(v_tenant,g,r.id,r.account,is_ambiguous,r.publish_claim_token,r.late_post_id,r.image_url) on conflict(gym_id,group_key,calendar_row_id)
            do update set state='active',released_at=null,channel=excluded.channel,
              ambiguous=public.visual_group_usage_sibling.ambiguous or excluded.ambiguous,
              attempt_id=case when not public.visual_group_usage_sibling.ambiguous and excluded.ambiguous then gen_random_uuid() else public.visual_group_usage_sibling.attempt_id end,
              original_claim_token=case when not public.visual_group_usage_sibling.ambiguous and excluded.ambiguous then excluded.original_claim_token else coalesce(public.visual_group_usage_sibling.original_claim_token,excluded.original_claim_token) end,
              original_provider_post_id=case when not public.visual_group_usage_sibling.ambiguous and excluded.ambiguous then excluded.original_provider_post_id else coalesce(public.visual_group_usage_sibling.original_provider_post_id,excluded.original_provider_post_id) end,
              original_image_url=case when not public.visual_group_usage_sibling.ambiguous and excluded.ambiguous then excluded.original_image_url else coalesce(public.visual_group_usage_sibling.original_image_url,excluded.original_image_url) end;
          if is_ambiguous then
            update public.visual_group_usage_ledger set ambiguous=true
              where gym_id=v_tenant and group_key=g and state<>'published' and not ambiguous;
          end if;
        end if;
        if not is_pub then
          update public.content_calendar set visual_group_key=g,
            media_not_ready_reason=case when media_not_ready_reason in
              ('visual_group_identity_unresolved','visual_group_date_unresolved','visual_group_scene_review_required','cross_date_media_repeat_needs_new_visual')
              then null else media_not_ready_reason end
            where id=r.id and (visual_group_key is distinct from g or media_not_ready_reason in
              ('visual_group_identity_unresolved','visual_group_date_unresolved','visual_group_scene_review_required','cross_date_media_repeat_needs_new_visual'));
          if is_ambiguous and not exists(select 1 from public.visual_group_member_event e
            where gym_id=v_tenant and alias_value=r.id::text and actor='backfill_ambiguous' and action='confirmed') then
            insert into public.visual_group_member_event(gym_id,group_key,alias_value,action,actor,reason)
              values(v_tenant,g,r.id::text,'confirmed','backfill_ambiguous','identity hydrated; ambiguous reservation retained');
          end if;
        end if;
        if not blocked then grouped:=grouped+1; end if;
      exception when check_violation or raise_exception or unique_violation then
        -- Recoverable per-row conflict: roll back ONLY this row's
        -- ledger/sibling/calendar writes and record a review hold below.
        blocked:=true; v_reason:=sqlerrm; v_amb_hold:=v_amb_hold or l_amb or is_ambiguous;
      end;
      end if;
      -- Any held row whose group reservation or own markers are ambiguous is
      -- reported in the ambiguous queue (with claim evidence), even when the
      -- block itself was an alias/scene/date conflict.
      v_amb_hold:=v_amb_hold or l_amb or is_ambiguous;
      if blocked then
        held:=held+1;
        review_details:=case when is_ambiguous or v_amb_hold then jsonb_build_object('reason',v_reason,
          'publish_claim_token',r.publish_claim_token,'late_post_id',r.late_post_id,
          'image_url',r.image_url,'post_date',r.post_date)::text else v_reason end;
        -- Never touch published rows; unresolved history is persistent review
        -- evidence. Unknown groups are legal ONLY for review_hold events.
        if not is_pub then
          -- The guard trigger also refuses identity/status changes on
          -- unreconciled ambiguous rows; keep even the hold write per-row
          -- recoverable so one stubborn row cannot abort the gym backfill.
          begin
            update public.content_calendar set
              visual_group_key=g,
              media_not_ready_reason=coalesce(media_not_ready_reason,v_reason),
              status=case when status='approved' then 'pending' else status end
            where id=r.id;
          exception when check_violation or raise_exception or unique_violation then
            null; -- review_hold event below is the durable conflict record
          end;
        end if;
        if not exists(select 1 from public.visual_group_member_event where gym_id=v_tenant
          and action='review_hold' and actor=case when v_amb_hold then 'backfill_ambiguous_review' when is_pub then 'backfill_published_review' when is_ambiguous then 'backfill_ambiguous_review' else 'backfill' end and alias_value=r.id::text and reason=review_details) then
          insert into public.visual_group_member_event(gym_id,group_key,alias_value,action,actor,reason)
            values(v_tenant,g,r.id::text,'review_hold',case when v_amb_hold then 'backfill_ambiguous_review' when is_pub then 'backfill_published_review' when is_ambiguous then 'backfill_ambiguous_review' else 'backfill' end,review_details);
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
          where l.gym_id=public.visual_group_tenant_id(rows.gym_id)::text and l.group_key=rows.resolved_group and l.state<>'released' and l.reserved_date=rows.post_date))) as uncovered_unheld_rows
    from rows group by gym_id
  ), conflicts as (
    select gym_id,resolved_group,array_agg(distinct post_date order by post_date) as dates,
      array_agg(id order by post_date,id) as row_ids from rows
    where (active or is_pub) and resolved_group is not null group by gym_id,resolved_group
    having count(distinct post_date)>1
  ) select jsonb_build_object('per_gym',coalesce((select jsonb_agg(to_jsonb(c) order by gym_id) from coverage c),'[]'::jsonb),
    'cross_date_conflicts',coalesce((select jsonb_agg(to_jsonb(c) order by gym_id,resolved_group) from conflicts c),'[]'::jsonb),
    'scene_cross_date_conflicts',coalesce((select jsonb_agg(to_jsonb(sc) order by sc.gym_id,sc.component) from (
      select gym_id,component,
        array_agg(distinct group_key order by group_key) as group_keys,
        array_agg(distinct coalesce(reserved_date,'0001-01-01'::date) order by coalesce(reserved_date,'0001-01-01'::date)) as dates,
        array_agg(distinct state order by state) as states
      from (
        select l.gym_id,l.group_key,l.reserved_date,l.state,
          (select array_agg(m order by m) from public.visual_group_scene_members(l.gym_id,l.group_key) members(m)) as component
        from public.visual_group_usage_ledger l
        where l.state<>'released'
          and (p_gym_id is null or l.gym_id=public.visual_group_tenant_id(p_gym_id)::text)
      ) comp
      where array_length(component,1)>1
      group by gym_id,component
      having count(distinct coalesce(reserved_date,'0001-01-01'::date))>1
    ) sc),'[]'::jsonb),
    'unresolved_published_history',coalesce((select jsonb_agg(to_jsonb(e) - 'hold_rank' order by e.id) from (
      select e.*,row_number() over (partition by e.alias_value order by e.id desc) as hold_rank from public.visual_group_member_event e
      where action='review_hold' and actor='backfill_published_review' and (p_gym_id is null or gym_id=public.visual_group_tenant_id(p_gym_id)::text)
      and not exists(select 1 from public.visual_group_member_event resolved where resolved.gym_id=e.gym_id
        and resolved.alias_value=e.alias_value and resolved.actor='backfill_published' and resolved.action='confirmed' and resolved.id>e.id)
    ) e where hold_rank=1),'[]'::jsonb),
    'unresolved_ambiguous_history',coalesce((select jsonb_agg(to_jsonb(e) - 'hold_rank' order by e.id) from (
      select e.*,row_number() over (partition by e.alias_value order by e.id desc) as hold_rank from public.visual_group_member_event e
      where action='review_hold' and actor in ('backfill_ambiguous_review','runtime_ambiguous_review') and (p_gym_id is null or gym_id=public.visual_group_tenant_id(p_gym_id)::text)
      and not exists(select 1 from public.visual_group_member_event resolved where resolved.gym_id=e.gym_id
        and resolved.alias_value=e.alias_value and resolved.actor=case when e.actor='runtime_ambiguous_review' then 'runtime_ambiguous_reconciled' else 'backfill_ambiguous' end and resolved.action='confirmed' and resolved.id>e.id)
      and not exists(select 1 from public.visual_group_reconciliation r where r.gym_id=e.gym_id and e.id=any(r.hold_event_ids))
    ) e where hold_rank=1),'[]'::jsonb),
    'activation_ready',false,
    'activation_blockers',jsonb_build_array('tenant alias registration of every covered calendar key and owner ruling on unmapped keys','owner review of reported scene cross-date conflicts','transactional activation write barrier and locked coverage recheck'),
    'policy','Exact identities only. pHash 7-30 requires review. JCK_6328/JCK_6331 distance 28 may share a group only after manual confirmation. Published history is immutable.');
$$;
revoke all on function public.visual_group_backfill_gym(text,boolean) from public,anon,authenticated;
grant execute on function public.visual_group_backfill_gym(text,boolean) to service_role;
revoke all on function public.visual_group_conflict_report(text) from public,anon,authenticated;
grant execute on function public.visual_group_conflict_report(text) to service_role;
