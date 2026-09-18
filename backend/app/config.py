from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load backend settings from environment variables or `.env`.

    Defaults support the local Docker/PostgreSQL development setup.
    """

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    api_prefix: str = "/api"
    frontend_origin: str = "http://localhost:5173"
    database_url: str = (
        "postgresql+asyncpg://surgeguard:surgeguard@localhost:5432/surgeguard"
    )

    # n8n executes approved actions. Unset means approvals still persist as
    # PENDING and wait to be collected from GET /recovery-actions instead.
    n8n_recovery_webhook_url: str | None = None
    n8n_request_timeout_seconds: float = 5.0

    # Demo controls drive the simulator and reset operational data, so the whole
    # router stays disabled until this is set deliberately.
    simulator_url: str | None = None
    simulator_timeout_seconds: float = 60.0

    # A second, explicit switch on top of `simulator_url`. A configured
    # simulator URL used to be the only gate on demo controls, so a shared
    # environment template or a misconfigured deployment was enough to expose
    # destructive resets in production. Demo controls now require BOTH this
    # flag and a facility explicitly flagged `is_demo`.
    demo_mode: bool = True

    # -- Auth settings (phase 1). Owned by this file for the whole phase --
    # plans 02, 03 and 04 consume these and add none of their own.

    password_min_length: int = 8
    # The floor for the "length" rule. The full rule set -- length, uppercase,
    # lowercase, digit, symbol -- lives in `app/auth/passwords.PASSWORD_RULES`
    # and is served to the signup form by GET /auth/password-policy so the
    # form's live checklist cannot drift from what the server enforces.
    #
    # Composition rules are a deliberate product decision, not an oversight:
    # NIST SP 800-63B and OWASP ASVS V2.1.1 both advise length over forced
    # composition, because mandating character classes tends to produce
    # predictable shapes like "Password1!". That tradeoff was raised and the
    # composition checklist was chosen anyway, because the signup flow is built
    # around one and a checklist the server disagrees with is worse than none.
    # See the comment on PASSWORD_RULES.

    password_max_length: int = 128
    # A guard on Argon2 input size, not a security control.

    session_cookie_name: str = "surgeguard_session"

    session_cookie_secure: bool = False
    # False matches the local http setup. A `secure` cookie is
    # enforced by the CLIENT, so True here would make the plain-http
    # TestClient silently drop the cookie and every session test would fail
    # looking like a lookup bug. Production over https sets
    # SESSION_COOKIE_SECURE=true -- environment, not code.

    session_lifetime_seconds: int = 60 * 60 * 12
    # 12 hours. Long enough that a demo never re-authenticates mid-run, short
    # enough to bound a stolen cookie. Named here rather than buried in
    # sessions.py precisely so it is easy to find and change.

    remembered_session_lifetime_seconds: int = 60 * 60 * 24 * 30
    # 30 days. Used instead of session_lifetime_seconds only when login's
    # "remember me" is checked -- same cookie, same sessions table, just a
    # longer expires_at (see create_session's ttl_seconds parameter).

    service_token: str | None = None
    # None, never "". An empty-string default would let a caller sending an
    # empty X-Service-Token header match an unconfigured deployment. None
    # fails closed. Consumed by plan 03.

    # -- Email settings (phase 2). SMTP, token lifetimes, and link bases. --

    smtp_host: str | None = None
    # None, never "". This is the switch between the console backend and a
    # real SMTP send (D-05): None means "not configured", so dev machines
    # and the test suite fall back to logging the message instead of ever
    # erroring or opening a socket. An empty string would be a
    # configured-but-broken host, a different failure mode entirely.
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_sender: str = "surgeguard@example.invalid"
    smtp_timeout_seconds: float = 10.0
    # Mirrors n8n_request_timeout_seconds above: smtplib.SMTP() has NO
    # timeout by default, and a hung connection against an unreachable relay
    # would permanently consume one of Starlette's threadpool tokens without
    # this.

    verification_token_lifetime_seconds: int = 60 * 60 * 24  # 24h
    reset_token_lifetime_seconds: int = 60 * 60  # 1h
    # Reset is strictly shorter than verification: a reset link can seize an
    # account outright (change the password, lock the real owner out), while
    # a verification link can only flip a boolean nobody currently gates on.
    # The narrower window bounds how long a leaked reset link stays useful.

    frontend_base_url: str | None = None
    # Set (e.g. http://localhost:5173) to send emailed links to the frontend's
    # /verify-email and /set-new-password pages, which call the backend.
    public_base_url: str = "http://localhost:8000"
    # Links fall back to this backend URL when frontend_base_url is unset
    # (D-08/D-09), so the backend stays demoable standalone with no frontend.

    # -- Seeded demo account (phase 3, ACCESS-03) --
    #
    # A durable local demo administrator. Seeding happens on every backend
    # startup, after the compose migration service has prepared the database,
    # so a fresh Postgres volume gets this account automatically. It is not
    # baked into a migration: hashing belongs to application startup.
    demo_user_email: str = "demo@surgeguard.local"
    demo_user_password: str = "SurgeGuardDemo1!"

    # -- Alerting settings. Consumed by app/alerts/. --

    alert_max_attempts: int = 3
    # How many times one outbox row may be tried before it is marked FAILED
    # and stops being retried. Bounded on purpose: Brevo's free tier allows
    # 300 emails a day, shared with signup verification and password reset,
    # so an unbounded retry against a persistently broken address would eat
    # the day's allowance and take the real alerts down with it.

    alert_worker_tick_seconds: float = 15.0
    # REAL seconds between worker passes, not simulated ones -- this paces a
    # loop, it is not a deadline in the modelled world. Cooldowns are the
    # opposite: those are simulated, so they compress with the demo speed.
    # 15s is frequent enough that a 25x run surfaces an alert while somebody
    # is still watching, and cheap enough to leave running.

    alert_dispatch_batch_size: int = 20
    # Rows one drain pass claims. Small enough that a slow SMTP relay cannot
    # hold a transaction open across the whole backlog.

    @field_validator(
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "frontend_base_url",
        "demo_user_email",
        "demo_user_password",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty string as "not configured", the same as unset.

        docker-compose passes these through as `${SMTP_HOST:-}`, which
        resolves to an EMPTY STRING rather than leaving the variable unset -
        compose has no way to conditionally omit a key. Without this, an
        unconfigured stack would arrive here as `smtp_host == ""`, the
        mailer's `smtp_host is None` check would be False, and it would try
        to open an SMTP connection to an empty host instead of falling back
        to the console. That fallback is a safety property, not a
        convenience, so the distinction is collapsed here at the source
        rather than left for every consumer to remember.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def allowed_frontend_origins(self) -> list[str]:
        """Return configured CORS origins from a comma-separated setting.

        A single origin remains valid, while demos can add deployed or remote clients.
        """
        return [origin.strip() for origin in self.frontend_origin.split(",") if origin.strip()]


settings = Settings()
