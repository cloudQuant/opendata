"""Counterfact arms for the shared offline engine-contract fixture.

These tests exercise only engine-generated declarations. Benzinga PriceTarget now has a custom
source-specific row adapter, so its independent response and typed-output contract lives in
``tests/test_benzinga_price_target_spec.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

import tests.test_provider_model_contracts as suite
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    ParamSpec,
    RowFilterSpec,
    RowOrderSpec,
)
from opendata.data.providers.econdb.specs import AVAILABLE_INDICATORS


@pytest.fixture(autouse=True)
def _seal_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_json, "_http_get_json", suite.unmeasured_transport)


@pytest.fixture(autouse=True)
def _supply_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    if AVAILABLE_INDICATORS.credential:
        monkeypatch.setenv(AVAILABLE_INDICATORS.credential, "fixture-token-only")


def reading(spec: ModelSpec, source: str, label: str) -> str:
    """Run the suite's own positive case against ``spec`` and report what it said."""
    try:
        case = suite.TestDeclarationIsTheContract()
        case.test_typical_request_normalizes_the_declared_columns(source, spec)
    except Exception as raised:  # the arm IS the error, and it is reported, not swallowed
        line = f"RED {type(raised).__name__}: {str(raised).splitlines()[0]}"
        print(f"[{label}] {line}")
        return line
    print(f"[{label}] GREEN")
    return "GREEN"


def test_unbroken_declarations_are_green() -> None:
    """The baseline: the engine-generated declaration passes the case the arms below can fail."""
    assert reading(AVAILABLE_INDICATORS, "econdb", "baseline econdb") == "GREEN"


def test_descending_order_on_a_column_with_no_descending_cell_is_refused() -> None:
    """``direction="desc"`` needs a strictly descending page; the fixture only has one for dates."""
    broken = dataclasses.replace(
        AVAILABLE_INDICATORS,
        order_by=RowOrderSpec(column="multiplier", direction="desc", limit=None),
    )
    line = reading(broken, "econdb", "arm B desc order on an int column")
    assert "RED AssertionError: AvailableIndicators: order_by sorts 'multiplier' descending" in line
    assert "no descending cell for a 'int' column" in line, line


def test_declared_row_cap_is_judged_against_the_page_size() -> None:
    """``limit=1`` must come back as one row, and the scripted page has two -- a value-level red.

    This tamper is legal as a *declaration*, so nothing in the spec guards should fire. The case
    still catches it because the row count it expects is read off the declaration and compared with
    what the engine actually returns.
    """
    broken = dataclasses.replace(
        AVAILABLE_INDICATORS,
        order_by=RowOrderSpec(column="last_date", direction="desc", limit=1),
    )
    line = reading(broken, "econdb", "arm C order_limit=1 against a two-record page")
    assert line.startswith("RED AssertionError: assert 1 == 2"), line


def test_row_filter_needed_on_an_undeclared_key_is_refused() -> None:
    """A filter whose needle is the caller's parameter is not a cell the fixture can script."""
    broken = dataclasses.replace(
        AVAILABLE_INDICATORS,
        params=(ParamSpec("iso_group", "str", query_key="iso_group"),),
        row_filters=(
            RowFilterSpec(op="not_equals", columns=("iso",), value="", value_param="iso_group"),
        ),
    )
    line = reading(broken, "econdb", "arm D value_param needle on an undeclared key")
    assert "RED AssertionError: AvailableIndicators: its row filter tests the undeclared key 'iso'"
    assert "which rows survive is the caller's" in line, line


def test_wrong_source_key_stays_self_consistent() -> None:
    """MEASURED BLIND SPOT: a ``source_key`` that the real payload never publishes still passes.

    :func:`opendata.data.providers._engine.testing.raw_key` resolves the record key as
    ``column.source_key or column.name`` (testing.py:515), so the body the fixture scripts always
    carries whatever the declaration says to read. Whether the upstream roster really spells that
    column ``short_ticker`` is judged by the package's recorded-header test
    (``tests/test_econdb_available_indicators_spec.py:56`` and the header tamper at ``:295``), not
    here.
    """
    renamed: list[ColumnSpec] = [
        dataclasses.replace(column, source_key="not_the_published_key")
        if column.name == "symbol"
        else column
        for column in AVAILABLE_INDICATORS.columns
    ]
    broken = dataclasses.replace(AVAILABLE_INDICATORS, columns=tuple(renamed))
    assert reading(broken, "econdb", "arm F source_key rename") == "GREEN"
