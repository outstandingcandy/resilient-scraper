"""Task queue contract.

The framework defines **what** the queue must expose; the calling application
decides **where** to store tasks. resilient_scraper does not ship a default
persistence layer — inject an implementation that satisfies this Protocol.

A reference Postgres+SQLAlchemy implementation is still available under
``resilient_scraper.service.postgres_queue`` for applications that want it, but
it is no longer auto-wired into the Worker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TaskQueue(Protocol):
    """Async task-queue contract consumed by :class:`Worker`.

    Implementations must be safe to invoke from an asyncio event loop. Methods
    that are called from worker helper threads (login screenshot / SMS code
    storage) are dispatched via ``asyncio.run_coroutine_threadsafe`` — they are
    still awaited on the main loop.

    Status strings exchanged with this interface use the values defined in
    :class:`resilient_scraper.models.TaskStatus`.
    """

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------
    async def claim_task(
        self,
        worker_id: str,
        task_types: list[str],
        stale_minutes: int = 5,
    ) -> dict[str, Any] | None:
        """Atomically claim the oldest runnable task matching ``task_types``.

        Implementations are expected to also reset stale claimed/processing
        tasks (no heartbeat within ``stale_minutes``) back to pending before
        claiming.

        Returns a mapping shaped like a row from ``scraper_tasks`` (keys:
        ``id``, ``task_type``, ``task_key``, ``payload``, ``priority``,
        ``status``, ``attempts``, ``max_attempts``, ``scheduled_for``) or
        ``None`` if nothing is available.
        """
        ...

    async def get_task(self, task_id: int) -> dict[str, Any] | None: ...

    async def update_status(self, task_id: int, status: str) -> None: ...

    async def update_heartbeat(self, task_id: int) -> None: ...

    async def complete_task(
        self,
        task_id: int,
        result_data: dict[str, Any],
        worker_id: str,
        duration: float,
    ) -> None: ...

    async def complete_task_no_data(
        self,
        task_id: int,
        reason: str,
        worker_id: str,
        duration: float,
    ) -> None: ...

    async def fail_task(
        self,
        task_id: int,
        error: str,
        worker_id: str,
        duration: float,
        retry: bool = True,
    ) -> None: ...

    # ------------------------------------------------------------------
    # Worker registry
    # ------------------------------------------------------------------
    async def register_worker(
        self, worker_id: str, metadata: dict[str, Any] | None = None
    ) -> None: ...

    async def deactivate_worker(self, worker_id: str) -> None: ...

    async def update_worker_heartbeat(
        self, worker_id: str, current_task_id: int | None = None
    ) -> None: ...

    async def increment_worker_completed(self, worker_id: str) -> None: ...

    # ------------------------------------------------------------------
    # Login interaction (optional, may no-op for task types that don't need login)
    # ------------------------------------------------------------------
    async def set_login_required(
        self, task_id: int, screenshot_data: bytes, phase: str = "qr_scan"
    ) -> None: ...

    async def update_login_screenshot(
        self, task_id: int, screenshot_url: str
    ) -> None: ...

    async def clear_login_screenshot(self, task_id: int) -> None: ...

    async def submit_user_input(self, task_id: int, value: str) -> int: ...

    async def consume_user_input(self, task_id: int) -> str | None: ...


# Kept for applications that want a type-level mnemonic of what a task row
# looks like; the Worker itself deals in plain dicts.
TaskRow = dict[str, Any]


__all__ = ["TaskQueue", "TaskRow", "datetime"]
