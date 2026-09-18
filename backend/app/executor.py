"""Hand approved recovery actions to the n8n execution workflow.

The approval endpoint only records intent. Execution belongs to n8n, so this
module is the single outbound hop that starts it.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def request_execution(action_id: str, plan_id: str, facility_id: str) -> None:
    """Ask n8n to execute one approved recovery action.

    Runs after the approval transaction has committed, so n8n can read the
    action back through `GET /recovery-actions/{action_id}`.

    A failure here is deliberately not fatal and never changes the action's
    state: the action stays PENDING and remains collectable from
    `GET /recovery-actions`, which is the backstop when n8n is unreachable or no
    webhook is configured. Marking it FAILED would claim an execution attempt
    that never reached the workflow.

    Args:
        action_id: Persisted action to execute.
        plan_id: Recovery plan the operator approved.
        facility_id: Facility the action applies to.
    """
    if not settings.n8n_recovery_webhook_url:
        logger.info(
            "no n8n webhook configured; action %s awaits collection from "
            "GET /recovery-actions",
            action_id,
        )
        return

    payload = {"action_id": action_id, "plan_id": plan_id, "facility_id": facility_id}

    try:
        async with httpx.AsyncClient(timeout=settings.n8n_request_timeout_seconds) as client:
            response = await client.post(settings.n8n_recovery_webhook_url, json=payload)
            response.raise_for_status()
    except httpx.HTTPError:
        logger.exception(
            "could not hand action %s to n8n; it stays PENDING for collection", action_id
        )
    else:
        logger.info("handed action %s to n8n for execution", action_id)
