"""
Pydantic models for resilient scraper.

Provides base task and result types that can be extended by specific
scraper implementations.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    """Status of a scraper task."""

    PENDING = "pending"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    COMPLETED = "completed"
    NO_DATA = "no_data"
    FAILED = "failed"
    LOGIN_REQUIRED = "login_required"
    CANCELLED = "cancelled"


class ScraperTask(BaseModel):
    """A task to be processed by a scraper.

    Attributes:
        id: Unique task identifier.
        task_type: Type of scraper to use.
        task_key: Unique key identifying the target.
        status: Current task status.
        priority: Task priority (higher = more urgent).
        payload: Additional task-specific data.
        claimed_by: Worker ID that claimed this task.
        claimed_at: When the task was claimed.
        attempts: Number of processing attempts.
        max_attempts: Maximum allowed attempts.
        last_error: Error message from last failed attempt.
        result: Task result data.
        scheduled_for: Earliest time to process this task.
        created_at: When the task was created.
        completed_at: When the task was completed.
    """

    id: int | None = None
    task_type: str
    task_key: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    result: dict[str, Any] | None = None
    scheduled_for: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    model_config = ConfigDict(use_enum_values=True)


class ScraperResult(BaseModel):
    """Result from a scraper execution.

    Attributes:
        success: Whether the scrape was successful.
        task_key: Key of the processed task.
        task_type: Type of scraper that processed this.
        data: Extracted data from the scrape.
        error: Error message if failed.
        duration_seconds: How long the scrape took.
        retry_scheduled: Whether a retry has been scheduled.
    """

    success: bool
    task_key: str
    task_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_seconds: float = 0.0
    retry_scheduled: bool = False


class WorkerStatus(str, Enum):
    """Status of a scraper worker."""

    ACTIVE = "active"
    IDLE = "idle"
    BUSY = "busy"
    STOPPED = "stopped"
    ERROR = "error"


class WorkerInfo(BaseModel):
    """Information about a scraper worker.

    Attributes:
        worker_id: Unique worker identifier.
        status: Current worker status.
        last_heartbeat: Last heartbeat timestamp.
        tasks_completed: Total tasks completed by this worker.
        current_task_id: ID of currently processing task.
        metadata: Additional worker metadata.
    """

    worker_id: str
    status: WorkerStatus = WorkerStatus.IDLE
    last_heartbeat: datetime = Field(default_factory=utc_now)
    tasks_completed: int = 0
    current_task_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


class ScraperConfig(BaseModel):
    """Configuration for a scraper type.

    Attributes:
        enabled: Whether this scraper is enabled.
        delay_min: Minimum delay between requests (seconds).
        delay_max: Maximum delay between requests (seconds).
        max_retries: Maximum retry attempts per task.
        timeout: Request timeout (seconds).
        custom_settings: Scraper-specific settings.
    """

    enabled: bool = True
    delay_min: float = 5.0
    delay_max: float = 15.0
    max_retries: int = 3
    timeout: int = 60
    custom_settings: dict[str, Any] = Field(default_factory=dict)
