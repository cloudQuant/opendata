"""Reviewed native DWD schemas for FRED, BLS, and equity history contracts.

This module returns offline schema definitions only; it does not connect to a
database or authorize storage. Generated DDL assumes MySQL >= 8.0.19 and
``innodb_page_size >= 16 KiB`` for the declared key widths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opendata.data import domains
from opendata.data.models import BlsObservation, EquityHistorical, SeriesObservation
from opendata.pipeline import ddl, provider_model_codec

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

_TEXT_PRIMARY_KEY_TYPE = "varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
_FRED_CONTEXT_TYPE = "varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
_RENDERER_SUFFIX = ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
_DYNAMIC_SUFFIX = ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4;"

_EXPECTED_MODELS: dict[str, type[ContractModel]] = {
    "fred_series": SeriesObservation,
    "bls_series": BlsObservation,
    "equity_historical": EquityHistorical,
}

_EXPECTED_PHYSICAL_KEYS: dict[str, tuple[str, ...]] = {
    "fred_series": ("series_id", "date", "realtime_start", "realtime_end", "_request_context"),
    "bls_series": ("series_id", "year", "period"),
    "equity_historical": ("symbol", "date"),
}

_EXPECTED_MODEL_COLUMNS: dict[str, tuple[ddl.Column, ...]] = {
    "fred_series": (
        ddl.Column("series_id", "varchar(255)", nullable=False),
        ddl.Column("date", "date", nullable=False),
        ddl.Column("value", "double", nullable=True),
        ddl.Column("realtime_start", "date", nullable=False),
        ddl.Column("realtime_end", "date", nullable=False),
        ddl.Column("transform_units", "varchar(255)", nullable=False),
        ddl.Column("output_type", "bigint", nullable=False),
        ddl.Column("requested_frequency", "varchar(255)", nullable=True),
        ddl.Column("requested_aggregation_method", "varchar(255)", nullable=False),
    ),
    "bls_series": (
        ddl.Column("series_id", "varchar(255)", nullable=False),
        ddl.Column("year", "bigint", nullable=False),
        ddl.Column("period", "varchar(255)", nullable=False),
        ddl.Column("period_name", "varchar(255)", nullable=False),
        ddl.Column("value", "double", nullable=False),
        ddl.Column("footnotes", "json", nullable=False),
        ddl.Column("latest", "tinyint(1)", nullable=True),
        ddl.Column("preliminary", "tinyint(1)", nullable=False),
        ddl.Column("api_version", "varchar(255)", nullable=False),
    ),
    "equity_historical": (
        ddl.Column("symbol", "varchar(255)", nullable=False),
        ddl.Column("date", "date", nullable=False),
        ddl.Column("open", "double", nullable=False),
        ddl.Column("high", "double", nullable=False),
        ddl.Column("low", "double", nullable=False),
        ddl.Column("close", "double", nullable=False),
        ddl.Column("volume", "json", nullable=True),
        ddl.Column("change", "double", nullable=True),
        ddl.Column("change_percent", "double", nullable=True),
        ddl.Column("vwap", "double", nullable=True),
        ddl.Column("currency", "varchar(255)", nullable=True),
        ddl.Column("currency_semantics", "varchar(255)", nullable=False),
        ddl.Column("volume_unit", "varchar(255)", nullable=True),
        ddl.Column("volume_unit_semantics", "varchar(255)", nullable=False),
        ddl.Column("query_window_scope", "varchar(255)", nullable=False),
        ddl.Column("window_boundary_semantics", "varchar(255)", nullable=False),
        ddl.Column("provider_default_window_semantics", "varchar(255)", nullable=False),
        ddl.Column("close_adjustment_semantics", "varchar(255)", nullable=False),
        ddl.Column("adj_close_provided", "tinyint(1)", nullable=False),
    ),
}

_FRED_CONTEXT_COLUMN = ddl.Column("_request_context", _FRED_CONTEXT_TYPE, nullable=False)
_EXPECTED_DWD_TRACE_COLUMNS = (
    ddl.Column("source", "varchar(32)", nullable=False),
    ddl.Column("_merged_at", "datetime", nullable=False),
    ddl.Column("_diff_flag", "tinyint(1)", nullable=False, sql_default="0"),
    ddl.Column("_as_of", "date", nullable=False),
)
_UNKNOWN_PIT_DWD_COLUMN = ddl.Column("_as_of", "date", nullable=True)
_EQUITY_HISTORICAL_PHYSICAL_OVERRIDES: dict[str, tuple[object, ddl.Column]] = {
    "volume": (
        int | float | None,
        ddl.Column("volume", "json", nullable=True),
    ),
    "currency": (
        type(None),
        ddl.Column("currency", "varchar(255)", nullable=True),
    ),
    "volume_unit": (
        type(None),
        ddl.Column("volume_unit", "varchar(255)", nullable=True),
    ),
}
_ODS_SOURCE_BY_DOMAIN = {
    "fred_series": "fred",
    "bls_series": "bls",
    "equity_historical": "fmp",
}
_ODS_BATCH_ID_TYPE = "varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
_ODS_RENDERER_SUFFIX = ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
_ODS_DYNAMIC_SUFFIX = ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4;"
_EXPECTED_ODS_COLUMNS = (
    ddl.Column("_batch_id", _ODS_BATCH_ID_TYPE, nullable=False),
    ddl.Column("_source", "varchar(32)", nullable=False),
    ddl.Column("_fetched_at", "datetime", nullable=False),
    ddl.Column("_raw_scope", "varchar(32)", nullable=False),
    ddl.Column("_schema_version", "smallint", nullable=False),
    ddl.Column("_raw_row_count", "int", nullable=False),
    ddl.Column("_dwd_row_count", "int", nullable=False),
    ddl.Column("_payload", "longtext", nullable=False),
    ddl.Column("_query_context", "longtext", nullable=False),
    ddl.Column("_payload_sha256", "varchar(64)", nullable=False),
    ddl.Column("_query_sha256", "varchar(64)", nullable=False),
)


def _equity_historical_columns(model: type[ContractModel]) -> tuple[ddl.Column, ...]:
    """Map only EquityHistorical's three unsupported field shapes explicitly."""
    columns: list[ddl.Column] = []
    for name, field in model.model_fields.items():
        override = _EQUITY_HISTORICAL_PHYSICAL_OVERRIDES.get(name)
        if override is not None:
            expected_annotation, column = override
            if field.annotation != expected_annotation:
                raise ValueError("equity historical physical override annotation changed")
            columns.append(column)
            continue

        sql_type = ddl._sql_type(field.annotation, name)
        if name == "symbol" and sql_type == "varchar(64)":
            sql_type = "varchar(255)"
        columns.append(ddl.Column(name, sql_type, nullable=ddl._is_optional(field.annotation)))
    return tuple(columns)


def _checked_storage_schema(domain: str) -> tuple[tuple[ddl.Column, ...], tuple[str, ...]]:
    """Validate one exact production domain and return its columns and key."""
    if not isinstance(domain, str) or domain not in _EXPECTED_MODELS:
        raise ValueError("native DWD schema does not support this domain")

    try:
        # The codec applies the full reviewed storage-semantics guard. Avoid
        # duplicating unrelated DomainSpec presentation and routing metadata.
        physical_key = provider_model_codec.physical_model_key(domain)
        model = domains.contract_model(domain)
        if domain == "equity_historical":
            public_columns = _equity_historical_columns(model)
        else:
            public_columns = tuple(ddl.contract_columns(domain))
    except (LookupError, TypeError, ValueError):
        raise ValueError("native DWD domain is not fully reviewed") from None

    if model is not _EXPECTED_MODELS[domain]:
        raise ValueError("native DWD contract model differs from the reviewed production schema")

    expected_model_columns = _EXPECTED_MODEL_COLUMNS[domain]
    expected_field_names = tuple(column.name for column in expected_model_columns)
    if (
        tuple(model.model_fields) != expected_field_names
        or public_columns != expected_model_columns
    ):
        raise ValueError("native DWD contract columns differ from the reviewed production schema")

    expected_key = _EXPECTED_PHYSICAL_KEYS[domain]
    if physical_key != expected_key:
        raise ValueError("native DWD physical key differs from the reviewed codec")

    storage_columns = list(public_columns)
    if domain == "fred_series":
        if any(column.name == _FRED_CONTEXT_COLUMN.name for column in storage_columns):
            raise ValueError("FRED model unexpectedly declares the physical request context")
        storage_columns.append(_FRED_CONTEXT_COLUMN)

    collated_columns: list[ddl.Column] = []
    for column in storage_columns:
        if column.name == "_request_context":
            if column.sql_type != _FRED_CONTEXT_TYPE or column.nullable:
                raise ValueError("FRED request context differs from the reviewed physical schema")
            collated_columns.append(column)
            continue
        if column.name in physical_key and column.sql_type.startswith("varchar("):
            if column.sql_type != "varchar(255)":
                raise ValueError("native DWD text key width differs from the reviewed schema")
            column = ddl.Column(
                column.name,
                _TEXT_PRIMARY_KEY_TYPE,
                nullable=column.nullable,
                sql_default=column.sql_default,
            )
        collated_columns.append(column)

    names = tuple(column.name for column in collated_columns)
    if len(names) != len(set(names)):
        raise ValueError("native DWD schema has duplicate column names")
    by_name = {column.name: column for column in collated_columns}
    if any(name not in by_name or by_name[name].nullable for name in physical_key):
        raise ValueError("native DWD physical key must use present, non-null columns")
    if domain == "fred_series" and by_name["requested_frequency"].nullable is not True:
        raise ValueError("FRED requested_frequency must remain nullable outside the key")
    return tuple(collated_columns), physical_key


def model_storage_columns(domain: str) -> tuple[ddl.Column, ...]:
    """Return reviewed model columns plus explicit physical-only mappings.

    This function only describes a future storage schema. It does not imply
    database creation, storage permission, or a live DDL acceptance result.
    """
    columns, _ = _checked_storage_schema(domain)
    return columns


def model_dwd_table_ddl(domain: str) -> str:
    """Render an unpartitioned native DWD DDL for reviewed storage domains.

    The output uses the existing DWD renderer and trace columns. It assumes
    MySQL >= 8.0.19 and ``innodb_page_size >= 16 KiB``; no database is opened.
    """
    columns, physical_key = _checked_storage_schema(domain)
    table = domains.dwd_table(domain)
    trace_columns = tuple(ddl.DWD_TRACE_COLUMNS)
    if trace_columns != _EXPECTED_DWD_TRACE_COLUMNS:
        raise ValueError("shared DWD trace columns differ from the reviewed native schema")
    # These sources do not supply a reviewed PIT version date. Preserve the
    # trace column name while leaving its value unknown until a real PIT date
    # is available; local observation time belongs in _merged_at.
    full_columns = (*columns, *trace_columns[:-1], _UNKNOWN_PIT_DWD_COLUMN)
    names = tuple(column.name for column in full_columns)
    if len(names) != len(set(names)):
        raise ValueError("native DWD table has duplicate model and trace column names")

    rendered = ddl._table_ddl(
        table,
        list(full_columns),
        key=physical_key,
        partition_key=None,
        start_year=None,
        years=1,
    )
    expected_key = f"  PRIMARY KEY ({', '.join(f'`{name}`' for name in physical_key)})"
    expected_lines = [
        f"CREATE TABLE IF NOT EXISTS `{table}` (",
        *(f"  {ddl._column_fragment(column)}," for column in full_columns),
        expected_key,
        _RENDERER_SUFFIX,
    ]
    if rendered.splitlines() != expected_lines or "PARTITION BY" in rendered:
        raise ValueError("shared DDL renderer output differs from the reviewed native schema")
    if not rendered.endswith(_RENDERER_SUFFIX):
        raise ValueError("shared DDL renderer suffix changed unexpectedly")

    dynamic_ddl = rendered[: -len(_RENDERER_SUFFIX)] + _DYNAMIC_SUFFIX
    expected_lines[-1] = _DYNAMIC_SUFFIX
    if dynamic_ddl.splitlines() != expected_lines:
        raise ValueError("native DWD DDL failed its post-render structure check")
    return dynamic_ddl


def _checked_ods_schema(domain: str, source: str) -> tuple[str, tuple[ddl.Column, ...]]:
    """Bind an ODS schema to one exact reviewed domain/source pair."""
    if (
        type(domain) is not str
        or type(source) is not str
        or _ODS_SOURCE_BY_DOMAIN.get(domain) != source
    ):
        raise ValueError("native ODS schema does not support this domain and source")
    try:
        _checked_storage_schema(domain)
        table = domains.ods_table(domain, source)
    except (LookupError, TypeError, ValueError):
        raise ValueError("native ODS domain/source pair is not fully reviewed") from None

    columns = _EXPECTED_ODS_COLUMNS
    names = tuple(column.name for column in columns)
    if len(names) != len(set(names)) or names != (
        "_batch_id",
        "_source",
        "_fetched_at",
        "_raw_scope",
        "_schema_version",
        "_raw_row_count",
        "_dwd_row_count",
        "_payload",
        "_query_context",
        "_payload_sha256",
        "_query_sha256",
    ):
        raise ValueError("native ODS columns differ from the reviewed capture schema")
    if columns[0].nullable or columns[0].sql_type != _ODS_BATCH_ID_TYPE:
        raise ValueError("native ODS batch key differs from the reviewed capture schema")
    if any(column.nullable for column in columns):
        raise ValueError("native ODS capture columns must all be required")
    return table, columns


def model_ods_storage_columns(domain: str, source: str) -> tuple[ddl.Column, ...]:
    """Return the exact offline ODS capture columns for a reviewed provider."""
    _, columns = _checked_ods_schema(domain, source)
    return columns


def model_ods_table_ddl(domain: str, source: str) -> str:
    """Render an append-only extractor-capture table without connecting to MySQL."""
    table, columns = _checked_ods_schema(domain, source)
    primary_key = ("_batch_id",)
    rendered = ddl._table_ddl(
        table,
        list(columns),
        key=primary_key,
        partition_key=None,
        start_year=None,
        years=1,
    )
    expected_key = "  PRIMARY KEY (`_batch_id`)"
    expected_lines = [
        f"CREATE TABLE IF NOT EXISTS `{table}` (",
        *(f"  {ddl._column_fragment(column)}," for column in columns),
        expected_key,
        _ODS_RENDERER_SUFFIX,
    ]
    if rendered.splitlines() != expected_lines or "PARTITION BY" in rendered:
        raise ValueError("shared DDL renderer output differs from the reviewed native ODS schema")

    dynamic_ddl = rendered[: -len(_ODS_RENDERER_SUFFIX)] + _ODS_DYNAMIC_SUFFIX
    expected_lines[-1] = _ODS_DYNAMIC_SUFFIX
    if dynamic_ddl.splitlines() != expected_lines:
        raise ValueError("native ODS DDL failed its post-render structure check")
    return dynamic_ddl
