"""REST query building: identifier safety, bind parameters, fail-closed filters.

``build_data_select`` is the only producer of the SQL the data endpoints
run, so every clause and every bind name asserted here is the contract
the warehouse receives. Nothing here opens a connection.
"""

import pathlib
import sys
import types
import typing
from collections.abc import Sequence
from datetime import date

import pytest

from opendata.data.models import Bar
from opendata.pipeline import query as query_module
from opendata.pipeline.query import (
    EXPORT_MAX_ROWS,
    MAX_PAGE_SIZE,
    DataQuery,
    apply_adjust_to_rows,
    build_data_select,
    validate_fields,
)

TODAY = date(2026, 10, 1)
BAR_COLUMNS = ("symbol", "trade_date", "open", "high", "low", "close", "volume", "amount")
BAR_KEY = ("symbol", "trade_date")


def _query(**overrides: object) -> DataQuery:
    """Build a stock-daily query over one week with the defaults intact."""
    values: dict[str, object] = {
        "domain": "stock_daily",
        "start": date(2026, 9, 1),
        "end": date(2026, 9, 30),
    }
    values.update(overrides)
    return DataQuery(**values)  # type: ignore[arg-type]


def _select(
    request: DataQuery,
    *,
    columns: Sequence[str] = BAR_COLUMNS,
    key: Sequence[str] = BAR_KEY,
    **kwargs: object,
) -> tuple[str, dict[str, object]]:
    """Run the builder with the injected ``today`` the tests depend on."""
    return build_data_select(
        request, table="dwd_stock_daily", columns=columns, key=key, today=TODAY, **kwargs
    )  # type: ignore[arg-type]


def test_builds_the_paged_windowed_select() -> None:
    sql, params = _select(_query(page=3, page_size=50))

    assert sql == (
        "SELECT `symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, `amount` "
        "FROM `dwd_stock_daily` "
        "WHERE `trade_date` >= :start AND `trade_date` <= :end "
        "ORDER BY `symbol`, `trade_date` LIMIT :limit OFFSET :offset"
    )
    assert params == {
        "start": date(2026, 9, 1),
        "end": date(2026, 9, 30),
        "limit": 50,
        # 1-based page three of a 50-row page is the 100th zero-based row.
        "offset": 100,
    }


def test_page_size_is_capped_and_the_default_window_is_applied() -> None:
    sql, params = _select(_query(start=None, end=None, page_size=5_000))

    assert "LIMIT :limit OFFSET :offset" in sql
    assert params["limit"] == MAX_PAGE_SIZE
    assert params["end"] == TODAY
    assert params["start"] == date(2025, 10, 1)


def test_bounded_export_offset_overrides_the_page_arithmetic() -> None:
    _, params = _select(_query(page=1, page_size=200), offset=10_000, max_rows=EXPORT_MAX_ROWS)

    assert params == {
        "start": date(2026, 9, 1),
        "end": date(2026, 9, 30),
        "limit": 200,
        "offset": 10_000,
    }


def test_negative_export_offset_is_refused() -> None:
    with pytest.raises(ValueError, match="offset must be >= 0"):
        _select(_query(), offset=-1)


def test_symbols_travel_as_bind_parameters_never_as_literals() -> None:
    attack = "600519' OR 1=1 --"
    sql, params = _select(_query(symbols=("600519", attack)))

    assert "`symbol` IN (:symbol_0, :symbol_1)" in sql
    assert "600519' OR 1=1" not in sql
    assert params["symbol_0"] == "600519"
    assert params["symbol_1"] == attack


def test_every_optional_filter_renders_its_own_clause() -> None:
    sql, params = _select(
        _query(
            symbols=("600519",),
            source="akshare",
            period="2026-03-31",
            report_date=date(2026, 4, 28),
            fields=("close",),
        ),
        columns=(*BAR_COLUMNS, "source", "report_period", "announce_date"),
    )

    assert sql.startswith("SELECT `symbol`, `trade_date`, `close` FROM `dwd_stock_daily` ")
    assert "`symbol` IN (:symbol_0)" in sql
    assert "`source` = :source_filter" in sql
    assert "`report_period` = :period" in sql
    assert "`announce_date` = :report_date" in sql
    assert params["source_filter"] == "akshare"
    # The period is parsed into a date so the column comparison is typed.
    assert params["period"] == date(2026, 3, 31)
    assert params["report_date"] == date(2026, 4, 28)


def test_ods_layer_does_not_filter_on_the_source_column() -> None:
    sql, params = _select(_query(layer="ods", source="akshare"), columns=BAR_COLUMNS)

    assert "`source`" not in sql
    assert "source_filter" not in params


def test_fields_are_whitelisted_and_the_business_key_survives_the_projection() -> None:
    selected = validate_fields(("close", "volume"), available=BAR_COLUMNS, always_include=BAR_KEY)

    assert selected == ["symbol", "trade_date", "close", "volume"]
    assert validate_fields((), available=BAR_COLUMNS) == list(BAR_COLUMNS)


def test_unknown_field_is_reported_with_the_available_columns() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        validate_fields(("nope",), available=BAR_COLUMNS, always_include=BAR_KEY)


def test_column_identifier_that_cannot_be_quoted_is_refused() -> None:
    with pytest.raises(ValueError, match="invalid SQL column identifier"):
        _select(_query(), columns=(*BAR_COLUMNS, "close; DROP TABLE x"))


def test_injected_time_field_must_be_a_table_column() -> None:
    with pytest.raises(ValueError, match=r"time field 'as_of' is not a table column"):
        _select(_query(), columns=BAR_COLUMNS, time_field="as_of")


def test_injected_symbol_field_must_be_a_table_column() -> None:
    with pytest.raises(ValueError, match=r"symbol field 'thscode' is not a table column"):
        _select(_query(symbols=("600519",)), columns=BAR_COLUMNS, symbol_field="thscode")


def test_source_filter_requires_the_dwd_trace_column() -> None:
    with pytest.raises(
        ValueError, match="explicit source filtering requires the DWD source trace column"
    ):
        _select(_query(source="akshare"), columns=BAR_COLUMNS)


def test_report_period_filter_requires_its_column() -> None:
    with pytest.raises(ValueError, match="report-period filter field is not a table column"):
        _select(
            _query(period="2026-03-31"),
            columns=(*BAR_COLUMNS, "report_period", "announce_date"),
            period_field="first_pass_date",
        )


def test_report_date_filter_requires_its_column() -> None:
    with pytest.raises(ValueError, match="report-date filter field is not a table column"):
        _select(
            _query(report_date=date(2026, 4, 28)),
            columns=(*BAR_COLUMNS, "report_period", "announce_date"),
            report_date_field="notice_date",
        )


def test_a_period_that_is_not_an_iso_date_is_refused() -> None:
    with pytest.raises(
        ValueError, match="period must be an ISO report-period date \\(YYYY-MM-DD\\)"
    ) as raised:
        _select(
            _query(period="Q1 2026"),
            columns=(*BAR_COLUMNS, "report_period", "announce_date"),
        )

    assert isinstance(raised.value.__cause__, ValueError)


def test_unknown_adjust_method_is_rejected_before_any_factor_lookup() -> None:
    rows = [{"symbol": "600519", "trade_date": date(2026, 9, 1)}]

    with pytest.raises(ValueError, match=r"unknown adjust method 'raw'; expected one of"):
        apply_adjust_to_rows("stock_daily", rows, method="raw", factors=[])


def test_adjust_none_returns_copies_of_the_read_rows() -> None:
    rows = [{"symbol": "600519", "trade_date": date(2026, 9, 1), "close": 1.0}]

    output = apply_adjust_to_rows("stock_daily", rows, method="none", factors=[])

    assert output == rows
    assert output is not rows
    output[0]["close"] = 99.0
    assert rows[0]["close"] == 1.0


def test_type_checking_only_imports_resolve() -> None:
    """Execute the annotation-only block with the flag forced true.

    ``typing.TYPE_CHECKING`` is ``False`` at runtime, so the guarded
    imports never run; re-executing the source with the flag on proves
    every name the module's annotations reference is importable.
    """
    namespace = _exec_with_type_checking(query_module)

    assert namespace["Sequence"] is Sequence
    assert namespace["Bar"] is Bar


def _exec_with_type_checking(module: object) -> dict[str, object]:
    """Re-execute ``module``'s source with ``TYPE_CHECKING`` true.

    The probe runs under its own throwaway name: dataclass resolution
    looks the defining module up in ``sys.modules``, so the namespace has
    to be registered while the source executes (and removed after).
    """
    path = pathlib.Path(module.__file__)
    source = path.read_text(encoding="utf-8")
    name = f"opendata_c85_probe_{path.stem}"
    probe = types.ModuleType(name)
    probe.__dict__["__file__"] = str(path)
    original = typing.TYPE_CHECKING
    typing.TYPE_CHECKING = True
    sys.modules[name] = probe
    try:
        exec(compile(source, str(path), "exec"), probe.__dict__)
    finally:
        typing.TYPE_CHECKING = original
        sys.modules.pop(name, None)
    return dict(probe.__dict__)
