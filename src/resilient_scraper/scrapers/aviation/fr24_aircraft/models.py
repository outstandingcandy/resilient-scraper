"""Pydantic result models for the FR24 aircraft schedule scraper."""

from datetime import datetime

from pydantic import BaseModel, Field

from resilient_scraper.models import ScraperResult


class FlightData(BaseModel):
    """Individual flight record (arrival or departure).

    Shared by FR24 aircraft and FR24 airport scrapers. Both produce flight rows
    with identical shape; aircraft scraper returns a flight history for one
    tail, airport scraper returns a flight list for one airport.
    """

    flight_type: str | None = None
    flight_number: str | None = None
    callsign: str | None = None
    airline_name: str | None = None
    airline_iata: str | None = None
    remote_airport_iata: str | None = None
    remote_airport_name: str | None = None
    aircraft_type: str | None = None
    aircraft_registration: str | None = None
    scheduled_time: datetime | None = None
    estimated_time: datetime | None = None
    actual_time: datetime | None = None
    status: str | None = None
    terminal: str | None = None
    gate: str | None = None
    flight_id: str | None = None


class FR24AircraftResult(ScraperResult):
    """Result from FR24 aircraft schedule scraper."""

    aircraft_registration: str = ""
    aircraft_type: str | None = None
    aircraft_model: str | None = None
    airline_name: str | None = None
    flights: list[FlightData] = Field(default_factory=list)
    flights_count: int = 0
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    load_more_clicks: int = 0
