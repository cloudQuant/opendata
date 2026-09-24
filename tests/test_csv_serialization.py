"""CSV serialization tests (AC-11 parameter safety, design §10.1).

The export surface is where a data source's text reaches an analyst's
spreadsheet, so the formula-injection neutralization is tested as a
property of the shared serializer rather than of one endpoint: every
CSV built through :func:`serialize_for_csv` inherits it.
"""

from __future__ import annotations

import decimal
from datetime import date, datetime

import pytest

from opendata.utils.serialization import (
    FORMULA_ESCAPE,
    escape_csv_formula,
    serialize_for_csv,
    serialize_for_json,
)

#: Payloads a spreadsheet would execute on open.
INJECTIONS = (
    "=cmd|'/c calc'!A0",
    "+1+1",
    "-2+3+cmd|'/c calc'!A0",
    "@SUM(1+1)",
    "\t=1+1",
    "\r=1+1",
)


class TestFormulaEscaping:
    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_dangerous_prefixes_are_neutralized(self, payload):
        escaped = escape_csv_formula(payload)

        assert escaped.startswith(FORMULA_ESCAPE)
        assert escaped == f"{FORMULA_ESCAPE}{payload}"

    def test_ordinary_text_is_untouched(self):
        assert escape_csv_formula("600519") == "600519"
        assert escape_csv_formula("贵州茅台") == "贵州茅台"
        assert escape_csv_formula("") == ""

    def test_a_dangerous_prefix_in_the_middle_is_fine(self):
        assert escape_csv_formula("A=B") == "A=B"

    def test_serialize_for_csv_escapes_strings(self):
        assert serialize_for_csv("=1+1") == "'=1+1"

    def test_serialize_for_csv_escapes_decoded_bytes(self):
        assert serialize_for_csv(b"=1+1") == "'=1+1"

    def test_numbers_are_not_quoted(self):
        # A negative price is a number, not a formula: escaping it would
        # turn a numeric column into text for every spreadsheet reader.
        assert serialize_for_csv(decimal.Decimal("-1.5")) == "-1.5"
        assert serialize_for_csv(-1.5) == -1.5
        assert serialize_for_csv(3) == 3

    def test_dates_keep_their_iso_form(self):
        assert serialize_for_csv(date(2026, 9, 23)) == "2026-09-23"
        assert serialize_for_csv(datetime(2026, 9, 23, 8, 30)) == "2026-09-23T08:30:00"

    def test_json_serialization_is_not_escaped(self):
        # JSON is not executed by a spreadsheet; escaping there would
        # corrupt the value the API returns.
        assert serialize_for_json("=1+1") == "=1+1"
        assert serialize_for_json(decimal.Decimal("1.5")) == 1.5
