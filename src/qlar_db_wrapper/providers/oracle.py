"""Oracle provider (python-oracledb, thin mode).

Thin mode needs no Oracle Instant Client on the host — which for an on-premise install is
the difference between "pip install" and "get a DBA to stage a client library".
"""

from __future__ import annotations

from typing import Any

from ..config import DatabaseSettings
from .base import CATEGORY_CONNECTION, CATEGORY_SQL_ERROR, CATEGORY_TIMEOUT, DriverError

DEFAULT_PORT = 1521

CONNECTION_ERROR_NUMBERS = {1017, 1005, 1004, 28000, 3113, 3114, 3135,
                            12154, 12170, 12505, 12514, 12537, 12541, 12545, 12560}
# ORA-01013 "user requested cancel of current operation" is how a client-side timeout
# surfaces; ORA-00051/00054 are resource waits.
TIMEOUT_ERROR_NUMBERS = {1013, 51, 54}


class OracleProvider:
    name = "oracle"

    def connect(self, settings: DatabaseSettings) -> Any:
        import oracledb

        options: dict[str, Any] = dict(settings.options)
        # DB_NAME carries either a service name (the common case) or, with DB_OPT_SID set,
        # a SID. Both spellings exist in the wild and a customer should not have to know
        # which one this wrapper prefers.
        sid = options.pop("sid", None)
        dsn = oracledb.makedsn(
            settings.host,
            settings.port or DEFAULT_PORT,
            sid=sid,
            service_name=None if sid else settings.database,
        )

        return oracledb.connect(
            user=settings.user,
            password=settings.password,
            dsn=dsn,
            tcp_connect_timeout=10,
            **options,
        )

    def begin_read_only(self, connection: Any, statement_timeout_seconds: int) -> Any:
        cursor = connection.cursor()
        # Enforced by the server for the whole transaction.
        cursor.execute("SET TRANSACTION READ ONLY")
        # oracledb's call timeout is in milliseconds and covers a round trip, which is the
        # closest Oracle equivalent to a statement timeout available without a resource
        # manager plan.
        connection.call_timeout = int(statement_timeout_seconds) * 1000
        return cursor

    def classify_error(self, error: BaseException) -> DriverError | None:
        try:
            import oracledb
        except ImportError:  # pragma: no cover
            return None

        if not isinstance(error, oracledb.Error):
            return None

        info = error.args[0] if error.args else None
        number = getattr(info, "code", None)
        message = str(getattr(info, "message", None) or error).strip()
        offset = getattr(info, "offset", None)

        if number in TIMEOUT_ERROR_NUMBERS or isinstance(error, oracledb.OperationalError) and number is None:
            category = CATEGORY_TIMEOUT if number in TIMEOUT_ERROR_NUMBERS else CATEGORY_CONNECTION
        elif number in CONNECTION_ERROR_NUMBERS:
            category = CATEGORY_CONNECTION
        else:
            category = CATEGORY_SQL_ERROR

        return DriverError(
            category=category,
            driver_code=str(number) if number is not None else None,
            message=message,
            position=int(offset) + 1 if isinstance(offset, int) and offset >= 0 else None,
        )

    def type_name(self, description_entry: Any) -> str:
        type_code = description_entry[1] if len(description_entry) > 1 else None
        return str(getattr(type_code, "name", None) or getattr(type_code, "__name__", None) or "unknown")

    def privilege_check_sql(self) -> str | None:
        return (
            "SELECT COUNT(*) FROM session_privs "
            "WHERE privilege IN ('CREATE TABLE', 'CREATE ANY TABLE', 'DROP ANY TABLE')"
        )
