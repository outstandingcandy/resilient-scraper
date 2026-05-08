"""Scraper type registry — maps task_type strings to scraper classes."""

import logging
from typing import Any

from resilient_scraper.scraper import ResilientScraper

logger = logging.getLogger("resilient_scraper.service.registry")


class ScraperRegistry:
    """Registry for scraper types.

    Both the API (for validation) and worker (for execution)
    share the same registry.
    """

    def __init__(self) -> None:
        self._scrapers: dict[str, tuple[type[ResilientScraper], dict[str, Any]]] = {}

    def register(
        self,
        scraper_class: type[ResilientScraper],
        config: dict[str, Any] | None = None,
    ) -> None:
        """Register a scraper class with optional config."""
        task_type = scraper_class.task_type
        self._scrapers[task_type] = (scraper_class, config or {})
        logger.info("Registered scraper: %s (%s)", task_type, scraper_class.__name__)

    def create(self, task_type: str) -> ResilientScraper | None:
        """Create a new scraper instance for the given task type."""
        entry = self._scrapers.get(task_type)
        if not entry:
            return None
        scraper_class, config = entry
        return scraper_class(config=config)

    def has(self, task_type: str) -> bool:
        """Check if a task type is registered."""
        return task_type in self._scrapers

    def list_types(self) -> list[dict[str, Any]]:
        """List all registered scraper types with metadata."""
        result = []
        for task_type, (scraper_class, _) in self._scrapers.items():
            result.append({
                "task_type": task_type,
                "requires_browser": getattr(scraper_class, "requires_browser", True),
                "description": (scraper_class.__doc__ or "").split("\n")[0].strip(),
            })
        return result

    @property
    def task_types(self) -> list[str]:
        """Return list of registered task type names."""
        return list(self._scrapers.keys())
