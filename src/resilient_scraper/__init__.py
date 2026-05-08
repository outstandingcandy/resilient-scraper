"""
resilient-scraper — Browser-based web scraper with anti-detection capabilities.

Provides ResilientScraper, an abstract base class for scraping pages protected
by login walls, Cloudflare challenges, cookie consent dialogs, and more.
"""

from resilient_scraper.errors import (
    BrowserDisconnectedError,
    CloudflareBlockedError,
    LoginRequiredError,
    NoDataFoundError,
    PageLoadError,
    ScraperError,
)
from resilient_scraper.models import ScraperResult, ScraperTask, TaskStatus
from resilient_scraper.scraper import ResilientScraper

__all__ = [
    "ResilientScraper",
    "ScraperError",
    "CloudflareBlockedError",
    "PageLoadError",
    "NoDataFoundError",
    "LoginRequiredError",
    "BrowserDisconnectedError",
    "ScraperTask",
    "ScraperResult",
    "TaskStatus",
]
