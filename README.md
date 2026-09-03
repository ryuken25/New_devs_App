# Property Revenue Dashboard

A multi-tenant revenue dashboard for property management clients. Each client
sees revenue aggregated from the reservations on the properties they own, with a
Redis layer in front of the aggregation query.

This repository is the debugging exercise described in
[`ASSIGNMENT.md`](ASSIGNMENT.md).

## Running the stack

```bash
docker-compose up --build
```

- Frontend on http://localhost:3000
- API docs on http://localhost:8000/docs

Postgres is initialised from `database/schema.sql` and `database/seed.sql` on
first start. Storage is ephemeral, so bringing the stack down and up again
resets the data.

### Client accounts

| Client | Email | Password |
| --- | --- | --- |
| Sunset Properties (`tenant-a`) | sunset@propertyflow.com | client_a_2024 |
| Ocean Rentals (`tenant-b`) | ocean@propertyflow.com | client_b_2024 |

## Running the backend outside Docker

Postgres and Redis still need to be reachable at the addresses in
`docker-compose.yml`, or overridden through `DATABASE_URL` and `REDIS_URL`.

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest
```

`backend/tests/test_revenue_dashboard.py` is a regression suite for the reported
issues. It runs without a database or a Redis instance.

## What was fixed

Three defects match the three reported symptoms.

**Client B saw another company's revenue.** The Redis key for a revenue summary
was `revenue:{property_id}`, but property ids are only unique within a tenant.
`properties` is keyed on `(id, tenant_id)`, and `prop-001` belongs to both
tenants in the seed data, so whichever client asked first populated the entry
that the other client then read. Keys are now scoped as
`revenue:{tenant_id}:{property_id}`, and a cached payload is rejected if its
tenant does not match the caller.

**Client A's March total did not match their records.** Two causes. The
connection pool built its URL from settings that do not exist on the `Settings`
class, so every query raised `AttributeError` and a fallback returned hardcoded
totals instead of reading the database. Separately, `calculate_monthly_revenue`
built month boundaries from naive datetimes, which puts a Paris property's
00:30 local check-in into the previous month. Boundaries are now built at local
midnight in the property's own timezone and converted to UTC.

**Finance found totals off by a few cents.** `total_amount` is
`NUMERIC(10, 3)`, so amounts can carry a third of a cent, and rounding each
reservation before summing loses a cent that rounding once at the end does not.
The total is now summed exactly, rounded once with `ROUND_HALF_UP`, and returned
as a decimal string so a JSON double cannot reintroduce the error.

Six further issues on the same code path were fixed alongside them: a tenant
resolver that defaulted unknown users to `tenant-a`, a `"default_tenant"`
placeholder in the dashboard endpoint, a client-supplied `X-Simulated-Tenant`
header, a hardcoded currency, a hardcoded growth percentage on the revenue card,
and misuse of `async with` on a coroutine in the session helper.

## Layout

```
backend/app/api/v1/dashboard.py        revenue endpoint
backend/app/services/cache.py          Redis layer in front of the aggregation
backend/app/services/reservations.py   revenue aggregation and month boundaries
backend/app/core/database_pool.py      async engine and session management
backend/app/core/tenant_resolver.py    maps an authenticated user to a tenant
frontend/src/components/RevenueSummary.tsx   revenue card
database/schema.sql                    tables, including per-property timezone
database/seed.sql                      two tenants, five properties, reservations
```
