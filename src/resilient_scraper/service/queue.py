"""PostgreSQL-backed task queue with SELECT FOR UPDATE SKIP LOCKED."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from resilient_scraper.service.database import Database

logger = logging.getLogger("resilient_scraper.service.queue")


class TaskQueue:
    """Async task queue backed by PostgreSQL."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add_task(
        self,
        task_type: str,
        task_key: str,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        scheduled_for: datetime | None = None,
    ) -> int | None:
        """Add a task. Returns task id, or None if duplicate active task exists."""
        scheduled = scheduled_for or datetime.now(timezone.utc)
        async with self._db.session() as session:
            result = await session.execute(
                text("""
                    INSERT INTO scraper_tasks (task_type, task_key, payload, priority, max_attempts, scheduled_for)
                    VALUES (:task_type, :task_key, CAST(:payload AS jsonb), :priority, :max_attempts, :scheduled_for)
                    ON CONFLICT (task_type, task_key)
                        WHERE status IN ('pending', 'claimed', 'processing', 'login_required')
                    DO NOTHING
                    RETURNING id
                """),
                {
                    "task_type": task_type,
                    "task_key": task_key,
                    "payload": _json_dumps(payload or {}),
                    "priority": priority,
                    "max_attempts": max_attempts,
                    "scheduled_for": scheduled,
                },
            )
            await session.commit()
            row = result.fetchone()
            return row[0] if row else None

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        """Get a task by id."""
        async with self._db.session() as session:
            result = await session.execute(
                text("SELECT * FROM scraper_tasks WHERE id = :id"),
                {"id": task_id},
            )
            row = result.mappings().fetchone()
            return dict(row) if row else None

    async def list_tasks(
        self,
        status: str | None = None,
        task_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List tasks with optional filters."""
        conditions = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if status:
            conditions.append("status = :status")
            params["status"] = status
        if task_type:
            conditions.append("task_type = :task_type")
            params["task_type"] = task_type

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM scraper_tasks {where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

        async with self._db.session() as session:
            result = await session.execute(text(query), params)
            return [dict(row) for row in result.mappings().fetchall()]

    async def cancel_task(self, task_id: int) -> bool:
        """Cancel a pending task. Returns True if cancelled."""
        async with self._db.session() as session:
            result = await session.execute(
                text("""
                    UPDATE scraper_tasks
                    SET status = 'failed', last_error = 'cancelled', completed_at = NOW()
                    WHERE id = :id AND status = 'pending'
                """),
                {"id": task_id},
            )
            await session.commit()
            return result.rowcount > 0

    async def claim_task(
        self,
        worker_id: str,
        task_types: list[str],
        stale_minutes: int = 5,
    ) -> dict[str, Any] | None:
        """Claim one pending task atomically. Also resets stale tasks."""
        async with self._db.session() as session:
            # Reset stale tasks (claimed/processing but no heartbeat)
            await session.execute(
                text("""
                    UPDATE scraper_tasks
                    SET status = 'pending', claimed_by = NULL, claimed_at = NULL
                    WHERE status IN ('claimed', 'processing')
                      AND heartbeat_at < NOW() - MAKE_INTERVAL(mins => :stale_minutes)
                """),
                {"stale_minutes": stale_minutes},
            )

            # Claim one task (parameterized to prevent SQL injection)
            result = await session.execute(
                text("""
                    WITH next_task AS (
                        SELECT id FROM scraper_tasks
                        WHERE status = 'pending'
                          AND scheduled_for <= NOW()
                          AND task_type = ANY(:task_types)
                        ORDER BY priority DESC, scheduled_for ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE scraper_tasks t
                    SET status = 'claimed',
                        claimed_by = :worker_id,
                        claimed_at = NOW(),
                        heartbeat_at = NOW(),
                        attempts = attempts + 1
                    FROM next_task
                    WHERE t.id = next_task.id
                    RETURNING t.*
                """),
                {"worker_id": worker_id, "task_types": task_types},
            )
            await session.commit()
            row = result.mappings().fetchone()
            return dict(row) if row else None

    async def update_status(self, task_id: int, status: str) -> None:
        """Update task status."""
        async with self._db.session() as session:
            await session.execute(
                text("UPDATE scraper_tasks SET status = :status, heartbeat_at = NOW() WHERE id = :id"),
                {"id": task_id, "status": status},
            )
            await session.commit()

    async def update_heartbeat(self, task_id: int) -> None:
        """Update task heartbeat timestamp."""
        async with self._db.session() as session:
            await session.execute(
                text("UPDATE scraper_tasks SET heartbeat_at = NOW() WHERE id = :id"),
                {"id": task_id},
            )
            await session.commit()

    async def complete_task(
        self,
        task_id: int,
        result_data: dict[str, Any],
        worker_id: str,
        duration: float,
    ) -> None:
        """Mark task as completed and record result."""
        async with self._db.session() as session:
            await session.execute(
                text("""
                    UPDATE scraper_tasks
                    SET status = 'completed', result = CAST(:result AS jsonb),
                        completed_at = NOW(), heartbeat_at = NOW()
                    WHERE id = :id
                """),
                {"id": task_id, "result": _json_dumps(result_data)},
            )
            await session.execute(
                text("""
                    INSERT INTO scraper_results (task_id, worker_id, success, duration_seconds, result)
                    VALUES (:task_id, :worker_id, TRUE, :duration, CAST(:result AS jsonb))
                """),
                {
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "duration": duration,
                    "result": _json_dumps(result_data),
                },
            )
            await session.commit()

    async def complete_task_no_data(
        self,
        task_id: int,
        reason: str,
        worker_id: str,
        duration: float,
    ) -> None:
        """Mark task as no_data (not a failure, just nothing found)."""
        async with self._db.session() as session:
            await session.execute(
                text("""
                    UPDATE scraper_tasks
                    SET status = 'no_data', last_error = :reason,
                        completed_at = NOW(), heartbeat_at = NOW()
                    WHERE id = :id
                """),
                {"id": task_id, "reason": reason},
            )
            await session.execute(
                text("""
                    INSERT INTO scraper_results (task_id, worker_id, success, duration_seconds, error)
                    VALUES (:task_id, :worker_id, FALSE, :duration, :reason)
                """),
                {
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "duration": duration,
                    "reason": reason,
                },
            )
            await session.commit()

    async def fail_task(
        self,
        task_id: int,
        error: str,
        worker_id: str,
        duration: float,
        retry: bool = True,
    ) -> None:
        """Mark task as failed, optionally scheduling a retry with exponential backoff."""
        async with self._db.session() as session:
            # Get current attempt count
            row = await session.execute(
                text("SELECT attempts, max_attempts FROM scraper_tasks WHERE id = :id"),
                {"id": task_id},
            )
            task_info = row.mappings().fetchone()

            if retry and task_info and task_info["attempts"] < task_info["max_attempts"]:
                # Exponential backoff: min(5 * 2^(attempt-1), 60) minutes
                backoff = min(5 * (2 ** (task_info["attempts"] - 1)), 60)
                await session.execute(
                    text("""
                        UPDATE scraper_tasks
                        SET status = 'pending',
                            last_error = :error,
                            claimed_by = NULL,
                            claimed_at = NULL,
                            heartbeat_at = NULL,
                            scheduled_for = NOW() + MAKE_INTERVAL(mins => :backoff)
                        WHERE id = :id
                    """),
                    {"id": task_id, "error": error, "backoff": backoff},
                )
            else:
                await session.execute(
                    text("""
                        UPDATE scraper_tasks
                        SET status = 'failed', last_error = :error,
                            completed_at = NOW(), heartbeat_at = NOW()
                        WHERE id = :id
                    """),
                    {"id": task_id, "error": error},
                )

            # Record execution
            await session.execute(
                text("""
                    INSERT INTO scraper_results (task_id, worker_id, success, duration_seconds, error)
                    VALUES (:task_id, :worker_id, FALSE, :duration, :error)
                """),
                {
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "duration": duration,
                    "error": error,
                },
            )
            await session.commit()

    async def get_stats(self) -> dict[str, Any]:
        """Get queue statistics."""
        async with self._db.session() as session:
            # Status counts
            result = await session.execute(
                text("SELECT status, COUNT(*) as cnt FROM scraper_tasks GROUP BY status")
            )
            counts = {row["status"]: row["cnt"] for row in result.mappings()}

            # Pending by type
            result = await session.execute(
                text("""
                    SELECT task_type, COUNT(*) as cnt FROM scraper_tasks
                    WHERE status = 'pending' GROUP BY task_type
                """)
            )
            pending_by_type = {row["task_type"]: row["cnt"] for row in result.mappings()}

            # Active workers
            result = await session.execute(
                text("""
                    SELECT COUNT(*) as cnt FROM scraper_workers
                    WHERE status = 'active'
                      AND last_heartbeat > NOW() - INTERVAL '2 minutes'
                """)
            )
            workers_active = result.scalar() or 0

            return {
                "pending": counts.get("pending", 0),
                "claimed": counts.get("claimed", 0),
                "processing": counts.get("processing", 0),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
                "no_data": counts.get("no_data", 0),
                "login_required": counts.get("login_required", 0),
                "workers_active": workers_active,
                "pending_by_type": pending_by_type,
            }

    async def list_workers(self) -> list[dict[str, Any]]:
        """List all workers."""
        async with self._db.session() as session:
            result = await session.execute(
                text("SELECT * FROM scraper_workers ORDER BY last_heartbeat DESC")
            )
            return [dict(row) for row in result.mappings().fetchall()]

    async def register_worker(self, worker_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Register or update a worker."""
        async with self._db.session() as session:
            await session.execute(
                text("""
                    INSERT INTO scraper_workers (worker_id, metadata)
                    VALUES (:worker_id, CAST(:metadata AS jsonb))
                    ON CONFLICT (worker_id) DO UPDATE
                    SET status = 'active', last_heartbeat = NOW(), metadata = CAST(:metadata AS jsonb)
                """),
                {"worker_id": worker_id, "metadata": _json_dumps(metadata or {})},
            )
            await session.commit()

    async def update_worker_heartbeat(
        self,
        worker_id: str,
        current_task_id: int | None = None,
    ) -> None:
        """Update worker heartbeat."""
        async with self._db.session() as session:
            await session.execute(
                text("""
                    UPDATE scraper_workers
                    SET last_heartbeat = NOW(), current_task_id = :current_task_id
                    WHERE worker_id = :worker_id
                """),
                {"worker_id": worker_id, "current_task_id": current_task_id},
            )
            await session.commit()

    async def deactivate_worker(self, worker_id: str) -> None:
        """Mark worker as stopped."""
        async with self._db.session() as session:
            await session.execute(
                text("""
                    UPDATE scraper_workers
                    SET status = 'stopped', current_task_id = NULL
                    WHERE worker_id = :worker_id
                """),
                {"worker_id": worker_id},
            )
            await session.commit()

    async def set_login_required(
        self, task_id: int, screenshot_data: bytes, phase: str = "qr_scan"
    ) -> None:
        """Mark task as login_required and store the screenshot.

        Args:
            task_id: Task ID.
            screenshot_data: PNG screenshot bytes.
            phase: Login phase — "qr_scan" or "sms_verification".
        """
        async with self._db.session() as session:
            # Update task status and phase (only if task is still active,
            # to avoid unique index violation when task was cancelled externally)
            await session.execute(
                text("""
                    UPDATE scraper_tasks
                    SET status = 'login_required', last_error = :phase, heartbeat_at = NOW()
                    WHERE id = :id
                      AND status IN ('pending', 'claimed', 'processing', 'login_required')
                """),
                {"id": task_id, "phase": phase},
            )
            # Upsert screenshot (keep only the latest per task)
            await session.execute(
                text("DELETE FROM scraper_screenshots WHERE task_id = :task_id"),
                {"task_id": task_id},
            )
            await session.execute(
                text("""
                    INSERT INTO scraper_screenshots (task_id, data)
                    VALUES (:task_id, :data)
                """),
                {"task_id": task_id, "data": screenshot_data},
            )
            await session.commit()

    async def update_login_screenshot(self, task_id: int, screenshot_url: str) -> None:
        """Update the screenshot URL for a task (from page screenshot callback)."""
        async with self._db.session() as session:
            await session.execute(
                text("DELETE FROM scraper_screenshots WHERE task_id = :task_id"),
                {"task_id": task_id},
            )
            await session.execute(
                text("""
                    INSERT INTO scraper_screenshots (task_id, screenshot_url)
                    VALUES (:task_id, :screenshot_url)
                """),
                {"task_id": task_id, "screenshot_url": screenshot_url},
            )
            await session.commit()

    async def get_login_screenshot(self, task_id: int) -> dict[str, Any] | None:
        """Get the latest login screenshot for a task.

        Returns:
            Dict with either 'url' (S3 URL string) or 'data' (bytes), or None.
        """
        async with self._db.session() as session:
            result = await session.execute(
                text("""
                    SELECT data, screenshot_url FROM scraper_screenshots
                    WHERE task_id = :task_id
                    ORDER BY created_at DESC LIMIT 1
                """),
                {"task_id": task_id},
            )
            row = result.fetchone()
            if not row:
                return None
            if row[1]:
                return {"url": row[1]}
            if row[0]:
                return {"data": row[0]}
            return None

    async def clear_login_screenshot(self, task_id: int) -> None:
        """Remove login screenshots for a task (after login succeeds)."""
        async with self._db.session() as session:
            await session.execute(
                text("DELETE FROM scraper_screenshots WHERE task_id = :task_id"),
                {"task_id": task_id},
            )
            await session.commit()

    async def submit_user_input(self, task_id: int, value: str) -> int:
        """Submit user input for a task (e.g., SMS verification code)."""
        async with self._db.session() as session:
            result = await session.execute(
                text("""
                    INSERT INTO scraper_user_inputs (task_id, value)
                    VALUES (:task_id, :value) RETURNING id
                """),
                {"task_id": task_id, "value": value},
            )
            row = result.fetchone()
            await session.commit()
            return row[0]

    async def consume_user_input(self, task_id: int) -> str | None:
        """Consume the oldest unconsumed user input for a task. Returns value or None."""
        async with self._db.session() as session:
            result = await session.execute(
                text("""
                    UPDATE scraper_user_inputs
                    SET consumed = TRUE
                    WHERE id = (
                        SELECT id FROM scraper_user_inputs
                        WHERE task_id = :task_id AND consumed = FALSE
                        ORDER BY created_at ASC LIMIT 1
                    )
                    RETURNING value
                """),
                {"task_id": task_id},
            )
            row = result.fetchone()
            await session.commit()
            return row[0] if row else None

    async def increment_worker_completed(self, worker_id: str) -> None:
        """Increment worker completed task count."""
        async with self._db.session() as session:
            await session.execute(
                text("""
                    UPDATE scraper_workers
                    SET tasks_completed = tasks_completed + 1
                    WHERE worker_id = :worker_id
                """),
                {"worker_id": worker_id},
            )
            await session.commit()


def _json_dumps(data: Any) -> str:
    """Serialize to JSON string for JSONB columns."""
    import json
    return json.dumps(data, default=str)
