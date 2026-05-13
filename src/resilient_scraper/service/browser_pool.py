"""Thread-safe browser pool for DrissionPage Chromium instances."""

import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from resilient_scraper.service.config import BrowserSettings

logger = logging.getLogger("resilient_scraper.service.browser_pool")


@dataclass
class BrowserInstance:
    """Tracked browser instance."""

    browser: Any       # Chromium manager (for quit/lifecycle)
    page: Any          # ChromiumPage tab (for scraper: .get(), .ele(), etc.)
    id: int
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    in_use: bool = False
    tasks_processed: int = 0
    healthy: bool = True


class BrowserPool:
    """Thread-safe pool of DrissionPage browser instances.

    Features:
    - Pre-allocated pool with configurable size
    - Health checking for crashed browsers
    - Auto-recycling after max_tasks_per_browser
    """

    def __init__(self, settings: BrowserSettings) -> None:
        self._size = settings.size
        self._max_tasks = settings.max_tasks_per_browser
        self._headless = settings.headless
        self._instances: list[BrowserInstance] = []
        self._lock = threading.Lock()
        self._available = threading.Semaphore(0)
        self._browser_path = self._find_browser_path()

    def initialize(self) -> None:
        """Create all browser instances in the pool."""
        logger.info("Initializing browser pool (size=%d, headless=%s)", self._size, self._headless)
        for i in range(self._size):
            try:
                browser, page = self._create_browser(i)
                instance = BrowserInstance(browser=browser, page=page, id=i)
                self._instances.append(instance)
                self._available.release()
                logger.info("Browser %d created", i)
            except Exception as e:
                logger.error("Failed to create browser %d: %s", i, e)

    def acquire(self, timeout: float = 60) -> Any:
        """Acquire a browser from the pool. Blocks until one is available."""
        if not self._available.acquire(timeout=timeout):
            raise TimeoutError(f"No browser available within {timeout}s")

        with self._lock:
            for instance in self._instances:
                if not instance.in_use and instance.healthy:
                    # Check if browser needs recycling
                    if instance.tasks_processed >= self._max_tasks:
                        self._recycle_instance(instance)

                    instance.in_use = True
                    instance.last_used_at = time.time()
                    return instance.page

        # Should not reach here, but release semaphore to avoid deadlock
        self._available.release()
        raise RuntimeError("No healthy browser found in pool")

    def release(self, page: Any) -> None:
        """Release a browser page back to the pool."""
        with self._lock:
            for instance in self._instances:
                if instance.page is page:
                    instance.in_use = False
                    instance.tasks_processed += 1
                    instance.last_used_at = time.time()
                    self._available.release()
                    return
        logger.warning("Released unknown browser instance")

    def shutdown(self) -> None:
        """Close all browsers in the pool."""
        logger.info("Shutting down browser pool")
        with self._lock:
            for instance in self._instances:
                try:
                    instance.browser.quit()
                except Exception:
                    pass
            self._instances.clear()

    def _create_browser(self, browser_id: int) -> tuple[Any, Any]:
        """Create a new DrissionPage Chromium browser.

        Returns:
            Tuple of (Chromium manager, ChromiumPage tab).
            The Chromium object manages the browser lifecycle (quit).
            The ChromiumPage tab is what scrapers use (.get(), .ele(), etc.).
        """
        from DrissionPage import ChromiumOptions, Chromium

        options = ChromiumOptions()
        if self._browser_path:
            options.set_browser_path(self._browser_path)

        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        options.set_argument("--disable-gpu")
        options.set_argument("--disable-extensions")

        if self._headless:
            options.headless()

        # Use unique port per browser to avoid conflicts
        options.set_local_port(9222 + browser_id)

        browser = Chromium(options)
        page = browser.latest_tab
        return browser, page

    def _recycle_instance(self, instance: BrowserInstance) -> None:
        """Close and recreate a browser instance."""
        logger.info("Recycling browser %d (processed %d tasks)", instance.id, instance.tasks_processed)
        try:
            instance.browser.quit()
        except Exception:
            pass
        try:
            instance.browser, instance.page = self._create_browser(instance.id)
            instance.tasks_processed = 0
            instance.created_at = time.time()
            instance.healthy = True
        except Exception as e:
            logger.error("Failed to recycle browser %d: %s", instance.id, e)
            instance.healthy = False

    @staticmethod
    def _find_browser_path() -> str | None:
        """Find Chromium/Chrome binary path."""
        for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
            path = shutil.which(name)
            if path:
                return path
        return None
