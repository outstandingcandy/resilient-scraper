"""Async database engine and schema management for the task queue."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

from resilient_scraper.service.config import DatabaseSettings

logger = logging.getLogger("resilient_scraper.service.database")

QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS scraper_tasks (
    id            BIGSERIAL PRIMARY KEY,
    task_type     VARCHAR(50) NOT NULL,
    task_key      VARCHAR(500) NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',
    priority      INTEGER NOT NULL DEFAULT 0,
    payload       JSONB NOT NULL DEFAULT '{}',
    claimed_by    VARCHAR(100),
    claimed_at    TIMESTAMPTZ,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    last_error    TEXT,
    result        JSONB,
    scheduled_for TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    heartbeat_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS scraper_results (
    id               BIGSERIAL PRIMARY KEY,
    task_id          BIGINT NOT NULL REFERENCES scraper_tasks(id) ON DELETE CASCADE,
    worker_id        VARCHAR(100) NOT NULL,
    success          BOOLEAN NOT NULL,
    duration_seconds NUMERIC(10, 3),
    result           JSONB,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scraper_screenshots (
    id             BIGSERIAL PRIMARY KEY,
    task_id        BIGINT NOT NULL REFERENCES scraper_tasks(id) ON DELETE CASCADE,
    data           BYTEA,
    screenshot_url VARCHAR(1000),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scraper_user_inputs (
    id         BIGSERIAL PRIMARY KEY,
    task_id    BIGINT NOT NULL REFERENCES scraper_tasks(id) ON DELETE CASCADE,
    value      TEXT NOT NULL,
    consumed   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scraper_workers (
    id              BIGSERIAL PRIMARY KEY,
    worker_id       VARCHAR(100) UNIQUE NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tasks_completed INTEGER NOT NULL DEFAULT 0,
    current_task_id BIGINT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB NOT NULL DEFAULT '{}'
);
"""

QUEUE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_claim ON scraper_tasks (status, scheduled_for, priority DESC) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_status ON scraper_tasks (status)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_type ON scraper_tasks (task_type)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_results_task ON scraper_results (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_screenshots_task ON scraper_screenshots (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_user_inputs_task ON scraper_user_inputs (task_id, consumed)",
]

# Partial unique index for dedup — separate because ON CONFLICT needs it
DEDUP_INDEX = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_scraper_tasks_type_key_active'
    ) THEN
        CREATE UNIQUE INDEX idx_scraper_tasks_type_key_active
        ON scraper_tasks (task_type, task_key)
        WHERE status IN ('pending', 'claimed', 'processing', 'login_required');
    END IF;
END $$;
"""


class Database:
    """Async database manager."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._engine = create_async_engine(
            settings.url,
            pool_size=settings.pool_size,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    async def ensure_tables(self) -> None:
        """Create queue tables and indexes if they don't exist."""
        async with self._engine.begin() as conn:
            for statement in QUEUE_DDL.split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(text(statement))
            for idx_sql in QUEUE_INDEXES:
                await conn.execute(text(idx_sql))
            await conn.execute(text(DEDUP_INDEX))
        logger.info("Queue tables ensured")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Provide an async session."""
        async with self._session_factory() as session:
            yield session

    async def close(self) -> None:
        """Close the engine."""
        await self._engine.dispose()
