"""Tests for the MCP tool surface, driven through an in-memory client."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mssql_mcp_server.server import mcp


@pytest.fixture
def client():
    return Client(mcp)


# --------------------------------------------------------------------------
# Surface
# --------------------------------------------------------------------------


async def test_server_name():
    assert mcp.name == "mssql_mcp_server"


async def test_the_four_tools_are_registered(client):
    async with client as c:
        names = {t.name for t in await c.list_tools()}
    assert names == {"read_query", "execute_write", "list_tables", "describe_table"}


async def test_the_old_single_tool_is_gone(client):
    """execute_sql ran DROP TABLE as readily as SELECT 1."""
    async with client as c:
        names = {t.name for t in await c.list_tools()}
    assert "execute_sql" not in names


async def test_read_tools_are_annotated_read_only(client):
    async with client as c:
        tools = {t.name: t for t in await c.list_tools()}
    for name in ("read_query", "list_tables", "describe_table"):
        assert tools[name].annotations.readOnlyHint is True, name


async def test_the_write_tool_is_annotated_destructive(client):
    async with client as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert tools["execute_write"].annotations.destructiveHint is True
    assert tools["execute_write"].annotations.readOnlyHint is False


async def test_context_is_not_exposed_as_a_tool_argument(client):
    """ctx is injected by annotation; the model must never see it."""
    async with client as c:
        for tool in await c.list_tools():
            assert "ctx" not in tool.inputSchema.get("properties", {}), tool.name


async def test_every_tool_documents_itself(client):
    async with client as c:
        for tool in await c.list_tools():
            assert tool.description and len(tool.description) > 30, tool.name


async def test_the_resource_template_is_registered(client):
    async with client as c:
        templates = {t.uriTemplate for t in await c.list_resource_templates()}
    assert "mssql://{table}/data" in templates


# --------------------------------------------------------------------------
# read_query
# --------------------------------------------------------------------------


async def test_read_query_returns_columns_and_rows(env, fake_db, client):
    fake_db(columns=["id", "name"], rows=[[1, "alice"], [2, "bob"]])
    async with client as c:
        result = await c.call_tool("read_query", {"sql": "SELECT id, name FROM users"})
    assert result.data.columns == ["id", "name"]
    assert result.data.rows == [[1, "alice"], [2, "bob"]]
    assert result.data.row_count == 2


async def test_read_query_rejects_a_write(env, no_db, client):
    async with client as c:
        with pytest.raises(ToolError, match="not accepted as read-only"):
            await c.call_tool("read_query", {"sql": "DROP TABLE users"})


async def test_read_query_rejecting_a_write_never_touches_the_database(env, no_db, client):
    """no_db turns any connection attempt into a failure."""
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("read_query", {"sql": "DELETE FROM users"})
    assert no_db == []


async def test_read_query_rejects_a_batch(env, no_db, client):
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("read_query", {"sql": "SELECT 1; DROP TABLE t"})


async def test_read_query_rejects_an_empty_query(env, no_db, client):
    async with client as c:
        with pytest.raises(ToolError, match="required"):
            await c.call_tool("read_query", {"sql": "   "})


async def test_read_query_rolls_back_and_never_commits(env, fake_db, client):
    fake_db(columns=["a"], rows=[[1]])
    async with client as c:
        await c.call_tool("read_query", {"sql": "SELECT a FROM t"})
    conn = fake_db.holder["last"]
    assert conn.rollbacks == 1
    assert conn.commits == 0


async def test_read_query_honours_the_row_cap(env, fake_db, client):
    env(MSSQL_MAX_ROWS="3")
    fake_db(columns=["a"], rows=[[i] for i in range(50)])
    async with client as c:
        result = await c.call_tool("read_query", {"sql": "SELECT a FROM t"})
    assert result.data.row_count == 3
    assert result.data.truncated is True


async def test_read_query_max_rows_argument_overrides_the_default(env, fake_db, client):
    fake_db(columns=["a"], rows=[[i] for i in range(50)])
    async with client as c:
        result = await c.call_tool("read_query", {"sql": "SELECT a FROM t", "max_rows": 2})
    assert result.data.row_count == 2
    assert result.data.max_rows == 2


async def test_read_query_rejects_a_nonsense_max_rows(env, fake_db, client):
    fake_db(columns=["a"], rows=[[1]])
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("read_query", {"sql": "SELECT a FROM t", "max_rows": 0})


async def test_read_query_reports_a_database_error_without_leaking(env, fake_db, client):
    import mssql_python

    fake_db(
        columns=None,
        execute_error=mssql_python.ProgrammingError("Invalid column name 'nope'", ""),
    )
    async with client as c:
        with pytest.raises(ToolError, match="Invalid column name"):
            await c.call_tool("read_query", {"sql": "SELECT nope FROM t"})


# --------------------------------------------------------------------------
# list_tables and describe_table
# --------------------------------------------------------------------------


async def test_list_tables(env, fake_db, client):
    fake_db(columns=["TABLE_SCHEMA", "TABLE_NAME"], rows=[["dbo", "users"], ["dbo", "orders"]])
    async with client as c:
        result = await c.call_tool("list_tables", {})
    assert [t.name for t in result.data] == ["users", "orders"]
    assert result.data[0].schema == "dbo"


async def test_list_tables_filters_by_schema_using_a_parameter(env, fake_db, client):
    """Schema is bound as a parameter, not interpolated."""
    fake_db(columns=["TABLE_SCHEMA", "TABLE_NAME"], rows=[["sales", "orders"]])
    async with client as c:
        await c.call_tool("list_tables", {"schema": "sales"})
    sql, params = fake_db.holder["last"].executed[0]
    assert "?" in sql
    assert params == ("sales",)


async def test_describe_table(env, fake_db, client):
    fake_db(
        columns=[
            "COLUMN_NAME",
            "DATA_TYPE",
            "IS_NULLABLE",
            "CHARACTER_MAXIMUM_LENGTH",
            "COLUMN_DEFAULT",
        ],
        rows=[
            ["id", "int", "NO", None, None],
            ["name", "varchar", "YES", 255, "('')"],
        ],
    )
    async with client as c:
        result = await c.call_tool("describe_table", {"table": "users"})
    assert [col.name for col in result.data.columns] == ["id", "name"]
    assert result.data.columns[0].nullable is False
    assert result.data.columns[1].nullable is True
    assert result.data.columns[1].max_length == 255


async def test_describe_table_binds_the_name_as_a_parameter(env, fake_db, client):
    fake_db(
        columns=[
            "COLUMN_NAME",
            "DATA_TYPE",
            "IS_NULLABLE",
            "CHARACTER_MAXIMUM_LENGTH",
            "COLUMN_DEFAULT",
        ],
        rows=[["id", "int", "NO", None, None]],
    )
    async with client as c:
        await c.call_tool("describe_table", {"table": "dbo.users"})
    sql, params = fake_db.holder["last"].executed[0]
    assert params == ("users", "dbo")
    assert "users" not in sql  # bound, not interpolated


async def test_describe_table_reports_a_missing_table(env, fake_db, client):
    fake_db(
        columns=[
            "COLUMN_NAME",
            "DATA_TYPE",
            "IS_NULLABLE",
            "CHARACTER_MAXIMUM_LENGTH",
            "COLUMN_DEFAULT",
        ],
        rows=[],
    )
    async with client as c:
        with pytest.raises(ToolError, match="not found"):
            await c.call_tool("describe_table", {"table": "nosuch"})


# --------------------------------------------------------------------------
# Resource
# --------------------------------------------------------------------------


async def test_resource_returns_csv(env, fake_db, client):
    fake_db(columns=["id", "name"], rows=[[1, "alice"], [2, "bob"]])
    async with client as c:
        contents = await c.read_resource("mssql://users/data")
    assert contents[0].text == "id,name\n1,alice\n2,bob"


async def test_resource_brackets_the_table_name(env, fake_db, client):
    fake_db(columns=["a"], rows=[[1]])
    async with client as c:
        await c.read_resource("mssql://users/data")
    assert "FROM [users]" in fake_db.holder["last"].sql[0]


async def test_resource_is_capped(env, fake_db, client):
    env(MSSQL_MAX_ROWS="10")
    fake_db(columns=["a"], rows=[[1]])
    async with client as c:
        await c.read_resource("mssql://users/data")
    assert "TOP 10" in fake_db.holder["last"].sql[0]


# --------------------------------------------------------------------------
# Configuration errors
# --------------------------------------------------------------------------


async def test_missing_configuration_is_reported_clearly(monkeypatch, no_db, client):
    from mssql_mcp_server.config import get_settings

    for key in ("MSSQL_DATABASE", "MSSQL_USER", "MSSQL_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()

    async with client as c:
        with pytest.raises(ToolError, match="MSSQL_DATABASE is required"):
            await c.call_tool("read_query", {"sql": "SELECT 1"})


async def test_a_bad_tool_name_is_reported_as_such(env, no_db, client):
    """Regression: this used to fail with 'Missing required database configuration'."""
    async with client as c:
        with pytest.raises(Exception, match="[Uu]nknown tool"):
            await c.call_tool("execute_sql", {"query": "SELECT 1"})
