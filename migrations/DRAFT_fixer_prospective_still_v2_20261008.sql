-- DRAFT / UNAPPLIED / DEFAULT OFF. Apply after the complete owner/staged,
-- historical v1 exclusion guard and prospective occupancy drafts.
-- Schema 2 signs only no_prior_published_still_image_or_derivative_use.
-- Reviewed videos are individually accounted outside that scope; no video
-- frame clearance or provider publication is asserted. Unknown kinds HOLD.
-- Existing schema 1 certificate/preparation RPCs retain zero-exclusion guards.
-- Shared immutable certificate/reservation storage preserves one history spine;
-- separate v2 record/preparation/admission RPCs reject schema 1 at entry.
-- Rollback: turn the prospective gate OFF. Preserve all receipts and permanent
-- fences after use; do not restore old authority definitions over used rows.
begin;

create function public.fixer_still_photo_snapshot_v2_20261008()
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; videos jsonb;
begin
 snap:=public.fixer_forward_media_photo_snapshot_exclusion_20261008();
 select b.excluded_rows_json into videos from public.fixer_forward_media_photo_baseline_20261007 b
 where b.baseline_id=(snap->>'baseline_id')::uuid;
 return snap||jsonb_build_object('scope','published_still_images_and_derivatives',
   'accounted_video_rows',coalesce(videos,'[]'::jsonb));
end; $$;
revoke all on function public.fixer_still_photo_snapshot_v2_20261008()
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_still_photo_snapshot_v2_20261008()
 to fixer_forward_media_owner_20261006,fixer_forward_media_photo_auditor_20261007;

create function public.fixer_assert_still_packet_v2_20261008(p jsonb,snap jsonb)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare v jsonb; d jsonb;
begin
 if p->'schema_version' is distinct from '2'::jsonb
   or p->>'decision' is distinct from 'no_prior_published_still_image_or_derivative_use'
   or p->>'scope' is distinct from 'published_still_images_and_derivatives'
   or p->>'candidate_media_kind' is distinct from 'still_photo'
   or snap->'policy_approved' is distinct from 'true'::jsonb
   or snap->'scope_complete' is distinct from 'true'::jsonb
   or p->>'accounted_video_digest' is distinct from snap->>'excluded_rows_digest'
   or jsonb_typeof(p->'accounted_videos') is distinct from 'array'
   or jsonb_array_length(p->'accounted_videos') is distinct from jsonb_array_length(snap->'accounted_video_rows')
   or jsonb_array_length(p->'accounted_videos')>2500
   or (select count(distinct value->>'history_key') from jsonb_array_elements(p->'accounted_videos'))
      <>jsonb_array_length(snap->'accounted_video_rows')
   or nullif(btrim(p->>'stated_visual_uncertainty'),'') is null then
  raise exception 'complete separately versioned still scope required' using errcode='23514'; end if;
 for v in select value from jsonb_array_elements(snap->'accounted_video_rows') loop
  select value into d from jsonb_array_elements(p->'accounted_videos')
    where value->>'history_key'=v->>'history_key';
  if v->>'media_kind' is distinct from 'reviewed_video_scope_exclusion'
   or nullif(v->>'published_binding_ref','') is null
   or d->>'published_binding_ref' is distinct from v->>'published_binding_ref'
   or d->>'disposition' is distinct from 'accounted_out_of_scope_video_frames_unreviewed'
   or nullif(btrim(d->>'review_evidence_ref'),'') is null then
   raise exception 'unknown media kind or unaccounted video HOLD' using errcode='23514'; end if;
 end loop;
 if exists(select 1 from jsonb_array_elements(snap->'rows') h
   where h->'resolved' is distinct from 'true'::jsonb or h->>'media_kind' is distinct from 'still_photo') then
  raise exception 'unknown or ambiguous still history HOLD' using errcode='23514'; end if;
 if not exists(select 1 from public.content_calendar r
   where r.id=(p#>>'{candidate,calendar_row_id}')::uuid
    and r.logical_post_id::text=p#>>'{candidate,logical_post_id}') then
  raise exception 'exact signed logical post required' using errcode='23514'; end if;
end; $$;
revoke all on function public.fixer_assert_still_packet_v2_20261008(jsonb,jsonb)
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,
 fixer_forward_media_owner_20261006,fixer_forward_media_photo_auditor_20261007;

-- Clone only the frozen validated v1 recorder's composed body; retain all
-- authenticated verifier, current source, content, uniqueness and replay checks.
-- Schema version, decision, exclusion accounting and RPC name are distinct.
do $clone$
declare definition text;
begin
 definition:=pg_get_functiondef('public.fixer_forward_media_photo_record_20261007(text,text,text)'::regprocedure);
 definition:=replace(definition,'fixer_forward_media_photo_record_20261007','fixer_still_photo_record_v2_20261008');
 definition:=replace(definition,'perform public.fixer_assert_photo_no_exclusions_20261008();','');
 definition:=replace(definition,'fixer_forward_media_photo_snapshot_20261007()','fixer_still_photo_snapshot_v2_20261008()');
 definition:=replace(definition,'p->''schema_version'' is distinct from ''1''::jsonb','p->''schema_version'' is distinct from ''2''::jsonb');
 definition:=replace(definition,'''reviewed_no_prior_visual_use''','''no_prior_published_still_image_or_derivative_use''');
 definition:=replace(definition,'snapshot:=public.fixer_still_photo_snapshot_v2_20261008();',
  'snapshot:=public.fixer_still_photo_snapshot_v2_20261008(); perform public.fixer_assert_still_packet_v2_20261008(p,snapshot);');
 execute definition;
end; $clone$;
revoke all on function public.fixer_still_photo_record_v2_20261008(text,text,text)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_still_photo_record_v2_20261008(text,text,text)
 to fixer_forward_media_photo_auditor_20261007;

-- Reuse exact full owner source/rendition and certified-positive-clearance
-- triggers. Old v1 preparation remains guarded and cannot consume v2.
do $clone$
declare old_name text; new_name text; definition text;
begin
 foreach old_name in array array['fixer_prepare_owner_photo_20261007','fixer_prepare_owner_staged_photo_20261008'] loop
  new_name:=case old_name when 'fixer_prepare_owner_photo_20261007' then 'fixer_prepare_owner_still_v2_20261008' else 'fixer_prepare_owner_staged_still_v2_20261008' end;
  definition:=pg_get_functiondef(to_regprocedure('public.'||old_name||'(uuid,jsonb,jsonb)'));
  if definition is null then raise exception 'full owner/staged preparation stack required'; end if;
  definition:=replace(definition,old_name,new_name);
  definition:=replace(definition,'perform public.fixer_assert_photo_no_exclusions_20261008();','');
  definition:=replace(definition,'candidate:=cert.payload_json::jsonb->''candidate'';',
   'candidate:=cert.payload_json::jsonb->''candidate''; if not exists(select 1 from public.forward_prospective_photo_gate_20261008 where singleton and enabled) then raise exception ''prospective photo authority is OFF pending review'' using errcode=''55000''; end if; perform public.fixer_assert_still_packet_v2_20261008(cert.payload_json::jsonb,public.fixer_still_photo_snapshot_v2_20261008());');
  execute definition;
  execute 'revoke all on function public.'||new_name||'(uuid,jsonb,jsonb) from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007';
  execute 'grant execute on function public.'||new_name||'(uuid,jsonb,jsonb) to fixer_forward_media_owner_20261006';
 end loop;
end; $clone$;

-- Validate an exact immutable owner receipt at shared downstream boundaries.
-- Only v2 has this exemption; absence always executes the preserved v1 guard.
create function public.fixer_assert_still_row_v2_or_v1_20261008(p_id uuid)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare p jsonb; content jsonb; grant_row public.fixer_owner_photo_reservation_20261007%rowtype;
begin
 select c.payload_json::jsonb into p
 from public.fixer_owner_photo_reservation_20261007 r
 join public.fixer_forward_media_photo_certificate_20261007 c on c.audit_id=r.audit_id
 join public.fixer_forward_media_photo_state_20261007 state on state.singleton
 join public.fixer_forward_media_photo_key_20261007 k on k.key_id=c.key_id
 join public.fixer_forward_media_photo_policy_20261007 policy on policy.policy_id=k.policy_id
 where r.calendar_row_id=p_id and (c.payload_json::jsonb)->'schema_version'='2'::jsonb
   and c.baseline_id=state.baseline_id and c.generation=state.generation
   and state.enabled and k.approved and policy.approved and c.verified_by=k.verifier_role
   and not exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=c.audit_id)
   and not exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id)
 order by r.reserved_at desc limit 1;
 if p is null then perform public.fixer_assert_photo_no_exclusions_20261008(); return; end if;
 select * into grant_row from public.fixer_owner_photo_reservation_20261007 where audit_id=(p->>'audit_id')::uuid;
 if not exists(select 1 from public.forward_prospective_photo_gate_20261008 where singleton and enabled) then
  raise exception 'prospective photo authority is OFF pending review' using errcode='55000'; end if;
 perform public.fixer_assert_still_packet_v2_20261008(p,public.fixer_still_photo_snapshot_v2_20261008());
 content:=public.fixer_forward_media_photo_content_20261007(p_id);
 if p#>>'{candidate,content_digest}' is distinct from 'sha256:'||encode(sha256(convert_to(content::text,'UTF8')),'hex')
   or not public.fixer_owner_photo_source_ready_20261007(p#>>'{candidate,source_asset_id}',p#>>'{candidate,source_sha256}')
   or not exists(select 1 from public.content_calendar r where r.id=p_id
    and r.source_media_asset_id=p#>>'{candidate,source_asset_id}'
    and r.source_media_url=p#>>'{candidate,source_url}'
    and r.render_manifest_digest=grant_row.manifest_json->>'manifest_digest') then
  raise exception 'current exact still v2 receipt binding required' using errcode='23514'; end if;
end; $$;
revoke all on function public.fixer_assert_still_row_v2_or_v1_20261008(uuid)
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;

-- Permanent global consumption uses ALL prospective rows, including revoked.
-- Follow the MD5 connected component in BOTH directions through images/
-- thumbnails; an ancestor of an already occupied derivative is also consumed; inspect
-- SHA and near pHash across all roles, not only the candidate source.
create function public.fixer_prospective_conflict_fence_20261008(p_id uuid,p_require_proof boolean)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; tenant text; hashes text[]; shas text[];
 x public.forward_prospective_photo_occupancy_20261008%rowtype; rev text; proof jsonb;
begin
 select * into r from public.content_calendar where id=p_id;
 if not found then raise exception 'exact prospective boundary row unavailable' using errcode='23514'; end if;
 select coalesce((select a.tenant_id from public.fixer_forward_media_tenant_alias_20261006 a where a.alias_key=btrim(r.gym_id)),btrim(r.gym_id)) into tenant;
 with recursive seed(fp) as (
  select source_fingerprint from public.fixer_forward_media_source_receipt_20261007 where calendar_row_id=r.id
  union select image_fingerprint from public.fixer_forward_media_render_manifest_20261006 where manifest_digest=r.render_manifest_digest
  union select thumbnail_fingerprint from public.fixer_forward_media_render_manifest_20261006 where manifest_digest=r.render_manifest_digest
  union select source_fingerprint from public.fixer_forward_media_original_registry_20261006 where tenant_id=tenant and source_asset_id=r.source_media_asset_id
 ), ancestry(fp) as (
  select fp from seed where fp is not null
  union
  select case when original.fingerprint=a.fp then delivered.fingerprint else original.fingerprint end
   from ancestry a
   join public.fixer_forward_media_object_read_20261006 original on true
   join public.fixer_forward_media_lineage_20261006 edge on edge.source_read_receipt=original.receipt_id
   join public.fixer_forward_media_object_read_20261006 delivered on delivered.receipt_id=any(array[edge.image_read_receipt,edge.thumbnail_read_receipt])
    and (original.fingerprint=a.fp or delivered.fingerprint=a.fp)
 ) select array_agg(distinct fp) into hashes from (
  select fp from ancestry union all
  select image_fingerprint from public.fixer_forward_media_render_manifest_20261006 where manifest_digest=r.render_manifest_digest union all
  select thumbnail_fingerprint from public.fixer_forward_media_render_manifest_20261006 where manifest_digest=r.render_manifest_digest
 ) all_hashes where fp is not null;
 select array_agg(distinct source_sha256) into shas from public.forward_media_visual_attestation a
 where a.tenant_key=tenant and a.media_url=any(array[r.source_media_url,r.image_url,r.thumbnail_url]);
 for x in select o.* from public.forward_prospective_photo_occupancy_20261008 o where
   o.source_sha256=any(coalesce(shas,array[]::text[]))
   or ('md5:'||o.source_md5)=any(coalesce(hashes,array[]::text[]))
   or ('md5:'||o.image_md5)=any(coalesce(hashes,array[]::text[]))
   or ('md5:'||o.thumbnail_md5)=any(coalesce(hashes,array[]::text[]))
   or exists(select 1 from public.forward_media_visual_attestation a where a.tenant_key=tenant
     and a.media_url=any(array[r.source_media_url,r.image_url,r.thumbnail_url])
     and a.phash_version=1 and length(replace(((a.phash_v1 # o.phash_v1)::bit(64))::text,'0',''))<=30)
   or exists(select 1 from public.forward_prospective_photo_binding_20261008 occupied_binding
     join public.forward_media_visual_attestation occupied_role on occupied_role.attestation_id=any(occupied_binding.attestation_ids)
     join public.forward_media_visual_attestation outgoing_role on outgoing_role.tenant_key=tenant
       and outgoing_role.media_url=any(array[r.source_media_url,r.image_url,r.thumbnail_url])
     where occupied_binding.occupancy_id=o.occupancy_id
       and (outgoing_role.source_sha256=occupied_role.source_sha256
         or (outgoing_role.phash_version=1 and occupied_role.phash_version=1
           and length(replace(((outgoing_role.phash_v1 # occupied_role.phash_v1)::bit(64))::text,'0',''))<=30)))
 loop
  if x.state<>'active' or x.tenant_id is distinct from tenant or x.post_date is distinct from r.post_date
    or x.logical_post_id is distinct from r.logical_post_id then
   raise exception 'permanent prospective still occupancy conflict or review HOLD' using errcode='23514'; end if;
  if p_require_proof then
   rev:=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision';
   if not exists(select 1 from public.forward_prospective_photo_binding_20261008 b
     join public.fixer_forward_media_lineage_20261006 l on l.evidence_id=b.lineage_evidence_id
     where b.occupancy_id=x.occupancy_id and b.calendar_row_id=r.id and b.row_revision=rev
       and l.calendar_row_id=r.id and l.row_revision=rev and l.tenant_id=tenant
       and l.source_asset_id=r.source_media_asset_id and l.manifest_digest=r.render_manifest_digest
       and l.source_read_receipt=x.source_read_receipt
       and exists(select 1 from public.forward_media_visual_attestation a where a.attestation_id=any(b.attestation_ids) and a.role='original' and a.source_sha256=x.source_sha256)) then
    raise exception 'exact immutable prospective receipt required at boundary' using errcode='23514'; end if;
   proof:=public.forward_prospective_photo_proof_20261008(r.id,x.source_sha256);
  end if;
 end loop;
 if p_require_proof and proof is null and exists(select 1 from public.fixer_owner_photo_reservation_20261007 g join public.fixer_forward_media_photo_certificate_20261007 c on c.audit_id=g.audit_id where g.calendar_row_id=r.id and c.payload_json::jsonb->'schema_version'='2'::jsonb) then
  raise exception 'still v2 boundary requires permanent prospective admission' using errcode='23514'; end if;
 return proof;
end; $$;
revoke all on function public.fixer_prospective_conflict_fence_20261008(uuid,boolean)
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;

-- Owner-only v2 admission, retaining all trusted attestation/negative/use/
-- reservation checks, exact revision binding and immutable occupancy writes.
do $clone$
declare definition text;
begin
 definition:=pg_get_functiondef('public.admit_prospective_photo_occupancy_20261008(uuid,uuid,text,uuid[],uuid)'::regprocedure);
 definition:=replace(definition,'admit_prospective_photo_occupancy_20261008','admit_prospective_still_v2_20261008');
 definition:=replace(definition,'perform public.fixer_assert_photo_no_exclusions_20261008();','perform public.fixer_assert_still_row_v2_or_v1_20261008(p_calendar_row_id);');
 definition:=replace(definition,'''reviewed_no_prior_visual_use''','''no_prior_published_still_image_or_derivative_use''');
 definition:=replace(definition,'or r.variant_status is distinct from ''active''',
   'or not (r.variant_status=''active'' or (r.variant_status=''candidate'' and r.media_not_ready_reason=''forward_reservation_staged'' and exists(select 1 from public.forward_schedule_stage_member_20261008 sm join public.forward_schedule_stage_batch_20261008 sb on sb.batch_id=sm.batch_id where sm.calendar_row_id=r.id and sb.state=''staged'')))');
 definition:=replace(definition,'or r.media_not_ready_reason is not null',
   'or (r.media_not_ready_reason is not null and r.media_not_ready_reason<>''forward_reservation_staged'')');
 definition:=replace(definition,'where x.state=''active'' and x.source_sha256=src_sha',
   'where x.source_sha256=src_sha');
 definition:=replace(definition,'where x.state=''active'' and x.source_sha256 is distinct from src_sha',
   'where x.source_sha256 is distinct from src_sha');
 definition:=replace(definition,'expected_url:=case att.role',
   'if att.source_sha256 is distinct from replace(coalesce(candidate->>(case att.role when ''original'' then ''source_sha256'' when ''delivered'' then ''image_sha256'' else case when r.thumbnail_url is null then ''image_sha256'' else ''thumbnail_sha256'' end end),''''),''sha256:'','''') then raise exception ''prospective signed role SHA mismatch'' using errcode=''23514''; end if; expected_url:=case att.role');
 definition:=replace(definition,'-- Slot resolution under the slot advisory lock;',
   'perform public.fixer_prospective_conflict_fence_20261008(r.id,false); -- Slot resolution under the slot advisory lock;');
 execute definition;
end; $clone$;
revoke all on function public.admit_prospective_still_v2_20261008(uuid,uuid,text,uuid[],uuid)
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.admit_prospective_still_v2_20261008(uuid,uuid,text,uuid[],uuid)
 to fixer_forward_media_owner_20261006;

-- Adapt OIDs in place across every shared reachable boundary, including
-- internal renamed implementations and both finalizer replay branches.
-- Schema1-only owner/recorder RPCs are deliberately not adapted.
do $fence$
declare f record; body text; definition text; binding text; require_proof boolean;
 names text[]:=array['fixer_forward_media_provenance_lookup_20261006','fixer_photo_base_provenance_20261007',
 'fixer_pre_generated_provenance_20261007','fixer_pre_still_provenance_20261007','fixer_pre_staged_provenance_20261008',
 'fixer_claim_forward_media_internal_20261008','fixer_forward_visual_index_claim_internal_schedule_20261008',
 'admit_prospective_photo_occupancy_20261008','reserve_forward_slot_20261008','forward_reservation_proof_20261008','fixer_claim_forward_media_20261006',
 'fixer_forward_visual_index_claim_20261008','finalize_forward_schedule_batch_20261008','finalize_forward_schedule_staged_batch_20261008'];
begin
 for f in select p.oid,p.proname,p.prosrc,p.proargnames from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='public' and p.proname=any(names) loop
  body:=f.prosrc;
  if position('fixer_prospective_conflict_fence_20261008' in body)>0 then continue; end if;
  require_proof:=f.proname not like '%provenance%' and f.proname<>'admit_prospective_photo_occupancy_20261008';
  if f.proname like 'finalize%' then
   binding:='perform public.fixer_prospective_conflict_fence_20261008((v->>''calendar_row_id'')::uuid,true) from jsonb_array_elements(p_candidates) v; perform public.fixer_assert_still_row_v2_or_v1_20261008((v->>''calendar_row_id'')::uuid) from jsonb_array_elements(p_candidates) v;';
  else
   binding:='perform public.fixer_prospective_conflict_fence_20261008('||quote_ident(f.proargnames[1])||','||case when require_proof then 'true' else 'false' end||'); perform public.fixer_assert_still_row_v2_or_v1_20261008('||quote_ident(f.proargnames[1])||');';
  end if;
  if f.proname='admit_prospective_photo_occupancy_20261008' then
   binding:='perform public.fixer_prospective_conflict_fence_20261008(p_calendar_row_id,false);';
  else
   body:=replace(body,'perform public.fixer_assert_photo_no_exclusions_20261008();',binding);
  end if;
  -- Cover boundaries lacking a prior guard too and repeat after every graph
  -- and census acquisition, before row locks/replays or changed snapshots.
  body:=regexp_replace(body,'(^|
)([ 	]*begin[ 	]*
)',E'\\1\\2 '||binding||E'
','i');
  body:=regexp_replace(body,'(perform pg_advisory_xact_lock[^;]*(fixer_forward_graph_20261006|fixer_forward_photo_census_20261007)[^;]*;)',E'\\1\n '||binding,'gi');
  definition:=pg_get_functiondef(f.oid);
  execute replace(definition,f.prosrc,body);
 end loop;
end; $fence$;
-- V1-only preparation must never consume a v2 receipt, even on a later zero
-- exclusion baseline. Its preserved guard runs first; schema refusal prevents
-- inheriting the v2 default-OFF gate through the older owner entry points.
do $v1$
declare f record; body text;
begin
 for f in select p.oid,p.prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='public' and p.proname=any(array['fixer_prepare_owner_photo_20261007','fixer_prepare_owner_staged_photo_20261008']) loop
  body:=replace(f.prosrc,'candidate:=cert.payload_json::jsonb->''candidate'';',
    'if cert.payload_json::jsonb->''schema_version'' is distinct from ''1''::jsonb then raise exception ''v1 owner preparation requires schema 1 certificate'' using errcode=''23514''; end if; candidate:=cert.payload_json::jsonb->''candidate'';');
  execute replace(pg_get_functiondef(f.oid),f.prosrc,body);
 end loop;
end; $v1$;

-- Read-only planner conflicts include permanent prospective occupancy even
-- after schedule release/revocation and when the feature gate is OFF. This
-- advisory check never substitutes for the locked proof checks above.
do $planner$
declare definition text; marker text := 'return jsonb_build_object(''allowed'',jsonb_array_length(conflicts)=0,''conflicts'',conflicts);';
begin
 definition:=pg_get_functiondef('public.check_reservation_conflicts_20261008(text,date,uuid,text,bigint)'::regprocedure);
 if position(marker in definition)=0 then raise exception 'reservation planner conflict contract changed; review required'; end if;
 definition:=replace(definition,marker,$body$
 select conflicts||coalesce(jsonb_agg(jsonb_build_object('kind','permanent_prospective_still',
   'occupancy_id',x.occupancy_id,'tenant_id',x.tenant_id,'post_date',x.post_date,
   'state',x.state)),'[]'::jsonb) into conflicts
 from public.forward_prospective_photo_occupancy_20261008 x
 where (x.state<>'active' or x.tenant_id is distinct from p_tenant_id
   or x.post_date is distinct from p_post_date or x.logical_post_id is distinct from p_logical_post_id)
  and (x.source_sha256=p_source_sha256
    or length(replace(((p_phash_v1 # x.phash_v1)::bit(64))::text,'0',''))<=30
    or exists(select 1 from public.forward_prospective_photo_binding_20261008 b
      join public.forward_media_visual_attestation a on a.attestation_id=any(b.attestation_ids)
      where b.occupancy_id=x.occupancy_id and (a.source_sha256=p_source_sha256
        or (a.phash_version=1 and length(replace(((p_phash_v1 # a.phash_v1)::bit(64))::text,'0',''))<=30))));
 return jsonb_build_object('allowed',jsonb_array_length(conflicts)=0,'conflicts',conflicts);
 $body$);
 execute definition;
end; $planner$;

commit;
