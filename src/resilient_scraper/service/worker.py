"""Worker process: polls task queue, executes scrapers, reports results.

The Worker is persistence-agnostic — it receives an already-constructed
:class:`resilient_scraper.queue.TaskQueue` implementation from the caller.
Deciding where tasks live (Postgres, SQLite, in-memory, ...) is the calling
application's responsibility.
"""

import asyncio
import functools
import logging
import os
import signal
import subprocess
import time
from typing import TYPE_CHECKING, Any

from resilient_scraper.errors import NoDataFoundError, ScraperError
from resilient_scraper.models import ScraperTask, TaskStatus
from resilient_scraper.queue import TaskQueue
from resilient_scraper.service.config import ServiceSettings
from resilient_scraper.service.feishu import FeishuClient
from resilient_scraper.service.registry import ScraperRegistry

if TYPE_CHECKING:
    # Imported for typing only; `setup()` imports it lazily at runtime so that
    # merely importing Worker doesn't pull in DrissionPage.
    from resilient_scraper.service.browser_pool import BrowserPool

logger = logging.getLogger("resilient_scraper.service.worker")


class Worker:
    """Scraper worker process.

    Polls a caller-supplied TaskQueue for pending tasks, executes them with
    the appropriate scraper, and reports results back. Manages a browser pool
    and sends periodic heartbeats.
    """

    def __init__(
        self,
        settings: ServiceSettings,
        registry: ScraperRegistry,
        queue: TaskQueue,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._queue = queue
        self._worker_id = settings.worker.get_worker_id()
        self._shutdown = asyncio.Event()
        self._current_task_id: int | None = None
        self._browser_pool: BrowserPool | None = None
        self._xvfb_process: subprocess.Popen | None = None
        self._scrapers: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._feishu: FeishuClient | None = None
        # task_id → {"message_id": str, "timestamp": float}
        self._feishu_alerts: dict[int, dict[str, Any]] = {}

    async def run(self) -> None:
        """Main entry point: setup → run loops → teardown."""
        self._setup_signals()
        logging.basicConfig(level=self._settings.log_level)
        logger.info("Worker %s starting", self._worker_id)
        self._loop = asyncio.get_running_loop()

        try:
            await self._setup()
            loops = [self._work_loop(), self._heartbeat_loop()]
            if self._feishu:
                loops.append(self._feishu_poll_loop())
            await asyncio.gather(*loops)
        except asyncio.CancelledError:
            logger.info("Worker cancelled")
        finally:
            await self._teardown()

    async def _setup(self) -> None:
        """Initialize browser, Feishu client, and scrapers.

        The queue is injected via the constructor; this method assumes its
        storage is already set up (the caller is responsible for migrations /
        schema creation before instantiating the Worker).
        """
        use_pool = self._settings.browser.pool

        if use_pool:
            # Self-managed headless Chromium pool
            from resilient_scraper.service.browser_pool import BrowserPool

            self._start_xvfb()
            self._browser_pool = BrowserPool(self._settings.browser)
            self._browser_pool.initialize()
            logger.info("Browser pool mode (size=%d)", self._settings.browser.size)
        else:
            # Connect to external real browser via CDP
            logger.info(
                "External browser mode (cdp://%s:%d)",
                self._settings.browser.chrome_debug_host,
                self._settings.browser.chrome_debug_port,
            )

        # Feishu client
        if self._settings.feishu.enabled:
            self._feishu = FeishuClient(self._settings.feishu)
            logger.info("Feishu notifications enabled (receive_id=%s)", self._settings.feishu.receive_id or "auto")

        # Setup scrapers
        for type_info in self._registry.list_types():
            task_type = type_info["task_type"]
            scraper = self._registry.create(task_type)
            if scraper:
                if self._settings.s3.bucket:
                    scraper.s3_enabled = True
                    scraper.s3_bucket = self._settings.s3.bucket
                    scraper.s3_prefix = self._settings.s3.prefix
                    scraper.delete_local_after_upload = self._settings.s3.delete_local_after_upload
                if self._settings.db.url:
                    sync_url = self._settings.db.url.replace("+asyncpg", "")
                    scraper.database_url = sync_url

                if not use_pool:
                    # Tell scraper to connect to the external browser
                    scraper.use_existing_browser = True
                    scraper.chrome_debug_port = self._settings.browser.chrome_debug_port

                # Set login screenshot callback so QR codes are stored in DB
                scraper.on_login_screenshot = functools.partial(
                    self._sync_store_screenshot, task_type
                )
                # Set user input callback so scraper can poll for SMS codes etc.
                scraper.on_poll_user_input = self._sync_poll_user_input
                # Set login success callback to restore task status to processing
                scraper.on_login_success = self._sync_login_success
                # Set page screenshot callback to store debug screenshots in DB
                scraper.on_page_screenshot = self._sync_store_page_screenshot
                # Set alert callback for Feishu notifications
                if self._feishu:
                    scraper.on_send_alert = functools.partial(
                        self._sync_send_feishu_alert, task_type
                    )
                scraper.setup()
                self._scrapers[task_type] = scraper
                logger.info("Scraper %s ready", task_type)

        # Register worker
        await self._queue.register_worker(
            self._worker_id,
            metadata={
                "scrapers": self._registry.task_types,
                "browser_mode": "pool" if use_pool else "external",
                "pid": os.getpid(),
            },
        )
        logger.info("Worker %s setup complete", self._worker_id)

    async def _teardown(self) -> None:
        """Clean up resources."""
        logger.info("Worker %s shutting down", self._worker_id)

        # Teardown scrapers
        for scraper in self._scrapers.values():
            try:
                scraper.teardown()
            except Exception as e:
                logger.error("Scraper teardown error: %s", e)

        # Deactivate worker
        if self._queue:
            try:
                await self._queue.deactivate_worker(self._worker_id)
            except Exception:
                pass

        # Shutdown browser pool
        if self._browser_pool:
            self._browser_pool.shutdown()

        # Stop Xvfb
        if self._xvfb_process:
            self._xvfb_process.terminate()

        # The queue's lifecycle (engine disposal, etc.) is owned by the caller.

        logger.info("Worker %s stopped", self._worker_id)

    async def _work_loop(self) -> None:
        """Main work loop: claim and process tasks."""
        poll_interval = self._settings.worker.poll_interval
        task_types = self._registry.task_types

        while not self._shutdown.is_set():
            try:
                task_dict = await self._queue.claim_task(
                    worker_id=self._worker_id,
                    task_types=task_types,
                    stale_minutes=self._settings.worker.stale_task_minutes,
                )

                if not task_dict:
                    await asyncio.sleep(poll_interval)
                    continue

                await self._process_task(task_dict)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Work loop error: %s", e, exc_info=True)
                await asyncio.sleep(poll_interval)

    def _run_async(
        self,
        coro: Any,
        timeout: float = 10.0,
        label: str = "callback",
    ) -> Any:
        """Run an async coroutine from a sync thread via the main event loop.

        Bridge for sync callbacks running inside asyncio.to_thread that need
        to call async methods on the event loop.

        Returns:
            Coroutine result, or None if the loop is unavailable or the call fails.
        """
        if not self._loop:
            logger.warning("Event loop not available for %s", label)
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result(timeout=timeout)
        except Exception as e:
            logger.warning("%s failed: %s", label, e)
            return None

    def _sync_store_screenshot(
        self, task_type: str, task_id: int, screenshot_data: bytes, phase: str = "qr_scan"
    ) -> None:
        """Synchronous callback for storing login screenshots from scraper thread."""
        self._run_async(
            self._queue.set_login_required(task_id, screenshot_data, phase=phase),
            label=f"store_screenshot(task={task_id})",
        )

    def _sync_store_page_screenshot(self, task_id: int, screenshot_url: str) -> None:
        """Synchronous callback for storing page screenshot URL from scraper thread."""
        self._run_async(
            self._queue.update_login_screenshot(task_id, screenshot_url),
            label=f"store_page_screenshot(task={task_id})",
        )

    def _sync_login_success(self, task_id: int) -> None:
        """Synchronous callback to restore task status after successful login."""
        self._run_async(
            self._queue.clear_login_screenshot(task_id),
            timeout=5.0,
            label=f"clear_login(task={task_id})",
        )
        self._run_async(
            self._queue.update_status(task_id, "processing"),
            label=f"restore_status(task={task_id})",
        )
        logger.info("Task %d: login successful, status restored to processing", task_id)

    def _sync_poll_user_input(self, task_id: int) -> str | None:
        """Synchronous callback for polling user input from scraper thread."""
        return self._run_async(
            self._queue.consume_user_input(task_id),
            timeout=5.0,
            label=f"poll_input(task={task_id})",
        )

    def _sync_send_feishu_alert(
        self,
        task_type: str,
        task_id: int,
        context_key: str,
        screenshot_bytes: bytes | None,
        phase: str = "qr_scan",
    ) -> None:
        """Synchronous callback for sending Feishu login alerts from scraper thread."""
        if not self._feishu:
            return
        try:
            image_key = ""
            if screenshot_bytes:
                image_key = self._feishu.upload_image(screenshot_bytes)

            if not image_key:
                logger.warning("Task %d: no image to send via Feishu", task_id)
                return

            scraper = self._scrapers.get(task_type)
            platform = (scraper.platform_display_name if scraper else "") or task_type

            message_id = self._feishu.send_login_alert(
                image_key=image_key,
                platform=platform,
                context_key=context_key,
                phase=phase,
                task_id=task_id,
            )
            if message_id:
                self._feishu_alerts[task_id] = {
                    "message_id": message_id,
                    "timestamp": time.time(),
                }
        except Exception as e:
            logger.error("Feishu alert failed for task %d: %s", task_id, e)

    async def _process_task(self, task_dict: dict[str, Any]) -> None:
        """Process a single task."""
        task_id = task_dict["id"]
        task_type = task_dict["task_type"]
        self._current_task_id = task_id

        scraper = self._scrapers.get(task_type)
        if not scraper:
            logger.error("No scraper for type: %s", task_type)
            await self._queue.fail_task(task_id, f"Unknown type: {task_type}", self._worker_id, 0, retry=False)
            self._current_task_id = None
            return

        # Set current task id on scraper so screenshot callback knows which task
        scraper._current_task_id = task_id

        # Build ScraperTask from dict
        task = ScraperTask(
            task_type=task_dict["task_type"],
            task_key=task_dict["task_key"],
            id=task_dict["id"],
            status=TaskStatus(task_dict["status"]),
            priority=task_dict["priority"],
            payload=task_dict.get("payload") or {},
            attempts=task_dict["attempts"],
            max_attempts=task_dict["max_attempts"],
        )

        logger.info("Processing task %d: %s/%s (attempt %d)", task_id, task_type, task.task_key, task.attempts)

        browser = None
        start_time = time.time()

        try:
            # Update status to processing
            await self._queue.update_status(task_id, "processing")

            # Acquire browser from pool (only in pool mode; in external mode
            # the scraper connects to the real browser itself via CDP)
            if scraper.requires_browser and self._browser_pool:
                browser = self._browser_pool.acquire(timeout=60)

            # Execute scrape in thread pool (DrissionPage is synchronous)
            # Use per-scraper timeout if set, otherwise fall back to worker default
            timeout = scraper.task_timeout or self._settings.worker.task_timeout
            result = await asyncio.wait_for(
                asyncio.to_thread(scraper.scrape, task, browser),
                timeout=timeout,
            )

            duration = time.time() - start_time

            # Handle login_required result
            if hasattr(result, "login_required") and result.login_required:
                logger.warning("Task %d: login required, waiting for user scan", task_id)
                # Status already set to login_required by the screenshot callback.
                # Clean up screenshot after login succeeds or task fails on retry.
                await self._queue.fail_task(
                    task_id, "Login required — scan QR code via GET /tasks/{id}/screenshot",
                    self._worker_id, duration, retry=True,
                )
                return

            # Build summary for task result (not full data)
            summary = {
                "success": result.success,
                "task_key": result.task_key,
                "task_type": result.task_type,
                "duration_seconds": duration,
            }
            # Add scraper-specific summary fields
            for field_name in ("notes_count", "images_downloaded", "account_id",
                               "aircraft_count", "pages_scraped", "records_updated",
                               "family_name", "scrape_mode"):
                if hasattr(result, field_name):
                    summary[field_name] = getattr(result, field_name)

            # Clean up any leftover login screenshots
            await self._queue.clear_login_screenshot(task_id)

            await self._queue.complete_task(task_id, summary, self._worker_id, duration)
            await self._queue.increment_worker_completed(self._worker_id)

            # Call scraper's on_success hook
            try:
                await asyncio.to_thread(scraper.on_success, task, result)
            except Exception as e:
                logger.warning("on_success hook error: %s", e)

            logger.info("Task %d completed in %.1fs", task_id, duration)

        except NoDataFoundError as e:
            duration = time.time() - start_time
            await self._queue.complete_task_no_data(task_id, str(e), self._worker_id, duration)
            await self._queue.increment_worker_completed(self._worker_id)
            logger.info("Task %d: no data found (%.1fs)", task_id, duration)

        except ScraperError as e:
            duration = time.time() - start_time
            await self._queue.fail_task(task_id, str(e), self._worker_id, duration, retry=e.retryable)
            logger.warning("Task %d failed: %s (retryable=%s)", task_id, e, e.retryable)

        except asyncio.TimeoutError:
            duration = time.time() - start_time
            await self._queue.fail_task(task_id, "Task timed out", self._worker_id, duration, retry=True)
            logger.warning("Task %d timed out after %.1fs", task_id, duration)

        except Exception as e:
            duration = time.time() - start_time
            # Clear cached browser on page disconnect so next retry reconnects
            if "PageDisconnectedError" in type(e).__name__:
                if scraper and hasattr(scraper, "_external_browser"):
                    scraper._external_browser = None
                    logger.info("Cleared stale browser connection for next retry")
            await self._queue.fail_task(task_id, str(e), self._worker_id, duration, retry=True)
            logger.error("Task %d unexpected error: %s", task_id, e, exc_info=True)

        finally:
            if browser and self._browser_pool:
                self._browser_pool.release(browser)
            self._current_task_id = None
            self._feishu_alerts.pop(task_id, None)
            if scraper:
                scraper._current_task_id = None

            # Delay between tasks
            if scraper:
                try:
                    await asyncio.to_thread(scraper.wait_delay)
                except Exception:
                    pass

    async def _feishu_poll_loop(self) -> None:
        """Poll Feishu message replies for verification codes and submit them to the task queue."""
        poll_interval = self._settings.feishu.poll_interval
        logger.info("Feishu poll loop started (interval=%.1fs)", poll_interval)

        while not self._shutdown.is_set():
            try:
                # Snapshot keys to avoid dict-changed-during-iteration
                alert_items = list(self._feishu_alerts.items())
                for task_id, state in alert_items:
                    # Check if task is still in a login-waiting state
                    task = await self._queue.get_task(task_id)
                    if not task or task["status"] not in (
                        "login_required", "processing", "claimed",
                    ):
                        self._feishu_alerts.pop(task_id, None)
                        continue

                    code = self._feishu.get_replies(
                        state["message_id"], state["timestamp"],
                    )
                    if code:
                        await self._queue.submit_user_input(task_id, code)
                        self._feishu.send_text(
                            f"✅ 验证码 {code} 已提交 (task #{task_id})",
                        )
                        # Update timestamp so we don't re-process this reply
                        state["timestamp"] = time.time()
                        logger.info(
                            "Feishu: submitted code %s for task %d", code, task_id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Feishu poll error: %s", e)

            await asyncio.sleep(poll_interval)

    async def _heartbeat_loop(self) -> None:
        """Periodically update worker and task heartbeat."""
        interval = self._settings.worker.heartbeat_interval
        while not self._shutdown.is_set():
            try:
                await self._queue.update_worker_heartbeat(
                    self._worker_id,
                    current_task_id=self._current_task_id,
                )
                if self._current_task_id:
                    await self._queue.update_heartbeat(self._current_task_id)
            except Exception as e:
                logger.warning("Heartbeat error: %s", e)
            await asyncio.sleep(interval)

    def _start_xvfb(self) -> None:
        """Start Xvfb if DISPLAY is not set."""
        if os.environ.get("DISPLAY"):
            return
        try:
            display = ":99"
            self._xvfb_process = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1920x1080x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = display
            import time
            time.sleep(0.5)
            logger.info("Xvfb started on %s", display)
        except FileNotFoundError:
            logger.warning("Xvfb not found, skipping")

    def _setup_signals(self) -> None:
        """Setup graceful shutdown signal handlers."""
        def _signal_handler(sig: int, frame: Any) -> None:
            logger.info("Received signal %d, shutting down", sig)
            self._shutdown.set()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)


def main() -> None:
    """CLI entry point for scraper-worker."""
    from resilient_scraper.scrapers.xiaohongshu import XiaohongshuScraper
    # from resilient_scraper.scrapers.planespotters import PlanespottersScraper
    from resilient_scraper.scrapers.ebay import EbayStoreScraper

    settings = ServiceSettings()
    registry = ScraperRegistry()

    # Register built-in scrapers
    registry.register(XiaohongshuScraper)
    # registry.register(PlanespottersScraper)
    registry.register(EbayStoreScraper)

    worker = Worker(settings, registry)
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
