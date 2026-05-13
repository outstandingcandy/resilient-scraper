"""Pydantic result models for the FR24 map scraper."""

from datetime import datetime

from pydantic import BaseModel, Field

from resilient_scraper.models import ScraperResult


class FR24MapAircraftData(BaseModel):
    """Individual aircraft data from FR24 map.

    Attributes:
        fr24_id: FR24 internal aircraft/flight ID.
        flight_number: Flight number (e.g., 'CA123').
        callsign: Aircraft callsign.
        registration: Aircraft registration number.
        aircraft_type: ICAO aircraft type code (e.g., 'A320', 'B738').
        latitude: Current latitude.
        longitude: Current longitude.
        altitude: Current altitude in feet.
        ground_speed: Ground speed in knots.
        heading: Heading in degrees.
        vertical_speed: Vertical speed in feet per minute.
        squawk: Transponder squawk code.
        origin_iata: Origin airport IATA code.
        destination_iata: Destination airport IATA code.
        airline_iata: Airline IATA code.
        airline_name: Airline name.
        on_ground: Whether aircraft is on ground.
        timestamp: Data timestamp from FR24.
    """

    fr24_id: str | None = None
    flight_number: str | None = None
    callsign: str | None = None
    registration: str | None = None
    aircraft_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: int | None = None
    ground_speed: int | None = None
    heading: int | None = None
    vertical_speed: int | None = None
    squawk: str | None = None
    origin_iata: str | None = None
    destination_iata: str | None = None
    airline_iata: str | None = None
    airline_name: str | None = None
    on_ground: bool = False
    timestamp: datetime | None = None


class FR24MapResult(ScraperResult):
    """Result from FR24 map scraper.

    Attributes:
        center_lat: Center latitude of the map view.
        center_lon: Center longitude of the map view.
        zoom_level: Map zoom level.
        bounds: Map bounds (north, south, east, west).
        aircraft: List of aircraft in view.
        aircraft_count: Number of aircraft found.
        scraped_at: When the data was scraped.
    """

    center_lat: float = 0.0
    center_lon: float = 0.0
    zoom_level: int = 4
    bounds: dict[str, float] = Field(default_factory=dict)
    aircraft: list[FR24MapAircraftData] = Field(default_factory=list)
    aircraft_count: int = 0
    scraped_at: datetime | None = None
