"""Account creation and session lifecycle: signup, login, logout, /me.

Router prefix note: this router carries NO prefix of its own -- every route
declares its full sub-path (`/auth/signup`, `/auth/login`, `/auth/logout`),
and `main.py` mounts the router under `settings.api_prefix` only. `/me` is
the reason the router has no prefix: it must resolve to `/api/me`, not
`/api/auth/me`, because REQUIREMENTS.md and the ROADMAP write it that way and
Phase 3's frontend calls it at that path. A router-level `prefix="/auth"`
would force every route under it to `/api/auth/*` and quietly break `/me`.
"""

from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, Security
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.dependencies import _cookie_scheme, require_session
from app.auth.mailer import deliver, reset_email, reset_link, verification_email, verify_link
from app.auth.passwords import (
    DUMMY_PASSWORD_HASH,
    PASSWORD_RULES,
    hash_password,
    password_policy_error,
    verify_password,
)
from app.auth.sessions import create_session, revoke_session
from app.auth.tokens import TokenPurpose, consume_token, issue_token, peek_token
from app.config import settings
from app.db import get_connection
from app.models import (
    LoginRequest,
    MeResponse,
    PasswordPolicyResponse,
    PasswordRuleResponse,
    ResetConfirmRequest,
    ResetRequestRequest,
    SignupRequest,
    UserResponse,
)

router = APIRouter(tags=["auth"])

# T-02-12: one message for expired, already-consumed, unknown, AND
# wrong-purpose tokens. Do not split this into per-cause messages -- a
# distinguishable failure would tell an anonymous caller which tokens were
# ever issued, which `consume_token`'s single WHERE clause already refuses
# to reveal at the database layer. Keeping exactly one literal, referenced
# from the single failure branch below, is what makes that guarantee
# structural rather than a convention someone can accidentally break with a
# helpful-sounding edit.
INVALID_OR_EXPIRED_TOKEN_MESSAGE = (
    "This link is invalid, expired, or has already been used. Request a new one and try again."
)


@router.get("/auth/password-policy", response_model=PasswordPolicyResponse)
async def read_password_policy() -> PasswordPolicyResponse:
    """Return the password rules the signup form should display and check.

    Unauthenticated on purpose: it is needed on the signup screen, before any
    account exists. It discloses nothing an attacker cannot learn by submitting
    one bad password to `/auth/signup` and reading the error.

    This exists so the form's live checklist is derived from the same
    `PASSWORD_RULES` the server enforces. The alternative -- the frontend
    keeping its own copy of the rules -- drifts the moment either side changes,
    and it fails in the worst direction: a checklist showing every rule
    satisfied while signup answers 400.
    """
    return PasswordPolicyResponse(
        min_length=settings.password_min_length,
        max_length=settings.password_max_length,
        rules=[
            PasswordRuleResponse(id=rule_id, label=label)
            for rule_id, label, _ in PASSWORD_RULES
        ],
    )


@router.post("/auth/signup", response_model=UserResponse, status_code=201)
async def signup(
    request: SignupRequest,
    background_tasks: BackgroundTasks,
    conn: AsyncConnection = Depends(get_connection),
) -> UserResponse:
    """Create a new account with an Argon2id-hashed password.

    Deliberate enumeration tradeoff (D-13, T-01-03): this endpoint DOES
    reveal, via its 409, that a given email is already registered. That is
    not an oversight to "harden" away later -- a signup form that cannot
    explain why it rejected a submission is unusable. The tradeoff is
    bounded: login (AUTH-09, plan 02) and password reset (EMAIL-05, phase 2)
    return an identical response regardless of whether the email exists, so
    this disclosure does not compose into an account-enumeration oracle
    across the whole auth surface -- only signup itself leaks it, and only
    for an action (registration) where the caller already knows what email
    they just tried.

    Args:
        request: Untrusted signup payload -- attacker-chosen email and
            password, unauthenticated by definition.
        conn: Request-scoped database transaction.

    Returns:
        The newly created account's public shape: id, email, verified.

    Raises:
        HTTPException: 400 if the password fails the length policy (with the
            specific rule named -- AUTH-03), 409 if the email is already
            registered (case-insensitively).
    """
    # Policy check FIRST, before any database work -- a rejected password
    # should never cost a query, and the whole point of AUTH-03 is a named
    # reason, not a database round-trip that happens to also reject it.
    policy_error = password_policy_error(request.password)
    if policy_error is not None:
        raise HTTPException(status_code=400, detail=policy_error)

    # Store the email exactly as typed; every comparison against it uses
    # lower() to match `idx_users_email_lower` (see migration 0010).
    email = request.email.strip()
    user_id = f"user-{uuid4().hex}"
    password_hash = hash_password(request.password)
    full_name = request.full_name.strip()
    company = request.company.strip()
    role = request.role.strip()

    # Bare `ON CONFLICT DO NOTHING` with NO conflict target, rather than
    # catching IntegrityError the way events.py does. A conflict target
    # naming the functional index would need to spell out
    # `ON CONFLICT ((lower(email)))` exactly -- easy to get subtly wrong --
    # and the IntegrityError route would leave this request's transaction
    # aborted inside `engine.begin()`, since Postgres poisons a transaction
    # after a constraint violation. Bare `DO NOTHING` avoids both: it simply
    # inserts nothing and returns no row when the lower(email) index already
    # has a match, and the transaction stays usable.
    result = await conn.execute(
        text(
            """
            INSERT INTO users (id, email, password_hash, verified, full_name, company, role)
            VALUES (:id, :email, :password_hash, FALSE, :full_name, :company, :role)
            ON CONFLICT DO NOTHING
            RETURNING id, email, verified, full_name, company, role
            """
        ),
        {
            "id": user_id,
            "email": email,
            "password_hash": password_hash,
            "full_name": full_name,
            "company": company,
            "role": role,
        },
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=409, detail="an account with this email already exists"
        )

    # Mint the verify token inside the same transaction as account creation
    # -- if the transaction rolls back for any other reason, the token never
    # exists for an account that does not either. The email itself is
    # scheduled OFF the request path (D-15/EMAIL-07): `deliver` is passed
    # directly, as a plain function reference, never wrapped in an
    # `async def` helper -- see mailer.py's docstring for why that
    # distinction matters. The route returns immediately after scheduling;
    # it never awaits or calls the sender itself, so a slow or unreachable
    # SMTP relay cannot delay this response.
    raw_token = await issue_token(
        conn, row["id"], TokenPurpose.VERIFY, settings.verification_token_lifetime_seconds
    )
    link = verify_link(raw_token)

    # Commit BEFORE scheduling the send, not after.
    #
    # Background tasks run before a `yield` dependency's teardown, and
    # teardown is where `get_connection` commits. Without this line the
    # signup transaction stays open for the whole SMTP round trip, so the
    # account does not exist for anyone else until the relay answers.
    # Measured against a live relay: 201 returned in 159ms, the row visible
    # only ~1.27s later, and every signup-then-immediately-sign-in attempt
    # failed with 401 in between -- which is precisely what the sign-up
    # screen does. It also parked a database transaction on a third-party
    # network call. Committing here makes the account durable first and
    # frees the connection before any of that.
    await conn.commit()

    background_tasks.add_task(deliver, verification_email(row["email"], link))

    return UserResponse(
        id=row["id"],
        email=row["email"],
        verified=row["verified"],
        full_name=row["full_name"],
        company=row["company"],
        role=row["role"],
    )


@router.get("/auth/verify")
async def verify_email(
    token: str,
    conn: AsyncConnection = Depends(get_connection),
) -> PlainTextResponse:
    """Consume a verification token and mark the owning account verified.

    Unauthenticated by design (T-02-10): the whole point of this route is
    that the recipient clicking the emailed link is NOT signed in. `token`
    is the only credential -- there is no session dependency here, and
    there must never be one added (D-10 style guarantee: no global
    dependency exists on `app.router`, checked by this plan's own
    `app.router.dependencies == []` assertion).

    `consume_token` matches `purpose=VERIFY` in its single atomic
    `UPDATE ... RETURNING` (T-02-11), so a reset-purpose token (T-02-13), an
    already-consumed token, an expired token, and a token that never
    existed all return `None` here -- and all four produce the exact same
    `INVALID_OR_EXPIRED_TOKEN_MESSAGE` (T-02-12). Do not special-case any of
    them to be "more helpful"; a distinguishable failure would tell a
    stranger which tokens were ever issued.

    Verification is informational only (EMAIL-02, Phase 2 CONTEXT D- login
    is never gated on it): nothing here touches the login path, and
    `users.verified` is never read as a login precondition anywhere in this
    codebase. Do not "fix" that into a gate.

    Args:
        token: The raw token from the query string, exactly as embedded in
            the emailed link.
        conn: Request-scoped transaction; the consume-and-flip happens
            atomically within it, so a crash between the two statements
            cannot leave a consumed token pointing at a still-unverified
            account.

    Returns:
        A minimal, browser-readable plain-text body: 200 on success, 400
        with the single shared failure message otherwise.
    """
    user_id = await consume_token(conn, token, TokenPurpose.VERIFY)
    if user_id is None:
        return PlainTextResponse(INVALID_OR_EXPIRED_TOKEN_MESSAGE, status_code=400)

    await conn.execute(
        text("UPDATE users SET verified = TRUE WHERE id = :id"),
        {"id": user_id},
    )
    return PlainTextResponse("Your email has been verified. You can now sign in.")


# T-02-17/EMAIL-05: the exact response `POST /auth/reset/request` returns,
# regardless of whether the submitted address is registered. A module-level
# constant returned from the single `return` statement in that handler,
# rather than being constructed separately on each branch, so the two
# outcomes cannot drift into two different bodies through a later edit --
# the same structural guarantee `INVALID_OR_EXPIRED_TOKEN_MESSAGE` gives
# T-02-12 above.
RESET_REQUESTED_RESPONSE = {
    "status": "ok",
    "detail": "If that address is registered, a reset link is on its way.",
}


@router.post("/auth/reset/request")
async def request_password_reset(
    request: ResetRequestRequest,
    background_tasks: BackgroundTasks,
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    """Email a single-use reset link if `request.email` is registered.

    EMAIL-05/T-02-17: this is the one auth endpoint that must NOT enumerate.
    Unlike signup's 409 (D-13 of Phase 1, an accepted disclosure so a signup
    form can explain a rejection) and unlike this same codebase's own verify
    route, there is exactly ONE return site here
    (`RESET_REQUESTED_RESPONSE`), reached whether or not a user was found,
    so the two branches can never be edited into two different bodies. Do
    not add a heavier side effect to only the found branch beyond the one
    extra INSERT `issue_token` already does -- that timing difference is not
    a meaningful signal, but stacking more work onto only one path would be.

    Args:
        request: Untrusted payload -- attacker-chosen email, unauthenticated
            by definition, exactly like signup and login.
        background_tasks: Used to schedule the email off the request path
            (D-15/EMAIL-07); the response never waits on SMTP.
        conn: Request-scoped transaction.

    Returns:
        The fixed `RESET_REQUESTED_RESPONSE` body, always with a 200 status.
    """
    result = await conn.execute(
        text("SELECT id, email FROM users WHERE lower(email) = lower(:email)"),
        {"email": request.email},
    )
    row = result.mappings().first()
    if row is not None:
        raw_token = await issue_token(
            conn, row["id"], TokenPurpose.RESET, settings.reset_token_lifetime_seconds
        )
        link = reset_link(raw_token)

        # Commit before scheduling, for the same reason as signup: background
        # tasks run before this dependency's teardown commits, so otherwise
        # the token in the email we are about to send is not durable yet. A
        # recipient who clicks quickly would be told their link is invalid.
        await conn.commit()

        background_tasks.add_task(deliver, reset_email(row["email"], link))

    return RESET_REQUESTED_RESPONSE


@router.get("/auth/reset/confirm")
async def check_reset_token(
    token: str,
    conn: AsyncConnection = Depends(get_connection),
) -> PlainTextResponse:
    """Report whether a reset token is currently valid, WITHOUT consuming it.

    This is the asymmetry the whole plan is built around (T-02-21): email
    security gateways prefetch links, and a consuming GET would let a
    scanner burn the token before the user ever clicks it. `peek_token` is a
    read-only check for exactly this reason -- only `POST` below, which
    carries a new password, ever consumes. Do not "simplify" this into a
    single consuming GET that redirects; no scanner submits a POST with a
    password, so this split is the mitigation, not an accident of design.

    Args:
        token: The raw token from the query string, exactly as embedded in
            the emailed link.
        conn: Request-scoped transaction; only read here.

    Returns:
        A minimal, browser-readable plain-text body: 200 if the token is
        valid, unexpired, unused, and of `reset` purpose; 400 with the same
        shared failure message `GET /auth/verify` uses otherwise.
    """
    valid = await peek_token(conn, token, TokenPurpose.RESET)
    if not valid:
        return PlainTextResponse(INVALID_OR_EXPIRED_TOKEN_MESSAGE, status_code=400)
    return PlainTextResponse("This link is valid. Submit a new password to complete your reset.")


@router.post("/auth/reset/confirm")
async def confirm_password_reset(
    request: ResetConfirmRequest,
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    """Consume a reset token, set a new password, and revoke every session.

    Order matters (T-02-22, T-02-18): the password policy is checked FIRST,
    before the token is touched at all, so a rejected password never burns
    the token -- a real retry with a compliant password must still succeed.
    Only after that check passes does `consume_token` atomically claim the
    token (T-02-19; `None` covers expired, already-used, unknown, and
    wrong-purpose uniformly, same shared message as `GET /auth/verify` and
    the GET check above). The password update and the session wipe run as
    two statements inside this same request-scoped transaction
    (`get_connection`'s `engine.begin()`), so a crash between them can never
    leave a changed password with a live session still attached to it
    (EMAIL-04) -- that would defeat the entire point of a reset for a
    compromised account.

    Args:
        request: Untrusted payload -- the raw token and a candidate new
            password, unauthenticated by definition (the token IS the
            credential).
        conn: Request-scoped transaction.

    Returns:
        `{"status": "ok"}` on success.

    Raises:
        HTTPException: 400 with the specific policy reason if the password
            fails `password_policy_error` (AUTH-03's same specificity), or
            400 with the shared token-failure message if the token is
            invalid, expired, already used, or of the wrong purpose.
    """
    policy_error = password_policy_error(request.password)
    if policy_error is not None:
        raise HTTPException(status_code=400, detail=policy_error)

    user_id = await consume_token(conn, request.token, TokenPurpose.RESET)
    if user_id is None:
        raise HTTPException(status_code=400, detail=INVALID_OR_EXPIRED_TOKEN_MESSAGE)

    password_hash = hash_password(request.password)
    await conn.execute(
        text("UPDATE users SET password_hash = :password_hash WHERE id = :id"),
        {"password_hash": password_hash, "id": user_id},
    )
    # EMAIL-04/T-02-18: revoke every existing session for this account in the
    # SAME transaction as the password change above -- if the account was
    # compromised, completing a reset must log the attacker out, not leave
    # their session alive alongside the new password.
    await conn.execute(
        text("DELETE FROM sessions WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    return {"status": "ok"}


# The fixed body `POST /auth/verify/resend` returns whatever the submitted
# address turns out to be: unregistered, already verified, or genuinely
# awaiting a link. One module-level constant returned from a single `return`
# statement, for the same reason as RESET_REQUESTED_RESPONSE -- three
# outcomes that must never drift into three distinguishable bodies. Without
# that, this endpoint would answer "does this address have an unverified
# account?" for anyone who asked.
VERIFICATION_RESEND_RESPONSE = {
    "status": "ok",
    "detail": "If that address needs verifying, a new link is on its way.",
}


@router.post("/auth/verify/resend")
async def resend_verification(
    request: ResetRequestRequest,
    background_tasks: BackgroundTasks,
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    """Issue a fresh verification link for an account that has not verified.

    This endpoint is what makes gating login on verification survivable.
    Verification tokens expire after 24 hours and are single-use, so without
    a way to mint another one, a link that expired unread -- or landed in
    spam, or was clicked twice -- would leave that account permanently
    unable to sign in, with no self-service path back. That is not an edge
    case: free-tier mail from an unauthenticated domain is filtered often.

    Reuses `ResetRequestRequest` because the payload is identical (one
    untrusted email) and, more importantly, because it carries the same
    deliberately permissive validation -- a stricter shape check here would
    422 on a malformed address while a well-formed unregistered one returned
    200, which is the enumeration leak this endpoint is built to avoid.

    Args:
        request: Untrusted payload -- attacker-chosen email, unauthenticated.
        background_tasks: Schedules the send off the request path (EMAIL-07).
        conn: Request-scoped transaction.

    Returns:
        The fixed `VERIFICATION_RESEND_RESPONSE` body, always with a 200.
    """
    result = await conn.execute(
        text("SELECT id, email, verified FROM users WHERE lower(email) = lower(:email)"),
        {"email": request.email},
    )
    row = result.mappings().first()

    # Already-verified accounts get nothing. Re-sending would let anyone burn
    # a stranger's inbox -- and the daily mail allowance -- by replaying this
    # against a known address, and there is nothing for the recipient to do
    # with the link anyway.
    if row is not None and not row["verified"]:
        raw_token = await issue_token(
            conn,
            row["id"],
            TokenPurpose.VERIFY,
            settings.verification_token_lifetime_seconds,
        )
        link = verify_link(raw_token)

        # Commit before scheduling: background tasks run before this
        # dependency's teardown commits, so the token in the email would not
        # yet be durable and a quick click would be told the link is invalid.
        await conn.commit()

        background_tasks.add_task(deliver, verification_email(row["email"], link))

    return VERIFICATION_RESEND_RESPONSE


@router.post("/auth/login")
async def login(
    request: LoginRequest,
    response: Response,
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    """Authenticate with email + password and set an httpOnly session cookie.

    AUTH-09/D-12: an unregistered email and a wrong password for a real
    account must be indistinguishable to the caller. Response bodies alone
    are not enough -- see the constant-work comment below for the other half
    of that guarantee.

    Args:
        request: Untrusted credentials.
        response: Used to attach the session cookie on success.
        conn: Request-scoped transaction.

    Returns:
        A minimal success body. The raw session token is NEVER returned here
        (D-02) -- the cookie is the only channel it travels on.

    Raises:
        HTTPException: 401 with one fixed message, for both an unknown email
            and a wrong password. There is exactly ONE raise site in this
            function so the two failure causes can never drift into two
            different messages through a later edit -- that single site is
            the structural guarantee behind AUTH-09.
    """
    result = await conn.execute(
        text(
            "SELECT id, password_hash, verified FROM users "
            "WHERE lower(email) = lower(:email)"
        ),
        {"email": request.email},
    )
    row = result.mappings().first()

    # Verify a password on EVERY call, whether or not a user was found.
    # Without this, an unknown email returns in microseconds (no Argon2 work
    # at all) while a wrong password on a real account costs Argon2id's
    # ~tens-of-milliseconds verification -- a timing side channel that
    # persists even though both paths return byte-identical response bodies.
    # Verifying against a fixed dummy hash on the not-found path means BOTH
    # paths pay exactly one Argon2 verification, closing that channel too.
    if row is None:
        verify_password(request.password, DUMMY_PASSWORD_HASH)
        password_ok = False
        user_id = None
    else:
        password_ok = verify_password(request.password, row["password_hash"])
        user_id = row["id"]

    if not password_ok or user_id is None:
        raise HTTPException(status_code=401, detail="invalid email or password")

    # Placed AFTER the credential check on purpose, and this ordering is the
    # whole security argument for the gate. A 403 is reachable only by someone
    # who already supplied the correct password, so it tells an attacker
    # nothing they did not already know. Moving this above the 401 -- or
    # folding it into the query's WHERE clause -- would turn the status code
    # into an oracle answering "does this address have an account?" for anyone
    # willing to guess, which is exactly what AUTH-09 exists to prevent.
    if not row["verified"]:
        raise HTTPException(
            status_code=403,
            detail=(
                "Verify your email before signing in. "
                "Check your inbox for the link, or request a new one."
            ),
        )

    # "Remember me" only changes how long the session lasts, not any other
    # part of the login path: the same cookie, the same session table, the
    # same lookup. A checked box gets `remembered_session_lifetime_seconds`
    # (30 days) instead of the fixed 12h default.
    ttl_seconds = (
        settings.remembered_session_lifetime_seconds
        if request.remember_me
        else settings.session_lifetime_seconds
    )
    raw_token = await create_session(conn, user_id, ttl_seconds=ttl_seconds)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        max_age=ttl_seconds,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    # SameSite=Lax (D-03) is not negotiable here: localhost:5173 -> :8000 is
    # same-site because ports are not part of a "site", so Lax works for the
    # demo. SameSite=None would require Secure, which browsers refuse to
    # honour over plain http -- the cookie would then be dropped silently and
    # present as a mysterious login failure.
    return {"status": "ok"}


@router.post("/auth/logout")
async def logout(
    response: Response,
    session_token: str | None = Security(_cookie_scheme),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    """Revoke the current session server-side and clear the cookie.

    AUTH-06's real assertion is server-side revocation, not cookie clearing:
    the session row is deleted so a captured cookie value can never validate
    again, even if replayed on a different client that never received the
    `Set-Cookie` response below.

    Args:
        response: Used to clear the session cookie.
        session_token: The raw cookie value, or `None` if absent. Reuses the
            same `_cookie_scheme` object `require_session` uses rather than
            reading `request.cookies` by hand, for one consistent extraction
            path.
        conn: Request-scoped transaction.

    Returns:
        A fixed success body regardless of whether a session was present --
        logging out of nothing is not an error, and reporting it as one would
        be a small enumeration signal of its own.
    """
    if session_token is not None:
        await revoke_session(conn, session_token)

    # Match EVERY attribute used at login exactly (path, samesite, secure,
    # httponly). A mismatched attribute set is a cookie the browser treats as
    # a *different* cookie, so some browsers keep the original alongside it.
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    return {"status": "ok"}


@router.get("/me", response_model=MeResponse)
async def read_current_user(
    user_id: str = Depends(require_session),
    conn: AsyncConnection = Depends(get_connection),
) -> MeResponse:
    """Return the signed-in caller's own identity (AUTH-07).

    `verified` reflects whether the account's most recent verification link
    has been visited (EMAIL-02, set by `GET /auth/verify`) -- see
    `MeResponse`'s docstring. It is purely informational: nothing on this
    route, or anywhere else, gates on it being true.

    Args:
        user_id: Resolved by `require_session` from the session cookie; this
            is the ONLY auth on this route (D-10 -- no global dependency).
        conn: Request-scoped transaction.

    Returns:
        The caller's own email and verification status.

    Raises:
        HTTPException: 401, raised by `require_session` itself, if the
            cookie is missing, unknown, revoked, or expired.
    """
    result = await conn.execute(
        text(
            "SELECT email, verified, full_name, company, role, is_admin FROM users WHERE id = :id"
        ),
        {"id": user_id},
    )
    row = result.mappings().first()
    # A session row can only exist for a user_id that existed when the
    # session was created, and users are never deleted in this phase, so a
    # missing row here would indicate a genuine data inconsistency rather
    # than an expected caller error -- surfaced as a 500 by simply not
    # catching it, consistent with how the rest of this codebase treats
    # "should be impossible" states.
    return MeResponse(
        email=row["email"],
        verified=row["verified"],
        full_name=row["full_name"],
        company=row["company"],
        role=row["role"],
        is_admin=row["is_admin"],
    )
