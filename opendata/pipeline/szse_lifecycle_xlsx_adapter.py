"""Safely decode a deliberately small, synthetic SZSE lifecycle XLSX contract.

The adapter accepts bytes already supplied by a caller. It performs no file
extraction, network access, database work, or canonical identity assignment.
The dataset class is caller-declared metadata. Header names below belong only
to this synthetic contract; they have not been verified against an SZSE
publication. Source identity, row semantics, market coverage, and lifecycle
meaning remain unknown in every returned result.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast
from xml.parsers import expat

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

if TYPE_CHECKING:
    # Runtime parsing uses defusedxml after the bounded Expat preflight.
    from xml.etree.ElementTree import Element  # nosec B405

__all__ = [
    "SZSE_LIFECYCLE_SYNTHETIC_HEADERS",
    "SzseLifecycleDatasetClass",
    "SzseLifecycleRawCell",
    "SzseLifecycleRawRow",
    "SzseLifecycleXlsxError",
    "SzseLifecycleXlsxResult",
    "parse_szse_lifecycle_xlsx",
]

SzseLifecycleDatasetClass = Literal["current_a", "suspended", "terminated"]

# Fixture-only labels. No source schema or vendor contract is claimed here.
SZSE_LIFECYCLE_SYNTHETIC_HEADERS: tuple[str, ...] = (
    "source_code",
    "source_name",
    "source_date",
    "source_status",
)

_DATASET_CLASSES = frozenset(("current_a", "suspended", "terminated"))
_MAX_BODY_BYTES = 16 * 1024 * 1024
_MAX_MEMBERS = 64
_MAX_MEMBER_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 24 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_MAX_XML_DEPTH = 64
_MAX_XML_ELEMENTS = 300_000
_MAX_XML_TEXT_BYTES = 8 * 1024 * 1024
_MAX_ROWS = 50_000
_MAX_COLUMNS = 32
_MAX_CELLS = 250_000
_MAX_CELL_CHARS = 4096
_MAX_SHARED_STRINGS = 100_000
_MAX_SHARED_STRING_CHARS = 4 * 1024 * 1024
_MAX_STYLE_INDEX = 10_000
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_SPREADSHEET_NS_PREFIX = f"{{{_MAIN_NS}}}"
_RELATIONSHIP_NS_PREFIX = f"{{{_REL_NS}}}"
_PACKAGE_REL_NS_PREFIX = f"{{{_PACKAGE_REL_NS}}}"
_CELL_REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$", re.ASCII)
_SHEET_MEMBER = re.compile(r"^xl/worksheets/sheet[1-9][0-9]*\.xml$", re.ASCII)
_XML_FORBIDDEN_WORDS = (
    b"macroenabled",
    b"vbaproject",
    b"externallink",
    b"ddeLink".lower(),
)
_SAFE_ERROR_CODES = frozenset(
    {
        "body_type_invalid",
        "body_too_large",
        "dataset_class_invalid",
        "zip_invalid",
        "zip_member_count_invalid",
        "zip_member_path_invalid",
        "zip_member_duplicate",
        "zip_member_unsupported",
        "zip_member_encrypted",
        "zip_member_too_large",
        "zip_expanded_too_large",
        "zip_ratio_too_large",
        "macro_or_external_content_rejected",
        "package_member_unsupported",
        "package_structure_invalid",
        "xml_unsafe_or_invalid",
        "relationship_rejected",
        "workbook_unsupported",
        "sheet_unsupported",
        "row_limit_exceeded",
        "column_limit_exceeded",
        "cell_limit_exceeded",
        "cell_value_invalid",
        "header_contract_mismatch",
        "xlsx_parse_failed",
    }
)


class SzseLifecycleXlsxError(ValueError):
    """Fixed-code parser failure that never includes source values or paths."""

    def __init__(self, code: str) -> None:
        """Create a content-free, stable error code."""
        safe_code = code if code in _SAFE_ERROR_CODES else "xlsx_parse_failed"
        self.code = safe_code
        super().__init__(safe_code)

    def __repr__(self) -> str:
        """Return only the fixed error code."""
        return f"{type(self).__name__}({self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class SzseLifecycleRawCell:
    """One source cell without interpreting its value or business meaning."""

    reference: str
    column_index: int
    cell_type: str
    raw_value: str
    text_value: str
    style_index: int | None

    def __repr__(self) -> str:
        """Describe the cell shape without rendering source data."""
        return (
            f"{type(self).__name__}(reference={self.reference!r}, "
            f"cell_type={self.cell_type!r}, value_chars={len(self.raw_value)}, "
            f"style_index={self.style_index!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SzseLifecycleRawRow:
    """One non-header worksheet row with original row numbering and cells."""

    source_row_number: int
    cells: tuple[SzseLifecycleRawCell, ...]

    def __repr__(self) -> str:
        """Describe the row shape without rendering any cell values."""
        return (
            f"{type(self).__name__}(source_row_number={self.source_row_number}, "
            f"cell_count={len(self.cells)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SzseLifecycleXlsxResult:
    """Raw rows and explicit unknown states from one bounded XLSX body."""

    dataset_class: SzseLifecycleDatasetClass
    dataset_class_binding_status: Literal["caller_declared"]
    source_id: None
    source_id_status: Literal["unknown"]
    source_bytes_sha256: str
    date_system: Literal["1900", "1904"]
    headers: tuple[str, ...]
    header_cells: tuple[SzseLifecycleRawCell, ...]
    rows: tuple[SzseLifecycleRawRow, ...]
    lifecycle_semantics_status: Literal["unknown"]
    market_scope_status: Literal["unknown"]
    source_completeness_status: Literal["unknown"]
    header_contract_status: Literal["synthetic_contract_only"]

    def __repr__(self) -> str:
        """Summarize the result without exposing headers or row values."""
        return (
            f"{type(self).__name__}(dataset_class={self.dataset_class!r}, "
            f"row_count={len(self.rows)}, source_bytes_sha256={self.source_bytes_sha256!r}, "
            f"date_system={self.date_system!r})"
        )


class _RejectedXmlError(Exception):
    """Internal signal for a forbidden or over-budget XML document."""


def parse_szse_lifecycle_xlsx(
    body: bytes, *, dataset_class: SzseLifecycleDatasetClass
) -> SzseLifecycleXlsxResult:
    """Parse a bounded XLSX body against the synthetic, raw-only contract.

    The workbook must contain exactly one visible worksheet whose first
    non-empty row exactly matches :data:`SZSE_LIFECYCLE_SYNTHETIC_HEADERS`.
    Spreadsheet dates remain their original XML cell type, value, and style;
    no date, listing, delisting, market, or identity meaning is assigned.

    Args:
        body: Complete, already-acquired XLSX bytes.
        dataset_class: Explicit caller declaration: current A, suspended, or
            terminated. This label does not prove the workbook's source meaning.

    Returns:
        An immutable raw candidate with the complete-body SHA-256 and explicit
        unknown source, lifecycle, market-scope, and completeness states.

    Raises:
        SzseLifecycleXlsxError: For invalid, ambiguous, unsupported, or unsafe
            input. Error messages contain only a fixed code.
    """
    if type(body) is not bytes:
        raise SzseLifecycleXlsxError("body_type_invalid")
    if len(body) > _MAX_BODY_BYTES:
        raise SzseLifecycleXlsxError("body_too_large")
    if type(dataset_class) is not str or dataset_class not in _DATASET_CLASSES:
        raise SzseLifecycleXlsxError("dataset_class_invalid")

    try:
        files = _read_bounded_package(body)
        workbook = _parse_xml(files["xl/workbook.xml"])
        workbook_relationships = _parse_relationships(
            files["xl/_rels/workbook.xml.rels"], base_path="xl"
        )
        _validate_root_relationships(files)
        date_system, sheet_path, shared_strings_path = _select_sheet(
            workbook, workbook_relationships, files
        )
        _validate_content_types(files, sheet_path)
        shared_strings = (
            _parse_shared_strings(files[shared_strings_path])
            if shared_strings_path is not None
            else ()
        )
        header, header_cells, rows = _parse_worksheet(files[sheet_path], shared_strings)
        if header != SZSE_LIFECYCLE_SYNTHETIC_HEADERS:
            raise SzseLifecycleXlsxError("header_contract_mismatch")
        return SzseLifecycleXlsxResult(
            dataset_class=dataset_class,
            dataset_class_binding_status="caller_declared",
            source_id=None,
            source_id_status="unknown",
            source_bytes_sha256=hashlib.sha256(body).hexdigest(),
            date_system=date_system,
            headers=header,
            header_cells=header_cells,
            rows=rows,
            lifecycle_semantics_status="unknown",
            market_scope_status="unknown",
            source_completeness_status="unknown",
            header_contract_status="synthetic_contract_only",
        )
    except SzseLifecycleXlsxError:
        raise
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, SafeET.ParseError):
        raise SzseLifecycleXlsxError("xlsx_parse_failed") from None
    except Exception:
        # Keep parser/library details and any source-derived values out of errors.
        raise SzseLifecycleXlsxError("xlsx_parse_failed") from None


def _read_bounded_package(body: bytes) -> dict[str, bytes]:
    """Read a strictly allowlisted set of ZIP members without extraction."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(body), mode="r")
        infos = archive.infolist()
    except (OSError, ValueError, zipfile.BadZipFile):
        raise SzseLifecycleXlsxError("zip_invalid") from None
    if not infos or len(infos) > _MAX_MEMBERS:
        raise SzseLifecycleXlsxError("zip_member_count_invalid")

    seen: set[str] = set()
    total_size = 0
    for info in infos:
        name = info.filename
        if not _safe_member_name(name):
            raise SzseLifecycleXlsxError("zip_member_path_invalid")
        if name in seen:
            raise SzseLifecycleXlsxError("zip_member_duplicate")
        seen.add(name)
        if name.lower().endswith((".xlsm", ".xlsb", ".bin")):
            raise SzseLifecycleXlsxError("macro_or_external_content_rejected")
        if not _allowed_member(name):
            raise SzseLifecycleXlsxError("package_member_unsupported")
        if info.flag_bits & 0x1:
            raise SzseLifecycleXlsxError("zip_member_encrypted")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise SzseLifecycleXlsxError("zip_member_unsupported")
        if info.file_size < 0 or info.compress_size < 0:
            raise SzseLifecycleXlsxError("zip_member_unsupported")
        if info.file_size > _MAX_MEMBER_BYTES:
            raise SzseLifecycleXlsxError("zip_member_too_large")
        total_size += info.file_size
        if total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise SzseLifecycleXlsxError("zip_expanded_too_large")
        if info.file_size and info.file_size / max(info.compress_size, 1) > _MAX_COMPRESSION_RATIO:
            raise SzseLifecycleXlsxError("zip_ratio_too_large")

    required = {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
    }
    if not required.issubset(seen):
        raise SzseLifecycleXlsxError("package_structure_invalid")

    contents: dict[str, bytes] = {}
    try:
        with archive:
            for info in infos:
                data = _read_member_bounded(archive, info)
                if _is_xml_member(info.filename):
                    _validate_xml_safety(data)
                    lowered = data.lower()
                    if any(word in lowered for word in _XML_FORBIDDEN_WORDS):
                        raise SzseLifecycleXlsxError("macro_or_external_content_rejected")
                contents[info.filename] = data
    except SzseLifecycleXlsxError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        raise SzseLifecycleXlsxError("zip_invalid") from None
    return contents


def _safe_member_name(name: str) -> bool:
    """Reject archive names that could alias or escape a package path."""
    if type(name) is not str or not name or "\x00" in name or "\\" in name:
        return False
    if name.startswith("/") or name.startswith("~") or ":" in name:
        return False
    parts = name.split("/")
    return not any(part in ("", ".", "..") for part in parts)


def _allowed_member(name: str) -> bool:
    """Accept only the non-active OOXML subset supported by this parser."""
    if name in {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/sharedStrings.xml",
        "xl/styles.xml",
        "docProps/app.xml",
        "docProps/core.xml",
        "docProps/custom.xml",
    }:
        return True
    return bool(
        _SHEET_MEMBER.fullmatch(name) or re.fullmatch(r"xl/theme/theme[1-9][0-9]*\.xml", name)
    )


def _read_member_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Read one member with runtime caps in addition to ZIP metadata checks."""
    chunks = bytearray()
    try:
        with archive.open(info, mode="r") as member:
            while True:
                remaining = _MAX_MEMBER_BYTES - len(chunks)
                chunk = member.read(min(64 * 1024, remaining + 1))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > _MAX_MEMBER_BYTES or len(chunks) > info.file_size:
                    raise SzseLifecycleXlsxError("zip_member_too_large")
    except SzseLifecycleXlsxError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        raise SzseLifecycleXlsxError("zip_invalid") from None
    if len(chunks) != info.file_size:
        raise SzseLifecycleXlsxError("zip_invalid")
    return bytes(chunks)


def _is_xml_member(name: str) -> bool:
    """Return whether a package member must pass XML safety checks."""
    return name.endswith(".xml") or name.endswith(".rels")


def _validate_xml_safety(data: bytes) -> None:
    """Validate XML with a non-tree Expat pass and strict parser budgets."""
    if len(data) > _MAX_MEMBER_BYTES:
        raise SzseLifecycleXlsxError("xml_unsafe_or_invalid")
    parser = expat.ParserCreate(namespace_separator="}")
    depth = 0
    elements = 0
    text_bytes = 0

    def start_element(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, elements
        depth += 1
        elements += 1
        local_name = _local_name(name).casefold()
        if depth > _MAX_XML_DEPTH or elements > _MAX_XML_ELEMENTS or len(attrs) > 32:
            raise _RejectedXmlError
        if any("formula" in _local_name(key).casefold() for key in attrs):
            raise _RejectedXmlError
        if local_name in {
            "f",
            "formula",
            "formula1",
            "formula2",
            "calculatedcolumnformula",
            "totalsrowformula",
        }:
            raise _RejectedXmlError
        if local_name in {"externalreference", "externallink", "ddeLink".casefold(), "olelink"}:
            raise _RejectedXmlError

    def end_element(_name: str) -> None:
        nonlocal depth
        depth -= 1

    def character_data(value: str) -> None:
        nonlocal text_bytes
        text_bytes += len(value.encode("utf-8"))
        if text_bytes > _MAX_XML_TEXT_BYTES:
            raise _RejectedXmlError

    def reject_doctype(*_args: object) -> None:
        raise _RejectedXmlError

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = character_data
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.EntityDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = lambda *_args: 0
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, _RejectedXmlError, ValueError):
        raise SzseLifecycleXlsxError("xml_unsafe_or_invalid") from None


def _parse_xml(data: bytes) -> Element:
    """Build a small XML tree only after the safe streaming pass succeeds."""
    _validate_xml_safety(data)
    try:
        return cast("Element", SafeET.fromstring(data))
    except (DefusedXmlException, SafeET.ParseError, ValueError):
        raise SzseLifecycleXlsxError("xml_unsafe_or_invalid") from None


def _local_name(tag: str) -> str:
    """Return a namespace-independent XML local name."""
    return tag.rsplit("}", 1)[-1]


def _parse_relationships(data: bytes, *, base_path: str) -> dict[str, tuple[str, str]]:
    """Parse internal package links and reject external or ambiguous targets."""
    root = _parse_xml(data)
    if root.tag != f"{{{_PACKAGE_REL_NS}}}Relationships":
        raise SzseLifecycleXlsxError("relationship_rejected")
    result: dict[str, tuple[str, str]] = {}
    for relationship in root:
        if relationship.tag != f"{{{_PACKAGE_REL_NS}}}Relationship":
            raise SzseLifecycleXlsxError("relationship_rejected")
        relation_id = relationship.get("Id")
        relation_type = relationship.get("Type")
        target = relationship.get("Target")
        target_mode = relationship.get("TargetMode", "Internal")
        if (
            not relation_id
            or not relation_type
            or not target
            or target_mode.casefold() != "internal"
            or relation_id in result
        ):
            raise SzseLifecycleXlsxError("relationship_rejected")
        if not _safe_relationship_target(target):
            raise SzseLifecycleXlsxError("relationship_rejected")
        resolved = f"{base_path}/{target}" if base_path else target
        result[relation_id] = (_relationship_kind(relation_type), resolved)
    if len({path for _kind, path in result.values()}) != len(result):
        raise SzseLifecycleXlsxError("relationship_rejected")
    if not result:
        raise SzseLifecycleXlsxError("relationship_rejected")
    return result


def _relationship_kind(relation_type: str) -> str:
    """Resolve only the exact OOXML relationship type namespaces supported."""
    office_prefix = f"{_REL_NS}/"
    if relation_type.startswith(office_prefix):
        return relation_type[len(office_prefix) :]
    if relation_type == f"{_PACKAGE_REL_NS}/metadata/core-properties":
        return "core-properties"
    raise SzseLifecycleXlsxError("relationship_rejected")


def _safe_relationship_target(target: str) -> bool:
    """Reject URL-like, absolute, encoded, or traversal relationship targets."""
    if not target or "\\" in target or ":" in target or "?" in target or "#" in target:
        return False
    if target.startswith("/") or "%" in target:
        return False
    return all(part not in ("", ".", "..") for part in target.split("/"))


def _validate_root_relationships(files: dict[str, bytes]) -> None:
    """Require an internal officeDocument link to the expected workbook."""
    relationships = _parse_relationships(files["_rels/.rels"], base_path="")
    expected_paths = {
        "officeDocument": "xl/workbook.xml",
        "core-properties": "docProps/core.xml",
        "extended-properties": "docProps/app.xml",
        "custom-properties": "docProps/custom.xml",
    }
    office_documents = [path for kind, path in relationships.values() if kind == "officeDocument"]
    if office_documents != [expected_paths["officeDocument"]]:
        raise SzseLifecycleXlsxError("relationship_rejected")
    if any(path not in files for _kind, path in relationships.values()):
        raise SzseLifecycleXlsxError("relationship_rejected")
    if any(kind not in expected_paths for kind, _path in relationships.values()):
        raise SzseLifecycleXlsxError("relationship_rejected")
    if any(path != expected_paths[kind] for kind, path in relationships.values()):
        raise SzseLifecycleXlsxError("relationship_rejected")
    linked_properties = {path for kind, path in relationships.values() if kind != "officeDocument"}
    package_properties = {path for path in files if path.startswith("docProps/")}
    if package_properties != linked_properties:
        raise SzseLifecycleXlsxError("relationship_rejected")


def _validate_content_types(files: dict[str, bytes], sheet_path: str) -> None:
    """Require exact workbook and worksheet content-type declarations."""
    root = _parse_xml(files["[Content_Types].xml"])
    if root.tag != f"{{{_CONTENT_TYPES_NS}}}Types":
        raise SzseLifecycleXlsxError("package_structure_invalid")
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}
    for entry in root:
        if entry.tag == f"{{{_CONTENT_TYPES_NS}}}Override":
            part_name = entry.get("PartName")
            content_type = entry.get("ContentType")
            if (
                not part_name
                or not part_name.startswith("/")
                or not _safe_member_name(part_name[1:])
                or part_name[1:] not in files
                or not content_type
                or part_name in overrides
            ):
                raise SzseLifecycleXlsxError("package_structure_invalid")
            overrides[part_name] = content_type
        elif entry.tag == f"{{{_CONTENT_TYPES_NS}}}Default":
            extension = entry.get("Extension")
            content_type = entry.get("ContentType")
            if extension not in {"xml", "rels"} or not content_type or extension in defaults:
                raise SzseLifecycleXlsxError("package_structure_invalid")
            defaults[extension] = content_type
        else:
            raise SzseLifecycleXlsxError("package_structure_invalid")

    expected_overrides = {
        "/xl/workbook.xml": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
        ),
        f"/{sheet_path}": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
        ),
    }
    if any(overrides.get(part) != kind for part, kind in expected_overrides.items()):
        raise SzseLifecycleXlsxError("package_structure_invalid")
    if defaults.get("rels") != "application/vnd.openxmlformats-package.relationships+xml":
        raise SzseLifecycleXlsxError("package_structure_invalid")
    sheet_members = {name for name in files if _SHEET_MEMBER.fullmatch(name)}
    if sheet_members != {sheet_path}:
        raise SzseLifecycleXlsxError("sheet_unsupported")


def _select_sheet(
    workbook: Element,
    relationships: dict[str, tuple[str, str]],
    files: dict[str, bytes],
) -> tuple[Literal["1900", "1904"], str, str | None]:
    """Select one visible sheet and the optional shared-string table."""
    if workbook.tag != f"{_SPREADSHEET_NS_PREFIX}workbook":
        raise SzseLifecycleXlsxError("workbook_unsupported")
    allowed_workbook_elements = {
        f"{_SPREADSHEET_NS_PREFIX}workbookPr",
        f"{_SPREADSHEET_NS_PREFIX}bookViews",
        f"{_SPREADSHEET_NS_PREFIX}fileVersion",
        f"{_SPREADSHEET_NS_PREFIX}calcPr",
        f"{_SPREADSHEET_NS_PREFIX}sheets",
    }
    if any(child.tag not in allowed_workbook_elements for child in workbook):
        raise SzseLifecycleXlsxError("workbook_unsupported")
    workbook_property_nodes = workbook.findall(f"{_SPREADSHEET_NS_PREFIX}workbookPr")
    sheets_nodes = workbook.findall(f"{_SPREADSHEET_NS_PREFIX}sheets")
    if (
        len(workbook_property_nodes) > 1
        or len(sheets_nodes) != 1
        or any(sum(child.tag == tag for child in workbook) > 1 for tag in allowed_workbook_elements)
    ):
        raise SzseLifecycleXlsxError("workbook_unsupported")
    workbook_properties = workbook_property_nodes[0] if workbook_property_nodes else None
    date_1904 = workbook_properties.get("date1904", "0") if workbook_properties is not None else "0"
    if date_1904 in ("1", "true", "TRUE"):
        date_system: Literal["1900", "1904"] = "1904"
    elif date_1904 in ("0", "false", "FALSE"):
        date_system = "1900"
    else:
        raise SzseLifecycleXlsxError("workbook_unsupported")

    sheets_element = sheets_nodes[0]
    if len(sheets_element) != 1:
        raise SzseLifecycleXlsxError("sheet_unsupported")
    sheet = sheets_element[0]
    if sheet.tag != f"{_SPREADSHEET_NS_PREFIX}sheet":
        raise SzseLifecycleXlsxError("sheet_unsupported")
    sheet_name = sheet.get("name", "")
    state = sheet.get("state", "visible")
    relation_id = sheet.get(f"{_RELATIONSHIP_NS_PREFIX}id")
    if not sheet_name or len(sheet_name) > 128 or state != "visible" or not relation_id:
        raise SzseLifecycleXlsxError("sheet_unsupported")
    selected = relationships.get(relation_id)
    if selected is None or selected[0] != "worksheet" or not _SHEET_MEMBER.fullmatch(selected[1]):
        raise SzseLifecycleXlsxError("sheet_unsupported")
    if selected[1] not in files:
        raise SzseLifecycleXlsxError("sheet_unsupported")
    if {name for name in files if _SHEET_MEMBER.fullmatch(name)} != {selected[1]}:
        raise SzseLifecycleXlsxError("sheet_unsupported")

    worksheet_relations = [path for kind, path in relationships.values() if kind == "worksheet"]
    if worksheet_relations != [selected[1]]:
        raise SzseLifecycleXlsxError("sheet_unsupported")
    shared_paths = [path for kind, path in relationships.values() if kind == "sharedStrings"]
    if len(shared_paths) > 1:
        raise SzseLifecycleXlsxError("relationship_rejected")
    if any(
        kind not in {"worksheet", "sharedStrings", "styles", "theme"}
        for kind, _path in relationships.values()
    ):
        raise SzseLifecycleXlsxError("relationship_rejected")
    for kind, path in relationships.values():
        if path not in files:
            raise SzseLifecycleXlsxError("relationship_rejected")
        if kind == "sharedStrings" and path != "xl/sharedStrings.xml":
            raise SzseLifecycleXlsxError("relationship_rejected")
        if kind == "styles" and path != "xl/styles.xml":
            raise SzseLifecycleXlsxError("relationship_rejected")
        if kind == "theme" and not re.fullmatch(r"xl/theme/theme[1-9][0-9]*\.xml", path):
            raise SzseLifecycleXlsxError("relationship_rejected")
    if ("xl/sharedStrings.xml" in files) != bool(shared_paths):
        raise SzseLifecycleXlsxError("relationship_rejected")
    if ("xl/styles.xml" in files) != any(
        kind == "styles" for kind, _path in relationships.values()
    ):
        raise SzseLifecycleXlsxError("relationship_rejected")
    xl_payload_members = {
        name
        for name in files
        if name.startswith("xl/") and name not in {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
    }
    linked_xl_payloads = {path for _kind, path in relationships.values()}
    if xl_payload_members != linked_xl_payloads:
        raise SzseLifecycleXlsxError("relationship_rejected")
    return date_system, selected[1], shared_paths[0] if shared_paths else None


def _parse_shared_strings(data: bytes) -> tuple[str, ...]:
    """Decode bounded shared strings while retaining their original indexes."""
    root = _parse_xml(data)
    if root.tag != f"{_SPREADSHEET_NS_PREFIX}sst":
        raise SzseLifecycleXlsxError("workbook_unsupported")
    values: list[str] = []
    total_chars = 0
    for item in root:
        if item.tag != f"{_SPREADSHEET_NS_PREFIX}si" or len(values) >= _MAX_SHARED_STRINGS:
            raise SzseLifecycleXlsxError("cell_value_invalid")
        if item.find(f"{_SPREADSHEET_NS_PREFIX}rPh") is not None:
            raise SzseLifecycleXlsxError("cell_value_invalid")
        value = _text_runs(item)
        if len(value) > _MAX_CELL_CHARS:
            raise SzseLifecycleXlsxError("cell_value_invalid")
        total_chars += len(value)
        if total_chars > _MAX_SHARED_STRING_CHARS:
            raise SzseLifecycleXlsxError("cell_value_invalid")
        values.append(value)
    return tuple(values)


def _text_runs(element: Element) -> str:
    """Join only spreadsheet text runs and reject unsupported phonetic text."""
    pieces: list[str] = []
    for child in element:
        if child.tag == f"{_SPREADSHEET_NS_PREFIX}t":
            pieces.append(child.text or "")
        elif child.tag == f"{_SPREADSHEET_NS_PREFIX}r":
            for run_child in child:
                if run_child.tag == f"{_SPREADSHEET_NS_PREFIX}t":
                    pieces.append(run_child.text or "")
                elif run_child.tag != f"{_SPREADSHEET_NS_PREFIX}rPr":
                    raise SzseLifecycleXlsxError("cell_value_invalid")
        else:
            raise SzseLifecycleXlsxError("cell_value_invalid")
    return "".join(pieces)


def _parse_worksheet(
    data: bytes, shared_strings: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[SzseLifecycleRawCell, ...], tuple[SzseLifecycleRawRow, ...]]:
    """Decode sparse worksheet cells without date or business-field coercion."""
    root = _parse_xml(data)
    if root.tag != f"{_SPREADSHEET_NS_PREFIX}worksheet":
        raise SzseLifecycleXlsxError("sheet_unsupported")
    if root.find(f"{_SPREADSHEET_NS_PREFIX}mergeCells") is not None:
        raise SzseLifecycleXlsxError("sheet_unsupported")
    if root.find(f"{_SPREADSHEET_NS_PREFIX}extLst") is not None:
        raise SzseLifecycleXlsxError("sheet_unsupported")
    sheet_data_nodes = root.findall(f"{_SPREADSHEET_NS_PREFIX}sheetData")
    dimension_nodes = root.findall(f"{_SPREADSHEET_NS_PREFIX}dimension")
    if len(sheet_data_nodes) != 1 or len(dimension_nodes) > 1:
        raise SzseLifecycleXlsxError("sheet_unsupported")
    sheet_data = sheet_data_nodes[0]
    allowed_root_elements = {
        f"{_SPREADSHEET_NS_PREFIX}sheetData",
        f"{_SPREADSHEET_NS_PREFIX}dimension",
    }
    if any(child.tag not in allowed_root_elements for child in root):
        raise SzseLifecycleXlsxError("sheet_unsupported")

    header: tuple[str, ...] | None = None
    raw_header_cells: tuple[SzseLifecycleRawCell, ...] | None = None
    data_rows: list[SzseLifecycleRawRow] = []
    previous_row = 0
    total_cells = 0
    physical_rows = 0
    for row_element in sheet_data:
        physical_rows += 1
        if physical_rows > _MAX_ROWS:
            raise SzseLifecycleXlsxError("row_limit_exceeded")
        if row_element.tag != f"{_SPREADSHEET_NS_PREFIX}row":
            raise SzseLifecycleXlsxError("sheet_unsupported")
        row_number = _positive_int(row_element.get("r"), "row_limit_exceeded")
        if row_number <= previous_row or row_number > 1_048_576:
            raise SzseLifecycleXlsxError("sheet_unsupported")
        previous_row = row_number
        if row_element.get("hidden", "0").casefold() in {"1", "true"}:
            raise SzseLifecycleXlsxError("sheet_unsupported")

        cells: list[SzseLifecycleRawCell] = []
        seen_columns: set[int] = set()
        for cell_element in row_element:
            if cell_element.tag != f"{_SPREADSHEET_NS_PREFIX}c":
                raise SzseLifecycleXlsxError("cell_value_invalid")
            total_cells += 1
            if total_cells > _MAX_CELLS:
                raise SzseLifecycleXlsxError("cell_limit_exceeded")
            cell = _parse_cell(cell_element, row_number, shared_strings)
            if cell.column_index > len(SZSE_LIFECYCLE_SYNTHETIC_HEADERS):
                raise SzseLifecycleXlsxError("column_limit_exceeded")
            if cell.column_index in seen_columns:
                raise SzseLifecycleXlsxError("cell_value_invalid")
            seen_columns.add(cell.column_index)
            cells.append(cell)
        cells.sort(key=lambda cell: cell.column_index)

        if header is None:
            if not cells or all(cell.text_value == "" for cell in cells):
                continue
            if len(cells) != len(SZSE_LIFECYCLE_SYNTHETIC_HEADERS):
                raise SzseLifecycleXlsxError("header_contract_mismatch")
            header_values_by_column = {cell.column_index: cell.text_value for cell in cells}
            if set(header_values_by_column) != {1, 2, 3, 4}:
                raise SzseLifecycleXlsxError("header_contract_mismatch")
            header = tuple(header_values_by_column[index] for index in range(1, 5))
            raw_header_cells = tuple(cells)
            continue
        if any(cell.column_index > len(SZSE_LIFECYCLE_SYNTHETIC_HEADERS) for cell in cells):
            raise SzseLifecycleXlsxError("column_limit_exceeded")
        if any(
            len(cell.raw_value) > _MAX_CELL_CHARS or len(cell.text_value) > _MAX_CELL_CHARS
            for cell in cells
        ):
            raise SzseLifecycleXlsxError("cell_value_invalid")
        data_rows.append(SzseLifecycleRawRow(row_number, tuple(cells)))

    if header is None or raw_header_cells is None:
        raise SzseLifecycleXlsxError("header_contract_mismatch")
    return header, raw_header_cells, tuple(data_rows)


def _parse_cell(
    cell_element: Element,
    row_number: int,
    shared_strings: tuple[str, ...],
) -> SzseLifecycleRawCell:
    """Retain a cell's raw XML value, shared text, native type, and style."""
    reference = cell_element.get("r")
    if reference is None:
        raise SzseLifecycleXlsxError("cell_value_invalid")
    match = _CELL_REF.fullmatch(reference)
    if match is None or int(match.group(2)) != row_number:
        raise SzseLifecycleXlsxError("cell_value_invalid")
    column_index = _column_number(match.group(1))
    if column_index > _MAX_COLUMNS:
        raise SzseLifecycleXlsxError("column_limit_exceeded")
    cell_type = cell_element.get("t", "n")
    if cell_type not in {"n", "s", "inlineStr", "str", "b", "d"}:
        raise SzseLifecycleXlsxError("cell_value_invalid")
    style_raw = cell_element.get("s")
    style_index = _nonnegative_int(style_raw, _MAX_STYLE_INDEX) if style_raw is not None else None
    value_elements = [child for child in cell_element if child.tag == f"{_SPREADSHEET_NS_PREFIX}v"]
    inline_elements = [
        child for child in cell_element if child.tag == f"{_SPREADSHEET_NS_PREFIX}is"
    ]
    if len(value_elements) > 1 or len(inline_elements) > 1 or (value_elements and inline_elements):
        raise SzseLifecycleXlsxError("cell_value_invalid")
    if any(
        child.tag not in {f"{_SPREADSHEET_NS_PREFIX}v", f"{_SPREADSHEET_NS_PREFIX}is"}
        for child in cell_element
    ):
        raise SzseLifecycleXlsxError("cell_value_invalid")

    if inline_elements:
        if cell_type != "inlineStr":
            raise SzseLifecycleXlsxError("cell_value_invalid")
        raw_value = _text_runs(inline_elements[0])
        text_value = raw_value
    elif value_elements:
        raw_value = value_elements[0].text or ""
        if cell_type == "s":
            shared_index = _nonnegative_int(raw_value, len(shared_strings) - 1)
            text_value = shared_strings[shared_index]
        elif cell_type in {"str", "d"}:
            text_value = raw_value
        elif cell_type == "b":
            if raw_value not in {"0", "1"}:
                raise SzseLifecycleXlsxError("cell_value_invalid")
            text_value = raw_value
        elif cell_type == "n":
            if raw_value and len(raw_value) > _MAX_CELL_CHARS:
                raise SzseLifecycleXlsxError("cell_value_invalid")
            text_value = raw_value
        else:
            raise SzseLifecycleXlsxError("cell_value_invalid")
    else:
        raw_value = ""
        text_value = ""
    if len(raw_value) > _MAX_CELL_CHARS or len(text_value) > _MAX_CELL_CHARS:
        raise SzseLifecycleXlsxError("cell_value_invalid")
    return SzseLifecycleRawCell(
        reference=reference,
        column_index=column_index,
        cell_type=cell_type,
        raw_value=raw_value,
        text_value=text_value,
        style_index=style_index,
    )


def _column_number(letters: str) -> int:
    """Convert an Excel column reference to a one-based integer."""
    number = 0
    for character in letters:
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _positive_int(value: str | None, error_code: str) -> int:
    """Parse a positive bounded integer without echoing invalid input."""
    if value is None or not value.isascii() or not value.isdecimal():
        raise SzseLifecycleXlsxError(error_code)
    number = int(value)
    if number <= 0:
        raise SzseLifecycleXlsxError(error_code)
    return number


def _nonnegative_int(value: str, maximum: int) -> int:
    """Parse a bounded non-negative integer from a source cell or style."""
    if not value or not value.isascii() or not value.isdecimal():
        raise SzseLifecycleXlsxError("cell_value_invalid")
    number = int(value)
    if number > maximum:
        raise SzseLifecycleXlsxError("cell_value_invalid")
    return number
