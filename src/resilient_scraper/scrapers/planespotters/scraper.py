"""
Planespotters.net scraper implementation.

Scrapes aircraft production lists from planespotters.net with support for:
- Aircraft family index scraping
- Production list pagination
- Raw HTML storage to S3
- planespotters_aircraft table updates
- Cloudflare bypass handling
- Cookie-based authentication for paginated access
"""

import http.cookiejar
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from resilient_scraper import ResilientScraper
from resilient_scraper.errors import (
    CloudflareBlockedError,
    NoDataFoundError,
    ScraperError,
)
from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.planespotters.db import PlanespottersDB
from resilient_scraper.scrapers.planespotters.models import (
    PlanespottersAircraftData,
    PlanespottersResult,
)

logger = logging.getLogger("scraper.planespotters")


class PlanespottersScraper(ResilientScraper[PlanespottersResult]):
    """Scraper for aircraft data from Planespotters.net.

    Supports two scrape modes:
    - "families": Scrape the aircraft index to get all family URLs
    - "production_list": Scrape paginated production list for a specific family

    Configuration options (in scraper config):
        screenshots_dir: Directory for screenshots (default: "data/planespotters_screenshots")
        s3_upload: Whether to upload HTML to S3 (default: False)
        s3_bucket: S3 bucket name (required if s3_upload is True)
        s3_prefix: S3 key prefix (default: "data/planespotters_raw")
        max_pages_per_family: Maximum pages to scrape per family (default: 50)
        skip_existing: Skip registrations already in DB (default: True)
        cookies_file: Path to Netscape format cookies file for authentication

    Task payload options:
        mode: Scrape mode ("families" or "production_list")
        manufacturer: Manufacturer slug (e.g., "boeing")
        family: Family slug (e.g., "747")

    Authentication:
        Planespotters requires login to view paginated results (page 2+).
        Export cookies from browser using an extension like "Get cookies.txt LOCALLY"
        and save to the cookies_file path. The file should be in Netscape format.
    """

    task_type = "planespotters"
    default_delay = (15.0, 25.0)  # Conservative for Cloudflare
    requires_browser = True
    cloudflare_protected = True

    platform_display_name = "Planespotters.net"

    LOGIN_INDICATORS = [
        "login | planespotters",
        "sign in",
        "log in to your account",
        "username",
        "password",
    ]

    LOGIN_SELECTORS = [
        'input[name="username"]',
        'input[name="email"]',
        'input[type="password"]',
        'form[action*="login"]',
    ]

    BASE_URL = "https://www.planespotters.net"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the Planespotters scraper.

        Args:
            config: Scraper configuration dictionary.
        """
        super().__init__(config)

        # Override s3_prefix default for planespotters
        self.s3_prefix = self.config.get("s3_prefix", "data/planespotters_raw")

        self.max_pages = self.config.get("max_pages_per_family", 50)
        self.skip_existing = self.config.get("skip_existing", True)

        # Cookie/auth configuration
        self.cookies_file = self.config.get("cookies_file", "")
        self._cookies_loaded = False

        # Track existing registrations for skip_existing
        self._existing_registrations: set[str] = set()

        # Database operations
        self.db = PlanespottersDB(self.db_engine)

    def setup(self) -> None:
        """Setup scraper resources."""
        super().setup()

        # Ensure own table exists and update db reference
        self.db = PlanespottersDB(self.db_engine)
        self.db.ensure_tables_exist()

        # Load existing registrations if skip_existing and DB is available
        if self.skip_existing and self.db_engine:
            self._existing_registrations = self.db.load_existing_registrations()

    def _detect_cloudflare_challenge(self, browser: Any) -> bool:
        """Check if Cloudflare challenge is shown.

        Args:
            browser: DrissionPage browser instance.

        Returns:
            True if Cloudflare challenge is present.
        """
        try:
            html = browser.html.lower() if browser.html else ""
            title = (browser.title or "").lower()

            cf_indicators = [
                "just a moment",
                "checking your browser",
                "verify you are human",
                "security verification",
                "cloudflare",
            ]

            return any(ind in title or ind in html for ind in cf_indicators)
        except Exception:
            return False

    def _wait_for_cloudflare(self, browser: Any, task_key: str) -> bool:
        """Wait for user to complete Cloudflare challenge.

        Args:
            browser: DrissionPage browser instance.
            task_key: Task identifier for logging.

        Returns:
            True if challenge passed, False if timeout.
        """
        logger.info(
            f"[{task_key}] Cloudflare challenge detected. "
            f"Please complete the verification in the browser window..."
        )

        start_time = time.time()
        check_count = 0

        while time.time() - start_time < self.login_timeout:
            time.sleep(self.login_check_interval)
            check_count += 1

            if not self._detect_cloudflare_challenge(browser):
                elapsed = time.time() - start_time
                logger.info(
                    f"[{task_key}] Cloudflare challenge passed! "
                    f"(waited {elapsed:.1f}s)"
                )
                return True

            # Log progress
            if check_count % 6 == 0:
                elapsed = time.time() - start_time
                logger.info(
                    f"[{task_key}] Waiting for Cloudflare... "
                    f"({elapsed:.0f}s elapsed)"
                )

        logger.warning(f"[{task_key}] Cloudflare timeout after {self.login_timeout}s")
        return False

    def _load_cookies_from_file(self) -> list[dict[str, Any]]:
        """Load cookies from Netscape format cookie file.

        Returns:
            List of cookie dictionaries for browser.
        """
        if not self.cookies_file:
            return []

        cookies_path = Path(self.cookies_file)
        if not cookies_path.exists():
            logger.warning(f"Cookies file not found: {self.cookies_file}")
            return []

        cookies: list[dict[str, Any]] = []
        try:
            # Use http.cookiejar to parse Netscape format
            jar = http.cookiejar.MozillaCookieJar(str(cookies_path))
            jar.load(ignore_discard=True, ignore_expires=True)

            for cookie in jar:
                # Filter for planespotters.net domain
                if "planespotters" in cookie.domain:
                    cookies.append({
                        "name": cookie.name,
                        "value": cookie.value,
                        "domain": cookie.domain,
                        "path": cookie.path,
                        "secure": cookie.secure,
                        "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
                    })

            logger.info(f"Loaded {len(cookies)} cookies from {self.cookies_file}")
        except Exception as e:
            logger.error(f"Failed to load cookies from file: {e}")

        return cookies

    def _set_browser_cookies(self, browser: Any) -> bool:
        """Set cookies on browser for authentication.

        Args:
            browser: DrissionPage browser instance.

        Returns:
            True if cookies were set successfully.
        """
        if self._cookies_loaded:
            return True

        cookies = self._load_cookies_from_file()
        if not cookies:
            return False

        try:
            # First navigate to the domain to set cookies
            browser.get(self.BASE_URL)
            time.sleep(3)

            # Handle Cloudflare if needed (including clicking turnstile)
            self._handle_cloudflare_turnstile(browser, max_wait=120)

            # Set each cookie
            for cookie in cookies:
                try:
                    # DrissionPage uses set.cookies() method
                    browser.set.cookies(cookie)
                except Exception as e:
                    logger.debug(f"Failed to set cookie {cookie.get('name')}: {e}")

            self._cookies_loaded = True
            logger.info(f"Set {len(cookies)} cookies on browser")

            # Refresh to apply cookies
            browser.refresh()
            time.sleep(3)

            return True
        except Exception as e:
            logger.error(f"Failed to set browser cookies: {e}")
            return False

    def _handle_cloudflare_turnstile(
        self, browser: Any, max_wait: int = 120
    ) -> bool:
        """Handle Cloudflare Turnstile challenge by clicking checkbox.

        Args:
            browser: Browser instance.
            max_wait: Maximum seconds to wait.

        Returns:
            True if challenge was resolved.
        """
        html = browser.html.lower()
        title = (browser.title or "").lower()

        # Check for Cloudflare challenge indicators
        cf_indicators = [
            "just a moment",
            "checking your browser",
            "verify you are human",
            "security verification",
        ]

        if not any(ind in title or ind in html for ind in cf_indicators):
            return True

        logger.info("[planespotters] Cloudflare Turnstile detected, attempting to solve...")

        start_time = time.time()
        clicked = False

        while time.time() - start_time < max_wait:
            try:
                # Try to find and click the Turnstile checkbox
                # It's usually in an iframe with class containing 'cf-turnstile'
                # or directly as a checkbox element

                # Method 1: Try clicking checkbox directly
                checkbox_selectors = [
                    'input[type="checkbox"]',
                    '.cf-turnstile input',
                    '#cf-turnstile input',
                    'iframe[src*="turnstile"]',
                    'div.cf-turnstile',
                    '[data-turnstile-callback]',
                ]

                for selector in checkbox_selectors:
                    try:
                        elem = browser.ele(selector, timeout=2)
                        if elem:
                            logger.info(f"[planespotters] Found element: {selector}")
                            elem.click()
                            clicked = True
                            time.sleep(3)
                            break
                    except Exception:
                        continue

                # Method 2: Try clicking inside iframe
                if not clicked:
                    try:
                        iframes = browser.eles('iframe')
                        for iframe in iframes:
                            src = iframe.attr('src') or ''
                            if 'challenges' in src or 'turnstile' in src:
                                logger.info("[planespotters] Found Turnstile iframe, switching...")
                                # Switch to iframe and click
                                browser.get(iframe)
                                time.sleep(1)
                                checkbox = browser.ele('input[type="checkbox"]', timeout=2)
                                if checkbox:
                                    checkbox.click()
                                    clicked = True
                                    time.sleep(3)
                                browser.back()
                                break
                    except Exception as e:
                        logger.debug(f"Iframe method failed: {e}")

                # Check if challenge is resolved
                time.sleep(5)
                html = browser.html.lower()
                title = (browser.title or "").lower()

                if not any(ind in title for ind in cf_indicators):
                    if "planespotters" in title:
                        logger.info("[planespotters] Cloudflare Turnstile resolved!")
                        return True

            except Exception as e:
                logger.debug(f"Turnstile handling error: {e}")

            time.sleep(5)

        logger.warning("[planespotters] Cloudflare Turnstile timeout")
        return False

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that the task can be processed.

        Args:
            task: The task to validate.

        Returns:
            True if valid, False otherwise.
        """
        if not task.task_key:
            return False

        # "index" is valid for families mode
        if task.task_key == "index":
            return True

        # Accept family slugs like "airbus-a320", "boeing-747-8", etc.
        # Format: manufacturer-model[-variant]
        if not re.match(r"^[a-z0-9-]+$", task.task_key.lower()):
            logger.warning(f"Invalid task_key format: {task.task_key}")
            return False

        return True

    def scrape(
        self, task: ScraperTask, browser: Any | None = None
    ) -> PlanespottersResult:
        """Execute the scraping operation.

        Args:
            task: The task to process.
            browser: DrissionPage browser instance.

        Returns:
            PlanespottersResult with extracted data.

        Raises:
            ScraperError: If scraping fails.
        """
        browser = self._prepare_browser(browser, task.task_key)

        # Load cookies for authentication if configured (skip if using existing browser)
        if self.cookies_file and not self._cookies_loaded and not self.use_existing_browser:
            self._set_browser_cookies(browser)

        mode = task.payload.get("mode", "production_list")
        if task.task_key == "index":
            mode = "families"

        if mode == "families":
            return self._scrape_families(task, browser)
        return self._scrape_production_list(task, browser)

    def _scrape_families(
        self, task: ScraperTask, browser: Any
    ) -> PlanespottersResult:
        """Scrape the aircraft index to get all family URLs.

        Args:
            task: The task to process.
            browser: DrissionPage browser instance.

        Returns:
            PlanespottersResult with family URLs.
        """
        url = f"{self.BASE_URL}/aircraft/index"
        logger.info(f"[families] Loading index: {url}")

        browser.get(url)
        time.sleep(8)

        if not self.handle_cloudflare(browser, max_wait=180):
            logger.warning("[families] Cloudflare challenge failed")
            raise CloudflareBlockedError(task_key=task.task_key)

        html = browser.html
        title = browser.title or ""

        # Always save screenshot for debugging
        self._save_screenshot(browser, "families_index")

        if "aircraft" not in title.lower() and "index" not in title.lower():
            logger.warning(f"[families] Page load may have failed: {title}")

        # Save HTML to S3
        s3_paths = []
        html_path = self._upload_html_to_s3(html, "families/index")
        if html_path:
            s3_paths.append(html_path)

        # Extract family URLs
        # Pattern: /aircraft/production/family-slug (e.g., /aircraft/production/airbus-a320)
        family_links = re.findall(
            r'href="(/aircraft/production/([a-z0-9-]+))"',
            html,
            re.IGNORECASE,
        )

        # Deduplicate and format
        seen: set[str] = set()
        family_urls: list[str] = []
        for link, family_slug in family_links:
            if family_slug not in seen and family_slug not in ("index",):
                seen.add(family_slug)
                family_urls.append(family_slug)  # Store just the slug for task keys

        logger.info(f"[families] Found {len(family_urls)} aircraft families")

        return PlanespottersResult(
            success=len(family_urls) > 0,
            task_key=task.task_key,
            task_type=self.task_type,
            scrape_mode="families",
            family_name="index",
            family_urls=family_urls,
            aircraft_count=0,
            pages_scraped=1,
            s3_paths=s3_paths,
        )

    def _scrape_production_list(
        self, task: ScraperTask, browser: Any
    ) -> PlanespottersResult:
        """Scrape production list pages for a specific aircraft family.

        Args:
            task: The task with family slug as task_key (e.g., "airbus-a320").
            browser: DrissionPage browser instance.

        Returns:
            PlanespottersResult with aircraft data.
        """
        # task_key is the family slug (e.g., "airbus-a320", "boeing-747-8")
        family_slug = task.task_key.lower()
        family_name = family_slug

        # Extract manufacturer from slug (first part before hyphen followed by letter)
        # e.g., "airbus-a320" -> "airbus", "boeing-747-8" -> "boeing"
        parts = family_slug.split("-")
        manufacturer = parts[0] if parts else "unknown"

        all_aircraft: list[PlanespottersAircraftData] = []
        s3_paths: list[str] = []
        pages_scraped = 0

        # Support resuming from a specific page
        start_page = task.payload.get("start_page", 1)
        current_page = max(1, start_page)
        if start_page > 1:
            logger.info(f"[{family_name}] Resuming from page {start_page}")

        while current_page <= self.max_pages:
            url = self._build_production_list_url(family_slug, current_page)
            logger.info(f"[{family_name}] Loading page {current_page}: {url}")

            browser.get(url)
            time.sleep(10)

            # Check for Cloudflare challenge and wait for manual completion
            if self._detect_cloudflare_challenge(browser):
                logger.info(f"[{family_name}] Cloudflare detected on page {current_page}")
                self._save_screenshot(browser, f"{family_name}_cloudflare_page{current_page}")

                if self.wait_for_login_enabled:
                    if not self._wait_for_cloudflare(browser, family_name):
                        if current_page == 1:
                            raise CloudflareBlockedError(task_key=task.task_key)
                        break
                else:
                    if current_page == 1:
                        raise CloudflareBlockedError(task_key=task.task_key)
                    break

            # Check for login page and wait for manual login
            if self._detect_login_required(browser):
                logger.info(f"[{family_name}] Login required on page {current_page}")
                self._save_screenshot(browser, f"{family_name}_login_page{current_page}")

                if self.wait_for_login_enabled:
                    if not self._wait_for_login(browser, family_name):
                        logger.warning(f"[{family_name}] Login timeout, stopping")
                        break
                    # Reload page after login
                    browser.get(url)
                    time.sleep(5)
                else:
                    logger.warning(f"[{family_name}] Login required, stopping")
                    break

            html = browser.html
            pages_scraped += 1

            # Save screenshot for debugging
            if current_page == 1:
                self._save_screenshot(browser, f"{family_name}_page1")

            # Upload HTML to S3
            html_path = self._upload_html_to_s3(
                html, f"production_lists/{family_name}_page{current_page}"
            )
            if html_path:
                s3_paths.append(html_path)

            # Parse aircraft from table
            aircraft_on_page = self._parse_production_list(html, url)
            if not aircraft_on_page:
                logger.info(f"[{family_name}] No aircraft found on page {current_page}, stopping")
                break

            logger.info(
                f"[{family_name}] Page {current_page}: found {len(aircraft_on_page)} aircraft"
            )

            # Filter out already-processed registrations if skip_existing
            new_aircraft = []
            for ac in aircraft_on_page:
                if self.skip_existing and ac.registration in self._existing_registrations:
                    continue
                new_aircraft.append(ac)
                # Mark as processed
                self._existing_registrations.add(ac.registration)

            all_aircraft.extend(new_aircraft)

            # Update database for new aircraft
            updated_count = self._upsert_aircraft(new_aircraft, manufacturer, family_slug)
            logger.info(
                f"[{family_name}] Page {current_page}: {updated_count} records updated"
            )

            # Check for next page
            if not self._has_next_page(html, current_page):
                logger.info(f"[{family_name}] No more pages after {current_page}")
                break

            current_page += 1
            self.wait_delay()

        records_updated = len(all_aircraft)
        logger.info(
            f"[{family_name}] Complete: {len(all_aircraft)} aircraft, "
            f"{pages_scraped} pages, {records_updated} records updated"
        )

        return PlanespottersResult(
            success=len(all_aircraft) > 0 or pages_scraped > 0,
            task_key=task.task_key,
            task_type=self.task_type,
            scrape_mode="production_list",
            family_name=family_name,
            aircraft=all_aircraft,
            aircraft_count=len(all_aircraft),
            pages_scraped=pages_scraped,
            records_updated=records_updated,
            s3_paths=s3_paths,
        )

    def _build_production_list_url(self, family_slug: str, page: int = 1) -> str:
        """Build the production list URL.

        Args:
            family_slug: Family slug (e.g., "airbus-a320", "boeing-747").
            page: Page number (1-based).

        Returns:
            Production list URL.
        """
        base = f"{self.BASE_URL}/aircraft/production/{family_slug}"
        if page <= 1:
            return base
        return f"{base}?page={page}"

    def _parse_production_list(
        self, html: str, source_url: str
    ) -> list[PlanespottersAircraftData]:
        """Parse aircraft data from production list page HTML.

        Args:
            html: HTML content of the page.
            source_url: URL of the page.

        Returns:
            List of PlanespottersAircraftData objects.
        """
        aircraft: list[PlanespottersAircraftData] = []
        seen_regs: set[str] = set()

        # Planespotters uses /airframe/ URLs with registration in link text
        # Pattern: /airframe/...">  F-WWAI  </a>
        # Handles various registration formats:
        # - European: F-GFKQ, D-AIBA, G-BUSD (X-XXXX or XX-XXXX)
        # - US: N293AT, N115AT (NXXXXX)
        # - Others: VH-XXX, JA8XXX, etc.
        reg_pattern = re.compile(
            r'/airframe/[^"]+\"[^>]*>\s*'
            r'([A-Z]{1,2}-[A-Z0-9]{2,5}|'  # European format: F-GFKQ, D-AIBA
            r'[A-Z][0-9][A-Z0-9]{2,5}|'     # Mixed format: JA8123
            r'N[0-9]{1,5}[A-Z]{0,2})'        # US format: N293AT
            r'\s*</a>',
            re.IGNORECASE,
        )

        reg_matches = reg_pattern.findall(html)
        logger.debug(f"Found {len(reg_matches)} registration matches")

        for reg in reg_matches:
            registration = reg.strip().upper()
            if registration in seen_regs:
                continue
            seen_regs.add(registration)

            aircraft.append(
                PlanespottersAircraftData(
                    registration=registration,
                    source_url=source_url,
                )
            )

        # If basic extraction worked, try to enrich with more data
        if aircraft:
            logger.info(f"Found {len(aircraft)} aircraft from registration links")
            # Try to extract additional data from table rows
            self._enrich_aircraft_data(html, aircraft)
        else:
            # Fallback: extract from airframe URL patterns
            aircraft = self._parse_from_airframe_urls(html, source_url)

        return aircraft

    def _enrich_aircraft_data(
        self, html: str, aircraft_list: list[PlanespottersAircraftData]
    ) -> None:
        """Enrich aircraft data with additional fields from HTML.

        Args:
            html: HTML content.
            aircraft_list: List of aircraft to enrich.
        """
        # Create a lookup by registration
        reg_lookup = {ac.registration: ac for ac in aircraft_list}

        # Find table rows and extract additional data
        # Look for rows containing known registrations
        for reg, ac in reg_lookup.items():
            # Find the row containing this registration
            # Pattern: look for the airframe link and surrounding content
            row_pattern = re.compile(
                rf'<tr[^>]*>.*?{re.escape(reg)}.*?</tr>',
                re.DOTALL | re.IGNORECASE,
            )
            row_match = row_pattern.search(html)
            if not row_match:
                continue

            row = row_match.group(0)

            # Extract MSN/serial number (usually first column, 3-digit number)
            msn_match = re.search(r'>(\d{3,5})<', row)
            if msn_match:
                ac.serial_number = msn_match.group(1)

            # Extract aircraft type/model
            type_match = re.search(
                r'Airbus\s+(A\d{3}[^<]*)',
                row,
                re.IGNORECASE,
            )
            if type_match:
                ac.model = f"Airbus {type_match.group(1).strip()}"

            # Extract operator/airline
            operator_match = re.search(
                r'title="[^"]*airline[^"]*">([^<]+)</a>',
                row,
                re.IGNORECASE,
            )
            if operator_match:
                ac.operator = operator_match.group(1).strip()

            # Extract delivery date (format: Mon YYYY or full date)
            date_match = re.search(
                r'([A-Z][a-z]{2}\s+\d{4})',
                row,
            )
            if date_match:
                ac.delivery_date = date_match.group(1)

            # Extract status
            status_keywords = ['Active', 'Stored', 'Scrapped', 'Preserved', 'Parked', 'Written off']
            for status in status_keywords:
                if status.lower() in row.lower():
                    ac.status = status
                    break

    def _parse_from_airframe_urls(
        self, html: str, source_url: str
    ) -> list[PlanespottersAircraftData]:
        """Parse aircraft from airframe URL patterns.

        Args:
            html: HTML content.
            source_url: URL of the page.

        Returns:
            List of aircraft data.
        """
        aircraft: list[PlanespottersAircraftData] = []
        seen: set[str] = set()

        # Pattern: /airframe/type-registration-operator/id
        # Example: /airframe/airbus-a320-100-f-wwai-airbus-industrie/e54273
        airframe_links = re.findall(
            r'/airframe/([a-z0-9-]+)/[a-z0-9]+',
            html,
            re.IGNORECASE,
        )

        for link_path in airframe_links:
            parts = link_path.split('-')
            # Try to find registration pattern in parts
            # Registrations often have format: X-XXXX or XXXXX
            for i, part in enumerate(parts):
                # Check if this part + next part forms a registration
                if i < len(parts) - 1:
                    potential_reg = f"{part}-{parts[i+1]}".upper()
                    if re.match(r'^[A-Z]{1,2}-[A-Z0-9]{2,5}$', potential_reg):
                        if potential_reg not in seen:
                            seen.add(potential_reg)
                            aircraft.append(
                                PlanespottersAircraftData(
                                    registration=potential_reg,
                                    source_url=source_url,
                                )
                            )
                        break

        logger.info(f"Extracted {len(aircraft)} aircraft from airframe URLs")
        return aircraft

    def _parse_production_list_alternative(
        self, html: str, source_url: str
    ) -> list[PlanespottersAircraftData]:
        """Alternative parsing approach for production list.

        Args:
            html: HTML content of the page.
            source_url: URL of the page.

        Returns:
            List of PlanespottersAircraftData objects.
        """
        # This method is now handled by _parse_from_airframe_urls
        return self._parse_from_airframe_urls(html, source_url)

    def _has_next_page(self, html: str, current_page: int) -> bool:
        """Check if there's a next page in pagination.

        Args:
            html: HTML content of the page.
            current_page: Current page number.

        Returns:
            True if next page exists.
        """
        # Look for pagination links
        next_page = current_page + 1

        # Pattern 1: ?page=N
        if f"page={next_page}" in html:
            return True

        # Pattern 2: "next" link
        if re.search(r'class="[^"]*next[^"]*"', html, re.IGNORECASE):
            return True

        # Pattern 3: Check for page numbers greater than current
        page_numbers = re.findall(r'\?page=(\d+)', html)
        if page_numbers:
            max_page = max(int(p) for p in page_numbers)
            return max_page > current_page

        return False

    def _upsert_aircraft(
        self,
        aircraft_list: list[PlanespottersAircraftData],
        manufacturer: str,
        family: str,
    ) -> int:
        """Upsert aircraft data into planespotters_aircraft table.

        Args:
            aircraft_list: List of aircraft to upsert.
            manufacturer: Manufacturer name.
            family: Aircraft family.

        Returns:
            Number of records updated.
        """
        return self.db.upsert_aircraft(aircraft_list, manufacturer, family)

    def _save_html_local(self, html: str, key_suffix: str) -> str | None:
        """Save HTML content to local file.

        Args:
            html: HTML content to save.
            key_suffix: Suffix for the filename.

        Returns:
            Local file path if successful, None otherwise.
        """
        try:
            # Create html directory under screenshots_dir
            html_dir = f"{self.screenshots_dir}/html"
            os.makedirs(html_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Replace slashes in key_suffix with underscores
            safe_suffix = key_suffix.replace("/", "_")
            filename = f"{html_dir}/ps_{safe_suffix}_{timestamp}.html"

            with open(filename, "w", encoding="utf-8") as f:
                f.write(html)

            logger.info(f"Saved HTML to {filename}")
            return filename
        except Exception as e:
            logger.warning(f"Failed to save HTML locally: {e}")
            return None

    def _upload_html_to_s3(
        self, html: str, key_suffix: str
    ) -> str | None:
        """Upload HTML content to S3, or save locally if S3 disabled.

        Args:
            html: HTML content to upload.
            key_suffix: Suffix for the S3 key.

        Returns:
            S3 path or local path if successful, None otherwise.
        """
        # Always save locally for debugging
        local_path = self._save_html_local(html, key_suffix)

        if not self.s3_enabled or not self.s3_client:
            return local_path

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        s3_key = f"{self.s3_prefix}/{key_suffix}_{timestamp}.html"

        try:
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=s3_key,
                Body=html.encode("utf-8"),
                ContentType="text/html; charset=utf-8",
            )
            logger.info(f"Uploaded HTML to s3://{self.s3_bucket}/{s3_key}")
            return f"s3://{self.s3_bucket}/{s3_key}"
        except ClientError as e:
            logger.warning(f"Failed to upload HTML to S3: {e}")
            return local_path

    def _save_screenshot(self, browser: Any, suffix: str) -> None:
        """Save a screenshot for debugging.

        Args:
            browser: Browser instance.
            suffix: Suffix for the filename.
        """
        try:
            os.makedirs(self.screenshots_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.screenshots_dir}/ps_{suffix}_{timestamp}.png"

            if hasattr(browser, "get_screenshot"):
                browser.get_screenshot(path=filename)
            elif hasattr(browser, "save_screenshot"):
                browser.save_screenshot(filename)

            logger.info(f"Saved screenshot: {filename}")
        except Exception as e:
            logger.warning(f"Failed to save screenshot: {e}")
