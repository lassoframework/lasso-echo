-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Append-only durable sink for exact-byte immutable-media verification proofs
-- (agent/exact_byte_proof_store.py, consumed by r2_immutable_media's
-- trusted_bool_adapter proof_sink). Railway's ephemeral local disk is not a
-- production store; this table is the durable production path.
--
-- Append-only authority: the table grants INSERT to no role directly. The sole
-- write path is the security-definer RPC exact_byte_proof_append_20261010,
-- executable only by service_role, which strictly validates the proof shape
-- and inserts one row per call. Triggers make UPDATE/DELETE/TRUNCATE fail.
-- There is intentionally no correction path: a disputed proof is retained and
-- superseded by a newer verification, never edited or removed.
--
-- Rollback BEFORE any use: drop the function, triggers and table. AFTER any
-- use: preserve ALL proof rows; never DROP/DELETE/TRUNCATE this history.
begin;

create table public.exact_byte_proof_sink_20261010 (
  proof_id uuid primary key default gen_random_uuid(),
  public_url text not null check (public_url ~ '^https://[^[:space:]]+$'),
  account_id text not null check (account_id ~ '^[0-9a-f]{32}$'),
  bucket text not null check (bucket ~ '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'),
  object_key text not null check (object_key=btrim(object_key) and object_key<>''),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint not null check (size_bytes > 0 and size_bytes <= 134217728),
  observed_at bigint not null check (observed_at > 0),
  retention_until bigint not null check (retention_until > observed_at),
  lock_rule_id text not null check (lock_rule_id=btrim(lock_rule_id) and lock_rule_id<>''),
  attestation_sha256 text not null check (attestation_sha256 ~ '^[0-9a-f]{64}$'),
  recorded_at timestamptz not null default now()
);

create function public.exact_byte_proof_immutable_20261010() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin
  raise exception 'exact-byte proofs are append-only' using errcode='23514';
end; $$;

create trigger exact_byte_proof_no_update_20261010
  before update or delete on public.exact_byte_proof_sink_20261010
  for each row execute function public.exact_byte_proof_immutable_20261010();

create trigger exact_byte_proof_no_truncate_20261010
  before truncate on public.exact_byte_proof_sink_20261010
  for each statement execute function public.exact_byte_proof_immutable_20261010();

-- Sole write path. Validates the exact proof shape the runtime serializes;
-- no extra keys, no nulls, no caller-supplied identity columns. Duplicate
-- appends after a lost response are harmless: every row is a new proof_id.
create function public.exact_byte_proof_append_20261010(p_proof jsonb)
returns boolean language plpgsql security definer
set search_path=pg_catalog,public as $$
declare
  v public.exact_byte_proof_sink_20261010%rowtype;
begin
  if p_proof is null or jsonb_typeof(p_proof) <> 'object'
     or p_proof ?| array['proof_id','recorded_at']
     or not (p_proof ?& array['public_url','account_id','bucket','key','sha256',
             'size_bytes','observed_at','retention_until','lock_rule_id',
             'attestation_sha256'])
     or (select count(*) from jsonb_object_keys(p_proof)) <> 10 then
    raise exception 'proof shape invalid' using errcode='22023';
  end if;
  insert into public.exact_byte_proof_sink_20261010(
    public_url, account_id, bucket, object_key, sha256, size_bytes,
    observed_at, retention_until, lock_rule_id, attestation_sha256)
  values(
    p_proof->>'public_url', p_proof->>'account_id', p_proof->>'bucket',
    p_proof->>'key', p_proof->>'sha256',
    (p_proof->>'size_bytes')::bigint, (p_proof->>'observed_at')::bigint,
    (p_proof->>'retention_until')::bigint,
    p_proof->>'lock_rule_id', p_proof->>'attestation_sha256')
  returning * into v;
  return true;
exception
  when check_violation or invalid_text_representation or numeric_value_out_of_range then
    raise exception 'proof shape invalid' using errcode='22023';
end; $$;

revoke all on public.exact_byte_proof_sink_20261010
  from public, anon, authenticated, service_role;
revoke all on function public.exact_byte_proof_immutable_20261010()
  from public, anon, authenticated, service_role;
revoke all on function public.exact_byte_proof_append_20261010(jsonb)
  from public, anon, authenticated;
grant execute on function public.exact_byte_proof_append_20261010(jsonb)
  to service_role;

commit;
