"""End-to-end tests through the in-memory MCP client.

These exercise the whole path a real client takes: tool call -> classification
-> elicitation round-trip -> execution. The elicitation handler stands in for
the human clicking Approve or Reject.
"""

import os

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mssql_mcp_server.approval import APPROVE_OPTION, REJECT_OPTION
from mssql_mcp_server.server import mcp

REQUIRES_LIVE_DB = pytest.mark.skipif(
    os.getenv("MSSQL_LIVE_TESTS") != "1",
    reason="Set MSSQL_LIVE_TESTS=1 and start docker compose to run live tests.",
)


def handler_choosing(option):
    async def _handler(message, response_type, params, ctx):
        return response_type(value=option)

    return _handler


async def handler_declining(message, response_type, params, ctx):
    from fastmcp.client.elicitation import ElicitResult

    return ElicitResult(action="decline")


async def handler_cancelling(message, response_type, params, ctx):
    from fastmcp.client.elicitation import ElicitResult

    return ElicitResult(action="cancel")


# --------------------------------------------------------------------------
# The three outcomes, end to end
# --------------------------------------------------------------------------


async def test_approving_executes_and_commits(env, fake_db):
    fake_db(columns=None, affected=5)
    async with Client(mcp, elicitation_handler=handler_choosing(APPROVE_OPTION)) as c:
        result = await c.call_tool("execute_write", {"sql": "DELETE FROM orders"})

    assert result.data.status == "executed"
    assert result.data.rows_affected == 5
    assert result.data.statements == ["DELETE"]
    conn = fake_db.holder["last"]
    assert conn.commits == 1
    assert conn.rollbacks == 0


async def test_rejecting_does_not_execute(env, no_db):
    async with Client(mcp, elicitation_handler=handler_choosing(REJECT_OPTION)) as c:
        result = await c.call_tool("execute_write", {"sql": "DELETE FROM orders"})

    assert result.data.status == "declined"
    assert result.data.rows_affected is None
    assert no_db == []


async def test_declining_the_prompt_does_not_execute(env, no_db):
    async with Client(mcp, elicitation_handler=handler_declining) as c:
        result = await c.call_tool("execute_write", {"sql": "DROP TABLE orders"})

    assert result.data.status == "declined"
    assert no_db == []


async def test_cancelling_the_prompt_does_not_execute(env, no_db):
    async with Client(mcp, elicitation_handler=handler_cancelling) as c:
        result = await c.call_tool("execute_write", {"sql": "DROP TABLE orders"})

    assert result.data.status == "cancelled"
    assert no_db == []


async def test_a_decline_is_a_result_not_an_error(env, no_db):
    """Raising would invite a retry loop and train the user to click Approve."""
    async with Client(mcp, elicitation_handler=handler_choosing(REJECT_OPTION)) as c:
        result = await c.call_tool("execute_write", {"sql": "DELETE FROM t"})
    assert result.is_error is False
    assert "do not retry" in result.data.message.lower()


# --------------------------------------------------------------------------
# The prompt the user actually sees
# --------------------------------------------------------------------------


async def test_the_prompt_contains_everything_needed_to_decide(env, fake_db):
    seen = {}
    fake_db(columns=None, affected=1)

    async def capture(message, response_type, params, ctx):
        seen["message"] = message
        return response_type(value=APPROVE_OPTION)

    sql = "UPDATE customers SET archived = 1 WHERE last_seen < '2020-01-01'"
    async with Client(mcp, elicitation_handler=capture) as c:
        await c.call_tool("execute_write", {"sql": sql})

    message = seen["message"]
    assert sql in message  # the exact statement
    assert "testdb" in message  # which database
    assert "localhost" in message  # which server
    assert "UPDATE" in message  # what kind of change
    assert "Rejecting is safe" in message
    assert "secret123" not in message


async def test_a_multi_statement_batch_lists_every_kind(env, fake_db):
    seen = {}
    fake_db(columns=None, affected=1)

    async def capture(message, response_type, params, ctx):
        seen["message"] = message
        return response_type(value=APPROVE_OPTION)

    async with Client(mcp, elicitation_handler=capture) as c:
        result = await c.call_tool("execute_write", {"sql": "DELETE FROM a; UPDATE b SET c = 1"})

    assert "DELETE, UPDATE" in seen["message"]
    assert result.data.statements == ["DELETE", "UPDATE"]


# --------------------------------------------------------------------------
# Approval modes
# --------------------------------------------------------------------------


async def test_allow_mode_executes_without_prompting(env, fake_db):
    env(MSSQL_APPROVAL_MODE="allow")
    fake_db(columns=None, affected=2)
    prompted = []

    async def should_not_be_called(message, response_type, params, ctx):
        prompted.append(message)
        return response_type(value=APPROVE_OPTION)

    async with Client(mcp, elicitation_handler=should_not_be_called) as c:
        result = await c.call_tool("execute_write", {"sql": "DELETE FROM t"})

    assert result.data.status == "executed"
    assert prompted == []


async def test_allow_mode_works_without_an_elicitation_capable_client(env, fake_db):
    """The escape hatch for headless and CI use."""
    env(MSSQL_APPROVAL_MODE="allow")
    fake_db(columns=None, affected=1)
    async with Client(mcp) as c:
        result = await c.call_tool("execute_write", {"sql": "DELETE FROM t"})
    assert result.data.status == "executed"


async def test_readonly_mode_unregisters_the_write_tool(env):
    """A tool the model cannot see beats one it keeps being refused."""
    from mssql_mcp_server.config import get_settings
    from mssql_mcp_server.server import apply_approval_policy

    env(MSSQL_APPROVAL_MODE="readonly")
    saved = await mcp.get_tool("execute_write")
    try:
        apply_approval_policy(get_settings())
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
        assert "execute_write" not in names
        assert "read_query" in names
    finally:
        # The FastMCP instance is module-level and shared across tests.
        mcp.add_tool(saved)


async def test_the_write_tool_is_present_in_the_default_mode(env):
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert "execute_write" in names


# --------------------------------------------------------------------------
# Reads end to end
# --------------------------------------------------------------------------


async def test_read_and_write_round_trip(env, fake_db):
    fake_db(columns=["id"], rows=[[1], [2]])
    async with Client(mcp) as c:
        read = await c.call_tool("read_query", {"sql": "SELECT id FROM t"})
    assert read.data.rows == [[1], [2]]

    fake_db(columns=None, affected=1)
    async with Client(mcp, elicitation_handler=handler_choosing(APPROVE_OPTION)) as c:
        write = await c.call_tool("execute_write", {"sql": "INSERT INTO t VALUES (3)"})
    assert write.data.status == "executed"


async def test_null_values_survive_the_round_trip(env, fake_db):
    fake_db(columns=["a", "b"], rows=[[None, "x"]])
    async with Client(mcp) as c:
        result = await c.call_tool("read_query", {"sql": "SELECT a, b FROM t"})
    assert result.data.rows == [[None, "x"]]


async def test_a_write_sent_to_read_query_is_redirected(env, no_db):
    async with Client(mcp) as c:
        with pytest.raises(ToolError, match="execute_write"):
            await c.call_tool("read_query", {"sql": "DELETE FROM t"})
    assert no_db == []


async def test_a_read_sent_to_execute_write_is_redirected(env, no_db):
    """Otherwise every query routes through the approval prompt."""
    async with Client(mcp) as c:
        with pytest.raises(ToolError, match="read_query"):
            await c.call_tool("execute_write", {"sql": "SELECT 1"})
    assert no_db == []


# --------------------------------------------------------------------------
# Live tests -- opt in with MSSQL_LIVE_TESTS=1
# --------------------------------------------------------------------------


@pytest.mark.live
@REQUIRES_LIVE_DB
async def test_live_read_query():
    async with Client(mcp) as c:
        result = await c.call_tool("read_query", {"sql": "SELECT 1 AS n"})
    assert result.data.rows == [[1]]


@pytest.mark.live
@REQUIRES_LIVE_DB
async def test_live_list_tables():
    async with Client(mcp) as c:
        result = await c.call_tool("list_tables", {})
    assert isinstance(result.data, list)


@pytest.mark.live
@REQUIRES_LIVE_DB
async def test_live_declined_write_leaves_the_database_untouched():
    async with Client(mcp, elicitation_handler=handler_choosing(REJECT_OPTION)) as c:
        result = await c.call_tool(
            "execute_write", {"sql": "CREATE TABLE should_not_exist (a INT)"}
        )
        assert result.data.status == "declined"

        check = await c.call_tool(
            "read_query",
            {
                "sql": (
                    "SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_NAME = 'should_not_exist'"
                )
            },
        )
    assert check.data.rows == [[0]]
