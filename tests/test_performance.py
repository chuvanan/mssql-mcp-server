"""Performance and resource-usage contracts.

Deliberately deterministic. The previous version of this file asserted things
like `elapsed < 5.0`, which measures the CI runner rather than the code. Each
test here pins a specific behaviour that has a real cost if it regresses.
"""

import asyncio
import threading
import time

import anyio
import pytest
from fastmcp import Client

from mssql_mcp_server import db
from mssql_mcp_server.db import run_read
from mssql_mcp_server.server import mcp

# --------------------------------------------------------------------------
# Bounded memory
# --------------------------------------------------------------------------


async def test_reads_never_call_fetchall(env, fake_db):
    """fetchall() on a large table pulls the whole result set into memory."""
    fake_db(columns=["a"], rows=[[i] for i in range(10_000)])
    await run_read("SELECT a FROM t", max_rows=100)
    assert fake_db.holder["last"].fetchall_calls == 0


async def test_reads_fetch_exactly_one_row_past_the_cap(env, fake_db):
    """One extra row is all that is needed to detect truncation."""
    fake_db(columns=["a"], rows=[[i] for i in range(10_000)])
    await run_read("SELECT a FROM t", max_rows=250)
    assert fake_db.holder["last"].fetch_sizes == [251]


async def test_a_huge_table_produces_a_bounded_result(env, fake_db):
    fake_db(columns=["a", "b"], rows=[[i, "x" * 100] for i in range(50_000)])
    result = await run_read("SELECT a, b FROM t", max_rows=100)
    assert result.row_count == 100
    assert result.truncated is True


async def test_the_row_cap_is_enforced_through_the_tool(env, fake_db):
    env(MSSQL_MAX_ROWS="25")
    fake_db(columns=["a"], rows=[[i] for i in range(100_000)])
    async with Client(mcp) as c:
        result = await c.call_tool("read_query", {"sql": "SELECT a FROM t"})
    assert result.data.row_count == 25
    assert result.data.truncated is True


async def test_truncation_warns_the_caller(env, fake_db):
    """Silent truncation would let the model reason about partial data."""
    fake_db(columns=["a"], rows=[[i] for i in range(100)])
    messages = []

    async def log_handler(message):
        messages.append(message.data)

    async with Client(mcp, log_handler=log_handler) as c:
        await c.call_tool("read_query", {"sql": "SELECT a FROM t", "max_rows": 5})

    assert any("truncated" in str(m).lower() for m in messages)


# --------------------------------------------------------------------------
# The event loop must stay free
# --------------------------------------------------------------------------


async def test_database_work_runs_off_the_event_loop(env, fake_db, monkeypatch):
    """The highest-value test in this file.

    mssql_python is a blocking C extension. If a query ran on the event loop,
    it would block the elicitation round-trip -- deadlocking the approval
    checkpoint, which is the one thing that must never hang.
    """
    loop_thread = threading.get_ident()
    observed = {}
    original = db._read_sync

    def spy(*args, **kwargs):
        observed["thread"] = threading.get_ident()
        time.sleep(0.05)  # a real blocking call, not an await
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "_read_sync", spy)
    fake_db(columns=["a"], rows=[[1]])

    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await anyio.sleep(0.005)
            ticks += 1

    async with anyio.create_task_group() as tg:
        tg.start_soon(tick)
        await run_read("SELECT a FROM t", max_rows=10)
        tg.cancel_scope.cancel()

    assert observed["thread"] != loop_thread, "query ran on the event loop"
    assert ticks > 0, "the event loop was blocked while the query ran"


async def test_a_slow_query_does_not_block_other_requests(env, fake_db, monkeypatch):
    original = db._read_sync

    def slow(*args, **kwargs):
        time.sleep(0.1)
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "_read_sync", slow)
    fake_db(columns=["a"], rows=[[1]])

    async with Client(mcp) as c:
        slow_call = asyncio.create_task(c.call_tool("read_query", {"sql": "SELECT a FROM t"}))
        # A cheap round-trip must complete while the slow query is in flight.
        tools = await c.list_tools()
        assert len(tools) == 4
        await slow_call


# --------------------------------------------------------------------------
# Connection handling under concurrency
# --------------------------------------------------------------------------


async def test_concurrent_reads_each_get_their_own_connection(env, fake_db):
    """Sharing one connection across concurrent cursors is not safe."""
    fake_db(columns=["a"], rows=[[1]])
    await asyncio.gather(*(run_read("SELECT a FROM t", max_rows=10) for _ in range(8)))
    assert fake_db.holder["calls"] == 8


async def test_every_connection_is_closed(env, fake_db):
    fake_db(columns=["a"], rows=[[1]])
    await asyncio.gather(*(run_read("SELECT a FROM t", max_rows=10) for _ in range(5)))
    assert all(conn.closed for conn in fake_db.holder["all"])


async def test_connections_are_closed_even_when_queries_fail(env, fake_db):
    import mssql_python

    fake_db(columns=None, execute_error=mssql_python.ProgrammingError("bad", ""))
    for _ in range(5):
        with pytest.raises(mssql_python.ProgrammingError):
            await run_read("SELECT bad", max_rows=10)
    assert all(conn.closed for conn in fake_db.holder["all"])


async def test_repeated_calls_do_not_accumulate_state(env, fake_db):
    fake_db(columns=["a"], rows=[[1]])
    for _ in range(50):
        result = await run_read("SELECT a FROM t", max_rows=10)
        assert result.row_count == 1
    assert len(fake_db.holder["all"]) == 50
    assert all(conn.closed for conn in fake_db.holder["all"])
