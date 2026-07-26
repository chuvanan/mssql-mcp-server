# Tutorial: exploring a SQL Server database with Claude

A complete walkthrough — a database from nothing, sample data, a configured MCP
client, and prompts that show what the server is actually for.

Every output in this document was captured from a real run against the dataset
below. If you follow along you should see the same numbers.

**Time:** about 15 minutes. **You need:** Docker, [uv](https://docs.astral.sh/uv/),
and an MCP client (Claude Desktop, Claude Code, or opencode).

---

## 1. Start SQL Server

From a clone of this repository:

```bash
git clone https://github.com/chuvanan/mssql-mcp-server.git
cd mssql-mcp-server
docker compose up -d mssql
```

That starts SQL Server 2022 on **host port 1434** (container 1433, remapped so it
does not collide with a local install). Wait for the health check to pass:

```bash
until [ "$(docker inspect -f '{{.State.Health.Status}}' mssql-mcp-server-mssql-1)" = "healthy" ]; do
  sleep 5; done && echo "ready"
```

First start pulls a ~1.5 GB image and takes a minute or two.

## 2. Load the demo data

[`examples/demo_data.sql`](examples/demo_data.sql) builds a storefront at a
useful scale — **122,200 rows** — so aggregates mean something and the row cap
is a real constraint rather than a contrived one.

```bash
docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd \
  -C -S localhost -U sa -P 'StrongPassword123!' \
  -i /dev/stdin < examples/demo_data.sql
```

```text
table_name  rows
----------- -----------
customers          2000
products            200
orders            30000
order_items       90000
TOTAL            122200
```

Loads in about three seconds — generation is set-based, not 122,000 `INSERT`
statements. It is also **deterministic**: every value derives from the row
number via modular arithmetic rather than `RAND()` or `NEWID()`, so reloading
gives byte-identical data and the query results quoted below are the ones you
will see.

The data is deliberately *skewed*, because uniform test data makes every
analytical question a tie:

| | |
|---|---|
| Countries | `US` 440 customers down to `NG` 60 |
| Orders per customer | 15 to 675 — a few heavy accounts, a long tail |
| Customers with no orders | 500 of 2000, so data-quality questions return rows |
| Order volume | 12,000 in 2023, 18,000 in 2024 — a growth trend to find |
| Referrals | ~⅓ of `referred_by` values are `NULL` |
| Prices paid | every seventh line was discounted 10% off list |

The script also creates a dedicated login, `mcp_demo`, in the `db_datareader`
and `db_datawriter` roles. **Use it rather than `sa`.** Database permissions —
not this server's SQL classifier — are what actually bound what the model can
do; [section 8](#8-lock-it-down) tightens this further.

## 3. Verify the connection

Before involving an MCP client, confirm the settings work. This separates
"database is misconfigured" from "MCP is misconfigured", which are very
different problems:

```bash
export MSSQL_SERVER=localhost MSSQL_PORT=1434 \
       MSSQL_USER=mcp_demo MSSQL_PASSWORD='DemoPassword123!' \
       MSSQL_DATABASE=storefront MSSQL_TRUST_SERVER_CERTIFICATE=true
make check-connection
```

```text
Configuration: server=localhost,1434 database=storefront auth=sql user=mcp_demo
encrypt=yes trust_cert=yes approval=elicit max_rows=1000

Connecting...
Connected. Microsoft SQL Server 2022 (RTM-CU26) ...

Found 4 table(s):
  - dbo.customers
  - dbo.order_items
  - dbo.orders
  - dbo.products
```

`MSSQL_TRUST_SERVER_CERTIFICATE=true` is required here and only here: ODBC
Driver 18 verifies certificates by default, and the container's is self-signed.
Never set it against a real server.

## 4. Configure your MCP client

<details open>
<summary><b>Claude Desktop</b> — <code>claude_desktop_config.json</code></summary>

```json
{
  "mcpServers": {
    "storefront": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/chuvanan/mssql-mcp-server.git",
        "mssql_mcp_server"
      ],
      "env": {
        "MSSQL_SERVER": "localhost",
        "MSSQL_PORT": "1434",
        "MSSQL_DATABASE": "storefront",
        "MSSQL_USER": "mcp_demo",
        "MSSQL_PASSWORD": "DemoPassword123!",
        "MSSQL_TRUST_SERVER_CERTIFICATE": "true"
      }
    }
  }
}
```
</details>

<details>
<summary><b>opencode</b> — <code>opencode.json</code></summary>

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "storefront": {
      "type": "local",
      "enabled": true,
      "command": [
        "uvx",
        "--from", "git+https://github.com/chuvanan/mssql-mcp-server.git",
        "mssql_mcp_server"
      ],
      "environment": {
        "MSSQL_SERVER": "localhost",
        "MSSQL_PORT": "1434",
        "MSSQL_DATABASE": "storefront",
        "MSSQL_USER": "mcp_demo",
        "MSSQL_PASSWORD": "DemoPassword123!",
        "MSSQL_TRUST_SERVER_CERTIFICATE": "true"
      }
    }
  }
}
```
</details>

<details>
<summary><b>Claude Code</b> — one command</summary>

```bash
claude mcp add storefront \
  --env MSSQL_SERVER=localhost --env MSSQL_PORT=1434 \
  --env MSSQL_DATABASE=storefront \
  --env MSSQL_USER=mcp_demo --env MSSQL_PASSWORD='DemoPassword123!' \
  --env MSSQL_TRUST_SERVER_CERTIFICATE=true \
  -- uvx --from git+https://github.com/chuvanan/mssql-mcp-server.git mssql_mcp_server
```
</details>

Restart the client. You should see four tools: `read_query`, `execute_write`,
`list_tables`, `describe_table`.

Want to see the surface without a client? `make inspect` prints it — and works
without any database running.

---

## 5. Explore the data

### Start with the schema

> **Prompt:** *What tables are in this database, and how are they related?*

The model calls `list_tables`, then `describe_table` on each. It gets:

```text
dbo.customers   dbo.orders   dbo.order_items   dbo.products
```

```text
orders
  id           int        nullable=False
  customer_id  int        nullable=False
  placed_at    datetime2  nullable=False
  status       nvarchar   nullable=False  max_len=20
```

This matters more than it looks. Without `describe_table` the model guesses
column names, writes a failing query, reads the error and retries — three round
trips to learn what one call answers.

### Ask a question in plain language

> **Prompt:** *Which five countries generate the most revenue from shipped orders?*

```sql
SELECT TOP 5 c.country, COUNT(DISTINCT o.id) AS orders,
       CAST(SUM(oi.quantity * oi.unit_price) AS DECIMAL(12,2)) AS revenue
FROM customers c
JOIN orders o ON o.customer_id = c.id
JOIN order_items oi ON oi.order_id = o.id
WHERE o.status = 'shipped'
GROUP BY c.country ORDER BY revenue DESC
```

```json
{
  "columns": ["country", "orders", "revenue"],
  "rows": [["US", 5100, "7643013.53"],
           ["VN", 3915, "5556629.56"],
           ["DE", 2490, "3450329.17"],
           ["IN", 2055, "2877139.82"],
           ["BR", 1845, "2429500.83"]]
}
```

Note `revenue` comes back as a **string**. `DECIMAL` is serialized as text
deliberately — routing money through a float loses precision, and silently
wrong totals are worse than obviously awkward ones. At this scale the totals
run to eight figures, which is exactly where float error starts to show.

### Aggregate over the whole fact table

> **Prompt:** *What are the three best-selling products by units?*

```json
[["Studio Webcam",     "Peripherals", 9000],
 ["Ergonomic Trackpad", "Peripherals", 4500],
 ["Wireless Earbuds",   "Audio",       4050]]
```

That scanned all 90,000 line items but returned three rows — the shape you want
from a database tool. The model gets the answer, not the data.

> **Prompt:** *Did order volume grow between 2023 and 2024?*

```json
[[2023, 12000], [2024, 18000]]
```

### More prompts to try

| Use case | Prompt |
|---|---|
| Cohort analysis | *Group customers by signup month and show how many placed an order within 90 days.* |
| Top-N | *What are the ten best-selling products by units, and by revenue? Do the lists differ, and why?* |
| Concentration | *What share of revenue comes from the top 1% of customers?* |
| Data quality | *Which customers have never ordered? How many are there?* (500) |
| Referrals | *How many customers were referred, and who referred the most people?* (1,333 referred) |
| Pricing drift | *Find line items where the price paid differs from the product's current list price.* |
| Time series | *Show monthly order counts for 2024 and tell me whether there is a trend.* |
| Segmentation | *Compare average order value by country for shipped orders only.* |
| Discontinued stock | *Are we still selling discontinued products? Which ones, and how recently?* |
| Schema-first | *Describe every table first, then tell me which foreign keys have no supporting index.* |

The referral prompt is a good NULL test — `referred_by` is a nullable
self-reference, and comes back as a real `null` rather than an empty string:

```json
[1, "Bao Haddad",   "US", null],
[2, "Chidi Silva",  "US", 1],
[3, "Dana Martins", "US", null],
[4, "Elif Kim",     "VN", 2]
```

The concentration and data-quality prompts are the ones worth trying first —
they only have interesting answers because the data is skewed, and they are the
kind of question you would actually ask a warehouse.

### Reads cannot write, whatever the prompt says

> **Prompt:** *Use read_query to run: DELETE FROM orders WHERE id = 1*

```text
This query was not accepted as read-only. read_query allows a single SELECT
statement with no INTO clause, no EXEC and no data or schema changes. Use
execute_write for statements that modify the database -- the user will be
asked to approve them.
```

No connection was opened. The same refusal covers `SELECT * INTO ...` (which
creates a table), `EXEC`, and multi-statement batches like
`SELECT 1; DROP TABLE t`.

The classifier lexes SQL rather than pattern-matching it, which makes it
correct in both directions — try these two:

| Statement | Accepted? | Why |
|---|---|---|
| `SELECT 1 -- ; DROP TABLE t` | **yes** | `--` starts a real comment; the `DROP` is inert text and nothing else runs. |
| `SELECT '--' ; DROP TABLE t` | **no** | `'--'` is a *string*, not a comment, so the `;` really does separate two statements. |

Naive comment-stripping gets the second one backwards and runs the `DROP`.

---

## 6. The write-approval checkpoint

This is the part the server exists for.

> **Prompt:** *Put the Studio Webcam (PR-0001) on sale at $19.99.*

Your client shows this and waits:

```text
Approve this write against SQL Server?

  Server:     localhost,1434
  Database:   storefront
  Statements: UPDATE

UPDATE products SET unit_price = 19.99 WHERE sku = 'PR-0001'

This will modify data or schema. Rejecting is safe: nothing has been executed
yet and no database connection has been opened.
```

**Choose "Reject".** The model receives:

```json
{
  "status": "declined",
  "rows_affected": null,
  "statements": ["UPDATE"],
  "message": "The user declined this statement. Nothing was executed. Do not
              retry it without new instructions from the user."
}
```

A rejection is a *normal result*, not an error. That is deliberate: an error
invites the model to retry, and a model that retries trains you to click
Approve without reading. Verify nothing happened:

> **Prompt:** *What is PR-0001 priced at now?* → still `55.70`.

Now try one that touches many rows, and **approve** it:

> **Prompt:** *Discontinue every Networking product over $600.*

```json
{"status": "executed", "rows_affected": 6,
 "message": "Statement executed. Rows affected: 6."}
```

`rows_affected` is the point here: at this scale you cannot eyeball whether a
`WHERE` clause was too broad, so the count is your confirmation that six rows
changed rather than all two hundred. Verify it:

> **Prompt:** *How many Networking products are discontinued now?* → `8`

Eight, not six: two were already discontinued before your update. `rows_affected`
counts the rows the `WHERE` clause *matched*, not the ones whose value actually
changed — a distinction worth knowing before you read a row count as an
impact assessment.

### Things worth trying

- **A destructive statement.** *Drop the order_items table.* The prompt shows
  `Statements: DDL`. Reject it, then confirm all 90,000 rows are still there.
- **A dangerously broad update.** *Set every order to shipped.* The prompt shows
  the missing `WHERE` clause plainly — this is the case the checkpoint exists
  for, and 30,000 rows is why.
- **A batch.** *Mark order 12 shipped and delete order 99999.* One prompt lists
  `Statements: UPDATE, DELETE`, and approval covers **both**. Approval is one
  decision for the whole batch — worth knowing before you approve a long one.
- **A read sent to the write tool.** It is redirected to `read_query` rather
  than interrupting you.

### If your client cannot prompt

Not every MCP client implements elicitation yet. On one that does not:

```text
This client cannot show approval prompts, so writes are blocked. Retrying will
not succeed. The operator can set MSSQL_APPROVAL_MODE=allow to disable the
approval checkpoint, or use a client that supports MCP elicitation.
```

The server **fails closed** — no prompt means no write. Reads work normally.

---

## 7. The table resource

Besides tools, the server exposes `mssql://{table}/data`, which returns rows as
CSV. Attach it the way your client attaches context (in Claude Desktop, the
paperclip → the `storefront` server).

```csv
id,sku,name,category,unit_price,discontinued
1,PR-0001,Studio Webcam,Peripherals,55.70,False
2,PR-0002,Ergonomic Trackpad,Peripherals,91.41,False
3,PR-0003,Pro Mouse,Peripherals,127.12,False
```

Useful when you want the model to *see* a small table rather than query it —
`products` has 200 rows and fits comfortably. Do not attach `order_items`: at
90,000 rows it is capped at `MSSQL_MAX_ROWS` and you would be handing the model
a truncated fraction of the table. Query that one instead.

---

## 8. Lock it down

Everything so far used a login that can write. For real use, decide deliberately.

### Read-only deployment

Two independent controls, and you want both:

```sql
-- 1. The database says no.
ALTER ROLE db_datawriter DROP MEMBER mcp_demo;
```

```bash
# 2. The server does not even offer the tool.
MSSQL_APPROVAL_MODE=readonly
```

With `readonly`, `execute_write` is **not registered at all** — the model cannot
see it, so it will not try and be refused. Confirm with `make inspect`: three
tools instead of four.

Belt and braces is the point. The permission is the real control; the mode
saves tokens and stops retry loops. If the classifier ever has a bug, the
permission still holds.

### Row caps

`MSSQL_MAX_ROWS` (default 1000) bounds every read. With 90,000 line items this
is not hypothetical:

> **Prompt:** *Show me all the order line items.*

```json
{"row_count": 1000, "truncated": true, "max_rows": 1000}
```

`truncated: true` plus a warning to the client, so the model knows it is looking
at 1,000 of 90,000 rows and should aggregate instead of paging. Compare a query
that fits:

> **Prompt:** *List every product.* → `{"row_count": 200, "truncated": false}`

The cap is a guardrail against flooding the context window, not a paging API.
The right response to hitting it is a `GROUP BY`, and telling the model that it
was truncated is what prompts it to write one.

### What this server does not protect against

Read [SECURITY.md](SECURITY.md). The short version: the SQL classifier is a
guardrail, not a security boundary. Linked servers, extended procedures and CLR
can all escape it. A least-privilege login is the control that holds.

---

## 9. Clean up

```bash
docker compose down -v          # remove the container and its data
```

Drop `-v` to keep the data for next time.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Could not connect... set MSSQL_TRUST_SERVER_CERTIFICATE=true` | ODBC Driver 18 verifies certificates; the container's is self-signed. |
| `MSSQL_DATABASE is required.` | The client is not passing env vars. They go in `env` (Claude) or `environment` (opencode). |
| `Login failed for user 'mcp_demo'` | The seed script has not run, or it ran against the wrong container. |
| Writes always refused | The client does not support elicitation — see [section 6](#if-your-client-cannot-prompt). |
| Connection refused on 1433 | The compose file maps host **1434**. Set `MSSQL_PORT=1434`. |
| Tools missing after config edit | Restart the client; MCP servers are launched at startup. |
| `Project virtual environment directory ... is not a valid Python environment` | You have `UV_PROJECT_ENVIRONMENT` exported globally. See below. |

Isolate MCP problems from database problems with `make check-connection` — it uses the exact
same configuration path as the server.

### `UV_PROJECT_ENVIRONMENT` set globally

If a raw `uv run ...` fails with:

```text
error: Project virtual environment directory `/some/path` cannot be used because it is not a
valid Python environment (no Python executable was found)
```

your shell exports `UV_PROJECT_ENVIRONMENT` to a fixed path. uv expects that to be the
virtualenv *itself*, not a folder to keep venvs in — and because it is one path, every project
on the machine would share a single environment anyway, so `uv sync` in one project overwrites
another's dependencies.

The `make` targets are immune: the Makefile pins the value to the in-project `.venv`. For a raw
`uv` command, override it per-invocation:

```bash
UV_PROJECT_ENVIRONMENT=.venv uv run python scripts/check_connection.py
```

To fix it permanently, remove the export from your shell profile (`~/.zshrc`, `~/.bashrc`); uv
then defaults to a `.venv` inside each project, which is already gitignored here.
