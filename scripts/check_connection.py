#!/usr/bin/env python
"""Diagnose the database connection using the server's own configuration.

Env-driven, exactly like the server itself, so that a success here means the
server will connect too:

    MSSQL_SERVER=localhost,1433 MSSQL_DATABASE=master MSSQL_USER=sa \
    MSSQL_PASSWORD=... MSSQL_TRUST_SERVER_CERTIFICATE=true \
    python scripts/check_connection.py

Lives in scripts/ rather than the repo root so pytest never collects it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import mssql_python  # noqa: E402

from mssql_mcp_server.config import ConfigError, load_settings  # noqa: E402


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    print(f"Configuration: {settings.describe()}")
    print("\nConnecting...")

    try:
        with mssql_python.connect(
            timeout=settings.connect_timeout, **settings.odbc_params()
        ) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT @@VERSION")
            print(f"Connected. {cursor.fetchone()[0].splitlines()[0]}")

            cursor.execute(
                "SELECT TOP 5 TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME"
            )
            rows = cursor.fetchall()
            print(f"\nFound {len(rows)} table(s):")
            for schema, name in rows:
                print(f"  - {schema}.{name}")
            cursor.close()
    except mssql_python.ConnectionStringParseError as exc:
        print(f"\nThe connection settings are invalid: {exc}")
        return 1
    except Exception as exc:
        print(f"\nConnection failed: {type(exc).__name__}: {exc}")
        if not settings.trust_server_certificate and settings.encrypt:
            print(
                "\nHint: ODBC Driver 18 verifies the server certificate by default. "
                "For a local development server with a self-signed certificate, set "
                "MSSQL_TRUST_SERVER_CERTIFICATE=true."
            )
        return 1

    print("\nConnection test completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
