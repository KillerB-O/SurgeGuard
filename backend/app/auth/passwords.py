"""Argon2id password hashing and the pure, DB-free password policy check.

Nothing in this module touches a database or the network. That is what lets
`password_policy_error` be verified by `tests/test_auth_unit.py` on a machine
with Docker fully down -- AUTH-03's "specific reason" has to be checkable
without standing up Postgres first.
"""

from collections.abc import Callable

from pwdlib import PasswordHash

from app.config import settings

# One Argon2id hasher for the whole process. `.recommended()` currently
# configures m=65536 (64 MiB), t=3, p=4 -- OWASP's interactive-login minimum
# for Argon2id (T-01-01).
_password_hash = PasswordHash.recommended()


def hash_password(plaintext: str) -> str:
    """Return an Argon2id hash of `plaintext`, salted per call.

    Args:
        plaintext: The password as submitted by the user. Never stored.

    Returns:
        A `$argon2id$...` encoded hash string, unique even for repeated input
        because `pwdlib` generates a fresh random salt on every call.
    """
    return _password_hash.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return whether `plaintext` matches a previously stored Argon2id hash.

    Uses the plain verification call, deliberately not the variant pwdlib
    offers for migrating accounts off a deprecated hashing algorithm onto a
    new one -- this phase configures exactly one algorithm, so that migration
    branch can never fire, and adding it would be dead complexity with no
    test that could ever exercise it.

    Args:
        plaintext: Candidate password to check.
        hashed: Previously stored Argon2id hash.

    Returns:
        `True` if `plaintext` hashes to `hashed`, `False` otherwise (including
        for a malformed or unrecognised hash string).
    """
    try:
        return _password_hash.verify(plaintext, hashed)
    except ValueError:
        # pwdlib raises on a hash it cannot parse. Treat that as "does not
        # match" rather than letting a malformed row 500 the request.
        return False


# The password rules, in the order the signup form displays them.
#
# Each entry is (rule_id, label, predicate). `rule_id` is the stable contract
# with the frontend -- the sign-up form renders a live checklist and must tick
# exactly the rules the server actually enforces, so both sides read this one
# list rather than each keeping their own copy. `GET /auth/password-policy`
# serves it; see docs/auth-frontend-contract.md.
#
# A NOTE ON WHY THESE EXIST, because the code previously argued the opposite:
# NIST SP 800-63B and OWASP ASVS V2.1.1 both advise length over forced
# composition rules, on the evidence that mandating character classes pushes
# users toward predictable shapes like "Password1!". This project nonetheless
# enforces composition, as a deliberate product decision taken with that
# tradeoff stated -- the sign-up flow is built around a strength checklist that
# would be dishonest if the server accepted passwords the checklist marked as
# failing. Recorded here so the next reader knows it was chosen, not overlooked.
PASSWORD_RULES: tuple[tuple[str, str, Callable[[str], bool]], ...] = (
    (
        "length",
        f"At least {settings.password_min_length} characters",
        lambda p: len(p) >= settings.password_min_length,
    ),
    ("uppercase", "One uppercase letter", lambda p: any(c.isupper() for c in p)),
    ("lowercase", "One lowercase letter", lambda p: any(c.islower() for c in p)),
    ("digit", "One number", lambda p: any(c.isdigit() for c in p)),
    ("symbol", "One symbol", lambda p: any(not c.isalnum() for c in p)),
)

# Kept separate from PASSWORD_RULES because it is a guard on Argon2 input size,
# not something a strength checklist should display as a goal to reach.
_TOO_LONG_MESSAGE = "password must be at most {n} characters"


def failed_password_rules(plaintext: str) -> list[str]:
    """Return the ids of every `PASSWORD_RULES` entry `plaintext` fails.

    The frontend evaluates the same rules locally as the user types, so this
    exists mainly to keep the server's answer and the form's checklist
    provably derived from one definition.

    Args:
        plaintext: Candidate password to check.

    Returns:
        Rule ids in `PASSWORD_RULES` order; empty when the password passes.
    """
    return [rule_id for rule_id, _, ok in PASSWORD_RULES if not ok(plaintext)]


def password_policy_error(plaintext: str) -> str | None:
    """Return a specific, named reason `plaintext` fails the password policy.

    Returns `None` when the password is acceptable. Callers must check this
    BEFORE doing any database work, and must surface the returned message
    verbatim rather than a generic "invalid password" -- that specificity is
    the entirety of AUTH-03.

    The maximum-length check is deliberately first: an over-long input is a
    resource concern, and it should be rejected before anything iterates it.

    Args:
        plaintext: Candidate password to check.

    Returns:
        A message naming the first failed rule, or `None` if the password
        passes. Use `failed_password_rules` when you need all of them.
    """
    if len(plaintext) > settings.password_max_length:
        return _TOO_LONG_MESSAGE.format(n=settings.password_max_length)
    for rule_id, label, ok in PASSWORD_RULES:
        if not ok(plaintext):
            if rule_id == "length":
                return f"password must be at least {settings.password_min_length} characters"
            return f"password must contain {label.lower()}"
    return None


# A throwaway Argon2 hash of a fixed dummy string, computed once at import.
# Plan 02's login handler verifies against this constant when no user is
# found for the submitted email, so BOTH the "unknown email" and "wrong
# password" failure paths cost exactly one Argon2 verification each. Without
# this, an unknown-email login would return in microseconds while a
# known-email/wrong-password login pays Argon2's ~64 MiB, tens-of-millisecond
# cost -- a timing side channel that D-12's identical *response body* does
# nothing to close, because the two paths would still be distinguishable by
# how long they take to produce that identical body.
DUMMY_PASSWORD_HASH: str = hash_password("dummy-password-for-timing-parity-only")
