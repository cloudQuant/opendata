"""Governed execution for exact canonical provider-model queries."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from opendata.data.async_execution import AsyncExecutionTimeoutError
from opendata.data.protocol import FetchContext
from opendata.data.request_budget import (
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetError,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestOperation,
)
from opendata.services.provider_models import (
    ProviderModelIdentityError,
    ProviderModelNotFoundError,
    find_provider_model,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel
    from opendata.data.registry import ProviderModelDescriptor


class _DomainPrincipal(Protocol):
    """The authorization surface needed by a provider query."""

    def allows_domain(self, domain: str) -> bool: ...


class ProviderModelQueryIdentityError(ValueError):
    """The external exact identity is malformed or conflicts with query input."""


class ProviderModelQueryNotFoundError(LookupError):
    """The exact provider/model pair is not registered."""


class ProviderModelQueryConfigurationError(RuntimeError):
    """Registry or request-budget configuration cannot execute this query."""


class ProviderModelQueryForbiddenError(PermissionError):
    """The caller is not authorized to query the model's domain."""


class ProviderModelQueryAuthorizationError(PermissionError):
    """No trusted request grant authorizes this provider query."""


class ProviderModelQueryUnavailableError(RuntimeError):
    """The domain has no complete, query-enabled local semantic declaration."""


class ProviderModelQueryValidationError(ValueError):
    """The query is invalid under the registered Fetcher's query model."""


class ProviderModelQueryBudgetError(RuntimeError):
    """The trusted request attempt allowance is exhausted."""


class ProviderModelQueryTimeoutError(TimeoutError):
    """The bounded query exceeded its trusted deadline."""


class ProviderModelQueryCancelledError(RuntimeError):
    """The bounded query was cancelled."""


class ProviderModelQueryUpstreamError(RuntimeError):
    """The Fetcher or its upstream failed; details are intentionally hidden."""


class ProviderModelQueryOutputError(RuntimeError):
    """The Fetcher returned data outside the declared contract shape."""


_FORBIDDEN_QUERY_CONTROL_FIELDS = frozenset(
    {"ctx", "context", "execution_context", "operation", "request_budget", "budget"}
)
_DEFAULT_TIMEOUT_SECONDS = 30.0


async def query_provider_model(
    *,
    registry: Any,  # noqa: ANN401 - injected process registry
    source: str,
    model: str,
    query: Mapping[str, Any],
    principal: _DomainPrincipal,
    ctx: FetchContext | None = None,
) -> dict[str, object]:
    """Execute one exact registered model through its Fetcher template.

    The optional context is an internal trusted-code seam for offline tests.
    HTTP request data never reaches it. Production callers get a finite
    timeout and a zero-grant request budget, which fails closed at Fetcher
    admission before the source extraction stage.
    """
    descriptor, fetcher = _resolve_fetcher(registry, source, model)
    if not principal.allows_domain(descriptor.domain):
        raise ProviderModelQueryForbiddenError from None

    _, contract_type = _reviewed_query_contract(descriptor.domain)
    normalized_query = _prepare_query(
        query,
        source=source,
        market=fetcher.capability.market,
    )
    context = _trusted_context(ctx)

    try:
        output = await fetcher.fetch_async(ctx=context, **normalized_query)
    except asyncio.CancelledError:
        raise
    except ValidationError:
        raise ProviderModelQueryValidationError from None
    except (AsyncExecutionTimeoutError, RequestExecutionDeadlineError, TimeoutError):
        raise ProviderModelQueryTimeoutError from None
    except RequestExecutionCancelledError:
        raise ProviderModelQueryCancelledError from None
    except (RequestAuthorizationError, RequestBudgetScopeError):
        raise ProviderModelQueryAuthorizationError from None
    except RequestAttemptLimitError:
        raise ProviderModelQueryBudgetError from None
    except RequestBudgetError:
        raise ProviderModelQueryConfigurationError from None
    except Exception:
        raise ProviderModelQueryUpstreamError from None

    results, pagination = _serialize_output(output, model, contract_type)
    observed_at = datetime.now(timezone.utc).isoformat()
    response: dict[str, object] = {
        "source": source,
        "model": model,
        "domain": descriptor.domain,
        "verified": descriptor.verified,
        "observed_at": observed_at,
        "completeness": "NOT_ASSESSED",
        "results": results,
        "pagination": pagination,
    }
    return response


def _resolve_fetcher(
    registry: Any,  # noqa: ANN401 - injected process registry
    source: str,
    model: str,
) -> tuple[ProviderModelDescriptor, Any]:
    """Find and resolve one exact identity, then cross-check its Fetcher."""
    try:
        descriptor = find_provider_model(registry, source, model)
    except ProviderModelIdentityError:
        raise ProviderModelQueryIdentityError from None
    except ProviderModelNotFoundError:
        raise ProviderModelQueryNotFoundError from None
    except Exception:
        raise ProviderModelQueryConfigurationError from None

    try:
        fetcher = registry.resolve_model(source, model)
    except Exception:
        raise ProviderModelQueryConfigurationError from None

    capability = getattr(fetcher, "capability", None)
    capability_identity = (
        getattr(capability, "asset_class", None),
        getattr(capability, "domain", None),
        getattr(capability, "period", None),
        getattr(capability, "market", None),
        getattr(capability, "source", None),
    )
    if (
        descriptor.source != source
        or descriptor.model != model
        or getattr(fetcher, "canonical_model", None) != model
        or getattr(capability, "source", None) != source
        or getattr(capability, "domain", None) != descriptor.domain
        or capability_identity != descriptor.full_capability_identity
        or getattr(capability, "verified", None) != descriptor.verified
        or not callable(getattr(fetcher, "fetch_async", None))
    ):
        raise ProviderModelQueryConfigurationError from None
    return descriptor, fetcher


def _reviewed_query_contract(domain: str) -> tuple[Any, type[ContractModel]]:
    """Load full DomainSpec semantics and the exact declared contract lazily."""
    from opendata.data.domains import DomainSpec, contract_model, require_domain_semantics
    from opendata.data.models.base import ContractModel

    try:
        spec = require_domain_semantics(domain)
        contract_type = contract_model(domain)
    except Exception:
        raise ProviderModelQueryUnavailableError from None

    if (
        not isinstance(spec, DomainSpec)
        or not spec.semantics_declared
        or not isinstance(contract_type, type)
        or not issubclass(contract_type, ContractModel)
        or spec.contract != contract_type.__name__
        or not spec.natural_key
        or "query" not in spec.permissions
    ):
        raise ProviderModelQueryUnavailableError from None

    contract_fields = set(contract_type.model_fields)
    semantic_fields = (
        (() if spec.time_field is None else (spec.time_field,))
        + spec.natural_key
        + spec.filter_dims
    )
    if any(field_name not in contract_fields for field_name in semantic_fields):
        raise ProviderModelQueryUnavailableError from None
    return spec, contract_type


def _prepare_query(query: Mapping[str, Any], *, source: str, market: str) -> dict[str, Any]:
    """Pin source/market to Registry identity and reject execution controls."""
    if not isinstance(query, Mapping):
        raise ProviderModelQueryValidationError from None
    try:
        values = dict(query)
    except Exception:
        raise ProviderModelQueryValidationError from None
    if any(not isinstance(key, str) for key in values):
        raise ProviderModelQueryValidationError from None
    if _FORBIDDEN_QUERY_CONTROL_FIELDS.intersection(values):
        raise ProviderModelQueryValidationError from None

    requested_source = values.get("source", source)
    if requested_source == "auto" or "source" not in values:
        values["source"] = source
    elif requested_source != source:
        raise ProviderModelQueryIdentityError from None

    requested_market = values.get("market", market)
    if "market" not in values or requested_market is None:
        values["market"] = market
    elif requested_market != market:
        raise ProviderModelQueryIdentityError from None
    return values


def _trusted_context(ctx: FetchContext | None) -> FetchContext:
    """Supply a bounded, query-only context and reject untrusted variants."""
    if ctx is None:
        return FetchContext(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            request_budget=RequestBudget(),
            operation=RequestOperation.QUERY,
        )
    if (
        not isinstance(ctx, FetchContext)
        or ctx.operation is not RequestOperation.QUERY
        or ctx.timeout is None
        or isinstance(ctx.timeout, bool)
        or not isinstance(ctx.timeout, (int, float))
        or not math.isfinite(float(ctx.timeout))
        or ctx.timeout <= 0
    ):
        raise ProviderModelQueryValidationError from None
    return ctx


def _serialize_output(
    output: object,
    model: str,
    contract_type: type[ContractModel],
) -> tuple[list[dict[str, object]], dict[str, int] | None]:
    """Revalidate strict contract rows and preserve the central BLS page."""
    from opendata.data.models.period_series import BlsCatalogItem, BlsCatalogPage

    pagination: dict[str, int] | None = None
    if model == "BlsSearch":
        if contract_type is not BlsCatalogItem or type(output) is not BlsCatalogPage:
            raise ProviderModelQueryOutputError from None
        page = output
        if (
            type(page.total) is not int
            or type(page.offset) is not int
            or type(page.limit) is not int
            or page.total < 0
            or page.offset < 0
            or page.limit < 1
            or not isinstance(page.items, tuple)
            or len(page.items) > page.limit
            or len(page.items) > max(page.total - page.offset, 0)
        ):
            raise ProviderModelQueryOutputError from None
        rows: object = page.items
        pagination = {"total": page.total, "offset": page.offset, "limit": page.limit}
    else:
        if type(output) is BlsCatalogPage:
            raise ProviderModelQueryOutputError from None
        rows = output

    if isinstance(rows, (str, bytes, bytearray, Mapping)) or not isinstance(rows, Sequence):
        raise ProviderModelQueryOutputError from None
    serialized = [_serialize_contract_row(row, contract_type) for row in rows]
    return serialized, pagination


def _serialize_contract_row(
    row: object,
    contract_type: type[ContractModel],
) -> dict[str, object]:
    """Revalidate even model_construct rows before lossless JSON-mode dumping."""
    if type(row) is not contract_type:
        raise ProviderModelQueryOutputError from None
    try:
        row_state = vars(row)
        declared_fields = set(contract_type.model_fields)
        extras = set(row_state) - declared_fields
        pydantic_extras = getattr(row, "__pydantic_extra__", None)
        if extras or pydantic_extras:
            raise ValueError
        raw_data = row.model_dump(mode="python", by_alias=False)
        if not isinstance(raw_data, dict) or set(raw_data) - declared_fields:
            raise ValueError
        validated = contract_type.model_validate(raw_data, strict=True)
        if type(validated) is not contract_type:
            raise ValueError
        serialized = validated.model_dump(mode="json", by_alias=False)
        if not isinstance(serialized, dict):
            raise ValueError
        return serialized
    except Exception:
        raise ProviderModelQueryOutputError from None


__all__ = [
    "ProviderModelQueryAuthorizationError",
    "ProviderModelQueryBudgetError",
    "ProviderModelQueryCancelledError",
    "ProviderModelQueryConfigurationError",
    "ProviderModelQueryForbiddenError",
    "ProviderModelQueryIdentityError",
    "ProviderModelQueryNotFoundError",
    "ProviderModelQueryOutputError",
    "ProviderModelQueryTimeoutError",
    "ProviderModelQueryUnavailableError",
    "ProviderModelQueryUpstreamError",
    "ProviderModelQueryValidationError",
    "query_provider_model",
]
