-- DRAFT / UNAPPLIED. Per-asset historical media clearance receipts.
-- Roughly 610 older approved Drive media assets in public.media_asset must
-- remain unavailable until EACH is individually proven unused and explicitly
-- cleared by a named reviewer. This file records ONLY the reviewer's
-- receipts; it has no backfill, arms nothing, and adds no trigger on
-- media_asset or content_calendar. Applying it does NOT make any historical
-- asset available by itself: availability remains the responsibility of the
-- separate writer-side change, which must fail closed (absence of an active
-- 'cleared' receipt means the asset stays unavailable). The writer-side gate
-- applies only to assets first seen BEFORE the gym's configured activation
-- cutoff (AGENT_HISTORICAL_MEDIA_CLEARANCE_CUTOFF_<GYM_ID>); newer assets are
-- exempt and stay subject to normal review and the global ledger.
-- Identity is NEVER inferred from filename, URL or pHash: a receipt binds
-- (gym_id, asset Drive file ID, source_id, current content_hash) under a
-- SELECT ... FOR UPDATE compare-and-set on the live media_asset row.
-- Known-used assets are permanently ineligible: once ANY known_used receipt
-- exists for (gym_id, asset_id) no further receipt can ever be recorded and
-- that receipt can never be revoked, replaced or mutated.
-- Ambiguous assets are 'held'.
-- VERSIONED APPEND-ONLY (2026-10-03 repair): receipts are versioned rows
-- (gym_id, asset_id, version), never deleted, never updated except a one-way
-- revocation stamp. A revoked 'cleared'/'held' receipt does NOT block
-- re-review: after revocation the reviewer may record a NEW version of the
-- receipt for the SAME asset at its CURRENT content_hash (e.g. a corrected
-- review after a Drive byte change). At most ONE active (unrevoked) receipt
-- may exist per (gym_id, asset_id); recording against an active receipt is
-- refused until it is revoked.
-- Apply order: after the base media_source/media_asset schema
-- (media_source_media_asset_20260827.sql). Independent of the visual-group
-- drafts. Rollback before any writer-side arming: DROP FUNCTION
-- public.record_historical_media_clearance(text,text,text,text,text,jsonb),
-- DROP FUNCTION public.revoke_historical_media_clearance(text,text,text,text,text),
-- DROP FUNCTION public.media_historical_clearance_guard(),
-- DROP TABLE public.media_historical_clearance. Once any 'known_used' or
-- 'cleared' receipt has been consumed by writers, the table is permanent
-- history and DROP is not an acceptable data rollback.
begin;

create table if not exists public.media_historical_clearance (
  gym_id            text        not null check (btrim(gym_id) <> ''),
  asset_id          text        not null check (btrim(asset_id) <> ''),
  -- Append-only receipt version for this asset: 1, 2, 3, ... assigned by the
  -- record RPC. Versioning is what lets a corrected review (same asset, new
  -- content_hash) be recorded after a revocation without mutating history.
  version           int         not null check (version >= 1),
  source_id         text        not null check (btrim(source_id) <> ''),
  -- Must equal the asset's CURRENT content_hash at record time (CAS-checked
  -- by the record RPC under a row lock). MD5 (32) or SHA256 (64), lowercase.
  content_hash      text        not null check (
    lower(btrim(content_hash)) ~ '^[0-9a-f]{32}$|^[0-9a-f]{64}$'),
  decision          text        not null check (
    decision in ('cleared','known_used','held')),
  -- A named human/operator identity; never an automatic value.
  reviewer          text        not null check (btrim(reviewer) <> ''),
  -- Non-empty object that must bind gym_id, asset_id and content_hash to the
  -- exact values of the row columns.
  evidence          jsonb       not null check (
    jsonb_typeof(evidence) = 'object'
      and evidence <> '{}'::jsonb
      and evidence->>'gym_id' = gym_id
      and evidence->>'asset_id' = asset_id
      and lower(btrim(coalesce(evidence->>'content_hash',''))) = lower(btrim(content_hash))),
  recorded_at       timestamptz not null default now(),
  -- One-way revocation metadata: all null (active) or all non-null (revoked).
  -- Revocation supersedes THIS version only; it never un-revokes and never
  -- touches known_used.
  revoked_at        timestamptz,
  revoked_by        text,
  revocation_reason text,
  primary key (gym_id, asset_id, version),
  check ((revoked_at is null and revoked_by is null and revocation_reason is null)
      or (revoked_at is not null and btrim(revoked_by) <> ''
          and btrim(revocation_reason) <> ''))
);

comment on table public.media_historical_clearance is
  'Append-only VERSIONED reviewer receipts for per-asset historical media clearance. One or more versions per (gym_id, asset Drive file id); at most one active (unrevoked) version at a time. known_used is permanent and blocks all future receipts for the asset. cleared/held versions may only be revoked (one-way); a revoked version may be superseded by a new version for the same asset at its current content_hash. Presence of this table alone never makes an asset available.';

create index if not exists media_historical_clearance_hash_idx
  on public.media_historical_clearance (gym_id, content_hash);
create index if not exists media_historical_clearance_asset_idx
  on public.media_historical_clearance (gym_id, asset_id);

-- These are owner-only writes. The service role can inspect receipts but must
-- use the validated SECURITY DEFINER RPCs to record or revoke them.
alter table public.media_historical_clearance enable row level security;
revoke all on public.media_historical_clearance
  from public, anon, authenticated, service_role;
grant select on public.media_historical_clearance to service_role;

-- DELETE is always refused. UPDATE is refused entirely for 'known_used' rows
-- (known-used is permanently ineligible). For other rows ONLY the revocation
-- columns may change, and only from NULL to non-NULL: no un-revoke, no
-- decision flips, no re-pointing to another hash, asset identity or version.
-- INSERTs are unrestricted here; the record RPC is the sole validated writer.
create or replace function public.media_historical_clearance_guard()
returns trigger language plpgsql set search_path = public as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'historical media clearance receipts cannot be deleted'
      using errcode = '23514';
  end if;
  if old.decision = 'known_used' then
    raise exception 'known_used historical media clearance is permanent and immutable'
      using errcode = '23514';
  end if;
  if new.gym_id <> old.gym_id or new.asset_id <> old.asset_id
      or new.version <> old.version
      or new.source_id <> old.source_id
      or new.content_hash <> old.content_hash
      or new.decision <> old.decision
      or new.reviewer <> old.reviewer
      or new.evidence <> old.evidence
      or new.recorded_at is distinct from old.recorded_at
      or old.revoked_at is not null
      or new.revoked_at is null or new.revoked_by is null
      or btrim(new.revoked_by) = ''
      or new.revocation_reason is null or btrim(new.revocation_reason) = '' then
    raise exception 'historical media clearance allows only one-way revocation of active cleared/held receipts'
      using errcode = '23514';
  end if;
  return new;
end;
$$;
drop trigger if exists media_historical_clearance_guard
  on public.media_historical_clearance;
create trigger media_historical_clearance_guard before update or delete
  on public.media_historical_clearance for each row
  execute function public.media_historical_clearance_guard();

-- Record one per-asset clearance receipt VERSION. Locks the live media_asset
-- row and requires an exact compare-and-set: the asset exists, belongs to
-- p_gym_id, has a non-null source_id, and its CURRENT content_hash equals
-- lower(btrim(p_content_hash)). Any mismatch raises with errcode '23514'.
-- Any known_used receipt for (gym_id, asset_id) — revoked or not — makes the
-- asset permanently ineligible and refuses the insert. Any ACTIVE (unrevoked)
-- receipt refuses the insert: revoke the stale receipt first, then re-record
-- a new version (e.g. a corrected review for the same asset at its current
-- content_hash). Revoked receipts remain as immutable history.
create or replace function public.record_historical_media_clearance(
  p_gym_id text, p_asset_id text, p_content_hash text,
  p_decision text, p_reviewer text, p_evidence jsonb
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_asset public.media_asset%rowtype;
  v_version int;
begin
  p_gym_id := btrim(p_gym_id);
  p_asset_id := btrim(p_asset_id);
  p_content_hash := lower(btrim(p_content_hash));
  if nullif(p_gym_id,'') is null or nullif(p_asset_id,'') is null
      or p_content_hash is null
      or p_content_hash !~ '^[0-9a-f]{32}$|^[0-9a-f]{64}$'
      or p_decision is null or p_decision not in ('cleared','known_used','held')
      or nullif(btrim(p_reviewer),'') is null
      or p_evidence is null or jsonb_typeof(p_evidence) <> 'object'
      or p_evidence = '{}'::jsonb
      or p_evidence->>'gym_id' is distinct from p_gym_id
      or p_evidence->>'asset_id' is distinct from p_asset_id
      or lower(btrim(coalesce(p_evidence->>'content_hash',''))) is distinct from p_content_hash then
    raise exception 'invalid historical clearance: decision, named reviewer and evidence binding gym_id/asset_id/content_hash are required'
      using errcode = '23514';
  end if;
  -- Compare-and-set against the live asset row under a row lock. A hash that
  -- changed since review, an asset of another gym, or a missing source
  -- binding refuses the receipt.
  select * into v_asset from public.media_asset
    where id = p_asset_id for update;
  if not found or v_asset.gym_id <> p_gym_id
      or nullif(btrim(v_asset.source_id),'') is null
      or lower(btrim(coalesce(v_asset.content_hash,''))) is distinct from p_content_hash then
    raise exception 'media asset identity, gym or current content_hash does not match the clearance request'
      using errcode = '23514';
  end if;
  -- known_used is permanent: ANY known_used version, even a revoked one that
  -- could only exist by bypassing the guard, forever blocks new receipts.
  if exists (select 1 from public.media_historical_clearance
             where gym_id = p_gym_id and asset_id = p_asset_id
               and decision = 'known_used') then
    raise exception 'asset is permanently known_used and can never be re-recorded'
      using errcode = '23514';
  end if;
  -- At most one ACTIVE (unrevoked) receipt per asset. Superseding a stale
  -- receipt requires explicit revocation first; the new receipt is recorded
  -- as the next version so the correction history is preserved.
  if exists (select 1 from public.media_historical_clearance
             where gym_id = p_gym_id and asset_id = p_asset_id
               and revoked_at is null) then
    raise exception 'an active clearance receipt already exists; revoke the stale receipt before re-recording'
      using errcode = '23514';
  end if;
  select coalesce(max(version), 0) + 1 into v_version
    from public.media_historical_clearance
    where gym_id = p_gym_id and asset_id = p_asset_id;
  insert into public.media_historical_clearance
    (gym_id, asset_id, version, source_id, content_hash, decision, reviewer,
     evidence)
    values (p_gym_id, p_asset_id, v_version, v_asset.source_id, p_content_hash,
      p_decision, btrim(p_reviewer), p_evidence);
  return jsonb_build_object(
    'gym_id', p_gym_id,
    'asset_id', p_asset_id,
    'version', v_version,
    'source_id', v_asset.source_id,
    'content_hash', p_content_hash,
    'decision', p_decision,
    'reviewer', btrim(p_reviewer),
    'availability_changed', false,
    'note', 'receipt recorded only; this never makes a historical asset available by itself');
end;
$$;

-- One-way revocation of the ACTIVE 'cleared' or 'held' receipt version.
-- Requires a named reviewer and a reason, locks the live asset row and
-- compare-and-sets its current content_hash against p_expected_content_hash
-- ('23514' on mismatch). Refuses when no active receipt exists or when the
-- only receipts are 'known_used' (permanent). Revocation supersedes the
-- version only; a corrected receipt may then be recorded as a new version.
create or replace function public.revoke_historical_media_clearance(
  p_gym_id text, p_asset_id text, p_expected_content_hash text,
  p_reviewer text, p_reason text
) returns boolean language plpgsql security definer set search_path = public as $$
declare v_asset public.media_asset%rowtype;
  v_receipt public.media_historical_clearance%rowtype;
begin
  p_gym_id := btrim(p_gym_id);
  p_asset_id := btrim(p_asset_id);
  p_expected_content_hash := lower(btrim(p_expected_content_hash));
  if nullif(p_gym_id,'') is null or nullif(p_asset_id,'') is null
      or p_expected_content_hash is null
      or p_expected_content_hash !~ '^[0-9a-f]{32}$|^[0-9a-f]{64}$'
      or nullif(btrim(p_reviewer),'') is null
      or nullif(btrim(p_reason),'') is null then
    raise exception 'revocation requires asset identity, expected content_hash, a named reviewer and a reason'
      using errcode = '23514';
  end if;
  select * into v_asset from public.media_asset
    where id = p_asset_id for update;
  if not found or v_asset.gym_id <> p_gym_id
      or lower(btrim(coalesce(v_asset.content_hash,''))) is distinct from p_expected_content_hash then
    raise exception 'media asset gym or current content_hash does not match the revocation request'
      using errcode = '23514';
  end if;
  select * into v_receipt from public.media_historical_clearance
    where gym_id = p_gym_id and asset_id = p_asset_id and revoked_at is null
    order by version desc limit 1 for update;
  if not found then
    raise exception 'no active historical clearance receipt to revoke'
      using errcode = '23514';
  end if;
  if v_receipt.decision = 'known_used' then
    raise exception 'known_used historical media clearance is permanent and cannot be revoked'
      using errcode = '23514';
  end if;
  update public.media_historical_clearance
    set revoked_at = now(), revoked_by = btrim(p_reviewer),
        revocation_reason = btrim(p_reason)
    where gym_id = p_gym_id and asset_id = p_asset_id
      and version = v_receipt.version;
  return true;
end;
$$;

revoke all on function public.media_historical_clearance_guard()
  from public, anon, authenticated, service_role;
revoke all on function public.record_historical_media_clearance(text,text,text,text,text,jsonb)
  from public, anon, authenticated;
revoke all on function public.revoke_historical_media_clearance(text,text,text,text,text)
  from public, anon, authenticated;
grant execute on function public.record_historical_media_clearance(text,text,text,text,text,jsonb)
  to service_role;
grant execute on function public.revoke_historical_media_clearance(text,text,text,text,text)
  to service_role;

commit;
