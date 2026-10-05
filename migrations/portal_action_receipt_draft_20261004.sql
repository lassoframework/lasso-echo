-- DRAFT -- portal action receipts v3 (2026-10-04, redesigned after independent
-- review rejected v2). DO NOT APPLY to production from this draft; the database
-- operator reviews and applies it by hand. The code path that reads/writes this
-- table is gated by ECHO_SWAP_ACTION_RECEIPT (default OFF) and refuses closed
-- while the table is absent. This file REPLACES the earlier v1/v2 drafts of
-- the same name; no version has ever been applied.
--
-- v3 redesign (what changed from rejected v2, per review):
--   * NO same-date / same-media sibling inference anywhere. v2 derived the
--     "same post" group from (gym_id, post_date, feed/story IG/FB, same media
--     identity), which could couple unrelated posts sharing a date and asset
--     and missed rows whose rendition differs (e.g. a Story crop). The active
--     group is now EXACTLY (gym_id, logical_post_id, variant_status = 'active')
--     using the immutable content_calendar.logical_post_id from the reviewed
--     foundation migration (logical_post_id_20261004.sql).
--   * The primary row MUST carry a non-null logical_post_id; a NULL (historical
--     or unbackfilled) row is HELD: begin refuses it with a conflict /
--     manual-review error and no receipt is created.
--   * claim_selection persists an IMMUTABLE exact member manifest: the full
--     sorted set of active row ids plus each row's own identity and
--     before-state (format/account/post_date/status/caption/media/thumbnail/
--     source/asset) captured at selection time. Every row later CAS-compares
--     against its OWN manifest entry -- never against the primary's
--     before-image.
--   * apply locks in documented order -- receipt FOR UPDATE, then the
--     content_calendar phantom barrier (SHARE ROW EXCLUSIVE), then sorted
--     ascending row locks -- re-verifies the EXACT current active member set
--     against the frozen manifest, and performs a full expected-before CAS
--     UPDATE per row (media/asset/thumbnail/source/status/caption/publish
--     markers/media hold). Any stale, approved/publishing/published, or
--     media-held row raises and rolls the WHOLE transaction back; the receipt
--     reaches terminal 'succeeded' only in that same transaction, so a receipt
--     can never claim success after a partial update.
--   * Replay of the same (gym_id, action_id) with the same binding returns the
--     stored terminal result verbatim with no repick; a different post, actor,
--     or payload fingerprint is a conflicting reuse and raises.
--
-- Durable, idempotent receipts for authenticated portal actions (first user:
-- the free photo swap). One row per (gym_id, action_id): the client supplies an
-- opaque action_id, Echo binds it to a request fingerprint (action + post +
-- actor + the persisted media identity the request was issued against -- never
-- a mutable "current media" re-read), the selected asset, the frozen active
-- member manifest, before/after persisted media/caption/status snapshots,
-- sibling outcomes, and a normalized terminal state. No signed or tokenized
-- URLs, picker dicts, local paths, portal tokens, secrets, raw headers, or raw
-- provider/client response bodies are ever stored here: there is deliberately
-- NO free-form response column.
--
-- ============================ FROZEN RPC CONTRACTS ============================
-- All three RPCs are SECURITY DEFINER, SET search_path = public, and EXECUTE is
-- granted to service_role ONLY. The portal (anon/authenticated) can never call
-- them or touch the table. All three return the typed receipt row
-- (public.portal_action_receipt), never arbitrary JSON.
--
-- 1. portal_action_receipt_begin(gym, action_id, action, row_id, actor, fp):
--    NO caller-supplied before_state exists: the caller (Portal PR750) sends
--    only action_id plus the immutable request fingerprint derived from
--    (gym, row, actor, action, action_id) BEFORE any row read, and SQL begin
--    is authoritative for the first before_state. Any existing receipt under
--    the SAME tenant (gym_id) is fetched FOR UPDATE first -- BEFORE any
--    calendar lookup -- and the receipt binds action + post (row_id) +
--    actor + request fingerprint; an existing receipt whose binding differs
--    is a CONFLICTING REUSE and raises (SQLSTATE 23514) instead of being
--    silently adopted, so a conflicting replay aimed at another or a
--    since-removed/nonexistent row (or a row that turned historical-NULL)
--    still reports the immutable action conflict, never a manual-review or
--    missing-row error. Only on a genuinely NEW action does begin read the
--    primary content_calendar row: it must be own-tenant with a non-null
--    logical_post_id (a historical NULL row is HELD, no receipt persists,
--    and the action is routed to manual review; sibling grouping by
--    date/media inference is never a fallback), and begin captures the
--    persisted before_state ITSELF in the same transaction that inserts the
--    receipt. The INSERT uses the composite key (gym_id, action_id) ON
--    CONFLICT DO NOTHING (race-safe); an insert-race loser re-checks the
--    winner's binding under FOR UPDATE. An exactly matching binding returns
--    the stored receipt verbatim -- including its original captured
--    before_state, even after the primary row was deleted. No media write
--    happens here.
--
-- 2. portal_action_receipt_claim_selection(gym, action_id, fp, selected_asset,
--    planned_siblings):
--    Exclusive CAS from status 'started' with selected_asset IS NULL to an
--    immutable, allowlisted selection AND the frozen exact member manifest.
--    The manifest is the sorted set of every own-tenant content_calendar row
--    with the primary's logical_post_id and variant_status = 'active', each
--    with its own before-state; archived/candidate/inactive variants are
--    excluded by definition. selected_asset keeps ONLY asset_id + public
--    object identity keys (image_url, source_media_url, thumbnail_url, kind,
--    key); asset_id must be an own-tenant, eligible, not coach-excluded
--    public.media_asset row (locked FOR UPDATE through the CAS), or a
--    'local:<library-key>' provenance id minted by the service-role picker
--    from the tenant's own library. Every stored URL must ALSO bind to the
--    TRUSTED, operator-configured public media origin
--    (public.portal_action_receipt_config 'public_origin', seeded by the
--    database operator at rollout -- never caller-controlled, EMPTY by
--    default and claim FAILS CLOSED while unset) followed by the anchored
--    tenant path echo/<slug(base)>/ or echo/<slug(base)_ig>/ plus the
--    16-hex content-address segment and a file name. An alternate or
--    lookalike host, userinfo, a URL that merely CONTAINS the tenant path,
--    a slug-prefix lookalike, cross-gym path, query/fragment/token
--    separator, or literal/encoded '..' traversal is rejected and never
--    retained, so a valid own asset id cannot launder a foreign URL.
--    planned_siblings is a map of row uuid -> per-row media identity, same
--    key/URL rules. The whole picker dict, local paths and raw responses are
--    refused by the key allowlist. The receipt row is locked FOR UPDATE for
--    the whole CAS, so two concurrent claims serialize: exactly one wins; the
--    loser receives the WINNER's stored selection (returned row) instead of
--    its own candidate. A stored selection and manifest are FROZEN: they can
--    never be overwritten, here or by any direct UPDATE (immutability
--    trigger).
--
-- 3. portal_action_receipt_apply(gym, action_id, fp, prepared):
--    ONE database transaction: locks the receipt FOR UPDATE; replay of an
--    already-terminal receipt returns it unchanged (idempotent replay; a lost
--    RPC response is reconciled by the caller re-reading/returning the receipt,
--    never by re-writing). Otherwise takes the documented phantom-exclusion
--    lock (LOCK TABLE content_calendar IN SHARE ROW EXCLUSIVE MODE, so no
--    INSERT/UPDATE/DELETE can commit for the rest of the transaction and a
--    newly inserted active sibling cannot slip past the row locks), then locks
--    the manifest content_calendar rows FOR UPDATE in deterministic (id
--    ascending) order. It re-derives the CURRENT active member set
--    (gym_id, logical_post_id, variant_status = 'active') and requires it to
--    equal the frozen manifest exactly; checks each row against its OWN
--    manifest before-state (tenant, status in (pending, coach_review), no
--    publish markers, no media hold, unchanged caption/media/thumbnail/
--    source/asset and account/format/post_date row identity), validates the
--    prepared per-row media in a NO-WRITE validation phase against the same
--    allowlist the claim accepted AND exact equality with the frozen per-row
--    intended identity (selected_asset / planned_siblings, NULL-tolerant
--    optional keys) plus own-tenant asset ownership, with the media_asset
--    row locked FOR SHARE through the calendar writes so a concurrent
--    eligibility/exclusion flip is serialized or refused -- a repicked,
--    foreign or tokenized prepared identity is rejected before any mutation
--    -- then
--    writes every row with a full expected-before CAS UPDATE in id order and
--    re-reads the persisted row after EVERY update (a soft BEFORE UPDATE
--    trigger can alter NEW while ROW_COUNT stays 1; only the persisted
--    re-read catches that drift: expected media, unchanged eligible status,
--    active group, no media hold/claim). A stale row, a media hold, an
--    approved/publishing/published row, or any membership or post-write
--    drift raises and
--    ROLLS BACK THE WHOLE GROUP -- there are no partial writes and this
--    function never finalizes 'failed' from a row read: a timed-out write
--    stays 'selected' on the receipt and is reconciled by replay, not guessed.
--    Only after every row write succeeds does the same transaction persist the
--    terminal 'succeeded' receipt with the exact row ids and outcomes, and
--    only that persisted receipt is returned.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.portal_action_receipt (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  gym_id text NOT NULL,
  action_id text NOT NULL,
  action text NOT NULL,
  row_id uuid NOT NULL,
  actor_id text NOT NULL DEFAULT '',
  request_fingerprint text NOT NULL
    CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
  status text NOT NULL DEFAULT 'started'
    CHECK (status IN ('started', 'selected', 'succeeded', 'failed', 'uncertain')),
  selected_asset jsonb,
  planned_siblings jsonb,
  -- Frozen exact active-member manifest captured at selection:
  -- {"logical_post_id": uuid, "members": [per-row identity + before-state]}.
  member_manifest jsonb,
  before_state jsonb,
  after_state jsonb,
  sibling_outcomes jsonb,
  error jsonb,
  response_status integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT portal_action_receipt_tenant_action_key UNIQUE (gym_id, action_id),
  -- Selection payload shape backstops (the RPCs enforce the full allowlist):
  CONSTRAINT portal_action_receipt_selected_asset_shape CHECK (
    selected_asset IS NULL OR (jsonb_typeof(selected_asset) = 'object')),
  CONSTRAINT portal_action_receipt_planned_siblings_shape CHECK (
    planned_siblings IS NULL OR (jsonb_typeof(planned_siblings) = 'object')),
  CONSTRAINT portal_action_receipt_member_manifest_shape CHECK (
    member_manifest IS NULL OR (jsonb_typeof(member_manifest) = 'object'
      AND jsonb_typeof(member_manifest->'members') = 'array'))
);

ALTER TABLE public.portal_action_receipt ENABLE ROW LEVEL SECURITY;
-- No policies for anon/authenticated: RLS denies them outright. Only the
-- service_role RPCs below reach this table.

CREATE INDEX IF NOT EXISTS portal_action_receipt_gym_row_idx
  ON public.portal_action_receipt (gym_id, row_id, id DESC);

-- Keep updated_at honest on every finalize.
CREATE OR REPLACE FUNCTION public.touch_portal_action_receipt()
RETURNS trigger LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS portal_action_receipt_touch ON public.portal_action_receipt;
CREATE TRIGGER portal_action_receipt_touch
  BEFORE UPDATE ON public.portal_action_receipt
  FOR EACH ROW EXECUTE FUNCTION public.touch_portal_action_receipt();

-- Immutability + terminal backstop, enforced even against a privileged
-- direct UPDATE (service_role has no direct UPDATE grant at all; the RPCs
-- already CAS on these rules, and this trigger makes bypassing an RPC --
-- e.g. as table owner -- unable to weaken them):
--   * identity + binding columns (gym_id, action_id, action, row_id, actor_id,
--     request_fingerprint, before_state) never change after insert;
--   * a stored selection (selected_asset / planned_siblings /
--     member_manifest) is FROZEN;
--   * a terminal receipt (succeeded / failed) is FULLY frozen: status,
--     after_state, sibling_outcomes, response_status and error cannot be
--     rewritten even by a same-status UPDATE -- only the updated_at touch
--     may differ; 'uncertain' may only move to a terminal state or
--     'selected'.
CREATE OR REPLACE FUNCTION public.portal_action_receipt_immutable()
RETURNS trigger LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
  IF NEW.gym_id IS DISTINCT FROM OLD.gym_id
     OR NEW.action_id IS DISTINCT FROM OLD.action_id
     OR NEW.action IS DISTINCT FROM OLD.action
     OR NEW.row_id IS DISTINCT FROM OLD.row_id
     OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
     OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
     OR NEW.before_state IS DISTINCT FROM OLD.before_state THEN
    RAISE EXCEPTION 'receipt identity and request binding are immutable'
      USING ERRCODE = '23514';
  END IF;
  IF OLD.selected_asset IS NOT NULL
     AND (NEW.selected_asset IS DISTINCT FROM OLD.selected_asset
          OR NEW.planned_siblings IS DISTINCT FROM OLD.planned_siblings
          OR NEW.member_manifest IS DISTINCT FROM OLD.member_manifest) THEN
    RAISE EXCEPTION 'a stored selection is frozen and cannot be overwritten'
      USING ERRCODE = '23514';
  END IF;
  IF OLD.member_manifest IS NOT NULL
     AND NEW.member_manifest IS DISTINCT FROM OLD.member_manifest THEN
    RAISE EXCEPTION 'the frozen member manifest cannot be rewritten'
      USING ERRCODE = '23514';
  END IF;
  -- Terminal freeze: once succeeded/failed, EVERY stored field is frozen
  -- (identity, binding, selection, manifest and before_state are already
  -- frozen above). Only the updated_at touch may differ. A same-status
  -- direct UPDATE therefore cannot rewrite after_state, sibling_outcomes,
  -- response_status or error on a terminal receipt either.
  IF OLD.status IN ('succeeded', 'failed')
     AND (NEW.status IS DISTINCT FROM OLD.status
          OR NEW.after_state IS DISTINCT FROM OLD.after_state
          OR NEW.sibling_outcomes IS DISTINCT FROM OLD.sibling_outcomes
          OR NEW.response_status IS DISTINCT FROM OLD.response_status
          OR NEW.error IS DISTINCT FROM OLD.error) THEN
    RAISE EXCEPTION 'a terminal receipt cannot be rewritten'
      USING ERRCODE = '23514';
  END IF;
  IF OLD.status = 'uncertain'
     AND NEW.status NOT IN ('uncertain', 'selected', 'succeeded', 'failed') THEN
    RAISE EXCEPTION 'invalid receipt status transition'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS portal_action_receipt_immutable ON public.portal_action_receipt;
CREATE TRIGGER portal_action_receipt_immutable
  BEFORE UPDATE ON public.portal_action_receipt
  FOR EACH ROW EXECUTE FUNCTION public.portal_action_receipt_immutable();

-- URL identity hygiene: a stored public object identity never carries a query
-- string, fragment, or token.
CREATE OR REPLACE FUNCTION public.portal_action_receipt_clean_url(p_url text)
RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = public AS $$
  SELECT p_url IS NOT NULL
     AND p_url ~ '^https://[^?#[:space:]]+$';
$$;

-- Trusted public media origin: operator-seeded at rollout, EMPTY by default
-- (fail closed), locked down from every PostgREST role, and deliberately
-- nonsecret. The value is a bare public origin only -- https scheme,
-- lowercase host, optional port, no userinfo, path, query, fragment or
-- trailing slash. The database operator seeds it once when applying this
-- migration (never committed here, so no environment host is hardcoded):
--   INSERT INTO public.portal_action_receipt_config(key, value)
--   VALUES ('public_origin', 'https://<public media origin>');
CREATE TABLE IF NOT EXISTS public.portal_action_receipt_config (
  key text PRIMARY KEY,
  value text NOT NULL,
  CONSTRAINT portal_action_receipt_config_public_origin_shape CHECK (
    key <> 'public_origin'
    OR value ~ '^https://[a-z0-9.-]+(:[0-9]+)?$')
);
ALTER TABLE public.portal_action_receipt_config ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.portal_action_receipt_config
  FROM public, anon, authenticated, service_role;

-- Trusted hosted-URL binding: an identity URL must be EXACTLY
-- <trusted origin>/echo/<slug>/ or <trusted origin>/echo/<slug>_ig/ plus a
-- 16-hex content-address segment and a file name. strpos anchors the
-- LITERAL prefix at position 1, so an alternate or lookalike host
-- (cdn.example.com.evil.com), userinfo (origin@evil), a URL that merely
-- CONTAINS the tenant path, a slug that only starts with the tenant slug,
-- or a cross-gym path can never pass. Query/fragment are refused by
-- clean_url; literal or percent-encoded '..' and encoded slashes are
-- refused so the anchored path cannot traverse out of the tenant prefix.
CREATE OR REPLACE FUNCTION public.portal_action_receipt_hosted_url(
  p_url text, p_origin text, p_slug text
) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = public AS $$
  SELECT p_url IS NOT NULL
     AND p_origin IS NOT NULL
     AND p_slug IS NOT NULL AND p_slug <> ''
     AND public.portal_action_receipt_clean_url(p_url)
     AND (strpos(p_url, p_origin || '/echo/' || p_slug || '/') = 1
          OR strpos(p_url, p_origin || '/echo/' || p_slug || '_ig/') = 1)
     AND (CASE
            WHEN strpos(p_url, p_origin || '/echo/' || p_slug || '/') = 1
              THEN substr(p_url, length(p_origin || '/echo/' || p_slug || '/') + 1)
            ELSE substr(p_url, length(p_origin || '/echo/' || p_slug || '_ig/') + 1)
          END) ~ '^[0-9a-f]{16}/[^/?#[:space:]]+$'
     AND strpos(p_url, '..') = 0
     AND strpos(lower(p_url), '%2e') = 0
     AND strpos(lower(p_url), '%2f') = 0;
$$;

-- ---------------------------------------------------------------------------
-- CONTRACT 1: begin. See the frozen contract block at the top of this file.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.portal_action_receipt_begin(
  p_gym_id text, p_action_id text, p_action text, p_row_id uuid,
  p_actor_id text, p_request_fingerprint text
) RETURNS public.portal_action_receipt
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_receipt public.portal_action_receipt;
  v_row public.content_calendar;
  v_logical uuid;
  v_before jsonb;
BEGIN
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
$$;

-- ---------------------------------------------------------------------------
-- CONTRACT 2: claim_selection. See the frozen contract block at the top.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.portal_action_receipt_claim_selection(
  p_gym_id text, p_action_id text, p_request_fingerprint text,
  p_selected_asset jsonb, p_planned_siblings jsonb
) RETURNS public.portal_action_receipt
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
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
$$;

-- ---------------------------------------------------------------------------
-- CONTRACT 3: apply. See the frozen contract block at the top of this file.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.portal_action_receipt_apply(
  p_gym_id text, p_action_id text, p_request_fingerprint text,
  p_prepared jsonb
) RETURNS public.portal_action_receipt
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
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
$$;

-- ---------------------------------------------------------------------------
-- Grants: service-role-only, table and RPCs alike. No direct client access,
-- no DELETE for anyone through PostgREST (receipts are an audit trail).
-- ---------------------------------------------------------------------------
REVOKE ALL ON public.portal_action_receipt FROM public, anon, authenticated, service_role;
-- No direct UPDATE even for service_role: receipts move only through the
-- SECURITY DEFINER RPCs above, and the immutability trigger backstops
-- any privileged direct write (terminal fields are fully frozen).
GRANT SELECT ON public.portal_action_receipt TO service_role;

REVOKE ALL ON FUNCTION public.touch_portal_action_receipt()
  FROM public, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.portal_action_receipt_immutable()
  FROM public, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.portal_action_receipt_clean_url(text)
  FROM public, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.portal_action_receipt_hosted_url(text, text, text)
  FROM public, anon, authenticated, service_role;

REVOKE ALL ON FUNCTION public.portal_action_receipt_begin(text, text, text, uuid, text, text)
  FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.portal_action_receipt_begin(text, text, text, uuid, text, text)
  TO service_role;
REVOKE ALL ON FUNCTION public.portal_action_receipt_claim_selection(text, text, text, jsonb, jsonb)
  FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.portal_action_receipt_claim_selection(text, text, text, jsonb, jsonb)
  TO service_role;
REVOKE ALL ON FUNCTION public.portal_action_receipt_apply(text, text, text, jsonb)
  FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.portal_action_receipt_apply(text, text, text, jsonb)
  TO service_role;
