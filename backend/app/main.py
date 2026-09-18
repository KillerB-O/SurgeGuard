"""Create the FastAPI app and register its routers."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage database lifecycle on startup and shutdown."""
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    yield
    await engine.dispose()


app = FastAPI(title="SurgeGuard Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.exception_handler(OperationalError)
@app.exception_handler(ConnectionError)
async def database_unavailable_handler(_: Request, __: Exception) -> JSONResponse:
    """Return a retryable response for database connectivity failures.

    Application errors are intentionally allowed to use FastAPI's normal handling.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": "database temporarily unavailable, please retry"},
    )

