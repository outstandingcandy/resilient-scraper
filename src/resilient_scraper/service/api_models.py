"""Request/response Pydantic models for the API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from resilient_scraper.models import TaskStatus


# --- Requests ---


class CreateTaskRequest(BaseModel):
    """Request body for POST /tasks."""

    task_type: str
    task_key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    max_attempts: int = 3
    scheduled_for: datetime | None = None


# --- Responses ---


class CreateTaskResponse(BaseModel):
    """Response for POST /tasks."""

    id: int
    status: str = "pending"


class TaskResponse(BaseModel):
    """Response for GET /tasks/{id} and GET /tasks."""

    id: int
    task_type: str
    task_key: str
    status: TaskStatus
    priority: int
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    last_error: str | None
    result: dict[str, Any] | None
    scheduled_for: datetime
    created_at: datetime
    completed_at: datetime | None
    login_required: bool = False


class WorkerResponse(BaseModel):
    """Response for GET /workers."""

    worker_id: str
    status: str
    last_heartbeat: datetime
    tasks_completed: int
    current_task_id: int | None
    started_at: datetime


class ScraperTypeResponse(BaseModel):
    """Response for GET /scrapers."""

    task_type: str
    requires_browser: bool
    description: str


class StatsResponse(BaseModel):
    """Response for GET /stats."""

    pending: int = 0
    claimed: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0
    no_data: int = 0
    login_required: int = 0
    workers_active: int = 0
    pending_by_type: dict[str, int] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: str
    database: bool
    workers_active: int
    tasks_pending: int
