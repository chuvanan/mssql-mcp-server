"""Security tests: injection, credential leakage, and the write checkpoint.

These are the assertions that matter most. Several of them exist because the
previous implementation failed them.
"""

import mssql_python
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from mssql_mcp_server.approval import APPROVE_OPTION
from mssql_mcp_server.server import mcp
from mssql_mcp_server.sql import validate_table_name
from test_sql import WRITES


@pytest.fixture
def client():
    return Client(mcp)


async def approving_handler(message, response_type, params, ctx):
    return response_type(value=APPROVE_OPTION)


# --------------------------------------------------------------------------
# The write checkpoint cannot be bypassed
# --------------------------------------------------------------------------


async def test_a_client_without_elicitation_cannot_write(env, no_db, client):
    """The single most important assertion in the suite.

    ctx.elicit() raises on such a client, so without the capability pre-check
    this would surface as an opaque protocol error rather than a refusal.
    """
    async with client as c:
        with pytest.raises(ToolError, match="cannot show approval prompts"):
            await c.call_tool("execute_write", {"sql": "DROP TABLE users"})
    assert no_db == []


async def test_a_refused_write_never_opens_a_connection(env, no_db):
    """No database connection is opened until the user has approved."""

    async def rejecting_handler(message, response_type, params, ctx):
        return response_type(value="Reject - do not run")

    async with Client(mcp, elicitation_handler=rejecting_handler) as c:
        result = await c.call_tool("execute_write", {"sql": "DELETE FROM users"})
    assert result.data.status == "declined"
    assert no_db == []


async def test_an_approved_write_does_run(env, fake_db):
    fake_db(columns=None, affected=3)
    async with Client(mcp, elicitation_handler=approving_handler) as c:
        result = await c.call_tool("execute_write", {"sql": "DELETE FROM users"})
    assert result.data.status == "executed"
    assert result.data.rows_affected == 3
    assert fake_db.holder["last"].commits == 1


async def test_the_user_is_shown_the_actual_sql(env, fake_db):
    """Approving something you cannot see is not approval."""
    seen = {}
    fake_db(columns=None, affected=1)

    async def capture(message, response_type, params, ctx):
        seen["message"] = message
        return response_type(value=APPROVE_OPTION)

    sql = "DELETE FROM orders WHERE created_at < '2020-01-01'"
    async with Client(mcp, elicitation_handler=capture) as c:
        await c.call_tool("execute_write", {"sql": sql})

    assert sql in seen["message"]
    assert "testdb" in seen["message"]  # the target database
    assert "DELETE" in seen["message"]  # the statement kind
    assert "secret123" not in seen["message"]  # never the password


async def test_read_query_cannot_be_used_to_write(env, no_db, client):
    async with client as c:
        for sql in ["DROP TABLE users", "DELETE FROM users", "UPDATE users SET a = 1"]:
            with pytest.raises(ToolError):
                await c.call_tool("read_query", {"sql": sql})
    assert no_db == []


@pytest.mark.parametrize("sql", WRITES)
async def test_no_write_in_the_corpus_passes_as_a_read(env, no_db, sql):
    """The full adversarial corpus from test_sql.py, driven through the real tool."""
    async with Client(mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool("read_query", {"sql": sql})
    assert no_db == []


# --------------------------------------------------------------------------
# Credentials must not leak
# --------------------------------------------------------------------------


async def test_a_driver_error_does_not_leak_the_password(env, fake_db, client):
    """Regression: the old code returned str(exc) verbatim and failed this."""
    fake_db(
        columns=None,
        execute_error=mssql_python.OperationalError(
            "Login failed for user 'sa' with password 'secret123'", ""
        ),
    )
    async with client as c:
        result = await c.call_tool("read_query", {"sql": "SELECT 1"}, raise_on_error=False)

    text = str(result.content[0].text)
    assert "secret123" not in text
    assert result.is_error is True


async def test_a_connection_failure_does_not_leak_the_connection_string(env, fake_db, client):
    fake_db(
        connect_error=mssql_python.InterfaceError(
            "failed: Server=localhost;UID=test_user;PWD=secret123", ""
        )
    )
    async with client as c:
        result = await c.call_tool("read_query", {"sql": "SELECT 1"}, raise_on_error=False)

    text = str(result.content[0].text)
    assert "secret123" not in text
    assert "PWD" not in text


async def test_the_password_is_absent_from_settings_repr(env):
    from mssql_mcp_server.config import get_settings

    assert "secret123" not in repr(get_settings())


async def test_the_password_is_absent_from_the_startup_log_line(env):
    from mssql_mcp_server.config import get_settings

    assert "secret123" not in get_settings().describe()


# --------------------------------------------------------------------------
# Identifier validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "users; DROP TABLE users",
        "users--",
        "users'; DELETE FROM users; --",
        "[users]",
        "users]",
        "../etc/passwd",
        "us ers",
        "a.b.c",
        "*",
        "",
    ],
)
def test_table_name_validation_rejects_injection(name):
    with pytest.raises(ValueError, match="Invalid table name"):
        validate_table_name(name)


def test_table_name_validation_brackets_valid_names():
    assert validate_table_name("users") == "[users]"
    assert validate_table_name("dbo.users") == "[dbo].[users]"


async def test_the_resource_rejects_an_injected_table_name(env, no_db, client):
    """Rejected either as a malformed URI or by validate_table_name.

    Which of the two fires depends on the payload; what matters is that none
    of them reaches the database.
    """
    async with client as c:
        for bad in ["users;DROP TABLE users", "users--", "users'"]:
            with pytest.raises((McpError, ValueError)):
                await c.read_resource(f"mssql://{bad}/data")
    assert no_db == []


async def test_describe_table_rejects_an_injected_name(env, no_db, client):
    async with client as c:
        with pytest.raises(ToolError, match="Invalid table name"):
            await c.call_tool("describe_table", {"table": "users; DROP TABLE users"})
    assert no_db == []


# --------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [{}, {"sql": ""}, {"sql": "   "}, {"sql": None}, {"query": "SELECT 1"}]
)
async def test_read_query_rejects_malformed_arguments(env, no_db, bad, client):
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("read_query", bad)
    assert no_db == []


async def test_a_non_string_query_is_rejected_by_the_schema(env, no_db, client):
    """FastMCP's type-driven validation catches this before any of our code runs."""
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("read_query", {"sql": {"$ne": None}})
    assert no_db == []
