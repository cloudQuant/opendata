"""Exact instrument/capture selection profile for published datasets.

The reserved profile is intentionally narrower than a general filter system.
It binds API selection to two non-null business-key fields without changing
the canonical representation of existing dataset contracts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from pydantic_core import PydanticSerializationError

from opendata.data.platform_contracts import DatasetContract, canonical_sha256

__all__ = [
    "INSTRUMENT_CAPTURE_FILTER_FIELDS",
    "InstrumentCaptureSelection",
    "InstrumentCaptureSelectionError",
    "parse_instrument_capture_selection",
    "selection_profile",
    "validate_selector_pair",
]

INSTRUMENT_CAPTURE_FILTER_FIELDS: Final[tuple[str, str]] = ("instrument_id", "capture_id")
"""Exact ordered filter-field tuple reserved for instrument/capture reads."""

_INSTRUMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]*", re.ASCII)
_CAPTURE_ID_PATTERN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_PROFILE_DATASET: Final[Literal["dataset"]] = "dataset"
_PROFILE_INSTRUMENT_CAPTURE: Final[Literal["instrument_capture_v1"]] = "instrument_capture_v1"


class InstrumentCaptureSelectionError(ValueError):
    """Invalid selection input or malformed reserved dataset contract."""


@dataclass(frozen=True, slots=True)
class InstrumentCaptureSelection:
    """Canonical pair identifying one instrument's immutable capture scope."""

    instrument_id: str
    capture_id: str

    def __post_init__(self) -> None:
        """Require exact built-in strings and canonical selector identifiers."""
        _validate_instrument_id(self.instrument_id)
        _validate_capture_id(self.capture_id)


def selection_profile(
    contract: DatasetContract,
) -> Literal["dataset", "instrument_capture_v1"]:
    """Return the exact selector profile after revalidating the contract graph.

    Args:
        contract: Contract used by the publication or read boundary.

    Returns:
        ``instrument_capture_v1`` only for the exact reserved ordered pair;
        ``dataset`` for all other valid filter-field tuples.

    Raises:
        InstrumentCaptureSelectionError: If the object is not an exact
            ``DatasetContract``, its graph is invalid, or its reserved profile
            does not satisfy the table-selection shape.
    """
    if type(contract) is not DatasetContract:
        raise InstrumentCaptureSelectionError("dataset contract is invalid")
    try:
        # Contracts are attribute-frozen but contain mutable nested mappings.
        # The canonicalizer bounds the original graph. Strictly validating a
        # Python-mode dump then rejects model_copy list/field bypasses and gives
        # shape checks a detached normalized object, without mutating the input.
        # Promote serializer warnings to errors before Pydantic can emit their
        # messages, which may include values from a malformed model_copy graph.
        canonical_sha256(contract)
        validated_contract = DatasetContract.model_validate(
            contract.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except (PydanticSerializationError, TypeError, ValueError, RecursionError, OverflowError):
        raise InstrumentCaptureSelectionError("dataset contract is invalid") from None

    if validated_contract.filter_fields != INSTRUMENT_CAPTURE_FILTER_FIELDS:
        return _PROFILE_DATASET

    if (
        validated_contract.record_kind != "table"
        or validated_contract.temporal_semantics != "none"
        or validated_contract.time_field is not None
        or validated_contract.pit_policy != "unsupported"
    ):
        raise InstrumentCaptureSelectionError("reserved selection contract is invalid")

    fields = {field.name: field for field in validated_contract.fields}
    for name in INSTRUMENT_CAPTURE_FILTER_FIELDS:
        field = fields.get(name)
        if (
            field is None
            or field.data_type != "string"
            or field.nullable
            or name not in validated_contract.business_key
        ):
            raise InstrumentCaptureSelectionError("reserved selection contract is invalid")
    return _PROFILE_INSTRUMENT_CAPTURE


def validate_selector_pair(
    *, instrument_id: object | None, capture_id: object | None
) -> InstrumentCaptureSelection | None:
    """Validate an optional exact pair, returning ``None`` only when both are absent.

    Args:
        instrument_id: Instrument identifier or ``None`` when omitted.
        capture_id: Lowercase SHA-256 run identity or ``None`` when omitted.

    Returns:
        An immutable canonical selector pair, or ``None`` if both inputs are
        absent.

    Raises:
        InstrumentCaptureSelectionError: If the pair is partial or either
            identifier is not in its canonical form.
    """
    if instrument_id is None and capture_id is None:
        return None
    if instrument_id is None or capture_id is None:
        raise InstrumentCaptureSelectionError("instrument_id and capture_id are required together")
    return InstrumentCaptureSelection(
        instrument_id=_validate_instrument_id(instrument_id),
        capture_id=_validate_capture_id(capture_id),
    )


def parse_instrument_capture_selection(
    contract: DatasetContract,
    *,
    instrument_id: object | None,
    capture_id: object | None,
    required: bool,
) -> InstrumentCaptureSelection | None:
    """Validate selectors against the exact contract profile.

    Args:
        contract: Registered contract at the publication or read boundary.
        instrument_id: Requested instrument selector, if supplied.
        capture_id: Requested capture selector, if supplied.
        required: Whether the reserved profile requires both selector values.

    Returns:
        A validated pair for the reserved profile, or ``None`` for an
        unselected legacy dataset request.

    Raises:
        InstrumentCaptureSelectionError: If the profile is malformed, a
            dataset contract receives selectors, or a required pair is absent.
    """
    if type(required) is not bool:
        raise InstrumentCaptureSelectionError("selection requirement is invalid")
    profile = selection_profile(contract)
    selection = validate_selector_pair(
        instrument_id=instrument_id,
        capture_id=capture_id,
    )
    if profile == _PROFILE_DATASET:
        if selection is not None:
            raise InstrumentCaptureSelectionError("dataset contract does not accept selectors")
        return None
    if required and selection is None:
        raise InstrumentCaptureSelectionError("instrument_id and capture_id are required together")
    return selection


def _validate_instrument_id(value: object) -> str:
    """Enforce the existing ASCII identifier grammar without echoing input."""
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or _INSTRUMENT_ID_PATTERN.fullmatch(value) is None
    ):
        raise InstrumentCaptureSelectionError("instrument_id is invalid")
    return value


def _validate_capture_id(value: object) -> str:
    """Require the lowercase 64-hex RunIdentity idempotency-key form."""
    if type(value) is not str or _CAPTURE_ID_PATTERN.fullmatch(value) is None:
        raise InstrumentCaptureSelectionError("capture_id is invalid")
    return value
