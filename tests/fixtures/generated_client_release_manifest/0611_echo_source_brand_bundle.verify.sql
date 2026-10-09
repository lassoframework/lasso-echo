-- Read-only production catalog verification for 0611. No gym data is changed.
do $$
declare name text; role_name text; privilege_name text; sequence_name text;
  expected_insert boolean; expected_execute boolean; expected record; actual record;
  table_oid oid; function_oid oid;
begin
  foreach name in array array[
    'echo_source_captures','echo_source_brand_provider_status',
    'echo_source_brand_capability_events','echo_source_brand_bundles',
    'echo_source_brand_observations','echo_source_brand_receipts'
  ] loop
    table_oid:=to_regclass('public.'||name);
    if table_oid is null then raise exception '0611 missing table: %',name; end if;
    if not (select relrowsecurity from pg_class where oid=table_oid) then
      raise exception '0611 RLS disabled: %',name;
    end if;
    if exists(select 1 from pg_class c cross join lateral aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a where c.oid=table_oid and a.grantee=0) then
      raise exception '0611 PUBLIC table access: %',name;
    end if;
    foreach role_name in array array['anon','authenticated','service_role'] loop
      expected_insert:=role_name='service_role' and name='echo_source_captures';
      foreach privilege_name in array array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN'] loop
        if has_table_privilege(role_name,table_oid,privilege_name||' WITH GRANT OPTION') then
          raise exception '0611 table grant option: %.% %',role_name,name,privilege_name;
        end if;
        if has_table_privilege(role_name,table_oid,privilege_name) is distinct from
          (role_name='service_role' and (privilege_name='SELECT' or (privilege_name='INSERT' and expected_insert))) then
          raise exception '0611 table ACL mismatch: %.% %',role_name,name,privilege_name;
        end if;
      end loop;
      -- Column ACLs can grant INSERT/UPDATE/REFERENCES even when table ACLs deny.
      foreach privilege_name in array array['SELECT','INSERT','UPDATE','REFERENCES'] loop
        if has_any_column_privilege(role_name,table_oid,privilege_name||' WITH GRANT OPTION') then
          raise exception '0611 column grant option: %.% %',role_name,name,privilege_name;
        end if;
        if has_any_column_privilege(role_name,table_oid,privilege_name) is distinct from
          (role_name='service_role' and (privilege_name='SELECT' or (privilege_name='INSERT' and expected_insert))) then
          raise exception '0611 column ACL mismatch: %.% %',role_name,name,privilege_name;
        end if;
      end loop;
    end loop;
    if not exists(select 1 from pg_trigger where tgrelid=table_oid and tgname=name||'_immutable'
      and tgfoid='public.echo_source_brand_immutable()'::regprocedure and tgtype=27 and tgenabled='O' and not tgisinternal) then
      raise exception '0611 immutable trigger mismatch: %',name;
    end if;
    if (select count(*) from pg_trigger where tgrelid=table_oid and not tgisinternal)<>
      (case when name='echo_source_captures' then 2 else 1 end) then
      raise exception '0611 unexpected trigger: %',name;
    end if;
    sequence_name:=pg_get_serial_sequence('public.'||name,'id');
    if name not in ('echo_source_captures','echo_source_brand_bundles') and sequence_name is null then
      raise exception '0611 identity sequence missing: %',name;
    end if;
    if sequence_name is not null then
      if exists(select 1 from pg_class c cross join lateral aclexplode(coalesce(c.relacl,acldefault('s',c.relowner))) a where c.oid=sequence_name::regclass and a.grantee=0) then
        raise exception '0611 PUBLIC sequence access: %',sequence_name;
      end if;
      foreach role_name in array array['anon','authenticated','service_role'] loop
        foreach privilege_name in array array['SELECT','USAGE','UPDATE'] loop
          if has_sequence_privilege(role_name,sequence_name,privilege_name) then
            raise exception '0611 sequence ACL mismatch: %.% %',role_name,sequence_name,privilege_name;
          end if;
        end loop;
      end loop;
    end if;
  end loop;
  for expected in select * from (values
    ('echo_source_brand_immutable()',false,false),
    ('echo_source_capture_insert_lock()',false,false),
    ('echo_source_brand_authority(text,uuid)',true,true),
    ('echo_source_brand_fact_spans(uuid,text,uuid[],jsonb)',false,true),
    ('echo_source_brand_attest_provider(uuid,text,jsonb,uuid)',true,true),
    ('echo_source_brand_provider_current(uuid,text)',false,true),
    ('echo_source_brand_policy_from_report(uuid,text,text,jsonb)',false,true),
    ('echo_source_brand_policy(uuid,text,text)',false,true),
    ('echo_source_brand_provider_receipt(uuid,text)',false,true),
    ('echo_source_brand_provider_receipt_valid(uuid,text,jsonb,jsonb)',false,true),
    ('echo_source_brand_current_policy(uuid)',true,true),
    ('echo_source_brand_prepare(uuid,text,uuid[],uuid,integer,integer,uuid,jsonb,text)',true,true),
    ('echo_source_brand_prepare(uuid,text,uuid[],uuid,integer,integer,uuid,jsonb)',true,true),
    ('echo_source_brand_decide(uuid,text,uuid,text,integer,uuid,text)',true,true),
    ('echo_source_brand_configuration(uuid)',true,true),
    ('echo_source_brand_active(uuid)',true,true),
    ('echo_source_brand_revalidate(uuid,uuid,text,uuid[],uuid,integer,integer,jsonb,text,jsonb)',true,true),
    ('echo_source_brand_grant(uuid,text,text,text,uuid)',true,true)
  ) as functions(signature,service_execute,definer) loop
    function_oid:=to_regprocedure('public.'||expected.signature);
    if function_oid is null then raise exception '0611 missing function: %',expected.signature; end if;
    select * into actual from pg_proc where oid=function_oid;
    if actual.prosecdef is distinct from expected.definer or
      (expected.signature<>'echo_source_brand_immutable()' and actual.proconfig is distinct from array['search_path=public, pg_temp']::text[]) then
      raise exception '0611 function safety mismatch: %',expected.signature;
    end if;
    if exists(select 1 from aclexplode(coalesce(actual.proacl,acldefault('f',actual.proowner))) a where a.grantee=0) then
      raise exception '0611 PUBLIC function access: %',expected.signature;
    end if;
    foreach role_name in array array['anon','authenticated','service_role'] loop
      expected_execute:=role_name='service_role' and expected.service_execute;
      if has_function_privilege(role_name,function_oid,'EXECUTE WITH GRANT OPTION') then
        raise exception '0611 function grant option: %.%',role_name,expected.signature;
      end if;
      if has_function_privilege(role_name,function_oid,'EXECUTE') is distinct from expected_execute then
        raise exception '0611 function ACL mismatch: %.%',role_name,expected.signature;
      end if;
    end loop;
  end loop;
  if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public'
    and (p.proname like 'echo_source_brand_%' or p.proname='echo_source_capture_insert_lock'))<>18 then
    raise exception '0611 unexpected function inventory';
  end if;
  if not exists(select 1 from pg_trigger where tgrelid='public.echo_source_captures'::regclass
    and tgname='echo_source_capture_insert_lock' and tgfoid='public.echo_source_capture_insert_lock()'::regprocedure
    and tgtype=7 and tgenabled='O' and not tgisinternal) then
    raise exception '0611 capture lock trigger mismatch';
  end if;
  raise notice 'PASS 0611 exact table/column/sequence/function ACLs, RLS and trigger verification';
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
