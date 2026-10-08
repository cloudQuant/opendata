"""Controlled ECB euro-reference-rate query and Fetcher template."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import ClassVar, Literal, cast

from pydantic import Field, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.currency import CurrencyReferenceRate  # noqa: TC001
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models._reference_client import fetch_reference_rates
from opendata.data.providers.ecb.models._reference_rates import (
    RawReferenceRateRecord,
    ReferenceRatesParseError,
    normalize_reference_rates,
)

_CURRENCY_CODE = re.compile(r"[A-Z][A-Z0-9_-]{0,31}\Z")
_MAX_QUOTE_CURRENCIES = 32
_MAX_RECORDS = 10_000


class EcbCurrencyReferenceRatesQuery(QueryParams):
    """Validated daily EUR-reference query for a bounded set of quote currencies."""

    quote_currencies: tuple[StrictStr, ...]
    source: Literal["auto", "ecb"] = "auto"
    market: Literal["eu"] | None = None
    symbol: None = None
    start_date: date | None = None
    end_date: date | None = None
    max_records: int = Field(default=_MAX_RECORDS, strict=True, ge=1, le=_MAX_RECORDS)

    @field_validator("quote_currencies", mode="before")
    @classmethod
    def _require_sequence(cls, value: object) -> tuple[object, ...]:
        if type(value) not in (list, tuple):
            raise ValueError("quote_currencies must be a list or tuple")
        sequence = cast("list[object] | tuple[object, ...]", value)
        if not sequence or len(sequence) > _MAX_QUOTE_CURRENCIES:
            raise ValueError("quote_currencies must contain between 1 and 32 currencies")
        return tuple(sequence)

    @field_validator("quote_currencies")
    @classmethod
    def _validate_currency_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("quote_currencies must not contain duplicates")
        if any(_CURRENCY_CODE.fullmatch(code) is None for code in value):
            raise ValueError("quote_currencies contains an unsafe currency dimension token")
        return value

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_date_input(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is date:
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date must be a canonical YYYY-MM-DD date") from exc
            if parsed.isoformat() != value:
                raise ValueError("date must be a canonical YYYY-MM-DD date")
            return parsed
        if isinstance(value, datetime):
            raise ValueError("date must not include a time")
        raise ValueError("date must be a date or canonical YYYY-MM-DD string")

    @model_validator(mode="after")
    def _validate_date_window(self) -> EcbCurrencyReferenceRatesQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must not precede start_date")
        return self


class EcbCurrencyReferenceRatesFetcher(
    Fetcher[EcbCurrencyReferenceRatesQuery, tuple[RawReferenceRateRecord, ...]]
):
    """Fetch and normalize controlled ECB daily EUR-reference rates."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CurrencyReferenceRates"
    capability: ClassVar[Capability] = Capability(
        asset_class="currency",
        domain="currency_reference_rates",
        period="1D",
        market="eu",
        source="ecb",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EcbCurrencyReferenceRatesQuery:
        """Validate Fetcher keyword arguments as the fixed ECB query schema."""
        return EcbCurrencyReferenceRatesQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: EcbCurrencyReferenceRatesQuery,
        ctx: FetchContext,
    ) -> tuple[RawReferenceRateRecord, ...]:
        """Fetch source-native rows once through the governed ECB transport."""
        return fetch_reference_rates(params, timeout=ctx.timeout)

    def transform_data(
        self,
        raw: tuple[RawReferenceRateRecord, ...],
        params: EcbCurrencyReferenceRatesQuery,
    ) -> tuple[CurrencyReferenceRate, ...]:
        """Normalize every source row and reject rows outside the requested window."""
        try:
            records = normalize_reference_rates(raw)
        except ReferenceRatesParseError:
            raise EcbProviderError("ECB_BAD_OBSERVATION") from None

        requested_currencies = set(params.quote_currencies)
        for record in records:
            if record.quote_currency not in requested_currencies:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            if params.start_date is not None and record.date < params.start_date:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
            if params.end_date is not None and record.date > params.end_date:
                raise EcbProviderError("ECB_BAD_OBSERVATION")
        return records
