"""FastAPI application for the resilient-scraper service."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from resilient_scraper.models import TaskStatus
from resilient_scraper.service.api_models import (
    CreateTaskRequest,
    CreateTaskResponse,
    HealthResponse,
    ScraperTypeResponse,
    StatsResponse,
    TaskResponse,
    WorkerResponse,
)
from resilient_scraper.service.config import AutoScaleSettings, ServiceSettings
from resilient_scraper.service.database import Database
from resilient_scraper.service.queue import TaskQueue
from resilient_scraper.service.registry import ScraperRegistry

logger = logging.getLogger("resilient_scraper.service.api")

# Module-level state (set during app creation)
_db: Database | None = None
_queue: TaskQueue | None = None
_registry: ScraperRegistry | None = None


def _get_queue() -> TaskQueue:
    if _queue is None:
        raise RuntimeError("Queue not initialized")
    return _queue


def _get_registry() -> ScraperRegistry:
    if _registry is None:
        raise RuntimeError("Registry not initialized")
    return _registry


def create_app(
    settings: ServiceSettings | None = None,
    registry: ScraperRegistry | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = settings or ServiceSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        global _db, _queue, _registry

        logging.basicConfig(level=settings.log_level)

        # Initialize database and queue
        _db = Database(settings.db)
        await _db.ensure_tables()
        _queue = TaskQueue(_db)

        # Initialize registry with built-in scrapers
        if registry:
            _registry = registry
        else:
            _registry = _create_default_registry()

        # Include scraper-specific routers
        _include_scraper_routers(app, _db)

        # Start auto-scale background task
        autoscale_task = None
        if settings.autoscale.enabled and settings.autoscale.asg_name:
            autoscale_task = asyncio.create_task(
                _auto_scale_loop(settings.autoscale, _queue)
            )

        logger.info("API started")
        yield

        if autoscale_task:
            autoscale_task.cancel()
            try:
                await autoscale_task
            except asyncio.CancelledError:
                pass

        await _db.close()
        logger.info("API stopped")

    app = FastAPI(
        title="Resilient Scraper API",
        version="0.3.0",
        lifespan=lifespan,
    )

    # --- Task Management Endpoints ---

    @app.post("/tasks", response_model=CreateTaskResponse, status_code=201)
    async def create_task(
        req: CreateTaskRequest,
        queue: TaskQueue = Depends(_get_queue),
        reg: ScraperRegistry = Depends(_get_registry),
    ) -> CreateTaskResponse:
        """Submit a new scrape task."""
        if not reg.has(req.task_type):
            raise HTTPException(422, f"Unknown task type: {req.task_type}")

        task_id = await queue.add_task(
            task_type=req.task_type,
            task_key=req.task_key,
            payload=req.payload,
            priority=req.priority,
            max_attempts=req.max_attempts,
            scheduled_for=req.scheduled_for,
        )

        if task_id is None:
            raise HTTPException(409, "Active task already exists for this type/key")

        return CreateTaskResponse(id=task_id)

    @app.get("/tasks/{task_id}", response_model=TaskResponse)
    async def get_task(
        task_id: int,
        queue: TaskQueue = Depends(_get_queue),
    ) -> TaskResponse:
        """Get task status and result."""
        task = await queue.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        task["login_required"] = task.get("status") == "login_required"
        return TaskResponse(**task)

    @app.get("/tasks", response_model=list[TaskResponse])
    async def list_tasks(
        status: TaskStatus | None = None,
        task_type: str | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
        queue: TaskQueue = Depends(_get_queue),
    ) -> list[TaskResponse]:
        """List tasks with optional filters."""
        tasks = await queue.list_tasks(
            status=status.value if status else None,
            task_type=task_type,
            limit=limit,
            offset=offset,
        )
        for t in tasks:
            t["login_required"] = t.get("status") == "login_required"
        return [TaskResponse(**t) for t in tasks]

    @app.delete("/tasks/{task_id}", status_code=204)
    async def cancel_task(
        task_id: int,
        queue: TaskQueue = Depends(_get_queue),
    ) -> None:
        """Cancel a pending task."""
        task = await queue.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task["status"] != "pending":
            raise HTTPException(409, f"Cannot cancel task in '{task['status']}' status")

        await queue.cancel_task(task_id)

    # --- Login Screenshot Endpoint ---

    @app.get("/tasks/{task_id}/screenshot")
    async def get_task_screenshot(
        task_id: int,
        queue: TaskQueue = Depends(_get_queue),
    ) -> Response:
        """Get login QR code screenshot for a task.

        When a scraper encounters a login page (e.g., XHS QR code),
        it stores the screenshot here. The user can view and scan
        the QR code, then the worker will detect login success and
        continue scraping automatically.

        Returns JSON {"url": "..."} if screenshot is stored as S3 URL,
        or raw PNG bytes for legacy BLOB screenshots.
        """
        task = await queue.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")

        screenshot = await queue.get_login_screenshot(task_id)
        if not screenshot:
            raise HTTPException(404, "No login screenshot available for this task")

        if "url" in screenshot:
            return JSONResponse(
                content={"url": screenshot["url"]},
                headers={"Cache-Control": "no-cache"},
            )

        return Response(
            content=screenshot["data"],
            media_type="image/png",
            headers={"Cache-Control": "no-cache"},
        )

    # --- User Input Endpoint ---

    @app.post("/tasks/{task_id}/input", status_code=201)
    async def submit_user_input(
        task_id: int,
        body: dict,
        queue: TaskQueue = Depends(_get_queue),
    ) -> dict:
        """Submit user input to a running task (e.g., SMS verification code).

        The scraper polls for this input and injects it into the browser.
        Body: {"value": "123456"}
        """
        task = await queue.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")

        value = body.get("value")
        if not value:
            raise HTTPException(422, "Missing 'value' field")

        input_id = await queue.submit_user_input(task_id, str(value))
        return {"id": input_id, "task_id": task_id}

    # --- Info Endpoints ---

    @app.get("/workers", response_model=list[WorkerResponse])
    async def list_workers(
        queue: TaskQueue = Depends(_get_queue),
    ) -> list[WorkerResponse]:
        """List all workers."""
        workers = await queue.list_workers()
        return [WorkerResponse(**w) for w in workers]

    @app.get("/scrapers", response_model=list[ScraperTypeResponse])
    async def list_scrapers(
        reg: ScraperRegistry = Depends(_get_registry),
    ) -> list[ScraperTypeResponse]:
        """List registered scraper types."""
        return [ScraperTypeResponse(**t) for t in reg.list_types()]

    @app.get("/stats", response_model=StatsResponse)
    async def get_stats(
        queue: TaskQueue = Depends(_get_queue),
    ) -> StatsResponse:
        """Get queue statistics."""
        stats = await queue.get_stats()
        return StatsResponse(**stats)

    @app.get("/health", response_model=HealthResponse)
    async def health(
        queue: TaskQueue = Depends(_get_queue),
    ) -> HealthResponse:
        """Health check."""
        try:
            stats = await queue.get_stats()
            return HealthResponse(
                status="healthy",
                database=True,
                workers_active=stats["workers_active"],
                tasks_pending=stats["pending"],
            )
        except Exception:
            return HealthResponse(
                status="degraded",
                database=False,
                workers_active=0,
                tasks_pending=0,
            )

    return app


async def _auto_scale_loop(cfg: AutoScaleSettings, queue: TaskQueue) -> None:
    """Periodically adjust ASG desired capacity based on pending task count.

    - On startup, syncs min/max from config to ASG (so .env is the source of truth)
    - Scale up immediately when pending tasks exceed capacity
    - Scale down with cooldown to prevent flapping
    """
    try:
        import boto3
    except ImportError:
        logger.error("boto3 not installed, auto-scaling disabled")
        return

    asg_client = boto3.client("autoscaling")
    scale_down_counter = 0

    # Sync min/max from config to ASG on startup
    try:
        asg_client.update_auto_scaling_group(
            AutoScalingGroupName=cfg.asg_name,
            MinSize=cfg.min_instances,
            MaxSize=cfg.max_instances,
        )
        logger.info(
            "Auto-scale loop started (asg=%s, min=%d, max=%d, interval=%ds)",
            cfg.asg_name, cfg.min_instances, cfg.max_instances, int(cfg.check_interval),
        )
    except Exception as e:
        logger.warning("Failed to sync ASG min/max: %s", e)

    while True:
        try:
            stats = await queue.get_stats()
            pending = stats.get("pending", 0) + stats.get("claimed", 0)

            resp = asg_client.describe_auto_scaling_groups(
                AutoScalingGroupNames=[cfg.asg_name]
            )
            if not resp.get("AutoScalingGroups"):
                logger.warning("ASG not found: %s", cfg.asg_name)
                await asyncio.sleep(cfg.check_interval)
                continue

            current = resp["AutoScalingGroups"][0]["DesiredCapacity"]

            if pending > 0:
                raw = (pending + cfg.tasks_per_worker - 1) // cfg.tasks_per_worker
            else:
                raw = cfg.min_instances
            desired = max(cfg.min_instances, min(cfg.max_instances, raw))

            if desired > current:
                scale_down_counter = 0
                logger.info("Auto-scale UP: %d → %d (pending=%d)", current, desired, pending)
                asg_client.set_desired_capacity(
                    AutoScalingGroupName=cfg.asg_name,
                    DesiredCapacity=desired,
                )
            elif desired < current:
                scale_down_counter += 1
                if scale_down_counter >= cfg.scale_down_cooldown:
                    logger.info(
                        "Auto-scale DOWN: %d → %d (pending=%d, cooldown reached)",
                        current, desired, pending,
                    )
                    asg_client.set_desired_capacity(
                        AutoScalingGroupName=cfg.asg_name,
                        DesiredCapacity=desired,
                    )
                    scale_down_counter = 0
                else:
                    logger.debug("Auto-scale down pending %d/%d", scale_down_counter, cfg.scale_down_cooldown)
            else:
                scale_down_counter = 0

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Auto-scale error: %s", e)

        await asyncio.sleep(cfg.check_interval)


def _create_default_registry() -> ScraperRegistry:
    """Create registry with built-in scrapers."""
    registry = ScraperRegistry()
    try:
        from resilient_scraper.scrapers.xiaohongshu import XiaohongshuScraper
        registry.register(XiaohongshuScraper)
    except ImportError:
        pass
    try:
        from resilient_scraper.scrapers.planespotters import PlanespottersScraper
        registry.register(PlanespottersScraper)
    except ImportError:
        pass
    try:
        from resilient_scraper.scrapers.ebay import EbayStoreScraper
        registry.register(EbayStoreScraper)
    except ImportError:
        pass
    return registry


def _include_scraper_routers(app: FastAPI, db: Database) -> None:
    """Include scraper-specific data query routers."""
    try:
        from resilient_scraper.scrapers.xiaohongshu.router import create_router as xhs_router
        app.include_router(xhs_router(db), prefix="/xiaohongshu", tags=["xiaohongshu"])
    except ImportError:
        pass
    try:
        from resilient_scraper.scrapers.planespotters.router import create_router as ps_router
        app.include_router(ps_router(db), prefix="/planespotters", tags=["planespotters"])
    except ImportError:
        pass
    try:
        from resilient_scraper.scrapers.ebay.router import create_router as ebay_router
        app.include_router(ebay_router(db), prefix="/ebay", tags=["ebay"])
    except ImportError:
        pass


def main() -> None:
    """CLI entry point for scraper-api."""
    import uvicorn

    settings = ServiceSettings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
