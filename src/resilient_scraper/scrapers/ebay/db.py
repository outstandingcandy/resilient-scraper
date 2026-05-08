"""
Database operations for the eBay store scraper.

Owns the `ebay_listings` table — keyed by (listing_id, seller_username) —
and provides upsert + load-existing-ids helpers used by the scraper for
idempotent per-page writes and skip-existing filtering.
"""

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from resilient_scraper.scrapers.ebay.models import EbayListing

logger = logging.getLogger("scraper.ebay")


class EbayDB:
    """Database operations for eBay listings.

    Args:
        db_engine: SQLAlchemy database engine instance.
    """

    def __init__(self, db_engine: Engine | None) -> None:
        self.db_engine = db_engine

    def ensure_tables_exist(self) -> None:
        """Create ebay_listings table and indexes if they don't exist."""
        if not self.db_engine:
            return

        try:
            with self.db_engine.connect() as conn:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ebay_listings (
                        id BIGSERIAL PRIMARY KEY,
                        listing_id VARCHAR(30) NOT NULL,
                        seller_username VARCHAR(100) NOT NULL,
                        title TEXT,
                        url TEXT,
                        image_url TEXT,
                        price NUMERIC(12,2),
                        currency VARCHAR(10),
                        display_price VARCHAR(50),
                        condition VARCHAR(50),
                        quantity INTEGER,
                        shipping_cost VARCHAR(50),
                        brand VARCHAR(100),
                        source_url TEXT,
                        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(listing_id, seller_username)
                    )
                """))

                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_ebay_listings_seller
                    ON ebay_listings(seller_username)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_ebay_listings_brand
                    ON ebay_listings(brand)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_ebay_listings_updated
                    ON ebay_listings(updated_at)
                """))

                conn.commit()
                logger.info("ebay_listings table ready")
        except SQLAlchemyError as e:
            logger.error(f"Failed to create ebay_listings table: {e}")

    def load_existing_listing_ids(self, seller_username: str) -> set[str]:
        """Return the set of listing_ids already stored for `seller_username`."""
        if not self.db_engine:
            return set()

        try:
            with self.db_engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT listing_id FROM ebay_listings "
                        "WHERE seller_username = :s"
                    ),
                    {"s": seller_username},
                )
                ids = {row[0] for row in result}
                logger.info(
                    f"Loaded {len(ids)} existing listing_ids for "
                    f"seller={seller_username}"
                )
                return ids
        except SQLAlchemyError as e:
            logger.warning(f"Failed to load existing listing_ids: {e}")
            return set()

    def upsert_listings(self, listings: list[EbayListing]) -> int:
        """Upsert each listing. Returns the number of rows written."""
        if not self.db_engine or not listings:
            return 0

        updated = 0
        sql = text("""
            INSERT INTO ebay_listings (
                listing_id, seller_username, title, url, image_url,
                price, currency, display_price, condition, quantity,
                shipping_cost, brand, source_url, updated_at
            ) VALUES (
                :listing_id, :seller_username, :title, :url, :image_url,
                :price, :currency, :display_price, :condition, :quantity,
                :shipping_cost, :brand, :source_url, CURRENT_TIMESTAMP
            )
            ON CONFLICT (listing_id, seller_username) DO UPDATE SET
                title = COALESCE(EXCLUDED.title, ebay_listings.title),
                url = COALESCE(EXCLUDED.url, ebay_listings.url),
                image_url = COALESCE(EXCLUDED.image_url, ebay_listings.image_url),
                price = COALESCE(EXCLUDED.price, ebay_listings.price),
                currency = COALESCE(EXCLUDED.currency, ebay_listings.currency),
                display_price = COALESCE(EXCLUDED.display_price, ebay_listings.display_price),
                condition = COALESCE(EXCLUDED.condition, ebay_listings.condition),
                quantity = COALESCE(EXCLUDED.quantity, ebay_listings.quantity),
                shipping_cost = COALESCE(EXCLUDED.shipping_cost, ebay_listings.shipping_cost),
                brand = COALESCE(EXCLUDED.brand, ebay_listings.brand),
                source_url = COALESCE(EXCLUDED.source_url, ebay_listings.source_url),
                updated_at = CURRENT_TIMESTAMP
        """)

        try:
            with self.db_engine.connect() as conn:
                for listing in listings:
                    try:
                        conn.execute(sql, listing.model_dump())
                        updated += 1
                    except SQLAlchemyError as e:
                        logger.warning(
                            f"Failed to upsert listing {listing.listing_id}: {e}"
                        )
                conn.commit()
        except SQLAlchemyError as e:
            logger.error(f"Database error during upsert: {e}")

        return updated
