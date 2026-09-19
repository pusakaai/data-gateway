"""MySQL / MariaDB provider (PyMySQL)."""

from __future__ import annotations

from typing import Any

from ..config import DatabaseSettings
from .base import CATEGORY_CONNECTION, CATEGORY_SQL_ERROR, CATEGORY_TIMEOUT, DriverError

DEFAULT_PORT = 3306

# Client-side failures (host unreachable, socket dropped, handshake refused) and the
# server-side codes that mean the session never got off the ground.
CONNECTION_ERROR_CODES = {0, 1042, 1043, 1044, 1045, 1049, 2002, 2003, 2005, 2006, 2013, 2026}
TIMEOUT_ERROR_CODES = {1205, 3024}

# Field type codes worth naming; anything else is reported by its numeric code, which is
# still more useful to a human than "unknown".
FIELD_TYPES = {
    0: "decimal", 1: "tinyint", 2: "smallint", 3: "int", 4: "float", 5: "double",
    7: "timestamp", 8: "bigint", 9: "mediumint", 10: "date", 11: "time", 12: "datetime",
    13: "year", 15: "varchar", 16: "bit", 245: "json", 246: "decimal", 252: "blob",
    253: "varchar", 254: "char",
}


class MySqlProvider:
    name = "mysql"

    def connect(self, settings: DatabaseSettings) -> Any:
        import pymysql

        options: dict[str, Any] = dict(settings.options)
        return pymysql.connect(
            host=settings.host,
            port=settings.port or DEFAULT_PORT,
            database=settings.database,
            user=settings.user,
            password=settings.password,
            connect_timeout=10,
            autocommit=False,
            **options,
        )

    def begin_read_only(self, connection: Any, statement_timeout_seconds: int) -> Any:
        cursor = connection.cursor()
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        # MySQL counts this one in milliseconds and applies it per statement; MariaDB
        # spells it `max_statement_time` in seconds, so a failure to set it is tolerated
        # rather than fatal — the client-side deadline in executor.py still applies.
        try:
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(statement_timeout_seconds) * 1000}")
        except Exception:
            try:
                cursor.execute(f"SET SESSION max_statement_time = {int(statement_timeout_seconds)}")
            except Exception:  # noqa: S110 - deliberate best-effort cleanup
                pass
        cursor.execute("START TRANSACTION READ ONLY")
        return cursor

    def classify_error(self, error: BaseException) -> DriverError | None:
        try:
            import pymysql
        except ImportError:  # pragma: no cover
            return None

        if not isinstance(error, pymysql.Error):
            return None

        code = error.args[0] if error.args and isinstance(error.args[0], int) else 0
        message = str(error.args[1]) if len(error.args) > 1 else str(error)

        if code in TIMEOUT_ERROR_CODES:
            category = CATEGORY_TIMEOUT
        elif code in CONNECTION_ERROR_CODES:
            category = CATEGORY_CONNECTION
        else:
            category = CATEGORY_SQL_ERROR

        return DriverError(category=category, driver_code=str(code), message=message.strip())

    def type_name(self, description_entry: Any) -> str:
        type_code = description_entry[1] if len(description_entry) > 1 else None
        if type_code is None:
            return "unknown"
        return FIELD_TYPES.get(int(type_code), f"type_{int(type_code)}")

    def privilege_check_sql(self) -> str | None:
        return "SHOW GRANTS FOR CURRENT_USER()"
