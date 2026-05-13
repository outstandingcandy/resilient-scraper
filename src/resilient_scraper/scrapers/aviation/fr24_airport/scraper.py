"""
FR24 Airport Flights scraper implementation.

Downloads airport arrival and departure flight information from Flightradar24.
Supports both arrivals and departures with pagination.

DB persistence is delegated to the calling application via
``scraper.on_success``.
"""

import logging
import re
import time
from datetime import date, datetime
from typing import Any

from resilient_scraper.errors import NoDataFoundError, PageLoadError, ScraperError
from resilient_scraper.models import ScraperTask
from resilient_scraper.scraper import ResilientScraper
from resilient_scraper.scrapers.aviation.fr24_airport.models import (
    FlightData,
    FR24FlightsResult,
)

logger = logging.getLogger("resilient_scraper.scrapers.fr24_airport")


class _FR24AirportScraper(ResilientScraper[FR24FlightsResult]):
    """Base class for FR24 airport arrivals/departures scrapers.

    This class contains shared logic for both arrival and departure scrapers.
    Subclasses only need to define task_type, flight_type, and url_suffix.

    Configuration options (in scraper config):
        max_load_more_clicks: Maximum pagination clicks for future flights (default: 10).
        max_load_earlier_clicks: Maximum pagination clicks for past flights (default: 10).
        load_more_delay: Delay between pagination clicks (default: 2.0s).
        sync_to_database: Whether to save to DB (default: True).
        database_url: Database connection string.

    Task payload options:
        max_clicks: Override max_load_more_clicks for this task.
    """

    task_type: str = "fr24_flights_base"
    flight_type: str = ""  # "arrival" or "departure"
    url_suffix: str = ""  # "arrivals" or "departures"

    default_delay = (10.0, 20.0)
    requires_browser = True
    cloudflare_protected = True
    task_timeout = 900  # 15 minutes — airport page with heavy pagination

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the FR24 flights scraper.

        Args:
            config: Scraper configuration dictionary.
        """
        super().__init__(config)

        # Pagination configuration
        self.max_load_more_clicks = self.config.get("max_load_more_clicks", 10)
        self.load_more_delay = self.config.get("load_more_delay", 2.0)

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that the task can be processed.

        Validates airport code format (3-letter IATA or 4-letter ICAO).

        Args:
            task: The task to validate.

        Returns:
            True if valid, False otherwise.
        """
        if not task.task_key:
            return False

        airport_code = task.task_key.strip().upper()

        # Validate length (3 for IATA, 4 for ICAO)
        if len(airport_code) < 3 or len(airport_code) > 4:
            return False

        # Must be alphanumeric
        if not airport_code.isalnum():
            return False

        # Check for invalid values
        invalid_values = {"", "UNKNOWN", "N/A", "NA", "NONE", "NULL", "TEST"}
        if airport_code in invalid_values:
            return False

        return True

    def build_url(self, task: ScraperTask) -> str:
        """Build the FR24 arrivals/departures URL.

        Args:
            task: The task with airport code as task_key.

        Returns:
            FR24 airport arrivals/departures page URL.
        """
        airport_code = task.task_key.strip().upper()
        return (
            f"https://www.flightradar24.com/data/airports/{airport_code.lower()}/{self.url_suffix}"
        )

    def scrape(self, task: ScraperTask, browser: Any | None = None) -> FR24FlightsResult:
        """Scrape flight arrivals or departures for an airport.

        Args:
            task: Task with airport code as task_key.
            browser: DrissionPage browser instance.

        Returns:
            FR24FlightsResult with extracted flight data.

        Raises:
            ScraperError: If scraping fails.
        """
        if browser is None:
            raise ScraperError(
                "Browser required for FR24 flights scraper",
                task_key=task.task_key,
                retryable=False,
            )

        airport_code = task.task_key.strip().upper()
        max_clicks = task.payload.get("max_clicks", self.max_load_more_clicks)

        # Visit airport page
        url = self.build_url(task)
        logger.info(f"[{airport_code}] Visiting: {url}")
        try:
            browser.get(url)
        except Exception as e:
            error_msg = str(e).lower()
            if "timeout" in error_msg or "timed out" in error_msg:
                logger.error(f"[{airport_code}] Page load timeout: {e}")
                raise PageLoadError(url, task_key=task.task_key)
            raise
        time.sleep(8)

        # Handle cookie consent dialog if present
        self._dismiss_cookie_consent(browser, airport_code)

        # Verify page loaded correctly
        title = browser.title or ""
        if "flightradar24" not in title.lower() and airport_code.lower() not in title.lower():
            logger.warning(f"[{airport_code}] Page load failed: {title}")
            raise PageLoadError(url, task_key=task.task_key)

        html = browser.html

        # Check for Cloudflare
        self.handle_cloudflare(browser)
        html = browser.html

        # Extract airport name from page
        airport_name = self._extract_airport_name(html)

        # Handle pagination - click "Load later flights" to get future scheduled flights
        # Focus on future flights first, then get some past flights
        load_later_clicks = self._handle_load_later(browser, max_clicks)
        logger.info(f"[{airport_code}] Performed {load_later_clicks} 'Load later flights' clicks")

        # Save HTML and screenshot after loading future flights for debugging
        html_after_later = browser.html
        if "Jan 26" in html_after_later or "Jan 27" in html_after_later:
            logger.info(f"[{airport_code}] Found Jan 26/27 in HTML after 'Load later' clicks!")
        else:
            logger.debug(f"[{airport_code}] No Jan 26/27 found in HTML yet")
            # Save HTML and screenshot for debugging (only first airport)
            if airport_code == "PKX":
                debug_path = f"/tmp/fr24_debug_{airport_code}.html"
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(html_after_later)
                logger.info(f"[{airport_code}] Saved debug HTML to {debug_path}")
                # Take screenshot
                try:
                    screenshot_path = f"/tmp/fr24_screenshot_{airport_code}.png"
                    browser.get_screenshot(path=screenshot_path, full_page=True)
                    logger.info(f"[{airport_code}] Saved screenshot to {screenshot_path}")
                except Exception as e:
                    logger.warning(f"[{airport_code}] Failed to save screenshot: {e}")

        # Then load past flights (bottom of page) - minimal clicks to preserve future data
        load_earlier_clicks = self._handle_load_earlier(
            browser, 10
        )  # Fixed 10 clicks for past flights
        logger.info(
            f"[{airport_code}] Performed {load_earlier_clicks} 'Load earlier flights' clicks"
        )

        load_more_clicks = load_later_clicks + load_earlier_clicks

        # Get final HTML after pagination
        html = browser.html

        # Extract flights from the table
        flights = self._extract_flights(html, airport_code)

        if not flights:
            logger.warning(f"[{airport_code}] No flights found")
            raise NoDataFoundError(task_key=task.task_key)

        logger.info(f"[{airport_code}] Extracted {len(flights)} flights")

        # Calculate date range
        date_range_start = None
        date_range_end = None
        flight_times = [f.scheduled_time for f in flights if f.scheduled_time]
        if flight_times:
            date_range_start = min(flight_times)
            date_range_end = max(flight_times)

        return FR24FlightsResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            airport_code=airport_code,
            airport_name=airport_name,
            flight_type=self.flight_type,
            flights=flights,
            flights_count=len(flights),
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            load_more_clicks=load_more_clicks,
        )

    def _extract_airport_name(self, html: str) -> str:
        """Extract airport name from the page HTML.

        Args:
            html: Page HTML content.

        Returns:
            Airport name or empty string if not found.
        """
        # Pattern: <h1>Airport Name</h1> or similar
        patterns = [
            r"<h1[^>]*>([^<]+)</h1>",
            r'class="airport-name[^"]*"[^>]*>([^<]+)<',
            r"<title>([^|<]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Clean up common suffixes
                name = re.sub(
                    r"\s*(Airport|International|Arrivals|Departures).*$",
                    "",
                    name,
                    flags=re.IGNORECASE,
                )
                if name:
                    return name.strip()
        return ""

    def _handle_load_later(self, browser: Any, max_clicks: int) -> int:
        """Click "Load later flights" button repeatedly to load future flights.

        Args:
            browser: Browser instance.
            max_clicks: Maximum number of clicks.

        Returns:
            Number of successful clicks.
        """
        clicks = 0

        for _ in range(max_clicks):
            try:
                # Try to find the "Load later flights" button (for future flights)
                btn = browser.ele("text:Load later flights", timeout=3)
                if not btn:
                    btn = browser.ele("text:load later", timeout=2)

                if not btn:
                    logger.debug("'Load later flights' button not found, stopping")
                    break

                # Check if button is visible and clickable
                try:
                    if not btn.states.is_displayed:
                        logger.debug("'Load later flights' button not displayed, stopping")
                        break
                except Exception:
                    pass

                # Click the button
                btn.click()
                clicks += 1
                logger.debug(f"Clicked 'Load later flights' button ({clicks}/{max_clicks})")
                # Wait for content to load
                time.sleep(self.load_more_delay)

            except Exception as e:
                logger.debug(f"Error during 'Load later flights' pagination: {e}")
                break

        return clicks

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

    def _extract_flights(self, html: str, airport_code: str) -> list[FlightData]:
        """Parse HTML and extract flight data.

        Args:
            html: Page HTML content.
            airport_code: Airport code for context.

        Returns:
            List of FlightData objects.
        """
        from datetime import date as date_type
        from zoneinfo import ZoneInfo

        flights: list[FlightData] = []
        beijing_tz = ZoneInfo("Asia/Shanghai")
        now_beijing = datetime.now(beijing_tz)
        default_date = now_beijing.date()

        # Track seen flights to avoid duplicates: (flight_number, scheduled_time, registration)
        seen_flights: set[tuple[str | None, datetime | None, str | None]] = set()

        # Extract all tbodys (FR24 may have multiple table sections)
        tbody_matches = re.findall(r"<tbody[^>]*>(.*?)</tbody>", html, re.DOTALL | re.IGNORECASE)
        if not tbody_matches:
            return flights

        logger.debug(f"[{airport_code}] Found {len(tbody_matches)} tbody sections")

        # Process each tbody independently to handle date separators correctly
        for tbody_idx, tbody_content in enumerate(tbody_matches):
            # Reset current_date for each tbody section
            current_date: date_type | None = None
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody_content, re.DOTALL)

            logger.debug(f"[{airport_code}] Processing tbody {tbody_idx + 1} with {len(rows)} rows")

            for row_html in rows:
                # Check if this is a date separator row
                # Format: "Saturday, Jan 24" or "Friday, Jan 23"
                if "/data/flights/" not in row_html:
                    # Extract text content
                    text = re.sub(r"<[^>]+>", " ", row_html).strip()
                    text = " ".join(text.split())

                    # Try to parse date from separator
                    # Pattern: "Weekday, Mon DD"
                    date_match = re.search(
                        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*"
                        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})",
                        text,
                        re.IGNORECASE,
                    )
                    if date_match:
                        month_str = date_match.group(1)
                        day = int(date_match.group(2))
                        months = {
                            "jan": 1,
                            "feb": 2,
                            "mar": 3,
                            "apr": 4,
                            "may": 5,
                            "jun": 6,
                            "jul": 7,
                            "aug": 8,
                            "sep": 9,
                            "oct": 10,
                            "nov": 11,
                            "dec": 12,
                        }
                        month = months.get(month_str.lower(), now_beijing.month)
                        # Determine year (handle year boundary)
                        year = now_beijing.year
                        if month == 12 and now_beijing.month == 1:
                            year -= 1
                        elif month == 1 and now_beijing.month == 12:
                            year += 1
                        try:
                            current_date = date_type(year, month, day)
                            logger.debug(f"[{airport_code}] Found date separator: {current_date}")
                        except ValueError:
                            pass
                    continue

                # This is a flight row - parse it with the current date
                # Skip flights that appear before any date separator to avoid wrong date assignment
                if current_date is None:
                    logger.debug(f"[{airport_code}] Skipping flight row before date separator")
                    continue

                try:
                    flight = self._parse_flight_row(
                        row_html, airport_code, flight_date=current_date
                    )
                    if flight:
                        # Create a key for deduplication
                        flight_key = (
                            flight.flight_number,
                            flight.scheduled_time,
                            flight.aircraft_registration,
                        )

                        # Skip if we've already seen this exact flight
                        if flight_key in seen_flights:
                            logger.debug(
                                f"[{airport_code}] Skipping duplicate flight: "
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
        airport_code: str,
        flight_date: "date | None" = None,
    ) -> FlightData | None:
        """Parse a single flight row.

        FR24 HTML structure (based on actual page):
        - Flight: <a href="/data/flights/mu2851" title="MU2851">MU2851</a>
        - Airport: <span class="hide-mobile-only">Nanjing </span><a href="/data/airports/nkg">(NKG)</a>
        - Airline: <a href="/data/airlines/mu-ces" title="...">China Eastern Airlines</a>
        - Aircraft: <span>A20N </span><a href="/data/aircraft/b-320h">(B-320H)</a>
        - Status: <span>Landed</span>

        Args:
            row_html: HTML content of a flight row.
            airport_code: Airport code for context.
            flight_date: Date for this flight (from date separator row).

        Returns:
            FlightData object or None if parsing fails.
        """
        # Suppress unused variable warning
        _ = airport_code
        flight = FlightData()
        # Set flight type (arrival or departure) from scraper instance
        flight.flight_type = self.flight_type

        # Extract flight ID and number from link
        # Pattern: href="/data/flights/mu2851" title="MU2851"
        flight_id_match = re.search(r'href="/data/flights/([^"]+)"', row_html)
        if flight_id_match:
            flight.flight_id = flight_id_match.group(1)
            flight.flight_number = flight_id_match.group(1).upper()

        # Try to get flight number from title attribute (more reliable)
        title_match = re.search(r'href="/data/flights/[^"]+"\s+title="([^"]+)"', row_html)
        if title_match:
            flight.flight_number = title_match.group(1).upper()

        # Extract callsign if present
        callsign_match = re.search(r'data-callsign="([^"]+)"', row_html)
        if callsign_match:
            flight.callsign = callsign_match.group(1)

        # Extract airline info
        # Pattern: href="/data/airlines/mu-ces" title="China Eastern Airlines"
        airline_match = re.search(r'href="/data/airlines/([^"]+)"[^>]*title="([^"]+)"', row_html)
        if airline_match:
            # Extract IATA code (first part before dash): mu-ces -> MU
            airline_code = airline_match.group(1).split("-")[0].upper()
            flight.airline_iata = airline_code
            flight.airline_name = airline_match.group(2).strip()
        else:
            # Fallback: get name from link text
            airline_match = re.search(r'href="/data/airlines/([^"]+)"[^>]*>([^<]+)</a>', row_html)
            if airline_match:
                airline_code = airline_match.group(1).split("-")[0].upper()
                flight.airline_iata = airline_code
                flight.airline_name = airline_match.group(2).strip()

        # Extract remote airport (origin for arrivals, destination for departures)
        # Pattern: <span class="hide-mobile-only">Nanjing </span><a href="/data/airports/nkg">(NKG)</a>
        airport_name_match = re.search(
            r'class="[^"]*hide-mobile-only[^"]*"[^>]*>([^<]+)</span>', row_html
        )
        if airport_name_match:
            flight.remote_airport_name = airport_name_match.group(1).strip()

        airport_code_match = re.search(
            r'href="/data/airports/([^"]+)"[^>]*>\(([A-Z]{3})\)', row_html
        )
        if airport_code_match:
            flight.remote_airport_iata = airport_code_match.group(2)
        else:
            # Fallback: find any 3-letter code in parentheses
            airport_code_match = re.search(r"\(([A-Z]{3})\)", row_html)
            if airport_code_match:
                flight.remote_airport_iata = airport_code_match.group(1)

        # Extract aircraft registration
        # Pattern: href="/data/aircraft/b-320h" title="B-320H"
        aircraft_match = re.search(r'href="/data/aircraft/([^"]+)"[^>]*title="([^"]+)"', row_html)
        if aircraft_match:
            flight.aircraft_registration = aircraft_match.group(2).upper()
        else:
            # Fallback: get from URL
            aircraft_match = re.search(r'href="/data/aircraft/([^"]+)"', row_html)
            if aircraft_match:
                flight.aircraft_registration = aircraft_match.group(1).upper()

        # Extract aircraft type
        # Pattern: <span...>A20N </span> before the aircraft link
        # Look for ICAO aircraft type codes (4 characters like A20N, B738, E190)
        type_patterns = [
            # Standard ICAO type codes
            r">([A-Z]\d{2}[A-Z0-9])\s*</span>",  # A20N, B738, E190, etc.
            r">([A-Z]{2}\d{2})\s*</span>",  # AT72, etc.
            r">\s*([A-Z]\d{3})\s*<",  # A320, B737, etc.
            r">\s*([A-Z]{2}\d{2}[A-Z]?)\s*<",  # AT72, CRJ9, etc.
        ]
        for pattern in type_patterns:
            type_match = re.search(pattern, row_html)
            if type_match:
                candidate_type = type_match.group(1)
                # Validate: aircraft type should not match flight number
                # Flight numbers like CX50, JL21, TK26 can be falsely matched
                if flight.flight_number and candidate_type == flight.flight_number:
                    continue  # Skip this match, it's the flight number not aircraft type
                flight.aircraft_type = candidate_type
                break

        # Extract status
        # Pattern: <span...>Landed</span> or state-block with color class
        # Note: Text patterns must come before color patterns to avoid misclassification
        # FR24 shows "Estimated dep. 7:00 AM" or "Estimated arr. 10:30 PM" for estimated flights
        status_patterns = [
            (r">Estimated\b", "Estimated"),  # Matches ">Estimated" followed by space/punctuation
            (r">Landed<", "Landed"),
            (r">En Route<", "En Route"),
            (r">en route<", "En Route"),
            (r">Scheduled<", "Scheduled"),
            (r">Delayed<", "Delayed"),
            (r">Cancelled<", "Cancelled"),
            (r">Canceled<", "Cancelled"),
            (r">Diverted<", "Diverted"),
            # Color-based patterns as fallback only
            (r'class="[^"]*state-block[^"]*yellow', "En Route"),
            (r'class="[^"]*state-block[^"]*red', "Delayed"),
            # Green can mean Landed OR Estimated, so don't use it as primary indicator
        ]
        for pattern, status in status_patterns:
            if re.search(pattern, row_html, re.IGNORECASE):
                flight.status = status
                break

        # For departure flights, convert "Landed" to "Departed"
        # (on FR24 departures page, "Landed" means the flight has arrived at destination)
        if flight.flight_type == "departure" and flight.status == "Landed":
            flight.status = "Departed"

        # Fallback: if no status found, try to infer from time
        # This handles cases where FR24's status format doesn't match our patterns
        if not flight.status:
            # Check if we can find any status-like element we might have missed
            # Look for common state indicators in the HTML
            if re.search(r"state-block.*?(?:bg-|text-)(?:green|success)", row_html, re.IGNORECASE):
                # Green usually means completed (Landed for arrival, Departed for departure)
                flight.status = "Departed" if flight.flight_type == "departure" else "Landed"
            elif re.search(
                r"state-block.*?(?:bg-|text-)(?:yellow|warning)", row_html, re.IGNORECASE
            ):
                flight.status = "Delayed"
            elif re.search(r"state-block.*?(?:bg-|text-)(?:red|danger)", row_html, re.IGNORECASE):
                flight.status = "Cancelled"
            elif re.search(
                r"state-block.*?(?:bg-|text-)(?:blue|info|primary)", row_html, re.IGNORECASE
            ):
                flight.status = "En Route"

        # Extract terminal and gate
        terminal_match = re.search(r"Terminal\s*([A-Z0-9]+)", row_html, re.IGNORECASE)
        if terminal_match:
            flight.terminal = terminal_match.group(1)

        gate_match = re.search(r"Gate\s*([A-Z0-9]+)", row_html, re.IGNORECASE)
        if gate_match:
            flight.gate = gate_match.group(1)

        # Extract time information
        # FR24 displays times in various formats: "HH:MM", timestamps, or relative times
        flight.scheduled_time, flight.estimated_time, flight.actual_time = self._extract_times(
            row_html, flight_date
        )

        return flight

    def _extract_times(
        self,
        row_html: str,
        flight_date: "date | None" = None,
    ) -> tuple[datetime | None, datetime | None, datetime | None]:
        """Extract scheduled, estimated, and actual times from flight row.

        FR24 displays times in 12-hour format with AM/PM (e.g., "7:15 PM").
        Times are in local airport timezone (Beijing time for Chinese airports).
        We convert to UTC for storage.

        Args:
            row_html: HTML content of the flight row.
            flight_date: The date for this flight (from date separator).

        Returns:
            Tuple of (scheduled_time, estimated_time, actual_time) in UTC.
        """
        from zoneinfo import ZoneInfo

        times: list[datetime] = []
        # Use Beijing timezone for Chinese airports
        beijing_tz = ZoneInfo("Asia/Shanghai")
        # Use provided date or default to today in Beijing timezone
        if flight_date:
            target_date = flight_date
        else:
            now_beijing = datetime.now(beijing_tz)
            target_date = now_beijing.date()

        # Pattern 1: 12-hour format with AM/PM (FR24's primary format)
        # Matches: "6:40 PM", "11:30 AM", "7:15 PM"
        ampm_matches = re.findall(r"(\d{1,2}):(\d{2})\s*([AP]M)", row_html, re.IGNORECASE)
        for hour_str, minute_str, ampm in ampm_matches:
            try:
                hour = int(hour_str)
                minute = int(minute_str)
                # Convert to 24-hour format
                if ampm.upper() == "PM" and hour != 12:
                    hour += 12
                elif ampm.upper() == "AM" and hour == 12:
                    hour = 0
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    # Create datetime in Beijing timezone
                    dt_beijing = datetime(
                        target_date.year,
                        target_date.month,
                        target_date.day,
                        hour,
                        minute,
                        tzinfo=beijing_tz,
                    )
                    # Convert to UTC for storage
                    dt_utc = dt_beijing.astimezone(UTC)
                    if dt_utc not in times:
                        times.append(dt_utc)
            except (ValueError, AttributeError):
                continue

        # Pattern 2: 24-hour format HH:MM (fallback)
        time_matches = re.findall(r">\s*(\d{1,2}:\d{2})\s*<", row_html)
        for time_str in time_matches:
            # Skip if already matched by AM/PM pattern
            if re.search(rf"{re.escape(time_str)}\s*[AP]M", row_html, re.IGNORECASE):
                continue
            try:
                hour, minute = map(int, time_str.split(":"))
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    # Create datetime in Beijing timezone
                    dt_beijing = datetime(
                        target_date.year,
                        target_date.month,
                        target_date.day,
                        hour,
                        minute,
                        tzinfo=beijing_tz,
                    )
                    # Convert to UTC for storage
                    dt_utc = dt_beijing.astimezone(UTC)
                    if dt_utc not in times:
                        times.append(dt_utc)
            except (ValueError, AttributeError):
                continue

        # Pattern 3: Unix timestamp in data attributes
        timestamp_matches = re.findall(r'data-timestamp="(\d{10,13})"', row_html)
        for ts_str in timestamp_matches:
            try:
                ts = int(ts_str)
                if ts > 1e12:
                    ts = ts // 1000
                dt = datetime.fromtimestamp(ts, tz=UTC)
                if dt not in times:
                    times.append(dt)
            except (ValueError, OSError):
                continue

        # Pattern 4: ISO format datetime
        # FR24 displays times in local airport timezone, so treat ISO times as Beijing time
        iso_matches = re.findall(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)", row_html)
        for iso_str in iso_matches:
            try:
                iso_str = iso_str.replace(" ", "T")
                dt_naive = datetime.fromisoformat(iso_str)
                # Treat as Beijing local time and convert to UTC
                dt_beijing = dt_naive.replace(tzinfo=beijing_tz)
                dt_utc = dt_beijing.astimezone(UTC)
                if dt_utc not in times:
                    times.append(dt_utc)
            except ValueError:
                continue

        # Sort times chronologically
        times.sort()

        # Assign times: first is scheduled, second is actual (for landed flights)
        scheduled_time: datetime | None = None
        estimated_time: datetime | None = None
        actual_time: datetime | None = None

        if len(times) >= 1:
            # The clock icon time (scheduled) usually comes first in HTML
            scheduled_time = times[0]
        if len(times) >= 2:
            # Second time is typically actual/estimated
            actual_time = times[1]

        return scheduled_time, estimated_time, actual_time


class FR24AirportArrivalsScraper(_FR24AirportScraper):
    """Scraper for FR24 airport arrivals.

    Scrapes arrival flight information from Flightradar24's airport arrivals page.

    Example usage:
        queue.add_task(
            task_type="fr24_arrivals",
            task_key="PKX",
            payload={"max_clicks": 15}
        )
    """

    task_type = "fr24_arrivals"
    flight_type = "arrival"
    url_suffix = "arrivals"


class FR24AirportDeparturesScraper(_FR24AirportScraper):
    """Scraper for FR24 airport departures.

    Scrapes departure flight information from Flightradar24's airport departures page.

    Example usage:
        queue.add_task(
            task_type="fr24_departures",
            task_key="PKX",
            payload={"max_clicks": 15}
        )
    """

    task_type = "fr24_departures"
    flight_type = "departure"
    url_suffix = "departures"


class FR24AirportScraper(_FR24AirportScraper):
    """Combined scraper for FR24 airport arrivals and departures.

    Scrapes both arrival and departure flight information in a single task.
    This reduces task count and browser switching overhead.

    Example usage:
        queue.add_task(
            task_type="fr24_airport",
            task_key="PKX",
            payload={"max_clicks": 50}
        )
    """

    task_type = "fr24_airport"
    flight_type = ""  # Set dynamically during scraping
    url_suffix = ""  # Set dynamically during scraping

    def scrape(self, task: ScraperTask, browser: Any | None = None) -> FR24FlightsResult:
        """Scrape both arrivals and departures for an airport.

        Args:
            task: Task with airport code as task_key.
            browser: DrissionPage browser instance.

        Returns:
            FR24FlightsResult with combined flight data.

        Raises:
            ScraperError: If scraping fails.
            NoDataFoundError: If neither arrivals nor departures found.
        """
        airport_code = task.task_key.strip().upper()
        all_flights: list[FlightData] = []
        airport_name = ""
        load_more_clicks = 0
        arrivals_count = 0
        departures_count = 0

        # Scrape arrivals
        try:
            self.flight_type = "arrival"
            self.url_suffix = "arrivals"
            arrivals_result = super().scrape(task, browser)
            all_flights.extend(arrivals_result.flights)
            arrivals_count = len(arrivals_result.flights)
            airport_name = arrivals_result.airport_name
            load_more_clicks += arrivals_result.load_more_clicks
            logger.info(f"[{airport_code}] Scraped {arrivals_count} arrivals")
        except NoDataFoundError:
            logger.info(f"[{airport_code}] No arrivals found, continuing with departures")

        # Scrape departures
        try:
            self.flight_type = "departure"
            self.url_suffix = "departures"
            departures_result = super().scrape(task, browser)
            all_flights.extend(departures_result.flights)
            departures_count = len(departures_result.flights)
            if not airport_name:
                airport_name = departures_result.airport_name
            load_more_clicks += departures_result.load_more_clicks
            logger.info(f"[{airport_code}] Scraped {departures_count} departures")
        except NoDataFoundError:
            logger.info(f"[{airport_code}] No departures found")

        # Raise if both are empty
        if not all_flights:
            raise NoDataFoundError(task_key=task.task_key)

        # Calculate combined date range
        flight_times = [f.scheduled_time for f in all_flights if f.scheduled_time]
        date_range_start = min(flight_times) if flight_times else None
        date_range_end = max(flight_times) if flight_times else None

        logger.info(
            f"[{airport_code}] Total: {len(all_flights)} flights "
            f"({arrivals_count} arrivals, {departures_count} departures)"
        )

        return FR24FlightsResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            airport_code=airport_code,
            airport_name=airport_name,
            flight_type="both",
            flights=all_flights,
            flights_count=len(all_flights),
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            load_more_clicks=load_more_clicks,
        )
