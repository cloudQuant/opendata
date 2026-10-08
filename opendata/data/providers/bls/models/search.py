"""BLS survey-specific bulk-file series catalog search."""

from __future__ import annotations

import re
from typing import ClassVar

from pydantic import Field, field_validator

from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.bls.models import _client
from opendata.data.providers.bls.models._client import BlsProviderError, BlsRawCatalogPage
from opendata.data.providers.bls.models._contracts import BlsCatalogItem, BlsCatalogPage


class BlsSearchQuery(QueryParams):
    """A bounded search within one official BLS survey catalog file."""

    survey: str
    search_text: str | None = None
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=100, ge=1, le=1000, strict=True)

    @field_validator("survey")
    @classmethod
    def _validate_survey(cls, value: str) -> str:
        if re.fullmatch(r"[a-z]{2}", value) is None:
            raise ValueError("survey must be exactly two lowercase letters")
        return value

    @field_validator("search_text")
    @classmethod
    def _validate_search_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("search_text must not be empty when supplied")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("search_text must not contain control characters")
        return value


class BlsSearchFetcher(Fetcher[BlsSearchQuery, BlsRawCatalogPage]):
    """Search all series in one BLS survey's official `.series` file."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "BlsSearch"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="bls_search",
        period="snapshot",
        market="us",
        source="bls",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> BlsSearchQuery:
        """Validate survey path and optional search terms before I/O."""
        return BlsSearchQuery.model_validate(kwargs)

    def extract_data(self, params: BlsSearchQuery, ctx: FetchContext) -> BlsRawCatalogPage:
        """Read and search the complete survey catalog within its byte cap."""
        return _client.fetch_catalog(
            survey=params.survey,
            search_text=params.search_text,
            offset=params.offset,
            limit=params.limit,
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: BlsRawCatalogPage, params: BlsSearchQuery) -> FetchResult:
        """Build typed rows and retain the full filtered match count."""
        del params
        try:
            items = tuple(BlsCatalogItem.model_validate(item) for item in raw.items)
            return BlsCatalogPage(
                items=items,
                total=raw.total,
                offset=raw.offset,
                limit=raw.limit,
            )
        except (TypeError, ValueError):
            raise BlsProviderError("BLS_CATALOG_BAD_RESPONSE") from None
