"""Database access: connections, query execution, and error sanitization.

Two things here are load-bearing and easy to get wrong:

1. **Everything blocking runs in a worker thread.** ``mssql_python`` is a C
   extension; calling it directly from the event loop would freeze the whole
   server for the duration of a query -- including the elicitation round-trip,
   which would deadlock the write-approval checkpoint.

2. **The read path never commits.** It connects with ``autocommit=False`` and
   always rolls back. SQL Server makes DDL transactional, so if the classifier
   in ``sql.py`` is ever fooled, the write is undone rather than persisted.
"""

from __future__ import annotations

import datetime
import decimal
import logging
import re
import uuid
from dataclasses import dataclass, field
from functools import partial
from typing import Any, cast

import anyio
import mssql_python

from mssql_mcp_server.config import Settings, get_settings

logger = logging.getLogger("mssql_mcp_server.db")

__all__ = [
    "DatabaseError",
    "NotAReadQuery",
    "ReadResult",
    "jsonable",
    "run_read",
    "run_write",
    "sanitize_error",
]

# Connection-level failures embed the connection string, and therefore the
# password. Their text must never reach the client.
_CONNECT_LEVEL_ERRORS = (mssql_python.InterfaceError, mssql_python.OperationalError)

# Query-level failures carry syntax and constraint detail that genuinely helps
# the model correct itself, so they are passed through -- after scrubbing.
_QUERY_LEVEL_ERRORS = (
    mssql_python.ProgrammingError,
    mssql_python.DataError,
    mssql_python.IntegrityError,
)

_GENERIC_CONNECT_MESSAGE = (
    "Could not connect to the database. Check the MSSQL_* environment variables "
    "(server, database, credentials). If this is a local development server with a "
    "self-signed certificate, set MSSQL_TRUST_SERVER_CERTIFICATE=true."
)
_GENERIC_MESSAGE = "The database operation failed."

_PWD_RE = re.compile(r"(?i)\bpwd\s*=\s*[^;]*")
_PASSWORD_PHRASE_RE = re.compile(r"(?i)password\s+'[^']*'")


class DatabaseError(RuntimeError):
    """A database failure whose message is safe to show the client."""


class NotAReadQuery(RuntimeError):
    """A statement that reached the read path produced no result set.

    This is the tripwire described in ``sql.py``: it means the classifier let
    something through that was not a SELECT. The transaction is rolled back
    and the event is logged at ERROR.
    """


@dataclass(frozen=True, slots=True)
class ReadResult:
    """The outcome of a read query.

    ``columns`` + ``rows`` rather than a list of dicts: repeating every column
    name on every row roughly triples the token cost of a wide result.
    """

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    max_rows: int = 0


def jsonable(value: Any) -> Any:
    """Coerce a database cell into something JSON-serializable.

    Rows arrive containing ``datetime``, ``Decimal``, ``bytes`` and ``UUID``.
    Pydantic rejects bytes that are not valid UTF-8, so coerce before the
    value ever reaches the serializer.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        # str, not float: converting through float loses precision, which for
        # money columns is the one thing you must not do.
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _scrub(text: str, settings: Settings | None) -> str:
    """Remove credentials from a message before it crosses the MCP boundary."""
    if settings is not None:
        for secret in (settings.password, settings.user):
            # The length floor avoids turning every 'sa' substring into '***'.
            if secret and len(secret) >= 4:
                text = text.replace(secret, "***")
    text = _PWD_RE.sub("PWD=***", text)
    return _PASSWORD_PHRASE_RE.sub("password '***'", text)


def sanitize_error(exc: BaseException) -> str:
    """Turn a driver exception into a message that is safe to return.

    Connection-level errors are replaced wholesale; query-level errors are
    passed through so the model can fix its own SQL. The full exception,
    including anything redacted here, still goes to the stderr log.
    """
    try:
        settings: Settings | None = get_settings()
    except Exception:  # configuration itself may be what failed
        settings = None

    if isinstance(exc, mssql_python.ConnectionStringParseError):
        return (
            "The database connection settings are invalid. Check the MSSQL_* environment variables."
        )
    if isinstance(exc, _CONNECT_LEVEL_ERRORS):
        return _GENERIC_CONNECT_MESSAGE
    if isinstance(exc, _QUERY_LEVEL_ERRORS):
        return _scrub(str(exc), settings)
    if isinstance(exc, (NotAReadQuery, DatabaseError)):
        return _scrub(str(exc), settings)
    return _GENERIC_MESSAGE


def _connect(*, autocommit: bool = False) -> Any:
    """Open a connection. One per request; the driver pools underneath."""
    settings = get_settings()
    # cast: the driver's signature declares typed optional keywords alongside
    # **kwargs, so a dict of connection keywords cannot be checked statically.
    connect = cast(Any, mssql_python.connect)
    return connect(
        autocommit=autocommit,
        timeout=settings.connect_timeout,
        **settings.odbc_params(),
    )


def _read_sync(sql: str, params: tuple[Any, ...], max_rows: int) -> ReadResult:
    # __exit__ closes the connection, and closing without committing performs
    # an implicit rollback -- so the guarantee holds on the exception path too.
    with _connect(autocommit=False) as conn:
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(sql, *params)
            else:
                cursor.execute(sql)

            if cursor.description is None:
                logger.error(
                    "Tripwire: a statement reaching the read path produced no result set. "
                    "This should be impossible; treat it as a classifier bug. SQL: %s",
                    sql,
                )
                conn.rollback()
                raise NotAReadQuery(
                    "That statement did not return a result set, so it was not a read. "
                    "It has been rolled back. Use execute_write for statements that "
                    "modify data."
                )

            columns = [d[0] for d in cursor.description]
            # One extra row is how we detect truncation without fetchall().
            fetched = cursor.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            if truncated:
                fetched = fetched[:max_rows]

            rows = [[jsonable(cell) for cell in row] for row in fetched]
            conn.rollback()
            return ReadResult(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                max_rows=max_rows,
            )
        finally:
            cursor.close()


def _write_sync(sql: str, autocommit: bool) -> int | None:
    with _connect(autocommit=autocommit) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            rowcount = cursor.rowcount
            if not autocommit:
                conn.commit()
            # The driver reports -1 for statements with no row count, which is
            # not the same as "zero rows changed".
            return None if rowcount is None or rowcount < 0 else rowcount
        finally:
            cursor.close()


async def run_read(sql: str, max_rows: int, params: tuple[Any, ...] = ()) -> ReadResult:
    """Execute a read query in a worker thread."""
    return await anyio.to_thread.run_sync(partial(_read_sync, sql, params, max_rows))


async def run_write(sql: str, *, autocommit: bool = False) -> int | None:
    """Execute a write statement in a worker thread. Returns rows affected."""
    return await anyio.to_thread.run_sync(partial(_write_sync, sql, autocommit))
