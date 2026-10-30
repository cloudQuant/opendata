"""Offline contract tests for the SZSE lifecycle workbook census adapter."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, cast
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

import opendata.pipeline.szse_lifecycle_census_adapter as census_adapter
from opendata.pipeline.szse_lifecycle_census_adapter import (
    SzseLifecycleCensusCapture,
    SzseLifecycleCensusError,
    SzseLifecycleDatasetClass,
    SzseLifecycleDatasetInput,
    build_szse_lifecycle_census,
)
from opendata.pipeline.szse_lifecycle_xlsx_adapter import (
    SzseLifecycleRawCell,
    SzseLifecycleRawRow,
    SzseLifecycleXlsxError,
    SzseLifecycleXlsxResult,
)

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DOC_REL_TYPE = f"{_DOC_REL}/"
_OBSERVED_HEADERS = {
    "current_a": (
        "板块",
        "公司全称",
        "英文名称",
        "注册地址",
        "A股代码",
        "A股简称",
        "A股上市日期",
        "A股总股本",
        "A股流通股本",
        "B股代码",
        "B股 简 称",
        "B股上市日期",
        "B股总股本",
        "B股流通股本",
        "地      区",
        "省    份",
        "城     市",
        "所属行业",
        "公司网址",
        "目前尚未盈利",
        "具有表决权差异安排",
        "具有协议控制架构",
    ),
    "suspended": ("证券代码", "证券简称", "上市日期", "暂停上市日期"),
    "terminated": ("证券代码", "证券简称", "上市日期", "终止上市日期"),
}
_COLUMN_LETTERS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_OBSERVED_AT = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)


def _inline_cell(reference: str, value: str) -> str:
    """Render a source-shaped inline string cell in a tiny workbook fixture."""
    return f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def _workbook(dataset_class: str, values: dict[str, str]) -> bytes:
    """Build one bounded workbook with the parser's observed header contract."""
    headers = _OBSERVED_HEADERS[dataset_class]
    header_cells = "".join(
        _inline_cell(f"{_COLUMN_LETTERS[index]}1", value) for index, value in enumerate(headers)
    )
    data_cells = "".join(_inline_cell(reference, value) for reference, value in values.items())
    worksheet = (
        f'<worksheet xmlns="{_MAIN}"><dimension ref="A1:V2"/>'
        f'<sheetData><row r="1">{header_cells}</row>'
        f'<row r="2">{data_cells}</row></sheetData></worksheet>'
    ).encode()
    workbook = (
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<workbookPr date1904="1"/><sheets><sheet name="Synthetic" sheetId="1" '
        'state="visible" r:id="rId1"/></sheets></workbook>'
    ).encode()
    workbook_relationships = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    ).encode()
    root_relationships = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    ).encode()
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        b'relationships+xml"/>'
        b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        b'officedocument.spreadsheetml.sheet.main+xml"/>'
        b'<Override PartName="/xl/worksheets/sheet1.xml" '
        b'ContentType="application/vnd.openxmlformats-'
        b'officedocument.spreadsheetml.worksheet+xml"/>'
        b"</Types>"
    )
    output = io.BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


class _NoOffsetTimezone(tzinfo):
    """Represent a malformed aware datetime without a UTC offset."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None


class _RaisingTimezone(tzinfo):
    """Raise one selected safe exception while resolving an offset."""

    def __init__(self, error_type: type[Exception]) -> None:
        self.error_type = error_type

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        del dt
        raise self.error_type("PRIVATE_TIMEZONE_DETAIL")

    def dst(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None


class _FailingIterable:
    """Raise while a lazy caller-supplied dataset sequence is consumed."""

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError("PRIVATE_ITERATION_DETAIL")


def _parser_result(
    dataset_class: SzseLifecycleDatasetClass,
    *,
    rows: tuple[SzseLifecycleRawRow, ...] = (),
) -> SzseLifecycleXlsxResult:
    """Build a minimal result value for internal post-init guard tests."""
    headers = _OBSERVED_HEADERS[dataset_class]
    return SzseLifecycleXlsxResult(
        dataset_class=dataset_class,
        dataset_class_binding_status="caller_declared",
        source_id=None,
        source_id_status="unknown",
        source_bytes_sha256="0" * 64,
        date_system="1904",
        headers=headers,
        header_cells=tuple(
            SzseLifecycleRawCell(
                reference=f"{_COLUMN_LETTERS[index]}1",
                column_index=index + 1,
                cell_type="inlineStr",
                raw_value=header,
                text_value=header,
                style_index=None,
            )
            for index, header in enumerate(headers)
        ),
        rows=rows,
        lifecycle_semantics_status="unknown",
        market_scope_status="unknown",
        source_completeness_status="unknown",
        header_contract_status="selector_specific_observed_headers_only",
    )


def test_capture_groups_all_three_szse_workbook_classes_without_completeness_claims() -> None:
    """Keep current, suspended and terminated rows separate with all claims unknown."""
    private_company_name = "PRIVATE-RAW-COMPANY-NAME"
    current_dataset = SzseLifecycleDatasetInput(
        dataset_class="current_a",
        raw_xlsx_body=_workbook(
            "current_a",
            {"E2": "000001", "F2": private_company_name, "G2": "1991-04-03"},
        ),
    )
    assert private_company_name not in repr(current_dataset)
    capture = build_szse_lifecycle_census(
        (
            current_dataset,
            SzseLifecycleDatasetInput(
                dataset_class="suspended",
                raw_xlsx_body=_workbook("suspended", {"A2": "000001", "D2": "2019-05-10"}),
            ),
            SzseLifecycleDatasetInput(
                dataset_class="terminated",
                raw_xlsx_body=_workbook("terminated", {"A2": "000002", "D2": "2024-12-31"}),
            ),
        ),
        observed_at=_OBSERVED_AT,
    )

    assert capture.source_id == "szse.lifecycle.xlsx"
    assert capture.exchange_scope == ("SZ",)
    assert capture.dataset_classes == ("current_a", "suspended", "terminated")
    assert capture.row_count == 3
    assert capture.source_identity_status == "UNKNOWN"
    assert capture.pagination_status == "UNKNOWN"
    assert capture.lifecycle_semantics_status == "UNKNOWN"
    assert capture.market_completeness_status == "UNKNOWN"
    assert capture.historical_coverage_status == "UNKNOWN"
    current_cells = {cell.reference: cell for cell in capture.datasets[0].rows[0].cells}
    terminated_cells = {cell.reference: cell for cell in capture.datasets[2].rows[0].cells}
    assert current_cells["E2"].raw_value == "000001"
    assert current_cells["G2"].raw_value == "1991-04-03"
    assert terminated_cells["A2"].raw_value == "000002"
    assert terminated_cells["D2"].raw_value == "2024-12-31"
    assert "2024-12-31" not in repr(capture)
    assert private_company_name not in repr(capture)
    assert private_company_name not in repr(capture.datasets[0])
    assert private_company_name not in repr(capture.datasets[0].rows[0])


def test_missing_dataset_class_is_not_fabricated_as_an_empty_capture() -> None:
    """Omitted exchange categories stay omitted while overall coverage remains unknown."""
    capture = build_szse_lifecycle_census(
        (
            SzseLifecycleDatasetInput(
                dataset_class="terminated",
                raw_xlsx_body=_workbook("terminated", {"A2": "920001"}),
            ),
        ),
        observed_at=_OBSERVED_AT,
    )

    assert capture.dataset_classes == ("terminated",)
    assert capture.row_count == 1
    assert capture.market_completeness_status == "UNKNOWN"
    assert capture.historical_coverage_status == "UNKNOWN"


def test_capture_cannot_be_constructed_with_caller_fabricated_parser_results() -> None:
    """Only the bounded workbook parser builder can issue capture instances."""
    with pytest.raises(SzseLifecycleCensusError, match="capture_builder_required"):
        SzseLifecycleCensusCapture(
            observed_at=_OBSERVED_AT,
            datasets=(),
        )


def test_duplicate_category_and_naive_timestamp_fail_closed() -> None:
    """Reject ambiguous workbook grouping and timestamps without an offset."""
    body = _workbook("current_a", {"E2": "000001"})
    with pytest.raises(SzseLifecycleCensusError, match="dataset_class_duplicate"):
        build_szse_lifecycle_census(
            (
                SzseLifecycleDatasetInput("current_a", body),
                SzseLifecycleDatasetInput("current_a", body),
            ),
            observed_at=_OBSERVED_AT,
        )

    with pytest.raises(SzseLifecycleCensusError, match="observed_at_invalid"):
        build_szse_lifecycle_census(
            (SzseLifecycleDatasetInput("current_a", body),),
            observed_at=datetime(2026, 10, 30),
        )


@pytest.mark.parametrize(
    ("dataset_class", "expected_code"),
    [
        ("unlisted_category", "dataset_class_invalid"),
        (None, "dataset_class_invalid"),
    ],
)
def test_dataset_input_rejects_unrecognized_or_wrong_type_categories(
    dataset_class: object, expected_code: str
) -> None:
    """Only the three declared SZSE workbook categories are accepted."""
    with pytest.raises(SzseLifecycleCensusError) as raised:
        SzseLifecycleDatasetInput(
            dataset_class=cast("SzseLifecycleDatasetClass", dataset_class),
            raw_xlsx_body=b"synthetic",
        )

    assert raised.value.code == expected_code
    assert str(raised.value) == expected_code
    assert str(dataset_class) not in str(raised.value)


@pytest.mark.parametrize("body", ["PRIVATE_BODY", bytearray(b"PRIVATE_BODY"), None])
def test_dataset_input_rejects_non_bytes_without_echoing_body(body: object) -> None:
    """Reject body type confusion with a fixed diagnostic."""
    with pytest.raises(SzseLifecycleCensusError) as raised:
        SzseLifecycleDatasetInput(
            dataset_class="current_a",
            raw_xlsx_body=cast("bytes", body),
        )

    assert raised.value.code == "body_type_invalid"
    assert "PRIVATE_BODY" not in str(raised.value)
    assert "PRIVATE_BODY" not in repr(raised.value)


@pytest.mark.parametrize(
    "datasets",
    [
        "PRIVATE_DATASET_SEQUENCE",
        b"PRIVATE_DATASET_SEQUENCE",
        bytearray(b"PRIVATE_DATASET_SEQUENCE"),
        {"private": "value"},
        None,
        9,
    ],
)
def test_builder_rejects_non_iterable_or_mapping_containers(datasets: object) -> None:
    """A scalar or mapping cannot masquerade as the ordered workbook list."""
    with pytest.raises(SzseLifecycleCensusError) as raised:
        build_szse_lifecycle_census(
            cast("Iterable[SzseLifecycleDatasetInput]", datasets),
            observed_at=_OBSERVED_AT,
        )

    assert raised.value.code == "datasets_invalid"
    assert "PRIVATE_DATASET_SEQUENCE" not in repr(raised.value)


def test_builder_sanitizes_iteration_failures_and_rejects_bad_items() -> None:
    """A hostile iterator or item fails closed without exposing its details."""
    with pytest.raises(SzseLifecycleCensusError) as iterator_error:
        build_szse_lifecycle_census(
            cast("Iterable[SzseLifecycleDatasetInput]", _FailingIterable()),
            observed_at=_OBSERVED_AT,
        )
    assert iterator_error.value.code == "datasets_invalid"
    assert "PRIVATE_ITERATION_DETAIL" not in str(iterator_error.value)

    with pytest.raises(SzseLifecycleCensusError) as item_error:
        build_szse_lifecycle_census(
            cast("Iterable[SzseLifecycleDatasetInput]", ("PRIVATE_ITEM",)),
            observed_at=_OBSERVED_AT,
        )
    assert item_error.value.code == "dataset_input_invalid"
    assert "PRIVATE_ITEM" not in repr(item_error.value)


def test_builder_rejects_empty_and_overfull_groups_before_parsing() -> None:
    """Require at least one workbook and stop before consuming a fourth item."""
    with pytest.raises(SzseLifecycleCensusError, match="dataset_count_invalid"):
        build_szse_lifecycle_census((), observed_at=_OBSERVED_AT)

    four_items = tuple(SzseLifecycleDatasetInput("current_a", b"not-parsed") for _ in range(4))
    with pytest.raises(SzseLifecycleCensusError, match="dataset_count_invalid"):
        build_szse_lifecycle_census(four_items, observed_at=_OBSERVED_AT)


@pytest.mark.parametrize(
    "observed_at",
    [
        "PRIVATE_TIME_STRING",
        None,
        datetime(2026, 10, 30, tzinfo=_NoOffsetTimezone()),
        datetime(2026, 10, 30, tzinfo=_RaisingTimezone(ValueError)),
        datetime(2026, 10, 30, tzinfo=_RaisingTimezone(OverflowError)),
        datetime(2026, 10, 30, tzinfo=_RaisingTimezone(RuntimeError)),
    ],
)
def test_builder_rejects_invalid_time_shapes_without_echoing_time(
    observed_at: object,
) -> None:
    """Reject non-datetimes and offsets that cannot be safely resolved."""
    with pytest.raises(SzseLifecycleCensusError) as raised:
        build_szse_lifecycle_census((), observed_at=cast("datetime", observed_at))

    assert raised.value.code == "observed_at_invalid"
    assert "PRIVATE_TIME" not in str(raised.value)


@pytest.mark.parametrize("unsafe_code", ["PRIVATE_ERROR_DETAIL", [], None])
def test_unknown_error_codes_fall_back_to_a_fixed_message(unsafe_code: object) -> None:
    """Keep errors safe even if callers pass arbitrary or unhashable codes."""
    error = SzseLifecycleCensusError(cast("str", unsafe_code))

    assert error.code == "datasets_invalid"
    assert str(error) == "datasets_invalid"
    assert "PRIVATE_ERROR_DETAIL" not in repr(error)


def test_capture_post_init_revalidates_private_factory_outputs() -> None:
    """Internal factory output still passes the capture's structural guards."""
    valid_result = _parser_result("current_a")

    with pytest.raises(SzseLifecycleCensusError, match="observed_at_invalid"):
        census_adapter._create_parser_capture(
            observed_at=cast("datetime", None),
            datasets=(valid_result,),
        )
    with pytest.raises(SzseLifecycleCensusError, match="dataset_count_invalid"):
        census_adapter._create_parser_capture(observed_at=_OBSERVED_AT, datasets=())
    with pytest.raises(SzseLifecycleCensusError, match="dataset_result_invalid"):
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=cast("tuple[SzseLifecycleXlsxResult, ...]", (object(),)),
        )
    with pytest.raises(SzseLifecycleCensusError, match="dataset_result_invalid"):
        malformed_rows = replace(valid_result, rows=cast("tuple[SzseLifecycleRawRow, ...]", []))
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(malformed_rows,),
        )
    with pytest.raises(SzseLifecycleCensusError, match="dataset_result_invalid"):
        malformed_class = replace(
            valid_result,
            dataset_class=cast("SzseLifecycleDatasetClass", ["private", "value"]),
        )
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(malformed_class,),
        )
    with pytest.raises(SzseLifecycleCensusError, match="dataset_result_invalid"):
        malformed_row = SzseLifecycleRawRow(
            source_row_number=2,
            cells=cast("tuple[SzseLifecycleRawCell, ...]", []),
        )
        malformed_result = replace(valid_result, rows=(malformed_row,))
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(malformed_result,),
        )
    with pytest.raises(SzseLifecycleCensusError, match="dataset_result_invalid"):
        malformed_cell = SzseLifecycleRawCell(
            reference="A2",
            column_index=2,
            cell_type="inlineStr",
            raw_value="PRIVATE_NESTED_CELL",
            text_value="PRIVATE_NESTED_CELL",
            style_index=None,
        )
        malformed_row = SzseLifecycleRawRow(source_row_number=2, cells=(malformed_cell,))
        malformed_result = replace(valid_result, rows=(malformed_row,))
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(malformed_result,),
        )
    with pytest.raises(SzseLifecycleCensusError, match="dataset_result_invalid"):
        malformed_headers = replace(
            valid_result,
            header_cells=cast("tuple[SzseLifecycleRawCell, ...]", []),
        )
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(malformed_headers,),
        )
    with pytest.raises(SzseLifecycleCensusError, match="dataset_class_duplicate"):
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(valid_result, valid_result),
        )


@pytest.mark.parametrize(
    "observed_at",
    [
        datetime(2026, 10, 30, tzinfo=_NoOffsetTimezone()),
        datetime(2026, 10, 30, tzinfo=_RaisingTimezone(RuntimeError)),
    ],
)
def test_capture_post_init_sanitizes_unresolvable_timezones(observed_at: datetime) -> None:
    """The internal capture guard hides custom timezone implementation errors."""
    with pytest.raises(SzseLifecycleCensusError) as raised:
        census_adapter._create_parser_capture(
            observed_at=observed_at,
            datasets=(_parser_result("current_a"),),
        )

    assert raised.value.code == "observed_at_invalid"
    assert "PRIVATE_TIMEZONE_DETAIL" not in repr(raised.value)


def test_capture_post_init_rejects_aggregate_row_count_over_limit() -> None:
    """Keep even internal factory misuse bounded before any row traversal."""
    oversized_result = _parser_result(
        "current_a",
        rows=(SzseLifecycleRawRow(source_row_number=2, cells=()),) * 150_001,
    )

    with pytest.raises(SzseLifecycleCensusError, match="capture_row_count_exceeded"):
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(oversized_result,),
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_bytes_sha256": "not-a-sha256"},
        {"source_bytes_sha256": "A" * 64},
        {"source_id": "szse"},
        {"source_id_status": "verified"},
        {"dataset_class_binding_status": "verified"},
        {"date_system": "1905"},
        {"lifecycle_semantics_status": "known"},
        {"market_scope_status": "szse"},
        {"source_completeness_status": "complete"},
        {"header_contract_status": "synthetic_contract_only"},
    ],
)
def test_capture_post_init_rejects_malformed_result_metadata(metadata: dict[str, object]) -> None:
    """A private-factory call cannot attach unsupported metadata or digest shapes."""
    malformed_result = replace(_parser_result("current_a"), **metadata)

    with pytest.raises(SzseLifecycleCensusError) as raised:
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(malformed_result,),
        )

    assert raised.value.code == "dataset_result_invalid"
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("reference", "PRIVATE_MALFORMED_REFERENCE"),
        ("column_index", 0),
        ("cell_type", "unsupported"),
        ("raw_value", bytearray(b"private")),
        ("text_value", 7),
        ("style_index", True),
    ],
)
def test_capture_post_init_rejects_malformed_nested_cell_fields(
    field_name: str, bad_value: object
) -> None:
    """A private factory rejects malformed nested cell shape with a fixed code."""
    cell = SzseLifecycleRawCell(
        reference="A2",
        column_index=1,
        cell_type="inlineStr",
        raw_value="safe",
        text_value="safe",
        style_index=None,
    )
    malformed_cell = replace(cell, **{field_name: bad_value})
    malformed_row = SzseLifecycleRawRow(source_row_number=2, cells=(malformed_cell,))
    malformed_result = replace(_parser_result("current_a"), rows=(malformed_row,))

    with pytest.raises(SzseLifecycleCensusError) as raised:
        census_adapter._create_parser_capture(
            observed_at=_OBSERVED_AT,
            datasets=(malformed_result,),
        )

    assert raised.value.code == "dataset_result_invalid"
    assert "PRIVATE_MALFORMED_REFERENCE" not in str(raised.value)


def test_private_factory_does_not_authenticate_parser_provenance() -> None:
    """Parser-shaped caller-created DTOs can pass structure checks, so origin stays unknown."""
    fabricated = _parser_result("current_a")

    capture = census_adapter._create_parser_capture(
        observed_at=_OBSERVED_AT,
        datasets=(fabricated,),
    )

    assert capture.datasets == (fabricated,)
    assert capture.datasets[0].source_id is None
    assert capture.datasets[0].source_id_status == "unknown"
    assert capture.source_identity_status == "UNKNOWN"
    assert capture.pagination_status == "UNKNOWN"
    assert capture.market_completeness_status == "UNKNOWN"


def test_parser_contract_failure_is_propagated_without_falling_back_to_empty() -> None:
    """A bad workbook cannot become an empty census or expose its raw bytes."""
    private_body = b"PRIVATE_RAW_WORKBOOK_CONTENT"
    with pytest.raises(SzseLifecycleXlsxError, match="zip_invalid") as raised:
        build_szse_lifecycle_census(
            (SzseLifecycleDatasetInput("current_a", private_body),),
            observed_at=_OBSERVED_AT,
        )
    assert private_body.decode() not in str(raised.value)
    assert private_body.decode() not in repr(raised.value)
