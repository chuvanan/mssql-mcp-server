"""The MCP server: tool definitions and transport.

Glue only. The interesting decisions live in ``sql.py`` (what counts as a
read), ``approval.py`` (how the user consents to a write) and ``db.py`` (how
queries actually run).

The tool surface is split so that reads and writes are distinguishable to the
client *before* they run: ``read_query`` is annotated read-only, and
``execute_write`` is annotated destructive and always asks the user first.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from mssql_mcp_server.approval import Decision, request_approval
from mssql_mcp_server.config import ApprovalMode, ConfigError, Settings, get_settings
from mssql_mcp_server.db import ReadResult, run_read, run_write, sanitize_error
from mssql_mcp_server.sql import (
    SqlLexError,
    classify,
    is_read_only,
    needs_autocommit,
    validate_table_name,
)

logger = logging.getLogger("mssql_mcp_server")

__all__ = ["apply_approval_policy", "mcp", "run"]

mcp = FastMCP(
    name="mssql_mcp_server",
    instructions=(
        "Query a Microsoft SQL Server database. Use read_query for SELECT statements "
        "and execute_write for anything that modifies data or schema. Writes require "
        "explicit approval from the user, who is shown the SQL before it runs; a "
        "declined write is a normal outcome, not an error to retry. Use list_tables "
        "and describe_table to discover the schema rather than guessing column names."
    ),
)


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The outcome of an ``execute_write`` call."""

    status: Literal["executed", "declined", "cancelled"]
    statements: list[str] = field(default_factory=list)
    rows_affected: int | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class TableInfo:
    schema: str
    name: str


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    max_length: int | None = None
    default: str | None = None


@dataclass(frozen=True, slots=True)
class TableDescription:
    table: str
    columns: list[ColumnInfo] = field(default_factory=list)


def _settings() -> Settings:
    """Resolve configuration, turning a config error into a clear tool error."""
    try:
        return get_settings()
    except ConfigError as exc:
        raise ToolError(str(exc)) from None


def _fail(exc: BaseException) -> ToolError:
    """Log the real exception; return a sanitized one for the client."""
    logger.error("Database operation failed.", exc_info=exc)
    return ToolError(sanitize_error(exc))


def _split_qualified(table: str) -> tuple[str | None, str]:
    validate_table_name(table)  # raises on anything outside the strict allowlist
    if "." in table:
        schema, name = table.split(".", 1)
        return schema, name
    return None, table


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def read_query(
    sql: Annotated[
        str,
        Field(
            description=(
                "A single read-only T-SQL SELECT statement. Multiple statements, "
                "SELECT ... INTO, EXEC and any DML or DDL are rejected -- use "
                "execute_write for those."
            )
        ),
    ],
    ctx: Context,
    max_rows: Annotated[
        int | None,
        Field(description="Maximum rows to return for this call.", ge=1, le=100_000),
    ] = None,
) -> ReadResult:
    """Run a read-only SELECT query and return its columns and rows.

    The result is capped (see MSSQL_MAX_ROWS); when it is, `truncated` is true
    and you should refine the query rather than assume you saw everything.
    """
    if not sql or not sql.strip():
        raise ToolError("A SQL query is required.")

    settings = _settings()

    if not is_read_only(sql):
        raise ToolError(
            "This query was not accepted as read-only. read_query allows a single "
            "SELECT statement with no INTO clause, no EXEC and no data or schema "
            "changes. Use execute_write for statements that modify the database "
            "-- the user will be asked to approve them."
        )

    limit = max_rows or settings.max_rows
    try:
        result = await run_read(sql, limit)
    except Exception as exc:
        raise _fail(exc) from None

    if result.truncated:
        await ctx.warning(
            f"Result truncated to {limit} rows. Narrow the query or raise max_rows to see the rest."
        )
    return result


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
)
async def execute_write(
    sql: Annotated[
        str,
        Field(
            description=(
                "T-SQL that modifies data or schema (INSERT, UPDATE, DELETE, MERGE, "
                "CREATE, ALTER, DROP, TRUNCATE, EXEC). The user is shown this exact "
                "text and must approve it before it runs."
            )
        ),
    ],
    ctx: Context,
) -> WriteResult:
    """Run a statement that modifies the database, after the user approves it.

    The user sees the SQL and can reject it. A declined or cancelled write is a
    normal result, not an error: do not retry it without new instructions from
    the user. `rows_affected` is null when the statement reports no row count.
    """
    if not sql or not sql.strip():
        raise ToolError("A SQL statement is required.")

    settings = _settings()

    if is_read_only(sql):
        raise ToolError(
            "That is a read-only SELECT statement. Use read_query instead, which "
            "does not interrupt the user for approval."
        )

    kinds = classify(sql)
    try:
        statements = [str(k) for k in kinds]
    except SqlLexError:  # pragma: no cover -- classify() does not raise
        statements = ["UNKNOWN"]

    decision = await request_approval(ctx, sql=sql, kinds=kinds, settings=settings)

    if decision is Decision.UNAVAILABLE:
        # An operator problem, not something the model can fix by retrying.
        raise ToolError(
            "This client cannot show approval prompts, so writes are blocked. "
            "Retrying will not succeed. The operator can set MSSQL_APPROVAL_MODE=allow "
            "to disable the approval checkpoint, or use a client that supports MCP "
            "elicitation."
        )

    if decision is Decision.FORBIDDEN:  # pragma: no cover -- tool is unregistered
        raise ToolError("Writes are disabled (MSSQL_APPROVAL_MODE=readonly).")

    if decision is Decision.DECLINED:
        return WriteResult(
            status="declined",
            statements=statements,
            message=(
                "The user declined this statement. Nothing was executed. Do not retry "
                "it without new instructions from the user."
            ),
        )

    if decision is Decision.CANCELLED:
        return WriteResult(
            status="cancelled",
            statements=statements,
            message=(
                "The approval prompt was dismissed. Nothing was executed. Ask the user "
                "how they would like to proceed."
            ),
        )

    try:
        rows_affected = await run_write(sql, autocommit=needs_autocommit(sql))
    except Exception as exc:
        raise _fail(exc) from None

    affected = "unknown" if rows_affected is None else str(rows_affected)
    return WriteResult(
        status="executed",
        statements=statements,
        rows_affected=rows_affected,
        message=f"Statement executed. Rows affected: {affected}.",
    )


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def list_tables(
    ctx: Context,
    schema: Annotated[
        str | None,
        Field(description="Restrict results to a single schema, e.g. 'dbo'."),
    ] = None,
) -> list[TableInfo]:
    """List the base tables in the database."""
    settings = _settings()

    sql = (
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE'"
    )
    params: tuple[Any, ...] = ()
    if schema:
        sql += " AND TABLE_SCHEMA = ?"
        params = (schema,)
    sql += " ORDER BY TABLE_SCHEMA, TABLE_NAME"

    try:
        result = await run_read(sql, settings.max_rows, params)
    except Exception as exc:
        raise _fail(exc) from None

    return [TableInfo(schema=str(row[0]), name=str(row[1])) for row in result.rows]


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def describe_table(
    table: Annotated[
        str,
        Field(description="Table name, optionally schema-qualified, e.g. 'dbo.orders'."),
    ],
    ctx: Context,
) -> TableDescription:
    """Describe a table's columns, types and nullability.

    Prefer this over guessing column names and letting a query fail.
    """
    settings = _settings()

    try:
        schema, name = _split_qualified(table)
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    sql = (
        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, "
        "COLUMN_DEFAULT FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?"
    )
    params: tuple[Any, ...] = (name,)
    if schema:
        sql += " AND TABLE_SCHEMA = ?"
        params += (schema,)
    sql += " ORDER BY ORDINAL_POSITION"

    try:
        result = await run_read(sql, settings.max_rows, params)
    except Exception as exc:
        raise _fail(exc) from None

    if not result.rows:
        raise ToolError(f"Table {table!r} was not found, or it has no columns.")

    return TableDescription(
        table=table,
        columns=[
            ColumnInfo(
                name=str(row[0]),
                data_type=str(row[1]),
                nullable=str(row[2]).upper() == "YES",
                max_length=int(row[3]) if row[3] is not None else None,
                default=None if row[4] is None else str(row[4]),
            )
            for row in result.rows
        ],
    )


# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------


@mcp.resource("mssql://{table}/data", mime_type="text/csv")
async def table_data(table: str) -> str:
    """The first rows of a table, as CSV."""
    settings = _settings()

    try:
        safe_table = validate_table_name(table)
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    # validate_table_name has already restricted this to [A-Za-z0-9_.], so the
    # interpolation below cannot carry anything but an identifier.
    sql = f"SELECT TOP {int(settings.max_rows)} * FROM {safe_table}"
    try:
        result = await run_read(sql, settings.max_rows)
    except Exception as exc:
        raise _fail(exc) from None

    lines = [",".join(result.columns)]
    lines += [",".join("" if cell is None else str(cell) for cell in row) for row in result.rows]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def apply_approval_policy(settings: Settings) -> None:
    """Drop the write tool entirely when writes are disabled.

    Unregistering beats refusing: a tool the model cannot see is better than
    one it keeps trying and being rejected by.
    """
    if settings.approval_mode is ApprovalMode.READONLY:
        mcp.local_provider.remove_tool("execute_write")
        logger.info("MSSQL_APPROVAL_MODE=readonly: the execute_write tool is not available.")


def run() -> None:
    """Configure logging, apply the approval policy, and serve over stdio."""
    # stderr, never stdout: anything on stdout corrupts the MCP framing.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )

    settings = _settings_or_exit()
    logger.info("Starting MSSQL MCP server. %s", settings.describe())
    apply_approval_policy(settings)
    mcp.run()


def _settings_or_exit() -> Settings:
    try:
        return get_settings()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    run()
