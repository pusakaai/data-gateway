"""Microsoft SQL Server provider (pymssql).

pymssql over pyodbc deliberately: it ships manylinux wheels with FreeTDS bundled, so an
on-premise install needs no system ODBC driver and no `odbcinst.ini` surgery. The people
installing this are doing it once, under time pressure, on a server they did not choose.
"""

from __future__ import annotations

from typing import Any

from ..config import DatabaseSettings
from .base import CATEGORY_CONNECTION, CATEGORY_SQL_ERROR, CATEGORY_TIMEOUT, DriverError

DEFAULT_PORT = 1433

CONNECTION_ERROR_NUMBERS = {2, 53, 64, 233, 4060, 4064, 10053, 10054, 10060, 10061, 18456,
                            40197, 40501, 40613, 20009, 20002}
TIMEOUT_ERROR_NUMBERS = {-2, 1222}


class SqlServerProvider:
    name = "sqlserver"

    def connect(self, settings: DatabaseSettings) -> Any:
        import pymssql

        options: dict[str, Any] = dict(settings.options)
        return pymssql.connect(
            server=settings.host,
            port=str(settings.port or DEFAULT_PORT),
            database=settings.database,
            user=settings.user,
            password=settings.password,
            login_timeout=10,
            autocommit=False,
            **options,
        )

    def begin_read_only(self, connection: Any, statement_timeout_seconds: int) -> Any:
        cursor = connection.cursor()
        # SQL Server has no READ ONLY transaction. The closest server-side equivalents are
        # a read-only database user (documented as required in INSTALL.md) and this
        # isolation level, which at least keeps the read from blocking writers. The guard
        # and the account's own permissions carry the weight here, so setting up a
        # SELECT-only login is not optional advice on this provider.
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        # Server-side query governor, in seconds.
        cursor.execute(f"SET LOCK_TIMEOUT {int(statement_timeout_seconds) * 1000}")
        cursor.execute("BEGIN TRANSACTION")
        return cursor

    def classify_error(self, error: BaseException) -> DriverError | None:
        try:
            import pymssql
        except ImportError:  # pragma: no cover
            return None

        if not isinstance(error, pymssql.Error):
            return None

        number = error.args[0] if error.args and isinstance(error.args[0], int) else None
        message = str(error.args[1]) if len(error.args) > 1 else str(error)
        if isinstance(message, bytes):  # FreeTDS hands some messages back as bytes
            message = message.decode("utf-8", errors="replace")

        if number in TIMEOUT_ERROR_NUMBERS:
            category = CATEGORY_TIMEOUT
        elif number in CONNECTION_ERROR_NUMBERS or isinstance(error, pymssql.InterfaceError):
            category = CATEGORY_CONNECTION
        else:
            category = CATEGORY_SQL_ERROR

        return DriverError(
            category=category,
            driver_code=str(number) if number is not None else None,
            message=message.strip(),
        )

    def type_name(self, description_entry: Any) -> str:
        type_code = description_entry[1] if len(description_entry) > 1 else None
        return str(getattr(type_code, "__name__", type_code) or "unknown")

    def privilege_check_sql(self) -> str | None:
        return "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE')"
