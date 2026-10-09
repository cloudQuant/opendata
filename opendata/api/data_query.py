"""Data query REST endpoints (design §10.1, milestone A4.9).

The read-only interface over the warehouse:

```
GET /api/v1/data/{asset_class}/{domain}     # dwd by default, ods on request
GET /api/v1/data/catalog                    # per domain: coverage, range, sources, quality
GET /api/v1/data/domains/{domain}/freshness
GET /api/v1/data/domains/{domain}/diff-report
```

Parameter safety follows the design: ``layer`` / ``adjust`` are
enum-typed (FastAPI answers 422 on anything else), ``fields`` is
whitelisted against the real table columns (400 otherwise), symbols
travel as bind parameters, and the query layer always applies a
window. ``adjust`` synthesizes qfq/hfq from Bar + AdjustFactor (D10);
until the stock-adjust factor table lands it answers 501 with the
reason instead of returning a silently wrong series.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal, get_type_hints

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from starlette.responses import Response

from opendata.api.dependencies import (  # FastAPI resolves them at runtime
    CurrentPrincipal,
    Principal,
    require_domain_access,
)
from opendata.api.schemas import APIResponse
from opendata.data.domains import (
    contract_model,
    dwd_table,
    load_domains,
    ods_table,
    require_domain,
)
from opendata.data.mapping import require_domain_mapping
from opendata.data.registry import get_registry
from opendata.pipeline.alert_matrix import registered_legs
from opendata.pipeline.freshness import (
    STATUS_MISSING,
    check_freshness,
    freshness_field,
    ods_freshness,
)
from opendata.pipeline.query import (
    DEFAULT_PAGE_SIZE,
    EXPORT_BATCH_ROWS,
    EXPORT_MAX_ROWS,
    MAX_PAGE_SIZE,
    DataQuery,
    apply_adjust_to_rows,
    build_data_select,
    resolve_time_field,
    validate_fields,
    window_bounds,
)
from opendata.pipeline.trading_calendar import resolve_calendar
from opendata.utils.serialization import serialize_for_json

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence

    from sqlalchemy import Engine
    from starlette.requests import Request

    from opendata.data.capability import Capability
    from opendata.data.domains import DomainSpec
    from opendata.data.protocol import Fetcher

#: Status codes whose response may not carry a body (Starlette's own rule).
_BODYLESS_STATUSES = frozenset({100, 101, 102, 103, 204, 205, 304})


def _error_response(exc: HTTPException) -> Response:
    """Render one endpoint error as FastAPI's ``HTTPException`` handler would.

    Args:
        exc: The error the endpoint raised, with its status and ``detail``.

    Returns:
        The JSON body ``{"detail": exc.detail}`` (or a bodyless response).
    """
    if exc.status_code in _BODYLESS_STATUSES:
        return Response(status_code=exc.status_code, headers=exc.headers)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


class _DetailedErrorRoute(APIRoute):
    """Route class that answers its own error body instead of re-raising it.

    An application may register a catch-all handler for a whole HTTP status:
    ``opendata/main.py`` registers ``@app.exception_handler(404)`` to hand
    unknown frontend paths to the SPA build, and for an ``/api/`` path that
    handler answers ``{"detail": "Not Found"}`` for *every* 404. Starlette
    looks status codes up before exception classes
    (``starlette/_exception_handler.py``: ``status_handlers.get(exc.status_code)``),
    so a 404 raised by an endpoint never reaches FastAPI's ``HTTPException``
    handler and its reason is lost. ``table 'dwd_stock_daily' is not
    available`` - the canonical answer of a missing warehouse table, which
    this API gives *before* any read - then reads as the same body a path that
    never matched a route produces, and a caller cannot tell the two apart.
    Building the response here keeps the reason in the payload whatever the
    deployment registers for a status code.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the endpoint handler so its errors leave as answers, not raises."""
        answer_request = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await answer_request(request)
            except HTTPException as exc:
                return _error_response(exc)

        return handler


router = APIRouter(route_class=_DetailedErrorRoute)

#: Adjust factor table of the stock-adjust domain (A4.1 scope: pending).
FACTOR_TABLE = "dwd_stock_adjust"
_FACTOR_KEY_BIND_BATCH = 400

#: Cross-check detail table the catalog's quality flag counts.
DIFF_TABLE = "dq_diff_report"

# Fields needed to build ``Bar`` for server-side adjustment even when a caller
# requests only a price subset. The response still projects the caller's fields.
_ADJUSTMENT_FIELDS = ("open", "high", "low", "close", "volume", "amount")


@dataclass(frozen=True)
class _QueryPlan:
    """Validated table shape and select fields shared by query and export."""

    query: DataQuery
    table: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    selected: tuple[str, ...]
    time_field: str | None = None
    symbol_field: str | None = None
    period_field: str | None = None
    report_date_field: str | None = None


@dataclass(frozen=True)
class _ResolvedDomainPath:
    """Canonical identity behind one registered path pair."""

    domain: str
    asset_class: str


@lru_cache(maxsize=1)
def get_warehouse_engine() -> Engine:
    """Process-wide sync engine of the data warehouse.

    The pipeline modules (freshness, partitions, writers) are sync;
    the endpoints hand them to ``asyncio.to_thread`` so the event loop
    stays free.
    """
    from opendata.core.config import settings

    return create_engine(settings.data_database_url, poolclass=NullPool)


def _resolve_data_path(
    asset_class: str,
    domain: str,
    *,
    domain_specs: Mapping[str, DomainSpec] | None = None,
    capabilities: Sequence[Capability] | None = None,
) -> _ResolvedDomainPath:
    """Resolve a canonical or declared REST alias to a registered identity.

    Canonical ``asset_class/domain_id`` paths remain valid. An alias is only
    the exact two-segment ``DomainSpec.rest_path`` pair, and it must resolve
    to one domain with one registered asset class. A path is rejected when
    canonical and alias interpretations collide or when an alias is ambiguous.

    Args:
        asset_class: First path segment supplied by the caller.
        domain: Second path segment supplied by the caller.
        domain_specs: Optional domain metadata for isolated tests.
        capabilities: Optional registered capabilities for isolated tests.

    Returns:
        The canonical domain id and asset class from registered capabilities.

    Raises:
        HTTPException: 404 when the path is unknown, unregistered, or ambiguous.
    """
    specs = load_domains() if domain_specs is None else domain_specs
    registered = get_registry().capabilities() if capabilities is None else capabilities
    path_pair = f"{asset_class}/{domain}"
    alias_domains = [name for name, spec in specs.items() if spec.rest_path == path_pair]

    if domain in specs:
        if alias_domains and any(name != domain for name in alias_domains):
            raise _unknown_data_path(path_pair)
        # Keep canonical validation and its scope-first error ordering in the
        # existing query guard. This path cannot be interpreted as another
        # domain's alias because collisions were rejected above.
        return _ResolvedDomainPath(domain=domain, asset_class=asset_class)

    if len(alias_domains) != 1:
        raise _unknown_data_path(path_pair)

    canonical_domain = alias_domains[0]
    asset_classes = {
        capability.asset_class for capability in registered if capability.domain == canonical_domain
    }
    if len(asset_classes) != 1:
        raise _unknown_data_path(path_pair)
    return _ResolvedDomainPath(domain=canonical_domain, asset_class=next(iter(asset_classes)))


def _unknown_data_path(path_pair: str) -> HTTPException:
    """Build the uniform 404 for unknown or ambiguous path pairs."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"data path {path_pair!r} is not a unique registered domain route",
    )


@router.get("/catalog")
async def data_catalog(
    principal: CurrentPrincipal,
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """List domains with coverage, range, per-source freshness and quality (FR-20).

    One row is a *domain*, not a capability leg: ``symbol_count`` /
    ``time_range`` / ``quality`` describe the merged dwd table, while
    ``sources`` carries what each source itself last delivered. A leg
    whose source has no field mapping for the domain is reported as
    ``unmapped`` with the reason - it is not measured with another
    leg's column and it is not silently dropped.

    An API key only sees the domains its scopes cover.
    """
    expected = await _expected_data_date(engine)
    registry = get_registry()
    capabilities = registry.capabilities()
    domain_specs = load_domains()
    legs_by_domain = registered_legs()
    model_descriptors = registry.list_model_descriptors()
    verified: dict[tuple[str, str], bool] = {
        (cap.domain, cap.source): cap.verified for cap in capabilities
    }
    asset_class: dict[str, str] = {cap.domain: cap.asset_class for cap in capabilities}
    capabilities_by_domain: dict[str, list[dict[str, Any]]] = {}
    registered_capabilities_by_domain: dict[str, list[Capability]] = {}
    markets_by_domain: dict[str, set[str]] = {}
    provider_query_domains = {
        descriptor.domain
        for descriptor in model_descriptors
        if (spec := domain_specs.get(descriptor.domain)) is not None and spec.semantics_declared
    }
    for capability in capabilities:
        if not principal.allows_domain(capability.domain):
            continue
        fetcher = registry.resolve(
            capability.asset_class,
            capability.domain,
            period=capability.period,
            market=capability.market,
            source=capability.source,
        )
        spec = domain_specs.get(capability.domain)
        model_query_endpoint = _provider_model_query_endpoint_for_capability(
            capability,
            fetcher,
            spec,
            model_descriptors,
        )
        capabilities_by_domain.setdefault(capability.domain, []).append(
            _catalog_capability(
                capability,
                fetcher,
                domain_defined=capability.domain in domain_specs,
                provider_query_domain=capability.domain in provider_query_domains,
                model_query_endpoint=model_query_endpoint,
            )
        )
        registered_capabilities_by_domain.setdefault(capability.domain, []).append(capability)
        market = capability.market.strip()
        if market:
            markets_by_domain.setdefault(capability.domain, set()).add(market)
    model_labels_by_domain: dict[str, set[str]] = {}
    for descriptor in model_descriptors:
        if principal.allows_domain(descriptor.domain):
            model_labels_by_domain.setdefault(descriptor.domain, set()).add(descriptor.model)
    diff_reports = await _diff_report_counts(engine)
    rows: list[dict[str, Any]] = []
    domains = sorted(set(legs_by_domain) | set(registered_capabilities_by_domain))
    for domain in domains:
        if not principal.allows_domain(domain):
            continue
        if domain not in domain_specs:
            domain_capabilities = registered_capabilities_by_domain[domain]
            sources = tuple(sorted({capability.source for capability in domain_capabilities}))
            rows.append(
                {
                    "domain": domain,
                    "asset_class": asset_class[domain],
                    "markets": sorted(markets_by_domain.get(domain, set())),
                    "capabilities": capabilities_by_domain[domain],
                    "display_name": " / ".join(
                        sorted(model_labels_by_domain.get(domain, {domain}))
                    ),
                    "domain_defined": False,
                    "service_state": "metadata_only",
                    "reason": "domain_not_declared",
                    "layer": None,
                    "table": None,
                    "freshness_field": None,
                    "latest": None,
                    "lag_days": None,
                    "status": "unmapped",
                    "coverage": None,
                    "quality": None,
                    "sources": [
                        {
                            "source": source,
                            "verified": all(
                                capability.verified
                                for capability in domain_capabilities
                                if capability.source == source
                            ),
                            "table": None,
                            "status": "unmapped",
                            "reason": "domain_not_declared",
                            "latest": None,
                            "lag_days": None,
                        }
                        for source in sources
                    ],
                }
            )
            continue
        if domain in provider_query_domains:
            spec = domain_specs[domain]
            domain_capabilities = registered_capabilities_by_domain[domain]
            sources = tuple(sorted({capability.source for capability in domain_capabilities}))
            reason = (
                "transient_model" if spec.storage_mode == "transient" else "warehouse_not_ready"
            )
            rows.append(
                {
                    "domain": domain,
                    "asset_class": asset_class[domain],
                    "markets": sorted(markets_by_domain.get(domain, set())),
                    "capabilities": capabilities_by_domain[domain],
                    "display_name": spec.display_name,
                    "domain_defined": True,
                    "service_state": "provider_query",
                    "reason": reason,
                    "temporal_kind": spec.temporal_kind,
                    "time_field": spec.time_field,
                    "natural_key": list(spec.natural_key),
                    "filter_dims": list(spec.filter_dims),
                    "storage_mode": spec.storage_mode,
                    "permissions": list(spec.permissions),
                    "layer": None,
                    "table": None,
                    "freshness_field": None,
                    "latest": None,
                    "lag_days": None,
                    "status": "unmapped",
                    "coverage": None,
                    "quality": None,
                    "sources": [
                        {
                            "source": source,
                            "verified": all(
                                capability.verified
                                for capability in domain_capabilities
                                if capability.source == source
                            ),
                            "table": None,
                            "status": "unmapped",
                            "reason": reason,
                            "latest": None,
                            "lag_days": None,
                        }
                        for source in sources
                    ],
                }
            )
            continue
        sources = legs_by_domain[domain]
        table = dwd_table(domain)
        freshness = await _freshness(engine, domain, table, expected=expected)
        coverage = await _coverage_facts(engine, domain, table)
        status = "missing" if freshness is None else freshness["status"]
        rows.append(
            {
                "domain": domain,
                "asset_class": asset_class.get(domain, "unrouted"),
                "markets": sorted(markets_by_domain.get(domain, set())),
                "capabilities": capabilities_by_domain.get(domain, []),
                "display_name": _display_name(domain),
                "domain_defined": True,
                "service_state": "warehouse",
                "layer": "dwd",
                "table": table,
                "freshness_field": None if coverage is None else coverage["field"],
                "latest": None if freshness is None else _iso(freshness["latest"]),
                "lag_days": None if freshness is None else freshness["lag_days"],
                "status": status,
                "coverage": None if coverage is None else coverage["facts"],
                "quality": None
                if coverage is None
                else _quality(coverage["facts"], diff_reports, domain),
                "sources": [
                    await _source_leg(
                        engine,
                        domain,
                        source,
                        verified=verified.get((domain, source), False),
                        expected=expected,
                    )
                    for source in sources
                ],
            }
        )
    return APIResponse(
        success=True,
        message="success",
        data={
            "domains": rows,
            "markets": sorted({market for row in rows for market in row["markets"]}),
            "expected_data_date": expected.isoformat(),
            "domains_total": len(rows),
            "source_legs_total": sum(len(row["sources"]) for row in rows),
        },
    )


def _catalog_capability(
    capability: Capability,
    fetcher: Fetcher[Any, Any],
    *,
    domain_defined: bool,
    provider_query_domain: bool = False,
    model_query_endpoint: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Describe one registered provider callable and its read endpoint.

    The provider callable is the actual registry fetcher. ``endpoint`` is
    the existing warehouse read route for this domain; its ``source`` and
    ``period`` filters are included without suggesting that the route invokes
    a provider fetch. ``model_query_endpoint`` is an independent exact-model
    POST route and is present only for a declared query-capable binding.
    """
    query_model = get_type_hints(type(fetcher).transform_query).get("return")
    model_fields = getattr(query_model, "model_fields", {})
    parameters: list[dict[str, Any]] = []
    for name, field in model_fields.items():
        annotation = field.annotation
        field_type = getattr(annotation, "__name__", str(annotation))
        parameters.append(
            {
                "name": name,
                "type": field_type,
                "required": field.is_required(),
                "description": field.description,
            }
        )

    fetcher_type = type(fetcher)
    endpoint = (
        {
            "name": "query_domain_data",
            "method": "GET",
            "path": f"/api/v1/data/{capability.asset_class}/{capability.domain}",
            "query_filters": {"source": capability.source, "period": capability.period},
        }
        if domain_defined and not provider_query_domain
        else None
    )
    result = {
        "asset_class": capability.asset_class,
        "domain": capability.domain,
        "period": capability.period,
        "market": capability.market,
        "source": capability.source,
        "verified": capability.verified,
        "notes": capability.notes,
        "callable": {
            "module": fetcher_type.__module__,
            "name": f"{fetcher_type.__name__}.fetch",
        },
        "endpoint": endpoint,
        "model_query_endpoint": model_query_endpoint,
        "parameters": parameters,
    }
    return result


def _provider_model_query_endpoint_for_capability(
    capability: Capability,
    fetcher: Fetcher[Any, Any],
    spec: DomainSpec | None,
    descriptors: Sequence[Any],
) -> dict[str, str] | None:
    """Return an exact model POST route only for a reviewed, query-enabled binding."""
    if spec is None or not spec.semantics_declared or "query" not in spec.permissions:
        return None
    identity = (
        capability.asset_class,
        capability.domain,
        capability.period,
        capability.market,
        capability.source,
    )
    matching = [
        descriptor
        for descriptor in descriptors
        if descriptor.source == capability.source and descriptor.capability_identity == identity
    ]
    if len(matching) != 1 or matching[0].model != getattr(fetcher, "canonical_model", None):
        return None
    descriptor = matching[0]
    return {
        "model": descriptor.model,
        "method": "POST",
        "path": f"/api/v1/providers/{descriptor.source}/models/{descriptor.model}/query",
    }


def _provider_model_domain_state(domain: str) -> tuple[bool, dict[str, str] | None]:
    """Return model-domain presence and any exact, query-enabled Registry route."""
    spec = load_domains().get(domain)
    if spec is None or not spec.semantics_declared:
        return False, None
    registry = get_registry()
    descriptors = registry.list_model_descriptors()
    domain_descriptors = [descriptor for descriptor in descriptors if descriptor.domain == domain]
    if not domain_descriptors:
        return False, None
    if "query" not in spec.permissions:
        return True, None
    for capability in registry.capabilities():
        if capability.domain != domain:
            continue
        fetcher = registry.resolve(
            capability.asset_class,
            capability.domain,
            period=capability.period,
            market=capability.market,
            source=capability.source,
        )
        endpoint = _provider_model_query_endpoint_for_capability(
            capability, fetcher, spec, descriptors
        )
        if endpoint is not None:
            return True, endpoint
    return True, None


def _reject_legacy_provider_model_read(domain: str, principal: Principal) -> None:
    """Keep model-backed domains off legacy date-based warehouse routes."""
    require_domain_access(principal, domain)
    is_provider_model_domain, endpoint = _provider_model_domain_state(domain)
    if not is_provider_model_domain:
        return
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Provider model query binding is unavailable",
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "This provider-model domain has no warehouse date-based service; "
            f"use {endpoint['method']} {endpoint['path']}"
        ),
    )


@router.get("/domains/{domain}/freshness")
async def domain_freshness(
    domain: str,
    principal: CurrentPrincipal,
    source: str | None = Query(None, description="Source for the ods layer"),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Report how current one domain's data is (design §9.4).

    The payload carries ``expected_data_date`` next to ``lag_days``: a lag
    without the date it was measured against cannot be checked, and this is
    the door a caller uses to check one domain without reading the catalog.
    """
    return await _domain_freshness(domain, principal=principal, source=source, engine=engine)


@router.get("/domains/{asset_class}/{domain}/freshness")
async def domain_freshness_alias(
    asset_class: str,
    domain: str,
    principal: CurrentPrincipal,
    source: str | None = Query(None, description="Source for the ods layer"),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Report freshness using the exact declared two-segment REST alias."""
    resolved = _resolve_data_path(asset_class, domain)
    return await _domain_freshness(
        resolved.domain, principal=principal, source=source, engine=engine
    )


async def _domain_freshness(
    domain: str,
    *,
    principal: Principal,
    source: str | None,
    engine: Engine,
) -> APIResponse:
    """Build canonical freshness output after enforcing its domain scope."""
    # Scope comes first: a key that may not read the domain gets the
    # same 403 whether or not the domain exists.
    require_domain_access(principal, domain)
    try:
        require_domain(domain)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _reject_legacy_provider_model_read(domain, principal)
    table = ods_table(domain, source) if source else dwd_table(domain)
    expected = await _expected_data_date(engine)
    freshness = await _freshness(engine, domain, table, source=source, expected=expected)
    if freshness is None:
        return APIResponse(
            success=True,
            message="no data",
            data={
                "domain": domain,
                "source": source,
                "status": STATUS_MISSING,
                "latest": None,
                "expected_data_date": expected.isoformat(),
            },
        )
    return APIResponse(
        success=True,
        message="success",
        data={
            "domain": domain,
            "source": source,
            "field": freshness["field"],
            "latest": _iso(freshness["latest"]),
            "lag_days": freshness["lag_days"],
            "status": freshness["status"],
            "expected_data_date": expected.isoformat(),
        },
    )


@router.get("/domains/{domain}/diff-report")
async def domain_diff_report(
    domain: str,
    principal: CurrentPrincipal,
    batch_id: str | None = Query(None, description="Filter one cross-check batch"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Query the cross-check differences of one domain (design §8.2)."""
    # Scope comes first: a key that may not read the domain gets the
    # same 403 whether or not the domain exists.
    require_domain_access(principal, domain)
    try:
        require_domain(domain)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    sql = (
        "SELECT batch_id, source_a, source_b, biz_key, field, value_a, value_b, "
        "deviation, verdict, checked_at FROM `dq_diff_report` WHERE `domain` = :domain"
    )
    params: dict[str, object] = {"domain": domain, "limit": limit}
    if batch_id:
        sql += " AND `batch_id` = :batch_id"
        params["batch_id"] = batch_id
    sql += " ORDER BY `checked_at` DESC, `biz_key` LIMIT :limit"
    try:
        rows = await asyncio.to_thread(_fetch_rows, engine, sql, params)
    except Exception as exc:  # warehouse table not migrated yet
        logger.warning("diff report unavailable for {}: {}", domain, exc)
        rows = []
    return APIResponse(
        success=True, message="success", data={"domain": domain, "rows": rows, "count": len(rows)}
    )


@router.get("/{asset_class}/{domain}")
async def query_domain_data(
    asset_class: str,
    domain: str,
    principal: CurrentPrincipal,
    symbols: str | None = Query(None, description="Comma separated symbols"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    source: str = Query("auto"),
    layer: Literal["dwd", "ods"] = Query("dwd"),
    adjust: Literal["none", "qfq", "hfq"] = Query("none"),
    fields: str | None = Query(None, description="Comma separated field filter"),
    period: str | None = Query(None),
    report_date: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Query one domain's data (design §10.1)."""
    resolved = _resolve_data_path(asset_class, domain)
    asset_class = resolved.asset_class
    domain = resolved.domain
    _reject_legacy_provider_model_read(domain, principal)
    plan = await _validated_query(
        engine,
        principal,
        asset_class=asset_class,
        domain=domain,
        symbols=symbols,
        start=start,
        end=end,
        source=source,
        layer=layer,
        adjust=adjust,
        fields=fields,
        period=period,
        report_date=report_date,
        page=page,
        page_size=page_size,
    )
    try:
        fetch_query = _query_for_adjustment(plan.query, plan.selected)
        sql, params = build_data_select(
            fetch_query,
            table=plan.table,
            columns=plan.columns,
            key=plan.key,
            time_field=plan.time_field,
            symbol_field=plan.symbol_field,
            period_field=plan.period_field,
            report_date_field=plan.report_date_field,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if adjust != "none":
        await _validate_adjustment_version_window(engine, plan)
    try:
        rows = await asyncio.to_thread(_fetch_rows, engine, sql, params)
    except Exception as exc:
        logger.error("data query failed for {}/{}: {}", asset_class, domain, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ODS query failed" if layer == "ods" else "query failed",
        ) from exc
    if adjust != "none":
        try:
            rows = await _adjusted_rows(engine, domain, rows, adjust)
            rows = _project_rows(rows, plan.selected)
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return APIResponse(
        success=True,
        message="success",
        data={
            "domain": domain,
            "asset_class": asset_class,
            "layer": layer,
            "source": source,
            "adjust": adjust,
            "columns": list(plan.selected),
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "count": len(rows),
        },
    )


@router.get("/{asset_class}/{domain}/export")
async def export_domain_data(
    asset_class: str,
    domain: str,
    principal: CurrentPrincipal,
    symbols: str | None = Query(None, description="Comma separated symbols"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    source: str = Query("auto"),
    layer: Literal["dwd", "ods"] = Query("dwd"),
    adjust: Literal["none", "qfq", "hfq"] = Query("none"),
    fields: str | None = Query(None, description="Comma separated field filter"),
    period: str | None = Query(None),
    report_date: date | None = Query(None),
    limit: int = Query(EXPORT_MAX_ROWS, ge=1, le=EXPORT_MAX_ROWS),
    engine: Engine = Depends(get_warehouse_engine),
) -> StreamingResponse:
    """Stream one domain's rows as CSV (design §10.1, AC-11).

    Same parameters and the same safety rules as the JSON query -
    field whitelist, enum layers/adjust, bound symbols, always a
    window - but read in batches so a large export does not have to fit
    in memory. Cells go through :func:`serialize_for_csv`, which
    neutralizes spreadsheet formula prefixes (``=``/``+``/``-``/``@``):
    a data source must not be able to run code in the analyst's Excel.
    """
    resolved = _resolve_data_path(asset_class, domain)
    asset_class = resolved.asset_class
    domain = resolved.domain
    _reject_legacy_provider_model_read(domain, principal)
    plan = await _validated_query(
        engine,
        principal,
        asset_class=asset_class,
        domain=domain,
        symbols=symbols,
        start=start,
        end=end,
        source=source,
        layer=layer,
        adjust=adjust,
        fields=fields,
        period=period,
        report_date=report_date,
        page=1,
        page_size=min(limit, EXPORT_BATCH_ROWS),
    )
    if adjust != "none":
        await _validate_adjustment_version_window(engine, plan, max_rows=limit)
    filename = f"{asset_class}_{domain}.csv"
    return StreamingResponse(
        _csv_stream(
            engine,
            plan.query,
            table=plan.table,
            columns=plan.columns,
            key=plan.key,
            selected=plan.selected,
            time_field=plan.time_field,
            symbol_field=plan.symbol_field,
            period_field=plan.period_field,
            report_date_field=plan.report_date_field,
            limit=limit,
        ),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _csv_stream(
    engine: Engine,
    query: DataQuery,
    *,
    table: str,
    columns: Sequence[str],
    key: tuple[str, ...],
    selected: Sequence[str],
    time_field: str | None,
    symbol_field: str | None,
    period_field: str | None,
    report_date_field: str | None,
    limit: int,
) -> AsyncIterator[str]:
    """Yield the CSV text of a query, one batch at a time.

    Args:
        engine: Warehouse engine.
        query: The validated query (its page fields are driven here).
        table: Warehouse table.
        columns: Table columns.
        key: Business-key columns (ordering and always-selected fields).
        selected: Columns to write, already whitelisted by the caller.
        time_field: Native date column for the query, or ``None`` for DWD.
        symbol_field: Native symbol column for the query, or ``None`` for DWD.
        period_field: Native report-period column, or ``None`` for DWD.
        report_date_field: Native announcement-date column, or ``None`` for DWD.
        limit: Row ceiling of the export.

    Yields:
        CSV fragments: a header first, then one buffer per batch.
    """
    import csv
    import io

    from opendata.utils.serialization import serialize_for_csv

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(selected)
    yield buffer.getvalue()

    written = 0
    adjustment_versions: dict[str, str] = {}
    while written < limit:
        batch = min(EXPORT_BATCH_ROWS, limit - written)
        page_query = replace(query, page=1, page_size=batch)
        fetch_query = _query_for_adjustment(page_query, selected)
        try:
            sql, params = build_data_select(
                fetch_query,
                table=table,
                columns=columns,
                key=key,
                time_field=time_field,
                symbol_field=symbol_field,
                period_field=period_field,
                report_date_field=report_date_field,
                offset=written,
                max_rows=EXPORT_BATCH_ROWS,
            )
            rows = await asyncio.to_thread(_fetch_rows, engine, sql, params)
        except Exception:
            logger.error("CSV export failed before completion for {}", query.domain)
            raise RuntimeError("CSV export failed before completion") from None
        if not rows:
            return
        if query.adjust != "none":
            try:
                rows = await _adjusted_rows(
                    engine,
                    query.domain,
                    rows,
                    query.adjust,
                    versions_by_symbol=adjustment_versions,
                )
            except Exception:
                logger.error("CSV export adjustment failed before completion for {}", query.domain)
                raise RuntimeError("CSV export failed before completion") from None
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for row in rows:
            writer.writerow([serialize_for_csv(row.get(column)) for column in selected])
        yield buffer.getvalue()
        written += len(rows)


def _query_for_adjustment(query: DataQuery, selected: Sequence[str]) -> DataQuery:
    """Fetch the full Bar inputs for adjusted queries, preserving query filters.

    Field validation happens before this helper is called. Adding the internal
    inputs here therefore cannot make an invalid caller field acceptable.
    """
    if query.adjust == "none":
        return query
    return replace(query, fields=tuple(dict.fromkeys((*selected, *_ADJUSTMENT_FIELDS))))


def _project_rows(rows: list[dict], selected: Sequence[str]) -> list[dict]:
    """Remove internal adjustment inputs from the JSON response projection."""
    return [{column: row[column] for column in selected} for row in rows]


async def _validate_adjustment_version_window(
    engine: Engine, plan: _QueryPlan, *, max_rows: int | None = None
) -> None:
    """Check adjustment-version consistency across the rows this request selects.

    JSON pagination must not let one page use legacy factors and a later page
    use affine factors for the same symbol. CSV performs this check before its
    response starts so a mismatch cannot leave a partially streamed file.
    Factor rows are considered only when their keys match bars in the selected
    date window (and, for exports, within the row limit).
    """
    query = plan.query
    if query.domain != "stock_daily" or query.adjust == "none":
        return

    batch_size = min(MAX_PAGE_SIZE, max_rows) if max_rows is not None else MAX_PAGE_SIZE
    key_query = replace(query, fields=("symbol", "trade_date"), page=1, page_size=batch_size)
    offset = 0
    versions_by_symbol: dict[str, str] = {}

    while max_rows is None or offset < max_rows:
        batch_size = (
            min(MAX_PAGE_SIZE, max_rows - offset) if max_rows is not None else MAX_PAGE_SIZE
        )
        key_query = replace(key_query, page_size=batch_size)
        try:
            sql, params = build_data_select(
                key_query,
                table=plan.table,
                columns=plan.columns,
                key=plan.key,
                time_field=plan.time_field,
                symbol_field=plan.symbol_field,
                period_field=plan.period_field,
                report_date_field=plan.report_date_field,
                offset=offset,
                max_rows=batch_size,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        try:
            bars = await asyncio.to_thread(_fetch_rows, engine, sql, params)
        except Exception as exc:
            logger.error("adjustment window query failed for {}: {}", query.domain, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="query failed"
            ) from exc
        if not bars:
            return

        symbols = list(dict.fromkeys(str(row["symbol"]) for row in bars))
        bar_keys = [(row.get("symbol"), row.get("trade_date")) for row in bars]
        factors = await asyncio.to_thread(_factor_rows, engine, symbols, bar_keys=bar_keys)
        if factors is None:
            # The page query retains its existing 501 behavior when it
            # needs factors; an empty page does not need the table.
            return
        try:
            _track_adjustment_versions(bars, factors, versions_by_symbol)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        offset += len(bars)
        if len(bars) < batch_size:
            return


async def _validated_query(
    engine: Engine,
    principal: Principal,
    *,
    asset_class: str,
    domain: str,
    symbols: str | None,
    start: date | None,
    end: date | None,
    source: str,
    layer: str,
    adjust: str,
    fields: str | None,
    period: str | None,
    report_date: date | None,
    page: int,
    page_size: int,
) -> _QueryPlan:
    """Run every guard the data endpoints share and build the query.

    Args:
        engine: Warehouse engine.
        principal: The authenticated caller.
        asset_class: Asset class segment of the path.
        domain: Domain identifier.
        symbols: Comma separated symbols.
        start: Inclusive start date.
        end: Inclusive end date.
        source: A registered leg of the domain, or ``auto``.
        layer: ``dwd`` or ``ods``.
        adjust: ``none`` / ``qfq`` / ``hfq``.
        fields: Comma separated field filter.
        period: Period filter for financial domains.
        report_date: Report-date filter for financial domains.
        page: One-based page number.
        page_size: Rows per page.

    Returns:
        The validated query and its reflected table shape.

    Raises:
        HTTPException: 400/403/404 exactly as the JSON endpoint answers.
    """
    # Scope comes first: a key that may not read the domain gets the
    # same 403 whether or not the domain exists.
    require_domain_access(principal, domain)
    try:
        require_domain(domain)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if layer == "ods" and source == "auto":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="layer=ods needs an explicit source (auto is the merged dwd view)",
        )
    caps = [
        capability for capability in get_registry().capabilities() if capability.domain == domain
    ]
    registered = {capability.asset_class for capability in caps}
    if not registered:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"domain {domain!r} has no registered capability",
        )
    if asset_class not in registered:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"domain {domain!r} is not registered under asset class {asset_class!r}",
        )
    # An unregistered source would otherwise be answered with the merged
    # table while the payload echoes the name the caller asked for.
    legs = {"auto", *(capability.source for capability in caps)}
    if source not in legs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"source {source!r} is not a registered leg of {domain!r}: "
            f"{', '.join(sorted(legs))}",
        )
    query = DataQuery(
        domain=domain,
        layer=layer,
        source=source,
        symbols=tuple(item.strip() for item in symbols.split(",") if item.strip())
        if symbols
        else (),
        start=start,
        end=end,
        fields=tuple(item.strip() for item in fields.split(",") if item.strip()) if fields else (),
        page=page,
        page_size=page_size,
        adjust=adjust,
        period=period,
        report_date=report_date,
    )
    try:
        query_start, query_end = window_bounds(query, today=date.today())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    query = replace(query, start=query_start, end=query_end)
    table = ods_table(domain, source) if layer == "ods" else dwd_table(domain)
    time_field: str | None = None
    symbol_field: str | None = None
    period_field: str | None = None
    report_date_field: str | None = None
    mapping = None
    contract_fields = set(contract_model(domain).model_fields)
    if period is not None and "report_period" not in contract_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="period filter is available only for financial domains",
        )
    if report_date is not None and "announce_date" not in contract_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="report_date filter is available only for financial domains",
        )
    if period is not None:
        period_field = "report_period"
    if report_date is not None:
        report_date_field = "announce_date"
    if layer == "ods":
        if adjust != "none":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="adjust is unavailable for raw ODS rows; query layer=dwd instead",
            )
        try:
            mapping = require_domain_mapping(source, domain)
        except (LookupError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source mapping is unavailable for direct ODS reads",
            ) from exc
        if mapping.pivot is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "direct ODS reads require a one-to-one source mapping; this source uses a pivot"
                ),
            )
        try:
            contract_time_field = resolve_time_field(domain)
        except (LookupError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source mapping has no directly queryable date field",
            ) from exc
        time_mapping = mapping.fields.get(contract_time_field)
        if time_mapping is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source mapping has no directly queryable date field",
            )
        time_field = time_mapping.source_column
        symbol_mapping = mapping.fields.get("symbol")
        symbol_field = symbol_mapping.source_column if symbol_mapping is not None else None
        if query.period is not None:
            period_mapping = mapping.fields.get("report_period")
            if period_mapping is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="this source has no one-to-one native report-period field",
                )
            period_field = period_mapping.source_column
        if query.report_date is not None:
            report_date_mapping = mapping.fields.get("announce_date")
            if report_date_mapping is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="this source has no one-to-one native report-date field",
                )
            report_date_field = report_date_mapping.source_column
        if query.symbols and symbol_field is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="this source has no directly queryable symbol field",
            )

    columns = await _table_columns(engine, table)
    if not columns:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"table {table!r} is not available"
        )
    if layer == "ods":
        if mapping is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source mapping is unavailable for direct ODS reads",
            )
        mapped_columns = {field.source_column for field in mapping.fields.values()}
        if not mapped_columns.issubset(columns) or time_field not in columns:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ODS table schema does not match its registered source mapping",
            )
        if symbol_field is not None and symbol_field not in columns:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ODS table schema does not match its registered source mapping",
            )
        key = tuple(await asyncio.to_thread(_inspect_primary_key, engine, table))
        source_key = mapping.source_key
        if (
            not key
            or not source_key
            or key[: len(source_key)] != source_key
            or not set(key).issubset(columns)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ODS table needs its mapped source key as a reflected primary key",
            )
    else:
        key = _key(domain)
        if source != "auto" and "source" not in columns:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="explicit source filtering requires the DWD source trace column",
            )
    try:
        selected = validate_fields(query.fields, available=columns, always_include=key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        # Validate every field, identifier and filter while the request can
        # still receive a normal 400 response. StreamingResponse has already
        # committed its status by the time its generator first runs.
        build_data_select(
            query,
            table=table,
            columns=columns,
            key=key,
            time_field=time_field,
            symbol_field=symbol_field,
            period_field=period_field,
            report_date_field=report_date_field,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _QueryPlan(
        query=query,
        table=table,
        columns=tuple(columns),
        key=key,
        selected=tuple(selected),
        time_field=time_field,
        symbol_field=symbol_field,
        period_field=period_field,
        report_date_field=report_date_field,
    )


async def _adjusted_rows(
    engine: Engine,
    domain: str,
    rows: list[dict],
    method: str,
    *,
    versions_by_symbol: dict[str, str] | None = None,
) -> list[dict]:
    """Synthesize an adjusted series (D10) or explain why it cannot."""
    if domain != "stock_daily":
        raise ValueError(f"adjust is only defined for bar series; {domain!r} has none")
    symbols = list(dict.fromkeys(str(row.get("symbol")) for row in rows))
    bar_keys = [(row.get("symbol"), row.get("trade_date")) for row in rows]
    factors = await asyncio.to_thread(_factor_rows, engine, symbols, bar_keys=bar_keys)
    if factors is None:
        raise NotImplementedError(
            "adjust factors are not available yet: the stock-adjust factor table "
            f"({FACTOR_TABLE}) lands with the adjustment domain (see A2.5/A4.1 scope)"
        )
    if versions_by_symbol is not None:
        _track_adjustment_versions(rows, factors, versions_by_symbol)
    return apply_adjust_to_rows(domain, rows, method=method, factors=factors)


def _track_adjustment_versions(
    rows: list[dict], factors: list[dict], versions_by_symbol: dict[str, str]
) -> None:
    """Reject a CSV page that changes adjustment contract for one symbol.

    Legacy ``None`` and explicit ``legacy-multiplicative-v1`` rows share one
    semantic bucket. Only factor rows joined to a selected bar participate.
    """
    factors_by_key: dict[tuple[object, object], dict] = {}
    duplicate_keys: set[tuple[object, object]] = set()
    for factor in factors:
        key = factor.get("symbol"), factor.get("trade_date")
        if key in factors_by_key:
            duplicate_keys.add(key)
        factors_by_key[key] = factor
    bar_keys = {(row.get("symbol"), row.get("trade_date")) for row in rows}
    if duplicate_keys & bar_keys:
        symbol, trade_date = sorted(duplicate_keys & bar_keys, key=str)[0]
        raise ValueError(f"duplicate adjustment factor for {symbol} {trade_date}")
    for row in rows:
        symbol = str(row.get("symbol"))
        matched_factor = factors_by_key.get((row.get("symbol"), row.get("trade_date")))
        if matched_factor is None:
            continue
        version = matched_factor.get("adjustment_version")
        contract = "legacy" if version in (None, "legacy-multiplicative-v1") else str(version)
        previous = versions_by_symbol.get(symbol)
        if previous is not None and previous != contract:
            raise ValueError(
                f"mixed adjustment versions for {symbol}: {previous!r} and {contract!r}"
            )
        versions_by_symbol[symbol] = contract


def _factor_rows(
    engine: Engine,
    symbols: list[str],
    *,
    bar_keys: Sequence[tuple[object, object]] | None = None,
) -> list[dict] | None:
    """Read known factor fields, optionally limited to actual bar keys.

    ``SELECT *`` keeps a factor table created before the affine migration
    readable. Only fields understood by ``AdjustFactor`` are returned, so
    DWD lineage columns never become model input. The date values are
    serialized like queried bars because adjustment joins on the same
    ``(symbol, trade_date)`` key. ``bar_keys`` constrains each SQL read to
    matching bars instead of loading a symbol's full factor history.
    """
    if bar_keys is None and not symbols:
        return []
    key_batches: list[list[tuple[object, object]]] = []
    if bar_keys is not None:
        keys = list(dict.fromkeys(bar_keys))
        if not keys:
            return []
        key_batches = [
            keys[index : index + _FACTOR_KEY_BIND_BATCH]
            for index in range(0, len(keys), _FACTOR_KEY_BIND_BATCH)
        ]
    known_fields = {
        "symbol",
        "trade_date",
        "qfq_factor",
        "hfq_factor",
        "qfq_scale",
        "qfq_offset",
        "hfq_scale",
        "hfq_offset",
        "adjustment_version",
        "legacy_source",
    }
    try:
        with engine.connect() as connection:
            statements: list[tuple[str, dict[str, object]]] = []
            if bar_keys is None:
                names = ", ".join(f":symbol_{index}" for index in range(len(symbols)))
                symbol_params: dict[str, object] = {
                    f"symbol_{index}": symbol for index, symbol in enumerate(symbols)
                }
                statements.append((f"`symbol` IN ({names})", symbol_params))
            else:
                for batch in key_batches:
                    predicates: list[str] = []
                    key_params: dict[str, object] = {}
                    for index, (symbol, trade_date) in enumerate(batch):
                        symbol_param = f"key_symbol_{index}"
                        date_param = f"key_date_{index}"
                        predicates.append(
                            f"(`symbol` = :{symbol_param} AND `trade_date` = :{date_param})"
                        )
                        key_params[symbol_param] = symbol
                        key_params[date_param] = trade_date
                    statements.append((" OR ".join(predicates), key_params))
            rows: list[dict] = []
            for predicate, statement_params in statements:
                sql = f"SELECT * FROM `{FACTOR_TABLE}` WHERE {predicate}"  # nosec B608  # table = FACTOR_TABLE module literal; predicate joins only backticked column names and :name bind placeholders
                result = connection.execute(text(sql), statement_params)
                rows.extend(
                    {
                        field: serialize_for_json(value)
                        for field, value in row.items()
                        if field in known_fields
                    }
                    for row in result.mappings().all()
                )
            return rows
    except Exception:  # table absent: the caller decides the status code
        return None


def _fetch_rows(engine: Engine, sql: str, params: dict) -> list[dict]:
    """Run a read query and serialize its rows (sync, for to_thread)."""
    with engine.connect() as connection:
        result = connection.execute(text(sql), params)
        columns = list(result.keys())
        return [_serialize_row(columns, row) for row in result.fetchall()]


async def _table_columns(engine: Engine, table: str) -> list[str]:
    """Columns of a warehouse table, empty when it does not exist."""
    return await asyncio.to_thread(_inspect_columns, engine, table)


def _inspect_columns(engine: Engine, table: str) -> list[str]:
    """Inspector helper (sync)."""
    from sqlalchemy import inspect

    try:
        with engine.connect() as connection:
            return [column["name"] for column in inspect(connection).get_columns(table)]
    except Exception:
        return []


async def _freshness(
    engine: Engine,
    domain: str,
    table: str,
    *,
    source: str | None = None,
    expected: date,
) -> dict | None:
    """Freshness facts of a table, None when they cannot be measured.

    ``expected`` comes from the trading calendar (A4.7), so the lag is
    measured against the last date that should have data rather than
    against the wall clock - a weekend is not a two-day outage.
    """
    try:
        resolve_time_field(domain)
    except ValueError:
        return None
    try:
        if source:
            report = await asyncio.to_thread(
                ods_freshness, engine, domain, source, table=table, expected=expected
            )
        else:
            report = await asyncio.to_thread(
                check_freshness,
                engine,
                domain=domain,
                source=None,
                table=table,
                expected=expected,
            )
    except Exception as exc:
        logger.debug("freshness unavailable for {}: {}", table, exc)
        return None
    if report.status == STATUS_MISSING:
        return None
    return {
        "field": report.field,
        "latest": report.latest,
        "lag_days": report.lag_days,
        "status": report.status,
    }


async def _source_leg(
    engine: Engine,
    domain: str,
    source: str,
    *,
    verified: bool,
    expected: date,
) -> dict:
    """One source's own freshness reading for the catalog.

    Args:
        engine: Warehouse engine.
        domain: Domain identifier.
        source: Source identifier.
        verified: Whether the registry marks this leg verified.
        expected: Date the data should have reached.

    Returns:
        The leg row. ``unmapped`` is a measured state (the source has no
        field mapping, so no column of its own can be read), never a
        borrowed reading from another leg and never a silent drop.
    """
    table = ods_table(domain, source)
    base = {"source": source, "verified": verified, "table": table}
    try:
        report = await asyncio.to_thread(
            ods_freshness, engine, domain, source, table=table, expected=expected
        )
    except LookupError as exc:
        return {**base, "status": "unmapped", "reason": str(exc), "latest": None, "lag_days": None}
    return {
        **base,
        "status": report.status,
        "reason": None,
        "latest": _iso(report.latest),
        "lag_days": report.lag_days,
    }


async def _coverage_facts(engine: Engine, domain: str, table: str) -> dict | None:
    """Coverage/range/quality facts of one table, None when unmeasurable."""
    try:
        field = freshness_field(domain)
    except (LookupError, ValueError):
        return None
    return await asyncio.to_thread(_read_coverage, engine, table, field)


def _read_coverage(engine: Engine, table: str, field: str) -> dict | None:
    """Run the coverage aggregate (sync).

    The subject columns come from the table's own primary key rather than
    a name heuristic: the dwd DDL declares the business key (AC-8), so
    "覆盖标的数" counts what actually identifies a row minus the date the
    range is measured over. A table whose key is only its date column has
    no subject to count, and says so with ``symbol_columns: null``.
    """
    from sqlalchemy.exc import SQLAlchemyError

    columns = _inspect_columns(engine, table)
    if field not in columns:
        return None
    subjects = [column for column in _inspect_primary_key(engine, table) if column != field]
    flagged = "SUM(`_diff_flag`)" if "_diff_flag" in columns else "NULL"
    if subjects:
        joined = ", ".join(f"`{column}`" for column in subjects)
        symbols = f"(SELECT COUNT(*) FROM (SELECT DISTINCT {joined} FROM `{table}`) AS `subj`)"  # nosec B608  # cols from _inspect_primary_key, dwd_table
    else:
        symbols = "NULL"
    sql = (
        f"SELECT COUNT(*) AS `rows`, MIN(`{field}`) AS `start`, MAX(`{field}`) AS `end`, "  # nosec B608  # field in _inspect_columns, table=dwd_table
        f"{flagged} AS `diff_flagged`, {symbols} AS `symbols` FROM `{table}`"
    )
    try:
        with engine.connect() as connection:
            row = connection.execute(text(sql)).one()
    except SQLAlchemyError as exc:
        logger.debug("coverage of {} unavailable: {}", table, exc)
        return None
    return {
        "field": field,
        "symbol_columns": subjects or None,
        "facts": {
            "rows": int(row[0]),
            "start": _iso(row[1]),
            "end": _iso(row[2]),
            "diff_flagged": None if row[3] is None else int(row[3]),
            "symbols": None if row[4] is None else int(row[4]),
        },
    }


def _inspect_primary_key(engine: Engine, table: str) -> list[str]:
    """Business-key columns of a warehouse table, empty when unknown."""
    from sqlalchemy import inspect

    try:
        with engine.connect() as connection:
            constraint = inspect(connection).get_pk_constraint(table)
    except Exception:
        return []
    return [str(column) for column in constraint.get("constrained_columns") or []]


async def _diff_report_counts(engine: Engine) -> dict[str, int] | None:
    """Per-domain ``dq_diff_report`` row counts, None when the table is absent.

    None and an empty mapping are different readings: the first says the
    cross-check detail was never migrated, the second says it is there and
    no domain has any.
    """
    from sqlalchemy.exc import SQLAlchemyError

    def read() -> dict[str, int]:
        with engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT `domain`, COUNT(*) AS `rows` FROM `{DIFF_TABLE}` GROUP BY `domain`")  # nosec B608  # only DIFF_TABLE, a module constant
            ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    try:
        return await asyncio.to_thread(read)
    except SQLAlchemyError as exc:
        logger.debug("diff report counts unavailable: {}", exc)
        return None


def _quality(facts: dict, diff_reports: dict[str, int] | None, domain: str) -> dict:
    """The catalog's 质量标记 for one domain.

    ``_diff_flag`` is the marker the dwd merge leaves on a row the two
    sources disagreed about (design §8.3), so the flag reads the merged
    table first and the cross-check detail second; ``unmeasured`` is only
    used when the marker column itself is absent - an empty table is
    ``clean`` about flags and ``missing`` about freshness, two separate
    readings on purpose.
    """
    flagged = facts.get("diff_flagged")
    reports = None if diff_reports is None else diff_reports.get(domain, 0)
    if flagged is None:
        flag = "unmeasured"
    elif flagged > 0 or (reports or 0) > 0:
        flag = "flagged"
    else:
        flag = "clean"
    return {"diff_flagged": flagged, "diff_report_rows": reports, "flag": flag}


async def _expected_data_date(engine: Engine, *, on: date | None = None) -> date:
    """Latest date that should carry data, per the trading calendar (A4.7).

    Resolved once per request instead of per table: the catalog endpoint
    measures every domain, and each measurement would otherwise re-read
    the calendar.

    Args:
        engine: Warehouse engine.
        on: Reference date; defaults to today (tests pin it so a weekend
            cannot change what the assertion means).

    Returns:
        The date the freshness lag is measured against.
    """
    calendar = await asyncio.to_thread(resolve_calendar, engine)
    return calendar.expected_data_date(on or date.today())


def _key(domain: str) -> tuple[str, ...]:
    """Business-key columns derived from a domain's contract model.

    The contract models carry no key metadata yet, so the key is the
    identifier fields plus the date field: exactly the columns that
    identify a row and that the ordering and the always-included
    selection need.
    """
    date_fields = {"trade_date", "ex_date", "report_period", "as_of", "date"}
    model = contract_model(domain)
    return tuple(
        name
        for name in model.model_fields
        if name == "symbol" or name.endswith("_symbol") or name in date_fields
    )


def _display_name(domain: str) -> str:
    """Frontend display name of a domain."""
    from opendata.data.domains import display_name

    return display_name(domain)


def _serialize_row(columns: list[str], row: Sequence[object]) -> dict:
    """Serialize one row for JSON."""
    return {column: serialize_for_json(row[index]) for index, column in enumerate(columns)}


def _iso(value: object) -> str | None:
    """ISO rendering of a date-ish value.

    Drivers disagree about aggregates: MySQL types ``MIN(col)`` as a DATE
    and hands back a ``date``, while SQLite returns the stored text. The
    catalog's range must read the same either way, and a text date is
    already ISO.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()[:10]
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        raise TypeError(f"{type(value).__name__} has no ISO rendering")
    return str(isoformat())


async def _selected_columns(engine: Engine, table: str, query: DataQuery, domain: str) -> list[str]:
    """Selected columns after the field whitelist (for the response)."""
    from opendata.pipeline.query import validate_fields

    columns = await _table_columns(engine, table)
    return validate_fields(query.fields, available=columns, always_include=_key(domain))
