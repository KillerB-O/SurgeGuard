"""FastAPI dependencies that resolve a caller's identity from a session
cookie or, for n8n, a service token.

Applied PER-ENDPOINT only (D-10) -- never registered as a global dependency
or blanket middleware. `require_session` is a human caller's identity;
`require_service_token` and `require_session_or_service_token` (added by
plan 03) authenticate or admit the machine caller without ever resolving a
user id for it -- see each function's docstring.
"""

import secrets

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyCookie, APIKeyHeader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.sessions import lookup_session
from app.config import settings
from app.db import get_connection

# `auto_error=False` is deliberate: with it unset (the default `True`), a
# missing cookie would make FastAPI itself raise a 403 before this function
# ever runs, so a bare "no cookie" and an "invalid/expired cookie" would come
# back with two different status codes and two different bodies from two
# different code paths. With `auto_error=False`, a missing cookie resolves to
# `None` and `require_session` owns the single 401 it emits for every failure
# mode. This also lets plan 03 reuse this exact scheme object inside an OR
# with the service-token check for `GET /api/recovery-actions` (D-11) without
# a mismatched auto-error short-circuiting that OR.
_cookie_scheme = APIKeyCookie(name=settings.session_cookie_name, auto_error=False)

# Same `auto_error=False` reasoning as `_cookie_scheme` above, and the reason
# an OR between the two is expressible at all: a missing `X-Service-Token`
# header resolves to `None` instead of FastAPI raising its own 403 first, so
# `require_session_or_service_token` owns the single 401 it emits.
_header_scheme = APIKeyHeader(name="X-Service-Token", auto_error=False)


async def require_session(
    session_token: str | None = Security(_cookie_scheme),
    conn: AsyncConnection = Depends(get_connection),
) -> str:
    """Resolve the authenticated user's id from the session cookie, or 401.

    Args:
        session_token: The raw cookie value, or `None` if absent (see the
            `auto_error=False` note above).
        conn: Request-scoped transaction, used to look the token up.

    Returns:
        The authenticated user's id.

    Raises:
        HTTPException: 401 if the cookie is missing, unknown, revoked, or its
            session has expired -- `lookup_session` collapses all of those
            into the same `None` result, and this function does not
            distinguish them in the response either. A more specific error
            would tell a caller which guess to make next.
    """
    if session_token is not None:
        user_id = await lookup_session(conn, session_token)
        if user_id is not None:
            return user_id
    raise HTTPException(status_code=401, detail="not authenticated")


async def require_admin(
    user_id: str = Depends(require_session),
    conn: AsyncConnection = Depends(get_connection),
) -> str:
    """Resolve the authenticated user's id, requiring `users.is_admin`.

    `users.role` cannot serve this purpose: it is free text a person types
    into the signup form, so anyone could grant themselves any role by typing
    it. This checks a column the application controls instead, currently
    settable only by direct database access -- there is no self-service path
    to becoming an administrator.

    Args:
        user_id: The session's authenticated user, from `require_session`.
        conn: Request-scoped transaction.

    Returns:
        The authenticated user's id.

    Raises:
        HTTPException: 403 if the user is not an administrator.
    """
    result = await conn.execute(
        text("SELECT is_admin FROM users WHERE id = :id"), {"id": user_id}
    )
    row = result.first()
    if row is None or not row[0]:
        raise HTTPException(status_code=403, detail="administrator access required")
    return user_id


def _token_matches(candidate: str, configured: str) -> bool:
    """Constant-time equality check for the service token (D-09).

    `secrets.compare_digest` performs a fixed-time byte comparison rather
    than the early-exit comparison `==` (or `str.__eq__`) performs -- a naive
    comparison returns as soon as it finds the first differing byte, and that
    timing difference is measurable by a remote caller making enough
    requests, letting the shared secret leak one character at a time. Never
    compare the presented token against the configured secret with the plain
    equality operator anywhere in this module: it looks like ordinary
    correct code in review and is exactly the mistake D-09 exists to rule out.

    `compare_digest` raises `TypeError` when given a `str` containing
    non-ASCII characters (its ASCII-only fast path cannot encode them safely
    for a fixed-time comparison). A caller sending such a value is simply
    wrong, not exceptional, so that `TypeError` is caught here and turned
    into an ordinary mismatch -- a malformed header must 401, never 500.

    Args:
        candidate: The value presented by the caller.
        configured: The value `settings.service_token` was configured with.

    Returns:
        Whether the two values are identical, compared in constant time.
    """
    try:
        return secrets.compare_digest(candidate, configured)
    except TypeError:
        return False


async def require_service_token(token: str | None = Security(_header_scheme)) -> None:
    """Authenticate n8n via a single static shared secret (D-08).

    Returns `None`, never a user id: the service token identifies "n8n", not
    a person, so no handler behind this dependency can mistake a machine
    caller for an authenticated user. `/api/me` and every future
    session-gated endpoint stay unreachable with this token no matter how it
    is presented, because nothing here ever calls `lookup_session` or returns
    anything a caller identity could be built from.

    Declares NO database dependency on purpose. That is what lets a bad or
    missing token 401 without ever touching Postgres, and what keeps an
    unauthenticated poll off the connection pool entirely -- see
    `tests/test_service_token.py` for the assertion that this resolves before
    `Depends(get_connection)` on every endpoint that uses only this
    dependency.

    Args:
        token: The raw `X-Service-Token` header value, or `None` if absent
            (see `_header_scheme`'s `auto_error=False`).

    Raises:
        HTTPException: 401 for every rejection cause -- missing header,
            wrong value, or an unconfigured deployment (`settings.service_token
            is None`, D-08's fail-closed default). Read `settings.service_token`
            fresh on every call rather than caching it at import, since tests
            (and, in principle, a future runtime reconfiguration) mutate it
            after the module has already been imported.
    """
    configured = settings.service_token
    if configured is None or token is None or not _token_matches(token, configured):
        raise HTTPException(status_code=401, detail="missing or invalid service token")


async def require_session_or_service_token(
    session_token: str | None = Security(_cookie_scheme),
    service_token: str | None = Security(_header_scheme),
    conn: AsyncConnection = Depends(get_connection),
) -> None:
    """D-11: accept EITHER a valid service token OR a valid session.

    `GET /api/recovery-actions` is the one endpoint in the codebase both n8n
    (polling with a token, on a schedule -- the higher-frequency caller) and
    the frontend (calling with a session cookie) hit on the same URL, and
    neither caller knows the other exists. The service token is checked
    FIRST because it costs only an in-process string comparison with no
    database round trip, whereas the cookie path needs `lookup_session`.

    Like `require_service_token`, this returns `None` rather than a user id.
    A token-authenticated call has no identity beyond "n8n"; a
    cookie-authenticated call's identity is intentionally not surfaced here
    either, because the handler this guards does not need one -- keeping the
    return shape identical between success paths is what makes "either
    credential" collapse to one boolean outcome instead of two
    differently-shaped ones.

    This dependency's signature includes `Depends(get_connection)` (the same
    shape `require_session` already uses) unconditionally, even on the
    token-only path that does not need it: FastAPI resolves that connection
    once per request and the handler needs one anyway, so the token path
    costs nothing extra in the common case. The cost shows up only when
    Postgres itself is unreachable -- see this function's note in
    `tests/test_service_token.py` on why a token-only call to this one
    endpoint surfaces a 503 instead of a 401 in that situation, unlike the
    five `require_service_token`-only endpoints.

    Known cosmetic wart, deliberately not engineered around: OpenAPI's
    security array expresses AND across schemes, not OR, so the generated
    docs will render both the cookie and header schemes as required for this
    one operation. That is a docs-rendering inaccuracy, not a runtime one --
    splitting this into two routes to make the docs accurate would break the
    single URL both callers already use.

    Args:
        session_token: The raw cookie value, or `None` if absent.
        service_token: The raw `X-Service-Token` header value, or `None` if absent.
        conn: Request-scoped transaction, used only by the cookie path.

    Raises:
        HTTPException: 401 only when BOTH credentials fail -- there is no
            branch in which neither check succeeds and the request still
            proceeds.
    """
    configured = settings.service_token
    if (
        configured is not None
        and service_token is not None
        and _token_matches(service_token, configured)
    ):
        return

    if session_token is not None:
        user_id = await lookup_session(conn, session_token)
        if user_id is not None:
            return

    raise HTTPException(status_code=401, detail="missing or invalid credentials")
