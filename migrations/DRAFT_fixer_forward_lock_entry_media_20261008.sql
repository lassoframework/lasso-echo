-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- FORWARD LOCK ENTRY, media/receipt tranche (2026-10-08).
--
-- Ordering contract: inside each covered function, acquire
--   G: pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0))
--   C: pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0))
-- in that order, at the TOP of the function body, BEFORE any existing advisory
-- lock, row lock (FOR UPDATE / FOR SHARE / SKIP LOCKED), or table lock. This
-- matches the established attester/claim/binder entry order in
-- DRAFT_fixer_forward_media_claim_20261006.sql and
-- DRAFT_fixer_owner_photo_clearance_20261007.sql; no runtime ever upgrades a
-- shared graph hold, so a fixed G-then-C entry cannot deadlock against them.
--
-- INTER-CHILD NOTE (child 1): the ordinary G-then-C entry sequence is
-- delegated to the shared least-privilege helper defined by child 0:
--   public.fixer_forward_calendar_entry_lock_20261008() returns void,
--   plpgsql, security definer, set search_path=pg_catalog,public; it raises
--   25000 unless isolation is read committed, then takes G (shared) then C
--   (exclusive). EXECUTE is revoked from public/anon/authenticated/
--   service_role; these SECURITY DEFINER functions reach it as their owner.
-- ORDERING DEPENDENCY: apply
-- migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql FIRST; the
-- precondition guard below aborts this migration if the helper is absent.
--
-- Production drift guard: the DO block below aborts the migration unless every
-- covered public function has its exact frozen CREATE identity arguments
-- validated by evidence/portal-entry-function-identities-20261008.json,
-- owner postgres, current_user postgres, and frozen 2026-10-08 inventory hash
-- (md5(prosrc), per portal-legacy-function-inventory-20261008.json). Bodies
-- below are the frozen production definitions with ONLY the forward lock entry
-- inserted; signatures, SECURITY settings, validation, tenant/idempotency/
-- approval checks and return contracts are unchanged. No provider (external
-- API) call exists inside any of these function bodies.
begin;

-- ---------------------------------------------------------------------------
-- Precondition: production definitions must equal the frozen 2026-10-08
-- snapshot before any CREATE OR REPLACE below runs. Fails closed on drift or
-- on a missing/overloaded function.
-- ---------------------------------------------------------------------------
do $guard$
declare
  v_expected constant jsonb := '{
    "record_gym_media_review": "626aedcebdd94728ead0475897246004",
    "claim_gym_media_sync": "f218677db408370afc141b3163767cb6",
    "request_gym_media_sync": "12b2084799cf8c5f9084096f62ef3f3b",
    "finish_gym_media_sync": "7b98042e5e057b97821789b62e6d89d2",
    "portal_action_receipt_begin": "8f77330dc8cb84a2f1e60d96544c4337",
    "portal_action_receipt_claim_selection": "d7e91f2feee4ae48b307272f55279c5e",
    "portal_action_receipt_apply": "832506e101c49f4c39ff7b016cefa4e3"
  }'::jsonb;
  -- Exact identity arguments are from the frozen CREATE headers; defaults
  -- are excluded by pg_get_function_identity_arguments.
  v_expected_identity constant jsonb := '{
    "record_gym_media_review": "p_gym_id text, p_asset_id text, p_expected_hash text, p_expected_status text, p_expected_reviewed_at timestamp with time zone, p_fields jsonb",
    "claim_gym_media_sync": "",
    "request_gym_media_sync": "p_source_id text, p_gym_id text",
    "finish_gym_media_sync": "p_source_id text, p_token text, p_ok boolean, p_error text",
    "portal_action_receipt_begin": "p_gym_id text, p_action_id text, p_action text, p_row_id uuid, p_actor_id text, p_request_fingerprint text",
    "portal_action_receipt_claim_selection": "p_gym_id text, p_action_id text, p_request_fingerprint text, p_selected_asset jsonb, p_planned_siblings jsonb",
    "portal_action_receipt_apply": "p_gym_id text, p_action_id text, p_request_fingerprint text, p_prepared jsonb"
  }'::jsonb;
  v_identity text;
  v_owner text;
  v_helper_valid boolean;
  v_name text;
  v_md5 text;
  v_count integer;
begin
  if current_user is distinct from 'postgres' then
    raise exception 'forward lock entry drift guard: current_user must be postgres' using errcode = '23514';
  end if;
  -- ORDERING DEPENDENCY: child 0's calendar tranche migration must be applied
  -- first. Abort unless its shared least-privilege entry helper exists exactly
  -- once as a zero-argument, void-returning SECURITY DEFINER function.
  select count(*), bool_and(
           p.pronargs = 0
           and pg_get_function_identity_arguments(p.oid) = ''
           and pg_get_userbyid(p.proowner) = 'postgres'
           and t.typname = 'void'
           and p.prosecdef) into v_count, v_helper_valid
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    join pg_type t on t.oid = p.prorettype
   where n.nspname = 'public'
     and p.proname = 'fixer_forward_calendar_entry_lock_20261008';
  if v_count is distinct from 1 or v_helper_valid is distinct from true then
    raise exception
      'forward lock entry drift guard: apply DRAFT_fixer_forward_lock_entry_calendar_20261008.sql first; helper public.fixer_forward_calendar_entry_lock_20261008() missing or mis-shaped (count %)',
      v_count using errcode = '23514';
  end if;
  for v_name in select jsonb_object_keys(v_expected) loop
    select count(*), md5(min(p.prosrc)),
           max(pg_get_function_identity_arguments(p.oid)), max(pg_get_userbyid(p.proowner))
      into v_count, v_md5, v_identity, v_owner
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = v_name;
    if v_count is distinct from 1 then
      raise exception
        'forward lock entry drift guard: function % missing or overloaded (count %)',
        v_name, v_count using errcode = '23514';
    end if;
    if v_md5 is distinct from v_expected->>v_name
        or v_identity is distinct from v_expected_identity->>v_name
        or v_owner is distinct from 'postgres' then
      raise exception
        'forward lock entry drift guard: production definition of % drifted (prosrc md5 %, identity %, owner %)',
        v_name, v_md5, v_identity, v_owner using errcode = '23514';
    end if;
  end loop;
end
$guard$;

-- ---------------------------------------------------------------------------
-- record_gym_media_review
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.record_gym_media_review(p_gym_id text, p_asset_id text, p_expected_hash text, p_expected_status text, p_expected_reviewed_at timestamp with time zone, p_fields jsonb)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  current_asset public.media_asset%ROWTYPE;
  new_status text;
  new_at timestamptz;
BEGIN
  -- FORWARD LOCK ENTRY (2026-10-08): the shared least-privilege helper takes
  -- graph shared, then census exclusive, in that fixed order and never upgrades.
  -- before any other advisory or row lock. Never upgraded; xact-scoped.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  IF p_gym_id IS NULL OR p_asset_id IS NULL OR
     coalesce(btrim(p_expected_hash), '') = '' OR
     jsonb_typeof(p_fields) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT * INTO current_asset FROM public.media_asset
    WHERE id = p_asset_id AND gym_id = p_gym_id FOR UPDATE;
  IF NOT FOUND OR current_asset.content_hash IS DISTINCT FROM p_expected_hash OR
     current_asset.review_status IS DISTINCT FROM p_expected_status OR
     current_asset.reviewed_at IS DISTINCT FROM p_expected_reviewed_at OR
     (p_fields->>'review_content_hash') IS DISTINCT FROM p_expected_hash THEN
    RETURN false;
  END IF;
  new_status := p_fields->>'review_status';
  IF new_status IS NULL OR
     new_status NOT IN ('approved', 'rejected', 'pending_review') OR
     coalesce(btrim(p_fields->>'reviewed_by'), '') = '' OR
     coalesce(btrim(p_fields->>'reviewed_at'), '') = '' THEN
    RETURN false;
  END IF;
  new_at := (p_fields->>'reviewed_at')::timestamptz;
  UPDATE public.media_asset SET
    review_status = new_status,
    reviewed_by = p_fields->>'reviewed_by',
    reviewed_at = new_at,
    review_note = p_fields->>'review_note',
    review_content_hash = p_expected_hash,
    consent_status = CASE WHEN p_fields ? 'consent_status'
      THEN p_fields->>'consent_status' ELSE consent_status END,
    release_ref = CASE WHEN p_fields ? 'release_ref'
      THEN p_fields->>'release_ref' ELSE release_ref END,
    consent_member_ref = CASE WHEN p_fields ? 'consent_member_ref'
      THEN p_fields->>'consent_member_ref' ELSE consent_member_ref END,
    consent_expires_at = CASE WHEN p_fields ? 'consent_expires_at'
      THEN (p_fields->>'consent_expires_at')::timestamptz
      ELSE consent_expires_at END
    WHERE id = p_asset_id AND gym_id = p_gym_id;
  INSERT INTO public.media_asset_review_event
    (gym_id, asset_id, content_hash, prior_status, decision,
     reviewed_by, reviewed_at, review_note)
  VALUES (p_gym_id, p_asset_id, p_expected_hash, current_asset.review_status,
          new_status, p_fields->>'reviewed_by', new_at, p_fields->>'review_note');
  RETURN true;
END;
$function$
;

-- ---------------------------------------------------------------------------
-- claim_gym_media_sync
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_gym_media_sync()
 RETURNS SETOF media_source
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
BEGIN
  -- FORWARD LOCK ENTRY (2026-10-08): the shared least-privilege helper takes
  -- graph shared, then census exclusive, in that fixed order and never upgrades.
  -- before the FOR UPDATE SKIP LOCKED pick below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  -- Deliberately never steal an indexing claim by age. A long-running worker
  -- may still be writing assets; reclaiming it would create concurrent writers.
  -- After a confirmed worker crash, an operator must inspect and requeue that
  -- specific source (sync_status='queued', sync_claim_token=NULL) manually.
  RETURN QUERY WITH pick AS (
    SELECT id FROM media_source
    WHERE active AND kind = 'gym_drive' AND sync_requested_at IS NOT NULL
      AND sync_status = 'queued'
    ORDER BY sync_requested_at LIMIT 1 FOR UPDATE SKIP LOCKED
  ) UPDATE media_source s SET sync_status = 'indexing', sync_started_at = clock_timestamp(),
      sync_claim_token = md5(random()::text || clock_timestamp()::text), sync_error = NULL
    FROM pick WHERE s.id = pick.id RETURNING s.*;
END $function$
;

-- ---------------------------------------------------------------------------
-- request_gym_media_sync
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.request_gym_media_sync(p_source_id text, p_gym_id text)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE changed integer;
BEGIN
  -- FORWARD LOCK ENTRY (2026-10-08): the shared least-privilege helper takes
  -- graph shared, then census exclusive, in that fixed order and never upgrades.
  -- before the row-locking UPDATE below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  UPDATE media_source SET sync_requested_at = clock_timestamp(),
    sync_status = CASE WHEN sync_status = 'indexing' THEN 'indexing' ELSE 'queued' END,
    sync_error = NULL
  WHERE id = p_source_id AND gym_id = p_gym_id AND active AND kind = 'gym_drive';
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed > 0;
END $function$
;

-- ---------------------------------------------------------------------------
-- finish_gym_media_sync
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.finish_gym_media_sync(p_source_id text, p_token text, p_ok boolean, p_error text DEFAULT NULL::text)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE changed integer;
BEGIN
  -- FORWARD LOCK ENTRY (2026-10-08): the shared least-privilege helper takes
  -- graph shared, then census exclusive, in that fixed order and never upgrades.
  -- before the row-locking UPDATE below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  UPDATE media_source SET
    sync_status = CASE WHEN sync_requested_at > sync_started_at THEN 'queued'
                       WHEN p_ok THEN 'ready' ELSE 'failed' END,
    sync_finished_at = clock_timestamp(),
    sync_error = CASE WHEN p_ok THEN NULL ELSE left(coalesce(p_error, 'sync failed'), 120) END,
    sync_claim_token = NULL
  WHERE id = p_source_id AND sync_claim_token = p_token AND sync_status = 'indexing';
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed > 0;
END $function$
;

-- ---------------------------------------------------------------------------
-- portal_action_receipt_begin
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.portal_action_receipt_begin(p_gym_id text, p_action_id text, p_action text, p_row_id uuid, p_actor_id text, p_request_fingerprint text)
 RETURNS portal_action_receipt
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_receipt public.portal_action_receipt;
  v_row public.content_calendar;
  v_logical uuid;
  v_before jsonb;
BEGIN
  -- FORWARD LOCK ENTRY (2026-10-08): the shared least-privilege helper takes
  -- graph shared, then census exclusive, in that fixed order and never upgrades.
  -- before the receipt FOR UPDATE lock and every later row lock.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  IF nullif(btrim(p_gym_id), '') IS NULL
     OR nullif(btrim(p_action_id), '') IS NULL OR length(p_action_id) > 128
     OR nullif(btrim(p_action), '') IS NULL
     OR p_row_id IS NULL
     OR p_request_fingerprint IS NULL
     OR p_request_fingerprint !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid receipt begin arguments' USING ERRCODE = '22023';
  END IF;

  -- Existing receipt FIRST, before any calendar lookup: replay /
  -- conflicting-reuse is resolved from the immutable binding alone, so a
  -- replay aimed at a since-removed, nonexistent or historical-NULL row
  -- still returns the stored binding (exact replay) or raises the immutable
  -- action conflict -- never a manual-review or missing-row error. SELECT
  -- FOR UPDATE also waits out a concurrent in-flight INSERT of the same key.
  SELECT * INTO v_receipt FROM public.portal_action_receipt
   WHERE gym_id = p_gym_id AND action_id = p_action_id
   FOR UPDATE;
  IF FOUND THEN
    IF v_receipt.action IS DISTINCT FROM p_action
       OR v_receipt.row_id IS DISTINCT FROM p_row_id
       OR v_receipt.actor_id IS DISTINCT FROM coalesce(p_actor_id, '')
       OR v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
      RAISE EXCEPTION 'conflicting reuse of action_id with a different request binding'
        USING ERRCODE = '23514';
    END IF;
    RETURN v_receipt;
  END IF;

  -- Genuinely new action: SQL begin is authoritative for the first
  -- before_state. No caller-supplied before image is ever accepted; the
  -- own-tenant primary row is read HERE, in the same transaction that
  -- inserts the receipt, and the persisted snapshot is captured from it.
  -- The primary must carry a non-null logical_post_id; a historical NULL
  -- (unbackfilled) or missing row is HELD: no receipt is inserted and the
  -- action is routed to manual review. Sibling grouping by date/media
  -- inference is never a fallback.
  SELECT c.* INTO v_row
    FROM public.content_calendar c
   WHERE c.id = p_row_id AND c.gym_id = p_gym_id;
  v_logical := v_row.logical_post_id;
  IF v_logical IS NULL THEN
    RAISE EXCEPTION
      'primary row has no logical_post_id; conflicting request held for manual review'
      USING ERRCODE = '23514';
  END IF;
  v_before := jsonb_build_object(
    'id', v_row.id::text, 'status', v_row.status,
    'caption', v_row.caption, 'post_date', v_row.post_date::text,
    'format', v_row.format, 'image_url', v_row.image_url,
    'thumbnail_url', v_row.thumbnail_url,
    'source_media_url', v_row.source_media_url,
    'source_media_asset_id', v_row.source_media_asset_id);

  -- Race-safe insert: a concurrent begin of the same key loses ON CONFLICT
  -- DO NOTHING and falls through to the locked binding re-check below.
  INSERT INTO public.portal_action_receipt
    (gym_id, action_id, action, row_id, actor_id, request_fingerprint,
     status, before_state)
  VALUES
    (p_gym_id, p_action_id, p_action, p_row_id, coalesce(p_actor_id, ''),
     p_request_fingerprint, 'started', v_before)
  ON CONFLICT (gym_id, action_id) DO NOTHING
  RETURNING * INTO v_receipt;
  IF FOUND THEN
    RETURN v_receipt;
  END IF;

  -- Lost the insert race: wait out the winner's INSERT and apply the same
  -- immutable binding check, so a duplicate begin can never observe its own
  -- action as missing and a racing conflicting binding still raises.
  SELECT * INTO v_receipt FROM public.portal_action_receipt
   WHERE gym_id = p_gym_id AND action_id = p_action_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'receipt insert conflicted but no receipt is visible'
      USING ERRCODE = 'P0002';
  END IF;
  IF v_receipt.action IS DISTINCT FROM p_action
     OR v_receipt.row_id IS DISTINCT FROM p_row_id
     OR v_receipt.actor_id IS DISTINCT FROM coalesce(p_actor_id, '')
     OR v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
    RAISE EXCEPTION 'conflicting reuse of action_id with a different request binding'
      USING ERRCODE = '23514';
  END IF;
  RETURN v_receipt;
END;
$function$
;

-- ---------------------------------------------------------------------------
-- portal_action_receipt_claim_selection
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.portal_action_receipt_claim_selection(p_gym_id text, p_action_id text, p_request_fingerprint text, p_selected_asset jsonb, p_planned_siblings jsonb)
 RETURNS portal_action_receipt
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_receipt public.portal_action_receipt;
  v_asset jsonb;
  v_asset_row public.media_asset;
  v_origin text;
  v_base text;
  v_slug text;
  v_siblings jsonb := '{}'::jsonb;
  v_entry record;
  v_media jsonb;
  v_row_gym text;
  v_key text;
  v_logical uuid;
  v_members jsonb;
  v_manifest jsonb;
BEGIN
  -- FORWARD LOCK ENTRY (2026-10-08): the shared least-privilege helper takes
  -- graph shared, then census exclusive, in that fixed order and never upgrades.
  -- before the receipt FOR UPDATE lock that serializes concurrent claimants.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  SELECT * INTO v_receipt FROM public.portal_action_receipt
   WHERE gym_id = p_gym_id AND action_id = p_action_id
   FOR UPDATE;  -- serializes concurrent claimants for the full CAS
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no receipt for this tenant and action_id'
      USING ERRCODE = 'P0002';
  END IF;
  IF v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
    RAISE EXCEPTION 'request fingerprint does not match the receipt binding'
      USING ERRCODE = '23514';
  END IF;
  -- Terminal or already-selected: the loser / replayer receives the stored
  -- (winner) selection and manifest. A frozen selection is never overwritten.
  IF v_receipt.status IN ('succeeded', 'failed') OR v_receipt.selected_asset IS NOT NULL THEN
    RETURN v_receipt;
  END IF;
  IF v_receipt.status NOT IN ('started', 'uncertain') THEN
    RAISE EXCEPTION 'receipt is not claimable in status %', v_receipt.status
      USING ERRCODE = '23514';
  END IF;

  -- Trusted public origin, operator-configured and NEVER caller-controlled.
  -- Fail CLOSED while unset or malformed: no URL identity is accepted until
  -- the operator's rollout seed lands.
  SELECT value INTO v_origin FROM public.portal_action_receipt_config
   WHERE key = 'public_origin';
  IF v_origin IS NULL
     OR v_origin !~ '^https://[a-z0-9.-]+(:[0-9]+)?$' THEN
    RAISE EXCEPTION 'portal action receipts have no trusted public media origin configured; refusing closed'
      USING ERRCODE = '23514';
  END IF;

  -- Candidate validation: strict key allowlist, no picker dict, no local path,
  -- no raw response. Only asset_id + public object identity keys survive.
  IF jsonb_typeof(p_selected_asset) IS DISTINCT FROM 'object'
     OR EXISTS (SELECT 1 FROM jsonb_object_keys(p_selected_asset) k
                WHERE k NOT IN ('asset_id', 'image_url', 'source_media_url',
                                'thumbnail_url', 'kind', 'key'))
     OR nullif(btrim(coalesce(p_selected_asset->>'asset_id', '')), '') IS NULL
     OR NOT public.portal_action_receipt_clean_url(p_selected_asset->>'image_url')
     OR (p_selected_asset ? 'source_media_url'
         AND p_selected_asset->>'source_media_url' IS NOT NULL
         AND NOT public.portal_action_receipt_clean_url(p_selected_asset->>'source_media_url'))
     OR (p_selected_asset ? 'thumbnail_url'
         AND p_selected_asset->>'thumbnail_url' IS NOT NULL
         AND NOT public.portal_action_receipt_clean_url(p_selected_asset->>'thumbnail_url')) THEN
    RAISE EXCEPTION 'selected asset must be an allowlisted public object identity'
      USING ERRCODE = '22023';
  END IF;
  -- Own-tenant public object allowlist. A 'local:<library-key>' id is the
  -- local-library provenance minted ONLY by the service-role swap picker from
  -- the tenant's own library root (no media_asset row exists for it); every
  -- other id must be an own-tenant, still-ELIGIBLE, not coach-excluded
  -- media_asset. The asset row is locked FOR UPDATE so an eligibility or
  -- exclusion flip cannot race this claim.
  IF p_selected_asset->>'asset_id' NOT LIKE 'local:%' THEN
    SELECT * INTO v_asset_row FROM public.media_asset
     WHERE id = p_selected_asset->>'asset_id' AND gym_id = p_gym_id
       AND eligible IS TRUE
       AND excluded_by_coach IS NOT TRUE
     FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'selected asset is not an own-tenant allowlisted media asset'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  -- Hosted-URL tenant binding to the TRUSTED configured origin: every URL
  -- this pipeline mints is a tenant-scoped, content-addressed Echo object
  -- on the operator-configured public origin
  -- (media_host._build_key/_public_url: <origin>/echo/<slug(gym
  -- base)>/<sha1-16>/<file> for Drive picks,
  -- <origin>/echo/<slug(base)_ig>/<sha1-16>/<file> for local picks and
  -- story re-burns). A valid own asset id can never launder a cross-gym or
  -- foreign-host public URL. strpos (literal, position 1), not LIKE:
  -- tenant slugs may contain the LIKE wildcard '_'.
  v_base := regexp_replace(p_gym_id, '_(ig|fb|gbp)$', '');
  v_slug := lower(regexp_replace(v_base, '[^a-z0-9_-]+', '-', 'g'));
  v_slug := regexp_replace(v_slug, '-{2,}', '-', 'g');
  v_slug := btrim(v_slug, '-_');
  IF v_slug = '' THEN
    v_slug := 'tenant';
  END IF;
  IF NOT public.portal_action_receipt_hosted_url(p_selected_asset->>'image_url', v_origin, v_slug)
     OR (p_selected_asset ? 'source_media_url'
         AND p_selected_asset->>'source_media_url' IS NOT NULL
         AND NOT public.portal_action_receipt_hosted_url(p_selected_asset->>'source_media_url', v_origin, v_slug))
     OR (p_selected_asset ? 'thumbnail_url'
         AND p_selected_asset->>'thumbnail_url' IS NOT NULL
         AND NOT public.portal_action_receipt_hosted_url(p_selected_asset->>'thumbnail_url', v_origin, v_slug)) THEN
    RAISE EXCEPTION 'selected media URL is not under this tenant''s hosted media prefix'
      USING ERRCODE = '23514';
  END IF;
  v_asset := jsonb_build_object(
    'asset_id', p_selected_asset->>'asset_id',
    'image_url', p_selected_asset->>'image_url',
    'source_media_url', p_selected_asset->>'source_media_url',
    'thumbnail_url', p_selected_asset->>'thumbnail_url',
    'kind', p_selected_asset->>'kind',
    'key', p_selected_asset->>'key');

  -- Planned sibling identity: map of own-tenant calendar row uuid -> allowlisted
  -- per-row media identity. No other keys are retained.
  IF p_planned_siblings IS NOT NULL THEN
    IF jsonb_typeof(p_planned_siblings) IS DISTINCT FROM 'object' THEN
      RAISE EXCEPTION 'planned siblings must be a row-id to media identity map'
        USING ERRCODE = '22023';
    END IF;
    FOR v_entry IN SELECT key, value FROM jsonb_each(p_planned_siblings) LOOP
      v_key := v_entry.key;
      v_media := v_entry.value;
      IF v_key !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
         OR jsonb_typeof(v_media) IS DISTINCT FROM 'object'
         OR EXISTS (SELECT 1 FROM jsonb_object_keys(v_media) k
                    WHERE k NOT IN ('image_url', 'source_media_url',
                                    'thumbnail_url', 'source_media_asset_id'))
         OR NOT public.portal_action_receipt_clean_url(v_media->>'image_url')
         OR (v_media ? 'source_media_url' AND v_media->>'source_media_url' IS NOT NULL
             AND NOT public.portal_action_receipt_clean_url(v_media->>'source_media_url'))
         OR (v_media ? 'thumbnail_url' AND v_media->>'thumbnail_url' IS NOT NULL
             AND NOT public.portal_action_receipt_clean_url(v_media->>'thumbnail_url')) THEN
        RAISE EXCEPTION 'planned sibling identity is not allowlisted public media'
          USING ERRCODE = '22023';
      END IF;
      SELECT gym_id INTO v_row_gym FROM public.content_calendar WHERE id = v_key::uuid;
      IF v_row_gym IS DISTINCT FROM p_gym_id THEN
        RAISE EXCEPTION 'planned sibling row is not owned by this tenant'
          USING ERRCODE = '23514';
      END IF;
      -- Same trusted-origin hosted-URL tenant binding as the selected
      -- asset (image and any present source/thumbnail URL).
      IF NOT public.portal_action_receipt_hosted_url(v_media->>'image_url', v_origin, v_slug)
         OR (v_media ? 'source_media_url' AND v_media->>'source_media_url' IS NOT NULL
             AND NOT public.portal_action_receipt_hosted_url(v_media->>'source_media_url', v_origin, v_slug))
         OR (v_media ? 'thumbnail_url' AND v_media->>'thumbnail_url' IS NOT NULL
             AND NOT public.portal_action_receipt_hosted_url(v_media->>'thumbnail_url', v_origin, v_slug)) THEN
        RAISE EXCEPTION 'planned sibling URL is not under this tenant''s hosted media prefix'
          USING ERRCODE = '23514';
      END IF;
      v_siblings := v_siblings || jsonb_build_object(v_key, v_media);
    END LOOP;
  END IF;

  -- Freeze the EXACT active member manifest at selection: every own-tenant row
  -- sharing the primary's logical_post_id with variant_status = 'active', each
  -- with its own identity and before-state. No date/media inference; a Story
  -- rendition with different media is included, an unrelated post with the
  -- same date and asset is not, archived/candidate variants are excluded.
  SELECT c.logical_post_id INTO v_logical
    FROM public.content_calendar c
   WHERE c.id = v_receipt.row_id AND c.gym_id = p_gym_id;
  IF v_logical IS NULL THEN
    RAISE EXCEPTION
      'primary row has no logical_post_id; conflicting request held for manual review'
      USING ERRCODE = '23514';
  END IF;
  SELECT jsonb_agg(jsonb_build_object(
           'calendar_row_id', c.id::text,
           'format', c.format,
           'account', c.account,
           'post_date', c.post_date::text,
           'status', c.status,
           'caption', c.caption,
           'image_url', c.image_url,
           'thumbnail_url', c.thumbnail_url,
           'source_media_url', c.source_media_url,
           'source_media_asset_id', c.source_media_asset_id)
           ORDER BY c.id)
    INTO v_members
    FROM public.content_calendar c
   WHERE c.gym_id = p_gym_id
     AND c.logical_post_id = v_logical
     AND c.variant_status = 'active';
  IF v_members IS NULL
     OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_members) m
                    WHERE (m.value->>'calendar_row_id')::uuid = v_receipt.row_id) THEN
    RAISE EXCEPTION 'primary row is not in its own active logical-post group'
      USING ERRCODE = '23514';
  END IF;
  v_manifest := jsonb_build_object(
    'logical_post_id', v_logical::text,
    'members', v_members);

  -- The CAS itself: exclusive transition from started/unselected. The FOR
  -- UPDATE row lock above makes two concurrent claims serialize; the predicate
  -- is the backstop. The manifest freezes WITH the selection, atomically.
  UPDATE public.portal_action_receipt
     SET selected_asset = v_asset, planned_siblings = v_siblings,
         member_manifest = v_manifest,
         status = 'selected'
   WHERE id = v_receipt.id
     AND status IN ('started', 'uncertain')
     AND selected_asset IS NULL
  RETURNING * INTO v_receipt;
  IF NOT FOUND THEN
    -- Lost the race between lock and update: return the winner's selection.
    SELECT * INTO v_receipt FROM public.portal_action_receipt
     WHERE id = v_receipt.id;
  END IF;
  RETURN v_receipt;
END;
$function$
;

-- ---------------------------------------------------------------------------
-- portal_action_receipt_apply
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.portal_action_receipt_apply(p_gym_id text, p_action_id text, p_request_fingerprint text, p_prepared jsonb)
 RETURNS portal_action_receipt
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_receipt public.portal_action_receipt;
  v_manifest jsonb;
  v_members jsonb;
  v_member jsonb;
  v_logical uuid;
  v_ids uuid[];
  v_id uuid;
  v_current_ids uuid[];
  v_row public.content_calendar;
  v_media jsonb;
  v_outcomes jsonb := '[]'::jsonb;
  v_after public.content_calendar;
  v_intended jsonb;
  v_persisted public.content_calendar;
  v_n integer;
BEGIN
  -- FORWARD LOCK ENTRY (2026-10-08): the shared least-privilege helper takes
  -- graph shared, then census exclusive, in that fixed order and never upgrades.
  -- before the receipt lock, the SHARE ROW EXCLUSIVE table lock and the
  -- deterministic sibling row locks below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  -- Receipt lock first: one apply per action at a time. The new prepared
  -- payload is deliberately NOT validated before this: replay of an
  -- already-terminal receipt returns the stored result for the exact
  -- binding regardless of any (malformed or missing) new payload, and a
  -- fingerprint mismatch is reported as the immutable binding conflict,
  -- never masked by a payload-shape error.
  SELECT * INTO v_receipt FROM public.portal_action_receipt
   WHERE gym_id = p_gym_id AND action_id = p_action_id
   FOR UPDATE;  -- receipt lock first: one apply per action at a time
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no receipt for this tenant and action_id'
      USING ERRCODE = 'P0002';
  END IF;
  IF v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
    RAISE EXCEPTION 'request fingerprint does not match the receipt binding'
      USING ERRCODE = '23514';
  END IF;
  -- Idempotent replay: a terminal receipt is returned exactly as persisted. A
  -- lost RPC response is reconciled by this return, never by a rewrite, and a
  -- timed-out write is NEVER reclassified as failed from a row read.
  IF v_receipt.status IN ('succeeded', 'failed') THEN
    RETURN v_receipt;
  END IF;

  IF jsonb_typeof(p_prepared) IS DISTINCT FROM 'object'
     OR jsonb_typeof(p_prepared->'rows') IS DISTINCT FROM 'array'
     OR jsonb_array_length(p_prepared->'rows') = 0 THEN
    RAISE EXCEPTION 'prepared swap must name a non-empty row set'
      USING ERRCODE = '22023';
  END IF;
  IF v_receipt.status <> 'selected' OR v_receipt.selected_asset IS NULL
     OR v_receipt.member_manifest IS NULL THEN
    RAISE EXCEPTION 'receipt has no claimed selection and frozen manifest to apply'
      USING ERRCODE = '23514';
  END IF;

  v_manifest := v_receipt.member_manifest;
  v_members := v_manifest->'members';
  v_logical := (v_manifest->>'logical_post_id')::uuid;
  IF v_logical IS NULL THEN
    RAISE EXCEPTION
      'frozen manifest has no logical_post_id; conflicting request held for manual review'
      USING ERRCODE = '23514';
  END IF;

  -- The prepared row set must equal the frozen manifest row set exactly: no
  -- repick, no omission, no extra.
  SELECT array_agg((e.value->>'calendar_row_id')::uuid
                   ORDER BY (e.value->>'calendar_row_id')::uuid)
    INTO v_ids
    FROM jsonb_array_elements(p_prepared->'rows') e;
  IF v_ids IS NULL
     OR cardinality(v_ids) <> (SELECT count(DISTINCT id) FROM unnest(v_ids) a(id)) THEN
    RAISE EXCEPTION 'prepared rows must name each row exactly once'
      USING ERRCODE = '22023';
  END IF;
  IF v_ids IS DISTINCT FROM (
       SELECT array_agg((m.value->>'calendar_row_id')::uuid
                        ORDER BY (m.value->>'calendar_row_id')::uuid)
         FROM jsonb_array_elements(v_members) m) THEN
    RAISE EXCEPTION 'prepared rows must match the frozen member manifest exactly'
      USING ERRCODE = '23514';
  END IF;

  -- PHANTOM EXCLUSION (documented lock strategy): SHARE ROW EXCLUSIVE
  -- conflicts with ROW EXCLUSIVE, so no concurrent INSERT/UPDATE/DELETE on
  -- content_calendar can commit for the rest of this transaction; a newly
  -- inserted active same-logical-post sibling cannot slip past the
  -- deterministic row locks taken below.
  LOCK TABLE public.content_calendar IN SHARE ROW EXCLUSIVE MODE;

  -- Deterministic row locks, id ascending.
  FOR v_id IN SELECT id FROM unnest(v_ids) u(id) ORDER BY id LOOP
    PERFORM 1 FROM public.content_calendar WHERE id = v_id FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'manifest sibling row % is gone', v_id USING ERRCODE = '23514';
    END IF;
  END LOOP;

  -- Re-verify the EXACT CURRENT active member set against the frozen manifest.
  -- Any activation, archival, or new sibling since selection aborts the group.
  SELECT array_agg(c.id ORDER BY c.id) INTO v_current_ids
    FROM public.content_calendar c
   WHERE c.gym_id = p_gym_id
     AND c.logical_post_id = v_logical
     AND c.variant_status = 'active';
  IF v_current_ids IS DISTINCT FROM v_ids THEN
    RAISE EXCEPTION 'active logical-post membership changed since selection'
      USING ERRCODE = '23514';
  END IF;

  -- VALIDATION PHASE (no writes): every row is checked against its OWN
  -- manifest before-state (tenant, group + row identity including
  -- account/format/post_date, status, no publish markers, no media hold,
  -- unchanged caption/media) and its prepared media is checked against the
  -- frozen per-row intended identity (exact selected_asset /
  -- planned_siblings equality, own-tenant asset ownership, clean public URLs
  -- for image/source/thumbnail) BEFORE any row is mutated. A repicked,
  -- foreign, or tokenized prepared identity aborts before the first write.
  FOR v_member IN SELECT m.value FROM jsonb_array_elements(v_members) m
                  ORDER BY (m.value->>'calendar_row_id')::uuid LOOP
    v_id := (v_member->>'calendar_row_id')::uuid;
    SELECT * INTO v_row FROM public.content_calendar WHERE id = v_id;
    IF v_row.gym_id IS DISTINCT FROM p_gym_id
       OR v_row.logical_post_id IS DISTINCT FROM v_logical
       OR v_row.variant_status IS DISTINCT FROM 'active'
       OR v_row.account IS DISTINCT FROM (v_member->>'account')
       OR v_row.format IS DISTINCT FROM (v_member->>'format')
       OR v_row.post_date IS DISTINCT FROM (v_member->>'post_date')::date
       OR v_row.status NOT IN ('pending', 'coach_review')
       OR v_row.published_at IS NOT NULL OR v_row.late_post_id IS NOT NULL
       OR v_row.publish_claim_token IS NOT NULL
       OR v_row.media_not_ready_reason IS NOT NULL
       OR v_row.status IS DISTINCT FROM (v_member->>'status')
       OR coalesce(v_row.caption, '') IS DISTINCT FROM coalesce(v_member->>'caption', '')
       OR v_row.image_url IS DISTINCT FROM (v_member->>'image_url')
       OR v_row.thumbnail_url IS DISTINCT FROM (v_member->>'thumbnail_url')
       OR v_row.source_media_url IS DISTINCT FROM (v_member->>'source_media_url')
       OR v_row.source_media_asset_id IS DISTINCT FROM (v_member->>'source_media_asset_id') THEN
      RAISE EXCEPTION 'stale eligible sibling or media hold on row %', v_id
        USING ERRCODE = '23514';
    END IF;

    SELECT e.value->'media' INTO v_media
      FROM jsonb_array_elements(p_prepared->'rows') e
     WHERE (e.value->>'calendar_row_id')::uuid = v_id;
    IF v_media IS NULL OR jsonb_typeof(v_media) IS DISTINCT FROM 'object'
       OR EXISTS (SELECT 1 FROM jsonb_object_keys(v_media) k
                  WHERE k NOT IN ('image_url', 'source_media_url',
                                  'thumbnail_url', 'source_media_asset_id'))
       OR NOT public.portal_action_receipt_clean_url(v_media->>'image_url')
       OR (v_media ? 'source_media_url' AND v_media->>'source_media_url' IS NOT NULL
           AND NOT public.portal_action_receipt_clean_url(v_media->>'source_media_url'))
       OR (v_media ? 'thumbnail_url' AND v_media->>'thumbnail_url' IS NOT NULL
           AND NOT public.portal_action_receipt_clean_url(v_media->>'thumbnail_url')) THEN
      RAISE EXCEPTION 'prepared media for row % is not allowlisted', v_id
        USING ERRCODE = '22023';
    END IF;

    -- Frozen intended identity for this row: its own planned_siblings entry,
    -- else the frozen selected asset. Exact equality with NULL-tolerant
    -- optional source/thumbnail/asset keys: no repick, no foreign asset and
    -- no tokenized URL can differ from what claim froze.
    SELECT s.value INTO v_intended
      FROM jsonb_each(v_receipt.planned_siblings) s
     WHERE s.key::uuid = v_id;
    IF NOT FOUND THEN
      v_intended := jsonb_build_object(
        'image_url', v_receipt.selected_asset->>'image_url',
        'source_media_url', v_receipt.selected_asset->>'source_media_url',
        'thumbnail_url', v_receipt.selected_asset->>'thumbnail_url',
        'source_media_asset_id', v_receipt.selected_asset->>'asset_id');
    END IF;
    IF v_media->>'image_url' IS DISTINCT FROM (v_intended->>'image_url')
       OR v_media->>'source_media_url' IS DISTINCT FROM (v_intended->>'source_media_url')
       OR v_media->>'thumbnail_url' IS DISTINCT FROM (v_intended->>'thumbnail_url')
       OR v_media->>'source_media_asset_id' IS DISTINCT FROM (v_intended->>'source_media_asset_id') THEN
      RAISE EXCEPTION 'prepared media for row % does not match the frozen selection', v_id
        USING ERRCODE = '23514';
    END IF;
    -- Own-tenant asset ownership on the identity actually being written:
    -- a foreign, coach-excluded or no-longer-eligible asset id is refused
    -- before mutation ('local:<key>' is the local-library provenance the
    -- claim allowlisted; it has no media_asset row by design). The asset is
    -- re-checked HERE, at apply, so an eligibility/exclusion flip between
    -- claim and apply aborts the group -- and the row is locked FOR SHARE
    -- THROUGH the calendar writes below, so a concurrent coach flip either
    -- commits first (this recheck then refuses) or waits out this whole
    -- transaction (serialized); it can never slip between the recheck and
    -- the calendar updates. FOR SHARE, not FOR UPDATE: apply never writes
    -- media_asset, and the weaker mode avoids a needless write lock while
    -- still conflicting with any concurrent UPDATE row lock.
    IF v_media->>'source_media_asset_id' IS NOT NULL
       AND v_media->>'source_media_asset_id' NOT LIKE 'local:%' THEN
      PERFORM 1 FROM public.media_asset
       WHERE id = v_media->>'source_media_asset_id' AND gym_id = p_gym_id
         AND eligible IS TRUE
         AND excluded_by_coach IS NOT TRUE
       FOR SHARE;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'prepared media for row % is not an own-tenant allowlisted media asset', v_id
          USING ERRCODE = '23514';
      END IF;
    END IF;
  END LOOP;

  -- WRITE PHASE: full expected-before CAS UPDATE per row in deterministic id
  -- order (account/format/post_date are part of the CAS: the manifest froze
  -- them and a re-dated or re-targeted row is stale). Zero matched rows means
  -- a concurrent change won and the whole transaction aborts; approved,
  -- publishing/published, and media-held rows are never mutated. After EVERY
  -- update the persisted row is re-read and must show the expected media, an
  -- unchanged eligible status, active group membership and no media
  -- hold/claim -- a soft BEFORE UPDATE trigger that altered NEW still leaves
  -- ROW_COUNT = 1, so only the persisted re-read catches that drift; any
  -- drift raises and rolls the whole transaction back.
  FOR v_member IN SELECT m.value FROM jsonb_array_elements(v_members) m
                  ORDER BY (m.value->>'calendar_row_id')::uuid LOOP
    v_id := (v_member->>'calendar_row_id')::uuid;
    SELECT * INTO v_row FROM public.content_calendar WHERE id = v_id;
    SELECT e.value->'media' INTO v_media
      FROM jsonb_array_elements(p_prepared->'rows') e
     WHERE (e.value->>'calendar_row_id')::uuid = v_id;

    UPDATE public.content_calendar
       SET image_url = v_media->>'image_url',
           source_media_url = v_media->>'source_media_url',
           thumbnail_url = v_media->>'thumbnail_url',
           source_media_asset_id = v_media->>'source_media_asset_id',
           media_not_ready_reason = NULL
     WHERE id = v_id AND gym_id = p_gym_id
       AND logical_post_id = v_logical
       AND variant_status = 'active'
       AND account IS NOT DISTINCT FROM (v_member->>'account')
       AND format IS NOT DISTINCT FROM (v_member->>'format')
       AND post_date IS NOT DISTINCT FROM (v_member->>'post_date')::date
       AND status IN ('pending', 'coach_review')
       AND published_at IS NULL AND late_post_id IS NULL
       AND publish_claim_token IS NULL
       AND media_not_ready_reason IS NULL
       AND caption IS NOT DISTINCT FROM v_row.caption
       AND image_url IS NOT DISTINCT FROM (v_member->>'image_url')
       AND thumbnail_url IS NOT DISTINCT FROM (v_member->>'thumbnail_url')
       AND source_media_url IS NOT DISTINCT FROM (v_member->>'source_media_url')
       AND source_media_asset_id IS NOT DISTINCT FROM (v_member->>'source_media_asset_id');
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'stale sibling row % aborted the swap group', v_id
        USING ERRCODE = '23514';
    END IF;

    -- Persisted re-read: ROW_COUNT alone cannot see a soft trigger rewrite.
    SELECT * INTO v_persisted FROM public.content_calendar WHERE id = v_id;
    IF NOT FOUND
       OR v_persisted.gym_id IS DISTINCT FROM p_gym_id
       OR v_persisted.logical_post_id IS DISTINCT FROM v_logical
       OR v_persisted.variant_status IS DISTINCT FROM 'active'
       OR v_persisted.account IS DISTINCT FROM v_row.account
       OR v_persisted.format IS DISTINCT FROM v_row.format
       OR v_persisted.post_date IS DISTINCT FROM v_row.post_date
       OR v_persisted.status IS DISTINCT FROM v_row.status
       OR v_persisted.status NOT IN ('pending', 'coach_review')
       OR v_persisted.published_at IS NOT NULL
       OR v_persisted.late_post_id IS NOT NULL
       OR v_persisted.publish_claim_token IS NOT NULL
       OR v_persisted.media_not_ready_reason IS NOT NULL
       OR v_persisted.image_url IS DISTINCT FROM (v_media->>'image_url')
       OR v_persisted.source_media_url IS DISTINCT FROM (v_media->>'source_media_url')
       OR v_persisted.thumbnail_url IS DISTINCT FROM (v_media->>'thumbnail_url')
       OR v_persisted.source_media_asset_id IS DISTINCT FROM (v_media->>'source_media_asset_id') THEN
      RAISE EXCEPTION 'post-write drift on row % aborted the swap group', v_id
        USING ERRCODE = '23514';
    END IF;

    v_outcomes := v_outcomes || jsonb_build_array(jsonb_build_object(
      'id', v_id::text, 'swapped', true,
      'image_url', v_media->>'image_url',
      'source_media_asset_id', v_media->>'source_media_asset_id'));
  END LOOP;

  SELECT * INTO v_after FROM public.content_calendar WHERE id = v_receipt.row_id;

  -- Terminal success is persisted in the SAME transaction as the media writes;
  -- only this persisted receipt is ever returned as success.
  UPDATE public.portal_action_receipt
     SET status = 'succeeded',
         after_state = jsonb_build_object(
           'id', v_after.id::text, 'status', v_after.status,
           'caption', v_after.caption, 'post_date', v_after.post_date::text,
           'format', v_after.format, 'image_url', v_after.image_url,
           'thumbnail_url', v_after.thumbnail_url,
           'source_media_url', v_after.source_media_url,
           'source_media_asset_id', v_after.source_media_asset_id),
         sibling_outcomes = v_outcomes,
         response_status = 200,
         error = NULL
   WHERE id = v_receipt.id AND status = 'selected'
  RETURNING * INTO v_receipt;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'receipt terminal write raced; aborting the group'
      USING ERRCODE = '23514';
  END IF;
  RETURN v_receipt;
END;
$function$
;

commit;
