"""
FR24 Map scraper implementation.

Downloads real-time aircraft positions from Flightradar24 map view.
Scrapes aircraft visible on the map at a given location and zoom level.

DB persistence is delegated to the calling application via
``scraper.on_success``. This scraper returns structured Pydantic results and
does not touch any application-owned tables.
"""

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import requests

from resilient_scraper.errors import PageLoadError, ScraperError
from resilient_scraper.models import ScraperTask
from resilient_scraper.scraper import ResilientScraper
from resilient_scraper.scrapers.aviation.fr24_map.models import (
    FR24MapAircraftData,
    FR24MapResult,
)

logger = logging.getLogger("resilient_scraper.scrapers.fr24_map")

# The feed endpoint answers 403 to a request with no User-Agent, so this is
# required rather than cosmetic.
_API_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class FR24MapScraper(ResilientScraper[FR24MapResult]):
    """Scraper for FR24 map view aircraft positions.

    This scraper loads the FR24 map page and extracts aircraft data
    either from the page's JavaScript state or by intercepting API calls.

    Configuration options (in scraper config):
        wait_for_load: Seconds to wait for map to fully load (default: 15).
        save_debug_html: Whether to save HTML for debugging (default: False).
        api_attempts: Tries against the feed API before giving up on it and
            falling back to the map page (default: 4). See _fetch_from_api
            for why more than one is needed.
        api_retry_delay: Seconds between those attempts (default: 1.5).
        api_timeout: Per-request timeout in seconds (default: 20).

    Task payload options:
        lat: Center latitude (required).
        lon: Center longitude (required).
        zoom: Zoom level (default: 4).

    Example usage:
        queue.add_task(
            task_type="fr24_map",
            task_key="beijing_area",
            payload={"lat": 37.09, "lon": 116.62, "zoom": 4}
        )
    """

    task_type = "fr24_map"
    default_delay = (10.0, 20.0)
    requires_browser = True
    cloudflare_protected = True
    task_timeout = 300  # 5 minutes — single map page load

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the FR24 map scraper.

        Args:
            config: Scraper configuration dictionary.
        """
        super().__init__(config)
        self.wait_for_load = self.config.get("wait_for_load", 15)
        self.save_debug_html = self.config.get("save_debug_html", True)
        # See _fetch_from_api: the feed endpoint answers with an empty stub
        # about 60% of the time, so one attempt is not enough to trust a
        # "no aircraft" answer.
        self.api_attempts = int(self.config.get("api_attempts", 4))
        self.api_retry_delay = float(self.config.get("api_retry_delay", 1.5))
        self.api_timeout = float(self.config.get("api_timeout", 20))

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that the task can be processed.

        Args:
            task: The task to validate.

        Returns:
            True if valid, False otherwise.
        """
        payload = task.payload or {}

        # Check for required coordinates
        lat = payload.get("lat")
        lon = payload.get("lon")

        if lat is None or lon is None:
            return False

        # Validate latitude range (-90 to 90)
        try:
            lat = float(lat)
            if lat < -90 or lat > 90:
                return False
        except (ValueError, TypeError):
            return False

        # Validate longitude range (-180 to 180)
        try:
            lon = float(lon)
            if lon < -180 or lon > 180:
                return False
        except (ValueError, TypeError):
            return False

        return True

    def build_url(self, task: ScraperTask) -> str:
        """Build the FR24 map URL.

        Args:
            task: The task with coordinates in payload.

        Returns:
            FR24 map URL.
        """
        payload = task.payload or {}
        lat = payload.get("lat", 37.09)
        lon = payload.get("lon", 116.62)
        zoom = payload.get("zoom", 4)

        return f"https://www.flightradar24.com/{lat},{lon}/{zoom}"

    def scrape(self, task: ScraperTask, browser: Any | None = None) -> FR24MapResult:
        """Scrape aircraft data from FR24 map view.

        Args:
            task: Task with coordinates in payload.
            browser: DrissionPage browser instance.

        Returns:
            FR24MapResult with extracted aircraft data.

        Raises:
            ScraperError: If scraping fails.
        """
        if browser is None:
            raise ScraperError(
                "Browser required for FR24 map scraper",
                task_key=task.task_key,
                retryable=False,
            )

        payload = task.payload or {}
        lat = float(payload.get("lat", 37.09))
        lon = float(payload.get("lon", 116.62))
        zoom = int(payload.get("zoom", 4))

        # Calculate bounds from center and zoom
        # At zoom level 4, roughly 45 degrees lat/lon span
        # Scale factor: ~180 / (2^zoom)
        span = 180 / (2**zoom)
        bounds = {
            "north": lat + span / 2,
            "south": lat - span / 2,
            "west": lon - span / 2,
            "east": lon + span / 2,
        }

        # First, try to get data directly from FR24 API
        api_aircraft = self._fetch_from_api(task.task_key, bounds)
        if api_aircraft:
            logger.info(f"[{task.task_key}] Found {len(api_aircraft)} aircraft from direct API")
            return FR24MapResult(
                success=True,
                task_key=task.task_key,
                task_type=self.task_type,
                center_lat=lat,
                center_lon=lon,
                zoom_level=zoom,
                bounds=bounds,
                aircraft=api_aircraft,
                aircraft_count=len(api_aircraft),
                scraped_at=datetime.now(UTC),
            )

        # Fallback: Visit map page and try to extract from page state
        url = self.build_url(task)
        logger.info(f"[{task.task_key}] Visiting: {url}")

        try:
            browser.get(url)
        except Exception as e:
            error_msg = str(e).lower()
            if "timeout" in error_msg or "timed out" in error_msg:
                logger.error(f"[{task.task_key}] Page load timeout: {e}")
                raise PageLoadError(url, task_key=task.task_key)
            raise

        # Wait for initial load
        time.sleep(self.wait_for_load)

        # Handle cookie consent dialog if present
        self._dismiss_cookie_consent(browser, task.task_key)

        # Handle Cloudflare if needed
        html = browser.html
        if "just a moment" in html.lower() or "checking your browser" in html.lower():
            logger.info(f"[{task.task_key}] Cloudflare detected, waiting...")
            if not self.handle_cloudflare(browser):
                raise ScraperError(
                    "Cloudflare challenge failed",
                    task_key=task.task_key,
                    retryable=True,
                )
            time.sleep(5)

        # Wait a bit more for the map and aircraft to load
        time.sleep(10)

        # Get page HTML and try to extract aircraft data
        html = browser.html

        # Save debug HTML if enabled
        if self.save_debug_html:
            self._save_debug_files(browser, task.task_key, html)

        # Try multiple methods to extract aircraft data
        aircraft: list[FR24MapAircraftData] = []

        # Method 1: Try to extract from page's JavaScript state/window objects
        js_aircraft = self._extract_from_js_state(browser, task.task_key)
        if js_aircraft:
            aircraft.extend(js_aircraft)
            logger.info(f"[{task.task_key}] Found {len(js_aircraft)} aircraft from JS state")

        # Method 2: Try to extract from network requests (if Method 1 didn't work)
        if not aircraft:
            api_aircraft_fallback = self._extract_from_api_response(browser, task.task_key)
            if api_aircraft_fallback:
                aircraft.extend(api_aircraft_fallback)
                logger.info(
                    f"[{task.task_key}] Found {len(api_aircraft_fallback)} aircraft from API"
                )

        # Method 3: Try to parse aircraft icons from HTML/SVG
        if not aircraft:
            html_aircraft = self._extract_from_html(html, task.task_key)
            if html_aircraft:
                aircraft.extend(html_aircraft)
                logger.info(f"[{task.task_key}] Found {len(html_aircraft)} aircraft from HTML")

        if not aircraft:
            logger.warning(f"[{task.task_key}] No aircraft found")

        logger.info(f"[{task.task_key}] Total aircraft extracted: {len(aircraft)}")

        return FR24MapResult(
            success=True,
            task_key=task.task_key,
            task_type=self.task_type,
            center_lat=lat,
            center_lon=lon,
            zoom_level=zoom,
            bounds=bounds,
            aircraft=aircraft,
            aircraft_count=len(aircraft),
            scraped_at=datetime.now(UTC),
        )

    def _fetch_from_api(
        self, task_key: str, bounds: dict[str, float]
    ) -> list[FR24MapAircraftData]:
        """Fetch aircraft data directly from FR24's feed API.

        FR24 uses data-cloud.flightradar24.com/zones/fcgi/feed.js.

        Two things about this endpoint are load-bearing:

        * **A User-Agent is mandatory.** Without one it answers 403.
        * **It intermittently answers with an empty stub** — HTTP 200 whose
          body is just ``{"full_count": N, "version": 4}`` and not one
          aircraft, even for bounds that certainly contain traffic. Measured
          at roughly 40% good responses, independent of request spacing, so
          it reads as some edge nodes serving a stale cached payload rather
          than rate limiting. A single attempt therefore loses the data
          outright most of the time, which is why this retries.

        Retrying costs `api_attempts` requests for bounds that are genuinely
        empty (mid-ocean, say). That is the deliberate trade: a wasted
        request is cheap, and silently reporting "no aircraft" for a busy
        sector is not.

        This uses a plain HTTP request rather than the browser: the endpoint
        needs no session or cookies, and driving it through the browser also
        meant parsing Chrome's JSON-viewer DOM back into JSON.

        Args:
            task_key: Task key for logging.
            bounds: Geographic bounds (north, south, east, west).

        Returns:
            List of aircraft data. Empty if every attempt came back empty.
        """
        api_url = (
            f"https://data-cloud.flightradar24.com/zones/fcgi/feed.js?"
            f"bounds={bounds['north']:.2f},{bounds['south']:.2f},"
            f"{bounds['west']:.2f},{bounds['east']:.2f}"
            f"&faa=1&satellite=1&mlat=1&flarm=1&adsb=1&gnd=1&air=1"
            f"&vehicles=0&estimated=1&maxage=14400&gliders=1&stats=0"
        )
        logger.info(f"[{task_key}] Fetching from API: {api_url}")

        for attempt in range(1, self.api_attempts + 1):
            if attempt > 1:
                time.sleep(self.api_retry_delay)
            try:
                response = requests.get(
                    api_url,
                    headers={"User-Agent": _API_USER_AGENT},
                    timeout=self.api_timeout,
                )
            except requests.RequestException as e:
                logger.debug(f"[{task_key}] API request failed (attempt {attempt}): {e}")
                continue

            if response.status_code != 200:
                logger.debug(
                    f"[{task_key}] API returned HTTP {response.status_code} "
                    f"(attempt {attempt})"
                )
                continue

            try:
                data = response.json()
            except ValueError as e:
                logger.debug(
                    f"[{task_key}] API response was not JSON (attempt {attempt}): {e}"
                )
                if self.save_debug_html:
                    debug_path = f"/tmp/fr24_api_response_{task_key}.txt"
                    with open(debug_path, "w", encoding="utf-8") as f:
                        f.write(response.text[:10000])  # Save first 10k chars
                    logger.debug(f"[{task_key}] Saved API response to {debug_path}")
                continue

            aircraft = self._parse_flight_data(data, task_key)
            if aircraft:
                logger.info(
                    f"[{task_key}] API returned {len(aircraft)} aircraft "
                    f"(attempt {attempt})"
                )
                return aircraft

            logger.debug(
                f"[{task_key}] API returned an empty payload on attempt "
                f"{attempt}/{self.api_attempts} (full_count="
                f"{data.get('full_count') if isinstance(data, dict) else '?'})"
            )

        logger.info(
            f"[{task_key}] API returned 0 aircraft after {self.api_attempts} "
            f"attempts; falling back to the map page"
        )
        return []

    def _save_debug_files(self, browser: Any, task_key: str, html: str) -> None:
        """Save debug HTML and screenshot.

        Args:
            browser: Browser instance.
            task_key: Task key for filename.
            html: Page HTML content.
        """
        try:
            debug_path = f"/tmp/fr24_map_debug_{task_key}.html"
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info(f"[{task_key}] Saved debug HTML to {debug_path}")

            # Take screenshot
            try:
                screenshot_path = f"/tmp/fr24_map_screenshot_{task_key}.png"
                browser.get_screenshot(path=screenshot_path, full_page=False)
                logger.info(f"[{task_key}] Saved screenshot to {screenshot_path}")
            except Exception as e:
                logger.warning(f"[{task_key}] Failed to save screenshot: {e}")

        except Exception as e:
            logger.warning(f"[{task_key}] Failed to save debug files: {e}")

    def _extract_from_js_state(self, browser: Any, task_key: str) -> list[FR24MapAircraftData]:
        """Extract aircraft data from page's JavaScript state.

        FR24 stores flight data in JavaScript objects. We try to access them
        via browser's execute_script.

        Args:
            browser: Browser instance.
            task_key: Task key for logging.

        Returns:
            List of extracted aircraft data.
        """
        aircraft: list[FR24MapAircraftData] = []

        # Try different JS expressions to find aircraft data
        js_expressions = [
            # FR24's global flight data store
            "return window.flights || null",
            "return window.FRData || null",
            "return window.flightData || null",
            # Try accessing via FR24's internal API
            "return window.__INITIAL_STATE__ || null",
            "return window.__FR24_STATE__ || null",
            # Try to get data from the map instance
            "return window.mapInstance?.flights || null",
            # Check for data in localStorage/sessionStorage
            """
            try {
                const data = sessionStorage.getItem('fr24_flights');
                return data ? JSON.parse(data) : null;
            } catch(e) { return null; }
            """,
        ]

        for expr in js_expressions:
            try:
                result = browser.run_js(expr)
                if result:
                    logger.debug(f"[{task_key}] Found data with: {expr[:50]}...")
                    parsed = self._parse_flight_data(result, task_key)
                    if parsed:
                        aircraft.extend(parsed)
                        break
            except Exception as e:
                logger.debug(f"[{task_key}] JS expression failed: {e}")
                continue

        # Try to intercept network responses by checking performance entries
        try:
            network_data = browser.run_js("""
                const entries = performance.getEntries();
                const flightEntries = entries.filter(e =>
                    e.name.includes('/zones/') ||
                    e.name.includes('/flight/') ||
                    e.name.includes('flights.json')
                );
                return flightEntries.map(e => e.name);
            """)
            if network_data:
                logger.debug(f"[{task_key}] Found flight-related URLs: {network_data}")
        except Exception:
            pass

        return aircraft

    def _extract_from_api_response(self, browser: Any, task_key: str) -> list[FR24MapAircraftData]:
        """Try to extract aircraft by making direct API calls.

        FR24 uses a zones API that returns aircraft in specific geographic areas.

        Args:
            browser: Browser instance.
            task_key: Task key for logging.

        Returns:
            List of extracted aircraft data.
        """
        aircraft: list[FR24MapAircraftData] = []

        try:
            # FR24's API structure: /zones/fcgi/feed.js with bounds
            # We can try to fetch the current visible aircraft via JS
            api_result = browser.run_js("""
                return new Promise((resolve) => {
                    // Try to find any XHR/fetch responses in memory
                    // This is a best-effort approach
                    if (window._flightCache) {
                        resolve(window._flightCache);
                    } else {
                        resolve(null);
                    }
                });
            """)

            if api_result:
                parsed = self._parse_flight_data(api_result, task_key)
                if parsed:
                    aircraft.extend(parsed)

        except Exception as e:
            logger.debug(f"[{task_key}] API extraction failed: {e}")

        return aircraft

    def _extract_from_html(self, html: str, task_key: str) -> list[FR24MapAircraftData]:
        """Extract aircraft data from HTML/SVG elements.

        As a fallback, we can try to parse aircraft markers from the rendered HTML.

        Args:
            html: Page HTML content.
            task_key: Task key for logging.

        Returns:
            List of extracted aircraft data.
        """
        aircraft: list[FR24MapAircraftData] = []

        # Look for aircraft data embedded in HTML attributes
        # FR24 uses data attributes on aircraft icons
        patterns = [
            # Pattern for data-flight attribute
            r'data-flight="([^"]+)"',
            # Pattern for flight ID in various formats
            r'data-id="([a-f0-9]+)"',
            r'data-flightid="([^"]+)"',
            # Pattern for aircraft markers (SVG or canvas)
            r'class="[^"]*aircraft[^"]*"[^>]*data-([^=]+)="([^"]+)"',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches:
                logger.debug(f"[{task_key}] Found HTML match: {match}")

        # Try to extract JSON data embedded in scripts
        script_pattern = r"<script[^>]*>(.*?)</script>"
        scripts = re.findall(script_pattern, html, re.DOTALL)

        for script in scripts:
            # Look for flight data arrays/objects
            if "flight" in script.lower() and "{" in script:
                # Try to find JSON-like structures
                json_patterns = [
                    r'"flights"\s*:\s*(\{[^}]+\}|\[[^\]]+\])',
                    r'"aircraft"\s*:\s*(\{[^}]+\}|\[[^\]]+\])',
                    r"var flights\s*=\s*(\{[^;]+\}|\[[^\]]+\]);",
                ]
                for jp in json_patterns:
                    json_match = re.search(jp, script, re.IGNORECASE)
                    if json_match:
                        try:
                            data = json.loads(json_match.group(1))
                            parsed = self._parse_flight_data(data, task_key)
                            if parsed:
                                aircraft.extend(parsed)
                        except json.JSONDecodeError:
                            pass

        return aircraft

    def _parse_flight_data(self, data: Any, task_key: str) -> list[FR24MapAircraftData]:
        """Parse raw flight data into FR24MapAircraftData objects.

        FR24 API returns data in format:
        {
            "flight_id": [
                icao24, lat, lon, track, alt, speed, squawk, radar,
                aircraft_type, registration, timestamp, origin, destination,
                flight_number, on_ground, vertical_speed, callsign, ...
            ]
        }

        Args:
            data: Raw flight data (dict or list).
            task_key: Task key for logging.

        Returns:
            List of FR24MapAircraftData objects.
        """
        aircraft: list[FR24MapAircraftData] = []

        if not data:
            return aircraft

        # Handle dict format (most common from FR24 API)
        if isinstance(data, dict):
            for flight_id, flight_info in data.items():
                # Skip metadata keys
                if flight_id in ("full_count", "version", "stats", "selected"):
                    continue

                try:
                    ac = self._parse_single_flight(flight_id, flight_info)
                    if ac:
                        aircraft.append(ac)
                except Exception as e:
                    logger.debug(f"[{task_key}] Failed to parse flight {flight_id}: {e}")

        # Handle list format
        elif isinstance(data, list):
            for idx, flight_info in enumerate(data):
                try:
                    if isinstance(flight_info, dict):
                        ac = self._parse_flight_dict(flight_info)
                    else:
                        ac = self._parse_single_flight(str(idx), flight_info)
                    if ac:
                        aircraft.append(ac)
                except Exception as e:
                    logger.debug(f"[{task_key}] Failed to parse flight at index {idx}: {e}")

        return aircraft

    def _parse_single_flight(self, flight_id: str, flight_info: Any) -> FR24MapAircraftData | None:
        """Parse a single flight from FR24 array format.

        FR24 array indices (0-based):
        0: ICAO24 hex
        1: Latitude
        2: Longitude
        3: Heading/Track
        4: Altitude (feet)
        5: Ground speed (knots)
        6: Squawk
        7: Radar station
        8: Aircraft type (ICAO)
        9: Registration
        10: Timestamp
        11: Origin airport
        12: Destination airport
        13: Flight number/IATA
        14: On ground (0/1)
        15: Vertical speed (fpm)
        16: Callsign
        17: Unknown
        18: Airline ICAO

        Args:
            flight_id: FR24 flight ID.
            flight_info: Array of flight data.

        Returns:
            FR24MapAircraftData or None if parsing fails.
        """
        if not isinstance(flight_info, (list, tuple)):
            return None

        if len(flight_info) < 10:
            return None

        try:
            ac = FR24MapAircraftData(
                fr24_id=flight_id,
                latitude=float(flight_info[1]) if flight_info[1] else None,
                longitude=float(flight_info[2]) if flight_info[2] else None,
                heading=int(flight_info[3]) if flight_info[3] else None,
                altitude=int(flight_info[4]) if flight_info[4] else None,
                ground_speed=int(flight_info[5]) if flight_info[5] else None,
                squawk=str(flight_info[6]) if flight_info[6] else None,
                aircraft_type=str(flight_info[8])
                if len(flight_info) > 8 and flight_info[8]
                else None,
                registration=str(flight_info[9]).upper()
                if len(flight_info) > 9 and flight_info[9]
                else None,
            )

            # Additional fields if available
            if len(flight_info) > 10 and flight_info[10]:
                try:
                    ts = int(flight_info[10])
                    ac.timestamp = datetime.fromtimestamp(ts, tz=UTC)
                except (ValueError, OSError):
                    pass

            if len(flight_info) > 11 and flight_info[11]:
                ac.origin_iata = str(flight_info[11])

            if len(flight_info) > 12 and flight_info[12]:
                ac.destination_iata = str(flight_info[12])

            if len(flight_info) > 13 and flight_info[13]:
                ac.flight_number = str(flight_info[13])

            if len(flight_info) > 14:
                ac.on_ground = bool(flight_info[14])

            if len(flight_info) > 15 and flight_info[15]:
                ac.vertical_speed = int(flight_info[15])

            if len(flight_info) > 16 and flight_info[16]:
                ac.callsign = str(flight_info[16])

            return ac

        except (ValueError, TypeError, IndexError) as e:
            logger.debug(f"Failed to parse flight {flight_id}: {e}")
            return None

    def _parse_flight_dict(self, flight_info: dict) -> FR24MapAircraftData | None:
        """Parse a single flight from dict format.

        Args:
            flight_info: Dictionary with flight data.

        Returns:
            FR24MapAircraftData or None if parsing fails.
        """
        try:
            ac = FR24MapAircraftData(
                fr24_id=flight_info.get("id") or flight_info.get("flight_id"),
                flight_number=flight_info.get("flight") or flight_info.get("flight_number"),
                callsign=flight_info.get("callsign"),
                registration=flight_info.get("registration") or flight_info.get("reg"),
                aircraft_type=flight_info.get("aircraft_type") or flight_info.get("type"),
                latitude=flight_info.get("lat") or flight_info.get("latitude"),
                longitude=flight_info.get("lon") or flight_info.get("longitude"),
                altitude=flight_info.get("alt") or flight_info.get("altitude"),
                ground_speed=flight_info.get("speed") or flight_info.get("ground_speed"),
                heading=flight_info.get("track") or flight_info.get("heading"),
                vertical_speed=flight_info.get("vspeed") or flight_info.get("vertical_speed"),
                squawk=flight_info.get("squawk"),
                origin_iata=flight_info.get("origin") or flight_info.get("dep"),
                destination_iata=flight_info.get("destination") or flight_info.get("arr"),
                airline_iata=flight_info.get("airline"),
                on_ground=flight_info.get("on_ground", False),
            )
            return ac
        except Exception:
            return None

