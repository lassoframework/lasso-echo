-- DRAFT / UNAPPLIED / DEFAULT OFF. Requires the 20261006 authority draft.
-- Producer candidates are immutable UNTRUSTED observations, NEVER original,
-- history, render, lineage or permission receipts. No gates are enabled here.
-- No owner login, transport, calendar/asset access or progress API is provisioned.
-- A missing candidate/authority keeps a row unpublishable under the global guard.
-- Rollback before use: drop these RPCs, triggers and this empty table. After use
-- preserve the candidate table as evidence; disable the Python flag first.
begin;

create table public.fixer_forward_media_observation_20261007 (
  calendar_row_id uuid not null,
  row_revision text not null check(row_revision ~ '^[0-9a-f]{32}$'),
  observation_digest text not null check(observation_digest ~ '^[0-9a-f]{64}$'),
  tenant_id text not null,
  gym_id text not null,
  source_asset_id text not null,
  source_exact_url text not null,
  delivered_exact_url text not null,
  calendar_snapshot jsonb not null,
  observation_json text not null,
  digest_input text not null,
  provenance_status text not null default 'unverified' check(provenance_status='unverified'),
  recorded_at timestamptz not null default clock_timestamp(),
  primary key(calendar_row_id,row_revision)
  -- Deliberately no calendar FK: delete/rebuild must retain immutable evidence.
  -- A future owner joins the exact UUID/revision under lock, never a slot/URL.
);
alter table public.fixer_forward_media_observation_20261007 enable row level security;
revoke all on public.fixer_forward_media_observation_20261007
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,
       fixer_forward_media_owner_20261006;
create trigger immutable_row before update or delete
  on public.fixer_forward_media_observation_20261007 for each row
  execute function public.fixer_forward_media_immutable_20261006();
create trigger immutable_truncate before truncate
  on public.fixer_forward_media_observation_20261007 for each statement
  execute function public.fixer_forward_media_immutable_20261006();

create function public.fixer_forward_media_observation_bridge_ready_20261007()
returns boolean language sql security definer set search_path=pg_catalog,public as $$
  select to_regclass('public.fixer_forward_media_observation_20261007') is not null;
$$;
revoke all on function public.fixer_forward_media_observation_bridge_ready_20261007()
  from public,anon,authenticated,fixer_forward_media_attester_20261006,
       fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_media_observation_bridge_ready_20261007()
  to service_role;

create function public.fixer_record_forward_media_observation_20261007(
  p_calendar_row_id uuid,p_expected_row jsonb,p_observation_json text,p_digest_input text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; snapshot jsonb; observation jsonb;
  tenant text; revision text; digest text;
  old public.fixer_forward_media_observation_20261007%rowtype;
begin
  -- Fail before parsing an unbounded candidate. sha256(bytea) is built-in on PG17.
  if p_observation_json is null or p_digest_input is null
      or octet_length(p_observation_json)>65536 or octet_length(p_digest_input)>65536 then
    raise exception 'bounded unverified observation required' using errcode='23514';
  end if;
  observation:=p_observation_json::jsonb;
  digest:=encode(sha256(convert_to(p_digest_input,'UTF8')),'hex');
  if jsonb_typeof(observation) is distinct from 'object'
      or observation->'schema_version' is distinct from '1'::jsonb
      or observation->>'provenance_status' is distinct from 'unverified'
      or observation->>'observation_digest' is distinct from digest
      or (observation-'observation_digest') is distinct from p_digest_input::jsonb
      or jsonb_typeof(observation->'recipe') is distinct from 'object'
      or jsonb_typeof(observation->'hold_reasons') is distinct from 'array'
      or coalesce(observation->>'source_sha256','') !~ '^[0-9a-f]{64}$'
      or coalesce(observation->>'delivered_sha256','') !~ '^[0-9a-f]{64}$'
      or jsonb_typeof(observation->'source_byte_length') is distinct from 'number'
      or jsonb_typeof(observation->'delivered_byte_length') is distinct from 'number'
      or (observation->>'source_byte_length') !~ '^[1-9][0-9]{0,8}$'
      or (observation->>'delivered_byte_length') !~ '^[1-9][0-9]{0,8}$'
      or (observation->>'source_byte_length')::bigint>134217728
      or (observation->>'delivered_byte_length')::bigint>134217728 then
    raise exception 'unverified observation contract invalid' using errcode='23514';
  end if;
  select * into r from public.content_calendar where id=p_calendar_row_id for update;
  if not found then
    raise exception 'exact inserted calendar row unavailable' using errcode='23514';
  end if;
  snapshot:=to_jsonb(r);
  revision:=md5(snapshot::text);
  -- A returned UUID alone cannot associate a stale writer response with new media.
  if p_expected_row is distinct from snapshot
      or r.status is null or r.status not in ('draft','pending','queued')
      or r.variant_status is distinct from 'active' or r.post_date is null
      or r.published_at is not null or r.late_post_id is not null
      or r.publish_claim_token is not null or r.render_manifest_digest is not null
      or nullif(btrim(r.gym_id),'') is null
      or nullif(btrim(r.source_media_asset_id),'') is null
      or r.source_media_url is null or r.source_media_url !~ '^https://[^[:space:]]+$'
      or r.image_url is null or r.image_url !~ '^https://[^[:space:]]+$' then
    raise exception 'exact unsent calendar revision required' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,btrim(r.gym_id));
  if observation->>'tenant' is distinct from tenant
      or observation->>'source_asset_id' is distinct from r.source_media_asset_id
      or observation->>'source_exact_url' is distinct from r.source_media_url
      or observation->>'delivered_exact_url' is distinct from r.image_url then
    raise exception 'canonical observation media binding invalid' using errcode='23514';
  end if;
  insert into public.fixer_forward_media_observation_20261007
    (calendar_row_id,row_revision,observation_digest,tenant_id,gym_id,source_asset_id,
     source_exact_url,delivered_exact_url,calendar_snapshot,observation_json,digest_input)
    values(r.id,revision,digest,tenant,r.gym_id,r.source_media_asset_id,
      r.source_media_url,r.image_url,snapshot,p_observation_json,p_digest_input)
    on conflict(calendar_row_id,row_revision) do nothing;
  select * into old from public.fixer_forward_media_observation_20261007
    where calendar_row_id=r.id and row_revision=revision;
  if old.observation_digest is distinct from digest
      or old.observation_json is distinct from p_observation_json
      or old.digest_input is distinct from p_digest_input
      or old.calendar_snapshot is distinct from snapshot
      or old.tenant_id is distinct from tenant or old.gym_id is distinct from r.gym_id
      or old.source_asset_id is distinct from r.source_media_asset_id
      or old.source_exact_url is distinct from r.source_media_url
      or old.delivered_exact_url is distinct from r.image_url then
    raise exception 'immutable observation conflict' using errcode='23514';
  end if;
  return jsonb_build_object('calendar_row_id',r.id,'revision',revision,
    'observation_digest',digest,'provenance_status','unverified');
end;
$$;
revoke all on function public.fixer_record_forward_media_observation_20261007(uuid,jsonb,text,text)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,
       fixer_forward_media_owner_20261006;
grant execute on function public.fixer_record_forward_media_observation_20261007(uuid,jsonb,text,text)
  to service_role;
-- No service grant on original registry, history clearance, render manifest,
-- lineage, claim ledger or gate. Existing authority boundaries stay intact.
commit;
