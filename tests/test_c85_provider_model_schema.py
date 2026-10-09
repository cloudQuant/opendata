"""Branch-level offline tests for the reviewed native DWD/ODS schemas (C85).

Each guard in :mod:`opendata.pipeline.provider_model_schema` protects one
reviewed physical fact: the contract model identity, the codec's physical key,
the collated text-key width, the FRED request-context column, the shared DWD
trace columns, and the DDL renderer's post-render structure. Tests drift that
single fact and assert the rejection (type and message), then assert the
undrifted schema still renders. Nothing here opens a database connection.
"""

from __future__ import annotations

import importlib
import typing
from typing import TYPE_CHECKING

import pytest
from pydantic.fields import FieldInfo

from opendata.data import domains
from opendata.data.models import BlsObservation, EquityHistorical, SeriesObservation
from opendata.data.models.base import ContractModel
from opendata.pipeline import ddl, provider_model_codec
from opendata.pipeline import provider_model_schema as schema

if TYPE_CHECKING:
    from collections.abc import Callable

TEXT_KEY_TYPE = "varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
CONTEXT_TYPE = "varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
BATCH_KEY_TYPE = "varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
DYNAMIC_SUFFIX = ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4;"
ODS_COLUMN_NAMES = (
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
)
REVIEWED_KEYS = {
    "fred_series": (
        "series_id",
        "date",
        "realtime_start",
        "realtime_end",
        "_request_context",
    ),
    "bls_series": ("series_id", "year", "period"),
    "equity_historical": ("symbol", "date"),
}


def _replace_column(
    columns: tuple[ddl.Column, ...], name: str, **changes: object
) -> tuple[ddl.Column, ...]:
    """Return ``columns`` with one column's attributes changed."""
    replaced: list[ddl.Column] = []
    for column in columns:
        fields = {
            "name": column.name,
            "sql_type": column.sql_type,
            "nullable": column.nullable,
            "sql_default": column.sql_default,
        }
        if column.name == name:
            fields.update(changes)
        replaced.append(ddl.Column(**fields))  # type: ignore[arg-type]
    return tuple(replaced)


def _drift_model_columns(
    monkeypatch: pytest.MonkeyPatch, domain: str, mutate: Callable
) -> tuple[ddl.Column, ...]:
    """Drift ``ddl.contract_columns`` and the reviewed expectation together.

    The schema compares the live projection against ``_EXPECTED_MODEL_COLUMNS``,
    so a one-sided change is rejected by the earlier column check; both sides
    have to move before the narrower physical-schema guards are reached.
    """
    real = ddl.contract_columns
    drifted = mutate(tuple(real(domain)))

    def patched(target: str) -> list[ddl.Column]:
        return list(drifted) if target == domain else list(real(target))

    monkeypatch.setattr(ddl, "contract_columns", patched)
    monkeypatch.setattr(schema, "_EXPECTED_MODEL_COLUMNS", {**schema._EXPECTED_MODEL_COLUMNS})
    monkeypatch.setitem(schema._EXPECTED_MODEL_COLUMNS, domain, drifted)
    return drifted


@pytest.mark.parametrize("domain", [None, 7, True, b"fred_series", "fred_search", "equity_quote"])
def test_only_reviewed_storage_domains_are_projected(domain: object) -> None:
    with pytest.raises(ValueError, match="does not support this domain"):
        schema.model_storage_columns(domain)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="does not support this domain"):
        schema.model_dwd_table_ddl(domain)  # type: ignore[arg-type]


def test_reviewed_projection_still_matches_the_codec_and_the_renderer() -> None:
    fred = {column.name: column for column in schema.model_storage_columns("fred_series")}
    bls = {column.name: column for column in schema.model_storage_columns("bls_series")}
    equity = {column.name: column for column in schema.model_storage_columns("equity_historical")}

    assert fred["_request_context"].sql_type == CONTEXT_TYPE
    assert fred["_request_context"].nullable is False
    assert fred["series_id"].sql_type == TEXT_KEY_TYPE
    assert fred["requested_frequency"].nullable is True
    assert bls["period"].sql_type == TEXT_KEY_TYPE
    assert bls["footnotes"].sql_type == "json"
    assert equity["volume"] == ddl.Column("volume", "json", nullable=True)
    assert equity["symbol"].sql_type == TEXT_KEY_TYPE
    for domain, key in REVIEWED_KEYS.items():
        assert provider_model_codec.physical_model_key(domain) == key
        rendered = schema.model_dwd_table_ddl(domain)
        assert rendered.splitlines()[-2] == f"  PRIMARY KEY ({', '.join(f'`{n}`' for n in key)})"
        assert rendered.splitlines()[-1] == DYNAMIC_SUFFIX
        assert "PARTITION BY" not in rendered


@pytest.mark.parametrize(
    ("domain", "model", "field_names"),
    [
        ("fred_series", SeriesObservation, tuple(SeriesObservation.model_fields)),
        ("bls_series", BlsObservation, tuple(BlsObservation.model_fields)),
        ("equity_historical", EquityHistorical, tuple(EquityHistorical.model_fields)),
    ],
)
def test_reviewed_model_identity_and_field_order_are_pinned(
    domain: str, model: type, field_names: tuple
) -> None:
    assert domains.contract_model(domain) is model
    assert tuple(column.name for column in schema._EXPECTED_MODEL_COLUMNS[domain]) == field_names
    projected = tuple(column.name for column in schema.model_storage_columns(domain))
    assert projected[: len(field_names)] == field_names


def test_equity_physical_override_annotation_drift_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert EquityHistorical.model_fields["volume"].annotation == int | float | None
    assert schema.model_storage_columns("equity_historical")[6] == ddl.Column(
        "volume", "json", nullable=True
    )
    monkeypatch.setitem(
        schema._EQUITY_HISTORICAL_PHYSICAL_OVERRIDES,
        "volume",
        (int | None, ddl.Column("volume", "json", nullable=True)),
    )

    with pytest.raises(ValueError, match="equity historical physical override annotation changed"):
        schema._equity_historical_columns(EquityHistorical)
    # The projection guard is raised inside the reviewed-domain check, so the
    # public entry point still fails closed with the domain-level message.
    with pytest.raises(ValueError, match="native DWD domain is not fully reviewed"):
        schema.model_storage_columns("equity_historical")
    with pytest.raises(ValueError, match="native DWD domain is not fully reviewed"):
        schema.model_dwd_table_ddl("equity_historical")


@pytest.mark.parametrize("domain", ["fred_series", "bls_series", "equity_historical"])
def test_contract_model_identity_drift_is_rejected(
    monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    monkeypatch.setattr(schema, "_EXPECTED_MODELS", {**schema._EXPECTED_MODELS})
    monkeypatch.setitem(schema._EXPECTED_MODELS, domain, dict)

    with pytest.raises(ValueError, match="contract model differs from the reviewed"):
        schema.model_storage_columns(domain)


def test_physical_key_drift_from_the_codec_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    assert provider_model_codec.physical_model_key("bls_series") == ("series_id", "year", "period")
    monkeypatch.setattr(schema, "_EXPECTED_PHYSICAL_KEYS", {**schema._EXPECTED_PHYSICAL_KEYS})
    monkeypatch.setitem(schema._EXPECTED_PHYSICAL_KEYS, "bls_series", ("series_id", "year"))

    with pytest.raises(ValueError, match="physical key differs from the reviewed codec"):
        schema.model_storage_columns("bls_series")
    with pytest.raises(ValueError, match="physical key differs from the reviewed codec"):
        schema.model_dwd_table_ddl("bls_series")


def test_reviewed_model_column_set_drift_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    unreviewed = (
        *schema._EXPECTED_MODEL_COLUMNS["bls_series"][:-1],
        ddl.Column("unreviewed", "varchar(255)", nullable=False),
    )
    monkeypatch.setattr(schema, "_EXPECTED_MODEL_COLUMNS", {**schema._EXPECTED_MODEL_COLUMNS})
    monkeypatch.setitem(schema._EXPECTED_MODEL_COLUMNS, "bls_series", unreviewed)

    with pytest.raises(ValueError, match="contract columns differ from the reviewed"):
        schema.model_storage_columns("bls_series")


def test_fred_model_declaring_the_physical_context_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "_request_context" not in tuple(SeriesObservation.model_fields)
    declared = {
        **SeriesObservation.model_fields,
        "_request_context": FieldInfo(annotation=str, required=True),
    }
    monkeypatch.setattr(SeriesObservation, "model_fields", declared)
    monkeypatch.setattr(schema, "_EXPECTED_MODEL_COLUMNS", {**schema._EXPECTED_MODEL_COLUMNS})
    monkeypatch.setitem(
        schema._EXPECTED_MODEL_COLUMNS, "fred_series", tuple(ddl.contract_columns("fred_series"))
    )

    with pytest.raises(ValueError, match="unexpectedly declares the physical request context"):
        schema.model_storage_columns("fred_series")


@pytest.mark.parametrize(
    "context_column",
    [
        ddl.Column("_request_context", "varchar(64)", nullable=False),
        ddl.Column("_request_context", CONTEXT_TYPE, nullable=True),
        ddl.Column("_request_context", "json", nullable=False),
    ],
)
def test_fred_request_context_physical_shape_is_checked(
    monkeypatch: pytest.MonkeyPatch, context_column: ddl.Column
) -> None:
    assert schema.model_storage_columns("fred_series")[-1] == schema._FRED_CONTEXT_COLUMN
    monkeypatch.setattr(schema, "_FRED_CONTEXT_COLUMN", context_column)

    with pytest.raises(ValueError, match="request context differs from the reviewed"):
        schema.model_storage_columns("fred_series")


@pytest.mark.parametrize(
    ("domain", "column_name"),
    [("bls_series", "series_id"), ("bls_series", "period"), ("fred_series", "realtime_start")],
)
def test_text_key_width_drift_is_rejected(
    monkeypatch: pytest.MonkeyPatch, domain: str, column_name: str
) -> None:
    drifted = _drift_model_columns(
        monkeypatch,
        domain,
        lambda columns: _replace_column(columns, column_name, sql_type="varchar(64)"),
    )

    assert {column.name: column.sql_type for column in drifted}[column_name] == "varchar(64)"
    with pytest.raises(ValueError, match="text key width differs from the reviewed schema"):
        schema.model_storage_columns(domain)


def test_key_column_that_becomes_nullable_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    drifted = _drift_model_columns(
        monkeypatch,
        "bls_series",
        lambda columns: _replace_column(columns, "period", nullable=True),
    )

    assert [column.nullable for column in drifted][2] is True
    with pytest.raises(ValueError, match="physical key must use present, non-null columns"):
        schema.model_storage_columns("bls_series")


def test_missing_physical_key_column_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    assert schema.model_storage_columns("fred_series")[-1].name == "_request_context"
    monkeypatch.setattr(
        schema, "_FRED_CONTEXT_COLUMN", ddl.Column("_merged_at", "date", nullable=False)
    )

    with pytest.raises(ValueError, match="physical key must use present, non-null columns"):
        schema.model_storage_columns("fred_series")


def test_fred_requested_frequency_must_stay_nullable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert schema.model_storage_columns("fred_series")[7].nullable is True
    _drift_model_columns(
        monkeypatch,
        "fred_series",
        lambda columns: _replace_column(columns, "requested_frequency", nullable=False),
    )

    with pytest.raises(ValueError, match="requested_frequency must remain nullable"):
        schema.model_storage_columns("fred_series")


def test_model_and_trace_column_collisions_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    assert schema.model_dwd_table_ddl("bls_series").count("`_as_of` date NULL") == 1
    monkeypatch.setattr(
        schema, "_UNKNOWN_PIT_DWD_COLUMN", ddl.Column("series_id", "date", nullable=True)
    )

    with pytest.raises(ValueError, match="duplicate model and trace column names"):
        schema.model_dwd_table_ddl("bls_series")


@pytest.mark.parametrize(
    "rendered",
    [
        "SELECT 1",
        "CREATE TABLE IF NOT EXISTS `dwd_fred_series` (\n) ENGINE=InnoDB;",
        "  PRIMARY KEY (`series_id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
    ],
)
def test_unreviewed_renderer_lines_are_rejected(
    monkeypatch: pytest.MonkeyPatch, rendered: str
) -> None:
    monkeypatch.setattr(ddl, "_table_ddl", lambda *args, **kwargs: rendered)

    with pytest.raises(ValueError, match="renderer output differs from the reviewed native schema"):
        schema.model_dwd_table_ddl("fred_series")


def test_renderer_smuggled_partition_clause_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    smuggled = "varchar(255) DEFAULT CHARSET=utf8mb4 PARTITION BY (`period`)"
    _drift_model_columns(
        monkeypatch,
        "bls_series",
        lambda columns: _replace_column(columns, "api_version", sql_type=smuggled),
    )

    assert schema.model_storage_columns("bls_series")[-1].sql_type == smuggled
    with pytest.raises(ValueError, match="renderer output differs from the reviewed native schema"):
        schema.model_dwd_table_ddl("bls_series")


def test_renderer_without_the_reviewed_suffix_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    real = ddl._table_ddl
    monkeypatch.setattr(ddl, "_table_ddl", lambda *args, **kwargs: real(*args, **kwargs) + "\n")

    with pytest.raises(ValueError, match="shared DDL renderer suffix changed unexpectedly"):
        schema.model_dwd_table_ddl("fred_series")


def test_dynamic_row_format_rewrite_is_structurally_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert schema.model_dwd_table_ddl("fred_series").splitlines()[-1] == DYNAMIC_SUFFIX
    monkeypatch.setattr(schema, "_DYNAMIC_SUFFIX", f"\n{DYNAMIC_SUFFIX}")

    with pytest.raises(ValueError, match="post-render structure check"):
        schema.model_dwd_table_ddl("fred_series")


@pytest.mark.parametrize(
    ("domain", "source"),
    [("fred_series", "bls"), ("bls_series", "fmp"), ("equity_historical", "fred"), (7, "fred")],
)
def test_ods_schema_is_bound_to_one_reviewed_domain_and_source(domain: object, source: str) -> None:
    message = "native ODS schema does not support this domain and source"
    with pytest.raises(ValueError, match=message):
        schema.model_ods_storage_columns(domain, source)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=message):
        schema.model_ods_table_ddl(domain, source)  # type: ignore[arg-type]


@pytest.mark.parametrize("failure", [LookupError, TypeError, ValueError])
@pytest.mark.parametrize(("domain", "source"), [("fred_series", "fred"), ("bls_series", "bls")])
def test_ods_table_lookup_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, domain: str, source: str, failure: type[Exception]
) -> None:
    def raiser(_domain: str, _source: str) -> str:
        raise failure("ods table is not registered")

    monkeypatch.setattr(domains, "ods_table", raiser)
    message = "native ODS domain/source pair is not fully reviewed"

    with pytest.raises(ValueError, match=message):
        schema.model_ods_storage_columns(domain, source)
    with pytest.raises(ValueError, match=message):
        schema.model_ods_table_ddl(domain, source)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda columns: columns[:-1],
        lambda columns: (columns[0], *columns[2:], columns[1]),
        lambda columns: (*columns[:-1], ddl.Column("_payload_hash", "varchar(64)", False)),
        lambda columns: (columns[0], columns[0], *columns[1:]),
    ],
)
def test_ods_capture_column_drift_is_rejected(
    monkeypatch: pytest.MonkeyPatch, mutate: Callable
) -> None:
    monkeypatch.setattr(schema, "_EXPECTED_ODS_COLUMNS", mutate(schema._EXPECTED_ODS_COLUMNS))

    with pytest.raises(ValueError, match="native ODS columns differ from the reviewed capture"):
        schema.model_ods_storage_columns("fred_series", "fred")


def test_ods_batch_key_drift_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    original = schema._EXPECTED_ODS_COLUMNS
    assert schema.model_ods_storage_columns("fred_series", "fred")[0] == ddl.Column(
        "_batch_id", BATCH_KEY_TYPE, nullable=False
    )
    for changes in ({"nullable": True}, {"sql_type": "char(36)"}):
        drifted = _replace_column(original, "_batch_id", **changes)
        monkeypatch.setattr(schema, "_EXPECTED_ODS_COLUMNS", drifted)
        with pytest.raises(ValueError, match="native ODS batch key differs from the reviewed"):
            schema.model_ods_storage_columns("fred_series", "fred")


@pytest.mark.parametrize("name", ["_payload", "_query_sha256", "_raw_row_count"])
def test_ods_capture_columns_must_all_be_required(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    original = schema._EXPECTED_ODS_COLUMNS
    assert all(column.nullable is False for column in original)
    drifted = _replace_column(original, name, nullable=True)
    monkeypatch.setattr(schema, "_EXPECTED_ODS_COLUMNS", drifted)

    with pytest.raises(ValueError, match="capture columns must all be required"):
        schema.model_ods_storage_columns("fred_series", "fred")
    with pytest.raises(ValueError, match="capture columns must all be required"):
        schema.model_ods_table_ddl("bls_series", "bls")


def test_ods_capture_columns_and_ddl_are_exact_and_unpartitioned() -> None:
    columns = schema.model_ods_storage_columns("equity_historical", "fmp")
    rendered = schema.model_ods_table_ddl("equity_historical", "fmp")

    assert tuple(column.name for column in columns) == ODS_COLUMN_NAMES
    assert all(column.nullable is False for column in columns)
    assert rendered.splitlines()[0] == "CREATE TABLE IF NOT EXISTS `ods_equity_historical_fmp` ("
    assert "  PRIMARY KEY (`_batch_id`)" in rendered.splitlines()
    assert "PARTITION BY" not in rendered
    assert rendered.splitlines()[-1] == DYNAMIC_SUFFIX


@pytest.mark.parametrize(
    "rendered", ["SELECT 1", "CREATE TABLE IF NOT EXISTS `x` (\n) ENGINE=InnoDB;"]
)
def test_ods_renderer_lines_are_checked(monkeypatch: pytest.MonkeyPatch, rendered: str) -> None:
    monkeypatch.setattr(ddl, "_table_ddl", lambda *args, **kwargs: rendered)

    with pytest.raises(ValueError, match="differs from the reviewed native ODS schema"):
        schema.model_ods_table_ddl("fred_series", "fred")


def test_ods_renderer_smuggled_partition_clause_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smuggled = "varchar(64) PARTITION BY (`_batch_id`)"
    drifted = _replace_column(schema._EXPECTED_ODS_COLUMNS, "_query_sha256", sql_type=smuggled)
    monkeypatch.setattr(schema, "_EXPECTED_ODS_COLUMNS", drifted)

    assert schema.model_ods_storage_columns("bls_series", "bls")[-1].sql_type == smuggled
    with pytest.raises(ValueError, match="differs from the reviewed native ODS schema"):
        schema.model_ods_table_ddl("bls_series", "bls")


def test_ods_renderer_trailing_newline_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    real = ddl._table_ddl
    monkeypatch.setattr(ddl, "_table_ddl", lambda *args, **kwargs: real(*args, **kwargs) + "\n")

    with pytest.raises(ValueError, match="native ODS DDL failed its post-render structure check"):
        schema.model_ods_table_ddl("fred_series", "fred")


def test_annotation_only_contract_import_is_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not hasattr(schema, "ContractModel")
    monkeypatch.setattr(typing, "TYPE_CHECKING", True)

    reloaded = importlib.reload(schema)
    assert reloaded.ContractModel is ContractModel
    monkeypatch.undo()
    restored = importlib.reload(schema)

    assert restored._EXPECTED_MODELS["bls_series"] is BlsObservation
    assert tuple(column.name for column in restored.model_storage_columns("bls_series"))[0] == (
        "series_id"
    )
    assert restored.model_dwd_table_ddl("bls_series").splitlines()[-1] == DYNAMIC_SUFFIX
