"""
Error classes for resilient scraper.

Provides a hierarchy of scraper-specific exceptions with retryability
information to guide retry logic in the scraper framework.
"""


class ScraperError(Exception):
    """Base exception for scraper errors.

    Attributes:
        task_key: Key of the task that failed.
        retryable: Whether this error is recoverable by retrying.
    """

    def __init__(
        self,
        message: str,
        task_key: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.task_key = task_key
        self.retryable = retryable


class CloudflareBlockedError(ScraperError):
    """Raised when Cloudflare blocks the request."""

    def __init__(self, task_key: str | None = None) -> None:
        super().__init__(
            "Request blocked by Cloudflare",
            task_key=task_key,
            retryable=True,
        )


class PageLoadError(ScraperError):
    """Raised when a page fails to load properly."""

    def __init__(self, url: str, task_key: str | None = None) -> None:
        super().__init__(
            f"Failed to load page: {url}",
            task_key=task_key,
            retryable=True,
        )


class NoDataFoundError(ScraperError):
    """Raised when no data is found on the page."""

    def __init__(self, task_key: str | None = None) -> None:
        super().__init__(
            "No data found on page",
            task_key=task_key,
            retryable=False,
        )


class LoginRequiredError(ScraperError):
    """Raised when login is required to continue scraping."""

    def __init__(
        self,
        task_key: str | None = None,
        screenshot_path: str | None = None,
    ) -> None:
        super().__init__(
            "Login required to continue scraping",
            task_key=task_key,
            retryable=False,
        )
        self.screenshot_path = screenshot_path


class BrowserDisconnectedError(ScraperError):
    """Raised when browser connection is lost during scraping."""

    def __init__(
        self,
        task_key: str | None = None,
        items_extracted: int = 0,
        processed_ids: set[str] | None = None,
    ) -> None:
        super().__init__(
            f"Browser disconnected after extracting {items_extracted} items",
            task_key=task_key,
            retryable=True,
        )
        self.items_extracted = items_extracted
        self.processed_ids = processed_ids or set()
