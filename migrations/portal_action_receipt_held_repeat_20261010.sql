-- Upgrade the installed v3 receipt RPCs to accept only an exact frozen
-- cross-date repeat media hold. Run after portal_action_receipt_draft_20261004.sql.
-- No table/grant change. CREATE OR REPLACE preserves service_role-only EXECUTE.
-- The apply transaction validates the before hold, distinct public media,
-- tenant asset and frozen manifest before writing, then reads back each row;
-- any mismatch rolls back the media and hold release together.

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
           'source_media_asset_id', c.source_media_asset_id,
           'media_not_ready_reason', c.media_not_ready_reason)
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
       OR v_row.media_not_ready_reason IS DISTINCT FROM (v_member->>'media_not_ready_reason')
       OR (v_row.media_not_ready_reason IS NOT NULL
           AND v_row.media_not_ready_reason <> 'cross_date_media_repeat_needs_new_visual')
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

    -- A held repeat may clear only when the frozen replacement is visibly
    -- different from the held pixels and source asset. Byte identity is
    -- verified by the service before claim; SQL pins the frozen choice here.
    IF v_row.media_not_ready_reason = 'cross_date_media_repeat_needs_new_visual'
       AND (v_media->>'image_url' IS NOT DISTINCT FROM v_row.image_url
            OR v_media->>'source_media_asset_id' IS NOT DISTINCT FROM v_row.source_media_asset_id) THEN
      RAISE EXCEPTION 'held repeat replacement is not distinct on row %', v_id
        USING ERRCODE = '23514';
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
       AND media_not_ready_reason IS NOT DISTINCT FROM (v_member->>'media_not_ready_reason')
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
           'source_media_asset_id', v_after.source_media_asset_id,
           'media_not_ready_reason', v_after.media_not_ready_reason),
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
