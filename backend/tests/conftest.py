"""Provide pytest fixtures and point the suite at a dedicated test database.

The integration tests truncate tables on every test. Pointing them at the
database the demo runs on destroys it, so the suite resolves its own database
and refuses to run against anything not named as one.
"""

import asyncio
import os

import asyncpg
import pytest

from app.config import settings

# A database the suite is allowed to truncate must be named for the job.
TEST_DATABASE_SUFFIX = "_test"


def _as_test_database_url(url: str) -> str:
    """Return the same DSN pointed at the matching test database.

    Args:
        url: Configured application database URL.

    Returns:
        The URL with a test-suffixed database name.
    """
    base, _, database = url.rpartition("/")
    name, sep, query = database.partition("?")
    if name.endswith(TEST_DATABASE_SUFFIX):
        return url
    return f"{base}/{name}{TEST_DATABASE_SUFFIX}{sep}{query}"


def _resolve_test_database_url() -> str:
    """Choose the database the suite may truncate, and refuse anything else.

    Raises:
        RuntimeError: If the resolved database is not a test database.
    """
    url = os.getenv("TEST_DATABASE_URL") or _as_test_database_url(settings.database_url)
    database = url.rpartition("/")[2].partition("?")[0]
    if not database.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(
            f"refusing to run destructive tests against database {database!r}: "
            f"the name must end with {TEST_DATABASE_SUFFIX!r}. "
            "Set TEST_DATABASE_URL to a dedicated database."
        )
    return url


# Applied at import, before any test module imports app.db and builds its engine.
settings.database_url = _resolve_test_database_url()

# A fixed, known value for the suite's own X-Service-Token header. Production
# leaves `settings.service_token` at its `None` default (D-08, fail closed) --
# an unconfigured deployment must never authenticate anyone, including a test
# runner that forgot to set one. The suite has to configure an explicit value
# for the same reason plan 03's dependency exists at all: there is no way to
# exercise "a valid token is accepted" without one. Read by
# tests/test_service_token.py and imported by test_events_integration.py and
# test_interventions.py so every TestClient those files construct carries it
# as a default header (see plan 03's task 1).
TEST_SERVICE_TOKEN = "test-service-token"
settings.service_token = TEST_SERVICE_TOKEN


@pytest.fixture(scope="session")
def require_database():
    """Skip integration tests when the test database is unavailable -- unless
    the caller demanded proof it ran.

    Two modes exist on purpose. A bare `pytest` run must still SKIP cleanly for
    a contributor who has never set up Postgres -- that is the whole reason
    this fixture exists rather than letting every integration test crash with
    a raw connection error. But a green, skipped suite is not evidence of
    anything: INTEGRATION-NOTES.md section 13/18 records that this exact
    fixture once hid 73 integration tests behind a silent skip for an entire
    phase, because a native Postgres install shadowed the Docker Postgres on
    the same host port and nobody noticed the suite had stopped proving what
    it claimed to prove. Setting `GSD_REQUIRE_DATABASE=1` is how a
    verification gate (a human or an agent checking "did this actually run
    against a database") tells this fixture that a skip here is not
    acceptable -- it must fail loudly instead, with the same remediation
    commands, so the gate itself cannot be satisfied by an unreachable
    database.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    database = dsn.rpartition("/")[2].partition("?")[0]

    async def _check() -> None:
        """Open and close a throwaway connection for the availability check."""
        conn = await asyncpg.connect(dsn)
        await conn.close()

    try:
        asyncio.run(_check())
    except Exception as exc:  # noqa: BLE001 -- any connection failure means "skip" or "fail"
        message = (
            f"test database {database!r} not reachable, skipping integration tests: {exc}\n"
            f"Create it once with:\n"
            f"  docker compose exec postgres createdb -U surgeguard {database}\n"
            f"  DATABASE_URL=<dsn ending in /{database}> alembic upgrade head"
        )
        if os.getenv("GSD_REQUIRE_DATABASE") == "1":
            pytest.fail(
                "GSD_REQUIRE_DATABASE=1: this run was asked to prove database "
                f"behaviour and could not reach Postgres.\n{message}"
            )
        pytest.skip(message)
