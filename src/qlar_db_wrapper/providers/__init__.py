"""Provider registry.

Each provider is imported lazily so that installing one database extra does not require
the other three drivers to be present — an on-premise install should only pull the driver
it actually uses.
"""

from __future__ import annotations

from .base import CATEGORY_CONNECTION, CATEGORY_SQL_ERROR, CATEGORY_TIMEOUT, DriverError, Provider

__all__ = [
    "CATEGORY_CONNECTION",
    "CATEGORY_SQL_ERROR",
    "CATEGORY_TIMEOUT",
    "DriverError",
    "Provider",
    "get_provider",
]


def get_provider(name: str) -> Provider:
    """Returns the provider for a name, raising a readable error for a missing driver."""
    key = (name or "").strip().lower()

    if key == "postgresql":
        from .postgres import PostgresProvider

        return _ensure_driver(PostgresProvider(), "psycopg[binary]", "postgresql")
    if key == "mysql":
        from .mysql import MySqlProvider

        return _ensure_driver(MySqlProvider(), "PyMySQL", "mysql")
    if key == "sqlserver":
        from .sqlserver import SqlServerProvider

        return _ensure_driver(SqlServerProvider(), "pymssql", "sqlserver")
    if key == "oracle":
        from .oracle import OracleProvider

        return _ensure_driver(OracleProvider(), "oracledb", "oracle")

    raise ValueError(f"unsupported provider {name!r}")


def _ensure_driver(provider: Provider, package: str, extra: str) -> Provider:
    """Turns a missing optional driver into an instruction rather than a traceback."""
    import importlib.util

    modules = {"postgresql": "psycopg", "mysql": "pymysql", "sqlserver": "pymssql", "oracle": "oracledb"}
    module = modules[extra]
    if importlib.util.find_spec(module) is None:
        raise ImportError(
            f"the {extra} driver is not installed. Install it with: "
            f"pip install 'qlar-db-wrapper[{extra}]'  (package: {package})"
        )
    return provider
