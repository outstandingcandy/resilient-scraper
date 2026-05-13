"""FR24 aircraft schedule scraper — flight history by registration."""

from resilient_scraper.scrapers.aviation.fr24_aircraft.models import (
    FlightData,
    FR24AircraftResult,
)
from resilient_scraper.scrapers.aviation.fr24_aircraft.scraper import FR24AircraftScraper

__all__ = ["FlightData", "FR24AircraftResult", "FR24AircraftScraper"]
