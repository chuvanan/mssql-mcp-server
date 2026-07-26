# Security Policy

## Reporting security issues

If you discover a security vulnerability, please email security@example.com rather than using
the public issue tracker.

## The security model, stated plainly

This server exposes a SQL database to a language model. Several mechanisms limit what that model
can do, and they are not equally strong.

### What actually protects you: database permissions

**A dedicated, least-privilege SQL login is the real control.** Everything below is defense in
depth layered on top of it. If the model must only read, give it a login that can only read —
then a classifier bug is a failed query rather than lost data.

```sql
CREATE LOGIN mcp_user WITH PASSWORD = 'UseAStrongPasswordHere';
CREATE USER mcp_user FOR LOGIN mcp_user;

-- Read-only: pair with MSSQL_APPROVAL_MODE=readonly
ALTER ROLE db_datareader ADD MEMBER mcp_user;

-- Or grant writes on specific objects only
GRANT SELECT ON Schema.TableName TO mcp_user;
GRANT INSERT, UPDATE ON Schema.AuditLog TO mcp_user;
```

Never use `sa` or any account in `sysadmin`. Prefer Windows Authentication where available.

### The approval checkpoint

Every statement classified as a write requires explicit user approval before it runs. The user
is shown the exact SQL, the server and the database. No connection is opened until they approve.

The checkpoint **fails closed**: if the MCP client cannot display prompts, writes are refused
rather than executed. `MSSQL_APPROVAL_MODE=allow` disables this entirely — it exists for
headless use and should be set deliberately, never to make an error message go away.

### The statement classifier is a guardrail, not a boundary

`read_query` accepts a statement only if a T-SQL lexer confirms it is a single `SELECT` with no
`INTO`, no `EXEC`, and no DML or DDL keyword at the top level. Comments and string literals are
tokenized rather than pattern-matched, so `SELECT '--'`, `SELECT ';'` and `SELECT [drop--table]`
are handled correctly. Anything that fails to lex is treated as a write.

Two further defenses back it up:

1. **The read path always rolls back.** It connects with `autocommit=False` and rolls back
   unconditionally. SQL Server makes DDL transactional, so a misclassified write is undone.
2. **A tripwire.** If a statement on the read path produces no result set, it was not a SELECT.
   The transaction is rolled back and the event is logged at ERROR.

### Known residual risks

These are real. Database permissions are the mitigation, not the classifier.

- **Linked servers and passthrough**: `OPENQUERY`, `OPENROWSET` and `OPENDATASOURCE` can carry a
  write inside a string this server deliberately never parses. They are rejected at the top
  level, but that is a keyword check, not a structural guarantee.
- **Procedures with side effects**: a scalar function or CLR procedure invoked from inside a
  `SELECT` can do anything the login permits.
- **Effects rollback cannot undo**: `xp_cmdshell`, `sp_send_dbmail` and filesystem writes are not
  transactional.
- **Prompt truncation**: statements longer than 4000 characters are truncated in the approval
  prompt. The truncation is always labelled, but a user approving a very long batch is not
  seeing all of it.

## Credential handling

- The password is excluded from `repr()` and from every log line; only a redacted summary is
  logged.
- Connection-level driver errors — which embed the connection string — are replaced with a
  generic message before reaching the client. Query-level errors are passed through, scrubbed,
  because they help the model correct its own SQL.
- Connection parameters are passed to the driver as a dict, so the driver escapes values itself.
  Passwords containing `;` or `=` are handled correctly.
- Encryption is on by default and certificates are verified. For Azure SQL this is forced.

## Identifier and input handling

Table names reaching the resource template and `describe_table` are validated against a strict
allowlist (`[A-Za-z0-9_]`, optionally one `.`) and then bracketed. `list_tables` and
`describe_table` bind their arguments as query parameters rather than interpolating them.

Arbitrary SQL submitted to `read_query` and `execute_write` is **not** parameterized — it cannot
be, since the statement itself is the input. That is precisely what the classifier, the approval
checkpoint and database permissions are for.
