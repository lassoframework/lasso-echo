-- DRAFT / UNAPPLIED. Postgres authority for generated sources and palettes.
-- Default OFF: no runtime flag, provider call, deployment or coach review.
-- service_role is a trusted, tenant-aware writer, not an end-user role. Its
-- caller must verify approval evidence and enforce AGENT_INTAKE_AUTO_APPROVE
-- before submitting auto approval. Evidence strings are receipts, not proof
-- SQL can independently authenticate. Production roles/schema remain unverified.
-- Reads are tenant-scoped service APIs; callers enforce principal-to-tenant
-- authorization. Integration must canonicalize tenant aliases and namespace
-- listener-local SQLite source IDs with their origin/account key before writes;
-- preserving a bare local ID across accounts can collide. Palette receipts must
-- reference the original verified file digest/revision. Arbitrary text does not
-- establish live palette parity. Publish validation authorizes a decision only in its open
-- transaction; external publication/delivery requires separate runtime gates.
begin;

create table public.generated_tenant_epoch_20261007 (
 tenant_id text primary key check (tenant_id ~ '[^[:space:]]'),
 epoch bigint not null default 0 check (epoch >= 0)
);
create table public.generated_source_authority_20261007 (
 tenant_id text not null references public.generated_tenant_epoch_20261007(tenant_id),
 source_id text not null check (source_id ~ '[^[:space:]]'),
 category text not null check (category ~ '[^[:space:]]'),
 exact_text text not null,
 citation text not null,
 status text not null check (status in ('pending','approved','revoked')),
 revision bigint not null check (revision >= 1),
 approval_revision bigint,
 approval_mode text check (approval_mode in ('explicit','auto')),
 approval_evidence text,
 approved_by text,
 epoch bigint not null,
 tombstoned_at timestamptz,
 updated_at timestamptz not null default clock_timestamp(),
 primary key (tenant_id, source_id),
 check ((status='approved' and approval_revision is not null and approval_revision=revision
         and approval_mode is not null and coalesce(approval_evidence,'') ~ '[^[:space:]]'
         and coalesce(approved_by,'') ~ '[^[:space:]]')
        or (status<>'approved' and approval_revision is null and approval_mode is null
            and approval_evidence is null and approved_by is null)),
 check ((status='revoked') = (tombstoned_at is not null))
);
create table public.generated_palette_authority_20261007 (
 tenant_id text not null references public.generated_tenant_epoch_20261007(tenant_id),
 palette_key text not null check (palette_key ~ '[^[:space:]]'),
 colors jsonb not null check (jsonb_typeof(colors)='array' and jsonb_array_length(colors)>0),
 verification_evidence text not null check (verification_evidence ~ '[^[:space:]]'),
 status text not null check (status in ('active','revoked')),
 revision bigint not null check (revision >= 1),
 epoch bigint not null,
 tombstoned_at timestamptz,
 updated_at timestamptz not null default clock_timestamp(),
 primary key (tenant_id, palette_key),
 check ((status='revoked') = (tombstoned_at is not null))
);
create table public.generated_authority_audit_20261007 (
 audit_id bigint generated always as identity primary key,
 tenant_id text not null,
 op text not null check (op in ('source_upsert','source_approve','source_tombstone','palette_upsert','palette_revoke')),
 entity_key text not null,
 epoch bigint not null,
 revision bigint not null,
 payload jsonb not null,
 -- SECURITY DEFINER current_user is the function owner, not the login actor.
 actor text not null default session_user,
 created_at timestamptz not null default clock_timestamp()
);

do $$ begin
 if not exists(select 1 from pg_roles where rolname='generated_authority_owner_20261007') then
  create role generated_authority_owner_20261007 nologin; end if;
 if not exists(select 1 from pg_roles where rolname='generated_authority_publisher_20261007') then
  create role generated_authority_publisher_20261007 nologin; end if;
 if not exists(select 1 from pg_roles where rolname='service_role') then
  raise exception 'expected writer role service_role is missing' using errcode='42501'; end if;
end; $$;
-- Revoke inherited default ACLs, including service_role's realistic ALL grants.
-- BYPASSRLS does not bypass table/sequence ACLs. The migration/function owner
-- remains privileged; do not grant membership in that owner to runtime roles.
do $$ declare t text; begin
 foreach t in array array['generated_tenant_epoch_20261007','generated_source_authority_20261007',
                          'generated_palette_authority_20261007','generated_authority_audit_20261007'] loop
  execute format('alter table public.%I enable row level security', t);
  execute format('revoke all on table public.%I from public, anon, authenticated, service_role,
    generated_authority_owner_20261007, generated_authority_publisher_20261007', t);
 end loop;
end; $$;
revoke all on sequence public.generated_authority_audit_20261007_audit_id_seq
 from public, anon, authenticated, service_role,
 generated_authority_owner_20261007, generated_authority_publisher_20261007;

create function public.generated_authority_immutable_20261007()
returns trigger language plpgsql set search_path=pg_catalog as $$
begin raise exception 'generated authority audit is append-only' using errcode='23514'; end; $$;
revoke all on function public.generated_authority_immutable_20261007() from public, anon, authenticated, service_role;
create trigger audit_immutable_row before update or delete on public.generated_authority_audit_20261007
 for each row execute function public.generated_authority_immutable_20261007();
create trigger audit_immutable_truncate before truncate on public.generated_authority_audit_20261007
 for each statement execute function public.generated_authority_immutable_20261007();
create trigger epoch_immutable_delete before delete on public.generated_tenant_epoch_20261007
 for each row execute function public.generated_authority_immutable_20261007();
create trigger epoch_immutable_truncate before truncate on public.generated_tenant_epoch_20261007
 for each statement execute function public.generated_authority_immutable_20261007();

-- Serialized CAS writer: one epoch per accepted batch, one receipt per op.
-- Sources start pending; every content revision clears prior approval. Approval
-- binds to that exact content revision. Revocation permanently tombstones IDs.
create function public.generated_authority_write_20261007(
 p_tenant_id text, p_expected_epoch bigint, p_ops jsonb)
returns bigint language plpgsql security definer set search_path=pg_catalog,public as $$
declare cur_epoch bigint; new_epoch bigint; op jsonb; entity_id text;
 src public.generated_source_authority_20261007%rowtype;
 pal public.generated_palette_authority_20261007%rowtype; rev bigint;
begin
 if coalesce(p_tenant_id,'') !~ '[^[:space:]]' or jsonb_typeof(p_ops) is distinct from 'array'
  or jsonb_array_length(p_ops)=0 then
  raise exception 'tenant id and a non-empty op array are required' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'), hashtext(p_tenant_id));
 insert into public.generated_tenant_epoch_20261007(tenant_id) values (p_tenant_id)
  on conflict (tenant_id) do nothing;
 -- Row lock is essential: after another transaction changes this row,
 -- REPEATABLE READ/SERIALIZABLE abort rather than accept an old MVCC snapshot.
 select epoch into cur_epoch from public.generated_tenant_epoch_20261007
  where tenant_id=p_tenant_id for update;
 if cur_epoch is distinct from p_expected_epoch then
  raise exception 'stale epoch: expected %, current %', p_expected_epoch, cur_epoch using errcode='40001'; end if;
 new_epoch := cur_epoch+1;
 for op in select * from jsonb_array_elements(p_ops) loop
  if op->>'op' in ('source_upsert','source_approve','source_tombstone') then
   if jsonb_typeof(op->'source_id') is distinct from 'string'
    or coalesce(op->>'source_id','') !~ '[^[:space:]]' then
    raise exception 'nonblank source_id is required' using errcode='23514'; end if;
   entity_id := op->>'source_id';
   select * into src from public.generated_source_authority_20261007
    where tenant_id=p_tenant_id and source_id=entity_id;
  elsif op->>'op' in ('palette_upsert','palette_revoke') then
   if jsonb_typeof(op->'palette_key') is distinct from 'string'
    or coalesce(op->>'palette_key','') !~ '[^[:space:]]' then
    raise exception 'nonblank palette_key is required' using errcode='23514'; end if;
   entity_id := op->>'palette_key';
   select * into pal from public.generated_palette_authority_20261007
    where tenant_id=p_tenant_id and palette_key=entity_id;
  end if;
  case op->>'op'
  when 'source_upsert' then
   if src.status='revoked' then
    raise exception 'tombstoned source % cannot be resurrected', entity_id using errcode='23514'; end if;
   if coalesce(op->>'category','') !~ '[^[:space:]]'
    or jsonb_typeof(op->'exact_text') is distinct from 'string'
    or jsonb_typeof(op->'citation') is distinct from 'string' then
    raise exception 'source_upsert requires category, exact_text and citation' using errcode='23514'; end if;
   rev := coalesce(src.revision,0)+1;
   insert into public.generated_source_authority_20261007
    (tenant_id,source_id,category,exact_text,citation,status,revision,epoch)
   values (p_tenant_id,entity_id,op->>'category',op->>'exact_text',op->>'citation','pending',rev,new_epoch)
   on conflict (tenant_id,source_id) do update set category=excluded.category,
    exact_text=excluded.exact_text,citation=excluded.citation,status='pending',revision=excluded.revision,
    approval_revision=null,approval_mode=null,approval_evidence=null,approved_by=null,
    epoch=new_epoch,updated_at=clock_timestamp();
  when 'source_approve' then
   if src.source_id is null or src.status='revoked' then
    raise exception 'unknown or revoked source cannot be approved' using errcode='23514'; end if;
   if jsonb_typeof(op->'revision') is distinct from 'number'
    or op->>'revision' !~ '^[1-9][0-9]*$' then
    raise exception 'approval requires exact revision' using errcode='23514'; end if;
   if (op->>'revision')::bigint is distinct from src.revision then
    raise exception 'stale source approval revision' using errcode='40001'; end if;
   if src.status<>'pending' then
    raise exception 'source already approved' using errcode='23514'; end if;
   if op->>'approval_mode' is null or op->>'approval_mode' not in ('explicit','auto')
    or jsonb_typeof(op->'approval_evidence') is distinct from 'string'
    or coalesce(op->>'approval_evidence','') !~ '[^[:space:]]' then
    raise exception 'approval requires explicit/auto mode and evidence' using errcode='23514'; end if;
   rev := src.revision;
   update public.generated_source_authority_20261007 set status='approved',approval_revision=rev,
    approval_mode=op->>'approval_mode',approval_evidence=op->>'approval_evidence',approved_by=session_user,
    epoch=new_epoch,updated_at=clock_timestamp() where tenant_id=p_tenant_id and source_id=entity_id;
  when 'source_tombstone' then
   if src.source_id is null then
    raise exception 'cannot tombstone unknown source %', entity_id using errcode='23514'; end if;
   if src.status='revoked' then
    raise exception 'source % already revoked', entity_id using errcode='23514'; end if;
   rev := src.revision+1;
   update public.generated_source_authority_20261007 set status='revoked',revision=rev,
    approval_revision=null,approval_mode=null,approval_evidence=null,approved_by=null,
    epoch=new_epoch,tombstoned_at=clock_timestamp(),updated_at=clock_timestamp()
    where tenant_id=p_tenant_id and source_id=entity_id;
  when 'palette_upsert' then
   if pal.status='revoked' then
    raise exception 'tombstoned palette cannot be resurrected' using errcode='23514'; end if;
   if jsonb_typeof(op->'verification_evidence') is distinct from 'string'
    or coalesce(op->>'verification_evidence','') !~ '[^[:space:]]'
    or jsonb_typeof(op->'colors') is distinct from 'array' or jsonb_array_length(op->'colors')=0
    or exists(select 1 from jsonb_array_elements(op->'colors') c
              where jsonb_typeof(c) is distinct from 'string' or (c #>> '{}') !~ '^#[0-9a-fA-F]{6}$') then
    raise exception 'palette_upsert requires non-empty hex colors and verification_evidence' using errcode='23514'; end if;
   rev := coalesce(pal.revision,0)+1;
   insert into public.generated_palette_authority_20261007
    (tenant_id,palette_key,colors,verification_evidence,status,revision,epoch)
   values (p_tenant_id,entity_id,op->'colors',op->>'verification_evidence','active',rev,new_epoch)
   on conflict (tenant_id,palette_key) do update set colors=excluded.colors,
    verification_evidence=excluded.verification_evidence,revision=excluded.revision,
    epoch=new_epoch,updated_at=clock_timestamp();
  when 'palette_revoke' then
   if pal.palette_key is null or pal.status='revoked' then
    raise exception 'unknown or already revoked palette' using errcode='23514'; end if;
   rev := pal.revision+1;
   update public.generated_palette_authority_20261007 set status='revoked',revision=rev,
    epoch=new_epoch,tombstoned_at=clock_timestamp(),updated_at=clock_timestamp()
    where tenant_id=p_tenant_id and palette_key=entity_id;
  else raise exception 'unknown op %', op->>'op' using errcode='23514';
  end case;
  insert into public.generated_authority_audit_20261007(tenant_id,op,entity_key,epoch,revision,payload)
   values (p_tenant_id,op->>'op',entity_id,new_epoch,rev,op);
 end loop;
 update public.generated_tenant_epoch_20261007 set epoch=new_epoch where tenant_id=p_tenant_id;
 return new_epoch;
end; $$;

create function public.generated_authority_snapshot_20261007(p_tenant_id text)
returns jsonb language plpgsql security definer stable set search_path=pg_catalog,public as $$
declare e bigint; sources jsonb; palettes jsonb;
begin
 select epoch into e from public.generated_tenant_epoch_20261007 where tenant_id=p_tenant_id;
 select coalesce(jsonb_agg(to_jsonb(s) order by s.source_id),'[]'::jsonb) into sources
 from public.generated_source_authority_20261007 s where s.tenant_id=p_tenant_id;
 select coalesce(jsonb_agg(to_jsonb(p) order by p.palette_key),'[]'::jsonb) into palettes
 from public.generated_palette_authority_20261007 p where p.tenant_id=p_tenant_id;
 return jsonb_build_object('tenant_id',p_tenant_id,'epoch',e,'sources',sources,'palettes',palettes);
end; $$;

-- Hold this transaction through the publish decision. The shared lock orders
-- validation against revocation; FOR UPDATE also defeats stale RR snapshots.
-- Every cited source and the exact palette revision must be supplied.
create function public.generated_authority_validate_publish_20261007(
 p_tenant_id text, p_expected_epoch bigint, p_source_ids text[], p_source_revisions bigint[],
 p_palette_key text, p_palette_revision bigint)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare e bigint;
begin
 if coalesce(p_tenant_id,'') !~ '[^[:space:]]' or p_source_ids is null or cardinality(p_source_ids)=0
  or p_source_revisions is null or cardinality(p_source_revisions)<>cardinality(p_source_ids)
  or exists(select 1 from unnest(p_source_ids) s where coalesce(s,'') !~ '[^[:space:]]')
  or exists(select 1 from unnest(p_source_revisions) r where r is null or r<1)
  or coalesce(p_palette_key,'') !~ '[^[:space:]]' or p_palette_revision is null or p_palette_revision<1 then
  raise exception 'publish requires nonblank source IDs, exact revisions and an exact palette' using errcode='23514'; end if;
 perform pg_advisory_xact_lock(hashtext('generated-authority-20261007'), hashtext(p_tenant_id));
 select epoch into e from public.generated_tenant_epoch_20261007 where tenant_id=p_tenant_id for update;
 if e is null or e is distinct from p_expected_epoch then
  raise exception 'stale epoch at publish validation: expected %, current %', p_expected_epoch,e using errcode='40001'; end if;
 if exists(select 1 from unnest(p_source_ids,p_source_revisions) refs(id,rev)
   where not exists(select 1 from public.generated_source_authority_20261007 a
    where a.tenant_id=p_tenant_id and a.source_id=refs.id and a.revision=refs.rev
     and a.status='approved' and a.approval_revision=refs.rev)) then
  raise exception 'source is unknown, pending, revoked or has stale approval/revision' using errcode='23514'; end if;
 if not exists(select 1 from public.generated_palette_authority_20261007 p
   where p.tenant_id=p_tenant_id and p.palette_key=p_palette_key and p.revision=p_palette_revision
    and p.status='active' and p.verification_evidence ~ '[^[:space:]]') then
  raise exception 'palette is unknown, revoked or has stale revision' using errcode='23514'; end if;
 return jsonb_build_object('tenant_id',p_tenant_id,'epoch',e,'validated',true,
  'source_ids',p_source_ids,'source_revisions',p_source_revisions,
  'palette_key',p_palette_key,'palette_revision',p_palette_revision);
end; $$;

revoke all on function public.generated_authority_write_20261007(text,bigint,jsonb)
 from public, anon, authenticated, generated_authority_owner_20261007, generated_authority_publisher_20261007;
revoke all on function public.generated_authority_snapshot_20261007(text) from public, anon, authenticated;
revoke all on function public.generated_authority_validate_publish_20261007(text,bigint,text[],bigint[],text,bigint)
 from public, anon, authenticated, generated_authority_owner_20261007;
grant execute on function public.generated_authority_write_20261007(text,bigint,jsonb) to service_role;
grant execute on function public.generated_authority_snapshot_20261007(text)
 to generated_authority_owner_20261007, generated_authority_publisher_20261007, service_role;
grant execute on function public.generated_authority_validate_publish_20261007(text,bigint,text[],bigint[],text,bigint)
 to generated_authority_publisher_20261007, service_role;
grant usage on schema public to generated_authority_owner_20261007, generated_authority_publisher_20261007, service_role;
commit;
