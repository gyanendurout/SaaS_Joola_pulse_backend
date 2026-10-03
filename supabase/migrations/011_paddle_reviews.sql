-- ============================================================
-- Migration 011 — Paddle Reviews Intelligence
-- Idempotent: safe to run multiple times.
-- Paste into Supabase SQL editor and click Run.
--
-- Sources (see PADDLE_REVIEWS_PLAN.md §2):
--   bazaarvoice -> joola.com          okendo -> selkirk.com
--   judgeme     -> paddletek / crbn   yotpo  -> pickleballcentral.com
--   dicks       -> dickssportinggoods.com (gated on O3)
-- ============================================================

DO $$ BEGIN

  -- ------------------------------------------------------------
  -- paddle_products : one row per (source, source_product_id)
  -- ------------------------------------------------------------
  CREATE TABLE IF NOT EXISTS paddle_products (
    id                  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id            UUID,
    brand               TEXT          NOT NULL DEFAULT 'other',
    source              TEXT          NOT NULL,
    retailer            TEXT,
    source_product_id   TEXT          NOT NULL,
    family_id           TEXT,
    canonical_name      TEXT,
    title               TEXT,
    handle              TEXT,
    product_url         TEXT,
    image_url           TEXT,
    price               NUMERIC(10,2),
    currency            TEXT          DEFAULT 'USD',
    review_count        INTEGER       DEFAULT 0,
    avg_rating          NUMERIC(3,2),
    rating_distribution JSONB,
    gtin                TEXT[],
    is_paddle           BOOLEAN       DEFAULT TRUE,
    is_active           BOOLEAN       DEFAULT TRUE,
    first_seen_at       TIMESTAMPTZ   DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ   DEFAULT NOW(),
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT paddle_products_source_pid_key UNIQUE (source, source_product_id)
  );

  -- ------------------------------------------------------------
  -- paddle_reviews : unified review row across every source
  -- ------------------------------------------------------------
  CREATE TABLE IF NOT EXISTS paddle_reviews (
    id                    UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id              UUID,
    source                TEXT          NOT NULL,
    external_review_id    TEXT          NOT NULL,
    product_id            UUID          REFERENCES paddle_products(id) ON DELETE SET NULL,
    source_product_id     TEXT,
    family_id             TEXT,
    canonical_name        TEXT,
    brand                 TEXT,
    retailer              TEXT,

    -- reviewer_name is NULLABLE on purpose: Bazaarvoice UserNickname can be null,
    -- Judge.me emits "" / "Anonymous". Public display names only — no emails, no order ids.
    reviewer_name         TEXT,
    reviewer_location     TEXT,

    rating                SMALLINT,
    title                 TEXT,
    body                  TEXT,
    pros                  TEXT,
    cons                  TEXT,
    secondary_ratings     JSONB,
    context_values        JSONB,
    posted_at             TIMESTAMPTZ,

    is_verified           BOOLEAN,
    is_recommended        BOOLEAN,
    is_incentivized       BOOLEAN,
    is_syndicated         BOOLEAN       DEFAULT FALSE,
    helpful_count         INTEGER       DEFAULT 0,
    unhelpful_count       INTEGER       DEFAULT 0,

    brand_response        TEXT,
    brand_response_at     TIMESTAMPTZ,
    media_urls            TEXT[],
    language_code         TEXT,

    -- Yotpo ships its own sentiment; kept separate from our LLM enrichment.
    source_sentiment      TEXT,

    content_hash          VARCHAR(64),
    scraped_at            TIMESTAMPTZ   DEFAULT NOW(),

    -- AI enrichment (matches tiktok_comments convention)
    sentiment_label       TEXT,
    sentiment_score       NUMERIC(4,3),
    topics                TEXT[],
    is_crisis             BOOLEAN,
    is_opportunity        BOOLEAN,
    complaint_category    TEXT,
    mentioned_competitors TEXT[],

    created_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT paddle_reviews_source_extid_key UNIQUE (source, external_review_id)
  );

  -- ------------------------------------------------------------
  -- paddle_review_runs : one row per sync job (mirrors news_scrape_runs)
  -- ------------------------------------------------------------
  CREATE TABLE IF NOT EXISTS paddle_review_runs (
    id                  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    status              TEXT          NOT NULL DEFAULT 'pending',
    run_type            TEXT          DEFAULT 'manual',
    stages_total        INTEGER       NOT NULL DEFAULT 0,
    stages_done         INTEGER       NOT NULL DEFAULT 0,
    products_found      INTEGER       NOT NULL DEFAULT 0,
    products_new        INTEGER       NOT NULL DEFAULT 0,
    reviews_found       INTEGER       NOT NULL DEFAULT 0,
    reviews_new         INTEGER       NOT NULL DEFAULT 0,
    reviews_enriched    INTEGER       NOT NULL DEFAULT 0,
    sources_ok          INTEGER       NOT NULL DEFAULT 0,
    sources_failed      INTEGER       NOT NULL DEFAULT 0,
    per_source_stats    JSONB,
    error_message       TEXT,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
  );

  -- ------------------------------------------------------------
  -- paddle_review_errors : per-source error log
  -- ------------------------------------------------------------
  CREATE TABLE IF NOT EXISTS paddle_review_errors (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID        REFERENCES paddle_review_runs(id) ON DELETE CASCADE,
    source         TEXT        NOT NULL,
    stage          TEXT        DEFAULT '',
    target         TEXT        DEFAULT '',
    error_type     TEXT        DEFAULT 'scrape_error',
    error_message  TEXT,
    status_code    INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );

END $$;

-- Indexes (CREATE INDEX IF NOT EXISTS is safe outside a DO block)
CREATE INDEX IF NOT EXISTS paddle_products_brand_idx      ON paddle_products(brand);
CREATE INDEX IF NOT EXISTS paddle_products_brand_id_idx   ON paddle_products(brand_id);
CREATE INDEX IF NOT EXISTS paddle_products_source_idx     ON paddle_products(source);
CREATE INDEX IF NOT EXISTS paddle_products_family_idx     ON paddle_products(family_id);
CREATE INDEX IF NOT EXISTS paddle_products_canonical_idx  ON paddle_products(canonical_name);
CREATE INDEX IF NOT EXISTS paddle_products_active_idx     ON paddle_products(is_active, is_paddle);

CREATE INDEX IF NOT EXISTS paddle_reviews_product_idx     ON paddle_reviews(product_id);
CREATE INDEX IF NOT EXISTS paddle_reviews_brand_idx       ON paddle_reviews(brand);
CREATE INDEX IF NOT EXISTS paddle_reviews_brand_id_idx    ON paddle_reviews(brand_id);
CREATE INDEX IF NOT EXISTS paddle_reviews_source_idx      ON paddle_reviews(source);
CREATE INDEX IF NOT EXISTS paddle_reviews_family_idx      ON paddle_reviews(family_id);
CREATE INDEX IF NOT EXISTS paddle_reviews_posted_idx      ON paddle_reviews(posted_at DESC);
CREATE INDEX IF NOT EXISTS paddle_reviews_rating_idx      ON paddle_reviews(rating);
CREATE INDEX IF NOT EXISTS paddle_reviews_hash_idx        ON paddle_reviews(content_hash);
CREATE INDEX IF NOT EXISTS paddle_reviews_sentiment_idx   ON paddle_reviews(sentiment_label);
CREATE INDEX IF NOT EXISTS paddle_reviews_crisis_idx      ON paddle_reviews(is_crisis);
CREATE INDEX IF NOT EXISTS paddle_reviews_enrich_idx      ON paddle_reviews(sentiment_label, scraped_at DESC);

CREATE INDEX IF NOT EXISTS paddle_review_runs_created_idx ON paddle_review_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS paddle_review_errors_run_idx   ON paddle_review_errors(run_id);

-- Add any missing columns to pre-existing tables (idempotent re-runs / schema drift)
ALTER TABLE paddle_products ADD COLUMN IF NOT EXISTS retailer              TEXT;
ALTER TABLE paddle_products ADD COLUMN IF NOT EXISTS family_id             TEXT;
ALTER TABLE paddle_products ADD COLUMN IF NOT EXISTS rating_distribution   JSONB;
ALTER TABLE paddle_products ADD COLUMN IF NOT EXISTS gtin                  TEXT[];

ALTER TABLE paddle_reviews  ADD COLUMN IF NOT EXISTS source_sentiment      TEXT;
ALTER TABLE paddle_reviews  ADD COLUMN IF NOT EXISTS is_syndicated         BOOLEAN DEFAULT FALSE;
ALTER TABLE paddle_reviews  ADD COLUMN IF NOT EXISTS retailer              TEXT;
ALTER TABLE paddle_reviews  ADD COLUMN IF NOT EXISTS brand                 TEXT;
ALTER TABLE paddle_reviews  ADD COLUMN IF NOT EXISTS complaint_category    TEXT;
ALTER TABLE paddle_reviews  ADD COLUMN IF NOT EXISTS mentioned_competitors TEXT[];

ALTER TABLE paddle_review_runs ADD COLUMN IF NOT EXISTS per_source_stats   JSONB;
ALTER TABLE paddle_review_runs ADD COLUMN IF NOT EXISTS reviews_enriched   INTEGER DEFAULT 0;

-- Rating sanity: 1-5 or NULL. The Judge.me parser must fail loudly rather than
-- silently store NULL ratings (see PADDLE_REVIEWS_PLAN.md §8).
ALTER TABLE paddle_reviews DROP CONSTRAINT IF EXISTS paddle_reviews_rating_check;
ALTER TABLE paddle_reviews ADD  CONSTRAINT paddle_reviews_rating_check
  CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5));

-- Force PostgREST to reload schema cache
NOTIFY pgrst, 'reload schema';
