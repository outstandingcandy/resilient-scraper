"""
Pydantic models for the Planespotters scraper.

Defines data structures for aircraft data and scraper results.
"""

from pydantic import BaseModel, Field

from resilient_scraper.models import ScraperResult


class PlanespottersAircraftData(BaseModel):
    """Aircraft data extracted from Planespotters production list page.

    Attributes:
        registration: Aircraft registration number (e.g., "N747AF").
        serial_number: Construction/serial number.
        aircraft_type: ICAO type code (e.g., "B744").
        manufacturer: Manufacturer name (e.g., "Boeing").
        model: Full model name (e.g., "747-400").
        operator: Current operator name.
        delivery_date: Delivery date if available.
        status: Aircraft status (Active, Stored, Scrapped).
        source_url: Production list page URL.
    """

    registration: str
    serial_number: str | None = None
    aircraft_type: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    operator: str | None = None
    delivery_date: str | None = None
    status: str | None = None
    source_url: str | None = None


class PlanespottersResult(ScraperResult):
    """Result from Planespotters scraper.

    Attributes:
        scrape_mode: Scrape mode ("families" or "production_list").
        family_name: Aircraft family name (e.g., "boeing-747").
        family_urls: List of family URLs (for families mode).
        aircraft: List of extracted aircraft data.
        aircraft_count: Number of aircraft found.
        pages_scraped: Number of pages scraped.
        records_updated: Number of planespotters_aircraft records updated.
        s3_paths: List of S3 paths for uploaded HTML files.
    """

    scrape_mode: str = "production_list"
    family_name: str = ""
    family_urls: list[str] = Field(default_factory=list)
    aircraft: list[PlanespottersAircraftData] = Field(default_factory=list)
    aircraft_count: int = 0
    pages_scraped: int = 0
    records_updated: int = 0
    s3_paths: list[str] = Field(default_factory=list)
