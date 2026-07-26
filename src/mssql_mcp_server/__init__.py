"""A Model Context Protocol server for Microsoft SQL Server."""


def main() -> None:
    """Run the MCP server over stdio."""
    # Imported lazily so that importing this package -- which the test suite and
    # `fastmcp inspect` both do -- never pulls in the database driver or reads
    # configuration as a side effect.
    from mssql_mcp_server.server import run

    run()


__all__ = ["main"]
