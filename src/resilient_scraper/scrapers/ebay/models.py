"""
Pydantic models for the eBay seller storefront scraper.

Defines data structures for listings and scraper results.
"""

from pydantic import BaseModel, Field

from resilient_scraper.models import ScraperResult


class EbayListing(BaseModel):
    """A single listing from an eBay seller storefront.

    Attributes:
        listing_id: eBay item/listing ID (numeric string).
        seller_username: Username of the storefront owner.
        title: Listing title.
        url: Canonical `/itm/{id}` URL.
        image_url: Primary product image URL.
        price: Numeric price value (currency-agnostic).
        currency: ISO currency code (e.g., USD).
        display_price: Display-formatted price string (e.g., "$19.99").
        condition: Normalized condition label ("Brand New", "Pre-Owned", etc).
        quantity: Remaining inventory count, parsed from "N remaining".
        shipping_cost: Shipping string (e.g., "+$7.40 delivery").
        brand: First displayAttributes entry (e.g., "Matchbox").
        source_url: Page URL this listing was scraped from.
    """

    listing_id: str
    seller_username: str
    title: str | None = None
    url: str | None = None
    image_url: str | None = None
    price: float | None = None
    currency: str | None = None
    display_price: str | None = None
    condition: str | None = None
    quantity: int | None = None
    shipping_cost: str | None = None
    brand: str | None = None
    source_url: str | None = None


class EbayStoreResult(ScraperResult):
    """Result from the eBay store scraper.

    Attributes:
        seller_username: The seller slug that was scraped.
        listings: All listings collected across pages.
        listings_count: Number of listings collected.
        pages_scraped: Number of pages successfully fetched.
        records_updated: Number of DB rows upserted.
        total_items: Seller's total item count, parsed from page 1 header.
        s3_paths: Local or S3 paths of saved HTML dumps.
        login_required: Whether a bot challenge was hit.
        login_screenshot_path: Path to challenge screenshot if captured.
    """

    seller_username: str = ""
    listings: list[EbayListing] = Field(default_factory=list)
    listings_count: int = 0
    pages_scraped: int = 0
    records_updated: int = 0
    total_items: int | None = None
    s3_paths: list[str] = Field(default_factory=list)
    login_required: bool = False
    login_screenshot_path: str | None = None
