"""The late-dispatch rate must not become insensitive as history accumulates.

`count_late_dispatches` used to measure against every order the facility had
ever placed. Amazon's own Late Shipment Rate -- the ceiling this metric is
built to mirror -- is a trailing window, not an all-time average. Without a
window, a facility with years of history can have a catastrophic week and see
the reported rate barely move, exactly when an operator most needs the number
to react.

These drive the fix directly against the repository function, since nothing
in the existing suite exercised it at all: an order placed inside the window
counts on both sides of the ratio, one placed outside it counts on neither.

Each test does its whole scenario inside one `asyncio.run` call. The shared
`engine` pools connections across calls, and pooled connections are tied to
the event loop that created them -- calling `asyncio.run` a second time inside
one test spins up a new loop and hands the old pool's connections a closed one
to write to, which is the "'NoneType' object has no attribute 'send'" failure
documented for the same reason in test_alert_recipients.py.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db import engine
from app.repository import count_late_dispatches

FACILITY = "WH-01"
WINDOW_DAYS = 10


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    asyncio.run(engine.dispose())


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.usefixtures("require_database")
def test_an_order_outside_the_window_counts_on_neither_side():
    """Old history must not water down or inflate today's rate."""
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    old_miss = "LATE-WINDOW-OLD-MISS"

    async def _scenario():
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orders WHERE order_id = :order_id"),
                {"order_id": old_miss},
            )
            baseline_misses, baseline_placed = await count_late_dispatches(
                conn, FACILITY, now, window_days=WINDOW_DAYS
            )

            await conn.execute(
                text(
                    """
                    INSERT INTO orders (order_id, facility_id, created_at,
                                        promised_dispatch_at, item_count,
                                        work_units, order_value, status)
                    VALUES (:order_id, :facility_id, :created_at,
                            :promised_dispatch_at, 1, 1.0, :order_value, :status)
                    """
                ),
                {
                    "order_id": old_miss,
                    "facility_id": FACILITY,
                    "created_at": now - timedelta(days=WINDOW_DAYS + 5),
                    "promised_dispatch_at": now - timedelta(days=WINDOW_DAYS + 4),
                    "order_value": Decimal(0),
                    "status": "PENDING",
                },
            )
            with_old_order_misses, with_old_order_placed = await count_late_dispatches(
                conn, FACILITY, now, window_days=WINDOW_DAYS
            )

            await conn.execute(
                text("DELETE FROM orders WHERE order_id = :order_id"),
                {"order_id": old_miss},
            )
            return (
                baseline_misses,
                baseline_placed,
                with_old_order_misses,
                with_old_order_placed,
            )

    baseline_misses, baseline_placed, with_old_misses, with_old_placed = _run(_scenario())

    # The old order predates the window on both counts, so it must
    # contribute to neither the numerator nor the denominator.
    assert with_old_misses == baseline_misses
    assert with_old_placed == baseline_placed


@pytest.mark.usefixtures("require_database")
def test_an_order_inside_the_window_counts_on_both_sides():
    """A recent miss must still be visible."""
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    recent_miss = "LATE-WINDOW-RECENT-MISS"

    async def _scenario():
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM orders WHERE order_id = :order_id"),
                {"order_id": recent_miss},
            )
            before_misses, before_placed = await count_late_dispatches(
                conn, FACILITY, now, window_days=WINDOW_DAYS
            )

            await conn.execute(
                text(
                    """
                    INSERT INTO orders (order_id, facility_id, created_at,
                                        promised_dispatch_at, item_count,
                                        work_units, order_value, status)
                    VALUES (:order_id, :facility_id, :created_at,
                            :promised_dispatch_at, 1, 1.0, :order_value, :status)
                    """
                ),
                {
                    "order_id": recent_miss,
                    "facility_id": FACILITY,
                    "created_at": now - timedelta(days=1),
                    "promised_dispatch_at": now - timedelta(hours=1),
                    "order_value": Decimal(0),
                    "status": "PENDING",
                },
            )
            after_misses, after_placed = await count_late_dispatches(
                conn, FACILITY, now, window_days=WINDOW_DAYS
            )

            await conn.execute(
                text("DELETE FROM orders WHERE order_id = :order_id"),
                {"order_id": recent_miss},
            )
            return before_misses, before_placed, after_misses, after_placed

    before_misses, before_placed, after_misses, after_placed = _run(_scenario())

    assert after_misses == before_misses + 1
    assert after_placed == before_placed + 1
