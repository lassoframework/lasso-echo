-- DRAFT / UNAPPLIED / DEFAULT OFF. Apply AFTER full 0625 and hosted-byte authority.
-- No issuer credential/grant. Binds committed receipt to the existing isolated
-- owner reservation. Keeps portal digest/actor/CAS and every existing send gate.
begin;
do $$ begin
 if current_user <> 'postgres'
  or to_regprocedure('public.generated_hosted_byte_lookup_20261009(text,uuid,text,text,text,uuid)') is null
  or to_regprocedure('public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)') is null
  or not exists(select 1 from pg_trigger where tgrelid='public.content_calendar'::regclass
    and tgname='zzz_calendar_generated_approval_guard' and tgenabled='O') then
  raise exception 'full generated client stack required' using errcode='23514'; end if;
end $$;

-- Operator provisioning only. An absent row or disabled control holds all new
-- admissions AND subsequent approval/send. Owner/reader roles cannot arm it.
create table public.generated_client_control_20261009 (
 owner_principal name not null, gym_id text not null,
 reader_principal name not null, enabled boolean not null default false,
 primary key(owner_principal,gym_id)
);
create table public.generated_client_admission_20261009 (
 calendar_row_id uuid primary key, job_id uuid not null,
 owner_principal name not null, gym_id text not null,
 artifact_version_id uuid not null references public.calendar_generated_artifact_versions(id),
 receipt_id uuid not null references public.generated_hosted_byte_receipts_20261009(receipt_id),
 manifest_sha256 text not null, manifest_bytes bytea not null,
 candidate_json jsonb not null, reservation_manifest jsonb not null,
 source_revision text not null, row_binding jsonb not null,
 admitted_at timestamptz not null default clock_timestamp(),
 unique(artifact_version_id),unique(receipt_id)
);
do $$ declare t text; begin
 foreach t in array array['generated_client_control_20261009','generated_client_admission_20261009'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,generated_hosted_byte_reader_20261009,generated_hosted_byte_issuer_20261009',t);
 end loop;
end $$;
create trigger generated_client_immutable before update or delete on public.generated_client_admission_20261009
 for each row execute function public.generated_hosted_byte_immutable_20261009();
create trigger generated_client_no_truncate before truncate on public.generated_client_admission_20261009
 for each statement execute function public.generated_hosted_byte_immutable_20261009();

create function public.generated_client_row_binding_20261009(r public.content_calendar)
returns jsonb language sql immutable set search_path=pg_catalog,public as $$
 select jsonb_build_object('id',r.id,'gym_id',r.gym_id,'post_date',r.post_date,
  'logical_post_id',r.logical_post_id,'visual_group_key',r.visual_group_key,
  'caption',r.caption,'account',r.account,'format',r.format,'byte_hash',r.byte_hash,
  'gbp_topic_type',r.gbp_topic_type,'gbp_cta_type',r.gbp_cta_type,'gbp_cta_url',r.gbp_cta_url,
  'gbp_event',r.gbp_event,'gbp_offer',r.gbp_offer,'gbp_location_id',r.gbp_location_id);
$$;

-- Every caller uses only committed receipt tables; a version row on its own is
-- insufficient. Hold config and reader-grant revocation through the decision.
create function public.generated_client_evidence_check_20261009(a public.generated_client_admission_20261009)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare ctrl public.generated_client_control_20261009%rowtype; grant_row public.generated_hosted_byte_principals_20261009%rowtype;
 receipt public.generated_hosted_byte_receipts_20261009%rowtype; version public.calendar_generated_artifact_versions%rowtype;
begin
 select * into ctrl from public.generated_client_control_20261009
  where owner_principal=a.owner_principal and gym_id=a.gym_id for share;
 if not found or not ctrl.enabled
  or not pg_has_role(ctrl.reader_principal,'generated_hosted_byte_reader_20261009','MEMBER')
  or pg_has_role(ctrl.reader_principal,'generated_hosted_byte_issuer_20261009','MEMBER')
  or pg_has_role(ctrl.reader_principal,'service_role','MEMBER')
  or pg_has_role(ctrl.reader_principal,'fixer_forward_media_owner_20261006','MEMBER')
  or exists(select 1 from pg_roles where rolname=ctrl.reader_principal and (rolsuper or rolbypassrls)) then
  raise exception 'generated client control or dedicated reader unavailable' using errcode='42501'; end if;
 select * into grant_row from public.generated_hosted_byte_principals_20261009
  where principal=ctrl.reader_principal and gym_id=a.gym_id for share;
 if not found or not grant_row.can_lookup or grant_row.can_issue then
  raise exception 'generated client reader grant unavailable' using errcode='42501'; end if;
 select * into receipt from public.generated_hosted_byte_receipts_20261009 where receipt_id=a.receipt_id;
 select * into version from public.calendar_generated_artifact_versions where id=a.artifact_version_id;
 if receipt.receipt_id is null or version.id is null
  or row(receipt.artifact_version_id,receipt.gym_id,receipt.hosted_url,receipt.delivered_sha256,receipt.manifest_sha256,receipt.manifest_bytes)
    is distinct from row(a.artifact_version_id,a.gym_id,a.candidate_json->>'original_url',a.candidate_json->>'original_sha256',a.manifest_sha256,a.manifest_bytes)
  or row(version.gym_id,version.image_url,version.delivered_sha256,version.render_manifest_digest)
    is distinct from row(receipt.gym_id,receipt.hosted_url,receipt.delivered_sha256,receipt.manifest_sha256)
  or version.delivery_receipt is distinct from jsonb_build_object('receipt_id',receipt.receipt_id,
    'gym_id',receipt.gym_id,'artifact_version_id',receipt.artifact_version_id,'hosted_url',receipt.hosted_url,
    'delivered_sha256',receipt.delivered_sha256,'render_manifest',convert_from(receipt.manifest_bytes,'UTF8')::jsonb)
  or encode(sha256(a.manifest_bytes),'hex') is distinct from a.manifest_sha256 then
  raise exception 'generated client committed receipt mismatch' using errcode='23514'; end if;
 return true;
end $$;

-- Runs before existing generated policy/approval triggers. Only an already
-- created owner admission and its matching real reservation can add identity.
create function public.generated_client_calendar_guard_20261009()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare a public.generated_client_admission_20261009%rowtype; g public.fixer_generated_reservation_20261007%rowtype;
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
 if tg_op='UPDATE' and old.creative_origin is null and old.source_media_asset_id is null
  and old.generated_artifact_version_id is null and old.generated_artifact_sha256 is null then
  if session_user is distinct from a.owner_principal or new.status is distinct from 'pending'
   or new.approval_kind is not null or new.approved_by is not null or new.approved_at is not null
   or new.approval_digest is not null then
   raise exception 'generated client stage must be owner pending without proof' using errcode='23514'; end if;
  new.creative_origin:='generated';new.generated_artifact_version_id:=a.artifact_version_id;
  new.generated_artifact_sha256:=a.candidate_json->>'original_sha256';
 elsif new.creative_origin is distinct from 'generated'
  or new.generated_artifact_version_id is distinct from a.artifact_version_id
  or new.generated_artifact_sha256 is distinct from a.candidate_json->>'original_sha256' then
  raise exception 'generated client artifact identity immutable' using errcode='23514';
 end if;
 if new.status in ('approved','publishing','published') then
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
create trigger "001_generated_client_admission" before insert or update on public.content_calendar
 for each row execute function public.generated_client_calendar_guard_20261009();

create function public.generated_client_stage_20261009(p_row uuid,c jsonb,visuals jsonb,m jsonb,
 p_source text,p_version uuid,p_receipt uuid,p_manifest bytea)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin raise exception 'generated clients require separate staged preparation/finalization' using errcode='23514'; end $$;

create function public.generated_client_reconcile_20261009(p_row uuid,p_version uuid,p_receipt uuid,p_manifest_sha text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare a public.generated_client_admission_20261009%rowtype; r public.content_calendar%rowtype;
begin
 if not pg_has_role(session_user,'fixer_forward_media_owner_20261006','MEMBER')
  or pg_has_role(session_user,'service_role','MEMBER')
  or pg_has_role(session_user,'generated_hosted_byte_issuer_20261009','MEMBER') then
  raise exception 'isolated generated client owner required' using errcode='42501'; end if;
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into a from public.generated_client_admission_20261009 where calendar_row_id=p_row;
 if not found then return null; end if;
 if a.owner_principal is distinct from session_user or a.artifact_version_id is distinct from p_version
  or a.receipt_id is distinct from p_receipt or a.manifest_sha256 is distinct from p_manifest_sha then
  raise exception 'generated client reconciliation binding changed' using errcode='23514'; end if;
 perform public.generated_client_evidence_check_20261009(a);
 select * into r from public.content_calendar where id=p_row;
 if public.generated_client_row_binding_20261009(r) is distinct from a.row_binding
  or r.creative_origin is distinct from 'generated' or r.generated_artifact_version_id is distinct from p_version
  or r.generated_artifact_sha256 is distinct from a.candidate_json->>'original_sha256' then
  raise exception 'generated client reconciliation row changed' using errcode='23514'; end if;
 perform public.fixer_generated_runtime_check_20261007(p_row);
 perform public.fixer_generated_bundle_validate_20261007(r.gym_id,a.candidate_json->'authority_pins',
  a.candidate_json->'copy_derivation_receipt',r.caption,a.candidate_json->>'copy_digest',
  a.candidate_json->>'palette_digest',a.candidate_json->>'palette_revision');
 return jsonb_build_object('admitted',true,'replayed',true,'reserved',true,'calendar_row_id',p_row,
  'artifact_version_id',p_version,'receipt_id',p_receipt,'manifest_sha256',p_manifest_sha);
end $$;
revoke all on function public.generated_client_row_binding_20261009(public.content_calendar),
 public.generated_client_evidence_check_20261009(public.generated_client_admission_20261009),
 public.generated_client_calendar_guard_20261009(),
 public.generated_client_stage_20261009(uuid,jsonb,jsonb,jsonb,text,uuid,uuid,bytea),
 public.generated_client_reconcile_20261009(uuid,uuid,uuid,text)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,
 generated_hosted_byte_reader_20261009,generated_hosted_byte_issuer_20261009;
grant execute on function public.generated_client_stage_20261009(uuid,jsonb,jsonb,jsonb,text,uuid,uuid,bytea),
 public.generated_client_reconcile_20261009(uuid,uuid,uuid,text) to fixer_forward_media_owner_20261006;
commit;
