-- DRAFT / UNAPPLIED / DEFAULT OFF. First certificate slice, NO provider release.
-- Requires forward authority/source-history drafts. No LOGIN/key is provisioned.
-- PostgreSQL authenticates the independent verification role; Ed25519 is verified
-- by the isolated Python verifier and verified AGAIN by the owner. No native PG
-- Ed25519 verifier is asserted. Treat this role as privileged verification state,
-- like the independently credentialed byte attester. Never grant it to producers,
-- publishers or source owners. Raw table writes remain denied to every runtime.
-- Approved keys, explicit scope policy and a sealed fully resolved corpus need
-- independent ADMIN provisioning. Missing/unknown corpus keeps approvals held.
-- A still-photo scope may exclude videos ONLY with an explicit ADMIN ruling;
-- content-type headers, dHash absence and current Drive metadata are insufficient.
-- Snapshot includes frozen historical evidence + committed forward claims. Full
-- claim/provider fencing is a REQUIRED subsequent integration, not enabled here.
-- Rollback before use: remove empty objects. After use preserve signed receipts.
begin;
create role fixer_forward_media_photo_auditor_20261007 nologin;
grant usage on schema public to fixer_forward_media_photo_auditor_20261007;

create table public.fixer_forward_media_photo_policy_20261007 (
 policy_id text primary key, approved boolean not null default false,
 scope text not null check(scope='complete_fleet_still_photo_history'),
 video_exclusion_ruling_ref text,
 cutover_reconciliation_ref text not null check(btrim(cutover_reconciliation_ref)<>''),
 approved_by text not null check(btrim(approved_by)<>'')
);
create table public.fixer_forward_media_photo_key_20261007 (
 key_id text primary key, auditor_id text not null, verifier_role text not null,
 policy_id text not null references public.fixer_forward_media_photo_policy_20261007(policy_id),
 public_key_hex text not null check(public_key_hex ~ '^[0-9a-f]{64}$'),
 approved boolean not null default false
);
create table public.fixer_forward_media_photo_key_revocation_20261007 (
 key_id text primary key references public.fixer_forward_media_photo_key_20261007(key_id),
 reason_ref text not null check(btrim(reason_ref)<>'')
);
create table public.fixer_forward_media_photo_baseline_20261007 (
 baseline_id uuid primary key, policy_id text not null references public.fixer_forward_media_photo_policy_20261007(policy_id),
 scope_complete boolean not null default false,
 rows_json jsonb not null check(jsonb_typeof(rows_json)='array' and jsonb_array_length(rows_json)<=2500),
 historical_manifest_ref text not null check(btrim(historical_manifest_ref)<>''),
 excluded_video_manifest_ref text,
 excluded_rows_json jsonb not null default '[]'::jsonb check(jsonb_typeof(excluded_rows_json)='array'),
 declared_full_fleet_row_count integer not null check(declared_full_fleet_row_count>=0),
 recorded_at timestamptz not null default clock_timestamp()
);
create table public.fixer_forward_media_photo_state_20261007 (
 singleton boolean primary key default true check(singleton),
 baseline_id uuid references public.fixer_forward_media_photo_baseline_20261007(baseline_id),
 generation bigint not null default 0 check(generation>=0),
 enabled boolean not null default false,
 routes_reconciled_ref text
);
insert into public.fixer_forward_media_photo_state_20261007(singleton) values(true);

create table public.fixer_forward_media_photo_certificate_20261007 (
 audit_id uuid primary key, receipt_ref text unique not null check(receipt_ref ~ '^photo-audit:sha256:[0-9a-f]{64}$'),
 calendar_row_id uuid not null,
 key_id text not null references public.fixer_forward_media_photo_key_20261007(key_id),
 baseline_id uuid not null references public.fixer_forward_media_photo_baseline_20261007(baseline_id),
 generation bigint not null, spine_digest text not null,
 payload_json text not null, signature_hex text not null check(signature_hex ~ '^[0-9a-f]{128}$'),
 verified_by text not null, verified_at timestamptz not null default clock_timestamp()
);
create index fixer_photo_certificate_candidate_20261007
 on public.fixer_forward_media_photo_certificate_20261007(calendar_row_id,generation desc,verified_at desc);

do $$ declare t text; begin
 foreach t in array array['fixer_forward_media_photo_policy_20261007','fixer_forward_media_photo_key_20261007',
 'fixer_forward_media_photo_key_revocation_20261007',
 'fixer_forward_media_photo_baseline_20261007','fixer_forward_media_photo_state_20261007',
 'fixer_forward_media_photo_certificate_20261007'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007',t);
  if t<>'fixer_forward_media_photo_state_20261007' then
   execute format('create trigger immutable_row before update or delete on public.%I for each row execute function public.fixer_forward_media_immutable_20261006()',t);
   execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.fixer_forward_media_immutable_20261006()',t);
  end if;
 end loop;
end; $$;

create function public.fixer_forward_media_photo_content_20261007(p_id uuid)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$
 select jsonb_build_object('calendar_row_id',r.id,'tenant_id',r.gym_id,'group_key',r.visual_group_key,
 'post_date',r.post_date,'source_asset_id',r.source_media_asset_id,'source_url',r.source_media_url,
 'image_url',r.image_url,'thumbnail_url',r.thumbnail_url,'account',r.account,'format',r.format,
 'caption',r.caption,'gbp_location_id',r.gbp_location_id) from public.content_calendar r where r.id=p_id;
$$;

create function public.fixer_forward_media_photo_approved_key_20261007(p_key_id text)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$
 select jsonb_build_object('key_id',k.key_id,'auditor_id',k.auditor_id,'policy_id',k.policy_id,
 'public_key_hex',k.public_key_hex,'approved',k.approved and p.approved
  and not exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id),
 'verifier_role',k.verifier_role)
 from public.fixer_forward_media_photo_key_20261007 k
 join public.fixer_forward_media_photo_policy_20261007 p on p.policy_id=k.policy_id
 where k.key_id=p_key_id;
$$;

create function public.fixer_forward_media_photo_snapshot_20261007()
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare state public.fixer_forward_media_photo_state_20261007%rowtype;
 b public.fixer_forward_media_photo_baseline_20261007%rowtype;
 p public.fixer_forward_media_photo_policy_20261007%rowtype;
 claims jsonb; spine text; rows jsonb; live_rows jsonb;
begin
 select * into state from public.fixer_forward_media_photo_state_20261007 where singleton;
 select * into b from public.fixer_forward_media_photo_baseline_20261007 where baseline_id=state.baseline_id;
 if not found then return jsonb_build_object('policy_approved',false,'scope_complete',false,'rows','[]'::jsonb); end if;
 select * into p from public.fixer_forward_media_photo_policy_20261007 where policy_id=b.policy_id;
 select coalesce(jsonb_agg(to_jsonb(r) order by r.claim_token),'[]'::jsonb) into claims
  from public.fixer_forward_media_claim_receipt_20261006 r;
 select coalesce(jsonb_agg(public.fixer_forward_media_photo_content_20261007(r.id) order by r.id),'[]'::jsonb)
 into live_rows from public.content_calendar r
 where r.status='published' or r.published_at is not null or r.late_post_id is not null;
 -- A new committed/in-flight claim is part of history before provider I/O.
 -- It needs explicit reviewed visual evidence in a replacement baseline; a
 -- signed old baseline cannot silently omit its claimed delivered image.
 select coalesce(jsonb_agg(value order by value->>'history_key'),'[]'::jsonb) into rows from (
  select value from jsonb_array_elements(b.rows_json)
  union all
  select jsonb_build_object('history_key','claim:'||r.claim_token::text,
    'resolved',false,'media_kind','unknown','visual_sha256',null,
    'published_binding_ref','forward-claim:'||r.claim_token::text)
  from public.fixer_forward_media_claim_receipt_20261006 r
  where not exists(select 1 from jsonb_array_elements(b.rows_json) j
    where j->>'history_key'='claim:'||r.claim_token::text)
  union all
  select jsonb_build_object('history_key','unresolved-calendar:'||(live.value->>'calendar_row_id'),
    'resolved',false,'media_kind','unknown','visual_sha256',null,
    'published_binding_ref','calendar:'||(live.value->>'calendar_row_id'))
  from jsonb_array_elements(live_rows) live(value)
  where not exists(select 1 from jsonb_array_elements(b.rows_json||b.excluded_rows_json) covered(value)
    where covered.value->>'history_key'='calendar:'||(live.value->>'calendar_row_id')
      and covered.value->>'calendar_visual_digest'='sha256:'||encode(sha256(convert_to(live.value::text,'UTF8')),'hex'))
    and not exists(select 1 from public.fixer_forward_media_claim_receipt_20261006 receipt
      where receipt.calendar_row_id=(live.value->>'calendar_row_id')::uuid)
 ) complete_rows;
 spine:='sha256:'||encode(sha256(convert_to(jsonb_build_object('baseline_id',b.baseline_id,
   'rows',b.rows_json,'excluded_rows',b.excluded_rows_json,'live_rows',live_rows,'claims',claims)::text,'UTF8')),'hex');
 return jsonb_build_object('baseline_id',b.baseline_id,'policy_id',p.policy_id,
   'policy_approved',p.approved,'scope_complete',b.scope_complete
      and jsonb_array_length(rows)<=2500
      and b.declared_full_fleet_row_count=jsonb_array_length(b.rows_json)+jsonb_array_length(b.excluded_rows_json)
      and (jsonb_array_length(b.excluded_rows_json)=0
        or (nullif(btrim(p.video_exclusion_ruling_ref),'') is not null
          and nullif(btrim(b.excluded_video_manifest_ref),'') is not null
          and not exists(select 1 from jsonb_array_elements(b.excluded_rows_json) e
            where e->>'media_kind' is distinct from 'reviewed_video_scope_exclusion'
              or nullif(e->>'published_binding_ref','') is null))),
   'generation',state.generation,'spine_digest',spine,'rows',rows,
   'declared_full_fleet_row_count',b.declared_full_fleet_row_count,
   'video_exclusion_ruling_ref',p.video_exclusion_ruling_ref,
   'excluded_video_manifest_ref',b.excluded_video_manifest_ref);
end; $$;

create function public.fixer_forward_media_photo_record_20261007(
 p_payload_json text,p_signature_hex text,p_receipt_ref text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare p jsonb; snapshot jsonb; k public.fixer_forward_media_photo_key_20261007%rowtype;
 s public.fixer_forward_media_source_receipt_20261007%rowtype; r public.content_calendar%rowtype;
 c jsonb; d jsonb; history jsonb; old public.fixer_forward_media_photo_certificate_20261007%rowtype;
 caller text;
begin
 if p_payload_json is null or octet_length(p_payload_json)>4194304
   or p_signature_hex is null or p_signature_hex !~ '^[0-9a-f]{128}$' then
  raise exception 'bounded independently verified certificate required' using errcode='23514'; end if;
 -- SECURITY DEFINER current_user is function owner. Capture invoker through
 -- session_user/SET ROLE for authenticated verifier binding, never packet data.
 caller:=coalesce(nullif(current_setting('role',true),'none'),session_user);
 if not pg_has_role(caller,'fixer_forward_media_photo_auditor_20261007','member')
   or pg_has_role(caller,'fixer_forward_media_owner_20261006','member')
   or pg_has_role(caller,'service_role','member') then
  raise exception 'independent authenticated verifier required' using errcode='42501'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'certificate authority requires read committed' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
 p:=p_payload_json::jsonb; c:=p->'candidate';
 select * into k from public.fixer_forward_media_photo_key_20261007 where key_id=p->>'key_id';
 if not found or not k.approved
   or exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id)
   or k.auditor_id is distinct from p->>'auditor_id'
   or k.verifier_role is distinct from caller or k.policy_id is distinct from p->>'policy_id' then
  raise exception 'approved independent signer required' using errcode='23514'; end if;
 snapshot:=public.fixer_forward_media_photo_snapshot_20261007();
 if p->'schema_version' is distinct from '1'::jsonb
   or p->>'decision' is distinct from 'reviewed_no_prior_visual_use'
   or p->>'policy_id' is distinct from snapshot->>'policy_id'
   or snapshot->'policy_approved' is distinct from 'true'::jsonb
   or snapshot->'scope_complete' is distinct from 'true'::jsonb
   or p->>'baseline_id' is distinct from snapshot->>'baseline_id'
   or p->'generation' is distinct from snapshot->'generation'
   or p->>'spine_digest' is distinct from snapshot->>'spine_digest'
   or p_receipt_ref is distinct from 'photo-audit:sha256:'||encode(sha256(convert_to(p_payload_json||E'\n'||p_signature_hex,'UTF8')),'hex')
   or jsonb_typeof(p->'dispositions') is distinct from 'array'
   or jsonb_array_length(p->'dispositions') is distinct from jsonb_array_length(snapshot->'rows') then
  raise exception 'complete current audit corpus required' using errcode='23514'; end if;
 if (select count(distinct value->>'history_key') from jsonb_array_elements(p->'dispositions'))
     <>jsonb_array_length(snapshot->'rows') then
  raise exception 'complete unique audit dispositions required' using errcode='23514'; end if;
 for history in select value from jsonb_array_elements(snapshot->'rows') loop
  select value into d from jsonb_array_elements(p->'dispositions')
   where value->>'history_key'=history->>'history_key';
  if d is null or history->'resolved' is distinct from 'true'::jsonb
   or history->>'media_kind' is distinct from 'still_photo'
   or d->>'disposition' is distinct from 'reviewed_visual_nonmatch'
   or d->>'inspected_sha256' is distinct from history->>'visual_sha256'
   or d->>'published_binding_ref' is distinct from history->>'published_binding_ref'
   or nullif(btrim(d->>'review_evidence_ref'),'') is null then
   raise exception 'unresolved or matching historical visual' using errcode='23514'; end if;
 end loop;
 select * into s from public.fixer_forward_media_source_receipt_20261007
  where receipt_ref=c->>'source_receipt_ref';
 if not found or s.calendar_row_id is distinct from (c->>'calendar_row_id')::uuid
  or s.tenant_id is distinct from c->>'tenant_id' or s.source_asset_id is distinct from c->>'source_asset_id'
  or s.exact_source_url is distinct from c->>'source_url'
  or s.source_fingerprint is distinct from c->>'source_fingerprint'
  or s.source_sha256 is distinct from c->>'source_sha256'
  or s.source_length is distinct from (c->>'source_length')::bigint then
  raise exception 'exact durable original receipt required' using errcode='23514'; end if;
 select * into r from public.content_calendar where id=s.calendar_row_id for share;
 if not found or r.gym_id is distinct from c->>'tenant_id'
  or r.source_media_asset_id is distinct from c->>'source_asset_id'
  or r.source_media_url is distinct from c->>'source_url'
  or r.visual_group_key is distinct from c->>'group_key' or r.post_date::text is distinct from c->>'post_date'
  or r.image_url is distinct from c->>'image_url' or r.thumbnail_url is not null
  or c->>'content_digest' is distinct from 'sha256:'||encode(sha256(convert_to(public.fixer_forward_media_photo_content_20261007(r.id)::text,'UTF8')),'hex') then
  raise exception 'exact still candidate content required' using errcode='23514'; end if;
 if not exists(select 1 from public.media_asset a join public.media_source source on source.id=a.source_id
   where a.id=r.source_media_asset_id and a.gym_id=r.gym_id and source.gym_id=r.gym_id
     and source.active and source.kind='gym_drive' and source.id=s.source_id
     and source.folder_id=s.folder_id) then
  raise exception 'current same gym active source required' using errcode='23514'; end if;
 insert into public.fixer_forward_media_photo_certificate_20261007
  (audit_id,receipt_ref,calendar_row_id,key_id,baseline_id,generation,spine_digest,payload_json,signature_hex,verified_by)
 values((p->>'audit_id')::uuid,p_receipt_ref,r.id,k.key_id,(p->>'baseline_id')::uuid,
  (p->>'generation')::bigint,p->>'spine_digest',p_payload_json,p_signature_hex,caller)
 on conflict(audit_id) do nothing;
 select * into old from public.fixer_forward_media_photo_certificate_20261007 where audit_id=(p->>'audit_id')::uuid;
 if old.receipt_ref is distinct from p_receipt_ref or old.payload_json is distinct from p_payload_json
   or old.signature_hex is distinct from p_signature_hex then
  raise exception 'immutable audit identity conflict' using errcode='23514'; end if;
 return true;
end; $$;

create function public.fixer_forward_media_photo_certificate_20261007(p_audit_id uuid)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$
 select jsonb_build_object('packet',jsonb_build_object('payload',c.payload_json::jsonb,'signature_hex',c.signature_hex),
  'approved_key',public.fixer_forward_media_photo_approved_key_20261007(c.key_id))
 from public.fixer_forward_media_photo_certificate_20261007 c where audit_id=p_audit_id;
$$;

do $$ declare f text; begin
 foreach f in array array['fixer_forward_media_photo_content_20261007(uuid)',
 'fixer_forward_media_photo_approved_key_20261007(text)','fixer_forward_media_photo_snapshot_20261007()',
 'fixer_forward_media_photo_certificate_20261007(uuid)'] loop
  execute 'revoke all on function public.'||f||' from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006';
  execute 'grant execute on function public.'||f||' to fixer_forward_media_photo_auditor_20261007,fixer_forward_media_owner_20261006';
 end loop;
end; $$;
revoke all on function public.fixer_forward_media_photo_record_20261007(text,text,text)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_forward_media_photo_record_20261007(text,text,text)
 to fixer_forward_media_photo_auditor_20261007;
commit;
