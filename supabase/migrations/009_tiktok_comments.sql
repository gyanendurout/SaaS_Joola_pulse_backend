-- ============================================================
-- Migration 009 — TikTok Comments table
-- Idempotent: safe to run multiple times.
-- Paste into Supabase SQL editor and click Run.
-- ============================================================

DO $$ BEGIN

  -- ------------------------------------------------------------
  -- tiktok_comments : one row per TikTok comment (unique on tiktok_comment_id)
  -- ------------------------------------------------------------
  CREATE TABLE IF NOT EXISTS tiktok_comments (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tiktok_comment_id   TEXT        UNIQUE NOT NULL,
    video_id            UUID        REFERENCES tiktok_videos(id) ON DELETE SET NULL,
    brand_id            UUID        NOT NULL,
    commenter_username  TEXT,
    comment_text        TEXT,
    comment_likes       INTEGER     DEFAULT 0,
    is_brand_reply      BOOLEAN     DEFAULT FALSE,
    posted_at           TIMESTAMPTZ,
    scraped_at          TIMESTAMPTZ DEFAULT NOW(),

    -- AI enrichment (populated separately)
    sentiment_label     TEXT,
    sentiment_score     NUMERIC(4,3),
    topics              TEXT[],
    is_crisis           BOOLEAN,
    is_opportunity      BOOLEAN
  );

END $$;

-- Indexes (CREATE INDEX IF NOT EXISTS is safe outside DO block)
CREATE INDEX IF NOT EXISTS tiktok_comments_brand_id_idx
  ON tiktok_comments(brand_id);

CREATE INDEX IF NOT EXISTS tiktok_comments_video_id_idx
  ON tiktok_comments(video_id);

CREATE INDEX IF NOT EXISTS tiktok_comments_posted_at_idx
  ON tiktok_comments(posted_at DESC);

CREATE INDEX IF NOT EXISTS tiktok_comments_scraped_at_idx
  ON tiktok_comments(scraped_at DESC);

-- Add any missing columns to existing tiktok_comments table (idempotent)
ALTER TABLE tiktok_comments ADD COLUMN IF NOT EXISTS sentiment_label  TEXT;
ALTER TABLE tiktok_comments ADD COLUMN IF NOT EXISTS sentiment_score  NUMERIC(4,3);
ALTER TABLE tiktok_comments ADD COLUMN IF NOT EXISTS topics           TEXT[];
ALTER TABLE tiktok_comments ADD COLUMN IF NOT EXISTS is_crisis        BOOLEAN;
ALTER TABLE tiktok_comments ADD COLUMN IF NOT EXISTS is_opportunity   BOOLEAN;

-- Force PostgREST to reload schema cache
NOTIFY pgrst, 'reload schema';
