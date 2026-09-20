"""PostgreSQL provider (psycopg 3)."""

from __future__ import annotations

from typing import Any

from ..config import DatabaseSettings
from .base import CATEGORY_CONNECTION, CATEGORY_SQL_ERROR, CATEGORY_TIMEOUT, DriverError

DEFAULT_PORT = 5432


class PostgresProvider:
    name = "postgresql"

    def connect(self, settings: DatabaseSettings) -> Any:
        import psycopg

        # `sslmode=require` by default: the gateway usually sits on the same network as the
        # database, but "usually" is not a security model, and the operator can still
        # downgrade it explicitly with DB_OPT_SSLMODE.
        options: dict[str, Any] = {"sslmode": "require"}
        options.update(settings.options)

        return psycopg.connect(
            host=settings.host,
            port=settings.port or DEFAULT_PORT,
            dbname=settings.database,
            user=settings.user,
            password=settings.password,
            # Bounded so a wedged network gives a clear failure inside the job's lifetime
            # rather than hanging until Qlar gives up on us.
            connect_timeout=10,
            autocommit=False,
            **options,
        )

    def begin_read_only(self, connection: Any, statement_timeout_seconds: int) -> Any:
        cursor = connection.cursor()
        # Enforced by the server: even a statement our parser misread cannot write.
        cursor.execute("BEGIN TRANSACTION READ ONLY")
        # Server-side timeout as well as a client-side one. Only this one actually stops
        # the query burning the customer's CPU — a client that walks away leaves the
        # backend running.
        cursor.execute(f"SET LOCAL statement_timeout = {int(statement_timeout_seconds) * 1000}")
        return cursor

    def classify_error(self, error: BaseException) -> DriverError | None:
        try:
            import psycopg
        except ImportError:  # pragma: no cover - only when the extra is not installed
            return None

        if isinstance(error, psycopg.errors.QueryCanceled):
            return DriverError(
                category=CATEGORY_TIMEOUT,
                driver_code=error.sqlstate,
                message=_message_of(error),
            )

        if isinstance(error, psycopg.Error) and getattr(error, "sqlstate", None):
            sqlstate = str(error.sqlstate)
            # Class 08 is "Connection Exception" in the SQLSTATE standard; 28/3D/53 cover
            # a refused login, a missing database and exhausted server resources.
            connection_like = sqlstate.startswith(("08", "28", "3D", "53", "57P"))
            diagnostic = getattr(error, "diag", None)
            return DriverError(
                category=CATEGORY_CONNECTION if connection_like else CATEGORY_SQL_ERROR,
                driver_code=sqlstate,
                message=_message_of(error),
                hint=getattr(diagnostic, "message_hint", None),
                position=_int_or_none(getattr(diagnostic, "statement_position", None)),
            )

        if isinstance(error, psycopg.Error):
            # No SQLSTATE means the server never answered: host unreachable, TLS failure,
            # socket dropped. A connection failure by construction.
            return DriverError(
                category=CATEGORY_CONNECTION,
                driver_code=None,
                message=str(error).strip(),
            )

        return None

    def type_name(self, description_entry: Any) -> str:
        # psycopg 3's Column exposes the resolved PostgreSQL type name directly.
        return str(getattr(description_entry, "type_display", None) or "unknown")

    def privilege_check_sql(self) -> str | None:
        # True when the account can create objects in the current database — the cheapest
        # signal that it is more than a reader.
        return "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"


def _message_of(error: BaseException) -> str:
    diagnostic = getattr(error, "diag", None)
    primary = getattr(diagnostic, "message_primary", None)
    return str(primary or error).strip()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
