"""Shared test fixtures.

Deliberately imports no database driver at module scope, so that collection of
the suite never depends on the driver being importable.

The fakes below are hand-written rather than ``unittest.mock.Mock`` on purpose.
A ``Mock`` auto-creates ``rowcount`` and ``description`` as truthy Mock objects,
which is precisely how the previous suite came to have thirty-odd assertions
that could never fail. These reproduce the driver's real, awkward semantics:
``rowcount == -1`` for SELECT, ``description is None`` when there is no result
set, and ``fetchmany(n)`` actually honouring ``n``.
"""

from __future__ import annotations

from typing import Any

import pytest

BASE_ENV = {
    "MSSQL_SERVER": "localhost",
    "MSSQL_DATABASE": "testdb",
    "MSSQL_USER": "test_user",
    "MSSQL_PASSWORD": "secret123",
}


class FakeCursor:
    """A cursor that behaves like mssql_python's."""

    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self.description: list[tuple] | None = None
        self.rowcount: int = -1
        self.closed = False
        self._pending: list[list[Any]] = []

    def execute(self, sql: str, *params: Any) -> FakeCursor:
        self._connection.executed.append((sql, params))

        if self._connection.execute_error is not None:
            raise self._connection.execute_error

        if self._connection.columns is None:
            # No result set: exactly what a DML or DDL statement produces.
            self.description = None
            self.rowcount = self._connection.affected
        else:
            self.description = [(name,) + (None,) * 6 for name in self._connection.columns]
            self._pending = [list(row) for row in self._connection.rows]
            self.rowcount = -1  # the driver reports -1 for SELECT, by design
        return self

    def fetchmany(self, size: int) -> list[list[Any]]:
        self._connection.fetch_sizes.append(size)
        taken, self._pending = self._pending[:size], self._pending[size:]
        return taken

    def fetchall(self) -> list[list[Any]]:
        self._connection.fetchall_calls += 1
        taken, self._pending = self._pending, []
        return taken

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeConnection:
    """A connection that records what was asked of it."""

    def __init__(
        self,
        *,
        columns: list[str] | None = None,
        rows: list[list[Any]] | None = None,
        affected: int = 0,
        connect_error: BaseException | None = None,
        execute_error: BaseException | None = None,
        autocommit: bool = False,
    ) -> None:
        if connect_error is not None:
            raise connect_error
        self.columns = columns
        self.rows = rows or []
        self.affected = affected
        self.execute_error = execute_error
        self.autocommit = autocommit

        self.executed: list[tuple[str, tuple]] = []
        self.fetch_sizes: list[int] = []
        self.fetchall_calls = 0
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def sql(self) -> list[str]:
        return [sql for sql, _ in self.executed]


@pytest.fixture(autouse=True)
def clean_env(request, monkeypatch):
    """Clear MSSQL_* and the settings cache so tests cannot leak into each other.

    Tests marked ``live`` are exempt: they need the real MSSQL_* configuration
    pointing at an actual server.
    """
    import os

    from mssql_mcp_server.config import get_settings

    if request.node.get_closest_marker("live") is None:
        for key in [k for k in os.environ if k.startswith("MSSQL_")]:
            monkeypatch.delenv(key, raising=False)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch):
    """Set a working configuration; call with kwargs to override."""

    def _apply(**overrides: str | None):
        from mssql_mcp_server.config import get_settings

        for key, value in {**BASE_ENV, **overrides}.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    _apply()
    return _apply


@pytest.fixture
def fake_db(monkeypatch):
    """Patch our own connection seam and return a factory for the fake.

    Patching ``db._connect`` rather than ``mssql_python.connect`` keeps the
    tests pinned to a one-line signature under our control.
    """
    from mssql_mcp_server import db

    holder: dict[str, Any] = {}

    def _install(**kwargs: Any) -> FakeConnection | None:
        connect_error = kwargs.pop("connect_error", None)

        def _connect(*, autocommit: bool = False) -> FakeConnection:
            if connect_error is not None:
                raise connect_error
            conn = FakeConnection(autocommit=autocommit, **kwargs)
            holder["last"] = conn
            holder.setdefault("all", []).append(conn)
            holder["calls"] = holder.get("calls", 0) + 1
            return conn

        monkeypatch.setattr(db, "_connect", _connect)
        return holder.get("last")

    _install.holder = holder  # type: ignore[attr-defined]
    return _install


@pytest.fixture
def no_db(monkeypatch):
    """Make any connection attempt an immediate test failure.

    Used to assert that a rejected write never touches the database.
    """
    from mssql_mcp_server import db

    calls: list[bool] = []

    def _connect(*, autocommit: bool = False):
        calls.append(True)
        raise AssertionError("_connect() was called, but no connection was expected")

    monkeypatch.setattr(db, "_connect", _connect)
    return calls
