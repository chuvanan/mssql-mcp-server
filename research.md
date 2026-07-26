# Repository research report

## 1. Executive summary

This repository implements an MCP server that exposes Microsoft SQL Server to an
AI client. It is a deliberately narrow adapter rather than a general database
abstraction: it registers four tools and one resource template with FastMCP,
translates environment variables into `mssql-python` connection parameters,
classifies submitted T-SQL, requires approval before writes by default, and
converts database results into MCP-friendly structured data.

The central safety model is layered:

1. The database account should be least-privilege, ideally read-only.
2. `read_query` accepts only one statement that the lexer can prove is a bare
   `SELECT` without `INTO`, DML, DDL, `EXEC`, administrative commands, or
   passthrough surfaces.
3. Reads execute with `autocommit=False` and are always rolled back.
4. A read-path tripwire rejects any executed statement that produces no result
   set.
5. `execute_write` asks the MCP client for explicit approval before opening a
   database connection, unless the operator deliberately selects `allow` mode.
6. Credentials are kept out of settings representations, startup summaries,
   approval prompts, and returned connection-level error messages.

The implementation is thoughtfully tested and documents its threat model
honestly: the SQL classifier is a guardrail, not a security boundary. The most
important deployment control remains SQL Server permissions. The repository is
currently on a refactor branch that replaces the original single SQL tool and
`pymssql` implementation with FastMCP, Microsoft's `mssql-python` driver, and a
read/write split.

## 2. Repository shape and purpose

The project uses a `src/` layout and Hatchling packaging. The effective runtime
package is `src/mssql_mcp_server`; tests are under `tests/` and scripts under
`scripts/`.

| Area | Role |
|---|---|
| `src/mssql_mcp_server/__init__.py` | Lazy package entry point; exposes `main()` and avoids driver/config side effects on import. |
| `src/mssql_mcp_server/__main__.py` | Supports `python -m mssql_mcp_server`. |
| `config.py` | Environment parsing, validation, Azure/LocalDB/port rules, authentication mode, and cached `Settings`. |
| `sql.py` | T-SQL lexer, statement splitting, read/write classification, autocommit decisions, and identifier allowlisting. |
| `approval.py` | MCP elicitation capability check, approval prompt construction, decision mapping, and fail-closed behavior. |
| `db.py` | Connection creation, threaded blocking execution, rollback/commit semantics, result limiting, JSON coercion, and error sanitization. |
| `server.py` | FastMCP instance, public tools/resource, result models, policy application, logging, and stdio startup. |
| `scripts/check_connection.py` | Standalone diagnostic using the same configuration and driver. |
| `tests/conftest.py` | Hand-built database fakes and environment fixtures. |
| `tests/test_sql.py` | Adversarial lexer/classifier corpus. |
| `tests/test_approval.py` | Direct approval decision tests. |
| `tests/test_config.py` | Configuration and driver-key contracts. |
| `tests/test_db.py` | Transaction, connection lifecycle, coercion, and error behavior. |
| `tests/test_server.py` | Tool/resource surface and individual tool behavior. |
| `tests/test_integration.py` | End-to-end in-memory MCP calls, with optional live SQL Server tests. |
| `tests/test_performance.py` | Bounded fetches, worker-thread execution, concurrency, and cleanup. |
| `tests/test_security.py` | Approval bypass, injection, and credential leakage checks. |
| `.github/workflows/` | CI, release automation, dependency/security checks, and Docker build checks. |
| `Dockerfile`, `docker-compose*.yml` | Container image and local SQL Server/MCP test topology. |

There is no application database schema or migration layer. The server operates
against an existing SQL Server selected entirely through environment variables.

## 3. Runtime architecture and control flow

### Startup

The console script is `mssql_mcp_server = mssql_mcp_server:main`. `main()` lazily
imports `server.run`, which is important because ordinary package import and
`fastmcp inspect` must work without database variables or an import-time driver
connection.

`server.run()`:

1. Configures logging to stderr. This is required because stdout carries MCP
   framing and must remain clean.
2. Resolves and validates cached settings.
3. Logs a redacted one-line configuration summary.
4. Removes `execute_write` from the local FastMCP provider when approval mode is
   `readonly`.
5. Starts FastMCP over stdio.

Configuration errors terminate startup with exit code 2. Tool calls also resolve
settings lazily and convert configuration errors into `ToolError`, which makes
the server testable and gives clients a direct explanation.

### Read request

`read_query` validates non-empty input, resolves settings, then calls
`is_read_only`. Rejected SQL never reaches the driver. Accepted SQL is passed to
`run_read`, which dispatches `_read_sync` through `anyio.to_thread.run_sync`.

The synchronous read path opens one connection for the request, executes the
SQL, checks `cursor.description`, fetches at most `max_rows + 1`, trims the extra
row when necessary, coerces cells to JSON-safe values, rolls back, closes the
cursor, and lets the context manager close the connection. A warning is sent to
the MCP client when the result was capped.

The returned `ReadResult` is column-oriented:

```text
{
  columns: ["id", "name"],
  rows: [[1, "alice"], [2, "bob"]],
  row_count: 2,
  truncated: false,
  max_rows: 1000
}
```

Column names are not repeated in every row, reducing token overhead for wide
results.

### Write request

`execute_write` validates non-empty input and rejects a statement that the
classifier recognizes as a read, directing it to `read_query`. Everything else
is treated as potentially destructive. The SQL is classified for display in the
approval prompt, but the original SQL text is executed unchanged after approval.

In default `elicit` mode, the server first checks the connected client's MCP
elicitation capability. If unavailable, the write is blocked with an actionable
error. If available, the user sees the server, database, statement kinds, and
SQL. SQL longer than 4000 characters is visibly truncated in the prompt. The
user can approve, reject, or dismiss the prompt.

Only an explicit accepted response containing the exact approve option is an
approval. Rejection returns a normal `WriteResult` with `status="declined"`;
dismissal returns `status="cancelled"`. Neither opens a connection. This is
intentional: returning a normal result discourages automatic model retry loops.

Approved SQL runs through `run_write` in a worker thread. Ordinary writes use an
explicit transaction and commit once. Statements identified by
`needs_autocommit`—for example `CREATE DATABASE`, `ALTER DATABASE`, `BACKUP`,
`RESTORE`, and `RECONFIGURE`—use driver autocommit and do not call `commit()`.
The result reports a non-negative driver row count or `null` when SQL Server
reports no meaningful count (`None` or a negative value).

### Metadata tools

`list_tables` queries `INFORMATION_SCHEMA.TABLES` for base tables and optionally
binds a schema filter as `?`; it orders by schema and table name and returns
`TableInfo` objects.

`describe_table` accepts an unqualified or `schema.table` identifier, validates
it, then binds the table and optional schema as query parameters against
`INFORMATION_SCHEMA.COLUMNS`. It returns name, data type, nullable status,
character maximum length, and default expression in ordinal order.

### Resource template

`mssql://{table}/data` is a `text/csv` resource. The table path is validated and
bracket-quoted, then the server runs `SELECT TOP <max_rows> * FROM <table>` via
the same rollback-protected read path. The resource returns a header line and
one comma-joined line per row.

## 4. Configuration behavior

`load_settings()` trims environment values and turns blank values into unset.
`MSSQL_DATABASE` is always required. SQL authentication requires both
`MSSQL_USER` and `MSSQL_PASSWORD`; Windows authentication instead emits
`trusted_connection=yes` and deliberately discards supplied SQL credentials.

Supported variables and behavior:

| Variable | Behavior |
|---|---|
| `MSSQL_SERVER` | Defaults to `localhost`; accepts host, `host\\instance`, explicit `host,port`, or `(localdb)\\Instance`. |
| `MSSQL_DATABASE` | Required. |
| `MSSQL_USER`, `MSSQL_PASSWORD` | Required for SQL authentication; password is `repr=False`. |
| `MSSQL_PORT` | Parsed as 1–65535 and folded into `server` as `host,port`; ignored when server already has a port, names an instance, or is LocalDB. |
| `MSSQL_WINDOWS_AUTH` | Forgiving boolean parser; enables integrated authentication. |
| `MSSQL_ENCRYPT` | Defaults true for normal servers. |
| `MSSQL_TRUST_SERVER_CERTIFICATE` | Defaults false; intended for local self-signed development servers. |
| `MSSQL_APPROVAL_MODE` | `elicit` (default), `allow`, or `readonly`; invalid values are configuration errors. |
| `MSSQL_MAX_ROWS` | Positive integer, default 1000; invalid/non-positive input falls back to the default with a warning. |
| `MSSQL_CONNECT_TIMEOUT` | Positive integer seconds, default 30. |

Connection arguments are passed as a dictionary to `mssql_python.connect`, not
as a hand-built connection string. This lets the driver escape passwords with
characters such as `;`, `=`, braces, or spaces. The tests explicitly compare
emitted keys with the driver's allowlist and reject reserved keys.

Azure is detected by the hostname suffix `.database.windows.net` after removing
an optional port or instance suffix. Azure forces encryption on and certificate
trust override off, logging warnings when the operator asked for an unsafe
override. LocalDB is passed through unchanged because ODBC Driver 18 understands
the `(localdb)\\...` form; the old pymssql rewrite is intentionally gone.

Settings are cached with `lru_cache(maxsize=1)`. Tests clear the cache between
cases; a long-lived process therefore treats environment changes as ineffective
after the first settings resolution.

## 5. SQL lexer and classifier

The lexer is intentionally independent of the rest of the package. It removes
comments, discards string contents and quoted identifier contents, tracks
parenthesis depth, recognizes words/numbers/punctuation, and treats top-level
semicolons as statement boundaries. It supports nested T-SQL block comments,
escaped single quotes, `N'...'` literals, double-quoted identifiers, and
bracketed identifiers. Unterminated literals, comments, or identifiers raise
`SqlLexError`; `classify()` converts that to `UNKNOWN`, while `is_read_only()`
returns false.

Top-level write tokens include DML (`INSERT`, `UPDATE`, `DELETE`, `MERGE`), DDL,
`EXEC`, permissions, transaction control, `USE`, `SET`, `DECLARE`, `WAITFOR`,
bulk/admin operations, and linked-server/passthrough functions
(`OPENQUERY`, `OPENROWSET`, `OPENDATASOURCE`). The list is intentionally broad
to prefer false write classifications over false read classifications.

CTEs are classified by the statement they feed: `WITH ... DELETE` is a delete,
and CTE-body words at nested depth do not become top-level write indicators.
`SELECT ... INTO` is classified as an insert/write. A read requires exactly one
parsed statement whose effective head is `SELECT`, with no top-level `INTO` or
write token.

`GO` at top level is rejected as a sqlcmd batch separator. Empty statements are
dropped when splitting, so trailing or repeated semicolons do not create fake
statements. Multi-statement SQL is never accepted by `read_query`, but is shown
and can be approved as one write request.

`validate_table_name` is stricter than SQL Server identifiers: only ASCII
letters, digits, and underscores are allowed, with at most one dot. Valid names
are converted to bracketed identifiers. This protects the two interpolation
sites: the table resource and the fixed `TOP` query. Metadata queries bind their
names instead.

## 6. Database layer details

The driver is blocking native code, so all connection and cursor work is moved
off the event loop. This is operationally important because a blocked event loop
could prevent the MCP elicitation round trip from completing and deadlock the
approval path.

Each request gets a new connection; the code relies on the driver underneath for
pooling. Cursors are explicitly closed in `finally` blocks and connections are
closed by context managers even when execution fails.

`jsonable()` handles common SQL Server result types as follows:

- `None`, booleans, integers, floats, and strings pass through.
- `Decimal` becomes a string to preserve exact precision, especially for money.
- date/time values become ISO-8601 strings.
- `timedelta` becomes its string representation.
- bytes-like values become hexadecimal strings, avoiding invalid UTF-8 failures.
- UUIDs become canonical strings.
- unknown values fall back to `str(value)`.

Error handling distinguishes connection errors from query errors. Interface and
operational connection errors are replaced with a generic connection message so
driver text cannot expose the connection string and password. Syntax/data/
integrity errors are passed through after scrubbing configured user/password
values and common `PWD=` or `password '...'` patterns, because query detail helps
the model correct SQL. Unknown exceptions receive a generic message. Full
exceptions are logged server-side with traceback, while sanitized messages cross
the MCP boundary.

## 7. Security model and residual risk

The repository's `SECURITY.md` is unusually explicit about the boundary:
classifier logic is not equivalent to database authorization. A determined SQL
payload may exploit linked servers, external procedures, CLR code, scalar
functions with side effects, or effects that transaction rollback cannot undo.
The recommended mitigation is a dedicated login with only the required table
permissions, never `sa` or `sysadmin`; for read-only deployments, combine a
`db_datareader`-style account with `MSSQL_APPROVAL_MODE=readonly`.

The approval design is fail-closed in default mode. Capability checks and
elicitation calls are wrapped so a client that cannot prompt, a transport with
no session, or a prompt failure results in no write. The `allow` mode is an
intentional escape hatch for headless/CI operation and is noisy both at settings
load and on every write.

The prompt includes the exact SQL up to the 4000-character display limit, the
target server/database, and statement categories. The truncation marker is
visible, but a user approving a very long statement still does not see the full
payload. That is a documented residual risk.

## 8. Tests and quality signals

The suite contains 410 collected tests, with three live tests deselected by the
default `-m 'not live'` configuration. The tests use hand-built fakes rather than
loose mocks so that details such as `description is None`, `rowcount == -1`,
`fetchmany()` limits, commits, rollbacks, and connection closure remain
observable.

Coverage areas include:

- adversarial SQL strings, comments, literals, brackets, CTEs, batches, `GO`,
  malformed input, and table-name injection;
- every approval outcome and capability failure;
- no connection before approval;
- read rollback and tripwire behavior;
- write commit/autocommit behavior;
- bounded result fetching and event-loop responsiveness;
- per-request connections and cleanup under concurrency/failure;
- JSON coercion and decimal precision;
- password and connection-string leakage prevention;
- FastMCP annotations, descriptions, resource registration, and readonly tool
  removal;
- optional live SQL Server tests using Docker Compose.

Local validation observations in this environment:

- The direct virtualenv test run progresses through the suite, but the full run
  did not complete normally: with the repository's timeout configuration, the
  first `test_db.py` cases stalled in the AnyIO worker-thread lifecycle and were
  terminated after timeout. This looks environment/runtime-specific and should
  be reproduced in CI before treating it as a product defect.
- Ruff currently reports four `UP038` violations in `db.py` for tuple-style
  `isinstance` calls. The configured lint command therefore fails unless the
  project pins an older Ruff behavior or updates those expressions.
- `uv run` could not acquire its cache lock because `/home/anchu/.cache/uv` is
  read-only in this workspace; direct `.venv/bin/pytest` was used instead.

The GitHub CI workflow runs Python 3.11–3.13 on Linux, Windows, and macOS,
performs Ruff and mypy checks, runs tests, builds a package, runs Bandit and
pip-audit (currently allowed to fail with `|| true`), and builds the Docker
image. The separate security workflow also runs scheduled dependency checks and
CodeQL. The project metadata intentionally caps Python at `<3.14` because the
current `mssql-python` 1.x wheels do not cover CPython 3.14.

## 9. Deployment and operations

The default Docker image is Debian/glibc-based Python 3.12. Alpine is avoided
because `mssql-python` ships manylinux native bindings. The image installs
`libstdc++6` and the Kerberos runtime, installs runtime requirements, copies the
source package, and performs an import smoke test at build time. It does not
install FreeTDS or system unixODBC because the Microsoft driver bundles its own
ODBC layer.

The primary Compose file starts SQL Server 2022 and the MCP server, waits for a
SQL Server health check, maps host port 1434 to container port 1433 by default,
uses a persistent named volume, and enables trust of the container's self-signed
certificate. The example Compose file is a more generic two-service topology.
Both examples contain development credentials and should not be used unchanged
for production.

`check_connection.py` loads the same settings as the server, prints the redacted
summary, connects, prints the SQL Server version, and lists up to five base
tables. It gives a certificate hint for local development failures. It is useful
for separating configuration/connectivity problems from MCP protocol problems.

## 10. Specific findings and improvement opportunities

### Confirmed limitations

1. **The resource is not a fully compliant CSV serializer.** `table_data()`
   joins fields with commas and does not quote values containing commas, quotes,
   or newlines. A table value such as `hello, world` will produce extra CSV
   columns. `None` is represented as an empty field, which also makes it
   indistinguishable from an actual empty string. Python's `csv` module or an
   equivalent escaping implementation would make the declared `text/csv`
   contract reliable.
2. **Metadata truncation is silent.** `list_tables()` and `describe_table()`
   use `settings.max_rows` but ignore `ReadResult.truncated` and do not warn the
   client. A database with more than the cap can yield an incomplete schema
   without an explicit signal. `list_tables` could either return metadata about
   truncation or issue the same warning as `read_query`.
3. **The classifier is intentionally not a full T-SQL parser.** It does not
   attempt to prove semantic safety, cannot inspect procedure/function bodies,
   and has residual edge cases around SQL Server features. It also clamps an
   unmatched closing parenthesis rather than reporting unbalanced parentheses;
   malformed SQL will normally fail at the database or tripwire, but the lexer
   should not be interpreted as a complete parser.
4. **`execute_write` can submit arbitrary SQL after approval.** This is
   intentional and necessary for schema/admin operations, but approval is one
   decision for the whole original batch. Operators must understand that a
   multi-statement request can contain multiple effects.
5. **The approval prompt is bounded.** Long SQL is visibly truncated at 4000
   characters, but the user cannot inspect the whole statement through the
   prompt itself.

### Maintenance findings

1. Ruff's current configured rule set does not pass against `db.py`; this is a
   small compatibility/style issue but makes the documented `make lint` command
   fail in the inspected environment.
2. The CI security scans intentionally use `|| true`, so they generate reports
   without failing the pipeline. That is appropriate for advisory scanning but
   should be understood as non-blocking security enforcement.
3. `close_fixed_issues.sh` contains historical issue-closing text that still
   describes pymssql-era behavior (for example LocalDB rewriting and TDS
   version details). It is an operational GitHub script, not runtime code, but
   its claims are stale relative to the current implementation.
4. `SECURITY.md` uses `security@example.com` as the reporting address. That is a
   placeholder rather than an actionable project security contact and should be
   replaced before public release.
5. The Compose files use simple example passwords and, in the main file,
   default to `sa`. This is acceptable for local development but reinforces the
   need for production-specific secrets and least-privilege accounts.

## 11. Overall assessment

The repository is a focused, security-conscious refactor with clear separation
of responsibilities. Its strongest properties are the explicit approval
boundary, no-connection-before-approval invariant, rollback-protected reads,
thread offloading for the blocking driver, credential scrubbing, and unusually
strong adversarial tests around the classifier. The design also communicates
its limits rather than claiming that lexical classification alone prevents SQL
damage.

The main things to resolve before treating it as production-finished are the
CSV serialization correctness, metadata truncation visibility, current lint
failure, reproducibility of the test-suite stall observed in this environment,
and replacement of placeholder/stale operational documentation. With a
least-privilege SQL account and default elicitation/readonly policy, the runtime
model is substantially safer than the predecessor's single unrestricted SQL
tool while remaining intentionally capable of executing arbitrary approved
T-SQL.
