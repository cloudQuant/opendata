"""Biztoc world-news fetcher with the documented RapidAPI header credential."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import quote

from pydantic import ValidationError

from opendata.data.capability import Capability
from opendata.data.models.biztoc import BiztocWorldNews
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    _http_get_json,
)
from opendata.data.providers.biztoc._source import CREDENTIAL_ENV_VAR, SOURCE

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpGet

BASE_URL = "https://biztoc.p.rapidapi.com"
RAPIDAPI_HOST = "biztoc.p.rapidapi.com"
DEFAULT_TIMEOUT_SECONDS = 15.0


class BiztocWorldNewsQuery(QueryParams):
    """World-news filters: a search term, a publisher, or the latest feed."""

    source: Literal["biztoc"] = "biztoc"
    market: Literal["global"] = "global"
    term: str | None = None
    publisher: str | None = None


class BiztocWorldNewsFetcher(Fetcher[BiztocWorldNewsQuery, tuple[BiztocWorldNews, ...]]):
    """Fetch one Biztoc feed and validate every article against the frozen contract."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "WorldNews"
    row_model: ClassVar[type[BiztocWorldNews]] = BiztocWorldNews
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="news",
        domain="biztoc_world_news",
        period="d",
        market="global",
        source="biztoc",
        verified=False,
        notes="Public news feed; live access requires a RapidAPI key, offline acceptance uses a fixture.",
    )

    def transform_query(self, **kwargs: object) -> BiztocWorldNewsQuery:
        """Reject keys the documented query does not accept."""
        return BiztocWorldNewsQuery.model_validate(kwargs)

    def _token(self) -> str:
        token = os.environ.get(CREDENTIAL_ENV_VAR, "").strip()
        if not token:
            raise ProviderEngineError("BIZTOC_CREDENTIAL_MISSING")
        return token

    def _get(self, url: str, ctx: FetchContext) -> Any:  # noqa: ANN401
        transport = self.http_transport or _http_get_json
        timeout = min(float(ctx.timeout or DEFAULT_TIMEOUT_SECONDS), DEFAULT_TIMEOUT_SECONDS)
        response: HttpResponse = transport(
            url,
            {},
            timeout=timeout,
            source=SOURCE,
            headers={
                "X-RapidAPI-Key": self._token(),
                "X-RapidAPI-Host": RAPIDAPI_HOST,
                "Accept": "application/json",
            },
        )
        if not 200 <= response.status < 300:
            code = "BIZTOC_CREDENTIAL_REJECTED" if response.status in (401, 403) else (
                "BIZTOC_HTTP_ERROR"
            )
            raise ProviderEngineError(code, status=response.status, url=url)
        return response.document

    def extract_data(
        self,
        params: BiztocWorldNewsQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Request the selected feed and refuse a non-list envelope."""
        if params.term:
            url = f"{BASE_URL}/search?q={quote(params.term)}"
        elif params.publisher:
            url = f"{BASE_URL}/news/source/{quote(params.publisher.lower())}"
        else:
            url = f"{BASE_URL}/news/latest"
        document = self._get(url, ctx)
        if isinstance(document, dict):
            raise ProviderEngineError("BIZTOC_CREDENTIAL_REJECTED")
        if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
            raise ProviderEngineError("BIZTOC_SHAPE_INVALID")
        return document

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: BiztocWorldNewsQuery,
    ) -> FetchResult:
        """Map the published article fields onto the frozen contract."""
        rows: list[BiztocWorldNews] = []
        for item in raw:
            published = item.get("published")
            if not isinstance(published, str):
                raise ProviderEngineError("BIZTOC_SHAPE_INVALID")
            try:
                moment = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                raise ProviderEngineError("BIZTOC_SHAPE_INVALID") from None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            mapped = {
                "date": moment,
                "title": item.get("title"),
                "author": item.get("author"),
                "excerpt": item.get("excerpt"),
                "body": item.get("body"),
                "images": item.get("img"),
                "tags": item.get("tags"),
                "score": item.get("score"),
                "url": item.get("url"),
            }
            try:
                rows.append(BiztocWorldNews.model_validate(mapped, strict=True))
            except ValidationError:
                raise ProviderEngineError("BIZTOC_SHAPE_INVALID") from None
        return tuple(rows)


__all__ = ["BiztocWorldNewsFetcher", "BiztocWorldNewsQuery"]
