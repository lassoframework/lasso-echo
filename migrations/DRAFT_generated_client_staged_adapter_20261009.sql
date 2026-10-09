-- DRAFT / UNAPPLIED / DEFAULT OFF. Generated-specific staged adapter.
-- Frozen source: fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)
-- prosrc SHA-256 65a0f94cc8da12d658e73abbe783ad1c1daab39680c91a92d851b3bb4b4ae724.
-- Detached helper retains all v2 validation,
-- registry, history, census and permanent reservation writes; only row lookup
-- uses exact planned tuple + placeholder snapshot and calendar UPDATE is omitted.
-- No temporary activation. Public finalizer wraps generated singleton batches;
-- ordinary batches delegate through the unchanged calendar capability chain.
-- Existing forward staged CAS and finalizer activation/reservation logic remain intact.
begin;
do $$ declare f record; inner_proc record; helper_name text; normalized text; begin
 if current_user <> 'postgres' or
  (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc where oid='public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)'::regprocedure)
    is distinct from '65a0f94cc8da12d658e73abbe783ad1c1daab39680c91a92d851b3bb4b4ae724'
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc where oid='public.fixer_generated_runtime_check_20261007(uuid)'::regprocedure) is distinct from '0181bf7326da07c6b46e801e2d04fa90e61bd49aa8c8be1fb443a58b5d8bf441'
  or (select pg_get_userbyid(proowner) from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure) is distinct from 'postgres'
  or to_regprocedure('public.stage_forward_schedule_batch_20261008(text,uuid,text,text)') is null
  or to_regprocedure('public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)') is null then
  raise exception 'frozen generated owner and actual separate staging stack required' using errcode='23514'; end if;
 select * into strict f from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure;
 helper_name:='fixer_admission_body_'||f.oid::text;
 select * into strict inner_proc from pg_proc where oid=to_regprocedure('public.'||helper_name||'(text,uuid,jsonb,jsonb)');
 normalized:=replace(f.prosrc,helper_name,'fixer_admission_body_VALIDATED');
 if normalized is distinct from $expected$declare cap uuid:=gen_random_uuid(); result jsonb;
   begin
    insert into public.fixer_calendar_admission_capability_20261009 values
      (cap,pg_backend_pid(),pg_current_xact_id(),'finalize',array(select (x->>'calendar_row_id')::uuid from jsonb_array_elements(p_candidates) x) || array(select (x->>'id')::uuid from jsonb_array_elements(p_expected_old_rows) x));
    result:=public.fixer_admission_body_VALIDATED(p_tenant_id,p_batch_id,p_candidates,p_expected_old_rows);
    delete from public.fixer_calendar_admission_capability_20261009 where capability_id=cap;
    return result;
   end;$expected$
  or not f.prosecdef or f.prorettype<>'jsonb'::regtype
  or f.proargnames is distinct from array['p_tenant_id','p_batch_id','p_candidates','p_expected_old_rows']::text[]
  or inner_proc.proargnames is distinct from f.proargnames
  or f.prolang<>(select oid from pg_language where lanname='plpgsql')
  or f.proconfig is distinct from array['search_path=pg_catalog, public']::text[]
  or inner_proc.proowner<>'postgres'::regrole or not inner_proc.prosecdef
  or inner_proc.prorettype<>'jsonb'::regtype
  or inner_proc.prolang<>f.prolang or inner_proc.proconfig is distinct from f.proconfig
  or (select count(*) from pg_proc where pronamespace='public'::regnamespace and proname=helper_name)<>1
  or exists(select 1 from aclexplode(coalesce(inner_proc.proacl,acldefault('f',inner_proc.proowner))) a
    where a.grantee<>inner_proc.proowner)
  or encode(sha256(convert_to(inner_proc.prosrc,'UTF8')),'hex') is distinct from '7eaacf4f87922e0f4948fc56a40a9da44b6bdc77cb90b553c6e413d0a7827f29' then
  raise exception 'validated private calendar admission finalizer chain required' using errcode='23514'; end if;
end $$;
-- Derive from the fully composed, SHA-pinned authority body. Only the
-- enumerated existing row/replay deltas are permitted; newest-census and
-- inventory-generation/pending-mutation checks are inherited verbatim.
do $derive$
declare source_body text; derived text; delta record;
begin
 select prosrc into strict source_body from pg_proc where oid='public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)'::regprocedure;
 derived:=source_body;
 for delta in select * from (values
 ($old$ select * into r from public.content_calendar where id=p_id for update;
$old$,$new$ r:=jsonb_populate_record(null::public.content_calendar,p_planned);
 if r.id is distinct from p_id then raise exception 'frozen generated candidate required' using errcode='23514'; end if;
$new$),
 ($old$ snap:=public.fixer_generated_snapshot_20261007(p_id);
$old$,$new$ snap:=public.fixer_generated_snapshot_20261007(p_placeholder);
$new$),
 ($old$   if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'active'
$old$,$new$   if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'candidate'
$new$),
 ($old$   update public.content_calendar set source_media_asset_id='generated-astra:'||(c->>'job_id'),source_media_url=c->>'original_url',image_url=c->>'original_url',thumbnail_url=null,render_manifest_digest=m->>'manifest_digest' where id=p_id;
$old$,$new$   raise exception 'detached generated sibling requires separate preparation' using errcode='23514';
$new$),
 ($old$  perform public.fixer_generated_runtime_check_20261007(p_id);
$old$,$new$  raise exception 'detached replay requires immutable preparation reconciliation' using errcode='23514';
$new$),
 ($old$ end if;
 if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'active'
$old$,$new$ end if;
 if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'candidate'
$new$),
 ($old$ update public.content_calendar set source_media_asset_id=asset,source_media_url=c->>'original_url',image_url=c->>'original_url',thumbnail_url=null,render_manifest_digest=m->>'manifest_digest' where id=p_id;
$old$,$new$ -- Calendar insertion/activation belongs to separate stage/finalizer.
$new$)
 ) edits(old_text,new_text) loop
  if (length(derived)-length(replace(derived,delta.old_text,'')))/length(delta.old_text)<>1 then
   raise exception 'exact detached authority delta drift' using errcode='23514'; end if;
  derived:=replace(derived,delta.old_text,delta.new_text);
 end loop;
 execute $header$create function public.generated_client_reserve_detached_20261009(p_id uuid,p_placeholder uuid,p_planned jsonb,c jsonb,visuals jsonb,m jsonb,
 p_source_revision text default null)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $header$||'$$'||derived||'$$;';
end $derive$;

create table public.generated_client_finalizer_decision_20261009 (
 calendar_row_id uuid not null references public.generated_client_admission_20261009(calendar_row_id),
 batch_id uuid not null references public.forward_schedule_stage_batch_20261008(batch_id),
 transaction_id xid8 not null, primary key(calendar_row_id,transaction_id)
);
alter table public.generated_client_finalizer_decision_20261009 enable row level security;
revoke all on public.generated_client_finalizer_decision_20261009 from public,anon,authenticated,service_role,
 fixer_forward_media_owner_20261006,generated_hosted_byte_reader_20261009,generated_hosted_byte_issuer_20261009;
create trigger generated_client_finalizer_immutable before update or delete on public.generated_client_finalizer_decision_20261009
 for each row execute function public.generated_hosted_byte_immutable_20261009();
create trigger generated_client_finalizer_no_truncate before truncate on public.generated_client_finalizer_decision_20261009
 for each statement execute function public.generated_hosted_byte_immutable_20261009();

-- A read-only exact plan; end its transaction before external authority lookup.
create function public.generated_client_plan_20261009(p_placeholder uuid,p_row uuid,p_version uuid,c jsonb,m jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare old_row public.content_calendar%rowtype; planned public.content_calendar%rowtype; gap public.fixer_generated_gap_request_20261007%rowtype;
begin
 if not pg_has_role(session_user,'fixer_forward_media_owner_20261006','MEMBER')
  or pg_has_role(session_user,'service_role','MEMBER') or pg_has_role(session_user,'generated_hosted_byte_issuer_20261009','MEMBER') then
  raise exception 'isolated generated owner required' using errcode='42501'; end if;
 select * into old_row from public.content_calendar where id=p_placeholder;
 if not found or p_row is null or p_version is null or p_row=p_placeholder
  or old_row.status is distinct from 'pending' or old_row.variant_status is distinct from 'active'
  or old_row.image_url is not null or old_row.thumbnail_url is not null
  or old_row.source_media_url is not null or old_row.source_media_asset_id is not null or old_row.render_manifest_digest is not null
  or old_row.creative_origin is not null or old_row.generated_artifact_version_id is not null or old_row.generated_artifact_sha256 is not null
  or old_row.approval_kind is not null or old_row.approval_digest is not null or old_row.approved_by is not null
  or old_row.approved_at is not null or old_row.publish_claim_token is not null or old_row.publish_reservation_day is not null
  or old_row.published_at is not null or old_row.late_post_id is not null or old_row.media_not_ready_reason is not null
  or old_row.gym_id is distinct from c->>'gym_id' or old_row.post_date::text is distinct from c->>'local_date'
  or old_row.logical_post_id::text is distinct from c->>'logical_post_id'
  or old_row.caption is distinct from c#>>'{copy_derivation_receipt,caption}'
  or exists(select 1 from public.content_calendar where id=p_row) then
  raise exception 'exact null-image gap replacement required' using errcode='23514'; end if;
 select * into gap from public.fixer_generated_gap_request_20261007 where calendar_row_id=p_placeholder;
 if not found or gap.state is distinct from 'bound' or gap.gym_id is distinct from old_row.gym_id
  or gap.local_date is distinct from old_row.post_date or gap.logical_post_id is distinct from old_row.logical_post_id
  or gap.group_key is distinct from old_row.visual_group_key or gap.caption is distinct from old_row.caption
  or gap.account is distinct from old_row.account or gap.format is distinct from old_row.format
  or gap.account not in ('instagram','facebook') or gap.format is distinct from 'feed' then
  raise exception 'exact bound generated gap required' using errcode='23514'; end if;
 planned:=old_row;planned.id:=p_row;planned.variant_status:='candidate';
 planned.image_url:=c->>'original_url';planned.source_media_url:=c->>'original_url';planned.thumbnail_url:=null;
 planned.source_media_asset_id:='generated-astra:'||(c->>'job_id');planned.render_manifest_digest:=m->>'manifest_digest';
 planned.creative_origin:='generated';planned.generated_artifact_version_id:=p_version;
 planned.generated_artifact_sha256:=c->>'original_sha256';
 return jsonb_build_object('placeholder_row_id',p_placeholder,'old_snapshot',to_jsonb(old_row),'gap_snapshot',to_jsonb(gap),'planned_row',to_jsonb(planned));
end $$;

-- Original runtime-check SHA-256 0181bf7326da07c6b46e801e2d04fa90e61bd49aa8c8be1fb443a58b5d8bf441; exact planned-row fallback only.
-- Derive from the fully composed, SHA-pinned authority body. Only the
-- enumerated existing row/replay deltas are permitted; newest-census and
-- inventory-generation/pending-mutation checks are inherited verbatim.
do $derive$
declare source_body text; derived text; delta record;
begin
 select prosrc into strict source_body from pg_proc where oid='public.fixer_generated_runtime_check_20261007(uuid)'::regprocedure;
 derived:=source_body;
 for delta in select * from (values
 ($old$ s record; snap jsonb; h jsonb; ph text; i public.fixer_still_inventory_20261007%rowtype;
$old$,$new$ s record; snap jsonb; h jsonb; ph text; i public.fixer_still_inventory_20261007%rowtype; a public.generated_client_admission_20261009%rowtype; snapshot_id uuid:=p_id;
$new$),
 ($old$ select * into r from public.content_calendar where id=p_id;
$old$,$new$ select * into r from public.content_calendar where id=p_id;
 if not found then
  select * into a from public.generated_client_admission_20261009 where calendar_row_id=p_id;
  if not found then raise exception 'generated staged preparation missing' using errcode='23514'; end if;
  r:=jsonb_populate_record(null::public.content_calendar,convert_from(a.manifest_bytes,'UTF8')::jsonb#>'{stage_plan,planned_row}');
  snapshot_id:=(convert_from(a.manifest_bytes,'UTF8')::jsonb#>>'{stage_plan,placeholder_row_id}')::uuid;
 end if;
$new$),
 ($old$ snap:=public.fixer_generated_snapshot_20261007(p_id);
$old$,$new$ snap:=public.fixer_generated_snapshot_20261007(snapshot_id);
$new$)
 ) edits(old_text,new_text) loop
  if (length(derived)-length(replace(derived,delta.old_text,'')))/length(delta.old_text)<>1 then
   raise exception 'exact detached authority delta drift' using errcode='23514'; end if;
  derived:=replace(derived,delta.old_text,delta.new_text);
 end loop;
 execute $header$create function public.generated_client_runtime_check_20261009(p_id uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $header$||'$$'||derived||'$$;';
end $derive$;

create function public.generated_client_current_preparation_20261009(a public.generated_client_admission_20261009)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare raw jsonb; old_row public.content_calendar%rowtype; snap jsonb; g public.fixer_generated_reservation_20261007%rowtype;
begin
 perform public.generated_client_evidence_check_20261009(a);
 raw:=convert_from(a.manifest_bytes,'UTF8')::jsonb;
 select * into g from public.fixer_generated_reservation_20261007 where job_id=a.job_id;
 if not found or g.calendar_row_id is distinct from a.calendar_row_id or g.candidate_json is distinct from a.candidate_json
  or g.manifest_json is distinct from a.reservation_manifest or g.approved_source_revision is distinct from a.source_revision then
  raise exception 'generated staged reservation mismatch' using errcode='23514'; end if;
 perform public.fixer_generated_bundle_validate_20261007(a.gym_id,a.candidate_json->'authority_pins',
  a.candidate_json->'copy_derivation_receipt',a.candidate_json#>>'{copy_derivation_receipt,caption}',
  a.candidate_json->>'copy_digest',a.candidate_json->>'palette_digest',a.candidate_json->>'palette_revision');
 if exists(select 1 from public.content_calendar where id=a.calendar_row_id) then
  perform public.generated_client_runtime_check_20261009(a.calendar_row_id);
 else
  select * into old_row from public.content_calendar where id=(raw#>>'{stage_plan,placeholder_row_id}')::uuid;
  if not found or to_jsonb(old_row) is distinct from raw#>'{stage_plan,old_snapshot}' then
   raise exception 'generated placeholder changed' using errcode='23514'; end if;
  snap:=public.fixer_generated_snapshot_20261007(old_row.id);
  if snap->'photo_inventory_complete' is distinct from 'true'::jsonb
   or snap->'eligible_photo_count' is distinct from '0'::jsonb
   or snap->'history_complete' is distinct from 'true'::jsonb or snap#>'{history,epoch}' is distinct from g.history_epoch
   or snap->>'inventory_revision' is distinct from a.candidate_json->>'inventory_revision'
   or snap->>'copy_revision' is distinct from a.candidate_json->>'copy_revision' then
   raise exception 'generated current preparation changed' using errcode='23514'; end if;
 end if;
 perform public.generated_client_runtime_check_20261009(a.calendar_row_id);
 return true;
end $$;

create function public.generated_client_prepare_staged_20261009(p_placeholder uuid,p_row uuid,c jsonb,visuals jsonb,m jsonb,
 p_source text,p_version uuid,p_receipt uuid,p_manifest bytea)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare a public.generated_client_admission_20261009%rowtype; raw jsonb; plan jsonb; reserved jsonb; prior public.generated_client_admission_20261009%rowtype;
begin
 if not pg_has_role(session_user,'fixer_forward_media_owner_20261006','MEMBER')
  or pg_has_role(session_user,'service_role','MEMBER') or pg_has_role(session_user,'generated_hosted_byte_issuer_20261009','MEMBER') then
  raise exception 'isolated generated owner required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated staged preparation requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 if p_manifest is null or octet_length(p_manifest) not between 1 and 65536 then
  raise exception 'original staged manifest required' using errcode='23514'; end if;
 raw:=convert_from(p_manifest,'UTF8')::jsonb;
 select * into prior from public.generated_client_admission_20261009 where calendar_row_id=p_row;
 if found then
  if prior.manifest_bytes is distinct from p_manifest or prior.owner_principal is distinct from session_user
   or prior.artifact_version_id is distinct from p_version or prior.receipt_id is distinct from p_receipt
   or prior.candidate_json is distinct from c or prior.reservation_manifest is distinct from m
   or prior.source_revision is distinct from p_source then
   raise exception 'immutable staged preparation conflict' using errcode='23514'; end if;
  perform public.generated_client_current_preparation_20261009(prior);
  return jsonb_build_object('admitted',true,'reserved',true,'prepared',true,'replayed',true,'calendar_row_id',p_row,
   'artifact_version_id',p_version,'receipt_id',p_receipt,'manifest_sha256',prior.manifest_sha256,
   'stage_plan',raw->'stage_plan');
 end if;
 perform public.fixer_generated_bundle_validate_20261007(c->>'gym_id',c->'authority_pins',c->'copy_derivation_receipt',
  c#>>'{copy_derivation_receipt,caption}',c->>'copy_digest',c->>'palette_digest',c->>'palette_revision');
 perform 1 from public.content_calendar where id=p_placeholder for update;
 plan:=public.generated_client_plan_20261009(p_placeholder,p_row,p_version,c,m);
 if raw is distinct from jsonb_build_object('calendar_row_id',p_row,'candidate',c,'reservation_manifest',m,
  'source_revision',p_source,'gym_id',c->>'gym_id','artifact_version_id',p_version,
  'hosted_url',c->>'original_url','delivered_sha256',c->>'original_sha256','stage_plan',plan) then
  raise exception 'exact original staged manifest required' using errcode='23514'; end if;
 a.calendar_row_id:=p_row;a.job_id:=(c->>'job_id')::uuid;a.owner_principal:=session_user;
 a.gym_id:=c->>'gym_id';a.artifact_version_id:=p_version;a.receipt_id:=p_receipt;
 a.manifest_sha256:=encode(sha256(p_manifest),'hex');a.manifest_bytes:=p_manifest;
 a.candidate_json:=c;a.reservation_manifest:=m;a.source_revision:=p_source;
 a.row_binding:=public.generated_client_row_binding_20261009(jsonb_populate_record(null::public.content_calendar,plan->'planned_row'));
 perform public.generated_client_evidence_check_20261009(a);
 reserved:=public.generated_client_reserve_detached_20261009(p_row,p_placeholder,plan->'planned_row',c,visuals,m,p_source);
 insert into public.generated_client_admission_20261009(calendar_row_id,job_id,owner_principal,gym_id,
  artifact_version_id,receipt_id,manifest_sha256,manifest_bytes,candidate_json,reservation_manifest,source_revision,row_binding)
 values(a.calendar_row_id,a.job_id,a.owner_principal,a.gym_id,a.artifact_version_id,a.receipt_id,a.manifest_sha256,
  a.manifest_bytes,a.candidate_json,a.reservation_manifest,a.source_revision,a.row_binding);
 perform public.generated_client_current_preparation_20261009(a);
 return reserved||jsonb_build_object('admitted',true,'prepared',true,'calendar_row_id',p_row,
  'artifact_version_id',p_version,'receipt_id',p_receipt,'manifest_sha256',a.manifest_sha256,'stage_plan',plan);
end $$;


-- Service consumes only an exact committed preparation. This confers no table,
-- writer, approval, source-reader or issuer authority. The full old snapshot is
-- checked even if a candidate was inserted, so finalized preparations refuse.
create function public.generated_client_service_preparation_20261009(
 p_tenant text,p_row uuid,p_version uuid,p_receipt uuid,p_manifest_sha text,p_stage_plan jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare a public.generated_client_admission_20261009%rowtype; raw jsonb;
 old_row public.content_calendar%rowtype; candidate public.content_calendar%rowtype;
begin
 if not pg_has_role(session_user,'service_role','MEMBER') then
  raise exception 'generated preparation service required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated preparation requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into a from public.generated_client_admission_20261009 where calendar_row_id=p_row;
 if not found or a.gym_id is distinct from p_tenant
  or a.artifact_version_id is distinct from p_version or a.receipt_id is distinct from p_receipt
  or a.manifest_sha256 is distinct from p_manifest_sha then
  raise exception 'exact generated preparation required' using errcode='23514'; end if;
 raw:=convert_from(a.manifest_bytes,'UTF8')::jsonb;
 if jsonb_typeof(raw->'stage_plan') is distinct from 'object'
  or raw->'stage_plan' is distinct from p_stage_plan
  or raw->>'gym_id' is distinct from a.gym_id
  or raw->>'calendar_row_id' is distinct from a.calendar_row_id::text
  or raw->>'artifact_version_id' is distinct from a.artifact_version_id::text then
  raise exception 'exact generated stage plan required' using errcode='23514'; end if;
 select * into old_row from public.content_calendar
  where id=(raw#>>'{stage_plan,placeholder_row_id}')::uuid for share;
 if not found or old_row.gym_id is distinct from p_tenant
  or to_jsonb(old_row) is distinct from raw#>'{stage_plan,old_snapshot}' then
  raise exception 'generated preparation old snapshot changed' using errcode='23514'; end if;
 select * into candidate from public.content_calendar where id=p_row for share;
 if found and to_jsonb(candidate) is distinct from raw#>'{stage_plan,planned_row}' then
  -- The stage RPC adds exactly this sentinel. It is authority only with its
  -- immutable singleton batch, member and full old-row request bindings.
  if candidate.variant_status is distinct from 'candidate'
   or candidate.media_not_ready_reason is distinct from 'forward_reservation_staged'
   or to_jsonb(candidate) is distinct from jsonb_set(raw#>'{stage_plan,planned_row}',
      '{media_not_ready_reason}','"forward_reservation_staged"'::jsonb)
   or not exists(select 1 from public.forward_schedule_stage_member_20261008 member
    join public.forward_schedule_stage_batch_20261008 batch using(batch_id)
    join public.forward_schedule_stage_old_row_20261008 prior using(batch_id)
    where member.calendar_row_id=p_row and member.position=0
     and member.tenant_id=p_tenant and member.gym_id=candidate.gym_id
     and member.logical_post_id=candidate.logical_post_id and member.post_date=candidate.post_date
     and member.source_media_url=candidate.source_media_url and member.image_url=candidate.image_url
     and member.thumbnail_url is not distinct from candidate.thumbnail_url
     and member.observation_digest is null and member.staged_snapshot=to_jsonb(candidate)
     and batch.tenant_id=p_tenant and batch.state='staged'
     and batch.request_digest ~ '^[0-9a-f]{64}$'
     and batch.finalize_request is null and batch.finalize_receipt is null and batch.finalized_at is null
     and batch.request_payload=jsonb_build_object('members',jsonb_build_array(
       jsonb_build_object('row',raw#>'{stage_plan,planned_row}','observation',null)),
       'old_rows',jsonb_build_array(raw#>'{stage_plan,old_snapshot}'))
     and prior.position=0 and prior.calendar_row_id=old_row.id and prior.tenant_id=p_tenant
     and prior.old_snapshot=to_jsonb(old_row)
     and (select count(*) from public.forward_schedule_stage_member_20261008 x where x.batch_id=batch.batch_id)=1
     and (select count(*) from public.forward_schedule_stage_old_row_20261008 x where x.batch_id=batch.batch_id)=1
     and public.fixer_forward_schedule_staged_authorized_20261008(p_row,p_tenant,batch.batch_id)) then
   raise exception 'generated preparation candidate changed' using errcode='23514'; end if;
 end if;
 perform public.generated_client_current_preparation_20261009(a);
 return jsonb_build_object('admitted',true,'reserved',true,'prepared',true,
  'calendar_row_id',a.calendar_row_id,'artifact_version_id',a.artifact_version_id,
  'receipt_id',a.receipt_id,'manifest_sha256',a.manifest_sha256,'stage_plan',raw->'stage_plan');
end $$;
revoke all on function public.generated_client_service_preparation_20261009(text,uuid,uuid,uuid,text,jsonb)
 from public,anon,authenticated,fixer_forward_media_owner_20261006,
 generated_hosted_byte_reader_20261009,generated_hosted_byte_issuer_20261009;
grant execute on function public.generated_client_service_preparation_20261009(text,uuid,uuid,uuid,text,jsonb) to service_role;

-- Explicitly reject the abandoned direct ACTIVE-row route.
create or replace function public.generated_client_stage_20261009(p_row uuid,c jsonb,visuals jsonb,m jsonb,
 p_source text,p_version uuid,p_receipt uuid,p_manifest bytea)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin raise exception 'generated clients require separate staged preparation/finalization' using errcode='23514'; end $$;

-- Clone the current capability wrapper byte-for-byte, including its exact
-- cluster-local validated private callee. Underlying historical/prospective
-- body SHA-256 7eaacf4f87922e0f4948fc56a40a9da44b6bdc77cb90b553c6e413d0a7827f29.
-- Normalized wrapper SHA-256 c658ed1e8e4194eb961e32445890f734d26d156d79011b658149a77c06caf5bc.
-- Raw wrapper SHA is recorded only as installation-local drift evidence;
-- it is never a portable release pin. Public OID/owner/ACL remain unchanged.
create table public.generated_client_install_pin_20261009 (
 signature text primary key, body_sha256 text not null, original_oid oid not null,
 original_owner oid not null, original_acl aclitem[], original_config text[], original_definer boolean not null,
 normalized_sha256 text not null, admission_body_oid oid not null, admission_body_sha256 text not null
);
alter table public.generated_client_install_pin_20261009 enable row level security;
revoke all on public.generated_client_install_pin_20261009 from public,anon,authenticated,service_role,
 fixer_forward_media_owner_20261006,generated_hosted_byte_reader_20261009,generated_hosted_byte_issuer_20261009;
insert into public.generated_client_install_pin_20261009
 select 'finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)',
 encode(sha256(convert_to(prosrc,'UTF8')),'hex'),oid,proowner,proacl,proconfig,prosecdef,
 encode(sha256(convert_to(replace(prosrc,'fixer_admission_body_'||oid::text,'fixer_admission_body_VALIDATED'),'UTF8')),'hex'),
 to_regprocedure('public.fixer_admission_body_'||oid::text||'(text,uuid,jsonb,jsonb)'),
 '7eaacf4f87922e0f4948fc56a40a9da44b6bdc77cb90b553c6e413d0a7827f29'
 from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure;
do $clone$
declare definition text; renamed text;
begin
 definition:=pg_get_functiondef('public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure);
 renamed:=replace(definition,'CREATE OR REPLACE FUNCTION public.finalize_forward_schedule_staged_batch_20261008(',
  'CREATE FUNCTION public.generated_client_original_finalizer_20261009(');
 if renamed=definition then raise exception 'exact capability wrapper header drift'; end if;
 execute renamed;
end $clone$;

create or replace function public.generated_client_calendar_guard_20261009()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare a public.generated_client_admission_20261009%rowtype; g public.fixer_generated_reservation_20261007%rowtype; planned jsonb;
begin
 if new.creative_origin is distinct from 'generated' and new.generated_artifact_version_id is null
  and new.generated_artifact_sha256 is null and coalesce(new.source_media_asset_id,'') not like 'generated-astra:%'
  and (tg_op='INSERT' or (old.creative_origin is distinct from 'generated' and old.generated_artifact_version_id is null
   and old.generated_artifact_sha256 is null and coalesce(old.source_media_asset_id,'') not like 'generated-astra:%')) then return new; end if;
 select * into a from public.generated_client_admission_20261009 where calendar_row_id=new.id;
 if not found then raise exception 'generated client owner admission required' using errcode='23514'; end if;
 perform public.generated_client_evidence_check_20261009(a);
 select * into g from public.fixer_generated_reservation_20261007 where job_id=a.job_id;
 if not found or g.calendar_row_id is distinct from new.id or g.candidate_json is distinct from a.candidate_json
  or g.manifest_json is distinct from a.reservation_manifest or g.approved_source_revision is distinct from a.source_revision
  or public.generated_client_row_binding_20261009(new) is distinct from a.row_binding
  or new.source_media_asset_id is distinct from 'generated-astra:'||a.job_id::text
  or new.source_media_url is distinct from a.candidate_json->>'original_url'
  or new.image_url is distinct from a.candidate_json->>'original_url' or new.thumbnail_url is not null
  or new.render_manifest_digest is distinct from a.reservation_manifest->>'manifest_digest' then
  raise exception 'generated client exact reserved row binding required' using errcode='23514'; end if;
 planned:=convert_from(a.manifest_bytes,'UTF8')::jsonb#>'{stage_plan,planned_row}';
 if new.creative_origin is distinct from 'generated'
  or new.generated_artifact_version_id is distinct from a.artifact_version_id
  or new.generated_artifact_sha256 is distinct from a.candidate_json->>'original_sha256' then
  raise exception 'generated client artifact identity immutable' using errcode='23514'; end if;
 if tg_op='INSERT' then
  if (to_jsonb(new)-'media_not_ready_reason') is distinct from (planned-'media_not_ready_reason')
   or new.variant_status is distinct from 'candidate' or new.status is distinct from 'pending'
   or new.media_not_ready_reason is distinct from 'forward_reservation_staged'
   or new.approval_kind is not null or new.approval_digest is not null
   or new.approved_by is not null or new.approved_at is not null then
   raise exception 'exact prepared inactive generated insertion required' using errcode='23514'; end if;
  perform public.generated_client_current_preparation_20261009(a);
 elsif old.variant_status='candidate' then
  if new.status is distinct from 'pending' or new.approval_kind is not null or new.approval_digest is not null
   or new.approved_by is not null or new.approved_at is not null or new.publish_claim_token is not null then
   raise exception 'generated candidate cannot approve or claim before finalization' using errcode='23514'; end if;
  if new.variant_status='active' then
   if not exists(select 1 from public.generated_client_finalizer_decision_20261009 d
    join public.forward_schedule_stage_member_20261008 member on member.batch_id=d.batch_id and member.calendar_row_id=d.calendar_row_id
    join public.forward_schedule_stage_batch_20261008 batch on batch.batch_id=d.batch_id
    where d.calendar_row_id=new.id and d.transaction_id=pg_current_xact_id() and batch.state='staged')
    or not pg_has_role(coalesce(nullif(current_setting('role',true),'none'),session_user),'service_role','MEMBER') then
    raise exception 'exact private finalizer transaction required' using errcode='42501'; end if;
  elsif new.variant_status is distinct from 'candidate' then
   raise exception 'generated candidate finalization required' using errcode='23514'; end if;
 elsif old.creative_origin is distinct from 'generated' then
  raise exception 'direct generated active-row mutation forbidden' using errcode='23514';
 end if;
 if new.status in ('approved','publishing','published') then
  if new.variant_status is distinct from 'active' or not exists(select 1 from public.forward_schedule_stage_member_20261008 member
   join public.forward_schedule_stage_batch_20261008 batch using(batch_id) where member.calendar_row_id=new.id and batch.state='finalized') then
   raise exception 'generated client separate finalization required' using errcode='23514'; end if;
  if new.approval_kind is not null and new.approval_kind is distinct from 'human' then
   raise exception 'generated client cannot use autonomous approval' using errcode='23514'; end if;
  if new.approval_digest is distinct from public.calendar_approval_digest(new) then
   raise exception 'generated client exact approval digest required' using errcode='23514'; end if;
  if new.approval_kind='human' then
   if (select count(*) from public.app_users u
    join public.gym_assignments ga on ga.app_user_id=u.id
    join public.echo_intake_tokens t on t.gym_id=ga.gym_id
    where u.clerk_user_id=new.approved_by and u.role='client'
     and ga.relationship='client_owner' and t.echo_account_key=new.gym_id)<>1 then
    raise exception 'generated client current client owner required' using errcode='23514'; end if;
   perform u.id from public.app_users u
    join public.gym_assignments ga on ga.app_user_id=u.id
    join public.echo_intake_tokens t on t.gym_id=ga.gym_id
    where u.clerk_user_id=new.approved_by and u.role='client'
     and ga.relationship='client_owner' and t.echo_account_key=new.gym_id
    for share of u,ga,t;
   if not found then raise exception 'generated client owner revoked' using errcode='23514'; end if;
  end if;
  -- Already entered with G -> C before row locks for approval/RPCs. A trigger
  -- caller with reverse order never waits for competing graph/inventory writers.
  if not pg_try_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0))
   or not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0)) then
   raise exception 'generated client authority busy' using errcode='55000'; end if;
  if not pg_try_advisory_xact_lock(hashtext('generated-authority-20261007'),hashtext(new.gym_id))
   or not pg_try_advisory_xact_lock(hashtextextended(a.candidate_json#>>'{authority_pins,gym_id}',0)) then
   raise exception 'generated client source authority busy' using errcode='55000'; end if;
  if exists(select 1 from public.generated_revocation_request_20261007 where tenant_id=new.gym_id and state='pending') then
   raise exception 'generated client revocation pending' using errcode='55000'; end if;
  perform public.fixer_generated_runtime_check_20261007(new.id);
  perform public.fixer_generated_bundle_validate_20261007(new.gym_id,a.candidate_json->'authority_pins',
    a.candidate_json->'copy_derivation_receipt',new.caption,a.candidate_json->>'copy_digest',
    a.candidate_json->>'palette_digest',a.candidate_json->>'palette_revision');
 end if;
 return new;
end $$;

create or replace function public.finalize_forward_schedule_staged_batch_20261008(
 p_tenant_id text,p_batch_id uuid,p_candidates jsonb,p_expected_old_rows jsonb
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare b public.forward_schedule_stage_batch_20261008%rowtype; a public.generated_client_admission_20261009%rowtype;
 gap public.fixer_generated_gap_request_20261007%rowtype; plan jsonb; old_row public.content_calendar%rowtype;
 receipt jsonb; caller name:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 -- Ordinary batches retain the frozen capability wrapper + underlying guards.
 if not exists(select 1 from public.forward_schedule_stage_member_20261008 member
  join public.generated_client_admission_20261009 admission on admission.calendar_row_id=member.calendar_row_id
  where member.batch_id=p_batch_id) then
  return public.generated_client_original_finalizer_20261009(p_tenant_id,p_batch_id,p_candidates,p_expected_old_rows); end if;
 if not pg_has_role(caller,'service_role','MEMBER') or pg_has_role(caller,'fixer_forward_media_owner_20261006','MEMBER')
  or pg_has_role(caller,'generated_hosted_byte_issuer_20261009','MEMBER') then
  raise exception 'separate service finalizer required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated finalizer requires read committed' using errcode='25000'; end if;
 -- Latest inventory consumer requires exclusive G. Acquire it at entry,
 -- before C/batch/calendar locks; never upgrade after acquiring rows.
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into b from public.forward_schedule_stage_batch_20261008 where batch_id=p_batch_id for update;
 if not found or b.tenant_id is distinct from btrim(p_tenant_id) then
  raise exception 'exact generated batch required' using errcode='23514'; end if;
 if b.state='finalized' then
  -- Exact persisted terminal receipt; original helper enforces frozen request.
  return public.generated_client_original_finalizer_20261009(p_tenant_id,p_batch_id,p_candidates,p_expected_old_rows); end if;
 if (select count(*) from public.forward_schedule_stage_member_20261008 where batch_id=p_batch_id)<>1
  or (select count(*) from public.forward_schedule_stage_old_row_20261008 where batch_id=p_batch_id)<>1
  or jsonb_typeof(p_candidates) is distinct from 'array' or jsonb_array_length(p_candidates)<>1 then
  raise exception 'generated singleton replacement required' using errcode='23514'; end if;
 select admission.* into a from public.generated_client_admission_20261009 admission
  join public.forward_schedule_stage_member_20261008 member on member.calendar_row_id=admission.calendar_row_id
  where member.batch_id=p_batch_id;
 plan:=convert_from(a.manifest_bytes,'UTF8')::jsonb->'stage_plan';
 if p_candidates#>>'{0,calendar_row_id}' is distinct from a.calendar_row_id::text
  or p_expected_old_rows is distinct from jsonb_build_array(plan->'old_snapshot')
  or not exists(select 1 from public.forward_schedule_stage_old_row_20261008
   where batch_id=p_batch_id and calendar_row_id=(plan->>'placeholder_row_id')::uuid and old_snapshot=plan->'old_snapshot')
  or b.request_payload is distinct from jsonb_build_object('members',jsonb_build_array(jsonb_build_object('row',plan->'planned_row','observation',null)),
   'old_rows',jsonb_build_array(plan->'old_snapshot')) then
  raise exception 'generated exact persisted replacement binding required' using errcode='23514'; end if;
 perform public.generated_client_current_preparation_20261009(a);
 -- Full placeholder and full gap-request CAS, locked before any rebind.
 perform 1 from public.content_calendar where id in (a.calendar_row_id,(plan->>'placeholder_row_id')::uuid) order by id for update;
 select * into old_row from public.content_calendar where id=(plan->>'placeholder_row_id')::uuid;
 if not found or to_jsonb(old_row) is distinct from plan->'old_snapshot' then
  raise exception 'generated finalizer placeholder changed' using errcode='23514'; end if;
 select * into gap from public.fixer_generated_gap_request_20261007 where request_id=(plan#>>'{gap_snapshot,request_id}')::uuid for update;
 if not found or to_jsonb(gap) is distinct from plan->'gap_snapshot' then
  raise exception 'generated finalizer gap binding changed' using errcode='23514'; end if;
 insert into public.generated_client_finalizer_decision_20261009(calendar_row_id,batch_id,transaction_id)
 values(a.calendar_row_id,p_batch_id,pg_current_xact_id());
 -- Existing gap-slot guard observes an inactive candidate after this exact
 -- rebind. Original finalizer validates the complete CAS, activates/reserves,
 -- archives old placeholder and records receipt. Any failure rolls back all.
 update public.fixer_generated_gap_request_20261007 set calendar_row_id=a.calendar_row_id where request_id=gap.request_id;
 receipt:=public.generated_client_original_finalizer_20261009(p_tenant_id,p_batch_id,p_candidates,p_expected_old_rows);
 if not exists(select 1 from public.content_calendar where id=a.calendar_row_id and variant_status='active' and status='pending'
  and approval_kind is null and approval_digest is null and approved_by is null and approved_at is null)
  or not exists(select 1 from public.content_calendar where id=old_row.id and variant_status='archived') then
  raise exception 'generated finalization did not preserve pending replacement' using errcode='23514'; end if;
 return receipt;
end $$;

create or replace function public.generated_client_reconcile_20261009(p_row uuid,p_version uuid,p_receipt uuid,p_manifest_sha text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare a public.generated_client_admission_20261009%rowtype;
begin
 if not pg_has_role(session_user,'fixer_forward_media_owner_20261006','MEMBER')
  or pg_has_role(session_user,'service_role','MEMBER') or pg_has_role(session_user,'generated_hosted_byte_issuer_20261009','MEMBER') then
  raise exception 'isolated generated owner required' using errcode='42501'; end if;
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into a from public.generated_client_admission_20261009 where calendar_row_id=p_row;
 if not found then return null; end if;
 if a.owner_principal is distinct from session_user or a.artifact_version_id is distinct from p_version
  or a.receipt_id is distinct from p_receipt or a.manifest_sha256 is distinct from p_manifest_sha then
  raise exception 'immutable staged reconciliation conflict' using errcode='23514'; end if;
 perform public.generated_client_current_preparation_20261009(a);
 return jsonb_build_object('admitted',true,'reserved',true,'prepared',true,'replayed',true,'calendar_row_id',p_row,
  'artifact_version_id',p_version,'receipt_id',p_receipt,'manifest_sha256',p_manifest_sha,
  'stage_plan',convert_from(a.manifest_bytes,'UTF8')::jsonb->'stage_plan');
end $$;

revoke all on function public.generated_client_reserve_detached_20261009(uuid,uuid,jsonb,jsonb,jsonb,jsonb,text),
 public.generated_client_original_finalizer_20261009(text,uuid,jsonb,jsonb),
 public.generated_client_runtime_check_20261009(uuid),
 public.generated_client_current_preparation_20261009(public.generated_client_admission_20261009),
 public.generated_client_plan_20261009(uuid,uuid,uuid,jsonb,jsonb),
 public.generated_client_prepare_staged_20261009(uuid,uuid,jsonb,jsonb,jsonb,text,uuid,uuid,bytea)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,
 generated_hosted_byte_reader_20261009,generated_hosted_byte_issuer_20261009;
grant execute on function public.generated_client_plan_20261009(uuid,uuid,uuid,jsonb,jsonb),
 public.generated_client_prepare_staged_20261009(uuid,uuid,jsonb,jsonb,jsonb,text,uuid,uuid,bytea)
 to fixer_forward_media_owner_20261006;
-- Wrapper OID/owner/ACL preserved, copied capability prosrc byte-for-byte.
do $$ declare pin public.generated_client_install_pin_20261009%rowtype; current_proc record; begin
 select * into pin from public.generated_client_install_pin_20261009;
 select * into current_proc from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure;
 if row(current_proc.oid,current_proc.proowner,current_proc.proacl,current_proc.proconfig,current_proc.prosecdef)
  is distinct from row(pin.original_oid,pin.original_owner,pin.original_acl,pin.original_config,pin.original_definer)
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc where oid='public.generated_client_original_finalizer_20261009(text,uuid,jsonb,jsonb)'::regprocedure)
   is distinct from pin.body_sha256 then raise exception 'finalizer identity/body drift' using errcode='23514'; end if;
end $$;
commit;
