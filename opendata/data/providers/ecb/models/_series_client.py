"""Governed ECB transport for the fixed YC and BPS series queries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypeVar

from pydantic import ValidationError

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.protocol import QueryParams
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models._reference_client import (
    ECB_REFERENCE_ACCEPT,
    ECB_REFERENCE_BASE_URL,
)
from opendata.data.providers.ecb.models._reference_rates import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_OBSERVATIONS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_XML_NODES,
    ReferenceRatesParseError,
)
from opendata.data.providers.ecb.models._series_query import (
    EcbBalanceOfPaymentsQuery,
    EcbYieldCurveQuery,
)
from opendata.data.providers.ecb.models._series_sdmx import (
    parse_balance_of_payments_generic_data,
    parse_yield_curve_generic_data,
)
from opendata.data.request_budget import (
    RequestAuthorizationError,
    current_request_scopes,
    validate_scope_grants,
)

if TYPE_CHECKING:
    from opendata.data.providers.ecb.models._series_sdmx import RawEcbSeriesRecord

_BASE_URL = ECB_REFERENCE_BASE_URL
_SOURCE = "ecb"
_ACCEPT = ECB_REFERENCE_ACCEPT
_QueryT = TypeVar("_QueryT", bound=QueryParams)

__all__ = ("fetch_balance_of_payments", "fetch_yield_curve")


def _validate_scoped_query(
    query: _QueryT,
    *,
    query_type: type[_QueryT],
    canonical_model: str,
) -> _QueryT:
    """Require the exact live scope before revalidating the query object."""
    scopes = current_request_scopes()
    if not scopes:
        raise RequestAuthorizationError(
            "ECB series requests require an authorized Fetcher execution scope"
        )
    if any(scope.source != _SOURCE or scope.canonical_model != canonical_model for scope in scopes):
        raise RequestAuthorizationError(
            "ECB series requests require the exact source and canonical model scope"
        )
    validate_scope_grants(scopes)

    if type(query) is not query_type:
        raise RequestAuthorizationError("ECB series transport requires the exact query model")
    try:
        return query_type.model_validate(query.model_dump())
    except ValidationError:
        raise RequestAuthorizationError("ECB series query is no longer valid") from None


def _fetch_series(
    *,
    product: Literal["YC", "BPS"],
    series_key: str,
    request_params: dict[str, str],
    max_records: int,
    timeout: float | None,
) -> tuple[RawEcbSeriesRecord, ...]:
    """Issue one governed GET and parse its complete bounded response."""
    if not series_key.startswith(f"{product}."):
        raise RequestAuthorizationError("ECB series query has the wrong product prefix")
    source_series_key = series_key.removeprefix(f"{product}.")
    url = f"{_BASE_URL}/service/data/{product}/{source_series_key}"
    request_params = {
        "format": "genericdata",
        "detail": "full",
        "includeHistory": "false",
        **request_params,
    }

    try:
        response = get_shared_http_client().get(
            url,
            params=request_params,
            headers={"Accept": _ACCEPT},
            timeout=timeout,
            source=_SOURCE,
        )
    except HttpFetchError as exc:
        raise EcbProviderError("ECB_HTTP_ERROR", status=exc.status) from None

    if response.status_code != 200:
        raise EcbProviderError("ECB_HTTP_ERROR", status=response.status_code)

    parser = (
        parse_yield_curve_generic_data
        if product == "YC"
        else parse_balance_of_payments_generic_data
    )
    try:
        return parser(
            response.content,
            max_body_bytes=DEFAULT_MAX_BODY_BYTES,
            max_xml_nodes=DEFAULT_MAX_XML_NODES,
            max_observations=min(max_records, DEFAULT_MAX_OBSERVATIONS),
            max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
        )
    except ReferenceRatesParseError:
        raise EcbProviderError("ECB_BAD_RESPONSE", status=response.status_code) from None


def fetch_yield_curve(
    query: EcbYieldCurveQuery,
    *,
    timeout: float | None = None,
) -> tuple[RawEcbSeriesRecord, ...]:
    """Fetch one bounded YC series under its exact active request grant."""
    validated_query = _validate_scoped_query(
        query,
        query_type=EcbYieldCurveQuery,
        canonical_model="YieldCurve",
    )
    params: dict[str, str] = {}
    if validated_query.start_date is not None:
        params["startPeriod"] = validated_query.start_date.isoformat()
    if validated_query.end_date is not None:
        params["endPeriod"] = validated_query.end_date.isoformat()
    return _fetch_series(
        product="YC",
        series_key=validated_query.series_key,
        request_params=params,
        max_records=validated_query.max_records,
        timeout=timeout,
    )


def fetch_balance_of_payments(
    query: EcbBalanceOfPaymentsQuery,
    *,
    timeout: float | None = None,
) -> tuple[RawEcbSeriesRecord, ...]:
    """Fetch one bounded BPS series under its exact active request grant."""
    validated_query = _validate_scoped_query(
        query,
        query_type=EcbBalanceOfPaymentsQuery,
        canonical_model="BalanceOfPayments",
    )
    params: dict[str, str] = {}
    if validated_query.start_period is not None:
        params["startPeriod"] = validated_query.start_period
    if validated_query.end_period is not None:
        params["endPeriod"] = validated_query.end_period
    return _fetch_series(
        product="BPS",
        series_key=validated_query.series_key,
        request_params=params,
        max_records=validated_query.max_records,
        timeout=timeout,
    )
