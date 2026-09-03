import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.database_pool import db_pool

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "UTC"
DEFAULT_CURRENCY = "USD"


class PropertyNotFound(Exception):
    """The property does not exist, or does not belong to the tenant asking."""


class MixedCurrencyRevenue(Exception):
    """A property has reservations in several currencies, so no single total exists."""

    def __init__(self, property_id: str, breakdown: Dict[str, Decimal]):
        self.property_id = property_id
        self.breakdown = breakdown
        currencies = ", ".join(sorted(breakdown))
        super().__init__(
            f"Property {property_id} has revenue in multiple currencies: {currencies}"
        )


def month_bounds_utc(
    year: int, month: int, timezone_name: str
) -> Tuple[datetime, datetime]:
    """UTC bounds of a calendar month as it is lived at the property.

    Returned as a half-open range [start, end). The database stores check-in
    times as timestamptz (i.e. UTC), so the boundaries have to be built in the
    property's own zone and converted, not built in UTC. A stay that starts at
    23:30 UTC on 29 Feb is already 1 March in Paris and belongs to the March
    report, which is exactly the kind of booking that goes missing otherwise.
    """
    tz = ZoneInfo(timezone_name or DEFAULT_TIMEZONE)

    start_local = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=tz)

    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def get_property_timezone(session, property_id: str, tenant_id: str) -> str:
    """Read a property's timezone, scoped to the tenant that owns it."""
    query = text(
        """
        SELECT timezone
        FROM properties
        WHERE id = :property_id AND tenant_id = :tenant_id
        """
    )
    params = {"property_id": property_id, "tenant_id": tenant_id}
    row = (await session.execute(query, params)).fetchone()

    if row is None:
        raise PropertyNotFound(
            f"Property {property_id} not found for tenant {tenant_id}"
        )

    return row.timezone or DEFAULT_TIMEZONE


async def calculate_monthly_revenue(
    property_id: str, tenant_id: str, month: int, year: int
) -> Decimal:
    """
    Calculates revenue for a specific month.
    """
    async with db_pool.get_session() as session:
        property_timezone = await get_property_timezone(session, property_id, tenant_id)
        start_date, end_date = month_bounds_utc(year, month, property_timezone)

        query = text(
            """
            SELECT COALESCE(SUM(total_amount), 0) AS total
            FROM reservations
            WHERE property_id = :property_id
              AND tenant_id = :tenant_id
              AND check_in_date >= :start_date
              AND check_in_date < :end_date
            """
        )
        total = (
            await session.execute(
                query,
                {
                    "property_id": property_id,
                    "tenant_id": tenant_id,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
        ).scalar_one()

        logger.debug(
            "Monthly revenue for %s (%s) %s-%02d [%s, %s) = %s",
            property_id,
            property_timezone,
            year,
            month,
            start_date,
            end_date,
            total,
        )

    return Decimal(str(total))


async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Aggregates revenue from database.

    The total is kept as an exact Decimal all the way out of here; rounding to
    cents is the caller's job and must only happen once, on the way to display.
    """
    async with db_pool.get_session() as session:
        # Also confirms the property belongs to this tenant before reporting on it.
        await get_property_timezone(session, property_id, tenant_id)

        query = text(
            """
            SELECT
                currency,
                SUM(total_amount) AS total_revenue,
                COUNT(*) AS reservation_count
            FROM reservations
            WHERE property_id = :property_id AND tenant_id = :tenant_id
            GROUP BY currency
            """
        )
        params = {"property_id": property_id, "tenant_id": tenant_id}
        rows = (await session.execute(query, params)).fetchall()

    if not rows:
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": "0",
            "currency": DEFAULT_CURRENCY,
            "count": 0,
        }

    if len(rows) > 1:
        # Adding up different currencies would produce a number that means
        # nothing, so refuse instead of guessing an exchange rate.
        raise MixedCurrencyRevenue(
            property_id,
            {row.currency: Decimal(str(row.total_revenue)) for row in rows},
        )

    row = rows[0]
    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "total": str(Decimal(str(row.total_revenue))),
        "currency": row.currency or DEFAULT_CURRENCY,
        "count": row.reservation_count,
    }
