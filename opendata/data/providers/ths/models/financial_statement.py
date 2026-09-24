"""Financial statement fetcher (domain ``financial_statement``, contract ``FinancialStatement``).

Wraps the fuyao (THS) three A-share statement endpoints through
:mod:`opendata_fuyao`. The endpoint answers one stock and one statement at
a time and returns **wide** rows (one row per report period, one column per
line item); the melt to the contract's long shape lives in
:func:`opendata_fuyao.endpoints.normalize_financial_statements`, so this
adapter only owns the query vocabulary and the row-identity check.

Item naming follows the contract rather than the disclosure's Chinese
headings: upstream already publishes normalized English codes
(``net_profit``, ``assets_total``, ...), which is exactly what
``FinancialStatement.item`` asks for. The stored ``statement_type`` is the
contract's ``income`` / ``balance`` / ``cashflow``, with the Chinese
disclosure names accepted as aliases - but the *reference* source in this
domain (the ported sina legs) stores Chinese item names and Chinese
statement types, so the two providers of one domain do not share a merge key
yet. That gap is deliberate and tracked: normalizing ~50 Chinese line items
per statement is a 口径 mapping that needs human review (AC-4), not something
to guess here.
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models import FinancialStatement
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import (
    ThsProviderError,
    client,
    resolve_code,
)

#: 中文披露名 → 契约 ``statement_type`` 取值；入库一律用右列。
_STATEMENT_ALIASES: dict[str, str] = {
    "income": "income",
    "利润表": "income",
    "balance": "balance",
    "资产负债表": "balance",
    "cashflow": "cashflow",
    "现金流量表": "cashflow",
}


class ThsFinancialStatementQuery(QueryParams):
    """Validated query for the financial statement domain."""

    symbol: str
    statement_type: str = "income"
    period: str = "annual"


class ThsFinancialStatementFetcher(
    Fetcher[ThsFinancialStatementQuery, tuple[FinancialStatement, ...]]
):
    """One A-share statement as a multi-period long-format series.

    ``verified`` is true because every line item that the contract shares
    with the other provider of this domain was compared value-by-value
    against that provider on live data, and the reported figures and their
    disclosure dates matched to the last decimal (see ``docs/evidence/C10``).

    ``revision`` is always ``1``: one response carries only the currently
    effective figure, so there is no restatement series to number.
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="financial_statement",
        period="Q",
        market="cn",
        source=SOURCE,
        verified=True,
    )

    def transform_query(self, **kwargs: object) -> ThsFinancialStatementQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required), ``statement_type`` (``income``
                / ``balance`` / ``cashflow``, Chinese names accepted),
                ``period`` (``annual`` / ``quarterly``), and an optional
                closed ``start_date`` / ``end_date`` report-period window.

        Returns:
            The validated query with the statement type normalized.

        Raises:
            ValidationError: On unknown fields.
            ThsProviderError: Unknown statement type or period.
        """
        query = ThsFinancialStatementQuery.model_validate(kwargs)
        if query.statement_type not in _STATEMENT_ALIASES:
            raise ThsProviderError("THS_STATEMENT_TYPE_UNSUPPORTED")
        if query.period not in ("annual", "quarterly"):
            raise ThsProviderError("THS_FINANCIAL_PERIOD_UNSUPPORTED")
        if (query.start_date is None) != (query.end_date is None):
            raise ThsProviderError("THS_FINANCIAL_WINDOW_INCOMPLETE")
        normalized = _STATEMENT_ALIASES[query.statement_type]
        if normalized != query.statement_type:
            return query.model_copy(update={"statement_type": normalized})
        return query

    def extract_data(
        self, params: ThsFinancialStatementQuery, ctx: FetchContext
    ) -> tuple[FinancialStatement, ...]:
        """Fetch one statement's window (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Long-format rows for the requested statement.

        Raises:
            ThsProviderError: Credentials missing or the code is not exactly
                one instrument in the upstream stock universe.
        """
        from opendata_fuyao.endpoints import fetch_financial_statements

        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_code(active, params.symbol)
            return fetch_financial_statements(
                active,
                symbol=code,
                statement_type=params.statement_type,
                period=params.period,
                start=params.start_date,
                end=params.end_date,
            )

    def transform_data(
        self, raw: tuple[FinancialStatement, ...], params: ThsFinancialStatementQuery
    ) -> FetchResult:
        """Check row identity and order the series (the normalize stage).

        Args:
            raw: Rows from the extraction stage.
            params: The validated query.

        Returns:
            The same rows, ordered by ``(report_period, announce_date, item)``.

        Raises:
            ThsProviderError: The response is empty or carries another
                issuer's rows.
        """
        if not raw:
            raise ThsProviderError("THS_EMPTY_RESPONSE")
        expected = params.symbol.strip().upper().partition(".")[0]
        if any(row.symbol != expected for row in raw):
            raise ThsProviderError("THS_FINANCIAL_SYMBOL_MISMATCH")
        return tuple(sorted(raw, key=lambda r: (r.report_period, r.announce_date, r.item)))


__all__ = ["ThsFinancialStatementFetcher", "ThsFinancialStatementQuery"]
