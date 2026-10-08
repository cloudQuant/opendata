"""Offline DWD schema projections for the reviewed FRED and BLS contracts."""

from __future__ import annotations

from datetime import date
from hashlib import sha256

import pytest

from opendata.data import domains
from opendata.data.models import (
    BlsFootnote,
    BlsObservation,
    EquityHistorical,
    SeriesObservation,
)
from opendata.pipeline import ddl, provider_model_codec
from opendata.pipeline.provider_model_schema import model_dwd_table_ddl, model_storage_columns


def _fred_row() -> SeriesObservation:
    return SeriesObservation(
        series_id="CPIAUCSL",
        date=date(2024, 2, 1),
        value=3.1,
        realtime_start=date(2024, 1, 1),
        realtime_end=date(2024, 12, 31),
        transform_units="lin",
        output_type=1,
        requested_frequency=None,
        requested_aggregation_method="avg",
    )


def _bls_row() -> BlsObservation:
    return BlsObservation(
        series_id="LNS14000000",
        year=2025,
        period="M13",
        period_name="Annual average",
        value=4.1,
        footnotes=(BlsFootnote(code="P", text="Preliminary"),),
        latest=None,
        preliminary=False,
        api_version="v2",
    )


def _equity_row() -> EquityHistorical:
    return EquityHistorical.model_validate(
        {
            "symbol": "AAPL",
            "date": date(2026, 1, 2),
            "open": 100.0,
            "high": 103.0,
            "low": 99.5,
            "close": 102.0,
            "volume": 2**53 + 1,
            "query_window_scope": "explicit",
        }
    )


def _assert_unknown_pit_and_nonnull_ddl_key(ddl_text: str, key: tuple[str, ...]) -> None:
    lines = ddl_text.splitlines()
    assert "  `_as_of` date NULL," in lines
    assert not any("`_as_of` date NOT NULL" in line for line in lines)
    assert all(
        any(line.startswith(f"  `{name}` ") and line.endswith(" NOT NULL,") for line in lines)
        for name in key
    )


def test_only_exact_production_domains_and_codec_keys_are_supported() -> None:
    assert domains.require_domain_semantics("fred_series").contract == "SeriesObservation"
    assert domains.contract_model("fred_series") is SeriesObservation
    assert domains.require_domain_semantics("bls_series").contract == "BlsObservation"
    assert domains.contract_model("bls_series") is BlsObservation
    assert provider_model_codec.physical_model_key("fred_series") == (
        "series_id",
        "date",
        "realtime_start",
        "realtime_end",
        "_request_context",
    )
    assert provider_model_codec.physical_model_key("bls_series") == (
        "series_id",
        "year",
        "period",
    )
    for unsupported in ("fred_search", "bls_search", "stock_daily", "equity_quote", "unknown"):
        with pytest.raises(ValueError, match="does not support this domain"):
            model_storage_columns(unsupported)


def test_storage_columns_match_encoded_rows_and_keep_keys_nonnull() -> None:
    fred_columns = model_storage_columns("fred_series")
    bls_columns = model_storage_columns("bls_series")
    fred = provider_model_codec.encode_model_storage_row("fred_series", _fred_row())
    bls = provider_model_codec.encode_model_storage_row("bls_series", _bls_row())

    assert tuple(column.name for column in fred_columns) == tuple(fred)
    assert tuple(column.name for column in bls_columns) == tuple(bls)
    assert fred["requested_frequency"] is None
    assert fred["_request_context"] == '["lin",1,null,"avg"]'
    assert bls["period"] == "M13"
    assert bls["footnotes"] == '[{"code":"P","text":"Preliminary"}]'

    for domain, columns, row in (
        ("fred_series", fred_columns, fred),
        ("bls_series", bls_columns, bls),
    ):
        names = tuple(column.name for column in columns)
        assert len(names) == len(set(names))
        by_name = {column.name: column for column in columns}
        key = provider_model_codec.physical_model_key(domain)
        assert all(name in by_name and not by_name[name].nullable for name in key)
        assert all(row[name] is not None for name in key)

    fred_by_name = {column.name: column for column in fred_columns}
    assert fred_by_name["requested_frequency"].nullable is True
    assert "requested_frequency" not in provider_model_codec.physical_model_key("fred_series")
    assert fred_by_name["_request_context"].sql_type == (
        "varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
    )
    assert fred_by_name["series_id"].sql_type == (
        "varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
    )
    bls_by_name = {column.name: column for column in bls_columns}
    assert bls_by_name["series_id"].sql_type == (
        "varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
    )
    assert bls_by_name["period"].sql_type == (
        "varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
    )
    assert bls_by_name["year"].sql_type == "bigint"
    assert bls_by_name["footnotes"].sql_type == "json"


def test_equity_historical_schema_uses_audited_native_overrides_only() -> None:
    assert domains.contract_model("equity_historical") is EquityHistorical
    assert provider_model_codec.physical_model_key("equity_historical") == ("symbol", "date")
    columns = model_storage_columns("equity_historical")
    encoded = provider_model_codec.encode_model_storage_row("equity_historical", _equity_row())
    assert (
        tuple(column.name for column in columns)
        == tuple(encoded)
        == tuple(EquityHistorical.model_fields)
    )
    by_name = {column.name: column for column in columns}
    assert by_name["symbol"].sql_type == (
        "varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
    )
    assert by_name["volume"] == ddl.Column("volume", "json", nullable=True)
    assert by_name["currency"] == ddl.Column("currency", "varchar(255)", nullable=True)
    assert by_name["volume_unit"] == ddl.Column("volume_unit", "varchar(255)", nullable=True)
    assert encoded["volume"] == '{"kind":"int","value":"9007199254740993"}'
    assert encoded["currency"] is None and encoded["volume_unit"] is None
    assert encoded["currency_semantics"] == "source_unverified"
    assert encoded["volume_unit_semantics"] == "source_unverified"
    with pytest.raises(ValueError, match="unsupported contract field type"):
        ddl.contract_columns("equity_historical")
    with pytest.raises(ValueError, match="does not support this domain"):
        model_storage_columns("equity_quote")
    with pytest.raises(ValueError, match="does not support this domain"):
        model_dwd_table_ddl("equity_quote")


def test_equity_historical_native_ddl_is_exact_unpartitioned_and_not_pit() -> None:
    generated = model_dwd_table_ddl("equity_historical")
    _assert_unknown_pit_and_nonnull_ddl_key(generated, ("symbol", "date"))
    assert (
        generated
        == """CREATE TABLE IF NOT EXISTS `dwd_equity_historical` (
  `symbol` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
  `date` date NOT NULL,
  `open` double NOT NULL,
  `high` double NOT NULL,
  `low` double NOT NULL,
  `close` double NOT NULL,
  `volume` json NULL,
  `change` double NULL,
  `change_percent` double NULL,
  `vwap` double NULL,
  `currency` varchar(255) NULL,
  `currency_semantics` varchar(255) NOT NULL,
  `volume_unit` varchar(255) NULL,
  `volume_unit_semantics` varchar(255) NOT NULL,
  `query_window_scope` varchar(255) NOT NULL,
  `window_boundary_semantics` varchar(255) NOT NULL,
  `provider_default_window_semantics` varchar(255) NOT NULL,
  `close_adjustment_semantics` varchar(255) NOT NULL,
  `adj_close_provided` tinyint(1) NOT NULL,
  `source` varchar(32) NOT NULL,
  `_merged_at` datetime NOT NULL,
  `_diff_flag` tinyint(1) NOT NULL DEFAULT 0,
  `_as_of` date NULL,
  PRIMARY KEY (`symbol`, `date`)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4;"""
    )


def test_fred_native_dwd_ddl_is_exact_and_unpartitioned() -> None:
    generated = model_dwd_table_ddl("fred_series")
    _assert_unknown_pit_and_nonnull_ddl_key(
        generated,
        ("series_id", "date", "realtime_start", "realtime_end", "_request_context"),
    )
    assert (
        generated
        == """CREATE TABLE IF NOT EXISTS `dwd_fred_series` (
  `series_id` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
  `date` date NOT NULL,
  `value` double NULL,
  `realtime_start` date NOT NULL,
  `realtime_end` date NOT NULL,
  `transform_units` varchar(255) NOT NULL,
  `output_type` bigint NOT NULL,
  `requested_frequency` varchar(255) NULL,
  `requested_aggregation_method` varchar(255) NOT NULL,
  `_request_context` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
  `source` varchar(32) NOT NULL,
  `_merged_at` datetime NOT NULL,
  `_diff_flag` tinyint(1) NOT NULL DEFAULT 0,
  `_as_of` date NULL,
  PRIMARY KEY (`series_id`, `date`, `realtime_start`, `realtime_end`, `_request_context`)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4;"""
    )


def test_bls_native_dwd_ddl_preserves_period_and_json_footnotes() -> None:
    generated = model_dwd_table_ddl("bls_series")
    _assert_unknown_pit_and_nonnull_ddl_key(generated, ("series_id", "year", "period"))
    assert (
        generated
        == """CREATE TABLE IF NOT EXISTS `dwd_bls_series` (
  `series_id` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
  `year` bigint NOT NULL,
  `period` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL,
  `period_name` varchar(255) NOT NULL,
  `value` double NOT NULL,
  `footnotes` json NOT NULL,
  `latest` tinyint(1) NULL,
  `preliminary` tinyint(1) NOT NULL,
  `api_version` varchar(255) NOT NULL,
  `source` varchar(32) NOT NULL,
  `_merged_at` datetime NOT NULL,
  `_diff_flag` tinyint(1) NOT NULL DEFAULT 0,
  `_as_of` date NULL,
  PRIMARY KEY (`series_id`, `year`, `period`)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4;"""
    )


def test_legacy_stock_daily_ddl_hash_is_unchanged() -> None:
    legacy_ddl = ddl.dwd_table_ddl(
        "stock_daily",
        key=("symbol", "trade_date"),
        partition_key="trade_date",
        start_year=2025,
        years=2,
    )

    assert sha256(legacy_ddl.encode()).hexdigest() == (
        "5ab3ff53fd61ca7f4cff31e3580a4e5250276198adf7b6f2965dfae8e3d22e9f"
    )


def test_domain_semantic_drift_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    drifted = domains.require_domain_semantics("fred_series").model_copy(
        update={"storage_mode": "append"}
    )
    monkeypatch.setattr(
        provider_model_codec,
        "require_domain_semantics",
        lambda _domain: drifted,
    )

    with pytest.raises(ValueError, match="not fully reviewed"):
        model_storage_columns("fred_series")


def test_non_storage_domain_metadata_changes_do_not_change_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_columns = model_storage_columns("fred_series")
    expected_ddl = model_dwd_table_ddl("fred_series")
    relabeled = domains.require_domain_semantics("fred_series").model_copy(
        update={
            "display_name": "renamed for presentation",
            "rest_path": "new/presentation/path",
            "priority": 17,
        }
    )
    monkeypatch.setattr(provider_model_codec, "require_domain_semantics", lambda _domain: relabeled)

    assert model_storage_columns("fred_series") == expected_columns
    assert model_dwd_table_ddl("fred_series") == expected_ddl


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("natural_key", ("series_id", "date")),
        ("time_field", "realtime_start"),
        ("permissions", ("query", "export")),
    ],
)
def test_storage_domain_semantic_changes_remain_rejected(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    changed_value: object,
) -> None:
    drifted = domains.require_domain_semantics("fred_series").model_copy(
        update={field_name: changed_value}
    )
    monkeypatch.setattr(provider_model_codec, "require_domain_semantics", lambda _domain: drifted)

    with pytest.raises(ValueError, match="not fully reviewed"):
        model_storage_columns("fred_series")


def test_dwd_trace_schema_drift_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ddl, "DWD_TRACE_COLUMNS", (*ddl.DWD_TRACE_COLUMNS[:-1],))

    with pytest.raises(ValueError, match="DWD trace columns differ"):
        model_dwd_table_ddl("fred_series")


@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_missing_or_unreviewed_model_columns_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    actual_columns = ddl.contract_columns
    if change == "missing":

        def changed_columns(domain: str) -> list[ddl.Column]:
            return actual_columns(domain)[:-1]
    else:

        def changed_columns(domain: str) -> list[ddl.Column]:
            return [
                *actual_columns(domain),
                ddl.Column("unreviewed_field", "varchar(255)", nullable=False),
            ]

    monkeypatch.setattr(ddl, "contract_columns", changed_columns)

    with pytest.raises(ValueError, match="contract columns differ"):
        model_storage_columns("fred_series")
