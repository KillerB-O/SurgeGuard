"""Operator-managed alert recipients: who gets told when a rule fires."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from app.alerts import repository
from app.auth.dependencies import require_session
from app.db import get_connection
from app.schemas import (
    AlertHistoryEntry,
    AlertHistoryResponse,
    AlertRecipientCreate,
    AlertRecipientResponse,
    AlertRecipientsResponse,
)

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get(
    "/recipients",
    response_model=AlertRecipientsResponse,
    # Per-route, never a global dependency or middleware. n8n calls this
    # backend with a service token and no session, so a global gate would
    # break event ingestion and recovery execution -- the structural guard in
    # tests/test_service_token.py asserts `app.router.dependencies == []`.
    dependencies=[Depends(require_session)],
)
async def list_recipients(
    conn: AsyncConnection = Depends(get_connection),
) -> AlertRecipientsResponse:
    """List everyone currently on the alert list.

    Args:
        conn: Request-scoped database transaction.

    Returns:
        The active recipients, oldest first.
    """
    rows = await repository.list_recipients(conn)
    return AlertRecipientsResponse(
        recipients=[AlertRecipientResponse(**row) for row in rows]
    )


@router.post(
    "/recipients",
    response_model=AlertRecipientResponse,
    status_code=201,
    dependencies=[Depends(require_session)],
)
async def add_recipient(
    payload: AlertRecipientCreate,
    conn: AsyncConnection = Depends(get_connection),
) -> AlertRecipientResponse:
    """Add somebody to the alert list.

    Args:
        payload: Validated name, address, and severity floor.
        conn: Request-scoped database transaction.

    Returns:
        The stored recipient.

    Raises:
        HTTPException: 409 if the address is already on the active list.
            Deliberately the same status signup uses for an already-registered
            email; this list is operator-visible by definition, so there is no
            enumeration concern to hide behind a vaguer response.
    """
    try:
        row = await repository.add_recipient(
            conn, payload.name, payload.email, payload.min_level
        )
    except repository.DuplicateRecipientError:
        raise HTTPException(
            status_code=409, detail=f"{payload.email} is already on the alert list"
        )
    return AlertRecipientResponse(**row)


@router.delete(
    "/recipients/{recipient_id}",
    status_code=204,
    dependencies=[Depends(require_session)],
)
async def remove_recipient(
    recipient_id: str,
    conn: AsyncConnection = Depends(get_connection),
) -> Response:
    """Remove somebody from the alert list.

    A soft delete -- see `repository.deactivate_recipient`. An unknown id and
    an already-removed one both return 204 rather than 404: the caller asked
    for this person not to be on the list, and after either outcome they are
    not. Answering 404 for one and 204 for the other would report a
    difference that does not matter to the caller and would confirm whether
    an id had ever existed.

    Args:
        recipient_id: Recipient to remove.
        conn: Request-scoped database transaction.

    Returns:
        An empty 204 response.
    """
    await repository.deactivate_recipient(conn, recipient_id)
    return Response(status_code=204)


@router.get(
    "/history",
    response_model=AlertHistoryResponse,
    dependencies=[Depends(require_session)],
)
async def list_alert_history(
    limit: int = 50,
    conn: AsyncConnection = Depends(get_connection),
) -> AlertHistoryResponse:
    """List recently decided alerts and whether they were delivered.

    This is what makes the outbox worth having from an operator's seat: a
    failed send is a row they can see, with the reason, rather than a line in
    a container log they will never read.

    Args:
        limit: Maximum alerts to return, newest first.
        conn: Request-scoped database transaction.

    Returns:
        Recent alerts with their delivery state.
    """
    rows = await repository.list_recent_alerts(conn, min(max(limit, 1), 200))
    return AlertHistoryResponse(alerts=[AlertHistoryEntry(**row) for row in rows])
