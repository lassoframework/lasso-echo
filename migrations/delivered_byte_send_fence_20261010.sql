-- DRAFT / UNAPPLIED / DEFAULT OFF. Interim EXACT OUTBOUND IMAGE BYTES only.
-- No provider activation, no original-scene/derivative lineage claim, no waiver
-- of approval, consent, billing, source, existing visual or provider gates.
-- A historical DB-published URL fetch is an observation, NEVER proof that its
-- bytes were accepted/published by a provider. Observe EVERY past occurrence;
-- never collapse a digest to its first tenant/date. Ambiguous occupancy is final.
--
-- Trusted operator preparation (separate authorization required): provision a
-- login inheriting exact_byte_owner_20261010, populate tenant/target authority,
-- per-row history coverage + append-only historical observations, and optional
-- independently proven sibling memberships. No service credential can seed,
-- establish sibling identity, enable a gate, or directly read/write these tables.
-- All seed writes use READ COMMITTED and caller lock/statement deadlines.
-- Freeze both RPC digests, audit complete history and all lower provider call
-- boundaries, then call the operator activation RPC with frozen digests and
-- deployment/cutover evidence. New flags remain OFF until that release gate.
--
-- Runtime API (PostgREST calls must COMMIT before networking):
-- 1 exact_byte_send_context_20261010(row UUID,current persisted token UUID).
--   OFF: {enabled:false}. ON: exact immutable routing + row revision + complete
--   image list. Unsupported gallery/video/missing authority holds, not fallback.
-- 2 End the context transaction. Verify immutable public object authority and
--   read EVERY listed URL with approved-host/SSRF/size/image checks. This fence
--   cannot verify remote bytes in SQL. A publisher-supplied digest is evidence
--   of its observed bytes, not trusted original lineage or provider publication.
-- 3 exact_byte_authorize_send_20261010(row,token,unchanged context,images), where
--   images are ordered [{ordinal:0,role:'image',url:...,sha256:'sha256:<64 hex>',
--   byte_length:positive int}, ...]. All keys required; no extra/missing images.
--   Literal authorized=true grants ONE invocation with the bound target/images.
--   Lost response, error, replay or authorized=false means NO invocation. Never
--   auto retry a committed grant, even with a fresh calendar token. Durable
--   reservation never expires. COMMIT is the grant's linearization point.
-- 4 Append observed outcomes with exact_byte_record_outcome_20261010; unknown,
--   provider acceptance and platform readback are distinct. Outcomes NEVER free
--   occupancy. Read a specific receipt only through exact_byte_attempt_read.
-- Calendar mutation guard preserves bound creative/lease even after disable.
--
-- Single image + explicitly owner-mapped thumbnail is the interim supported
-- shape. Every actual provider image MUST be represented; galleries/videos hold.
-- Raw thumbnail_url is included only if that provider actually sends it, as
-- declared by its audited image_fields. Runtime rejects any unsupported payload.
-- URL immutability and complete lower-provider interception are release
-- prerequisites, not guarantees SQL can establish from arbitrary caller inputs.
--
-- Rollback BEFORE activation/use: drop new calendar triggers then new functions,
-- tables and role in dependency order in one gated migration. AFTER any use:
-- operator disable RPC only; preserve ALL observations, cutovers, attempts,
-- outcomes, guards and sibling evidence. Never DROP/DELETE/TRUNCATE history.
begin;

create role exact_byte_owner_20261010 nologin;
grant usage on schema public to exact_byte_owner_20261010;

create function public.exact_byte_immutable_20261010() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin raise exception 'exact-byte history is append-only' using errcode='23514'; end; $$;

-- Use one narrow authority lock, independent of unapplied forward-media drafts.
-- Try-lock guards on ordinary writes avoid calendar-row -> authority-lock
-- inversion. A competing write fails closed and must start a fresh transaction.
create function public.exact_byte_seed_lock_20261010() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin
 if current_setting('transaction_isolation') <> 'read committed' then
  raise exception 'exact-byte authority requires read committed' using errcode='25000'; end if;
 if not pg_try_advisory_xact_lock(hashtextextended('exact_byte_send_20261010',0)) then
  raise exception 'exact-byte authority busy' using errcode='55P03'; end if;
 return new;
end; $$;

create table public.exact_byte_tenant_20261010 (
 calendar_gym_id text primary key check(calendar_gym_id=btrim(calendar_gym_id) and calendar_gym_id<>''),
 canonical_tenant text not null check(canonical_tenant=btrim(canonical_tenant) and canonical_tenant<>''),
 posting_timezone text not null check(btrim(posting_timezone)<>''),
 evidence_ref text not null check(btrim(evidence_ref)<>'')
);
create table public.exact_byte_target_20261010 (
 calendar_gym_id text not null references public.exact_byte_tenant_20261010,
 account text not null check(account in ('instagram','facebook','googlebusiness')),
 format text not null check(format in ('feed','story')),
 provider_target jsonb not null check(jsonb_typeof(provider_target)='object'
  and jsonb_typeof(provider_target->'provider')='string' and provider_target->>'provider'<>''
  and jsonb_typeof(provider_target->'platform')='string' and provider_target->>'platform'<>''
  and jsonb_typeof(provider_target->'account_id')='string' and provider_target->>'account_id'<>''),
 -- This is an audited adapter declaration, not a service-supplied field list.
 image_fields text[] not null check(image_fields=array['image_url']::text[]
  or image_fields=array['image_url','thumbnail_url']::text[]),
 evidence_ref text not null check(btrim(evidence_ref)<>''),
 primary key(calendar_gym_id,account,format)
);
create table public.exact_byte_sibling_proof_20261010 (
 shared_posting_identity uuid primary key,
 canonical_tenant text not null check(btrim(canonical_tenant)<>''),
 post_date date not null,
 logical_post_id uuid not null,
 evidence_ref text not null check(btrim(evidence_ref)<>''),
 unique(canonical_tenant,post_date,logical_post_id)
);
create table public.exact_byte_sibling_member_20261010 (
 calendar_row_id uuid not null,
 row_revision text not null check(row_revision ~ '^sha256:[0-9a-f]{64}$'),
 shared_posting_identity uuid not null references public.exact_byte_sibling_proof_20261010,
 evidence_ref text not null check(btrim(evidence_ref)<>''),
 primary key(calendar_row_id,row_revision)
);
-- Coverage is a reviewed census assertion for the frozen DB row, distinct from
-- a byte observation. complete=false makes activation impossible. Explicit
-- no-image historical records need independently reviewed no-media evidence.
create table public.exact_byte_history_coverage_20261010 (
 calendar_row_id uuid primary key,
 row_revision text not null check(row_revision ~ '^sha256:[0-9a-f]{64}$'),
 complete boolean not null default false,
 no_images_verified boolean not null default false,
 evidence_ref text not null check(btrim(evidence_ref)<>'')
);
create table public.exact_byte_history_observation_20261010 (
 observation_id uuid primary key,
 calendar_row_id uuid,
 row_revision text check(row_revision ~ '^sha256:[0-9a-f]{64}$'),
 canonical_tenant text,
 post_date date,
 shared_posting_identity uuid references public.exact_byte_sibling_proof_20261010,
 exact_url text not null check(exact_url ~ '^https://[^[:space:]]+$'),
 sha256 text not null check(sha256 ~ '^sha256:[0-9a-f]{64}$'),
 byte_length bigint not null check(byte_length between 1 and 134217728),
 observation_kind text not null check(observation_kind in
  ('db_published_url_bytes','provider_receipt_bytes','platform_readback_bytes','uncertain_bytes')),
 evidence_ref text not null check(btrim(evidence_ref)<>''),
 observed_at timestamptz not null,
 recorded_at timestamptz not null default clock_timestamp()
);
create index exact_byte_history_digest_20261010 on public.exact_byte_history_observation_20261010(sha256);
create index exact_byte_history_row_20261010 on public.exact_byte_history_observation_20261010(calendar_row_id,row_revision,exact_url);

create table public.exact_byte_cutover_20261010 (
 cutover_id uuid primary key,
 corpus_sha256 text not null check(corpus_sha256 ~ '^sha256:[0-9a-f]{64}$'),
 historical_snapshot_sha256 text not null check(historical_snapshot_sha256 ~ '^sha256:[0-9a-f]{64}$'),
 runtime_commit text not null check(runtime_commit ~ '^[0-9a-f]{40}$'),
 boundary_inventory_sha256 text not null check(boundary_inventory_sha256 ~ '^sha256:[0-9a-f]{64}$'),
 deployment_evidence_ref text not null check(btrim(deployment_evidence_ref)<>''),
 complete_history_evidence_ref text not null check(btrim(complete_history_evidence_ref)<>''),
 activated_at timestamptz not null default clock_timestamp()
);
create table public.exact_byte_gate_20261010 (
 singleton boolean primary key default true check(singleton),
 enabled boolean not null default false,
 active_cutover_id uuid references public.exact_byte_cutover_20261010
);
insert into public.exact_byte_gate_20261010(singleton,enabled) values(true,false);
create table public.exact_byte_send_attempt_20261010 (
 attempt_id uuid primary key,
 calendar_row_id uuid not null,
 claim_token uuid not null,
 row_revision text not null,
 canonical_tenant text not null,
 post_date date not null,
 reservation_day date not null,
 shared_posting_identity uuid,
 provider_target jsonb not null,
 context jsonb not null,
 cutover_id uuid not null references public.exact_byte_cutover_20261010,
 authorized_at timestamptz not null default clock_timestamp(),
 -- No second grant for the same token OR row/native destination, even after
 -- failed/unknown outcome, a row edit, or token renewal.
 unique(claim_token), unique(calendar_row_id,provider_target)
);
create table public.exact_byte_attempt_image_20261010 (
 attempt_id uuid not null references public.exact_byte_send_attempt_20261010,
 ordinal integer not null check(ordinal between 0 and 1),
 role text not null check(role in ('image','thumbnail')),
 exact_url text not null,
 sha256 text not null check(sha256 ~ '^sha256:[0-9a-f]{64}$'),
 byte_length bigint not null check(byte_length between 1 and 134217728),
 primary key(attempt_id,ordinal)
);
create index exact_byte_attempt_digest_20261010 on public.exact_byte_attempt_image_20261010(sha256);
create table public.exact_byte_outcome_20261010 (
 outcome_id uuid primary key,
 attempt_id uuid not null references public.exact_byte_send_attempt_20261010,
 outcome text not null check(outcome in
  ('reserved_uncertain','uncertain','definite_no_send','provider_accepted','platform_verified')),
 evidence_ref text not null check(btrim(evidence_ref)<>''),
 recorded_at timestamptz not null default clock_timestamp()
);

-- No PUBLIC default execute, no direct service SELECT, no direct service writes.
-- Trusted operator seed privileges never include update/delete/truncate.
do $$ declare t text; begin
 foreach t in array array['exact_byte_tenant_20261010','exact_byte_target_20261010',
  'exact_byte_sibling_proof_20261010','exact_byte_sibling_member_20261010',
  'exact_byte_history_coverage_20261010','exact_byte_history_observation_20261010',
  'exact_byte_cutover_20261010','exact_byte_send_attempt_20261010',
  'exact_byte_attempt_image_20261010','exact_byte_outcome_20261010'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role,exact_byte_owner_20261010',t);
  execute format('create trigger immutable_row before update or delete on public.%I for each row execute function public.exact_byte_immutable_20261010()',t);
  execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.exact_byte_immutable_20261010()',t);
  execute format('create trigger seed_lock before insert on public.%I for each row execute function public.exact_byte_seed_lock_20261010()',t);
  execute format('grant select on public.%I to exact_byte_owner_20261010',t);
  execute format('create policy owner_read on public.%I for select to exact_byte_owner_20261010 using(true)',t);
  if t in ('exact_byte_tenant_20261010','exact_byte_target_20261010',
   'exact_byte_sibling_proof_20261010','exact_byte_sibling_member_20261010',
   'exact_byte_history_coverage_20261010','exact_byte_history_observation_20261010') then
   execute format('grant insert on public.%I to exact_byte_owner_20261010',t);
   execute format('create policy owner_seed on public.%I for insert to exact_byte_owner_20261010 with check(true)',t);
  end if;
 end loop;
end; $$;
alter table public.exact_byte_gate_20261010 enable row level security;
revoke all on public.exact_byte_gate_20261010 from public,anon,authenticated,service_role,exact_byte_owner_20261010;

-- Sibling proof/member appends are independently trusted preparations, not
-- modifications to the frozen historical seed. They may append after cutover
-- under the same authority lock; claims always validate persisted row revision.
-- Pin every calendar field (including optional/future creative columns), except
-- lease/terminal bookkeeping and derived schedule metadata. Raw image/account/
-- format/gym/date and approval provenance are always inside this SHA256.
create function public.exact_byte_row_revision_20261010(p_row public.content_calendar)
returns text language sql immutable set search_path=pg_catalog,public set TimeZone='UTC' as $$
 select 'sha256:'||encode(sha256(convert_to((to_jsonb(p_row)-array[
  'status','publish_claim_token','publish_reservation_day','published_at',
  'late_post_id','updated_at','scheduled_at','reject_reason'])::text,'UTF8')),'hex'); $$;

create function public.exact_byte_corpus_digest_20261010() returns text
language sql security definer set search_path=pg_catalog,public set TimeZone='UTC' as $$
 select 'sha256:'||encode(sha256(convert_to(jsonb_build_object(
 'tenants',(select coalesce(jsonb_agg(to_jsonb(t) order by calendar_gym_id),'[]'::jsonb) from public.exact_byte_tenant_20261010 t),
 'targets',(select coalesce(jsonb_agg(to_jsonb(t) order by calendar_gym_id,account,format),'[]'::jsonb) from public.exact_byte_target_20261010 t),
 'coverage',(select coalesce(jsonb_agg(to_jsonb(t) order by calendar_row_id),'[]'::jsonb) from public.exact_byte_history_coverage_20261010 t),
 'observations',(select coalesce(jsonb_agg(to_jsonb(t) order by observation_id),'[]'::jsonb) from public.exact_byte_history_observation_20261010 t)
 )::text,'UTF8')),'hex'); $$;

create function public.exact_byte_historical_snapshot_20261010() returns text
language sql security definer set search_path=pg_catalog,public as $$
 select 'sha256:'||encode(sha256(convert_to(coalesce(jsonb_agg(
  jsonb_build_object('row_id',c.id,'revision',public.exact_byte_row_revision_20261010(c)) order by c.id),
  '[]'::jsonb)::text,'UTF8')),'hex') from public.content_calendar c
 where c.status='published' or c.published_at is not null or c.late_post_id is not null; $$;

-- Complete, current coverage is required before activation; all historical
-- row media URLs are conservatively covered, including raw thumbnails.
create function public.exact_byte_history_complete_20261010() returns boolean
language sql security definer set search_path=pg_catalog,public as $$
 select not exists(select 1 from public.exact_byte_history_coverage_20261010 where not complete)
 and not exists(select 1 from public.content_calendar c where
 (c.status='published' or c.published_at is not null or c.late_post_id is not null)
 and not exists(select 1 from public.exact_byte_send_attempt_20261010 a
  where a.calendar_row_id=c.id and (
   a.row_revision=public.exact_byte_row_revision_20261010(c)
   -- A pre-connect GBP row can acquire the already-bound native location only
   -- in its terminal publish stamp. Compare every other frozen row field.
   or (c.status='published' and c.account='googlebusiness'
    and a.context->'row_snapshot'->'gbp_location_id'='null'::jsonb
    and nullif(a.provider_target->>'location_id','') is not null
    and to_jsonb(c)->>'gbp_location_id'=a.provider_target->>'location_id'
    and a.row_revision=public.exact_byte_row_revision_20261010(
     jsonb_populate_record(c,jsonb_build_object('gbp_location_id',null))))))
 and not exists(select 1 from public.exact_byte_history_coverage_20261010 h where
  h.calendar_row_id=c.id and h.row_revision=public.exact_byte_row_revision_20261010(c) and h.complete
  and not exists(select 1 from jsonb_each(to_jsonb(c)) e where e.key in
   ('image_urls','slide_urls','media_urls','carousel_urls','media_items','gbp_media','video_url')
   and e.value not in ('null'::jsonb,'[]'::jsonb,'""'::jsonb))
  and ((h.no_images_verified and nullif(btrim(c.image_url),'') is null
    and nullif(btrim(to_jsonb(c)->>'thumbnail_url'),'') is null)
   or (nullif(btrim(c.image_url),'') is not null and not exists(
    select 1 from unnest(array[c.image_url,to_jsonb(c)->>'thumbnail_url']) as u(url)
    where nullif(btrim(u.url),'') is not null and not exists(
     select 1 from public.exact_byte_history_observation_20261010 o
     where o.calendar_row_id=c.id and o.row_revision=h.row_revision and o.exact_url=u.url)))))); $$;

create function public.exact_byte_activate_20261010(
 p_cutover_id uuid,p_corpus_sha256 text,p_historical_snapshot_sha256 text,
 p_runtime_commit text,p_boundary_inventory_sha256 text,
 p_deployment_evidence_ref text,p_complete_history_evidence_ref text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public
set lock_timeout='5s' as $$
begin
 if current_setting('transaction_isolation') <> 'read committed' then
  raise exception 'read committed required' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('exact_byte_send_20261010',0));
 -- Freeze the historical corpus through validation + cutover. No other function
 -- acquires this table lock while holding calendar row locks.
 lock table public.content_calendar in share mode;
 if exists(select 1 from public.exact_byte_gate_20261010 where enabled)
  or public.exact_byte_corpus_digest_20261010() is distinct from p_corpus_sha256
  or public.exact_byte_historical_snapshot_20261010() is distinct from p_historical_snapshot_sha256
  or public.exact_byte_history_complete_20261010() is distinct from true
  or not exists(select 1 from public.exact_byte_tenant_20261010)
  or not exists(select 1 from public.exact_byte_target_20261010)
  or exists(select 1 from public.exact_byte_tenant_20261010 t where not exists(
    select 1 from pg_timezone_names n where n.name=t.posting_timezone))
 then return false; end if;
 insert into public.exact_byte_cutover_20261010(cutover_id,corpus_sha256,historical_snapshot_sha256,
 runtime_commit,boundary_inventory_sha256,deployment_evidence_ref,complete_history_evidence_ref)
 values(p_cutover_id,p_corpus_sha256,p_historical_snapshot_sha256,p_runtime_commit,
 p_boundary_inventory_sha256,p_deployment_evidence_ref,p_complete_history_evidence_ref);
 update public.exact_byte_gate_20261010 set enabled=true,active_cutover_id=p_cutover_id where singleton;
 return true;
end; $$;
create function public.exact_byte_disable_20261010() returns boolean
language plpgsql security definer set search_path=pg_catalog,public set lock_timeout='5s' as $$
begin
 perform pg_advisory_xact_lock(hashtextextended('exact_byte_send_20261010',0));
 update public.exact_byte_gate_20261010 set enabled=false where singleton;
 return found;
end; $$;

-- Internal validated snapshot. Service RPC does not accept tenant/date/target
-- overrides. Exact matching persisted authority is mandatory; no suffix guess,
-- cached mapping, global timezone, server UTC date or inferred sibling identity.
create function public.exact_byte_context_locked_20261010(p_calendar_row_id uuid,p_claim_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public set TimeZone='UTC' as $$
declare c public.content_calendar%rowtype; t public.exact_byte_tenant_20261010%rowtype;
 route public.exact_byte_target_20261010%rowtype; gate public.exact_byte_gate_20261010%rowtype;
 cutover public.exact_byte_cutover_20261010%rowtype; rev text; grp uuid; images jsonb:='[]';
 fld text; u text; ordinal integer:=0; j jsonb;
begin
 select * into gate from public.exact_byte_gate_20261010 where singleton;
 if not found then raise exception 'exact-byte gate unavailable' using errcode='23514'; end if;
 if gate.enabled is not true then return jsonb_build_object('enabled',false); end if;
 select * into cutover from public.exact_byte_cutover_20261010 where cutover_id=gate.active_cutover_id;
 if not found or cutover.corpus_sha256 is distinct from public.exact_byte_corpus_digest_20261010()
  or public.exact_byte_history_complete_20261010() is distinct from true then
  raise exception 'frozen exact-byte corpus drift' using errcode='23514'; end if;
 select * into c from public.content_calendar where id=p_calendar_row_id for update;
 if not found or p_claim_token is null or c.publish_claim_token is distinct from p_claim_token
  or c.status is distinct from 'publishing' or c.variant_status is distinct from 'active'
  or c.published_at is not null or c.late_post_id is not null
  or c.post_date is null or c.publish_reservation_day is null then
  raise exception 'current owned calendar claim required' using errcode='23514'; end if;
 select * into t from public.exact_byte_tenant_20261010 where calendar_gym_id=c.gym_id;
 if not found or not exists(select 1 from pg_timezone_names n where n.name=t.posting_timezone)
  or c.publish_reservation_day is distinct from (clock_timestamp() at time zone t.posting_timezone)::date then
  raise exception 'canonical tenant/local reservation unavailable' using errcode='23514'; end if;
 select * into route from public.exact_byte_target_20261010 where calendar_gym_id=c.gym_id
  and account=c.account and format=c.format;
 if not found then raise exception 'audited provider target unavailable' using errcode='23514'; end if;
 j:=to_jsonb(c);
 if c.account='googlebusiness' and (
  jsonb_typeof(route.provider_target->'location_id') is distinct from 'string'
  or nullif(btrim(route.provider_target->>'location_id'),'') is null
  or (j->>'gbp_location_id' is not null
   and j->>'gbp_location_id' is distinct from route.provider_target->>'location_id')) then
  raise exception 'audited GBP location unavailable or changed' using errcode='23514'; end if;
 -- This narrow adapter supports images only. Nonempty alternative payload
 -- fields cannot be silently ignored in the complete outbound set.
 if exists(select 1 from jsonb_each(j) e where e.key in
  ('image_urls','slide_urls','media_urls','carousel_urls','media_items','gbp_media','video_url')
  and e.value not in ('null'::jsonb,'[]'::jsonb,'""'::jsonb))
  or coalesce(c.image_url,'') ~* '\.(mp4|mov|m4v|webm|avi)([?#]|$)' then
  raise exception 'unsupported complete outbound image shape' using errcode='23514'; end if;
 foreach fld in array route.image_fields loop
  u:=j->>fld;
  if fld='image_url' and nullif(btrim(u),'') is null then
   raise exception 'outbound image missing' using errcode='23514'; end if;
  if u is not null then
   if u !~ '^https://[^[:space:]]+$' then raise exception 'outbound URL invalid' using errcode='23514'; end if;
   images:=images||jsonb_build_array(jsonb_build_object('ordinal',ordinal,
    'role',case when fld='image_url' then 'image' else 'thumbnail' end,'url',u));
   ordinal:=ordinal+1;
  end if;
 end loop;
 rev:=public.exact_byte_row_revision_20261010(c);
 select s.shared_posting_identity into grp from public.exact_byte_sibling_member_20261010 m
 join public.exact_byte_sibling_proof_20261010 s using(shared_posting_identity)
 where m.calendar_row_id=c.id and m.row_revision=rev and s.canonical_tenant=t.canonical_tenant
  and s.post_date=c.post_date and s.logical_post_id::text=j->>'logical_post_id';
 return jsonb_build_object('enabled',true,'calendar_row_id',c.id,'row_revision',rev,
  'canonical_tenant',t.canonical_tenant,'calendar_gym_id',c.gym_id,'post_date',c.post_date,
  'account',c.account,'format',c.format,'row_snapshot',j-array[
    'status','publish_claim_token','publish_reservation_day','published_at',
    'late_post_id','updated_at','scheduled_at','reject_reason'],
  'reservation_day',c.publish_reservation_day,'posting_timezone',t.posting_timezone,
  'publish_claim_token',p_claim_token,'provider_target',route.provider_target,'images',images,
  'shared_posting_identity',grp,'corpus_sha256',cutover.corpus_sha256,'cutover_id',cutover.cutover_id);
end; $$;

create function public.exact_byte_send_context_20261010(p_calendar_row_id uuid,p_claim_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public set lock_timeout='5s' as $$
begin
 if current_setting('transaction_isolation') <> 'read committed' then
  raise exception 'read committed required' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('exact_byte_send_20261010',0));
 return public.exact_byte_context_locked_20261010(p_calendar_row_id,p_claim_token);
end; $$;

create function public.exact_byte_authorize_send_20261010(
 p_calendar_row_id uuid,p_claim_token uuid,p_expected_context jsonb,p_images jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public set lock_timeout='5s' as $$
declare ctx jsonb; image jsonb; expected jsonb; i integer; a uuid:=gen_random_uuid(); grp uuid;
begin
 if current_setting('transaction_isolation') <> 'read committed' then
  raise exception 'read committed required' using errcode='25000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('exact_byte_send_20261010',0));
 ctx:=public.exact_byte_context_locked_20261010(p_calendar_row_id,p_claim_token);
 if ctx->'enabled' is distinct from 'true'::jsonb or ctx is distinct from p_expected_context then
  return jsonb_build_object('authorized',false,'reason','current_context_unavailable'); end if;
 if jsonb_typeof(p_images) is distinct from 'array'
  or jsonb_array_length(p_images)<>jsonb_array_length(ctx->'images') then
  return jsonb_build_object('authorized',false,'reason','complete_images_required'); end if;
 grp:=nullif(ctx->>'shared_posting_identity','')::uuid;
 for i in 0..jsonb_array_length(p_images)-1 loop
  image:=p_images->i; expected:=ctx->'images'->i;
  if jsonb_typeof(image) is distinct from 'object' or image-array['sha256','byte_length'] is distinct from expected
   or jsonb_typeof(image->'sha256') is distinct from 'string'
   or (image->>'sha256') !~ '^sha256:[0-9a-f]{64}$'
   or jsonb_typeof(image->'byte_length') is distinct from 'number'
   or (image->>'byte_length') !~ '^[0-9]{1,9}$'
   or (image->>'byte_length')::bigint not between 1 and 134217728 then
   return jsonb_build_object('authorized',false,'reason','exact_byte_observation_invalid'); end if;
  -- ALL historic occupants must independently prove this exact same logical
  -- posting identity/tenant/calendar date. Unknown context NEVER equals known.
  if exists(select 1 from public.exact_byte_history_observation_20261010 o where o.sha256=image->>'sha256'
   and (grp is null or o.calendar_row_id is not distinct from p_calendar_row_id
    or o.byte_length is distinct from (image->>'byte_length')::bigint
    or o.shared_posting_identity is distinct from grp
    or o.canonical_tenant is distinct from ctx->>'canonical_tenant'
    or o.post_date is distinct from (ctx->>'post_date')::date
    or o.observation_kind='uncertain_bytes'
    or not exists(select 1 from public.exact_byte_sibling_member_20261010 m
      where m.calendar_row_id=o.calendar_row_id and m.row_revision=o.row_revision
       and m.shared_posting_identity=grp)))
   or exists(select 1 from public.exact_byte_attempt_image_20261010 b
    join public.exact_byte_send_attempt_20261010 old using(attempt_id)
    where b.sha256=image->>'sha256' and (grp is null
     or old.calendar_row_id is not distinct from p_calendar_row_id
     or b.byte_length is distinct from (image->>'byte_length')::bigint
     or old.shared_posting_identity is distinct from grp
     or old.canonical_tenant is distinct from ctx->>'canonical_tenant'
     or old.post_date is distinct from (ctx->>'post_date')::date
     -- Same-day sibling authority never consumes an unresolved reservation.
     -- Only the latest evidenced provider acceptance/readback permits it.
     or coalesce((select o.outcome from public.exact_byte_outcome_20261010 o
       where o.attempt_id=old.attempt_id order by o.recorded_at desc,o.outcome_id desc limit 1),'uncertain')
       not in ('provider_accepted','platform_verified'))) then
   return jsonb_build_object('authorized',false,'reason','historical_or_reserved_digest'); end if;
 end loop;
 if exists(select 1 from public.exact_byte_send_attempt_20261010 old
  where old.claim_token=p_claim_token or (old.calendar_row_id=p_calendar_row_id and old.provider_target=ctx->'provider_target')) then
  return jsonb_build_object('authorized',false,'reason','attempt_already_reserved'); end if;
 insert into public.exact_byte_send_attempt_20261010(attempt_id,calendar_row_id,claim_token,row_revision,
  canonical_tenant,post_date,reservation_day,shared_posting_identity,provider_target,context,cutover_id)
 values(a,p_calendar_row_id,p_claim_token,ctx->>'row_revision',ctx->>'canonical_tenant',
  (ctx->>'post_date')::date,(ctx->>'reservation_day')::date,grp,ctx->'provider_target',ctx,(ctx->>'cutover_id')::uuid);
 for image in select value from jsonb_array_elements(p_images) loop
  insert into public.exact_byte_attempt_image_20261010(attempt_id,ordinal,role,exact_url,sha256,byte_length)
   values(a,(image->>'ordinal')::integer,image->>'role',image->>'url',image->>'sha256',(image->>'byte_length')::bigint);
 end loop;
 insert into public.exact_byte_outcome_20261010(outcome_id,attempt_id,outcome,evidence_ref)
  values(gen_random_uuid(),a,'reserved_uncertain','committed-send-grant:'||a::text);
 -- An actual tuple write defeats stale REPEATABLE READ editor snapshots;
 -- calendar guard also refuses non-read-committed writes after reservation.
 update public.content_calendar set status=status where id=p_calendar_row_id;
 return jsonb_build_object('authorized',true,'reason','reserved','attempt_id',a);
end; $$;

create function public.exact_byte_record_outcome_20261010(
 p_attempt_id uuid,p_claim_token uuid,p_outcome text,p_evidence_ref text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public set lock_timeout='5s' as $$
begin
 if p_outcome not in ('uncertain','definite_no_send','provider_accepted','platform_verified')
  or nullif(btrim(p_evidence_ref),'') is null or not exists(
   select 1 from public.exact_byte_send_attempt_20261010 where attempt_id=p_attempt_id and claim_token=p_claim_token)
 then return false; end if;
 insert into public.exact_byte_outcome_20261010(outcome_id,attempt_id,outcome,evidence_ref)
 values(gen_random_uuid(),p_attempt_id,p_outcome,p_evidence_ref);
 return true;
end; $$;
create function public.exact_byte_attempt_read_20261010(p_attempt_id uuid,p_claim_token uuid)
returns jsonb language sql security definer set search_path=pg_catalog,public as $$
 select to_jsonb(a)||jsonb_build_object('images',(select jsonb_agg(to_jsonb(i) order by ordinal)
  from public.exact_byte_attempt_image_20261010 i where i.attempt_id=a.attempt_id),
  'outcomes',(select jsonb_agg(to_jsonb(o) order by recorded_at,outcome_id)
  from public.exact_byte_outcome_20261010 o where o.attempt_id=a.attempt_id))
 from public.exact_byte_send_attempt_20261010 a where a.attempt_id=p_attempt_id and a.claim_token=p_claim_token; $$;

-- A committed uncertain send cannot be recycled into pending/approved, deleted,
-- or edited. Token-CAS terminal writes remain possible. These restrictions
-- continue after flag disable; disabling cannot erase a possibly sent attempt.
create function public.exact_byte_calendar_guard_20261010() returns trigger
language plpgsql security definer set search_path=pg_catalog,public as $$
declare reserved boolean; compared_new public.content_calendar%rowtype;
begin
 select exists(select 1 from public.exact_byte_send_attempt_20261010 where calendar_row_id=old.id) into reserved;
 if not reserved then if tg_op='DELETE' then return old; else return new; end if; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'exact-byte attempted row requires read committed' using errcode='25000'; end if;
 if not pg_try_advisory_xact_lock(hashtextextended('exact_byte_send_20261010',0)) then
  raise exception 'exact-byte authority busy' using errcode='55P03'; end if;
 if tg_op='DELETE' then raise exception 'exact-byte attempted row requires reconciliation' using errcode='23514'; end if;
 compared_new:=new;
 -- Keep native location in the approved revision. The sole exception is the
 -- existing GBP terminal stamp from NULL to the immutable grant's destination.
 -- Neither a pre-send edit nor failure/status-only update can change it.
 if to_jsonb(new)->'gbp_location_id' is distinct from to_jsonb(old)->'gbp_location_id'
  and old.status='publishing' and new.status='published'
  and old.account='googlebusiness' and to_jsonb(old)->'gbp_location_id'='null'::jsonb
  and exists(select 1 from public.exact_byte_send_attempt_20261010 a
   where a.calendar_row_id=old.id and a.claim_token=old.publish_claim_token
    and a.row_revision=public.exact_byte_row_revision_20261010(old)
    and a.context->'row_snapshot'->'gbp_location_id'='null'::jsonb
    and nullif(a.provider_target->>'location_id','') is not null
    and a.provider_target->>'location_id'=to_jsonb(new)->>'gbp_location_id') then
  compared_new:=jsonb_populate_record(new,jsonb_build_object('gbp_location_id',null));
 end if;
 if public.exact_byte_row_revision_20261010(compared_new) is distinct from public.exact_byte_row_revision_20261010(old)
  or new.status is null or new.status not in ('publishing','published','failed')
  or (new.status='publishing' and (new.publish_claim_token is distinct from old.publish_claim_token
    or new.publish_reservation_day is distinct from old.publish_reservation_day))
  or (new.status in ('published','failed') and new.publish_claim_token is not null)
  or (old.status in ('published','failed') and new.status='publishing') then
  raise exception 'exact-byte attempted creative/lease cannot be revoked' using errcode='23514'; end if;
 return new;
end; $$;
create trigger exact_byte_calendar_guard_20261010 before update or delete on public.content_calendar
 for each row execute function public.exact_byte_calendar_guard_20261010();
create function public.exact_byte_calendar_truncate_guard_20261010() returns trigger
language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if exists(select 1 from public.exact_byte_send_attempt_20261010)
  or exists(select 1 from public.exact_byte_gate_20261010 where enabled) then
  raise exception 'exact-byte corpus/attempt history forbids calendar truncate' using errcode='23514'; end if;
 return null;
end; $$;
create trigger exact_byte_calendar_truncate_guard_20261010 before truncate on public.content_calendar
 for each statement execute function public.exact_byte_calendar_truncate_guard_20261010();

-- Explicit signatures avoid PostgREST overload ambiguity and public-default ACLs.
do $$ declare f record; begin
 for f in select p.oid::regprocedure as identity from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname like 'exact_byte_%20261010' loop
  execute format('revoke all on function %s from public,anon,authenticated,service_role,exact_byte_owner_20261010',f.identity);
 end loop;
end; $$;
grant execute on function public.exact_byte_send_context_20261010(uuid,uuid),
 public.exact_byte_authorize_send_20261010(uuid,uuid,jsonb,jsonb),
 public.exact_byte_record_outcome_20261010(uuid,uuid,text,text),
 public.exact_byte_attempt_read_20261010(uuid,uuid) to service_role;
grant execute on function public.exact_byte_row_revision_20261010(public.content_calendar),
 public.exact_byte_corpus_digest_20261010(),public.exact_byte_historical_snapshot_20261010(),
 public.exact_byte_history_complete_20261010(),
 public.exact_byte_activate_20261010(uuid,text,text,text,text,text,text),
 public.exact_byte_disable_20261010() to exact_byte_owner_20261010;
commit;
