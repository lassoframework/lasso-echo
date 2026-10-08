-- DRAFT / UNAPPLIED / DEFAULT OFF. Apply LAST after the composed photo,
-- generated, reservation, staged preparation and corpus entry migrations.
-- Frozen 20261006/20261007 migrations remain unchanged. No state, role,
-- approval, sender or provider activation is performed.
-- Schema version 1 cannot prove excluded video frames were visually unused.
-- Both Python evidence and direct SQL authority therefore require zero
-- authoritative baseline exclusions, including old certificates and replay.
-- CREATE OR REPLACE below retains OIDs, owners, ACLs and function options.
-- Later authority replacements require reapplying this additive guard.
-- Rollback before use: restore the saved pre-install function definitions in
-- one transaction and remove these two helpers; never alter signed receipts.

begin;

create or replace function public.fixer_forward_media_photo_snapshot_exclusion_20261008()
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb; b public.fixer_forward_media_photo_baseline_20261007%rowtype;
begin
 snap:=public.fixer_forward_media_photo_snapshot_20261007();
 if coalesce((snap->>'policy_approved')::boolean,false) is not true
   or coalesce((snap->>'scope_complete')::boolean,false) is not true then
  -- Fail closed with the frozen not-approved shape: no exclusion evidence is
  -- ever attached to an unapproved or incomplete scope.
  return jsonb_build_object('policy_approved',false,'scope_complete',false,'rows','[]'::jsonb);
 end if;
 select * into b from public.fixer_forward_media_photo_baseline_20261007
  where baseline_id=(snap->>'baseline_id')::uuid;
 if not found then
  return jsonb_build_object('policy_approved',false,'scope_complete',false,'rows','[]'::jsonb);
 end if;
 return snap||jsonb_build_object(
   'excluded_rows_count',jsonb_array_length(b.excluded_rows_json),
   'excluded_rows_digest','sha256:'||encode(sha256(convert_to(b.excluded_rows_json::text,'UTF8')),'hex'));
end; $$;

-- Same privilege envelope as the frozen read-only snapshot RPC: the
-- independent auditor and the owner may read it; no one else.
revoke all on function public.fixer_forward_media_photo_snapshot_exclusion_20261008()
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_forward_media_photo_snapshot_exclusion_20261008()
 to fixer_forward_media_photo_auditor_20261007,fixer_forward_media_owner_20261006;

-- Read only check: use the selected authoritative baseline, never caller
-- packet fields. Existing graph locks are retained and checked again after
-- acquisition, before replay branches. No new lock upgrade/order is introduced.
create or replace function public.fixer_assert_photo_no_exclusions_20261008()
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if exists(select 1 from public.fixer_forward_media_photo_state_20261007 s
   join public.fixer_forward_media_photo_baseline_20261007 b on b.baseline_id=s.baseline_id
   where s.singleton and jsonb_array_length(b.excluded_rows_json)>0) then
  raise exception 'historical photo exclusions require frame-aware certificate schema'
   using errcode='23514';
 end if;
end; $$;
revoke all on function public.fixer_assert_photo_no_exclusions_20261008()
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,
 fixer_forward_media_photo_auditor_20261007,fixer_forward_media_owner_20261006;

-- Adapt the final composed bodies, rather than rename functions (which can
-- leave earlier dependencies bound to the old OID). Optional later layers
-- are guarded if present; the mandatory certificate entry must exist.
do $guard$
declare f record; original text; revised text; definition text;
 marker text := 'perform public.fixer_assert_photo_no_exclusions_20261008();';
 names text[] := array[
  'fixer_forward_media_photo_record_20261007',
  'fixer_prepare_owner_photo_20261007','fixer_prepare_owner_staged_photo_20261008',
  'fixer_owner_photo_reserve_20261007','fixer_owner_photo_finish_20261007',
  'fixer_owner_photo_pending_20261007','fixer_reconcile_owner_photo_20261007',
  'fixer_forward_schedule_staged_photo_pending_20261008',
  'fixer_forward_media_provenance_lookup_20261006',
  'fixer_photo_base_provenance_20261007','fixer_pre_generated_provenance_20261007',
  'fixer_pre_still_provenance_20261007','fixer_pre_staged_provenance_20261008',
  'fixer_claim_forward_media_internal_20261008',
  'fixer_forward_visual_index_claim_internal_schedule_20261008',
  'reserve_forward_slot_20261008','forward_reservation_proof_20261008',
  'fixer_claim_forward_media_20261006','fixer_forward_visual_index_claim_20261008',
  'finalize_forward_schedule_batch_20261008','finalize_forward_schedule_staged_batch_20261008'
 ];
begin
 if to_regprocedure('public.fixer_forward_media_photo_record_20261007(text,text,text)') is null then
  raise exception 'photo certificate stack required' using errcode='23514';
 end if;
 for f in select p.oid,p.prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   join pg_language l on l.oid=p.prolang
   where n.nspname='public' and p.proname=any(names) and l.lanname='plpgsql'
 loop
  original:=f.prosrc;
  if position(marker in original)>0 then continue; end if;
  revised:=regexp_replace(original,'(^|\n)([ \t]*begin[ \t]*\n)',
    E'\\1\\2 '||marker||E'\n','i');
  if revised=original then raise exception 'unguardable authority body: %',f.oid::regprocedure; end if;
  -- Recheck after every existing graph lock to cover a baseline changed while
  -- waiting. The entry check also fences delegating wrappers and idempotence.
  revised:=regexp_replace(revised,
    '(perform pg_advisory_xact_lock[^;]*fixer_forward_graph_20261006[^;]*;)',
    E'\\1\n '||marker,'gi');
  definition:=pg_get_functiondef(f.oid);
  if position(original in definition)=0 then raise exception 'function body not found'; end if;
  execute replace(definition,original,revised);
 end loop;
end; $guard$;

commit;
