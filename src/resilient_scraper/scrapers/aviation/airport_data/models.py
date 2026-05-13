"""Pydantic result models for the airport-data.com scraper."""

from pydantic import BaseModel, Field

from resilient_scraper.models import ScraperResult


class AirportDataAircraftData(BaseModel):
    """Aircraft data from airport-data.com."""

    registration: str
    year_built: int | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    engines: int | None = None
    seats: int | None = None
    location: str | None = None
    owner: str | None = None
    status: str | None = None
    mode_s_code: str | None = None
    delivery_date: str | None = None
    source_url: str | None = None


class AirportDataResult(ScraperResult):
    """Result from AirportData scraper."""

    scrape_mode: str = "manufacturer"
    manufacturer_name: str = ""
    manufacturer_urls: list[str] = Field(default_factory=list)
    aircraft: list[AirportDataAircraftData] = Field(default_factory=list)
    aircraft_count: int = 0
    pages_scraped: int = 0
    records_updated: int = 0
    s3_paths: list[str] = Field(default_factory=list)
