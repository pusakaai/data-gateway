"""What every database provider has to supply.

The interface is small on purpose. All the judgement — which SQL is acceptable, how many
rows to return, how to encode a value — lives in shared code, so adding a provider means
answering four mechanical questions:

1. How do I open a connection?
2. How do I make this session read-only and time-limited?
3. What does this driver's error object actually mean?
4. What is this column's type called?

Point 3 is the one that matters most. Qlar classifies failures from the driver's own error
code — never from message text — because message-text matching once made plain syntax
errors report as connection failures in production. A provider that loses the code here
would reintroduce that bug through the back door, so `classify_error` returns the raw code
and lets Qlar decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..config import DatabaseSettings


@dataclass(frozen=True)
class DriverError:
    """A driver failure, described in the terms Qlar's classifier needs.

    Attributes:
        category: the wrapper's best guess — `connection`, `timeout` or `sql_error`. Qlar
            treats it as a hint; `driver_code` is what it actually classifies on.
        driver_code: SQLSTATE for PostgreSQL, the error number for MySQL/SQL Server/Oracle.
            Carried verbatim, as a string, so nothing is lost in translation.
        message: the server's own message, with any driver-appended noise stripped.
        hint: the server's suggested fix, where the dialect provides one (PostgreSQL does).
        position: 1-based character offset of the offending token, where available.
    """

    category: str
    driver_code: str | None
    message: str
    hint: str | None = None
    position: int | None = None


class Provider(Protocol):
    """A database this wrapper can read from."""

    name: str

    def connect(self, settings: DatabaseSettings) -> Any:
        """Opens a connection. Raises the driver's own exception on failure."""

    def begin_read_only(self, connection: Any, statement_timeout_seconds: int) -> Any:
        """Starts a read-only transaction and arms the statement timeout.

        Returns a cursor ready to execute. The read-only transaction is the wrapper's
        strongest guarantee: it is enforced by the database rather than by our parser, so
        it holds even against a statement the guard failed to understand.
        """

    def classify_error(self, error: BaseException) -> DriverError | None:
        """Describes a driver exception, or returns None if it is not this driver's."""

    def type_name(self, description_entry: Any) -> str:
        """Human-readable type for one entry of `cursor.description`."""

    def privilege_check_sql(self) -> str | None:
        """A query that reveals whether the configured account can write.

        Used once at startup to warn an operator who pointed the wrapper at an account
        with more rights than it needs. Returning None skips the check.
        """


CATEGORY_CONNECTION = "connection"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_SQL_ERROR = "sql_error"
