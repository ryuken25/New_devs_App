"""Regression tests for the three issues the clients reported.

Client B saw another company's revenue, Client A's March total did not match
their own books, and finance kept finding totals off by a cent. Each block
below pins down one of those.
"""
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest
from fastapi import HTTPException

from app.api.v1 import dashboard
from app.core.database_pool import to_async_url
from app.core.tenant_resolver import TenantResolver
from app.models.auth import AuthenticatedUser
from app.services import cache, reservations
from app.services.reservations import (
    MixedCurrencyRevenue,
    PropertyNotFound,
    month_bounds_utc,
)

# prop-001 exists for both tenants in database/seed.sql, which is what makes
# the cache key collision reachable in the first place.
SHARED_PROPERTY_ID = "prop-001"

# res-tz-1 from the seed: 23:30 UTC on a leap day, so 00:30 on 1 March in Paris.
BOUNDARY_CHECK_IN = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)


class FakeRedis:
    """Just the three calls the cache service makes."""

    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cache, "redis_client", fake)
    return fake


@pytest.fixture
def revenue_by_tenant(monkeypatch):
    """Stub the database aggregation with a per-tenant answer."""
    totals = {"tenant-a": ("2250.000", 4), "tenant-b": ("875.500", 2)}
    calls = []

    async def calculate_total_revenue(property_id, tenant_id):
        calls.append((property_id, tenant_id))
        total, count = totals[tenant_id]
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": total,
            "currency": "USD",
            "count": count,
        }

    monkeypatch.setattr(
        reservations, "calculate_total_revenue", calculate_total_revenue
    )
    return calls


def client_user(tenant_id, email="sunset@propertyflow.com"):
    return AuthenticatedUser(
        id="user-test",
        email=email,
        permissions=[],
        cities=[],
        is_admin=False,
        tenant_id=tenant_id,
    )


# --- Client B: revenue belonging to another company -----------------------


@pytest.mark.asyncio
async def test_tenants_sharing_a_property_id_get_their_own_revenue(
    redis, revenue_by_tenant
):
    first = await cache.get_revenue_summary(SHARED_PROPERTY_ID, "tenant-a")
    second = await cache.get_revenue_summary(SHARED_PROPERTY_ID, "tenant-b")

    assert first["total"] == "2250.000"
    assert second["total"] == "875.500"
    assert set(redis.store) == {
        "revenue:tenant-a:prop-001",
        "revenue:tenant-b:prop-001",
    }


@pytest.mark.asyncio
async def test_repeat_request_is_still_served_from_cache(redis, revenue_by_tenant):
    await cache.get_revenue_summary(SHARED_PROPERTY_ID, "tenant-a")
    await cache.get_revenue_summary(SHARED_PROPERTY_ID, "tenant-a")

    assert revenue_by_tenant == [(SHARED_PROPERTY_ID, "tenant-a")]


@pytest.mark.asyncio
async def test_entry_holding_another_tenants_payload_is_not_served(
    redis, revenue_by_tenant
):
    redis.store["revenue:tenant-b:prop-001"] = json.dumps(
        {
            "property_id": SHARED_PROPERTY_ID,
            "tenant_id": "tenant-a",
            "total": "2250.000",
            "currency": "USD",
            "count": 4,
        }
    )

    result = await cache.get_revenue_summary(SHARED_PROPERTY_ID, "tenant-b")

    assert result["tenant_id"] == "tenant-b"
    assert result["total"] == "875.500"


def test_cache_key_refuses_to_build_without_a_tenant():
    with pytest.raises(ValueError):
        cache.revenue_cache_key(SHARED_PROPERTY_ID, "")


@pytest.mark.asyncio
async def test_request_without_a_tenant_is_rejected():
    with pytest.raises(HTTPException) as raised:
        await dashboard.get_dashboard_summary(SHARED_PROPERTY_ID, client_user(None))

    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_unknown_account_resolves_to_no_tenant():
    tenant_id = await TenantResolver.resolve_tenant_id(
        user_id="user-stranger", user_email="stranger@example.com"
    )

    assert tenant_id is None


@pytest.mark.asyncio
async def test_tenant_comes_from_the_verified_token_claims():
    tenant_id = await TenantResolver.resolve_tenant_id(
        user_id="user-ocean",
        user_email="ocean@propertyflow.com",
        app_metadata={"tenant_id": "tenant-b"},
    )

    assert tenant_id == "tenant-b"


# --- Client A: March does not match their books ---------------------------


def test_march_is_bounded_by_local_midnight_in_paris():
    start, end = month_bounds_utc(2024, 3, "Europe/Paris")

    # Paris is UTC+1 on 1 March and UTC+2 on 1 April (DST starts 31 March).
    assert start == datetime(2024, 2, 29, 23, 0, tzinfo=timezone.utc)
    assert end == datetime(2024, 3, 31, 22, 0, tzinfo=timezone.utc)


def test_march_is_bounded_by_local_midnight_in_new_york():
    start, end = month_bounds_utc(2024, 3, "America/New_York")

    assert start == datetime(2024, 3, 1, 5, 0, tzinfo=timezone.utc)
    assert end == datetime(2024, 4, 1, 4, 0, tzinfo=timezone.utc)


def test_december_rolls_over_into_the_next_year():
    start, end = month_bounds_utc(2024, 12, "UTC")

    assert start == datetime(2024, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_late_night_booking_counts_in_the_month_the_property_is_living_in():
    february = month_bounds_utc(2024, 2, "Europe/Paris")
    march = month_bounds_utc(2024, 3, "Europe/Paris")

    assert not february[0] <= BOUNDARY_CHECK_IN < february[1]
    assert march[0] <= BOUNDARY_CHECK_IN < march[1]


def test_the_same_booking_stays_in_february_for_a_new_york_property():
    february = month_bounds_utc(2024, 2, "America/New_York")

    assert february[0] <= BOUNDARY_CHECK_IN < february[1]


def test_months_are_half_open_so_nothing_is_counted_twice():
    _, february_end = month_bounds_utc(2024, 2, "Europe/Paris")
    march_start, _ = month_bounds_utc(2024, 3, "Europe/Paris")

    assert february_end == march_start


# --- Finance: totals off by a cent ----------------------------------------


@pytest.mark.asyncio
async def test_total_is_rounded_once_at_the_end(monkeypatch):
    # The seed splits 1000.000 into 333.333 / 333.333 / 333.334 precisely
    # because NUMERIC(10,3) can hold a third of a cent.
    rows = [Decimal("333.333"), Decimal("333.333"), Decimal("333.334")]
    rounded_per_row = sum(
        row.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for row in rows
    )

    async def get_revenue_summary(property_id, tenant_id):
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": str(sum(rows)),
            "currency": "USD",
            "count": len(rows),
        }

    monkeypatch.setattr(dashboard, "get_revenue_summary", get_revenue_summary)

    response = await dashboard.get_dashboard_summary(
        "prop-001", client_user("tenant-a")
    )

    assert response["total_revenue"] == "1000.00"
    # What rounding each row before summing would have cost the client.
    assert rounded_per_row == Decimal("999.99")


@pytest.mark.asyncio
async def test_half_a_cent_rounds_up_rather_than_to_even(monkeypatch):
    async def get_revenue_summary(property_id, tenant_id):
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": "1000.005",
            "currency": "USD",
            "count": 1,
        }

    monkeypatch.setattr(dashboard, "get_revenue_summary", get_revenue_summary)

    response = await dashboard.get_dashboard_summary(
        "prop-001", client_user("tenant-a")
    )

    assert response["total_revenue"] == "1000.01"
    # The old path went through float, which loses that cent instead.
    assert round(float("1000.005") * 100) / 100 == 1000.0


@pytest.mark.asyncio
async def test_total_leaves_the_api_as_a_decimal_string(monkeypatch):
    async def get_revenue_summary(property_id, tenant_id):
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": "2250.000",
            "currency": "EUR",
            "count": 4,
        }

    monkeypatch.setattr(dashboard, "get_revenue_summary", get_revenue_summary)

    response = await dashboard.get_dashboard_summary(
        "prop-001", client_user("tenant-a")
    )

    assert response == {
        "property_id": "prop-001",
        "total_revenue": "2250.00",
        "currency": "EUR",
        "reservations_count": 4,
    }
    assert isinstance(response["total_revenue"], str)


# --- Failure handling -----------------------------------------------------


@pytest.mark.asyncio
async def test_missing_property_is_a_404_not_a_zero(monkeypatch):
    async def get_revenue_summary(property_id, tenant_id):
        raise PropertyNotFound(property_id)

    monkeypatch.setattr(dashboard, "get_revenue_summary", get_revenue_summary)

    with pytest.raises(HTTPException) as raised:
        await dashboard.get_dashboard_summary("prop-999", client_user("tenant-a"))

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_mixed_currencies_are_refused_instead_of_added_up(monkeypatch):
    async def get_revenue_summary(property_id, tenant_id):
        raise MixedCurrencyRevenue(
            property_id, {"USD": Decimal("100"), "EUR": Decimal("50")}
        )

    monkeypatch.setattr(dashboard, "get_revenue_summary", get_revenue_summary)

    with pytest.raises(HTTPException) as raised:
        await dashboard.get_dashboard_summary("prop-001", client_user("tenant-a"))

    assert raised.value.status_code == 409
    assert "EUR" in raised.value.detail


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("postgresql://u:p@db:5432/flow", "postgresql+asyncpg://u:p@db:5432/flow"),
        ("postgres://u:p@db:5432/flow", "postgresql+asyncpg://u:p@db:5432/flow"),
        ("postgresql+asyncpg://u:p@db:5432/flow", "postgresql+asyncpg://u:p@db:5432/flow"),
    ],
)
def test_database_url_is_pointed_at_the_async_driver(configured, expected):
    assert to_async_url(configured) == expected
