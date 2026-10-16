"""Bounded FRED observations for the source-derived SLOOS category map."""

from __future__ import annotations

import json
import math
from datetime import date as date_type
from typing import Any, ClassVar

from pydantic import ValidationError

import opendata.core.config as config
from opendata.data.capability import Capability
from opendata.data.models.fred_sloos import (
    FredSeniorLoanOfficerSurvey,
    FredSeniorLoanOfficerSurveyQuery,
    SloosFREDTransform,
    SloosTransform,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.fred.models import _client
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.sloos_series import series_for_category
from opendata.data.request_budget import authorize_request, current_request_scopes

_HOST = "api.stlouisfed.org"
_URL = f"https://{_HOST}/fred/series/observations"
_OBSERVATION_PAGE_LIMIT = 100_000


class FredSloosQueryLimitError(FredProviderError):
    """The complete SLOOS selection exceeds a local request or row bound."""

    def __init__(self, *, limit_kind: str, requested: int, maximum: int) -> None:
        """Report a stable FRED query-limit failure without returning partial rows."""
        super().__init__("FRED_QUERY_LIMIT", url=_URL)
        self.limit_kind = limit_kind
        self.requested = requested
        self.maximum = maximum
        self.partial = False


def _fred_transform(query_transform: SloosTransform | None) -> SloosFREDTransform:
    """Map the public ``None`` selector to FRED's no-transform ``lin`` code."""
    return "lin" if query_transform is None else query_transform


def _preflight_request_allowance(
    *,
    required_requests: int,
    max_requests: int,
) -> None:
    """Reject a partial fan-out before transport if either local cap is short."""
    if required_requests > max_requests:
        raise FredSloosQueryLimitError(
            limit_kind="max_requests",
            requested=required_requests,
            maximum=max_requests,
        )

    scopes = current_request_scopes()
    policy = authorize_request(scopes, "fred", _HOST)
    if policy is None:
        raise FredSloosQueryLimitError(
            limit_kind="request_budget",
            requested=required_requests,
            maximum=0,
        )
    budget = policy.budget
    available = min(
        policy.task_attempts - budget.attempts_used,
        policy.source_attempts - budget.attempts_used_for("fred"),
    )
    available = max(0, available)
    if required_requests > available:
        raise FredSloosQueryLimitError(
            limit_kind="request_budget",
            requested=required_requests,
            maximum=available,
        )


def _parse_date(raw: object) -> date_type:
    """Parse one canonical FRED observation date."""
    if type(raw) is not str or len(raw) != 10:
        raise ValueError("observation date must be canonical YYYY-MM-DD text")
    parsed = date_type.fromisoformat(raw)
    if parsed.isoformat() != raw:
        raise ValueError("observation date must be canonical YYYY-MM-DD text")
    return parsed


def _parse_value(raw: object) -> float | None:
    """Parse FRED's numeric string or its documented dot missing sentinel."""
    if type(raw) is not str:
        raise ValueError("FRED observation value must be text")
    text = raw.strip()
    if text == ".":
        return None
    if not text:
        raise ValueError("FRED observation value must not be empty")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("FRED observation value must be finite")
    return value


def _nonnegative_int(raw: object) -> int | None:
    """Accept only JSON integer response counts and offsets, not bools."""
    return raw if type(raw) is int and raw >= 0 else None


def _fetch_single_observation_page(
    *,
    params: dict[str, str],
    page_size: int,
    max_records: int,
    timeout: float | None,
) -> list[dict[str, Any]]:
    """Read and strictly validate one complete FRED observations page."""
    api_key = _client.require_api_key()
    base_url = config.get_settings().fred_api_base_url or _client.FRED_DEFAULT_BASE_URL
    url = f"{base_url.rstrip('/')}/fred/series/observations"
    request_params = {
        **params,
        "api_key": api_key,
        "file_type": "json",
        "limit": str(page_size),
        "offset": "0",
    }
    status, body = _client._http_get(url, request_params, timeout)
    if status != 200:
        raise FredProviderError("FRED_HTTP_ERROR", status=status, url=url)
    try:
        document = json.loads(body)
    except json.JSONDecodeError as error:
        raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=url) from error
    if not isinstance(document, dict):
        raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=url)

    count = _nonnegative_int(document.get("count"))
    offset = _nonnegative_int(document.get("offset"))
    limit = _nonnegative_int(document.get("limit"))
    observations = document.get("observations")
    if (
        count is None
        or offset != 0
        or limit != page_size
        or not isinstance(observations, list)
        or any(not isinstance(item, dict) for item in observations)
    ):
        raise FredProviderError("FRED_PAGINATION_ERROR", status=status, url=url)
    if count > max_records:
        raise FredSloosQueryLimitError(
            limit_kind="max_records",
            requested=count,
            maximum=max_records,
        )
    if count > page_size:
        raise FredSloosQueryLimitError(
            limit_kind="single_page",
            requested=count,
            maximum=page_size,
        )
    if len(observations) != count:
        raise FredProviderError("FRED_PAGINATION_ERROR", status=status, url=url)
    return observations


class FredSeniorLoanOfficerSurveyFetcher(
    Fetcher[FredSeniorLoanOfficerSurveyQuery, list[dict[str, Any]]]
):
    """Fetch one selected SLOOS category from the official FRED observations API."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "SeniorLoanOfficerSurvey"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="senior_loan_officer_survey",
        period="quarterly",
        market="us",
        source="fred",
        verified=False,
        notes="source-derived candidate map; no live FRED request in this implementation",
    )
    query_model = FredSeniorLoanOfficerSurveyQuery
    output_model = FredSeniorLoanOfficerSurvey
    row_model = FredSeniorLoanOfficerSurvey

    def transform_query(self, **kwargs: object) -> FredSeniorLoanOfficerSurveyQuery:
        """Validate public category, transform, dates, and local request bounds."""
        return self.query_model.model_validate(kwargs)

    def extract_data(
        self,
        params: FredSeniorLoanOfficerSurveyQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Read every selected FRED series in mapping order as one bounded page."""
        series_specs = series_for_category(params.category)
        _preflight_request_allowance(
            required_requests=len(series_specs),
            max_requests=params.max_requests,
        )

        records: list[dict[str, Any]] = []
        records_used = 0
        units = _fred_transform(params.transform)
        for series_spec in series_specs:
            if records_used >= params.max_records:
                raise FredSloosQueryLimitError(
                    limit_kind="max_records",
                    requested=records_used + 1,
                    maximum=params.max_records,
                )
            remaining_records = params.max_records - records_used
            request_params = {
                "series_id": series_spec.fred_series_id,
                "units": units,
                "output_type": "1",
                "order_by": "observation_date",
                "sort_order": "asc",
            }
            if params.start_date is not None:
                request_params["observation_start"] = params.start_date.isoformat()
            if params.end_date is not None:
                request_params["observation_end"] = params.end_date.isoformat()
            observations = _fetch_single_observation_page(
                params=request_params,
                page_size=min(_OBSERVATION_PAGE_LIMIT, remaining_records),
                max_records=remaining_records,
                timeout=ctx.timeout,
            )
            if records_used + len(observations) > params.max_records:
                raise FredSloosQueryLimitError(
                    limit_kind="max_records",
                    requested=records_used + len(observations),
                    maximum=params.max_records,
                )
            records_used += len(observations)
            records.append({"series": series_spec, "observations": observations})
        return records

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: FredSeniorLoanOfficerSurveyQuery,
    ) -> FetchResult:
        """Normalize complete ordered observation pages into typed provenance rows."""
        expected_series = series_for_category(params.category)
        if type(raw) is not list or len(raw) != len(expected_series):
            raise FredProviderError("FRED_BAD_RESPONSE", url=_URL)

        output: list[FredSeniorLoanOfficerSurvey] = []
        transform = _fred_transform(params.transform)
        try:
            for batch, series_spec in zip(raw, expected_series, strict=True):
                if type(batch) is not dict or batch.get("series") != series_spec:
                    raise ValueError("FRED SLOOS response series does not match its mapping")
                observations = batch.get("observations")
                if type(observations) is not list:
                    raise ValueError("FRED SLOOS observations must be a list")
                previous_date: date_type | None = None
                for observation in observations:
                    if type(observation) is not dict:
                        raise ValueError("FRED SLOOS observation must be an object")
                    echoed_series_id = observation.get("series_id")
                    if (
                        echoed_series_id is not None
                        and echoed_series_id != series_spec.fred_series_id
                    ):
                        raise ValueError("FRED observation echoed a different series ID")
                    observed_date = _parse_date(observation.get("date"))
                    if previous_date is not None and observed_date <= previous_date:
                        raise ValueError("FRED observations must be strictly ascending by date")
                    if params.start_date is not None and observed_date < params.start_date:
                        raise ValueError("FRED returned an observation before observation_start")
                    if params.end_date is not None and observed_date > params.end_date:
                        raise ValueError("FRED returned an observation after observation_end")
                    previous_date = observed_date
                    output.append(
                        FredSeniorLoanOfficerSurvey.model_validate(
                            {
                                "date": observed_date,
                                "symbol": series_spec.fred_series_id,
                                "value": _parse_value(observation.get("value")),
                                "title": None,
                                "category": series_spec.category,
                                "board_series_id": series_spec.board_series_id,
                                "fred_series_id": series_spec.fred_series_id,
                                "panel": series_spec.panel,
                                "bank_size": series_spec.bank_size,
                                "loan_type": series_spec.loan_type,
                                "measure": series_spec.measure,
                                "terms": series_spec.terms,
                                "source_unit_label": series_spec.source_unit_label,
                                "source_unit_multiplier": series_spec.unit_multiplier,
                                "source_frequency_code": series_spec.frequency_code,
                                "transform": transform,
                                "selection_scope": series_spec.selection_scope,
                                "categories_are_additive": False,
                            }
                        )
                    )
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as error:
            raise FredProviderError("FRED_BAD_OBSERVATION", url=_URL) from error
        return tuple(output)


__all__ = (
    "FredSeniorLoanOfficerSurveyFetcher",
    "FredSloosQueryLimitError",
)
