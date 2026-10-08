-- DRAFT / UNAPPLIED / DEFAULT OFF. Apply after the complete still schema 2 stack.
-- Dedicated owner RPCs only. No role/login, key, activation, approval or send.
-- Reserve COMMIT must be acknowledged before remote reads. Locked + authority +
-- finish share ONE transaction; commit ambiguity stops this runtime. Recovery
-- reads immutable status for the exact token, never replays an uncertain write.
-- Quarantine is permanent until independently reconciled; no expiry/delete.
begin;

create table public.fixer_still_v2_owner_progress_20261008 (
 audit_id uuid not null references public.fixer_forward_media_photo_certificate_20261007(audit_id),
 phase text not null check(phase in ('prepare','admit')),
 attempt_token uuid not null unique,
 candidate_json jsonb not null check(jsonb_typeof(candidate_json)='object'),
 state text not null default 'quarantine' check(state in ('quarantine','final')),
 outcome jsonb, reserved_at timestamptz not null default clock_timestamp(), finished_at timestamptz,
 primary key(audit_id,phase),
 check((state='quarantine' and outcome is null and finished_at is null)
    or (state='final' and outcome is not null and finished_at is not null))
);
alter table public.fixer_still_v2_owner_progress_20261008 enable row level security;
revoke all on public.fixer_still_v2_owner_progress_20261008 from public,anon,authenticated,service_role,
 fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;

-- Preserve identity and completed/quarantined evidence even from accidental
-- privileged table writes. Only one quarantine -> final transition is legal.
create function public.fixer_still_v2_owner_progress_guard_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if tg_op<>'UPDATE' then raise exception 'owner progress evidence cannot be deleted or truncated' using errcode='23514'; end if;
 if old.state<>'quarantine' or new.state<>'final'
  or new.audit_id is distinct from old.audit_id or new.phase is distinct from old.phase
  or new.attempt_token is distinct from old.attempt_token or new.candidate_json is distinct from old.candidate_json
  or new.reserved_at is distinct from old.reserved_at then
  raise exception 'immutable owner phase evidence conflict' using errcode='23514'; end if;
 return new;
end; $$;
create trigger immutable_phase_identity before update or delete on public.fixer_still_v2_owner_progress_20261008
 for each row execute function public.fixer_still_v2_owner_progress_guard_20261008();
create trigger immutable_phase_truncate before truncate on public.fixer_still_v2_owner_progress_20261008
 for each statement execute function public.fixer_still_v2_owner_progress_guard_20261008();

create function public.fixer_assert_still_v2_owner_20261008()
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
begin
 perform public.fixer_assert_owner_photo_runtime_20261007();
 if pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  raise exception 'isolated dedicated owner required' using errcode='42501'; end if;
end; $$;

-- Reconstruct the CURRENT signed review corpus, excluding only this exact
-- immutable grant's own source/image/thumbnail rows and its own spine entry.
-- This is not the certificate's old stored snapshot. New foreign history,
-- reservations, generated grants, claims and baseline changes remain included.
create function public.fixer_still_v2_owner_certificate_snapshot_20261008(p_audit_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; base jsonb; rows jsonb; grants jsonb; generated jsonb; spine text;
begin
 snap:=public.fixer_still_photo_snapshot_v2_20261008();
 if not exists(select 1 from public.fixer_owner_photo_reservation_20261007 g
   join public.fixer_forward_media_photo_certificate_20261007 c on c.audit_id=g.audit_id
   where g.audit_id=p_audit_id and g.calendar_row_id=c.calendar_row_id
     and g.candidate_json=c.payload_json::jsonb->'candidate' and c.payload_json::jsonb->'schema_version'='2'::jsonb) then
  return snap; end if;
 base:=public.fixer_photo_history_snapshot_20261007();
 select coalesce(jsonb_agg(to_jsonb(g) order by g.audit_id),'[]'::jsonb) into grants
 from public.fixer_owner_photo_reservation_20261007 g where g.audit_id<>p_audit_id;
 spine:='sha256:'||encode(sha256(convert_to(jsonb_build_object(
  'history_spine',base->>'spine_digest','owner_reservations',grants)::text,'UTF8')),'hex');
 select coalesce(jsonb_agg(to_jsonb(g) order by job_id),'[]'::jsonb) into generated
 from public.fixer_generated_reservation_20261007 g;
 spine:='sha256:'||encode(sha256(convert_to(jsonb_build_object('spine',spine,'generated',generated)::text,'UTF8')),'hex');
 select coalesce(jsonb_agg(j order by j->>'history_key'),'[]'::jsonb) into rows
 from jsonb_array_elements(snap->'rows') j where j->>'history_key'<>all(array[
  'owner-reserved-source:'||p_audit_id::text,'owner-reserved-image:'||p_audit_id::text,
  'owner-reserved-thumbnail:'||p_audit_id::text]);
 return snap||jsonb_build_object('rows',rows,'rows_count',jsonb_array_length(rows),'spine_digest',spine);
end; $$;

-- Private read-only candidate validation shared by discovery/reservation/CAS.
-- No producer URL, stage marker or used_count alone grants admission readiness.
create function public.fixer_still_v2_owner_candidate_20261008(p_audit_id uuid,p_phase text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare c public.fixer_forward_media_photo_certificate_20261007%rowtype;
 r public.content_calendar%rowtype; m public.forward_schedule_stage_member_20261008%rowtype;
 src public.fixer_forward_media_source_receipt_20261007%rowtype;
 g public.fixer_owner_photo_reservation_20261007%rowtype;
 snapshot jsonb; corpus jsonb; candidate jsonb; content jsonb; recipe jsonb; rev text; ids uuid[];
begin
 if p_phase is null or p_phase not in ('prepare','admit') then
  raise exception 'exact still v2 phase required' using errcode='23514'; end if;
 if not exists(select 1 from public.forward_prospective_photo_gate_20261008 where singleton and enabled) then return null; end if;
 select cert.* into c from public.fixer_forward_media_photo_certificate_20261007 cert
 join public.fixer_forward_media_photo_key_20261007 k on k.key_id=cert.key_id
 join public.fixer_forward_media_photo_policy_20261007 policy on policy.policy_id=k.policy_id
 join public.fixer_forward_media_photo_state_20261007 state on state.singleton
 where cert.audit_id=p_audit_id and cert.payload_json::jsonb->'schema_version'='2'::jsonb
   and state.enabled and nullif(btrim(state.routes_reconciled_ref),'') is not null
   and cert.baseline_id=state.baseline_id and cert.generation=state.generation
   and k.approved and policy.approved and cert.verified_by=k.verifier_role
   and not exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id)
   and not exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=cert.audit_id);
 if not found then return null; end if;
 candidate:=c.payload_json::jsonb->'candidate';
 select * into r from public.content_calendar where id=c.calendar_row_id;
 select * into m from public.forward_schedule_stage_member_20261008 where calendar_row_id=c.calendar_row_id;
 if r.id is null or m.calendar_row_id is null or r.publish_reservation_day is not null
   or not public.fixer_forward_schedule_staged_authorized_20261008(r.id,m.tenant_id,m.batch_id)
   or r.gym_id is distinct from m.tenant_id or r.gym_id is distinct from candidate->>'tenant_id'
   or candidate->>'logical_post_id' is distinct from r.logical_post_id::text then return null; end if;
 snapshot:=public.fixer_forward_media_source_snapshot_20261007(r.id,md5(to_jsonb(r)::text));
 select * into src from public.fixer_forward_media_source_receipt_20261007 where receipt_ref=candidate->>'source_receipt_ref';
 content:=public.fixer_forward_media_photo_content_20261007(r.id);
 if not found or src.calendar_row_id is distinct from r.id or src.tenant_id is distinct from m.tenant_id
   or src.binding_revision is distinct from snapshot->>'binding_revision'
   or src.source_asset_id is distinct from r.source_media_asset_id
   or src.source_id is distinct from snapshot#>>'{source,id}' or src.folder_id is distinct from snapshot#>>'{source,folder_id}'
   or src.exact_source_url is distinct from r.source_media_url
   or src.source_sha256 is distinct from candidate->>'source_sha256'
   or src.source_fingerprint is distinct from candidate->>'source_fingerprint'
   or src.source_length is distinct from (candidate->>'source_length')::bigint
   or not public.fixer_owner_photo_source_ready_20261007(src.source_asset_id,src.source_sha256)
   or candidate->>'content_digest' is distinct from 'sha256:'||encode(sha256(convert_to(content::text,'UTF8')),'hex')
   or c.receipt_ref is distinct from 'photo-audit:sha256:'||encode(sha256(convert_to(c.payload_json||E'\n'||c.signature_hex,'UTF8')),'hex') then return null; end if;
 select ob.observation_json::jsonb->'recipe' into recipe from public.fixer_forward_media_observation_20261007 ob
 where ob.calendar_row_id=r.id and ob.row_revision=src.row_revision and ob.tenant_id=m.tenant_id
  and md5(ob.calendar_snapshot::text)=src.row_revision
  and ob.source_asset_id=r.source_media_asset_id and ob.source_exact_url=r.source_media_url
  and ob.delivered_exact_url=r.image_url
  and jsonb_typeof(ob.observation_json::jsonb->'recipe')='object'
  and candidate->>'render_recipe_digest'='sha256:'||encode(sha256(convert_to(
   public.fixer_owner_photo_canonical_20261007(ob.observation_json::jsonb->'recipe'),'UTF8')),'hex')
 order by ob.observation_digest limit 1;
 if not found then return null; end if;
 corpus:=public.fixer_still_v2_owner_certificate_snapshot_20261008(c.audit_id);
 perform public.fixer_assert_still_packet_v2_20261008(c.payload_json::jsonb,corpus);
 if c.spine_digest is distinct from corpus->>'spine_digest' then return null; end if;
 select * into g from public.fixer_owner_photo_reservation_20261007 where audit_id=c.audit_id;
 if p_phase='prepare' then
  if r.render_manifest_digest is not null or src.row_revision is distinct from snapshot->>'revision' then return null; end if;
 else
  if g.audit_id is null or g.calendar_row_id is distinct from r.id or g.candidate_json is distinct from candidate
   or g.manifest_json->>'manifest_digest' is distinct from r.render_manifest_digest
   or not exists(select 1 from public.fixer_forward_media_original_registry_20261006 o
    join public.fixer_forward_media_history_clearance_20261006 h on h.tenant_id=o.tenant_id and h.source_asset_id=o.source_asset_id
    join public.fixer_forward_media_render_manifest_20261006 mf on mf.manifest_digest=r.render_manifest_digest
    where o.tenant_id=m.tenant_id and o.source_asset_id=r.source_media_asset_id
     and to_jsonb(o)-array['registered_at','registry_evidence_ref']=g.original_json-'registry_evidence_ref' and h.decision='cleared_unused'
     and to_jsonb(h)-array['checked_at','decision','history_evidence_ref','registry_evidence_ref']=g.original_json-'registry_evidence_ref'
     and exists(select 1 from public.fixer_owner_photo_reservation_20261007 anchor
       where anchor.receipt_ref=h.history_evidence_ref
        and to_jsonb(o)-'registered_at'=anchor.original_json
        and anchor.candidate_json->>'tenant_id'=g.candidate_json->>'tenant_id'
        and anchor.candidate_json->>'post_date'=g.candidate_json->>'post_date'
        and anchor.candidate_json->>'group_key'=g.candidate_json->>'group_key'
        and not exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=anchor.audit_id))
     and to_jsonb(mf)-'registered_at'=g.manifest_json) then return null; end if;
  perform public.fixer_assert_still_row_v2_or_v1_20261008(r.id);
  rev:=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision';
  -- Every accepted role binds exact signed SHA/MD5/length, URLs, revision,
  -- one immutable lineage and its exact trusted object-read receipt.
  select q.attestation_ids into ids from (
   select a.lineage_receipt_id,array_agg(a.attestation_id order by a.role) attestation_ids,max(a.created_at) newest
   from public.forward_media_visual_attestation a
   join public.fixer_forward_media_lineage_20261006 l on l.evidence_id=a.lineage_receipt_id
   join public.fixer_forward_media_object_read_20261006 o on o.receipt_id=a.object_read_receipt_id
   where a.tenant_key=m.tenant_id and a.row_revision=('x'||substr(rev,1,15))::bit(60)::bigint
    and l.calendar_row_id=r.id and l.row_revision=rev and l.tenant_id=m.tenant_id
    and l.group_key=r.visual_group_key and l.source_asset_id=r.source_media_asset_id and l.manifest_digest=r.render_manifest_digest
    and a.media_url=case a.role when 'original' then r.source_media_url when 'delivered' then r.image_url else coalesce(r.thumbnail_url,r.image_url) end
    and 'sha256:'||a.source_sha256=candidate->>(case a.role when 'original' then 'source_sha256' when 'delivered' then 'image_sha256' else case when r.thumbnail_url is null then 'image_sha256' else 'thumbnail_sha256' end end)
    and 'md5:'||a.source_md5=candidate->>(case a.role when 'original' then 'source_fingerprint' when 'delivered' then 'image_fingerprint' else case when r.thumbnail_url is null then 'image_fingerprint' else 'thumbnail_fingerprint' end end)
    and a.byte_length=(candidate->>(case a.role when 'original' then 'source_length' when 'delivered' then 'image_length' else case when r.thumbnail_url is null then 'image_length' else 'thumbnail_length' end end))::bigint
    and o.tenant_id=m.tenant_id and o.exact_url=a.media_url and o.fingerprint='md5:'||a.source_md5 and o.byte_length=a.byte_length
    and o.receipt_id=case a.role when 'original' then l.source_read_receipt when 'delivered' then l.image_read_receipt else coalesce(l.thumbnail_read_receipt,l.image_read_receipt) end
   group by a.lineage_receipt_id having count(*)=3 and count(distinct a.role)=3
   order by newest desc,a.lineage_receipt_id limit 1) q;
  if ids is null then return null; end if;
 end if;
 return jsonb_build_object('audit_id',c.audit_id,'phase',p_phase,'calendar_row_id',r.id,
  'tenant_id',m.tenant_id,'batch_id',m.batch_id,'logical_post_id',r.logical_post_id,
  'revision',snapshot->>'revision','binding_revision',snapshot->>'binding_revision',
  'recipe',recipe,
  'expected_revision',rev,'attestation_ids',ids);
exception when check_violation or object_not_in_prerequisite_state then return null;
end; $$;

create function public.fixer_still_v2_owner_pending_20261008(p_tenants text[],p_limit integer)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform public.fixer_assert_still_v2_owner_20261008();
 if p_tenants is null or cardinality(p_tenants) not between 1 and 32 or array_position(p_tenants,null) is not null
  or exists(select 1 from unnest(p_tenants) t where t<>btrim(t) or t='')
  or p_limit is null or p_limit not between 1 and 100 then
  raise exception 'bounded owner tenant scope required' using errcode='23514'; end if;
 return coalesce((select jsonb_agg(item order by tenant_rank,audit_id,phase) from (
  select item,c.audit_id,ph.phase,row_number() over(partition by item->>'tenant_id' order by c.audit_id,ph.phase) tenant_rank
  from public.fixer_forward_media_photo_certificate_20261007 c
  cross join (values('prepare'),('admit')) ph(phase)
  cross join lateral (select public.fixer_still_v2_owner_candidate_20261008(c.audit_id,ph.phase) item) q
  where c.payload_json::jsonb#>>'{candidate,tenant_id}'=any(p_tenants)
   and item is not null and item->>'tenant_id'=any(p_tenants)
   and not exists(select 1 from public.fixer_still_v2_owner_progress_20261008 p where p.audit_id=c.audit_id and p.phase=ph.phase)
   and (ph.phase<>'prepare' or not exists(select 1 from public.fixer_owner_photo_reservation_20261007 g where g.audit_id=c.audit_id))
   and (ph.phase<>'admit' or not exists(select 1 from public.forward_prospective_photo_binding_20261008 b
     where b.calendar_row_id=c.calendar_row_id and b.row_revision=item->>'expected_revision'))
  order by tenant_rank,c.audit_id,ph.phase limit p_limit) candidates),'[]'::jsonb);
end; $$;

create function public.fixer_still_v2_owner_reserve_20261008(p_audit_id uuid,p_phase text,p_token uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare candidate jsonb;
begin
 perform public.fixer_assert_still_v2_owner_20261008();
 if p_token is null then raise exception 'exact owner attempt token required' using errcode='23514'; end if;
 -- Serialize authority and capture current immutable quarantine identity.
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 if exists(select 1 from public.fixer_still_v2_owner_progress_20261008 p where p.audit_id=p_audit_id and p.phase=p_phase) then return false; end if;
 candidate:=public.fixer_still_v2_owner_candidate_20261008(p_audit_id,p_phase);
 if candidate is null then raise exception 'current exact still v2 candidate required' using errcode='23514'; end if;
 if p_phase='prepare' and exists(select 1 from public.fixer_owner_photo_reservation_20261007 grant_exists where grant_exists.audit_id=p_audit_id) then return false; end if;
 if p_phase='admit' and exists(select 1 from public.forward_prospective_photo_binding_20261008 b
  where b.calendar_row_id=(candidate->>'calendar_row_id')::uuid and b.row_revision=candidate->>'expected_revision') then return false; end if;
 insert into public.fixer_still_v2_owner_progress_20261008(audit_id,phase,attempt_token,candidate_json)
 values(p_audit_id,p_phase,p_token,candidate) on conflict do nothing;
 return found;
end; $$;

create function public.fixer_still_v2_owner_snapshot_20261008(p_audit_id uuid,p_phase text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare p public.fixer_still_v2_owner_progress_20261008%rowtype; candidate jsonb; snap jsonb; src jsonb;
begin
 perform public.fixer_assert_still_v2_owner_20261008();
 select * into p from public.fixer_still_v2_owner_progress_20261008
  where audit_id=p_audit_id and phase=p_phase and attempt_token=p_token and state='quarantine';
 if not found then raise exception 'exact quarantine attempt required' using errcode='23514'; end if;
 candidate:=public.fixer_still_v2_owner_candidate_20261008(p_audit_id,p_phase);
 if candidate is null or candidate is distinct from p.candidate_json then
  return jsonb_build_object('hold_reason','still_v2_candidate_binding_changed'); end if;
 snap:=public.fixer_forward_media_source_snapshot_20261007((candidate->>'calendar_row_id')::uuid,candidate->>'revision');
 select to_jsonb(s) into src from public.fixer_forward_media_source_receipt_20261007 s
 join public.fixer_forward_media_photo_certificate_20261007 c on s.receipt_ref=c.payload_json::jsonb#>>'{candidate,source_receipt_ref}' where c.audit_id=p_audit_id;
 return snap||candidate||jsonb_build_object('source_receipt',src,'source_receipt_revision',src->>'row_revision',
  'certificate_snapshot',public.fixer_still_v2_owner_certificate_snapshot_20261008(p_audit_id));
end; $$;

create function public.fixer_still_v2_owner_locked_20261008(p_audit_id uuid,p_phase text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare p public.fixer_still_v2_owner_progress_20261008%rowtype;
begin
 perform public.fixer_assert_still_v2_owner_20261008();
 -- Global order: exclusive graph -> census -> batch -> calendar -> asset/source.
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into p from public.fixer_still_v2_owner_progress_20261008
  where audit_id=p_audit_id and phase=p_phase and attempt_token=p_token and state='quarantine' for update;
 if not found then raise exception 'exact quarantine attempt required' using errcode='23514'; end if;
 perform 1 from public.forward_schedule_stage_batch_20261008 where batch_id=(p.candidate_json->>'batch_id')::uuid for update;
 perform 1 from public.content_calendar where id=(p.candidate_json->>'calendar_row_id')::uuid for update;
 perform 1 from public.media_asset a join public.content_calendar r on r.source_media_asset_id=a.id
  where r.id=(p.candidate_json->>'calendar_row_id')::uuid for update of a;
 perform 1 from public.media_source s join public.media_asset a on a.source_id=s.id
  join public.content_calendar r on r.source_media_asset_id=a.id where r.id=(p.candidate_json->>'calendar_row_id')::uuid for share of s;
 return public.fixer_still_v2_owner_snapshot_20261008(p_audit_id,p_phase,p_token);
end; $$;

create function public.fixer_still_v2_owner_finish_20261008(p_audit_id uuid,p_phase text,p_token uuid,p_outcome jsonb)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare p public.fixer_still_v2_owner_progress_20261008%rowtype; snap jsonb;
 g public.fixer_owner_photo_reservation_20261007%rowtype; proof jsonb; expected jsonb;
begin
 perform public.fixer_assert_still_v2_owner_20261008();
 if p_outcome is null or jsonb_typeof(p_outcome)<>'object' or octet_length(p_outcome::text)>4096
  or coalesce(p_outcome->>'status','') not in ('persisted','hold') then
  raise exception 'bounded still v2 outcome required' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into p from public.fixer_still_v2_owner_progress_20261008
  where audit_id=p_audit_id and phase=p_phase and attempt_token=p_token for update;
 if not found then raise exception 'exact owner attempt required' using errcode='23514'; end if;
 if p.state='final' then
  if p.outcome is distinct from p_outcome then raise exception 'immutable outcome conflict' using errcode='23514'; end if;
  return true; -- Exact durable replay, no authority write/reexecution.
 end if;
 if p_outcome->>'status'='hold' then
  if p_outcome is distinct from jsonb_build_object('status','hold','reason',p_outcome->>'reason')
   or nullif(btrim(p_outcome->>'reason'),'') is null
   or (p_phase='prepare' and exists(select 1 from public.fixer_owner_photo_reservation_20261007 grant_exists where grant_exists.audit_id=p_audit_id))
   or (p_phase='admit' and exists(select 1 from public.forward_prospective_photo_binding_20261008 b
    where b.calendar_row_id=(p.candidate_json->>'calendar_row_id')::uuid and b.row_revision=p.candidate_json->>'expected_revision')) then
   raise exception 'hold cannot conceal persisted phase authority' using errcode='23514'; end if;
 else
  snap:=public.fixer_still_v2_owner_locked_20261008(p_audit_id,p_phase,p_token);
  if snap ? 'hold_reason' then raise exception 'exact owner phase CAS failed' using errcode='23514'; end if;
  if p_phase='prepare' then
   select * into g from public.fixer_owner_photo_reservation_20261007 where audit_id=p_audit_id;
   if not found or g.calendar_row_id::text is distinct from p.candidate_json->>'calendar_row_id'
    or not exists(select 1 from public.fixer_forward_media_render_manifest_20261006 mf
      join public.fixer_forward_media_original_registry_20261006 original
       on original.tenant_id=mf.tenant_id and original.source_asset_id=mf.source_asset_id
      join public.fixer_forward_media_history_clearance_20261006 clearance
       on clearance.tenant_id=original.tenant_id and clearance.source_asset_id=original.source_asset_id
      join public.fixer_owner_photo_reservation_20261007 anchor on anchor.receipt_ref=clearance.history_evidence_ref
      where mf.manifest_digest=g.manifest_json->>'manifest_digest' and to_jsonb(mf)-'registered_at'=g.manifest_json
       and to_jsonb(original)-'registered_at'=anchor.original_json
       and clearance.decision='cleared_unused'
       and to_jsonb(clearance)-array['checked_at','decision','history_evidence_ref']=anchor.original_json
       and anchor.original_json-'registry_evidence_ref'=g.original_json-'registry_evidence_ref'
       and anchor.candidate_json->>'tenant_id'=g.candidate_json->>'tenant_id'
       and anchor.candidate_json->>'post_date'=g.candidate_json->>'post_date'
       and anchor.candidate_json->>'group_key'=g.candidate_json->>'group_key'
       and not exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=anchor.audit_id)) then
    raise exception 'complete persisted v2 grant required' using errcode='23514'; end if;
   expected:=jsonb_build_object('status','persisted','decision','cleared_unused','manifest_digest',g.manifest_json->>'manifest_digest');
  else
   proof:=public.forward_prospective_photo_proof_20261008((p.candidate_json->>'calendar_row_id')::uuid,
    replace((select c.payload_json::jsonb#>>'{candidate,source_sha256}' from public.fixer_forward_media_photo_certificate_20261007 c where c.audit_id=p_audit_id),'sha256:',''));
   if proof is null or not exists(select 1 from public.forward_prospective_photo_binding_20261008 b
     where b.calendar_row_id=(p.candidate_json->>'calendar_row_id')::uuid
      and b.row_revision=p.candidate_json->>'expected_revision'
      and b.occupancy_id::text=proof->>'occupancy_id'
      and b.attestation_ids=array(select jsonb_array_elements_text(p.candidate_json->'attestation_ids'))::uuid[]) then
    raise exception 'exact immutable v2 admission receipt required' using errcode='23514'; end if;
   expected:=jsonb_build_object('status','persisted','occupancy_id',proof->>'occupancy_id');
  end if;
  if p_outcome is distinct from expected then raise exception 'exact persisted phase outcome required' using errcode='23514'; end if;
 end if;
 update public.fixer_still_v2_owner_progress_20261008 set state='final',outcome=p_outcome,finished_at=clock_timestamp()
 where audit_id=p_audit_id and phase=p_phase;
 return true;
end; $$;

create function public.fixer_still_v2_owner_status_20261008(p_audit_id uuid,p_phase text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare p public.fixer_still_v2_owner_progress_20261008%rowtype;
begin
 perform public.fixer_assert_still_v2_owner_20261008();
 select * into p from public.fixer_still_v2_owner_progress_20261008 where audit_id=p_audit_id and phase=p_phase and attempt_token=p_token;
 if not found then return jsonb_build_object('state','absent','audit_id',p_audit_id,'phase',p_phase); end if;
 return jsonb_build_object('audit_id',p.audit_id,'phase',p.phase,'attempt_token',p.attempt_token,
  'state',p.state,'candidate',p.candidate_json,'outcome',p.outcome);
end; $$;

revoke all on function public.fixer_still_v2_owner_progress_guard_20261008(),
 public.fixer_assert_still_v2_owner_20261008(),
 public.fixer_still_v2_owner_certificate_snapshot_20261008(uuid),
 public.fixer_still_v2_owner_candidate_20261008(uuid,text),
 public.fixer_still_v2_owner_pending_20261008(text[],integer),
 public.fixer_still_v2_owner_reserve_20261008(uuid,text,uuid),
 public.fixer_still_v2_owner_snapshot_20261008(uuid,text,uuid),
 public.fixer_still_v2_owner_locked_20261008(uuid,text,uuid),
 public.fixer_still_v2_owner_finish_20261008(uuid,text,uuid,jsonb),
 public.fixer_still_v2_owner_status_20261008(uuid,text,uuid)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,
 fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;
grant execute on function public.fixer_still_v2_owner_pending_20261008(text[],integer),
 public.fixer_still_v2_owner_reserve_20261008(uuid,text,uuid),
 public.fixer_still_v2_owner_snapshot_20261008(uuid,text,uuid),
 public.fixer_still_v2_owner_locked_20261008(uuid,text,uuid),
 public.fixer_still_v2_owner_finish_20261008(uuid,text,uuid,jsonb),
 public.fixer_still_v2_owner_status_20261008(uuid,text,uuid)
 to fixer_forward_media_owner_20261006;
commit;
