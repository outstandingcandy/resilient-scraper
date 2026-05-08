"""Basic tests for resilient_scraper package."""

from resilient_scraper import (
    ResilientScraper,
    ScraperError,
    LoginRequiredError,
    BrowserDisconnectedError,
    ScraperTask,
    ScraperResult,
    TaskStatus,
)


def test_imports():
    """Verify all public exports are importable."""
    assert ResilientScraper is not None
    assert ScraperError is not None
    assert LoginRequiredError is not None
    assert BrowserDisconnectedError is not None
    assert ScraperTask is not None
    assert ScraperResult is not None
    assert TaskStatus is not None


def test_cannot_instantiate_abstract():
    """ResilientScraper cannot be instantiated directly."""
    try:
        ResilientScraper()
        assert False, "Should have raised TypeError"
    except TypeError:
        pass


def test_task_status_values():
    """TaskStatus enum has expected values."""
    assert TaskStatus.PENDING == "pending"
    assert TaskStatus.COMPLETED == "completed"
    assert TaskStatus.FAILED == "failed"


def test_scraper_error_attributes():
    """ScraperError carries task_key and retryable flag."""
    err = ScraperError("test error", task_key="key1", retryable=False)
    assert str(err) == "test error"
    assert err.task_key == "key1"
    assert err.retryable is False


def test_login_required_error():
    """LoginRequiredError has screenshot_path attribute."""
    err = LoginRequiredError(task_key="k", screenshot_path="/tmp/x.png")
    assert err.screenshot_path == "/tmp/x.png"
    assert err.retryable is False


def test_browser_disconnected_error():
    """BrowserDisconnectedError tracks extracted items."""
    err = BrowserDisconnectedError(
        task_key="k", items_extracted=5, processed_ids={"a", "b"}
    )
    assert err.items_extracted == 5
    assert err.processed_ids == {"a", "b"}
    assert err.retryable is True


def test_scraper_result_defaults():
    """ScraperResult has sensible defaults."""
    result = ScraperResult(success=False, task_key="k1", task_type="test")
    assert result.success is False
    assert result.error is None
    assert result.retry_scheduled is False


def test_scraper_task_defaults():
    """ScraperTask has sensible defaults."""
    task = ScraperTask(task_type="test", task_key="k1")
    assert task.task_type == "test"
    assert task.status == TaskStatus.PENDING
    assert task.max_attempts == 3
