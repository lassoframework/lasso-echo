-- DRAFT / UNAPPLIED / DEFAULT OFF. Exact-byte protected media preparation
-- binding (20261010). Requires migrations/delivered_byte_send_fence_20261010.sql
-- to be applied first (role exact_byte_owner_20261010, guard functions, and
-- exact_byte_send_attempt_20261010 must already exist). This package does NOT
-- enable the fence, does NOT seed tenant/target authority, does NOT create or
-- demote approvals, does NOT send, and grants no new capability to any
-- service credential. It adds one owner-only read snapshot function and one
-- owner-only RPC that rebinds a reviewed, eligible content_calendar row's
-- image_url/thumbnail_url to operator-verified protected R2 objects under a
-- complete before-image compare-and-swap.
--
-- Safety contract (all enforced server-side by the RPC):
--   * READ COMMITTED + the single exact-byte authority advisory lock.
--   * Row locked FOR UPDATE; the caller's complete before image must equal
--     to_jsonb(row) exactly. Any changed approval, caption/content, tenant,
--     post_date, account/format target, source fields, or lease aborts.
--   * Refuses published/denied/killed rows, historical rows (published_at or
--     late_post_id set), any active claim/lease (publish_claim_token set),
--     rows with an existing exact-byte send attempt, and rows already
--     prepared (one terminal binding per row).
--   * Approved/provenance fail-closed (2026-10-10 independent-review ruling):
--     the deployed approval contract
--     (migrations/calendar_approval_provenance_20261005.sql) binds approval
--     provenance to the FINAL image_url. Rebinding image_url on ANY approved
--     or provenance-bearing row -- including legacy 'approved' rows whose
--     approval_digest is NULL -- would silently publish new bytes under a
--     stale approval. This RPC therefore works only on pending/unapproved
--     rows: it refuses any non-pending status or any recorded approval
--     provenance (digest/kind/actor/time); it never recomputes, rewrites, or
--     forges an approval. Prepared rows go back through the normal client
--     reapproval flow before they can be approved and sent.
--   * Complete outbound binding: when the reviewed row carries a thumbnail_url,
--     a new thumbnail_url AND its audited binding are REQUIRED (a mutable
--     leftover thumbnail is never accepted); when the row has none, no new
--     thumbnail may be introduced.
--   * Only image_url / thumbnail_url may change. Status, approvals, claim
--     fields and every other column are rewritten from the OLD row image;
--     after the UPDATE the persisted row is re-read and verified: every field
--     except image_url/thumbnail_url/updated_at (trigger-maintained) must be
--     identical to the locked before image, and the receipt records the exact
--     persisted before/after images, not a synthetic projection.
--   * Every binding is captured in an append-only before/after receipt.
--
-- Commit-outcome rule (client-side, enforced by scripts/exact_byte_prepare_media.py):
-- the RPC's COMMIT is the linearization point. A lost response or connection
-- failure at commit is an UNKNOWN outcome: abort the batch and reconcile from
-- exact_byte_media_prepare_20261010 before any retry. Never blind-retry.
--
-- Rollback BEFORE any use: drop the trigger-created dependencies, functions and
-- table in one gated migration. AFTER any use: receipts are append-only;
-- never DROP/DELETE/TRUNCATE them. Prepared rows remain subject to the fence
-- calendar guard once an attempt exists.
begin;

create table public.exact_byte_media_prepare_20261010 (
 prepare_id uuid primary key,
 calendar_row_id uuid not null unique,  -- one terminal preparation per row
 row_revision_before text not null check(row_revision_before ~ '^sha256:[0-9a-f]{64}$'),
 row_revision_after text not null check(row_revision_after ~ '^sha256:[0-9a-f]{64}$'),
 before_image jsonb not null check(jsonb_typeof(before_image)='object'),
 after_image jsonb not null check(jsonb_typeof(after_image)='object'),
 bindings jsonb not null check(jsonb_typeof(bindings)='array'),
 evidence_ref text not null check(btrim(evidence_ref)<>''),
 recorded_at timestamptz not null default clock_timestamp()
);
alter table public.exact_byte_media_prepare_20261010 enable row level security;
revoke all on public.exact_byte_media_prepare_20261010
 from public,anon,authenticated,service_role,exact_byte_owner_20261010;
create trigger immutable_row before update or delete on public.exact_byte_media_prepare_20261010
 for each row execute function public.exact_byte_immutable_20261010();
create trigger immutable_truncate before truncate on public.exact_byte_media_prepare_20261010
 for each statement execute function public.exact_byte_immutable_20261010();
create trigger seed_lock before insert on public.exact_byte_media_prepare_20261010
 for each row execute function public.exact_byte_seed_lock_20261010();
grant select on public.exact_byte_media_prepare_20261010 to exact_byte_owner_20261010;
create policy owner_read on public.exact_byte_media_prepare_20261010
 for select to exact_byte_owner_20261010 using(true);
-- No direct owner insert: receipts are written only through the RPC below.

-- Narrow owner-only read path (review defect: the owner role has no SELECT on
-- content_calendar, so the CLI snapshot could not read the row it prepares).
-- Returns exactly one row's complete jsonb image, or null. SECURITY DEFINER
-- read-only; granted to exact_byte_owner_20261010 only. It exposes no other
-- table and performs no write.
create function public.exact_byte_prepare_read_row_20261010(p_calendar_row_id uuid)
returns jsonb language plpgsql security definer
set search_path=pg_catalog,public set lock_timeout='5s' set statement_timeout='15s' as $$
declare snapshot jsonb;
begin
 if p_calendar_row_id is null then
  return null; end if;
 select to_jsonb(c) into snapshot from public.content_calendar c
  where c.id=p_calendar_row_id;
 return snapshot;
end; $$;
revoke all on function public.exact_byte_prepare_read_row_20261010(uuid)
 from public,anon,authenticated,service_role,exact_byte_owner_20261010;
grant execute on function public.exact_byte_prepare_read_row_20261010(uuid)
 to exact_byte_owner_20261010;

create function public.exact_byte_prepare_bind_20261010(
 p_prepare_id uuid,
 p_calendar_row_id uuid,
 p_before_image jsonb,
 p_image_url text,
 p_thumbnail_url text,
 p_bindings jsonb,
 p_evidence_ref text
) returns jsonb language plpgsql security definer
set search_path=pg_catalog,public set lock_timeout='5s' set statement_timeout='30s' as $$
declare
 c public.content_calendar%rowtype;
 after_row public.content_calendar%rowtype;
 persisted public.content_calendar%rowtype;
 rev_before text; rev_after text; b jsonb; ord integer:=0;
 has_thumbnail boolean;
 seen_roles text[]:=array[]::text[];
begin
 if current_setting('transaction_isolation') <> 'read committed' then
  raise exception 'exact-byte preparation requires read committed' using errcode='25000'; end if;
 if not pg_try_advisory_xact_lock(hashtextextended('exact_byte_send_20261010',0)) then
  raise exception 'exact-byte authority busy' using errcode='55P03'; end if;
 if p_prepare_id is null or p_calendar_row_id is null
  or jsonb_typeof(p_before_image) is distinct from 'object'
  or nullif(btrim(coalesce(p_evidence_ref,'')),'') is null
  or jsonb_typeof(p_bindings) is distinct from 'array' then
  raise exception 'exact-byte preparation arguments invalid' using errcode='23514'; end if;

 select * into c from public.content_calendar where id=p_calendar_row_id for update;
 if not found then
  raise exception 'exact-byte preparation row unavailable' using errcode='23514'; end if;

 -- Refused states: published/denied/killed, historical, active claim/lease,
 -- existing exact-byte attempt, prior preparation, non-active variant.
 if c.status in ('published','denied','killed')
  or c.published_at is not null or c.late_post_id is not null then
  raise exception 'exact-byte preparation refuses published/historical row' using errcode='23514'; end if;
 if c.publish_claim_token is not null or c.publish_reservation_day is not null then
  raise exception 'exact-byte preparation refuses active claim/lease' using errcode='23514'; end if;
 if c.variant_status is distinct from 'active' then
  raise exception 'exact-byte preparation refuses non-active variant' using errcode='23514'; end if;
 if exists(select 1 from public.exact_byte_send_attempt_20261010 a where a.calendar_row_id=c.id) then
  raise exception 'exact-byte preparation refuses attempted row' using errcode='23514'; end if;
 if exists(select 1 from public.exact_byte_media_prepare_20261010 p where p.calendar_row_id=c.id) then
  raise exception 'exact-byte preparation already recorded' using errcode='23514'; end if;

 -- Approved/provenance fail-closed (2026-10-10 independent-review ruling):
 -- approval provenance binds the FINAL image_url
 -- (calendar_approval_provenance_20261005). Rebinding media on ANY approved
 -- or provenance-bearing row -- including legacy 'approved' rows whose
 -- approval_digest is NULL -- would silently invalidate the recorded human
 -- approval. Preparation works only on pending/unapproved rows; a prepared
 -- row must return through the normal client reapproval flow. Never
 -- recompute, rewrite, or forge an approval. to_jsonb()->> returns NULL for
 -- absent provenance columns, so this fails closed on both legacy and
 -- provenance-era schemas.
 if c.status is distinct from 'pending'
  or to_jsonb(c)->>'approval_digest' is not null
  or to_jsonb(c)->>'approval_kind' is not null
  or to_jsonb(c)->>'approved_by' is not null
  or to_jsonb(c)->>'approved_at' is not null then
  raise exception 'exact-byte preparation refuses approved/provenance row' using errcode='23514'; end if;

 -- Complete before-image CAS: approval, content, tenant, date, target and all
 -- source fields must be byte-identical to the operator's reviewed snapshot.
 if to_jsonb(c) is distinct from p_before_image then
  raise exception 'exact-byte preparation before-image mismatch' using errcode='23514'; end if;

 has_thumbnail := nullif(btrim(coalesce(to_jsonb(c)->>'thumbnail_url','')),'') is not null;

 -- New URLs: https only, image required. Complete audited outbound binding:
 -- a reviewed row carrying a thumbnail MUST be rebound with a new thumbnail
 -- (never left mutable), and a row without one must not gain one.
 if nullif(btrim(coalesce(p_image_url,'')),'') is null
  or p_image_url !~ '^https://[^[:space:]]+$' then
  raise exception 'exact-byte prepared image URL invalid' using errcode='23514'; end if;
 if has_thumbnail and nullif(btrim(coalesce(p_thumbnail_url,'')),'') is null then
  raise exception 'exact-byte preparation requires complete thumbnail binding' using errcode='23514'; end if;
 if p_thumbnail_url is not null
  and (not has_thumbnail or p_thumbnail_url !~ '^https://[^[:space:]]+$') then
  raise exception 'exact-byte prepared thumbnail URL invalid' using errcode='23514'; end if;

 -- Bindings must exactly cover the prepared fields, in ordinal order, with
 -- verified proof fields recorded by the offline preparation step. SQL cannot
 -- verify remote bytes; it pins the operator's verified digests to this row.
 if jsonb_array_length(p_bindings) <> (case when has_thumbnail then 2 else 1 end) then
  raise exception 'exact-byte preparation binding set incomplete' using errcode='23514'; end if;
 for b in select value from jsonb_array_elements(p_bindings) loop
  if jsonb_typeof(b) is distinct from 'object'
   or b->>'ordinal' is null or (b->>'ordinal')::int <> ord
   or b->>'role' is null or b->>'role' <> (case when ord=0 then 'image' else 'thumbnail' end)
   or b->>'role' = any(seen_roles)
   or coalesce(b->>'source_url','') !~ '^https://[^[:space:]]+$'
   or coalesce(b->>'prepared_url','') !~ '^https://[^[:space:]]+$'
   or coalesce(b->>'sha256','') !~ '^sha256:[0-9a-f]{64}$'
   or jsonb_typeof(b->'byte_length') is distinct from 'number'
   or (b->>'byte_length')::bigint not between 1 and 134217728
   or nullif(btrim(coalesce(b->>'lock_rule_id','')),'') is null
   or jsonb_typeof(b->'retention_until') is distinct from 'number'
   or coalesce(b->>'attestation_sha256','') !~ '^[0-9a-f]{64}$' then
   raise exception 'exact-byte preparation binding invalid' using errcode='23514'; end if;
  if (ord=0 and b->>'prepared_url' <> p_image_url)
   or (ord=1 and b->>'prepared_url' is distinct from p_thumbnail_url) then
   raise exception 'exact-byte preparation binding URL mismatch' using errcode='23514'; end if;
  -- The recorded source URL must be the exact reviewed row field it replaces.
  if (ord=0 and b->>'source_url' is distinct from c.image_url)
   or (ord=1 and b->>'source_url' is distinct from (to_jsonb(c)->>'thumbnail_url')) then
   raise exception 'exact-byte preparation source binding mismatch' using errcode='23514'; end if;
  seen_roles := array_append(seen_roles, b->>'role'); ord:=ord+1;
 end loop;

 rev_before:=public.exact_byte_row_revision_20261010(c);

 -- Rewrite the row from its OLD image with only the two media fields replaced;
 -- approval, status, lease and tenant/date/target columns cannot move.
 after_row:=jsonb_populate_record(c, jsonb_build_object('image_url', p_image_url)
  || case when p_thumbnail_url is null then '{}'::jsonb
     else jsonb_build_object('thumbnail_url', p_thumbnail_url) end);
 update public.content_calendar set image_url=after_row.image_url,
  thumbnail_url=after_row.thumbnail_url where id=c.id;

 -- Re-read the persisted row (triggers such as updated_at may have fired).
 -- Verify ONLY the allowed fields moved, then pin the receipt to the exact
 -- persisted image rather than the synthetic projection.
 select * into persisted from public.content_calendar where id=c.id;
 if not found then
  raise exception 'exact-byte preparation persisted row missing' using errcode='23514'; end if;
 if persisted.image_url is distinct from after_row.image_url
  or persisted.thumbnail_url is distinct from after_row.thumbnail_url then
  raise exception 'exact-byte preparation persisted media mismatch' using errcode='23514'; end if;
 if (to_jsonb(persisted) - 'image_url' - 'thumbnail_url' - 'updated_at')
  is distinct from (to_jsonb(c) - 'image_url' - 'thumbnail_url' - 'updated_at') then
  raise exception 'exact-byte preparation post-update drift' using errcode='23514'; end if;
 after_row:=persisted;
 rev_after:=public.exact_byte_row_revision_20261010(after_row);
 if rev_after = rev_before then
  raise exception 'exact-byte preparation made no bound change' using errcode='23514'; end if;

 insert into public.exact_byte_media_prepare_20261010(
  prepare_id, calendar_row_id, row_revision_before, row_revision_after,
  before_image, after_image, bindings, evidence_ref)
 values(p_prepare_id, c.id, rev_before, rev_after,
  to_jsonb(c), to_jsonb(after_row), p_bindings, p_evidence_ref);

 return jsonb_build_object('prepared',true,'prepare_id',p_prepare_id,
  'calendar_row_id',c.id,'row_revision_before',rev_before,'row_revision_after',rev_after);
end; $$;

revoke all on function public.exact_byte_prepare_bind_20261010(uuid,uuid,jsonb,text,text,jsonb,text)
 from public,anon,authenticated,service_role,exact_byte_owner_20261010;
grant execute on function public.exact_byte_prepare_bind_20261010(uuid,uuid,jsonb,text,text,jsonb,text)
 to exact_byte_owner_20261010;
commit;
