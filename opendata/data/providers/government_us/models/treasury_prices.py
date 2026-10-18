"""U.S. Treasury open-market security prices from TreasuryDirect's price listing.

The row contract is the class this package already reserved for this identity:
:class:`opendata.data.models.government_us.TreasuryPrice` (``opendata/data/models/
government_us.py:155-175``).  The source is TreasuryDirect's published
``securityPriceDetail`` listing.  Every endpoint, payload field, request header and
column name below is transcribed from the pinned upstream declaration kept on this
machine as an **output-contract reference only** --
``openbb_platform/providers/government_us/openbb_government_us/models/treasury_prices.py``
(URL :66, headers :68-76, form payload :78-84, POST verb :86, the eight wire columns
:112-123, the echoed as-of date :124, the ``%m/%d/%Y`` date stamp :128, the CUSIP and
security-type selectors :139-144, the empty-document refusal :109-110).

**The one transport deviation, stated on the record.**  Upstream posts the four date
selectors plus ``fileType=csv&csv=CSV FORMAT`` as an
``application/x-www-form-urlencoded`` **body** (:78-86).  The governed transport this
repository ships has no form-body verb: ``GovernedHttpClient.request``
(``opendata/data/http_client.py:426-438``) accepts ``params``/``headers``/``json_body``
only, and ``providers/government_us/models/_client.py`` wraps GET (:42-79).  Neither
file is in this wave's editable set, so this leg sends the same selector names and the
same values through ``params`` on a POST.  Nothing in an offline witness can price that
difference -- the seam answers by URL substring, not by body -- so the claim this module
makes is the mapping, the guards and the refusal codes, **not** that TreasuryDirect
accepts query-string selectors.  A live binding also needs ``treasurydirect.gov`` in the
source-use policy's ``allowed_hosts`` (``opendata/data/source_policy.py:105``), which is
a runtime admission grant, and in the provider's witness ``CONFIG`` host list
(``scripts/quality/iter2_witnesses/government_us.py:311-316``), which this shard cannot
edit.

Field coverage: the source publishes eight columns and this mapper echoes the required
``TreasuryPrice.date`` (:124), so it writes nine keys.  Measured on the witnessed row, eight of
the contract's eighteen fields come out non-null -- ``call_date`` is blank on the listing for a
bill, so it stays ``None`` -- and ten stay null: ``call_date`` for this row plus
``issuer_name``, ``isin``, ``issue_date``, ``last_traded_date``, ``total_trades``, ``last_price``,
``highest_price``, ``lowest_price`` and ``ytm``, none of which any source this wave could evidence
publishes.  Upstream's ``read_csv(header=0)`` then renames the columns (:111-123), which
consumes the first **data** line as a header; this mapper reads the lines positionally
instead and drops a leading line only when it really is a header (its first cell is not
a nine-character CUSIP), so no security is silently lost.

The refusals are this provider's own typed error (``_client.GovernmentUSProviderError``)
with stable codes, all of them judgements about a document the source did serve, so each
is reachable by tampering the witnessed document: an HTTP status outside 2xx, an
over-sized body, an HTML answer where a CSV was asked for, an empty body, a line that
carries fewer than the eight published columns, a numeric cell that is not a number, an
unparsable date, a ``security_type`` selector no served row matches, and -- the one the
counterfact runs -- a row whose CUSIP is not the CUSIP the caller asked for.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import ClassVar, Literal, TypeVar
from urllib.parse import urlsplit

from pydantic import ConfigDict, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.domains import contract_model
from opendata.data.models.base import ContractModel
from opendata.data.models.government_us import TreasuryPrice
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.government_us.models import _client

TREASURY_PRICES_DOMAIN = "government_us_treasury_prices"
TREASURY_PRICES_URL = "https://treasurydirect.gov/GA-FI/FedInvest/securityPriceDetail"
TREASURY_PRICE_HOSTS = frozenset({"treasurydirect.gov"})
#: The eight columns the listing publishes, in wire order (upstream :112-123).
PRICE_COLUMNS: tuple[str, ...] = (
    "cusip",
    "security_type",
    "rate",
    "maturity_date",
    "call_date",
    "bid",
    "offer",
    "eod_price",
)
#: The selectors upstream puts in the form body (:78-84); sent as ``params`` here.
FORM_FIELDS: tuple[str, ...] = (
    "priceDateDay",
    "priceDateMonth",
    "priceDateYear",
    "fileType",
    "csv",
)
#: The page-context headers the listing checks (:72-74).  No rotating User-Agent: the
#: governed client owns its own identity and this row must not spoof one.
REQUEST_HEADERS: dict[str, str] = {
    "Accept": "text/csv,application/csv;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": "https://treasurydirect.gov/",
    "Origin": "https://treasurydirect.gov",
}
MAX_DATE_AGE_DAYS = 30
MAX_PRICE_ROWS = 5_000
#: The as-of type under the name the query field cannot shadow: a field literally called
#: ``date`` with a ``None`` default puts ``date = None`` in the class namespace, and pydantic
#: resolves the annotation there, so ``date | None`` evaluates as ``NoneType | NoneType``.
DayType = date

_ContractT = TypeVar("_ContractT", bound=ContractModel)


def _row_model(domain: str, model: type[_ContractT]) -> type[_ContractT]:
    """Bind the domain's contract class, refusing a mismatch at import time."""
    resolved = contract_model(domain)
    if resolved is not model:
        raise TypeError(f"{domain}: contract resolves to {resolved.__name__}, not {model.__name__}")
    return model


def _refuse(code: str, status: int | None = None) -> _client.GovernmentUSProviderError:
    """Return this provider's typed, code-bearing refusal."""
    return _client.GovernmentUSProviderError(code, status=status)


def last_business_day(today: date | None = None) -> date:
    """Return the most recent weekday, the as-of date the listing is published for."""
    day = today or date.today()
    while day.weekday() > 4:
        day -= timedelta(days=1)
    return day


class TreasuryPricesQuery(QueryParams):
    """Bounded TreasuryDirect price request by as-of date, CUSIP or security type."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source: Literal["government_us"] = "government_us"
    market: Literal["us"] = "us"
    date: DayType | None = None
    security_type: Literal["bill", "note", "bond", "tips", "frn"] | None = None
    cusip: str | None = None

    @field_validator("cusip")
    @classmethod
    def _validate_cusip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if len(value) != 9 or not value.isalnum():
            raise ValueError("CUSIP must be exactly nine alphanumeric characters")
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> TreasuryPricesQuery:
        if self.symbol is not None:
            raise ValueError("Treasury price queries use cusip, not symbol")
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("Treasury price queries carry one as-of date, not a window")
        if self.date is not None and self.date > date.today():
            raise ValueError("Treasury price as-of date cannot be in the future")
        return self


class TreasuryPricesFetcher(Fetcher[TreasuryPricesQuery, list[dict[str, object]]]):
    """Fetch TreasuryDirect published prices for one as-of date."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "TreasuryPrices"
    domain: ClassVar[str] = TREASURY_PRICES_DOMAIN
    query_type: ClassVar[type[TreasuryPricesQuery]] = TreasuryPricesQuery
    row_model: ClassVar[type[TreasuryPrice]] = _row_model(TREASURY_PRICES_DOMAIN, TreasuryPrice)
    capability: ClassVar[Capability] = Capability(
        asset_class="fixed_income",
        domain=TREASURY_PRICES_DOMAIN,
        period="snapshot",
        market="us",
        source="government_us",
        verified=False,
        notes=(
            "TreasuryDirect securityPriceDetail listing mapped to the reserved contract; "
            "form-body selectors sent as query parameters pending a governed POST-form verb."
        ),
    )

    def transform_query(self, **kwargs: object) -> TreasuryPricesQuery:
        """Validate the as-of date, CUSIP and security-type selectors."""
        if kwargs.get("date") is None:
            kwargs["date"] = last_business_day()
        return TreasuryPricesQuery.model_validate(kwargs)

    def extract_data(
        self, params: TreasuryPricesQuery, ctx: FetchContext
    ) -> list[dict[str, object]]:
        """Read one day's published price listing through the governed HTTP client."""
        parts = urlsplit(TREASURY_PRICES_URL)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in TREASURY_PRICE_HOSTS:
            raise _refuse("TREASURY_PRICE_URL_REJECTED")
        as_of = params.date or last_business_day()
        if (date.today() - as_of).days > MAX_DATE_AGE_DAYS:
            raise _refuse("TREASURY_PRICE_DATE_TOO_OLD")
        payload = {
            "priceDateDay": str(as_of.day),
            "priceDateMonth": str(as_of.month),
            "priceDateYear": str(as_of.year),
            "fileType": "csv",
            "csv": "CSV FORMAT",
        }
        timeout = (
            _client._DEFAULT_TIMEOUT
            if ctx.timeout is None
            else min(_client._DEFAULT_TIMEOUT, ctx.timeout)
        )
        try:
            response = _client.get_shared_http_client().request(
                "POST",
                TREASURY_PRICES_URL,
                params=payload,
                headers=REQUEST_HEADERS,
                timeout=timeout,
                source="government_us",
            )
        except _client.RequestBudgetError:
            raise
        except _client.HttpFetchError as exc:
            raise _refuse("GOV_US_HTTP_ERROR", exc.status) from None
        except Exception:
            raise _refuse("GOV_US_TRANSPORT_ERROR") from None
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise _refuse("TREASURY_PRICE_HTTP_ERROR", status)
        body: bytes = bytes(getattr(response, "content", b"") or b"")
        if len(body) > _client.DOCUMENT_RESPONSE_LIMIT:
            raise _refuse("TREASURY_PRICE_RESPONSE_TOO_LARGE")
        if not body.strip():
            raise _refuse("TREASURY_PRICE_EMPTY_DOCUMENT")
        declared = str(dict(getattr(response, "headers", {}) or {}).get("Content-Type", ""))
        content_type = declared.split(";", 1)[0].strip().lower()
        opener = body.lstrip().lower().startswith((b"<!doctype html", b"<html"))
        if "html" in content_type or opener:
            raise _refuse("TREASURY_PRICE_HTML_RESPONSE")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("iso-8859-1")
        rows = _read_listing(text)
        if not rows:
            raise _refuse("TREASURY_PRICE_EMPTY_DOCUMENT")
        return rows

    def transform_data(
        self, raw: list[dict[str, object]], params: TreasuryPricesQuery
    ) -> FetchResult:
        """Map published price rows into ``TreasuryPrice``, echoing the as-of date."""
        as_of = params.date or last_business_day()
        selected = _select(raw, params)
        if not selected:
            raise _refuse("TREASURY_PRICE_NO_MATCHING_ROW")
        result: list[TreasuryPrice] = []
        for row in selected:
            cusip = _text(row.get("cusip"))
            if cusip is None:
                raise _refuse("TREASURY_PRICE_BAD_COLUMN_COUNT")
            values: dict[str, object] = {
                "cusip": cusip,
                "security_type": _text(row.get("security_type")),
                "date": as_of,
            }
            for column in ("rate", "bid", "offer", "eod_price"):
                values[column] = _number(column, row.get(column))
            for column in ("maturity_date", "call_date"):
                values[column] = _day(column, row.get(column))
            try:
                result.append(TreasuryPrice.model_validate(values))
            except (TypeError, ValueError):
                raise _refuse("TREASURY_PRICE_BAD_ROW") from None
        return tuple(result)


def _select(raw: list[dict[str, object]], params: TreasuryPricesQuery) -> list[dict[str, object]]:
    """Keep the served rows the caller asked for; the selectors upstream applies (:139-144)."""
    rows = raw
    if params.cusip is not None:
        rows = [row for row in rows if _text(row.get("cusip")) == params.cusip]
    if params.security_type is not None:
        rows = [
            row
            for row in rows
            if params.security_type in str(_text(row.get("security_type")) or "").lower()
        ]
    return rows


def _read_listing(text: str) -> list[dict[str, object]]:
    """Split the published listing into the eight named columns, positionally."""
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and not _is_cusip(_first_cell(lines[0])):
        lines = lines[1:]
    if len(lines) > MAX_PRICE_ROWS:
        raise _refuse("TREASURY_PRICE_TOO_MANY_ROWS")
    rows: list[dict[str, object]] = []
    for line in lines:
        cells = next(csv.reader(io.StringIO(line)))
        if len(cells) != len(PRICE_COLUMNS):
            raise _refuse("TREASURY_PRICE_BAD_COLUMN_COUNT")
        rows.append(dict(zip(PRICE_COLUMNS, cells, strict=True)))
    return rows


def _first_cell(line: str) -> str:
    cells = next(csv.reader(io.StringIO(line)))
    return cells[0].strip() if cells else ""


def _is_cusip(value: str) -> bool:
    return len(value) == 9 and value.isalnum()


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(column: str, value: object) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = float(text)
    except ValueError:
        raise _refuse("TREASURY_PRICE_BAD_VALUE") from None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise _refuse("TREASURY_PRICE_BAD_VALUE")
    return parsed


def _day(column: str, value: object) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        month, day, year = (int(part) for part in text.split("/", 2))
        return date(year, month, day)
    except (TypeError, ValueError):
        raise _refuse("TREASURY_PRICE_BAD_DATE") from None


__all__ = [
    "FORM_FIELDS",
    "PRICE_COLUMNS",
    "TREASURY_PRICES_DOMAIN",
    "TREASURY_PRICES_URL",
    "TREASURY_PRICE_HOSTS",
    "TreasuryPricesFetcher",
    "TreasuryPricesQuery",
    "last_business_day",
]
