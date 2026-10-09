-- DRAFT -- media_asset composite tenant binding (source_id + gym_id), 2026-10-05.
-- DO NOT APPLY to production from this draft; the database operator reviews the
-- evidence and applies it through the controlled migration process. This file
-- has NEVER been applied in any version.
--
-- WHY: production currently has 95 media_asset rows whose gym_id differs from
-- their linked media_source.gym_id. Those rows are pre-existing fact: they must
-- be PRESERVED (not rewritten, not deleted) and the sync job now refuses to
-- multiply them (agent/jobs/sync_gym_media.py, tenant-source binding guard,
-- 2026-10-05: a source whose tenant identity cannot be ESTABLISHED, or whose
-- stored gym_id does not resolve to the registered tenant, is refused before
-- any walk/insert, and source ownership is never rewritten by the sync). This
-- draft closes the hole for FUTURE writes only.
--
-- MECHANISM: a composite FOREIGN KEY media_asset(source_id, gym_id) REFERENCES
-- media_source(id, gym_id), added NOT VALID. NOT VALID means PostgreSQL does NOT
-- scan existing rows when the constraint is created, so the 95 historical
-- mismatches keep loading untouched. Enforcement is still immediate for new
-- writes: EVERY inserted row is checked, and ANY update that changes source_id
-- or gym_id is checked. Updates that touch only UNRELATED columns of a
-- pre-existing mismatched row are NOT checked (that is the precise meaning of
-- NOT VALID), so legacy rows keep their day-to-day mutability. DO NOT run
-- VALIDATE CONSTRAINT until the 95 mismatches have been reconciled through
-- evidence-reviewed per-row rebinds (never a bulk UPDATE, never a silent
-- rewrite; the PR #262 immutable-gym guard forbids a plain UPDATE of
-- media_source.gym_id) and a preflight recount returns 0 — validating early
-- would fail the transaction, not fix the rows.
--
-- ================================ PREFLIGHT ==================================
-- Run these FIRST. Abort the apply if any check fails; report numbers.
--
-- 1. Count the known mismatches (expected: 95 at draft time; any DIFFERENT
--    number means someone else touched the data — stop and re-verify):
--      SELECT count(*) AS mismatched_assets
--      FROM public.media_asset a JOIN public.media_source s ON s.id = a.source_id
--      WHERE a.gym_id IS DISTINCT FROM s.gym_id;
--
-- 2. Confirm the composite parent key is unique (required for a FK target).
--    PR #262's historical clearance already created this exact index; if the
--    query below returns 1 the shared index is present and the apply step's
--    shape-verifying DO block treats it as the shared object (after checking
--    its uniqueness, columns and predicate-free shape; a wrong shape ABORTS):
--      SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--      WHERE n.nspname = 'public' AND c.relname = 'media_source_id_gym_key'
--        AND c.relkind = 'i';
--
-- 3. Confirm no in-flight migration holds a conflicting lock:
--      SELECT count(*) FROM pg_locks
--      WHERE relation IN ('public.media_asset'::regclass, 'public.media_source'::regclass)
--        AND mode IN ('ACCESS EXCLUSIVE', 'SHARE ROW EXCLUSIVE');
--    Must be 0 before applying.
--
-- ================================ APPLY ======================================
-- One transaction. NOT VALID keeps it cheap and mismatch-preserving.

BEGIN;

-- FK parent key: (id, gym_id) must be unique on media_source. PR #262's
-- historical clearance ALREADY created this exact object (a UNIQUE INDEX, not
-- a table constraint), so this draft SHARES it rather than creating a
-- duplicate. FAIL CLOSED ON SHAPE: a same-named index is only accepted when it
-- is EXACTLY the expected object — unique, on precisely (id, gym_id) in that
-- order, no partial-index predicate, no expression columns, valid and ready.
-- Any other shape means the name is taken by something this draft must not
-- silently rely on: abort the transaction instead of proceeding.
DO $$
DECLARE
  idx_oid oid;
BEGIN
  SELECT c.oid INTO idx_oid
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname = 'media_source_id_gym_key'
    AND c.relkind = 'i';
  IF idx_oid IS NULL THEN
    EXECUTE 'CREATE UNIQUE INDEX media_source_id_gym_key
               ON public.media_source (id, gym_id)';
  ELSIF NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indexrelid = idx_oid
      AND i.indrelid = 'public.media_source'::regclass
      AND i.indisunique
      AND i.indisvalid
      AND i.indisready
      AND i.indpred IS NULL              -- no partial-index predicate
      AND i.indexprs IS NULL             -- no expression columns
      AND i.indnatts = 2                 -- exactly two key columns
      AND (SELECT a.attname FROM pg_attribute a
           WHERE a.attrelid = i.indrelid AND a.attnum = i.indkey[0]) = 'id'
      AND (SELECT a.attname FROM pg_attribute a
           WHERE a.attrelid = i.indrelid AND a.attnum = i.indkey[1]) = 'gym_id'
  ) THEN
    RAISE EXCEPTION 'media_source_id_gym_key exists with the wrong shape '
        '(must be a UNIQUE, valid, predicate-free index on exactly '
        'media_source(id, gym_id)); refusing to proceed — fail closed';
  END IF;
END $$;

-- The tenant binding. FAIL CLOSED ON SHAPE: a same-named constraint is only
-- accepted when it is EXACTLY the expected object — a FOREIGN KEY on
-- media_asset(source_id, gym_id) in that column order, referencing
-- media_source(id, gym_id) in that column order, ON DELETE CASCADE, and still
-- NOT VALID (an already-VALIDATED constraint means someone validated over
-- unreconciled rows — stop and review, do not proceed silently). Any other
-- shape aborts the transaction. NOT VALID means existing rows are NOT scanned,
-- so the 95 preserved mismatches keep loading; new rows and any update
-- touching source_id or gym_id are enforced from here on.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint
             WHERE conname = 'media_asset_source_gym_fkey'
               AND conrelid = 'public.media_asset'::regclass) THEN
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint con
      WHERE con.conname = 'media_asset_source_gym_fkey'
        AND con.conrelid = 'public.media_asset'::regclass
        AND con.contype = 'f'
        AND con.confrelid = 'public.media_source'::regclass
        AND NOT con.convalidated          -- still NOT VALID, as drafted
        AND con.confdeltype = 'c'         -- ON DELETE CASCADE
        AND con.confupdtype = ' '         -- default ON UPDATE NO ACTION
        AND con.confmatchtype = 's'       -- MATCH SIMPLE (the default)
        AND NOT con.condeferrable         -- non-deferrable ...
        AND NOT con.condeferred           -- ... and not currently deferred
        AND EXISTS (                      -- parent index identity: the shared
          SELECT 1 FROM pg_class ic       -- unique index this draft verified /
          JOIN pg_namespace inx ON inx.oid = ic.relnamespace  -- created above
          WHERE inx.nspname = 'public'
            AND ic.relname = 'media_source_id_gym_key'
            AND ic.relkind = 'i')
        AND con.conkey = (SELECT array_agg(a.attnum ORDER BY u.ord)
                          FROM unnest(ARRAY['source_id','gym_id']::name[])
                               WITH ORDINALITY AS u(attname, ord)
                          JOIN pg_attribute a
                            ON a.attrelid = con.conrelid
                           AND a.attname = u.attname)
        AND con.confkey = (SELECT array_agg(a.attnum ORDER BY u.ord)
                           FROM unnest(ARRAY['id','gym_id']::name[])
                                WITH ORDINALITY AS u(attname, ord)
                           JOIN pg_attribute a
                             ON a.attrelid = con.confrelid
                            AND a.attname = u.attname)
    ) THEN
      RAISE EXCEPTION 'media_asset_source_gym_fkey exists with the wrong shape '
          '(must be FK public.media_asset(source_id, gym_id) REFERENCES '
          'public.media_source(id, gym_id) ON DELETE CASCADE, ON UPDATE NO '
          'ACTION, MATCH SIMPLE, non-deferrable, NOT VALID, backed by the '
          'public.media_source_id_gym_key parent index); refusing to proceed '
          '— fail closed';
    END IF;
  ELSE
    EXECUTE 'ALTER TABLE public.media_asset
               ADD CONSTRAINT media_asset_source_gym_fkey
               FOREIGN KEY (source_id, gym_id)
               REFERENCES public.media_source (id, gym_id)
               ON DELETE CASCADE
               NOT VALID';
  END IF;
END $$;

COMMIT;

-- Post-apply verification (read-only):
--   \d public.media_asset          -- confirm the FK is listed as NOT VALID
--   -- The 95 must still be there and still readable:
--   SELECT count(*) FROM public.media_asset a JOIN public.media_source s ON s.id = a.source_id
--    WHERE a.gym_id IS DISTINCT FROM s.gym_id;   -- expected: unchanged (95)
--   -- Enforcement wording check (all three must hold):
--   --   (a) a NEW bad row fails:
--   --   INSERT INTO public.media_asset (id, source_id, gym_id, kind, title, indexed_at)
--   --   VALUES ('__guard_probe', '<any real source id>', '__wrong_tenant__',
--   --           'photo', 'guard probe', now());          -- FK violation
--   --   (b) an UPDATE that MOVES an existing row's gym_id onto the wrong
--   --       tenant fails the same way.
--   --   (c) an UPDATE of an UNRELATED column of a pre-existing mismatched row
--   --       still succeeds (NOT VALID does not police unrelated-column writes).
--
-- ============================== ROLLBACK =====================================
-- Applying this draft changed NOTHING about existing rows, so rollback is a
-- plain drop of the child FK. The parent index is NOT dropped here: it is the
-- SAME object PR #262's historical clearance created and other code may rely
-- on. Drop it separately, ONLY after confirming this draft was the one that
-- created it (the preflight check-2 count was 0 before apply) and no other
-- object references it:
--
--   BEGIN;
--   ALTER TABLE public.media_asset
--     DROP CONSTRAINT IF EXISTS media_asset_source_gym_fkey;
--   COMMIT;
--
--   -- Only if this draft independently owns the index (preflight check 2
--   -- returned 0 before apply) AND nothing references it:
--   -- DROP INDEX IF EXISTS public.media_source_id_gym_key;
--
-- ============================ FUTURE VALIDATION ==============================
-- Only AFTER the 95 mismatches are reconciled by evidence-reviewed per-row
-- rebinds (per-row operator ruling; the sync-side guard and any portal re-bind
-- flow decide the correct owner — NEVER a bulk UPDATE guessed from one side,
-- and the PR #262 immutable-gym guard forbids a plain UPDATE of
-- media_source.gym_id anyway):
--   1. Re-run preflight check 1; require 0 mismatches.
--   2. ALTER TABLE public.media_asset VALIDATE CONSTRAINT media_asset_source_gym_fkey;
--   3. The unique index must stay: a FK depends on its parent key, so
--      DROP INDEX public.media_source_id_gym_key is refused while the FK exists.
--      That is intended — it is also a cheap uniqueness assertion on
--      (id, gym_id) shared with the PR #262 clearance.
--
-- ============================ HELD / OUT OF SCOPE ============================
-- * This draft does NOT validate, NOT rewrite, NOT delete, NOT quarantine the
--   95 existing mismatches. They remain readable and unrelated-column-updatable
--   exactly as before.
-- * It does NOT touch media_source.gym_id on any row (ownership rewrites are a
--   separate, by-hand, per-row operator action with before/after evidence).
-- * It does NOT add any trigger, RLS policy, or application change; the sync
--   guard lives in agent/jobs/sync_gym_media.py and is enforced in code for
--   the read/insert path regardless of whether this constraint is applied.
