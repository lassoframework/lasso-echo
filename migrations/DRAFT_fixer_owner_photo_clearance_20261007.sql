-- DRAFT / UNAPPLIED / DEFAULT OFF. Requires claim, source-history and photo
-- certificate drafts. No credentials, keys, full-corpus evidence or activation.
-- Signed visual judgment is verified by isolated auditor AND owner runtimes.
-- PostgreSQL trusts authenticated immutable auditor receipts, not native Ed25519.
-- Clearance is owner preparation, never a callback from a publisher. Byte
-- occupancy handles sends/replays/siblings; near-scene nonmatch remains an
-- explicit independent visual judgment against history AND reserved visuals.
begin;
create table public.fixer_owner_photo_reservation_20261007 (
 audit_id uuid primary key references public.fixer_forward_media_photo_certificate_20261007(audit_id),
 receipt_ref text unique not null,
 calendar_row_id uuid not null,
 original_json jsonb not null,
 manifest_json jsonb not null,
 candidate_json jsonb not null,
 reserved_at timestamptz not null default clock_timestamp()
);
alter table public.fixer_owner_photo_reservation_20261007 enable row level security;
revoke all on public.fixer_owner_photo_reservation_20261007 from public,anon,authenticated,service_role,
 fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;
create trigger immutable_row before update or delete on public.fixer_owner_photo_reservation_20261007
 for each row execute function public.fixer_forward_media_immutable_20261006();
create trigger immutable_truncate before truncate on public.fixer_owner_photo_reservation_20261007
 for each statement execute function public.fixer_forward_media_immutable_20261006();

-- Preserve the historical census implementation; augment with prepared visuals
-- before future independent certificates can be recorded. Pending/prepared
-- photos are not silently absent from the next candidate's visual review.
alter function public.fixer_forward_media_photo_snapshot_20261007() rename to fixer_photo_history_snapshot_20261007;
revoke all on function public.fixer_photo_history_snapshot_20261007() from public,anon,authenticated,service_role,
 fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;
create function public.fixer_forward_media_photo_snapshot_20261007()
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; reservations jsonb; rows jsonb;
begin
 snap:=public.fixer_photo_history_snapshot_20261007();
 select coalesce(jsonb_agg(to_jsonb(r) order by r.audit_id),'[]'::jsonb) into reservations
 from public.fixer_owner_photo_reservation_20261007 r;
 select coalesce(jsonb_agg(item order by item->>'history_key'),'[]'::jsonb) into rows from (
  select case when j->>'history_key' like 'claim:%' and evidence.audit_id is not null then
   j || jsonb_build_object('resolved',true,'media_kind','still_photo',
     'visual_sha256',evidence.candidate_json->>'image_sha256','visual_url',claim.image_url) else j end item
  from jsonb_array_elements(snap->'rows') j
  left join public.fixer_forward_media_claim_receipt_20261006 claim
    on j->>'history_key'='claim:'||claim.claim_token::text
  left join lateral (
    select r.* from public.fixer_owner_photo_reservation_20261007 r
    where claim.tenant_id=r.candidate_json->>'tenant_id'
      and claim.post_date::text=r.candidate_json->>'post_date'
      and claim.group_key=r.candidate_json->>'group_key'
      and claim.source_url=r.candidate_json->>'source_url' and claim.image_url=r.candidate_json->>'image_url'
      and claim.thumbnail_url is null
      and claim.fingerprints=(select array_agg(distinct fp order by fp)
        from unnest(array[r.candidate_json->>'source_fingerprint',r.candidate_json->>'image_fingerprint']) fp)
    order by r.audit_id limit 1
  ) evidence on true
  union all
  select jsonb_build_object('history_key','unresolved-claimed-calendar:'||live.id::text,
    'resolved',false,'media_kind','unknown','visual_sha256',null,
    'published_binding_ref','calendar:'||live.id::text)
  from public.content_calendar live
  where (live.status='published' or live.published_at is not null or live.late_post_id is not null)
    and exists(select 1 from public.fixer_forward_media_claim_receipt_20261006 receipt where receipt.calendar_row_id=live.id)
    and not exists(select 1 from public.fixer_forward_media_claim_receipt_20261006 receipt
      where receipt.calendar_row_id=live.id and receipt.tenant_id=live.gym_id
       and receipt.post_date=live.post_date and receipt.group_key=live.visual_group_key
       and receipt.source_url=live.source_media_url and receipt.image_url=live.image_url
       and receipt.thumbnail_url is not distinct from live.thumbnail_url)
  union all
  select jsonb_build_object('history_key','owner-reserved-source:'||r.audit_id::text,
    'resolved',true,'media_kind','still_photo','visual_sha256',r.candidate_json->>'source_sha256',
    'published_binding_ref',r.receipt_ref,'visual_url',r.candidate_json->>'source_url') from public.fixer_owner_photo_reservation_20261007 r
  union all
  select jsonb_build_object('history_key','owner-reserved-image:'||r.audit_id::text,
    'resolved',true,'media_kind','still_photo','visual_sha256',r.candidate_json->>'image_sha256',
    'published_binding_ref',r.receipt_ref,'visual_url',r.candidate_json->>'image_url') from public.fixer_owner_photo_reservation_20261007 r
 ) all_visuals;
 return snap || jsonb_build_object('rows',rows,
   'scope_complete',snap->'scope_complete'='true'::jsonb and jsonb_array_length(rows)<=2500,
   'spine_digest','sha256:'||encode(sha256(convert_to(jsonb_build_object(
      'history_spine',snap->>'spine_digest','owner_reservations',reservations)::text,'UTF8')),'hex'));
end; $$;
revoke all on function public.fixer_forward_media_photo_snapshot_20261007() from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_forward_media_photo_snapshot_20261007() to fixer_forward_media_owner_20261006,fixer_forward_media_photo_auditor_20261007;

create function public.fixer_guard_positive_owner_photo_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if new.decision='cleared_unused' and not exists(
  select 1 from public.fixer_owner_photo_reservation_20261007 r
  where r.receipt_ref=new.history_evidence_ref
   and r.original_json=jsonb_build_object('tenant_id',new.tenant_id,'source_asset_id',new.source_asset_id,
    'source_url',new.source_url,'source_fingerprint',new.source_fingerprint,'source_length',new.source_length,
    'registry_evidence_ref',new.registry_evidence_ref)) then
  raise exception 'positive photo clearance requires certified owner reservation' using errcode='23514'; end if;
 return new;
end; $$;
revoke all on function public.fixer_guard_positive_owner_photo_20261007() from public,anon,authenticated,service_role;
create trigger certified_positive_clearance before insert on public.fixer_forward_media_history_clearance_20261006
 for each row execute function public.fixer_guard_positive_owner_photo_20261007();

-- Canonical UTF-8 JSON for the signed recipe digest (same contract as Python
-- photo certificate canonical()). Reject any differing numeric representation.
create function public.fixer_owner_photo_canonical_20261007(v jsonb)
returns text language plpgsql immutable set search_path=pg_catalog,public as $$
declare result text;
begin
 case jsonb_typeof(v)
 when 'object' then select '{'||coalesce(string_agg(to_jsonb(key)::text||':'||public.fixer_owner_photo_canonical_20261007(value),',' order by key),'')||'}' into result from jsonb_each(v);
 when 'array' then select '['||coalesce(string_agg(public.fixer_owner_photo_canonical_20261007(value),',' order by n),'')||']' into result from jsonb_array_elements(v) with ordinality e(value,n);
 else result:=v::text;
 end case;
 return result;
end; $$;
revoke all on function public.fixer_owner_photo_canonical_20261007(jsonb) from public,anon,authenticated,service_role;

create function public.fixer_prepare_owner_photo_20261007(p_audit_id uuid,p_original jsonb,p_manifest jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare cert public.fixer_forward_media_photo_certificate_20261007%rowtype;
 src public.fixer_forward_media_source_receipt_20261007%rowtype;
 state public.fixer_forward_media_photo_state_20261007%rowtype;
 old public.fixer_owner_photo_reservation_20261007%rowtype;
 snap jsonb; candidate jsonb; content jsonb; ref text; caller text; clearance jsonb;
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
   or p_manifest->'thumbnail_url' is distinct from 'null'::jsonb
   or p_manifest->'thumbnail_fingerprint' is distinct from 'null'::jsonb
   or p_manifest->'thumbnail_length' is distinct from 'null'::jsonb
   or p_manifest->>'render_evidence_ref' is distinct from cert.receipt_ref
   or candidate->>'render_recipe_digest' is distinct from 'sha256:'||encode(sha256(convert_to(public.fixer_owner_photo_canonical_20261007(p_manifest->'render_recipe'),'UTF8')),'hex') then
  raise exception 'exact certified original and rendition tuples required' using errcode='23514'; end if;
 ref:='owner-photo-reservation:'||cert.receipt_ref;
 clearance:=p_original || jsonb_build_object('decision','cleared_unused','history_evidence_ref',ref);
 select * into old from public.fixer_owner_photo_reservation_20261007 where audit_id=p_audit_id;
 if found then
  if cert.baseline_id is distinct from state.baseline_id or cert.generation is distinct from state.generation
   or exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=p_audit_id) then
   raise exception 'retired photo history epoch requires HOLD' using errcode='23514'; end if;
  if old.original_json is distinct from p_original or old.manifest_json is distinct from p_manifest then
   raise exception 'immutable owner photo identity conflict' using errcode='23514'; end if;
  return clearance; -- owner idempotency, no new eligibility grant
 end if;
 if not exists(select 1 from public.content_calendar row where row.id=cert.calendar_row_id
   and row.status in ('draft','pending','queued','approved') and row.variant_status='active'
   and row.publish_claim_token is null and row.published_at is null and row.late_post_id is null
   and row.render_manifest_digest is null and row.thumbnail_url is null) then
  raise exception 'new owner photo clearance requires an unsent canonical candidate' using errcode='23514'; end if;
 -- Every new approval sees all preceding reservations and sends. Missing/unknown
 -- legacy rows remain unresolved; signature shape or absence of hashes is never
 -- enough to turn uncertainty into a positive decision.
 snap:=public.fixer_forward_media_photo_snapshot_20261007();
 if snap->'policy_approved' is distinct from 'true'::jsonb or snap->'scope_complete' is distinct from 'true'::jsonb
  or cert.baseline_id is distinct from state.baseline_id or cert.generation is distinct from state.generation
  or cert.spine_digest is distinct from snap->>'spine_digest'
  or exists(select 1 from jsonb_array_elements(snap->'rows') h
    where h->'resolved' is distinct from 'true'::jsonb or h->>'media_kind' is distinct from 'still_photo') then
  raise exception 'complete current independently reviewed history and reservations required' using errcode='23514'; end if;
 if exists(select 1 from jsonb_array_elements(snap->'rows') h
     where h->>'visual_sha256'=any(array[candidate->>'source_sha256',candidate->>'image_sha256']))
  or exists(select 1 from public.fixer_forward_media_historical_original_20261007 h
     where h.source_fingerprint=any(array[candidate->>'source_fingerprint',candidate->>'image_fingerprint']))
  or exists(select 1 from public.fixer_forward_media_use_20261006 u
     where u.fingerprint=any(array[candidate->>'source_fingerprint',candidate->>'image_fingerprint']))
  or exists(select 1 from public.fixer_forward_media_history_clearance_20261006 h
     where h.source_fingerprint=any(array[candidate->>'source_fingerprint',candidate->>'image_fingerprint']))
  or exists(select 1 from public.fixer_owner_photo_reservation_20261007 r
     where r.candidate_json->>'source_sha256'=any(array[candidate->>'source_sha256',candidate->>'image_sha256'])
       or r.candidate_json->>'image_sha256'=any(array[candidate->>'source_sha256',candidate->>'image_sha256'])) then
  raise exception 'already used cleared or reserved visual requires HOLD' using errcode='23514'; end if;
 insert into public.fixer_owner_photo_reservation_20261007(audit_id,receipt_ref,calendar_row_id,original_json,manifest_json,candidate_json)
 values(p_audit_id,ref,cert.calendar_row_id,p_original,p_manifest,candidate);
 insert into public.fixer_forward_media_original_registry_20261006
 (tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)
 values(src.tenant_id,src.source_asset_id,src.exact_source_url,src.source_fingerprint,src.source_length,src.receipt_ref);
 insert into public.fixer_forward_media_history_clearance_20261006
 (tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref)
 values(src.tenant_id,src.source_asset_id,src.exact_source_url,src.source_fingerprint,src.source_length,src.receipt_ref,'cleared_unused',ref);
 insert into public.fixer_forward_media_render_manifest_20261006
 (manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,thumbnail_url,thumbnail_fingerprint,thumbnail_length,operation,render_recipe,render_evidence_ref)
 values(p_manifest->>'manifest_digest',src.tenant_id,src.source_asset_id,p_manifest->>'image_url',p_manifest->>'image_fingerprint',
 (p_manifest->>'image_length')::bigint,null,null,null,p_manifest->>'operation',p_manifest->'render_recipe',cert.receipt_ref);
 return clearance;
end; $$;
revoke all on function public.fixer_prepare_owner_photo_20261007(uuid,jsonb,jsonb) from public,anon,authenticated,service_role,
 fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;
grant execute on function public.fixer_prepare_owner_photo_20261007(uuid,jsonb,jsonb) to fixer_forward_media_owner_20261006;

-- Once a certified original has positive clearance, OWNER cannot attach an
-- arbitrary unsigned rendition to it through its legacy manifest INSERT grant.
create function public.fixer_guard_owner_photo_manifest_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if exists(select 1 from public.fixer_owner_photo_reservation_20261007 r
   where r.original_json->>'tenant_id'=new.tenant_id and r.original_json->>'source_asset_id'=new.source_asset_id)
  and not exists(select 1 from public.fixer_owner_photo_reservation_20261007 r
   where r.manifest_json=to_jsonb(new)-'registered_at') then
  raise exception 'certified original requires its exact signed rendition reservation' using errcode='23514'; end if;
 return new;
end; $$;
revoke all on function public.fixer_guard_owner_photo_manifest_20261007() from public,anon,authenticated,service_role;
create trigger certified_photo_rendition before insert on public.fixer_forward_media_render_manifest_20261006
 for each row execute function public.fixer_guard_owner_photo_manifest_20261007();

-- Negative authority is append-only. No runtime can revoke or un-revoke its
-- own grant. A retired history epoch never silently reactivates old clearance.
create table public.fixer_owner_photo_revocation_20261007 (
 audit_id uuid primary key references public.fixer_owner_photo_reservation_20261007(audit_id),
 reason_ref text not null check(btrim(reason_ref)<>'')
);
alter table public.fixer_owner_photo_revocation_20261007 enable row level security;
revoke all on public.fixer_owner_photo_revocation_20261007 from public,anon,authenticated,service_role,
 fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;
create trigger immutable_row before update or delete on public.fixer_owner_photo_revocation_20261007
 for each row execute function public.fixer_forward_media_immutable_20261006();
create trigger immutable_truncate before truncate on public.fixer_owner_photo_revocation_20261007
 for each statement execute function public.fixer_forward_media_immutable_20261006();
create function public.fixer_owner_photo_epoch_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if new.generation<old.generation or (
   exists(select 1 from public.fixer_owner_photo_reservation_20261007)
   and (new.baseline_id is distinct from old.baseline_id or new.routes_reconciled_ref is distinct from old.routes_reconciled_ref)
   and new.generation<=old.generation) then
  raise exception 'photo history epoch must advance and cannot regress' using errcode='23514'; end if;
 return new;
end; $$;
revoke all on function public.fixer_owner_photo_epoch_20261007() from public,anon,authenticated,service_role;
create trigger monotonic_photo_epoch before update on public.fixer_forward_media_photo_state_20261007
 for each row execute function public.fixer_owner_photo_epoch_20261007();

-- Recheck negative authority at existing attestation/claim provenance boundary.
-- This never reissues a certificate or clears a new image. Legitimate group
-- siblings and same-token claims share immutable eligibility within its epoch.
alter function public.fixer_forward_media_provenance_lookup_20261006(uuid) rename to fixer_photo_base_provenance_20261007;
revoke all on function public.fixer_photo_base_provenance_20261007(uuid) from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
create function public.fixer_forward_media_provenance_lookup_20261006(p_calendar_row_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare provenance jsonb; grant_row public.fixer_owner_photo_reservation_20261007%rowtype;
 snap jsonb;
begin
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 provenance:=public.fixer_photo_base_provenance_20261007(p_calendar_row_id);
 select r.* into grant_row from public.fixer_owner_photo_reservation_20261007 r
 where r.receipt_ref=provenance#>>'{clearance,history_evidence_ref}';
 if found then
  -- Recheck the signed rendition at the read/send boundary as well as INSERT.
  -- A manifest predating this guard must never inherit another rendition's
  -- positive source clearance merely because its tenant/source IDs match.
  if (provenance->'manifest')-'registered_at' is distinct from grant_row.manifest_json then
   raise exception 'photo provenance requires exact signed rendition reservation' using errcode='23514'; end if;
  if exists(select 1 from public.fixer_owner_photo_revocation_20261007 v where v.audit_id=grant_row.audit_id)
    or not exists(select 1 from public.fixer_forward_media_photo_certificate_20261007 c
     join public.fixer_forward_media_photo_state_20261007 state on state.singleton
     join public.fixer_forward_media_photo_key_20261007 k on k.key_id=c.key_id
     join public.fixer_forward_media_photo_policy_20261007 p on p.policy_id=k.policy_id
     where c.audit_id=grant_row.audit_id and c.baseline_id=state.baseline_id and c.generation=state.generation
      and state.enabled and nullif(btrim(state.routes_reconciled_ref),'') is not null and k.approved and p.approved
      and not exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id)) then
   raise exception 'photo reservation revoked disabled or retired epoch' using errcode='23514'; end if;
  if exists(select 1 from public.fixer_forward_media_historical_original_20261007 h
    where h.source_fingerprint=any(array[grant_row.candidate_json->>'source_fingerprint',grant_row.candidate_json->>'image_fingerprint'])) then
   raise exception 'new trusted historical byte match requires HOLD' using errcode='23514'; end if;
  snap:=public.fixer_forward_media_photo_snapshot_20261007();
  if snap->'policy_approved' is distinct from 'true'::jsonb or snap->'scope_complete' is distinct from 'true'::jsonb
    or exists(select 1 from jsonb_array_elements(snap->'rows') h where h->'resolved' is distinct from 'true'::jsonb
      or h->>'media_kind' is distinct from 'still_photo') then
   raise exception 'new unknown historical photo corpus requires HOLD' using errcode='23514'; end if;
 end if;
 return provenance;
end; $$;
revoke all on function public.fixer_forward_media_provenance_lookup_20261006(uuid) from public,anon,authenticated,service_role;
grant execute on function public.fixer_forward_media_provenance_lookup_20261006(uuid) to fixer_forward_media_attester_20261006;

-- Shared statement locks precede row locks. Owner preparation is exclusive;
-- existing publishers/attesters preserve their own graph lock order, with no
-- shared-to-exclusive publisher upgrades introduced by this migration.
create function public.fixer_owner_photo_corpus_write_lock_20261007()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0)); return null;
end; $$;
revoke all on function public.fixer_owner_photo_corpus_write_lock_20261007() from public,anon,authenticated,service_role;
do $$ declare t text; begin
 foreach t in array array['content_calendar','media_asset','media_source','fixer_forward_media_photo_state_20261007',
   'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007'] loop
  execute format('create trigger owner_photo_corpus_write before insert or update or delete or truncate on public.%I for each statement execute function public.fixer_owner_photo_corpus_write_lock_20261007()',t);
 end loop;
end; $$;
commit;
