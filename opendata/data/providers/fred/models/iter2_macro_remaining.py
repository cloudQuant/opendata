"""Evidence-backed additions for the partial FRED retail-price model.

This module adds the unambiguous public ``gas`` selector while keeping the
existing partial mappings and their fail-closed behavior. The BLS average
price contract distinguishes utility (piped) gas, measured per therm, from
automotive gasoline and diesel.
"""

from __future__ import annotations

from pydantic import model_validator

from opendata.data.models.fred_macro_iter2 import FredRetailPrice
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.iter2_macro import (
    _RETAIL_AREA_CODES,
    _RETAIL_AREA_NAMES,
    _RETAIL_ITEM_CODES,
    _RETAIL_ITEM_DESCRIPTIONS,
    _RETAIL_SELECTOR_GAPS,
    RetailPricesFetcher,
    RetailPricesQuery,
)

_UTILITY_GAS_ITEM_CODE = "72620"
_UTILITY_GAS_DESCRIPTION = "Utility (piped) gas per therm — All piped gas"
_SUPPORTED_ITEMS = frozenset((*_RETAIL_ITEM_CODES, "gas"))


class RetailPricesRemainingQuery(RetailPricesQuery):
    """Retail query contract with the official piped-gas item added."""

    @model_validator(mode="after")
    def _require_exact_item_mapping(self) -> RetailPricesRemainingQuery:
        item = self.item
        if item is None:
            raise ValueError("item=None selects the unsupported default full fuel basket")
        if item not in _SUPPORTED_ITEMS:
            reason = _RETAIL_SELECTOR_GAPS.get(
                item,
                "the public selector has no verified BLS average-price mapping",
            )
            raise ValueError(f"RetailPrices item '{item}' is not mapped: {reason}")
        if self.country != "united_states":
            raise ValueError("only United States BLS average-price series are mapped")
        return self


class RetailPricesRemainingFetcher(RetailPricesFetcher):
    """Add the official piped-gas series to the existing partial adapter."""

    canonical_model = "RetailPrices"
    query_model = RetailPricesRemainingQuery

    def selected_series(self, params: RetailPricesQuery) -> list[tuple[str, str]]:
        """Return the exact BLS AP series for the selected item and area."""
        if (
            not isinstance(params, RetailPricesRemainingQuery)
            or type(params) is not RetailPricesRemainingQuery
        ):
            raise FredProviderError("FRED_BAD_QUERY") from None
        item = params.item
        if item is None:
            raise FredProviderError("FRED_BAD_QUERY") from None
        area_code = _RETAIL_AREA_CODES[params.region]
        item_code = _UTILITY_GAS_ITEM_CODE if item == "gas" else _RETAIL_ITEM_CODES.get(item)
        if item_code is None:
            raise FredProviderError("FRED_BAD_QUERY") from None
        return [(item, f"APU{area_code}{item_code}")]

    def transform_data(
        self, raw: list[dict[str, object]], params: RetailPricesQuery
    ) -> tuple[FredRetailPrice, ...]:
        """Validate source identities and serialize the typed retail output."""
        if (
            not isinstance(params, RetailPricesRemainingQuery)
            or type(params) is not RetailPricesRemainingQuery
        ):
            raise FredProviderError("FRED_BAD_QUERY") from None
        if params.item is None:
            raise FredProviderError("FRED_BAD_QUERY") from None
        item = params.item
        expected = set(self.selected_series(params))
        rows: list[FredRetailPrice] = []
        for (
            selection,
            series_id,
            observation_date,
            value,
            realtime_start,
            realtime_end,
        ) in self._validate_raw(raw, params):
            if (selection, series_id) not in expected:
                raise FredProviderError("FRED_BAD_OBSERVATION") from None
            description = (
                _UTILITY_GAS_DESCRIPTION if item == "gas" else _RETAIL_ITEM_DESCRIPTIONS[item]
            )
            row = FredRetailPrice.model_validate(
                {
                    "date": observation_date,
                    "symbol": series_id,
                    "country": "united_states",
                    "description": (
                        f"{description} in {_RETAIL_AREA_NAMES[params.region]}, "
                        "average price, not seasonally adjusted"
                    ),
                    "value": value,
                }
            )
            row._series_ids = (series_id,)
            row._realtime_by_series = {series_id: (realtime_start, realtime_end)}
            rows.append(row)
        return self._sort(rows, params)


__all__ = (
    "RetailPricesRemainingFetcher",
    "RetailPricesRemainingQuery",
)
