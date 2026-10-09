-- DRAFT / UNAPPLIED / UNWIRED. Shared PostgreSQL queue + dispatch ledger for
-- REQUIRES PostgreSQL 17+: submit uses the IS JSON ... WITH UNIQUE KEYS
-- predicate (native duplicate-key rejection), unavailable on earlier majors.
-- the frozen generated hosted-byte issuer API. Producers submit immutable
-- original manifest bytes and read their own committed result only; dedicated
-- issuers read pending requests, claim dispatch keys and commit receipts.
-- SQL authenticates session_user plus an admin-provisioned tenant grant on
-- every call; a tenant value inside request content is never trusted alone.
begin;
do $$
begin
 if current_user <> 'postgres' then
  raise exception 'issuer dispatch migration must be applied by the database owner' using errcode='23514'; end if;
 -- Commit cross-verifies receipts against the committed hosted-byte
 -- authority, so the authority migration must already be applied.
 if to_regclass('public.generated_hosted_byte_receipts_20261009') is null
  or to_regclass('public.generated_hosted_byte_principals_20261009') is null
  or to_regclass('public.calendar_generated_artifact_versions') is null then
  raise exception 'hosted byte authority migration must be applied first' using errcode='23514'; end if;
end $$;
do $$ begin
 if not exists(select 1 from pg_roles where rolname='generated_issuer_dispatch_producer_20261009') then
  create role generated_issuer_dispatch_producer_20261009 nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
 end if;
 if not exists(select 1 from pg_roles where rolname='generated_issuer_dispatch_issuer_20261009') then
  create role generated_issuer_dispatch_issuer_20261009 nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
 end if;
 if exists(select 1 from pg_roles where rolname in ('generated_issuer_dispatch_producer_20261009','generated_issuer_dispatch_issuer_20261009')
  and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole or rolbypassrls or rolinherit)) then
  raise exception 'dedicated dispatch roles must be unprivileged NOLOGIN' using errcode='23514'; end if;
 if exists(select 1 from pg_roles p where p.rolname in ('anon','authenticated','service_role') and
  (pg_has_role(p.rolname,'generated_issuer_dispatch_producer_20261009','MEMBER')
   or pg_has_role(p.rolname,'generated_issuer_dispatch_issuer_20261009','MEMBER'))) then
  raise exception 'producer/service roles cannot inherit dedicated dispatch roles' using errcode='23514'; end if;
end $$;
grant usage on schema public to generated_issuer_dispatch_producer_20261009,generated_issuer_dispatch_issuer_20261009;

-- Admin-only grant dictionary. Authorization always joins the authenticated
-- session_user to a provisioned tenant grant; request content cannot grant.
create table public.generated_issuer_dispatch_principals_20261009 (
 principal name not null, gym_id text not null check(btrim(gym_id)<>'' and length(gym_id)<=128),
 can_submit boolean not null default false, can_dispatch boolean not null default false,
 primary key(principal,gym_id)
);

-- Immutable original request bytes plus tenant/version/binding. safe_status
-- and receipt_id are NULL/'submitted' until the issuer commits a receipt.
create table public.generated_issuer_dispatch_requests_20261009 (
 id uuid primary key default gen_random_uuid(),
 dispatch_key text not null unique check(dispatch_key ~ '^[0-9a-f]{64}$'),
 gym_id text not null check(btrim(gym_id)<>'' and length(gym_id)<=128),
 artifact_version_id uuid not null,
 binding_digest text not null check(binding_digest ~ '^[0-9a-f]{64}$'),
 hosted_url text not null check(length(hosted_url)<=2048),
 expected_sha256 text not null check(expected_sha256 ~ '^[0-9a-f]{64}$'),
 manifest_sha256 text not null check(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 manifest_bytes bytea not null check(octet_length(manifest_bytes) between 1 and 65536),
 safe_status text not null default 'submitted' check(safe_status in ('submitted','issued')),
 receipt_id uuid,
 submitted_by name not null, submitted_at timestamptz not null default clock_timestamp(),
 unique(gym_id,artifact_version_id)
);

-- Durable dispatch ledger. One row per dispatch key, inserted by the atomic
-- claim and committed before claim returns is_new. receipt_id stays NULL
-- until commit; a permanently NULL row means reconcile, never re-issue.
create table public.generated_issuer_dispatch_ledger_20261009 (
 dispatch_key text primary key check(dispatch_key ~ '^[0-9a-f]{64}$'),
 gym_id text not null check(btrim(gym_id)<>'' and length(gym_id)<=128),
 artifact_version_id uuid not null,
 binding_digest text not null check(binding_digest ~ '^[0-9a-f]{64}$'),
 receipt_id uuid,
 claimed_by name not null, claimed_at timestamptz not null default clock_timestamp(),
 committed_at timestamptz
);

-- Durable issuer-only quarantine for queued rows SQL accepted but the frozen
-- Python request validation rejects (second line of defense; submit-time
-- rejections stay). Keyed by exact (dispatch_key,binding_digest), immutable,
-- and excluded from pending so one poison row can never abort or starve the
-- queue. Never issued, never resets the ledger, never touches authority.
create table public.generated_issuer_dispatch_quarantine_20261009 (
 dispatch_key text not null check(dispatch_key ~ '^[0-9a-f]{64}$'),
 binding_digest text not null check(binding_digest ~ '^[0-9a-f]{64}$'),
 gym_id text not null check(btrim(gym_id)<>'' and length(gym_id)<=128),
 artifact_version_id uuid not null,
 safe_code text not null check(safe_code ~ '^[a-z0-9_]{1,64}$'),
 quarantined_by name not null, quarantined_at timestamptz not null default clock_timestamp(),
 primary key(dispatch_key,binding_digest)
);

alter table public.generated_issuer_dispatch_principals_20261009 enable row level security;
alter table public.generated_issuer_dispatch_requests_20261009 enable row level security;
alter table public.generated_issuer_dispatch_ledger_20261009 enable row level security;
alter table public.generated_issuer_dispatch_quarantine_20261009 enable row level security;
revoke all on public.generated_issuer_dispatch_principals_20261009,
 public.generated_issuer_dispatch_requests_20261009,
 public.generated_issuer_dispatch_ledger_20261009,
 public.generated_issuer_dispatch_quarantine_20261009
 from public,anon,authenticated,service_role,
 generated_issuer_dispatch_producer_20261009,generated_issuer_dispatch_issuer_20261009;

-- Rows are immutable except the two guarded status transitions the issuer
-- commit performs through the security-definer functions below.
create function public.generated_issuer_dispatch_request_guard_20261009() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin
 if TG_OP='UPDATE' and OLD.safe_status='submitted' and NEW.safe_status='issued'
  and OLD.receipt_id is null and NEW.receipt_id is not null
  and row(OLD.id,OLD.dispatch_key,OLD.gym_id,OLD.artifact_version_id,OLD.binding_digest,OLD.hosted_url,
   OLD.expected_sha256,OLD.manifest_sha256,OLD.manifest_bytes,OLD.submitted_by,OLD.submitted_at)
   is not distinct from row(NEW.id,NEW.dispatch_key,NEW.gym_id,NEW.artifact_version_id,NEW.binding_digest,
   NEW.hosted_url,NEW.expected_sha256,NEW.manifest_sha256,NEW.manifest_bytes,NEW.submitted_by,NEW.submitted_at) then
  return NEW; end if;
 raise exception 'issuer dispatch requests are immutable' using errcode='23514';
end $$;
create trigger generated_issuer_dispatch_request_guard before update or delete
 on public.generated_issuer_dispatch_requests_20261009
 for each row execute function public.generated_issuer_dispatch_request_guard_20261009();
create trigger generated_issuer_dispatch_request_no_truncate before truncate
 on public.generated_issuer_dispatch_requests_20261009
 for each statement execute function public.generated_issuer_dispatch_request_guard_20261009();

create function public.generated_issuer_dispatch_ledger_guard_20261009() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin
 if TG_OP='UPDATE' and OLD.receipt_id is null and NEW.receipt_id is not null
  and row(OLD.dispatch_key,OLD.gym_id,OLD.artifact_version_id,OLD.binding_digest,OLD.claimed_by,OLD.claimed_at)
   is not distinct from row(NEW.dispatch_key,NEW.gym_id,NEW.artifact_version_id,NEW.binding_digest,
   NEW.claimed_by,NEW.claimed_at) then
  return NEW; end if;
 raise exception 'issuer dispatch ledger rows are immutable' using errcode='23514';
end $$;
create trigger generated_issuer_dispatch_ledger_guard before update or delete
 on public.generated_issuer_dispatch_ledger_20261009
 for each row execute function public.generated_issuer_dispatch_ledger_guard_20261009();
create trigger generated_issuer_dispatch_ledger_no_truncate before truncate
 on public.generated_issuer_dispatch_ledger_20261009
 for each statement execute function public.generated_issuer_dispatch_ledger_guard_20261009();

-- Quarantine rows are immutable: they record a permanent poison verdict.
create function public.generated_issuer_dispatch_quarantine_guard_20261009() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin
 raise exception 'issuer dispatch quarantine rows are immutable' using errcode='23514';
end $$;
create trigger generated_issuer_dispatch_quarantine_guard before update or delete
 on public.generated_issuer_dispatch_quarantine_20261009
 for each row execute function public.generated_issuer_dispatch_quarantine_guard_20261009();
create trigger generated_issuer_dispatch_quarantine_no_truncate before truncate
 on public.generated_issuer_dispatch_quarantine_20261009
 for each statement execute function public.generated_issuer_dispatch_quarantine_guard_20261009();

create function public.generated_issuer_dispatch_authorized_20261009(p_gym text,p_purpose text) returns boolean
language sql stable security definer set search_path=pg_catalog,public as $$
 select coalesce(exists(select 1 from public.generated_issuer_dispatch_principals_20261009 g
  where g.principal=session_user and g.gym_id=p_gym and (
   (p_purpose in ('submit','result') and g.can_submit and
    pg_has_role(session_user,'generated_issuer_dispatch_producer_20261009','MEMBER'))
   or (p_purpose='dispatch' and g.can_dispatch and
    pg_has_role(session_user,'generated_issuer_dispatch_issuer_20261009','MEMBER')))),false);
$$;

-- Producer submit. Stores the immutable original manifest bytes exactly once
-- per dispatch key; an identical replay returns the committed row and the
-- same tenant+version with ANY changed binding is a conflict, never a row.
create function public.generated_issuer_dispatch_submit_20261009(
 p_gym text,p_version uuid,p_url text,p_expected_sha text,p_manifest_sha text,
 p_manifest_bytes bytea,p_binding text,p_key text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare m jsonb; existing public.generated_issuer_dispatch_requests_20261009%rowtype; allowed boolean;
begin
 if not public.generated_issuer_dispatch_authorized_20261009(p_gym,'submit') then
  raise exception 'dedicated tenant producer required' using errcode='42501'; end if;
 -- Serialize principal-grant revocation with the whole submit transaction.
 select can_submit into allowed from public.generated_issuer_dispatch_principals_20261009
  where principal=session_user and gym_id=p_gym for share;
 if not coalesce(allowed,false) then
  raise exception 'dedicated tenant producer required' using errcode='42501'; end if;
 if p_version is null or p_url is null or length(p_url)>2048
  -- Mirror the frozen Python _url() rules exactly: one canonical HTTPS
  -- spelling, printable ASCII only, no credentials/port/query/fragment, no
  -- backslash, no '%', no empty or dot path segments. The base regex alone
  -- is weaker than _url(); the extra predicates close that gap so a poison
  -- row is rejected at submit and never stored then read back.
  or p_url !~ '^https://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?/[^[:space:]?#]*$'
  or p_url ~ '[^\x21-\x7e]' or position(chr(92) in p_url)>0 or position('%' in p_url)>0
  or substring(p_url from 9) like '%//%'
  or exists(select 1 from unnest(string_to_array(substring(p_url from 9),'/')) s(seg)
   where seg in ('.','..'))
  or p_expected_sha is null or p_expected_sha !~ '^[0-9a-f]{64}$'
  or p_manifest_sha is null or p_manifest_sha !~ '^[0-9a-f]{64}$'
  or p_binding is null or p_binding !~ '^[0-9a-f]{64}$'
  or p_key is null or p_key !~ '^[0-9a-f]{64}$'
  or p_manifest_bytes is null or octet_length(p_manifest_bytes) not between 1 and 65536
  or encode(sha256(p_manifest_bytes),'hex') is distinct from p_manifest_sha
  -- The dispatch key derivation is fixed in SQL; a caller cannot substitute
  -- a key that binds another tenant or version.
  or p_key is distinct from encode(sha256(convert_to(
   'echo-generated-hosted-byte-dispatch-20261009:'||p_gym||':'||p_version::text,'UTF8')),'hex')
  -- The canonical binding digest is recomputed in SQL exactly as the frozen
  -- Python IssuerDispatchRequest.binding_digest(): sha256 of the JSON object
  -- with sorted keys and (',',':') separators over hosted_url,
  -- expected_sha256 and manifest_sha256. to_json supplies the identical
  -- string escaping for the printable-ASCII URL space _url() allows, so a
  -- caller-supplied binding that differs in any field is rejected.
  or p_binding is distinct from encode(sha256(convert_to(
   '{"expected_sha256":'||to_json(p_expected_sha)::text||',"hosted_url":'||to_json(p_url)::text
   ||',"manifest_sha256":'||to_json(p_manifest_sha)::text||'}','UTF8')),'hex') then
  raise exception 'exact manifest digest, dispatch key and binding digest required' using errcode='23514'; end if;
 -- Native PG17 guard: reject duplicate object keys at any depth (the jsonb
 -- cast below would silently collapse them, while frozen Python validation
 -- refuses them), malformed JSON and non-object roots, fail closed with a
 -- static code. Oversized numerics such as 1e400 pass by design; the durable
 -- quarantine is the handler for anything SQL accepts but Python rejects.
 if (convert_from(p_manifest_bytes,'UTF8') is json object with unique keys) is not true then
  raise exception 'manifest must be a JSON object with unique keys' using errcode='23514'; end if;
 m:=convert_from(p_manifest_bytes,'UTF8')::jsonb;
 if jsonb_typeof(m) is distinct from 'object' or m->>'gym_id' is distinct from p_gym
  or m->>'artifact_version_id' is distinct from p_version::text
  or m->>'hosted_url' is distinct from p_url or m->>'delivered_sha256' is distinct from p_expected_sha then
  raise exception 'manifest identity binding required' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtextextended(p_key,20261009));
 select * into existing from public.generated_issuer_dispatch_requests_20261009 where dispatch_key=p_key;
 if found then
  if row(existing.gym_id,existing.artifact_version_id,existing.binding_digest,existing.hosted_url,
   existing.expected_sha256,existing.manifest_sha256,existing.manifest_bytes)
   is distinct from row(p_gym,p_version,p_binding,p_url,p_expected_sha,p_manifest_sha,p_manifest_bytes) then
   raise exception 'issuer dispatch binding conflict' using errcode='23514'; end if;
  return jsonb_build_object('dispatch_key',existing.dispatch_key,'safe_status',existing.safe_status,
   'receipt_id',existing.receipt_id);
 end if;
 insert into public.generated_issuer_dispatch_requests_20261009(
  dispatch_key,gym_id,artifact_version_id,binding_digest,hosted_url,expected_sha256,
  manifest_sha256,manifest_bytes,submitted_by)
 values(p_key,p_gym,p_version,p_binding,p_url,p_expected_sha,p_manifest_sha,p_manifest_bytes,session_user);
 return jsonb_build_object('dispatch_key',p_key,'safe_status','submitted','receipt_id',null);
end $$;

-- Producer read of its own committed result: exact safe status and receipt
-- UUID only, nothing else. An absent or foreign binding returns NULL.
create function public.generated_issuer_dispatch_result_20261009(
 p_gym text,p_version uuid,p_binding text)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare result jsonb;
begin
 if not public.generated_issuer_dispatch_authorized_20261009(p_gym,'result') then
  raise exception 'dedicated tenant producer required' using errcode='42501'; end if;
 select jsonb_build_object('safe_status',r.safe_status,'receipt_id',r.receipt_id) into result
  from public.generated_issuer_dispatch_requests_20261009 r
  where r.gym_id=p_gym and r.artifact_version_id=p_version and r.binding_digest=p_binding
   and r.dispatch_key=encode(sha256(convert_to(
    'echo-generated-hosted-byte-dispatch-20261009:'||p_gym||':'||p_version::text,'UTF8')),'hex');
 return result;
end $$;

-- Issuer pending read. Every requested tenant must be an authorized dispatch
-- grant for session_user; the queue is never enumerated across tenants. Rows
-- already issued (safe_status/receipt on the queue row, or a committed ledger
-- receipt for the same dispatch key) are never returned again.
create function public.generated_issuer_dispatch_pending_20261009(p_gyms text[],p_limit integer)
returns table(gym_id text,artifact_version_id uuid,hosted_url text,expected_sha256 text,manifest_bytes bytea)
language plpgsql stable security definer set search_path=pg_catalog,public as $$
begin
 if p_gyms is null or cardinality(p_gyms)=0 or p_limit is null or p_limit not between 1 and 1000 then
  raise exception 'tenant list and bounded limit required' using errcode='23514'; end if;
 if exists(select 1 from unnest(p_gyms) g
  where g is null or not public.generated_issuer_dispatch_authorized_20261009(g,'dispatch')) then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 return query select r.gym_id,r.artifact_version_id,r.hosted_url,r.expected_sha256,r.manifest_bytes
  from public.generated_issuer_dispatch_requests_20261009 r
  where r.gym_id=any(p_gyms) and r.safe_status='submitted' and r.receipt_id is null
   and not exists(select 1 from public.generated_issuer_dispatch_ledger_20261009 l
    where l.dispatch_key=r.dispatch_key and l.receipt_id is not null)
   -- Quarantined poison never reappears and never starves later valid work.
   and not exists(select 1 from public.generated_issuer_dispatch_quarantine_20261009 z
    where z.dispatch_key=r.dispatch_key and z.binding_digest=r.binding_digest)
  order by r.submitted_at,r.id limit p_limit;
end $$;

-- Issuer-only durable quarantine. Requires an exact immutable match of the
-- queued row: the dispatch key must exist and the caller-supplied binding
-- digest must equal the stored binding, which cryptographically commits to
-- the exact tenant, version, hosted URL, expected sha and stored manifest
-- bytes, so a compromised caller cannot quarantine arbitrary rows by
-- guessing keys. Idempotent for an identical repeat; never issues, never
-- resets the ledger, never touches authority state.
create function public.generated_issuer_dispatch_quarantine_20261009(
 p_key text,p_binding text,p_safe_code text)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.generated_issuer_dispatch_requests_20261009%rowtype;
begin
 if p_key is null or p_key !~ '^[0-9a-f]{64}$'
  or p_binding is null or p_binding !~ '^[0-9a-f]{64}$'
  or p_safe_code is null or p_safe_code !~ '^[a-z0-9_]{1,64}$' then
  raise exception 'dispatch key, binding digest and safe code required' using errcode='23514'; end if;
 select * into q from public.generated_issuer_dispatch_requests_20261009 where dispatch_key=p_key;
 if not found then
  raise exception 'no queued issuer dispatch matches the key' using errcode='P0002'; end if;
 if not public.generated_issuer_dispatch_authorized_20261009(q.gym_id,'dispatch') then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 -- Serialize principal-grant revocation with the quarantine transaction.
 if not exists(select 1 from public.generated_issuer_dispatch_principals_20261009
  where principal=session_user and gym_id=q.gym_id and can_dispatch for share) then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 -- Exact immutable match: the stored manifest digest and binding must agree
 -- with the caller claim, and the stored manifest sha must still match the
 -- stored bytes (rows are immutable, so this is belt-and-braces).
 if q.binding_digest is distinct from p_binding
  or encode(sha256(q.manifest_bytes),'hex') is distinct from q.manifest_sha256 then
  raise exception 'issuer dispatch quarantine binding mismatch' using errcode='23514'; end if;
 insert into public.generated_issuer_dispatch_quarantine_20261009(
  dispatch_key,binding_digest,gym_id,artifact_version_id,safe_code,quarantined_by)
 values(p_key,p_binding,q.gym_id,q.artifact_version_id,p_safe_code,session_user)
 on conflict (dispatch_key,binding_digest) do nothing;
 return true;
end $$;

-- Atomic cross-process claim. INSERT ... ON CONFLICT DO NOTHING inside one
-- short transaction: exactly one concurrent claimant inserts (is_new=true);
-- every other claimant observes the committed stored row (is_new=false with
-- the stored binding and receipt). A first claim whose binding differs from
-- the queued request raises and rolls back, so no row can be captured under
-- a forged binding. The stored row is returned unchanged on replay, which is
-- how the worker detects a same-key changed-binding conflict.
create function public.generated_issuer_dispatch_claim_20261009(p_key text,p_binding text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.generated_issuer_dispatch_requests_20261009%rowtype;
 l public.generated_issuer_dispatch_ledger_20261009%rowtype;
begin
 if p_key is null or p_key !~ '^[0-9a-f]{64}$' or p_binding is null or p_binding !~ '^[0-9a-f]{64}$' then
  raise exception 'dispatch key and binding digest required' using errcode='23514'; end if;
 select * into q from public.generated_issuer_dispatch_requests_20261009 where dispatch_key=p_key;
 if not found then
  raise exception 'no queued issuer dispatch matches the key' using errcode='P0002'; end if;
 if not public.generated_issuer_dispatch_authorized_20261009(q.gym_id,'dispatch') then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 -- Serialize principal-grant revocation with the claim transaction.
 if not exists(select 1 from public.generated_issuer_dispatch_principals_20261009
  where principal=session_user and gym_id=q.gym_id and can_dispatch for share) then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 insert into public.generated_issuer_dispatch_ledger_20261009(
  dispatch_key,gym_id,artifact_version_id,binding_digest,claimed_by)
 values(p_key,q.gym_id,q.artifact_version_id,p_binding,session_user)
 on conflict (dispatch_key) do nothing
 returning * into l;
 if found then
  if q.binding_digest is distinct from p_binding then
   raise exception 'issuer dispatch binding conflict' using errcode='23514'; end if;
  return jsonb_build_object('is_new',true,'binding_digest',p_binding,'receipt_id',null);
 end if;
 select * into l from public.generated_issuer_dispatch_ledger_20261009 where dispatch_key=p_key;
 return jsonb_build_object('is_new',false,'binding_digest',l.binding_digest,'receipt_id',l.receipt_id);
end $$;

-- Commit the receipt exactly once. Idempotent for the same receipt UUID;
-- rejects a missing key, a binding mismatch, or a different existing receipt.
-- The receipt UUID must be verifiable as an exact committed hosted-byte
-- authority receipt for this exact binding: same tenant, artifact version,
-- hosted URL, expected sha and manifest bytes, with the immutable portal
-- version row and delivery receipt agreeing. A fabricated or foreign receipt
-- can never commit.
create function public.generated_issuer_dispatch_commit_20261009(
 p_key text,p_binding text,p_receipt uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare l public.generated_issuer_dispatch_ledger_20261009%rowtype;
 q public.generated_issuer_dispatch_requests_20261009%rowtype;
begin
 if p_key is null or p_key !~ '^[0-9a-f]{64}$'
  or p_binding is null or p_binding !~ '^[0-9a-f]{64}$' or p_receipt is null then
  raise exception 'dispatch key, binding digest and receipt UUID required' using errcode='23514'; end if;
 select * into l from public.generated_issuer_dispatch_ledger_20261009
  where dispatch_key=p_key for update;
 if not found then
  raise exception 'no claimed issuer dispatch matches the key' using errcode='P0002'; end if;
 if not public.generated_issuer_dispatch_authorized_20261009(l.gym_id,'dispatch') then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 -- Serialize principal-grant revocation with the commit transaction.
 if not exists(select 1 from public.generated_issuer_dispatch_principals_20261009
  where principal=session_user and gym_id=l.gym_id and can_dispatch for share) then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 if l.binding_digest is distinct from p_binding then
  raise exception 'issuer dispatch binding conflict' using errcode='23514'; end if;
 if l.receipt_id is not null then
  if l.receipt_id=p_receipt then return true; end if;
  raise exception 'issuer dispatch receipt conflict' using errcode='23514'; end if;
 select * into q from public.generated_issuer_dispatch_requests_20261009 where dispatch_key=p_key;
 if not found then
  raise exception 'no queued issuer dispatch matches the key' using errcode='P0002'; end if;
 -- Exact committed-receipt verification against the hosted-byte authority:
 -- the receipt row, the immutable portal version row and the delivery
 -- receipt must all agree on the exact binding of this dispatch key.
 if l.gym_id is distinct from q.gym_id or l.artifact_version_id is distinct from q.artifact_version_id
  or not exists(select 1 from public.generated_hosted_byte_receipts_20261009 r
   join public.calendar_generated_artifact_versions v on v.id=r.artifact_version_id
   where r.receipt_id=p_receipt and r.gym_id=q.gym_id and r.artifact_version_id=q.artifact_version_id
    and r.hosted_url=q.hosted_url and r.delivered_sha256=q.expected_sha256
    and r.manifest_sha256=q.manifest_sha256 and r.manifest_bytes=q.manifest_bytes
    and row(v.gym_id,v.image_url,v.delivered_sha256,v.render_manifest_digest)
     is not distinct from row(r.gym_id,r.hosted_url,r.delivered_sha256,r.manifest_sha256)
    and v.delivery_receipt=jsonb_build_object('receipt_id',r.receipt_id,'gym_id',r.gym_id,
     'artifact_version_id',r.artifact_version_id,'hosted_url',r.hosted_url,
     'delivered_sha256',r.delivered_sha256,'render_manifest',convert_from(r.manifest_bytes,'UTF8')::jsonb)) then
  raise exception 'issuer dispatch receipt is not committed authority' using errcode='23514'; end if;
 update public.generated_issuer_dispatch_ledger_20261009
  set receipt_id=p_receipt,committed_at=clock_timestamp() where dispatch_key=p_key;
 update public.generated_issuer_dispatch_requests_20261009
  set safe_status='issued',receipt_id=p_receipt where dispatch_key=p_key;
 return true;
end $$;

revoke all on function public.generated_issuer_dispatch_request_guard_20261009(),
 public.generated_issuer_dispatch_ledger_guard_20261009(),
 public.generated_issuer_dispatch_quarantine_guard_20261009(),
 public.generated_issuer_dispatch_authorized_20261009(text,text),
 public.generated_issuer_dispatch_submit_20261009(text,uuid,text,text,text,bytea,text,text),
 public.generated_issuer_dispatch_result_20261009(text,uuid,text),
 public.generated_issuer_dispatch_pending_20261009(text[],integer),
 public.generated_issuer_dispatch_claim_20261009(text,text),
 public.generated_issuer_dispatch_commit_20261009(text,text,uuid),
 public.generated_issuer_dispatch_quarantine_20261009(text,text,text)
 from public,anon,authenticated,service_role,
 generated_issuer_dispatch_producer_20261009,generated_issuer_dispatch_issuer_20261009;
grant execute on function public.generated_issuer_dispatch_authorized_20261009(text,text)
 to generated_issuer_dispatch_producer_20261009,generated_issuer_dispatch_issuer_20261009;
grant execute on function public.generated_issuer_dispatch_submit_20261009(text,uuid,text,text,text,bytea,text,text),
 public.generated_issuer_dispatch_result_20261009(text,uuid,text)
 to generated_issuer_dispatch_producer_20261009;
grant execute on function public.generated_issuer_dispatch_pending_20261009(text[],integer),
 public.generated_issuer_dispatch_claim_20261009(text,text),
 public.generated_issuer_dispatch_commit_20261009(text,text,uuid),
 public.generated_issuer_dispatch_quarantine_20261009(text,text,text)
 to generated_issuer_dispatch_issuer_20261009;
commit;
