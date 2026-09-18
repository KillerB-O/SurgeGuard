"""Verify a capacity commitment against measured throughput, not trust it forever.

A capacity_delta effect's target is a claim -- "the floor can now do 72
wu/hr" -- and `_apply_facility_policy` (P6) no longer writes it straight to
`facilities.capacity_per_hour` the instant execution completes. This module
is what turns the claim into a verdict, using the same
`order_status_transitions`-derived signal `get_throughput_signal` already
prefers over configured capacity.

The false-negative this exists to avoid: a floor genuinely running at the new
capacity reports low derived throughput once its own backlog clears, simply
because there is no work left to prove the capacity against. Comparing raw
`derived` to the raw `target` would then report PARTIAL or NOT_OBSERVED for a
floor that did exactly what was asked -- worse than no verification, since
UNVERIFIED/NOT_OBSERVED is required to render loudly in the UI. So this
verifies against `effective_target = min(target, demand)`: a capacity claim
can only be tested by the demand that existed to test it.
"""

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import repository

# How much of the effective target a floor must clear to count as fully
# achieved. Floors do not hit an exact number, and a genuine claim measured
# at 71.5 against a 72 target is not a failure.
ACHIEVEMENT_TOLERANCE = 0.95

# How long past a lever's lead time to keep waiting for a derived signal
# before giving up and reporting NOT_OBSERVED, rather than leaving a
# genuinely silent floor UNVERIFIED forever. Distinct from
# repository.DERIVED_THROUGHPUT_WINDOW, which is how far back a signal looks;
# this is how long a commitment may sit unresolved.
MAX_VERIFICATION_WAIT = timedelta(hours=1)


async def verify_capacity_commitments(conn: AsyncConnection, facility_id: str, now: datetime) -> int:
    """Resolve every due, unverified commitment for one facility.

    A commitment not yet past its lead time is left alone -- judging it early
    would be no more honest than trusting it unconditionally. One with a
    derived signal below `effective_target` but positive demand to test it
    against resolves now, not later: waiting does not make a partial result
    more true.

    Args:
        conn: Request-scoped database transaction.
        facility_id: Facility whose commitments should be checked.
        now: Evaluation instant, from the shared clock.

    Returns:
        How many commitments were resolved this call.
    """
    result = await conn.execute(
        text(
            """
            SELECT * FROM capacity_commitments
            WHERE facility_id = :facility_id AND verification_status = 'UNVERIFIED'
            FOR UPDATE
            """
        ),
        {"facility_id": facility_id},
    )
    rows = result.mappings().all()
    if not rows:
        return 0

    facility = await repository.get_facility(conn, facility_id)
    signal = await repository.get_throughput_signal(
        conn, facility_id, facility["capacity_per_hour"], now
    )
    derived = signal.observed_work_units_per_hour if signal.source == "derived" else None
    demand = await repository.get_demand_work_units_per_hour(conn, facility_id, now)

    resolved = 0
    for row in rows:
        due_at = row["committed_at"] + timedelta(minutes=row["lead_time_minutes"])
        if now < due_at:
            continue

        if derived is None:
            # No measurement exists yet at all -- not "below target", nothing
            # to compare. Only after MAX_VERIFICATION_WAIT does silence
            # itself become the verdict.
            if now >= due_at + MAX_VERIFICATION_WAIT:
                await _resolve(conn, row["id"], "NOT_OBSERVED", now, None)
                resolved += 1
            continue

        effective_target = min(row["target_work_units_per_hour"], demand)
        if effective_target <= 0:
            # Nothing has arrived to test the claim against. Holding rather
            # than judging: a quiet queue is not the floor's failure.
            continue

        status = "ACHIEVED" if derived / effective_target >= ACHIEVEMENT_TOLERANCE else "PARTIAL"
        await _resolve(conn, row["id"], status, now, derived)
        resolved += 1

    return resolved


async def _resolve(
    conn: AsyncConnection, commitment_id: int, status: str, verified_at: datetime, observed: float | None
) -> None:
    """Write a commitment's terminal verdict, once."""
    await conn.execute(
        text(
            """
            UPDATE capacity_commitments
            SET verification_status = :status,
                verified_at = :verified_at,
                observed_work_units_per_hour = :observed
            WHERE id = :id
            """
        ),
        {
            "id": commitment_id,
            "status": status,
            "verified_at": verified_at,
            "observed": observed,
        },
    )
