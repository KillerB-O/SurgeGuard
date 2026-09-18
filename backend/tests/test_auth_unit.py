"""Pure, DB-free tests for the password policy and Argon2id hashing.

No `require_database` marker and no database access anywhere in this file --
that absence is the point. `password_policy_error`, `hash_password` and
`verify_password` are plain functions with no `Depends(get_connection)` and
no network I/O, so this file must stay green even with Docker fully down.
That is what makes AUTH-02 and AUTH-03 verifiable on a machine that cannot
reach Postgres, unlike everything in test_auth.py.
"""

import pytest

from app.auth.passwords import (
    PASSWORD_RULES,
    failed_password_rules,
    hash_password,
    password_policy_error,
    verify_password,
)


def test_password_too_short_names_the_minimum():
    error = password_policy_error("short")
    assert error is not None
    assert "8 characters" in error


def test_password_too_long_names_the_maximum():
    error = password_policy_error("a" * 200)
    assert error is not None
    assert "128" in error


def test_acceptable_password_has_no_policy_error():
    assert password_policy_error("Correct-Horse-Battery9") is None


def test_hash_password_produces_argon2id():
    assert hash_password("Correct-Horse-Battery9").startswith("$argon2id$")


def test_verify_password_true_for_right_password_false_for_wrong():
    hashed = hash_password("Correct-Horse-Battery9")
    assert verify_password("Correct-Horse-Battery9", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_hash_password_salts_per_call():
    # Two hashes of the identical input must differ -- that is what "salted"
    # means in AUTH-02. A deterministic hash would make a rainbow table over
    # this codebase's own password_min_length trivial to build.
    first = hash_password("Correct-Horse-Battery9")
    second = hash_password("Correct-Horse-Battery9")
    assert first != second


# --- Composition rules -----------------------------------------------------
#
# One case per rule, each failing exactly that rule and nothing else, so a
# regression names the rule it broke instead of just "policy changed".


@pytest.mark.parametrize(
    ("password", "rule_id", "expected_phrase"),
    [
        ("Sh0rt!", "length", "8 characters"),
        ("correct-horse-9", "uppercase", "one uppercase letter"),
        ("CORRECT-HORSE-9", "lowercase", "one lowercase letter"),
        ("Correct-Horse-Xy", "digit", "one number"),
        ("CorrectHorse9xy", "symbol", "one symbol"),
    ],
)
def test_each_rule_is_enforced_and_named(password, rule_id, expected_phrase):
    assert rule_id in failed_password_rules(password)
    error = password_policy_error(password)
    assert error is not None
    # AUTH-03: the message must name the rule, not say "invalid password".
    assert expected_phrase in error


def test_failed_rules_is_empty_for_a_compliant_password():
    assert failed_password_rules("Correct-Horse-Battery9") == []


def test_failed_rules_reports_every_failure_not_just_the_first():
    # The signup form ticks a checklist, so it needs all of them at once.
    # password_policy_error deliberately returns only the first.
    assert set(failed_password_rules("abc")) == {"length", "uppercase", "digit", "symbol"}


def test_rule_ids_are_unique_and_stable():
    # The frontend keys its checklist off these ids, so a duplicate or a
    # rename is a breaking API change, not a refactor.
    ids = [rule_id for rule_id, _, _ in PASSWORD_RULES]
    assert ids == ["length", "uppercase", "lowercase", "digit", "symbol"]


def test_every_rule_has_a_human_label():
    for rule_id, label, _ in PASSWORD_RULES:
        assert label and label[0].isupper(), rule_id
