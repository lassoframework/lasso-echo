-- Read-only production catalog verification for 0611. No gym data is changed.
do $$
declare name text;
begin
  foreach name in array array[
    'echo_source_captures','echo_source_brand_provider_status',
    'echo_source_brand_capability_events','echo_source_brand_bundles',
    'echo_source_brand_observations','echo_source_brand_receipts'
  ] loop
    if to_regclass('public.' || name) is null then
      raise exception '0611 missing table: %', name;
    end if;
    if has_table_privilege('anon','public.' || name,'select')
      or has_table_privilege('authenticated','public.' || name,'select') then
      raise exception '0611 client table access: %', name;
    end if;
  end loop;
  if to_regprocedure('public.echo_source_brand_current_policy(uuid)') is null
    or to_regprocedure('public.echo_source_brand_attest_provider(uuid,text,jsonb,uuid)') is null
    or to_regprocedure('public.echo_source_brand_revalidate(uuid,uuid,text,uuid[],uuid,integer,integer,jsonb,text,jsonb)') is null then
    raise exception '0611 missing source-brand authority';
  end if;
  if has_function_privilege('authenticated','public.echo_source_brand_current_policy(uuid)','execute')
    or has_function_privilege('authenticated','public.echo_source_brand_attest_provider(uuid,text,jsonb,uuid)','execute')
    or has_table_privilege('service_role','public.echo_source_brand_provider_status','insert') then
    raise exception '0611 privilege boundary failed';
  end if;
  if not exists(select 1 from pg_trigger where tgrelid='public.echo_source_captures'::regclass
      and tgname='echo_source_capture_insert_lock' and not tgisinternal and tgenabled <> 'D') then
    raise exception '0611 capture lock trigger missing';
  end if;
  raise notice 'PASS 0611 source-brand catalog and privilege verification';
end $$;

-- Pure synthetic policy checks. No provider attestation or gym rows are written.
do $$
declare g uuid:='11111111-1111-4111-8111-111111111111'; report jsonb; policy jsonb; connected jsonb; mode text;
begin
  report:=jsonb_build_object('gym_id',g,'echo_account_key','catalog-verification','lookup_status','complete','authenticated',true,'observed_at',clock_timestamp(),'instagram',jsonb_build_object('connected',false));
  policy:=echo_source_brand_policy_from_report(g,'catalog-verification','website_only_no_connected_instagram_v2',report);
  if policy->'instagram_connection' is distinct from 'null'::jsonb then raise exception '0611 negative provider proof invalid'; end if;
  begin
    perform echo_source_brand_policy_from_report(g,'catalog-verification','website_and_social_v2',report);
    raise exception '0611 disconnected provider authorized social policy';
  exception when others then if sqlerrm<>'source_policy_invalid' then raise; end if; end;
  begin
    perform echo_source_brand_policy_from_report(g,'catalog-verification','website_only_no_connected_instagram_v2',report-'instagram');
    raise exception '0611 missing connection proof authorized policy';
  exception when others then if sqlerrm<>'provider_status_unavailable' then raise; end if; end;
  foreach connected in array array['"true"'::jsonb,'"false"'::jsonb,'null'::jsonb,'1'::jsonb] loop
    foreach mode in array array['website_and_social_v2','website_only_no_connected_instagram_v2'] loop
      begin
        perform echo_source_brand_policy_from_report(g,'catalog-verification',mode,jsonb_set(report,'{instagram,connected}',connected));
        raise exception '0611 nonboolean connection proof authorized policy';
      exception when others then if sqlerrm<>'provider_status_unavailable' then raise; end if; end;
    end loop;
  end loop;
  report:=jsonb_set(report,'{instagram}',jsonb_build_object('connected',true,'account_id','verified-id','platform_user_id','123','handle','verified'));
  policy:=echo_source_brand_policy_from_report(g,'catalog-verification','website_and_social_v2',report);
  if policy->'instagram_connection'->>'platform_user_id' is distinct from '123' then raise exception '0611 connected provider identity invalid'; end if;
  begin
    perform echo_source_brand_policy_from_report(g,'catalog-verification','website_only_no_connected_instagram_v2',report);
    raise exception '0611 connected provider authorized website-only policy';
  exception when others then if sqlerrm<>'connected_instagram_requires_social' then raise; end if; end;
  raise notice 'PASS 0611 symmetric provider policy verification';
end $$;
