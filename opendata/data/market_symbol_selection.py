"""Exact market/symbol selection profile for published business-date series.

The reserved profile deliberately supports one complete market and symbol
pair. It does not define wildcard, prefix, list, or source-specific matching
semantics and does not alter the canonical dataset contract representation.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from pydantic_core import PydanticSerializationError

from opendata.data.platform_contracts import DatasetContract, canonical_sha256

__all__ = [
    "MAX_MARKET_LENGTH",
    "MAX_SYMBOL_LENGTH",
    "MARKET_SYMBOL_FILTER_FIELDS",
    "MarketSymbolSelection",
    "MarketSymbolSelectionError",
    "parse_market_symbol_selection",
    "selection_profile",
    "validate_selector_pair",
]

MARKET_SYMBOL_FILTER_FIELDS: Final[tuple[str, str]] = ("market", "symbol")
"""Exact ordered filter-field tuple reserved for market/symbol reads."""

MAX_MARKET_LENGTH: Final[int] = 32
"""Maximum number of Unicode characters accepted for a market value."""
MAX_SYMBOL_LENGTH: Final[int] = 128
"""Maximum number of Unicode characters accepted for a symbol value."""

_WILDCARD_CHARACTERS: Final[frozenset[str]] = frozenset("*?%_[]")
_PROFILE_DATASET: Final[Literal["dataset"]] = "dataset"
_PROFILE_MARKET_SYMBOL: Final[Literal["market_symbol_v1"]] = "market_symbol_v1"


class MarketSymbolSelectionError(ValueError):
    """Invalid market/symbol selection or malformed reserved contract."""


@dataclass(frozen=True, slots=True)
class MarketSymbolSelection:
    """Canonical pair selecting one exact market and symbol value."""

    market: str
    symbol: str

    def __post_init__(self) -> None:
        """Require exact built-in strings without trimming or case folding."""
        _validate_market(self.market)
        _validate_symbol(self.symbol)


def selection_profile(contract: DatasetContract) -> Literal["dataset", "market_symbol_v1"]:
    """Return the exact market/symbol profile after revalidating the contract.

    Args:
        contract: Contract used by a publication or read boundary.

    Returns:
        ``market_symbol_v1`` only for the exact reserved ordered filter pair;
        ``dataset`` for other valid filter-field tuples.

    Raises:
        MarketSymbolSelectionError: If the contract graph is invalid or its
            reserved profile does not describe a business-date time series.
    """
    if type(contract) is not DatasetContract:
        raise MarketSymbolSelectionError("dataset contract is invalid")
    try:
        # Contracts are attribute-frozen but contain mutable nested mappings.
        # Revalidate a detached copy before applying profile-specific rules.
        # Promote serializer warnings to errors so malformed model_copy graphs
        # cannot leak caller-controlled values through a warning message.
        canonical_sha256(contract)
        validated_contract = DatasetContract.model_validate(
            contract.model_dump(mode="python", warnings="error"),
            strict=True,
        )
    except (PydanticSerializationError, TypeError, ValueError, RecursionError, OverflowError):
        raise MarketSymbolSelectionError("dataset contract is invalid") from None

    if validated_contract.filter_fields != MARKET_SYMBOL_FILTER_FIELDS:
        return _PROFILE_DATASET

    if (
        validated_contract.record_kind != "time_series"
        or validated_contract.temporal_semantics != "business_date"
        or validated_contract.time_field is None
    ):
        raise MarketSymbolSelectionError("reserved selection contract is invalid")

    fields = {field.name: field for field in validated_contract.fields}
    for name in MARKET_SYMBOL_FILTER_FIELDS:
        field = fields.get(name)
        if (
            field is None
            or field.data_type != "string"
            or field.nullable
            or name not in validated_contract.business_key
        ):
            raise MarketSymbolSelectionError("reserved selection contract is invalid")

    time_field = fields.get(validated_contract.time_field)
    if (
        time_field is None
        or time_field.data_type != "date"
        or validated_contract.time_field not in validated_contract.business_key
    ):
        raise MarketSymbolSelectionError("reserved selection contract is invalid")
    return _PROFILE_MARKET_SYMBOL


def validate_selector_pair(
    *, market: object | None, symbol: object | None
) -> MarketSymbolSelection | None:
    """Validate an optional complete pair, returning ``None`` only if absent.

    Args:
        market: Exact market value, or ``None`` when omitted.
        symbol: Exact symbol value, or ``None`` when omitted.

    Returns:
        The immutable pair, or ``None`` when both selectors are absent.

    Raises:
        MarketSymbolSelectionError: If the pair is partial or either value is
            empty, padded, a wildcard, a control character, or not a string.
    """
    if market is None and symbol is None:
        return None
    if market is None or symbol is None:
        raise MarketSymbolSelectionError("market and symbol are required together")
    return MarketSymbolSelection(
        market=_validate_market(market),
        symbol=_validate_symbol(symbol),
    )


def parse_market_symbol_selection(
    contract: DatasetContract,
    *,
    market: object | None,
    symbol: object | None,
    required: bool,
    instrument_id: object | None = None,
    capture_id: object | None = None,
) -> MarketSymbolSelection | None:
    """Validate selectors against the exact contract profile.

    Args:
        contract: Registered contract at the publication or read boundary.
        market: Requested exact market value, if supplied.
        symbol: Requested exact symbol value, if supplied.
        required: Whether the reserved profile requires both selector values.
        instrument_id: Legacy Sina selector, which cannot be mixed with this
            profile.
        capture_id: Legacy Sina selector, which cannot be mixed with this
            profile.

    Returns:
        A validated pair for the reserved profile, or ``None`` for an
        unselected dataset-profile request.

    Raises:
        MarketSymbolSelectionError: If the profile is malformed, a dataset
            contract receives selectors, a required pair is absent, or a Sina
            selector is mixed with this profile.
    """
    if type(required) is not bool:
        raise MarketSymbolSelectionError("selection requirement is invalid")
    profile = selection_profile(contract)
    selection = validate_selector_pair(market=market, symbol=symbol)
    if profile == _PROFILE_DATASET:
        if selection is not None:
            raise MarketSymbolSelectionError("dataset contract does not accept selectors")
        return None
    if instrument_id is not None or capture_id is not None:
        raise MarketSymbolSelectionError("selection profiles are mutually exclusive")
    if required and selection is None:
        raise MarketSymbolSelectionError("market and symbol are required together")
    return selection


def _validate_market(value: object) -> str:
    """Require a non-empty exact market string without wildcard syntax."""
    return _validate_selector_value(value, "market")


def _validate_symbol(value: object) -> str:
    """Require a non-empty exact symbol string without wildcard syntax."""
    return _validate_selector_value(value, "symbol")


def _validate_selector_value(value: object, name: Literal["market", "symbol"]) -> str:
    """Validate a selection token without normalizing or echoing its value."""
    max_length = MAX_MARKET_LENGTH if name == "market" else MAX_SYMBOL_LENGTH
    if (
        type(value) is not str
        or not value
        or len(value) > max_length
        or not value.strip()
        or value.strip() != value
        or any(character in _WILDCARD_CHARACTERS for character in value)
        or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)
    ):
        raise MarketSymbolSelectionError(f"{name} is invalid")
    return value
