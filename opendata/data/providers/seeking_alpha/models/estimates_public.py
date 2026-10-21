"""Seeking Alpha forward estimate adapters: resolve the site id, then read the estimates envelope.

Two requests, both public and unauthenticated, matching the route C144 walked through its 400
parameter chain: ``/api/v3/searches`` resolves a ticker to the numeric Seeking Alpha id that the
estimates route insists on, and ``/api/v3/symbol_data/estimates`` answers per data item rather than
per period. No credential, cookie, or session token exists on either call.

The envelope's shape is recorded in
``docs/evidence/C147/wave41-objective-scoreboard-20261020/runs/w152_sa_estimates_shape_inventory.txt``:
``estimates.<id>.<item>.<relative_period>[{dataitemvalue, effectivedate, period{...}}]``. Because
that is a transposed layout, an item that the source does not publish simply leaves the column
null; a cell that *is* published but is not a number is a corrupt body and is refused.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar, Literal

from pydantic import ConfigDict, StrictStr, ValidationError

from opendata.data.capability import Capability
from opendata.data.http_client import get_shared_http_client
from opendata.data.models.base import ContractModel  # noqa: TC001  # row_model ClassVar
from opendata.data.models.seeking_alpha_estimates import (
    SeekingAlphaForwardEpsEstimates,
    SeekingAlphaForwardSalesEstimates,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.seeking_alpha._source import SOURCE

BASE_URL = "https://seekingalpha.com"
SEARCH_PATH = "/api/v3/searches"
ESTIMATES_PATH = "/api/v3/symbol_data/estimates"
TIMEOUT_SECONDS = 20.0
MAX_RELATIVE_PERIODS = 8

ID_UNRESOLVED = "SEEKING_ALPHA_ESTIMATES_ID_UNRESOLVED"
HTTP_ERROR = "SEEKING_ALPHA_ESTIMATES_HTTP_ERROR"
SHAPE_INVALID = "SEEKING_ALPHA_ESTIMATES_SHAPE_INVALID"
BAD_NUMERIC = "SEEKING_ALPHA_ESTIMATES_BAD_NUMERIC"
EMPTY = "SEEKING_ALPHA_ESTIMATES_EMPTY"
BAD_QUERY = "SEEKING_ALPHA_ESTIMATES_QUERY_INVALID"

EPS_ITEMS = (
    "eps_gaap_actual",
    "eps_gaap_consensus_low",
    "eps_gaap_consensus_mean",
    "eps_gaap_consensus_high",
    "eps_normalized_actual",
    "eps_normalized_consensus_low",
    "eps_normalized_consensus_mean",
    "eps_normalized_consensus_high",
    "eps_normalized_num_of_estimates",
)
SALES_ITEMS = (
    "revenue_actual",
    "revenue_consensus_low",
    "revenue_consensus_mean",
    "revenue_consensus_high",
    "revenue_num_of_estimates",
)
_COUNT_ITEMS = frozenset({"eps_normalized_num_of_estimates", "revenue_num_of_estimates"})


class SeekingAlphaForwardEstimatesQuery(QueryParams):
    """One symbol (or a comma list), a reporting period, and how many forward periods to ask for."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: StrictStr
    source: Literal["auto", "seeking_alpha"] = "auto"
    market: Literal["us"] | None = None
    period: Literal["annual", "quarter"] = "quarter"
    relative_periods: int = 4

    def resolved_symbols(self) -> list[str]:
        """Split the multi-symbol form and refuse an empty or oversized request."""
        parts = [part.strip().upper() for part in self.symbol.split(",") if part.strip()]
        if not parts:
            raise ProviderEngineError(BAD_QUERY, invalid=("symbol",))
        if len(parts) > 8:
            raise ProviderEngineError(BAD_QUERY, invalid=("symbol",))
        return parts


def _moment(value: object) -> datetime | None:
    """Parse an ISO timestamp with a UTC offset, the only form the envelope publishes."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProviderEngineError(SHAPE_INVALID)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProviderEngineError(SHAPE_INVALID) from None


def _day(value: object) -> date:
    """Parse the period end date; the row declares it non-null."""
    if not isinstance(value, str) or not value:
        raise ProviderEngineError(SHAPE_INVALID)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        raise ProviderEngineError(SHAPE_INVALID) from None


def _number(value: object, *, integer: bool) -> float | int | None:
    """Read ``dataitemvalue``: the source sends numeric strings, and a boolean is a corrupt cell."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ProviderEngineError(BAD_NUMERIC)
    if isinstance(value, str):
        try:
            number: float = float(value)
        except ValueError:
            raise ProviderEngineError(BAD_NUMERIC) from None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ProviderEngineError(BAD_NUMERIC)
    if integer:
        if not number.is_integer():
            raise ProviderEngineError(BAD_NUMERIC)
        return int(number)
    return number


def _int_cell(value: object) -> int | None:
    """A period-object integer, refused when present but not a whole number."""
    if value is None:
        return None
    number = _number(value, integer=True)
    return None if number is None else int(number)


class _SeekingAlphaForwardEstimatesFetcher(
    Fetcher[SeekingAlphaForwardEstimatesQuery, list[dict[str, Any]]]
):
    """Shared request and envelope-walking behavior for the two estimate rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None]
    items: ClassVar[tuple[str, ...]]
    row_model: ClassVar[type[ContractModel]]
    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> SeekingAlphaForwardEstimatesQuery:
        """Validate the documented query, including the forward-period bound."""
        query = SeekingAlphaForwardEstimatesQuery.model_validate(kwargs)
        if not 1 <= query.relative_periods <= MAX_RELATIVE_PERIODS:
            raise ProviderEngineError(BAD_QUERY, invalid=("relative_periods",))
        query.resolved_symbols()
        return query

    def _get(self, path: str, params: dict[str, str], ctx: FetchContext) -> dict[str, Any]:
        """One unauthenticated GET that must answer with a JSON object."""
        timeout = min(float(ctx.timeout or TIMEOUT_SECONDS), TIMEOUT_SECONDS)
        response = get_shared_http_client().get(
            f"{BASE_URL}{path}",
            params=params,
            timeout=timeout,
            source=SOURCE,
            headers={"Accept": "application/json"},
            cache_context={"canonical_model": self.canonical_model},
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise ProviderEngineError(HTTP_ERROR, status=status, url=f"{BASE_URL}{path}")
        try:
            payload: object = response.json()
        except Exception:
            raise ProviderEngineError(SHAPE_INVALID) from None
        if not isinstance(payload, dict):
            raise ProviderEngineError(SHAPE_INVALID)
        return payload

    def _resolve_id(self, symbol: str, ctx: FetchContext) -> str:
        """Ask the public search route for the numeric id the estimates route insists on."""
        payload = self._get(
            SEARCH_PATH,
            {"filter[phrase]": symbol, "count": "1", "type": "ticker"},
            ctx,
        )
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ProviderEngineError(ID_UNRESOLVED)
        identifier = data[0].get("id")
        if not isinstance(identifier, (str, int)) or not str(identifier).strip().isdigit():
            raise ProviderEngineError(ID_UNRESOLVED)
        return str(identifier)

    def extract_data(
        self,
        params: SeekingAlphaForwardEstimatesQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Resolve every symbol, then read its per-item series in one estimates call each."""
        period_type = "annual" if params.period == "annual" else "quarterly"
        relative = ",".join(str(step) for step in range(params.relative_periods + 1))
        collected: list[dict[str, Any]] = []
        for symbol in params.resolved_symbols():
            sa_id = self._resolve_id(symbol, ctx)
            body = self._get(
                ESTIMATES_PATH,
                {
                    "ticker_ids": sa_id,
                    "period_type": period_type,
                    "relative_periods": relative,
                    "estimates_data_items": ",".join(self.items),
                },
                ctx,
            )
            estimates = body.get("estimates")
            if not isinstance(estimates, dict):
                raise ProviderEngineError(SHAPE_INVALID)
            collected.append(
                {"symbol": symbol, "sa_ticker_id": sa_id, "series": estimates.get(sa_id)}
            )
        return collected

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: SeekingAlphaForwardEstimatesQuery,
    ) -> FetchResult:
        """Transpose per-item series into one row per ``(relative_period, period_end_date)``."""
        rows: list[ContractModel] = []
        for entry in raw:
            rows.extend(self._rows_for(entry))
        if not rows:
            raise ProviderEngineError(EMPTY)
        return tuple(rows)

    def _rows_for(self, entry: dict[str, Any]) -> list[ContractModel]:
        """Build the rows of one symbol, refusing a series that is not the documented mapping."""
        symbol = str(entry["symbol"])
        sa_id = str(entry["sa_ticker_id"])
        series = entry["series"]
        if series is None:
            return []
        if not isinstance(series, dict):
            raise ProviderEngineError(SHAPE_INVALID)
        cells: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for item in self.items:
            buckets = series.get(item)
            if buckets is None:
                continue
            if not isinstance(buckets, dict):
                raise ProviderEngineError(SHAPE_INVALID)
            for relative, records in sorted(buckets.items()):
                if not isinstance(records, list):
                    raise ProviderEngineError(SHAPE_INVALID)
                for record in records:
                    if not isinstance(record, dict):
                        raise ProviderEngineError(SHAPE_INVALID)
                    period = record.get("period")
                    if not isinstance(period, dict):
                        raise ProviderEngineError(SHAPE_INVALID)
                    end_date = _day(period.get("periodenddate"))
                    key = (str(relative), end_date.isoformat())
                    if key not in cells:
                        cells[key] = {
                            "symbol": symbol,
                            "sa_ticker_id": sa_id,
                            "relative_period": str(relative),
                            "period_type": str(period.get("periodtypeid") or "quarterly"),
                            "fiscal_year": _int_cell(period.get("fiscalyear")),
                            "fiscal_quarter": _int_cell(period.get("fiscalquarter")),
                            "calendar_year": _int_cell(period.get("calendaryear")),
                            "calendar_quarter": _int_cell(period.get("calendarquarter")),
                            "period_end_date": end_date,
                            "as_of": _moment(record.get("effectivedate")),
                        }
                        order.append(key)
                    cells[key][item] = _number(
                        record.get("dataitemvalue"), integer=item in _COUNT_ITEMS
                    )
        built: list[ContractModel] = []
        for key in order:
            cell = cells[key]
            for name in self.items:
                cell.setdefault(name, None)
            if cell["fiscal_year"] is None:
                raise ProviderEngineError(SHAPE_INVALID)
            try:
                built.append(self.row_model.model_validate(cell, strict=False))
            except ValidationError:
                raise ProviderEngineError(SHAPE_INVALID) from None
        return built


class SeekingAlphaForwardEpsEstimatesFetcher(_SeekingAlphaForwardEstimatesFetcher):
    """Forward EPS estimates: GAAP and normalized actual plus consensus low/mean/high."""

    canonical_model: ClassVar[str | None] = "ForwardEpsEstimates"
    items: ClassVar[tuple[str, ...]] = EPS_ITEMS
    row_model: ClassVar[type[SeekingAlphaForwardEpsEstimates]] = SeekingAlphaForwardEpsEstimates
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="seeking_alpha_forward_eps_estimates",
        period="series",
        market="us",
        source=SOURCE,
        verified=False,
        notes=(
            "Two public unauthenticated GETs: the search route resolves the numeric site id the "
            "estimates route requires, then per-item series are transposed into rows. Offline "
            "acceptance only: the live route answers a bot challenge without credentials."
        ),
    )


class SeekingAlphaForwardSalesEstimatesFetcher(_SeekingAlphaForwardEstimatesFetcher):
    """Forward revenue estimates: actual plus consensus low/mean/high and the estimate count."""

    canonical_model: ClassVar[str | None] = "ForwardSalesEstimates"
    items: ClassVar[tuple[str, ...]] = SALES_ITEMS
    row_model: ClassVar[type[SeekingAlphaForwardSalesEstimates]] = SeekingAlphaForwardSalesEstimates
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="seeking_alpha_forward_sales_estimates",
        period="series",
        market="us",
        source=SOURCE,
        verified=False,
        notes=(
            "Same two-request walk as the EPS row, over the five revenue_* data items. Offline "
            "acceptance only: the live route answers a bot challenge without credentials."
        ),
    )


__all__ = [
    "EPS_ITEMS",
    "SALES_ITEMS",
    "SeekingAlphaForwardEpsEstimatesFetcher",
    "SeekingAlphaForwardSalesEstimatesFetcher",
]
