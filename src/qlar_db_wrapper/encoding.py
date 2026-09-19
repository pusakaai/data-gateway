"""Turning driver values into JSON that survives the trip to Qlar.

The result format is columnar — a list of column descriptors plus positional rows —
because that is what Qlar's `TabularResult` already hands to the model, and because
repeating a column name once per row is paid for twice: once on the wire and again in
tokens.

The type rules exist because JSON is lossier than a database:

* **Decimals and large integers become strings.** A JSON number is an IEEE double to most
  parsers, so `decimal(38,10)` and anything past 2^53 would silently lose digits. A money
  column that comes back subtly wrong is worse than one that comes back as text.
* **Dates become ISO-8601 strings**, timezone offset included when the value has one.
* **Binary becomes `base64:`-prefixed text**, so a consumer can tell an encoded blob from
  a string that merely looks like base64.
* **NULL stays `null`** and rows are never shortened — a positional row is only readable
  while every row has the same arity.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import ipaddress
import json
import uuid
from collections.abc import Callable
from typing import Any

# Beyond this an integer is no longer exactly representable as an IEEE double, which is
# what most JSON parsers produce. Such values are emitted as strings instead.
MAX_EXACT_INT = 2**53 - 1

BINARY_PREFIX = "base64:"


class TruncationReason:
    NONE = None
    ROW_LIMIT = "row_limit"
    BYTE_LIMIT = "byte_limit"


def encode_value(value: Any) -> Any:
    """Maps one driver value to something `json.dumps` can render without losing it."""
    if value is None or isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value if abs(value) <= MAX_EXACT_INT else str(value)

    if isinstance(value, float):
        # NaN/Infinity are not JSON. Python would happily emit them and the receiving
        # parser would reject the whole payload, so one odd value must not poison a result.
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        return value

    if isinstance(value, decimal.Decimal):
        return str(value)

    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()

    if isinstance(value, dt.timedelta):
        return str(value)

    if isinstance(value, (bytes, bytearray, memoryview)):
        return BINARY_PREFIX + base64.b64encode(bytes(value)).decode("ascii")

    if isinstance(value, uuid.UUID):
        return str(value)

    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address,
                          ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return str(value)

    # PostgreSQL arrays, JSON/JSONB columns and composite types arrive as native Python
    # containers; encode through them so a nested Decimal is handled too.
    if isinstance(value, (list, tuple, set)):
        return [encode_value(item) for item in value]

    if isinstance(value, dict):
        return {str(key): encode_value(item) for key, item in value.items()}

    if isinstance(value, range):
        return [encode_value(item) for item in value]

    # Anything a driver invented for itself (PostgreSQL ranges, Oracle LOB handles that
    # already materialized, ...). Stringifying beats failing the whole query.
    return str(value)


def column_descriptors(
    cursor: Any,
    type_namer: Callable[[Any], str] | None = None,
) -> list[dict[str, str]]:
    """Builds the `columns` array from a DB-API cursor description.

    Duplicate names are kept as-is rather than de-duplicated: rows are positional, so two
    columns called `name` do not collide the way they would in a per-row object — which is
    exactly the bug Qlar had to fix on its own side for the direct path.
    """
    description = cursor.description or []
    columns: list[dict[str, str]] = []
    for entry in description:
        name = entry[0] if entry and entry[0] is not None else ""
        type_name = "unknown"
        if type_namer is not None:
            try:
                type_name = type_namer(entry)
            except Exception:  # a driver's type map should never fail a query
                type_name = "unknown"
        columns.append({"name": str(name), "type": type_name})
    return columns


def encode_rows(
    cursor: Any,
    max_rows: int,
    max_bytes: int,
) -> tuple[list[list[Any]], str | None]:
    """Reads at most `max_rows` rows, stopping early if the payload gets too large.

    Returns the encoded rows and why reading stopped, if it did.

    The caller asks for one row beyond the ceiling it actually wants, exactly as the
    direct path does, so that "there was more data" can be reported rather than silently
    truncating. The byte ceiling is the new constraint the wrapper introduces: on the
    direct path the result never crossed the public internet, and a single wide `SELECT *`
    could otherwise try to push hundreds of megabytes through Qlar.
    """
    rows: list[list[Any]] = []
    running_bytes = 0

    while len(rows) < max_rows:
        record = cursor.fetchone()
        if record is None:
            return rows, TruncationReason.NONE

        encoded = [encode_value(value) for value in record]

        # Measured on the encoded row rather than estimated: a text column holding a 2 MB
        # document is the case this ceiling exists for, and guessing would miss it.
        running_bytes += len(json.dumps(encoded, ensure_ascii=False).encode("utf-8"))
        if running_bytes > max_bytes and rows:
            return rows, TruncationReason.BYTE_LIMIT

        rows.append(encoded)
        if running_bytes > max_bytes:
            # The very first row alone exceeded the ceiling — keep it, since returning
            # nothing would be less useful than returning one oversized row, but say so.
            return rows, TruncationReason.BYTE_LIMIT

    return rows, TruncationReason.ROW_LIMIT
