"""Pydantic result models for the JetPhotos scraper."""

from pydantic import BaseModel, Field

from resilient_scraper.models import ScraperResult


class ImageMetadata(BaseModel):
    """Metadata for a single aircraft image."""

    image_path: str
    source_url: str | None = None
    jetphotos_id: str | None = None
    photographer: str | None = None
    photo_date: str | None = None  # ISO date string
    upload_date: str | None = None  # ISO date string
    location: str | None = None
    airport_icao: str | None = None
    airport_name: str | None = None
    file_size_bytes: int | None = None
    notes: str | None = None
    camera: str | None = None
    views: int | None = None
    likes: int | None = None
    badges: str | None = None
    html_s3_path: str | None = None


class JetPhotosResult(ScraperResult):
    """Result from JetPhotos scraper."""

    registration: str = ""
    image_paths: list[str] = Field(default_factory=list)
    image_count: int = 0
    s3_uploaded: bool = False
    images_metadata: list[ImageMetadata] = Field(default_factory=list)
