# Microsoft SQL Server MCP Server

[![PyPI](https://img.shields.io/pypi/v/microsoft_sql_server_mcp)](https://pypi.org/project/microsoft_sql_server_mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

<a href="https://glama.ai/mcp/servers/29cpe19k30">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/29cpe19k30/badge" alt="Microsoft SQL Server MCP server" />
</a>

A [Model Context Protocol](https://modelcontextprotocol.io) server for SQL Server, built on
[FastMCP](https://gofastmcp.com) and the
[mssql-python](https://github.com/microsoft/mssql-python) driver.

**Reads and writes are separate tools, and every write asks you first.** Before any
`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER` or `EXEC` runs, you are shown the exact SQL, the
server and the database, and you approve or reject it. Nothing connects to the database until
you approve.

## Features

- **Approval checkpoint on every write**, with the SQL shown in full before it runs
- Separate `read_query` (read-only) and `execute_write` (destructive) tools, annotated so
  clients can treat them differently
- Schema discovery via `list_tables` and `describe_table`
- SQL Authentication, Windows Authentication, Azure SQL and LocalDB
- No system packages required — the driver bundles its own ODBC layer

## Quick start

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mssql": {
      "command": "uvx",
      "args": ["microsoft_sql_server_mcp"],
      "env": {
        "MSSQL_SERVER": "localhost",
        "MSSQL_DATABASE": "your_database",
        "MSSQL_USER": "your_username",
        "MSSQL_PASSWORD": "your_password"
      }
    }
  }
}
```

Connecting to a local development server with a self-signed certificate? Add
`"MSSQL_TRUST_SERVER_CERTIFICATE": "true"` — see [Encryption](#encryption).

## Tools

| Tool | What it does |
|---|---|
| `read_query` | Runs a single read-only `SELECT`. Rejects batches, `SELECT ... INTO`, `EXEC` and all DML/DDL. |
| `execute_write` | Runs a statement that modifies data or schema — **after you approve it**. |
| `list_tables` | Lists base tables, optionally filtered by schema. |
| `describe_table` | Lists a table's columns, types and nullability. |

A resource template, `mssql://{table}/data`, returns the first rows of a table as CSV.

## The approval workflow

When the model calls `execute_write`, you see a prompt like:

```
Approve this write against SQL Server?

  Server:     dbhost,1433
  Database:   sales_prod
  Statements: DELETE

  DELETE FROM orders WHERE created_at < '2020-01-01'

This will modify data or schema. Rejecting is safe: nothing has been
executed yet and no database connection has been opened.
```

Rejecting returns a normal result telling the model not to retry — it is not an error, and the
database is never touched.

`MSSQL_APPROVAL_MODE` controls this:

| Value | Behavior |
|---|---|
| `elicit` *(default)* | Ask before every write. If the client cannot show prompts, **refuse the write.** |
| `allow` | Skip the checkpoint entirely. Headless and CI use only. |
| `readonly` | Do not register `execute_write` at all; the model cannot write. |

**Your MCP client must support [elicitation](https://modelcontextprotocol.io/specification/basic/elicitation)**
for the default mode to work. If it does not, writes are refused with an explanatory message
rather than silently running. `MSSQL_APPROVAL_MODE=allow` disables the protection this server
exists to provide — set it deliberately, not to make an error go away.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `MSSQL_SERVER` | `localhost` | Hostname, `host\instance`, or `(localdb)\MSSQLLocalDB` |
| `MSSQL_DATABASE` | — | **Required** |
| `MSSQL_USER` | — | Required unless using Windows Authentication |
| `MSSQL_PASSWORD` | — | Required unless using Windows Authentication |
| `MSSQL_PORT` | — | Folded into the server as `host,port` |
| `MSSQL_WINDOWS_AUTH` | `false` | `true` uses Windows credentials |
| `MSSQL_ENCRYPT` | `true` | Forced on for Azure SQL |
| `MSSQL_TRUST_SERVER_CERTIFICATE` | `false` | `true` accepts self-signed certificates |
| `MSSQL_APPROVAL_MODE` | `elicit` | See above |
| `MSSQL_MAX_ROWS` | `1000` | Row cap for reads |
| `MSSQL_CONNECT_TIMEOUT` | `30` | Seconds |

### Encryption

This server uses ODBC Driver 18, which **encrypts by default and verifies the server
certificate**. A local SQL Server with a self-signed certificate will be rejected until you set:

```bash
MSSQL_TRUST_SERVER_CERTIFICATE=true
```

Do that for development only. For Azure SQL, encryption and certificate verification are forced
on and cannot be disabled.

### Windows Authentication

```bash
MSSQL_SERVER=localhost
MSSQL_DATABASE=your_database
MSSQL_WINDOWS_AUTH=true
```

### Azure SQL

```bash
MSSQL_SERVER=your-server.database.windows.net
MSSQL_DATABASE=your_database
MSSQL_USER=your_username
MSSQL_PASSWORD=your_password
```

## Security model

Read [SECURITY.md](SECURITY.md) before pointing this at anything that matters. In short:

**A read-only SQL login is the real protection.** The statement classifier is a guardrail, not a
security boundary. It is backed by two further defenses — the read path always rolls back, and a
tripwire fires if a read ever produces no result set — but neither replaces database permissions.

If you only need reads, use both `MSSQL_APPROVAL_MODE=readonly` and a login with `db_datareader`.

## Alternative installation

```bash
pip install microsoft_sql_server_mcp
```

```json
{
  "mcpServers": {
    "mssql": {
      "command": "python",
      "args": ["-m", "mssql_mcp_server"],
      "env": { "...": "..." }
    }
  }
}
```

## Development

```bash
git clone https://github.com/chuvanan/mssql-mcp-server.git
cd mssql-mcp-server
uv sync --group dev

make test              # unit and in-memory integration tests
make lint              # ruff + mypy
make inspect           # print the tool surface without connecting
make dev               # MCP Inspector, which can render the approval prompt
make check-connection  # diagnose connection settings
```

Live tests need a real server:

```bash
docker compose up -d mssql
MSSQL_LIVE_TESTS=1 make test-live
```

## Migrating from the pymssql version

This release replaces the low-level MCP SDK with FastMCP and `pymssql` with `mssql-python`.
Breaking changes:

- **`execute_sql` is gone.** Use `read_query` for SELECTs and `execute_write` for everything
  else. Writes now require approval.
- **`MSSQL_COMMAND` is gone.** Tool names are fixed.
- **Encryption defaults changed.** ODBC Driver 18 encrypts and verifies certificates by default;
  local servers usually need `MSSQL_TRUST_SERVER_CERTIFICATE=true`.
- **Results are structured**, returning `columns` and `rows` rather than CSV text. The resource
  template still returns CSV.
- **Tables are no longer enumerated** under `resources/list`. Use `list_tables`.
- **LocalDB connection strings pass through unchanged.** The old `(localdb)\X` → `.\X` rewrite
  existed for pymssql and would break ODBC.
- **FreeTDS is no longer required**, on any platform.

## License

MIT
