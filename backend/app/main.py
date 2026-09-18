"""Create the FastAPI app and register its routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.alerts import router as alerts
from app.auth import router as auth
from app.auth.seed import seed_demo_user
from app.config import settings
from app.db import engine
from app.routers import demo, events, health, reads, simulation


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Seed the demo account (ACCESS-03) before the app starts serving traffic.

    Runs once per process start, not per request. `seed_demo_user` never
    raises -- see its docstring -- so a database that is not yet reachable at
    startup cannot prevent the application from coming up and serving
    `/health` (ACCESS-04).
    """
    await seed_demo_user(engine)
    yield


app = FastAPI(title="SurgeGuard Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Keep database outages distinguishable from application errors.
@app.exception_handler(OperationalError)
@app.exception_handler(ConnectionError)
async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a retryable response for database connectivity failures.

    Application errors are intentionally allowed to use FastAPI's normal handling.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": "database temporarily unavailable, please retry"},
    )


app.include_router(health.router)
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(events.router, prefix=settings.api_prefix)
app.include_router(reads.router, prefix=settings.api_prefix)
app.include_router(simulation.router, prefix=settings.api_prefix)
app.include_router(demo.router, prefix=settings.api_prefix)
app.include_router(alerts.router, prefix=settings.api_prefix)
