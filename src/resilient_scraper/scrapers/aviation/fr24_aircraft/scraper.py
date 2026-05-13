"""
FR24 Aircraft Schedule scraper implementation.

Downloads flight history and schedules for specific aircraft from FlightRadar24.
Complements the airport-based scrapers by providing aircraft-centric flight data.

DB persistence is delegated to the calling application via
``scraper.on_success``.
"""

import logging
import re
import time
from datetime import datetime
from typing import Any

from resilient_scraper.errors import NoDataFoundError, PageLoadError, ScraperError
from resilient_scraper.models import ScraperTask
from resilient_scraper.scraper import ResilientScraper
from resilient_scraper.scrapers.aviation.fr24_aircraft.models import (
    FlightData,
    FR24AircraftResult,
)

logger = logging.getLogger("resilient_scraper.scrapers.fr24_aircraft")


class FR24AircraftScraper(ResilientScraper[FR24AircraftResult]):
    """Scraper for FR24 aircraft schedule pages.

    Fetches flight history and schedules for a specific aircraft by registration.
    URL format: https://www.flightradar24.com/data/aircraft/{registration}

    Configuration options (in scraper config):
        max_load_earlier_clicks: Maximum pagination clicks for past flights (default: 15).
        load_more_delay: Delay between pagination clicks (default: 2.0s).
        sync_to_database: Whether to save to DB (default: True).
        database_url: Database connection string.

    Task payload options:
        max_clicks: Override max_load_earlier_clicks for this task.
    """

    task_type: str = "fr24_aircraft"
    default_delay = (10.0, 20.0)
    requires_browser = True
    cloudflare_protected = True
    task_timeout = 600  # 10 minutes — single aircraft page with pagination

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the FR24 aircraft scraper.

        Args:
            config: Scraper configuration dictionary.
        """
        super().__init__(config)

        # Pagination configuration
        self.max_load_earlier_clicks = self.config.get("max_load_earlier_clicks", 15)
        self.load_more_delay = self.config.get("load_more_delay", 2.0)

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that the task can be processed.

        Validates aircraft registration format (2-10 alphanumeric chars with hyphens).

        Args:
            task: The task to validate.

        Returns:
            True if valid, False otherwise.
        """
        if not task.task_key:
            return False

        registration = task.task_key.strip().upper()

        # Validate length (2-10 characters)
        if len(registration) < 2 or len(registration) > 10:
            return False

        # Must be alphanumeric with optional hyphens or plus signs
        # Valid formats: B-1343, N12345, G-ABCD, JA8089, 10+01 (German military)
        if not re.match(r"^[A-Z0-9][-+A-Z0-9]*[A-Z0-9]$|^[A-Z0-9]{2}$", registration):
            return False

        # Check for invalid values
        invalid_values = {"", "UNKNOWN", "N/A", "NA", "NONE", "NULL", "TEST"}
        if registration in invalid_values:
            return False

        return True

    def build_url(self, task: ScraperTask) -> str:
        """Build the FR24 aircraft page URL.

        Args:
            task: The task with aircraft registration as task_key.

        Returns:
            FR24 aircraft page URL.
        """
        registration = task.task_key.strip().lower()
        return f"https://www.flightradar24.com/data/aircraft/{registration}"

    def scrape(self, task: ScraperTask, browser: Any | None = None) -> FR24AircraftResult:
        """Scrape flight schedule for an aircraft.

        Args:
            task: Task with aircraft registration as task_key.
            browser: DrissionPage browser instance.

        Returns:
            FR24AircraftResult with extracted flight data.

        Raises:
            ScraperError: If scraping fails.
        """
        if browser is None:
            raise ScraperError(
                "Browser required for FR24 aircraft scraper",
                task_key=task.task_key,
                retryable=False,
            )

        registration = task.task_key.strip().upper()
        max_clicks = task.payload.get("max_clicks", self.max_load_earlier_clicks)

        # Visit aircraft page
        url = self.build_url(task)
        logger.info(f"[{registration}] Visiting: {url}")
        try:
            browser.get(url)
        except Exception as e:
            error_msg = str(e).lower()
            if "timeout" in error_msg or "timed out" in error_msg:
                logger.error(f"[{registration}] Page load timeout: {e}")
                raise PageLoadError(url, task_key=task.task_key)
            raise
        time.sleep(8)

        # Handle cookie consent dialog if present
        self._dismiss_cookie_consent(browser, registration)

        # Verify page loaded correctly
        title = browser.title or ""
        if "flightradar24" not in title.lower() and registration.lower() not in title.lower():
            logger.warning(f"[{registration}] Page load failed: {title}")
            raise PageLoadError(url, task_key=task.task_key)

        html = browser.html

        # Check for Cloudflare
        self.handle_cloudflare(browser)
        html = browser.html

        # Check if aircraft exists
        if "Aircraft not found" in html or "No data available" in html:
            logger.warning(f"[{registration}] Aircraft not found")
            raise NoDataFoundError(task_key=task.task_key)

        # Extract aircraft info from page header
        aircraft_type, aircraft_model, airline_name = self._extract_aircraft_info(html)

        # Handle pagination - click "Load earlier flights" to get more history
        load_more_clicks = self._handle_load_earlier(browser, max_clicks)
        logger.info(f"[{registration}] Performed {load_more_clicks} 'Load earlier flights' clicks")

        # Get final HTML after pagination
        html = browser.html

        # Extract flights from the table
        flights = self._extract_flights(html, registration)

        if not flights:
            logger.warning(f"[{registration}] No flights found")
            raise NoDataFoundError(task_key=task.task_key)

        logger.info(f"[{registration}] Extracted {len(flights)} flights")

        # Calculate date range
        date_range_start = None
        date_range_end = None
        flight_times = [f.scheduled_time for f in flights if f.scheduled_time]
        if flight_times:
            date_range_start = min(flight_times)
            date_range_end = max(flight_times)

        return FR24AircraftResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            aircraft_registration=registration,
            aircraft_type=aircraft_type,
            aircraft_model=aircraft_model,
            airline_name=airline_name,
            flights=flights,
            flights_count=len(flights),
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            load_more_clicks=load_more_clicks,
        )

    def _extract_aircraft_info(self, html: str) -> tuple[str | None, str | None, str | None]:
        """Extract aircraft metadata from the page header.

        FR24 aircraft pages show info like:
        - Aircraft type: A320
        - Model: Airbus A320-200
        - Operator: China Eastern Airlines

        Args:
            html: Page HTML content.

        Returns:
            Tuple of (aircraft_type, aircraft_model, airline_name).
        """
        aircraft_type = None
        aircraft_model = None
        airline_name = None

        # Extract aircraft type (ICAO code like A320, B738)
        # Pattern: <a href="/data/aircraft/a320">A320</a> or similar
        type_patterns = [
            r'href="/data/aircraft/([a-z0-9]{3,4})"[^>]*>([A-Z0-9]{3,4})</a>',
            r"Aircraft type[:\s]*<[^>]+>([A-Z0-9]{3,4})<",
            r">\s*([A-Z]\d{2}[A-Z0-9])\s*</(?:span|a|div)",
        ]
        for pattern in type_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                lastindex = match.lastindex or 1
                aircraft_type = match.group(2 if lastindex >= 2 else 1).upper()
                break

        # Extract full model name
        # Pattern: "Airbus A320-200" or "Boeing 737-800"
        model_patterns = [
            r"((?:Airbus|Boeing|Embraer|Bombardier|ATR|Cessna|Gulfstream|Dassault)\s+[A-Z0-9][-A-Z0-9\s]+)",
            r"<h2[^>]*>([^<]+(?:Airbus|Boeing|Embraer)[^<]+)</h2>",
            r'title="([^"]*(?:A3\d{2}|B7[0-9]{2}|E[0-9]{3})[^"]*)"',
        ]
        for pattern in model_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                model = match.group(1).strip()
                # Clean up common artifacts
                model = re.sub(r"\s+", " ", model)
                if len(model) > 5:
                    aircraft_model = model
                    break

        # Extract airline/operator name
        # Pattern: href="/data/airlines/..." title="Airline Name"
        airline_match = re.search(r'href="/data/airlines/[^"]+"\s*title="([^"]+)"', html)
        if airline_match:
            airline_name = airline_match.group(1).strip()
        else:
            # Fallback: get from link text
            airline_match = re.search(r'href="/data/airlines/[^"]+">([^<]+)</a>', html)
            if airline_match:
                airline_name = airline_match.group(1).strip()

        return aircraft_type, aircraft_model, airline_name

    def _handle_load_earlier(self, browser: Any, max_clicks: int) -> int:
        """Click "Load earlier flights" button repeatedly to load past flights.

        Args:
            browser: Browser instance.
            max_clicks: Maximum number of clicks.

        Returns:
            Number of successful clicks.
        """
        clicks = 0

        for _ in range(max_clicks):
            try:
                # Try to find the "Load earlier flights" button
                btn = browser.ele("text:Load earlier flights", timeout=3)
                if not btn:
                    # Try alternative patterns
                    btn = browser.ele("text:load earlier", timeout=2)
                if not btn:
                    btn = browser.ele("text:Load more", timeout=2)

                if not btn:
                    logger.debug("'Load earlier flights' button not found, stopping pagination")
                    break

                # Check if button is visible and clickable
                try:
                    if not btn.states.is_displayed:
                        logger.debug("'Load earlier flights' button not displayed, stopping")
                        break
                except Exception:
                    pass

                # Click the button
                btn.click()
                clicks += 1
                logger.debug(f"Clicked 'Load earlier flights' button ({clicks}/{max_clicks})")

                # Wait for content to load
                time.sleep(self.load_more_delay)

            except Exception as e:
                logger.debug(f"Error during 'Load earlier flights' pagination: {e}")
                break

        return clicks

    def _extract_flights(self, html: str, registration: str) -> list[FlightData]:
        """Parse HTML and extract flight data.

        The aircraft page has a different structure from airport pages:
        - No date separator rows - dates are embedded in each row
        - Each row has data-timestamp attributes with Unix timestamps
        - Flight data is in <tr class="data-row"> elements

        Args:
            html: Page HTML content.
            registration: Aircraft registration for context.

        Returns:
            List of FlightData objects.
        """
        flights: list[FlightData] = []

        # Track seen flights to avoid duplicates
        seen_flights: set[tuple[str | None, datetime | None, str | None]] = set()

        # Extract all tbodys (FR24 may have multiple table sections)
        tbody_matches = re.findall(r"<tbody[^>]*>(.*?)</tbody>", html, re.DOTALL | re.IGNORECASE)
        if not tbody_matches:
            return flights

        logger.debug(f"[{registration}] Found {len(tbody_matches)} tbody sections")

        # Process each tbody
        for tbody_idx, tbody_content in enumerate(tbody_matches):
            # Extract data-row rows (flight rows have class="data-row")
            rows = re.findall(
                r'<tr[^>]*class="[^"]*data-row[^"]*"[^>]*>(.*?)</tr>',
                tbody_content,
                re.DOTALL,
            )

            logger.debug(
                f"[{registration}] Processing tbody {tbody_idx + 1} with {len(rows)} data rows"
            )

            for row_html in rows:
                # Skip rows without flight links
                if "/data/flights/" not in row_html:
                    continue

                try:
                    flight = self._parse_flight_row(row_html, registration)
                    if flight and flight.flight_number:
                        # Always set aircraft_registration from task key
                        flight.aircraft_registration = registration

                        # Create a key for deduplication
                        flight_key = (
                            flight.flight_number,
                            flight.scheduled_time,
                            flight.remote_airport_iata,
                        )

                        # Skip if we've already seen this exact flight
                        if flight_key in seen_flights:
                            logger.debug(
                                f"[{registration}] Skipping duplicate flight: "
                                f"{flight.flight_number} at {flight.scheduled_time}"
                            )
                            continue

                        seen_flights.add(flight_key)
                        flights.append(flight)
                except Exception as e:
                    logger.debug(f"Error parsing flight row: {e}")
                    continue

        return flights

    def _parse_flight_row(
        self,
        row_html: str,
        registration: str,
    ) -> FlightData | None:
        """Parse a single flight row from aircraft page.

        FR24 aircraft page has data-timestamp attributes with Unix timestamps
        for STD (scheduled), ATD (actual departure), STA (scheduled arrival).

        Args:
            row_html: HTML content of a flight row.
            registration: Aircraft registration for context.

        Returns:
            FlightData object or None if parsing fails.
        """
        _ = registration  # Suppress unused variable warning
        flight = FlightData()

        # Extract flight ID and number from link
        # Pattern: href="/data/flights/hu7157" class="fbold">HU7157</a>
        flight_id_match = re.search(r'href="/data/flights/([^"]+)"', row_html)
        if flight_id_match:
            flight.flight_id = flight_id_match.group(1)
            flight.flight_number = flight_id_match.group(1).upper()

        # Try to get flight number from link text (more reliable)
        flight_text_match = re.search(r'href="/data/flights/[^"]+[^>]*>([A-Z0-9]+)</a>', row_html)
        if flight_text_match:
            flight.flight_number = flight_text_match.group(1).upper()

        # Extract callsign if present
        callsign_match = re.search(r'data-callsign="([^"]+)"', row_html)
        if callsign_match:
            flight.callsign = callsign_match.group(1)

        # Extract airline info from flight number (first 2 letters)
        if flight.flight_number and len(flight.flight_number) >= 2:
            # Extract IATA code from flight number
            iata_match = re.match(r"([A-Z]{2})", flight.flight_number)
            if iata_match:
                flight.airline_iata = iata_match.group(1)

        # Extract airports - aircraft page shows both origin and destination
        # Pattern: <a href="/data/airports/szx" class="fbold">(SZX)</a>
        airport_matches = re.findall(
            r'href="/data/airports/([^"]+)"[^>]*>\(?([A-Z]{3})\)?', row_html
        )
        if len(airport_matches) >= 2:
            # First is origin (FROM), second is destination (TO)
            # Store both for reference
            origin_iata = airport_matches[0][1]
            dest_iata = airport_matches[1][1]
            # Set remote_airport to destination
            flight.remote_airport_iata = dest_iata
            # Store origin in a temporary way (we could add origin_airport_iata to FlightData)
            logger.debug(f"Flight {flight.flight_number}: {origin_iata} -> {dest_iata}")
        elif len(airport_matches) == 1:
            flight.remote_airport_iata = airport_matches[0][1]

        # Extract airport names
        # Pattern: title="Shenzhen Bao'an International Airport, China"
        airport_title_matches = re.findall(r'title="([^"]+Airport[^"]*)"', row_html)
        if len(airport_title_matches) >= 2:
            # Second one is destination
            flight.remote_airport_name = airport_title_matches[1].split(",")[0].strip()
        elif len(airport_title_matches) == 1:
            flight.remote_airport_name = airport_title_matches[0].split(",")[0].strip()

        # Extract status from state-block color and text
        if re.search(r"state-block[^>]*green", row_html, re.IGNORECASE):
            flight.status = "Landed"
        elif re.search(r"state-block[^>]*red", row_html, re.IGNORECASE):
            flight.status = "Delayed"
        elif re.search(r"state-block[^>]*yellow", row_html, re.IGNORECASE):
            flight.status = "En Route"
        elif re.search(r"state-block[^>]*blue", row_html, re.IGNORECASE):
            flight.status = "Scheduled"

        # Also check for text status
        if re.search(r"data-prefix=['\"]Landed", row_html, re.IGNORECASE):
            flight.status = "Landed"
        elif re.search(r">Scheduled<", row_html, re.IGNORECASE):
            flight.status = "Scheduled"
        elif re.search(r">En Route<", row_html, re.IGNORECASE):
            flight.status = "En Route"
        elif re.search(r">Cancelled<|>Canceled<", row_html, re.IGNORECASE):
            flight.status = "Cancelled"

        # Extract times from data-timestamp attributes
        # The page has multiple timestamps - we need to find STD, ATD, STA
        # STD = Scheduled Time of Departure
        # ATD = Actual Time of Departure
        # STA = Scheduled Time of Arrival
        timestamps = re.findall(r'data-timestamp="(\d{10,13})"', row_html)

        if timestamps:
            # Convert all timestamps to datetime
            times: list[datetime] = []
            for ts_str in timestamps:
                try:
                    ts = int(ts_str)
                    if ts > 1e12:
                        ts = ts // 1000
                    dt = datetime.fromtimestamp(ts, tz=UTC)
                    if dt not in times:
                        times.append(dt)
                except (ValueError, OSError):
                    continue

            # Sort times and assign (first is usually scheduled departure)
            times.sort()
            if times:
                # First timestamp is typically scheduled departure time
                flight.scheduled_time = times[0]
                # If there are more times, the last one might be actual
                if len(times) >= 2:
                    flight.actual_time = times[-1]

        return flight

