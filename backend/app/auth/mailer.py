"""Build outgoing account emails and deliver them off the request path.

Two clearly separated halves, matching `app/executor.py`'s outbound-call
shape (read directly for this):

- `build_message` / `verification_email` are pure, no I/O, unit-testable
  with no database or socket (see `tests/test_mailer_unit.py`).
- `deliver` performs the actual dispatch and is the ONLY function that ever
  touches a socket or the log.

The sender MUST be a plain `def`, never `async def`. Starlette's
`BackgroundTask.__call__` inspects the scheduled callable: a coroutine
function is `await`ed directly on the request-serving event loop, while a
plain function is handed to `run_in_threadpool`. `smtplib` performs blocking
socket I/O with no `await` points, so an `async def` sender here would stall
every other in-flight request for the whole SMTP round trip.
`app/executor.py` schedules an `async def` for its n8n handoff, and that is
CORRECT there because `httpx.AsyncClient` is genuinely non-blocking -- do
NOT "fix" this module to look the same way.
"""

import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

from app.config import settings

logger = logging.getLogger(__name__)


def build_message(to: str, subject: str, body: str) -> EmailMessage:
    """Construct a plain-text message. Pure -- no I/O, no logging, no send.

    Uses `email.message.EmailMessage` (the modern stdlib API, not the legacy
    `email.mime` classes): its default policy raises `ValueError` on an
    embedded CR/LF in a header value, which is what closes the header-
    injection vector for an attacker-chosen `to` address (T-02-03). That
    exception must surface inside `deliver`'s try/except, not here, which is
    why `verification_email` builds the message eagerly but `deliver` is
    the one responsible for catching it (see that function's docstring).

    Args:
        to: Recipient address.
        subject: Message subject line.
        body: Plain-text message body.

    Returns:
        A fully-formed, unsent `EmailMessage`.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_sender
    msg["To"] = to
    msg.set_content(body)
    return msg


def link_base() -> str:
    """Return the base URL email links should point at.

    `frontend_base_url` is None until Phase 3 builds the routes that handle
    these links; until then every link points at this backend directly
    (D-08), which is what makes this phase demoable with no frontend at
    all. Once Phase 3 sets `frontend_base_url`, links flip there -- a
    config change, not a code change (D-09).
    """
    return settings.frontend_base_url or settings.public_base_url


def _token_link(frontend_path: str, backend_path: str, raw_token: str) -> str:
    """Build an emailed link carrying `raw_token`, for whichever side handles it.

    With `frontend_base_url` set, the link opens the frontend page built for
    it, which then calls the backend route itself. Without it, the link hits
    the backend route directly, exactly as before, so the backend stays
    demoable with no frontend running.
    """
    token = quote(raw_token, safe="")
    if settings.frontend_base_url:
        return f"{settings.frontend_base_url.rstrip('/')}{frontend_path}?token={token}"
    return f"{settings.public_base_url.rstrip('/')}{settings.api_prefix}{backend_path}?token={token}"


def verify_link(raw_token: str) -> str:
    """The link in a verification email: `/verify-email` or `GET /auth/verify`."""
    return _token_link("/verify-email", "/auth/verify", raw_token)


def reset_link(raw_token: str) -> str:
    """The link in a reset email: `/set-new-password` or `GET /auth/reset/confirm`.

    Both destinations only peek at the token; nothing is consumed until the
    new password is POSTed, so mail scanners prefetching the link are harmless.
    """
    return _token_link("/set-new-password", "/auth/reset/confirm", raw_token)


def verification_email(to: str, link: str) -> EmailMessage:
    """Build the specific message sent when a new account signs up.

    Args:
        to: The new account's email address.
        link: The single-use verification URL, already carrying the raw
            token as a query parameter.

    Returns:
        The unsent verification message.
    """
    return build_message(
        to=to,
        subject="Verify your SurgeGuard account",
        body=(
            f"Click the link below to verify your SurgeGuard account.\n\n"
            f"{link}\n\n"
            f"This link expires in 24 hours. If you did not create this "
            f"account, you can ignore this message."
        ),
    )


def reset_email(to: str, link: str) -> EmailMessage:
    """Build the message sent when an account requests a password reset.

    Args:
        to: The registered account's email address.
        link: The single-use reset URL, already carrying the raw token as a
            query parameter.

    Returns:
        The unsent reset message.
    """
    return build_message(
        to=to,
        subject="Reset your SurgeGuard password",
        body=(
            f"Click the link below to reset your SurgeGuard password.\n\n"
            f"{link}\n\n"
            f"This link expires in 1 hour and can only be used once. If you "
            f"did not request this, you can ignore this message."
        ),
    )


def send_message(message: EmailMessage) -> None:
    """Send `message`, choosing console or SMTP from configuration, and RAISE on failure.

    The transport itself, shared by both error contracts. `deliver` wraps this
    and swallows; `app/alerts/dispatcher.py` calls it directly because an
    outbox row cannot be marked SENT or FAILED by a function that reports
    neither. Keeping one implementation means the console fallback and the
    STARTTLS/login sequence cannot drift between the two callers.

    The choice of transport is `settings.smtp_host is None` (D-05): None is
    "not configured" and means log-only, never an error and never a real
    connection attempt. This is a safety property, not a convenience -- the
    test suite must never send a real email, and the console path is what
    every test and every unconfigured dev machine actually exercises.

    Blocking socket I/O with no `await` points. An async caller must hand it
    to a thread (`asyncio.to_thread`) rather than calling it directly, for
    the same reason this module's docstring gives for `deliver`.

    Args:
        message: A fully-built message from `build_message`.

    Raises:
        Exception: Whatever the transport raised. Callers that must not fail
            their caller use `deliver` instead.
    """
    if settings.smtp_host is None:
        # Log the DECODED body, not `message.as_string()`. A serialized
        # EmailMessage is quoted-printable: an `=` becomes `=3D` and long
        # lines are soft-wrapped with a trailing `=`, which mangles exactly
        # the thing a human reads this log for -- the link. A real mail
        # client decodes that transparently; a terminal does not, and the
        # console backend exists precisely so a person can read the mail.
        # Headers are logged separately so the recipient is still visible.
        logger.info(
            "EMAIL (console backend) To: %s | Subject: %s\n\n%s",
            message["To"],
            message["Subject"],
            message.get_content(),
        )
        return

    with smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
    ) as server:
        server.starttls()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)


def deliver(message: EmailMessage) -> None:
    """Send `message`, logging any failure rather than raising it (D-16).

    A plain `def`, never `async def` -- see this module's docstring. Passed
    directly to `background_tasks.add_task` at the call site with no
    intermediate wrapper, so Starlette's callable inspection sees this
    function itself.

    The ENTIRE call is inside one try/except, message serialization included:
    nothing here may ever raise past this function, because a background task
    that raises is swallowed by the ASGI server without ever reaching an HTTP
    response (the response for the triggering request was already sent).
    `logger.exception` gives the failure a place to actually be seen, matching
    `executor.py`'s `except httpx.HTTPError: logger.exception(...)` shape for
    the n8n handoff.

    This is the right contract for a verification or reset link, which the
    user can always request again. It is NOT the right contract for an alert
    nobody knows was supposed to arrive -- that path uses `send_message` and
    records the outcome in `alert_outbox`.

    Args:
        message: A fully-built message from `build_message`/
            `verification_email`.
    """
    try:
        send_message(message)
    except Exception:
        logger.exception("failed to deliver email to %s", message.get("To"))
