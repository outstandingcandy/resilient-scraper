"""HTML → structured-field extractors.

Extractors are pure functions with no network or DB dependencies, so they
can be reused both during scraping and when re-processing saved HTML.
"""

from resilient_scraper.extractors.base import BaseExtractor

__all__ = ["BaseExtractor"]
