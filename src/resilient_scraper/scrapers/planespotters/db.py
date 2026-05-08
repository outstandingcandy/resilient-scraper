"""
Database operations for the Planespotters scraper.

Encapsulates all database interaction logic including table creation
and aircraft data persistence. Uses its own `planespotters_aircraft`
table instead of the shared `aircraft_static_info` table.
"""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from resilient_scraper.scrapers.planespotters.models import PlanespottersAircraftData

logger = logging.getLogger("scraper.planespotters")


class PlanespottersDB:
    """Database operations for Planespotters scraper data.

    Args:
        db_engine: SQLAlchemy database engine instance.
    """

    def __init__(self, db_engine: Engine | None) -> None:
        self.db_engine = db_engine

    def ensure_tables_exist(self) -> None:
        """Create planespotters_aircraft table if it doesn't exist."""
        if not self.db_engine:
            return

        try:
            with self.db_engine.connect() as conn:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS planespotters_aircraft (
                        id BIGSERIAL PRIMARY KEY,
                        registration VARCHAR(20) UNIQUE NOT NULL,
                        serial_number VARCHAR(50),
                        aircraft_type VARCHAR(10),
                        manufacturer VARCHAR(100),
                        model VARCHAR(100),
                        operator VARCHAR(200),
                        delivery_date VARCHAR(50),
                        status VARCHAR(50),
                        source_url VARCHAR(500),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))

                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_planespotters_aircraft_manufacturer
                    ON planespotters_aircraft(manufacturer)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_planespotters_aircraft_operator
                    ON planespotters_aircraft(operator)
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_planespotters_aircraft_updated
                    ON planespotters_aircraft(updated_at)
                """))

                conn.commit()
                logger.info("planespotters_aircraft table ready")
        except SQLAlchemyError as e:
            logger.error(f"Failed to create planespotters_aircraft table: {e}")

    def load_existing_registrations(self) -> set[str]:
        """Load existing registrations for skip_existing mode.

        Returns:
            Set of registration strings already in the table.
        """
        if not self.db_engine:
            return set()

        try:
            with self.db_engine.connect() as conn:
                result = conn.execute(
                    text("SELECT registration FROM planespotters_aircraft")
                )
                regs = {row[0] for row in result}
                logger.info(f"Loaded {len(regs)} existing registrations")
                return regs
        except SQLAlchemyError as e:
            logger.warning(f"Failed to load existing registrations: {e}")
            return set()

    def upsert_aircraft(
        self,
        aircraft_list: list[PlanespottersAircraftData],
        manufacturer: str,
        family: str,
    ) -> int:
        """Upsert aircraft data into planespotters_aircraft table.

        Args:
            aircraft_list: List of aircraft to upsert.
            manufacturer: Manufacturer name (slug form, e.g., "boeing").
            family: Aircraft family (e.g., "747").

        Returns:
            Number of records upserted.
        """
        if not self.db_engine or not aircraft_list:
            return 0

        updated = 0
        manufacturer_name = manufacturer.replace("-", " ").title()

        try:
            with self.db_engine.connect() as conn:
                for ac in aircraft_list:
                    try:
                        conn.execute(
                            text("""
                                INSERT INTO planespotters_aircraft (
                                    registration, serial_number, aircraft_type,
                                    manufacturer, model, operator,
                                    delivery_date, status, source_url,
                                    updated_at
                                ) VALUES (
                                    :registration, :serial_number, :aircraft_type,
                                    :manufacturer, :model, :operator,
                                    :delivery_date, :status, :source_url,
                                    CURRENT_TIMESTAMP
                                )
                                ON CONFLICT (registration) DO UPDATE SET
                                    serial_number = COALESCE(EXCLUDED.serial_number, planespotters_aircraft.serial_number),
                                    aircraft_type = COALESCE(EXCLUDED.aircraft_type, planespotters_aircraft.aircraft_type),
                                    manufacturer = COALESCE(EXCLUDED.manufacturer, planespotters_aircraft.manufacturer),
                                    model = COALESCE(EXCLUDED.model, planespotters_aircraft.model),
                                    operator = COALESCE(EXCLUDED.operator, planespotters_aircraft.operator),
                                    delivery_date = COALESCE(EXCLUDED.delivery_date, planespotters_aircraft.delivery_date),
                                    status = COALESCE(EXCLUDED.status, planespotters_aircraft.status),
                                    source_url = COALESCE(EXCLUDED.source_url, planespotters_aircraft.source_url),
                                    updated_at = CURRENT_TIMESTAMP
                            """),
                            {
                                "registration": ac.registration,
                                "serial_number": ac.serial_number,
                                "aircraft_type": ac.aircraft_type,
                                "manufacturer": manufacturer_name,
                                "model": ac.model or f"{manufacturer_name} {family}".strip(),
                                "operator": ac.operator,
                                "delivery_date": ac.delivery_date,
                                "status": ac.status,
                                "source_url": ac.source_url,
                            },
                        )
                        updated += 1
                    except SQLAlchemyError as e:
                        logger.warning(f"Failed to upsert {ac.registration}: {e}")
                conn.commit()
        except SQLAlchemyError as e:
            logger.error(f"Database error during upsert: {e}")

        return updated
