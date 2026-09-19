"""Value encoding: what must survive the trip to Qlar intact.

The cases that matter are the ones where JSON is lossier than the database — money
columns, big identifiers, timestamps and binary — plus the ceilings that stop one wide
query from pushing a hundred megabytes over someone's internet connection.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import uuid

from qlar_db_wrapper.encoding import (
    BINARY_PREFIX,
    MAX_EXACT_INT,
    TruncationReason,
    encode_rows,
    encode_value,
)


class FakeCursor:
    """The two DB-API members the encoder uses."""

    def __init__(self, rows, description=None):
        self._rows = list(rows)
        self.description = description or []

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class TestPrecision:
    def test_decimal_becomes_a_string(self):
        # As a JSON number this would be parsed as a double and lose its last digits —
        # on a money column that is a wrong answer delivered with full confidence.
        value = decimal.Decimal("12345678901234567890.12345678")
        assert encode_value(value) == "12345678901234567890.12345678"

    def test_small_integers_stay_numbers(self):
        assert encode_value(42) == 42
        assert encode_value(-1000) == -1000
        assert encode_value(MAX_EXACT_INT) == MAX_EXACT_INT

    def test_integers_beyond_double_precision_become_strings(self):
        big = MAX_EXACT_INT + 1
        assert encode_value(big) == str(big)

    def test_round_trip_through_json_keeps_the_digits(self):
        value = decimal.Decimal("0.1") + decimal.Decimal("0.2")
        restored = json.loads(json.dumps({"v": encode_value(value)}))["v"]
        assert decimal.Decimal(restored) == decimal.Decimal("0.3")


class TestTypes:
    def test_none_and_bool(self):
        assert encode_value(None) is None
        assert encode_value(True) is True

    def test_datetime_is_iso_with_offset(self):
        value = dt.datetime(2026, 9, 19, 14, 30, tzinfo=dt.UTC)
        assert encode_value(value) == "2026-09-19T14:30:00+00:00"

    def test_date_and_time(self):
        assert encode_value(dt.date(2026, 1, 2)) == "2026-01-02"
        assert encode_value(dt.time(7, 5)) == "07:05:00"

    def test_binary_is_prefixed_base64(self):
        encoded = encode_value(b"\x00\x01hello")
        assert encoded.startswith(BINARY_PREFIX)
        assert base64.b64decode(encoded[len(BINARY_PREFIX):]) == b"\x00\x01hello"

    def test_uuid_becomes_text(self):
        value = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert encode_value(value) == "12345678-1234-5678-1234-567812345678"

    def test_nested_containers_are_encoded_through(self):
        value = {"totals": [decimal.Decimal("1.50"), None], "at": dt.date(2026, 1, 1)}
        assert encode_value(value) == {"totals": ["1.50", None], "at": "2026-01-01"}

    def test_non_finite_floats_do_not_produce_invalid_json(self):
        # json.dumps would happily write NaN, and the receiving parser would reject the
        # entire payload because of one odd value.
        payload = json.dumps([encode_value(float("nan")), encode_value(float("inf"))])
        assert "NaN" not in payload.replace('"nan"', "")
        json.loads(payload)  # must parse


class TestCeilings:
    def test_stops_at_the_row_ceiling(self):
        cursor = FakeCursor([(i,) for i in range(100)])
        rows, reason = encode_rows(cursor, max_rows=10, max_bytes=10_000_000)
        assert len(rows) == 10
        assert reason == TruncationReason.ROW_LIMIT

    def test_reports_no_truncation_when_the_result_fits(self):
        cursor = FakeCursor([(1,), (2,)])
        rows, reason = encode_rows(cursor, max_rows=10, max_bytes=10_000_000)
        assert len(rows) == 2
        assert reason == TruncationReason.NONE

    def test_stops_at_the_byte_ceiling(self):
        wide = "x" * 5000
        cursor = FakeCursor([(wide,) for _ in range(100)])
        rows, reason = encode_rows(cursor, max_rows=100, max_bytes=20_000)
        assert reason == TruncationReason.BYTE_LIMIT
        assert 0 < len(rows) < 100

    def test_a_single_oversized_row_is_still_returned(self):
        # Returning nothing would be less useful than returning the one row, as long as
        # the caller is told the result was truncated.
        cursor = FakeCursor([("y" * 50_000,)])
        rows, reason = encode_rows(cursor, max_rows=10, max_bytes=1_000)
        assert len(rows) == 1
        assert reason == TruncationReason.BYTE_LIMIT

    def test_rows_keep_their_arity_when_values_are_null(self):
        cursor = FakeCursor([(1, None, "a"), (2, None, None)])
        rows, _ = encode_rows(cursor, max_rows=10, max_bytes=10_000_000)
        assert [len(row) for row in rows] == [3, 3]
