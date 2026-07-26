"""Tests for the statement classifier.

Pure: no mocks, no env, no async, no database. Every case here is a claim
about what reaches the server, so the corpus is deliberately adversarial.
"""

import pytest

from mssql_mcp_server.sql import (
    SqlLexError,
    StatementKind,
    classify,
    is_read_only,
    needs_autocommit,
    parse,
    split_statements,
    validate_table_name,
)

# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

READ_ONLY = [
    "SELECT 1",
    "select 1",
    "  \t\n SELECT 1  ",
    "SELECT * FROM users",
    "SELECT * FROM dbo.users WHERE id = 5",
    "SELECT TOP 10 * FROM users ORDER BY id",
    "SELECT 1;",
    "SELECT 1 ;  ",
    # Comments before the statement
    "-- a comment\nSELECT 1",
    "/* a comment */ SELECT 1",
    "/* multi\nline */\nSELECT 1",
    "-- one\n-- two\nSELECT 1",
    "SELECT 1 -- trailing",
    "SELECT 1 /* trailing */",
    # T-SQL nests block comments; a non-greedy regex would stop at the first */
    "/* outer /* inner */ still outer */ SELECT 1",
    # CTEs feeding a SELECT
    "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
    "with x as (select 1) select * from x",
    "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a JOIN b ON 1=1",
    # Comment- and separator-shaped string literals
    "SELECT '--not a comment'",
    "SELECT ';'",
    "SELECT '/*'",
    "SELECT N'/*'",
    "SELECT 'DROP TABLE users'",
    "SELECT 'it''s fine'",
    "SELECT 'a;b'",
    # Quoted and bracketed identifiers containing scary text
    "SELECT [drop--table] FROM t",
    'SELECT "a--b" FROM t',
    "SELECT [a;b] FROM t",
    "SELECT [weird]]name] FROM t",
    # Subqueries and functions
    "SELECT (SELECT COUNT(*) FROM b) FROM a",
    "SELECT * FROM t WHERE id IN (SELECT id FROM u)",
    "SELECT * FROM t WITH (NOLOCK)",
    "SELECT * FROM t ORDER BY x OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY",
    "SELECT a, b FROM t GROUP BY a, b HAVING COUNT(*) > 1",
    "SELECT * FROM a UNION SELECT * FROM b",
    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'",
    "﻿SELECT 1",  # BOM
]


@pytest.mark.parametrize("sql", READ_ONLY)
def test_read_only_accepted(sql):
    assert is_read_only(sql) is True


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

WRITES = [
    # Plain DML
    "INSERT INTO t VALUES (1)",
    "insert into t values (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "DELETE FROM t WHERE id = 1",
    "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.a = s.a",
    # DDL
    "DROP TABLE t",
    "DROP DATABASE d",
    "ALTER TABLE t ADD c INT",
    "CREATE TABLE t (a INT)",
    "TRUNCATE TABLE t",
    "CREATE INDEX ix ON t (a)",
    # CTEs feeding a write -- the DELETE sits at depth 0, the CTE body does not
    "WITH x AS (SELECT 1) DELETE FROM t",
    "WITH x AS (SELECT id FROM u) UPDATE t SET a = 1 FROM x",
    "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "WITH x AS (SELECT 1) MERGE t USING x ON 1=1 WHEN MATCHED THEN DELETE",
    # SELECT ... INTO creates and populates a table
    "SELECT * INTO t2 FROM t1",
    "SELECT a INTO #tmp FROM t",
    "select * into t2 from t1",
    # Procedures: contents unknowable, so always a write
    "EXEC sp_who",
    "EXECUTE sp_who",
    "EXEC sp_executesql N'DROP TABLE t'",
    "exec dbo.my_proc @a = 1",
    # Permissions, admin, transactions
    "GRANT SELECT ON t TO public",
    "REVOKE SELECT ON t FROM public",
    "DENY SELECT ON t TO public",
    "BACKUP DATABASE d TO DISK = 'x'",
    "RESTORE DATABASE d FROM DISK = 'x'",
    "DBCC CHECKDB",
    "KILL 52",
    "SHUTDOWN",
    "USE otherdb",
    "SET ANSI_NULLS ON",
    "DECLARE @x INT",
    "BEGIN TRAN",
    "COMMIT",
    "ROLLBACK",
    "WAITFOR DELAY '00:00:05'",
    # Passthrough surfaces that can smuggle a write inside an opaque literal
    "SELECT * FROM OPENQUERY(linked, 'DELETE FROM t')",
    "SELECT * FROM OPENROWSET(BULK 'f.txt', SINGLE_CLOB) AS x",
    # Multi-statement batches: the second statement is the problem
    "SELECT 1; DROP TABLE t",
    "SELECT 1; SELECT 2",
    "DROP TABLE t; SELECT 1",
    # Comments cannot smuggle a write past the classifier, but nor can they
    # smuggle a read past it
    "-- SELECT 1\nDROP TABLE t",
    "/* SELECT 1 */ DROP TABLE t",
]


@pytest.mark.parametrize("sql", WRITES)
def test_writes_rejected(sql):
    assert is_read_only(sql) is False


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------

MALFORMED = [
    "SELECT 'unterminated",
    "SELECT * FROM [unterminated",
    'SELECT * FROM "unterminated',
    "/* unterminated SELECT 1",
    "/* outer /* inner */ SELECT 1",  # still one comment level open
]


@pytest.mark.parametrize("sql", MALFORMED)
def test_malformed_is_not_read_only(sql):
    """Anything we cannot lex is a write. Failing closed is the point."""
    assert is_read_only(sql) is False


@pytest.mark.parametrize("sql", MALFORMED)
def test_malformed_classifies_as_unknown(sql):
    assert classify(sql) == [StatementKind.UNKNOWN]


@pytest.mark.parametrize("sql", MALFORMED)
def test_malformed_raises_from_parse(sql):
    with pytest.raises(SqlLexError):
        parse(sql)


@pytest.mark.parametrize("sql", ["", "   ", "-- just a comment", "/* just a comment */", ";"])
def test_empty_input_is_not_read_only(sql):
    assert is_read_only(sql) is False


def test_empty_input_classifies_as_unknown():
    assert classify("") == [StatementKind.UNKNOWN]


# --------------------------------------------------------------------------
# GO batch separator
# --------------------------------------------------------------------------


def test_go_is_rejected_with_a_useful_message():
    with pytest.raises(SqlLexError, match="sqlcmd batch separator"):
        parse("SELECT 1\nGO\nSELECT 2")


def test_go_inside_a_literal_is_not_a_batch_separator():
    assert is_read_only("SELECT 'GO'") is True


def test_go_as_an_identifier_prefix_is_fine():
    assert is_read_only("SELECT gold FROM t") is True


# --------------------------------------------------------------------------
# Statement splitting
# --------------------------------------------------------------------------


def test_split_ignores_semicolons_inside_literals():
    assert split_statements("SELECT 'a;b'") == ["SELECT 'a;b'"]


def test_split_ignores_semicolons_inside_brackets():
    assert split_statements("SELECT [a;b] FROM t") == ["SELECT [a;b] FROM t"]


def test_split_separates_top_level_statements():
    assert split_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]


def test_split_drops_trailing_semicolon():
    assert split_statements("SELECT 1;") == ["SELECT 1"]


def test_split_drops_empty_statements():
    assert split_statements("SELECT 1;;; SELECT 2") == ["SELECT 1", "SELECT 2"]


def test_split_preserves_source_text():
    sql = "UPDATE t SET a = 1 WHERE b = 'x;y'; DELETE FROM u"
    assert split_statements(sql) == ["UPDATE t SET a = 1 WHERE b = 'x;y'", "DELETE FROM u"]


# --------------------------------------------------------------------------
# Classification detail (drives the approval message)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", StatementKind.SELECT),
        ("WITH x AS (SELECT 1) SELECT * FROM x", StatementKind.SELECT),
        ("INSERT INTO t VALUES (1)", StatementKind.INSERT),
        ("SELECT * INTO t2 FROM t1", StatementKind.INSERT),
        ("WITH x AS (SELECT 1) DELETE FROM t", StatementKind.DELETE),
        ("UPDATE t SET a = 1", StatementKind.UPDATE),
        ("DELETE FROM t", StatementKind.DELETE),
        ("MERGE t USING s ON 1=1 WHEN MATCHED THEN DELETE", StatementKind.MERGE),
        ("DROP TABLE t", StatementKind.DDL),
        ("CREATE TABLE t (a INT)", StatementKind.DDL),
        ("ALTER TABLE t ADD c INT", StatementKind.DDL),
        ("TRUNCATE TABLE t", StatementKind.DDL),
        ("EXEC sp_who", StatementKind.EXEC),
        ("GRANT SELECT ON t TO public", StatementKind.PERMISSION),
        ("BEGIN TRAN", StatementKind.TRANSACTION),
        ("USE otherdb", StatementKind.OTHER),
    ],
)
def test_classify_single_statement(sql, expected):
    assert classify(sql) == [expected]


def test_classify_batch():
    assert classify("SELECT 1; DELETE FROM t; DROP TABLE u") == [
        StatementKind.SELECT,
        StatementKind.DELETE,
        StatementKind.DDL,
    ]


def test_statement_kind_stringifies_to_its_name():
    assert f"{StatementKind.DELETE}" == "DELETE"
    assert ", ".join(classify("DELETE FROM t; DROP TABLE u")) == "DELETE, DDL"


# --------------------------------------------------------------------------
# Autocommit-only statements
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE DATABASE d",
        "ALTER DATABASE d SET RECOVERY SIMPLE",
        "DROP DATABASE d",
        "BACKUP DATABASE d TO DISK = 'x'",
        "RESTORE DATABASE d FROM DISK = 'x'",
        "RECONFIGURE",
    ],
)
def test_statements_that_cannot_run_in_a_transaction(sql):
    assert needs_autocommit(sql) is True


@pytest.mark.parametrize(
    "sql",
    ["CREATE TABLE t (a INT)", "DELETE FROM t", "ALTER TABLE t ADD c INT", "SELECT 1"],
)
def test_ordinary_statements_run_in_a_transaction(sql):
    assert needs_autocommit(sql) is False


# --------------------------------------------------------------------------
# Table name validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("users", "[users]"),
        ("dbo.users", "[dbo].[users]"),
        ("Table_1", "[Table_1]"),
        ("a1.b2", "[a1].[b2]"),
    ],
)
def test_validate_table_name_accepts_and_brackets(name, expected):
    assert validate_table_name(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "users; DROP TABLE users",
        "users--",
        "users]",
        "[users]",
        "users'",
        "a.b.c",
        "",
        " users",
        "users ",
        "us ers",
        "*",
        "users/*",
        "dbo.users; SELECT 1",
    ],
)
def test_validate_table_name_rejects(name):
    with pytest.raises(ValueError, match="Invalid table name"):
        validate_table_name(name)
