"""Wave-39 ``cboe::FuturesCurve`` row: the dated settlement curve off Cboe's own CSV route.

The row was refused in ``opendata/data/providers/cboe/specs.py`` ``NOT_DECLARABLE`` with the reason
"The reviewed official All Access docs do not establish the fixed model's underlying list,
quote/history join, or curve output protocol." An unauthenticated artifact answers all three
halves of that refusal, and each is cited here rather than paraphrased:

* the curve itself -- one public ``text/csv`` table whose header is ``Product,Symbol,Expiration
  Date,Price``, ``HTTP/2 200``, 56 rows over 12 products, VX carrying 14 expiries from 2026-10-21
  through 2027-06-16, captured at
  ``docs/evidence/C144/wave39-unauthenticated-access-20261019/sources/cboe-futures-settlement-csv.csv``;
* the underlying list -- the ``Product`` column, so the row does not need a join to know what it is
  a curve for, and the public symbol book ``futures-roots.json`` (``HTTP/2 307`` then ``200``)
  publishes the roots and their underlyings;
* the history -- ``dt=`` is honoured: 2026-10-09, 2026-10-15 and 2026-10-16 each return their own
  bytes, and every response names the day in its ``content-disposition`` filename.

That last point corrects this round's own first reading. The only dated probe in the first take
asked for 2026-10-16, which is the day the route was already serving, so the byte comparison had no
way to come out different and concluded "``dt`` is ignored". A control that cannot differ
measures nothing; the re-take is in the same bundle and ``history_note``/``corrections`` carry
the retraction.

Two spelling facts are pinned because they are what was measured: the undated request is
``.../settlement/csv/`` (the route's own ``rel="canonical"`` link) and the dated request is
``.../settlement/csv?dt=<YYYY-MM-DD>``. The undated form carries no ``dt`` at all rather than a
guessed one, and the witness seam's URL matching is what makes keeping them distinct observable --
``GovernedHttpClient.get`` forwards ``params`` but the seam's fake matches on the ``url`` argument
alone (``scripts/quality/iter2_claims_probe_runner.py:302``), so the day belongs in the URL the
adapter builds, not in a params dict a witness cannot see.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date as date_type
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import ConfigDict, StrictBool, StrictInt, StrictStr, field_validator

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.cboe_futures_curve import CboeFuturesCurve
from opendata.data.protocol import (
    BaseQueryParams,
    FetchContext,
    Fetcher,
    FetchResult,
)
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.specs import CBOE_WEBSITE_BASE_URL
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

#: The public settlement table's path on the website origin. No credential is sent to it.
SETTLEMENT_PATH = "/us/futures/market_statistics/settlement/csv"

#: The four cells this decoder reads, spelled exactly as the captured header row spells them.
_REQUIRED_COLUMNS = ("Product", "Symbol", "Expiration Date", "Price")

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_ROWS = 5_000
_TIMEOUT_SECONDS = 30.0


class CboeFuturesCurveError(RuntimeError):
    """Sanitized transport, document or cell failure for the Cboe settlement curve."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        """Expose only a stable code and an optional HTTP status."""
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


class CboeFuturesCurveQuery(BaseQueryParams):
    """One product's settlement curve, optionally as of a named settlement day."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: StrictStr
    date: date_type | None = None
    limit: StrictInt | None = None
    use_cache: StrictBool | None = True
    source: Literal["auto", "cboe"] = "auto"

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: object) -> object:
        """Uppercase text symbols without coercing anything else."""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int | None) -> int | None:
        """Refuse a non-positive limit before any request is admitted."""
        if value is not None and value < 1:
            raise ValueError("limit must be positive")
        return value


def settlement_url(day: date_type | None) -> str:
    """Build the request URL the way the captured round spells it, day inside the URL."""
    origin = f"{CBOE_WEBSITE_BASE_URL}{SETTLEMENT_PATH}"
    if day is None:
        return f"{origin}/"
    return f"{origin}?dt={day.isoformat()}"


def _http_error_code(status: int | None) -> str:
    """Map a governed HTTP failure onto a provider-specific code."""
    if status == 403:
        return "CBOE_FUTURES_CURVE_FORBIDDEN"
    if status == 404:
        return "CBOE_FUTURES_CURVE_NOT_FOUND"
    if status == 429:
        return "CBOE_FUTURES_CURVE_RATE_LIMITED"
    if status is not None and status >= 500:
        return "CBOE_FUTURES_CURVE_UPSTREAM_ERROR"
    return "CBOE_FUTURES_CURVE_HTTP_ERROR"


def _fetch_table(day: date_type | None, *, use_cache: bool) -> str:
    """Read one settlement day off the public route through the governed, grant-checked client."""
    try:
        response = get_shared_http_client().get(
            settlement_url(day),
            params={},
            headers={"Accept": "text/csv"},
            timeout=(_TIMEOUT_SECONDS, _TIMEOUT_SECONDS),
            source=SOURCE,
            use_cache=use_cache,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise CboeFuturesCurveError(_http_error_code(exc.status), status=exc.status) from None
    except Exception:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_TRANSPORT_ERROR") from None
    body = getattr(response, "content", None)
    if not isinstance(body, bytes) or not body:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_RESPONSE")
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_RESPONSE_TOO_LARGE")
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_RESPONSE") from None


def _header_row(reader: list[str]) -> None:
    """Refuse a body whose header row is not the published settlement header."""
    missing = [name for name in _REQUIRED_COLUMNS if name not in reader]
    if missing:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_HEADER")


def _price(cell: object) -> float:
    """Map the published settlement price, refusing anything that is not a finite number.

    A settlement table either states a price or does not publish the expiry: every one of the 56
    rows in the 2026-10-16 capture carries a number, and a non-settlement day returns the header row
    alone rather than null-priced rows. So a blank, boolean or text cell here is a corrupt body, and
    ``StrictFloat`` would reject it anyway -- widening a ``True`` into a price of ``1.0`` is the
    failure this guard exists to prevent.
    """
    if isinstance(cell, bool) or not isinstance(cell, str) or not cell.strip():
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_NUMERIC_FIELD")
    try:
        value = float(cell.strip())
    except ValueError:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_NUMERIC_FIELD") from None
    if not math.isfinite(value):
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_NUMERIC_FIELD")
    return value


def _expiry(cell: object) -> date_type:
    """Parse the published ``YYYY-MM-DD`` expiry, refusing a cell that is not one."""
    if not isinstance(cell, str) or not cell.strip():
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_EXPIRATION")
    try:
        return date_type.fromisoformat(cell.strip())
    except ValueError:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_EXPIRATION") from None


def _cell(row: dict[str, Any], name: str) -> object:
    """Read one named cell, refusing a record the reader never keyed."""
    if name not in row:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_CSV")
    value: object = row[name]
    return value


def _parse_table(
    text: str, *, product: str, day: date_type | None, limit: int | None
) -> list[dict[str, object]]:
    """Select one product's settlements out of the published table."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not rows[0]:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_CSV")
    _header_row(rows[0])
    reader = csv.DictReader(io.StringIO(text))
    curve: list[dict[str, object]] = []
    seen_any = False
    for record in reader:
        seen_any = True
        cell = _cell(record, "Product")
        if not isinstance(cell, str) or not cell.strip():
            raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_PRODUCT_CELL")
        if cell.strip().upper() != product:
            continue
        curve.append(
            {
                "symbol": cell.strip().upper(),
                "date": day,
                "expiration": _expiry(_cell(record, "Expiration Date")),
                "price": _price(_cell(record, "Price")),
            }
        )
        if len(curve) > _MAX_ROWS:
            raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_RESPONSE_TOO_LARGE")
    if not seen_any:
        # The header row is there and nothing under it: a day the exchange did not settle.
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_EMPTY_TABLE")
    if not curve:
        raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_UNKNOWN_PRODUCT")
    if limit is not None:
        curve = curve[:limit]
    return curve


class CboeFuturesCurveFetcher(Fetcher[CboeFuturesCurveQuery, list[dict[str, object]]]):
    """Publish Cboe's own dated settlement table as one product's futures curve."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FuturesCurve"
    row_model: ClassVar[type[ContractModel]] = CboeFuturesCurve
    capability: ClassVar[Capability] = Capability(
        asset_class="futures",
        domain="cboe_futures_curve",
        period="snapshot",
        market="us",
        source=SOURCE,
        verified=False,
        notes=(
            "Official public Cboe settlement CSV; dt= selects the settlement day and the source "
            "names that day in its content-disposition filename. date is the validated request "
            "echo, because the table body carries no as-of cell and the offline witness seam "
            "cannot deliver response headers. symbol is the Product cell, not an echo."
        ),
    )

    def transform_query(self, **kwargs: object) -> CboeFuturesCurveQuery:
        """Validate the product and any named settlement day before any request is admitted."""
        return CboeFuturesCurveQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CboeFuturesCurveQuery,
        ctx: FetchContext,
    ) -> list[dict[str, object]]:
        """Fetch one settlement table and select the requested product's curve."""
        del ctx
        product = params.symbol.strip().upper()
        return _parse_table(
            _fetch_table(params.date, use_cache=params.use_cache is not False),
            product=product,
            day=params.date,
            limit=params.limit,
        )

    def transform_data(
        self,
        raw: list[dict[str, object]],
        params: CboeFuturesCurveQuery,
    ) -> FetchResult:
        """Validate every selected settlement against the Cboe curve contract."""
        del params
        try:
            return [CboeFuturesCurve.model_validate(row, strict=True) for row in raw]
        except Exception:
            raise CboeFuturesCurveError("CBOE_FUTURES_CURVE_BAD_ROW") from None


__all__ = [
    "SETTLEMENT_PATH",
    "CboeFuturesCurveError",
    "CboeFuturesCurveFetcher",
    "CboeFuturesCurveQuery",
    "settlement_url",
]
