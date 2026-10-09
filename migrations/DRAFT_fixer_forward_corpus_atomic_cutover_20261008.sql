-- DRAFT / UNAPPLIED / DEFAULT OFF. No production DDL is authorized.
-- Apply AFTER ALL B P1/calendar/media/publish-caption/LASSO entry migrations.
-- The complete resulting function inventory is a required atomic prerequisite.
-- This is ONE atomic cutover: ordinary statement guards and removal of ONLY
-- the two frozen content_calendar SHARE ROW EXCLUSIVE clauses commit together.
-- Never apply individual sections or install the blocking trigger beforehand.
-- C now excludes every ordinary INSERT/UPDATE/DELETE in the guarded corpus;
-- removing SRE avoids relation -> C versus C -> SRE deadlocks. Volatile RPCs
-- re-read at Read Committed after waiting on C; business predicates are intact.
-- Owner/attester entry already takes its FINAL G exclusive mode before C.
-- Ordinary DML takes G shared -> C. Negative authority tables take FINAL G
-- exclusive -> C (never shared -> exclusive). Statement triggers run after
-- implicit RowExclusive relation acquisition, but before row locks.
-- Lock and statement deadlines belong to the caller; the trigger does not
-- replace a caller's lock_timeout with a shorter function-local deadline.
--
-- The numeric trigger name sorts before owner_photo_corpus_write and the
-- optional #345 generated_inventory_lock. Their existing try-lock checks
-- therefore encounter transaction-owned G/C and succeed reentrantly. Preserve
-- those triggers and their TRUNCATE/authority checks. The optional generated
-- trigger also sorts later when #345 is installed AFTER this B migration.
-- Revoke tenant TRUNCATE explicitly: it otherwise takes AccessExclusive before
-- a statement trigger and can form the same relation/C inversion. No new
-- privileges, approval, tenant, sender, provider or release activation.
--
-- Frozen input hashes are the actual 2026-10-08 B entry draft prosrc bodies,
-- NOT the original production bodies. Identity/owner/security/helper hashes
-- and trigger-order checks fail closed. pg_get_functiondef preserves function
-- options; CREATE OR REPLACE preserves OID, owner and ACL. Each resulting body
-- must equal its frozen input with precisely ONE lock statement line removed.
-- Rollback before use: restore BOTH frozen B definitions and drop all new
-- statement triggers/function in ONE transaction. Do not restore tenant
-- TRUNCATE privileges while any graph/census protocol remains in use.
begin;

-- Freeze corpus DDL/DML for the whole catalog check + atomic cutover. This
-- transaction never acquires G or C, so it cannot wait on advisory authority
-- while holding these relation locks. In-flight writes finish before cutover.
lock table public.content_calendar, public.media_asset, public.media_source,
 public.fixer_forward_media_claim_receipt_20261006,
 public.fixer_forward_media_photo_state_20261007,
 public.fixer_forward_media_photo_key_revocation_20261007,
 public.fixer_owner_photo_revocation_20261007 in access exclusive mode;

do $guard$
declare f record; v_proc record; t text;
begin
 if current_user is distinct from 'postgres' then
  raise exception 'atomic corpus cutover requires postgres' using errcode='23514'; end if;
 -- FULL ENTRY INVENTORY: 22 P2 replacements, helper, four P1 authority
 -- entries, two unchanged delegating wrappers and owner-photo corpus guard.
 -- A missing/legacy row-first entry invalidates blocking statement safety.
 for f in select * from (values
  ('fixer_forward_calendar_entry_lock_20261008','','f7804f90164613bff2e70d7f7bc5b4e2',true,array['search_path=pg_catalog, public']::text[],'void',false,0),
  ('approve_calendar_row_if_media_ready','p_row_id uuid, p_gym_id text, p_expected jsonb','8124b945c06d8adeaa5c63cf4712a73f',true,array['search_path=public']::text[],'setof content_calendar',true,1),
  ('calendar_recover_unproved_approval','p_row_id uuid, p_gym_id text, p_expected jsonb','4d0ac4fd7d68e60e78850dbd7f2a1876',true,array['search_path=public']::text[],'setof content_calendar',true,0),
  ('calendar_stamp_verified_approval','p_gym_id uuid, p_calendar_id uuid, p_clerk_actor_id text, p_echo_approval_digest text','efba82fc5f34a108c7ad96c42dba48ee',true,array['search_path=public']::text[],'table(id uuid, gym_id uuid, clerk_actor_id text, approval_digest text)',true,0),
  ('claim_calendar_gbp_publish_owned','p_row_id uuid, p_gym_id text','5503a8abf954c5fc7967b4c92918111a',true,array['search_path=public']::text[],'setof content_calendar',true,0),
  ('claim_calendar_publish_slot_owned','p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean, p_require_approval_proof boolean','0f0a4ee00e2a31f2e0d3e7c0f59e7e77',true,array['search_path=public']::text[],'uuid',false,1),
  ('content_calendar_swap_variant','p_gym_id text, p_candidate_id uuid, p_actor text','5251cf2b4d6015ac9c66b070af8f4f5d',true,array['search_path=public']::text[],'jsonb',false,1),
  ('record_gym_media_review','p_gym_id text, p_asset_id text, p_expected_hash text, p_expected_status text, p_expected_reviewed_at timestamp with time zone, p_fields jsonb','77a98f3101f79302828328be7b783b45',true,array['search_path=public']::text[],'boolean',false,0),
  ('claim_gym_media_sync','','0ccb81e5836ab7f0d0c40850016ceb84',true,array['search_path=public']::text[],'setof media_source',true,0),
  ('request_gym_media_sync','p_source_id text, p_gym_id text','665c64d946b0c63ebe79fb52a14f0790',true,array['search_path=public']::text[],'boolean',false,0),
  ('finish_gym_media_sync','p_source_id text, p_token text, p_ok boolean, p_error text','7b071942070e868362ff075a5e036d2f',true,array['search_path=public']::text[],'boolean',false,1),
  ('portal_action_receipt_begin','p_gym_id text, p_action_id text, p_action text, p_row_id uuid, p_actor_id text, p_request_fingerprint text','3425d3c0e5d40a10b475e1a432ce293e',true,array['search_path=public']::text[],'portal_action_receipt',false,0),
  ('portal_action_receipt_claim_selection','p_gym_id text, p_action_id text, p_request_fingerprint text, p_selected_asset jsonb, p_planned_siblings jsonb','a4ad7749288d195d5b75c0707696cfb9',true,array['search_path=public']::text[],'portal_action_receipt',false,0),
  ('portal_action_receipt_apply','p_gym_id text, p_action_id text, p_request_fingerprint text, p_prepared jsonb','3e17e0f6392bbc4ba4dd8ea72ff718f8',true,array['search_path=public']::text[],'portal_action_receipt',false,0),
  ('calendar_patch_caption_autonomous_clean','p_row_id uuid, p_gym_id text, p_expected_status text, p_expected_caption text, p_clean_caption text','3c955479952e70aa093a571d41c360bf',true,array['search_path=public']::text[],'setof content_calendar',true,0),
  ('calendar_patch_caption_manual_format','p_row_id uuid, p_gym_id text, p_expected_status text, p_expected_caption text, p_clean_caption text','acc86a7be80cac06ee424b818010aaf6',true,array['search_path=public']::text[],'setof content_calendar',true,0),
  ('claim_calendar_publish_slot','p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean','745b7946b1e0e70c82bda9fe00ff7489',true,array['search_path=public']::text[],'boolean',false,0),
  ('release_lasso_backlog_feed_hold','p_feed_id uuid, p_story_id uuid, p_expected_feed jsonb, p_expected_story jsonb','5fd99dc9400795622446972fa8182328',true,array['search_path=public']::text[],'jsonb',false,0),
  ('release_lasso_paired_story_hold','p_story_id uuid','6e1cbe16ae094ea0b563a8915b75c221',true,array['search_path=public']::text[],'jsonb',false,0),
  ('repair_lasso_paired_story','p_story_id uuid, p_feed_id uuid, p_expected_story jsonb, p_expected_feed jsonb, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone','18c1f2ae9dbbcfe0c4a11ce41253d761',true,array['search_path=public']::text[],'jsonb',false,0),
  ('stage_lasso_campaign_row','p_row jsonb, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_image_sha256 text','9054fad04a968a88a5cfb4db8a23347b',true,array['search_path=public']::text[],'jsonb',false,0),
  ('stage_lasso_paired_story','p_feed_id uuid, p_account text, p_day date, p_slot integer, p_feed_status text, p_feed_caption text, p_feed_image_url text, p_feed_scheduled_at timestamp with time zone, p_feed_logical_post_id uuid, p_story_id uuid, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone','613ed6cbb44e2d460f83b974632efec5',true,array['search_path=public']::text[],'jsonb',false,0),
  ('stage_lasso_third_story','p_feed_id uuid, p_feed_caption text, p_feed_image_url text, p_story_id uuid, p_story_image_url text, p_story_source_url text, p_story_sha256 text, p_source_hash text, p_policy_version text, p_scheduled_at timestamp with time zone, p_caption_hash text','2357e36b0e3f77a08c84cce85ea28164',true,array['search_path=public']::text[],'jsonb',false,1),
  ('fixer_attest_forward_media_20261006','p_calendar_row_id uuid, p_expected_revision text, p_evidence_id uuid, p_source_fingerprint text, p_source_length bigint, p_image_fingerprint text, p_image_length bigint, p_thumbnail_fingerprint text, p_thumbnail_length bigint, p_operation text, p_evidence_ref text','be6aded33788957b4ab8f67cb8c380a7',true,array['search_path=pg_catalog, public','lock_timeout=5s']::text[],'uuid',false,0),
  ('fixer_record_forward_media_observation_20261007','p_calendar_row_id uuid, p_expected_row jsonb, p_observation_json text, p_digest_input text','1f67896a66d76c28e7971248621f13fa',true,array['search_path=pg_catalog, public']::text[],'jsonb',false,0),
  ('fixer_prepare_owner_photo_20261007','p_audit_id uuid, p_original jsonb, p_manifest jsonb','d7db73793bdb072f002d57b83c927cd1',true,array['search_path=pg_catalog, public']::text[],'jsonb',false,0),
  ('fixer_forward_media_provenance_lookup_20261006','p_calendar_row_id uuid','83bd049295540e5e6671e443707f9268',true,array['search_path=pg_catalog, public']::text[],'jsonb',false,0),
  ('fixer_owner_photo_corpus_write_lock_20261007','','12af6fc87e424ab5a5041a4c6d8765d5',true,array['search_path=pg_catalog, public']::text[],'trigger',false,0),
  ('claim_calendar_gbp_publish_with_mode_owned','p_row_id uuid, p_gym_id text','528dcc3c13a98f705cf3c021d0a60dae',true,array['search_path=public']::text[],'jsonb',false,0),
  ('claim_calendar_publish_slot_proven_owned','p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean','c36ca7a901a9a84c772dad34c1c439b2',true,array['search_path=public']::text[],'jsonb',false,0)
 ) as expected(name,identity_args,body_md5,security_definer,config,result,retset,default_count) loop
  if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      where n.nspname='public' and p.proname=f.name) <> 1 then
   raise exception 'atomic corpus cutover function count drift: %',f.name using errcode='23514'; end if;
  select p.*,pg_get_function_identity_arguments(p.oid) as identity_args,
   l.lanname,lower(pg_get_function_result(p.oid)) as result
   into v_proc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   join pg_language l on l.oid=p.prolang
   where n.nspname='public' and p.proname=f.name;
  if v_proc.identity_args is distinct from f.identity_args
   or md5(v_proc.prosrc) is distinct from f.body_md5
   or pg_get_userbyid(v_proc.proowner) is distinct from 'postgres'
   or v_proc.prosecdef is distinct from f.security_definer or v_proc.provolatile <> 'v'
   or v_proc.lanname is distinct from 'plpgsql' or v_proc.prokind <> 'f'
   or v_proc.proisstrict or v_proc.proleakproof or v_proc.proparallel <> 'u'
   or v_proc.proconfig is distinct from f.config
   or v_proc.result is distinct from f.result or v_proc.proretset is distinct from f.retset
   or v_proc.pronargdefaults is distinct from f.default_count then
   raise exception 'atomic corpus cutover definition drift: %',f.name using errcode='23514'; end if;
 end loop;
 -- #345 is stacked later; if already present, only its exact reviewed body
 -- may participate. No conditional deletion or bypass of a drifted guard.
 if exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
           where n.nspname='public' and p.proname='fixer_generated_inventory_lock_20261007') then
  if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      where n.nspname='public' and p.proname='fixer_generated_inventory_lock_20261007') <> 1
   or not exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname='fixer_generated_inventory_lock_20261007'
     and pg_get_function_identity_arguments(p.oid)=''
     and md5(p.prosrc)='8d40842a1c5211262f24b5035e9f4827'
     and pg_get_userbyid(p.proowner)='postgres' and not p.prosecdef) then
   raise exception 'atomic corpus cutover generated inventory drift' using errcode='23514'; end if;
 end if;
 if exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname not in ('portal_action_receipt_apply','stage_lasso_campaign_row')
   and p.prosrc ~* 'lock[[:space:]]+table[[:space:]]+(public[.])?content_calendar[[:space:]]+in[[:space:]]+share[[:space:]]+row[[:space:]]+exclusive') then
  raise exception 'atomic corpus cutover additional calendar SRE function requires review' using errcode='23514'; end if;
 foreach t in array array['content_calendar','media_asset','media_source',
  'fixer_forward_media_claim_receipt_20261006','fixer_forward_media_photo_state_20261007',
  'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007'] loop
  if not exists(select 1 from pg_trigger g
    where g.tgrelid=('public.'||t)::regclass and g.tgname='owner_photo_corpus_write'
     and not g.tgisinternal and g.tgtype=62 and g.tgenabled='O'
     and g.tgfoid='public.fixer_owner_photo_corpus_write_lock_20261007()'::regprocedure) then
   raise exception 'atomic corpus cutover owner-photo trigger drift on %',t using errcode='23514'; end if;
  if t in ('media_asset','media_source')
   and to_regprocedure('public.fixer_generated_inventory_lock_20261007()') is not null
   and not exists(select 1 from pg_trigger g
    where g.tgrelid=('public.'||t)::regclass and g.tgname='generated_inventory_lock'
     and not g.tgisinternal and g.tgtype=62 and g.tgenabled='O'
     and g.tgfoid=to_regprocedure('public.fixer_generated_inventory_lock_20261007()')) then
   raise exception 'atomic corpus cutover generated trigger drift on %',t using errcode='23514'; end if;
  if exists(select 1 from pg_trigger g where g.tgrelid=('public.'||t)::regclass
   and not g.tgisinternal and (g.tgtype & 1)=0 and (g.tgtype & 2)=2
   and (g.tgtype & 28)<>0 and g.tgname collate "C" <= '000_fixer_forward_corpus_entry_20261008' collate "C") then
   raise exception 'atomic corpus cutover earlier statement trigger on %',t using errcode='23514'; end if;
 end loop;
end $guard$;

create function public.fixer_forward_corpus_entry_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public
as $$
declare v_graph bigint := hashtextextended('fixer_forward_graph_20261006',0);
begin
 if current_setting('transaction_isolation') <> 'read committed' then
  raise exception 'forward corpus entry requires read committed' using errcode='25000'; end if;
 if tg_table_name in ('fixer_forward_media_photo_state_20261007',
  'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007') then
  -- An administrator must enter with its final exclusive mode. Refuse a
  -- reused ordinary transaction instead of upgrading its shared graph lock.
  if exists(select 1 from pg_locks l where l.pid=pg_backend_pid() and l.granted
    and l.locktype='advisory' and l.objsubid=1
    and l.classid::bigint=((v_graph>>32)&4294967295)
    and l.objid::bigint=(v_graph&4294967295) and l.mode='ShareLock')
   and not exists(select 1 from pg_locks l where l.pid=pg_backend_pid() and l.granted
    and l.locktype='advisory' and l.objsubid=1
    and l.classid::bigint=((v_graph>>32)&4294967295)
    and l.objid::bigint=(v_graph&4294967295) and l.mode='ExclusiveLock') then
   raise exception 'negative authority requires final exclusive graph entry before ordinary corpus writes'
    using errcode='25000'; end if;
  perform pg_advisory_xact_lock(v_graph);
 else
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 return null;
end; $$;
revoke all on function public.fixer_forward_corpus_entry_20261008()
 from public,anon,authenticated,service_role;

do $install$
declare t text;
begin
 foreach t in array array['content_calendar','media_asset','media_source',
  'fixer_forward_media_claim_receipt_20261006','fixer_forward_media_photo_state_20261007',
  'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007'] loop
  execute format('create trigger "000_fixer_forward_corpus_entry_20261008" before insert or update or delete on public.%I for each statement execute function public.fixer_forward_corpus_entry_20261008()',t);
 end loop;
end $install$;

-- Rewrite the catalog-frozen definitions without reimplementing their bodies.
-- Pin post-rewrite prosrc as well: no removal from comments, extra statements,
-- weakened business checks, signature changes or hidden helper are accepted.
do $remove_only_two_locks$
declare f record; v_oid oid; v_before text; v_definition text; v_clause text;
begin
 for f in select * from (values
  ('portal_action_receipt_apply','  LOCK TABLE public.content_calendar IN SHARE ROW EXCLUSIVE MODE;', '653db99604cb8624b60b563bde0f242f'),
  ('stage_lasso_campaign_row','  lock table public.content_calendar in share row exclusive mode;', '33e8151ec24b71c8859ddfb356c175e4')
 ) as expected(name,lock_line,after_md5) loop
  select p.oid,p.prosrc,pg_get_functiondef(p.oid) into v_oid,v_before,v_definition
   from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='public' and p.proname=f.name;
  v_clause := f.lock_line||chr(10);
  if (length(v_before)-length(replace(v_before,v_clause,'')))/length(v_clause) <> 1 then
   raise exception 'atomic corpus cutover expected exactly one SRE clause in %',f.name using errcode='23514'; end if;
  execute replace(v_definition,v_clause,'');
  if (select md5(prosrc) from pg_proc where oid=v_oid) is distinct from f.after_md5 then
   raise exception 'atomic corpus cutover unexpected rewrite of %',f.name using errcode='23514'; end if;
 end loop;
end $remove_only_two_locks$;

revoke truncate on public.content_calendar, public.media_asset, public.media_source,
 public.fixer_forward_media_claim_receipt_20261006,
 public.fixer_forward_media_photo_state_20261007,
 public.fixer_forward_media_photo_key_revocation_20261007,
 public.fixer_owner_photo_revocation_20261007 from public,anon,authenticated,service_role;

do $privileges$
declare t text; r text;
begin
 foreach t in array array['content_calendar','media_asset','media_source',
  'fixer_forward_media_claim_receipt_20261006','fixer_forward_media_photo_state_20261007',
  'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007'] loop
  foreach r in array array['anon','authenticated','service_role'] loop
   if has_table_privilege(r,'public.'||t,'TRUNCATE') then
    raise exception 'atomic corpus cutover inherited TRUNCATE remains on % for %',t,r using errcode='23514'; end if;
  end loop;
 end loop;
end $privileges$;
commit;
