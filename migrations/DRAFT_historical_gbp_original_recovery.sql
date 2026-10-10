BEGIN;

-- DRAFT -- historical GBP original lineage recovery (2026-10-10). DO NOT APPLY
-- to production from this draft; the database operator reviews and applies it
-- by hand. The Python path that reads/writes these objects is gated by
-- AGENT_HISTORICAL_GBP_ORIGINAL_RECOVERY (default OFF) plus an exact gym
-- allowlist (AGENT_HISTORICAL_GBP_ORIGINAL_RECOVERY_GYMS, default EMPTY) and
-- refuses closed while these objects are absent or unconfigured.
--
-- PURPOSE (bounded): a small number of HISTORICAL held GBP content_calendar
-- rows have source_media_url / source_media_asset_id / drive_file_id all NULL
-- and their raw original is no longer tenant-local, so the ordinary
-- media_guard original-lineage proof cannot run. A same-tenant historical
-- IG/FB/Story row still references the exact raw hosted original, and the
-- deployed gbp.crop_4x3 recipe (ImageOps.fit RGB 1200x900 LANCZOS, JPEG
-- quality 90) reproduces the held row's delivered object BYTE-FOR-BYTE. This
-- migration adds a fresh, operator-pinned, fully re-verified original-source
-- authority for exactly those rows. It is NOT a signed historical owner
-- manifest, it imports NO new originals, and it performs NO photo swap: the
-- only calendar write ever made is target.source_media_url NULL -> the pinned
-- trusted raw URL, with the hold, approval, status, caption, date and media
-- bytes preserved byte-identical.
--
-- DEPENDENCY: public.portal_action_receipt_config ('public_origin') and
-- public.portal_action_receipt_hosted_url(text,text,text) from
-- migrations/portal_action_receipt_draft_20261004.sql. The trusted origin is
-- operator-seeded there, EMPTY by default; every RPC here FAILS CLOSED while
-- it is unset or differs from a binding's pinned source_origin.
--
-- This migration seeds NO row bindings. Every binding is inserted by the
-- database operator by hand after independent review of the private per-row
-- proof (kept outside the repo); callers can never create or alter one
-- (no direct write grant for any PostgREST role, service_role included).
--
-- ============================ FROZEN RPC CONTRACTS ============================
-- Both RPCs are SECURITY DEFINER, SET search_path = public, EXECUTE granted
-- to service_role ONLY. No direct table write grant exists for any
-- PostgREST role; service_role holds SELECT only.
--
-- 1. historical_gbp_original_recovery_read(gym, row_id):
--    Read-only. Returns jsonb {"binding": ..., "receipt": ...} for the exact
--    (gym_id, row_id), either field NULL when absent; NULL row when no
--    binding exists. This is the reconcile path after an UNKNOWN transport
--    outcome: the caller re-reads the exact persisted receipt and compares
--    its immutable identity fields, never replays blind.
--
-- 2. historical_gbp_original_recovery_apply(gym, row_id, request_id,
--    request_fingerprint, proof):
--    ONE transaction:
--      * loads the operator binding FOR UPDATE (missing -> 23514);
--      * requires the trusted origin configured and EQUAL to the binding's
--        pinned source_origin, and the pinned source_url to satisfy the
--        anchored tenant content-addressed hosted-URL shape;
--      * requires proof to be an object whose allowlisted fields EQUAL the
--        binding (a caller can never introduce a URL, hash or recipe the
--        operator did not pin);
--      * an existing receipt for (gym_id, row_id) with the SAME request_id,
--        fingerprint, proof and binding returns it verbatim (idempotent
--        replay / lost response); ANY difference is a conflicting reuse and
--        raises 23514. ONE recovery per target row, ever;
--      * takes the content_calendar exact target/history row locks,
--        locks the exact rows in stable UUID order FOR UPDATE, and requires the
--        target's full live snapshot to EQUAL the binding's expected_before
--        (which also pins GBP account/format, pending|coach_review status,
--        active variant, the exact cross_date_media_repeat_needs_new_visual
--        hold, no publish markers, and NULL source/asset/drive lineage) and
--        the historical row's snapshot to EQUAL the pinned historical
--        snapshot, same tenant, actually referencing the pinned raw URL;
--      * updates ONLY target.source_media_url, re-reads the persisted row,
--        and requires the exact expected after snapshot (any trigger soft
--        rewrite or drift ROLLS BACK update and receipt together);
--      * inserts the immutable terminal receipt (exact before+after
--        snapshots + binding + proof) in the same transaction and returns
--        the persisted receipt row only.
-- =============================================================================

-- Independent database brake: bindings may be reviewed/installed while OFF.
CREATE TABLE public.historical_gbp_original_recovery_gate (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 enabled boolean NOT NULL DEFAULT false
);
INSERT INTO public.historical_gbp_original_recovery_gate VALUES (true, false);
ALTER TABLE public.historical_gbp_original_recovery_gate ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.historical_gbp_original_recovery_gate FROM public,anon,authenticated,service_role;
GRANT SELECT ON public.historical_gbp_original_recovery_gate TO service_role;

-- Operator-pinned per-row binding. One row per recoverable target; seeded by
-- hand after independent review. expected_before / historical_snapshot are
-- FULL row snapshots (every column, including audit timestamps).
CREATE TABLE IF NOT EXISTS public.historical_gbp_original_recovery_binding (
  gym_id text NOT NULL,
  row_id uuid PRIMARY KEY,
  historical_row_id uuid NOT NULL,
  tenant_slug text NOT NULL,
  source_origin text NOT NULL,
  source_url text NOT NULL,
  source_sha256 text NOT NULL
    CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  delivered_sha256 text NOT NULL
    CHECK (delivered_sha256 ~ '^[0-9a-f]{64}$'),
  recipe text NOT NULL
    CHECK (recipe = 'gbp-crop-4x3-jpeg90-v1'),
  expected_before jsonb NOT NULL,
  historical_snapshot jsonb NOT NULL,
  seeded_by text NOT NULL DEFAULT '',
  seeded_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT historical_gbp_recovery_binding_tenant_key UNIQUE (gym_id, row_id),
  CONSTRAINT historical_gbp_recovery_binding_historical_key
    UNIQUE (gym_id, historical_row_id),
  CONSTRAINT historical_gbp_recovery_binding_snapshots_shape CHECK (
    jsonb_typeof(expected_before) = 'object'
    AND jsonb_typeof(historical_snapshot) = 'object')
);

ALTER TABLE public.historical_gbp_original_recovery_binding ENABLE ROW LEVEL SECURITY;

-- Immutable terminal receipts. Terminal-only at insert (status 'succeeded');
-- the trigger below makes EVERY column frozen against any later UPDATE, so
-- even a privileged direct write cannot rewrite a stored recovery.
CREATE TABLE IF NOT EXISTS public.historical_gbp_original_recovery_receipt (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  gym_id text NOT NULL,
  row_id uuid NOT NULL,
  request_id text NOT NULL,
  request_fingerprint text NOT NULL
    CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
  status text NOT NULL DEFAULT 'succeeded'
    CHECK (status IN ('succeeded')),
  before_state jsonb NOT NULL,
  after_state jsonb NOT NULL,
  binding jsonb NOT NULL,
  proof jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT historical_gbp_recovery_receipt_target_key UNIQUE (gym_id, row_id),
  CONSTRAINT historical_gbp_recovery_receipt_request_key
    UNIQUE (gym_id, request_id),
  CONSTRAINT historical_gbp_recovery_receipt_shapes CHECK (
    jsonb_typeof(before_state) = 'object'
    AND jsonb_typeof(after_state) = 'object'
    AND jsonb_typeof(binding) = 'object'
    AND jsonb_typeof(proof) = 'object')
);

ALTER TABLE public.historical_gbp_original_recovery_receipt ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS historical_gbp_recovery_receipt_gym_idx
  ON public.historical_gbp_original_recovery_receipt (gym_id, row_id);

-- Full immutability: receipts are written once by the apply RPC and never
-- change afterwards. updated_at is deliberately absent.
CREATE OR REPLACE FUNCTION public.historical_gbp_original_recovery_immutable()
RETURNS trigger LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
  RAISE EXCEPTION 'a historical GBP original recovery receipt is immutable'
    USING ERRCODE = '23514';
END;
$$;

DROP TRIGGER IF EXISTS historical_gbp_original_recovery_immutable
  ON public.historical_gbp_original_recovery_receipt;
CREATE TRIGGER historical_gbp_original_recovery_immutable
  BEFORE UPDATE OR DELETE ON public.historical_gbp_original_recovery_receipt
  FOR EACH ROW EXECUTE FUNCTION public.historical_gbp_original_recovery_immutable();

-- Bindings are operator-frozen against UPDATE too (no direct DML grant
-- exists for any PostgREST role; the trigger backstops a privileged
-- rewrite). DELETE stays available to the database operator alone (the
-- table owner) so a never-applied, wrongly seeded binding can be withdrawn
-- before any receipt exists; the apply RPC locks the binding FOR UPDATE and
-- the receipt freezes its copy, so a withdrawal can never rewrite history.
DROP TRIGGER IF EXISTS historical_gbp_original_recovery_binding_immutable
  ON public.historical_gbp_original_recovery_binding;
CREATE TRIGGER historical_gbp_original_recovery_binding_immutable
  BEFORE UPDATE ON public.historical_gbp_original_recovery_binding
  FOR EACH ROW EXECUTE FUNCTION public.historical_gbp_original_recovery_immutable();

-- Preserve every present column, including future schema additions.
CREATE OR REPLACE FUNCTION public.historical_gbp_original_recovery_snapshot(
 c public.content_calendar
) RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
 SELECT to_jsonb(c);
$$;

-- ---------------------------------------------------------------------------
-- CONTRACT 1: read. See the frozen contract block at the top of this file.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.historical_gbp_original_recovery_read(
  p_gym_id text, p_row_id uuid
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_binding public.historical_gbp_original_recovery_binding;
  v_receipt public.historical_gbp_original_recovery_receipt;
BEGIN
  IF nullif(btrim(coalesce(p_gym_id, '')), '') IS NULL OR p_row_id IS NULL THEN
    RAISE EXCEPTION 'invalid recovery read arguments' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v_binding
    FROM public.historical_gbp_original_recovery_binding
   WHERE gym_id = p_gym_id AND row_id = p_row_id;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  SELECT * INTO v_receipt
    FROM public.historical_gbp_original_recovery_receipt
   WHERE gym_id = p_gym_id AND row_id = p_row_id;
  RETURN jsonb_build_object(
    'binding', to_jsonb(v_binding),
    'receipt', CASE WHEN v_receipt.id IS NULL THEN NULL
                    ELSE to_jsonb(v_receipt) END);
END;
$$;

-- ---------------------------------------------------------------------------
-- CONTRACT 2: apply. See the frozen contract block at the top of this file.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.historical_gbp_original_recovery_apply(
  p_gym_id text, p_row_id uuid, p_request_id text,
  p_request_fingerprint text, p_proof jsonb
) RETURNS public.historical_gbp_original_recovery_receipt
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_binding public.historical_gbp_original_recovery_binding;
  v_receipt public.historical_gbp_original_recovery_receipt;
  v_target public.content_calendar;
  v_hist public.content_calendar;
  v_origin text;
  v_before jsonb;
  v_after jsonb;
  v_expected_after jsonb;
  v_hist_snapshot jsonb;
BEGIN
  IF nullif(btrim(coalesce(p_gym_id, '')), '') IS NULL
     OR p_row_id IS NULL
     OR nullif(btrim(coalesce(p_request_id, '')), '') IS NULL
     OR p_request_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
     OR p_request_fingerprint IS NULL
     OR p_request_fingerprint !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid recovery apply arguments' USING ERRCODE = '22023';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.historical_gbp_original_recovery_gate WHERE singleton AND enabled) THEN
    RAISE EXCEPTION 'historical GBP recovery is OFF' USING ERRCODE='55000';
  END IF;

  -- Operator binding first; there is no recovery without one.
  SELECT * INTO v_binding
    FROM public.historical_gbp_original_recovery_binding
   WHERE gym_id = p_gym_id AND row_id = p_row_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no configured historical GBP original recovery binding'
      USING ERRCODE = '23514';
  END IF;

  -- Trusted origin: operator-seeded, EMPTY by default; fail closed while
  -- unset or when a binding pins a different origin.
  SELECT value INTO v_origin FROM public.portal_action_receipt_config
   WHERE key = 'public_origin';
  IF v_origin IS NULL OR v_origin <> v_binding.source_origin THEN
    RAISE EXCEPTION 'trusted public media origin is not configured for recovery'
      USING ERRCODE = '23514';
  END IF;
  IF v_binding.tenant_slug IS DISTINCT FROM btrim(regexp_replace(regexp_replace(lower(p_gym_id),'[^a-z0-9_-]+','-','g'),'-{2,}','-','g'),'-_') THEN
    RAISE EXCEPTION 'configured tenant slug mismatch' USING ERRCODE='23514';
  END IF;
  IF NOT public.portal_action_receipt_hosted_url(
       v_binding.source_url, v_origin, v_binding.tenant_slug) THEN
    RAISE EXCEPTION 'bound source URL is outside the trusted tenant origin'
      USING ERRCODE = '23514';
  END IF;

  -- The proof only ever RESTATES the configured binding; a caller can never
  -- introduce a URL, hash or recipe the operator did not pin.
  IF p_proof IS NULL OR jsonb_typeof(p_proof) <> 'object'
     OR p_proof <> jsonb_build_object(
          'source_url', v_binding.source_url,
          'source_sha256', v_binding.source_sha256,
          'delivered_sha256', v_binding.delivered_sha256,
          'recipe', v_binding.recipe,
          'tenant_slug', v_binding.tenant_slug) THEN
    RAISE EXCEPTION 'recovery proof does not match the configured binding'
      USING ERRCODE = '22023';
  END IF;

  -- One recovery per target. Exact replay returns the stored receipt
  -- verbatim (this is also the lost-response reconcile path); ANY different
  -- request id, fingerprint, proof or binding is a conflicting reuse.
  SELECT * INTO v_receipt
    FROM public.historical_gbp_original_recovery_receipt
   WHERE gym_id = p_gym_id AND row_id = p_row_id
   FOR UPDATE;
  IF FOUND THEN
    IF v_receipt.request_id IS DISTINCT FROM p_request_id
       OR v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint
       OR v_receipt.proof IS DISTINCT FROM p_proof
       OR v_receipt.binding IS DISTINCT FROM to_jsonb(v_binding) THEN
      RAISE EXCEPTION 'conflicting reuse of a historical GBP recovery target'
        USING ERRCODE = '23514';
    END IF;
    RETURN v_receipt;
  END IF;

  -- Lock exactly the target/history rows in stable UUID order. No fleet lock.
  PERFORM 1 FROM public.content_calendar
   WHERE id = ANY(ARRAY[p_row_id,v_binding.historical_row_id])
   ORDER BY id FOR UPDATE;

  -- Target row: exact live snapshot must equal the pinned expected_before.
  SELECT c.* INTO v_target
    FROM public.content_calendar c
   WHERE c.id = p_row_id AND c.gym_id = p_gym_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'recovery target row missing or cross-tenant'
      USING ERRCODE = '23514';
  END IF;
  v_before := public.historical_gbp_original_recovery_snapshot(v_target);
  IF v_before <> v_binding.expected_before THEN
    RAISE EXCEPTION 'recovery target snapshot drifted from the configured binding'
      USING ERRCODE = '23514';
  END IF;
  -- Explicit eligibility backstops (the snapshot equality above already pins
  -- these exact values; they are restated so a malformed binding can never
  -- recover an ineligible row).
  IF v_target.account IS DISTINCT FROM 'googlebusiness'
     OR coalesce(v_target.format,'') NOT IN ('update', 'photo')
     OR coalesce(v_target.status,'') NOT IN ('pending', 'coach_review')
     OR v_target.variant_status IS DISTINCT FROM 'active'
     OR v_target.media_not_ready_reason
        IS DISTINCT FROM 'cross_date_media_repeat_needs_new_visual'
     OR v_target.published_at IS NOT NULL
     OR v_target.publish_claim_token IS NOT NULL
     OR v_before->>'late_post_id' IS NOT NULL
     OR v_before->>'approval_kind' IS NOT NULL
     OR v_before->>'approved_by' IS NOT NULL
     OR v_before->>'approved_at' IS NOT NULL
     OR v_before->>'approval_digest' IS NOT NULL
     OR v_target.source_media_url IS NOT NULL
     OR v_target.source_media_asset_id IS NOT NULL
     OR v_target.drive_file_id IS NOT NULL THEN
    RAISE EXCEPTION 'recovery target is not an eligible held GBP row'
      USING ERRCODE = '23514';
  END IF;

  -- Historical row: same tenant, exact pinned snapshot, actually referencing
  -- the pinned raw original URL.
  SELECT c.* INTO v_hist
    FROM public.content_calendar c
   WHERE c.id = v_binding.historical_row_id AND c.gym_id = p_gym_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'historical source row missing or cross-tenant'
      USING ERRCODE = '23514';
  END IF;
  v_hist_snapshot := public.historical_gbp_original_recovery_snapshot(v_hist);
  IF v_hist_snapshot <> v_binding.historical_snapshot THEN
    RAISE EXCEPTION 'historical source row drifted from the configured binding'
      USING ERRCODE = '23514';
  END IF;
  IF v_hist.source_media_url IS DISTINCT FROM v_binding.source_url
     AND v_hist.image_url IS DISTINCT FROM v_binding.source_url THEN
    RAISE EXCEPTION 'historical source row does not reference the pinned original'
      USING ERRCODE = '23514';
  END IF;

  -- The ONLY calendar write this feature ever performs: NULL -> pinned raw
  -- URL on the verified target. Hold, approval, status, caption, date and
  -- media bytes are not touched.
  UPDATE public.content_calendar
     SET source_media_url = v_binding.source_url
   WHERE id = v_target.id AND gym_id = p_gym_id
     AND source_media_url IS NULL;

  -- Post-write re-read: a BEFORE UPDATE trigger can soft-rewrite NEW while
  -- the UPDATE reports one row; only the persisted row is evidence. Any
  -- drift raises and rolls back the update AND the receipt together.
  SELECT c.* INTO v_target
    FROM public.content_calendar c
   WHERE c.id = p_row_id AND c.gym_id = p_gym_id;
  v_after := public.historical_gbp_original_recovery_snapshot(v_target);
  v_expected_after := v_binding.expected_before
    || jsonb_build_object('source_media_url', v_binding.source_url);
  IF (v_after - 'updated_at') <> (v_expected_after - 'updated_at') THEN
    RAISE EXCEPTION 'recovery write drifted from the expected after snapshot'
      USING ERRCODE = '23514';
  END IF;

  INSERT INTO public.historical_gbp_original_recovery_receipt
    (gym_id, row_id, request_id, request_fingerprint, status,
     before_state, after_state, binding, proof)
  VALUES
    (p_gym_id, p_row_id, p_request_id, p_request_fingerprint, 'succeeded',
     v_before, v_after, to_jsonb(v_binding), p_proof)
  ON CONFLICT (gym_id, row_id) DO NOTHING
  RETURNING * INTO v_receipt;
  IF NOT FOUND THEN
    -- Lost the insert race: adopt the winner under lock with the same
    -- immutable identity check.
    SELECT * INTO v_receipt
      FROM public.historical_gbp_original_recovery_receipt
     WHERE gym_id = p_gym_id AND row_id = p_row_id
     FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'recovery receipt conflicted but none is visible'
        USING ERRCODE = 'P0002';
    END IF;
    IF v_receipt.request_id IS DISTINCT FROM p_request_id
       OR v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint
       OR v_receipt.proof IS DISTINCT FROM p_proof
       OR v_receipt.binding IS DISTINCT FROM to_jsonb(v_binding) THEN
      RAISE EXCEPTION 'conflicting reuse of a historical GBP recovery target'
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN v_receipt;
END;
$$;

-- ---------------------------------------------------------------------------
-- Grants: no direct write for ANY PostgREST role (service_role included);
-- service_role holds SELECT (readback/reconcile) plus EXECUTE on the two
-- SECURITY DEFINER RPCs. Bindings are seeded by the database operator as the
-- table owner, never through PostgREST.
-- ---------------------------------------------------------------------------
REVOKE ALL ON public.historical_gbp_original_recovery_binding
  FROM public, anon, authenticated, service_role;
GRANT SELECT ON public.historical_gbp_original_recovery_binding TO service_role;
REVOKE ALL ON public.historical_gbp_original_recovery_receipt
  FROM public, anon, authenticated, service_role;
GRANT SELECT ON public.historical_gbp_original_recovery_receipt TO service_role;

REVOKE ALL ON FUNCTION public.historical_gbp_original_recovery_immutable()
  FROM public, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.historical_gbp_original_recovery_snapshot(public.content_calendar)
  FROM public, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.historical_gbp_original_recovery_read(text, uuid)
  FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.historical_gbp_original_recovery_read(text, uuid)
  TO service_role;
REVOKE ALL ON FUNCTION public.historical_gbp_original_recovery_apply(text, uuid, text, text, jsonb)
  FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.historical_gbp_original_recovery_apply(text, uuid, text, text, jsonb)
  TO service_role;

COMMIT;
