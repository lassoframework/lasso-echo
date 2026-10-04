-- DRAFT / UNAPPLIED / OFF — DO NOT APPLY, DO NOT ACTIVATE.
--
-- STATUS: DRAFT / UNAPPLIED / OFF — atomic media-PATCH scene-binding RPC
-- (KIMI_SCENE_PATCH_SWARM_20261004.md, 2026-10-04; repaired after independent
-- review rejection). No production apply, no flag activation:
-- SCENE_GUARD_OPERATIONAL stays False and AGENT_VISUAL_SCENE_GUARD stays OFF.
-- This file adds ONE function and no tables, triggers, or policy changes.
--
-- MIGRATION ORDER (exact):
--   1. (base schema + claim trigger drafts)
--   2. migrations/DRAFT_visual_global_history_20261002.sql
--   3. migrations/DRAFT_visual_scene_claim_wave_20261003.sql
--   4. migrations/DRAFT_visual_scene_calendar_atomic_write_20261004.sql
--      (canonical candidate binding + atomic insert writer; PREREQUISITE of
--      this file — resolved from THIS repository's migrations/ directory)
--   5. THIS FILE: migrations/DRAFT_visual_scene_calendar_atomic_patch_20261004.sql
--   6. (any activation draft — must remain LAST; none exists yet)
-- NOTE: migrations/DRAFT_visual_scene_phash_20261003.sql is a SUPERSEDED
-- design sketch whose record functions raise 0A000; it is NOT applied in
-- this stack and is not part of the order above.
-- This file must be applied only after the atomic WRITE draft: it depends on
-- the canonical unique binding visual_scene_candidate_canonical_binding_uq
-- (tenant_id, group_key, object_role, exact_url), on the write draft's
-- revocation of the direct service_role registration route, and on the
-- claim-wave helpers public.visual_group_tenant_strict,
-- public.visual_scene_row_candidate and
-- public.visual_scene_row_delivered_object. It applies strictly before any
-- activation draft.
--
-- ROLLBACK (before any activation): nothing is applied anywhere; delete the
-- file. If it were ever applied to a scratch database:
--   drop function if exists
--     public.visual_scene_atomic_media_patch(uuid, text, jsonb, jsonb, jsonb);
--
-- WHAT THIS FILE IMPLEMENTS
--   A narrow SECURITY DEFINER RPC,
--   public.visual_scene_atomic_media_patch(p_row_id, p_gym_id, p_expected,
--   p_patch, p_candidate), for a SINGLE content_calendar media PATCH in which
--   canonical scene-candidate staging and the existing full-row/media
--   compare-and-swap happen in ONE PostgreSQL transaction:
--     0. DEPENDENCY GUARDS. The canonical binding index
--        visual_scene_candidate_canonical_binding_uq must exist, and
--        service_role must NOT hold EXECUTE on the frozen random-UUID
--        registration route (both installed by the atomic WRITE draft). If
--        either is missing the function raises before touching anything:
--        deployment-order or privilege drift must never reopen the
--        random-UUID registration path beside this writer.
--     1. VALIDATION. p_patch keys are checked against a fixed media-column
--        allowlist (image_url, thumbnail_url, source_media_url,
--        source_media_asset_id, drive_file_id, byte_hash, r2_key,
--        visual_group_key, media_not_ready_reason) — the union of the payload
--        keys the guarded portal patch lanes (patch_image_url / patch_media /
--        swap_media and their visual_writer_prepare additions) may carry. Any
--        other key (e.g. status, caption, post_date) is refused with 22023.
--        p_expected must carry EVERY one of the 23 _VISUAL_MEDIA_CAS_COLUMNS
--        keys (agent/portal_calendar_store.py:66-72), including keys whose
--        observed value is JSON null; an ABSENT key is refused with 22023
--        rather than silently compared as NULL. The candidate (when present)
--        must be the same staging-only evidence shape the atomic insert
--        writer accepts (kind/stage/usage_claimed=false/counts_as_use=false)
--        and must name the row's OWN canonical tenant.
--     2. CANONICAL STAGING BEFORE UPDATE. When p_candidate is present, the
--        candidate is staged against the SAME canonical binding as the atomic
--        insert writer: (tenant_id, group_key, object_role, exact_url) under
--        visual_scene_candidate_canonical_binding_uq. The group must equal
--        the post-patch visual_group_key and the object role/exact URL must
--        equal the post-patch delivered object (display=image_url,
--        poster=thumbnail_url when distinct) — a bare tenant match is never
--        sufficient. An existing canonical candidate is REUSED (stable UUID)
--        only when its pHash AND fingerprint match exactly; a contradictory
--        pHash/fingerprint on the same binding raises 23514. On a first
--        registration, concurrent same-object retries converge on the unique
--        index: the loser catches unique_violation in its subtransaction and
--        re-reads the committed winner. This SECURITY DEFINER function calls
--        the frozen public.visual_scene_register_candidate only as its owner
--        (implicit EXECUTE); the direct service_role route stays revoked.
--        Staging is preparation ONLY: this function NEVER writes
--        visual_scene_phash_occupied.
--     2a. ELIGIBLE BEFORE-IMAGE (fail closed BEFORE any candidate staging
--        or update). The frozen scene guard scans ACTIVE rows
--        (visual_group_row_active includes 'approved'), so an approved
--        UNSENT row IS re-decided on a media update and is patchable here:
--        NULL-safe POSITIVE eligibility — status must PROVE one of
--        pending/coach_review/approved and variant_status must PROVE
--        exactly 'active' (a NULL status or any non-active variant, e.g.
--        'candidate' or NULL, is refused: the frozen guard only re-decides
--        variant_status='active' rows), with no
--        published_at/publish_claim_token/publish_reservation_day/
--        late_post_id send markers and no current
--        scene-held state (media_not_ready_reason='scene_review_hold') or
--        OPEN visual_scene_review_hold for the row. Published, claimed,
--        reserved, non-active-variant or currently held rows are refused
--        with 23514 before anything is staged (a held row's review evidence
--        still names the OLD media and must be released through scene review
--        first). Only OPEN holds block: resolved (approved/rejected) hold
--        HISTORY on the row never does — a reactivated row with a closed
--        hold history is patchable again.
--     3. ONE CAS UPDATE. The UPDATE carries the exact current full-row/media
--        CAS the portal's guarded lanes build in Python
--        (SupabaseCalendarStore._visual_media_cas over _VISUAL_MEDIA_CAS_COLUMNS,
--        agent/portal_calendar_store.py): id + gym_id (account/tenant scope)
--        plus NULL-safe equality (IS NOT DISTINCT FROM) on all 23 expected
--        columns. Only the allowlisted patch keys are SET; keys absent from
--        p_patch keep their values (a key present with a JSON null clears the
--        column, matching the Python payload semantics). The UPDATE re-enters
--        the ONE authoritative content_calendar BEFORE trigger
--        (content_calendar_visual_group_guard), which re-resolves identity
--        and runs the scene decision against the just-staged candidate; the
--        RETURNING payload is the persisted POST-TRIGGER row.
--     4. POST-TRIGGER DISPOSITION. The persisted row decides the result:
--        * HELD. If the merged guard held the row (status='pending',
--          variant_status='archived',
--          media_not_ready_reason='scene_review_hold', no claim token or
--          reservation), the function returns a distinct {"outcome":"held"}
--          WITH the actual persisted row. The committed hold is PRESERVED —
--          the function does NOT raise after it, and never reports a held
--          row as patched success.
--        * PATCHED. Otherwise every intended patch value must have persisted
--          exactly, and (with a candidate) the row's rebound candidate
--          (visual_scene_row_candidate), visual_group_key, delivered object
--          role and exact URL must equal the stable candidate just staged.
--          Any drift raises 23514 and rolls the whole transaction back —
--          success is never reported for a row that did not take the patch.
--     5. STALE CAS. Zero matching rows means the observed before-image no
--        longer holds (concurrent swap, approval, claim, hold, or tenant
--        mismatch). The function raises a private marker SQLSTATE (PZ001) so
--        the enclosing plpgsql subtransaction ROLLS BACK the staged candidate
--        (no compensating DELETE) and classifies the miss: {"outcome":"stale"}
--        when the row still exists in this tenant's scope, else
--        {"outcome":"not_found"} (cross-tenant and missing ids are
--        indistinguishable — the portal's 404 discipline). The handler catches
--        ONLY PZ001; validation, staging and trigger errors propagate and
--        roll back everything.
--   Return jsonb:
--     {"outcome":"patched",  "candidate_id":<uuid|null>,
--      "candidate_reused":<bool>, "row":{<post-trigger row>}}
--     {"outcome":"held",     "candidate_id":<uuid|null>,
--      "candidate_reused":<bool>, "row":{<persisted held row>}}
--     {"outcome":"stale",    "row_id":..., "gym_id":...}
--     {"outcome":"not_found","row_id":..., "gym_id":...}
--
-- PRIVILEGES (matches the claim-wave safe-surface pattern): EXECUTE is
-- revoked from public, anon and authenticated and granted ONLY to
-- service_role. The function is SECURITY DEFINER with a pinned search_path.
-- ===========================================================================
begin;

create or replace function public.visual_scene_atomic_media_patch(
  p_row_id uuid,
  p_gym_id text,
  p_expected jsonb,
  p_patch jsonb,
  p_candidate jsonb default null
) returns jsonb language plpgsql security definer set search_path = public, pg_temp as $$
declare
  v_tenant text;
  v_candidate_tenant text;
  v_candidate_id uuid;
  v_candidate_reused boolean := false;
  v_existing_phash text;
  v_existing_fingerprint text;
  v_group text;
  v_role text;
  v_exact_url text;
  v_image_url text;
  v_thumbnail_url text;
  v_phash text;
  v_fingerprint text;
  v_evidence jsonb;
  v_row jsonb;
  v_updated integer;
  v_key text;
  v_bound_candidate_id uuid;
  v_bound_role text;
  v_bound_url text;
  v_pre_status text;
  v_pre_variant text;
  v_pre_reason text;
  v_pre_published_at timestamptz;
  v_pre_claim_token uuid;
  v_pre_reservation_day date;
  v_pre_late_post_id text;
begin
  -- Dependency guards: this writer exists only alongside the canonical
  -- binding installed by the atomic WRITE draft. A missing index or a
  -- reopened direct service-role registration route is deployment drift;
  -- fail closed before any write.
  if to_regclass('public.visual_scene_candidate_canonical_binding_uq') is null then
    raise exception 'atomic media patch requires the canonical scene candidate binding (apply DRAFT_visual_scene_calendar_atomic_write_20261004 first)'
      using errcode = '42P01';
  end if;
  if has_function_privilege(
      'service_role',
      'public.visual_scene_register_candidate(text,text,text,text,text,jsonb,text,text)',
      'EXECUTE') then
    raise exception 'unsafe direct service-role scene registration is enabled'
      using errcode = '42501';
  end if;
  if p_row_id is null or nullif(btrim(p_gym_id), '') is null
      or p_expected is null or jsonb_typeof(p_expected) <> 'object'
      or p_patch is null or jsonb_typeof(p_patch) <> 'object'
      or p_patch = '{}'::jsonb
      or (p_candidate is not null and jsonb_typeof(p_candidate) <> 'object') then
    raise exception 'atomic media patch needs row id, tenant scope, expected row image, a non-empty patch object and an optional candidate object'
      using errcode = '22023';
  end if;
  -- The expected image must be COMPLETE: every one of the 23
  -- _VISUAL_MEDIA_CAS_COLUMNS keys, including keys whose observed value is a
  -- JSON null. An absent key is a caller bug (a truncated before-image would
  -- silently widen the CAS), so it is refused rather than treated as NULL.
  foreach v_key in array array[
    'status', 'format', 'image_url', 'thumbnail_url', 'source_media_url',
    'caption', 'account', 'post_date', 'time_slot', 'slot_index',
    'variant_status', 'source_media_asset_id', 'drive_file_id',
    'visual_group_key', 'byte_hash', 'r2_key', 'media_not_ready_reason',
    'scheduled_at', 'published_at', 'late_post_id', 'publish_claim_token',
    'publish_reservation_day', 'created_at'] loop
    if not (p_expected ? v_key) then
      raise exception 'atomic media patch expected image is missing CAS key %', v_key
        using errcode = '22023';
    end if;
  end loop;
  -- Fixed media-column allowlist: exactly the payload keys the guarded portal
  -- patch lanes may carry. Anything else (status, caption, post_date, ...)
  -- is refused so approval state, slot identity and tenant scope can never be
  -- rewritten through this RPC.
  if exists(select 1 from jsonb_object_keys(p_patch) k
      where k not in ('image_url', 'thumbnail_url', 'source_media_url',
                      'source_media_asset_id', 'drive_file_id', 'byte_hash',
                      'r2_key', 'visual_group_key', 'media_not_ready_reason')) then
    raise exception 'atomic media patch may only set media identity columns'
      using errcode = '22023';
  end if;
  -- The candidate must name THIS row's canonical tenant: re-resolve from the
  -- raw calendar key (raises for an unmapped key) and refuse a mismatch, so a
  -- candidate can never be staged against another tenant's row.
  v_tenant := public.visual_group_tenant_strict(p_gym_id)::text;
  if p_candidate is not null then
    if p_candidate->>'kind' is distinct from 'visual_scene_candidate'
        or p_candidate->>'stage' is distinct from 'candidate'
        or p_candidate->'usage_claimed' is distinct from 'false'::jsonb
        or p_candidate->'counts_as_use' is distinct from 'false'::jsonb then
      raise exception 'atomic media patch candidate is not staging-only evidence'
        using errcode = '22023';
    end if;
    v_candidate_tenant := public.visual_group_tenant_strict(
      p_candidate->>'tenant_id')::text;
    if v_candidate_tenant is distinct from v_tenant then
      raise exception 'scene candidate tenant does not match the calendar row tenant'
        using errcode = '23514';
    end if;
    -- Canonical binding fields: the candidate must bind the row's POST-PATCH
    -- group and exact delivered object, never just the tenant. The group is
    -- the patch's visual_group_key when the patch carries one, else the
    -- observed (expected) one; the delivered object derives from the
    -- post-patch image/thumbnail exactly like
    -- public.visual_scene_row_delivered_object.
    v_group := case when p_patch ? 'visual_group_key'
      then p_patch->>'visual_group_key' else p_expected->>'visual_group_key' end;
    v_image_url := case when p_patch ? 'image_url'
      then p_patch->>'image_url' else p_expected->>'image_url' end;
    v_thumbnail_url := case when p_patch ? 'thumbnail_url'
      then p_patch->>'thumbnail_url' else p_expected->>'thumbnail_url' end;
    v_image_url := nullif(btrim(v_image_url), '');
    v_thumbnail_url := nullif(btrim(v_thumbnail_url), '');
    if v_thumbnail_url is not null and v_thumbnail_url is distinct from v_image_url then
      v_role := 'poster';
      v_exact_url := v_thumbnail_url;
    elsif v_image_url is not null then
      v_role := 'display';
      v_exact_url := v_image_url;
    end if;
    if nullif(btrim(v_group), '') is null
        or p_candidate->>'group_key' is distinct from v_group then
      raise exception 'scene candidate group does not match the patched row visual group'
        using errcode = '23514';
    end if;
    if v_exact_url is null then
      raise exception 'scene candidate has no displayed object on the patched row'
        using errcode = '23514';
    end if;
    if p_candidate->>'object_role' is distinct from v_role
        or p_candidate->>'exact_url' is distinct from v_exact_url then
      raise exception 'scene candidate does not bind the patched row exact delivered object'
        using errcode = '23514';
    end if;
    v_phash := lower(btrim(p_candidate->>'phash'));
    v_fingerprint := lower(btrim(p_candidate->>'fingerprint'));
    v_evidence := p_candidate->'evidence';
    if v_phash is null or v_phash !~ '^[0-9a-f]{16}$'
        or v_fingerprint is null or v_fingerprint !~ '^md5:[0-9a-f]{32}$'
        or v_evidence is null or jsonb_typeof(v_evidence) <> 'object'
        or lower(btrim(v_evidence->>'verified_bytes')) is distinct from v_fingerprint then
      raise exception 'scene candidate lacks exact verified byte evidence'
        using errcode = '22023';
    end if;
    -- Server-owned fields are written last and cannot be overridden by the
    -- JSON caller (same shape as the atomic insert writer).
    v_evidence := v_evidence || jsonb_build_object(
      'verified_bytes', v_fingerprint,
      'tenant_id', v_tenant,
      'group_key', v_group,
      'object_role', v_role,
      'exact_url', v_exact_url,
      'phash', v_phash,
      'calendar_row_id', p_row_id::text,
      'registration_provenance', 'first_atomic_calendar_patch_registration',
      'atomic_calendar_patch', true
    );
  end if;
  -- ELIGIBLE BEFORE-IMAGE (fail closed BEFORE any candidate staging or
  -- update). Independent-review P1 repair (2026-10-04): the frozen scene
  -- guard's active predicate (public.visual_group_row_active,
  -- DRAFT_visual_group_claim_trigger_20261002.sql) scans ACTIVE rows whose
  -- status includes 'approved', so an approved UNSENT row IS re-decided on
  -- a media update — refusing it here was wrong. Patchable before-images
  -- are exactly the unsent active states the guarded portal PATCH lanes may
  -- write: status pending/coach_review/approved (see
  -- portal_calendar_store.py patch_image_url, whose publish-time form
  -- allows approved, and replace_future_infographic_media, which preserves
  -- approved) with NO send markers (published_at, publish_claim_token,
  -- publish_reservation_day, late_post_id) — a published/claimed/reserved
  -- row is occupied history, not patchable media. A PREEXISTING
  -- non-active or held row (variant_status archived/candidate/NULL, or a
  -- NULL/unknown status) is still refused outright: the guard only scans
  -- ACTIVE rows (variant_status='active'), so a media update on such a row
  -- would never be re-decided, and any old visual_scene_review_hold
  -- evidence still names the OLD media. Such rows must go through the scene-review release path
  -- (which re-activates and clears the hold) before any media patch. Only
  -- OPEN holds block: hold rows are resolved exactly once
  -- (state 'open' -> 'approved'/'rejected',
  -- visual_scene_review_hold_open_terminal_ck), so resolved hold HISTORY
  -- (e.g. on a row reactivated after approval) must not refuse the patch.
  -- A NEWLY trigger-created hold below is still the intended 'held' result
  -- — this check runs before the UPDATE, when the row is not yet held by
  -- this call. A missing/cross-tenant row simply fails the CAS below
  -- (stale/not_found), unchanged.
  select c.status, c.variant_status, c.media_not_ready_reason,
         c.published_at, c.publish_claim_token, c.publish_reservation_day,
         c.late_post_id
    into v_pre_status, v_pre_variant, v_pre_reason,
         v_pre_published_at, v_pre_claim_token, v_pre_reservation_day,
         v_pre_late_post_id
    from public.content_calendar c
    where c.id = p_row_id and c.gym_id = p_gym_id;
  if found then
    -- NULL-safe POSITIVE eligibility (independent-review P1 repair
    -- 2026-10-04, second pass): status must PROVE one of
    -- pending/coach_review/approved and variant_status must PROVE exactly
    -- 'active' — the frozen public.visual_group_row_active scans only
    -- variant_status='active' rows, so any other value ('archived',
    -- 'candidate', ...) or a NULL in either column would slip a row through
    -- a plain NOT IN / 'archived'-only check and return 'patched' without
    -- any scene re-decision. NULL status makes `not in (...)` evaluate to
    -- NULL (not TRUE), and `is not distinct from 'archived'` passes NULL
    -- and 'candidate'; both are refused here by the positive form.
    if v_pre_status is null
        or v_pre_status not in ('pending', 'coach_review', 'approved')
        or v_pre_variant is distinct from 'active'
        or v_pre_reason is not distinct from 'scene_review_hold'
        or v_pre_published_at is not null
        or v_pre_claim_token is not null
        or v_pre_reservation_day is not null
        or v_pre_late_post_id is not null
        or exists(select 1 from public.visual_scene_review_hold h
          where h.calendar_row_id = p_row_id and h.state = 'open') then
      raise exception 'atomic media patch refuses this row: only an UNSENT row with status pending/coach_review/approved and variant_status exactly active is patchable — NULL/unknown status, non-active variant (archived, candidate, NULL), published, claimed, reserved, late_post_id or currently scene-held rows (including any OPEN scene review hold) must be released through scene review before patching'
        using errcode = '23514';
    end if;
  end if;
  begin
    -- 1. Stage against the ONE canonical binding (tenant, group, role, exact
    --    URL) BEFORE the UPDATE. Reuse is allowed only for the exact same
    --    pHash+fingerprint; a contradictory identity on the same binding
    --    raises 23514 and aborts the whole transaction (fail closed). A
    --    concurrent first registration of the same object hits the unique
    --    index; the loser catches unique_violation here and re-reads the
    --    committed winner, so race-safe same-object retries converge on one
    --    stable UUID. The frozen register function runs as this function's
    --    owner — the direct service_role route stays revoked.
    if p_candidate is not null then
      select c.candidate_id, btrim(c.phash::text), c.fingerprint
        into v_candidate_id, v_existing_phash, v_existing_fingerprint
        from public.visual_scene_candidate c
        where c.tenant_id = v_tenant and c.group_key = v_group
          and c.object_role = v_role and c.exact_url = v_exact_url;
      v_candidate_reused := v_candidate_id is not null;
      if v_candidate_id is null then
        begin
          v_candidate_id := public.visual_scene_register_candidate(
            v_tenant, v_group, v_phash, v_exact_url, v_fingerprint,
            v_evidence, coalesce(nullif(btrim(p_candidate->>'actor'), ''),
                                 'visual_scene_atomic_media_patch'),
            v_role);
          v_existing_phash := v_phash;
          v_existing_fingerprint := v_fingerprint;
          v_candidate_reused := false;
        exception when unique_violation then
          v_candidate_id := null;
          select c.candidate_id, btrim(c.phash::text), c.fingerprint
            into v_candidate_id, v_existing_phash, v_existing_fingerprint
            from public.visual_scene_candidate c
            where c.tenant_id = v_tenant and c.group_key = v_group
              and c.object_role = v_role and c.exact_url = v_exact_url;
          if v_candidate_id is null then
            raise;
          end if;
          v_candidate_reused := true;
        end;
      end if;
      if v_candidate_id is null then
        raise exception 'scene candidate registration returned null'
          using errcode = '23514';
      end if;
      if v_existing_phash is distinct from v_phash
          or v_existing_fingerprint is distinct from v_fingerprint then
        raise exception 'scene candidate conflicts with the canonical candidate identity for this exact displayed object'
          using errcode = '23514';
      end if;
    end if;
    -- 2. The single full-row/media compare-and-swap UPDATE. The WHERE clause
    --    is the exact CAS the guarded portal lanes build in Python
    --    (NULL-safe equality over the whole observed media/slot/publish
    --    before-image, plus id + gym_id tenant scope); the SET clause carries
    --    only allowlisted patch keys. The UPDATE re-enters the merged
    --    exact-byte/scene guard trigger and returns the PERSISTED
    --    post-trigger row.
    update public.content_calendar set
      image_url = case when p_patch ? 'image_url'
        then p_patch->>'image_url' else image_url end,
      thumbnail_url = case when p_patch ? 'thumbnail_url'
        then p_patch->>'thumbnail_url' else thumbnail_url end,
      source_media_url = case when p_patch ? 'source_media_url'
        then p_patch->>'source_media_url' else source_media_url end,
      source_media_asset_id = case when p_patch ? 'source_media_asset_id'
        then p_patch->>'source_media_asset_id' else source_media_asset_id end,
      drive_file_id = case when p_patch ? 'drive_file_id'
        then p_patch->>'drive_file_id' else drive_file_id end,
      byte_hash = case when p_patch ? 'byte_hash'
        then p_patch->>'byte_hash' else byte_hash end,
      r2_key = case when p_patch ? 'r2_key'
        then p_patch->>'r2_key' else r2_key end,
      visual_group_key = case when p_patch ? 'visual_group_key'
        then p_patch->>'visual_group_key' else visual_group_key end,
      media_not_ready_reason = case when p_patch ? 'media_not_ready_reason'
        then p_patch->>'media_not_ready_reason' else media_not_ready_reason end
    where id = p_row_id
      and gym_id = p_gym_id
      and status is not distinct from (p_expected->>'status')
      and format is not distinct from (p_expected->>'format')
      and image_url is not distinct from (p_expected->>'image_url')
      and thumbnail_url is not distinct from (p_expected->>'thumbnail_url')
      and source_media_url is not distinct from (p_expected->>'source_media_url')
      and caption is not distinct from (p_expected->>'caption')
      and account is not distinct from (p_expected->>'account')
      and post_date is not distinct from (p_expected->>'post_date')::date
      and time_slot is not distinct from (p_expected->>'time_slot')
      and slot_index is not distinct from (p_expected->>'slot_index')::integer
      and variant_status is not distinct from (p_expected->>'variant_status')
      and source_media_asset_id is not distinct from (p_expected->>'source_media_asset_id')
      and drive_file_id is not distinct from (p_expected->>'drive_file_id')
      and visual_group_key is not distinct from (p_expected->>'visual_group_key')
      and byte_hash is not distinct from (p_expected->>'byte_hash')
      and r2_key is not distinct from (p_expected->>'r2_key')
      and media_not_ready_reason is not distinct from (p_expected->>'media_not_ready_reason')
      and scheduled_at is not distinct from (p_expected->>'scheduled_at')::timestamptz
      and published_at is not distinct from (p_expected->>'published_at')::timestamptz
      and late_post_id is not distinct from (p_expected->>'late_post_id')
      and publish_claim_token is not distinct from (p_expected->>'publish_claim_token')::uuid
      and publish_reservation_day is not distinct from (p_expected->>'publish_reservation_day')::date
      and created_at is not distinct from (p_expected->>'created_at')::timestamptz
    returning to_jsonb(content_calendar) into v_row;
    get diagnostics v_updated = row_count;
    if v_updated = 1 then
      -- 3a. HELD disposition: the merged guard persisted an archived review
      --     hold on this row. Return the actual committed row as a distinct
      --     'held' outcome — never 'patched' — and do NOT raise: the hold is
      --     the intended committed result of the scene decision.
      if v_row->>'status' is not distinct from 'pending'
          and v_row->>'variant_status' is not distinct from 'archived'
          and v_row->>'media_not_ready_reason' is not distinct from 'scene_review_hold'
          and v_row->>'publish_claim_token' is null
          and v_row->>'publish_reservation_day' is null then
        return jsonb_build_object('outcome', 'held',
          'candidate_id', v_candidate_id,
          'candidate_reused', v_candidate_reused, 'row', v_row);
      end if;
      -- 3b. PATCHED requires proof: every intended patch value persisted on
      --     the post-trigger row, and (with a candidate) the rebound row
      --     candidate equals the stable candidate staged above — group,
      --     object role and exact URL included. Any drift raises and rolls
      --     the whole transaction back; success is never reported for a row
      --     that did not take the patch.
      for v_key in select k from jsonb_object_keys(p_patch) k loop
        if (v_row ->> v_key) is distinct from (p_patch ->> v_key) then
          raise exception 'atomic media patch value did not persist for %', v_key
            using errcode = '23514';
        end if;
      end loop;
      if v_candidate_id is not null then
        select public.visual_scene_row_candidate(r), d.object_role, d.exact_url
          into v_bound_candidate_id, v_bound_role, v_bound_url
          from jsonb_populate_record(null::public.content_calendar, v_row) r
          left join lateral public.visual_scene_row_delivered_object(r) d on true;
        if v_bound_candidate_id is distinct from v_candidate_id
            or v_row->>'visual_group_key' is distinct from v_group
            or v_bound_role is distinct from v_role
            or v_bound_url is distinct from v_exact_url then
          raise exception 'atomic media patch persisted candidate binding changed'
            using errcode = '23514';
        end if;
      end if;
      return jsonb_build_object('outcome', 'patched',
        'candidate_id', v_candidate_id,
        'candidate_reused', v_candidate_reused, 'row', v_row);
    end if;
    -- 4. Stale CAS: roll back the staged candidate with this subtransaction
    --    (private marker SQLSTATE; no compensating DELETE) and classify below.
    raise exception 'scene atomic media patch CAS marker' using errcode = 'PZ001';
  exception
    when sqlstate 'PZ001' then
      -- The candidate insert above is undone with the subtransaction. Catch
      -- ONLY the private marker: validation, staging and trigger errors
      -- propagate and roll back the whole transaction.
      if exists(select 1 from public.content_calendar
          where id = p_row_id and gym_id = p_gym_id) then
        return jsonb_build_object('outcome', 'stale',
          'row_id', p_row_id, 'gym_id', p_gym_id);
      end if;
      -- Cross-tenant or missing ids are indistinguishable (the portal's 404
      -- discipline): never reveal that another tenant's row exists.
      return jsonb_build_object('outcome', 'not_found',
        'row_id', p_row_id, 'gym_id', p_gym_id);
  end;
end;
$$;

comment on function public.visual_scene_atomic_media_patch(uuid, text, jsonb, jsonb, jsonb) is
  'DRAFT/UNAPPLIED/OFF: one-transaction calendar media PATCH — refuses before any write any row outside the unsent active pending/coach_review/approved states or carrying send markers (published_at/claim/reservation/late_post_id), a non-active variant (archived/candidate/NULL), a NULL/unknown status, a held state or an OPEN scene review hold (resolved hold history never blocks; preexisting held rows must be released through scene review first), then stages the scene candidate against the canonical (tenant, group, role, exact URL) binding from the atomic insert writer (stable same-evidence reuse, contradictory identity fails closed, race-safe via unique-index retry; preparation only, never sets occupancy), then applies the existing full-row/media compare-and-swap in the same transaction and returns the persisted post-trigger row. A persisted scene hold returns a distinct held outcome with the actual row (the committed hold is preserved); patched requires every patch value persisted and the rebound row candidate equal to the stable candidate. A stale CAS rolls back the staging via a subtransaction (no compensating DELETE) and returns a distinguishable stale/not_found outcome. EXECUTE granted to service_role only.';

-- ---------------------------------------------------------------------------
-- Privileges: service_role only, matching the claim-wave safe read/review
-- surface pattern.
-- ---------------------------------------------------------------------------
revoke all on function public.visual_scene_atomic_media_patch(uuid, text, jsonb, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.visual_scene_atomic_media_patch(uuid, text, jsonb, jsonb, jsonb)
  to service_role;

commit;
