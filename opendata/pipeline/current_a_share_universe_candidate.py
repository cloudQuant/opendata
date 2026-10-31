"""Build an offline, evidence-bound current A-share universe candidate.

The builder consumes an explicit manifest of already captured catalog files,
verifies each file's size and SHA-256, applies only caller-declared field
mappings, and emits a deterministic JSON candidate snapshot. It never performs
network access, never reads environment files, never writes a database, and
never publishes an API resource.

The result is a review candidate. It is not an authoritative universe, does not
prove market completeness, does not include a delisted-security lifecycle, and
does not attest source pagination or retention. Duplicates and cross-source
field disagreements are reported rather than merged or repaired. A security is
identified only by its explicitly declared market plus six-digit symbol; the
market is never inferred from a symbol prefix.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from itertools import islice
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

from opendata.pipeline.fuyao_instrument_census import (
    FuyaoInstrumentCensusError,
    FuyaoInstrumentPageInput,
    build_fuyao_instrument_census,
)
from opendata.pipeline.sse_instrument_census import (
    SSEInstrumentCensusError,
    SSEInstrumentPageInput,
    SSEInstrumentSelector,
    build_sse_instrument_census,
)
from opendata.pipeline.szse_lifecycle_xlsx_adapter import (
    SzseLifecycleXlsxError,
    SzseLifecycleXlsxResult,
    parse_szse_lifecycle_xlsx,
)

__all__ = [
    "MAX_MANIFEST_BYTES",
    "MAX_SOURCE_BYTES",
    "CurrentAShareUniverseCandidate",
    "EvidenceFormat",
    "Market",
    "PageEvidence",
    "RowDisposition",
    "SourceEvidence",
    "UniverseCandidateError",
    "UniverseCandidateManifest",
    "build_universe_candidate",
    "load_manifest",
    "render_snapshot",
]

SCHEMA_VERSION = "current-a-share-universe-candidate.v1"
MANIFEST_SCHEMA = "opendata.current-a-share-universe-candidate-manifest.v1"

MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCES = 512
MAX_ROWS = 100_000
MAX_COLUMNS = 64
MAX_FIELD_NAME_CHARS = 128
MAX_CELL_CHARS = 4_096
MAX_ROW_FIELDS = 64

_MARKET_EXCHANGE: Mapping[str, str] = MappingProxyType(
    {"CN-SSE": "SH", "CN-SZSE": "SZ", "CN-BSE": "BJ"}
)
_EXCHANGE_MARKET: Mapping[str, str] = MappingProxyType(
    {"SH": "CN-SSE", "SZ": "CN-SZSE", "BJ": "CN-BSE"}
)
_MARKETS = frozenset(_MARKET_EXCHANGE)
_EXCHANGES = frozenset(_EXCHANGE_MARKET)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", re.ASCII)
_CAPTURE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$", re.ASCII)
_COLUMN_PATTERN = re.compile(r"^[A-Z]{1,3}$", re.ASCII)
_BARE_SYMBOL_PATTERN = re.compile(r"^([0-9]{6})$", re.ASCII)
_SUFFIX_SYMBOL_PATTERN = re.compile(r"^([0-9]{6})\.(SH|SZ|BJ)$", re.ASCII)
_PREFIX_SYMBOL_PATTERN = re.compile(r"^(SH|SZ|BJ)([0-9]{6})$", re.ASCII)
_BSE_PREFIXES = ("4", "8", "920")

FrozenScalar: TypeAlias = bool | int | float | str | None


class UniverseCandidateError(ValueError):
    """A stable, content-free refusal for malformed manifests or evidence."""

    def __init__(self, code: str) -> None:
        """Create an error carrying only a machine-readable code.

        Args:
            code: Stable lowercase refusal code.
        """
        self.code = code
        super().__init__(code)


class Market(str, Enum):
    """Supported China A-share venue namespaces."""

    CN_SSE = "CN-SSE"
    CN_SZSE = "CN-SZSE"
    CN_BSE = "CN-BSE"


class EvidenceFormat(str, Enum):
    """Captured evidence layouts accepted by the offline builder."""

    NORMALIZED_ROWS_JSON = "normalized_rows_json"
    SZSE_CURRENT_A_XLSX = "szse_current_a_xlsx"
    SSE_INSTRUMENT_PAGES_JSON = "sse_instrument_pages_json"
    FUYAO_INSTRUMENT_PAGES_JSON = "fuyao_instrument_pages_json"


class RowDisposition(str, Enum):
    """How far one source row could be carried toward a candidate identity."""

    CANDIDATE = "candidate"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class ReportedTotalBasis(str, Enum):
    """Caller-declared meaning of a supplied page or record total."""

    ROW_COUNT = "row_count"
    PAGE_COUNT = "page_count"


_PAGE_REASONS = (
    "no_pagination_evidence",
    "page_sequence_gap",
    "terminal_evidence_missing",
    "final_page_not_terminal",
    "nonfinal_page_marked_terminal",
    "nonfinal_page_without_next_cursor",
    "initial_cursor_not_empty",
    "cursor_chain_broken",
    "final_page_has_next_cursor",
    "reported_total_missing",
    "reported_total_conflict",
    "reported_total_basis_missing",
    "reported_total_basis_conflict",
    "row_count_mismatch",
    "page_count_mismatch",
    "declared_row_count_mismatch",
)


def _iso_utc(value: datetime) -> str:
    """Render a timezone-aware datetime as a stable UTC second-precision string."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_instant(value: object, label: str) -> datetime:
    """Parse one strict ISO-8601 timestamp and require an explicit offset."""
    if type(value) is not str or not value or len(value) > 64:
        raise UniverseCandidateError(f"{label}_invalid")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise UniverseCandidateError(f"{label}_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UniverseCandidateError(f"{label}_invalid")
    return parsed


def _require_text(value: object, label: str, limit: int) -> str:
    """Require nonempty printable text without trimming the caller's value."""
    if (
        type(value) is not str
        or not value
        or len(value) > limit
        or any(not character.isprintable() for character in value)
    ):
        raise UniverseCandidateError(f"{label}_invalid")
    return value


def _require_sha256(value: object, label: str) -> str:
    """Require a lowercase SHA-256 hex digest without claiming authenticity."""
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise UniverseCandidateError(f"{label}_invalid")
    return value


def _canonical_scalar(value: object, label: str) -> FrozenScalar:
    """Validate one flat scalar source value and return an immutable copy."""
    if value is None or type(value) in (bool, int):
        if type(value) is int and value.bit_length() > 64:
            raise UniverseCandidateError(f"{label}_invalid")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise UniverseCandidateError(f"{label}_invalid")
        return value
    if type(value) is str:
        if len(value) > MAX_CELL_CHARS:
            raise UniverseCandidateError(f"{label}_invalid")
        return value
    raise UniverseCandidateError(f"{label}_invalid")


def _validate_flat_row(row: object) -> Mapping[str, FrozenScalar]:
    """Require a bounded flat JSON object with scalar values only."""
    if not isinstance(row, Mapping) or not row or len(row) > MAX_ROW_FIELDS:
        raise UniverseCandidateError("source_row_invalid")
    copied: dict[str, FrozenScalar] = {}
    for key, value in row.items():
        if (
            type(key) is not str
            or not key
            or len(key) > MAX_FIELD_NAME_CHARS
            or any(not character.isprintable() for character in key)
            or key in copied
        ):
            raise UniverseCandidateError("source_row_field_invalid")
        copied[key] = _canonical_scalar(value, "source_row_value")
    return MappingProxyType(copied)


def _canonical_row_sha256(row: Mapping[str, FrozenScalar]) -> str:
    """Hash one validated flat row using canonical compact JSON."""
    encoded = json.dumps(
        dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON object keys instead of silently keeping the last."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise UniverseCandidateError("json_duplicate_key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject NaN and Infinity tokens that are not valid JSON."""
    raise UniverseCandidateError(f"json_constant_{value.lower()}")


def _parse_json_bytes(data: bytes, label: str) -> object:
    """Decode one UTF-8 JSON document with strict keys and finite numbers."""
    if type(data) is not bytes or not data:
        raise UniverseCandidateError(f"{label}_empty")
    if data.startswith(b"\xef\xbb\xbf"):
        raise UniverseCandidateError(f"{label}_bom_not_supported")
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except UniverseCandidateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise UniverseCandidateError(f"{label}_invalid_json") from None


def _read_bounded_regular_file(path_text: str, max_bytes: int) -> tuple[bytes, str]:
    """Read an absolute, non-symlinked regular file within an explicit bound."""
    if type(path_text) is not str or not path_text or len(path_text) > 4_096:
        raise UniverseCandidateError("path_invalid")
    if "\x00" in path_text:
        raise UniverseCandidateError("path_invalid")
    path = Path(path_text)
    if not path.is_absolute():
        raise UniverseCandidateError("path_not_absolute")
    try:
        absolute = os.path.abspath(path_text)
        real = os.path.realpath(path_text)
        metadata = os.lstat(path_text)
    except OSError:
        raise UniverseCandidateError("path_unreadable") from None
    if absolute != real or path.is_symlink():
        raise UniverseCandidateError("path_symlink_rejected")
    if not stat.S_ISREG(metadata.st_mode):
        raise UniverseCandidateError("path_not_regular_file")
    if metadata.st_size > max_bytes:
        raise UniverseCandidateError("path_oversize")
    try:
        data = path.read_bytes()
    except OSError:
        raise UniverseCandidateError("path_unreadable") from None
    if len(data) > max_bytes:
        raise UniverseCandidateError("path_oversize")
    return data, hashlib.sha256(data).hexdigest()


def _verify_source_bytes(data: bytes, expected_sha256: str) -> None:
    """Verify exact bytes against the manifest's declared SHA-256."""
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):
        raise UniverseCandidateError("source_sha256_mismatch")


@dataclass(frozen=True, slots=True)
class PageEvidence:
    """Caller-declared paging facts for one captured source file.

    These values are declarations, not proof of remote pagination or source
    authenticity. Missing values deliberately downgrade the source assessment
    to ``UNKNOWN`` instead of being filled in from row counts.
    """

    page_sequence: int
    selector: str | None = None
    terminal: bool | None = None
    reported_total: int | None = None
    reported_total_basis: ReportedTotalBasis | str | None = None
    declared_row_count: int | None = None
    request_cursor: str | None = None
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        """Validate paging declarations without repairing or inferring them."""
        if type(self.page_sequence) is not int or not 1 <= self.page_sequence <= 4_096:
            raise UniverseCandidateError("page_sequence_invalid")
        if self.selector is not None:
            _require_text(self.selector, "page_selector", 128)
        if self.terminal is not None and type(self.terminal) is not bool:
            raise UniverseCandidateError("page_terminal_invalid")
        if self.reported_total is not None and (
            type(self.reported_total) is not int or self.reported_total < 0
        ):
            raise UniverseCandidateError("reported_total_invalid")
        if self.reported_total_basis is not None:
            try:
                basis = ReportedTotalBasis(self.reported_total_basis)
            except (TypeError, ValueError):
                raise UniverseCandidateError("reported_total_basis_invalid") from None
            object.__setattr__(self, "reported_total_basis", basis)
        if (self.reported_total is None) != (self.reported_total_basis is None):
            raise UniverseCandidateError("reported_total_pair_invalid")
        if self.declared_row_count is not None and (
            type(self.declared_row_count) is not int or self.declared_row_count < 0
        ):
            raise UniverseCandidateError("declared_row_count_invalid")
        for label in ("request_cursor", "next_cursor"):
            value = getattr(self, label)
            if value is not None and (type(value) is not str or len(value) > 512):
                raise UniverseCandidateError(f"{label}_invalid")


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """One manifest entry pointing at exact captured catalog bytes."""

    source_id: str
    capture_id: str
    market: Market | str
    as_of: datetime
    format: EvidenceFormat | str
    path: str
    sha256: str
    mapping: Mapping[str, Any] = field(default_factory=dict, repr=False)
    page: PageEvidence | None = None

    def __post_init__(self) -> None:
        """Validate identifiers, timestamp, digest, mapping, and paging facts."""
        if type(self.source_id) is not str or _SOURCE_ID_PATTERN.fullmatch(self.source_id) is None:
            raise UniverseCandidateError("source_id_invalid")
        if (
            type(self.capture_id) is not str
            or _CAPTURE_ID_PATTERN.fullmatch(self.capture_id) is None
        ):
            raise UniverseCandidateError("capture_id_invalid")
        try:
            market = Market(self.market)
        except (TypeError, ValueError):
            raise UniverseCandidateError("market_invalid") from None
        object.__setattr__(self, "market", market)
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise UniverseCandidateError("source_as_of_invalid")
        if self.as_of.utcoffset() is None:
            raise UniverseCandidateError("source_as_of_invalid")
        try:
            evidence_format = EvidenceFormat(self.format)
        except (TypeError, ValueError):
            raise UniverseCandidateError("source_format_invalid") from None
        object.__setattr__(self, "format", evidence_format)
        _require_text(self.path, "source_path", 4_096)
        if not Path(self.path).is_absolute():
            raise UniverseCandidateError("source_path_not_absolute")
        object.__setattr__(self, "sha256", _require_sha256(self.sha256, "source_sha256"))
        if not isinstance(self.mapping, Mapping) or len(self.mapping) > 16:
            raise UniverseCandidateError("source_mapping_invalid")
        copied_mapping: dict[str, Any] = {}
        for key, value in self.mapping.items():
            if type(key) is not str or _FIELD_PATTERN.fullmatch(key) is None:
                raise UniverseCandidateError("source_mapping_field_invalid")
            if value is not None and (type(value) is not str or len(value) > 128):
                raise UniverseCandidateError("source_mapping_value_invalid")
            copied_mapping[key] = value
        object.__setattr__(self, "mapping", MappingProxyType(copied_mapping))
        if self.page is not None and not isinstance(self.page, PageEvidence):
            raise UniverseCandidateError("source_page_invalid")
        if evidence_format is EvidenceFormat.SSE_INSTRUMENT_PAGES_JSON:
            expected_market = Market.CN_SSE
            if market is not expected_market or self.page is None:
                raise UniverseCandidateError("sse_source_contract_invalid")
            if self.page.selector is None:
                raise UniverseCandidateError("sse_source_selector_required")
            try:
                SSEInstrumentSelector(self.page.selector)
            except (TypeError, ValueError):
                raise UniverseCandidateError("sse_source_selector_invalid") from None
        if evidence_format is EvidenceFormat.SZSE_CURRENT_A_XLSX and market is not Market.CN_SZSE:
            raise UniverseCandidateError("szse_source_market_invalid")
        if evidence_format is EvidenceFormat.FUYAO_INSTRUMENT_PAGES_JSON and self.page is None:
            raise UniverseCandidateError("fuyao_source_page_required")

    @property
    def exchange(self) -> str:
        """Return the explicit exchange qualifier for this declared market."""
        return _MARKET_EXCHANGE[self.market.value]


@dataclass(frozen=True, slots=True)
class UniverseCandidateManifest:
    """A bounded set of explicitly declared source captures."""

    schema: str
    as_of: datetime
    sources: tuple[SourceEvidence, ...]

    def __post_init__(self) -> None:
        """Require the expected schema, an aware as-of, and unique captures."""
        if self.schema != MANIFEST_SCHEMA:
            raise UniverseCandidateError("manifest_schema_invalid")
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise UniverseCandidateError("manifest_as_of_invalid")
        if self.as_of.utcoffset() is None:
            raise UniverseCandidateError("manifest_as_of_invalid")
        if isinstance(self.sources, (str, bytes, bytearray, Mapping)) or not isinstance(
            self.sources, Iterable
        ):
            raise UniverseCandidateError("manifest_sources_invalid")
        sources = tuple(islice(iter(self.sources), MAX_SOURCES + 1))
        if not sources or len(sources) > MAX_SOURCES:
            raise UniverseCandidateError("manifest_sources_invalid")
        if any(not isinstance(source, SourceEvidence) for source in sources):
            raise UniverseCandidateError("manifest_sources_invalid")
        keys = {
            (source.source_id, source.capture_id, source.path)
            for source in sources
            if isinstance(source, SourceEvidence)
        }
        if len(keys) != len(sources):
            raise UniverseCandidateError("manifest_source_duplicate")
        object.__setattr__(self, "sources", sources)


def _manifest_from_mapping(raw: object) -> UniverseCandidateManifest:
    """Convert a parsed manifest document into validated evidence DTOs."""
    if not isinstance(raw, Mapping):
        raise UniverseCandidateError("manifest_invalid")
    allowed = {"schema", "as_of", "sources"}
    if set(raw) != allowed:
        raise UniverseCandidateError("manifest_fields_invalid")
    schema = raw["schema"]
    as_of = _parse_instant(raw["as_of"], "manifest_as_of")
    entries = raw["sources"]
    if not isinstance(entries, list) or not entries:
        raise UniverseCandidateError("manifest_sources_invalid")
    sources = [_source_from_mapping(entry) for entry in entries]
    return UniverseCandidateManifest(schema=schema, as_of=as_of, sources=tuple(sources))


def _source_from_mapping(raw: object) -> SourceEvidence:
    """Convert one manifest source object into a validated evidence DTO."""
    if not isinstance(raw, Mapping):
        raise UniverseCandidateError("manifest_source_invalid")
    allowed = {
        "source_id",
        "capture_id",
        "market",
        "as_of",
        "format",
        "path",
        "sha256",
        "mapping",
        "page",
    }
    if not set(raw).issubset(allowed) or not {
        "source_id",
        "capture_id",
        "market",
        "as_of",
        "format",
        "path",
        "sha256",
    }.issubset(raw):
        raise UniverseCandidateError("manifest_source_fields_invalid")
    mapping_raw = raw.get("mapping", {})
    if not isinstance(mapping_raw, Mapping):
        raise UniverseCandidateError("source_mapping_invalid")
    page_raw = raw.get("page")
    page = _page_from_mapping(page_raw) if page_raw is not None else None
    return SourceEvidence(
        source_id=raw["source_id"],
        capture_id=raw["capture_id"],
        market=raw["market"],
        as_of=_parse_instant(raw["as_of"], "source_as_of"),
        format=raw["format"],
        path=raw["path"],
        sha256=raw["sha256"],
        mapping=dict(mapping_raw),
        page=page,
    )


def _page_from_mapping(raw: object) -> PageEvidence:
    """Convert one optional manifest page object into validated paging facts."""
    if not isinstance(raw, Mapping):
        raise UniverseCandidateError("source_page_invalid")
    allowed = {
        "page_sequence",
        "selector",
        "terminal",
        "reported_total",
        "reported_total_basis",
        "declared_row_count",
        "request_cursor",
        "next_cursor",
    }
    if not set(raw).issubset(allowed):
        raise UniverseCandidateError("source_page_fields_invalid")
    return PageEvidence(
        page_sequence=raw.get("page_sequence", 1),
        selector=raw.get("selector"),
        terminal=raw.get("terminal"),
        reported_total=raw.get("reported_total"),
        reported_total_basis=raw.get("reported_total_basis"),
        declared_row_count=raw.get("declared_row_count"),
        request_cursor=raw.get("request_cursor"),
        next_cursor=raw.get("next_cursor"),
    )


def load_manifest(path: str | Path) -> tuple[UniverseCandidateManifest, str]:
    """Load and hash one strict manifest file without touching other paths.

    Args:
        path: Absolute path to a regular, non-symlinked manifest file.

    Returns:
        The validated manifest and the SHA-256 of its exact bytes.

    Raises:
        UniverseCandidateError: If the path, JSON, or manifest contract fails.
    """
    data, digest = _read_bounded_regular_file(str(path), MAX_MANIFEST_BYTES)
    raw = _parse_json_bytes(data, "manifest")
    return _manifest_from_mapping(raw), digest


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """One normalized source row with exact capture and row provenance."""

    market: Market
    symbol: str | None
    source_native_code: str | None
    source_id: str
    capture_id: str
    as_of: datetime
    page_sequence: int
    source_page_number: int | str | None
    source_row_number: int
    file_sha256: str
    source_row_sha256: str
    disposition: RowDisposition
    reason: str | None
    display_name: str | None
    source_exchange: str | None
    asset_type: str | None
    source_status: FrozenScalar
    source_context: str | None

    def as_dict(self) -> dict[str, object]:
        """Render this row as a deterministic JSON-compatible object."""
        return {
            "market": self.market.value,
            "symbol": self.symbol,
            "source_native_code": self.source_native_code,
            "source_id": self.source_id,
            "capture_id": self.capture_id,
            "as_of": _iso_utc(self.as_of),
            "page_sequence": self.page_sequence,
            "source_page_number": self.source_page_number,
            "source_row_number": self.source_row_number,
            "file_sha256": self.file_sha256,
            "source_row_sha256": self.source_row_sha256,
            "disposition": self.disposition.value,
            "reason": self.reason,
            "display_name": self.display_name,
            "source_exchange": self.source_exchange,
            "asset_type": self.asset_type,
            "source_status": self.source_status,
            "source_context": self.source_context,
        }


@dataclass(frozen=True, slots=True)
class _SourceOutcome:
    """Internal per-capture parse result used for counts and paging review."""

    source: SourceEvidence
    records: tuple[CandidateRecord, ...]
    pagination_status: str
    pagination_reasons: tuple[str, ...]
    page_count: int
    row_count: int


@dataclass(frozen=True, slots=True)
class CurrentAShareUniverseCandidate:
    """Deterministic candidate snapshot with explicit non-attestation fields."""

    manifest_sha256: str
    manifest_as_of: datetime
    snapshot_as_of: datetime
    sources: tuple[SourceEvidence, ...]
    outcomes: tuple[_SourceOutcome, ...]
    records: tuple[CandidateRecord, ...]
    candidates: tuple[Mapping[str, object], ...]
    duplicates: tuple[Mapping[str, object], ...]
    conflicts: tuple[Mapping[str, object], ...]
    counts: Mapping[str, object]
    pagination: Mapping[str, object]
    bse_supported: str

    def as_dict(self) -> dict[str, object]:
        """Render the complete snapshot as a deterministic JSON object."""
        return {
            "schema_version": SCHEMA_VERSION,
            "candidate_kind": "current_a_share_universe_candidate",
            "as_of": _iso_utc(self.snapshot_as_of),
            "manifest": {
                "schema": MANIFEST_SCHEMA,
                "sha256": self.manifest_sha256,
                "as_of": _iso_utc(self.manifest_as_of),
            },
            "source_manifests": [
                {
                    "source_id": source.source_id,
                    "capture_id": source.capture_id,
                    "market": source.market.value,
                    "format": source.format.value,
                    "as_of": _iso_utc(source.as_of),
                    "path": source.path,
                    "sha256": source.sha256,
                    "page_sequence": source.page.page_sequence if source.page else None,
                }
                for source in sorted(
                    self.sources,
                    key=lambda item: (
                        item.market.value,
                        item.source_id,
                        item.capture_id,
                        item.page.page_sequence if item.page else 0,
                        item.path,
                    ),
                )
            ],
            "source_assessments": [_source_summary(outcome) for outcome in self.outcomes],
            "counts": dict(self.counts),
            "pagination": dict(self.pagination),
            "retention": {
                "status": "UNKNOWN",
                "detail": (
                    "No source-retention, historical-availability, or delisted-history "
                    "evidence is included in this candidate."
                ),
            },
            "unknowns": {
                "pagination_termination": "UNKNOWN",
                "retention": "UNKNOWN",
                "market_completeness": "UNKNOWN",
                "source_authenticity": "UNKNOWN",
                "delisted_lifecycle": "NOT_INCLUDED",
                "identity_review": "REQUIRED",
            },
            "bse_supported": self.bse_supported,
            "universe_census_attested": False,
            "delisted_lifecycle_included": False,
            "authoritative": False,
            "publication": "NOT_PERFORMED",
            "database_or_api_published": False,
            "candidates": list(self.candidates),
            "records": [record.as_dict() for record in self.records],
            "duplicates": list(self.duplicates),
            "conflicts": list(self.conflicts),
        }


def _source_summary(outcome: _SourceOutcome) -> dict[str, object]:
    """Render one verified source capture with its paging assessment."""
    source = outcome.source
    return {
        "source_id": source.source_id,
        "capture_id": source.capture_id,
        "market": source.market.value,
        "format": source.format.value,
        "as_of": _iso_utc(source.as_of),
        "path": source.path,
        "sha256": source.sha256,
        "page_count": outcome.page_count,
        "row_count": outcome.row_count,
        "pagination_status": outcome.pagination_status,
        "pagination_reasons": list(outcome.pagination_reasons),
    }


def render_snapshot(snapshot: CurrentAShareUniverseCandidate) -> str:
    """Serialize a snapshot with sorted keys and a trailing newline.

    Args:
        snapshot: Built candidate snapshot.

    Returns:
        Deterministic UTF-8 JSON text.
    """
    return (
        json.dumps(
            snapshot.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def _mapping_text(mapping: Mapping[str, Any], key: str, *, required: bool = False) -> str | None:
    """Read one validated explicit mapping value."""
    value = mapping.get(key)
    if value is None:
        if required:
            raise UniverseCandidateError(f"source_mapping_{key}_required")
        return None
    if type(value) is not str or not value:
        raise UniverseCandidateError(f"source_mapping_{key}_invalid")
    return value


def _field_value(row: Mapping[str, FrozenScalar], field_name: str | None) -> FrozenScalar:
    """Return a selected field value or None when unmapped or absent."""
    if field_name is None:
        return None
    return row.get(field_name)


def _parse_native_code(native: object, market: Market) -> tuple[str | None, str | None, str | None]:
    """Parse only explicit code forms, returning symbol, exchange, and reason."""
    if type(native) is not str or not native or len(native) > 32:
        return None, None, "source_code_missing_or_not_text"
    bare = _BARE_SYMBOL_PATTERN.fullmatch(native)
    if bare is not None:
        return bare.group(1), None, None
    suffixed = _SUFFIX_SYMBOL_PATTERN.fullmatch(native)
    if suffixed is not None:
        return suffixed.group(1), suffixed.group(2), None
    prefixed = _PREFIX_SYMBOL_PATTERN.fullmatch(native)
    if prefixed is not None:
        return prefixed.group(2), prefixed.group(1), None
    return None, None, "source_code_unrecognized"


def _bse_reserved_prefix(symbol: str) -> bool:
    """Identify explicit BSE code prefixes without using them for market inference."""
    return symbol.startswith(_BSE_PREFIXES)


def _record_from_row(
    *,
    row: Mapping[str, FrozenScalar],
    source: SourceEvidence,
    mapping: Mapping[str, Any],
    page_sequence: int,
    source_page_number: int | str | None,
    source_row_number: int,
    source_context: str | None,
    default_asset_type: str | None = None,
) -> CandidateRecord:
    """Normalize one flat row using only declared mappings and explicit code forms."""
    symbol_field = _mapping_text(mapping, "symbol_field", required=True)
    name_field = _mapping_text(mapping, "name_field")
    exchange_field = _mapping_text(mapping, "exchange_field")
    asset_type_field = _mapping_text(mapping, "asset_type_field")
    asset_type_value = _mapping_text(mapping, "asset_type_value")
    status_field = _mapping_text(mapping, "status_field")
    if default_asset_type is not None and asset_type_value is None:
        asset_type_value = default_asset_type
    if asset_type_value is not None and asset_type_field is None:
        raise UniverseCandidateError("source_mapping_asset_type_field_required")

    native_value = _field_value(row, symbol_field)
    native = native_value if type(native_value) is str else None
    symbol, suffix_exchange, code_reason = _parse_native_code(native, source.market)
    observed_exchange_value = _field_value(row, exchange_field)
    observed_exchange = observed_exchange_value if type(observed_exchange_value) is str else None
    name_value = _field_value(row, name_field)
    display_name = name_value if type(name_value) is str and name_value else None
    asset_value = _field_value(row, asset_type_field)
    asset_type = asset_value if type(asset_value) is str else None

    disposition = RowDisposition.CANDIDATE
    reason: str | None = code_reason
    if symbol is None:
        disposition = RowDisposition.UNKNOWN
    elif suffix_exchange is not None and suffix_exchange != source.exchange:
        disposition = RowDisposition.CONFLICT
        reason = "source_code_exchange_conflict"
    elif source.market is not Market.CN_BSE and _bse_reserved_prefix(symbol):
        disposition = RowDisposition.CONFLICT
        reason = "bse_reserved_prefix_outside_declared_market"
    elif observed_exchange is not None:
        if observed_exchange not in _EXCHANGES:
            disposition = RowDisposition.CONFLICT
            reason = "source_exchange_field_unrecognized"
        elif observed_exchange != source.exchange:
            disposition = RowDisposition.CONFLICT
            reason = "source_exchange_field_conflict"
    if disposition is RowDisposition.CANDIDATE and asset_type_value is not None:
        if asset_value is None:
            disposition = RowDisposition.UNSUPPORTED
            reason = "asset_type_missing"
        elif asset_value != asset_type_value:
            disposition = RowDisposition.UNSUPPORTED
            reason = "asset_type_not_supported"

    return CandidateRecord(
        market=source.market,
        symbol=symbol,
        source_native_code=native,
        source_id=source.source_id,
        capture_id=source.capture_id,
        as_of=source.as_of,
        page_sequence=page_sequence,
        source_page_number=source_page_number,
        source_row_number=source_row_number,
        file_sha256=source.sha256,
        source_row_sha256=_canonical_row_sha256(row),
        disposition=disposition,
        reason=reason,
        display_name=display_name,
        source_exchange=observed_exchange,
        asset_type=asset_type,
        source_status=_field_value(row, status_field),
        source_context=source_context,
    )


@dataclass(slots=True)
class _ReadBudget:
    """Track cumulative bytes read across one manifest build."""

    used: int = 0


def _read_source(source: SourceEvidence, budget: _ReadBudget) -> bytes:
    """Read one manifest source, verify its digest, and charge the read budget."""
    data, _ = _read_bounded_regular_file(source.path, MAX_SOURCE_BYTES)
    budget.used += len(data)
    if budget.used > MAX_TOTAL_SOURCE_BYTES:
        raise UniverseCandidateError("snapshot_total_bytes_exceeded")
    _verify_source_bytes(data, source.sha256)
    return data


def _parse_normalized_source(source: SourceEvidence, budget: _ReadBudget) -> _SourceOutcome:
    """Parse one normalized JSON row-list capture."""
    data = _read_source(source, budget)
    raw = _parse_json_bytes(data, "normalized_rows")
    if isinstance(raw, Mapping):
        if set(raw) != {"rows"} or not isinstance(raw["rows"], list):
            raise UniverseCandidateError("normalized_rows_shape_invalid")
        rows = raw["rows"]
    elif isinstance(raw, list):
        rows = raw
    else:
        raise UniverseCandidateError("normalized_rows_shape_invalid")
    if len(rows) > MAX_ROWS:
        raise UniverseCandidateError("normalized_rows_count_invalid")
    page_sequence = source.page.page_sequence if source.page is not None else 1
    records = tuple(
        _record_from_row(
            row=_validate_flat_row(row),
            source=source,
            mapping=source.mapping,
            page_sequence=page_sequence,
            source_page_number=None,
            source_row_number=index,
            source_context=source.format.value,
        )
        for index, row in enumerate(rows, start=1)
    )
    status, reasons = _assess_pagination(source, (source.page,), len(records))
    return _SourceOutcome(
        source=source,
        records=records,
        pagination_status=status,
        pagination_reasons=reasons,
        page_count=1 if source.page is not None else 0,
        row_count=len(records),
    )


def _xlsx_rows(result: SzseLifecycleXlsxResult) -> Iterable[tuple[int, Mapping[str, FrozenScalar]]]:
    """Convert raw workbook rows into header-keyed flat rows."""
    headers = result.headers
    for row in result.rows:
        values: dict[str, FrozenScalar] = {}
        for cell in row.cells:
            if 1 <= cell.column_index <= len(headers):
                values[headers[cell.column_index - 1]] = cell.text_value
        yield row.source_row_number, MappingProxyType(values)


def _parse_szse_xlsx_source(source: SourceEvidence, budget: _ReadBudget) -> _SourceOutcome:
    """Parse one captured SZSE current-A workbook with explicit column mapping."""
    symbol_column = _mapping_text(source.mapping, "symbol_column", required=True)
    name_column = _mapping_text(source.mapping, "name_column")
    if symbol_column is None:
        raise UniverseCandidateError("source_mapping_symbol_column_required")
    if _COLUMN_PATTERN.fullmatch(symbol_column) is None:
        raise UniverseCandidateError("source_mapping_symbol_column_invalid")
    if name_column is not None and _COLUMN_PATTERN.fullmatch(name_column) is None:
        raise UniverseCandidateError("source_mapping_name_column_invalid")
    data = _read_source(source, budget)
    try:
        result = parse_szse_lifecycle_xlsx(data, dataset_class="current_a")
    except SzseLifecycleXlsxError:
        raise UniverseCandidateError("szse_xlsx_parse_failed") from None
    headers = result.headers
    symbol_index = _column_index(symbol_column)
    name_index = _column_index(name_column) if name_column is not None else None
    if symbol_index > len(headers) or (name_index is not None and name_index > len(headers)):
        raise UniverseCandidateError("source_mapping_column_out_of_range")
    mapping: dict[str, Any] = {"symbol_field": headers[symbol_index - 1]}
    if name_index is not None:
        mapping["name_field"] = headers[name_index - 1]
    records: list[CandidateRecord] = []
    for row_number, values in _xlsx_rows(result):
        records.append(
            _record_from_row(
                row=values,
                source=source,
                mapping=mapping,
                page_sequence=1,
                source_page_number=None,
                source_row_number=row_number,
                source_context="szse.current_a_xlsx",
            )
        )
    status, reasons = _assess_pagination(source, (source.page,), len(records))
    return _SourceOutcome(
        source=source,
        records=tuple(records),
        pagination_status=status,
        pagination_reasons=reasons,
        page_count=1 if source.page is not None else 0,
        row_count=len(records),
    )


def _column_index(letters: str | None) -> int:
    """Convert validated Excel column letters to a one-based index."""
    if letters is None:
        raise UniverseCandidateError("source_mapping_column_invalid")
    index = 0
    for character in letters:
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index


def _group_sources(
    sources: Iterable[SourceEvidence],
) -> dict[tuple[str, str], tuple[SourceEvidence, ...]]:
    """Group page entries into capture runs by source and capture identifier."""
    grouped: dict[tuple[str, str], list[SourceEvidence]] = defaultdict(list)
    for source in sources:
        grouped[(source.source_id, source.capture_id)].append(source)
    return {
        key: tuple(sorted(entries, key=lambda item: item.page.page_sequence if item.page else 0))
        for key, entries in grouped.items()
    }


def _validate_capture_group(entries: tuple[SourceEvidence, ...]) -> SourceEvidence:
    """Require one market, format, and as-of for all pages in a capture group."""
    first = entries[0]
    if any(
        entry.market is not first.market
        or entry.format is not first.format
        or entry.as_of != first.as_of
        for entry in entries
    ):
        raise UniverseCandidateError("capture_group_context_mismatch")
    sequences = [entry.page.page_sequence if entry.page else 0 for entry in entries]
    if any(sequence < 1 for sequence in sequences):
        raise UniverseCandidateError("capture_page_sequence_required")
    if len(sequences) != len(set(sequences)):
        raise UniverseCandidateError("capture_page_sequence_duplicate")
    return first


def _parse_sse_capture_group(
    entries: tuple[SourceEvidence, ...], budget: _ReadBudget
) -> _SourceOutcome:
    """Parse a raw SSE page group, preserving gaps as UNKNOWN pagination."""
    first = _validate_capture_group(entries)
    inputs: list[SSEInstrumentPageInput] = []
    for entry in entries:
        if entry.page is None or entry.page.selector is None:
            raise UniverseCandidateError("sse_source_page_required")
        body = _read_source(entry, budget)
        inputs.append(
            SSEInstrumentPageInput(
                selector=entry.page.selector,
                sequence_number=entry.page.page_sequence,
                raw_response_body=body,
                declared_response_sha256=entry.sha256,
                request_summary=None,
                http_status_code=200,
            )
        )
    parsed_pages: list[tuple[SourceEvidence, Any]] = []
    try:
        capture = build_sse_instrument_census(inputs, observed_at=first.as_of)
    except SSEInstrumentCensusError:
        parsed_pages = _parse_sse_pages_individually(entries, inputs, first.as_of)
    else:
        bodies = {entry.page.page_sequence: entry for entry in entries if entry.page is not None}
        parsed_pages = [(bodies[page.sequence_number], page) for page in capture.pages]
    records: list[CandidateRecord] = []
    for entry, page in parsed_pages:
        for row_index, source_row in enumerate(page.source_rows, start=1):
            records.append(
                _record_from_row(
                    row=_validate_flat_row(source_row.raw_row),
                    source=entry,
                    mapping=entry.mapping,
                    page_sequence=entry.page.page_sequence if entry.page else 1,
                    source_page_number=_sse_page_number(page.page_help),
                    source_row_number=row_index,
                    source_context=page.selector.value,
                )
            )
    return _outcome_from_entries(entries, tuple(records), len(entries))


def _parse_sse_pages_individually(
    entries: tuple[SourceEvidence, ...],
    inputs: list[SSEInstrumentPageInput],
    observed_at: datetime,
) -> list[tuple[SourceEvidence, Any]]:
    """Parse each page alone so a missing page becomes UNKNOWN, not data loss."""
    parsed: list[tuple[SourceEvidence, Any]] = []
    for entry, page_input in zip(entries, inputs, strict=True):
        single = SSEInstrumentPageInput(
            selector=page_input.selector,
            sequence_number=1,
            raw_response_body=page_input.raw_response_body,
            declared_response_sha256=page_input.declared_response_sha256,
            request_summary=None,
            http_status_code=200,
        )
        try:
            capture = build_sse_instrument_census([single], observed_at=observed_at)
        except SSEInstrumentCensusError:
            raise UniverseCandidateError("sse_capture_invalid") from None
        parsed.append((entry, capture.pages[0]))
    return parsed


def _sse_page_number(page_help: Mapping[str, object] | None) -> int | str | None:
    """Read only an explicitly retained SSE page number, if present."""
    if page_help is None:
        return None
    value = page_help.get("pageNo")
    return value if type(value) is int or isinstance(value, str) else None


def _parse_fuyao_capture_group(
    entries: tuple[SourceEvidence, ...], budget: _ReadBudget
) -> _SourceOutcome:
    """Parse a contiguous group of raw Fuyao catalog response pages."""
    first = _validate_capture_group(entries)
    inputs: list[FuyaoInstrumentPageInput] = []
    bodies: dict[int, SourceEvidence] = {}
    for entry in entries:
        if entry.page is None:
            raise UniverseCandidateError("fuyao_source_page_required")
        body = _read_source(entry, budget)
        inputs.append(
            FuyaoInstrumentPageInput(
                sequence_number=entry.page.page_sequence,
                raw_response_body=body,
                declared_response_sha256=entry.sha256,
                request_summary=None,
                source_page_number=None,
                http_status_code=200,
                request_cursor=entry.page.request_cursor,
                next_cursor=entry.page.next_cursor,
                terminal=entry.page.terminal,
                reported_total=entry.page.reported_total,
            )
        )
        bodies[entry.page.page_sequence] = entry
    try:
        capture = build_fuyao_instrument_census(inputs, observed_at=first.as_of)
    except FuyaoInstrumentCensusError:
        raise UniverseCandidateError("fuyao_capture_invalid") from None
    records: list[CandidateRecord] = []
    for page in capture.source_census.pages:
        entry = bodies[page.sequence_number]
        for row_index, observation in enumerate(page.rows, start=1):
            records.append(
                _record_from_row(
                    row=_validate_flat_row(observation.raw_row),
                    source=entry,
                    mapping=_fuyao_mapping(entry.mapping),
                    page_sequence=page.sequence_number,
                    source_page_number=page.source_page_number,
                    source_row_number=row_index,
                    source_context="fuyao_a_share_catalog",
                    default_asset_type="a-share",
                )
            )
    return _outcome_from_entries(entries, tuple(records), len(entries))


def _fuyao_mapping(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    """Fill the documented Fuyao profile field names only when not overridden."""
    defaults: dict[str, Any] = {
        "symbol_field": "thscode",
        "name_field": "name",
        "exchange_field": "exchange",
        "asset_type_field": "asset_type",
        "status_field": "status",
    }
    merged = dict(defaults)
    merged.update(mapping)
    return merged


def _outcome_from_entries(
    entries: tuple[SourceEvidence, ...], records: tuple[CandidateRecord, ...], page_count: int
) -> _SourceOutcome:
    """Assess one capture group's page declarations and package its rows."""
    first = entries[0]
    status, reasons = _assess_pagination(
        first, tuple(entry.page for entry in entries), len(records)
    )
    return _SourceOutcome(
        source=first,
        records=records,
        pagination_status=status,
        pagination_reasons=reasons,
        page_count=page_count,
        row_count=len(records),
    )


def _assess_pagination(
    source: SourceEvidence,
    pages: tuple[PageEvidence | None, ...],
    row_count: int,
) -> tuple[str, tuple[str, ...]]:
    """Assess caller-declared page facts without upgrading them to proof."""
    reasons: list[str] = []

    def add(reason: str) -> None:
        """Append each stable reason code once."""
        if reason not in reasons and reason in _PAGE_REASONS:
            reasons.append(reason)

    if not pages or any(page is None for page in pages):
        add("no_pagination_evidence")
        return "UNKNOWN", tuple(reasons)
    typed_pages: list[PageEvidence] = [page for page in pages if page is not None]
    sequences = [page.page_sequence for page in typed_pages]
    if sequences != list(range(1, len(typed_pages) + 1)):
        add("page_sequence_gap")
    if any(page.terminal is None for page in typed_pages):
        add("terminal_evidence_missing")
    if typed_pages[-1].terminal is False:
        add("final_page_not_terminal")
    for index, page in enumerate(typed_pages):
        if index < len(typed_pages) - 1:
            if page.terminal is True:
                add("nonfinal_page_marked_terminal")
            if page.next_cursor is None:
                add("nonfinal_page_without_next_cursor")
        elif page.next_cursor is not None:
            add("final_page_has_next_cursor")
    if typed_pages[0].request_cursor is not None:
        add("initial_cursor_not_empty")
    for current, following in zip(typed_pages, typed_pages[1:], strict=False):
        if current.next_cursor != following.request_cursor:
            add("cursor_chain_broken")

    totals = [page.reported_total for page in typed_pages]
    if any(total is None for total in totals):
        add("reported_total_missing")
    present_totals = {total for total in totals if total is not None}
    if len(present_totals) > 1:
        add("reported_total_conflict")
    bases = {
        ReportedTotalBasis(page.reported_total_basis)
        for page in typed_pages
        if page.reported_total is not None and page.reported_total_basis is not None
    }
    if any(
        page.reported_total is not None and page.reported_total_basis is None
        for page in typed_pages
    ):
        add("reported_total_basis_missing")
    if len(bases) > 1:
        add("reported_total_basis_conflict")
    if len(present_totals) == 1 and len(bases) == 1:
        total = next(iter(present_totals))
        basis = next(iter(bases))
        if basis is ReportedTotalBasis.ROW_COUNT and total != row_count:
            add("row_count_mismatch")
        if basis is ReportedTotalBasis.PAGE_COUNT and total != len(typed_pages):
            add("page_count_mismatch")
    if any(
        page.declared_row_count is not None and page.declared_row_count != row_count
        for page in typed_pages
    ):
        add("declared_row_count_mismatch")
    if source.page is not None and len(typed_pages) == 1:
        declared = typed_pages[0].declared_row_count
        if declared is not None and declared != row_count:
            add("declared_row_count_mismatch")
    return ("DECLARED_CONSISTENT" if not reasons else "UNKNOWN"), tuple(reasons)


def _record_ref(record: CandidateRecord) -> dict[str, object]:
    """Render a compact immutable record reference for review lists."""
    return {
        "source_id": record.source_id,
        "capture_id": record.capture_id,
        "page_sequence": record.page_sequence,
        "source_row_number": record.source_row_number,
        "file_sha256": record.file_sha256,
        "source_row_sha256": record.source_row_sha256,
        "disposition": record.disposition.value,
        "reason": record.reason,
    }


def _reconcile(
    records: Iterable[CandidateRecord],
) -> tuple[
    tuple[CandidateRecord, ...],
    tuple[Mapping[str, object], ...],
    tuple[Mapping[str, object], ...],
    tuple[Mapping[str, object], ...],
]:
    """Report duplicates and display-name conflicts without merging them."""
    ordered = sorted(
        records,
        key=lambda record: (
            record.market.value,
            record.symbol or "",
            record.source_id,
            record.capture_id,
            record.page_sequence,
            record.source_row_number,
        ),
    )
    groups: dict[tuple[Market, str], list[CandidateRecord]] = defaultdict(list)
    for record in ordered:
        if record.disposition is RowDisposition.CANDIDATE and record.symbol is not None:
            groups[(record.market, record.symbol)].append(record)

    conflict_groups: list[Mapping[str, object]] = []
    duplicate_groups: list[Mapping[str, object]] = []
    candidates: list[Mapping[str, object]] = []
    conflicting_keys: set[tuple[Market, str]] = set()
    for (market, symbol), members in sorted(
        groups.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        names = {member.display_name for member in members if member.display_name is not None}
        source_ids = sorted({member.source_id for member in members})
        capture_ids = sorted({member.capture_id for member in members})
        if len(members) > 1:
            duplicate_groups.append(
                {
                    "market": market.value,
                    "symbol": symbol,
                    "agreement": len(names) <= 1,
                    "record_count": len(members),
                    "source_count": len(source_ids),
                    "source_ids": source_ids,
                    "capture_ids": capture_ids,
                    "records": [_record_ref(member) for member in members],
                }
            )
        if len(names) > 1:
            conflicting_keys.add((market, symbol))
            conflict_groups.append(
                {
                    "kind": (
                        "cross_source_display_name_disagreement"
                        if len(source_ids) > 1
                        else "same_source_display_name_disagreement"
                    ),
                    "market": market.value,
                    "symbol": symbol,
                    "values": sorted(
                        (
                            {
                                "value": member.display_name,
                                "source_id": member.source_id,
                                "capture_id": member.capture_id,
                                "source_row_number": member.source_row_number,
                            }
                            for member in members
                            if member.display_name is not None
                        ),
                        key=lambda item: (
                            str(item["value"]),
                            str(item["source_id"]),
                            str(item["capture_id"]),
                            int(item["source_row_number"]),
                        ),
                    ),
                    "source_ids": source_ids,
                    "records": [_record_ref(member) for member in members],
                }
            )
            continue
        display_name = next(
            (member.display_name for member in members if member.display_name is not None),
            None,
        )
        candidates.append(
            {
                "market": market.value,
                "symbol": symbol,
                "display_name": display_name,
                "record_count": len(members),
                "source_ids": source_ids,
                "capture_ids": capture_ids,
            }
        )

    final_records: list[CandidateRecord] = []
    for record in ordered:
        if record.symbol is not None and (record.market, record.symbol) in conflicting_keys:
            record = replace(
                record,
                disposition=RowDisposition.CONFLICT,
                reason="display_name_disagreement",
            )
        final_records.append(record)
    final_by_key: dict[tuple[Market, str], list[CandidateRecord]] = defaultdict(list)
    for record in final_records:
        if record.symbol is not None:
            final_by_key[(record.market, record.symbol)].append(record)
    for item in duplicate_groups:
        key = (Market(str(item["market"])), str(item["symbol"]))
        item["records"] = [_record_ref(record) for record in final_by_key.get(key, [])]
    for item in conflict_groups:
        key = (Market(str(item["market"])), str(item["symbol"]))
        item["records"] = [_record_ref(record) for record in final_by_key.get(key, [])]
    return (
        tuple(final_records),
        tuple(candidates),
        tuple(duplicate_groups),
        tuple(conflict_groups),
    )


def _counts(
    records: tuple[CandidateRecord, ...], sources: tuple[SourceEvidence, ...]
) -> Mapping[str, object]:
    """Aggregate record dispositions by market and source."""
    by_market: dict[str, dict[str, int]] = {
        market.value: {
            "records": 0,
            "candidates": 0,
            "conflicts": 0,
            "unknown": 0,
            "unsupported": 0,
        }
        for market in Market
    }
    by_source: dict[str, dict[str, int]] = {}
    totals = {
        "records": 0,
        "candidates": 0,
        "conflicts": 0,
        "unknown": 0,
        "unsupported": 0,
    }
    for record in records:
        market_counts = by_market[record.market.value]
        source_counts = by_source.setdefault(
            record.source_id,
            {"records": 0, "candidates": 0, "conflicts": 0, "unknown": 0, "unsupported": 0},
        )
        disposition = record.disposition.value
        bucket = {
            "candidate": "candidates",
            "conflict": "conflicts",
            "unknown": "unknown",
            "unsupported": "unsupported",
        }[disposition]
        totals["records"] += 1
        totals[bucket] += 1
        market_counts["records"] += 1
        market_counts[bucket] += 1
        source_counts["records"] += 1
        source_counts[bucket] += 1
    for source in sources:
        by_source.setdefault(
            source.source_id,
            {"records": 0, "candidates": 0, "conflicts": 0, "unknown": 0, "unsupported": 0},
        )
    return {
        "records_total": totals["records"],
        "candidates_total": totals["candidates"],
        "conflicts_total": totals["conflicts"],
        "unknown_total": totals["unknown"],
        "unsupported_total": totals["unsupported"],
        "by_market": by_market,
        "by_source": {key: by_source[key] for key in sorted(by_source)},
    }


def _pagination_summary(outcomes: tuple[_SourceOutcome, ...]) -> Mapping[str, object]:
    """Summarize per-source declared paging evidence without attesting it."""
    sources = [
        {
            "source_id": outcome.source.source_id,
            "capture_id": outcome.source.capture_id,
            "market": outcome.source.market.value,
            "status": outcome.pagination_status,
            "reasons": list(outcome.pagination_reasons),
            "pages": outcome.page_count,
            "rows": outcome.row_count,
        }
        for outcome in outcomes
    ]
    all_consistent = bool(sources) and all(
        source["status"] == "DECLARED_CONSISTENT" for source in sources
    )
    return {
        "status": "DECLARED_CONSISTENT" if all_consistent else "UNKNOWN",
        "attested": False,
        "detail": (
            "Declared page facts are self-consistency checks only; they do not prove "
            "source pagination termination, retention, or market completeness."
        ),
        "sources": sources,
    }


def _source_outcomes(manifest: UniverseCandidateManifest) -> tuple[_SourceOutcome, ...]:
    """Parse all manifest sources, grouping raw page captures when required."""
    outcomes: list[_SourceOutcome] = []
    raw_sources: list[SourceEvidence] = []
    budget = _ReadBudget()
    for source in sorted(
        manifest.sources,
        key=lambda item: (
            item.market.value,
            item.source_id,
            item.capture_id,
            item.page.page_sequence if item.page else 0,
            item.path,
        ),
    ):
        if source.format is EvidenceFormat.NORMALIZED_ROWS_JSON:
            outcomes.append(_parse_normalized_source(source, budget))
        elif source.format is EvidenceFormat.SZSE_CURRENT_A_XLSX:
            outcomes.append(_parse_szse_xlsx_source(source, budget))
        else:
            raw_sources.append(source)
    for entries in _group_sources(raw_sources).values():
        first = entries[0]
        if first.format is EvidenceFormat.SSE_INSTRUMENT_PAGES_JSON:
            outcomes.append(_parse_sse_capture_group(entries, budget))
        elif first.format is EvidenceFormat.FUYAO_INSTRUMENT_PAGES_JSON:
            outcomes.append(_parse_fuyao_capture_group(entries, budget))
        else:
            raise UniverseCandidateError("source_format_invalid")
    if sum(outcome.row_count for outcome in outcomes) > MAX_ROWS:
        raise UniverseCandidateError("snapshot_row_limit_exceeded")
    return tuple(
        sorted(
            outcomes,
            key=lambda item: (
                item.source.market.value,
                item.source.source_id,
                item.source.capture_id,
            ),
        )
    )


def build_universe_candidate(
    manifest: UniverseCandidateManifest, *, manifest_sha256: str
) -> CurrentAShareUniverseCandidate:
    """Build a deterministic candidate snapshot from a validated manifest.

    Args:
        manifest: Bounded manifest of already captured catalog files.
        manifest_sha256: SHA-256 of the exact manifest bytes that produced it.

    Returns:
        Candidate snapshot with records, duplicates, conflicts, counts, and
        explicit non-attestation fields.

    Raises:
        UniverseCandidateError: If source bytes, hashes, formats, or mappings
            fail validation or exceed their bounds.
    """
    if not isinstance(manifest, UniverseCandidateManifest):
        raise UniverseCandidateError("manifest_required")
    digest = _require_sha256(manifest_sha256, "manifest_sha256")
    outcomes = _source_outcomes(manifest)
    records = tuple(record for outcome in outcomes for record in outcome.records)
    final_records, candidates, duplicates, conflicts = _reconcile(records)
    if sum(len(outcome.records) for outcome in outcomes) != len(records):
        raise UniverseCandidateError("source_outcome_inconsistent")
    bse_supported = (
        "true" if any(outcome.source.market is Market.CN_BSE for outcome in outcomes) else "unknown"
    )
    snapshot_as_of = max([manifest.as_of] + [outcome.source.as_of for outcome in outcomes])
    return CurrentAShareUniverseCandidate(
        manifest_sha256=digest,
        manifest_as_of=manifest.as_of,
        snapshot_as_of=snapshot_as_of,
        sources=manifest.sources,
        outcomes=outcomes,
        records=final_records,
        candidates=candidates,
        duplicates=duplicates,
        conflicts=tuple(
            list(conflicts)
            + [
                {
                    "kind": "row_identity_conflict",
                    "market": record.market.value,
                    "symbol": record.symbol,
                    "reason": record.reason,
                    "record": _record_ref(record),
                }
                for record in final_records
                if record.disposition is RowDisposition.CONFLICT
                and record.reason != "display_name_disagreement"
            ]
        ),
        counts={
            **_counts(final_records, manifest.sources),
            "candidate_identities_total": len(candidates),
        },
        pagination=_pagination_summary(outcomes),
        bse_supported=bse_supported,
    )
