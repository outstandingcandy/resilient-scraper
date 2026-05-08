"""FastAPI router for Planespotters data query endpoints."""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from resilient_scraper.service.database import Database

logger = logging.getLogger("resilient_scraper.scrapers.planespotters.router")


class AircraftResponse(BaseModel):
    """Aircraft from Planespotters data."""

    registration: str
    aircraft_type: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    operator: str | None = None
    serial_number: str | None = None
    delivery_date: str | None = None
    status: str | None = None
    source_url: str | None = None
    updated_at: datetime | None = None


def create_router(db: Database) -> APIRouter:
    """Create the Planespotters data query router."""
    router = APIRouter()

    @router.get("/aircraft", response_model=list[AircraftResponse])
    async def list_aircraft(
        manufacturer: str | None = None,
        operator: str | None = None,
        search: str | None = None,
        limit: int = Query(50, le=200),
        offset: int = 0,
    ) -> list[AircraftResponse]:
        """List aircraft with optional filters."""
        conditions = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if manufacturer:
            conditions.append("manufacturer ILIKE :manufacturer")
            params["manufacturer"] = f"%{manufacturer}%"
        if operator:
            conditions.append("operator ILIKE :operator")
            params["operator"] = f"%{operator}%"
        if search:
            conditions.append("(registration ILIKE :search OR model ILIKE :search)")
            params["search"] = f"%{search}%"

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT registration, aircraft_type, manufacturer, model,
                   operator, serial_number, delivery_date, status,
                   source_url, updated_at
            FROM planespotters_aircraft {where}
            ORDER BY updated_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """

        async with db.session() as session:
            result = await session.execute(text(query), params)
            rows = result.mappings().fetchall()
            return [AircraftResponse(**dict(row)) for row in rows]

    @router.get("/aircraft/{registration}", response_model=AircraftResponse)
    async def get_aircraft(registration: str) -> AircraftResponse:
        """Get aircraft details by registration."""
        async with db.session() as session:
            result = await session.execute(
                text("""
                    SELECT registration, aircraft_type, manufacturer, model,
                           operator, serial_number, delivery_date, status,
                           source_url, updated_at
                    FROM planespotters_aircraft WHERE registration = :reg
                """),
                {"reg": registration},
            )
            row = result.mappings().fetchone()
            if not row:
                raise HTTPException(404, "Aircraft not found")
            return AircraftResponse(**dict(row))

    return router
