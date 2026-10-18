"""Bounded NOAA/NWS bulletin document download."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import ClassVar, Literal, TypeGuard
from urllib.parse import urlsplit

from pydantic import model_validator

from opendata.data.capability import Capability
from opendata.data.models.government_us import WeatherBulletinDownload
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.government_us.models import _client

NWS_DOWNLOAD_HOSTS = frozenset(
    {"api.weather.gov", "weather.gov", "www.weather.gov", "forecast.weather.gov"}
)
MAX_DOCUMENTS = 10
#: The media types the fetcher's own ``Accept`` header asks NWS for. ``_client.get_bytes`` strips
#: parameters and lowercases, and an archive that announces nothing arrives as the empty type, so
#: that is the fourth member rather than a permissive hole.
DOCUMENT_MEDIA_TYPES = frozenset({"application/octet-stream", "application/pdf", "text/plain", ""})


class WeatherBulletinDownloadQuery(QueryParams):
    """Accept a bounded set of official NOAA/NWS bulletin URLs."""

    source: Literal["government_us"] = "government_us"
    market: Literal["us"] = "us"
    urls: object

    @model_validator(mode="after")
    def _validate_urls(self) -> WeatherBulletinDownloadQuery:
        if self.start_date is not None or self.end_date is not None or self.symbol is not None:
            raise ValueError("NWS document download accepts URLs only")
        urls = _flatten_urls(self.urls)
        if not urls or len(urls) > MAX_DOCUMENTS:
            raise ValueError("NWS document download accepts one to ten URLs")
        normalized: list[str] = []
        for url in urls:
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or parts.hostname is None
                or parts.hostname.lower() not in NWS_DOWNLOAD_HOSTS
                or parts.username is not None
                or parts.password is not None
                or parts.port not in (None, 443)
                or parts.fragment
                or parts.query
                or len(parts.path) > 2_000
            ):
                raise ValueError("NWS download URL must be a plain official HTTPS URL")
            normalized.append(url)
        if len(set(normalized)) != len(normalized):
            raise ValueError("NWS download URLs must not repeat")
        object.__setattr__(self, "urls", tuple(normalized))
        return self


def _flatten_urls(value: object) -> list[str]:
    if isinstance(value, str):
        url = value.strip()
        if not url or len(url) > 2_048:
            raise ValueError("NWS download URL is blank or too long")
        return [url]
    if isinstance(value, (list, tuple)):
        urls: list[str] = []
        for item in value:
            urls.extend(_flatten_urls(item))
            if len(urls) > MAX_DOCUMENTS:
                raise ValueError("NWS document download accepts at most ten URLs")
        return urls
    if isinstance(value, Mapping):
        allowed = {"url", "download_url", "href"}
        if len(value) != 1 or not set(value).issubset(allowed):
            raise ValueError("NWS URL objects require one url, download_url, or href field")
        return _flatten_urls(next(iter(value.values())))
    raise ValueError("NWS download input must contain URLs")


def _is_validated_urls(value: object) -> TypeGuard[tuple[str, ...]]:
    """Narrow the tuple established by query validation without coercing it again."""
    return isinstance(value, tuple) and all(isinstance(url, str) for url in value)


class WeatherBulletinDownloadFetcher(
    Fetcher[WeatherBulletinDownloadQuery, list[tuple[bytes, str, str]]],
):
    """Download original bulletin bytes and encode them as base64 text."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "WeatherBulletinDownload"
    row_model: ClassVar[type[WeatherBulletinDownload]] = WeatherBulletinDownload
    capability: ClassVar[Capability] = Capability(
        asset_class="weather",
        domain="government_us_weather_bulletin_download",
        period="event",
        market="us",
        source="government_us",
        verified=False,
        notes="NWS bytes are preserved in the documented base64 document contract.",
    )

    def transform_query(self, **kwargs: object) -> WeatherBulletinDownloadQuery:
        """Validate URL allowlist and count before any HTTP request."""
        return WeatherBulletinDownloadQuery.model_validate(kwargs)

    def extract_data(
        self, params: WeatherBulletinDownloadQuery, ctx: FetchContext
    ) -> list[tuple[bytes, str, str]]:
        """Retrieve each fixed official URL once; redirects are rejected.

        A zero-length answer is not a bulletin and an interstitial page is not one either, so both
        are refused here, before any of the bytes reach ``transform_data`` to be base64-encoded into
        a ``content`` column that would look well-formed either way.
        """
        urls = params.urls
        if not _is_validated_urls(urls):
            raise ValueError("NWS URLs must be validated before fetching")
        results: list[tuple[bytes, str, str]] = []
        for url in urls:
            body, content_type, _ = _client.get_bytes(
                url,
                params=None,
                headers={"Accept": "application/octet-stream, application/pdf, text/plain"},
                context=ctx,
                source="government_us",
                allowed_hosts=NWS_DOWNLOAD_HOSTS,
                maximum_bytes=_client.DOCUMENT_RESPONSE_LIMIT,
            )
            if not body:
                raise _client.GovernmentUSProviderError("NWS_DOCUMENT_EMPTY_BODY")
            if content_type not in DOCUMENT_MEDIA_TYPES:
                raise _client.GovernmentUSProviderError("NWS_DOCUMENT_BAD_CONTENT_TYPE")
            results.append((body, content_type or "application/octet-stream", url))
        return results

    def transform_data(
        self,
        raw: list[tuple[bytes, str, str]],
        params: WeatherBulletinDownloadQuery,
    ) -> FetchResult:
        """Return one base64 record per downloaded document, preserving type."""
        return tuple(
            WeatherBulletinDownload(
                content=base64.b64encode(body).decode("ascii"),
                data_format={
                    "media_type": content_type,
                    "encoding": "base64",
                    "source_url": url,
                },
            )
            for body, content_type, url in raw
        )
