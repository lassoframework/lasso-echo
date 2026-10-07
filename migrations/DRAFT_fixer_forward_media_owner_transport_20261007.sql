-- DRAFT / UNAPPLIED / DEFAULT OFF. No login, credentials or runtime is provisioned.
-- Requires the authority + observation bridge drafts and existing media_asset.
-- Only the dedicated owner group receives RPC access. Producer observations and
-- media_asset.used_count/content_hash are NOT trusted historical evidence.
-- No source URL/audited registry/history transport exists: Python holds safely.
-- A committed quarantine reservation is never retried/expired automatically.
-- First reservation COMMIT uncertainty halts before authority; a never-committed
-- key cannot be excluded across fresh processes. Separate durable coordinator
-- remains a release gap; do not claim global quarantine for the first write.
-- Recovery is MANUAL: independently reconcile authority, outcome and original
-- exact revision before any operator-authorized new work. Never delete evidence.
-- Rollback before use: disable worker, drop RPCs and empty progress table.
-- After use preserve progress/quarantine as reconciliation evidence.
begin;
create table public.fixer_forward_media_owner_progress_20261007 (
  calendar_row_id uuid not null,
  row_revision text not null check(row_revision ~ '^[0-9a-f]{32}$'),
  observation_digest text not null check(observation_digest ~ '^[0-9a-f]{64}$'),
  reservation_token uuid not null unique,
  state text not null default 'quarantine' check(state in ('quarantine','final')),
  outcome jsonb,
  reserved_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  primary key(calendar_row_id,row_revision,observation_digest),
  check((state='quarantine' and outcome is null and finished_at is null)
     or (state='final' and outcome is not null and finished_at is not null))
);
alter table public.fixer_forward_media_owner_progress_20261007 enable row level security;
revoke all on public.fixer_forward_media_owner_progress_20261007
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,
 fixer_forward_media_owner_20261006;

create function public.fixer_forward_media_owner_pending_20261007(p_tenants text[],p_limit integer)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if p_tenants is null or cardinality(p_tenants) not between 1 and 32
     or p_limit is null or p_limit not between 1 and 100 then
   raise exception 'bounded owner tenant scope required' using errcode='23514';
 end if;
 return coalesce((select jsonb_agg(candidate order by tenant_rank,recorded_at,calendar_row_id)
 from (select jsonb_build_object('calendar_row_id',o.calendar_row_id,
       'revision',o.row_revision,'observation_digest',o.observation_digest) candidate,
       o.recorded_at,o.calendar_row_id,
       row_number() over(partition by o.tenant_id order by o.recorded_at,o.calendar_row_id) tenant_rank
   from public.fixer_forward_media_observation_20261007 o
   join public.content_calendar r on r.id=o.calendar_row_id
   where o.tenant_id=any(p_tenants) and r.gym_id=o.gym_id
     and o.row_revision=md5(to_jsonb(r)::text)
     and r.status in ('draft','pending','queued','approved') and r.variant_status='active'
     and r.publish_claim_token is null and r.published_at is null
     and r.late_post_id is null and r.render_manifest_digest is null
     and not exists(select 1 from public.fixer_forward_media_owner_progress_20261007 p
       where p.calendar_row_id=o.calendar_row_id and p.row_revision=o.row_revision
         and p.observation_digest=o.observation_digest)
   order by tenant_rank,o.recorded_at,o.calendar_row_id limit p_limit) q),'[]'::jsonb);
end;
$$;

create function public.fixer_forward_media_owner_reserve_20261007(
 p_id uuid,p_revision text,p_digest text,p_token uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare added integer;
begin
 if p_token is null or not exists(select 1 from public.fixer_forward_media_observation_20261007 o
   where o.calendar_row_id=p_id and o.row_revision=p_revision and o.observation_digest=p_digest) then
   raise exception 'exact owner candidate required' using errcode='23514';
 end if;
 insert into public.fixer_forward_media_owner_progress_20261007
   (calendar_row_id,row_revision,observation_digest,reservation_token)
   values(p_id,p_revision,p_digest,p_token) on conflict do nothing;
 get diagnostics added=row_count;
 return added=1;
end;
$$;

create function public.fixer_forward_media_owner_locked_20261007(
 p_id uuid,p_revision text,p_digest text,p_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype;
 o public.fixer_forward_media_observation_20261007%rowtype;
 a public.media_asset%rowtype; revision text;
begin
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
 if not found or o.calendar_snapshot is distinct from to_jsonb(r) then
   return jsonb_build_object('hold_reason','candidate_canonical_binding_invalid');
 end if;
 select * into a from public.media_asset where id=r.source_media_asset_id for update;
 if not found or a.gym_id is distinct from r.gym_id or o.tenant_id is distinct from r.gym_id then
   return jsonb_build_object('hold_reason','canonical_tenant_asset_mismatch');
 end if;
 -- Raw asset cannot establish exact source URL/registry receipts. Return it
 -- honestly, with no fabricated URL, receipt or zero-use clearance.
 return jsonb_build_object('calendar',to_jsonb(r),'asset',to_jsonb(a),
   'observation',o.observation_json::jsonb,'revision',revision);
end;
$$;

create function public.fixer_forward_media_owner_record_20261007(
 p_id uuid,p_revision text,p_digest text,p_token uuid,p_outcome jsonb)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare p public.fixer_forward_media_owner_progress_20261007%rowtype;
begin
 if p_outcome is null or jsonb_typeof(p_outcome) is distinct from 'object'
     or octet_length(p_outcome::text)>4096
     or coalesce(p_outcome->>'status','') not in ('persisted','hold') then
   raise exception 'bounded owner outcome required' using errcode='23514';
 end if;
 select * into p from public.fixer_forward_media_owner_progress_20261007
   where calendar_row_id=p_id and row_revision=p_revision
     and observation_digest=p_digest and reservation_token=p_token for update;
 if not found then raise exception 'exact reservation required' using errcode='23514'; end if;
 if p.state='final' then
   if p.outcome is distinct from p_outcome then
     raise exception 'immutable outcome conflict' using errcode='23514';
   end if;
   return true;
 end if;
 if p_outcome->>'status'='persisted' then
   perform 1 from public.content_calendar r where r.id=p_id
     and md5(to_jsonb(r)::text)=p_revision for update;
   if not found then raise exception 'canonical revision changed' using errcode='23514'; end if;
   -- A durable success may only reference authority staged in this transaction.
   perform 1 from public.fixer_forward_media_render_manifest_20261006 m
     join public.fixer_forward_media_observation_20261007 o
       on o.calendar_row_id=p_id and o.row_revision=p_revision and o.observation_digest=p_digest
     join public.fixer_forward_media_original_registry_20261006 a
       on a.tenant_id=m.tenant_id and a.source_asset_id=m.source_asset_id
     join public.fixer_forward_media_history_clearance_20261006 h
       on h.tenant_id=a.tenant_id and h.source_asset_id=a.source_asset_id
     where m.manifest_digest=p_outcome->>'manifest_digest'
       and m.tenant_id=o.tenant_id and m.source_asset_id=o.source_asset_id
       and a.source_url=o.source_exact_url and m.image_url=o.delivered_exact_url
       and h.decision=p_outcome->>'decision';
   if not found then raise exception 'exact authority outcome required' using errcode='23514'; end if;
 end if;
 update public.fixer_forward_media_owner_progress_20261007
   set state='final',outcome=p_outcome,finished_at=clock_timestamp()
   where calendar_row_id=p_id and row_revision=p_revision and observation_digest=p_digest;
 return true;
end;
$$;
revoke all on function public.fixer_forward_media_owner_pending_20261007(text[],integer),
 public.fixer_forward_media_owner_reserve_20261007(uuid,text,text,uuid),
 public.fixer_forward_media_owner_locked_20261007(uuid,text,text,uuid),
 public.fixer_forward_media_owner_record_20261007(uuid,text,text,uuid,jsonb)
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_forward_media_owner_pending_20261007(text[],integer),
 public.fixer_forward_media_owner_reserve_20261007(uuid,text,text,uuid),
 public.fixer_forward_media_owner_locked_20261007(uuid,text,text,uuid),
 public.fixer_forward_media_owner_record_20261007(uuid,text,text,uuid,jsonb)
 to fixer_forward_media_owner_20261006;
commit;
