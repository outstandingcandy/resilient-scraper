"""Pydantic result models for the ADS-B Exchange globe scraper."""

from datetime import datetime

from pydantic import BaseModel, Field

from resilient_scraper.models import ScraperResult


class ADSBxMapAircraftData(BaseModel):
    """A single aircraft row from an ADS-B Exchange tar1090 feed.

    Field names mirror the tar1090 JSON shape so the parser stays mechanical.
    """

    hex: str
    flight: str | None = None
    registration: str | None = None
    aircraft_type: str | None = None
    type_description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude_baro: int | None = None
    altitude_geom: int | None = None
    ground_speed: float | None = None
    track: float | None = None
    heading: float | None = None
    vertical_rate: int | None = None
    squawk: str | None = None
    category: str | None = None
    emergency: str | None = None
    db_flags: int | None = None
    # Military bit is dbFlags & 1 — cached as a top-level bool for quick filters.
    mil: bool = False
    on_ground: bool = False
    country: str | None = None
    seen_pos: float | None = None
    messages: int | None = None
    rssi: float | None = None
    timestamp: datetime | None = None


class ADSBxMapResult(ScraperResult):
    """Result from one ADS-B Exchange globe scrape."""

    center_lat: float = 0.0
    center_lon: float = 0.0
    zoom_level: int = 4
    bounds: dict[str, float] = Field(default_factory=dict)
    aircraft: list[ADSBxMapAircraftData] = Field(default_factory=list)
    aircraft_count: int = 0
    military_count: int = 0
    feed_generated_at: datetime | None = None
    scraped_at: datetime | None = None
