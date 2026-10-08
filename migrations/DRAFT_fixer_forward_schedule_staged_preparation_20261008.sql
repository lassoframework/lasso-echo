-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Requires DRAFT_fixer_forward_schedule_worker_discovery_20261008.sql (staged
-- discovery + fixer_forward_schedule_staged_authorized_20261008), the stage
-- draft predicate forward_schedule_preparation_eligible_20261008, the owner
-- transport draft, the source/history draft and the owner photo clearance
-- draft beneath them.
--
-- This draft adds the STAGED-SPECIFIC preparation authority the discovery
-- draft deliberately did not create. The active-lane RPCs
-- (fixer_forward_media_owner_snapshot/locked_20261007,
-- fixer_prepare_owner_photo_20261007, fixer_bind_forward_media_manifest_20261006)
-- remain ACTIVE-ONLY and are unchanged. Every function here:
--   * requires the exact authorizing predicate result
--     {eligible:true, mode:'staged', tenant_id, batch_id, reason:null}
--     re-read INSIDE the same locked transaction,
--   * binds registered NONTERMINAL batch membership and the immutable
--     content/media binding of the member record,
--   * resolves the canonical tenant through
--     fixer_forward_media_tenant_alias_20261006 while still checking RAW
--     source/asset ownership (media_asset.gym_id = content_calendar.gym_id and
--     the same-gym active Drive media_source), and
--   * never activates, approves, claims or sends anything.
--
-- Signed-photo clearance stays the ONLY positive authority: the staged photo
-- grant reuses the complete certified-certificate + frozen-corpus + sibling
-- scope checks of the active grant and preserves every hold semantic.
-- Uncertain or used history keeps its durable hold receipts; nothing here
-- turns uncertainty into clearance.
--
-- Lock order is unchanged: shared graph -> exclusive census -> batch row ->
-- sorted calendar rows -> asset/source rows. Owner authority inserts acquire
-- the graph EXCLUSIVE lock before row locks, matching the existing stack.
-- Rollback before use: remove the new functions. After use preserve all rows.
begin;

do $$
begin
  if to_regprocedure('public.fixer_forward_schedule_staged_authorized_20261008(uuid,text,uuid)') is null
      or to_regprocedure('public.forward_schedule_preparation_eligible_20261008(uuid)') is null then
    raise exception 'forward schedule staged discovery draft is required' using errcode='23514';
  end if;
  if to_regprocedure('public.fixer_forward_media_owner_locked_20261007(uuid,text,text,uuid)') is null
      or to_regprocedure('public.fixer_forward_media_source_snapshot_20261007(uuid,text)') is null
      or to_regprocedure('public.fixer_prepare_owner_photo_20261007(uuid,jsonb,jsonb)') is null
      or to_regprocedure('public.fixer_bind_forward_media_manifest_20261006(uuid)') is null then
    raise exception 'forward media owner/photo/binder drafts are required' using errcode='23514';
  end if;
end;
$$;

-- Canonical tenant of a calendar row: alias mapping wins, else the raw gym.
-- Discovery and the owner-locked transport MUST agree on this resolution while
-- raw source/asset ownership is still checked against the raw gym key.
create function public.fixer_forward_schedule_canonical_tenant_20261008(p_gym_id text)
returns text language plpgsql stable security definer set search_path=pg_catalog,public as $$
begin
  return coalesce((select a.tenant_id from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(coalesce(p_gym_id,''))),nullif(btrim(coalesce(p_gym_id,'')),''));
end;
$$;
revoke all on function public.fixer_forward_schedule_canonical_tenant_20261008(text)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,
  fixer_forward_media_owner_20261006;

-- STAGED owner pre-read snapshot. Mirrors fixer_forward_media_owner_snapshot
-- _20261007 (quarantine-gated, lock-free, ends before remote I/O) but resolves
-- the canonical tenant: the immutable observation binds the canonical tenant
-- while the raw calendar/asset/source binding still binds the raw gym key.
-- The authorizing predicate is re-read here so a forged marker alone can never
-- reach remote byte verification.
create function public.fixer_forward_schedule_staged_owner_snapshot_20261008(
 p_id uuid,p_revision text,p_digest text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snapshot jsonb; o public.fixer_forward_media_observation_20261007%rowtype;
 m public.forward_schedule_stage_member_20261008%rowtype; tenant text;
begin
 if not exists(select 1 from public.fixer_forward_media_owner_progress_20261007 p
   where p.calendar_row_id=p_id and p.row_revision=p_revision and p.observation_digest=p_digest
    and p.reservation_token=p_token and p.state='quarantine') then
   raise exception 'exact quarantine reservation required' using errcode='23514'; end if;
 begin
   snapshot:=public.fixer_forward_media_source_snapshot_20261007(p_id,p_revision);
 exception when check_violation then
   return jsonb_build_object('hold_reason','owner_source_snapshot_invalid');
 end;
 tenant:=public.fixer_forward_schedule_canonical_tenant_20261008(snapshot#>>'{calendar,gym_id}');
 select * into o from public.fixer_forward_media_observation_20261007
   where calendar_row_id=p_id and row_revision=p_revision and observation_digest=p_digest;
 if not found or tenant is null or o.calendar_snapshot is distinct from snapshot->'calendar'
   or o.tenant_id is distinct from tenant then
   return jsonb_build_object('hold_reason','candidate_canonical_binding_invalid'); end if;
 select * into m from public.forward_schedule_stage_member_20261008 where calendar_row_id=p_id;
 if not found or m.tenant_id is distinct from tenant
     or not public.fixer_forward_schedule_staged_authorized_20261008(p_id,tenant,m.batch_id) then
   return jsonb_build_object('hold_reason','staged_preparation_not_eligible'); end if;
 return snapshot||jsonb_build_object('observation',o.observation_json::jsonb,
   'tenant_id',tenant,'batch_id',m.batch_id);
end;
$$;
revoke all on function public.fixer_forward_schedule_staged_owner_snapshot_20261008(uuid,text,text,uuid)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_forward_schedule_staged_owner_snapshot_20261008(uuid,text,text,uuid)
  to fixer_forward_media_owner_20261006;

-- STAGED owner final locked recheck. Mirrors fixer_forward_media_owner_locked
-- _20261007 (graph EXCLUSIVE first, progress quarantine row lock, calendar row
-- lock, observation match, asset row lock, source share lock) but authorizes
-- by the staged predicate and resolves the canonical tenant for the
-- observation binding. RAW ownership is unchanged: media_asset.gym_id must
-- equal the row's raw gym key and the same-gym active Drive source is locked.
create function public.fixer_forward_schedule_staged_owner_locked_20261008(
 p_id uuid,p_revision text,p_digest text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype;
 o public.fixer_forward_media_observation_20261007%rowtype;
 a public.media_asset%rowtype; revision text; source_snapshot jsonb;
 m public.forward_schedule_stage_member_20261008%rowtype; tenant text;
begin
 if current_setting('transaction_isolation')<>'read committed' then
   raise exception 'forward media authority requires read committed isolation' using errcode='25000';
 end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 perform 1 from public.fixer_forward_media_owner_progress_20261007 p
   where p.calendar_row_id=p_id and p.row_revision=p_revision
     and p.observation_digest=p_digest and p.reservation_token=p_token and p.state='quarantine'
   for update;
 if not found then raise exception 'manual reconciliation required' using errcode='23514'; end if;
 select * into r from public.content_calendar where id=p_id for update;
 if not found then return jsonb_build_object('hold_reason','canonical_revision_changed'); end if;
 revision:=md5(to_jsonb(r)::text);
 if revision is distinct from p_revision then
   return jsonb_build_object('hold_reason','canonical_revision_changed');
 end if;
 select * into o from public.fixer_forward_media_observation_20261007
   where calendar_row_id=p_id and row_revision=p_revision and observation_digest=p_digest for share;
 tenant:=public.fixer_forward_schedule_canonical_tenant_20261008(r.gym_id);
 if not found or tenant is null or o.calendar_snapshot is distinct from to_jsonb(r)
   or o.tenant_id is distinct from tenant then
   return jsonb_build_object('hold_reason','candidate_canonical_binding_invalid');
 end if;
 select * into a from public.media_asset where id=r.source_media_asset_id for update;
 if not found or a.gym_id is distinct from r.gym_id then
   return jsonb_build_object('hold_reason','canonical_tenant_asset_mismatch');
 end if;
 if to_regclass('public.media_source') is not null then
   select to_jsonb(s) into source_snapshot from public.media_source s
     where s.id=to_jsonb(a)->>'source_id' for share;
 end if;
 select * into m from public.forward_schedule_stage_member_20261008 where calendar_row_id=r.id;
 if not found or m.tenant_id is distinct from tenant
     or not public.fixer_forward_schedule_staged_authorized_20261008(r.id,tenant,m.batch_id) then
   return jsonb_build_object('hold_reason','staged_preparation_not_eligible');
 end if;
 return jsonb_build_object('calendar',to_jsonb(r),'asset',to_jsonb(a),
   'source',source_snapshot,'binding_revision',case when source_snapshot is not null
      then md5(jsonb_build_array(to_jsonb(a),source_snapshot)::text) end,
   'observation',o.observation_json::jsonb,'revision',revision,
   'tenant_id',tenant,'batch_id',m.batch_id);
end;
$$;
revoke all on function public.fixer_forward_schedule_staged_owner_locked_20261008(uuid,text,text,uuid)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_forward_schedule_staged_owner_locked_20261008(uuid,text,text,uuid)
  to fixer_forward_media_owner_20261006;

-- STAGED signed-photo grant. Mirrors fixer_prepare_owner_photo_20261007 check
-- for check (isolated owner role, graph lock, photo policy state, approved
-- unrevoked signed certificate, exact durable source receipt, exact certified
-- original/rendition tuples, sibling anchor scope, frozen complete corpus,
-- used/held visual fences) but the candidate-state precondition is the STAGED
-- one: candidate row with the exact stage marker in a registered nonterminal
-- batch whose canonical tenant matches, re-authorized by the predicate inside
-- this same locked transaction. The active grant is unchanged and remains the
-- only authority for active rows; this grant never admits an active row.
create function public.fixer_prepare_owner_staged_photo_20261008(p_audit_id uuid,p_original jsonb,p_manifest jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare cert public.fixer_forward_media_photo_certificate_20261007%rowtype;
 src public.fixer_forward_media_source_receipt_20261007%rowtype;
 state public.fixer_forward_media_photo_state_20261007%rowtype;
 old public.fixer_owner_photo_reservation_20261007%rowtype;
 snap jsonb; candidate jsonb; content jsonb; ref text; caller text; clearance jsonb;
 anchor public.fixer_owner_photo_reservation_20261007%rowtype;
 sibling_ids uuid[]; sibling_history_keys text[];
 m public.forward_schedule_stage_member_20261008%rowtype;
 b public.forward_schedule_stage_batch_20261008%rowtype;
 staged_tenant text;
begin
 caller:=coalesce(nullif(current_setting('role',true),'none'),session_user);
 if not pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
   or pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_photo_auditor_20261007','member') then
  raise exception 'isolated dedicated owner required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'owner photo authority requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 select * into state from public.fixer_forward_media_photo_state_20261007 where singleton for share;
 if not state.enabled or nullif(btrim(state.routes_reconciled_ref),'') is null then
  raise exception 'owner photo policy OFF or unreconciled' using errcode='55000'; end if;
 select c.* into cert from public.fixer_forward_media_photo_certificate_20261007 c
 join public.fixer_forward_media_photo_key_20261007 k on k.key_id=c.key_id
 join public.fixer_forward_media_photo_policy_20261007 p on p.policy_id=k.policy_id
 where c.audit_id=p_audit_id and k.approved and p.approved and c.verified_by=k.verifier_role
   and not exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id);
 if not found then raise exception 'approved signed photo certificate required' using errcode='23514'; end if;
 candidate:=cert.payload_json::jsonb->'candidate';
 perform 1 from public.content_calendar where id=cert.calendar_row_id for update;
 perform 1 from public.media_asset a join public.content_calendar r on r.source_media_asset_id=a.id
  where r.id=cert.calendar_row_id for update of a;
 perform 1 from public.media_source s join public.media_asset a on a.source_id=s.id
  join public.content_calendar r on r.source_media_asset_id=a.id where r.id=cert.calendar_row_id for share of s;
 content:=public.fixer_forward_media_photo_content_20261007(cert.calendar_row_id);
 select * into src from public.fixer_forward_media_source_receipt_20261007
  where receipt_ref=candidate->>'source_receipt_ref';
 if not found or src.calendar_row_id<>cert.calendar_row_id
   or src.tenant_id is distinct from content->>'tenant_id' or src.source_asset_id is distinct from content->>'source_asset_id'
   or src.exact_source_url is distinct from content->>'source_url'
   or src.source_sha256 is distinct from candidate->>'source_sha256'
   or src.source_fingerprint is distinct from candidate->>'source_fingerprint'
   or src.source_length is distinct from (candidate->>'source_length')::bigint
   or not public.fixer_owner_photo_source_ready_20261007(src.source_asset_id,src.source_sha256)
   or candidate->>'content_digest' is distinct from 'sha256:'||encode(sha256(convert_to(content::text,'UTF8')),'hex')
   or cert.receipt_ref is distinct from 'photo-audit:sha256:'||encode(sha256(convert_to(cert.payload_json||E'\n'||cert.signature_hex,'UTF8')),'hex')
   or not exists(select 1 from public.media_asset a join public.media_source s on s.id=a.source_id
      where a.id=src.source_asset_id and a.gym_id=src.tenant_id and s.gym_id=a.gym_id
        and s.id=src.source_id and s.folder_id=src.folder_id and s.active and s.kind='gym_drive') then
  raise exception 'current exact same gym source and candidate required' using errcode='23514'; end if;
 if p_original is distinct from jsonb_build_object('tenant_id',src.tenant_id,'source_asset_id',src.source_asset_id,
    'source_url',src.exact_source_url,'source_fingerprint',src.source_fingerprint,'source_length',src.source_length,
    'registry_evidence_ref',src.receipt_ref)
   or p_manifest->>'tenant_id' is distinct from src.tenant_id
   or p_manifest->>'source_asset_id' is distinct from src.source_asset_id
   or p_manifest->>'image_url' is distinct from candidate->>'image_url'
   or p_manifest->>'image_fingerprint' is distinct from candidate->>'image_fingerprint'
   or p_manifest->'image_length' is distinct from candidate->'image_length'
   or p_manifest->'thumbnail_url' is distinct from coalesce(candidate->'thumbnail_url','null'::jsonb)
   or p_manifest->'thumbnail_fingerprint' is distinct from coalesce(candidate->'thumbnail_fingerprint','null'::jsonb)
   or p_manifest->'thumbnail_length' is distinct from coalesce(candidate->'thumbnail_length','null'::jsonb)
   or p_manifest->>'render_evidence_ref' is distinct from cert.receipt_ref
   or candidate->>'render_recipe_digest' is distinct from 'sha256:'||encode(sha256(convert_to(public.fixer_owner_photo_canonical_20261007(p_manifest->'render_recipe'),'UTF8')),'hex') then
  raise exception 'exact certified original and rendition tuples required' using errcode='23514'; end if;
 ref:='owner-photo-reservation:'||cert.receipt_ref;
 clearance:=p_original || jsonb_build_object('decision','cleared_unused','history_evidence_ref',ref);
 select coalesce(array_agg(r.audit_id),'{}'::uuid[]) into sibling_ids
 from public.fixer_owner_photo_reservation_20261007 r
 join public.fixer_forward_media_photo_certificate_20261007 c on c.audit_id=r.audit_id
 join public.fixer_forward_media_photo_key_20261007 k on k.key_id=c.key_id
 join public.fixer_forward_media_photo_policy_20261007 policy on policy.policy_id=k.policy_id
 where r.candidate_json->>'tenant_id'=candidate->>'tenant_id'
  and r.candidate_json->>'post_date'=candidate->>'post_date'
  and r.candidate_json->>'group_key'=candidate->>'group_key'
  and r.candidate_json->>'source_asset_id'=candidate->>'source_asset_id'
  and r.candidate_json->>'source_url'=candidate->>'source_url'
  and r.candidate_json->>'source_sha256'=candidate->>'source_sha256'
  and r.candidate_json->>'source_fingerprint'=candidate->>'source_fingerprint'
  and r.candidate_json->'source_length'=candidate->'source_length'
  and c.baseline_id=state.baseline_id and c.generation=state.generation
  and k.approved and policy.approved and c.verified_by=k.verifier_role
  and not exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=r.audit_id)
  and not exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id);
 select r.* into anchor from public.fixer_forward_media_history_clearance_20261006 h
 join public.fixer_owner_photo_reservation_20261007 r on r.receipt_ref=h.history_evidence_ref
 join public.fixer_forward_media_original_registry_20261006 o
  on o.tenant_id=h.tenant_id and o.source_asset_id=h.source_asset_id
 where h.tenant_id=src.tenant_id and h.source_asset_id=src.source_asset_id
  and h.decision='cleared_unused' and to_jsonb(o)-'registered_at'=r.original_json
  and to_jsonb(h)-array['checked_at','decision','history_evidence_ref']=r.original_json
  and r.original_json-'registry_evidence_ref'=p_original-'registry_evidence_ref'
  and r.audit_id=any(sibling_ids);
 if exists(select 1 from public.fixer_forward_media_original_registry_20261006 o
     where o.tenant_id=src.tenant_id and o.source_asset_id=src.source_asset_id)
   and anchor.audit_id is null then
  raise exception 'already used cleared or reserved original outside signed sibling scope requires HOLD' using errcode='23514'; end if;
 if anchor.audit_id is not null then
  clearance:=anchor.original_json || jsonb_build_object('decision','cleared_unused','history_evidence_ref',anchor.receipt_ref);
 end if;
 select * into old from public.fixer_owner_photo_reservation_20261007 where audit_id=p_audit_id;
 if found then
  if cert.baseline_id is distinct from state.baseline_id or cert.generation is distinct from state.generation
   or exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=p_audit_id) then
   raise exception 'retired photo history epoch requires HOLD' using errcode='23514'; end if;
  if old.original_json is distinct from p_original or old.manifest_json is distinct from p_manifest then
   raise exception 'immutable owner photo identity conflict' using errcode='23514'; end if;
  snap:=public.fixer_forward_media_photo_snapshot_20261007();
  if snap->'policy_approved' is distinct from 'true'::jsonb or snap->'scope_complete' is distinct from 'true'::jsonb
    or exists(select 1 from jsonb_array_elements(snap->'rows') h
      where h->'resolved' is distinct from 'true'::jsonb or h->>'media_kind' is distinct from 'still_photo')
    or exists(select 1 from public.fixer_forward_media_historical_original_20261007 h
      where h.source_fingerprint=any(array[candidate->>'source_fingerprint',candidate->>'image_fingerprint',candidate->>'thumbnail_fingerprint'])) then
   raise exception 'existing photo grant requires complete known current history' using errcode='23514'; end if;
  return clearance; -- owner idempotency, no new eligibility grant
 end if;
 -- STAGED candidate precondition: exact stage marker, registered NONTERMINAL
 -- membership, canonical tenant agreement and the authorizing predicate, all
 -- re-read under the graph/row locks taken above. An active row is refused.
 select * into m from public.forward_schedule_stage_member_20261008 where calendar_row_id=cert.calendar_row_id;
 select * into b from public.forward_schedule_stage_batch_20261008 where batch_id=m.batch_id;
 staged_tenant:=public.fixer_forward_schedule_canonical_tenant_20261008(content->>'tenant_id');
 if not exists(select 1 from public.content_calendar row where row.id=cert.calendar_row_id
   and row.status in ('pending','draft') and row.variant_status='candidate'
   and row.media_not_ready_reason='forward_reservation_staged'
   and row.publish_claim_token is null and row.published_at is null and row.late_post_id is null
   and row.render_manifest_digest is null and row.thumbnail_url is not distinct from candidate->>'thumbnail_url')
   or m.calendar_row_id is null or b.state is distinct from 'staged'
   or staged_tenant is null or staged_tenant is distinct from m.tenant_id
   or m.tenant_id is distinct from b.tenant_id
   or m.logical_post_id is distinct from (select r.logical_post_id from public.content_calendar r where r.id=cert.calendar_row_id)
   or m.post_date is distinct from (content->>'post_date')::date
   or m.gym_id is distinct from content->>'tenant_id'
   or m.source_media_url is distinct from content->>'source_url'
   or m.image_url is distinct from content->>'image_url'
   or m.thumbnail_url is distinct from content->>'thumbnail_url'
   or not public.fixer_forward_schedule_staged_authorized_20261008(cert.calendar_row_id,m.tenant_id,m.batch_id)
   or not exists(select 1 from public.media_asset a join public.media_source s on s.id=a.source_id
    where a.id=src.source_asset_id and src.binding_revision=md5(jsonb_build_array(to_jsonb(a),to_jsonb(s))::text)) then
  raise exception 'new staged owner photo clearance requires an authorized staged candidate' using errcode='23514'; end if;
 snap:=public.fixer_forward_media_photo_snapshot_20261007();
 if snap->'policy_approved' is distinct from 'true'::jsonb or snap->'scope_complete' is distinct from 'true'::jsonb
  or cert.baseline_id is distinct from state.baseline_id or cert.generation is distinct from state.generation
  or cert.spine_digest is distinct from snap->>'spine_digest'
  or exists(select 1 from jsonb_array_elements(snap->'rows') h
    where h->'resolved' is distinct from 'true'::jsonb or h->>'media_kind' is distinct from 'still_photo') then
  raise exception 'complete current independently reviewed history and reservations required' using errcode='23514'; end if;
 select coalesce(array_agg(history_key),'{}'::text[]) into sibling_history_keys from (
  select 'owner-reserved-source:'||id::text history_key from unnest(sibling_ids) id
  union all select 'owner-reserved-image:'||id::text from unnest(sibling_ids) id
  union all select 'owner-reserved-thumbnail:'||id::text from unnest(sibling_ids) id
  union all select 'claim:'||claim.claim_token::text
   from public.fixer_forward_media_claim_receipt_20261006 claim
   join public.fixer_owner_photo_reservation_20261007 r on r.audit_id=any(sibling_ids)
   where claim.tenant_id=r.candidate_json->>'tenant_id'
    and claim.post_date::text=r.candidate_json->>'post_date'
    and claim.group_key=r.candidate_json->>'group_key'
    and claim.source_url=r.candidate_json->>'source_url' and claim.image_url=r.candidate_json->>'image_url'
    and claim.thumbnail_url is not distinct from r.candidate_json->>'thumbnail_url'
    and claim.fingerprints=(select array_agg(distinct fp order by fp)
      from unnest(array[r.candidate_json->>'source_fingerprint',r.candidate_json->>'image_fingerprint',r.candidate_json->>'thumbnail_fingerprint']) fp where fp is not null)
 ) known_siblings;
 if exists(select 1 from jsonb_array_elements(snap->'rows') h
     where h->>'visual_sha256'=any(array[candidate->>'source_sha256',candidate->>'image_sha256',candidate->>'thumbnail_sha256'])
       and not (h->>'history_key'=any(sibling_history_keys)))
  or exists(select 1 from public.fixer_forward_media_historical_original_20261007 h
     where h.source_fingerprint=any(array[candidate->>'source_fingerprint',candidate->>'image_fingerprint',candidate->>'thumbnail_fingerprint']))
  or exists(select 1 from public.fixer_forward_media_use_20261006 u
     where u.fingerprint=any(array[candidate->>'source_fingerprint',candidate->>'image_fingerprint',candidate->>'thumbnail_fingerprint'])
       and not (anchor.audit_id is not null and u.fingerprint=any(array[candidate->>'source_fingerprint',candidate->>'image_fingerprint',candidate->>'thumbnail_fingerprint'])
         and u.tenant_id=candidate->>'tenant_id' and u.post_date::text=candidate->>'post_date'
         and u.group_key=candidate->>'group_key'))
  or exists(select 1 from public.fixer_forward_media_history_clearance_20261006 h
     where h.source_fingerprint=any(array[candidate->>'source_fingerprint',candidate->>'image_fingerprint',candidate->>'thumbnail_fingerprint'])
       and not (anchor.audit_id is not null and h.tenant_id=src.tenant_id and h.source_asset_id=src.source_asset_id
         and to_jsonb(h)-'checked_at'=clearance))
  or exists(select 1 from public.fixer_owner_photo_reservation_20261007 r
     where (r.candidate_json->>'source_sha256'=any(array[candidate->>'source_sha256',candidate->>'image_sha256',candidate->>'thumbnail_sha256'])
       or r.candidate_json->>'image_sha256'=any(array[candidate->>'source_sha256',candidate->>'image_sha256',candidate->>'thumbnail_sha256'])
       or r.candidate_json->>'thumbnail_sha256'=any(array[candidate->>'source_sha256',candidate->>'image_sha256',candidate->>'thumbnail_sha256']))
      and (not r.audit_id=any(sibling_ids) or r.candidate_json->>'image_sha256'=candidate->>'image_sha256')) then
  raise exception 'already used cleared or reserved visual requires HOLD' using errcode='23514'; end if;
 insert into public.fixer_owner_photo_reservation_20261007(audit_id,receipt_ref,calendar_row_id,original_json,manifest_json,candidate_json)
 values(p_audit_id,ref,cert.calendar_row_id,p_original,p_manifest,candidate);
 if anchor.audit_id is null then
 insert into public.fixer_forward_media_original_registry_20261006
 (tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)
 values(src.tenant_id,src.source_asset_id,src.exact_source_url,src.source_fingerprint,src.source_length,src.receipt_ref);
 insert into public.fixer_forward_media_history_clearance_20261006
 (tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref)
 values(src.tenant_id,src.source_asset_id,src.exact_source_url,src.source_fingerprint,src.source_length,src.receipt_ref,'cleared_unused',ref);
 end if;
 insert into public.fixer_forward_media_render_manifest_20261006
 (manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,thumbnail_url,thumbnail_fingerprint,thumbnail_length,operation,render_recipe,render_evidence_ref)
 values(p_manifest->>'manifest_digest',src.tenant_id,src.source_asset_id,p_manifest->>'image_url',p_manifest->>'image_fingerprint',
 (p_manifest->>'image_length')::bigint,p_manifest->>'thumbnail_url',p_manifest->>'thumbnail_fingerprint',
 (p_manifest->>'thumbnail_length')::bigint,p_manifest->>'operation',p_manifest->'render_recipe',cert.receipt_ref);
 return clearance;
end; $$;
revoke all on function public.fixer_prepare_owner_staged_photo_20261008(uuid,jsonb,jsonb) from public,anon,authenticated,service_role,
 fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;
grant execute on function public.fixer_prepare_owner_staged_photo_20261008(uuid,jsonb,jsonb) to fixer_forward_media_owner_20261006;

-- STAGED service-role binder: the ONLY authority that may attach a persisted
-- render manifest digest to a STAGED candidate row before finalization. It
-- accepts only the row ID; a caller can never supply a digest, hash, URL,
-- claim token or credential. Registered nonterminal membership, canonical
-- tenant agreement (alias-resolved), the immutable member content/media
-- binding and the authorizing predicate are all re-read under the row lock.
-- The exact owner authority tuple (registry + cleared history + exactly one
-- matching immutable manifest) must already exist. render_manifest_digest is
-- one of the two trusted preparation output fields the stage predicate and
-- the finalizer allow to differ from the staged snapshot; every other column
-- must still equal the staged bytes. Never activates, approves, claims or
-- sends; the active binder RPC remains the only authority for active rows.
create function public.fixer_bind_forward_schedule_staged_manifest_20261008(p_calendar_row_id uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare
  r public.content_calendar%rowtype; tenant text; digest text; matches integer;
  original public.fixer_forward_media_original_registry_20261006%rowtype;
  m public.forward_schedule_stage_member_20261008%rowtype;
  b public.forward_schedule_stage_batch_20261008%rowtype;
begin
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  select * into r from public.content_calendar where id=p_calendar_row_id for update;
  if not found or r.status not in ('draft','pending')
      or r.publish_claim_token is not null
      or r.published_at is not null or r.late_post_id is not null
      or r.variant_status is distinct from 'candidate'
      or r.media_not_ready_reason is distinct from 'forward_reservation_staged'
      or r.post_date is null
      or nullif(btrim(r.gym_id),'') is null or nullif(btrim(r.visual_group_key),'') is null
      or nullif(btrim(r.source_media_asset_id),'') is null
      or r.source_media_url is null or r.source_media_url !~ '^https://[^[:space:]]+$'
      or r.image_url is null or r.image_url !~ '^https://[^[:space:]]+$'
      or (r.thumbnail_url is not null and r.thumbnail_url !~ '^https://[^[:space:]]+$') then
    raise exception 'staged binding requires an unsent staged candidate with complete explicit source and media identity' using errcode='23514';
  end if;
  tenant:=public.fixer_forward_schedule_canonical_tenant_20261008(r.gym_id);
  select * into m from public.forward_schedule_stage_member_20261008 where calendar_row_id=r.id;
  select * into b from public.forward_schedule_stage_batch_20261008 where batch_id=m.batch_id;
  if m.calendar_row_id is null or b.state is distinct from 'staged'
      or tenant is null or tenant is distinct from m.tenant_id or m.tenant_id is distinct from b.tenant_id
      or m.logical_post_id is distinct from r.logical_post_id
      or m.post_date is distinct from r.post_date or m.gym_id is distinct from r.gym_id
      or m.source_media_url is distinct from r.source_media_url
      or m.image_url is distinct from r.image_url
      or m.thumbnail_url is distinct from r.thumbnail_url
      or not public.fixer_forward_schedule_staged_authorized_20261008(r.id,m.tenant_id,m.batch_id) then
    raise exception 'staged membership and preparation authorization required' using errcode='23514';
  end if;
  -- Raw ownership: the authoritative original binds the row's RAW gym key and
  -- exact source URL; the canonical tenant only scopes batch membership.
  select * into original from public.fixer_forward_media_original_registry_20261006 o
    where o.tenant_id=btrim(r.gym_id) and o.source_asset_id=btrim(r.source_media_asset_id);
  if not found or original.source_url is distinct from r.source_media_url then
    raise exception 'authoritative original registry binding unavailable' using errcode='23514';
  end if;
  if not exists(select 1 from public.fixer_forward_media_history_clearance_20261006 c
      where c.tenant_id=original.tenant_id and c.source_asset_id=original.source_asset_id
        and c.source_url=original.source_url and c.source_fingerprint=original.source_fingerprint
        and c.source_length=original.source_length and c.registry_evidence_ref=original.registry_evidence_ref
        and c.decision='cleared_unused')
      or exists(select 1 from public.fixer_forward_media_history_clearance_20261006 c
        where c.source_fingerprint=original.source_fingerprint and c.decision<>'cleared_unused') then
    raise exception 'original historical eligibility clearance unavailable or held' using errcode='23514';
  end if;
  select count(*), min(mf.manifest_digest) into matches, digest
    from public.fixer_forward_media_render_manifest_20261006 mf
    where mf.tenant_id=original.tenant_id and mf.source_asset_id=original.source_asset_id
      and mf.image_url=r.image_url and mf.thumbnail_url is not distinct from r.thumbnail_url;
  if matches is distinct from 1 then
    raise exception 'exactly one matching immutable render manifest is required' using errcode='23514';
  end if;
  if r.render_manifest_digest is null then
    update public.content_calendar set render_manifest_digest=digest where id=r.id;
    return true;
  end if;
  if r.render_manifest_digest=digest then
    return true;
  end if;
  raise exception 'row is already bound to a conflicting render manifest digest' using errcode='23514';
end;
$$;
revoke all on function public.fixer_bind_forward_schedule_staged_manifest_20261008(uuid)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_bind_forward_schedule_staged_manifest_20261008(uuid) to service_role;

-- ---------------------------------------------------------------------------
-- Staged alias provenance handoff (create-or-replace of the attester-facing
-- provenance lookup; the frozen claim-migration original and the photo
-- clearance wrapper chain beneath it are UNCHANGED).
--
-- For a genuine staged member row whose gym key is a registered ALIAS, the
-- owner/photo/binder authority rows (original registry, history clearance,
-- render manifest) are keyed under the RAW gym key, while the prepared
-- attestation snapshot resolves the canonical tenant through
-- fixer_forward_media_tenant_alias_20261006. The inherited lookup therefore
-- raised 'authoritative original registry binding unavailable' for exactly
-- those rows. This replacement keeps the complete inherited logic byte-for-byte
-- and adds a STAGED-ONLY fallback: when the canonical-keyed registry lookup
-- fails with exactly that message, the raw registry key is derived from the
-- persisted immutable row itself (snapshot gym_id), the alias->canonical
-- mapping is re-verified against the REGISTERED staged membership and the
-- exact preparation predicate, and the identical registry/clearance/manifest
-- checks run under the raw key. Rows without genuine staged membership, a
-- matching alias mapping, exact content binding and the exact predicate result
-- keep the original refusal. A finalized member uses its exact durable batch
-- receipt and active reservation/revision binding for readback only; terminal
-- batches remain closed to staged preparation. Ordinary active rows, ACLs and
-- every signed-source guard retain the inherited behavior.
create or replace function public.fixer_forward_media_provenance_lookup_20261006(p_calendar_row_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare provenance jsonb; grant_row public.fixer_owner_photo_reservation_20261007%rowtype;
 snap jsonb; content jsonb; anchor_audit uuid; required_audit uuid;
 caller text:=coalesce(nullif(current_setting('role',true),'none'),session_user);
 staged_snapshot jsonb; staged_row public.content_calendar%rowtype;
 staged_member public.forward_schedule_stage_member_20261008%rowtype;
 staged_batch public.forward_schedule_stage_batch_20261008%rowtype;
 staged_original public.fixer_forward_media_original_registry_20261006%rowtype;
 staged_clearance public.fixer_forward_media_history_clearance_20261006%rowtype;
 staged_manifest public.fixer_forward_media_render_manifest_20261006%rowtype;
 raw_tenant text; predicate jsonb;
begin
 -- Production callbacks read provenance before later appending attestation in
 -- this SAME transaction. The attester must take its final exclusive graph
 -- mode now, before census authority; upgrading a shared graph while another
 -- claimant shares it and waits for census would deadlock.
 if pg_has_role(caller,'fixer_forward_media_attester_20261006','member') then
  if pg_has_role(caller,'service_role','member') or pg_has_role(caller,'fixer_forward_media_owner_20261006','member') then
   raise exception 'isolated attester provenance identity required' using errcode='42501'; end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 else
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 begin
  provenance:=public.fixer_photo_base_provenance_20261007(p_calendar_row_id);
 exception when check_violation then
  if sqlerrm is distinct from 'authoritative original registry binding unavailable' then
   raise; end if;
  -- STAGED-ONLY alias handoff: derive the raw registry key from the persisted
  -- immutable row and verify the canonical mapping against registered staged
  -- membership plus the exact preparation predicate before any raw-keyed read.
  staged_snapshot:=public.fixer_forward_media_attestation_request_20261006(p_calendar_row_id);
  select * into staged_row from public.content_calendar where id=p_calendar_row_id;
  raw_tenant:=nullif(btrim(coalesce(staged_row.gym_id,'')),'');
  if raw_tenant is null or staged_snapshot->>'tenant_id' is null
      or raw_tenant = staged_snapshot->>'tenant_id' then
   -- Same key: the canonical-keyed miss is genuine, not an alias handoff.
   raise exception 'authoritative original registry binding unavailable' using errcode='23514'; end if;
  if not exists(select 1 from public.fixer_forward_media_tenant_alias_20261006 a
     where a.alias_key=raw_tenant and a.tenant_id=staged_snapshot->>'tenant_id') then
   raise exception 'authoritative original registry binding unavailable' using errcode='23514'; end if;
  select * into staged_member from public.forward_schedule_stage_member_20261008
    where calendar_row_id=staged_row.id;
  select * into staged_batch from public.forward_schedule_stage_batch_20261008
    where batch_id=staged_member.batch_id;
  if staged_member.calendar_row_id is null or staged_batch.state not in ('staged','finalized')
      or staged_member.tenant_id is distinct from staged_snapshot->>'tenant_id'
      or staged_batch.tenant_id is distinct from staged_member.tenant_id
      or staged_member.gym_id is distinct from staged_row.gym_id
      or staged_member.logical_post_id is distinct from staged_row.logical_post_id
      or staged_member.post_date is distinct from staged_row.post_date
      or staged_member.source_media_url is distinct from staged_row.source_media_url
      or staged_member.image_url is distinct from staged_row.image_url
      or staged_member.thumbnail_url is distinct from staged_row.thumbnail_url then
   raise exception 'authoritative original registry binding unavailable' using errcode='23514'; end if;
  -- Exact predicate: eligible under the canonical tenant. A still-staged row
  -- must bind its batch; a row activated inside the atomic finalizer (mode
  -- 'active', batch still staged) is the same registered member mid-commit.
  if staged_batch.state='staged' then
   predicate:=public.forward_schedule_preparation_eligible_20261008(staged_row.id);
   if predicate->>'eligible' is distinct from 'true' or predicate->>'reason' is not null
       or predicate->>'tenant_id' is distinct from staged_member.tenant_id
       or predicate->>'mode' not in ('staged','active')
       or (predicate->>'mode'='staged'
           and predicate->>'batch_id' is distinct from staged_member.batch_id::text) then
    raise exception 'authoritative original registry binding unavailable' using errcode='23514'; end if;
  else
   -- Terminal batches never admit new staged preparation. Read authority for
   -- an already finalized alias survives ONLY through its exact durable
   -- receipt, active reservation and immutable revision/lineage binding.
   if staged_row.variant_status is distinct from 'active'
       or staged_batch.finalize_receipt->>'batch_id' is distinct from staged_batch.batch_id::text
       or staged_batch.finalize_receipt->>'tenant_id' is distinct from staged_member.tenant_id
       or staged_batch.finalize_receipt->>'state' is distinct from 'finalized'
       or staged_batch.finalize_receipt->>'request_digest' is distinct from staged_batch.request_digest
       or not exists (
         select 1 from jsonb_array_elements_text(staged_batch.finalize_receipt->'row_ids')
             with ordinality rows(row_id,position)
         join jsonb_array_elements_text(staged_batch.finalize_receipt->'reservation_ids')
             with ordinality slots(reservation_id,position) using(position)
         join jsonb_array_elements(staged_batch.finalize_request->'candidates')
             with ordinality finalized(candidate,position) using(position)
         join public.forward_schedule_reservation reserved
             on reserved.reservation_id::text=slots.reservation_id
         join public.forward_schedule_reservation_binding_20261008 bound
             on bound.reservation_id=reserved.reservation_id
         join public.fixer_forward_media_lineage_20261006 lineage
             on lineage.evidence_id=bound.lineage_evidence_id
         where rows.row_id=staged_row.id::text
           and finalized.candidate->>'calendar_row_id'=staged_row.id::text
           and finalized.candidate->>'logical_post_id'=staged_row.logical_post_id::text
           and finalized.candidate->>'expected_revision'=staged_snapshot->>'revision'
           and reserved.state='active' and reserved.tenant_id=staged_member.tenant_id
           and reserved.logical_post_id=staged_row.logical_post_id
           and reserved.post_date=staged_row.post_date
           and reserved.source_asset_id=staged_snapshot->>'source_asset_id'
           and reserved.source_url=staged_snapshot->>'source_url'
           -- Platform siblings share slot ownership but retain their OWN
           -- row revision, lineage and visual IDs in the immutable binding.
           and bound.calendar_row_id=staged_row.id
           and bound.row_revision=staged_snapshot->>'revision'
           and to_jsonb(bound.attestation_ids)=finalized.candidate->'attestation_ids'
           and lineage.calendar_row_id=staged_row.id and lineage.row_revision=bound.row_revision
           and lineage.tenant_id=reserved.tenant_id
           and lineage.source_asset_id=reserved.source_asset_id
           and lineage.manifest_digest=staged_snapshot->>'render_manifest_digest'
           and lineage.group_key=staged_snapshot->>'group_key'
           and exists(select 1 from public.forward_media_visual_attestation original
             where original.attestation_id=any(bound.attestation_ids) and original.role='original'
               and original.lineage_receipt_id=bound.lineage_evidence_id
               and original.tenant_key=reserved.tenant_id
               and original.row_revision=('x'||substr(bound.row_revision,1,15))::bit(60)::bigint
               and original.source_sha256=reserved.source_sha256)) then
    raise exception 'finalized alias provenance requires exact active reservation receipt' using errcode='23514';
   end if;
  end if;
  -- Identical authority checks under the RAW gym key.
  select * into staged_original from public.fixer_forward_media_original_registry_20261006
    where tenant_id=raw_tenant and source_asset_id=staged_snapshot->>'source_asset_id';
  if not found or staged_original.source_url is distinct from staged_snapshot->>'source_url' then
   raise exception 'authoritative original registry binding unavailable' using errcode='23514'; end if;
  select * into staged_clearance from public.fixer_forward_media_history_clearance_20261006 c
    where c.tenant_id=staged_original.tenant_id and c.source_asset_id=staged_original.source_asset_id;
  if not found or staged_clearance.decision<>'cleared_unused'
      or staged_clearance.source_url is distinct from staged_original.source_url
      or staged_clearance.source_fingerprint is distinct from staged_original.source_fingerprint
      or staged_clearance.source_length is distinct from staged_original.source_length
      or staged_clearance.registry_evidence_ref is distinct from staged_original.registry_evidence_ref
      or exists(select 1 from public.fixer_forward_media_history_clearance_20261006 c
        where c.source_fingerprint=staged_original.source_fingerprint and c.decision<>'cleared_unused') then
   raise exception 'original historical eligibility clearance unavailable or held' using errcode='23514'; end if;
  select * into staged_manifest from public.fixer_forward_media_render_manifest_20261006
    where manifest_digest=staged_snapshot->>'render_manifest_digest';
  if not found or staged_manifest.tenant_id is distinct from staged_original.tenant_id
      or staged_manifest.source_asset_id is distinct from staged_original.source_asset_id
      or staged_manifest.image_url is distinct from staged_snapshot->>'image_url'
      or staged_manifest.thumbnail_url is distinct from staged_snapshot->>'thumbnail_url' then
   raise exception 'versioned render manifest binding unavailable' using errcode='23514'; end if;
  provenance:=jsonb_build_object('original',to_jsonb(staged_original),
    'manifest',to_jsonb(staged_manifest),'clearance',to_jsonb(staged_clearance),
    -- Explicit callback bridge from this exact canonical snapshot to its
    -- verified raw source authority. Signed registry/manifest/certificate
    -- tenancy remains unchanged. Every check below must pass before return.
    'staged_alias_binding',jsonb_build_object('authority_tenant_id',raw_tenant,
      'snapshot',staged_snapshot));
 end;
 select r.* into grant_row from public.fixer_owner_photo_reservation_20261007 r
 where r.receipt_ref=provenance#>>'{clearance,history_evidence_ref}';
 if found then
  anchor_audit:=grant_row.audit_id;
  select r.* into grant_row from public.fixer_owner_photo_reservation_20261007 r
   where r.manifest_json=(provenance->'manifest')-'registered_at';
  if not found then
   raise exception 'photo provenance requires exact signed rendition reservation' using errcode='23514'; end if;
  -- Hold current safety/source bindings until the attestation/claim commits.
  -- A concurrent pending-moderation change must serialize before the check or
  -- after the immutable grant; a stale SELECT cannot admit it in between.
  perform 1 from public.media_asset a join public.media_source s on s.id=a.source_id
    where a.id=grant_row.candidate_json->>'source_asset_id' for share of a,s;
  perform 1 from public.fixer_forward_media_photo_state_20261007 where singleton for share;
  content:=public.fixer_forward_media_photo_content_20261007(p_calendar_row_id);
  if content->>'tenant_id' is distinct from grant_row.candidate_json->>'tenant_id'
    or content->>'post_date' is distinct from grant_row.candidate_json->>'post_date'
    or content->>'group_key' is distinct from grant_row.candidate_json->>'group_key' then
   raise exception 'photo provenance requires signed tenant date and group' using errcode='23514'; end if;
  if not public.fixer_owner_photo_source_ready_20261007(grant_row.candidate_json->>'source_asset_id',grant_row.candidate_json->>'source_sha256')
    or not exists(select 1 from public.fixer_forward_media_source_receipt_20261007 src
      join public.media_asset a on a.id=src.source_asset_id
      join public.media_source s on s.id=a.source_id
      where src.receipt_ref=grant_row.candidate_json->>'source_receipt_ref'
       and a.gym_id=src.tenant_id and s.gym_id=a.gym_id and s.active and s.kind='gym_drive'
       and s.id=src.source_id and s.folder_id=src.folder_id) then
   raise exception 'photo provenance requires current approved byte-bound same-gym source' using errcode='23514'; end if;
  -- Recheck the signed rendition at the read/send boundary as well as INSERT.
  -- A manifest predating this guard must never inherit another rendition's
  -- positive source clearance merely because its tenant/source IDs match.
  if (provenance->'manifest')-'registered_at' is distinct from grant_row.manifest_json then
   raise exception 'photo provenance requires exact signed rendition reservation' using errcode='23514'; end if;
  for required_audit in select distinct id from unnest(array[anchor_audit,grant_row.audit_id]) id loop
  if exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=required_audit)
    or not exists(select 1 from public.fixer_forward_media_photo_certificate_20261007 c
     join public.fixer_forward_media_photo_state_20261007 state on state.singleton
     join public.fixer_forward_media_photo_key_20261007 k on k.key_id=c.key_id
     join public.fixer_forward_media_photo_policy_20261007 p on p.policy_id=k.policy_id
     where c.audit_id=required_audit and c.baseline_id=state.baseline_id and c.generation=state.generation
      and state.enabled and nullif(btrim(state.routes_reconciled_ref),'') is not null and k.approved and p.approved
      and not exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id)) then
   raise exception 'photo reservation revoked disabled or retired epoch' using errcode='23514'; end if;
  end loop;
  if exists(select 1 from public.fixer_forward_media_historical_original_20261007 h
    where h.source_fingerprint=any(array[grant_row.candidate_json->>'source_fingerprint',grant_row.candidate_json->>'image_fingerprint',grant_row.candidate_json->>'thumbnail_fingerprint'])) then
   raise exception 'new trusted historical byte match requires HOLD' using errcode='23514'; end if;
  snap:=public.fixer_forward_media_photo_snapshot_20261007();
  if snap->'policy_approved' is distinct from 'true'::jsonb or snap->'scope_complete' is distinct from 'true'::jsonb
    or exists(select 1 from jsonb_array_elements(snap->'rows') h where h->'resolved' is distinct from 'true'::jsonb
      or h->>'media_kind' is distinct from 'still_photo') then
   raise exception 'new unknown historical photo corpus requires HOLD' using errcode='23514'; end if;
 end if;
 return provenance;
end; $$;
revoke all on function public.fixer_forward_media_provenance_lookup_20261006(uuid)
  from public,anon,authenticated,service_role;
grant execute on function public.fixer_forward_media_provenance_lookup_20261006(uuid)
  to fixer_forward_media_attester_20261006;
commit;
