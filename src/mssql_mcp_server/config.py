"""Configuration: environment variables to ODBC connection parameters.

``Settings.odbc_params()`` returns a **dict**, which callers pass to
``mssql_python.connect(**params)``. It deliberately does not build a connection
string: the driver's own builder braces any value containing ``;{}=`` or a
space, so passing a dict gets password escaping for free. A hand-concatenated
string silently corrupts on a password containing ``;``.

Every key produced here must appear in the driver's connection-string
allowlist; ``tests/test_config.py`` asserts that against the driver itself.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache

logger = logging.getLogger("mssql_mcp_server.config")

__all__ = ["ApprovalMode", "Settings", "get_settings", "load_settings"]

_AZURE_SUFFIX = ".database.windows.net"
_DEFAULT_MAX_ROWS = 1000
_DEFAULT_CONNECT_TIMEOUT = 30
_LOCALDB_PREFIX = "(localdb)"


class ApprovalMode(StrEnum):
    """How ``execute_write`` obtains the user's consent."""

    ELICIT = "elicit"
    """Ask the user via MCP elicitation. Refuse if the client cannot ask."""

    ALLOW = "allow"
    """Skip the checkpoint entirely. For headless and CI use only."""

    READONLY = "readonly"
    """Never register the write tool at all."""


class ConfigError(ValueError):
    """The environment does not describe a usable connection."""


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    if raw.lower() in ("true", "1", "yes", "on"):
        return True
    if raw.lower() in ("false", "0", "no", "off"):
        return False
    logger.warning("Invalid %s value %r; using default %s.", name, raw, default)
    return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s value %r; using default %d.", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s must be >= %d, got %d; using default %d.", name, minimum, value, default)
        return default
    return value


def _host_of(server: str) -> str:
    """The hostname alone, without any ``,port`` or ``\\instance`` suffix."""
    return re.split(r"[,\\]", server, maxsplit=1)[0].strip()


def _fold_port(server: str, raw_port: str | None) -> str:
    """Fold ``MSSQL_PORT`` into the server value as ODBC's ``host,port`` form.

    ODBC Driver 18 has no separate ``Port`` keyword. A named instance and an
    explicit port are mutually exclusive, so the instance wins.
    """
    if raw_port is None:
        return server

    try:
        port = int(raw_port)
    except ValueError:
        logger.warning("Invalid MSSQL_PORT value %r; ignoring it.", raw_port)
        return server

    if not 1 <= port <= 65535:
        logger.warning("MSSQL_PORT %d is out of range 1-65535; ignoring it.", port)
        return server

    if server.lower().startswith(_LOCALDB_PREFIX):
        logger.warning("MSSQL_PORT is not applicable to a LocalDB connection; ignoring it.")
        return server

    if "," in server:
        logger.warning(
            "MSSQL_SERVER %r already specifies a port; ignoring MSSQL_PORT=%d.", server, port
        )
        return server

    if "\\" in server:
        logger.warning(
            "MSSQL_SERVER %r names an instance, which is mutually exclusive with a port in "
            "ODBC; ignoring MSSQL_PORT=%d.",
            server,
            port,
        )
        return server

    return f"{server},{port}"


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved connection and policy configuration."""

    server: str
    database: str
    user: str | None = None
    # repr=False keeps the password out of reprs, pytest output and tracebacks.
    password: str | None = field(default=None, repr=False)
    windows_auth: bool = False
    encrypt: bool = True
    trust_server_certificate: bool = False
    approval_mode: ApprovalMode = ApprovalMode.ELICIT
    max_rows: int = _DEFAULT_MAX_ROWS
    connect_timeout: int = _DEFAULT_CONNECT_TIMEOUT

    @property
    def is_azure(self) -> bool:
        return _host_of(self.server).lower().endswith(_AZURE_SUFFIX)

    def odbc_params(self) -> dict[str, str]:
        """Connection keywords for ``mssql_python.connect(**params)``.

        Never log the result: it contains the password.
        """
        params: dict[str, str] = {
            "server": self.server,
            "database": self.database,
            "encrypt": "yes" if self.encrypt else "no",
            "trustservercertificate": "yes" if self.trust_server_certificate else "no",
        }
        if self.windows_auth:
            params["trusted_connection"] = "yes"
        else:
            params["uid"] = self.user or ""
            params["pwd"] = self.password or ""
        return params

    def describe(self) -> str:
        """A safe one-line summary. This is the only form ever logged."""
        auth = "windows" if self.windows_auth else f"sql user={self.user}"
        return (
            f"server={self.server} database={self.database} auth={auth} "
            f"encrypt={'yes' if self.encrypt else 'no'} "
            f"trust_cert={'yes' if self.trust_server_certificate else 'no'} "
            f"approval={self.approval_mode} max_rows={self.max_rows}"
        )


def load_settings() -> Settings:
    """Build ``Settings`` from the environment. Raises ``ConfigError``."""
    raw_server = _env("MSSQL_SERVER") or "localhost"
    server = _fold_port(raw_server, _env("MSSQL_PORT"))

    database = _env("MSSQL_DATABASE")
    if not database:
        raise ConfigError("MSSQL_DATABASE is required.")

    windows_auth = _env_bool("MSSQL_WINDOWS_AUTH", False)
    user = _env("MSSQL_USER")
    password = _env("MSSQL_PASSWORD")

    if not windows_auth and not (user and password):
        raise ConfigError(
            "MSSQL_USER and MSSQL_PASSWORD are required for SQL authentication. "
            "Set MSSQL_WINDOWS_AUTH=true to use Windows authentication instead."
        )
    if windows_auth:
        user = None
        password = None

    encrypt = _env_bool("MSSQL_ENCRYPT", True)
    trust_cert = _env_bool("MSSQL_TRUST_SERVER_CERTIFICATE", False)

    # Azure SQL always requires a verified encrypted connection. Overriding
    # that is never what the user wants, so force it and say so.
    if _host_of(server).lower().endswith(_AZURE_SUFFIX):
        if not encrypt:
            logger.warning("Azure SQL requires encryption; ignoring MSSQL_ENCRYPT=false.")
        if trust_cert:
            logger.warning(
                "Azure SQL presents a verifiable certificate; ignoring "
                "MSSQL_TRUST_SERVER_CERTIFICATE=true."
            )
        encrypt, trust_cert = True, False

    raw_mode = (_env("MSSQL_APPROVAL_MODE") or ApprovalMode.ELICIT.value).lower()
    try:
        approval_mode = ApprovalMode(raw_mode)
    except ValueError:
        raise ConfigError(
            f"Invalid MSSQL_APPROVAL_MODE {raw_mode!r}. "
            f"Expected one of: {', '.join(m.value for m in ApprovalMode)}."
        ) from None

    settings = Settings(
        server=server,
        database=database,
        user=user,
        password=password,
        windows_auth=windows_auth,
        encrypt=encrypt,
        trust_server_certificate=trust_cert,
        approval_mode=approval_mode,
        max_rows=_env_int("MSSQL_MAX_ROWS", _DEFAULT_MAX_ROWS),
        connect_timeout=_env_int("MSSQL_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT),
    )

    if settings.approval_mode is ApprovalMode.ALLOW:
        logger.warning(
            "MSSQL_APPROVAL_MODE=allow: the write-approval checkpoint is DISABLED. "
            "Write statements will execute without asking the user."
        )

    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached ``load_settings()``.

    Lazy on purpose: importing this package must never read configuration, so
    that `fastmcp inspect` and the test suite work without MSSQL_* set.
    """
    return load_settings()
