"""Read-only explanation API for already-computed recovery plans."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.dependencies import require_session
from app.db import get_connection
from app.gemini import explain_recovery_plan
from app.routers.simulation import get_recovery_plans
from app.schemas import RecoveryChatRequest, RecoveryChatResponse

router = APIRouter(tags=["recovery-chat"])


@router.post(
    "/recovery-chat",
    response_model=RecoveryChatResponse,
    dependencies=[Depends(require_session)],
)
async def recovery_chat(
    request: RecoveryChatRequest,
    facility_id: str,
    projection_horizon_minutes: int = Query(default=240, gt=0, le=24 * 60),
    conn: AsyncConnection = Depends(get_connection),
) -> RecoveryChatResponse:
    """Explain an existing recovery plan using backend-generated data.

    Gemini is an explanation layer only. It does not calculate operational
    truth, choose a plan, approve an action, or execute anything.
    """
    recovery_plans = await get_recovery_plans(
        facility_id=facility_id,
        projection_horizon_minutes=projection_horizon_minutes,
        conn=conn,
    )

    selected_plan = next(
        (
            item
            for item in recovery_plans.plans
            if item.plan_id == request.plan_id
        ),
        None,
    )

    if selected_plan is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown or unavailable recovery plan {request.plan_id}",
        )

    do_nothing_plan = next(
        (
            item
            for item in recovery_plans.plans
            if item.plan_id == "do-nothing"
        ),
        None,
    )

    plan_context = {
        "facility_id": facility_id,
        "projection_horizon_minutes": projection_horizon_minutes,
        "selected_plan": selected_plan.model_dump(mode="json"),
        "do_nothing_plan": (
            do_nothing_plan.model_dump(mode="json")
            if do_nothing_plan is not None
            else None
        ),
        "comparison_plans": [
            item.model_dump(mode="json")
            for item in recovery_plans.plans
            if item.plan_id != selected_plan.plan_id
        ],
        "unavailable_levers": [
            item.model_dump(mode="json")
            for item in recovery_plans.unavailable_levers
        ],
    }

    try:
        answer = await explain_recovery_plan(
            question=request.question,
            plan_context=plan_context,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="recovery explanation service unavailable",
        )

    return RecoveryChatResponse(
        plan_id=selected_plan.plan_id,
        answer=answer,
        grounded=True,
    )
