-- Migration: Unify scraper task queue schema
-- Merges parent project (scraper_tasks) and resilient-scraper (rs_scraper_tasks) into one set of tables.
-- Safe to run on production — all operations are backwards-compatible.

BEGIN;

-- 1a. scraper_tasks: rename heartbeat column
ALTER TABLE scraper_tasks RENAME COLUMN task_heartbeat TO heartbeat_at;

-- 1b. scraper_tasks: widen task_key for XHS keys
ALTER TABLE scraper_tasks ALTER COLUMN task_key TYPE VARCHAR(500);

-- 1c. scraper_tasks: convert timestamps to TIMESTAMPTZ (lossless)
ALTER TABLE scraper_tasks ALTER COLUMN claimed_at TYPE TIMESTAMPTZ;
ALTER TABLE scraper_tasks ALTER COLUMN scheduled_for TYPE TIMESTAMPTZ;
ALTER TABLE scraper_tasks ALTER COLUMN created_at TYPE TIMESTAMPTZ;
ALTER TABLE scraper_tasks ALTER COLUMN completed_at TYPE TIMESTAMPTZ;
ALTER TABLE scraper_tasks ALTER COLUMN heartbeat_at TYPE TIMESTAMPTZ;

-- 1d. scraper_tasks: add NOT NULL + DEFAULT constraints
ALTER TABLE scraper_tasks ALTER COLUMN task_type SET NOT NULL;
ALTER TABLE scraper_tasks ALTER COLUMN task_key SET NOT NULL;
ALTER TABLE scraper_tasks ALTER COLUMN status SET NOT NULL, ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE scraper_tasks ALTER COLUMN priority SET NOT NULL, ALTER COLUMN priority SET DEFAULT 0;
ALTER TABLE scraper_tasks ALTER COLUMN payload SET NOT NULL, ALTER COLUMN payload SET DEFAULT '{}';
ALTER TABLE scraper_tasks ALTER COLUMN attempts SET NOT NULL, ALTER COLUMN attempts SET DEFAULT 0;
ALTER TABLE scraper_tasks ALTER COLUMN max_attempts SET NOT NULL, ALTER COLUMN max_attempts SET DEFAULT 3;
ALTER TABLE scraper_tasks ALTER COLUMN scheduled_for SET NOT NULL, ALTER COLUMN scheduled_for SET DEFAULT NOW();
ALTER TABLE scraper_tasks ALTER COLUMN created_at SET NOT NULL, ALTER COLUMN created_at SET DEFAULT NOW();

-- 1e. scraper_tasks: partial unique index for dedup
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_scraper_tasks_type_key_active') THEN
        CREATE UNIQUE INDEX idx_scraper_tasks_type_key_active
        ON scraper_tasks (task_type, task_key)
        WHERE status IN ('pending', 'claimed', 'processing', 'login_required');
    END IF;
END $$;

-- 1f. scraper_tasks: claim index
CREATE INDEX IF NOT EXISTS idx_scraper_tasks_claim
ON scraper_tasks (status, scheduled_for, priority DESC) WHERE status = 'pending';

-- 1g. scraper_workers: add started_at column
ALTER TABLE scraper_workers ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ DEFAULT NOW();

-- 1h. scraper_workers: convert timestamp to TIMESTAMPTZ
ALTER TABLE scraper_workers ALTER COLUMN last_heartbeat TYPE TIMESTAMPTZ;

-- 1i. scraper_results: convert timestamp to TIMESTAMPTZ
ALTER TABLE scraper_results ALTER COLUMN created_at TYPE TIMESTAMPTZ;

-- 1j. scraper_screenshots table
CREATE TABLE IF NOT EXISTS scraper_screenshots (
    id             BIGSERIAL PRIMARY KEY,
    task_id        BIGINT NOT NULL REFERENCES scraper_tasks(id) ON DELETE CASCADE,
    data           BYTEA,
    screenshot_url VARCHAR(1000),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scraper_screenshots_task ON scraper_screenshots (task_id);

-- 1k. scraper_user_inputs table
CREATE TABLE IF NOT EXISTS scraper_user_inputs (
    id         BIGSERIAL PRIMARY KEY,
    task_id    BIGINT NOT NULL REFERENCES scraper_tasks(id) ON DELETE CASCADE,
    value      TEXT NOT NULL,
    consumed   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scraper_user_inputs_task ON scraper_user_inputs (task_id, consumed);

-- 1l. Drop empty rs_* tables (resilient-scraper tables that were never populated)
DROP TABLE IF EXISTS rs_scraper_screenshots CASCADE;
DROP TABLE IF EXISTS rs_scraper_user_inputs CASCADE;
DROP TABLE IF EXISTS rs_scraper_results CASCADE;
DROP TABLE IF EXISTS rs_scraper_workers CASCADE;
DROP TABLE IF EXISTS rs_scraper_tasks CASCADE;

COMMIT;
