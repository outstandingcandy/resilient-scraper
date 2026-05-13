"""
Base extractor abstract class for field extraction from HTML.

Extractors are pure functions that parse HTML content without network dependencies,
enabling re-extraction from saved HTML files.
"""

from abc import ABC, abstractmethod
from typing import Any


class BaseExtractor(ABC):
    """Abstract base class for HTML field extractors.

    Extractors are responsible for parsing HTML content and extracting structured
    fields. They are designed to be pure functions with no network dependencies,
    allowing them to be used for:

    1. Real-time extraction during scraping
    2. Re-extraction from saved HTML files (e.g., from S3)

    Subclasses must implement:
        - extract(): Main extraction logic
        - version: Property returning the extractor version string

    The version property is used to track changes in extraction logic,
    allowing detection of when fields may need re-extraction.

    Example:
        ```python
        class MyExtractor(BaseExtractor):
            version = "1.0.0"

            def extract(self, html: str, context: dict = {}) -> dict:
                return {
                    "field1": self._extract_field1(html),
                    "field2": self._extract_field2(html),
                }
        ```
    """

    @property
    @abstractmethod
    def version(self) -> str:
        """Extractor version string.

        Use semantic versioning (e.g., "1.0.0"):
        - Major: Breaking changes in output structure
        - Minor: New fields added
        - Patch: Bug fixes in extraction logic

        Returns:
            Version string in semantic versioning format.
        """
        ...

    @abstractmethod
    def extract(self, html: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Extract fields from HTML content.

        This method should be a pure function that only depends on the HTML
        content and optional context, with no network or I/O operations.

        Args:
            html: Raw HTML content to parse.
            context: Optional context dictionary with additional information
                     that may be needed for extraction (e.g., URL, registration).
                     Keys and values are extractor-specific.

        Returns:
            Dictionary of extracted fields. Field names and types are
            extractor-specific. Fields with no data should be set to None.

        Raises:
            ValueError: If HTML content is invalid or cannot be parsed.
        """
        ...

    def extract_safe(
        self, html: str, context: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], list[str]]:
        """Extract fields with error handling.

        Wraps extract() to catch exceptions and return partial results
        along with a list of errors encountered.

        Args:
            html: Raw HTML content to parse.
            context: Optional context dictionary.

        Returns:
            Tuple of (extracted_fields, error_messages).
            extracted_fields may be partial if errors occurred.
        """
        errors: list[str] = []
        try:
            result = self.extract(html, context)
            return result, errors
        except ValueError as e:
            errors.append(f"ValueError: {e}")
            return {}, errors
        except Exception as e:
            errors.append(f"Unexpected error: {type(e).__name__}: {e}")
            return {}, errors

    def get_version_info(self) -> dict[str, str]:
        """Get version information about this extractor.

        Returns:
            Dictionary with version information.
        """
        return {
            "extractor": self.__class__.__name__,
            "version": self.version,
        }
