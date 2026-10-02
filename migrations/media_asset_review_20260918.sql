-- Additive quarantine: existing Drive media remains indexed, but cannot be selected
-- until a human review and recorded moderation/people/consent evidence exist.
ALTER TABLE media_asset
  ADD COLUMN IF NOT EXISTS review_status text NOT NULL DEFAULT 'pending_review'
    CHECK (review_status IN ('pending_review', 'approved', 'rejected')),
  ADD COLUMN IF NOT EXISTS reviewed_by text,
  ADD COLUMN IF NOT EXISTS reviewed_at timestamptz,
  ADD COLUMN IF NOT EXISTS review_note text,
  ADD COLUMN IF NOT EXISTS moderation_status text NOT NULL DEFAULT 'pending'
    CHECK (moderation_status IN ('pending', 'clean', 'flagged', 'rejected')),
  ADD COLUMN IF NOT EXISTS moderation_json jsonb,
  ADD COLUMN IF NOT EXISTS people_detected boolean,
  ADD COLUMN IF NOT EXISTS consent_status text NOT NULL DEFAULT 'pending'
    CHECK (consent_status IN ('not_required', 'pending', 'granted', 'denied')),
  ADD COLUMN IF NOT EXISTS consent_member_ref text,
  ADD COLUMN IF NOT EXISTS release_ref text,
  ADD COLUMN IF NOT EXISTS consent_expires_at timestamptz;

CREATE INDEX IF NOT EXISTS media_asset_review_queue_idx
  ON media_asset (gym_id, review_status, indexed_at DESC);
