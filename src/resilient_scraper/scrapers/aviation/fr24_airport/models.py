"""Pydantic result models for the FR24 airport arrivals/departures scrapers."""

from datetime import datetime

from pydantic import Field

from resilient_scraper.models import ScraperResult
from resilient_scraper.scrapers.aviation.fr24_aircraft.models import FlightData


class FR24FlightsResult(ScraperResult):
    """Result from FR24 arrivals/departures scraper.

    Attributes:
        airport_code: Airport code used for scraping.
        airport_name: Full name of the airport.
        flight_type: Type of flights ('arrival' or 'departure').
        flights: List of extracted flight records.
        flights_count: Number of flights extracted.
        date_range_start: Earliest flight time in the data.
        date_range_end: Latest flight time in the data.
        load_more_clicks: Number of pagination clicks performed.
    """

    airport_code: str = ""
    airport_name: str = ""
    flight_type: str = ""  # "arrival" or "departure"
    flights: list[FlightData] = Field(default_factory=list)
    flights_count: int = 0
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    load_more_clicks: int = 0


__all__ = ["FR24FlightsResult", "FlightData"]
