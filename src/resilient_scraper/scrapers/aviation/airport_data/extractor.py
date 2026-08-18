"""
Airport-data.com field extractor.

Extracts aircraft data from airport-data.com pages, including aircraft details,
manufacturer information, and ownership data.
"""

import logging
import re
from typing import Any

from resilient_scraper.extractors.base import BaseExtractor

logger = logging.getLogger("resilient_scraper.scrapers.airport_data.extractor")

# No `www.`: the site's certificate lists `airport-data.com` as its only subject
# alternative name, so `https://www.airport-data.com/...` fails verification.
BASE_URL = "https://airport-data.com"


def aircraft_detail_url(registration: str) -> str:
    """Return the canonical detail-page URL for `registration`.

    Extension-less on purpose: the site now redirects the older
    ``/aircraft/<reg>.html`` form here, and following that redirect costs a
    round trip on every aircraft we scrape.

    Args:
        registration: Aircraft registration, e.g. ``"N703PA"``.

    Returns:
        Absolute URL of the aircraft's detail page.
    """
    return f"{BASE_URL}/aircraft/{registration}"


def first_record_html(html: str) -> str:
    """Narrow a detail page to the first aircraft record it contains.

    A registration can be reused, and the site then renders every airframe that
    ever wore it on one page — N703PA carries both a 1999 Cessna 208B and a 1959
    Boeing 707. Each record sits in its own ``<div id="aircraftNNN">`` card, and
    the first one is the current airframe. Extracting from the whole page mixed
    the two: fields missing from the first record were filled in from the second.

    Args:
        html: Full HTML of an aircraft detail page.

    Returns:
        The first record's HTML, or `html` unchanged when the page holds a single
        record or the card markup isn't recognised.
    """
    starts = [m.start() for m in re.finditer(r'<div[^>]*\sid="aircraft\d+"', html)]
    if len(starts) < 2:
        return html
    return html[starts[0] : starts[1]]


class AirportDataExtractor(BaseExtractor):
    """Extractor for airport-data.com aircraft detail pages.

    Extracts the following fields:
    - registration: Aircraft registration number
    - year_built: Year the aircraft was built
    - manufacturer: Aircraft manufacturer name
    - model: Aircraft model name
    - serial_number: Construction/serial number (C/N)
    - engines: Number of engines
    - seats: Seat count
    - location: Current location
    - owner: Aircraft owner
    - status: Aircraft status (Active, De-registered, etc.)
    - mode_s_code: Mode S transponder code
    - delivery_date: Delivery date

    Context keys:
    - registration: Aircraft registration (required for identification)
    - source_url: URL of the source page
    """

    @property
    def version(self) -> str:
        """Extractor version."""
        return "1.1.0"

    def extract(self, html: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Extract aircraft data from an airport-data.com detail page.

        Args:
            html: HTML content of the aircraft detail page.
            context: Optional context with "registration" key.

        Returns:
            Dictionary with extracted aircraft data fields.
        """
        context = context or {}
        registration = context.get("registration", "")
        source_url = context.get("source_url", "")

        # If no source_url provided, construct it from registration
        if not source_url and registration:
            source_url = aircraft_detail_url(registration)

        # Initialize data
        data: dict[str, Any] = {
            "registration": registration,
            "source_url": source_url,
            "year_built": None,
            "manufacturer": None,
            "model": None,
            "serial_number": None,
            "engines": None,
            "seats": None,
            "location": None,
            "owner": None,
            "status": None,
            "mode_s_code": None,
            "delivery_date": None,
        }

        # Check if page exists
        if "not found" in html.lower() or "no data" in html.lower():
            return data

        # Scope to the current airframe before parsing; see first_record_html.
        record_html = first_record_html(html)

        # Extract data using table row pattern
        self._extract_from_table_rows(record_html, data)

        # Try alternative extraction patterns if table parsing missed fields
        self._extract_from_dl_lists(record_html, data)

        return data

    def _extract_from_table_rows(self, html: str, data: dict[str, Any]) -> None:
        """Extract data from table rows with label/value pairs.

        Args:
            html: HTML content of the page.
            data: Dictionary to update with extracted data.
        """
        # Pattern for airport-data.com format:
        # <tr><td class="ac_property_title"><b>Label:</b></td><td>Value</td></tr>
        row_pattern = (
            r"<tr[^>]*>\s*<td[^>]*>(?:<b>)?([^<]+?)(?:</b>)?:?\s*</td>\s*<td[^>]*>(.*?)</td>"
        )
        rows = re.findall(row_pattern, html, re.IGNORECASE | re.DOTALL)

        for label, value in rows:
            label = label.strip().lower().rstrip(":")
            # Clean value - remove HTML tags and extra whitespace
            value = re.sub(r"<[^>]+>", "", value)
            value = re.sub(r"&nbsp;", " ", value)
            value = re.sub(r"\s+", " ", value).strip()
            # Drop the trailing "Search all <make> <model>" navigation link that
            # shares the cell with the value. Collapse whitespace first: the raw
            # cell spreads the link over several lines, and `.*$` without DOTALL
            # stops at the first newline, which left the text in place.
            value = re.sub(r"\s*Search all\s+\S.*$", "", value, flags=re.IGNORECASE).strip()

            if not value:
                continue

            self._map_field(label, value, data)

    def _extract_from_dl_lists(self, html: str, data: dict[str, Any]) -> None:
        """Extract data from definition lists (<dl>/<dt>/<dd>).

        Args:
            html: HTML content of the page.
            data: Dictionary to update with extracted data.
        """
        # Pattern for definition lists
        dt_dd_pattern = r"<dt[^>]*>([^<]+)</dt>\s*<dd[^>]*>([^<]*(?:<[^>]+>[^<]*)*)</dd>"
        items = re.findall(dt_dd_pattern, html, re.IGNORECASE | re.DOTALL)

        for label, value in items:
            label = label.strip().lower()
            value = re.sub(r"<[^>]+>", "", value).strip()

            if not value:
                continue

            self._map_field(label, value, data)

    def _map_field(self, label: str, value: str, data: dict[str, Any]) -> None:
        """Map a label/value pair to the appropriate data field.

        Every field is first-write-wins. One detail page can carry several
        aircraft that shared a registration over time — N703PA lists both a 1999
        Cessna 208B and a 1959 Boeing 707 — and the first block on the page is
        the current one. Letting a later block overwrite produced records that
        mixed the Cessna's identity with the Boeing's year, engine count and
        seating.

        Args:
            label: Field label (lowercase).
            value: Field value.
            data: Dictionary to update with extracted data.
        """
        if "year" in label or "built" in label:
            if value.isdigit() and not data.get("year_built"):
                year_val = int(value)
                if 1900 < year_val <= 2100:
                    data["year_built"] = year_val
        elif "manufacturer" in label or "maker" in label:
            if not data.get("manufacturer"):
                data["manufacturer"] = value
        elif "model" in label or "type" in label:
            # Avoid overwriting model with generic "type" values
            if not data.get("model") and value.lower() not in ["aircraft", "airplane"]:
                data["model"] = value
        elif "serial" in label or "c/n" in label or "cn" in label:
            if not data.get("serial_number"):
                data["serial_number"] = value
        elif "engine" in label:
            if value.isdigit() and not data.get("engines"):
                data["engines"] = int(value)
        elif "seat" in label:
            if value.isdigit() and not data.get("seats"):
                data["seats"] = int(value)
        elif "owner" in label or "operator" in label:
            if not data.get("owner"):
                data["owner"] = value
        elif "status" in label:
            if not data.get("status"):
                data["status"] = value
        elif "mode s" in label or "modes" in label or "icao24" in label:
            if not data.get("mode_s_code"):
                # Mode S codes are typically 6 hex characters
                cleaned = value.upper().strip()
                if re.match(r"^[0-9A-F]{6}$", cleaned):
                    data["mode_s_code"] = cleaned
                elif re.match(r"^[0-7]{8}$", cleaned):
                    # Convert 8-digit octal to 6-digit hex
                    try:
                        hex_val = format(int(cleaned, 8), "06X")
                        data["mode_s_code"] = hex_val
                    except ValueError:
                        pass  # Skip invalid octal values
        elif "delivery" in label:
            if not data.get("delivery_date"):
                data["delivery_date"] = value
        elif "location" in label:
            if not data.get("location"):
                data["location"] = value

    def extract_from_table_page(
        self, html: str, manufacturer: str | None = None
    ) -> list[dict[str, Any]]:
        """Extract aircraft list from a manufacturer page table.

        This method parses the aircraft listing table that appears on
        manufacturer pages (e.g., /manuf/Cessna.html).

        Args:
            html: HTML content of the manufacturer page.
            manufacturer: Optional manufacturer name to use as default.

        Returns:
            List of aircraft data dictionaries.
        """
        aircraft_list: list[dict[str, Any]] = []

        # Find table rows
        row_pattern = r"<tr[^>]*>(.*?)</tr>"
        rows = re.findall(row_pattern, html, re.IGNORECASE | re.DOTALL)

        for row_html in rows:
            # Extract cells
            cell_pattern = r"<td[^>]*>(.*?)</td>"
            cells = re.findall(cell_pattern, row_html, re.IGNORECASE | re.DOTALL)

            if len(cells) < 5:
                continue

            # Look for registration link in first cell
            reg_match = re.search(r'href="[^"]*">([A-Z0-9-]+)</a>', cells[0], re.IGNORECASE)
            if not reg_match:
                continue

            registration = reg_match.group(1).strip()
            if not registration:
                continue

            aircraft = self._parse_table_row_cells(cells, registration, manufacturer)
            if aircraft:
                aircraft_list.append(aircraft)

        return aircraft_list

    def _parse_table_row_cells(
        self, cells: list[str], registration: str, manufacturer: str | None
    ) -> dict[str, Any] | None:
        """Parse a table row into aircraft data.

        Args:
            cells: List of cell HTML contents.
            registration: Aircraft registration.
            manufacturer: Default manufacturer name.

        Returns:
            Aircraft data dictionary or None if parsing fails.
        """

        # Clean cell contents
        def clean_cell(cell: str) -> str:
            return re.sub(r"<[^>]+>", "", cell).strip()

        # Actual columns: Tail Number, Year Maker Model (combined), C/N, Engines, Seats, Location
        year_maker_model = clean_cell(cells[1]) if len(cells) > 1 else ""
        cn = clean_cell(cells[2]) if len(cells) > 2 else ""
        engines_text = clean_cell(cells[3]) if len(cells) > 3 else ""
        seats_text = clean_cell(cells[4]) if len(cells) > 4 else ""
        location = clean_cell(cells[5]) if len(cells) > 5 else ""

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
                    if 1900 < year_val <= 2100:
                        year_built = year_val
                    # Rest is "Maker Model"
                    if len(parts) > 1:
                        maker = parts[1].strip()
                else:
                    # No year, entire string is maker model
                    maker = year_maker_model

        # Parse numeric values
        engines = None
        if engines_text and engines_text.isdigit():
            engines = int(engines_text)

        seats = None
        if seats_text and seats_text.isdigit():
            seats = int(seats_text)

        return {
            "registration": registration,
            "year_built": year_built,
            "manufacturer": maker if maker else manufacturer,
            "model": model,
            "serial_number": cn if cn else None,
            "engines": engines,
            "seats": seats,
            "location": location if location else None,
            "source_url": aircraft_detail_url(registration),
        }
