from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Return a lightweight liveness response without database access."""
    return {"status": "ok"}
