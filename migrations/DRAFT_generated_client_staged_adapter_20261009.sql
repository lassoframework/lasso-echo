-- DRAFT / UNAPPLIED / DEFAULT OFF. Generated-specific staged adapter.
-- Frozen source: fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)
-- prosrc SHA-256 16aaf2a5aa367d35c8c546bbdd9c2be0c7a32c05b7ed0c4423a9bffa01cb22a9.
-- Detached helper retains all v2 validation,
-- registry, history, census and permanent reservation writes; only row lookup
-- uses exact planned tuple + placeholder snapshot and calendar UPDATE is omitted.
-- No temporary activation. Public finalizer wraps generated singleton batches;
-- ordinary batches delegate to a private byte-for-byte original finalizer.
-- Existing forward staged CAS and finalizer activation/reservation logic remain intact.
begin;
do $$ begin
 if current_user <> 'postgres' or
  (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc where oid='public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)'::regprocedure)
    is distinct from '16aaf2a5aa367d35c8c546bbdd9c2be0c7a32c05b7ed0c4423a9bffa01cb22a9'
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc where oid='public.fixer_generated_runtime_check_20261007(uuid)'::regprocedure) is distinct from 'afe550a47ade18d757a47ee382a8a1a4aa8c8021afe5e2cb2bbebb4dad66e1ba'
  or (select pg_get_userbyid(proowner) from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure) is distinct from 'postgres'
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure) is distinct from '092e3b3823a26c4ad5b446455274b99e11b4efae5e60632006376654a9a4937b'
  or to_regprocedure('public.stage_forward_schedule_batch_20261008(text,uuid,text,text)') is null
  or to_regprocedure('public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)') is null then
  raise exception 'frozen generated owner and actual separate staging stack required' using errcode='23514'; end if;
end $$;
create function public.generated_client_reserve_detached_20261009(p_id uuid,p_placeholder uuid,p_planned jsonb,c jsonb,visuals jsonb,m jsonb,
 p_source_revision text default null)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; h jsonb; proof jsonb; prior public.fixer_generated_reservation_20261007%rowtype;
 r public.content_calendar%rowtype; receipt text; original jsonb; clearance jsonb; fp text; asset text;
 s public.fixer_still_cutover_20261007%rowtype; i public.fixer_still_inventory_20261007%rowtype;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
  or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated existing owner required' using errcode='42501'; end if;
 -- Only the isolated owner supplies a current delegated bundle derivation.
 -- SQL rechecks it against the authenticated portal contract under its locks.
 if (p_source_revision ~ '^source-brand:sha256:[0-9a-f]{64}$') is distinct from true then
  raise exception 'verified approved source revision required' using errcode='23514'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated reservation requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 if jsonb_typeof(c) is distinct from 'object' or not (c ?& array['schema_version','source_type','gym_id','local_date','logical_post_id','copy_revision','palette_revision','inventory_revision','history_revision','copy_digest','palette_digest','review_policy_id','job_id','provider','model','provider_response_id','provider_output_id','original_sha256','original_md5','original_length','original_phash','width','height','review_response_id','storage_key','original_url','storage_readback_sha256','authority_pins','copy_derivation_receipt'])
  or c-array['schema_version','source_type','gym_id','local_date','logical_post_id','copy_revision','palette_revision','inventory_revision','history_revision','copy_digest','palette_digest','review_policy_id','job_id','provider','model','provider_response_id','provider_output_id','original_sha256','original_md5','original_length','original_phash','width','height','review_response_id','storage_key','original_url','storage_readback_sha256','authority_pins','copy_derivation_receipt'] is distinct from '{}'::jsonb
  or c->'width' is distinct from '1024'::jsonb or c->'height' is distinct from '1280'::jsonb
  or c->>'storage_key' is distinct from 'echo-generated-originals/'||(c->>'gym_id')||'/'||(c->>'original_sha256')||'.png' then
  raise exception 'strict complete v2 generated candidate required' using errcode='23514'; end if;
 perform public.fixer_generated_bundle_validate_20261007(c->>'gym_id',c->'authority_pins',c->'copy_derivation_receipt',
  c->'copy_derivation_receipt'->>'caption',c->>'copy_digest',c->>'palette_digest',c->>'palette_revision');
 if p_source_revision is distinct from 'source-brand:sha256:'||public.fixer_generated_bundle_digest_20261007(c->'authority_pins')
  or c->>'review_policy_id' is distinct from 'gym-infographic-copy-palette-v1'
  or c->>'job_id' is distinct from public.fixer_generated_bundle_job_20261007(c)::text then
  raise exception 'exact v2 generated job identity required' using errcode='23514'; end if;
 r:=jsonb_populate_record(null::public.content_calendar,p_planned);
 if r.id is distinct from p_id then raise exception 'frozen generated candidate required' using errcode='23514'; end if;
 if r.caption is distinct from c->'copy_derivation_receipt'->>'caption' then
  raise exception 'reserved caption differs from verbatim derivation' using errcode='23514'; end if;
 perform public.fixer_generated_approved_visual_guard_20261007(r,c,m);
 snap:=public.fixer_generated_snapshot_20261007(p_placeholder);
 if jsonb_typeof(c)<>'object' or c->'schema_version' is distinct from '2'::jsonb
  or c->>'source_type' is distinct from 'generated_astra_infographic' or c->>'provider' is distinct from 'astra'
  or c->>'model' is distinct from 'gpt-6-astra' or c->>'job_id' is null
  or c->>'gym_id' is distinct from snap->>'gym_id' or c->>'local_date' is distinct from snap->>'local_date'
  or c->>'logical_post_id' is distinct from snap->>'logical_post_id'
  or c->>'copy_revision' is distinct from snap->>'copy_revision'
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or snap->'eligible_photo_count' is distinct from '0'::jsonb
  or snap->'history_complete' is distinct from 'true'::jsonb
  or (c->>'original_sha256' ~ '^[0-9a-f]{64}$') is distinct from true
  or (c->>'original_md5' ~ '^[0-9a-f]{32}$') is distinct from true
  or (c->>'original_phash' ~ '^scene:phash64:[0-9a-f]{16}$') is distinct from true
  or c->>'storage_readback_sha256' is distinct from c->>'original_sha256'
  or (c->>'original_url' ~ '^https://[^[:space:]]+$') is distinct from true
  or jsonb_typeof(c->'original_length') is distinct from 'number'
  or (c->>'original_length')::bigint not between 1 and 134217728
  or exists(select 1 from unnest(array['palette_revision','copy_digest','palette_digest','provider_response_id','provider_output_id','storage_key','review_response_id','review_policy_id']) key where nullif(btrim(c->>key),'') is null) then
  raise exception 'complete current generated original authority required' using errcode='23514'; end if;
 perform public.fixer_still_negative_check_20261007(jsonb_build_object('gym_id',c->>'gym_id','source_asset_id','generated-astra:'||(c->>'job_id'),
  'source_url',c->>'original_url','sha256','sha256:'||(c->>'original_sha256'),'md5','md5:'||(c->>'original_md5'),'phash',c->>'original_phash'));
 if exists(select 1 from public.fixer_still_reservation_20261007 still where
   still.original->>'sha256'='sha256:'||(c->>'original_sha256') or still.original->>'md5'='md5:'||(c->>'original_md5')
   or still.original->>'source_url'=c->>'original_url'
   or public.fixer_generated_hamming_20261007(still.original->>'phash',c->>'original_phash')<=6) then
  raise exception 'generated visual already held by common still reservation' using errcode='23514'; end if;
 select * into prior from public.fixer_generated_reservation_20261007 where job_id=(c->>'job_id')::uuid;
 if found then
  if prior.candidate_json is distinct from c or prior.manifest_json is distinct from m
   or prior.approved_source_revision is distinct from p_source_revision then
   raise exception 'generated job immutable identity conflict' using errcode='23514'; end if;
  if prior.group_key is distinct from r.visual_group_key then
   raise exception 'generated sibling group changed' using errcode='23514'; end if;
  if prior.calendar_row_id is distinct from p_id then
   if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'candidate'
    or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null then
    raise exception 'generated sibling must be unsent' using errcode='23514'; end if;
   -- Approved-visual guard already ran right after the row lock, before
   -- any generated authority checks, for both first reserves and replays.
   raise exception 'detached generated sibling requires separate preparation' using errcode='23514';
  elsif r.source_media_asset_id is distinct from 'generated-astra:'||(c->>'job_id')
    or r.source_media_url is distinct from c->>'original_url' or r.image_url is distinct from c->>'original_url'
    or r.thumbnail_url is not null or r.render_manifest_digest is distinct from m->>'manifest_digest' then
   raise exception 'generated replay binding changed' using errcode='23514'; end if;
  raise exception 'detached replay requires immutable preparation reconciliation' using errcode='23514';
  return jsonb_build_object('reserved',true,'replayed',true,'receipt_ref',prior.receipt_ref,'manifest',m);
 end if;
 if r.status not in ('draft','pending','queued','approved') or r.variant_status is distinct from 'candidate'
  or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null
  or c->>'inventory_revision' is distinct from snap->>'inventory_revision'
  or c->>'history_revision' is distinct from snap->>'history_revision'
  or jsonb_typeof(visuals) is distinct from 'array' or jsonb_array_length(visuals)<>jsonb_array_length(snap#>'{history,rows}') then
  raise exception 'generated candidate stale or unsent depletion unavailable' using errcode='23514'; end if;
 -- A first generated reservation also requires the newest trusted
 -- current-epoch/current-gym census, observed under the
 -- census advisory lock already held above: exact current revision, complete, zero-supply and fresh.
 -- A late local photo arrival between preflight and reserve leaves a newer
 -- positive/incomplete census that an older zero row can never override.
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if coalesce(s.enabled,false) and s.epoch_id is not null then
  select * into i from public.fixer_still_inventory_20261007 ci
   where ci.epoch_id=s.epoch_id and ci.gym_id=c->>'gym_id'
   order by ci.observed_at desc,ci.receipt_id desc limit 1;
 end if;
 if not coalesce(s.enabled,false) or s.epoch_id is null or i.receipt_id is null
  or i.inventory_revision is distinct from snap->>'inventory_revision'
  or not i.local_complete or i.local_available<>0
  or i.observed_at<clock_timestamp()-interval '10 minutes'
  or i.observed_at>clock_timestamp() then
  raise exception 'generated reservation requires fresh local depletion authority' using errcode='23514'; end if;
 for h in select value from jsonb_array_elements(snap#>'{history,rows}') loop
  select value into proof from jsonb_array_elements(visuals) v(value)
   where value->>'history_key'=h->>'history_key'
     and (h->>'visual_sha256' is null or value->>'visual_sha256'=h->>'visual_sha256')
     and value->>'published_binding_ref'=h->>'published_binding_ref';
  if not found or (proof->>'phash' ~ '^scene:phash64:[0-9a-f]{16}$') is distinct from true
   or (proof->>'visual_sha256' ~ '^sha256:[0-9a-f]{64}$') is distinct from true
   or proof->>'visual_url' is distinct from h->>'visual_url'
   or public.fixer_generated_hamming_20261007(c->>'original_phash',proof->>'phash')<=6
   or 'sha256:'||(c->>'original_sha256')=proof->>'visual_sha256'
   or c->>'original_url'=h->>'visual_url' then
   -- Explicit same-date logical siblings can use their already-reserved source.
   if not exists(select 1 from public.fixer_generated_reservation_20261007 g
    where h->>'history_key'='generated-reserved:'||g.job_id::text
     and g.candidate_json->>'gym_id'=c->>'gym_id' and g.candidate_json->>'local_date'=c->>'local_date'
     and g.candidate_json->>'logical_post_id'=c->>'logical_post_id'
     and g.candidate_json->>'original_sha256'=c->>'original_sha256'
     and g.candidate_json->>'original_url'=c->>'original_url') then
    raise exception 'unresolved or repeated historical generated visual' using errcode='23514'; end if;
  end if;
  if h->>'history_key' not like 'retained:%' then
  insert into public.fixer_generated_history_visual_20261007
    (history_key,visual_sha256,published_binding_ref,phash,visual_url,tenant_id,post_date,group_key)
  values(h->>'history_key',proof->>'visual_sha256',h->>'published_binding_ref',proof->>'phash',proof->>'visual_url',
    h->>'tenant_id',(h->>'local_date')::date,h->>'group_key') on conflict do nothing;
  if not exists(select 1 from public.fixer_generated_history_visual_20261007 v
    where v.history_key=h->>'history_key' and v.visual_sha256=proof->>'visual_sha256'
     and v.published_binding_ref=h->>'published_binding_ref' and v.phash=proof->>'phash'
     and v.visual_url=proof->>'visual_url' and v.tenant_id is not distinct from h->>'tenant_id'
     and v.post_date is not distinct from (h->>'local_date')::date
     and v.group_key is not distinct from h->>'group_key') then
   raise exception 'historical perceptual receipt identity conflict' using errcode='23514'; end if;
  elsif proof->>'phash' is distinct from h->>'phash' then
   raise exception 'retained perceptual receipt identity conflict' using errcode='23514';
  end if;
 end loop;
 if exists(select 1 from public.fixer_generated_reservation_20261007 g
  where (g.candidate_json->>'original_sha256'=c->>'original_sha256'
   or g.candidate_json->>'original_md5'=c->>'original_md5'
   or g.candidate_json->>'original_url'=c->>'original_url'
   or public.fixer_generated_hamming_20261007(g.candidate_json->>'original_phash',c->>'original_phash')<=6)
   and not(g.candidate_json->>'gym_id'=c->>'gym_id' and g.candidate_json->>'local_date'=c->>'local_date'
    and g.candidate_json->>'logical_post_id'=c->>'logical_post_id'))
  or exists(select 1 from public.fixer_forward_media_history_clearance_20261006 negative
    where negative.source_fingerprint='md5:'||(c->>'original_md5') and negative.decision<>'cleared_unused')
  or exists(select 1 from public.fixer_forward_media_use_20261006 u where u.fingerprint='md5:'||(c->>'original_md5')
    and not(u.tenant_id=c->>'gym_id' and u.post_date::text=c->>'local_date' and u.group_key=r.visual_group_key)) then
  raise exception 'generated source already reserved by another tenant/date/group' using errcode='23514'; end if;
 fp:='md5:'||(c->>'original_md5'); asset:='generated-astra:'||(c->>'job_id');
 receipt:='generated-reservation:sha256:'||encode(sha256(convert_to(c::text,'UTF8')),'hex');
 if m is distinct from jsonb_build_object('manifest_digest',m->>'manifest_digest','tenant_id',c->>'gym_id','source_asset_id',asset,
  'image_url',c->>'original_url','image_fingerprint',fp,'image_length',(c->>'original_length')::bigint,
  'thumbnail_url',null,'thumbnail_fingerprint',null,'thumbnail_length',null,'operation','same_object','render_recipe',null,'render_evidence_ref',asset)
  or m->>'manifest_digest' is distinct from 'sha256:'||encode(sha256(convert_to(public.fixer_owner_photo_canonical_20261007(m-'manifest_digest'),'UTF8')),'hex') then
  raise exception 'generated original requires exact same-object manifest' using errcode='23514'; end if;
 insert into public.fixer_generated_reservation_20261007(job_id,calendar_row_id,group_key,candidate_json,manifest_json,receipt_ref,history_epoch,approved_source_revision)
 values((c->>'job_id')::uuid,p_id,r.visual_group_key,c,m,receipt,snap#>'{history,epoch}',p_source_revision);
 insert into public.fixer_forward_media_original_registry_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)
 values(c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'astra-job:'||(c->>'job_id'));
 insert into public.fixer_forward_media_history_clearance_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref)
 values(c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'astra-job:'||(c->>'job_id'),'cleared_unused',receipt);
 insert into public.fixer_forward_media_render_manifest_20261006(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,operation,render_recipe,render_evidence_ref)
 values(m->>'manifest_digest',c->>'gym_id',asset,c->>'original_url',fp,(c->>'original_length')::bigint,'same_object',null,asset);
 -- Calendar insertion/activation belongs to separate stage/finalizer.
 return jsonb_build_object('reserved',true,'replayed',false,'receipt_ref',receipt,'manifest',m);
end; $$;

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

-- Original runtime-check SHA-256 afe550a47ade18d757a47ee382a8a1a4aa8c8021afe5e2cb2bbebb4dad66e1ba; exact planned-row fallback only.
create function public.generated_client_runtime_check_20261009(p_id uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; g public.fixer_generated_reservation_20261007%rowtype;
 s record; snap jsonb; h jsonb; ph text; i public.fixer_still_inventory_20261007%rowtype; a public.generated_client_admission_20261009%rowtype; snapshot_id uuid:=p_id;
begin
 select * into r from public.content_calendar where id=p_id;
 if not found then
  select * into a from public.generated_client_admission_20261009 where calendar_row_id=p_id;
  if not found then raise exception 'generated staged preparation missing' using errcode='23514'; end if;
  r:=jsonb_populate_record(null::public.content_calendar,convert_from(a.manifest_bytes,'UTF8')::jsonb#>'{stage_plan,planned_row}');
  snapshot_id:=(convert_from(a.manifest_bytes,'UTF8')::jsonb#>>'{stage_plan,placeholder_row_id}')::uuid;
 end if;
 select * into g from public.fixer_generated_reservation_20261007
  where 'generated-astra:'||(candidate_json->>'job_id')=r.source_media_asset_id and candidate_json->>'gym_id'=r.gym_id;
 if not found then
  if r.source_media_asset_id like 'generated-astra:%' then raise exception 'generated source reservation missing' using errcode='23514'; end if;
  return true;
 end if;
 snap:=public.fixer_generated_snapshot_20261007(snapshot_id);
 perform public.fixer_still_negative_check_20261007(jsonb_build_object('gym_id',g.candidate_json->>'gym_id','source_asset_id','generated-astra:'||g.job_id::text,
  'source_url',g.candidate_json->>'original_url','sha256','sha256:'||(g.candidate_json->>'original_sha256'),
  'md5','md5:'||(g.candidate_json->>'original_md5'),'phash',g.candidate_json->>'original_phash'));
 if exists(select 1 from public.fixer_still_reservation_20261007 still where
   still.original->>'sha256'='sha256:'||(g.candidate_json->>'original_sha256')
   or still.original->>'md5'='md5:'||(g.candidate_json->>'original_md5')
   or still.original->>'source_url'=g.candidate_json->>'original_url'
   or public.fixer_generated_hamming_20261007(still.original->>'phash',g.candidate_json->>'original_phash')<=6) then
  raise exception 'generated visual already held by common still reservation' using errcode='23514'; end if;
 if g.candidate_json->>'local_date' is distinct from r.post_date::text
  or g.candidate_json->>'logical_post_id' is distinct from to_jsonb(r)->>'logical_post_id'
  or g.group_key is distinct from r.visual_group_key
  or g.candidate_json->>'copy_revision' is distinct from snap->>'copy_revision'
  or g.candidate_json->>'inventory_revision' is distinct from snap->>'inventory_revision'
  or snap->'photo_inventory_complete' is distinct from 'true'::jsonb
  or snap->'eligible_photo_count' is distinct from '0'::jsonb
  or snap->'history_complete' is distinct from 'true'::jsonb
  or g.history_epoch is distinct from snap#>'{history,epoch}'
  or g.candidate_json->>'original_url' is distinct from r.source_media_url
  or r.source_media_url is distinct from r.image_url or r.thumbnail_url is not null
  or g.manifest_json->>'manifest_digest' is distinct from r.render_manifest_digest then
  raise exception 'generated current content/depletion/history binding changed' using errcode='23514'; end if;
 -- Final send re-observes local depletion: reserve-time revisions were
 -- identity, not freshness. The single NEWEST trusted current-epoch census at
 -- newest row must match the live inventory revision, be complete, zero-supply and fresh
 -- inside ten minutes. An older zero row never overrides a newer positive or
 -- incomplete census; a missing, stale, noncomplete or nonzero latest row
 -- holds the send, exactly as for stills.
 select * into s from public.fixer_still_cutover_20261007 where singleton;
 if coalesce(s.enabled,false) and s.epoch_id is not null then
  select * into i from public.fixer_still_inventory_20261007 c
   where c.epoch_id=s.epoch_id and c.gym_id=r.gym_id
   order by c.observed_at desc,c.receipt_id desc limit 1;
 end if;
 if not coalesce(s.enabled,false) or s.epoch_id is null or i.receipt_id is null
  or i.inventory_revision is distinct from snap->>'inventory_revision'
  or not i.local_complete or i.local_available<>0
  or i.observed_at<clock_timestamp()-interval '10 minutes'
  or i.observed_at>clock_timestamp() then
  raise exception 'generated final send requires fresh local depletion authority' using errcode='23514'; end if;
 for h in select value from jsonb_array_elements(snap#>'{history,rows}') loop
  -- Known same logical generated siblings, including their committed claim.
  if exists(select 1 from public.fixer_generated_reservation_20261007 sibling
    where sibling.candidate_json->>'gym_id'=g.candidate_json->>'gym_id'
     and sibling.candidate_json->>'local_date'=g.candidate_json->>'local_date'
     and sibling.candidate_json->>'logical_post_id'=g.candidate_json->>'logical_post_id'
     and (h->>'visual_sha256' is null or 'sha256:'||(sibling.candidate_json->>'original_sha256')=h->>'visual_sha256')
     and h->>'tenant_id'=sibling.candidate_json->>'gym_id'
     and h->>'local_date'=sibling.candidate_json->>'local_date' and h->>'group_key'=sibling.group_key
     and h->>'visual_url'=sibling.candidate_json->>'original_url'
     and (h->>'history_key'='generated-reserved:'||sibling.job_id::text
      or exists(select 1 from public.fixer_forward_media_claim_receipt_20261006 claim
        where (coalesce(h->>'origin_history_key',h->>'history_key') in ('claim-source:'||claim.claim_token::text,'claim-image:'||claim.claim_token::text)
          or (h->>'history_key' like 'retained:%'
            and h->>'origin_history_key'='calendar-image:'||claim.calendar_row_id::text
            and nullif(h->>'history_proof_ref','') is not null
            and h->>'visual_sha256'='sha256:'||(sibling.candidate_json->>'original_sha256')))
         and claim.tenant_id=sibling.candidate_json->>'gym_id'
         and claim.post_date::text=sibling.candidate_json->>'local_date'
         and claim.group_key=sibling.group_key
         and claim.source_url=sibling.candidate_json->>'original_url'
         and claim.image_url=claim.source_url and claim.thumbnail_url is null
         and claim.fingerprints=array['md5:'||(sibling.candidate_json->>'original_md5')])
      or exists(select 1 from public.content_calendar live where h->>'history_key'='calendar-image:'||live.id::text
        and live.gym_id=sibling.candidate_json->>'gym_id' and live.post_date::text=sibling.candidate_json->>'local_date'
        and live.visual_group_key=sibling.group_key and live.source_media_asset_id='generated-astra:'||sibling.job_id::text
        and live.image_url=sibling.candidate_json->>'original_url' and live.thumbnail_url is null)
      or (h->>'history_key' like 'retained:%'
        and h->>'origin_history_key'='calendar-image:'||sibling.calendar_row_id::text
        and nullif(h->>'history_proof_ref','') is not null
        and h->>'visual_sha256'='sha256:'||(sibling.candidate_json->>'original_sha256')))) then continue; end if;
  if h->>'history_key' like 'retained:%' then ph:=h->>'phash'; else
  select phash into ph from public.fixer_generated_history_visual_20261007 v where v.history_key=h->>'history_key'
   and v.visual_sha256=h->>'visual_sha256' and v.published_binding_ref=h->>'published_binding_ref'
   and v.visual_url=h->>'visual_url';
  if not found then
   select candidate_json->>'original_phash' into ph from public.fixer_generated_reservation_20261007 sibling
    where h->>'history_key'='generated-reserved:'||sibling.job_id::text;
  end if;
  end if;
  if ph is null or public.fixer_generated_hamming_20261007(ph,g.candidate_json->>'original_phash')<=6
   or h->>'visual_sha256'='sha256:'||(g.candidate_json->>'original_sha256')
   or h->>'visual_url'=g.candidate_json->>'original_url' then
   raise exception 'generated historical perceptual identity unresolved or repeated' using errcode='23514'; end if;
 end loop;
 return true;
end; $$;

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

-- Explicitly reject the abandoned direct ACTIVE-row route.
create or replace function public.generated_client_stage_20261009(p_row uuid,c jsonb,visuals jsonb,m jsonb,
 p_source text,p_version uuid,p_receipt uuid,p_manifest bytea)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin raise exception 'generated clients require separate staged preparation/finalization' using errcode='23514'; end $$;

-- Frozen original finalizer prosrc SHA-256 092e3b3823a26c4ad5b446455274b99e11b4efae5e60632006376654a9a4937b.
-- Public CREATE OR REPLACE preserves its OID, owner and ACL. Exact private
-- helper uses the frozen original body; no caller can invoke it directly.
create table public.generated_client_install_pin_20261009 (
 signature text primary key, body_sha256 text not null, original_oid oid not null,
 original_owner oid not null, original_acl aclitem[], original_config text[], original_definer boolean not null
);
alter table public.generated_client_install_pin_20261009 enable row level security;
revoke all on public.generated_client_install_pin_20261009 from public,anon,authenticated,service_role,
 fixer_forward_media_owner_20261006,generated_hosted_byte_reader_20261009,generated_hosted_byte_issuer_20261009;
insert into public.generated_client_install_pin_20261009
 select 'finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)',
 encode(sha256(convert_to(prosrc,'UTF8')),'hex'),oid,proowner,proacl,proconfig,prosecdef
 from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure;
create function public.generated_client_original_finalizer_20261009(
 p_tenant_id text,p_batch_id uuid,p_candidates jsonb,p_expected_old_rows jsonb
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare
  b public.forward_schedule_stage_batch_20261008%rowtype;
  m public.forward_schedule_stage_member_20261008%rowtype;
  item jsonb; old_snapshot jsonb; r public.content_calendar%rowtype; tenant text;
  candidates uuid[]; member_ids uuid[]; old_ids uuid[]; old_snapshots jsonb[];
  row_ids uuid[]:=array[]::uuid[]; reservations uuid[]:=array[]::uuid[];
  reservation uuid; ids uuid[]; expected uuid;
  v_finalize_request jsonb; receipt jsonb;
begin
  if nullif(btrim(p_tenant_id),'') is null or p_batch_id is null
      or jsonb_typeof(p_candidates) is distinct from 'array'
      or jsonb_array_length(p_candidates)=0
      or jsonb_typeof(p_expected_old_rows) is distinct from 'array' then
    raise exception 'exact schedule batch binding required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  if not exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled) then
    raise exception 'schedule reservation authority is OFF pending review' using errcode='55000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  select * into b from public.forward_schedule_stage_batch_20261008
    where batch_id=p_batch_id for update;
  if not found then
    raise exception 'schedule batch unavailable' using errcode='23514';
  end if;
  if b.tenant_id is distinct from btrim(p_tenant_id) then
    raise exception 'batch tenant mismatch' using errcode='23514';
  end if;
  v_finalize_request:=jsonb_build_object('candidates',p_candidates,'expected_old_rows',p_expected_old_rows);
  if b.state='finalized' then
    if b.finalize_request is distinct from v_finalize_request then
      raise exception 'finalized schedule batch request changed' using errcode='23514';
    end if;
    return b.finalize_receipt;
  end if;
  -- The PERSISTED old-row set is the sole finalization authority. The caller
  -- must present exactly that set (proving it read back the staged batch);
  -- any substitution, omission or addition is refused before any write.
  select coalesce(array_agg(o.calendar_row_id order by o.position),array[]::uuid[]),
         coalesce(array_agg(o.old_snapshot order by o.position),array[]::jsonb[])
    into old_ids, old_snapshots
    from public.forward_schedule_stage_old_row_20261008 o where o.batch_id=b.batch_id;
  if (select coalesce(jsonb_agg(v order by v),'[]'::jsonb) from jsonb_array_elements(p_expected_old_rows) v)
      is distinct from
     (select coalesce(jsonb_agg(s order by s),'[]'::jsonb) from unnest(old_snapshots) s) then
    raise exception 'staged old row set mismatch' using errcode='23514';
  end if;
  select coalesce(array_agg(x.calendar_row_id),array[]::uuid[]) into member_ids
    from public.forward_schedule_stage_member_20261008 x where x.batch_id=b.batch_id;
  select array_agg((v->>'calendar_row_id')::uuid) into candidates from jsonb_array_elements(p_candidates) v;
  if candidates is null or array_position(candidates,null) is not null
      or not (candidates <@ member_ids and member_ids <@ candidates) then
    raise exception 'incomplete staged batch membership' using errcode='23514';
  end if;
  if exists(select 1 from unnest(candidates||old_ids) id group by id having count(*)>1)
      or array_position(old_ids,null) is not null then
    raise exception 'duplicate or missing calendar batch identity' using errcode='22023';
  end if;
  -- Deterministic row order; census serializes all cooperating transactions.
  perform 1 from public.content_calendar where id=any(candidates||old_ids) order by id for update;
  -- CAS re-validation uses ONLY the persisted snapshots: any old row changed
  -- after staging (approval, media edit, claim, send) holds the whole batch.
  for old_snapshot in select s from unnest(old_snapshots) s loop
    select * into r from public.content_calendar where id=(old_snapshot->>'id')::uuid;
    if not found or to_jsonb(r) is distinct from old_snapshot or r.status is null or r.status not in ('pending','draft')
     or r.variant_status is distinct from 'active' or r.media_not_ready_reason is not null or r.publish_claim_token is not null
     or r.publish_reservation_day is not null or r.published_at is not null or r.late_post_id is not null then
     raise exception 'old calendar snapshot changed or protected' using errcode='23514'; end if;
    select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a where a.alias_key=btrim(r.gym_id);
    tenant:=coalesce(tenant,btrim(r.gym_id));
    if tenant is distinct from b.tenant_id then raise exception 'batch tenant mismatch' using errcode='23514'; end if;
  end loop;
  for item in select value from jsonb_array_elements(p_candidates) loop
    select * into r from public.content_calendar where id=(item->>'calendar_row_id')::uuid;
    select * into m from public.forward_schedule_stage_member_20261008 where calendar_row_id=r.id;
    if not found or r.variant_status is distinct from 'candidate' or r.media_not_ready_reason is distinct from 'forward_reservation_staged' or r.status is null or r.status not in ('pending','draft')
     or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null then
     raise exception 'inactive unapproved schedule candidate required' using errcode='23514'; end if;
    -- Immutable staged content/media binding must match exactly; m was found
    -- because candidates <@ member_ids was validated above.
    if m.logical_post_id is distinct from (item->>'logical_post_id')::uuid
     or r.logical_post_id is distinct from m.logical_post_id
     or r.post_date is distinct from m.post_date or r.gym_id is distinct from m.gym_id
     or r.source_media_url is distinct from m.source_media_url
     or r.image_url is distinct from m.image_url
     or r.thumbnail_url is distinct from m.thumbnail_url then
     raise exception 'staged member binding changed' using errcode='23514'; end if;
    -- Full-row binding against the persisted staged snapshot: a caller edit
    -- to ANY planned content/media/tenant field after staging (account,
    -- format, caption, visual_group_key, dates, URLs, ...) refuses the whole
    -- batch before activation. Only the trusted preparation output fields
    -- (source_media_asset_id, render_manifest_digest) may differ from the
    -- staged bytes; the reservation stack re-validates those against the
    -- owner/attester lineage, so a forged value cannot pass.
    if (to_jsonb(r) - 'source_media_asset_id' - 'render_manifest_digest')
       is distinct from (m.staged_snapshot - 'source_media_asset_id' - 'render_manifest_digest') then
     raise exception 'staged member snapshot changed' using errcode='23514'; end if;
    if m.tenant_id is distinct from b.tenant_id then raise exception 'batch tenant mismatch' using errcode='23514'; end if;
    ids:=array(select jsonb_array_elements_text(item->'attestation_ids'))::uuid[];
    expected:=nullif(item->>'expected_reservation_id','')::uuid;
    update public.content_calendar set variant_status='active',media_not_ready_reason=null where id=r.id;
    reservation:=public.reserve_forward_slot_20261008(r.id,(item->>'logical_post_id')::uuid,item->>'expected_revision',ids,expected);
    row_ids:=array_append(row_ids,r.id);
    reservations:=array_append(reservations,reservation);
  end loop;
  -- No old rows change until ALL candidates have passed. Only the persisted
  -- old IDs are archived; a row added after staging is never touched. The
  -- entire function is a transaction, so any following constraint/trigger
  -- failure also rolls back.
  update public.content_calendar set variant_status='archived' where id=any(old_ids);
  for reservation in select x.reservation_id from public.forward_schedule_reservation x
   where x.state='active' and x.calendar_row_id=any(old_ids) and not (x.reservation_id=any(reservations)) loop
   perform public.release_forward_slot_20261008(reservation,'atomic-calendar-replacement');
  end loop;
  receipt:=jsonb_build_object('batch_id',b.batch_id,'tenant_id',b.tenant_id,
    'request_digest',b.request_digest,'state','finalized',
    'row_ids',row_ids,'reservation_ids',reservations,'archived_old_row_ids',old_ids);
  update public.forward_schedule_stage_batch_20261008
    set state='finalized',finalize_request=v_finalize_request,finalize_receipt=receipt,finalized_at=now()
    where batch_id=b.batch_id and state='staged';
  return receipt;
end;
$$;

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
 -- Ordinary batch behavior is exactly the frozen original body.
 if not exists(select 1 from public.forward_schedule_stage_member_20261008 member
  join public.generated_client_admission_20261009 admission on admission.calendar_row_id=member.calendar_row_id
  where member.batch_id=p_batch_id) then
  return public.generated_client_original_finalizer_20261009(p_tenant_id,p_batch_id,p_candidates,p_expected_old_rows); end if;
 if not pg_has_role(caller,'service_role','MEMBER') or pg_has_role(caller,'fixer_forward_media_owner_20261006','MEMBER')
  or pg_has_role(caller,'generated_hosted_byte_issuer_20261009','MEMBER') then
  raise exception 'separate service finalizer required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated finalizer requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
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
-- Wrapper OID/owner/ACL preserved, private original prosrc byte-for-byte.
do $$ declare pin public.generated_client_install_pin_20261009%rowtype; current_proc record; begin
 select * into pin from public.generated_client_install_pin_20261009;
 select * into current_proc from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure;
 if row(current_proc.oid,current_proc.proowner,current_proc.proacl,current_proc.proconfig,current_proc.prosecdef)
  is distinct from row(pin.original_oid,pin.original_owner,pin.original_acl,pin.original_config,pin.original_definer)
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc where oid='public.generated_client_original_finalizer_20261009(text,uuid,jsonb,jsonb)'::regprocedure)
   is distinct from pin.body_sha256 then raise exception 'finalizer identity/body drift' using errcode='23514'; end if;
end $$;
commit;
