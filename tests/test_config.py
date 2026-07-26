"""Tests for environment -> ODBC configuration mapping. Pure: no database."""

import logging

import pytest

from mssql_mcp_server.config import (
    ApprovalMode,
    ConfigError,
    Settings,
    get_settings,
    load_settings,
)

BASE_ENV = {
    "MSSQL_SERVER": "localhost",
    "MSSQL_DATABASE": "testdb",
    "MSSQL_USER": "sa",
    "MSSQL_PASSWORD": "secret123",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove every MSSQL_* variable so ambient config cannot leak into a test."""
    import os

    for key in [k for k in os.environ if k.startswith("MSSQL_")]:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def env(monkeypatch, **overrides):
    for key, value in {**BASE_ENV, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


# --------------------------------------------------------------------------
# Required configuration
# --------------------------------------------------------------------------


def test_database_is_required(monkeypatch):
    env(monkeypatch, MSSQL_DATABASE=None)
    with pytest.raises(ConfigError, match="MSSQL_DATABASE is required"):
        load_settings()


def test_user_and_password_required_for_sql_auth(monkeypatch):
    env(monkeypatch, MSSQL_PASSWORD=None)
    with pytest.raises(ConfigError, match="MSSQL_USER and MSSQL_PASSWORD are required"):
        load_settings()


def test_server_defaults_to_localhost(monkeypatch):
    env(monkeypatch, MSSQL_SERVER=None)
    assert load_settings().server == "localhost"


def test_blank_values_are_treated_as_unset(monkeypatch):
    env(monkeypatch, MSSQL_DATABASE="   ")
    with pytest.raises(ConfigError, match="MSSQL_DATABASE is required"):
        load_settings()


# --------------------------------------------------------------------------
# Port folding
# --------------------------------------------------------------------------


def test_port_is_folded_into_the_server_value(monkeypatch):
    env(monkeypatch, MSSQL_SERVER="db.example.com", MSSQL_PORT="1433")
    assert load_settings().server == "db.example.com,1433"


def test_no_port_leaves_the_server_alone(monkeypatch):
    env(monkeypatch, MSSQL_SERVER="db.example.com")
    assert load_settings().server == "db.example.com"


def test_invalid_port_falls_back_instead_of_being_passed_through(monkeypatch, caplog):
    """Regression: the old code passed the literal string 'invalid' to the driver."""
    env(monkeypatch, MSSQL_SERVER="db.example.com", MSSQL_PORT="invalid")
    with caplog.at_level(logging.WARNING):
        settings = load_settings()
    assert settings.server == "db.example.com"
    assert "invalid" not in settings.odbc_params()["server"]
    assert "Invalid MSSQL_PORT" in caplog.text


@pytest.mark.parametrize("port", ["0", "-1", "70000"])
def test_out_of_range_port_is_ignored(monkeypatch, port):
    env(monkeypatch, MSSQL_SERVER="db.example.com", MSSQL_PORT=port)
    assert load_settings().server == "db.example.com"


def test_explicit_port_in_server_wins_over_the_port_variable(monkeypatch, caplog):
    env(monkeypatch, MSSQL_SERVER="db.example.com,1444", MSSQL_PORT="1433")
    with caplog.at_level(logging.WARNING):
        settings = load_settings()
    assert settings.server == "db.example.com,1444"
    assert "already specifies a port" in caplog.text


def test_named_instance_wins_over_the_port_variable(monkeypatch, caplog):
    env(monkeypatch, MSSQL_SERVER="host\\SQLEXPRESS", MSSQL_PORT="1433")
    with caplog.at_level(logging.WARNING):
        settings = load_settings()
    assert settings.server == "host\\SQLEXPRESS"
    assert "mutually exclusive" in caplog.text


# --------------------------------------------------------------------------
# LocalDB -- passes through now that we speak ODBC
# --------------------------------------------------------------------------


def test_localdb_passes_through_unrewritten(monkeypatch):
    """ODBC Driver 18 understands (localdb)\\X natively.

    The old code rewrote it to '.\\X' for pymssql; doing that now would break it.
    """
    env(monkeypatch, MSSQL_SERVER="(localdb)\\MSSQLLocalDB")
    assert load_settings().server == "(localdb)\\MSSQLLocalDB"


def test_localdb_never_gets_a_port_appended(monkeypatch):
    env(monkeypatch, MSSQL_SERVER="(localdb)\\MSSQLLocalDB", MSSQL_PORT="1433")
    assert load_settings().server == "(localdb)\\MSSQLLocalDB"


# --------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------


def test_encryption_is_on_by_default(monkeypatch):
    env(monkeypatch)
    settings = load_settings()
    assert settings.encrypt is True
    assert settings.trust_server_certificate is False
    assert settings.odbc_params()["encrypt"] == "yes"
    assert settings.odbc_params()["trustservercertificate"] == "no"


def test_encryption_can_be_disabled(monkeypatch):
    env(monkeypatch, MSSQL_ENCRYPT="false")
    assert load_settings().odbc_params()["encrypt"] == "no"


def test_trust_server_certificate_for_local_dev(monkeypatch):
    env(monkeypatch, MSSQL_TRUST_SERVER_CERTIFICATE="true")
    assert load_settings().odbc_params()["trustservercertificate"] == "yes"


@pytest.mark.parametrize("value", ["TRUE", "True", "1", "yes", "on"])
def test_boolean_parsing_is_forgiving(monkeypatch, value):
    env(monkeypatch, MSSQL_WINDOWS_AUTH=value)
    assert load_settings().windows_auth is True


def test_azure_is_detected_and_forces_verified_encryption(monkeypatch, caplog):
    env(
        monkeypatch,
        MSSQL_SERVER="myserver.database.windows.net",
        MSSQL_ENCRYPT="false",
        MSSQL_TRUST_SERVER_CERTIFICATE="true",
    )
    with caplog.at_level(logging.WARNING):
        settings = load_settings()
    assert settings.is_azure is True
    assert settings.encrypt is True
    assert settings.trust_server_certificate is False
    assert "Azure SQL requires encryption" in caplog.text


def test_azure_detection_survives_a_port_suffix(monkeypatch):
    env(monkeypatch, MSSQL_SERVER="myserver.database.windows.net", MSSQL_PORT="1433")
    settings = load_settings()
    assert settings.server == "myserver.database.windows.net,1433"
    assert settings.is_azure is True


def test_non_azure_host_is_not_treated_as_azure(monkeypatch):
    env(monkeypatch, MSSQL_SERVER="localhost")
    assert load_settings().is_azure is False


def test_server_string_is_never_mangled_with_odbc_keywords(monkeypatch):
    """Regression: the old code appended ';Encrypt=yes;...' to the hostname."""
    env(monkeypatch, MSSQL_SERVER="myserver.database.windows.net")
    assert load_settings().odbc_params()["server"] == "myserver.database.windows.net"


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_windows_auth_omits_credentials(monkeypatch):
    env(monkeypatch, MSSQL_WINDOWS_AUTH="true", MSSQL_USER=None, MSSQL_PASSWORD=None)
    params = load_settings().odbc_params()
    assert params["trusted_connection"] == "yes"
    assert "uid" not in params
    assert "pwd" not in params


def test_windows_auth_discards_any_supplied_credentials(monkeypatch):
    env(monkeypatch, MSSQL_WINDOWS_AUTH="true")
    settings = load_settings()
    assert settings.user is None
    assert settings.password is None
    assert "pwd" not in settings.odbc_params()


def test_windows_auth_still_requires_a_database(monkeypatch):
    env(monkeypatch, MSSQL_WINDOWS_AUTH="true", MSSQL_DATABASE=None)
    with pytest.raises(ConfigError, match="MSSQL_DATABASE is required"):
        load_settings()


def test_sql_auth_supplies_credentials(monkeypatch):
    env(monkeypatch)
    params = load_settings().odbc_params()
    assert params["uid"] == "sa"
    assert params["pwd"] == "secret123"
    assert "trusted_connection" not in params


# --------------------------------------------------------------------------
# Approval mode and limits
# --------------------------------------------------------------------------


def test_approval_mode_defaults_to_elicit(monkeypatch):
    env(monkeypatch)
    assert load_settings().approval_mode is ApprovalMode.ELICIT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("elicit", ApprovalMode.ELICIT),
        ("allow", ApprovalMode.ALLOW),
        ("readonly", ApprovalMode.READONLY),
        ("READONLY", ApprovalMode.READONLY),
    ],
)
def test_approval_mode_parsing(monkeypatch, value, expected):
    env(monkeypatch, MSSQL_APPROVAL_MODE=value)
    assert load_settings().approval_mode is expected


def test_invalid_approval_mode_is_rejected_loudly(monkeypatch):
    env(monkeypatch, MSSQL_APPROVAL_MODE="maybe")
    with pytest.raises(ConfigError, match="Invalid MSSQL_APPROVAL_MODE"):
        load_settings()


def test_allow_mode_logs_a_warning_banner(monkeypatch, caplog):
    env(monkeypatch, MSSQL_APPROVAL_MODE="allow")
    with caplog.at_level(logging.WARNING):
        load_settings()
    assert "checkpoint is DISABLED" in caplog.text


def test_max_rows_default_and_override(monkeypatch):
    env(monkeypatch)
    assert load_settings().max_rows == 1000
    env(monkeypatch, MSSQL_MAX_ROWS="50")
    assert load_settings().max_rows == 50


@pytest.mark.parametrize("value", ["0", "-5", "lots"])
def test_invalid_max_rows_falls_back(monkeypatch, value):
    env(monkeypatch, MSSQL_MAX_ROWS=value)
    assert load_settings().max_rows == 1000


# --------------------------------------------------------------------------
# The password must not escape
# --------------------------------------------------------------------------


def test_password_absent_from_repr(monkeypatch):
    env(monkeypatch)
    assert "secret123" not in repr(load_settings())


def test_password_absent_from_describe(monkeypatch):
    env(monkeypatch)
    settings = load_settings()
    assert "secret123" not in settings.describe()
    assert settings.database in settings.describe()


def test_password_present_only_in_odbc_params(monkeypatch):
    env(monkeypatch)
    settings = load_settings()
    assert settings.odbc_params()["pwd"] == "secret123"
    assert "secret123" not in str(settings)


def test_password_with_special_characters_is_passed_through_verbatim(monkeypatch):
    """The driver's own builder brackets values containing ';', '=' and spaces.

    Passing a dict is what lets it do that; a hand-built string would corrupt.
    """
    env(monkeypatch, MSSQL_PASSWORD="p;w=d {x}")
    assert load_settings().odbc_params()["pwd"] == "p;w=d {x}"


# --------------------------------------------------------------------------
# The driver must actually accept every key we emit
# --------------------------------------------------------------------------


def _all_param_sets(monkeypatch):
    env(monkeypatch)
    sql_auth = load_settings().odbc_params()
    env(monkeypatch, MSSQL_WINDOWS_AUTH="true")
    win_auth = load_settings().odbc_params()
    return [sql_auth, win_auth]


def test_every_emitted_key_is_in_the_driver_allowlist(monkeypatch):
    """Regression test against mssql-python itself.

    Keys outside its allowlist raise ConnectionStringParseError; keys passed as
    kwargs are silently dropped with only a log line. Either way we would not
    find out at runtime, so assert it here.
    """
    from mssql_python import constants

    allowed = constants._ALLOWED_CONNECTION_STRING_PARAMS
    for params in _all_param_sets(monkeypatch):
        for key in params:
            assert key in allowed, f"{key!r} is not a supported connection keyword"


def test_no_emitted_key_is_reserved(monkeypatch):
    """'Driver' and 'APP' are set by the driver itself and raise if we pass them."""
    from mssql_python import constants

    reserved = {name.lower() for name in constants._RESERVED_PARAMETERS}
    for params in _all_param_sets(monkeypatch):
        assert not (set(params) & reserved)


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


def test_get_settings_is_cached(monkeypatch):
    env(monkeypatch)
    assert get_settings() is get_settings()


def test_importing_the_package_does_not_read_configuration():
    """`fastmcp inspect` imports the module; it must not need MSSQL_* set."""
    import importlib

    import mssql_mcp_server

    importlib.reload(mssql_mcp_server)  # no ConfigError


def test_settings_can_be_constructed_directly_for_tests():
    settings = Settings(server="h", database="d", user="u", password="p")
    assert settings.approval_mode is ApprovalMode.ELICIT
    assert settings.encrypt is True
