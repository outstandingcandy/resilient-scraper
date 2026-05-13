"""
Airport-data.com scraper implementation.

Scrapes aircraft data from airport-data.com with support for:
- Manufacturer index scraping (A-Z + 09 for numeric names)
- Manufacturer page pagination (aircraft list)
- Individual aircraft detail scraping
- Raw HTML storage to S3

DB persistence and task-queue enqueueing for derived follow-up tasks are
delegated to the calling application via injected callables — see
``add_task_callback`` and ``record_sink_callback`` config keys.
"""

import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from resilient_scraper.errors import NoDataFoundError, ScraperError
from resilient_scraper.models import ScraperTask
from resilient_scraper.scraper import ResilientScraper
from resilient_scraper.scrapers.aviation.airport_data.extractor import AirportDataExtractor
from resilient_scraper.scrapers.aviation.airport_data.models import (
    AirportDataAircraftData,
    AirportDataResult,
)

logger = logging.getLogger("resilient_scraper.scrapers.airport_data")


class AirportDataScraper(ResilientScraper[AirportDataResult]):
    """Scraper for aircraft data from airport-data.com.

    Supports three scrape modes:
    - "manufacturers": Scrape index pages (09, A-Z) to get all manufacturer URLs
    - "manufacturer": Scrape paginated manufacturer page for aircraft list
    - "aircraft": Scrape individual aircraft detail page

    Configuration options (in scraper config):
        screenshots_dir: Directory for screenshots (default: "data/airport_data_screenshots")
        s3_upload: Whether to upload HTML to S3 (default: False)
        s3_bucket: S3 bucket name (required if s3_upload is True)
        s3_prefix: S3 key prefix (default: "data/airport_data_raw")
        max_pages_per_manufacturer: Maximum pages to scrape per manufacturer (default: 500)
        skip_existing: Skip registrations already on file (hint passed to
            ``existing_registrations_callback``, default: True)
    """

    task_type: str = "airport_data"

    # Airport-data.com is not Cloudflare protected
    cloudflare_protected: bool = False
    default_delay: tuple[float, float] = (3.0, 6.0)
    requires_browser: bool = True

    BASE_URL = "https://www.airport-data.com"

    # Index letters: 09 for numeric names, then A-Z
    INDEX_LETTERS = ["09"] + [chr(i) for i in range(ord("A"), ord("Z") + 1)]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the AirportData scraper.

        Args:
            config: Configuration dictionary. Recognised keys (in addition to
                the base class options):

                add_task_callback: Callable invoked when this scraper needs
                    to enqueue follow-up tasks (manufacturer pages,
                    per-aircraft detail fetches). Signature::

                        fn(list[dict]) -> int

                    where each dict has keys ``task_type`` / ``task_key`` /
                    ``payload`` / ``priority``. Returning the number of tasks
                    actually added is optional. If not provided, follow-up
                    enqueue calls are logged and silently dropped.
                existing_registrations_callback: Callable returning a
                    ``set[str]`` of registrations already on file, used to
                    suppress duplicate aircraft-detail enqueue. Optional.
        """
        super().__init__(config)

        # Configuration
        self.screenshots_dir = self.config.get("screenshots_dir", "data/airport_data_screenshots")
        self.s3_upload = self.config.get("s3_upload", False)
        self.s3_bucket = self.config.get("s3_bucket", "") or ""
        # Auto-disable S3 if bucket is empty or still a `${VAR}` placeholder.
        if "${" in self.s3_bucket or not self.s3_bucket.strip():
            self.s3_upload = False
            self.s3_bucket = ""
        self.s3_prefix = self.config.get("s3_prefix", "data/airport_data_raw")
        self.max_pages = self.config.get("max_pages_per_manufacturer", 500)
        self.skip_existing = self.config.get("skip_existing", True)

        # Align parent-class attributes with subclass-specific config so that
        # ``ResilientScraper.setup()`` initialises the S3 client automatically.
        self.s3_enabled = self.s3_upload

        # Optional callables injected by the calling application.
        self._add_task_callback = self.config.get("add_task_callback")
        self._existing_registrations_callback = self.config.get(
            "existing_registrations_callback"
        )
        # Callable persisting a batch of AirportDataAircraftData to the
        # application's DB. Signature: fn(list[AirportDataAircraftData]) -> int.
        # Returns the number of records actually written.
        self._persist_aircraft_callback = self.config.get("persist_aircraft_callback")

        # Initialize extractor for field extraction
        self.extractor = AirportDataExtractor()

        # Ensure directories exist
        os.makedirs(self.screenshots_dir, exist_ok=True)
        os.makedirs(f"{self.screenshots_dir}/html", exist_ok=True)
        os.makedirs(f"{self.screenshots_dir}/screenshots", exist_ok=True)

    def _ensure_s3_client(self) -> Any:
        """Lazy S3 client init for the subclass-specific ``_save_html`` path.

        We need an S3 client even when ``setup()`` hasn't been called (e.g. in
        ad-hoc scripts that exercise the scraper directly). Parent class
        populates ``self.s3_client`` via ``setup()``; here we top-up on first
        use so the subclass works standalone too.
        """
        if self.s3_client is None and self.s3_upload and self.s3_bucket:
            self.s3_client = boto3.client("s3")
        return self.s3_client

    def _add_manufacturer_tasks(self, manufacturers: list[str]) -> int:
        """Enqueue manufacturer-scraping follow-ups via the injected callback.

        Args:
            manufacturers: List of manufacturer names to scrape.

        Returns:
            Number of tasks added (0 if no callback was injected).
        """
        if not manufacturers or self._add_task_callback is None:
            if manufacturers and self._add_task_callback is None:
                logger.debug(
                    f"No add_task_callback injected; dropping {len(manufacturers)} "
                    "manufacturer follow-ups"
                )
            return 0

        tasks = [
            {
                "task_type": self.task_type,
                "task_key": manufacturer,
                "payload": {},
                "priority": 0,
            }
            for manufacturer in manufacturers
        ]

        added = self._add_task_callback(tasks) or 0
        logger.info(f"Added {added} manufacturer tasks to queue")
        return added

    def _add_aircraft_detail_tasks(self, aircraft_list: list[AirportDataAircraftData]) -> int:
        """Enqueue aircraft-detail follow-ups via the injected callback."""
        if not aircraft_list or self._add_task_callback is None:
            if aircraft_list and self._add_task_callback is None:
                logger.debug(
                    f"No add_task_callback injected; dropping {len(aircraft_list)} "
                    "aircraft-detail follow-ups"
                )
            return 0

        tasks = [
            {
                "task_type": self.task_type,
                "task_key": f"aircraft:{aircraft.registration}",
                "payload": {},
                "priority": -1,  # Lower priority than manufacturer tasks
            }
            for aircraft in aircraft_list
            if aircraft.registration
        ]

        added = self._add_task_callback(tasks) or 0
        logger.info(f"Added {added} aircraft detail tasks to queue")
        return added

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that a task can be processed.

        Args:
            task: The task to validate.

        Returns:
            True if valid, False otherwise.
        """
        if task.task_type != self.task_type:
            return False

        task_key = task.task_key
        if not task_key:
            return False

        # Valid task keys:
        # - "index" or single letters (09, A-Z) for manufacturers mode
        # - manufacturer name for manufacturer mode
        # - "aircraft:REGISTRATION" for aircraft mode
        return True

    def scrape(self, task: ScraperTask, browser: Any | None = None) -> AirportDataResult:
        """Execute the scraping operation.

        Args:
            task: The task to process.
            browser: Browser instance (DrissionPage).

        Returns:
            AirportDataResult with extracted data.
        """
        if browser is None:
            raise ScraperError("Browser instance required", task.task_key)

        task_key = task.task_key
        time.time()

        # Determine scrape mode from task key
        if task_key == "index":
            # Scrape all index pages
            return self._scrape_all_index_pages(browser, task)
        elif task_key in self.INDEX_LETTERS:
            # Scrape single index page
            return self._scrape_index_page(browser, task_key, task)
        elif task_key.startswith("aircraft:"):
            # Scrape individual aircraft
            registration = task_key.replace("aircraft:", "")
            return self._scrape_aircraft_detail(browser, registration, task)
        else:
            # Assume it's a manufacturer name
            return self._scrape_manufacturer(browser, task_key, task)

    def _scrape_all_index_pages(self, browser: Any, task: ScraperTask) -> AirportDataResult:
        """Scrape all manufacturer index pages (09, A-Z).

        Args:
            browser: Browser instance.
            task: The task being processed.

        Returns:
            AirportDataResult with all manufacturer URLs.
        """
        all_manufacturers: list[str] = []
        s3_paths: list[str] = []

        for letter in self.INDEX_LETTERS:
            try:
                result = self._scrape_index_page(browser, letter, task)
                all_manufacturers.extend(result.manufacturer_urls)
                s3_paths.extend(result.s3_paths)
                self.wait_delay()
            except Exception as e:
                logger.error(f"Failed to scrape index {letter}: {e}")
                continue

        # Add manufacturer tasks to queue (tier 1 -> tier 2)
        tasks_added = self._add_manufacturer_tasks(all_manufacturers)

        return AirportDataResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            scrape_mode="manufacturers",
            manufacturer_urls=all_manufacturers,
            aircraft_count=0,
            pages_scraped=len(self.INDEX_LETTERS),
            records_updated=tasks_added,
            s3_paths=s3_paths,
        )

    def _scrape_index_page(self, browser: Any, letter: str, task: ScraperTask) -> AirportDataResult:
        """Scrape a single manufacturer index page.

        Args:
            browser: Browser instance.
            letter: Index letter (09, A, B, ..., Z).
            task: The task being processed.

        Returns:
            AirportDataResult with manufacturer URLs.
        """
        url = f"{self.BASE_URL}/manuf/{letter}.html"
        logger.info(f"Scraping index page: {url}")

        browser.get(url)
        time.sleep(2)  # Wait for page load

        # Save screenshot and HTML
        self._save_screenshot(browser, f"index_{letter}")
        html = browser.html
        s3_path = self._save_html(html, f"manufacturers/index_{letter}")

        # Extract manufacturer links
        manufacturers: list[str] = []
        try:
            # Look for links to manufacturer pages in the HTML
            # Pattern: /manuf/ManufacturerName.html
            links = browser.eles("tag:a")
            for link in links:
                href = link.attr("href") or ""
                if "/manuf/" in href and href != f"/manuf/{letter}.html":
                    # Extract manufacturer name from URL
                    match = re.search(r"/manuf/([^.]+)\.html", href)
                    if match:
                        manuf_name = match.group(1)
                        if manuf_name not in self.INDEX_LETTERS:
                            manufacturers.append(manuf_name)
        except Exception as e:
            logger.error(f"Error extracting manufacturers from {url}: {e}")

        # Remove duplicates while preserving order
        seen = set()
        unique_manufacturers = []
        for m in manufacturers:
            if m not in seen:
                seen.add(m)
                unique_manufacturers.append(m)

        logger.info(f"Found {len(unique_manufacturers)} manufacturers in index {letter}")

        # Add manufacturer tasks to queue (tier 1 -> tier 2)
        tasks_added = self._add_manufacturer_tasks(unique_manufacturers)

        return AirportDataResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            scrape_mode="manufacturers",
            manufacturer_urls=unique_manufacturers,
            aircraft_count=0,
            pages_scraped=1,
            records_updated=tasks_added,
            s3_paths=[s3_path] if s3_path else [],
        )

    def _scrape_manufacturer(
        self, browser: Any, manufacturer: str, task: ScraperTask
    ) -> AirportDataResult:
        """Scrape paginated manufacturer page for aircraft list.

        Args:
            browser: Browser instance.
            manufacturer: Manufacturer name (URL-encoded format).
            task: The task being processed.

        Returns:
            AirportDataResult with aircraft data.
        """
        all_aircraft: list[AirportDataAircraftData] = []
        s3_paths: list[str] = []
        page = task.payload.get("start_page", 1)
        records_updated = 0

        logger.info(f"Scraping manufacturer: {manufacturer} starting at page {page}")

        while page <= self.max_pages:
            # Build URL with pagination
            if page == 1:
                url = f"{self.BASE_URL}/manuf/{manufacturer}.html"
            else:
                url = f"{self.BASE_URL}/manuf/{manufacturer}.html?p={page}"

            logger.info(f"Scraping manufacturer page: {url}")
            browser.get(url)
            time.sleep(2)  # Wait for page load

            # Save screenshot and HTML
            self._save_screenshot(browser, f"{manufacturer}_page{page}")
            html = browser.html
            s3_path = self._save_html(html, f"manufacturer_lists/{manufacturer}_page{page}")
            if s3_path:
                s3_paths.append(s3_path)

            # Check if page has any data
            if "No matching data available" in html or "no data" in html.lower():
                logger.info(f"No more data for {manufacturer} at page {page}")
                break

            # Parse aircraft table
            aircraft_on_page = self._parse_aircraft_table(browser, manufacturer)

            if not aircraft_on_page:
                logger.info(f"No aircraft found on page {page} for {manufacturer}")
                break

            logger.info(f"Found {len(aircraft_on_page)} aircraft on page {page} for {manufacturer}")
            all_aircraft.extend(aircraft_on_page)

            # Update database
            updated = self._update_database(aircraft_on_page)
            records_updated += updated

            # Check for next page
            if not self._has_next_page(browser, page):
                logger.info(f"No more pages for {manufacturer}")
                break

            page += 1
            self.wait_delay()

        # Add aircraft detail tasks to queue (tier 2 -> tier 3)
        detail_tasks_added = self._add_aircraft_detail_tasks(all_aircraft)

        return AirportDataResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            scrape_mode="manufacturer",
            manufacturer_name=manufacturer,
            aircraft=all_aircraft,
            aircraft_count=len(all_aircraft),
            pages_scraped=page,
            records_updated=records_updated,
            s3_paths=s3_paths,
            data={"detail_tasks_added": detail_tasks_added},
        )

    def _scrape_aircraft_detail(
        self, browser: Any, registration: str, task: ScraperTask
    ) -> AirportDataResult:
        """Scrape individual aircraft detail page.

        Args:
            browser: Browser instance.
            registration: Aircraft registration number.
            task: The task being processed.

        Returns:
            AirportDataResult with detailed aircraft data.
        """
        url = f"{self.BASE_URL}/aircraft/{registration}.html"
        logger.info(f"Scraping aircraft detail: {url}")

        browser.get(url)
        time.sleep(2)  # Wait for page load

        # Save screenshot and HTML
        self._save_screenshot(browser, f"aircraft_{registration}")
        html = browser.html
        s3_path = self._save_html(html, f"aircraft/{registration}")

        # Parse aircraft details
        aircraft = self._parse_aircraft_detail(browser, registration)

        if not aircraft:
            raise NoDataFoundError(task.task_key)

        # Update database
        records_updated = self._update_database([aircraft])

        return AirportDataResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            scrape_mode="aircraft",
            aircraft=[aircraft],
            aircraft_count=1,
            pages_scraped=1,
            records_updated=records_updated,
            s3_paths=[s3_path] if s3_path else [],
        )

    def _parse_aircraft_table(
        self, browser: Any, manufacturer: str
    ) -> list[AirportDataAircraftData]:
        """Parse aircraft table from manufacturer page.

        Args:
            browser: Browser instance with loaded page.
            manufacturer: Manufacturer name.

        Returns:
            List of aircraft data.
        """
        aircraft_list: list[AirportDataAircraftData] = []

        try:
            # Find the main data table
            # Look for table rows with aircraft data
            rows = browser.eles("tag:tr")

            for row in rows:
                try:
                    cells = row.eles("tag:td")
                    if len(cells) < 5:
                        continue

                    # Actual columns: Tail Number, Year Maker Model (combined), C/N, Engines, Seats, Location
                    # First column contains registration as link
                    reg_link = cells[0].ele("tag:a", timeout=0.5)
                    if not reg_link:
                        continue

                    registration = reg_link.text.strip()
                    if not registration:
                        continue

                    # Column 1: "Year Maker Model" combined - need to parse
                    year_maker_model = cells[1].text.strip() if len(cells) > 1 else ""
                    cn = cells[2].text.strip() if len(cells) > 2 else ""
                    engines_text = cells[3].text.strip() if len(cells) > 3 else ""
                    seats_text = cells[4].text.strip() if len(cells) > 4 else ""
                    location = cells[5].text.strip() if len(cells) > 5 else ""

                    # Parse "Year Maker Model" combined column
                    # Format: "2004 New Century Aerosport RADIAL ROCKET"
                    year_built = None
                    maker = manufacturer
                    model = None

                    if year_maker_model:
                        parts = year_maker_model.split(None, 1)  # Split on first space
                        if parts:
                            # First part might be year (4 digits)
                            if parts[0].isdigit() and len(parts[0]) == 4:
                                year_val = int(parts[0])
                                if year_val > 0 and year_val != 0:
                                    year_built = year_val if year_val > 1900 else None
                                # Rest is "Maker Model"
                                if len(parts) > 1:
                                    maker_model = parts[1].strip()
                                    # Try to split maker and model
                                    # Usually manufacturer name comes first, model is often UPPERCASE at end
                                    maker = maker_model
                                    model = None
                            else:
                                # No year, entire string is maker model
                                maker = year_maker_model
                                model = None

                    # Parse numeric values
                    engines = None
                    if engines_text and engines_text.isdigit():
                        engines = int(engines_text)

                    seats = None
                    if seats_text and seats_text.isdigit():
                        seats = int(seats_text)

                    aircraft = AirportDataAircraftData(
                        registration=registration,
                        year_built=year_built,
                        manufacturer=maker if maker else manufacturer,
                        model=model,
                        serial_number=cn if cn else None,
                        engines=engines,
                        seats=seats,
                        location=location if location else None,
                        source_url=f"{self.BASE_URL}/aircraft/{registration}.html",
                    )
                    aircraft_list.append(aircraft)

                except Exception as e:
                    logger.debug(f"Error parsing row: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error parsing aircraft table: {e}")

        return aircraft_list

    def _parse_aircraft_detail(
        self, browser: Any, registration: str
    ) -> AirportDataAircraftData | None:
        """Parse aircraft detail page using the extractor.

        Args:
            browser: Browser instance with loaded page.
            registration: Aircraft registration number.

        Returns:
            AirportDataAircraftData or None if not found.
        """
        try:
            html = browser.html

            # Use extractor to parse the HTML
            data = self.extractor.extract(html, {"registration": registration})

            # Check if any meaningful data was extracted
            if not data.get("registration"):
                return None

            return AirportDataAircraftData(**data)

        except Exception as e:
            logger.error(f"Error parsing aircraft detail: {e}")
            return None

    def _has_next_page(self, browser: Any, current_page: int) -> bool:
        """Check if there is a next page.

        Args:
            browser: Browser instance.
            current_page: Current page number.

        Returns:
            True if next page exists.
        """
        try:
            # Look for pagination links
            html = browser.html
            next_page = current_page + 1

            # Check for next page link
            if f"?p={next_page}" in html:
                return True

            # Check for "Next" link
            links = browser.eles("tag:a")
            for link in links:
                text = (link.text or "").lower()
                if "next" in text or ">" in text:
                    href = link.attr("href") or ""
                    if f"p={next_page}" in href or "p=" in href:
                        return True

            return False

        except Exception:
            return False

    def _save_screenshot(self, browser: Any, name: str) -> str:
        """Save a screenshot of the current page.

        Args:
            browser: Browser instance.
            name: Base name for the file (without extension or date).

        Returns:
            Path to saved screenshot.
        """
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        # Sanitize name for filename
        safe_name = re.sub(r"[^\w\-]", "_", name)
        filename = f"{safe_name}_{timestamp}.png"

        local_path = Path(self.screenshots_dir) / "screenshots" / filename
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # DrissionPage screenshot method
            if hasattr(browser, "get_screenshot"):
                browser.get_screenshot(path=str(local_path))
            elif hasattr(browser, "save_screenshot"):
                browser.save_screenshot(str(local_path))
            else:
                # Try generic screenshot
                browser.get_screenshot(path=str(local_path), full_page=True)

            logger.info(f"Saved screenshot to {local_path}")
            return str(local_path)
        except Exception as e:
            logger.warning(f"Failed to save screenshot: {e}")
            return ""

    def _save_html(self, html: str, name: str) -> str:
        """Save HTML to local filesystem and optionally to S3.

        Args:
            html: HTML content to save.
            name: Base name for the file (without extension or date).

        Returns:
            S3 path if uploaded, empty string otherwise.
        """
        timestamp = datetime.now(UTC).strftime("%Y%m%d")
        # Sanitize name for filename
        safe_name = re.sub(r"[^\w\-/]", "_", name)
        filename = f"{safe_name}_{timestamp}.html"

        # Save locally
        local_path = Path(self.screenshots_dir) / "html" / filename
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(html, encoding="utf-8")
        logger.debug(f"Saved HTML to {local_path}")

        # Upload to S3 if enabled
        if self.s3_upload and self.s3_bucket:
            client = self._ensure_s3_client()
            if client is None:
                return ""
            s3_key = f"{self.s3_prefix}/{filename}"
            try:
                client.put_object(
                    Bucket=self.s3_bucket,
                    Key=s3_key,
                    Body=html.encode("utf-8"),
                    ContentType="text/html",
                )
                logger.info(f"Uploaded HTML to s3://{self.s3_bucket}/{s3_key}")
                return f"s3://{self.s3_bucket}/{s3_key}"
            except ClientError as e:
                logger.error(f"Failed to upload to S3: {e}")

        return ""

    def _update_database(self, aircraft_list: list[AirportDataAircraftData]) -> int:
        """Persist a batch of aircraft records via the injected callback.

        The actual DB write lives in the calling application (see
        ``persist_aircraft_callback`` config key). If no callback is injected,
        this returns 0 and the records are expected to be picked up from the
        returned ``AirportDataResult`` by ``on_success``.
        """
        if not aircraft_list:
            return 0
        if self._persist_aircraft_callback is None:
            return 0
        try:
            return int(self._persist_aircraft_callback(aircraft_list) or 0)
        except Exception as e:
            logger.error(f"persist_aircraft_callback failed: {e}")
            return 0
