"""Tests for the database layer: read/write semantics, cell coercion, sanitization."""

import datetime
import decimal
import uuid

import mssql_python
import pytest

from mssql_mcp_server import db
from mssql_mcp_server.db import NotAReadQuery, jsonable, run_read, run_write, sanitize_error


def driver_error(cls, message):
    """mssql_python exceptions take (driver_error, ddbc_error) -- two args."""
    return cls(message, "")


# --------------------------------------------------------------------------
# Read path
# --------------------------------------------------------------------------


async def test_read_returns_columns_and_rows(env, fake_db):
    fake_db(columns=["id", "name"], rows=[[1, "a"], [2, "b"]])
    result = await run_read("SELECT id, name FROM t", max_rows=10)
    assert result.columns == ["id", "name"]
    assert result.rows == [[1, "a"], [2, "b"]]
    assert result.row_count == 2
    assert result.truncated is False


async def test_read_always_rolls_back_and_never_commits(env, fake_db):
    """The core safety guarantee: a read cannot persist anything."""
    fake_db(columns=["a"], rows=[[1]])
    await run_read("SELECT a FROM t", max_rows=10)
    conn = fake_db.holder["last"]
    assert conn.rollbacks == 1
    assert conn.commits == 0


async def test_read_closes_the_connection(env, fake_db):
    fake_db(columns=["a"], rows=[[1]])
    await run_read("SELECT a FROM t", max_rows=10)
    assert fake_db.holder["last"].closed is True


async def test_read_uses_fetchmany_and_never_fetchall(env, fake_db):
    """fetchall() on a large table is how you exhaust memory."""
    fake_db(columns=["a"], rows=[[i] for i in range(10)])
    await run_read("SELECT a FROM t", max_rows=5)
    conn = fake_db.holder["last"]
    assert conn.fetchall_calls == 0
    assert conn.fetch_sizes == [6]  # max_rows + 1, to detect truncation


async def test_read_truncates_and_reports_it(env, fake_db):
    fake_db(columns=["a"], rows=[[i] for i in range(100)])
    result = await run_read("SELECT a FROM t", max_rows=5)
    assert result.truncated is True
    assert result.row_count == 5
    assert len(result.rows) == 5
    assert result.max_rows == 5


async def test_read_exactly_at_the_cap_is_not_truncated(env, fake_db):
    fake_db(columns=["a"], rows=[[i] for i in range(5)])
    result = await run_read("SELECT a FROM t", max_rows=5)
    assert result.truncated is False
    assert result.row_count == 5


async def test_read_of_an_empty_table(env, fake_db):
    fake_db(columns=["a"], rows=[])
    result = await run_read("SELECT a FROM t", max_rows=10)
    assert result.rows == []
    assert result.row_count == 0
    assert result.columns == ["a"]


async def test_read_passes_parameters_through(env, fake_db):
    fake_db(columns=["a"], rows=[[1]])
    await run_read("SELECT a FROM t WHERE b = ?", max_rows=10, params=("x",))
    assert fake_db.holder["last"].executed[0] == ("SELECT a FROM t WHERE b = ?", ("x",))


# --------------------------------------------------------------------------
# The tripwire
# --------------------------------------------------------------------------


async def test_no_result_set_on_the_read_path_trips_the_wire(env, fake_db, caplog):
    """If the classifier is ever fooled, this is the loud failure that catches it."""
    fake_db(columns=None, affected=3)  # a DML statement produces no description
    with pytest.raises(NotAReadQuery):
        await run_read("DELETE FROM t", max_rows=10)
    assert "Tripwire" in caplog.text


async def test_tripwire_rolls_back(env, fake_db):
    fake_db(columns=None, affected=3)
    with pytest.raises(NotAReadQuery):
        await run_read("DELETE FROM t", max_rows=10)
    conn = fake_db.holder["last"]
    assert conn.rollbacks == 1
    assert conn.commits == 0


# --------------------------------------------------------------------------
# Write path
# --------------------------------------------------------------------------


async def test_write_commits_and_reports_rows_affected(env, fake_db):
    fake_db(columns=None, affected=7)
    assert await run_write("DELETE FROM t") == 7
    conn = fake_db.holder["last"]
    assert conn.commits == 1
    assert conn.autocommit is False


async def test_write_maps_negative_rowcount_to_none(env, fake_db):
    """The driver reports -1 for 'no row count', which is not 'zero rows'."""
    fake_db(columns=None, affected=-1)
    assert await run_write("CREATE TABLE t (a INT)") is None


async def test_write_in_autocommit_mode_does_not_commit_explicitly(env, fake_db):
    """CREATE DATABASE and friends cannot run inside an explicit transaction."""
    fake_db(columns=None, affected=-1)
    await run_write("CREATE DATABASE d", autocommit=True)
    conn = fake_db.holder["last"]
    assert conn.autocommit is True
    assert conn.commits == 0


async def test_write_does_not_commit_when_execute_fails(env, fake_db):
    fake_db(
        columns=None,
        execute_error=driver_error(mssql_python.ProgrammingError, "syntax error"),
    )
    with pytest.raises(mssql_python.ProgrammingError):
        await run_write("DELETE FROM")
    conn = fake_db.holder["last"]
    assert conn.commits == 0
    assert conn.closed is True  # closing without a commit is an implicit rollback


# --------------------------------------------------------------------------
# Connection lifecycle
# --------------------------------------------------------------------------


async def test_each_call_gets_its_own_connection(env, fake_db):
    fake_db(columns=["a"], rows=[[1]])
    for _ in range(3):
        await run_read("SELECT a FROM t", max_rows=10)
    assert fake_db.holder["calls"] == 3


async def test_a_failed_call_does_not_wedge_later_calls(env, fake_db):
    """The regression test for per-request connections vs. a shared singleton."""
    fake_db(columns=None, execute_error=driver_error(mssql_python.ProgrammingError, "boom"))
    with pytest.raises(mssql_python.ProgrammingError):
        await run_read("SELECT bad", max_rows=10)

    fake_db(columns=["a"], rows=[[1]])
    result = await run_read("SELECT a FROM t", max_rows=10)
    assert result.rows == [[1]]


async def test_read_runs_off_the_event_loop(env, fake_db, monkeypatch):
    """A blocking C extension on the event loop would deadlock elicitation."""
    import threading

    seen: dict[str, int] = {}
    original = db._read_sync

    def spy(*args, **kwargs):
        seen["thread"] = threading.get_ident()
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "_read_sync", spy)
    fake_db(columns=["a"], rows=[[1]])
    await run_read("SELECT a FROM t", max_rows=10)
    assert seen["thread"] != threading.get_ident()


# --------------------------------------------------------------------------
# Cell coercion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (42, 42),
        (3.5, 3.5),
        ("text", "text"),
        (decimal.Decimal("10.50"), "10.50"),
        (datetime.date(2026, 7, 25), "2026-07-25"),
        (datetime.datetime(2026, 7, 25, 12, 30), "2026-07-25T12:30:00"),
        (datetime.time(12, 30), "12:30:00"),
        (b"\xff\xfe", "fffe"),
        (uuid.UUID("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
    ],
)
def test_jsonable_coercion(value, expected):
    assert jsonable(value) == expected


def test_decimal_keeps_full_precision():
    """Going via float would silently corrupt money columns."""
    assert jsonable(decimal.Decimal("0.1234567890123456789")) == "0.1234567890123456789"


def test_non_utf8_bytes_do_not_raise():
    assert jsonable(b"\x80\x81") == "8081"


async def test_rows_are_coerced_end_to_end(env, fake_db):
    fake_db(columns=["when", "amount"], rows=[[datetime.date(2026, 1, 1), decimal.Decimal("1.5")]])
    result = await run_read("SELECT * FROM t", max_rows=10)
    assert result.rows == [["2026-01-01", "1.5"]]


# --------------------------------------------------------------------------
# Error sanitization
# --------------------------------------------------------------------------


def test_connect_errors_never_leak_their_text(env):
    """These embed the connection string, and therefore the password."""
    exc = driver_error(
        mssql_python.OperationalError,
        "Login failed for user 'test_user' with password 'secret123'",
    )
    message = sanitize_error(exc)
    assert "secret123" not in message
    assert "test_user" not in message
    assert "Could not connect" in message


def test_connect_error_message_names_the_fix_for_local_dev(env):
    message = sanitize_error(driver_error(mssql_python.InterfaceError, "SSL error"))
    assert "MSSQL_TRUST_SERVER_CERTIFICATE" in message


def test_query_errors_pass_through_so_the_model_can_self_correct(env):
    exc = driver_error(mssql_python.ProgrammingError, "Invalid column name 'foo'")
    assert "Invalid column name 'foo'" in sanitize_error(exc)


def test_query_errors_are_still_scrubbed(env):
    exc = driver_error(mssql_python.ProgrammingError, "failed with password 'secret123'")
    message = sanitize_error(exc)
    assert "secret123" not in message
    assert "***" in message


def test_pwd_keyword_is_scrubbed_even_without_settings(env):
    exc = driver_error(mssql_python.ProgrammingError, "conn: Server=h;PWD=hunter2;Database=d")
    assert "hunter2" not in sanitize_error(exc)


def test_connection_string_parse_error_is_reported_as_configuration(env):
    """It is not a subclass of Error, so it needs its own branch."""
    assert not issubclass(mssql_python.ConnectionStringParseError, mssql_python.Error)
    message = sanitize_error(mssql_python.ConnectionStringParseError(["bad key"]))
    assert "connection settings are invalid" in message


def test_unknown_exceptions_get_a_generic_message(env):
    assert sanitize_error(RuntimeError("internal detail")) == "The database operation failed."


def test_tripwire_message_reaches_the_client(env):
    message = sanitize_error(NotAReadQuery("did not return a result set"))
    assert "did not return a result set" in message


def test_sanitize_works_when_configuration_itself_is_broken(monkeypatch):
    """A config failure must not turn into a crash inside the error handler."""
    assert sanitize_error(RuntimeError("x")) == "The database operation failed."
