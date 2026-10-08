"""Governed ECB transport for the bounded currency-reference fetcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models._reference_rates import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_OBSERVATIONS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_XML_NODES,
    RawReferenceRateRecord,
    ReferenceRatesParseError,
    parse_generic_data,
)
from opendata.data.request_budget import (
    RequestAuthorizationError,
    current_request_scopes,
    validate_scope_grants,
)

if TYPE_CHECKING:
    from opendata.data.providers.ecb.models.reference_rates import (
        EcbCurrencyReferenceRatesQuery,
    )

ECB_REFERENCE_BASE_URL = "https://data-api.ecb.europa.eu"
ECB_REFERENCE_ACCEPT = "application/vnd.sdmx.genericdata+xml;version=2.1"
_SOURCE = "ecb"
_CANONICAL_MODEL = "CurrencyReferenceRates"


def _require_exact_fetcher_scope() -> None:
    scopes = current_request_scopes()
    if not scopes:
        raise RequestAuthorizationError(
            "ECB reference requests require an authorized Fetcher execution scope"
        )
    if any(
        scope.source != _SOURCE or scope.canonical_model != _CANONICAL_MODEL for scope in scopes
    ):
        raise RequestAuthorizationError(
            "ECB reference requests require the exact source and canonical model scope"
        )
    validate_scope_grants(scopes)


def fetch_reference_rates(
    query: EcbCurrencyReferenceRatesQuery,
    *,
    timeout: float | None = None,
) -> tuple[RawReferenceRateRecord, ...]:
    """Fetch and extract raw reference-rate rows under the active exact grant.

    The transport is deliberately reachable only from the matching Fetcher
    scope. The XML parser owns all response interpretation; this function
    neither introduces retries nor promotes the current request operation.
    """
    _require_exact_fetcher_scope()

    # Revalidate the immutable shape at this trust boundary as well as in the
    # Fetcher template, including objects created with Pydantic model_construct.
    from pydantic import ValidationError

    from opendata.data.providers.ecb.models.reference_rates import (
        EcbCurrencyReferenceRatesQuery,
    )

    if type(query) is not EcbCurrencyReferenceRatesQuery:
        raise RequestAuthorizationError("ECB reference transport requires a validated query")
    try:
        validated_query = EcbCurrencyReferenceRatesQuery.model_validate(query.model_dump())
    except ValidationError:
        raise RequestAuthorizationError("ECB reference query is no longer valid") from None

    series_ids = "+".join(validated_query.quote_currencies)
    url = f"{ECB_REFERENCE_BASE_URL}/service/data/EXR/D.{series_ids}.EUR.SP00.A"
    request_params = {
        "format": "genericdata",
        "detail": "full",
        "includeHistory": "false",
    }
    if validated_query.start_date is not None:
        request_params["startPeriod"] = validated_query.start_date.isoformat()
    if validated_query.end_date is not None:
        request_params["endPeriod"] = validated_query.end_date.isoformat()

    try:
        response = get_shared_http_client().get(
            url,
            params=request_params,
            headers={"Accept": ECB_REFERENCE_ACCEPT},
            timeout=timeout,
            source=_SOURCE,
        )
    except HttpFetchError as exc:
        raise EcbProviderError("ECB_HTTP_ERROR", status=exc.status) from None

    if response.status_code != 200:
        raise EcbProviderError("ECB_HTTP_ERROR", status=response.status_code)
    try:
        return parse_generic_data(
            response.content,
            max_body_bytes=DEFAULT_MAX_BODY_BYTES,
            max_xml_nodes=DEFAULT_MAX_XML_NODES,
            max_observations=min(validated_query.max_records, DEFAULT_MAX_OBSERVATIONS),
            max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
        )
    except ReferenceRatesParseError:
        raise EcbProviderError("ECB_BAD_RESPONSE", status=response.status_code) from None
