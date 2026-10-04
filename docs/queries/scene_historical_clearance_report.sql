-- scene_historical_clearance_report.sql — REPORT-ONLY clearance manifest (PR262 draft)
--
-- Purpose: one row per APPROVED PHOTO asset with every positive use signal Echo
-- can observe today, and a CONSERVATIVE status. This query NEVER returns
-- 'CLEARED': absence of joins, used_count = 0, indexed_at, or a missing URL
-- match is NOT proof of no prior use. Byte hash (content_hash) alone cannot
-- prove non-use either; pHash is review-only and NOT available in this schema,
-- so no perceptual-match logic is attempted here.
--
-- READ-ONLY: plain SELECT + CTEs against media_asset / media_source /
-- content_calendar. No writes, no DDL, no arbitrary user text or media bytes in
-- output (ids, gym keys, integer counts and boolean flags only).
--
-- Status rule:
--   KNOWN_USED_OR_HOLD  — any same-tenant published asset-id link, same-tenant
--                         exact source-URL link, cross-tenant match (HOLD), or
--                         used_count > 0.
--   UNKNOWN_HELD        — everything else. Never CLEARED.
--
-- Safe to run at ~3605 assets / ~1316 published posts; all joins are on
-- indexed/id columns or exact URL equality within the approved-photos subset.
--
-- Target dialect: Postgres (Supabase). The statement set is also SQLite-runnable
-- (see tests/test_scene_historical_clearance_report.py), which validates syntax
-- locally; production Postgres syntax (timestamptz, jsonb) is not exercised here
-- because this query touches none of those columns.

-- ---------------------------------------------------------------------------
-- Statement 1: per-asset clearance manifest (exactly one row per approved photo)
-- ---------------------------------------------------------------------------
WITH approved_photos AS (
  SELECT a.id, a.source_id, a.gym_id, a.content_hash, a.rendition_url, a.used_count
  FROM media_asset AS a
  WHERE a.kind = 'photo'
    AND a.review_status = 'approved'
),
published_posts AS (
  SELECT p.id, p.gym_id, p.source_media_asset_id, p.source_media_url
  FROM content_calendar AS p
  WHERE p.status = 'published'
     OR p.published_at IS NOT NULL
),
same_tenant_asset_links AS (
  SELECT p.source_media_asset_id AS asset_id, COUNT(DISTINCT p.id) AS post_count
  FROM published_posts AS p
  JOIN approved_photos AS a
    ON a.id = p.source_media_asset_id
   AND a.gym_id = p.gym_id
  GROUP BY p.source_media_asset_id
),
same_tenant_url_links AS (
  SELECT a.id AS asset_id, COUNT(DISTINCT p.id) AS post_count
  FROM published_posts AS p
  JOIN approved_photos AS a
    ON NULLIF(TRIM(p.source_media_url), '') IS NOT NULL
   AND NULLIF(TRIM(a.rendition_url), '') IS NOT NULL
   AND p.source_media_url = a.rendition_url
   AND a.gym_id = p.gym_id
  GROUP BY a.id
),
cross_tenant_links AS (
  SELECT a.id AS asset_id, COUNT(DISTINCT p.id) AS post_count
  FROM published_posts AS p
  JOIN approved_photos AS a
    ON (a.id = p.source_media_asset_id
        OR (NULLIF(TRIM(p.source_media_url), '') IS NOT NULL
            AND NULLIF(TRIM(a.rendition_url), '') IS NOT NULL
            AND p.source_media_url = a.rendition_url))
   AND a.gym_id <> p.gym_id
  GROUP BY a.id
)
SELECT
  a.id                                    AS asset_id,
  a.gym_id                                AS gym_id,
  (s.id IS NULL OR s.gym_id <> a.gym_id)  AS source_ownership_mismatch,
  (a.content_hash IS NOT NULL)            AS has_content_hash,
  COALESCE(al.post_count, 0)              AS same_tenant_published_asset_id_matches,
  COALESCE(ul.post_count, 0)              AS same_tenant_published_source_url_matches,
  COALESCE(ct.post_count, 0)              AS cross_tenant_match_count,
  (a.used_count > 0)                      AS used_count_positive,
  CASE
    WHEN COALESCE(ct.post_count, 0) > 0
      THEN 'KNOWN_USED_OR_HOLD'   -- cross-tenant match: hold, not a use grant
    WHEN COALESCE(al.post_count, 0) > 0
      THEN 'KNOWN_USED_OR_HOLD'
    WHEN COALESCE(ul.post_count, 0) > 0
      THEN 'KNOWN_USED_OR_HOLD'
    WHEN a.used_count > 0
      THEN 'KNOWN_USED_OR_HOLD'
    ELSE 'UNKNOWN_HELD'
  END                                     AS conservative_status
FROM approved_photos AS a
LEFT JOIN media_source AS s
  ON s.id = a.source_id
LEFT JOIN same_tenant_asset_links AS al
  ON al.asset_id = a.id
LEFT JOIN same_tenant_url_links AS ul
  ON ul.asset_id = a.id
LEFT JOIN cross_tenant_links AS ct
  ON ct.asset_id = a.id
ORDER BY a.gym_id, a.id;

-- ---------------------------------------------------------------------------
-- Statement 2: explicit missing-history cohort counts (aggregate only; these
-- are the cohorts the per-asset manifest CANNOT clear by construction)
-- ---------------------------------------------------------------------------
SELECT
  (SELECT COUNT(*) FROM media_asset
    WHERE kind = 'photo' AND review_status = 'approved')       AS approved_photo_assets,
  (SELECT COUNT(*) FROM media_asset ma
    WHERE ma.kind = 'photo' AND ma.review_status = 'approved'
      AND ma.content_hash IS NULL)                             AS approved_photos_missing_content_hash,
  (SELECT COUNT(*) FROM media_asset ma
    LEFT JOIN media_source ms ON ms.id = ma.source_id
    WHERE ma.kind = 'photo' AND ma.review_status = 'approved'
      AND (ms.id IS NULL OR ms.gym_id <> ma.gym_id))           AS approved_photos_source_ownership_mismatch,
  (SELECT COUNT(*) FROM media_asset
    WHERE kind = 'photo' AND review_status = 'approved'
      AND used_count = 0)                                      AS approved_photos_used_count_zero,
  (SELECT COUNT(*) FROM content_calendar
    WHERE status = 'published' OR published_at IS NOT NULL)    AS published_or_dated_posts,
  (SELECT COUNT(*) FROM content_calendar
    WHERE (status = 'published' OR published_at IS NOT NULL)
      AND NULLIF(TRIM(source_media_url), '') IS NULL)          AS published_posts_missing_source_media_url,
  (SELECT COUNT(*) FROM content_calendar
    WHERE (status = 'published' OR published_at IS NOT NULL)
      AND NULLIF(TRIM(source_media_asset_id), '') IS NULL)     AS published_posts_missing_source_media_asset_id,
  -- assets whose only possible evidence is used_count (no hash, no published
  -- link columns populated anywhere): strictly UNKNOWN_HELD, never CLEARED
  (SELECT COUNT(*) FROM media_asset ma
    WHERE ma.kind = 'photo' AND ma.review_status = 'approved'
      AND ma.used_count = 0
      AND NOT EXISTS (
        SELECT 1 FROM content_calendar p
        WHERE (p.status = 'published' OR p.published_at IS NOT NULL)
          AND (p.source_media_asset_id = ma.id
               OR (NULLIF(TRIM(p.source_media_url), '') IS NOT NULL
                   AND NULLIF(TRIM(ma.rendition_url), '') IS NOT NULL
                   AND p.source_media_url = ma.rendition_url)))) AS approved_photos_no_observable_history;
