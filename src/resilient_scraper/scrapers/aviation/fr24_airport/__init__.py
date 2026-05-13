"""FR24 airport arrivals/departures scrapers."""

from resilient_scraper.scrapers.aviation.fr24_airport.models import (
    FlightData,
    FR24FlightsResult,
)
from resilient_scraper.scrapers.aviation.fr24_airport.scraper import (
    FR24AirportArrivalsScraper,
    FR24AirportDeparturesScraper,
    FR24AirportScraper,
)

__all__ = [
    "FR24AirportArrivalsScraper",
    "FR24AirportDeparturesScraper",
    "FR24AirportScraper",
    "FR24FlightsResult",
    "FlightData",
]
