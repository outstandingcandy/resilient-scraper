"""FastAPI router for eBay listing query endpoints."""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from resilient_scraper.service.database import Database

logger = logging.getLogger("resilient_scraper.scrapers.ebay.router")


class ListingResponse(BaseModel):
    """An eBay listing as stored in the DB."""

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
    scraped_at: datetime | None = None
    updated_at: datetime | None = None


_SELECT = """
    SELECT listing_id, seller_username, title, url, image_url,
           price, currency, display_price, condition, quantity,
           shipping_cost, brand, source_url, scraped_at, updated_at
    FROM ebay_listings
"""


def create_router(db: Database) -> APIRouter:
    """Create the eBay data query router."""
    router = APIRouter()

    @router.get("/listings", response_model=list[ListingResponse])
    async def list_listings(
        seller_username: str | None = None,
        brand: str | None = None,
        condition: str | None = None,
        search: str | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> list[ListingResponse]:
        """List listings with optional filters."""
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if seller_username:
            conditions.append("seller_username = :seller_username")
            params["seller_username"] = seller_username
        if brand:
            conditions.append("brand ILIKE :brand")
            params["brand"] = f"%{brand}%"
        if condition:
            conditions.append("condition = :condition")
            params["condition"] = condition
        if search:
            conditions.append("title ILIKE :search")
            params["search"] = f"%{search}%"

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            {_SELECT}
            {where}
            ORDER BY updated_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """

        async with db.session() as session:
            result = await session.execute(text(query), params)
            rows = result.mappings().fetchall()
            return [ListingResponse(**dict(row)) for row in rows]

    @router.get("/listings/{listing_id}", response_model=ListingResponse)
    async def get_listing(
        listing_id: str,
        seller_username: str | None = None,
    ) -> ListingResponse:
        """Fetch a single listing by ID (optionally scoped to a seller)."""
        sql = f"{_SELECT} WHERE listing_id = :listing_id"
        params: dict[str, Any] = {"listing_id": listing_id}
        if seller_username:
            sql += " AND seller_username = :seller_username"
            params["seller_username"] = seller_username
        sql += " LIMIT 1"

        async with db.session() as session:
            result = await session.execute(text(sql), params)
            row = result.mappings().fetchone()
            if not row:
                raise HTTPException(404, "Listing not found")
            return ListingResponse(**dict(row))

    @router.get(
        "/sellers/{seller_username}/listings",
        response_model=list[ListingResponse],
    )
    async def list_seller_listings(
        seller_username: str,
        brand: str | None = None,
        condition: str | None = None,
        search: str | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> list[ListingResponse]:
        """List all listings for one seller with optional filters."""
        conditions = ["seller_username = :seller_username"]
        params: dict[str, Any] = {
            "seller_username": seller_username,
            "limit": limit,
            "offset": offset,
        }
        if brand:
            conditions.append("brand ILIKE :brand")
            params["brand"] = f"%{brand}%"
        if condition:
            conditions.append("condition = :condition")
            params["condition"] = condition
        if search:
            conditions.append("title ILIKE :search")
            params["search"] = f"%{search}%"

        query = f"""
            {_SELECT}
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """

        async with db.session() as session:
            result = await session.execute(text(query), params)
            rows = result.mappings().fetchall()
            return [ListingResponse(**dict(row)) for row in rows]

    return router
