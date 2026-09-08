"""
The log redaction filter, which had no tests at all until it broke a deployment.

WHY THIS FILE EXISTS
====================
``RedactingFilter`` sits on the root logger and on uvicorn's loggers, so every
line this system emits passes through it -- application logs, access logs and
third-party output alike. It is the net under the tightrope for credentials.

It also had zero test coverage, and has now broken twice in the same way: by
mangling the relationship between a format string and its arguments, which does
not fail where the filter is, it fails later inside the handler.

  1. An early version called ``str()`` on every argument, turning integers into
     strings, so every ``%d`` in the codebase raised TypeError at log time.
  2. The version that replaced it redacted the template and the arguments
     separately. The "names itself a secret" pattern matched ``password : %s``
     and consumed the *placeholder*, leaving the template one ``%s`` short of
     its arguments -- so the record died with "not all arguments converted
     during string formatting", and Python's logging error path then printed
     ``Arguments:`` with the raw secret still in it. Found on the first real
     deployment, on the one log line that shows the generated administrator
     password.

Both failures are invisible in normal use and destroy exactly the output that
was meant to be protected, so they are worth a file of their own.
"""

from __future__ import annotations

import logging

from app.main import ADMIN_BANNER
from app.security.crypto import RedactingFilter, redact

SECRET = "s3cr3t-vAlue-Not-In-A-Log-Please"


def _record(msg: str, args: object = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.WARNING, pathname="test.py", lineno=1,
        msg=msg, args=args, exc_info=None,
    )


def _emit(msg: str, args: object = None) -> str:
    """Push a record through the real filter and render it, as a handler would."""
    record = _record(msg, args)
    RedactingFilter().filter(record)
    return record.getMessage()


# ---------------------------------------------------------------------------
# The regression that broke a deployment
# ---------------------------------------------------------------------------

def test_a_redacted_placeholder_does_not_destroy_the_record():
    """
    A template whose placeholder sits in a secret-shaped pair must still render.

    ``password : %s`` is the shape that broke. Redacting the template replaced
    the ``%s`` with ``<REDACTED>``, so two placeholders remained against three
    arguments and the record raised TypeError inside the handler -- never
    reaching the log at all.
    """
    rendered = _emit(
        "Sign in at %s / email %s / password : %s",
        ("http://localhost:8000", "admin@localhost", SECRET),
    )

    assert "not all arguments" not in rendered
    assert "http://localhost:8000" in rendered
    assert "admin@localhost" in rendered
    # The secret arrived in a `password :` pair, so it SHOULD be gone.
    assert SECRET not in rendered
    assert "<REDACTED>" in rendered


def test_the_arguments_are_cleared_so_a_handler_cannot_reprint_them():
    """
    The secret must not survive on ``record.args``.

    This is the half of the old bug that made it worse than useless: the record
    failed to format, and Python's logging error handler printed the raw
    ``Arguments:`` tuple to stderr. The redaction destroyed the message and
    published the secret in the same breath.
    """
    record = _record("password : %s", (SECRET,))
    RedactingFilter().filter(record)

    assert not record.args, "arguments must be dropped once folded into the message"
    assert SECRET not in str(record.args)
    assert SECRET not in record.getMessage()


# ---------------------------------------------------------------------------
# The protection must still work
# ---------------------------------------------------------------------------

def test_a_secret_passed_as_an_argument_is_redacted():
    """The common shape: a named secret whose value arrives as an argument."""
    assert SECRET not in _emit("client_secret=%s", (SECRET,))


def test_a_secret_already_inside_the_template_is_redacted():
    """
    Third-party libraries interpolate before logging, so the template itself
    carries the value and there are no arguments to inspect.
    """
    assert SECRET not in _emit(f"Authorization: Bearer {SECRET}aaaaaaaaaaaaaaaaaaaa")


def test_an_amazon_refresh_token_is_redacted_wherever_it_appears():
    token = "Atzr|IwEBIexampleexampleexampleexample"
    assert token not in _emit("refreshing with %s", (token,))
    assert token not in _emit(f"refreshing with {token}")


# ---------------------------------------------------------------------------
# The first failure, kept as a test so it cannot come back either
# ---------------------------------------------------------------------------

def test_integer_format_specifiers_still_work():
    """
    ``%d`` must survive the filter.

    The first version of this filter called ``str()`` on every argument, so
    every ``%d`` in the codebase raised TypeError at log time and the logging
    that was meant to be protected stopped working entirely.
    """
    assert _emit("processed %d rows in %.1f seconds", (1158340, 11.2)) == (
        "processed 1158340 rows in 11.2 seconds"
    )


def test_a_dict_style_format_still_works():
    # Wrapped in a tuple because that is how logging itself hands a mapping to
    # LogRecord; passed bare, LogRecord tries to subscript it with 0.
    assert _emit("%(stage)s finished", ({"stage": "FETCH"},)) == "FETCH finished"


def test_a_malformed_format_string_does_not_break_the_filter():
    """
    A caller's broken format string is the caller's bug.

    It will surface in the handler regardless; the filter must not turn it into
    a second failure deeper inside logging, where it is far harder to trace.
    """
    record = _record("two placeholders %s %s", ("only one",))
    assert RedactingFilter().filter(record) is True


# ---------------------------------------------------------------------------
# The banner, which is the reason any of this was noticed
# ---------------------------------------------------------------------------

def test_the_administrator_banner_survives_redaction():
    """
    The generated administrator password must reach the operator's screen.

    This banner is emitted once, on the first start of a fresh install. The
    password is stored nowhere and cannot be recovered, so if redaction eats it
    the operator cannot sign in and the only fix is to go back to the database.

    That is exactly what happened: the banner read ``password : %s``, the
    filter replaced the placeholder, and the record never rendered.

    The colons after "email" and "password" in ``ADMIN_BANNER`` are therefore
    absent ON PURPOSE, and this test is what stops somebody tidying them back
    in. If it fails, do not "fix" the test -- look at the banner.
    """
    rendered = _emit(ADMIN_BANNER, ("http://localhost:8000", "admin@localhost", SECRET))

    assert SECRET in rendered, (
        "the generated administrator password was redacted out of the one message "
        "whose purpose is to display it. A colon after 'password' in ADMIN_BANNER "
        "will do this: the redaction filter strips the value from any "
        "password:/password= pair. The password cannot be recovered, so this "
        "locks the operator out of a fresh install."
    )
    assert "ADMINISTRATOR ACCOUNT CREATED" in rendered
    assert "admin@localhost" in rendered
    assert "WRITE THIS DOWN NOW" in rendered


def test_the_banner_template_has_no_colon_before_its_values():
    """
    Guards the same thing one level earlier, with a message that names the cause.

    The rendered-output test above is the real check; this one fails with the
    reason rather than with a missing-password assertion, because the two look
    unrelated at three in the morning.
    """
    for label in ("email", "password"):
        assert f"{label} :" not in ADMIN_BANNER and f"{label}:" not in ADMIN_BANNER, (
            f"ADMIN_BANNER has a colon after '{label}'. The redaction filter strips "
            f"the value from any {label}:/{label}= pair, which blanks the generated "
            "password in the only message that shows it."
        )


def test_redact_leaves_ordinary_text_alone():
    """A false positive is cheap, but it should not rewrite whole log lines."""
    line = "run 41: 38,341 quantities out of step, 196 products with zero stock"
    assert redact(line) == line
