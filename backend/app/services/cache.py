import json
import logging
import os
from typing import Any, Dict

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

CACHE_TTL_SECONDS = 300


def revenue_cache_key(property_id: str, tenant_id: str) -> str:
    """Build the Redis key for a property's revenue summary.

    Property ids are only unique within a tenant (properties is keyed on
    (id, tenant_id)), so the tenant has to be part of the key. Without it two
    tenants that happen to share a property id serve each other's revenue for
    as long as the entry lives.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required to build a revenue cache key")
    if not property_id:
        raise ValueError("property_id is required to build a revenue cache key")
    return f"revenue:{tenant_id}:{property_id}"


async def get_revenue_summary(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    cache_key = revenue_cache_key(property_id, tenant_id)

    # Try to get from cache
    cached = await redis_client.get(cache_key)
    if cached:
        payload = json.loads(cached)
        # Second line of defence: never hand back an entry that was written for
        # another tenant, whatever the key it was found under.
        if payload.get("tenant_id") == tenant_id:
            return payload
        logger.warning(
            "Discarding revenue cache entry under %s: belongs to tenant %s",
            cache_key,
            payload.get("tenant_id"),
        )

    # Revenue calculation is delegated to the reservation service.
    from app.services.reservations import calculate_total_revenue

    # Calculate revenue
    result = await calculate_total_revenue(property_id, tenant_id)

    # Cache the result for 5 minutes
    await redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))

    return result


async def invalidate_revenue_summary(property_id: str, tenant_id: str) -> None:
    """Drop a cached summary, e.g. after a reservation changes."""
    await redis_client.delete(revenue_cache_key(property_id, tenant_id))
