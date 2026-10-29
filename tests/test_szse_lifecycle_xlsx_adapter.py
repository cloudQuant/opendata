"""Synthetic safety and raw-preservation tests for the SZSE XLSX adapter."""

from __future__ import annotations

import hashlib
import io
import warnings
import zipfile
from typing import TYPE_CHECKING, TypeAlias, cast
from xml.sax.saxutils import escape

import pytest

import opendata.pipeline.szse_lifecycle_xlsx_adapter as adapter
from opendata.pipeline.szse_lifecycle_xlsx_adapter import (
    SZSE_LIFECYCLE_SYNTHETIC_HEADERS,
    SzseLifecycleXlsxError,
    parse_szse_lifecycle_xlsx,
)

if TYPE_CHECKING:
    from collections.abc import Callable

RelationshipMap: TypeAlias = dict[str, tuple[str, str]]
WorkbookCase: TypeAlias = tuple[str, RelationshipMap, dict[str, bytes], str]

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DOC_REL_TYPE = f"{_DOC_REL}/"


def _inline_cell(reference: str, value: str, *, style: int | None = None) -> str:
    """Render one synthetic inline string without creating a spreadsheet file."""
    style_attribute = f' s="{style}"' if style is not None else ""
    return f'<c r="{reference}" t="inlineStr"{style_attribute}><is><t>{escape(value)}</t></is></c>'


def _numeric_cell(reference: str, value: str, *, style: int | None = None) -> str:
    """Render one synthetic numeric cell while retaining its source serial."""
    style_attribute = f' s="{style}"' if style is not None else ""
    return f'<c r="{reference}"{style_attribute}><v>{escape(value)}</v></c>'


def _worksheet(
    *,
    header: tuple[str, ...] = SZSE_LIFECYCLE_SYNTHETIC_HEADERS,
    data_rows: tuple[str, ...] | None = None,
    extra_xml: str = "",
) -> bytes:
    """Build a small in-memory worksheet from the synthetic header contract."""
    header_cells = "".join(
        _inline_cell(f"{column}1", value)
        for column, value in zip(("A", "B", "C", "D"), header, strict=False)
    )
    if data_rows is None:
        data_rows = (
            "".join(
                (
                    _numeric_cell("A2", "000123"),
                    _inline_cell("B2", "SENSITIVE-PRIVATE-COMPANY"),
                    _numeric_cell("C2", "45292", style=3),
                    _inline_cell("D2", "unverified-source-status"),
                )
            ),
        )
    row_xml = "".join(
        content if content.lstrip().startswith("<row ") else f'<row r="{index + 2}">{content}</row>'
        for index, content in enumerate(data_rows)
    )
    xml = (
        f'<worksheet xmlns="{_MAIN}"><dimension ref="A1:D{len(data_rows) + 1}"/>'
        f'<sheetData><row r="1">{header_cells}</row>{row_xml}</sheetData>'
        f"{extra_xml}</worksheet>"
    )
    return xml.encode("utf-8")


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


def _observed_worksheet(
    dataset_class: str,
    *,
    views: str | None = None,
    format_properties: str | None = None,
    margins: str | None = None,
) -> bytes:
    """Build a header-only/synthetic-row workbook using observed structure."""
    headers = _OBSERVED_HEADERS[dataset_class]
    columns = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ"[: len(headers)])
    header = "".join(
        _inline_cell(f"{column}1", value) for column, value in zip(columns, headers, strict=True)
    )
    views = views or '<sheetViews><sheetView workbookViewId="0" tabSelected="0"/></sheetViews>'
    format_properties = format_properties or (
        '<sheetFormatPr baseColWidth="10" defaultRowHeight="15"/>'
    )
    margins = margins or (
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
    )
    data_cells = {
        "current_a": '<c r="A2" t="n"><v>1</v></c><c r="V2" t="n"><v>2</v></c>',
        "suspended": '<c r="A2" t="n"><v>3</v></c>',
        "terminated": '<c r="A2" t="n"><v>4</v></c><c r="D2" t="n"><v>5</v></c>',
    }[dataset_class]
    xml = (
        f'<worksheet xmlns="{_MAIN}"><dimension ref="A1"/>'
        f"{views}{format_properties}"
        f'<sheetData><row r="1">{header}</row><row r="2">{data_cells}</row></sheetData>'
        f"{margins}"
        "</worksheet>"
    )
    return xml.encode("utf-8")


def _package(
    *,
    worksheet: bytes | None = None,
    workbook: bytes | None = None,
    workbook_relationships: bytes | None = None,
    root_relationships: bytes | None = None,
    content_type_xml: bytes | None = None,
    extra_members: tuple[tuple[str, bytes], ...] = (),
    omit_members: tuple[str, ...] = (),
    compression_overrides: dict[str, int] | None = None,
    duplicate_member: bool = False,
) -> bytes:
    """Build a minimal in-memory OOXML package for offline parser tests."""
    sheet = worksheet if worksheet is not None else _worksheet()
    book = (
        workbook
        or (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            '<workbookPr date1904="1"/><sheets><sheet name="Synthetic" sheetId="1" '
            'state="visible" r:id="rId1"/></sheets></workbook>'
        ).encode()
    )
    book_rels = (
        workbook_relationships
        or (
            f'<Relationships xmlns="{_PACKAGE_REL}">'
            f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        ).encode()
    )
    root_rels = (
        root_relationships
        or (
            f'<Relationships xmlns="{_PACKAGE_REL}">'
            f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'
        ).encode()
    )
    package_content_types = content_type_xml or (
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
    members = [
        ("[Content_Types].xml", package_content_types),
        ("_rels/.rels", root_rels),
        ("xl/workbook.xml", book),
        ("xl/_rels/workbook.xml.rels", book_rels),
        ("xl/worksheets/sheet1.xml", sheet),
        *extra_members,
    ]
    members = [(name, content) for name, content in members if name not in omit_members]
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members:
                archive.writestr(
                    name,
                    content,
                    compress_type=(compression_overrides or {}).get(name, zipfile.ZIP_DEFLATED),
                )
            if duplicate_member:
                archive.writestr("xl/workbook.xml", book)
    return output.getvalue()


def _error_code(body: bytes, dataset_class: str = "current_a") -> str:
    """Return the fixed public error code for one bounded parser rejection."""
    with pytest.raises(SzseLifecycleXlsxError) as caught:
        parse_szse_lifecycle_xlsx(body, dataset_class=dataset_class)  # type: ignore[arg-type]
    return caught.value.code


def _element(xml: str) -> adapter.Element:
    """Parse one tiny synthetic XML element for focused helper checks."""
    return cast("adapter.Element", adapter.SafeET.fromstring(xml.encode()))


def _cell_element(xml: str) -> adapter.Element:
    """Parse one standalone cell using the spreadsheet namespace expected by the parser."""
    if xml == "<c/>":
        xml = f'<c xmlns="{_MAIN}"/>'
    else:
        xml = xml.replace("<c ", f'<c xmlns="{_MAIN}" ', 1)
    return cast("adapter.Element", adapter.SafeET.fromstring(xml.encode()))


def _error_code_for(call: Callable[[], object]) -> str:
    """Return the fixed code for a focused helper rejection."""
    with pytest.raises(SzseLifecycleXlsxError) as caught:
        call()
    return caught.value.code


def test_parser_preserves_native_cells_and_explicit_unknown_states() -> None:
    """Keep the numeric date serial and all source-meaning claims unresolved."""
    body = _package()
    result = parse_szse_lifecycle_xlsx(body, dataset_class="current_a")

    assert result.dataset_class == "current_a"
    assert result.dataset_class_binding_status == "caller_declared"
    assert result.source_id is None
    assert result.source_id_status == "unknown"
    assert result.lifecycle_semantics_status == "unknown"
    assert result.market_scope_status == "unknown"
    assert result.source_completeness_status == "unknown"
    assert result.header_contract_status == "synthetic_contract_only"
    assert result.headers == SZSE_LIFECYCLE_SYNTHETIC_HEADERS
    assert tuple(cell.text_value for cell in result.header_cells) == result.headers
    assert tuple(cell.cell_type for cell in result.header_cells) == ("inlineStr",) * 4
    assert result.date_system == "1904"
    assert result.source_bytes_sha256 == hashlib.sha256(body).hexdigest()
    assert len(result.rows) == 1
    date_cell = result.rows[0].cells[2]
    assert date_cell.reference == "C2"
    assert date_cell.cell_type == "n"
    assert date_cell.raw_value == "45292"
    assert date_cell.text_value == "45292"
    assert date_cell.style_index == 3
    assert result.rows[0].cells[0].raw_value == "000123"
    assert "SENSITIVE-PRIVATE-COMPANY" not in repr(result)
    assert "SENSITIVE-PRIVATE-COMPANY" not in repr(result.rows[0])
    assert "SENSITIVE-PRIVATE-COMPANY" not in repr(result.rows[0].cells[1])


@pytest.mark.parametrize("dataset_class", ["current_a", "suspended", "terminated"])
def test_dataset_category_is_required_as_a_caller_declaration(dataset_class: str) -> None:
    """Accept only the three explicit dataset labels and retain the chosen label."""
    result = parse_szse_lifecycle_xlsx(_package(), dataset_class=dataset_class)  # type: ignore[arg-type]

    assert result.dataset_class == dataset_class
    assert result.dataset_class_binding_status == "caller_declared"
    assert result.lifecycle_semantics_status == "unknown"


@pytest.mark.parametrize("dataset_class", ["current_a", "suspended", "terminated"])
def test_observed_selector_headers_preserve_raw_cells_without_assigning_meaning(
    dataset_class: str,
) -> None:
    """Accept only the observed selector header and keep every semantic claim unknown."""
    body = _package(worksheet=_observed_worksheet(dataset_class))
    result = parse_szse_lifecycle_xlsx(body, dataset_class=dataset_class)  # type: ignore[arg-type]

    assert result.headers == _OBSERVED_HEADERS[dataset_class]
    assert result.header_contract_status == "selector_specific_observed_headers_only"
    assert result.dataset_class == dataset_class
    assert result.dataset_class_binding_status == "caller_declared"
    assert result.source_id is None
    assert result.source_id_status == "unknown"
    assert result.lifecycle_semantics_status == "unknown"
    assert result.market_scope_status == "unknown"
    assert result.source_completeness_status == "unknown"
    assert tuple(cell.column_index for cell in result.header_cells) == tuple(
        range(1, len(_OBSERVED_HEADERS[dataset_class]) + 1)
    )
    assert result.rows[0].cells[0].raw_value in {"1", "3", "4"}
    assert all(cell.cell_type == "n" for row in result.rows for cell in row.cells)


@pytest.mark.parametrize(
    ("source_class", "selected_class"),
    [("current_a", "suspended"), ("suspended", "terminated"), ("terminated", "suspended")],
)
def test_observed_headers_are_bound_to_the_selected_parser_contract(
    source_class: str, selected_class: str
) -> None:
    """Do not accept a different observed table just because its header is known."""
    body = _package(worksheet=_observed_worksheet(source_class))
    assert _error_code(body, dataset_class=selected_class) == "header_contract_mismatch"


@pytest.mark.parametrize(
    "layout",
    [
        {"views": '<sheetViews mode="unsafe"><sheetView workbookViewId="0"/></sheetViews>'},
        {
            "views": (
                '<sheetViews><sheetView workbookViewId="0"><selection/></sheetView></sheetViews>'
            )
        },
        {"views": '<sheetViews><sheetView workbookViewId="not-an-index"/></sheetViews>'},
        {
            "format_properties": (
                '<sheetFormatPr baseColWidth="10" defaultRowHeight="15" customHeight="1"/>'
            )
        },
        {"format_properties": ('<sheetFormatPr baseColWidth="256" defaultRowHeight="15"/>')},
        {
            "margins": (
                '<pageMargins left="NaN" right="0.7" top="0.75" bottom="0.75" '
                'header="0.3" footer="0.3"/>'
            )
        },
        {
            "margins": (
                '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" '
                'header="0.3" footer="0.3" extra="1"/>'
            )
        },
    ],
)
def test_observed_layout_rejects_unknown_or_invalid_display_metadata(
    layout: dict[str, str],
) -> None:
    """Keep accepted workbook metadata limited to bounded inert display nodes."""
    body = _package(worksheet=_observed_worksheet("terminated", **layout))
    assert _error_code(body, dataset_class="terminated") == "sheet_unsupported"


def test_shared_strings_and_iso_date_cells_are_retained_without_date_coercion() -> None:
    """Decode shared header text while preserving an ISO date cell verbatim."""
    strings = (
        f'<sst xmlns="{_MAIN}" count="4" uniqueCount="4">'
        + "".join(f"<si><t>{escape(value)}</t></si>" for value in SZSE_LIFECYCLE_SYNTHETIC_HEADERS)
        + "</sst>"
    ).encode("utf-8")
    header_cells = "".join(
        f'<c r="{column}1" t="s"><v>{index}</v></c>' for index, column in enumerate("ABCD")
    )
    row = (
        '<row r="2"><c r="A2" t="str"><v>000777</v></c>'
        '<c r="B2" t="inlineStr"><is><r><t>Issuer</t></r></is></c>'
        '<c r="C2" t="d" s="2"><v>2025-01-03</v></c>'
        '<c r="D2" t="b"><v>1</v></c></row>'
    )
    sheet = (
        f'<worksheet xmlns="{_MAIN}"><sheetData><row r="1">{header_cells}</row>'
        f"{row}</sheetData></worksheet>"
    ).encode()
    book_rels = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{_DOC_REL_TYPE}sharedStrings" '
        'Target="sharedStrings.xml"/>'
        "</Relationships>"
    ).encode()
    body = _package(
        worksheet=sheet,
        workbook_relationships=book_rels,
        extra_members=(("xl/sharedStrings.xml", strings),),
    )

    result = parse_szse_lifecycle_xlsx(body, dataset_class="suspended")

    assert result.headers == SZSE_LIFECYCLE_SYNTHETIC_HEADERS
    assert tuple(cell.raw_value for cell in result.header_cells) == ("0", "1", "2", "3")
    assert tuple(cell.cell_type for cell in result.header_cells) == ("s",) * 4
    assert result.rows[0].cells[2].cell_type == "d"
    assert result.rows[0].cells[2].raw_value == "2025-01-03"
    assert result.rows[0].cells[2].style_index == 2
    assert result.rows[0].cells[3].cell_type == "b"
    assert result.rows[0].cells[3].raw_value == "1"


def test_rejects_unknown_header_and_unknown_dataset_class_without_echoing_values() -> None:
    """Reject header drift and invalid labels with fixed, value-free errors."""
    unknown_header = _worksheet(header=("unknown-secret", *SZSE_LIFECYCLE_SYNTHETIC_HEADERS[1:]))
    assert _error_code(_package(worksheet=unknown_header)) == "header_contract_mismatch"
    assert _error_code(_package(), dataset_class="all_a_shares") == "dataset_class_invalid"
    error = SzseLifecycleXlsxError("SENSITIVE-PRIVATE-COMPANY")
    assert str(error) == "xlsx_parse_failed"
    assert "SENSITIVE-PRIVATE-COMPANY" not in repr(error)


@pytest.mark.parametrize(
    ("worksheet", "expected_code"),
    [
        (_worksheet(extra_xml="<mergeCells/>"), "sheet_unsupported"),
        (_worksheet(extra_xml="<extLst/>"), "sheet_unsupported"),
        (
            _worksheet(
                extra_xml=(
                    '<sheetData><row r="3">'
                    '<c r="A3" t="inlineStr"><is><t>SECOND-CONTAINER</t></is></c>'
                    "</row></sheetData>"
                )
            ),
            "sheet_unsupported",
        ),
        (_worksheet(extra_xml='<dimension ref="A1"/>'), "sheet_unsupported"),
        (
            _worksheet(data_rows=('<c r="A2" t="n"><f>1+1</f><v>2</v></c>',)),
            "xml_unsafe_or_invalid",
        ),
        (
            _worksheet(data_rows=('<c r="A2" t="e"><v>#VALUE!</v></c>',)),
            "cell_value_invalid",
        ),
        (
            _worksheet(
                data_rows=('<c r="A2" t="n"><v>45292</v></c><c r="A2" t="n"><v>45293</v></c>',)
            ),
            "cell_value_invalid",
        ),
        (_worksheet(data_rows=('<c r="E2" t="n"><v>1</v></c>',)), "column_limit_exceeded"),
        (
            _worksheet(data_rows=('<row r="2" hidden="1"><c r="A2" t="n"><v>1</v></c></row>',)),
            "sheet_unsupported",
        ),
    ],
)
def test_rejects_unsupported_sheet_and_cell_content(worksheet: bytes, expected_code: str) -> None:
    """Fail closed for formulas, unsupported worksheet structure, and bad cells."""
    assert _error_code(_package(worksheet=worksheet)) == expected_code


def test_rejects_dtd_entities_and_malformed_xml_before_tree_parsing() -> None:
    """Reject entity-capable XML and malformed XML with one safe code."""
    dtd_sheet = (
        f'<!DOCTYPE worksheet [<!ENTITY secret "SENSITIVE-PRIVATE-COMPANY">]>'
        f'<worksheet xmlns="{_MAIN}"><sheetData/></worksheet>'
    ).encode()
    malformed_sheet = f'<worksheet xmlns="{_MAIN}"><sheetData>'.encode()

    assert _error_code(_package(worksheet=dtd_sheet)) == "xml_unsafe_or_invalid"
    assert _error_code(_package(worksheet=malformed_sheet)) == "xml_unsafe_or_invalid"


def test_rejects_external_relationship_and_macro_content_type() -> None:
    """Disallow external targets and macro-enabled package declarations."""
    external_relationships = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}officeDocument" '
        'Target="https://example.invalid/book.xml" TargetMode="External"/>'
        "</Relationships>"
    ).encode()
    external_body = _package(root_relationships=external_relationships)
    macro_xml = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.ms-excel.'
        b'sheet.macroEnabled.main+xml"/>'
        b"</Types>"
    )
    macro_body = _package(extra_members=(("docProps/custom.xml", macro_xml),))

    assert _error_code(external_body) == "relationship_rejected"
    assert _error_code(macro_body) == "macro_or_external_content_rejected"


def test_rejects_path_traversal_duplicate_members_and_corrupt_archives() -> None:
    """Check ZIP member names, duplicate paths, and malformed container bytes."""
    traversal = _package(extra_members=(("xl/../evil.xml", b"<x/>"),))
    duplicate = _package(duplicate_member=True)

    assert _error_code(traversal) == "zip_member_path_invalid"
    assert _error_code(duplicate) == "zip_member_duplicate"
    assert _error_code(b"not a zip archive") == "zip_invalid"


def test_rejects_large_expansion_ratio_before_xml_parsing() -> None:
    """Apply the declared compression-ratio limit before decompression."""
    highly_compressible = b"<x>" + (b"A" * 512_000) + b"</x>"
    body = _package(extra_members=(("docProps/custom.xml", highly_compressible),))

    assert _error_code(body) == "zip_ratio_too_large"


def test_rejects_input_and_archive_budgets_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforce total body, entry-count, member, and expansion budgets."""
    valid_body = _package()
    with pytest.raises(SzseLifecycleXlsxError, match="body_type_invalid"):
        parse_szse_lifecycle_xlsx(object(), dataset_class="current_a")  # type: ignore[arg-type]

    monkeypatch.setattr(adapter, "_MAX_BODY_BYTES", 1)
    assert _error_code(valid_body) == "body_too_large"
    monkeypatch.setattr(adapter, "_MAX_BODY_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(adapter, "_MAX_MEMBERS", 2)
    assert _error_code(valid_body) == "zip_member_count_invalid"
    monkeypatch.setattr(adapter, "_MAX_MEMBERS", 64)
    monkeypatch.setattr(adapter, "_MAX_MEMBER_BYTES", 10)
    assert _error_code(valid_body) == "zip_member_too_large"
    monkeypatch.setattr(adapter, "_MAX_MEMBER_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(adapter, "_MAX_TOTAL_UNCOMPRESSED_BYTES", 10)
    assert _error_code(valid_body) == "zip_expanded_too_large"


def test_rejects_unknown_package_members_and_unrelated_sheets() -> None:
    """Do not silently ignore OOXML parts outside the single selected sheet."""
    unsupported = _package(extra_members=(("xl/externalLinks/externalLink1.xml", b"<x/>"),))
    extra_sheet = _package(extra_members=(("xl/worksheets/sheet2.xml", _worksheet()),))

    assert _error_code(unsupported) == "package_member_unsupported"
    assert _error_code(extra_sheet) == "sheet_unsupported"


def test_rejects_multiple_or_hidden_sheets_and_bad_relationship_targets() -> None:
    """Reject ambiguous sheet selection and path-like package references."""
    two_sheets = (
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}"><sheets>'
        '<sheet name="One" sheetId="1" r:id="rId1"/>'
        '<sheet name="Two" sheetId="2" r:id="rId2"/>'
        "</sheets></workbook>"
    ).encode()
    hidden_sheet = (
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}"><sheets>'
        '<sheet name="Hidden" sheetId="1" state="hidden" r:id="rId1"/>'
        "</sheets></workbook>"
    ).encode()
    duplicate_sheet_containers = (
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<sheets><sheet name="One" sheetId="1" r:id="rId1"/></sheets>'
        '<sheets><sheet name="Two" sheetId="2" r:id="rId1"/></sheets>'
        "</workbook>"
    ).encode()
    duplicate_workbook_properties = (
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<workbookPr date1904="0"/><workbookPr date1904="1"/>'
        '<sheets><sheet name="One" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    ).encode()
    traversal_rels = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}worksheet" '
        'Target="../outside.xml"/>'
        "</Relationships>"
    ).encode()

    assert _error_code(_package(workbook=two_sheets)) == "sheet_unsupported"
    assert _error_code(_package(workbook=hidden_sheet)) == "sheet_unsupported"
    assert _error_code(_package(workbook=duplicate_sheet_containers)) == "workbook_unsupported"
    assert _error_code(_package(workbook=duplicate_workbook_properties)) == "workbook_unsupported"
    assert _error_code(_package(workbook_relationships=traversal_rels)) == "relationship_rejected"


def test_rejects_bad_shared_string_indices_and_oversized_source_values() -> None:
    """Bound source text and validate indexes before returning raw cells."""
    shared_sheet = (
        f'<worksheet xmlns="{_MAIN}"><sheetData><row r="1">'
        + "".join(
            f'<c r="{column}1" t="s"><v>{index if index < 3 else 4}</v></c>'
            for index, column in enumerate("ABCD")
        )
        + "</row></sheetData></worksheet>"
    ).encode("utf-8")
    shared_rels = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{_DOC_REL_TYPE}sharedStrings" '
        'Target="sharedStrings.xml"/>'
        "</Relationships>"
    ).encode()
    four_strings = (
        f'<sst xmlns="{_MAIN}">'
        + "".join(f"<si><t>{escape(value)}</t></si>" for value in SZSE_LIFECYCLE_SYNTHETIC_HEADERS)
        + "</sst>"
    ).encode("utf-8")
    bad_index = _package(
        worksheet=shared_sheet,
        workbook_relationships=shared_rels,
        extra_members=(("xl/sharedStrings.xml", four_strings),),
    )
    long_value = "".join(hashlib.sha256(str(index).encode()).hexdigest() for index in range(65))[
        :4097
    ]
    huge_row = _worksheet(
        data_rows=(
            "".join(
                (
                    _inline_cell("A2", long_value),
                    _inline_cell("B2", "Name"),
                    _inline_cell("C2", "date"),
                    _inline_cell("D2", "status"),
                )
            ),
        )
    )

    assert _error_code(bad_index) == "cell_value_invalid"
    assert _error_code(_package(worksheet=huge_row)) == "cell_value_invalid"


def test_sanitizes_unexpected_parser_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize expected and unexpected internal exceptions without leaking details."""

    def raise_os_error(_body: bytes) -> dict[str, bytes]:
        raise OSError("SENSITIVE-PRIVATE-COMPANY")

    def raise_unexpected(_body: bytes) -> dict[str, bytes]:
        raise RuntimeError("SENSITIVE-PRIVATE-COMPANY")

    monkeypatch.setattr(adapter, "_read_bounded_package", raise_os_error)
    expected_error = _error_code(_package())
    assert expected_error == "xlsx_parse_failed"

    monkeypatch.setattr(adapter, "_read_bounded_package", raise_unexpected)
    unexpected_error = _error_code(_package())
    assert unexpected_error == "xlsx_parse_failed"

    def raise_missing_part(_body: bytes) -> dict[str, bytes]:
        raise KeyError("SENSITIVE-PRIVATE-COMPANY")

    monkeypatch.setattr(adapter, "_read_bounded_package", raise_missing_part)
    assert _error_code(_package()) == "xlsx_parse_failed"


def test_rejects_missing_required_package_parts_and_unsupported_compression() -> None:
    """Reject incomplete OOXML packages and compression outside the allowlist."""
    missing_workbook = _package(omit_members=("xl/workbook.xml",))
    bzip_member = _package(
        extra_members=(("docProps/custom.xml", b"<custom/>"),),
        compression_overrides={"docProps/custom.xml": zipfile.ZIP_BZIP2},
    )
    macro_member = _package(extra_members=(("xl/vbaProject.bin", b"macro"),))
    empty_zip = io.BytesIO()
    with zipfile.ZipFile(empty_zip, mode="w"):
        pass

    assert _error_code(missing_workbook) == "package_structure_invalid"
    assert _error_code(bzip_member) == "zip_member_unsupported"
    assert _error_code(macro_member) == "macro_or_external_content_rejected"
    assert _error_code(empty_zip.getvalue()) == "zip_member_count_invalid"


def test_rejects_declared_member_size_over_cap_before_opening() -> None:
    """Reject an oversized directory entry before attempting any member read."""
    info = zipfile.ZipInfo("xl/workbook.xml")
    info.file_size = adapter._MAX_MEMBER_BYTES + 1
    info.compress_size = 1

    class MetadataOnlyArchive:
        def __init__(self, _source: io.BytesIO, *, mode: str) -> None:
            assert mode == "r"

        def infolist(self) -> list[zipfile.ZipInfo]:
            return [info]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(adapter.zipfile, "ZipFile", MetadataOnlyArchive)
        assert (
            _error_code_for(lambda: adapter._read_bounded_package(b"synthetic"))
            == "zip_member_too_large"
        )


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("flag_bits", 0x1, "zip_member_encrypted"),
        ("compress_type", zipfile.ZIP_BZIP2, "zip_member_unsupported"),
        ("file_size", -1, "zip_member_unsupported"),
        ("compress_size", -1, "zip_member_unsupported"),
    ],
)
def test_rejects_unsafe_zip_metadata_before_opening_members(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int, expected_code: str
) -> None:
    """Reject encryption, unsupported methods, and impossible ZIP sizes."""
    info = zipfile.ZipInfo("xl/workbook.xml")
    setattr(info, field, value)

    class MetadataOnlyArchive:
        def __init__(self, _source: io.BytesIO, *, mode: str) -> None:
            assert mode == "r"

        def infolist(self) -> list[zipfile.ZipInfo]:
            return [info]

    monkeypatch.setattr(adapter.zipfile, "ZipFile", MetadataOnlyArchive)
    code = _error_code_for(lambda: adapter._read_bounded_package(b"synthetic"))

    assert code == expected_code


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "xl\x00bad.xml",
        "xl\\bad.xml",
        "/xl/file.xml",
        "~xl/file.xml",
        "C:/x.xml",
        "xl//x.xml",
        "xl/./x.xml",
        "xl/../x.xml",
    ],
)
def test_rejects_unsafe_member_names(name: str | None) -> None:
    """Cover package member aliases and traversal forms explicitly."""
    assert not adapter._safe_member_name(name)  # type: ignore[arg-type]


def test_classifies_only_supported_package_members_and_xml_parts() -> None:
    """Recognize worksheet/theme entries while classifying unknown payloads safely."""
    assert adapter._allowed_member("xl/worksheets/sheet1.xml")
    assert adapter._allowed_member("xl/theme/theme2.xml")
    for name in (
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/sharedStrings.xml",
        "xl/styles.xml",
        "docProps/app.xml",
        "docProps/core.xml",
        "docProps/custom.xml",
    ):
        assert adapter._allowed_member(name)
    assert not adapter._allowed_member("xl/theme/theme0.xml")
    assert not adapter._allowed_member("xl/unlisted.xml")
    assert adapter._is_xml_member("xl/workbook.xml")
    assert adapter._is_xml_member("xl/_rels/workbook.xml.rels")
    assert not adapter._is_xml_member("docProps/data.json")


def test_zip_member_reader_enforces_runtime_size_and_read_integrity() -> None:
    """Enforce the read-time cap even when ZIP metadata and bytes disagree."""

    class StreamArchive:
        def __init__(self, data: bytes | None = None, *, fail: bool = False) -> None:
            self._data = data or b""
            self._fail = fail

        def open(self, _info: zipfile.ZipInfo, *, mode: str) -> io.BytesIO:
            assert mode == "r"
            if self._fail:
                raise RuntimeError("SENSITIVE-PRIVATE-COMPANY")
            return io.BytesIO(self._data)

    info = zipfile.ZipInfo("xl/workbook.xml")
    info.file_size = 2
    with pytest.raises(SzseLifecycleXlsxError) as too_small:
        adapter._read_member_bounded(StreamArchive(b"three"), info)  # type: ignore[arg-type]
    assert too_small.value.code == "zip_member_too_large"

    info.file_size = 3
    with pytest.raises(SzseLifecycleXlsxError) as truncated:
        adapter._read_member_bounded(StreamArchive(b"ok"), info)  # type: ignore[arg-type]
    assert truncated.value.code == "zip_invalid"

    with pytest.raises(SzseLifecycleXlsxError) as unreadable:
        adapter._read_member_bounded(StreamArchive(fail=True), info)  # type: ignore[arg-type]
    assert unreadable.value.code == "zip_invalid"

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(adapter, "_MAX_MEMBER_BYTES", 2)
        info.file_size = 3
        with pytest.raises(SzseLifecycleXlsxError) as runtime_cap:
            adapter._read_member_bounded(StreamArchive(b"abc"), info)  # type: ignore[arg-type]
    assert runtime_cap.value.code == "zip_member_too_large"


def test_xml_preflight_enforces_depth_element_attribute_formula_and_text_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise independent streaming XML resource and active-content guards."""
    monkeypatch.setattr(adapter, "_MAX_MEMBER_BYTES", 3)
    assert _error_code_for(lambda: adapter._validate_xml_safety(b"<root/>")) == (
        "xml_unsafe_or_invalid"
    )
    monkeypatch.setattr(adapter, "_MAX_MEMBER_BYTES", 8 * 1024 * 1024)

    monkeypatch.setattr(adapter, "_MAX_XML_DEPTH", 1)
    assert _error_code_for(lambda: adapter._validate_xml_safety(b"<a><b/></a>")) == (
        "xml_unsafe_or_invalid"
    )
    monkeypatch.setattr(adapter, "_MAX_XML_DEPTH", 64)

    monkeypatch.setattr(adapter, "_MAX_XML_ELEMENTS", 1)
    assert _error_code_for(lambda: adapter._validate_xml_safety(b"<a><b/></a>")) == (
        "xml_unsafe_or_invalid"
    )
    monkeypatch.setattr(adapter, "_MAX_XML_ELEMENTS", 300_000)

    attributes = " ".join(f'a{index}="v"' for index in range(33))
    too_many_attributes = ("<a " + attributes + "/>").encode()
    assert (
        _error_code_for(lambda: adapter._validate_xml_safety(too_many_attributes))
        == "xml_unsafe_or_invalid"
    )
    assert (
        _error_code_for(lambda: adapter._validate_xml_safety(b'<cell customFormula="1"/>'))
        == "xml_unsafe_or_invalid"
    )
    assert (
        _error_code_for(lambda: adapter._validate_xml_safety(b"<externalLink/>"))
        == "xml_unsafe_or_invalid"
    )
    assert (
        _error_code_for(lambda: adapter._validate_xml_safety(b"<f>SUM(A1:A2)</f>"))
        == "xml_unsafe_or_invalid"
    )

    monkeypatch.setattr(adapter, "_MAX_XML_TEXT_BYTES", 1)
    assert _error_code_for(lambda: adapter._validate_xml_safety(b"<a>xy</a>")) == (
        "xml_unsafe_or_invalid"
    )


def test_tree_parser_normalizes_defusedxml_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the second XML parser's implementation details out of public errors."""

    def fail_parse(_data: bytes) -> adapter.Element:
        raise ValueError("SENSITIVE-PRIVATE-COMPANY")

    monkeypatch.setattr(adapter.SafeET, "fromstring", fail_parse)
    assert _error_code_for(lambda: adapter._parse_xml(b"<valid/>")) == "xml_unsafe_or_invalid"


@pytest.mark.parametrize(
    "failure",
    [adapter.SafeET.ParseError("malformed"), adapter.DefusedXmlException("unsafe")],
)
def test_tree_parser_normalizes_xml_library_exceptions(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Keep XML parser error classes and details out of the adapter surface."""

    def fail_parse(_data: bytes) -> adapter.Element:
        raise failure

    monkeypatch.setattr(adapter.SafeET, "fromstring", fail_parse)
    assert _error_code_for(lambda: adapter._parse_xml(b"<valid/>")) == "xml_unsafe_or_invalid"


@pytest.mark.parametrize(
    ("xml", "expected_code"),
    [
        ("<wrong/>", "relationship_rejected"),
        (
            f'<Relationships xmlns="{_PACKAGE_REL}"><Wrong/></Relationships>',
            "relationship_rejected",
        ),
        (
            f'<Relationships xmlns="{_PACKAGE_REL}">'
            f'<Relationship Id="r1" Type="{_DOC_REL_TYPE}worksheet" Target="a.xml"/>'
            f'<Relationship Id="r1" Type="{_DOC_REL_TYPE}styles" Target="b.xml"/>'
            "</Relationships>",
            "relationship_rejected",
        ),
        (
            f'<Relationships xmlns="{_PACKAGE_REL}">'
            f'<Relationship Id="r1" Type="{_DOC_REL_TYPE}worksheet" Target="same.xml"/>'
            f'<Relationship Id="r2" Type="{_DOC_REL_TYPE}styles" Target="same.xml"/>'
            "</Relationships>",
            "relationship_rejected",
        ),
        (f'<Relationships xmlns="{_PACKAGE_REL}"/>', "relationship_rejected"),
        (
            f'<Relationships xmlns="{_PACKAGE_REL}">'
            '<Relationship Id="r1" Type="urn:unknown" Target="a.xml"/>'
            "</Relationships>",
            "relationship_rejected",
        ),
    ],
)
def test_relationship_parser_rejects_ambiguous_and_unsupported_links(
    xml: str, expected_code: str
) -> None:
    """Reject invalid namespaces, IDs, targets, empty maps, and relation types."""
    code = _error_code_for(lambda: adapter._parse_relationships(xml.encode(), base_path="xl"))
    assert code == expected_code


def test_relationship_parser_accepts_only_safe_internal_targets() -> None:
    """Preserve internal relationship kinds while rejecting URL-like target forms."""
    valid = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="r1" Type="{_DOC_REL_TYPE}worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="r2" Type="{_PACKAGE_REL}/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        "</Relationships>"
    ).encode()
    result = adapter._parse_relationships(valid, base_path="")
    assert result["r1"] == ("worksheet", "worksheets/sheet1.xml")
    assert result["r2"] == ("core-properties", "docProps/core.xml")

    unsafe_targets = ("", "../x.xml", "/x.xml", "a\\b.xml", "https://x", "a%2fb.xml", "a?x", "a#x")
    for target in unsafe_targets:
        assert not adapter._safe_relationship_target(target)
    assert adapter._safe_relationship_target("worksheets/sheet1.xml")


def test_relationship_parser_rejects_missing_attributes_and_external_mode() -> None:
    """Require every internal relationship to have a complete, nonexternal shape."""
    malformed = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="r1" Type="{_DOC_REL_TYPE}worksheet" Target="sheet.xml"/>'
        '<Relationship Id="r2" Type="urn:unused" Target="other.xml" TargetMode="external"/>'
        "</Relationships>"
    ).encode()
    missing_type = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        '<Relationship Id="r1" Target="sheet.xml"/>'
        "</Relationships>"
    ).encode()
    assert (
        _error_code_for(lambda: adapter._parse_relationships(malformed, base_path="xl"))
        == "relationship_rejected"
    )
    assert (
        _error_code_for(lambda: adapter._parse_relationships(missing_type, base_path="xl"))
        == "relationship_rejected"
    )


def test_root_relationship_validation_requires_exact_package_closure() -> None:
    """Require all root property parts and the workbook to match their exact links."""
    office_rel = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="r1" Type="{_DOC_REL_TYPE}officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ).encode()
    valid_files = {"_rels/.rels": office_rel, "xl/workbook.xml": b"<workbook/>"}
    adapter._validate_root_relationships(valid_files)

    wrong_target = office_rel.replace(b"xl/workbook.xml", b"xl/other.xml")
    assert (
        _error_code_for(
            lambda: adapter._validate_root_relationships(
                {"_rels/.rels": wrong_target, "xl/other.xml": b"x"}
            )
        )
        == "relationship_rejected"
    )

    assert (
        _error_code_for(
            lambda: adapter._validate_root_relationships(
                {**valid_files, "docProps/core.xml": b"<core/>"}
            )
        )
        == "relationship_rejected"
    )

    core_rel = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="r1" Type="{_DOC_REL_TYPE}officeDocument" Target="xl/workbook.xml"/>'
        f'<Relationship Id="r2" Type="{_PACKAGE_REL}/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        "</Relationships>"
    ).encode()
    assert (
        _error_code_for(
            lambda: adapter._validate_root_relationships({**valid_files, "_rels/.rels": core_rel})
        )
        == "relationship_rejected"
    )
    adapter._validate_root_relationships(
        {**valid_files, "_rels/.rels": core_rel, "docProps/core.xml": b"<core/>"}
    )

    missing_linked_part = core_rel.replace(b"docProps/core.xml", b"docProps/app.xml")
    assert (
        _error_code_for(
            lambda: adapter._validate_root_relationships(
                {**valid_files, "_rels/.rels": missing_linked_part}
            )
        )
        == "relationship_rejected"
    )

    unsupported_root_kind = core_rel.replace(
        b"metadata/core-properties", b"metadata/unknown-properties"
    )
    assert (
        _error_code_for(
            lambda: adapter._validate_root_relationships(
                {**valid_files, "_rels/.rels": unsupported_root_kind, "docProps/core.xml": b"x"}
            )
        )
        == "relationship_rejected"
    )


def test_content_type_validator_rejects_ambiguous_declarations() -> None:
    """Reject malformed content-type paths, duplicates, defaults, and entries."""
    valid_files = {
        "xl/workbook.xml": b"<workbook/>",
        "xl/worksheets/sheet1.xml": b"<worksheet/>",
    }
    malformed_types = (
        b"<Types/>",
        (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Override PartName="/xl/missing.xml" ContentType="application/xml"/>'
            b"</Types>"
        ),
        (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Override PartName="/xl/../workbook.xml" ContentType="application/xml"/>'
            b"</Types>"
        ),
        (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Override PartName="/xl/workbook.xml"/>'
            b"</Types>"
        ),
        (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="json" ContentType="application/json"/>'
            b"</Types>"
        ),
        (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b'<Default Extension="xml" ContentType="text/xml"/>'
            b"</Types>"
        ),
        (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b"<Unknown/>"
            b"</Types>"
        ),
    )
    for xml in malformed_types:
        files = {"[Content_Types].xml": xml, **valid_files}

        def reject_current_types(package_files: dict[str, bytes] = files) -> None:
            adapter._validate_content_types(package_files, "xl/worksheets/sheet1.xml")

        assert _error_code_for(reject_current_types) == "package_structure_invalid"

    incomplete_overrides = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        b'relationships+xml"/>'
        b'<Override PartName="/xl/workbook.xml" ContentType="application/xml"/>'
        b"</Types>"
    )
    files = {"[Content_Types].xml": incomplete_overrides, **valid_files}
    assert (
        _error_code_for(lambda: adapter._validate_content_types(files, "xl/worksheets/sheet1.xml"))
        == "package_structure_invalid"
    )


def test_select_sheet_rejects_ambiguous_workbook_and_sheet_links() -> None:
    """Fail closed for invalid workbook declarations and relationship closure."""
    valid_relation = {"rId1": ("worksheet", "xl/worksheets/sheet1.xml")}
    valid_files = {"xl/worksheets/sheet1.xml": b"<sheet/>"}

    workbooks: tuple[WorkbookCase, ...] = (
        (
            f'<workbook xmlns="{_MAIN}"/>',
            valid_relation,
            valid_files,
            "workbook_unsupported",
        ),
        (
            f'<wrong xmlns="{_MAIN}"/>',
            valid_relation,
            valid_files,
            "workbook_unsupported",
        ),
        (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            '<sheets><sheet name="One" sheetId="1" r:id="rId1"/></sheets>'
            "<unsupported/></workbook>",
            valid_relation,
            valid_files,
            "workbook_unsupported",
        ),
        (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            '<workbookPr date1904="yes"/><sheets>'
            '<sheet name="One" sheetId="1" r:id="rId1"/></sheets></workbook>',
            valid_relation,
            valid_files,
            "workbook_unsupported",
        ),
        (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            "<sheets><unknown/></sheets></workbook>",
            valid_relation,
            valid_files,
            "sheet_unsupported",
        ),
        (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            '<sheets><sheet name="" sheetId="1" r:id="rId1"/></sheets></workbook>',
            valid_relation,
            valid_files,
            "sheet_unsupported",
        ),
        (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            f'<sheets><sheet name="{"n" * 129}" sheetId="1" r:id="rId1"/></sheets></workbook>',
            valid_relation,
            valid_files,
            "sheet_unsupported",
        ),
        (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            f'<sheets><sheet name="One" sheetId="1" r:id="missing"/></sheets></workbook>',
            valid_relation,
            valid_files,
            "sheet_unsupported",
        ),
        (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            '<sheets><sheet name="One" sheetId="1" r:id="rId1"/></sheets></workbook>',
            {"rId1": ("styles", "xl/styles.xml")},
            valid_files,
            "sheet_unsupported",
        ),
        (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
            '<sheets><sheet name="One" sheetId="1" r:id="rId1"/></sheets></workbook>',
            {"rId1": ("worksheet", "xl/worksheets/sheet1.xml")},
            {},
            "sheet_unsupported",
        ),
    )
    for workbook_xml, relations, files, expected in workbooks:

        def reject_workbook(
            xml: str = workbook_xml,
            relation_map: RelationshipMap = relations,
            package_files: dict[str, bytes] = files,
        ) -> object:
            return adapter._select_sheet(_element(xml), relation_map, package_files)

        code = _error_code_for(reject_workbook)
        assert code == expected

    valid_workbooks = (
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<sheets><sheet name="One" sheetId="1" r:id="rId1"/></sheets></workbook>',
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<sheets><sheet name="One" sheetId="1" r:id="rId1"/></sheets>'
        "<bookViews/><fileVersion/><calcPr/></workbook>",
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<workbookPr date1904="FALSE"/><sheets>'
        '<sheet name="One" sheetId="1" r:id="rId1"/></sheets></workbook>',
    )
    for workbook_xml in valid_workbooks:
        assert adapter._select_sheet(_element(workbook_xml), valid_relation, valid_files) == (
            "1900",
            "xl/worksheets/sheet1.xml",
            None,
        )


def test_select_sheet_enforces_optional_relationship_payload_closure() -> None:
    """Require supported relationship kinds and exact optional payload paths."""
    workbook = _element(
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_REL}">'
        '<sheets><sheet name="One" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    sheet_path = "xl/worksheets/sheet1.xml"
    sheet_files = {sheet_path: b"<sheet/>"}

    all_optional_relations = {
        "rId1": ("worksheet", sheet_path),
        "rId2": ("sharedStrings", "xl/sharedStrings.xml"),
        "rId3": ("styles", "xl/styles.xml"),
        "rId4": ("theme", "xl/theme/theme1.xml"),
    }
    optional_files = {
        **sheet_files,
        "xl/sharedStrings.xml": b"<sst/>",
        "xl/styles.xml": b"<styleSheet/>",
        "xl/theme/theme1.xml": b"<theme/>",
    }
    assert adapter._select_sheet(workbook, all_optional_relations, optional_files) == (
        "1900",
        sheet_path,
        "xl/sharedStrings.xml",
    )

    scenarios: tuple[tuple[RelationshipMap, dict[str, bytes], str], ...] = (
        (
            {"rId1": ("worksheet", sheet_path), "rId2": ("worksheet", "xl/worksheets/sheet2.xml")},
            sheet_files,
            "sheet_unsupported",
        ),
        (
            {
                "rId1": ("worksheet", sheet_path),
                "rId2": ("sharedStrings", "xl/sharedStrings.xml"),
                "rId3": ("sharedStrings", "xl/sharedStrings2.xml"),
            },
            sheet_files,
            "relationship_rejected",
        ),
        (
            {"rId1": ("worksheet", sheet_path), "rId2": ("unexpected", "xl/extra.xml")},
            sheet_files,
            "relationship_rejected",
        ),
        (
            {"rId1": ("worksheet", sheet_path), "rId2": ("sharedStrings", "xl/moved.xml")},
            {**sheet_files, "xl/moved.xml": b"<sst/>"},
            "relationship_rejected",
        ),
        (
            {"rId1": ("worksheet", sheet_path), "rId2": ("styles", "xl/moved.xml")},
            {**sheet_files, "xl/moved.xml": b"<styleSheet/>"},
            "relationship_rejected",
        ),
        (
            {"rId1": ("worksheet", sheet_path), "rId2": ("theme", "xl/theme/bad.xml")},
            {**sheet_files, "xl/theme/bad.xml": b"<theme/>"},
            "relationship_rejected",
        ),
        (
            {"rId1": ("worksheet", sheet_path), "rId2": ("sharedStrings", "xl/sharedStrings.xml")},
            sheet_files,
            "relationship_rejected",
        ),
        (
            {"rId1": ("worksheet", sheet_path)},
            {**sheet_files, "xl/sharedStrings.xml": b"<sst/>"},
            "relationship_rejected",
        ),
        (
            {"rId1": ("worksheet", sheet_path), "rId2": ("styles", "xl/styles.xml")},
            {**sheet_files, "xl/styles.xml": b"<styleSheet/>", "xl/unlinked.xml": b"x"},
            "relationship_rejected",
        ),
    )
    for relations, files, expected in scenarios:

        def reject_relationships(
            relation_map: RelationshipMap = relations,
            package_files: dict[str, bytes] = files,
        ) -> object:
            return adapter._select_sheet(workbook, relation_map, package_files)

        code = _error_code_for(reject_relationships)
        assert code == expected


def test_shared_string_parser_rejects_unsupported_structure_and_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound shared-string count and text while rejecting phonetic/unknown nodes."""
    empty_table = f'<sst xmlns="{_MAIN}"/>'.encode()
    assert adapter._parse_shared_strings(empty_table) == ()

    assert (
        _error_code_for(lambda: adapter._parse_shared_strings(f'<wrong xmlns="{_MAIN}"/>'.encode()))
        == "workbook_unsupported"
    )


def test_shared_string_parser_rejects_bad_runs_and_text_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject phonetic runs, unexpected rich-text children, and oversize text."""
    phonetic = f'<sst xmlns="{_MAIN}"><si><rPh/></si></sst>'.encode()
    invalid_item = f'<sst xmlns="{_MAIN}"><bad/></sst>'.encode()
    invalid_run = f'<sst xmlns="{_MAIN}"><si><r><x/></r></si></sst>'.encode()
    invalid_outer = f'<sst xmlns="{_MAIN}"><si><x/></si></sst>'.encode()
    assert _error_code_for(lambda: adapter._parse_shared_strings(phonetic)) == "cell_value_invalid"
    assert (
        _error_code_for(lambda: adapter._parse_shared_strings(invalid_item)) == "cell_value_invalid"
    )
    assert (
        _error_code_for(lambda: adapter._parse_shared_strings(invalid_run)) == "cell_value_invalid"
    )
    assert (
        _error_code_for(lambda: adapter._parse_shared_strings(invalid_outer))
        == "cell_value_invalid"
    )

    monkeypatch.setattr(adapter, "_MAX_SHARED_STRINGS", 0)
    too_many = f'<sst xmlns="{_MAIN}"><si><t>x</t></si></sst>'.encode()
    assert _error_code_for(lambda: adapter._parse_shared_strings(too_many)) == "cell_value_invalid"
    monkeypatch.setattr(adapter, "_MAX_SHARED_STRINGS", 100_000)

    monkeypatch.setattr(adapter, "_MAX_CELL_CHARS", 1)
    too_long = f'<sst xmlns="{_MAIN}"><si><t>xy</t></si></sst>'.encode()
    assert _error_code_for(lambda: adapter._parse_shared_strings(too_long)) == "cell_value_invalid"
    monkeypatch.setattr(adapter, "_MAX_CELL_CHARS", 4096)

    monkeypatch.setattr(adapter, "_MAX_SHARED_STRING_CHARS", 1)
    too_much_total = f'<sst xmlns="{_MAIN}"><si><t>a</t></si><si><t>b</t></si></sst>'.encode()
    assert (
        _error_code_for(lambda: adapter._parse_shared_strings(too_much_total))
        == "cell_value_invalid"
    )


def test_shared_string_parser_accepts_plain_and_rich_text_runs() -> None:
    """Concatenate supported rich-text runs and preserve empty text nodes safely."""
    xml = (
        f'<sst xmlns="{_MAIN}"><si><t>plain</t><r><rPr/><t>rich</t></r></si><si><t/></si></sst>'
    ).encode()
    assert adapter._parse_shared_strings(xml) == ("plainrich", "")


def test_worksheet_parser_rejects_invalid_rows_and_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject malformed row metadata and enforce worksheet row/cell budgets."""
    header_cells = "".join(
        _inline_cell(f"{column}1", value)
        for column, value in zip("ABCD", SZSE_LIFECYCLE_SYNTHETIC_HEADERS, strict=True)
    )
    invalid_worksheets: tuple[tuple[bytes, str], ...] = (
        (b"<wrong/>", "sheet_unsupported"),
        (f'<worksheet xmlns="{_MAIN}"/>'.encode(), "sheet_unsupported"),
        (
            f'<worksheet xmlns="{_MAIN}"><sheetData><unknown/></sheetData></worksheet>'.encode(),
            "sheet_unsupported",
        ),
        (
            (
                f'<worksheet xmlns="{_MAIN}"><sheetData><row><c r="A1"/></row>'
                "</sheetData></worksheet>"
            ).encode(),
            "row_limit_exceeded",
        ),
        (
            f'<worksheet xmlns="{_MAIN}"><sheetData><row r="0"/></sheetData></worksheet>'.encode(),
            "row_limit_exceeded",
        ),
        (
            f'<worksheet xmlns="{_MAIN}"><sheetData><row r="甲"/></sheetData></worksheet>'.encode(),
            "row_limit_exceeded",
        ),
        (
            (
                f'<worksheet xmlns="{_MAIN}"><sheetData><row r="2"/><row r="1"/>'
                "</sheetData></worksheet>"
            ).encode(),
            "sheet_unsupported",
        ),
        (
            (
                f'<worksheet xmlns="{_MAIN}"><sheetData><row r="1048577"/></sheetData></worksheet>'
            ).encode(),
            "sheet_unsupported",
        ),
        (
            (
                f'<worksheet xmlns="{_MAIN}"><sheetData><row r="1"><x/></row>'
                "</sheetData></worksheet>"
            ).encode(),
            "cell_value_invalid",
        ),
        (
            (
                f'<worksheet xmlns="{_MAIN}"><sheetData><row r="1">'
                f"{header_cells[:0]}</row></sheetData></worksheet>"
            ).encode(),
            "header_contract_mismatch",
        ),
    )
    for worksheet, expected in invalid_worksheets:

        def reject_worksheet(xml: bytes = worksheet) -> object:
            return adapter._parse_worksheet(xml, ())

        code = _error_code_for(reject_worksheet)
        assert code == expected

    blank_then_header = (
        f'<worksheet xmlns="{_MAIN}"><sheetData><row r="1"/><row r="2">'
        f"{header_cells.replace('1', '2')}</row></sheetData></worksheet>"
    ).encode()
    parsed_header, _header_cells, rows = adapter._parse_worksheet(blank_then_header, ())
    assert parsed_header == SZSE_LIFECYCLE_SYNTHETIC_HEADERS
    assert rows == ()

    no_header = f'<worksheet xmlns="{_MAIN}"><sheetData/></worksheet>'.encode()
    assert _error_code_for(lambda: adapter._parse_worksheet(no_header, ())) == (
        "header_contract_mismatch"
    )

    monkeypatch.setattr(adapter, "_MAX_ROWS", 1)
    with_header_and_row = _worksheet()
    assert (
        _error_code_for(lambda: adapter._parse_worksheet(with_header_and_row, ()))
        == "row_limit_exceeded"
    )
    monkeypatch.setattr(adapter, "_MAX_ROWS", 50_000)

    monkeypatch.setattr(adapter, "_MAX_CELLS", 0)
    assert (
        _error_code_for(lambda: adapter._parse_worksheet(_worksheet(), ())) == "cell_limit_exceeded"
    )


def test_parse_cell_rejects_invalid_references_types_children_and_values() -> None:
    """Validate cell address, type, style, child shape, and native value limits."""
    invalid_cells: tuple[tuple[str, int, tuple[str, ...], str], ...] = (
        ("<c/>", 2, (), "cell_value_invalid"),
        ('<c r="A0"/>', 0, (), "cell_value_invalid"),
        ('<c r="A3"/>', 2, (), "cell_value_invalid"),
        ('<c r="AAA2"/>', 2, (), "column_limit_exceeded"),
        ('<c r="A2" t="error"><v>bad</v></c>', 2, (), "cell_value_invalid"),
        ('<c r="A2" s="10001"/>', 2, (), "cell_value_invalid"),
        ('<c r="A2"><v>1</v><v>2</v></c>', 2, (), "cell_value_invalid"),
        ('<c r="A2"><is/><is/></c>', 2, (), "cell_value_invalid"),
        ('<c r="A2"><v>1</v><is/></c>', 2, (), "cell_value_invalid"),
        ('<c r="A2"><x/></c>', 2, (), "cell_value_invalid"),
        ('<c r="A2" t="n"><is><t>x</t></is></c>', 2, (), "cell_value_invalid"),
        ('<c r="A2" t="b"><v>2</v></c>', 2, (), "cell_value_invalid"),
        ('<c r="A2" t="s"><v>0</v></c>', 2, (), "cell_value_invalid"),
        ('<c r="A2" t="inlineStr"><v>x</v></c>', 2, (), "cell_value_invalid"),
        (
            '<c r="A2" t="inlineStr"><is><rPh><t>phonetic</t></rPh></is></c>',
            2,
            (),
            "cell_value_invalid",
        ),
        ('<c r="AG2"/>', 2, (), "column_limit_exceeded"),
        (
            f'<c r="A2"><v>{"1" * 4097}</v></c>',
            2,
            (),
            "cell_value_invalid",
        ),
    )
    for xml, row_number, strings, expected in invalid_cells:

        def reject_cell(
            cell_xml: str = xml,
            source_row_number: int = row_number,
            shared: tuple[str, ...] = strings,
        ) -> object:
            return adapter._parse_cell(_cell_element(cell_xml), source_row_number, shared)

        code = _error_code_for(reject_cell)
        assert code == expected

    empty = adapter._parse_cell(_cell_element('<c r="A2"/>'), 2, ())
    assert empty.raw_value == ""
    assert empty.text_value == ""
    multi_letter = adapter._parse_cell(_cell_element('<c r="AA2"><v>7</v></c>'), 2, ())
    assert multi_letter.column_index == 27


def test_numeric_helpers_reject_invalid_ascii_and_range_inputs() -> None:
    """Keep row and shared-string/style index conversion bounded and ASCII-only."""
    invalid_positive_values: tuple[str | None, ...] = (None, "", "0", "１２", "abc")
    for value in invalid_positive_values:

        def reject_positive(candidate: str | None = value) -> object:
            return adapter._positive_int(candidate, "row_limit_exceeded")

        assert _error_code_for(reject_positive) == "row_limit_exceeded"
    assert adapter._positive_int("12", "row_limit_exceeded") == 12

    invalid_nonnegative_values: tuple[tuple[str, int], ...] = (
        ("", 10),
        ("１２", 10),
        ("11", 10),
        ("bad", 10),
    )
    for value, maximum in invalid_nonnegative_values:

        def reject_nonnegative(candidate: str = value, upper_bound: int = maximum) -> object:
            return adapter._nonnegative_int(candidate, upper_bound)

        assert _error_code_for(reject_nonnegative) == "cell_value_invalid"
    assert adapter._nonnegative_int("10", 10) == 10
