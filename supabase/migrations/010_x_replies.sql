-- ============================================================
-- Migration 010 — X (Twitter) replies table
-- Idempotent: safe to run multiple times.
-- Paste into Supabase SQL editor and click Run.
-- ============================================================

-- ------------------------------------------------------------
-- x_replies : one row per reply tweet scraped from x_posts
-- ------------------------------------------------------------

DO $$ BEGIN

  CREATE TABLE IF NOT EXISTS x_replies (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tweet_reply_id   TEXT        NOT NULL UNIQUE,
    post_id          UUID        REFERENCES x_posts(id) ON DELETE SET NULL,
    brand_id         UUID        NOT NULL,
    replier_username TEXT,
    reply_text       TEXT,
    reply_likes      INTEGER     DEFAULT 0,
    retweet_count    INTEGER     DEFAULT 0,
    is_brand_reply   BOOLEAN     DEFAULT FALSE,
    posted_at        TIMESTAMPTZ,
    scraped_at       TIMESTAMPTZ DEFAULT NOW(),

    -- AI enrichment columns (populated by a separate enrichment pipeline)
    sentiment_label  TEXT,
    sentiment_score  NUMERIC(4,3),
    topics           TEXT[],
    is_crisis        BOOLEAN,
    is_opportunity   BOOLEAN
  );

END $$;

-- Indexes
CREATE INDEX IF NOT EXISTS x_replies_brand_id_idx  ON x_replies(brand_id);
CREATE INDEX IF NOT EXISTS x_replies_post_id_idx   ON x_replies(post_id);
CREATE INDEX IF NOT EXISTS x_replies_posted_at_idx ON x_replies(posted_at DESC);

-- Add any missing columns to existing x_replies table (safe to re-run)
ALTER TABLE x_replies ADD COLUMN IF NOT EXISTS sentiment_label  TEXT;
ALTER TABLE x_replies ADD COLUMN IF NOT EXISTS sentiment_score  NUMERIC(4,3);
ALTER TABLE x_replies ADD COLUMN IF NOT EXISTS topics           TEXT[];
ALTER TABLE x_replies ADD COLUMN IF NOT EXISTS is_crisis        BOOLEAN;
ALTER TABLE x_replies ADD COLUMN IF NOT EXISTS is_opportunity   BOOLEAN;
ALTER TABLE x_replies ADD COLUMN IF NOT EXISTS retweet_count    INTEGER DEFAULT 0;
ALTER TABLE x_replies ADD COLUMN IF NOT EXISTS reply_likes      INTEGER DEFAULT 0;

-- Force PostgREST to reload schema cache
NOTIFY pgrst, 'reload schema';
