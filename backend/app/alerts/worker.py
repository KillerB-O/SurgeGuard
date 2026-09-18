"""The alert worker: a separate process that evaluates rules and sends mail.

Why a process rather than a hook on the request path
----------------------------------------------------
The obvious place to evaluate alerts is inside `POST /events/fulfillment`,
which n8n already calls on a schedule. That is wrong in the one case that
matters most: if telemetry STOPS arriving -- the simulator dies, n8n breaks,
the floor systems go down -- a request-path evaluator stops running too, and
the system goes quiet at exactly the moment an operator most needs to hear
from it. A request path cannot detect the absence of requests.

So this is a loop. It also means adding alerting required no change to any
n8n workflow, which matters because those are executed from n8n's own
database and have to be re-imported through its UI, reassigning workflow ids
in the process.

Two passes per tick, in separate transactions
---------------------------------------------
Evaluation commits its queued alerts before delivery is attempted. If the
relay is down, the alerts are already durable and the next tick sends them;
if they shared a transaction, an SMTP failure would roll back the decision to
alert at all.

Neither pass may raise past `run_once`. An unhandled exception here would
kill the loop, and a dead alert worker looks exactly like a quiet warehouse.
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock, verification
from app import repository as core_repository
from app.alerts import dispatcher, evaluator
from app.alerts.rules import load_rules
from app.config import settings
from app.db import engine

logger = logging.getLogger(__name__)


async def run_once(db: AsyncEngine) -> tuple[int, dispatcher.DrainResult]:
    """Run one evaluation pass and one delivery pass.

    Args:
        db: Engine to open this tick's transactions on.

    Returns:
        How many alerts were queued, and what delivery did.
    """
    # The rule catalog is lru_cached, so without this an operator's edit to
    # alert_rules.json would not take effect until the process restarted --
    # which would make "rules are data, no deploy needed" only half true. One
    # file read per tick is affordable in a loop; it would not be on a
    # request path, which is why the cache exists at all.
    load_rules.cache_clear()

    # The demo clock is cached in module state and refreshed only by
    # start()/stop(), both of which run in the API process. Without this the
    # worker would pin the anchors from its first tick and never see a demo
    # restart or a speed change, drifting away from the clock the dashboard
    # renders. See `clock.invalidate`.
    clock.invalidate()

    queued = 0
    async with db.begin() as conn:
        for facility_id in await core_repository.list_facility_ids(conn):
            result = await evaluator.evaluate_facility(conn, facility_id)
            queued += result.alerts_queued

    # Own pass, own transaction, same reasoning as evaluation-then-delivery
    # above: resolving a capacity_commitments row is independent of both, and
    # a failure here must not roll back alerts already queued or block mail
    # that has nothing to do with verification.
    async with db.begin() as conn:
        now = await clock.now(conn)
        for facility_id in await core_repository.list_facility_ids(conn):
            await verification.verify_capacity_commitments(conn, facility_id, now)

    async with db.begin() as conn:
        now = await clock.now(conn)
        drained = await dispatcher.drain_outbox(
            conn,
            now,
            max_attempts=settings.alert_max_attempts,
            batch_size=settings.alert_dispatch_batch_size,
        )

    return queued, drained


async def run_forever(db: AsyncEngine, tick_seconds: float) -> None:
    """Tick until cancelled, surviving any single tick's failure.

    A tick that raises is logged and the loop continues. The alternative --
    letting it propagate -- stops alerting entirely, and nothing would say so:
    a worker that has crashed and a warehouse with nothing wrong look
    identical from the outside.

    Args:
        db: Engine to run each tick on.
        tick_seconds: REAL seconds between passes.
    """
    logger.info("alert worker started, ticking every %.1fs", tick_seconds)
    while True:
        try:
            queued, drained = await run_once(db)
            if queued or drained.attempted:
                logger.info(
                    "tick: queued %d, sent %d, retrying %d, failed %d",
                    queued,
                    drained.sent,
                    drained.retrying,
                    drained.failed,
                )
        except asyncio.CancelledError:
            logger.info("alert worker stopping")
            raise
        except Exception:
            logger.exception("alert worker tick failed; continuing")

        await asyncio.sleep(tick_seconds)


def main() -> None:
    """Process entrypoint: `python -m app.alerts.worker`."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        asyncio.run(run_forever(engine, settings.alert_worker_tick_seconds))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
