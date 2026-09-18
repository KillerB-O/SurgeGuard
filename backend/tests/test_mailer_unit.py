"""DB-free unit tests for the mailer's message shape and transport switch.

No `require_database` anywhere in this file: message construction and the
console/SMTP switch are pure and synchronous, so this suite must never touch
Postgres. This is also the test that proves D-05 -- with no SMTP configured,
`deliver()` opens no socket -- and D-16 -- a transport failure is logged, not
raised.
"""

import logging

from app.auth.mailer import build_message, deliver, verification_email


def test_build_message_has_recipient_subject_and_link_in_body():
    msg = build_message(
        to="new-user@example.com",
        subject="Verify your SurgeGuard account",
        body="Click to verify: http://localhost:8000/api/auth/verify?token=abc123",
    )
    assert msg["To"] == "new-user@example.com"
    assert msg["Subject"]
    assert "http://localhost:8000/api/auth/verify?token=abc123" in msg.get_content()


def test_verification_email_body_contains_the_token_exactly_once():
    link = "http://localhost:8000/api/auth/verify?token=only-once-token"
    msg = verification_email("new-user@example.com", link)

    body = msg.get_content()
    assert body.count("only-once-token") == 1
    assert msg["To"] == "new-user@example.com"
    assert msg["Subject"]


def test_deliver_with_no_smtp_configured_logs_and_opens_no_socket(monkeypatch, caplog):
    """D-05: the console backend is the default and must not touch a socket."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "smtp_host", None)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("smtplib.SMTP must not be constructed when smtp_host is None")

    monkeypatch.setattr("smtplib.SMTP", _fail_if_called)

    msg = verification_email("console-only@example.com", "http://localhost:8000/x")
    with caplog.at_level(logging.INFO):
        deliver(msg)

    assert any("console-only@example.com" in record.getMessage() for record in caplog.records)


def test_deliver_swallows_and_logs_a_transport_exception(monkeypatch, caplog):
    """D-16: a send failure is logged, never raised past deliver()."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(mailer.settings, "smtp_username", None)

    class _ExplodingSMTP:
        def __init__(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", _ExplodingSMTP)

    msg = verification_email("bounces@example.com", "http://localhost:8000/x")
    with caplog.at_level(logging.ERROR):
        deliver(msg)  # must not raise

    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_link_base_falls_back_to_the_backend_when_no_frontend_is_configured(monkeypatch):
    """D-08: links point at this backend until Phase 3's frontend routes exist."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "frontend_base_url", None)
    assert mailer.link_base() == mailer.settings.public_base_url


def test_link_base_flips_to_the_frontend_once_configured(monkeypatch):
    """D-09: the Phase 3 handoff is a config change, not a code change."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "frontend_base_url", "https://app.test")
    assert mailer.link_base() == "https://app.test"


def test_links_hit_backend_routes_when_no_frontend_is_configured(monkeypatch):
    """With no frontend, emailed links must still work against the backend alone."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "frontend_base_url", None)
    monkeypatch.setattr(mailer.settings, "public_base_url", "http://api.test")
    monkeypatch.setattr(mailer.settings, "api_prefix", "/api")

    assert mailer.verify_link("tok123") == "http://api.test/api/auth/verify?token=tok123"
    assert mailer.reset_link("tok123") == "http://api.test/api/auth/reset/confirm?token=tok123"


def test_links_open_the_frontend_pages_once_configured(monkeypatch):
    """The frontend owns the pages; the API prefix must not leak into their paths."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "frontend_base_url", "https://app.test/")
    monkeypatch.setattr(mailer.settings, "api_prefix", "/api")

    assert mailer.verify_link("tok123") == "https://app.test/verify-email?token=tok123"
    assert mailer.reset_link("tok123") == "https://app.test/set-new-password?token=tok123"


def test_links_escape_the_token(monkeypatch):
    """A token can never break out of its query parameter."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "frontend_base_url", "https://app.test")
    assert mailer.reset_link("a b&c=d") == "https://app.test/set-new-password?token=a%20b%26c%3Dd"


def test_configured_smtp_starttls_logs_in_and_sends(monkeypatch):
    """The console and SMTP paths are the same code -- only environment differs.

    Unlike the exploding-SMTP test above (which only proves the branch is
    entered), this records every call to prove `starttls`/`login`/
    `send_message` are actually reached with the right arguments, including
    the explicit `timeout=` (Pitfall 2).
    """
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(mailer.settings, "smtp_username", "relay-user")
    monkeypatch.setattr(mailer.settings, "smtp_password", "relay-pass")

    events: list = []

    class _RecordingSMTP:
        def __init__(self, host, port, timeout=None):
            events.append(("init", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def starttls(self):
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username))

        def send_message(self, message):
            events.append(("send", message["To"]))

    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)

    deliver(verification_email("relayed@example.com", "http://localhost:8000/x"))

    assert events[0] == ("init", "smtp.test", 587, mailer.settings.smtp_timeout_seconds)
    assert ("starttls",) in events
    assert ("login", "relay-user") in events
    assert ("send", "relayed@example.com") in events


def test_configured_smtp_skips_login_when_no_username_is_set(monkeypatch):
    """`login()` runs only when a username is configured (Task 2's action)."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(mailer.settings, "smtp_username", None)

    events: list = []

    class _RecordingSMTP:
        def __init__(self, host, port, timeout=None):
            events.append(("init", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def starttls(self):
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username))

        def send_message(self, message):
            events.append(("send", message["To"]))

    monkeypatch.setattr("smtplib.SMTP", _RecordingSMTP)

    deliver(verification_email("no-auth-relay@example.com", "http://localhost:8000/x"))

    assert not any(event[0] == "login" for event in events)
    assert ("send", "no-auth-relay@example.com") in events


# --- Empty-string configuration is "unconfigured" --------------------------


def test_blank_smtp_settings_are_treated_as_unconfigured():
    """docker-compose passes ${SMTP_HOST:-} as an EMPTY STRING, not unset.

    Compose cannot conditionally omit an environment key, so an unconfigured
    stack arrives with `SMTP_HOST=""`. If that reached the mailer as `""`, the
    `smtp_host is None` check would be False and delivery would try to open an
    SMTP connection to an empty host instead of falling back to the console -
    breaking the safety property that an unconfigured deployment cannot send
    (and cannot hang on) real mail. Caught exactly once, when the compose
    wiring first met the mailer; this test is why it stays caught.
    """
    from app.config import Settings

    blank = Settings(
        _env_file=None,
        smtp_host="",
        smtp_username="   ",
        smtp_password="",
        frontend_base_url="",
    )
    assert blank.smtp_host is None
    assert blank.smtp_username is None
    assert blank.smtp_password is None
    assert blank.frontend_base_url is None


def test_a_real_smtp_host_is_not_blanked():
    """The coercion must only fire on genuinely empty values."""
    from app.config import Settings

    configured = Settings(_env_file=None, smtp_host="smtp-relay.brevo.com")
    assert configured.smtp_host == "smtp-relay.brevo.com"
