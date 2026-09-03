from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import authenticate_request as get_current_user
from app.models.auth import AuthenticatedUser
from app.services.cache import get_revenue_summary
from app.services.reservations import MixedCurrencyRevenue, PropertyNotFound

router = APIRouter()

CENTS = Decimal("0.01")


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user)
) -> Dict[str, Any]:

    tenant_id = current_user.tenant_id
    if not tenant_id:
        # No tenant means we cannot scope the query, and falling back to some
        # placeholder tenant would hand this user somebody else's revenue.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tenant is assigned to this account",
        )

    try:
        revenue_data = await get_revenue_summary(property_id, tenant_id)
    except PropertyNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property {property_id} not found",
        )
    except MixedCurrencyRevenue as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        )

    # The total arrives exact (NUMERIC(10,3) holds a third of a cent) and is
    # rounded to cents once, here, rather than per reservation. It leaves as a
    # string because a JSON number is a double and the browser would then round
    # it a second time.
    total_revenue = Decimal(revenue_data["total"]).quantize(
        CENTS, rounding=ROUND_HALF_UP
    )

    return {
        "property_id": revenue_data["property_id"],
        "total_revenue": str(total_revenue),
        "currency": revenue_data["currency"],
        "reservations_count": revenue_data["count"]
    }
