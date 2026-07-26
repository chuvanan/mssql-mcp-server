"""Tests for the write-approval checkpoint.

Uses a hand-built fake context rather than a full client, so that each branch
(accept / decline / cancel / no capability / elicit raises) can be driven
directly. The end-to-end version lives in test_integration.py.
"""

from types import SimpleNamespace

import pytest

from mssql_mcp_server.approval import (
    APPROVE_OPTION,
    REJECT_OPTION,
    Decision,
    build_message,
    request_approval,
)
from mssql_mcp_server.config import ApprovalMode, Settings
from mssql_mcp_server.sql import StatementKind, classify

SETTINGS = Settings(
    server="dbhost,1433",
    database="sales_prod",
    user="svc_app",
    password="secret123",
)


class FakeContext:
    """A context whose elicitation behaviour is fully controllable."""

    def __init__(self, *, supports=True, result=None, raises=None):
        self._supports = supports
        self._result = result
        self._raises = raises
        self.elicit_calls: list[tuple[str, object]] = []
        self.warnings: list[str] = []
        self.session = SimpleNamespace(check_client_capability=self._check)

    def _check(self, _capability):
        if isinstance(self._supports, Exception):
            raise self._supports
        return self._supports

    async def elicit(self, message, response_type=None, **kwargs):
        self.elicit_calls.append((message, response_type))
        if self._raises is not None:
            raise self._raises
        return self._result

    async def warning(self, message):
        self.warnings.append(message)


def accepted(value):
    return SimpleNamespace(action="accept", data=value)


async def approve(ctx, sql="DELETE FROM t", settings=SETTINGS):
    return await request_approval(ctx, sql=sql, kinds=classify(sql), settings=settings)


# --------------------------------------------------------------------------
# The three user responses
# --------------------------------------------------------------------------


async def test_choosing_approve_runs_the_statement():
    ctx = FakeContext(result=accepted(APPROVE_OPTION))
    assert await approve(ctx) is Decision.APPROVED


async def test_choosing_reject_does_not():
    ctx = FakeContext(result=accepted(REJECT_OPTION))
    assert await approve(ctx) is Decision.DECLINED


async def test_dismissing_the_prompt_declines():
    ctx = FakeContext(result=SimpleNamespace(action="decline"))
    assert await approve(ctx) is Decision.DECLINED


async def test_cancelling_the_prompt_cancels():
    ctx = FakeContext(result=SimpleNamespace(action="cancel"))
    assert await approve(ctx) is Decision.CANCELLED


async def test_an_unrecognised_choice_is_not_an_approval():
    """Anything we do not positively recognise must not run the statement."""
    ctx = FakeContext(result=accepted("something else"))
    assert await approve(ctx) is Decision.DECLINED


async def test_accept_without_data_is_not_an_approval():
    ctx = FakeContext(result=SimpleNamespace(action="accept", data=None))
    assert await approve(ctx) is Decision.DECLINED


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


async def test_client_without_elicitation_is_refused():
    ctx = FakeContext(supports=False)
    assert await approve(ctx) is Decision.UNAVAILABLE


async def test_client_without_elicitation_is_never_prompted():
    """ctx.elicit() raises on such a client; the point is not to call it."""
    ctx = FakeContext(supports=False)
    await approve(ctx)
    assert ctx.elicit_calls == []


async def test_elicit_raising_is_refused_not_crashed():
    """A false positive from the capability check still must not write."""
    ctx = FakeContext(supports=True, raises=RuntimeError("Elicitation not supported"))
    assert await approve(ctx) is Decision.UNAVAILABLE


async def test_capability_check_raising_is_refused():
    ctx = FakeContext(supports=RuntimeError("no session"))
    assert await approve(ctx) is Decision.UNAVAILABLE
    assert ctx.elicit_calls == []


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("boom"), ValueError("bad"), TimeoutError("slow"), OSError("network")],
)
async def test_any_elicitation_failure_becomes_a_refusal(exc):
    ctx = FakeContext(supports=True, raises=exc)
    assert await approve(ctx) is Decision.UNAVAILABLE


async def test_cancellation_propagates_rather_than_being_swallowed():
    """Client disconnect must cancel the call, not read as 'cannot ask'."""
    import asyncio

    ctx = FakeContext(supports=True, raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await approve(ctx)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


async def test_allow_mode_skips_the_prompt():
    settings = Settings(
        server="h", database="d", user="u", password="p", approval_mode=ApprovalMode.ALLOW
    )
    ctx = FakeContext()
    assert await approve(ctx, settings=settings) is Decision.APPROVED
    assert ctx.elicit_calls == []


async def test_allow_mode_warns_on_every_write():
    """A mode set once in a config file must not stay invisible."""
    settings = Settings(
        server="h", database="d", user="u", password="p", approval_mode=ApprovalMode.ALLOW
    )
    ctx = FakeContext()
    await approve(ctx, settings=settings)
    assert any("disabled" in w.lower() for w in ctx.warnings)


async def test_readonly_mode_forbids_writes():
    settings = Settings(
        server="h", database="d", user="u", password="p", approval_mode=ApprovalMode.READONLY
    )
    ctx = FakeContext()
    assert await approve(ctx, settings=settings) is Decision.FORBIDDEN
    assert ctx.elicit_calls == []


async def test_elicit_mode_is_the_default():
    assert SETTINGS.approval_mode is ApprovalMode.ELICIT


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------


async def test_the_prompt_offers_exactly_two_choices():
    ctx = FakeContext(result=accepted(APPROVE_OPTION))
    await approve(ctx)
    _, response_type = ctx.elicit_calls[0]
    assert response_type == [APPROVE_OPTION, REJECT_OPTION]


def test_message_names_the_target_database_and_server():
    message = build_message("DELETE FROM orders", classify("DELETE FROM orders"), SETTINGS)
    assert "sales_prod" in message
    assert "dbhost,1433" in message


def test_message_shows_the_sql():
    sql = "DELETE FROM orders WHERE created_at < '2020-01-01'"
    assert sql in build_message(sql, classify(sql), SETTINGS)


def test_message_summarises_the_statement_kinds():
    sql = "DELETE FROM a; UPDATE b SET c = 1"
    message = build_message(sql, classify(sql), SETTINGS)
    assert "DELETE, UPDATE" in message


def test_message_deduplicates_repeated_kinds():
    sql = "DELETE FROM a; DELETE FROM b"
    assert "DELETE, DELETE" not in build_message(sql, classify(sql), SETTINGS)


def test_message_never_contains_the_password():
    sql = "DELETE FROM t"
    assert "secret123" not in build_message(sql, classify(sql), SETTINGS)


def test_message_says_rejecting_is_safe():
    """Users who believe rejection is costly click Approve reflexively."""
    message = build_message("DELETE FROM t", classify("DELETE FROM t"), SETTINGS)
    assert "Rejecting is safe" in message


def test_long_sql_is_truncated_visibly_never_silently():
    sql = "DELETE FROM t WHERE x = 1 OR " + " OR ".join(f"y = {i}" for i in range(2000))
    message = build_message(sql, classify(sql), SETTINGS)
    assert len(sql) > 4000
    assert "showing the first 4000 of" in message
    assert str(len(sql)) in message


def test_short_sql_is_not_truncated():
    message = build_message("DELETE FROM t", classify("DELETE FROM t"), SETTINGS)
    assert "showing the first" not in message


@pytest.mark.parametrize(
    "sql",
    ["DELETE FROM t", "DROP TABLE t", "UPDATE t SET a = 1", "EXEC sp_who"],
)
def test_message_builds_for_every_write_kind(sql):
    message = build_message(sql, classify(sql), SETTINGS)
    assert str(classify(sql)[0]) in message


def test_unlexable_sql_still_produces_a_prompt():
    """UNKNOWN is a write; the user must still be shown something coherent."""
    sql = "DELETE FROM t WHERE x = 'unterminated"
    kinds = classify(sql)
    assert kinds == [StatementKind.UNKNOWN]
    assert sql in build_message(sql, kinds, SETTINGS)
